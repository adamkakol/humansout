"""Protocol 0.2: runnable counterexamples, meaning disputes and rulings.

These tests inject verification results directly (as if the node had re-run the
check), so they need no container engine. tests/test_e2e_sandbox.py runs the same
flow for real in the sandbox over HTTP.
"""

import json

import pytest

from humansout import HumansOutError, Node
from humansout.mcp_server import _tools, handle


class Clock:
    def __init__(self, start=1_800_000_000_000):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += int(seconds * 1000)


def share(*nodes):
    for src in nodes:
        for dst in nodes:
            if src is dst:
                continue
            for author in src.store.feed_authors():
                if author != dst.agent_id:
                    dst.store.ingest(author, src.store.load_feed(author))


def mark(node, challenge_id, result):
    """Record what re-running this counterexample gave on `node`: pass, fail or flaky."""
    key = node.world().items[challenge_id]["vkey"]
    node.store.save_verification(key, {"result": result})


def mark_all(nodes, challenge_id, result):
    for n in nodes:
        mark(n, challenge_id, result)


@pytest.fixture
def net(tmp_path):
    clock = Clock()
    nodes = {name: Node(tmp_path / name, clock=clock) for name in ("creator", "alice", "bob", "carol")}
    test = tmp_path / "test_basic.py"
    test.write_text("import unittest\nfrom slugify import slugify\n\n"
                    "class T(unittest.TestCase):\n    def test_basic(self):\n"
                    "        self.assertEqual(slugify('Hello World'), 'hello-world')\n")
    mid = nodes["creator"].create_mission(
        "Write slugify(text)", "software/python", "lower-case ASCII slugs; all tests pass",
        challenge_window=100, dispute_window=5,
        check={"command": ["python", "-m", "unittest", "discover", "-s", "/work"],
               "deliverable": "slugify.py", "files": [str(test)]})["mission_id"]
    share(*nodes.values())
    return clock, nodes, mid, tmp_path


def claim(n, mid, tmp_path, name="v1", text="def slugify(s):\n    return s.lower().replace(' ', '-')\n"):
    path = tmp_path / f"slugify_{name}.py"
    path.write_text(text)
    if n["alice"].agent_id not in [p["agent"] for p in n["alice"].mission_state(mid)["participants"]]:
        n["alice"].join(mid)
    cid = n["alice"].publish_artifact(mid, str(path), "slugify.py")["cid"]
    out = n["alice"].claim_done(mid, f"{name}: basic test passes", cids=[cid])
    share(*n.values())
    for node in n.values():  # every node has run the mission's own check on it: it passes
        node.store.save_verification(node.world().items[out["claim"]]["check_key"], {"result": "pass"})
    return out["claim"]


def counterexample(n, who, claim_id, tmp_path, body="self.assertEqual(slugify('Łódź'), 'lodz')", name="test_polish.py"):
    folder = tmp_path / who  # the check sees the basename, so each challenger writes in its own folder
    folder.mkdir(exist_ok=True)
    path = folder / name
    path.write_text("import unittest\nfrom slugify import slugify\n\n"
                    f"class T(unittest.TestCase):\n    def test_case(self):\n        {body}\n")
    out = n[who].challenge(claim_id, "Polish letters: Łódź must become lodz", counterexample=[str(path)], preflight=False)
    share(*n.values())
    return out


def state_of(node, item_id):
    return node.world().item_view(item_id)


def test_check_is_part_of_the_mission(net):
    clock, n, mid, _ = net
    state = n["bob"].mission_state(mid)
    assert state["check"]["deliverable"] == "slugify.py"
    assert state["check"]["files"][0]["name"] == "test_basic.py"
    assert state["dispute_window_s"] == 5
    assert n["bob"].missions()[0]["runnable_check"] is True


def test_invalid_check_is_refused(net):
    clock, n, _, tmp_path = net
    with pytest.raises(HumansOutError, match="deliverable must be a plain file name"):
        n["creator"].create_mission("x", "software/python", "y",
                                    check={"command": ["true"], "deliverable": "../escape.py"})
    with pytest.raises(HumansOutError, match="needs a command and a deliverable"):
        n["creator"].create_mission("x", "software/python", "y", check={"command": ["true"]})


def test_reproduced_counterexample_refutes_before_the_window_ends(net):
    clock, n, mid, tmp_path = net
    c = claim(n, mid, tmp_path)
    clock.advance(1)
    ch = counterexample(n, "bob", c, tmp_path)
    assert ch["kind"] == "counterexample"
    assert state_of(n["creator"], c)["verification_status"] == "awaiting_verification"
    mark_all(n.values(), ch["challenge"], "fail")
    view = state_of(n["creator"], c)
    assert view["status"] == "pending" and view["verification_status"] == "failing"
    clock.advance(6)  # dispute window (5 s) is over; the claim window (100 s) is not
    for node in n.values():
        world = node.world()
        assert world.items[c]["status"] == "refuted"
        assert world.items[ch["challenge"]]["status"] == "upheld"
        assert world.balance(n["alice"].agent_id, "software/python") == 9
        assert world.balance(n["bob"].agent_id, "software/python") == 10 + 1 + 8  # stake + find points
    row = {r["agent"]: r for r in n["creator"].ranking("software/python")["software/python"]}
    assert row[n["bob"].agent_id]["found"] == 1 and row[n["alice"].agent_id]["refuted"] == 1


def test_counterexample_that_does_not_reproduce_costs_the_challenger(net):
    clock, n, mid, tmp_path = net
    c = claim(n, mid, tmp_path)
    clock.advance(1)
    ch = counterexample(n, "bob", c, tmp_path, body="self.assertEqual(slugify('A B'), 'a-b')")
    mark_all(n.values(), ch["challenge"], "pass")
    clock.advance(6)
    world = n["creator"].world()
    assert world.items[ch["challenge"]]["status"] == "rejected"
    assert "not reproduced" in world.items[ch["challenge"]]["reason"]
    assert world.items[c]["status"] == "pending"  # the claim still waits for its own window
    clock.advance(100)
    world = n["creator"].world()
    assert world.items[c]["status"] == "accepted"
    assert world.missions[mid]["status"] == "done"
    assert world.balance(n["alice"].agent_id, "software/python") == 10 + 1 + 5
    assert world.balance(n["bob"].agent_id, "software/python") == 9


def test_flaky_counterexample_does_not_count(net):
    clock, n, mid, tmp_path = net
    c = claim(n, mid, tmp_path)
    clock.advance(1)
    ch = counterexample(n, "bob", c, tmp_path)
    mark_all(n.values(), ch["challenge"], "flaky")
    clock.advance(6)
    item = n["creator"].world().items[ch["challenge"]]
    assert item["status"] == "rejected" and "flaky" in item["reason"]


def test_unverified_counterexample_blocks_settlement(net):
    clock, n, mid, tmp_path = net
    c = claim(n, mid, tmp_path)
    clock.advance(1)
    ch = counterexample(n, "bob", c, tmp_path)
    clock.advance(200)  # far past every window, but this node never re-ran it
    view = state_of(n["creator"], c)
    assert view["status"] == "pending" and view["verification_status"] == "awaiting_verification"
    assert n["creator"].mission_state(mid)["awaiting_verification"] == [ch["challenge"]]
    mark(n["creator"], ch["challenge"], "fail")
    assert state_of(n["creator"], c)["status"] == "refuted"


def test_meaning_dispute_ruled_by_the_creator(net):
    clock, n, mid, tmp_path = net
    c = claim(n, mid, tmp_path)
    clock.advance(1)
    ch = counterexample(n, "carol", c, tmp_path, body="self.assertEqual(slugify('Łódź'), 'łódź')")
    mark_all(n.values(), ch["challenge"], "fail")  # it does fail: the code returns ASCII
    clock.advance(1)
    with pytest.raises(HumansOutError, match="only the challenged claim's author"):
        n["bob"].dispute(ch["challenge"], "not mine to dispute")
    n["alice"].dispute(ch["challenge"], "The mission asks for ASCII slugs; keeping ł contradicts it")
    share(*n.values())
    clock.advance(10)  # past the dispute window: without a ruling it waits for the challenge window
    assert n["creator"].world().items[ch["challenge"]]["status"] == "pending"
    with pytest.raises(HumansOutError, match="only the mission creator"):
        n["alice"].rule(ch["challenge"], "rejected", "I wrote the code")
    n["creator"].rule(ch["challenge"], "rejected", "ASCII slugs were asked for")
    share(*n.values())
    world = n["bob"].world()
    item = world.items[ch["challenge"]]
    assert item["status"] == "rejected" and item["reason"] == "ruled rejected by the mission creator"
    assert world.balance(n["carol"].agent_id, "software/python") == 9
    clock.advance(100)
    world = n["bob"].world()
    assert world.items[c]["status"] == "accepted"
    assert world.balance(n["alice"].agent_id, "software/python") == 10 + 1 + 5


def test_dispute_without_ruling_lets_the_failing_test_stand(net):
    clock, n, mid, tmp_path = net
    c = claim(n, mid, tmp_path)
    clock.advance(1)
    ch = counterexample(n, "bob", c, tmp_path)
    mark_all(n.values(), ch["challenge"], "fail")
    clock.advance(1)
    n["alice"].dispute(ch["challenge"], "I think Ł is out of scope")
    share(*n.values())
    clock.advance(50)
    assert state_of(n["creator"], c)["status"] == "pending"
    clock.advance(60)  # the counterexample's own window closes with no ruling
    assert state_of(n["creator"], c)["status"] == "refuted"


def test_dispute_after_the_window_is_refused(net):
    clock, n, mid, tmp_path = net
    c = claim(n, mid, tmp_path)
    clock.advance(1)
    ch = counterexample(n, "bob", c, tmp_path)
    clock.advance(6)
    with pytest.raises(HumansOutError, match="dispute window is closed"):
        n["alice"].dispute(ch["challenge"], "too late")


def test_only_the_first_reproduced_counterexample_is_rewarded(net):
    clock, n, mid, tmp_path = net
    c = claim(n, mid, tmp_path)
    clock.advance(1)
    first = counterexample(n, "bob", c, tmp_path)
    clock.advance(1)
    second = counterexample(n, "carol", c, tmp_path, body="self.assertEqual(slugify('Ł'), 'l')", name="test_l.py")
    mark_all(n.values(), first["challenge"], "fail")
    mark_all(n.values(), second["challenge"], "fail")
    clock.advance(4.5)  # bob's dispute window is over, carol's is not
    world = n["creator"].world()
    assert world.items[c]["refuted_by"] == first["challenge"]
    assert world.items[second["challenge"]]["status"] == "moot"
    assert world.balance(n["bob"].agent_id, "software/python") == 19
    assert world.balance(n["carol"].agent_id, "software/python") == 10
    assert world.available(n["carol"].agent_id, "software/python") == 10


def test_fix_after_a_counterexample(net):
    clock, n, mid, tmp_path = net
    c1 = claim(n, mid, tmp_path)
    clock.advance(1)
    ch = counterexample(n, "bob", c1, tmp_path)
    mark_all(n.values(), ch["challenge"], "fail")
    clock.advance(1)
    c2 = claim(n, mid, tmp_path, "v2", "def slugify(s):\n    return s.lower().replace('ł', 'l').replace(' ', '-')\n")
    clock.advance(110)
    world = n["creator"].world()
    assert world.items[c1]["status"] == "refuted"
    assert world.items[c2]["status"] == "accepted"
    assert world.missions[mid]["status"] == "done"


def test_counterexample_rules(net):
    clock, n, mid, tdir = net
    plain = n["creator"].create_mission("Collect idioms", "data/datasets", "100 idioms", challenge_window=50)["mission_id"]
    share(*n.values())
    n["alice"].join(plain)
    plain_claim = n["alice"].claim_done(plain, "csv attached")
    share(*n.values())
    with pytest.raises(HumansOutError, match="no runnable check"):
        n["bob"].challenge(plain_claim["claim"], "wrong", counterexample=[], preflight=False)
    c = claim(n, mid, tdir)
    clash = tdir / "test_basic.py"  # same name as a check file
    with pytest.raises(HumansOutError, match="collides with the check"):
        n["bob"].challenge(c, "overwrite the check", counterexample=[str(clash)], preflight=False)
    ch = n["bob"].challenge(c, "the mission's own check fails", counterexample=[], preflight=False)
    share(*n.values())
    with pytest.raises(HumansOutError, match="settled by re-running"):
        n["carol"].challenge(ch["challenge"], "this challenge is wrong")
    with pytest.raises(HumansOutError, match="name your deliverable"):  # nothing could re-run or break it
        n["alice"].claim_done(mid, "fixed, trust me")


def test_runnable_missions_refuse_written_objections(net):
    """Found in the prompt-injection test: an unanswered "I just think it is wrong" refuted work that
    passed every test and paid the objector. Where a check can run, objections must run too."""
    clock, n, mid, tmp_path = net
    c = claim(n, mid, tmp_path)
    clock.advance(1)
    with pytest.raises(HumansOutError, match="object with a counterexample"):
        n["bob"].challenge(c, "I just think it is wrong")
    clock.advance(200)
    assert n["creator"].world().items[c]["status"] == "accepted"


def test_text_challenges_keep_the_v01_economics_without_a_check(net):
    clock, n, _, tmp_path = net
    plain = n["creator"].create_mission("Collect idioms", "data/datasets", "100 idioms", challenge_window=100)["mission_id"]
    share(*n.values())
    n["alice"].join(plain)
    c = n["alice"].claim_done(plain, "idioms.csv attached")["claim"]
    share(*n.values())
    clock.advance(1)
    n["bob"].challenge(c, "half of them are not idioms")  # nothing to run: a written objection is the only kind
    share(*n.values())
    clock.advance(200)
    world = n["creator"].world()
    assert world.items[c]["status"] == "refuted"
    assert world.balance(n["bob"].agent_id, "data/datasets") == 11  # the stake only, no find points


def test_cli_runnable_mission_and_counterexample(tmp_path, capsys):
    from humansout.cli import main
    test = tmp_path / "test_basic.py"
    test.write_text("import unittest\n")
    home_c, home_a, home_b = (str(tmp_path / x) for x in "cab")
    assert main(["--home", home_c, "mission", "create", "--goal", "slugify", "--category", "software/python",
                 "--criteria", "tests pass", "--check-cmd", "python -m unittest discover -s /work",
                 "--deliverable", "slugify.py", "--check-file", str(test), "--dispute-window", "30"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["runnable_check"] is True
    assert main(["--home", home_c, "mission", "create", "--goal", "g", "--category", "c", "--criteria", "x",
                 "--check-cmd", "pytest"]) == 1
    assert "needs both --check-cmd and --deliverable" in capsys.readouterr().err
    creator, alice, bob = Node(home_c), Node(home_a), Node(home_b)
    share(creator, alice, bob)
    mid = created["mission_id"]
    alice.join(mid)
    code = tmp_path / "slugify.py"
    code.write_text("def slugify(s):\n    return s\n")
    cid = alice.publish_artifact(mid, str(code))["cid"]
    claim_id = alice.claim_done(mid, "done", cids=[cid])["claim"]
    share(creator, alice, bob)
    extra = tmp_path / "test_case.py"
    extra.write_text("import unittest\n")
    assert main(["--home", home_b, "challenge", claim_id[:12], "--evidence", "fails case",
                 "--counterexample", str(extra), "--no-preflight"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["kind"] == "counterexample" and "humansout verify" in out["next"]
    state = bob.mission_state(mid)
    assert state["claims"][0]["challenges"][0]["files"][0]["name"] == "test_case.py"


def test_mcp_exposes_the_runnable_protocol(tmp_path):
    node = Node(tmp_path / "m")
    tools = _tools(node)
    assert {"verify", "dispute", "rule"} <= set(tools)
    test = tmp_path / "test_basic.py"
    test.write_text("import unittest\n")
    reply = handle(node, tools, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "create_mission", "arguments": {
            "goal": "slugify", "category": "software/python", "success_criteria": "tests pass",
            "check_command": ["python", "-m", "unittest", "discover", "-s", "/work"],
            "deliverable": "slugify.py", "check_files": [str(test)]}}})
    assert reply["result"]["isError"] is False
    assert json.loads(reply["result"]["content"][0]["text"])["runnable_check"] is True


def test_claims_are_accepted_only_after_their_own_check_passes(net):
    """Found by asking 'is it easy to cheat?': junk that failed the mission's check was accepted, and paid,
    when nobody objected. Every node now runs the mission's check on every claim before accepting it."""
    clock, n, mid, tmp_path = net
    junk = tmp_path / "junk.py"
    junk.write_text("this is not even python\n")
    n["alice"].join(mid)
    cid = n["alice"].publish_artifact(mid, str(junk), "slugify.py")["cid"]
    c = n["alice"].claim_done(mid, "trust me", cids=[cid])["claim"]
    share(*n.values())
    creator = n["creator"]
    clock.advance(200)  # far past the window, nobody objected, nobody ran the check: it waits, it is not accepted
    view = state_of(creator, c)
    assert view["status"] == "pending" and view["check"] == "not run yet"
    assert view["verification_status"] == "awaiting_verification"
    assert c in creator.mission_state(mid)["awaiting_verification"]
    before = creator.world().balance(n["alice"].agent_id, "software/python")
    for node in n.values():  # every node runs the check (verify, watch): it fails
        node.store.save_verification(node.world().items[c]["check_key"], {"result": "fail"})
    world = creator.world()
    assert world.items[c]["status"] == "failed_check" and world.items[c]["minted"] == 0
    assert world.balance(n["alice"].agent_id, "software/python") == before - world.items[c]["stake"]
    assert "fails" in world.item_view(c)["reason"]
    fixed = claim(n, mid, tmp_path, name="v2")  # a rejected claim leaves the author free to claim again
    clock.advance(200)
    assert n["creator"].world().items[fixed]["status"] == "accepted"


def test_work_item_claims_in_runnable_missions_need_a_deliverable(net):
    clock, n, mid, tmp_path = net
    clock.advance(1)
    wid = n["creator"].add_work(mid, "District 1")["work_id"]
    share(*n.values())
    n["alice"].join(mid)
    with pytest.raises(HumansOutError, match="name your deliverable"):
        n["alice"].claim_done(mid, "done", work_id=wid)
