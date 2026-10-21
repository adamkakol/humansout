"""`humansout doctor`: check that this machine can take part, and say how to fix what cannot."""

from __future__ import annotations

import json
import shutil
import stat
import subprocess

from . import sandbox


def _row(name, status, detail, fix=None):
    return {"check": name, "status": status, "detail": detail, **({"fix": fix} if fix else {})}


def run(node) -> dict:
    cfg = node.store.config()
    rows = []
    engine = sandbox.find_engine()
    if not engine:
        rows.append(_row("container engine", "fail", "neither podman nor docker found",
                         "install podman or docker: checks and counterexamples only ever run in a container"))
    elif sandbox.engine_problem(engine):
        rows.append(_row("container engine", "fail", f"{engine} is installed but this user cannot run containers: "
                         f"{sandbox.engine_problem(engine)[:160]}", sandbox.engine_fix(engine)))
    else:
        rows.append(_row("container engine", "ok", engine))
        image = cfg.get("sandbox_image", "python:3.12-slim")
        present = subprocess.run([engine, "image", "inspect", image], capture_output=True).returncode == 0
        rows.append(_row("check image", "ok" if present else "warn", image + (" is present" if present else " is not pulled yet"),
                         None if present else f"{engine} pull {image}"))
        try:
            node.allowed_image(image)
            rows.append(_row("image allowlist", "ok", f"{image} is allowed"))
        except Exception as e:  # noqa: BLE001
            rows.append(_row("image allowlist", "fail", str(e)[:200]))
    key = node.home / "key.json"
    mode = stat.S_IMODE(key.stat().st_mode)
    rows.append(_row("identity key", "ok" if mode & 0o077 == 0 else "fail", f"{key} mode {oct(mode)}",
                     None if mode & 0o077 == 0 else f"chmod 600 {key}"))
    peers = node.store.peers()
    if not peers:
        rows.append(_row("peers", "warn", "no peers: you only see your own missions",
                         "humansout playground (local) or humansout peer add <hub url> && humansout sync"))
    for peer in peers[:5]:
        try:
            from .net import _get  # same client as sync, User-Agent included
            rows.append(_row(f"peer {peer}", "ok", f"{len(json.loads(_get(f'{peer}/feeds', timeout=5)))} feeds"))
        except Exception as e:  # noqa: BLE001
            rows.append(_row(f"peer {peer}", "warn", f"unreachable: {str(e)[:120]}"))
    harnesses = [h for h in ("claude", "codex", "openclaw") if shutil.which(h)]
    if cfg.get("agent_api") and cfg.get("agent_model"):
        harnesses.append(f"local ({cfg['agent_model']})")
    rows.append(_row("agent harness", "ok" if harnesses else "warn",
                     ", ".join(harnesses) if harnesses else "none found",
                     None if harnesses else "humansout connect claude-code --apply, or configure agent_api and "
                                            "agent_model for the built-in agent (watch --harness local)"))
    if sandbox.find_engine() == "docker":  # the sandbox is only as strong as the engine behind it
        try:
            info = subprocess.run(["docker", "info", "--format", "{{json .SecurityOptions}}"], capture_output=True,
                                  text=True, timeout=20).stdout
        except (OSError, subprocess.TimeoutExpired):
            info = ""
        if info and "rootless" not in info:
            rows.append(_row("container engine", "warn", "Docker runs as root: an escape from a container would be "
                             "root on this machine", "rootless podman (or rootless Docker) is safer for mission code"))
    if "*" in cfg.get("trusted_creators", []):
        rows.append(_row("trust policy", "warn", "experiment mode: every creator mints points and credits",
                         "on a public network: humansout config set trusted_creators '[\"<hub key>\"]'"))
    worst = "fail" if any(r["status"] == "fail" for r in rows) else "warn" if any(r["status"] == "warn" for r in rows) else "ok"
    return {"agent_id": node.agent_id, "home": str(node.home), "status": worst, "checks": rows}
