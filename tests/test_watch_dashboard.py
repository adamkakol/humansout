"""`humansout watch` and the dashboard.

The end-to-end test drives four nodes over HTTP only through watch cycles, with a scripted
agent harness (tests/fake_agent.py) and real sandbox verification. Real clock, short windows.
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from humansout import Node, sandbox
from humansout import watch
from humansout.net import serve

HERE = Path(__file__).resolve().parent
IMAGE = "python:3.12-slim"


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


def engine_ready() -> bool:
    engine = sandbox.find_engine()
    return bool(engine) and subprocess.run([engine, "image", "inspect", IMAGE], capture_output=True).returncode == 0


def runnable_mission(creator, tmp_path, window=100, dispute=5):
    test = tmp_path / "creator" / "test_basic.py"
    test.parent.mkdir(parents=True, exist_ok=True)
    test.write_text("import unittest\nfrom slugify import slugify\n\nclass T(unittest.TestCase):\n"
                    "    def test_basic(self):\n        self.assertEqual(slugify('Hello World'), 'hello-world')\n")
    return creator.create_mission("Write slugify(text)", "software/python", "lower-case ASCII words joined by '-'",
                                  challenge_window=window, dispute_window=dispute,
                                  check={"command": ["python", "-m", "unittest", "discover", "-s", "/work"],
                                         "deliverable": "slugify.py", "files": [str(test)], "image": IMAGE})["mission_id"]


# ---- unit: what wakes the agent -----------------------------------------------------------
def test_find_work_orders_and_filters(tmp_path):
    clock = Clock()
    creator, alice, bob = (Node(tmp_path / n, clock=clock) for n in ("creator", "alice", "bob"))
    mid = runnable_mission(creator, tmp_path)
    other = creator.create_mission("Collect idioms", "data/datasets", "100 idioms")["mission_id"]
    share(creator, alice, bob)
    cfg = watch.WatchConfig(roles=("build", "break"), topics=("software",))
    events = watch.find_work(alice.world(), alice.agent_id, clock(), cfg, watch.load_state(alice.home))
    assert [(e.kind, e.mission) for e in events] == [("build", mid)]  # data mission filtered out by topic
    assert other not in [e.mission for e in events]

    alice.join(mid)
    code = tmp_path / "slugify.py"
    code.write_text("def slugify(s):\n    return s\n")
    cid = alice.publish_artifact(mid, str(code))["cid"]
    claim = alice.claim_done(mid, "v1", cids=[cid])["claim"]
    share(creator, alice, bob)
    bob_events = watch.find_work(bob.world(), bob.agent_id, clock(), cfg, watch.load_state(bob.home))
    # free choice: bob is offered review first, and building too; the prompt tells him alice's claim may win
    assert [(e.kind, e.item or e.mission) for e in bob_events] == [("review", claim), ("build", mid)]
    build = next(e for e in bob_events if e.kind == "build")
    assert "Claims by others: a claim by agent " + alice.agent_id[:8] in watch.render_prompt(
        bob, bob.world(), build, watch.WatchConfig(), tmp_path)
    assert watch.find_work(alice.world(), alice.agent_id, clock(), cfg, watch.load_state(alice.home)) == []

    clock.advance(1)
    ch = bob.challenge(claim, "fails", counterexample=[], preflight=False)
    share(creator, alice, bob)
    alice_events = watch.find_work(alice.world(), alice.agent_id, clock(), cfg, watch.load_state(alice.home))
    assert [(e.kind, e.detail["objections"]) for e in alice_events] == [("respond", [ch["challenge"]])]


def test_the_creator_is_woken_only_where_no_jury_can_sit(tmp_path):
    clock = Clock()
    creator, alice, bob = (Node(tmp_path / n, clock=clock) for n in ("creator", "alice", "bob"))
    mid = runnable_mission(creator, tmp_path)
    share(creator, alice, bob)  # only the two parties take part: no juror is left
    alice.join(mid)
    code = tmp_path / "slugify.py"
    code.write_text("def slugify(s):\n    return s\n")
    claim = alice.claim_done(mid, "v1", cids=[alice.publish_artifact(mid, str(code))["cid"]])["claim"]
    share(creator, alice, bob)
    clock.advance(1)
    edge = tmp_path / "bob" / "test_edge.py"
    edge.parent.mkdir(parents=True, exist_ok=True)
    edge.write_text("import unittest\n")
    ce = bob.challenge(claim, "fails", counterexample=[str(edge)], preflight=False)["challenge"]
    share(creator, alice, bob)
    for node in (creator, alice, bob):
        node.store.save_verification(node.world().items[ce]["vkey"], {"result": "fail"})
    clock.advance(1)
    alice.dispute(ce, "it expects something the mission never asks")
    share(creator, alice, bob)
    assert creator.world().items[ce]["jury"] == []
    events = watch.find_work(creator.world(), creator.agent_id, clock(), watch.WatchConfig(roles=()),
                             watch.load_state(creator.home))
    assert [(e.kind, e.item) for e in events] == [("ruling", ce)]


def test_budget_and_dry_run(tmp_path):
    clock = Clock()
    creator, alice = Node(tmp_path / "c", clock=clock), Node(tmp_path / "a", clock=clock)
    runnable_mission(creator, tmp_path)
    share(creator, alice)
    record = tmp_path / "turns.txt"
    harness = [sys.executable, "-c", f"open({str(record)!r}, 'a').write('turn\\n')", "{prompt_file}"]
    cfg = watch.WatchConfig(harness=harness, max_turns_per_hour=1, retry_build_after=0, verify=False)
    dry = watch.cycle(alice, watch.WatchConfig(harness=harness, dry_run=True, verify=False))
    assert dry["events"][0]["kind"] == "build" and dry["turns"] == [] and not record.exists()
    first = watch.cycle(alice, cfg)
    assert [t["kind"] for t in first["turns"]] == ["build"] and record.read_text() == "turn\n"
    second = watch.cycle(alice, cfg)  # the agent did not claim, so it may retry, but the hourly budget is spent
    assert second["turns"] == [] and "exhausted" in second["budget"]
    prompt = next((alice.home / "watch" / "turns").glob("*.prompt.txt")).read_text()
    assert "Do only this, then stop." in prompt and "slugify.py" in prompt


# ---- dashboard API ---------------------------------------------------------------------------
def get(url, accept="application/json"):
    req = urllib.request.Request(url, headers={"Accept": accept})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, dict(r.headers), r.read()


def test_dashboard_views(tmp_path):
    clock = Clock(int(time.time() * 1000) - 60_000)  # the dashboard reads the wall clock
    creator, alice = Node(tmp_path / "c", clock=clock), Node(tmp_path / "a", clock=clock)
    mid = runnable_mission(creator, tmp_path)
    share(creator, alice)
    alice.join(mid)
    code = tmp_path / "slugify.py"
    code.write_text("def slugify(s):\n    return '<script>alert(1)</script>'\n")
    cid = alice.publish_artifact(mid, str(code))["cid"]
    alice.claim_done(mid, "<img src=x onerror=alert(1)>", cids=[cid])
    share(creator, alice)
    creator.store.put_blob(alice.store.get_blob(cid))  # what sync does for files
    srv = serve(creator.store, "127.0.0.1", 0, background=True, agent_id=creator.agent_id)
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        status, headers, body = get(url + "/dashboard", "text/html")
        assert status == 200 and b"dashboard.js" in body
        assert "script-src 'self'" in headers["Content-Security-Policy"]
        _, _, root_html = get(url + "/", "text/html")
        assert b"<title>HumansOut node</title>" in root_html
        _, _, root_json = get(url + "/")
        assert json.loads(root_json)["service"] == "humansout-node"
        _, js_headers, js = get(url + "/dashboard.js")
        assert js_headers["Content-Type"].startswith("text/javascript") and b"innerHTML" not in js
        overview = json.loads(get(url + "/api/overview")[2])
        assert overview["agent_id"] == creator.agent_id
        assert overview["missions"][0]["id"] == mid and overview["missions"][0]["claims_pending"] == 1
        detail = json.loads(get(url + f"/api/mission/{mid}")[2])
        assert [e["type"] for e in detail["timeline"]] == ["mission.announce", "join", "artifact.publish", "claim_done"]
        assert detail["claims"][0]["evidence"]["text"] == "<img src=x onerror=alert(1)>"  # raw data, rendered as text
        _, blob_headers, blob = get(url + f"/api/blob/{cid}")
        assert blob_headers["Content-Type"].startswith("text/plain") and b"<script>" in blob
        assert blob_headers["X-Content-Type-Options"] == "nosniff"
    finally:
        srv.shutdown()


# ---- end to end: only watch cycles drive the agents --------------------------------------------
@pytest.mark.skipif(not engine_ready(), reason=f"needs docker or podman with {IMAGE}")
def test_watch_drives_a_mission_end_to_end(tmp_path):
    names = ["creator", "builder", "breaker", "breaker2"]
    nodes = {n: Node(tmp_path / n) for n in names}
    servers = {n: serve(nodes[n].store, "127.0.0.1", 0, background=True, agent_id=nodes[n].agent_id) for n in names}
    urls = {n: f"http://127.0.0.1:{servers[n].server_address[1]}" for n in names}
    for n in names:
        for m in names:
            if m != n:
                nodes[n].store.add_peer(urls[m])
    notes = tmp_path / "notifications.txt"

    def cfg(who, roles):
        return watch.WatchConfig(
            harness=[sys.executable, str(HERE / "fake_agent.py"), "{prompt_file}", who], roles=roles,
            retry_build_after=0, turn_timeout=120, server="humansout",
            notify=[sys.executable, "-c", f"import sys; open({str(notes)!r}, 'a').write(sys.argv[1] + '\\n')", "{message}"],
            cli=[sys.executable, "-m", "humansout"])

    def run(who, roles):
        report = watch.cycle(nodes[who], cfg(who, roles))
        time.sleep(1.05)  # serving nodes pick up other processes' writes within a second (store.refresh_index)
        return report

    try:
        mid = runnable_mission(nodes["creator"], tmp_path / "files", window=60, dispute=6)
        nodes["builder"].sync()
        assert [t["kind"] for t in run("builder", ("build",))["turns"]] == ["build"]
        assert [t["kind"] for t in run("breaker", ("break",))["turns"]] == ["review"]
        respond = run("builder", ("build",))
        assert [v["counterexample"] for v in respond["verified"] if "counterexample" in v] == ["reproduced"]
        assert [t["kind"] for t in respond["turns"]] == ["respond"]  # it fixed the code and claimed v2
        time.sleep(7)  # the first claim's dispute window ends
        run("creator", ())
        assert run("breaker", ("break",))["turns"][0]["kind"] == "review"  # tries the same test on v2
        assert [t["kind"] for t in run("breaker2", ("break",))["turns"]] == ["review"]
        builder = run("builder", ("build",))
        assert [v["counterexample"] for v in builder["verified"] if "counterexample" in v] == ["reproduced"]  # bob's v2 objection was stopped by preflight
        assert [t["kind"] for t in builder["turns"]] == ["respond"]  # it disputes the unicode test
        creator = run("creator", ())
        assert creator["notifications"] == [] and not notes.exists()  # a jury of agents decides: no human is woken
        assert json.loads(get(urls["creator"] + "/api/overview")[2])["missions"][0]["with_jury"] == 1
        juror = run("breaker", ("break",))  # the only agent outside the dispute sits on its jury
        assert [t["kind"] for t in juror["turns"]] == ["jury"]
        for _ in range(40):  # wait for v2's challenge window to close
            for n in names:
                run(n, ())
            if all(nodes[n].missions()[0]["status"] == "done" for n in names):
                break
            time.sleep(2)
        views = {n: nodes[n].mission_state(mid)["claims"] for n in names}
        assert len({json.dumps(v, sort_keys=True) for v in views.values()}) == 1
        statuses = [c["status"] for c in views["creator"]]
        assert statuses == ["refuted", "accepted"]
        unicode_test = next(ch for ch in views["creator"][1]["challenges"] if ch["files"][0]["name"] == "test_unicode.py")
        assert unicode_test["status"] == "rejected" and unicode_test["reason"] == "rejected by the jury"
        overview = json.loads(get(urls["creator"] + "/api/overview")[2])
        assert overview["missions"][0]["status"] == "done" and overview["missions"][0]["needs_ruling"] == 0
    finally:
        for srv in servers.values():
            srv.shutdown()


def test_model_flags_and_a_per_node_mcp_config(tmp_path):
    claude = watch.parse_harness("claude", "humansout", "claude-haiku-5-5")
    assert claude[:3] == ["claude", "--model", "{model}"] and "--strict-mcp-config" in claude
    assert watch.parse_harness("codex", "humansout", "gpt-mini")[:4] == ["codex", "exec", "-m", "{model}"]
    assert "--model" not in watch.parse_harness("claude", "humansout")
    tools = claude[claude.index("--allowedTools") + 1].split(",")
    # never a bare Write (it reaches anywhere), never "./**" (it follows the agent's cd): the work folder, absolute
    assert tools == ["mcp__humansout", "Read(/{workdir}/**)", "Write(/{workdir}/**)", "Edit(/{workdir}/**)"]
    assert tools[2].format(workdir="/home/me/work/abc") == "Write(//home/me/work/abc/**)"
    clock = Clock()
    creator, me = Node(tmp_path / "c", clock=clock), Node(tmp_path / "me", clock=clock)
    runnable_mission(creator, tmp_path)
    share(creator, me)
    seen = tmp_path / "argv.json"
    harness = [sys.executable, "-c", "import json, sys; json.dump(sys.argv[1:], open(sys.argv[1], 'w'))",
               str(seen), "{model}", "{mcp_config}", "{prompt_file}"]
    cfg = watch.WatchConfig(harness=harness, roles=("build",), verify=False, model="claude-haiku-5-5",
                            cli=[sys.executable, "-m", "humansout"])
    assert [t["kind"] for t in watch.cycle(me, cfg)["turns"]] == ["build"]
    _, model, mcp_path, _ = json.loads(seen.read_text())
    assert model == "claude-haiku-5-5"
    server = json.loads(Path(mcp_path).read_text())["mcpServers"]["humansout"]
    assert server["args"][-3:] == ["--home", str(me.home), "mcp"] and server["env"]["HUMANSOUT_HOME"] == str(me.home)
    # the config really starts this node's MCP server
    req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n"
    out = subprocess.run([server["command"], *server["args"]], input=req, capture_output=True, text=True, timeout=30)
    tools = {t["name"] for t in json.loads(out.stdout.splitlines()[0])["result"]["tools"]}
    assert {"try_check", "repo_checkout", "challenge"} <= tools


def test_turns_are_private_unless_enabled_and_never_leak_paths(tmp_path):
    clock = Clock(int(time.time() * 1000) - 60_000)
    node = Node(tmp_path / "n", clock=clock)
    turns = node.home / "watch" / "turns"
    turns.mkdir(parents=True)
    name = "20261020-120000-build-4a5dbd12d6"
    (turns / f"{name}.prompt.txt").write_text("This turn: build mission 4a5dbd12")
    (turns / f"{name}.log").write_text('$ humansout ...\nexit: 0\n\n{"round": 1, "function": "repo_checkout", "result": "ok"}\n'
                                       '{"round": 2, "function": "submit", "result": "claim 1"}\n')
    claude = "20261020-121000-review-e931302eff"
    (turns / f"{claude}.prompt.txt").write_text("review it")
    (turns / f"{claude}.log").write_text('$ claude ...\nexit: 0\n\n' + json.dumps({
        "result": "No problem found.", "total_cost_usd": 0.0071, "num_turns": 7, "modelUsage": {"claude-haiku-5-5": {}},
        "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "cat ~/.ssh/id_rsa"}}]}))
    (turns / "notes.log").write_text("not a turn")
    (node.home / "watch.json").write_text(json.dumps({"last_cycle_ms": clock(), "recent": [
        {"kind": "build", "mission": "4a5dbd12", "started": clock(), "exit": 0, "seconds": 3.1, "log": str(turns / f"{name}.log")}]}))
    private = serve(node.store, "127.0.0.1", 0, background=True, agent_id=node.agent_id)
    shown = serve(node.store, "127.0.0.1", 0, background=True, agent_id=node.agent_id, show_turns=True)
    try:
        p, s = (f"http://127.0.0.1:{x.server_address[1]}" for x in (private, shown))
        overview = json.loads(get(p + "/api/overview")[2])
        assert overview["turns_enabled"] is False and str(tmp_path) not in json.dumps(overview)  # no local paths
        assert overview["watch"]["recent"][0]["turn"] == name
        with pytest.raises(urllib.error.HTTPError) as e:
            get(p + "/api/turns")
        assert e.value.code == 403
        listed = json.loads(get(s + "/api/turns")[2])
        assert [t["turn"] for t in listed] == [claude, name] and listed[1]["exit"] == "0"
        steps = json.loads(get(s + f"/api/turn/{name}")[2])
        assert [x["function"] for x in steps["steps"]] == ["repo_checkout", "submit"] and "build mission" in steps["prompt"]
        summary = json.loads(get(s + f"/api/turn/{claude}")[2])["summary"]
        assert summary["models"] == ["claude-haiku-5-5"] and summary["denied"] == [{"tool": "Bash"}]
        for bad in ("notes", "..%2Fwatch.json", "20261020-120000-build-4a5dbd12d6.prompt"):
            with pytest.raises(urllib.error.HTTPError) as e:
                get(s + "/api/turn/" + bad)
            assert e.value.code == 404
    finally:
        private.shutdown()
        shown.shutdown()


def test_dashboard_keeps_itself_in_sync(tmp_path):
    other = Node(tmp_path / "other")
    srv = serve(other.store, "127.0.0.1", 0, background=True)
    me = tmp_path / "me"
    Node(me).store.add_peer(f"http://127.0.0.1:{srv.server_address[1]}")
    import socket
    with socket.socket() as probe:  # a port nobody uses
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "-m", "humansout", "--home", str(me), "dashboard", "--port", str(port),
                             "--sync", "5"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        time.sleep(1.5)
        mid = other.create_mission("made elsewhere", "software/python", "c")["mission_id"]
        for _ in range(30):
            time.sleep(0.5)
            try:
                overview = json.loads(get(f"http://127.0.0.1:{port}/api/overview")[2])
            except OSError:
                continue
            if [m["id"] for m in overview["missions"]] == [mid]:
                break
        assert [m["id"] for m in overview["missions"]] == [mid] and overview["sync"]["new_events"] >= 0
        assert overview["sync"]["peers"] == {f"http://127.0.0.1:{srv.server_address[1]}": "ok"}
    finally:
        proc.terminate()
        proc.wait(10)
        srv.shutdown()


def test_build_prompts_offer_one_open_work_item(tmp_path):
    clock = Clock()
    creator, me = Node(tmp_path / "c", clock=clock), Node(tmp_path / "me", clock=clock)
    mid = runnable_mission(creator, tmp_path)
    clock.advance(1)
    items = [creator.add_work(mid, f"District {n}")["work_id"] for n in (1, 2, 3)]
    share(creator, me)
    world = me.world()
    ev = watch.find_work(world, me.agent_id, clock(), watch.WatchConfig(roles=("build",)), watch.load_state(me.home))[0]
    prompt = watch.render_prompt(me, world, ev, watch.WatchConfig(), tmp_path)
    assert ev.detail["suggested"] in items and f"Suggested for you, as the least crowded: {ev.detail['suggested']}" in prompt
    assert all(f"{w}: District" in prompt for w in items) and "nobody can stop anyone" in prompt
    from humansout.agent import LocalAgent
    me.store.set_config("agent_api", "http://127.0.0.1:9/v1")
    me.store.set_config("agent_model", "m")
    agent = LocalAgent(me, me.home / "work" / "x", log=lambda line: None)
    agent.call("claim_work", {"mission": mid, "work_id": items[1]})
    agent.call("write_file", {"name": "slugify.py", "content": "def slugify(s):\n    return s\n"})
    claim = agent.call("submit", {"mission": mid, "name": "slugify.py", "evidence": "district 2", "work_id": items[1]})
    world = me.world()
    assert world.items[claim["claim"]]["target"] == items[1] and me.agent_id in world.missions[mid]["work"][items[1]]["workers"]


def test_harness_tools_start_from_this_installation_even_off_path(tmp_path, monkeypatch):
    """A venv that is not activated: `humansout` is not on PATH, and the MCP server must still start."""
    import subprocess
    import sys
    from humansout import watch
    monkeypatch.setenv("PATH", "/nonexistent")
    cfg = watch.WatchConfig(harness=None)
    assert cfg.cli[:1] == [sys.executable]
    import os
    env = {"PATH": "/nonexistent", "HOME": str(tmp_path),  # however this interpreter finds the package, it still does
           **({"PYTHONPATH": os.environ["PYTHONPATH"]} if os.environ.get("PYTHONPATH") else {})}
    out = subprocess.run([*cfg.cli, "--home", str(tmp_path / "n"), "whoami"], capture_output=True, text=True,
                         env=env, timeout=60)
    assert out.returncode == 0 and "agent_id" in out.stdout


def test_the_swarm_map_shows_a_mission_from_its_first_joins(tmp_path):
    """The security-review mission had three agents working and no claim yet: it was missing from the map."""
    from humansout import dashboard
    creator = Node(tmp_path / "creator")
    mid = creator.create_mission("Security review", "security/x", "c")["mission_id"]
    wid = creator.add_work(mid, "area one")["work_id"]
    agents = []
    for name in ("a", "b"):
        n = Node(tmp_path / name)
        n.store.ingest(creator.agent_id, creator.store.load_feed(creator.agent_id))
        n.join(mid)
        creator.store.ingest(n.agent_id, n.store.load_feed(n.agent_id))
        agents.append(n)
    agents[0].claim_work(mid, wid)
    creator.store.ingest(agents[0].agent_id, agents[0].store.load_feed(agents[0].agent_id))
    graph = dashboard.home(creator.store)["graph"]
    assert [m[0] for m in graph["missions"]] == [mid]
    assert sorted((a, m, k) for a, m, k in graph["edges"]) == sorted((n.agent_id, mid, "work") for n in agents)



def test_agents_choose_freely_and_see_who_works_on_what(tmp_path):
    """Adam: no limits a malicious agent could use to block others (one agent leasing every item); agents choose
    any item, see who works on it, since when and how good they are, and the first correct claim earns."""
    clock = Clock()
    creator = Node(tmp_path / "c", clock=clock)
    mid = creator.create_mission("Review", "security/x", "c")["mission_id"]
    items = []
    for n in range(5):
        clock.advance(1)
        items.append(creator.add_work(mid, f"area {n}")["work_id"])
    hog, a, b = (Node(tmp_path / n, clock=clock) for n in ("hog", "a", "b"))
    for x in (hog, a, b):
        share(creator, x)
        clock.advance(1)
        x.join(mid)
    for wid in items:  # one agent takes every item for a week
        hog.claim_work(mid, wid, lease_seconds=7 * 86400)
    everyone = [creator, hog, a, b]
    for x in everyone:
        for y in everyone:
            if x is not y:
                share(x, y)
    clock.advance(60)
    picks = []
    for x in (a, b):  # nobody is blocked: both are offered work, and suggested different items
        ev = next(e for e in watch.find_work(x.world(), x.agent_id, clock(), watch.WatchConfig(roles=("build",)),
                                             watch.load_state(x.home)) if e.kind == "build")
        picks.append(ev.detail["suggested"])
        prompt = watch.render_prompt(x, x.world(), ev, watch.WatchConfig(), tmp_path)
        assert f"agent {hog.agent_id[:8]} (built 0, found 0, refuted 0) for 1 min" in prompt
    assert picks[0] != picks[1]
    a.claim_work(mid, items[0])  # leases never refuse: a works on the hog's item anyway, and both are listed
    share(a, b)
    work = b.world().missions[mid]["work"][items[0]]
    assert set(work["workers"]) == {hog.agent_id, a.agent_id}
    claim = a.claim_done(mid, "fixed area 0", work_id=items[0])["claim"]  # and its claim counts
    assert a.world().items[claim]["target"] == items[0]
    for x in (a, creator):
        share(x, b)
    clock.advance(1)
    b.review_lease(claim)
    share(b, hog)
    clock.advance(1)
    hog.review_lease(claim)  # reviewers are never refused either; the others see them in their prompt
    share(hog, creator)
    rev = next(e for e in watch.find_work(creator.world(), "f" * 64, clock(), watch.WatchConfig(roles=("break",)),
                                          watch.load_state(tmp_path / "nobody")) if e.kind == "review")
    text = watch.render_prompt(creator, creator.world(), rev, watch.WatchConfig(), tmp_path)
    assert f"agent {b.agent_id[:8]}" in text and f"agent {hog.agent_id[:8]}" in text


def test_a_build_that_delivered_nothing_holds_back_only_its_own_item(tmp_path):
    """Found in the security-review swarm: one unproductive turn on one item kept an agent off all eight others
    for an hour."""
    clock = Clock()
    creator, me = Node(tmp_path / "c", clock=clock), Node(tmp_path / "me", clock=clock)
    mid = creator.create_mission("Review", "security/x", "c")["mission_id"]
    for n in range(3):
        clock.advance(1)
        creator.add_work(mid, f"area {n}")
    share(creator, me)
    state = watch.load_state(me.home)
    cfg = watch.WatchConfig(roles=("build",))
    first = next(e for e in watch.find_work(me.world(), me.agent_id, clock(), cfg, state) if e.kind == "build")
    state["built"][f"{mid}:{first.detail['suggested']}"] = {"attempts": 1, "at": clock()}  # took it, claimed nothing
    clock.advance(60)
    second = next(e for e in watch.find_work(me.world(), me.agent_id, clock(), cfg, state) if e.kind == "build")
    assert second.detail["suggested"] != first.detail["suggested"]
