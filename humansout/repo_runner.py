"""The check of a repo mission. It runs inside the sandbox on every verifier, standard library only.

/work holds repo.tar.gz (the snapshot), repo_check.json (the test commands), this file, the
deliverable (a unified diff, as from `git diff` or `humansout repo diff`) and any counterexample
files. The snapshot is unpacked into /work/.humansout-run/repo, the diff applied strictly, vendored wheels
installed offline, then the mission's test command runs, and the counterexample tests after it.
Exit code 0 only if everything passed.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile

# Under /work, not /tmp: the sandbox's /tmp is noexec, so compiled extensions (cryptography, numpy) cannot load there.
WORK = "/work"
REPO, DEPS = "/work/.humansout-run/repo", "/work/.humansout-run/deps"
OWN = {"repo.tar.gz", "repo_check.json", "run_repo_check.py"}
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class PatchError(Exception):
    pass


def _path(raw):
    raw = raw.split("\t")[0].strip()
    if raw == "/dev/null":
        return None
    if raw[:2] in ("a/", "b/"):
        raw = raw[2:]
    parts = raw.split("/")
    if raw.startswith("/") or ".." in parts or not raw:
        raise PatchError(f"unsafe path in diff: {raw!r}")
    return raw


def parse(text):
    """[(old_path, new_path, [(old_start, [lines with ' ', '-', '+' prefixes])])]"""
    lines = text.splitlines(keepends=True)
    files, i = [], 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("GIT binary patch") or line.startswith("Binary files"):
            raise PatchError("binary patches are not supported")
        if line.startswith("rename from ") or line.startswith("copy from "):
            raise PatchError("renames and copies need content lines: make the diff with `git diff --no-renames`")
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old, new = _path(line[4:]), _path(lines[i + 1][4:])
            hunks, i = [], i + 2
            while i < len(lines) and lines[i].startswith("@@"):
                m = HUNK.match(lines[i])
                if not m:
                    raise PatchError(f"bad hunk header: {lines[i].strip()}")
                old_len = int(m.group(2)) if m.group(2) is not None else 1
                new_len = int(m.group(4)) if m.group(4) is not None else 1
                body, i = [], i + 1
                seen_old = seen_new = 0
                while i < len(lines) and (seen_old < old_len or seen_new < new_len or lines[i].startswith("\\")):
                    ln = lines[i]
                    if ln.startswith("\\"):  # "\ No newline at end of file" applies to the previous line
                        if body:
                            body[-1] = body[-1][:-1] if body[-1].endswith("\n") else body[-1]
                        i += 1
                        continue
                    tag = ln[:1] if ln[:1] in " -+" else " "
                    rest = ln[1:] if ln[:1] in " -+" else ln
                    if ln in ("\n", "\r\n"):
                        tag, rest = " ", ln
                    body.append(tag + rest)
                    seen_old += tag in " -"
                    seen_new += tag in " +"
                    i += 1
                hunks.append((int(m.group(1)), body))
            files.append((old, new, hunks))
            continue
        i += 1
    return files


def patched(src, target, hunks):
    """A file's lines after its hunks: each found at its line, or nearby (like patch's offset)."""
    out, pos = [], 0
    for start, body in hunks:
        want = [ln[1:] for ln in body if ln[0] in " -"]
        at = max(0, start - 1) if want else start
        found = None
        for delta in range(0, len(src) + 1):  # exact place first, then nearby
            for cand in (at - delta, at + delta):
                if pos <= cand <= len(src) and src[cand:cand + len(want)] == want:
                    found = cand
                    break
            if found is not None:
                break
        if found is None:
            raise PatchError(f"{target}: hunk at line {start} does not match the snapshot")
        out.extend(src[pos:found])
        out.extend(ln[1:] for ln in body if ln[0] in " +")
        pos = found + len(want)
    out.extend(src[pos:])
    return out


def apply(root, text):
    changed = []
    for old, new, hunks in parse(text):
        target = new or old
        path = os.path.join(root, target)
        if old is None:
            src = []
        else:
            try:
                with open(os.path.join(root, old), encoding="utf-8", newline="") as f:
                    src = f.read().splitlines(keepends=True)
            except FileNotFoundError:
                raise PatchError(f"{old}: no such file in the snapshot") from None
        out = patched(src, target, hunks)
        if new is None:
            os.remove(os.path.join(root, old))
        else:
            os.makedirs(os.path.dirname(path) or root, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write("".join(out))
            if old and old != new:
                os.remove(os.path.join(root, old))
        changed.append(target)
    return changed


def main():
    with open(os.path.join(WORK, "repo_check.json"), encoding="utf-8") as f:
        cfg = json.load(f)
    extra = sorted(n for n in os.listdir(WORK)  # counterexample files: whatever else the verifier put here
                   if n not in OWN and n != cfg["deliverable"] and not n.startswith("."))
    shutil.rmtree(os.path.dirname(REPO), ignore_errors=True)
    with tarfile.open(os.path.join(WORK, "repo.tar.gz")) as tar:
        tar.extractall(REPO, filter="data")
    with open(os.path.join(WORK, cfg["deliverable"]), encoding="utf-8", newline="") as f:
        patch = f.read()
    try:
        touched = sorted({p for old, new, _ in parse(patch) for p in (old, new) if p})
    except PatchError as e:
        print(f"humansout: the patch does not apply: {e}", file=sys.stderr)
        return 2
    before = {p: _read(os.path.join(REPO, p)) for p in touched if os.path.basename(p) in RUNNER_SECTIONS}
    existing = {os.path.relpath(os.path.join(d, n), REPO).replace(os.sep, "/")
                for d, dirs, files in os.walk(REPO) for n in dirs + files}
    try:
        changed = apply(REPO, patch)
    except PatchError as e:
        print(f"humansout: the patch does not apply: {e}", file=sys.stderr)
        return 2
    print(f"humansout: patch applied, {len(changed)} file(s) changed", flush=True)
    env = dict(os.environ, HOME="/tmp", PYTHONDONTWRITEBYTECODE="1", PIP_NO_CACHE_DIR="1",
               PIP_DISABLE_PIP_VERSION_CHECK="1", PYTHONPATH=os.pathsep.join([DEPS, REPO]))
    wheels = os.path.join(REPO, ".humansout", "wheels")
    if os.path.isdir(wheels) and os.listdir(wheels):
        # by distribution name, so pip picks the wheel that fits this machine (x86_64 and aarch64 can both be vendored)
        names = sorted({w.split("-")[0] for w in os.listdir(wheels) if w.endswith(".whl")})
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-index", "--find-links", wheels,
                            "--target", DEPS] + names, env=env)
        if r.returncode:
            print("humansout: installing the vendored wheels failed", file=sys.stderr)
            return 3
    deps = {n.split(".")[0] for n in os.listdir(DEPS)} if os.path.isdir(DEPS) else set()
    after = {p: _read(os.path.join(REPO, p)) for p in before}
    refused = runner_changes(touched, before, after, existing, deps)
    if refused:  # before anything runs: the patch decides nothing about how it is checked
        print("humansout: the patch may not change how the tests run: " + "; ".join(refused), file=sys.stderr)
        return 1
    code = subprocess.run(cfg["test"], cwd=REPO, env=env).returncode
    tests = [n for n in extra if n.startswith("test_") and n.endswith(".py")]
    if tests:  # counterexamples: copied next to the repo and run after the mission's own tests
        dest = os.path.join(REPO, ".humansout_extra")
        os.makedirs(dest, exist_ok=True)
        for n in extra:
            shutil.copy(os.path.join(WORK, n), dest)
        cmd = list(cfg.get("extra", [sys.executable, "-m", "unittest"]))
        if cmd[-1:] == ["unittest"] or cmd[-2:] == ["-m", "unittest"]:
            env["PYTHONPATH"] = os.pathsep.join([dest, env["PYTHONPATH"]])
            cmd += [t[:-3] for t in tests]
        else:
            cmd += [os.path.join(dest, t) for t in tests]
        code = code or subprocess.run(cmd, cwd=REPO, env=env).returncode
    if not code and cfg.get("regression"):
        code = regression(cfg, patch, env)
    return 1 if code else 0


# What decides how the tests run: a patch may not add or change it, in any mode (found by the security-review swarm,
# round 4: a patch could reconfigure pytest, add a conftest.py, or put its own module where the test runner or a
# standard module is imported from, and then the suite, its own regression test and every counterexample "passed",
# so a wrong fix could not be refuted). The tests run with the repository first on the path, so a new top-level
# module named like a standard or vendored one would be imported instead of it.
RUNNER_FILES = ("pytest.ini", ".pytest.ini", "pytest.toml", ".pytest.toml", "conftest.py", "sitecustomize.py",
                "usercustomize.py")  # .pytest.ini: found by a reviewing agent (round 4), in a fix that left it out
RUNNER_SECTIONS = {"pyproject.toml": "[tool.pytest", "setup.cfg": "[tool:pytest]", "tox.ini": "[pytest]"}
RUNNER_MODULES = {"pytest", "_pytest", "pluggy", "iniconfig", "packaging", "py"}
COMMON_TOP = {"test", "tests"}  # standard names, but every project's test folder


def _read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _section(text, header):
    """A config file's test-runner section, as written (or "" when there is none)."""
    out, inside = [], False
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            inside = stripped.startswith(header)
        if inside and stripped:
            out.append(stripped)
    return "\n".join(out)


def _import_roots(path):
    """(name, root) for each way a path could be imported directly: from the repository's root (on the path), and
    from a folder inside a test folder (pytest puts test folders without __init__.py first on the path). The root is
    the module file or package folder that would be found."""
    parts = path.split("/")
    starts = [0] + [i + 1 for i, part in enumerate(parts[:-1]) if part.startswith("test")]
    out = set()
    for start in starts:
        for end in range(start, len(parts) if start else start + 1):
            part = parts[end]
            if end == len(parts) - 1:
                if part.endswith(".py"):
                    out.add((part[:-3], "/".join(parts[:end + 1])))
            else:
                out.add((part, "/".join(parts[:end + 1])))
    return out


def runner_changes(paths, before, after, existing, deps):
    """Why a patch touching `paths` changes how the tests run ([] if it does not). `before` and `after` hold the
    texts of the config files that have a test-runner section; `existing` the snapshot's files and folders (a
    module the project already had is its own, not a new one put in front of a standard one)."""
    shadowed = (set(getattr(sys, "stdlib_module_names", ())) | RUNNER_MODULES | set(deps)) - COMMON_TOP
    reasons = []
    for p in paths:
        base = os.path.basename(p)
        if base in RUNNER_FILES or base.endswith(".pth"):
            reasons.append(f"{p} configures or hooks the test run")
        elif base in RUNNER_SECTIONS and _section(before.get(p), RUNNER_SECTIONS[base]) != \
                _section(after.get(p), RUNNER_SECTIONS[base]):
            reasons.append(f"{p} changes its test-runner section")
        for name, root in sorted(_import_roots(p)):
            if name in shadowed and root not in existing:
                reasons.append(f"{p} would be imported in place of the module {name}")
                break
    return reasons


def _tests_command(runner, root, tests):
    """The extra runner on these test files: module names for unittest, paths for the others."""
    cmd = list(runner)
    if cmd[-1:] == ["unittest"] or cmd[-2:] == ["-m", "unittest"]:
        return cmd + [t[:-3].replace("/", ".") for t in tests]
    return cmd + [os.path.join(root, t) for t in tests]


def regression(cfg, patch, env):
    """A bug-fix mission's stronger check (repo mission --require-regression-test): the patch must add or change
    a test_*.py file, and those tests must fail on the snapshot without the rest of the patch. An empty patch, or
    one whose test passes without the fix, cannot pass. It cannot tell a test that shows a real problem from one
    that only needs a name the patch adds: the test's author decides how it fails (an ImportError can as well be
    an assertion), so no reading of the failure closes that (found by the security-review swarm, round 3).
    Reviewers do: a counterexample showing that the work item's problem is still there refutes the claim."""
    changed = sorted({p for old, new, _ in parse(patch) for p in (old, new) if p})  # main() refused runner changes
    tests = [p for p in changed if os.path.basename(p).startswith("test_") and p.endswith(".py")
             and os.path.exists(os.path.join(REPO, p))]
    if not tests:
        print("humansout: this mission asks for a regression test: the patch must add or change a test_*.py file "
              "that fails without the fix", file=sys.stderr)
        return 1
    orig = os.path.join(os.path.dirname(REPO), "orig")
    shutil.rmtree(orig, ignore_errors=True)
    with tarfile.open(os.path.join(WORK, "repo.tar.gz")) as tar:
        tar.extractall(orig, filter="data")
    for t in tests:
        os.makedirs(os.path.dirname(os.path.join(orig, t)), exist_ok=True)
        shutil.copy(os.path.join(REPO, t), os.path.join(orig, t))
    runner = cfg.get("extra", [sys.executable, "-m", "unittest"])
    if subprocess.run(_tests_command(runner, REPO, tests), cwd=REPO, env=env).returncode:
        print("humansout: the patch's tests do not pass with the patch", file=sys.stderr)
        return 1
    old_env = dict(env, PYTHONPATH=os.pathsep.join([DEPS, orig]))
    unfixed = subprocess.run(_tests_command(runner, orig, tests), cwd=orig, env=old_env).returncode
    if unfixed != 1:  # 0: they pass without the fix; 2 and more: they do not even run there (pytest: collection)
        print(f"humansout: the patch's tests must fail on the unfixed code (exit code 1); they gave {unfixed}: "
              "they do not show a problem that the patch fixes", file=sys.stderr)
        return 1
    print(f"humansout: regression test: {', '.join(tests)} fail without the fix and pass with it", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
