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
import tempfile
import time
import uuid
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


MAX_WORK_BYTES = 2 << 30      # what mission code may write into /work before it is stopped
MAX_OUTPUT_BYTES = 64 << 20   # what it may print
TAIL = 20000                  # characters of output kept


def build_command(workdir: str, command: list[str], image: str = "python:3.12-slim", engine: str | None = None,
                  memory: str = "2g", cpus: str = "2", pids: int = 256, network: bool = False,
                  name: str | None = None) -> list[str]:
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
           "--pids-limit", str(pids), "--memory", memory, "--memory-swap", memory, "--cpus", cpus,
           "--ulimit", "nofile=4096:4096", "--ulimit", "fsize=2147483648",
           "--network", "bridge" if network else "none",
           "-v", f"{work}:/work:rw", "-w", "/work",
           "-e", "HOME=/tmp"]
    if name:
        cmd += ["--name", name]
    if hasattr(os, "getuid"):
        cmd += ["--user", f"{os.getuid()}:{os.getgid()}"]
    return cmd + ["--", image] + list(command)


def _size(folder: Path, limit: int) -> int:
    """Bytes under `folder` (not following links), counting up to just past `limit`."""
    total = 0
    for root, _dirs, files in os.walk(folder):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                continue
            if total > limit:
                return total
    return total


def _tail(path: Path) -> str:
    with path.open("rb") as f:
        f.seek(max(0, path.stat().st_size - TAIL * 4))
        return f.read().decode("utf-8", errors="replace")[-TAIL:]


def run(workdir: str, command: list[str], image: str = "python:3.12-slim", timeout: int = 900,
        dry_run: bool = False, network: bool = False, max_work_bytes: int = MAX_WORK_BYTES,
        max_output_bytes: int = MAX_OUTPUT_BYTES) -> dict:
    """Run a command in a fresh container. The container is stopped (not only the engine's client) when it runs
    past `timeout`, writes more than `max_work_bytes` into /work or prints more than `max_output_bytes`;
    output goes to files outside the work folder, and only its tail is kept in memory."""
    name = f"humansout-{uuid.uuid4().hex[:16]}"
    cmd = build_command(workdir, command, image=image, network=network, name=name)
    if dry_run:
        return {"dry_run": True, "command": cmd}
    engine = find_engine()
    if engine is None:
        raise HumansOutError("no container engine found (install podman or docker); refusing to run mission code on the host")
    problem = engine_problem(engine)
    if problem:  # never let an engine error pass for a failing check
        raise HumansOutError(f"{engine} is installed but cannot run containers for this user ({problem[:200]}): "
                             f"{engine_fix(engine)}")
    work = Path(workdir).expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix="humansout-out-") as out_dir:
        out_path, err_path = Path(out_dir) / "stdout", Path(out_dir) / "stderr"
        with out_path.open("wb") as out, err_path.open("wb") as err:
            proc = subprocess.Popen(cmd, stdout=out, stderr=err, stdin=subprocess.DEVNULL)
            deadline, why = time.monotonic() + timeout, None
            while proc.poll() is None:
                try:
                    proc.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if time.monotonic() > deadline:
                    why = "timeout"
                elif out_path.stat().st_size + err_path.stat().st_size > max_output_bytes:
                    why = f"printed more than {max_output_bytes >> 20} MB"
                elif _size(work, max_work_bytes) > max_work_bytes:
                    why = f"wrote more than {max_work_bytes >> 20} MB into /work"
                if why:
                    _stop(engine, name, proc)
                    break
        stdout, stderr = _tail(out_path), _tail(err_path)
    if why == "timeout":
        return {"exit_code": None, "timeout": True, "stdout": stdout[-TAIL:], "stderr": f"killed after {timeout}s"}
    if why:
        return {"exit_code": None, "killed": why, "stdout": stdout, "stderr": stderr + f"\nhumansout: stopped: {why}"}
    return {"exit_code": proc.returncode, "stdout": stdout, "stderr": stderr}


def _stop(engine: str, name: str, proc: subprocess.Popen) -> None:
    """Stop the container itself: killing the engine's client alone leaves it running."""
    try:
        subprocess.run([engine, "kill", name], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pass
    proc.kill()
    try:
        proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        pass
    try:
        subprocess.run([engine, "rm", "-f", name], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        pass
