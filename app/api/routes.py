from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path

from fastapi import APIRouter, HTTPException
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
    PreTradeRiskCheckRequest,
    PortfolioActionCheckRequest,
    PortfolioActionCheckResponse,
    PreTradeRiskCheckResponse,
)
from app.audit.logger import AuditLogger
from app.core.evaluator import CollateralRiskEngine, RiskEvaluationError
from app.lifecycle.service import CreditLifecycleEngine
from app.market_data.aggregator import MarketDataAggregator

router = APIRouter()
audit_logger = AuditLogger(Path("./data/audit/audit_log.jsonl"))
engine = CollateralRiskEngine(audit_logger=audit_logger)
lifecycle_engine = CreditLifecycleEngine(risk_engine=engine, audit_logger=audit_logger)


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
    fx_decisions = {
        key: {
            "fx_rate_used": data.fx_rate_used,
            "fx_source": data.fx_source,
            "fx_timestamp": data.fx_timestamp,
            "fx_quality_score": data.fx_quality_score,
        }
        for key, data in result.normalized_market_data.items()
    }
    return MarketDataNormalizeResponse(
        normalized_market_data=normalized_payload,
        warnings=result.warnings_by_instrument,
        quality_scores=result.quality_report,
        fx_decisions=jsonable_encoder(fx_decisions),
        missing_data=result.missing_data,
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
