# Interest Accrual Methodology

The loan interest foundation is implemented in `app.credit.interest`.

## Modes

- `client_supplied`: the supplied `Loan` object is returned unchanged; no engine interest calculation is performed.
- `engine_calculated`: interest accrues from `from_datetime` to `to_datetime` using the configured annual rate and day-count convention.

## Day counts

- `actual_365`: actual elapsed days divided by 365.
- `actual_360`: actual elapsed days divided by 360.
- `thirty_360`: 30/360 convention for monthly/annual accrual modeling.

## Compounding

Simple interest is added to `accrued_interest`. Compound interest capitalizes existing accrued interest and new interest into principal.

## Repayment order

Repayments apply to fees first, accrued interest second, and principal last.
