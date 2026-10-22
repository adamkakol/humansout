"""Mission budgets (protocol 0.5): what work a budget pays, who pays for being wrong, and where credits come from."""

import pytest

from humansout import HumansOutError, Node


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
    n = {name: Node(tmp_path / name, clock=clock) for name in ("treasury", "alice", "bob", "carol", "dave")}
    for node in n.values():  # every node runs the same policy: the treasury is the only trusted creator
        node.store.set_config("trusted_creators", [n["treasury"].agent_id])
        node.store.set_config("start_credits", 50)
    test = tmp_path / "test_basic.py"
    test.write_text("import unittest\n")
    return clock, n, tmp_path, test


def mission(node, test, budget=None, **rules):
    return node.create_mission("Write slugify(text)", "software/python", "ASCII slugs", challenge_window=100,
                               dispute_window=5, check={"command": ["python", "-m", "unittest"],
                                                        "deliverable": "slugify.py", "files": [str(test)]},
                               bounty=budget, rules={"audit_window": 0, "vouch_slots": 1, **rules})["mission_id"]


def build(n, who, mid, tmp_path, tag, work_id=None):
    if n[who].agent_id not in [p["agent"] for p in n[who].mission_state(mid)["participants"]]:
        n[who].join(mid)
    f = tmp_path / f"{who}-{tag}.py"
    f.write_text(f"# {tag}\n")
    cid = n[who].publish_artifact(mid, str(f), "slugify.py")["cid"]
    claim = n[who].claim_done(mid, tag, work_id=work_id, cids=[cid])["claim"]
    share(*n.values())
    for node in n.values():  # every node ran the mission's check on it: it passes
        node.store.save_verification(node.world().items[claim]["check_key"], {"result": "pass"})
    return claim


def refute(n, who, claim, tmp_path):
    folder = tmp_path / f"{who}-tests"
    folder.mkdir(exist_ok=True)
    (folder / "test_edge.py").write_text("import unittest\n")
    ch = n[who].challenge(claim, "fails on an edge case", counterexample=[str(folder / "test_edge.py")],
                          preflight=False)["challenge"]
    share(*n.values())
    for node in n.values():
        node.store.save_verification(node.world().items[ch]["vkey"], {"result": "fail"})
    return ch


def credits(n, who, view="treasury"):
    return n[view].world().credit_view(n[who].agent_id)


def test_a_budget_pays_funded_items_and_their_reviewers_and_returns_the_rest(net):
    clock, n, tmp, test = net
    mid = mission(n["alice"], test, {"budget": 30, "item": 10, "review": 2, "find": 3, "expires": 1000})
    assert credits(n, "alice", "alice")["locked"] == 30
    one = n["alice"].add_work(mid, "ASCII letters")["work_id"]
    n["alice"].add_work(mid, "Polish letters")
    share(*n.values())
    clock.advance(1)
    claim = build(n, "bob", mid, tmp, "v1", work_id=one)
    n["carol"].join(mid)
    n["carol"].vouch(claim, "ran the tests and tried odd input")
    share(*n.values())
    clock.advance(101)
    world = n["treasury"].world()
    assert world.items[claim]["status"] == "accepted" and world.items[claim]["credits"] == 10
    assert credits(n, "bob")["credits"] == 60 and credits(n, "carol")["credits"] == 52
    assert credits(n, "alice") == {"agent": n["alice"].agent_id, "credits": 38, "locked": 18, "held": 0,
                                   "available": 20}
    clock.advance(1000)  # the budget expires: what it did not pay comes back
    assert credits(n, "alice")["locked"] == 0 and credits(n, "alice")["available"] == 38
    assert [e["kind"] for e in n["treasury"].world().credit_log if e["mission"] == mid] == [
        "item", "review", "refund (expired)"]


def test_items_others_add_are_paid_only_once_the_creator_funds_them(net):
    """Anyone may add work items (scouting), so a budget that paid every item could be emptied by an agent adding
    trivial items and claiming them itself."""
    clock, n, tmp, test = net
    mid = mission(n["alice"], test, {"budget": 30, "item": 10})
    share(*n.values())
    n["bob"].join(mid)
    own = n["bob"].add_work(mid, "an item bob found")["work_id"]
    n["carol"].join(mid)
    scouted = n["carol"].add_work(mid, "an item carol found")["work_id"]
    share(*n.values())
    clock.advance(1)
    first = build(n, "bob", mid, tmp, "v1", work_id=own)
    with pytest.raises(HumansOutError, match="only the mission creator can fund"):
        n["bob"].fund_work(mid, scouted)
    n["alice"].fund_work(mid, scouted)
    share(*n.values())
    clock.advance(1)
    second = build(n, "bob", mid, tmp, "v2", work_id=scouted)
    clock.advance(101)
    world = n["treasury"].world()
    assert world.items[first]["status"] == "accepted" and world.items[first]["credits"] == 0
    assert world.items[second]["credits"] == 10
    assert credits(n, "bob")["credits"] == 60


def test_a_refutation_is_paid_by_the_wrong_claim_never_by_the_budget(net):
    """Two identities could farm a budget that paid finds: one posts a deliberately wrong claim, the other refutes
    it. The finder is paid from the claim's bond, so the pair only passes its own credits around."""
    clock, n, tmp, test = net
    mid = mission(n["alice"], test, {"budget": 30, "item": 10, "find": 5})
    wid = n["alice"].add_work(mid, "ASCII letters")["work_id"]
    share(*n.values())
    clock.advance(1)
    claim = build(n, "bob", mid, tmp, "v1", work_id=wid)
    assert credits(n, "bob")["locked"] == 5  # the bond: the find price, from what bob can spend
    clock.advance(1)
    refute(n, "carol", claim, tmp)
    clock.advance(106)
    world = n["treasury"].world()
    assert world.items[claim]["status"] == "refuted"
    assert credits(n, "carol")["credits"] == 55 and credits(n, "bob") == {
        "agent": n["bob"].agent_id, "credits": 45, "locked": 0, "held": 0, "available": 45}
    assert credits(n, "alice")["locked"] == 30  # the budget paid nothing
    # a newcomer with nothing bonds nothing, so refuting a planted mistake earns the pair nothing
    n["dave"].transfer(n["alice"].agent_id, 50)
    share(*n.values())
    clock.advance(1)
    junk = build(n, "dave", mid, tmp, "junk", work_id=wid)
    assert n["treasury"].world().items[junk].get("bond", 0) == 0
    clock.advance(1)
    refute(n, "carol", junk, tmp)
    clock.advance(106)
    assert credits(n, "carol")["credits"] == 55 and credits(n, "alice")["locked"] == 30


def test_a_late_refutation_pays_the_finder_from_what_the_wrong_work_earned(net):
    clock, n, tmp, test = net
    mid = mission(n["alice"], test, {"budget": 40, "item": 10, "review": 2, "find": 4}, audit_window=3600)
    wid = n["alice"].add_work(mid, "ASCII letters")["work_id"]
    share(*n.values())
    clock.advance(1)
    claim = build(n, "bob", mid, tmp, "v1", work_id=wid)
    n["carol"].join(mid)
    n["carol"].vouch(claim, "looked fine to me")
    share(*n.values())
    clock.advance(101)
    assert credits(n, "bob")["held"] == 10 and credits(n, "carol")["held"] == 2
    assert credits(n, "alice")["locked"] == 28
    refute(n, "dave", claim, tmp)  # inside the audit window
    clock.advance(10)
    world = n["treasury"].world()
    assert world.items[claim]["status"] == "refuted_late"
    assert credits(n, "bob")["credits"] == 50 and credits(n, "carol")["credits"] == 50
    assert credits(n, "dave")["credits"] == 54  # the find price, out of the 12 the wrong work earned
    assert credits(n, "alice")["locked"] == 36  # the other 8 went back into the budget, for the redone item
    second = build(n, "carol", mid, tmp, "v2", work_id=wid)
    clock.advance(101 + 3600 + 106)
    assert n["treasury"].world().items[second]["credits"] == 10


def test_a_treasury_issues_credits_only_through_budgets_under_its_weekly_cap(net):
    """Where credits come from: a trusted creator's treasury funds budgets by minting, at most its weekly cap
    committed in any 7 days; what a budget does not pay is never minted."""
    clock, n, tmp, test = net
    with pytest.raises(HumansOutError, match="only a trusted creator can run a treasury"):
        n["bob"].treasury(1000)
    n["treasury"].treasury(100)
    mid = mission(n["treasury"], test, {"budget": 60, "item": 20, "expires": 500})
    with pytest.raises(HumansOutError, match="weekly cap is 100 credits and 60 are committed"):
        mission(n["treasury"], test, {"budget": 50, "item": 10}, max_builders=3)
    with pytest.raises(HumansOutError, match="a treasury funds only budgets"):
        mission(n["treasury"], test, {"build": 5, "find": 2, "max_finds": 3})
    wid = n["treasury"].add_work(mid, "ASCII letters")["work_id"]
    share(*n.values())
    clock.advance(1)
    claim = build(n, "bob", mid, tmp, "v1", work_id=wid)
    clock.advance(101)
    assert n["treasury"].world().items[claim]["credits"] == 20
    clock.advance(500)
    economy = n["carol"].world().economy()
    assert economy["issued_total"] == 20 and credits(n, "bob")["credits"] == 70  # the unpaid 40 were never minted
    assert economy["treasury_budgets_open"] == 0
    assert economy["treasuries"][n["treasury"].agent_id]["weekly_cap"] == 100
    assert credits(n, "treasury")["credits"] == 50  # its own credits paid nothing
    clock.advance(7 * 86400)
    mission(n["treasury"], test, {"budget": 50, "item": 10}, max_builders=3)  # a new week


def test_after_its_treasury_a_creator_mints_credits_only_through_budgets(net):
    clock, n, tmp, test = net
    before = mission(n["treasury"], test)  # before 0.5: trusted missions minted a credit per accepted build
    share(*n.values())
    first = build(n, "bob", before, tmp, "v1")
    clock.advance(101)
    assert n["treasury"].world().items[first]["credits"] == 1
    n["treasury"].treasury(100)
    after = mission(n["treasury"], test, max_builders=3)
    share(*n.values())
    second = build(n, "carol", after, tmp, "v1")
    clock.advance(101)
    world = n["treasury"].world()
    assert world.items[second]["credits"] == 0 and world.items[second]["minted"] > 0  # points still


def test_transfers_move_only_available_credits(net):
    clock, n, tmp, test = net
    mid = mission(n["alice"], test, {"budget": 10, "item": 10}, audit_window=600)
    wid = n["alice"].add_work(mid, "ASCII letters")["work_id"]
    share(*n.values())
    clock.advance(1)
    build(n, "bob", mid, tmp, "v1", work_id=wid)
    clock.advance(101)
    assert credits(n, "bob", "bob")["held"] == 10
    with pytest.raises(HumansOutError, match="not enough available credits for 60"):
        n["bob"].transfer(n["dave"].agent_id, 60)
    for bad, match in ((n["bob"].agent_id, "another agent"), ("ab" * 31, "full agent id")):
        with pytest.raises(HumansOutError, match=match):
            n["bob"].transfer(bad, 1)
    n["bob"].transfer(n["dave"].agent_id, 50, memo="to my main wallet")
    clock.advance(600 + 106)  # the claim can no longer be refuted: its credits are free
    n["bob"].transfer(n["dave"].agent_id, 10)
    share(*n.values())
    assert credits(n, "dave")["credits"] == 110 and credits(n, "bob")["credits"] == 0
    kinds = [(e["kind"], e["amount"]) for e in n["treasury"].world().credit_log if e["kind"] == "transfer"]
    assert kinds == [("transfer", 50), ("transfer", 10)]


@pytest.mark.parametrize("budget,match", [
    ({"budget": 10, "item": 11}, "no price may be more than the whole budget"),
    ({"budget": 0, "item": 0}, "at least one credit"),
    ({"budget": 10, "max_finds": 2}, "takes only budget"),
    ({"budget": 10, "item": -1}, "non-negative integers"),
    ({"budget": 100, "item": 10}, "not enough credits for a budget of 100"),
])
def test_bad_budgets_are_refused(net, budget, match):
    clock, n, tmp, test = net
    with pytest.raises(HumansOutError, match=match):
        mission(n["alice"], test, budget)


def test_agents_see_what_a_budget_pays_and_take_paid_items_first(net):
    from humansout import watch
    clock, n, tmp, test = net
    mid = mission(n["alice"], test, {"budget": 30, "item": 10, "review": 2, "find": 3})
    paid = n["alice"].add_work(mid, "ASCII letters")["work_id"]
    share(*n.values())
    n["carol"].join(mid)
    n["carol"].add_work(mid, "an item carol found")
    share(*n.values())
    clock.advance(1)
    bob = n["bob"]
    cfg = watch.WatchConfig(roles=("build", "break"), topics=("software",))
    events = watch.find_work(bob.world(), bob.agent_id, clock(), cfg, watch.load_state(bob.home))
    build = next(e for e in events if e.kind == "build" and e.mission == mid)
    assert build.detail["suggested"] == paid and build.detail["bounty"] == 10
    prompt = watch.render_prompt(bob, bob.world(), build, cfg, tmp)
    assert "Budget: 30 of 30 credits left" in prompt and "10 per accepted funded work item" in prompt
    assert "Pays 10 credits." in prompt and "Not funded: points only." in prompt
    assert "bonds up to 3" in prompt
    cheap = watch.WatchConfig(roles=("build", "break"), topics=("software",), min_bounty=11)
    assert not [e for e in watch.find_work(bob.world(), bob.agent_id, clock(), cheap, watch.load_state(bob.home))
                if e.kind == "build" and e.mission == mid]


def test_the_dashboard_shows_budgets_and_the_money_supply(net):
    from humansout import dashboard
    clock, n, tmp, test = net
    n["treasury"].treasury(500)
    mid = mission(n["treasury"], test, {"budget": 40, "item": 10, "review": 1})
    share(*n.values())
    home = dashboard.home(n["bob"].store, now=clock())
    assert [(r["id"], r["bounty_value"]) for r in home["bounties"]] == [(mid, 40)]
    assert home["network"]["on_offer"] == 40 and home["economy"]["treasuries"][n["treasury"].agent_id]["weekly_cap"] == 500
    rows = dashboard.missions(n["bob"].store, q="", domain="", status="open", sort="bounty", offset=0, limit=10)
    assert rows["items"][0]["bounty"]["prices"]["item"] == 10
    assert dashboard.mission_detail(n["bob"].store, mid)["bounty"]["sponsored"] is True


def test_the_whole_loop_from_treasury_to_an_owners_own_mission(net):
    """Credits enter as pay for verified work in a treasury's mission, and leave the earner as the budget of the
    earner's own mission, which pays another agent."""
    clock, n, tmp, test = net
    for node in n.values():
        node.store.set_config("start_credits", 0)  # nobody starts with anything: every credit is earned
    n["treasury"].treasury(1000)
    commons = mission(n["treasury"], test, {"budget": 100, "item": 20, "review": 2}, audit_window=600)
    wid = n["treasury"].add_work(commons, "ASCII letters")["work_id"]
    share(*n.values())
    clock.advance(1)
    claim = build(n, "bob", commons, tmp, "v1", work_id=wid)
    n["dave"].join(commons)
    n["dave"].vouch(claim, "ran the tests; tried empty and long input")
    share(*n.values())
    clock.advance(101)
    assert credits(n, "bob", "bob") == {"agent": n["bob"].agent_id, "credits": 20, "locked": 0, "held": 20,
                                        "available": 0}
    with pytest.raises(HumansOutError, match="held until its audit window ends"):
        mission(n["bob"], test, {"budget": 15, "item": 10})
    clock.advance(600 + 106)  # nobody refuted it in its audit window: the credits are bob's to spend
    own = mission(n["bob"], test, {"budget": 15, "item": 10, "find": 2})
    item = n["bob"].add_work(own, "Polish letters")["work_id"]
    share(*n.values())
    clock.advance(1)
    build(n, "carol", own, tmp, "v1", work_id=item)
    clock.advance(101)
    world = n["alice"].world()  # any node that follows the network agrees
    assert world.credit_view(n["carol"].agent_id)["credits"] == 10
    assert world.credit_view(n["bob"].agent_id) == {"agent": n["bob"].agent_id, "credits": 10, "locked": 5,
                                                    "held": 0, "available": 5}
    assert world.credit_view(n["dave"].agent_id)["credits"] == 2
    economy = world.economy()
    assert economy["issued_total"] == 22 and economy["supply"] == 22  # 20 + 2 minted for the commons work


def test_agents_get_no_tool_that_spends_their_owners_credits(net):
    """Mission content can steer an agent: if it could move credits, a hostile mission could empty the wallet."""
    from humansout.mcp_server import _tools
    clock, n, tmp, test = net
    tools = _tools(n["bob"])
    assert not {"transfer", "treasury", "fund_work", "credit_transfer"} & set(tools)
    assert "bounty" not in tools["create_mission"]["schema"]["properties"]
    assert tools["create_mission"]["schema"]["additionalProperties"] is False
