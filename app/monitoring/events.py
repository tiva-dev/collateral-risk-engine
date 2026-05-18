from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any

from fastapi.encoders import jsonable_encoder

from app.monitoring.models import MonitoringEvent


def monitoring_jsonable(obj: Any) -> Any:
    if is_dataclass(obj):
        return jsonable_encoder(asdict(obj))
    return jsonable_encoder(obj)


def serialize_event(event: MonitoringEvent) -> dict[str, Any]:
    return monitoring_jsonable(event)


def serialize_sse_event(event: MonitoringEvent) -> str:
    payload = json.dumps(serialize_event(event), default=str, sort_keys=True)
    return f"event: {event.event_type.value}\nid: {event.event_id}\ndata: {payload}\n\n"
