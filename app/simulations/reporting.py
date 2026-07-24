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

    scenario_rows = "\n".join(
        "| {scenario} | {regime} | {stress} | {breach} | {loss} |".format(
            scenario=metric.get("base_scenario"),
            regime=metric.get("comparison_regime"),
            stress=metric.get("stress_name", "baseline"),
            breach=metric.get("worst_credit_limit_breach"),
            loss=metric.get("worst_economic_recovery_shortfall"),
        )
        for metric in metrics
    )
    write(
        "official_validation_report.md",
        "# Official Validation Report\n\n"
        "## Scenario outcomes\n\n"
        "| Scenario | Comparison regime | Stress | Worst credit-limit breach | "
        "Worst economic recovery shortfall |\n"
        "|---|---|---|---:|---:|\n"
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
        "path under the scenario interest policy.\n",
    )
    write(
        "simulation_assumptions.md",
        "# Simulation Assumptions\n\n"
        "Credit-limit breach and economic recovery shortfall are distinct. "
        "Stress overlays are recorded per result. Synthetic THIN observations, "
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
