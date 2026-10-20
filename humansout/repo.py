"""Repo missions: a repository snapshot is the starting point, its own tests are the check, a patch is the deliverable.

    humansout repo mission . --goal "Fix #42: ..." --test "python -m unittest discover -s tests"
    humansout repo checkout <mission> work/fix42          # a working copy for your agent
    humansout repo diff <mission> work/fix42 --out patch.diff
    humansout try <mission> patch.diff                    # exactly what verifiers will run

The snapshot holds the files git tracks plus untracked files git does not ignore (or every file
when there is no git), never anything that looks like a secret. Dependencies are vendored as
wheels (`--deps pytest`), because checks run with no network. A breaker objects with an extra
test file (test_*.py): the runner runs it after the mission's own tests.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from importlib import resources
from pathlib import Path

from . import sandbox
from .core import HumansOutError
from .node import secret_reason

DELIVERABLE = "patch.diff"
RUNNER = "run_repo_check.py"
SKIP_DIRS = {".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "env", "node_modules", ".tox", ".nox",
             ".mypy_cache", ".pytest_cache", ".ruff_cache", ".eggs", ".humansout", ".humansout_extra"}
DEFAULT_CRITERIA = ("The patch applies to the snapshot and the mission's tests pass; no counterexample test that "
                    "checks what the goal asks for may fail.")


def is_repo_mission(check: dict | None) -> bool:
    return bool(check) and check.get("deliverable") == DELIVERABLE and any(
        f["name"] == RUNNER for f in check.get("files", []))


def _walk(root: Path) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info"))
        for name in sorted(filenames):
            out.append((Path(dirpath) / name).relative_to(root).as_posix())
    return out


def list_files(repo: Path, tracked_only: bool = False) -> tuple[list[str], list[dict]]:
    """Files to put in the snapshot, and what was left out and why."""
    repo = repo.resolve()
    if (repo / ".git").exists() and shutil.which("git"):
        args = ["git", "-C", str(repo), "ls-files", "-z", "--cached"] + ([] if tracked_only else ["--others", "--exclude-standard"])
        names = sorted(set(n for n in subprocess.run(args, capture_output=True, check=True).stdout.decode().split("\0") if n))
    else:
        names = _walk(repo)
    keep, skipped = [], []
    for rel in names:
        p = repo / rel
        reason = secret_reason(Path(rel))
        if not reason and any(part in SKIP_DIRS for part in Path(rel).parts):
            reason = "tool or cache folder"
        if not reason and (p.is_symlink() or not p.is_file()):
            reason = "not a regular file (symlink or missing)"
        if reason:
            skipped.append({"file": rel, "reason": reason})
        else:
            keep.append(rel)
    return keep, skipped


def pack(repo: Path, files: list[str], wheels: list[Path] = ()) -> bytes:
    """A deterministic .tar.gz: the same files give the same bytes (and so the same mission)."""
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as tar:
        entries = [(rel, repo / rel) for rel in files] + [(f".humansout/wheels/{w.name}", w) for w in wheels]
        for name, path in sorted(entries):
            data = path.read_bytes()
            info = tarfile.TarInfo(name)
            info.size, info.mtime, info.uid, info.gid, info.uname, info.gname = len(data), 0, 0, 0, "", ""
            info.mode = 0o755 if os.access(path, os.X_OK) and not name.startswith(".humansout/") else 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


ARCHES = ("x86_64", "aarch64")  # verifiers run on both: vendor wheels for each


def download_wheels(packages: list[str], python: str, dest: Path, arches=ARCHES) -> list[Path]:
    """Fetch wheels for the check's Python, for every architecture verifiers use (pip needs network here,
    on the creator's machine only; checks stay offline). Pure-Python wheels are shared."""
    for arch in arches:
        tags = [f"manylinux_2_{v}_{arch}" for v in (36, 34, 28, 17)] + [f"manylinux2014_{arch}"]
        cmd = [sys.executable, "-m", "pip", "download", "-q", "--disable-pip-version-check", "--only-binary=:all:",
               "--python-version", python, "--implementation", "cp", "-d", str(dest), *packages]
        for tag in tags:
            cmd[cmd.index("-d"):cmd.index("-d")] = ["--platform", tag]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode:
            raise HumansOutError(f"pip download failed for {arch}: " + (res.stderr or res.stdout)[-600:])
    return sorted(dest.glob("*.whl"))


def _image_python(image: str) -> str:
    tag = image.split(":", 1)[1] if ":" in image else ""
    version = tag.split("-")[0].split("@")[0]
    return version if version.replace(".", "").isdigit() and version.count(".") == 1 else "3.12"


def prepare(node, repo: str | Path, test: list[str], extra: list[str] | None = None, deps: list[str] = (),
            wheels_dir: str | None = None, image: str | None = None, tracked_only: bool = False) -> dict:
    """Pack the snapshot and write the check files. Nothing is announced."""
    repo = Path(repo).expanduser().resolve()
    if not repo.is_dir():
        raise HumansOutError(f"{repo} is not a directory")
    image = image or node.store.config().get("sandbox_image", "python:3.12-slim")
    files, skipped = list_files(repo, tracked_only)
    if not files:
        raise HumansOutError("nothing to pack: the repository has no files (are they committed or at least not ignored?)")
    wheels = sorted(Path(wheels_dir).expanduser().glob("*.whl")) if wheels_dir else []
    tmp = Path(tempfile.mkdtemp(prefix="humansout-wheels-"))
    try:
        if deps:
            wheels += download_wheels(list(deps), _image_python(image), tmp)
        arch_of = lambda w: next((a for a in ARCHES if a in w.name), None)  # noqa: E731
        binary_dists = {w.name.split("-")[0] for w in wheels if not w.name.endswith("-none-any.whl")}
        binary = [d for d in sorted(binary_dists)
                  if {arch_of(w) for w in wheels if w.name.split("-")[0] == d} != set(ARCHES)]
        data = pack(repo, files, wheels)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    limit = int(node.store.config().get("max_blob_bytes", 50 * 1024 * 1024))
    if len(data) > limit:
        raise HumansOutError(f"the snapshot is {len(data)} bytes, more than max_blob_bytes ({limit}); "
                             "leave out large files (commit or .gitignore them) or vendor fewer wheels")
    folder = node.home / "repo-missions" / hashlib.sha256(data).hexdigest()[:16]
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "repo.tar.gz").write_bytes(data)
    (folder / "repo_check.json").write_text(json.dumps(
        {"deliverable": DELIVERABLE, "test": list(test), "extra": list(extra or ["python", "-m", "unittest"])}, indent=1))
    (folder / RUNNER).write_text(resources.files("humansout").joinpath("repo_runner.py").read_text(encoding="utf-8"))
    check = {"command": ["python", f"/work/{RUNNER}"], "deliverable": DELIVERABLE, "image": image,
             "files": [str(folder / n) for n in ("repo.tar.gz", "repo_check.json", RUNNER)]}
    warnings = [f"{d} is vendored as a binary wheel for only some architectures: verifiers on the others cannot "
                f"install it (use --deps to fetch every architecture)" for d in binary]
    return {"check": check, "files": len(files), "bytes": len(data), "wheels": [w.name for w in wheels],
            "skipped": skipped, "warnings": warnings}


def baseline(node, check: dict, timeout: int) -> tuple[str, str]:
    """Run the mission's tests on the unchanged snapshot (an empty patch): a bug-fix mission should fail."""
    from .verify import run_check
    if not sandbox.find_engine():
        return "not run (no container engine)", ""
    node.allowed_image(check["image"])
    entries = node._file_entries(check["files"])
    empty = node.store.put_blob(b"")
    res = run_check(node.store.get_blob, dict(check, files=entries, timeout=timeout), empty, (), 1)
    return {"pass": "passes", "fail": "fails", "flaky": "flaky"}[res["result"]], (res["stdout"] + res["stderr"])[-1500:]


def create(node, repo, goal: str, test: list[str], criteria: str | None = None, category: str = "software/python",
           extra=None, deps=(), wheels_dir=None, image=None, timeout: int = 600, tracked_only: bool = False,
           require_witness: bool = False, dry_run: bool = False, **mission_kwargs) -> dict:
    prep = prepare(node, repo, test, extra, deps, wheels_dir, image, tracked_only)
    check = dict(prep["check"], timeout=timeout, require_witness=require_witness)
    base, output = baseline(node, check, timeout)
    report = {"files": prep["files"], "bytes": prep["bytes"], "wheels": prep["wheels"],
              "skipped": prep["skipped"][:50], "baseline": base, "baseline_output": output,
              "warnings": list(prep["warnings"])}
    if base == "passes":
        report["warnings"].append("the tests already pass on the unchanged code: an empty patch would be accepted. "
                                  "Point --test at a test that shows the problem (it should fail now).")
    if dry_run:
        return {"dry_run": True, **report}
    created = node.create_mission(goal, category, criteria or DEFAULT_CRITERIA, check=check, **mission_kwargs)
    mid = created["mission_id"]
    return {**created, **report, "next": [f"humansout repo checkout {mid[:12]} <dir>   # what builders do",
                                          f"humansout repo diff {mid[:12]} <dir> --out patch.diff",
                                          f"humansout try {mid[:12]} patch.diff"]}


def _snapshot(node, mission: str) -> tuple[str, bytes]:
    world = node.world()
    mid = node._mid(world, mission)
    check = world.missions[mid]["check"]
    if not is_repo_mission(check):
        raise HumansOutError("this is not a repo mission (its deliverable is not patch.diff)")
    cid = next(f["cid"] for f in check["files"] if f["name"] == "repo.tar.gz")
    data = node._blob_getter()(cid)
    if data is None:
        raise HumansOutError("the repository snapshot is not available here; run `humansout sync` first")
    return mid, data


def _members(data: bytes) -> dict[str, bytes]:
    out = {}
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            if m.isfile() and not m.name.startswith(".humansout/"):
                out[m.name] = tar.extractfile(m).read()
    return out


def checkout(node, mission: str, dest: str) -> dict:
    """Unpack the mission's snapshot into an empty folder, as a working copy for an agent."""
    path = Path(dest).expanduser()
    node.check_local_path(path, "check out into")
    if path.exists() and any(path.iterdir()):
        raise HumansOutError(f"{path} is not empty")
    mid, data = _snapshot(node, mission)
    path.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        members = [m for m in tar.getmembers() if not m.name.startswith(".humansout/")]
        if hasattr(tarfile, "data_filter"):
            tar.extractall(path, members=members, filter="data")
        else:  # Python releases before the extraction filters: allow only plain files at safe paths
            for m in members:
                parts = Path(m.name).parts
                if not m.isfile() or m.name.startswith("/") or ".." in parts:
                    raise HumansOutError(f"unsafe entry in the snapshot: {m.name!r}")
                target = path.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(tar.extractfile(m).read())
    return {"mission_id": mid, "checked_out_to": str(path.resolve()), "files": len(_members(data)),
            "next": f"change the code, then: humansout repo diff {mid[:12]} {path} --out patch.diff"}


def _lines(data: bytes, name: str) -> list[str]:
    try:
        return data.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        raise HumansOutError(f"{name} is not UTF-8 text: repo missions take text diffs only") from None


def _hunks(a: list[str], b: list[str], old: str, new: str) -> list[str]:
    import difflib
    out = []
    for line in difflib.unified_diff(a, b, old, new):
        out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return out


def diff(node, mission: str, workdir: str, out: str | None = None) -> dict:
    """The unified diff from the mission's snapshot to a working copy (no git needed)."""
    root = Path(workdir).expanduser().resolve()
    node.check_local_path(root, "diff")
    if not root.is_dir() or not any(root.iterdir()):
        # never turn a missing working copy into a patch that deletes the whole repository
        raise HumansOutError(f"no working copy at {workdir}: check the mission out there first (repo checkout)")
    mid, data = _snapshot(node, mission)
    before = _members(data)
    after_names, skipped = [], []
    for rel in _walk(root):
        path = root / rel
        reason = secret_reason(Path(rel))
        if not reason and (path.is_symlink() or root not in path.resolve().parents):
            # a link (an agent can make one from its sandbox) could pull any file of this machine into the patch
            reason = "a symbolic link: never read or published"
        (skipped.append({"file": rel, "reason": reason}) if reason else after_names.append(rel))
    text, changed = [], []
    for name in sorted(set(before) | set(after_names)):
        old = before.get(name)
        new = (root / name).read_bytes() if name in after_names else None
        if old == new:
            continue  # unchanged, binary or not: never decoded
        a = _lines(old, name) if old is not None else []
        b = _lines(new, name) if new is not None else []
        lines = _hunks(a, b, f"a/{name}" if name in before else "/dev/null",
                       f"b/{name}" if name in after_names else "/dev/null")
        if lines:
            text.extend(lines)
            changed.append(name)
    patch = "".join(text)
    deleted = [n for n in before if n not in after_names]
    if before and len(deleted) == len(before):
        raise HumansOutError(f"{workdir} shares no file with the snapshot: is it a checkout of mission {mid[:12]}?")
    result = {"changed": changed, "bytes": len(patch.encode()), "skipped": skipped}
    if out:
        target = Path(out).expanduser()
        node.check_local_path(target, "write")
        target.write_text(patch, encoding="utf-8")
        result["written_to"] = str(target.resolve())
    else:
        result["patch"] = patch
    return result
