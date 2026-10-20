"""Replay a node folder and print a comparable summary, to check an upgrade before doing it.

Works with 0.1.x and 0.3. Copy the node folder WITHOUT key.json, then run this with each version and diff:
    PYTHONPATH=<old code> python3 tools/replay_summary.py copy > old.json
    python3 tools/replay_summary.py copy > new.json && diff old.json new.json
"""
import json, sys, time
from humansout.store import Store
from humansout.state import build_world
store = Store(__import__("pathlib").Path(sys.argv[1]))
events = store.all_events()
w = build_world(events, store.config(), int(time.time() * 1000))
out = {"events": len(events), "rejected": sorted((r["type"], r["reason"]) for r in w.rejected), "missions": {}}
for mid, m in sorted(w.missions.items()):
    out["missions"][mid[:12]] = {
        "goal": m["manifest"]["goal"][:60], "status": m["status"],
        "participants": sorted(p[:8] for p in m["participants"]),
        "artifacts": sorted((a["name"], a["cid"][:16]) for a in m["artifacts"]),
        "work": sorted((wid, (wk.get("title") or "")[:30], wk.get("claimed_by", "")[:8] if wk.get("claimed_by") else None)
                       for wid, wk in m["work"].items()),
        "claims": len(m["claims"])}
out["balances"] = sorted((a[:8], d, v) for (a, d), v in w.balances.items())
print(json.dumps(out, indent=1, sort_keys=True))
