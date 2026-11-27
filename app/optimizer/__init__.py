"""CRI Optimizer: decision optimisation around the frozen Core engine.

Reads Core outputs (loss distributions, capacities); never edits Core
parameters. Institutional limits and contractual safeguards stay separate.
"""

from app.optimizer.allocation import Opportunity, allocate
from app.optimizer.frontier import (
    PRESETS,
    LendingEconomics,
    RiskAppetite,
    capacity_for_appetite,
    economics,
    frontier,
    shortfall_metrics,
)

__all__ = ["PRESETS", "LendingEconomics", "Opportunity", "RiskAppetite", "allocate", "capacity_for_appetite",
           "economics", "frontier", "shortfall_metrics"]
