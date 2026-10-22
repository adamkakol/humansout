"""Hubs that only coordinate (host_files "own"): files stay with the agents that hold them, and travel between
agents over direct paths. Adam's requirement of 22 October: node.humansout.si must not host the agents' artifacts."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from humansout import Node, holepunch, seeding
from humansout.net import serve
from test_holepunch import stun_server


@pytest.fixture
def stuns():
    a, addr_a = stun_server()
    b, addr_b = stun_server()
    yield (addr_a, addr_b)
    a.close()
    b.close()


@pytest.fixture
def net(tmp_path):
    hub = Node(tmp_path / "hub")
    for key, value in (("accept_push", True), ("host_files", "own")):
        hub.store.set_config(key, value)
    srv = serve(hub.store, "127.0.0.1", 0, background=True, agent_id=hub.agent_id)
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    mid = hub.create_mission("Collect idioms", "data/datasets", "100 idioms")["mission_id"]
    alice, bob = Node(tmp_path / "alice"), Node(tmp_path / "bob")
    for n in (alice, bob):
        n.store.add_peer(url)
        n.ensure_messaging_key()
        n.sync()
        n.join(mid)
        n.sync()
    yield {"hub": hub, "url": url, "alice": alice, "bob": bob, "mission": mid}
    srv.shutdown()


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def publish(node, mission, name, data):
    f = node.home / "work" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(data)
    return node.publish_artifact(mission, str(f), name)["cid"]


def test_a_coordinating_hub_takes_events_but_hosts_only_its_own_files(net, tmp_path):
    hub, url, bob = net["hub"], net["url"], net["bob"]
    cid = publish(bob, net["mission"], "idioms.csv", b"a,b\n" * 1000)
    report = bob.sync()
    assert report["pushed_events"] >= 1 and not hub.store.has_blob(cid)  # the event, never the file
    assert cid in [a["cid"] for a in hub.world().missions[net["mission"]]["artifacts"]]
    hub.store.put_blob((bob.home / "work" / "idioms.csv").read_bytes())  # even a copy it holds is not served
    assert get(f"{url}/blob/{cid}")[0] == 404 and get(f"{url}/manifest/{cid}")[0] == 404
    assert get(f"{url}/api/blob/{cid}")[0] == 404
    own = publish(hub, net["mission"], "rules.md", b"# rules\n")  # what the hub publishes itself, it serves
    assert get(f"{url}/blob/{own}") == (200, b"# rules\n")
    (hub.home / "hosted_keep.json").write_text(json.dumps([cid]))  # files kept while their missions run
    assert get(f"{url}/blob/{cid}")[0] == 200


def test_a_coordinating_hub_still_carries_sealed_messages(net):
    alice, bob = net["alice"], net["bob"]
    alice.sync()  # bob's messaging key
    alice.send_message(bob.agent_id, "hello through the mailbox", mission=net["mission"])
    alice.sync()
    bob.sync()
    texts = [m.get("text") for m in bob.inbox(everyone=True)["messages"]]
    assert "hello through the mailbox" in texts


def test_agents_fetch_files_straight_from_the_agent_that_holds_them(net, stuns, monkeypatch):
    hub, alice, bob = net["hub"], net["alice"], net["bob"]
    data = bytes(range(256)) * 2000
    cid = publish(bob, net["mission"], "dataset.bin", data)
    bob.sync()
    alice.sync()
    assert seeding.holders(alice.world(), cid)[0] == bob.agent_id
    stop = threading.Event()
    holepunch.keep_answering(bob, net["url"], stun=stuns, stop=stop)
    try:
        got = seeding.fetch_direct(alice, cid, 10_000_000, hub=net["url"], stun=stuns)
    finally:
        stop.set()
    assert got == data and alice.store.get_blob(cid) == data
    assert not hub.store.has_blob(cid)  # it never touched the hub
    # and the node's ordinary fetch falls back to it when no reachable node serves the file
    calls = []
    monkeypatch.setattr(seeding, "fetch_direct", lambda node, c, limit, **kw: calls.append(c) or None)
    assert net["alice"].fetch_file("sha256-" + "0" * 64) is None and calls == ["sha256-" + "0" * 64]


def test_a_claim_whose_deliverable_nobody_serves_fails_after_the_grace(tmp_path, monkeypatch):
    from humansout import node as node_module
    from humansout.node import UNSERVED_GRACE_MS, UNSERVED_RETRY_MS

    class Clock:
        t = 1_800_000_000_000

        def __call__(self):
            return self.t
    clock = Clock()
    creator, builder = Node(tmp_path / "c", clock=clock), Node(tmp_path / "b", clock=clock)
    test = tmp_path / "test_x.py"
    test.write_text("import unittest\n")
    mid = creator.create_mission("Write x", "software/python", "tests pass", challenge_window=6 * 3600,
                                 check={"command": ["python", "-m", "unittest"], "deliverable": "x.py",
                                        "files": [str(test)]})["mission_id"]
    builder.store.ingest(creator.agent_id, creator.store.load_feed(creator.agent_id))
    builder.join(mid)
    f = tmp_path / "x.py"
    f.write_text("X = 1\n")
    cid = builder.publish_artifact(mid, str(f), "x.py")["cid"]
    claim = builder.claim_done(mid, "done", cids=[cid])["claim"]
    creator.store.ingest(builder.agent_id, builder.store.load_feed(builder.agent_id))  # the events, not the file
    monkeypatch.setattr(seeding, "fetch_direct", lambda *a, **k: None)  # the builder is offline
    runs = []
    monkeypatch.setattr(node_module, "run_check", lambda g, check, d, files, n: runs.append(d) or {
        "result": "pass", "exit_codes": [0], "image": check["image"], "stdout": "", "stderr": ""})
    out = creator.verify()
    assert any("not available yet" in s["reason"] for s in out["skipped"]) and not runs
    clock.t += UNSERVED_GRACE_MS + 1
    out = creator.verify()
    assert [r["check"] for r in out["verified"]] == ["fail (its deliverable is not served)"]
    assert creator.world().items[claim]["check_result"] == "fail"
    creator.store.put_blob(f.read_bytes())  # the builder comes back before the window closes and serves it
    clock.t += UNSERVED_RETRY_MS + 1
    creator.verify()
    assert runs == [cid] and creator.world().items[claim]["check_result"] == "pass"


def test_a_coordinating_hub_does_not_serve_others_files_over_direct_paths_either(net):
    hub, bob = net["hub"], net["bob"]
    cid = publish(bob, net["mission"], "notes.md", b"# notes\n")
    bob.sync()
    hub.store.put_blob(b"# notes\n")
    serve_path = holepunch.server_for(hub)
    assert serve_path(f"/blob/{cid}")[0] == 404 and serve_path(f"/manifest/{cid}")[0] == 404
    own = publish(hub, net["mission"], "rules.md", b"# rules\n")
    assert serve_path(f"/blob/{own}") == (200, b"# rules\n")
    assert holepunch.server_for(bob)(f"/blob/{cid}") == (200, b"# notes\n")  # agents seed what they hold
