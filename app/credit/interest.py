from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal
from app.core.models import Loan

@dataclass(frozen=True)
class InterestPolicy:
    annual_interest_rate: float
    accrual_frequency: Literal["daily","monthly","quarterly","yearly"]="daily"
    compounding: Literal["simple","compound"]="simple"
    day_count_convention: Literal["actual_365","actual_360","thirty_360"]="actual_365"
    interest_accrual_mode: Literal["engine_calculated","client_supplied"]="engine_calculated"
    last_accrual_at: datetime|None=None
    next_accrual_at: datetime|None=None

LoanTerms = InterestPolicy

def calculate_day_count_fraction(start: datetime, end: datetime, convention: str) -> float:
    if end <= start: return 0.0
    if convention == "actual_360": return (end-start).total_seconds()/86400/360
    if convention == "actual_365": return (end-start).total_seconds()/86400/365
    if convention == "thirty_360":
        d1=min(start.day,30); d2=end.day if d1<30 else min(end.day,30)
        return ((end.year-start.year)*360+(end.month-start.month)*30+(d2-d1))/360
    raise ValueError("unsupported day_count_convention")

def next_accrual_time(loan_terms: InterestPolicy, from_datetime: datetime) -> datetime:
    if loan_terms.accrual_frequency == "daily": return from_datetime + timedelta(days=1)
    if loan_terms.accrual_frequency == "monthly":
        y=from_datetime.year + (from_datetime.month//12); m=from_datetime.month%12+1
        return from_datetime.replace(year=y, month=m, day=min(from_datetime.day,28))
    if loan_terms.accrual_frequency == "quarterly": return from_datetime + timedelta(days=91)
    if loan_terms.accrual_frequency == "yearly": return from_datetime.replace(year=from_datetime.year+1)
    raise ValueError("unsupported accrual_frequency")

def accrue_interest(loan: Loan, loan_terms: InterestPolicy, from_datetime: datetime, to_datetime: datetime) -> tuple[Loan, dict]:
    if loan_terms.interest_accrual_mode == "client_supplied":
        return loan, {"mode":"client_supplied","interest_accrued":0.0,"from":from_datetime,"to":to_datetime}
    frac=calculate_day_count_fraction(from_datetime,to_datetime,loan_terms.day_count_convention)
    base=loan.principal if loan_terms.compounding == "simple" else loan.principal + loan.accrued_interest
    interest=max(0.0, base * loan_terms.annual_interest_rate * frac)
    if loan_terms.compounding == "compound": new_loan=replace(loan, principal=loan.principal+loan.accrued_interest+interest, accrued_interest=0.0)
    else: new_loan=replace(loan, accrued_interest=loan.accrued_interest+interest)
    return new_loan, {"mode":"engine_calculated","interest_accrued":interest,"day_count_fraction":frac,"from":from_datetime,"to":to_datetime,"compounding":loan_terms.compounding}

def apply_repayment(loan: Loan, amount: float) -> tuple[Loan, dict]:
    remaining=max(0.0, amount); fees_paid=min(loan.fees,remaining); remaining-=fees_paid
    interest_paid=min(loan.accrued_interest,remaining); remaining-=interest_paid
    principal_paid=min(loan.principal,remaining)
    return replace(loan, fees=loan.fees-fees_paid, accrued_interest=loan.accrued_interest-interest_paid, principal=loan.principal-principal_paid), {"fees_paid":fees_paid,"interest_paid":interest_paid,"principal_paid":principal_paid}
