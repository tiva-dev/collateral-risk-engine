"""Per-instrument estimation from stored daily bars, with uncertainty.

Every estimate uses only bars dated on or before the decision date and is
keyed by the instrument's stable identity (exchange, symbol, currency, type),
never by bare symbol.

Volatility is estimated with the same formulas in every market:

* EWMA (lambda 0.94) and long-run (up to 252 sessions) daily volatility,
  the larger of the two (anti-procyclical floor);
* multiplied by sqrt(VR(q)), the variance ratio of q-session returns, so that
  markets with stale prices and partial price adjustment are measured on a
  multi-session-consistent basis (VR = 1 for an efficient market);
* partially pooled toward a group prior of instruments with full histories
  (empirical Bayes on log volatility), with the instrument's own weight set by
  its effective number of observations;
* raised to a one-sided credible bound using the posterior variance of log
  volatility, so short or poor histories carry an explicit margin.

The tail of the standardised residuals is trusted in proportion to the number
of residuals; the remainder of the probability mass is a unit-variance
Student-t(4) tail, applied by quantile mapping so dependence is preserved.
This is a stated modelling assumption, not a substitute for missing data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np

from app.candidate.config import CandidateConfig
from app.markets.hierarchical import Prior, log_sd_sampling_variance


@dataclass
class InstrumentEstimate:
    key: str
    sessions: int
    dates: list[date]
    returns: np.ndarray               # observed daily log returns
    residuals: np.ndarray             # EWMA-standardised, winsorised at |z| <= 10
    sigma_raw: float                  # max(EWMA, long-run) daily vol
    variance_ratio: float
    sigma_own: float                  # sigma_raw * sqrt(VR)
    sigma_posterior: float            # after pooling
    sigma_used: float                 # credible bound used for risk
    pooling_weight: float             # weight on the instrument's own estimate
    posterior_sd_log_vol: float
    effective_observations: float
    stale_share: float
    missing_share: float
    range_share: float
    volume_share: float
    data_quality: float
    tail_weight: float                # weight on the empirical residual tail
    adv_shares: float | None
    spread: float
    spread_source: str
    eligible: bool
    reasons: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "sessions": self.sessions,
            "effective_observations": round(self.effective_observations, 1),
            "daily_vol_raw": self.sigma_raw,
            "variance_ratio": self.variance_ratio,
            "daily_vol_own": self.sigma_own,
            "daily_vol_posterior": self.sigma_posterior,
            "daily_vol_used": self.sigma_used,
            "annual_vol_used": self.sigma_used * math.sqrt(252),
            "pooling_weight_own": self.pooling_weight,
            "posterior_sd_log_vol": self.posterior_sd_log_vol,
            "stale_share": self.stale_share,
            "missing_share": self.missing_share,
            "range_reported_share": self.range_share,
            "volume_reported_share": self.volume_share,
            "data_quality": self.data_quality,
            "empirical_tail_weight": self.tail_weight,
            "adv_shares": self.adv_shares,
            "spread": self.spread,
            "spread_source": self.spread_source,
            "eligible_for_lending_value": self.eligible,
            "reasons": self.reasons,
        }


def _as_date(x: Any) -> date:
    if isinstance(x, date):
        return x
    return date.fromisoformat(str(x)[:10])


def corwin_schultz(high: np.ndarray, low: np.ndarray, ok: np.ndarray) -> np.ndarray:
    out = np.full(high.size, np.nan)
    k = 3 - 2 * math.sqrt(2)
    for t in range(1, high.size):
        if not (ok[t] and ok[t - 1]) or min(low[t], low[t - 1]) <= 0 or high[t] <= 0:
            continue
        beta = math.log(high[t] / low[t]) ** 2 + math.log(high[t - 1] / low[t - 1]) ** 2
        gamma = math.log(max(high[t], high[t - 1]) / min(low[t], low[t - 1])) ** 2
        a = (math.sqrt(2 * beta) - math.sqrt(beta)) / k - math.sqrt(gamma / k)
        out[t] = max(0.0, 2 * (math.exp(a) - 1) / (1 + math.exp(a)))
    return out


def variance_ratio(r: np.ndarray, q: int) -> float:
    if r.size < 3 * q:
        return 1.0
    v1 = float(r.var(ddof=1))
    if v1 <= 0:
        return 1.0
    c = np.concatenate([[0.0], np.cumsum(r)])
    vq = float((c[q:] - c[:-q]).var(ddof=1))
    return float(np.clip(vq / (q * v1), 1.0, 6.0))


def ewma(r: np.ndarray, lam: float) -> tuple[np.ndarray, float]:
    seed = float(np.var(r[: min(60, r.size)])) + 1e-10
    var = seed
    prev = np.empty(r.size)
    for t, x in enumerate(r):
        prev[t] = math.sqrt(var)
        var = lam * var + (1 - lam) * x * x
    return prev, math.sqrt(var)


def raw_daily_vol(bars: list[dict[str, Any]], cfg: CandidateConfig) -> float | None:
    """Daily vol of a reference instrument (for building group priors)."""
    closes = np.array([float(b["close"]) for b in bars if b.get("close")])
    if closes.size < cfg.full_tail_sessions:
        return None
    r = np.diff(np.log(closes))
    _, now = ewma(r, cfg.ewma_lambda)
    return max(now, float(r[-252:].std(ddof=1))) * math.sqrt(variance_ratio(r, cfg.multi_session_lag))


def estimate_instrument(
    key: str,
    bars: list[dict[str, Any]],
    as_of: date,
    prior: Prior | None,
    spread_floor: float,
    spread_fallback: float,
    cfg: CandidateConfig,
    quote_spread: float | None = None,
) -> InstrumentEstimate:
    rows = sorted((b for b in bars if _as_date(b["bar_date"]) <= as_of and b.get("close")),
                  key=lambda b: _as_date(b["bar_date"]))[-(cfg.history + 1):]
    reasons: list[str] = []
    dates = [_as_date(b["bar_date"]) for b in rows]
    closes = np.array([float(b["close"]) for b in rows])
    n = max(0, closes.size - 1)
    if n < 2:
        return InstrumentEstimate(
            key, n, dates, np.zeros(0), np.zeros(0), math.nan, 1.0, math.nan, math.nan, math.nan, 0.0,
            math.inf, 0.0, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, None, spread_fallback, "profile_fallback", False,
            [f"only {n} stored sessions; no lending value"])
    r = np.diff(np.log(closes))
    # Missing sessions: weekdays between first and last bar with no bar.
    span = int(np.busday_count(dates[0], dates[-1])) + 1
    missing_share = float(max(0, span - len(dates)) / max(span, 1))
    stale_share = float(np.mean(np.abs(r) < 1e-12))
    range_ok = np.array([bool(b.get("range_reported")) for b in rows])
    vol_ok = np.array([bool(b.get("volume_reported")) and (b.get("volume") or 0) > 0 for b in rows])
    range_share, volume_share = float(range_ok.mean()), float(vol_ok.mean())

    prev, now = ewma(r, cfg.ewma_lambda)
    long = float(r[-252:].std(ddof=1)) if n >= 3 else now
    sigma_raw = max(now, long, 1e-6)
    vr = variance_ratio(r, cfg.multi_session_lag)
    sigma_own = sigma_raw * math.sqrt(vr)
    resid = np.clip(r / np.maximum(prev, 1e-8), -10.0, 10.0)
    if n > 60:
        resid = resid[60:]
    # Effective observations: non-stale returns, deflated by serial dependence.
    n_eff = max(1.0, n * (1 - stale_share) / vr)
    v = log_sd_sampling_variance(round(n_eff))
    if prior is not None:
        w = prior.tau2 / (prior.tau2 + v)
        mu_post = w * math.log(sigma_own) + (1 - w) * prior.mu_log_vol
        var_post = 1.0 / (1.0 / v + 1.0 / prior.tau2)
    else:
        w, mu_post, var_post = 1.0, math.log(sigma_own), v
        reasons.append("no group prior available: own estimate with its full sampling uncertainty")
    sd_post = math.sqrt(var_post)
    sigma_post = math.exp(mu_post)
    sigma_used = math.exp(mu_post + cfg.uncertainty_z * sd_post)
    tail_weight = float(min(1.0, resid.size / cfg.full_tail_sessions))

    # Liquidity: median reported shares volume over the last 20 sessions.
    vols = [float(b["volume"]) for b, ok in zip(rows[-20:], vol_ok[-20:], strict=True) if ok]
    adv = float(np.median(vols)) if len(vols) >= 10 else None
    if adv is None:
        reasons.append("fewer than 10 of the last 20 sessions report volume")

    highs = np.array([float(b.get("high") or 0) for b in rows])
    lows = np.array([float(b.get("low") or 0) for b in rows])
    cs = corwin_schultz(highs, lows, range_ok)[-21:]
    cs = cs[~np.isnan(cs)]
    if cs.size >= 5:
        spread, source = max(spread_floor, float(np.median(cs))), "estimated_from_range"
    else:
        spread, source = spread_fallback, "profile_fallback"
    if quote_spread is not None and quote_spread > spread:
        spread, source = quote_spread, "current_quote"

    dq = (1 - missing_share) * (0.5 + 0.5 * range_share) * (0.5 + 0.5 * volume_share)
    eligible = True
    if n < cfg.min_sessions:
        eligible = False
        reasons.append(f"{n} sessions is below the {cfg.min_sessions}-session minimum")
    if dq < cfg.min_data_quality:
        eligible = False
        reasons.append(f"data quality {dq:.2f} below {cfg.min_data_quality}")
    if adv is None:
        eligible = False
    if w < 0.5:
        reasons.append(f"short or stale history: own estimate weighted {w:.0%}, group prior {1 - w:.0%}")
    if tail_weight < 1:
        reasons.append(f"empirical tail weighted {tail_weight:.0%}; remainder Student-t(4)")
    if vr > 1.2:
        reasons.append(f"variance ratio {vr:.2f}: daily volatility scaled for multi-session dependence")
    return InstrumentEstimate(
        key, n, dates, r, resid, sigma_raw, vr, sigma_own, sigma_post, sigma_used, w, sd_post, n_eff,
        stale_share, missing_share, range_share, volume_share, dq, tail_weight, adv, spread, source,
        eligible, reasons)


def group_prior(log_vols: list[float], source: str) -> Prior | None:
    if len(log_vols) < 3:
        return None
    mu = float(np.mean(log_vols))
    tau2 = float(np.var(log_vols, ddof=1))
    return Prior(mu, max(tau2, 1e-4), len(log_vols), source)
