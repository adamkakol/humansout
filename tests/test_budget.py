"""Mission budgets (protocol 0.5): a fixed budget from the creator's own credits, prices reserved before the work
starts, who pays for being wrong, where credits come from, canaries, and what stops credits being locked for ever."""

import pytest

from humansout import HumansOutError, Node
from humansout.state import CANARY_TTL_MS, CLOSE_GRACE_MS, DORMANT_MS, World


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


def build(n, who, mid, tmp_path, tag, work_id=None, verified=True):
    if n[who].agent_id not in [p["agent"] for p in n[who].mission_state(mid)["participants"]]:
        n[who].join(mid)
    f = tmp_path / f"{who}-{tag}.py"
    f.write_text(f"# {tag}\n")
    cid = n[who].publish_artifact(mid, str(f), "slugify.py")["cid"]
    claim = n[who].claim_done(mid, tag, work_id=work_id, cids=[cid])["claim"]
    share(*n.values())
    for node in n.values() if verified else ():  # every node ran the mission's check on it: it passes
        node.store.save_verification(node.world().items[claim]["check_key"], {"result": "pass"})
    return claim


def deliverable(tmp_path, who, tag):
    """The file build() will publish for this agent and tag: a canary's commitment names it in advance."""
    f = tmp_path / f"{who}-{tag}.py"
    f.write_text(f"# {tag}\n")
    return str(f)


def vouch(n, who, mid, claim):
    if n[who].agent_id not in [p["agent"] for p in n[who].mission_state(mid)["participants"]]:
        n[who].join(mid)
    n[who].vouch(claim, "ran the tests and tried odd input")
    share(*n.values())


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


def budget_of(n, mid, view="treasury"):
    return n[view].world().missions[mid]["bounty"]


def test_a_budget_reserves_each_items_price_and_returns_the_rest(net):
    clock, n, tmp, test = net
    mid = mission(n["alice"], test, {"budget": 36, "item": 10, "review": 3, "find": 3, "expires": 1000})
    assert credits(n, "alice", "alice")["locked"] == 36
    one = n["alice"].add_work(mid, "ASCII letters")
    two = n["alice"].add_work(mid, "Polish letters")
    three = n["alice"].add_work(mid, "German letters")
    assert one["funded"] and two["funded"] and not three["funded"]  # 13 each (price and one reviewer); 10 left
    assert (budget_of(n, mid, "alice")["reserved"], budget_of(n, mid, "alice")["locked"]) == (26, 36)
    share(*n.values())
    clock.advance(1)
    claim = build(n, "bob", mid, tmp, "v1", work_id=one["work_id"])
    vouch(n, "carol", mid, claim)
    clock.advance(101)
    world = n["treasury"].world()
    assert world.items[claim]["status"] == "accepted" and world.items[claim]["credits"] == 10
    assert credits(n, "bob")["credits"] == 60
    assert credits(n, "carol")["credits"] == 51  # a newcomer's review pays a third of the price
    assert credits(n, "alice") == {"agent": n["alice"].agent_id, "credits": 39, "locked": 25, "held": 0,
                                   "available": 14}
    assert budget_of(n, mid)["reserved"] == 13  # only the second item's; the first's unpaid review went back
    clock.advance(1000)  # the budget expires: what it did not pay comes back
    assert credits(n, "alice")["locked"] == 0 and credits(n, "alice")["available"] == 39
    assert [e["kind"] for e in n["treasury"].world().credit_log if e["mission"] == mid] == [
        "item", "review", "refund (expired)"]


def test_review_pay_grows_with_a_clean_record_and_drops_with_a_wrong_vouch():
    world = World({})
    mission = {"bounty": {"prices": {"review": 30}}}

    def pay(vouched, wrong):  # vouched counts the vouch being paid
        st = world._stat("a", "d")
        st["vouched"], st["vouched_wrong"] = vouched, wrong
        return world._review_pay(mission, "a", "d")
    assert pay(1, 0) == 10    # a newcomer: a third
    assert pay(10, 0) == 25   # nine good vouches before
    assert pay(10, 1) == 20   # one of them let wrong work through
    assert pay(1, 2) == 3


def test_items_others_add_are_paid_only_once_the_creator_funds_them(net):
    """Anyone may add work items (scouting), so a budget that paid every item could be emptied by an agent adding
    trivial items and claiming them itself."""
    clock, n, tmp, test = net
    mid = mission(n["alice"], test, {"budget": 30, "item": 10, "scout": 2})
    share(*n.values())
    n["bob"].join(mid)
    own = n["bob"].add_work(mid, "an item bob found")
    with pytest.raises(HumansOutError, match="only the mission creator prices"):
        n["bob"].add_work(mid, "a priced item", price=50)
    n["carol"].join(mid)
    scouted = n["carol"].add_work(mid, "an item carol found")["work_id"]
    share(*n.values())
    clock.advance(1)
    first = build(n, "bob", mid, tmp, "v1", work_id=own["work_id"])
    with pytest.raises(HumansOutError, match="only the mission creator can fund"):
        n["bob"].fund_work(mid, scouted)
    with pytest.raises(HumansOutError, match="unreserved credits: not enough"):
        n["alice"].fund_work(mid, scouted, price=40)
    assert n["alice"].fund_work(mid, scouted, price=8)["reserved"] == 10  # its price and its scout's share
    share(*n.values())
    clock.advance(1)
    second = build(n, "bob", mid, tmp, "v2", work_id=scouted)
    clock.advance(101)
    world = n["treasury"].world()
    assert not own["funded"] and world.items[first]["status"] == "accepted" and world.items[first]["credits"] == 0
    assert world.items[second]["credits"] == 8 and world.items[second]["scout_credits"] == 2
    assert credits(n, "bob")["credits"] == 58 and credits(n, "carol")["credits"] == 52


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
    mid = mission(n["alice"], test, {"budget": 40, "item": 10, "review": 3, "find": 4}, audit_window=3600)
    wid = n["alice"].add_work(mid, "ASCII letters")["work_id"]
    share(*n.values())
    clock.advance(1)
    claim = build(n, "bob", mid, tmp, "v1", work_id=wid)
    vouch(n, "carol", mid, claim)
    clock.advance(101)
    assert credits(n, "bob")["held"] == 10 and credits(n, "carol")["held"] == 1
    assert credits(n, "alice")["locked"] == 29
    refute(n, "dave", claim, tmp)  # inside the audit window
    clock.advance(10)
    world = n["treasury"].world()
    assert world.items[claim]["status"] == "refuted_late"
    assert credits(n, "bob")["credits"] == 50 and credits(n, "carol")["credits"] == 50
    assert credits(n, "dave")["credits"] == 54  # the find price, out of the 11 the wrong work earned
    assert credits(n, "alice")["locked"] == 36  # the other 7 went back into the budget,
    assert world.missions[mid]["work"][wid]["reserve"] == 13  # and the item's price is reserved again
    second = build(n, "carol", mid, tmp, "v2", work_id=wid)
    clock.advance(101 + 3600 + 106)
    assert n["treasury"].world().items[second]["credits"] == 10


def test_the_whole_result_keeps_what_pending_item_claims_are_owed(net):
    """Accepting the whole mission's result closes its budget, but not before the item claims made in time, which
    the budget promised to pay, have settled."""
    clock, n, tmp, test = net
    mid = mission(n["alice"], test, {"budget": 50, "build": 20, "item": 10})
    item = n["alice"].add_work(mid, "ASCII letters")["work_id"]
    n["alice"].add_work(mid, "Polish letters")  # nobody claims it: its reserve comes back with the result
    share(*n.values())
    clock.advance(1)
    whole = build(n, "carol", mid, tmp, "whole")
    clock.advance(50)
    part = build(n, "bob", mid, tmp, "part", work_id=item)
    clock.advance(60)  # the whole result is accepted; bob's item claim is still in its window
    world = n["treasury"].world()
    assert world.items[whole]["status"] == "accepted" and world.items[whole]["credits"] == 20
    assert budget_of(n, mid)["locked"] == 10 and not budget_of(n, mid)["closed"]
    assert credits(n, "alice")["credits"] == 30 and credits(n, "alice")["locked"] == 10
    clock.advance(50)
    assert n["treasury"].world().items[part]["credits"] == 10
    assert budget_of(n, mid)["closed"] == "done" and credits(n, "alice") == {
        "agent": n["alice"].agent_id, "credits": 20, "locked": 0, "held": 0, "available": 20}


def test_a_budget_closes_even_if_a_claim_never_settles(net):
    """A claim made in time is paid when it is accepted after expiry, but one that never settles (a check nobody
    runs) cannot keep the budget locked for ever."""
    clock, n, tmp, test = net
    mid = mission(n["alice"], test, {"budget": 20, "item": 10, "expires": 500})
    wid = n["alice"].add_work(mid, "ASCII letters")["work_id"]
    share(*n.values())
    clock.advance(1)
    stuck = build(n, "bob", mid, tmp, "v1", work_id=wid, verified=False)
    clock.advance(600)
    assert n["treasury"].world().items[stuck]["status"] == "pending"
    assert not budget_of(n, mid)["closed"]  # it waits for the claim made in time
    clock.advance(CLOSE_GRACE_MS // 1000)
    assert budget_of(n, mid)["closed"] == "expired" and credits(n, "alice")["locked"] == 0


def test_a_treasury_creates_credits_only_by_public_issues_and_spends_them_like_anyone(net):
    """Where credits come from: a trusted creator's treasury issues them into its own balance in public, signed
    events (the genesis allocation), and funds budgets from that balance like any owner."""
    clock, n, tmp, test = net
    with pytest.raises(HumansOutError, match="only a trusted creator can run a treasury"):
        n["bob"].treasury(issue=1000)
    n["treasury"].treasury(issue=1000, weekly_cap=100)
    share(*n.values())
    assert credits(n, "treasury", "carol")["credits"] == 1050
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
    assert economy["issued_total"] == 1000 and credits(n, "bob")["credits"] == 70
    assert economy["treasuries"][n["treasury"].agent_id]["balance"] == 1030  # the unpaid 40 came back
    clock.advance(7 * 86400)
    mission(n["treasury"], test, {"budget": 50, "item": 10}, max_builders=3)  # a new week


def test_after_its_treasury_a_creator_mints_credits_only_through_budgets(net):
    clock, n, tmp, test = net
    before = mission(n["treasury"], test)  # before 0.5: trusted missions minted a credit per accepted build
    share(*n.values())
    first = build(n, "bob", before, tmp, "v1")
    clock.advance(101)
    assert n["treasury"].world().items[first]["credits"] == 1
    n["treasury"].treasury(issue=100)
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


def test_a_canary_nobody_catches_costs_its_reviewers_what_they_staked_and_earned(net):
    """Reviewers cannot tell a planted faulty claim from real work: vouching without checking stops paying."""
    clock, n, tmp, test = net
    n["treasury"].treasury(issue=1000)
    mid = mission(n["treasury"], test, {"budget": 100, "item": 20, "review": 6}, audit_window=3600)
    wid = n["treasury"].add_work(mid, "ASCII letters")["work_id"]
    commit = n["treasury"].canary_commit(n["dave"].agent_id, deliverable(tmp, "dave", "faulty"), bounty=30)["commit"]
    share(*n.values())
    assert n["carol"].world().economy()["canaries_open"] == 1  # public: a canary is out, nobody knows where
    clock.advance(1)
    canary = build(n, "dave", mid, tmp, "faulty", work_id=wid)
    vouch(n, "carol", mid, canary)
    clock.advance(101)
    points_before = n["treasury"].world().balance(n["carol"].agent_id, "software/python")
    assert credits(n, "dave")["credits"] == 70 and credits(n, "carol")["credits"] == 52
    with pytest.raises(HumansOutError, match="no canary"):
        n["carol"].canary_reveal(commit, canary)  # the salt is on the treasury's node only
    n["treasury"].canary_reveal(commit, canary, defect="drops the last character")
    share(*n.values())
    world = n["alice"].world()
    assert world.items[canary]["status"] == "canary"
    assert credits(n, "dave", "alice")["credits"] == 50 and credits(n, "carol", "alice")["credits"] == 50
    assert world.balance(n["carol"].agent_id, "software/python") < points_before
    assert world.stats[(n["carol"].agent_id, "software/python")]["vouched_wrong"] == 1
    assert world.missions[mid]["work"][wid]["done_claim"] is None and world.missions[mid]["work"][wid]["reserve"] == 26
    assert credits(n, "treasury", "alice")["locked"] == 100  # the bounty came back; the budget is whole again


def test_a_caught_canary_pays_its_finder_the_bounty(net):
    clock, n, tmp, test = net
    n["treasury"].treasury(issue=1000)
    mid = mission(n["treasury"], test, {"budget": 100, "item": 20, "find": 5})
    wid = n["treasury"].add_work(mid, "ASCII letters")["work_id"]
    commit = n["treasury"].canary_commit(n["dave"].agent_id, deliverable(tmp, "dave", "faulty"), bounty=30)["commit"]
    share(*n.values())
    clock.advance(1)
    canary = build(n, "dave", mid, tmp, "faulty", work_id=wid)
    with pytest.raises(HumansOutError, match="has not settled yet"):
        n["treasury"].canary_reveal(commit, canary)
    clock.advance(1)
    refute(n, "bob", canary, tmp)
    clock.advance(106)
    n["treasury"].canary_reveal(commit, canary, defect="drops the last character")
    share(*n.values())
    assert credits(n, "bob", "carol")["credits"] == 85  # the canary's bond (5) and the bounty (30)
    assert n["carol"].world().canaries[commit]["caught_by"] == n["bob"].agent_id


def test_a_canary_reveal_must_name_its_own_agents_claim_in_time(net):
    clock, n, tmp, test = net
    n["treasury"].treasury(issue=1000)
    mid = mission(n["treasury"], test, {"budget": 100, "item": 20}, audit_window=600)
    wid = n["treasury"].add_work(mid, "ASCII letters")["work_id"]
    commit = n["treasury"].canary_commit(n["dave"].agent_id, deliverable(tmp, "dave", "faulty"), bounty=30)["commit"]
    # committing to an honest agent in advance frames nothing: its real deliverable is not the one committed to
    framing = n["treasury"].canary_commit(n["bob"].agent_id, deliverable(tmp, "x", "guess"), bounty=10)["commit"]
    share(*n.values())
    with pytest.raises(HumansOutError, match="only a treasury plants canaries"):
        n["bob"].canary_commit(n["dave"].agent_id, deliverable(tmp, "dave", "faulty"))
    clock.advance(1)
    honest = build(n, "bob", mid, tmp, "v1", work_id=wid)
    clock.advance(101)
    with pytest.raises(HumansOutError, match="a claim by that agent"):
        n["treasury"].canary_reveal(commit, honest)
    with pytest.raises(HumansOutError, match="with that deliverable"):
        n["treasury"].canary_reveal(framing, honest)
    later = n["treasury"].canary_commit(n["bob"].agent_id, deliverable(tmp, "bob", "v1"), bounty=10)["commit"]
    with pytest.raises(HumansOutError, match="posted after the commitment"):
        n["treasury"].canary_reveal(later, honest)
    n["treasury"].canary_reveal(commit)  # never planted: the bounty comes back
    assert n["treasury"].world().canaries[commit]["status"] == "revealed"
    clock.advance(CANARY_TTL_MS // 1000)
    assert n["treasury"].world().canaries[later]["status"] == "expired"
    assert credits(n, "treasury")["locked"] == 0  # the budget expired and every bounty is free again


def test_dormant_accounts_give_their_credits_back_to_the_treasury(net):
    """A fixed supply must not leak into abandoned accounts: after DORMANT_MS without a signed event, what an
    account can spend goes back to the treasury, which funds new work with it."""
    clock, n, tmp, test = net
    n["treasury"].treasury(issue=1000)
    mid = mission(n["treasury"], test, {"budget": 100, "item": 20, "expires": 3600})
    wid = n["treasury"].add_work(mid, "ASCII letters")["work_id"]
    share(*n.values())
    clock.advance(1)
    build(n, "bob", mid, tmp, "v1", work_id=wid)
    clock.advance(DORMANT_MS // 1000 - 2 * 86400)
    other = mission(n["alice"], test, {"budget": 10, "item": 10})  # alice and carol act; bob does not
    share(*n.values())
    n["carol"].join(other)
    share(*n.values())
    clock.advance(3 * 86400)
    world = n["treasury"].world()
    assert world.credit_view(n["bob"].agent_id)["credits"] == 0  # its 70 went back
    assert world.credit_view(n["carol"].agent_id)["credits"] == 50
    economy = world.economy()
    assert economy["reclaimed_total"] == 70
    assert economy["treasuries"][n["treasury"].agent_id]["balance"] == 1050 - 20 + 70
    n["bob"].join(other)  # back again: the clock restarts, and what it earns from now on stays
    share(*n.values())
    clock.advance(30 * 86400)
    assert n["treasury"].world().economy()["reclaimed_total"] == 70


def test_the_whole_loop_from_genesis_to_an_owners_own_mission(net):
    """Credits enter once, as the treasury's public genesis issue; it funds a commons mission; the agent that
    did the work spends what it earned as the budget of its own mission, which pays another agent. The supply
    never changes."""
    clock, n, tmp, test = net
    for node in n.values():
        node.store.set_config("start_credits", 0)  # nobody starts with anything: every credit is earned
    n["treasury"].treasury(issue=1000)
    commons = mission(n["treasury"], test, {"budget": 100, "item": 20, "review": 3}, audit_window=600)
    wid = n["treasury"].add_work(commons, "ASCII letters")["work_id"]
    share(*n.values())
    clock.advance(1)
    claim = build(n, "bob", commons, tmp, "v1", work_id=wid)
    vouch(n, "dave", commons, claim)
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
    assert world.credit_view(n["dave"].agent_id)["credits"] == 1
    assert world.economy()["supply"] == 1000  # issued once; it only moves


@pytest.mark.parametrize("budget,match", [
    ({"budget": 10, "item": 11}, "no price may be more than the whole budget"),
    ({"budget": 0, "item": 0}, "at least one credit"),
    ({"budget": 10, "review": 2}, "give it an item or a build price"),
    ({"budget": 10, "build": 9, "review": 2}, "does not cover the build price and its reviewers"),
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
    build_ev = next(e for e in events if e.kind == "build" and e.mission == mid)
    assert build_ev.detail["suggested"] == paid and build_ev.detail["bounty"] == 10
    prompt = watch.render_prompt(bob, bob.world(), build_ev, cfg, tmp)
    assert "Budget: 30 credits, 12 reserved" in prompt
    assert "Pays 10 credits (reserved)." in prompt and "Not funded: points only." in prompt
    assert "bonds up to 3" in prompt and "Expected for you" in prompt
    cheap = watch.WatchConfig(roles=("build", "break"), topics=("software",), min_bounty=11)
    assert not [e for e in watch.find_work(bob.world(), bob.agent_id, clock(), cheap, watch.load_state(bob.home))
                if e.kind == "build" and e.mission == mid]


def test_the_dashboard_shows_budgets_and_the_money_supply(net):
    from humansout import dashboard
    clock, n, tmp, test = net
    n["treasury"].treasury(issue=500)
    mid = mission(n["treasury"], test, {"budget": 40, "item": 10, "review": 1})
    share(*n.values())
    home = dashboard.home(n["bob"].store, now=clock())
    assert [(r["id"], r["bounty_value"]) for r in home["bounties"]] == [(mid, 40)]
    assert home["network"]["on_offer"] == 40 and home["economy"]["issued_total"] == 500
    rows = dashboard.missions(n["bob"].store, q="", domain="", status="open", sort="bounty", offset=0, limit=10)
    assert rows["items"][0]["bounty"]["prices"]["item"] == 10
    assert dashboard.mission_detail(n["bob"].store, mid)["bounty"]["treasury"] is True


def test_agents_get_no_tool_that_spends_their_owners_credits(net):
    """Mission content can steer an agent: if it could move credits, a hostile mission could empty the wallet."""
    from humansout.mcp_server import _tools
    clock, n, tmp, test = net
    tools = _tools(n["bob"])
    assert not {"transfer", "treasury", "fund_work", "credit_transfer", "canary_commit", "canary_reveal"} & set(tools)
    assert "bounty" not in tools["create_mission"]["schema"]["properties"]
    assert tools["create_mission"]["schema"]["additionalProperties"] is False


def test_a_scouts_share_is_held_and_given_back_when_the_fix_is_refuted(net):
    """Found by the security-review swarm (round 4): the scout's share was neither held nor taken back, so a scout
    and a second identity could spend at once what a wrong fix earned."""
    clock, n, tmp, test = net
    mid = mission(n["alice"], test, {"budget": 40, "item": 10, "scout": 3, "find": 2}, audit_window=600)
    share(*n.values())
    n["carol"].join(mid)
    wid = n["carol"].add_work(mid, "an item carol found")["work_id"]
    share(*n.values())
    n["alice"].fund_work(mid, wid)
    share(*n.values())
    clock.advance(1)
    claim = build(n, "bob", mid, tmp, "v1", work_id=wid)
    clock.advance(101)
    assert credits(n, "carol")["held"] == 3
    refute(n, "dave", claim, tmp)  # inside the audit window
    clock.advance(10)
    world = n["treasury"].world()
    assert world.items[claim]["status"] == "refuted_late"
    assert credits(n, "carol")["credits"] == 50  # the share came back
    assert world.missions[mid]["work"][wid]["reserve"] == 13  # reserved again with the scout's share
    redo = build(n, "dave", mid, tmp, "v2", work_id=wid)
    clock.advance(101 + 600 + 106)
    assert n["treasury"].world().items[redo]["scout_credits"] == 3 and credits(n, "carol")["credits"] == 53


def test_canary_salts_are_never_readable_by_others(net, monkeypatch):
    """Found by the security-review swarm (round 4): canaries.json was written first and chmod-ed after, so for a
    moment other local users could read the salts and tell which claim is a canary."""
    import os
    clock, n, tmp, test = net
    n["treasury"].treasury(issue=100)
    monkeypatch.setattr(os, "chmod", lambda *a, **k: None)  # the old code relied on it; the file must be private anyway
    old = os.umask(0)
    try:
        n["treasury"].canary_commit(n["dave"].agent_id, deliverable(tmp, "dave", "faulty"), bounty=1)
    finally:
        os.umask(old)
    assert (n["treasury"].home / "canaries.json").stat().st_mode & 0o777 == 0o600


def test_the_dashboard_shows_what_a_budget_paid_to_whom(net):
    """Adam could not find where to see a mission's budget: the mission page has a budget tab."""
    from humansout import dashboard
    clock, n, tmp, test = net
    mid = mission(n["alice"], test, {"budget": 40, "item": 10, "review": 3, "find": 3, "expires": 1000})
    wid = n["alice"].add_work(mid, "ASCII letters")["work_id"]
    n["alice"].add_work(mid, "Polish letters")
    share(*n.values())
    clock.advance(1)
    claim = build(n, "bob", mid, tmp, "v1", work_id=wid)
    vouch(n, "carol", mid, claim)
    clock.advance(101)
    b = dashboard.mission_detail(n["dave"].store, mid, now=clock())["budget"]
    assert b["paid"] == {"item": 10, "review": 1} and b["paid_total"] == 11
    assert b["paid_by_agent"] == [{"agent": n["bob"].agent_id, "amount": 10}, {"agent": n["carol"].agent_id, "amount": 1}]
    assert (b["total"], b["locked"], b["reserved"], b["unreserved"]) == (40, 29, 13, 16)
    clock.advance(1000)
    b = dashboard.mission_detail(n["dave"].store, mid, now=clock())["budget"]
    assert b["closed"] == "expired" and b["refunds"] == 29 and b["locked"] == 0
    other = mission(n["alice"], test, max_builders=3)  # no budget: no budget tab
    share(*n.values())
    assert dashboard.budget_view(n["dave"].world(), other) is None
