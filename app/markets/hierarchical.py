"""Hierarchical (empirical-Bayes) estimation of asset risk parameters.

For an instrument with a short history, its own estimate is noisy; for a new
market, there may be no long-history peers at all. Partial pooling shrinks
each instrument's estimate toward a group prior, in proportion to how noisy
the instrument's own estimate is:

    theta_i = w_i * x_i + (1 - w_i) * (mu_group + delta_market)
    w_i     = tau^2 / (tau^2 + v_i),   v_i = sampling variance of x_i

Log volatility is pooled (v_i ~= 1 / (2 (n_i - 1)) for a log sample s.d.);
mu_group and tau^2 come from instruments with full histories, possibly in
other markets; delta_market is a random effect estimated from the target
market's own cross-section and shrunk by its precision. The same formulas
apply to every market; only the data differ.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Prior:
    mu_log_vol: float   # mean log daily volatility of the reference group
    tau2: float         # between-instrument variance of log volatility
    n_instruments: int
    source: str         # e.g. "US full histories", "NGX banks"


def log_sd_sampling_variance(n_obs: int) -> float:
    return 1.0 / (2.0 * max(n_obs - 1, 1))


def prior_from_full_histories(log_vols: list[float], source: str) -> Prior:
    if len(log_vols) < 2:
        raise ValueError("a prior needs at least two instruments")
    mu = sum(log_vols) / len(log_vols)
    var = sum((x - mu) ** 2 for x in log_vols) / (len(log_vols) - 1)
    return Prior(mu, max(var, 1e-4), len(log_vols), source)


def market_effect(prior: Prior, target_log_vols: list[float], n_obs: int) -> float:
    """Shrunk mean deviation of the target market's cross-section from the prior."""
    if not target_log_vols:
        return 0.0
    v = log_sd_sampling_variance(n_obs)
    dev = sum(target_log_vols) / len(target_log_vols) - prior.mu_log_vol
    precision_data = len(target_log_vols) / (prior.tau2 + v)
    precision_prior = 1.0 / prior.tau2
    return dev * precision_data / (precision_data + precision_prior)


def shrink_log_vol(own_log_vol: float, n_obs: int, prior: Prior, delta_market: float = 0.0) -> tuple[float, float]:
    """Return (posterior log daily vol, weight on the instrument's own estimate)."""
    v = log_sd_sampling_variance(n_obs)
    w = prior.tau2 / (prior.tau2 + v)
    return w * own_log_vol + (1 - w) * (prior.mu_log_vol + delta_market), w


def annualised(log_daily_vol: float) -> float:
    return math.exp(log_daily_vol) * math.sqrt(252.0)
