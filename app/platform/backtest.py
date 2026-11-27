"""Historical backtests over persisted market bars.

Two kinds, which answer different questions and must not be confused:

``decision`` (default) is a DECISION REPLAY: it re-evaluates each model's
capacity and state on past dates for a fixed loan. It shows how decisions
would have differed; it does not simulate what would have happened to a loan.

``lifecycle`` is a LIFECYCLE VALIDATION (app.candidate.lifecycle): matched
loans originated on past dates run to maturity under each intervention
variant with interest, warnings, margin calls, cures, partial and forced
liquidation, execution limits and settlement, using common random numbers.

Decision replay:

For a portfolio's current holdings, replay every stored session: rebuild the
market inputs each model would have seen on that date from bars up to that
date only, evaluate the selected models, and record capacity, risk state and
margin calls along the path. The loan is the current principal accruing simple
interest at the account's rate. No future information enters a decision.
"""

from __future__ import annotations

import itertools
import json
import math
from datetime import UTC, datetime, time
from typing import Any

from app.platform import shadow
from app.platform.db import Database

DEFAULT_MODELS = ["baseline", "cre_v070", "A", "B", "B_gap", "candidate"]


def _market_data_on(asset_id: str, rows: list[dict[str, Any]], ts: datetime) -> Any:
    from app.core.models import MarketData

    closes = [r["close"] for r in rows]
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]

    def vol(w: int) -> float | None:
        x = rets[-w:]
        if len(x) < max(5, w // 3):
            return None
        mu = sum(x) / len(x)
        return math.sqrt(sum((v - mu) ** 2 for v in x) / (len(x) - 1)) * math.sqrt(252)

    vols = [r["volume"] for r in rows[-20:] if r["volume_reported"] and r["volume"]]
    adv = sum(vols) / len(vols) if vols else None
    return MarketData(asset_id=asset_id, last_price=closes[-1], average_daily_volume=adv,
                      average_dollar_volume=adv * closes[-1] if adv else None, volatility_30d=vol(30),
                      volatility_90d=vol(90), recent_return_1d=(math.exp(rets[-1]) - 1) if rets else None,
                      timestamp=ts, data_quality_score=1.0)


def _lifecycle(account: Any, holdings: list[Any], bars: dict[str, list[dict[str, Any]]], params: dict[str, Any],
               tenant: str) -> dict[str, Any]:
    from datetime import date as _date

    from app.candidate.lifecycle import DEFAULT_VARIANTS, aggregate, run_cohort

    term = int(params.get("term_sessions", 63))
    every = int(params.get("every", 10))
    warmup = int(params.get("warmup", 120))
    cure_p = float(params.get("cure_probability", 0.5))
    workout = int(params.get("workout_sessions", 40))
    dates = sorted(set.intersection(*[{b["bar_date"] for b in v} for v in bars.values()])) if bars else []
    if len(dates) < warmup + term + 1:
        raise ValueError(f"only {len(dates)} common sessions; lifecycle needs {warmup + term + 1}")
    priors = shadow.candidate_priors(datetime.combine(_date.fromisoformat(str(dates[warmup])[:10]), time(21), UTC))
    cohorts = []
    for k, i in enumerate(range(warmup, len(dates) - term, every)):
        d = _date.fromisoformat(str(dates[i])[:10])
        try:
            c = run_cohort(holdings=holdings, bars=bars, origination=d, term_sessions=term, variants=DEFAULT_VARIANTS,
                           policy=account.policy, interest=account.interest_policy,
                           execution=account.liquidation_execution_policy, cure_probability=cure_p, seed=1000 + k,
                           priors=shadow.candidate_priors(datetime.combine(d, time(21), UTC)) or priors,
                           workout_sessions=workout)
        except ValueError as exc:
            c = {"origination": d.isoformat(), "skipped": str(exc)}
        cohorts.append(c)
    summary = aggregate(cohorts)
    summary.update({"kind": "lifecycle", "term_sessions": term, "every": every, "cure_probability": cure_p,
                    "window": [str(dates[warmup]), str(dates[-1])],
                    "note": "historical lifecycle validation on observed bars; matched principal and common "
                            "random numbers across variants; sessions without an observed bar have no trade"})
    return {"summary": summary, "cohorts": cohorts}


def run(db: Database, services: Any, run_id: str) -> dict[str, Any]:
    from app.candidate.engine import data_key
    from app.lifecycle.service import aggregate_holdings
    from app.platform.context import Principal, reset_principal, set_principal
    from app.platform.ingestion import load_bars

    with db.transaction("*") as conn:
        row = conn.fetchone("SELECT * FROM backtest_runs WHERE run_id=%s", (run_id,))
        if row is None:
            raise KeyError(run_id)
        conn.execute("UPDATE backtest_runs SET status='running', started_at=%s WHERE run_id=%s",
                     (datetime.now(UTC).isoformat(), run_id))
    tenant, ref = row["tenant_id"], row["account_ref"]
    models = [m for m in json.loads(row["models"]) if m in shadow.MODELS]
    params = json.loads(row["params"] or "{}")
    step = int(params.get("step", 5))
    token = set_principal(Principal(tenant_id=tenant, role="system", key_id="backtest"))
    try:
        with db.transaction(tenant):
            account = services.service(tenant).account_repo.get(ref)
            if account is None:
                raise KeyError(f"account {ref} not found")
            all_holdings = aggregate_holdings(account.holdings)
            holdings = [h for h in all_holdings if h.asset_type.value != "cash"]
            # Stable identity, never bare symbol: market data is keyed by the
            # holding's stable key and bars by its exchange:symbol:ccy:type.
            keys = {h.stable_key: data_key(h) for h in holdings}
            bars = {k: load_bars(k, 2000) for k in keys.values()}
        # Locked-period protection: no bar after the examined boundary unless this is
        # the authorised formal evaluation (app/platform/governance/data_lock.py).
        from app.platform.governance.data_lock import (
            EXAMINED_THROUGH,
            cap_bars,
            locked_evaluation_authorised,
        )

        bars = cap_bars(bars, params)
        params["data_through"] = (None if locked_evaluation_authorised(params) else EXAMINED_THROUGH.isoformat())
        asset_ids = {h.stable_key: h.asset_id for h in holdings}

        def cash_md(ts: datetime) -> dict[str, Any]:
            from app.core.models import MarketData

            # Pledged cash is collateral: valued at par in its own currency.
            return {h.stable_key: MarketData(h.asset_id, 1.0, bid=1.0, ask=1.0, average_daily_volume=1e15,
                                             average_dollar_volume=1e15, volatility_30d=0.0, volatility_90d=0.0,
                                             timestamp=ts)
                    for h in all_holdings if h.asset_type.value == "cash"}

        if params.get("kind") == "lifecycle":
            out = _lifecycle(account, all_holdings, bars, params, tenant)
            with db.transaction("*") as conn:
                conn.execute("UPDATE backtest_runs SET status='succeeded', finished_at=%s, summary=%s, series=%s "
                             "WHERE run_id=%s", (datetime.now(UTC).isoformat(), json.dumps(out["summary"], default=str),
                                                 json.dumps(out["cohorts"], default=str), run_id))
            return {"models": out["summary"], **out["summary"]}
        dates = sorted(set.intersection(*[{b["bar_date"] for b in v} for v in bars.values()])) if bars else []
        start = shadow.RESEARCH_MIN_HISTORY + 1
        if len(dates) <= start:
            raise ValueError(f"only {len(dates)} common sessions stored; need more than {start}")
        rate = float(getattr(account.interest_policy, "annual_interest_rate", 0.0) or 0.0)
        principal = account.loan.balance
        series: list[dict[str, Any]] = []
        first_date = datetime.combine(datetime.fromisoformat(dates[start]).date(), time(21, 0), tzinfo=UTC)
        for idx in range(start, len(dates), step):
            d = dates[idx]
            ts = datetime.combine(datetime.fromisoformat(d).date(), time(21, 0), tzinfo=UTC)
            visible = {k: [b for b in v if b["bar_date"] <= d] for k, v in bars.items()}
            md = {sk: _market_data_on(asset_ids[sk], visible[k], ts) for sk, k in keys.items()}
            md.update(cash_md(ts))
            years = (ts - first_date).days / 365.0
            loan = account.loan.__class__(**{**{f: getattr(account.loan, f) for f in account.loan.__dataclass_fields__},
                                            "principal": principal * (1 + rate * years), "accrued_interest": 0.0})
            acc = account.__class__(**{**{f: getattr(account, f) for f in account.__dataclass_fields__ if
                                          account.__dataclass_fields__[f].init}, "loan": loan})
            inp = shadow.ShadowInputs(tenant, acc, all_holdings, md, ts, bars=visible)
            point: dict[str, Any] = {"date": d}
            for r in shadow.evaluate_all(inp, {m: shadow.MODELS[m] for m in models}):
                point[r.model_id] = {"state": r.risk_state, "approved_credit": r.approved_credit,
                                     "capacity_ltv": r.capacity_ltv, "collateral_value": r.collateral_value,
                                     "loan": r.loan_balance, "margin_call": r.margin_call, "error": r.error}
            series.append(point)
        summary: dict[str, dict[str, Any]] = {}
        for m in models:
            pts = [p[m] for p in series if m in p and not p[m]["error"]]
            if not pts:
                summary[m] = {"evaluations": 0, "errors": sum(1 for p in series if p.get(m, {}).get("error"))}
                continue
            calls = [i for i, p in enumerate(pts) if p["margin_call"]]
            changes = sum(1 for a, b in itertools.pairwise(pts) if a["state"] != b["state"])
            summary[m] = {
                "evaluations": len(pts),
                "margin_call_share": len(calls) / len(pts),
                "first_margin_call": series[calls[0]]["date"] if calls else None,
                "state_changes": changes,
                "mean_capacity_ltv": sum((p["capacity_ltv"] or 0) for p in pts) / len(pts),
                "min_available_credit": min((p["approved_credit"] or 0) - (p["loan"] or 0) for p in pts),
            }
        out = {"sessions": len(dates), "window": [dates[start], dates[-1]], "step": step, "models": summary}
        with db.transaction("*") as conn:
            conn.execute("UPDATE backtest_runs SET status='succeeded', finished_at=%s, summary=%s, series=%s "
                         "WHERE run_id=%s", (datetime.now(UTC).isoformat(), json.dumps(out),
                                             json.dumps(series, default=str), run_id))
        return out
    except Exception as exc:
        with db.transaction("*") as conn:
            conn.execute("UPDATE backtest_runs SET status='failed', finished_at=%s, error=%s WHERE run_id=%s",
                         (datetime.now(UTC).isoformat(), f"{type(exc).__name__}: {exc}"[:1000], run_id))
        raise
    finally:
        reset_principal(token)
