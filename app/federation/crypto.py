"""Cryptographic primitives for CRI Federation.

* Ed25519 identities: the coordinator signs job packages and model releases;
  each Learning Agent signs its contributions. Nothing unsigned is accepted.
* X25519 + HKDF: pairwise mask seeds between agents (secure aggregation).
* AES-GCM: Shamir shares are routed through the coordinator encrypted
  end-to-end between agents, so the coordinator never sees a share it may not use.
* Shamir secret sharing over the prime field GF(2^127 - 1).
* Masks: a deterministic PRG (SHA-256 counter mode) expands a 32-byte seed
  into uint64 words; masked vectors are summed modulo 2^64.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any

import numpy as np
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

PRIME = (1 << 127) - 1
SCALE = float(1 << 24)          # fixed-point scale for masked aggregation


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


# ------------------------------------------------------------- identities

@dataclass
class Identity:
    """Ed25519 signing identity (coordinator or agent)."""

    key: Ed25519PrivateKey

    @classmethod
    def generate(cls) -> Identity:
        return cls(Ed25519PrivateKey.generate())

    @property
    def public(self) -> bytes:
        return self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def sign(self, payload: dict[str, Any]) -> bytes:
        return self.key.sign(canonical(payload))


def verify(public: bytes, payload: dict[str, Any], signature: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(public).verify(signature, canonical(payload))
        return True
    except Exception:  # noqa: BLE001 - any failure is a rejection
        return False


# --------------------------------------------------------- key agreement

@dataclass
class ExchangeKey:
    key: X25519PrivateKey

    @classmethod
    def generate(cls) -> ExchangeKey:
        return cls(X25519PrivateKey.generate())

    @property
    def public(self) -> bytes:
        return self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def shared(self, peer_public: bytes, context: bytes) -> bytes:
        raw = self.key.exchange(X25519PublicKey.from_public_bytes(peer_public))
        return HKDF(hashes.SHA256(), 32, salt=None, info=b"cri-fed|" + context).derive(raw)


def encrypt(key: bytes, data: bytes, aad: bytes) -> bytes:
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(key).encrypt(nonce, data, aad)


def decrypt(key: bytes, blob: bytes, aad: bytes) -> bytes:
    return AESGCM(key).decrypt(blob[:12], blob[12:], aad)


# ----------------------------------------------------------------- masks

def prg(seed: bytes, n: int) -> np.ndarray:
    out = bytearray()
    ctr = 0
    while len(out) < 8 * n:
        out += hashlib.sha256(seed + ctr.to_bytes(8, "big")).digest()
        ctr += 1
    return np.frombuffer(bytes(out[: 8 * n]), dtype=np.uint64).copy()


def encode(x: np.ndarray) -> np.ndarray:
    return np.round(np.asarray(x, float) * SCALE).astype(np.int64).view(np.uint64)


def decode(u: np.ndarray) -> np.ndarray:
    return u.view(np.int64).astype(float) / SCALE


# --------------------------------------------------------------- Shamir

def shamir_split(secret: int, n: int, t: int) -> dict[int, int]:
    if not 0 <= secret < PRIME or not 1 <= t <= n:
        raise ValueError("invalid Shamir parameters")
    coeffs = [secret] + [secrets.randbelow(PRIME) for _ in range(t - 1)]
    return {x: sum(c * pow(x, k, PRIME) for k, c in enumerate(coeffs)) % PRIME for x in range(1, n + 1)}


def shamir_combine(shares: dict[int, int]) -> int:
    total = 0
    xs = list(shares)
    for i in xs:
        num, den = 1, 1
        for j in xs:
            if j != i:
                num = num * (-j) % PRIME
                den = den * (i - j) % PRIME
        total = (total + shares[i] * num * pow(den, PRIME - 2, PRIME)) % PRIME
    return total


def seed_to_int(seed: bytes) -> int:
    return int.from_bytes(seed[:15], "big")          # 120 bits < PRIME


def int_to_seed(v: int) -> bytes:
    return v.to_bytes(15, "big")
