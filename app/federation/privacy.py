"""Update clipping, distributed Gaussian noise and privacy-budget accounting.

Privacy unit: the participating institution (institution-level differential
privacy of each round's contribution). Each agent clips its contribution to
L2 norm C and adds Gaussian noise N(0, (z C)^2 / m), so the securely aggregated
sum of m contributions carries N(0, (z C)^2): the coordinator only ever sees the
noised sum. The accountant composes rounds with Renyi DP for the Gaussian
mechanism (no subsampling amplification is claimed) and refuses rounds that
would exceed the budget.

Limits that remain (stated, not hidden): if fewer than m agents survive a round
the realised noise is smaller than planned (the accountant charges the
survivor-based multiplier); institution-level DP does not bound what an
institution's own model reveals about its own borrowers; federated learning
without DP and secure aggregation provides no formal privacy guarantee.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

ORDERS = (1.25, 1.5, 2, 3, 4, 6, 8, 12, 16, 24, 32, 48, 64, 128, 256)


def clip(v: np.ndarray, c: float) -> tuple[np.ndarray, bool]:
    n = float(np.linalg.norm(v))
    return (v * (c / n), True) if n > c > 0 else (v, False)


def agent_noise(dim: int, clip_norm: float, multiplier: float, planned: int,
                rng: np.random.Generator) -> np.ndarray:
    if multiplier <= 0:
        return np.zeros(dim)
    return rng.normal(0.0, multiplier * clip_norm / math.sqrt(max(planned, 1)), dim)


def epsilon(multiplier: float, rounds: int, delta: float) -> float:
    if multiplier <= 0:
        return math.inf
    if rounds == 0:
        return 0.0
    return min(rounds * a / (2 * multiplier ** 2) + math.log(1 / delta) / (a - 1) for a in ORDERS)


@dataclass
class PrivacyBudget:
    epsilon_limit: float
    delta: float = 1e-6
    charged: list[float] = field(default_factory=list)     # effective multiplier per round

    def spent(self) -> float:
        if not self.charged:
            return 0.0
        return min(sum(a / (2 * z ** 2) for z in self.charged) + math.log(1 / self.delta) / (a - 1)
                   for a in ORDERS) if all(z > 0 for z in self.charged) else math.inf

    def can_run(self, multiplier: float) -> bool:
        trial = PrivacyBudget(self.epsilon_limit, self.delta, [*self.charged, multiplier])
        return trial.spent() <= self.epsilon_limit

    def charge(self, planned: int, survivors: int, multiplier: float) -> None:
        # noise was sized for `planned` agents; with fewer survivors the sum carries less
        self.charged.append(multiplier * math.sqrt(survivors / max(planned, 1)))
