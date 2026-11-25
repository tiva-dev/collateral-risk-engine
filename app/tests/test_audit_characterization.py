"""Characterization tests for defects found in the November 2026 audit.

Each test states the behaviour the engine *should* have. Tests for known
defects were marked ``xfail(strict=True)`` until the fix landed (ADR-004).
The C3, H6 and margin-call sizing fixes removed their markers in
fix/financial-correctness; see docs/governance/DECISIONS.md ADR-007.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.enums import AssetType, MarginState
from app.core.evaluator import CollateralRiskEngine
from app.core.models import Holding, Loan, MarketData, Policy


def _market(**overrides) -> MarketData:
    base = {
        "asset_id": "X",
        "last_price": 100.0,
        "bid": 99.95,
        "ask": 100.05,
        "average_daily_volume": 5_000_000,
        "average_dollar_volume": 500_000_000,
        "volatility_30d": 0.30,
        "volatility_90d": 0.30,
        "data_quality_score": 1.0,
    }
    base.update(overrides)
    return MarketData(**base)


def _evaluate(market_data, principal: float, quantity: float = 1000.0):
    return CollateralRiskEngine().evaluate(
        "audit",
        [Holding("X", AssetType.LISTED_EQUITY, quantity, "USD", "US")],
        Loan(principal=principal),
        Policy.default(),
        market_data,
    )


def test_missing_market_data_is_not_treated_as_insolvency() -> None:
    result = _evaluate({}, principal=30_000.0)
    assert result.margin_state != MarginState.LIQUIDATION


def test_halted_collateral_is_not_treated_as_insolvency() -> None:
    result = _evaluate({"X": _market(halted=True)}, principal=30_000.0)
    assert result.margin_state != MarginState.LIQUIDATION


def test_low_data_quality_blocks_new_lending() -> None:
    """Fail-closed for new credit is intended behaviour and must stay."""
    result = _evaluate({"X": _market(data_quality_score=0.30)}, principal=0.0)
    assert result.approved_credit_limit == 0.0


def test_executing_margin_call_plan_leaves_margin_call() -> None:
    engine_market = {"X": _market()}
    result = None
    for principal in range(40_000, 95_000, 50):
        result = _evaluate(engine_market, float(principal))
        if result.margin_state == MarginState.MARGIN_CALL:
            break
    assert result is not None and result.margin_state == MarginState.MARGIN_CALL
    plan = result.liquidation_plan
    assert plan is not None and plan.orders
    sold = sum(order.quantity for order in plan.orders)
    after = _evaluate(
        engine_market,
        principal=result.loan_balance - plan.estimated_total_recovery,
        quantity=1000.0 - sold,
    )
    assert after.margin_state not in {MarginState.MARGIN_CALL, MarginState.LIQUIDATION}


def _monitoring_service():
    pytest.importorskip("fastapi")
    from app.lifecycle.service import CreditLifecycleEngine
    from app.monitoring.market_updates import InMemoryMarketDataCache
    from app.monitoring.repositories import (
        InMemoryMonitoredAccountRepository,
        InMemoryMonitoringEventRepository,
    )
    from app.monitoring.service import MonitoringService

    return MonitoringService(
        InMemoryMonitoredAccountRepository(),
        InMemoryMonitoringEventRepository(),
        CreditLifecycleEngine(CollateralRiskEngine(audit_logger=None), audit_logger=None),
        market_data_cache=InMemoryMarketDataCache(),
        audit_logger=None,
    )


def test_frequent_monitoring_ticks_preserve_interest() -> None:
    from datetime import UTC, datetime

    from app.core.enums import DataMode
    from app.credit.interest import InterestPolicy
    from app.market_data.identity import InstrumentIdentity
    from app.market_data.policy import MarketDataPolicy
    from app.market_data.providers import RawQuote

    service = _monitoring_service()
    instrument = InstrumentIdentity("SPY", "SPY", "NYSE", "USD", AssetType.ETF)
    quote = RawQuote(
        instrument, 100.0, 99.9, 100.1, 1_000_000, 100_000_000, 0.15, 0.15,
        timestamp=datetime.now(UTC), provider_name="test", data_quality_score=1.0,
        warnings=[],
    )
    service.register_account(
        account_ref="interest",
        holdings=[Holding("SPY", AssetType.ETF, 1000.0, "USD")],
        pledged_cash_balance=0.0,
        loan=Loan(10_000.0, currency="USD"),
        loan_currency="USD",
        policy=Policy.default(),
        interest_policy=InterestPolicy(annual_interest_rate=0.10),
        data_mode=DataMode.CLIENT_SUPPLIED,
        market_data_policy=MarketDataPolicy(),
        client_supplied_quotes={"SPY": quote},
    )
    ticks = 120  # one hour of 30-second margin-call cadence
    for _ in range(ticks):
        account = service.account_repo.get("interest")
        account.last_interest_accrual_at = account.last_interest_accrual_at - timedelta(
            seconds=30
        )
        service.account_repo.update(account)
        service.evaluate_account("interest", force=True)
    accrued = service.account_repo.get("interest").loan.accrued_interest
    expected = 10_000.0 * 0.10 * ticks * 30 / (365 * 86_400)
    assert accrued == pytest.approx(expected, abs=0.01)


def test_partially_unavailable_collateral_requires_review_not_sale() -> None:
    """One halted holding must not trigger a forced sale of the other."""
    holdings = [
        Holding("X", AssetType.LISTED_EQUITY, 1000.0, "USD", "US"),
        Holding("Y", AssetType.LISTED_EQUITY, 1000.0, "USD", "US"),
    ]
    market = {"X": _market(), "Y": _market(asset_id="Y", halted=True)}
    result = CollateralRiskEngine().evaluate(
        "audit", holdings, Loan(principal=80_000.0), Policy.default(), market
    )
    assert result.data_review_required
    assert result.data_unavailable_assets == ["Y"]
    assert result.margin_state == MarginState.RESTRICT_NEW_BORROWING
    assert result.liquidation_plan is None


def test_economic_breach_with_complete_data_still_liquidates() -> None:
    """The C3 rule must not suppress a breach that the data does show."""
    result = _evaluate({"X": _market()}, principal=99_000.0)
    assert not result.data_review_required
    assert result.margin_state == MarginState.LIQUIDATION
    assert result.liquidation_plan is not None


def test_two_asset_margin_call_plan_restores_coverage() -> None:
    holdings = [
        Holding("X", AssetType.LISTED_EQUITY, 1000.0, "USD", "US"),
        Holding("Y", AssetType.HIGH_VOLATILITY_EQUITY, 1000.0, "USD", "US"),
    ]
    market = {
        "X": _market(),
        "Y": _market(asset_id="Y", volatility_30d=0.8, volatility_90d=0.8,
                     average_dollar_volume=5_000_000),
    }
    engine = CollateralRiskEngine()
    result = None
    for principal in range(40_000, 190_000, 100):
        result = engine.evaluate(
            "audit", holdings, Loan(principal=float(principal)), Policy.default(), market
        )
        if result.margin_state == MarginState.MARGIN_CALL:
            break
    assert result is not None and result.margin_state == MarginState.MARGIN_CALL
    plan = result.liquidation_plan
    assert plan is not None and plan.plan_complete
    sold = {o.asset_id: o.quantity for o in plan.orders}
    after = engine.evaluate(
        "audit",
        [Holding(h.asset_id, h.asset_type, h.quantity - sold.get(h.asset_id, 0.0),
                 "USD", "US") for h in holdings if h.quantity - sold.get(h.asset_id, 0.0) > 0],
        Loan(principal=result.loan_balance - plan.estimated_total_recovery),
        Policy.default(),
        market,
    )
    assert after.margin_state not in {MarginState.MARGIN_CALL, MarginState.LIQUIDATION}
    # The restoring sale is larger than the repayment cure it replaces.
    assert plan.target_cash_recovery > result.trigger_levels.required_cure_amount
