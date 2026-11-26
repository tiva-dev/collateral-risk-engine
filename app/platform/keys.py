"""API keys: generated once, shown once, stored only as a peppered SHA-256."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from app.platform.context import Principal
from app.platform.db import Database

KEY_PREFIX = "cri_"


def hash_key(raw_key: str, pepper: str) -> str:
    return hmac.new(pepper.encode(), raw_key.encode(), hashlib.sha256).hexdigest()


def generate_key(environment: str) -> str:
    return f"{KEY_PREFIX}{environment[:4]}_{secrets.token_urlsafe(32)}"


def create_tenant(conn, tenant_id: str, name: str, kind: str) -> None:
    conn.execute(
        "INSERT INTO tenants (tenant_id, name, kind, status, created_at) VALUES (%s,%s,%s,'active',%s)",
        (tenant_id, name, kind, datetime.now(UTC).isoformat()),
    )


def issue_key(conn, tenant_id: str, role: str, label: str, pepper: str, environment: str) -> tuple[str, str]:
    raw = generate_key(environment)
    key_id = "key_" + uuid.uuid4().hex[:16]
    conn.execute(
        "INSERT INTO api_keys (key_id, tenant_id, key_hash, role, label, created_at) VALUES (%s,%s,%s,%s,%s,%s)",
        (key_id, tenant_id, hash_key(raw, pepper), role, label, datetime.now(UTC).isoformat()),
    )
    return key_id, raw


def authenticate(db: Database, raw_key: str, pepper: str) -> Principal | None:
    if not raw_key.startswith(KEY_PREFIX) or not pepper:
        return None
    digest = hash_key(raw_key, pepper)
    with db.transaction("*") as conn:
        row = conn.fetchone(
            "SELECT k.key_id, k.tenant_id, k.role, k.last_used_at, t.kind, t.status FROM api_keys k "
            "JOIN tenants t ON t.tenant_id = k.tenant_id WHERE k.key_hash=%s AND k.revoked_at IS NULL",
            (digest,),
        )
        if row is None or row["status"] != "active":
            return None
        last = row["last_used_at"]
        last_dt = datetime.fromisoformat(last) if isinstance(last, str) else last
        now = datetime.now(UTC)
        if last_dt is None or now - last_dt > timedelta(minutes=15):
            conn.execute("UPDATE api_keys SET last_used_at=%s WHERE key_id=%s", (now.isoformat(), row["key_id"]))
        return Principal(tenant_id=row["tenant_id"], role=row["role"], key_id=row["key_id"],
                         tenant_kind=row["kind"])
