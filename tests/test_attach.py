"""`humansout attach` and `humansout status`: one command to lend an agent's idle hours, one screen to check it."""

import plistlib
from pathlib import Path

import pytest

from humansout import Node, attach
from humansout.net import serve


def test_defaults_are_cheap_and_bounded(tmp_path):
    node = Node(tmp_path / "me")
    p = attach.profile(node, harness="claude")
    assert p["model"] == "claude-haiku-5-5" and p["active_hours"] == "23-7" and p["max_cost_per_day"] == 1.0
    argv = attach.watch_argv(node, p)
    joined = " ".join(argv)
    for part in ("watch --harness claude", "--model claude-haiku-5-5", "--active-hours 23-7", "--max-cost-per-day 1.0",
                 "--max-turns-per-day 12", f"--home {node.home}"):
        assert part in joined


def test_service_files(tmp_path):
    argv = ["/usr/bin/humansout", "--home", "/home/me/.humansout", "watch", "--harness", "claude"]
    unit = attach.unit_text(argv, "/usr/local/bin:/usr/bin")
    assert "ExecStart=/usr/bin/humansout --home /home/me/.humansout watch --harness claude" in unit
    assert "Environment=PATH=/usr/local/bin:/usr/bin" in unit and "WantedBy=default.target" in unit
    plist = plistlib.loads(attach.plist_bytes(argv, "/usr/bin", tmp_path / "log"))
    assert plist["ProgramArguments"] == argv and plist["Label"] == "si.humansout.watch"


@pytest.fixture
def healthy_machine(monkeypatch):
    """What doctor says on a machine that can take part (these tests are about attach, not this machine)."""
    from humansout import doctor
    monkeypatch.setattr(doctor, "run", lambda node: {"status": "ok", "checks": [
        {"check": "container engine", "status": "ok", "detail": "docker"}]})


def test_dry_run_changes_nothing(tmp_path, healthy_machine):
    node = Node(tmp_path / "me")
    out = attach.attach(node, hub="http://127.0.0.1:9", dry_run=True, harness="claude")
    assert out["status"].startswith("dry run") and node.store.peers() == []
    assert node.store.config().get("watch_profile") is None


def test_attach_connects_and_trusts_the_hub(tmp_path, healthy_machine):
    hub = Node(tmp_path / "hub")
    hub.store.set_config("accept_push", True)
    hub.create_mission("Write slugify", "software/python", "tests pass")
    srv = serve(hub.store, "127.0.0.1", 0, background=True, agent_id=hub.agent_id)
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        me = Node(tmp_path / "me")
        out = attach.attach(me, hub=url, harness="claude", hours="22-6", budget=0.5)
    finally:
        srv.shutdown()
    assert out["status"].startswith("ready") and url in me.store.peers()
    assert me.store.config()["trusted_creators"] == [hub.agent_id]
    assert me.store.config()["watch_profile"]["max_cost_per_day"] == 0.5
    assert len(me.missions()) == 1  # synced
    st = attach.status(me)
    assert st["profile"]["active_hours"] == "22-6" and st["today"] == {"turns": 0, "spent_usd": 0, "paused_until": None}
    assert st["watch_it"].endswith("/#/a/" + me.agent_id)


def test_remove_without_a_service(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    if attach.platform.system() not in ("Linux", "Darwin"):
        pytest.skip("services exist only on Linux and macOS")
    assert attach.remove()["removed"] is False
