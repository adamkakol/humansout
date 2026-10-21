"""Protocol 0.4, the swarm economy: one result per target, intents, vouches, juries and the audit window.

Verification results are injected (as if every node had re-run the check), so no container engine is needed.
"""

import pytest

from humansout import HumansOutError, Node

AGENTS = ("creator", "alice", "bob", "carol", "dave", "erin", "frank")


class Clock:
    def __init__(self, start=1_800_000_000_000):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += int(seconds * 1000)


def share(nodes):
    for src in nodes:
        for dst in nodes:
            if src is not dst:
                for author in src.store.feed_authors():
                    if author != dst.agent_id:
                        dst.store.ingest(author, src.store.load_feed(author))


class Net:
    def __init__(self, tmp_path, **rules):
        self.clock, self.tmp = Clock(), tmp_path
        self.n = {name: Node(tmp_path / name, clock=self.clock) for name in AGENTS}
        test = tmp_path / "test_basic.py"
        test.write_text("import unittest\n")
        self.mid = self.n["creator"].create_mission(
            "Write slugify(text)", "software/python", "lower-case ASCII slugs", challenge_window=100,
            dispute_window=5, points_per_find=8, rules=rules or None,
            check={"command": ["python", "-m", "unittest"], "deliverable": "slugify.py", "files": [str(test)]})["mission_id"]
        self.sync()
        for name in AGENTS[1:]:
            self.tick()
            self.n[name].join(self.mid)
        self.sync()

    def sync(self):
        share(list(self.n.values()))

    def tick(self, seconds=1):
        self.clock.advance(seconds)

    def claim(self, who, text, result="pass"):
        self.tick()
        path = self.tmp / f"{who}-{abs(hash(text))}.py"
        path.write_text(text)
        cid = self.n[who].publish_artifact(self.mid, str(path), "slugify.py")["cid"]
        claim = self.n[who].claim_done(self.mid, "done", cids=[cid])["claim"]
        self.sync()
        if result:  # None: not run yet anywhere
            for node in self.n.values():
                node.store.save_verification(node.world().items[claim]["check_key"], {"result": result})
        return claim

    def counterexample(self, who, claim, result="fail"):
        self.tick()
        folder = self.tmp / f"ce-{who}"
        folder.mkdir(exist_ok=True)
        (folder / "test_edge.py").write_text("import unittest\n")
        (folder / "fixed.py").write_text("x = 2\n")
        ce = self.n[who].challenge(claim, "fails on an edge case", counterexample=[str(folder / "test_edge.py")],
                                   witness=str(folder / "fixed.py"), preflight=False)["challenge"]
        self.sync()
        for node in self.n.values():
            item = node.world().items[ce]
            node.store.save_verification(item["vkey"], {"result": result})
            node.store.save_verification(item["wkey"], {"result": "pass"})
        return ce

    def world(self, who="creator"):
        return self.n[who].world()

    def points(self, who):
        return self.world().balance(self.n[who].agent_id, "software/python")

    def status(self, iid):
        return self.world().items[iid]["status"]


@pytest.fixture
def net(tmp_path):
    return Net(tmp_path)


def test_a_second_correct_claim_for_the_same_target_is_superseded(net):
    first = net.claim("alice", "v = 1")
    second = net.claim("bob", "v = 2")
    net.tick(200)
    assert net.status(first) == "accepted" and net.status(second) == "superseded"
    assert net.points("alice") == 15 and net.points("bob") == 10  # bob's stake came back; nothing was minted


def test_the_first_vouchers_of_accepted_work_earn(net):
    claim = net.claim("alice", "v = 1")
    for who in ("bob", "carol", "dave", "erin"):
        net.tick()
        net.n[who].vouch(claim, "ran the check and tried Unicode, empty and long input")
        net.sync()
    assert net.world().items[claim]["vouches"][0]["stake"] == 1
    net.tick(200)
    assert net.status(claim) == "accepted"
    assert [net.points(w) for w in ("bob", "carol", "dave", "erin")] == [12, 12, 12, 10]  # 3 slots by default
    assert net.world().stats[(net.n["erin"].agent_id, "software/python")]["vouched"] == 1


def test_vouchers_of_refuted_work_pay_the_finder(net):
    claim = net.claim("alice", "v = 1")
    net.tick()
    net.n["bob"].vouch(claim, "looked fine to me")
    net.sync()
    ce = net.counterexample("carol", claim)
    net.tick(10)  # past the dispute window: the reproduced counterexample stands
    assert net.status(claim) == "refuted" and net.status(ce) == "upheld"
    assert net.points("bob") == 9                         # the voucher's stake went to the finder
    assert net.points("carol") == 10 + 1 + 1 + 8          # alice's stake, bob's stake, find points
    assert net.world().stats[(net.n["bob"].agent_id, "software/python")]["vouched_wrong"] == 1


def test_work_that_fails_its_check_is_never_open_for_vouching(net):
    claim = net.claim("alice", "v = 1", result="fail")
    with pytest.raises(HumansOutError, match="window is closed"):
        net.n["bob"].vouch(claim, "late")  # it settled at once: failed_check
    claim2 = net.claim("alice", "v = 3", result=None)
    net.tick()
    net.n["bob"].vouch(claim2, "trusting it before it ran")  # accepted while the check had not run anywhere
    net.sync()
    for node in net.n.values():
        node.store.save_verification(node.world().items[claim2]["check_key"], {"result": "fail"})
    # once the check has run, the claim failed from its first moment: the vouch never counted, either way
    assert net.status(claim2) == "failed_check" and net.world().items[claim2]["vouches"] == []
    assert net.points("bob") == 10


def test_no_vouching_for_your_own_claim_or_one_you_challenged(net):
    claim = net.claim("alice", "v = 1")
    with pytest.raises(HumansOutError, match="your own claim"):
        net.n["alice"].vouch(claim, "mine")
    net.counterexample("carol", claim)
    with pytest.raises(HumansOutError, match="you challenged this claim"):
        net.n["carol"].vouch(claim, "changed my mind")


def test_intents_spread_the_swarm_out(tmp_path):
    net = Net(tmp_path, max_builders=2)
    net.tick()
    net.n["alice"].intend(net.mid, 600)
    net.sync()
    net.tick()
    net.n["bob"].intend(net.mid, 600)
    net.sync()
    net.tick()
    with pytest.raises(HumansOutError, match="already building this mission"):
        net.n["carol"].intend(net.mid, 600)
    assert {b["agent"] for b in net.n["carol"].mission_state(net.mid)["builders"]} == {
        net.n["alice"].agent_id, net.n["bob"].agent_id}
    net.claim("alice", "v = 1")  # a claim ends its author's intent
    net.tick()
    net.n["carol"].intend(net.mid, 600)
    net.tick(700)  # leases run out
    assert net.n["dave"].mission_state(net.mid)["builders"] == []


def disputed(net):
    claim = net.claim("alice", "v = 1")
    ce = net.counterexample("carol", claim)
    net.tick()
    net.n["alice"].dispute(ce, "the test builds a state the real system never produces")
    net.sync()
    jury = net.world().items[ce]["jury"]
    names = {net.n[name].agent_id: name for name in AGENTS}
    return claim, ce, [names[j] for j in jury]


def test_a_jury_of_agents_decides_a_disputed_counterexample(net):
    claim, ce, jurors = disputed(net)
    assert len(jurors) == 3 and not {"alice", "carol", "creator"} & set(jurors)
    first, second, third = jurors
    net.tick()
    net.n[first].vote(ce, "upheld", "it fails")
    net.sync()
    net.tick()
    net.n[second].vote(ce, "rejected", "deletes fields the system always writes")
    net.sync()
    assert net.status(ce) == "pending"
    net.tick()
    net.n[third].vote(ce, "rejected", "not a reachable state")
    net.sync()
    assert net.status(ce) == "rejected"  # a majority of the seats decided at once
    assert net.world().items[ce]["reason"] == "rejected by the jury"
    assert net.points(first) == 9                         # voted against the outcome: stake lost
    assert net.points(second) == net.points(third) == 11  # jury point (the lost stake splits to 0 each)
    assert net.points("carol") == 9 and net.points("alice") == 11  # the challenger's stake went to the author
    net.tick(200)
    assert net.status(claim) == "accepted"


def test_a_jury_majority_can_uphold_the_test(net):
    claim, ce, jurors = disputed(net)
    for who in jurors[:2]:
        net.tick()
        net.n[who].vote(ce, "upheld", "a real failure")
        net.sync()
    assert net.status(ce) == "upheld" and net.status(claim) == "refuted"
    with pytest.raises(HumansOutError):
        net.tick()
        net.n[jurors[2]].vote(ce, "rejected", "too late")


def test_only_seated_jurors_vote(net):
    claim, ce, jurors = disputed(net)
    outsider = next(name for name in AGENTS if name not in jurors + ["alice", "carol"])
    with pytest.raises(HumansOutError, match="do not sit on this jury"):
        net.tick()
        net.n[outsider].vote(ce, "rejected", "me too")


def test_the_jury_is_the_same_on_every_node(net):
    claim, ce, jurors = disputed(net)
    assert len({tuple(node.world().items[ce]["jury"]) for node in net.n.values()}) == 1


def test_vouchers_may_dispute_but_strangers_may_not(net):
    claim = net.claim("alice", "v = 1")
    net.tick()
    net.n["bob"].vouch(claim, "checked it")
    net.sync()
    ce = net.counterexample("carol", claim)
    with pytest.raises(HumansOutError, match="author or one of its vouchers"):
        net.tick()
        net.n["dave"].dispute(ce, "not mine to dispute")
    net.tick()
    net.n["bob"].dispute(ce, "my stake is on it: the test is artificial")
    net.sync()
    assert net.world().items[ce]["jury"]


def test_accepted_work_refuted_in_its_audit_window_costs_everyone_who_let_it_through(net):
    claim = net.claim("alice", "v = 1")
    net.tick()
    net.n["bob"].vouch(claim, "checked it")
    net.sync()
    net.tick(200)
    assert net.status(claim) == "accepted" and net.n["dave"].mission_state(net.mid)["status"] == "done"
    assert net.points("alice") == 15 and net.points("bob") == 12
    ce = net.counterexample("carol", claim)
    assert net.world().items[ce]["late"]
    net.tick(10)
    assert net.status(claim) == "refuted_late" and net.status(ce) == "upheld"
    assert net.points("alice") == 15 - 1 - 5   # stake and minted points
    assert net.points("bob") == 12 - 1 - 2     # stake and vouch points
    assert net.points("carol") == 10 + 6 + 3 + 8
    assert net.n["dave"].mission_state(net.mid)["status"] == "open"  # the mission reopens
    net.claim("dave", "v = 4")                 # and can be done again


def test_the_audit_window_ends(tmp_path):
    net = Net(tmp_path, audit_window=50)
    claim = net.claim("alice", "v = 1")
    net.tick(200)
    assert net.status(claim) == "accepted"
    with pytest.raises(HumansOutError, match="window is closed"):
        net.counterexample("carol", claim)


def test_mission_rules_are_validated(tmp_path):
    node = Node(tmp_path / "x", clock=Clock())
    with pytest.raises(HumansOutError, match="unknown mission rules"):
        node.create_mission("g", "software/python", "c", rules={"jury": 3})
    with pytest.raises(HumansOutError, match="jury_size must be an integer"):
        node.create_mission("g", "software/python", "c", rules={"jury_size": 99})


def test_idle_agents_audit_accepted_work_with_their_own_prompt(net, tmp_path):
    from humansout import watch
    claim = net.claim("alice", "v = 1")
    net.tick(200)
    assert net.status(claim) == "accepted"
    bob = net.n["bob"]
    events = watch.find_work(bob.world(), bob.agent_id, net.clock(), watch.WatchConfig(roles=("break",)),
                             watch.load_state(bob.home))
    assert [(e.kind, e.item, e.detail.get("audit")) for e in events] == [("review", claim, True)]
    prompt = watch.render_prompt(bob, bob.world(), events[0], watch.WatchConfig(), tmp_path)
    assert "was accepted" in prompt and "counterexample posted against other claims" in prompt.replace("\n", " ") or \
        "every counterexample" in prompt
    assert "vouch for it" not in prompt
