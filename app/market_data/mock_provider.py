from __future__ import annotations

from app.core.models import MarketData, OrderBook, OrderBookLevel
from app.market_data.base import LegacySnapshotProvider


class MockMarketDataProvider(LegacySnapshotProvider):
    """Deprecated legacy snapshot mock; use MockEquityProvider for RawQuote tests."""

    def __init__(self, snapshots: dict[str, MarketData] | None = None) -> None:
        self.snapshots = snapshots or default_snapshots()

    def get_snapshot(self, asset_ids: list[str]) -> dict[str, MarketData]:
        return {
            asset_id: self.snapshots[asset_id]
            for asset_id in asset_ids
            if asset_id in self.snapshots
        }


def default_snapshots() -> dict[str, MarketData]:
    return {
        "AAPL": MarketData(
            asset_id="AAPL",
            last_price=190.0,
            bid=189.98,
            ask=190.02,
            average_daily_volume=60_000_000,
            average_dollar_volume=11_400_000_000,
            volatility_30d=0.28,
            volatility_90d=0.31,
            recent_return_1d=-0.012,
            data_quality_score=0.99,
            order_book=OrderBook(
                bids=[
                    OrderBookLevel(price=189.98, quantity=5_000),
                    OrderBookLevel(price=189.95, quantity=10_000),
                    OrderBookLevel(price=189.90, quantity=25_000),
                ]
            ),
        ),
        "NVDA": MarketData(
            asset_id="NVDA",
            last_price=900.0,
            bid=899.50,
            ask=900.50,
            average_daily_volume=45_000_000,
            average_dollar_volume=40_500_000_000,
            volatility_30d=0.72,
            volatility_90d=0.68,
            intraday_volatility=0.85,
            recent_return_1d=-0.055,
            data_quality_score=0.98,
            order_book=OrderBook(
                bids=[
                    OrderBookLevel(price=899.50, quantity=300),
                    OrderBookLevel(price=898.80, quantity=700),
                    OrderBookLevel(price=897.20, quantity=2_000),
                    OrderBookLevel(price=892.00, quantity=5_000),
                ]
            ),
        ),
        "SPY": MarketData(
            asset_id="SPY",
            last_price=520.0,
            bid=519.99,
            ask=520.01,
            average_daily_volume=75_000_000,
            average_dollar_volume=39_000_000_000,
            volatility_30d=0.18,
            volatility_90d=0.20,
            recent_return_1d=-0.004,
            data_quality_score=0.99,
        ),
        "THIN": MarketData(
            asset_id="THIN",
            last_price=10.0,
            bid=9.80,
            ask=10.20,
            average_daily_volume=7_500,
            average_dollar_volume=75_000,
            volatility_30d=1.10,
            volatility_90d=0.95,
            recent_return_1d=-0.13,
            data_quality_score=0.80,
            order_book=OrderBook(
                bids=[
                    OrderBookLevel(price=9.80, quantity=500),
                    OrderBookLevel(price=9.10, quantity=1_000),
                    OrderBookLevel(price=8.25, quantity=1_500),
                ]
            ),
        ),
    }
