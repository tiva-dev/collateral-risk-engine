"""Numerical probes from the November 2026 repository audit.

Each probe calls the operational engine in the checkout it is run from and
returns plain data. They change nothing. Run against any branch with:

    python -m research.audit_probes.probes            # prints JSON

The same functions are exercised by ``app/tests/test_audit_probes.py`` so the
probes stay runnable as the code evolves. Interpretation lives in the audit
and methodology documents, not here.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.enums import AssetType, MarginState
from app.core.evaluator import CollateralRiskEngine
from app.core.models import Holding, Loan, MarketData, Policy
from app.credit.interest import InterestPolicy, accrue_interest
from app.lifecycle.service import CreditLifecycleEngine


def _market(asset_id: str = "X", **overrides: Any) -> MarketData:
    base: dict[str, Any] = {
        "asset_id": asset_id,
        "last_price": 100.0,
        "bid": 99.95,
        "ask": 100.05,
        "average_daily_volume": 2_000_000,
        "average_dollar_volume": 200_000_000,
        "volatility_30d": 0.25,
        "volatility_90d": 0.25,
        "data_quality_score": 1.0,
    }
    base.update(overrides)
    return MarketData(**base)


def ltv_sensitivity() -> list[dict[str, Any]]:
    """Single listed equity, default policy: how each input moves effective LTV."""
    engine = CollateralRiskEngine()
    cases = {
        "baseline_vol25": _market(),
        "dq_0.40": _market(data_quality_score=0.40),
        "vol_60": _market(volatility_30d=0.6, volatility_90d=0.6),
        "volume_unknown": _market(average_daily_volume=None, average_dollar_volume=None),
        "spread_5pct": _market(bid=97.5, ask=102.5),
    }
    rows = []
    for label, market in cases.items():
        result = engine.evaluate(
            "probe",
            [Holding("X", AssetType.LISTED_EQUITY, 1000, "USD", "US")],
            Loan(principal=0.0),
            Policy.default(),
            {"X": market},
        )
        asset = result.asset_results[0]
        rows.append(
            {
                "case": label,
                "effective_ltv": asset.effective_ltv,
                "adjustment_product": round(asset.adjustments.product, 4),
                "slv_over_mv": round(
                    asset.stressed_liquidation_value / asset.market_value, 4
                ),
            }
        )
    return rows


def volatility_concentration_grid() -> list[dict[str, Any]]:
    """Approved and lifecycle-safe limit / MV for 1 vs 10 equal names."""
    engine = CollateralRiskEngine()
    lifecycle = CreditLifecycleEngine(risk_engine=engine)
    rows = []
    for vol in (0.15, 0.30, 0.60, 0.90, 1.20):
        for names in (1, 10):
            holdings = [
                Holding(f"X{i}", AssetType.LISTED_EQUITY, 1000 / names, "USD", "US")
                for i in range(names)
            ]
            market = {
                f"X{i}": _market(
                    f"X{i}",
                    volatility_30d=vol,
                    volatility_90d=vol,
                    bid=99.95,
                    ask=100.05,
                )
                for i in range(names)
            }
            result = engine.evaluate(
                "probe", holdings, Loan(principal=0.0), Policy.default(), market
            )
            mv = result.portfolio_market_value
            rows.append(
                {
                    "volatility": vol,
                    "names": names,
                    "approved_ltv": round(result.approved_credit_limit / mv, 4),
                    "safe_ltv": round(lifecycle._safe_credit_limit(result) / mv, 4),
                    "portfolio_risk_score": result.portfolio_risk_score,
                }
            )
    return rows


def fail_closed_states() -> dict[str, str]:
    """Margin state of a 30% LTV loan when market data is missing or unusable."""
    engine = CollateralRiskEngine()
    holdings = [Holding("X", AssetType.LISTED_EQUITY, 1000, "USD", "US")]
    loan = Loan(principal=30_000.0)
    cases = {
        "missing_market_data": {},
        "data_quality_0.30": {"X": _market(data_quality_score=0.30)},
        "halted": {"X": _market(halted=True)},
    }
    return {
        label: engine.evaluate(
            "probe", holdings, loan, Policy.default(), market
        ).margin_state.value
        for label, market in cases.items()
    }


def interest_tick_rounding(principal: float = 10_000.0) -> list[dict[str, Any]]:
    """Accrued interest after 30 days when each tick rounds to 2 dp.

    Reproduces the per-tick rounding in ``MonitoringService.evaluate_account``.
    """
    terms = InterestPolicy(annual_interest_rate=0.10)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    exact = principal * 0.10 * 30 / 365
    rows = []
    for minutes in (1, 5, 60, 1440):
        loan = Loan(principal=principal)
        current = start
        while current < start + timedelta(days=30):
            nxt = current + timedelta(minutes=minutes)
            accrued, _ = accrue_interest(loan, terms, current, nxt)
            loan = replace(
                accrued,
                principal=round(accrued.principal, 2),
                accrued_interest=round(accrued.accrued_interest, 2),
                fees=round(accrued.fees, 2),
            )
            current = nxt
        rows.append(
            {
                "tick_minutes": minutes,
                "accrued": loan.accrued_interest,
                "exact": round(exact, 2),
            }
        )
    return rows


def collateral_funded_cure() -> dict[str, Any]:
    """Execute the engine's own margin-call plan and re-evaluate."""
    engine = CollateralRiskEngine()
    policy = Policy.default()
    market = _market(volatility_30d=0.30, volatility_90d=0.30,
                     average_daily_volume=5e6, average_dollar_volume=5e8)
    quantity = 1000.0

    def evaluate(qty: float, principal: float):
        return engine.evaluate(
            "probe",
            [Holding("X", AssetType.LISTED_EQUITY, qty, "USD", "US")],
            Loan(principal=principal),
            policy,
            {"X": market},
        )

    result = None
    for principal in range(40_000, 95_000, 50):
        result = evaluate(quantity, float(principal))
        if result.margin_state == MarginState.MARGIN_CALL:
            break
    assert result is not None and result.liquidation_plan is not None
    plan = result.liquidation_plan
    sold = sum(order.quantity for order in plan.orders)
    after = evaluate(quantity - sold, result.loan_balance - plan.estimated_total_recovery)
    slv, obligation = result.stressed_liquidation_value, result.loan_balance
    target = result.trigger_levels.dynamic_margin_call_coverage
    return {
        "state_before": result.margin_state.value,
        "coverage_before": round(slv / obligation, 4),
        "margin_call_coverage": target,
        "repayment_only_cure": result.trigger_levels.repayment_only_cure,
        "plan_target_cash": plan.target_cash_recovery,
        "state_after_plan": after.margin_state.value,
        "coverage_after_plan": after.recovery_coverage_ratio,
        # Closed form with net proceeds equal to stressed value removed.
        "collateral_funded_sale_required": round(
            max(0.0, (target * obligation - slv) / (target - 1.0)), 2
        ),
    }


def run_all() -> dict[str, Any]:
    return {
        "ltv_sensitivity": ltv_sensitivity(),
        "volatility_concentration_grid": volatility_concentration_grid(),
        "fail_closed_states": fail_closed_states(),
        "interest_tick_rounding": interest_tick_rounding(),
        "collateral_funded_cure": collateral_funded_cure(),
    }


if __name__ == "__main__":
    print(json.dumps(run_all(), indent=2, default=str))
