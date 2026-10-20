"""Counterexamples with a witness: a crashing or always-failing test is rejected without a human.

Found by H4: tests that crash (a file missing from the check environment) "reproduced" on any
deliverable. A witness is a deliverable on which the check and the counterexample pass; verifiers
run both, so a test that fails everywhere is `invalid` and the challenger pays.
"""

import subprocess

import pytest

from humansout import HumansOutError, Node, sandbox
from humansout.state import combine

IMAGE = "python:3.12-slim"
BUGGY = "def slugify(s):\n    return s.lower().replace(' ', '-').replace('ł', '')\n"
FIXED = "def slugify(s):\n    return s.lower().replace(' ', '-').replace('ł', 'l')\n"


def test_combine_rules():
    assert combine(None, None, False) is None
    assert combine("fail", None, True) is None             # witness not run yet: do not guess
    assert combine("fail", "pass", True) == "fail"         # a real defect
    assert combine("fail", "fail", True) == "invalid"      # fails everywhere: a broken test
    assert combine("pass", "pass", True) == "pass"
    assert combine("fail", None, False) == "fail"          # no witness: the old rule


def engine_ready() -> bool:
    engine = sandbox.find_engine()
    return bool(engine) and subprocess.run([engine, "image", "inspect", IMAGE], capture_output=True).returncode == 0


needs_engine = pytest.mark.skipif(not engine_ready(), reason=f"needs docker or podman with {IMAGE}")


def scene(tmp_path, require_witness=False):
    creator, alice, bob = (Node(tmp_path / n) for n in ("creator", "alice", "bob"))
    base = tmp_path / "files"
    base.mkdir()
    test = base / "test_basic.py"
    test.write_text("import unittest\nfrom slugify import slugify\n\nclass T(unittest.TestCase):\n"
                    "    def test_basic(self):\n        self.assertEqual(slugify('A B'), 'a-b')\n")
    mid = creator.create_mission("slugify", "software/python", "ASCII slugs", challenge_window=100, dispute_window=2,
                                 check={"command": ["python", "-m", "unittest", "discover", "-s", "/work"],
                                        "deliverable": "slugify.py", "files": [str(test)], "image": IMAGE,
                                        "require_witness": require_witness})["mission_id"]
    share(creator, alice, bob)
    alice.join(mid)
    code = alice.home / "work" / "slugify.py"
    code.parent.mkdir(parents=True)
    code.write_text(BUGGY)
    cid = alice.publish_artifact(mid, str(code))["cid"]
    claim = alice.claim_done(mid, "basic test passes", cids=[cid])["claim"]
    share(creator, alice, bob)
    for src in (creator, alice):  # what sync does for files
        for blob in src.store.blobs_dir.iterdir():
            for dst in (creator, alice, bob):
                dst.store.put_blob(blob.read_bytes())
    work = bob.home / "work"
    work.mkdir(parents=True)
    (work / "fixed.py").write_text(FIXED)
    return creator, alice, bob, mid, claim, work


def share(*nodes):
    for src in nodes:
        for dst in nodes:
            if src is not dst:
                for author in src.store.feed_authors():
                    if author != dst.agent_id:
                        dst.store.ingest(author, src.store.load_feed(author))


def unit_test(path, body):
    path.write_text("import unittest\nfrom slugify import slugify\n\nclass T(unittest.TestCase):\n"
                    f"    def test_case(self):\n        {body}\n")
    return str(path)


@needs_engine
def test_real_counterexample_with_witness_reproduces(tmp_path):
    creator, alice, bob, mid, claim, work = scene(tmp_path)
    test = unit_test(work / "test_polish.py", "self.assertEqual(slugify('ł'), 'l')")
    out = bob.challenge(claim, "ł is dropped", counterexample=[test], witness=str(work / "fixed.py"))
    assert out["preflight"] == "reproduced" and "AssertionError" in out["on_claim"]


@needs_engine
def test_crashing_counterexample_is_refused_then_invalid(tmp_path):
    creator, alice, bob, mid, claim, work = scene(tmp_path)
    crash = unit_test(work / "test_crash.py", "open('/work/source.txt').read()")  # a file the check does not have
    before = bob.store.feed_len(bob.agent_id)
    with pytest.raises(HumansOutError, match="preflight: your counterexample is invalid"):
        bob.challenge(claim, "it is wrong", counterexample=[crash], witness=str(work / "fixed.py"))
    assert bob.store.feed_len(bob.agent_id) == before  # nothing was posted, nothing staked
    forced = bob.challenge(claim, "it is wrong", counterexample=[crash], witness=str(work / "fixed.py"), preflight=False)
    share(creator, alice, bob)
    for blob in bob.store.blobs_dir.iterdir():  # what sync does for files
        creator.store.put_blob(blob.read_bytes())
        alice.store.put_blob(blob.read_bytes())
    for node in (creator, alice):
        assert [r["counterexample"] for r in node.verify()["verified"]] == ["invalid"]
    import time
    time.sleep(2.5)  # the dispute window (2 s) ends: no human was needed
    item = creator.world().items[forced["challenge"]]
    assert item["status"] == "rejected" and item["reason"].startswith("invalid")


def test_missions_can_require_a_witness(tmp_path):
    creator, alice, bob = (Node(tmp_path / n) for n in ("creator", "alice", "bob"))
    t = tmp_path / "test_basic.py"
    t.write_text("import unittest\n")
    mid = creator.create_mission("g", "software/python", "c", check={"command": ["python", "-m", "unittest"],
                                                                   "deliverable": "a.py", "files": [str(t)],
                                                                   "require_witness": True})["mission_id"]
    share(creator, alice, bob)
    alice.join(mid)
    f = alice.home / "work" / "a.py"
    f.parent.mkdir(parents=True)
    f.write_text("x = 1\n")
    claim = alice.claim_done(mid, "done", cids=[alice.publish_artifact(mid, str(f))["cid"]])["claim"]
    share(creator, alice, bob)
    ce = tmp_path / "test_edge.py"
    ce.write_text("import unittest\n")
    with pytest.raises(HumansOutError, match="this mission requires a witness"):
        bob.challenge(claim, "wrong", counterexample=[str(ce)], preflight=False)


def test_runnable_missions_refuse_claims_without_a_deliverable(tmp_path):
    """Found with Claude Code (Haiku): a claim without its deliverable could not be re-run or broken,
    so a defective result would have been accepted by default."""
    creator, alice = Node(tmp_path / "creator"), Node(tmp_path / "alice")
    t = tmp_path / "test_basic.py"
    t.write_text("import unittest\n")
    mid = creator.create_mission("g", "software/python", "c", check={"command": ["python", "-m", "unittest"],
                                                                   "deliverable": "a.py", "files": [str(t)]})["mission_id"]
    share(creator, alice)
    alice.join(mid)
    with pytest.raises(HumansOutError, match=r"name your deliverable \(a.py\) as the first cid"):
        alice.claim_done(mid, "done, see a.py")
    f = alice.home / "work" / "a.py"
    f.parent.mkdir(parents=True)
    f.write_text("x = 1\n")
    assert alice.claim_done(mid, "done", cids=[alice.publish_artifact(mid, str(f))["cid"]])["claim"]


def test_mcp_submit_publishes_and_claims(tmp_path):
    from humansout.mcp_server import _tools
    creator, alice = Node(tmp_path / "creator"), Node(tmp_path / "alice", restrict_paths=True)
    t = tmp_path / "test_basic.py"
    t.write_text("import unittest\n")
    mid = creator.create_mission("g", "software/python", "c", check={"command": ["python", "-m", "unittest"],
                                                                   "deliverable": "a.py", "files": [str(t)]})["mission_id"]
    share(creator, alice)
    f = alice.home / "work" / "a.py"
    f.parent.mkdir(parents=True)
    f.write_text("x = 1\n")
    out = _tools(alice)["submit"]["fn"]({"mission": mid[:10], "file": str(f), "evidence": "x is 1"})
    claim = alice.world().items[out["claim"]]
    assert claim["evidence"]["cids"] == [out["published"]] and alice.agent_id in alice.world().missions[mid]["participants"]
