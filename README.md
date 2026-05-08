# Collateral Risk Engine

Institution-grade first-pass engine for investment-backed lending.

The engine calculates dynamic collateral value, effective LTV, margin state, stressed liquidation recovery, and liquidation recommendations from portfolio, loan, policy, and market data inputs.

## What is included

- Python risk engine
- FastAPI endpoint: `POST /risk/evaluate`
- Pre-trade risk check endpoint: `POST /risk/pre-trade-check`
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
- `outstanding_balance`
- `available_credit`
- `requested_draw_amount`
- `projected_loan_balance`
- `projected_available_credit`
- `risk_adjusted_collateral_value`
- `stressed_liquidation_value`
- `dynamic_safety_requirement`
- `recovery_coverage_ratio`
- `margin_state`
- `trigger_levels`
- `asset_results`
- `liquidation_plan`
- `audit_id`

## Pre-trade check

```http
POST /risk/pre-trade-check
```

Submit current holdings, current outstanding balance, market data, and proposed actions. Supported action types are:

- `buy`
- `sell`
- `withdrawal`
- `transfer`
- `repayment`
- `credit_draw`

The engine projects the post-action portfolio and loan balance. It only approves the action when projected stressed liquidation value remains above projected loan balance plus the dynamic safety requirement. Otherwise it returns one of:

- `reject`
- `require_repayment`
- `reduce_available_credit`
- `margin_call`
- `liquidation`

## Design principle

Client policy controls the base LTV and risk appetite. The engine determines effective LTV, dynamic triggers, liquidation thresholds, and recovery confidence.
