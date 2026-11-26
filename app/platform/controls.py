"""Administrative controls and the hard safety interlocks.

Lending and execution-mode liquidation need BOTH the deployment permit
(environment variable set by Terraform, false in every environment today)
AND an enabled control row changed by an administrator with a recorded
reason. Neither can be turned on from the API alone.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from app.platform.audit import append_audit
from app.platform.db import current_connection
from app.platform.settings import PlatformSettings

DEFAULTS: dict[str, Any] = {
    "lending_enabled": False,
    "liquidation_execution_enabled": False,
    "monitoring_enabled": True,
    "ingestion_enabled": True,
    "webhooks_enabled": True,
    "maintenance_mode": False,
}

PERMIT_REQUIRED = {
    "lending_enabled": "lending_permitted",
    "liquidation_execution_enabled": "execution_permitted",
}


class ControlError(Exception):
    pass


def get_controls() -> dict[str, Any]:
    rows = current_connection().fetchall("SELECT control_key, value FROM platform_controls")
    values = dict(DEFAULTS)
    for r in rows:
        values[r["control_key"]] = json.loads(r["value"])
    return values


def effective(settings: PlatformSettings) -> dict[str, Any]:
    values = get_controls()
    for key, permit in PERMIT_REQUIRED.items():
        values[key] = bool(values[key]) and bool(getattr(settings, permit))
    return values


def set_control(settings: PlatformSettings, key: str, value: Any, reason: str, actor: str) -> dict[str, Any]:
    if key not in DEFAULTS:
        raise ControlError(f"unknown control {key}")
    if not reason or len(reason.strip()) < 10:
        raise ControlError("a reason of at least 10 characters is required")
    if key in PERMIT_REQUIRED and value and not getattr(settings, PERMIT_REQUIRED[key]):
        raise ControlError(
            f"{key} cannot be enabled: this deployment does not permit it "
            f"({PERMIT_REQUIRED[key]} is false; changing it requires an infrastructure change approved by Tiva)"
        )
    conn = current_connection()
    before = get_controls().get(key)
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO platform_controls (control_key, value, reason, updated_by, updated_at) VALUES (%s,%s,%s,%s,%s) "
        "ON CONFLICT (control_key) DO UPDATE SET value=excluded.value, reason=excluded.reason, "
        "updated_by=excluded.updated_by, updated_at=excluded.updated_at",
        (key, json.dumps(value), reason, actor, now),
    )
    append_audit("platform_control_changed", {"control": key, "before": before, "after": value, "reason": reason},
                 tenant_id="*", actor=actor)
    return get_controls()
