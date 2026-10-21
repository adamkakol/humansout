"""Time witnesses: an event counts from when the network first saw it, so backdating wins nothing."""

from humansout import Node
from humansout.state import build_world
from humansout.witness import MAX_HEADS, checkpoint, effective_times


class Clock:
    def __init__(self, start=1_800_000_000_000):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds=1):
        self.t += int(seconds * 1000)


def share(src, *dsts):
    for dst in dsts:
        for author in src.store.feed_authors():
            if author != dst.agent_id:
                dst.store.ingest(author, src.store.load_feed(author))


def fold(node, witnesses, now):
    return build_world(node.store.all_events(), dict(node.store.config(), time_witnesses=witnesses), now, {})


def test_a_backdated_claim_loses_to_the_claim_the_network_saw_first(tmp_path):
    clock = Clock()
    hub, creator, alice = (Node(tmp_path / n, clock=clock) for n in ("hub", "creator", "alice"))
    back = [0]
    mallory = Node(tmp_path / "mallory", clock=lambda: back[0])  # writes whatever time suits her
    mid = creator.create_mission("Port the parser", "software/python", "tests pass", challenge_window=3600)["mission_id"]
    share(creator, hub, alice, mallory)
    checkpoint(hub)
    clock.advance(60)
    alice.join(mid)
    clock.advance(1)
    alice_claim = alice.claim_done(mid, "ported; all tests pass")["claim"]
    share(alice, hub, mallory)
    clock.advance(30)
    checkpoint(hub)  # the hub saw alice's claim
    for _ in range(20):  # ten minutes of heartbeats, then mallory copies the idea and dates hers before alice's
        clock.advance(30)
        checkpoint(hub)
    back[0] = alice.store.load_feed(alice.agent_id)[-1]["ts"] - 300_000
    mallory.join(mid)
    back[0] += 1
    mallory_claim = mallory.claim_done(mid, "ported; all tests pass")["claim"]
    share(mallory, hub)
    clock.advance(30)
    checkpoint(hub)
    clock.advance(7200)  # both windows are long closed
    trusting_authors = fold(hub, [], clock())
    assert trusting_authors.missions[mid]["done_claim"] == mallory_claim  # the attack, without a witness
    witnessed = fold(hub, [hub.agent_id], clock())
    assert witnessed.missions[mid]["done_claim"] == alice_claim
    assert witnessed.items[mallory_claim]["status"] == "superseded"
    seen = witnessed.items[mallory_claim]["ts"]
    assert seen > witnessed.items[alice_claim]["ts"] + 600_000  # timed within the 30 s in which the hub got it
    assert clock() - 7200_000 - 60_000 <= seen


def test_starting_a_witness_does_not_move_history(tmp_path):
    clock = Clock()
    hub, alice = Node(tmp_path / "hub", clock=clock), Node(tmp_path / "alice", clock=clock)
    mid = alice.create_mission("g", "software/python", "c", challenge_window=600)["mission_id"]
    clock.advance(5)
    alice.add_work(mid, "part one")
    clock.advance(86400)  # a day later, the hub starts witnessing
    share(alice, hub)
    first, _ = checkpoint(hub)
    assert first and first[0]["body"].get("genesis") is True
    times = effective_times(hub.store.all_events(), {hub.agent_id})
    assert all(times[e["id"]] == e["ts"] for e in alice.store.load_feed(alice.agent_id))
    clock.advance(30)
    again, _ = checkpoint(hub)
    assert [e["body"] for e in again] == [{"heads": {}}]  # nothing grew: a heartbeat
    clock.advance(10)
    alice.add_work(mid, "part two")
    share(alice, hub)
    clock.advance(30)
    later, _ = checkpoint(hub)
    assert later and "genesis" not in later[0]["body"]


def test_an_event_no_witness_has_seen_counts_after_the_latest_checkpoint(tmp_path):
    clock = Clock()
    hub, alice = Node(tmp_path / "hub", clock=clock), Node(tmp_path / "alice", clock=clock)
    alice.create_mission("g", "software/python", "c")
    share(alice, hub)
    checkpoint(hub)
    clock.advance(300)
    hub.store.ingest(alice.agent_id, alice.store.load_feed(alice.agent_id))
    clock.advance(1)
    checkpoint(hub)  # nothing new: no event
    last = max(e["ts"] for e in hub.store.load_feed(hub.agent_id))
    old = Node(tmp_path / "old", clock=lambda: last - 3_600_000)  # dated an hour before the last checkpoint
    old.create_mission("late", "software/python", "c")
    share(old, hub)
    times = effective_times(hub.store.all_events(), {hub.agent_id})
    event = old.store.load_feed(old.agent_id)[0]
    assert times[event["id"]] == last + 1


def test_only_configured_witnesses_count_and_big_networks_split_into_several_events(tmp_path):
    clock = Clock()
    hub, rogue = Node(tmp_path / "hub", clock=clock), Node(tmp_path / "rogue", clock=clock)
    authors = []
    for i in range(MAX_HEADS + 5):  # more feeds than one witness event may list
        n = Node(tmp_path / f"a{i}", clock=clock)
        n.store.append_own(n.identity, "keys", {"x25519": "0" * 64}, clock())
        hub.store.ingest(n.agent_id, n.store.load_feed(n.agent_id))
        authors.append(n.agent_id)
    written, last = checkpoint(hub)
    assert len(written) == 2 and set(last) == set(authors)
    share(hub, rogue)
    checkpoint(rogue)
    world = fold(rogue, [hub.agent_id], clock() + 1000)
    assert {r["author"] for r in world.rejected if r["type"] == "witness"} == {rogue.agent_id}


def test_a_witnessing_hub_says_so_and_attach_trusts_it_for_time(tmp_path, monkeypatch):
    import json
    import urllib.request
    from humansout import attach as attach_mod
    from humansout.net import serve
    hub = Node(tmp_path / "hub")
    hub.store.set_config("witness", True)
    srv = serve(hub.store, "127.0.0.1", 0, background=True, agent_id=hub.agent_id)
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with urllib.request.urlopen(f"{url}/", timeout=10) as resp:
            assert "witness" in json.loads(resp.read())["features"]
        assert attach_mod.hub_witnesses(url, hub.agent_id)
        assert not attach_mod.hub_witnesses(url, "f" * 64)  # someone else's key: not trusted for time
    finally:
        srv.shutdown()


def test_a_scoped_sync_still_pulls_the_witness_feed(tmp_path):
    from humansout.net import serve
    clock = Clock()
    hub, alice, bob = (Node(tmp_path / n, clock=clock) for n in ("hub", "alice", "bob"))
    hub.store.set_config("accept_push", True)
    mid = alice.create_mission("g", "software/python", "c")["mission_id"]
    share(alice, hub)
    checkpoint(hub)
    srv = serve(hub.store, "127.0.0.1", 0, background=True, agent_id=hub.agent_id)
    try:
        bob.store.set_config("time_witnesses", [hub.agent_id])
        bob.sync([f"http://127.0.0.1:{srv.server_address[1]}"], missions=[mid])
    finally:
        srv.shutdown()
    assert bob.store.feed_len(hub.agent_id) == hub.store.feed_len(hub.agent_id) > 0


def test_within_one_checkpoint_the_earlier_lease_wins_not_the_smaller_agent_id(tmp_path):
    """Found in the security-review swarm: three leases on one item reached the hub within one interval and
    the smallest agent id won, though another agent had leased first."""
    clock = Clock()
    hub, creator = Node(tmp_path / "hub", clock=clock), Node(tmp_path / "creator", clock=clock)
    mid = creator.create_mission("Review", "security/x", "c", rules={"exclusive_items": 1})["mission_id"]
    wid = creator.add_work(mid, "area")["work_id"]
    share(creator, hub)
    checkpoint(hub)
    agents = sorted((Node(tmp_path / f"a{i}", clock=clock) for i in range(4)), key=lambda n: n.agent_id)
    first, later = agents[-1], agents[0]  # the one with the largest id leases first
    for n in (first, later):
        share(creator, n)
        n.join(mid)
    clock.advance(5)
    first.claim_work(mid, wid)
    clock.advance(5)
    later.claim_work(mid, wid)
    for _ in range(4):  # the hub's heartbeats go on while both agents work, then both arrive in one interval
        clock.advance(30)
        checkpoint(hub)
    for n in (first, later):
        share(n, hub)
    clock.advance(10)
    checkpoint(hub)
    world = fold(hub, [hub.agent_id], clock())
    work = world.missions[mid]["work"][world.resolve(wid, world.missions[mid]["work"])]
    assert work["claimed_by"] == first.agent_id
