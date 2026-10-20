"""Mission spec files, the seed pack and the playground."""

import json
import subprocess
from pathlib import Path

import pytest

from humansout import HumansOutError, Node, sandbox
from humansout.cli import main
from humansout.missions import create_from_spec, load_spec, seed_specs, setup_playground

ROOT = Path(__file__).resolve().parent.parent
PLANTED = ROOT / "experiments" / "h4_runnable" / "planted"


def test_seed_pack_is_complete():
    specs = seed_specs()
    assert [p.parent.name for p in specs] == ["data-invoices", "data-pl-dates", "data-weights",
                                              "py-duration", "py-intervals", "py-slugify"]
    for path in specs:
        spec = load_spec(path)
        assert spec["check"]["files"] and spec["starter"]
        assert "hidden" not in spec["criteria"].lower()  # seed missions are judged by counterexamples


def test_spec_files_stay_inside_their_folder(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    folder = tmp_path / "m"
    folder.mkdir()
    (folder / "mission.json").write_text(json.dumps({"goal": "g", "category": "c", "criteria": "x",
                                                     "starter": ["../secret.txt"]}))
    with pytest.raises(HumansOutError, match="must be inside the spec's folder"):
        load_spec(folder / "mission.json")


def test_cli_mission_from_spec(tmp_path, capsys):
    spec = next(p for p in seed_specs() if p.parent.name == "py-slugify")
    assert main(["--home", str(tmp_path / "n"), "mission", "create", "--from", str(spec)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["runnable_check"] and len(out["starter"]) == 2
    assert main(["--home", str(tmp_path / "n"), "mission", "create", "--goal", "only a goal"]) == 1
    assert "needs --goal, --category and --criteria" in capsys.readouterr().err


def test_playground_is_idempotent(tmp_path):
    host = Node(tmp_path / "host")
    first = setup_playground(host)
    assert len(first["created"]) == 6
    assert setup_playground(host)["created"] == []


def engine_ready() -> bool:
    engine = sandbox.find_engine()
    return bool(engine) and subprocess.run([engine, "image", "inspect", "python:3.12-slim"],
                                           capture_output=True).returncode == 0


@pytest.mark.skipif(not engine_ready() or not PLANTED.is_dir(), reason="needs a container engine and the repo checkout")
def test_seed_checks_pass_on_correct_work(tmp_path):
    host, agent = Node(tmp_path / "host"), Node(tmp_path / "agent")
    for spec_path in seed_specs():
        task = spec_path.parent.name
        mid = create_from_spec(host, spec_path)["mission_id"]
        deliverable = load_spec(spec_path)["check"]["deliverable"]
        for author in host.store.feed_authors():
            agent.store.ingest(author, host.store.load_feed(author))
        for blob in host.store.blobs_dir.iterdir():
            agent.store.put_blob(blob.read_bytes())
        agent.join(mid)
        work = agent.home / "work" / task
        work.mkdir(parents=True)
        (work / deliverable).write_bytes((PLANTED / task / "correct" / deliverable).read_bytes())
        cid = agent.publish_artifact(mid, str(work / deliverable))["cid"]
        claim = agent.claim_done(mid, "correct deliverable", cids=[cid])["claim"]
        assert agent.verify(claim)["check"] == "passes", task


def test_spec_checks_include_the_starter_inputs(tmp_path):
    specs = {p.parent.name: load_spec(p) for p in seed_specs()}
    names = {task: sorted(Path(f).name for f in spec["check"]["files"]) for task, spec in specs.items()}
    assert names["data-invoices"] == ["invoices.txt", "test_public.py"]  # the input travels with the check
    assert names["py-slugify"] == ["test_public.py", "test_visible.py"]  # the starter slugify.py is the deliverable
    assert all(spec["check"]["require_witness"] for spec in specs.values())
    folder = tmp_path / "m"
    folder.mkdir()
    for name in ("input.txt", "test_x.py"):
        (folder / name).write_text("x")
    (folder / "mission.json").write_text(json.dumps({
        "goal": "g", "category": "data/x", "criteria": "c", "starter": ["input.txt"],
        "check": {"command": ["true"], "deliverable": "out.csv", "files": ["test_x.py"], "include_starter": False}}))
    spec = load_spec(folder / "mission.json")
    assert [Path(f).name for f in spec["check"]["files"]] == ["test_x.py"] and "include_starter" not in spec["check"]


H4_TEST = '''import csv, re, unittest

class T(unittest.TestCase):
    def test_total_matches_the_source(self):
        with open('/work/invoices.txt', encoding='utf-8') as f:
            doc = f.read().split('Invoice number: INV-2024-006')[1].split('===')[0]
        total = re.search(r'Total to pay: PLN ([0-9,]+[.][0-9]{2})', doc).group(1).replace(',', '')
        with open('/work/invoices.csv', encoding='utf-8') as f:
            rows = {r['invoice_no']: r['total'] for r in csv.DictReader(f)}
        self.assertEqual(rows['INV-2024-006'], total)
'''


@pytest.mark.skipif(not engine_ready() or not PLANTED.is_dir(), reason="needs a container engine and the repo checkout")
def test_counterexamples_can_read_the_mission_inputs(tmp_path):
    """The H4 failure: a sound test that reads the input crashed for every node. Now it separates
    the defective deliverable from the correct one, exactly as verifiers will run it."""
    host = Node(tmp_path / "host")
    mid = create_from_spec(host, next(p for p in seed_specs() if p.parent.name == "data-invoices"))["mission_id"]
    work = host.home / "work"
    work.mkdir(parents=True)
    test = work / "test_totals.py"
    test.write_text(H4_TEST)
    planted = PLANTED / "data-invoices"
    assert host.try_check(mid, str(planted / "correct" / "invoices.csv"), [str(test)])["result"] == "pass"
    bad = host.try_check(mid, str(planted / "defective" / "invoices.csv"), [str(test)])
    assert bad["result"] == "fail" and "AssertionError" in bad["output"] and "FileNotFoundError" not in bad["output"]
    assert bad["environment"] == ["invoices.csv", "test_public.py", "invoices.txt", "test_totals.py"]


def test_playground_never_announces_a_port_it_does_not_own(tmp_path, capsys):
    from humansout.net import serve
    busy = serve(Node(tmp_path / "other").store, "127.0.0.1", 0, background=True)
    try:
        port = busy.server_address[1]
        assert main(["--home", str(tmp_path / "me"), "playground", "--port", str(port)]) == 1
        captured = capsys.readouterr()
        assert "already in use" in captured.err and "dashboard" not in captured.out
    finally:
        busy.shutdown()
