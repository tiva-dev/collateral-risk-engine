# Calibration Review Guide (v0.6)

The calibration pack writes `calibration_diagnostics.json` and `calibration_diagnostics.md`.

Diagnostics include average approved credit, average lifecycle safe credit limit, credit capacity preserved, shortfall rate, worst shortfall, margin call frequency, liquidation frequency, over-conservatism indicators, under-protection indicators, and suggested review areas.

Over-conservatism examples include dynamic capacity below flat/static while shortfall rate is zero, excessive margin calls with no shortfalls, and available credit collapse due to data quality alone.

Under-protection examples include remaining shortfalls, incomplete liquidation plans, short warning lead time, and FX/data gaps causing unprotected exposure.

This stage reports review areas only. It does not automatically tune or change the model.

## Credit diagnostics
Approved-credit diagnostics use `average_approved_credit`; lifecycle-safe-limit diagnostics use `average_lifecycle_safe_credit_limit`; preserved capacity uses the corresponding capacity metric. Loan balance is never a substitute. Missing measures carry an explicit unavailable status and reason.
