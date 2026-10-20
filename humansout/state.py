"""Deterministic mission state, computed from signed events.

Every node runs the same fold over the events it has, so every node reaches the
same view (given the same events, the same `now`, the same local config and the
same local verification results). There is no server that owns the state.

The acceptance rule (section 5.1 of the concept doc):
  1. claim_done   - an agent announces that the mission or a work item is done, with evidence.
  2. challenge    - any other agent may challenge within the challenge window, with counter-evidence.
                    A challenge is itself a claim, so it can be challenged the same way.
  3. accept       - when the window closes and no challenge stands, the claim is accepted.

Runnable counterexamples (protocol 0.2):
  - a mission may declare a check: container image, command, check files, deliverable name;
    a claim's first artifact is the deliverable,
  - a counterexample challenge adds files and says the check then fails; every node re-runs
    it in its sandbox (verify.py) and keeps the result locally; the fold reads those results,
  - reproduced     -> the claim is failing at once; stakes settle after the dispute window,
  - not reproduced -> (or flaky) the challenger loses its stake to the claim's author,
  - unverified     -> a node that has not re-run it does not guess: the claim waits,
  - meaning        -> the claim's author may dispute what a reproduced counterexample expects;
                      the mission creator rules; without a ruling it stands when its window closes,
  - the first reproduced counterexample that refutes a claim earns find points.

Reputation as stake (section 9.7):
  - points are kept per agent and per domain (the mission category),
  - claim_done stakes a share of the author's available points,
  - a challenge stakes the same amount as its target,
  - whoever refutes an item takes its stake (the earliest standing challenger wins),
  - an accepted claim creates new points for its author (only in missions of trusted creators).
"""

from __future__ import annotations

import heapq
import re

from .core import canonical, sha256_hex
from .store import CID_RE, PUB_RE
from .verify import DEFAULT_TIMEOUT, LABELS, verification_key

PROFILES = ("S0", "S1", "S2", "S3")
DAY_S = 86400
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}$")
CHECK_KEYS = {"image", "command", "deliverable", "files", "timeout", "require_witness"}
MAX_FILES = 20


def file_list_error(files, reserved=()) -> str | None:
    if not isinstance(files, list) or len(files) > MAX_FILES:
        return f"files must be a list of at most {MAX_FILES}"
    seen = set(reserved)
    for f in files:
        if not isinstance(f, dict) or set(f) != {"name", "cid"}:
            return "each file needs exactly a name and a cid"
        if not isinstance(f["name"], str) or not NAME_RE.match(f["name"]):
            return f"invalid file name {f.get('name')!r}"
        if not isinstance(f["cid"], str) or not CID_RE.match(f["cid"]):
            return "invalid file cid"
        if f["name"] in seen:
            return f"file name {f['name']} is used twice or collides with the check"
        seen.add(f["name"])
    return None


def check_error(check) -> str | None:
    if not isinstance(check, dict) or not set(check) <= CHECK_KEYS:
        return "check must be an object with image, command, deliverable, files and timeout"
    image, command = check.get("image"), check.get("command")
    if not isinstance(image, str) or not image.strip() or len(image) > 300:
        return "check.image is required"
    if (not isinstance(command, list) or not 1 <= len(command) <= 50
            or not all(isinstance(c, str) and c and len(c) <= 2000 for c in command)):
        return "check.command must be a non-empty list of strings"
    if not isinstance(check.get("deliverable"), str) or not NAME_RE.match(check["deliverable"]):
        return "check.deliverable must be a plain file name"
    err = file_list_error(check.get("files", []), reserved=(check["deliverable"],))
    if err:
        return "check." + err
    timeout = check.get("timeout", DEFAULT_TIMEOUT)
    if not isinstance(timeout, int) or not 1 <= timeout <= 3600:
        return "check.timeout must be 1 s .. 1 hour"
    if not isinstance(check.get("require_witness", False), bool):
        return "check.require_witness must be true or false"
    return None


def combine(claim_result, witness_result, has_witness):
    """One counterexample verdict from the run on the claim and, if given, the run on the witness."""
    if claim_result is None or (has_witness and witness_result is None):
        return None                       # not re-run yet on this node
    if claim_result != "fail":
        return claim_result               # passes, or flaky: not reproduced
    if has_witness and witness_result != "pass":
        return "invalid"                  # fails on the challenger's own witness too: a broken test
    return "fail"


class World:
    def __init__(self, config: dict, verifications: dict | None = None):
        self.config = config
        self.verifications = verifications or {}  # verification key -> "pass" | "fail" | "flaky"
        self.missions: dict[str, dict] = {}
        self.items: dict[str, dict] = {}        # claims and challenges by event id
        self.pending: set[str] = set()
        self.balances: dict[tuple, int] = {}
        self.locked: dict[tuple, int] = {}
        self.stats: dict[tuple, dict] = {}
        self.rejected: list[dict] = []          # events that were ignored, with the reason
        self.credits: dict[str, int] = {}       # global, spendable on bounties (reputation stays per domain)
        self.credits_locked: dict[str, int] = {}
        self.credit_log: list[dict] = []        # every credit movement, for records and audits
        # Settling is re-checked only when it can change: after an event that touches items, or once the
        # clock reaches the next deadline (a window closing, a dispute window ending, a bounty expiring).
        self._dirty = True
        self._next_due = 0
        self._deadlines: list[tuple] = []        # heap of (time, item id): when an item's settling can change
        self._bounty_missions: list[dict] = []  # only these can expire, so only these are checked

    # ---- helpers ----------------------------------------------------------------
    def _reject(self, event, reason):
        self.rejected.append({"event": event["id"], "type": event["type"], "author": event["author"], "reason": reason})

    def balance(self, agent, domain) -> int:
        key = (agent, domain)
        if key not in self.balances:
            self.balances[key] = int(self.config.get("start_credit", 10))
        return self.balances[key]

    def available(self, agent, domain) -> int:
        return self.balance(agent, domain) - self.locked.get((agent, domain), 0)

    def _stat(self, agent, domain) -> dict:
        self.balance(agent, domain)
        return self.stats.setdefault((agent, domain), {"built": 0, "found": 0, "refuted": 0, "failed_challenges": 0})

    def stake_for(self, agent, domain) -> int:
        avail = self.available(agent, domain)
        if avail < 1:
            return 0
        return max(1, int(avail * float(self.config.get("stake_fraction", 0.1))))

    # ---- credits: earn by working for others, spend to get your own missions done --------------
    def credit(self, agent) -> int:
        if agent not in self.credits:
            self.credits[agent] = int(self.config.get("start_credits", 0))
        return self.credits[agent]

    def credits_available(self, agent) -> int:
        return self.credit(agent) - self.credits_locked.get(agent, 0)

    def _credit_move(self, mission, kind, amount, to=None, frm=None, item=None):
        """Mint (frm None), pay from escrow (frm = creator) or refund an escrow (to None)."""
        if amount <= 0:
            return
        if frm is not None:
            self.credit(frm)
            self.credits_locked[frm] = self.credits_locked.get(frm, 0) - amount
            if to is not None:
                self.credits[frm] -= amount
        if to is not None:
            self.credits[to] = self.credit(to) + amount
        self.credit_log.append({"mission": mission["id"], "kind": kind, "amount": amount, "to": to,
                                "from": frm, "item": item})

    def _pay(self, mission, role, agent, item_id):
        """Pay a builder (role build) or a finder (role find): from the bounty escrow, or minted by a trusted creator."""
        b = mission["bounty"]
        if b and not b["closed"]:
            amount = b["build"] if role == "build" else (b["find"] if b["finds_left"] > 0 else 0)
            if role == "find" and amount:
                b["finds_left"] -= 1
            b["locked"] -= amount
            self._credit_move(mission, role, amount, to=agent, frm=mission["creator"], item=item_id)
            return amount
        if not b and self._trusted(mission):
            amount = int(self.config.get(f"credits_per_{role}", 2 if role == "find" else 1))
            self._credit_move(mission, f"{role} (minted)", amount, to=agent, item=item_id)
            return amount
        return 0

    def _close_bounty(self, mission, reason):
        b = mission["bounty"]
        if b and not b["closed"]:
            self._credit_move(mission, f"refund ({reason})", b["locked"], frm=mission["creator"])
            b["locked"], b["closed"] = 0, reason

    def _trusted(self, mission) -> bool:
        trusted = self.config.get("trusted_creators", ["*"])
        return "*" in trusted or mission["creator"] in trusted

    @staticmethod
    def may_act(mission, agent) -> bool:
        m = mission["manifest"]
        return (agent == mission["creator"] or m.get("join_policy", "open") == "open"
                or agent in m.get("invited", []))

    @staticmethod
    def _evidence(body):
        ev = body.get("evidence") or {}
        text = ev.get("text", "")
        cids = ev.get("cids", [])
        if not isinstance(text, str) or not isinstance(cids, list) or not all(isinstance(c, str) and CID_RE.match(c) for c in cids):
            return None
        if not text.strip() and not cids:
            return None
        return {"text": text[:20000], "cids": cids[:50]}

    # ---- event application -----------------------------------------------------
    def apply(self, e):
        handler = {
            "mission.announce": self._announce,
            "join": self._join,
            "work.create": self._work_create,
            "work.claim": self._work_claim,
            "work.release": self._work_release,
            "artifact.publish": self._artifact,
            "claim_done": self._claim_done,
            "challenge": self._challenge,
            "dispute": self._dispute,
            "ruling": self._ruling,
        }.get(e["type"])
        if handler is None:
            return self._reject(e, "unknown event type")
        handler(e)
        if e["type"] in ("dispute", "ruling"):
            self._dirty = True  # a ruling can decide a counterexample at once; disputes move its deadline
        elif e["type"] in ("claim_done", "challenge") and e["id"] in self.items:
            # A new claim or objection settles nothing at once: an objection must come inside its target's
            # window, and the new item's own deadlines lie ahead. Only those deadlines need watching.
            item = self.items[e["id"]]
            for ts in (item["close_ts"], item.get("resolve_ts")):
                if ts:
                    heapq.heappush(self._deadlines, (ts, e["id"]))
                    self._next_due = min(self._next_due, ts)

    def _mission_of(self, e):
        mid = e["body"].get("mission_id")
        mission = self.missions.get(mid) if isinstance(mid, str) else None
        if mission is None:
            self._reject(e, "unknown mission")
        elif e["ts"] <= mission["ts"]:
            self._reject(e, "event must be later than the mission it refers to")
            return None
        return mission

    def _participant(self, e):
        mission = self._mission_of(e)
        if mission is None:
            return None
        if e["author"] not in mission["participants"]:
            self._reject(e, "not a participant of this mission")
            return None
        return mission

    def _announce(self, e):
        m = e["body"].get("manifest")
        if not isinstance(m, dict) or m.get("creator") != e["author"]:
            return self._reject(e, "manifest must name its author as creator")
        for key in ("goal", "category", "success_criteria"):
            if not isinstance(m.get(key), str) or not m[key].strip():
                return self._reject(e, f"manifest field '{key}' is required")
        window = m.get("challenge_window", DAY_S)
        if not isinstance(window, int) or not 1 <= window <= 30 * DAY_S:
            return self._reject(e, "challenge_window must be 1 s .. 30 days")
        if m.get("join_policy", "open") not in ("open", "invite-only"):
            return self._reject(e, "join_policy must be open or invite-only")
        invited = m.get("invited", [])
        if not isinstance(invited, list) or not all(isinstance(p, str) and PUB_RE.match(p) for p in invited):
            return self._reject(e, "invited must be a list of agent keys")
        if m.get("security_profile", "S1") not in PROFILES:
            return self._reject(e, "unknown security_profile")
        reward = m.get("reward", {"type": "nonprofit", "points_per_claim": 5})
        if not isinstance(reward, dict) or reward.get("type") != "nonprofit":
            return self._reject(e, "v0 supports only nonprofit missions (reputation, no money)")
        for key in ("points_per_claim", "points_per_find"):
            if key in reward and (not isinstance(reward[key], int) or reward[key] < 0):
                return self._reject(e, f"reward.{key} must be a non-negative integer")
        check = m.get("check")
        if check is not None:
            err = check_error(check)
            if err:
                return self._reject(e, err)
        dispute = m.get("dispute_window", min(3600, window))
        if not isinstance(dispute, int) or not 1 <= dispute <= 30 * DAY_S:
            return self._reject(e, "dispute_window must be 1 s .. 30 days")
        bounty = m.get("bounty")
        escrow = None
        if bounty is not None:
            if not isinstance(bounty, dict) or not set(bounty) <= {"build", "find", "max_finds", "expires"}:
                return self._reject(e, "bounty must have only build, find, max_finds and expires")
            values = {k: bounty.get(k, d) for k, d in (("build", 0), ("find", 0), ("max_finds", 0), ("expires", 30 * DAY_S))}
            if not all(isinstance(v, int) and v >= 0 for v in values.values()) or not 1 <= values["expires"] <= 365 * DAY_S:
                return self._reject(e, "bounty values must be non-negative integers; expires 1 s .. 365 days")
            total = values["build"] + values["find"] * values["max_finds"]
            if total <= 0:
                return self._reject(e, "a bounty must offer at least one credit")
            if self.credits_available(e["author"]) < total:
                return self._reject(e, f"not enough credits for a bounty of {total} "
                                       f"(available: {self.credits_available(e['author'])})")
            escrow = {"build": values["build"], "find": values["find"], "finds_left": values["max_finds"],
                      "total": total, "locked": total, "expires_ts": e["ts"] + values["expires"] * 1000, "closed": None}
        mid = sha256_hex(canonical(m))
        if mid in self.missions:
            return self._reject(e, "mission already announced")
        if escrow:
            self.credits_locked[e["author"]] = self.credits_locked.get(e["author"], 0) + escrow["total"]
        self.missions[mid] = {
            "id": mid, "manifest": m, "creator": e["author"], "ts": e["ts"],
            "window_ms": window * 1000, "dispute_ms": dispute * 1000, "domain": m["category"],
            "check": check, "participants": {e["author"]: {"ts": e["ts"], "owner": None}},
            "work": {}, "artifacts": [], "claims": [], "status": "open", "done_claim": None, "bounty": escrow,
        }
        if escrow:
            self._bounty_missions.append(self.missions[mid])
            self._next_due = min(self._next_due, escrow["expires_ts"])

    def _join(self, e):
        mission = self._mission_of(e)
        if mission is None:
            return
        if e["author"] in mission["participants"]:
            return self._reject(e, "already joined")
        if not self.may_act(mission, e["author"]):
            return self._reject(e, "mission is invite-only")
        limit = mission["manifest"].get("max_participants")
        if isinstance(limit, int) and len(mission["participants"]) >= limit:
            return self._reject(e, "mission is full")
        owner = e["body"].get("owner")
        mission["participants"][e["author"]] = {"ts": e["ts"], "owner": owner if isinstance(owner, str) else None}

    def _work_create(self, e):
        mission = self._participant(e)
        if mission is None:
            return
        title = e["body"].get("title")
        if not isinstance(title, str) or not title.strip():
            return self._reject(e, "work item needs a title")
        wid = e["id"][:12]
        mission["work"][wid] = {"id": wid, "title": title[:300], "description": str(e["body"].get("description", ""))[:5000],
                                "created_by": e["author"], "claimed_by": None, "lease_until": 0, "done_claim": None,
                                "ts": e["ts"]}

    def _work_item(self, e, mission):
        work = mission["work"].get(e["body"].get("work_id"))
        if work is None:
            self._reject(e, "unknown work item")
        elif e["ts"] <= work["ts"]:
            self._reject(e, "event must be later than the work item it refers to")
            return None
        return work

    def _work_claim(self, e):
        mission = self._participant(e)
        work = self._work_item(e, mission) if mission else None
        if work is None:
            return
        if work["done_claim"]:
            return self._reject(e, "work item is already done")
        if work["claimed_by"] not in (None, e["author"]) and work["lease_until"] > e["ts"]:
            return self._reject(e, "work item is claimed by another agent")
        lease = e["body"].get("lease_seconds", 3600)
        lease = min(max(int(lease) if isinstance(lease, int) else 3600, 60), 7 * DAY_S)
        work["claimed_by"], work["lease_until"] = e["author"], e["ts"] + lease * 1000

    def _work_release(self, e):
        mission = self._participant(e)
        work = self._work_item(e, mission) if mission else None
        if work is None:
            return
        if work["claimed_by"] != e["author"]:
            return self._reject(e, "only the claimant can release")
        work["claimed_by"], work["lease_until"] = None, 0

    def _artifact(self, e):
        mission = self._participant(e)
        if mission is None:
            return
        b = e["body"]
        if not isinstance(b.get("cid"), str) or not CID_RE.match(b["cid"]):
            return self._reject(e, "invalid cid")
        mission["artifacts"].append({"cid": b["cid"], "name": str(b.get("name", ""))[:200],
                                     "size": b.get("size") if isinstance(b.get("size"), int) else None,
                                     "author": e["author"], "ts": e["ts"], "event": e["id"]})

    def _claim_done(self, e):
        mission = self._participant(e)
        if mission is None:
            return
        target = e["body"].get("target", "mission")
        if target == "mission":
            if mission["status"] == "done":
                return self._reject(e, "mission is already done")
        else:
            work = mission["work"].get(target)
            if work is None:
                return self._reject(e, "unknown work item")
            if e["ts"] <= work["ts"]:
                return self._reject(e, "event must be later than the work item it refers to")
            if work["done_claim"]:
                return self._reject(e, "work item is already done")
            if work["claimed_by"] not in (None, e["author"]) and work["lease_until"] > e["ts"]:
                return self._reject(e, "work item is claimed by another agent")
        for iid in mission["claims"]:
            other = self.items[iid]
            if (other["author"] == e["author"] and other["target"] == target and not other["settled"]
                    and not other["challengers"]):
                # A new claim is allowed only as a fix after the previous one was challenged.
                return self._reject(e, "you already have an unchallenged pending claim for this target")
        evidence = self._evidence(e["body"])
        if evidence is None:
            return self._reject(e, "claim needs evidence (text or cids)")
        if mission["check"] and target == "mission" and not evidence["cids"]:
            # without a deliverable nothing can be re-run or broken, and the claim would pass by default
            return self._reject(e, f"this mission has a runnable check: name your deliverable "
                                   f"({mission['check']['deliverable']}) as the first cid; publish it first")
        domain = mission["domain"]
        stake = self.stake_for(e["author"], domain)
        self._open_item(e, "claim", mission, target, evidence, stake, mode="claim")
        mission["claims"].append(e["id"])

    def _challenge(self, e):
        body = e["body"]
        target_id = body.get("target")
        target = self.items.get(target_id) if isinstance(target_id, str) else None
        if target is None:
            return self._reject(e, "unknown target")
        if e["ts"] <= target["ts"]:
            return self._reject(e, "event must be later than the item it challenges")
        mission = self.missions[target["mission"]]
        if target["settled"] or e["ts"] > target["close_ts"]:
            return self._reject(e, "challenge window is closed")
        if target["mode"] == "counterexample":
            return self._reject(e, "a counterexample is settled by re-running it; the claim's author can dispute it")
        if e["author"] == target["author"]:
            return self._reject(e, "cannot challenge your own item")
        if not self.may_act(mission, e["author"]):
            return self._reject(e, "mission is invite-only")
        own = mission["participants"].get(e["author"], {}).get("owner")
        other = mission["participants"].get(target["author"], {}).get("owner")
        if own and other and own == other:
            return self._reject(e, "agents of the same owner cannot challenge each other")
        if any(self.items[c]["author"] == e["author"] for c in target["challengers"]):
            return self._reject(e, "you already challenged this item")
        evidence = self._evidence(body)
        if evidence is None:
            return self._reject(e, "challenge needs counter-evidence (text or cids)")
        kind = body.get("kind", "evidence")
        if kind not in ("evidence", "counterexample"):
            return self._reject(e, "unknown challenge kind")
        if (kind == "evidence" and mission["check"] and target["kind"] == "claim" and target["target"] == "mission"):
            # an unanswered written objection would refute work that passes every test: objections here must run
            return self._reject(e, "this mission has a runnable check: object with a counterexample (a test that "
                                   "fails on the deliverable), not with text")
        extra = {}
        if kind == "counterexample":
            check = mission["check"]
            if target["kind"] != "claim":
                return self._reject(e, "a counterexample can only target a claim")
            if not check:
                return self._reject(e, "this mission has no runnable check")
            if not target["evidence"]["cids"]:
                return self._reject(e, "the claim has no deliverable artifact to run")
            spec = body.get("counterexample")
            files = spec.get("files", []) if isinstance(spec, dict) else None
            reserved = [check["deliverable"]] + [f["name"] for f in check.get("files", [])]
            err = file_list_error(files, reserved)
            if err:
                return self._reject(e, "counterexample " + err)
            witness = spec.get("witness") if isinstance(spec, dict) else None
            if witness is not None and not (isinstance(witness, dict) and set(witness) == {"cid"}
                                            and isinstance(witness["cid"], str) and CID_RE.match(witness["cid"])):
                return self._reject(e, "counterexample witness must be {\"cid\": \"sha256-...\"}")
            if check.get("require_witness") and witness is None:
                return self._reject(e, "this mission requires a witness: a deliverable on which your "
                                       "counterexample and the check pass")
            vkey = verification_key(check, target["evidence"]["cids"][0], files)
            wkey = verification_key(check, witness["cid"], files) if witness else None
            extra = {"files": files, "vkey": vkey, "wkey": wkey, "witness": witness["cid"] if witness else None,
                     "verification": combine(self.verifications.get(vkey),
                                             self.verifications.get(wkey) if wkey else None, bool(witness)),
                     "resolve_ts": e["ts"] + mission["dispute_ms"]}
        stake = target["stake"]
        if self.available(e["author"], mission["domain"]) < stake:
            return self._reject(e, f"not enough available points to match the stake of {stake}")
        self._open_item(e, "challenge", mission, target_id, evidence, stake, mode=kind, **extra)
        target["challengers"].append(e["id"])

    def _dispute(self, e):
        body = e["body"]
        tid = body.get("target")
        item = self.items.get(tid) if isinstance(tid, str) else None
        if item is None or item["mode"] != "counterexample":
            return self._reject(e, "a dispute must target a counterexample challenge")
        if e["ts"] <= item["ts"]:
            return self._reject(e, "event must be later than the item it disputes")
        if e["author"] != self.items[item["target"]]["author"]:
            return self._reject(e, "only the author of the challenged claim can dispute its counterexample")
        if item["settled"] or e["ts"] >= item["resolve_ts"]:
            return self._reject(e, "the dispute window is closed")
        if item["disputes"]:
            return self._reject(e, "this counterexample is already disputed")
        evidence = self._evidence(body)
        if evidence is None:
            return self._reject(e, "a dispute needs an explanation")
        item["disputes"].append({"id": e["id"], "author": e["author"], "ts": e["ts"], "evidence": evidence})

    def _ruling(self, e):
        body = e["body"]
        tid = body.get("target")
        item = self.items.get(tid) if isinstance(tid, str) else None
        if item is None or item["mode"] != "counterexample":
            return self._reject(e, "a ruling must target a disputed counterexample")
        if e["author"] != self.missions[item["mission"]]["creator"]:
            return self._reject(e, "only the mission creator can rule on what the mission means")
        if not item["disputes"]:
            return self._reject(e, "nothing to rule on: the counterexample is not disputed")
        if e["ts"] <= item["disputes"][0]["ts"]:
            return self._reject(e, "event must be later than the dispute it answers")
        if item["settled"] or e["ts"] >= item["close_ts"]:
            return self._reject(e, "the ruling deadline has passed")
        if item["ruling"]:
            return self._reject(e, "this counterexample was already ruled on")
        decision, reason = body.get("decision"), body.get("reason", "")
        if decision not in ("upheld", "rejected") or not isinstance(reason, str):
            return self._reject(e, "decision must be upheld or rejected")
        item["ruling"] = {"id": e["id"], "decision": decision, "reason": reason[:5000], "ts": e["ts"]}

    def _open_item(self, e, kind, mission, target, evidence, stake, mode="evidence", **extra):
        domain = mission["domain"]
        self.balance(e["author"], domain)
        self.locked[(e["author"], domain)] = self.locked.get((e["author"], domain), 0) + stake
        self.items[e["id"]] = {
            "id": e["id"], "kind": kind, "mode": mode, "mission": mission["id"], "target": target,
            "author": e["author"], "ts": e["ts"], "close_ts": e["ts"] + mission["window_ms"], "stake": stake,
            "domain": domain, "evidence": evidence, "challengers": [], "settled": False, "upheld": None,
            "status": "pending", "refuted_by": None, "minted": 0, "credits": 0, "reason": None,
            "files": [], "vkey": None, "wkey": None, "witness": None, "verification": None, "resolve_ts": None,
            "disputes": [], "ruling": None,
            **extra,
        }
        self.pending.add(e["id"])

    # ---- settlement ------------------------------------------------------------
    def _ce_outcome(self, item, t):
        """Counterexample at time t: True = stands, False = fails, None = not decidable yet."""
        result = item["verification"]
        if result is None:
            return None                      # never guess: wait until this node has re-run it
        if result != "fail":                 # it passes, or the runs disagree: nothing reproducible
            return False if t >= item["resolve_ts"] else None
        if item["ruling"]:
            return item["ruling"]["decision"] == "upheld"
        if item["disputes"]:
            return True if t >= item["close_ts"] else None  # no ruling in time: the failing test stands
        return True if t >= item["resolve_ts"] else None

    def _proven(self, iid, t) -> bool:
        c = self.items[iid]
        if c["mode"] != "counterexample":
            return False
        return c["upheld"] is True if c["settled"] else self._ce_outcome(c, t) is True

    def _resolvable(self, iid, t) -> bool:
        item = self.items[iid]
        if item["settled"]:
            return True
        if item["mode"] == "counterexample":
            return self._ce_outcome(item, t) is not None or self.items[item["target"]]["settled"]
        if item["kind"] == "claim" and any(self._proven(c, t) for c in item["challengers"]):
            return True                      # a reproduced counterexample refutes before the window ends
        if t < item["close_ts"]:
            return False
        return all(self._resolvable(c, t) for c in item["challengers"])

    def _settle_counterexample(self, item, t):
        key = (item["author"], item["domain"])
        outcome = self._ce_outcome(item, t)
        self.locked[key] -= item["stake"]
        self.pending.discard(item["id"])
        item["settled"] = True
        if outcome is None:                  # its claim was decided by an earlier counterexample
            item["status"] = "moot"
            return
        item["upheld"] = outcome
        if outcome:
            item["status"] = "upheld"
            self._stat(*key)["found"] += 1
            return
        item["status"] = "rejected"
        item["reason"] = ("ruled rejected by the mission creator" if item["ruling"]
                          else "flaky: repeated runs disagreed" if item["verification"] == "flaky"
                          else "invalid: the counterexample also fails on the challenger's own witness"
                          if item["verification"] == "invalid"
                          else "not reproduced: the check passes with the counterexample")
        self._stat(*key)["failed_challenges"] += 1
        defender = self.items[item["target"]]["author"]
        self.balances[key] -= item["stake"]
        self.balances[(defender, item["domain"])] = self.balance(defender, item["domain"]) + item["stake"]

    def _settle(self, iid, t):
        item = self.items[iid]
        if item["settled"]:
            return
        if item["mode"] == "counterexample":
            return self._settle_counterexample(item, t)
        for c in item["challengers"]:
            if self._resolvable(c, t):
                self._settle(c, t)
        standing = sorted((self.items[c] for c in item["challengers"]
                           if self.items[c]["settled"] and self.items[c]["upheld"]),
                          key=lambda x: (x["ts"], x["id"]))
        key = (item["author"], item["domain"])
        self.locked[key] -= item["stake"]
        self.pending.discard(iid)
        item["settled"] = True
        item["upheld"] = not standing
        mission = self.missions[item["mission"]]
        if standing:
            winner = standing[0]
            self.balances[key] -= item["stake"]
            self.balances[(winner["author"], item["domain"])] = self.balance(winner["author"], item["domain"]) + item["stake"]
            item["refuted_by"] = winner["id"]
            if item["kind"] == "claim":
                item["status"] = "refuted"
                self._stat(item["author"], item["domain"])["refuted"] += 1
                if winner["mode"] == "counterexample" and self._trusted(mission):
                    reward = mission["manifest"].get("reward", {})
                    points = min(int(reward.get("points_per_find", 8)), int(self.config.get("max_points_per_find", 15)))
                    winner["minted"] = max(points, 0)
                    self.balances[(winner["author"], item["domain"])] += winner["minted"]
                if winner["mode"] == "counterexample" and (not mission["bounty"] or winner["ts"] < mission["bounty"]["expires_ts"]):
                    winner["credits"] = self._pay(mission, "find", winner["author"], winner["id"])
            else:
                item["status"] = "rejected"
                self._stat(item["author"], item["domain"])["failed_challenges"] += 1
            return
        if item["kind"] == "challenge":
            item["status"] = "upheld"
            self._stat(item["author"], item["domain"])["found"] += 1
            return
        item["status"] = "accepted"
        self._stat(item["author"], item["domain"])["built"] += 1
        if self._trusted(mission):
            reward = mission["manifest"].get("reward", {})
            points = min(int(reward.get("points_per_claim", 5)), int(self.config.get("max_points_per_claim", 10)))
            item["minted"] = max(points, 0)
            self.balances[key] += item["minted"]
        if item["target"] == "mission":
            if mission["status"] != "done":
                mission["status"], mission["done_claim"] = "done", iid
                if not mission["bounty"] or item["ts"] < mission["bounty"]["expires_ts"]:
                    item["credits"] = self._pay(mission, "build", item["author"], iid)
                self._close_bounty(mission, "done")
        else:
            work = mission["work"][item["target"]]
            if not work["done_claim"]:
                work["done_claim"] = iid

    def _with_ancestors(self, iid):
        """An item and everything it objects to, up the chain: their settling depends on it."""
        out = []
        while iid in self.items:
            out.append(iid)
            iid = self.items[iid]["target"]
        return out

    def _affected_by(self, iid):
        """Items whose settling can change when `iid` settles: its ancestors, and counterexamples against it
        (a counterexample whose claim is settled becomes moot)."""
        item = self.items[iid]
        return self._with_ancestors(item["target"]) + [c for c in item["challengers"]
                                                      if self.items[c]["mode"] == "counterexample"]

    def settle(self, t):
        """Settle every pending item that is resolvable at t: in passes ordered by (close_ts, id), until a
        pass settles nothing. Lazily: an item is only re-checked when something it depends on changed (a
        deadline of it or below it passed, a dependency settled, or a dispute or ruling arrived). Items that
        nothing touched were not resolvable before and still are not, so the outcome is the same as
        re-checking everything (tests/test_fold_speed.py compares against that original)."""
        if not self._dirty and t < self._next_due:
            return  # no event touched an item since the last check and no deadline has passed
        candidates = set(self.pending) if self._dirty else set()
        while self._deadlines and self._deadlines[0][0] <= t:
            _, iid = heapq.heappop(self._deadlines)
            if iid in self.pending:
                candidates.update(self._with_ancestors(iid))
        def key(i):
            return (self.items[i]["close_ts"], i)
        while candidates:
            current = [(key(i), i) for i in candidates if i in self.pending]
            heapq.heapify(current)
            candidates = set()  # what the next pass must look at again
            while current:
                k, iid = heapq.heappop(current)
                if iid not in self.pending or not self._resolvable(iid, t):
                    continue
                self._settle(iid, t)
                for j in self._affected_by(iid):
                    if j in self.pending:
                        if key(j) > k:
                            heapq.heappush(current, (key(j), j))  # still ahead in this pass
                        else:
                            candidates.add(j)                       # already passed: next pass
        for mission in self._bounty_missions:
            b = mission["bounty"]
            if not b["closed"] and t >= b["expires_ts"] and mission["status"] != "done":
                waiting = any(self.items[i]["target"] == "mission" and not self.items[i]["settled"]
                              and self.items[i]["ts"] < b["expires_ts"] for i in mission["claims"])
                if not waiting:  # work submitted in time is still paid when it is accepted later
                    self._close_bounty(mission, "expired")
        self._dirty = False
        while self._deadlines and self._deadlines[0][1] not in self.pending:
            heapq.heappop(self._deadlines)
        due = [self._deadlines[0][0]] if self._deadlines else []
        due += [m["bounty"]["expires_ts"] for m in self._bounty_missions
                if not m["bounty"]["closed"] and m["bounty"]["expires_ts"] > t]
        self._next_due = min(due, default=float("inf"))

    def credit_view(self, agent) -> dict:
        return {"agent": agent, "credits": self.credit(agent), "locked": self.credits_locked.get(agent, 0),
                "available": self.credits_available(agent)}

    # ---- views -------------------------------------------------------------------
    def resolve(self, prefix: str, pool) -> str:
        prefix = (prefix or "").strip().lower()
        matches = [k for k in pool if k.startswith(prefix)] if prefix else []
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise KeyError(f"nothing matches '{prefix}'")
        raise KeyError(f"'{prefix}' is ambiguous ({len(matches)} matches); use more characters")

    def mission_view(self, mid, now) -> dict:
        m = self.missions[mid]
        work = []
        for w in m["work"].values():
            pending = [i for i in m["claims"] if self.items[i]["target"] == w["id"] and not self.items[i]["settled"]]
            if w["done_claim"]:
                status = "done"
            elif pending:
                status = "in_review"
            elif w["claimed_by"] and w["lease_until"] > now:
                status = "claimed"
            else:
                status = "open"
            work.append({"id": w["id"], "title": w["title"], "description": w["description"], "status": status,
                         "claimed_by": w["claimed_by"] if status in ("claimed", "in_review") else None,
                         "lease_until_ms": w["lease_until"] if status == "claimed" else None})
        return {
            "mission_id": mid, "status": m["status"], "manifest": m["manifest"], "creator": m["creator"],
            "domain": m["domain"], "challenge_window_s": m["window_ms"] // 1000,
            "dispute_window_s": m["dispute_ms"] // 1000, "check": m["check"], "bounty": m["bounty"],
            "participants": [{"agent": a, **info} for a, info in m["participants"].items()],
            "work": work, "artifacts": m["artifacts"],
            "claims": [self.item_view(i) for i in m["claims"]],
        }

    def _claim_signal(self, item):
        """What re-running has shown about a pending claim, before stakes settle."""
        if item["settled"]:
            return None
        ces = [self.items[c] for c in item["challengers"] if self.items[c]["mode"] == "counterexample"]
        if any(c["verification"] == "fail" and not (c["ruling"] and c["ruling"]["decision"] == "rejected") for c in ces):
            return "failing"
        if any(c["verification"] is None and not c["settled"] for c in ces):
            return "awaiting_verification"
        return None

    def item_view(self, iid) -> dict:
        item = self.items[iid]
        view = {
            "id": iid, "kind": item["kind"], "mode": item["mode"], "target": item["target"], "author": item["author"],
            "status": item["status"], "stake": item["stake"], "closes_at_ms": item["close_ts"],
            "evidence": item["evidence"], "refuted_by": item["refuted_by"], "minted": item["minted"],
            "credits": item["credits"],
            "challenges": [self.item_view(c) for c in item["challengers"]],
        }
        if item["kind"] == "claim":
            view["verification_status"] = self._claim_signal(item)
        if item["mode"] == "counterexample":
            view.update({"files": item["files"], "witness": item["witness"],
                         "verification": LABELS.get(item["verification"], "unverified"),
                         "resolves_at_ms": item["resolve_ts"], "disputes": item["disputes"],
                         "ruling": item["ruling"], "reason": item["reason"]})
        return view

    def open_for_review(self, now, domain=None) -> list[dict]:
        """Claims and challenges still inside their challenge window: the review queue."""
        out = []
        for iid in self.pending:
            item = self.items[iid]
            if item["close_ts"] > now and (domain is None or item["domain"] == domain):
                row = {"id": iid, "kind": item["kind"], "mode": item["mode"], "mission": item["mission"],
                       "domain": item["domain"], "author": item["author"], "stake": item["stake"],
                       "closes_at_ms": item["close_ts"], "evidence": item["evidence"],
                       "runnable": bool(self.missions[item["mission"]]["check"])}
                if item["mode"] == "counterexample":
                    row["verification"] = LABELS.get(item["verification"], "unverified")
                out.append(row)
        return sorted(out, key=lambda x: (-x["stake"], x["closes_at_ms"]))

    def pending_verifications(self) -> list[dict]:
        """Counterexamples this node has not re-run yet."""
        out = []
        for iid in sorted(self.pending):
            item = self.items[iid]
            if item["mode"] == "counterexample" and item["verification"] is None:
                claim = self.items[item["target"]]
                out.append({"challenge": iid, "claim": claim["id"], "mission": item["mission"],
                            "deliverable": claim["evidence"]["cids"][0], "files": item["files"],
                            "witness": item["witness"]})
        return out

    def ranking(self, domain=None) -> dict:
        table: dict[str, list] = {}
        for (agent, dom), points in self.balances.items():
            if domain and dom != domain:
                continue
            s = self.stats.get((agent, dom), {"built": 0, "found": 0, "refuted": 0, "failed_challenges": 0})
            table.setdefault(dom, []).append({"agent": agent, "points": points, **s})
        for rows in table.values():
            rows.sort(key=lambda r: (-r["points"], -r["built"], -r["found"], r["agent"]))
        return table

    def season_ranking(self, start_ms: int, end_ms: int, domain=None) -> dict:
        """Points created by work done in [start, end): accepted builds and reproduced finds.
        Stakes that change hands are left out, so a newcomer can top a season."""
        table: dict[tuple, dict] = {}
        for item in self.items.values():
            if not item["settled"] or not (start_ms <= item["ts"] < end_ms) or (domain and item["domain"] != domain):
                continue
            row = table.setdefault((item["domain"], item["author"]), {
                "agent": item["author"], "points": 0, "built": 0, "found": 0, "refuted": 0, "failed_challenges": 0})
            if item["kind"] == "claim":
                if item["status"] == "accepted":
                    row["built"] += 1
                    row["points"] += item["minted"]
                elif item["status"] == "refuted":
                    row["refuted"] += 1
            elif item["status"] == "upheld":
                row["found"] += 1
                row["points"] += item["minted"]
            elif item["status"] == "rejected":
                row["failed_challenges"] += 1
        out: dict[str, list] = {}
        for (dom, _), row in table.items():
            out.setdefault(dom, []).append(row)
        for rows in out.values():
            rows.sort(key=lambda r: (-r["points"], -r["found"], -r["built"], r["agent"]))
        return out


MAX_CLOCK_SKEW_MS = 5 * 60 * 1000


def build_world(events: list[dict], config: dict, now: int, verifications: dict | None = None) -> World:
    world = World(config, verifications)
    for e in sorted(events, key=lambda x: (x["ts"], x["author"], x["seq"])):
        if e["ts"] > now + MAX_CLOCK_SKEW_MS:
            continue  # too far in the future; applied once time catches up
        world.settle(e["ts"])
        world.apply(e)
    world.settle(now)
    return world
