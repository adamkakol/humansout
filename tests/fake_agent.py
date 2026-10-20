"""A scripted stand-in for an agent harness, driven by `humansout watch` in tests.

Usage: fake_agent.py <prompt_file> <personality>. It reads the turn's prompt, works out the
task (build, review, respond) and acts through the real `humansout` CLI on $HUMANSOUT_HOME.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

V1 = ("import unicodedata\n\ndef slugify(text):\n"
      "    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode()\n"
      "    return '-'.join(text.lower().split())\n")
V2 = ("import unicodedata\n\ndef slugify(text):\n    text = text.replace('ł', 'l').replace('Ł', 'L')\n"
      "    text = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode()\n"
      "    return '-'.join(text.lower().split())\n")
TESTS = {"breaker": ("test_polish.py", "slugify('Łódź'), 'lodz'"),
         "breaker2": ("test_unicode.py", "slugify('Łódź'), 'łódź'")}


def ho(*args):
    out = subprocess.run([sys.executable, "-m", "humansout", "--home", os.environ["HUMANSOUT_HOME"], *args],
                         capture_output=True, text=True)
    print("$ humansout", *args, "->", out.returncode, out.stdout[-300:], out.stderr[-300:])
    return json.loads(out.stdout) if out.returncode == 0 and out.stdout.strip().startswith(("{", "[")) else None


def main():
    prompt = Path(sys.argv[1]).read_text()
    who = sys.argv[2]
    mission = re.search(r"Mission ([0-9a-f]{64})|mission ([0-9a-f]{64})", prompt)
    mission = next(g for g in mission.groups() if g)
    claim = re.search(r"Claim ([0-9a-f]{64})|claim ([0-9a-f]{64})", prompt)
    claim = next(g for g in claim.groups() if g) if claim else None
    work = Path.cwd()
    if "has no accepted result" in prompt and who == "builder":
        assert "join it first" in prompt
        ho("join", mission)
        (work / "slugify.py").write_text(V1)
        cid = ho("artifact", "publish", mission, str(work / "slugify.py"))["cid"]
        ho("done", mission, "--evidence", "v1: basic test passes", "--cid", cid)
    elif "is open for challenge" in prompt and who in TESTS:
        name, body = TESTS[who]
        (work / name).write_text("import unittest\nfrom slugify import slugify\n\nclass T(unittest.TestCase):\n"
                                 f"    def test_case(self):\n        self.assertEqual({body})\n")
        ho("challenge", claim, "--evidence", f"{name} fails", "--counterexample", str(work / name))
    elif "has new objection" in prompt and who == "builder":
        state = ho("state", mission)
        mine = next(c for c in state["claims"] if c["id"] == claim)
        for ch in mine["challenges"]:
            names = [f["name"] for f in ch.get("files", [])]
            if ch["status"] != "pending":
                continue
            if any(n.startswith("test_unicode") for n in names):
                ho("dispute", ch["id"], "--evidence", "The mission asks for ASCII slugs; keeping ł contradicts it")
            elif any(n.startswith("test_polish") for n in names) and ch.get("verification") == "reproduced":
                (work / "slugify.py").write_text(V2)
                cid = ho("artifact", "publish", mission, str(work / "slugify.py"))["cid"]
                ho("done", mission, "--evidence", "v2: maps ł to l first", "--cid", cid)
    else:
        print("nothing to do for", who)


if __name__ == "__main__":
    main()
