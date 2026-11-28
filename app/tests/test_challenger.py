"""US ridge challenger: frozen artifact, train/serve parity, point-in-time live forecasts."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from app.intelligence import challenger as ch
from app.intelligence.benchmark import build_panel
from app.learning.datasets import load_research_dataset
from app.tests.test_intelligence import write_dataset


@pytest.fixture(scope="module")
def frozen(tmp_path_factory):
    d = tmp_path_factory.mktemp("ch")
    ds = load_research_dataset(write_dataset(d / "us", n_sym=6, seed=12))
    return ds, ch.freeze(ds, "test")


def test_artifact_is_complete_and_self_verifying(frozen) -> None:
    ds, art = frozen
    assert art["artifact_sha256"] == ch.artifact_sha(art)
    again = ch.load_artifact(json.dumps(art).encode())
    assert again["model"] == art["model"]
    tampered = dict(art, floor=art["floor"] * 2)
    with pytest.raises(ValueError):
        ch.load_artifact(json.dumps(tampered).encode())
    for k in ("specification", "training_data", "evaluation_plan", "calibration_pool", "rc1_standalone_pool",
              "code_version", "libraries", "universe"):
        assert art[k]
    assert art["training_data"]["dataset_id"] == ds.meta["dataset_id"]
    assert len(art["calibration_pool"]["u"]) >= 250 and len(art["model"]["coef"]) == len(ch.FEATURE_COLS)


def test_non_us_dataset_is_refused(tmp_path) -> None:
    ds = load_research_dataset(write_dataset(tmp_path / "n", market="NGX", n_sym=3, seed=2))
    with pytest.raises(ValueError):
        ch.freeze(ds, "test")


def test_prediction_matches_the_fitted_ridge(frozen) -> None:
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    ds, art = frozen
    p = build_panel([ds], step=1, min_hist=ch.MIN_HIST, rc1=False)
    y = np.log(np.maximum(p.rv, art["floor"]))
    mdl = make_pipeline(StandardScaler(), Ridge(alpha=ch.RIDGE_ALPHA)).fit(p.X[:, ch.FEATURE_COLS], y)
    np.testing.assert_allclose(ch.predict_sigma(art, p.X[:50]),
                               np.maximum(np.exp(mdl.predict(p.X[:50][:, ch.FEATURE_COLS])), art["floor"]),
                               rtol=1e-9)


def test_live_features_equal_training_features(frozen) -> None:
    """Train/serve parity: the live path reproduces the panel's features at a date."""
    ds, art = frozen
    p = build_panel([ds], step=1, min_hist=ch.MIN_HIST, rc1=False)
    d = sorted(set(p.day))[len(set(p.day)) // 2]
    rec = ch.forecast_day(art, ds.bars, __import__("datetime").date.fromordinal(int(d)), [], rc1_full=False)
    for sym, f in rec["instruments"].items():
        j = np.where((p.day == d) & (p.sym == f"US:{sym}"))[0]
        assert j.size == 1
        assert f["challenger"]["sigma"] == pytest.approx(float(ch.predict_sigma(art, p.X[j])[0]), rel=1e-9)


def test_live_forecast_uses_no_future_rows(frozen) -> None:
    ds, art = frozen
    days = sorted({r["date"] for v in ds.bars.values() for r in v})
    decision = days[-30]
    truncated = {s: [r for r in v if r["date"] <= decision] for s, v in ds.bars.items()}
    a = ch.forecast_day(art, ds.bars, decision, [], rc1_full=False)
    b = ch.forecast_day(art, truncated, decision, [], rc1_full=False)
    assert a["instruments"] == b["instruments"]
    for f in a["instruments"].values():
        assert f["challenger"]["ltv"] == pytest.approx(math.exp(f["challenger"]["q"]))
        assert f["challenger"]["q"] < 0 and f["rc1_standalone"]["q"] < 0


def test_pool_rule_counts_only_completed_outcomes() -> None:
    rng = np.random.default_rng(0)
    e = np.arange(1000, 1600).astype(float)
    u = rng.normal(0, 1, e.size)
    z = ch.z_from_pool(e, u, 1500)
    u2 = u.copy()
    u2[e > 1500] = -50.0
    assert ch.z_from_pool(e, u2, 1500) == z
    assert ch.z_from_pool(e[:100], u[:100], 1500) is None         # fewer than CAL_MIN outcomes


def test_realised_return(frozen) -> None:
    ds, _ = frozen
    rows = next(iter(ds.bars.values()))
    end, r = ch.realised(rows, rows[100]["date"])
    assert end == rows[110]["date"]
    assert r == pytest.approx(math.log(rows[110]["adj_close"] / rows[100]["adj_close"]))
    assert ch.realised(rows, rows[-5]["date"]) is None
