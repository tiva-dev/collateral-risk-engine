"""Platform layer: persistence, tenancy, idempotency, audit, controls, webhooks.

Unit tests run on SQLite everywhere. When CRI_TEST_DATABASE_URL (owner role)
and CRI_TEST_APP_DATABASE_URL (non-owner application role) are set, as in the
CI Postgres job, the same scenarios also run on Postgres and row-level
security is checked directly.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.core.enums import AssetType, DataMode
from app.core.evaluator import CollateralRiskEngine
from app.core.models import Holding, Loan, Policy
from app.lifecycle.service import CreditLifecycleEngine
from app.market_data.identity import InstrumentIdentity
from app.market_data.policy import MarketDataPolicy
from app.market_data.providers import RawQuote
from app.monitoring.service import MonitoringService
from app.platform import codec, controls, webhooks
from app.platform.audit import append_audit, verify_chain
from app.platform.db import Database
from app.platform.ingestion import parse_stable_key, quote_from_bars
from app.platform.keys import create_tenant, issue_key
from app.platform.repositories import (
    SqlMarketDataCache,
    SqlMonitoredAccountRepository,
    SqlMonitoringEventRepository,
)
from app.platform.schema import migrate
from app.platform.settings import PlatformSettings

try:
    from fastapi.testclient import TestClient
except (ImportError, RuntimeError):  # pragma: no cover - minimal local environments
    TestClient = None

PEPPER = "test-pepper-0123456789"


def _quote(price: float = 100.0) -> RawQuote:
    inst = InstrumentIdentity("SPY", "SPY", "NYSE", "USD", AssetType.ETF)
    return RawQuote(inst, price, price - 0.1, price + 0.1, 1_000_000, price * 1_000_000, 0.15, 0.15,
                    timestamp=datetime.now(UTC), provider_name="test")


def _engine() -> CreditLifecycleEngine:
    return CreditLifecycleEngine(CollateralRiskEngine(audit_logger=None), audit_logger=None)


def _service(tenant: str, hook=None) -> MonitoringService:
    return MonitoringService(SqlMonitoredAccountRepository(tenant), SqlMonitoringEventRepository(tenant, hook),
                             _engine(), market_data_cache=SqlMarketDataCache(), audit_logger=None)


def _register(svc: MonitoringService, ref: str = "acct", loan: float = 1_000.0, price: float = 100.0):
    return svc.register_account(
        account_ref=ref, holdings=[Holding("SPY", AssetType.ETF, 100.0, "USD")], pledged_cash_balance=0.0,
        loan=Loan(loan, currency="USD"), loan_currency="USD", policy=Policy.default(),
        data_mode=DataMode.CLIENT_SUPPLIED, market_data_policy=MarketDataPolicy(),
        client_supplied_quotes={"SPY": _quote(price)}, run_initial_evaluation=True)


def _settings(db_url: str, **kw) -> PlatformSettings:
    base = {"environment": "test", "role": "api", "database_url": db_url, "api_key_pepper": PEPPER,
            "lending_permitted": False, "execution_permitted": False, "webhook_allow_http": False}
    base.update(kw)
    return PlatformSettings(**base)


class _DbCase(unittest.TestCase):
    url: str | None = None

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(self.url or f"sqlite:///{Path(self.tmp.name) / 'cri.sqlite3'}")
        if self.db.dialect == "postgres":
            _reset_postgres(self.db)
        migrate(self.db)
        with self.db.transaction("*") as conn:
            create_tenant(conn, "alpha", "Alpha", "client")
            create_tenant(conn, "beta", "Beta", "client")

    def tearDown(self) -> None:
        self.db.close()
        self.tmp.cleanup()


def _reset_postgres(db: Database) -> None:
    with db.transaction("*") as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
        conn.execute("GRANT USAGE ON SCHEMA public TO PUBLIC")


class CodecTests(unittest.TestCase):
    def test_monitored_account_round_trips_losslessly(self) -> None:
        from app.monitoring.repositories import (
            InMemoryMonitoredAccountRepository,
            InMemoryMonitoringEventRepository,
        )

        svc = MonitoringService(InMemoryMonitoredAccountRepository(), InMemoryMonitoringEventRepository(),
                                _engine(), audit_logger=None)
        account, events = _register(svc)
        self.assertEqual(codec.dumps(codec.loads(codec.dumps(account))), codec.dumps(account))
        self.assertTrue(events)
        self.assertEqual(codec.dumps(codec.loads(codec.dumps(events[0]))), codec.dumps(events[0]))

    def test_decoder_refuses_types_outside_app(self) -> None:
        with self.assertRaises(ValueError):
            codec.loads(json.dumps({"__dc__": "subprocess:Popen", "f": {}}))


class PersistenceTests(_DbCase):
    def test_service_state_persists_and_is_tenant_scoped(self) -> None:
        with self.db.transaction("alpha"):
            account, events = _register(_service("alpha"))
        with self.db.transaction("alpha"):
            stored = _service("alpha").get_account("acct")
            self.assertIsNotNone(stored)
            self.assertEqual(codec.dumps(stored), codec.dumps(account))
            self.assertGreaterEqual(len(SqlMonitoringEventRepository("alpha").list()), len(events))
            self.assertEqual(len(SqlMonitoredAccountRepository("alpha").list_by_instrument("SPY")), 1)
        with self.db.transaction("beta"):
            self.assertIsNone(_service("beta").get_account("acct"))
            self.assertEqual(SqlMonitoringEventRepository("beta").list(), [])

    def test_failure_rolls_back_account_events_and_audit_together(self) -> None:
        with self.assertRaises(RuntimeError), self.db.transaction("alpha"):
            _register(_service("alpha"))
            append_audit("x", {"a": 1}, tenant_id="alpha")
            raise RuntimeError("boom")
        with self.db.transaction("alpha") as conn:
            self.assertIsNone(_service("alpha").get_account("acct"))
            self.assertEqual(SqlMonitoringEventRepository("alpha").list(), [])
            self.assertEqual(conn.fetchall("SELECT * FROM audit_log WHERE tenant_id='alpha'"), [])

    def test_tick_after_reload_continues_from_stored_state(self) -> None:
        with self.db.transaction("alpha"):
            _register(_service("alpha"))
        with self.db.transaction("alpha"):
            account, _ = _service("alpha").evaluate_account("acct", force_tick_event=True, force=True)
            self.assertIsNotNone(account.last_checked_at)

    def test_migrations_are_idempotent(self) -> None:
        self.assertEqual(migrate(self.db), [])


class AuditTests(_DbCase):
    def test_hash_chain_verifies_and_detects_tampering(self) -> None:
        with self.db.transaction("alpha"):
            for i in range(5):
                append_audit("event", {"i": i}, tenant_id="alpha", actor="test")
            self.assertEqual(verify_chain("alpha")["valid"], True)
            self.assertEqual(verify_chain("alpha")["records"], 5)
        if self.db.dialect == "postgres":
            with self.assertRaises(Exception), self.db.transaction("*") as conn:
                conn.execute("UPDATE audit_log SET payload='{}' WHERE tenant_id='alpha'")
            return
        with self.db.transaction("*") as conn:
            conn.execute("UPDATE audit_log SET payload='{\"i\":99}' WHERE seq = (SELECT MIN(seq) FROM audit_log)")
            result = verify_chain("alpha")
        self.assertFalse(result["valid"])
        self.assertEqual(result["first_invalid_index"], 0)


class ControlTests(_DbCase):
    def test_lending_cannot_be_enabled_without_deployment_permit(self) -> None:
        s = _settings(self.db.url)
        with self.db.transaction("*"):
            with self.assertRaises(controls.ControlError):
                controls.set_control(s, "lending_enabled", True, "testing the interlock", "admin:k")
            with self.assertRaises(controls.ControlError):
                controls.set_control(s, "monitoring_enabled", False, "short", "admin:k")
            controls.set_control(s, "monitoring_enabled", False, "pausing for maintenance window", "admin:k")
            self.assertFalse(controls.effective(s)["monitoring_enabled"])
            self.assertEqual(verify_chain("*")["records"], 1)

    def test_effective_value_requires_both_permit_and_control(self) -> None:
        permitted = _settings(self.db.url, lending_permitted=True)
        with self.db.transaction("*"):
            self.assertFalse(controls.effective(permitted)["lending_enabled"])
            controls.set_control(permitted, "lending_enabled", True, "approved pilot enablement", "admin:k")
            self.assertTrue(controls.effective(permitted)["lending_enabled"])
            self.assertFalse(controls.effective(_settings(self.db.url))["lending_enabled"])


class _FakeResponse:
    def __init__(self, status: int) -> None:
        self.status_code = status


class _FakeClient:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses = statuses
        self.calls: list[dict] = []

    def post(self, url, content, headers):
        self.calls.append({"url": url, "content": content, "headers": headers})
        return _FakeResponse(self.statuses.pop(0))


class WebhookTests(_DbCase):
    def _endpoint(self) -> dict:
        with self.db.transaction("alpha") as conn:
            out = {"endpoint_id": "whe_test", "signing_secret": "whsec_test"}
            conn.execute(
                "INSERT INTO webhook_endpoints (endpoint_id, tenant_id, url, signing_secret, event_types, active, "
                "created_at) VALUES (%s,%s,%s,%s,%s,1,%s)",
                ("whe_test", "alpha", "https://example.com/hook", "whsec_test", '["*"]', datetime.now(UTC).isoformat()))
        return out

    def _event_delivery(self) -> str:
        self._endpoint()
        hook = lambda tenant, event: webhooks.enqueue_for_event(tenant, event, False)
        with self.db.transaction("alpha"):
            _register(_service("alpha", hook))
        with self.db.transaction("*") as conn:
            rows = conn.fetchall("SELECT delivery_id, payload FROM webhook_deliveries")
        self.assertTrue(rows)
        payload = json.loads(rows[0]["payload"])
        self.assertTrue(payload["advisory_only"])
        self.assertFalse(payload["execution_authorised"])
        return rows[0]["delivery_id"]

    def test_outbox_written_with_event_and_signed_delivery_succeeds(self) -> None:
        delivery = self._event_delivery()
        client = _FakeClient([200])
        s = _settings(self.db.url)
        original = webhooks.resolve_destination
        webhooks.resolve_destination = lambda url, allow_http=False: ("example.com", 443, ["93.184.216.34"])
        try:
            result = webhooks.deliver_one(self.db, delivery, s, client)
        finally:
            webhooks.resolve_destination = original
        self.assertEqual(result["status"], "delivered")
        call = client.calls[0]
        ts = call["headers"]["X-CRI-Timestamp"]
        expected = "v1=" + hmac.new(b"whsec_test", ts.encode() + b"." + call["content"], hashlib.sha256).hexdigest()
        self.assertEqual(call["headers"]["X-CRI-Signature"], expected)
        self.assertEqual(webhooks.deliver_one(self.db, delivery, s, client).get("skipped"), True)

    def test_failures_back_off_then_dead_letter(self) -> None:
        delivery = self._event_delivery()
        s = _settings(self.db.url, webhook_max_attempts=3)
        original = webhooks.resolve_destination
        webhooks.resolve_destination = lambda url, allow_http=False: ("example.com", 443, ["93.184.216.34"])
        try:
            r1 = webhooks.deliver_one(self.db, delivery, s, _FakeClient([302]))
            self.assertIn("redirects are not followed", r1["error"])
            self.assertEqual(r1["status"], "retrying")
            self.assertNotIn(delivery, webhooks.due_deliveries(self.db))  # backoff in force
            webhooks.deliver_one(self.db, delivery, s, _FakeClient([503]))
            r3 = webhooks.deliver_one(self.db, delivery, s, _FakeClient([500]))
        finally:
            webhooks.resolve_destination = original
        self.assertEqual(r3["status"], "dead")
        self.assertEqual(webhooks.backoff(1), timedelta(seconds=30))
        self.assertEqual(webhooks.backoff(20), timedelta(hours=6))

    def test_private_addresses_are_rejected(self) -> None:
        for url in ("http://example.com/x", "https://127.0.0.1/x", "https://10.0.0.1/x", "https://localhost/x",
                    "https://169.254.169.254/computeMetadata/v1/", "https://[::ffff:10.0.0.1]/x",
                    "https://100.64.1.1/x", "https://user:pw@example.com/x", "https://example.com:22/x",
                    "https://metadata.google.internal/x", "https://[fd00::1]/x"):
            with self.assertRaises(webhooks.WebhookError, msg=url):
                webhooks.validate_url(url)

    def test_dns_rebinding_is_defeated_by_pinning_the_validated_address(self) -> None:
        import socket as _socket

        answers = [[(2, 1, 6, "", ("93.184.216.34", 443))], [(2, 1, 6, "", ("10.0.0.5", 443))]]
        original = _socket.getaddrinfo
        _socket.getaddrinfo = lambda *a, **k: answers.pop(0) if answers else original(*a, **k)
        try:
            _host, _port, addrs = webhooks.resolve_destination("https://hooks.example.com/cri")
        finally:
            _socket.getaddrinfo = original
        self.assertEqual(addrs, ["93.184.216.34"])
        url, headers, ext = webhooks.pinned_request("https://hooks.example.com/cri?x=1", addrs[0])
        self.assertEqual(url, "https://93.184.216.34/cri?x=1")
        self.assertEqual(headers["Host"], "hooks.example.com")
        self.assertEqual(ext["sni_hostname"], "hooks.example.com")
        # A later lookup that returns a private address is never consulted:
        # the delivery connects to the pinned public address.
        url6, _, _ = webhooks.pinned_request("https://hooks.example.com:8443/cri", "2606:2800:220:1::1")
        self.assertEqual(url6, "https://[2606:2800:220:1::1]:8443/cri")

    def test_mixed_public_and_private_answers_are_rejected(self) -> None:
        import socket as _socket

        original = _socket.getaddrinfo
        _socket.getaddrinfo = lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443)), (2, 1, 6, "", ("192.168.1.9", 443))]
        try:
            with self.assertRaises(webhooks.WebhookError):
                webhooks.resolve_destination("https://hooks.example.com/cri")
        finally:
            _socket.getaddrinfo = original


class IngestionTests(unittest.TestCase):
    def test_quote_from_observed_bars_flags_unobserved_spread(self) -> None:
        from app.historical_data.models import HistoricalBar

        bars = [HistoricalBar("MTNN", date(2026, 6, 1) + timedelta(days=i), 1, 1, 1, 200 + i, volume=1e5 + i)
                for i in range(60)]
        ident = parse_stable_key("NGX:MTNN:NGN:LISTED_EQUITY")
        q = quote_from_bars(ident, bars, "ngnmarket", now=datetime(2026, 8, 1, tzinfo=UTC))
        self.assertEqual(q.local_price, 259)
        self.assertIsNone(q.bid)
        self.assertIn("bid_ask_not_observed", q.warnings)
        self.assertIsNotNone(q.volatility_30d)
        self.assertAlmostEqual(q.average_daily_volume, sum(1e5 + i for i in range(40, 60)) / 20)
        self.assertIsNone(parse_stable_key("SPY"))
        same_day = quote_from_bars(ident, bars, "ngnmarket", now=datetime(2026, 7, 30, 10, 0, tzinfo=UTC))
        self.assertLessEqual(same_day.timestamp, datetime(2026, 7, 30, 10, 0, tzinfo=UTC))


@unittest.skipIf(TestClient is None, "fastapi TestClient unavailable")
class ApiTests(_DbCase):
    def setUp(self) -> None:
        super().setUp()
        self.app_url = os.environ.get("CRI_TEST_APP_DATABASE_URL") if self.url else None
        app_db = Database(self.app_url) if self.app_url else self.db
        self.app_db = app_db
        from app.platform.app import create_app

        with self.db.transaction("*") as conn:
            create_tenant(conn, "tiva", "Tiva", "internal")
            _, self.admin = issue_key(conn, "tiva", "admin", "t", PEPPER, "test")
            _, self.alpha = issue_key(conn, "alpha", "client", "t", PEPPER, "test")
            _, self.beta = issue_key(conn, "beta", "client", "t", PEPPER, "test")
        self.client = TestClient(create_app(_settings(self.db.url), app_db))
        worker_settings = _settings(self.db.url, role="worker")
        self.worker = TestClient(create_app(worker_settings, app_db))

    def tearDown(self) -> None:
        if self.app_db is not self.db:
            self.app_db.close()
        super().tearDown()

    def h(self, key: str, idem: str | None = None) -> dict:
        out = {"Authorization": f"Bearer {key}"}
        if idem:
            out["Idempotency-Key"] = idem
        return out

    _quote_time = datetime.now(UTC).isoformat()

    def _account_body(self, ref: str = "acct-1") -> dict:
        return {
            "account_ref": ref,
            "holdings": [{"asset_id": "SPY", "asset_type": "etf", "quantity": 100, "currency": "USD"}],
            "loan": {"principal": 1000.0, "currency": "USD"},
            "loan_currency": "USD",
            "policy": {"base_ltv": {"etf": 0.5}},
            "data_mode": "client_supplied",
            "client_supplied_quotes": {"SPY": {"asset_id": "SPY", "symbol": "SPY", "exchange": "NYSE",
                                               "currency": "USD", "asset_type": "etf", "local_price": 100.0,
                                               "bid": 99.9, "ask": 100.1, "average_daily_volume": 1000000,
                                               "volatility_30d": 0.15,
                                               "timestamp": self._quote_time}},
        }

    def test_authentication_and_internal_route_isolation(self) -> None:
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/monitoring/accounts").status_code, 401)
        self.assertEqual(self.client.get("/monitoring/accounts", headers=self.h("cri_test_wrong")).status_code, 401)
        self.assertEqual(self.client.post("/internal/monitoring/tick-all", headers=self.h(self.admin)).status_code, 404)
        self.assertEqual(self.client.get("/admin/tenants", headers=self.h(self.alpha)).status_code, 403)
        self.assertEqual(self.worker.get("/monitoring/accounts").status_code, 404)
        who = self.client.get("/platform/whoami", headers=self.h(self.alpha)).json()
        self.assertEqual(who["tenant_id"], "alpha")

    def test_idempotency_is_permanent_and_request_bound(self) -> None:
        r0 = self.client.post("/monitoring/accounts", json=self._account_body(), headers=self.h(self.alpha))
        self.assertEqual(r0.status_code, 428)
        r1 = self.client.post("/monitoring/accounts", json=self._account_body(), headers=self.h(self.alpha, "k1"))
        self.assertEqual(r1.status_code, 200, r1.text)
        r2 = self.client.post("/monitoring/accounts", json=self._account_body(), headers=self.h(self.alpha, "k1"))
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.headers.get("Idempotent-Replayed"), "true")
        self.assertEqual(r2.json(), r1.json())
        r3 = self.client.post("/monitoring/accounts", json=self._account_body("acct-2"),
                              headers=self.h(self.alpha, "k1"))
        self.assertEqual(r3.status_code, 422)
        # Same key in another tenant is independent.
        r4 = self.client.post("/monitoring/accounts", json=self._account_body(), headers=self.h(self.beta, "k1"))
        self.assertEqual(r4.status_code, 200, r4.text)
        self.assertNotIn("Idempotent-Replayed", r4.headers)

    def _idem_row(self, key: str, status: str, age_minutes: float, body: dict, tenant: str = "alpha") -> bytes:
        import hashlib as _h

        raw = json.dumps(body, separators=(",", ":")).encode()
        digest = _h.sha256(b"POST /monitoring/accounts\n" + raw).hexdigest()
        with self.db.transaction("*") as conn:
            conn.execute(
                "INSERT INTO idempotency_keys (tenant_id, idem_key, method, path, request_sha256, status, created_at) "
                "VALUES (%s,%s,'POST','/monitoring/accounts',%s,%s,%s)",
                (tenant, key, digest, status, (datetime.now(UTC) - timedelta(minutes=age_minutes)).isoformat()))
        return raw

    def test_in_flight_duplicate_is_refused_and_stale_claim_is_recovered(self) -> None:
        body = self._account_body("acct-crash")
        raw = self._idem_row("k-crash", "in_progress", 1, body)
        h = {**self.h(self.alpha, "k-crash"), "Content-Type": "application/json"}
        r1 = self.client.post("/monitoring/accounts", content=raw, headers=h)
        self.assertEqual(r1.status_code, 409, r1.text)  # a live request still owns the key
        with self.db.transaction("*") as conn:
            conn.execute("UPDATE idempotency_keys SET created_at=%s WHERE idem_key='k-crash'",
                         ((datetime.now(UTC) - timedelta(minutes=11)).isoformat(),))
        r2 = self.client.post("/monitoring/accounts", content=raw, headers=h)  # worker crashed: claim is stale
        self.assertEqual(r2.status_code, 200, r2.text)
        r3 = self.client.post("/monitoring/accounts", content=raw, headers=h)  # delayed duplicate
        self.assertEqual(r3.headers.get("Idempotent-Replayed"), "true")
        self.assertEqual(r3.json(), r2.json())

    def test_failed_request_releases_key_and_rolls_back(self) -> None:
        from fastapi.testclient import TestClient as _TC

        app = self.client.app

        def boom() -> dict:
            from app.platform.audit import append_audit

            append_audit("should_roll_back", {"x": 1})
            raise RuntimeError("simulated crash inside the request")

        app.add_api_route("/monitoring/accounts-boom", boom, methods=["POST"])
        c = _TC(app, raise_server_exceptions=False)
        r = c.post("/monitoring/accounts-boom", json={}, headers=self.h(self.alpha, "k-boom"))
        self.assertEqual(r.status_code, 500)
        with self.db.transaction("*") as conn:
            self.assertIsNone(conn.fetchone("SELECT 1 FROM idempotency_keys WHERE idem_key='k-boom'"))
            self.assertIsNone(conn.fetchone("SELECT 1 FROM audit_log WHERE action='should_roll_back'"))

    @unittest.skipUnless(os.environ.get("CRI_TEST_DATABASE_URL"), "concurrency needs Postgres")
    def test_concurrent_duplicates_execute_once(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        body = self._account_body("acct-race")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.client.post(
                "/monitoring/accounts", json=body, headers=self.h(self.alpha, "k-race")), range(8)))
        codes = sorted(r.status_code for r in results)
        fresh = [r for r in results if r.status_code == 200 and r.headers.get("Idempotent-Replayed") != "true"]
        self.assertEqual(len(fresh), 1, codes)
        self.assertTrue(all(c in (200, 409) for c in codes), codes)
        with self.db.transaction("*") as conn:
            n = conn.fetchone("SELECT COUNT(*) AS n FROM monitored_accounts WHERE account_ref='acct-race'")["n"]
        self.assertEqual(n, 1)

    def test_console_and_operator_tenant_view(self) -> None:
        r = self.client.get("/console/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("frame-ancestors 'none'", r.headers.get("content-security-policy", ""))
        self.client.post("/monitoring/accounts", json=self._account_body(), headers=self.h(self.alpha, "op1"))
        view = self.client.get("/monitoring/accounts", headers={**self.h(self.admin), "X-CRI-Tenant": "alpha"})
        self.assertEqual([a["account_ref"] for a in view.json()["accounts"]], ["acct-1"])
        blocked = self.client.post("/monitoring/accounts", json=self._account_body("x"),
                                   headers={**self.h(self.admin, "op2"), "X-CRI-Tenant": "alpha"})
        self.assertEqual(blocked.status_code, 403)
        ignored = self.client.get("/monitoring/accounts", headers={**self.h(self.beta), "X-CRI-Tenant": "alpha"})
        self.assertEqual(ignored.json()["accounts"], [])  # only operators may switch tenant

    def test_tenants_cannot_see_each_other(self) -> None:
        self.client.post("/monitoring/accounts", json=self._account_body(), headers=self.h(self.alpha, "a"))
        self.assertEqual(self.client.get("/monitoring/accounts/acct-1", headers=self.h(self.alpha)).status_code, 200)
        self.assertEqual(self.client.get("/monitoring/accounts/acct-1", headers=self.h(self.beta)).status_code, 404)
        self.assertEqual(self.client.get("/monitoring/accounts", headers=self.h(self.beta)).json()["accounts"], [])
        self.assertEqual(self.client.get("/monitoring/events", headers=self.h(self.beta)).json()["events"], [])
        self.assertTrue(self.client.get("/monitoring/events", headers=self.h(self.alpha)).json()["events"])
        audit = self.client.get("/platform/audit/verify", headers=self.h(self.alpha)).json()
        self.assertTrue(audit["valid"])

    def test_lending_is_blocked_for_clients_and_non_binding_for_sandbox(self) -> None:
        body = {"holdings": [], "loan": {"principal": 0, "currency": "USD"}}
        r = self.client.post("/credit/originate", json=body, headers=self.h(self.alpha))
        self.assertEqual(r.status_code, 403)
        admin_t = self.client.post("/admin/tenants", json={"tenant_id": "sbx-one", "name": "S", "kind": "sandbox"},
                                   headers=self.h(self.admin, "t1"))
        self.assertEqual(admin_t.status_code, 201, admin_t.text)
        sandbox_key = admin_t.json()["api_key"]
        r2 = self.client.post("/credit/originate", json=body, headers=self.h(sandbox_key))
        self.assertNotEqual(r2.status_code, 403)
        self.assertEqual(r2.headers.get("X-CRI-Lending"), "disabled-non-binding")
        c = self.client.put("/admin/controls/lending_enabled", json={"value": True, "reason": "trying to enable"},
                            headers=self.h(self.admin, "c1"))
        self.assertEqual(c.status_code, 409)

    def test_worker_ticks_every_tenant(self) -> None:
        self.client.post("/monitoring/accounts", json=self._account_body(), headers=self.h(self.alpha, "a"))
        self.client.post("/monitoring/accounts", json=self._account_body(), headers=self.h(self.beta, "b"))
        r = self.worker.post("/internal/monitoring/tick-all")
        self.assertEqual(r.status_code, 200, r.text)
        by_tenant = {x["tenant_id"]: x for x in r.json()["results"]}
        self.assertIn("result", by_tenant["alpha"])
        self.assertIn("result", by_tenant["beta"])

    def test_policy_versions_need_a_different_approver(self) -> None:
        p1 = self.client.post("/policies", json={"policy_id": "std", "body": {"max_ltv": 0.5}},
                              headers=self.h(self.alpha, "p1"))
        p2 = self.client.post("/policies", json={"policy_id": "std", "body": {"max_ltv": 0.45}},
                              headers=self.h(self.alpha, "p2"))
        self.assertEqual((p1.json()["version"], p2.json()["version"]), (1, 2))
        ok = self.client.post("/admin/policies/alpha/std/2/approve",
                              json={"value": True, "reason": "credit committee 25 Nov"},
                              headers=self.h(self.admin, "ap1"))
        self.assertEqual(ok.status_code, 200, ok.text)
        listed = self.client.get("/policies", headers=self.h(self.alpha)).json()["policies"]
        self.assertEqual([p["status"] for p in listed], ["draft", "approved"])


_PG = os.environ.get("CRI_TEST_DATABASE_URL")


@unittest.skipUnless(_PG, "Postgres not configured")
class PostgresPersistenceTests(PersistenceTests):
    url = _PG


@unittest.skipUnless(_PG, "Postgres not configured")
class PostgresAuditTests(AuditTests):
    url = _PG


@unittest.skipUnless(_PG, "Postgres not configured")
class PostgresWebhookTests(WebhookTests):
    url = _PG


@unittest.skipUnless(_PG and TestClient is not None, "Postgres not configured")
class PostgresApiTests(ApiTests):
    url = _PG


@unittest.skipUnless(_PG and os.environ.get("CRI_TEST_APP_DATABASE_URL"), "Postgres app role not configured")
class PostgresRowLevelSecurityTests(_DbCase):
    url = _PG

    def test_application_role_only_sees_bound_tenant(self) -> None:
        with self.db.transaction("alpha"):
            _register(_service("alpha"))
        app_db = Database(os.environ["CRI_TEST_APP_DATABASE_URL"])
        try:
            with app_db.transaction("beta") as conn:
                self.assertEqual(conn.fetchall("SELECT account_ref FROM monitored_accounts"), [])
            with app_db.transaction("alpha") as conn:
                self.assertEqual(len(conn.fetchall("SELECT account_ref FROM monitored_accounts")), 1)
            with self.assertRaises(Exception), app_db.transaction("alpha") as conn:
                conn.execute("DELETE FROM audit_log")
        finally:
            app_db.close()


if __name__ == "__main__":
    unittest.main()
