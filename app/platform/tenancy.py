"""Per-tenant engine services behind the existing API module's globals.

``app.api.routes`` reads module-level ``monitoring_service`` and
``monitoring_event_repo``. The platform replaces them with proxies that
resolve to the calling tenant's service, so every existing endpoint becomes
tenant-scoped without changing its code. A call made outside a request
transaction (for example while a streaming response is being iterated) opens
its own short transaction for the same tenant.
"""

from __future__ import annotations

import functools
from typing import Any

from app.lifecycle.service import CreditLifecycleEngine
from app.market_data.aggregator import MarketDataAggregator
from app.monitoring.service import MonitoringService
from app.platform.context import require_principal
from app.platform.db import Database, has_transaction
from app.platform.repositories import (
    SqlMarketDataCache,
    SqlMonitoredAccountRepository,
    SqlMonitoringEventRepository,
)


class TenantServices:
    def __init__(self, db: Database, lifecycle_engine: CreditLifecycleEngine, audit_logger: Any,
                 on_event_created: Any) -> None:
        self.db = db
        self.lifecycle_engine = lifecycle_engine
        self.audit_logger = audit_logger
        self.market_data_cache = SqlMarketDataCache()
        provider = self.market_data_cache.provider()
        self.aggregator = MarketDataAggregator(equity_provider=provider, fx_provider=provider)
        self.on_event_created = on_event_created
        self._services: dict[str, MonitoringService] = {}
        self._events: dict[str, SqlMonitoringEventRepository] = {}

    def events(self, tenant_id: str) -> SqlMonitoringEventRepository:
        if tenant_id not in self._events:
            self._events[tenant_id] = SqlMonitoringEventRepository(tenant_id, self.on_event_created)
        return self._events[tenant_id]

    def service(self, tenant_id: str) -> MonitoringService:
        if tenant_id not in self._services:
            self._services[tenant_id] = MonitoringService(
                account_repo=SqlMonitoredAccountRepository(tenant_id),
                event_repo=self.events(tenant_id),
                lifecycle_engine=self.lifecycle_engine,
                aggregator=self.aggregator,
                market_data_cache=self.market_data_cache,
                audit_logger=self.audit_logger,
            )
        return self._services[tenant_id]


class _TenantProxy:
    def __init__(self, services: TenantServices, kind: str) -> None:
        self._services = services
        self._kind = kind

    def _target(self) -> Any:
        tenant = require_principal().tenant_id
        if tenant == "*":
            raise RuntimeError("tenant-scoped API used without a tenant principal")
        return self._services.service(tenant) if self._kind == "service" else self._services.events(tenant)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._target(), name)
        if not callable(attr):
            return attr

        @functools.wraps(attr)
        def call(*args: Any, **kwargs: Any) -> Any:
            if has_transaction():
                return attr(*args, **kwargs)
            with self._services.db.transaction(require_principal().tenant_id):
                return attr(*args, **kwargs)

        return call


def service_proxy(services: TenantServices) -> Any:
    return _TenantProxy(services, "service")


def events_proxy(services: TenantServices) -> Any:
    return _TenantProxy(services, "events")
