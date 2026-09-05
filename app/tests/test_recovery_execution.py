from __future__ import annotations

import unittest
from datetime import UTC, date, datetime

from app.core.enums import AssetType
from app.core.models import Holding, Loan, MarketData
from app.credit.interest import InterestPolicy
from app.historical_data.models import HistoricalBar
from app.liquidation.execution import (
    build_recovery_advisory,
    execute_recovery_advisory,
    projected_interest_buffer,
)
from app.liquidation.policy import LiquidationExecutionPolicy
from app.simulations.replay import (
    POLICY_ORIGINATION,
    HistoricalReplayEngine,
    StressOverlay,
)
from app.simulations.scenarios.official_portfolios import (
    OfficialPortfolioScenario,
    official_portfolio_scenarios,
)


class RecoveryExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.holdings = [
            Holding("AAA", AssetType.LISTED_EQUITY, 100, "USD", "NYSE")
        ]
        self.market_data = {
            "AAA": MarketData(
                "AAA",
                101,
                bid=100,
                ask=102,
                average_daily_volume=1_000,
                timestamp=datetime(2026, 1, 5, tzinfo=UTC),
            )
        }

    def test_advisory_contains_quantity_limit_price_and_net_recovery(self) -> None:
        policy = LiquidationExecutionPolicy(execution_cost_rate=0.01)
        advisory = build_recovery_advisory(
            holdings=self.holdings,
            market_data=self.market_data,
            target_net_recovery=4_950,
            policy=policy,
            trigger_state="liquidation",
            issued_date=date(2026, 1, 5),
        )

        order = advisory["orders"][0]
        self.assertEqual(order["asset_id"], "AAA")
        self.assertEqual(order["requested_quantity"], 50)
        self.assertEqual(order["minimum_limit_price"], 90)
        self.assertEqual(order["estimated_net_proceeds"], 4_950)
        self.assertTrue(advisory["plan_complete"])

    def test_execution_uses_bid_costs_and_participation_cap(self) -> None:
        policy = LiquidationExecutionPolicy(
            execution_cost_rate=0.01,
            max_participation_rate=0.02,
        )
        advisory = build_recovery_advisory(
            holdings=self.holdings,
            market_data=self.market_data,
            target_net_recovery=4_950,
            policy=policy,
            trigger_state="liquidation",
            issued_date=date(2026, 1, 5),
        )
        updated, execution = execute_recovery_advisory(
            holdings=self.holdings,
            market_data=self.market_data,
            advisory=advisory,
            remaining_target=4_950,
            policy=policy,
            observation_date=date(2026, 1, 5),
        )

        fill = execution["fills"][0]
        self.assertEqual(fill["filled_quantity"], 20)
        self.assertEqual(fill["execution_price"], 100)
        self.assertEqual(fill["execution_costs"], 20)
        self.assertEqual(fill["net_proceeds"], 1_980)
        self.assertEqual(updated[0].quantity, 80)
        self.assertEqual(execution["status"], "partial")

    def test_halted_stale_and_below_limit_quotes_do_not_fill(self) -> None:
        policy = LiquidationExecutionPolicy(maximum_quote_age_days=1)
        advisory = build_recovery_advisory(
            holdings=self.holdings,
            market_data=self.market_data,
            target_net_recovery=1_000,
            policy=policy,
            trigger_state="liquidation",
            issued_date=date(2026, 1, 5),
        )
        stale = {
            "AAA": MarketData(
                "AAA",
                80,
                bid=80,
                ask=81,
                average_daily_volume=1_000,
                timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            )
        }
        updated, execution = execute_recovery_advisory(
            holdings=self.holdings,
            market_data=stale,
            advisory=advisory,
            remaining_target=1_000,
            policy=policy,
            observation_date=date(2026, 1, 5),
        )

        self.assertEqual(updated, self.holdings)
        self.assertEqual(execution["status"], "unfilled")
        self.assertIn("AAA:quote_too_stale", execution["unfilled_reasons"])

    def test_interest_buffer_covers_execution_and_settlement_delay(self) -> None:
        self.assertEqual(
            projected_interest_buffer(Loan(10_000), 0.365, 2),
            20,
        )

    def test_official_scenarios_use_full_limit_and_market_policy_rates(self) -> None:
        scenarios = official_portfolio_scenarios()
        self.assertTrue(
            all(item.initial_draw_assumption == 1 for item in scenarios.values())
        )
        self.assertEqual(
            scenarios["ngx_banking_heavy_portfolio"].loan_terms.annual_interest_rate,
            0.48,
        )
        self.assertEqual(
            scenarios["ngx_banking_heavy_portfolio"].loan_terms.accrual_frequency,
            "monthly",
        )
        self.assertEqual(
            scenarios["ngx_banking_heavy_portfolio"].benchmark_flat_ltv,
            0.30,
        )
        self.assertEqual(
            scenarios["us_diversified_etf_portfolio"].benchmark_flat_ltv,
            0.50,
        )

    def test_policy_replay_draws_full_limit_and_executes_recovery(self) -> None:
        bars = {
            "AAA": [
                HistoricalBar(
                    "AAA",
                    date(2026, 1, day),
                    price,
                    price,
                    price,
                    price,
                    volume=1_000_000,
                    currency="USD",
                    provider_name="fixture",
                )
                for day, price in enumerate([100, 100, 80, 80, 80, 80], start=1)
            ]
        }
        scenario = OfficialPortfolioScenario(
            "full_limit_recovery",
            [Holding("AAA", AssetType.LISTED_EQUITY, 100, "USD", "NYSE")],
            "USD",
            loan_terms=InterestPolicy(0.10),
            execution_policy=LiquidationExecutionPolicy(
                settlement_delay_observations=1
            ),
        )

        result = HistoricalReplayEngine(seed=7).replay(
            scenario,
            bars,
            comparison_regime=POLICY_ORIGINATION,
        )

        initial = result["initial_credit_snapshot"]
        self.assertEqual(initial["credit_limit_utilization"], 1)
        self.assertAlmostEqual(initial["approved_ltv"], initial["draw_ltv"])
        self.assertIn("flat_ltv_30", result["baseline_results"])
        self.assertIn("flat_ltv_50", result["baseline_results"])
        forced = [
            episode
            for episode in result["liquidation_episodes"]
            if episode["action"] == "full_recovery"
        ]
        self.assertEqual(len(forced), 1)
        self.assertEqual(forced[0]["status"], "fully_recovered")
        self.assertEqual(forced[0]["residual_obligation"], 0)
        self.assertTrue(forced[0]["advisory"]["orders"])
        self.assertIn(
            "minimum_limit_price", forced[0]["advisory"]["orders"][0]
        )
        self.assertEqual(result["records"][-1]["total_obligation"], 0)

    def test_stress_is_applied_after_unstressed_origination(self) -> None:
        bars = {
            "AAA": [
                HistoricalBar(
                    "AAA",
                    date(2026, 2, day),
                    100,
                    100,
                    100,
                    100,
                    volume=1_000_000,
                    currency="USD",
                    provider_name="fixture",
                )
                for day in range(1, 6)
            ]
        }
        scenario = OfficialPortfolioScenario(
            "post_origination_shock",
            [Holding("AAA", AssetType.LISTED_EQUITY, 100, "USD", "NYSE")],
            "USD",
        )
        result = HistoricalReplayEngine(seed=7).replay(
            scenario,
            bars,
            stress=StressOverlay(price_gap=0.50),
            comparison_regime=POLICY_ORIGINATION,
        )
        self.assertEqual(result["records"][0]["market_value"], 10_000)
        self.assertEqual(result["records"][1]["market_value"], 5_000)
        self.assertTrue(result["liquidation_episodes"])


if __name__ == "__main__":
    unittest.main()
