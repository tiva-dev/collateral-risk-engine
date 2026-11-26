from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AuditLogger:
    """Append-only JSONL audit logger used by local and managed deployments.

    A production deployment can replace this with Postgres/S3/Kinesis while keeping
    the same payload contract.
    """

    def __init__(self, path: str | Path = "audit_log.jsonl") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, payload: dict[str, Any]) -> str:
        enriched = dict(payload)
        enriched["audit_created_at"] = datetime.now(UTC).isoformat()
        normalized = json.dumps(enriched, sort_keys=True, default=str)
        audit_id = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
        enriched["audit_id"] = audit_id
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(enriched, sort_keys=True, default=str) + "\n")
        return audit_id
