from __future__ import annotations

from dataclasses import dataclass

from app.core.evaluator import CollateralRiskEngine
from app.core.models import Holding, Loan, MarketData, Policy
from app.simulations.scenarios import SCENARIOS, apply_market_shock


@dataclass(frozen=True)
class PolicyComparisonResult:
    scenario: str
    flat_draw_limit: float
    dynamic_draw_limit: float
    credit_capacity_preserved: float
    stressed_liquidation_value: float
    flat_ltv_shortfall: bool
    dynamic_shortfall: bool
    flat_shortfall_amount: float
    dynamic_shortfall_amount: float
    dynamic_margin_state: str
    dynamic_recovery_coverage_ratio: float | None


def compare_flat_ltv_to_dynamic_engine(
    account_ref: str,
    holdings: list[Holding],
    loan: Loan,
    policy: Policy,
    market_data: dict[str, MarketData],
    flat_ltv: float = 0.70,
) -> list[PolicyComparisonResult]:
    """Replay stress scenarios and compare max-draw exposure under flat LTV vs dynamic engine.

    The comparison assumes a borrower draws the full limit available under each policy
    before the scenario shock occurs. This is the relevant policy-risk comparison:
    it tests how much debt each policy would have permitted before the market moved.
    """
    engine = CollateralRiskEngine(audit_logger=None)
    normal_eval = engine.evaluate(
        account_ref, holdings, Loan(principal=0), policy, market_data
    )
    flat_draw_limit = normal_eval.portfolio_market_value * flat_ltv
    recovery_limited_limit = normal_eval.stressed_liquidation_value / max(
        normal_eval.trigger_levels.dynamic_warning_coverage, 1e-9
    )
    dynamic_draw_limit = min(normal_eval.approved_credit_limit, recovery_limited_limit)
    credit_capacity_preserved = dynamic_draw_limit / max(flat_draw_limit, 1e-9)

    results: list[PolicyComparisonResult] = []
    for scenario_name, shock in SCENARIOS.items():
        shocked_data = {
            asset_id: apply_market_shock(snapshot, **shock)
            for asset_id, snapshot in market_data.items()
        }
        scenario_eval = engine.evaluate(
            account_ref,
            holdings,
            Loan(principal=dynamic_draw_limit),
            policy,
            shocked_data,
        )
        stressed_recovery = scenario_eval.stressed_liquidation_value
        flat_shortfall_amount = max(0.0, flat_draw_limit - stressed_recovery)
        dynamic_shortfall_amount = max(0.0, dynamic_draw_limit - stressed_recovery)
        results.append(
            PolicyComparisonResult(
                scenario=scenario_name,
                flat_draw_limit=round(flat_draw_limit, 2),
                dynamic_draw_limit=round(dynamic_draw_limit, 2),
                credit_capacity_preserved=round(credit_capacity_preserved, 4),
                stressed_liquidation_value=round(stressed_recovery, 2),
                flat_ltv_shortfall=flat_shortfall_amount > 0,
                dynamic_shortfall=dynamic_shortfall_amount > 0,
                flat_shortfall_amount=round(flat_shortfall_amount, 2),
                dynamic_shortfall_amount=round(dynamic_shortfall_amount, 2),
                dynamic_margin_state=scenario_eval.margin_state.value,
                dynamic_recovery_coverage_ratio=scenario_eval.recovery_coverage_ratio,
            )
        )
    return results
