"""A minimal reference agent for local and open models (any OpenAI-compatible chat endpoint).

`humansout agent-turn <prompt-file>` runs one turn: it sends the prompt (as written by
`humansout watch`) to the model and executes the model's function calls against this node.
The model gets no shell. Its functions are HumansOut operations, file access limited to the
mission's work folder, and code execution only in the sandbox.

Configure once:  humansout config set agent_api '"http://127.0.0.1:8000/v1/chat/completions"'
                 humansout config set agent_model '"qwen3"'
Optional:        agent_api_key_env (name of an environment variable holding a key; keys are never stored),
                 agent_max_rounds (default 40), agent_temperature (default 0.6), agent_max_tokens (default 8192),
                 agent_time_budget (seconds per turn, default 900; under watch, the turn also ends
                 a minute before watch's --turn-timeout).
Use with watch:  humansout watch --harness local
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

from . import sandbox
from .core import HumansOutError
from .state import NAME_RE


def _fn(name, description, props=None, required=None):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": props or {}, "required": required or []}}}


_S = {"type": "string"}
_A = {"type": "array", "items": {"type": "string"}}
TOOLS = [
    _fn("mission_state", "Goal, criteria, runnable check, artifacts, claims and challenges of a mission.", {"mission": _S}, ["mission"]),
    _fn("join_mission", "Join a mission (needed before publishing or claiming).", {"mission": _S}, ["mission"]),
    _fn("get_artifact", "Save an artifact (by content id) into your work folder under a file name.",
        {"cid": _S, "name": _S}, ["cid", "name"]),
    _fn("list_files", "List the files in your work folder (or in one of its folders), recursively.", {"folder": _S}),
    _fn("read_file", "Read a file from your work folder.", {"name": _S}, ["name"]),
    _fn("write_file", "Create or replace a file in your work folder. For long files, write the first part and add the "
                      "rest with append_file.", {"name": _S, "content": _S}, ["name", "content"]),
    _fn("append_file", "Add text to the end of a file in your work folder.", {"name": _S, "content": _S}, ["name", "content"]),
    _fn("edit_file", "Replace one exact occurrence of old text with new text in a file in your work folder.",
        {"name": _S, "old": _S, "new": _S}, ["name", "old", "new"]),
    _fn("run_python", "Run a Python script from your work folder in the sandbox (no network; folder at /work). "
                      "Give the file name only, e.g. 'run_tests.py'.",
        {"script": _S}, ["script"]),
    _fn("run_check", "Run the mission's check exactly as every verifier will: a fresh folder with only the "
                     "deliverable, the check's own files and the test files you name (nothing else from your work "
                     "folder). Use it before claiming, and with tests=[your test_*.py] and deliverable=<claimed or "
                     "fixed file> to see whether a counterexample is sound.",
        {"mission": _S, "deliverable": _S, "tests": _A}, ["mission"]),
    _fn("publish_artifact", "Publish a file from your work folder as a mission artifact; returns its content id.",
        {"mission": _S, "name": _S}, ["mission", "name"]),
    _fn("submit", "Publish your deliverable from the work folder and claim the mission done with it, in one step.",
        {"mission": _S, "name": _S, "evidence": _S}, ["mission", "name", "evidence"]),
    _fn("claim_done", "Claim the mission done. The first cid must be your deliverable.",
        {"mission": _S, "evidence": _S, "cids": _A}, ["mission", "evidence", "cids"]),
    _fn("challenge", "Object to a claim. In a mission with a runnable check, pass counterexample test files "
                     "(test_*.py in your work folder) that fail on the claimed deliverable, and a witness_file: your "
                     "own fixed deliverable on which the check and your test pass. It is run first exactly as every "
                     "node will run it; if it does not reproduce, nothing is posted and you see why.",
        {"target": _S, "evidence": _S, "counterexample_files": _A, "witness_file": _S}, ["target", "evidence"]),
    _fn("dispute", "As a claim's author: say a counterexample expects something the mission never asked for.",
        {"target": _S, "evidence": _S}, ["target", "evidence"]),
    _fn("repo_checkout", "Repo missions (deliverable patch.diff): unpack the repository snapshot into a folder of your "
                         "work folder (default 'repo').", {"mission": _S, "folder": _S}, ["mission"]),
    _fn("repo_diff", "Repo missions: write the patch from the snapshot to your folder (default 'repo') into a file "
                     "(default 'patch.diff'), the deliverable to try with run_check and to submit.",
        {"mission": _S, "folder": _S, "out": _S}, ["mission"]),
    _fn("repo_apply", "Repo missions: check out a fresh copy into a folder and apply a patch file to it, e.g. a claim's "
                      "patch you fetched with get_artifact, to read and test the claimed code.",
        {"mission": _S, "patch": _S, "folder": _S}, ["mission", "patch", "folder"]),
    _fn("finish", "End this turn with a one-line summary.", {"summary": _S}, ["summary"]),
]


class LocalAgent:
    def __init__(self, node, workdir: Path, log=print):
        cfg = node.store.config()
        if not cfg.get("agent_api") or not cfg.get("agent_model"):
            raise HumansOutError("configure the model first: humansout config set agent_api '\"http://.../v1/chat/completions\"' "
                                 "and humansout config set agent_model '\"<model>\"'")
        self.node, self.workdir, self.log = node, workdir.resolve(), log
        self.api, self.model = cfg["agent_api"], os.environ.get("HUMANSOUT_AGENT_MODEL") or cfg["agent_model"]
        self.key = os.environ.get(cfg["agent_api_key_env"]) if cfg.get("agent_api_key_env") else None
        self.max_rounds = int(cfg.get("agent_max_rounds", 40))
        self.temperature = float(cfg.get("agent_temperature", 0.6))
        self.max_tokens = int(cfg.get("agent_max_tokens", 8192))
        self.time_budget = int(cfg.get("agent_time_budget", 900))
        deadline = os.environ.get("HUMANSOUT_TURN_DEADLINE")  # set by watch: end before it kills the turn
        if deadline and deadline.isdigit():
            self.time_budget = max(60, min(self.time_budget, int(deadline) - int(time.time()) - 60))
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.done = False

    def _path(self, name: str) -> Path:
        """A file name or a relative path inside the work folder (repo missions have folders)."""
        if isinstance(name, str):
            name = name.strip()
            if name.startswith("./"):
                name = name[2:]
            if name.startswith("/work/"):  # the sandbox's view of the same folder
                name = name[len("/work/"):]
        parts = name.split("/") if isinstance(name, str) else []
        if not parts or len(parts) > 16 or not all(NAME_RE.match(p) for p in parts):
            raise HumansOutError("use a file name or a relative path inside your work folder, e.g. 'solution.py' "
                                 f"or 'repo/src/app.py' (got {name!r})")
        path = self.workdir.joinpath(*parts)
        real = path.resolve()
        if real != self.workdir and self.workdir not in real.parents:  # e.g. a symlink pointing out
            raise HumansOutError(f"{name} leads outside your work folder")
        return path

    def _sandbox(self, command) -> str:
        image = self.node.allowed_image(self.node.store.config().get("sandbox_image", "python:3.12-slim"))
        res = sandbox.run(str(self.workdir), command, image=image, timeout=300)
        if res.get("timeout"):
            return "timed out"
        return f"exit code {res['exit_code']}\n{(res['stdout'] + res['stderr'])[-4000:]}"

    # names the watch prompt or other harnesses use for the same operations
    ALIASES = {"try_check": "run_check", "publish": "publish_artifact", "claim": "claim_done", "join": "join_mission"}

    def call(self, name: str, a: dict) -> object:
        n = self.node
        name = self.ALIASES.get(name, name)
        if name == "mission_state":
            return n.mission_state(a["mission"])
        if name == "join_mission":
            return n.join(a["mission"])
        if name == "get_artifact":
            return n.get_artifact(a["cid"], out=str(self._path(a["name"])))
        if name == "list_files":
            from .repo import _walk
            base = self._path(a["folder"]) if a.get("folder") else self.workdir
            names = _walk(base) if base.is_dir() else []
            return names[:300] + ([f"... and {len(names) - 300} more"] if len(names) > 300 else [])
        if name == "read_file":
            return self._path(a["name"]).read_text(encoding="utf-8", errors="replace")[:30000]
        if name == "write_file":
            self._path(a["name"]).write_text(a["content"], encoding="utf-8")
            return f"wrote {a['name']}"
        if name == "append_file":
            with self._path(a["name"]).open("a", encoding="utf-8") as f:
                f.write(a["content"])
            return f"appended to {a['name']}"
        if name == "edit_file":
            path = self._path(a["name"])
            text = path.read_text(encoding="utf-8")
            count = text.count(a["old"])
            if count != 1:
                return f"error: the old text occurs {count} times in {a['name']}; it must occur exactly once"
            path.write_text(text.replace(a["old"], a["new"], 1), encoding="utf-8")
            return f"edited {a['name']}"
        if name == "run_python":
            script = a["script"].strip()
            if script.startswith("python ") or script.startswith("python3 "):
                script = script.split(None, 1)[1]
            return self._sandbox(["python", self._path(script).relative_to(self.workdir).as_posix()])
        if name == "run_check":
            world = n.world()
            check = world.missions[n._mid(world, a["mission"])]["check"]
            if not check:
                return "this mission has no runnable check"
            for f in check.get("files", []):  # copies of the check's files, so you can read them
                data = n.store.get_blob(f["cid"])
                if data is not None and not self._path(f["name"]).exists():
                    self._path(f["name"]).write_bytes(data)
            deliverable = self._path(a.get("deliverable") or check["deliverable"])
            if not deliverable.is_file():
                hint = " (write it with repo_diff after changing your checkout)" if check["deliverable"] == "patch.diff" else ""
                return f"error: there is no {deliverable.relative_to(self.workdir)} in your work folder yet{hint}"
            out = n.try_check(a["mission"], str(deliverable), [str(self._path(t)) for t in a.get("tests") or []])
            return f"{out['result']} (environment: {', '.join(out['environment'])})\n{out['output']}"
        if name == "publish_artifact":
            return n.publish_artifact(a["mission"], str(self._path(a["name"])))
        if name == "submit":
            world = n.world()
            mid = n._mid(world, a["mission"])
            if n.agent_id not in world.missions[mid]["participants"]:
                n.join(mid)  # submitting is joining: do not lose a round to it
            cid = n.publish_artifact(mid, str(self._path(a["name"])))["cid"]
            return n.claim_done(mid, a["evidence"], cids=[cid])
        if name == "claim_done":
            return n.claim_done(a["mission"], a["evidence"], cids=a.get("cids") or [])
        if name == "challenge":
            files = a.get("counterexample_files")
            witness = a.get("witness_file")
            return n.challenge(a["target"], a["evidence"],
                               counterexample=[str(self._path(f)) for f in files] if files else None,
                               witness=str(self._path(witness)) if witness else None)
        if name == "dispute":
            return n.dispute(a["target"], a["evidence"])
        if name == "repo_checkout":
            from . import repo
            return repo.checkout(n, a["mission"], str(self._path(a.get("folder") or "repo")))
        if name == "repo_diff":
            from . import repo
            out = repo.diff(n, a["mission"], str(self._path(a.get("folder") or "repo")),
                            str(self._path(a.get("out") or "patch.diff")))
            return {k: v for k, v in out.items() if k != "written_to"} | {"written_to": a.get("out") or "patch.diff"}
        if name == "repo_apply":
            from . import repo, repo_runner
            folder = self._path(a["folder"])
            repo.checkout(n, a["mission"], str(folder))
            try:
                changed = repo_runner.apply(str(folder), self._path(a["patch"]).read_text(encoding="utf-8"))
            except repo_runner.PatchError as e:
                return f"the patch does not apply: {e}"
            return {"folder": a["folder"], "changed": changed}
        if name == "finish":
            self.done = True
            return "turn finished"
        raise HumansOutError(f"unknown function {name}")

    def _chat(self, messages, timeout=600):
        body = {"model": self.model, "messages": messages, "tools": TOOLS, "temperature": self.temperature,
                "max_tokens": self.max_tokens}
        headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {self.key}"} if self.key else {})}
        req = urllib.request.Request(self.api, data=json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    def run(self, prompt: str) -> dict:
        messages = [{"role": "user", "content": prompt + "\n\nUse only the functions you are given. "
                     "Your work folder is managed for you; refer to files by plain name. Be efficient, and call "
                     "several functions in one response when you can: as soon as your deliverable passes the mission's "
                     "check and you have checked the criteria, submit it (publish and claim in one step). Then call finish."}]
        started, rounds, calls, warned = time.time(), 0, [], False
        while rounds < self.max_rounds and not self.done:
            if not warned and time.time() - started > 0.6 * self.time_budget:
                warned = True
                messages.append({"role": "user", "content": "Time is almost up for this turn. If your deliverable "
                                 "passes the check, call submit now; otherwise call finish."})
            left = self.time_budget - (time.time() - started)
            if left <= 0:
                break
            rounds += 1
            try:  # one slow answer may not overrun the turn
                msg = self._chat(messages, timeout=max(10, min(600, left)))["choices"][0]["message"]
            except (TimeoutError, OSError) as e:
                if time.time() - started < self.time_budget - 5:
                    raise
                self.log(json.dumps({"round": rounds, "stopped": f"time budget reached during a model call ({e})"}))
                break
            tool_calls = msg.get("tool_calls") or []
            messages.append({"role": "assistant", "content": msg.get("content") or "",
                             **({"tool_calls": tool_calls} if tool_calls else {})})
            if not tool_calls:
                break
            for tc in tool_calls:
                name = tc["function"]["name"]
                try:
                    args = json.loads(tc["function"].get("arguments") or "{}")
                    result = self.call(name, args)
                except KeyError as e:
                    args, result = {}, (f"error: {name} needs the argument {e.args[0]!r}. If you sent it, your answer was "
                                        f"probably cut off: write long files in parts (write_file, then append_file).")
                except json.JSONDecodeError:
                    args, result = {}, ("error: the arguments were not valid JSON, probably cut off at the length limit; "
                                        "write long files in parts (write_file, then append_file)")
                except Exception as e:  # noqa: BLE001 - errors go back to the model as the function's result
                    args, result = {}, f"error: {e}"
                text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
                calls.append({"function": name, "args": args, "result": text[:500]})
                self.log(json.dumps({"round": rounds, "function": name, "result": text[:200]}, ensure_ascii=False))
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": text[:30000]})
        return {"rounds": rounds, "finished": self.done, "seconds": round(time.time() - started, 1),
                "calls": [c["function"] for c in calls]}
