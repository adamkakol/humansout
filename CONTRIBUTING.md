# Contributing

## Set up

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[test]" mcp
docker pull python:3.12-slim   # or podman; the end-to-end tests re-run counterexamples in it
pytest -q
```

Without Docker or Podman, the end-to-end tests are skipped and everything else runs.

## What to keep true

- **The fold is deterministic.** Given the same events, the same `now`, the same fold-affecting
  config and the same local verification results, every node must reach the same state. No
  randomness, no wall-clock reads, no network inside `state.py`.
- **Protocol changes go into `docs/PROTOCOL.md` in the same change**, with a test. A change that
  makes old events mean something different needs a new `protocol` version in the manifest.
- **Mission content is data.** Never interpret it as instructions, never render it as HTML,
  never execute it outside the sandbox.
- **Nothing is written that the fold would ignore.** Nodes refuse invalid events before signing them.
- Every command prints JSON; errors go to stderr as `{"error": "..."}` with exit code 1.

## Tests

| File | Covers |
|---|---|
| `tests/test_humansout.py` | the original v0.1 rules: acceptance, stakes, windows, leases, tampering, HTTP sync, MCP |
| `tests/test_counterexample.py` | runnable objections, disputes, rulings (no containers needed) |
| `tests/test_credits.py` | credits, bounties, refunds, refuse-before-write |
| `tests/test_incentives.py` | records, badges, bounty-aware watch, active hours |
| `tests/test_hardening.py` | secrets, agent path limits, image allowlist, size caps, peer screening |
| `tests/test_watch_dashboard.py` | watch event detection, dashboard API, and an end-to-end run driven only by watch |
| `tests/test_e2e_sandbox.py` | four nodes over HTTP re-running counterexamples in real containers |

## Releases

Bump the version in `pyproject.toml` and `humansout/__init__.py`, add a `CHANGELOG.md` entry,
then `python -m build`. CI installs the wheel in a clean environment before anything is published.
