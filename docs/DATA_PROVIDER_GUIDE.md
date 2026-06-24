# Historical Data Provider Guide

Provider credentials are supplied through GitHub Actions Secrets for integration validation and through environment variables for local experimentation. Do not commit real API keys. `.env.example` is documentation only.

## Providers

- **AlpacaTradingHistoricalProvider**: US equity and ETF daily bars. `ALPACA_BASE_URL` must be `https://data.alpaca.markets`; the provider appends `/v2/stocks/bars` internally.
- **AlphaVantageHistoricalProvider**: FX fallback/cross-check and equity daily adjusted fallback. `ALPHA_VANTAGE_BASE_URL` is the query endpoint.
- **NGNMarketHistoricalProvider**: NGX company lists, company charts, FX history, and index endpoints using bearer-token authentication.

## Cache-first behavior

`HistoricalDataCache` stores raw/normalized JSON payloads and checksums under `HISTORICAL_DATA_CACHE_DIR`. Valid cache hits avoid provider calls unless `force_refresh=True`. Secret-like fields are redacted before cache writes.

## Integration workflow

`.github/workflows/provider-integration.yml` is manual only (`workflow_dispatch`). It maps expected GitHub Secrets into environment variables and must not echo secret values.

## v0.5A provider operations
Historical providers use cache-first retrieval and maintain separate raw response and normalized cache artifacts. Dry runs of the official dataset builder only print planned calls and create manifest metadata; they do not call external APIs or consume quota. Real credentials must be stored as GitHub Actions Secrets for the manual provider-integration workflow, not committed to the repository or logged. Provider integration tests are skipped unless `RUN_PROVIDER_INTEGRATION_TESTS=true` is set and provider-specific secrets are present.

Known limitation: full replay, simulation, and reporting are deferred to v0.5B.
