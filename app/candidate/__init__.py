"""CRI candidate risk and recovery engine (shadow only, not approved).

The candidate engine runs beside the operational engine in shadow mode. It
does not drive account state, emit margin events, send webhooks or produce
executable orders. Its methodology is documented in
``docs/methodology/CANDIDATE_ENGINE.md`` and requires Tiva's separate review
before any operational use.
"""

from app.candidate.config import CandidateConfig
from app.candidate.engine import CandidateEngine, CandidateEvaluation

__all__ = ["CandidateConfig", "CandidateEngine", "CandidateEvaluation"]
