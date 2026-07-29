# Recovery acceptance record

## Economic definitions

All monetary outputs use the engine's two-decimal `round_money` boundary policy. Holdings are aggregated before valuation by asset identifier, exchange, currency, asset type, and provider identifier.

* `repayment_only_cure = max(0, obligation - stressed_liquidation_value / target_coverage)` is the cash amount used by margin notices, draw/action checks, and liquidation-plan cash targets.
* `collateral_injection_only_cure = max(0, target_coverage * obligation - stressed_liquidation_value)` is disclosed separately and is never substituted for repayment cash.
* `credit_limit_breach = max(0, total_obligation - policy_credit_limit)` measures a policy capacity violation.
* `economic_recovery_shortfall = max(0, total_obligation + liquidation_costs - stressed_liquidation_proceeds)` measures loss after stressed recovery.
* `recovery_coverage_ratio = stressed_liquidation_proceeds / max(total_obligation + liquidation_costs, epsilon)`.

Ineligible, halted, missing-FX, or unusable market-data positions have zero borrowing and stressed-recovery contribution. This is a fail-closed policy, not an estimate of actual residual recovery.

## Issue / fix / test matrix

| Issue | Fix | Test/evidence |
|---|---|---|
| Split rows changed concentration and recovery | Aggregate stable identities before all calculations | `test_position_row_splitting_is_financially_invariant` |
| Cure amount mixed repayment and collateral units | Named formulas and separately exposed trigger fields | parametrized boundary/zero/cured/coverage tests |
| Runtime API used hard-coded quotes and FX | API router injects `MissingProvider` and returns missing instruments | runtime AAPL/MTNN test |
| Provider payload/parameters diverged from contracts | Nested NGNMarket chart/top-level FX parsing, documented parameters, Alpaca pagination counts, Alpha Vantage bounded retries | provider fixture suites and opt-in integrations |
| Empty/error provider data entered normalized cache | Fetches reject empty covered series before normalized writes | provider parser/cache tests |
| Replay called a limit breach a collateral shortfall | Persisted obligation components, limit breach, recovery economics, provenance, plans, and transitions | replay and metric tests |
| Placeholder unavailable metrics became 0/1 | Structured `available:false` values with reason and blocking status | metric recomputation/QA tests |
| Policies shared one origination exposure | Separate common-exposure and policy-origination regimes, each explicitly labelled | `test_comparison_regimes_use_distinct_origination_paths` and evidence QA |
| Replay repeated zero returns and treated daily volume as ADV | Update histories only on new observations; use rolling volume and carry-forward age markers | replay regression suite |
| Synthetic depth appeared as observed execution evidence | Official replay uses historical spread/volume proxies and no synthetic order book | replay record provenance and evidence methodology |
| NGN FX stress was directionally inconsistent | Apply devaluation after pair orientation in both direct and inverse directions | `test_ngn_devaluation_is_directionally_consistent` |
| Evidence omitted raw records and did not fail closed | Save full replay results, checksum every artifact, independently recompute metrics, and make QA failure nonzero | `test_mocked_provider_to_evidence_qa_and_calibration` |
| Runtime constructors defaulted to mock providers | Default provider routing uses `MissingProvider`; mocks require explicit test injection | runtime API and aggregation tests |
| Portfolio actions used last price | Buys use ask and sells use bid, with conservative side-specific fallback | portfolio action control tests |
| Client policy could not cap the entire portfolio | Add `portfolio_ltv_cap` as a ceiling after risk adjustment | portfolio-cap regression test |
| Monitoring ignored lender-specific interest | Persist daily/monthly/quarterly/yearly simple/compound interest policy and accrue it into the protected obligation | monitoring interest and replay tests |
| Draws and repayments were not an idempotent monitored lifecycle | Add event-referenced draw, repayment, interest, and fee updates; recheck every draw against current collateral | duplicate-event and unsafe-draw API tests |
| Liquidation output was an abstract target | Produce security, quantity, reference bid, minimum limit price, costs, and estimated net recovery | advisory and monitoring-event tests |
| Replay did not execute or settle the plan | Model quote age, halts, bid, volume participation, partial fills, costs, execution horizon, settlement, and proceeds allocation | execution unit tests and forced-recovery replay |
| Policy outcome used less than the approved limit | Draw 100% of the day-zero CRI limit and accrue the policy obligation | evidence QA requires utilization `1.0` |
| Stress could occur before a loan existed | Originate on unstressed observations, then apply the stress during monitoring | forced-boundary regression test |
| Non-trading observations distorted the usable replay window | Start policy outcomes when every holding has positive observed volume | common-window and liquidity-start tests |

## Local acceptance gates

The non-live recovery gate requires the full unit suite, Ruff, mypy, Bandit with no medium/high findings, dependency audit, byte-code compilation, and package build. The provider integration suite remains opt-in. The mocked end-to-end test covers provider response → checksummed cache → dataset manifest → both replay regimes → complete records → metrics → evidence → QA → calibration diagnostics.

## Live provider evidence status

GitHub Actions
[run 30455533912](https://github.com/tiva-dev/collateral-risk-engine/actions/runs/30455533912)
completed all 260 replays and uploaded the checksummed
[evidence artifact](https://github.com/tiva-dev/collateral-risk-engine/actions/runs/30455533912/artifacts/8725588429).
The ZIP checksum is
`302e2a99a3843de3ca73c72931250bd5def194a759bdb03cd0d5cf182dfa3e8a`.
Evidence QA passed with coverage score `1.0`, no blocking errors, no synthetic
data, and independently recomputed metrics.

Provider coverage for the requested 2018-01-01 to 2026-07-29 window was:

| Provider | Requested | Usable | Missing | Calls |
|---|---:|---:|---:|---:|
| Alpaca equities | 11 | 11 | 0 | 11 |
| NGNMarket equities/FX | 21 | 15 | 6 | 15 |
| Alpha Vantage FX | 6 | 6 | 0 | 4 |

All 15 requested NGX equities were usable. NGNMarket returned no usable rows
for six FX pairs, but Alpha Vantage supplied or supported derivation of all six,
so strict scenario coverage passed. NGNMarket OHLC gaps filled from the same
row's close remain disclosed in the dataset warnings.

### Credit limits at full utilization

Each policy-originated loan drew 100% of its approved CRI limit. The median
initial LTV across the ten portfolios was 29.19%, the mean was 33.41%, and the
range was 2.00% to 56.91%. The low end is deliberate: a concentrated
high-volatility portfolio was penalized rather than forced toward a market
convention.

| Portfolio | CRI LTV | Conventional comparison | Difference |
|---|---:|---:|---:|
| US diversified ETF | 56.91% | 50% | +6.91 pp |
| US concentrated mega-cap | 44.04% | 50% | -5.96 pp |
| US high-volatility concentrated | 2.00% | 50% | -48.00 pp |
| NGX diversified large-cap | 44.48% | 30% | +14.48 pp |
| NGX banking-heavy | 44.83% | 30% | +14.83 pp |
| NGX energy/industrial | 30.14% | 30% | +0.14 pp |
| Mixed NGX/US, NGN loan | 28.13% | 30% | -1.87 pp |
| Mixed NGX/US, USD loan | 28.24% | 50% | -21.76 pp |
| Cross-currency, EUR loan | 28.01% | 50% | -21.99 pp |
| Single-name AAPL | 27.35% | 50% | -22.65 pp |

The result supports a selective, not universal, higher-LTV claim: diversified
NGX portfolios and the US ETF portfolio exceeded their conventional comparison,
while concentrated, volatile, and cross-currency portfolios were constrained.

### Monitoring and recovery

Baseline policy outcomes accrued lender-specific interest and produced 142
liquidation episodes with zero failed executions, zero theoretical economic
shortfall, zero realized creditor loss, and zero terminal unresolved exposure.
Two episodes were right-censored because the NGX energy and mixed NGX/US NGN
windows ended before another observation; they are QA warnings, not completed
recovery claims.

The separately forced, executable liquidation boundary produced one
liquidation in every portfolio. All ten recovered fees, accrued interest, and
principal in one observation after the trigger, after recorded execution costs:
10/10 full recoveries, zero realized creditor loss, and zero terminal exposure.

Trading-halt, stale-quote, and missing-FX counterfactuals correctly contain
failed or incomplete executions because a position cannot be sold reliably
without a tradable market and current valuation. QA records these as explicit
warnings. They are not counted as proof of successful recovery.

Calibration output is diagnostic only. It changed no parameters and flags
credit-limit breaches, counterfactual recovery shortfalls, and material
data-quality haircuts for review. The model is therefore evidence-backed but
not yet statistically calibrated or proven superior for every portfolio.

## Remaining production gaps

Broker execution, durable transactional storage, streaming provider clients, operational secret management, alert delivery, and production deployment controls are not implemented. Synthetic order-book depth remains sensitivity analysis only and is not observed execution evidence.
