"""Tiva-managed federation coordinator.

Registration and authentication of participants, round scheduling, signed job
packages, secure aggregation with dropout recovery, privacy-budget enforcement,
global model updates, evaluation from participants' local metrics, versioning
in the model registry (always at the research stage), signed releases, rollback
and an audit trail. Institutions never initiate rounds or choose algorithms.

Secure aggregation (double masking, after Bonawitz et al. 2017; honest-but-
curious coordinator): the coordinator receives only masked vectors and can
remove the masks only from the *sum* of surviving contributions. A dropped
agent's late contribution stays protected by its self-mask, whose shares are
never revealed for dropped agents. Tension with poisoning resistance: the
coordinator cannot inspect individual updates, so robustness relies on agent-
side clipping (signed code), capped weights, minimum participation, and
monitoring of the aggregate (norm and global holdout metrics) with rollback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np

from app.federation import crypto
from app.federation.agent import AgentRefusal, LearningAgent
from app.federation.algorithms import REGISTRY, ols_from_suffstats
from app.federation.privacy import PrivacyBudget
from app.learning.governance import ModelRecord, ModelRegistry


class RoundError(RuntimeError):
    pass


@dataclass
class Coordinator:
    registry: ModelRegistry
    min_participants: int = 3
    threshold_fraction: float = 0.6
    identity: crypto.Identity = field(default_factory=crypto.Identity.generate)
    participants: dict[str, dict[str, Any]] = field(default_factory=dict)
    budgets: dict[str, PrivacyBudget] = field(default_factory=dict)
    models: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    audit: list[dict[str, Any]] = field(default_factory=list)
    round_no: int = 0

    def _log(self, event: str, **kw: Any) -> None:
        self.audit.append({"at": datetime.now(UTC).isoformat(), "event": event, **kw})

    # ---------------------------------------------------- registration

    def register(self, msg: dict[str, Any]) -> bool:
        p = msg["payload"]
        ok = crypto.verify(bytes.fromhex(p["identity"]), p, bytes.fromhex(msg["signature"]))
        if ok:
            self.participants[p["institution"]] = {"identity": bytes.fromhex(p["identity"]), "status": "active",
                                                   "algorithms": set(p["algorithms"])}
        self._log("register", institution=p.get("institution"), accepted=ok)
        return ok

    def withdraw(self, institution: str) -> None:
        if institution in self.participants:
            self.participants[institution]["status"] = "withdrawn"
        self._log("withdraw", institution=institution)

    # ----------------------------------------------------------- rounds

    def run_round(self, agents: dict[str, LearningAgent], algorithm: str, params: dict[str, Any],
                  feature_mean: list[float], feature_sd: list[float], *, drop_after_keys: set[str] | None = None,
                  privacy_limit: float | None = None) -> dict[str, Any]:
        alg = REGISTRY[algorithm]
        eligible = sorted(i for i, p in self.participants.items()
                          if p["status"] == "active" and algorithm in p["algorithms"] and i in agents)
        if len(eligible) < self.min_participants:
            raise RoundError(f"only {len(eligible)} eligible participants; minimum is {self.min_participants}")
        z = float(params.get("noise_multiplier", 0.0))
        if alg.kind == "ml" and privacy_limit is not None:
            budget = self.budgets.setdefault(algorithm, PrivacyBudget(privacy_limit))
            if not budget.can_run(z):
                raise RoundError("privacy budget exhausted for this model")
        self.round_no += 1
        history = list(self.models.get(algorithm, []))          # copy: appended to below
        parent = history[-1]["version"] if history else None
        k = len(feature_mean)
        global_model = history[-1]["model"] if history else [0.0] * (k + 1)
        pkg = {"round": self.round_no, "algorithm": algorithm, "params": params, "global_model": global_model,
               "feature_mean": feature_mean, "feature_sd": feature_sd, "planned": len(eligible),
               "issued": datetime.now(UTC).isoformat()}
        sig = self.identity.sign(pkg)
        roster, refused = [], {}
        for i in eligible:
            try:
                roster.append(agents[i].advertise(pkg, sig))
            except AgentRefusal as e:
                refused[i] = str(e)
        u1 = sorted(m["payload"]["institution"] for m in roster)
        t = max(2, int(np.ceil(self.threshold_fraction * len(u1))))
        if len(u1) < self.min_participants:
            raise RoundError(f"only {len(u1)} agents accepted the round")
        ids = {i: self.participants[i]["identity"] for i in u1}
        outbox = {i: agents[i].share_keys(roster, ids, t) for i in u1}
        for sender, msgs in outbox.items():
            for recipient, blob in msgs.items():
                agents[recipient].receive_shares(sender, blob)
        drop = drop_after_keys or set()
        masked = {}
        for i in u1:
            if i in drop:
                continue
            m = agents[i].masked_contribution()
            if not crypto.verify(ids[i], m["payload"], bytes.fromhex(m["signature"])):
                raise RoundError(f"unsigned contribution from {i}")
            masked[i] = m["payload"]
        u2 = sorted(masked)
        if len(u2) < max(t, self.min_participants):
            raise RoundError(f"{len(u2)} survivors is below the threshold {t}")
        dropped = sorted(set(u1) - set(u2))
        reveals = [agents[i].unmask(u2, dropped) for i in u2]
        dim = alg.dimension(k) + (1 if alg.kind == "ml" else 0)
        total = np.zeros(dim, dtype=np.uint64)
        for i in u2:
            total = total + np.frombuffer(bytes.fromhex(masked[i]["masked"]), dtype=np.uint64)
        for i in u2:                                           # remove survivors' self-masks
            shares = {r["b_shares"][i][0]: r["b_shares"][i][1] for r in reveals if i in r["b_shares"]}
            if len(shares) < t:
                raise RoundError(f"not enough shares to unmask {i}")
            total = total - crypto.prg(crypto.int_to_seed(crypto.shamir_combine(dict(list(shares.items())[:t]))), dim)
        for r in reveals:                                       # cancel pairs with dropped agents
            j = r["institution"]
            for d, s in r["dropped_seeds"].items():
                pair_mask = crypto.prg(bytes.fromhex(s), dim)
                total = total + pair_mask if d < j else total - pair_mask
        agg = crypto.decode(total)
        result: dict[str, Any] = {"round": self.round_no, "algorithm": algorithm, "participants": u1,
                                  "survivors": u2, "dropped": dropped, "refused": refused, "threshold": t}
        if alg.kind == "ml":
            weight = agg[-1]
            new = np.array(global_model) + agg[:-1] / max(weight, 1e-9)
            result["update_norm"] = float(np.linalg.norm(agg[:-1] / max(weight, 1e-9)))
            if privacy_limit is not None:
                self.budgets[algorithm].charge(len(u1), len(u2), z)
                result["epsilon_spent"] = self.budgets[algorithm].spent()
            result["local_metrics"] = {i: masked[i]["metrics"].get("global_model_holdout") for i in u2}
        else:
            est = ols_from_suffstats(agg, k)
            new = est["beta"]
            result.update(sigma=est["sigma"], rows=est["n"])
        version = f"0.{self.round_no}.0"
        entry = {"version": version, "model": [float(x) for x in new], "round": self.round_no}
        self.models.setdefault(algorithm, []).append(entry)
        self.registry.register(ModelRecord(f"federation.{algorithm}", version, "federation", alg.schema,
                                           crypto.canonical(params).hex()[:16], [f"round-{self.round_no}"],
                                           "2026-11-25", origin="federated",
                                           parent=parent,
                                           metrics={k2: v for k2, v in result.items()
                                                    if k2 in ("update_norm", "epsilon_spent", "rows", "sigma")}))
        self._log("round", **{k2: v for k2, v in result.items() if k2 != "local_metrics"})
        result["version"] = version
        return result

    # -------------------------------------------------- distribution

    def release(self, algorithm: str, feature_mean: list[float], feature_sd: list[float]) -> tuple[dict[str, Any], bytes]:
        latest = self.models[algorithm][-1]
        payload = {"algorithm": algorithm, "version": latest["version"], "model": latest["model"],
                   "feature_mean": feature_mean, "feature_sd": feature_sd, "stage": "research"}
        self._log("release", algorithm=algorithm, version=latest["version"])
        return payload, self.identity.sign(payload)

    def rollback(self, algorithm: str) -> str:
        if len(self.models.get(algorithm, [])) < 2:
            raise RoundError("nothing to roll back to")
        bad = self.models[algorithm].pop()
        self._log("rollback", algorithm=algorithm, removed=bad["version"])
        return str(self.models[algorithm][-1]["version"])
