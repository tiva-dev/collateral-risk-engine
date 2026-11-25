"""Candidate capacity methodologies A, B, C and the feature estimation they share.

Every method returns, at origination, a capacity ratio ell_0 = P / V_0 (loan
principal per unit of collateral value) and a function giving the capacity
ratio ell_t used for maintenance monitoring along a path. All estimates use
only information observable at or before the decision date.

A  Transparent risk-based haircut: parametric Student-t expected shortfall of
   the unsold position over a recovery-latency-weighted horizon, stressed
   correlation, plus additive (non-diversifying) liquidation cost.
B  Filtered historical simulation (Barone-Adesi et al. 1999; Hull-White 1998
   volatility updating with an EWMA filter) of the joint loss of the
   liquidation schedule, expected shortfall at confidence alpha.
C  Stochastic recovery: choose ell_0 so that the simulated distribution of
   lender loss over the loan term, under the full monitoring/liquidation
   policy and FHS scenarios, meets explicit risk-appetite constraints.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy import stats

from . import lifecycle

LAMBDA = 0.94
DAILY = 252.0


@dataclass
class Features:
    weights: np.ndarray
    r_hist: np.ndarray           # (H, n) observed loan-currency log returns
    ewma_sigma: np.ndarray       # (n,) daily, at decision date
    long_sigma: np.ndarray       # (n,) daily, 252-day realised
    resid: np.ndarray            # (H, n) EWMA-standardised residual vectors
    stressed_corr: np.ndarray    # (n, n)
    nu: float
    spread: np.ndarray           # (n,) current
    spread_stress_mult: float
    adv: np.ndarray              # (n,) current, value units (V0 = 1)
    halt_freq: float
    halt_mean_days: float
    fx_sigma: float              # daily, 0 if same currency
    engine_inputs: list[dict]


def ewma_filter(r: np.ndarray, seed_var: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    var = seed_var.copy()
    sig_prev = np.zeros_like(r)
    for t in range(r.shape[0]):
        sig_prev[t] = np.sqrt(var)
        var = LAMBDA * var + (1 - LAMBDA) * r[t] ** 2
    return sig_prev, np.sqrt(var)


def estimate_features(
    r_obs: np.ndarray, spread_hist: np.ndarray, adv_hist: np.ndarray,
    halted_hist: np.ndarray, fx_hist: np.ndarray | None, weights: np.ndarray,
    asset_type: str,
) -> Features:
    h, n = r_obs.shape
    seed = np.var(r_obs[:60], axis=0) + 1e-10
    sig_prev, sig_now = ewma_filter(r_obs, seed)
    resid = r_obs[60:] / np.maximum(sig_prev[60:], 1e-8)
    # Halts make observed returns zero and then catch up in one day; the EWMA
    # volatility has decayed meanwhile, so the catch-up residual is extreme.
    # Residuals are winsorised at |z| <= 10 to keep the FHS filter stable; the
    # gap itself is a separate risk (proposed G_alpha term, section 6.2).
    resid = np.clip(resid, -10.0, 10.0)
    long_sigma = r_obs[-252:].std(axis=0, ddof=1)
    port = r_obs @ weights
    cutoff = np.quantile(port, 0.2)
    worst = r_obs[port <= cutoff]
    if n > 1:
        corr = np.corrcoef(worst, rowvar=False)
        corr = np.nan_to_num(corr, nan=0.0)
        np.fill_diagonal(corr, 1.0)
        off = corr[~np.eye(n, dtype=bool)].mean()
        corr = 0.8 * corr + 0.2 * (np.full((n, n), off) + np.eye(n) * (1 - off))
    else:
        corr = np.ones((1, 1))
    kurt = stats.kurtosis(resid.ravel(), fisher=True)
    nu = float(np.clip(4 + 6 / max(kurt, 1e-3), 3.0, 30.0))
    halt_starts = (halted_hist[1:] & ~halted_hist[:-1]).sum()
    halt_days = halted_hist.sum()
    halt_freq = float(halt_starts / max(h * n, 1))
    halt_mean = float(halt_days / max(halt_starts, 1)) if halt_starts else 0.0
    med = np.median(spread_hist, axis=0)
    p90 = np.quantile(spread_hist, 0.9, axis=0)
    engine_inputs = []
    for i in range(n):
        series = r_obs[:, i]
        price = np.exp(np.cumsum(series[-252:]))
        mdd = float(np.max(1 - price / np.maximum.accumulate(price)))
        engine_inputs.append(
            {
                "id": f"A{i}",
                "type": asset_type,
                "weight": float(weights[i]),
                "spread": float(spread_hist[-1, i]),
                "adv_value_ratio": float(adv_hist[-1, i]),
                "vol30": float(series[-30:].std(ddof=1) * np.sqrt(DAILY)),
                "vol90": float(series[-90:].std(ddof=1) * np.sqrt(DAILY)),
                "vol252": float(series[-252:].std(ddof=1) * np.sqrt(DAILY)),
                "mdd252": mdd,
                "gap252": float(np.max(np.abs(np.expm1(series[-252:])))),
            }
        )
    return Features(
        weights=weights, r_hist=r_obs, ewma_sigma=sig_now, long_sigma=long_sigma,
        resid=resid, stressed_corr=corr, nu=nu, spread=spread_hist[-1],
        spread_stress_mult=float(np.mean(p90 / np.maximum(med, 1e-12))),
        adv=adv_hist[-1], halt_freq=halt_freq, halt_mean_days=halt_mean,
        fx_sigma=float(fx_hist[-252:].std(ddof=1)) if fx_hist is not None else 0.0,
        engine_inputs=engine_inputs,
    )


def t_es_unit(alpha: float, nu: float) -> float:
    """Expected shortfall of a unit-variance Student-t at level alpha."""
    q = stats.t.ppf(alpha, nu)
    es = stats.t.pdf(q, nu) / (1 - alpha) * (nu + q * q) / (nu - 1)
    return float(es * np.sqrt((nu - 2) / nu))


def execution_days(f: Features, policy: lifecycle.Policy, adv: np.ndarray | None = None) -> np.ndarray:
    adv = f.adv if adv is None else adv
    return np.ceil(f.weights / np.maximum(policy.participation * adv, 1e-12))


def recovery_days(f: Features, policy: lifecycle.Policy) -> float:
    return float(policy.grace + 1 + execution_days(f, policy).max() + policy.settle_days)


def haircut_a(
    f: Features, policy: lifecycle.Policy, alpha: float, sigma: np.ndarray,
    spread: np.ndarray, adv: np.ndarray,
) -> float:
    """Approach A haircut H (fraction of collateral value)."""
    t_exec = execution_days(f, policy, adv)
    # Price exposure: full during detection + grace, then linearly declining
    # during execution, so the variance-equivalent horizon is g + 1 + T_exec/3.
    horizon = policy.grace + 1 + t_exec / 3.0
    h = f.weights * sigma * np.sqrt(horizon)
    sigma_p = float(np.sqrt(h @ f.stressed_corr @ h))
    market = 1 - np.exp(-t_es_unit(alpha, f.nu) * sigma_p)
    s_stress = spread * f.spread_stress_mult
    liq = f.weights * (
        s_stress / 2 + policy.impact_y * sigma * np.sqrt(policy.participation) + policy.fee
    )
    settle_fx = 1 - np.exp(-t_es_unit(alpha, f.nu) * f.fx_sigma * np.sqrt(policy.settle_days))
    return float(min(0.99, market + liq.sum() + settle_fx))


def ratio_from_haircut(haircut: float, f: Features, policy: lifecycle.Policy, cap: float) -> float:
    growth = 1 + policy.rate * recovery_days(f, policy) / DAILY
    return float(np.clip((1 - haircut) / growth, 0.0, cap))


def approach_a(f: Features, policy: lifecycle.Policy, alpha: float, cap: float) -> tuple[float, float]:
    sigma = np.maximum(f.ewma_sigma, f.long_sigma)  # anti-procyclicality floor
    hcut = haircut_a(f, policy, alpha, sigma, f.spread, f.adv)
    return ratio_from_haircut(hcut, f, policy, cap), hcut


def stressed_rows(f: Features, share: float = 0.2) -> np.ndarray:
    """Indices of residual vectors on the worst `share` of portfolio days."""
    port = f.resid @ f.weights
    return np.flatnonzero(port <= np.quantile(port, share))


def fhs_paths(
    f: Features, days: int, n_paths: int, rng: np.random.Generator,
    stress_share: float = 0.0,
) -> tuple[np.ndarray, np.ndarray]:
    """FHS scenarios: bootstrap joint residual vectors, EWMA-updated volatility.

    With stress_share > 0, that fraction of days is drawn from the stressed
    subset of residual vectors (worst 20% portfolio days): a stressed overlay.
    """
    idx = rng.integers(0, f.resid.shape[0], size=(n_paths, days))
    if stress_share > 0:
        pool = stressed_rows(f)
        swap = rng.random((n_paths, days)) < stress_share
        idx = np.where(swap, pool[rng.integers(0, pool.size, size=(n_paths, days))], idx)
    z = f.resid[idx]                                   # (M, D, n)
    var = np.broadcast_to(np.maximum(f.ewma_sigma, f.long_sigma) ** 2, (n_paths, f.weights.size)).copy()
    r = np.empty_like(z)
    sig = np.empty_like(z)
    for t in range(days):
        s = np.sqrt(var)
        sig[:, t] = s
        r[:, t] = s * z[:, t]
        var = LAMBDA * var + (1 - LAMBDA) * r[:, t] ** 2
    return r, sig


def gap_term(f: Features, tail: float = 0.01) -> float:
    """Proposed gap add-on G: mean of the worst `tail` share of single-day
    portfolio losses in the observed history (one extra gap during recovery)."""
    port_loss = -np.expm1(f.r_hist @ f.weights)
    k = max(1, int(np.ceil(tail * port_loss.size)))
    return float(np.sort(port_loss)[-k:].mean())


def approach_b(
    f: Features, policy: lifecycle.Policy, alpha: float, cap: float,
    rng: np.random.Generator, n_paths: int = 2000, stress_share: float = 0.0,
    with_gap: bool = False,
) -> tuple[float, float]:
    t_exec = execution_days(f, policy).astype(int)
    start = policy.grace + 1
    days = int(start + t_exec.max())
    r, sig = fhs_paths(f, days, n_paths, rng, stress_share)
    prices = np.exp(np.cumsum(r, axis=1))
    s_stress = f.spread * f.spread_stress_mult
    proceeds = np.zeros(n_paths)
    for i, w in enumerate(f.weights):
        k = max(int(t_exec[i]), 1)
        sale_days = np.arange(start, start + k) - 1
        impact = policy.impact_y * sig[:, sale_days, i] * np.sqrt(policy.participation)
        px = prices[:, sale_days, i] * (1 - s_stress[i] / 2 - impact - policy.fee)
        proceeds += w * px.mean(axis=1)
    loss = 1 - proceeds
    var_level = np.quantile(loss, alpha)
    es = float(loss[loss >= var_level].mean())
    settle_fx = 1 - np.exp(-t_es_unit(alpha, f.nu) * f.fx_sigma * np.sqrt(policy.settle_days))
    gap = gap_term(f) if with_gap else 0.0
    hcut = float(min(0.99, max(es, 0.0) + settle_fx + gap))
    return ratio_from_haircut(hcut, f, policy, cap), hcut


def dynamic_ratio(
    ell0: float, haircut0: float, f: Features, r_obs_path: np.ndarray, cap: float,
    policy: lifecycle.Policy,
) -> np.ndarray:
    """Capacity ratio along a path: haircut scaled by portfolio EWMA vol (floored).

    r_obs_path: (M, D, n) observed loan-currency returns after origination.
    """
    m, d, n = r_obs_path.shape
    var = np.broadcast_to(f.ewma_sigma**2, (m, n)).copy()
    floor = f.long_sigma
    base = float(np.sqrt(f.weights**2 @ np.maximum(f.ewma_sigma, floor) ** 2))
    growth = 1 + policy.rate * recovery_days(f, policy) / DAILY
    out = np.empty((m, d))
    for t in range(d):
        sig = np.maximum(np.sqrt(var), floor)
        port = np.sqrt((f.weights**2 * sig**2).sum(axis=1))
        hcut = np.clip(haircut0 * port / max(base, 1e-12), 0.0, 0.99)
        out[:, t] = np.minimum(cap, ell0 * growth * (1 - hcut) / max(1 - haircut0, 1e-9) / growth)
        var = LAMBDA * var + (1 - LAMBDA) * r_obs_path[:, t] ** 2
    return out


@dataclass(frozen=True)
class Appetite:
    name: str
    max_loss_probability: float      # P(L > 0) over the loan term
    max_expected_loss: float         # E[L] / P
    max_es_975: float                # ES_97.5(L) / P


APPETITES = {
    "conservative": Appetite("conservative", 0.005, 0.0001, 0.02),
    "balanced": Appetite("balanced", 0.02, 0.0005, 0.10),
    "commercial": Appetite("commercial", 0.05, 0.0020, 0.25),
}


def approach_c(
    f: Features, policy: lifecycle.Policy, appetite: Appetite, cap: float,
    term_days: int, rng: np.random.Generator, n_paths: int = 1000,
    haircut_b: float | None = None,
    maintenance_ell0: float | None = None,
) -> tuple[float, dict]:
    """Largest ell_0 whose simulated lifecycle loss meets the appetite."""
    r, sig = fhs_paths(f, term_days, n_paths, rng)
    n = f.weights.size
    halted = np.zeros_like(r, dtype=bool)
    if f.halt_freq > 0:
        starts = rng.random(r.shape) < f.halt_freq
        mean = max(f.halt_mean_days, 1.0)
        for t in range(term_days):
            if t:
                carry = halted[:, t - 1] & (rng.random((n_paths, n)) > 1 / mean)
                halted[:, t] = carry | starts[:, t]
            else:
                halted[:, t] = starts[:, t]
    spread = np.broadcast_to(f.spread * f.spread_stress_mult, r.shape)
    adv = np.broadcast_to(f.adv, r.shape)
    hb = haircut_b if haircut_b is not None else 0.2
    shape = dynamic_ratio(1.0, hb, f, r, cap=10.0, policy=policy)  # relative path shape
    lo, hi = 0.0, cap
    best = 0.0
    stats_best: dict = {}
    for _ in range(12):
        mid = (lo + hi) / 2
        ell = (
            np.minimum(cap, mid * shape)
            if maintenance_ell0 is None
            else np.minimum(10.0, maintenance_ell0 * shape)
        )
        out = lifecycle.simulate_loans(
            mid, f.weights, r, halted, spread, adv, sig, ell, None, policy, rng
        )
        loss = out["loss"] / max(mid, 1e-12)
        p_loss = float((out["loss"] > 1e-9).mean())
        el = float(loss.mean())
        q = np.quantile(loss, 0.975)
        es = float(loss[loss >= q].mean())
        ok = (
            p_loss <= appetite.max_loss_probability
            and el <= appetite.max_expected_loss
            and es <= appetite.max_es_975
        )
        if ok:
            best, lo = mid, mid
            stats_best = {"p_loss": p_loss, "el": el, "es975": es}
        else:
            hi = mid
    return best, stats_best
