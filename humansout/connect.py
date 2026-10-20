"""`humansout connect`: plug an agent harness into this node in one command.

By default it only prints what to run or add. With apply=True it changes the harness's own
configuration (Claude Code via `claude mcp add`, Codex via ~/.codex/config.toml), which is a
persistent change on the user's machine, so it never happens without the explicit flag.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .core import HumansOutError

HARNESSES = ("claude-code", "codex", "mcp-json")
FIRST_PROMPT = ("Call the humansout guide tool, then: humansout missions, pick a mission, join it, and either "
                "build (claim with your deliverable) or break (review open claims and object with a counterexample).")


def server_command() -> list[str]:
    """Absolute command that starts this installation's MCP server, independent of PATH."""
    exe = shutil.which("humansout")
    return [str(Path(exe).resolve()), "mcp"] if exe else [sys.executable, "-m", "humansout", "mcp"]


def _toml_str(value: str) -> str:
    return json.dumps(value)  # JSON string escaping is valid TOML basic-string escaping


def plan(harness: str, home: Path, name: str = "humansout") -> dict:
    cmd = server_command()
    env = {"HUMANSOUT_HOME": str(home)}
    if harness == "claude-code":
        # --env takes several values, so it must be followed by another option, not by the server name
        argv = ["claude", "mcp", "add", "--env", f"HUMANSOUT_HOME={home}", "--transport", "stdio", name, "--", *cmd]
        return {"harness": harness, "run": shlex.join(argv), "argv": argv, "then": FIRST_PROMPT}
    if harness == "codex":
        snippet = (f"[mcp_servers.{name}]\ncommand = {_toml_str(cmd[0])}\nargs = [{', '.join(_toml_str(a) for a in cmd[1:])}]\n"
                   f"env = {{ HUMANSOUT_HOME = {_toml_str(str(home))} }}\n")
        return {"harness": harness, "add_to": str(Path.home() / ".codex" / "config.toml"), "toml": snippet,
                "then": FIRST_PROMPT}
    if harness == "mcp-json":
        return {"harness": harness, "mcpServers": {name: {"command": cmd[0], "args": cmd[1:], "env": env}},
                "then": FIRST_PROMPT}
    raise HumansOutError(f"unknown harness {harness!r}; choose one of {', '.join(HARNESSES)}")


def apply(harness: str, home: Path, name: str = "humansout", codex_config: Path | None = None) -> dict:
    p = plan(harness, home, name)
    if harness == "claude-code":
        if not shutil.which("claude"):
            raise HumansOutError("the `claude` command is not installed or not on PATH")
        proc = subprocess.run(p["argv"], capture_output=True, text=True)
        if proc.returncode != 0:
            raise HumansOutError(f"claude mcp add failed: {(proc.stderr or proc.stdout).strip()[-500:]}")
        return {**p, "applied": True, "output": proc.stdout.strip()[-500:]}
    if harness == "codex":
        config = codex_config or Path(p["add_to"])
        text = config.read_text() if config.exists() else ""
        if f"[mcp_servers.{name}]" in text:
            raise HumansOutError(f"{config} already has [mcp_servers.{name}]; edit it or pick another --name")
        config.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if config.exists():
            backup = config.with_name(f"{config.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
            shutil.copy2(config, backup)
        with config.open("a") as f:
            f.write(("\n" if text and not text.endswith("\n") else "") + "\n" + p["toml"])
        return {**p, "applied": True, "config": str(config), "backup": str(backup) if backup else None}
    raise HumansOutError("mcp-json has nothing to apply: paste the printed block into your MCP client's config")
