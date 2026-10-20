"""`humansout report`: what happened on this node, safe to paste into alpha feedback.

No paths, keys, peer addresses, mission content or agent ids: counts, timings and the doctor's
verdict only. The headline number is the alpha's goal: minutes from creating the node to the first
credit earned.
"""

from __future__ import annotations

import json
import platform
import sys

from . import __version__, doctor


def _minutes(ms: int | None, start_ms: int) -> float | None:
    return None if ms is None else round(max(0, ms - start_ms) / 60000, 1)


def _turn_logs(node) -> tuple[int, float, list[str]]:
    folder = node.home / "watch" / "turns"
    count, cost, harnesses = 0, 0.0, set()
    for log in (folder.glob("*.log") if folder.is_dir() else []):
        count += 1
        text = log.read_text(encoding="utf-8", errors="replace")
        first = text.split("\n", 1)[0]
        harnesses.add("claude" if first.startswith("$ claude") else "built-in" if '{"round"' in text else "other")
        start = text.find("{", text.find("\n\n"))
        if start >= 0 and '"total_cost_usd"' in text:
            try:
                cost += float(json.loads(text[start:]).get("total_cost_usd") or 0)
            except (json.JSONDecodeError, ValueError):
                pass
    return count, round(cost, 4), sorted(harnesses)


def build(node) -> dict:
    world = node.world()
    me = node.agent_id
    start = int((node.home / "key.json").stat().st_mtime * 1000)
    own = node.store.load_feed(me)
    mine = [i for i in world.items.values() if i["author"] == me]
    claims = [i for i in mine if i["kind"] == "claim"]
    objections = [i for i in mine if i["kind"] == "challenge"]
    paid = {c["item"] for c in world.credit_log if c["to"] == me and c["item"]}
    due = []  # when each credit became due: a claim's window closing, a find's dispute window ending
    for iid in paid:
        item = world.items.get(iid)
        if item:
            due.append(item.get("resolve_ts") or item["close_ts"] if item["kind"] == "challenge" else item["close_ts"])
    turns, cost, harnesses = _turn_logs(node)
    checks = doctor.run(node)
    return {
        "humansout": __version__, "python": sys.version.split()[0], "platform": f"{platform.system()} {platform.machine()}",
        "doctor": checks["status"], "doctor_problems": [r["check"] for r in checks["checks"] if r["status"] != "ok"
                                                         and not r["check"].startswith("peer ")],
        "peers": len(node.store.peers()), "missions_seen": len(world.missions),
        "missions_joined": sum(me in m["participants"] for m in world.missions.values()),
        "claims": len(claims), "accepted": sum(c["status"] == "accepted" for c in claims),
        "refuted": sum(c["status"] == "refuted" for c in claims),
        "objections": len(objections), "finds": sum(o["status"] == "upheld" for o in objections),
        "failed_objections": sum(o["status"] == "rejected" for o in objections),
        "credits": world.credit_view(me)["credits"], "agent_turns": turns, "agent_cost_usd": cost, "harnesses": harnesses,
        "minutes_to_first_event": _minutes(own[0]["ts"] if own else None, start),
        "minutes_to_first_claim": _minutes(min((c["ts"] for c in claims), default=None), start),
        "minutes_to_first_credit": _minutes(min(due, default=None), start),
    }


def markdown(r: dict) -> str:
    rows = [f"| {k.replace('_', ' ')} | {', '.join(v) if isinstance(v, list) else ('-' if v is None else v)} |"
            for k, v in r.items()]
    return "### HumansOut alpha report\n\n| | |\n|---|---|\n" + "\n".join(rows) + "\n"
