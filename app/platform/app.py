"""Production application: the engine API wrapped with the platform layer.

Entry point for Cloud Run: ``uvicorn app.platform.app:app``. The same image
runs as the public API service (CRI_SERVICE_ROLE=api) and as the private
worker service (CRI_SERVICE_ROLE=worker) that Cloud Scheduler, Cloud Tasks and
Pub/Sub invoke through Cloud Run IAM.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
import logging
import math
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware

from app.api import routes as engine_routes
from app.audit.logger import AuditLogger
from app.platform import controls as ctl
from app.platform import keys as keymod
from app.platform import webhooks
from app.platform.audit import DatabaseAuditLogger, append_audit, verify_chain
from app.platform.context import (
    SYSTEM,
    Principal,
    require_principal,
    reset_principal,
    set_principal,
)
from app.platform.db import Database, current_connection
from app.platform.schema import migrate
from app.platform.settings import PlatformSettings, load_settings
from app.platform.tenancy import TenantServices, events_proxy, service_proxy
from app.version import (
    API_VERSION,
    LIFECYCLE_MODEL_VERSION,
    MARKET_DATA_MODEL_VERSION,
    MONITORING_MODEL_VERSION,
    PORTFOLIO_ACTION_MODEL_VERSION,
    RISK_MODEL_VERSION,
)

logger = logging.getLogger("cri.platform")

PUBLIC_PATHS = {"/health", "/ready", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
IDEMPOTENCY_REQUIRED_PREFIXES = ("/monitoring/accounts", "/webhooks", "/policies", "/admin")
LENDING_PATHS = ("/credit/originate", "/credit/draw/check")
STALE_IN_PROGRESS = timedelta(minutes=10)


class _Rollback(Exception):
    def __init__(self, response: Response) -> None:
        self.response = response


def _json_error(status: int, detail: Any) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status)


class PlatformMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Any, platform: Platform) -> None:
        super().__init__(app)
        self.p = platform

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        path = request.url.path
        s = self.p.settings
        if path in PUBLIC_PATHS or (path.startswith("/console") and s.role == "api"):
            response = await call_next(request)
            if path.startswith("/console"):
                response.headers["Content-Security-Policy"] = (
                    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
                response.headers["X-Frame-Options"] = "DENY"
                response.headers["Referrer-Policy"] = "no-referrer"
                response.headers["Cache-Control"] = "no-store"
            return response
        if path.startswith("/sandbox/webhook-receiver/") and s.role == "api" and s.environment != "production":
            # Signature-verified test receiver; authentication is the HMAC signature.
            return await call_next(request)
        is_internal = path.startswith("/internal/")
        if s.role == "worker":
            if not is_internal:
                return _json_error(404, "not found")
            # Cloud Run IAM has already authenticated the caller (Scheduler,
            # Tasks or Pub/Sub service account); the service is not public.
            # Internal endpoints open one transaction per tenant themselves.
            token = set_principal(SYSTEM)
            try:
                return await call_next(request)
            finally:
                reset_principal(token)
        if is_internal:
            return _json_error(404, "not found")
        auth = request.headers.get("authorization", "")
        raw = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        principal = keymod.authenticate(self.p.db, raw, s.api_key_pepper) if raw else None
        if principal is None:
            return _json_error(401, "missing or invalid API key")
        method = request.method.upper()
        mutating = method in {"POST", "PUT", "PATCH", "DELETE"}
        act_as = request.headers.get("x-cri-tenant")
        if act_as and principal.role == "admin" and not path.startswith("/admin"):
            # Operator view of a tenant: reads, plus backtest requests. Every
            # use is recorded in the operator audit chain.
            if mutating and not path.startswith("/backtests"):
                return _json_error(403, "operators may only read tenant data or request backtests")
            with self.p.db.transaction("*") as conn:
                t = conn.fetchone("SELECT kind, status FROM tenants WHERE tenant_id=%s", (act_as,))
                if t is None:
                    return _json_error(404, "tenant not found")
                if mutating:
                    append_audit("operator_acted_as_tenant", {"tenant_id": act_as, "method": method, "path": path},
                                 tenant_id="*", actor=principal.actor)
            principal = Principal(tenant_id=act_as, role="admin", key_id=principal.key_id, tenant_kind=t["kind"])
        if path.startswith("/admin") and principal.role != "admin":
            return _json_error(403, "admin role required")
        if principal.role == "readonly" and mutating and not path.startswith(("/risk/", "/portfolio/", "/market-data/normalize")):
            return _json_error(403, "read-only key")
        with self.p.db.transaction("*"):
            controls = ctl.effective(s)
        if controls["maintenance_mode"] and mutating and not path.startswith("/admin"):
            return _json_error(503, "platform is in maintenance mode")
        if path.startswith(LENDING_PATHS) and not controls["lending_enabled"] and principal.tenant_kind != "sandbox":
            return _json_error(403, "lending is disabled on this deployment; use a sandbox tenant for testing")
        tx_tenant = "*" if path.startswith("/admin") else principal.tenant_id
        response = await self._run(request, call_next, principal, idem=mutating, tx_tenant=tx_tenant)
        if path.startswith(LENDING_PATHS) and not controls["lending_enabled"]:
            response.headers["X-CRI-Lending"] = "disabled-non-binding"
        response.headers["X-CRI-Environment"] = s.environment
        return response

    async def _run(self, request: Request, call_next: Any, principal: Principal, idem: bool,
                   tx_tenant: str) -> Response:
        path = request.url.path
        idem_key = request.headers.get("idempotency-key") if idem else None
        if idem and not idem_key and path.startswith(IDEMPOTENCY_REQUIRED_PREFIXES):
            return _json_error(428, "Idempotency-Key header is required for this endpoint")
        body = await request.body()
        request_hash = hashlib.sha256(request.method.encode() + b" " + path.encode() + b"\n" + body).hexdigest()
        if idem_key:
            if len(idem_key) > 200:
                return _json_error(422, "Idempotency-Key too long")
            replay = self._claim(principal, idem_key, request.method, path, request_hash)
            if replay is not None:
                return replay

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": body, "more_body": False}

        request._receive = receive  # body already consumed; replay it to the endpoint
        token = set_principal(principal)
        bucket, wtoken = webhooks.collect_new_deliveries()
        try:
            try:
                with self.p.db.transaction(tx_tenant):
                    response = await call_next(request)
                    content = b"".join([chunk async for chunk in response.body_iterator])
                    final = Response(content=content, status_code=response.status_code,
                                     headers={k: v for k, v in response.headers.items() if k.lower() != "content-length"},
                                     media_type=response.media_type)
                    if response.status_code >= 500:
                        raise _Rollback(final)
                    if idem_key:
                        current_connection().execute(
                            "UPDATE idempotency_keys SET status='completed', response_status=%s, response_body=%s, "
                            "response_content_type=%s, completed_at=%s WHERE tenant_id=%s AND idem_key=%s",
                            (final.status_code, content.decode("utf-8", "replace"),
                             response.headers.get("content-type", "application/json"),
                             datetime.now(UTC).isoformat(), principal.tenant_id, idem_key))
            except _Rollback as rb:
                if idem_key:
                    self._release(principal, idem_key)
                return rb.response
            except Exception:
                if idem_key:
                    self._release(principal, idem_key)
                raise
        finally:
            webhooks.release_collector(wtoken)
            reset_principal(token)
        if bucket:
            try:
                webhooks.enqueue_cloud_tasks(bucket, self.p.settings)
            except Exception:
                logger.warning("cloud tasks fast-path enqueue failed", exc_info=True)
        if idem_key:
            final.headers["Idempotency-Key"] = idem_key
        return final

    def _claim(self, principal: Principal, key: str, method: str, path: str, request_hash: str) -> Response | None:
        now = datetime.now(UTC)
        with self.p.db.transaction(principal.tenant_id) as conn:
            conn.advisory_lock(f"idem:{principal.tenant_id}:{key}")
            row = conn.fetchone(
                "SELECT request_sha256, status, response_status, response_body, response_content_type, created_at "
                "FROM idempotency_keys WHERE tenant_id=%s AND idem_key=%s FOR UPDATE", (principal.tenant_id, key))
            if row is None:
                conn.execute(
                    "INSERT INTO idempotency_keys (tenant_id, idem_key, method, path, request_sha256, status, created_at) "
                    "VALUES (%s,%s,%s,%s,%s,'in_progress',%s)",
                    (principal.tenant_id, key, method, path, request_hash, now.isoformat()))
                return None
            if row["request_sha256"] != request_hash:
                return _json_error(422, "Idempotency-Key was already used with a different request")
            if row["status"] == "completed":
                resp = Response(content=row["response_body"] or "", status_code=int(row["response_status"]),
                                media_type=row["response_content_type"] or "application/json")
                resp.headers["Idempotent-Replayed"] = "true"
                resp.headers["Idempotency-Key"] = key
                return resp
            created = row["created_at"]
            created_dt = datetime.fromisoformat(created) if isinstance(created, str) else created
            if now - created_dt < STALE_IN_PROGRESS:
                return _json_error(409, "a request with this Idempotency-Key is still in progress")
            conn.execute("UPDATE idempotency_keys SET created_at=%s WHERE tenant_id=%s AND idem_key=%s",
                         (now.isoformat(), principal.tenant_id, key))
            return None

    def _release(self, principal: Principal, key: str) -> None:
        with self.p.db.transaction(principal.tenant_id) as conn:
            conn.execute("DELETE FROM idempotency_keys WHERE tenant_id=%s AND idem_key=%s AND status='in_progress'",
                         (principal.tenant_id, key))


class Platform:
    def __init__(self, settings: PlatformSettings, db: Database) -> None:
        self.settings = settings
        self.db = db
        file_logger = AuditLogger(os.environ.get("CRI_FALLBACK_AUDIT_PATH", "./data/audit/audit_log.jsonl"))
        self.audit_logger = DatabaseAuditLogger(fallback=file_logger)
        engine_routes.engine.audit_logger = self.audit_logger
        engine_routes.lifecycle_engine.audit_logger = self.audit_logger
        engine_routes.audit_logger = self.audit_logger
        self.services = TenantServices(db, engine_routes.lifecycle_engine, self.audit_logger, self._on_event)
        engine_routes.monitoring_service = service_proxy(self.services)
        engine_routes.monitoring_event_repo = events_proxy(self.services)

    def _on_event(self, tenant_id: str, event: Any) -> None:
        if not ctl.effective(self.settings)["webhooks_enabled"]:
            return
        webhooks.enqueue_for_event(tenant_id, event, ctl.effective(self.settings)["liquidation_execution_enabled"])

    def register_model_versions(self) -> None:
        sha = os.environ.get("CRI_GIT_SHA", "unknown")
        versions = {
            "api": API_VERSION, "risk_model": RISK_MODEL_VERSION, "lifecycle_model": LIFECYCLE_MODEL_VERSION,
            "market_data_model": MARKET_DATA_MODEL_VERSION, "monitoring_model": MONITORING_MODEL_VERSION,
            "portfolio_action_model": PORTFOLIO_ACTION_MODEL_VERSION,
        }
        with self.db.transaction("*") as conn:
            for component, version in versions.items():
                conn.execute(
                    "INSERT INTO model_versions (component, version, git_sha, first_seen_at) VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT (component, version, git_sha) DO NOTHING",
                    (component, version, sha, datetime.now(UTC).isoformat()))


# ---------------------------------------------------------------- request models

class TenantCreate(BaseModel):
    tenant_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,62}$")
    name: str
    kind: str = Field(pattern=r"^(client|sandbox)$")


class KeyCreate(BaseModel):
    role: str = Field(pattern=r"^(client|readonly|admin)$")
    label: str = ""


class ControlUpdate(BaseModel):
    value: Any
    reason: str


class WebhookCreate(BaseModel):
    url: str
    event_types: list[str] = Field(default_factory=lambda: ["*"])


class DrillRecord(BaseModel):
    kind: str = Field(pattern=r"^(rollback|load_test|disaster_recovery|security|isolation_probe)$")
    source: str
    passed: bool | None = None
    result: dict[str, Any]


class BacktestCreate(BaseModel):
    account_ref: str
    models: list[str] | None = None
    step: int = Field(default=5, ge=1, le=21)


class PolicyCreate(BaseModel):
    policy_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,80}$")
    body: dict[str, Any]


def build_router(p: Platform) -> APIRouter:
    r = APIRouter()

    @r.get("/ready")
    def ready() -> dict[str, Any]:
        with p.db.transaction("*") as conn:
            version = conn.fetchone("SELECT MAX(version) AS v FROM schema_migrations")
        return {"status": "ready", "schema_version": version["v"] if version else None,
                "environment": p.settings.environment, "role": p.settings.role}

    @r.post("/sandbox/webhook-receiver/{tenant_id}")
    async def sandbox_receiver(tenant_id: str, request: Request) -> dict[str, Any]:
        if p.settings.environment == "production":
            raise HTTPException(404, "not found")
        body = await request.body()
        ts = request.headers.get("x-cri-timestamp", "")
        sig = request.headers.get("x-cri-signature", "")
        url = str(request.url).split("?")[0].replace("http://", "https://", 1)
        with p.db.transaction("*") as conn:
            ep = conn.fetchone("SELECT signing_secret FROM webhook_endpoints WHERE tenant_id=%s AND url=%s AND active=1",
                               (tenant_id, url))
            valid = ep is not None and hmac.compare_digest(webhooks.sign(ep["signing_secret"], ts, body), sig)
            try:
                event_type = json.loads(body).get("type")
            except ValueError:
                event_type = None
            conn.execute(
                "INSERT INTO sandbox_webhook_receipts (receipt_id, tenant_id, event_id, delivery_id, signature_valid, "
                "received_at, event_type) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                ("rcpt_" + uuid.uuid4().hex[:16], tenant_id, request.headers.get("x-cri-event-id", ""),
                 request.headers.get("x-cri-delivery-id", ""), 1 if valid else 0, datetime.now(UTC).isoformat(),
                 event_type))
        if not valid:
            raise HTTPException(401, "invalid signature")
        return {"received": True}

    @r.get("/sandbox/webhook-receipts")
    def sandbox_receipts(limit: int = 50) -> dict[str, Any]:
        rows = current_connection().fetchall(
            "SELECT event_id, delivery_id, event_type, signature_valid, received_at FROM sandbox_webhook_receipts "
            "WHERE tenant_id=%s ORDER BY received_at DESC LIMIT %s", (require_principal().tenant_id, min(limit, 500)))
        return {"receipts": rows}

    @r.get("/platform/whoami")
    def whoami() -> dict[str, Any]:
        pr = require_principal()
        return {"tenant_id": pr.tenant_id, "role": pr.role, "key_id": pr.key_id, "tenant_kind": pr.tenant_kind,
                "environment": p.settings.environment}

    @r.get("/platform/controls")
    def get_controls() -> dict[str, Any]:
        return {"controls": ctl.effective(p.settings),
                "deployment_permits": {"lending": p.settings.lending_permitted,
                                       "liquidation_execution": p.settings.execution_permitted}}

    @r.get("/platform/models")
    def models() -> dict[str, Any]:
        rows = current_connection().fetchall(
            "SELECT component, version, git_sha, first_seen_at FROM model_versions ORDER BY first_seen_at DESC")
        return {"running": {"git_sha": os.environ.get("CRI_GIT_SHA", "unknown"), "risk_model": RISK_MODEL_VERSION,
                            "monitoring_model": MONITORING_MODEL_VERSION}, "history": rows}

    @r.get("/platform/audit")
    def tenant_audit(limit: int = 100) -> dict[str, Any]:
        pr = require_principal()
        rows = current_connection().fetchall(
            "SELECT audit_id, actor, action, created_at, payload, record_hash FROM audit_log WHERE tenant_id=%s "
            "ORDER BY seq DESC LIMIT %s", (pr.tenant_id, max(1, min(limit, 1000))))
        return {"records": [{**x, "payload": json.loads(x["payload"])} for x in rows]}

    @r.get("/platform/audit/verify")
    def tenant_audit_verify() -> dict[str, Any]:
        return verify_chain(require_principal().tenant_id)

    # ------------------------------------------------------------ shadow models
    @r.get("/shadow/models")
    def shadow_models() -> dict[str, Any]:
        from app.platform import shadow

        return {"baseline": "baseline", "models": list(shadow.MODELS), "descriptions": {
            "baseline": "Deployed engine (main) with the confirmed financial-correctness fixes; drives monitoring",
            "cre_v070": "CRI v0.7.0 recoverability engine with the same fixes (frozen snapshot, shadow only)",
            "A": "Research A: parametric Student-t expected shortfall haircut, 99%",
            "B": "Research B: filtered historical simulation expected shortfall, 99%",
            "B_gap": "Research B_gap: FHS at 99.5% with stressed sampling and empirical gap term",
            "C_balanced": "Research C: stochastic-recovery capacity meeting the balanced loss appetite",
        }, "lending_effect": "none: shadow results never drive state, events, webhooks or orders"}

    @r.get("/shadow/summary")
    def shadow_summary(hours: int = 168) -> dict[str, Any]:
        from app.platform import shadow

        since = datetime.now(UTC) - timedelta(hours=max(1, min(hours, 24 * 90)))
        return shadow.summary(require_principal().tenant_id, since)

    @r.get("/shadow/accounts/{account_ref}")
    def shadow_account(account_ref: str) -> dict[str, Any]:
        from app.platform import shadow

        rows = shadow.latest_by_account(require_principal().tenant_id, account_ref)
        return {"account_ref": account_ref, "evaluations": rows, "disagreements": shadow.disagreements(rows)}

    @r.get("/shadow/disagreements")
    def shadow_disagreements() -> dict[str, Any]:
        from app.platform import shadow

        tenant = require_principal().tenant_id
        refs = current_connection().fetchall(
            "SELECT DISTINCT account_ref FROM shadow_evaluations WHERE tenant_id=%s ORDER BY account_ref", (tenant,))
        out = []
        for x in refs:
            out.extend(shadow.disagreements(shadow.latest_by_account(tenant, x["account_ref"])))
        return {"disagreements": out}

    @r.get("/shadow/history/{account_ref}")
    def shadow_history(account_ref: str, model_id: str | None = None, limit: int = 500) -> dict[str, Any]:
        sql = ("SELECT model_id, evaluated_at, approved_credit, available_credit, capacity_ltv, risk_state, "
               "margin_call, estimated_recovery, collateral_value, loan_balance, error FROM shadow_evaluations "
               "WHERE tenant_id=%s AND account_ref=%s")
        params: list[Any] = [require_principal().tenant_id, account_ref]
        if model_id:
            sql += " AND model_id=%s"
            params.append(model_id)
        sql += " ORDER BY evaluated_at DESC LIMIT %s"
        params.append(max(1, min(limit, 5000)))
        return {"history": current_connection().fetchall(sql, params)}

    # ------------------------------------------------------ explanation (live)
    @r.get("/explain/accounts/{account_ref}")
    def explain_account(account_ref: str) -> dict[str, Any]:
        from app.platform import shadow

        pr = require_principal()
        account = p.services.service(pr.tenant_id).account_repo.get(account_ref)
        if account is None:
            raise HTTPException(404, "monitored account not found")
        inp = shadow.build_inputs(p.services, pr.tenant_id, account, datetime.now(UTC))
        res = shadow.baseline_model(inp)
        return {"account_ref": account_ref, "model_version": res.model_version, "risk_state": res.risk_state,
                "approved_credit": res.approved_credit, "available_credit": res.available_credit,
                "collateral_value": res.collateral_value, "loan_balance": res.loan_balance,
                "estimated_recovery": res.estimated_recovery, "proposed_liquidation": res.liquidation,
                "explanation": res.explanation,
                "note": "Computed now from the current persisted holdings, loan and latest ingested market data."}

    # ------------------------------------------------------ market data status
    @r.get("/platform/market-data/status")
    def market_data_status() -> dict[str, Any]:
        conn = current_connection()
        now = datetime.now(UTC)
        quotes = []
        for q in conn.fetchall("SELECT stable_key, quote, observed_at, source, ingested_at FROM market_quotes "
                               "WHERE quote_key = stable_key ORDER BY stable_key"):
            body = json.loads(q["quote"])["f"]
            obs = q["observed_at"]
            obs_dt = datetime.fromisoformat(obs) if isinstance(obs, str) else obs
            quotes.append({"stable_key": q["stable_key"], "price": body.get("local_price"),
                           "provider": body.get("provider_name"), "observed_at": q["observed_at"],
                           "age_hours": round((now - obs_dt).total_seconds() / 3600, 2),
                           "quality": body.get("data_quality_score"), "warnings": body.get("warnings"),
                           "volatility_30d": body.get("volatility_30d"),
                           "average_daily_volume": body.get("average_daily_volume"),
                           "ingested_at": q["ingested_at"]})
        coverage = conn.fetchall("SELECT stable_key, COUNT(*) AS sessions, MIN(bar_date) AS first_bar, "
                                 "MAX(bar_date) AS last_bar, SUM(range_reported) AS ranges_reported, "
                                 "SUM(volume_reported) AS volumes_reported FROM market_bars GROUP BY stable_key "
                                 "ORDER BY stable_key")
        runs = conn.fetchall("SELECT run_id, started_at, finished_at, instruments, quotes_written, errors "
                             "FROM ingestion_runs ORDER BY started_at DESC LIMIT 48")
        providers: dict[str, dict[str, Any]] = {}
        for run in runs:
            for e in json.loads(run["errors"] or "[]"):
                providers.setdefault(e.get("instrument", "?"), {"errors": 0})["errors"] += 1
        return {"quotes": quotes, "coverage": coverage, "ingestion_runs": runs[:12],
                "runs_last_48": len(runs), "error_counts": providers}

    # Webhooks
    @r.post("/webhooks/endpoints", status_code=201)
    def create_webhook(req: WebhookCreate) -> dict[str, Any]:
        pr = require_principal()
        try:
            out = webhooks.register_endpoint(pr.tenant_id, req.url, req.event_types, p.settings.webhook_allow_http)
        except webhooks.WebhookError as exc:
            raise HTTPException(422, str(exc)) from exc
        append_audit("webhook_endpoint_registered", {"endpoint_id": out["endpoint_id"], "url": req.url,
                                                     "event_types": out["event_types"]})
        return out

    @r.get("/webhooks/endpoints")
    def list_webhooks() -> dict[str, Any]:
        return {"endpoints": webhooks.list_endpoints(require_principal().tenant_id)}

    @r.delete("/webhooks/endpoints/{endpoint_id}")
    def delete_webhook(endpoint_id: str) -> dict[str, Any]:
        pr = require_principal()
        if not webhooks.deactivate_endpoint(pr.tenant_id, endpoint_id):
            raise HTTPException(404, "endpoint not found")
        append_audit("webhook_endpoint_deactivated", {"endpoint_id": endpoint_id})
        return {"endpoint_id": endpoint_id, "active": False}

    @r.get("/webhooks/deliveries")
    def list_deliveries(status: str | None = None, limit: int = 100) -> dict[str, Any]:
        pr = require_principal()
        sql = ("SELECT delivery_id, endpoint_id, event_id, status, attempts, next_attempt_at, last_status_code, "
               "last_error, created_at, delivered_at FROM webhook_deliveries WHERE tenant_id=%s")
        params: list[Any] = [pr.tenant_id]
        if status:
            sql += " AND status=%s"
            params.append(status)
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(max(1, min(limit, 500)))
        return {"deliveries": current_connection().fetchall(sql, params)}

    # Backtests (Cloud Run job; results persisted)
    @r.post("/backtests", status_code=202)
    def create_backtest(req: BacktestCreate) -> dict[str, Any]:
        from app.platform import backtest, shadow

        pr = require_principal()
        models = [m for m in (req.models or backtest.DEFAULT_MODELS) if m in shadow.MODELS]
        if not p.services.service(pr.tenant_id).account_repo.get(req.account_ref):
            raise HTTPException(404, "monitored account not found")
        run_id = "btr_" + uuid.uuid4().hex[:16]
        current_connection().execute(
            "INSERT INTO backtest_runs (run_id, tenant_id, account_ref, requested_by, models, params, status, "
            "created_at) VALUES (%s,%s,%s,%s,%s,%s,'queued',%s)",
            (run_id, pr.tenant_id, req.account_ref, pr.actor, json.dumps(models), json.dumps({"step": req.step}),
             datetime.now(UTC).isoformat()))
        append_audit("backtest_requested", {"run_id": run_id, "account_ref": req.account_ref, "models": models})
        execution = None
        if p.settings.gcp_project:
            from app.platform import gcp

            try:
                op = gcp.run_job(p.settings.gcp_project, p.settings.gcp_region, "cri-backtest",
                                 ["-m", "app.platform.cli", "backtest", run_id])
                execution = (op.get("metadata") or {}).get("name") or op.get("name")
            except Exception as exc:  # noqa: BLE001 - stays queued; the pending-backtest job picks it up
                execution = f"not started: {type(exc).__name__}: {str(exc)[:200]}"
            current_connection().execute("UPDATE backtest_runs SET execution=%s WHERE run_id=%s", (execution, run_id))
        return {"run_id": run_id, "status": "queued", "models": models, "execution": execution}

    @r.get("/backtests")
    def list_backtests(limit: int = 50) -> dict[str, Any]:
        rows = current_connection().fetchall(
            "SELECT run_id, account_ref, requested_by, models, status, created_at, started_at, finished_at, summary, "
            "error FROM backtest_runs WHERE tenant_id=%s ORDER BY created_at DESC LIMIT %s",
            (require_principal().tenant_id, max(1, min(limit, 200))))
        for x in rows:
            x["models"] = json.loads(x["models"])
            x["summary"] = json.loads(x["summary"]) if x["summary"] else None
        return {"backtests": rows}

    @r.get("/backtests/{run_id}")
    def get_backtest(run_id: str) -> dict[str, Any]:
        row = current_connection().fetchone("SELECT * FROM backtest_runs WHERE tenant_id=%s AND run_id=%s",
                                            (require_principal().tenant_id, run_id))
        if row is None:
            raise HTTPException(404, "backtest not found")
        for k in ("models", "params", "summary", "series"):
            row[k] = json.loads(row[k]) if row.get(k) else None
        return row

    # Versioned lending policies
    @r.post("/policies", status_code=201)
    def create_policy(req: PolicyCreate) -> dict[str, Any]:
        pr = require_principal()
        conn = current_connection()
        conn.advisory_lock(f"policy:{pr.tenant_id}:{req.policy_id}")
        row = conn.fetchone("SELECT MAX(version) AS v FROM lending_policies WHERE tenant_id=%s AND policy_id=%s",
                            (pr.tenant_id, req.policy_id))
        version = int(row["v"] or 0) + 1 if row else 1
        body = json.dumps(req.body, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(body.encode()).hexdigest()
        conn.execute(
            "INSERT INTO lending_policies (tenant_id, policy_id, version, body, body_sha256, status, created_by, "
            "created_at) VALUES (%s,%s,%s,%s,%s,'draft',%s,%s)",
            (pr.tenant_id, req.policy_id, version, body, digest, pr.actor, datetime.now(UTC).isoformat()))
        append_audit("lending_policy_version_created", {"policy_id": req.policy_id, "version": version,
                                                        "body_sha256": digest})
        return {"policy_id": req.policy_id, "version": version, "status": "draft", "body_sha256": digest}

    @r.get("/policies")
    def list_policies() -> dict[str, Any]:
        rows = current_connection().fetchall(
            "SELECT policy_id, version, status, body_sha256, created_by, created_at, approved_by, approved_at, body "
            "FROM lending_policies WHERE tenant_id=%s ORDER BY policy_id, version", (require_principal().tenant_id,))
        return {"policies": [{**x, "body": json.loads(x["body"])} for x in rows]}

    # Administration (Tiva staff keys only)
    @r.post("/admin/tenants", status_code=201)
    def admin_create_tenant(req: TenantCreate) -> dict[str, Any]:
        conn = current_connection()
        if conn.fetchone("SELECT tenant_id FROM tenants WHERE tenant_id=%s", (req.tenant_id,)):
            raise HTTPException(409, "tenant exists")
        keymod.create_tenant(conn, req.tenant_id, req.name, req.kind)
        key_id, raw = keymod.issue_key(conn, req.tenant_id, "client", "initial", p.settings.api_key_pepper,
                                       p.settings.environment)
        append_audit("tenant_created", {"tenant_id": req.tenant_id, "kind": req.kind, "key_id": key_id},
                     tenant_id="*")
        return {"tenant_id": req.tenant_id, "kind": req.kind, "key_id": key_id, "api_key": raw,
                "note": "store this key now; it is not retrievable later"}

    @r.get("/admin/tenants")
    def admin_list_tenants() -> dict[str, Any]:
        return {"tenants": current_connection().fetchall(
            "SELECT tenant_id, name, kind, status, created_at FROM tenants ORDER BY tenant_id")}

    @r.post("/admin/tenants/{tenant_id}/keys", status_code=201)
    def admin_issue_key(tenant_id: str, req: KeyCreate) -> dict[str, Any]:
        conn = current_connection()
        if not conn.fetchone("SELECT tenant_id FROM tenants WHERE tenant_id=%s", (tenant_id,)):
            raise HTTPException(404, "tenant not found")
        key_id, raw = keymod.issue_key(conn, tenant_id, req.role, req.label, p.settings.api_key_pepper,
                                       p.settings.environment)
        append_audit("api_key_issued", {"tenant_id": tenant_id, "key_id": key_id, "role": req.role}, tenant_id="*")
        return {"key_id": key_id, "api_key": raw, "role": req.role}

    @r.delete("/admin/keys/{key_id}")
    def admin_revoke_key(key_id: str) -> dict[str, Any]:
        cur = current_connection().execute(
            "UPDATE api_keys SET revoked_at=%s WHERE key_id=%s AND revoked_at IS NULL",
            (datetime.now(UTC).isoformat(), key_id))
        if not cur.rowcount:
            raise HTTPException(404, "active key not found")
        append_audit("api_key_revoked", {"key_id": key_id}, tenant_id="*")
        return {"key_id": key_id, "revoked": True}

    @r.post("/admin/tenants/{tenant_id}/status")
    def admin_tenant_status(tenant_id: str, req: ControlUpdate) -> dict[str, Any]:
        if req.value not in ("active", "suspended") or len(req.reason.strip()) < 10:
            raise HTTPException(422, "value must be active|suspended with a reason of at least 10 characters")
        cur = current_connection().execute("UPDATE tenants SET status=%s WHERE tenant_id=%s", (req.value, tenant_id))
        if not cur.rowcount:
            raise HTTPException(404, "tenant not found")
        append_audit("tenant_status_changed", {"tenant_id": tenant_id, "status": req.value, "reason": req.reason},
                     tenant_id="*")
        return {"tenant_id": tenant_id, "status": req.value}

    @r.put("/admin/controls/{key}")
    def admin_set_control(key: str, req: ControlUpdate) -> dict[str, Any]:
        try:
            values = ctl.set_control(p.settings, key, req.value, req.reason, require_principal().actor)
        except ctl.ControlError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"controls": values}

    @r.post("/admin/policies/{tenant_id}/{policy_id}/{version}/approve")
    def admin_approve_policy(tenant_id: str, policy_id: str, version: int, req: ControlUpdate) -> dict[str, Any]:
        pr = require_principal()
        conn = current_connection()
        row = conn.fetchone("SELECT status, created_by FROM lending_policies WHERE tenant_id=%s AND policy_id=%s "
                            "AND version=%s", (tenant_id, policy_id, version))
        if row is None:
            raise HTTPException(404, "policy version not found")
        if row["status"] != "draft":
            raise HTTPException(409, f"policy version is {row['status']}")
        if row["created_by"] == pr.actor:
            raise HTTPException(409, "the author of a policy version cannot approve it")
        if len(req.reason.strip()) < 10:
            raise HTTPException(422, "approval needs a reason of at least 10 characters")
        now = datetime.now(UTC).isoformat()
        conn.execute("UPDATE lending_policies SET status='retired' WHERE tenant_id=%s AND policy_id=%s AND "
                     "status='approved'", (tenant_id, policy_id))
        conn.execute("UPDATE lending_policies SET status='approved', approved_by=%s, approved_at=%s WHERE "
                     "tenant_id=%s AND policy_id=%s AND version=%s", (pr.actor, now, tenant_id, policy_id, version))
        append_audit("lending_policy_approved", {"tenant_id": tenant_id, "policy_id": policy_id, "version": version,
                                                 "reason": req.reason}, tenant_id=tenant_id)
        return {"tenant_id": tenant_id, "policy_id": policy_id, "version": version, "status": "approved"}

    @r.post("/admin/drills", status_code=201)
    def admin_record_drill(req: DrillRecord) -> dict[str, Any]:
        drill_id = "drl_" + uuid.uuid4().hex[:16]
        current_connection().execute(
            "INSERT INTO operational_drills (drill_id, kind, environment, recorded_at, source, passed, result) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (drill_id, req.kind, p.settings.environment, datetime.now(UTC).isoformat(), req.source,
             None if req.passed is None else int(req.passed), json.dumps(req.result)))
        append_audit("operational_drill_recorded", {"drill_id": drill_id, "kind": req.kind, "passed": req.passed},
                     tenant_id="*")
        return {"drill_id": drill_id}

    @r.get("/admin/drills")
    def admin_list_drills(limit: int = 50) -> dict[str, Any]:
        rows = current_connection().fetchall(
            "SELECT drill_id, kind, environment, recorded_at, source, passed, result FROM operational_drills "
            "ORDER BY recorded_at DESC LIMIT %s", (max(1, min(limit, 500)),))
        for x in rows:
            x["result"] = json.loads(x["result"])
        return {"drills": rows}

    @r.get("/admin/infrastructure")
    def admin_infrastructure() -> dict[str, Any]:
        from app.platform import gcp

        if not p.settings.gcp_project:
            return {"available": False, "reason": "not running on Google Cloud"}
        return {"available": True, "project": p.settings.gcp_project, "region": p.settings.gcp_region,
                **gcp.infrastructure(p.settings.gcp_project, p.settings.gcp_region)}

    @r.get("/admin/markets/onboarding")
    def admin_market_onboarding() -> dict[str, Any]:
        from app.markets.hierarchical import prior_from_full_histories, shrink_log_vol
        from app.markets.onboarding import validate_market
        from app.markets.profiles import PROFILES
        from app.platform.ingestion import load_bars

        conn = current_connection()
        keys = [r["stable_key"] for r in conn.fetchall("SELECT DISTINCT stable_key FROM market_bars ORDER BY stable_key")]
        reports = {}
        log_vols: dict[str, dict[str, tuple[float, int]]] = {}
        for code, prof in PROFILES.items():
            mine = {k: load_bars(k, 1000) for k in keys if k.split(":")[0] in prof.exchanges}
            if not mine:
                continue
            reports[code] = validate_market(prof, mine)
            for k, bars in mine.items():
                closes = [b["close"] for b in bars if b["close"]]
                r = [math.log(b / a) for a, b in itertools.pairwise(closes) if a > 0 and b > 0]
                if len(r) > 20:
                    mu = sum(r) / len(r)
                    sd = math.sqrt(sum((x - mu) ** 2 for x in r) / (len(r) - 1))
                    log_vols.setdefault(code, {})[k] = (math.log(max(sd, 1e-6)), len(r))
        # Cross-market sharing illustration on live data: each market's
        # instruments shrunk toward a prior estimated from the OTHER market.
        sharing = {}
        for code, inst in log_vols.items():
            others = [v[0] for c, d in log_vols.items() if c != code for v in d.values()]
            if len(others) < 2:
                continue
            prior = prior_from_full_histories(others, f"other markets ({len(others)} instruments)")
            sharing[code] = {k: {"own_annual_vol": math.exp(lv) * math.sqrt(252), "sessions": n,
                                 "weight_on_own_with_full_history": shrink_log_vol(lv, n, prior)[1]}
                             for k, (lv, n) in inst.items()}
        return {"markets": reports, "cross_market_prior_weights": sharing}

    @r.get("/admin/research/findings")
    def admin_research_findings() -> Response:
        from pathlib import Path

        f = Path(__file__).resolve().parents[2] / "research/methodology_lab/results/real_data/REAL_DATA_FINDINGS.md"
        if not f.exists():
            raise HTTPException(404, "findings not packaged in this image")
        return Response(f.read_text(), media_type="text/markdown")

    @r.get("/admin/audit")
    def admin_audit(tenant_id: str = "*", limit: int = 100) -> dict[str, Any]:
        rows = current_connection().fetchall(
            "SELECT audit_id, tenant_id, actor, action, created_at, payload, record_hash FROM audit_log "
            "WHERE tenant_id=%s ORDER BY seq DESC LIMIT %s", (tenant_id, max(1, min(limit, 1000))))
        return {"records": [{**x, "payload": json.loads(x["payload"])} for x in rows]}

    @r.get("/admin/audit/verify")
    def admin_audit_verify(tenant_id: str = "*") -> dict[str, Any]:
        return verify_chain(tenant_id)

    @r.get("/admin/operations")
    def admin_operations() -> dict[str, Any]:
        conn = current_connection()
        return {
            "ingestion_runs": conn.fetchall("SELECT run_id, started_at, finished_at, instruments, quotes_written, "
                                            "errors FROM ingestion_runs ORDER BY started_at DESC LIMIT 10"),
            "webhook_backlog": conn.fetchall("SELECT status, COUNT(*) AS n FROM webhook_deliveries GROUP BY status"),
            "accounts_by_state": conn.fetchall("SELECT tenant_id, margin_state, COUNT(*) AS n FROM monitored_accounts "
                                               "GROUP BY tenant_id, margin_state ORDER BY tenant_id"),
            "latest_quotes": [
                {"stable_key": q["stable_key"], "price": json.loads(q["quote"])["f"]["local_price"],
                 "observed_at": q["observed_at"], "source": q["source"], "ingested_at": q["ingested_at"]}
                for q in conn.fetchall("SELECT stable_key, quote, observed_at, source, ingested_at FROM market_quotes "
                                       "WHERE quote_key = stable_key ORDER BY stable_key")
            ],
        }

    return r


def build_internal_router(p: Platform) -> APIRouter:
    r = APIRouter(prefix="/internal")

    def _controls() -> dict[str, Any]:
        with p.db.transaction("*"):
            return ctl.effective(p.settings)

    def _active_tenants() -> list[str]:
        with p.db.transaction("*") as conn:
            rows = conn.fetchall("SELECT tenant_id FROM tenants WHERE status='active' ORDER BY tenant_id")
        return [x["tenant_id"] for x in rows]

    def _per_tenant(fn: Any) -> list[dict[str, Any]]:
        out = []
        for tenant in _active_tenants():
            token = set_principal(Principal(tenant_id=tenant, role="system", key_id="worker"))
            try:
                with p.db.transaction(tenant):
                    out.append({"tenant_id": tenant, "result": fn(p.services.service(tenant))})
            except Exception as exc:  # noqa: BLE001 - one tenant's failure must not stop the rest
                out.append({"tenant_id": tenant, "error": str(exc)[:500]})
            finally:
                reset_principal(token)
        return out

    @r.post("/monitoring/tick-all")
    def tick_all() -> dict[str, Any]:
        if not _controls()["monitoring_enabled"]:
            return {"skipped": "monitoring_enabled is false"}
        results = _per_tenant(lambda svc: svc.tick_all())
        return {"tenants": len(results), "results": results}

    @r.post("/market-data/ingest")
    def ingest() -> dict[str, Any]:
        from app.platform import ingestion

        if not _controls()["ingestion_enabled"]:
            return {"skipped": "ingestion_enabled is false"}
        started = datetime.now(UTC)
        instruments = ingestion.universe(p.db)
        bars: dict[str, list[Any]] = {}
        quotes, errors = ingestion.fetch_quotes(instruments, bars_out=bars)
        if bars:
            try:
                ingestion.store_bars(p.db, bars, {k: q.provider_name or "unknown" for k, q in quotes.items()})
            except Exception as exc:  # noqa: BLE001 - quotes still update; bar storage failure is reported
                errors.append({"instrument": "market_bars", "error": str(exc)[:300]})
        if quotes:
            with p.db.transaction("*"):
                p.services.market_data_cache.merge(quotes, {}, "cri_ingestion", started)
        run_id = ingestion.record_run(p.db, started, len(instruments), len(quotes), errors)
        for err in errors:
            logger.error("market_data_ingestion_error %s", json.dumps({"run_id": run_id, **err}))
        keys = sorted(quotes)
        published = None
        if keys and p.settings.market_data_topic:
            from app.platform.gcp import publish

            published = publish(p.settings.market_data_topic, {"run_id": run_id, "instruments": keys})
        ticks: list[dict[str, Any]] = []
        if keys and not published and _controls()["monitoring_enabled"]:
            ticks = _per_tenant(lambda svc: svc.ingest_market_data_update({}, {}, keys, "cri_ingestion", True))
        return {"run_id": run_id, "instruments": len(instruments), "quotes": len(quotes), "errors": errors,
                "pubsub_message_id": published, "direct_ticks": len(ticks)}

    @r.post("/pubsub/market-data")
    async def pubsub_market_data(request: Request) -> dict[str, Any]:
        from app.platform.gcp import decode_push

        msg = decode_push(await request.json())
        keys = list(msg.get("instruments") or [])
        if not keys or not _controls()["monitoring_enabled"]:
            return {"skipped": True}
        results = _per_tenant(lambda svc: svc.ingest_market_data_update({}, {}, keys, "pubsub", True))
        return {"run_id": msg.get("run_id"), "tenants": len(results), "results": results}

    @r.post("/shadow/run-all")
    def shadow_run_all() -> dict[str, Any]:
        from app.platform import shadow

        if not _controls()["monitoring_enabled"]:
            return {"skipped": "monitoring_enabled is false"}
        return shadow.run_all(p.db, p.services)

    @r.post("/webhooks/dispatch")
    def dispatch() -> dict[str, Any]:
        return webhooks.dispatch_due(p.db, p.settings)

    @r.post("/webhooks/deliver/{delivery_id}")
    def deliver(delivery_id: str) -> dict[str, Any]:
        result = webhooks.deliver_one(p.db, delivery_id, p.settings)
        if result.get("status") == "retrying":
            # Let the sweep own retries with backoff rather than Cloud Tasks' own policy.
            return result
        return result

    return r


def create_app(settings: PlatformSettings | None = None, db: Database | None = None) -> FastAPI:
    settings = settings or load_settings()
    if settings.is_cloud and not settings.api_key_pepper:
        raise RuntimeError("CRI_API_KEY_PEPPER must be set in cloud environments")
    db = db or Database(settings.database_url)
    if os.environ.get("CRI_AUTO_MIGRATE", "true" if db.dialect == "sqlite" else "false").lower() == "true":
        migrate(db)
    platform = Platform(settings, db)
    try:
        platform.register_model_versions()
    except Exception:
        logger.warning("model version registration skipped", exc_info=True)
    application = FastAPI(
        title="Collateral Risk Intelligence",
        version=API_VERSION,
        description="CRI platform API. Lending and autonomous liquidation are disabled on this deployment.",
        docs_url=None if settings.role == "worker" else "/docs",
        openapi_url=None if settings.role == "worker" else "/openapi.json",
    )

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "environment": settings.environment, "role": settings.role,
                "git_sha": os.environ.get("CRI_GIT_SHA", "unknown")}

    if settings.role == "api":
        from pathlib import Path

        from fastapi.staticfiles import StaticFiles

        console_dir = Path(__file__).parent / "console"
        if console_dir.exists():
            application.mount("/console", StaticFiles(directory=console_dir, html=True), name="console")
    application.include_router(build_router(platform))
    application.include_router(build_internal_router(platform))
    application.include_router(engine_routes.router)
    application.add_middleware(PlatformMiddleware, platform=platform)
    application.state.platform = platform
    application.state.request_id = lambda: uuid.uuid4().hex
    return application


# Served with: uvicorn app.platform.app:create_app --factory
