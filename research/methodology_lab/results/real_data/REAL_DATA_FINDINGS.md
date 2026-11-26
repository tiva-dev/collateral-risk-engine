# Real-data methodology comparison: findings (v2)

**Research, not validation evidence (ADR-002, ADR-006).**

## Run

| Item | Value |
|---|---|
| Workflow run | Research Real-Data Experiments 45682584351, commit 6cc7fcf (NGX and US jobs) |
| Inputs | Provider run 45681534542, snapshot `3410aba8…`; all 41 replay inputs matched it (0 mismatches) |
| Provider calls | None |
| Term | 126 sessions |
| Cohort spacing | New loans originated every 21 sessions |
| History used at origination | 260 sessions |
| After maturity | 63-session workout window |
| Maintenance trigger | 1.15 × the method's own capacity |
| Interest rates | NGN 25%; USD 10% |
| Client ceilings | 0.65 for equities, 0.70 for ETFs |

This version fixes the issues raised in the 25 November review:

1. **No future-dependent cohort selection.** Only the past decides whether a loan enters the sample.
2. **No exclusion screen.** Every extreme move stays in the data.
3. **Terminal workout.** Defaults at maturity are worked out by actual sales, not settled instantly.
4. **Each method stands alone.** Each method uses its own monitoring standard. Its rejected and unlendable loans are counted separately and never exclude a loan from another method.
5. **Matched-exposure comparisons** are added.
6. **Frontiers** are added.
7. **Approach C** is included.
8. **NGX variants** are added: variance-ratio volatility (`*_vr`) and stressed liquidity (`*_liq`).

**NGX liquidity before August 2025 cannot be observed.** NGX 2018–2026 is therefore run under two liquidity scenarios fixed in advance: the position takes 5 sessions (`liq5d`) or 20 sessions (`liq20d`) to sell at 10% of daily volume.

## 1. US (Alpaca, July 2020 onward)

**Policy-originated loans** (each method lends its own LTV; 638 single-name loans per size; 58 portfolio loans):

- **No losses.** No method lost money, on single names or portfolios, at either size. The 0.65 ceiling binds for A, B and C. Median LTVs:

  | Method | Median LTV |
  |---|---|
  | A, B | 0.65 |
  | v0.7.0 | 0.62 |
  | Flat | 0.50 |
  | v0.6.2 | 0.27 |

- **Liquidations are frequent and mostly unnecessary.** 29–39% of single-name loans were liquidated, and 86–100% of those liquidations were unnecessary under the no-action counterfactual.

**Matched exposure** (single names, institutional size; share of loans liquidated, every method monitoring the same loan):

| LTV | A, B | B_gap | v0.7.0 | Flat | v0.6.2 |
|---|---|---|---|---|---|
| 0.50 | 6–7% | 14% | 10% | 29% | 100% |
| 0.65 | 29% | 38% | 53% | 100% | 100% |

At 0.65, monitoring with no action at all would have lost 0.21% of principal; every method's own monitoring prevented that loss.

**Frontier** (no client ceiling, single names, institutional size):

| Method | Average capacity | Loss / principal | Worst loss |
|---|---|---|---|
| A, 95–99.9% | 0.82–0.90 | 0.78–2.6% | 16–24% |
| B, 95–99.9% | 0.84–0.90 | 1.16–2.7% | 19–24% |
| B_gap, 95–99.9% | 0.75–0.82 | 0.22–0.68% | 8–16% |
| C, conservative / balanced / commercial (Tesla, NVIDIA subset) | 0.61 / 0.65 / 0.69 | 0 / 0.02% / 0.10% | 0 / 2.4% / 3.5% |

At similar capacity B_gap has the lowest loss (B_gap at 95% vs A at 99.9%: both about 0.82 capacity; 0.68% vs 0.78% loss). Losses appear only above about 0.75 capacity.

**Tesla** (58 six-month cohorts, May 2021 to June 2026):

- **Every method liquidated in 39–42 of the 58 cohorts,** about 70%. That includes v0.6.2 lending at 0.25 and the flat 0.50.
- **Origination LTVs:**

  | Method | LTV at origination |
  |---|---|
  | A, B | 0.65 |
  | B_gap | 0.57 |
  | v0.7.0 | 0.55–0.58 |
  | C balanced | 0.65 |

- **No method lost money or defaulted at maturity on Tesla.** Liquidations ran nearly continuously from November 2021 to May 2026.
- **The cost of lending against Tesla is forced selling, not credit loss.**

## 2. NGX (NGNMarket, 2018 onward; NGN loans)

**Policy-originated single names** (1,089 loans per scenario, 14 names):

| Scenario | Method | Median LTV | Loss / principal | Worst | Loss events (all maturity defaults) | Liquidations (unnecessary) |
|---|---|---|---|---|---|---|
| liq20d | A / B | 0.65 | 0.06% | 10–13% | 9–12 | 60–62% (80%) |
| liq20d | B_gap | 0.48 | 0.009% | 5.4% | 2 | 76% (96%) |
| liq20d | B_gap_vr_liq | 0.33 | 0.012% | 5.4% | 2 | 75% (98%) |
| liq20d | C balanced (subset of 154 loans) | 0.64 | 0.26% | 11.4% | 5 | 55% |
| liq20d | v0.7.0 / flat / v0.6.2 | 0.33 / 0.30 / 0.12 | 0 | 0 | 0 | 48–67% (100%) |
| liq5d | A / B | 0.65 | 0.06–0.07% | 22% | 6–9 | 56–57% |
| liq5d | B_gap | 0.63 | 0.008% | 4.3% | 3 | 68% |
| liq5d | v0.7.0 | 0.48 | 0 | 0 | 0 | 49% |

- **Observed liquidity** (August 2025 onward, 117 loans): no losses for any method. 9–22 positions per method are unlendable at retail and institutional sizes, mostly AIRTELAFRI and NESTLE.
- **Portfolios:** no losses for any method. C rejects 14–16 of 39 portfolio cohorts.

**Frontier** (liq20d, single names, no ceiling):

| Method | Capacity | Loss / principal | Worst loss |
|---|---|---|---|
| A, B | 0.59–0.80 | 0.08–1.6% | 18–42% |
| B_gap | 0.43–0.62 | 0.42–0.48% | 39–41% |
| C conservative / balanced | 0.55 / 0.59 | 0.26% / 0.70% | 13% / 18% |
| Flat | up to 0.50 | 0 | 0 |
| Flat | 0.60 | 0.014% | 11% |
| Flat | 0.70 | 0.24% | 27% |

Without the ceiling, NGX tails are much heavier than US tails. B_gap's loss rate stays around 0.4–0.5% whatever its confidence level, so the worst single NGX losses are events these models don't capture.

**Variance-ratio adjustment (`*_vr`)** for stale, step-wise prices: it changes NGX results only marginally, with B_gap_vr close to B_gap. One-day NGX moves cluster at exactly ±10%, but scaling volatility for autocorrelation does not remove the tail losses. Those come from discrete gaps and the maturity workout, not from understated diffusion.

**C under-predicts its own losses here, as in the synthetic study.** The balanced appetite permits 0.05% expected loss and realises 0.26–0.70%.

**USD loans on the NGX portfolio** (liq20d), by USD/NGN source:

| USD/NGN source | Cohorts | Losses | B_gap capacity | A, B capacity |
|---|---|---|---|---|
| Alpha Vantage | 39 | None | 0.18 | 0.55 |
| CBN official | 39 | None | 0.23 | 0.55 |
| NGNMarket | 19 (from Oct 2023 only) | None | 0.65 | 0.65 |

The choice of rate changes origination capacity materially and changes realised loss not at all in this window.

## 3. What this means for selecting a methodology

1. **Lending capacity is decided by the client ceiling.** At 0.65 every risk-based method lends the ceiling on liquid US names and almost all NGX names. The methods then differ mainly in how often they force sales.
2. **Within the ceiling, the trade-off is forced sales against tail loss:**
   - US: A and B liquidate least (29% at a matched 0.65, against 53% for v0.7.0 and 100% for flat) with zero loss.
   - NGX: A and B carry 10–22% worst-case losses at 0.65. B_gap cuts NGX losses by about 85% but liquidates 76–96% of the time, mostly unnecessarily.
3. **Neither the synthetic recommendation (B_gap) nor any single method dominates on real data.** The evidence points to:
   - a market-specific ceiling or appetite (NGX far below the US);
   - risk-based monitoring with explicit headroom, to cut unnecessary liquidations;
   - a separate treatment of discrete NGX gap risk, which no model here estimates well.

   Choosing the NGX appetite and ceiling is Tiva's decision.
4. **Limits on this evidence:**
   - US history starts after the 2020 crash.
   - NGX liquidity before August 2025 is a scenario, not data.
   - No NGX corporate action is verified yet; 11 drops are disclosure candidates, kept in the data.
   - US volume comes from the IEX feed only.
   - 2022 dominates US stress.
