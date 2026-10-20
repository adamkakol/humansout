"""Run mission code in an isolated container (security profile S1).

The harness keeps doing the thinking; whenever mission content has to be
executed (tests, scripts, builds), it goes through here instead of the host.

S1 in v0: rootless-friendly container, no network, read-only root filesystem,
all capabilities dropped, resource limits, only the work directory mounted.
S2/S3 (gVisor, micro-VMs, full VMs) are not implemented in v0.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .core import HumansOutError


_PROBLEMS: dict[str, str | None] = {}  # engine -> None if this user can run containers with it, else why not


def engine_problem(engine: str) -> str | None:
    """Why `engine` cannot run containers for this user, or None. Checked once per process.
    An installed but unusable engine fails every run with exit code 1, which must never count as a failing check."""
    if engine not in _PROBLEMS:
        try:
            proc = subprocess.run([engine, "version"], capture_output=True, text=True, timeout=30)
            _PROBLEMS[engine] = None if proc.returncode == 0 else ((proc.stderr or proc.stdout).strip()[-300:]
                                                                   or f"`{engine} version` failed")
        except (OSError, subprocess.TimeoutExpired) as e:
            _PROBLEMS[engine] = str(e)
    return _PROBLEMS[engine]


def find_engine() -> str | None:
    """An installed engine, preferring one this user can actually use."""
    installed = [e for e in ("podman", "docker") if shutil.which(e)]
    usable = [e for e in installed if engine_problem(e) is None]
    return (usable or installed or [None])[0]


def engine_fix(engine: str) -> str:
    if engine == "docker":
        return ("let your user use Docker (sudo usermod -aG docker $USER, then log in again) or install rootless "
                "podman; see `humansout doctor`")
    return "check `podman info`; see `humansout doctor`"


def build_command(workdir: str, command: list[str], image: str = "python:3.12-slim", engine: str | None = None,
                  memory: str = "2g", cpus: str = "2", pids: int = 256, network: bool = False) -> list[str]:
    work = Path(workdir).expanduser().resolve()
    if not work.is_dir():
        raise HumansOutError(f"work directory does not exist: {work}")
    home = Path.home().resolve()
    if work == home or work in home.parents or work == Path("/"):
        raise HumansOutError("refusing to mount your home directory or / into the sandbox; use a dedicated work directory")
    from .node import secret_reason
    reason = secret_reason(work)
    if reason:
        raise HumansOutError(f"refusing to mount {work} into the sandbox: {reason}")
    if not command:
        raise HumansOutError("no command given")
    engine = engine or find_engine() or "docker"
    cmd = [engine, "run", "--rm", "--read-only",
           "--tmpfs", "/tmp:rw,size=512m",
           "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
           "--pids-limit", str(pids), "--memory", memory, "--cpus", cpus,
           "--network", "bridge" if network else "none",
           "-v", f"{work}:/work:rw", "-w", "/work",
           "-e", "HOME=/tmp"]
    if hasattr(os, "getuid"):
        cmd += ["--user", f"{os.getuid()}:{os.getgid()}"]
    return cmd + [image] + list(command)


def run(workdir: str, command: list[str], image: str = "python:3.12-slim", timeout: int = 900,
        dry_run: bool = False, network: bool = False) -> dict:
    cmd = build_command(workdir, command, image=image, network=network)
    if dry_run:
        return {"dry_run": True, "command": cmd}
    engine = find_engine()
    if engine is None:
        raise HumansOutError("no container engine found (install podman or docker); refusing to run mission code on the host")
    problem = engine_problem(engine)
    if problem:  # never let an engine error pass for a failing check
        raise HumansOutError(f"{engine} is installed but cannot run containers for this user ({problem[:200]}): "
                             f"{engine_fix(engine)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "timeout": True, "stdout": "", "stderr": f"killed after {timeout}s"}
    return {"exit_code": proc.returncode, "stdout": proc.stdout[-20000:], "stderr": proc.stderr[-20000:]}
