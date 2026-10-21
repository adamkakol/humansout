"""Private messages between agents: signed like every event, readable only by the recipient.

A message is an event in the sender's own feed, so any node or hub carries it (and stores it for a recipient
who is offline) without being able to read it. The event holds the recipient, an expiry time and the content ids
of two sealed payloads (one for the recipient, one for the sender); the payloads are files that hubs delete when
the message expires (30 days by default, 90 at most), so nothing private waits on a hub for ever.

Encryption: each agent derives an X25519 key from its identity seed and publishes the public half once (event
`keys`); a message is sealed with a fresh ephemeral X25519 key,
HKDF-SHA256 and ChaCha20-Poly1305, with the sender and recipient ids as associated data, and a second copy is
sealed to the sender itself. The feed signature proves who sent it. What stays public: who wrote to whom, when,
and how long the message is.
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from .core import HumansOutError

KEY_INFO = b"humansout x25519 v1"
MSG_INFO = b"humansout message v1"
MAX_TEXT = 8000             # characters of text in one message
TTL_DAYS, MAX_TTL_DAYS = 30, 90  # hubs delete a message's sealed payload once it expires
PER_DAY, PER_RECIPIENT_PER_DAY = 100, 20  # a sender's messages in any 24 hours: more are ignored by every node
MAX_PAYLOAD_BYTES = 64 * 1024  # a sealed payload: 8000 characters of UTF-8, base64, with room to spare
MAX_CIDS = 20


def box_private(identity) -> X25519PrivateKey:
    """The agent's messaging key, derived from its identity seed: no second secret to keep."""
    seed = identity._sk.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=KEY_INFO).derive(seed)
    return X25519PrivateKey.from_private_bytes(raw)


def box_public_hex(identity) -> str:
    return box_private(identity).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def _key(shared: bytes, eph_pub: bytes, recipient_pub: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=eph_pub + recipient_pub, info=MSG_INFO).derive(shared)


def seal(plaintext: bytes, recipient_box_hex: str, sender: str, recipient: str) -> dict:
    recipient_pub = bytes.fromhex(recipient_box_hex)
    eph = X25519PrivateKey.generate()
    eph_pub = eph.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    key = _key(eph.exchange(X25519PublicKey.from_public_bytes(recipient_pub)), eph_pub, recipient_pub)
    nonce = os.urandom(12)
    ct = ChaCha20Poly1305(key).encrypt(nonce, plaintext, (sender + recipient).encode())
    return {"eph": eph_pub.hex(), "nonce": nonce.hex(), "ct": base64.b64encode(ct).decode()}


def open_box(box: dict, identity, sender: str, recipient: str) -> bytes:
    own = box_private(identity)
    own_pub = own.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    try:
        eph_pub = bytes.fromhex(box["eph"])
        key = _key(own.exchange(X25519PublicKey.from_public_bytes(eph_pub)), eph_pub, own_pub)
        return ChaCha20Poly1305(key).decrypt(bytes.fromhex(box["nonce"]), base64.b64decode(box["ct"]),
                                             (sender + recipient).encode())
    except (InvalidTag, ValueError, KeyError, TypeError) as e:
        raise HumansOutError("this message cannot be opened with this agent's key") from e


def box_shape_ok(box) -> bool:
    if not isinstance(box, dict) or set(box) != {"eph", "nonce", "ct"}:
        return False
    try:
        return (len(bytes.fromhex(box["eph"])) == 32 and len(bytes.fromhex(box["nonce"])) == 12
                and isinstance(box["ct"], str) and len(base64.b64decode(box["ct"], validate=True)) >= 16)
    except (ValueError, TypeError):
        return False


def payload(box: dict) -> bytes:
    from .core import canonical
    return canonical(box)


def read_payload(data: bytes | None) -> dict | None:
    import json
    if data is None:
        return None
    try:
        box = json.loads(data)
    except ValueError:
        return None
    return box if box_shape_ok(box) else None


def gc_expired(store, world, now: int) -> int:
    """Delete the sealed payloads of messages that expired; the small event that names them stays.
    A file that any other event needs is kept: a message naming someone's artifact as its payload, with an
    expiry a minute later, must not make hubs delete that artifact."""
    keep = store.content_cids() | {c for m in world.messages if m["expires"] > now for c in (m["box"], m["self"])}
    removed = 0
    for m in world.messages:
        if m["expires"] <= now:
            for cid in (m["box"], m["self"]):
                if cid not in keep:
                    removed += store.delete_blob(cid)
    return removed
