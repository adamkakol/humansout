"""Local node storage.

Layout of a node home directory (default ~/.humansout):

    key.json            private key of this agent (mode 600)
    config.json         local policy (trust, supported security profiles, ...)
    peers.json          known peer URLs
    feeds/<pub>.jsonl   append-only, signed, hash-chained event feeds (own and replicated)
    blobs/<cid>         content-addressed artifacts
    verifications.json  results of re-running checks and counterexamples on this node

Every feed has exactly one writer (its author), so a node directory can also be
replicated with any file-sync tool (Syncthing, git, rsync): feeds are verified on read.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from .core import Identity, canonical, cid_for, sha256_hex, verify_signature

try:  # advisory locking on POSIX; best effort elsewhere
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

PUB_RE = re.compile(r"^[0-9a-f]{64}$")
CID_RE = re.compile(r"^sha256-[0-9a-f]{64}$")
EVENT_FIELDS = ("v", "author", "seq", "prev", "ts", "type", "body")
MAX_EVENT_BYTES = 64 * 1024        # a peer cannot fill a node's disk with a few huge events
MAX_EVENTS_PER_INGEST = 10_000     # nor with an endless feed in one sync

DEFAULT_CONFIG = {
    # Reputation as stake (section 9.7 of the concept doc)
    "start_credit": 10,            # points every agent starts with in each domain
    "stake_fraction": 0.1,         # share of available points staked on claim_done
    "max_points_per_claim": 10,    # cap on newly created points per accepted claim
    "max_points_per_find": 15,     # cap on newly created points per reproduced counterexample that refutes a claim
    "verify_runs": 1,              # re-run each counterexample this many times; disagreeing runs count as flaky
    # Images this node will pull and run for mission checks. Checks name their image, so this is
    # what stops a hostile mission from making every node run an image of its choosing.
    "allowed_check_images": ["python:3.12-slim", "python:3.12-slim@sha256:*",
                             "python:3.13-slim", "python:3.13-slim@sha256:*"],
    "publish_roots": [],           # extra folders agents (MCP) may publish from; <home>/work is always allowed
    "credits_per_build": 1,        # credits minted per accepted build in a trusted creator's mission
    "credits_per_find": 2,         # credits minted per reproduced find in a trusted creator's mission
    "start_credits": 0,
    "trusted_creators": ["*"],     # creators whose accepted missions create new points ("*" = everyone, experiment mode)
    # Host protection (section 16.1)
    "supported_profiles": ["S0", "S1"],
    "sandbox_image": "python:3.12-slim",
    # Network
    "max_blob_bytes": 50 * 1024 * 1024,
    "public_url": None,            # set if this node is reachable by others; shared via peer exchange
}


def unsigned_part(event: dict) -> dict:
    return {k: event[k] for k in EVENT_FIELDS}


def event_id(event: dict) -> str:
    return sha256_hex(canonical(unsigned_part(event)))


def check_event(event, author: str, seq: int, prev) -> str | None:
    """Validate one event as the next element of a feed. Returns its id or None."""
    if not isinstance(event, dict) or event.get("v") != 1:
        return None
    try:
        if (event["author"] != author or event["seq"] != seq or event["prev"] != prev
                or not isinstance(event["ts"], int) or not isinstance(event["type"], str)
                or not isinstance(event["body"], dict) or not isinstance(event.get("sig"), str)):
            return None
    except KeyError:
        return None
    data = canonical(unsigned_part(event))
    if len(data) > MAX_EVENT_BYTES or not verify_signature(author, event["sig"], data):
        return None
    return sha256_hex(data)


def public_host(url: str) -> bool:
    """True if every address the URL's host resolves to is a public one."""
    import ipaddress
    import socket
    import urllib.parse
    host = urllib.parse.urlparse(url).hostname or ""
    if not host or host == "localhost" or host.endswith((".local", ".localhost", ".internal")):
        return False
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(host, None)}
    except (socket.gaierror, UnicodeError, OSError):
        return False
    for address in addresses:
        ip = ipaddress.ip_address(address.split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return bool(addresses)


class Store:
    def __init__(self, home: Path):
        self.home = Path(home).expanduser()
        self.feeds_dir = self.home / "feeds"
        self.blobs_dir = self.home / "blobs"
        self.feeds_dir.mkdir(parents=True, exist_ok=True)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self._feed_cache: dict[str, tuple] = {}  # path -> (mtime_ns, size, verified events)

    # ---- config & peers -------------------------------------------------
    def config(self) -> dict:
        cfg = dict(DEFAULT_CONFIG)
        path = self.home / "config.json"
        if path.exists():
            cfg.update(json.loads(path.read_text()))
        return cfg

    def set_config(self, key: str, value) -> None:
        path = self.home / "config.json"
        current = json.loads(path.read_text()) if path.exists() else {}
        current[key] = value
        path.write_text(json.dumps(current, indent=2))

    def peers(self) -> list[str]:
        path = self.home / "peers.json"
        return json.loads(path.read_text()) if path.exists() else []

    def add_peer(self, url: str, learned: bool = False) -> bool:
        """Add a peer URL. Peers learned from other peers (peer exchange) must be public hosts:
        otherwise a hostile peer could make this node probe its own local network."""
        url = url.rstrip("/")
        if not url.startswith(("http://", "https://")) or len(url) > 300:
            return False
        if learned and not self.config().get("allow_private_peers", False) and not public_host(url):
            return False
        peers = self.peers()
        if url in peers:
            return False
        peers.append(url)
        (self.home / "peers.json").write_text(json.dumps(peers[-200:], indent=2))
        return True

    # ---- feeds ------------------------------------------------------------
    def feed_path(self, author: str) -> Path:
        if not PUB_RE.match(author):
            raise ValueError("invalid author key")
        return self.feeds_dir / f"{author}.jsonl"

    def feed_authors(self) -> list[str]:
        return sorted(p.stem for p in self.feeds_dir.glob("*.jsonl") if PUB_RE.match(p.stem))

    def load_feed(self, author: str) -> list[dict]:
        """Read and verify a feed. Stops at the first invalid or broken event.
        Verified feeds are cached until the file changes: signatures are checked once, not per command."""
        path = self.feed_path(author)
        if not path.exists():
            return []
        stat = path.stat()
        cached = self._feed_cache.get(str(path))
        if cached and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
            return list(cached[2])
        events = self._read_feed(path, author)
        self._feed_cache[str(path)] = (stat.st_mtime_ns, stat.st_size, events)
        return list(events)

    def _read_feed(self, path: Path, author: str) -> list[dict]:
        events, prev = [], None
        with path.open("r", encoding="utf-8") as f:
            for seq, line in enumerate(f):
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    break
                eid = check_event(event, author, seq, prev)
                if eid is None:
                    break
                event = dict(event, id=eid)
                events.append(event)
                prev = eid
        return events

    def feed_len(self, author: str) -> int:
        return len(self.load_feed(author))

    def all_events(self) -> list[dict]:
        out = []
        for author in self.feed_authors():
            out.extend(self.load_feed(author))
        return out

    def _append_lines(self, author: str, events: list[dict]) -> None:
        path = self.feed_path(author)
        with path.open("a", encoding="utf-8") as f:
            if fcntl:
                fcntl.flock(f, fcntl.LOCK_EX)
            for event in events:
                clean = {k: event[k] for k in EVENT_FIELDS + ("sig",)}
                f.write(json.dumps(clean, ensure_ascii=False, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def build_own(self, identity: Identity, type_: str, body: dict, ts: int) -> dict:
        """Sign the next event of our own feed without writing it (so it can be checked first)."""
        feed = self.load_feed(identity.public_hex)
        prev = feed[-1]["id"] if feed else None
        if feed and ts < feed[-1]["ts"]:
            ts = feed[-1]["ts"]  # keep own feed monotonic
        event = {"v": 1, "author": identity.public_hex, "seq": len(feed), "prev": prev,
                 "ts": int(ts), "type": type_, "body": body}
        if len(canonical(unsigned_part(event))) > MAX_EVENT_BYTES:
            raise ValueError(f"event larger than {MAX_EVENT_BYTES} bytes; publish large content as an artifact")
        event["sig"] = identity.sign(canonical(unsigned_part(event)))
        return dict(event, id=event_id(event))

    def write_own(self, event: dict) -> dict:
        current = self.load_feed(event["author"])
        if event["seq"] != len(current) or event["prev"] != (current[-1]["id"] if current else None):
            raise ValueError("our own feed changed while the event was being prepared; try again")
        self._append_lines(event["author"], [event])
        self._extend_cache(event["author"], current, [event])
        return event

    def _extend_cache(self, author: str, before: list[dict], added: list[dict]) -> None:
        """Appending keeps the verified cache valid: no need to re-check every signature."""
        path = self.feed_path(author)
        stat = path.stat()
        self._feed_cache[str(path)] = (stat.st_mtime_ns, stat.st_size, before + added)

    def append_own(self, identity: Identity, type_: str, body: dict, ts: int) -> dict:
        return self.write_own(self.build_own(identity, type_, body, ts))

    def ingest(self, author: str, events: list[dict]) -> int:
        """Append events received from a peer if they correctly continue the local copy."""
        if not PUB_RE.match(author):
            return 0
        current = self.load_feed(author)
        prev = current[-1]["id"] if current else None
        seq = len(current)
        accepted = []
        for event in events[:MAX_EVENTS_PER_INGEST + len(current)]:
            if len(accepted) >= MAX_EVENTS_PER_INGEST:
                break
            if not isinstance(event, dict) or event.get("seq") != seq:
                if isinstance(event, dict) and isinstance(event.get("seq"), int) and event["seq"] < seq:
                    continue  # already have it
                break
            eid = check_event(event, author, seq, prev)
            if eid is None:
                break
            accepted.append(dict(event, id=eid))
            prev, seq = eid, seq + 1
        if accepted:
            self._append_lines(author, accepted)
            self._extend_cache(author, current, accepted)
        return len(accepted)

    # ---- blobs --------------------------------------------------------------
    def blob_path(self, cid: str) -> Path:
        if not CID_RE.match(cid):
            raise ValueError("invalid content id")
        return self.blobs_dir / cid

    def has_blob(self, cid: str) -> bool:
        return CID_RE.match(cid) is not None and self.blob_path(cid).exists()

    def put_blob(self, data: bytes) -> str:
        cid = cid_for(data)
        path = self.blob_path(cid)
        if not path.exists():
            fd, tmp = tempfile.mkstemp(dir=self.blobs_dir)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        return cid

    def get_blob(self, cid: str) -> bytes | None:
        if not self.has_blob(cid):
            return None
        data = self.blob_path(cid).read_bytes()
        return data if cid_for(data) == cid else None

    # ---- local verification results -----------------------------------------
    # Results of re-running checks and counterexamples on this node. They are local
    # facts, not events: every node re-runs for itself and nobody can write them for it.
    def verifications(self) -> dict:
        path = self.home / "verifications.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    def save_verification(self, key: str, record: dict) -> None:
        data = self.verifications()
        data[key] = record
        fd, tmp = tempfile.mkstemp(dir=self.home)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.home / "verifications.json")
