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
        out, pos = [], 0
        for start, body in hunks:
            want = [ln[1:] for ln in body if ln[0] in " -"]
            at = max(0, start - 1) if want else start
            found = None
            for delta in range(0, len(src) + 1):  # exact place first, then nearby (like patch's offset)
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
    return 1 if code else 0


if __name__ == "__main__":
    sys.exit(main())
