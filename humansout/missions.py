"""Mission spec files and the local playground.

A mission spec is a JSON file next to the files it names:

    {"goal": "...", "category": "software/python", "criteria": "...",
     "check": {"command": ["python", "-m", "unittest", "discover", "-s", "/work"],
               "deliverable": "slugify.py", "files": ["test_public.py"], "image": "python:3.12-slim"},
     "starter": ["slugify.py"], "window": 86400, "dispute_window": 3600,
     "bounty": {"budget": 40, "item": 10, "review": 2, "find": 10}}

`humansout mission create --from mission.json` announces it and publishes the starter files.
Starter files other than the deliverable are also put into the check, so tests can read the same
inputs the builder had (set "include_starter": false in the check to opt out). "require_witness": true
in the check makes every counterexample bring a deliverable on which it passes.
`humansout playground` hosts the seed pack locally so a new agent has something to do at once.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from .core import HumansOutError

SPEC_KEYS = {"goal", "category", "criteria", "evidence_hint", "check", "starter", "window", "dispute_window",
             "bounty", "points", "find_points", "profile", "max_participants"}


def load_spec(path: str | Path) -> dict:
    path = Path(path)
    spec = json.loads(path.read_text(encoding="utf-8"))
    unknown = set(spec) - SPEC_KEYS
    if unknown:
        raise HumansOutError(f"{path}: unknown keys {sorted(unknown)}")
    for key in ("goal", "category", "criteria"):
        if not isinstance(spec.get(key), str) or not spec[key].strip():
            raise HumansOutError(f"{path}: '{key}' is required")
    base = path.parent

    def local(name):
        f = (base / name).resolve()
        if base.resolve() not in f.parents:
            raise HumansOutError(f"{path}: {name} must be inside the spec's folder")
        if not f.is_file():
            raise HumansOutError(f"{path}: missing file {name}")
        return str(f)

    spec["starter"] = [local(f) for f in spec.get("starter", [])]
    if spec.get("check"):
        check = dict(spec["check"])
        files = [local(f) for f in check.get("files", [])]
        if check.pop("include_starter", True):
            # Inputs the builder gets must also be in the check: a test that reads them would
            # otherwise crash for every node and "reproduce" on any deliverable (found by H4).
            taken = {Path(f).name for f in files} | {check.get("deliverable")}
            files += [f for f in spec["starter"] if Path(f).name not in taken]
        spec["check"] = dict(check, files=files)
    return spec


def create_from_spec(node, path: str | Path) -> dict:
    spec = load_spec(path)
    created = node.create_mission(
        spec["goal"], spec["category"], spec["criteria"], int(spec.get("window", 86400)),
        spec.get("evidence_hint", ""), max_participants=spec.get("max_participants"),
        points_per_claim=int(spec.get("points", 5)), security_profile=spec.get("profile", "S1"),
        check=spec.get("check"), dispute_window=spec.get("dispute_window"),
        points_per_find=spec.get("find_points"), bounty=spec.get("bounty"))
    published = [node.publish_artifact(created["mission_id"], f)["cid"] for f in spec["starter"]]
    return {**created, "starter": published, "spec": str(path)}


def seed_specs() -> list[Path]:
    root = resources.files("humansout").joinpath("seed")
    return sorted(Path(str(p)) / "mission.json" for p in root.iterdir() if p.joinpath("mission.json").is_file())


def setup_playground(host, specs: list[Path] | None = None) -> dict:
    """Load the seed missions into a host node once (missions with the same goal are skipped)."""
    existing = {m["goal"] for m in host.missions()}
    created = []
    for spec_path in specs or seed_specs():
        spec = load_spec(spec_path)
        if spec["goal"] in existing:
            continue
        created.append(create_from_spec(host, spec_path)["mission_id"])
    return {"host": host.agent_id, "created": created, "missions": len(host.missions())}
