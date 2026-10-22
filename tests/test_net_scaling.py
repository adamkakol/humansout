"""Network layer at scale: cacheable blobs and feed ranges, mission-scoped sync, ETags and long-polls,
several hubs at once, and two hubs federating (docs/SCALING.md)."""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from humansout import Node, net
from humansout.cli import main
from humansout.core import cid_for
from humansout.net import fetch_blob, serve, wait_for_change
from humansout.store import normalize_peer


def request(url, headers=None, data=None, method=None):
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def push(url, node):
    body = "".join(json.dumps({k: v for k, v in e.items() if k != "id"}) + "\n" for e in node.store.load_feed(node.agent_id))
    return request(f"{url}/feed/{node.agent_id}", {"Content-Type": "application/x-ndjson"}, body.encode(), "POST")


@pytest.fixture
def hubs(tmp_path):
    started = []

    def start(name="hub", **config):
        node = Node(tmp_path / name)
        for key, value in {"accept_push": True, **config}.items():
            node.store.set_config(key, value)
        srv = serve(node.store, "127.0.0.1", 0, background=True, agent_id=node.agent_id)
        started.append(srv)
        return node, f"http://127.0.0.1:{srv.server_address[1]}"
    yield start
    for srv in started:
        srv.shutdown()


@pytest.fixture
def requests_seen(monkeypatch):
    """Every request a syncing node makes: (path, status)."""
    seen = []
    conditional, plain = net._get_conditional, net._get

    def counting_conditional(url, etag, *a, **kw):
        out = conditional(url, etag, *a, **kw)
        seen.append((url.split("/", 3)[3], out[0]))
        return out

    def counting_get(url, *a, **kw):
        try:
            out = plain(url, *a, **kw)
        except urllib.error.HTTPError as e:
            seen.append((url.split("/", 3)[3], e.code))
            raise
        seen.append((url.split("/", 3)[3], 200))
        return out
    monkeypatch.setattr(net, "_get_conditional", counting_conditional)
    monkeypatch.setattr(net, "_get", counting_get)
    return seen


def two_missions(tmp_path, url):
    """Mission A (software/python): creator, alice (claim), bob (challenge). Mission B (math/lean): creator, carol."""
    creator, alice, bob, carol = (Node(tmp_path / n) for n in ("creator", "alice", "bob", "carol"))
    ma = creator.create_mission("A", "software/python", "c", challenge_window=3600)["mission_id"]
    mb = creator.create_mission("B", "math/lean", "c", challenge_window=3600)["mission_id"]
    alice.store.ingest(creator.agent_id, creator.store.load_feed(creator.agent_id))
    bob.store.ingest(creator.agent_id, creator.store.load_feed(creator.agent_id))
    carol.store.ingest(creator.agent_id, creator.store.load_feed(creator.agent_id))
    alice.join(ma)
    f = alice.home / "work" / "a.py"
    f.parent.mkdir(parents=True)
    f.write_text("x = 1\n")
    cid = alice.publish_artifact(ma, str(f))["cid"]
    claim = alice.claim_done(ma, "done", cids=[cid])["claim"]
    bob.store.ingest(alice.agent_id, alice.store.load_feed(alice.agent_id))
    bob.join(ma)
    bob.challenge(claim, "x must be 2", preflight=False)
    carol.join(mb)
    for n in (creator, alice, bob, carol):
        n.sync([url])
    return {"creator": creator, "alice": alice, "bob": bob, "carol": carol, "a": ma, "b": mb, "claim": claim, "cid": cid}


# ---- 1. immutable blobs ---------------------------------------------------------------------

def test_blobs_are_served_as_immutable_with_their_id_as_etag(hubs, tmp_path):
    hub, url = hubs()
    cid = hub.store.put_blob(b"hello")
    status, body, headers = request(f"{url}/blob/{cid}")
    assert status == 200 and body == b"hello"
    assert headers["Cache-Control"] == "public, max-age=31536000, immutable" and headers["ETag"] == f'"{cid}"'
    status, body, _ = request(f"{url}/blob/{cid}", {"If-None-Match": f'W/"{cid}"'})  # CDNs weaken ETags
    assert status == 304 and body == b""
    status, _, headers = request(f"{url}/blob/{cid_for(b'missing')}")
    assert status == 404 and headers["Cache-Control"] == "no-cache"  # a later push must not be hidden by a cached 404


def test_fetch_blob_tries_every_known_peer(hubs, tmp_path):
    _, empty = hubs("empty")
    holder, has_it = hubs("holder")
    cid = holder.store.put_blob(b"the file")
    me = Node(tmp_path / "me")
    for peer in ("http://127.0.0.1:9", empty, has_it):  # dead, without it, with it
        me.store.add_peer(peer)
    assert fetch_blob(me.store, cid) == b"the file" and me.store.has_blob(cid)
    other = Node(tmp_path / "other")
    other.store.add_peer(has_it)
    assert fetch_blob(other.store, cid, [empty]) == b"the file"  # the peers given first, then every known one


def test_complete_feed_ranges_are_cacheable(hubs, tmp_path):
    hub, url = hubs()
    alice = Node(tmp_path / "alice")
    mid = alice.create_mission("g", "c", "x")["mission_id"]
    alice.add_work(mid, "part")
    push(url, alice)
    status, body, headers = request(f"{url}/feed/{alice.agent_id}?from=0&to=1")
    assert status == 200 and len(body.splitlines()) == 1 and "immutable" in headers["Cache-Control"]
    _, body, headers = request(f"{url}/feed/{alice.agent_id}?from=1")
    assert len(body.splitlines()) == 1 and headers["Cache-Control"] == "no-cache"  # the open end grows
    _, body, headers = request(f"{url}/feed/{alice.agent_id}?from=0&to=5")
    assert len(body.splitlines()) == 2 and headers["Cache-Control"] == "no-cache"  # not all there yet


# ---- 2. mission-scoped sync -----------------------------------------------------------------

def test_mission_index_lists_every_feed_with_events_in_a_mission(hubs, tmp_path):
    hub, url = hubs()
    n = two_missions(tmp_path, url)
    _, body, _ = request(f"{url}/missions/{n['a']}/feeds")
    index = json.loads(body)
    assert set(index) == {n["creator"].agent_id, n["alice"].agent_id, n["bob"].agent_id}
    assert index[n["bob"].agent_id] == hub.store.feed_len(n["bob"].agent_id)
    _, body, _ = request(f"{url}/missions/{n['b']}/feeds")
    assert set(json.loads(body)) == {n["creator"].agent_id, n["carol"].agent_id}
    assert request(f"{url}/missions/{'0' * 64}/feeds")[0] == 404
    _, body, _ = request(f"{url}/missions?topic=software/")
    assert json.loads(body) == {n["a"]: {"category": "software/python", "creator": n["creator"].agent_id, "feeds": 3}}
    _, body, _ = request(f"{url}/missions")
    assert set(json.loads(body)) == {n["a"], n["b"]}


def test_objections_are_indexed_even_when_they_arrive_before_their_target(tmp_path):
    clock = lambda: 1_800_000_000_000  # noqa: E731
    creator, alice, bob = (Node(tmp_path / n, clock=clock) for n in ("creator", "alice", "bob"))
    mid = creator.create_mission("g", "c", "x", challenge_window=3600)["mission_id"]
    for n in (alice, bob):
        n.store.ingest(creator.agent_id, creator.store.load_feed(creator.agent_id))
    alice.join(mid)
    claim = alice.claim_done(mid, "done")["claim"]
    bob.store.ingest(alice.agent_id, alice.store.load_feed(alice.agent_id))
    bob.challenge(claim, "wrong", preflight=False)  # names only the claim, not the mission
    hub = Node(tmp_path / "hub", clock=clock)
    hub.store.ingest(bob.agent_id, bob.store.load_feed(bob.agent_id))
    hub.store.ingest(creator.agent_id, creator.store.load_feed(creator.agent_id))
    assert bob.agent_id not in json.loads(hub.store.mission_feeds_view(mid)[0])  # target unknown yet
    hub.store.ingest(alice.agent_id, alice.store.load_feed(alice.agent_id))
    assert bob.agent_id in json.loads(hub.store.mission_feeds_view(mid)[0])
    assert hub.store.missions_of(bob.agent_id) == {mid}


def test_index_agrees_with_the_fold(tmp_path):
    """Every author the fold counts in a mission is in that mission's index."""
    srv_node = Node(tmp_path / "hub")
    srv_node.store.set_config("accept_push", True)
    srv = serve(srv_node.store, "127.0.0.1", 0, background=True)
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        n = two_missions(tmp_path, url)
        world = srv_node.world()
        for mid, mission in world.missions.items():
            index = set(json.loads(srv_node.store.mission_feeds_view(mid)[0]))
            assert set(mission["participants"]) | {mission["creator"]} <= index
            assert {i["author"] for i in world.items.values() if i["mission"] == mid} <= index
        assert n["cid"] in srv_node.store.referenced_cids()
    finally:
        srv.shutdown()


def test_scoped_sync_pulls_only_the_missions_asked_for(hubs, tmp_path):
    hub, url = hubs()
    n = two_missions(tmp_path, url)
    dave = Node(tmp_path / "dave")
    report = dave.sync([url], missions=[n["a"][:10]])  # a prefix is enough
    assert report["scope"]["missions"] == 0 and report["new_events"] > 0
    have = set(dave.store.feed_authors())
    assert have == {n["creator"].agent_id, n["alice"].agent_id, n["bob"].agent_id}  # not carol
    assert dave.mission_state(n["a"])["claims"][0]["id"] == n["claim"]
    erin = Node(tmp_path / "erin")
    erin.sync([url], topics=["math/"])
    assert set(erin.store.feed_authors()) == {n["creator"].agent_id, n["carol"].agent_id}
    # the missions an agent takes part in always come along: erin joins A, then syncs by topic only
    erin.store.ingest(n["creator"].agent_id, hub.store.load_feed(n["creator"].agent_id))
    erin.join(n["a"])
    erin.sync([url], topics=["math/"])
    assert n["alice"].agent_id in erin.store.feed_authors()
    erin.store.set_config("sync_topics", ["nothing/"])  # config keys work as defaults too
    assert erin.sync([url])["scope"]["topics"] == ["nothing/"]


def test_scoped_sync_falls_back_to_everything_on_older_hubs(hubs, tmp_path, monkeypatch):
    hub, url = hubs()
    n = two_missions(tmp_path, url)
    monkeypatch.setattr(net, "FEATURES", [])  # a 0.3 hub announces no mission index
    dave = Node(tmp_path / "dave")
    dave.sync([url], missions=[n["a"]])
    assert n["carol"].agent_id in dave.store.feed_authors()


def test_cli_sync_takes_missions_and_topics(hubs, tmp_path, capsys):
    hub, url = hubs()
    n = two_missions(tmp_path, url)
    home = tmp_path / "cli"
    assert main(["--home", str(home), "sync", url, "--missions", f"{n['b']},{n['b'][:8]}"]) == 0
    assert set(Node(home).store.feed_authors()) == {n["creator"].agent_id, n["carol"].agent_id}
    capsys.readouterr()
    assert main(["--home", str(home), "sync", url, "--missions", "not-hex"]) == 1


# ---- 3. cheap polling -----------------------------------------------------------------------

def test_unchanged_listings_answer_304(hubs, tmp_path):
    hub, url = hubs()
    alice = Node(tmp_path / "alice")
    mid = alice.create_mission("g", "c", "x")["mission_id"]
    push(url, alice)
    for path in ("/feeds", f"/missions/{mid}/feeds", "/missions", "/peers", "/"):
        status, body, headers = request(url + path)
        assert status == 200 and headers["ETag"] and headers["Cache-Control"] == ("no-cache" if path == "/" else "public, max-age=5")
        status, body, _ = request(url + path, {"If-None-Match": headers["ETag"]})
        assert status == 304 and body == b""
    _, _, before = request(f"{url}/feeds")
    alice.add_work(mid, "part")
    push(url, alice)
    status, _, after = request(f"{url}/feeds", {"If-None-Match": before["ETag"]})
    assert status == 200 and after["ETag"] != before["ETag"]


def test_a_second_sync_of_an_unchanged_hub_is_all_304s(hubs, tmp_path, requests_seen):
    hub, url = hubs()
    n = two_missions(tmp_path, url)
    dave = Node(tmp_path / "dave")
    dave.sync([url])
    requests_seen.clear()
    assert dave.sync([url])["new_events"] == 0
    assert requests_seen == [("feeds", 304)]  # peer exchange is not repeated within 10 minutes
    requests_seen.clear()
    dave.sync([url], missions=[n["a"]])
    assert requests_seen == [("", 200), (f"missions/{n['a']}/feeds", 200)]  # what the hub supports, then the index
    requests_seen.clear()
    dave.sync([url], missions=[n["a"]])
    assert requests_seen == [(f"missions/{n['a']}/feeds", 304)]


def test_long_poll_wakes_on_change_and_times_out_otherwise(hubs, tmp_path):
    hub, url = hubs()
    alice = Node(tmp_path / "alice")
    mid = alice.create_mission("g", "c", "x")["mission_id"]
    push(url, alice)
    _, _, headers = request(f"{url}/feeds")
    started = time.monotonic()
    assert wait_for_change(url, headers["ETag"], timeout=1) is None
    assert 0.9 < time.monotonic() - started < 5
    threading.Timer(0.3, lambda: (alice.add_work(mid, "part"), push(url, alice))).start()
    started = time.monotonic()
    new = wait_for_change(url, headers["ETag"], timeout=20)
    assert new and new != headers["ETag"] and time.monotonic() - started < 5
    assert new == request(f"{url}/feeds")[2]["ETag"]
    _, _, mh = request(f"{url}/missions/{mid}/feeds")
    assert wait_for_change(url, mh["ETag"], mission=mid, timeout=0.5) is None


def test_long_polls_are_capped(hubs, tmp_path):
    hub, url = hubs(max_waiters=1)
    _, _, headers = request(f"{url}/feeds")
    first = threading.Thread(target=lambda: request(f"{url}/wait?since={headers['ETag']}&timeout=2"))
    first.start()
    time.sleep(0.3)
    status, _, h = request(f"{url}/wait?since={headers['ETag']}&timeout=2")
    first.join()
    assert status == 429 and h["Retry-After"] == "30"


# ---- 4. several hubs ------------------------------------------------------------------------

def test_each_feed_comes_from_the_peer_with_the_most_of_it(hubs, tmp_path, requests_seen):
    _, short = hubs("short")
    _, long = hubs("long")
    alice = Node(tmp_path / "alice")
    mid = alice.create_mission("g", "c", "x")["mission_id"]
    push(short, alice)
    alice.add_work(mid, "one")
    alice.add_work(mid, "two")
    push(long, alice)
    me = Node(tmp_path / "me")
    report = me.sync([short, long])
    assert me.store.feed_len(alice.agent_id) == 3 and report["new_events"] == 3
    pulls = [p for p, _ in requests_seen if p.startswith("feed/")]
    assert pulls == [f"feed/{alice.agent_id}?from=0&to=3"]
    assert report["peers"][long]["events"] == 3 and report["peers"][short]["events"] == 0


def test_a_failing_source_falls_back_to_the_next(hubs, tmp_path, monkeypatch):
    _, a = hubs("a")
    _, b = hubs("b")
    alice = Node(tmp_path / "alice")
    alice.create_mission("g", "c", "x")
    push(a, alice)
    push(b, alice)
    real = net._get

    def flaky(url, *args, **kw):
        if url.startswith(a + "/feed/"):
            raise urllib.error.URLError("connection reset")
        return real(url, *args, **kw)
    monkeypatch.setattr(net, "_get", flaky)
    me = Node(tmp_path / "me")
    assert me.sync([a, b])["new_events"] == 1


def test_peers_are_learned_deduplicated_and_never_ourselves(hubs, tmp_path):
    first, url_a = hubs("a")
    second, url_b = hubs("b", allow_private_peers=True)
    first.store.set_config("public_url", url_a)
    second.store.set_config("public_url", url_b + "/")
    second.store.add_peer(url_a)  # so b lists a
    me = Node(tmp_path / "me")
    me.store.set_config("allow_private_peers", True)
    me.store.add_peer(url_b.replace("http://", "HTTP://") + "/")
    report = me.sync()
    assert report["learned_peers"] == [url_a] and me.store.peers() == [url_b, url_a]
    assert second.sync()["learned_peers"] == []  # a lists b's own address back: not added
    assert url_b not in second.store.peers()


def test_peer_urls_have_one_spelling():
    assert normalize_peer("HTTPS://Node.Example.org:443/") == "https://node.example.org"
    assert normalize_peer("http://h:80") == "http://h" and normalize_peer("http://h:7400/x/") == "http://h:7400/x"
    assert normalize_peer("http://[::1]:7400") == "http://[::1]:7400"
    assert normalize_peer("ftp://h") is None and normalize_peer("http://h:99999") is None


def test_two_hubs_federate(hubs, tmp_path):
    """alice pushes only to hub A, bob uses only hub B; the hubs pull from each other (blobs included)."""
    hub_a, url_a = hubs("a")
    hub_b, url_b = hubs("b")
    creator, alice, bob = (Node(tmp_path / n) for n in ("creator", "alice", "bob"))
    mid = creator.create_mission("g", "software/python", "c", challenge_window=3600)["mission_id"]
    creator.sync([url_a])
    alice.sync([url_a])
    alice.join(mid)
    f = alice.home / "work" / "a.py"
    f.parent.mkdir(parents=True)
    f.write_text("x = 1\n")
    cid = alice.publish_artifact(mid, str(f))["cid"]
    claim = alice.claim_done(mid, "done", cids=[cid])["claim"]
    alice.sync([url_a])
    hub_b.sync([url_a])  # what `serve --sync` does on each hub
    hub_a.sync([url_b])
    assert hub_b.store.has_blob(cid)  # hubs mirror every referenced blob
    bob.sync([url_b])
    bob.join(mid)
    bob.challenge(claim, "x must be 2", preflight=False)
    bob.sync([url_b])
    assert bob.get_artifact(cid, fetch=False)["text"] == "x = 1\n"
    hub_a.sync([url_b])
    alice.sync([url_a])
    assert alice.mission_state(mid)["claims"][0]["challenges"][0]["author"] == bob.agent_id


def test_concurrent_ingest_of_one_feed_never_corrupts_it(tmp_path):
    alice = Node(tmp_path / "alice")
    mid = alice.create_mission("g", "c", "x")["mission_id"]
    for i in range(30):
        alice.add_work(mid, f"part {i}")
    events = alice.store.load_feed(alice.agent_id)
    hub = Node(tmp_path / "hub")
    threads = [threading.Thread(target=hub.store.ingest, args=(alice.agent_id, events[:k])) for k in (5, 31, 17, 31, 9, 25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    path = hub.store.feed_path(alice.agent_id)
    assert hub.store.feed_len(alice.agent_id) == 31 and len(path.read_text().splitlines()) == 31


def test_rate_limit_counts_clients_behind_a_local_proxy_separately(hubs, tmp_path):
    hub, url = hubs(push_rate_per_min=1)
    alice = Node(tmp_path / "alice")
    alice.create_mission("g", "c", "x")
    body = "".join(json.dumps({k: v for k, v in e.items() if k != "id"}) + "\n" for e in alice.store.load_feed(alice.agent_id))

    def post(ip, spoofed="198.51.100.7"):  # the proxy appends the address it saw; the client wrote the rest
        return request(f"{url}/feed/{alice.agent_id}", {"X-Forwarded-For": f"{spoofed}, {ip}"}, body.encode(),
                       "POST")[0]
    assert post("203.0.113.1") == 200 and post("203.0.113.1", spoofed="198.51.100.8") == 429
    assert post("203.0.113.2") == 200  # a different agent behind the same proxy


def test_a_client_cannot_name_itself_with_cf_connecting_ip_behind_a_plain_proxy(hubs, tmp_path):
    """Found by the security-review swarm (round 3): behind nginx, which passes a client's own CF-Connecting-IP
    through, every request could carry a fresh value and so a fresh rate-limit key."""
    hub, url = hubs(push_rate_per_min=1)
    alice = Node(tmp_path / "alice")
    alice.create_mission("g", "c", "x")
    body = "".join(json.dumps({k: v for k, v in e.items() if k != "id"}) + "\n" for e in alice.store.load_feed(alice.agent_id))

    def post(fake):
        return request(f"{url}/feed/{alice.agent_id}", {"CF-Connecting-IP": fake, "X-Forwarded-For": "203.0.113.1"},
                       body.encode(), "POST")[0]
    assert post("198.51.100.1") == 200 and post("198.51.100.2") == 429
    hub.store.set_config("client_ip_header", "cf-connecting-ip")  # an owner behind Cloudflare, which sets it
    assert post("198.51.100.3") == 200 and post("198.51.100.3") == 429


def test_blobs_nobody_has_are_not_asked_for_every_round(hubs, tmp_path, requests_seen):
    hub, url = hubs()
    creator, alice = Node(tmp_path / "creator"), Node(tmp_path / "alice")
    mid = creator.create_mission("g", "c", "x", challenge_window=3600)["mission_id"]
    creator.sync([url])
    alice.sync([url])
    alice.join(mid)
    f = alice.home / "work" / "a.py"
    f.parent.mkdir(parents=True)
    f.write_text("x = 1\n")
    cid = alice.publish_artifact(mid, str(f))["cid"]
    alice.claim_done(mid, "done", cids=[cid])
    push(url, alice)  # events only: the file never reaches the hub
    creator.sync([url])
    assert any(p == f"blob/{cid}" for p, _ in requests_seen)
    requests_seen.clear()
    creator.sync([url])
    assert not any(p.startswith("blob/") for p, _ in requests_seen)
