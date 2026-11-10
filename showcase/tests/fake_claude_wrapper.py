# SPDX-License-Identifier: MIT
"""A module-like stand-in for upstream's claude_wrapper (spec 5.1).

`new_module(plugin_dir)` returns a fresh module whose `run` uses its module-level name `subprocess` exactly as upstream's
`claude_wrapper.run` does at L583-L668 (Popen with PIPE/DEVNULL, two reader threads that call only `readline` and
`close`, a poll loop, `kill()` only when the wrapper has a deadline of its own, then `wait()` and `returncode`), and whose
`build_command` builds upstream's command (L115-L168). Each call gets its own module, so a test that swaps attributes
cannot leak into another.
"""

from __future__ import annotations

import types
from pathlib import Path

_SOURCE = r'''
import json
import os
import queue
import subprocess
import threading
import time
from pathlib import Path


class SessionResult:
    def __init__(self, **kw):
        self.__dict__.update(kw)
        self.shown = 0

    def show(self):
        self.shown += 1
        if self.exit_code != 0:
            print(f"  FAILED (exit={self.exit_code})")
            return
        print(f"  text: {self.text[:200]}")


def build_command(prompt, model="sonnet", allowed_tools=None, system_prompt=None, tools=None, disallowed_tools=None,
                  disable_skills=True, disable_mcp=True, effort=None):
    cmd = ["claude", "--dangerously-skip-permissions", "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--model", model, "--setting-sources", ""]
    effective_tools = tools if tools is not None else allowed_tools
    if effective_tools:
        cmd.extend(["--tools", ",".join(effective_tools)])
    if allowed_tools:
        cmd.append("--allowedTools")
        cmd.extend(allowed_tools)
    if disallowed_tools:
        cmd.append("--disallowedTools")
        cmd.extend(disallowed_tools)
    if disable_skills:
        cmd.append("--disable-slash-commands")
    if disable_mcp:
        cmd.append("--strict-mcp-config")
    _EMPTY_PLUGIN_DIR.mkdir(exist_ok=True)
    cmd.extend(["--plugin-dir", str(_EMPTY_PLUGIN_DIR)])
    if system_prompt:
        cmd.extend(["--append-system-prompt", system_prompt])
    if effort:
        cmd.extend(["--effort", effort])
    return cmd


def _enqueue_lines(pipe, q, stream_name):
    try:
        for line in iter(pipe.readline, ""):
            q.put((stream_name, line))
    finally:
        pipe.close()


def _load_skills(skills):
    loaded = []
    for s in skills or ():
        p = Path(s)
        if p.is_dir() and (p / "SKILL.md").is_file():
            loaded.append({"path": str(p / "SKILL.md"), "name": p.name, "content": (p / "SKILL.md").read_text()})
    return loaded


def run(prompt, model="sonnet", allowed_tools=None, tools=None, disallowed_tools=None, cwd=None, log_dir=None,
        name=None, system_prompt=None, skill_path=None, skills=None, skill_dir=None, timeout_seconds=None,
        disable_skills=True, disable_mcp=True, progress=True, effort=None):
    global LAST_COMMAND
    if allowed_tools is None:
        allowed_tools = ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]
    all_skills = _load_skills(skills)
    if all_skills:
        skill_text = "\n\n".join(f"## Skill: {s['name']}\n{s['content']}" for s in all_skills)
        system_prompt = f"Follow these skill instructions:\n\n{skill_text}\n\n" + (system_prompt or "")
    cmd = build_command(prompt, model, allowed_tools, system_prompt, tools=tools, disallowed_tools=disallowed_tools,
                        disable_skills=disable_skills, disable_mcp=disable_mcp, effort=effort)
    LAST_COMMAND = list(cmd)
    env = {"FAKE_WRAPPER_PARENT_ENV": "1", "PATH": "/usr/bin:/bin"}   # upstream passes a copy of its own environment
    start = time.time()
    stdout_lines, stderr_lines = [], []
    exit_code = 0
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                text=True, cwd=cwd, env=env)
        deadline = start + timeout_seconds if timeout_seconds else None
        q = queue.Queue()
        threading.Thread(target=_enqueue_lines, args=(proc.stdout, q, "stdout"), daemon=True).start()
        threading.Thread(target=_enqueue_lines, args=(proc.stderr, q, "stderr"), daemon=True).start()
        while True:
            if deadline and time.time() > deadline:
                proc.kill()
                stderr_lines.append(f"\nProcess timed out after {timeout_seconds} seconds.")
                exit_code = 124
                break
            try:
                stream_name, line = q.get(timeout=0.1)
            except queue.Empty:
                if proc.poll() is not None:
                    break
                continue
            (stdout_lines if stream_name == "stdout" else stderr_lines).append(line)
        proc.wait()
        if exit_code == 0:
            exit_code = proc.returncode
    except FileNotFoundError as e:
        stderr_lines = [str(e)]
        exit_code = 127
    session_id, text, cost = "", "", 0.0
    events = []
    for line in "".join(stdout_lines).splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        events.append(ev)
        if ev.get("type") == "result":
            session_id = ev.get("session_id", "")
            text = ev.get("result", "") or ""
            cost = ev.get("total_cost_usd", 0.0)
    result = SessionResult(prompt=prompt, text=text, session_id=session_id, exit_code=exit_code, cost_usd=cost,
                           raw_events=events, command=cmd, cwd=cwd, stderr="".join(stderr_lines), name=name,
                           duration_seconds=time.time() - start)
    if log_dir:
        run_dir = Path(log_dir) / f"{time.strftime('%Y%m%d_%H%M%S')}_{name or 'run'}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "meta.json").write_text(json.dumps({"command": cmd, "exit_code": exit_code,
                                                       "session_id": session_id}))
        result.log_dir = str(run_dir)
    return result
'''


def new_module(plugin_dir: Path, name: str = "claude_wrapper") -> types.ModuleType:
    """A fresh claude_wrapper-like module; `_EMPTY_PLUGIN_DIR` is `plugin_dir` (upstream: <EX>/.empty_plugins)."""
    module = types.ModuleType(name)
    module.__file__ = __file__
    exec(compile(_SOURCE, f"<fake {name}>", "exec"), module.__dict__)
    module._EMPTY_PLUGIN_DIR = Path(plugin_dir)
    module.LAST_COMMAND = None
    return module
