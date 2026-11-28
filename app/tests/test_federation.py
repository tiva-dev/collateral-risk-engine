"""CRI Federation prototype: the ten demonstration points and protocol properties."""

from __future__ import annotations

from itertools import pairwise

import numpy as np
import pytest

from app.federation import crypto
from app.federation.privacy import PrivacyBudget, epsilon
from app.federation.simulate import run


@pytest.fixture(scope="module")
def demo():
    return run(seed=7, rounds=5, noise_multiplier=1.0, eps_limit=40.0)


def test_registration_rounds_dropout_and_privacy(demo) -> None:
    assert all(demo["1_registration"].values()) and demo["1_forged_registration_accepted"] is False
    rounds = demo["2_4_rounds"]
    assert len(rounds) == 5
    drop = rounds[3]
    assert drop["dropped"] == ["fintech_US"] and "fintech_US" not in drop["survivors"]
    eps = [r["epsilon_spent"] for r in rounds]
    assert all(b > a for a, b in pairwise(eps)) and eps[-1] <= 40.0


def test_registry_release_personalisation_and_withdrawal(demo) -> None:
    reg = demo["7_registry"]
    assert reg["stages"] == ["research"] and reg["origin"] == ["federated"] and reg["lineage_depth"] == reg["versions"]
    assert all(demo["8_release_accepted"].values()) and demo["8_tampered_release_accepted"] is False
    comp = demo["5_6_comparison"]
    for name, row in comp.items():
        if name.startswith("_"):
            continue
        assert row["personal_choice"] in {"global", "finetune:1.0", "finetune:0.1", "finetune:0.01", "local_only"}
    assert "wealth_NGX" not in demo["9_withdrawal"]["participants_after"]
    assert len(demo["9_withdrawal"]["participants_after"]) == 5


def test_federated_statistics_equal_pooled_estimate(demo) -> None:
    for alg in ("slippage_ols_v1", "duration_ols_v1"):
        s = demo["statistics"][alg]
        assert s["max_abs_diff_vs_pooled"] < 1e-6
        gains = s["participation_slope_se"]["precision_gain_vs_local"]
        assert min(gains.values()) >= 1.0 and gains["wealth_NGX"] > gains["retail_broker_US"]


def test_security_refusals(demo) -> None:
    sec = demo["10_security"]
    for k in ("unsigned_package", "unlisted_algorithm", "remote_code", "unexpected_parameter", "paused_agent",
              "below_min_participation"):
        assert sec[k].startswith("refused"), (k, sec[k])
    assert sec["agent_exposes_row_export"] is False and sec["messages_to_non_coordinator"] == []
    assert sec["max_contribution_elements"] <= 16


def test_secure_aggregation_hides_inputs_and_recovers_from_dropout() -> None:
    """Direct protocol check: masked vectors reveal nothing useful individually; the
    unmasked sum equals the plain sum of survivors."""
    rng = np.random.default_rng(0)
    names = ["a", "b", "c", "d"]
    xs = {n: rng.normal(size=5) for n in names}
    keys = {n: crypto.ExchangeKey.generate() for n in names}
    seeds = {(i, j): keys[i].shared(keys[j].public, b"mask|1") for i in names for j in names if i != j}
    assert all(seeds[(i, j)] == seeds[(j, i)] for i in names for j in names if i != j)
    b = {n: crypto.int_to_seed(crypto.seed_to_int(crypto.secrets.token_bytes(16))) for n in names}
    shares = {n: crypto.shamir_split(crypto.seed_to_int(b[n]), 4, 3) for n in names}
    masked = {}
    for i in names:
        y = crypto.encode(xs[i]) + crypto.prg(b[i], 5)
        for j in names:
            if j != i:
                m = crypto.prg(seeds[(i, j)], 5)
                y = y + m if i < j else y - m
        masked[i] = y
    assert np.abs(crypto.decode(masked["a"]) - xs["a"]).max() > 1e3          # individual vector is masked
    survivors = ["a", "b", "c"]                     # "d" dropped after masking
    total = np.zeros(5, dtype=np.uint64)
    for i in survivors:
        total = total + masked[i]
        sub = {k: shares[i][k] for k in (1, 2, 3)}
        total = total - crypto.prg(crypto.int_to_seed(crypto.shamir_combine(sub)), 5)
    for j in survivors:
        m = crypto.prg(seeds[(j, "d")], 5)
        total = total + m if "d" < j else total - m
    assert np.allclose(crypto.decode(total), sum(xs[i] for i in survivors), atol=1e-5)


def test_privacy_budget_enforced() -> None:
    b = PrivacyBudget(10.0)
    assert epsilon(2.0, 1, 1e-6) < epsilon(2.0, 5, 1e-6) < epsilon(1.0, 5, 1e-6)
    rounds = 0
    while b.can_run(2.0) and rounds < 100:
        b.charge(6, 6, 2.0)
        rounds += 1
    assert 0 < rounds < 100 and b.spent() <= 10.0 and not b.can_run(2.0)
    b2 = PrivacyBudget(100.0)
    b2.charge(6, 3, 2.0)                  # fewer survivors: less noise, more privacy spent
    b3 = PrivacyBudget(100.0)
    b3.charge(6, 6, 2.0)
    assert b2.spent() > b3.spent()
    assert not PrivacyBudget(10.0).can_run(0.0)
