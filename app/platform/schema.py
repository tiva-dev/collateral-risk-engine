"""Versioned, forward-only schema migrations.

Each migration is applied once and recorded in ``schema_migrations``. Cloud
environments run migrations from the dedicated ``cri-migrate`` Cloud Run job
before a new revision receives traffic, as the database owner role; the
application connects as a non-owner role so Postgres row-level security
applies to it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.platform.db import Database

_TYPES = {
    "postgres": {"TS": "TIMESTAMPTZ", "SERIAL_PK": "BIGSERIAL PRIMARY KEY", "BIGINT": "BIGINT"},
    "sqlite": {"TS": "TEXT", "SERIAL_PK": "INTEGER PRIMARY KEY AUTOINCREMENT", "BIGINT": "INTEGER"},
}

TENANT_TABLES = (
    "api_keys",
    "monitored_accounts",
    "account_instruments",
    "monitoring_events",
    "idempotency_keys",
    "lending_policies",
    "webhook_endpoints",
    "webhook_deliveries",
)

_M1 = """
CREATE TABLE IF NOT EXISTS tenants (
  tenant_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('client','sandbox','internal')),
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended')),
  created_at {TS} NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
  key_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  key_hash TEXT NOT NULL UNIQUE,
  role TEXT NOT NULL CHECK (role IN ('client','readonly','admin')),
  label TEXT NOT NULL DEFAULT '',
  created_at {TS} NOT NULL,
  revoked_at {TS},
  last_used_at {TS}
);
CREATE TABLE IF NOT EXISTS monitored_accounts (
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  account_ref TEXT NOT NULL,
  state TEXT NOT NULL,
  monitoring_status TEXT NOT NULL,
  margin_state TEXT,
  version {BIGINT} NOT NULL DEFAULT 1,
  created_at {TS} NOT NULL,
  updated_at {TS} NOT NULL,
  PRIMARY KEY (tenant_id, account_ref)
);
CREATE TABLE IF NOT EXISTS account_instruments (
  tenant_id TEXT NOT NULL,
  account_ref TEXT NOT NULL,
  instrument_key TEXT NOT NULL,
  PRIMARY KEY (tenant_id, account_ref, instrument_key),
  FOREIGN KEY (tenant_id, account_ref) REFERENCES monitored_accounts(tenant_id, account_ref) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS account_instruments_key ON account_instruments(instrument_key);
CREATE TABLE IF NOT EXISTS monitoring_events (
  event_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  account_ref TEXT,
  event_type TEXT NOT NULL,
  severity TEXT NOT NULL,
  dedupe_key TEXT,
  audit_id TEXT,
  created_at {TS} NOT NULL,
  body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS monitoring_events_tenant_time ON monitoring_events(tenant_id, created_at);
CREATE INDEX IF NOT EXISTS monitoring_events_dedupe ON monitoring_events(tenant_id, dedupe_key);
CREATE TABLE IF NOT EXISTS idempotency_keys (
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  idem_key TEXT NOT NULL,
  method TEXT NOT NULL,
  path TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('in_progress','completed')),
  response_status INTEGER,
  response_body TEXT,
  response_content_type TEXT,
  created_at {TS} NOT NULL,
  completed_at {TS},
  PRIMARY KEY (tenant_id, idem_key)
);
CREATE TABLE IF NOT EXISTS audit_log (
  seq {SERIAL_PK},
  audit_id TEXT NOT NULL UNIQUE,
  tenant_id TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  created_at {TS} NOT NULL,
  payload TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  record_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_log_tenant ON audit_log(tenant_id, seq);
CREATE TABLE IF NOT EXISTS platform_controls (
  control_key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  reason TEXT NOT NULL,
  updated_by TEXT NOT NULL,
  updated_at {TS} NOT NULL
);
CREATE TABLE IF NOT EXISTS model_versions (
  component TEXT NOT NULL,
  version TEXT NOT NULL,
  git_sha TEXT NOT NULL,
  first_seen_at {TS} NOT NULL,
  PRIMARY KEY (component, version, git_sha)
);
CREATE TABLE IF NOT EXISTS lending_policies (
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  policy_id TEXT NOT NULL,
  version INTEGER NOT NULL,
  body TEXT NOT NULL,
  body_sha256 TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('draft','approved','retired')),
  created_by TEXT NOT NULL,
  created_at {TS} NOT NULL,
  approved_by TEXT,
  approved_at {TS},
  PRIMARY KEY (tenant_id, policy_id, version)
);
CREATE TABLE IF NOT EXISTS webhook_endpoints (
  endpoint_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  url TEXT NOT NULL,
  signing_secret TEXT NOT NULL,
  event_types TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at {TS} NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
  delivery_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  endpoint_id TEXT NOT NULL REFERENCES webhook_endpoints(endpoint_id),
  event_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('pending','delivered','dead')),
  attempts INTEGER NOT NULL DEFAULT 0,
  next_attempt_at {TS} NOT NULL,
  last_error TEXT,
  last_status_code INTEGER,
  created_at {TS} NOT NULL,
  delivered_at {TS},
  UNIQUE (endpoint_id, event_id)
);
CREATE INDEX IF NOT EXISTS webhook_deliveries_due ON webhook_deliveries(status, next_attempt_at);
CREATE TABLE IF NOT EXISTS market_quotes (
  quote_key TEXT PRIMARY KEY,
  stable_key TEXT NOT NULL,
  quote TEXT NOT NULL,
  observed_at {TS} NOT NULL,
  source TEXT NOT NULL,
  ingested_at {TS} NOT NULL
);
CREATE TABLE IF NOT EXISTS market_quote_history (
  id {SERIAL_PK},
  stable_key TEXT NOT NULL,
  price DOUBLE PRECISION NOT NULL,
  observed_at {TS} NOT NULL,
  source TEXT NOT NULL,
  ingested_at {TS} NOT NULL
);
CREATE TABLE IF NOT EXISTS fx_rates (
  pair TEXT PRIMARY KEY,
  rate TEXT NOT NULL,
  observed_at {TS} NOT NULL,
  ingested_at {TS} NOT NULL
);
CREATE TABLE IF NOT EXISTS ingestion_runs (
  run_id TEXT PRIMARY KEY,
  started_at {TS} NOT NULL,
  finished_at {TS},
  instruments INTEGER NOT NULL DEFAULT 0,
  quotes_written INTEGER NOT NULL DEFAULT 0,
  errors TEXT NOT NULL DEFAULT '[]'
);
"""

# Postgres-only hardening: append-only audit log and row-level security.
_M2_POSTGRES = """
CREATE OR REPLACE FUNCTION cri_forbid_mutation() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'audit_log is append-only';
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log;
CREATE TRIGGER audit_log_append_only BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION cri_forbid_mutation();
"""


def _rls_statements() -> str:
    out = []
    for table in TENANT_TABLES:
        out.append(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        out.append(f"DROP POLICY IF EXISTS tenant_isolation ON {table};")
        # '*' is set only by internal worker paths and migrations, never by
        # tenant-authenticated requests.
        out.append(
            f"CREATE POLICY tenant_isolation ON {table} USING ("
            "current_setting('cri.tenant_id', true) = '*' OR "
            "tenant_id = current_setting('cri.tenant_id', true)) WITH CHECK ("
            "current_setting('cri.tenant_id', true) = '*' OR "
            "tenant_id = current_setting('cri.tenant_id', true));"
        )
    return "\n".join(out)


def _grants() -> str:
    return """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cri_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO cri_app;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO cri_app;
    REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM cri_app;
    REVOKE UPDATE, DELETE, TRUNCATE ON market_quote_history FROM cri_app;
  END IF;
END $$;
"""


MIGRATIONS: list[tuple[int, str, dict[str, str]]] = [
    (1, "initial platform schema", {"postgres": _M1, "sqlite": _M1}),
    (2, "append-only audit and row-level security", {"postgres": _M2_POSTGRES + _rls_statements(), "sqlite": ""}),
    (3, "application role grants", {"postgres": _grants(), "sqlite": ""}),
]


def _split(sql: str) -> list[str]:
    # Split on semicolons outside $$-quoted bodies.
    parts, buf, in_dollar = [], [], False
    for line in sql.splitlines():
        if line.count("$$") % 2 == 1:
            in_dollar = not in_dollar
        buf.append(line)
        if not in_dollar and line.rstrip().endswith(";"):
            stmt = "\n".join(buf).strip()
            if stmt:
                parts.append(stmt)
            buf = []
    tail = "\n".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def migrate(db: Database) -> list[int]:
    applied: list[int] = []
    with db.transaction("*") as conn:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, "
            f"description TEXT NOT NULL, applied_at {_TYPES[db.dialect]['TS']} NOT NULL)"
        )
        conn.advisory_lock("cri-schema-migrations")
        done = {r["version"] for r in conn.fetchall("SELECT version FROM schema_migrations")}
        for version, description, by_dialect in MIGRATIONS:
            if version in done:
                continue
            sql = by_dialect[db.dialect].format(**_TYPES[db.dialect]) if by_dialect[db.dialect] else ""
            if db.dialect == "postgres":
                sql = sql.replace("{{", "{").replace("}}", "}")
            for stmt in _split(sql):
                conn.execute(stmt)
            conn.execute(
                "INSERT INTO schema_migrations (version, description, applied_at) VALUES (%s, %s, %s)",
                (version, description, datetime.now(UTC).isoformat()),
            )
            applied.append(version)
    return applied
