"""Federated prototype with simulated institutions (architectural validation).

    python -m app.federation.simulate [--seed 7] [--rounds 15]

Six simulated institutions, each with its own isolated data environment and
different portfolios, markets, credit policies (notice periods), loan sizes,
borrower behaviour and recovery outcomes. One is large (about 60% of all rows),
so the experiment also checks that a large participant does not overwhelm the
smaller ones.

Compared on every institution's own held-out data:
  local      logistic regression trained on that institution's data only
  central    pooled research benchmark (synthetic data only; no institution effects)
  federated  global model from secure, differentially private FedProx rounds
  personal   federated global model fine-tuned locally (never leaves the institution)
Plus federated statistics (slippage, recovery duration): the securely
aggregated sufficient statistics reproduce the pooled estimate exactly.

Evidence class: simulated institutional data. This validates the architecture
and measures relative behaviour under known heterogeneity; it does not show
that equivalent improvements will occur with real institutions.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np

from app.federation.agent import AgentRefusal, Consent, DataEnvironment, LearningAgent
from app.federation.algorithms import (
    logistic_fit,
    logistic_predict,
    metrics,
    standardise,
    suffstats,
)
from app.federation.coordinator import Coordinator, RoundError
from app.learning.governance import ModelRegistry

GLOBAL_BETA = np.array([-2.2, -0.35, 0.25, 0.18, -0.30, -0.55, 0.40])   # breach, notice, size, tenure, prior, stress, liq


@dataclass(frozen=True)
class Institution:
    name: str
    n: int
    intercept: float        # institution-specific cure propensity (client base)
    notice: tuple[int, int] # policy: notice sessions range
    size_mu: float          # log USD balance mean
    stress_share: float     # market (NGX more stressed periods)
    slip_shift: float       # execution quality
    settle: int


INSTITUTIONS = (
    Institution("retail_broker_US", 9000, 0.0, (1, 3), 10.5, 0.15, 0.00, 1),
    Institution("private_bank_US", 900, 0.9, (3, 7), 13.5, 0.15, -0.20, 1),
    Institution("fintech_US", 700, -0.7, (1, 2), 9.0, 0.15, 0.10, 1),
    Institution("broker_NGX", 1500, -0.3, (2, 5), 11.0, 0.35, 0.45, 2),
    Institution("bank_NGX", 400, 0.4, (3, 6), 12.5, 0.35, 0.30, 2),
    Institution("wealth_NGX", 180, 0.7, (5, 10), 13.0, 0.35, 0.25, 2),
)
K = 7


def generate(inst: Institution, rng: np.random.Generator, n: int) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    breach = rng.gamma(2.0, 0.05, n).clip(0, 0.5)
    notice = rng.integers(inst.notice[0], inst.notice[1] + 1, n).astype(float)
    size = rng.normal(inst.size_mu, 1.0, n)
    tenure = rng.gamma(2.0, 2.5, n).clip(0, 30)
    prior = rng.poisson(0.6, n).astype(float)
    stress = (rng.random(n) < inst.stress_share).astype(float)
    liq = rng.beta(4, 2, n)
    x = np.c_[breach, notice, size, tenure, prior, stress, liq]
    # standardised true effects; breach in tenths, size centred at 11
    lin = (inst.intercept + 0.8 - 6.0 * breach + 0.25 * (notice - 3) + 0.18 * (size - 11)
           + 0.04 * tenure - 0.45 * prior - 0.6 * stress + 1.0 * (liq - 0.65))
    cured = (rng.random(n) < 1 / (1 + np.exp(-lin))).astype(float)
    part = rng.normal(-3.0, 1.2, n)
    spread = rng.gamma(2.0, 8.0 + 20 * inst.stress_share, n)
    slip = inst.slip_shift - 5.5 + 0.5 * part + 0.02 * spread + 0.4 * stress + rng.normal(0, 0.5, n)
    dur = 0.3 + 0.45 * part.clip(-6, 2) + 0.35 * stress + 0.15 * inst.settle + rng.normal(0, 0.4, n)
    return {"margin_call_outcome": (x, cured),
            "liquidation_execution": (np.c_[part, spread, stress], slip),
            "recovery_episode": (np.c_[part, stress, np.full(n, float(inst.settle))], dur)}


def build(seed: int) -> tuple[dict[str, DataEnvironment], dict[str, dict[str, tuple[np.ndarray, np.ndarray]]]]:
    rng = np.random.default_rng(seed)
    envs, tests = {}, {}
    for inst in INSTITUTIONS:
        tr = generate(inst, rng, inst.n)
        ho = generate(inst, rng, max(400, inst.n // 3))
        envs[inst.name] = DataEnvironment(tr, ho)
        tests[inst.name] = ho
    return envs, tests


def run(seed: int = 7, rounds: int = 15, noise_multiplier: float = 0.8, eps_limit: float = 20.0) -> dict[str, Any]:
    envs, tests = build(seed)
    registry = ModelRegistry(Path(tempfile.mkdtemp()) / f"cri-federation-{seed}.jsonl")
    coord = Coordinator(registry, min_participants=3)
    consent = Consent(frozenset({"cure_logistic_v1", "slippage_ols_v1", "duration_ols_v1"}),
                      frozenset({"margin_call_outcome", "liquidation_execution", "recovery_episode"}))
    # each institution holds its own consent (pause / withdraw affect only itself)
    agents = {n: LearningAgent(n, e, replace(consent), coord.identity.public) for n, e in envs.items()}
    demo: dict[str, Any] = {}
    for n, a in agents.items():                       # onboarding: identities enrolled out of band
        coord.enroll(n, a.identity.public)
    # 1. automated registration (signed); a forged registration is refused
    demo["1_registration"] = {n: coord.register(a.registration()) for n, a in agents.items()}
    forged = agents["fintech_US"].registration()
    forged["payload"]["institution"] = "impostor"
    demo["1_forged_registration_accepted"] = coord.register(forged)
    from app.federation import crypto as _c
    hijacker = _c.Identity.generate()                 # fresh key claiming an enrolled institution
    hp = {**agents["bank_NGX"].registration()["payload"], "identity": hijacker.public.hex()}
    demo["1_hijack_registration_accepted"] = coord.register({"payload": hp, "signature": hijacker.sign(hp).hex()})
    # feature standardisation: a public, schema-level reference (no institution data)
    ref = np.r_[[0.1, 4.0, 11.0, 5.0, 0.6, 0.25, 0.65]]
    refsd = np.r_[[0.07, 2.0, 1.6, 3.5, 0.8, 0.43, 0.18]]
    params = {"epochs": 3, "lr": 0.2, "l2": 1e-3, "prox_mu": 0.1, "clip": 1.0,
              "noise_multiplier": noise_multiplier, "weight_cap": 1500}
    # 2-4. centrally scheduled rounds; local learning; secure aggregation; DP budget
    history = []
    for r in range(rounds):
        drop = {"fintech_US"} if r == 3 else set()           # 9. dropout mid-round
        try:
            res = coord.run_round(agents, "cure_logistic_v1", params, ref.tolist(), refsd.tolist(),
                                  drop_after_keys=drop, privacy_limit=eps_limit if noise_multiplier > 0 else None)
        except RoundError as e:                              # privacy budget exhausted: training stops
            if "privacy budget" not in str(e) or r == 0:
                raise                                        # nothing trained: not a demonstrable result
            demo["2_4_stopped"] = {"after_rounds": r, "reason": str(e), "epsilon_limit": eps_limit}
            break
        history.append({k: res.get(k) for k in ("round", "survivors", "dropped", "update_norm", "epsilon_spent")})
    demo["2_4_rounds"] = history
    # 7. validation/versioning in the registry (research stage only)
    recs = [r for r in registry.records().values() if r.model_id == "federation.cure_logistic_v1"]
    demo["7_registry"] = {"versions": len(recs), "stages": sorted({r.stage.value for r in recs}),
                          "origin": sorted({r.origin for r in recs}),
                          "lineage_depth": len(registry.lineage(recs[-1].key))}
    # 8. redistribution of a signed research release; personalisation stays local
    rel, sig = coord.release("cure_logistic_v1", ref.tolist(), refsd.tolist())
    demo["8_release_accepted"] = {n: a.accept_release(rel, sig) for n, a in agents.items()}
    tampered = {**rel, "model": [0.0] * len(rel["model"])}
    demo["8_tampered_release_accepted"] = agents["bank_NGX"].accept_release(tampered, sig)
    # 5-6. global improvement and personalisation vs local-only and central
    comparison = compare(envs, tests, np.array(rel["model"]), agents, ref, refsd)
    demo["5_6_comparison"] = comparison
    # federated statistics: exact equality with the pooled estimate
    demo["statistics"] = statistics(coord, agents, envs)
    # 9. withdrawal: the institution leaves; it is no longer scheduled
    agents["wealth_NGX"].consent.withdrawn = True
    coord.withdraw("wealth_NGX")
    after = coord.run_round(agents, "slippage_ols_v1", {"min_rows": 30}, [-3.0, 20.0, 0.25], [1.2, 15.0, 0.43])
    demo["9_withdrawal"] = {"participants_after": after["participants"]}
    # 10. unauthorised access and data transfer are refused
    demo["10_security"] = security(agents, coord, ref, refsd)
    return demo


def compare(envs: dict[str, DataEnvironment], tests: dict[str, Any], fed: np.ndarray,
            agents: dict[str, LearningAgent], ref: np.ndarray, refsd: np.ndarray) -> dict[str, Any]:
    sch = "margin_call_outcome"
    px = np.vstack([e._train[sch][0] for e in envs.values()])
    py = np.concatenate([e._train[sch][1] for e in envs.values()])
    central = logistic_fit(standardise(px, ref, refsd), py, np.zeros(K + 1), epochs=20, lr=0.2, l2=1e-3)
    out: dict[str, Any] = {}
    for name, env in envs.items():
        x, y = env._train[sch]
        local = logistic_fit(standardise(x, ref, refsd), y, np.zeros(K + 1), epochs=40, lr=0.2, l2=1e-2)
        hx, hy = tests[name][sch]
        hs = standardise(hx, ref, refsd)
        row = {"train_rows": int(y.size),
               "local": metrics(hy, logistic_predict(hs, local)),
               "central": metrics(hy, logistic_predict(hs, central)),
               "federated": metrics(hy, logistic_predict(hs, fed))}
        if "cure" in agents[name].personal:
            row["personal"] = metrics(hy, logistic_predict(hs, agents[name].personal["cure"]))
            row["personal_choice"] = agents[name].personal_choice
        out[name] = row
    for m in ("local", "central", "federated", "personal"):
        vals = [r[m]["log_loss"] for r in out.values() if m in r]
        out.setdefault("_mean_log_loss", {})[m] = float(np.mean(vals))
    return out


def statistics(coord: Coordinator, agents: dict[str, LearningAgent], envs: dict[str, DataEnvironment]) -> dict[str, Any]:
    from app.federation.algorithms import ols_from_suffstats
    out = {}
    for alg, sch, mean, sd in (("slippage_ols_v1", "liquidation_execution", [-3.0, 20.0, 0.25], [1.2, 15.0, 0.43]),
                               ("duration_ols_v1", "recovery_episode", [-3.0, 0.25, 1.5], [1.2, 0.43, 0.5])):
        res = coord.run_round(agents, alg, {"min_rows": 30}, mean, sd)
        pooled = np.sum([suffstats(standardise(e._train[sch][0], np.array(mean), np.array(sd)), e._train[sch][1])
                         for e in envs.values()], axis=0)
        exact = ols_from_suffstats(pooled, 3)["beta"]
        fed = np.array(coord.models[alg][-1]["model"])
        fed_se = ols_from_suffstats(pooled, 3)["se"][1]
        local_se = {n: float(ols_from_suffstats(suffstats(standardise(e._train[sch][0], np.array(mean), np.array(sd)),
                                                          e._train[sch][1]), 3)["se"][1]) for n, e in envs.items()}
        out[alg] = {"rows": res["rows"], "max_abs_diff_vs_pooled": float(np.max(np.abs(fed - exact))),
                    "beta": [round(float(b), 4) for b in fed], "residual_sd": res["sigma"],
                    "participation_slope_se": {"federated": float(fed_se), "local": local_se,
                                               "precision_gain_vs_local": {n: v / float(fed_se)
                                                                           for n, v in local_se.items()}}}
    return out


def security(agents: dict[str, LearningAgent], coord: Coordinator, ref: np.ndarray, refsd: np.ndarray) -> dict[str, Any]:
    from app.federation import crypto
    a = agents["bank_NGX"]
    out: dict[str, Any] = {}
    pkg = {"round": 999, "algorithm": "cure_logistic_v1", "params": {}, "global_model": [0.0] * (K + 1),
           "feature_mean": ref.tolist(), "feature_sd": refsd.tolist(), "planned": 3}
    rogue = crypto.Identity.generate()
    for label, p, signer in (("unsigned_package", pkg, rogue),
                             ("unlisted_algorithm", {**pkg, "algorithm": "export_rows"}, coord.identity),
                             ("remote_code", {**pkg, "code": "send(rows)"}, coord.identity),
                             ("unexpected_parameter", {**pkg, "params": {"dump": True}}, coord.identity)):
        try:
            a.advertise(p, signer.sign(p))
            out[label] = "ACCEPTED"
        except AgentRefusal as e:
            out[label] = f"refused: {e}"
    a.consent.paused = True
    try:
        a.advertise(pkg, coord.identity.sign(pkg))
        out["paused_agent"] = "ACCEPTED"
    except AgentRefusal as e:
        out["paused_agent"] = f"refused: {e}"
    a.consent.paused = False
    out["agent_exposes_row_export"] = any(hasattr(a, m) for m in ("export", "rows", "dump", "download"))
    out["messages_to_non_coordinator"] = sorted({e["sent_to"] for x in agents.values() for e in x.log} - {"coordinator"})
    out["max_contribution_elements"] = max(e["elements"] for x in agents.values() for e in x.log)
    try:
        coord.run_round({k: v for k, v in list(agents.items())[:2]}, "slippage_ols_v1", {"min_rows": 30},
                        [-3.0, 20.0, 0.25], [1.2, 15.0, 0.43])
        out["below_min_participation"] = "ACCEPTED"
    except RoundError as e:
        out["below_min_participation"] = f"refused: {e}"
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--rounds", type=int, default=15)
    ap.add_argument("--noise", type=float, default=0.8)
    a = ap.parse_args()
    res = run(a.seed, a.rounds, a.noise)
    print("CRI_FEDERATION_DEMO " + json.dumps(res, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o)))


if __name__ == "__main__":
    main()
