# Collateral Risk Engine

A testable reference engine for collateral valuation, borrowing limits, lifecycle controls, liquidation planning, and historical replay. The authoritative definitions and recovery acceptance matrix are in [docs/RECOVERY_ACCEPTANCE.md](docs/RECOVERY_ACCEPTANCE.md).

## Implemented and tested

* Stable-identity holding aggregation before concentration, HHI, lending, recovery, portfolio-risk, and margin calculations.
* Distinct repayment-only and collateral-injection-only cure amounts.
* Fail-closed treatment of ineligible or unusable collateral and zero-limit origination rejection.
* Runtime API construction that does not supply mock AAPL, MTNN, or FX values when a provider is absent.
* Historical adapters for NGNMarket, Alpaca, and Alpha Vantage with canonical checksummed caches, coverage metadata, pagination/call counts, error handling, and opt-in network tests.
* Replay records that separate credit-limit breach from economic recovery shortfall, persist obligation components and provenance, and emit state-change events.
* Evidence QA and calibration utilities. Unavailable metrics are represented with a reason rather than invented zero/one values.

Run the non-network suite:

```bash
python -m pytest -q
```

### API contract

`/risk/evaluate` is the direct evaluation endpoint. `/portfolio/action/check` is the preferred endpoint for executable portfolio controls; `/risk/pre-trade-check` is the legacy endpoint. Responses distinguish `loan_balance`, `current_outstanding_balance`, `outstanding_balance`, `current_available_credit`, and `minimum_stressed_liquidation_value`. A `withdrawal` is an alias for `withdraw_security`; invalid or unsafe actions are rejected.

## Provider-backed validation

**Not completed in this environment.** Required provider credentials were absent, so this repository contains no newly claimed provider-backed result. Follow `docs/OFFICIAL_PROVIDER_VALIDATION_RUNBOOK.md` after configuring credentials and quota budgets. Provider integrations are deliberately opt-in; official evidence must fail on missing coverage or QA and must not use `--allow-synthetic`.

## Synthetic sensitivity analysis

Deterministic synthetic THIN liquidity and synthetic order-book depth may be used only as explicitly labelled sensitivity inputs. They are not provider observations, cannot establish execution quality, and must not be mixed into official provider-backed conclusions.

## Not implemented production infrastructure

This repository does not provide broker execution, transactional production persistence, streaming feeds, secret-management infrastructure, notification delivery, or deployment/SRE controls. See `docs/RECOVERY_ACCEPTANCE.md` for the concise gap list and exact financial formulas.
