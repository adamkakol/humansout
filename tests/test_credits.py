"""Credits and bounties: earn by working on others' missions, spend to get your own done."""

import json

import pytest

from humansout import HumansOutError, Node
from humansout.cli import main


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
            if src is not dst:
                for author in src.store.feed_authors():
                    if author != dst.agent_id:
                        dst.store.ingest(author, src.store.load_feed(author))


@pytest.fixture
def net(tmp_path):
    clock = Clock()
    n = {name: Node(tmp_path / name, clock=clock) for name in ("faucet", "alice", "bob", "carol")}
    for node in n.values():  # every node runs the same policy: only the faucet mints
        node.store.set_config("trusted_creators", [n["faucet"].agent_id])
    test = tmp_path / "test_basic.py"
    test.write_text("import unittest\n")
    return clock, n, tmp_path, test


def runnable(node, test, **kw):
    return node.create_mission("Write slugify(text)", "software/python", "ASCII slugs", challenge_window=100,
                               dispute_window=5, check={"command": ["python", "-m", "unittest"], "deliverable": "slugify.py",
                                                        "files": [str(test)]}, **kw)["mission_id"]


def build(n, who, mid, tmp_path, tag):
    if n[who].agent_id not in [p["agent"] for p in n[who].mission_state(mid)["participants"]]:
        n[who].join(mid)
    f = tmp_path / f"{who}-{tag}.py"
    f.write_text(f"# {tag}\n")
    cid = n[who].publish_artifact(mid, str(f), "slugify.py")["cid"]
    claim = n[who].claim_done(mid, tag, cids=[cid])["claim"]
    share(*n.values())
    for node in n.values():  # every node ran the mission's check on it: it passes
        node.store.save_verification(node.world().items[claim]["check_key"], {"result": "pass"})
    return claim


def edge_test(tmp_path, who):
    """A counterexample file: what the claimed deliverable gets wrong."""
    f = tmp_path / f"{who}-tests" / "test_edge.py"
    f.parent.mkdir(exist_ok=True)
    f.write_text("import unittest\n")
    return str(f)


def reproduce(nodes, challenge_id):
    for node in nodes:
        node.store.save_verification(node.world().items[challenge_id]["vkey"], {"result": "fail"})


def credits(node, who):
    return node.world().credit_view(who.agent_id)


def test_trusted_missions_mint_credits_others_do_not(net):
    clock, n, tmp, test = net
    trusted = runnable(n["faucet"], test)
    untrusted = runnable(n["carol"], test)
    share(*n.values())
    c1 = build(n, "alice", trusted, tmp, "v1")
    build(n, "alice", untrusted, tmp, "v1")
    share(*n.values())
    clock.advance(1)
    ch = n["bob"].challenge(c1, "fails", counterexample=[edge_test(tmp, "bob")], preflight=False)
    share(*n.values())
    reproduce(n.values(), ch["challenge"])
    clock.advance(1)
    c2 = build(n, "alice", trusted, tmp, "v2")
    share(*n.values())
    clock.advance(200)
    view = n["carol"]
    assert credits(view, n["bob"])["credits"] == 2      # a reproduced find in a trusted mission
    assert credits(view, n["alice"])["credits"] == 1    # one accepted build in a trusted mission, none in carol's
    assert view.world().items[c2]["credits"] == 1


def test_bounty_needs_credits(net):
    clock, n, tmp, test = net
    with pytest.raises(HumansOutError, match="not enough credits for a bounty of 5"):
        runnable(n["carol"], test, bounty={"build": 5})


def test_earn_then_spend_on_a_bounty(net):
    clock, n, tmp, test = net
    for node in n.values():
        node.store.set_config("credits_per_build", 10)
    faucet = runnable(n["faucet"], test, rules={"audit_window": 0})  # refutable only in its windows
    share(*n.values())
    build(n, "alice", faucet, tmp, "faucet-work")
    share(*n.values())
    clock.advance(101)
    assert credits(n["alice"], n["alice"])["credits"] == 10
    clock.advance(106)  # past the window a last objection would need to resolve: the credits are free

    mid = runnable(n["alice"], test, bounty={"build": 6, "find": 2, "max_finds": 2})
    share(*n.values())
    assert credits(n["bob"], n["alice"]) == {"agent": n["alice"].agent_id, "credits": 10, "locked": 10, "held": 0,
                                             "available": 0}
    first = build(n, "bob", mid, tmp, "v1")
    share(*n.values())
    clock.advance(1)
    ch = n["carol"].challenge(first, "Łódź", counterexample=[edge_test(tmp, "carol")], preflight=False)
    share(*n.values())
    reproduce(n.values(), ch["challenge"])
    clock.advance(6)  # dispute window over: the finder is paid from the bounty
    assert credits(n["faucet"], n["carol"])["credits"] == 2
    second = build(n, "bob", mid, tmp, "v2")
    share(*n.values())
    clock.advance(101)
    world = n["faucet"].world()
    assert world.missions[mid]["status"] == "done"
    assert world.items[second]["credits"] == 6
    assert credits(n["faucet"], n["bob"])["credits"] == 6
    assert credits(n["faucet"], n["alice"]) == {"agent": n["alice"].agent_id, "credits": 2, "locked": 0, "held": 0,
                                                "available": 2}
    assert world.missions[mid]["bounty"]["closed"] == "done"
    kinds = [e["kind"] for e in world.credit_log if e["mission"] == mid]
    assert kinds == ["find", "build", "refund (done)"]


def test_unclaimed_bounty_returns_after_expiry(net):
    clock, n, tmp, test = net
    for node in n.values():
        node.store.set_config("start_credits", 10)
    runnable(n["alice"], test, bounty={"build": 7, "expires": 50})
    share(*n.values())
    assert credits(n["bob"], n["alice"])["available"] == 3
    clock.advance(51)
    assert credits(n["bob"], n["alice"])["available"] == 10


def test_work_submitted_in_time_is_paid_after_expiry(net):
    clock, n, tmp, test = net
    for node in n.values():
        node.store.set_config("start_credits", 10)
    mid = runnable(n["alice"], test, bounty={"build": 7, "expires": 50})
    share(*n.values())
    clock.advance(10)
    claim = build(n, "bob", mid, tmp, "v1")  # window: 100 s, so acceptance comes after expiry
    share(*n.values())
    clock.advance(60)
    assert credits(n["carol"], n["alice"])["locked"] == 7  # still held for the claim in review
    clock.advance(60)
    world = n["carol"].world()
    assert world.items[claim]["status"] == "accepted" and world.items[claim]["credits"] == 7
    assert credits(n["carol"], n["bob"])["credits"] == 17


def test_refused_events_are_never_written(net):
    clock, n, tmp, test = net
    before = n["carol"].store.feed_len(n["carol"].agent_id)
    with pytest.raises(HumansOutError, match="refused \\(nothing was written\\)"):
        runnable(n["carol"], test, bounty={"build": 5})
    assert n["carol"].store.feed_len(n["carol"].agent_id) == before
    n["carol"].store.set_config("start_credits", 50)  # a later policy change cannot revive the refused event
    assert n["carol"].missions() == []


def test_cli_bounty_and_credits(tmp_path, capsys):
    home = str(tmp_path / "me")
    test = tmp_path / "test_basic.py"
    test.write_text("import unittest\n")
    assert main(["--home", home, "mission", "create", "--goal", "g", "--category", "software/python", "--criteria", "c",
                 "--bounty-build", "3"]) == 1
    assert "not enough credits" in capsys.readouterr().err
    assert main(["--home", home, "config", "set", "start_credits", "5"]) == 0
    capsys.readouterr()
    assert main(["--home", home, "mission", "create", "--goal", "g", "--category", "software/python", "--criteria", "c",
                 "--bounty-build", "3"]) == 0
    capsys.readouterr()
    assert main(["--home", home, "credits"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["credits"], out["locked"], out["available"]) == (5, 3, 2)
    assert main(["--home", home, "missions"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["bounty"]["build"] == 3


def test_report_measures_time_to_first_credit_and_holds_nothing_private(net, capsys):
    from humansout import report
    clock, n, tmp, test = net
    mid = runnable(n["faucet"], test)
    share(*n.values())
    clock.advance(600)  # alice starts working ten minutes after her node was created... by the clock of her events
    build(n, "alice", mid, tmp, "v1")
    share(*n.values())
    clock.advance(101)
    r = report.build(n["alice"])
    assert (r["claims"], r["accepted"], r["credits"]) == (1, 1, 1)
    assert r["minutes_to_first_credit"] is not None and r["minutes_to_first_credit"] >= r["minutes_to_first_claim"]
    text = json.dumps(r)
    for private in (str(tmp), n["alice"].agent_id, n["faucet"].agent_id, mid):
        assert private not in text
    assert main(["--home", str(n["alice"].home), "report", "--markdown"]) == 0
    assert "minutes to first credit" in capsys.readouterr().out


def test_credits_from_refutable_work_cannot_be_spent_yet(net):
    """Found by the security-review swarm (round 3): credits from an accepted claim could be spent on a bounty at
    once, and a second identity collected them before a refutation in the audit window clawed them back."""
    clock, n, tmp, test = net
    for node in n.values():
        node.store.set_config("credits_per_build", 10)
    faucet = runnable(n["faucet"], test, rules={"audit_window": 3600})
    share(*n.values())
    build(n, "alice", faucet, tmp, "faucet-work")
    share(*n.values())
    clock.advance(101)
    view = credits(n["alice"], n["alice"])
    assert view["credits"] == 10 and view["held"] == 10 and view["available"] == 0
    with pytest.raises(HumansOutError, match="held until its audit window ends"):
        runnable(n["alice"], test, bounty={"build": 6})
    clock.advance(3600 + 106)  # the audit window and a last objection's windows are over
    assert credits(n["alice"], n["alice"])["available"] == 10
    runnable(n["alice"], test, bounty={"build": 6})
