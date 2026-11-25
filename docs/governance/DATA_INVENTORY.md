# Historical Data Inventory and Coverage Gaps

Recorded 25 November 2026. Source: Data Inventory workflow (runs 45667659308,
45667858003) and the full-window provider run 45669690566 on
`data/coverage-provenance-fx` (window 2018-01-01 to 2026-11-24, NGNMarket
Hobby plan, Alpaca IEX feed, Alpha Vantage free key).

## 1. What exists

| Location | Content | Status |
|---|---|---|
| Actions artifact, run 30455533912 (v0.6.2 evidence, July 2026) | Official evidence package | **Expired**; cannot be re-verified |
| Artifact, run 34000303563 (6 Sep 2026, `main`) | Alpaca US equities and Alpha Vantage FX, 2025-09-05 to 2026-09-04 (252 / 261 rows); NGX empty | Expires 5 Dec 2026 |
| Artifact, run 34000523667 (6 Sep 2026, v0.7.0 branch) | 403 PLAN_REQUIRED responses only; inverted window | Expires 5 Dec 2026; no usable data |
| Artifact, run 45665911547 (25 Nov 2026) | NGX 15 names x 38 sessions, USD/NGN 55 days | Expires 23 Feb 2027 |
| Artifacts, run 45669690566 (25 Nov 2026) | Full window, all providers; snapshot `2cbfe971849c35fc11268ba941a8b66dc955022240b2d6d588bcaeab90716f58`, 70 files, 89.5 MB | Expires 23 Feb 2027 |
| Artifacts, run 45673315376 (25 Nov 2026) | Same window with volume provenance; snapshot `4308c6ca57eb19c59247827574db842dca391e01b419b3491770dec78b109bcf`, 70 files, 92.1 MB, verified | Expires 23 Feb 2027; used by the research run |
| Actions caches | One entry, 66 KB (`official-provider-cache-v1-2026-10-01-2026-11-24`) | Not a durable store |

No historical data is committed to git, by design (licensing).

## 2. Coverage of the full window (run 45669690566)

Calendar-aware verdicts (`app/historical_data/coverage.py`). "Expected" counts
sessions from the later of 2018-01-01 and the listing date.

| Series | First obs | Coverage | Verdict | Cause |
|---|---|---|---|---|
| 14 NGX names (all except NB) | 2018-01-02 or listing | 98.3%–100% | warn | Short gaps, unverified listing dates, provider rows on holidays |
| NB | 2018-01-01 | 80.2% | **fail** | 408 gaps; 1,698 of 1,827 bars carry only a close |
| 11 US names (Alpaca IEX) | 2020-07-27 (SPY 2018-11-01) | 71.2% | **fail** | IEX history starts mid-2020: 645 sessions missing, including the March 2020 crash; SPY also has a 433-session hole |
| USD/NGN, EUR/NGN (NGNMarket) | 2023-10-18 | 34.9% | **fail** | History starts 2023-10-18 on any request; 2,116 calendar days missing, including the June 2023 unification |
| USD/NGN, EUR/NGN (Alpha Vantage) | 2018-01-02 | 92.4% | **fail** | 74 gaps, longest 17 sessions (Aug–Sep 2018) |
| EUR/USD, USD/EUR (Alpha Vantage) | 2018-01-01 | 100% | pass | — |

NGNMarket's pricing page states two years of chart history on Hobby; the
equity endpoint actually returned history from 2018. **An NGNMarket upgrade
is not needed for NGX equity depth.** NGNMarket's FX start date
(2023-10-18) matches no plan depth (two years would be 2024-11, five years
2021-11), so an upgrade is unlikely to extend FX history. Ask NGNMarket
support before paying for one.

## 3. Source disagreement: USD/NGN

On 800 common dates, NGNMarket and Alpha Vantage USD/NGN differ by a median
1.5%; 514 days differ by more than 1%, 70 by more than 5%, and the largest
gap is 33% (2024-01-31, during the devaluation). The correlation of their
daily log changes is 0.016: day-to-day moves are essentially unrelated.
They are not the same rate. Which NGN reference rate governs valuation is a
Tiva decision (ROADMAP); until it is made, FX precedence in
`data_builder.FX_SOURCE_PRECEDENCE` is provisional.

## 4. Other data-quality findings

* **NGX liquidity history is short.** NGNMarket rows carry `volume`,
  `value_traded` and `trade_count` keys, but the values are null before
  2025-08-01 for 12 of the 15 names (SEPLAT before 2026-07-21, NB before
  2026-03-23); 77%–96% of each series has no volume. Price history from
  2018 is usable; liquidity (ADV, execution horizon) can be measured from
  August 2025 only. The parser previously turned these nulls into a volume
  of 0.0; it now records them as missing.
* **US volume is IEX-only.** Alpaca's IEX feed reports volume on the IEX
  venue only (SPY shows 200 shares on 2018-11-01), a small fraction of
  consolidated volume. ADV from it is a lower bound; the SIP feed would fix
  both this and the 2018–2020 gap.

* **OHLC imputation (NGX).** NGNMarket leaves high/low empty on 25%–60% of
  bars for most names (NB 93%). Each imputed field is now labelled; range-based
  estimators must use `has_reported_range()`.
* **Predecessor history.** GTCO's series starts 2018-01-02, before the
  holding company listed (24 June 2021): the provider has spliced Guaranty
  Trust Bank history under the new ticker. ACCESSCORP starts 2022-03-14,
  before its 28 March 2022 listing. Spliced history is usable only if the
  1:1 exchange is accepted as continuous; this is recorded, not decided.
* **Rows on closed days.** NESTLE, SEPLAT, UBA and ZENITHBANK have 41–44 rows
  on dates the NGX calendar marks closed. These look like carried prices and
  must not count as trading days.
* **Inferred NGX closures.** 24 market-wide closures inferred from the
  cross-section, matching Eid al-Fitr, Eid al-Adha, Mawlid and special
  holidays (e.g. 2019-05-29, 2024-04-09/10, 2026-03-19/20). Earlier Mawlid
  dates were not inferred because some names carry rows on them.
* **Alpha Vantage key.** Free keys are for non-commercial use under Alpha
  Vantage's terms; using them for lender validation evidence needs review.

## 5. Remaining gaps and what closes them

| Gap | Sessions missing | Closes it | Decision owner |
|---|---|---|---|
| US equities 2018-01 to 2020-07 (incl. COVID crash) | 645 per name | Alpaca SIP feed (paid plan) or another licensed source | Tiva (expenditure) |
| NGN FX 2018-01 to 2023-10 from NGNMarket | 2,116 days | Alpha Vantage already covers it, but disagrees; or CBN/FMDQ published rates | Tiva (reference rate) |
| Alpha Vantage NGN gaps | 177 sessions | Second source for the same rate | Tiva |
| NB | 20% of sessions | Exclude NB, or another NGX source | Engineering proposes exclusion |
| Unverified listing dates | MTNN, AIRTELAFRI, BUACEMENT, FIRSTHOLDCO | Check NGX bulletins | Engineering |
| NGX volume before 2025-08-01 | 77%–96% of bars per name | NGX daily official list (value and volume) or another vendor; NGNMarket support may know if older volume exists | Tiva (source/expenditure) |
| US consolidated volume | all US bars | Alpaca SIP feed | Tiva (expenditure) |

## 6. Storage

Each provider run now writes a content-addressed snapshot (manifest,
normalized series, raw responses, SHA-256 per file, snapshot id over the
listing) and verifies it (`python -m app.historical_data.snapshot`). The
snapshot is an Actions artifact retained for 90 days. Metadata-only ledger
entries are committed in `data/ledger/snapshots.jsonl`. A durable store for
the snapshots themselves (beyond 90 days), consistent with each provider's
terms, is a Tiva decision.
