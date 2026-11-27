"""CRI v1.0 Release Candidate 1: the frozen configuration under evaluation.

Advisory and shadow-only. Not approved for lending, margin events, webhooks or
orders. Confidence levels, horizons and the utilisation share are research
defaults awaiting Tiva's risk-appetite decision.

RC1 = candidate engine (VERSION in config.py) with:
  * delayed suspension valuation (approved as a research default, 27 Nov);
  * regime-conditioned scenarios when two or more multivariate stress
    indicators deteriorate (current liquidity, 5-session move, correlation shift);
  * origination at 92.5% of the computed capacity (authorised band 0.85-1.0).
Kept at baseline (evidence in report 13): fixed 5-session headroom, no
concentration add-on, restoration on current volatility, stress-grace rule off,
no hysteresis or staged liquidation.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, replace
from typing import Any

from app.candidate.config import VERSION, CandidateConfig

RC_ID = "CRI-1.0.0-rc1"
ENGINE_VERSION = "candidate-2026.11.8"
RC_OVERRIDES: tuple[tuple[str, Any], ...] = (
    ("suspension_value", "delayed"),
    ("regime_conditioning", True),
    ("capacity_utilisation", 0.925),
)


def rc_config(**extra: Any) -> CandidateConfig:
    if VERSION != ENGINE_VERSION:
        raise RuntimeError(f"{RC_ID} is frozen on {ENGINE_VERSION}; this engine is {VERSION}")
    return replace(CandidateConfig(), **dict(RC_OVERRIDES), **extra)


def rc_fingerprint() -> str:
    """SHA-256 of the complete frozen parameter set (simulation size excluded)."""
    params = {k: v for k, v in asdict(rc_config()).items() if k != "n_paths"}
    blob = json.dumps({"rc": RC_ID, "engine": ENGINE_VERSION, "params": params}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()
