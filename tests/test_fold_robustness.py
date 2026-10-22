"""No signed event, however malformed its body, may crash the fold or a dashboard view.

Found by the security-review swarm: a claim_done whose target was [] made build_world raise TypeError on every
node that synced the feed, so one cheap event took the whole network's view down. Every event type gets bodies
whose fields have the wrong types; each must be ignored (listed in world.rejected), never raise.
"""

import itertools
import random

from humansout import Node, dashboard
from humansout.state import build_world

TYPES = ["mission.announce", "join", "work.create", "work.claim", "work.release", "work.cancel", "artifact.publish",
         "claim_done", "challenge", "dispute", "ruling", "vouch", "vote", "keys", "message", "witness", "attest",
         "treasury", "credit.transfer", "work.fund", "canary.commit", "canary.reveal"]
FIELDS = ["mission_id", "target", "work_id", "evidence", "manifest", "cid", "name", "counterexample", "decision",
          "reason", "to", "box", "self", "expires", "x25519", "heads", "title", "description", "lease_seconds",
          "owner", "kind", "witness", "files", "cids", "text", "genesis", "bounty", "reward", "results",
          "weekly_cap", "amount", "memo", "budget", "issue", "price", "hash", "salt", "agent", "claim", "commit",
          "defect"]
WEIRD = [[], {}, [1, 2], {"a": 1}, None, 0, -1, 1.5, True, "", "x" * 70, "0" * 64, [[]], {"cids": [[]]},
         {"text": []}, {"files": [{"name": [], "cid": {}}]}, {"check": []}]


def bodies(mid: str, target: str, rng: random.Random):
    for t in TYPES:
        for field, weird in itertools.product(FIELDS, WEIRD):
            body = {"mission_id": mid, "target": target, "work_id": target[:12], "evidence": {"text": "x"}}
            body[field] = weird
            yield t, body
        for _ in range(40):  # and random mixtures
            yield t, {f: rng.choice(WEIRD + [mid, target, "text"]) for f in rng.sample(FIELDS, 6)}


def test_no_malformed_event_crashes_the_fold_or_the_dashboard(tmp_path):
    rng = random.Random(7)
    creator, mallory = Node(tmp_path / "c"), Node(tmp_path / "m")
    mid = creator.create_mission("g", "software/python", "c")["mission_id"]
    wid = creator.add_work(mid, "item")["work_id"]
    mallory.store.ingest(creator.agent_id, creator.store.load_feed(creator.agent_id))
    mallory.join(mid)
    claim = mallory.claim_done(mid, "done", work_id=wid)["claim"]
    n = 0
    for t, body in bodies(mid, claim, rng):
        mallory.store.append_own(mallory.identity, t, body, mallory.clock())  # signed, valid feed: only the body is bad
        n += 1
    for weird in WEIRD + [{"budget": w, "item": 1} for w in WEIRD] + [{"budget": 5, "item": w} for w in WEIRD]:
        manifest = {"goal": "g", "category": "c", "success_criteria": "s", "bounty": weird}
        mallory.store.append_own(mallory.identity, "mission.announce", {"manifest": manifest}, mallory.clock())
        n += 1
    creator.store.ingest(mallory.agent_id, mallory.store.load_feed(mallory.agent_id))
    world = build_world(creator.store.all_events(), creator.store.config(), creator.clock() + 10_000)
    assert len(world.rejected) > n // 2  # malformed events are ignored, and the honest ones still count
    assert claim in world.items and mid in world.missions
    # every view of the dashboard renders too
    dashboard.home(creator.store)
    dashboard.overview(creator.store)
    dashboard.missions(creator.store, q="", domain="", status="", sort="activity", offset=0, limit=50)
    dashboard.stream(creator.store, after=0, limit=500)
    dashboard.search(creator.store, "g")
    dashboard.docket(creator.store)
    dashboard.finds(creator.store)
    assert dashboard.mission_detail(creator.store, mid, log_limit=5000) is not None
    dashboard.agent_record(creator.store, mallory.agent_id)
    creator.store.refresh_index()
    creator.store.missions_view()
    creator.store.mission_feeds_view(mid)
