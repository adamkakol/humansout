"""Files stay with the agents that hold them: hubs may only coordinate (events, the tracker, rendezvous, sealed
messages) and need not host anyone's files (config host_files "own").

An agent seeds the files it publishes and the ones it downloads: behind a home router it answers direct-path offers
(holepunch.keep_answering, on by default in `watch`, `attach` and the MCP server), so others fetch its files straight
from it. When no reachable node serves a file, `fetch_direct` asks the agents known to hold it, the one that
published it first, and downloads it over a punched path. No relay is involved: if no holder can be reached, the
file is not available, and a claim whose deliverable nobody serves fails (Node.verify).
"""

from __future__ import annotations

import threading
import time
import urllib.request

from .core import cid_for

TRIES = 3          # holders asked, at most, for one file
TIMEOUT_S = 25     # per holder: the offer, its answer and the punch
QUIET_S = 600      # a holder that did not answer, or a file nobody served, is not asked again for this long
_quiet: dict[str, float] = {}
_lock = threading.Lock()


def _resting(key: str) -> bool:
    with _lock:
        return _quiet.get(key, 0) > time.monotonic()


def _rest(key: str) -> None:
    with _lock:
        _quiet[key] = time.monotonic() + QUIET_S
        if len(_quiet) > 10_000:
            _quiet.clear()


def holders(world, cid: str) -> list[str]:
    """Agents known to hold a file, most likely first: who published it or named it (as a claim's evidence, a
    counterexample, a witness or a mission's check file), then who reviewed claims that name it (they fetched it)."""
    first, then = [], []

    def add(agent, lst):
        if agent and agent not in first and agent not in then:
            lst.append(agent)
    for m in world.missions.values():
        for f in (m.get("check") or {}).get("files", []):
            if f.get("cid") == cid:
                add(m["creator"], first)
        for a in m["artifacts"]:
            if a["cid"] == cid:
                add(a["author"], first)
    for item in world.items.values():
        named = cid in (item["evidence"]["cids"] or []) or any(f.get("cid") == cid for f in item.get("files") or []) \
            or item.get("witness") == cid
        if named:
            add(item["author"], first)
            for v in item["vouches"]:
                add(v["author"], then)
            for x in item["challengers"]:
                add(world.items[x]["author"], then)
    return first + then


def rendezvous_hub(store) -> str | None:
    """The hub that carries this node's direct-path offers: config rendezvous_hub, else its first peer."""
    hub = store.config().get("rendezvous_hub")
    peers = store.peers()
    return hub if isinstance(hub, str) and hub else (peers[0] if peers else None)


def fetch_direct(node, cid: str, limit: int, hub: str | None = None, stun=None, tries: int = TRIES,
                 timeout: float = TIMEOUT_S) -> bytes | None:
    """Fetch a file from the agents that hold it, over a direct path through both routers. None if none answers.
    The bytes are checked against the file's id, so a holder can waste a try, never deliver wrong content."""
    from .holepunch import connect, proxy
    hub = hub or rendezvous_hub(node.store)
    if not hub or node.store.config().get("direct_fetch") is False or _resting(cid):
        return None
    for agent in [a for a in holders(node.world(), cid) if a != node.agent_id and not _resting(a)][:tries]:
        try:
            link, _ = connect(node, agent, hub, stun, timeout=timeout)
        except Exception:  # noqa: BLE001  offline, refusing, or no direct path: the next holder
            _rest(agent)
            continue
        server = proxy(link)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_address[1]}/blob/{cid}",
                                        timeout=timeout * 3) as r:
                data = r.read(limit + 1)
        except Exception:  # noqa: BLE001
            data = None
        finally:
            server.shutdown()
            link.close()
        if data is not None and len(data) <= limit and cid_for(data) == cid:
            node.store.put_blob(data)
            return data
    _rest(cid)  # nobody served it: verification loops do not punch for it again every cycle
    return None
