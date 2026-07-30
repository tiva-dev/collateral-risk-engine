from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock

from pydantic import TypeAdapter

from app.monitoring.models import (
    MonitoredAccount,
    MonitoringEvent,
    MonitoringEventType,
    MonitoringSeverity,
    MonitoringStatus,
)


class MonitoredAccountRepository(ABC):
    """Storage contract for monitored accounts.

    The in-memory implementation is a development adapter. Production deployments
    should replace it with a database-backed repository such as Postgres,
    DynamoDB, Redis, S3, or another durable store.
    """

    @abstractmethod
    def save(self, account: MonitoredAccount) -> MonitoredAccount: ...

    @abstractmethod
    def get(self, account_ref: str) -> MonitoredAccount | None: ...

    @abstractmethod
    def delete(self, account_ref: str) -> bool: ...

    @abstractmethod
    def list_active(self) -> list[MonitoredAccount]: ...

    @abstractmethod
    def list_by_instrument(
        self, stable_key_or_asset_id: str
    ) -> list[MonitoredAccount]: ...

    @abstractmethod
    def update(self, account: MonitoredAccount) -> MonitoredAccount: ...

    @abstractmethod
    def list(self) -> list[MonitoredAccount]: ...


@dataclass(frozen=True)
class EventAppendResult:
    event: MonitoringEvent
    created: bool


class MonitoringEventRepository(ABC):
    """Storage contract for monitoring events; replace in-memory storage in production."""

    @abstractmethod
    def append(
        self, event: MonitoringEvent, *, dedupe_ttl_seconds: int | None = None
    ) -> EventAppendResult: ...

    @abstractmethod
    def list(
        self,
        account_ref: str | None = None,
        event_type: MonitoringEventType | str | None = None,
        severity: MonitoringSeverity | str | None = None,
        limit: int = 100,
    ) -> list[MonitoringEvent]: ...

    @abstractmethod
    def get(self, event_id: str) -> MonitoringEvent | None: ...

    def update_event_audit_id(
        self, event_id: str, audit_id: str
    ) -> MonitoringEvent | None: ...


class InMemoryMonitoredAccountRepository(MonitoredAccountRepository):
    def __init__(self) -> None:
        self._accounts: dict[str, MonitoredAccount] = {}
        self._instrument_index: dict[str, set[str]] = {}
        self._lock = RLock()

    def save(self, account: MonitoredAccount) -> MonitoredAccount:
        with self._lock:
            stored = deepcopy(account)
            self._accounts[stored.account_ref] = stored
            self._reindex(stored)
            return deepcopy(stored)

    def update(self, account: MonitoredAccount) -> MonitoredAccount:
        return self.save(account)

    def get(self, account_ref: str) -> MonitoredAccount | None:
        with self._lock:
            account = self._accounts.get(account_ref)
            return deepcopy(account) if account is not None else None

    def delete(self, account_ref: str) -> bool:
        with self._lock:
            existed = self._accounts.pop(account_ref, None) is not None
            for refs in self._instrument_index.values():
                refs.discard(account_ref)
            return existed

    def list(self) -> list[MonitoredAccount]:
        with self._lock:
            return [deepcopy(account) for account in self._accounts.values()]

    def list_active(self) -> list[MonitoredAccount]:
        return [
            account
            for account in self.list()
            if account.monitoring_status == MonitoringStatus.ACTIVE
        ]

    def list_by_instrument(self, stable_key_or_asset_id: str) -> list[MonitoredAccount]:
        key = stable_key_or_asset_id.upper()
        with self._lock:
            refs = set(self._instrument_index.get(stable_key_or_asset_id, set())) | set(
                self._instrument_index.get(key, set())
            )
            return [
                deepcopy(self._accounts[ref]) for ref in refs if ref in self._accounts
            ]

    def _reindex(self, account: MonitoredAccount) -> None:
        for refs in self._instrument_index.values():
            refs.discard(account.account_ref)
        from app.market_data.identity import InstrumentIdentity

        for holding in account.holdings:
            identity = InstrumentIdentity.from_holding(holding)
            keys = {
                holding.asset_id,
                holding.asset_id.upper(),
                identity.stable_key,
                identity.symbol,
                identity.symbol.upper(),
            }
            for key in keys:
                self._instrument_index.setdefault(key, set()).add(account.account_ref)


class InMemoryMonitoringEventRepository(MonitoringEventRepository):
    def __init__(self) -> None:
        self._events: list[MonitoringEvent] = []
        self._by_id: dict[str, MonitoringEvent] = {}
        self._dedupe_keys: dict[str, MonitoringEvent] = {}
        self._lock = RLock()

    def append(
        self, event: MonitoringEvent, *, dedupe_ttl_seconds: int | None = None
    ) -> EventAppendResult:
        with self._lock:
            if event.dedupe_key and event.dedupe_key in self._dedupe_keys:
                existing = self._dedupe_keys[event.dedupe_key]
                if (
                    dedupe_ttl_seconds is None
                    or event.created_at
                    <= existing.created_at + timedelta(seconds=dedupe_ttl_seconds)
                ):
                    return EventAppendResult(deepcopy(existing), False)
            stored = deepcopy(event)
            self._events.append(stored)
            self._by_id[stored.event_id] = stored
            if stored.dedupe_key:
                self._dedupe_keys[stored.dedupe_key] = stored
            return EventAppendResult(deepcopy(stored), True)

    def list(
        self,
        account_ref: str | None = None,
        event_type: MonitoringEventType | str | None = None,
        severity: MonitoringSeverity | str | None = None,
        limit: int = 100,
    ) -> list[MonitoringEvent]:
        with self._lock:
            events = sorted(
                self._events, key=lambda event: event.created_at, reverse=True
            )
        if account_ref is not None:
            events = [event for event in events if event.account_ref == account_ref]
        if event_type is not None:
            value = (
                event_type.value if hasattr(event_type, "value") else str(event_type)
            )
            events = [event for event in events if event.event_type.value == value]
        if severity is not None:
            value = severity.value if hasattr(severity, "value") else str(severity)
            events = [event for event in events if event.severity.value == value]
        return [deepcopy(event) for event in events[: max(0, min(limit, 1000))]]

    def get(self, event_id: str) -> MonitoringEvent | None:
        with self._lock:
            event = self._by_id.get(event_id)
            return deepcopy(event) if event is not None else None

    def update_event_audit_id(
        self, event_id: str, audit_id: str
    ) -> MonitoringEvent | None:
        with self._lock:
            event = self._by_id.get(event_id)
            if event is None:
                return None
            event.audit_id = audit_id
            return deepcopy(event)


class SQLiteMonitoredAccountRepository(MonitoredAccountRepository):
    """Durable monitored-account storage using the standard-library SQLite."""

    _adapter = TypeAdapter(MonitoredAccount)

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS monitored_accounts (
                    account_ref TEXT PRIMARY KEY,
                    payload BLOB NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    @classmethod
    def _load_account(cls, payload: bytes | str) -> MonitoredAccount:
        data = json.loads(payload)
        encoded_fx = data.get("client_supplied_fx_rates") or {}
        decoded_fx = {}
        for key, value in encoded_fx.items():
            parts = str(key).split(",", 1)
            if len(parts) != 2:
                raise ValueError(f"invalid persisted FX pair key: {key}")
            decoded_fx[(parts[0].upper(), parts[1].upper())] = value
        data["client_supplied_fx_rates"] = decoded_fx
        return cls._adapter.validate_python(data)

    def save(self, account: MonitoredAccount) -> MonitoredAccount:
        payload = self._adapter.dump_json(account)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO monitored_accounts(account_ref, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(account_ref) DO UPDATE SET
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (account.account_ref, payload, account.updated_at.isoformat()),
            )
        return deepcopy(account)

    def update(self, account: MonitoredAccount) -> MonitoredAccount:
        return self.save(account)

    def get(self, account_ref: str) -> MonitoredAccount | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM monitored_accounts WHERE account_ref=?",
                (account_ref,),
            ).fetchone()
        return self._load_account(row[0]) if row else None

    def delete(self, account_ref: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM monitored_accounts WHERE account_ref=?",
                (account_ref,),
            )
        return cursor.rowcount > 0

    def list(self) -> list[MonitoredAccount]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM monitored_accounts ORDER BY updated_at"
            ).fetchall()
        return [self._load_account(row[0]) for row in rows]

    def list_active(self) -> list[MonitoredAccount]:
        return [
            account
            for account in self.list()
            if account.monitoring_status == MonitoringStatus.ACTIVE
        ]

    def list_by_instrument(self, stable_key_or_asset_id: str) -> list[MonitoredAccount]:
        needle = stable_key_or_asset_id.upper()
        return [
            account
            for account in self.list()
            if any(
                needle
                in {
                    holding.asset_id.upper(),
                    holding.stable_key.upper(),
                }
                for holding in account.holdings
            )
        ]


class SQLiteMonitoringEventRepository(MonitoringEventRepository):
    """Durable event outbox with deduplication and queryable delivery state."""

    _adapter = TypeAdapter(MonitoringEvent)

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS monitoring_events (
                    event_id TEXT PRIMARY KEY,
                    account_ref TEXT,
                    event_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    dedupe_key TEXT,
                    created_at TEXT NOT NULL,
                    payload BLOB NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_account "
                "ON monitoring_events(account_ref, created_at)"
            )

    def append(
        self,
        event: MonitoringEvent,
        *,
        dedupe_ttl_seconds: int | None = None,
    ) -> EventAppendResult:
        with self._lock, self._connect() as connection:
            if event.dedupe_key:
                row = connection.execute(
                    """
                    SELECT payload, created_at FROM monitoring_events
                    WHERE dedupe_key=? ORDER BY created_at DESC LIMIT 1
                    """,
                    (event.dedupe_key,),
                ).fetchone()
                if row:
                    existing = self._adapter.validate_json(row[0])
                    created_at = datetime.fromisoformat(row[1])
                    if (
                        dedupe_ttl_seconds is None
                        or event.created_at
                        <= created_at + timedelta(seconds=dedupe_ttl_seconds)
                    ):
                        return EventAppendResult(existing, False)
            connection.execute(
                """
                INSERT INTO monitoring_events(
                    event_id, account_ref, event_type, severity,
                    dedupe_key, created_at, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.account_ref,
                    event.event_type.value,
                    event.severity.value,
                    event.dedupe_key,
                    event.created_at.isoformat(),
                    self._adapter.dump_json(event),
                ),
            )
        return EventAppendResult(deepcopy(event), True)

    def list(
        self,
        account_ref: str | None = None,
        event_type: MonitoringEventType | str | None = None,
        severity: MonitoringSeverity | str | None = None,
        limit: int = 100,
    ) -> list[MonitoringEvent]:
        clauses: list[str] = []
        params: list[object] = []
        if account_ref is not None:
            clauses.append("account_ref=?")
            params.append(account_ref)
        if event_type is not None:
            clauses.append("event_type=?")
            params.append(
                event_type.value if hasattr(event_type, "value") else str(event_type)
            )
        if severity is not None:
            clauses.append("severity=?")
            params.append(
                severity.value if hasattr(severity, "value") else str(severity)
            )
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(0, min(limit, 1000)))
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                f"SELECT payload FROM monitoring_events{where} "
                "ORDER BY created_at DESC LIMIT ?",  # nosec B608
                params,
            ).fetchall()
        return [self._adapter.validate_json(row[0]) for row in rows]

    def get(self, event_id: str) -> MonitoringEvent | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM monitoring_events WHERE event_id=?",
                (event_id,),
            ).fetchone()
        return self._adapter.validate_json(row[0]) if row else None

    def update_event_audit_id(
        self, event_id: str, audit_id: str
    ) -> MonitoringEvent | None:
        event = self.get(event_id)
        if event is None:
            return None
        event.audit_id = audit_id
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE monitoring_events SET payload=? WHERE event_id=?",
                (self._adapter.dump_json(event), event_id),
            )
        return event
