"""Reliable webhook delivery: transactional outbox, signed requests, retries.

A delivery row is written in the same transaction as the monitoring event, so
an event is never emitted without its deliveries or vice versa. Workers send
due deliveries with an HMAC-SHA256 signature, retry with exponential backoff
and mark a delivery dead after the maximum attempts. Delivery is at-least-once;
receivers deduplicate on ``X-CRI-Event-Id``.
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from app.monitoring.events import serialize_event
from app.monitoring.models import MonitoringEvent
from app.platform.db import Database, current_connection
from app.platform.settings import PlatformSettings

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger("cri.webhooks")

_pending_ids: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar("cri_new_deliveries", default=None)

LIQUIDATION_EVENT_PREFIXES = ("LIQUIDATION", "MARGIN_CALL", "RECOVERY")


class WebhookError(ValueError):
    pass


def validate_url(url: str, allow_http: bool = False) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" and not (allow_http and parsed.scheme == "http"):
        raise WebhookError("webhook URL must use https")
    if not parsed.hostname:
        raise WebhookError("webhook URL needs a host")
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise WebhookError(f"webhook host does not resolve: {parsed.hostname}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise WebhookError("webhook URL must resolve to public addresses only")


def sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return f"v1={mac}"


def register_endpoint(tenant_id: str, url: str, event_types: list[str], allow_http: bool = False) -> dict[str, Any]:
    validate_url(url, allow_http)
    endpoint_id = "whe_" + uuid.uuid4().hex[:16]
    secret = "whsec_" + secrets.token_urlsafe(32)
    current_connection().execute(
        "INSERT INTO webhook_endpoints (endpoint_id, tenant_id, url, signing_secret, event_types, active, created_at) "
        "VALUES (%s,%s,%s,%s,%s,1,%s)",
        (endpoint_id, tenant_id, url, secret, json.dumps(sorted(set(event_types))), datetime.now(UTC).isoformat()),
    )
    return {"endpoint_id": endpoint_id, "url": url, "event_types": sorted(set(event_types)), "signing_secret": secret}


def list_endpoints(tenant_id: str) -> list[dict[str, Any]]:
    rows = current_connection().fetchall(
        "SELECT endpoint_id, url, event_types, active, created_at FROM webhook_endpoints WHERE tenant_id=%s "
        "ORDER BY created_at", (tenant_id,))
    return [{**r, "event_types": json.loads(r["event_types"]), "active": bool(r["active"])} for r in rows]


def deactivate_endpoint(tenant_id: str, endpoint_id: str) -> bool:
    cur = current_connection().execute(
        "UPDATE webhook_endpoints SET active=0 WHERE tenant_id=%s AND endpoint_id=%s", (tenant_id, endpoint_id))
    return (cur.rowcount or 0) > 0


def event_payload(tenant_id: str, event: MonitoringEvent, execution_enabled: bool) -> dict[str, Any]:
    body = serialize_event(event)
    etype = str(body.get("event_type", ""))
    return {
        "id": event.event_id,
        "tenant_id": tenant_id,
        "type": etype,
        "created_at": body.get("created_at"),
        "data": body,
        # Liquidation-related messages are advisory until Tiva enables execution.
        "advisory_only": not execution_enabled,
        "execution_authorised": bool(execution_enabled) and etype.upper().startswith(LIQUIDATION_EVENT_PREFIXES),
    }


def enqueue_for_event(tenant_id: str, event: MonitoringEvent, execution_enabled: bool = False) -> list[str]:
    conn = current_connection()
    endpoints = conn.fetchall(
        "SELECT endpoint_id, event_types FROM webhook_endpoints WHERE tenant_id=%s AND active=1", (tenant_id,))
    etype = event.event_type.value if hasattr(event.event_type, "value") else str(event.event_type)
    payload = json.dumps(event_payload(tenant_id, event, execution_enabled), sort_keys=True, default=str)
    now = datetime.now(UTC).isoformat()
    created = []
    for ep in endpoints:
        types = json.loads(ep["event_types"])
        if types and etype not in types and "*" not in types:
            continue
        delivery_id = "whd_" + uuid.uuid4().hex[:20]
        conn.execute(
            "INSERT INTO webhook_deliveries (delivery_id, tenant_id, endpoint_id, event_id, payload, status, attempts, "
            "next_attempt_at, created_at) VALUES (%s,%s,%s,%s,%s,'pending',0,%s,%s) "
            "ON CONFLICT (endpoint_id, event_id) DO NOTHING",
            (delivery_id, tenant_id, ep["endpoint_id"], event.event_id, payload, now, now),
        )
        created.append(delivery_id)
    pending = _pending_ids.get()
    if pending is not None:
        pending.extend(created)
    return created


def collect_new_deliveries() -> tuple[list[str], contextvars.Token]:
    bucket: list[str] = []
    return bucket, _pending_ids.set(bucket)


def release_collector(token: contextvars.Token) -> None:
    _pending_ids.reset(token)


def backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(30 * (2 ** max(0, attempts - 1)), 6 * 3600))


def deliver_one(db: Database, delivery_id: str, settings: PlatformSettings,
                client: httpx.Client | None = None) -> dict[str, Any]:
    try:
        import httpx

        http_errors: tuple[type[BaseException], ...] = (httpx.HTTPError,)
    except ImportError:  # pragma: no cover - minimal local environments with an injected client
        httpx = None  # type: ignore[assignment]
        http_errors = (OSError,)

    with db.transaction("*") as conn:
        conn.advisory_lock(f"delivery:{delivery_id}")
        row = conn.fetchone(
            "SELECT d.delivery_id, d.tenant_id, d.event_id, d.payload, d.status, d.attempts, e.url, e.signing_secret, "
            "e.active FROM webhook_deliveries d JOIN webhook_endpoints e ON e.endpoint_id=d.endpoint_id "
            "WHERE d.delivery_id=%s FOR UPDATE", (delivery_id,))
        if row is None or row["status"] != "pending":
            return {"delivery_id": delivery_id, "skipped": True}
        attempts = int(row["attempts"]) + 1
        status_code: int | None = None
        error: str | None = None
        if not row["active"]:
            error = "endpoint deactivated"
        else:
            body = row["payload"].encode()
            ts = str(int(time.time()))
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "CRI-Webhooks/1",
                "X-CRI-Event-Id": row["event_id"],
                "X-CRI-Delivery-Id": row["delivery_id"],
                "X-CRI-Timestamp": ts,
                "X-CRI-Signature": sign(row["signing_secret"], ts, body),
            }
            try:
                validate_url(row["url"], settings.webhook_allow_http)
                http = client or httpx.Client(timeout=10.0, follow_redirects=False)
                try:
                    resp = http.post(row["url"], content=body, headers=headers)
                finally:
                    if client is None:
                        http.close()
                status_code = resp.status_code
                if not 200 <= resp.status_code < 300:
                    error = f"HTTP {resp.status_code}"
            except (*http_errors, WebhookError) as exc:  # type: ignore[misc]
                error = f"{type(exc).__name__}: {exc}"[:500]
        now = datetime.now(UTC)
        if error is None:
            conn.execute(
                "UPDATE webhook_deliveries SET status='delivered', attempts=%s, last_status_code=%s, last_error=NULL, "
                "delivered_at=%s WHERE delivery_id=%s", (attempts, status_code, now.isoformat(), delivery_id))
            return {"delivery_id": delivery_id, "status": "delivered", "attempts": attempts}
        dead = attempts >= settings.webhook_max_attempts or not row["active"]
        if dead:
            logger.error(json.dumps({"event": "webhook_dead_letter", "delivery_id": delivery_id,
                                     "tenant_id": row["tenant_id"], "attempts": attempts, "error": error}))
        conn.execute(
            "UPDATE webhook_deliveries SET status=%s, attempts=%s, last_status_code=%s, last_error=%s, "
            "next_attempt_at=%s WHERE delivery_id=%s",
            ("dead" if dead else "pending", attempts, status_code, error,
             (now + backoff(attempts)).isoformat(), delivery_id))
        return {"delivery_id": delivery_id, "status": "dead" if dead else "retrying", "attempts": attempts,
                "error": error}


def due_deliveries(db: Database, limit: int = 100) -> list[str]:
    with db.transaction("*") as conn:
        rows = conn.fetchall(
            "SELECT delivery_id FROM webhook_deliveries WHERE status='pending' AND next_attempt_at <= %s "
            "ORDER BY next_attempt_at LIMIT %s", (datetime.now(UTC).isoformat(), limit))
    return [r["delivery_id"] for r in rows]


def dispatch_due(db: Database, settings: PlatformSettings, client: httpx.Client | None = None) -> dict[str, Any]:
    results = [deliver_one(db, d, settings, client) for d in due_deliveries(db)]
    summary: dict[str, int] = {}
    for r in results:
        key = str(r.get("status", "skipped"))
        summary[key] = summary.get(key, 0) + 1
    return {"processed": len(results), "summary": summary}


def enqueue_cloud_tasks(delivery_ids: list[str], settings: PlatformSettings) -> int:
    """Fast path: one Cloud Task per new delivery. The scheduled sweep is the safety net."""
    if not delivery_ids or not (settings.tasks_queue and settings.worker_url and settings.tasks_invoker_sa):
        return 0
    import httpx

    from app.platform.gcp import access_token

    token = access_token()
    sent = 0
    with httpx.Client(timeout=10.0) as http:
        for delivery_id in delivery_ids:
            task = {
                "task": {
                    "httpRequest": {
                        "httpMethod": "POST",
                        "url": f"{settings.worker_url}/internal/webhooks/deliver/{delivery_id}",
                        "oidcToken": {"serviceAccountEmail": settings.tasks_invoker_sa,
                                      "audience": settings.worker_url},
                    }
                }
            }
            r = http.post(f"https://cloudtasks.googleapis.com/v2/{settings.tasks_queue}/tasks",
                          json=task, headers={"Authorization": f"Bearer {token}"})
            if r.status_code < 300:
                sent += 1
    return sent
