# Preliminary real-data comparison: findings

**This is research, not validation evidence (ADR-002, ADR-006).**

- **Run:** Research Real-Data Experiments, 45674441279, commit 19bd309.
- **Inputs:** provider run 45673315376, snapshot `4308c6ca…`. All 37 replay inputs were checked against the snapshot, with 0 mismatches. No provider calls were made.
- **Mechanics:** the synthetic study's lifecycle simulator and methods are reused, with the common B-based maintenance standard and ψ = 1.15.
- **Cohorts:** each loan uses 260 sessions of history and a 126-session term, with a new origination every 21 sessions.
- **Sizes:**

  | Market | Retail | Institutional |
  |---|---|---|
  | NGX | ₦50m | ₦2bn |
  | US | $250k | $25m |

- **Loan rates:** 25% for NGN loans and 10% for USD loans.
- **Ceilings:** 0.65 for equities and 0.70 for ETFs. The flat benchmark lends 0.30 on NGX and 0.50 on US.

Loss figures are the total loss as a percentage of total principal over the 126-session term. "Worst" is the largest single-loan loss, also as a percentage of that loan's principal. A liquidation is "unnecessary" when the no-action counterfactual never goes underwater after the first sale.

## Data limits that shape these results

1. **US history is short and the volume understated.** The Alpaca IEX feed starts on 2020-07-27, so the 2020 crash is not covered, and its volume is IEX-only.
2. **NGX liquidity is short.** NGNMarket volume starts on 2025-08-01. Strict NGX cohorts therefore originate only from August 2025: 95–108 loans per size, all in a rising market.
3. **The backfill variant uses future information.** "advbackfill" carries the first observed NGX average daily volume (ADV) back to 2018, so that the 2018–2025 price history can be replayed. That is look-ahead: treat these rows as a stress illustration, not a backtest.
4. **NGX prices are not adjusted for corporate actions.** No one-day move beyond 25% was found, but bonus issues smaller than that may remain as spurious drops.
5. **Some cohorts couldn't be evaluated.** Where B finds a position unsellable within 250 sessions, the cohort is excluded for every method:
   - AIRTELAFRI, 71 cohorts;
   - NESTLE institutional, 85 cohorts;
   - the ₦2bn NGX portfolio, all cohorts.

## Results

### US single names, 638 loans per size, 2020-07 to 2026-05 originations

| Method | Median LTV | Loss events | Loss / principal | Worst | Liquidations | Unnecessary |
|---|---|---|---|---|---|---|
| v0.6.2 | 0.27 | 0 | 0 | 0 | 0 | – |
| v0.7.0 | 0.62 | 6 | 0.039% | 12.7% | 31 | 58% |
| flat | 0.50 | 3 | 0.042% | 16.5% | 20 | 60% |
| A | 0.65 | 13 | 0.081% | 8.8% | 50 | 42% |
| B | 0.65 | 13 | 0.081% | 8.8% | 52 | 44% |
| B_gap | 0.65 | 11 | 0.060% | 7.3% | 35 | 31% |

*Institutional size. Retail shows the same ordering. B_gap retail: 0.032%, against 0.056% for A and B.*

- **The ceiling binds.** The risk-based capacity of A, B and B_gap exceeds the 0.65 ceiling in nearly every cohort, so the ceiling decides the loan size.
- **Where the losses come from.** Mostly 2022, in TSLA and NVDA.
- **B_gap at the same LTV.** It cuts losses by about a quarter, liquidations by a third and the unnecessary share from 44% to 31%. Its worst loss is the smallest of all the methods that lend above 0.5.
- **US portfolios (58 cohorts).** No method made a loss or a liquidation.

### NGX single names, strict liquidity, Aug 2025+ originations

- **Results.** One loss event (BUACEMENT, 2026) under A, B, B_gap and v0.7.0; otherwise none.
- **Limits.** The period is too short and too calm to tell the methods apart.

### NGX single names, ADV backfill (look-ahead), 2018–2026, retail

| Method | Median LTV | Loss events | Loss / principal | Worst |
|---|---|---|---|---|
| v0.6.2 | 0.26 | 0 | 0 | 0 |
| flat | 0.30 | 0 | 0 | 0 |
| v0.7.0 | 0.55 | 11 | 0.041% | 11.8% |
| A | 0.65 | 46 | 0.258% | 23.1% |
| B | 0.65 | 45 | 0.260% | 23.1% |
| B_gap | 0.65 | 40 | 0.292% | 23.1% |

- **Where the losses come from.** Concentrated in 2019–2020 originations, in ZENITHBANK, GTCO, FIRSTHOLDCO and OANDO.
- **Institutional size.** At 0.41 LTV, B_gap loses 0.106%, about the same as B at 0.65 (0.110%).

### NGX portfolio as a USD loan through the 2023–24 devaluations (retail, backfill, Alpha Vantage USD/NGN)

- **Losses.** None under any method.
- **Liquidations.** Between 7 and 15 per method, mostly unnecessary.

## What this means for the methodology recommendation

1. **US.** The real data supports the synthetic finding: at the same LTV, B_gap has the best loss and liquidation trade-off. The client ceiling, not the model, sets US capacity.
2. **NGX.** The real data contradicts it:
   - **The ceiling decides the loan.** The B-family models put capacity at or above the 0.65 ceiling for NGX single names, and lending at 0.65 lost 0.26% of principal per six-month term in 2019–20, with single losses up to 23%. The gap term does not help, because the ceiling, not the model, sets the loan size.
   - **Hypothesis.** Daily NGX returns understate multi-day risk: non-trading days and stale prices reduce measured volatility, and the methods' recovery horizon then underestimates the drawdown.
   - **How to address it is a methodology decision:**
     - multi-day return aggregation or a stale-price correction;
     - a lower NGX ceiling;
     - any treatment of NGX price limits, which has not been approved.
3. **What none of this is.** Not validation evidence; the v0.7.0 freeze (ADR-001) is unaffected.
