"""Production hardening: secrets, agent path limits, image allowlist, size caps, peer screening."""

import time

import pytest

from humansout import HumansOutError, Node, sandbox
from humansout.core import Identity, canonical
from humansout.mcp_server import _tools, handle
from humansout.net import make_handler
from humansout.store import MAX_EVENT_BYTES, unsigned_part


@pytest.fixture
def node(tmp_path):
    n = Node(tmp_path / "node")
    mid = n.create_mission("Collect idioms", "data/datasets", "100 idioms")["mission_id"]
    return n, mid, tmp_path


@pytest.mark.parametrize("rel", [".ssh/id_rsa", ".aws/credentials", "project/.env", "certs/server.pem", "id_ed25519"])
def test_secrets_are_never_published(node, rel):
    n, mid, tmp = node
    f = tmp / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("secret")
    with pytest.raises(HumansOutError, match="refusing to publish"):
        n.publish_artifact(mid, str(f))


def test_node_identity_cannot_be_published(node):
    n, mid, tmp = node
    with pytest.raises(HumansOutError, match="key.json usually holds credentials"):
        n.publish_artifact(mid, str(n.home / "key.json"))


def test_agents_publish_only_from_work_folders(node):
    n, mid, tmp = node
    agent = Node(n.home, restrict_paths=True)
    outside = tmp / "notes.txt"
    outside.write_text("private notes")
    with pytest.raises(HumansOutError, match="agents may only use files under"):
        agent.publish_artifact(mid, str(outside))
    inside = n.home / "work" / mid[:12] / "rows.csv"
    inside.parent.mkdir(parents=True)
    inside.write_text("id\n1\n")
    assert agent.publish_artifact(mid, str(inside))["cid"].startswith("sha256-")
    n.store.set_config("publish_roots", [str(tmp)])
    assert agent.publish_artifact(mid, str(outside))["cid"].startswith("sha256-")


def test_mcp_tools_are_restricted(node):
    n, mid, tmp = node
    outside = tmp / "notes.txt"
    outside.write_text("private")
    tools = _tools(n)
    n.restrict_paths = True  # what serve_stdio does

    def call(name, args):
        reply = handle(n, tools, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
        return reply["result"]

    assert call("publish_artifact", {"mission": mid, "path": str(outside)})["isError"]
    assert call("sandbox_run", {"workdir": str(tmp), "command": ["true"], "dry_run": True})["isError"]
    work = n.home / "work" / "w"
    work.mkdir(parents=True)
    bad_image = call("sandbox_run", {"workdir": str(work), "command": ["true"], "image": "evil/miner:latest", "dry_run": True})
    assert bad_image["isError"] and "does not run the image" in bad_image["content"][0]["text"]
    ok = call("sandbox_run", {"workdir": str(work), "command": ["true"], "dry_run": True})
    assert not ok["isError"]


def test_checks_only_use_allowed_images(node):
    n, mid, tmp = node
    test = tmp / "test_x.py"
    test.write_text("import unittest\n")
    hostile = n.create_mission("x", "software/python", "y", check={"command": ["python", "-m", "unittest"],
                                                                     "deliverable": "a.py", "files": [str(test)],
                                                                     "image": "attacker/registry:latest"})["mission_id"]
    f = n.home / "work" / "a.py"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("x = 1\n")
    cid = n.publish_artifact(hostile, str(f))["cid"]
    claim = n.claim_done(hostile, "done", cids=[cid])["claim"]
    with pytest.raises(HumansOutError, match="does not run the image 'attacker/registry:latest'"):
        n.verify(claim)


def test_sandbox_never_mounts_secret_folders(tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    with pytest.raises(HumansOutError, match="refusing to mount"):
        sandbox.build_command(str(ssh), ["cat", "id_rsa"])


def test_oversized_events_are_refused_and_not_ingested(node):
    n, mid, tmp = node
    with pytest.raises(ValueError, match="event larger than"):
        n.store.build_own(n.identity, "claim_done", {"evidence": {"text": "x" * (MAX_EVENT_BYTES + 1)}}, 1)
    peer = Identity.load_or_create(tmp / "peer-key.json")
    event = {"v": 1, "author": peer.public_hex, "seq": 0, "prev": None, "ts": int(time.time() * 1000),
             "type": "join", "body": {"pad": "y" * (MAX_EVENT_BYTES + 10)}}
    event["sig"] = peer.sign(canonical(unsigned_part(event)))
    assert n.store.ingest(peer.public_hex, [event]) == 0


def test_learned_peers_must_be_public(tmp_path):
    store = Node(tmp_path / "n").store
    assert store.add_peer("http://192.168.1.25:7400") is True           # added on purpose: allowed
    assert store.add_peer("http://169.254.169.254", learned=True) is False  # learned from a peer: refused
    assert store.add_peer("http://127.0.0.1:9999", learned=True) is False
    store.set_config("allow_private_peers", True)
    assert store.add_peer("http://10.0.0.7:7400", learned=True) is True


def test_feed_cache_sees_new_events(node):
    n, mid, tmp = node
    first = n.store.load_feed(n.agent_id)
    assert n.store.load_feed(n.agent_id) == first
    n.add_work(mid, "item")
    assert len(n.store.load_feed(n.agent_id)) == len(first) + 1


def test_http_server_has_a_socket_timeout(tmp_path):
    handler = make_handler(Node(tmp_path / "n").store)
    assert handler.timeout == 15


def test_relative_home_is_made_absolute(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    n = Node("rel-node")
    assert n.home.is_absolute() and n.home == tmp_path / "rel-node"


def test_doctor_reports_and_suggests_fixes(tmp_path):
    from humansout import doctor
    report = doctor.run(Node(tmp_path / "n"))
    names = {r["check"]: r for r in report["checks"]}
    assert names["identity key"]["status"] == "ok"
    assert names["peers"]["status"] == "warn" and "playground" in names["peers"]["fix"]
    assert names["trust policy"]["status"] == "warn"
    assert report["status"] in ("warn", "fail")


def test_doctor_exit_code_follows_failures(tmp_path, capsys):
    from humansout.cli import main
    home = tmp_path / "n"
    Node(home)
    assert main(["--home", str(home), "config", "set", "allowed_check_images", "[]"]) == 0
    assert main(["--home", str(home), "doctor"]) == 1  # the default check image is no longer allowed
    assert main(["--home", str(home), "config", "set", "allowed_check_images", '["python:3.12-slim"]']) == 0
    report_code = main(["--home", str(home), "doctor"])
    capsys.readouterr()
    assert report_code == (1 if any(r["status"] == "fail" for r in __import__("humansout.doctor", fromlist=["run"]).run(Node(home))["checks"]) else 0)


def test_every_request_names_itself(tmp_path):
    """Public hubs sit behind CDNs that ban urllib's default User-Agent (Cloudflare 1010)."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from humansout import doctor
    seen = []

    class Picky(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append(self.headers.get("User-Agent", ""))
            ok = seen[-1].startswith("humansout/")
            body = (b"[]" if self.path.endswith("/peers") else b"{}") if ok else b"error code: 1010"
            self.send_response(200 if ok else 403)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Picky)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        node = Node(tmp_path / "n")
        url = f"http://127.0.0.1:{srv.server_address[1]}"
        node.store.add_peer(url)
        node.sync()
        rows = {r["check"]: r for r in doctor.run(node)["checks"]}
        assert rows[f"peer {url}"]["status"] == "ok"
        assert seen and all(ua.startswith("humansout/0.") for ua in seen)
    finally:
        srv.shutdown()


def test_a_broken_peer_does_not_stop_the_sync(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from humansout.net import serve

    class Broken(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b'["not", "a", "dict"]' if self.path == "/feeds" else b'{"x": 1}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    bad = ThreadingHTTPServer(("127.0.0.1", 0), Broken)
    threading.Thread(target=bad.serve_forever, daemon=True).start()
    creator = Node(tmp_path / "creator")
    creator.create_mission("g", "software/python", "c")
    good = serve(creator.store, "127.0.0.1", 0, background=True)
    try:
        me = Node(tmp_path / "me")
        out = me.sync([f"http://127.0.0.1:{bad.server_address[1]}", f"http://127.0.0.1:{good.server_address[1]}"])
        assert [p["ok"] for p in out["peers"].values()] == [False, True] and out["new_events"] == 1
    finally:
        bad.shutdown()
        good.shutdown()


def test_an_unusable_container_engine_never_counts_as_a_failing_check(tmp_path, monkeypatch):
    """Found on an ARM test machine: docker installed but the user cannot reach its socket, so every run exits 1,
    which would have been recorded as a failing check (and every counterexample as reproduced)."""
    import os
    from humansout import doctor, sandbox
    from humansout.verify import run_check
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "docker").write_text("#!/bin/sh\necho 'permission denied while trying to connect to the docker API' >&2\nexit 1\n")
    (fake / "docker").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}/usr/bin{os.pathsep}/bin")
    monkeypatch.setattr(sandbox, "_PROBLEMS", {})
    monkeypatch.setattr(sandbox.shutil, "which", lambda name: str(fake / name) if name == "docker" else None)
    node = Node(tmp_path / "n")
    rows = {r["check"]: r for r in doctor.run(node)["checks"]}
    assert rows["container engine"]["status"] == "fail" and "usermod -aG docker" in rows["container engine"]["fix"]
    blob = node.store.put_blob(b"x = 1\n")
    check = {"image": "python:3.12-slim", "command": ["python", "-c", "1"], "deliverable": "a.py", "files": []}
    with pytest.raises(HumansOutError, match="cannot run containers for this user"):
        run_check(node.store.get_blob, check, blob)
    assert node.store.verifications() == {}  # nothing was recorded
