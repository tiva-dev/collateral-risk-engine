# Interest and obligation methodology

CRI protects the full obligation, not principal alone:

`total obligation = principal + accrued interest + fees`

The policy accepts either an annual rate or an explicitly quoted daily,
monthly, quarterly, or yearly rate. A quoted periodic rate is normalized before
accrual. For example, 4% monthly is treated as a 48% simple annualized rate, not
4% annually paid monthly.

Interest may accrue daily, monthly, quarterly, or yearly using actual/365,
actual/360, or 30/360 day count and simple or compound treatment. Client-supplied
interest remains available when the lender is the accounting source of truth.

At origination, CRI converts safe total-obligation capacity into principal
capacity after reserving fixed fees and projected interest:

`safe principal = (safe obligation capacity - fixed fees) / projected obligation factor`

For interest payable at maturity, the projection runs through maturity. For
periodic interest payments, it runs through the next payment date plus any grace
period, capped at maturity. Monitoring accrues interest from the last persisted
accrual time before every risk decision, draw, repayment, or liquidation fill.

Repayments and net liquidation proceeds are allocated to fees first, then
accrued interest, then principal. Idempotency references prevent the same client
transaction from being applied twice.
