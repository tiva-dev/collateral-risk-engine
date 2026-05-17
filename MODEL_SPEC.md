# Model Specification v0.1

## Product boundary

The engine is a dynamic collateral risk and liquidation intelligence service. It is not a static LTV calculator and not a single exported ML model.

## Inputs

- Account reference, preferably anonymized or hashed
- Loan balance: principal, accrued interest, fees
- Holdings: asset id, asset type, quantity, currency
- Business policy: base LTV by asset class and risk appetite
- Market data: price, bid, ask, volume, volatility, data quality, and optional order book depth

## Outputs

- Approved credit limit
- Available credit
- Effective LTV per asset
- Risk adjusted collateral value
- Stressed liquidation value
- Minimum stressed liquidation value required to satisfy the dynamic safety requirement
- Recovery coverage ratio
- Dynamic trigger levels
- Margin state
- Liquidation recommendation
- Audit id

## Core formula

Effective LTV is calculated per asset:

```text
effective_ltv =
base_ltv
× volatility_adjustment
× liquidity_adjustment
× spread_adjustment
× concentration_adjustment
× stress_adjustment
× data_quality_adjustment
```

Asset lendable value:

```text
asset_lendable_value = market_value × effective_ltv
```

Portfolio approved credit limit:

```text
approved_credit_limit = sum(asset_lendable_value)
```

## Recovery model

The engine separately estimates stressed liquidation value.

If order book depth is available, it simulates a sell order through the bid stack and overlays stress.

If order book depth is unavailable, it estimates stressed recovery with:

- half-spread cost
- volume-based market impact
- volatility expected shortfall
- data quality penalty

## Margin state

The liquidation threshold is not client-fixed.

The engine calculates dynamic coverage requirements from portfolio risk. The backward-compatible `/risk/evaluate` output reports the evaluated obligation as `loan_balance`; credit lifecycle outputs report top-level pre-trade and projected obligations as `current_outstanding_balance` and `projected_outstanding_balance`.


```text
dynamic_liquidation_coverage = 1.01 + 0.34 × portfolio_risk_score
```

Then margin call, restriction, and warning levels are layered above that. A portfolio enters liquidation when stressed recovery no longer covers loan balance plus the engine's dynamic safety requirement.

## Liquidation plan

When margin call or liquidation is triggered, the engine recommends orders based on:

- liquidity quality
- risk contribution
- concentration
- stressed recovery per unit

The output is a recommendation or execution instruction, depending on deployment mode.

## Deployment

The engine is designed to run as a Python service exposed by API.

Supported deployment patterns:

- Managed cloud API
- Private deployment inside client AWS/VPC
- Client-supplied market data
- Engine-supplied market data
- Hybrid market data

## Validation direction

The simulation layer should be expanded to run:

- Historical high-volatility periods
- Synthetic liquidity collapse
- Overnight gap events
- Order book thinning
- Concentrated portfolio stress
- Flat LTV versus dynamic engine comparison

Core performance metrics:

- Collateral shortfall rate
- Shortfall severity
- Recovery coverage ratio
- Credit capacity preserved
- Warning lead time
- False trigger rate
- Liquidation recovery ratio


## Credit lifecycle v0.2

The lifecycle layer reuses the existing evaluator for origination, draw checks, and monitoring. Lifecycle top-level response fields use `outstanding_balance` terminology and include `current_available_credit`, `projected_available_credit`, and `projected_margin_state` so projected values are not mixed with current fields. Draw decisions use the enum values `approved`, `partially_approved`, and `rejected`; monitoring decisions use the margin-state values `safe`, `watch`, `restrict_new_borrowing`, `margin_call`, and `liquidation`.

For repayment included in a draw check, cash is applied to fees first, then accrued interest, then principal. Remaining principal, accrued interest, and fees are preserved separately in the projected loan.
