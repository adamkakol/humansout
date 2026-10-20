#!/usr/bin/env python3
"""Phase 1 of the replay study: sample merged agent PRs and find the ones later reverted.

Read-only GitHub API calls through the `gh` CLI (the user's login). The search API allows
about 30 requests a minute; this script paces itself and waits out rate limits.
"""

from __future__ import annotations

import argparse
import calendar
import json
import subprocess
import time
from pathlib import Path

AGENTS = ["copilot-swe-agent", "google-labs-jules", "devin-ai-integration", "claude", "cursor"]
MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09"]
SEARCH_PAUSE = 2.3  # seconds between search calls: stays under 30/minute


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def search(query: str, per_page: int = 100, page: int = 1) -> dict:
    while True:
        time.sleep(SEARCH_PAUSE)
        proc = subprocess.run(["gh", "api", "-X", "GET", "search/issues", "-f", f"q={query}",
                               "-f", f"per_page={per_page}", "-f", f"page={page}"],
                              capture_output=True, text=True)
        if proc.returncode == 0:
            return json.loads(proc.stdout)
        err = (proc.stderr + proc.stdout).lower()
        if "rate limit" in err or "secondary" in err or "403" in err:
            log("rate limited; waiting 65 s")
            time.sleep(65)
            continue
        raise RuntimeError(f"search failed for {query!r}: {proc.stderr.strip()[:300]}")


def last_day(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{month}-{calendar.monthrange(y, m)[1]:02d}"


def sample_agent(agent: str, per_month: int) -> list[dict]:
    prs = []
    for month in MONTHS:
        q = f"is:pr is:merged author:app/{agent} language:python created:{month}-01..{last_day(month)}"
        items = search(q, per_page=per_month).get("items", [])
        for it in items:
            repo = "/".join(it["repository_url"].split("/")[-2:])
            prs.append({"agent": agent, "repo": repo, "number": it["number"], "title": it["title"],
                        "url": it["html_url"], "created_at": it["created_at"],
                        "merged_at": (it.get("pull_request") or {}).get("merged_at")})
        log(f"{agent} {month}: {len(items)} PRs")
    return prs


def find_revert(pr: dict) -> dict | None:
    by_body = search(f'repo:{pr["repo"]} is:pr is:merged "Reverts {pr["repo"]}#{pr["number"]}"', per_page=5)
    items = by_body.get("items", [])
    if not items:
        title = pr["title"].replace('"', "")[:80]
        by_title = search(f'repo:{pr["repo"]} is:pr is:merged in:title "Revert {title}"', per_page=5)
        items = [i for i in by_title.get("items", []) if i["number"] != pr["number"]]
    if not items:
        return None
    r = items[0]
    return {"number": r["number"], "title": r["title"], "url": r["html_url"], "created_at": r["created_at"],
            "author": (r.get("user") or {}).get("login"), "body": (r.get("body") or "")[:1500]}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out", required=True)
    p.add_argument("--per-month", type=int, default=30, help="PRs sampled per agent per month")
    args = p.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows_path = out / "candidates.jsonl"
    done = set()
    if rows_path.exists():
        done = {(r["repo"], r["number"]) for r in map(json.loads, rows_path.read_text().splitlines()) if r}
    for agent in AGENTS:
        prs = sample_agent(agent, args.per_month)
        for i, pr in enumerate(prs, 1):
            if (pr["repo"], pr["number"]) in done:
                continue
            pr["revert"] = find_revert(pr)
            with rows_path.open("a") as f:
                f.write(json.dumps(pr, ensure_ascii=False) + "\n")
            if pr["revert"]:
                log(f"{agent} {i}/{len(prs)} REVERTED {pr['url']} by {pr['revert']['url']}")
        log(f"{agent}: checked {len(prs)} PRs")
    summarize(out)


def summarize(out: Path):
    rows = [json.loads(l) for l in (out / "candidates.jsonl").read_text().splitlines() if l.strip()]
    lines = ["# Replay study, phase 1: reverted agent PRs", "",
             "| Agent | Sampled merged PRs (Python, 2026) | Reverted | Rate |", "|---|---|---|---|"]
    for agent in AGENTS:
        mine = [r for r in rows if r["agent"] == agent]
        rev = [r for r in mine if r.get("revert")]
        rate = f"{100 * len(rev) / len(mine):.1f}%" if mine else "-"
        lines.append(f"| {agent} | {len(mine)} | {len(rev)} | {rate} |")
    lines += ["", "Reverts are found by the GitHub revert button's text (`Reverts <repo>#<n>`) or a",
              "`Revert \"<title>\"` title, so this undercounts manual reverts and later fixes.", "",
              "## Reverted PRs", ""]
    lines += [f"- [{r['repo']}#{r['number']}]({r['url']}) ({r['agent']}): {r['title'][:90]} "
              f"→ reverted by [#{r['revert']['number']}]({r['revert']['url']})" for r in rows if r.get("revert")]
    (out / "summary.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines[:9]))


if __name__ == "__main__":
    main()
