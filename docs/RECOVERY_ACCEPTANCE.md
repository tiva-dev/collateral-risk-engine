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

## Local acceptance gates

The non-live recovery gate requires the full unit suite, Ruff, mypy, Bandit with no medium/high findings, dependency audit, byte-code compilation, and package build. The provider integration suite remains opt-in. The mocked end-to-end test covers provider response → checksummed cache → dataset manifest → both replay regimes → complete records → metrics → evidence → QA → calibration diagnostics.

## Live provider evidence status

The acceptance environment had no `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPHA_VANTAGE_API_KEY`, or `NGNMARKET_API_KEY`. No provider call was attempted and no provider-backed claim is made. Genuine provider-backed records, metrics, QA, and calibration artifacts are therefore **blocked solely on credentials/quota** after local non-live gates. Provider integration remains opt-in and the official workflow must be run without `--allow-synthetic` for official evidence.

## Remaining production gaps

Broker execution, durable transactional storage, streaming provider clients, operational secret management, alert delivery, and production deployment controls are not implemented. Synthetic order-book depth remains sensitivity analysis only and is not observed execution evidence.
