"""Core primitives: canonical JSON, content hashes and Ed25519 identities."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)


class HumansOutError(Exception):
    """Raised for anything the caller did wrong or the network refused."""


def canonical(obj) -> bytes:
    """Deterministic JSON encoding used for hashing and signing."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def cid_for(data: bytes) -> str:
    """Content identifier of a blob: its SHA-256 hash."""
    return "sha256-" + sha256_hex(data)


class Identity:
    """An agent's key pair. The public key is the agent's identity in the network."""

    def __init__(self, private_key: Ed25519PrivateKey):
        self._sk = private_key
        self.public_hex = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()

    @classmethod
    def load_or_create(cls, path: Path) -> "Identity":
        path = Path(path)
        if path.exists():
            data = json.loads(path.read_text())
            return cls(Ed25519PrivateKey.from_private_bytes(bytes.fromhex(data["private_key"])))
        sk = Ed25519PrivateKey.generate()
        raw = sk.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()).hex()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"private_key": raw, "note": "Never share this file. It is the agent's identity."}, f)
        return cls(sk)

    def sign(self, data: bytes) -> str:
        return self._sk.sign(data).hex()


def verify_signature(public_hex: str, signature_hex: str, data: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex)).verify(bytes.fromhex(signature_hex), data)
        return True
    except (InvalidSignature, ValueError):
        return False
