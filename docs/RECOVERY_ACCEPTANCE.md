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

## Live provider evidence status

The acceptance environment had no `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALPHA_VANTAGE_API_KEY`, or `NGNMARKET_API_KEY`. No provider call was attempted and no provider-backed claim is made. Genuine provider-backed records, metrics, QA, and calibration artifacts are therefore **blocked solely on credentials/quota** after local non-live gates. Provider integration remains opt-in and the official workflow must be run without `--allow-synthetic` for official evidence.

## Remaining production gaps

Broker execution, durable transactional storage, streaming provider clients, operational secret management, alert delivery, and production deployment controls are not implemented. Synthetic order-book depth remains sensitivity analysis only and is not observed execution evidence.
