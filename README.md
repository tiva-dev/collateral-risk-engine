# Collateral Risk Engine

Institution-grade first-pass engine for investment-backed lending.

The engine calculates dynamic collateral value, effective LTV, margin state, stressed liquidation recovery, and liquidation recommendations from portfolio, loan, policy, and market data inputs. v0.2 adds credit lifecycle endpoints for origination, draw checks, and active monitoring without replacing the existing v0.1 evaluator.

## What is included

- Python risk engine
- FastAPI endpoints: `POST /risk/evaluate`, `POST /credit/originate`, `POST /credit/draw/check`, and `POST /loan/monitor`
- Dynamic LTV adjustments for volatility, liquidity, spread, concentration, stress, and data quality
- Recovery-based margin state calculation
- Order-book-aware recovery estimate when depth is available
- Proxy recovery estimate when order book data is unavailable
- Liquidation recommendation engine
- JSONL audit logging for risk evaluations and lifecycle events
- Simulation hooks and stress scenarios
- Unit tests
- Dockerfile

## Run locally

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s app/tests
python -m app.examples.run_evaluation
uvicorn app.main:app --reload
```

## API contract and terminology

### Backward-compatible risk evaluation

```http
POST /risk/evaluate
```

The payload contains:

- `account_ref`
- `loan`
- `policy`
- `holdings`
- `market_data`

The `/risk/evaluate` response remains backward compatible and continues to use the v0.1 `loan_balance` field. It also returns:

- `approved_credit_limit`
- `risk_adjusted_collateral_value`
- `stressed_liquidation_value`
- `minimum_stressed_liquidation_value`
- `loan_balance`
- `available_credit`
- `recovery_coverage_ratio`
- `margin_state`
- `trigger_levels`
- `asset_results`
- `liquidation_plan`
- `audit_id`

`minimum_stressed_liquidation_value` is the minimum stressed liquidation value required to satisfy the dynamic safety requirement. It is calculated from the evaluated balance and the dynamic warning coverage threshold.

### Credit lifecycle endpoints

Lifecycle endpoints use `outstanding_balance` terminology at the top level instead of `loan_balance`:

- `POST /credit/originate` evaluates a zero-outstanding-balance credit line and returns the approved limit, current/projected outstanding balances, current/projected available credit, collateral values, asset results, margin state, and `audit_id`.
- `POST /credit/draw/check` evaluates requested draw activity, optional repayment activity, projected outstanding balance, projected available credit, and projected margin state before the action proceeds.
- `POST /loan/monitor` evaluates an active loan using principal + accrued interest + fees as the current outstanding balance and returns the current/projected margin state, required cure amount, and liquidation recommendation where applicable.

Lifecycle responses include these explicit pre-trade/projected fields:

- `current_outstanding_balance`
- `current_available_credit`
- `projected_outstanding_balance`
- `projected_available_credit`
- `projected_margin_state`
- `minimum_stressed_liquidation_value`

Lifecycle decision values are:

- `approved`
- `partially_approved`
- `rejected`
- `safe`
- `watch`
- `restrict_new_borrowing`
- `margin_call`
- `liquidation`
- `reduce_available_credit`

`rejected` means an unsafe requested action must not proceed. `reduced_available_credit` is not returned for general projected-state reporting; projected state uses `projected_available_credit`.

For draw checks with repayment, repayment is allocated to fees first, then accrued interest, then principal. Remaining principal, accrued interest, and fees are preserved separately in the projected loan fields.

## Design principle

Client policy controls the base LTV and risk appetite. The engine determines effective LTV, dynamic triggers, liquidation thresholds, and recovery confidence.
