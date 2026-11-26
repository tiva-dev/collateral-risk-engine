"""Runtime settings read from the environment (Cloud Run injects these)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class PlatformSettings:
    environment: str = field(default_factory=lambda: os.environ.get("CRI_ENV", "local"))
    # "api" serves tenant traffic; "worker" serves internal endpoints only
    # (Cloud Scheduler, Cloud Tasks, Pub/Sub push), authenticated by Cloud Run IAM.
    role: str = field(default_factory=lambda: os.environ.get("CRI_SERVICE_ROLE", "api"))
    database_url: str = field(
        default_factory=lambda: os.environ.get("CRI_DATABASE_URL", "sqlite:///./data/cri.sqlite3")
    )
    api_key_pepper: str = field(default_factory=lambda: os.environ.get("CRI_API_KEY_PEPPER", ""))
    # Hard deployment-level safety switches. Both must be true in the
    # environment AND enabled in the platform_controls table before the
    # platform will accept lending or execution-mode liquidation instructions.
    lending_permitted: bool = field(default_factory=lambda: _flag("CRI_LENDING_PERMITTED"))
    execution_permitted: bool = field(default_factory=lambda: _flag("CRI_EXECUTION_PERMITTED"))
    gcp_project: str = field(default_factory=lambda: os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
    gcp_region: str = field(default_factory=lambda: os.environ.get("CRI_REGION", "europe-west1"))
    tasks_queue: str = field(default_factory=lambda: os.environ.get("CRI_WEBHOOK_QUEUE", ""))
    worker_url: str = field(default_factory=lambda: os.environ.get("CRI_WORKER_URL", ""))
    tasks_invoker_sa: str = field(default_factory=lambda: os.environ.get("CRI_TASKS_INVOKER_SA", ""))
    market_data_topic: str = field(default_factory=lambda: os.environ.get("CRI_MARKET_DATA_TOPIC", ""))
    bootstrap_admin_key_hash: str = field(
        default_factory=lambda: os.environ.get("CRI_BOOTSTRAP_ADMIN_KEY_SHA256", "")
    )
    webhook_max_attempts: int = field(
        default_factory=lambda: int(os.environ.get("CRI_WEBHOOK_MAX_ATTEMPTS", "12"))
    )
    webhook_allow_http: bool = field(default_factory=lambda: _flag("CRI_WEBHOOK_ALLOW_HTTP"))

    @property
    def is_cloud(self) -> bool:
        return self.environment in {"dev", "staging", "production"}


def load_settings() -> PlatformSettings:
    return PlatformSettings()
