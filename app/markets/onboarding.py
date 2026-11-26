"""Standard market-onboarding validation.

A market is onboarded by (1) writing a MarketProfile with provenance for every
execution characteristic and (2) running these checks on the market's observed
data. The checks decide whether CRI's universal estimators can be applied
as-is, need a documented data treatment, or must not be used yet. They never
create a market-specific risk model.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from typing import Any

from app.markets.profiles import MarketProfile


def _returns(closes: Sequence[float]) -> list[float]:
    return [math.log(b / a) for a, b in itertools.pairwise(closes) if a > 0 and b > 0]


def variance_ratio(r: Sequence[float], q: int = 10) -> float | None:
    if len(r) < q * 10:
        return None
    mu = sum(r) / len(r)
    v1 = sum((x - mu) ** 2 for x in r) / (len(r) - 1)
    agg = [sum(r[i:i + q]) for i in range(len(r) - q + 1)]
    mq = sum(agg) / len(agg)
    vq = sum((x - mq) ** 2 for x in agg) / (len(agg) - 1)
    return vq / (q * v1) if v1 > 0 else None


def instrument_checks(profile: MarketProfile, bars: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [b["close"] for b in bars if b.get("close")]
    r = _returns(closes)
    n = len(bars)
    out: dict[str, Any] = {"sessions": n}
    if n < 60:
        out["status"] = "insufficient_history"
        return out
    zero = sum(1 for x in r if abs(x) < 1e-12) / max(len(r), 1)
    out["zero_return_share"] = zero
    out["range_reported_share"] = sum(1 for b in bars if b.get("range_reported")) / n
    out["volume_reported_share"] = sum(1 for b in bars if b.get("volume_reported")) / n
    out["variance_ratio_10"] = variance_ratio(r)
    big = [abs(math.expm1(x)) for x in r]
    if profile.daily_price_limit:
        lim = profile.daily_price_limit
        at = sum(1 for x in big if abs(x - lim) < 0.0051) / max(len(big), 1)
        beyond = sum(1 for x in big if x > lim + 0.0051) / max(len(big), 1)
        out["moves_at_configured_limit"] = at
        out["moves_beyond_configured_limit"] = beyond
    out["max_abs_daily_move"] = max(big) if big else None
    return out


def validate_market(profile: MarketProfile, bars_by_instrument: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    per = {k: instrument_checks(profile, v) for k, v in bars_by_instrument.items()}
    ok = [v for v in per.values() if v.get("status") != "insufficient_history"]
    findings: list[dict[str, str]] = []

    def share(key: str) -> float | None:
        vals = [v[key] for v in ok if v.get(key) is not None]
        return sum(vals) / len(vals) if vals else None

    vol_rep, rng_rep, zero = share("volume_reported_share"), share("range_reported_share"), share("zero_return_share")
    vr = [v["variance_ratio_10"] for v in ok if v.get("variance_ratio_10")]
    if vol_rep is not None and vol_rep < 0.8:
        findings.append({"check": "volume", "result": "treatment_required",
                         "detail": f"volume reported on {vol_rep:.0%} of sessions; liquidity must use explicit "
                                   "scenarios where unobserved (never backfilled)"})
    if rng_rep is not None and rng_rep < 0.8:
        findings.append({"check": "spread", "result": "treatment_required",
                         "detail": f"high/low reported on {rng_rep:.0%} of sessions; spread falls back to the "
                                   "profile's documented fallback where not estimable"})
    if zero is not None and zero > 0.15:
        findings.append({"check": "stale_prices", "result": "treatment_required",
                         "detail": f"{zero:.0%} of returns are exactly zero; one-day volatility understates "
                                   "multi-session risk (use multi-session aggregation)"})
    if vr and sorted(vr)[len(vr) // 2] > 1.3:
        findings.append({"check": "autocorrelation", "result": "treatment_required",
                         "detail": f"median 10-day variance ratio {sorted(vr)[len(vr) // 2]:.2f} > 1.3"})
    if profile.daily_price_limit:
        at = share("moves_at_configured_limit")
        beyond = share("moves_beyond_configured_limit")
        findings.append({"check": "price_limit", "result": "recorded_only",
                         "detail": f"{(at or 0):.1%} of moves at the configured {profile.daily_price_limit:.0%} "
                                   f"limit, {(beyond or 0):.2%} beyond it; price-limit modelling is "
                                   f"{profile.status.get('daily_price_limit', 'configured')}"})
    assumptions = [k for k, v in profile.provenance.items() if v.startswith("assumption")]
    if assumptions:
        findings.append({"check": "profile_provenance", "result": "confirmation_required",
                         "detail": "unconfirmed profile fields: " + ", ".join(sorted(assumptions))})
    blocking = [f for f in findings if f["result"] == "blocked"]
    return {
        "market": profile.code,
        "instruments": len(per),
        "instruments_with_history": len(ok),
        "decision": "blocked" if blocking else ("usable_with_treatments" if findings else "usable"),
        "findings": findings,
        "per_instrument": per,
        "profile": profile.to_dict(),
    }
