from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.historical_data.cache import content_hash
from app.historical_data.config import load_config
from app.version import (
    LIFECYCLE_MODEL_VERSION,
    PROJECT_VERSION,
    RISK_MODEL_VERSION,
)

SIMULATION_CONFIG_VERSION = PROJECT_VERSION


def generate_evidence_package(
    results: list[dict[str, Any]],
    metrics: list[dict[str, Any]],
    output_dir: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, str]:
    out = Path(output_dir or load_config().simulation_output_dir)
    out.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}

    def write(name: str, text: str) -> Path:
        path = out / name
        path.write_text(text)
        files[name] = str(path)
        return path

    write(
        "official_validation_records.json",
        json.dumps(results, indent=2, sort_keys=True, default=str),
    )
    write(
        "official_validation_metrics.json",
        json.dumps(metrics, indent=2, sort_keys=True, default=str),
    )
    csv_path = out / "official_validation_metrics.csv"
    keys = sorted({key for metric in metrics for key in metric})
    with csv_path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=keys)
        writer.writeheader()
        writer.writerows(metrics)
    files[csv_path.name] = str(csv_path)

    result_windows = {
        (
            result.get("base_scenario") or result.get("scenario"),
            result.get("comparison_regime"),
            result.get("stress_name", "baseline"),
        ): (
            result.get("actual_common_start_date"),
            result.get("actual_common_end_date"),
        )
        for result in results
    }
    scenario_rows = "\n".join(
        "| {scenario} | {regime} | {stress} | {start} | {end} | {ltv} | "
        "{utilization} | {breach} | {paper_loss} | {realized_loss} | "
        "{recovery_rate} | {flat30} | {flat50} |".format(
            scenario=metric.get("base_scenario"),
            regime=metric.get("comparison_regime"),
            stress=metric.get("stress_name", "baseline"),
            start=result_windows.get(
                (
                    metric.get("base_scenario"),
                    metric.get("comparison_regime"),
                    metric.get("stress_name", "baseline"),
                ),
                (None, None),
            )[0],
            end=result_windows.get(
                (
                    metric.get("base_scenario"),
                    metric.get("comparison_regime"),
                    metric.get("stress_name", "baseline"),
                ),
                (None, None),
            )[1],
            breach=metric.get("worst_credit_limit_breach"),
            ltv=metric.get("initial_approved_ltv"),
            utilization=metric.get("credit_limit_utilization_at_origination"),
            paper_loss=metric.get("worst_economic_recovery_shortfall"),
            realized_loss=metric.get("realized_creditor_loss"),
            recovery_rate=metric.get("forced_liquidation_full_recovery_rate"),
            flat30=metric.get("flat_30pct_economic_recovery_shortfall_rate"),
            flat50=metric.get("flat_50pct_economic_recovery_shortfall_rate"),
        )
        for metric in metrics
    )
    write(
        "official_validation_report.md",
        "# Official Validation Report\n\n"
        "## Scenario outcomes\n\n"
        "| Scenario | Comparison regime | Stress | Common start | Common end | "
        "Initial CRI LTV | Limit utilization | Worst credit-limit breach | "
        "Worst theoretical shortfall | Realized creditor loss | Forced recovery "
        "rate | 30% flat shortfall rate | 50% flat shortfall rate |\n"
        "|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|\n"
        f"{scenario_rows}\n\n"
        "Common-exposure surveillance and policy-origination outcomes are "
        "reported separately. Completion alone is not evidence of superiority "
        "or calibration.\n",
    )
    write(
        "provider_coverage_report.md",
        "# Provider Coverage Report\n\n"
        "Requested and actual coverage, missing observations, adjustment "
        "methodology, provider call counts, and cache identities are embedded "
        "in the dataset manifest referenced by the evidence manifest.\n",
    )
    write(
        "data_methodology.md",
        "# Data Methodology\n\n"
        "Replay consumes checksum-verified canonical provider caches. Adjusted "
        "prices are used when available. Carry-forward is valuation-only and "
        "records observation age. Volatility and average volume use rolling "
        "histories. Synthetic order-book depth is excluded from official "
        "execution evidence.\n",
    )
    write(
        "interest_accrual_methodology.md",
        "# Interest Accrual Methodology\n\n"
        "Each comparison policy accrues its own principal, interest, and fee "
        "path under the scenario interest policy. NGN policies are validated at "
        "a 4% monthly nominal rate (48% annual simple); USD and EUR policies use "
        "10% annual simple interest. The CRI principal limit reserves interest "
        "through detection, cure, execution, and settlement latency.\n",
    )
    write(
        "simulation_assumptions.md",
        "# Simulation Assumptions\n\n"
        "Credit-limit breach and economic recovery shortfall are distinct. "
        "Policy-originated loans draw 100% of the CRI principal limit. Recovery "
        "advisories contain securities, quantities, and minimum executable limit "
        "prices. Historical execution uses current bids, volume participation "
        "caps, explicit costs, partial fills, and settlement delay; settled "
        "proceeds pay fees, interest, then principal. Passive 30% and 50% flat "
        "LTV benchmarks are persisted separately. Stress overlays are recorded "
        "per result. Synthetic THIN observations, "
        "when explicitly allowed, are a separately labelled sensitivity and "
        "cannot qualify a run as provider-backed.\n",
    )

    artifact_checksums = {
        name: content_hash(json.loads(Path(path).read_text()))
        if name.endswith(".json")
        else content_hash(Path(path).read_text())
        for name, path in files.items()
    }
    evidence_manifest = {
        "artifact": "official_validation_manifest",
        "simulation_config_version": SIMULATION_CONFIG_VERSION,
        "project_version": PROJECT_VERSION,
        "model_versions": {
            "risk": RISK_MODEL_VERSION,
            "lifecycle": LIFECYCLE_MODEL_VERSION,
        },
        "run_timestamp": datetime.now(UTC).isoformat(),
        "config": config or {},
        "dataset_manifest_identity": (config or {}).get("dataset_manifest_identity"),
        "dataset_manifest": (config or {}).get("dataset_manifest"),
        "result_count": len(results),
        "metric_count": len(metrics),
        "results_checksum": content_hash(results),
        "metrics_checksum": content_hash(metrics),
        "artifact_checksums": artifact_checksums,
        "synthetic_data_declared": any(
            result.get("synthetic_data_used") for result in results
        ),
    }
    manifest_path = out / "official_validation_manifest.json"
    manifest_path.write_text(
        json.dumps(evidence_manifest, indent=2, sort_keys=True, default=str)
    )
    files[manifest_path.name] = str(manifest_path)
    return files
