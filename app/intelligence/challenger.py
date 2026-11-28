"""US ridge volatility challenger: frozen specification and prospective shadow
(RC2 research; Report 17 review, section 2).

Freeze (once)
    Fits ``ridge_ns`` (ridge on the benchmark features without the history-length
    feature) on every examined US observation (to EXAMINED_THROUGH), fixes the
    volatility floor and the ex-ante calibration rule, and computes the initial
    calibration pools for the challenger and for RC1's standalone estimator from
    out-of-fold forecasts. Everything that determines a forecast is written to one
    immutable artifact in the private research bucket (create-only), with the
    training-data hashes, code version and library versions, and its own SHA-256.
    The model is registered at research stage and moved to shadow. Parameters are
    never printed to logs (the repository is public).

Prospective shadow (daily, after the US close)
    Fetches SIP daily bars for the frozen universe up to the decision date only,
    computes features with the training code (``instrument_series``/``row_features``),
    and records per instrument: the challenger's ex-ante haircut, RC1's standalone
    estimator under the same rule, and RC1's own approved, uncapped and ceiling LTVs.
    Outcomes that have completed by the decision date join the calibration pools as
    the frozen rule prescribes; nothing else adapts. Records are create-only.
    RC1 decisions are never read from or written to here, and RC1's locked
    evaluation snapshot is never read: like RC1's live shadow, this decides on
    current data. Results are evaluated only under the pre-registered plan below.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from app.intelligence.benchmark import (
    H,
    _rc1_sigma,
    build_panel,
    instrument_series,
    row_features,
)
from app.intelligence.exante import BREACH, CAL_CONF, CAL_MIN, CAL_YEARS, SESSIONS_COL

CHALLENGER_ID = "intelligence.us_ridge_ns"
VERSION = "1.0.0"
MARKET = "US"
RIDGE_ALPHA = 1.0
FLOOR_PCT = 0.05
MIN_HIST = 260
FEATURE_COLS = [i for i in range(17) if i != SESSIONS_COL]
EVALUATION_PLAN = {
    "minimum_sessions": 60,
    "unit": "instrument x decision date; outcome = log return over the next 10 sessions",
    "primary": ["breach rate vs 1% (Kupiec)", "1% pinball loss of the haircut", "mean LTV",
                "mean LTV with RC1's contractual ceiling applied to every method"],
    "secondary": ["QLIKE", "breach clustering", "by-instrument results"],
    "comparators": ["RC1 standalone estimator under the same ex-ante rule", "RC1 approved LTV (complete framework)"],
    "inference": "moving-block bootstrap over calendar blocks; no interim tuning; results reported once >= 60 sessions",
    "promotion": ("no automatic promotion; any use in lending requires an RC2 methodology version, "
                  "independent validation and an authenticated approval"),
}


# ------------------------------------------------------------------ storage

def _token() -> str:
    from app.analyst.llm import gcp_access_token
    return gcp_access_token()


def _split(uri: str) -> tuple[str, str]:
    bucket, _, name = uri[len("gs://"):].partition("/")
    return bucket, name


def gcs_read(uri: str) -> tuple[bytes, int] | None:
    """(bytes, generation) or None if the object does not exist."""
    b, n = _split(uri)
    meta = urllib.request.Request(f"https://storage.googleapis.com/storage/v1/b/{b}/o/{urllib.parse.quote(n, safe='')}",
                                  headers={"Authorization": f"Bearer {_token()}"})
    try:
        with urllib.request.urlopen(meta, timeout=60) as r:  # nosec B310 - fixed GCS endpoint
            gen = int(json.loads(r.read())["generation"])
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    req = urllib.request.Request(
        f"https://storage.googleapis.com/storage/v1/b/{b}/o/{urllib.parse.quote(n, safe='')}?alt=media&generation={gen}",
        headers={"Authorization": f"Bearer {_token()}"})
    with urllib.request.urlopen(req, timeout=300) as r:  # nosec B310 - fixed GCS endpoint
        return r.read(), gen


def gcs_write(uri: str, data: bytes, if_generation: int = 0) -> bool:
    """Create-only by default (ifGenerationMatch=0). False if the precondition fails."""
    b, n = _split(uri)
    req = urllib.request.Request(
        f"https://storage.googleapis.com/upload/storage/v1/b/{b}/o?uploadType=media"
        f"&name={urllib.parse.quote(n, safe='')}&ifGenerationMatch={if_generation}",
        data=data, method="POST", headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120):  # nosec B310 - fixed GCS endpoint
            return True
    except urllib.error.HTTPError as e:
        if e.code == 412:
            return False
        raise


def gcs_list(prefix_uri: str) -> list[str]:
    b, p = _split(prefix_uri)
    out, token = [], ""
    while True:
        url = (f"https://storage.googleapis.com/storage/v1/b/{b}/o?prefix={urllib.parse.quote(p, safe='')}"
               + (f"&pageToken={token}" if token else ""))
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": f"Bearer {_token()}"}),
                                    timeout=60) as r:  # nosec B310 - fixed GCS endpoint
            j = json.loads(r.read())
        out += [f"gs://{b}/{it['name']}" for it in j.get("items", [])]
        token = j.get("nextPageToken", "")
        if not token:
            return out


# ------------------------------------------------------------------ model

def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=float).encode()


def artifact_sha(art: dict[str, Any]) -> str:
    return hashlib.sha256(canonical({k: v for k, v in art.items() if k != "artifact_sha256"})).hexdigest()


def predict_sigma(art: dict[str, Any], X: np.ndarray) -> np.ndarray:
    m = art["model"]
    Z = (X[:, FEATURE_COLS] - np.array(m["scaler_mean"])) / np.array(m["scaler_scale"])
    return np.maximum(np.exp(Z @ np.array(m["coef"]) + m["intercept"]), art["floor"])


def z_from_pool(end_days: np.ndarray, u: np.ndarray, decision_day: int) -> float | None:
    """The frozen ex-ante rule: outcomes completed by the decision date in the trailing
    CAL_YEARS; conservative order statistic at the lower (1 - CAL_CONF) binomial point."""
    from scipy.stats import binom
    sel = (end_days <= decision_day) & (end_days >= decision_day - round(365.25 * CAL_YEARS)) & np.isfinite(u)
    v = u[sel]
    if v.size < CAL_MIN:
        return None
    j = max(1, int(binom.ppf(1 - CAL_CONF, v.size, BREACH)))
    return float(np.partition(v, j - 1)[j - 1])


def _oof_sigma(p: Any, y: np.ndarray, cols: list[int], floor: float) -> np.ndarray:
    """Out-of-fold ridge forecasts by calendar year, purged of overlapping targets."""
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    years = np.array([date.fromordinal(int(d)).year for d in p.day])
    s = np.full(y.size, np.nan)
    for yr in np.unique(years):
        lo, hi = date(int(yr), 1, 1).toordinal(), date(int(yr), 12, 31).toordinal()
        fold = years == yr
        fit = (p.end_day < lo) | (p.day > hi)
        if fit.sum() < 500:
            continue
        mdl = make_pipeline(StandardScaler(), Ridge(alpha=RIDGE_ALPHA)).fit(p.X[fit][:, cols], y[fit])
        s[fold] = np.maximum(np.exp(mdl.predict(p.X[fold][:, cols])), floor)
    return s


def freeze(dataset: Any, code_version: str) -> dict[str, Any]:
    from sklearn import __version__ as skv
    from sklearn.linear_model import Ridge
    from sklearn.preprocessing import StandardScaler

    from app.platform.governance.data_lock import EXAMINED_THROUGH
    if dataset.meta["market"] != MARKET:
        raise ValueError("the US challenger is trained on the US research dataset only")
    p = build_panel([dataset], step=1, min_hist=MIN_HIST, rc1=False)
    if p.end_day.max() > EXAMINED_THROUGH.toordinal():
        raise ValueError("training targets extend past the examined boundary")
    pos = p.rv[p.rv > 0]
    floor = max(1e-4, float(np.quantile(pos, FLOOR_PCT)))
    y = np.log(np.maximum(p.rv, floor))
    sc = StandardScaler().fit(p.X[:, FEATURE_COLS])
    rg = Ridge(alpha=RIDGE_ALPHA).fit(sc.transform(p.X[:, FEATURE_COLS]), y)
    s_oof = _oof_sigma(p, y, FEATURE_COLS, floor)
    cut = p.end_day.max() - round(365.25 * CAL_YEARS)
    keep = (p.end_day >= cut) & np.isfinite(s_oof)
    pool = {"end_day": p.end_day[keep].astype(int).tolist(), "u": (p.ret[keep] / s_oof[keep]).tolist()}
    # RC1 standalone comparator pool, same rule (RC1's frozen estimator; every 5th session).
    p5 = build_panel([dataset], step=5, min_hist=MIN_HIST, rc1=True)
    k5 = (p5.end_day >= cut) & np.isfinite(p5.rc1) & (p5.rc1 > 0)
    rc1_pool = {"end_day": p5.end_day[k5].astype(int).tolist(), "u": (p5.ret[k5] / p5.rc1[k5]).tolist()}
    universe = sorted({str(s).split(":", 1)[1] for s in p.sym})
    art: dict[str, Any] = {
        "challenger_id": CHALLENGER_ID, "version": VERSION, "market": MARKET, "horizon_sessions": H,
        "frozen_at": datetime.now(UTC).isoformat(), "code_version": code_version,
        "libraries": {"numpy": np.__version__, "scikit-learn": skv},
        "specification": {
            "target": "log(max(realised 10-session volatility, floor))",
            "features": "app.intelligence.benchmark.row_features without the history-length feature",
            "feature_columns": FEATURE_COLS, "preprocessing": "standardise (training mean/scale)",
            "estimator": f"ridge alpha={RIDGE_ALPHA}", "min_history_sessions": MIN_HIST, "training_step_sessions": 1,
            "floor": f"{FLOOR_PCT:.0%} quantile of positive training realised volatility",
            "calibration": {"rule": "z_t = conservative order statistic of completed standardised outcomes",
                            "breach": BREACH, "window_years": CAL_YEARS, "min_outcomes": CAL_MIN,
                            "confidence": CAL_CONF, "initial_pool": "out-of-fold (calendar-year folds, purged)",
                            "prospective_update": "completed live outcomes join the pool; nothing else adapts"},
            "haircut": "log-return threshold q = z_t * sigma_t; LTV = exp(q)",
        },
        "training_data": {"dataset_id": dataset.meta["dataset_id"],
                          "files": {f["name"]: f["sha256"] for f in dataset.meta.get("files", [])},
                          "first_target_day": date.fromordinal(int(p.day.min())).isoformat(),
                          "data_through": dataset.meta["last_date"], "rows": int(p.X.shape[0]),
                          "evidence_class": "public_historical (examined)"},
        "universe": universe,
        "model": {"scaler_mean": sc.mean_.tolist(), "scaler_scale": sc.scale_.tolist(),
                  "coef": rg.coef_.tolist(), "intercept": float(rg.intercept_)},
        "floor": floor, "calibration_pool": pool, "rc1_standalone_pool": rc1_pool,
        "evaluation_plan": EVALUATION_PLAN,
    }
    art["artifact_sha256"] = artifact_sha(art)
    return art


def load_artifact(blob: bytes) -> dict[str, Any]:
    art = json.loads(blob)
    if artifact_sha(art) != art.get("artifact_sha256"):
        raise ValueError("challenger artifact failed its integrity check")
    return art


# ------------------------------------------------------------------ live

def rows_from_bars(bars: list[Any]) -> list[dict[str, Any]]:
    out, seen = [], set()
    for b in sorted(bars, key=lambda x: x.timestamp):
        d = b.timestamp.date() if hasattr(b.timestamp, "date") else b.timestamp
        if d in seen:
            continue
        seen.add(d)
        prov = getattr(b, "field_provenance", None) or {}
        out.append({"date": d, "open": float(b.open), "high": float(b.high), "low": float(b.low),
                    "close": float(b.close), "adj_close": float(b.adjusted_close or b.close),
                    "volume": float(b.volume or 0.0),
                    "range_reported": prov.get("high", "reported") == "reported"
                    and prov.get("low", "reported") == "reported",
                    "volume_reported": prov.get("volume", "reported") == "reported"})
    return out


def forecast_day(art: dict[str, Any], rows_by_symbol: dict[str, list[dict[str, Any]]], decision: date,
                 prospective: list[tuple[int, str, float, float]], rc1_full: bool = True) -> dict[str, Any]:
    """Forecasts at ``decision`` from rows dated on or before it. ``prospective``: completed
    live outcomes (end_day, method, ret, sigma) that join the frozen calibration pools."""
    from app.intelligence.exante import rc1_full_ltv_task
    from research.methodology_lab.candidate_validation import priors_at
    rows_by_symbol = {s: [r for r in v if r["date"] <= decision] for s, v in rows_by_symbol.items()}
    pre: dict[str, tuple[list[dict[str, Any]], dict[str, np.ndarray]]] = {}
    for s in art["universe"]:
        rows = rows_by_symbol.get(s, [])
        if len(rows) < MIN_HIST + 1 or rows[-1]["date"] != decision:
            continue
        ser = instrument_series(rows)
        if ser is not None:
            pre[s] = (rows, ser)
    if not pre:
        return {"decision_date": decision.isoformat(), "instruments": {}, "note": "no instrument traded on this date"}
    med = float(np.nanmedian([ser["rv22"][len(rows) - 1] for rows, ser in pre.values()]))
    dd = decision.toordinal()
    pools = {}
    for method, key in (("challenger", "calibration_pool"), ("rc1_standalone", "rc1_standalone_pool")):
        live = [(e, r / sg) for e, m, r, sg in prospective if m == method and sg > 0]
        e = np.array(art[key]["end_day"] + [x[0] for x in live], float)
        u = np.array(art[key]["u"] + [x[1] for x in live], float)
        pools[method] = z_from_pool(e, u, dd)
    all_bars = {s: [{"bar_date": r["date"], "close": r["adj_close"]} for r in rows] for s, (rows, _) in pre.items()}
    prior = priors_at(MARKET, all_bars, decision) if rc1_full else {}
    out: dict[str, Any] = {}
    for s, (rows, ser) in pre.items():
        i = len(rows) - 1
        x = np.array([row_features(ser, i, med)])
        if not np.all(np.isfinite(x[:, FEATURE_COLS])):
            out[s] = {"skipped": "non-finite features"}
            continue
        sig = float(predict_sigma(art, x)[0])
        r1 = _rc1_sigma(rows, i)
        rec: dict[str, Any] = {"close": rows[i]["adj_close"]}
        for method, sigma in (("challenger", sig), ("rc1_standalone", r1 * math.sqrt(H))):
            z = pools[method]
            q = None if z is None or not math.isfinite(sigma) else z * sigma
            rec[method] = {"sigma": sigma, "z": z, "q": q, "ltv": None if q is None else min(1.0, math.exp(q))}
        if rc1_full:
            appr, unc, ceil = rc1_full_ltv_task((rows, i, MARKET, s, prior))
            rec["rc1_full"] = {"approved_ltv": appr, "uncapped_ltv": unc, "ceiling_ltv": ceil}
        out[s] = rec
    return {"decision_date": decision.isoformat(), "artifact_sha256": art["artifact_sha256"],
            "market_median_rv22": med, "pool_sizes": {k: v is not None for k, v in pools.items()},
            "instruments": out}


def realised(rows: list[dict[str, Any]], decision: date, h: int = H) -> tuple[date, float] | None:
    """End date and log return over the h sessions after ``decision`` (None if incomplete)."""
    idx = next((j for j, r in enumerate(rows) if r["date"] == decision), None)
    if idx is None or idx + h >= len(rows):
        return None
    return rows[idx + h]["date"], math.log(rows[idx + h]["adj_close"] / rows[idx]["adj_close"])


# ------------------------------------------------------------------ CLI

def _base(prefix: str) -> str:
    return f"{prefix.rstrip('/')}/challengers/us_ridge_ns/{VERSION}"


def cmd_freeze(a: argparse.Namespace) -> None:
    from app.intelligence.benchmark import _gcs_download
    from app.learning.datasets import load_research_dataset
    from app.learning.governance import ModelRecord, ModelRegistry, Stage
    tmp = Path(tempfile.mkdtemp())
    path = _gcs_download(a.dataset, tmp / "ds") if a.dataset.startswith("gs://") else Path(a.dataset)
    ds = load_research_dataset(path)
    base = _base(a.out)
    existing = gcs_read(f"{base}/artifact.json")
    if existing is not None:
        # Versions are immutable. Resume registration with the stored artifact only if it is
        # intact and was frozen from the same training dataset; never re-freeze over it.
        art = load_artifact(existing[0])
        if art["training_data"]["dataset_id"] != ds.meta["dataset_id"]:
            raise SystemExit("a different artifact already exists for this version: versions are immutable")
    else:
        art = freeze(ds, a.code_version or os.environ.get("CRI_CODE_VERSION", "unknown"))
        blob = json.dumps(art, sort_keys=True, default=float).encode()
        if not gcs_write(f"{base}/artifact.json", blob):
            raise SystemExit("artifact created concurrently; re-run to resume registration")
    # Registry (append-only JSONL in the private bucket; optimistic concurrency).
    reg_uri = f"{a.out.rstrip('/')}/registry/models.jsonl"
    cur = gcs_read(reg_uri)
    local = tmp / "models.jsonl"
    local.write_bytes(cur[0] if cur else b"")
    reg = ModelRegistry(local)
    key = f"{CHALLENGER_ID}@{VERSION}"
    known = reg.records().get(key)
    if known is not None and known.config_hash != art["artifact_sha256"]:
        raise SystemExit(f"{key} is registered with a different artifact")
    rec = known or reg.register(ModelRecord(model_id=CHALLENGER_ID, version=VERSION, component="intelligence",
                                   task="10-session downside volatility and ex-ante haircut (US)",
                                   config_hash=art["artifact_sha256"],
                                   dataset_hashes=[art["training_data"]["dataset_id"]],
                                   data_through=art["training_data"]["data_through"],
                                   metrics={"artifact": f"{base}/artifact.json"}))
    if rec.stage != Stage.SHADOW:
        reg.promote(rec.key, Stage.SHADOW, actor="cri-research/freeze",
                    reason="frozen for prospective evaluation (Report 17 review); no influence on RC1")
    if not gcs_write(reg_uri, local.read_bytes(), if_generation=cur[1] if cur else 0):
        raise SystemExit("registry changed concurrently; the artifact exists, re-run registration")
    print("CRI_CHALLENGER_FROZEN " + json.dumps({"id": CHALLENGER_ID, "version": VERSION,
                                                 "artifact_sha256": art["artifact_sha256"],
                                                 "training_rows": art["training_data"]["rows"],
                                                 "universe": len(art["universe"]), "stage": "shadow"}), flush=True)


# History needed: every calibration outcome in the trailing window plus its decision bar
# (h sessions earlier) and a holiday buffer; never less than the feature lookback.
FETCH_DAYS = max(1100, round(365.25 * CAL_YEARS) + 2 * H + 30)

INPUT_FIELDS = ("open", "high", "low", "close", "adj_close", "volume", "range_reported", "volume_reported")


def inputs_digest(rows_by_symbol: dict[str, list[dict[str, Any]]], decision: date) -> str:
    """sha256 of every bar field that can influence a forecast, up to the decision date."""
    return hashlib.sha256(canonical({s: [[r["date"].isoformat(), *(r[k] for k in INPUT_FIELDS)]
                                         for r in v if r["date"] <= decision]
                                     for s, v in sorted(rows_by_symbol.items())})).hexdigest()


def last_completed_session_day(now_utc: datetime) -> date:
    """Latest calendar day whose US daily bar is final and older than the 15-minute SIP
    embargo: today after 16:20 New York time, otherwise yesterday."""
    from zoneinfo import ZoneInfo
    ny = now_utc.astimezone(ZoneInfo("America/New_York"))
    return ny.date() if (ny.hour, ny.minute) >= (16, 20) else ny.date() - timedelta(days=1)


def cmd_forecast(a: argparse.Namespace) -> None:
    from app.historical_data.alpaca import AlpacaTradingHistoricalProvider
    base = _base(a.out)
    got = gcs_read(f"{base}/artifact.json")
    if got is None:
        raise SystemExit("no frozen artifact")
    art = load_artifact(got[0])
    today = date.fromisoformat(a.asof) if a.asof else last_completed_session_day(datetime.now(UTC))
    prov = AlpacaTradingHistoricalProvider()
    rows_by_symbol = {}
    for s in art["universe"]:
        ser = prov.fetch_equity_history(s, today - timedelta(days=FETCH_DAYS), today, adjustment="all", feed="sip",
                                        force_refresh=True)
        rows_by_symbol[s] = rows_from_bars(ser.bars)
    days = sorted({r["date"] for v in rows_by_symbol.values() for r in v if r["date"] <= today})
    decision = days[-1]
    # Completed prospective outcomes from earlier records (create-only files).
    prospective: list[tuple[int, str, float, float]] = []
    for uri in gcs_list(f"{base}/forecasts/"):
        rec = json.loads(gcs_read(uri)[0])                     # type: ignore[index]
        d0 = date.fromisoformat(rec["decision_date"])
        for s, f in rec["instruments"].items():
            if "skipped" in f:
                continue
            rz = realised(rows_by_symbol.get(s, []), d0)
            if rz is None or rz[0] > decision:
                continue
            for method in ("challenger", "rc1_standalone"):
                sg = f[method]["sigma"]
                if sg and math.isfinite(sg):
                    prospective.append((rz[0].toordinal(), method, rz[1], sg))
    rec = forecast_day(art, rows_by_symbol, decision, prospective)
    rec["generated_at"] = datetime.now(UTC).isoformat()
    rec["inputs_sha256"] = inputs_digest(rows_by_symbol, decision)
    rec["inputs_fields"] = list(INPUT_FIELDS)
    rec["prospective_outcomes_in_pool"] = len(prospective) // 2
    wrote = gcs_write(f"{base}/forecasts/{decision.isoformat()}.json", canonical(rec))
    print("CRI_CHALLENGER_FORECAST " + json.dumps({"decision_date": decision.isoformat(), "written": wrote,
                                                   "instruments": len(rec["instruments"]),
                                                   "prospective_outcomes": len(prospective) // 2}), flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("freeze")
    f.add_argument("--dataset", required=True)
    f.add_argument("--out", required=True, help="gs://bucket prefix of the private research store")
    f.add_argument("--code-version", default="")
    g = sub.add_parser("forecast")
    g.add_argument("--out", required=True)
    g.add_argument("--asof", default="")
    a = ap.parse_args()
    {"freeze": cmd_freeze, "forecast": cmd_forecast}[a.cmd](a)


if __name__ == "__main__":
    main()
