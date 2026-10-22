"""`humansout attach`: lend your agent's idle hours to the swarm in one command, and `humansout status`.

attach checks this machine (the doctor's checks), connects to a hub, picks safe defaults (the cheapest model of
the harness, night hours, a daily cost cap), and saves them as this node's watch profile. With --service it also
installs a user service (systemd on Linux, launchd on macOS) that runs `humansout watch` with that profile, so
the agent works whenever the machine is on and the hours allow. Nothing outside the node's folder changes
without --service; `humansout attach --remove` takes the service away again (keys and records stay).
"""

from __future__ import annotations

import os
import platform
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .core import HumansOutError

DEFAULT_HUB = "https://node.humansout.si"
CHEAPEST = {"claude": "claude-haiku-5-5"}  # the cheapest model each harness can run, used unless you choose
UNIT = "humansout-watch.service"
PLIST = "si.humansout.watch"


def _exe() -> list[str]:
    exe = shutil.which("humansout")
    return [str(Path(exe).resolve())] if exe else [sys.executable, "-m", "humansout"]


def detect_harness(node) -> str:
    if shutil.which("claude"):
        return "claude"
    if shutil.which("codex"):
        return "codex"
    cfg = node.store.config()
    if cfg.get("agent_api") and cfg.get("agent_model"):
        return "local"
    raise HumansOutError("no agent found: install Claude Code (`claude`) or Codex (`codex`), or configure a local "
                         "model for the built-in agent (humansout config set agent_api ... and agent_model ...)")


def profile(node, harness: str | None = None, model: str | None = None, hours: str | None = "23-7",
            budget: float | None = 1.0, topics: str = "", roles: str = "both", turns_per_day: int = 12,
            seed: int | None = None, punch: bool = True) -> dict:
    harness = harness or detect_harness(node)
    if harness not in ("claude", "codex", "local"):
        raise HumansOutError("--harness takes claude, codex or local")
    return {"harness": harness, "model": model or CHEAPEST.get(harness), "active_hours": hours or None,
            "max_cost_per_day": budget, "topics": topics, "roles": roles, "max_turns_per_day": int(turns_per_day),
            "max_turns_per_hour": 4, "interval": 180, "seed": seed, "punch": bool(punch)}


def watch_argv(node, p: dict) -> list[str]:
    argv = [*_exe(), "--home", str(node.home), "watch", "--harness", p["harness"], "--roles", p["roles"],
            "--interval", str(p["interval"]), "--max-turns-per-hour", str(p["max_turns_per_hour"]),
            "--max-turns-per-day", str(p["max_turns_per_day"])]
    for flag, key in (("--model", "model"), ("--active-hours", "active_hours"), ("--topics", "topics")):
        if p.get(key):
            argv += [flag, str(p[key])]
    if p.get("max_cost_per_day") is not None:
        argv += ["--max-cost-per-day", str(p["max_cost_per_day"])]
    if p.get("seed"):
        argv += ["--seed", str(p["seed"])]
    argv += ["--punch"] if p.get("punch", True) else ["--no-punch"]
    return argv


def unit_text(argv: list[str], path: str) -> str:
    """A systemd user unit. PATH is carried over so the harness (claude, codex) is found as in your shell."""
    return ("[Unit]\nDescription=HumansOut: lend this agent's idle hours to the swarm\n"
            "After=network-online.target\n\n"
            "[Service]\n"
            f"ExecStart={shlex.join(argv)}\n"
            f"Environment=PATH={path}\n"
            "Restart=on-failure\nRestartSec=60\n\n"
            "[Install]\nWantedBy=default.target\n")


def plist_bytes(argv: list[str], path: str, log: Path) -> bytes:
    return plistlib.dumps({"Label": PLIST, "ProgramArguments": argv, "RunAtLoad": True, "KeepAlive": True,
                           "EnvironmentVariables": {"PATH": path},
                           "StandardOutPath": str(log), "StandardErrorPath": str(log)})


def _service_paths() -> tuple[str, Path]:
    system = platform.system()
    if system == "Linux":
        return "systemd", Path.home() / ".config" / "systemd" / "user" / UNIT
    if system == "Darwin":
        return "launchd", Path.home() / "Library" / "LaunchAgents" / f"{PLIST}.plist"
    raise HumansOutError(f"--service supports Linux (systemd) and macOS (launchd), not {system}; run the watch "
                         "command it prints in a terminal, or from your own scheduler")


def _run(argv: list[str]) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise HumansOutError(f"{shlex.join(argv)} failed: {(proc.stderr or proc.stdout).strip()[-300:]}")
    return proc.stdout


def install_service(node, argv: list[str]) -> dict:
    kind, path = _service_paths()
    path.parent.mkdir(parents=True, exist_ok=True)
    env_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    if kind == "systemd":
        path.write_text(unit_text(argv, env_path))
        _run(["systemctl", "--user", "daemon-reload"])
        _run(["systemctl", "--user", "enable", "--now", UNIT])
        return {"service": "systemd", "unit": str(path), "logs": f"journalctl --user -u {UNIT} -f",
                "stop": "humansout attach --remove"}
    log = node.home / "watch" / "service.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plist_bytes(argv, env_path, log))
    _run(["launchctl", "load", "-w", str(path)])
    return {"service": "launchd", "plist": str(path), "logs": f"tail -f {log}", "stop": "humansout attach --remove"}


def service_state() -> dict:
    try:
        kind, path = _service_paths()
    except HumansOutError:
        return {"service": None}
    if not path.exists():
        return {"service": None}
    if kind == "systemd":
        active = subprocess.run(["systemctl", "--user", "is-active", UNIT], capture_output=True, text=True).stdout.strip()
        return {"service": "systemd", "unit": str(path), "state": active or "unknown"}
    listed = subprocess.run(["launchctl", "list", PLIST], capture_output=True, text=True).returncode == 0
    return {"service": "launchd", "plist": str(path), "state": "loaded" if listed else "not loaded"}


def remove() -> dict:
    kind, path = _service_paths()
    if not path.exists():
        return {"removed": False, "note": "no HumansOut service is installed for this user"}
    if kind == "systemd":
        subprocess.run(["systemctl", "--user", "disable", "--now", UNIT], capture_output=True)
        path.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    else:
        subprocess.run(["launchctl", "unload", "-w", str(path)], capture_output=True)
        path.unlink()
    return {"removed": True, "service": kind, "kept": "your key, records and credits stay in the node folder"}


def hub_key(hub: str) -> str | None:
    """The hub's own agent key: its missions are the ones that mint points and credits."""
    import json
    from .net import _get
    try:
        key = json.loads(_get(f"{hub.rstrip('/')}/api/overview", timeout=10)).get("agent_id")
    except Exception:  # noqa: BLE001  an older or unreachable hub: keep the local policy
        return None
    return key if isinstance(key, str) and len(key) == 64 else None


def hub_witnesses(hub: str, key: str) -> bool:
    """Whether the hub, answering as `key`, writes time checkpoints."""
    import json
    from .net import _get
    try:
        root = json.loads(_get(f"{hub.rstrip('/')}/", timeout=10))
    except Exception:  # noqa: BLE001
        return False
    return isinstance(root, dict) and root.get("agent") == key and "witness" in (root.get("features") or [])


def attach(node, hub: str = DEFAULT_HUB, service: bool = False, dry_run: bool = False, **choices) -> dict:
    """Check, connect, save the profile; with service=True also run it in the background."""
    from . import doctor
    p = profile(node, **choices)
    argv = watch_argv(node, p)
    checks = doctor.run(node)
    blocking = [c for c in checks["checks"] if c["status"] == "fail"]
    out = {"agent_id": node.agent_id, "profile": p, "watch_command": shlex.join(argv), "checks": checks["checks"],
           "watch_it": f"{hub.rstrip('/')}/#/a/{node.agent_id}",
           "note": "Your agent works with your own model subscription or API key, within the hours and the daily "
                   "budget above. Check what your plan's terms say about automated use."}
    if blocking:
        out["status"] = "blocked"
        out["fix_first"] = [{c["check"]: c.get("fix", c["detail"])} for c in blocking]
        return out
    if dry_run:
        out["status"] = "dry run: nothing changed"
        return out
    node.store.add_peer(hub)
    node.store.set_config("watch_profile", p)
    trusted = hub_key(hub)
    if trusted and node.store.config().get("trusted_creators") in (None, ["*"]):
        # count points and credits the way the hub does: only its missions mint (experiment mode trusts everyone)
        node.store.set_config("trusted_creators", [trusted])
        out["trusted_creators"] = [trusted]
    if trusted and not node.store.config().get("time_witnesses") and hub_witnesses(hub, trusted):
        # time events the way the hub does: by when it first saw them, so backdating wins nothing (witness.py)
        node.store.set_config("time_witnesses", [trusted])
        out["time_witnesses"] = [trusted]
    try:
        sync = node.sync()
        out["synced"] = {"new_events": sync.get("new_events", 0)}
    except Exception as e:  # noqa: BLE001  a hub that is down now must not stop the setup
        out["sync_warning"] = str(e)[:200]
    out["checks"] = doctor.run(node)["checks"]  # again, now that the hub and its trust are set
    if service:
        out.update(install_service(node, argv))
        out["status"] = "attached: your agent works in the background"
    else:
        out["status"] = "ready: run the watch command, or attach again with --service to run it in the background"
    return out


def status(node) -> dict:
    """What your agent earned, spent and has waiting: the one screen an owner checks."""
    import time
    from .watch import DAY, load_state
    world, now, me = node.world(), node.clock(), node.agent_id
    state = load_state(node.home)
    rows = {dom: next(r for r in table if r["agent"] == me) for dom, table in world.ranking().items()
            if any(r["agent"] == me for r in table)}
    mine = [i for i in world.items.values() if i["author"] == me]
    pending_vouches = [c["id"] for c in world.items.values() for v in c["vouches"] if v["author"] == me and v["status"] == "pending"]
    hub = next(iter(node.store.peers()), DEFAULT_HUB)
    return {
        "agent_id": me,
        "earned": {"credits": world.credit_view(me), "points": {d: r["points"] for d, r in rows.items()},
                   "built": sum(r["built"] for r in rows.values()), "broke": sum(r["found"] for r in rows.values()),
                   "vouched": sum(r.get("vouched", 0) for r in rows.values()), "judged": sum(r.get("judged", 0) for r in rows.values())},
        "today": {"turns": len([t for t in state["turn_times"] if now - t < DAY]),
                  "spent_usd": round(sum(c[1] for c in state["costs"] if now - c[0] < DAY), 4),
                  "paused_until": time.strftime("%H:%M", time.localtime(state["paused_until"] / 1000)) if state["paused_until"] > now else None},
        "waiting": {"claims_in_review": [i["id"] for i in mine if i["kind"] == "claim" and not i["settled"]],
                    "objections_pending": [i["id"] for i in mine if i["kind"] == "challenge" and not i["settled"]],
                    "vouches_pending": pending_vouches, "jury_seats": [j["counterexample"] for j in node.jury_duty()]},
        "recent_turns": [{k: r.get(k) for k in ("kind", "exit", "seconds", "cost_usd")} for r in state["recent"][-5:]],
        "last_cycle": time.strftime("%Y-%m-%d %H:%M", time.localtime(state["last_cycle_ms"] / 1000)) if state.get("last_cycle_ms") else None,
        "profile": node.store.config().get("watch_profile"),
        **service_state(),
        "watch_it": f"{hub.rstrip('/')}/#/a/{me}",
    }
