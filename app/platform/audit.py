"""Tamper-evident, append-only audit trail in the database.

Each record stores the SHA-256 of its own canonical content chained to the
previous record's hash for the same tenant. ``verify_chain`` recomputes the
chain; any edited, removed or reordered record breaks it. In Postgres the
table is also protected by a trigger and by revoking UPDATE/DELETE from the
application role.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from app.audit.logger import AuditLogger
from app.platform.context import current_principal
from app.platform.db import current_connection, has_transaction

GENESIS = "0" * 64


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def ts_str(value: Any) -> str:
    """Canonical UTC timestamp text, identical whichever database returned it."""
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def _record_hash(prev_hash: str, audit_id: str, tenant_id: str, actor: str, action: str,
                 created_at: str, payload_json: str) -> str:
    material = "\n".join([prev_hash, audit_id, tenant_id, actor, action, created_at, payload_json])  # noqa: FLY002
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def append_audit(action: str, payload: dict[str, Any], *, tenant_id: str | None = None,
                 actor: str | None = None) -> str:
    conn = current_connection()
    principal = current_principal()
    tenant = tenant_id or (principal.tenant_id if principal else "*")
    who = actor or (principal.actor if principal else "system")
    conn.advisory_lock(f"audit:{tenant}")
    last = conn.fetchone(
        "SELECT record_hash FROM audit_log WHERE tenant_id = %s ORDER BY seq DESC LIMIT 1 FOR UPDATE",
        (tenant,),
    )
    prev = last["record_hash"] if last else GENESIS
    audit_id = uuid.uuid4().hex[:24]
    created_at = ts_str(datetime.now(UTC))
    payload_json = _canonical(payload)
    rec = _record_hash(prev, audit_id, tenant, who, action, created_at, payload_json)
    conn.execute(
        "INSERT INTO audit_log (audit_id, tenant_id, actor, action, created_at, payload, prev_hash, record_hash) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (audit_id, tenant, who, action, created_at, payload_json, prev, rec),
    )
    return audit_id


def verify_chain(tenant_id: str) -> dict[str, Any]:
    conn = current_connection()
    rows = conn.fetchall(
        "SELECT audit_id, tenant_id, actor, action, created_at, payload, prev_hash, record_hash "
        "FROM audit_log WHERE tenant_id = %s ORDER BY seq",
        (tenant_id,),
    )
    prev = GENESIS
    for i, r in enumerate(rows):
        created_s = ts_str(r["created_at"])
        expected = _record_hash(prev, r["audit_id"], r["tenant_id"], r["actor"], r["action"],
                                created_s, r["payload"])
        if r["prev_hash"] != prev or r["record_hash"] != expected:
            return {"tenant_id": tenant_id, "valid": False, "records": len(rows),
                    "first_invalid_index": i, "audit_id": r["audit_id"]}
        prev = r["record_hash"]
    return {"tenant_id": tenant_id, "valid": True, "records": len(rows), "head": prev}


class DatabaseAuditLogger(AuditLogger):
    """Drop-in replacement for app.audit.logger.AuditLogger.

    The engine calls ``write(payload)``; the record is attributed to the
    current principal's tenant. Calls outside a database transaction (for
    example the stateless /risk/evaluate path in local mode) fall back to the
    file logger so engine behaviour is unchanged.
    """

    def __init__(self, fallback: Any | None = None) -> None:
        self.fallback = fallback

    def write(self, payload: dict[str, Any]) -> str:
        if not has_transaction():
            if self.fallback is not None:
                return str(self.fallback.write(payload))
            raise RuntimeError("audit write outside a transaction")
        action = str(payload.get("event_type") or payload.get("action") or payload.get("type") or "engine_event")
        return append_audit(action, payload)
