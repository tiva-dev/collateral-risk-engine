"""Shared learning platform for CRI Intelligence, Optimizer and Federation.

Every learned parameter or model is a *candidate*: it is registered with its
dataset provenance and reproducible configuration, evaluated, run in shadow,
and only becomes usable for decisions after a recorded human approval. Nothing
here can modify the frozen RC1 financial definition (app/candidate) or read
its locked evaluation data.
"""

from app.learning.governance import (
    ApprovalRequired,
    LockedDataError,
    ModelRecord,
    ModelRegistry,
    Stage,
    assert_research_window,
)
from app.learning.tracking import ExperimentTracker, config_hash, dataset_hash

__all__ = [
    "ApprovalRequired", "ExperimentTracker", "LockedDataError", "ModelRecord", "ModelRegistry", "Stage",
    "assert_research_window", "config_hash", "dataset_hash",
]
