#!/usr/bin/env python3
"""Summarize an H4 run against the pre-registered thresholds in README.md."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def outcome(row: dict) -> str:
    obj = row.get("objection") or {}
    defective = row["variant"] == "defective"
    if not obj:
        return "defect accepted" if defective else "correct, no objection"
    if obj["kind"] == "written":
        return "caught (written)" if defective else "false alarm (written)"
    reproduced = obj.get("verification") == "reproduced"
    if defective:
        return "caught (reproduced)" if reproduced else "defect accepted (counterexample did not reproduce)"
    return "false alarm escalated (reproduced on correct work)" if reproduced else "false alarm auto-rejected"


def metrics(rows: list[dict]) -> dict:
    defective = [r for r in rows if r["variant"] == "defective"]
    correct = [r for r in rows if r["variant"] == "correct"]
    caught = sum(outcome(r).startswith("caught") for r in defective)
    accepted = sum(outcome(r).startswith("defect accepted") for r in defective)
    false_alarms = sum(bool(r.get("objection")) for r in correct)

    def needs_human(r):
        obj = r.get("objection") or {}
        if not obj:
            return False
        if obj["kind"] == "written":
            return True
        return r["variant"] == "correct" and obj.get("verification") == "reproduced"

    return {"sessions": len(rows), "defective": len(defective), "correct": len(correct),
            "caught": caught, "accepted": accepted, "false_alarms": false_alarms,
            "needs_human": sum(needs_human(r) for r in rows),
            "minutes": round(sum(r.get("seconds", 0) for r in rows) / 60, 1),
            "tokens": sum(sum((r.get("usage") or {}).values()) for r in rows)}


def verdict(t: dict, r: dict) -> str:
    if t["caught"] == 0 and t["false_alarms"] == 0 and r["caught"] == 0:
        return "inconclusive: neither condition caught anything"
    checks = {
        "1. caught not lower by more than 1": r["caught"] >= t["caught"] - 1,
        "2. objections needing a human at least halved": r["needs_human"] <= t["needs_human"] / 2,
        "3. defects accepted not higher": r["accepted"] <= t["accepted"],
    }
    lines = [f"- {name}: {'yes' if ok else 'NO'}" for name, ok in checks.items()]
    head = "SUPPORTED" if all(checks.values()) else "NOT SUPPORTED"
    return head + "\n" + "\n".join(lines)


def main(out: Path):
    rows = [json.loads(line) for line in (out / "rows.jsonl").read_text().splitlines() if line.strip()]
    errors = [r for r in rows if "error" in r]
    rows = [r for r in rows if "error" not in r]
    by = {reg: [r for r in rows if r["regime"] == reg] for reg in ("T", "R")}
    m = {reg: metrics(rs) for reg, rs in by.items()}
    lines = ["# H4 results", "",
             f"Run: `{out.name}`. Sessions: {len(rows)} completed, {len(errors)} errors. "
             "Thresholds were fixed in README.md before the run.", "",
             "| Metric | T (written) | R (runnable) |", "|---|---|---|"]
    labels = [("caught", "Defects caught (of defective)"), ("accepted", "Defects accepted"),
              ("false_alarms", "False alarms on correct work"), ("needs_human", "Objections needing a human"),
              ("minutes", "Reviewer minutes"), ("tokens", "Tokens")]
    for key, label in labels:
        suffix = {"caught": "defective", "accepted": "defective", "false_alarms": "correct"}.get(key)
        fmt = (lambda reg: f"{m[reg][key]} / {m[reg][suffix]}") if suffix else (lambda reg: str(m[reg][key]))
        lines.append(f"| {label} | {fmt('T')} | {fmt('R')} |")
    lines += ["", "## Verdict", "", verdict(m["T"], m["R"]), "",
              "With 6 + 6 sessions per condition, one session is about 17 percentage points: a pilot signal, not proof.",
              "", "## Per session", "", "| Task | Variant | T | R |", "|---|---|---|---|"]
    for task in sorted({r["task"] for r in rows}):
        for variant in ("defective", "correct"):
            cell = {reg: next((outcome(r) for r in by[reg] if r["task"] == task and r["variant"] == variant), "-")
                    for reg in ("T", "R")}
            lines.append(f"| {task} | {variant} | {cell['T']} | {cell['R']} |")
    if errors:
        lines += ["", "## Errors", ""] + [f"- {e['regime']} {e['task']} {e['variant']}: {e['error'][:200]}" for e in errors]
    stops = {}
    for r in rows:
        stops[r.get("stop")] = stops.get(r.get("stop"), 0) + 1
    lines += ["", f"Session endings: {stops}"]
    (out / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
