# CRI v1.0 Release Candidate 1 — methodology specification

| | |
|---|---|
| Release candidate | `CRI-1.0.0-rc1` |
| Engine | `candidate-2026.11.8` (`app/candidate/`) |
| Frozen configuration | `app/candidate/rc.py`, exported to `CRI_v1.0_RC1_config.json` |
| Configuration fingerprint | `17534330c290f443fed44c7442560b967a017469ec6db47d5fd3c3ba146bf9f5` |
| Frozen code revision | git tag `cri-1.0.0-rc1` (see report 13 for the commit) |
| Status | **Advisory, shadow only.** Not approved methodology. Does not drive account state, margin events, webhooks, orders, lending or liquidation. |
| Owner of risk appetite and production acceptance | Tiva |

Everything below is implemented exactly as written; where the code and this
document disagree, the code is authoritative and the disagreement is a defect.

## 1. What CRI decides

For each account, using only information available on the decision date, CRI
computes five balance levels from one scenario set and classifies the current
balance `B` (principal, accrued interest and fees) against them.

| Level | Definition | Confidence / horizon |
|---|---|---|
| Initial capacity `B0` | Largest balance from which the probability of the balance reaching the margin-call level at any session within `h` sessions is at most `1 − q_h` (first passage), then capped by the client's LTV ceilings | `q_h` = 95 %, `h` = 5 sessions |
| Approved credit | `u · B0` | utilisation `u` = 0.925 |
| Early warning `B_ew` | A one-session move at `q_ew` would reach `B_mc` | `q_ew` = 90 % |
| Margin call `B_mc` | Largest balance at which waiting the grace period and then selling still covers the obligation in expectation over the worst `1 − q_mc` of scenarios (expected shortfall) | `q_mc` = 99 % |
| Forced recovery `B_f` | Same, selling after the liquidation delay only | `q_f` = 95 % |
| Restoration target | Uncapped `B0` of the remaining lendable collateral | — |

States: `B ≤ B0` safe; `≤ B_ew` restricted (no new borrowing); `≤ B_mc`
watch; `≤ B_f` margin call (grace, cure to the restoration target, otherwise
the smallest partial sale that restores it); above `B_f` forced recovery (no
grace). Client LTV limits are **ceilings**: they can only lower `B0`; the client
never sets participation rates above the market profile's cap.

## 2. Formulas

### 2.1 Coverage levels

`B*(q, d) = ES_q[(C + P(d)) / g(d + T_exec + T_settle)]`, where

* `C` pledged cash in the loan currency (never sold);
* `P(d)` net sale proceeds on a scenario path when selling starts `d` sessions
  from now: each instrument sells at most `π · V_t` shares per session (`π` the
  participation cap, `V_t` the path's simulated session volume), at the path's
  bid less the stressed extra half-spread `s/2 · (m_s − 1)`, less square-root
  impact `k · σ_t · sqrt(x_t / V_t)` and the fee; limit-locked and no-fill
  sessions do not trade; quantity unsold within `max_exit_sessions` counts zero;
* `g(T)` contractual growth of the obligation over `T` sessions (capitalisation
  schedule, day count, outstanding accrued interest: `accrue_scheduled_periods`);
* `ES_q` the mean over the worst `1 − q` of paths of the coverage ratio, applied
  as a balance level;
* `d = 1 + grace` for `B_mc`, `1 + delay` for `B_f`; `B_f ≥ B_mc` by construction.

### 2.2 Initial capacity (first passage)

`B0 = sup{B : P(∃ t ≤ h : B · g(t) > B_mc(t)) ≤ 1 − q_h}` where the
margin-call level evolves along each path: `B_mc(t) = C' + V0 · M_t · (1 − H_t)`,
`M_t` the path's collateral value multiple, and the haircut
`H_t = H_0 · (σ_t/σ_0) · sqrt(1/ν_t)` rises with the path's volatility and with
falling volume (`ν_t` volume multiplier). A concentration add-on is **not**
applied (evidence: §6.3).

### 2.3 Scenarios

Filtered historical simulation per instrument, identical in every market:

* volatility `σ = max(EWMA_0.94, long-run) · sqrt(VR(10))` (the variance ratio
  corrects stale prices and partial adjustment);
* partial pooling of log volatility toward the market group prior (instruments
  with ≥ 250 sessions, point in time); risk uses the one-sided credible bound
  `exp(μ_post + z · sd_post)`, `z = 1`;
* standardised residuals bootstrapped **jointly by date** when ≥ 60 common dates
  exist, otherwise comonotonically; the empirical tail is weighted by residual
  count (full at 250), the rest is a Student-t(4) tail by quantile mapping;
* stressed liquidity: AR(1) log volume (persistence 0.7, sd 0.35) falling by
  2 × the portfolio drawdown (floor 5 %), no-fill probability 0.5 × the
  observed stale share, random suspensions (floor 0.1 %/session, mean 3
  sessions) with catch-up on resumption, profile daily price limits as an
  execution constraint.

**Regime conditioning (RC1).** Three asset-driven indicators are computed from
observed data: current liquidity `L` = median volume of the last 5 sessions /
ADV (value-weighted); 5-session move `M` in portfolio sigmas; correlation shift
`ΔR` = mean pairwise correlation over 20 sessions minus over 250. The account is
*stressed* when at least two of `L < 0.5`, `M < −2`, `ΔR > 0.2` hold. When
stressed, the joint bootstrap draws the last 60 sessions with weight 5 (current
volatility and dependence), and execution capacity uses current liquidity
(`π · ADV · clip(L, 0.05, 1)`). No geographic haircut or market ceiling exists.

### 2.4 Suspended instruments (RC1: delayed valuation)

A confirmed or suspected suspension is never immediately sellable and never
has lending value. It joins the scenario set as a leg that cannot trade until
its modelled resumption `t_res = min(40, max(5, mean historical gap spell,
current lag))` sessions; from then it sells within execution capacity at the
same costs, settles, and accrues interest. Its recovery enters `B_mc` and `B_f`
**jointly** with the other collateral on the same paths (not additively). An
instrument without trades for more than 60 sessions is impaired (value 0).

### 2.5 Data rules

Material missing, stale (quote older than 1 exchange session, last bar older
than 2 exchange sessions, measured on the exchange holiday calendar) or
unreliable data blocks additional borrowing. A breach that disappears on the
latest reliable data is *data deterioration*: no sale, escalation. A missing
price always holds the account for review. Collateral in another currency than
the loan restricts the account (capacity 0).

### 2.6 Recovery

Margin calls and forced recovery are sized by the unified planner
(`app/liquidation/planning.plan_recovery`): the smallest sale, over priority
and pro-rata orderings, after which the residual obligation with interest to
settlement is within the restoration target. Orders execute from the next
session at the session's typical price within participation limits; only
settled proceeds reduce the debt.

## 3. Parameters and provenance

| Parameter | Value | Status |
|---|---|---|
| `q_mc`, `q_f`, `q_h`, `q_ew` | 99 %, 95 %, 95 %, 90 % | Approved research defaults (26 Nov); risk appetite unapproved |
| Headroom `h` | 5 sessions, fixed | Approved research default; adaptive form rejected for RC (§6.3) |
| Restoration | initial capacity, current volatility | Approved default; long-run alternative retained for research |
| Utilisation `u` | 0.925 | Proposal inside the authorised 0.85–1.0 band (§6.4) |
| Suspension value | delayed (§2.4) | Research default (27 Nov) |
| Regime conditioning | on, 2-of-3 rule, 60 sessions × 5 | Research; thresholds are stated assumptions, not fitted to validation data |
| Stress-grace rule | off | Tested; never triggered at 5 % (§6.2) |
| Volatility / tail | EWMA 0.94, VR(10), z = 1, t(4), full tail at 250 | Assumptions carried from v2026.11.x |
| Liquidity model | AR(1) 0.7 / 0.35, elasticity 2, floor 0.05 | Assumptions |
| Impact `k`, spread stress | 0.7, 1.5 × | Assumptions |
| Participation cap | 10 % of ADV (profile); client may lower | Assumption (both markets) |
| Settlement | US T+1 (configured: SEC rule); NGX T+2 | NGX to be confirmed against CSCS rule |
| NGX price limit | ±10 % | Observed in data; used only as a generic execution constraint; NGX-specific modelling not approved |
| NGX volume before 1 Aug 2025 | assumption: sells in 20 sessions at 10 % | Labelled in every result that uses it |

Full values: `CRI_v1.0_RC1_config.json`.

## 4. Supported and unsupported cases

Supported (shadow evaluation): long-only listed equities and ETFs on venues with
an onboarded market profile (US: NYSE, NASDAQ, ARCA, BATS, AMEX, IEX; NGX),
cash in the loan currency, daily bars with reported volume, single-currency
accounts, scheduled interest (simple or compound, any capitalisation schedule).

Unsupported: cross-currency collateral (restricted), derivatives, short
positions, bonds and funds without exchange bars, intraday monitoring,
venues without a profile (no lending value), borrower behaviour beyond a cure
probability, instruments with < 20 sessions or data quality < 0.5 (no lending
value), autonomous liquidation, actual lending.

## 5. Validation method

* Lifecycle validation (`app/candidate/lifecycle.py`): loans originated every
  21 sessions, run to maturity with contractual interest, warnings, margin
  calls, cures (probability 0.5, also 0), partial and forced liquidation from
  the next session, participation limits, settlement, and maturity repayment,
  default, partial repayment or refinance with workout.
* Matched exposure (same principal for all policies, common random numbers)
  and policy-originated comparisons (each policy lends its own capacity).
* Outcomes: completed loans have observed loss; loans with unsold collateral
  or unsettled proceeds at the cutoff are **censored** (loss not observed) and
  counted separately; pending proceeds are never realised recovery. The
  conservative upper bound counts each censored loan's entire outstanding debt
  as lost.
* Inference: loans originated in the same quarter share shocks, so the unit is
  the quarter cluster. With `k` of `n` clusters containing a loss, the reported
  bound is the exact one-sided 95 % Clopper–Pearson bound; with zero events it
  equals `1 − 0.05^(1/n)`. No per-loan rule-of-three claim is made. Intervals
  for rates resample quarter clusters.
* Data tiers (`docs/governance/DATA_TIERS.md`): all history to 25 Nov 2026 has
  been examined; locked unseen data begins 27 Nov 2026 and is reserved for the
  acceptance evaluation; prospective shadow evidence is collected on staging.

## 6. Evidence summary (detail and tables: report 13)

6.1 **Historical (US 2018–2026 SIP, 736 loans; NGX 2018–2026, 342 loans).** No
completed lender loss for any candidate variant or fixed policy at capacity;
cluster bound on a quarter-cohort loss ≈ 10 % (US, 29–31 clusters) and ≈ 12 %
(NGX, 24–25). The candidate makes ≈ 14× fewer margin calls than fixed ψ 1.15
(US 0.04 vs 0.57 per loan) with more restricted time (40 % vs 31 %).

6.2 **Synthetic joint stress.** *US* (RC1 at 0.925, 160 loans per scenario): no
RC1 loss in any of the eight scenarios; fixed ψ 1.30 loses in the combined
scenario; RC1 makes 2–6× fewer margin calls than fixed ψ 1.15. *NGX* (108
loans per scenario): losses occur only in the combined scenario (3–4 loss
events for every policy). At 0.925 × capacity: expected loss RC1 0.30 %
[0.04, 0.62], v2026.11.6 0.17 % [0, 0.45], fixed ψ 1.15 0.14 % [0, 0.34];
worst loss 10.5 %, 12.3 %, 9.6 %. The intervals overlap: **the policies are
not distinguishable on loss in NGX joint stress**. The robust lever is
utilisation: from 1.0 × to 0.925 × the combined-scenario expected loss falls
from ≈ 0.7 % to 0.2–0.3 % and the worst loss from 17–18 % to 10–12 %. Regime
conditioning lowers stress margin calls in NGX (combined 0.78 → 0.42 per loan,
liquidity collapse 0.39 → 0.11) but raises them in the US combined scenario
(0.46 → 1.04, with fewer liquidations, 0.57 → 0.35); its loss effect is
inconclusive. The 5 % stress-grace rule never triggered. Delayed suspension
valuation cuts suspension-scenario liquidations (US 0.28 → 0.03, NGX 0.11 →
0.03) and borrower costs.

6.3 **Recalibrated controls.** Adaptive headroom (recalibrated) still lowers
NGX capacity by ≈ 30 % and raises restricted time without a loss benefit at
matched exposure; its combined-stress loss reduction comes entirely from
lending less. The recalibrated concentration add-on removes all capacity for
single-name portfolios. Both duplicate risks already in the joint scenarios
(jumps, tail, liquidity) and are excluded from RC1.

6.4 **Frontier.** At 0.925 × capacity the US restricted time falls from 39 % to
22 % (NGX 50 % → 38 %) with no loss at any scale through 1.15 × for the
candidate; fixed ψ 1.15 and 1.30 lose at 1.15 ×.

## 7. Proposed acceptance criteria (for Tiva to set)

Evaluated on the locked unseen period and at least 60 sessions of prospective
shadow evidence, against the v2026.11.6 baseline and fixed ψ 1.15 at matched
exposure, with intervals clustered by origination quarter.

Lender protection
1. No completed lender loss on observed history at RC utilisation in either
   market; conservative upper bound (censored = full loss) reported.
2. In each synthetic joint-stress scenario, expected loss ≤ fixed ψ 1.15 + 0.25
   percentage points of principal and worst loss ≤ 20 %; any scenario failing
   this is a named limitation, not averaged away.
3. Coverage calibration: realised shortfall of executed recoveries below the
   margin-call balance in no more than `1 − q_mc` + 1 pp of margin calls.
4. Zero shadow evaluation errors; data holds only for genuine data defects.

Commercial viability
5. Approved credit ≥ 0.85 × the v2026.11.6 candidate capacity.
6. Margin calls per loan ≤ fixed ψ 1.15 and repeated-call share ≤ 10 %.
7. Restricted time ≤ the v2026.11.6 candidate at matched exposure.
8. Borrower execution cost ≤ fixed ψ 1.15 + 0.5 pp, reported separately for
   intervention sales and maturity workouts.

A policy is not described as dominant if it is worse on 6–8.

Final acceptance thresholds are Tiva's decision; the values above are proposals.

## 8. Limitations

* NGX combined-stress losses (≈ 3.7 % of loans, worst ≈ 10 %) are not
  distinguishable from fixed ψ 1.15 with the available sample; regime
  conditioning's effect on loss is unproven and is the component that most
  needs prospective and locked-period evidence.
* NGX volume before August 2025 is an assumption; NGX settlement T+2 is
  unconfirmed; no executable USD/NGN source.
* All historical data have been examined in development; only the locked
  period and prospective shadow are unseen.
* Synthetic stress overlays are designed scenarios, not probabilities.
* Borrower cost includes maturity workouts that the lifecycle does not yet
  separate from intervention sales.
* Expected shortfall levels are scenario estimates, not guarantees.

## 9. Reproduction

```
pytest app/tests/test_candidate_engine.py app/tests/test_candidate_validation.py
python -m research.methodology_lab.candidate_validation <manifest> --part sim-US --variant-set rc1 --out out
```
Datasets: US Alpaca SIP run 45873299276 (2018-01-02 → 2026-11-25); NGX run
45681534542. Validation workflow: `.github/workflows/candidate-validation.yml`
driven by `infra/requests/validation.json`.
