"""RC2 research: ex-ante calibration, out-of-fold residuals, SPA and the RC1
complete-framework comparison."""

from __future__ import annotations

import json
import math
import sys
from dataclasses import replace

import numpy as np
import pytest

from app.intelligence import exante as ex
from app.intelligence.benchmark import _splits, build_panel
from app.learning.datasets import load_research_dataset
from app.tests.test_intelligence import write_dataset


@pytest.fixture(scope="module")
def panel(tmp_path_factory):
    d = tmp_path_factory.mktemp("ex")
    ds = load_research_dataset(write_dataset(d / "us", n_sym=8, seed=4))
    p = build_panel([ds], step=10, rc1=True)
    return ds, p, _splits(p)


def test_calibration_uses_only_outcomes_known_at_the_decision_date(panel) -> None:
    _, p, _ = panel
    sigma = np.full(p.rv.size, 0.05)
    z = ex.exante_z(p, sigma, "US", n_min=50)
    t = int(np.median(np.unique(p.day)))
    future = p.end_day > t
    p2 = replace(p, ret=np.where(future, -10.0, p.ret))     # catastrophic outcomes not yet known at t
    z2 = ex.exante_z(p2, sigma, "US", n_min=50)
    at_t = p.day <= t
    assert np.isfinite(z[at_t]).any()
    np.testing.assert_array_equal(z[at_t], z2[at_t])
    assert np.nanmin(z2[~at_t]) < np.nanmin(z[~at_t])        # they do count once observed


def test_calibration_is_conservative_order_statistic(panel) -> None:
    _, p, _ = panel
    sigma = np.full(p.rv.size, 0.05)
    z = ex.exante_z(p, sigma, "US", n_min=50)
    last = p.day == p.day.max()
    known = (p.end_day <= p.day.max()) & (p.day >= p.day.max() - round(365.25 * ex.CAL_YEARS))
    u = np.sort(p.ret[known] / 0.05)
    assert z[last][0] <= np.quantile(u, ex.BREACH)            # at least as severe as the plain quantile
    assert z[last][0] in u


def test_training_forecasts_are_out_of_fold(panel) -> None:
    _, p, sp = panel
    f = ex.market_forecasts(p, sp, "US")
    yr = np.array([__import__("datetime").date.fromordinal(int(d)).year for d in p.day])
    fold = sp["train"] & (yr == 2020)
    assert fold.any()
    p2 = replace(p, rv=np.where(fold, p.rv * 5.0, p.rv))      # perturb the fold's own targets
    f2 = ex.market_forecasts(p2, sp, "US")
    np.testing.assert_allclose(f["ridge_ns"][fold], f2["ridge_ns"][fold])
    assert not np.allclose(f["ridge_ns"][~fold & sp["test"]], f2["ridge_ns"][~fold & sp["test"]])


def test_spa_detects_better_and_not_equal(panel) -> None:
    _, p, _ = panel
    rng = np.random.default_rng(0)
    day = p.day
    base = rng.gamma(2.0, 1.0, size=day.size)
    same = ex.spa_test(base, {"a": base + rng.normal(0, 0.01, day.size), "b": base.copy()}, day, reps=500)
    better = ex.spa_test(base, {"a": base - 0.5 + rng.normal(0, 0.3, day.size), "b": base.copy()}, day, reps=500)
    worse = ex.spa_test(base, {"a": base + 0.5 + rng.normal(0, 0.3, day.size)}, day, reps=500)
    assert same["p_value"] > 0.2
    assert better["p_value"] < 0.01 and better["best"] == "a"
    assert worse["p_value"] > 0.5


def test_block_ci_covers_mean(panel) -> None:
    _, p, _ = panel
    x = np.random.default_rng(1).normal(0.3, 1.0, p.day.size)
    lo, hi = ex.block_ci(x, p.day, reps=500)
    assert lo < x.mean() < hi


def test_rc1_complete_framework_ltv_is_a_probability(panel) -> None:
    ds, p, _ = panel
    sym = str(p.sym[-1]).split(":", 1)[1]
    rows = ds.bars[sym]
    appr, unc, ceil = ex.rc1_full_ltv_task((rows, len(rows) - 20, "US", sym, {}))
    assert 0.0 <= appr <= 1.0 and appr == pytest.approx(min(unc, ceil))


def test_end_to_end(tmp_path, monkeypatch) -> None:
    write_dataset(tmp_path / "us", n_sym=6, seed=8)
    write_dataset(tmp_path / "ngx", market="NGX", n_sym=6, seed=9, stale=0.4)
    out = tmp_path / "out"
    monkeypatch.setattr(sys, "argv", ["exante", "--dataset", f"{tmp_path / 'us'}+{tmp_path / 'ngx'}",
                                      "--out", str(out), "--step", "10", "--full-every", "25"])
    monkeypatch.setattr(ex, "rc1_full", lambda p, dss, mask, workers=4: np.where(mask[:, None], [0.6, 0.7, 0.6], np.nan))
    ex.main()
    rep = json.loads((out / "exante.json").read_text())
    assert rep["experiment"] == ex.EXPERIMENT
    for m in ("US", "NGX"):
        r = rep["results"]["test"][m]
        for k in ("rc1", "ridge_ns", "selector"):
            assert 0.0 <= r[k]["breach_rate"] <= 1.0 and 0.0 <= r[k]["mean_ltv"] <= 1.0
        assert 0.0 <= rep["spa"][m]["qlike"]["p_value"] <= 1.0
    assert rep["selector"]["tau"] in ex.TAU_GRID
    assert rep["results"]["test"]["rc1_full_subsample"]["US"]["rc1_full"]["mean_ltv"] == pytest.approx(0.6)


def test_held_out_instruments_never_calibrate(panel) -> None:
    _, p, sp = panel
    sigma = np.full(p.rv.size, 0.05)
    z = ex.exante_z(p, sigma, "US", n_min=50, pool_mask=~sp["unseen"])
    p2 = replace(p, ret=np.where(sp["unseen"], -10.0, p.ret))   # held-out outcomes change
    z2 = ex.exante_z(p2, sigma, "US", n_min=50, pool_mask=~sp["unseen"])
    np.testing.assert_array_equal(z, z2)
    assert np.isfinite(z[sp["unseen"]]).any()                     # but they receive a calibration


def test_ltv_is_exponential_of_log_threshold() -> None:
    np.testing.assert_allclose(ex.ltv_of(np.array([-0.2, 0.0, 0.1, -50.0])), [math.exp(-0.2), 1.0, 1.0, 0.0],
                               atol=1e-12)
