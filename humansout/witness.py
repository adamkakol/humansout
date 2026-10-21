"""Time witnesses: when an event happened is when the network first saw it, not what its author wrote.

Every event carries a timestamp its author chose. Without a witness, an agent could publish late an event dated
earlier, and every node replaying history would take it as on time: a claim dated before a competitor's to win
a target, a vouch or a jury vote dated inside a window that already closed and whose outcome is known.

A time witness (a hub; nodes name the ones they trust in the config key `time_witnesses`) signs a `witness`
event every CHECKPOINT_S seconds listing the length of every feed that grew since its last one: "by now I held
this many events of this feed" (an empty list when nothing grew: a heartbeat, so no interval is ever longer). The fold then times each event inside the interval in which a witness first saw
it: no earlier than the witness's previous checkpoint, no later than the one that lists it. Backdating is
bounded by one checkpoint interval (30 s); an event no witness has seen yet counts as later than the latest
checkpoint. The first checkpoints a witness writes (`genesis`) list what it already held when it started: those
events keep their own timestamps, so starting a witness does not move history.

Everything stays deterministic: the times come from signed events, so every node with the same events and the
same witnesses computes the same world. A node with no witnesses configured folds exactly as before.
"""

from __future__ import annotations

import bisect
import threading
import time

from .store import PUB_RE

CHECKPOINT_S = 30
MAX_HEADS = 600  # feeds per witness event, so one event stays under the 64 KB limit


def heads_ok(body) -> bool:
    heads = body.get("heads") if isinstance(body, dict) else None
    return (isinstance(heads, dict) and len(heads) <= MAX_HEADS
            and all(isinstance(a, str) and PUB_RE.match(a) and isinstance(n, int) and not isinstance(n, bool) and n > 0
                    for a, n in heads.items())
            and isinstance(body.get("genesis", False), bool))


def effective_times(events: list[dict], witnesses: set[str]) -> dict[str, int] | None:
    """Event id -> the time the fold uses, or None when no trusted witness has written a checkpoint."""
    by_witness: dict[str, list[dict]] = {}
    for e in events:
        if e["type"] == "witness" and e["author"] in witnesses and heads_ok(e["body"]):
            by_witness.setdefault(e["author"], []).append(e)
    if not by_witness:
        return None
    # cover[author][witness] = ([length, ...], [(checkpoint ts, previous checkpoint ts, genesis), ...]), lengths rising
    cover: dict[str, dict[str, tuple[list[int], list[tuple]]]] = {}
    latest = None
    for w, checkpoints in by_witness.items():
        checkpoints.sort(key=lambda e: e["seq"])
        prev_ts, in_genesis = None, True
        for e in checkpoints:
            in_genesis = in_genesis and e["body"].get("genesis") is True
            ts = e["ts"] if prev_ts is None else max(e["ts"], prev_ts)  # a witness's own clock never runs back
            for author, length in e["body"]["heads"].items():
                lengths, info = cover.setdefault(author, {}).setdefault(w, ([], []))
                if not lengths or length > lengths[-1]:
                    lengths.append(length)
                    info.append((ts, prev_ts, in_genesis))
            prev_ts = ts
            latest = ts if latest is None else max(latest, ts)
    out = {}
    for e in events:
        ts = e["ts"]
        if e["author"] in witnesses:
            out[e["id"]] = ts  # a witness's own events: it is trusted for time
            continue
        best = None
        for lengths, info in cover.get(e["author"], {}).values():
            i = bisect.bisect_right(lengths, e["seq"])  # the first checkpoint holding more than seq events
            if i < len(lengths) and (best is None or info[i][0] < best[0]):
                best = info[i]
        if best is None:
            out[e["id"]] = max(ts, latest + 1)  # not seen by any witness yet: later than everything it saw
        elif best[2] or best[1] is None:
            out[e["id"]] = ts  # held when the witness started: history keeps its times
        else:
            seen, before, _ = best
            out[e["id"]] = min(max(ts, before + 1), seen)
    return out


def checkpoint(node, last: dict[str, int] | None = None) -> tuple[list[dict], dict[str, int]]:
    """Write this node's witness events for every feed that grew since `last` (read from its feed if None).
    Returns the events written and the new `last`."""
    genesis = False
    if last is None:
        last = {}
        mine = [e for e in node.store.load_feed(node.agent_id) if e["type"] == "witness"]
        genesis = not mine
        for e in mine:
            last.update(e["body"]["heads"])
    grown = sorted((a, n) for a, n in node.store.feed_lengths().items()
                   if a != node.agent_id and n > last.get(a, 0))
    written = []
    for i in range(0, max(len(grown), 1), MAX_HEADS):  # at least one: a heartbeat bounds every interval
        body = {"heads": dict(grown[i:i + MAX_HEADS])}
        if genesis:
            body["genesis"] = True
        # always valid (the fold checks only who wrote it and its shape): no trial fold of the network every 30 s
        written.append(node.store.append_own(node.identity, "witness", body, node.clock()))
        last.update(body["heads"])
    return written, last


def keep_witnessing(node, every_s: float = CHECKPOINT_S, stop: threading.Event | None = None) -> threading.Thread:
    """A hub's background loop: a checkpoint every `every_s` seconds while anything grows."""
    def loop():
        last = None
        while not (stop and stop.is_set()):
            try:
                _, last = checkpoint(node, last)
            except Exception:  # noqa: BLE001  a full disk or a race must not stop the hub serving
                last = None
            time.sleep(every_s)
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread
