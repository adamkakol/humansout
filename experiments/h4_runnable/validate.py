"""Were the R counterexamples sound? Re-run each posted test as verifiers would.

For every R session on defective work, take the counterexample the reviewer posted and run the
mission's check with it, on the defective deliverable and on the correct one (as a witness), in two
environments: as in the H4 run (the check's test file only) and with the mission's inputs added
(what spec missions do since 0.3). A sound counterexample fails on the defect and passes on the
correct work.

    python experiments/h4_runnable/validate.py results-20261020
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from humansout import Node
from humansout.missions import load_spec
from humansout.state import combine

HERE = Path(__file__).resolve().parent
SEED = HERE.parent.parent / "humansout" / "seed"
LABEL = {"fail": "reproduced", "pass": "not reproduced", "invalid": "invalid (fails on correct work too)",
         "flaky": "flaky"}


def mission(node: Node, task: str, with_inputs: bool) -> str:
    spec = load_spec(SEED / task / "mission.json")
    files = [str(HERE / "checks" / task / "test_public.py")]
    if with_inputs:
        files += [f for f in spec["check"]["files"] if Path(f).name != "test_public.py"]
    check = dict(spec["check"], files=files)
    check.pop("require_witness", None)
    return node.create_mission(f"{task} inputs={with_inputs}", spec["category"], spec["criteria"], check=check)["mission_id"]


def main(results: str) -> int:
    root = HERE / results
    node = Node(Path(tempfile.mkdtemp(prefix="h4-validate-")) / "node")
    rows = []
    for session in sorted(root.glob("R-*-defective")):
        task = session.name[2:-len("-defective")]
        call = next(c for c in json.loads((session / "calls.json").read_text())
                    if c["function"] == "object_with_counterexample")
        tests = [str(session / "review" / f) for f in call["args"]["files"]]
        deliverable = load_spec(SEED / task / "mission.json")["check"]["deliverable"]
        row = {"task": task, "test": ", ".join(call["args"]["files"])}
        for with_inputs in (False, True):
            mid = mission(node, task, with_inputs)
            on_claim = node.try_check(mid, str(HERE / "planted" / task / "defective" / deliverable), tests)
            on_witness = node.try_check(mid, str(HERE / "planted" / task / "correct" / deliverable), tests)
            verdict = combine(on_claim["result"], on_witness["result"], True)
            crashed = "FileNotFoundError" in on_claim["output"] + on_witness["output"]
            row["with inputs" if with_inputs else "as in H4"] = LABEL[verdict] + (" - missing input file" if crashed else "")
        rows.append(row)
    print("| Task | Counterexample | As run in H4 (check file only) | With the mission's inputs |")
    print("|---|---|---|---|")
    for r in rows:
        print(f"| {r['task']} | `{r['test']}` | {r['as in H4']} | {r['with inputs']} |")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "results-20261020"))
