"""Minimal Google Cloud helpers using the Cloud Run metadata server (no SDK dependency)."""

from __future__ import annotations

import base64
import json
from typing import Any

_METADATA = "http://metadata.google.internal/computeMetadata/v1"


def access_token() -> str:
    import httpx

    r = httpx.get(f"{_METADATA}/instance/service-accounts/default/token",
                  headers={"Metadata-Flavor": "Google"}, timeout=5.0)
    r.raise_for_status()
    return str(r.json()["access_token"])


def publish(topic: str, message: dict[str, Any], attributes: dict[str, str] | None = None) -> str | None:
    import httpx

    if not topic:
        return None
    data = base64.b64encode(json.dumps(message, default=str).encode()).decode()
    r = httpx.post(f"https://pubsub.googleapis.com/v1/{topic}:publish",
                   json={"messages": [{"data": data, "attributes": attributes or {}}]},
                   headers={"Authorization": f"Bearer {access_token()}"}, timeout=10.0)
    r.raise_for_status()
    ids = r.json().get("messageIds") or [None]
    return ids[0]


def decode_push(envelope: dict[str, Any]) -> dict[str, Any]:
    msg = envelope.get("message") or {}
    raw = msg.get("data")
    return json.loads(base64.b64decode(raw)) if raw else {}
