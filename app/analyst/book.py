"""Loan-book access for the Analyst: a narrow, read-only, tenant-bound port.

The Analyst never sees a database handle. A ``BookSource`` is constructed for
exactly one tenant by the gateway and exposes read-only views of that tenant's
monitored accounts and the inputs RC1 needs to evaluate them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol

import numpy as np

from app.core.enums import AssetType
from app.core.models import Holding, Loan, MarketData, Policy
from app.credit.interest import InterestPolicy
from app.liquidation.policy import LiquidationExecutionPolicy


@dataclass(frozen=True)
class AccountView:
    account_ref: str
    holdings: list[Holding]
    loan: Loan
    policy: Policy
    interest_policy: InterestPolicy
    execution_policy: LiquidationExecutionPolicy
    market_data: dict[str, MarketData]
    bars: dict[str, list[dict[str, Any]]]
    as_of: datetime


class BookSource(Protocol):
    tenant_id: str

    def account_refs(self) -> list[str]: ...
    def account(self, ref: str) -> AccountView: ...
    def sector_of(self) -> dict[str, str]: ...


@dataclass
class InMemoryBook:
    """Tenant-bound in-memory book (sandbox, tests, demonstrations)."""

    tenant_id: str
    accounts: dict[str, AccountView]
    sectors: dict[str, str] = field(default_factory=dict)

    def account_refs(self) -> list[str]:
        return sorted(self.accounts)

    def account(self, ref: str) -> AccountView:
        if ref not in self.accounts:
            raise KeyError(f"unknown account {ref!r} for this tenant")
        return self.accounts[ref]

    def sector_of(self) -> dict[str, str]:
        return dict(self.sectors)


# ------------------------------------------------------------ synthetic book

_SYN = {  # symbol: (exchange, currency, sector, daily vol, price, ADV)
    "TECA": ("NASDAQ", "USD", "technology", 0.028, 180.0, 4e6),
    "TECB": ("NASDAQ", "USD", "technology", 0.034, 95.0, 2.5e6),
    "BNKA": ("NYSE", "USD", "financials", 0.018, 52.0, 3e6),
    "ENRA": ("NYSE", "USD", "energy", 0.022, 74.0, 2e6),
    "IDXF": ("NYSE", "USD", "index_fund", 0.011, 450.0, 6e7),
}


def _bars(seed: int, n: int, vol: float, p0: float, adv: float, end: date) -> list[dict[str, Any]]:
    rng = np.random.default_rng(seed)
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    p, out = p0, []
    for i, day in enumerate(days):
        p *= float(np.exp(rng.standard_t(4) * vol * np.sqrt(0.5)))
        out.append({"bar_date": day, "open": p, "high": p * 1.01, "low": p * 0.99, "close": p,
                    "volume": adv * float(np.exp(rng.normal(0, 0.25))), "range_reported": True,
                    "volume_reported": True})
    return out


def synthetic_book(tenant_id: str = "sandbox-demo", end: date = date(2026, 11, 20), seed: int = 7) -> InMemoryBook:
    """Five synthetic margin-lending accounts with different concentrations.

    Synthetic and labelled as such everywhere it is used (sandbox evidence
    class); prices and histories are simulated, not market data."""
    from app.candidate.engine import data_key

    rng = np.random.default_rng(seed)
    asof = datetime.combine(end, time(21), UTC)
    bars = {s: _bars(seed + i, 420, v, p, adv, end) for i, (s, (_, _, _, v, p, adv)) in enumerate(_SYN.items())}
    mix = {  # account: ({symbol: quantity}, cash, loan balance)
        "ACC-001": ({"TECA": 3000, "TECB": 4000}, 20_000.0, 420_000.0),     # concentrated technology
        "ACC-002": ({"BNKA": 8000, "ENRA": 5000}, 50_000.0, 300_000.0),
        "ACC-003": ({"IDXF": 1500}, 0.0, 380_000.0),                         # index fund
        "ACC-004": ({"TECA": 1000, "BNKA": 4000, "IDXF": 400}, 30_000.0, 260_000.0),
        "ACC-005": ({"TECB": 9000}, 0.0, 520_000.0),                         # single-name technology, high LTV
    }
    accounts = {}
    for ref, (pos, cash, bal) in mix.items():
        hs, md, bx = [], {}, {}
        for sym, q in pos.items():
            ex, ccy, _, _, _, adv = _SYN[sym]
            h = Holding(sym, AssetType.ETF if sym == "IDXF" else AssetType.LISTED_EQUITY, float(q), ccy, ex)
            last = bars[sym][-1]["close"]
            spread = 0.0005 if sym == "IDXF" else 0.001 + 0.001 * float(rng.random())
            md[h.stable_key] = MarketData(sym, last, bid=last * (1 - spread), ask=last * (1 + spread), timestamp=asof)
            bx[data_key(h)] = bars[sym]
            hs.append(h)
        if cash:
            hs.append(Holding("USD", AssetType.CASH, cash, "USD", "CASH"))
        accounts[ref] = AccountView(ref, hs, Loan(bal), Policy.default(), InterestPolicy(0.16),
                                    LiquidationExecutionPolicy(), md, bx, asof)
    return InMemoryBook(tenant_id, accounts, {s: v[2] for s, v in _SYN.items()})
