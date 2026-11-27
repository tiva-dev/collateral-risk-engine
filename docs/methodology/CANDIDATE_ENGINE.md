# CRI candidate risk and recovery engine

Version: candidate-2026.11.3. Status: **candidate, shadow only.** It is not approved methodology and does not
drive account state, margin events, webhooks or orders. Confidence levels and
horizons are research defaults (`app/candidate/config.py`) awaiting Tiva's
risk-appetite decision.

## 1. What it decides

For each account, from information available on the decision date:

| Level | Definition | Default |
|---|---|---|
| Margin call `B_mc` | Largest balance at which waiting the grace period and then selling still covers the obligation at confidence `q_mc` | q_mc = 99% (ES) |
| Forced recovery `B_f` | Largest balance at which an immediate sale still covers at `q_f`; above it there is no grace | q_f = 95% (ES) |
| Initial capacity `B_0` | From `B_0`, the probability of reaching `B_mc` within `h` sessions is at most `1 - q_h`; then capped by the client's LTV ceilings | h = 5, q_h = 95% |
| Early warning `B_ew` | A one-session move at `q_ew` would reach `B_mc` | q_ew = 90% |
| Restoration target | Uncapped `B_0` of the remaining collateral (variant: `B_ew`) | initial capacity |

States: balance ≤ B_0 safe; ≤ B_ew restricted (no new borrowing); ≤ B_mc early
warning; ≤ B_f margin call (grace, cure to the restoration target, otherwise
partial liquidation after grace); above B_f forced recovery (immediate partial
liquidation to the restoration target).

## 1a. Corrections of 27 November 2026

* Initial capacity and early warning use **first passage**: the probability
  that the balance crosses the margin-call level at any session within the
  horizon (path-wise, with the obligation growing under the contract), not the
  terminal quantile.
* Obligation growth is projected with the **contractual interest engine**
  (capitalization schedule, day count, outstanding accrued interest).
* **Stressed liquidity** in every scenario: AR(1) session volume that falls
  with the portfolio's drawdown, no-fill sessions (from the observed stale
  share), suspensions with a catch-up move on resumption, and the market
  profile's daily price limit as an execution constraint (limit-down sessions
  do not fill; the excess move carries over). Sale completion and interest are
  path-dependent; quantity unsold within the horizon counts for nothing.
* **Restoration** counts only collateral that had lending value.
* **Data quality**: material missing, stale (quote > 1 business day, last bar
  > 2 business days) or unreliable data blocks additional borrowing. A breach
  that disappears on the latest reliable data is data deterioration: no sale,
  escalation. A missing price always holds the account for review.
* **Execution consistency**: the planner receives the candidate's reference
  bid, stressed extra spread, expected impact, reported-volume ADV,
  participation and settlement.
* **Lifecycle** executes the candidate's own planner decisions.

## 2. How the levels are computed

`B*(q, d) = (C + P_q(d)) / g(d + T_exec + T_settle)`

* `C` pledged cash in the loan currency (at par, never sold).
* `P_q(d)` mean net sale proceeds over the worst `1 - q` of scenarios when
  selling starts `d` sessions from now; each instrument is sold at its
  participation cap of reported volume (the market profile's cap, lowered by
  the client's execution policy, never raised), at the bid less the stressed
  half-spread, square-root impact on the simulated volatility, and the fee.
* `g(T)` interest growth over `T` sessions in calendar days; the balance
  already contains accrued interest and fees.
* `d = 1 + grace` for margin calls, `1 + delay` for forced recovery.
* Settlement is the longer of the client policy and the market profile.

Scenarios are filtered historical simulations: each instrument's standardised
residuals (EWMA, λ = 0.94) are bootstrapped jointly by date when at least 60
common dates exist, otherwise comonotonically (conservative); volatility is
updated along each path. Seeds are derived from the decision date and the
instrument identities, so results are reproducible and comparable.

## 3. Uncertainty and short histories

Per instrument (`app/candidate/estimation.py`), the same formulas in every market:

* volatility = max(EWMA, long-run) × √VR(10) (variance ratio corrects for stale
  prices and partial adjustment; VR = 1 in an efficient market);
* effective observations `n_eff = n × (1 − stale share) / VR`;
* partial pooling of log volatility toward the market's group prior of
  instruments with ≥ 250 sessions up to the decision date (point in time);
* risk uses the one-sided credible bound `exp(μ_post + z·sd_post)`, z = 1;
* the empirical tail is trusted in proportion to the residual count (full at
  250); the remaining mass is a Student-t(4) tail by quantile mapping, which is
  a stated modelling assumption and never replaces missing observations;
* fewer than 20 sessions, data quality below 0.5 or no reported volume: no
  lending value (the instrument may still be sold in a recovery).

The 60-session finding from the generalisation study is treated as preliminary
evidence, not a rule: the engine has no hard 60-session cut; short histories are
handled by the continuous safeguards above.

## 4. Data, identity and currency

* Market data and bars are looked up by stable identity
  (exchange:symbol:currency:type), never by bare symbol.
* A holding without a current price (missing or older than the policy's quote
  age) puts the account in data review: no new borrowing, no automated sale,
  escalation to a person.
* Collateral in a currency other than the loan currency restricts the account
  (capacity 0, no automated plan) until cross-currency risk is implemented.

## 5. Recovery

Margin calls and forced recovery are sized by the unified recovery planner
(`app/liquidation/planning.plan_recovery`) with a candidate adaptor: the
smallest sale, over priority and pro-rata orderings, after which the residual
obligation (with interest to settlement) is within the restoration target of
the remaining collateral. The planner receives the same reported-volume ADV
the thresholds assumed.

## 6. Validation

* Decision replay (`/backtests`, kind `decision`): what each model would have
  decided on past dates for a fixed loan. Not a lifecycle result.
* Lifecycle validation (`/backtests`, kind `lifecycle`;
  `app/candidate/lifecycle.py`): matched loans (same origination, collateral
  and principal) run to maturity under each intervention variant with
  contractual interest, warnings, margin calls, cures, partial and forced
  liquidation against observed sessions, participation limits and settlement,
  maturity repayment or workout. Common random numbers (cure draws, scenario
  seeds) across variants. Metrics: lender loss, capacity, borrower execution
  cost, liquidation frequency, time under restriction and in margin call.
* Variants compared: fixed ψ = 1.15, fixed ψ = 1.30, adaptive, adaptive with
  restoration to early warning.

## 7. Not included

NGX price-limit modelling (not approved); cross-currency collateral;
intraday monitoring; borrower behaviour beyond a cure probability.
