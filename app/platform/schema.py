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


def _shadow_rls() -> str:
    return (
        "ALTER TABLE backtest_runs ENABLE ROW LEVEL SECURITY;\n"
        "DROP POLICY IF EXISTS tenant_isolation ON backtest_runs;\n"
        "CREATE POLICY tenant_isolation ON backtest_runs USING ("
        "current_setting('cri.tenant_id', true) = '*' OR tenant_id = current_setting('cri.tenant_id', true)) "
        "WITH CHECK (current_setting('cri.tenant_id', true) = '*' OR tenant_id = current_setting('cri.tenant_id', true));\n"
        "ALTER TABLE shadow_evaluations ENABLE ROW LEVEL SECURITY;\n"
        "DROP POLICY IF EXISTS tenant_isolation ON shadow_evaluations;\n"
        "CREATE POLICY tenant_isolation ON shadow_evaluations USING ("
        "current_setting('cri.tenant_id', true) = '*' OR tenant_id = current_setting('cri.tenant_id', true)) "
        "WITH CHECK (current_setting('cri.tenant_id', true) = '*' OR tenant_id = current_setting('cri.tenant_id', true));\n"
    )


def _grants() -> str:
    return """
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cri_app') THEN
    GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO cri_app;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO cri_app;
    REVOKE UPDATE, DELETE, TRUNCATE ON audit_log FROM cri_app;
    REVOKE UPDATE, DELETE, TRUNCATE ON market_quote_history FROM cri_app;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO cri_app;
    ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO cri_app;
  END IF;
END $$;
"""


_M4 = """
CREATE TABLE IF NOT EXISTS sandbox_webhook_receipts (
  receipt_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  delivery_id TEXT NOT NULL,
  signature_valid INTEGER NOT NULL,
  received_at {TS} NOT NULL,
  event_type TEXT
);
"""

_M5 = """
CREATE TABLE IF NOT EXISTS market_bars (
  stable_key TEXT NOT NULL,
  bar_date TEXT NOT NULL,
  open DOUBLE PRECISION,
  high DOUBLE PRECISION,
  low DOUBLE PRECISION,
  close DOUBLE PRECISION NOT NULL,
  volume DOUBLE PRECISION,
  range_reported INTEGER NOT NULL DEFAULT 0,
  volume_reported INTEGER NOT NULL DEFAULT 0,
  provider TEXT NOT NULL,
  ingested_at {TS} NOT NULL,
  PRIMARY KEY (stable_key, bar_date)
);
CREATE TABLE IF NOT EXISTS shadow_runs (
  run_id TEXT PRIMARY KEY,
  started_at {TS} NOT NULL,
  finished_at {TS},
  accounts INTEGER NOT NULL DEFAULT 0,
  evaluations INTEGER NOT NULL DEFAULT 0,
  errors TEXT NOT NULL DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS shadow_evaluations (
  evaluation_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  account_ref TEXT NOT NULL,
  model_id TEXT NOT NULL,
  model_version TEXT NOT NULL,
  role TEXT NOT NULL CHECK (role IN ('baseline','candidate')),
  evaluated_at {TS} NOT NULL,
  collateral_value DOUBLE PRECISION,
  loan_balance DOUBLE PRECISION,
  approved_credit DOUBLE PRECISION,
  available_credit DOUBLE PRECISION,
  capacity_ltv DOUBLE PRECISION,
  risk_state TEXT,
  margin_call INTEGER NOT NULL DEFAULT 0,
  estimated_recovery DOUBLE PRECISION,
  liquidation TEXT NOT NULL DEFAULT '[]',
  explanation TEXT NOT NULL DEFAULT '{{}}',
  error TEXT
);
CREATE INDEX IF NOT EXISTS shadow_eval_account ON shadow_evaluations(tenant_id, account_ref, evaluated_at);
CREATE INDEX IF NOT EXISTS shadow_eval_model ON shadow_evaluations(tenant_id, model_id, evaluated_at);
CREATE TABLE IF NOT EXISTS backtest_runs (
  run_id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL REFERENCES tenants(tenant_id),
  account_ref TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  models TEXT NOT NULL,
  params TEXT NOT NULL DEFAULT '{{}}',
  status TEXT NOT NULL CHECK (status IN ('queued','running','succeeded','failed')),
  created_at {TS} NOT NULL,
  started_at {TS},
  finished_at {TS},
  execution TEXT,
  summary TEXT,
  series TEXT,
  error TEXT
);
"""

_M6 = """
CREATE TABLE IF NOT EXISTS operational_drills (
  drill_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  environment TEXT NOT NULL,
  recorded_at {TS} NOT NULL,
  source TEXT NOT NULL,
  passed INTEGER,
  result TEXT NOT NULL
);
"""

MIGRATIONS: list[tuple[int, str, dict[str, str]]] = [
    (1, "initial platform schema", {"postgres": _M1, "sqlite": _M1}),
    (2, "append-only audit and row-level security", {"postgres": _M2_POSTGRES + _rls_statements(), "sqlite": ""}),
    (3, "application role grants", {"postgres": _grants(), "sqlite": ""}),
    (4, "sandbox webhook receiver", {"postgres": _M4 + _grants(), "sqlite": _M4}),
    (5, "market bars and shadow model evaluations", {"postgres": _M5 + _shadow_rls() + _grants(), "sqlite": _M5}),
    (6, "operational drill records", {"postgres": _M6 + _grants(), "sqlite": _M6}),
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
