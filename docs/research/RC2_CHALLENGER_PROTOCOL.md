# RC2 research: prospective challenger protocol

Status: research. Nothing here changes RC1 or any lending decision.

## Frozen challenger: `intelligence.us_ridge_ns` 1.0.0

| Item | Frozen value |
|---|---|
| Target | log of realised 10-session volatility, floored |
| Features | `app.intelligence.benchmark.row_features`, without the history-length feature |
| Preprocessing | standardise with training mean and scale |
| Estimator | ridge regression, alpha 1.0 |
| Training data | every examined US observation (to 25 Nov 2026), research dataset hash recorded |
| Floor | 5% quantile of positive training realised volatility |
| Calibration | 1% breach target. Haircut threshold q = z_t × σ_t; z_t is the conservative order statistic (lower 25% binomial point) of standardised outcomes completed by the decision date, in a trailing 3-year window |
| Calibration pool | out-of-fold forecasts (calendar-year folds, purged); completed live outcomes join it |
| Haircut | LTV = exp(q) |

The artifact in the private research bucket holds every parameter, the training-data hashes, the code version, the library versions and its own SHA-256. It is create-only, and versions are immutable. The registry entry moves from research to shadow stage. Promotion beyond shadow needs an authenticated approval and does not happen automatically.

## Separation from RC1

- The challenger never reads, writes or influences RC1 decisions.
- It never reads RC1's locked evaluation snapshot. Like RC1's own live shadow, it decides on current data each day.
- No parameter, feature or rule changes after freezing. A change means a new version with a new artifact, evaluated from its own freeze date.

## Pre-registered evaluation

- **Minimum and timing:** at least 60 sessions of decisions, reported once.
- **Primary metrics:**
  - breach rate against 1% (Kupiec test);
  - 1% pinball loss;
  - mean LTV;
  - mean LTV with RC1's contractual ceiling applied to every method.
- **Comparators:**
  - RC1's standalone estimator under the same ex-ante rule;
  - RC1's own approved LTV (the complete framework).
- **Inference:** moving-block bootstrap over calendar blocks, covering all instruments and overlapping horizons.
- **Use of results:** they are evidence for RC2 research only, and never trigger automatic use.
