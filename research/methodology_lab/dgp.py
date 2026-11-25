"""Synthetic market data-generating process (DGP) for methodology experiments.

SYNTHETIC DATA ONLY. These paths are not historical observations and must
never be presented as empirical validation. Parameters are stylised, chosen
to reproduce well-documented qualitative features of equity returns
(volatility clustering, fat tails, correlations that rise in stress, jumps,
liquidity that deteriorates in stress, trading suspensions, currency
devaluation). They are not calibrated to any provider dataset.

Model, per trading day t and asset i (log returns in local currency):

    regime s_t in {0 calm, 1 stress}, Markov with P(0->1)=p01, P(1->1)=p11
    h_{i,t} = omega_i + a * (sigma_{i,t-1} z_{i,t-1})^2 + b * h_{i,t-1}   (GARCH(1,1))
    sigma_{i,t} = sqrt(h_{i,t}) * (k_stress if s_t = 1 else 1)
    z_t = L_{s_t} x_t * sqrt((nu - 2) / W_t),  x_t ~ N(0, I), W_t ~ chi2(nu)
          (multivariate Student-t, unit variance, regime correlation R_s = L_s L_s')
    r_{i,t} = sigma_{i,t} z_{i,t} + J^c_t + J^i_{i,t}
    J^c_t  ~ Bernoulli(lambda_c[s_t]) * N(mu_c, sd_c)      common gap
    J^i_{i,t} ~ Bernoulli(lambda_i) * N(mu_i, sd_i)        idiosyncratic gap

Liquidity: ADV_{i,t} = ADV0_i * exp(u_{i,t}) * (adv_stress if s_t=1),
u AR(1); relative spread s_{i,t} = s0_i * exp(v_{i,t}) * (spread_stress if s_t=1).
Halts: each asset starts a suspension with probability halt_p per day, with
geometric duration of mean halt_days; during a halt the observed price is
stale and no trading is possible; the true value keeps moving.
FX (optional): log local-currency units per loan-currency unit follows
sigma_fx * N(0,1) plus devaluation jumps; loan-currency return
r_loan = r_local - d log X.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(frozen=True)
class Profile:
    name: str
    n_assets: int
    vol: tuple[float, ...]                  # long-run annualised vol per asset
    rho_calm: float
    rho_stress: float
    spread: tuple[float, ...]               # calm relative bid-ask spread
    asset_type: str                         # CRI AssetType value for engines
    idio_jump_p: float = 1 / 500
    idio_jump_mu: float = -0.02
    idio_jump_sd: float = 0.06
    halt_p: float = 0.0
    halt_days: float = 1.0
    fx: bool = False                        # collateral currency != loan currency
    fx_vol: float = 0.10
    fx_jump_p_calm: float = 1 / 400
    fx_jump_p_stress: float = 1 / 60
    fx_jump_mu: float = 0.20                # depreciation of collateral currency
    adv_stress: float = 0.5
    spread_stress: float = 2.5
    market: str = "US"
    size_ratio: dict[str, float] = field(default_factory=dict)  # position / ADV


GARCH_A, GARCH_B = 0.08, 0.90
NU = 5.0
P01, P11 = 1 / 250, 0.97
K_STRESS = 1.8
COMMON_JUMP_P = (1 / 1000, 1 / 50)
COMMON_JUMP_MU, COMMON_JUMP_SD = -0.05, 0.03
TRADING_DAYS = 252


def _corr_chol(n: int, rho: float) -> np.ndarray:
    if n == 1:
        return np.ones((1, 1))
    corr = np.full((n, n), rho)
    np.fill_diagonal(corr, 1.0)
    return np.linalg.cholesky(corr)


def simulate(profile: Profile, days: int, rng: np.random.Generator) -> dict[str, np.ndarray]:
    """Return arrays (days x n) of true loan-currency log returns and market state."""
    n = profile.n_assets
    vol = np.asarray(profile.vol) / np.sqrt(TRADING_DAYS)
    omega = vol**2 * (1 - GARCH_A - GARCH_B)
    chol = (_corr_chol(n, profile.rho_calm), _corr_chol(n, profile.rho_stress))
    regime = np.zeros(days, dtype=int)
    h = vol**2
    last_shock = np.zeros(n)
    r_local = np.zeros((days, n))
    sigma_out = np.zeros((days, n))
    u = np.zeros(n)
    v = np.zeros(n)
    adv = np.zeros((days, n))
    spread = np.zeros((days, n))
    halted = np.zeros((days, n), dtype=bool)
    halt_left = np.zeros(n, dtype=int)
    fx_ret = np.zeros(days)
    state = 0
    for t in range(days):
        if t > 0:
            state = int(rng.random() < (P11 if state == 1 else P01))
        regime[t] = state
        h = omega + GARCH_A * last_shock**2 + GARCH_B * h
        sig = np.sqrt(h) * (K_STRESS if state else 1.0)
        w = rng.chisquare(NU)
        z = chol[state] @ rng.standard_normal(n) * np.sqrt((NU - 2) / w)
        shock = sig * z
        last_shock = np.sqrt(h) * z
        common = (
            rng.normal(COMMON_JUMP_MU, COMMON_JUMP_SD)
            if rng.random() < COMMON_JUMP_P[state]
            else 0.0
        )
        idio = np.where(
            rng.random(n) < profile.idio_jump_p,
            rng.normal(profile.idio_jump_mu, profile.idio_jump_sd, n),
            0.0,
        )
        r_local[t] = shock + common + idio
        sigma_out[t] = sig
        u = 0.9 * u + 0.3 * np.sqrt(1 - 0.81) * rng.standard_normal(n)
        v = 0.9 * v + 0.2 * np.sqrt(1 - 0.81) * rng.standard_normal(n)
        adv[t] = np.exp(u) * (profile.adv_stress if state else 1.0)
        spread[t] = np.asarray(profile.spread) * np.exp(v) * (
            profile.spread_stress if state else 1.0
        )
        start = (halt_left == 0) & (rng.random(n) < profile.halt_p)
        halt_left = np.where(
            start, rng.geometric(1 / max(profile.halt_days, 1.0), n), halt_left
        )
        halted[t] = halt_left > 0
        halt_left = np.maximum(0, halt_left - 1)
        if profile.fx:
            jump_p = profile.fx_jump_p_stress if state else profile.fx_jump_p_calm
            dev = rng.normal(profile.fx_jump_mu, 0.08) if rng.random() < jump_p else 0.0
            fx_ret[t] = profile.fx_vol / np.sqrt(TRADING_DAYS) * rng.standard_normal() + max(dev, 0.0)
    r_loan = r_local - fx_ret[:, None]
    return {
        "r_loan": r_loan,
        "r_local": r_local,
        "fx_ret": fx_ret,
        "sigma_true": sigma_out,
        "regime": regime,
        "adv_mult": adv,
        "spread": spread,
        "halted": halted,
    }


PROFILES: dict[str, Profile] = {
    "us_etf_pair": Profile(
        "us_etf_pair", 2, (0.17, 0.23), 0.85, 0.95, (0.0001, 0.0001), "etf",
        idio_jump_p=1 / 2000, idio_jump_sd=0.03,
        size_ratio={"retail": 0.001, "institutional": 0.5},
    ),
    "us_single_aapl_like": Profile(
        "us_single_aapl_like", 1, (0.28,), 0.0, 0.0, (0.0002,), "listed_equity",
        size_ratio={"retail": 0.005, "institutional": 2.0},
    ),
    "us_single_tsla_like": Profile(
        "us_single_tsla_like", 1, (0.58,), 0.0, 0.0, (0.0004,), "high_volatility_equity",
        idio_jump_p=1 / 150, idio_jump_mu=-0.03, idio_jump_sd=0.10,
        size_ratio={"retail": 0.005, "institutional": 2.0},
    ),
    "us_correlated_tech10": Profile(
        "us_correlated_tech10", 10, (0.38,) * 10, 0.75, 0.92, (0.0005,) * 10,
        "listed_equity", size_ratio={"retail": 0.01, "institutional": 3.0},
    ),
    "us_diversified10": Profile(
        "us_diversified10", 10, (0.30,) * 10, 0.20, 0.55, (0.0005,) * 10,
        "listed_equity", size_ratio={"retail": 0.01, "institutional": 3.0},
    ),
    "ngx_two_name": Profile(
        "ngx_two_name", 2, (0.33, 0.38), 0.30, 0.60, (0.015, 0.020), "listed_equity",
        halt_p=1 / 400, halt_days=5, market="NGX",
        size_ratio={"retail": 0.2, "institutional": 10.0},
    ),
    "ngx_collateral_usd_loan": Profile(
        "ngx_collateral_usd_loan", 2, (0.33, 0.38), 0.30, 0.60, (0.015, 0.020),
        "listed_equity", halt_p=1 / 400, halt_days=5, fx=True, market="NGX",
        size_ratio={"retail": 0.2, "institutional": 10.0},
    ),
}
