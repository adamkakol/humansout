"""Swarming file transfer: pieces from several sources, each checked; a tracker on hubs; seeding."""

import json
import os
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from humansout import Node, swarm
from humansout.core import cid_for
from humansout.net import fetch_blob, serve


def _get(url, headers=None, method="GET"):
    req = urllib.request.Request(url, headers=headers or {}, method=method)
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, dict(resp.headers), resp.read()


@pytest.fixture
def servers():
    started = []

    def start(node, **kw):
        srv = serve(node.store, "127.0.0.1", 0, background=True, agent_id=node.agent_id, **kw)
        started.append(srv)
        return f"http://127.0.0.1:{srv.server_address[1]}"
    yield start
    for srv in started:
        srv.shutdown()


def liar(size):
    """A source that claims the file and sends zeros for every piece."""
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Length", str(size))
            self.end_headers()

        def do_GET(self):
            if self.path.startswith("/manifest/"):
                self.send_response(404)
                self.end_headers()
                return
            start, end = (int(x) for x in self.headers["Range"].split("=")[1].split("-"))
            body = b"\0" * (end - start + 1)
            self.send_response(206)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_manifest_is_the_same_for_every_holder():
    data = os.urandom(3 * swarm.CHUNK + 5)
    m = swarm.manifest(data)
    assert m["cid"] == cid_for(data) and m["size"] == len(data) and len(m["hashes"]) == 4
    assert m == swarm.manifest(bytes(data))


def test_nodes_serve_pieces_and_sizes(tmp_path, servers):
    node = Node(tmp_path / "n")
    data = os.urandom(10_000)
    cid = node.store.put_blob(data)
    url = servers(node)
    status, headers, body = _get(f"{url}/blob/{cid}", {"Range": "bytes=100-199"})
    assert status == 206 and body == data[100:200] and headers["Content-Range"] == f"bytes 100-199/{len(data)}"
    assert _get(f"{url}/blob/{cid}", method="HEAD")[1]["Content-Length"] == str(len(data))
    assert json.loads(_get(f"{url}/manifest/{cid}")[2]) == swarm.manifest(data)


def test_a_big_file_comes_in_pieces_from_several_sources_and_a_liar_is_dropped(tmp_path, servers):
    data = os.urandom(6 * swarm.CHUNK + 123)
    cid = cid_for(data)
    honest = []
    for name in ("a", "b"):
        n = Node(tmp_path / name)
        n.store.put_blob(data)
        honest.append(servers(n))
    bad, bad_url = liar(len(data))
    try:
        got, report = swarm.download(cid, [bad_url] + honest, limit=64 << 20)
    finally:
        bad.shutdown()
    assert got == data
    assert report["dropped"] == [bad_url] and set(report["sources"]) == set(honest)
    assert sum(report["chunks"].values()) == 7


def test_seeders_announce_to_a_hub_and_others_download_from_them(tmp_path, servers):
    hub, seeder, newcomer = Node(tmp_path / "hub"), Node(tmp_path / "seed"), Node(tmp_path / "new")
    hub.store.set_config("accept_push", True)
    hub.store.set_config("providers_allow_private", True)  # this test runs on 127.0.0.1
    hub_url = servers(hub)
    seed_url = servers(seeder)
    seeder.store.set_config("public_url", seed_url)
    mid = seeder.create_mission("Collect idioms", "data/datasets", "100 idioms")["mission_id"]
    f = seeder.home / "work" / "idioms.csv"
    f.parent.mkdir(parents=True)
    f.write_bytes(os.urandom(5 * swarm.CHUNK))
    cid = seeder.publish_artifact(mid, str(f))["cid"]
    seeder.store.add_peer(hub_url)
    from humansout import net
    for author in seeder.store.feed_authors():  # the hub has the events (so the file is of this network), not the file
        net._post(f"{hub_url}/feed/{author}", "".join(json.dumps(e) + "\n" for e in seeder.store.load_feed(author)).encode(),
                  "application/x-ndjson")
    assert not hub.store.has_blob(cid)
    out = swarm.announce(seeder)
    assert out["announced"] >= 1 and out["hubs"][hub_url] == 200
    assert json.loads(_get(f"{hub_url}/providers/{cid}")[2]) == [seed_url]
    newcomer.store.add_peer(hub_url)
    assert fetch_blob(newcomer.store, cid) is None  # the tracker named a local address: not reached by default
    newcomer.store.set_config("allow_private_peers", True)  # a LAN network (this test runs on 127.0.0.1)
    assert fetch_blob(newcomer.store, cid) == f.read_bytes()


def test_nobody_can_point_the_swarm_at_someone_elses_server(tmp_path, servers):
    hub, victim, attacker = Node(tmp_path / "hub"), Node(tmp_path / "victim"), Node(tmp_path / "attacker")
    hub.store.set_config("accept_push", True)
    hub.store.set_config("providers_allow_private", True)
    hub_url, victim_url = servers(hub), servers(victim)
    import time
    a = swarm.announcement(attacker.identity, victim_url, [], int(time.time() * 1000))  # the victim's address
    req = urllib.request.Request(f"{hub_url}/providers", data=json.dumps(a).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 403


def test_hubs_refuse_private_addresses_by_default(tmp_path, servers):
    hub, seeder = Node(tmp_path / "hub"), Node(tmp_path / "seed")
    hub.store.set_config("accept_push", True)
    hub_url, seed_url = servers(hub), servers(seeder)
    import time
    a = swarm.announcement(seeder.identity, seed_url, [], int(time.time() * 1000))
    req = urllib.request.Request(f"{hub_url}/providers", data=json.dumps(a).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 403


def test_a_hostile_tracker_cannot_point_downloads_at_local_files_or_addresses(tmp_path, monkeypatch):
    """A hub's provider list is network data: file: URLs and local addresses in it are never fetched."""
    node = Node(tmp_path / "n")
    secret = tmp_path / "secret.txt"
    secret.write_text("private")
    listed = [f"file://{secret}#", "http://127.0.0.1:9/", "gopher://example.org/"]
    calls = []
    real = swarm._request

    def fake(url, *a, **kw):
        if "/providers/" in url:
            return 200, {}, json.dumps(listed).encode()
        calls.append((url, kw.get("public_only")))
        return real(url, *a, **kw)
    monkeypatch.setattr(swarm, "_request", fake)
    sources, guarded = swarm.sources_for(node.store, "sha256-" + "0" * 64, ["https://hub.example"])
    assert all(s.startswith(("http://", "https://")) for s in sources)
    assert "http://127.0.0.1:9" in guarded and "https://hub.example" not in guarded
    data, _ = swarm.download("sha256-" + "0" * 64, sources, 1 << 20, guarded=guarded)
    assert data is None and all(public for url, public in calls if "127.0.0.1" in url)


def test_guarded_requests_refuse_local_addresses_even_through_a_redirect(tmp_path, servers):
    from humansout import net
    node = Node(tmp_path / "n")
    local = servers(node)
    with pytest.raises(urllib.error.URLError):
        net._get(f"{local}/", public_only=True)  # straight to a local address

    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", f"{local}/")
            self.send_header("Content-Length", "0")
            self.end_headers()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(urllib.error.HTTPError):  # to another host (and port): not followed, guarded or not
            net._get(f"http://localhost:{srv.server_address[1]}/")
    finally:
        srv.shutdown()
    assert json.loads(net._get(f"{local}/"))["service"] == "humansout-node"  # the owner's own peers still work
