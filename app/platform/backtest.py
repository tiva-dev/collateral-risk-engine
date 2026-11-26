"""Historical monitoring backtests over persisted market bars.

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

DEFAULT_MODELS = ["baseline", "cre_v070", "A", "B", "B_gap"]


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


def run(db: Database, services: Any, run_id: str) -> dict[str, Any]:
    from app.lifecycle.service import aggregate_holdings
    from app.market_data.identity import InstrumentIdentity
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
            holdings = [h for h in aggregate_holdings(account.holdings) if h.asset_type.value != "cash"]
            keys = {h.asset_id: InstrumentIdentity.from_holding(h).stable_key for h in holdings}
            bars = {k: load_bars(k, 2000) for k in keys.values()}
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
            md = {aid: _market_data_on(aid, visible[k], ts) for aid, k in keys.items()}
            years = (ts - first_date).days / 365.0
            loan = account.loan.__class__(**{**{f: getattr(account.loan, f) for f in account.loan.__dataclass_fields__},
                                            "principal": principal * (1 + rate * years), "accrued_interest": 0.0})
            acc = account.__class__(**{**{f: getattr(account, f) for f in account.__dataclass_fields__ if
                                          account.__dataclass_fields__[f].init}, "loan": loan})
            inp = shadow.ShadowInputs(tenant, acc, holdings, md, ts, bars=visible)
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
