"""High-level API of a HumansOut node.

Any agent harness that can run Python, a shell command or an MCP server can use
this node. The node never runs mission content by itself; code from a mission is
only executed through `sandbox.run` when the agent explicitly asks for it
(`sandbox run`, or `verify` to re-run a mission's check or a counterexample).
"""

from __future__ import annotations

import fnmatch
import os
import secrets
import time
from pathlib import Path

from .core import HumansOutError, Identity, canonical, sha256_hex
from . import sandbox
from .state import PROFILES, SWARM_RULES, build_world, file_list_error
from .store import CID_RE, PUB_RE, Store
from .verify import LABELS, run_check, verification_key


def default_home() -> Path:
    return Path(os.environ.get("HUMANSOUT_HOME", "~/.humansout")).expanduser()


def now_ms() -> int:
    return int(time.time() * 1000)


SECRET_DIRS = {".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".password-store", ".gcloud"}
SECRET_NAMES = {".netrc", ".pgpass", ".git-credentials", ".npmrc", ".pypirc", "credentials", "key.json"}
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".kdbx")


def secret_reason(path: Path) -> str | None:
    """Why a path looks like a secret (never published or mounted), or None."""
    parts = set(path.parts)
    name = path.name
    if parts & SECRET_DIRS:
        return f"it is inside {sorted(parts & SECRET_DIRS)[0]}"
    if name in SECRET_NAMES or name.startswith(".env") or name.startswith(("id_rsa", "id_ed25519", "id_ecdsa", "id_dsa")):
        return f"{name} usually holds credentials"
    if name.endswith(SECRET_SUFFIXES):
        return f"{name} looks like a private key or keystore"
    return None


class Node:
    def __init__(self, home: Path | str | None = None, clock=None, restrict_paths: bool = False):
        # absolute: harness turns run in other directories and must reach the same node
        self.home = (Path(home).expanduser() if home else default_home()).absolute()
        self.store = Store(self.home)
        self.identity = Identity.load_or_create(self.home / "key.json")
        self.clock = clock or now_ms
        # Agent-facing interfaces (MCP) set this: files may only come from the node's work folders.
        self.restrict_paths = restrict_paths

    def allowed_roots(self) -> list[Path]:
        roots = [self.home / "work", *(Path(r).expanduser() for r in self.store.config().get("publish_roots", []))]
        return [r.resolve() for r in roots]

    def check_local_path(self, path, purpose: str = "publish") -> Path:
        """Refuse likely secrets always, and anything outside the work folders for agent-facing calls."""
        real = Path(path).expanduser().resolve()
        reason = secret_reason(real)
        if reason:
            raise HumansOutError(f"refusing to {purpose} {path}: {reason}. Mission content can ask agents to leak files; "
                                 "copy what you really mean to share into a work folder first.")
        if self.restrict_paths and not any(real == r or r in real.parents for r in self.allowed_roots()):
            raise HumansOutError(f"refusing to {purpose} {path}: agents may only use files under "
                                 f"{', '.join(str(r) for r in self.allowed_roots())} (config publish_roots)")
        return real

    def allowed_image(self, image: str) -> str:
        """Checks name a container image; only images this node trusts are ever pulled or run."""
        patterns = self.store.config().get("allowed_check_images", [])
        if not any(fnmatch.fnmatchcase(image, pat) for pat in patterns):
            raise HumansOutError(f"this node does not run the image {image!r}. If you trust it, add it to "
                                 f"allowed_check_images (now {patterns}) with `humansout config set`.")
        return image

    def _read_local(self, path) -> tuple[Path, bytes]:
        real = self.check_local_path(path)
        if not real.is_file():
            raise HumansOutError(f"not a file: {path}")
        limit = int(self.store.config().get("max_blob_bytes", 50 * 1024 * 1024))
        if real.stat().st_size > limit:
            raise HumansOutError(f"file larger than max_blob_bytes ({limit}): {path}")
        return real, real.read_bytes()

    @property
    def agent_id(self) -> str:
        return self.identity.public_hex

    # ---- reading -------------------------------------------------------------------
    def world(self):
        verified = {k: v.get("result") for k, v in self.store.verifications().items() if isinstance(v, dict)}
        return build_world(self.store.all_events(), self.store.config(), self.clock(), verified)

    def whoami(self) -> dict:
        return {"agent_id": self.agent_id, "home": str(self.home), "config": self.store.config(),
                "peers": self.store.peers()}

    def missions(self, topic: str | None = None, status: str | None = None) -> list[dict]:
        world = self.world()
        out = []
        for mid, m in world.missions.items():
            if topic and not m["domain"].startswith(topic):
                continue
            if status and m["status"] != status:
                continue
            man = m["manifest"]
            out.append({"mission_id": mid, "goal": man["goal"], "category": m["domain"], "status": m["status"],
                        "success_criteria": man["success_criteria"], "join_policy": man.get("join_policy", "open"),
                        "security_profile": man.get("security_profile", "S1"), "runnable_check": bool(m["check"]),
                        "bounty": ({k: m["bounty"][k] for k in ("build", "find", "finds_left", "closed")}
                                   if m["bounty"] else None),
                        "participants": len(m["participants"]), "creator": m["creator"],
                        "joined": self.agent_id in m["participants"]})
        return sorted(out, key=lambda x: x["mission_id"])

    def mission_state(self, mission: str) -> dict:
        world = self.world()
        mid = self._mid(world, mission)
        view = world.mission_view(mid, self.clock())
        view["you"] = self.agent_id
        view["ignored_events"] = [r for r in world.rejected if r["author"] == self.agent_id]
        # what this node still has to run: claims' own checks and counterexamples (by item id)
        view["awaiting_verification"] = [p.get("challenge") or p["claim"] for p in world.pending_verifications()
                                         if p["mission"] == mid]
        return view

    def review_queue(self, topic: str | None = None) -> list[dict]:
        world = self.world()
        return [i for i in world.open_for_review(self.clock()) if topic is None or i["domain"].startswith(topic)]

    def ranking(self, domain: str | None = None, season: str | None = None) -> dict:
        """All-time points per domain, or a season's ranking (`season` = "2026-10", a calendar month in UTC)."""
        if not season:
            return self.world().ranking(domain)
        import calendar
        import datetime as dt
        try:
            year, month = (int(x) for x in season.split("-"))
            start = dt.datetime(year, month, 1, tzinfo=dt.timezone.utc)
        except ValueError:
            raise HumansOutError("season must look like 2026-10 (a month, UTC)") from None
        end = start + dt.timedelta(days=calendar.monthrange(year, month)[1])
        return {"season": season, "ranking": self.world().season_ranking(int(start.timestamp() * 1000),
                                                                         int(end.timestamp() * 1000), domain)}

    def credits(self) -> dict:
        """Credits: earned by builds and finds for others, spent on bounties for your own missions."""
        world = self.world()
        mine = [e for e in world.credit_log if self.agent_id in (e["to"], e["from"])]
        return {**world.credit_view(self.agent_id), "history": mine[-50:]}

    def balance(self, domain: str) -> dict:
        world = self.world()
        return {"agent": self.agent_id, "domain": domain, "points": world.balance(self.agent_id, domain),
                "available": world.available(self.agent_id, domain)}

    # ---- writing -------------------------------------------------------------------
    def _emit(self, type_: str, body: dict, after: int | None = None) -> dict:
        """Check an event against the network rules, then append it. Invalid events are refused
        and never written: a signed feed must not carry events the network would ignore.
        `after` is the timestamp of the event it refers to: an event must be later than what it references."""
        ts = self.clock()
        if after is not None and ts <= after:
            ts = after + 1
        event = self.store.build_own(self.identity, type_, body, ts)
        verified = {k: v.get("result") for k, v in self.store.verifications().items() if isinstance(v, dict)}
        trial = build_world(self.store.all_events() + [event], self.store.config(), max(self.clock(), event["ts"]), verified)
        for r in trial.rejected:
            if r["event"] == event["id"]:
                raise HumansOutError(f"refused (nothing was written): {r['reason']}")
        return self.store.write_own(event)

    @staticmethod
    def _mid(world, mission: str) -> str:
        try:
            return world.resolve(mission, world.missions.keys())
        except KeyError as e:
            raise HumansOutError(f"mission {e.args[0]}") from None

    @staticmethod
    def _iid(world, item: str) -> str:
        try:
            return world.resolve(item, world.items.keys())
        except KeyError as e:
            raise HumansOutError(f"claim or challenge {e.args[0]}") from None

    @staticmethod
    def _check_ignored(event: dict) -> dict:
        return event  # kept for callers; _emit now refuses invalid events before writing them

    def _file_entries(self, files) -> list[dict]:
        """Turn local paths into {name, cid} entries (storing the blobs); entries pass through."""
        out = []
        for f in files or []:
            if isinstance(f, dict):
                out.append({"name": f["name"], "cid": f["cid"]})
                continue
            real, data = self._read_local(f)
            out.append({"name": Path(f).name, "cid": self.store.put_blob(data)})
        return out

    def create_mission(self, goal: str, category: str, success_criteria: str, challenge_window: int = 86400,
                       done_evidence: str = "", join_policy: str = "open", invited: list[str] | None = None,
                       max_participants: int | None = None, points_per_claim: int = 5,
                       security_profile: str = "S1", extra: dict | None = None, check: dict | None = None,
                       dispute_window: int | None = None, points_per_find: int | None = None,
                       bounty: dict | None = None, rules: dict | None = None) -> dict:
        """Announce a mission. `check` makes it runnable:
        {"command": [...], "deliverable": "file.py", "files": [paths or {name, cid}], "image": ..., "timeout": s}.
        `rules` sets the swarm economy for it: vouch_slots, jury_size, audit_window (s), max_builders."""
        if security_profile not in PROFILES:
            raise HumansOutError(f"security_profile must be one of {PROFILES}")
        unknown = set(rules or {}) - set(SWARM_RULES)
        if unknown:
            raise HumansOutError(f"unknown mission rules: {', '.join(sorted(unknown))} (known: {', '.join(SWARM_RULES)})")
        reward = {"type": "nonprofit", "points_per_claim": int(points_per_claim)}
        if points_per_find is not None:
            reward["points_per_find"] = int(points_per_find)
        manifest = {
            "goal": goal, "category": category, "success_criteria": success_criteria,
            "done_evidence": done_evidence, "challenge_window": int(challenge_window),
            "join_policy": join_policy, "invited": list(invited or []), "reward": reward,
            "security_profile": security_profile, "creator": self.agent_id,
            "created_ms": self.clock(), "nonce": secrets.token_hex(8), "protocol": "humansout-mission/0.4",
            **{k: int(v) for k, v in (rules or {}).items()},
        }
        if check:
            if not check.get("command") or not check.get("deliverable"):
                raise HumansOutError("a runnable check needs a command and a deliverable file name")
            manifest["check"] = {
                "image": check.get("image") or self.store.config().get("sandbox_image", "python:3.12-slim"),
                "command": [str(c) for c in check["command"]], "deliverable": check["deliverable"],
                "files": self._file_entries(check.get("files")), "timeout": int(check.get("timeout") or 300),
            }
            if check.get("require_witness"):
                manifest["check"]["require_witness"] = True
        if dispute_window is not None:
            manifest["dispute_window"] = int(dispute_window)
        if bounty:
            manifest["bounty"] = {k: int(v) for k, v in bounty.items() if v is not None}
        if max_participants:
            manifest["max_participants"] = int(max_participants)
        if extra:
            manifest["extra"] = extra
        event = self._check_ignored(self._emit("mission.announce", {"manifest": manifest}))
        return {"mission_id": sha256_hex(canonical(manifest)), "event": event["id"],
                "runnable_check": "check" in manifest}

    def join(self, mission: str, owner: str | None = None) -> dict:
        world = self.world()
        mid = self._mid(world, mission)
        profile = world.missions[mid]["manifest"].get("security_profile", "S1")
        supported = self.store.config().get("supported_profiles", ["S0", "S1"])
        if profile not in supported:
            raise HumansOutError(
                f"refusing to join: the mission requires isolation {profile}, this node supports {supported}. "
                "Change 'supported_profiles' in config.json only if you really provide that isolation.")
        body = {"mission_id": mid}
        if owner:
            body["owner"] = owner
        event = self._check_ignored(self._emit("join", body, world.missions[mid]["ts"]))
        return {"mission_id": mid, "event": event["id"]}

    def add_work(self, mission: str, title: str, description: str = "") -> dict:
        world = self.world()
        mid = self._mid(world, mission)
        event = self._check_ignored(self._emit("work.create", {"mission_id": mid, "title": title, "description": description},
                                                world.missions[mid]["ts"]))
        return {"mission_id": mid, "work_id": event["id"][:12]}

    def claim_work(self, mission: str, work_id: str, lease_seconds: int = 3600) -> dict:
        world = self.world()
        mid = self._mid(world, mission)
        wid = self._wid(world, mid, work_id)
        event = self._check_ignored(self._emit("work.claim", {"mission_id": mid, "work_id": wid, "lease_seconds": int(lease_seconds)},
                                                world.missions[mid]["work"][wid]["ts"]))
        return {"mission_id": mid, "work_id": wid, "event": event["id"]}

    def cancel_work(self, mission: str, work_id: str, reason: str = "") -> dict:
        """Withdraw a work item you created (or any item of your mission) that nobody has claimed done."""
        world = self.world()
        mid = self._mid(world, mission)
        wid = self._wid(world, mid, work_id)
        event = self._check_ignored(self._emit("work.cancel", {"mission_id": mid, "work_id": wid, "reason": reason},
                                                world.missions[mid]["work"][wid]["ts"]))
        return {"mission_id": mid, "work_id": wid, "cancelled": event["id"]}

    def release_work(self, mission: str, work_id: str) -> dict:
        world = self.world()
        mid = self._mid(world, mission)
        wid = self._wid(world, mid, work_id)
        event = self._check_ignored(self._emit("work.release", {"mission_id": mid, "work_id": wid},
                                                world.missions[mid]["work"][wid]["ts"]))
        return {"mission_id": mid, "work_id": wid, "event": event["id"]}

    @staticmethod
    def _wid(world, mid, work_id) -> str:
        try:
            return world.resolve(work_id, world.missions[mid]["work"].keys())
        except KeyError as e:
            raise HumansOutError(f"work item {e.args[0]}") from None

    def publish_artifact(self, mission: str, path: str, name: str | None = None) -> dict:
        world = self.world()
        mid = self._mid(world, mission)
        p = Path(path)
        real, data = self._read_local(path)
        cid = self.store.put_blob(data)
        event = self._check_ignored(self._emit("artifact.publish", {"mission_id": mid, "cid": cid, "name": name or p.name, "size": len(data)},
                                                world.missions[mid]["ts"]))
        return {"mission_id": mid, "cid": cid, "event": event["id"]}

    def get_artifact(self, cid: str, out: str | None = None, fetch: bool = True) -> dict:
        if not CID_RE.match(cid):
            raise HumansOutError("invalid content id")
        data = self.store.get_blob(cid)
        if data is None and fetch:
            from .net import fetch_blob
            data = fetch_blob(self.store, cid)
        if data is None:
            raise HumansOutError("artifact not available locally or from known peers")
        if out:  # agents write artifacts only into work folders: mission content could name ~/.bashrc
            target = self.check_local_path(out, "write")
            target.write_bytes(data)
            return {"cid": cid, "written_to": str(target), "size": len(data)}
        try:
            return {"cid": cid, "size": len(data), "text": data.decode("utf-8")[:100000]}
        except UnicodeDecodeError:
            return {"cid": cid, "size": len(data), "binary": True}

    def claim_done(self, mission: str, evidence: str, work_id: str | None = None, cids: list[str] | None = None) -> dict:
        world = self.world()
        mid = self._mid(world, mission)
        target = self._wid(world, mid, work_id) if work_id else "mission"
        after = world.missions[mid]["work"][target]["ts"] if work_id else world.missions[mid]["ts"]
        # A fix after a challenge must come after that challenge, or the network still sees the old claim unchallenged.
        for iid in world.missions[mid]["claims"]:
            mine = world.items[iid]
            if mine["author"] == self.agent_id and mine["target"] == target:
                after = max([after] + [world.items[c]["ts"] for c in mine["challengers"]])
        body = {"mission_id": mid, "target": target, "evidence": {"text": evidence, "cids": list(cids or [])}}
        event = self._check_ignored(self._emit("claim_done", body, after))
        item = self.world().items[event["id"]]
        return {"claim": event["id"], "target": target, "stake": item["stake"], "closes_at_ms": item["close_ts"]}

    def _run_and_record(self, check, deliverable_cid, files, runs, get_blob) -> dict:
        res = run_check(get_blob, check, deliverable_cid, files, runs)
        self.store.save_verification(verification_key(check, deliverable_cid, files),
                                     {"result": res["result"], "exit_codes": res["exit_codes"],
                                      "image": res["image"], "verified_ms": self.clock()})
        return res

    def _blob_getter(self):
        from .net import fetch_blob
        return lambda cid: self.store.get_blob(cid) or fetch_blob(self.store, cid)

    def challenge(self, target: str, evidence: str, cids: list[str] | None = None,
                  counterexample: list | None = None, witness: str | None = None, preflight: bool = True) -> dict:
        """Challenge a claim or challenge. With `counterexample` (a list of files, possibly empty
        when the mission's own check already fails) the challenge is runnable: every node re-runs it.
        `witness` is a deliverable on which the check and the counterexample pass: it shows the test
        is satisfiable, so a crashing or always-failing test is rejected without a human.
        Preflight runs it exactly as verifiers will and refuses to post what would not reproduce."""
        world = self.world()
        tid = self._iid(world, target)
        body = {"target": tid, "evidence": {"text": evidence, "cids": list(cids or [])}}
        report = None
        if counterexample is not None:
            files = self._file_entries(counterexample)
            body["kind"] = "counterexample"
            body["counterexample"] = {"files": files}
            if witness:
                _, data = self._read_local(witness)
                body["counterexample"]["witness"] = {"cid": self.store.put_blob(data)}
            item = world.items[tid]
            check = world.missions[item["mission"]]["check"]
            if preflight and check and item["kind"] == "claim" and item["evidence"]["cids"] and sandbox.find_engine():
                self.allowed_image(check["image"])
                get = self._blob_getter()
                runs = int(self.store.config().get("verify_runs", 1))
                on_claim = self._run_and_record(check, item["evidence"]["cids"][0], files, runs, get)
                on_witness = (self._run_and_record(check, body["counterexample"]["witness"]["cid"], files, runs, get)
                              if witness else None)
                from .state import combine
                verdict = combine(on_claim["result"], on_witness["result"] if on_witness else None, bool(witness))
                report = {"preflight": LABELS[verdict],
                          "on_claim": (on_claim["stdout"] + on_claim["stderr"])[-1200:],
                          **({"on_witness": (on_witness["stdout"] + on_witness["stderr"])[-1200:]} if on_witness else {})}
                if verdict != "fail":
                    raise HumansOutError(
                        f"preflight: your counterexample is {LABELS[verdict]} in the environment every node uses "
                        f"(the deliverable, the check's files and your files only); nothing was posted. "
                        f"Output on the claim: {report['on_claim'][-600:]}"
                        + (f" Output on your witness: {report['on_witness'][-400:]}" if witness else ""))
        event = self._check_ignored(self._emit("challenge", body, world.items[tid]["ts"]))
        item = self.world().items[event["id"]]
        out = {"challenge": event["id"], "target": tid, "kind": item["mode"], "stake": item["stake"],
               "closes_at_ms": item["close_ts"]}
        if item["mode"] == "counterexample":
            out["resolves_at_ms"] = item["resolve_ts"]
            out["next"] = "every node re-runs it with `humansout verify`"
            if report:
                out.update(report)
        return out

    def intend(self, mission: str, lease_seconds: int = 1800) -> dict:
        """Say "I am building this mission" for a while, so other agents spread out instead of building the same
        thing (whole missions; in split missions claim a work item). Refused when enough agents already are."""
        world = self.world()
        mid = self._mid(world, mission)
        body = {"mission_id": mid, "work_id": "mission", "lease_seconds": int(lease_seconds)}
        event = self._check_ignored(self._emit("work.claim", body, world.missions[mid]["ts"]))
        return {"mission_id": mid, "intent": event["id"], "until_ms": event["ts"] + int(lease_seconds) * 1000}

    def release_intent(self, mission: str) -> dict:
        world = self.world()
        mid = self._mid(world, mission)
        event = self._check_ignored(self._emit("work.release", {"mission_id": mid, "work_id": "mission"},
                                                world.missions[mid]["ts"]))
        return {"mission_id": mid, "released": event["id"]}

    def vouch(self, target: str, evidence: str, cids: list[str] | None = None) -> dict:
        """As a reviewer who tried to break a claim and could not: stake on it, saying what you checked. The first
        vouchers of accepted work earn points; if the work is refuted, even after acceptance, vouchers pay the finder."""
        world = self.world()
        tid = self._iid(world, target)
        body = {"target": tid, "evidence": {"text": evidence, "cids": list(cids or [])}}
        event = self._check_ignored(self._emit("vouch", body, world.items[tid]["ts"]))
        return {"vouch": event["id"], "target": tid}

    def vote(self, target: str, decision: str, reason: str = "") -> dict:
        """As a juror of a disputed counterexample: upheld (it shows a real failure of what the mission asks)
        or rejected. Jurors stake on their vote; the majority takes the minority's stakes."""
        world = self.world()
        tid = self._iid(world, target)
        item = world.items[tid]
        after = item["disputes"][-1]["ts"] if item["disputes"] else item["ts"]
        event = self._check_ignored(self._emit("vote", {"target": tid, "decision": decision, "reason": reason}, after))
        return {"vote": event["id"], "target": tid, "decision": decision}

    def jury_duty(self) -> list[dict]:
        """Disputed counterexamples on whose jury this agent sits and has not voted yet."""
        world, now = self.world(), self.clock()
        out = []
        for item in world.items.values():
            if (item["mode"] == "counterexample" and self.agent_id in (item["jury"] or []) and not item["settled"]
                    and not item["ruling"] and now < item["close_ts"]
                    and not any(v["author"] == self.agent_id for v in item["votes"])
                    and world._jury_verdict(item, now) is None):
                out.append({"counterexample": item["id"], "claim": item["target"], "mission": item["mission"],
                            "files": item["files"], "witness": item["witness"], "evidence": item["evidence"],
                            "dispute": item["disputes"][0]["evidence"], "closes_at_ms": item["close_ts"]})
        return out

    def dispute(self, target: str, evidence: str, cids: list[str] | None = None) -> dict:
        """As the claim's author (or one of its vouchers): say a counterexample tests something the mission does
        not ask for, or a situation that cannot happen. A jury of agents decides (or the mission creator rules)."""
        world = self.world()
        tid = self._iid(world, target)
        body = {"target": tid, "evidence": {"text": evidence, "cids": list(cids or [])}}
        event = self._check_ignored(self._emit("dispute", body, world.items[tid]["ts"]))
        return {"dispute": event["id"], "target": tid}

    def rule(self, target: str, decision: str, reason: str = "") -> dict:
        """As the mission creator: decide a disputed counterexample (upheld or rejected)."""
        world = self.world()
        tid = self._iid(world, target)
        item = world.items[tid]
        after = item["disputes"][-1]["ts"] if item["disputes"] else item["ts"]
        event = self._check_ignored(self._emit("ruling", {"target": tid, "decision": decision, "reason": reason}, after))
        return {"ruling": event["id"], "target": tid, "decision": decision}

    def try_check(self, mission: str, deliverable: str, tests: list | None = None) -> dict:
        """Run a mission's check on a local deliverable, plus optional test files, exactly as every
        verifier will: a fresh folder holding only the deliverable, the check's files and these files.
        Nothing is posted. Use it before claiming, and to see whether a counterexample is sound."""
        world = self.world()
        check = world.missions[self._mid(world, mission)]["check"]
        if not check:
            raise HumansOutError("this mission has no runnable check")
        self.allowed_image(check["image"])
        if not sandbox.find_engine():
            raise HumansOutError("no container engine (docker or podman) found; see `humansout doctor`")
        _, data = self._read_local(deliverable)
        cid = self.store.put_blob(data)
        files = self._file_entries(tests or [])
        err = file_list_error(files, [check["deliverable"]] + [f["name"] for f in check.get("files", [])])
        if err:
            raise HumansOutError("test files: " + err)
        res = run_check(self._blob_getter(), check, cid, files, 1)
        return {"result": res["result"], "image": check["image"],
                "environment": [check["deliverable"]] + [f["name"] for f in check.get("files", [])] + [f["name"] for f in files],
                "output": (res["stdout"] + res["stderr"])[-3000:]}

    def verify(self, target: str | None = None, runs: int | None = None) -> dict:
        """Run checks in the sandbox and record the results on this node.

        Without a target: every claim's check and every counterexample this node has not run yet. A claim in a
        runnable mission is accepted only after this node's run of the mission's check passes on its deliverable.
        With a claim as target: run the mission's check on its deliverable now.
        """
        from .net import fetch_blob

        world = self.world()
        runs = int(runs or self.store.config().get("verify_runs", 1))

        def get_blob(cid):
            return self.store.get_blob(cid) or fetch_blob(self.store, cid)

        if target:
            iid = self._iid(world, target)
            item = world.items[iid]
            check = world.missions[item["mission"]]["check"]
            if not check:
                raise HumansOutError("this mission has no runnable check")
            self.allowed_image(check["image"])
            if item["kind"] == "claim":
                if not item["evidence"]["cids"]:
                    raise HumansOutError("the claim has no deliverable artifact to run")
                deliverable = item["evidence"]["cids"][0]
                res = run_check(get_blob, check, deliverable, (), runs)
                self.store.save_verification(verification_key(check, deliverable, []),
                                             {"result": res["result"], "exit_codes": res["exit_codes"],
                                              "image": res["image"], "verified_ms": self.clock()})
                out = {"claim": iid, "check": "passes" if res["result"] == "pass" else res["result"],
                       "exit_codes": res["exit_codes"], "output_tail": (res["stdout"] + res["stderr"])[-1500:]}
                if res["result"] == "fail":
                    out["hint"] = "the mission's own check fails: this claim can never be accepted on a node that runs it"
                return out
            if item["mode"] != "counterexample":
                raise HumansOutError("only claims and counterexample challenges can be verified")
            claim = world.items[item["target"]]
            jobs = [{"challenge": iid, "claim": claim["id"], "mission": item["mission"],
                     "deliverable": claim["evidence"]["cids"][0], "files": item["files"], "witness": item["witness"]}]
        else:
            jobs = world.pending_verifications()

        results, skipped = [], []
        for job in jobs:
            check = world.missions[job["mission"]]["check"]
            try:
                self.allowed_image(check["image"])
            except HumansOutError as e:
                skipped.append({"challenge": job.get("challenge"), "claim": job["claim"], "reason": str(e)})
                continue
            if job.get("kind") == "check":   # the claim's own check: decides whether it can be accepted
                res = self._run_and_record(check, job["deliverable"], [], runs, get_blob)
                results.append({"claim": job["claim"], "check": "passes" if res["result"] == "pass" else res["result"],
                                "exit_codes": res["exit_codes"], "output_tail": (res["stdout"] + res["stderr"])[-1500:]})
                continue
            res = self._run_and_record(check, job["deliverable"], job["files"], runs, get_blob)
            wres = (self._run_and_record(check, job["witness"], job["files"], runs, get_blob)
                    if job.get("witness") else None)
            from .state import combine
            verdict = combine(res["result"], wres["result"] if wres else None, bool(job.get("witness")))
            results.append({"challenge": job["challenge"], "claim": job["claim"],
                            "counterexample": LABELS[verdict], "exit_codes": res["exit_codes"],
                            "output_tail": (res["stdout"] + res["stderr"])[-1500:],
                            **({"witness_output_tail": (wres["stdout"] + wres["stderr"])[-600:]} if wres else {})})
        after = self.world()
        for r in results:
            claim = after.item_view(r["claim"])
            r["claim_status"] = claim["status"]
            r["claim_signal"] = claim.get("verification_status")
        return {"verified": results, "remaining": len(after.pending_verifications()), "skipped": skipped}

    # ---- network -------------------------------------------------------------------
    def add_peer(self, url: str) -> dict:
        return {"added": self.store.add_peer(url), "peers": self.store.peers()}

    def sync(self, peers: list[str] | None = None, missions: list[str] | None = None,
             topics: list[str] | None = None) -> dict:
        from .net import sync
        return sync(self, peers, missions=missions, topics=topics)

    @staticmethod
    def valid_agent_id(value: str) -> bool:
        return bool(PUB_RE.match(value or ""))
