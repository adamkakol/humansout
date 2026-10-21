import json
import os

import pytest

tomllib = pytest.importorskip("tomllib")  # Python 3.11+

from humansout import HumansOutError
from humansout import connect
from humansout.cli import main


def test_plans_point_at_this_installation(tmp_path):
    home = tmp_path / "node"
    claude = connect.plan("claude-code", home)
    assert claude["argv"][:3] == ["claude", "mcp", "add"]
    assert f"HUMANSOUT_HOME={home}" in claude["argv"]
    assert claude["argv"][-1] == "mcp" and os.path.isabs(claude["argv"][claude["argv"].index("--") + 1])
    codex = tomllib.loads(connect.plan("codex", home)["toml"])["mcp_servers"]["humansout"]
    assert codex["args"][-1] == "mcp" and codex["env"]["HUMANSOUT_HOME"] == str(home)
    generic = connect.plan("mcp-json", home)["mcpServers"]["humansout"]
    assert generic["env"]["HUMANSOUT_HOME"] == str(home)


def test_codex_apply_appends_once_with_backup(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('model = "o4"\n')
    out = connect.apply("codex", tmp_path / "node", codex_config=config)
    parsed = tomllib.loads(config.read_text())
    assert parsed["model"] == "o4" and "humansout" in parsed["mcp_servers"]
    assert out["backup"] and open(out["backup"]).read() == 'model = "o4"\n'
    with pytest.raises(HumansOutError, match="already has"):
        connect.apply("codex", tmp_path / "node", codex_config=config)


def test_claude_apply_runs_claude_mcp_add(tmp_path, monkeypatch):
    import subprocess
    ran = []
    monkeypatch.setattr(connect.shutil, "which", lambda name: "/usr/bin/" + name)  # no real claude needed
    monkeypatch.setattr(connect.subprocess, "run", lambda argv, **kw: ran.append(argv) or
                        subprocess.CompletedProcess(argv, 0, "added", ""))
    connect.apply("claude-code", tmp_path / "node")
    assert ran[0][0] == "claude"
    argv = ran[0][1:]
    assert argv[:3] == ["mcp", "add", "--env"] and argv[4:7] == ["--transport", "stdio", "humansout"]
    assert argv[-1] == "mcp"


def test_cli_connect_prints_by_default(tmp_path, capsys):
    assert main(["--home", str(tmp_path / "n"), "connect", "codex"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert "[mcp_servers.humansout]" in out["toml"] and "applied" not in out
