"""Durable, tenant-scoped implementations of the monitoring storage contracts."""

from __future__ import annotations

import builtins
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.market_data.identity import InstrumentIdentity
from app.market_data.providers import BaseProvider, FXRate, RawQuote
from app.monitoring.market_updates import (
    CachedMarketData,
    CachedMarketDataProvider,
    InMemoryMarketDataCache,
    MarketDataCache,
)
from app.monitoring.models import (
    MonitoredAccount,
    MonitoringEvent,
    MonitoringEventType,
    MonitoringSeverity,
    MonitoringStatus,
)
from app.monitoring.repositories import (
    EventAppendResult,
    MonitoredAccountRepository,
    MonitoringEventRepository,
)
from app.platform import codec
from app.platform.db import current_connection


def _value(x: Any) -> str | None:
    if x is None:
        return None
    return x.value if hasattr(x, "value") else str(x)


def _instrument_keys(account: MonitoredAccount) -> set[str]:
    keys: set[str] = set()
    for holding in account.holdings:
        identity = InstrumentIdentity.from_holding(holding)
        keys |= {
            holding.asset_id,
            holding.asset_id.upper(),
            identity.stable_key,
            identity.symbol,
            identity.symbol.upper(),
        }
    return keys


class SqlMonitoredAccountRepository(MonitoredAccountRepository):
    def __init__(self, tenant_id: str) -> None:
        if not tenant_id or tenant_id == "*":
            raise ValueError("account repository must be bound to a single tenant")
        self.tenant_id = tenant_id

    def save(self, account: MonitoredAccount) -> MonitoredAccount:
        conn = current_connection()
        now = datetime.now(UTC).isoformat()
        state = codec.dumps(account)
        existing = conn.fetchone(
            "SELECT version FROM monitored_accounts WHERE tenant_id=%s AND account_ref=%s",
            (self.tenant_id, account.account_ref),
        )
        if existing is None:
            conn.execute(
                "INSERT INTO monitored_accounts (tenant_id, account_ref, state, monitoring_status, margin_state, "
                "version, created_at, updated_at) VALUES (%s,%s,%s,%s,%s,1,%s,%s)",
                (self.tenant_id, account.account_ref, state, _value(account.monitoring_status),
                 _value(account.last_margin_state), now, now),
            )
        else:
            conn.execute(
                "UPDATE monitored_accounts SET state=%s, monitoring_status=%s, margin_state=%s, "
                "version=version+1, updated_at=%s WHERE tenant_id=%s AND account_ref=%s",
                (state, _value(account.monitoring_status), _value(account.last_margin_state), now,
                 self.tenant_id, account.account_ref),
            )
        conn.execute(
            "DELETE FROM account_instruments WHERE tenant_id=%s AND account_ref=%s",
            (self.tenant_id, account.account_ref),
        )
        for key in sorted(_instrument_keys(account)):
            conn.execute(
                "INSERT INTO account_instruments (tenant_id, account_ref, instrument_key) VALUES (%s,%s,%s)",
                (self.tenant_id, account.account_ref, key),
            )
        return codec.loads(state)

    def update(self, account: MonitoredAccount) -> MonitoredAccount:
        return self.save(account)

    def get(self, account_ref: str) -> MonitoredAccount | None:
        conn = current_connection()
        # Serialise concurrent work on one account for the rest of the transaction.
        conn.advisory_lock(f"account:{self.tenant_id}:{account_ref}")
        row = conn.fetchone(
            "SELECT state FROM monitored_accounts WHERE tenant_id=%s AND account_ref=%s",
            (self.tenant_id, account_ref),
        )
        return codec.loads(row["state"]) if row else None

    def delete(self, account_ref: str) -> bool:
        conn = current_connection()
        conn.execute(
            "DELETE FROM account_instruments WHERE tenant_id=%s AND account_ref=%s",
            (self.tenant_id, account_ref),
        )
        cur = conn.execute(
            "DELETE FROM monitored_accounts WHERE tenant_id=%s AND account_ref=%s",
            (self.tenant_id, account_ref),
        )
        return (cur.rowcount or 0) > 0

    def list(self) -> list[MonitoredAccount]:
        rows = current_connection().fetchall(
            "SELECT state FROM monitored_accounts WHERE tenant_id=%s ORDER BY account_ref",
            (self.tenant_id,),
        )
        return [codec.loads(r["state"]) for r in rows]

    def list_active(self) -> builtins.list[MonitoredAccount]:
        rows = current_connection().fetchall(
            "SELECT state FROM monitored_accounts WHERE tenant_id=%s AND monitoring_status=%s ORDER BY account_ref",
            (self.tenant_id, MonitoringStatus.ACTIVE.value),
        )
        return [codec.loads(r["state"]) for r in rows]

    def list_by_instrument(self, stable_key_or_asset_id: str) -> builtins.list[MonitoredAccount]:
        rows = current_connection().fetchall(
            "SELECT DISTINCT a.account_ref, a.state FROM monitored_accounts a JOIN account_instruments i "
            "ON a.tenant_id=i.tenant_id AND a.account_ref=i.account_ref "
            "WHERE a.tenant_id=%s AND (i.instrument_key=%s OR i.instrument_key=%s) ORDER BY a.account_ref",
            (self.tenant_id, stable_key_or_asset_id, stable_key_or_asset_id.upper()),
        )
        return [codec.loads(r["state"]) for r in rows]


EventHook = Callable[[str, MonitoringEvent], None]


class SqlMonitoringEventRepository(MonitoringEventRepository):
    def __init__(self, tenant_id: str, on_created: EventHook | None = None) -> None:
        if not tenant_id or tenant_id == "*":
            raise ValueError("event repository must be bound to a single tenant")
        self.tenant_id = tenant_id
        self.on_created = on_created

    def append(self, event: MonitoringEvent, *, dedupe_ttl_seconds: int | None = None) -> EventAppendResult:
        conn = current_connection()
        if event.dedupe_key:
            conn.advisory_lock(f"dedupe:{self.tenant_id}:{event.dedupe_key}")
            row = conn.fetchone(
                "SELECT body, created_at FROM monitoring_events WHERE tenant_id=%s AND dedupe_key=%s "
                "ORDER BY created_at DESC LIMIT 1",
                (self.tenant_id, event.dedupe_key),
            )
            if row is not None:
                existing: MonitoringEvent = codec.loads(row["body"])
                if dedupe_ttl_seconds is None or event.created_at <= existing.created_at + timedelta(
                    seconds=dedupe_ttl_seconds
                ):
                    return EventAppendResult(existing, False)
        conn.execute(
            "INSERT INTO monitoring_events (event_id, tenant_id, account_ref, event_type, severity, dedupe_key, "
            "audit_id, created_at, body) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (event.event_id, self.tenant_id, event.account_ref, _value(event.event_type), _value(event.severity),
             event.dedupe_key, event.audit_id, event.created_at.isoformat(), codec.dumps(event)),
        )
        if self.on_created is not None:
            self.on_created(self.tenant_id, event)
        return EventAppendResult(codec.loads(codec.dumps(event)), True)

    def list(
        self,
        account_ref: str | None = None,
        event_type: MonitoringEventType | str | None = None,
        severity: MonitoringSeverity | str | None = None,
        limit: int = 100,
    ) -> list[MonitoringEvent]:
        sql = "SELECT body FROM monitoring_events WHERE tenant_id=%s"
        params: list[Any] = [self.tenant_id]
        if account_ref is not None:
            sql += " AND account_ref=%s"
            params.append(account_ref)
        if event_type is not None:
            sql += " AND event_type=%s"
            params.append(_value(event_type))
        if severity is not None:
            sql += " AND severity=%s"
            params.append(_value(severity))
        sql += " ORDER BY created_at DESC LIMIT %s"
        params.append(max(0, min(int(limit), 1000)))
        return [codec.loads(r["body"]) for r in current_connection().fetchall(sql, params)]

    def get(self, event_id: str) -> MonitoringEvent | None:
        row = current_connection().fetchone(
            "SELECT body FROM monitoring_events WHERE tenant_id=%s AND event_id=%s",
            (self.tenant_id, event_id),
        )
        return codec.loads(row["body"]) if row else None

    def update_event_audit_id(self, event_id: str, audit_id: str) -> MonitoringEvent | None:
        event = self.get(event_id)
        if event is None:
            return None
        event.audit_id = audit_id
        current_connection().execute(
            "UPDATE monitoring_events SET audit_id=%s, body=%s WHERE tenant_id=%s AND event_id=%s",
            (audit_id, codec.dumps(event), self.tenant_id, event_id),
        )
        return event


class SqlMarketDataCache(MarketDataCache):
    """Shared market-data store: latest quote per key plus append-only price history.

    Market data is global (not tenant data). Reads rebuild the same keyed view
    as the in-memory cache, including its ambiguous-symbol handling.
    """

    def __init__(self) -> None:
        self._provider = CachedMarketDataProvider(self)  # type: ignore[arg-type]

    def merge(self, quotes: dict[str, RawQuote], fx_rates: dict[tuple[str, str], FXRate], source: str,
              received_at: datetime | None = None) -> CachedMarketData:
        conn = current_connection()
        now = (received_at or datetime.now(UTC)).isoformat()
        for key, quote in quotes.items():
            body = codec.dumps(quote)
            params = (key, quote.instrument.stable_key, body, quote.timestamp.isoformat(), source, now)
            conn.execute(
                "INSERT INTO market_quotes (quote_key, stable_key, quote, observed_at, source, ingested_at) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (quote_key) DO UPDATE SET stable_key=excluded.stable_key, "
                "quote=excluded.quote, observed_at=excluded.observed_at, source=excluded.source, "
                "ingested_at=excluded.ingested_at",
                params,
            )
            conn.execute(
                "INSERT INTO market_quote_history (stable_key, price, observed_at, source, ingested_at) "
                "VALUES (%s,%s,%s,%s,%s)",
                (quote.instrument.stable_key, quote.local_price, quote.timestamp.isoformat(), source, now),
            )
        for (src, dst), rate in fx_rates.items():
            conn.execute(
                "INSERT INTO fx_rates (pair, rate, observed_at, ingested_at) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (pair) DO UPDATE SET rate=excluded.rate, observed_at=excluded.observed_at, "
                "ingested_at=excluded.ingested_at",
                (f"{src.upper()}/{dst.upper()}", codec.dumps(rate), rate.timestamp.isoformat(), now),
            )
        return self.snapshot()

    def _view(self) -> InMemoryMarketDataCache:
        conn = current_connection()
        view = InMemoryMarketDataCache()
        quotes = {r["quote_key"]: codec.loads(r["quote"]) for r in conn.fetchall(
            "SELECT quote_key, quote FROM market_quotes ORDER BY quote_key")}
        fx = {}
        for r in conn.fetchall("SELECT pair, rate FROM fx_rates"):
            a, b = r["pair"].split("/")
            fx[(a, b)] = codec.loads(r["rate"])
        view.merge(quotes, fx, "database")
        return view

    def is_symbol_ambiguous(self, symbol: str) -> bool:
        return self._view().is_symbol_ambiguous(symbol)

    def provider(self) -> BaseProvider:
        return self._provider

    def snapshot(self) -> CachedMarketData:
        return self._view().snapshot()
