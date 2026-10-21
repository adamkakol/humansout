"""Load test: a simulated network of many agents, then the timings people would feel.

    python tools/loadtest.py --agents 50 --missions 300 --out /tmp/humansout-load

Agents write valid signed events (joins, work items, files, claims, counterexamples with verdicts,
disputes, rulings) into their own feeds; a hub holds everything. It reports: how long replaying all
events takes, one write, a fresh node's sync over HTTP, dashboard API calls, watch's work search,
and disk use. Nothing leaves this machine.

    python tools/loadtest.py --swarm --agents 1000 --missions 100 --out /tmp/humansout-swarm [--legacy-ref f664d8f]

Swarm mode measures the network layer: about a thousand agents syncing with one hub, once per round
(a round stands for one 2-minute polling interval), with full or mission-scoped sync, with or without
ETags. The hub is a real `humansout` process on localhost; each agent's own feed and files are real
(they are pushed and verified by the hub), while what an agent pulls is only counted, so a thousand
agents fit on one machine. Between rounds some agents post (a work item, sometimes a file). Reported
per round: requests, bytes, the hub's CPU time, and what a CDN caching immutable responses would leave
for the hub. --legacy-ref also runs the client and hub of an older commit (the protocol before this work).
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import random
import resource
import shutil
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from humansout import Node, watch
from humansout.core import Identity, cid_for
from humansout.net import serve
from humansout.state import build_world
from humansout.store import DEFAULT_CONFIG, Store
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


# ---- swarm mode: the network layer under a thousand agents ---------------------------------------

HUB_SCRIPT = """
import sys
sys.path.insert(0, sys.argv[3]) if sys.argv[3] else None
from humansout.store import Store
from humansout.net import serve
serve(Store(sys.argv[1]), "127.0.0.1", 0, on_ready=lambda s: open(sys.argv[2], "w").write(str(s.server_address[1])))
"""


class SimStore(Store):
    """An agent's store in the swarm: its own feed and files are real (they get pushed), what it pulls is only
    counted (feed lengths and blob ids), and its settings live in memory."""

    def __init__(self, home, own, peer, known, blobs):
        super().__init__(home)
        self.own, self.peer, self.known, self.blobs = own, peer, dict(known), set(blobs)
        self.cfg = dict(DEFAULT_CONFIG)
        self.cache = {}

    def config(self):
        return dict(self.cfg)

    def set_config(self, key, value):
        self.cfg[key] = value

    def peers(self):
        return [self.peer]

    def add_peer(self, url, learned=False):
        return False

    def feed_len(self, author):
        return super().feed_len(author) if author == self.own else self.known.get(author, 0)

    def ingest(self, author, events):
        if author == self.own:
            return 0
        have = self.known.get(author, 0)
        n = sum(1 for e in events if isinstance(e, dict) and isinstance(e.get("seq"), int) and e["seq"] >= have)
        self.known[author] = have + n
        return n

    def has_blob(self, cid):
        return cid in self.blobs or super().has_blob(cid)

    def put_blob(self, data):
        cid = cid_for(data)
        self.blobs.add(cid)
        return cid

    def netcache(self):
        return self.cache

    def save_netcache(self, data):
        self.cache = data

    def referenced_cids(self):
        return set()


class SimNode:
    def __init__(self, store, identity, world, clock):
        self.store, self.identity, self.world_ref, self.clock = store, identity, world, clock
        self.agent_id, self.home = identity.public_hex, store.home

    def world(self):
        return self.world_ref[0]


class Meter:
    """Counts what the clients send and receive (bodies; headers add a few hundred bytes per request)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.requests, self.statuses, self.down, self.up = Counter(), Counter(), 0, 0
        self.cacheable, self.cacheable_urls = 0, set()

    def record(self, url, status, down, up=0, method="GET"):
        path = urllib.parse.urlparse(url).path.split("/")
        kind = f"{method} /" + "/".join(p if not (len(p) >= 64) else "*" for p in path[1:3])
        cacheable = method == "GET" and status == 200 and (path[1] == "blob" or (path[1] == "feed" and "&to=" in url))
        with self.lock:
            self.requests[kind] += 1
            self.statuses[status] += 1
            self.down += down
            self.up += up
            if cacheable:
                self.cacheable += 1
                self.cacheable_urls.add(url)

    def install(self, module):
        get, post = module._get, module._post
        conditional = getattr(module, "_get_conditional", None)
        meter = self

        def counted_get(url, *a, **kw):
            try:
                data = get(url, *a, **kw)
            except urllib.error.HTTPError as e:
                meter.record(url, e.code, 0)
                raise
            meter.record(url, 200, len(data))
            return data

        def counted_post(url, data, ctype, *a, **kw):
            status, reply = post(url, data, ctype, *a, **kw)
            meter.record(url, status, len(reply), len(data), "POST")
            return status, reply
        module._get, module._post = counted_get, counted_post
        self.restore = lambda: setattr(module, "_get", get) or setattr(module, "_post", post) or (
            conditional and setattr(module, "_get_conditional", conditional))
        if conditional:
            def counted_conditional(url, etag, *a, **kw):
                out = conditional(url, etag, *a, **kw)
                meter.record(url, out[0], len(out[1]))
                return out
            module._get_conditional = counted_conditional

    def summary(self):
        total = sum(self.requests.values())
        return {"requests": total, "by_kind": dict(self.requests.most_common()),
                "statuses": {str(k): v for k, v in sorted(self.statuses.items())},
                "mb_down": round(self.down / 1e6, 2), "mb_up": round(self.up / 1e6, 2),
                "cdn_origin_requests": total - self.cacheable + len(self.cacheable_urls)}


def proc_cpu(pid) -> float:
    """User + system CPU seconds of a process (Linux)."""
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def proc_rss_mb(pid) -> float:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return round(int(line.split()[1]) / 1024)
    return 0


def generate_swarm(root: Path, n_agents: int, n_missions: int, seed: int) -> dict:
    """A creator with missions in ten categories and agents in them: join, some files, claims and objections."""
    rng = random.Random(seed)
    now = [int(time.time() * 1000) - 3_600_000]

    def clock():
        now[0] += 1000
        return now[0]
    creator = Node(root / "creator", clock=clock)
    missions = [creator.create_mission(f"Mission {m}", f"software/area{m % 10}", "criteria", challenge_window=7 * 86400)
                ["mission_id"] for m in range(n_missions)]
    hub = Store(root / "hub")
    hub.set_config("accept_push", True)
    hub.set_config("push_rate_per_min", 10 ** 9)  # every simulated agent connects from 127.0.0.1
    hub.ingest(creator.agent_id, creator.store.load_feed(creator.agent_id))
    agents, claims = [], {}
    for i in range(n_agents):
        identity = Identity.load_or_create(root / "agents" / f"a{i:04d}" / "key.json")
        store = Store(root / "agents" / f"a{i:04d}")
        mid = missions[i % n_missions]

        def emit(type_, body, store=store, identity=identity):
            return store.write_own(store.build_own(identity, type_, body, clock()))
        emit("join", {"mission_id": mid})
        role = rng.random()
        if role < 0.2:
            data = f"solution {i} ".encode() * 100
            cid = store.put_blob(data)
            emit("artifact.publish", {"mission_id": mid, "cid": cid, "name": "solution.py", "size": len(data)})
            claims.setdefault(mid, []).append(emit("claim_done", {"mission_id": mid, "target": "mission",
                                                                   "evidence": {"text": "done", "cids": [cid]}})["id"])
            hub.put_blob(data)
        elif role < 0.4 and claims.get(mid):
            emit("challenge", {"target": rng.choice(claims[mid]), "evidence": {"text": "wrong", "cids": []}})
        else:
            emit("work.create", {"mission_id": mid, "title": f"part {i}", "description": ""})
        hub.ingest(identity.public_hex, store.load_feed(identity.public_hex))
        agents.append({"dir": f"a{i:04d}", "id": identity.public_hex, "mission": mid})
    return {"agents": agents, "missions": missions, "creator": creator.agent_id}


def run_swarm_config(base: Path, work: Path, plan: dict, mode: str, rounds: int, post_fraction: float,
                     threads: int, legacy_root: Path | None, seed: int) -> dict:
    """One configuration: a fresh copy of the network, a hub process, a warm-up round, then measured rounds."""
    from humansout import net as new_net
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(base, work)
    legacy = mode == "legacy"
    scoped = mode.startswith("scoped")
    conditional = mode.endswith("+etag")
    port_file = work / "hub.port"
    proc = subprocess.Popen([sys.executable, "-c", HUB_SCRIPT, str(work / "hub"), str(port_file),
                             str(legacy_root) if legacy else ""], cwd=work)
    try:
        for _ in range(200):
            if port_file.exists() and port_file.read_text():
                break
            time.sleep(0.05)
        url = f"http://127.0.0.1:{port_file.read_text()}"
        net_module = load_legacy(legacy_root) if legacy else new_net
        meter = Meter()
        meter.install(net_module)
        hub = Store(work / "hub")
        world = [build_world(hub.all_events(), hub.config(), int(time.time() * 1000), {})]
        lengths = hub.feed_lengths()
        members = {mid: set(json.loads(hub.mission_feeds_view(mid)[0])) for mid in plan["missions"]}
        rng = random.Random(seed)
        nodes = []
        for a in plan["agents"]:
            mission = world[0].missions[a["mission"]]
            mine_blobs = {x["cid"] for x in mission["artifacts"]}
            known = {k: v for k, v in lengths.items() if not scoped or k in members[a["mission"]]}
            known.pop(a["id"], None)
            store = SimStore(work / "agents" / a["dir"], a["id"], url, known, mine_blobs)
            nodes.append(SimNode(store, Identity.load_or_create(store.home / "key.json"), world, lambda: int(time.time() * 1000)))
        missions_of = {a["id"]: [a["mission"]] for a in plan["agents"]}

        def sync_one(node):
            if legacy:
                return net_module.sync(node, None)
            return new_net.sync(node, None, missions=missions_of[node.agent_id] if scoped else None,
                                conditional=conditional, workers=4)

        def one_round(label):
            meter.reset()
            cpu0, t0 = proc_cpu(proc.pid), time.perf_counter()
            with ThreadPoolExecutor(max_workers=threads) as pool:
                reports = list(pool.map(sync_one, nodes))
            wall = time.perf_counter() - t0
            cpu = proc_cpu(proc.pid) - cpu0
            out = {"round": label, **meter.summary(), "hub_cpu_s": round(cpu, 2), "wall_s": round(wall, 1),
                   "achieved_rps": round(sum(meter.requests.values()) / wall), "pulled_events": sum(r["new_events"] for r in reports),
                   "errors": sum(1 for r in reports for p in r["peers"].values() if not p["ok"])}
            out["at_2min_polling"] = {"hub_rps": round(out["requests"] / 120, 1), "hub_cpu_share": round(cpu / 120, 3),
                                      "mbit_s_down": round(meter.down * 8 / 120 / 1e6, 2)}
            return out

        results = [one_round("warm-up")]
        for r in range(rounds):
            posters = rng.sample(nodes, max(1, int(len(nodes) * post_fraction)))
            for k, node in enumerate(posters):  # a work item; every second poster also a file
                st, ident = node.store, node.identity
                mid = missions_of[node.agent_id][0]
                if k % 2 == 0:
                    data = f"file {r} {k} {node.agent_id}".encode() * 60
                    cid = Store.put_blob(st, data)
                    st.write_own(st.build_own(ident, "artifact.publish", {"mission_id": mid, "cid": cid, "name": "f.txt",
                                                                          "size": len(data)}, int(time.time() * 1000)))
                st.write_own(st.build_own(ident, "work.create", {"mission_id": mid, "title": f"r{r}", "description": ""},
                                          int(time.time() * 1000)))
            results.append(one_round(f"round {r + 1}"))
            world[0] = build_world(hub.all_events(), hub.config(), int(time.time() * 1000), {})  # files posted get wanted
        results.append(one_round("settle"))  # picks up what was posted late in the last round
        results.append(one_round("idle"))  # nobody posted: what polling costs when nothing changes

        # a newcomer's first sync: what joining the network costs
        fresh_dir = work / "agents" / "newcomer"
        ident = Identity.load_or_create(fresh_dir / "key.json")
        fresh = SimStore(fresh_dir, ident.public_hex, url, {}, set())
        newcomer = SimNode(fresh, ident, world, lambda: int(time.time() * 1000))
        missions_of[ident.public_hex] = [plan["missions"][0]]
        meter.reset()
        cpu0, t0 = proc_cpu(proc.pid), time.perf_counter()
        sync_one(newcomer)
        first = {**meter.summary(), "hub_cpu_s": round(proc_cpu(proc.pid) - cpu0, 2),
                 "wall_s": round(time.perf_counter() - t0, 1), "feeds": len(fresh.known)}
        return {"mode": mode, "rounds": results, "newcomer_first_sync": first, "hub_rss_mb": proc_rss_mb(proc.pid)}
    finally:
        if "meter" in locals() and hasattr(meter, "restore"):
            meter.restore()  # the client functions are module globals: put them back for whoever runs next
        proc.terminate()
        proc.wait(10)


def load_legacy(root: Path):
    """The humansout package of an older commit, importable next to the current one."""
    spec = importlib.util.spec_from_file_location("humansout_legacy", root / "humansout" / "__init__.py",
                                                  submodule_search_locations=[str(root / "humansout")])
    module = importlib.util.module_from_spec(spec)
    sys.modules["humansout_legacy"] = module
    spec.loader.exec_module(module)
    return importlib.import_module("humansout_legacy.net")


def swarm(args) -> int:
    root = Path(args.out)
    shutil.rmtree(root, ignore_errors=True)
    (root / "base").mkdir(parents=True)
    t0 = time.perf_counter()
    plan = generate_swarm(root / "base", args.agents, args.missions, args.seed)
    gen_s = round(time.perf_counter() - t0, 1)
    legacy_root = None
    modes = [m for m in args.modes.split(",") if m]
    if args.legacy_ref:
        legacy_root = root / "legacy"
        legacy_root.mkdir()
        tar = subprocess.run(["git", "archive", args.legacy_ref, "humansout"], capture_output=True, check=True,
                             cwd=Path(__file__).resolve().parent.parent).stdout
        subprocess.run(["tar", "-x", "-C", str(legacy_root)], input=tar, check=True)
        modes = ["legacy"] + modes
    hub = Store(root / "base" / "hub")
    out = {"agents": args.agents, "missions": args.missions, "events": sum(hub.feed_lengths().values()),
           "feeds": len(hub.feed_lengths()), "post_fraction": args.post_fraction, "client_threads": args.threads,
           "generate_s": gen_s, "configs": []}
    for mode in modes:
        result = run_swarm_config(root / "base", root / "run", plan, mode, args.rounds, args.post_fraction,
                                  args.threads, legacy_root, args.seed)
        out["configs"].append(result)
        print(json.dumps(result), file=sys.stderr, flush=True)
    shutil.rmtree(root / "run", ignore_errors=True)
    print(json.dumps(out, indent=1))
    print(swarm_table(out))
    return 0


def swarm_table(out: dict) -> str:
    """Markdown: active rounds averaged, the idle round, and a newcomer's first sync."""
    rows = ["| Sync | Active round: requests | MB down | Hub CPU s | Origin requests behind a CDN | "
            "Idle round: requests | MB down | Hub CPU s | Newcomer's first sync: requests, MB |",
            "|---|---|---|---|---|---|---|---|---|"]
    for c in out["configs"]:
        active = [r for r in c["rounds"] if r["round"].startswith("round")]
        idle = next(r for r in c["rounds"] if r["round"] == "idle")

        def avg(key, active=active):
            return sum(r[key] for r in active) / len(active)
        n = c["newcomer_first_sync"]
        rows.append(f"| {c['mode']} | {avg('requests'):,.0f} | {avg('mb_down'):.2f} | {avg('hub_cpu_s'):.2f} | "
                    f"{avg('cdn_origin_requests'):,.0f} | {idle['requests']:,} | {idle['mb_down']:.2f} | "
                    f"{idle['hub_cpu_s']:.2f} | {n['requests']:,}, {n['mb_down']} |")
    return "\n".join(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agents", type=int, default=50)
    ap.add_argument("--missions", type=int, default=300)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="/tmp/humansout-load")
    ap.add_argument("--swarm", action="store_true", help="measure the network layer (see the module docstring)")
    ap.add_argument("--rounds", type=int, default=3, help="swarm: measured sync rounds after the warm-up")
    ap.add_argument("--post-fraction", type=float, default=0.05, help="swarm: share of agents posting before a round")
    ap.add_argument("--threads", type=int, default=32, help="swarm: agents syncing at the same time")
    ap.add_argument("--modes", default="full,full+etag,scoped,scoped+etag", help="swarm: configurations to run")
    ap.add_argument("--legacy-ref", help="swarm: also run the client and hub of this git commit")
    args = ap.parse_args(argv)
    if args.swarm:
        return swarm(args)
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
