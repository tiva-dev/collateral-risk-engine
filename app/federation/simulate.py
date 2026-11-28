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
import math
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


def institutions(count: int = 6, heterogeneity: float = 1.0, seed: int = 0) -> tuple[Institution, ...]:
    """The six reference institutions, a subset, or more participants drawn around them;
    ``heterogeneity`` scales the spread of institution-specific cure propensities."""
    base = list(INSTITUTIONS)
    rng = np.random.default_rng(1000 + seed)
    out = []
    for j in range(count):
        b = base[j % len(base)]
        extra = j >= len(base)
        n = int(b.n * (rng.uniform(0.3, 1.0) if extra else 1.0))
        out.append(replace(b, name=b.name if not extra else f"{b.name}_{j}", n=max(120, n),
                           intercept=b.intercept * heterogeneity + (rng.normal(0, 0.3) if extra else 0.0)))
    return tuple(out)


def build(seed: int, insts: tuple[Institution, ...] = INSTITUTIONS
          ) -> tuple[dict[str, DataEnvironment], dict[str, dict[str, tuple[np.ndarray, np.ndarray]]]]:
    rng = np.random.default_rng(seed)
    envs, tests = {}, {}
    for inst in insts:
        tr = generate(inst, rng, inst.n)
        ho = generate(inst, rng, max(400, inst.n // 3))
        envs[inst.name] = DataEnvironment(tr, ho)
        tests[inst.name] = ho
    return envs, tests


def run(seed: int = 7, rounds: int = 15, noise_multiplier: float = 0.8, eps_limit: float = 20.0,
        weight_cap: int = 1500, colluders: int = 0, insts: tuple[Institution, ...] = INSTITUTIONS,
        demos: bool = True) -> dict[str, Any]:
    """With noise_multiplier > 0 every institution requires DP: its agent keeps a persistent
    ledger (epsilon_limit, delta 1e-6, assumed colluders) and the coordinator keeps its own."""
    envs, tests = build(seed, insts)
    tmp = Path(tempfile.mkdtemp())
    registry = ModelRegistry(tmp / f"cri-federation-{seed}.jsonl")
    dp = noise_multiplier > 0
    from app.federation.privacy import PrivacyLedger
    coord = Coordinator(registry, min_participants=3, max_colluders=colluders,
                        ledger=PrivacyLedger(eps_limit, path=tmp / "coordinator-ledger.jsonl") if dp else None)
    consent = Consent(frozenset({"cure_logistic_v1", "slippage_ols_v1", "duration_ols_v1"}),
                      frozenset({"margin_call_outcome", "liquidation_execution", "recovery_episode"}),
                      epsilon_limit=eps_limit if dp else None, max_colluders=colluders)
    # each institution holds its own consent (pause / withdraw affect only itself)
    agents = {n: LearningAgent(n, e, replace(consent, ledger_path=tmp / f"agent-{n}.jsonl" if dp else None),
                               coord.identity.public) for n, e in envs.items()}
    demo: dict[str, Any] = {}
    for n, a in agents.items():                       # onboarding: identities enrolled out of band
        coord.enroll(n, a.identity.public)
    # 1. automated registration (signed); a forged registration is refused
    demo["1_registration"] = {n: coord.register(a.registration()) for n, a in agents.items()}
    names = sorted(agents)
    forged = agents[names[-1]].registration()
    forged["payload"]["institution"] = "impostor"
    demo["1_forged_registration_accepted"] = coord.register(forged)
    from app.federation import crypto as _c
    hijacker = _c.Identity.generate()                 # fresh key claiming an enrolled institution
    hp = {**agents[names[0]].registration()["payload"], "identity": hijacker.public.hex()}
    demo["1_hijack_registration_accepted"] = coord.register({"payload": hp, "signature": hijacker.sign(hp).hex()})
    # feature standardisation: a public, schema-level reference (no institution data)
    ref = np.r_[[0.1, 4.0, 11.0, 5.0, 0.6, 0.25, 0.65]]
    refsd = np.r_[[0.07, 2.0, 1.6, 3.5, 0.8, 0.43, 0.18]]
    params = {"epochs": 3, "lr": 0.2, "l2": 1e-3, "prox_mu": 0.1, "clip": 1.0,
              "noise_multiplier": noise_multiplier, "weight_cap": weight_cap}
    # 2-4. centrally scheduled rounds; local learning; secure aggregation; DP budget
    history = []
    for r in range(rounds):
        drop = {names[2]} if r == 3 and len(names) > 3 else set()   # 9. dropout mid-round
        try:
            res = coord.run_round(agents, "cure_logistic_v1", params, ref.tolist(), refsd.tolist(),
                                  drop_after_keys=drop, privacy_limit=eps_limit if noise_multiplier > 0 else None)
        except RoundError as e:                              # privacy budget exhausted: training stops
            if not ("privacy budget" in str(e) or "below the threshold" in str(e)) or r == 0:
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
    demo["8_tampered_release_accepted"] = agents[min(agents)].accept_release(tampered, sig)
    if dp:
        demo["privacy"] = {"unit": "institution (ML); record (statistics)", "delta": 1e-6,
                           "assumed_colluders": colluders,
                           "coordinator_epsilon": coord.ledger.spent("cure_logistic_v1|institution")
                           if coord.ledger else None,
                           "agent_epsilon_worst_case": max(a.ledger.spent("cure_logistic_v1|institution")
                                                           for a in agents.values() if a.ledger)}
    # 5-6. global improvement and personalisation vs local-only and central
    comparison = compare(envs, tests, np.array(rel["model"]), agents, ref, refsd)
    demo["5_6_comparison"] = comparison
    if not demos:
        return demo
    # federated statistics: exact pooled estimate (secure aggregation only) or record-level DP
    demo["statistics"] = statistics(coord, agents, envs, noise_multiplier, eps_limit if dp else None)
    # 9. withdrawal: the institution leaves; it is no longer scheduled
    leaver = max(agents)
    agents[leaver].consent.withdrawn = True
    coord.withdraw(leaver)
    sp = {"min_rows": 30, **({"noise_multiplier": noise_multiplier} if dp else {})}
    after = coord.run_round(agents, "slippage_ols_v1", sp, [-3.0, 20.0, 0.25], [1.2, 15.0, 0.43],
                            privacy_limit=eps_limit if dp else None)
    demo["9_withdrawal"] = {"participants_after": after["participants"]}
    # 10. unauthorised access and data transfer are refused
    demo["10_security"] = security(agents, coord, ref, refsd)
    return demo


L2_GRID = (1e-4, 1e-3, 1e-2, 1e-1, 1.0)


def tuned_fit(x: np.ndarray, y: np.ndarray, epochs: int) -> tuple[np.ndarray, float]:
    """Logistic regression with L2 chosen by 5-fold cross-validation (a well-tuned baseline)."""
    rng = np.random.default_rng(4)
    folds = np.array_split(rng.permutation(y.size), 5)
    best = (math.inf, L2_GRID[0])
    for l2 in L2_GRID:
        ll = np.mean([metrics(y[va], logistic_predict(x[va], logistic_fit(
            x[np.concatenate([f for j, f in enumerate(folds) if j != k])],
            y[np.concatenate([f for j, f in enumerate(folds) if j != k])], np.zeros(x.shape[1] + 1),
            epochs=epochs, lr=0.2, l2=l2)))["log_loss"] for k, va in enumerate(folds)])
        best = min(best, (float(ll), l2))
    return logistic_fit(x, y, np.zeros(x.shape[1] + 1), epochs=epochs, lr=0.2, l2=best[1]), best[1]


def compare(envs: dict[str, DataEnvironment], tests: dict[str, Any], fed: np.ndarray,
            agents: dict[str, LearningAgent], ref: np.ndarray, refsd: np.ndarray) -> dict[str, Any]:
    sch = "margin_call_outcome"
    px = np.vstack([e._train[sch][0] for e in envs.values()])
    py = np.concatenate([e._train[sch][1] for e in envs.values()])
    central, central_l2 = tuned_fit(standardise(px, ref, refsd), py, 20)
    out: dict[str, Any] = {"_central_l2": central_l2}
    for name, env in envs.items():
        x, y = env._train[sch]
        local, local_l2 = tuned_fit(standardise(x, ref, refsd), y, 40)
        hx, hy = tests[name][sch]
        hs = standardise(hx, ref, refsd)
        row = {"train_rows": int(y.size), "local_l2": local_l2,
               "local": metrics(hy, logistic_predict(hs, local)),
               "central": metrics(hy, logistic_predict(hs, central)),
               "federated": metrics(hy, logistic_predict(hs, fed))}
        if "cure" in agents[name].personal:
            row["personal"] = metrics(hy, logistic_predict(hs, agents[name].personal["cure"]))
            row["personal_choice"] = agents[name].personal_choice
        out[name] = row
    for m in ("local", "central", "federated", "personal"):
        vals = [r[m]["log_loss"] for r in out.values() if isinstance(r, dict) and m in r]
        out.setdefault("_mean_log_loss", {})[m] = float(np.mean(vals))
    return out


def statistics(coord: Coordinator, agents: dict[str, LearningAgent], envs: dict[str, DataEnvironment],
               noise_multiplier: float = 0.0, eps_limit: float | None = None) -> dict[str, Any]:
    from app.federation.algorithms import ols_from_suffstats
    from app.federation.privacy import FEATURE_BOUND, OUTCOME_BOUND
    out = {}
    for alg, sch, mean, sd in (("slippage_ols_v1", "liquidation_execution", [-3.0, 20.0, 0.25], [1.2, 15.0, 0.43]),
                               ("duration_ols_v1", "recovery_episode", [-3.0, 0.25, 1.5], [1.2, 0.43, 0.5])):
        params = {"min_rows": 30, **({"noise_multiplier": noise_multiplier} if noise_multiplier > 0 else {})}
        res = coord.run_round(agents, alg, params, mean, sd, privacy_limit=eps_limit)
        pooled = np.sum([suffstats(np.clip(standardise(e._train[sch][0], np.array(mean), np.array(sd)),
                                           -FEATURE_BOUND, FEATURE_BOUND),
                                   np.clip(e._train[sch][1], -OUTCOME_BOUND, OUTCOME_BOUND))
                         for e in envs.values()], axis=0)
        exact = ols_from_suffstats(pooled, 3)["beta"]
        fed = np.array(coord.models[alg][-1]["model"])
        fed_se = ols_from_suffstats(pooled, 3)["se"][1]
        local_se = {n: float(ols_from_suffstats(suffstats(standardise(e._train[sch][0], np.array(mean), np.array(sd)),
                                                          e._train[sch][1]), 3)["se"][1]) for n, e in envs.items()}
        out[alg] = {"rows": res["rows"], "max_abs_diff_vs_pooled": float(np.max(np.abs(fed - exact))),
                    "privacy": "record-level DP" if noise_multiplier > 0 else "secure aggregation only (exact)",
                    "epsilon_spent": res.get("epsilon_spent"),
                    "beta": [round(float(b), 4) for b in fed], "residual_sd": res["sigma"],
                    "participation_slope_se": {"federated": float(fed_se), "local": local_se,
                                               "precision_gain_vs_local": {n: v / float(fed_se)
                                                                           for n, v in local_se.items()}}}
    return out


def security(agents: dict[str, LearningAgent], coord: Coordinator, ref: np.ndarray, refsd: np.ndarray) -> dict[str, Any]:
    from app.federation import crypto
    a = agents[min(agents)]
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


def adversarial(seed: int = 7) -> dict[str, Any]:
    """Malicious participant, inconsistent dropout declaration, collusion threshold, and
    budget persistence across a coordinator restart and replacement."""
    from app.federation import crypto
    from app.federation.privacy import PrivacyLedger
    envs, _ = build(seed)
    tmp = Path(tempfile.mkdtemp())
    out: dict[str, Any] = {}
    names = sorted(envs)

    def federation(coord_ledger: PrivacyLedger | None, eps: float | None,
                   reuse: dict[str, LearningAgent] | None = None) -> tuple[Coordinator, dict[str, LearningAgent]]:
        coord = Coordinator(ModelRegistry(tmp / f"reg-{len(list(tmp.iterdir()))}.jsonl"), min_participants=3,
                            ledger=coord_ledger)
        consent = Consent(frozenset({"cure_logistic_v1"}), frozenset({"margin_call_outcome"}), epsilon_limit=eps)
        ags = reuse or {n: LearningAgent(n, envs[n], replace(consent, ledger_path=tmp / f"agent-{n}.jsonl"
                                                             if eps else None), coord.identity.public)
                        for n in names}
        for n, a in ags.items():
            a.coordinator_public = coord.identity.public
            coord.enroll(n, a.identity.public)
            coord.register(a.registration())
        return coord, ags
    ref = [0.1, 4.0, 11.0, 5.0, 0.6, 0.25, 0.65]
    refsd = [0.07, 2.0, 1.6, 3.5, 0.8, 0.43, 0.18]
    p = {"epochs": 2, "lr": 0.2, "l2": 1e-3, "prox_mu": 0.1, "clip": 1.0, "noise_multiplier": 0.0, "weight_cap": 1}
    # 1. A malicious participant bypasses clipping and sends a 50x update: the aggregate bound rejects it.
    coord, ags = federation(None, None)
    honest = coord.run_round(ags, "cure_logistic_v1", p, ref, refsd)
    bad = ags[names[0]]
    orig = bad._compute

    def poisoned(pkg: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
        v, m = orig(pkg)
        return np.r_[v[:-1] * 50.0 + 5.0, v[-1]], m
    bad._compute = poisoned                                       # type: ignore[method-assign]
    try:
        coord.run_round(ags, "cure_logistic_v1", p, ref, refsd)
        out["malicious_update_rejected"] = False
    except RoundError as e:
        out["malicious_update_rejected"] = "rejected" in str(e)
    out["honest_update_norm"] = honest["update_norm"]
    bad._compute = orig                                           # type: ignore[method-assign]
    # 2. Inconsistent dropout declaration: an agent never reveals both roles for one peer.
    a = ags[names[1]]
    pkg = {"round": 900, "algorithm": "cure_logistic_v1", "params": p, "global_model": [0.0] * (K + 1),
           "feature_mean": ref, "feature_sd": refsd, "planned": len(names), "issued": "x"}
    sig = coord.identity.sign(pkg)
    roster = [ags[n].advertise(pkg, sig) for n in names]
    ids = {n: ags[n].identity.public for n in names}
    boxes = {n: ags[n].share_keys(roster, ids, 4) for n in names}
    for snd, msgs in boxes.items():
        for rcp, blob in msgs.items():
            ags[rcp].receive_shares(snd, blob)
    a.unmask(names, [])
    try:
        a.unmask([n for n in names if n != names[2]], [names[2]])
        out["inconsistent_dropout_refused"] = False
    except AgentRefusal:
        out["inconsistent_dropout_refused"] = True
    # 3. Collusion: fewer than t shares of a self-mask seed do not reconstruct it.
    secret = crypto.seed_to_int(crypto.secrets.token_bytes(16))
    shares = crypto.shamir_split(secret, 6, 4)
    out["threshold_shares_reconstruct"] = crypto.shamir_combine({x: shares[x] for x in (1, 2, 3, 4)}) == secret
    out["below_threshold_reconstructs"] = crypto.shamir_combine({x: shares[x] for x in (1, 2, 3)}) == secret
    # 4. Budget persistence: restart and replacement of the coordinator cannot reset budgets.
    pd = {**p, "noise_multiplier": 3.0}
    led = PrivacyLedger(4.0, path=tmp / "coord.jsonl")
    coord, ags = federation(led, 4.0)
    done = 0
    for _ in range(50):
        try:
            coord.run_round(ags, "cure_logistic_v1", pd, ref, refsd, privacy_limit=4.0)
            done += 1
        except RoundError:
            break
    restarted = Coordinator(ModelRegistry(tmp / "reg-restart.jsonl"), ledger=PrivacyLedger(4.0, path=tmp / "coord.jsonl"))
    out["rounds_before_budget_exhausted"] = done
    out["restarted_coordinator_epsilon"] = restarted.ledger.spent("cure_logistic_v1|institution")  # type: ignore[union-attr]
    fresh, ags2 = federation(None, 4.0, reuse=ags)                # replacement coordinator, no ledger
    try:
        fresh.run_round(ags2, "cure_logistic_v1", pd, ref, refsd, privacy_limit=100.0)
        out["replacement_coordinator_round_accepted"] = True
    except RoundError as e:
        out["replacement_coordinator_round_accepted"] = False
        out["replacement_refusal"] = str(e)[:120]
    out["agent_epsilon_after"] = max(x.ledger.spent("cure_logistic_v1|institution")  # type: ignore[union-attr]
                                     for x in ags.values())
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--rounds", type=int, default=15)
    ap.add_argument("--noise", type=float, default=0.8)
    a = ap.parse_args()
    res = run(a.seed, a.rounds, a.noise)
    res["adversarial"] = adversarial(a.seed)
    print("CRI_FEDERATION_DEMO " + json.dumps(res, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o)))


if __name__ == "__main__":
    main()
