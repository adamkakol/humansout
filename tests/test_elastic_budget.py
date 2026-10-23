"""Elastic budgets (docs/ELASTIC_BUDGETS.md): the swarm funds the work it discovers from a budget's free part,
anyone can add to a budget, and the whole result pays the work it builds on. Adam, 23 October 2026: the research
mission's fixed list of prices left the hypotheses its agents found unpaid."""

import pytest

from humansout import HumansOutError, dashboard
from test_budget import assert_the_log_gives_every_balance, build, budget_of, credits, make_net, mission, refute, share, vouch


@pytest.fixture
def net(tmp_path):
    return make_net(tmp_path)


@pytest.fixture(autouse=True)
def every_credit_movement_is_logged(net):
    yield
    assert_the_log_gives_every_balance(net[1]["treasury"].world())

DOMAIN = "software/python"


def with_records(n, tmp, test, budget, **rules):
    """A mission whose first item carol built and dave reviewed: both now have a record in the domain."""
    clock = n["alice"].clock
    mid = mission(n["alice"], test, budget, **rules)
    first = n["alice"].add_work(mid, "ASCII letters")["work_id"]
    share(*n.values())
    n["bob"].join(mid)
    share(*n.values())
    clock.advance(1)
    claim = build(n, "carol", mid, tmp, "ascii", work_id=first)
    vouch(n, "dave", mid, claim)
    clock.advance(101)
    share(*n.values())
    assert n["treasury"].world().items[claim]["status"] == "accepted"
    return mid


def endorse(n, who, mid, work_id, why="it would show whether the swarm finds edge cases"):
    out = n[who].endorse_work(mid, work_id, why)
    share(*n.values())
    return out


def item(n, mid, wid, view="treasury"):
    return n[view].world().missions[mid]["work"][wid]


def test_the_swarm_funds_what_it_endorses_from_the_free_part(net):
    clock, n, tmp, test = net
    mid = with_records(n, tmp, test, {"budget": 50, "item": 20, "review": 3, "scout": 2, "find": 5, "open": 50})
    # the first item paid 20 and a newcomer's review 1 of its 3: 29 left, none of it reserved
    assert (budget_of(n, mid)["locked"], budget_of(n, mid)["reserved"]) == (29, 0)
    found = n["bob"].add_work(mid, "Polish letters", "found while reviewing: ł and ó are dropped")["work_id"]
    share(*n.values())
    assert not item(n, mid, found)["reserve"]  # someone else's item: not funded by being added
    assert endorse(n, "carol", mid, found)["funded"] is False  # one endorsement of the two needed
    view = dashboard.budget_view(n["treasury"].world(), mid)
    assert view["waiting"] == [{"work": found, "title": "Polish letters", "endorsements": 1}]
    assert view["next_item"] == {"price": 11, "review": 1, "scout": 1} and view["open"] == 50
    out = endorse(n, "dave", mid, found)
    work = item(n, mid, found)
    # half of the free 29 is 14, of a normal item's 25 (20 + a reviewer's 3 + the scout's 2): all prices scaled alike
    assert out["funded"] and (work["price"], work["review_price"], work["scout_price"], work["reserve"]) == (11, 1, 1, 13)
    assert work["pool"] and budget_of(n, mid)["reserved"] == 13
    another = n["bob"].add_work(mid, "German letters")["work_id"]
    share(*n.values())
    endorse(n, "carol", mid, another)
    endorse(n, "dave", mid, another)
    assert item(n, mid, another)["price"] == 6  # a share of what is left: later discoveries are paid less
    world = n["treasury"].world()
    stake = item(n, mid, found)["endorsements"][0]["stake"]
    assert world.locked[(n["carol"].agent_id, DOMAIN)] >= stake  # endorsing stakes points
    clock.advance(1)
    claim = build(n, "carol", mid, tmp, "polish", work_id=found)
    clock.advance(101)
    world = n["treasury"].world()
    assert world.items[claim]["credits"] == 11 and world.items[claim]["scout_credits"] == 1  # bob found it
    assert {x["status"] for x in world.missions[mid]["work"][found]["endorsements"]} == {"returned"}


def test_endorsing_needs_an_open_budget_a_record_and_other_owners(net):
    clock, n, tmp, test = net
    closed = mission(n["alice"], test, {"budget": 10, "item": 10})
    share(*n.values())
    n["bob"].join(closed)
    share(*n.values())
    theirs = n["bob"].add_work(closed, "Polish letters")["work_id"]
    share(*n.values())
    with pytest.raises(HumansOutError, match="does not let the swarm fund"):
        n["carol"].endorse_work(closed, theirs, "worth it")
    clock.advance(1)
    mid = mission(n["alice"], test, {"budget": 40, "item": 10, "open": 50})
    share(*n.values())
    for who in ("carol", "dave"):  # one owner's two agents
        n[who].join(mid, "one-owner")
    share(*n.values())
    clock.advance(1)
    first, second = (n["alice"].add_work(mid, t)["work_id"] for t in ("ASCII letters", "Polish letters"))
    share(*n.values())
    clock.advance(1)
    build(n, "carol", mid, tmp, "ascii", work_id=first)  # one owner's agents cannot review each other: both build
    build(n, "dave", mid, tmp, "polish", work_id=second)
    clock.advance(101)
    n["bob"].join(mid)
    share(*n.values())
    found = n["bob"].add_work(mid, "German letters")["work_id"]
    share(*n.values())
    with pytest.raises(HumansOutError, match="needs a record"):
        n["treasury"].endorse_work(mid, found, "worth it")  # no accepted work, review or find here
    with pytest.raises(HumansOutError, match="author"):
        n["bob"].endorse_work(mid, found, "mine")
    endorse(n, "carol", mid, found)
    with pytest.raises(HumansOutError, match="same owner"):
        n["dave"].endorse_work(mid, found, "seconded")
    assert not item(n, mid, found)["reserve"]


def test_the_creator_can_veto_what_the_swarm_funded_and_its_endorsers_pay(net):
    clock, n, tmp, test = net
    mid = with_records(n, tmp, test, {"budget": 50, "item": 20, "review": 3, "scout": 2, "open": 50})
    vetoed, withdrawn = (n["bob"].add_work(mid, title)["work_id"] for title in ("Polish letters", "Emoji"))
    share(*n.values())
    for wid in (vetoed, withdrawn):
        endorse(n, "carol", mid, wid)
        endorse(n, "dave", mid, wid)
    world = n["treasury"].world()
    before = world.balance(n["carol"].agent_id, DOMAIN)
    stake = item(n, mid, vetoed)["endorsements"][0]["stake"]
    clock.advance(1)
    n["alice"].cancel_work(mid, vetoed, "not what this mission is for")
    n["bob"].cancel_work(mid, withdrawn, "a duplicate")
    share(*n.values())
    world = n["treasury"].world()
    assert [x["status"] for x in item(n, mid, vetoed)["endorsements"]] == ["burned", "burned"]
    assert [x["status"] for x in item(n, mid, withdrawn)["endorsements"]] == ["returned", "returned"]
    assert world.balance(n["carol"].agent_id, DOMAIN) == before - stake
    assert budget_of(n, mid)["reserved"] == 0  # both reserves are free again


def test_anyone_can_add_to_a_budget_and_gets_back_their_share(net):
    clock, n, tmp, test = net
    mid = mission(n["alice"], test, {"budget": 40, "item": 10, "expires": 1000})
    share(*n.values())
    clock.advance(1)
    out = n["dave"].add_budget(mid, 20)
    share(*n.values())
    assert out["budget"] == 60 and credits(n, "dave")["credits"] == 30
    with pytest.raises(HumansOutError, match="only the mission's creator"):
        n["dave"].add_budget(mid, 5, open_percent=20)
    n["alice"].add_budget(mid, 0, open_percent=20, extend_days=1)
    share(*n.values())
    assert budget_of(n, mid)["open"] == 20 and budget_of(n, mid)["expires_ts"] > clock.t + 86_000_000
    clock.advance(1000 + 86400)
    world = n["treasury"].world()
    assert budget_of(n, mid)["closed"] == "expired"
    assert world.credit(n["alice"].agent_id) == 50 and world.credit(n["dave"].agent_id) == 50  # in proportion
    view = dashboard.budget_view(world, mid)
    assert view["funders"] == [{"agent": n["alice"].agent_id, "amount": 40}, {"agent": n["dave"].agent_id, "amount": 20}]
    assert view["refunds"] == 60 and view["paid_total"] == 0  # co-funding is neither pay nor a refund


def test_a_treasury_adds_within_its_weekly_cap(net):
    clock, n, tmp, test = net
    n["treasury"].treasury(issue=1000, weekly_cap=100)
    mid = mission(n["treasury"], test, {"budget": 80, "item": 10})
    with pytest.raises(HumansOutError, match="weekly cap"):
        n["treasury"].add_budget(mid, 30)
    assert n["treasury"].add_budget(mid, 20)["budget"] == 100


def test_the_whole_result_pays_the_work_it_cites(net):
    clock, n, tmp, test = net
    mid = mission(n["alice"], test, {"budget": 50, "build": 30, "item": 10, "cite": 40})
    first, second = (n["alice"].add_work(mid, t)["work_id"] for t in ("ASCII letters", "Polish letters"))
    share(*n.values())
    clock.advance(1)
    one = build(n, "bob", mid, tmp, "ascii", work_id=first)
    two = build(n, "carol", mid, tmp, "polish", work_id=second)
    clock.advance(101)
    whole = build(n, "dave", mid, tmp, "whole", cites=[one, two])
    clock.advance(101)
    world = n["treasury"].world()
    assert world.items[whole]["status"] == "accepted" and world.items[whole]["credits"] == 18  # 30 less the cited 40%
    assert [(c["claim"], c["amount"]) for c in world.items[whole]["cite_paid"]] == [(one, 6), (two, 6)]
    assert world.credit(n["bob"].agent_id) == 66 and world.credit(n["carol"].agent_id) == 66  # 50 + item 10 + cited 6


def test_citing_your_own_work_gains_you_nothing(net):
    clock, n, tmp, test = net
    mid = mission(n["alice"], test, {"budget": 50, "build": 30, "item": 10, "cite": 40})
    first, second = (n["alice"].add_work(mid, t)["work_id"] for t in ("ASCII letters", "Polish letters"))
    share(*n.values())
    clock.advance(1)
    mine = build(n, "bob", mid, tmp, "ascii", work_id=first)
    theirs = build(n, "carol", mid, tmp, "polish", work_id=second)
    clock.advance(101)
    build(n, "bob", mid, tmp, "whole", cites=[mine, theirs])
    clock.advance(101)
    world = n["treasury"].world()
    assert world.credit(n["bob"].agent_id) == 50 + 10 + 18  # its item, and the build price less the cited 40%
    assert world.credit(n["carol"].agent_id) == 50 + 10 + 12  # all of the cited 40%: the only other work cited


def test_a_cited_share_is_taken_back_with_the_claim_it_paid(net):
    clock, n, tmp, test = net
    mid = mission(n["alice"], test, {"budget": 50, "build": 30, "item": 10, "cite": 40}, audit_window=3600)
    first = n["alice"].add_work(mid, "ASCII letters")["work_id"]
    share(*n.values())
    clock.advance(1)
    cited = build(n, "carol", mid, tmp, "ascii", work_id=first)
    clock.advance(101)
    build(n, "dave", mid, tmp, "whole", cites=[cited])
    clock.advance(101)
    world = n["treasury"].world()
    assert world.items[cited]["cite_credits"] == 12 and credits(n, "carol")["held"] == 22  # held until the audit ends
    refute(n, "bob", cited, tmp)  # inside its audit window
    clock.advance(10)
    world = n["treasury"].world()
    assert world.items[cited]["status"] == "refuted_late" and world.credit(n["carol"].agent_id) == 50


def test_cites_name_claims_on_this_missions_items(net):
    clock, n, tmp, test = net
    mid = mission(n["alice"], test, {"budget": 50, "build": 30, "item": 10, "cite": 40})
    first = n["alice"].add_work(mid, "ASCII letters")["work_id"]
    share(*n.values())
    clock.advance(1)
    part = build(n, "bob", mid, tmp, "ascii", work_id=first)
    with pytest.raises(HumansOutError, match="only a claim on the whole mission"):
        build(n, "carol", mid, tmp, "x", work_id=first, cites=[part])
    whole = build(n, "carol", mid, tmp, "whole", cites=[part])
    with pytest.raises(HumansOutError, match="at most 50 claims on this mission's work items"):
        build(n, "dave", mid, tmp, "whole2", cites=[whole])


def test_agents_are_told_how_the_swarm_funds_work_and_what_citing_pays(net):
    from humansout.watch import budget_line
    clock, n, tmp, test = net
    mid = mission(n["alice"], test, {"budget": 50, "build": 20, "item": 10, "open": 20, "cite": 30})
    line = budget_line(n["alice"].world().missions[mid])
    assert "endorse_work" in line and "up to 20% of what is free" in line and "30% of its price" in line
    plain = mission(n["bob"], test, {"budget": 20, "item": 10})
    assert "endorse" not in budget_line(n["bob"].world().missions[plain])


def test_watch_asks_agents_with_a_record_to_judge_what_others_found(net, tmp_path):
    """Adam, 23 October: in the research mission nobody endorsed anything for hours. The prompts mentioned it, but
    every turn had one task and "do only this": endorsing needs a turn of its own."""
    from humansout.watch import WatchConfig, find_work, load_state, render_prompt
    clock, n, tmp, test = net
    mid = with_records(n, tmp, test, {"budget": 50, "item": 20, "review": 3, "scout": 2, "open": 50})
    found = n["bob"].add_work(mid, "Polish letters", "ł and ó are dropped")["work_id"]
    share(*n.values())
    cfg = WatchConfig(roles=("build", "break"))

    def offered(who, state):
        world = n[who].world()
        return [e for e in find_work(world, n[who].agent_id, clock.t, cfg, state) if e.kind == "endorse"]
    state = load_state(tmp_path / "nowhere")
    [ev] = offered("carol", state)  # carol built accepted work here
    assert ev.mission == mid and ev.detail["items"] == [found] and ev.detail["price"] == 11
    prompt = render_prompt(n["carol"], n["carol"].world(), ev, cfg, tmp_path)
    assert found in prompt and "Polish letters" in prompt and "endorse_work" in prompt and "0/2 endorsements" in prompt
    assert not offered("treasury", load_state(tmp_path / "nowhere"))  # no record in the domain
    assert not offered("bob", load_state(tmp_path / "nowhere"))       # its own item
    state["endorse_seen"].append(found)
    assert not offered("carol", state)  # judged once
    endorse(n, "dave", mid, found)
    assert not offered("dave", load_state(tmp_path / "nowhere"))  # already endorsed it
