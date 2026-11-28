"""Differential privacy for federated rounds: privacy units, sensitivity,
distributed Gaussian noise and persistent accounting (Report 17 review, section 5).

Secure aggregation and differential privacy are different protections
    Secure aggregation (``crypto``/``coordinator``) hides each institution's
    contribution from the coordinator; only the sum of surviving contributions
    is revealed. It says nothing about what that sum reveals. Differential
    privacy bounds what the released sum (and every model derived from it)
    reveals about the privacy unit. Neither protects against an institution's
    own use of its own data.

Privacy units and adjacency
    ``institution``  (federated ML, ``cure_logistic_v1``): neighbouring federations
        differ by the entire data of one participating institution (add/remove).
        The contribution v_i = [w_i * delta_i, w_i] has delta_i clipped to L2 norm
        C and w_i = min(n_i, W), so ||v_i|| <= W * sqrt(C^2 + 1) = S. The weighted
        numerator *and* the aggregate denominator (total weight) are both covered.
    ``record``  (federated statistics, ``*_ols_v1``): neighbouring federations differ
        by one loan record in one institution. Standardised features are clipped to
        [-B, B] and the outcome to [-Y, Y]; one record changes the sufficient
        statistics by at most S = sqrt((1 + k B^2)^2 + (1 + k B^2) Y^2 + Y^4 + 1).

Noise and accounting
    Every agent adds N(0, (z S)^2 / m) to every coordinate (m = planned agents), so
    the sum of the honest survivors carries variance (z S)^2 * h / m. The accountant
    charges each round with the effective multiplier z * sqrt(h / m), where h is the
    number of surviving agents not assumed to collude (survivors - max_colluders):
    colluders know their own noise. Rounds compose by Renyi DP of the Gaussian
    mechanism (no subsampling amplification). Agents keep their own ledger and
    refuse a round whose worst case (threshold survivors, maximum colluders) would
    exceed their consented budget, so a coordinator restart or replacement cannot
    reset a budget. Ledgers are append-only, hash-chained files.

Reading epsilon
    The epsilon values in the simulations (about 10-30 at delta = 1e-6) are weak
    formal guarantees for an institution-level unit. They are reported as computed,
    not described as strong privacy; whether a value is acceptable depends on the
    threat model and the participants' agreement.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

ORDERS = (1.25, 1.5, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 128, 256)
FEATURE_BOUND = 4.0       # B: standardised features clipped to [-B, B] for record-level statistics
OUTCOME_BOUND = 10.0      # Y: outcome clipped to [-Y, Y]


def clip(v: np.ndarray, c: float) -> tuple[np.ndarray, bool]:
    n = float(np.linalg.norm(v))
    return (v * (c / n), True) if n > c > 0 else (v, False)


def ml_sensitivity(clip_norm: float, weight_cap: float) -> float:
    """Institution-level L2 sensitivity of [w * delta, w]."""
    return weight_cap * math.sqrt(clip_norm ** 2 + 1.0)


def record_sensitivity(k: int, b: float = FEATURE_BOUND, y: float = OUTCOME_BOUND) -> float:
    """Record-level L2 sensitivity of the OLS sufficient statistics of k clipped features."""
    q = 1.0 + k * b * b                      # ||[1, x]||^2
    return math.sqrt(q * q + q * y * y + y ** 4 + 1.0)


def agent_noise(dim: int, sensitivity: float, multiplier: float, planned: int,
                rng: np.random.Generator) -> np.ndarray:
    """One agent's share of the distributed Gaussian noise (same scale for every agent)."""
    if multiplier <= 0:
        return np.zeros(dim)
    return rng.normal(0.0, multiplier * sensitivity / math.sqrt(max(planned, 1)), dim)


def effective_multiplier(multiplier: float, planned: int, survivors: int, colluders: int = 0) -> float:
    honest = max(survivors - colluders, 0)
    return multiplier * math.sqrt(honest / max(planned, 1)) if honest > 0 else 0.0


def rdp_epsilon(multipliers: list[float], delta: float) -> float:
    if not multipliers:
        return 0.0
    if any(z <= 0 for z in multipliers):
        return math.inf
    return min(sum(a / (2 * z ** 2) for z in multipliers) + math.log(1 / delta) / (a - 1) for a in ORDERS)


def epsilon(multiplier: float, rounds: int, delta: float) -> float:
    return rdp_epsilon([multiplier] * rounds, delta)


@dataclass
class PrivacyLedger:
    """Append-only, hash-chained privacy ledger. With a path it is persisted (file lock,
    replay on load), so restarting or replacing the process cannot reset the budget."""

    epsilon_limit: float
    delta: float = 1e-6
    path: Path | None = None
    entries: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not (self.epsilon_limit > 0 and 0 < self.delta < 1):
            raise ValueError("epsilon_limit must be > 0 and delta in (0, 1)")
        if self.path is not None:
            self.path = Path(self.path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.entries = self._load()

    def _load(self) -> list[dict[str, Any]]:
        assert self.path is not None
        if not self.path.exists():
            return []
        out, prev = [], ""
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            body = {k: v for k, v in e.items() if k != "hash"}
            if e.get("prev") != prev or e.get("hash") != _h(body):
                raise ValueError(f"privacy ledger {self.path} failed its integrity check")
            prev = e["hash"]
            out.append(e)
        return out

    def multipliers(self, scope: str) -> list[float]:
        return [float(e["z_eff"]) for e in self.entries if e["scope"] == scope]

    def spent(self, scope: str) -> float:
        return rdp_epsilon(self.multipliers(scope), self.delta)

    def can_charge(self, scope: str, z_eff: float) -> bool:
        return rdp_epsilon([*self.multipliers(scope), z_eff], self.delta) <= self.epsilon_limit

    def charge(self, scope: str, z_eff: float, **meta: Any) -> float:
        if self.path is not None:
            with self.path.open("a+") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                try:
                    self.entries = self._load()                     # another process may have charged
                    self._append(f, scope, z_eff, meta)
                finally:
                    fcntl.flock(f, fcntl.LOCK_UN)
        else:
            self._append(None, scope, z_eff, meta)
        return self.spent(scope)

    def _append(self, f: Any, scope: str, z_eff: float, meta: dict[str, Any]) -> None:
        if not self.can_charge(scope, z_eff):
            raise PermissionError(f"privacy budget for {scope} would be exceeded")
        body = {"scope": scope, "z_eff": z_eff, "at": datetime.now(UTC).isoformat(),
                "prev": self.entries[-1]["hash"] if self.entries else "", **meta}
        e = {**body, "hash": _h(body)}
        if f is not None:
            f.write(json.dumps(e, sort_keys=True) + "\n")
            f.flush()
        self.entries.append(e)


def _h(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


# Backwards-compatible name used by earlier callers: an in-memory ledger for one scope.
@dataclass
class PrivacyBudget:
    epsilon_limit: float
    delta: float = 1e-6
    charged: list[float] = field(default_factory=list)

    def spent(self) -> float:
        return rdp_epsilon(self.charged, self.delta)

    def can_run(self, multiplier: float) -> bool:
        return rdp_epsilon([*self.charged, multiplier], self.delta) <= self.epsilon_limit

    def charge(self, planned: int, survivors: int, multiplier: float, colluders: int = 0) -> None:
        self.charged.append(effective_multiplier(multiplier, planned, survivors, colluders))
