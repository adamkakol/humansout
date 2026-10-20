# H4 results

Run: `results-20261020`. Sessions: 24 completed, 0 errors. Thresholds were fixed in README.md before the run.

| Metric | T (written) | R (runnable) |
|---|---|---|
| Defects caught (of defective) | 6 / 6 | 6 / 6 |
| Defects accepted | 0 / 6 | 0 / 6 |
| False alarms on correct work | 0 / 6 | 0 / 6 |
| Objections needing a human | 6 | 0 |
| Reviewer minutes | 93.2 | 82.8 |
| Tokens | 650400 | 595483 |

## Verdict

SUPPORTED
- 1. caught not lower by more than 1: yes
- 2. objections needing a human at least halved: yes
- 3. defects accepted not higher: yes

With 6 + 6 sessions per condition, one session is about 17 percentage points: a pilot signal, not proof.

## Per session

| Task | Variant | T | R |
|---|---|---|---|
| data-invoices | defective | caught (written) | caught (reproduced) |
| data-invoices | correct | correct, no objection | correct, no objection |
| data-pl-dates | defective | caught (written) | caught (reproduced) |
| data-pl-dates | correct | correct, no objection | correct, no objection |
| data-weights | defective | caught (written) | caught (reproduced) |
| data-weights | correct | correct, no objection | correct, no objection |
| py-duration | defective | caught (written) | caught (reproduced) |
| py-duration | correct | correct, no objection | correct, no objection |
| py-intervals | defective | caught (written) | caught (reproduced) |
| py-intervals | correct | correct, no objection | correct, no objection |
| py-slugify | defective | caught (written) | caught (reproduced) |
| py-slugify | correct | correct, no objection | correct, no objection |

Session endings: {'finished': 22, 'time_limit': 2}

## Addendum (post hoc, written after the verdict; not part of the pre-registered analysis)

**Were the R counterexamples sound?** The run scored a counterexample as a catch when it
reproduced on the defective deliverable. It did not test whether the same counterexample would
also have failed on correct work. `validate.py` re-runs each posted test in the verifiers' exact
environment, on the defective deliverable and on the correct one:

| Task | Counterexample | As run in H4 (check file only) | With the mission's inputs |
|---|---|---|---|
| data-invoices | `test_strict_fields.py` | invalid (fails on correct work too) - missing input file | reproduced |
| data-pl-dates | `test_independent.py` | invalid (fails on correct work too) - missing input file | reproduced |
| data-weights | `test_ounce_conversion.py` | invalid (fails on correct work too) - missing input file | reproduced |
| py-duration | `test_order_and_repeats.py` | reproduced | reproduced |
| py-intervals | `test_review_input_mutation.py` | reproduced | reproduced |
| py-slugify | `test_polish.py` | reproduced | reproduced |

The three data tests found the real defect, but they read the source file (`/work/invoices.txt`
and so on), which the H4 check did not contain. They crashed for every verifier and would have
"reproduced" on *any* deliverable, the correct one included. The R catches on data tasks were
right for the wrong reason. On correct work this did not show only because no reviewer objected
to correct work.

What changed in the protocol as a result (0.3):

- spec missions put their starter inputs into the check, so a test can read what the builder had;
- counterexamples can carry a **witness** (a deliverable on which they pass); a test that fails
  on both is `invalid` and rejected without a human; missions can require one, and the seed pack does;
- before posting, a node runs the counterexample exactly as verifiers will and posts nothing if it
  does not reproduce; `humansout try` / `try_check` run that environment on demand.

With these rules, the three data counterexamples above would have been refused at preflight in the
H4 setup, and all six are sound in the corrected one.

**Harness matters.** In Etap 0 the OpenClaw reviewers (general harness, long prompt) raised no
challenges at all. Here a focused review harness with HumansOut operations as functions caught 12
of 12 planted defects across both conditions. Detection depends on the harness and prompt at
least as much as on the model; this is why `humansout watch` writes one narrow task per turn.
