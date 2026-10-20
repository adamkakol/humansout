"""Repo missions: snapshot, patch deliverable, the runner inside the sandbox, counterexamples on a repo."""

import base64
import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

from humansout import HumansOutError, Node, repo, sandbox
from humansout import repo_runner as runner
from humansout.cli import main

IMAGE = "python:3.12-slim"


def write(root: Path, files: dict):
    for name, text in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def tree(root: Path) -> dict:
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*")) if p.is_file()}


# ---- the patch format -----------------------------------------------------------------------------
def test_diff_round_trips_through_the_runner(tmp_path):
    before = {"pkg/a.py": "def f():\n    return 1\n", "gone.txt": "bye\n", "notes.md": "no newline at end",
              "pkg/łódź.py": "x = 'ł'\n" + "".join(f"line {i}\n" for i in range(40))}
    after = {"pkg/a.py": "def f():\n    return 2\n", "new/b.py": "y = 2\n", "notes.md": "no newline at end, edited",
             "pkg/łódź.py": "x = 'l'\n" + "".join(f"line {i}\n" for i in range(40)) + "tail\n"}
    old, new = tmp_path / "old", tmp_path / "new"
    write(old, before)
    write(new, after)
    patch = "".join(
        "".join(repo._hunks(old.joinpath(n).read_text().splitlines(True) if n in before else [],
                            new.joinpath(n).read_text().splitlines(True) if n in after else [],
                            f"a/{n}" if n in before else "/dev/null", f"b/{n}" if n in after else "/dev/null"))
        for n in sorted(set(before) | set(after)) if before.get(n) != after.get(n))
    runner.apply(str(old), patch)
    assert tree(old) == after


def test_git_style_diffs_apply_and_bad_ones_are_refused(tmp_path):
    write(tmp_path, {"src/m.py": "a = 1\nb = 2\nc = 3\n"})
    git_diff = ("diff --git a/src/m.py b/src/m.py\nindex 1234567..89abcde 100644\n--- a/src/m.py\n+++ b/src/m.py\n"
                "@@ -1,3 +1,3 @@\n a = 1\n-b = 2\n+b = 20\n c = 3\n")
    assert runner.apply(str(tmp_path), git_diff) == ["src/m.py"]
    assert (tmp_path / "src/m.py").read_text() == "a = 1\nb = 20\nc = 3\n"
    with pytest.raises(runner.PatchError, match="does not match"):
        runner.apply(str(tmp_path), git_diff)  # already applied: the context is gone
    with pytest.raises(runner.PatchError, match="unsafe path"):
        runner.apply(str(tmp_path), "--- a/../etc/passwd\n+++ b/../etc/passwd\n@@ -1 +1 @@\n-x\n+y\n")
    with pytest.raises(runner.PatchError, match="no-renames"):
        runner.parse("diff --git a/x b/y\nsimilarity index 100%\nrename from x\nrename to y\n")


# ---- the snapshot ----------------------------------------------------------------------------------
def test_snapshot_leaves_out_secrets_caches_and_ignored_files(tmp_path):
    write(tmp_path, {"app.py": "print(1)\n", ".env": "TOKEN=x\n", "deploy/id_rsa": "KEY\n", "key.json": "{}",
                     "pkg/__pycache__/a.pyc": "x", ".venv/lib/x.py": "x", "build.log": "noise\n", ".gitignore": "*.log\n"})
    files, skipped = repo.list_files(tmp_path)
    assert "app.py" in files and ".gitignore" in files
    assert not {".env", "deploy/id_rsa", "key.json"} & set(files)
    assert {s["file"] for s in skipped} >= {".env", "deploy/id_rsa", "key.json"}
    assert all("__pycache__" not in f and ".venv" not in f for f in files)
    if shutil.which("git"):
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
        files, _ = repo.list_files(tmp_path)
        assert "build.log" not in files and "app.py" in files  # .gitignore is respected
    assert repo.pack(tmp_path, files) == repo.pack(tmp_path, files)  # deterministic: same files, same mission


# ---- end to end in real containers -----------------------------------------------------------------
def engine_ready() -> bool:
    engine = sandbox.find_engine()
    return bool(engine) and subprocess.run([engine, "image", "inspect", IMAGE], capture_output=True).returncode == 0


needs_engine = pytest.mark.skipif(not engine_ready(), reason=f"needs docker or podman with {IMAGE}")


def make_wheel(folder: Path) -> Path:
    """A tiny pure-Python wheel, built by hand so the test needs no network."""
    files = {"tinydep.py": b"FACTOR = 1\n",
             "tinydep-1.0.dist-info/METADATA": b"Metadata-Version: 2.1\nName: tinydep\nVersion: 1.0\n",
             "tinydep-1.0.dist-info/WHEEL": b"Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    record = "".join(f"{n},sha256={base64.urlsafe_b64encode(hashlib.sha256(d).digest()).rstrip(b'=').decode()},{len(d)}\n"
                     for n, d in files.items()) + "tinydep-1.0.dist-info/RECORD,,\n"
    folder.mkdir(parents=True)
    path = folder / "tinydep-1.0-py3-none-any.whl"
    with zipfile.ZipFile(path, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
        z.writestr("tinydep-1.0.dist-info/RECORD", record)
    return path


def share(*nodes):
    for src in nodes:
        for dst in nodes:
            if src is not dst:
                for author in src.store.feed_authors():
                    if author != dst.agent_id:
                        dst.store.ingest(author, src.store.load_feed(author))
                for blob in src.store.blobs_dir.iterdir():
                    dst.store.put_blob(blob.read_bytes())


@needs_engine
def test_repo_mission_end_to_end(tmp_path, capsys):
    project = tmp_path / "project"
    write(project, {
        "calc/__init__.py": "import tinydep\n\n\ndef add(a, b):\n    return (a - b) * tinydep.FACTOR\n",
        "tests/__init__.py": "",
        "tests/test_calc.py": "import unittest\nfrom calc import add\n\n\nclass T(unittest.TestCase):\n"
                              "    def test_add(self):\n        self.assertEqual(add(2, 2), 4)\n",
        ".env": "SECRET=1\n"})
    wheels = make_wheel(tmp_path / "wheels").parent
    creator, builder, breaker = (Node(tmp_path / n) for n in ("creator", "builder", "breaker"))

    # the creator announces it from the CLI: the baseline fails, as a bug-fix mission should
    assert main(["--home", str(creator.home), "repo", "mission", str(project), "--goal", "Fix add()",
                 "--test", "python -m unittest discover -s tests -t .", "--wheels", str(wheels),
                 "--window", "100", "--dispute-window", "3"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["baseline"] == "fails" and created["wheels"] == ["tinydep-1.0-py3-none-any.whl"]
    assert ".env" in [s["file"] for s in created["skipped"]]
    mid = created["mission_id"]
    share(creator, builder, breaker)

    # the builder: checkout, fix (not quite right), diff, try, claim
    builder.join(mid)
    work = builder.home / "work" / "fix"
    repo.checkout(builder, mid, str(work))
    assert not (work / ".env").exists() and (work / "calc/__init__.py").exists()
    (work / "calc/__init__.py").write_text("import tinydep\n\n\ndef add(a, b):\n    return (abs(a) + abs(b)) * tinydep.FACTOR\n")
    out = repo.diff(builder, mid, str(work), str(builder.home / "work" / "patch.diff"))
    assert out["changed"] == ["calc/__init__.py"]
    assert builder.try_check(mid, out["written_to"])["result"] == "pass"
    cid = builder.publish_artifact(mid, out["written_to"])["cid"]
    claim = builder.claim_done(mid, "add() fixed; tests pass", cids=[cid])["claim"]
    share(creator, builder, breaker)

    # the breaker: a test the fix fails, and a witness patch on which it passes
    breaker.join(mid)
    bwork = breaker.home / "work" / "w"
    repo.checkout(breaker, mid, str(bwork))
    (bwork / "calc/__init__.py").write_text("import tinydep\n\n\ndef add(a, b):\n    return (a + b) * tinydep.FACTOR\n")
    witness = repo.diff(breaker, mid, str(bwork), str(breaker.home / "work" / "witness.diff"))["written_to"]
    test = breaker.home / "work" / "test_negative.py"
    test.write_text("import unittest\nfrom calc import add\n\n\nclass N(unittest.TestCase):\n"
                    "    def test_mixed_signs(self):\n        self.assertEqual(add(-1, 1), 0)\n")
    ch = breaker.challenge(claim, "add(-1, 1) returns 2", counterexample=[str(test)], witness=witness)
    assert ch["preflight"] == "reproduced" and "AssertionError" in ch["on_claim"]
    share(creator, builder, breaker)
    assert [r["counterexample"] for r in creator.verify()["verified"] if "counterexample" in r] == ["reproduced"]
    world = creator.world()
    view = world.item_view(claim)  # the dispute window is 3 s: by now the claim may already be refuted
    assert view["verification_status"] == "failing" or view["refuted_by"] == ch["challenge"]


@needs_engine
def test_a_patch_that_does_not_apply_fails_the_check(tmp_path):
    project = tmp_path / "p"
    write(project, {"m.py": "X = 1\n", "test_m.py": "import unittest\nimport m\n\n\nclass T(unittest.TestCase):\n"
                                                    "    def test_x(self):\n        self.assertEqual(m.X, 2)\n"})
    node = Node(tmp_path / "n")
    mid = repo.create(node, project, "X must be 2", ["python", "-m", "unittest", "test_m"], challenge_window=60)["mission_id"]
    bad = tmp_path / "bad.diff"
    bad.write_text("--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-X = 5\n+X = 2\n")
    out = node.try_check(mid, str(bad))
    assert out["result"] == "fail" and "does not apply" in out["output"]
    good = tmp_path / "good.diff"
    good.write_text("--- a/m.py\n+++ b/m.py\n@@ -1 +1 @@\n-X = 1\n+X = 2\n")
    assert node.try_check(mid, str(good))["result"] == "pass"


def test_not_a_repo_mission(tmp_path):
    node = Node(tmp_path / "n")
    mid = node.create_mission("g", "software/python", "c")["mission_id"]
    with pytest.raises(HumansOutError, match="not a repo mission"):
        repo.checkout(node, mid, str(tmp_path / "x"))


@needs_engine
def test_the_built_in_agent_can_work_on_a_repo_mission(tmp_path):
    from humansout.agent import LocalAgent
    project = tmp_path / "p"
    write(project, {"lib/text.py": "def shout(s):\n    return s.upper()\n", "lib/__init__.py": "", "tests/__init__.py": "",
                    "tests/test_text.py": "import unittest\nfrom lib.text import shout\n\n\nclass T(unittest.TestCase):\n"
                                          "    def test_bang(self):\n        self.assertEqual(shout('hi'), 'HI!')\n"})
    creator, me = Node(tmp_path / "creator"), Node(tmp_path / "me", restrict_paths=True)
    mid = repo.create(creator, project, "shout adds '!'", ["python", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
                      challenge_window=60)["mission_id"]
    share(creator, me)
    me.store.set_config("agent_api", "http://127.0.0.1:9/v1")
    me.store.set_config("agent_model", "m")
    agent = LocalAgent(me, me.home / "work" / mid[:12], log=lambda line: None)
    agent.call("join_mission", {"mission": mid})
    agent.call("repo_checkout", {"mission": mid})
    assert "repo/lib/text.py" in agent.call("list_files", {})
    assert agent.call("edit_file", {"name": "repo/lib/text.py", "old": "s.upper()", "new": "s.upper() + '!'"}) == "edited repo/lib/text.py"
    assert agent.call("repo_diff", {"mission": mid})["changed"] == ["lib/text.py"]
    assert agent.call("run_check", {"mission": mid}).startswith("pass")
    claim = agent.call("submit", {"mission": mid, "name": "patch.diff", "evidence": "adds '!'"})["claim"]
    assert me.world().items[claim]["evidence"]["cids"]
    applied = agent.call("repo_apply", {"mission": mid, "patch": "patch.diff", "folder": "claimed"})
    assert applied["changed"] == ["lib/text.py"] and "+ '!'" in agent.call("read_file", {"name": "claimed/lib/text.py"})
    with pytest.raises(HumansOutError, match="inside your work folder"):
        agent.call("read_file", {"name": "repo/../../escape"})


def test_diff_without_a_checkout_never_deletes_the_repository(tmp_path):
    """Found by a local model: repo_diff before repo_checkout produced a patch deleting every file."""
    project = tmp_path / "p"
    write(project, {"a.py": "x = 1\n", "b.py": "y = 2\n"})
    node = Node(tmp_path / "n")
    prep = repo.prepare(node, project, ["true"])
    mid = node.create_mission("g", "software/python", "c", check=dict(prep["check"]))["mission_id"]
    with pytest.raises(HumansOutError, match="no working copy"):
        repo.diff(node, mid, str(tmp_path / "missing"))
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "unrelated.txt").write_text("hi\n")
    with pytest.raises(HumansOutError, match="shares no file with the snapshot"):
        repo.diff(node, mid, str(tmp_path / "other"))


def test_submit_joins_and_run_check_explains_a_missing_patch(tmp_path):
    from humansout.agent import LocalAgent
    project = tmp_path / "p"
    write(project, {"a.py": "x = 1\n"})
    creator, me = Node(tmp_path / "creator"), Node(tmp_path / "me", restrict_paths=True)
    prep = repo.prepare(creator, project, ["true"])
    mid = creator.create_mission("g", "software/python", "c", check=dict(prep["check"]))["mission_id"]
    share(creator, me)
    me.store.set_config("agent_api", "http://127.0.0.1:9/v1")
    me.store.set_config("agent_model", "m")
    agent = LocalAgent(me, me.home / "work" / "x", log=lambda line: None)
    assert "no patch.diff in your work folder yet" in agent.call("try_check", {"mission": mid})
    agent.call("repo_checkout", {"mission": mid})
    agent.call("edit_file", {"name": "repo/a.py", "old": "x = 1", "new": "x = 2"})
    agent.call("repo_diff", {"mission": mid})
    claim = agent.call("submit", {"mission": mid, "name": "patch.diff", "evidence": "x is 2"})["claim"]
    assert me.agent_id in me.world().missions[mid]["participants"] and me.world().items[claim]["kind"] == "claim"


def test_diff_never_follows_links_out_of_the_working_copy(tmp_path):
    """Found in the prompt-injection test: a link made from inside the sandbox put an outside file into the patch."""
    import os
    project = tmp_path / "p"
    write(project, {"a.py": "x = 1\n"})
    secret = tmp_path / "notes.txt"
    secret.write_text("CANARY\n")
    node = Node(tmp_path / "n")
    prep = repo.prepare(node, project, ["true"])
    mid = node.create_mission("g", "software/python", "c", check=dict(prep["check"]))["mission_id"]
    co = tmp_path / "co"
    repo.checkout(node, mid, str(co))
    os.symlink(secret, co / "notes.txt")
    os.symlink(project / "a.py", co / "b.py")  # even a link to an ordinary file is not followed
    out = repo.diff(node, mid, str(co))
    assert "CANARY" not in out["patch"] and out["changed"] == []
    assert {s["file"] for s in out["skipped"]} == {"notes.txt", "b.py"}
