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


def test_review_fixes_identity_noise_bounds_budget_and_auc(demo) -> None:
    from app.federation.agent import (
        AgentRefusal,
        Consent,
        DataEnvironment,
        LearningAgent,
    )
    from app.federation.algorithms import metrics
    from app.federation.coordinator import Coordinator, RoundError
    from app.learning.governance import ModelRegistry
    assert demo["1_hijack_registration_accepted"] is False
    # constant predictions -> AUC 0.5 with average ranks
    assert metrics(np.array([0, 1, 0, 1.0]), np.full(4, 0.5))["auc"] == 0.5
    # out-of-range parameter values are refused before any work
    env = DataEnvironment({"margin_call_outcome": (np.zeros((40, 7)), np.zeros(40))},
                          {"margin_call_outcome": (np.zeros((40, 7)), np.zeros(40))})
    coord = Coordinator(ModelRegistry("unused.jsonl"))
    ag = LearningAgent("x", env, Consent(frozenset({"cure_logistic_v1"}), frozenset({"margin_call_outcome"})),
                       coord.identity.public)
    pkg = {"round": 1, "algorithm": "cure_logistic_v1", "params": {"epochs": 10_000}, "global_model": [0.0] * 8,
           "feature_mean": [0.0] * 7, "feature_sd": [1.0] * 7, "planned": 3}
    with pytest.raises(AgentRefusal):
        ag.advertise(pkg, coord.identity.sign(pkg))
    # privacy preflight reserves budget for the minimum survivor count
    from app.federation.privacy import PrivacyBudget
    b = PrivacyBudget(6.0)
    assert b.can_run(1.0) and not b.can_run(1.0 * (4 / 6) ** 0.5)
    with pytest.raises(RoundError):
        Coordinator(ModelRegistry("unused2.jsonl")).run_round({}, "cure_logistic_v1", {}, [0.0], [1.0])


def test_noise_is_not_reproducible_from_public_metadata() -> None:
    """Two agents with identical data and package must add different privacy noise."""
    from app.federation.agent import Consent, DataEnvironment, LearningAgent
    rng = np.random.default_rng(0)
    x, y = rng.normal(size=(200, 7)), (rng.random(200) < 0.5).astype(float)
    env = DataEnvironment({"margin_call_outcome": (x, y)}, {"margin_call_outcome": (x[:50], y[:50])})
    cons = Consent(frozenset({"cure_logistic_v1"}), frozenset({"margin_call_outcome"}))
    pkg = {"round": 1, "algorithm": "cure_logistic_v1", "global_model": [0.0] * 8, "feature_mean": [0.0] * 7,
           "feature_sd": [1.0] * 7, "planned": 3,
           "params": {"epochs": 1, "lr": 0.1, "l2": 0.0, "prox_mu": 0.0, "clip": 1.0, "noise_multiplier": 1.0,
                      "weight_cap": 100}}
    outs = []
    for _ in range(2):
        a = LearningAgent("same", env, cons, b"")
        a._round = {"t0": __import__("time").monotonic()}
        outs.append(a._compute(pkg)[0])
    assert not np.allclose(outs[0], outs[1])


def test_simulation_refuses_when_budget_permits_no_round() -> None:
    import pytest

    from app.federation.coordinator import RoundError
    from app.federation.simulate import run
    with pytest.raises(RoundError, match="privacy budget"):
        run(seed=7, rounds=2, noise_multiplier=1.0, eps_limit=0.0)
