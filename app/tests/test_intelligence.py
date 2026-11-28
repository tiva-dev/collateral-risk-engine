"""CRI Intelligence: research dataset boundary and integrity; benchmark pipeline."""

from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pytest

from app.learning.datasets import (
    BAR_FIELDS,
    DatasetError,
    _gz_csv,
    _sha256,
    load_research_dataset,
)


def write_dataset(out: Path, market: str = "US", n_sym: int = 10, start: date = date(2018, 1, 2),
                  end: date = date(2026, 11, 25), tier: str = "examined", boundary: str = "2026-11-25",
                  seed: int = 3, stale: float = 0.0) -> Path:
    rng = np.random.default_rng(seed)
    rows = []
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    days = [d for d in days if d.weekday() < 5]
    for k in range(n_sym):
        p = 50.0 + 10 * k
        vol = 0.01 + 0.004 * k
        regime = 1.0
        for d in days:
            if rng.random() < 0.01:
                regime = 2.5 if regime == 1.0 else 1.0          # persistent vol regimes (predictable)
            r = rng.standard_t(4) * vol * regime / np.sqrt(2)
            if stale and rng.random() < stale:
                r = 0.0                                         # stale print (illiquid market)
            o = p
            p *= float(np.exp(r))
            rows.append([f"S{k}", d.isoformat(), o, max(o, p) * 1.003, min(o, p) * 0.997, p, p,
                         1e6 * (1 + k), "", "USD", "traded", 1, 1, 1.0])
    out.mkdir(parents=True, exist_ok=True)
    _gz_csv(out / "bars.csv.gz", BAR_FIELDS, rows)
    _gz_csv(out / "fx.csv.gz", ["from", "to", "date", "rate"], [])
    files = [{"name": n, "sha256": _sha256(out / n), "bytes": (out / n).stat().st_size}
             for n in ("bars.csv.gz", "fx.csv.gz")]
    meta = {"dataset_id": f"{market.lower()}-" + hashlib.sha256(files[0]["sha256"].encode()).hexdigest()[:8],
            "market": market, "tier": tier, "boundary": boundary, "first_date": start.isoformat(),
            "last_date": end.isoformat(), "rows": len(rows), "files": files}
    (out / "DATASET.json").write_text(json.dumps(meta))
    return out


def test_loader_enforces_boundary_tier_and_checksums(tmp_path) -> None:
    ok = write_dataset(tmp_path / "ok", n_sym=2, start=date(2026, 1, 2))
    assert len(load_research_dataset(ok).bars) == 2
    with pytest.raises(DatasetError):
        load_research_dataset(write_dataset(tmp_path / "late", n_sym=1, start=date(2026, 11, 1),
                                            end=date(2026, 11, 27)))
    with pytest.raises(DatasetError):
        load_research_dataset(write_dataset(tmp_path / "b", n_sym=1, start=date(2026, 11, 1),
                                            boundary="2026-11-27"))
    with pytest.raises(DatasetError):
        load_research_dataset(write_dataset(tmp_path / "t", n_sym=1, start=date(2026, 11, 1), tier="locked"))
    bad = write_dataset(tmp_path / "x", n_sym=1, start=date(2026, 11, 1))
    (bad / "fx.csv.gz").write_bytes(b"tampered")
    with pytest.raises(DatasetError):
        load_research_dataset(bad)


def test_benchmark_pipeline_runs_and_is_point_in_time(tmp_path) -> None:
    from app.intelligence.benchmark import _splits, build_panel, evaluate, fit_predict
    ds = load_research_dataset(write_dataset(tmp_path / "d", n_sym=10))
    p = build_panel([ds], step=20, rc1=True)
    sp = _splits(p)
    assert sp["train"].any() and sp["val"].any() and sp["test"].any() and sp["test_unseen"].any()
    # purge by actual target end dates: no training target reaches past the training end
    from app.intelligence.benchmark import TRAIN_END, VAL_END
    assert p.end_day[sp["train"]].max() <= TRAIN_END.toordinal()
    assert p.end_day[sp["val"]].max() <= VAL_END.toordinal() and p.day[sp["val"]].min() > TRAIN_END.toordinal()
    # liquidity terciles are contemporaneous: every date with >= 3 names has all three buckets
    d0 = p.day[0]
    assert set(p.adv_bucket[p.day == d0]) == {0, 1, 2}
    preds = fit_predict(p, sp)
    ev = evaluate(p, sp, preds)
    t = ev["test"]
    assert set(t) >= {"rc1", "har", "ridge", "gbt", "hier", "gbt_q"}
    for name in ("rc1", "har", "gbt"):
        assert 0.0 <= t[name]["exceedance_rate"] <= 0.25
        assert 0.0 < t[name]["mean_ltv_ex_ante"] < 1.0
    # learned models exploit persistent regimes that a long-memory estimator smooths over
    assert t["gbt"]["qlike"] < t["rc1"]["qlike"] * 1.5


def test_stale_price_windows_do_not_degenerate_forecasts(tmp_path) -> None:
    """NGX-style stale windows (10 sessions with no price change) give rv == 0.
    v1 fitted log(rv + 1e-6) and collapsed LTV to ~0; v2 floors per market."""
    from app.intelligence.benchmark import _splits, build_panel, evaluate, fit_predict
    ds = load_research_dataset(write_dataset(tmp_path / "s", market="NGX", n_sym=10, stale=0.7, seed=5))
    p = build_panel([ds], step=10, rc1=True)
    assert (p.rv == 0).mean() > 0.01                              # the failure mode is present
    sp = _splits(p)
    preds = fit_predict(p, sp)
    fl = preds["_floor"]["floor"]
    assert np.all(fl > 0)
    ev = evaluate(p, sp, preds)
    t = ev["test"]
    assert t["zero_realised_share"] > 0
    for name in ("har", "ridge", "gbt", "hier"):
        assert np.all(preds[name]["sigma"] >= fl - 1e-15)
        assert t[name]["qlike"] < 5.0                             # v1: thousands
        assert t[name]["mean_ltv_ex_ante"] > 0.5                  # v1: ~0
        assert 0.0 <= t[name]["exceedance_rate"] <= 0.2


def test_market_without_training_rows_uses_pooled_calibration(tmp_path) -> None:
    from app.intelligence.benchmark import _splits, build_panel, evaluate, fit_predict
    us = load_research_dataset(write_dataset(tmp_path / "u", n_sym=8))
    ng = load_research_dataset(write_dataset(tmp_path / "n", market="NGX", n_sym=4, start=date(2023, 1, 2), seed=9))
    p = build_panel([us, ng], step=20)
    sp = _splits(p)
    assert not (sp["train"] & (p.market == "NGX")).any()        # NGX history starts after TRAIN_END
    preds = fit_predict(p, sp)
    for name in ("har", "ridge", "gbt", "hier", "rc1"):
        assert np.all(np.isfinite(preds[name]["q"]))
        assert set(preds[name]["z_by_market"]) == {"US", "NGX"}
    t = evaluate(p, sp, preds)["test"]
    assert np.isfinite(t["ridge"]["pinball_q05"])
