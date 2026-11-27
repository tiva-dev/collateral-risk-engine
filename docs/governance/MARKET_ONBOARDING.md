# Standard market-onboarding process

A market (an exchange or group of venues sharing rules, currency and data) is
onboarded through the same eight gates. A market that has not passed every gate
has **no lending value** in any CRI version: holdings on it are reported, may be
sold in a recovery, but never add capacity (`profile_for_exchange` returns
nothing, and the engine records "market not onboarded").

Onboarding a market never changes a frozen release candidate. A profile added or
changed after a freeze enters the next candidate (RC*n*+1) with its own manifest.

| Gate | Requirement | Evidence recorded |
|---|---|---|
| 1. Identity | Venue codes, currency, timezone, session hours; stable instrument identity `exchange:symbol:currency:type` | Profile fields; sources cited in `provenance` |
| 2. Rules | Settlement cycle **with effective dates** (point in time), daily price limits, trading halts and suspension practice, lot sizes | Rulebook / CSD notices cited as `configured: <source, date>`; any value not yet confirmed marked `assumption` |
| 3. Calendar | Exchange holiday calendar for staleness rules and session counting; unscheduled closures recorded as they occur | `app/historical_data/calendars.py` entry; tests for known holidays |
| 4. Data coverage | Daily bars (OHLC, reported volume) from a licensed source; coverage by year; gaps and stale shares measured | Coverage report from `app/historical_data/coverage.py`; provider and licence named |
| 5. Liquidity | Observed volume history long enough for ADV and participation; where volume is missing, the stated assumption and its dates | `volume_reported_from`; every result using an assumption carries its label |
| 6. Currency | Convertibility classification; loans only in the collateral currency until cross-currency risk is independently validated | `currency_convertibility`; cross-currency accounts restricted |
| 7. Validation | Lifecycle validation on the market's own history (previously examined tier) and its synthetic stress set, under the current release candidate, reported separately from other markets | Run ids, dataset hashes, results; limitations named |
| 8. Approval | Tiva records the onboarding decision; the profile version and its provenance enter the next release manifest | ADR in `docs/governance/DECISIONS.md` |

## Current status

| Market | Gates passed | Open items |
|---|---|---|
| US (NYSE, NASDAQ, ARCA, BATS, AMEX, IEX) | 1–7 for shadow evaluation | Participation 10 % and spread floor are assumptions; IEX-only volume in early provider runs (SIP used for RC1 evidence) |
| NGX | 1–7 for shadow evaluation | **Settlement:** T+3 until 27 Nov 2025, T+2 28 Nov 2025 – 29 May 2026, **T+1 from 1 Jun 2026** (CSCS/SEC notices); RC1 uses a fixed T+2 (one session optimistic before Nov 2025, one session conservative since Jun 2026) — point-in-time settlement is an RC2 item. Volume reported only from 1 Aug 2025 (earlier: stated assumption). Daily ±10 % limit used only as an execution constraint; NGX-specific limit modelling not approved. No executable USD/NGN source. |

Neither market has passed gate 8 for lending: no market is approved for actual
lending while RC1 is advisory and shadow-only.
