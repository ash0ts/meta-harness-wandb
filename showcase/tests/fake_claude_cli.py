# SPDX-License-Identifier: MIT
"""Writes a fake `claude` executable for the proposer tests (spec 5.1). It never contacts anything.

`write_fake_cli(directory, *sessions, **defaults)` writes `<directory>/claude` (a fixed script: its SHA-256 does not change
when the behaviour is reconfigured) and `<directory>/claude.config.json` (a list of plans, one per call; the last plan is
reused). Each session is a mode name, a tuple of mode names, or a dict {"modes": [...], <options>}; `defaults` are options
for every session. The plans are built here, in the test process, so the executable only replays them: it emits
stream-json lines, writes files, sets signal behaviours, spawns children, sleeps and exits, and it records its argv, the
NAMES of its environment and its cwd in `<directory>/claude.calls.jsonl`. With `write_transcript` it also writes a
synthetic transcript under `$HOME/.claude/projects/<slug of cwd>/<session id>.jsonl` in the shape of the corrected fixture
(one `api-request` record per message, `requestRef` on every assistant record, `sandboxed` on Bash tool results, a
`cost-state` record on a normal exit only, `version` on every record but `queue-operation` and `cost-state`).
"""

from __future__ import annotations

import hashlib
import json
import stat
import sys
from pathlib import Path

DEFAULT_VERSION = "2.1.317"
MODES = ("ok", "graceful", "ignore_int", "ignore_all", "limit_text", "not_logged_in", "writes_pending", "mentions_limit",
         "bad_hypothesis", "writes_stray_file", "edits_summary", "reads_state", "edits_upstream_file", "exit_nonzero",
         "spawns_child", "grandchild", "no_result_exit0", "spend_limit", "limit_window", "transient", "max_budget",
         "model_fallback", "wrong_effort", "plugins_loaded", "api_retry_wait", "unsandboxed_bash",
         "sandbox_unavailable", "no_bash")
DEFAULTS = {"model": "claude-opus-5-5", "version": DEFAULT_VERSION, "effort": "max", "init": {}, "pending_path": None,
            "candidates": ["x"], "base_system": "fewshot_all", "summary_path": None, "child_pid_file": None,
            "sleep_s": 30.0, "total_cost_usd": 1.25, "write_transcript": True, "bash_sandboxed": True, "resets_at": None,
            "rate_limit_type": "five_hour", "extra_events": [], "writes": [], "result": {}, "exit_code": None,
            "retries": 3, "retry_delay_ms": 600000, "loaded": ("plugins", "skills", "mcp_servers", "memory_paths"),
            "result_text": "SYNTHETIC: wrote candidates and pending_eval.json."}

_SCRIPT = r'''
import json, os, re, signal, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
ENV = getattr(os, "env" + "iron")
argv = sys.argv
calls_path = os.path.join(HERE, "claude.calls.jsonl")
try:
    with open(calls_path) as fh:
        n = sum(1 for line in fh if line.strip())
except FileNotFoundError:
    n = 0
with open(calls_path, "a") as fh:
    fh.write(json.dumps({"n": n, "argv": argv, "env_names": sorted(ENV), "cwd": os.getcwd(), "pid": os.getpid(),
                         "pgid": os.getpgid(0)}) + "\n")
with open(os.path.join(HERE, "claude.config.json")) as fh:
    plans = json.load(fh)["plans"]
plan = plans[min(n, len(plans) - 1)]
sid = "00000000-0000-4000-8000-%012d" % (n + 1)
prompt = argv[argv.index("-p") + 1] if "-p" in argv else ""

def sub(v):
    if isinstance(v, str):
        if v == "__PROMPT__":
            return prompt
        return v.replace("__SID__", sid).replace("__CWD__", os.getcwd())
    if isinstance(v, list):
        return [sub(x) for x in v]
    if isinstance(v, dict):
        return {k: sub(x) for k, x in v.items()}
    return v

def emit(ev):
    sys.stdout.write(json.dumps(sub(ev)) + "\n")
    sys.stdout.flush()

transcript = None
on_int = []

def graceful(signum, frame):
    for ev in on_int:
        emit(ev)
    os._exit(0)

for act in plan:
    op = act["op"]
    if op == "signals":
        on_int = act.get("on_int", [])
        if act.get("int") == "graceful":
            signal.signal(signal.SIGINT, graceful)
        elif act.get("int") == "ignore":
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        if act.get("term") == "ignore":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
    elif op == "transcript":
        home = ENV.get("HOME")
        if home:
            d = os.path.join(home, ".claude", "projects", re.sub(r"[^A-Za-z0-9]", "-", os.getcwd()))
            os.makedirs(d, exist_ok=True)
            transcript = os.path.join(d, sid + ".jsonl")
            with open(transcript, "w") as fh:
                for rec in act["records"]:
                    fh.write(json.dumps(sub(rec)) + "\n")
    elif op == "emit":
        emit(act["event"])
    elif op == "stderr":
        sys.stderr.write(act["text"])
        sys.stderr.flush()
    elif op == "write":
        path = act["path"] if os.path.isabs(act["path"]) else os.path.join(os.getcwd(), act["path"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a" if act.get("append") else "w") as fh:
            fh.write(act["text"])
    elif op == "spawn":
        code = ("import os, signal, time\n"
                + ("signal.signal(signal.SIGINT, signal.SIG_IGN)\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                   if act.get("ignore_signals") else "")
                + "open(%r, 'w').write(str(os.getpid()))\ntime.sleep(120)\n" % act["pid_file"])
        subprocess.Popen([sys.executable, "-I", "-c", code])
        for _ in range(200):
            if os.path.exists(act["pid_file"]) and open(act["pid_file"]).read().strip():
                break
            time.sleep(0.01)
    elif op == "sleep":
        end = time.time() + act["s"]
        while time.time() < end:
            time.sleep(0.05)
    elif op == "exit":
        if act.get("cost_state") is not None and transcript:
            with open(transcript, "a") as fh:
                fh.write(json.dumps(sub(act["cost_state"])) + "\n")
        sys.stdout.flush()
        sys.exit(act["code"])
sys.exit(0)
'''


# ---------------------------------------------------------------- event builders

def _assistant(mid: str, model: str, content: list, parent: str | None = None) -> dict:
    return {"type": "assistant", "session_id": "__SID__", "parent_tool_use_id": parent,
            "message": {"id": mid, "type": "message", "role": "assistant", "model": model, "content": content,
                        "stop_reason": None, "usage": {"input_tokens": 10, "output_tokens": 4,
                                                       "cache_creation_input_tokens": 100,
                                                       "cache_read_input_tokens": 0}}}


def _tool_result(tid: str, text: str, parent: str | None = None) -> dict:
    return {"type": "user", "session_id": "__SID__", "parent_tool_use_id": parent,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "is_error": False,
                                                     "content": text}]}}


def _tool(tid: str, name: str, inp: dict) -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def _ts(i: int) -> str:
    return "2026-11-10T01:%02d:%02d.000Z" % (i // 60 % 60, i % 60)


def transcript_records(events: list[dict], *, version: str, effort: str, bash_sandboxed: bool | None) -> list[dict]:
    """Main-thread transcript records for the emitted events, in the corrected fixture's shape (no cost-state)."""
    recs: list[dict] = [{"type": "queue-operation", "operation": "enqueue", "timestamp": _ts(0), "sessionId": "__SID__"},
                        {"type": "user", "isSidechain": False, "sessionId": "__SID__", "version": version,
                         "timestamp": _ts(1), "message": {"role": "user", "content": "__PROMPT__"}}]
    bash_ids: set[str] = set()
    refs: dict[str, str] = {}
    t = 2
    for ev in events:
        kind = ev.get("type")
        if kind == "assistant":
            msg = ev["message"]
            side = ev.get("parent_tool_use_id") is not None
            for block in msg.get("content", []):
                if block.get("type") == "tool_use" and block.get("name") == "Bash":
                    bash_ids.add(block["id"])
            if msg["id"] not in refs:
                refs[msg["id"]] = "apireq_fake_%04d" % (len(refs) + 1)
                recs.append({"type": "api-request", "id": refs[msg["id"]], "sessionId": "__SID__", "version": version,
                             "timestamp": _ts(t), "params": {"model": msg["model"],
                                                             "output_config": {"effort": effort}}})
                t += 1
            rec = {"type": "assistant", "isSidechain": side, "sessionId": "__SID__", "version": version,
                   "timestamp": _ts(t), "requestRef": refs[msg["id"]], "message": msg}
            if side:
                rec["agentId"] = "agentfake01"
            recs.append(rec)
            t += 1
        elif kind == "user":
            rec = {"type": "user", "isSidechain": ev.get("parent_tool_use_id") is not None, "sessionId": "__SID__",
                   "version": version, "timestamp": _ts(t), "message": ev["message"]}
            ids = [b.get("tool_use_id") for b in ev["message"]["content"] if b.get("type") == "tool_result"]
            if bash_sandboxed is not None and any(i in bash_ids for i in ids):
                rec["sandboxed"] = bash_sandboxed
            recs.append(rec)
            t += 1
    return recs


def _result(o: dict, **over) -> dict:
    ev = {"type": "result", "subtype": "success", "session_id": "__SID__", "is_error": False, "num_turns": 3,
          "result": o["result_text"], "total_cost_usd": o["total_cost_usd"], "usage": {}, "modelUsage": {},
          "permission_denials": [], "api_error_status": None}
    ev.update(over)
    ev.update(o["result"])
    return ev


def _pending_writes(o: dict, hypothesis: object = "SYNTHETIC hypothesis") -> list[dict]:
    if not o["pending_path"]:
        raise ValueError("a mode that writes pending_eval.json needs the pending_path option")
    writes = []
    cands = []
    for name in o["candidates"]:
        writes.append({"op": "write", "path": f"agents/{name}.py",
                       "text": f"# SYNTHETIC candidate {name}\nfrom ..memory_system import MemorySystem\n"})
        writes.append({"op": "write", "path": f"agents/__pycache__/{name}.cpython-313.pyc", "text": "pyc"})
        cands.append({"name": name, "base_system": o["base_system"], "axis": "exploration", "hypothesis": hypothesis,
                      "components": ["synthetic"]})
    writes.append({"op": "write", "path": o["pending_path"], "text": json.dumps({"candidates": cands})})
    return writes


def build_plan(session: object, defaults: dict) -> list[dict]:
    if isinstance(session, str):
        session = {"modes": [session]}
    elif isinstance(session, (list, tuple)):
        session = {"modes": list(session)}
    o = {**DEFAULTS, **defaults, **{k: v for k, v in session.items() if k != "modes"}}
    modes = set(session.get("modes", ["ok"]))
    unknown = modes - set(MODES)
    if unknown:
        raise ValueError(f"unknown fake CLI modes {sorted(unknown)}")
    model = o["model"]
    init = {"type": "system", "subtype": "init", "session_id": "__SID__", "cwd": "__CWD__",
            "tools": ["Read", "Glob", "Grep", "Agent", "Write", "Edit", "Bash"], "mcp_servers": [], "model": model,
            "permissionMode": "default", "claude_code_version": o["version"], "skills": [], "plugins": []}
    if "wrong_effort" in modes:
        init["effort"] = "high"
    if "plugins_loaded" in modes:
        values = {"plugins": [{"name": "evil", "path": "/nonexistent/evil-plugin"}], "skills": ["evil-skill"],
                  "mcp_servers": [{"name": "evil", "status": "connected"}], "memory_paths": ["/nonexistent/memory"]}
        for key in o["loaded"]:
            init[key] = values[key]
    init.update(o["init"])
    body: list[dict] = []
    if "no_bash" not in modes:
        body += [_assistant("msg_fake_1", model, [{"type": "text", "text": "SYNTHETIC: looking around."},
                                                  _tool("toolu_fake_bash_1", "Bash", {"command": "ls agents"})]),
                 _tool_result("toolu_fake_bash_1", "__init__.py\nno_memory.py")]
    if "reads_state" in modes:
        body += [_assistant("msg_fake_rs", model, [
            _tool("toolu_fake_read_rs", "Read", {"file_path": "../../jobs/val.x.USPTO/a1/out/sidecar.jsonl"}),
            _tool("toolu_fake_bash_rs", "Bash", {"command": "cat ../../calls/c-1.json"})]),
            _tool_result("toolu_fake_read_rs", "SYNTHETIC"), _tool_result("toolu_fake_bash_rs", "SYNTHETIC")]
    if "model_fallback" in modes:
        body += [_assistant("msg_fake_fb", "claude-sonnet-5-5", [{"type": "text", "text": "SYNTHETIC: other model."}]),
                 {"type": "system", "subtype": "model_fallback", "session_id": "__SID__",
                  "from": model, "to": "claude-sonnet-5-5"}]
    body.append(_assistant("msg_fake_2", model, [{"type": "text", "text": "SYNTHETIC: done."}]))
    plan: list[dict] = []
    signals: dict = {"op": "signals"}
    if "graceful" in modes:
        signals.update({"int": "graceful", "on_int": [_result(o, result="SYNTHETIC: interrupted.")]})
    if "ignore_int" in modes:
        signals["int"] = "ignore"
    if "ignore_all" in modes:
        signals.update({"int": "ignore", "term": "ignore"})
    plan.append(signals)
    events = [init] + body
    if o["write_transcript"]:
        sandboxed = False if "unsandboxed_bash" in modes else o["bash_sandboxed"]
        plan.append({"op": "transcript", "records": transcript_records(
            events, version=o["version"], effort="high" if "wrong_effort" in modes else o["effort"],
            bash_sandboxed=sandboxed)})
    early_failure = modes & {"sandbox_unavailable", "not_logged_in"}
    plan.append({"op": "emit", "event": init})
    if not early_failure:
        plan += [{"op": "emit", "event": ev} for ev in body]
    if modes & {"writes_pending", "mentions_limit", "bad_hypothesis"}:
        plan += _pending_writes(o, hypothesis=42 if "bad_hypothesis" in modes else "SYNTHETIC hypothesis")
    if "writes_stray_file" in modes:
        plan.append({"op": "write", "path": "yaml.py", "text": "# SYNTHETIC stray module\n"})
    if "edits_upstream_file" in modes:
        plan.append({"op": "write", "path": "benchmark.py", "text": "\n# SYNTHETIC edit\n", "append": True})
    if "edits_summary" in modes:
        if not o["summary_path"]:
            raise ValueError("edits_summary needs the summary_path option")
        plan.append({"op": "write", "path": o["summary_path"], "append": True,
                     "text": json.dumps({"iteration": 99, "system": "forged"}) + "\n"})
    plan += [dict(w, op="write") for w in o["writes"]]
    plan += [{"op": "emit", "event": ev} for ev in o["extra_events"]]
    for kind in ("spawns_child", "grandchild"):
        if kind in modes:
            if not o["child_pid_file"]:
                raise ValueError(f"{kind} needs the child_pid_file option")
            plan.append({"op": "spawn", "pid_file": o["child_pid_file"], "ignore_signals": kind == "grandchild"})
    if "api_retry_wait" in modes:
        for i in range(int(o["retries"])):
            plan.append({"op": "emit", "event": {"type": "system", "subtype": "api_retry", "session_id": "__SID__",
                                                 "attempt": i + 1, "max_retries": 10,
                                                 "retry_delay_ms": o["retry_delay_ms"], "error_status": 429,
                                                 "error": "rate_limit"}})
    if modes & {"graceful", "ignore_int", "ignore_all", "api_retry_wait", "grandchild"}:
        plan.append({"op": "sleep", "s": float(o["sleep_s"])})
    code = 0
    result: dict | None = _result(o)
    if "mentions_limit" in modes:
        result = _result(o, result="SYNTHETIC: the agent falls back when the char limit reached.")
    if "limit_text" in modes:
        result, code = _result(o, is_error=True, result="Claude AI usage limit reached, try later.", total_cost_usd=0), 1
    if "not_logged_in" in modes:
        plan.append({"op": "stderr", "text": "Not logged in · Please run /login\n"})
        result, code = _result(o, is_error=True, result="Not logged in · Please run /login", total_cost_usd=0), 1
    if "spend_limit" in modes or "limit_window" in modes:
        info = {"status": "rejected", "rateLimitType": "overage" if "spend_limit" in modes else o["rate_limit_type"],
                "isUsingOverage": False}
        if "spend_limit" in modes:
            info.update({"overageStatus": "rejected", "overageDisabledReason": "out_of_credits"})
        elif o["resets_at"] is not None:
            info["resetsAt"] = o["resets_at"]
        plan.append({"op": "emit", "event": {"type": "rate_limit_event", "session_id": "__SID__",
                                             "rate_limit_info": info}})
        result, code = _result(o, is_error=True, api_error_status=429, result="SYNTHETIC: limit hit.",
                               total_cost_usd=0), 1
    if "transient" in modes:
        plan.append({"op": "emit", "event": {"type": "system", "subtype": "api_retry", "session_id": "__SID__",
                                             "attempt": 10, "max_retries": 10, "retry_delay_ms": 1000,
                                             "error_status": 529, "error": "overloaded"}})
        result, code = _result(o, is_error=True, api_error_status=529, result="SYNTHETIC: Overloaded"), 1
    if "max_budget" in modes:
        result, code = _result(o, subtype="error_max_budget_usd", is_error=True, result=""), 1
    if "sandbox_unavailable" in modes:
        result, code = _result(o, subtype="error_during_execution", is_error=True, total_cost_usd=0,
                               startup_failure_reason="sandbox_required_unavailable", result=""), 1
    if "exit_nonzero" in modes:
        plan.append({"op": "stderr", "text": "fatal: SYNTHETIC failure\n"})
        result, code = None, 2
    if "no_result_exit0" in modes:
        result, code = None, 0
    if result is not None:
        plan.append({"op": "emit", "event": result})
    if o["exit_code"] is not None:
        code = int(o["exit_code"])
    cost_state = {"type": "cost-state", "sessionId": "__SID__", "totalCostUSD": o["total_cost_usd"], "modelUsage": {}}
    if modes & {"no_result_exit0", "exit_nonzero"}:
        cost_state = None                                   # the CLI writes cost-state on a normal exit only
    plan.append({"op": "exit", "code": code, "cost_state": cost_state})
    return plan


class FakeCli:
    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)
        self.path = self.dir / "claude"
        self.config_path = self.dir / "claude.config.json"
        self.calls_path = self.dir / "claude.calls.jsonl"

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def configure(self, *sessions: object, **defaults: object) -> "FakeCli":
        plans = [build_plan(s, defaults) for s in (sessions or ("ok",))]
        self.config_path.write_text(json.dumps({"plans": plans}))
        return self

    def calls(self) -> list[dict]:
        try:
            return [json.loads(line) for line in self.calls_path.read_text().splitlines() if line.strip()]
        except FileNotFoundError:
            return []


def write_fake_cli(directory: Path, *sessions: object, **defaults: object) -> FakeCli:
    """Write the executable (once) and its configuration; returns the FakeCli handle."""
    cli = FakeCli(directory)
    cli.dir.mkdir(parents=True, exist_ok=True)
    if not cli.path.exists():
        cli.path.write_text(f"#!{sys.executable} -I\n" + _SCRIPT)
        cli.path.chmod(cli.path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return cli.configure(*sessions, **defaults)


__all__ = ["write_fake_cli", "FakeCli", "build_plan", "transcript_records", "MODES", "DEFAULT_VERSION"]
