"""Tiva-managed federation coordinator.

Registration and authentication of participants, round scheduling, signed job
packages, secure aggregation with dropout recovery, privacy-budget enforcement,
global model updates, evaluation from participants' local metrics, versioning
in the model registry (always at the research stage), signed releases, rollback
and an audit trail. Institutions never initiate rounds or choose algorithms.

Privacy (``privacy``): secure aggregation hides individual contributions; the
differential-privacy accountant bounds what the released aggregate reveals, at
the algorithm's privacy unit, charging only the honest survivors' noise
(``max_colluders``). The coordinator's ledger can be persisted, and every agent
keeps its own, so restarting or replacing the coordinator cannot reset a budget.
No per-institution metric is received. The aggregate is rejected if it exceeds
what clipped contributions plus noise can produce (oversized or malicious input).

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
from app.federation.privacy import PrivacyLedger, effective_multiplier, ml_sensitivity
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
    # Trust anchor: institution identities enrolled out of band at onboarding (contract,
    # key ceremony); registration must prove possession of exactly the enrolled key.
    enrolled: dict[str, bytes] = field(default_factory=dict)
    ledger: PrivacyLedger | None = None          # persistent when constructed with a path
    max_colluders: int = 0
    rejected_rounds: int = 0
    models: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    audit: list[dict[str, Any]] = field(default_factory=list)
    round_no: int = 0

    def _log(self, event: str, **kw: Any) -> None:
        self.audit.append({"at": datetime.now(UTC).isoformat(), "event": event, **kw})

    # ---------------------------------------------------- registration

    def enroll(self, institution: str, identity_public: bytes) -> None:
        """Onboarding step performed by Tiva with the institution (out of band)."""
        if institution in self.enrolled and self.enrolled[institution] != identity_public:
            raise ValueError("institution already enrolled with a different identity; re-keying needs revocation")
        self.enrolled[institution] = identity_public
        self._log("enroll", institution=institution)

    def register(self, msg: dict[str, Any]) -> bool:
        p = msg["payload"]
        claimed = bytes.fromhex(p["identity"])
        anchor = self.enrolled.get(p.get("institution", ""))
        current = self.participants.get(p.get("institution", ""), {}).get("identity")
        ok = (anchor is not None and claimed == anchor and (current is None or current == anchor)
              and crypto.verify(claimed, p, bytes.fromhex(msg["signature"])))
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
        scope = f"{algorithm}|{'institution' if alg.kind == 'ml' else 'record'}"
        if privacy_limit is not None:
            if self.ledger is None:
                self.ledger = PrivacyLedger(privacy_limit)
            if z <= 0:
                raise RoundError("a privacy budget is set but the round adds no noise")
            # reserve budget for the worst case the round can still complete with
            worst = max(t, self.min_participants)
            if not self.ledger.can_charge(scope, effective_multiplier(z, len(u1), worst, self.max_colluders)):
                raise RoundError("privacy budget would be exceeded if the round completes at its minimum size")
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
            try:
                m = agents[i].masked_contribution()
            except AgentRefusal as e:
                refused[i] = str(e)            # treated as a dropout; its masks are cancelled below
                continue
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
        if privacy_limit is not None:
            assert self.ledger is not None
            # charged even if the aggregate is rejected below: it has been released to the coordinator
            result["epsilon_spent"] = self.ledger.charge(scope, effective_multiplier(z, len(u1), len(u2),
                                                                                     self.max_colluders),
                                                         round=self.round_no)
        if alg.kind == "ml":
            uniform = float(params.get("weight_cap", 1)) <= 1
            den = float(len(u2)) if uniform else float(agg[-1])   # uniform: public denominator
            if den < 0.5:
                self.rejected_rounds += 1
                self._log("round_rejected", round=self.round_no, reason="aggregate weight too small after noise")
                raise RoundError("aggregate weight too small after noise; round rejected")
            step = agg[:-1] / den
            # Clipped contributions bound the weighted mean update by the clip norm; allow for noise.
            sens = ml_sensitivity(float(params["clip"]), float(params.get("weight_cap", 1)))
            noise_norm = z * sens * np.sqrt(step.size * len(u2) / max(len(u1), 1)) / den
            bound = float(params["clip"]) * 1.05 + 4.0 * noise_norm
            result["update_norm"] = float(np.linalg.norm(step))
            result["update_bound"] = bound
            if result["update_norm"] > bound:
                self.rejected_rounds += 1
                self._log("round_rejected", round=self.round_no, reason="aggregate exceeds the clipped-update bound",
                          norm=result["update_norm"], bound=bound)
                raise RoundError("aggregate update exceeds what clipped contributions can produce; round rejected")
            new = np.array(global_model) + step
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
