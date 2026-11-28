"""Synthetic stress paths used by the extended lifecycle validation."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np

from research.methodology_lab.candidate_validation import STRESS, stress_paths


def _hist(seed: int) -> list[dict]:
    rng = np.random.default_rng(seed)
    d, p, out = date(2024, 1, 1), 10.0, []
    while len(out) < 300:
        if d.weekday() < 5:
            p *= float(np.exp(rng.normal(0, 0.02)))
            out.append({"bar_date": d, "close": p})
        d += timedelta(days=1)
    return out


def test_overlays_share_the_base_path() -> None:
    hist = {"a": _hist(1), "b": _hist(2)}
    base, _ = stress_paths(hist, ["a", "b"], "base", 60, np.random.default_rng(5))
    for kind in STRESS[1:-1]:
        r, vm = stress_paths(hist, ["a", "b"], kind, 60, np.random.default_rng(5))
        diff = sum(int(np.sum(~np.isclose(np.nan_to_num(r[k], nan=99), base[k]))) for k in r)
        assert 0 < diff <= 60 * 2, kind
    r, _ = stress_paths(hist, ["a", "b"], "suspension", 60, np.random.default_rng(5))
    assert np.isnan(r["a"]).sum() == 10
    r, vm = stress_paths(hist, ["a", "b"], "liquidity_collapse", 60, np.random.default_rng(5))
    assert (vm["a"] == 0.1).sum() == 30


def test_frontier_v2_stress_kinds() -> None:
    hist = {"a": _hist(1), "b": _hist(2)}
    base, _ = stress_paths(hist, ["a", "b"], "base", 80, np.random.default_rng(5))
    r, vm = stress_paths(hist, ["a", "b"], "prolonged_suspension", 80, np.random.default_rng(5))
    assert np.isnan(r["a"]).sum() == 30 and not np.isnan(r["b"]).any()
    assert np.allclose(r["b"], base["b"])                     # only the suspended holding changes
    r, vm = stress_paths(hist, ["a", "b"], "execution_failure", 80, np.random.default_rng(5))
    assert (vm["a"] == 0.0).sum() == 20 and (vm["b"] == 0.0).sum() == 20
    assert (r["a"] < base["a"] - 1e-12).sum() == 5
