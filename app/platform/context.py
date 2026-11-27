"""Request principal: who is calling and for which tenant."""

from __future__ import annotations

import contextvars
from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    role: str  # client | readonly | admin | system
    key_id: str
    tenant_kind: str = "client"

    @property
    def actor(self) -> str:
        return f"{self.role}:{self.key_id}"


SYSTEM = Principal(tenant_id="*", role="system", key_id="system", tenant_kind="internal")

_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "cri_principal", default=None
)


def current_principal() -> Principal | None:
    return _principal.get()


def require_principal() -> Principal:
    p = _principal.get()
    if p is None:
        raise RuntimeError("no authenticated principal in context")
    return p


def set_principal(principal: Principal | None) -> contextvars.Token:
    return _principal.set(principal)


def reset_principal(token: contextvars.Token) -> None:
    _principal.reset(token)
