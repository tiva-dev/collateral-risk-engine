# FX sources: what each rate is

Recorded 25 November 2026. This separates **reference rates** (what an
official publication says the rate was) from **executable conversion rates**
(what a lender could actually obtain when converting liquidation proceeds).
CRI needs both: valuation and reporting can use a reference rate, but
recovery in another currency happens at an executable rate, and the gap is
a risk.

| Source | Series | Economic definition | Publication | Rate kind in CRI | History available |
|---|---|---|---|---|---|
| CBN (`/api/GetAllExchangeRates`) | Buying / central / selling, NGN per unit, by currency | Official CBN rates. Since the June 2023 unification the CBN states that the NFEM rate, a volume-weighted average, "stands as the official exchange rate for the day". Before that, an administered official rate. | After the trading day, CBN business days | `official_reference` | USD from 2001-12-10 (about 245 days a year); free, no key |
| CBN NFEM table | NFEM rate (VWAP), high, low, closing, simple average, turnover | Official NFEM market statistics | After the trading day | `official_reference` | JSON graph endpoint holds only recent weeks; the history table is not yet integrated |
| FMDQ FX Closing Rate (formerly NAFEM Closing Rate, renamed 12 Dec 2024) | USD/NGN closing rate | FMDQ benchmark: VWAP in observation windows; falls back to best bid/offer midpoint or the previous close | Daily, at set times | (not integrated) | Paid end-of-day licence |
| NGNMarket `/forex/history` | `rate` by `rate_date`, every calendar day | Provider's own feed; documentation says rates "reflect the latest available market close". No named fixing or source. | Row `created_at` shortly after midnight | `market_indicative` | From 2023-10-18 only |
| Alpha Vantage `FX_DAILY` | Daily OHLC | Provider's daily series "updated realtime"; source and fixing time undocumented | Unspecified | `market_indicative` | USD/NGN from 2018 with 74 gaps; free key is non-commercial |

Observed (run 45673315376, 800 common dates): NGNMarket and Alpha Vantage
USD/NGN differ by a median 1.5% and up to 33%, with a daily-change
correlation of 0.016. They are not the same rate, and neither documents a
fixing. The CBN series is now fetched in every provider run and reconciled
against both (see the manifest's `fx_reconciliation`).

## Executable conversion rates

No source here is an executable dealer quote. For 2018 to June 2023, when
the official rate was administered and most conversions happened in the
I&E window or the parallel market, the official rate is **not** a valid
proxy for the rate at which proceeds could be converted. Two consequences:

1. A USD loan against NGN collateral must not be valued at the CBN official
   rate for 2018–2023 as if it were achievable.
2. The conversion risk is the spread between the reference rate and the
   achievable rate, plus the move between liquidation and settlement. The
   research experiments treat FX through market-indicative series and label
   which one; the choice of reference rate for valuation, and of the
   executable proxy for recovery, remains a Tiva decision.
