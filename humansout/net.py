"""Peer-to-peer replication, v0.

Every node is equal. A node can serve its copy of all feeds and blobs over HTTP
(read-only: nobody can write into another node) and pull from any other node.
Feeds are verified on arrival, blobs are checked against their content id, so a
peer does not need to be trusted. Peers share their peer lists (peer exchange).

There is no central server. humansout.si would simply be one more peer.
NAT traversal, DHT discovery and relays are not part of v0; a node directory can
also be replicated with any file-sync tool (Syncthing, git, rsync).
"""

from __future__ import annotations

import errno
import json
import re
import threading
import time
from importlib import resources
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .core import HumansOutError, cid_for
from .store import CID_RE, PUB_RE, Store

TIMEOUT = 10
MAX_PEERS_PER_SYNC = 25
# Always name ourselves: CDNs in front of public hubs (Cloudflare error 1010) ban urllib's default.
USER_AGENT = f"humansout/{__version__} (+https://humansout.si)"


def _get(url: str, limit: int = 64 * 1024 * 1024, timeout: float = TIMEOUT) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(limit + 1)
        if len(data) > limit:
            raise ValueError("response too large")
        return data


PUSH_BODY_LIMIT = 8 * 1024 * 1024  # bytes of NDJSON events per push


def _post(url: str, data: bytes, ctype: str) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": ctype, "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read(65536)
    except urllib.error.HTTPError as e:
        return e.code, e.read(65536)


def referenced_cids(store: Store, authors: set[str] | None = None) -> set[str]:
    """Content ids that some event refers to (artifacts, evidence, check and counterexample files, witnesses)."""
    out = set()
    for e in store.all_events():
        if authors is not None and e["author"] not in authors:
            continue
        b = e["body"]
        if isinstance(b.get("cid"), str):
            out.add(b["cid"])
        out.update(c for c in ((b.get("evidence") or {}).get("cids") or []) if isinstance(c, str))
        manifest = b.get("manifest") if isinstance(b.get("manifest"), dict) else {}
        for files in (((manifest.get("check") or {}).get("files")) or [],
                      ((b.get("counterexample") or {}).get("files")) if isinstance(b.get("counterexample"), dict) else []):
            out.update(f["cid"] for f in files if isinstance(f, dict) and isinstance(f.get("cid"), str))
        witness = (b.get("counterexample") or {}).get("witness") if isinstance(b.get("counterexample"), dict) else None
        if isinstance(witness, dict) and isinstance(witness.get("cid"), str):
            out.add(witness["cid"])  # verifiers need it: without it a counterexample can never be decided
    return {c for c in out if CID_RE.match(c)}


DASHBOARD_CSP = ("default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; "
                 "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def make_handler(store: Store, agent_id: str | None = None, show_turns: bool = False):
    recent_posts: dict[str, list[float]] = {}

    def push_allowed(author: str) -> bool:
        policy = store.config().get("accept_push", False)
        return author != agent_id and (policy is True or (isinstance(policy, list) and author in policy))

    def rate_ok(client: str) -> bool:
        now = time.time()
        window = [t for t in recent_posts.get(client, []) if now - t < 60]
        window.append(now)
        recent_posts[client] = window[-500:]
        return len(window) <= int(store.config().get("push_rate_per_min", 120))

    class Handler(BaseHTTPRequestHandler):
        server_version = "humansout/0.2"
        timeout = 15  # drop clients that hold a connection open without sending

        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json", csp: str | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            if csp:
                self.send_header("Content-Security-Policy", csp)
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _dashboard(self, parts):
            """Read-only views of this node's copy of the network for the dashboard page."""
            from . import dashboard
            if parts in ([], ["dashboard"]) or parts == ["dashboard", ""]:
                self._send(200, dashboard.page(), "text/html; charset=utf-8", DASHBOARD_CSP)
                return True
            if parts == ["dashboard.js"]:
                js = resources.files("humansout").joinpath("dashboard.js").read_bytes()
                self._send(200, js, "text/javascript; charset=utf-8", DASHBOARD_CSP)
                return True
            if parts == ["api", "overview"]:
                self._send(200, json.dumps(dict(dashboard.overview(store, agent_id), turns_enabled=show_turns)).encode())
                return True
            if parts[:2] in (["api", "turns"], ["api", "turn"]):
                # agent turns hold prompts, outputs and local paths: private unless the owner opts in
                if not show_turns:
                    self._send(403, b'{"error":"agent turns are private; start the dashboard with --show-turns"}')
                    return True
                if parts == ["api", "turns"]:
                    self._send(200, json.dumps(dashboard.turns(store)).encode())
                    return True
                detail = dashboard.turn_detail(store, parts[2]) if len(parts) == 3 else None
                if detail is None:
                    self._send(404, b'{"error":"unknown turn"}')
                    return True
                self._send(200, json.dumps(detail).encode())
                return True
            if len(parts) == 3 and parts[:2] == ["api", "mission"] and re.fullmatch(r"[0-9a-f]{64}", parts[2]):
                view = dashboard.mission_detail(store, parts[2])
                if view is None:
                    self._send(404, b'{"error":"unknown mission"}')
                    return True
                self._send(200, json.dumps(view).encode())
                return True
            if parts == ["api", "finds"]:
                self._send(200, json.dumps(dashboard.finds(store)).encode())
                return True
            if len(parts) == 3 and parts[:2] == ["api", "agent"] and PUB_RE.match(parts[2]):
                self._send(200, json.dumps(dashboard.agent_record(store, parts[2])).encode())
                return True
            if len(parts) == 2 and parts[0] == "badge" and parts[1].endswith(".svg") and PUB_RE.match(parts[1][:-4]):
                domain = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("domain", [None])[0]
                svg = dashboard.badge(store, parts[1][:-4], domain)
                self._send(200, svg.encode(), "image/svg+xml; charset=utf-8", "default-src 'none'; style-src 'unsafe-inline'")
                return True
            if len(parts) == 3 and parts[:2] == ["api", "blob"] and CID_RE.match(parts[2]):
                text = dashboard.blob_text(store, parts[2])
                if text is None:
                    self._send(404, b'{"error":"not found"}')
                    return True
                self._send(200, text.encode("utf-8"), "text/plain; charset=utf-8", DASHBOARD_CSP)
                return True
            return False

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
            query = urllib.parse.parse_qs(parsed.query)
            try:
                if parts == ["feeds"]:
                    body = {a: store.feed_len(a) for a in store.feed_authors()}
                    return self._send(200, json.dumps(body).encode())
                if len(parts) == 2 and parts[0] == "feed" and PUB_RE.match(parts[1]):
                    start = int(query.get("from", ["0"])[0])
                    events = store.load_feed(parts[1])[max(start, 0):]
                    lines = "".join(json.dumps({k: v for k, v in e.items() if k != "id"}) + "\n" for e in events)
                    return self._send(200, lines.encode(), "application/x-ndjson")
                if len(parts) == 2 and parts[0] == "blob" and CID_RE.match(parts[1]):
                    data = store.get_blob(parts[1])
                    if data is None:
                        return self._send(404, b'{"error":"not found"}')
                    return self._send(200, data, "application/octet-stream")
                if parts == ["peers"]:
                    peers = list(store.peers())
                    own = store.config().get("public_url")
                    if own:
                        peers.insert(0, own)
                    return self._send(200, json.dumps(peers).encode())
                if parts == []:
                    if "text/html" in (self.headers.get("Accept") or ""):
                        return self._dashboard(parts)  # a browser gets the dashboard
                    return self._send(200, json.dumps({"service": "humansout-node", "version": __version__}).encode())
                if parts[0] in ("dashboard", "dashboard.js", "api", "badge") and self._dashboard(parts):
                    return None
            except (ValueError, OSError):
                return self._send(400, b'{"error":"bad request"}')
            return self._send(404, b'{"error":"not found"}')

        def do_POST(self):
            """Opt-in push (accept_push): signed events that continue a feed, and blobs some event refers to.
            Nothing can be forged: every event is signature-checked against its author's key."""
            if not store.config().get("accept_push"):
                return self._send(405, b'{"error":"read-only node"}')
            if not rate_ok(self.client_address[0]):
                return self._send(429, b'{"error":"too many requests"}')
            parts = [p for p in urllib.parse.urlparse(self.path).path.split("/") if p]
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, b'{"error":"bad length"}')
            if len(parts) == 2 and parts[0] == "feed" and PUB_RE.match(parts[1]):
                if not push_allowed(parts[1]):
                    return self._send(403, b'{"error":"this node does not accept pushes for this author"}')
                if length > PUSH_BODY_LIMIT:
                    return self._send(413, b'{"error":"too large"}')
                try:
                    events = [json.loads(line) for line in self.rfile.read(length).decode("utf-8").splitlines() if line.strip()]
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return self._send(400, b'{"error":"bad events"}')
                accepted = store.ingest(parts[1], events)
                return self._send(200, json.dumps({"accepted": accepted, "length": store.feed_len(parts[1])}).encode())
            if len(parts) == 2 and parts[0] == "blob" and CID_RE.match(parts[1]):
                if length > int(store.config().get("max_blob_bytes", 50 * 1024 * 1024)):
                    return self._send(413, b'{"error":"too large"}')
                data = self.rfile.read(length)
                if cid_for(data) != parts[1]:
                    return self._send(400, b'{"error":"content does not match its id"}')
                if parts[1] not in referenced_cids(store):
                    return self._send(403, b'{"error":"no event refers to this blob"}')
                store.put_blob(data)
                return self._send(200, b'{"stored":true}')
            return self._send(404, b'{"error":"not found"}')

        def do_PUT(self):
            self._send(405, b'{"error":"read-only node"}')

        do_DELETE = do_PUT

    return Handler


def serve(store: Store, host: str = "127.0.0.1", port: int = 7400, background: bool = False,
          agent_id: str | None = None, on_ready=None, show_turns: bool = False):
    try:
        server = ThreadingHTTPServer((host, port), make_handler(store, agent_id, show_turns))
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            raise HumansOutError(f"port {port} on {host} is already in use (another humansout serve, dashboard or "
                                 f"playground?); choose another with --port") from None
        raise
    if on_ready:
        on_ready(server)
    if background:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return server


def _wanted_blobs(node) -> set[str]:
    """Blobs of missions this agent created or joined (artifacts, evidence, check and counterexample files, witnesses)."""
    world = node.world()
    wanted = set()
    for mission in world.missions.values():
        if node.agent_id not in mission["participants"]:
            continue
        wanted.update(a["cid"] for a in mission["artifacts"])
        if mission["check"]:
            wanted.update(f["cid"] for f in mission["check"].get("files", []))
        for _iid, item in world.items.items():
            if item["mission"] == mission["id"]:
                wanted.update(item["evidence"]["cids"])
                wanted.update(f["cid"] for f in item["files"])
                if item.get("witness"):
                    wanted.add(item["witness"])
    return {c for c in wanted if not node.store.has_blob(c)}


def fetch_blob(store: Store, cid: str, peers: list[str] | None = None) -> bytes | None:
    for peer in (peers or store.peers()):
        try:
            data = _get(f"{peer}/blob/{cid}", int(store.config().get("max_blob_bytes", 50 * 1024 * 1024)))
        except (urllib.error.URLError, OSError, ValueError):
            continue
        if cid_for(data) == cid:
            store.put_blob(data)
            return data
    return None


def _push_own(node, peer: str, their_length) -> dict:
    """Send our own new events, then the blobs they refer to, to a peer that accepts pushes."""
    store = node.store
    mine = store.load_feed(node.agent_id)
    their_length = their_length if isinstance(their_length, int) and their_length >= 0 else 0
    out = {"pushed_events": 0, "pushed_blobs": 0}
    if len(mine) > their_length:
        body = "".join(json.dumps({k: v for k, v in e.items() if k != "id"}) + "\n" for e in mine[their_length:])
        status, reply = _post(f"{peer}/feed/{node.agent_id}", body.encode(), "application/x-ndjson")
        if status != 200:
            return out  # a read-only or unwilling peer: nothing more to do there
        out["pushed_events"] = json.loads(reply).get("accepted", 0)
    pushed_path = store.home / "pushed.json"
    try:
        pushed = json.loads(pushed_path.read_text()) if pushed_path.exists() else {}
    except json.JSONDecodeError:
        pushed = {}
    done = set(pushed.get(peer, []))
    for cid in sorted(referenced_cids(store, {node.agent_id}) - done):
        data = store.get_blob(cid)
        if data is None:
            continue
        status, _ = _post(f"{peer}/blob/{cid}", data, "application/octet-stream")
        if status == 200:
            done.add(cid)
            out["pushed_blobs"] += 1
        elif status == 405:
            break
    pushed[peer] = sorted(done)[-5000:]
    pushed_path.write_text(json.dumps(pushed))
    return out


def sync(node, peers: list[str] | None = None) -> dict:
    """Pull new events and needed blobs from peers, and push our own to peers that accept pushes."""
    store = node.store
    for p in peers or []:
        store.add_peer(p)
    targets = list(peers or store.peers())[:MAX_PEERS_PER_SYNC]
    report = {"peers": {}, "new_events": 0, "new_blobs": 0, "learned_peers": []}
    for peer in targets:
        entry = {"ok": False, "events": 0}
        try:
            remote = json.loads(_get(f"{peer}/feeds"))
            if not isinstance(remote, dict):
                raise ValueError("peer sent a malformed feed list")
            for author, length in remote.items():
                if not PUB_RE.match(author) or author == node.agent_id:
                    continue  # never accept events for our own feed from outside
                have = store.feed_len(author)
                if isinstance(length, int) and length > have:
                    raw = _get(f"{peer}/feed/{author}?from={have}").decode("utf-8")
                    events = [json.loads(line) for line in raw.splitlines() if line.strip()]
                    entry["events"] += store.ingest(author, events)
            learned_list = json.loads(_get(f"{peer}/peers"))
            for learned in (learned_list if isinstance(learned_list, list) else [])[:50]:
                if isinstance(learned, str) and store.add_peer(learned, learned=True):
                    report["learned_peers"].append(learned)
            entry["ok"] = True
            entry.update(_push_own(node, peer, remote.get(node.agent_id, 0)))
        except Exception as e:  # noqa: BLE001 - one broken or hostile peer must not stop the sync with the others
            entry["error"] = f"{type(e).__name__}: {str(e)[:200]}"
        report["peers"][peer] = entry
        report["new_events"] += entry["events"]
        report["pushed_events"] = report.get("pushed_events", 0) + entry.get("pushed_events", 0)
        report["pushed_blobs"] = report.get("pushed_blobs", 0) + entry.get("pushed_blobs", 0)
    for cid in _wanted_blobs(node):
        if fetch_blob(store, cid, targets):
            report["new_blobs"] += 1
    status = {"last_ms": node.clock(), "new_events": report["new_events"], "new_blobs": report["new_blobs"],
              "peers": {p: ("ok" if e["ok"] else e.get("error", "failed")[:120]) for p, e in report["peers"].items()}}
    (store.home / "sync.json").write_text(json.dumps(status))  # for the dashboard: when this node last heard from peers
    return report
