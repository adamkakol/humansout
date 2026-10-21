"""Hole punching, on this machine: a fake STUN server, a hub for the rendezvous, two agents and a lossy path.
(Real routers are tested with tools/punch_test.py between two networks.)"""

import json
import os
import socket
import struct
import threading
import time

import pytest

from humansout import HumansOutError, Node, holepunch
from humansout.net import serve


def stun_server(shift_port=0):
    """Answers Binding requests with the address it saw (plus `shift_port`: a NAT that maps per destination)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))

    def loop():
        while True:
            try:
                data, (host, port) = s.recvfrom(2048)
            except OSError:
                return
            if len(data) < 20 or data[:2] != b"\x00\x01":
                continue
            port += shift_port
            xport = port ^ (holepunch.MAGIC >> 16)
            xip = bytes(a ^ b for a, b in zip(socket.inet_aton(host), struct.pack("!I", holepunch.MAGIC), strict=True))
            attr = struct.pack("!HHBBH", 0x0020, 8, 0, 1, xport) + xip
            s.sendto(struct.pack("!HHI", 0x0101, len(attr), holepunch.MAGIC) + data[8:20] + attr, (host, port - shift_port))
    threading.Thread(target=loop, daemon=True).start()
    return s, ("127.0.0.1", s.getsockname()[1])


@pytest.fixture
def stuns():
    a, addr_a = stun_server()
    b, addr_b = stun_server()
    yield (addr_a, addr_b)
    a.close()
    b.close()


@pytest.fixture
def net(tmp_path):
    """A hub that carries rendezvous, and three agents: alice and bob share a mission, carol is a stranger."""
    hub = Node(tmp_path / "hub")
    hub.store.set_config("accept_push", True)
    srv = serve(hub.store, "127.0.0.1", 0, background=True, agent_id=hub.agent_id)
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    alice, bob, carol = (Node(tmp_path / n) for n in ("alice", "bob", "carol"))
    mid = bob.create_mission("Collect idioms", "data/datasets", "100 idioms")["mission_id"]
    for n in (bob, alice, carol):
        n.store.add_peer(url)
        n.ensure_messaging_key()
        n.sync()
    alice.sync()
    alice.join(mid)
    for n in (alice, bob, carol):
        n.sync()
    yield {"hub": hub, "url": url, "alice": alice, "bob": bob, "carol": carol, "mission": mid}
    srv.shutdown()


def test_stun_tells_a_socket_how_it_looks_from_outside(stuns):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    try:
        seen = holepunch.probe(sock, stuns)
    finally:
        sock.close()
    assert seen["endpoints"] == [("127.0.0.1", port)] and seen["mapping"] == "endpoint-independent"


def test_a_nat_that_maps_per_destination_is_recognised():
    a, addr_a = stun_server()
    b, addr_b = stun_server(shift_port=7)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    try:
        assert holepunch.probe(sock, (addr_a, addr_b))["mapping"] == "endpoint-dependent"
    finally:
        for s in (a, b, sock):
            s.close()


def test_two_agents_reach_each_other_directly_and_sync_what_the_hub_never_had(net, stuns):
    alice, bob = net["alice"], net["bob"]
    stop = threading.Event()
    holepunch.keep_answering(bob, net["url"], stun=stuns, stop=stop)
    f = bob.home / "work" / "idioms.csv"
    f.parent.mkdir(parents=True)
    f.write_bytes(os.urandom(300_000))  # 260 datagrams
    cid = bob.publish_artifact(net["mission"], str(f))["cid"]  # not synced: the hub has neither event nor file
    assert not net["hub"].store.has_blob(cid)
    try:
        report = holepunch.punch_sync(alice, bob.agent_id[:12], net["url"], stun=stuns)
    finally:
        stop.set()
    assert report["path"] == "direct" and report["new_events"] >= 1 and report["new_blobs"] >= 1
    assert alice.store.get_blob(cid) == f.read_bytes()
    assert alice.store.feed_len(bob.agent_id) == bob.store.feed_len(bob.agent_id)
    assert not net["hub"].store.has_blob(cid)  # it came straight from bob
    assert all("127.0.0.1" not in p or p == net["url"] for p in alice.store.peers())  # the proxy is not kept


def test_a_stranger_gets_no_answer_and_learns_no_address(net, stuns):
    carol, bob = net["carol"], net["bob"]
    stop = threading.Event()
    holepunch.keep_answering(bob, net["url"], stun=stuns, stop=stop)
    try:
        with pytest.raises(HumansOutError, match="did not answer"):
            holepunch.connect(carol, bob.agent_id, net["url"], stun=stuns, timeout=4)
    finally:
        stop.set()


def test_the_hub_carries_offers_it_cannot_read_or_forge(net, stuns):
    alice, bob, url = net["alice"], net["bob"], net["url"]
    offer = {"kind": "offer", "from": alice.agent_id, "to": bob.agent_id, "ts": alice.clock(), "session": "00" * 8,
             "nonce": "11" * 16, "eph": "22" * 32, "endpoints": [["203.0.113.7", 40000]]}
    envelope = holepunch._seal_inner(alice, bob.agent_id, offer)
    assert "203.0.113.7" not in json.dumps(envelope)  # sealed: the hub never sees the address
    assert holepunch.post_envelope(url, envelope) == 200
    (at, carried), = holepunch.fetch_envelopes(url, bob.agent_id, 0)
    assert holepunch._open_inner(bob, carried)["endpoints"] == [("203.0.113.7", 40000)]
    forged = dict(envelope, **{"from": net["carol"].agent_id})  # someone else's name on alice's box
    assert holepunch.post_envelope(url, forged) == 400
    stale = holepunch._envelope(alice.identity, bob.agent_id, envelope["box"], alice.clock() - 10 * 60_000)
    assert holepunch.post_envelope(url, stale) == 400
    assert holepunch._open_inner(net["carol"], carried) is None  # not addressed to carol, and she cannot open it


def pair(serve_a=None, serve_b=None, drop=0.0):
    """Two linked endpoints on loopback, as after an exchange; `drop` loses that share of b's datagrams."""
    sa, sb = (socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(2))
    sa.bind(("127.0.0.1", 0))
    sb.bind(("127.0.0.1", 0))
    if drop:
        import random
        rng = random.Random(7)
        real = sb.sendto

        class Lossy:
            def __getattr__(self, name):
                return getattr(sb, name)

            def sendto(self, data, addr):
                return len(data) if rng.random() < drop else real(data, addr)
        sb_used = Lossy()
    else:
        sb_used = sb
    session, k1, k2 = os.urandom(8), os.urandom(32), os.urandom(32)
    a = holepunch.Link(sa, session, k1, k2, [sb.getsockname()], serve=serve_a)
    b = holepunch.Link(sb_used, session, k2, k1, [sa.getsockname()], serve=serve_b)
    return a, b, sa, sb


def test_a_lossy_path_still_delivers_every_byte():
    data = os.urandom(2_000_000)
    a, b, *_ = pair(serve_b=lambda path: (200, data) if path == "/blob/x" else (404, b""), drop=0.2)
    try:
        assert a.punch(3) and b.punch(3)
        status, body = a.get("/blob/x", timeout=60)
        assert status == 200 and body == data and b.stats["resent"] > 0
    finally:
        a.close()
        b.close()


def test_forged_and_replayed_datagrams_get_no_answer():
    a, b, sa, sb = pair(serve_b=lambda path: (200, b"x" * 100_000))
    assert a.punch(3)
    try:
        attacker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        attacker.bind(("127.0.0.1", 0))
        attacker.settimeout(1.0)
        for junk in (os.urandom(200), b.session + os.urandom(100)):  # random, and with the right session id
            attacker.sendto(junk, sb.getsockname())
        a._send(holepunch.REQ, struct.pack("!I", 99) + b"/blob/y")
        time.sleep(0.5)
        with a.lock:
            counter = a.counter
        header = a.session + struct.pack("!Q", counter)  # the frame a just sent, sent again by the attacker
        replay = header + a.send_aead.encrypt(b"\0\0\0\0" + header[8:], bytes([holepunch.REQ]) + struct.pack("!I", 99)
                                              + b"/blob/y", header)
        attacker.sendto(replay, sb.getsockname())
        with pytest.raises(socket.timeout):
            attacker.recvfrom(2048)  # nothing ever comes back to the attacker's address
        assert b.stats["dropped"] >= 3
    finally:
        a.close()
        b.close()


def _lab_possible() -> bool:
    import shutil
    import subprocess
    if not (shutil.which("unshare") and shutil.which("nft") and shutil.which("nsenter")):
        return False
    probe = subprocess.run(["unshare", "-rn", "sh", "-c", "nft add table ip t"], capture_output=True, timeout=20)
    return probe.returncode == 0


@pytest.mark.skipif(not _lab_possible(), reason="needs unprivileged user namespaces and nftables (Linux)")
@pytest.mark.parametrize("mode,direct", [("cone", True), ("symmetric", True), ("both-symmetric", False)])
def test_through_two_real_nat_routers(mode, direct):
    """tools/nat_lab.sh: two homes behind Linux routers doing NAT and refusing unsolicited traffic."""
    import subprocess
    from pathlib import Path
    script = Path(__file__).resolve().parent.parent / "tools" / "nat_lab.sh"
    out = subprocess.run([str(script), mode], capture_output=True, text=True, timeout=120)
    result = json.loads(out.stdout.strip().splitlines()[-1])
    assert result["direct"] is direct, out.stdout + out.stderr
    if direct:
        assert result["file_ok"] and not result["hub_has_file"] and result["peer_seen_at"].startswith("198.18.2.2:")
        assert result["method"] == ("simultaneous open" if mode == "cone" else "spray")
    else:
        assert "endpoint-dependent, the other's endpoint-dependent" in result["why"]


def test_the_punch_command_listens_and_reaches(net, stuns, monkeypatch, capsys):
    """Through the CLI, as a user runs it: one side listens, the other reaches it and pulls."""
    import threading as th
    from humansout import cli
    monkeypatch.setattr(holepunch, "STUN_SERVERS", stuns)  # no public STUN server is asked
    bob, alice = net["bob"], net["alice"]
    listener = th.Thread(target=cli.main, args=(["--home", str(bob.home), "punch", "--listen", "0.15",
                                                 "--hub", net["url"]],), daemon=True)
    listener.start()
    time.sleep(1)
    cli.main(["--home", str(alice.home), "punch", bob.agent_id[:12], "--hub", net["url"]])
    out = capsys.readouterr().out
    assert '"path": "direct"' in out
    listener.join(timeout=30)
