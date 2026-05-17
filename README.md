# Collateral Risk Engine

Institution-grade first-pass engine for investment-backed lending.

The engine calculates dynamic collateral value, effective LTV, margin state, stressed liquidation recovery, and liquidation recommendations from portfolio, loan, policy, and market data inputs. v0.2 also adds credit lifecycle endpoints for origination, draw checks, and active loan monitoring.

## What is included

- Python risk engine
- FastAPI endpoint: `POST /risk/evaluate`
- Preferred portfolio action endpoint: `POST /portfolio/action/check`
- Legacy pre-trade risk check endpoint: `POST /risk/pre-trade-check`
- Lifecycle endpoints: `POST /credit/originate`, `POST /credit/draw/check`, `POST /loan/monitor`
- Dynamic LTV adjustments for volatility, liquidity, spread, concentration, stress, and data quality
- Credit lifecycle fields for origination and monitoring
- Recovery-based margin state calculation
- Order-book-aware recovery estimate when depth is available
- Proxy recovery estimate when order book data is unavailable
- Liquidation recommendation engine
- JSONL audit logging
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

## API example

```http
POST /risk/evaluate
```

The payload contains:

- `account_ref`
- `loan`
- `policy`
- `holdings`
- `market_data`

The response contains:

- `approved_credit_limit`
- `current_outstanding_balance`
- `current_available_credit`
- `loan_balance` (legacy alias for the projected loan balance)
- `outstanding_balance` (legacy alias for `current_outstanding_balance`)
- `available_credit` (legacy alias for `current_available_credit`)
- `requested_draw_amount`
- `projected_loan_balance`
- `projected_available_credit`
- `risk_adjusted_collateral_value`
- `stressed_liquidation_value`
- `dynamic_safety_requirement`
- `minimum_stressed_liquidation_value`
- `recovery_coverage_ratio`
- `margin_state`
- `trigger_levels`
- `asset_results`
- `liquidation_plan`
- `audit_id`

## Portfolio action checks

```http
POST /portfolio/action/check
```

Use `POST /portfolio/action/check` for new portfolio action integrations. It is the preferred endpoint for validating single portfolio actions because it accepts the full `account_state`, supports pledged cash, and returns explicit `current_*` and `projected_*` response fields.

Supported canonical action types are:

- `buy`
- `sell`
- `withdraw_cash`
- `withdraw_security`
- `transfer_security`
- `repay`
- `rebalance`
- `draw`

Legacy action aliases are still accepted for backward compatibility but should not be used in new clients:

- `withdrawal` is an alias for `withdraw_security`
- `transfer` is an alias for `transfer_security`
- `repayment` is an alias for `repay`
- `credit_draw` remains supported by the legacy pre-trade endpoint; use `draw` with `POST /portfolio/action/check` for new clients

The response separates current and projected state with fields such as `current_outstanding_balance`, `current_available_credit`, `projected_outstanding_balance`, `projected_loan_balance`, `projected_available_credit`, and `projected_margin_state`.

## Pre-trade check

```http
POST /risk/pre-trade-check
```

`POST /risk/pre-trade-check` is a legacy endpoint retained for existing integrations. New portfolio action clients should use `POST /portfolio/action/check`.

Submit current holdings, current outstanding balance, market data, and proposed actions. Legacy supported action types are:

- `buy`
- `sell`
- `withdrawal`
- `transfer`
- `repayment`
- `credit_draw`

The engine projects the post-action portfolio and loan balance. Its response includes `current_outstanding_balance` and `current_available_credit` alongside legacy aliases `outstanding_balance` and `available_credit`. It only approves the action when projected stressed liquidation value remains above projected loan balance plus the dynamic safety requirement. Otherwise it returns one of:

- `reject`
- `require_repayment`
- `reduce_available_credit`
- `margin_call`
- `liquidation`

Lifecycle draw-check decisions include `approved`, `partially_approved`, or `rejected`.

## Design principle

Client policy controls the base LTV and risk appetite. The engine determines effective LTV, dynamic triggers, liquidation thresholds, and recovery confidence.
