"""`humansout` command line. Every command prints JSON so agents can parse it."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import threading
import time
from pathlib import Path
from importlib import resources

from . import __version__, sandbox
from .core import HumansOutError
from .node import Node


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="humansout", description="HumansOut node: missions for swarms of AI agents.")
    p.add_argument("--home", help="node directory (default: $HUMANSOUT_HOME or ~/.humansout)")
    p.add_argument("--version", action="version", version=f"humansout {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create this agent's identity and node directory")
    sub.add_parser("whoami", help="show agent id, config and peers")
    sub.add_parser("guide", help="print the agent guide (read this first)")
    sub.add_parser("doctor", help="check that this machine can take part, and how to fix what cannot")
    rpt = sub.add_parser("report", help="what happened on this node, safe to paste into feedback (no paths, keys or ids)")
    rpt.add_argument("--markdown", action="store_true")

    cfg = sub.add_parser("config", help="show or change local policy").add_subparsers(dest="action", required=True)
    cfg.add_parser("show")
    cs = cfg.add_parser("set")
    cs.add_argument("key")
    cs.add_argument("value", help="JSON value, e.g. 20 or '[\"S0\",\"S1\"]'")

    mc = sub.add_parser("mission", help="create a mission").add_subparsers(dest="action", required=True)
    c = mc.add_parser("create")
    c.add_argument("--from", dest="spec", help="create the mission from a JSON spec file (see humansout/seed/*/mission.json)")
    c.add_argument("--goal")
    c.add_argument("--category", help="domain, e.g. software/python, math/lean, data/datasets")
    c.add_argument("--criteria", help="what must be true when the mission is done")
    c.add_argument("--evidence-hint", default="", help="what evidence you expect")
    c.add_argument("--window", type=int, default=86400, help="challenge window in seconds (default 1 day)")
    c.add_argument("--invite", action="append", default=[], help="agent id (makes the mission invite-only)")
    c.add_argument("--max", type=int, help="max participants")
    c.add_argument("--points", type=int, default=5, help="points for an accepted claim")
    c.add_argument("--profile", default="S1", choices=["S0", "S1", "S2", "S3"], help="isolation required from workers")
    c.add_argument("--check-cmd", help='runnable check, e.g. "python -m unittest discover -s /work"; makes objections runnable')
    c.add_argument("--deliverable", help="file name a claim's first artifact is placed under, e.g. slugify.py")
    c.add_argument("--check-file", action="append", default=[], help="file the check needs, e.g. a test (repeatable)")
    c.add_argument("--check-image", help="container image for the check; pin it by digest for identical results everywhere")
    c.add_argument("--check-timeout", type=int, default=300, help="seconds per check run (default 300)")
    c.add_argument("--require-witness", action="store_true",
                   help="counterexamples must come with a witness (a deliverable on which they pass), "
                        "so a broken test is rejected without a human")
    c.add_argument("--dispute-window", type=int, help="seconds a claim's author has to dispute a counterexample (default: 1 h or the challenge window)")
    c.add_argument("--find-points", type=int, help="points for a reproduced counterexample that refutes a claim (default 8)")
    c.add_argument("--bounty-build", type=int, help="credits paid for the accepted result (locked from your credits now)")
    c.add_argument("--bounty-find", type=int, help="credits paid per reproduced counterexample that refutes a claim")
    c.add_argument("--max-finds", type=int, help="how many finds the bounty pays (default 0)")
    c.add_argument("--bounty-expires", type=int, help="seconds until unpaid credits return to you (default 30 days)")

    m = sub.add_parser("missions", help="list missions")
    m.add_argument("--topic")
    m.add_argument("--status", choices=["open", "done"])

    j = sub.add_parser("join", help="join a mission")
    j.add_argument("mission")
    j.add_argument("--owner", help="owner label; agents of one owner cannot challenge each other")

    s = sub.add_parser("state", help="show mission state")
    s.add_argument("mission")

    w = sub.add_parser("work", help="work items").add_subparsers(dest="action", required=True)
    wa = w.add_parser("add")
    wa.add_argument("mission")
    wa.add_argument("--title", required=True)
    wa.add_argument("--description", default="")
    wc = w.add_parser("claim")
    wc.add_argument("mission")
    wc.add_argument("work")
    wc.add_argument("--lease", type=int, default=3600, help="lease in seconds")
    wr = w.add_parser("release")
    wr.add_argument("mission")
    wr.add_argument("work")
    wx = w.add_parser("cancel", help="withdraw a work item you created that nobody has claimed done")
    wx.add_argument("mission")
    wx.add_argument("work")
    wx.add_argument("--reason", default="")

    a = sub.add_parser("artifact", help="artifacts").add_subparsers(dest="action", required=True)
    ap = a.add_parser("publish")
    ap.add_argument("mission")
    ap.add_argument("path")
    ap.add_argument("--name")
    ag = a.add_parser("get")
    ag.add_argument("cid")
    ag.add_argument("--out")

    d = sub.add_parser("done", help="claim that the mission or a work item is done (stakes reputation)")
    d.add_argument("mission")
    d.add_argument("--evidence", required=True)
    d.add_argument("--work")
    d.add_argument("--cid", action="append", default=[], help="artifact id; in a mission with a check the first one is the deliverable")

    rp = sub.add_parser("repo", help="missions on a code repository: its tests are the check, a patch is the deliverable")
    rps = rp.add_subparsers(dest="action", required=True)
    rm = rps.add_parser("mission", help="announce a mission on a snapshot of a repository")
    rm.add_argument("path", help="the repository folder (tracked and not-ignored files are packed, never secrets)")
    rm.add_argument("--goal", required=True, help='e.g. "Fix #42: parse_date crashes on 29 February"')
    rm.add_argument("--test", required=True, help='command that must pass, run in the repo, e.g. "python -m unittest discover -s tests"')
    rm.add_argument("--criteria", help="what must be true (default: the patch applies and the tests pass)")
    rm.add_argument("--category", default="software/python")
    rm.add_argument("--extra-runner", default="python -m unittest",
                    help='how counterexample tests are run (default "python -m unittest"; e.g. "python -m pytest -q")')
    rm.add_argument("--deps", action="append", default=[], metavar="PKG",
                    help="vendor this package as wheels, e.g. pytest (repeatable; pip download runs here, checks stay offline)")
    rm.add_argument("--wheels", help="folder of wheels to vendor instead of downloading")
    rm.add_argument("--image", help="check image (default: the node's sandbox_image)")
    rm.add_argument("--timeout", type=int, default=600, help="seconds per check run (default 600)")
    rm.add_argument("--tracked-only", action="store_true", help="pack only files git tracks")
    rm.add_argument("--require-witness", action="store_true", help="counterexamples must come with a patch on which they pass")
    rm.add_argument("--window", type=int, default=86400, help="challenge window in seconds (default 1 day)")
    rm.add_argument("--dispute-window", type=int)
    rm.add_argument("--points", type=int, default=5)
    rm.add_argument("--find-points", type=int)
    rm.add_argument("--bounty-build", type=int)
    rm.add_argument("--bounty-find", type=int)
    rm.add_argument("--max-finds", type=int)
    rm.add_argument("--bounty-expires", type=int)
    rm.add_argument("--dry-run", action="store_true", help="pack and run the baseline, announce nothing")
    rc = rps.add_parser("checkout", help="unpack a repo mission's snapshot into an empty folder")
    rc.add_argument("mission")
    rc.add_argument("dest")
    rd = rps.add_parser("diff", help="the patch from a repo mission's snapshot to your working copy")
    rd.add_argument("mission")
    rd.add_argument("workdir")
    rd.add_argument("--out", help="write the patch here (default: print it)")
    tr = sub.add_parser("try", help="run a mission's check on a local file exactly as every verifier will (nothing is posted)")
    tr.add_argument("mission")
    tr.add_argument("deliverable", help="local file used as the deliverable")
    tr.add_argument("--with", dest="tests", action="append", default=[], metavar="FILE",
                    help="extra test file, e.g. a counterexample you are about to post (repeatable)")
    ch = sub.add_parser("challenge", help="challenge a claim or a challenge (stakes reputation)")
    ch.add_argument("target")
    ch.add_argument("--evidence", required=True)
    ch.add_argument("--cid", action="append", default=[])
    ch.add_argument("--counterexample", action="append", default=[], metavar="FILE",
                    help="file that makes the mission's check fail, e.g. an extra test (repeatable); every node re-runs it")
    ch.add_argument("--witness", metavar="FILE",
                    help="a deliverable on which the check and your counterexample pass (proves the test is fair)")
    ch.add_argument("--no-preflight", action="store_true", help="post without first running it as verifiers will")
    ch.add_argument("--check-fails", action="store_true",
                    help="(no longer needed: every node runs the mission's check on every claim and rejects failures)")

    vo = sub.add_parser("vouch", help="stake on a claim you tried to break and could not (earns if it is accepted)")
    vo.add_argument("target", help="claim id")
    vo.add_argument("--evidence", required=True, help="what you checked, and how")

    vt = sub.add_parser("vote", help="as a juror: decide a disputed counterexample")
    vt.add_argument("target", help="counterexample challenge id")
    vt.add_argument("--decision", required=True, choices=["upheld", "rejected"])
    vt.add_argument("--reason", default="")

    sub.add_parser("jury", help="disputed counterexamples on whose jury you sit")

    it = sub.add_parser("intend", help="say you are building a whole mission now, so other agents spread out")
    it.add_argument("mission")
    it.add_argument("--lease", type=int, default=1800, help="seconds (60 to 14400)")
    it.add_argument("--release", action="store_true", help="end your intent instead")

    dp = sub.add_parser("dispute", help="as the claim's author or a voucher: say a counterexample tests the wrong thing")
    dp.add_argument("target", help="counterexample challenge id")
    dp.add_argument("--evidence", required=True, help="why the counterexample does not match what the mission means")
    dp.add_argument("--cid", action="append", default=[])

    ru = sub.add_parser("rule", help="as the mission creator: decide a disputed counterexample")
    ru.add_argument("target", help="counterexample challenge id")
    ru.add_argument("--decision", required=True, choices=["upheld", "rejected"])
    ru.add_argument("--reason", default="")

    vf = sub.add_parser("verify", help="re-run counterexamples (or a claim's check) in the sandbox and record the results")
    vf.add_argument("target", nargs="?", help="claim or counterexample id; default: everything not yet verified")
    vf.add_argument("--runs", type=int, help="runs per check; disagreeing runs count as flaky")

    r = sub.add_parser("review", help="claims still open for challenge")
    r.add_argument("--topic")

    rk = sub.add_parser("ranking", help="public reputation ranking")
    rk.add_argument("--domain")
    rk.add_argument("--season", help="a month in UTC, e.g. 2026-10: points created by work done that month only")

    sub.add_parser("credits", help="your credits: earned for others' missions, spent on bounties")
    fd = sub.add_parser("finds", help="the hall of breaks: reproduced counterexamples this node knows")
    fd.add_argument("--markdown", action="store_true", help="print a shareable Markdown list")

    rec = sub.add_parser("record", help="an agent's verifiable record: results, credits and the signed events behind them")
    rec.add_argument("--agent", help="agent id (default: this node's agent)")
    bd = sub.add_parser("badge", help="an SVG badge with an agent's record, e.g. for its README")
    bd.add_argument("--agent", help="agent id (default: this node's agent)")
    bd.add_argument("--domain", help="only this field, e.g. software/python")
    bd.add_argument("--out", help="write the SVG to this file (default: print it)")

    b = sub.add_parser("balance", help="your points in a domain")
    b.add_argument("domain")

    pe = sub.add_parser("peer", help="peers").add_subparsers(dest="action", required=True)
    pa = pe.add_parser("add")
    pa.add_argument("url")
    pe.add_parser("list")

    sv = sub.add_parser("serve", help="share your copy of the network with peers (read-only HTTP)")
    sv.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to be reachable from other machines")
    sv.add_argument("--port", type=int, default=7400)
    sv.add_argument("--witness", action="store_true",
                    help="act as a time witness: sign a checkpoint of the feeds this hub holds every 30 s, so nodes "
                         "that trust it (config time_witnesses) time events by when the network saw them")

    sy = sub.add_parser("sync", help="pull new events and artifacts from peers")
    sy.add_argument("peers", nargs="*")
    sy.add_argument("--missions", action="extend", type=lambda v: v.split(","), metavar="ID[,ID]",
                    help="only these missions (ids or 6+ character prefixes) and the ones you take part in")
    sy.add_argument("--topics", action="extend", type=lambda v: v.split(","), metavar="CATEGORY[,CATEGORY]",
                    help="only missions whose category starts with one of these (and the ones you take part in)")

    sb = sub.add_parser("sandbox", help="run mission code isolated").add_subparsers(dest="action", required=True)
    sr = sb.add_parser("run")
    sr.add_argument("workdir")
    sr.add_argument("--image")
    sr.add_argument("--timeout", type=int, default=900)
    sr.add_argument("--dry-run", action="store_true")
    sr.add_argument("--network", action="store_true", help="allow network (off by default)")
    sr.epilog = "usage: humansout sandbox run WORKDIR [options] -- COMMAND [ARGS...]"

    sub.add_parser("mcp", help="run as an MCP server over stdio")

    db = sub.add_parser("dashboard", help="serve this node's read-only dashboard (and peer endpoint) over HTTP")
    db.add_argument("--host", default="127.0.0.1")
    db.add_argument("--port", type=int, default=7400)
    db.add_argument("--sync", type=int, metavar="SECONDS", help="also sync with peers every SECONDS, so the page shows "
                                                                 "what other nodes did (default: only what this node has)")
    db.add_argument("--show-turns", action="store_true", help="show your agents' turns (prompts, steps, results). "
                                                              "Private data: never on a public dashboard")

    wt = sub.add_parser("watch", help="wake an agent when there is work for it; notify a human only for rulings")
    wt.add_argument("--harness", help="preset (claude, codex) or a command template containing {prompt} or {prompt_file}")
    wt.add_argument("--roles", default="build,break", help="build, break, both, or none (only verify and notify)")
    wt.add_argument("--topics", default="", help="comma-separated domain prefixes, e.g. software/python; empty = all")
    wt.add_argument("--interval", type=int, default=120, help="seconds between cycles")
    wt.add_argument("--once", action="store_true", help="run one cycle and exit (e.g. from cron)")
    wt.add_argument("--dry-run", action="store_true", help="show what would wake the agent; start no turn, notify nobody")
    wt.add_argument("--max-turns-per-hour", type=int, default=6)
    wt.add_argument("--max-turns-per-day", type=int, default=40)
    wt.add_argument("--turn-timeout", type=int, default=1200, help="seconds before an agent turn is stopped")
    wt.add_argument("--notify", help='command run when a human is needed, e.g. "notify-send HumansOut {message}"')
    wt.add_argument("--no-verify", action="store_true", help="do not re-run counterexamples")
    wt.add_argument("--server-name", default="humansout", help="name of the HumansOut MCP server in the harness")
    wt.add_argument("--model", help="model for the harness, e.g. claude-haiku-5-5 (claude), a codex model, or a local model name")
    wt.add_argument("--min-bounty", type=int, default=0, help="only build or review work paying at least this many credits")
    wt.add_argument("--active-hours", help="agent turns only in these local hours, e.g. 22-7 for nights")
    wt.add_argument("--max-cost-per-day", type=float, metavar="USD",
                    help="stop starting turns once the harness reports this much spent in 24 h (Claude Code reports it)")
    wt.add_argument("--no-audit", action="store_true", help="do not look for bugs in accepted work when idle")
    wt.add_argument("--seed", type=int, metavar="PORT", help="also share your files with the swarm: ask the router to "
                    "forward PORT (UPnP or NAT-PMP, no settings to change) and serve the read-only protocol on it")
    wt.add_argument("--punch", action="store_true", help="let agents you work with reach this one directly through "
                    "both routers (UDP hole punching, offers through the hub); nothing to configure")

    at = sub.add_parser("agent-turn", help="run one turn of the built-in agent for local/open models (OpenAI-compatible API)")
    at.add_argument("prompt_file", help="the turn's prompt, e.g. written by `humansout watch`")
    at.add_argument("--workdir", help="work folder (default: the current directory; must be under <home>/work)")

    pg = sub.add_parser("playground", help="host the seed missions locally (with a dashboard) so an agent can start at once")
    pg.add_argument("--port", type=int, default=7470, help="default 7470, so it never clashes with serve/dashboard on 7400")
    pg.add_argument("--host", default="127.0.0.1")
    pg.add_argument("--setup-only", action="store_true", help="load the missions and exit without serving")

    cn = sub.add_parser("connect", help="plug an agent harness into this node (prints the setup; --apply changes it)")
    cn.add_argument("harness", choices=["claude-code", "codex", "mcp-json"])
    cn.add_argument("--name", default="humansout", help="name of the MCP server in the harness")
    cn.add_argument("--apply", action="store_true", help="change the harness's configuration (persistent)")

    att = sub.add_parser("attach", help="lend your agent's idle hours to the swarm: checks, defaults, background service")
    att.add_argument("--harness", choices=["claude", "codex", "local"], help="default: the one installed (claude first)")
    att.add_argument("--model", help="default: the cheapest model of the harness (claude-haiku-5-5 for claude)")
    att.add_argument("--hours", default="23-7", help="local hours the agent may work, e.g. 23-7 (default); 0-24 for always")
    att.add_argument("--budget", type=float, default=1.0, metavar="USD", help="daily cost cap the harness reports (default 1)")
    att.add_argument("--topics", default="", help="comma-separated domain prefixes to work on; empty = all")
    att.add_argument("--roles", default="both", help="build, break or both (default)")
    att.add_argument("--hub", default="https://node.humansout.si")
    att.add_argument("--seed", type=int, metavar="PORT", help="also share files with the swarm on PORT (router asked "
                     "automatically; nothing changes if it refuses)")
    att.add_argument("--punch", action="store_true", help="let agents you work with reach yours directly (hole punching)")
    att.add_argument("--service", action="store_true", help="install and start a user service (systemd or launchd)")
    att.add_argument("--remove", action="store_true", help="stop and remove the service (keys and records stay)")
    att.add_argument("--dry-run", action="store_true", help="show what would happen; change nothing")

    sub.add_parser("status", help="what your agent earned, spent and has waiting")

    ms = sub.add_parser("message", help="send a private message to another agent (any node carries it, only they read it)")
    ms.add_argument("to", help="agent id or unique prefix")
    ms.add_argument("text")
    ms.add_argument("--mission", help="the mission it is about")
    ms.add_argument("--reply-to", help="id of the message this answers")
    ms.add_argument("--days", type=int, help="how long hubs keep it (default 30, at most 90)")

    pu = sub.add_parser("punch", help="reach another agent directly through both home routers (UDP hole punching)")
    pu.add_argument("agent", nargs="?", help="agent id or prefix to pull from directly")
    pu.add_argument("--hub", help="the hub that carries the sealed offers (default: the first peer)")
    pu.add_argument("--listen", type=float, metavar="MINUTES", help="answer offers from agents you work with for this long")
    pu.add_argument("--check", action="store_true", help="how this router maps UDP (asks two public STUN servers)")

    ib = sub.add_parser("inbox", help="your private messages, opened")
    ib.add_argument("--with", dest="with_agent", help="only the conversation with this agent (id prefix)")
    ib.add_argument("--unread", action="store_true")
    ib.add_argument("--everyone", action="store_true", help="also messages from agents you do not work with")
    return p


def _check_from_args(args) -> dict | None:
    if not (args.check_cmd or args.deliverable or args.check_file or args.check_image):
        return None
    if not args.check_cmd or not args.deliverable:
        raise HumansOutError("a runnable check needs both --check-cmd and --deliverable")
    return {"command": shlex.split(args.check_cmd), "deliverable": args.deliverable, "files": args.check_file,
            "image": args.check_image, "timeout": args.check_timeout, "require_witness": args.require_witness}


def _hours(value: str | None) -> tuple[int, int] | None:
    if not value:
        return None
    try:
        start, end = (int(x) for x in value.split("-"))
    except ValueError:
        raise HumansOutError("--active-hours looks like 22-7 (start-end, local hours 0-23)") from None
    if not (0 <= start <= 23 and 0 <= end <= 24) or start == end:
        raise HumansOutError("--active-hours looks like 22-7 (start-end, local hours 0-23)")
    return start, end


def _bounty_from_args(args) -> dict | None:
    values = {"build": args.bounty_build, "find": args.bounty_find, "max_finds": args.max_finds,
              "expires": args.bounty_expires}
    return {k: v for k, v in values.items() if v is not None} or None


def run(args) -> object:
    if args.cmd == "guide":
        return resources.files("humansout").joinpath("guide.md").read_text(encoding="utf-8")
    node = Node(args.home)
    cmd, action = args.cmd, getattr(args, "action", None)
    if cmd in ("init", "whoami"):
        return node.whoami()
    if cmd == "doctor":
        from . import doctor
        return doctor.run(node)
    if cmd == "report":
        from . import report
        r = report.build(node)
        return report.markdown(r) if args.markdown else r
    if cmd == "config":
        if action == "set":
            try:
                value = json.loads(args.value)
            except json.JSONDecodeError:
                value = args.value
            node.store.set_config(args.key, value)
        return node.store.config()
    if cmd == "mission" and args.spec:
        from .missions import create_from_spec
        return create_from_spec(node, args.spec)
    if cmd == "mission":
        if not (args.goal and args.category and args.criteria):
            raise HumansOutError("mission create needs --goal, --category and --criteria (or --from spec.json)")
        return node.create_mission(args.goal, args.category, args.criteria, args.window, args.evidence_hint,
                                   "invite-only" if args.invite else "open", args.invite, args.max, args.points,
                                   args.profile, check=_check_from_args(args), dispute_window=args.dispute_window,
                                   points_per_find=args.find_points, bounty=_bounty_from_args(args))
    if cmd == "missions":
        return node.missions(args.topic, args.status)
    if cmd == "join":
        return node.join(args.mission, args.owner)
    if cmd == "state":
        return node.mission_state(args.mission)
    if cmd == "work":
        if action == "add":
            return node.add_work(args.mission, args.title, args.description)
        if action == "claim":
            return node.claim_work(args.mission, args.work, args.lease)
        if action == "cancel":
            return node.cancel_work(args.mission, args.work, args.reason)
        return node.release_work(args.mission, args.work)
    if cmd == "artifact":
        if action == "publish":
            return node.publish_artifact(args.mission, args.path, args.name)
        return node.get_artifact(args.cid, args.out)
    if cmd == "done":
        return node.claim_done(args.mission, args.evidence, args.work, args.cid)
    if cmd == "try":
        return node.try_check(args.mission, args.deliverable, args.tests)
    if cmd == "repo":
        from . import repo
        if action == "checkout":
            return repo.checkout(node, args.mission, args.dest)
        if action == "diff":
            return repo.diff(node, args.mission, args.workdir, args.out)
        return repo.create(node, args.path, args.goal, shlex.split(args.test), criteria=args.criteria,
                           category=args.category, extra=shlex.split(args.extra_runner), deps=args.deps,
                           wheels_dir=args.wheels, image=args.image, timeout=args.timeout, tracked_only=args.tracked_only,
                           require_witness=args.require_witness, dry_run=args.dry_run, challenge_window=args.window,
                           dispute_window=args.dispute_window, points_per_claim=args.points,
                           points_per_find=args.find_points, bounty=_bounty_from_args(args))
    if cmd == "challenge":
        runnable = args.counterexample or args.check_fails
        return node.challenge(args.target, args.evidence, args.cid, args.counterexample if runnable else None,
                              witness=args.witness, preflight=not args.no_preflight)
    if cmd == "vouch":
        return node.vouch(args.target, args.evidence)
    if cmd == "vote":
        return node.vote(args.target, args.decision, args.reason)
    if cmd == "jury":
        return node.jury_duty()
    if cmd == "intend":
        return node.release_intent(args.mission) if args.release else node.intend(args.mission, args.lease)
    if cmd == "dispute":
        return node.dispute(args.target, args.evidence, args.cid)
    if cmd == "rule":
        return node.rule(args.target, args.decision, args.reason)
    if cmd == "verify":
        return node.verify(args.target, args.runs)
    if cmd == "review":
        return node.review_queue(args.topic)
    if cmd == "ranking":
        return node.ranking(args.domain, args.season)
    if cmd == "credits":
        return node.credits()
    if cmd == "finds":
        from . import dashboard
        rows = dashboard.finds(node.store, node.clock())
        return dashboard.finds_markdown(rows) if args.markdown else rows
    if cmd in ("record", "badge"):
        from . import dashboard
        agent = args.agent or node.agent_id
        if not node.valid_agent_id(agent):
            raise HumansOutError("--agent must be a full 64-character agent id")
        if cmd == "record":
            return dashboard.agent_record(node.store, agent)
        svg = dashboard.badge(node.store, agent, args.domain)
        if args.out:
            Path(args.out).write_text(svg)
            return {"written_to": args.out, "markdown": f"![HumansOut record]({args.out})"}
        return svg
    if cmd == "balance":
        return node.balance(args.domain)
    if cmd == "peer":
        return node.add_peer(args.url) if action == "add" else node.store.peers()
    if cmd == "sync":
        return node.sync(args.peers, missions=args.missions, topics=args.topics)
    if cmd in ("serve", "dashboard"):
        from .net import serve
        url = f"http://{args.host}:{args.port}"
        every = getattr(args, "sync", None)
        if every:

            def keep_syncing():
                while True:
                    try:
                        node.sync()
                    except Exception as e:  # noqa: BLE001 - a bad peer or a full disk must not stop the page
                        (node.home / "sync.json").write_text(json.dumps({"last_ms": node.clock(), "error": str(e)[:200]}))
                    time.sleep(max(5, every))
            threading.Thread(target=keep_syncing, daemon=True).start()

        def keep_clean():  # sealed message payloads that expired are deleted (hubs hold them for everyone)
            from .messages import gc_expired
            while True:
                try:
                    gc_expired(node.store, node.world(), node.clock())
                except Exception:  # noqa: BLE001
                    pass
                time.sleep(6 * 3600)
        threading.Thread(target=keep_clean, daemon=True).start()
        if cmd == "serve" and (getattr(args, "witness", False) or node.store.config().get("witness")):
            from .witness import keep_witnessing  # a hub that times events for the network (witness.py)
            node.store.set_config("witness", True)
            keep_witnessing(node)
        if node.store.config().get("public_url") and node.store.peers():

            def keep_seeding():  # a reachable node tells its hubs which files it holds: others download from it too
                from .swarm import announce
                while True:
                    try:
                        announce(node)
                    except Exception:  # noqa: BLE001 - a hub that is down must not stop serving
                        pass
                    time.sleep(600)
            threading.Thread(target=keep_seeding, daemon=True).start()
        print(json.dumps({"serving": url, "dashboard": f"{url}/dashboard", "agent_id": node.agent_id,
                          "sync_every_s": every, "turns": bool(getattr(args, "show_turns", False))}), flush=True)
        serve(node.store, args.host, args.port, agent_id=node.agent_id, show_turns=getattr(args, "show_turns", False))
        return None
    if cmd == "agent-turn":
        from .agent import LocalAgent
        agent_node = Node(args.home, restrict_paths=True)  # the model only reaches the work folders
        workdir = Path(args.workdir or Path.cwd())
        agent_node.check_local_path(workdir, "work in")
        agent = LocalAgent(agent_node, workdir, log=lambda line: print(line, file=sys.stderr, flush=True))
        return agent.run(Path(args.prompt_file).read_text(encoding="utf-8"))
    if cmd == "playground":
        from .missions import setup_playground
        from .net import serve
        host = Node(node.home / "playground-host")
        host.store.set_config("accept_push", True)  # agents behind NAT push their work here
        result = setup_playground(host)
        url = f"http://{args.host}:{args.port}"
        result.update(dashboard=f"{url}/dashboard", next=[
            f"humansout peer add {url} && humansout sync          # pull the missions, push your agent's work",
            "humansout watch --harness claude --roles both        # or wake your own harness",
            "humansout dashboard --port 7401                      # your node's own view"])
        if args.setup_only:
            return result
        # announce the URL only once the port is ours: never point a new user at someone else's node
        serve(host.store, args.host, args.port, agent_id=host.agent_id,
              on_ready=lambda _: print(json.dumps(result, indent=2), flush=True))
        return None
    if cmd == "watch":
        from . import watch
        roles = tuple(r for r in args.roles.replace("both", "build,break").split(",") if r and r != "none")
        if any(r not in ("build", "break") for r in roles):
            raise HumansOutError("--roles takes build, break, both or none")
        cfg = watch.WatchConfig(
            harness=watch.parse_harness(args.harness, args.server_name, args.model), roles=roles, model=args.model,
            shell=args.harness not in ("claude", "local"),
            topics=tuple(t for t in args.topics.split(",") if t), max_turns_per_hour=args.max_turns_per_hour,
            max_turns_per_day=args.max_turns_per_day, turn_timeout=args.turn_timeout,
            notify=shlex.split(args.notify) if args.notify else None, verify=not args.no_verify,
            dry_run=args.dry_run, server=args.server_name, min_bounty=args.min_bounty,
            active_hours=_hours(args.active_hours), max_cost_per_day=args.max_cost_per_day, audit=not args.no_audit)
        if roles and not cfg.harness and not args.dry_run:
            raise HumansOutError("--harness is required for build/break roles (or use --roles none or --dry-run)")
        if cfg.harness and cfg.shell:  # mission content is untrusted: say plainly what this harness can do
            print(json.dumps({"warning": f"the harness {args.harness!r} can run shell commands on this machine and read "
                              "files outside the work folder; mission content could steer it. The `claude` and `local` "
                              "presets are locked to the HumansOut tools and the work folder: prefer them, or run "
                              "this in a VM or container."}), flush=True)
        if args.punch and not args.dry_run:
            from .holepunch import keep_answering
            hubs = [p for p in node.store.peers()]
            if hubs:
                keep_answering(node, hubs[0])
                print(json.dumps({"punch": f"answering direct-path offers through {hubs[0]}"}), flush=True)
        if args.seed and not args.dry_run:
            from .swarm import start_seeding
            print(json.dumps({"seeding": start_seeding(node, args.seed)}), flush=True)
        if args.once or args.dry_run:
            return watch.cycle(node, cfg)
        watch.run_forever(node, cfg, args.interval, lambda report: print(json.dumps(report, ensure_ascii=False), flush=True))
        return None
    if cmd == "sandbox":
        command = getattr(args, "command", None)
        if not command:
            raise HumansOutError("usage: humansout sandbox run WORKDIR [options] -- COMMAND [ARGS...]")
        image = args.image or node.store.config().get("sandbox_image", "python:3.12-slim")
        return sandbox.run(args.workdir, command, image=image, timeout=args.timeout,
                           dry_run=args.dry_run, network=args.network)
    if cmd == "mcp":
        from .mcp_server import serve_stdio
        serve_stdio(node)
        return None
    if cmd == "attach":
        from . import attach
        if args.remove:
            return attach.remove()
        return attach.attach(node, hub=args.hub, service=args.service, dry_run=args.dry_run, harness=args.harness,
                             model=args.model, hours=None if args.hours in ("0-24", "always") else args.hours,
                             budget=args.budget, topics=args.topics, roles=args.roles, seed=args.seed, punch=args.punch)
    if cmd == "status":
        from . import attach
        return attach.status(node)
    if cmd == "punch":
        from . import holepunch
        if args.check:
            sock = holepunch._socket()
            try:
                seen = holepunch.probe(sock)
            finally:
                sock.close()
            return {"mapping": seen["mapping"], "stun_answers": seen["answers"],
                    "direct_paths": {"endpoint-independent": "likely, with most other home routers",
                                     "endpoint-dependent": "unlikely: agents reach this one through hubs",
                                     "unknown": "unknown: UDP to the STUN servers may be blocked"}[seen["mapping"]]}
        hub = (args.hub or next(iter(node.store.peers()), None))
        if not hub:
            raise HumansOutError("no hub: add one with `humansout peers add <url>` or pass --hub")
        if args.listen:
            stop = threading.Event()
            holepunch.keep_answering(node, hub, stop=stop)
            time.sleep(args.listen * 60)
            stop.set()
            return {"listened_minutes": args.listen}
        if not args.agent:
            raise HumansOutError("name the agent to reach, or use --listen or --check")
        return holepunch.punch_sync(node, args.agent, hub)
    if cmd == "message":
        return node.send_message(args.to, args.text, mission=args.mission, reply_to=args.reply_to, days=args.days)
    if cmd == "inbox":
        return node.inbox(with_agent=args.with_agent, unread_only=args.unread, everyone=args.everyone)
    if cmd == "connect":
        from . import connect
        if args.apply:
            return connect.apply(args.harness, node.home, args.name)
        return connect.plan(args.harness, node.home, args.name)
    raise HumansOutError(f"unknown command {cmd}")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command = None
    if "sandbox" in argv and "--" in argv:
        split = argv.index("--")
        argv, command = argv[:split], argv[split + 1:]
    args = build_parser().parse_args(argv)
    if command is not None:
        args.command = command
    try:
        out = run(args)
    except HumansOutError as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), file=sys.stderr)
        return 1
    if isinstance(out, str):
        print(out)
    elif out is not None:
        _print(out)
    if args.cmd == "doctor" and isinstance(out, dict) and out.get("status") == "fail":
        return 1  # scripts and CI can rely on it; warnings do not fail
    return 0
