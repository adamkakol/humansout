"""The fold settles items lazily (only what can have changed). It must decide exactly what the original
fold decides (re-check every pending item after every event), on random networks with bounties,
disputes and rulings, and with all, some or none of the counterexamples verified on this node."""

import importlib.util
import random
from pathlib import Path

import pytest

from humansout import state

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("loadtest", ROOT / "tools" / "loadtest.py")
loadtest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loadtest)


def reference_settle(self, t):
    """The original settle, frozen here: every pending item, every time, until nothing changes."""
    progress = True
    while progress:
        progress = False
        for iid in sorted(self.pending, key=lambda i: (self.items[i]["close_ts"], i)):
            if iid in self.pending and self._resolvable(iid, t):
                self._settle(iid, t)
                progress = True
    for mission in self.missions.values():
        b = mission["bounty"]
        if b and not b["closed"] and t >= b["expires_ts"] and mission["status"] != "done":
            waiting = any(self.items[i]["target"] == "mission" and not self.items[i]["settled"]
                          and self.items[i]["ts"] < b["expires_ts"] for i in mission["claims"])
            if not waiting:
                self._close_bounty(mission, "expired")


def decide(events, config, now, verified, reference, monkeypatch):
    with monkeypatch.context() as m:
        if reference:
            m.setattr(state.World, "settle", reference_settle)
        w = state.build_world(events, config, now, verified)
    return ({i: (x["status"], x["settled"], x.get("upheld"), x.get("minted"), x.get("credits"), x.get("refuted_by"))
             for i, x in w.items.items()},
            sorted(w.balances.items()), sorted(w.locked.items()), sorted(w.credits.items()), w.credit_log, w.rejected,
            {m: (x["status"], x.get("done_claim"), x["bounty"]) for m, x in w.missions.items()})


@pytest.mark.parametrize("seed", [1, 2, 3, 4])
@pytest.mark.parametrize("verified_share", [1.0, 0.5, 0.0])
def test_lazy_settling_decides_exactly_what_the_original_does(tmp_path, seed, verified_share, monkeypatch):
    hub, _, _, _ = loadtest.generate(tmp_path, n_agents=8, n_missions=15, seed=seed)
    events = hub.store.all_events()
    rng = random.Random(seed)
    verified = {k: v.get("result") for k, v in hub.store.verifications().items() if rng.random() < verified_share}
    config = hub.store.config()
    stamps = sorted(e["ts"] for e in events)
    times = rng.sample(stamps, 6) + [stamps[-1] + d * 60_000 for d in (1, 30, 70, 130)]
    if verified_share == 1.0:
        assert any(m["bounty"] for m in state.build_world(events, config, stamps[-1], verified).missions.values())
    for now in times:
        assert decide(events, config, now, verified, False, monkeypatch) == \
            decide(events, config, now, verified, True, monkeypatch), now
