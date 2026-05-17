from __future__ import annotations

from dataclasses import asdict
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
    PreTradeRiskCheckRequest,
    PreTradeRiskCheckResponse,
)
from app.audit.logger import AuditLogger
from app.core.evaluator import CollateralRiskEngine, RiskEvaluationError
from app.lifecycle.service import CreditLifecycleEngine

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


@router.post("/risk/pre-trade-check", response_model=PreTradeRiskCheckResponse)
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

    return PreTradeRiskCheckResponse(result=jsonable_encoder(asdict(result)))


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
