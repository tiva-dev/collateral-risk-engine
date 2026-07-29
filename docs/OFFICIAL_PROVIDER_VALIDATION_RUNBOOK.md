# Official Provider Validation Runbook (v0.6)

v0.6 prepares safe, reproducible, auditable provider-backed validation evidence. It does not change model parameters automatically, does not implement broker execution, and does not add live WebSocket market data.

## Dry-run validation

```bash
python -m app.simulations.build_official_dataset --dry-run --providers all --start-date 2018-01-01
python -m app.simulations.run_official_validation --dry-run --scenario all --qa --calibration
```

Dry-run mode prints planned provider calls and scenario eligibility without calling providers.

## Real provider smoke test

```bash
python -m app.simulations.run_provider_validation_smoke
```

Without `--confirm-real-provider-calls`, the command refuses real calls safely. To test credentials and parser wiring with minimal calls:

```bash
python -m app.simulations.run_provider_validation_smoke --confirm-real-provider-calls
```

The smoke test prints summary metadata only and must not print secrets.

## Real provider dataset build

Run manually through `.github/workflows/official-validation-provider-run.yml` or locally:

```bash
python -m app.simulations.build_official_dataset --providers all --start-date 2018-01-01 --output-dir data/simulation_results
```

Quota gates estimate planned calls and compare them with defaults: NGNMarket monthly 3000 and per-run 500, Alpha Vantage per-run 100, Alpaca per-run 500. Use overrides only for reviewed official runs.

## Official validation

```bash
python -m app.simulations.run_official_validation --dataset-manifest data/simulation_results/<manifest>.json --scenario all --stress baseline --strict-coverage --qa --calibration --output-dir data/simulation_results
```

Run baseline first, inspect its QA result, then repeat with `--stress severe` and
finally `--stress all`. Strict coverage is mandatory for an official run. Do not
use `--allow-synthetic`; that option is reserved for a separately labelled THIN
liquidity sensitivity and cannot support an official provider-backed claim.

## Provider coverage interpretation

Coverage reports classify gaps as blocking, non-blocking, synthetic allowed, or excluded. Missing THIN data is synthetic-only when explicitly allowed. Missing real holdings are blocking under strict coverage.

## Workflow and smoke safety

The GitHub workflow is manual-only. Choose `dry_run=false`,
`run_validation_after_build=true`, and `stress=baseline`, `severe`, or `all`.
It rejects `dry_run=true` together with post-build validation because a dry run
creates no cache evidence. The provider smoke command refuses calls without
explicit confirmation, sanitizes errors/warnings/quota metadata, and reports
each provider independently even after another provider fails.
