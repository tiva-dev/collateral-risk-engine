"""Candidate engine parameters.

Confidence levels and horizons are risk-appetite choices. The values here are
research defaults for shadow evaluation and are NOT approved by Tiva; each is
marked as such in every explanation the engine produces.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

VERSION = "candidate-2026.11.1"


@dataclass(frozen=True)
class CandidateConfig:
    # Margin call: the largest balance at which waiting the grace period and
    # then selling still covers the obligation at this confidence (ES level).
    margin_call_confidence: float = 0.99
    # Forced recovery: the largest balance at which an immediate sale still
    # covers the obligation at this confidence. Above it there is no grace.
    forced_confidence: float = 0.95
    # Initial capacity and restoration target: the balance from which the
    # probability of reaching the margin-call level within the headroom
    # horizon is at most 1 - headroom_confidence.
    headroom_confidence: float = 0.95
    headroom_sessions: int = 5
    # Early warning: a one-session move at this confidence would reach the
    # margin-call level.
    warning_confidence: float = 0.90
    warning_sessions: int = 1
    # Restoration target after a margin call or forced recovery:
    # "initial_capacity" (default) or "early_warning".
    restoration: str = "initial_capacity"
    # Data and uncertainty safeguards.
    min_sessions: int = 20          # below this an instrument has no lending value
    full_tail_sessions: int = 250   # residual count at which the empirical tail is fully trusted
    min_data_quality: float = 0.5
    uncertainty_z: float = 1.0      # one-sided credible bound on log volatility
    multi_session_lag: int = 10     # variance-ratio horizon for stale-price correction
    max_exit_sessions: int = 60     # lending value only for what can be sold in this many sessions
    # Execution cost model.
    impact_coefficient: float = 0.7
    spread_stress_multiplier: float = 1.5
    # Simulation.
    n_paths: int = 2000
    history: int = 500
    ewma_lambda: float = 0.94

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["status"] = "research default, not approved by Tiva"
        return out
