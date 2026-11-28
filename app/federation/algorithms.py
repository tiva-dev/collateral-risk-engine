"""Standardised outcome schemas and allow-listed federated algorithms.

Agents never execute remotely supplied code. A signed job package names an
algorithm id from ``REGISTRY`` and supplies only parameters; the agent's
installed (signed, versioned) code implements it.

Schemas (shared feature/outcome contracts; units fixed):
  margin_call_outcome   cure within the notice window (0/1) and its features
  liquidation_execution realised slippage (log of fraction of mid) and features
  recovery_episode      recovery duration (log sessions) and features

Methods:
  cure_logistic_v1      federated ML: FedProx logistic regression; contribution
                        is a weighted, clipped model delta
  slippage_ols_v1       federated statistics: sufficient statistics of OLS;
  duration_ols_v1       the aggregate gives exactly the pooled estimate
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import numpy as np

SCHEMAS: dict[str, dict[str, Any]] = {
    "margin_call_outcome": {
        "features": {"ltv_breach": "LTV above the margin-call threshold, fraction (0-0.5)",
                     "notice_sessions": "sessions given to cure (1-10)",
                     "log_balance": "natural log of loan balance in USD equivalent",
                     "tenure_years": "client tenure, years (0-30)",
                     "prior_calls_12m": "margin calls in the previous 12 months (count)",
                     "market_stress": "market-wide stress indicator (0/1)",
                     "collateral_liquidity": "share of collateral in liquid instruments (0-1)"},
        "outcome": {"cured": "borrower cured within the notice window (0/1)"}},
    "liquidation_execution": {
        "features": {"log_participation": "log of order size / 20-session ADV",
                     "spread_bps": "quoted half-spread, basis points", "market_stress": "0/1"},
        "outcome": {"log_slippage": "log of implementation shortfall as a fraction of mid"}},
    "recovery_episode": {
        "features": {"log_participation": "log of position / 20-session ADV", "market_stress": "0/1",
                     "settlement_sessions": "settlement cycle in sessions"},
        "outcome": {"log_duration": "log of sessions from first sale to completion"}},
}


def standardise(x: np.ndarray, mean: np.ndarray, sd: np.ndarray) -> np.ndarray:
    return (x - mean) / sd


def _sigmoid(z: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def logistic_fit(x: np.ndarray, y: np.ndarray, w0: np.ndarray, *, epochs: int, lr: float, l2: float,
                 prox_mu: float = 0.0, rng: np.random.Generator | None = None, batch: int = 64) -> np.ndarray:
    """Mini-batch gradient descent on logistic loss with L2 and a FedProx term
    mu/2 ||w - w0||^2 (keeps heterogeneous local updates near the global model)."""
    rng = rng or np.random.default_rng(0)
    xb = np.hstack([np.ones((x.shape[0], 1)), x])
    w = w0.copy()
    for _ in range(epochs):
        idx = rng.permutation(xb.shape[0])
        for s in range(0, idx.size, batch):
            b = idx[s:s + batch]
            g = xb[b].T @ (_sigmoid(xb[b] @ w) - y[b]) / b.size + l2 * np.r_[0.0, w[1:]] + prox_mu * (w - w0)
            w -= lr * g
    return w


def logistic_predict(x: np.ndarray, w: np.ndarray) -> np.ndarray:
    return _sigmoid(np.hstack([np.ones((x.shape[0], 1)), x]) @ w)


def metrics(y: np.ndarray, p: np.ndarray) -> dict[str, float]:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    ll = float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))
    brier = float(np.mean((p - y) ** 2))
    pos, neg = p[y == 1], p[y == 0]
    if pos.size and neg.size:
        ranks = np.argsort(np.argsort(np.r_[pos, neg])) + 1
        auc = float((ranks[: pos.size].sum() - pos.size * (pos.size + 1) / 2) / (pos.size * neg.size))
    else:
        auc = math.nan
    return {"log_loss": ll, "brier": brier, "auc": auc, "n": int(y.size)}


def suffstats(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    xb = np.hstack([np.ones((x.shape[0], 1)), x])
    xtx = xb.T @ xb
    iu = np.triu_indices(xtx.shape[0])
    return np.r_[xtx[iu], xb.T @ y, y @ y, float(y.size)]


def ols_from_suffstats(s: np.ndarray, k: int) -> dict[str, Any]:
    p = k + 1
    m = p * (p + 1) // 2
    xtx = np.zeros((p, p))
    iu = np.triu_indices(p)
    xtx[iu] = s[:m]
    xtx = xtx + xtx.T - np.diag(np.diag(xtx))
    xty, yty, n = s[m:m + p], s[m + p], s[m + p + 1]
    inv = np.linalg.inv(xtx + 1e-9 * np.eye(p))
    beta = inv @ xty
    resid_ss = max(yty - 2 * beta @ xty + beta @ xtx @ beta, 0.0)
    sigma = math.sqrt(resid_ss / max(n - p, 1))
    return {"beta": beta, "sigma": sigma, "n": n, "se": sigma * np.sqrt(np.diag(inv))}


@dataclass(frozen=True)
class Algorithm:
    id: str
    schema: str
    kind: str                              # ml | statistics
    dimension: Callable[[int], int]        # contribution length for k features
    params: frozenset[str]


REGISTRY: dict[str, Algorithm] = {
    "cure_logistic_v1": Algorithm("cure_logistic_v1", "margin_call_outcome", "ml", lambda k: k + 1,
                                  frozenset({"epochs", "lr", "l2", "prox_mu", "clip", "noise_multiplier",
                                             "weight_cap"})),
    "slippage_ols_v1": Algorithm("slippage_ols_v1", "liquidation_execution", "statistics",
                                 lambda k: (k + 1) * (k + 2) // 2 + (k + 1) + 2, frozenset({"min_rows"})),
    "duration_ols_v1": Algorithm("duration_ols_v1", "recovery_episode", "statistics",
                                 lambda k: (k + 1) * (k + 2) // 2 + (k + 1) + 2, frozenset({"min_rows"})),
}
