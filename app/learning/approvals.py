"""Authenticated human authorisation for model approval and rollback (finding 6).

An approval is an Ed25519-signed token from a registered human approver:

    {"model_key": "intelligence.forward_vol@0.3.0", "decision": "approve" | "rollback",
     "evidence": ["report-17#3.2", ...], "approver": "risk.committee.chair",
     "issued_at": ISO-8601, "expires_at": ISO-8601}

The registry verifies the signature against the approver's registered public
key, the approver's role for that decision, the model key and the expiry. The
approver identity recorded in the registry is the verified one; a caller's
self-declared actor string carries no authority. Private keys stay with the
approvers (hardware keys or an HSM-backed signer in production, bound to the
institution's SSO identity); the platform holds only public keys.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

ROLES = {"approve": "model_approval", "rollback": "model_rollback"}


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


@dataclass
class ApproverKey:
    """An approver's signing key. Lives on the approver's device or signer, never on the platform."""

    key: Ed25519PrivateKey

    @classmethod
    def generate(cls) -> ApproverKey:
        return cls(Ed25519PrivateKey.generate())

    @property
    def public(self) -> bytes:
        return self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def sign(self, payload: dict[str, Any]) -> bytes:
        return self.key.sign(_canonical(payload))


def _verify(public: bytes, payload: dict[str, Any], signature: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, _canonical(payload))
        return True
    except Exception:  # noqa: BLE001 - any failure is a rejection
        return False


class ApprovalInvalid(PermissionError):
    pass


@dataclass
class ApproverRegistry:
    approvers: dict[str, dict[str, Any]] = field(default_factory=dict)   # id -> {"public": bytes, "roles": set}

    def add(self, approver_id: str, public: bytes, roles: set[str]) -> None:
        if not roles <= set(ROLES.values()):
            raise ValueError(f"unknown roles {roles - set(ROLES.values())}")
        self.approvers[approver_id] = {"public": public, "roles": set(roles)}

    def verify(self, token: dict[str, Any], signature: bytes, *, model_key: str, decision: str,
               now: datetime | None = None) -> str:
        """Return the verified approver id, or raise ApprovalInvalid."""
        now = now or datetime.now(UTC)
        who = token.get("approver")
        entry = self.approvers.get(str(who))
        if entry is None:
            raise ApprovalInvalid("approver is not registered")
        if not _verify(entry["public"], token, signature):
            raise ApprovalInvalid("approval signature does not verify")
        if ROLES[decision] not in entry["roles"]:
            raise ApprovalInvalid(f"approver lacks the {ROLES[decision]} role")
        if token.get("decision") != decision or token.get("model_key") != model_key:
            raise ApprovalInvalid("token is for a different decision or model version")
        if not token.get("evidence"):
            raise ApprovalInvalid("approval requires evidence references")
        try:
            exp = datetime.fromisoformat(token["expires_at"])
            iss = datetime.fromisoformat(token["issued_at"])
        except (KeyError, ValueError) as e:
            raise ApprovalInvalid("token lacks valid issue/expiry times") from e
        if not iss <= now <= exp or exp - iss > timedelta(days=7):
            raise ApprovalInvalid("token is expired, not yet valid, or valid for too long")
        return str(who)


def sign_token(identity: ApproverKey, approver: str, model_key: str, decision: str, evidence: list[str],
               valid_for: timedelta = timedelta(hours=1)) -> tuple[dict[str, Any], bytes]:
    """Approver-side helper (runs on the approver's device / signer, not the platform)."""
    now = datetime.now(UTC)
    token = {"model_key": model_key, "decision": decision, "evidence": evidence, "approver": approver,
             "issued_at": now.isoformat(), "expires_at": (now + valid_for).isoformat()}
    return token, identity.sign(token)
