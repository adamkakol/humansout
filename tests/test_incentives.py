"""Records, badges and bounty-aware watch: what an agent owner gets out of the swarm."""

import json
import urllib.request

import pytest

from humansout import HumansOutError, Node
from humansout import dashboard, watch
from humansout.cli import main
from humansout.net import serve


class Clock:
    def __init__(self, start):
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


def setup(tmp_path, start=1_800_000_000_000):
    clock = Clock(start)
    n = {x: Node(tmp_path / x, clock=clock) for x in ("creator", "alice", "bob")}
    test = tmp_path / "test_basic.py"
    test.write_text("import unittest\n")
    mid = n["creator"].create_mission("Write slugify(text)", "software/python", "ASCII slugs", challenge_window=100,
                                      dispute_window=5, check={"command": ["python", "-m", "unittest"],
                                                               "deliverable": "slugify.py", "files": [str(test)]})["mission_id"]
    share(*n.values())
    n["alice"].join(mid)
    f = tmp_path / "slugify.py"
    f.write_text("def slugify(s):\n    return s\n")
    cid = n["alice"].publish_artifact(mid, str(f))["cid"]
    claim = n["alice"].claim_done(mid, "v1", cids=[cid])["claim"]
    share(*n.values())
    for node in n.values():  # the mission's own check passes on it; bob's extra test does not
        node.store.save_verification(node.world().items[claim]["check_key"], {"result": "pass"})
    clock.advance(1)
    edge = tmp_path / "bob-tests" / "test_edge.py"
    edge.parent.mkdir()
    edge.write_text("import unittest\n")
    ch = n["bob"].challenge(claim, "slugify returns its input unchanged", counterexample=[str(edge)],
                            preflight=False)["challenge"]
    share(*n.values())
    for node in n.values():
        node.store.save_verification(node.world().items[ch]["vkey"], {"result": "fail"})
    clock.advance(6)
    return clock, n, mid, claim, ch


def test_record_lists_the_signed_work(tmp_path):
    clock, n, mid, claim, ch = setup(tmp_path)
    rec = dashboard.agent_record(n["creator"].store, n["bob"].agent_id, now=clock())
    assert rec["totals"]["found"] == 1 and rec["fields"]["software/python"]["points"] == 10 + 1 + 8
    assert [(w["event"], w["mode"], w["verification"], w["status"]) for w in rec["work"]] == [
        (ch, "counterexample", "reproduced", "upheld")]
    assert rec["credits"]["credits"] == 2  # experiment mode: every creator is trusted, a find mints 2


def test_badge_cli_and_route_escape_input(tmp_path, capsys):
    clock, n, mid, claim, ch = setup(tmp_path, start=1_700_000_000_000)  # in the past: the route reads the wall clock
    svg = dashboard.badge(n["creator"].store, n["bob"].agent_id)
    assert svg.startswith("<svg") and "0 built · 1 found · 0 refuted" in svg
    out = tmp_path / "badge.svg"
    assert main(["--home", str(n["bob"].home), "badge", "--out", str(out)]) == 0
    assert "1 found" in out.read_text() and "![HumansOut record]" in json.loads(capsys.readouterr().out)["markdown"]
    srv = serve(n["creator"].store, "127.0.0.1", 0, background=True)
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/badge/{n['bob'].agent_id}.svg?domain=%3Cscript%3E"
        with urllib.request.urlopen(url, timeout=10) as r:
            body = r.read().decode()
            assert r.headers["Content-Type"].startswith("image/svg+xml")
            assert "default-src 'none'" in r.headers["Content-Security-Policy"]
        assert "<script>" not in body and "&lt;script&gt;" in body
        with urllib.request.urlopen(url.split("/badge/")[0] + f"/api/agent/{n['alice'].agent_id}", timeout=10) as r:
            assert json.loads(r.read())["totals"]["refuted"] == 1
    finally:
        srv.shutdown()


def test_watch_prefers_bounties_and_respects_the_floor(tmp_path):
    clock = Clock(1_800_000_000_000)
    creator, worker = Node(tmp_path / "c", clock=clock), Node(tmp_path / "w", clock=clock)
    creator.store.set_config("start_credits", 20)
    worker.store.set_config("start_credits", 20)
    plain = creator.create_mission("plain", "software/python", "x")["mission_id"]
    small = creator.create_mission("small", "software/python", "x", bounty={"build": 2})["mission_id"]
    big = creator.create_mission("big", "software/python", "x", bounty={"build": 9})["mission_id"]
    share(creator, worker)
    cfg = watch.WatchConfig(roles=("build",))
    order = [e.mission for e in watch.find_work(worker.world(), worker.agent_id, clock(), cfg, watch.load_state(worker.home))]
    assert order == [big, small, plain]
    floor = watch.WatchConfig(roles=("build",), min_bounty=3)
    assert [e.mission for e in watch.find_work(worker.world(), worker.agent_id, clock(), floor,
                                               watch.load_state(worker.home))] == [big]


def test_watch_pauses_agent_turns_outside_active_hours(tmp_path):
    clock = Clock(1_800_000_000_000)
    creator, worker = Node(tmp_path / "c", clock=clock), Node(tmp_path / "w", clock=clock)
    creator.create_mission("plain", "software/python", "x")
    share(creator, worker)
    hour = __import__("time").localtime().tm_hour
    closed = ((hour + 1) % 24, (hour + 2) % 24)  # a window that excludes the current hour
    cfg = watch.WatchConfig(harness=["true", "{prompt}"], roles=("build",), active_hours=closed, verify=False)
    report = watch.cycle(worker, cfg)
    assert report["turns"] == [] and report["paused"].startswith("outside active hours")
    assert [e["kind"] for e in report["events"]] == ["build"]  # it still sees the work


def test_hall_of_breaks(tmp_path, capsys):
    clock, n, mid, claim, ch = setup(tmp_path)
    rows = dashboard.finds(n["creator"].store, now=clock())
    assert [(r["challenge"], r["finder"], r["victim"]) for r in rows] == [(ch, n["bob"].agent_id, n["alice"].agent_id)]
    md = dashboard.finds_markdown(rows)
    assert md.startswith("# Hall of breaks") and "broke the claim" in md


def test_seasons_count_only_points_created_that_month(tmp_path, capsys):
    import datetime as dt
    from humansout.cli import main
    oct_ms = int(dt.datetime(2026, 10, 30, tzinfo=dt.timezone.utc).timestamp() * 1000)

    class Clock:
        t = oct_ms

        def __call__(self):
            return self.t

    clock = Clock()
    creator, old, new = (Node(tmp_path / n, clock=clock) for n in ("creator", "old", "new"))
    nodes = (creator, old, new)

    def share():
        for src in nodes:
            for dst in nodes:
                if src is not dst:
                    for author in src.store.feed_authors():
                        if author != dst.agent_id:
                            dst.store.ingest(author, src.store.load_feed(author))

    def build(who, goal):
        mid = creator.create_mission(goal, "software/python", "c", challenge_window=60)["mission_id"]
        share()
        who.join(mid)
        claim = who.claim_done(mid, "evidence text")["claim"]
        share()
        return claim

    build(old, "October work")
    build(old, "more October work")
    clock.t += 7 * 86400 * 1000  # November
    build(new, "November work")
    clock.t += 3600 * 1000
    view = creator.ranking(season="2026-11")["ranking"]["software/python"]
    assert [r["agent"] for r in view] == [new.agent_id] and view[0]["built"] == 1 and view[0]["points"] == 5
    alltime = creator.ranking("software/python")["software/python"]
    assert alltime[0]["agent"] == old.agent_id  # all time, the October builder still leads
    assert main(["--home", str(creator.home), "ranking", "--season", "2026-13"]) == 1
    assert "season must look like 2026-10" in capsys.readouterr().err


def test_a_mistyped_work_item_can_be_cancelled_by_its_creator(tmp_path):
    from humansout.cli import main
    import time
    clock = Clock(int(time.time() * 1000) - 3_600_000)  # in the past: the CLI below uses the real clock
    creator, agent = Node(tmp_path / "c", clock=clock), Node(tmp_path / "a", clock=clock)
    mid = creator.create_mission("g", "software/python", "c", challenge_window=60)["mission_id"]
    clock.advance(1)
    bad = creator.add_work(mid, "District Góra (, seats)")["work_id"]
    used = creator.add_work(mid, "District 8")["work_id"]
    clock.advance(1)
    for a in creator.store.feed_authors():
        agent.store.ingest(a, creator.store.load_feed(a))
    agent.join(mid)
    with pytest.raises(HumansOutError, match="only the work item's creator or the mission creator"):
        agent.cancel_work(mid, bad)
    agent.claim_done(mid, "done", work_id=used)
    clock.advance(1)  # events in the same millisecond are ordered by author: keep the order explicit
    for a in agent.store.feed_authors():
        creator.store.ingest(a, agent.store.load_feed(a))
    with pytest.raises(HumansOutError, match="already has a claim"):
        creator.cancel_work(mid, used)
    assert main(["--home", str(creator.home), "work", "cancel", mid, bad, "--reason", "typo"]) == 0
    clock.advance(7200)  # the CLI wrote at real time: catch up
    work = {w["id"]: w for w in creator.mission_state(mid)["work"]}
    assert work[bad]["status"] == "cancelled"
    for a in creator.store.feed_authors():
        agent.store.ingest(a, creator.store.load_feed(a))
    with pytest.raises(HumansOutError, match="was cancelled"):
        agent.claim_work(mid, bad)
