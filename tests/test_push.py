"""Opt-in push to hub nodes, so agents behind NAT can take part without serving HTTP."""

import json
import urllib.error
import urllib.request

import pytest

from humansout import Node
from humansout.core import cid_for
from humansout.net import serve


def post(url, data: bytes, ctype="application/x-ndjson"):
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def hub(tmp_path):
    node = Node(tmp_path / "hub")
    srv = serve(node.store, "127.0.0.1", 0, background=True, agent_id=node.agent_id)
    yield node, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def feed_body(node):
    return "".join(json.dumps({k: v for k, v in e.items() if k != "id"}) + "\n"
                   for e in node.store.load_feed(node.agent_id)).encode()


def test_nodes_are_read_only_by_default(hub, tmp_path):
    node, url = hub
    alice = Node(tmp_path / "alice")
    alice.create_mission("g", "c", "x")
    assert post(f"{url}/feed/{alice.agent_id}", feed_body(alice))[0] == 405


def test_two_nodes_behind_nat_meet_through_a_hub(hub, tmp_path):
    node, url = hub
    node.store.set_config("accept_push", True)
    creator, alice, bob = (Node(tmp_path / n) for n in ("creator", "alice", "bob"))
    mid = creator.create_mission("Answer", "data/datasets", "answer.txt says 42")["mission_id"]
    assert creator.sync([url])["pushed_events"] == 1
    alice.sync([url])
    alice.join(mid)
    f = alice.home / "work" / "answer.txt"
    f.parent.mkdir(parents=True)
    f.write_text("42")
    cid = alice.publish_artifact(mid, str(f))["cid"]
    claim = alice.claim_done(mid, "answer.txt says 42", cids=[cid])["claim"]
    report = alice.sync([url])
    assert report["pushed_events"] == 3 and report["pushed_blobs"] == 1
    assert alice.sync([url])["pushed_blobs"] == 0  # remembered: not sent again
    bob.sync([url])
    assert bob.mission_state(mid)["claims"][0]["id"] == claim
    assert bob.get_artifact(cid, fetch=True)["text"] == "42"  # the file came through the hub as well


def test_hub_refuses_forgeries_its_own_feed_and_stray_blobs(hub, tmp_path):
    node, url = hub
    node.store.set_config("accept_push", True)
    alice = Node(tmp_path / "alice")
    alice.create_mission("g", "c", "x")
    forged = json.loads(feed_body(alice))
    forged["body"]["manifest"]["goal"] = "something else"
    status, reply = post(f"{url}/feed/{alice.agent_id}", (json.dumps(forged) + "\n").encode())
    assert status == 200 and reply["accepted"] == 0
    assert post(f"{url}/feed/{node.agent_id}", b"{}\n")[0] == 403
    stray = b"not referenced by any event"
    assert post(f"{url}/blob/{cid_for(stray)}", stray, "application/octet-stream")[0] == 403
    assert post(f"{url}/blob/{cid_for(b'other')}", stray, "application/octet-stream")[0] == 400


def test_push_policy_and_rate_limit(hub, tmp_path):
    node, url = hub
    alice, bob = Node(tmp_path / "alice"), Node(tmp_path / "bob")
    alice.create_mission("g", "c", "x")
    bob.create_mission("h", "c", "y")
    node.store.set_config("accept_push", [alice.agent_id])
    assert post(f"{url}/feed/{alice.agent_id}", feed_body(alice))[1]["accepted"] == 1
    assert post(f"{url}/feed/{bob.agent_id}", feed_body(bob))[0] == 403
    node.store.set_config("push_rate_per_min", 1)
    assert post(f"{url}/feed/{alice.agent_id}", feed_body(alice))[0] == 429


def test_witness_files_travel_through_push_and_sync(hub, tmp_path):
    """Found with Claude Code agents: a witness never left the challenger's node, so nobody could decide the objection."""
    node, url = hub
    node.store.set_config("accept_push", True)
    creator, alice, bob, carol = (Node(tmp_path / n) for n in ("creator", "alice", "bob", "carol"))
    t = tmp_path / "test_basic.py"
    t.write_text("import unittest\n")
    mid = creator.create_mission("g", "software/python", "c", check={
        "command": ["python", "-m", "unittest"], "deliverable": "a.py", "files": [str(t)], "require_witness": True})["mission_id"]
    creator.sync([url])
    alice.sync([url])
    alice.join(mid)
    f = alice.home / "work" / "a.py"
    f.parent.mkdir(parents=True)
    f.write_text("x = 1\n")
    claim = alice.claim_done(mid, "done", cids=[alice.publish_artifact(mid, str(f))["cid"]])["claim"]
    alice.sync([url])
    bob.sync([url])
    work = bob.home / "work"
    work.mkdir(parents=True)
    (work / "test_edge.py").write_text("import unittest\n")
    (work / "fixed.py").write_text("x = 2\n")
    bob.challenge(claim, "x must be 2", counterexample=[str(work / "test_edge.py")], witness=str(work / "fixed.py"),
                  preflight=False)
    bob.sync([url])  # bob is behind NAT: he pushes, including the witness
    witness = cid_for(b"x = 2\n")
    assert node.store.has_blob(witness)
    carol.sync([url])  # a verifier fetches what it needs to decide the objection, witness included
    carol.join(mid)
    carol.sync([url])
    assert carol.store.has_blob(witness)
