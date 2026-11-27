# CRI-1.0.0-rc1 — predefined evaluation protocol (locked period and prospective shadow)

Status: adopted 27 November 2026, **before** any locked-period data has been used.
Applies to: `CRI-1.0.0-rc1` exactly as frozen in `release/CRI-1.0.0-rc1/manifest.json`
(commit `db5ee78ffe7eb6c2de705cce6d141d19da8cc734`, configuration fingerprint
`17534330…46bf9f5`). This protocol may only be amended by a dated, versioned
change made before the evaluation run, never after its results are seen.

Nothing in this protocol authorises lending or autonomous liquidation. The
outcome is evidence for Tiva's risk-appetite and production decision.

## 1. Evidence categories (never pooled)

| Category | What it is | Role |
|---|---|---|
| A. Previously examined history | All provider data to 25 Nov 2026 | Development evidence only; reported, never counted as validation |
| B. Synthetic stress | Designed overlays on observed history | Scenario evidence; never mixed with observed outcomes |
| C. Locked unseen | Provider data dated from 27 Nov 2026 | The formal out-of-sample test, run **once** |
| D. Prospective operational | Live staging shadow decisions, recorded before outcomes are known | Operational evidence; not sufficient alone for financial safety |

## 2. Locking rules

1. Locked data is ingested and stored by staging, but no experiment, chart,
   summary or test reads it before the evaluation run. Staging lifecycle jobs
   are restricted to origination dates on or before 25 Nov 2026 until then.
2. RC1 is not tuned against any evidence collected in categories C or D.
   Defects found operationally are fixed only as RC2 (new version, new manifest),
   and RC1 results remain reported unchanged.
3. The evaluation run executes from the tagged frozen revision, the pinned
   dependency constraints and the frozen configuration at **2,000 Monte Carlo
   paths**; the dataset snapshot ids and artifact hashes are recorded.

## 3. Prospective shadow (category D)

* At least **60 exchange sessions** of daily RC1 shadow evaluations per market
  (US and NGX counted separately on their own calendars), starting 27 Nov 2026.
* Every evaluation persists the release id and fingerprint; runs with any
  other fingerprint are excluded from RC1 evidence.
* Recorded daily: approved LTV, uncapped risk-capacity LTV, state, thresholds,
  data status, stress indicators, proposed recovery plans, and errors.
* Operational acceptance: zero evaluation errors; every data hold traceable to
  a genuine data defect; no state change unexplained by its recorded reasons.
* Sixty sessions establish operational reliability, not long-term financial
  safety; they are reported as such.

## 4. Locked-period lifecycle evaluation (category C)

Run once, when at least 60 sessions per market have accrued after 26 Nov 2026,
and again (pre-declared) at 126 and 252 sessions.

**Loans.** Originated every 5 sessions from 27 Nov 2026 on the same portfolio
set as the confirmation runs (US: concentrated TSLA, concentrated NVDA,
diversified, ETF; NGX: banks, telco, mixed), retail and institutional sizes.
Terms: 21, 63 and 126 sessions (longer-duration loans are included whenever
the locked period is long enough to observe them; loans not yet at maturity
are reported as open, never as safe).

**Matched exposure.** Every policy lends RC1's approved credit at origination
(RC1 engine capacity including regime conditioning × 0.925). A policy-
originated comparison (each policy lends its own capacity) is reported
alongside.

**Comparators.** v2026.11.6 at RC1 utilisation, fixed ψ 1.15, fixed ψ 1.30.

**Borrower behaviour.** Cure probability 0.5 and 0; maturity scenarios repay,
default, 50 % partial repayment, refinance.

**Execution failures.** Observed no-volume and limit-locked sessions do not
fill; in addition, each cohort is re-run with a pre-declared failure overlay:
(i) 20 % of fill sessions randomly fail, (ii) a 3-session execution outage
starting on the first liquidation session, (iii) settlement delayed by one
extra session. Seeds are fixed in advance (`seed = 10_000 + cohort index`).

**Settlement.** Proceeds count only when settled. NGX settlement is applied
as RC1 assumes (T+2) **and**, separately reported, as actually in force on
each date (T+1 from 1 Jun 2026). Proceeds pending at the cutoff are reported
separately and make the loan censored.

## 5. Measurements and statistics

For each policy and each market:

* completed / censored / open counts; loss probability and expected loss on
  completed loans only; conservative upper bound counting censored debt as
  lost; worst loss and ES95 of loss;
* margin calls per loan, repeated-call share, liquidation frequency, time
  restricted, recovery completion and duration;
* borrower costs split into intervention sales and maturity workouts, plus
  refused-refinancing frequency and cost;
* coverage calibration: share of margin-call (forced) events where the
  realised recovery level fell below the stated margin-call (forced) level,
  with an exact one-sided 95 % bound; censored events counted separately;
* recovery accuracy: realised / expected net proceeds per liquidation episode.

**Comparisons are paired.** Differences RC1 − comparator are computed per
cohort on identical paths and random numbers; uncertainty is a 95 % interval
from a moving-block bootstrap over origination-quarter clusters. Overlapping
marginal intervals are never used to claim equivalence; equivalence is
claimed only if the whole interval of the difference lies inside a
pre-declared margin (loss: ±0.10 pp of principal; cost: ±0.25 pp).

With few loss events, the reported quantity is the exact cluster-level bound
on the probability that a cohort-quarter contains a loss; per-loan rates are
not extrapolated.

## 6. Pre-declared provisional criteria (authorised as evaluation criteria, not risk appetite)

| # | Criterion |
|---|---|
| 1 | No completed lender loss at RC1 utilisation in either market (category C) |
| 2 | In each synthetic joint-stress scenario, paired EL difference to fixed ψ 1.15 has upper 95 % bound ≤ +0.25 pp of principal; worst loss ≤ 20 % |
| 3 | Margin-call coverage shortfall rate upper bound ≤ 1 − q_mc + 1 pp (2 %); forced ≤ 1 − q_f + 1 pp (6 %) |
| 4 | Zero shadow evaluation errors; data holds only for genuine defects |
| 5 | Approved credit ≥ 0.85 × v2026.11.6 candidate capacity |
| 6 | Margin calls per loan ≤ fixed ψ 1.15 (paired upper bound ≤ 0); repeated-call share ≤ 10 % on observed data |
| 7 | Time restricted ≤ v2026.11.6 at its own capacity |
| 8 | Intervention cost ≤ fixed ψ 1.15 + 0.5 pp (paired); maturity-workout cost reported separately |

A failed criterion is reported as failed, with its scenario named; it is not
averaged away, and RC1 is not modified to pass it.

## 7. Outputs

A single evaluation report with the run ids, manifest, dataset hashes, the
complete tables for categories A–D kept separate, and a statement of which
criteria passed, failed or could not be assessed.
