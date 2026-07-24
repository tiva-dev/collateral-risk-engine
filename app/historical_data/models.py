from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from app.market_data.identity import InstrumentIdentity


def canonical_series_payload(
    series: HistoricalSeries | HistoricalFXSeries,
) -> dict[str, Any]:
    """Return the provider-independent representation accepted by official replay."""
    from dataclasses import asdict

    payload = asdict(series)
    payload["cache_schema"] = (
        "historical_fx_series/v1"
        if isinstance(series, HistoricalFXSeries)
        else "historical_series/v1"
    )
    return payload


@dataclass(frozen=True)
class HistoricalBar:
    instrument: str
    timestamp: datetime | date
    open: float
    high: float
    low: float
    close: float
    adjusted_close: float | None = None
    volume: float = 0.0
    value_traded: float | None = None
    currency: str = "USD"
    source: str = "historical_provider"
    provider_name: str = "unknown"
    instrument_identity: InstrumentIdentity | None = None
    data_quality_score: float = 1.0
    warnings: list[str] = field(default_factory=list)
    raw_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HistoricalFXRate:
    from_currency: str
    to_currency: str
    rate: float
    timestamp: datetime | date
    source: str = "historical_provider"
    provider_name: str = "unknown"
    quality_score: float = 1.0
    warnings: list[str] = field(default_factory=list)
    raw_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HistoricalSeries:
    instrument: str
    bars: list[HistoricalBar]
    provider_name: str
    retrieved_at: datetime
    start_date: date
    end_date: date
    interval: str = "1d"
    warnings: list[str] = field(default_factory=list)
    data_quality_summary: dict[str, Any] = field(default_factory=dict)
    instrument_identity: InstrumentIdentity | None = None


@dataclass(frozen=True)
class HistoricalFXSeries:
    from_currency: str
    to_currency: str
    rates: list[HistoricalFXRate]
    provider_name: str
    retrieved_at: datetime
    start_date: date
    end_date: date
    warnings: list[str] = field(default_factory=list)
    data_quality_summary: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class HistoricalDatasetManifest:
    dataset_id: str
    provider: str
    universe: dict[str, Any]
    instruments: list[str]
    fx_pairs: list[str]
    start_date: date
    end_date: date
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    cache_paths: list[str] = field(default_factory=list)
    raw_response_paths: list[str] = field(default_factory=list)
    checksum: str = ""
    provider_quota_metadata: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    missing_symbols: list[str] = field(default_factory=list)
    earliest_available_date_by_symbol: dict[str, date | str] = field(
        default_factory=dict
    )
    methodology_notes: list[str] = field(default_factory=list)
    missing_symbol_reasons: dict[str, str] = field(default_factory=dict)
    provider_coverage_summary: dict[str, Any] = field(default_factory=dict)
    instrument_identities: dict[str, dict[str, Any]] = field(default_factory=dict)
