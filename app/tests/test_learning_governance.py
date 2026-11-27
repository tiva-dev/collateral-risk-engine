"""Shared learning platform: registry gates, lineage, rollback, tracking, data boundary."""

from __future__ import annotations

import numpy as np
import pytest

from app.learning import (
    ApprovalRequired,
    ExperimentTracker,
    LockedDataError,
    ModelRecord,
    ModelRegistry,
    Stage,
    assert_research_window,
    config_hash,
    dataset_hash,
)
from app.learning.tracking import population_stability

APPROVAL = {"approver": "risk.committee@tiva", "evidence": ["report-16#4.2"], "decision": "approved for shadow use"}


def rec(v: str, parent: str | None = None, through: str = "2026-11-20", origin: str = "central") -> ModelRecord:
    return ModelRecord("intelligence.forward_vol", v, "intelligence", "10d vol", "cfg", ["ds"], through,
                       parent=parent, origin=origin)


def test_locked_period_is_never_readable() -> None:
    assert_research_window("2026-11-25")
    for d in ("2026-11-26", "2026-11-27", "2027-01-05"):
        with pytest.raises(LockedDataError):
            assert_research_window(d)


def test_registry_gates_and_human_approval(tmp_path) -> None:
    reg = ModelRegistry(tmp_path / "r.jsonl")
    reg.register(rec("1"))
    with pytest.raises(LockedDataError):
        reg.register(rec("x", through="2026-11-27"))
    with pytest.raises(ValueError):
        reg.promote("intelligence.forward_vol@1", Stage.APPROVED, actor="me", reason="skip", approval=APPROVAL)
    reg.promote("intelligence.forward_vol@1", Stage.SHADOW, actor="pipeline:nightly", reason="benchmarks pass")
    reg.promote("intelligence.forward_vol@1", Stage.CANDIDATE, actor="pipeline:nightly", reason="shadow ok")
    for actor in ("federation:coordinator", "analyst:session-1", "pipeline:nightly"):
        with pytest.raises(ApprovalRequired):
            reg.promote("intelligence.forward_vol@1", Stage.APPROVED, actor=actor, reason="auto", approval=APPROVAL)
    with pytest.raises(ApprovalRequired):
        reg.promote("intelligence.forward_vol@1", Stage.APPROVED, actor="g.e", reason="x", approval={"approver": "g.e"})
    reg.promote("intelligence.forward_vol@1", Stage.APPROVED, actor="g.e", reason="committee", approval=APPROVAL)
    assert reg.champion("intelligence.forward_vol").version == "1"


def test_federated_results_enter_as_research_and_rollback_restores(tmp_path) -> None:
    reg = ModelRegistry(tmp_path / "r.jsonl")
    for v, parent in (("1", None), ("2", "1")):
        reg.register(rec(v, parent, origin="federated" if v == "2" else "central"))
        for st in (Stage.SHADOW, Stage.CANDIDATE):
            reg.promote(f"intelligence.forward_vol@{v}", st, actor="pipeline:x", reason="ok")
        reg.promote(f"intelligence.forward_vol@{v}", Stage.APPROVED, actor="g.e", reason="ok", approval=APPROVAL)
    assert reg.champion("intelligence.forward_vol").version == "2"
    assert reg.records()["intelligence.forward_vol@1"].stage is Stage.RETIRED
    assert reg.lineage("intelligence.forward_vol@2") == ["intelligence.forward_vol@2", "intelligence.forward_vol@1"]
    with pytest.raises(ApprovalRequired):
        bad = rec("3")
        bad.stage = Stage.APPROVED
        reg.register(bad)
    restored = reg.rollback("intelligence.forward_vol", actor="g.e", reason="drift")
    assert restored.version == "1" and reg.champion("intelligence.forward_vol").version == "1"


def test_tracking_is_reproducible_and_classifies_evidence(tmp_path) -> None:
    t = ExperimentTracker(tmp_path / "runs.jsonl")
    a = np.arange(10.0)
    assert config_hash({"b": 1, "a": 2.0}) == config_hash({"a": 2.0, "b": 1})
    assert dataset_hash(a) == dataset_hash(a.copy()) != dataset_hash(a + 1)
    run = t.log(experiment="vol", config={"k": 1}, datasets={"bars": dataset_hash(a)}, seed=7,
                metrics={"pinball": 0.1}, data_through="2026-11-25", evidence_class="public_historical")
    assert t.runs("vol")[0]["run_id"] == run["run_id"]
    with pytest.raises(ValueError):
        t.log(experiment="vol", config={}, datasets={}, seed=1, metrics={}, data_through="2026-11-25",
              evidence_class="mixed")
    with pytest.raises(LockedDataError):
        t.log(experiment="vol", config={}, datasets={}, seed=1, metrics={}, data_through="2026-11-30")


def test_drift_index_detects_shift() -> None:
    rng = np.random.default_rng(0)
    base = rng.normal(size=5000)
    assert population_stability(base, rng.normal(size=5000)) < 0.05
    assert population_stability(base, rng.normal(1.0, 1.0, size=5000)) > 0.25


def test_automated_approvals_are_rejected_for_any_actor(tmp_path) -> None:
    reg = ModelRegistry(tmp_path / "r.jsonl")
    reg.register(rec("1"))
    for st in (Stage.SHADOW, Stage.CANDIDATE):
        reg.promote("intelligence.forward_vol@1", st, actor="pipeline:x", reason="ok")
    with pytest.raises(ApprovalRequired):
        reg.promote("intelligence.forward_vol@1", Stage.APPROVED, actor="scheduler", reason="x",
                    approval={"approver": "alice", "evidence": ["x"], "automated": True})


def test_concurrent_promotions_leave_one_champion(tmp_path) -> None:
    import threading
    reg = ModelRegistry(tmp_path / "r.jsonl")
    for v in ("1", "2", "3"):
        reg.register(rec(v))
        for st in (Stage.SHADOW, Stage.CANDIDATE):
            reg.promote(f"intelligence.forward_vol@{v}", st, actor="pipeline:x", reason="ok")
    barrier = threading.Barrier(3)

    def go(v: str) -> None:
        barrier.wait()
        reg.promote(f"intelligence.forward_vol@{v}", Stage.APPROVED, actor="g.e", reason="ok", approval=APPROVAL)
    ts = [threading.Thread(target=go, args=(v,)) for v in ("1", "2", "3")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    approved = [r for r in reg.records().values() if r.stage is Stage.APPROVED]
    assert len(approved) == 1
