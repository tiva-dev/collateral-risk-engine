"""Characterization tests for defects found in the November 2026 audit.

Each test states the behaviour the engine *should* have. Tests for known
defects are marked ``xfail(strict=True)``: they currently fail, which pytest
reports as an expected failure. When a reviewed methodology fix lands, the
test passes, strict mode turns that into a failure, and the marker must be
removed deliberately (ADR-004). These tests do not change engine behaviour.
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


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="Audit C3: missing data sets the approved limit to 0, which maps to LIQUIDATION",
)
def test_missing_market_data_is_not_treated_as_insolvency() -> None:
    result = _evaluate({}, principal=30_000.0)
    assert result.margin_state != MarginState.LIQUIDATION


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="Audit C3: a trading halt maps a 30% LTV loan to LIQUIDATION",
)
def test_halted_collateral_is_not_treated_as_insolvency() -> None:
    result = _evaluate({"X": _market(halted=True)}, principal=30_000.0)
    assert result.margin_state != MarginState.LIQUIDATION


def test_low_data_quality_blocks_new_lending() -> None:
    """Fail-closed for new credit is intended behaviour and must stay."""
    result = _evaluate({"X": _market(data_quality_score=0.30)}, principal=0.0)
    assert result.approved_credit_limit == 0.0


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason=(
        "Audit/challenger finding: margin-call liquidation plans are sized with "
        "cash-cure or limit-breach amounts, which ignore that selling collateral "
        "also reduces collateral value"
    ),
)
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


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="Audit H6: each monitoring tick rounds accrued interest to 2 dp, so small increments vanish",
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
