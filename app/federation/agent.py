"""CRI Learning Agent: runs inside an institution's approved data environment.

The institution's role is limited to approving the deployment, authorising data
scopes and algorithms (``Consent``) and pausing or withdrawing. Everything else
is automatic. Guarantees enforced here:

* raw records live in a private ``DataEnvironment``; the agent has no method
  that returns or transmits rows, only fixed-length algorithm outputs;
* only coordinator-signed packages are executed, and only allow-listed
  algorithms the institution authorised, with parameters from the algorithm's
  declared set (no remotely supplied code);
* resource limits (row cap, wall-clock budget) per job;
* outbound messages go only to the registered coordinator and are validated
  against the algorithm's contribution length before leaving;
* contributions are clipped and noised at the algorithm's privacy unit
  (institution for ML, loan record for statistics; ``privacy``), small cells
  are suppressed, then masked for secure aggregation and signed;
* no per-institution evaluation metric or other auxiliary value leaves the agent;
* the agent keeps its own persistent privacy ledger and refuses a round whose
  worst case would exceed the institution's consented budget;
* it reveals, per round, either a peer's self-mask share or the pairwise seed
  shared with that peer, never both (inconsistent dropout declarations).
"""

from __future__ import annotations

import secrets
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from app.federation import crypto
from app.federation.algorithms import (
    REGISTRY,
    logistic_fit,
    logistic_predict,
    metrics,
    standardise,
    suffstats,
)
from app.federation.privacy import (
    FEATURE_BOUND,
    OUTCOME_BOUND,
    PrivacyLedger,
    agent_noise,
    clip,
    effective_multiplier,
    ml_sensitivity,
    record_sensitivity,
)


class AgentRefusal(PermissionError):
    pass


# Value bounds for every allow-listed parameter (bounded work and sane privacy settings).
PARAM_BOUNDS: dict[str, tuple[float, float]] = {
    "epochs": (1, 20), "lr": (1e-4, 1.0), "l2": (0.0, 10.0), "prox_mu": (0.0, 100.0), "clip": (1e-3, 100.0),
    "noise_multiplier": (0.0, 20.0), "weight_cap": (1, 1_000_000), "min_rows": (10, 1_000_000)}


@dataclass
class DataEnvironment:
    """Read-only view over an institution's approved data. Rows never leave it:
    only the agent's installed algorithms can read them, in-process."""

    _train: dict[str, tuple[np.ndarray, np.ndarray]]
    _holdout: dict[str, tuple[np.ndarray, np.ndarray]]

    def schemas(self) -> set[str]:
        return set(self._train)

    def rows(self, schema: str) -> int:
        return int(self._train[schema][1].size) if schema in self._train else 0


@dataclass
class Consent:
    algorithms: frozenset[str]
    schemas: frozenset[str]
    max_rows: int = 1_000_000
    max_seconds: float = 120.0
    paused: bool = False
    withdrawn: bool = False
    # Differential privacy the institution requires (None: secure aggregation only).
    epsilon_limit: float | None = None
    delta: float = 1e-6
    max_colluders: int = 0                  # participants assumed able to collude with the coordinator
    ledger_path: Path | None = None         # persistent ledger in the institution's environment


@dataclass
class LearningAgent:
    institution: str
    env: DataEnvironment
    consent: Consent
    coordinator_public: bytes
    coordinator_endpoint: str = "coordinator"
    identity: crypto.Identity = field(default_factory=crypto.Identity.generate)
    _round: dict[str, Any] = field(default_factory=dict)
    personal: dict[str, np.ndarray] = field(default_factory=dict)
    accepted_releases: list[str] = field(default_factory=list)
    personal_choice: str = ""
    log: list[dict[str, Any]] = field(default_factory=list)
    ledger: PrivacyLedger | None = None

    def __post_init__(self) -> None:
        if self.consent.epsilon_limit is not None and self.ledger is None:
            self.ledger = PrivacyLedger(self.consent.epsilon_limit, self.consent.delta, self.consent.ledger_path)

    # ----------------------------------------------------- registration

    def registration(self) -> dict[str, Any]:
        payload = {"institution": self.institution, "identity": self.identity.public.hex(),
                   "algorithms": sorted(self.consent.algorithms), "schemas": sorted(self.consent.schemas)}
        return {"payload": payload, "signature": self.identity.sign(payload).hex()}

    # ---------------------------------------------------------- jobs

    def _check(self, package: dict[str, Any], signature: bytes) -> dict[str, Any]:
        if self.consent.withdrawn:
            raise AgentRefusal("participation withdrawn")
        if self.consent.paused:
            raise AgentRefusal("participation paused by the institution")
        if not crypto.verify(self.coordinator_public, package, signature):
            raise AgentRefusal("package signature invalid")
        alg = REGISTRY.get(package.get("algorithm", ""))
        if alg is None:
            raise AgentRefusal("algorithm not on the allow-list")
        if alg.id not in self.consent.algorithms or alg.schema not in self.consent.schemas:
            raise AgentRefusal("algorithm or data scope not authorised by the institution")
        if set(package.get("params", {})) - alg.params:
            raise AgentRefusal("unexpected parameters")
        for k, v in package.get("params", {}).items():
            if k in PARAM_BOUNDS:
                lo, hi = PARAM_BOUNDS[k]
                if not (isinstance(v, (int, float)) and lo <= v <= hi):
                    raise AgentRefusal(f"parameter {k}={v!r} outside the allowed range [{lo}, {hi}]")
        if "code" in package or "script" in package:
            raise AgentRefusal("remote code is never executed")
        if self.env.rows(alg.schema) > self.consent.max_rows:
            raise AgentRefusal("row limit exceeded")
        return package

    def advertise(self, package: dict[str, Any], signature: bytes) -> dict[str, Any]:
        """Step 1: accept a signed package and publish a round exchange key."""
        self._check(package, signature)
        if self.ledger is not None and float(package.get("params", {}).get("noise_multiplier", 0.0)) <= 0:
            raise AgentRefusal("the institution requires differential privacy; the package adds no noise")
        xk = crypto.ExchangeKey.generate()
        self._round = {"package": package, "xk": xk, "t0": time.monotonic()}
        payload = {"round": package["round"], "institution": self.institution, "exchange": xk.public.hex()}
        return {"payload": payload, "signature": self.identity.sign(payload).hex()}

    def share_keys(self, roster: list[dict[str, Any]], identities: dict[str, bytes], threshold: int) -> dict[str, bytes]:
        """Step 2: derive pairwise keys from the signed roster, Shamir-share a self-mask
        seed, and encrypt one share to each peer (routed via the coordinator)."""
        rnd = self._round
        peers = {}
        for m in roster:
            p = m["payload"]
            if not crypto.verify(identities[p["institution"]], p, bytes.fromhex(m["signature"])):
                raise AgentRefusal(f"roster entry for {p['institution']} is not signed by it")
            peers[p["institution"]] = bytes.fromhex(p["exchange"])
        order = sorted(peers)
        rnd.update(order=order, peers=peers, threshold=threshold)
        ctx = str(rnd["package"]["round"]).encode()
        rnd["enc"] = {j: rnd["xk"].shared(pk, b"enc|" + ctx) for j, pk in peers.items() if j != self.institution}
        rnd["seed"] = {j: rnd["xk"].shared(pk, b"mask|" + ctx) for j, pk in peers.items() if j != self.institution}
        b = crypto.int_to_seed(crypto.seed_to_int(crypto.secrets.token_bytes(16)))
        rnd["b"] = b
        shares = crypto.shamir_split(crypto.seed_to_int(b), len(order), threshold)
        out = {}
        for j in order:
            x = order.index(j) + 1
            if j == self.institution:
                rnd.setdefault("received", {})[self.institution] = (x, shares[x])
                continue
            out[j] = crypto.encrypt(rnd["enc"][j], f"{x}:{shares[x]}".encode(), ctx + self.institution.encode())
        return out

    def receive_shares(self, sender: str, blob: bytes) -> None:
        rnd = self._round
        ctx = str(rnd["package"]["round"]).encode()
        x, v = crypto.decrypt(rnd["enc"][sender], blob, ctx + sender.encode()).decode().split(":")
        rnd.setdefault("received", {})[sender] = (int(x), int(v))

    def masked_contribution(self) -> dict[str, Any]:
        """Step 3: train / estimate locally, protect, mask, sign."""
        rnd = self._round
        pkg = rnd["package"]
        alg = REGISTRY[pkg["algorithm"]]
        from app.federation.algorithms import TimeBudgetExceeded
        try:
            contribution, local_metrics = self._compute(pkg)
        except TimeBudgetExceeded as e:
            raise AgentRefusal(str(e)) from e
        if time.monotonic() - rnd["t0"] > self.consent.max_seconds:
            raise AgentRefusal("time budget exceeded")
        k = len(pkg["feature_mean"])
        if contribution.shape != (alg.dimension(k) + (1 if alg.kind == "ml" else 0),):
            raise AgentRefusal("contribution shape does not match the algorithm contract")
        if self.ledger is not None:
            # Worst case for this contribution: the round completes with only the threshold
            # number of survivors, of whom the assumed maximum collude.
            z = float(pkg["params"].get("noise_multiplier", 0.0))
            z_worst = effective_multiplier(z, pkg["planned"], rnd["threshold"], self.consent.max_colluders)
            scope = f"{alg.id}|{'institution' if alg.kind == 'ml' else 'record'}"
            try:
                self.ledger.charge(scope, z_worst, round=pkg["round"])
            except PermissionError as e:
                raise AgentRefusal(str(e)) from e
        y = crypto.encode(contribution) + crypto.prg(rnd["b"], contribution.size)
        for j, s in rnd["seed"].items():
            m = crypto.prg(s, contribution.size)
            y = y + m if self.institution < j else y - m
        del local_metrics                            # auxiliary values never leave the agent
        payload = {"round": pkg["round"], "institution": self.institution, "masked": y.tobytes().hex(),
                   "metrics": {}}
        self.log.append({"round": pkg["round"], "algorithm": alg.id, "sent_to": self.coordinator_endpoint,
                         "elements": int(contribution.size)})
        return {"payload": payload, "signature": self.identity.sign(payload).hex()}

    def unmask(self, survivors: list[str], dropped: list[str]) -> dict[str, Any]:
        """Step 4: reveal self-mask shares of survivors only, and the pairwise seeds
        shared with dropped peers (never the self-mask of a dropped peer)."""
        rnd = self._round
        recv = rnd.get("received", {})
        s_set, d_set = set(survivors), set(dropped)
        if s_set & d_set:
            raise AgentRefusal("a participant cannot be both a survivor and dropped")
        if self.institution not in s_set:
            raise AgentRefusal("this agent is not listed as a survivor")
        if len(s_set) < rnd.get("threshold", 0):
            raise AgentRefusal("fewer survivors than the round threshold")
        if (s_set | d_set) - set(rnd.get("order", [])):
            raise AgentRefusal("unknown participants in the dropout declaration")
        prior = rnd.get("declared")
        if prior is not None and prior != (tuple(sorted(s_set)), tuple(sorted(d_set))):
            raise AgentRefusal("inconsistent dropout declaration within one round")
        rnd["declared"] = (tuple(sorted(s_set)), tuple(sorted(d_set)))
        payload = {"institution": self.institution,
                   "b_shares": {i: recv[i] for i in survivors if i in recv},
                   "dropped_seeds": {d: rnd["seed"][d].hex() for d in dropped if d in rnd["seed"]}}
        return payload

    # ------------------------------------------------------ algorithms

    def _compute(self, pkg: dict[str, Any]) -> tuple[np.ndarray, dict[str, Any]]:
        alg = REGISTRY[pkg["algorithm"]]
        x, y = self.env._train[alg.schema]
        mu, sd = np.array(pkg["feature_mean"]), np.array(pkg["feature_sd"])
        xs = standardise(x, mu, sd)
        p = pkg["params"]
        if alg.kind == "statistics":
            # record-level privacy unit: bounded per-record influence, then distributed noise
            xc, yc = np.clip(xs, -FEATURE_BOUND, FEATURE_BOUND), np.clip(y, -OUTCOME_BOUND, OUTCOME_BOUND)
            s = suffstats(xc, yc)
            if y.size < p.get("min_rows", 30):
                s = np.zeros_like(s)                 # small-cell suppression
            z = float(p.get("noise_multiplier", 0.0))
            if z > 0:
                s = s + agent_noise(s.size, record_sensitivity(xs.shape[1]), z, pkg["planned"],
                                    np.random.default_rng(secrets.randbits(128)))
            return s, {"rows_used": int(y.size >= p.get("min_rows", 30))}
        w0 = np.array(pkg["global_model"])
        rng = np.random.default_rng(pkg["round"] * 1009 + zlib.crc32(self.institution.encode()) % 1000)
        deadline = self._round["t0"] + self.consent.max_seconds
        w = logistic_fit(xs, y, w0, epochs=p["epochs"], lr=p["lr"], l2=p["l2"], prox_mu=p["prox_mu"], rng=rng,
                         deadline=deadline)
        delta, clipped = clip(w - w0, p["clip"])
        weight = float(min(y.size, p["weight_cap"]))
        # Institution-level unit: [w * delta, w] has L2 norm <= weight_cap * sqrt(clip^2 + 1); every
        # agent adds the same noise scale to every coordinate, including the weight (the aggregate
        # denominator). Noise comes from private entropy, never from public round metadata.
        noise_rng = np.random.default_rng(secrets.randbits(128))
        v = np.r_[weight * delta, weight]
        noisy = v + agent_noise(v.size, ml_sensitivity(p["clip"], p["weight_cap"]), p["noise_multiplier"],
                                pkg["planned"], noise_rng)
        return noisy, {"clipped": clipped}

    # ------------------------------------------------- redistribution

    def accept_release(self, release: dict[str, Any], signature: bytes, *, personalise: bool = True) -> bool:
        """Verify a signed model release; optionally personalise it locally. The
        personalised model never leaves the institution."""
        if not crypto.verify(self.coordinator_public, release, signature):
            return False
        self.accepted_releases.append(release["version"])
        if personalise and release["algorithm"] == "cure_logistic_v1":
            self.personal["cure"], self.personal_choice = self._personalise(release)
        return True

    def _personalise(self, release: dict[str, Any]) -> tuple[np.ndarray, str]:
        """Choose, on a private validation split, how much to rely on the pooled model:
        the global model as released, local fine-tunes anchored to it with decreasing
        strength, or a local-only model. Institutions with little or atypical data keep
        more of the pooled knowledge; those with ample distinctive data rely on their own."""
        x, y = self.env._train["margin_call_outcome"]
        xs = standardise(x, np.array(release["feature_mean"]), np.array(release["feature_sd"]))
        w = np.array(release["model"])
        rng = np.random.default_rng(1)
        idx = rng.permutation(y.size)
        folds = np.array_split(idx, 5)            # 5-fold CV: stable choice for small institutions

        def fit(choice: str, rows: np.ndarray) -> np.ndarray:
            if choice == "global":
                return w
            if choice == "local_only":
                return logistic_fit(xs[rows], y[rows], np.zeros_like(w), epochs=40, lr=0.2, l2=1e-2,
                                    rng=np.random.default_rng(2))
            return logistic_fit(xs[rows], y[rows], w, epochs=20, lr=0.1, l2=1e-3,
                                prox_mu=float(choice.split(":")[1]), rng=np.random.default_rng(3))
        choices = ["global", "finetune:1.0", "finetune:0.1", "finetune:0.01", "local_only"]
        scores = {c: float(np.mean([metrics(y[va], logistic_predict(xs[va], fit(c, np.concatenate(
            [f for j, f in enumerate(folds) if j != k]))))["log_loss"] for k, va in enumerate(folds)]))
            for c in choices}
        best = min(scores, key=lambda c: scores[c])
        return fit(best, idx), best

    def evaluate_local(self, model: np.ndarray, mean: list[float], sd: list[float]) -> dict[str, float]:
        hx, hy = self.env._holdout["margin_call_outcome"]
        return metrics(hy, logistic_predict(standardise(hx, np.array(mean), np.array(sd)), model))
