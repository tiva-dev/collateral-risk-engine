from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

try:
    from fastapi.testclient import TestClient
except RuntimeError as exc:  # pragma: no cover - exercised only in minimal local envs
    TestClient = None
    TESTCLIENT_IMPORT_ERROR = exc
else:
    TESTCLIENT_IMPORT_ERROR = None

from app.core.enums import AssetType, DataMode, MarginState
from app.core.evaluator import CollateralRiskEngine
from app.core.models import Holding, Loan, Policy
from app.lifecycle.service import CreditLifecycleEngine
from app.main import app
from app.market_data.identity import InstrumentIdentity
from app.market_data.policy import MarketDataPolicy
from app.market_data.providers import FXRate, RawQuote
from app.monitoring.events import serialize_sse_event
from app.monitoring.market_updates import InMemoryMarketDataCache
from app.monitoring.models import (
    MonitoringEvent,
    MonitoringEventType,
    MonitoringSeverity,
    MonitoringStatus,
)
from app.monitoring.repositories import (
    InMemoryMonitoredAccountRepository,
    InMemoryMonitoringEventRepository,
)
from app.monitoring.scheduler import SimpleMonitoringScheduler
from app.monitoring.service import MonitoringService

BASE_LTV = {
    "cash": 0.95,
    "bond": 0.80,
    "bond_fund": 0.78,
    "etf": 0.70,
    "listed_equity": 0.65,
    "high_volatility_equity": 0.35,
    "crypto": 0.20,
    "option": 0.05,
    "private_asset": 0.0,
    "other": 0.0,
}


def quote(
    asset_id="SPY",
    symbol="SPY",
    price=100.0,
    quality=1.0,
    warnings=None,
    currency="USD",
    exchange="NYSE",
):
    inst = InstrumentIdentity(asset_id, symbol, exchange, currency, AssetType.ETF)
    return RawQuote(
        inst,
        price,
        price - 0.1,
        price + 0.1,
        1_000_000,
        price * 1_000_000,
        0.15,
        0.15,
        timestamp=datetime.now(UTC),
        provider_name="test",
        data_quality_score=quality,
        warnings=warnings or [],
    )


def service() -> MonitoringService:
    return MonitoringService(
        InMemoryMonitoredAccountRepository(),
        InMemoryMonitoringEventRepository(),
        CreditLifecycleEngine(
            CollateralRiskEngine(audit_logger=None), audit_logger=None
        ),
        market_data_cache=InMemoryMarketDataCache(),
        audit_logger=None,
    )


def register(
    svc: MonitoringService,
    account_ref="acct",
    loan=1_000.0,
    price=100.0,
    status_data_mode=DataMode.CLIENT_SUPPLIED,
    monitoring_status=MonitoringStatus.ACTIVE,
    run_initial_evaluation=True,
):
    return svc.register_account(
        account_ref=account_ref,
        holdings=[Holding("SPY", AssetType.ETF, 100.0, "USD")],
        pledged_cash_balance=0.0,
        loan=Loan(loan, currency="USD"),
        loan_currency="USD",
        policy=Policy.default(),
        data_mode=status_data_mode,
        market_data_policy=MarketDataPolicy(),
        client_supplied_quotes={"SPY": quote(price=price)},
        monitoring_status=monitoring_status,
        run_initial_evaluation=run_initial_evaluation,
    )


class MonitoringRepositoryTests(unittest.TestCase):
    def test_in_memory_event_repository_append_get_list_filter(self):
        repo = InMemoryMonitoringEventRepository()
        event = MonitoringEvent(
            "evt1", "acct", MonitoringEventType.FX_MISSING, MonitoringSeverity.WARNING
        )
        result = repo.append(event)
        self.assertTrue(result.created)
        self.assertEqual(repo.get("evt1"), event)
        self.assertEqual(repo.list(account_ref="acct"), [event])
        self.assertEqual(repo.list(event_type="fx_missing"), [event])
        self.assertEqual(repo.list(severity="critical"), [])

    def test_account_repository_save_get_list_delete_and_instrument_lookup(self):
        svc = service()
        _account, _ = register(svc, "repo_acct")
        repo = svc.account_repo
        self.assertEqual(repo.get("repo_acct").account_ref, "repo_acct")
        self.assertTrue(repo.list_active())
        self.assertEqual(repo.list_by_instrument("SPY")[0].account_ref, "repo_acct")
        self.assertTrue(repo.delete("repo_acct"))
        self.assertIsNone(repo.get("repo_acct"))


class MonitoringServiceTests(unittest.TestCase):
    def test_idempotent_draw_repayment_and_obligation_update(self):
        svc = service()
        register(svc, "loan_updates", loan=1_000.0)

        account, events = svc.apply_loan_update(
            "loan_updates",
            event_reference="client-event-1",
            draw_amount=500.0,
            repayment_amount=100.0,
            accrued_interest_delta=10.0,
            fee_delta=5.0,
            trigger_tick=False,
        )
        self.assertEqual(account.loan.principal, 1_415.0)
        self.assertEqual(account.loan.accrued_interest, 0.0)
        self.assertEqual(account.loan.fees, 0.0)
        self.assertEqual(events[0].event_type, MonitoringEventType.LOAN_BALANCE_UPDATED)

        duplicate, duplicate_events = svc.apply_loan_update(
            "loan_updates",
            event_reference="client-event-1",
            draw_amount=500.0,
            trigger_tick=False,
        )
        self.assertEqual(duplicate.loan.principal, 1_415.0)
        self.assertEqual(duplicate_events, [])

    def test_register_account_emits_initial_tick_and_get_list_delete(self):
        svc = service()
        account, events = register(svc, "reg_acct")
        self.assertEqual(account.last_margin_state, MarginState.SAFE)
        self.assertEqual(
            events[0].event_type, MonitoringEventType.MONITORING_TICK_COMPLETED
        )
        self.assertEqual(svc.get_account("reg_acct"), account)
        self.assertEqual(len(svc.list_accounts()), 1)
        self.assertTrue(svc.delete_account("reg_acct"))

    def test_register_margin_call_emits_margin_event(self):
        svc = service()
        _, events = register(svc, "margin_acct", loan=5_000.0)
        self.assertIn(
            MonitoringEventType.MARGIN_CALL_TRIGGERED,
            [event.event_type for event in events],
        )

    def test_tick_safe_account_no_change_does_not_spam_persisted_info(self):
        svc = service()
        register(svc, "safe_acct")
        before = len(svc.event_repo.list(account_ref="safe_acct"))
        _, events = svc.evaluate_account("safe_acct")
        after = len(svc.event_repo.list(account_ref="safe_acct"))
        self.assertEqual(events, [])
        self.assertEqual(after, before)

    def test_state_change_margin_liquidation_and_available_credit_events(self):
        svc = service()
        register(svc, "state_acct", loan=1_000.0)
        svc.market_data_cache.merge({"SPY": quote(price=75.0)}, {}, "test")
        _, events = svc.evaluate_account("state_acct")
        self.assertIn(
            MonitoringEventType.AVAILABLE_CREDIT_CHANGED,
            [event.event_type for event in events],
        )
        svc.market_data_cache.merge({"SPY": quote(price=25.0)}, {}, "test")
        _, events = svc.evaluate_account("state_acct")
        types = [event.event_type for event in events]
        self.assertIn(MonitoringEventType.RISK_STATE_CHANGED, types)
        self.assertIn(MonitoringEventType.MARGIN_CALL_TRIGGERED, types)
        svc.market_data_cache.merge({"SPY": quote(price=10.0)}, {}, "test")
        _, events = svc.evaluate_account("state_acct")
        liquidation_event = next(
            event
            for event in events
            if event.event_type == MonitoringEventType.LIQUIDATION_TRIGGERED
        )
        self.assertTrue(liquidation_event.liquidation_plan["orders"])
        order = liquidation_event.liquidation_plan["orders"][0]
        self.assertEqual(order["asset_id"], "SPY")
        self.assertGreater(order["requested_quantity"], 0)
        self.assertGreater(order["minimum_limit_price"], 0)

    def test_missing_fx_and_market_data_degradation_events(self):
        svc = service()
        fx_quote = quote(
            asset_id="AIR", symbol="AIR", price=100.0, currency="EUR", exchange="XPAR"
        )
        svc.register_account(
            account_ref="fx_acct",
            holdings=[Holding("AIR", AssetType.ETF, 100.0, "EUR")],
            pledged_cash_balance=0.0,
            loan=Loan(1_000.0, currency="USD"),
            loan_currency="USD",
            policy=Policy.default(),
            data_mode=DataMode.CLIENT_SUPPLIED,
            market_data_policy=MarketDataPolicy(),
            client_supplied_quotes={"AIR": fx_quote},
        )
        # Initial warning exists, but changing to a worse low-quality quote creates degradation.
        svc.market_data_cache.merge(
            {
                "AIR": quote(
                    asset_id="AIR",
                    symbol="AIR",
                    price=100.0,
                    quality=0.1,
                    warnings=["stale_quote"],
                    currency="EUR",
                    exchange="XPAR",
                )
            },
            {},
            "test",
        )
        _, events = svc.evaluate_account("fx_acct")
        types = [event.event_type for event in events]
        self.assertIn(MonitoringEventType.MARKET_DATA_DEGRADED, types)
        self.assertTrue(
            any(
                "missing" in warning
                for event in svc.event_repo.list(account_ref="fx_acct")
                for warnings in event.market_data_warnings.values()
                for warning in warnings
            )
        )

    def test_monitoring_error_emits_event(self):
        svc = service()
        register(svc, "err_acct")
        account = svc.get_account("err_acct")
        account.holdings = []
        svc.account_repo.update(account)
        with self.assertRaises(Exception):
            svc.evaluate_account("err_acct")
        self.assertIn(
            MonitoringEventType.MONITORING_ERROR,
            [event.event_type for event in svc.event_repo.list(account_ref="err_acct")],
        )

    def test_market_data_update_affects_accounts_and_triggers_tick(self):
        svc = service()
        register(svc, "update_acct")
        result = svc.ingest_market_data_update(
            {"SPY": quote(price=80.0)}, {}, [], "internal_test", True
        )
        self.assertEqual(result["affected_accounts"], ["update_acct"])
        self.assertTrue(result["tick_results"])

    def test_ambiguous_symbol_update_returns_warning(self):
        svc = service()
        register(svc, "ambig1", price=100.0)
        svc.register_account(
            account_ref="ambig2",
            holdings=[Holding("NASDAQ:SPY:USD", AssetType.ETF, 10.0, "USD")],
            pledged_cash_balance=0.0,
            loan=Loan(100.0),
            loan_currency="USD",
            policy=Policy.default(),
            data_mode=DataMode.HYBRID,
            market_data_policy=MarketDataPolicy(),
        )
        result = svc.ingest_market_data_update(
            {"SPY": quote(price=90.0)}, {}, [], "test", False
        )
        self.assertIn("ambiguous_symbol:SPY", result["warnings"])
        self.assertEqual(result["affected_accounts"], ["ambig1"])

    def test_fx_update_affects_foreign_currency_account(self):
        svc = service()
        svc.register_account(
            account_ref="fx_update_acct",
            holdings=[Holding("AIR", AssetType.ETF, 10.0, "EUR")],
            pledged_cash_balance=0.0,
            loan=Loan(100.0),
            loan_currency="USD",
            policy=Policy.default(),
            data_mode=DataMode.CLIENT_SUPPLIED,
            market_data_policy=MarketDataPolicy(),
            client_supplied_quotes={
                "AIR": quote(
                    asset_id="AIR",
                    symbol="AIR",
                    price=100.0,
                    currency="EUR",
                    exchange="XPAR",
                )
            },
        )
        result = svc.ingest_market_data_update(
            {}, {("EUR", "USD"): FXRate("EUR", "USD", 1.1)}, [], "test", False
        )
        self.assertEqual(result["affected_accounts"], ["fx_update_acct"])

    def test_tick_status_enforcement_and_force(self):
        svc = service()
        register(svc, "active_acct")
        self.assertTrue(svc.evaluate_account("active_acct", force_tick_event=True)[1])
        register(
            svc,
            "paused_acct",
            monitoring_status=MonitoringStatus.PAUSED,
            run_initial_evaluation=False,
        )
        with self.assertRaises(ValueError):
            svc.evaluate_account("paused_acct")
        _, events = svc.evaluate_account(
            "paused_acct", force=True, force_tick_event=True
        )
        self.assertTrue(events)
        register(
            svc,
            "disabled_acct",
            monitoring_status=MonitoringStatus.DISABLED,
            run_initial_evaluation=False,
        )
        with self.assertRaises(ValueError):
            svc.evaluate_account("disabled_acct")
        self.assertTrue(
            svc.evaluate_account("disabled_acct", force=True, force_tick_event=True)[1]
        )

    def test_paused_registration_can_skip_initial_evaluation(self):
        svc = service()
        account, events = register(
            svc,
            "paused_reg",
            monitoring_status=MonitoringStatus.PAUSED,
            run_initial_evaluation=False,
        )
        self.assertEqual(events, [])
        self.assertIsNone(account.last_evaluation)
        self.assertIsNone(account.last_margin_state)
        self.assertIsNone(account.next_check_after)
        self.assertEqual(svc.event_repo.list(account_ref="paused_reg"), [])

    def test_failed_registration_rolls_back_account(self):
        svc = service()
        with self.assertRaises(Exception):
            svc.register_account(
                account_ref="bad_reg",
                holdings=[],
                pledged_cash_balance=0.0,
                loan=Loan(100.0),
                loan_currency="USD",
                policy=Policy.default(),
                data_mode=DataMode.CLIENT_SUPPLIED,
                market_data_policy=MarketDataPolicy(),
            )
        self.assertIsNone(svc.get_account("bad_reg"))

    def test_status_update_and_delete_semantics(self):
        svc = service()
        register(svc, "status_acct")
        svc.update_account_status("status_acct", MonitoringStatus.PAUSED)
        self.assertEqual(svc.account_repo.list_active(), [])
        svc.update_account_status("status_acct", MonitoringStatus.ACTIVE)
        self.assertEqual(
            [a.account_ref for a in svc.account_repo.list_active()], ["status_acct"]
        )
        self.assertTrue(svc.delete_account("status_acct"))
        self.assertIsNone(svc.get_account("status_acct"))

    def test_fx_update_uses_stable_identity_currency(self):
        svc = service()
        svc.register_account(
            account_ref="ngx_acct",
            holdings=[Holding("NGX:MTNN:NGN", AssetType.LISTED_EQUITY, 10.0, "USD")],
            pledged_cash_balance=0.0,
            loan=Loan(100.0),
            loan_currency="USD",
            policy=Policy.default(),
            data_mode=DataMode.CLIENT_SUPPLIED,
            market_data_policy=MarketDataPolicy(),
            client_supplied_quotes={
                "NGX:MTNN:NGN": quote(
                    asset_id="NGX:MTNN:NGN",
                    symbol="MTNN",
                    price=100.0,
                    currency="NGN",
                    exchange="NGX",
                )
            },
            client_supplied_fx_rates={("NGN", "USD"): FXRate("NGN", "USD", 0.001)},
        )
        result = svc.ingest_market_data_update(
            {}, {("NGN", "USD"): FXRate("NGN", "USD", 0.0011)}, [], "test", False
        )
        self.assertEqual(result["affected_accounts"], ["ngx_acct"])

    def test_event_dedupe_ttl_and_repository_mutability(self):
        repo = InMemoryMonitoringEventRepository()
        event = MonitoringEvent(
            "evt_a",
            "acct",
            MonitoringEventType.FX_MISSING,
            MonitoringSeverity.WARNING,
            dedupe_key="k",
        )
        self.assertTrue(repo.append(event, dedupe_ttl_seconds=1).created)
        self.assertFalse(
            repo.append(
                MonitoringEvent(
                    "evt_b",
                    "acct",
                    MonitoringEventType.FX_MISSING,
                    MonitoringSeverity.WARNING,
                    dedupe_key="k",
                ),
                dedupe_ttl_seconds=1,
            ).created
        )
        later = MonitoringEvent(
            "evt_c",
            "acct",
            MonitoringEventType.FX_MISSING,
            MonitoringSeverity.WARNING,
            dedupe_key="k",
            created_at=event.created_at + timedelta(seconds=2),
        )
        self.assertTrue(repo.append(later, dedupe_ttl_seconds=1).created)
        svc = service()
        _account, _ = register(svc, "mutable_acct")
        returned = svc.get_account("mutable_acct")
        returned.monitoring_status = MonitoringStatus.DISABLED
        self.assertEqual(
            svc.get_account("mutable_acct").monitoring_status, MonitoringStatus.ACTIVE
        )

    def test_market_data_cache_ambiguous_symbol_provider_lookup(self):
        cache = InMemoryMarketDataCache()
        q1 = quote(
            asset_id="NYSE:ABC:USD", symbol="ABC", exchange="NYSE", currency="USD"
        )
        q2 = quote(asset_id="LSE:ABC:GBP", symbol="ABC", exchange="LSE", currency="GBP")
        cache.merge({q1.instrument.stable_key: q1}, {}, "test")
        cache.merge({q2.instrument.stable_key: q2}, {}, "test")
        self.assertIsNotNone(cache.provider().get_quote(q1.instrument))
        self.assertIsNone(cache.snapshot().quotes.get("ABC"))
        self.assertIsNone(
            cache.provider().get_quote(
                InstrumentIdentity("TSX:ABC:CAD", "ABC", "TSX", "CAD", AssetType.ETF)
            )
        )

    def test_scheduling_policy_by_margin_state(self):
        scheduler = SimpleMonitoringScheduler()
        now = datetime.now(UTC)
        self.assertGreater(
            scheduler.next_check_after(MarginState.SAFE, now),
            scheduler.next_check_after(MarginState.MARGIN_CALL, now),
        )
        self.assertEqual(scheduler.next_check_after(MarginState.LIQUIDATION, now), now)

    def test_event_serialization(self):
        event = MonitoringEvent(
            "evt_stream",
            "acct",
            MonitoringEventType.MONITORING_TICK_COMPLETED,
            MonitoringSeverity.INFO,
        )
        payload = serialize_sse_event(event)
        self.assertIn(
            "text/event-stream" if False else "event: monitoring_tick_completed",
            payload,
        )
        self.assertIn("id: evt_stream", payload)


@unittest.skipIf(
    TestClient is None, f"fastapi TestClient unavailable: {TESTCLIENT_IMPORT_ERROR}"
)
class MonitoringEndpointTests(unittest.TestCase):
    def test_endpoints_register_get_list_events_stream_delete(self):
        client = TestClient(app)
        account_ref = "api_v04_acct"
        payload = {
            "account_ref": account_ref,
            "holdings": [
                {
                    "asset_id": "SPY",
                    "asset_type": "etf",
                    "quantity": 100.0,
                    "currency": "USD",
                }
            ],
            "pledged_cash_balance": 0.0,
            "loan": {"principal": 1000.0, "currency": "USD"},
            "loan_currency": "USD",
            "policy": {"base_ltv": BASE_LTV},
            "data_mode": "client_supplied",
            "client_supplied_quotes": {
                "SPY": {
                    "asset_id": "SPY",
                    "symbol": "SPY",
                    "currency": "USD",
                    "asset_type": "etf",
                    "local_price": 100.0,
                    "timestamp": "2025-01-02T10:00:00+00:00",
                }
            },
        }
        created = client.post("/monitoring/accounts", json=payload)
        self.assertEqual(created.status_code, 200, created.text)
        self.assertEqual(created.json()["account"]["account_ref"], account_ref)
        self.assertTrue(created.json()["events"])
        self.assertEqual(
            client.get(f"/monitoring/accounts/{account_ref}").status_code, 200
        )
        self.assertEqual(client.get("/monitoring/accounts").status_code, 200)
        self.assertEqual(
            client.post(f"/monitoring/accounts/{account_ref}/tick").status_code, 200
        )
        update = client.post(
            "/monitoring/market-data/update",
            json={
                "quote_updates": {
                    "SPY": {
                        "asset_id": "SPY",
                        "symbol": "SPY",
                        "currency": "USD",
                        "asset_type": "etf",
                        "local_price": 90.0,
                        "timestamp": "2025-01-03T10:00:00+00:00",
                    }
                },
                "trigger_tick": True,
            },
        )
        self.assertEqual(update.status_code, 200, update.text)
        self.assertIn(account_ref, update.json()["affected_accounts"])
        loan_update = client.post(
            f"/monitoring/accounts/{account_ref}/loan",
            json={
                "event_reference": "api-draw-1",
                "repayment_amount": 50.0,
                "trigger_tick": False,
            },
        )
        self.assertEqual(loan_update.status_code, 200, loan_update.text)
        self.assertEqual(loan_update.json()["account"]["loan"]["principal"], 950.0)
        duplicate = client.post(
            f"/monitoring/accounts/{account_ref}/loan",
            json={
                "event_reference": "api-draw-1",
                "repayment_amount": 50.0,
                "trigger_tick": False,
            },
        )
        self.assertEqual(duplicate.status_code, 200, duplicate.text)
        self.assertEqual(duplicate.json()["account"]["loan"]["principal"], 950.0)
        events = client.get("/monitoring/events", params={"account_ref": account_ref})
        self.assertEqual(events.status_code, 200)
        event_id = events.json()["events"][0]["event_id"]
        self.assertEqual(client.get(f"/monitoring/events/{event_id}").status_code, 200)
        stream = client.get("/monitoring/events/stream")
        self.assertEqual(stream.status_code, 200)
        self.assertIn("text/event-stream", stream.headers["content-type"])
        paused_ref = "api_paused_acct"
        paused_payload = {
            **payload,
            "account_ref": paused_ref,
            "monitoring_status": "paused",
            "run_initial_evaluation": False,
        }
        paused = client.post("/monitoring/accounts", json=paused_payload)
        self.assertEqual(paused.status_code, 200, paused.text)
        self.assertEqual(paused.json()["events"], [])
        self.assertEqual(
            client.post(f"/monitoring/accounts/{paused_ref}/tick").status_code, 422
        )
        self.assertEqual(
            client.post(
                f"/monitoring/accounts/{paused_ref}/tick", params={"force": True}
            ).status_code,
            200,
        )
        patched = client.patch(
            f"/monitoring/accounts/{paused_ref}/status",
            json={"monitoring_status": "disabled"},
        )
        self.assertEqual(patched.status_code, 200, patched.text)
        self.assertEqual(patched.json()["account"]["monitoring_status"], "disabled")
        empty_stream = client.get("/monitoring/events/stream", params={"limit": 0})
        self.assertIn(": monitoring stream ready", empty_stream.text)
        self.assertIn("data:", stream.text)
        self.assertEqual(
            client.delete(f"/monitoring/accounts/{account_ref}").status_code, 200
        )


if __name__ == "__main__":
    unittest.main()
