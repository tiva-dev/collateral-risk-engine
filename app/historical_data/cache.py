from __future__ import annotations

import hashlib, json, os
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from .config import load_config

SECRET_MARKERS = ("api_key", "secret", "authorization", "token", "password")


def _json_default(obj: Any) -> Any:
    if is_dataclass(obj):
        return asdict(obj)
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


def content_hash(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, default=_json_default).encode()
    return hashlib.sha256(blob).hexdigest()


def scrub_secrets(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {k: ("[REDACTED]" if any(m in k.lower() for m in SECRET_MARKERS) else scrub_secrets(v)) for k, v in payload.items()}
    if isinstance(payload, list):
        return [scrub_secrets(v) for v in payload]
    return payload


class HistoricalDataCache:
    def __init__(self, cache_dir: str | None = None):
        self.cache_dir = Path(cache_dir or load_config().cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def key_path(self, namespace: str, **parts: Any) -> Path:
        key = content_hash({"namespace": namespace, **parts})
        return self.cache_dir / namespace / f"{key}.json"

    def read_path(self, namespace: str, **parts: Any) -> Path:
        return self.key_path(namespace, **parts)

    def read(self, namespace: str, **parts: Any) -> dict[str, Any] | None:
        path = self.key_path(namespace, **parts)
        try:
            payload = json.loads(path.read_text())
            if payload.get("checksum") != content_hash(payload.get("data")):
                return None
            return payload
        except (OSError, json.JSONDecodeError, TypeError):
            return None

    def write(self, namespace: str, data: Any, **parts: Any) -> Path:
        path = self.key_path(namespace, **parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        safe = scrub_secrets(data)
        payload = {"checksum": content_hash(safe), "data": safe}
        path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=_json_default))
        return path
