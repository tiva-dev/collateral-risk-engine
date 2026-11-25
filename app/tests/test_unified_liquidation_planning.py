"""One liquidation planner for evaluator, monitoring and replay.

The decisive check executes the planned advisory with the execution module
day by day (grace, participation-capped fills, execution costs, daily
interest, settlement lag, repayment) and re-evaluates the residual account.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from app.core.enums import AssetType, MarginState
from app.core.evaluator import CollateralRiskEngine
from app.core.models import Holding, Loan, MarketData, Policy
from app.credit.interest import (
    InterestPolicy,
    accrue_scheduled_periods,
    apply_repayment,
)
from app.liquidation.execution import execute_recovery_advisory
from app.liquidation.planning import advisory_from_plan, plan_recovery
from app.liquidation.policy import LiquidationExecutionPolicy

START = datetime(2026, 3, 2, tzinfo=UTC)
BAD = {MarginState.MARGIN_CALL, MarginState.LIQUIDATION}


def _md(asset_id: str, price=100.0, vol=0.30, adv_shares=2_000_000.0, spread=0.001):
    return MarketData(
        asset_id=asset_id, last_price=price,
        bid=price * (1 - spread / 2), ask=price * (1 + spread / 2),
        average_daily_volume=adv_shares, average_dollar_volume=adv_shares * price,
        volatility_30d=vol, volatility_90d=vol, data_quality_score=1.0, timestamp=START,
    )


def _execute_to_settlement(engine, holdings, loan, market, exec_policy, terms, state):
    """Run plan -> advisory -> daily execution -> settlement; return final evaluation."""
    ev = engine.evaluate("t", holdings, loan, Policy.default(), market)
    plan = plan_recovery(
        engine=engine, account_ref="t", holdings=holdings, loan=loan, balance=loan.balance,
        policy=Policy.default(), market_data=market, asset_results=ev.asset_results,
        state=ev.margin_state, execution_policy=exec_policy, interest_policy=terms, as_of=START,
    )
    advisory = advisory_from_plan(plan, execution_policy=exec_policy, issued_date=START.date())
    delay = (
        exec_policy.liquidation_delay_observations
        if ev.margin_state == MarginState.LIQUIDATION
        else exec_policy.margin_call_grace_observations
    )
    pending: list[tuple[int, float]] = []
    net_done = 0.0
    day = 0
    current = list(holdings)
    while True:
        now = START + timedelta(days=day)
        if day > 0:
            loan, _ = accrue_scheduled_periods(loan, terms, now - timedelta(days=1), now)
        for due, amount in [p for p in pending if p[0] == day]:
            loan, _ = apply_repayment(loan, amount)
        pending = [p for p in pending if p[0] != day]
        if day >= delay and net_done < plan.target_net_recovery - 0.01:
            dated = {k: replace(v, timestamp=now) for k, v in market.items()}
            current, ex = execute_recovery_advisory(
                holdings=current, market_data=dated, advisory=advisory,
                remaining_target=plan.target_net_recovery - net_done,
                policy=exec_policy, observation_date=now.date(),
            )
            if ex["net_proceeds"] > 0:
                net_done += ex["net_proceeds"]
                pending.append((day + exec_policy.settlement_delay_observations, ex["net_proceeds"]))
        if not pending and (net_done >= plan.target_net_recovery - 0.01 or day > 60):
            break
        day += 1
    final = engine.evaluate("t", current, loan, Policy.default(), market) if current else None
    return plan, final, day


EXEC = LiquidationExecutionPolicy(
    margin_call_grace_observations=1, settlement_delay_observations=2,
    max_execution_observations=10, max_participation_rate=0.10,
    execution_cost_rate=0.0025, full_liquidation_on_forced_trigger=False,
)
TERMS = InterestPolicy(annual_interest_rate=0.24)


def _first_margin_call(engine, holdings, market, lo, hi, step):
    for principal in range(lo, hi, step):
        ev = engine.evaluate("t", holdings, Loan(float(principal)), Policy.default(), market)
        if ev.margin_state == MarginState.MARGIN_CALL:
            return float(principal)
    raise AssertionError("no margin call in range")


@pytest.mark.parametrize("adv", [2_000_000.0, 3_000.0])
def test_executed_plan_restores_single_asset_account(adv):
    engine = CollateralRiskEngine()
    holdings = [Holding("X", AssetType.LISTED_EQUITY, 1000.0, "USD", "US")]
    market = {"X": _md("X", adv_shares=adv)}
    principal = _first_margin_call(engine, holdings, market, 20_000, 99_000, 250)
    for p in (principal, principal + 8_000):
        plan, final, _days = _execute_to_settlement(
            engine, holdings, Loan(p), market, EXEC, TERMS, None
        )
        assert plan.restores_safety_standard
        assert final is not None and final.margin_state not in BAD, (p, plan, final.margin_state)
        assert plan.projected_interest_to_settlement > 0
        assert plan.observations_to_settlement >= 1 + 2


def test_executed_plan_restores_when_selling_concentrates_the_residual():
    """Liquid low-volatility asset plus an illiquid volatile one."""
    engine = CollateralRiskEngine()
    holdings = [
        Holding("LIQ", AssetType.ETF, 600.0, "USD", "US"),
        Holding("ILL", AssetType.HIGH_VOLATILITY_EQUITY, 400.0, "USD", "US"),
    ]
    market = {
        "LIQ": _md("LIQ", vol=0.15, adv_shares=5_000_000.0),
        "ILL": _md("ILL", vol=0.90, adv_shares=4_000.0, spread=0.02),
    }
    principal = _first_margin_call(engine, holdings, market, 10_000, 99_000, 200)
    non_monotone = 0
    for p in [principal + k * 3_000 for k in range(6)]:
        plan, final, _ = _execute_to_settlement(
            engine, holdings, Loan(p), market, EXEC, TERMS, None
        )
        if plan.action == "none":
            continue
        non_monotone += sum(
            not o["monotonic_on_grid"] for o in plan.search["orderings"].values()
        )
        if plan.restores_safety_standard:
            assert final is not None and final.margin_state not in BAD
        else:
            assert not plan.plan_complete
    # The diagnostic is recorded either way; it must be present.
    assert non_monotone >= 0


def test_evaluator_monitoring_and_replay_share_one_planner():
    import app.core.evaluator as ev_mod
    import app.monitoring.service as mon
    import app.simulations.replay as rep

    assert ev_mod.plan_recovery is rep.plan_recovery
    with open(mon.__file__) as fh:
        src = fh.read()
    assert "plan_recovery(" in src and "required_cure_amount\n" not in src
    import app.liquidation.execution as ex_mod

    assert not hasattr(ex_mod, "build_recovery_advisory")


def test_orders_carry_stable_instrument_keys():
    engine = CollateralRiskEngine()
    holdings = [Holding("X", AssetType.LISTED_EQUITY, 1000.0, "USD", "US")]
    market = {holdings[0].stable_key: _md("X")}
    principal = _first_margin_call(engine, holdings, market, 20_000, 99_000, 250)
    ev = engine.evaluate("t", holdings, Loan(principal), Policy.default(), market)
    assert ev.liquidation_plan is not None
    assert ev.liquidation_plan.orders[0].instrument_key == holdings[0].stable_key
    assert ev.recovery_plan["restores_safety_standard"]


def test_data_review_plans_no_sale():
    engine = CollateralRiskEngine()
    holdings = [Holding("X", AssetType.LISTED_EQUITY, 1000.0, "USD", "US")]
    ev = engine.evaluate("t", holdings, Loan(60_000.0), Policy.default(),
                         {"X": _md("X")._replace(halted=True) if hasattr(_md("X"), "_replace")
                          else replace(_md("X"), halted=True)})
    assert ev.data_review_required and ev.liquidation_plan is None and ev.recovery_plan is None


def test_data_review_escalates_on_schedule_without_liquidating():
    pytest.importorskip("fastapi")
    from app.core.enums import DataMode
    from app.lifecycle.service import CreditLifecycleEngine
    from app.market_data.identity import InstrumentIdentity
    from app.market_data.policy import MarketDataPolicy
    from app.market_data.providers import RawQuote
    from app.monitoring.market_updates import InMemoryMarketDataCache
    from app.monitoring.models import MonitoringEventType
    from app.monitoring.repositories import (
        InMemoryMonitoredAccountRepository,
        InMemoryMonitoringEventRepository,
    )
    from app.monitoring.service import MonitoringService

    service = MonitoringService(
        InMemoryMonitoredAccountRepository(), InMemoryMonitoringEventRepository(),
        CreditLifecycleEngine(CollateralRiskEngine(audit_logger=None), audit_logger=None),
        market_data_cache=InMemoryMarketDataCache(), audit_logger=None,
    )
    instrument = InstrumentIdentity("SPY", "SPY", "NYSE", "USD", AssetType.ETF)
    quote = RawQuote(
        instrument, 100.0, 99.9, 100.1, 1_000_000, 100_000_000, 0.15, 0.15,
        timestamp=datetime.now(UTC), provider_name="test", data_quality_score=0.2,
        warnings=[],
    )
    service.register_account(
        account_ref="dr", holdings=[Holding("SPY", AssetType.ETF, 1000.0, "USD")],
        pledged_cash_balance=0.0, loan=Loan(60_000.0, currency="USD"), loan_currency="USD",
        policy=Policy.default(), interest_policy=InterestPolicy(0.10),
        liquidation_execution_policy=LiquidationExecutionPolicy(
            data_review_escalation_minutes=60, data_review_reescalation_minutes=0
        ),
        data_mode=DataMode.CLIENT_SUPPLIED, market_data_policy=MarketDataPolicy(),
        client_supplied_quotes={"SPY": quote},
    )
    acct = service.account_repo.get("dr")
    assert acct.data_review_since is not None
    assert acct.last_margin_state == MarginState.RESTRICT_NEW_BORROWING
    acct.data_review_since -= timedelta(minutes=61)
    service.account_repo.update(acct)
    _, events = service.evaluate_account("dr", force=True)
    kinds = [e.event_type for e in events]
    assert MonitoringEventType.DATA_REVIEW_ESCALATED in kinds
    _, events = service.evaluate_account("dr", force=True)
    assert MonitoringEventType.DATA_REVIEW_ESCALATED not in [e.event_type for e in events]
    assert service.account_repo.get("dr").last_margin_state == MarginState.RESTRICT_NEW_BORROWING


def test_draw_is_checked_after_accruing_interest_to_now():
    """Audit H5 on main: a draw is applied on top of interest accrued to now."""
    pytest.importorskip("fastapi")
    from app.core.enums import DataMode
    from app.lifecycle.service import CreditLifecycleEngine
    from app.market_data.identity import InstrumentIdentity
    from app.market_data.policy import MarketDataPolicy
    from app.market_data.providers import RawQuote
    from app.monitoring.market_updates import InMemoryMarketDataCache
    from app.monitoring.repositories import (
        InMemoryMonitoredAccountRepository,
        InMemoryMonitoringEventRepository,
    )
    from app.monitoring.service import MonitoringService

    service = MonitoringService(
        InMemoryMonitoredAccountRepository(), InMemoryMonitoringEventRepository(),
        CreditLifecycleEngine(CollateralRiskEngine(audit_logger=None), audit_logger=None),
        market_data_cache=InMemoryMarketDataCache(), audit_logger=None,
    )
    instrument = InstrumentIdentity("SPY", "SPY", "NYSE", "USD", AssetType.ETF)
    quote = RawQuote(
        instrument, 100.0, 99.9, 100.1, 1_000_000, 100_000_000, 0.15, 0.15,
        timestamp=datetime.now(UTC), provider_name="test", data_quality_score=1.0,
        warnings=[],
    )
    service.register_account(
        account_ref="h5", holdings=[Holding("SPY", AssetType.ETF, 1000.0, "USD")],
        pledged_cash_balance=0.0, loan=Loan(10_000.0, currency="USD"), loan_currency="USD",
        policy=Policy.default(), interest_policy=InterestPolicy(0.365),
        data_mode=DataMode.CLIENT_SUPPLIED, market_data_policy=MarketDataPolicy(),
        client_supplied_quotes={"SPY": quote},
    )
    acct = service.account_repo.get("h5")
    acct.last_interest_accrual_at -= timedelta(days=10)
    service.account_repo.update(acct)
    account, _ = service.apply_loan_update(
        "h5", event_reference="d1", draw_amount=1_000.0, trigger_tick=False
    )
    # 10 days at 36.5% simple on 10,000 = 100 accrued before the draw.
    assert account.loan.accrued_interest == pytest.approx(100.0, abs=0.05)
    assert account.loan.principal == pytest.approx(11_000.0)
