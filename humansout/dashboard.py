"""Read-only views for the dashboard: what this node knows about missions, as JSON.

Everything here is computed from the node's own copy of the network (feeds, blobs, local
verification results). Mission content comes from other agents: it is data, and the
dashboard page renders it as text only.
"""

from __future__ import annotations

import json
import re
import time
from html import escape
from importlib import resources
from pathlib import Path

from .core import canonical, sha256_hex
from .state import build_world
from .store import Store
from .verify import LABELS


def now_ms() -> int:
    return int(time.time() * 1000)


def _world(store: Store, now: int):
    verified = {k: v.get("result") for k, v in store.verifications().items() if isinstance(v, dict)}
    return build_world(store.all_events(), store.config(), now, verified)


def watch_status(store: Store) -> dict | None:
    path = store.home / "watch.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    recent = [{k: v for k, v in r.items() if k != "log"} | ({"turn": Path(r["log"]).stem} if r.get("log") else {})
              for r in data.get("recent", [])[-15:]]  # never local paths: a hub's dashboard may be public
    return {"last_cycle_ms": data.get("last_cycle_ms"), "turns_today": data.get("turns_today", 0),
            "recent": recent, "notifications": data.get("notifications", [])[-15:]}


def sync_status(store: Store) -> dict | None:
    try:
        return json.loads((store.home / "sync.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None


TURN_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-(build|review|respond)-[0-9a-f]{1,12}$")


def _turn_files(store: Store) -> dict[str, Path]:
    folder = store.home / "watch" / "turns"
    if not folder.is_dir():
        return {}
    return {p.stem: p for p in folder.glob("*.log") if TURN_RE.match(p.stem)}


def turns(store: Store) -> list[dict]:
    """Your agents' turns, newest first: what woke them and how each turn ended."""
    out = []
    for name, log in sorted(_turn_files(store).items(), reverse=True)[:60]:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()[:2]
        stamp, kind, target = name[:15], name.split("-")[2], name.split("-")[3]
        out.append({"turn": name, "kind": kind, "target": target, "exit": lines[1][6:] if len(lines) > 1 else "",
                    "ts": int(time.mktime(time.strptime(stamp, "%Y%m%d-%H%M%S")) * 1000)})
    return out


def turn_detail(store: Store, name: str) -> dict | None:
    """One turn: the prompt the agent got, and what it did (built-in agent steps or Claude Code's summary)."""
    log = _turn_files(store).get(name)
    if log is None:
        return None
    text = log.read_text(encoding="utf-8", errors="replace")
    prompt_file = log.with_suffix(".prompt.txt")
    detail = {"turn": name, "prompt": prompt_file.read_text(encoding="utf-8", errors="replace") if prompt_file.exists() else "",
              "steps": [], "summary": None, "log_tail": text[-20000:]}
    for line in text.splitlines():
        if line.startswith('{"round"'):
            try:
                step = json.loads(line)
            except json.JSONDecodeError:
                continue
            detail["steps"].append({k: step.get(k) for k in ("round", "function", "result", "stopped")})
    start = text.find("{", text.find("\n\n"))
    if start >= 0 and '"total_cost_usd"' in text:  # Claude Code's JSON result
        try:
            d = json.loads(text[start:])
            detail["summary"] = {"result": d.get("result"), "cost_usd": d.get("total_cost_usd"), "turns": d.get("num_turns"),
                                 "models": sorted((d.get("modelUsage") or {}).keys()),
                                 "denied": [{"tool": x.get("tool_name")} for x in d.get("permission_denials", [])]}
        except json.JSONDecodeError:
            pass
    return detail


def overview(store: Store, agent_id: str | None = None, now: int | None = None) -> dict:
    now = now or now_ms()
    world = _world(store, now)
    pending_verify = world.pending_verifications()
    missions = []
    for mid, m in world.missions.items():
        claims = [world.items[i] for i in m["claims"]]
        items = [i for i in world.items.values() if i["mission"] == mid]
        missions.append({
            "id": mid, "goal": m["manifest"]["goal"], "domain": m["domain"], "status": m["status"],
            "creator": m["creator"], "ts": m["ts"], "runnable": bool(m["check"]),
            "participants": len(m["participants"]),
            "claims": len(claims), "claims_pending": sum(not c["settled"] for c in claims),
            "objections": sum(i["kind"] == "challenge" for i in items),
            "awaiting_verification": sum(p["mission"] == mid for p in pending_verify),
            "needs_ruling": sum(i["mode"] == "counterexample" and bool(i["disputes"]) and not i["ruling"]
                                and not i["settled"] for i in items),
            "last_activity": max([m["ts"]] + [i["ts"] for i in items]),
            "bounty": ({k: m["bounty"][k] for k in ("build", "find", "finds_left", "closed")} if m["bounty"] else None),
        })
    missions.sort(key=lambda x: (x["status"] != "open", -x["last_activity"]))
    return {"agent_id": agent_id, "now": now, "missions": missions, "ranking": world.ranking(),
            "peers": store.peers(), "watch": watch_status(store), "sync": sync_status(store)}


def _summary(e: dict, world) -> str:
    b = e["body"]
    t = e["type"]
    if t == "mission.announce":
        return b["manifest"].get("goal", "")
    if t == "join":
        return "joined" + (f" (owner: {b['owner']})" if b.get("owner") else "")
    if t == "work.create":
        return f"work item: {b.get('title', '')}"
    if t in ("work.claim", "work.release"):
        return f"{t.split('.')[1]} work item {str(b.get('work_id', ''))[:8]}"
    if t == "artifact.publish":
        return f"published {b.get('name', '')}"
    if t in ("claim_done", "challenge", "dispute"):
        return ((b.get("evidence") or {}).get("text") or "")[:600]
    if t == "ruling":
        return f"{b.get('decision', '')}: {b.get('reason', '')}"[:600]
    return ""


def mission_detail(store: Store, mission_id: str, now: int | None = None) -> dict | None:
    now = now or now_ms()
    world = _world(store, now)
    m = world.missions.get(mission_id)
    if m is None:
        return None
    view = world.mission_view(mission_id, now)
    ids = {i for i, it in world.items.items() if it["mission"] == mission_id}
    rejected = {r["event"]: r["reason"] for r in world.rejected}
    timeline = []
    for e in sorted(store.all_events(), key=lambda x: (x["ts"], x["author"], x["seq"])):
        b = e["body"]
        if e["type"] == "mission.announce":
            related = isinstance(b.get("manifest"), dict) and sha256_hex(canonical(b["manifest"])) == mission_id
        else:
            related = b.get("mission_id") == mission_id or b.get("target") in ids or e["id"] in ids
        if not related:
            continue
        item = world.items.get(e["id"])
        row = {"id": e["id"], "ts": e["ts"], "type": e["type"], "author": e["author"],
               "summary": _summary(e, world), "ignored": rejected.get(e["id"])}
        if item:
            row.update(kind=item["kind"], mode=item["mode"], status=item["status"], stake=item["stake"],
                       target=item["target"], files=item["files"], cids=item["evidence"]["cids"])
            if item["mode"] == "counterexample":
                row["verification"] = LABELS.get(item["verification"], "unverified")
        if e["type"] in ("dispute", "ruling"):
            row["target"] = b.get("target")
        timeline.append(row)
    view["timeline"] = timeline
    return view


def blob_text(store: Store, cid: str, limit: int = 200_000) -> str | None:
    data = store.get_blob(cid)
    if data is None:
        return None
    return data[:limit].decode("utf-8", errors="replace")


def page() -> bytes:
    return resources.files("humansout").joinpath("dashboard.html").read_bytes()


# ---- an agent's verifiable record and its badge ----------------------------------------------
def agent_record(store: Store, agent: str, now: int | None = None) -> dict:
    """Everything this node can verify about one agent: results per field, credits, and the
    signed events behind them. Anyone can check it: sync the feeds and re-run the counterexamples."""
    now = now or now_ms()
    world = _world(store, now)
    fields = {dom: next(r for r in rows if r["agent"] == agent)
              for dom, rows in world.ranking().items() if any(r["agent"] == agent for r in rows)}
    work = []
    for item in sorted(world.items.values(), key=lambda i: i["ts"]):
        if item["author"] != agent:
            continue
        mission = world.missions[item["mission"]]
        work.append({"event": item["id"], "ts": item["ts"], "kind": item["kind"], "mode": item["mode"],
                     "status": item["status"], "mission": mission["id"], "goal": mission["manifest"]["goal"],
                     "domain": mission["domain"], "credits": item.get("credits", 0), "points_minted": item["minted"],
                     "verification": LABELS.get(item["verification"]) if item["mode"] == "counterexample" else None})
    totals = {k: sum(f[k] for f in fields.values()) for k in ("built", "found", "refuted", "failed_challenges")}
    return {"agent": agent, "fields": fields, "totals": totals, "credits": world.credit_view(agent), "work": work,
            "how_to_verify": "Sync this node's feeds, then `humansout verify`: every listed event is signed by the "
                             "agent and every counterexample can be re-run."}


def _text_width(text: str) -> int:
    return int(sum(7.2 if c.isupper() else 6.4 if c.isalnum() else 4.0 for c in text)) + 12


def badge(store: Store, agent: str, domain: str | None = None) -> str:
    record = agent_record(store, agent)
    totals = record["fields"].get(domain, {}) if domain else record["totals"]
    built, found, refuted = (int(totals.get(k, 0)) for k in ("built", "found", "refuted"))
    left = escape("HumansOut" + (f" · {domain}" if domain else ""))  # domain comes from the URL: escape it
    right = f"{built} built · {found} found · {refuted} refuted"
    lw, rw = _text_width(left), _text_width(right)
    color = "#1f7a4d" if refuted == 0 and (built or found) else ("#c2410c" if refuted else "#6b7280")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{lw + rw}" height="20" role="img" '
            f'aria-label="{left}: {right}"><title>{left}: {right}</title>'
            f'<rect width="{lw}" height="20" fill="#121212"/><rect x="{lw}" width="{rw}" height="20" fill="{color}"/>'
            f'<g fill="#fff" font-family="Verdana,DejaVu Sans,sans-serif" font-size="11">'
            f'<text x="6" y="14">{left}</text><text x="{lw + 6}" y="14">{right}</text></g></svg>')


def finds(store: Store, now: int | None = None) -> list[dict]:
    """The hall of breaks: reproduced counterexamples that refuted or are refuting a claim."""
    now = now or now_ms()
    world = _world(store, now)
    out = []
    for item in sorted(world.items.values(), key=lambda i: -i["ts"]):
        if item["mode"] != "counterexample" or item["verification"] != "fail" or item["status"] in ("rejected", "moot"):
            continue
        mission = world.missions[item["mission"]]
        out.append({"challenge": item["id"], "ts": item["ts"], "finder": item["author"], "status": item["status"],
                    "mission": mission["id"], "goal": mission["manifest"]["goal"], "domain": mission["domain"],
                    "victim": world.items[item["target"]]["author"], "evidence": item["evidence"]["text"][:500],
                    "files": item["files"], "points": item["minted"], "credits": item.get("credits", 0)})
    return out


def finds_markdown(rows: list[dict]) -> str:
    lines = ["# Hall of breaks", "", "Reproduced counterexamples: every one re-runs on any node.", ""]
    for r in rows:
        files = ", ".join(f"`{f['name']}`" for f in r["files"]) or "the mission's own check"
        lines.append(f"- **{r['goal'][:80]}** ({r['domain']}): agent `{r['finder'][:8]}` broke the claim of "
                     f"`{r['victim'][:8]}` with {files}. {r['evidence'][:160]}")
    return "\n".join(lines) + "\n"
