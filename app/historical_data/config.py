from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class HistoricalDataConfig:
    alpaca_auth_mode: str = field(
        default_factory=lambda: os.getenv("ALPACA_AUTH_MODE", "trading")
    )
    alpaca_api_key: str | None = field(
        default_factory=lambda: os.getenv("ALPACA_API_KEY")
    )
    alpaca_secret_key: str | None = field(
        default_factory=lambda: os.getenv("ALPACA_SECRET_KEY")
    )
    alpaca_base_url: str = field(
        default_factory=lambda: os.getenv(
            "ALPACA_BASE_URL", "https://data.alpaca.markets"
        )
    )
    alpha_vantage_api_key: str | None = field(
        default_factory=lambda: os.getenv("ALPHA_VANTAGE_API_KEY")
    )
    alpha_vantage_base_url: str = field(
        default_factory=lambda: os.getenv(
            "ALPHA_VANTAGE_BASE_URL", "https://www.alphavantage.co/query"
        )
    )
    ngnmarket_api_key: str | None = field(
        default_factory=lambda: os.getenv("NGNMARKET_API_KEY")
    )
    ngnmarket_base_url: str = field(
        default_factory=lambda: os.getenv(
            "NGNMARKET_BASE_URL", "https://api.ngnmarket.com/v1"
        )
    )
    cache_dir: str = field(
        default_factory=lambda: os.getenv(
            "HISTORICAL_DATA_CACHE_DIR", "./data/historical_cache"
        )
    )
    simulation_output_dir: str = field(
        default_factory=lambda: os.getenv(
            "SIMULATION_OUTPUT_DIR", "./data/simulation_results"
        )
    )
    run_provider_integration_tests: bool = field(
        default_factory=lambda: (
            os.getenv("RUN_PROVIDER_INTEGRATION_TESTS", "false").lower() == "true"
        )
    )


def load_config() -> HistoricalDataConfig:
    return HistoricalDataConfig()
