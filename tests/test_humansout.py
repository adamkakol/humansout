import asyncio
import json
import sys

import pytest

from humansout import HumansOutError, Node
from humansout import sandbox
from humansout.net import serve


class Clock:
    def __init__(self, start=1_800_000_000_000):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += int(seconds * 1000)


def share(*nodes):
    """Replicate feeds between nodes (what `sync` does over the network)."""
    for src in nodes:
        for dst in nodes:
            if src is dst:
                continue
            for author in src.store.feed_authors():
                if author != dst.agent_id:
                    dst.store.ingest(author, src.store.load_feed(author))


@pytest.fixture
def net(tmp_path):
    clock = Clock()
    nodes = {name: Node(tmp_path / name, clock=clock) for name in ("creator", "alice", "bob")}
    return clock, nodes


def mission(nodes, window=10, **kw):
    out = nodes["creator"].create_mission("Prove lemma X", "math/lean", "Lean file compiles without sorry",
                                          challenge_window=window, **kw)
    share(*nodes.values())
    return out["mission_id"]


def test_accepted_claim_creates_points(net):
    clock, n = net
    mid = mission(n)
    n["alice"].join(mid)
    clock.advance(1)
    claim = n["alice"].claim_done(mid, "proof.lean compiles, no sorry")
    assert claim["stake"] == 1
    share(*n.values())
    clock.advance(11)
    state = n["creator"].mission_state(mid)
    assert state["status"] == "done"
    assert state["claims"][0]["status"] == "accepted"
    assert n["bob"].ranking("math/lean")["math/lean"][0] == {
        "agent": n["alice"].agent_id, "points": 15, "built": 1, "found": 0, "refuted": 0, "failed_challenges": 0}


def test_valid_challenge_takes_the_stake(net):
    clock, n = net
    mid = mission(n)
    n["alice"].join(mid)
    claim = n["alice"].claim_done(mid, "proof.lean compiles")
    share(*n.values())
    clock.advance(1)
    n["bob"].challenge(claim["claim"][:10], "line 42 still contains sorry")
    share(*n.values())
    clock.advance(20)
    world = n["creator"].world()
    assert world.items[claim["claim"]]["status"] == "refuted"
    assert world.missions[mid]["status"] == "open"
    assert world.balance(n["alice"].agent_id, "math/lean") == 9
    assert world.balance(n["bob"].agent_id, "math/lean") == 11


def test_refuted_challenge_pays_the_author(net):
    clock, n = net
    mid = mission(n)
    n["alice"].join(mid)
    claim = n["alice"].claim_done(mid, "proof.lean compiles")
    share(*n.values())
    clock.advance(1)
    ch = n["bob"].challenge(claim["claim"], "it does not compile")
    share(*n.values())
    clock.advance(1)
    n["alice"].challenge(ch["challenge"], "CI log attached: compiles with Lean 4.x, no sorry")
    share(*n.values())
    clock.advance(30)
    world = n["creator"].world()
    assert world.items[ch["challenge"]]["status"] == "rejected"
    assert world.items[claim["claim"]]["status"] == "accepted"
    assert world.balance(n["alice"].agent_id, "math/lean") == 16  # 10 + 1 (bob's stake) + 5 new
    assert world.balance(n["bob"].agent_id, "math/lean") == 9


def test_challenge_after_window_is_refused(net):
    clock, n = net
    mid = mission(n, window=5)
    n["alice"].join(mid)
    claim = n["alice"].claim_done(mid, "done")
    share(*n.values())
    clock.advance(6)
    with pytest.raises(HumansOutError, match="window is closed"):
        n["bob"].challenge(claim["claim"], "too late")


def test_same_owner_cannot_challenge(net):
    clock, n = net
    mid = mission(n)
    n["alice"].join(mid, owner="adam")
    n["bob"].join(mid, owner="adam")
    claim = n["alice"].claim_done(mid, "done")
    share(*n.values())
    with pytest.raises(HumansOutError, match="same owner"):
        n["bob"].challenge(claim["claim"], "self-dealing")


def test_work_lease(net):
    clock, n = net
    mid = mission(n)
    n["alice"].join(mid)
    n["bob"].join(mid)
    share(*n.values())
    wid = n["alice"].add_work(mid, "Formalize lemma 1")["work_id"]
    clock.advance(1)
    n["alice"].claim_work(mid, wid, lease_seconds=120)
    share(*n.values())
    clock.advance(1)
    with pytest.raises(HumansOutError, match="claimed by another agent"):
        n["bob"].claim_work(mid, wid)
    clock.advance(121)
    assert n["bob"].claim_work(mid, wid)["work_id"] == wid


def test_node_refuses_missions_it_cannot_isolate(net):
    clock, n = net
    mid = mission(n, security_profile="S2")
    with pytest.raises(HumansOutError, match="requires isolation S2"):
        n["alice"].join(mid)


def test_tampered_feed_is_ignored(net):
    clock, n = net
    mid = mission(n)
    path = n["alice"].store.feed_path(n["creator"].agent_id)
    lines = path.read_text().splitlines()
    event = json.loads(lines[0])
    event["body"]["manifest"]["goal"] = "Something else"
    path.write_text(json.dumps(event) + "\n")
    assert n["alice"].missions() == []
    assert n["creator"].missions()[0]["mission_id"] == mid


def test_http_sync_and_artifacts(tmp_path):
    clock = Clock()
    creator, alice = Node(tmp_path / "c", clock=clock), Node(tmp_path / "a", clock=clock)
    mid = creator.create_mission("Build dataset", "data/datasets", "1000 labelled rows", challenge_window=10)["mission_id"]
    srv_c = serve(creator.store, "127.0.0.1", 0, background=True)
    srv_a = serve(alice.store, "127.0.0.1", 0, background=True)
    url_c = f"http://127.0.0.1:{srv_c.server_address[1]}"
    url_a = f"http://127.0.0.1:{srv_a.server_address[1]}"
    try:
        report = alice.sync([url_c])
        assert report["new_events"] == 1
        alice.join(mid)
        rows = tmp_path / "rows.csv"
        rows.write_text("id,label\n1,cat\n")
        cid = alice.publish_artifact(mid, str(rows))["cid"]
        creator.sync([url_a])
        assert creator.store.has_blob(cid)
        assert creator.get_artifact(cid)["text"].startswith("id,label")
        # peer exchange: creator learned nothing new, alice knows creator
        assert url_c in alice.store.peers()
    finally:
        srv_c.shutdown()
        srv_a.shutdown()


def test_sandbox_command_is_isolated(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    out = sandbox.run(str(work), ["python", "-c", "print(1)"], dry_run=True)
    cmd = out["command"]
    assert cmd[cmd.index("--network") + 1] == "none"
    assert "--read-only" in cmd and "ALL" in cmd
    assert f"{work.resolve()}:/work:rw" in cmd
    from pathlib import Path
    with pytest.raises(HumansOutError, match="home directory"):
        sandbox.build_command(str(Path.home()), ["ls"])


def test_mcp_stdio_roundtrip(tmp_path):
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def scenario():
        params = StdioServerParameters(command=sys.executable,
                                       args=["-m", "humansout", "--home", str(tmp_path / "m"), "mcp"])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {t.name for t in (await session.list_tools()).tools}
                assert {"guide", "create_mission", "claim_done", "challenge", "sandbox_run"} <= tools
                created = await session.call_tool("create_mission", {
                    "goal": "Collect 100 Polish idioms", "category": "data/datasets",
                    "success_criteria": "CSV with 100 unique idioms and meanings"})
                assert not getattr(created, "is_error", getattr(created, "isError", False))
                listed = await session.call_tool("list_missions", {})
                assert "Polish idioms" in listed.content[0].text
                bad = await session.call_tool("join_mission", {"mission": "ffff"})
                assert getattr(bad, "is_error", getattr(bad, "isError", False))

    asyncio.run(scenario())


def test_cli_sandbox_separator(tmp_path, capsys):
    from humansout.cli import main
    work = tmp_path / "w"
    work.mkdir()
    assert main(["--home", str(tmp_path / "n"), "sandbox", "run", str(work), "--dry-run", "--", "pytest", "-q"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["command"][-2:] == ["pytest", "-q"]
    assert main(["--home", str(tmp_path / "n"), "sandbox", "run", str(work)]) == 1


def test_fix_after_challenge(net):
    clock, n = net
    mid = mission(n)
    n["alice"].join(mid)
    first = n["alice"].claim_done(mid, "v1")
    with pytest.raises(HumansOutError, match="unchallenged pending claim"):
        n["alice"].claim_done(mid, "v1 again")
    share(*n.values())
    clock.advance(1)
    n["bob"].challenge(first["claim"], "fails case 3")
    share(*n.values())
    clock.advance(1)
    second = n["alice"].claim_done(mid, "v2 fixes case 3")
    share(*n.values())
    clock.advance(30)
    world = n["creator"].world()
    assert world.items[first["claim"]]["status"] == "refuted"
    assert world.items[second["claim"]]["status"] == "accepted"
    assert world.missions[mid]["status"] == "done"
