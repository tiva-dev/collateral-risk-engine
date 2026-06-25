# Collateral Risk Engine

Institution-grade first-pass engine for investment-backed lending.

The engine calculates dynamic collateral value, effective LTV, margin state, stressed liquidation recovery, and liquidation recommendations from portfolio, loan, policy, and market data inputs. v0.2 also adds credit lifecycle endpoints for origination, draw checks, and active loan monitoring.

## What is included

- Python risk engine
- FastAPI endpoint: `POST /risk/evaluate`
- Preferred portfolio action endpoint: `POST /portfolio/action/check`
- Legacy pre-trade risk check endpoint: `POST /risk/pre-trade-check`
- Lifecycle endpoints: `POST /credit/originate`, `POST /credit/draw/check`, `POST /loan/monitor`
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
- `current_outstanding_balance`
- `current_available_credit`
- `loan_balance` (legacy alias for the projected loan balance)
- `outstanding_balance` (legacy alias for `current_outstanding_balance`)
- `available_credit` (legacy alias for `current_available_credit`)
- `requested_draw_amount`
- `projected_loan_balance`
- `projected_available_credit`
- `risk_adjusted_collateral_value`
- `stressed_liquidation_value`
- `dynamic_safety_requirement`
- `minimum_stressed_liquidation_value`
- `recovery_coverage_ratio`
- `margin_state`
- `trigger_levels`
- `asset_results`
- `liquidation_plan`
- `audit_id`

## Portfolio action checks

```http
POST /portfolio/action/check
```

Use `POST /portfolio/action/check` for new portfolio action integrations. It is the preferred endpoint for validating single portfolio actions because it accepts the full `account_state`, supports currency-aware pledged cash, and returns explicit `current_*` and `projected_*` response fields.

Supported canonical action types are:

- `buy`
- `sell`
- `withdraw_cash`
- `withdraw_security`
- `transfer_security`
- `repay`
- `rebalance`
- `draw`

Legacy action aliases are still accepted for backward compatibility but should not be used in new clients:

- `withdrawal` is an alias for `withdraw_security`
- `transfer` is an alias for `transfer_security`
- `repayment` is an alias for `repay`
- `credit_draw` remains supported by the legacy pre-trade endpoint; use `draw` with `POST /portfolio/action/check` for new clients

Buy actions must be funded by sufficient pledged cash or an explicit `funding_source` such as `draw`, `transfer_in`, or `external_cash`; unfunded buys are rejected rather than silently creating collateral. Pledged cash is modeled per currency with identities such as `PLEDGED_CASH_USD`, `PLEDGED_CASH_NGN`, and `PLEDGED_CASH_EUR`.

The response separates current and projected state with fields such as `current_outstanding_balance`, `current_available_credit`, `projected_outstanding_balance`, `projected_loan_balance`, `projected_available_credit`, and `projected_margin_state`.

## Pre-trade check

```http
POST /risk/pre-trade-check
```

`POST /risk/pre-trade-check` is a legacy endpoint retained for existing integrations. New portfolio action clients should use `POST /portfolio/action/check`.

Submit current holdings, current outstanding balance, market data, and proposed actions. Legacy supported action types are:

- `buy`
- `sell`
- `withdrawal`
- `transfer`
- `repayment`
- `credit_draw`

The legacy route is retained for existing integrations and now rejects buy actions that do not include an explicit draw or funding source. The engine projects the post-action portfolio and loan balance. Its response includes `current_outstanding_balance` and `current_available_credit` alongside legacy aliases `outstanding_balance` and `available_credit`. It only approves the action when projected stressed liquidation value remains above projected loan balance plus the dynamic safety requirement. Otherwise it returns one of:

- `reject`
- `require_repayment`
- `reduce_available_credit`
- `margin_call`
- `liquidation`

Lifecycle draw-check decisions include `approved`, `partially_approved`, or `rejected`.

## Design principle

Client policy controls the base LTV and risk appetite. The engine determines effective LTV, dynamic triggers, liquidation thresholds, and recovery confidence.

## v0.3 Multi-market market data aggregation

v0.3 adds a market data aggregation and normalization layer in `app/market_data` that sits before the existing collateral evaluator. The core evaluator still receives the same `MarketData` domain objects, so existing risk, lifecycle, pre-trade, pledged-cash, and portfolio action endpoints remain backward compatible.

### Instrument identity

Market data is no longer identified only by ticker. The new instrument identity model includes `asset_id`, `symbol`, `exchange`, `currency`, `asset_type`, optional `isin`, optional `figi`, optional `provider_symbol`, and optional metadata. Internally, instruments expose a stable key such as `NASDAQ:AAPL:USD`, `NGX:MTNN:NGN`, `XPAR:AIR:EUR`, or `XTKS:7203:JPY`.

### Data source modes

The aggregation layer supports the existing `DataMode` values:

- `client_supplied`: use only client-supplied quotes and FX rates, then validate and normalize them.
- `provided_by_us`: use configured provider adapters. v0.3 ships mock providers only.
- `hybrid`: prefer valid client-supplied data and fall back to configured mock providers when policy allows.

### Mock provider interface

The provider interface supports quote lookup, batch quote lookup, FX lookup, and market status lookup. v0.3 includes:

- `MockEquityProvider` for configured exchange-aware equity snapshots.
- `MockFXProvider` for configured mock FX rates.
- `ClientSuppliedProvider` for request-provided quotes and FX rates.

No paid or real external market data providers are connected in v0.3.

### FX conversion and quality scoring

Normalized market data includes the local price and currency, the loan currency, the converted price, bid/ask, volume, liquidity, volatility, recent return, optional order book, timestamp, source, provider name, exchange, market status, data quality score, warnings, and FX metadata. If asset currency equals loan currency, no FX conversion or FX warning is added. If currencies differ, the aggregator selects an FX rate according to policy and converts price and liquidity values into loan currency before the result is converted into the existing `MarketData` object.

The FX policy controls preferred source, fallback provider use, maximum FX age, stale FX haircut, conservative selection when sources disagree, and minimum FX quality score. Missing required FX is not guessed; the normalized result receives a `missing_required_fx` warning and a low data quality score so the existing risk engine heavily discounts or rejects the asset.

### Stale data and market status

Market data policy supports freshness thresholds by asset type and exchange, stale quote haircuts, and minimum quote quality. Fresh data receives high quality. Stale data receives warnings and a lower score. Closed markets add a `market_closed` warning but are not automatically rejected. Halted markets add a `halted` warning and convert into `MarketData.halted=True`, preserving compatibility with existing halted-asset risk logic.

Mock market status support covers `NASDAQ`, `NYSE`, `NGX`, `XPAR`, `XLON`, and `XTKS` using simple configured statuses rather than full exchange calendars.

### Normalization endpoint

`POST /market-data/normalize` is a validation and debugging endpoint. It accepts instruments or holdings, loan currency, data mode, market data policy, optional client-supplied quotes, and optional client-supplied FX rates. It returns normalized market data, per-instrument warnings, quality scores, FX decisions, and missing data. This endpoint does not replace the existing risk endpoints.

### Evaluation helper

`app/market_data/evaluation_adapter.py` exposes `normalize_market_data_for_evaluation(...)`, which returns core `MarketData` objects plus the full aggregation result for callers that want to evaluate a portfolio after normalization without changing existing endpoint contracts.

### Not included until v0.4

v0.3 does not implement live WebSocket streaming, live monitoring feeds, exchange calendar completeness, or real external provider connections. v0.4 is expected to handle live monitoring and event streams.

## v0.3.1 functional hardening notes

### Versioning

The project version is centralized in `app/version.py` and is currently `0.3.1`. The FastAPI app advertises this version, risk/lifecycle audit payloads use the same release family, and market-data normalization responses include `market_data_model_version` (`market-data-v0.3.1`).

### Stable market identity and evaluator keys

`InstrumentIdentity.stable_key` is the primary normalized market-data identity. It is composed from exchange, symbol, currency, and asset type (for example `NGX:MTNN:NGN:LISTED_EQUITY`) so two instruments with the same client `asset_id` or symbol cannot collide when they differ by exchange, currency, or asset type.

`asset_id` remains a client-facing or legacy reference. Normalization keeps stable-keyed `normalized_market_data` for review/debugging and separately produces evaluator-keyed `MarketData` through `MarketDataAggregationResult.to_core_market_data()` and the response fields `evaluator_market_data` / `evaluator_key_to_stable_key`. The evaluator map is keyed to the exact holding `asset_id` values passed to `CollateralRiskEngine.evaluate`, so a holding such as `NGX:MTNN:NGN` or a legacy holding `MTNN` with an explicit `InstrumentIdentity` receives correctly keyed market data without overwriting other instruments.

`POST /market-data/normalize` now rejects requests where both `instruments` and `holdings` are empty. If both are supplied, `instruments` take precedence for market identity; holdings are used only to produce evaluator-keyed output.

### Portfolio action currency requirements

When `buy`, inbound `transfer_security`, or `rebalance` creates a new security holding, the engine no longer assigns the loan currency as the new asset currency. The currency must be inferable from normalized `MarketData.metadata["instrument"]["currency"]`, explicit market-data metadata, or an instrument-style asset id such as `NGX:MTNN:NGN`. If no currency can be determined, the action is rejected with a clear error. Legacy USD-only snapshot mocks without metadata are treated as legacy USD snapshots for backward compatibility.

### Buy funded by draw

A buy with `funding_source = "draw"` or `"credit_draw"` must pass the same safe draw gate used by `/credit/draw/check` before the buy is projected. If the required shortfall exceeds the safe draw amount, the engine rejects the action rather than treating a partial draw as full buy funding.

### Liquidation plan completeness

`LiquidationPlan` now includes:

- `estimated_total_recovery`
- `unrecovered_target_amount`
- `plan_complete`

If available liquid collateral cannot meet the target recovery, `plan_complete` is `false` and the `reason` includes `insufficient_liquid_collateral_to_meet_target_recovery`.

### Market-data provider abstractions

The canonical provider contract is `app.market_data.providers.MarketDataProvider`, which supplies `RawQuote`, `FXRate`, and market-status data keyed by stable instrument identity. The old snapshot contract in `app.market_data.base` is retained only as a documented legacy compatibility layer (`LegacySnapshotProvider`), and `app.market_data.mock_provider.MockMarketDataProvider` is marked as a deprecated legacy snapshot mock. New tests and integrations should use `MockEquityProvider`, `MockFXProvider`, or `ClientSuppliedProvider`.

### Other hardening

- Conservative FX selection first filters sources by quality threshold before selecting the conservative acceptable rate; if no source passes, the FX decision is marked below threshold instead of blindly selecting the lowest stale rate.
- Numeric inputs validate positive FX rates/prices, positive bid/ask when supplied, `bid <= ask`, quality scores and LTV/haircut-like rates in `[0, 1]`, positive max-age windows, non-negative loan components, and non-negative holding quantities.
- Backtesting compares flat LTV with the lifecycle safe credit limit rather than raw approved credit.
- Stress scenarios now shock order-book bid and ask depth: prices follow price/spread shocks, quantities follow volume shocks, and liquidity collapse materially reduces visible depth.

## v0.4/v0.4.1 Monitoring engine, internal event stream, and market data update ingestion

v0.4 adds an internal monitoring layer in `app/monitoring` for continuously re-evaluating registered collateral accounts when account state or market data changes. The monitoring layer uses the existing v0.3 market data aggregation path and the existing `CreditLifecycleEngine.monitor(...)` flow; it does not rewrite the core evaluator or simplify risk logic.

### Monitored account registry

The monitoring registry stores `MonitoredAccount` records containing account reference, holdings, pledged cash, loan, loan currency, policy, market data mode/policy, optional client-supplied quotes and FX rates, monitoring status, last evaluation summary, latest margin state, latest available credit, market-data warnings, last check time, and next check time.

The API exposes:

- `POST /monitoring/accounts` to register an account. `run_initial_evaluation` defaults to `true`; active accounts run the initial evaluation and emit initial events, while paused/disabled accounts can be stored without evaluation by setting `run_initial_evaluation=false`. When evaluation is skipped, `last_evaluation`, `last_margin_state`, and `next_check_after` remain `null` and no monitoring tick event is emitted. If initial evaluation fails, registration rolls back and no partial account remains.
- `GET /monitoring/accounts/{account_ref}` to retrieve current monitored account state and last evaluation summary.
- `GET /monitoring/accounts` to list registered monitored accounts with status and latest risk state.
- `PATCH /monitoring/accounts/{account_ref}/status` to set `monitoring_status` to `active`, `paused`, or `disabled` and audit the previous and new status.
- `DELETE /monitoring/accounts/{account_ref}` to remove a monitored account from the in-memory registry. Delete is separate from disable and does not first mark the account disabled.

Monitoring status controls evaluation: active accounts are included in `list_active` and global ticks; paused and disabled accounts are excluded from global ticks. Single-account ticks reject paused/disabled accounts unless `?force=true` is supplied. v0.4.1 permits forced ticks for both paused and disabled accounts for operator diagnostics, and audits/evaluates them without changing status.

Registration that runs an initial evaluation normalizes current market data with the v0.3 aggregator, runs the lifecycle monitor evaluation, stores the latest state, emits an initial `monitoring_tick_completed` event, and emits entry events if the account starts in `margin_call` or `liquidation`.

### Repository interfaces and development adapters

Monitoring storage is behind repository/cache interfaces:

- `MonitoredAccountRepository`
- `MonitoringEventRepository`
- `MarketDataCache`

The included `InMemoryMonitoredAccountRepository`, `InMemoryMonitoringEventRepository`, and `InMemoryMarketDataCache` are development/test adapters only. They return/store copies where practical to reduce accidental mutation, but they are not durable, are not cross-process stores, and should not be used as production persistence. Production deployments should replace them with durable implementations backed by Postgres, DynamoDB, Redis, S3, or another production store appropriate for the deployment's durability, replay, retention, and consistency requirements.

### Manual monitoring ticks and scheduling policy

v0.4 intentionally does not start a background scheduler. Manual tick endpoints are provided:

- `POST /monitoring/accounts/{account_ref}/tick` evaluates one active account, or a paused/disabled account only when `force=true` is supplied.
- `POST /monitoring/tick` evaluates all active monitored accounts only.

Each tick refreshes/reuses market data through `MarketDataAggregator`, runs `CreditLifecycleEngine.monitor(...)`, compares prior and new state, emits only meaningful events, updates the account's last evaluation fields, and writes audit records.

The simple scheduling abstraction computes `next_check_after` with these default intervals:

- `safe`: 15 minutes
- `watch`: 5 minutes
- `restrict_new_borrowing`: 1 minute
- `margin_call`: 30 seconds
- `liquidation`: immediate

### Event types, severities, and deduplication

Monitoring events support these event types:

- `monitoring_tick_completed`
- `risk_state_changed`
- `available_credit_changed`
- `margin_call_triggered`
- `liquidation_triggered`
- `market_data_degraded`
- `fx_missing`
- `monitoring_error`

Severity levels are `info`, `warning`, and `critical`. Safe ticks are informational and unchanged informational ticks are not persisted repeatedly by default. Watch/restrict/margin-call conditions are warnings by default, liquidation is critical, missing FX can become warning/critical depending on resulting exposure, and monitoring errors are warning/critical depending on failure type.

Deduplication rules avoid event spam: state-change events require an actual margin-state transition, credit-change events require configured absolute or percentage thresholds, market-data degradation requires warning/quality deterioration, missing-FX events fire when missing required FX appears, and margin/liquidation events fire only when entering those states. v0.4.1 deduplication uses `MonitoringThresholds.dedupe_ttl_seconds` (default 300 seconds), so the same dedupe key is suppressed only within the TTL and may emit again after the TTL. This lets conditions such as missing FX resolve and later reappear as a new event.

### Market data update ingestion

`POST /monitoring/market-data/update` is an internal ingestion interface for future provider adapters or internal jobs. It is not an external provider WebSocket and does not connect to any paid/real provider stream.

The endpoint accepts quote updates, FX updates, affected instrument hints, source, and `trigger_tick`. Updates are merged into the in-memory market data cache, affected accounts are identified by stable key, exact asset id, or unambiguous symbol, and the response returns affected account refs plus ambiguity warnings. Symbol-only lookup/update is allowed only when the symbol maps to one stable instrument identity; ambiguous symbols return warnings and are not silently applied to unrelated instruments. The cache provider always permits stable-key and exact-asset-id lookup, but it does not serve symbol-only cached quotes when a symbol is ambiguous across exchanges or currencies. FX affected-account detection uses `InstrumentIdentity.from_holding(holding).currency` rather than relying only on `holding.currency`, so stable identities like `NGX:MTNN:NGN` can still be matched even if the holding currency field is stale or wrong. If `trigger_tick=true`, affected active accounts are evaluated immediately.

### Event retrieval and internal stream

Events can be retrieved with:

- `GET /monitoring/events` with optional `account_ref`, `event_type`, `severity`, and `limit` filters; results are newest first.
- `GET /monitoring/events/{event_id}` for a single event.
- `GET /monitoring/events/stream` for an internal snapshot `text/event-stream` Server-Sent Events response built with FastAPI `StreamingResponse` and no additional heavy dependency. It streams currently stored events and then closes; if no events are available it emits a readiness comment (`: monitoring stream ready`) and closes. It is not a live external WebSocket or long-running provider feed.

### Audit coverage and non-goals

v0.4 audit records cover account registration, status changes, account deletion, monitoring ticks, emitted events, monitoring errors, and market data update ingestion. Audit payloads include account/event identifiers, previous/new state where applicable, market-data warnings, missing data, model versions, and lifecycle evaluation audit ids.

v0.4/v0.4.1 does **not** add real external data-provider WebSockets, does **not** execute broker orders, and does **not** connect a production database. It is an internal monitoring/event foundation designed so production storage and provider adapters can be plugged in behind the interfaces later.

## v0.5A Historical Data + Interest Accrual Foundation

v0.5A adds the institutional validation foundation for historical simulations without changing the core collateral risk evaluator or existing API endpoints.

- Historical provider clients live under `app/historical_data/` and use environment variables via `os.getenv`; missing credentials do not break imports or normal unit tests.
- GitHub Actions Secrets are the source of truth for real provider credentials. `.env.example` documents variable names only; real keys must never be committed and a local `.env` file is not required.
- Provider integration is isolated in `.github/workflows/provider-integration.yml`, is `workflow_dispatch` only, and does not run on push or pull request.
- Retrieval is cache-first through `HISTORICAL_DATA_CACHE_DIR`; `force_refresh=true` is required to bypass valid cached data.
- The official validation universe includes US ETFs/equities, NGX equities, loan currencies NGN/USD/EUR, and FX pairs USD/NGN, NGN/USD, EUR/USD, USD/EUR, EUR/NGN, and NGN/EUR from 2018-01-01 through the configured/latest available end date.
- Loan interest accrual supports simple and compound accrual, daily/monthly/quarterly/yearly scheduling, actual/365, actual/360, and 30/360 day counts, plus client-supplied mode for externally calculated balances.
- Build planning is available with `python -m app.simulations.build_official_dataset --dry-run`, which records planned calls without spending real provider API quota.

### v0.5A Historical Data and Interest Foundation
v0.5A establishes historical data connectors, cache-first retrieval, official validation universe metadata, and the loan interest accrual foundation. `.env.example` is documentation only; GitHub Actions Secrets are the source of truth for real provider credentials. The provider integration workflow is manual-only, and provider integration tests are opt-in via `RUN_PROVIDER_INTEGRATION_TESTS=true` so normal CI does not consume API quota. `python -m app.simulations.build_official_dataset --dry-run` plans calls and writes a manifest without using provider quota. Full replay, simulation, and reporting are known limitations planned for v0.5B.

## v0.5B Official Historical Replay and Validation Evidence

v0.5B adds the official validation simulation package used to support institutional whitepaper evidence. The runner compares flat LTV lending, static haircut lending, and the dynamic collateral risk engine using cache-first historical datasets plus deterministic synthetic stress overlays.

### What is included

- Historical replay that converts cached historical bars into daily `MarketData` snapshots.
- Rolling volatility, liquidity, bid/ask spread, synthetic order book, and stale/missing data handling.
- Multi-currency replay for USD, NGN, and EUR loan cases with historical FX conversion support.
- Interest-aware loan simulation that preserves principal, accrued interest, and fees separately.
- Baseline comparison for configurable flat LTV and static haircut policies.
- Dynamic engine comparison using lifecycle safe credit limit semantics.
- Monitoring-style transition metrics for safe, watch, margin call, and liquidation states.
- Evidence outputs: JSON metrics, CSV metrics, Markdown reports, provider coverage, data methodology, interest methodology, and simulation assumptions.

### Commands

Build or inspect the official dataset manifest without provider calls:

```bash
python -m app.simulations.build_official_dataset --dry-run
```

Dry-run the validation runner and list scenarios:

```bash
python -m app.simulations.run_official_validation --dry-run --scenario all
```

Run an evidence package generation into an output directory:

```bash
python -m app.simulations.run_official_validation --dataset-manifest path/to/manifest.json --output-dir simulation_outputs
```


## v0.5C Simulation Readiness and Evidence Integrity Hardening

The official validation workflow is cache-first and uses a canonical normalized replay cache: equity data is stored as `HistoricalSeries` payloads and FX data as `HistoricalFXSeries` payloads. Replay no longer depends on raw provider response shapes; incompatible provider-native cache files should be skipped with warnings rather than treated as replay evidence.

FX is evaluated by replay date using date-indexed curves with nearest-prior lookup, inverse-pair support, stale-rate flags, and missing-FX flags. If an asset currency differs from the loan currency and required FX is unavailable, the loan-currency price, bid/ask, dollar volume, and order book are suppressed conservatively so local prices cannot be mistaken for loan-currency prices.

Validation compares three actual replay outputs: flat LTV, static haircut, and the dynamic lifecycle engine. Scenario base LTV, risk appetite, loan terms, and initial draw assumptions are applied consistently. The `thin_liquidity_portfolio` uses deterministic synthetic-only `THIN` bars by seed and is documented as excluded from provider dataset requirements.

Stress overlays include price gaps, FX devaluation, volume collapse, spread widening, order-book thinning, trading halt, stale market data, missing FX, single-name crash, correlated selloff, and combined severe stress. Monitoring replay transition events are labeled as simulated transition events unless an actual monitoring service is explicitly used.

### Running fixture or cache-backed simulation

```bash
python -m app.simulations.run_official_validation --dataset-manifest path/to/manifest.json --output-dir simulation_outputs
```

### Running the real provider dataset build manually

Provider-backed builds remain manual and require explicit secrets outside normal CI:

```bash
python -m app.simulations.build_official_dataset --providers alpaca,ngnmarket,alpha_vantage --output-dir simulation_outputs
```

After the provider dataset is built, run official validation against the generated manifest:

```bash
python -m app.simulations.run_official_validation --dataset-manifest simulation_outputs/official_dataset_manifest.json --output-dir simulation_outputs
```
