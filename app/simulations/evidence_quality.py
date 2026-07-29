from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.historical_data.cache import content_hash
from app.simulations.config.official_validation_universe import (
    FX_PAIRS,
    NGX_UNIVERSE,
    US_UNIVERSE,
)
from app.simulations.metrics import compute_simulation_metrics
from app.simulations.replay import COMMON_EXPOSURE, POLICY_ORIGINATION
from app.simulations.scenarios.official_portfolios import (
    official_portfolio_scenarios,
)

SUPPORTED_LOAN_CURRENCIES = {"USD", "NGN", "EUR"}
EXECUTION_BLOCKED_COUNTERFACTUALS = {
    "trading_halt",
    "stale_market_data",
    "missing_fx",
}


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return date.fromisoformat(value[:10])
    return None


def _manifest(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else getattr(value, "__dict__", {})


def _result(passed: bool = True) -> dict[str, Any]:
    return {
        "passed": passed,
        "warnings": [],
        "blocking_errors": [],
        "coverage_score": 1.0,
        "missing_symbols": [],
        "missing_fx_pairs": [],
        "earliest_available_dates": {},
        "methodology_notes": [],
        "missing_symbol_policy": {},
    }


def validate_provider_coverage(
    manifest: Any,
    required_symbols: list[str],
    required_fx_pairs: list[str],
) -> dict[str, Any]:
    data = _manifest(manifest)
    result = _result()
    earliest = data.get("earliest_available_date_by_symbol") or {}
    result["earliest_available_dates"] = earliest
    required = [symbol for symbol in required_symbols if symbol != "THIN"]
    result["missing_symbols"] = [
        symbol for symbol in required if symbol not in earliest
    ]
    result["missing_fx_pairs"] = [
        pair for pair in required_fx_pairs if pair not in earliest
    ]
    denominator = max(1, len(required) + len(required_fx_pairs))
    result["coverage_score"] = max(
        0.0,
        1.0
        - (len(result["missing_symbols"]) + len(result["missing_fx_pairs"]))
        / denominator,
    )
    for symbol in result["missing_symbols"]:
        classification = (
            "blocking" if symbol in US_UNIVERSE + NGX_UNIVERSE else "non_blocking"
        )
        result["missing_symbol_policy"][symbol] = classification
        target = (
            result["blocking_errors"]
            if classification == "blocking"
            else result["warnings"]
        )
        target.append(f"{classification} missing symbol: {symbol}")
    for pair in result["missing_fx_pairs"]:
        result["blocking_errors"].append(f"blocking missing FX pair: {pair}")
    result["methodology_notes"] = list(data.get("methodology_notes") or [])
    result["passed"] = not result["blocking_errors"]
    return result


def validate_minimum_history_length(
    manifest: Any, min_start_date: Any
) -> dict[str, Any]:
    data = _manifest(manifest)
    result = _result()
    minimum = _date(min_start_date)
    for symbol, value in (data.get("earliest_available_date_by_symbol") or {}).items():
        earliest = _date(value)
        if earliest and minimum and earliest > minimum:
            result["warnings"].append(
                f"{symbol} starts at {earliest}, after requested {minimum}"
            )
    result["earliest_available_dates"] = (
        data.get("earliest_available_date_by_symbol") or {}
    )
    return result


def validate_missing_symbol_policy(manifest: Any) -> dict[str, Any]:
    data = _manifest(manifest)
    result = _result()
    for symbol in data.get("missing_symbols") or []:
        classification = "synthetic_allowed" if symbol == "THIN" else "blocking"
        result["missing_symbol_policy"][symbol] = classification
        target = (
            result["warnings"]
            if classification != "blocking"
            else result["blocking_errors"]
        )
        target.append(f"{classification} missing symbol: {symbol}")
    result["missing_symbols"] = list(data.get("missing_symbols") or [])
    result["passed"] = not result["blocking_errors"]
    return result


def validate_fx_coverage(manifest: Any) -> dict[str, Any]:
    return validate_provider_coverage(
        manifest, [], list(_manifest(manifest).get("fx_pairs") or FX_PAIRS)
    )


def validate_cache_paths_exist(manifest: Any) -> dict[str, Any]:
    result = _result()
    for path_value in _manifest(manifest).get("cache_paths") or []:
        if not Path(path_value).exists():
            result["blocking_errors"].append(f"cache path missing: {path_value}")
    result["passed"] = not result["blocking_errors"]
    return result


def _load_json(mapping: dict[str, str], name: str, result: dict[str, Any]) -> Any:
    path = Path(mapping.get(name, name))
    if not path.exists():
        result["blocking_errors"].append(f"missing evidence file: {name}")
        return None
    if path.stat().st_size == 0:
        result["blocking_errors"].append(f"empty evidence file: {name}")
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        result["blocking_errors"].append(f"invalid JSON evidence file {name}: {exc}")
        return None


def validate_evidence_package(files: Any) -> dict[str, Any]:
    mapping = (
        files
        if isinstance(files, dict)
        else {Path(path).name: str(path) for path in files}
    )
    required = [
        "official_validation_manifest.json",
        "official_validation_records.json",
        "official_validation_metrics.json",
        "official_validation_metrics.csv",
        "official_validation_report.md",
        "provider_coverage_report.md",
        "data_methodology.md",
        "interest_accrual_methodology.md",
        "simulation_assumptions.md",
    ]
    result = _result()
    for name in required:
        path = Path(mapping.get(name, name))
        if not path.exists():
            result["blocking_errors"].append(f"missing evidence file: {name}")
        elif path.stat().st_size == 0:
            result["blocking_errors"].append(f"empty evidence file: {name}")

    evidence_manifest = _load_json(mapping, "official_validation_manifest.json", result)
    records = _load_json(mapping, "official_validation_records.json", result)
    metrics = _load_json(mapping, "official_validation_metrics.json", result)
    if not isinstance(evidence_manifest, dict):
        evidence_manifest = {}
    if not isinstance(records, list) or not records:
        result["blocking_errors"].append("no replay records present")
        records = []
    if not isinstance(metrics, list) or not metrics:
        result["blocking_errors"].append("no scenario metrics present")
        metrics = []

    if _provider_backed(evidence_manifest):
        for replay in records:
            if replay.get("stress_name", "baseline") != "baseline":
                continue
            replay_records = replay.get("records") or []
            label = (
                f"{replay.get('base_scenario') or replay.get('scenario')}/"
                f"{replay.get('comparison_regime')}"
            )
            if not replay_records:
                result["blocking_errors"].append(
                    f"provider-backed baseline has no replay records in {label}"
                )
                continue
            required_instruments = set(replay.get("required_instruments") or [])
            for record in replay_records:
                observed = set(
                    (record.get("data_quality") or {}).get("observations") or {}
                )
                missing_instruments = required_instruments - observed
                if missing_instruments:
                    result["blocking_errors"].append(
                        f"provider-backed baseline contains a partial portfolio "
                        f"on {record.get('date')} in {label}: missing "
                        f"{', '.join(sorted(missing_instruments))}"
                    )
                    break
            missing_fx_dates = replay.get("missing_fx_dates") or []
            if missing_fx_dates:
                result["blocking_errors"].append(
                    f"provider-backed baseline has missing FX observations in "
                    f"{label}: "
                    f"{', '.join(map(str, missing_fx_dates[:5]))}"
                )
            if any(record.get("fx_missing") for record in replay_records):
                result["blocking_errors"].append(
                    f"provider-backed baseline contains FX-missing replay records in "
                    f"{label}"
                )

    artifact_checksums = evidence_manifest.get("artifact_checksums", {})
    for name, expected in artifact_checksums.items():
        path = Path(mapping.get(name, name))
        if not path.exists():
            continue
        actual = (
            content_hash(json.loads(path.read_text()))
            if name.endswith(".json")
            else content_hash(path.read_text())
        )
        if actual != expected:
            result["blocking_errors"].append(f"artifact checksum mismatch: {name}")
    if records and content_hash(records) != evidence_manifest.get("results_checksum"):
        result["blocking_errors"].append("replay records checksum mismatch")
    if metrics and content_hash(metrics) != evidence_manifest.get("metrics_checksum"):
        result["blocking_errors"].append("metrics checksum mismatch")

    dataset_manifest = evidence_manifest.get("dataset_manifest") or {}
    recomputed = [
        compute_simulation_metrics(item, manifest=dataset_manifest) for item in records
    ]
    if metrics and content_hash(recomputed) != content_hash(metrics):
        result["blocking_errors"].append(
            "saved metrics do not recompute from saved replay records"
        )

    regimes: dict[tuple[str, str], set[str]] = {}
    for metric in metrics:
        for key in (
            "scenario",
            "comparison_regime",
            "stress_name",
            "fx_missing_events",
            "total_interest_accrued",
            "provider_coverage_by_symbol",
            "dynamic_engine_versus_static_ltv_outcome_table",
            "initial_approved_ltv",
            "initial_draw_ltv",
            "credit_limit_utilization_at_origination",
            "flat_30pct_economic_recovery_shortfall_rate",
            "flat_50pct_economic_recovery_shortfall_rate",
            "liquidation_episode_count",
            "forced_liquidation_episode_count",
            "forced_liquidation_full_recovery_rate",
            "realized_creditor_loss",
            "terminal_unresolved_exposure",
            "failed_liquidation_episode_count",
            "right_censored_liquidation_episode_count",
        ):
            if key not in metric or metric[key] in (None, ""):
                result["blocking_errors"].append(
                    f"missing metric {key} in {metric.get('scenario')}"
                )
        for key, value in metric.items():
            if isinstance(value, dict) and value.get("available") is False:
                if not value.get("reason"):
                    result["blocking_errors"].append(
                        f"unavailable metric {key} requires a reason"
                    )
                if value.get("blocking"):
                    result["blocking_errors"].append(
                        f"blocking metric unavailable: {key} in "
                        f"{metric.get('scenario')}"
                    )
        table = metric.get("dynamic_engine_versus_static_ltv_outcome_table")
        if not isinstance(table, list) or not table:
            result["blocking_errors"].append(
                f"empty policy comparison table in {metric.get('scenario')}"
            )
        elif not all(
            all(
                field in row
                for field in (
                    "dynamic_credit_limit_breach",
                    "flat_ltv_credit_limit_breach",
                    "flat_ltv_30_credit_limit_breach",
                    "flat_ltv_50_credit_limit_breach",
                    "static_haircut_credit_limit_breach",
                )
            )
            for row in table
        ):
            result["blocking_errors"].append(
                f"invalid policy comparison rows in {metric.get('scenario')}"
            )
        base = (
            metric.get("base_scenario")
            or str(metric.get("scenario", "")).split("::")[0]
        )
        stress = metric.get("stress_name", "baseline")
        if metric.get("comparison_regime") == POLICY_ORIGINATION:
            initial_limit = metric.get("initial_approved_credit_limit")
            utilization = metric.get("credit_limit_utilization_at_origination")
            if (
                isinstance(initial_limit, (int, float))
                and initial_limit > 0
                and (
                    not isinstance(utilization, (int, float))
                    or abs(float(utilization) - 1.0) > 1e-9
                )
            ):
                result["blocking_errors"].append(
                    f"{base}/{stress} did not draw 100% of the CRI-approved limit"
                )
            if float(metric.get("realized_creditor_loss", 0.0)) > 0:
                result["blocking_errors"].append(
                    f"{base}/{stress} produced realized creditor loss under CRI"
                )
            recovery_rate = metric.get("forced_liquidation_full_recovery_rate")
            forced_count = int(metric.get("forced_liquidation_episode_count", 0))
            if stress == "forced_liquidation_recovery" and forced_count < 1:
                result["blocking_errors"].append(
                    f"{base}/{stress} did not trigger forced liquidation"
                )
            if (
                isinstance(recovery_rate, (int, float))
                and float(recovery_rate) < 1.0
            ):
                target = (
                    result["warnings"]
                    if stress in EXECUTION_BLOCKED_COUNTERFACTUALS
                    else result["blocking_errors"]
                )
                target.append(
                    f"{base}/{stress} has an incomplete forced-liquidation recovery"
                )
            if int(metric.get("failed_liquidation_episode_count", 0)) > 0:
                target = (
                    result["warnings"]
                    if stress in EXECUTION_BLOCKED_COUNTERFACTUALS
                    else result["blocking_errors"]
                )
                target.append(f"{base}/{stress} has a failed liquidation episode")
            if int(
                metric.get("right_censored_liquidation_episode_count", 0)
            ) > 0:
                result["warnings"].append(
                    f"{base}/{stress} has a liquidation episode censored by replay end"
                )
        regimes.setdefault((str(base), str(stress)), set()).add(
            str(metric.get("comparison_regime"))
        )
    for (scenario, stress), observed in regimes.items():
        required_regimes = {COMMON_EXPOSURE, POLICY_ORIGINATION}
        if observed != required_regimes:
            result["blocking_errors"].append(
                f"{scenario}/{stress} missing comparison regimes: "
                f"{sorted(required_regimes - observed)}"
            )

    if _provider_backed(evidence_manifest):
        if evidence_manifest.get("synthetic_data_declared"):
            result["blocking_errors"].append(
                "provider-backed evidence declares synthetic data"
            )
        if not dataset_manifest.get("provider_coverage_summary"):
            result["blocking_errors"].append(
                "provider-backed evidence has no provider coverage"
            )
    result["passed"] = not result["blocking_errors"]
    result["coverage_score"] = 0.0 if result["blocking_errors"] else 1.0
    return result


def _provider_backed(evidence_manifest: dict[str, Any]) -> bool:
    provider = str(
        (evidence_manifest.get("dataset_manifest") or {}).get("provider") or ""
    )
    return bool(provider and provider not in {"synthetic", "dry_run"})


def required_fx_pairs_for_scenarios(
    scenario_names: list[str],
) -> list[str]:
    scenarios = official_portfolio_scenarios()
    pairs: set[str] = set()
    for name in scenario_names:
        scenario = scenarios[name]
        for currency in {
            holding.currency.upper()
            for holding in scenario.holdings
            if holding.currency.upper() != scenario.loan_currency.upper()
        }:
            pairs.add(f"{currency}/{scenario.loan_currency.upper()}")
    return sorted(pairs)


def scenario_eligibility(
    manifest: Any,
    scenario_names: list[str] | None = None,
    allow_synthetic: bool = False,
    stress: str = "all",
) -> dict[str, Any]:
    del stress
    data = _manifest(manifest)
    earliest = data.get("earliest_available_date_by_symbol") or {}
    scenarios = official_portfolio_scenarios()
    names = (
        list(scenarios)
        if not scenario_names or scenario_names == ["all"]
        else scenario_names
    )
    eligible: list[dict[str, Any]] = []
    ineligible: list[dict[str, Any]] = []
    for name in names:
        scenario = scenarios[name]
        reasons = []
        if scenario.loan_currency not in SUPPORTED_LOAN_CURRENCIES:
            reasons.append("unsupported loan currency")
        if getattr(scenario.loan_terms, "annual_interest_rate", -1) < 0:
            reasons.append("invalid interest terms")
        for holding in scenario.holdings:
            if holding.asset_id == "THIN" and allow_synthetic:
                continue
            if holding.asset_id not in earliest:
                reasons.append(f"missing bars for {holding.asset_id}")
        for pair in required_fx_pairs_for_scenarios([name]):
            source, target = pair.split("/")
            inverse = f"{target}/{source}"
            if pair not in earliest and inverse not in earliest:
                reasons.append(f"required FX coverage missing: {pair} or {inverse}")
        item = {
            "scenario": name,
            "reasons": reasons,
            "recommended_action": (
                "run validation"
                if not reasons
                else "refresh provider dataset or allow THIN sensitivity only"
            ),
        }
        (eligible if not reasons else ineligible).append(item)
    return {
        "eligible_scenarios": eligible,
        "ineligible_scenarios": ineligible,
    }
