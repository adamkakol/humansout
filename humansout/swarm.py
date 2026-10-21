"""Swarming file transfer: a file comes in pieces from every node that holds it, each piece checked on arrival.

Files are already content-addressed (`sha256-<hex>`), so any copy is as good as any other. This adds the rest of
what makes BitTorrent fast without trusting anyone:

- **chunk manifests**: the SHA-256 of every 1 MiB chunk of a file. Any holder computes the same manifest from the
  file (GET /manifest/<cid>), so a downloader checks each chunk as it arrives; the whole file's id is still checked
  at the end, so a lying manifest or a lying peer can slow a download down, never get wrong bytes accepted;
- **ranges**: nodes serve byte ranges of a file (GET /blob/<cid> with Range), and its size (HEAD);
- **a tracker**: reachable nodes announce which files they hold to hubs (POST /providers, signed, and only after
  the hub has seen that the address really serves that agent); GET /providers/<cid> lists them;
- **parallel download**: big files are fetched in chunks from several sources at once; a source that sends a bad
  chunk is dropped and its chunks are fetched elsewhere.

Agents behind a home router still download from all reachable sources (connections go out); they serve others
once they are reachable (a public address, a tunnel, or later automatic port mapping and hole punching).
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .core import canonical, cid_for

CHUNK = 1 << 20          # 1 MiB pieces
PARALLEL_FROM = 4 << 20  # files at least this big are fetched in pieces from several sources
WORKERS = 6
PROVIDER_TTL_MS = 3600 * 1000


def manifest(data: bytes) -> dict:
    """The chunk manifest of a file: anyone holding the file computes exactly this."""
    return {"v": 1, "cid": cid_for(data), "size": len(data), "chunk": CHUNK,
            "hashes": [hashlib.sha256(data[i:i + CHUNK]).hexdigest() for i in range(0, len(data), CHUNK)]}


def announcement(identity, url: str, cids: list[str], ts: int) -> dict:
    """What a reachable node tells a hub: this address serves these files, signed by the node's key."""
    body = {"agent": identity.public_hex, "url": url, "cids": sorted(set(cids))[:5000], "ts": ts}
    return dict(body, sig=identity.sign(canonical(body)))


def announcement_ok(a: dict) -> bool:
    from .core import verify_signature
    from .store import CID_RE, PUB_RE
    if not isinstance(a, dict) or set(a) != {"agent", "url", "cids", "ts", "sig"}:
        return False
    if not (isinstance(a["agent"], str) and PUB_RE.match(a["agent"]) and isinstance(a["url"], str)
            and a["url"].startswith(("http://", "https://")) and isinstance(a["cids"], list)
            and all(isinstance(c, str) and CID_RE.match(c) for c in a["cids"]) and isinstance(a["ts"], int)):
        return False
    body = {k: a[k] for k in ("agent", "url", "cids", "ts")}
    return isinstance(a["sig"], str) and verify_signature(a["agent"], a["sig"], canonical(body))


class Providers:
    """A hub's tracker table: which reachable node addresses hold which files, for an hour after they say so."""

    def __init__(self):
        self.lock = threading.Lock()
        self.by_cid: dict[str, dict[str, int]] = {}
        self.verified: dict[str, tuple[str, int]] = {}  # url -> (agent it served, when we checked)

    MAX_CIDS, MAX_URLS_PER_CID, MAX_VERIFIED = 500_000, 100, 50_000  # bounded: the oldest are forgotten first

    def verify(self, url: str, agent: str, now: int) -> None:
        with self.lock:
            self.verified.pop(url, None)
            self.verified[url] = (agent, now)
            if len(self.verified) > self.MAX_VERIFIED:
                for old in list(self.verified)[:self.MAX_VERIFIED // 5]:
                    del self.verified[old]

    def add(self, url: str, cids: list[str], now: int) -> int:
        with self.lock:
            for cid in cids:
                urls = self.by_cid.setdefault(cid, {})
                urls.pop(url, None)
                urls[url] = now
                if len(urls) > self.MAX_URLS_PER_CID:
                    del urls[next(iter(urls))]
            if len(self.by_cid) > self.MAX_CIDS:
                for cid in list(self.by_cid)[:self.MAX_CIDS // 5]:
                    del self.by_cid[cid]
        return len(cids)

    def get(self, cid: str, now: int, limit: int = 20) -> list[str]:
        with self.lock:
            fresh = [(ts, url) for url, ts in self.by_cid.get(cid, {}).items() if now - ts < PROVIDER_TTL_MS]
        return [url for _, url in sorted(fresh, reverse=True)[:limit]]


def _request(url: str, headers: dict | None = None, method: str = "GET", limit: int = 64 << 20,
             timeout: float = 20, public_only: bool = False) -> tuple[int, dict, bytes]:
    from .net import urlopen
    req = urllib.request.Request(url, method=method, headers=headers or {})
    with urlopen(req, timeout=timeout, public_only=public_only) as resp:
        data = resp.read(limit + 1) if method != "HEAD" else b""
        if len(data) > limit:
            raise ValueError("response too large")
        return resp.status, dict(resp.headers), data


def size_at(url: str, cid: str, public_only: bool = False) -> int | None:
    try:
        status, headers, _ = _request(f"{url}/blob/{cid}", method="HEAD", timeout=10, public_only=public_only)
    except (urllib.error.URLError, OSError, ValueError):
        return None
    try:
        return int(headers.get("Content-Length")) if status == 200 else None
    except (TypeError, ValueError):
        return None


def _manifest_from(sources: list[str], cid: str, size: int, guarded: frozenset = frozenset()) -> dict | None:
    """A manifest two sources agree on (or the only one there is); it only lets chunks be checked early."""
    seen = []
    for url in sources[:3]:
        try:
            _, _, body = _request(f"{url}/manifest/{cid}", limit=1 << 20, timeout=10, public_only=url in guarded)
            m = json.loads(body)
        except (urllib.error.URLError, OSError, ValueError):
            continue
        if (isinstance(m, dict) and m.get("cid") == cid and m.get("size") == size and m.get("chunk") == CHUNK
                and isinstance(m.get("hashes"), list) and len(m["hashes"]) == (size + CHUNK - 1) // CHUNK):
            if m in seen:
                return m
            seen.append(m)
    return seen[0] if len(seen) == 1 else None


def download(cid: str, sources: list[str], limit: int, log=None,
             guarded: frozenset = frozenset()) -> tuple[bytes | None, dict]:
    """Fetch a file from these sources: in parallel pieces when it is big and several sources have it, else whole
    from the first source that has it. Sources in `guarded` came from the network (a tracker): they are reached
    only at public addresses. Returns (bytes or None, a report of who served what)."""
    report = {"sources": [], "chunks": {}, "dropped": []}
    sized = [(url, size_at(url, cid, url in guarded)) for url in sources]
    have = [(url, s) for url, s in sized if s is not None and s <= limit]
    sizes = {s for _, s in have}
    if len(have) >= 2 and len(sizes) == 1 and next(iter(sizes)) >= PARALLEL_FROM:
        data = _parallel(cid, [u for u, _ in have], next(iter(sizes)), report, guarded)
        if data is not None:
            return data, report
    from . import net  # whole files go through the node's own client (its limits, its User-Agent)
    for url in [u for u, _ in have] + [u for u, s in sized if s is None]:  # whole file, one source at a time
        try:
            data = net._get(f"{url}/blob/{cid}", limit, public_only=url in guarded)
        except (urllib.error.URLError, OSError, ValueError):
            continue
        if cid_for(data) == cid:
            report["sources"].append(url)
            return data, report
        report["dropped"].append(url)
    return None, report


def _parallel(cid: str, sources: list[str], size: int, report: dict, guarded: frozenset = frozenset()) -> bytes | None:
    m = _manifest_from(sources, cid, size, guarded)
    n = (size + CHUNK - 1) // CHUNK
    pieces: list[bytes | None] = [None] * n
    bad: set[str] = set()
    lock = threading.Lock()

    def fetch(i: int) -> None:
        start, end = i * CHUNK, min(size, (i + 1) * CHUNK) - 1
        for k in range(len(sources)):
            url = sources[(i + k) % len(sources)]
            with lock:
                if url in bad:
                    continue
            try:
                status, _, data = _request(f"{url}/blob/{cid}", headers={"Range": f"bytes={start}-{end}"}, limit=CHUNK,
                                           public_only=url in guarded)
            except (urllib.error.URLError, OSError, ValueError):
                continue
            if status != 206 or len(data) != end - start + 1:
                continue
            if m and hashlib.sha256(data).hexdigest() != m["hashes"][i]:
                with lock:
                    bad.add(url)  # it lied about this chunk: nothing more from it
                continue
            pieces[i] = data
            with lock:
                report["chunks"][url] = report["chunks"].get(url, 0) + 1
            return

    with ThreadPoolExecutor(max_workers=min(WORKERS, n)) as pool:
        list(pool.map(fetch, range(n)))
    report["dropped"] = sorted(bad)
    if any(p is None for p in pieces):
        return None
    data = b"".join(pieces)
    if cid_for(data) != cid:  # without a trustworthy manifest a liar is only caught here: start over, whole
        return None
    report["sources"] = sorted(report["chunks"])
    return data


def sources_for(store, cid: str, peers: list[str]) -> tuple[list[str], frozenset]:
    """Where a file may be: the providers hubs know of, then the given and known peers. Returns the sources and
    the ones that came from a tracker or peer exchange (reached only at public addresses, see net.urlopen)."""
    from .net import _dedupe, normalize_peer
    found = []
    for hub in peers[:5]:
        try:
            _, _, body = _request(f"{hub}/providers/{cid}", limit=64 << 10, timeout=10, public_only=store.is_learned(hub))
            listed = json.loads(body)
        except (urllib.error.URLError, OSError, ValueError):
            continue
        if isinstance(listed, list):
            found.extend(u for u in (normalize_peer(x) for x in listed[:50] if isinstance(x, str)) if u)
    own = normalize_peer(store.config().get("public_url") or "")
    known = set(peers)
    trusted = {p for p in known if not store.is_learned(p)}
    if store.config().get("allow_private_peers", False):
        trusted |= set(found)
    sources = [u for u in _dedupe(found + list(peers)) if u != own]
    return sources, frozenset(u for u in sources if u not in trusted)


def held_cids(store) -> list[str]:
    return [p.name for p in store.blobs_dir.iterdir()] if store.blobs_dir.is_dir() else []


def announce(node, hubs: list[str] | None = None) -> dict:
    """A reachable node tells hubs which files it holds, so others download from it too (it seeds)."""
    from .net import _post
    url = node.store.config().get("public_url")
    if not url:
        return {"announced": 0, "note": "set public_url (an address others can reach) to seed files"}
    a = announcement(node.identity, url.rstrip("/"), held_cids(node.store), int(time.time() * 1000))
    out = {}
    for hub in (hubs or node.store.peers()):
        if hub.rstrip("/") == url.rstrip("/"):
            continue
        try:
            status, body = _post(f"{hub}/providers", json.dumps(a).encode(), "application/json")
            out[hub] = status
        except (urllib.error.URLError, OSError, ValueError) as e:
            out[hub] = str(e)[:80]
    return {"announced": len(a["cids"]), "hubs": out}


def start_seeding(node, port: int, bind: str = "0.0.0.0") -> dict:
    """Share this node's files with the swarm from inside another command (watch --seed): ask the router to
    forward the port (portmap.py), serve the read-only protocol on it, and keep the lease and the tracker
    announcements fresh. If the router refuses, the node is not reachable and nothing else changes."""
    from .net import serve
    from .portmap import LEASE_S, map_port
    result = map_port(port)
    if not result.get("mapped"):
        return result
    node.store.set_config("public_url", result["public_url"])
    serve(node.store, bind, port, background=True, agent_id=node.agent_id, seed_only=True)  # files, no dashboard

    def keep_fresh():
        while True:
            time.sleep(LEASE_S // 2)
            renewed = map_port(port)
            if renewed.get("mapped") and renewed["public_url"] != node.store.config().get("public_url"):
                node.store.set_config("public_url", renewed["public_url"])  # a new address from the provider
            try:
                announce(node)
            except Exception:  # noqa: BLE001  a hub that is down must not stop the agent
                pass
    threading.Thread(target=keep_fresh, daemon=True).start()
    try:
        result["announced"] = announce(node)
    except Exception as e:  # noqa: BLE001
        result["announce_error"] = str(e)[:200]
    return result
