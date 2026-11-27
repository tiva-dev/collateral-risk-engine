"""Initial loan-book capital allocation across competing opportunities.

Each opportunity offers a concave profit curve in its balance (income is
linear, expected credit cost is convex because E[max(B - C, 0)] is convex in
B). The allocator discretises each curve into increments and solves one linear
programme: maximise total risk-adjusted profit subject to total lending
capital, a per-borrower concentration cap, per-group (sector or market)
concentration caps and each opportunity's own maximum (its appetite-supported
capacity and any institutional limit). With concave curves the LP fills
increments in order, so the solution is the exact optimum of the discretised
problem.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

import numpy as np
from scipy.optimize import linprog


@dataclass(frozen=True)
class Opportunity:
    ref: str
    group: str                         # sector / market for concentration limits
    max_balance: float                 # appetite-supported capacity, after institutional limits
    profit: Callable[[float], float]   # risk-adjusted profit of lending B (concave)


def allocate(opps: Sequence[Opportunity], total_capital: float, max_share_per_borrower: float = 1.0,
             max_share_per_group: dict[str, float] | None = None, steps: int = 40) -> dict[str, Any]:
    rows: list[tuple[int, float, float]] = []   # (opportunity index, increment size, marginal profit)
    for i, o in enumerate(opps):
        if o.max_balance <= 0:
            continue
        grid = np.linspace(0.0, o.max_balance, steps + 1)
        prof = np.array([o.profit(float(b)) for b in grid])
        for k in range(steps):
            rows.append((i, float(grid[k + 1] - grid[k]), float(prof[k + 1] - prof[k])))
    if not rows:
        return {"allocations": {o.ref: 0.0 for o in opps}, "total": 0.0, "profit": 0.0, "status": "no capacity"}
    n = len(rows)
    sizes = np.array([r[1] for r in rows])
    c = -np.array([r[2] for r in rows])            # maximise profit; increments are fractions x in [0, 1]
    a_ub, b_ub = [sizes], [total_capital]
    for i, o in enumerate(opps):
        a_ub.append(np.array([s if r[0] == i else 0.0 for r, s in zip(rows, sizes)]))
        b_ub.append(max_share_per_borrower * total_capital)
    for g, share in (max_share_per_group or {}).items():
        a_ub.append(np.array([s if opps[r[0]].group == g else 0.0 for r, s in zip(rows, sizes)]))
        b_ub.append(share * total_capital)
    res = linprog(c, A_ub=np.vstack(a_ub), b_ub=np.array(b_ub), bounds=[(0, 1)] * n, method="highs")
    if not res.success:
        return {"status": res.message, "allocations": {}, "total": 0.0, "profit": 0.0}
    alloc = {o.ref: 0.0 for o in opps}
    for (i, size, _), x in zip(rows, res.x):
        alloc[opps[i].ref] += float(size * x)
    # Never lend into a negative-margin increment: the LP already leaves those at 0.
    return {"status": "optimal", "allocations": alloc, "total": float(sum(alloc.values())),
            "profit": float(-res.fun), "unused_capital": float(total_capital - sum(alloc.values()))}
