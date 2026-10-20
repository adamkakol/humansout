"""The built-in agent for local/open models, against a scripted OpenAI-compatible endpoint."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from humansout import HumansOutError, Node
from humansout.agent import LocalAgent
from humansout.cli import main
from humansout.core import cid_for


def call(name, args, i):
    return {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def fake_model(script):
    """Serve scripted assistant messages, one per request; record what the agent sent."""
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            msg = script[min(len(seen) - 1, len(script) - 1)]
            data = json.dumps({"choices": [{"message": msg}], "usage": {}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def test_local_agent_builds_and_claims_within_its_work_folder(tmp_path):
    creator, me = Node(tmp_path / "creator"), Node(tmp_path / "me", restrict_paths=True)
    mid = creator.create_mission("Answer", "data/datasets", "answer.txt contains 42")["mission_id"]
    for author in creator.store.feed_authors():
        me.store.ingest(author, creator.store.load_feed(author))
    cid = cid_for(b"42")
    script = [
        {"content": "", "tool_calls": [call("mission_state", {"mission": mid[:10]}, 1)]},
        {"content": "", "tool_calls": [call("join_mission", {"mission": mid}, 2),
                                       call("write_file", {"name": "answer.txt", "content": "42"}, 3),
                                       call("write_file", {"name": "../escape.txt", "content": "x"}, 4)]},
        {"content": "", "tool_calls": [call("publish_artifact", {"mission": mid, "name": "answer.txt"}, 5),
                                       call("claim_done", {"mission": mid, "evidence": "answer.txt says 42", "cids": [cid]}, 6)]},
        {"content": "", "tool_calls": [call("finish", {"summary": "claimed"}, 7)]},
    ]
    srv, seen = fake_model(script)
    try:
        me.store.set_config("agent_api", f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions")
        me.store.set_config("agent_model", "test-model")
        work = me.home / "work" / mid[:12]
        result = LocalAgent(me, work, log=lambda line: None).run("Build the mission.")
    finally:
        srv.shutdown()
    assert result["finished"] and result["calls"][-1] == "finish"
    assert not (me.home / "work" / "escape.txt").exists()
    escape_reply = next(m for m in seen[2]["messages"] if m.get("tool_call_id") == "c4")
    assert "inside your work folder" in escape_reply["content"]
    assert seen[0]["model"] == "test-model" and {t["function"]["name"] for t in seen[0]["tools"]} >= {"claim_done", "challenge"}
    claims = me.mission_state(mid)["claims"]
    assert claims[0]["author"] == me.agent_id and claims[0]["evidence"]["cids"] == [cid]


def test_agent_needs_a_configured_model(tmp_path):
    with pytest.raises(HumansOutError, match="configure the model first"):
        LocalAgent(Node(tmp_path / "n"), tmp_path / "n" / "work" / "x")


def test_agent_turn_refuses_work_folders_outside_the_node(tmp_path, capsys):
    prompt = tmp_path / "p.txt"
    prompt.write_text("hi")
    assert main(["--home", str(tmp_path / "n"), "agent-turn", str(prompt), "--workdir", str(tmp_path)]) == 1
    assert "agents may only use files under" in capsys.readouterr().err


def test_agent_is_told_to_wrap_up_near_its_time_budget(tmp_path, monkeypatch):
    me = Node(tmp_path / "me", restrict_paths=True)
    script = [{"content": "", "tool_calls": [call("list_files", {}, 1)]},
              {"content": "", "tool_calls": [call("finish", {"summary": "done"}, 2)]}]
    srv, seen = fake_model(script)
    clock = iter([0, 0, 800, 800, 800, 800, 800, 800])
    monkeypatch.setattr("humansout.agent.time.time", lambda: next(clock, 800))
    try:
        me.store.set_config("agent_api", f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions")
        me.store.set_config("agent_model", "m")
        me.store.set_config("agent_time_budget", 1000)
        LocalAgent(me, me.home / "work" / "x", log=lambda line: None).run("Do it.")
    finally:
        srv.shutdown()
    assert any("Time is almost up" in m.get("content", "") for m in seen[-1]["messages"] if m["role"] == "user")


def _engine_ready():
    import subprocess
    from humansout import sandbox
    engine = sandbox.find_engine()
    return bool(engine) and subprocess.run([engine, "image", "inspect", "python:3.12-slim"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _engine_ready(), reason="needs docker or podman with python:3.12-slim")
def test_run_check_sees_only_what_verifiers_see(tmp_path):
    me = Node(tmp_path / "me", restrict_paths=True)
    me.store.set_config("agent_api", "http://127.0.0.1:9/v1")
    me.store.set_config("agent_model", "m")
    check_test = me.home / "work" / "creator" / "test_env.py"
    check_test.parent.mkdir(parents=True)
    check_test.write_text("import os, unittest\n\nclass T(unittest.TestCase):\n    def test_env(self):\n"
                          "        print(sorted(os.listdir('/work')))\n        import answer\n")
    mid = me.create_mission("g", "software/python", "c", check={
        "command": ["python", "-m", "unittest", "discover", "-s", "/work"], "deliverable": "answer.py",
        "files": [str(check_test)], "image": "python:3.12-slim"})["mission_id"]
    agent = LocalAgent(me, me.home / "work" / "m", log=lambda line: None)
    agent.call("write_file", {"name": "answer.py", "content": "import helper\n"})
    agent.call("write_file", {"name": "helper.py", "content": "X = 1\n"})  # local only: verifiers never get it
    agent.call("write_file", {"name": "draft.py", "content": "import helper\n"})
    out = agent.call("run_check", {"mission": mid})
    assert out.startswith("fail") and "No module named 'helper'" in out and "helper.py" not in out.split("\n")[0]
    agent.call("write_file", {"name": "answer.py", "content": "X = 1\n"})
    assert agent.call("run_check", {"mission": mid}).startswith("pass")
    assert agent.call("run_check", {"mission": mid, "deliverable": "draft.py"}).startswith("fail")


def test_watch_deadline_shortens_the_turn_and_a_slow_model_cannot_overrun_it(tmp_path, monkeypatch):
    import time as _time
    me = Node(tmp_path / "me", restrict_paths=True)
    me.store.set_config("agent_api", "http://127.0.0.1:9/v1")
    me.store.set_config("agent_model", "m")
    monkeypatch.setenv("HUMANSOUT_TURN_DEADLINE", str(int(_time.time()) + 300))
    agent = LocalAgent(me, me.home / "work" / "x", log=lambda line: None)
    assert 230 <= agent.time_budget <= 240  # a minute before watch would kill the turn
    timeouts = []

    def slow(messages, timeout=600):
        timeouts.append(timeout)
        raise TimeoutError("timed out")
    monkeypatch.setattr(agent, "_chat", slow)
    clock = iter([0, 0, 0, 240, 240])
    monkeypatch.setattr("humansout.agent.time.time", lambda: next(clock, 240))
    result = agent.run("Do it.")
    assert timeouts and timeouts[0] <= 240 and not result["finished"]


def test_submit_publishes_and_claims_in_one_step(tmp_path):
    creator, me = Node(tmp_path / "creator"), Node(tmp_path / "me", restrict_paths=True)
    mid = creator.create_mission("Answer", "data/datasets", "answer.txt contains 42")["mission_id"]
    for author in creator.store.feed_authors():
        me.store.ingest(author, creator.store.load_feed(author))
    me.store.set_config("agent_api", "http://127.0.0.1:9/v1")
    me.store.set_config("agent_model", "m")
    agent = LocalAgent(me, me.home / "work" / "x", log=lambda line: None)
    agent.call("join_mission", {"mission": mid})
    agent.call("write_file", {"name": "answer.txt", "content": "42"})
    out = agent.call("submit", {"mission": mid, "name": "answer.txt", "evidence": "it says 42"})
    claim = me.mission_state(mid)["claims"][0]
    assert claim["id"] == out["claim"] and claim["evidence"]["cids"] == [cid_for(b"42")]


def test_file_tools_forgive_common_model_habits(tmp_path):
    me = Node(tmp_path / "me", restrict_paths=True)
    me.store.set_config("agent_api", "http://127.0.0.1:9/v1")
    me.store.set_config("agent_model", "m")
    agent = LocalAgent(me, me.home / "work" / "x", log=lambda line: None)
    agent.call("write_file", {"name": "./a.py", "content": "x = 1\ny = 2\n"})
    assert agent.call("read_file", {"name": "/work/a.py"}) == "x = 1\ny = 2\n"
    assert agent.call("edit_file", {"name": "a.py", "old": "y = 2", "new": "y = 3"}) == "edited a.py"
    assert "occurs 0 times" in agent.call("edit_file", {"name": "a.py", "old": "z", "new": "w"})
    with pytest.raises(HumansOutError, match="got '../a.py'"):
        agent.call("read_file", {"name": "../a.py"})


def test_a_cut_off_call_gets_a_useful_error(tmp_path):
    me = Node(tmp_path / "me", restrict_paths=True)
    script = [{"content": "", "tool_calls": [{"id": "c1", "type": "function",
                                              "function": {"name": "write_file", "arguments": json.dumps({"name": "t.py"})}},
                                             {"id": "c2", "type": "function",
                                              "function": {"name": "write_file", "arguments": '{"name": "t.py", "content": "abc'}}]},
              {"content": "", "tool_calls": [call("write_file", {"name": "t.py", "content": "a"}, 3),
                                             call("append_file", {"name": "t.py", "content": "b"}, 4),
                                             call("finish", {"summary": "ok"}, 5)]}]
    srv, seen = fake_model(script)
    try:
        me.store.set_config("agent_api", f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions")
        me.store.set_config("agent_model", "m")
        LocalAgent(me, me.home / "work" / "x", log=lambda line: None).run("Do it.")
    finally:
        srv.shutdown()
    replies = {m["tool_call_id"]: m["content"] for m in seen[1]["messages"] if m.get("tool_call_id")}
    assert "needs the argument 'content'" in replies["c1"] and "append_file" in replies["c1"]
    assert "not valid JSON" in replies["c2"]
    assert (me.home / "work" / "x" / "t.py").read_text() == "ab" and seen[0]["max_tokens"] == 8192
