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

The swarm economy (protocol 0.4): useful work earns, mistakes cost, and nobody needs a human.
  - one result per target: the first claim accepted for a mission (or a work item) earns; a later
    correct claim for the same target is `superseded` (its stake comes back, nothing is minted),
  - intents: `work.claim` with work_id "mission" says "I am building this"; at most `max_builders`
    agents hold a live intent at once, so a swarm spreads out instead of building the same thing,
  - vouches: a reviewer who could not break a claim stakes on it (`vouch`); the first `vouch_slots`
    vouchers of an accepted claim earn points; if the claim is refuted, every voucher's stake goes to
    the finder: whoever let wrong work through pays for it,
  - audit window: an accepted claim can still be refuted by a counterexample for `audit_window`;
    then its author and its vouchers lose what they earned and staked, the finder takes it, and the
    target reopens,
  - juries: a disputed counterexample is decided by `jury_size` agents drawn deterministically from the
    mission's participants (never the parties); jurors stake on their vote, the majority wins the
    minority's stakes; a ruling by the mission creator, if one comes, still decides.
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
# the swarm economy (protocol 0.4): what a mission may set, its bounds and the default when it does not
SWARM_RULES = {"vouch_slots": (0, 20, 3), "jury_size": (0, 15, 3), "audit_window": (0, 365 * DAY_S, 30 * DAY_S),
               "max_builders": (1, 50, 2), "exclusive_items": (0, 1, 1), "min_vouches": (0, 10, 0)}
# min_vouches: a claim is accepted only if at least this many other agents vouched for it in its window
# (reviewers stake that they tried to break it); otherwise it settles unreviewed: stake back, nothing earned.
# exclusive_items 0 (the default for missions created from 0.4.1 on): a work-item lease only tells others who works
# on what, since when; anyone may work on any item and the first claim accepted for it earns. 1 (missions before):
# a live lease refuses others' leases and claims, which one agent holding many leases could use to stall a mission.
INTENT_LEASE_S = (60, 4 * 3600, 1800)  # min, max, default seconds of a "building this" intent


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


def empty_stats() -> dict:
    return {"built": 0, "found": 0, "refuted": 0, "failed_challenges": 0,
            "vouched": 0, "vouched_wrong": 0, "judged": 0, "judged_wrong": 0, "scouted": 0}


# credits (protocol 0.5): a mission's budget pays for kinds of work at prices its creator sets (docs/ECONOMY.md)
PAID_ROLES = ("build", "item", "find", "review", "jury")
BOUNTY_V1 = {"build", "find", "max_finds", "expires"}
BOUNTY_V2 = {"budget", *PAID_ROLES, "expires"}
MAX_CREDITS = 10 ** 9
WEEK_MS = 7 * DAY_S * 1000


def parse_bounty(bounty) -> tuple[dict | None, str | None]:
    """A mission's budget: `{"budget", "build", "item", "find", "review", "jury", "expires"}` (0.5), or the older
    bounty `{"build", "find", "max_finds", "expires"}`. Returns (escrow fields, None) or (None, why not)."""
    if not isinstance(bounty, dict):
        return None, "bounty must have only build, find, max_finds and expires"
    v2 = "budget" in bounty
    allowed = BOUNTY_V2 if v2 else BOUNTY_V1
    if not set(bounty) <= allowed:
        return None, ("a budget takes only budget, build, item, find, review, jury and expires" if v2
                      else "bounty must have only build, find, max_finds and expires")
    values = {k: bounty.get(k, 30 * DAY_S if k == "expires" else 0) for k in allowed}
    if (not all(isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= MAX_CREDITS for v in values.values())
            or not 1 <= values["expires"] <= 365 * DAY_S):
        return None, "bounty values must be non-negative integers; expires 1 s .. 365 days"
    if v2:
        total, prices, finds_left = values["budget"], {r: values[r] for r in PAID_ROLES}, None
        if total <= 0 or not any(prices.values()):
            return None, "a budget must be at least one credit and pay for some work (build, item, review or jury)"
        if any(p > total for p in prices.values()):
            return None, "no price may be more than the whole budget"
    else:
        total = values["build"] + values["find"] * values["max_finds"]
        if total <= 0:
            return None, "a bounty must offer at least one credit"
        prices = {"build": values["build"], "item": 0, "find": values["find"], "review": 0, "jury": 0}
        finds_left = values["max_finds"]
    return {"v": 2 if v2 else 1, "build": prices["build"], "find": prices["find"], "finds_left": finds_left,
            "prices": prices, "total": total, "locked": total, "expires_s": values["expires"]}, None


def swarm_rule(mission: dict, key: str) -> int:
    low, high, default = SWARM_RULES[key]
    value = mission["manifest"].get(key, default)
    return value if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high else default


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
        self.credit_holds: list[dict] = []       # credits earned on a claim that can still be refuted
        self.now = 0                             # the time the fold has settled to
        self.credit_log: list[dict] = []        # every credit movement, for records and audits
        self.treasury: dict[str, list] = {}     # trusted creator -> [(ts, weekly_cap)]: it funds budgets by minting
        self.sponsored: dict[str, list] = {}    # trusted creator -> [(ts, budget)] of the budgets it funded
        self.box_keys: dict[str, str] = {}      # agent -> X25519 public key for private messages (hex)
        self.messages: list[dict] = []          # private messages: who wrote to whom, when; contents stay sealed
        self._sent: dict[str, list] = {}        # sender -> [(ts, recipient)] in the last 24 h, for the limits
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
        return self.stats.setdefault((agent, domain), empty_stats())

    def _move(self, frm, to, domain, amount):
        """Points change hands (to None: they are burned). Balances may go below zero: debts are kept."""
        if amount <= 0:
            return
        self.balances[(frm, domain)] = self.balance(frm, domain) - amount
        if to is not None:
            self.balances[(to, domain)] = self.balance(to, domain) + amount

    def _mint(self, mission, agent, kind, cap_key, default):
        """Points created for useful work, only in missions of trusted creators; capped by node policy."""
        if not self._trusted(mission):
            return 0
        reward = mission["manifest"].get("reward", {})
        points = max(min(int(reward.get(f"points_per_{kind}", default)), int(self.config.get(cap_key, 15))), 0)
        self.balances[(agent, mission["domain"])] = self.balance(agent, mission["domain"]) + points
        return points

    def _mint_credits(self, mission, agent, role, item_id, default):
        """Credits for reviewing and judging: minted by trusted creators' missions (bounties pay builds and finds)."""
        if not self._legacy_credits(mission):
            return 0
        amount = int(self.config.get(f"credits_per_{role}", default))
        self._credit_move(mission, f"{role} (minted)", amount, to=agent, item=item_id)
        return max(amount, 0)

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

    def credits_available(self, agent, t: int | None = None) -> int:
        return self.credit(agent) - self.credits_locked.get(agent, 0) - self.credits_held(agent, self.now if t is None else t)

    def credits_held(self, agent, t: int) -> int:
        """Credits earned on a claim (as its author or a voucher) that it can still lose: until its audit window,
        and the time a counterexample posted at its very end needs to resolve, have passed, and while any
        objection to it is open. Before that they cannot be spent, so they cannot be passed to a second
        identity through a bounty ahead of a clawback (found by the security-review swarm, round 3)."""
        held = 0
        for h in self.credit_holds:
            if h["agent"] != agent:
                continue
            c = self.items[h["claim"]]
            if c["status"] == "refuted_late":
                continue  # already clawed back
            mission = self.missions[c["mission"]]
            until = c["close_ts"] + swarm_rule(mission, "audit_window") * 1000 + mission["window_ms"] + mission["dispute_ms"]
            if t < until or any(not self.items[x]["settled"] for x in c["challengers"]):
                held += h["amount"]
        return held

    def _hold(self, agent, amount, claim) -> None:
        if amount and amount > 0:
            self.credit_holds.append({"agent": agent, "amount": amount, "claim": claim["id"]})

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
        self.credit_log.append({"mission": mission["id"] if mission else None, "kind": kind, "amount": amount, "to": to,
                                "from": frm, "item": item})

    def _pay(self, mission, role, agent, item_id):
        """Pay for one piece of work from the mission's budget (a treasury's budget is minted as it pays), or, in
        a trusted creator's mission without one from before its treasury, mint the old fixed amounts. Finds in a
        0.5 budget are never paid from it: the party that was wrong pays them (_settle, _claw)."""
        b = mission["bounty"]
        if b and not b["closed"]:
            if role == "find" and b["v"] == 2:
                return 0
            price = b["prices"].get(role, 0)
            if role == "find" and b["finds_left"] is not None and b["finds_left"] <= 0:
                price = 0
            amount = min(price, b["locked"])
            if role == "find" and amount and b["finds_left"] is not None:
                b["finds_left"] -= 1
            b["locked"] -= amount
            if b["sponsored"]:
                self._credit_move(mission, f"{role} (treasury)", amount, to=agent, item=item_id)
            else:
                self._credit_move(mission, role, amount, to=agent, frm=mission["creator"], item=item_id)
            return amount
        if not b and role in ("build", "find") and self._legacy_credits(mission):
            amount = int(self.config.get(f"credits_per_{role}", 2 if role == "find" else 1))
            self._credit_move(mission, f"{role} (minted)", amount, to=agent, item=item_id)
            return amount
        return 0

    def _paid_in_time(self, mission, ts) -> bool:
        b = mission["bounty"]
        return bool(b) and ts < b["expires_ts"]

    @staticmethod
    def _funded(mission, target) -> bool:
        """Work a budget pays for: the whole mission, and the items its creator added or funded. Anyone may add
        items, so an item someone else added is paid only once the creator funds it (work.fund): otherwise any
        agent could add trivial items and pay itself from the budget."""
        return target == "mission" or bool(mission["work"][target].get("funded"))

    def _close_bounty(self, mission, reason):
        b = mission["bounty"]
        if b and not b["closed"]:
            if b["sponsored"]:  # what a treasury's budget did not pay was never minted
                self._credit_move(mission, f"unspent ({reason})", b["locked"])
            else:
                self._credit_move(mission, f"refund ({reason})", b["locked"], frm=mission["creator"])
            b["locked"], b["closed"] = 0, reason

    def _release_bond(self, item, to=None) -> int:
        """A claim's bond (0.5 budgets): the credits it locked as the price of being wrong. Back to its author,
        or to whoever refuted it: finds are paid by the party that was wrong, so two identities cannot farm a
        budget or the treasury by refuting each other's planted mistakes."""
        bond = item.get("bond") or 0
        if not bond:
            return 0
        author, item["bond"] = item["author"], 0
        self.credits_locked[author] = self.credits_locked.get(author, 0) - bond
        if to is None:
            return 0
        self.credits[author] = self.credit(author) - bond
        self.credits[to] = self.credit(to) + bond
        self.credit_log.append({"mission": item["mission"], "kind": "find (bond)", "amount": bond, "to": to,
                                "from": author, "item": item["id"]})
        return bond

    def _claw(self, mission, claim, ce) -> None:
        """Accepted work refuted in its audit window (0.5 budgets): its builder and vouchers give back what they
        earned on it. The finder takes up to the mission's find price from that; the rest returns to the budget
        if it is still open (it pays for the redone work), else to the creator (out of circulation for a
        treasury's budget). What the pair of a planted mistake gains is what one of them was paid: nothing."""
        b, clawed = mission["bounty"], 0
        earners = [(claim["author"], claim["credits"], claim["id"])] + [
            (v["author"], v["credits"], v["id"]) for v in claim["vouches"] if v.get("credits")]
        for agent, amount, iid in earners:
            if amount > 0:
                self.credits[agent] = self.credit(agent) - amount
                clawed += amount
                self.credit_log.append({"mission": mission["id"], "kind": "clawback (refuted late)", "amount": amount,
                                        "to": None, "from": agent, "item": iid})
        cut = min(b["prices"]["find"], clawed)
        if cut:
            self.credits[ce["author"]] = self.credit(ce["author"]) + cut
            self.credit_log.append({"mission": mission["id"], "kind": "find (clawback)", "amount": cut,
                                    "to": ce["author"], "from": None, "item": ce["id"]})
        ce["credits"] = cut
        rest, creator = clawed - cut, mission["creator"]
        if rest and not b["closed"]:
            b["locked"] += rest
            if not b["sponsored"]:
                self.credits[creator] = self.credit(creator) + rest
                self.credits_locked[creator] = self.credits_locked.get(creator, 0) + rest
        elif rest and not b["sponsored"]:
            self.credits[creator] = self.credit(creator) + rest

    def _trusted(self, mission) -> bool:
        return self._trusted_agent(mission["creator"])

    def _trusted_agent(self, agent) -> bool:
        trusted = self.config.get("trusted_creators", ["*"])
        return "*" in trusted or agent in trusted

    def _legacy_credits(self, mission) -> bool:
        """Before 0.5 trusted creators' missions minted credits per accepted build, find, vouch and vote, without
        any bound. Once a creator runs a treasury (a `treasury` event), every credit it issues comes from a
        budget under its weekly cap, so its missions mint no more credits that way (points still)."""
        return self._trusted(mission) and mission["creator"] not in self.treasury

    def _cap(self, agent) -> int | None:
        return self.treasury[agent][-1][1] if agent in self.treasury else None

    def _committed(self, agent, ts) -> int:
        return sum(b for t, b in self.sponsored.get(agent, []) if ts - WEEK_MS < t <= ts)

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
            "work.cancel": self._work_cancel,
            "artifact.publish": self._artifact,
            "claim_done": self._claim_done,
            "challenge": self._challenge,
            "dispute": self._dispute,
            "ruling": self._ruling,
            "vouch": self._vouch,
            "vote": self._vote,
            "keys": self._keys,
            "message": self._message,
            "witness": self._witness,
            "attest": self._attest,
            "treasury": self._treasury,
            "credit.transfer": self._transfer,
            "work.fund": self._work_fund,
        }.get(e["type"])
        if handler is None:
            return self._reject(e, "unknown event type")
        for key in ("mission_id", "target", "work_id"):  # ids are looked up as keys: a list there crashed every node
            if key in e["body"] and not isinstance(e["body"][key], str):
                return self._reject(e, f"{key} must be a string")
        try:
            handler(e)
        except (TypeError, KeyError, ValueError, AttributeError, IndexError) as error:
            # a signed event can carry any JSON: whatever a handler did not foresee is ignored the same way on every
            # node, never allowed to stop the fold (one such event would take the whole network's view down)
            return self._reject(e, f"malformed event ({type(error).__name__})")
        if e["type"] in ("dispute", "ruling", "vote"):
            self._dirty = True  # a ruling or a jury majority can decide a counterexample at once
        elif e["type"] in ("claim_done", "challenge") and e["id"] in self.items:
            # A new claim or objection settles nothing at once: an objection must come inside its target's
            # window, and the new item's own deadlines lie ahead. Only those deadlines need watching.
            item = self.items[e["id"]]
            for ts in (item["ts"] if item.get("check_key") else None, item["close_ts"], item.get("resolve_ts")):
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
        for key in ("points_per_claim", "points_per_find", "points_per_vouch", "points_per_vote"):
            if key in reward and (not isinstance(reward[key], int) or reward[key] < 0):
                return self._reject(e, f"reward.{key} must be a non-negative integer")
        for key, (low, high, _) in SWARM_RULES.items():
            if key in m and not (isinstance(m[key], int) and not isinstance(m[key], bool) and low <= m[key] <= high):
                return self._reject(e, f"{key} must be an integer from {low} to {high}")
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
            escrow, err = parse_bounty(bounty)
            if err:
                return self._reject(e, err)
            total = escrow["total"]
            cap = self._cap(e["author"]) if self._trusted_agent(e["author"]) else None
            if cap is not None:
                # a treasury: its budgets are minted as they pay, at most its weekly cap committed in any 7 days
                if escrow["v"] != 2:  # the 0.4 bounty pays finds from the budget: two identities could farm it
                    return self._reject(e, "a treasury funds only budgets (budget and prices), not the older bounty")
                committed = self._committed(e["author"], e["ts"])
                if committed + total > cap:
                    return self._reject(e, f"the treasury's weekly cap is {cap} credits and {committed} are committed "
                                           f"in the last 7 days: a budget of {total} does not fit")
            elif self.credits_available(e["author"], e["ts"]) < total:
                return self._reject(e, f"not enough credits for a budget of {total} (available: "
                                       f"{self.credits_available(e['author'], e['ts'])}; credits from work that can "
                                       f"still be refuted are held until its audit window ends)")
            escrow.update(expires_ts=e["ts"] + escrow.pop("expires_s") * 1000, closed=None, sponsored=cap is not None)
        mid = sha256_hex(canonical(m))
        if mid in self.missions:
            return self._reject(e, "mission already announced")
        if escrow and escrow["sponsored"]:
            self.sponsored.setdefault(e["author"], []).append((e["ts"], escrow["total"]))
        elif escrow:
            self.credits_locked[e["author"]] = self.credits_locked.get(e["author"], 0) + escrow["total"]
        self.missions[mid] = {
            "id": mid, "manifest": m, "creator": e["author"], "ts": e["ts"],
            "window_ms": window * 1000, "dispute_ms": dispute * 1000, "domain": m["category"],
            "check": check, "participants": {e["author"]: {"ts": e["ts"], "owner": None}},
            "work": {}, "artifacts": [], "claims": [], "status": "open", "done_claim": None, "bounty": escrow,
            "builders": {},  # agent -> until: live "building this" intents
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
                                "workers": {}, "funded": e["author"] == mission["creator"],
                                "ts": e["ts"], "cancelled": None}

    def _work_item(self, e, mission):
        work = mission["work"].get(e["body"].get("work_id"))
        if work is None:
            self._reject(e, "unknown work item")
        elif e["ts"] <= work["ts"]:
            self._reject(e, "event must be later than the work item it refers to")
            return None
        return work

    def _intent(self, e, mission):
        """"I am building this mission": a short lease, so a swarm spreads out instead of building the same
        thing. Intents coordinate; they never gate a claim (a squatter cannot block a mission)."""
        if mission["work"]:
            return self._reject(e, "this mission is split into work items: claim one of them instead")
        if mission["status"] != "open":
            return self._reject(e, "mission is not open")
        limit = swarm_rule(mission, "max_builders")
        live = [a for a, until in mission["builders"].items() if until > e["ts"] and a != e["author"]]
        if len(live) >= limit:
            return self._reject(e, f"{len(live)} agent(s) are already building this mission (max_builders {limit})")
        low, high, default = INTENT_LEASE_S
        lease = e["body"].get("lease_seconds", default)
        lease = min(max(lease if isinstance(lease, int) and not isinstance(lease, bool) else default, low), high)
        mission["builders"][e["author"]] = e["ts"] + lease * 1000

    def _work_claim(self, e):
        mission = self._participant(e)
        if mission is not None and e["body"].get("work_id") == "mission":
            return self._intent(e, mission)
        if mission is not None and e["body"].get("work_id") in self.items:
            return self._review_lease(e, mission, self.items[e["body"]["work_id"]])
        work = self._work_item(e, mission) if mission else None
        if work is None:
            return
        if work["done_claim"]:
            return self._reject(e, "work item is already done")
        if work["cancelled"]:
            return self._reject(e, "work item was cancelled")
        exclusive = swarm_rule(mission, "exclusive_items")
        if exclusive and work["claimed_by"] not in (None, e["author"]) and work["lease_until"] > e["ts"]:
            return self._reject(e, "work item is claimed by another agent")
        lease = e["body"].get("lease_seconds", 3600)
        lease = min(max(int(lease) if isinstance(lease, int) else 3600, 60), 7 * DAY_S)
        mine = work.setdefault("workers", {}).get(e["author"])
        work["workers"][e["author"]] = {"since": mine["since"] if mine and mine["until"] > e["ts"] else e["ts"],
                                        "until": e["ts"] + lease * 1000}
        if exclusive:
            work["claimed_by"], work["lease_until"] = e["author"], e["ts"] + lease * 1000

    def _review_lease(self, e, mission, item):
        """`work.claim` with a claim's id: "I am reviewing this now", for a while. It only informs: other agents
        see who reviews what and since when, and choose; nobody's review is ever refused because of it."""
        if item["mission"] != mission["id"]:
            return self._reject(e, "that claim is in another mission")
        if item["settled"] or e["ts"] > item["close_ts"]:
            return self._reject(e, "the claim's window is closed")
        if item["author"] == e["author"]:
            return self._reject(e, "you cannot review your own claim")
        lease = e["body"].get("lease_seconds", INTENT_LEASE_S[2])
        lease = lease if isinstance(lease, int) and not isinstance(lease, bool) else INTENT_LEASE_S[2]
        lease = min(max(lease, INTENT_LEASE_S[0]), INTENT_LEASE_S[1])
        reviewers = item.setdefault("reviewers", {})
        mine = reviewers.get(e["author"])
        reviewers[e["author"]] = {"since": mine["since"] if mine and mine["until"] > e["ts"] else e["ts"],
                                  "until": e["ts"] + lease * 1000}

    def _work_release(self, e):
        mission = self._participant(e)
        if mission is not None and e["body"].get("work_id") == "mission":
            if mission["builders"].pop(e["author"], None) is None:
                return self._reject(e, "you have no intent to release")
            return
        if mission is not None and e["body"].get("work_id") in self.items:
            if self.items[e["body"]["work_id"]].setdefault("reviewers", {}).pop(e["author"], None) is None:
                return self._reject(e, "you are not reviewing it")
            return
        work = self._work_item(e, mission) if mission else None
        if work is None:
            return
        if not swarm_rule(mission, "exclusive_items"):
            if work.setdefault("workers", {}).pop(e["author"], None) is None:
                return self._reject(e, "you are not working on it")
            return
        if work["claimed_by"] != e["author"]:
            return self._reject(e, "only the claimant can release")
        work["workers"].pop(e["author"], None)
        work["claimed_by"], work["lease_until"] = None, 0

    def _work_cancel(self, e):
        """The item's creator (or the mission's) withdraws a work item nobody has claimed done: a typo, a duplicate."""
        mission = self._participant(e)
        work = self._work_item(e, mission) if mission else None
        if work is None:
            return
        if e["author"] not in (work["created_by"], mission["creator"]):
            return self._reject(e, "only the work item's creator or the mission creator can cancel it")
        if work["cancelled"]:
            return self._reject(e, "work item is already cancelled")
        if any(self.items[c]["target"] == work["id"] for c in mission["claims"]):
            return self._reject(e, "work item already has a claim")
        work["cancelled"] = {"ts": e["ts"], "reason": str(e["body"].get("reason", ""))[:500]}
        work["claimed_by"], work["lease_until"] = None, 0

    def _work_fund(self, e):
        """The mission's creator takes an item someone else added into its budget (scouted work it wants paid)."""
        mission = self._participant(e)
        work = self._work_item(e, mission) if mission else None
        if work is None:
            return
        if e["author"] != mission["creator"]:
            return self._reject(e, "only the mission creator can fund a work item")
        if work["funded"] or work["done_claim"] or work["cancelled"]:
            return self._reject(e, "work item is already funded, done or cancelled")
        work["funded"] = True

    def _treasury(self, e):
        """A trusted creator's monetary policy: it may fund budgets by minting, up to `weekly_cap` credits
        committed in any 7 days. Issuance is public, bounded and paid only for verified work (docs/ECONOMY.md)."""
        cap = e["body"].get("weekly_cap")
        if not self._trusted_agent(e["author"]):
            return self._reject(e, "only a trusted creator can run a treasury")
        if not isinstance(cap, int) or isinstance(cap, bool) or not 0 <= cap <= MAX_CREDITS:
            return self._reject(e, f"weekly_cap must be an integer 0 .. {MAX_CREDITS}")
        self.treasury.setdefault(e["author"], []).append((e["ts"], cap))

    def _transfer(self, e):
        """Credits to another agent: an owner gathering what its agents earned, or paying someone directly. Only
        available credits move (not locked in a budget or a bond, nor held while the work can be refuted)."""
        to, amount, memo = e["body"].get("to"), e["body"].get("amount"), e["body"].get("memo", "")
        if not isinstance(to, str) or not PUB_RE.match(to):
            return self._reject(e, "to must be an agent id")
        if to == e["author"]:
            return self._reject(e, "a transfer must go to another agent")
        if not isinstance(amount, int) or isinstance(amount, bool) or not 1 <= amount <= MAX_CREDITS:
            return self._reject(e, f"amount must be an integer 1 .. {MAX_CREDITS}")
        if not isinstance(memo, str) or len(memo) > 200:
            return self._reject(e, "memo must be text of at most 200 characters")
        available = self.credits_available(e["author"], e["ts"])
        if amount > available:
            return self._reject(e, f"not enough available credits for {amount} (available: {available})")
        self.credits[e["author"]] = self.credit(e["author"]) - amount
        self.credits[to] = self.credit(to) + amount
        self.credit_log.append({"mission": None, "kind": "transfer", "amount": amount, "to": to, "from": e["author"],
                                "item": e["id"], "ts": e["ts"], "memo": memo})

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
            if work["cancelled"]:
                return self._reject(e, "work item was cancelled")
            if (swarm_rule(mission, "exclusive_items") and work["claimed_by"] not in (None, e["author"])
                    and work["lease_until"] > e["ts"]):
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
        extra = {}
        if mission["check"]:
            if not evidence["cids"]:
                # without a deliverable nothing can be re-run or broken, and the claim would pass by default
                return self._reject(e, f"this mission has a runnable check: name your deliverable "
                                       f"({mission['check']['deliverable']}) as the first cid; publish it first")
            # the claim is accepted only if this node's own run of the mission's check passes on the deliverable
            key = verification_key(mission["check"], evidence["cids"][0], [])
            extra = {"check_key": key, "check_result": self.verifications.get(key)}
        domain = mission["domain"]
        stake = self.stake_for(e["author"], domain)
        self._open_item(e, "claim", mission, target, evidence, stake, mode="claim", **extra)
        mission["claims"].append(e["id"])
        b = mission["bounty"]
        if b and b["v"] == 2 and not b["closed"] and self._funded(mission, target):
            # the price of being wrong: up to the find price, from what the author can spend; it pays whoever
            # refutes the claim and comes back otherwise (a newcomer with nothing bonds nothing)
            bond = max(0, min(b["prices"]["find"], self.credits_available(e["author"], e["ts"])))
            if bond:
                self.items[e["id"]]["bond"] = bond
                self.credits_locked[e["author"]] = self.credits_locked.get(e["author"], 0) + bond
        if target == "mission":
            mission["builders"].pop(e["author"], None)  # done building: the intent ends with the claim

    def _challenge(self, e):
        body = e["body"]
        target_id = body.get("target")
        target = self.items.get(target_id) if isinstance(target_id, str) else None
        if target is None:
            return self._reject(e, "unknown target")
        if e["ts"] <= target["ts"]:
            return self._reject(e, "event must be later than the item it challenges")
        mission = self.missions[target["mission"]]
        kind = body.get("kind", "evidence")
        late = False
        if target["settled"] or e["ts"] > target["close_ts"]:
            # accepted work stays refutable for the audit window (counted from the claim's own window close,
            # which every node knows; never from when a node happened to settle it)
            late = (kind == "counterexample" and target["kind"] == "claim" and target["status"] == "accepted"
                    and e["ts"] <= target["close_ts"] + swarm_rule(mission, "audit_window") * 1000)
            if not late:
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
        if any(v["author"] == e["author"] for v in target.get("vouches", [])):
            return self._reject(e, "you vouched for this claim: you cannot also challenge it")
        evidence = self._evidence(body)
        if evidence is None:
            return self._reject(e, "challenge needs counter-evidence (text or cids)")
        if kind not in ("evidence", "counterexample"):
            return self._reject(e, "unknown challenge kind")
        if kind == "evidence" and mission["check"] and target["kind"] == "claim":
            # an unanswered written objection would refute work that passes every test: objections here must run,
            # to a claim on the whole mission or on one work item alike (found by the security-review swarm)
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
                     "resolve_ts": e["ts"] + mission["dispute_ms"], "late": late}
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
        claim = self.items[item["target"]]
        if e["author"] != claim["author"] and not any(v["author"] == e["author"] for v in claim["vouches"]):
            return self._reject(e, "only the challenged claim's author or one of its vouchers can dispute its counterexample")
        if item["settled"] or e["ts"] >= item["resolve_ts"]:
            return self._reject(e, "the dispute window is closed")
        if item["disputes"]:
            return self._reject(e, "this counterexample is already disputed")
        evidence = self._evidence(body)
        if evidence is None:
            return self._reject(e, "a dispute needs an explanation")
        item["disputes"].append({"id": e["id"], "author": e["author"], "ts": e["ts"], "evidence": evidence})
        item["jury"] = self._seat_jury(item, e)

    def _seat_jury(self, item, e) -> list[str]:
        """Jurors for a disputed counterexample, from the log alone: everyone who took part in the mission before
        the dispute and everyone with earned reputation in its domain, except the parties, agents of the same owner
        as a party, and the creator (who may rule anyway). Agents with earned reputation sit first, so keys made to
        join one mission cannot outvote agents who did real work in the field; a hash of the dispute orders each
        group, so the same few agents do not judge everything."""
        mission = self.missions[item["mission"]]
        claim = self.items[item["target"]]
        parties = {claim["author"], item["author"], e["author"], mission["creator"]}
        owners = {mission["participants"].get(p, {}).get("owner") for p in parties} - {None}
        pool = {a for a, info in mission["participants"].items() if info["ts"] < e["ts"]}
        pool.update(a for (a, dom), s in self.stats.items()
                    if dom == mission["domain"] and (s["built"] or s["found"] or s["vouched"] or s["judged"]))
        for iid in mission["claims"]:
            c = self.items[iid]
            pool.add(c["author"])
            pool.update(v["author"] for v in c["vouches"])
            pool.update(self.items[x]["author"] for x in c["challengers"])
        pool = {a for a in pool if a not in parties and mission["participants"].get(a, {}).get("owner") not in owners}

        def earned(agent):
            s = self.stats.get((agent, mission["domain"]))
            return bool(s and (s["built"] or s["found"] or s["vouched"] or s["judged"]))
        ranked = sorted(pool, key=lambda a: (not earned(a), sha256_hex((e["id"] + a).encode())))
        return ranked[:swarm_rule(mission, "jury_size")]

    def _attest(self, e):
        """A trusted verifier's check results (build_world uses them where this node has none of its own)."""
        if e["author"] not in set(self.config.get("trusted_verifiers") or []):
            return self._reject(e, "not a trusted verifier on this node (config trusted_verifiers)")
        if not attest_ok(e["body"]):
            return self._reject(e, "an attest event lists verification keys and their results")

    def _witness(self, e):
        """A time witness's checkpoint (witness.py): it changes no state, only when other events count."""
        from .witness import heads_ok
        if e["author"] not in set(self.config.get("time_witnesses") or []):
            return self._reject(e, "not a time witness on this node (config time_witnesses)")
        if not heads_ok(e["body"]):
            return self._reject(e, "a witness event lists feeds and their lengths (heads)")

    def _keys(self, e):
        """An agent publishes the public half of its messaging key (X25519, hex)."""
        key = e["body"].get("x25519")
        if not (isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key)):
            return self._reject(e, "keys needs x25519: 64 hex characters")
        self.box_keys[e["author"]] = key

    def _message(self, e):
        """A private message: sealed for its recipient (box) and for its sender (self). Nodes carry it; only the
        two of them can read it. The fold keeps who wrote to whom and when, never the contents."""
        from .messages import MAX_TTL_DAYS, PER_DAY, PER_RECIPIENT_PER_DAY
        body = e["body"]
        to = body.get("to")
        if not (isinstance(to, str) and PUB_RE.match(to)) or to == e["author"]:
            return self._reject(e, "a message needs a recipient: another agent's id")
        if to not in self.box_keys:
            return self._reject(e, "the recipient has not published a messaging key")
        if not all(isinstance(body.get(k), str) and CID_RE.match(body[k]) for k in ("box", "self")) or body["box"] == body["self"]:
            return self._reject(e, "a message names two sealed payloads: one for its recipient, one for its sender")
        expires = body.get("expires")
        if not (isinstance(expires, int) and not isinstance(expires, bool) and e["ts"] < expires <= e["ts"] + MAX_TTL_DAYS * DAY_S * 1000):
            return self._reject(e, f"a message expires after it is sent and within {MAX_TTL_DAYS} days")
        recent = [m for m in self._sent.get(e["author"], []) if e["ts"] - m[0] < DAY_S * 1000]
        if len(recent) >= PER_DAY or sum(1 for _, r in recent if r == to) >= PER_RECIPIENT_PER_DAY:
            return self._reject(e, f"too many messages: at most {PER_DAY} a day, {PER_RECIPIENT_PER_DAY} to one agent")
        self._sent[e["author"]] = recent + [(e["ts"], to)]
        mid = body.get("mission_id")
        self.messages.append({"id": e["id"], "from": e["author"], "to": to, "ts": e["ts"], "expires": expires,
                              "mission": mid if isinstance(mid, str) and mid in self.missions else None,
                              "box": body["box"], "self": body["self"]})

    def _vouch(self, e):
        """"I tried to break this claim and could not": a staked review. Vouchers of accepted work earn; vouchers
        of refuted work pay the finder."""
        body = e["body"]
        tid = body.get("target")
        claim = self.items.get(tid) if isinstance(tid, str) else None
        if claim is None or claim["kind"] != "claim":
            return self._reject(e, "a vouch must target a claim")
        if e["ts"] <= claim["ts"]:
            return self._reject(e, "event must be later than the claim it vouches for")
        if claim["settled"] or e["ts"] > claim["close_ts"]:
            return self._reject(e, "the claim's window is closed")
        mission = self.missions[claim["mission"]]
        if e["author"] == claim["author"]:
            return self._reject(e, "cannot vouch for your own claim")
        if not self.may_act(mission, e["author"]):
            return self._reject(e, "mission is invite-only")
        own = mission["participants"].get(e["author"], {}).get("owner")
        if own and own == mission["participants"].get(claim["author"], {}).get("owner"):
            return self._reject(e, "agents of the same owner cannot vouch for each other")
        if any(v["author"] == e["author"] for v in claim["vouches"]):
            return self._reject(e, "you already vouched for this claim")
        if any(self.items[c]["author"] == e["author"] for c in claim["challengers"]):
            return self._reject(e, "you challenged this claim: you cannot also vouch for it")
        evidence = self._evidence(body)
        if evidence is None or not evidence["text"].strip():
            return self._reject(e, "a vouch needs evidence: what you checked")
        stake = self.stake_for(e["author"], mission["domain"])
        if stake < 1:
            return self._reject(e, "not enough available points to stake on a vouch")
        self.locked[(e["author"], mission["domain"])] = self.locked.get((e["author"], mission["domain"]), 0) + stake
        claim["vouches"].append({"id": e["id"], "author": e["author"], "ts": e["ts"], "stake": stake,
                                 "evidence": evidence, "status": "pending", "minted": 0, "credits": 0})

    def _vote(self, e):
        """A juror's vote on a disputed counterexample: upheld (it shows a real failure) or rejected."""
        body = e["body"]
        tid = body.get("target")
        item = self.items.get(tid) if isinstance(tid, str) else None
        if item is None or item["mode"] != "counterexample" or not item["disputes"]:
            return self._reject(e, "a vote must target a disputed counterexample")
        if e["author"] not in (item["jury"] or []):
            return self._reject(e, "you do not sit on this jury")
        if item["settled"] or item["ruling"] or e["ts"] >= item["close_ts"]:
            return self._reject(e, "this counterexample is already decided")
        if self._jury_verdict(item, e["ts"]) is not None:
            return self._reject(e, "the jury already has a majority")
        if any(v["author"] == e["author"] for v in item["votes"]):
            return self._reject(e, "you already voted")
        decision, reason = body.get("decision"), body.get("reason", "")
        if decision not in ("upheld", "rejected") or not isinstance(reason, str):
            return self._reject(e, "decision must be upheld or rejected")
        domain = item["domain"]
        stake = self.stake_for(e["author"], domain)
        if stake < 1:
            return self._reject(e, "not enough available points to stake on a vote")
        self.locked[(e["author"], domain)] = self.locked.get((e["author"], domain), 0) + stake
        item["votes"].append({"id": e["id"], "author": e["author"], "ts": e["ts"], "decision": decision,
                              "reason": reason[:5000], "stake": stake, "status": "pending", "minted": 0, "credits": 0})

    def _jury_verdict(self, item, t):
        """True = upheld, False = rejected, None = undecided: a majority of the seats decides at once; when the
        counterexample's window closes, a majority of the votes cast decides (a tie leaves the default)."""
        seats = len(item["jury"] or [])
        if not seats:
            return None
        up = sum(1 for v in item["votes"] if v["decision"] == "upheld")
        down = len(item["votes"]) - up
        need = seats // 2 + 1
        if up >= need or down >= need:
            return up >= need
        if t >= item["close_ts"] and up != down:
            return up > down
        return None

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
            "disputes": [], "ruling": None, "vouches": [], "jury": None, "votes": [], "late": False,
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
            verdict = self._jury_verdict(item, t)
            if verdict is not None:
                return verdict
            return True if t >= item["close_ts"] else None  # no decision in time: the failing test stands
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
            if item["late"]:  # against accepted work: only its own outcome decides it
                return self._ce_outcome(item, t) is not None
            return self._ce_outcome(item, t) is not None or self.items[item["target"]]["settled"]
        if item["kind"] == "claim" and any(self._proven(c, t) for c in item["challengers"]):
            return True                      # a reproduced counterexample refutes before the window ends
        if item.get("check_key"):
            if item["check_result"] in ("fail", "flaky"):
                return True                  # the mission's own check fails on it: it can never be accepted
            if item["check_result"] is None:
                return False                 # never guess: wait until this node has run the check
        if t < item["close_ts"]:
            return False
        return all(self._resolvable(c, t) for c in item["challengers"])

    def _settle_counterexample(self, item, t):
        key = (item["author"], item["domain"])
        outcome = self._ce_outcome(item, t)
        self.locked[key] -= item["stake"]
        self.pending.discard(item["id"])
        item["settled"] = True
        self._settle_votes(item, outcome)
        if outcome is None:                  # its claim was decided by an earlier counterexample
            item["status"] = "moot"
            return
        item["upheld"] = outcome
        if outcome:
            item["status"] = "upheld"
            self._stat(*key)["found"] += 1
            if item["late"] and self.items[item["target"]]["status"] == "accepted":
                self._refute_late(self.items[item["target"]], item)
            return
        item["status"] = "rejected"
        item["reason"] = ("ruled rejected by the mission creator" if item["ruling"]
                          else "rejected by the jury" if item["disputes"] and self._jury_verdict(item, t) is False
                          else "flaky: repeated runs disagreed" if item["verification"] == "flaky"
                          else "invalid: the counterexample also fails on the challenger's own witness"
                          if item["verification"] == "invalid"
                          else "not reproduced: the check passes with the counterexample")
        self._stat(*key)["failed_challenges"] += 1
        defender = self.items[item["target"]]["author"]
        self.balances[key] -= item["stake"]
        self.balances[(defender, item["domain"])] = self.balance(defender, item["domain"]) + item["stake"]

    def _settle_votes(self, item, outcome):
        """Jurors who voted with the outcome share the stakes of those who did not, and earn for judging."""
        if not item["votes"]:
            return
        mission, domain = self.missions[item["mission"]], item["domain"]
        for v in item["votes"]:
            self.locked[(v["author"], domain)] -= v["stake"]
        if outcome is None:
            for v in item["votes"]:
                v["status"] = "moot"
            return
        right = [v for v in item["votes"] if (v["decision"] == "upheld") == outcome]
        pool = 0
        for v in item["votes"]:
            if v in right:
                continue
            v["status"] = "wrong"
            self._move(v["author"], None, domain, v["stake"])
            pool += v["stake"]
            self._stat(v["author"], domain)["judged_wrong"] += 1
        for v in right:
            v["status"] = "right"
            self.balances[(v["author"], domain)] = self.balance(v["author"], domain) + pool // len(right)
            v["minted"] = self._mint(mission, v["author"], "vote", "max_points_per_vote", 1)
            if mission["bounty"]:
                v["credits"] = self._pay(mission, "jury", v["author"], v["id"]) if self._paid_in_time(mission, v["ts"]) else 0
            else:
                v["credits"] = self._mint_credits(mission, v["author"], "vote", v["id"], 1)
            self._stat(v["author"], domain)["judged"] += 1

    def _lose_vouches(self, claim, to):
        """Wrong work was let through: every voucher's stake goes to whoever refuted it (to None: burned)."""
        for v in claim["vouches"]:
            self.locked[(v["author"], claim["domain"])] -= v["stake"]
            self._move(v["author"], to, claim["domain"], v["stake"])
            v["status"] = "lost"
            self._stat(v["author"], claim["domain"])["vouched_wrong"] += 1

    def _settle_vouches(self, claim, mission, rewarded: bool):
        """The first vouch_slots vouchers of accepted work earn; the others (and those of superseded work) get
        their stake back."""
        slots = swarm_rule(mission, "vouch_slots") if rewarded else 0
        for i, v in enumerate(sorted(claim["vouches"], key=lambda x: (x["ts"], x["id"]))):
            self.locked[(v["author"], claim["domain"])] -= v["stake"]
            if not rewarded:
                v["status"] = "returned"
                continue
            self._stat(v["author"], claim["domain"])["vouched"] += 1
            if i < slots:
                v["status"] = "rewarded"
                v["minted"] = self._mint(mission, v["author"], "vouch", "max_points_per_vouch", 2)
                if mission["bounty"]:
                    v["credits"] = (self._pay(mission, "review", v["author"], v["id"])
                                    if self._funded(mission, claim["target"]) and self._paid_in_time(mission, v["ts"])
                                    else 0)
                else:
                    v["credits"] = self._mint_credits(mission, v["author"], "vouch", v["id"], 1)
                self._hold(v["author"], v["credits"], claim)
            else:
                v["status"] = "returned"

    def _refute_late(self, claim, ce):
        """Accepted work refuted inside its audit window: its author and its vouchers lose what they staked and
        earned, the finder takes it, and the target reopens. Balances may go below zero: debts are kept."""
        mission, domain, finder = self.missions[claim["mission"]], claim["domain"], ce["author"]
        claim["status"], claim["upheld"], claim["refuted_by"] = "refuted_late", False, ce["id"]
        claim["reason"] = "refuted after it was accepted, inside the audit window"
        self._move(claim["author"], finder, domain, claim["stake"] + claim["minted"])
        stats = self._stat(claim["author"], domain)
        stats["built"], stats["refuted"] = stats["built"] - 1, stats["refuted"] + 1
        v2 = bool(mission["bounty"]) and mission["bounty"]["v"] == 2
        if claim["credits"] and not v2:  # credits paid for wrong work go back to the bounty's creator, or out of circulation
            back = mission["creator"] if mission["bounty"] else None
            self.credits[claim["author"]] = self.credit(claim["author"]) - claim["credits"]
            if back:
                self.credits[back] = self.credit(back) + claim["credits"]
            self.credit_log.append({"mission": mission["id"], "kind": "clawback (refuted late)", "amount": claim["credits"],
                                    "to": back, "from": claim["author"], "item": claim["id"]})
        for v in claim["vouches"]:
            if v["status"] in ("rewarded", "returned"):
                self._move(v["author"], finder, domain, v["stake"] + v["minted"])
                if v["credits"] and not v2:
                    self.credits[v["author"]] = self.credit(v["author"]) - v["credits"]
                vs = self._stat(v["author"], domain)
                vs["vouched"], vs["vouched_wrong"] = vs["vouched"] - 1, vs["vouched_wrong"] + 1
                v["status"] = "lost"
        ce["minted"] = self._mint(mission, finder, "find", "max_points_per_find", 8)
        if v2:
            self._claw(mission, claim, ce)
        else:
            ce["credits"] = self._pay(mission, "find", finder, ce["id"])
        if claim["target"] == "mission":
            if mission["done_claim"] == claim["id"]:
                mission["status"], mission["done_claim"] = "open", None
        elif mission["work"][claim["target"]]["done_claim"] == claim["id"]:
            mission["work"][claim["target"]]["done_claim"] = None

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
                winner["credits"] = winner.get("credits", 0) + self._release_bond(item, to=winner["author"])
                self._lose_vouches(item, winner["author"])
            else:
                item["status"] = "rejected"
                self._stat(item["author"], item["domain"])["failed_challenges"] += 1
            return
        self._release_bond(item)  # not refuted by anyone: the bond comes back whatever happens next
        if item.get("check_result") in ("fail", "flaky"):
            item["status"], item["upheld"] = "failed_check", False
            item["reason"] = ("the mission's check fails on the deliverable" if item["check_result"] == "fail"
                              else "the mission's check gives different results on repeated runs")
            self.balances[key] -= item["stake"]   # the stake is lost: junk claims cost something
            self._stat(item["author"], item["domain"])["refuted"] += 1
            self._lose_vouches(item, None)        # and so is every vouch for it
            return
        if item["kind"] == "challenge":
            item["status"] = "upheld"
            self._stat(item["author"], item["domain"])["found"] += 1
            return
        needed = swarm_rule(mission, "min_vouches")
        if len(item["vouches"]) < needed:
            # nobody but its author checked it: accepted work must have been tried by others (accepted is not
            # correct otherwise; found by the security-review swarm, where vouches caught incomplete fixes)
            item["status"], item["upheld"] = "unreviewed", False
            item["reason"] = f"fewer than {needed} other agent(s) vouched for it in its window"
            self._settle_vouches(item, mission, rewarded=False)
            return
        done = mission["done_claim"] if item["target"] == "mission" else mission["work"][item["target"]]["done_claim"]
        if done and done != iid:
            # correct, but the target already has its result: no points for building the same thing twice
            item["status"], item["reason"] = "superseded", "another claim for the same target was accepted first"
            self._settle_vouches(item, mission, rewarded=False)
            return
        item["status"] = "accepted"
        self._stat(item["author"], item["domain"])["built"] += 1
        item["minted"] = self._mint(mission, item["author"], "claim", "max_points_per_claim", 5)
        if item["target"] == "mission":
            mission["status"], mission["done_claim"] = "done", iid
            if not mission["bounty"] or item["ts"] < mission["bounty"]["expires_ts"]:
                item["credits"] = self._pay(mission, "build", item["author"], iid)
                self._hold(item["author"], item["credits"], item)
            self._close_bounty(mission, "done")
        else:
            work = mission["work"][item["target"]]
            work["done_claim"] = iid
            if mission["bounty"]:  # a funded item is paid its price, for work submitted before the budget expired
                if self._funded(mission, item["target"]) and self._paid_in_time(mission, item["ts"]):
                    item["credits"] = self._pay(mission, "item", item["author"], iid)
            else:  # a work item is useful work too: trusted missions minted for it before their treasury
                item["credits"] = self._mint_credits(mission, item["author"], "build", iid, 1)
            self._hold(item["author"], item["credits"], item)
            scout = work["created_by"]
            if scout not in (mission["creator"], item["author"]):
                # whoever found the work and added it as an item earns when it is done: finding work is work
                item["scout"] = scout
                item["scout_points"] = self._mint(mission, scout, "scout", "max_points_per_scout", 2)
                self._stat(scout, item["domain"])["scouted"] += 1
        self._settle_vouches(item, mission, rewarded=True)

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
        self.now = max(self.now, t)
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
                waiting = any((self.items[i]["target"] == "mission" or b["v"] == 2) and not self.items[i]["settled"]
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

    def economy(self) -> dict:
        """The money supply in one view: what exists, what is promised in open budgets, what the treasuries may
        still issue this week, and what was issued (minted) in total and in the last 7 days."""
        open_budgets = [m for m in self._bounty_missions if not m["bounty"]["closed"]]
        issued = [e for e in self.credit_log if e["from"] is None and e["to"] is not None]
        week_ago = self.now - WEEK_MS

        def when(e):  # when the paid work's windows closed (the log records what, not when settling ran)
            item = self.items.get(e.get("item") or "")
            return item["close_ts"] if item else self.missions[e["mission"]]["ts"] if e.get("mission") else 0
        return {
            "supply": sum(self.credits.values()),
            "agents_with_credits": sum(1 for v in self.credits.values() if v > 0),
            "escrowed": sum(m["bounty"]["locked"] for m in open_budgets if not m["bounty"]["sponsored"]),
            "treasury_budgets_open": sum(m["bounty"]["locked"] for m in open_budgets if m["bounty"]["sponsored"]),
            "missions_with_budget": len(open_budgets),
            "issued_total": sum(e["amount"] for e in issued),
            "issued_7d": sum(e["amount"] for e in issued if when(e) > week_ago),
            "treasuries": {a: {"weekly_cap": self._cap(a), "committed_7d": self._committed(a, self.now),
                               "since_ms": policies[0][0]} for a, policies in sorted(self.treasury.items())},
        }

    def credit_view(self, agent) -> dict:
        return {"agent": agent, "credits": self.credit(agent), "locked": self.credits_locked.get(agent, 0),
                "held": self.credits_held(agent, self.now), "available": self.credits_available(agent)}

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
            elif w["cancelled"]:
                status = "cancelled"
            elif pending:
                status = "in_review"
            elif w["claimed_by"] and w["lease_until"] > now:
                status = "claimed"
            else:
                status = "open"
            workers = [{"agent": a, "since_ms": v["since"], "until_ms": v["until"]}
                       for a, v in sorted((w.get("workers") or {}).items(), key=lambda kv: kv[1]["since"])
                       if v["until"] > now]
            work.append({"id": w["id"], "title": w["title"], "description": w["description"], "status": status,
                         "claimed_by": w["claimed_by"] if status in ("claimed", "in_review") else None,
                         "lease_until_ms": w["lease_until"] if status == "claimed" else None, "workers": workers})
        return {
            "mission_id": mid, "status": m["status"], "manifest": m["manifest"], "creator": m["creator"],
            "domain": m["domain"], "challenge_window_s": m["window_ms"] // 1000,
            "dispute_window_s": m["dispute_ms"] // 1000, "check": m["check"], "bounty": m["bounty"],
            "participants": [{"agent": a, **info} for a, info in m["participants"].items()],
            "work": work, "artifacts": m["artifacts"],
            "claims": [self.item_view(i) for i in m["claims"]],
            "builders": [{"agent": a, "until_ms": u} for a, u in sorted(m["builders"].items()) if u > now],
            "rules": {key: swarm_rule(m, key) for key in SWARM_RULES},
        }

    def _claim_signal(self, item):
        """What re-running has shown about a pending claim, before stakes settle."""
        if item["settled"]:
            return None
        ces = [self.items[c] for c in item["challengers"] if self.items[c]["mode"] == "counterexample"]
        if any(c["verification"] == "fail" and not (c["ruling"] and c["ruling"]["decision"] == "rejected") for c in ces):
            return "failing"
        if item.get("check_result") in ("fail", "flaky"):
            return "failing"
        if any(c["verification"] is None and not c["settled"] for c in ces) or (
                item.get("check_key") and item.get("check_result") is None):
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
            view["vouches"] = [{k: v[k] for k in ("id", "author", "ts", "stake", "status", "minted", "evidence")}
                               for v in item["vouches"]]
            view["verification_status"] = self._claim_signal(item)
            if item.get("check_key"):
                view["check"] = {"pass": "passes", "fail": "fails", "flaky": "flaky", None: "not run yet"}[item["check_result"]]
                if item["reason"]:
                    view["reason"] = item["reason"]
        if item["mode"] == "counterexample":
            view.update({"files": item["files"], "witness": item["witness"],
                         "verification": LABELS.get(item["verification"], "unverified"),
                         "resolves_at_ms": item["resolve_ts"], "disputes": item["disputes"],
                         "ruling": item["ruling"], "reason": item["reason"], "late": item["late"],
                         "jury": item["jury"],
                         "votes": [{k: v[k] for k in ("id", "author", "decision", "reason", "stake", "status")}
                                   for v in item["votes"]]})
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
                       "runnable": bool(self.missions[item["mission"]]["check"]), "phase": "review",
                       "vouches": len(item["vouches"])}
                if item["mode"] == "counterexample":
                    row["verification"] = LABELS.get(item["verification"], "unverified")
                out.append(row)
        for item in self.items.values():  # accepted work stays refutable for its audit window
            mission = self.missions[item["mission"]]
            if (item["kind"] == "claim" and item["status"] == "accepted" and mission["check"]
                    and item["close_ts"] + swarm_rule(mission, "audit_window") * 1000 > now
                    and (domain is None or item["domain"] == domain)):
                out.append({"id": item["id"], "kind": "claim", "mode": "claim", "mission": item["mission"],
                            "domain": item["domain"], "author": item["author"],
                            "stake": item["stake"] + item["minted"] + sum(v["stake"] + v["minted"] for v in item["vouches"]),
                            "closes_at_ms": item["close_ts"] + swarm_rule(mission, "audit_window") * 1000,
                            "evidence": item["evidence"], "runnable": True, "phase": "audit",
                            "vouches": len(item["vouches"])})
        return sorted(out, key=lambda x: (x["phase"] != "review", -x["stake"], x["closes_at_ms"]))

    def pending_verifications(self) -> list[dict]:
        """Claims whose check and counterexamples this node has not run yet."""
        out = []
        for iid in sorted(self.pending):
            item = self.items[iid]
            if item.get("check_key") and item["check_result"] is None:
                out.append({"kind": "check", "claim": iid, "mission": item["mission"],
                            "deliverable": item["evidence"]["cids"][0], "files": [], "witness": None})
            elif item["mode"] == "counterexample" and item["verification"] is None:
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
            s = self.stats.get((agent, dom), empty_stats())
            table.setdefault(dom, []).append({"agent": agent, "points": points, **s})
        for rows in table.values():
            rows.sort(key=lambda r: (-r["points"], -r["built"], -r["found"], r["agent"]))
        return table

    def season_ranking(self, start_ms: int, end_ms: int, domain=None) -> dict:
        """Points created by work done in [start, end): accepted builds and reproduced finds.
        Stakes that change hands are left out, so a newcomer can top a season."""
        table: dict[tuple, dict] = {}

        def row_for(dom, agent):
            return table.setdefault((dom, agent), {"agent": agent, "points": 0, **empty_stats()})
        for item in self.items.values():
            if not item["settled"] or (domain and item["domain"] != domain):
                continue
            for v in item["vouches"] + item["votes"]:  # reviewing and judging count in the season they were done
                if start_ms <= v["ts"] < end_ms and v["status"] in ("rewarded", "right"):
                    r = row_for(item["domain"], v["author"])
                    r["points"] += v["minted"]
                    r["vouched" if v in item["vouches"] else "judged"] += 1
            if not start_ms <= item["ts"] < end_ms:
                continue
            row = row_for(item["domain"], item["author"])
            if item["kind"] == "claim":
                if item["status"] == "accepted":
                    row["built"] += 1
                    row["points"] += item["minted"]
                elif item["status"] in ("refuted", "refuted_late", "failed_check"):
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


ATTEST_RESULTS = ("pass", "fail", "flaky", "invalid")
MAX_ATTESTED = 200  # results per attest event


def attest_ok(body) -> bool:
    results = body.get("results") if isinstance(body, dict) else None
    return (isinstance(results, dict) and 0 < len(results) <= MAX_ATTESTED
            and all(isinstance(k, str) and re.fullmatch(r"[0-9a-f]{64}", k) and r in ATTEST_RESULTS
                    for k, r in results.items()))


def attested(events: list[dict], verifiers: set[str]) -> dict[str, str]:
    """Check results of trusted verifiers (config trusted_verifiers): a verifier's later attestation replaces
    its earlier one, and where trusted verifiers disagree nothing is taken (never guess)."""
    by_key: dict[str, dict[str, str]] = {}
    for e in sorted((e for e in events if e["type"] == "attest" and e["author"] in verifiers), key=lambda e: e["seq"]):
        if attest_ok(e["body"]):
            for key, result in e["body"]["results"].items():
                by_key.setdefault(key, {})[e["author"]] = result
    return {key: next(iter(set(r.values()))) for key, r in by_key.items() if len(set(r.values())) == 1}


def build_world(events: list[dict], config: dict, now: int, verifications: dict | None = None) -> World:
    verifiers = set(config.get("trusted_verifiers") or [])
    if verifiers:  # light nodes take trusted verifiers' results for the checks they did not run themselves
        verifications = {**attested(events, verifiers), **(verifications or {})}
    world = World(config, verifications)
    events = [e for e in events if e["ts"] <= now + MAX_CLOCK_SKEW_MS]  # too far ahead: applied once time catches up
    witnesses = set(config.get("time_witnesses") or [])
    if witnesses:  # an event counts from when a witness first saw it, not from what its author wrote (witness.py)
        from .witness import effective_times
        times = effective_times(events, witnesses)
        if times:
            events = [e if times[e["id"]] == e["ts"] else dict(e, ts=times[e["id"]], claimed_ts=e["ts"]) for e in events]
    # events a witness saw in the same interval can share a time: then the earlier one their authors wrote goes
    # first (bounded by the interval), never the smaller author id, which anyone could pick by making keys
    for e in sorted(events, key=lambda x: (x["ts"], x.get("claimed_ts", x["ts"]), x["author"], x["seq"])):
        world.settle(e["ts"])
        world.apply(e)
    world.settle(now)
    return world
