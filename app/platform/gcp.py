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


def _get(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    import httpx

    r = httpx.get(url, params=params, headers={"Authorization": f"Bearer {access_token()}"}, timeout=15.0)
    r.raise_for_status()
    return r.json()


def run_job(project: str, region: str, job: str, args: list[str]) -> dict[str, Any]:
    import httpx

    body = {"overrides": {"containerOverrides": [{"args": args}]}}
    r = httpx.post(f"https://run.googleapis.com/v2/projects/{project}/locations/{region}/jobs/{job}:run",
                   json=body, headers={"Authorization": f"Bearer {access_token()}"}, timeout=30.0)
    r.raise_for_status()
    return r.json()


def infrastructure(project: str, region: str) -> dict[str, Any]:
    """Read-only operational view from Cloud Run, Scheduler and Monitoring APIs."""
    out: dict[str, Any] = {}
    base = f"https://run.googleapis.com/v2/projects/{project}/locations/{region}"
    try:
        services = _get(f"{base}/services").get("services", [])
        out["services"] = [{
            "name": s["name"].rsplit("/", 1)[-1],
            "latest_ready_revision": (s.get("latestReadyRevision") or "").rsplit("/", 1)[-1],
            "traffic": [{"revision": (t.get("revision") or "latest").rsplit("/", 1)[-1], "percent": t.get("percent"),
                         "tag": t.get("tag")} for t in s.get("trafficStatuses", [])],
            "conditions": [{"type": c.get("type"), "state": c.get("state")} for c in s.get("conditions", [])][:4],
            "update_time": s.get("updateTime"),
        } for s in services]
    except Exception as exc:  # noqa: BLE001 - each section degrades independently
        out["services_error"] = str(exc)[:300]
    try:
        jobs = _get(f"{base}/jobs").get("jobs", [])
        out["jobs"] = [{"name": j["name"].rsplit("/", 1)[-1],
                        "latest_execution": (j.get("latestCreatedExecution") or {}).get("name", "").rsplit("/", 1)[-1],
                        "latest_completion": (j.get("latestCreatedExecution") or {}).get("completionTime"),
                        "latest_status": (j.get("latestCreatedExecution") or {}).get("completionStatus")}
                       for j in jobs]
    except Exception as exc:  # noqa: BLE001
        out["jobs_error"] = str(exc)[:300]
    try:
        sched = _get(f"https://cloudscheduler.googleapis.com/v1/projects/{project}/locations/{region}/jobs")
        out["schedules"] = [{"name": j["name"].rsplit("/", 1)[-1], "schedule": j.get("schedule"),
                             "state": j.get("state"), "last_attempt": j.get("lastAttemptTime"),
                             "status_code": (j.get("status") or {}).get("code", 0)} for j in sched.get("jobs", [])]
    except Exception as exc:  # noqa: BLE001
        out["schedules_error"] = str(exc)[:300]
    try:
        pol = _get(f"https://monitoring.googleapis.com/v3/projects/{project}/alertPolicies")
        out["alert_policies"] = [{"name": p.get("displayName"), "enabled": p.get("enabled", True)}
                                 for p in pol.get("alertPolicies", [])]
        alerts = _get(f"https://monitoring.googleapis.com/v3/projects/{project}/alerts",
                      {"filter": 'state = "OPEN"', "pageSize": 20})
        out["open_alerts"] = [{"policy": (a.get("policy") or {}).get("displayName"), "opened": a.get("openTime"),
                               "state": a.get("state")} for a in alerts.get("alerts", [])]
    except Exception as exc:  # noqa: BLE001
        out["monitoring_error"] = str(exc)[:300]
    return out
