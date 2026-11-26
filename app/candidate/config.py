"""Candidate engine parameters.

Confidence levels and horizons are risk-appetite choices. The values here are
research defaults for shadow evaluation and are NOT approved by Tiva; each is
marked as such in every explanation the engine produces.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

VERSION = "candidate-2026.11.2"


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
    # Stressed liquidity in the engine's own scenarios (assumptions, labelled).
    liquidity_noise_sd: float = 0.35          # session-to-session volume variation (log sd)
    liquidity_persistence: float = 0.7        # AR(1) coefficient of log volume
    volume_drawdown_elasticity: float = 2.0   # volume falls by this x the portfolio drawdown
    min_volume_multiplier: float = 0.05
    no_fill_share_of_stale: float = 0.5       # no-fill probability = this x observed stale-price share
    suspension_floor: float = 0.001           # minimum per-session suspension probability
    suspension_mean_sessions: float = 3.0
    # Exchange price limits from the market profile, used as an execution
    # constraint in scenarios (limit-down sessions do not fill; the excess move
    # carries to the next session). Candidate only; not approved methodology.
    price_limit_scenarios: bool = True
    # Data quality: a holding is material at or above this share of securities value.
    material_share: float = 0.02
    max_quote_lag_business_days: int = 1
    max_bar_lag_business_days: int = 2
    # Simulation.
    n_paths: int = 2000
    history: int = 500
    ewma_lambda: float = 0.94

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["status"] = "research default, not approved by Tiva"
        return out
