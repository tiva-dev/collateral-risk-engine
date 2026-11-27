"""Model registry with stage gates, champion/challenger and rollback.

Stages (one direction, except rollback/retire):

    research -> shadow -> candidate -> approved
                                  \\-> rejected
    approved -> retired (rollback restores the previous approved version)

* ``research`` and ``shadow`` outputs are advisory and never feed a credit
  decision. ``shadow`` runs alongside the champion and is only compared.
* ``approved`` requires a recorded human approval (approver id, reason,
  evidence references). Code paths, federated rounds and automated pipelines
  cannot approve: ``promote(..., Stage.APPROVED)`` raises without an approval.
* Federated results enter as ``research`` like every other learned artefact.

The registry is an append-only JSONL event log, so lineage and every
transition are auditable and the current state is a pure function of the log.
"""

from __future__ import annotations

import fcntl
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from app.platform.governance.data_lock import EXAMINED_THROUGH


class Stage(str, Enum):
    RESEARCH = "research"
    SHADOW = "shadow"
    CANDIDATE = "candidate"
    APPROVED = "approved"
    REJECTED = "rejected"
    RETIRED = "retired"


_ALLOWED = {
    Stage.RESEARCH: {Stage.SHADOW, Stage.REJECTED},
    Stage.SHADOW: {Stage.CANDIDATE, Stage.REJECTED, Stage.RESEARCH},
    Stage.CANDIDATE: {Stage.APPROVED, Stage.REJECTED, Stage.SHADOW},
    Stage.APPROVED: {Stage.RETIRED},
    Stage.REJECTED: set(),
    Stage.RETIRED: set(),
}


class ApprovalRequired(PermissionError):
    pass


class LockedDataError(PermissionError):
    pass


def assert_research_window(last_observation: date | str) -> None:
    """AI and federated research never consume RC1's locked evaluation period.

    Unlike backtests there is no authorisation path: the locked period is
    reserved for the one-time formal evaluation of the frozen candidate."""
    d = date.fromisoformat(str(last_observation)[:10])
    if d > EXAMINED_THROUGH:
        raise LockedDataError(f"research data must end on or before {EXAMINED_THROUGH} (got {d})")


@dataclass
class ModelRecord:
    model_id: str            # e.g. "intelligence.forward_vol"
    version: str             # e.g. "0.1.0+ab12cd34"
    component: str           # intelligence | optimizer | federation | analyst
    task: str
    config_hash: str
    dataset_hashes: list[str]
    data_through: str        # last observation date used (provenance)
    metrics: dict[str, Any] = field(default_factory=dict)
    parent: str | None = None          # lineage: previous version
    origin: str = "central"            # central | federated | personalised
    stage: Stage = Stage.RESEARCH
    approvals: list[dict[str, Any]] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.model_id}@{self.version}"


class ModelRegistry:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Serialise read-modify-write transitions across threads and processes."""
        with self._lock, (self.path.parent / (self.path.name + ".lock")).open("a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    # ------------------------------------------------------------- log

    def _append(self, event: dict[str, Any]) -> None:
        event = {"at": datetime.now(UTC).isoformat(), **event}
        with self._lock, self.path.open("a") as f:
            f.write(json.dumps(event, sort_keys=True, default=str) + "\n")

    def events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def records(self) -> dict[str, ModelRecord]:
        out: dict[str, ModelRecord] = {}
        for e in self.events():
            if e["event"] == "register":
                r = dict(e["record"])
                r["stage"] = Stage(r["stage"])
                out[e["key"]] = ModelRecord(**r)
            elif e["event"] == "transition":
                rec = out[e["key"]]
                rec.stage = Stage(e["to"])
                if e.get("approval"):
                    rec.approvals.append(e["approval"])
            elif e["event"] == "metrics":
                out[e["key"]].metrics.update(e["metrics"])
        return out

    # ------------------------------------------------------- operations

    def register(self, rec: ModelRecord) -> ModelRecord:
        with self._exclusive():
            return self._register(rec)

    def _register(self, rec: ModelRecord) -> ModelRecord:
        assert_research_window(rec.data_through)
        if rec.key in self.records():
            raise ValueError(f"{rec.key} already registered (versions are immutable)")
        if rec.stage is not Stage.RESEARCH:
            raise ApprovalRequired("new models always enter at the research stage")
        self._append({"event": "register", "key": rec.key, "record": {**asdict(rec), "stage": rec.stage.value}})
        return rec

    def add_metrics(self, key: str, metrics: dict[str, Any]) -> None:
        self._get(key)
        self._append({"event": "metrics", "key": key, "metrics": metrics})

    def promote(self, key: str, to: Stage, *, actor: str, reason: str,
                approval: dict[str, Any] | None = None) -> ModelRecord:
        with self._exclusive():
            return self._promote(key, to, actor=actor, reason=reason, approval=approval)

    def _promote(self, key: str, to: Stage, *, actor: str, reason: str,
                 approval: dict[str, Any] | None) -> ModelRecord:
        rec = self._get(key)
        if to not in _ALLOWED[rec.stage]:
            raise ValueError(f"{key}: {rec.stage.value} -> {to.value} is not an allowed transition")
        if to is Stage.APPROVED:
            if not approval or not approval.get("approver") or not approval.get("evidence"):
                raise ApprovalRequired("approval needs a named human approver and evidence references")
            if approval.get("automated"):
                raise ApprovalRequired("automated approvals are never accepted")
            if actor.startswith(("federation:", "pipeline:", "agent:", "analyst:")):
                raise ApprovalRequired(f"{actor} cannot approve models")
            # one approved version per model id: the previous champion is retired
            for other in self.records().values():
                if other.model_id == rec.model_id and other.stage is Stage.APPROVED:
                    self._append({"event": "transition", "key": other.key, "from": "approved", "to": "retired",
                                  "actor": actor, "reason": f"superseded by {key}"})
        self._append({"event": "transition", "key": key, "from": rec.stage.value, "to": to.value,
                      "actor": actor, "reason": reason, "approval": approval})
        return self._get(key)

    def rollback(self, model_id: str, *, actor: str, reason: str) -> ModelRecord | None:
        """Retire the approved version and restore its approved parent, if any."""
        with self._exclusive():
            return self._rollback(model_id, actor=actor, reason=reason)

    def _rollback(self, model_id: str, *, actor: str, reason: str) -> ModelRecord | None:
        recs = self.records()
        cur = next((r for r in recs.values() if r.model_id == model_id and r.stage is Stage.APPROVED), None)
        if cur is None:
            raise ValueError(f"no approved version of {model_id}")
        self._append({"event": "transition", "key": cur.key, "from": "approved", "to": "retired",
                      "actor": actor, "reason": f"rollback: {reason}"})
        parent = recs.get(f"{model_id}@{cur.parent}") if cur.parent else None
        if parent and parent.approvals:
            self._append({"event": "transition", "key": parent.key, "from": "retired", "to": "approved",
                          "actor": actor, "reason": f"restored by rollback of {cur.key}",
                          "approval": {**parent.approvals[-1], "restored": True}})
            return self._get(parent.key)
        return None

    def champion(self, model_id: str) -> ModelRecord | None:
        return next((r for r in self.records().values() if r.model_id == model_id and r.stage is Stage.APPROVED), None)

    def challengers(self, model_id: str) -> list[ModelRecord]:
        return [r for r in self.records().values()
                if r.model_id == model_id and r.stage in (Stage.SHADOW, Stage.CANDIDATE)]

    def lineage(self, key: str) -> list[str]:
        recs, out = self.records(), []
        cur = recs.get(key)
        while cur is not None:
            out.append(cur.key)
            cur = recs.get(f"{cur.model_id}@{cur.parent}") if cur.parent else None
        return out

    def _get(self, key: str) -> ModelRecord:
        recs = self.records()
        if key not in recs:
            raise KeyError(key)
        return recs[key]
