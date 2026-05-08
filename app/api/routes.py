from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.encoders import jsonable_encoder

from app.api.schemas import EvaluateRequest, EvaluateResponse
from app.audit.logger import AuditLogger
from app.core.evaluator import CollateralRiskEngine, RiskEvaluationError

router = APIRouter()
engine = CollateralRiskEngine(audit_logger=AuditLogger(Path("./data/audit/audit_log.jsonl")))


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
        )
    except RiskEvaluationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return EvaluateResponse(result=jsonable_encoder(asdict(result)))
