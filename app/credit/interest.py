from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal
from app.core.models import Loan

@dataclass(frozen=True)
class InterestPolicy:
    """Interest terms. accrual_frequency controls scheduling and simulation stepping cadence."""
    annual_interest_rate: float
    accrual_frequency: Literal["daily","monthly","quarterly","yearly"]="daily"
    compounding: Literal["simple","compound"]="simple"
    day_count_convention: Literal["actual_365","actual_360","thirty_360"]="actual_365"
    interest_accrual_mode: Literal["engine_calculated","client_supplied"]="engine_calculated"
    last_accrual_at: datetime|None=None
    next_accrual_at: datetime|None=None

    def __post_init__(self):
        if self.annual_interest_rate < 0: raise ValueError("annual_interest_rate must be >= 0")
        if self.accrual_frequency not in {"daily","monthly","quarterly","yearly"}: raise ValueError("unsupported accrual_frequency")
        if self.compounding not in {"simple","compound"}: raise ValueError("unsupported compounding")
        if self.day_count_convention not in {"actual_365","actual_360","thirty_360"}: raise ValueError("unsupported day_count_convention")
        if self.interest_accrual_mode not in {"engine_calculated","client_supplied"}: raise ValueError("unsupported interest_accrual_mode")

@dataclass(frozen=True)
class InterestAccrualResult:
    mode: str
    interest_accrued: float
    from_datetime: datetime
    to_datetime: datetime
    day_count_fraction: float = 0.0
    compounding: str = "simple"
    periods: int = 1
    def __getitem__(self, key):
        aliases={"from":"from_datetime","to":"to_datetime"}
        return getattr(self, aliases.get(key,key))

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
    def add_months(dt: datetime, months: int) -> datetime:
        year = dt.year + (dt.month - 1 + months) // 12
        month = (dt.month - 1 + months) % 12 + 1
        day = dt.day
        while day > 28:
            try:
                return dt.replace(year=year, month=month, day=day)
            except ValueError:
                day -= 1
        return dt.replace(year=year, month=month, day=day)

    if loan_terms.accrual_frequency == "daily":
        return from_datetime + timedelta(days=1)
    if loan_terms.accrual_frequency == "monthly":
        return add_months(from_datetime, 1)
    if loan_terms.accrual_frequency == "quarterly":
        return add_months(from_datetime, 3)
    if loan_terms.accrual_frequency == "yearly":
        return add_months(from_datetime, 12)
    raise ValueError("unsupported accrual_frequency")

def accrue_interest(loan: Loan, loan_terms: InterestPolicy, from_datetime: datetime, to_datetime: datetime) -> tuple[Loan, InterestAccrualResult]:
    if loan_terms.interest_accrual_mode == "client_supplied":
        return loan, InterestAccrualResult("client_supplied",0.0,from_datetime,to_datetime,compounding=loan_terms.compounding)
    frac=calculate_day_count_fraction(from_datetime,to_datetime,loan_terms.day_count_convention)
    base=loan.principal if loan_terms.compounding == "simple" else loan.principal + loan.accrued_interest
    interest=max(0.0, base * loan_terms.annual_interest_rate * frac)
    if loan_terms.compounding == "compound": new_loan=replace(loan, principal=loan.principal+loan.accrued_interest+interest, accrued_interest=0.0)
    else: new_loan=replace(loan, accrued_interest=loan.accrued_interest+interest)
    return new_loan, InterestAccrualResult("engine_calculated",interest,from_datetime,to_datetime,frac,loan_terms.compounding)

def accrue_scheduled_periods(loan: Loan, loan_terms: InterestPolicy, from_datetime: datetime, to_datetime: datetime) -> tuple[Loan, InterestAccrualResult]:
    current=from_datetime; updated=loan; total=0.0; periods=0
    while current < to_datetime:
        nxt=min(next_accrual_time(loan_terms,current), to_datetime)
        updated,result=accrue_interest(updated, loan_terms, current, nxt)
        total += result.interest_accrued; periods += 1; current=nxt
    return updated, InterestAccrualResult(loan_terms.interest_accrual_mode,total,from_datetime,to_datetime,compounding=loan_terms.compounding,periods=periods)

def apply_repayment(loan: Loan, amount: float) -> tuple[Loan, dict]:
    remaining=max(0.0, amount); fees_paid=min(loan.fees,remaining); remaining-=fees_paid
    interest_paid=min(loan.accrued_interest,remaining); remaining-=interest_paid
    principal_paid=min(loan.principal,remaining)
    return replace(loan, fees=loan.fees-fees_paid, accrued_interest=loan.accrued_interest-interest_paid, principal=loan.principal-principal_paid), {"fees_paid":fees_paid,"interest_paid":interest_paid,"principal_paid":principal_paid}
