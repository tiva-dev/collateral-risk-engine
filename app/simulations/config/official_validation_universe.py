from __future__ import annotations

from datetime import date

LOAN_CURRENCIES = ["NGN", "USD", "EUR"]
US_UNIVERSE = [
    "SPY",
    "QQQ",
    "IWM",
    "TLT",
    "HYG",
    "AAPL",
    "MSFT",
    "NVDA",
    "TSLA",
    "JPM",
    "XOM",
]
NGX_UNIVERSE = [
    "MTNN",
    "AIRTELAFRI",
    "DANGCEM",
    "BUACEMENT",
    "BUAFOODS",
    "GTCO",
    "ZENITHBANK",
    "ACCESSCORP",
    "UBA",
    "FBNH",
    "SEPLAT",
    "OANDO",
    "NESTLE",
    "NB",
    "DANGSUGAR",
]
FX_PAIRS = ["USD/NGN", "NGN/USD", "EUR/USD", "USD/EUR", "EUR/NGN", "NGN/EUR"]
START_DATE = date(2018, 1, 1)
END_DATE = None


def official_universe():
    return {
        "loan_currencies": LOAN_CURRENCIES,
        "us_universe": US_UNIVERSE,
        "ngx_universe": NGX_UNIVERSE,
        "fx_pairs": FX_PAIRS,
        "start_date": START_DATE,
        "end_date": END_DATE,
    }
