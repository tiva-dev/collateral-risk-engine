"""Approved correctness fixes of 26 Nov 2026.

1. Interest accrual is separated from capitalization; compound interest is
   capitalized only at contractual boundaries, so monitoring frequency cannot
   change contractual balances.
2. Forced-liquidation interest reserve covers delay, the full execution window
   and settlement, counted in calendar days.
3. The risk model's liquidity horizon uses the participation rate execution is
   allowed to use.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from app.core.models import Loan
from app.credit.interest import (
    InterestPolicy,
    accrue_scheduled_periods,
    next_capitalization_boundary,
)
from app.liquidation.planning import calendar_days

START = datetime(2026, 1, 1, tzinfo=UTC)
END = datetime(2026, 9, 14, 17, tzinfo=UTC)


def _split(loan: Loan, terms: InterestPolicy, cuts: list[datetime]) -> Loan:
    cur = START
    for c in [*cuts, END]:
        loan, _ = accrue_scheduled_periods(loan, terms, cur, c)
        cur = c
    return loan


@pytest.mark.parametrize("freq", ["daily", "monthly", "quarterly", "yearly"])
@pytest.mark.parametrize("comp", ["simple", "compound"])
@pytest.mark.parametrize("anchor", [None, datetime(2025, 11, 30, 10, tzinfo=UTC)])
def test_monitoring_frequency_cannot_change_contractual_balance(freq, comp, anchor) -> None:
    terms = InterestPolicy(annual_interest_rate=0.30, accrual_frequency=freq, compounding=comp,
                           schedule_anchor=anchor)
    one, _ = accrue_scheduled_periods(Loan(250_000.0), terms, START, END)
    rng = random.Random(7)
    for _ in range(5):
        span = (END - START).total_seconds()
        cuts = sorted(START + timedelta(seconds=rng.uniform(0, span)) for _ in range(rng.randint(5, 400)))
        split = _split(Loan(250_000.0), terms, cuts)
        assert split.balance == pytest.approx(one.balance, abs=1e-4)
        assert split.principal == pytest.approx(one.principal, abs=1e-4)


def test_accrual_is_not_capitalized_between_boundaries() -> None:
    terms = InterestPolicy(annual_interest_rate=0.365, accrual_frequency="monthly", compounding="compound",
                           schedule_anchor=START)
    mid, _ = accrue_scheduled_periods(Loan(100_000.0), terms, START, START + timedelta(days=10))
    assert mid.principal == 100_000.0
    assert mid.accrued_interest == pytest.approx(1_000.0)
    after, res = accrue_scheduled_periods(Loan(100_000.0), terms, START, datetime(2026, 2, 1, 1, tzinfo=UTC))
    assert res.capitalizations == 1  # one capitalization on 1 Feb
    assert after.principal == pytest.approx(100_000.0 * (1 + 0.365 * 31 / 365))


def test_anchored_schedule_follows_the_contract_not_the_calendar() -> None:
    terms = InterestPolicy(annual_interest_rate=0.1, accrual_frequency="monthly", compounding="compound",
                           schedule_anchor=datetime(2026, 1, 31, tzinfo=UTC))
    assert next_capitalization_boundary(terms, datetime(2026, 2, 10, tzinfo=UTC)) == datetime(2026, 2, 28, tzinfo=UTC)
    assert next_capitalization_boundary(terms, datetime(2026, 2, 28, tzinfo=UTC)) == datetime(2026, 3, 31, tzinfo=UTC)


def test_sessions_convert_to_calendar_days_for_interest() -> None:
    assert [calendar_days(n) for n in (0, 1, 4, 5, 6, 10, 12)] == [0, 3, 6, 7, 10, 14, 18]


def test_forced_reserve_includes_execution_window() -> None:
    from app.core.enums import AssetType, MarginState
    from app.core.evaluator import CollateralRiskEngine
    from app.core.models import Holding, MarketData, Policy
    from app.liquidation.planning import plan_recovery
    from app.liquidation.policy import LiquidationExecutionPolicy

    engine = CollateralRiskEngine(audit_logger=None)
    holdings = [Holding("XYZ", AssetType.LISTED_EQUITY, 100_000.0, "USD")]
    md = {"XYZ": MarketData("XYZ", 10.0, bid=9.99, ask=10.01, average_daily_volume=50_000.0,
                            average_dollar_volume=500_000.0, volatility_30d=0.6, volatility_90d=0.6)}
    policy = Policy.default()
    loan = Loan(900_000.0)
    terms = InterestPolicy(annual_interest_rate=0.5)
    ev = engine.evaluate("f", holdings, loan, policy, md, _size_plan=False)
    exec_policy = LiquidationExecutionPolicy(full_liquidation_on_forced_trigger=True, max_execution_observations=40)
    plan = plan_recovery(engine=engine, account_ref="f", holdings=holdings, loan=loan, balance=loan.balance,
                         policy=policy, market_data=md, asset_results=ev.asset_results,
                         state=MarginState.LIQUIDATION, execution_policy=exec_policy,
                         interest_policy=terms, as_of=START)
    # ~92k shares at 10% of 50k ADV = 19 sessions; reserve covers them.
    assert plan.expected_execution_observations >= 18
    days = calendar_days(exec_policy.liquidation_delay_observations + plan.expected_execution_observations
                         + exec_policy.settlement_delay_observations)
    expected_interest = 900_000.0 * 0.5 * days / 365
    assert plan.target_net_recovery == pytest.approx(900_000.0 + expected_interest, rel=1e-3)


def test_risk_horizon_uses_execution_participation() -> None:
    from app.core.enums import AssetType
    from app.core.evaluator import CollateralRiskEngine
    from app.core.models import Holding, MarketData, Policy
    from app.liquidation.policy import LiquidationExecutionPolicy

    engine = CollateralRiskEngine(audit_logger=None)
    holdings = [Holding("XYZ", AssetType.LISTED_EQUITY, 100_000.0, "USD")]
    md = {"XYZ": MarketData("XYZ", 10.0, bid=9.99, ask=10.01, average_daily_volume=50_000.0,
                            average_dollar_volume=500_000.0, volatility_30d=0.4, volatility_90d=0.4)}
    policy = Policy.default()
    loose = engine.evaluate("p", holdings, Loan(1.0), policy, md, _size_plan=False,
                            execution_policy=LiquidationExecutionPolicy(max_participation_rate=0.10))
    tight = engine.evaluate("p", holdings, Loan(1.0), policy, md, _size_plan=False,
                            execution_policy=LiquidationExecutionPolicy(max_participation_rate=0.02))
    a, b = loose.asset_results[0], tight.asset_results[0]
    assert b.participation_rate == pytest.approx(0.02)
    assert b.liquidation_horizon_days > a.liquidation_horizon_days
    assert b.effective_ltv <= a.effective_ltv
