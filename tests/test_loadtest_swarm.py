"""The swarm load test runs end to end at a tiny size (a real hub process, simulated agents)."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("loadtest", ROOT / "tools" / "loadtest.py")
loadtest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loadtest)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads the hub's CPU time from /proc")
def test_swarm_mode_measures_full_and_scoped_sync(tmp_path, capsys):
    assert loadtest.main(["--swarm", "--agents", "24", "--missions", "4", "--rounds", "1", "--threads", "4",
                          "--modes", "full,scoped+etag", "--out", str(tmp_path / "swarm")]) == 0
    printed = capsys.readouterr().out
    out = json.loads(printed[:printed.rindex("}") + 1])
    full, scoped = out["configs"]
    assert [r["round"] for r in full["rounds"]] == ["warm-up", "round 1", "settle", "idle"]
    assert all(r["errors"] == 0 for c in out["configs"] for r in c["rounds"])
    idle = {c["mode"]: next(r for r in c["rounds"] if r["round"] == "idle") for c in out["configs"]}
    assert idle["scoped+etag"]["statuses"] == {"304": 24}  # nothing changed: one empty answer per agent
    assert idle["full"]["mb_down"] > 0 and idle["scoped+etag"]["mb_down"] == 0
    assert scoped["newcomer_first_sync"]["feeds"] < full["newcomer_first_sync"]["feeds"] == 25
    assert "| scoped+etag |" in printed
