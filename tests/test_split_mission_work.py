"""`watch` offers every open work item of a split mission, not just one per agent.

The first version stopped offering a mission once the agent had any claim on it, which wasted almost all
of an agent's capacity on a mission split into 42 districts. The fix was built by the swarm itself on the
public hub (mission "HumansOut builds HumansOut", claim ab921107); two of the tests below come from the
same run: a counterexample that refuted another agent's patch, and the attempt cap a reviewer found.
"""

import sys
import textwrap
import time

from humansout import Node, watch


class RealClock:
    """For tests whose harness runs in another process (on the real clock): advancing only keeps ticks apart."""
    def __call__(self):
        return int(time.time() * 1000)

    def advance(self, seconds):
        time.sleep(0.005)


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


def setup(tmp_path, items=3, clock=None):
    clock = clock or Clock()
    creator, agent = Node(tmp_path / "creator", clock=clock), Node(tmp_path / "agent", clock=clock)
    test = tmp_path / "test_basic.py"
    test.write_text("import unittest\n")
    mid = creator.create_mission("Split work", "software/python", "c", challenge_window=100, dispute_window=5,
                                 check={"command": ["python", "-m", "unittest"], "deliverable": "out.py",
                                        "files": [str(test)]})["mission_id"]
    clock.advance(1)
    ids = [creator.add_work(mid, f"Part {i + 1}")["work_id"] for i in range(items)]
    share(creator, agent)
    agent.join(mid)
    return clock, creator, agent, mid, ids


def deliver(agent, mid, work_id, tmp_path, clock, result="pass"):
    clock.advance(1)
    agent.claim_work(mid, work_id)
    f = tmp_path / f"out-{work_id}.py"
    f.write_text(f"x = {work_id!r}\n")
    cid = agent.publish_artifact(mid, str(f), "out.py")["cid"]
    claim = agent.claim_done(mid, f"part {work_id}", work_id=work_id, cids=[cid])["claim"]
    agent.store.save_verification(agent.world().items[claim]["check_key"], {"result": result})
    return claim


def offered(agent, clock):
    events = watch.find_work(agent.world(), agent.agent_id, clock(), watch.WatchConfig(roles=("build",)),
                             watch.load_state(agent.home))
    return [e.mission for e in events if e.kind == "build"]


def test_open_items_are_still_offered_while_another_claim_is_pending(tmp_path):
    clock, creator, agent, mid, ids = setup(tmp_path)
    deliver(agent, mid, ids[0], tmp_path, clock)
    assert offered(agent, clock) == [mid]


def test_an_accepted_item_does_not_end_the_offer(tmp_path):
    clock, creator, agent, mid, ids = setup(tmp_path)
    deliver(agent, mid, ids[0], tmp_path, clock)
    clock.advance(200)  # the first claim is accepted
    assert offered(agent, clock) == [mid]


def test_no_offer_once_no_open_item_is_left_for_the_agent(tmp_path):
    clock, creator, agent, mid, ids = setup(tmp_path, items=2)
    for work_id in ids:
        deliver(agent, mid, work_id, tmp_path, clock)
    assert offered(agent, clock) == []


def test_missions_without_work_items_keep_one_claim_per_agent(tmp_path):
    clock, creator, agent, mid, ids = setup(tmp_path, items=0)
    clock.advance(1)
    f = tmp_path / "out.py"
    f.write_text("x = 1\n")
    agent.claim_done(mid, "whole mission", cids=[agent.publish_artifact(mid, str(f))["cid"]])
    assert offered(agent, clock) == []


def test_a_failed_mission_level_claim_does_not_hide_the_open_items(tmp_path):
    """From the swarm: a settled mission-level claim that failed its check must not block the items."""
    clock, creator, agent, mid, ids = setup(tmp_path)
    clock.advance(1)
    f = tmp_path / "whole.py"
    f.write_text("x = 'whole'\n")
    claim = agent.claim_done(mid, "whole mission", cids=[agent.publish_artifact(mid, str(f), "out.py")["cid"]])["claim"]
    agent.store.save_verification(agent.world().items[claim]["check_key"], {"result": "fail"})
    clock.advance(200)
    assert agent.world().items[claim]["status"] == "failed_check"
    deliver(agent, mid, ids[0], tmp_path, clock)
    assert offered(agent, clock) == [mid]


def test_a_pending_mission_level_claim_holds_the_agent_back(tmp_path):
    clock, creator, agent, mid, ids = setup(tmp_path)
    clock.advance(1)
    f = tmp_path / "whole.py"
    f.write_text("x = 'whole'\n")
    agent.claim_done(mid, "whole mission", cids=[agent.publish_artifact(mid, str(f), "out.py")["cid"]])
    assert offered(agent, clock) == []


CLAIM_NEXT_ITEM = textwrap.dedent("""
    import sys
    from pathlib import Path
    from humansout import Node
    node, mid = Node(Path(sys.argv[1])), sys.argv[2]
    world = node.world()
    taken = set(world.items[c]["target"] for c in world.missions[mid]["claims"])
    item = sorted(w for w in world.missions[mid]["work"] if w not in taken)[0]
    node.claim_work(mid, item)
    out = Path(sys.argv[3]) / ("out-" + item + ".py")
    out.write_text("x = 1")
    node.claim_done(mid, "part", work_id=item, cids=[node.publish_artifact(mid, str(out), "out.py")["cid"]])
""")


def test_builds_that_claim_an_item_do_not_use_up_the_attempt_cap(tmp_path):
    """From the swarm: three items built in a row used to exhaust the cap of 3 attempts (and the hour
    between retries), although every build delivered. Only builds that claim nothing count."""
    clock, creator, agent, mid, ids = setup(tmp_path, items=5, clock=RealClock())
    harness = [sys.executable, "-c", CLAIM_NEXT_ITEM, "{home}", "{mission}", "{workdir}", "{prompt_file}"]
    cfg = watch.WatchConfig(harness=harness, roles=("build",), verify=False, max_build_attempts=3,
                            cli=[sys.executable, "-m", "humansout"])
    for _ in range(4):
        watch.cycle(agent, cfg)
    mine = [i for i in agent.world().missions[mid]["claims"] if agent.world().items[i]["author"] == agent.agent_id]
    assert len(mine) == 4


def test_two_agents_never_hold_the_same_work_item_where_items_are_exclusive(tmp_path):
    """Found in the security-review swarm: three agents took one item at once, each unaware of the others.
    claim_work now asks the network first, and the agent tools publish a lease as soon as it is written.
    (Only in missions that make items exclusive; by default a lease informs and never refuses.)"""
    from humansout.mcp_server import _tools, handle
    from humansout.net import serve
    hub = Node(tmp_path / "hub")
    hub.store.set_config("accept_push", True)
    srv = serve(hub.store, "127.0.0.1", 0, background=True, agent_id=hub.agent_id)
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        mid = hub.create_mission("Review", "security/x", "c", rules={"exclusive_items": 1})["mission_id"]
        wid = hub.add_work(mid, "area one")["work_id"]
        a, b = Node(tmp_path / "a"), Node(tmp_path / "b")
        for n in (a, b):
            n.store.add_peer(url)
            n.sync()
        tools_a, tools_b = _tools(a), _tools(b)
        call = lambda node, tools, name, args: handle(node, tools, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",  # noqa: E731
                                                                    "params": {"name": name, "arguments": args}})
        for node, tools in ((a, tools_a), (b, tools_b)):
            call(node, tools, "join_mission", {"mission": mid})
        assert not call(a, tools_a, "claim_work", {"mission": mid, "work_id": wid})["result"]["isError"]
        second = call(b, tools_b, "claim_work", {"mission": mid, "work_id": wid})["result"]  # b has not synced since
        assert second["isError"] and "claimed by another agent" in second["content"][0]["text"]
        assert hub.world().missions[mid]["work"][hub.world().resolve(wid, hub.world().missions[mid]["work"])]["claimed_by"] == a.agent_id
    finally:
        srv.shutdown()
