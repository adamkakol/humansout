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
    assert [(e.kind, e.item or e.mission) for e in bob_events] == [("review", claim), ("build", mid)]
    assert watch.find_work(alice.world(), alice.agent_id, clock(), cfg, watch.load_state(alice.home)) == []

    clock.advance(1)
    ch = bob.challenge(claim, "fails", counterexample=[], preflight=False)
    share(creator, alice, bob)
    alice_events = watch.find_work(alice.world(), alice.agent_id, clock(), cfg, watch.load_state(alice.home))
    assert [(e.kind, e.detail["objections"]) for e in alice_events] == [("respond", [ch["challenge"]])]


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
        return watch.cycle(nodes[who], cfg(who, roles))

    try:
        mid = runnable_mission(nodes["creator"], tmp_path / "files", window=60, dispute=6)
        nodes["builder"].sync()
        assert [t["kind"] for t in run("builder", ("build",))["turns"]] == ["build"]
        assert [t["kind"] for t in run("breaker", ("break",))["turns"]] == ["review"]
        respond = run("builder", ("build",))
        assert [v["counterexample"] for v in respond["verified"]] == ["reproduced"]
        assert [t["kind"] for t in respond["turns"]] == ["respond"]  # it fixed the code and claimed v2
        time.sleep(7)  # the first claim's dispute window ends
        run("creator", ())
        assert run("breaker", ("break",))["turns"][0]["kind"] == "review"  # tries the same test on v2
        assert [t["kind"] for t in run("breaker2", ("break",))["turns"]] == ["review"]
        builder = run("builder", ("build",))
        assert [v["counterexample"] for v in builder["verified"]] == ["reproduced"]  # bob's v2 objection was stopped by preflight
        assert [t["kind"] for t in builder["turns"]] == ["respond"]  # it disputes the unicode test
        creator = run("creator", ())
        assert len(creator["notifications"]) == 1 and "needs your ruling" in notes.read_text()
        assert json.loads(get(urls["creator"] + "/api/overview")[2])["missions"][0]["needs_ruling"] == 1
        disputed = creator["notifications"][0]["message"].split("humansout rule ")[1].split()[0]
        subprocess.run([sys.executable, "-m", "humansout", "--home", str(nodes["creator"].home), "rule", disputed,
                        "--decision", "rejected", "--reason", "ASCII slugs were asked for"], check=True, capture_output=True)
        assert run("creator", ())["notifications"] == []  # notified once only
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
        overview = json.loads(get(urls["creator"] + "/api/overview")[2])
        assert overview["missions"][0]["status"] == "done" and overview["missions"][0]["needs_ruling"] == 0
        assert overview["watch"]["notifications"]
    finally:
        for srv in servers.values():
            srv.shutdown()


def test_model_flags_and_a_per_node_mcp_config(tmp_path):
    claude = watch.parse_harness("claude", "humansout", "claude-haiku-5-5")
    assert claude[:3] == ["claude", "--model", "{model}"] and "--strict-mcp-config" in claude
    assert watch.parse_harness("codex", "humansout", "gpt-mini")[:4] == ["codex", "exec", "-m", "{model}"]
    assert "--model" not in watch.parse_harness("claude", "humansout")
    tools = claude[claude.index("--allowedTools") + 1].split(",")
    assert tools == ["mcp__humansout", "Read(./**)", "Write(./**)", "Edit(./**)"]  # never a bare Write: it reaches anywhere
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
