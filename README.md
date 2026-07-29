# Collateral Risk Engine

The Collateral Risk Engine (CRI) calculates a risk-adjusted credit limit for a
securities portfolio and protects a drawn loan as prices, accrued interest,
fees, FX, and market liquidity change.

It is not just an LTV calculator. Its intended lifecycle is:

1. a lender defines its credit, interest, monitoring, and liquidation policy;
2. CRI values the pledged portfolio and returns the current credit limit;
3. the lender reports a draw, repayment, interest adjustment, or fee;
4. CRI monitors the total obligation against stressed recoverable value;
5. a deterioration produces an early warning, margin call, or executable
   liquidation advisory;
6. liquidation proceeds settle against fees, interest, and principal, and CRI
   continues monitoring any residual exposure.

The authoritative formulas, test matrix, and evidence status are in
[docs/RECOVERY_ACCEPTANCE.md](docs/RECOVERY_ACCEPTANCE.md).

## Client policy controls

The API accepts:

* base LTVs and maximum LTVs by asset type;
* a maximum LTV for the whole portfolio;
* risk appetite, data-quality threshold, and maximum market participation;
* annual interest rate, daily/monthly/quarterly/yearly accrual frequency,
  simple or compound interest, day-count convention, and either
  engine-calculated or client-supplied accruals;
* loan currency;
* margin-call grace, liquidation delay, settlement delay, execution horizon,
  costs, maximum slippage, quote age, and forced-liquidation behavior.

CRI may approve less than the client's cap when concentration, volatility,
liquidity, price stress, FX, or data quality makes the collateral less
recoverable. Client caps are ceilings, not targets.

## Implemented and tested

* Stable-identity aggregation before concentration, HHI, valuation, lending,
  stressed recovery, portfolio risk, and margin calculations.
* Credit-limit and lifecycle endpoints with canonical available-credit logic.
* Idempotent draw, repayment, interest, and fee updates for monitored accounts.
* Interest accrual as part of the monitored obligation.
* State-change monitoring for normal, warning, margin-call, and liquidation
  conditions.
* Liquidation advisories naming the security, quantity, reference bid, minimum
  limit price, estimated costs, and estimated net recovery.
* Historical execution constrained by observed bid proxies, quote freshness,
  trading halts, rolling volume, participation limits, slippage limits, costs,
  execution delay, and settlement delay.
* Proceeds allocated to fees, interest, and principal, with residual exposure
  monitored after partial recovery.
* Fail-closed market-data and FX handling; runtime APIs do not silently use mock
  AAPL, MTNN, or FX values.
* NGNMarket, Alpaca, and Alpha Vantage historical adapters with checksummed
  caches, provider coverage, pagination/call counts, and error handling.
* Separate common-exposure surveillance and policy-origination comparisons.
* Complete replay records, checksummed evidence, independent metric
  recomputation, fail-closed QA, and calibration diagnostics.

The non-network suite is:

```bash
python -m pytest -q
```

## Operational API surface

| Endpoint | Purpose |
|---|---|
| `POST /risk/evaluate` | Evaluate a normalized portfolio and current/drawn obligation |
| `POST /credit/originate` | Return the safe initial credit limit |
| `POST /credit/draw/check` | Recheck a proposed draw or repayment |
| `POST /portfolio/action/check` | Approve or reject buys, sells, deposits, withdrawals, draws, and repayments at executable-side prices |
| `POST /monitoring/accounts` | Register holdings, loan, policy, interest policy, and execution policy |
| `POST /monitoring/accounts/{account_ref}/loan` | Apply an idempotent draw, repayment, interest, or fee event |
| `POST /monitoring/accounts/{account_ref}/tick` | Revalue one monitored account and emit state-change/advisory events |
| `POST /monitoring/market-data/update` | Ingest timestamped quote/FX updates and optionally trigger monitoring |
| `GET /monitoring/events` | Retrieve monitoring, margin, and liquidation-advisory events |

The direct risk endpoint accepts only holdings already normalized into the loan
currency. Client quotes and FX rates require explicit timezone-aware
timestamps. Buys are checked at ask plus costs and sells at bid minus costs.
`/portfolio/action/check` is the preferred endpoint for executable portfolio
controls; `/risk/pre-trade-check` is the legacy endpoint. Requests use
`loan_balance`; lifecycle responses expose `current_outstanding_balance` and
`current_available_credit`; monitoring records use `outstanding_balance` and
`minimum_stressed_liquidation_value`. Unsafe actions are `rejected`, and
`withdrawal` is an alias for `withdraw_security`.

## Provider-backed validation

The official validation uses real historical provider data, draws 100% of each
policy's approved day-zero limit, accrues interest, monitors the obligation,
and replays margin and liquidation execution through settlement. It compares
CRI limits with conventional 30% NGN and 50% US flat-LTV baselines and reports
both additional lendable value and realized recovery outcomes.

The exact run, provider coverage, LTV distribution, forced-recovery result,
warnings, and calibration status are recorded in
[docs/RECOVERY_ACCEPTANCE.md](docs/RECOVERY_ACCEPTANCE.md). Official runs must
use strict coverage and must not use synthetic data.

## What the evidence does not claim

The replay proves the engine's decision and advisory logic against historical
observations and documented execution proxies. It does not prove that a broker
would fill an order during a real future halt, missing-FX period, or stale
market. Those conditions fail closed and remain explicitly visible as
right-censored or blocked execution episodes.

The repository does not yet provide broker connectivity, durable transactional
storage, production streaming clients, authentication/tenant isolation,
external notification delivery, or deployment/SRE controls. The current event
stream and repositories are reference implementations, not production
infrastructure.

Synthetic THIN liquidity and order-book depth may be used only in separately
labelled sensitivity analysis; they cannot support provider-backed execution
claims.
