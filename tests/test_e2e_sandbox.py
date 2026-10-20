"""End to end: four nodes over HTTP, and every node re-runs checks in a real container.

Skipped when no container engine with the check image is available.
"""

import json
import subprocess

import pytest

from humansout import HumansOutError, Node, sandbox
from humansout.net import serve

IMAGE = "python:3.12-slim"


def engine_ready() -> bool:
    engine = sandbox.find_engine()
    return bool(engine) and subprocess.run([engine, "image", "inspect", IMAGE], capture_output=True).returncode == 0


pytestmark = pytest.mark.skipif(not engine_ready(), reason=f"needs docker or podman with {IMAGE}")


class Clock:
    def __init__(self, start=1_800_000_000_000):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += int(seconds * 1000)


def unit_test(body: str) -> str:
    return ("import unittest\nfrom slugify import slugify\n\n"
            f"class T(unittest.TestCase):\n    def test_case(self):\n        {body}\n")


V1 = ("import unicodedata\n\n"
      "def slugify(text):\n"
      "    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode()\n"
      "    return '-'.join(text.lower().split())\n")
V2 = ("import unicodedata\n\n"
      "def slugify(text):\n"
      "    text = text.replace('ł', 'l').replace('Ł', 'L')\n"
      "    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode()\n"
      "    return '-'.join(text.lower().split())\n")


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return str(path)


def test_four_nodes_settle_disputes_by_rerunning(tmp_path):
    clock = Clock()
    names = ["creator", "alice", "bob", "carol"]
    nodes = {n: Node(tmp_path / n, clock=clock) for n in names}
    servers = {n: serve(nodes[n].store, "127.0.0.1", 0, background=True) for n in names}
    urls = {n: f"http://127.0.0.1:{servers[n].server_address[1]}" for n in names}

    def sync_all():
        for n in names:
            nodes[n].sync([urls[m] for m in names if m != n])

    files = tmp_path / "files"
    try:
        mid = nodes["creator"].create_mission(
            "Write slugify(text)", "software/python",
            "slugify returns lower-case ASCII words joined by '-'; all tests pass",
            challenge_window=100, dispute_window=5,
            check={"command": ["python", "-m", "unittest", "discover", "-s", "/work"], "deliverable": "slugify.py",
                   "files": [write(files / "creator" / "test_basic.py",
                                   unit_test("self.assertEqual(slugify('Hello World'), 'hello-world')"))],
                   "image": IMAGE, "timeout": 120})["mission_id"]
        sync_all()

        # alice builds v1; it passes the mission's own check
        nodes["alice"].join(mid)
        cid1 = nodes["alice"].publish_artifact(mid, write(files / "v1" / "slugify.py", V1))["cid"]
        c1 = nodes["alice"].claim_done(mid, "v1: basic test passes", cids=[cid1])["claim"]
        sync_all()
        assert nodes["bob"].verify(c1[:12])["check"] == "passes"

        # bob finds a counterexample; every node re-runs it on its own and gets the same answer
        clock.advance(1)
        bob_test = write(files / "bob" / "test_polish.py", unit_test("self.assertEqual(slugify('Łódź'), 'lodz')"))
        ce1 = nodes["bob"].challenge(c1, "Łódź becomes 'odz': the letter ł is dropped", counterexample=[bob_test])["challenge"]
        sync_all()
        for n in names:
            out = nodes[n].verify()
            if n == "bob":  # the challenger's preflight already ran it exactly as verifiers do
                assert out["verified"] == [] and nodes[n].world().item_view(c1)["verification_status"] == "failing"
                continue
            assert [r["counterexample"] for r in out["verified"]] == ["reproduced"], (n, out)
            assert out["verified"][0]["claim_signal"] == "failing"
            assert out["remaining"] == 0
        clock.advance(6)  # dispute window over: refuted long before the 100 s claim window ends
        for n in names:
            world = nodes[n].world()
            assert world.items[c1]["status"] == "refuted" and world.items[c1]["refuted_by"] == ce1
            assert world.balance(nodes["bob"].agent_id, "software/python") == 10 + 1 + 8

        # alice fixes it; bob's test no longer fails; carol's test expects the wrong thing
        cid2 = nodes["alice"].publish_artifact(mid, write(files / "v2" / "slugify.py", V2))["cid"]
        c2 = nodes["alice"].claim_done(mid, "v2: maps ł to l before normalising", cids=[cid2])["claim"]
        sync_all()
        clock.advance(1)
        with pytest.raises(HumansOutError, match="preflight: your counterexample is not_reproduced"):
            nodes["bob"].challenge(c2, "same Polish test against v2", counterexample=[bob_test])  # nothing posted
        carol_test = write(files / "carol" / "test_unicode.py", unit_test("self.assertEqual(slugify('Łódź'), 'łódź')"))
        ce3 = nodes["carol"].challenge(c2, "slugs should keep Polish letters", counterexample=[carol_test])["challenge"]
        sync_all()
        for n in names:
            got = {r["challenge"]: r["counterexample"] for r in nodes[n].verify()["verified"]}
            assert got == ({} if n == "carol" else {ce3: "reproduced"}), (n, got)  # carol's preflight already ran it

        # carol's test does fail, but it asks for something the mission never asked for: a question of meaning
        clock.advance(1)
        nodes["alice"].dispute(ce3, "The mission asks for lower-case ASCII; keeping ł contradicts it")
        sync_all()
        clock.advance(1)
        nodes["creator"].rule(ce3, "rejected", "ASCII slugs were asked for")
        sync_all()
        clock.advance(110)

        views = {n: nodes[n].mission_state(mid) for n in names}
        claims = {n: json.dumps(v["claims"], sort_keys=True) for n, v in views.items()}
        assert len(set(claims.values())) == 1, "every node must reach the same verdicts"
        state = views["creator"]
        assert state["status"] == "done"
        by_id = {c["id"]: c for c in state["claims"]}
        assert by_id[c1]["status"] == "refuted" and by_id[c2]["status"] == "accepted"
        ch = {c["id"]: c for c in by_id[c2]["challenges"]}
        assert list(ch) == [ce3]
        assert ch[ce3]["status"] == "rejected" and ch[ce3]["ruling"]["decision"] == "rejected"

        ranking = {r["agent"]: r for r in nodes["carol"].ranking("software/python")["software/python"]}
        alice, bob, carol = (ranking[nodes[x].agent_id] for x in ("alice", "bob", "carol"))
        assert (alice["points"], alice["built"], alice["refuted"]) == (9 + 1 + 5, 1, 1)
        assert (bob["points"], bob["found"], bob["failed_challenges"]) == (19, 1, 0)
        assert (carol["points"], carol["failed_challenges"]) == (9, 1)
    finally:
        for srv in servers.values():
            srv.shutdown()
