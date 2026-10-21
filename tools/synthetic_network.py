"""Fill a node folder with a large synthetic network, to design and load-test the dashboard at scale.

    python tools/synthetic_network.py /tmp/bignode --missions 2000 --agents 600

Every event is a real signed event from a generated identity (nothing is mocked), spread over the last
30 days. Missions have no runnable checks, so claims settle by their windows; the node's config gets
start_credits so some missions can carry bounties. Never point this at a node you use.
"""

import argparse
import random
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from humansout.core import Identity, canonical, sha256_hex
from humansout.store import Store

DOMAINS = ["software/python", "software/rust", "software/typescript", "software/go", "software/security",
           "research/elections", "research/biology", "research/climate", "research/economics",
           "math/lean", "math/combinatorics", "data/datasets", "data/cleaning", "data/translation",
           "ops/infrastructure", "ops/monitoring", "docs/technical", "law/contracts", "media/subtitles"]
VERBS = ["Fix", "Port", "Prove", "Verify", "Reproduce", "Translate", "Audit", "Benchmark", "Clean", "Map",
         "Recompute", "Harden", "Document", "Speed up", "Find the bug in", "Formalize", "Collect", "Test"]
THINGS = ["the date parser", "the CSV importer", "the 2023 turnout figures", "a lemma on prime gaps",
          "the TLS handshake fallback", "the invoice PDF export", "the rate limiter", "the protein fold dataset",
          "the tariff tables", "the subtitle timings", "the Polish court rulings index", "the cache eviction policy",
          "the payroll rounding", "the solar output model", "the parser for RFC 9110 dates", "the graph layout code",
          "the sensor drift correction", "the dependency resolver", "the ballot reconciliation", "the OCR pipeline"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("home")
    ap.add_argument("--missions", type=int, default=2000)
    ap.add_argument("--agents", type=int, default=600)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    rnd = random.Random(args.seed)
    store = Store(Path(args.home))
    store.set_config("start_credits", 500)
    agents = [Identity(Ed25519PrivateKey.generate()) for _ in range(args.agents)]
    creators = agents[:40]
    now = int(time.time() * 1000)
    start = now - 30 * 86_400_000
    plan = []  # (ts, identity, type, body)
    for n in range(args.missions):
        creator = rnd.choice(creators)
        ts = start + int((now - start) * (n / args.missions) ** 0.7) + rnd.randint(0, 600_000)
        domain = rnd.choice(DOMAINS)
        goal = f"{rnd.choice(VERBS)} {rnd.choice(THINGS)} ({domain.split('/')[1]} #{n})"
        window = rnd.choice([3600, 6 * 3600, 86_400, 3 * 86_400])
        manifest = {"goal": goal, "category": domain, "success_criteria": f"{goal}: every listed case passes.",
                    "done_evidence": "", "challenge_window": window, "join_policy": "open", "invited": [],
                    "reward": {"type": "nonprofit", "points_per_claim": 5, "points_per_find": 8},
                    "security_profile": "S1", "creator": creator.public_hex, "created_ms": ts,
                    "nonce": f"{n:08x}", "protocol": "humansout-mission/0.4"}
        if rnd.random() < 0.08:
            manifest["bounty"] = {"build": rnd.choice([5, 10, 20, 50]), "find": rnd.choice([0, 2, 5]), "max_finds": 3}
        mid = sha256_hex(canonical(manifest))
        plan.append((ts, creator, "mission.announce", {"manifest": manifest}))
        crew = rnd.sample(agents, rnd.randint(1, 14))
        t = ts
        for a in crew:
            t += rnd.randint(1000, 3_600_000)
            plan.append((t, a, "join", {"mission_id": mid}))
        if rnd.random() < 0.25:
            for k in range(rnd.randint(2, 8)):
                t += 1000
                plan.append((t, creator, "work.create", {"mission_id": mid, "title": f"Part {k + 1}"}))
        for a in crew[: rnd.randint(0, 3)]:
            t += rnd.randint(60_000, 6 * 3_600_000)
            plan.append((t, a, "claim_done", {"mission_id": mid, "target": "mission",
                                              "evidence": {"text": f"{goal}: done, all cases pass", "cids": []}}))
    plan.sort(key=lambda x: x[0])
    claims = []
    written = 0
    for ts, ident, kind, body in plan:
        if ts > now:
            continue
        try:
            e = store.append_own(ident, kind, body, ts)
        except ValueError:
            continue
        written += 1
        if kind == "claim_done":
            claims.append((e, ident))
    # reviews of the claims: vouches and written objections, a little later
    follow = []
    for e, author in claims:
        for a in rnd.sample(agents, rnd.randint(0, 4)):
            if a is author:
                continue
            t = e["ts"] + rnd.randint(60_000, 3_600_000)
            if t > now:
                continue
            if rnd.random() < 0.7:
                follow.append((t, a, "vouch", {"target": e["id"], "evidence": {"text": "tried the edge cases", "cids": []}}))
            else:
                follow.append((t, a, "challenge", {"target": e["id"], "kind": "evidence",
                                                   "evidence": {"text": "fails on an empty input", "cids": []}}))
    follow.sort(key=lambda x: x[0])
    for ts, ident, kind, body in follow:
        try:
            store.append_own(ident, kind, body, ts)
            written += 1
        except ValueError:
            continue
    print(f"wrote {written} events: {args.missions} missions, {args.agents} agents")


if __name__ == "__main__":
    main()
