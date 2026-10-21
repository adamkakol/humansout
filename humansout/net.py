"""Peer-to-peer replication, v0.

Every node is equal. A node can serve its copy of all feeds and blobs over HTTP
(read-only: nobody can write into another node) and pull from any other node.
Feeds are verified on arrival, blobs are checked against their content id, so a
peer does not need to be trusted. Peers share their peer lists (peer exchange).

There is no central server. humansout.si would simply be one more peer.
NAT traversal, DHT discovery and relays are not part of v0; a node directory can
also be replicated with any file-sync tool (Syncthing, git, rsync).

Scaling (docs/SCALING.md): blobs and complete feed ranges are immutable and served with
year-long cache headers, so a CDN in front of a hub answers repeats; feed lists and mission
indexes carry ETags, so an unchanged hub answers 304; an agent can sync only the missions it
works on; and a node syncs with all its peers at once, taking each feed from whichever peer has
the most of it.
"""

from __future__ import annotations

import errno
import http.client
import ipaddress
import math
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from importlib import resources
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .core import HumansOutError, cid_for
from .store import CID_RE, PUB_RE, Store, etag_for, event_cids, normalize_peer, public_host

TIMEOUT = 10
MAX_PEERS_PER_SYNC = 25
SYNC_WORKERS = 8                       # concurrent requests per sync (peers, feeds, blobs)
IMMUTABLE = "public, max-age=31536000, immutable"
LISTING_CACHE = "public, max-age=5"    # feed lists, mission indexes, peers: a CDN may hold them a few seconds
WAIT_DEFAULT_S, WAIT_MAX_S = 25, 55    # long-poll; Cloudflare closes idle requests after 100 s
MAX_WAITERS = 64                       # long-polls hold a thread each: past this, clients are told to poll
BLOB_RETRY_S = 3600                    # a blob no peer had is asked for again after this long
FEATURES = ["etag", "missions", "ranges", "wait", "blob-ranges", "manifests", "providers"]  # announced at GET /
# Always name ourselves: CDNs in front of public hubs (Cloudflare error 1010) ban urllib's default.
USER_AGENT = f"humansout/{__version__} (+https://humansout.si)"


# ---- outgoing requests ---------------------------------------------------------------------------------------
# Every protocol request goes through one of two openers. Neither has urllib's file:, ftp: or data: handlers
# (a hostile hub could list "file:///...#" as a source), and both follow a redirect only to the same host.
# The guarded one also refuses a connection that ends at a non-public address, checked on the socket after
# it connects: an address learned from the network (peer exchange, a tracker) cannot make this node reach its
# own machine or local network, not even by a redirect or a DNS name that changes between check and use.
def _refuse_private(sock) -> None:
    ip = ipaddress.ip_address(sock.getpeername()[0].split("%")[0])
    ip = getattr(ip, "ipv4_mapped", None) or ip
    if not ip.is_global:
        sock.close()
        raise urllib.error.URLError(f"refusing to connect to a non-public address ({ip})")


class _GuardedHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        super().connect()
        _refuse_private(self.sock)


class _GuardedHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        super().connect()
        _refuse_private(self.sock)


class _GuardedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_GuardedHTTPConnection, req)


class _GuardedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_GuardedHTTPSConnection, req, context=self._context)


class _SameHostRedirect(urllib.request.HTTPRedirectHandler):
    """http -> https on the same host is fine (a CDN upgrading the scheme); anywhere else is not followed."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlsplit(urllib.parse.urljoin(req.full_url, newurl))
        if target.scheme not in ("http", "https") or target.hostname != urllib.parse.urlsplit(req.full_url).hostname:
            raise urllib.error.HTTPError(req.full_url, code, f"redirect to another host refused: {msg}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener(guarded: bool) -> urllib.request.OpenerDirector:
    opener = urllib.request.OpenerDirector()
    handlers = [urllib.request.UnknownHandler(), urllib.request.HTTPDefaultErrorHandler(), _SameHostRedirect(),
                urllib.request.HTTPErrorProcessor()]
    handlers += ([_GuardedHTTPHandler(), _GuardedHTTPSHandler()] if guarded
                 else [urllib.request.ProxyHandler(), urllib.request.HTTPHandler(), urllib.request.HTTPSHandler()])
    for handler in handlers:
        opener.add_handler(handler)
    return opener


_OPENERS = {False: _opener(False), True: _opener(True)}


def urlopen(req, timeout: float = TIMEOUT, public_only: bool = False):
    """Open an http(s) request for the protocol; `public_only` for addresses that came from the network."""
    if isinstance(req, str):
        req = urllib.request.Request(req)
    if urllib.parse.urlsplit(req.full_url).scheme not in ("http", "https"):
        raise urllib.error.URLError(f"not an http(s) address: {req.full_url[:80]}")
    req.add_header("User-Agent", USER_AGENT)
    return _OPENERS[bool(public_only)].open(req, timeout=timeout)


def _get(url: str, limit: int = 64 * 1024 * 1024, timeout: float = TIMEOUT, public_only: bool = False) -> bytes:
    with urlopen(url, timeout=timeout, public_only=public_only) as resp:
        data = resp.read(limit + 1)
        if len(data) > limit:
            raise ValueError("response too large")
        return data


PUSH_BODY_LIMIT = 8 * 1024 * 1024  # bytes of NDJSON events per push


def _post(url: str, data: bytes, ctype: str, public_only: bool = False) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": ctype})
    try:
        with urlopen(req, timeout=TIMEOUT, public_only=public_only) as resp:
            return resp.status, resp.read(65536)
    except urllib.error.HTTPError as e:
        return e.code, e.read(65536)


def _get_conditional(url: str, etag: str | None, limit: int = 64 * 1024 * 1024,
                     timeout: float = TIMEOUT, public_only: bool = False) -> tuple[int, bytes, str | None]:
    """GET with If-None-Match. Returns (status, body, etag); 304 means the cached body still holds."""
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    try:
        with urlopen(urllib.request.Request(url, headers=headers), timeout=timeout, public_only=public_only) as resp:
            data = resp.read(limit + 1)
            if len(data) > limit:
                raise ValueError("response too large")
            return resp.status, data, resp.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code in (304, 404):
            return e.code, b"", e.headers.get("ETag") or etag
        raise


def _etag_matches(header: str | None, etag: str | None) -> bool:
    """If-None-Match comparison (weak, as RFC 9110 asks; CDNs turn strong ETags weak when they compress)."""
    if not header or not etag:
        return False
    def bare(t):
        return t.strip().removeprefix("W/").strip('"')
    return header.strip() == "*" or any(bare(t) == bare(etag) for t in header.split(","))


def referenced_cids(store: Store, authors: set[str] | None = None) -> set[str]:
    """Content ids that some event refers to (artifacts, evidence, check and counterexample files, witnesses)."""
    if authors is None:
        return store.referenced_cids()  # kept up to date incrementally
    out = set()
    for author in authors:
        for e in store.load_feed(author):
            out |= event_cids(e)
    return out


def _client_key(handler) -> str:
    """Who is asking, for rate limits. Behind a local reverse proxy or tunnel (cloudflared, nginx) every request
    arrives from loopback, so the proxy's header names the client; from anywhere else headers are not trusted."""
    peer = handler.client_address[0]
    try:
        loopback = ipaddress.ip_address(peer.split("%")[0]).is_loopback
    except ValueError:
        loopback = False
    if loopback:
        # the LAST X-Forwarded-For entry is the one the local proxy appended (nginx's $proxy_add_x_forwarded_for);
        # the first is whatever the client sent, a fresh rate-limit key per request (found by the review swarm)
        forwarded = handler.headers.get("CF-Connecting-IP") or (handler.headers.get("X-Forwarded-For") or "").split(",")[-1]
        if forwarded.strip():
            return forwarded.strip()[:64]
    return peer


def _shareable(url: str) -> bool:
    """A peer address worth telling others about (no DNS lookup): not loopback, private or link-local."""
    host = urllib.parse.urlsplit(url).hostname or ""
    if host == "localhost" or host.endswith((".local", ".localhost", ".internal", ".lan", ".home.arpa")):
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True  # a name: others resolve it themselves, and refuse it if it is not public


def _host_name(handler) -> str:
    value = handler.headers.get("Host") or ""
    try:
        return (urllib.parse.urlsplit("//" + value).hostname or "").lower()
    except ValueError:
        return "\0"  # unparseable: matches nothing


def _local_host(name: str) -> bool:
    """`localhost`, a name under it, or an IP address: the names a browser uses for this machine. A web page that
    rebinds its own domain to 127.0.0.1 (DNS rebinding) still sends its domain: it is none of these."""
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def _known_host(handler, store) -> bool:
    """A Host this node answers to: a local name, its public_url's host, or one in allowed_hosts."""
    name = _host_name(handler)
    if not name or _local_host(name):
        return True
    own = urllib.parse.urlsplit(store.config().get("public_url") or "").hostname
    return name == (own or "").lower() or name in [h.lower() for h in store.config().get("allowed_hosts") or []]


def _local_viewer(handler) -> bool:
    """A request from this machine itself, by a local name, not relayed by a proxy or tunnel (those add a
    forwarding header). Without the name check a rebound web page would pass for the owner."""
    try:
        loopback = ipaddress.ip_address(handler.client_address[0].split("%")[0]).is_loopback
    except ValueError:
        return False
    name = _host_name(handler)
    return (loopback and (not name or _local_host(name))
            and not (handler.headers.get("CF-Connecting-IP") or handler.headers.get("X-Forwarded-For")
                     or handler.headers.get("Forwarded")))


DASHBOARD_CSP = ("default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; "
                 "img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def make_handler(store: Store, agent_id: str | None = None, show_turns: bool = False, seed_only: bool = False):
    """`seed_only`: the protocol's read-only routes and nothing else (a home node seeding through its router:
    no dashboard, no pushes). The dashboard shows this node's own watch and sync status only to a viewer on
    this machine (or with --show-turns): on a public address it would tell anyone when its owner works."""
    from . import dashboard, holepunch, swarm
    providers = swarm.Providers()   # the tracker: which reachable nodes hold which files
    rendezvous = holepunch.Rendezvous()  # sealed offers and answers of agents punching through their routers
    manifests: dict[str, bytes] = {}
    dashboard.REBUILD_EVERY_S = 3.0  # a public node's pages may lag new events by a few seconds, never more
    recent_posts: dict[str, list[float]] = {}
    rate_lock = threading.Lock()
    waiters = threading.BoundedSemaphore(int(store.config().get("max_waiters", MAX_WAITERS)))

    def push_allowed(author: str) -> bool:
        policy = store.config().get("accept_push", False)
        return author != agent_id and (policy is True or (isinstance(policy, list) and author in policy))

    def rate_ok(client: str, nbytes: int = 0) -> bool:
        """At most push_rate_per_min requests and push_bytes_per_min bytes from one client in any minute."""
        now = time.time()
        cfg = store.config()
        with rate_lock:
            if len(recent_posts) > 10_000:  # thousands of agents: forget clients that went quiet
                for key in [k for k, ts in recent_posts.items() if not ts or now - ts[-1][0] >= 60]:
                    del recent_posts[key]
            window = [(t, n) for t, n in recent_posts.get(client, []) if now - t < 60]
            window.append((now, nbytes))
            recent_posts[client] = window[-500:]
        return (len(window) <= int(cfg.get("push_rate_per_min", 120))
                and sum(n for _, n in window) <= int(cfg.get("push_bytes_per_min", 64 << 20)))

    class Handler(BaseHTTPRequestHandler):
        server_version = f"humansout/{__version__}"
        sys_version = ""
        timeout = 15  # drop clients that hold a connection open without sending

        def log_message(self, fmt, *args):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json", csp: str | None = None,
                  headers: dict | None = None):
            self.send_response(code)
            if code != 304:  # a 304's headers update a cache's stored copy: no length or type of its own
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            if csp:
                self.send_header("Content-Security-Policy", csp)
                self.send_header("Cache-Control", "no-store")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if code != 304:
                self.wfile.write(body)

        def _send_tagged(self, body: bytes, etag: str, cache: str = LISTING_CACHE, ctype: str = "application/json",
                         extra: dict | None = None):
            """A response with an ETag: a client that already has this version gets an empty 304."""
            headers = {"ETag": etag, "Cache-Control": cache, **(extra or {})}
            if _etag_matches(self.headers.get("If-None-Match"), etag):
                return self._send(304, b"", ctype, headers=headers)
            return self._send(200, body, ctype, headers=headers)

        def _wait(self, query):
            """Long-poll: answer when the feed list (or one mission's index) changes, or 304 after `timeout`."""
            mission = query.get("mission", [None])[0]
            if mission is not None and not PUB_RE.match(mission):
                return self._send(400, b'{"error":"bad mission id"}')

            def current():
                return (store.mission_feeds_view(mission) if mission else store.feeds_view())[1]
            if mission and current() is None:
                return self._send(404, b'{"error":"no feed mentions this mission"}')
            since = '"' + query.get("since", [""])[0].strip().removeprefix("W/").strip('"') + '"'
            timeout = float(query.get("timeout", [WAIT_DEFAULT_S])[0])
            if not math.isfinite(timeout):  # nan would never time out: a waiting slot held for ever
                return self._send(400, b'{"error":"bad timeout"}')
            timeout = min(max(timeout, 0), WAIT_MAX_S)
            if not waiters.acquire(blocking=False):
                return self._send(429, b'{"error":"too many waiting clients; poll instead"}',
                                  headers={"Retry-After": "30", "Cache-Control": "no-store"})
            try:
                etag = store.wait_for_change(current, since, timeout)
            finally:
                waiters.release()
            headers = {"ETag": etag, "Cache-Control": "no-store"}
            if _etag_matches(since, etag):
                return self._send(304, b"", headers=headers)
            return self._send(200, json.dumps({"etag": etag}).encode(), headers=headers)

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
                view = dashboard.overview(store, agent_id)
                if not ((show_turns and _known_host(self, store)) or _local_viewer(self)):
                    view.update(watch=None, sync=None)
                self._send(200, json.dumps(dict(view, turns_enabled=show_turns)).encode())
                return True
            if parts[:2] in (["api", "turns"], ["api", "turn"]):
                # agent turns hold prompts, outputs and local paths: private unless the owner opts in
                if not (show_turns and _known_host(self, store)):  # never to a rebound page
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
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

            def arg(name, default="", cast=str):
                try:
                    return cast(query.get(name, [default])[0][:200])
                except (TypeError, ValueError):
                    return cast(default)
            if parts == ["api", "home"]:
                view = dashboard.home(store, agent_id)
                if not ((show_turns and _known_host(self, store)) or _local_viewer(self)) and isinstance(view.get("sync"), dict):
                    view["sync"] = {"last_ms": view["sync"].get("last_ms")}  # not which peers failed, or their addresses
                self._send(200, json.dumps(view).encode())
                return True
            if parts == ["api", "missions"]:
                body = dashboard.missions(store, q=arg("q"), domain=arg("domain"), status=arg("status"),
                                          sort=arg("sort", "activity"), offset=arg("offset", "0", int),
                                          limit=arg("limit", "50", int))
                self._send(200, json.dumps(body).encode())
                return True
            if parts == ["api", "stream"]:
                mission = arg("mission") or None
                if mission and not re.fullmatch(r"[0-9a-f]{64}", mission):
                    mission = None
                body = dashboard.stream(store, after=arg("after", "0", int), limit=arg("limit", "100", int), mission=mission)
                self._send(200, json.dumps(body).encode())
                return True
            if parts == ["api", "docket"]:
                self._send(200, json.dumps(dashboard.docket(store)).encode())
                return True
            if parts == ["api", "search"]:
                self._send(200, json.dumps(dashboard.search(store, arg("q"))).encode())
                return True
            if len(parts) == 3 and parts[:2] == ["api", "mission"] and re.fullmatch(r"[0-9a-f]{64}", parts[2]):
                view = dashboard.mission_detail(store, parts[2], log_limit=arg("log", "300", int))
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
                    return self._send_tagged(*store.feeds_view())
                if parts == ["missions"]:
                    return self._send_tagged(*store.missions_view(query.get("topic", [None])[0]))
                if len(parts) == 3 and parts[0] == "missions" and parts[2] == "feeds" and PUB_RE.match(parts[1]):
                    body, etag = store.mission_feeds_view(parts[1])
                    if body is None:
                        return self._send(404, b'{"error":"no feed mentions this mission"}')
                    return self._send_tagged(body, etag)
                if parts == ["wait"]:
                    return self._wait(query)
                if len(parts) == 2 and parts[0] == "feed" and PUB_RE.match(parts[1]):
                    feed = store.load_feed(parts[1])
                    start = max(int(query.get("from", ["0"])[0]), 0)
                    end = int(query["to"][0]) if "to" in query else None
                    if end is not None and end < 0:
                        raise ValueError("negative range end")
                    events = feed[start:end]
                    lines = "".join(json.dumps({k: v for k, v in e.items() if k != "id"}) + "\n" for e in events)
                    # Feeds only grow and every event is signed, so a complete range never changes: a CDN may keep it.
                    complete = end is not None and start < end <= len(feed)
                    return self._send(200, lines.encode(), "application/x-ndjson",
                                      headers={"Cache-Control": IMMUTABLE if complete else "no-cache"})
                if len(parts) == 2 and parts[0] == "blob" and CID_RE.match(parts[1]):
                    # Content-addressed: the id is the ETag and the bytes never change, so edges serve repeats.
                    headers = {"ETag": f'"{parts[1]}"', "Cache-Control": IMMUTABLE, "Accept-Ranges": "bytes"}
                    if _etag_matches(self.headers.get("If-None-Match"), parts[1]) and store.has_blob(parts[1]):
                        return self._send(304, b"", "application/octet-stream", headers=headers)
                    wanted = re.fullmatch(r"bytes=(\d{1,15})-(\d{0,15})", (self.headers.get("Range") or "").strip())
                    if wanted:  # one piece of the file, for downloads from several sources at once
                        start = int(wanted.group(1))
                        piece = store.read_blob_range(parts[1], start, int(wanted.group(2)) if wanted.group(2) else None)
                        if piece is None:
                            return self._send(404, b'{"error":"not found"}', headers={"Cache-Control": "no-cache"})
                        data, size = piece
                        if not data:
                            return self._send(416, b"", headers={"Content-Range": f"bytes */{size}"})
                        return self._send(206, data, "application/octet-stream", headers={
                            **headers, "Content-Range": f"bytes {start}-{start + len(data) - 1}/{size}"})
                    data = store.get_blob(parts[1])
                    if data is None:
                        return self._send(404, b'{"error":"not found"}', headers={"Cache-Control": "no-cache"})
                    return self._send(200, data, "application/octet-stream", headers=headers)
                if len(parts) == 2 and parts[0] == "manifest" and CID_RE.match(parts[1]):
                    if parts[1] not in manifests:
                        data = store.get_blob(parts[1])
                        if data is None:
                            return self._send(404, b'{"error":"not found"}', headers={"Cache-Control": "no-cache"})
                        if len(manifests) > 2000:
                            manifests.clear()
                        manifests[parts[1]] = json.dumps(swarm.manifest(data)).encode()
                    return self._send(200, manifests[parts[1]], headers={"Cache-Control": IMMUTABLE})
                if len(parts) == 2 and parts[0] == "rendezvous" and PUB_RE.match(parts[1]) and not seed_only:
                    after = int(query.get("after", ["0"])[0])
                    wait = float(query.get("wait", ["0"])[0])
                    if not math.isfinite(wait):
                        return self._send(400, b'{"error":"bad wait"}')
                    if wait > 0 and not waiters.acquire(blocking=False):
                        wait = 0  # too many waiting clients: answer at once
                    try:
                        found = rendezvous.get(parts[1], after, wait)
                    finally:
                        if wait > 0:
                            waiters.release()
                    return self._send(200, json.dumps(found).encode(), headers={"Cache-Control": "no-store"})
                if len(parts) == 2 and parts[0] == "providers" and CID_RE.match(parts[1]):
                    body = json.dumps(providers.get(parts[1], int(time.time() * 1000))).encode()
                    return self._send(200, body, headers={"Cache-Control": "no-cache"})
                if parts == ["peers"]:
                    lan = store.config().get("allow_private_peers", False)  # a test or LAN network shares them
                    peers = [p for p in store.peers() if lan or _shareable(p)]  # else never this node's local network
                    own = normalize_peer(store.config().get("public_url") or "")
                    if own:
                        peers = [own] + [p for p in peers if p != own]
                    body = json.dumps(peers).encode()
                    return self._send_tagged(body, etag_for(body))
                if parts == []:
                    if not seed_only and "text/html" in (self.headers.get("Accept") or ""):
                        return self._dashboard(parts)  # a browser gets the dashboard
                    features = (FEATURES + (["witness"] if agent_id and store.config().get("witness") else [])
                                + (["rendezvous"] if store.config().get("accept_push") and not seed_only else []))
                    body = json.dumps({"service": "humansout-node", "version": __version__, "features": features,
                                       **({"agent": agent_id} if agent_id else {})}).encode()
                    # the same address serves browsers the dashboard: a shared cache must not keep either one
                    return self._send_tagged(body, etag_for(body), "no-cache", extra={"Vary": "Accept"})
                if not seed_only and parts[0] in ("dashboard", "dashboard.js", "api", "badge") and self._dashboard(parts):
                    return None
            except (ValueError, OSError):
                return self._send(400, b'{"error":"bad request"}')
            return self._send(404, b'{"error":"not found"}')

        def do_POST(self):
            """Opt-in push (accept_push): signed events that continue a feed, and blobs some event refers to.
            Nothing can be forged: every event is signature-checked against its author's key."""
            if seed_only or not store.config().get("accept_push"):
                return self._send(405, b'{"error":"read-only node"}')
            parts = [p for p in urllib.parse.urlparse(self.path).path.split("/") if p]
            # a body is read only up to the length it declares, and only a length within the limits: without
            # this a negative or missing length makes the server read until the client stops sending
            raw_length = self.headers.get("Content-Length")
            if raw_length is None or self.headers.get("Transfer-Encoding"):
                return self._send(411, b'{"error":"a Content-Length is required"}')
            try:
                length = int(raw_length)
            except ValueError:
                return self._send(400, b'{"error":"bad length"}')
            if length < 0:
                return self._send(400, b'{"error":"bad length"}')
            if not rate_ok(_client_key(self), length):
                return self._send(429, b'{"error":"too many requests"}', headers={"Retry-After": "60"})
            if not store.room_for(length):
                return self._send(507, b'{"error":"this node is low on disk space"}', headers={"Retry-After": "3600"})
            if parts == ["providers"]:
                return self._providers(length)
            if parts == ["rendezvous"]:
                if length > 16 << 10:
                    return self._send(413, b'{"error":"too large"}')
                try:
                    envelope = json.loads(self.rfile.read(length))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return self._send(400, b'{"error":"bad envelope"}')
                now = int(time.time() * 1000)
                if not holepunch.envelope_ok(envelope) or abs(now - envelope["ts"]) > holepunch.OFFER_TTL_MS:
                    return self._send(400, b'{"error":"bad or stale envelope"}')
                rendezvous.add(envelope, now)
                return self._send(200, b'{"carried":true}')
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
                from .messages import MAX_PAYLOAD_BYTES, read_payload
                if not store.is_referenced(parts[1]):
                    return self._send(403, b'{"error":"no event refers to this blob"}')
                payload = store.payload_only(parts[1])  # only a message names it: a small sealed box, nothing else
                limit = MAX_PAYLOAD_BYTES if payload else int(store.config().get("max_blob_bytes", 50 * 1024 * 1024))
                if length > limit:
                    return self._send(413, b'{"error":"too large"}')
                data = self.rfile.read(length)
                if cid_for(data) != parts[1]:
                    return self._send(400, b'{"error":"content does not match its id"}')
                if payload and read_payload(data) is None:
                    return self._send(400, b'{"error":"a message payload must be a sealed box"}')
                store.put_blob(data)
                return self._send(200, b'{"stored":true}')
            return self._send(404, b'{"error":"not found"}')

        def _providers(self, length: int):
            """The tracker: a reachable node says which files it holds. Listed only after this hub has seen that
            the address answers as that agent (nobody can point the swarm at someone else's server), only for
            files of this network, and never for private or local addresses."""
            if length > 1 << 20:
                return self._send(413, b'{"error":"too large"}')
            try:
                a = json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return self._send(400, b'{"error":"bad announcement"}')
            now = int(time.time() * 1000)
            if not swarm.announcement_ok(a) or abs(now - a["ts"]) > 600_000:
                return self._send(400, b'{"error":"bad or stale announcement"}')
            url = normalize_peer(a["url"])
            if not url or (not store.config().get("providers_allow_private") and not public_host(url)):
                return self._send(403, b'{"error":"only public addresses can seed"}')
            checked = providers.verified.get(url)
            if not checked or checked[0] != a["agent"] or now - checked[1] > 3_600_000:
                try:  # guarded: the address is checked again on the socket (a DNS name can change after the check)
                    root = json.loads(_get(f"{url}/", 64 * 1024, timeout=8,
                                           public_only=not store.config().get("providers_allow_private")))
                except (urllib.error.URLError, OSError, ValueError):
                    return self._send(400, b'{"error":"the address does not answer"}')
                if not isinstance(root, dict) or root.get("agent") != a["agent"]:
                    return self._send(403, b'{"error":"the address does not serve this agent"}')
                providers.verify(url, a["agent"], now)
            held = [c for c in a["cids"] if store.is_referenced(c)]
            providers.add(url, held, now)
            return self._send(200, json.dumps({"listed": len(held)}).encode())

        def do_HEAD(self):
            """The size of a file, before downloading it in pieces."""
            parts = [p for p in urllib.parse.urlparse(self.path).path.split("/") if p]
            if len(parts) == 2 and parts[0] == "blob" and CID_RE.match(parts[1]):
                path = store.blob_path(parts[1])
                if path.exists():
                    self.send_response(200)
                    for name, value in (("Content-Type", "application/octet-stream"), ("Content-Length", str(path.stat().st_size)),
                                        ("Accept-Ranges", "bytes"), ("ETag", f'"{parts[1]}"'), ("Cache-Control", IMMUTABLE)):
                        self.send_header(name, value)
                    self.end_headers()
                    return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_PUT(self):
            self._send(405, b'{"error":"read-only node"}')

        do_DELETE = do_PUT

    return Handler


class _Server(ThreadingHTTPServer):
    """One thread per connection, but a bounded number of them: past `max_connections` a client gets an
    immediate 503 instead of one more thread (slow or idle clients cannot exhaust the node)."""
    request_queue_size = 512  # listen backlog: the default 5 drops connections when many agents sync at once

    def __init__(self, address, handler, max_connections: int = 256):
        self._slots = threading.BoundedSemaphore(max(8, max_connections))
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nRetry-After: 5\r\nContent-Length: 0\r\n"
                                b"Connection: close\r\n\r\n")
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def serve(store: Store, host: str = "127.0.0.1", port: int = 7400, background: bool = False,
          agent_id: str | None = None, on_ready=None, show_turns: bool = False, seed_only: bool = False):
    try:
        server = _Server((host, port), make_handler(store, agent_id, show_turns, seed_only),
                         int(store.config().get("max_connections", 256)))
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
    now = node.clock()
    for m in world.messages:  # the sealed payloads of this agent's own messages, while they live
        if node.agent_id in (m["from"], m["to"]) and m["expires"] > now:
            wanted.add(m["box"] if m["to"] == node.agent_id else m["self"])
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


def mirrors_blobs(store: Store) -> bool:
    """Hubs keep every referenced blob (so a second hub can serve all of the first one's files); agents only their own."""
    cfg = store.config()
    if cfg.get("mirror_blobs") is not None:
        return bool(cfg["mirror_blobs"])
    return bool(cfg.get("accept_push") or cfg.get("public_url"))


def _dedupe(urls) -> list[str]:
    out = []
    for url in urls:
        url = normalize_peer(url) if isinstance(url, str) else None
        if url and url not in out:
            out.append(url)
    return out


def fetch_blob(store: Store, cid: str, peers: list[str] | None = None) -> bytes | None:
    """Fetch a blob from every node that has it: the providers hubs know of, then the given and known peers.
    Big files come in pieces from several sources at once (humansout/swarm.py). Any source will do: every piece
    and the whole file are checked against their hashes."""
    from . import swarm
    from .messages import MAX_PAYLOAD_BYTES, read_payload
    payload = store.payload_only(cid)  # a message's sealed box: small, and nothing else is accepted under its id
    limit = MAX_PAYLOAD_BYTES if payload else int(store.config().get("max_blob_bytes", 50 * 1024 * 1024))
    if not store.room_for(limit if payload else 0):
        return None
    sources, guarded = swarm.sources_for(store, cid, _dedupe(list(peers or []) + store.peers()))
    data, _report = swarm.download(cid, sources, limit, guarded=guarded)
    if data is None or (payload and read_payload(data) is None) or not store.room_for(len(data)):
        return None
    store.put_blob(data)
    return data


class _PeerCache:
    """What each peer last answered to listing requests, with ETags, and when we last asked (netcache.json)."""

    def __init__(self, store: Store, conditional: bool):
        self.store, self.conditional = store, conditional
        self.data = store.netcache()
        self.lock = threading.Lock()
        self.learned = {p for p in store.peers() if store.is_learned(p)}  # reached only at public addresses

    def get_json(self, peer: str, path: str):
        """The peer's JSON answer for `path`, or None if it does not know it (404)."""
        with self.lock:
            cached = (self.data.get("peers", {}).get(peer) or {}).get(path)
        etag = cached.get("etag") if cached and self.conditional else None
        status, body, etag = _get_conditional(f"{peer}{path}", etag, public_only=peer in self.learned)
        if status == 404:
            return None
        if status == 304 and cached:
            return cached["body"]
        value = json.loads(body)
        if self.conditional and etag:
            with self.lock:
                self.data.setdefault("peers", {}).setdefault(peer, {})[path] = {"etag": etag, "body": value}
        return value

    def due(self, key: str, peer: str, every_s: float) -> bool:
        """True (and noted) if `key` was last done for this peer more than `every_s` seconds ago."""
        now = time.time()
        with self.lock:
            last = self.data.setdefault(key, {}).get(peer, 0)
            if now - last < every_s:
                return False
            self.data[key][peer] = now
            return True

    def note(self, key: str, peer: str, value) -> None:
        with self.lock:
            self.data.setdefault(key, {})[peer] = value

    def save(self, keep: list[str]) -> None:
        known = set(keep)
        for key in ("peers", "own_length", "features", "features_at", "pex_at"):  # forget peers we no longer know
            self.data[key] = {p: v for p, v in self.data.get(key, {}).items() if p in known}
        self.store.save_netcache(self.data)


PEX_EVERY_S = 600        # peer exchange: ask a peer for its peers at most this often
FEATURES_EVERY_S = 3600  # how often to re-ask a peer what it supports


def sync_scope(node, missions=None, topics=None) -> dict | None:
    """Which missions to sync: the ones asked for (by id or a prefix of at least 6 characters), those in the topics,
    and the ones this agent already takes part in. None means everything (the default, and what hubs do)."""
    cfg = node.store.config()
    missions = [m for m in (missions or cfg.get("sync_missions") or []) if isinstance(m, str) and m.strip()]
    topics = [t.strip() for t in (topics or cfg.get("sync_topics") or []) if isinstance(t, str) and t.strip()]
    if not missions and not topics:
        return None
    full, prefixes = set(), set()
    for m in (m.strip().lower() for m in missions):
        if PUB_RE.match(m):
            full.add(m)
        elif re.fullmatch(r"[0-9a-f]{6,63}", m):
            prefixes.add(m)
        else:
            raise HumansOutError(f"{m!r} is not a mission id (64 hex characters, or a prefix of at least 6)")
    full |= node.store.missions_of(node.agent_id)
    return {"missions": full, "prefixes": prefixes, "topics": sorted(set(topics))}


def _lengths(answer) -> dict[str, int]:
    if not isinstance(answer, dict):
        raise ValueError("peer sent a malformed feed list")
    return {a: n for a, n in answer.items() if isinstance(a, str) and PUB_RE.match(a) and isinstance(n, int) and n >= 0}


def _features(cache: _PeerCache, peer: str) -> list:
    if cache.due("features_at", peer, FEATURES_EVERY_S) or peer not in cache.data.get("features", {}):
        info = cache.get_json(peer, "/")
        cache.note("features", peer, (info.get("features") if isinstance(info, dict) else None) or [])
    return cache.data["features"][peer]


def _survey(cache: _PeerCache, peer: str, scope: dict | None) -> dict[str, int]:
    """Ask one peer what it has: {author: length} for every feed, or only for the feeds in the scoped missions.
    Peers without mission indexes (older nodes) are asked for everything."""
    if scope is None or "missions" not in _features(cache, peer):
        return _lengths(cache.get_json(peer, "/feeds"))
    wanted = set(scope["missions"])
    for path in [f"/missions?topic={urllib.parse.quote(t)}" for t in scope["topics"]] + (["/missions"] if scope["prefixes"] else []):
        listing = cache.get_json(peer, path) or {}
        if not isinstance(listing, dict):
            raise ValueError("peer sent a malformed mission list")
        wanted |= {m for m in listing if path != "/missions" or any(m.startswith(p) for p in scope["prefixes"])}
    remote: dict[str, int] = {}
    for mid in sorted(m for m in wanted if PUB_RE.match(m)):
        for author, length in _lengths(cache.get_json(peer, f"/missions/{mid}/feeds") or {}).items():
            remote[author] = max(length, remote.get(author, 0))
    return remote


def _push_own(node, peer: str, their_length, cache: _PeerCache | None = None) -> dict:
    """Send our own new events, then the blobs they refer to, to a peer that accepts pushes."""
    store = node.store
    mine = store.load_feed(node.agent_id)
    their_length = their_length if isinstance(their_length, int) and their_length >= 0 else 0
    out = {"pushed_events": 0, "pushed_blobs": 0}
    guarded = peer in cache.learned if cache is not None else store.is_learned(peer)
    for _attempt in range(2):
        if len(mine) <= their_length:
            break
        body = "".join(json.dumps({k: v for k, v in e.items() if k != "id"}) + "\n" for e in mine[their_length:])
        status, reply = _post(f"{peer}/feed/{node.agent_id}", body.encode(), "application/x-ndjson",
                              public_only=guarded)
        if status != 200:
            return out  # a read-only or unwilling peer: nothing more to do there
        answer = json.loads(reply)
        out["pushed_events"] += answer.get("accepted", 0)
        length = answer.get("length")
        if cache is not None and isinstance(length, int):
            cache.note("own_length", peer, length)
        if not isinstance(length, int) or length >= len(mine) or length >= their_length:
            break
        their_length = length  # the peer has less than we thought (a stale cache): send from where it is
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
        status, _ = _post(f"{peer}/blob/{cid}", data, "application/octet-stream", public_only=guarded)
        if status == 200:
            done.add(cid)
            out["pushed_blobs"] += 1
        elif status == 405:
            break
    with store._lock:  # peers are synced concurrently; pushed.json holds all of them
        try:
            pushed = json.loads(pushed_path.read_text()) if pushed_path.exists() else {}
        except json.JSONDecodeError:
            pushed = {}
        pushed[peer] = sorted(done)[-5000:]
        pushed_path.write_text(json.dumps(pushed))
    return out


def sync(node, peers: list[str] | None = None, missions: list[str] | None = None, topics: list[str] | None = None,
         conditional: bool = True, workers: int | None = None) -> dict:
    """Pull new events and needed blobs from peers, and push our own to peers that accept pushes.

    All peers are asked at once; each feed is pulled from the peer that has the most of it (another one if that
    fails). With `missions` or `topics` (or the config keys sync_missions / sync_topics) only the feeds of those
    missions and of the missions this agent is in are pulled. `conditional` sends the ETags peers gave last time."""
    store = node.store
    for p in peers or []:
        store.add_peer(p)
    listed = _dedupe(peers or store.peers())
    if not peers:  # the owner's peers first, then learned ones: peer exchange cannot crowd the owner's hubs out
        learned = set(store._learned())
        listed = sorted(listed, key=lambda p: p in learned)
    targets = listed[:int(store.config().get("max_peers_per_sync", MAX_PEERS_PER_SYNC))]
    scope = sync_scope(node, missions, topics)
    workers = max(1, int(workers or store.config().get("sync_workers", SYNC_WORKERS)))
    cache = _PeerCache(store, conditional)
    report = {"peers": {p: {"ok": False, "events": 0} for p in targets}, "new_events": 0, "new_blobs": 0,
              "learned_peers": [], "pushed_events": 0, "pushed_blobs": 0}
    if scope is not None:
        report["scope"] = {"missions": len(scope["missions"]), "topics": scope["topics"]}
    lock = threading.Lock()

    def survey(peer):
        entry = report["peers"][peer]
        try:
            remote = _survey(cache, peer, scope)
            if cache.due("pex_at", peer, PEX_EVERY_S):
                learned_list = cache.get_json(peer, "/peers")
                for learned in (learned_list if isinstance(learned_list, list) else [])[:50]:
                    if isinstance(learned, str) and store.add_peer(learned, learned=True):
                        with lock:
                            report["learned_peers"].append(learned)
            entry["ok"] = True
            return remote
        except Exception as e:  # noqa: BLE001 - one broken or hostile peer must not stop the sync with the others
            entry["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            return None

    def pull(author, sources):
        for length, peer in sources:  # most complete peer first
            have = store.feed_len(author)
            if have >= length:
                return
            try:
                span = f"from={have}" + (f"&to={length}" if length < 1 << 30 else "")
                raw = _get(f"{peer}/feed/{author}?{span}", public_only=peer in cache.learned).decode("utf-8")
                if not store.room_for(len(raw)):
                    return  # the disk is nearly full: nothing more from the network
                got = store.ingest(author, [json.loads(line) for line in raw.splitlines() if line.strip()])
            except Exception:  # noqa: BLE001, S112 - try the next peer that has this feed
                continue
            with lock:
                report["peers"][peer]["events"] += got

    def push(peer, remote):
        own = remote.get(node.agent_id)
        if own is None:  # a mission-scoped answer may not list us: use what the peer told us after our last push
            own = (cache.data.get("own_length") or {}).get(peer, 0)
        try:
            report["peers"][peer].update(_push_own(node, peer, own, cache))
        except Exception as e:  # noqa: BLE001
            report["peers"][peer]["push_error"] = f"{type(e).__name__}: {str(e)[:200]}"

    with ThreadPoolExecutor(max_workers=min(workers, max(1, len(targets)))) as pool:
        surveys = dict(zip(targets, pool.map(survey, targets), strict=True))
    witnesses = [w for w in store.config().get("time_witnesses") or [] if isinstance(w, str) and PUB_RE.match(w)]
    for remote in surveys.values():  # a mission-scoped survey does not list the witnesses' feeds: ask for them
        if remote is not None and scope is not None:
            for w in witnesses:
                remote.setdefault(w, 1 << 30)  # "as much as it has": pull asks from what we hold, open-ended
    sources: dict[str, list] = {}
    for peer, remote in surveys.items():
        for author, length in (remote or {}).items():
            if author != node.agent_id:  # never accept events for our own feed from outside
                sources.setdefault(author, []).append((length, peer))
    todo = [(a, sorted(s, key=lambda x: -x[0])) for a, s in sources.items() if max(s)[0] > store.feed_len(a)]
    # a peer can list any number of made-up feeds: one sync pulls a bounded number, the next one goes on
    todo = todo[:int(store.config().get("max_feeds_per_sync", 20_000))]
    ok_peers = [p for p, remote in surveys.items() if remote is not None]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda job: pull(*job), todo))
        list(pool.map(lambda p: push(p, surveys[p]), ok_peers))
    for entry in report["peers"].values():
        report["new_events"] += entry["events"]
        report["pushed_events"] += entry.get("pushed_events", 0)
        report["pushed_blobs"] += entry.get("pushed_blobs", 0)

    wanted = _wanted_blobs(node)
    if mirrors_blobs(store):
        wanted |= {c for c in store.referenced_cids() if not store.has_blob(c)}
    now_s = time.time()
    misses = {c: t for c, t in (cache.data.get("blob_misses") or {}).items() if now_s - t < BLOB_RETRY_S}
    wanted = sorted(c for c in wanted if c not in misses)
    if wanted:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for cid, data in zip(wanted, pool.map(lambda c: fetch_blob(store, c, ok_peers), wanted), strict=True):
                if data is not None:
                    report["new_blobs"] += 1
                else:
                    misses[cid] = now_s
    cache.data["blob_misses"] = dict(sorted(misses.items(), key=lambda kv: kv[1])[-5000:])
    cache.save(_dedupe(targets + store.peers()))
    status = {"last_ms": node.clock(), "new_events": report["new_events"], "new_blobs": report["new_blobs"],
              "peers": {p: ("ok" if e["ok"] else e.get("error", "failed")[:120]) for p, e in report["peers"].items()}}
    (store.home / "sync.json").write_text(json.dumps(status))  # for the dashboard: when this node last heard from peers
    return report


def wait_for_change(peer: str, etag: str | None = None, mission: str | None = None,
                    timeout: float = WAIT_DEFAULT_S) -> str | None:
    """Long-poll a peer until its feed list (or one mission's index) differs from `etag`. Returns the new ETag,
    or None if nothing changed in `timeout` seconds or the peer cannot long-poll (then poll as usual)."""
    query = {"since": etag or "", "timeout": str(timeout), **({"mission": mission} if mission else {})}
    status, body, _ = _get_conditional(f"{peer}/wait?{urllib.parse.urlencode(query)}", None, timeout=timeout + TIMEOUT)
    if status != 200:
        return None
    answer = json.loads(body)
    return answer.get("etag") if isinstance(answer, dict) else None
