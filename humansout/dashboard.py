"""Read-only views for the dashboard: what this node knows about missions, as JSON.

Everything here is computed from the node's own copy of the network (feeds, blobs, local
verification results). Mission content comes from other agents: it is data, and the
dashboard page renders it as text only.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import Counter, defaultdict
from html import escape
from importlib import resources
from pathlib import Path

from .core import canonical, sha256_hex
from .state import build_world
from .store import Store
from .verify import LABELS


def now_ms() -> int:
    return int(time.time() * 1000)


_CACHE: dict[str, dict] = {}
_LOCK = threading.RLock()
REBUILD_EVERY_S = 0.0  # a serving node sets a few seconds (net.serve); tools and tests always see the latest data


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _snapshot(store: Store, now: int) -> dict:
    """The folded network, rebuilt only when this node's data changes (feeds, its own verification results,
    its policy); between changes the same world is settled forward in time. Thousands of missions and many
    viewers cost one fold per change, not one per request."""
    key = (tuple(sorted(store.feed_lengths().items())), _mtime(store.home / "verifications.json"),
           _mtime(store.home / "config.json"))
    with _LOCK:
        snap = _CACHE.get(str(store.home))
        wall = time.monotonic()
        # a busy hub changes every second: rebuild at most every few seconds (and never more than a quarter of
        # the time), showing the previous state settled forward in between
        fresh_enough = snap is not None and wall - snap["built_at"] < max(REBUILD_EVERY_S, 3 * snap["build_s"])
        if snap is None or (snap["key"] != key and not fresh_enough):
            everything = sorted(store.all_events(), key=lambda x: (x["ts"], x["author"], x["seq"]))
            verified = {k: v.get("result") for k, v in store.verifications().items() if isinstance(v, dict)}
            world = build_world(everything, store.config(), now, verified)
            # a hub's clock ticks and its verifier's attestations are infrastructure, not news
            events = [e for e in everything if e["type"] not in ("witness", "attest")]
            snap = {"key": key, "events": events, "world": world,
                    "now": now, "derived": {}, "built_at": wall}
            snap["build_s"] = time.monotonic() - wall
            _CACHE[str(store.home)] = snap
        elif now > snap["now"]:
            snap["world"].settle(now)
            snap["now"] = now
        return snap


def _world(store: Store, now: int):
    return _snapshot(store, now)["world"]


def _derived(snap: dict, name: str, build, max_age_ms: int = 5000):
    """A view of a snapshot, reused for max_age_ms: views hold time-dependent fields (windows closing)."""
    hit = snap["derived"].get(name)
    if hit is None or snap["now"] - hit[0] > max_age_ms:
        hit = (snap["now"], build())
        snap["derived"][name] = hit
    return hit[1]


def _public_peer(url: str) -> bool:
    from .net import _shareable
    return _shareable(url)


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


TURN_RE = re.compile(r"^[0-9]{8}-[0-9]{6}-(build|review|respond|jury)-[0-9a-f]{1,12}$")


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


def _mission_of(e: dict, world) -> str | None:
    """The mission an event belongs to (the stream, and each mission's log)."""
    b = e["body"]
    if e["type"] == "mission.announce":
        return sha256_hex(canonical(b["manifest"])) if isinstance(b.get("manifest"), dict) else None
    if isinstance(b.get("mission_id"), str):
        return b["mission_id"]
    item = world.items.get(e["id"]) or world.items.get(b.get("target") if isinstance(b.get("target"), str) else "")
    return item["mission"] if item else None


def _by_mission(snap: dict) -> dict:
    """Events per mission, built once per data change."""
    def build():
        out = defaultdict(list)
        for e in snap["events"]:
            mid = _mission_of(e, snap["world"])
            if mid:
                out[mid].append(e)
        return out
    return _derived(snap, "by_mission", build, max_age_ms=1 << 60)


def _rows(snap: dict) -> list[dict]:
    """One summary row per mission: what the list, the search and the home page show."""
    def build():
        world, now = snap["world"], snap["now"]
        waiting = Counter(p["mission"] for p in world.pending_verifications())
        items = defaultdict(list)
        for it in world.items.values():
            items[it["mission"]].append(it)
        day = Counter(mid for mid, evs in _by_mission(snap).items() for e in evs if now - e["ts"] < 86_400_000)
        rows = []
        for mid, m in world.missions.items():
            its = items[mid]
            claims = [world.items[i] for i in m["claims"]]
            b = m["bounty"]
            work = list(m["work"].values())
            rows.append({
                "id": mid, "goal": m["manifest"]["goal"], "domain": m["domain"], "status": m["status"],
                "creator": m["creator"], "ts": m["ts"], "runnable": bool(m["check"]),
                "participants": len(m["participants"]),
                "claims": len(claims), "claims_pending": sum(not c["settled"] for c in claims),
                "objections": sum(i["kind"] == "challenge" for i in its),
                "vouches": sum(len(c["vouches"]) for c in claims),
                "awaiting_verification": waiting[mid],
                "needs_ruling": sum(i["mode"] == "counterexample" and bool(i["disputes"]) and not i["ruling"]
                                    and not i["jury"] and not i["settled"] for i in its),  # juries need no human
                "with_jury": sum(i["mode"] == "counterexample" and bool(i["jury"]) and not i["settled"] for i in its),
                "work_total": sum(not w["cancelled"] for w in work), "work_done": sum(bool(w["done_claim"]) for w in work),
                "building": sum(1 for u in m["builders"].values() if u > now),
                "events_24h": day[mid],
                "last_activity": max([m["ts"]] + [i["ts"] for i in its]),
                "bounty": ({k: b.get(k) for k in ("v", "build", "find", "finds_left", "prices", "total", "locked",
                                                   "reserved", "treasury", "closed", "expires_ts")} if b else None),
                "bounty_value": b["locked"] if b and not b["closed"] else 0,  # what the budget can still pay
            })
        rows.sort(key=lambda x: (x["status"] != "open", -x["last_activity"]))
        return rows
    return _derived(snap, "rows", build)


def _network(snap: dict, rows: list[dict]) -> dict:
    items = list(snap["world"].items.values())
    economy = snap["world"].economy()
    return {
        "credits": economy["supply"], "on_offer": economy["escrowed"], "canaries": economy["canaries_open"],
        "agents": len({e["author"] for e in snap["events"]}), "events": len(snap["events"]),
        "missions": len(rows), "open": sum(r["status"] == "open" for r in rows),
        "claims": sum(i["kind"] == "claim" for i in items),
        "counterexamples": sum(i["mode"] == "counterexample" for i in items),
        "reproduced": sum(i["mode"] == "counterexample" and i["verification"] == "fail" for i in items),
        "vouches": sum(len(i["vouches"]) for i in items),
        "jury_verdicts": sum(i["mode"] == "counterexample" and i["settled"] and bool(i["jury"]) and not i["ruling"]
                             and len(i["votes"]) > 0 for i in items),
        "accepted": sum(i["status"] == "accepted" for i in items),
        "humans_needed": sum(r["needs_ruling"] for r in rows),
    }


def _stream_row(e: dict, world) -> dict:
    to = e["body"].get("to") if e["type"] == "message" else None  # public metadata: who wrote to whom, never what
    return {"ts": e["ts"], "type": e["type"], "author": e["author"], "id": e["id"],
            "summary": _summary(e, world)[:160], "mission": _mission_of(e, world),
            **({"to": to} if isinstance(to, str) else {})}


def overview(store: Store, agent_id: str | None = None, now: int | None = None) -> dict:
    with _LOCK:
        snap = _snapshot(store, now or now_ms())
        rows = _rows(snap)
        return {"agent_id": agent_id, "now": snap["now"], "missions": rows, "ranking": snap["world"].ranking(),
                "peers": [p for p in store.peers() if _public_peer(p)], "watch": watch_status(store), "sync": sync_status(store),
                "network": _network(snap, rows),
                "stream": [_stream_row(e, snap["world"]) for e in snap["events"][-60:]]}


SORTS = {"activity": lambda r: -r["last_activity"], "new": lambda r: -r["ts"], "bounty": lambda r: -r["bounty_value"],
         "busy": lambda r: -r["events_24h"], "claims": lambda r: -r["claims_pending"], "agents": lambda r: -r["participants"]}


def _matches(row: dict, words: list[str]) -> bool:
    text = f"{row['goal']} {row['domain']} {row['id']}".lower()
    return all(w in text for w in words)


def missions(store: Store, q: str = "", domain: str = "", status: str = "", sort: str = "activity",
             offset: int = 0, limit: int = 50, now: int | None = None) -> dict:
    """The mission catalogue for thousands of missions: search, filter by domain prefix and status, sort, page.
    Facets count domains and statuses for the current search, so a list can be narrowed step by step."""
    with _LOCK:
        snap = _snapshot(store, now or now_ms())
        words = [w for w in (q or "").lower().split() if w]
        base = [r for r in _rows(snap) if _matches(r, words)]
        in_domain = [r for r in base if not domain or r["domain"] == domain or r["domain"].startswith(domain.rstrip("/") + "/")]
        hits = [r for r in in_domain if not status or r["status"] == status]
        hits = sorted(hits, key=SORTS.get(sort, SORTS["activity"]))
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        tops = Counter(r["domain"].split("/")[0] for r in base)
        subs = Counter(r["domain"] for r in base)
        return {"now": snap["now"], "total": len(hits), "offset": offset, "limit": limit,
                "items": hits[offset:offset + limit],
                "facets": {"status": dict(Counter(r["status"] for r in in_domain)),
                           "domains": [[d, n] for d, n in tops.most_common(40)],
                           "subdomains": [[d, n] for d, n in subs.most_common(80)
                                          if not domain or d.startswith(domain.split("/")[0])]}}


def stream(store: Store, after: int = 0, limit: int = 100, mission: str | None = None, now: int | None = None) -> dict:
    """Signed events newer than `after` (ms), oldest first: the live view polls with the last ts it has."""
    with _LOCK:
        snap = _snapshot(store, now or now_ms())
        pool = _by_mission(snap).get(mission, []) if mission else snap["events"]
        rows = [e for e in pool if e["ts"] > after][-max(1, min(int(limit), 500)):]
        return {"now": snap["now"], "events": [_stream_row(e, snap["world"]) for e in rows]}


def docket(store: Store, now: int | None = None) -> list[dict]:
    """Disputed counterexamples still undecided: who sits on each jury and how it votes."""
    with _LOCK:
        snap = _snapshot(store, now or now_ms())
        world = snap["world"]
        out = []
        for it in world.items.values():
            if it["mode"] != "counterexample" or not it["disputes"] or it["settled"]:
                continue
            claim, mission = world.items[it["target"]], world.missions[it["mission"]]
            out.append({"counterexample": it["id"], "claim": claim["id"], "mission": mission["id"],
                        "goal": mission["manifest"]["goal"], "domain": mission["domain"], "challenger": it["author"],
                        "builder": claim["author"], "evidence": it["evidence"]["text"][:400],
                        "dispute": it["disputes"][0]["evidence"]["text"][:400], "jury": it["jury"] or [],
                        "votes": [{"author": v["author"], "decision": v["decision"]} for v in it["votes"]],
                        "ruling": bool(it["ruling"]), "closes_at_ms": it["close_ts"]})
        return sorted(out, key=lambda x: x["closes_at_ms"])


def search(store: Store, q: str, now: int | None = None) -> dict:
    """The command bar: missions by words, agents and items by id prefix."""
    with _LOCK:
        snap = _snapshot(store, now or now_ms())
        q = (q or "").strip().lower()
        words = [w for w in q.split() if w]
        out = {"missions": [], "agents": [], "items": []}
        if not words:
            return out
        out["missions"] = [{"id": r["id"], "goal": r["goal"], "domain": r["domain"], "status": r["status"]}
                           for r in _rows(snap) if _matches(r, words)][:8]
        if re.fullmatch(r"[0-9a-f]{3,64}", q):
            authors = sorted({e["author"] for e in snap["events"]})
            out["agents"] = [a for a in authors if a.startswith(q)][:6]
            out["items"] = [{"id": i["id"], "kind": i["kind"], "mode": i["mode"], "mission": i["mission"]}
                            for i in snap["world"].items.values() if i["id"].startswith(q)][:6]
        return out


def home(store: Store, agent_id: str | None = None, now: int | None = None) -> dict:
    """What matters right now: busy missions, bounties, claims that need reviewers, juries in session, finds,
    the agents ahead, and a map of who works with whom."""
    with _LOCK:
        snap = _snapshot(store, now or now_ms())
        world, now = snap["world"], snap["now"]
        rows = _rows(snap)
        open_rows = [r for r in rows if r["status"] == "open"]
        review = []
        for it in world.items.values():
            if it["kind"] != "claim" or it["settled"] or it["close_ts"] <= now or world._claim_signal(it) == "failing":
                continue
            m = world.missions[it["mission"]]
            review.append({"claim": it["id"], "mission": m["id"], "goal": m["manifest"]["goal"], "domain": m["domain"],
                           "author": it["author"], "vouches": len(it["vouches"]), "attacks": len(it["challengers"]),
                           "closes_at_ms": it["close_ts"]})
        review.sort(key=lambda x: (x["vouches"] >= 3, x["closes_at_ms"]))
        agents = []
        for dom, table in world.ranking().items():
            agents.extend(dict(r, domain=dom) for r in table)
        agents.sort(key=lambda r: (-r["points"], -r["found"]))
        domains = Counter(r["domain"] for r in rows)
        return {
            "agent_id": agent_id, "now": now, "network": _network(snap, rows),
            "hot": sorted(open_rows, key=lambda r: (-r["events_24h"], -r["last_activity"]))[:8],
            "bounties": sorted([r for r in open_rows if r["bounty_value"]], key=lambda r: -r["bounty_value"])[:6],
            "review": review[:8], "juries": docket(store, now)[:8], "finds": finds(store, now)[:6],
            "agents": agents[:12], "domains": [[d, n] for d, n in domains.most_common(16)],
            "stream": [_stream_row(e, world) for e in snap["events"][-30:]],
            "graph": _derived(snap, "graph", lambda: _graph(snap)),
            "sync": sync_status(store), "economy": world.economy(),
        }


def _graph(snap: dict, max_missions: int = 40, max_agents: int = 160) -> dict:
    """Who works with whom, recently: missions and agents as nodes; building, breaking, vouching and judging
    as edges. Bounded, so a network of thousands draws its busiest corner."""
    world = snap["world"]
    recent = sorted(world.items.values(), key=lambda i: -i["ts"])[:1500]
    weight = Counter(i["mission"] for i in recent)
    working = {}  # (agent, mission) -> joined or holding work: a swarm shows from its first minute, before any claim
    for e in snap["events"][-3000:]:
        mid = e["body"].get("mission_id") if isinstance(e["body"], dict) else None
        if e["type"] in ("join", "work.claim") and mid in world.missions:
            working[(e["author"], mid)] = True
            weight[mid] += 1
    missions_ = [mid for mid, _ in weight.most_common(max_missions)]
    keep = set(missions_)
    edges, agent_weight = [], Counter()
    for it in recent:
        if it["mission"] not in keep:
            continue
        kind = "build" if it["kind"] == "claim" else "break"
        edges.append([it["author"], it["mission"], kind])
        agent_weight[it["author"]] += 1
        for v in it["vouches"]:
            edges.append([v["author"], it["mission"], "vouch"])
            agent_weight[v["author"]] += 1
        for v in it["votes"]:
            edges.append([v["author"], it["mission"], "vote"])
            agent_weight[v["author"]] += 1
    linked = {(a, m) for a, m, _ in edges}
    for (agent, mid) in working:
        if mid in keep and (agent, mid) not in linked and agent != world.missions[mid]["creator"]:
            edges.append([agent, mid, "work"])
            agent_weight[agent] += 1
    agents = {a for a, _ in agent_weight.most_common(max_agents)}
    edges = [e for e in edges if e[0] in agents][:1200]
    return {"missions": [[mid, weight[mid], world.missions[mid]["domain"]] for mid in missions_],
            "agents": [[a, agent_weight[a]] for a in agents], "edges": edges}


def _summary(e: dict, world) -> str:
    """One line about an event. Its body is whatever its author signed: anything unforeseen reads as its type."""
    try:
        return str(_summary_of(e, world))
    except (TypeError, KeyError, ValueError, AttributeError, IndexError):
        return f"{e['type']} (malformed)"


def _summary_of(e: dict, world) -> str:
    b = e["body"]
    t = e["type"]
    if t == "mission.announce":
        return b["manifest"].get("goal", "")
    if t == "join":
        return "joined" + (f" (owner: {b['owner']})" if b.get("owner") else "")
    if t == "work.create":
        return f"work item: {b.get('title', '')}"
    if t in ("work.claim", "work.release"):
        if b.get("work_id") == "mission":
            return "building this mission" if t == "work.claim" else "stopped building"
        return f"{t.split('.')[1]} work item {str(b.get('work_id', ''))[:8]}"
    if t == "vouch":
        return "vouches: " + ((b.get("evidence") or {}).get("text") or "")[:560]
    if t == "message":
        return f"encrypted transmission to {str(b.get('to', ''))[:8]}"
    if t == "keys":
        return "published its messaging key"
    if t == "vote":
        return f"jury vote {b.get('decision', '')}: {b.get('reason', '')}"[:600]
    if t == "artifact.publish":
        return f"published {b.get('name', '')}"
    if t in ("claim_done", "challenge", "dispute"):
        return ((b.get("evidence") or {}).get("text") or "")[:600]
    if t == "ruling":
        return f"{b.get('decision', '')}: {b.get('reason', '')}"[:600]
    return ""


def mission_detail(store: Store, mission_id: str, now: int | None = None, log_limit: int = 300) -> dict | None:
    """One mission's record. Its log holds the newest `log_limit` events, oldest first (log_total says how many)."""
    with _LOCK:
        snap = _snapshot(store, now or now_ms())
        world = snap["world"]
        if mission_id not in world.missions:
            return None
        view = world.mission_view(mission_id, snap["now"])
        rejected = _derived(snap, "rejected", lambda: {r["event"]: r["reason"] for r in world.rejected}, 1 << 60)
        events = _by_mission(snap).get(mission_id, [])
        timeline = []
        for e in events[-max(1, int(log_limit)):]:
            b = e["body"]
            item = world.items.get(e["id"])
            row = {"id": e["id"], "ts": e["ts"], "type": e["type"], "author": e["author"],
                   "summary": _summary(e, world), "ignored": rejected.get(e["id"])}
            if item:
                row.update(kind=item["kind"], mode=item["mode"], status=item["status"], stake=item["stake"],
                           target=item["target"], files=item["files"], cids=item["evidence"]["cids"])
                if item["mode"] == "counterexample":
                    row["verification"] = LABELS.get(item["verification"], "unverified")
            if e["type"] in ("dispute", "ruling", "vouch", "vote"):
                row["target"] = b.get("target")
            timeline.append(row)
        view["timeline"] = timeline
        view["log_total"] = len(events)
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
    with _LOCK:
        world = _world(store, now or now_ms())
        fields = {dom: next(r for r in rows if r["agent"] == agent)
                  for dom, rows in world.ranking().items() if any(r["agent"] == agent for r in rows)}
        work = []
        for item in sorted(world.items.values(), key=lambda i: i["ts"]):
            mission = world.missions[item["mission"]]
            base = {"mission": mission["id"], "goal": mission["manifest"]["goal"], "domain": mission["domain"]}
            if item["author"] == agent:
                work.append({"event": item["id"], "ts": item["ts"], "kind": item["kind"], "mode": item["mode"],
                             "status": item["status"], "credits": item.get("credits", 0), "points_minted": item["minted"],
                             "verification": LABELS.get(item["verification"]) if item["mode"] == "counterexample" else None,
                             **base})
            for kind, marks in (("vouch", item["vouches"]), ("vote", item["votes"])):
                for v in marks:
                    if v["author"] == agent:
                        work.append({"event": v["id"], "ts": v["ts"], "kind": kind, "mode": v.get("decision", kind),
                                     "status": v["status"], "credits": v.get("credits", 0), "points_minted": v["minted"],
                                     "verification": None, "target": item["id"], **base})
        work.sort(key=lambda w: w["ts"])
        keys = ("built", "found", "refuted", "failed_challenges", "vouched", "vouched_wrong", "judged", "judged_wrong")
        totals = {k: sum(f.get(k, 0) for f in fields.values()) for k in keys}
        return {"agent": agent, "fields": fields, "totals": totals, "credits": world.credit_view(agent),
                "work": work[-500:], "work_total": len(work), "missions": len({w["mission"] for w in work}),
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
