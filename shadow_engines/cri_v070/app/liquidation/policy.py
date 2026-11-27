from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LiquidationExecutionPolicy:
    """Client-controlled warning, execution, settlement, and cost assumptions."""

    margin_call_grace_observations: int = 1
    liquidation_delay_observations: int = 0
    settlement_delay_observations: int = 1
    max_execution_observations: int = 5
    max_participation_rate: float = 0.10
    execution_cost_rate: float = 0.0025
    maximum_price_slippage: float = 0.10
    maximum_quote_age_days: int = 3
    full_liquidation_on_forced_trigger: bool = True
    # Audit C3 follow-up. While collateral data is unavailable the account is
    # held in data review (no automated sale). After this many minutes the
    # review is escalated to a person; it is re-escalated at the second
    # interval (0 disables repeats). Escalation never forces a sale.
    # Defaults are placeholders pending Tiva's risk-policy decision.
    data_review_escalation_minutes: float = 1440.0
    data_review_reescalation_minutes: float = 1440.0

    def __post_init__(self) -> None:
        for name in (
            "margin_call_grace_observations",
            "liquidation_delay_observations",
            "settlement_delay_observations",
            "max_execution_observations",
            "maximum_quote_age_days",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be greater than or equal to zero")
        for name in ("data_review_escalation_minutes", "data_review_reescalation_minutes"):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be greater than or equal to zero")
        for name in (
            "max_participation_rate",
            "execution_cost_rate",
            "maximum_price_slippage",
        ):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between zero and one")
