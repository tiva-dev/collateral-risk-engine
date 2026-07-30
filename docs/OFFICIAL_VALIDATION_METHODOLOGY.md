# Official validation methodology

## Data windows

US equities and FX use the requested provider window. NGX equities use the
latest 370 calendar days because this is the currently validated reliable
NGNMarket history. Every result records requested dates, the actual common
portfolio window, per-instrument provenance, carry-forward age, missing data,
and FX state.

Missing volume remains unavailable. It is never converted to zero turnover.
Adjusted prices are used when available, rolling histories exclude repeated
non-trading-day returns, and synthetic order books are excluded from official
execution evidence.

## Comparisons

Two regimes are reported separately:

1. Common-exposure surveillance: every policy monitors the same fixed debt.
2. Policy-origination outcome: each policy originates its own debt from its own
   day-zero principal capacity and follows that obligation path through
   contractual maturity. Relative-term scenarios use the most recent complete
   cohort available in the replay window.

The conventional flat benchmark is 30% for NGX/NGN collateral and 50% for other
collateral unless the evidence manifest records an explicit override.

## Economic outcomes

`credit limit breach = max(0, total obligation - policy credit limit)`

`economic recovery shortfall = max(0, total obligation + liquidation costs - stressed liquidation proceeds)`

Replay persists principal, interest, fees, safe principal capacity, effective
principal LTV, future-interest reserve, stressed proceeds, CRI-derived
participation rates, monitoring transitions, and liquidation advisories.

## Reproducibility and acceptance

Runs record the commit, deterministic seed, model/config versions, loan terms,
stress assumptions, provider coverage, checksummed normalized caches, complete
replay records, metrics, and artifact checksums. QA independently recomputes
metrics from saved records and exits nonzero on coverage, checksum, labelling,
or blocking-metric failure. Calibration output is diagnostic only and never
changes model coefficients automatically.
