"""Hole punching: two agents behind home routers that forward nothing reach each other directly, over UDP.

The way WebRTC and torrent clients do it, with no setting to change on either network:

1. Each side asks public STUN servers how its UDP socket looks from outside (address and port). Most home routers
   keep one outside port per socket whatever the destination ("endpoint-independent mapping"); carrier-grade and
   some mobile NATs give a new port per destination, and then punching usually fails.
2. They exchange those endpoints through a hub (POST and GET /rendezvous), sealed to each other with their
   messaging keys and signed with their identities: the hub carries the offer and the answer, cannot read them
   and cannot forge them. It learns only who wants to reach whom.
3. Both send encrypted probes to each other's endpoints at the same time. Each router sees its own side's packet
   go out first, so it lets the other side's packets in: the hole is punched.
4. Over that path a small request/response protocol serves the read-only protocol routes (feeds, missions,
   files). Every datagram is encrypted and authenticated (ChaCha20-Poly1305, keys from an X25519 exchange of
   ephemeral keys carried in the signed offer and answer), counters stop replays, and a node answers only frames
   of a session it agreed to, so its socket can never be used to send data to a forged address.
5. `sync` runs unchanged through a loopback HTTP proxy to the punched path.

If punching fails (both sides behind NATs that change ports), nothing is lost: the agents keep reaching each
other through hubs, as before.
"""

from __future__ import annotations

import ipaddress
import json
import os
import queue
import random
import selectors
import socket
import struct
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .core import HumansOutError, canonical, verify_signature
from .store import CID_RE, PUB_RE

STUN_SERVERS = (("stun.l.google.com", 19302), ("stun.cloudflare.com", 3478))
MAGIC = 0x2112A442
INFO = b"humansout punch v1"
OFFER_TTL_MS = 120_000
CHUNK = 1150                  # data per datagram: with headers under 1200 bytes, safe on every path
MAX_RESPONSE = 50 << 20      # the default max_blob_bytes
MAX_ANSWERING, MAX_CACHED = 4, 4  # requests answered at once, and answers kept for retransmission
PUNCH_S, IDLE_S = 10.0, 300.0
OPEN_TTLS, OPEN_WAIT_S = (2,), 0.5  # first probes: past this side's router, never as far as the other side's
KEEPALIVE_S = 2.0
SPRAY_PORTS, MANY_SOCKETS = 1024, 512  # one side behind a NAT that maps per destination: 1 - (1 - 1024/64512)^512
                                       # = 99.97% that one of the sockets' ports is among the sprayed ones
HELLO, HELLO_ACK, REQ, RES, NACK, DONE, BYE = 0x01, 0x02, 0x10, 0x11, 0x12, 0x13, 0x20


# ---- 1. STUN: how this socket looks from outside -------------------------------------------------------------
def stun_request(sock: socket.socket, server: tuple[str, int], timeout: float = 2.0) -> tuple[str, int] | None:
    """One RFC 5389 Binding request; the (address, port) the server saw, or None."""
    tid = os.urandom(12)
    sock.sendto(struct.pack("!HHI", 0x0001, 0, MAGIC) + tid, server)
    deadline = time.monotonic() + timeout
    while (left := deadline - time.monotonic()) > 0:
        sock.settimeout(left)
        try:
            data, _ = sock.recvfrom(2048)
        except (socket.timeout, OSError):
            return None
        if len(data) >= 20 and data[8:20] == tid and data[:2] == b"\x01\x01":
            return _mapped(data)
    return None


def _mapped(data: bytes) -> tuple[str, int] | None:
    i, plain = 20, None
    while i + 4 <= len(data):
        kind, length = struct.unpack("!HH", data[i:i + 4])
        value = data[i + 4:i + 4 + length]
        if kind == 0x0020 and len(value) >= 8 and value[1] == 1:  # XOR-MAPPED-ADDRESS, IPv4
            port = struct.unpack("!H", value[2:4])[0] ^ (MAGIC >> 16)
            return socket.inet_ntoa(bytes(a ^ b for a, b in zip(value[4:8], struct.pack("!I", MAGIC), strict=True))), port
        if kind == 0x0001 and len(value) >= 8 and value[1] == 1 and plain is None:  # MAPPED-ADDRESS
            plain = socket.inet_ntoa(value[4:8]), struct.unpack("!H", value[2:4])[0]
        i += 4 + length + (-length % 4)
    return plain


def probe(sock: socket.socket, servers=None) -> dict:
    """This socket's outside endpoints and the router's mapping behaviour, from up to two STUN servers."""
    seen = []
    for host, port in servers or STUN_SERVERS:
        try:
            address = (socket.gethostbyname(host), port)
        except OSError:
            continue
        got = stun_request(sock, address)
        if got:
            seen.append(got)
    mapping = ("unknown" if len(seen) < 2 else
               "endpoint-independent" if len(set(seen)) == 1 else "endpoint-dependent")
    return {"endpoints": sorted(set(seen)), "mapping": mapping, "answers": len(seen)}


def local_endpoints(sock: socket.socket) -> list[tuple[str, int]]:
    """This machine's own address with the socket's port: two agents on one network reach each other there."""
    port = sock.getsockname()[1]
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packet is sent: this only picks the outgoing interface
            return [(s.getsockname()[0], port)]
    except OSError:
        return []


# ---- 2. offers and answers, sealed and signed, through a hub ---------------------------------------------------
def _envelope(identity, to: str, box: dict, ts: int) -> dict:
    body = {"from": identity.public_hex, "to": to, "box": box, "ts": ts}
    return dict(body, sig=identity.sign(canonical(body)))


def envelope_ok(e) -> bool:
    """What a hub checks before carrying an envelope: its shape, its sender's signature, its freshness."""
    from .messages import box_shape_ok
    if not isinstance(e, dict) or set(e) != {"from", "to", "box", "ts", "sig"}:
        return False
    if not (isinstance(e["from"], str) and PUB_RE.match(e["from"]) and isinstance(e["to"], str)
            and PUB_RE.match(e["to"]) and e["from"] != e["to"] and isinstance(e["ts"], int)
            and isinstance(e["sig"], str) and box_shape_ok(e["box"])):
        return False
    body = {k: e[k] for k in ("from", "to", "box", "ts")}
    return verify_signature(e["from"], e["sig"], canonical(body))


def _seal_inner(node, to: str, inner: dict) -> dict:
    from .messages import seal
    world = node.world()
    if to not in world.box_keys:
        raise HumansOutError("that agent has not published a messaging key (it needs HumansOut 0.4.1 or later)")
    inner = dict(inner, sig=node.identity.sign(canonical(inner)))
    return _envelope(node.identity, to, seal(canonical(inner), world.box_keys[to], node.agent_id, to), node.clock())


def _open_inner(node, envelope: dict) -> dict | None:
    """The offer or answer inside an envelope addressed to this node, if its sender really signed it."""
    from .messages import open_box
    if not envelope_ok(envelope) or envelope["to"] != node.agent_id:
        return None
    try:
        inner = json.loads(open_box(envelope["box"], node.identity, envelope["from"], node.agent_id))
    except (HumansOutError, ValueError):
        return None
    if not isinstance(inner, dict) or not isinstance(inner.get("sig"), str):
        return None
    body = {k: v for k, v in inner.items() if k != "sig"}
    if (body.get("from") != envelope["from"] or body.get("to") != node.agent_id
            or not verify_signature(envelope["from"], inner["sig"], canonical(body))
            or abs(node.clock() - int(body.get("ts", 0))) > OFFER_TTL_MS):
        return None
    try:
        body["endpoints"] = [(str(ipaddress.ip_address(h)), int(p)) for h, p in body.get("endpoints", [])[:8]
                             if 0 < int(p) < 65536]
        body["outside"] = [(str(ipaddress.ip_address(h)), int(p)) for h, p in body.get("outside", [])[:4]
                           if 0 < int(p) < 65536]
        if body.get("mapping") not in ("endpoint-independent", "endpoint-dependent", "unknown", None):
            return None
        body["eph"] = bytes.fromhex(body["eph"])
        body["nonce"] = bytes.fromhex(body["nonce"])
        body["session"] = bytes.fromhex(body["session"])
    except (KeyError, TypeError, ValueError):
        return None
    if len(body["eph"]) != 32 or len(body["nonce"]) != 16 or len(body["session"]) != 8:
        return None
    return body


def post_envelope(hub: str, envelope: dict) -> int:
    from .net import _post
    status, _ = _post(f"{hub.rstrip('/')}/rendezvous", json.dumps(envelope).encode(), "application/json")
    return status


def fetch_envelopes(hub: str, agent: str, after: int, wait: float = 20) -> list[tuple[int, dict]]:
    """(when the hub got it, envelope) for envelopes to `agent` the hub got after `after` (the hub's clock)."""
    from .net import TIMEOUT, _get
    query = urllib.parse.urlencode({"after": after, "wait": int(max(0, wait))})
    try:
        listed = json.loads(_get(f"{hub.rstrip('/')}/rendezvous/{agent}?{query}", 256 << 10, timeout=wait + TIMEOUT))
    except (OSError, ValueError):
        return []
    return [(x["at"], x["envelope"]) for x in listed if isinstance(x, dict) and isinstance(x.get("at"), int)
            and isinstance(x.get("envelope"), dict)] if isinstance(listed, list) else []


class Rendezvous:
    """A hub's in-memory mailbox of sealed offers and answers, for two minutes each, at most 16 per agent."""

    def __init__(self):
        self.by_agent: dict[str, list[tuple[int, dict]]] = {}
        self.changed = threading.Condition()

    def add(self, envelope: dict, now: int) -> None:
        with self.changed:
            box = [(t, e) for t, e in self.by_agent.get(envelope["to"], []) if now - t < OFFER_TTL_MS]
            self.by_agent[envelope["to"]] = (box + [(now, envelope)])[-16:]
            if len(self.by_agent) > 100_000:
                for agent in list(self.by_agent)[:20_000]:
                    del self.by_agent[agent]
            self.changed.notify_all()

    def get(self, agent: str, after: int, wait: float) -> list[dict]:
        deadline = time.monotonic() + max(0.0, min(wait, 25.0))
        with self.changed:
            while True:
                now = int(time.time() * 1000)
                found = [{"at": t, "envelope": e} for t, e in self.by_agent.get(agent, [])
                         if t > after and now - t < OFFER_TTL_MS]
                left = deadline - time.monotonic()
                if found or left <= 0:
                    return found
                self.changed.wait(left)


# ---- 3 and 4. the punched path -----------------------------------------------------------------------------------
def _keys(mine: X25519PrivateKey, theirs: bytes, offer_nonce: bytes, answer_nonce: bytes, session: bytes):
    shared = mine.exchange(X25519PublicKey.from_public_bytes(theirs))
    okm = HKDF(algorithm=hashes.SHA256(), length=64, salt=offer_nonce + answer_nonce, info=INFO + session).derive(shared)
    return okm[:32], okm[32:]  # offerer -> answerer, answerer -> offerer


class Link:
    """One punched path between two agents: both serve the read-only routes to each other and can ask."""

    def __init__(self, sock: socket.socket, session: bytes, send_key: bytes, recv_key: bytes,
                 candidates: list[tuple[str, int]], serve=None, extra_socks=(), spray: list[str] = ()):
        # `extra_socks`: more sockets probing from this side (a router that maps each socket to its own port);
        # `spray`: the other side's outside addresses, whose random ports this side opens (see punch)
        self.sock, self.session, self.candidates = sock, session, list(dict.fromkeys(candidates))
        self.socks, self.spray = [sock, *extra_socks], list(dict.fromkeys(spray))
        self.send_aead, self.recv_aead = ChaCha20Poly1305(send_key), ChaCha20Poly1305(recv_key)
        self.serve = serve  # path -> (status, body), or None to answer nothing
        self.peer: tuple[str, int] | None = None
        self.counter = 0
        self.seen: set[int] = set()
        self.lowest = 0
        self.lock = threading.Lock()
        self.connected = threading.Event()
        self.closed = threading.Event()
        self.last_heard = time.monotonic()
        self.waiting: dict[int, queue.Queue] = {}
        self.next_id = 1
        self.responses: dict[int, tuple[list[bytes], float]] = {}  # requests this side answered: chunks, when
        self.rate = 2 << 20  # bytes per second sent; halved on loss, raised after clean transfers
        self.lossy: set[int] = set()
        self.answering = threading.BoundedSemaphore(MAX_ANSWERING)
        self.stats = {"sent": 0, "received": 0, "resent": 0, "dropped": 0}
        self.last_sent = time.monotonic()
        threading.Thread(target=self._receive, daemon=True).start()
        threading.Thread(target=self._keepalive, daemon=True).start()

    def _keepalive(self) -> None:
        """A probe when the link has been quiet: routers forget idle UDP mappings after about 30 s, and after
        a punch from many sockets it tells the other side which socket is the one that stayed open."""
        while not self.closed.wait(KEEPALIVE_S / 2):
            if self.connected.is_set() and self.peer and time.monotonic() - self.last_sent >= KEEPALIVE_S:
                self._send(HELLO, os.urandom(8))

    # framing: session id (8) | counter (8) | ChaCha20-Poly1305(type + payload), the header as associated data
    def _send(self, kind: int, payload: bytes = b"", to: tuple[str, int] | None = None, via=None) -> None:
        with self.lock:
            self.counter += 1
            counter = self.counter
        header = self.session + struct.pack("!Q", counter)
        data = header + self.send_aead.encrypt(b"\0\0\0\0" + header[8:], bytes([kind]) + payload, header)
        try:
            (via or self.sock).sendto(data, to or self.peer)
            self.stats["sent"] += 1
            self.last_sent = time.monotonic()
        except (OSError, TypeError):
            pass

    def _open(self, data: bytes) -> bytes | None:
        if len(data) < 33 or data[:8] != self.session:
            return None
        counter = struct.unpack("!Q", data[8:16])[0]
        with self.lock:
            if counter <= self.lowest or counter in self.seen:
                return None  # a replay, or too old
        try:
            frame = self.recv_aead.decrypt(b"\0\0\0\0" + data[8:16], data[16:], data[:16])
        except InvalidTag:
            return None
        with self.lock:
            self.seen.add(counter)
            if len(self.seen) > 8192:
                self.lowest = sorted(self.seen)[-4096]
                self.seen = {c for c in self.seen if c > self.lowest}
        return frame or None

    def _receive(self) -> None:
        while not self.closed.is_set():
            socks = [s for s in self.socks if s.fileno() != -1]
            if not socks:
                break
            try:
                with selectors.DefaultSelector() as selector:
                    for s in socks:
                        selector.register(s, selectors.EVENT_READ)
                    ready = [key.fileobj for key, _ in selector.select(0.5)]
            except (OSError, ValueError):  # a socket closed meanwhile (punch keeps only the one that got through)
                continue
            for sock in ready:
                try:
                    data, addr = sock.recvfrom(2048)
                except (BlockingIOError, socket.timeout):
                    continue
                except OSError:
                    continue
                self._frame(sock, data, addr)

    def _frame(self, sock, data: bytes, addr) -> None:
        frame = self._open(data)
        if frame is None:
            self.stats["dropped"] += 1  # not of this session: never answered (nothing to reflect)
            return
        self.stats["received"] += 1
        self.last_heard = time.monotonic()
        if self.peer != addr or self.sock is not sock:
            # the socket and address its packets really come through (a NAT may have changed the port)
            self.peer, self.sock = addr, sock
        self.connected.set()
        kind, payload = frame[0], frame[1:]
        if kind == HELLO:
            self._send(HELLO_ACK, to=addr)
        elif kind == REQ and len(payload) >= 4 and self.answering.acquire(blocking=False):
            threading.Thread(target=self._answer, args=(payload,), daemon=True).start()  # else: it asks again
        elif kind == NACK and len(payload) >= 4:
            threading.Thread(target=self._resend, args=(payload,), daemon=True).start()  # never block receiving
        elif kind == DONE and len(payload) == 4:
            rid = struct.unpack("!I", payload)[0]
            if self.responses.pop(rid, None) is not None and rid not in self.lossy:
                self.rate = min(64 << 20, self.rate * 3 // 2)  # a clean transfer: a little faster next time
            self.lossy.discard(rid)
        elif kind == RES and len(payload) >= 12:
            rid = struct.unpack("!I", payload[:4])[0]
            if rid in self.waiting:
                self.waiting[rid].put(payload[4:])
        elif kind == BYE:
            self.closed.set()

    def _low_ttl(self, sends, ttl: int) -> None:
        """Send while every socket's IP TTL is `ttl`: the packets open this side's router, then expire."""
        before = {}
        for sock in self.socks:
            try:
                before[sock] = sock.getsockopt(socket.IPPROTO_IP, socket.IP_TTL)
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, ttl)
            except OSError:
                pass
        try:
            sends()
        finally:
            for sock, value in before.items():
                try:
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_TTL, value)
                except OSError:
                    pass

    def punch(self, timeout: float = PUNCH_S, open_ttls=OPEN_TTLS) -> bool:
        """Probe every candidate endpoint until a frame of this session comes back.

        The first probes go out with a small IP TTL: they open this side's router mapping and expire on the way,
        before the other side's router. A probe that arrived there before its own side's mapping existed would
        make that router (Linux conntrack, as in most home routers) remember the flow, and then map its own
        side's outgoing probes to another port, which this side's router does not let in."""
        def opening():
            for sock in self.socks:
                for address in self.candidates:
                    self._send(HELLO, os.urandom(8), to=address, via=sock)
            # the other side's router gives every destination a new random port: open SPRAY_PORTS of them here,
            # so that one of its many sockets' ports is likely among them (the birthday paradox)
            for host in self.spray:
                for port in random.sample(range(1024, 65536), SPRAY_PORTS):
                    self._send(HELLO, b"", to=(host, port))
        for ttl in open_ttls:
            self._low_ttl(opening, ttl)
        self.connected.wait(OPEN_WAIT_S)  # the other side opens its mapping meanwhile
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self.connected.is_set():
            for sock in self.socks:
                for address in self.candidates:
                    self._send(HELLO, os.urandom(8), to=address, via=sock)
            self.connected.wait(0.3 if len(self.socks) > 1 else 0.2)
        if self.connected.is_set():
            for _ in range(3):  # make sure the other side hears us too
                self._send(HELLO)
            for sock in self.socks:  # keep only the socket that got through
                if sock is not self.sock:
                    sock.close()
            self.socks = [self.sock]
        return self.connected.is_set()

    # serving
    def _answer(self, payload: bytes) -> None:
        try:
            self._answer_one(payload)
        finally:
            self.answering.release()

    def _answer_one(self, payload: bytes) -> None:
        rid = struct.unpack("!I", payload[:4])[0]
        if rid in self.responses:
            return self._send_chunks(rid, range(len(self.responses[rid][0])))
        path = payload[4:304].decode("utf-8", errors="replace")
        status, body = self.serve(path) if self.serve else (404, b"")
        data = struct.pack("!H", status) + body[:MAX_RESPONSE]
        chunks = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)] or [b""]
        now = time.monotonic()
        self.responses = {k: v for k, v in self.responses.items() if now - v[1] < 60}
        while len(self.responses) >= MAX_CACHED:
            self.responses.pop(next(iter(self.responses)))
        self.responses[rid] = (chunks, now)
        self._send_chunks(rid, range(len(chunks)))

    def _send_chunks(self, rid: int, indexes) -> None:
        chunks = self.responses.get(rid, ([], 0))[0]
        total, started, sent = len(chunks), time.monotonic(), 0
        for i in indexes:
            if i >= total or self.closed.is_set():
                continue
            self._send(RES, struct.pack("!III", rid, i, total) + chunks[i])
            sent += len(chunks[i]) + 60
            ahead = sent / self.rate - (time.monotonic() - started)
            if ahead > 0.002:
                time.sleep(ahead)  # paced: a home uplink drops what it cannot carry

    def _resend(self, payload: bytes) -> None:
        rid = struct.unpack("!I", payload[:4])[0]
        missing = [struct.unpack("!I", payload[i:i + 4])[0] for i in range(4, len(payload) - 3, 4)]
        if rid not in self.lossy:
            self.rate = max(128 << 10, self.rate // 2)  # once per transfer: losses arrive in bursts
        self.lossy.add(rid)
        self.stats["resent"] += len(missing)
        self._send_chunks(rid, missing)

    # asking
    def get(self, path: str, timeout: float = 120) -> tuple[int, bytes]:
        with self.lock:
            rid, self.next_id = self.next_id, self.next_id + 1
        inbox: queue.Queue = queue.Queue()
        self.waiting[rid] = inbox
        pieces: dict[int, bytes] = {}
        total, deadline, asked, last = None, time.monotonic() + timeout, 0.0, time.monotonic()
        try:
            while time.monotonic() < deadline and not self.closed.is_set():
                if total is None and time.monotonic() - asked > 0.7:
                    self._send(REQ, struct.pack("!I", rid) + path.encode()[:300])  # again, if it was lost
                    asked = time.monotonic()
                try:
                    piece = inbox.get(timeout=0.25)
                except queue.Empty:
                    if total is not None and time.monotonic() - last > 0.25:
                        missing = [i for i in range(total) if i not in pieces][:250]
                        self._send(NACK, struct.pack("!I", rid) + b"".join(struct.pack("!I", i) for i in missing))
                        last = time.monotonic()
                    continue
                last = time.monotonic()
                i, n = struct.unpack("!II", piece[:8])
                if total is None:
                    if not 0 < n <= MAX_RESPONSE // CHUNK + 2:
                        continue
                    total = n
                if n == total and i < total:
                    pieces[i] = piece[8:]
                if total is not None and len(pieces) == total:
                    self._send(DONE, struct.pack("!I", rid))
                    data = b"".join(pieces[i] for i in range(total))
                    return struct.unpack("!H", data[:2])[0], data[2:]
        finally:
            self.waiting.pop(rid, None)
        raise TimeoutError(f"no complete answer for {path} over the punched path")

    def close(self) -> None:
        if not self.closed.is_set():
            if self.peer:
                self._send(BYE)
            self.closed.set()
        for sock in self.socks:
            try:
                sock.close()
            except OSError:
                pass


# ---- what a node serves over a punched path ------------------------------------------------------------------
def server_for(node):
    """The read-only protocol routes, as over HTTP (no dashboard, no pushes, no peer list)."""
    from . import __version__
    from .swarm import manifest
    store = node.store

    def serve(path: str) -> tuple[int, bytes]:
        parsed = urllib.parse.urlparse(path)
        parts = [p for p in parsed.path.split("/") if p]
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if parts == []:
                return 200, json.dumps({"service": "humansout-node", "version": __version__, "via": "udp",
                                        "features": ["missions"], "agent": node.agent_id}).encode()
            if parts == ["feeds"]:
                return 200, store.feeds_view()[0]
            if parts == ["missions"]:
                return 200, store.missions_view(query.get("topic", [None])[0])[0]
            if len(parts) == 3 and parts[0] == "missions" and parts[2] == "feeds" and PUB_RE.match(parts[1]):
                body = store.mission_feeds_view(parts[1])[0]
                return (200, body) if body is not None else (404, b"{}")
            if parts == ["peers"]:
                return 200, b"[]"
            if len(parts) == 2 and parts[0] == "feed" and PUB_RE.match(parts[1]):
                feed = store.load_feed(parts[1])
                start = max(int(query.get("from", ["0"])[0]), 0)
                end = int(query["to"][0]) if "to" in query else None
                return 200, "".join(json.dumps({k: v for k, v in e.items() if k != "id"}) + "\n"
                                    for e in feed[start:end]).encode()
            if len(parts) == 2 and parts[0] in ("blob", "manifest") and CID_RE.match(parts[1]):
                data = store.get_blob(parts[1])
                if data is None:
                    return 404, b"{}"
                return 200, data if parts[0] == "blob" else json.dumps(manifest(data)).encode()
        except (ValueError, OSError):
            return 400, b"{}"
        return 404, b"{}"
    return serve


# ---- putting it together ---------------------------------------------------------------------------------------
def _strategy(mine: str, theirs: dict) -> tuple[list[socket.socket], list[str]]:
    """How to punch, from both routers' mapping: (extra sockets for this side, addresses to spray).
    A router that maps each destination to a new port cannot be predicted, so that side probes from many
    sockets and the other side opens many random ports of its address: one match is enough."""
    if mine == "endpoint-dependent" and theirs.get("mapping") != "endpoint-dependent":
        try:
            import resource
            room = resource.getrlimit(resource.RLIMIT_NOFILE)[0] - 128
        except (ImportError, ValueError, OSError):
            room = MANY_SOCKETS
        return [_socket() for _ in range(max(0, min(MANY_SOCKETS, room)))], []
    if mine != "endpoint-dependent" and theirs.get("mapping") == "endpoint-dependent":
        return [], sorted({host for host, _ in theirs.get("outside", [])})
    return [], []


def _socket() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", 0))
    return sock


def connect(node, agent: str, hub: str, stun=None, timeout: float = 60) -> tuple[Link, dict]:
    """Reach another agent directly: offer through the hub, wait for its answer, punch. Raises if it fails."""
    world = node.world()
    agent = world.resolve(agent, set(world.box_keys) - {node.agent_id})
    node.ensure_messaging_key()
    sock = _socket()
    seen = probe(sock, stun)
    mine = seen["endpoints"] + local_endpoints(sock)
    eph, nonce, session = X25519PrivateKey.generate(), os.urandom(16), os.urandom(8)
    started = node.clock()
    offer = {"kind": "offer", "from": node.agent_id, "to": agent, "ts": started, "session": session.hex(),
             "nonce": nonce.hex(), "eph": eph.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex(),
             "endpoints": [list(e) for e in mine], "outside": [list(e) for e in seen["endpoints"]],
             "mapping": seen["mapping"]}
    status = post_envelope(hub, _seal_inner(node, agent, offer))
    if status != 200:
        sock.close()
        raise HumansOutError(f"the hub did not take the offer (HTTP {status}): does it support rendezvous?")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for _, envelope in fetch_envelopes(hub, node.agent_id, 0, wait=min(20, deadline - time.monotonic())):
            answer = _open_inner(node, envelope)
            if (answer and answer.get("kind") == "answer" and answer["from"] == agent
                    and answer.get("for") == nonce.hex() and answer["session"] == session):
                send, recv = _keys(eph, answer["eph"], nonce, answer["nonce"], session)
                extra, spray = _strategy(seen["mapping"], answer)
                link = Link(sock, session, send, recv, answer["endpoints"], serve=server_for(node),
                            extra_socks=extra, spray=spray)
                if link.punch():
                    return link, {"mapping": seen["mapping"], "peer_mapping": answer.get("mapping"),
                                  "path": "direct", "method": "spray" if spray else "many sockets" if extra
                                  else "simultaneous open"}
                link.close()
                raise HumansOutError(f"no direct path: this side's router maps UDP {seen['mapping']}, the other's "
                                     f"{answer.get('mapping')}; the agents keep reaching each other through hubs")
    sock.close()
    raise HumansOutError("the other agent did not answer (it may be offline, or not listening for direct paths)")


def answer(node, envelope: dict, hub: str, stun=None, policy: str = "known") -> Link | None:
    """Answer one offer addressed to this node and punch towards its sender; None if it is refused."""
    offer = _open_inner(node, envelope)
    if not offer or offer.get("kind") != "offer":
        return None
    if policy == "known" and not node._known(node.world(), offer["from"]):
        return None  # a stranger would learn this machine's address: only agents it works with
    sock = _socket()
    seen = probe(sock, stun)
    mine = seen["endpoints"] + local_endpoints(sock)
    eph, nonce = X25519PrivateKey.generate(), os.urandom(16)
    reply = {"kind": "answer", "from": node.agent_id, "to": offer["from"], "ts": node.clock(),
             "session": offer["session"].hex(), "for": offer["nonce"].hex(), "nonce": nonce.hex(),
             "eph": eph.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex(),
             "endpoints": [list(e) for e in mine], "outside": [list(e) for e in seen["endpoints"]],
             "mapping": seen["mapping"]}
    recv, send = _keys(eph, offer["eph"], offer["nonce"], nonce, offer["session"])
    extra, spray = _strategy(seen["mapping"], offer)
    link = Link(sock, offer["session"], send, recv, offer["endpoints"], serve=server_for(node),
                extra_socks=extra, spray=spray)
    if post_envelope(hub, _seal_inner(node, offer["from"], reply)) != 200:
        link.close()
        return None
    if not link.punch():
        link.close()
        return None
    return link


def keep_answering(node, hub: str, stun=None, policy: str | None = None, max_links: int = 8,
                   stop: threading.Event | None = None) -> threading.Thread:
    """Listen for offers at the hub and serve each punched link until it goes quiet (watch --punch)."""
    policy = policy or node.store.config().get("punch_policy", "known")
    links: list[Link] = []
    answered: list[str] = []

    def loop():
        after = 0
        while not (stop and stop.is_set()):
            asked = time.monotonic()
            got = fetch_envelopes(hub, node.agent_id, after)
            if not got and time.monotonic() - asked < 2:
                time.sleep(10)  # the hub answered at once (no free long-poll slot, or no rendezvous): poll gently
            for at, envelope in got:
                after = max(after, at)
                key = str(envelope.get("sig", ""))
                live = [l for l in links if not l.closed.is_set() and time.monotonic() - l.last_heard < IDLE_S]
                links[:] = live
                if key in answered or len(live) >= max_links:
                    continue
                answered[:] = (answered + [key])[-1000:]
                link = answer(node, envelope, hub, stun, policy)
                if link:
                    links.append(link)
            for link in [l for l in links if time.monotonic() - l.last_heard >= IDLE_S]:
                link.close()
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


def proxy(link: Link) -> ThreadingHTTPServer:
    """A loopback HTTP server that forwards GETs over the punched path, so `sync` and `fetch_blob` work as is."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            try:
                status, body = link.get(self.path)
            except TimeoutError:
                status, body = 504, b'{"error":"the punched path stopped answering"}'
            ctype = ("application/x-ndjson" if self.path.startswith("/feed/") else
                     "application/octet-stream" if self.path.startswith("/blob/") else "application/json")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_HEAD(self):
            self.send_response(404)  # sizes are not asked over the path: files come whole
            self.send_header("Content-Length", "0")
            self.end_headers()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def punch_sync(node, agent: str, hub: str, stun=None) -> dict:
    """Connect to an agent directly and pull from it what it has (feeds and the files this node wants)."""
    started = time.monotonic()
    link, info = connect(node, agent, hub, stun)
    server = proxy(link)
    url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        from .net import sync
        report = sync(node, [url], conditional=False)
    finally:
        server.shutdown()
        link.close()
        peers = [p for p in node.store.peers() if p != url]  # the proxy was only for this sync
        (node.home / "peers.json").write_text(json.dumps(peers, indent=2))
    return {**info, "seconds": round(time.monotonic() - started, 1), "new_events": report["new_events"],
            "new_blobs": report["new_blobs"], "datagrams": link.stats}
