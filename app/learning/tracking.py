"""Experiment tracking: reproducible configuration, dataset provenance, metrics.

Lightweight and dependency-free (append-only JSONL), so experiments run the
same on a laptop, in CI, in a Cloud Run job or inside a federated Learning
Agent. A run records the code revision, a canonical hash of its configuration,
the hashes of its datasets, the RNG seed, its metrics and its drift checks.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np


def _canon(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _canon(v) for k, v in sorted(obj.items(), key=lambda kv: str(kv[0]))}
    if isinstance(obj, (list, tuple)):
        return [_canon(v) for v in obj]
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, float):
        return round(obj, 12)
    return obj


def config_hash(cfg: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(_canon(cfg), sort_keys=True, default=str).encode()).hexdigest()[:16]


def dataset_hash(*arrays: Any) -> str:
    h = hashlib.sha256()
    for a in arrays:
        if isinstance(a, np.ndarray):
            h.update(str(a.dtype).encode() + str(a.shape).encode())
            h.update(np.ascontiguousarray(a).tobytes())
        else:
            h.update(json.dumps(_canon(a), sort_keys=True, default=str).encode())
    return h.hexdigest()[:16]


def code_revision() -> str:
    try:
        rev = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5,
                             cwd=Path(__file__).resolve().parents[2]).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], capture_output=True,
                               text=True, timeout=5, cwd=Path(__file__).resolve().parents[2]).stdout.strip()
        return (rev[:12] + ("+dirty" if dirty else "")) if rev else "unknown"
    except Exception:
        return "unknown"


def population_stability(expected: np.ndarray, actual: np.ndarray, bins: int = 10) -> float:
    """Population Stability Index of a feature or score (drift detection).
    < 0.1 stable, 0.1-0.25 moderate shift, > 0.25 significant shift."""
    expected, actual = np.asarray(expected, float), np.asarray(actual, float)
    edges = np.unique(np.quantile(expected, np.linspace(0, 1, bins + 1)))
    if edges.size < 3:
        return 0.0
    e = np.histogram(np.clip(expected, edges[0], edges[-1]), edges)[0] / expected.size
    a = np.histogram(np.clip(actual, edges[0], edges[-1]), edges)[0] / max(actual.size, 1)
    e, a = np.clip(e, 1e-6, None), np.clip(a, 1e-6, None)
    return float(np.sum((a - e) * np.log(a / e)))


class ExperimentTracker:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, *, experiment: str, config: dict[str, Any], datasets: dict[str, str], seed: int,
            metrics: dict[str, Any], data_through: str, notes: str = "",
            evidence_class: str = "synthetic") -> dict[str, Any]:
        """``evidence_class`` keeps evidence categories distinct:
        synthetic | public_historical | institutional_simulated | institutional_observed."""
        if evidence_class not in {"synthetic", "public_historical", "institutional_simulated",
                                  "institutional_observed"}:
            raise ValueError(evidence_class)
        from app.learning.governance import assert_research_window
        assert_research_window(data_through)
        run = {"run_id": uuid.uuid4().hex[:12], "at": datetime.now(UTC).isoformat(), "experiment": experiment,
               "code": code_revision(), "config": _canon(config), "config_hash": config_hash(config),
               "datasets": datasets, "seed": seed, "data_through": data_through,
               "evidence_class": evidence_class, "metrics": _canon(metrics), "notes": notes}
        with self.path.open("a") as f:
            f.write(json.dumps(run, sort_keys=True, default=str) + "\n")
        return run

    def runs(self, experiment: str | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        rs = [json.loads(x) for x in self.path.read_text().splitlines() if x.strip()]
        return [r for r in rs if experiment is None or r["experiment"] == experiment]
