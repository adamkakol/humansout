"""Runnable verification: settle disputes by re-running them, not by opinion.

A mission may declare a check in its manifest: a container image, a command, the
files the check needs and the file name of the deliverable. A claim's first
artifact is the deliverable. A counterexample challenge adds files (usually one
more test) and says: with these files, the mission's own check fails.

Every node can re-run that in its sandbox and reach the same answer, so whether
a counterexample *reproduces* is decided by execution. Whether it *expects the
right thing* is a question of meaning: the claim's author may dispute it and the
mission creator rules (see state.py).

Results are stored locally per node (Store.save_verification) and read by the
deterministic fold. A node that has not verified a counterexample does not guess.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from . import sandbox
from .core import HumansOutError, canonical, sha256_hex

NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$")  # as state.NAME_RE

DEFAULT_TIMEOUT = 300
LABELS = {"fail": "reproduced", "pass": "not_reproduced", "flaky": "flaky", "invalid": "invalid"}


def verification_key(check: dict, deliverable_cid: str, extra_files) -> str:
    """Identifies one run: same check, same deliverable, same extra files -> same key on every node."""
    return sha256_hex(canonical({
        "image": check["image"],
        "command": check["command"],
        "timeout": check.get("timeout", DEFAULT_TIMEOUT),
        "deliverable": [check["deliverable"], deliverable_cid],
        "check_files": [[f["name"], f["cid"]] for f in check.get("files", [])],
        "extra_files": [[f["name"], f["cid"]] for f in extra_files],
    }))


def run_check(get_blob, check: dict, deliverable_cid: str, extra_files=(), runs: int = 1) -> dict:
    """Run a mission's check on a deliverable, optionally with counterexample files.

    `get_blob(cid)` returns the bytes of a content id or None. Every run uses a fresh
    work directory inside the sandbox (no network, read-only root, only /work mounted).
    The result is "pass", "fail", or "flaky" when repeated runs disagree.
    """
    wanted = ([(check["deliverable"], deliverable_cid)]
              + [(f["name"], f["cid"]) for f in check.get("files", [])]
              + [(f["name"], f["cid"]) for f in extra_files])
    blobs = {}
    for name, cid in wanted:
        if not isinstance(name, str) or not NAME_RE.match(name):  # the fold checks this too; never "../x" on the host
            raise HumansOutError(f"invalid file name {name!r} in the check")
        data = get_blob(cid)
        if data is None:
            raise HumansOutError(f"artifact {cid} ({name}) is not available here; run `humansout sync` first")
        blobs[name] = data
    codes, last = [], {}
    for _ in range(max(1, int(runs))):
        work = Path(tempfile.mkdtemp(prefix="humansout-verify-"))
        try:
            for name, data in blobs.items():
                (work / name).write_bytes(data)
            last = sandbox.run(str(work), list(check["command"]), image=check["image"],
                               timeout=int(check.get("timeout", DEFAULT_TIMEOUT)))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if last.get("timeout") or last.get("killed"):
            codes.append("timeout")  # a check that never finishes (or floods the disk) did not pass
            continue
        if last["exit_code"] == 125 and not _engine_ready(check["image"]):
            # the engine itself failed (no image, no daemon). With the engine ready and the image here, 125 came
            # from the mission code (os._exit(125)): an ordinary failing exit code, never "the check did not run"
            # (found by the security-review swarm: posing as the engine stalled every node's verification)
            raise HumansOutError("the container engine could not start the check: " + last["stderr"][-400:])
        codes.append(last["exit_code"])
    failed = {code != 0 for code in codes}
    result = "flaky" if len(failed) > 1 else ("fail" if failed == {True} else "pass")
    return {"result": result, "exit_codes": codes, "image": check["image"],
            "stdout": last.get("stdout", "")[-4000:], "stderr": last.get("stderr", "")[-4000:]}


def _engine_ready(image: str) -> bool:
    import subprocess
    engine = sandbox.find_engine()
    if engine is None or sandbox.engine_problem(engine):
        return False
    try:
        return subprocess.run([engine, "image", "inspect", image], capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
