"""Operational report for a CRI environment, read through its public API.

Prints only non-secret information: accounts and their latest evaluation,
event counts, webhook delivery and receipt status, ingestion runs, audit-chain
verification and the safety controls.
"""

from __future__ import annotations

import argparse
import collections
import json

import httpx


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--admin-key", required=True)
    ap.add_argument("--request-backtest", default="")
    a = ap.parse_args()
    c = httpx.Client(base_url=a.url.rstrip("/"), timeout=60.0)
    sb = {"Authorization": f"Bearer {a.key}"}
    ad = {"Authorization": f"Bearer {a.admin_key}"}
    out: dict = {}
    out["controls"] = c.get("/platform/controls", headers=sb).json()
    out["models"] = c.get("/platform/models", headers=sb).json().get("running")
    accounts = []
    for acct in c.get("/monitoring/accounts", headers=sb).json().get("accounts", []):
        ref = acct["account_ref"]
        body = c.get(f"/monitoring/accounts/{ref}", headers=sb).json()
        full = body.get("account", body)
        ev = full.get("last_evaluation") or {}
        accounts.append({
            "account_ref": ref,
            "margin_state": full.get("last_margin_state"),
            "available_credit": full.get("last_available_credit"),
            "loan_principal": (full.get("loan") or {}).get("principal"),
            "current_ltv": ev.get("current_ltv"),
            "approved_ltv": ev.get("approved_ltv") or ev.get("max_ltv"),
            "maintenance_ltv": ev.get("maintenance_ltv"),
            "collateral_value": ev.get("collateral_value") or ev.get("total_collateral_value"),
            "warnings": list(full.get("last_market_data_warnings") or [])[:6],
            "missing_data": full.get("last_missing_data"),
            "last_checked_at": full.get("last_checked_at"),
            "evaluation_keys": sorted(ev)[:40] if isinstance(ev, dict) else type(ev).__name__,
        })
    out["accounts"] = accounts
    events = c.get("/monitoring/events?limit=500", headers=sb).json().get("events", [])
    out["event_counts"] = collections.Counter(e.get("event_type") for e in events)
    out["latest_events"] = [{k: e.get(k) for k in ("created_at", "account_ref", "event_type", "severity")}
                            for e in events[:12]]
    out["webhook_deliveries"] = collections.Counter(
        d["status"] for d in c.get("/webhooks/deliveries?limit=500", headers=sb).json().get("deliveries", []))
    receipts = c.get("/sandbox/webhook-receipts?limit=500", headers=sb).json().get("receipts", [])
    out["webhook_receipts"] = {"total": len(receipts),
                               "signature_valid": sum(1 for r in receipts if r["signature_valid"])}
    out["audit_chain_sandbox"] = c.get("/platform/audit/verify", headers=sb).json()
    out["audit_chain_operator"] = c.get("/admin/audit/verify", headers=ad).json()
    ops = c.get("/admin/operations", headers=ad).json()
    out["ingestion_runs"] = ops.get("ingestion_runs", [])[:6]
    out["latest_quotes"] = ops.get("latest_quotes")
    out["webhook_backlog"] = ops.get("webhook_backlog")
    out["shadow_summary"] = c.get("/shadow/summary?hours=168", headers=sb).json()
    out["shadow_disagreements"] = c.get("/shadow/disagreements", headers=sb).json().get("disagreements", [])
    if accounts:
        ref = accounts[0]["account_ref"]
        out["explanation_example"] = c.get(f"/explain/accounts/{ref}", headers=sb).json()
        out["shadow_example"] = c.get(f"/shadow/accounts/{ref}", headers=sb).json()
    out["backtests"] = c.get("/backtests", headers=sb).json().get("backtests", [])[:5]
    out["onboarding"] = {k: {"decision": v["decision"], "findings": v["findings"]}
                         for k, v in c.get("/admin/markets/onboarding", headers=ad).json().get("markets", {}).items()}
    out["console_status"] = c.get("/console/").status_code
    unauth = c.get("/monitoring/accounts").status_code
    cross = c.get("/admin/tenants", headers=sb).status_code
    lend = c.put("/admin/controls/lending_enabled", headers={**ad, "Idempotency-Key": "report-lending-probe"},
                 json={"value": True, "reason": "interlock probe from staging report"}).status_code
    out["security_probes"] = {"no_key_status": unauth, "sandbox_key_on_admin_status": cross,
                              "enable_lending_status": lend}
    if a.request_backtest and accounts:
        r = c.post("/backtests", headers={**sb, "Idempotency-Key": f"report-bt-{a.request_backtest}"},
                   json={"account_ref": a.request_backtest, "step": 5})
        out["backtest_requested"] = {"status": r.status_code, "body": r.json()}
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
