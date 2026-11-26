"""Continuous market-data ingestion for monitored instruments.

The instrument universe is every instrument held by a monitored account in
any tenant, plus an optional watchlist. US equities come from Alpaca (latest
trade plus daily bars); NGX equities from NGNMarket daily bars. Volatility and
average volume are computed from observed bars only. Bid/ask are not observed
from these feeds, so they are left empty and flagged rather than synthesised;
the engine then applies its documented spread assumptions.
"""

from __future__ import annotations

import json
import math
import os
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from app.core.enums import AssetType
from app.market_data.identity import InstrumentIdentity
from app.market_data.providers import RawQuote
from app.platform.db import Database

US_EXCHANGES = {"NASDAQ", "NYSE", "ARCA", "NYSEARCA", "BATS", "AMEX", "US", "IEX"}
NGX_EXCHANGES = {"NGX", "NGSE", "NSE"}


def _annualised_vol(returns: list[float], window: int) -> float | None:
    vals = [r for r in returns[-window:] if r is not None]
    if len(vals) < max(5, window // 3):
        return None
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
    return math.sqrt(var) * math.sqrt(252)


def parse_stable_key(key: str) -> InstrumentIdentity | None:
    parts = key.split(":")
    if len(parts) != 4:
        return None
    exchange, symbol, currency, asset_type = parts
    try:
        at = AssetType(asset_type.lower())
    except ValueError:
        return None
    return InstrumentIdentity(asset_id=f"{exchange}:{symbol}:{currency}", symbol=symbol, exchange=exchange,
                              currency=currency, asset_type=at)


def universe(db: Database) -> list[InstrumentIdentity]:
    with db.transaction("*") as conn:
        rows = conn.fetchall(
            "SELECT DISTINCT i.instrument_key FROM account_instruments i JOIN monitored_accounts a "
            "ON a.tenant_id=i.tenant_id AND a.account_ref=i.account_ref WHERE a.monitoring_status='active'")
    keys = {r["instrument_key"] for r in rows}
    keys |= {k.strip() for k in os.environ.get("CRI_WATCHLIST", "").split(",") if k.strip()}
    out = {}
    for k in keys:
        ident = parse_stable_key(k)
        if ident is not None:
            out[ident.stable_key] = ident
    return [out[k] for k in sorted(out)]


def quote_from_bars(identity: InstrumentIdentity, bars: list[Any], provider: str,
                    latest_price: float | None = None, latest_time: datetime | None = None,
                    now: datetime | None = None) -> RawQuote:
    now = now or datetime.now(UTC)
    closes = [float(b.adjusted_close or b.close) for b in bars if (b.adjusted_close or b.close)]
    if not closes:
        raise ValueError(f"no bars for {identity.stable_key}")
    returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    vols = [float(b.volume or 0.0) for b in bars[-20:]]
    observed_volumes = [v for v in vols if v > 0]
    adv = sum(observed_volumes) / len(observed_volumes) if observed_volumes else None
    last_bar = bars[-1]
    bar_ts = last_bar.timestamp
    if not isinstance(bar_ts, datetime):
        # Daily bars carry a date only: stamp them at the exchange close, but
        # never later than now (today's bar may still be forming).
        close_utc = time(13, 30) if identity.exchange.upper() in NGX_EXCHANGES else time(21, 0)
        bar_ts = datetime.combine(bar_ts, close_utc, tzinfo=UTC)
    elif bar_ts.tzinfo is None:
        bar_ts = bar_ts.replace(tzinfo=UTC)
    bar_ts = min(bar_ts, now)
    price = latest_price if latest_price and latest_price > 0 else closes[-1]
    ts = latest_time if latest_price and latest_time else bar_ts
    warnings = ["bid_ask_not_observed"]
    if adv is None:
        warnings.append("volume_not_observed")
    age_days = (now - ts).total_seconds() / 86400
    quality = 1.0 if age_days <= 4 else max(0.3, 1.0 - 0.1 * age_days)
    if age_days > 4:
        warnings.append(f"price_age_days={age_days:.1f}")
    return RawQuote(
        instrument=identity,
        local_price=price,
        average_daily_volume=adv,
        average_dollar_volume=adv * price if adv else None,
        volatility_30d=_annualised_vol(returns, 30),
        volatility_90d=_annualised_vol(returns, 90),
        recent_return_1d=(math.exp(returns[-1]) - 1) if returns else None,
        timestamp=ts,
        source="provided_by_us",
        provider_name=provider,
        data_quality_score=round(quality, 3),
        warnings=warnings,
        metadata={"bars_used": len(bars), "price_basis": "latest_trade" if latest_price else "daily_close",
                  "last_bar_date": str(last_bar.timestamp)},
    )


def _alpaca_latest_trades(symbols: list[str]) -> dict[str, tuple[float, datetime]]:
    from app.historical_data.alpaca import AlpacaTradingHistoricalProvider

    prov = AlpacaTradingHistoricalProvider()
    url = (prov.config.alpaca_base_url.rstrip("/") + "/v2/stocks/trades/latest?feed=iex&symbols="
           + ",".join(symbols))
    payload = prov._request_json(url)
    out = {}
    for sym, t in (payload.get("trades") or {}).items():
        try:
            out[sym] = (float(t["p"]), datetime.fromisoformat(t["t"]))
        except (KeyError, ValueError, TypeError):
            continue
    return out


def fetch_quotes(instruments: list[InstrumentIdentity], today: date | None = None) -> tuple[dict[str, RawQuote], list[dict[str, str]]]:
    today = today or datetime.now(UTC).date()
    start = today - timedelta(days=200)
    quotes: dict[str, RawQuote] = {}
    errors: list[dict[str, str]] = []
    us = [i for i in instruments if i.exchange.upper() in US_EXCHANGES]
    ngx = [i for i in instruments if i.exchange.upper() in NGX_EXCHANGES]
    for i in instruments:
        if i not in us and i not in ngx:
            errors.append({"instrument": i.stable_key, "error": "no live provider for exchange"})
    if us:
        from app.historical_data.alpaca import AlpacaTradingHistoricalProvider

        prov = AlpacaTradingHistoricalProvider()
        try:
            latest = _alpaca_latest_trades([i.symbol for i in us])
        except Exception as exc:  # noqa: BLE001 - fall back to daily closes, flagged
            latest = {}
            errors.append({"instrument": "alpaca:latest_trades", "error": str(exc)[:300]})
        for ident in us:
            try:
                series = prov.fetch_equity_history(ident.symbol, start, today, force_refresh=True, feed="iex")
                lp = latest.get(ident.symbol)
                quotes[ident.stable_key] = quote_from_bars(ident, series.bars, "alpaca",
                                                           lp[0] if lp else None, lp[1] if lp else None)
            except Exception as exc:  # noqa: BLE001 - isolate per instrument
                errors.append({"instrument": ident.stable_key, "error": str(exc)[:300]})
    if ngx:
        from app.historical_data.ngnmarket import NGNMarketHistoricalProvider

        nprov = NGNMarketHistoricalProvider()
        for ident in ngx:
            try:
                series = nprov.fetch_equity_history(ident.symbol, start, today, force_refresh=True)
                quotes[ident.stable_key] = quote_from_bars(ident, series.bars, "ngnmarket")
            except Exception as exc:  # noqa: BLE001
                errors.append({"instrument": ident.stable_key, "error": str(exc)[:300]})
    return quotes, errors


def record_run(db: Database, started: datetime, n_instruments: int, n_quotes: int,
               errors: list[dict[str, str]]) -> str:
    run_id = "ing_" + uuid.uuid4().hex[:16]
    with db.transaction("*") as conn:
        conn.execute(
            "INSERT INTO ingestion_runs (run_id, started_at, finished_at, instruments, quotes_written, errors) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (run_id, started.isoformat(), datetime.now(UTC).isoformat(), n_instruments, n_quotes,
             json.dumps(errors)))
    return run_id
