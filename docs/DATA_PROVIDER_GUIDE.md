# DATA PROVIDER GUIDE

This document is part of the v0.5B official validation evidence package.

## Scope

The validation framework replays cache-first historical datasets and applies deterministic synthetic stress overlays to compare flat LTV, static haircut, and dynamic collateral risk engine lending outcomes. It does not call live providers during normal CI, does not execute broker orders, and does not require production databases or real API keys.

## Reproducibility

Runs record deterministic seeds, manifest checksums, simulation configuration version, model versions, interest settings, provider coverage metadata, warnings, and stress assumptions.

## Outputs

The reporting layer produces official validation manifest JSON, metrics JSON, metrics CSV, validation report Markdown, provider coverage report, data methodology, interest accrual methodology, and simulation assumptions.

## Canonical official replay cache (v0.7.0)
Normalized equity caches are canonical `HistoricalSeries` objects containing `instrument`, optional `instrument_identity`, `bars`, `provider_name`, retrieval/date bounds, warnings, and a data-quality summary. FX caches use `HistoricalFXSeries` with `from_currency`, `to_currency`, and `rates`. Raw/provider-native responses remain useful audit inputs but are skipped, warned about, and are not official replay evidence.

Scenario eligibility derives every holding currency and requires its exact direct or inverse conversion against the loan currency. An unrelated pair merely containing the loan currency does not qualify.
