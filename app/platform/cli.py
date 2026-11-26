"""Operational commands run as Cloud Run Jobs.

  python -m app.platform.cli migrate      apply schema migrations
  python -m app.platform.cli bootstrap    ensure the Tiva admin tenant and the keys held in Secret Manager
  python -m app.platform.cli ingest       one market-data ingestion pass (also exposed on the worker)
"""

from __future__ import annotations

import json
import os
import sys

from app.platform import keys as keymod
from app.platform.db import Database
from app.platform.schema import migrate
from app.platform.settings import load_settings


def _ensure_key(conn, tenant_id: str, name: str, kind: str, role: str, raw: str, pepper: str, label: str) -> str:
    if not conn.fetchone("SELECT tenant_id FROM tenants WHERE tenant_id=%s", (tenant_id,)):
        keymod.create_tenant(conn, tenant_id, name, kind)
    digest = keymod.hash_key(raw, pepper)
    row = conn.fetchone("SELECT key_id FROM api_keys WHERE key_hash=%s", (digest,))
    if row:
        return "exists"
    import uuid
    from datetime import UTC, datetime

    conn.execute(
        "INSERT INTO api_keys (key_id, tenant_id, key_hash, role, label, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
        ("key_" + uuid.uuid4().hex[:16], tenant_id, digest, role, label, datetime.now(UTC).isoformat()))
    return "created"


def bootstrap(db: Database) -> dict[str, str]:
    s = load_settings()
    out = {}
    admin = os.environ.get("CRI_BOOTSTRAP_ADMIN_KEY", "")
    sandbox = os.environ.get("CRI_BOOTSTRAP_SANDBOX_KEY", "")
    with db.transaction("*") as conn:
        if admin:
            if not admin.startswith(keymod.KEY_PREFIX):
                raise SystemExit("bootstrap admin key must start with cri_")
            out["admin"] = _ensure_key(conn, "tiva", "Tiva Finance (operator)", "internal", "admin", admin,
                                       s.api_key_pepper, "bootstrap-admin")
        if sandbox:
            out["sandbox"] = _ensure_key(conn, "tiva-sandbox", "Tiva staging sandbox", "sandbox", "client", sandbox,
                                         s.api_key_pepper, "bootstrap-sandbox")
    return out


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    s = load_settings()
    db = Database(s.database_url)
    try:
        if cmd == "migrate":
            print(json.dumps({"applied": migrate(db)}))
        elif cmd == "bootstrap":
            migrate(db)
            print(json.dumps(bootstrap(db)))
        elif cmd == "backtest":
            from app.platform import backtest
            from app.platform.app import Platform

            platform = Platform(s, db)
            print(json.dumps(backtest.run(db, platform.services, argv[2]), default=str)[:4000])
        elif cmd == "backtest-pending":
            from app.platform import backtest
            from app.platform.app import Platform

            platform = Platform(s, db)
            with db.transaction("*") as conn:
                queued = [r["run_id"] for r in conn.fetchall(
                    "SELECT run_id FROM backtest_runs WHERE status='queued' ORDER BY created_at")]
            done = {}
            for run_id in queued:
                try:
                    done[run_id] = backtest.run(db, platform.services, run_id)["models"]
                except Exception as exc:  # noqa: BLE001 - recorded as failed in the run row
                    done[run_id] = {"error": str(exc)[:300]}
            print(json.dumps(done, default=str)[:6000])
        elif cmd == "dr-verify":
            # Compare the live database with a restored clone reached at argv[2].
            import re as _re

            from app.platform.audit import verify_chain

            clone_url = _re.sub(r"@[^:/]+:", f"@{argv[2]}:", s.database_url, count=1)
            clone = Database(clone_url)
            tables = ["tenants", "api_keys", "monitored_accounts", "monitoring_events", "audit_log",
                      "idempotency_keys", "webhook_deliveries", "market_bars", "shadow_evaluations",
                      "schema_migrations"]
            report: dict = {"tables": {}}
            for name, d in (("live", db), ("clone", clone)):
                with d.transaction("*") as conn:
                    for t in tables:
                        row = conn.fetchone(f"SELECT COUNT(*) AS n FROM {t}")
                        n = row["n"] if row else None
                        report["tables"].setdefault(t, {})[name] = n
                    mrow = conn.fetchone("SELECT MAX(created_at) AS m FROM audit_log")
                    latest = mrow["m"] if mrow else None
                    report.setdefault("latest_audit_record", {})[name] = str(latest)
                    tenants = [r["tenant_id"] for r in conn.fetchall("SELECT DISTINCT tenant_id FROM audit_log")]
                    report.setdefault("audit_chains_valid", {})[name] = {t: verify_chain(t)["valid"] for t in tenants}
            clone.close()
            same = all(v["live"] == v["clone"] for k, v in report["tables"].items()
                       if k not in ("market_bars", "shadow_evaluations", "monitoring_events", "audit_log",
                                    "idempotency_keys", "webhook_deliveries"))
            chains_ok = all(report["audit_chains_valid"]["clone"].values())
            report["static_tables_identical"] = same
            report["clone_audit_chains_valid"] = chains_ok
            import uuid as _uuid
            from datetime import UTC, datetime

            with db.transaction("*") as conn:
                conn.execute(
                    "INSERT INTO operational_drills (drill_id, kind, environment, recorded_at, source, passed, result) "
                    "VALUES (%s,'disaster_recovery',%s,%s,'cri-migrate dr-verify',%s,%s)",
                    ("drl_" + _uuid.uuid4().hex[:16], s.environment, datetime.now(UTC).isoformat(),
                     1 if (same and chains_ok) else 0, json.dumps(report, default=str)))
            print(json.dumps(report, default=str))
        elif cmd == "shadow":
            from app.platform import shadow
            from app.platform.app import Platform

            platform = Platform(s, db)
            print(json.dumps(shadow.run_all(db, platform.services), default=str)[:4000])
        elif cmd == "ingest":
            from fastapi.testclient import TestClient

            from app.platform.app import create_app

            os.environ["CRI_SERVICE_ROLE"] = "worker"
            client = TestClient(create_app(db=db))
            r = client.post("/internal/market-data/ingest")
            print(r.text)
            return 0 if r.status_code == 200 else 1
        else:
            print(__doc__)
            return 2
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
