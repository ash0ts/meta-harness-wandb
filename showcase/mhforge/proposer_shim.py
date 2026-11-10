# SPDX-License-Identifier: MIT
"""Subprocess shim for upstream's claude_wrapper (spec 3.19).

`claude_wrapper.run` uses its module-level name `subprocess` only for `Popen`, `PIPE` and `DEVNULL`, and the returned
object only through `stdout`/`stderr` (`readline`, `close`), `poll`, `kill`, `wait` and `returncode`. Installing a
`SubprocessShim` as `claude_wrapper.subprocess` (and a `build_command` wrapper) for one call gives the allow-listed
environment, the pinned binary with the permission mode's exact argv, a tee of stdout with arrival times, a deadline on a
monotonic clock that pauses for reported rate-limit waits, a graceful SIGINT/SIGTERM/SIGKILL escalation to the whole
process group, and group reaping after the CLI ended. No upstream file is edited and the runner's own process environment
is never touched.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable, Iterator, Mapping, TextIO

from . import util
from .errors import UpstreamContractError

__all__ = ["Tee", "ProcProxy", "SubprocessShim", "retry_pause_s", "CREDENTIAL_READ_DENIES", "ArgvSpec",
           "parse_upstream_argv", "session_argv", "command_fingerprint", "installed", "MODES"]

MODES = ("upstream", "restricted", "contained")
CREDENTIAL_READ_DENIES = ("Read(~/.netrc)", "Read(~/.ssh/**)", "Read(~/.config/gh/**)", "Read(~/.config/wandb/**)",
                          "Read(~/Library/Keychains/**)")
_POLL_S = 0.05


# ---------------------------------------------------------------- tee

class Tee:
    """File-like for claude_wrapper's reader threads. Every stdout line is appended to `sink_path` as one JSONL row
    {"t_utc", "stream", "line"}; a stderr Tee appends the raw text. `on_line` sees every line. "" at end of file."""

    def __init__(self, pipe: TextIO, sink_path: Path | None, stream: str, clock: util.Clock | None = None,
                 on_line: Callable[[str], None] | None = None) -> None:
        self._pipe = pipe
        self._sink = Path(sink_path) if sink_path is not None else None
        self._stream = stream
        self._clock = clock or util.SystemClock()
        self._on_line = on_line
        self.eof = threading.Event()
        self.lines = 0
        if self._sink is not None:                     # the sink exists even when the stream stays empty
            self._sink.parent.mkdir(parents=True, exist_ok=True)
            os.close(os.open(self._sink, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600))

    def _write_raw(self, text: str) -> None:
        self._sink.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._sink, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, text.encode("utf-8", "replace"))
        finally:
            os.close(fd)

    def readline(self) -> str:
        try:
            line = self._pipe.readline()
        except ValueError:                             # the pipe was closed under us
            line = ""
        if line == "":
            self.eof.set()
            return ""
        self.lines += 1
        if self._sink is not None:
            if self._stream == "stdout":
                raw = line[:-1] if line.endswith("\n") else line
                util.append_jsonl(self._sink, {"t_utc": util.utc_iso(self._clock.now()), "stream": self._stream,
                                               "line": raw})
            else:
                self._write_raw(line)
        if self._on_line is not None:
            try:
                self._on_line(line)
            except Exception:                          # a callback fault must never stop the reader thread
                pass
        return line

    def close(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            self._pipe.close()
        self.eof.set()


# ---------------------------------------------------------------- the process proxy

def retry_pause_s(line: str) -> float:
    """retry_delay_ms / 1000 of a `system`/`api_retry` event line whose error is a rate limit (or error_status 429);
    0.0 for any other line."""
    try:
        ev = json.loads(line)
    except (TypeError, ValueError):
        return 0.0
    if not isinstance(ev, dict) or ev.get("type") != "system" or ev.get("subtype") != "api_retry":
        return 0.0
    status = ev.get("error_status")
    if ev.get("error") != "rate_limit" and not (status == 429 and not isinstance(status, bool)):
        return 0.0
    delay = ev.get("retry_delay_ms")
    if isinstance(delay, bool) or not isinstance(delay, (int, float)) or delay != delay or delay <= 0:
        return 0.0
    return float(delay) / 1000.0


class ProcProxy:
    """What claude_wrapper.run uses of a Popen object, plus the deadline, the escalation and the group reaping."""

    def __init__(self, popen: subprocess.Popen, *, stdout_sink: Path | None, stderr_sink: Path | None,
                 timeout_s: float | None, grace_s: tuple[float, float], clock: util.Clock) -> None:
        self._popen = popen
        self._clock = clock
        self._lock = threading.Lock()
        self._timeout = None if timeout_s is None else float(timeout_s)
        self._grace = (float(grace_s[0]), float(grace_s[1]))
        self._start = clock.monotonic()
        self._stop = threading.Event()
        self.pid = popen.pid
        self.pgid = popen.pid                          # start_new_session=True: the CLI leads its own process group
        self.args = list(popen.args) if isinstance(popen.args, (list, tuple)) else [popen.args]
        self.timed_out = False
        self.paused_s = 0.0
        self.signals: list[tuple[str, str]] = []
        self.stdout = Tee(popen.stdout, stdout_sink, "stdout", clock, on_line=self._on_stdout_line)
        self.stderr = Tee(popen.stderr, stderr_sink, "stderr", clock)
        self._watcher = threading.Thread(target=self._watch, name="mhf-proposer-deadline", daemon=True)
        self._watcher.start()

    # -- what the wrapper uses
    @property
    def returncode(self) -> int | None:
        return self._popen.returncode

    def poll(self) -> int | None:
        return self._popen.poll()

    def wait(self, timeout: float | None = None) -> int:
        rc = self._popen.wait(timeout)
        self._stop.set()
        return rc

    def kill(self) -> None:
        """SIGKILL to the whole process group (upstream's proc.kill() would leave Bash children behind)."""
        self._signal_group(signal.SIGKILL, "SIGKILL")

    # -- deadline
    def _on_stdout_line(self, line: str) -> None:
        pause = retry_pause_s(line)
        if pause > 0 and self._timeout is not None:
            with self._lock:
                self.paused_s = min(self._timeout, self.paused_s + pause)

    def deadline(self) -> float | None:
        """The deadline on the injected monotonic clock, moved later by the reported rate-limit waits."""
        if self._timeout is None:
            return None
        with self._lock:
            return self._start + self._timeout + self.paused_s

    def _watch(self) -> None:
        while self._popen.poll() is None and not self._stop.is_set():
            limit = self.deadline()
            if limit is not None and self._clock.monotonic() >= limit:
                self._escalate()
                return
            self._stop.wait(_POLL_S)

    def _wait_exit(self, seconds: float) -> bool:
        end = time.monotonic() + seconds               # grace periods guard a real process: counted in real seconds
        while time.monotonic() < end:
            if self._popen.poll() is not None:
                return True
            time.sleep(_POLL_S)
        return self._popen.poll() is not None

    def _escalate(self) -> None:
        self.timed_out = True
        self._signal_group(signal.SIGINT, "SIGINT")
        if self._wait_exit(self._grace[0]):
            return
        self._signal_group(signal.SIGTERM, "SIGTERM")
        if self._wait_exit(self._grace[1]):
            return
        self._signal_group(signal.SIGKILL, "SIGKILL")

    def _signal_group(self, sig: int, name: str) -> bool:
        try:
            os.killpg(self.pgid, sig)
        except (ProcessLookupError, PermissionError):
            return False
        with self._lock:
            self.signals.append((name, util.utc_iso(self._clock.now())))
        return True

    # -- reaping
    def _group_alive(self) -> bool:
        try:
            os.killpg(self.pgid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    def reap_group(self) -> list[str]:
        """After the CLI ended: SIGTERM to what is left of its process group, then SIGKILL after grace_s[1]."""
        self._popen.poll()
        sent: list[str] = []
        if not self._group_alive():
            return sent
        if self._signal_group(signal.SIGTERM, "reap_term"):
            sent.append("reap_term")
        end = time.monotonic() + self._grace[1]
        while time.monotonic() < end:
            self._popen.poll()
            if not self._group_alive():
                return sent
            time.sleep(_POLL_S)
        if self._signal_group(signal.SIGKILL, "reap_kill"):
            sent.append("reap_kill")
        end = time.monotonic() + 2.0
        while time.monotonic() < end and self._group_alive():
            self._popen.poll()
            time.sleep(_POLL_S)
        return sent


class SubprocessShim:
    """Installed as claude_wrapper.subprocess for one session."""

    PIPE = subprocess.PIPE
    DEVNULL = subprocess.DEVNULL

    def __init__(self, *, env: Mapping[str, str], cli_path: str, stream_path: Path, stderr_path: Path,
                 timeout_s: float, grace_s: tuple[float, float], clock: util.Clock | None = None) -> None:
        self.env = {str(k): str(v) for k, v in env.items()}
        self.cli_path = str(cli_path)
        self.stream_path = Path(stream_path)
        self.stderr_path = Path(stderr_path)
        self.timeout_s = timeout_s
        self.grace_s = (float(grace_s[0]), float(grace_s[1]))
        self.clock = clock or util.SystemClock()
        self.last: ProcProxy | None = None

    def Popen(self, cmd: list[str], **kwargs: object) -> ProcProxy:          # noqa: N802 (the name the wrapper calls)
        """The wrapper's `env` is ignored; the allow-listed one is used. FileNotFoundError propagates."""
        if not isinstance(cmd, (list, tuple)) or not cmd or str(cmd[0]) != self.cli_path:
            raise UpstreamContractError("proposer command does not start with the pinned CLI path: the build_command "
                                        "wrapper was bypassed")
        popen = subprocess.Popen([str(c) for c in cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                 cwd=kwargs.get("cwd"), env=dict(self.env), start_new_session=True, close_fds=True)
        self.last = ProcProxy(popen, stdout_sink=self.stream_path, stderr_sink=self.stderr_path,
                              timeout_s=self.timeout_s, grace_s=self.grace_s, clock=self.clock)
        return self.last


# ---------------------------------------------------------------- argv per permission mode

@dataclass(frozen=True)
class ArgvSpec:
    mode: str                                      # "upstream" | "restricted" | "contained"
    cli_path: str
    model: str
    effort: str
    max_budget_usd: str                            # proposer.max_budget_usd_per_session, as the manifest writes it
    settings_file: Path                            # <session dir>/settings.json (every mode)
    scratch_dir: Path | None = None                # restricted and contained
    addendum: str | None = None                    # restricted only: the rendered addendum


_VALUE_FLAGS = {"-p": "prompt", "--output-format": "output_format", "--model": "model", "--setting-sources":
                "setting_sources", "--tools": "tools", "--plugin-dir": "plugin_dir", "--append-system-prompt":
                "system_prompt", "--effort": "effort"}
_BOOL_FLAGS = ("--dangerously-skip-permissions", "--verbose", "--disable-slash-commands", "--strict-mcp-config")
_REQUIRED = ("--dangerously-skip-permissions", "-p", "--output-format", "--verbose", "--model", "--setting-sources",
             "--tools", "--allowedTools", "--disable-slash-commands", "--strict-mcp-config", "--plugin-dir",
             "--append-system-prompt", "--effort")


def parse_upstream_argv(cmd: list[str]) -> dict:
    """{"prompt", "model", "tools", "allowed_tools", "plugin_dir", "system_prompt", "effort"} from the command upstream's
    claude_wrapper.build_command built (L127-L168); UpstreamContractError when a flag upstream always emits for the
    proposer is missing, a value is missing, or an unknown token appears."""
    if not isinstance(cmd, (list, tuple)) or not cmd:
        raise UpstreamContractError("upstream proposer command is empty")
    seen: dict[str, object] = {}
    i, n = 1, len(cmd)
    while i < n:
        tok = str(cmd[i])
        if tok in _VALUE_FLAGS:
            if i + 1 >= n:
                raise UpstreamContractError(f"upstream proposer command: {tok} has no value")
            seen[tok] = str(cmd[i + 1])
            i += 2
        elif tok in _BOOL_FLAGS:
            seen[tok] = True
            i += 1
        elif tok == "--allowedTools":
            j = i + 1
            while j < n and not str(cmd[j]).startswith("--"):
                j += 1
            if j == i + 1:
                raise UpstreamContractError("upstream proposer command: --allowedTools has no value")
            seen[tok] = [str(c) for c in cmd[i + 1:j]]
            i = j
        elif tok == "--disallowedTools":
            raise UpstreamContractError("upstream proposer command carries --disallowedTools (propose_claude never "
                                        "passes it)")
        else:
            raise UpstreamContractError(f"upstream proposer command: unknown token at position {i}")
    missing = [flag for flag in _REQUIRED if flag not in seen]
    if missing:
        raise UpstreamContractError(f"upstream proposer command lacks {', '.join(missing)}")
    if seen["--output-format"] != "stream-json" or seen["--setting-sources"] != "":
        raise UpstreamContractError("upstream proposer command: unexpected --output-format or --setting-sources")
    return {"prompt": seen["-p"], "model": seen["--model"], "tools": seen["--tools"],
            "allowed_tools": list(seen["--allowedTools"]), "plugin_dir": seen["--plugin-dir"],
            "system_prompt": seen["--append-system-prompt"], "effort": seen["--effort"]}


def session_argv(cmd: list[str], spec: ArgvSpec) -> list[str]:
    """Pure: the exact argv of the permission mode (3.19). Prompt, tools, allowed tools, plugin dir and skill text come
    from upstream's command; the binary, model, effort, settings file, scratch dir, addendum and cap are ours."""
    up = parse_upstream_argv(cmd)
    if spec.mode not in MODES:
        raise ValueError(f"unknown permission mode {spec.mode!r}")
    cli, settings, cap = str(spec.cli_path), str(spec.settings_file), str(spec.max_budget_usd)
    if spec.mode == "restricted":
        if spec.scratch_dir is None or spec.addendum is None:
            raise ValueError("mode restricted needs a scratch directory and the rendered addendum")
        return [cli, "-p", up["prompt"], "--output-format", "stream-json", "--verbose",
                "--model", spec.model, "--effort", spec.effort, "--setting-sources", "",
                "--tools", up["tools"],
                "--disable-slash-commands", "--strict-mcp-config",
                "--plugin-dir", up["plugin_dir"],
                "--permission-mode", "dontAsk", "--permission-prompts", "none",
                "--settings", settings,
                "--add-dir", str(spec.scratch_dir),
                "--disallowedTools", *CREDENTIAL_READ_DENIES,
                "--append-system-prompt", up["system_prompt"] + "\n\n" + spec.addendum,
                "--max-budget-usd", cap]
    argv = [cli, "--dangerously-skip-permissions", "-p", up["prompt"], "--output-format", "stream-json", "--verbose",
            "--model", spec.model, "--setting-sources", "",
            "--tools", up["tools"],
            "--allowedTools", *up["allowed_tools"],
            "--disable-slash-commands", "--strict-mcp-config",
            "--plugin-dir", up["plugin_dir"],
            "--append-system-prompt", up["system_prompt"],
            "--effort", spec.effort,
            "--settings", settings]
    if spec.mode == "contained":
        if spec.scratch_dir is None:
            raise ValueError("mode contained needs a scratch directory")
        argv += ["--add-dir", str(spec.scratch_dir)]
    return argv + ["--max-budget-usd", cap]


_REDACTED = ("-p", "--append-system-prompt")


def command_fingerprint(cmd: list[str]) -> dict:
    """{"argv_sha256", "argv_flags": [...], "redacted": {flag: {"sha256", "chars"}}}: the flag names in order and the
    prompt and system prompt only as hash and length. No prompt or skill text."""
    tokens = [str(c) for c in cmd]
    flags: list[str] = []
    redacted: dict[str, dict] = {}
    skip_next = False
    for i, tok in enumerate(tokens[1:], start=1):
        if skip_next:
            skip_next = False
            continue
        if tok.startswith("-"):
            flags.append(tok)
            if tok in _REDACTED and i + 1 < len(tokens):
                value = tokens[i + 1]
                redacted[tok] = {"sha256": util.sha256_bytes(value.encode("utf-8")), "chars": len(value)}
                skip_next = True
    return {"argv_sha256": util.sha256_bytes(util.canonical_json(tokens)), "argv_flags": flags, "redacted": redacted}


@contextlib.contextmanager
def installed(claude_wrapper: ModuleType, shim: SubprocessShim, *,
              build_command: Callable[..., list[str]]) -> Iterator[None]:
    """Sets claude_wrapper.subprocess and claude_wrapper.build_command; restores both in a finally."""
    old_subprocess = claude_wrapper.subprocess
    old_build = claude_wrapper.build_command
    claude_wrapper.subprocess = shim
    claude_wrapper.build_command = build_command
    try:
        yield
    finally:
        claude_wrapper.subprocess = old_subprocess
        claude_wrapper.build_command = old_build
