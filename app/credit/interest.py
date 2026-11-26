from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Literal

from app.core.models import Loan


@dataclass(frozen=True)
class InterestPolicy:
    """Interest terms. accrual_frequency controls scheduling and simulation stepping cadence."""

    annual_interest_rate: float
    accrual_frequency: Literal["daily", "monthly", "quarterly", "yearly"] = "daily"
    compounding: Literal["simple", "compound"] = "simple"
    day_count_convention: Literal["actual_365", "actual_360", "thirty_360"] = (
        "actual_365"
    )
    interest_accrual_mode: Literal["engine_calculated", "client_supplied"] = (
        "engine_calculated"
    )
    last_accrual_at: datetime | None = None
    next_accrual_at: datetime | None = None
    # Contractual capitalization schedule. Compound interest is capitalized
    # only at boundaries counted from this anchor (normally the drawdown
    # date). Without an anchor, calendar boundaries are used (midnight UTC,
    # first of month, first of quarter, 1 January).
    schedule_anchor: datetime | None = None

    def __post_init__(self):
        if self.annual_interest_rate < 0:
            raise ValueError("annual_interest_rate must be >= 0")
        if self.accrual_frequency not in {"daily", "monthly", "quarterly", "yearly"}:
            raise ValueError("unsupported accrual_frequency")
        if self.compounding not in {"simple", "compound"}:
            raise ValueError("unsupported compounding")
        if self.day_count_convention not in {"actual_365", "actual_360", "thirty_360"}:
            raise ValueError("unsupported day_count_convention")
        if self.interest_accrual_mode not in {"engine_calculated", "client_supplied"}:
            raise ValueError("unsupported interest_accrual_mode")


@dataclass(frozen=True)
class InterestAccrualResult:
    mode: str
    interest_accrued: float
    from_datetime: datetime
    to_datetime: datetime
    day_count_fraction: float = 0.0
    compounding: str = "simple"
    periods: int = 1
    capitalizations: int = 0

    def __getitem__(self, key):
        aliases = {"from": "from_datetime", "to": "to_datetime"}
        return getattr(self, aliases.get(key, key))


LoanTerms = InterestPolicy


def calculate_day_count_fraction(
    start: datetime, end: datetime, convention: str
) -> float:
    if end <= start:
        return 0.0
    if convention == "actual_360":
        return (end - start).total_seconds() / 86400 / 360
    if convention == "actual_365":
        return (end - start).total_seconds() / 86400 / 365
    if convention == "thirty_360":
        d1 = min(start.day, 30)
        d2 = end.day if d1 < 30 else min(end.day, 30)
        return (
            (end.year - start.year) * 360 + (end.month - start.month) * 30 + (d2 - d1)
        ) / 360
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


def accrue_interest(
    loan: Loan,
    loan_terms: InterestPolicy,
    from_datetime: datetime,
    to_datetime: datetime,
) -> tuple[Loan, InterestAccrualResult]:
    """Accrue interest over [from, to] WITHOUT capitalizing it.

    Interest is always added to ``accrued_interest``. For compound loans the
    accrual base is the principal, which includes interest capitalized at
    earlier contractual boundaries; capitalization itself happens only in
    ``accrue_scheduled_periods`` at those boundaries. Monitoring frequency
    therefore cannot change the contractual balance.
    """
    if loan_terms.interest_accrual_mode == "client_supplied":
        return loan, InterestAccrualResult(
            "client_supplied",
            0.0,
            from_datetime,
            to_datetime,
            compounding=loan_terms.compounding,
        )
    frac = calculate_day_count_fraction(
        from_datetime, to_datetime, loan_terms.day_count_convention
    )
    interest = max(0.0, loan.principal * loan_terms.annual_interest_rate * frac)
    new_loan = replace(loan, accrued_interest=loan.accrued_interest + interest)
    return new_loan, InterestAccrualResult(
        "engine_calculated",
        interest,
        from_datetime,
        to_datetime,
        frac,
        loan_terms.compounding,
    )


def _calendar_boundary(loan_terms: InterestPolicy, t: datetime) -> datetime:
    day_start = t.replace(hour=0, minute=0, second=0, microsecond=0)
    if loan_terms.accrual_frequency == "daily":
        return day_start + timedelta(days=1)
    month_start = day_start.replace(day=1)
    step = {"monthly": 1, "quarterly": 3, "yearly": 12}[loan_terms.accrual_frequency]
    if step == 3:
        month_start = month_start.replace(month=((month_start.month - 1) // 3) * 3 + 1)
    if step == 12:
        month_start = month_start.replace(month=1)
    nxt = month_start.month - 1 + step
    return month_start.replace(year=month_start.year + nxt // 12, month=nxt % 12 + 1)


def next_capitalization_boundary(loan_terms: InterestPolicy, t: datetime) -> datetime:
    """First contractual capitalization boundary strictly after ``t``."""
    anchor = loan_terms.schedule_anchor
    if anchor is None:
        return _calendar_boundary(loan_terms, t)
    if anchor > t:
        return anchor
    if loan_terms.accrual_frequency == "daily":
        days = int((t - anchor).total_seconds() // 86400) + 1
        return anchor + timedelta(days=days)
    months = {"monthly": 1, "quarterly": 3, "yearly": 12}[loan_terms.accrual_frequency]
    elapsed = (t.year - anchor.year) * 12 + (t.month - anchor.month)
    k = max(0, elapsed // months - 1)
    while True:
        b = _add_months(anchor, k * months)
        if b > t:
            return b
        k += 1


def _add_months(dt: datetime, months: int) -> datetime:
    year = dt.year + (dt.month - 1 + months) // 12
    month = (dt.month - 1 + months) % 12 + 1
    day = dt.day
    while day > 28:
        try:
            return dt.replace(year=year, month=month, day=day)
        except ValueError:
            day -= 1
    return dt.replace(year=year, month=month, day=day)


def capitalize(loan: Loan) -> Loan:
    return replace(loan, principal=loan.principal + loan.accrued_interest, accrued_interest=0.0)


def accrue_scheduled_periods(
    loan: Loan,
    loan_terms: InterestPolicy,
    from_datetime: datetime,
    to_datetime: datetime,
) -> tuple[Loan, InterestAccrualResult]:
    """Accrue from ``from_datetime`` to ``to_datetime`` and, for compound loans,
    capitalize accrued interest at every contractual boundary crossed.

    The result depends only on the start and end times, not on how the
    interval is split into calls (monitoring ticks, draws, replays).
    """
    current = from_datetime
    updated = loan
    total = 0.0
    periods = 0
    capitalizations = 0
    while current < to_datetime:
        boundary = next_capitalization_boundary(loan_terms, current)
        nxt = min(boundary, to_datetime)
        updated, result = accrue_interest(updated, loan_terms, current, nxt)
        total += result.interest_accrued
        periods += 1
        if (
            loan_terms.compounding == "compound"
            and loan_terms.interest_accrual_mode == "engine_calculated"
            and nxt == boundary
        ):
            updated = capitalize(updated)
            capitalizations += 1
        current = nxt
    return updated, InterestAccrualResult(
        loan_terms.interest_accrual_mode,
        total,
        from_datetime,
        to_datetime,
        compounding=loan_terms.compounding,
        periods=periods,
        capitalizations=capitalizations,
    )


def apply_repayment(loan: Loan, amount: float) -> tuple[Loan, dict]:
    remaining = max(0.0, amount)
    fees_paid = min(loan.fees, remaining)
    remaining -= fees_paid
    interest_paid = min(loan.accrued_interest, remaining)
    remaining -= interest_paid
    principal_paid = min(loan.principal, remaining)
    return replace(
        loan,
        fees=loan.fees - fees_paid,
        accrued_interest=loan.accrued_interest - interest_paid,
        principal=loan.principal - principal_paid,
    ), {
        "fees_paid": fees_paid,
        "interest_paid": interest_paid,
        "principal_paid": principal_paid,
    }
