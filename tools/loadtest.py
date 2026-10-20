"""Load test: a simulated network of many agents, then the timings people would feel.

    python tools/loadtest.py --agents 50 --missions 300 --out /tmp/humansout-load

Agents write valid signed events (joins, work items, files, claims, counterexamples with verdicts,
disputes, rulings) into their own feeds; a hub holds everything. It reports: how long replaying all
events takes, one write, a fresh node's sync over HTTP, dashboard API calls, watch's work search,
and disk use. Nothing leaves this machine.
"""

from __future__ import annotations

import argparse
import json
import random
import resource
import shutil
import statistics
import sys
import time
import urllib.request
from pathlib import Path

from humansout import Node, watch
from humansout.net import serve
from humansout.state import build_world
from humansout.verify import verification_key


def timed(fn, repeat=1):
    runs = []
    out = None
    for _ in range(repeat):
        start = time.perf_counter()
        out = fn()
        runs.append(time.perf_counter() - start)
    return round(statistics.median(runs), 3), out


def generate(root: Path, n_agents: int, n_missions: int, seed: int) -> tuple[Node, list[Node], Node, dict]:
    rng = random.Random(seed)
    # the network's history ends about now: nodes ignore events from the future, and the dashboard uses the real clock
    now = [int(time.time() * 1000) - n_missions * 90_000 - 3_600_000]

    def clock():
        return now[0]

    def tick(seconds=1):
        now[0] += int(seconds * 1000)

    creator = Node(root / "creator", clock=clock)
    creator.store.set_config("start_credits", 100000)  # so some missions can carry bounties
    agents = [Node(root / f"agent{i:03d}", clock=clock) for i in range(n_agents)]
    test = root / "test_basic.py"
    test.write_text("import unittest\n")
    domains = [f"software/area{i}" for i in range(10)]

    def emit(node, type_, body):
        tick()
        return node.store.write_own(node.store.build_own(node.identity, type_, body, clock()))

    missions = []
    for m in range(n_missions):
        tick()
        runnable = m % 2 == 0
        check = {"command": ["python", "-m", "unittest"], "deliverable": "solution.py", "files": [str(test)]} if runnable else None
        bounty = ({"build": 3, "find": 1, "max_finds": 2, "expires": rng.randint(300, 7200)} if m % 5 == 0 else None)
        mid = creator.create_mission(f"Mission {m}", rng.choice(domains), "criteria", challenge_window=3600,
                                     dispute_window=600, check=check, bounty=bounty)["mission_id"]
        missions.append(mid)
    world = creator.world()
    verdicts = {}  # what a verifier re-running the counterexamples would have found
    for mid in missions:
        check = world.missions[mid]["check"]
        people = rng.sample(agents, min(10, n_agents))
        for a in people:
            emit(a, "join", {"mission_id": mid})
        works = []
        for a in people[:3]:
            works.append(emit(a, "work.create", {"mission_id": mid, "title": "part", "description": ""})["id"][:12])
        for a, wid in zip(people[3:5], works, strict=False):  # two of the three work items get taken
            emit(a, "work.claim", {"mission_id": mid, "work_id": wid, "lease_seconds": 3600})
        claims = []
        for a in people[:2]:
            data = f"solution {mid} {a.agent_id}".encode()
            cid = a.store.put_blob(data)
            emit(a, "artifact.publish", {"mission_id": mid, "cid": cid, "name": "solution.py", "size": len(data)})
            claim = emit(a, "claim_done", {"mission_id": mid, "target": "mission", "evidence": {"text": "done", "cids": [cid]}})
            claims.append((a, claim["id"], cid))
            if check:  # the claim's own check, as a verifier ran it: mostly passes, sometimes fails or is not run yet
                roll = rng.random()
                if roll < 0.9:
                    verdicts[verification_key(check, cid, [])] = {"result": "pass" if roll < 0.82 else "fail"}
        for author, claim_id, deliverable in claims:
            for b in [p for p in people if p is not author][:2]:
                if check:
                    data = f"test {claim_id} {b.agent_id}".encode()
                    files = [{"name": "test_edge.py", "cid": b.store.put_blob(data)}]
                    ce = emit(b, "challenge", {"target": claim_id, "kind": "counterexample", "counterexample": {"files": files},
                                              "evidence": {"text": "fails", "cids": []}})
                    result = rng.choice(["fail", "pass"])
                    verdicts[verification_key(check, deliverable, files)] = {"result": result}
                    if result == "fail" and rng.random() < 0.3:
                        emit(author, "dispute", {"target": ce["id"], "evidence": {"text": "not asked for", "cids": []}})
                        if rng.random() < 0.5:
                            emit(creator, "ruling", {"target": ce["id"], "decision": rng.choice(["upheld", "rejected"]),
                                                     "reason": "r"})
                else:
                    emit(b, "challenge", {"target": claim_id, "evidence": {"text": "wrong", "cids": []}})
        tick(30)
    hub = Node(root / "hub", clock=clock)
    hub.store.set_config("start_credits", 100000)
    for node in [creator, *agents]:
        hub.store.ingest(node.agent_id, node.store.load_feed(node.agent_id))
        for blob in node.store.blobs_dir.iterdir():
            hub.store.put_blob(blob.read_bytes())
    for key, value in verdicts.items():
        hub.store.save_verification(key, value)
    return hub, agents, creator, {"missions": missions, "clock": clock}


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}), timeout=60) as r:
        return r.read()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, default=50)
    ap.add_argument("--missions", type=int, default=300)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="/tmp/humansout-load")
    args = ap.parse_args(argv)
    root = Path(args.out)
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)

    gen_s, (hub, agents, creator, info) = timed(lambda: generate(root, args.agents, args.missions, args.seed))
    clock = info["clock"]
    events = hub.store.all_events()
    verified = {k: v.get("result") for k, v in hub.store.verifications().items()}
    fold_s, world = timed(lambda: build_world(events, hub.store.config(), clock(), verified), repeat=3)
    load_s, _ = timed(lambda: Node(root / "hub", clock=clock).store.all_events())
    world_s, _ = timed(lambda: hub.world(), repeat=3)
    write_s, _ = timed(lambda: hub.create_mission("one more", "software/area0", "c"))

    srv = serve(hub.store, "127.0.0.1", 0, background=True, agent_id=hub.agent_id)
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        fresh = Node(root / "fresh", clock=clock)
        sync_s, report = timed(lambda: fresh.sync([url]))
        busiest = max(world.missions, key=lambda m: len(world.missions[m]["claims"]))
        overview_s, _ = timed(lambda: get(url + "/api/overview"), repeat=3)
        mission_s, _ = timed(lambda: get(url + f"/api/mission/{busiest}"), repeat=3)
    finally:
        srv.shutdown()
    agent = agents[0]
    for node in [creator, *agents]:
        if node is not agent:
            agent.store.ingest(node.agent_id, node.store.load_feed(node.agent_id))
    find_s, work = timed(lambda: watch.find_work(agent.world(), agent.agent_id, clock(), watch.WatchConfig(),
                                                 watch.load_state(agent.home)))
    feeds_bytes = sum(f.stat().st_size for f in (root / "hub" / "feeds").iterdir())
    statuses = {}
    for item in world.items.values():
        statuses[f"{item['kind']}:{item['status']}"] = statuses.get(f"{item['kind']}:{item['status']}", 0) + 1
    out = {
        "agents": args.agents, "missions": args.missions + 1, "events": len(events), "rejected": len(world.rejected),
        "items": statuses,
        "seconds": {"generate": gen_s, "replay_all_events": fold_s, "load_feeds_from_disk": load_s,
                    "node_world": world_s, "one_write": write_s, "fresh_node_sync_http": sync_s,
                    "dashboard_overview": overview_s, "dashboard_busiest_mission": mission_s,
                    "watch_find_work": find_s},
        "fresh_sync": {"events": report["new_events"], "blobs": report["new_blobs"]},
        "watch_events_found": len(work),
        "feeds_bytes": feeds_bytes, "bytes_per_event": round(feeds_bytes / max(1, len(events))),
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024),
    }
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
