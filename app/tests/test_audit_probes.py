"""Keep the audit probes runnable against the current engine."""

from __future__ import annotations

from research.audit_probes import probes


def test_audit_probes_run_and_return_finite_values() -> None:
    results = probes.run_all()
    assert len(results["ltv_sensitivity"]) == 5
    assert len(results["volatility_concentration_grid"]) == 10
    for row in results["volatility_concentration_grid"]:
        assert 0.0 <= row["approved_ltv"] <= 1.0
        assert 0.0 <= row["safe_ltv"] <= row["approved_ltv"] + 1e-9
    assert set(results["fail_closed_states"]) == {
        "missing_market_data",
        "data_quality_0.30",
        "halted",
    }
    exact = results["interest_tick_rounding"][0]["exact"]
    assert abs(results["interest_tick_rounding"][-1]["accrued"] - exact) <= 0.02
    assert results["collateral_funded_cure"]["state_before"] == "margin_call"
