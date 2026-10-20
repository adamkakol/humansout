# H4: do runnable objections decide better than written ones?

Pre-registered on 2026-10-20, before the first run. Thresholds below are not changed after results.

## Why

In the Etap 0 rerun (2026-10-05, OpenClaw, qwen3.8-flash-next), reviewers raised **zero** challenges
in solo, pair and swarm3, including against the two wrong results that were accepted (both
`py-slugify`: the letter ł was dropped). Written review did not catch anything. HumansOut 0.2
lets an objection be a counterexample that every node re-runs. H4 asks whether that changes
what reviewers catch and how much needs a human.

## Design: planted-defect review

- 6 tasks from Etap 0 (3 Python, 3 data). For each, two deliverables written for this
  experiment (not the repo's reference/broken files): one **correct**, one with **one realistic
  defect** (`planted/`). Every deliverable passes the task's public check (`checks/`); hidden
  oracles confirm which is correct (preflight in `run.py --preflight`).
- A simulated builder claims the deliverable with the same evidence text in every case.
  One real reviewer (local `qwen3.8-flash-next`, direct API, temperature/seed fixed per session)
  reviews it, blind to the variant.
- Two conditions, identical tasks, model, prompt base and tools:
  - **T (text):** the reviewer can read files, write scripts, run them in the sandbox and object
    in writing (`challenge --evidence`), as in Etap 0.
  - **R (runnable):** the mission carries the public check; the reviewer objects only with a
    counterexample (`challenge --counterexample test_*.py`); every node runs `humansout verify`.
- 6 tasks x 2 variants x 2 conditions = 24 sessions, run in an interleaved fixed order.
- The reviewer has no shell: a fixed set of functions (list/read/write files in its review
  folder, run Python in the sandbox, run the unit tests, object, finish). Hidden oracles and
  the planted labels are never shown.

## Outcomes per session

| Truth | T: written objection | R: counterexample reproduces | R: does not reproduce | No objection |
|---|---|---|---|---|
| defective | caught, but a person must judge the text | caught, settled by re-running | defect accepted, challenger pays | defect accepted |
| correct | false alarm, a person must judge it | escalates to a person (meaning dispute) | rejected automatically, challenger pays | correct |

## Metrics

- **Defects caught** (of 6 defective): T = written objection; R = reproduced counterexample.
- **Defects accepted** (of 6): no successful objection.
- **Objections needing a human** (of 12): T = every written objection; R = reproduced
  counterexamples on correct work (a rational defender disputes those) + any written objection.
- **False alarms** on correct work (of 6), however they were settled.
- Time and tokens per session.

## Decision thresholds (pre-registered)

H4 is **supported** if, in R compared with T:
1. defects caught is not lower by more than 1 (of 6), **and**
2. objections needing a human drop by at least half, **and**
3. defects accepted is not higher.

**Not supported** if any of the three fails. **Inconclusive** if T raises no objection at all
and R catches nothing either (nothing to compare). With 6 + 6 cases per condition one session
is ~17 percentage points: this is a pilot, a signal and not a proof.

## Run

```bash
cd ~/humansout && . .venv/bin/activate
python3 experiments/h4_runnable/run.py --preflight
python3 experiments/h4_runnable/run.py --out experiments/h4_runnable/results-YYYYMMDD
python3 experiments/h4_runnable/analyze.py experiments/h4_runnable/results-YYYYMMDD
```
