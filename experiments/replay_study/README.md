# Replay study: would breakers have caught agent mistakes before merge?

Pre-registered on 2026-10-20, before data collection.

## Question

Take real pull requests written by coding agents that were merged and later **reverted**. If,
before the merge, independent breaker agents had tried to produce a failing test (a HumansOut
counterexample), how often would they have shown the defect?

## Phase 1: candidates (this folder, `collect.py`)

- Agents: GitHub apps `copilot-swe-agent`, `google-labs-jules`, `devin-ai-integration`,
  `claude`, `cursor`.
- For each agent, a sample of merged PRs created in 2026 in Python repositories, spread over
  months (the search API returns at most 1000 results per query).
- For each sampled PR, search the same repository for a merged PR whose body says
  `Reverts <repo>#<number>` or whose title is `Revert "<title>"`.
- Output: `candidates.jsonl` (every sampled PR, with its revert if any) and `summary.md`
  (revert rate per agent). Read-only GitHub API calls through the user's `gh` login.
- Exclusions applied in phase 2, not hidden in phase 1: tutorial repos (`skills-*`), forks,
  repos under 20 stars, reverts that state a non-correctness reason (product decision, wrong
  branch, duplicate).

## Phase 2: replay (not built yet)

For each kept candidate:
1. Check out the PR's base and head commits; install dependencies in a preparation step that
   has network access, then run everything else in the offline HumansOut sandbox.
2. Give 2 breaker agents (different models where possible) the PR description, the diff and
   the repository at the head commit. Each may object with a counterexample test.
3. Re-run the counterexample: it must **fail on the head** commit and **pass after the revert**
   (or on the later fix). Only then does it count as catching the real defect.

## Decision thresholds (pre-registered)

- **Promising:** breakers catch the real defect in **at least 15%** of kept candidates, with
  counterexamples that also pass after the revert in at least half of the catches.
- **Weak:** below **10%**. Between 10% and 15%: inconclusive; enlarge the sample.
- Also reported: false alarms (counterexamples that fail on the head and still fail after the
  revert, i.e. not about this PR) and the cost per candidate.
