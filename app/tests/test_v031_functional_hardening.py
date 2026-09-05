from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from app.core.enums import (
    AssetType,
    DataMode,
    MarginState,
    PortfolioActionType,
    RiskDecision,
)
from app.core.evaluator import CollateralRiskEngine
from app.core.models import (
    AccountState,
    AssetRiskResult,
    Holding,
    Loan,
    MarketData,
    OrderBook,
    OrderBookLevel,
    Policy,
    PortfolioActionCheck,
    RiskAdjustmentBreakdown,
)
from app.lifecycle.service import CreditLifecycleEngine
from app.liquidation.plan import build_liquidation_plan
from app.main import app
from app.market_data.aggregator import MarketDataAggregator
from app.market_data.fx import FXSelector
from app.market_data.identity import InstrumentIdentity
from app.market_data.policy import FXPolicy, MarketDataPolicy
from app.market_data.providers import (
    ClientSuppliedProvider,
    FXRate,
    MockEquityProvider,
    MockFXProvider,
    RawQuote,
)
from app.simulations.backtester import compare_flat_ltv_to_dynamic_engine
from app.simulations.scenarios import apply_market_shock


class V031FunctionalHardeningTests(unittest.TestCase):
    def quote(self, identity: InstrumentIdentity, price: float = 100.0) -> RawQuote:
        return RawQuote(
            identity,
            price,
            bid=price - 1,
            ask=price + 1,
            timestamp=datetime.now(UTC),
        )

    def test_stable_keys_prevent_asset_id_exchange_currency_and_type_collisions(
        self,
    ) -> None:
        same_id_ngx = InstrumentIdentity(
            "DUP", "ABC", "NGX", "NGN", AssetType.LISTED_EQUITY
        )
        same_id_nyse = InstrumentIdentity(
            "DUP", "ABC", "NYSE", "USD", AssetType.LISTED_EQUITY
        )
        same_id_eur = InstrumentIdentity(
            "DUP", "ABC", "XPAR", "EUR", AssetType.LISTED_EQUITY
        )
        same_symbol_etf = InstrumentIdentity(
            "ABC_ETF", "ABC", "NYSE", "USD", AssetType.ETF
        )
        provider = MockEquityProvider(
            {
                i.stable_key: self.quote(i)
                for i in [same_id_ngx, same_id_nyse, same_id_eur, same_symbol_etf]
            }
        )
        result = MarketDataAggregator(equity_provider=provider).normalize(
            [same_id_ngx, same_id_nyse, same_id_eur, same_symbol_etf],
            data_mode=DataMode.PROVIDED_BY_US,
        )
        self.assertEqual(len(result.normalized_market_data), 4)
        self.assertIn("NGX:ABC:NGN:LISTED_EQUITY", result.normalized_market_data)
        self.assertIn("NYSE:ABC:USD:LISTED_EQUITY", result.normalized_market_data)
        self.assertIn("XPAR:ABC:EUR:LISTED_EQUITY", result.normalized_market_data)
        self.assertIn("NYSE:ABC:USD:ETF", result.normalized_market_data)

    def test_evaluator_keyed_map_matches_holdings_with_explicit_identity(self) -> None:
        holding = Holding("MTNN", AssetType.LISTED_EQUITY, 10, "NGN")
        identity = InstrumentIdentity(
            "MTNN", "MTNN", "NGX", "NGN", AssetType.LISTED_EQUITY
        )
        result = MarketDataAggregator(
            equity_provider=MockEquityProvider(),
            fx_provider=MockFXProvider(),
        ).normalize(
            instruments=[identity],
            holdings=[holding],
            loan_currency="USD",
            data_mode=DataMode.PROVIDED_BY_US,
        )
        core = result.to_core_market_data()
        self.assertIn("MTNN", core)
        self.assertEqual(core["MTNN"].asset_id, "MTNN")
        self.assertEqual(
            result.evaluator_key_to_stable_key["MTNN"], identity.stable_key
        )

    def test_evaluator_keyed_map_derived_colon_holding_and_two_exchanges(self) -> None:
        ngx = Holding("NGX:MTNN:NGN", AssetType.LISTED_EQUITY, 1, "NGN")
        nyse = Holding("NYSE:MTNN:USD", AssetType.LISTED_EQUITY, 1, "USD")
        id2 = InstrumentIdentity(
            "NYSE:MTNN:USD", "MTNN", "NYSE", "USD", AssetType.LISTED_EQUITY
        )
        id1 = InstrumentIdentity(
            "NGX:MTNN:NGN", "MTNN", "NGX", "NGN", AssetType.LISTED_EQUITY
        )
        provider = MockEquityProvider(
            {id1.stable_key: self.quote(id1), id2.stable_key: self.quote(id2)}
        )
        result = MarketDataAggregator(equity_provider=provider).normalize(
            holdings=[ngx, nyse], loan_currency="USD", data_mode=DataMode.PROVIDED_BY_US
        )
        core = result.to_core_market_data()
        self.assertIn("NGX:MTNN:NGN", core)
        self.assertIn("NYSE:MTNN:USD", core)

    def test_conservative_fx_filters_stale_lower_rate_before_selecting(self) -> None:
        now = datetime.now(UTC)
        stale_low = FXRate(
            "NGN", "USD", 0.0005, now - timedelta(days=10), quality_score=1.0
        )
        fresh_high = FXRate("NGN", "USD", 0.0007, now, quality_score=0.9)
        decision = FXSelector(
            ClientSuppliedProvider(fx_rates={("NGN", "USD"): stale_low}),
            MockFXProvider({("NGN", "USD"): fresh_high}),
        ).select_rate(
            "NGN",
            "USD",
            FXPolicy(
                max_fx_age_minutes=60,
                stale_fx_haircut=0.9,
                use_conservative_rate_when_sources_disagree=True,
            ),
            now=now,
        )
        self.assertEqual(decision.rate.rate, 0.0007)
        self.assertIn("conservative_fx_rate_selected", decision.warnings)

    def test_conservative_fx_no_quality_sources_marks_missing(self) -> None:
        now = datetime.now(UTC)
        stale = now - timedelta(days=10)
        decision = FXSelector(
            ClientSuppliedProvider(
                fx_rates={
                    ("NGN", "USD"): FXRate(
                        "NGN", "USD", 0.0005, stale, quality_score=1.0
                    )
                }
            ),
            MockFXProvider(
                {("NGN", "USD"): FXRate("NGN", "USD", 0.0007, stale, quality_score=1.0)}
            ),
        ).select_rate(
            "NGN",
            "USD",
            FXPolicy(
                max_fx_age_minutes=60,
                stale_fx_haircut=0.9,
                use_conservative_rate_when_sources_disagree=True,
            ),
            now=now,
        )
        self.assertIsNone(decision.rate)
        self.assertTrue(decision.missing_required_fx)
        self.assertIn("no_fx_source_passed_quality_threshold", decision.warnings)

    def test_normalize_endpoint_rejects_empty_and_prefers_instruments(self) -> None:
        try:
            from fastapi.testclient import TestClient
        except RuntimeError as exc:
            self.skipTest(f"FastAPI TestClient unavailable: {exc}")
        client = TestClient(app)
        self.assertEqual(
            client.post("/market-data/normalize", json={}).status_code, 422
        )
        payload = {
            "instruments": [
                {
                    "asset_id": "MTNN",
                    "symbol": "MTNN",
                    "exchange": "NGX",
                    "currency": "NGN",
                    "asset_type": "listed_equity",
                }
            ],
            "holdings": [
                {
                    "asset_id": "IGNORED",
                    "asset_type": "listed_equity",
                    "quantity": 1,
                    "currency": "USD",
                }
            ],
            "loan_currency": "USD",
            "data_mode": "provided_by_us",
        }
        response = client.post("/market-data/normalize", json=payload)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["normalized_market_data"], {})
        self.assertIn("MTNN", body["missing_data"])
        self.assertEqual(body["market_data_model_version"], "market-data-v0.7.0")

    def test_numeric_validation_rejects_invalid_inputs(self) -> None:
        with self.assertRaises(ValueError):
            Holding("BAD", AssetType.ETF, -1)
        with self.assertRaises(ValueError):
            Loan(-1)
        with self.assertRaises(ValueError):
            RawQuote(
                InstrumentIdentity("A", "A", "X", "USD", AssetType.ETF),
                10,
                bid=11,
                ask=10,
            )
        with self.assertRaises(ValueError):
            FXRate("USD", "NGN", 0)
        with self.assertRaises(ValueError):
            MarketData("BAD", 1, data_quality_score=1.2)
        with self.assertRaises(ValueError):
            MarketDataPolicy(max_quote_age_minutes_by_exchange={"X": 0})

    def test_buy_ngn_asset_in_usd_account_uses_market_identity_currency(self) -> None:
        engine = CreditLifecycleEngine(CollateralRiskEngine(None), None)
        md = {
            "SPY": MarketData("SPY", 100, metadata={"instrument": {"currency": "USD"}}),
            "NGX:MTNN:NGN": MarketData(
                "NGX:MTNN:NGN", 0.20, metadata={"instrument": {"currency": "NGN"}}
            ),
        }
        account = AccountState(
            "acct",
            [Holding("SPY", AssetType.ETF, 100)],
            100,
            Loan(0, currency="USD"),
            0,
            0,
            MarginState.SAFE,
        )
        result = engine.check_portfolio_action(
            account,
            PortfolioActionCheck(
                PortfolioActionType.BUY,
                "NGX:MTNN:NGN",
                AssetType.LISTED_EQUITY,
                quantity=100,
            ),
            Policy.default(),
            md,
        )
        added = next(
            h
            for h in result.projected_account_state.holdings
            if h.asset_id == "NGX:MTNN:NGN"
        )
        self.assertEqual(added.currency, "NGN")

    def test_buy_with_draw_safe_excess_and_partial_behavior(self) -> None:
        engine = CreditLifecycleEngine(CollateralRiskEngine(None), None)
        md = {
            "SPY": MarketData("SPY", 100, metadata={"instrument": {"currency": "USD"}}),
            "BND": MarketData("BND", 10, metadata={"instrument": {"currency": "USD"}}),
        }
        account = AccountState(
            "acct",
            [Holding("SPY", AssetType.ETF, 100)],
            0,
            Loan(0),
            0,
            0,
            MarginState.SAFE,
        )
        safe = engine.check_portfolio_action(
            account,
            PortfolioActionCheck(
                PortfolioActionType.BUY,
                "BND",
                AssetType.BOND,
                quantity=10,
                funding_source="draw",
            ),
            Policy.default(),
            md,
        )
        self.assertIn(
            safe.decision, {RiskDecision.APPROVE, RiskDecision.REDUCE_AVAILABLE_CREDIT}
        )
        with self.assertRaises(ValueError):
            engine.check_portfolio_action(
                account,
                PortfolioActionCheck(
                    PortfolioActionType.BUY,
                    "BND",
                    AssetType.BOND,
                    quantity=10000,
                    funding_source="draw",
                ),
                Policy.default(),
                md,
            )

    def test_liquidation_plan_incomplete_when_target_unmet(self) -> None:
        asset = AssetRiskResult(
            "THIN",
            AssetType.ETF,
            1,
            100,
            0.7,
            0.5,
            50,
            25,
            0.5,
            1,
            0.2,
            [],
            True,
            RiskAdjustmentBreakdown(1, 1, 1, 1, 1, 1),
        )
        plan = build_liquidation_plan("acct", [asset], MarginState.LIQUIDATION, 100)
        self.assertFalse(plan.plan_complete)
        self.assertEqual(plan.estimated_total_recovery, 25)
        self.assertGreater(plan.unrecovered_target_amount, 0)
        self.assertIn("insufficient_liquid_collateral", plan.reason)

    def test_backtester_uses_recovery_limited_safe_credit(self) -> None:
        policy = Policy.default()
        md = {
            "THIN": MarketData(
                "THIN",
                100,
                average_daily_volume=1,
                average_dollar_volume=100,
                volatility_30d=0.1,
                order_book=OrderBook(bids=[OrderBookLevel(50, 1)]),
                metadata={"instrument": {"currency": "USD"}},
            )
        }
        results = compare_flat_ltv_to_dynamic_engine(
            "acct", [Holding("THIN", AssetType.ETF, 100)], Loan(0), policy, md
        )
        engine_eval = CollateralRiskEngine(None).evaluate(
            "acct", [Holding("THIN", AssetType.ETF, 100)], Loan(0), policy, md
        )
        self.assertLess(
            results[0].dynamic_draw_limit, engine_eval.approved_credit_limit
        )

    def test_apply_market_shock_shocks_order_book_depth(self) -> None:
        market = MarketData(
            "X",
            100,
            bid=99,
            ask=101,
            order_book=OrderBook(
                bids=[OrderBookLevel(99, 1000), OrderBookLevel(98, 1000)]
            ),
        )
        shocked = apply_market_shock(
            market, price_shock=-0.1, spread_multiplier=5, volume_multiplier=0.2
        )
        self.assertLess(
            shocked.order_book.bids[0].price, market.order_book.bids[0].price
        )
        self.assertLess(shocked.order_book.bids[0].quantity, 1000 * 0.2)


if __name__ == "__main__":
    unittest.main()
