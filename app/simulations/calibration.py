from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any


def _values(rows: list[dict[str, Any]], key: str) -> list[float]:
    return [float(row[key]) for row in rows if isinstance(row.get(key), (int, float))]


def _diagnostic(rows: list[dict[str, Any]], keys: list[str]) -> dict[str, Any]:
    for key in keys:
        values = _values(rows, key)
        if values:
            return {
                "status": "available",
                "metric": key,
                "value": sum(values) / len(values),
            }
    return {
        "status": "unavailable",
        "reason": f"none of {', '.join(keys)} are present",
    }


def generate_calibration_diagnostics(
    metrics: list[dict[str, Any]], output_dir: str | None = None
) -> dict[str, Any]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for metric in metrics:
        scenario = metric.get("base_scenario") or metric.get("scenario")
        regime = metric.get("comparison_regime") or "unknown"
        grouped[(str(scenario), str(regime))].append(metric)

    scenarios = {}
    over_conservatism: list[str] = []
    under_protection: list[str] = []
    for (name, regime), rows in grouped.items():
        breach_rates = _values(rows, "credit_limit_breach_rate")
        recovery_rates = _values(rows, "economic_recovery_shortfall_rate")
        worst_losses = _values(rows, "worst_economic_recovery_shortfall")
        margin_rates = _values(rows, "margin_call_frequency")
        liquidation_rates = _values(rows, "liquidation_frequency")
        diagnostics: dict[str, Any] = {
            "comparison_regime": regime,
            "average_approved_credit_by_scenario": _diagnostic(
                rows, ["average_approved_credit"]
            ),
            "average_lifecycle_safe_credit_limit_by_scenario": _diagnostic(
                rows, ["average_lifecycle_safe_credit_limit"]
            ),
            "credit_capacity_preserved_by_scenario": _diagnostic(
                rows, ["average_credit_capacity_preserved"]
            ),
            "credit_limit_breach_rate_by_scenario": (
                sum(breach_rates) / len(breach_rates)
                if breach_rates
                else {"status": "unavailable", "reason": "breach rates absent"}
            ),
            "economic_recovery_shortfall_rate_by_scenario": (
                sum(recovery_rates) / len(recovery_rates)
                if recovery_rates
                else {
                    "status": "unavailable",
                    "reason": "economic recovery rates absent",
                }
            ),
            "worst_economic_recovery_shortfall_by_scenario": (
                max(worst_losses)
                if worst_losses
                else {
                    "status": "unavailable",
                    "reason": "economic recovery shortfalls absent",
                }
            ),
            "margin_call_frequency_by_scenario": (
                sum(margin_rates) / len(margin_rates)
                if margin_rates
                else {
                    "status": "unavailable",
                    "reason": "margin frequencies absent",
                }
            ),
            "liquidation_frequency_by_scenario": (
                sum(liquidation_rates) / len(liquidation_rates)
                if liquidation_rates
                else {
                    "status": "unavailable",
                    "reason": "liquidation frequencies absent",
                }
            ),
        }
        notes = []
        if breach_rates and max(breach_rates) > 0:
            notes.append("credit-limit breaches remain in replay")
        if recovery_rates and max(recovery_rates) > 0:
            notes.append("economic recovery shortfalls remain in replay")
        if margin_rates and sum(margin_rates) / len(margin_rates) > 0.20:
            notes.append("margin transition frequency requires review")
        if any(
            isinstance(row.get("data_quality_haircut_impact"), (int, float))
            and row["data_quality_haircut_impact"] > 0
            for row in rows
        ):
            notes.append("data quality materially reduces capacity")
        diagnostics["over_conservatism_indicators"] = [
            note for note in notes if "frequency" in note or "capacity" in note
        ]
        diagnostics["under_protection_indicators"] = [
            note
            for note in notes
            if note not in diagnostics["over_conservatism_indicators"]
        ]
        diagnostics["suggested_calibration_review_areas"] = notes or [
            "no automatic parameter change supported"
        ]
        key = name if regime == "unknown" else f"{name}::{regime}"
        scenarios[key] = diagnostics
        over_conservatism.extend(diagnostics["over_conservatism_indicators"])
        under_protection.extend(diagnostics["under_protection_indicators"])

    payload = {
        "scenarios": scenarios,
        "over_conservatism_indicators": over_conservatism,
        "under_protection_indicators": under_protection,
        "suggested_calibration_review_areas": sorted(
            set(over_conservatism + under_protection)
        )
        or ["review evidence; do not auto-change model parameters"],
    }
    if output_dir:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "calibration_diagnostics.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str)
        )
        lines = [
            "# Calibration Diagnostics",
            "",
            "Diagnostics only; no model parameters were changed automatically.",
            "",
        ]
        for name, diagnostics in scenarios.items():
            lines.extend(
                [
                    f"## {name}",
                    (
                        "- Economic recovery shortfall rate: "
                        f"{diagnostics['economic_recovery_shortfall_rate_by_scenario']}"
                    ),
                    (
                        "- Review areas: "
                        + ", ".join(diagnostics["suggested_calibration_review_areas"])
                    ),
                    "",
                ]
            )
        (out / "calibration_diagnostics.md").write_text("\n".join(lines))
    return payload
