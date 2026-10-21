"""Local node storage.

Layout of a node home directory (default ~/.humansout):

    key.json            private key of this agent (mode 600)
    config.json         local policy (trust, supported security profiles, ...)
    peers.json          known peer URLs
    feeds/<pub>.jsonl   append-only, signed, hash-chained event feeds (own and replicated)
    blobs/<cid>         content-addressed artifacts
    verifications.json  results of re-running checks and counterexamples on this node
    netcache.json       what peers last answered (ETags), so unchanged peers cost a 304

Every feed has exactly one writer (its author), so a node directory can also be
replicated with any file-sync tool (Syncthing, git, rsync): feeds are verified on read.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import time
import urllib.parse
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
    "sync_missions": [],           # sync only these missions (and the ones this agent is in); [] = everything
    "sync_topics": [],             # ... or the missions whose category starts with one of these
    "mirror_blobs": None,          # fetch every referenced blob, not only those of own missions (None: hubs do)
}


def event_cids(event: dict) -> set[str]:
    """Content ids an event refers to (artifacts, evidence, check and counterexample files, witnesses)."""
    out = set()
    b = event["body"]
    if isinstance(b.get("cid"), str):
        out.add(b["cid"])
    evidence = b.get("evidence") if isinstance(b.get("evidence"), dict) else {}
    out.update(c for c in (evidence.get("cids") or []) if isinstance(c, str))
    manifest = b.get("manifest") if isinstance(b.get("manifest"), dict) else {}
    check = manifest.get("check") if isinstance(manifest.get("check"), dict) else {}
    counterexample = b.get("counterexample") if isinstance(b.get("counterexample"), dict) else {}
    for files in (check.get("files") or [], counterexample.get("files") or []):
        if isinstance(files, list):
            out.update(f["cid"] for f in files if isinstance(f, dict) and isinstance(f.get("cid"), str))
    witness = counterexample.get("witness")
    if isinstance(witness, dict) and isinstance(witness.get("cid"), str):
        out.add(witness["cid"])  # verifiers need it: without it a counterexample can never be decided
    return {c for c in out if CID_RE.match(c)}


def event_mission(event: dict) -> str | None:
    """The mission an event names directly (for an announcement: the hash of its manifest), or None."""
    if event["type"] == "mission.announce":
        manifest = event["body"].get("manifest")
        return sha256_hex(canonical(manifest)) if isinstance(manifest, dict) else None
    mid = event["body"].get("mission_id")
    return mid if isinstance(mid, str) and PUB_RE.match(mid) else None


def normalize_peer(url: str) -> str | None:
    """One spelling per peer: lower-case scheme and host, no default port, no trailing slash."""
    try:
        p = urllib.parse.urlsplit(url.strip())
        port = p.port
    except (ValueError, AttributeError):
        return None
    if p.scheme.lower() not in ("http", "https") or not p.hostname:
        return None
    scheme = p.scheme.lower()
    host = p.hostname.lower()
    netloc = f"[{host}]" if ":" in host else host
    if port and port != {"http": 80, "https": 443}[scheme]:
        netloc += f":{port}"
    if p.username is not None:
        netloc = p.netloc.rsplit("@", 1)[0] + "@" + netloc
    return f"{scheme}://{netloc}{p.path.rstrip('/')}"


def etag_for(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest()[:32] + '"'


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
        self._lock = threading.RLock()           # appends and index updates (HTTP handlers and sync threads share a store)
        self._changed = threading.Condition()    # notified after every append (long-polling peers wait on it)
        self._reset_index()

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
        raw = json.loads(path.read_text()) if path.exists() else []
        out = []
        for url in raw if isinstance(raw, list) else []:
            url = normalize_peer(url) if isinstance(url, str) else None
            if url and url not in out:
                out.append(url)
        return out

    def add_peer(self, url: str, learned: bool = False) -> bool:
        """Add a peer URL. Peers learned from other peers (peer exchange) must be public hosts:
        otherwise a hostile peer could make this node probe its own local network."""
        if not isinstance(url, str) or len(url) > 300:
            return False
        url = normalize_peer(url)
        if url is None:
            return False
        own = self.config().get("public_url")
        if isinstance(own, str) and normalize_peer(own) == url:
            return False  # ourselves, as other peers list us
        if learned and not self.config().get("allow_private_peers", False) and not public_host(url):
            return False
        with self._lock:
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

    @staticmethod
    def _write_lines(f, events: list[dict]) -> None:
        for event in events:
            clean = {k: event[k] for k in EVENT_FIELDS + ("sig",)}
            f.write(json.dumps(clean, ensure_ascii=False, separators=(",", ":")) + "\n")
        f.flush()
        os.fsync(f.fileno())

    def _locked_append(self, author: str, decide) -> list[dict]:
        """Append what `decide(current_events)` returns, holding this store's lock and the file's lock,
        so two threads or processes extending the same feed never interleave lines."""
        with self._lock:
            current = self.load_feed(author)
            if not decide(current):
                return []  # nothing to write: do not create an empty feed file
            with self.feed_path(author).open("a", encoding="utf-8") as f:
                if fcntl:
                    fcntl.flock(f, fcntl.LOCK_EX)
                current = self.load_feed(author)  # another process may have appended meanwhile
                events = decide(current)
                if events:
                    self._write_lines(f, events)
                    self._extend_cache(author, current, events)
                    self._ix_dirty.add(author)
        if events:
            with self._changed:
                self._changed.notify_all()
        return events

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
        def decide(current):
            if event["seq"] != len(current) or event["prev"] != (current[-1]["id"] if current else None):
                raise ValueError("our own feed changed while the event was being prepared; try again")
            return [event]
        self._locked_append(event["author"], decide)
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
        checked: dict[tuple, str | None] = {}  # (seq, prev, sig) -> id: rechecking under the file lock does not verify twice

        def decide(current):
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
                key = (seq, prev, event.get("sig"))
                if key not in checked:
                    checked[key] = check_event(event, author, seq, prev)
                eid = checked[key]
                if eid is None:
                    break
                accepted.append(dict(event, id=eid))
                prev, seq = eid, seq + 1
            return accepted
        return len(self._locked_append(author, decide))

    # ---- indexes for serving many peers ------------------------------------------
    # Hubs answer the same questions for thousands of agents: which feeds exist and how long they are,
    # which feeds take part in a mission, which blobs some event refers to. These are kept up to date
    # incrementally (feeds only grow) and refreshed with one directory scan when nothing changed.
    def _reset_index(self) -> None:
        self._ix_sizes: dict[str, int] = {}      # feed file sizes at the last refresh
        self._ix_lengths: dict[str, int] = {}
        self._ix_missions: dict[str, dict] = {}  # mission id -> {"authors", "category", "creator"}
        self._ix_items: dict[str, str] = {}      # claim or challenge id -> its mission
        self._ix_pending: list[dict] = []        # objections whose target is not known yet
        self._ix_referenced: set[str] = set()
        self._ix_version = 0
        self._ix_views: dict = {}                # rendered answers for the current version
        self._ix_dirty: set[str] = set()         # feeds this process appended to since the last refresh
        self._ix_scanned_at = float("-inf")      # last full directory scan (finds other processes' appends)

    def _index_event(self, e: dict) -> None:
        self._ix_referenced.update(event_cids(e))
        mid = event_mission(e)
        if mid is None:
            target = e["body"].get("target")
            if not isinstance(target, str):
                return
            mid = self._ix_items.get(target)
            if mid is None:
                self._ix_pending.append(e)
                return
        info = self._ix_missions.setdefault(mid, {"authors": set(), "category": None, "creator": None})
        info["authors"].add(e["author"])
        if e["type"] == "mission.announce":
            manifest = e["body"].get("manifest") if isinstance(e["body"].get("manifest"), dict) else {}
            info["creator"] = e["author"]
            info["category"] = manifest.get("category") if isinstance(manifest.get("category"), str) else None
        if e["type"] in ("claim_done", "challenge"):
            self._ix_items[e["id"]] = mid

    def refresh_index(self, rescan_after: float = 1.0) -> bool:
        """Bring the indexes up to date with the feed files; True if anything changed.
        This process's own appends are picked up at once; other processes' within `rescan_after` seconds
        (one directory scan, so thousands of polling agents do not each cost a scan)."""
        with self._lock:
            now = time.monotonic()
            if now - self._ix_scanned_at >= rescan_after:
                sizes = {}
                with os.scandir(self.feeds_dir) as entries:
                    for entry in entries:
                        if entry.name.endswith(".jsonl") and PUB_RE.match(entry.name[:-6]):
                            try:
                                sizes[entry.name[:-6]] = entry.stat().st_size
                            except FileNotFoundError:
                                continue
                self._ix_scanned_at = now
            elif self._ix_dirty:
                sizes = dict(self._ix_sizes)
                for author in self._ix_dirty:
                    try:
                        sizes[author] = self.feed_path(author).stat().st_size
                    except FileNotFoundError:
                        continue
            else:
                return False
            self._ix_dirty = set()
            if sizes == self._ix_sizes:
                return False
            seen = dict(self._ix_lengths)
            if any(a not in sizes or sizes[a] < self._ix_sizes[a] for a in self._ix_sizes):
                self._reset_index()  # a feed was removed or replaced: start over
                self._ix_scanned_at = now
                seen = {}
            for author, size in sizes.items():
                if self._ix_sizes.get(author) == size:
                    continue
                events = self.load_feed(author)
                if len(events) < seen.get(author, 0):
                    self._reset_index()
                    return self.refresh_index(rescan_after=0)
                for e in events[seen.get(author, 0):]:
                    self._index_event(e)
                self._ix_lengths[author] = len(events)
            self._resolve_pending()
            self._ix_sizes = sizes
            self._ix_version += 1
            self._ix_views = {}
            return True

    def _resolve_pending(self) -> None:
        progress = True
        while progress and self._ix_pending:
            progress, waiting = False, []
            for e in self._ix_pending:
                if e["body"]["target"] in self._ix_items:
                    self._index_event(e)
                    progress = True
                else:
                    waiting.append(e)
            self._ix_pending = waiting[-100_000:]

    def _view(self, key, render) -> tuple[bytes, str]:
        """A rendered JSON answer and its ETag, computed once per index version."""
        with self._lock:
            self.refresh_index()
            if len(self._ix_views) > 1000:  # topics and mission ids come from clients: keep this bounded
                self._ix_views = {}
            if key not in self._ix_views:
                value = render()
                body = None if value is None else json.dumps(value, separators=(",", ":")).encode()
                self._ix_views[key] = (body, etag_for(body) if body is not None else None)
            return self._ix_views[key]

    def feed_lengths(self) -> dict[str, int]:
        with self._lock:
            self.refresh_index()
            return {a: n for a, n in self._ix_lengths.items() if n}

    def feeds_view(self) -> tuple[bytes, str]:
        """`{author: length}` for every non-empty feed, and its ETag."""
        return self._view("feeds", lambda: dict(sorted((a, n) for a, n in self._ix_lengths.items() if n)))

    def mission_feeds_view(self, mission_id: str) -> tuple[bytes | None, str | None]:
        """`{author: length}` for the feeds with events in one mission (None if no feed mentions it)."""
        def render():
            info = self._ix_missions.get(mission_id)
            if info is None:
                return None
            return {a: self._ix_lengths.get(a, 0) for a in sorted(info["authors"])}
        return self._view(("mission", mission_id), render)

    def missions_view(self, topic: str | None = None) -> tuple[bytes, str]:
        """Announced missions (optionally those whose category starts with `topic`) with creator, category, feeds."""
        def render():
            return {mid: {"category": info["category"], "creator": info["creator"], "feeds": len(info["authors"])}
                    for mid, info in sorted(self._ix_missions.items())
                    if info["creator"] and (not topic or (info["category"] or "").startswith(topic))}
        return self._view(("missions", topic or ""), render)

    def missions_of(self, author: str) -> set[str]:
        """Missions in which this author has events (created, joined, built, objected...)."""
        with self._lock:
            self.refresh_index()
            return {mid for mid, info in self._ix_missions.items() if author in info["authors"]}

    def is_referenced(self, cid: str) -> bool:
        with self._lock:
            self.refresh_index()
            return cid in self._ix_referenced

    def referenced_cids(self) -> set[str]:
        with self._lock:
            self.refresh_index()
            return set(self._ix_referenced)

    def wait_for_change(self, current_etag, since: str, timeout: float) -> str:
        """Block until `current_etag()` differs from `since` or `timeout` seconds pass; returns the latest ETag.
        Appends by this process wake waiters at once; other processes' appends are seen within a second."""
        deadline = time.monotonic() + timeout
        etag = current_etag()
        while etag == since:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            with self._changed:
                self._changed.wait(min(1.0, left))
            etag = current_etag()
        return etag

    # ---- what peers last answered (conditional requests) ------------------------
    def netcache(self) -> dict:
        path = self.home / "netcache.json"
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except (json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def save_netcache(self, data: dict) -> None:
        with self._lock:
            fd, tmp = tempfile.mkstemp(dir=self.home)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, separators=(",", ":"))
            os.replace(tmp, self.home / "netcache.json")

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
