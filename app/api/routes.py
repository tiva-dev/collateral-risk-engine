from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.encoders import jsonable_encoder

from app.api.schemas import (
    DrawCheckRequest,
    EvaluateRequest,
    EvaluateResponse,
    LifecycleResponse,
    MonitorRequest,
    OriginateRequest,
    MarketDataNormalizeRequest,
    MarketDataNormalizeResponse,
    MarketDataUpdateRequest,
    MarketDataUpdateResponse,
    MonitoredAccountCreateRequest,
    MonitoringAccountResponse,
    MonitoringAccountsListResponse,
    MonitoringEventsResponse,
    MonitoringEventOut,
    MonitoringTickResponse,
    PreTradeRiskCheckRequest,
    PortfolioActionCheckRequest,
    PortfolioActionCheckResponse,
    PreTradeRiskCheckResponse,
)
from app.audit.logger import AuditLogger
from app.core.evaluator import CollateralRiskEngine, RiskEvaluationError
from app.lifecycle.service import CreditLifecycleEngine
from app.market_data.aggregator import MarketDataAggregator
from app.version import MARKET_DATA_MODEL_VERSION
from app.monitoring.events import serialize_event, serialize_sse_event
from app.monitoring.market_updates import InMemoryMarketDataCache
from app.monitoring.models import MonitoringEventType, MonitoringSeverity
from app.monitoring.repositories import InMemoryMonitoredAccountRepository, InMemoryMonitoringEventRepository
from app.monitoring.service import MonitoringService

router = APIRouter()
audit_logger = AuditLogger(Path("./data/audit/audit_log.jsonl"))
engine = CollateralRiskEngine(audit_logger=audit_logger)
lifecycle_engine = CreditLifecycleEngine(risk_engine=engine, audit_logger=audit_logger)
monitoring_account_repo = InMemoryMonitoredAccountRepository()
monitoring_event_repo = InMemoryMonitoringEventRepository()
monitoring_market_data_cache = InMemoryMarketDataCache()
monitoring_service = MonitoringService(
    account_repo=monitoring_account_repo,
    event_repo=monitoring_event_repo,
    lifecycle_engine=lifecycle_engine,
    aggregator=MarketDataAggregator(),
    market_data_cache=monitoring_market_data_cache,
    audit_logger=audit_logger,
)


def serialize(obj):
    if hasattr(obj, "value"):
        return obj.value
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


@router.post("/market-data/normalize", response_model=MarketDataNormalizeResponse)
def normalize_market_data(request: MarketDataNormalizeRequest) -> MarketDataNormalizeResponse:
    if not request.instruments and not request.holdings:
        raise HTTPException(status_code=422, detail="normalize request requires instruments or holdings")
    aggregator = MarketDataAggregator()
    instruments = [instrument.to_domain() for instrument in request.instruments]
    holdings = [holding.to_domain() for holding in request.holdings]
    client_quotes = {
        key: quote.to_raw_quote() for key, quote in request.client_supplied_quotes.items()
    }
    client_fx_rates = {
        (fx.from_currency.upper(), fx.to_currency.upper()): fx.to_fx_rate()
        for fx in request.client_supplied_fx_rates
    }
    result = aggregator.normalize(
        instruments=instruments or None,
        holdings=holdings or None,
        loan_currency=request.loan_currency,
        market_data_policy=request.market_data_policy.to_domain(),
        data_mode=request.data_mode,
        client_supplied_quotes=client_quotes,
        client_supplied_fx_rates=client_fx_rates,
    )
    normalized_payload = {
        key: jsonable_encoder(data)
        for key, data in result.normalized_market_data.items()
    }
    for alias, stable_key in getattr(result.normalized_market_data, "_aliases", {}).items():
        if stable_key in normalized_payload:
            normalized_payload.setdefault(alias, normalized_payload[stable_key])
    fx_decisions = {
        key: {
            "fx_rate_used": data.fx_rate_used,
            "fx_source": data.fx_source,
            "fx_timestamp": data.fx_timestamp,
            "fx_quality_score": data.fx_quality_score,
        }
        for key, data in result.normalized_market_data.items()
    }
    for alias, stable_key in getattr(result.normalized_market_data, "_aliases", {}).items():
        if stable_key in fx_decisions:
            fx_decisions.setdefault(alias, fx_decisions[stable_key])
    return MarketDataNormalizeResponse(
        market_data_model_version=MARKET_DATA_MODEL_VERSION,
        normalized_market_data=normalized_payload,
        warnings=result.warnings_by_instrument,
        quality_scores=result.quality_report,
        fx_decisions=jsonable_encoder(fx_decisions),
        missing_data=result.missing_data,
        evaluator_market_data=jsonable_encoder(result.evaluator_market_data),
        evaluator_key_to_stable_key=result.evaluator_key_to_stable_key,
    )


@router.post("/risk/evaluate", response_model=EvaluateResponse)
def evaluate_risk(request: EvaluateRequest) -> EvaluateResponse:
    try:
        result = engine.evaluate(
            account_ref=request.account_ref,
            holdings=[holding.to_domain() for holding in request.holdings],
            loan=request.loan.to_domain(),
            policy=request.policy.to_domain(),
            market_data={k: v.to_domain() for k, v in request.market_data.items()},
            requested_draw_amount=request.requested_draw_amount,
        )
    except RiskEvaluationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return EvaluateResponse(result=jsonable_encoder(asdict(result)))


@router.post(
    "/risk/pre-trade-check",
    response_model=PreTradeRiskCheckResponse,
    deprecated=True,
)
def pre_trade_check(request: PreTradeRiskCheckRequest) -> PreTradeRiskCheckResponse:
    try:
        result = engine.pre_trade_check(
            account_ref=request.account_ref,
            holdings=[holding.to_domain() for holding in request.holdings],
            loan=request.loan.to_domain(),
            policy=request.policy.to_domain(),
            market_data={k: v.to_domain() for k, v in request.market_data.items()},
            actions=[action.to_domain() for action in request.actions],
        )
    except RiskEvaluationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if result.decision.value == "reduce_available_credit":
        result = replace(
            result, reduced_available_credit=result.projected_available_credit
        )
    return PreTradeRiskCheckResponse(result=jsonable_encoder(asdict(result)))


@router.post("/portfolio/action/check", response_model=PortfolioActionCheckResponse)
def check_portfolio_action(
    request: PortfolioActionCheckRequest,
) -> PortfolioActionCheckResponse:
    try:
        result = lifecycle_engine.check_portfolio_action(
            account_state=request.account_state.to_domain(),
            proposed_action=request.proposed_action.to_domain(),
            policy=request.policy.to_domain(),
            market_data={k: v.to_domain() for k, v in request.market_data.items()},
        )
    except (RiskEvaluationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return PortfolioActionCheckResponse(result=jsonable_encoder(asdict(result)))


@router.post("/credit/originate", response_model=LifecycleResponse)
def originate_credit(request: OriginateRequest) -> LifecycleResponse:
    try:
        result = lifecycle_engine.originate(
            account_ref=request.account_ref,
            holdings=[holding.to_domain() for holding in request.holdings],
            policy=request.policy.to_domain(),
            market_data={k: v.to_domain() for k, v in request.market_data.items()},
        )
    except RiskEvaluationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return LifecycleResponse(result=jsonable_encoder(asdict(result)))


@router.post("/credit/draw/check", response_model=LifecycleResponse)
def check_credit_draw(request: DrawCheckRequest) -> LifecycleResponse:
    try:
        result = lifecycle_engine.check_draw(
            account_ref=request.account_ref,
            current_loan=request.current_loan.to_domain(),
            requested_draw_amount=request.requested_draw_amount,
            requested_repayment_amount=request.requested_repayment_amount,
            holdings=[holding.to_domain() for holding in request.holdings],
            policy=request.policy.to_domain(),
            market_data={k: v.to_domain() for k, v in request.market_data.items()},
        )
    except RiskEvaluationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return LifecycleResponse(result=jsonable_encoder(asdict(result)))


@router.post("/loan/monitor", response_model=LifecycleResponse)
def monitor_loan(request: MonitorRequest) -> LifecycleResponse:
    try:
        result = lifecycle_engine.monitor(
            account_ref=request.account_ref,
            loan=request.loan.to_domain(),
            holdings=[holding.to_domain() for holding in request.holdings],
            policy=request.policy.to_domain(),
            market_data={k: v.to_domain() for k, v in request.market_data.items()},
        )
    except RiskEvaluationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return LifecycleResponse(result=jsonable_encoder(asdict(result)))


def _monitoring_account_out(account):
    return jsonable_encoder(
        {
            "account_ref": account.account_ref,
            "holdings": account.holdings,
            "pledged_cash_balance": account.pledged_cash_balance,
            "loan": account.loan,
            "loan_currency": account.loan_currency,
            "data_mode": account.data_mode,
            "monitoring_status": account.monitoring_status,
            "last_evaluation": account.last_evaluation,
            "last_margin_state": account.last_margin_state,
            "last_available_credit": account.last_available_credit,
            "last_market_data_warnings": account.last_market_data_warnings,
            "last_missing_data": account.last_missing_data,
            "last_checked_at": account.last_checked_at,
            "next_check_after": account.next_check_after,
            "created_at": account.created_at,
            "updated_at": account.updated_at,
        }
    )


def _events_out(events):
    return [serialize_event(event) for event in events]


@router.post("/monitoring/accounts", response_model=MonitoringAccountResponse)
def register_monitored_account(request: MonitoredAccountCreateRequest) -> MonitoringAccountResponse:
    try:
        account, events = monitoring_service.register_account(
            account_ref=request.account_ref,
            holdings=[holding.to_domain() for holding in request.holdings],
            pledged_cash_balance=request.pledged_cash_balance,
            loan=request.loan.to_domain(),
            loan_currency=request.loan_currency,
            policy=request.policy.to_domain(),
            data_mode=request.data_mode,
            market_data_policy=request.market_data_policy.to_domain(),
            client_supplied_quotes={key: quote.to_raw_quote() for key, quote in request.client_supplied_quotes.items()},
            client_supplied_fx_rates={(fx.from_currency.upper(), fx.to_currency.upper()): fx.to_fx_rate() for fx in request.client_supplied_fx_rates},
            monitoring_status=request.monitoring_status,
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return MonitoringAccountResponse(account=_monitoring_account_out(account), events=_events_out(events))


@router.get("/monitoring/accounts", response_model=MonitoringAccountsListResponse)
def list_monitored_accounts() -> MonitoringAccountsListResponse:
    return MonitoringAccountsListResponse(accounts=[_monitoring_account_out(account) for account in monitoring_service.list_accounts()])


@router.get("/monitoring/accounts/{account_ref}", response_model=MonitoringAccountResponse)
def get_monitored_account(account_ref: str) -> MonitoringAccountResponse:
    account = monitoring_service.get_account(account_ref)
    if account is None:
        raise HTTPException(status_code=404, detail="monitored account not found")
    return MonitoringAccountResponse(account=_monitoring_account_out(account), events=[])


@router.delete("/monitoring/accounts/{account_ref}")
def delete_monitored_account(account_ref: str) -> dict[str, bool | str]:
    deleted = monitoring_service.delete_account(account_ref)
    if not deleted:
        raise HTTPException(status_code=404, detail="monitored account not found")
    return {"account_ref": account_ref, "deleted": True}


@router.post("/monitoring/accounts/{account_ref}/tick", response_model=MonitoringTickResponse)
def tick_monitored_account(account_ref: str) -> MonitoringTickResponse:
    try:
        account, events = monitoring_service.evaluate_account(account_ref, force_tick_event=True)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="monitored account not found") from exc
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return MonitoringTickResponse(account=_monitoring_account_out(account), events=_events_out(events))


@router.post("/monitoring/tick", response_model=MonitoringTickResponse)
def tick_all_monitored_accounts() -> MonitoringTickResponse:
    return MonitoringTickResponse(results=monitoring_service.tick_all())


@router.post("/monitoring/market-data/update", response_model=MarketDataUpdateResponse)
def ingest_monitoring_market_data_update(request: MarketDataUpdateRequest) -> MarketDataUpdateResponse:
    quote_updates = {key: quote.to_raw_quote() for key, quote in request.quote_updates.items()}
    fx_updates = {(fx.from_currency.upper(), fx.to_currency.upper()): fx.to_fx_rate() for fx in request.fx_rate_updates}
    result = monitoring_service.ingest_market_data_update(
        quote_updates=quote_updates,
        fx_rate_updates=fx_updates,
        instruments=request.instruments,
        source=request.source,
        trigger_tick=request.trigger_tick,
    )
    return MarketDataUpdateResponse(**jsonable_encoder(result))


@router.get("/monitoring/events", response_model=MonitoringEventsResponse)
def list_monitoring_events(
    account_ref: str | None = None,
    event_type: MonitoringEventType | None = None,
    severity: MonitoringSeverity | None = None,
    limit: int = 100,
) -> MonitoringEventsResponse:
    events = monitoring_event_repo.list(account_ref=account_ref, event_type=event_type, severity=severity, limit=limit)
    return MonitoringEventsResponse(events=_events_out(events))


@router.get("/monitoring/events/stream")
def stream_monitoring_events(limit: int = 100):
    def event_iter():
        events = monitoring_event_repo.list(limit=limit)
        if not events:
            yield ": monitoring stream ready\n\n"
            return
        for event in reversed(events):
            yield serialize_sse_event(event)

    return StreamingResponse(event_iter(), media_type="text/event-stream")


@router.get("/monitoring/events/{event_id}", response_model=MonitoringEventOut)
def get_monitoring_event(event_id: str) -> MonitoringEventOut:
    event = monitoring_event_repo.get(event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="monitoring event not found")
    return MonitoringEventOut(**serialize_event(event))
