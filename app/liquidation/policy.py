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
        for name in (
            "max_participation_rate",
            "execution_cost_rate",
            "maximum_price_slippage",
        ):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between zero and one")
