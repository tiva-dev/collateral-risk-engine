"""Locked-period protection (docs/governance/LOCKED_PERIOD_PROTOCOL.md)."""

from app.platform.governance.data_lock import EXAMINED_THROUGH, LOCKED_FROM, cap_bars


def _bars() -> dict:
    return {"k": [{"bar_date": d} for d in ("2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27", "2026-12-01")]}


def test_backtests_never_see_locked_bars_by_default(monkeypatch) -> None:
    monkeypatch.delenv("CRI_LOCKED_EVALUATION_AUTHORISED", raising=False)
    got = [b["bar_date"] for b in cap_bars(_bars(), {})["k"]]
    assert got == ["2026-11-24", "2026-11-25"]
    assert EXAMINED_THROUGH.isoformat() == "2026-11-25" and LOCKED_FROM.isoformat() == "2026-11-27"


def test_param_alone_cannot_unlock(monkeypatch) -> None:
    monkeypatch.delenv("CRI_LOCKED_EVALUATION_AUTHORISED", raising=False)
    assert len(cap_bars(_bars(), {"locked_evaluation": True})["k"]) == 2


def test_formal_evaluation_needs_param_and_environment(monkeypatch) -> None:
    monkeypatch.setenv("CRI_LOCKED_EVALUATION_AUTHORISED", "1")
    assert len(cap_bars(_bars(), {})["k"]) == 2
    assert len(cap_bars(_bars(), {"locked_evaluation": True})["k"]) == 5


def test_exclusion_registry_covers_runs_that_reached_locked_data() -> None:
    import json
    from pathlib import Path

    spec = json.loads((Path(__file__).resolve().parents[2] / "app/platform/governance/excluded_runs.json").read_text())
    sels = [e["selector"] for e in spec["entries"] if e.get("selector")]
    assert {"requested_by": "*", "window_end_after": "2026-11-25"} in sels
