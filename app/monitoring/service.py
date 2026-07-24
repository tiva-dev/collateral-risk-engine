from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any
from uuid import uuid4

from fastapi.encoders import jsonable_encoder

from app.audit.logger import AuditLogger
from app.core.enums import DataMode, MarginState
from app.core.evaluator import RiskEvaluationError
from app.core.models import Holding, Loan, Policy
from app.lifecycle.service import CreditLifecycleEngine
from app.market_data.aggregator import MarketDataAggregator
from app.market_data.identity import InstrumentIdentity
from app.market_data.policy import MarketDataPolicy
from app.market_data.providers import FXRate, RawQuote
from app.monitoring.market_updates import MarketDataCache
from app.monitoring.models import (
    MonitoredAccount,
    MonitoringEvent,
    MonitoringEventType,
    MonitoringSeverity,
    MonitoringStatus,
    MonitoringThresholds,
    utc_now,
)
from app.monitoring.repositories import MonitoredAccountRepository, MonitoringEventRepository
from app.monitoring.scheduler import MonitoringScheduler, SimpleMonitoringScheduler
from app.version import API_VERSION, MARKET_DATA_MODEL_VERSION, MONITORING_MODEL_VERSION


def _json(obj: Any) -> Any:
    if is_dataclass(obj):
        return jsonable_encoder(asdict(obj))
    return jsonable_encoder(obj)


class MonitoringService:
    def __init__(
        self,
        account_repo: MonitoredAccountRepository,
        event_repo: MonitoringEventRepository,
        lifecycle_engine: CreditLifecycleEngine,
        aggregator: MarketDataAggregator | None = None,
        scheduler: MonitoringScheduler | None = None,
        market_data_cache: MarketDataCache | None = None,
        audit_logger: AuditLogger | None = None,
        thresholds: MonitoringThresholds | None = None,
    ) -> None:
        self.account_repo = account_repo
        self.event_repo = event_repo
        self.lifecycle_engine = lifecycle_engine
        self.aggregator = aggregator or MarketDataAggregator()
        self.scheduler = scheduler or SimpleMonitoringScheduler()
        self.market_data_cache = market_data_cache
        self.audit_logger = audit_logger
        self.thresholds = thresholds or MonitoringThresholds()

    def register_account(
        self,
        *,
        account_ref: str,
        holdings: list[Holding],
        pledged_cash_balance: float,
        loan: Loan,
        loan_currency: str,
        policy: Policy,
        data_mode: DataMode,
        market_data_policy: MarketDataPolicy,
        client_supplied_quotes: dict[str, RawQuote] | None = None,
        client_supplied_fx_rates: dict[tuple[str, str], FXRate] | None = None,
        monitoring_status: MonitoringStatus = MonitoringStatus.ACTIVE,
        run_initial_evaluation: bool = True,
    ) -> tuple[MonitoredAccount, list[MonitoringEvent]]:
        now = utc_now()
        account = MonitoredAccount(
            account_ref=account_ref,
            holdings=holdings,
            pledged_cash_balance=pledged_cash_balance,
            loan=loan,
            loan_currency=loan_currency,
            policy=policy,
            data_mode=data_mode,
            market_data_policy=market_data_policy,
            client_supplied_quotes=dict(client_supplied_quotes or {}),
            client_supplied_fx_rates=dict(client_supplied_fx_rates or {}),
            monitoring_status=monitoring_status,
            created_at=now,
            updated_at=now,
        )
        saved = self.account_repo.save(account)
        self._audit("monitoring_account_registered", account_ref, {"monitoring_status": monitoring_status.value, "run_initial_evaluation": run_initial_evaluation})
        should_evaluate = monitoring_status == MonitoringStatus.ACTIVE or run_initial_evaluation
        if not should_evaluate:
            return saved, []
        try:
            account, events = self.evaluate_account(account_ref, force_tick_event=True, is_initial=True, force=True)
            return account, events
        except Exception:
            self.account_repo.delete(account_ref)
            self._audit("monitoring_account_registration_rolled_back", account_ref, {"monitoring_status": monitoring_status.value})
            raise

    def get_account(self, account_ref: str) -> MonitoredAccount | None:
        return self.account_repo.get(account_ref)

    def list_accounts(self) -> list[MonitoredAccount]:
        return self.account_repo.list()

    def delete_account(self, account_ref: str) -> bool:
        deleted = self.account_repo.delete(account_ref)
        self._audit("monitoring_account_deleted", account_ref, {"deleted": deleted})
        return deleted

    def update_account_status(self, account_ref: str, monitoring_status: MonitoringStatus) -> MonitoredAccount:
        account = self.account_repo.get(account_ref)
        if account is None:
            raise KeyError(account_ref)
        previous = account.monitoring_status
        account.monitoring_status = monitoring_status
        account.updated_at = utc_now()
        updated = self.account_repo.update(account)
        self._audit("monitoring_account_status_updated", account_ref, {"previous_status": previous.value, "new_status": monitoring_status.value})
        return updated

    def evaluate_account(self, account_ref: str, *, force_tick_event: bool = False, is_initial: bool = False, force: bool = False) -> tuple[MonitoredAccount, list[MonitoringEvent]]:
        account = self.account_repo.get(account_ref)
        if account is None:
            raise KeyError(account_ref)
        if account.monitoring_status != MonitoringStatus.ACTIVE and not force:
            raise ValueError(f"monitoring account is {account.monitoring_status.value}; pass force=true to tick anyway")
        previous_state = account.last_margin_state
        previous_credit = account.last_available_credit
        previous_warnings = dict(account.last_market_data_warnings)
        previous_quality = dict(account.last_quality_scores)
        try:
            aggregation = self._normalize_market_data(account)
            market_data = self.lifecycle_engine._market_data_with_pledged_cash(aggregation.to_core_market_data(), account.loan_currency)
            holdings = self.lifecycle_engine._holdings_with_pledged_cash(account.holdings, account.pledged_cash_balance, account.loan_currency)
            decision = self.lifecycle_engine.monitor(account.account_ref, account.loan, holdings, account.policy, market_data)
            now = utc_now()
            evaluation_snapshot = _json(decision.evaluation)
            account.last_evaluation = evaluation_snapshot
            account.last_margin_state = decision.margin_state
            account.last_available_credit = decision.current_available_credit
            account.last_market_data_warnings = dict(aggregation.warnings_by_instrument)
            account.last_missing_data = list(aggregation.missing_data)
            account.last_quality_scores = dict(aggregation.quality_report)
            account.last_checked_at = now
            account.next_check_after = self.scheduler.next_check_after(decision.margin_state, now)
            account.updated_at = now
            self.account_repo.update(account)
            self._audit(
                "monitoring_tick",
                account.account_ref,
                {
                    "previous_state": previous_state.value if previous_state else None,
                    "new_state": decision.margin_state.value,
                    "previous_available_credit": previous_credit,
                    "new_available_credit": decision.current_available_credit,
                    "market_data_warnings": aggregation.warnings_by_instrument,
                    "missing_data": aggregation.missing_data,
                    "evaluation_audit_id": decision.audit_id,
                    "model_versions": self._model_versions(),
                },
            )
            events = self._build_events(
                account,
                previous_state,
                previous_credit,
                previous_warnings,
                previous_quality,
                force_tick_event=force_tick_event,
                is_initial=is_initial,
                reason=decision.reason,
                audit_id=decision.audit_id,
            )
            saved_events = [self._append_event(event) for event in events]
            return account, saved_events
        except Exception as exc:
            event = self._event(
                account,
                MonitoringEventType.MONITORING_ERROR,
                MonitoringSeverity.CRITICAL if isinstance(exc, RiskEvaluationError) else MonitoringSeverity.WARNING,
                previous_state,
                previous_state,
                previous_credit,
                previous_credit,
                f"monitoring evaluation failed: {exc}",
                {},
                {},
                [],
                audit_id=None,
                dedupe_key=f"{account.account_ref}:monitoring_error:{type(exc).__name__}:{exc}",
            )
            saved_event = self._append_event(event)
            self._audit("monitoring_error", account.account_ref, {"error": str(exc), "event_id": saved_event.event_id, "audit_id": saved_event.audit_id})
            raise

    def tick_all(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for account in self.account_repo.list_active():
            try:
                updated, events = self.evaluate_account(account.account_ref, force_tick_event=False)
                results.append({"account_ref": updated.account_ref, "status": "evaluated", "events": [_json(event) for event in events]})
            except Exception as exc:
                results.append({"account_ref": account.account_ref, "status": "error", "error": str(exc)})
        return results

    def ingest_market_data_update(
        self,
        quote_updates: dict[str, RawQuote],
        fx_rate_updates: dict[tuple[str, str], FXRate],
        instruments: list[str],
        source: str,
        trigger_tick: bool,
    ) -> dict[str, Any]:
        received_at = utc_now()
        if self.market_data_cache:
            self.market_data_cache.merge(quote_updates, fx_rate_updates, source, received_at)
        affected, warnings = self._affected_accounts(quote_updates, fx_rate_updates, instruments)
        tick_results = []
        if trigger_tick:
            for account_ref in affected:
                try:
                    account, events = self.evaluate_account(account_ref, force_tick_event=True)
                    tick_results.append({"account_ref": account.account_ref, "events": [_json(event) for event in events]})
                except Exception as exc:
                    tick_results.append({"account_ref": account_ref, "error": str(exc)})
        self._audit(
            "market_data_update_ingested",
            None,
            {
                "source": source,
                "received_at": received_at.isoformat(),
                "quote_update_count": len(quote_updates),
                "fx_update_count": len(fx_rate_updates),
                "affected_accounts": affected,
                "warnings": warnings,
                "trigger_tick": trigger_tick,
            },
        )
        return {"affected_accounts": affected, "warnings": warnings, "tick_results": tick_results, "received_at": received_at}

    def _normalize_market_data(self, account: MonitoredAccount):
        client_quotes = dict(account.client_supplied_quotes)
        client_fx = dict(account.client_supplied_fx_rates)
        provider_registry = {}
        if self.market_data_cache:
            cache = self.market_data_cache.snapshot()
            client_quotes = {**client_quotes, **cache.quotes}
            client_fx = {**client_fx, **cache.fx_rates}
            provider_registry["client"] = self.market_data_cache.provider()
        return self.aggregator.normalize(
            holdings=account.holdings,
            loan_currency=account.loan_currency,
            market_data_policy=account.market_data_policy,
            data_mode=account.data_mode,
            client_supplied_quotes=client_quotes,
            client_supplied_fx_rates=client_fx,
            provider_registry=provider_registry,
        )

    def _build_events(self, account: MonitoredAccount, previous_state, previous_credit, previous_warnings, previous_quality, *, force_tick_event: bool, is_initial: bool, reason: str, audit_id: str) -> list[MonitoringEvent]:
        events: list[MonitoringEvent] = []
        state = account.last_margin_state
        credit = account.last_available_credit
        snapshot = account.last_evaluation or {}
        warnings = account.last_market_data_warnings
        missing = account.last_missing_data
        if force_tick_event or self.thresholds.persist_unchanged_info_ticks or is_initial:
            dedupe = None if force_tick_event else f"{account.account_ref}:tick:{state}:{credit}:{self._warnings_signature(warnings)}"
            events.append(self._event(account, MonitoringEventType.MONITORING_TICK_COMPLETED, self._severity_for_state(state), previous_state, state, previous_credit, credit, reason, snapshot, warnings, missing, audit_id, dedupe))
        if previous_state is not None and previous_state != state:
            events.append(self._event(account, MonitoringEventType.RISK_STATE_CHANGED, self._severity_for_state(state), previous_state, state, previous_credit, credit, reason, snapshot, warnings, missing, audit_id, f"{account.account_ref}:risk_state:{previous_state}->{state}:{account.last_checked_at}"))
        if previous_state != MarginState.MARGIN_CALL and state == MarginState.MARGIN_CALL:
            events.append(self._event(account, MonitoringEventType.MARGIN_CALL_TRIGGERED, MonitoringSeverity.WARNING, previous_state, state, previous_credit, credit, reason, snapshot, warnings, missing, audit_id, f"{account.account_ref}:entered_margin_call:{account.last_checked_at}"))
        if previous_state != MarginState.LIQUIDATION and state == MarginState.LIQUIDATION:
            events.append(self._event(account, MonitoringEventType.LIQUIDATION_TRIGGERED, MonitoringSeverity.CRITICAL, previous_state, state, previous_credit, credit, reason, snapshot, warnings, missing, audit_id, f"{account.account_ref}:entered_liquidation:{account.last_checked_at}"))
        if previous_credit is not None and credit is not None and self._credit_changed(previous_credit, credit):
            events.append(self._event(account, MonitoringEventType.AVAILABLE_CREDIT_CHANGED, MonitoringSeverity.WARNING, previous_state, state, previous_credit, credit, "available credit changed beyond configured monitoring threshold", snapshot, warnings, missing, audit_id, f"{account.account_ref}:credit:{round(previous_credit,2)}->{round(credit,2)}:{account.last_checked_at}"))
        if self._data_quality_degraded(previous_quality, account.last_quality_scores, previous_warnings, warnings):
            events.append(self._event(account, MonitoringEventType.MARKET_DATA_DEGRADED, MonitoringSeverity.WARNING, previous_state, state, previous_credit, credit, "market data warnings or quality deteriorated", snapshot, warnings, missing, audit_id, f"{account.account_ref}:market_data_degraded:{self._warnings_signature(warnings)}"))
        if self._missing_fx_appeared(previous_warnings, warnings, missing):
            severity = MonitoringSeverity.CRITICAL if state in {MarginState.MARGIN_CALL, MarginState.LIQUIDATION} else MonitoringSeverity.WARNING
            events.append(self._event(account, MonitoringEventType.FX_MISSING, severity, previous_state, state, previous_credit, credit, "required FX data is missing for one or more pledged collateral instruments", snapshot, warnings, missing, audit_id, f"{account.account_ref}:fx_missing:{self._warnings_signature(warnings)}"))
        return events

    def _event(self, account: MonitoredAccount, event_type: MonitoringEventType, severity: MonitoringSeverity, previous_state, new_state, previous_credit, new_credit, reason: str, snapshot: Any, warnings: dict[str, list[str]], missing: list[str], audit_id: str | None, dedupe_key: str | None) -> MonitoringEvent:
        liquidation_plan = None
        if isinstance(snapshot, dict):
            liquidation_plan = snapshot.get("liquidation_plan")
        return MonitoringEvent(
            event_id=f"mevt_{uuid4().hex}", account_ref=account.account_ref, event_type=event_type, severity=severity,
            previous_margin_state=previous_state, new_margin_state=new_state, previous_available_credit=previous_credit,
            new_available_credit=new_credit, reason=reason, evaluation_snapshot=snapshot, market_data_warnings=warnings,
            missing_data=missing, liquidation_plan=liquidation_plan, model_versions=self._model_versions(), audit_id=audit_id,
            dedupe_key=dedupe_key,
        )

    def _append_event(self, event: MonitoringEvent) -> MonitoringEvent:
        result = self.event_repo.append(event, dedupe_ttl_seconds=self.thresholds.dedupe_ttl_seconds)
        saved = result.event
        if result.created:
            audit_id = self._audit("monitoring_event_emitted", saved.account_ref, {"event_id": saved.event_id, "event_type": saved.event_type.value, "severity": saved.severity.value, "previous_state": saved.previous_margin_state.value if saved.previous_margin_state else None, "new_state": saved.new_margin_state.value if saved.new_margin_state else None, "market_data_warnings": saved.market_data_warnings, "missing_data": saved.missing_data, "model_versions": saved.model_versions, "evaluation_audit_id": saved.audit_id})
            if not saved.audit_id:
                saved.audit_id = audit_id
                self.event_repo.update_event_audit_id(saved.event_id, audit_id) if hasattr(self.event_repo, "update_event_audit_id") else None
        return saved

    def _affected_accounts(self, quotes: dict[str, RawQuote], fx_rates: dict[tuple[str, str], FXRate], instruments: list[str]) -> tuple[list[str], list[str]]:
        affected: set[str] = set()
        warnings: list[str] = []
        keys = set(instruments)
        for key, quote in quotes.items():
            inst = quote.instrument
            keys.update({key, inst.asset_id, inst.asset_id.upper(), inst.stable_key, inst.symbol, inst.symbol.upper()})
        for key in keys:
            matches, key_warnings = self._match_accounts_for_market_key(key)
            warnings.extend(key_warnings)
            affected.update(account.account_ref for account in matches)
        if fx_rates:
            pairs = {(src.upper(), dst.upper()) for src, dst in fx_rates}
            for account in self.account_repo.list_active():
                currencies = {InstrumentIdentity.from_holding(holding).currency.upper() for holding in account.holdings}
                if any((currency, account.loan_currency.upper()) in pairs or (account.loan_currency.upper(), currency) in pairs for currency in currencies):
                    affected.add(account.account_ref)
        return sorted(affected), sorted(set(warnings))


    def _match_accounts_for_market_key(self, key: str) -> tuple[list[MonitoredAccount], list[str]]:
        if not key:
            return [], []
        exact_matches: dict[str, MonitoredAccount] = {}
        symbol_matches: dict[str, MonitoredAccount] = {}
        stable_keys_for_symbol: set[str] = set()
        lookup = key.upper()
        for account in self.account_repo.list():
            for holding in account.holdings:
                identity = InstrumentIdentity.from_holding(holding)
                if lookup in {holding.asset_id.upper(), identity.stable_key.upper()}:
                    exact_matches[account.account_ref] = account
                if lookup == identity.symbol.upper():
                    symbol_matches[account.account_ref] = account
                    stable_keys_for_symbol.add(identity.stable_key.upper())

        warnings: list[str] = []
        if symbol_matches and len(stable_keys_for_symbol) > 1:
            warnings.append(f"ambiguous_symbol:{key}")
            return list(exact_matches.values()), warnings
        merged = {**symbol_matches, **exact_matches}
        return list(merged.values()), warnings

    def _credit_changed(self, previous: float, new: float) -> bool:
        delta = abs(new - previous)
        if delta >= self.thresholds.available_credit_abs_change_threshold:
            return True
        denominator = max(abs(previous), 1.0)
        return delta / denominator >= self.thresholds.available_credit_pct_change_threshold

    def _data_quality_degraded(self, previous_quality, new_quality, previous_warnings, new_warnings) -> bool:
        if not previous_quality and not previous_warnings:
            return bool(new_warnings)
        for key, new_score in new_quality.items():
            old = previous_quality.get(key, new_score)
            if old - new_score >= self.thresholds.data_quality_change_threshold:
                return True
        return sum(len(v) for v in new_warnings.values()) > sum(len(v) for v in previous_warnings.values())

    def _missing_fx_appeared(self, previous_warnings, new_warnings, missing) -> bool:
        old = self._warnings_signature(previous_warnings)
        new = self._warnings_signature(new_warnings)
        fx_terms = ("missing_fx", "missing_required_fx", "missing_fx_rate")
        return any(term in new for term in fx_terms) and not any(term in old for term in fx_terms)

    def _warnings_signature(self, warnings: dict[str, list[str]]) -> str:
        return "|".join(f"{key}:{','.join(values)}" for key, values in sorted(warnings.items()))

    def _severity_for_state(self, state: MarginState | None) -> MonitoringSeverity:
        if state == MarginState.LIQUIDATION:
            return MonitoringSeverity.CRITICAL
        if state in {MarginState.WATCH, MarginState.RESTRICT_NEW_BORROWING, MarginState.MARGIN_CALL}:
            return MonitoringSeverity.WARNING
        return MonitoringSeverity.INFO

    def _model_versions(self) -> dict[str, str]:
        return {"api": API_VERSION, "market_data": MARKET_DATA_MODEL_VERSION, "monitoring": MONITORING_MODEL_VERSION}

    def _audit(self, event_type: str, account_ref: str | None, payload: dict[str, Any]) -> str:
        if not self.audit_logger:
            return "audit_disabled"
        return self.audit_logger.write({"event_type": event_type, "account_ref": account_ref, **payload})
