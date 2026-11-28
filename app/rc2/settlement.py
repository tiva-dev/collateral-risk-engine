"""Point-in-time settlement cycles (RC2 research; RC1 keeps its fixed T+n).

Sources (docs/governance/MARKET_ONBOARDING.md):
  US  T+2 until 27 May 2024; T+1 from 28 May 2024 (SEC).
  NGX T+3 until 27 Nov 2025; T+2 from 28 Nov 2025; T+1 from 1 Jun 2026 (CSCS/SEC Nigeria notices).
The cycle that applies is the one in force on the trade date.
"""

from __future__ import annotations

from datetime import date

SCHEDULES: dict[str, tuple[tuple[date, int], ...]] = {
    "US": ((date(1900, 1, 1), 2), (date(2024, 5, 28), 1)),
    "NGX": ((date(1900, 1, 1), 3), (date(2025, 11, 28), 2), (date(2026, 6, 1), 1)),
}
_EXCHANGE_MARKET = {"NYSE": "US", "NASDAQ": "US", "ARCA": "US", "NYSEARCA": "US", "BATS": "US", "AMEX": "US",
                    "IEX": "US", "US": "US", "NGX": "NGX", "NGSE": "NGX", "NSE": "NGX"}


def settlement_days(exchange_or_market: str, trade_date: date) -> int:
    market = _EXCHANGE_MARKET.get(exchange_or_market.upper(), exchange_or_market.upper())
    if market not in SCHEDULES:
        raise ValueError(f"no settlement schedule for {exchange_or_market}")
    days = SCHEDULES[market][0][1]
    for start, n in SCHEDULES[market]:
        if trade_date >= start:
            days = n
    return days
