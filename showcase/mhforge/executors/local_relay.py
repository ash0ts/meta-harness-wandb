# SPDX-License-Identifier: MIT
"""LocalRelayExecutor (spec 3.17): the relay stub and its worker run as host subprocesses; the worker's calls reach
the gateway over the MHF1 relay through pipes, exactly as they would through a sandbox's exec stream.

NOT ISOLATED. It proves the relay path offline. No gateway listener is opened on the host; the stub listens on
127.0.0.1 behind a per-attempt bearer that only its worker gets (MHF_STUB_BEARER, in the stub's own environment;
never in the job file, an argv or a record). ``mode="stdio"`` runs the stub on its own pipes; ``mode="daemon"``
daemonizes it on a Unix socket under a short ``/tmp/mhf-*`` directory and rotates and replaces ``bridge`` processes,
so the sandbox's default relay path runs offline too.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable, Sequence

from .. import bundle, procenv, util
from ..errors import ExecutorError, MhforgeError
from ..paths import StatePaths
from ..relay import Bridge, FrameChannel, PipeChannel, RelaySession
from ..wire import HandlesRequests, JobIdentity
from . import JobResult, JobSpec, merge_refusal_codes
from .local import MIB, _LocalBase, _process_start, _remove_sock_dir, _Watch

__all__ = ["LocalRelayExecutor", "DEADLINE_MARGIN_S", "KILL_WAIT_S"]

DEADLINE_MARGIN_S = 60.0            # the host's deadline is timeout_s + 60: the stub ends the worker at timeout_s itself
KILL_WAIT_S = 30.0                  # after a KILL frame: how long the stub may take to end the worker and send EXIT
STUB_EXIT_WAIT_S = 15.0             # after EXIT (and BYE): how long the stub may take to leave
SERVE_START_S = 60.0                # daemon mode: `serve --daemonize` returns once stub.ready exists
_RELAY_COUNTERS = ("attaches", "rotations", "frames_in", "frames_out", "chars_in", "noise_chars", "frame_errors",
                   "duplicate_requests", "cancelled", "pings", "flood_refusals")


def _default_channel(proc: subprocess.Popen) -> FrameChannel:
    return PipeChannel(proc, mimic_sdk=True)


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_worker_groups(stub_log: Path, *, not_before: float | None, home: str, term_wait_s: float) -> None:
    """Kill the worker's own process group (the stub starts it in a session of its own) when the stub is gone and
    left it behind, e.g. a stub that died by SIGKILL. Only a pid the stub logged as worker_started that still leads
    its own group and started no earlier than the stub itself (a reused pid fails this) is signalled."""
    if not_before is None:
        return
    try:
        lines = Path(stub_log).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    for line in lines:
        if '"worker_started"' not in line:
            continue
        try:
            row = json.loads(line)
            pid = row["pid"]
        except (ValueError, KeyError, TypeError):
            continue
        if row.get("event") != "worker_started" or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
            continue
        try:
            if os.getpgid(pid) != pid or pid == os.getpgrp():
                continue
        except OSError:
            continue
        started = _process_start(pid, home=home)
        if started is None or started < not_before - 1.0:
            continue
        bundle.kill_group(pid, term_wait_s=term_wait_s)


class _Stub:
    """The stub's process: a Popen child (stdio) or a daemon known by its pid and group (daemon)."""

    def __init__(self, *, proc: subprocess.Popen | None = None, pid: int | None = None, pgid: int | None = None,
                 proc_start: float | None = None):
        self.proc, self.pid = proc, (proc.pid if proc is not None else pid)
        self.pgid = proc.pid if proc is not None else pgid
        self.proc_start = proc_start

    def alive(self) -> bool:
        if self.proc is not None:
            return self.proc.poll() is None
        return _pid_alive(self.pid)

    def wait(self, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while self.alive():
            if time.monotonic() >= end:
                return False
            time.sleep(0.05)
        return True

    def kill(self, term_wait_s: float) -> None:
        """SIGTERM to the stub's group (its handler SIGKILLs the worker's group), then SIGKILL."""
        if self.pgid and self.pgid > 1:
            bundle.kill_group(self.pgid, term_wait_s=term_wait_s)
        if self.proc is not None:
            try:
                self.proc.wait(timeout=term_wait_s + 5)
            except subprocess.TimeoutExpired:
                pass


class LocalRelayExecutor(_LocalBase):
    name = "local_relay"
    isolated = False
    # How a stub or bridge process's pipes become a FrameChannel; tests swap in tests/fake_channel.FaultChannel.
    channel_factory: Callable[[subprocess.Popen], FrameChannel] = staticmethod(_default_channel)

    def __init__(self, manifest: dict, paths: StatePaths, *, gateway: HandlesRequests, mode: str = "stdio",
                 tokenizer_dir: Path | None = None, command_prefix: Sequence[str] = (),
                 clock: util.Clock | None = None, ids: util.IdSource | None = None) -> None:
        if mode not in ("stdio", "daemon"):
            raise ValueError("mode must be 'stdio' or 'daemon'")
        super().__init__(manifest, paths, gateway=gateway, tokenizer_dir=tokenizer_dir, command_prefix=command_prefix,
                         clock=clock, ids=ids)
        self.mode = mode
        self.rng = random.Random()
        r = lambda key: util.lookup(manifest, "relay." + key)   # noqa: E731
        self.max_stream_chars = float(r("max_stream_mb")) * MIB
        self.max_job_stream_chars = float(r("max_job_stream_mb")) * MIB
        self.rotate_chars = float(r("rotate_after_mb")) * MIB
        self.reattach_attempts = int(r("reattach_attempts"))
        lo, hi = r("reattach_backoff_s")
        self.reattach_backoff_s = (float(lo), float(hi))

    # ------------------------------------------------------------ run
    def run(self, spec: JobSpec, attempt_dir: Path, on_handle: Callable[[dict], None]) -> JobResult:
        job: JobIdentity = spec.identity
        attempt_dir = Path(attempt_dir)
        started_utc = util.utc_now(self.clock)
        box, env, _jobfile = self._prepare(spec, attempt_dir)
        report = self._selfcheck(box, env, attempt_dir)
        stub_env = procenv.passthrough_env(home=str(box / "home"), python_bin_dir=self.python_bin_dir)
        stub_env["MHF_STUB_BEARER"] = self.ids.token()       # only the stub's environment; never stored
        self.gateway.register_job(job)
        watch = _Watch(self.clock, self.gateway, job.job_id, self.sleep_gap_s)
        state = {"relay_end": None, "kill_reason": None, "timed_out": False, "stub": None, "bridges": [],
                 "sock_dir": None}
        session = None
        mono0 = self.clock.monotonic()
        seconds = 0.0
        log = open(box / "run" / "stub.txt", "ab")
        try:
            session = RelaySession(self.gateway, job, self.manifest, clock=self.clock)
            deadline = mono0 + float(spec.timeout_s) + DEADLINE_MARGIN_S
            if self.mode == "stdio":
                self._run_stdio(box, stub_env, log, session, watch, deadline, on_handle, state)
            else:
                self._run_daemon(box, stub_env, log, session, watch, deadline, on_handle, state)
            stub_env.pop("MHF_STUB_BEARER", None)
            watch.beat()
            seconds = max(0.0, self.clock.monotonic() - mono0)
        finally:
            stub_env.pop("MHF_STUB_BEARER", None)
            counters = session.counters() if session is not None else {}
            if session is not None:
                session.close()
            stub: _Stub | None = state["stub"]
            # A stub whose link ended ends its worker (exit.json reason uplink_lost) and leaves by itself.
            if stub is not None and not stub.wait(STUB_EXIT_WAIT_S):
                stub.kill(self.term_wait_s)
            if stub is not None:                              # a stub that died abnormally leaves its worker behind
                _kill_worker_groups(box / "run" / "stub.txt", not_before=stub.proc_start, home=str(self.paths.root),
                                    term_wait_s=self.term_wait_s)
            for proc in state["bridges"]:
                self._reap(proc)
            _remove_sock_dir(state["sock_dir"])
            log.close()
            gw = self.gateway.close_job(job.job_id)
        outputs, exit_file, stderr_tail = self._finish(attempt_dir, box)
        relay = {"mode": self.mode}
        relay.update({k: int(counters.get(k, 0) or 0) for k in _RELAY_COUNTERS})
        return JobResult(
            returncode=exit_file.get("returncode") if exit_file else None,
            timed_out=bool(state["timed_out"] or (exit_file or {}).get("timed_out") is True),
            exit_file=exit_file, relay_end=state["relay_end"], kill_reason=state["kill_reason"],
            started_utc=started_utc, ended_utc=util.utc_now(self.clock), outputs=outputs, gateway=gw,
            refusal_codes=merge_refusal_codes((gw or {}).get("refusal_codes"), exit_file, relay["flood_refusals"]),
            stderr_tail=stderr_tail, host_sleep_s=watch.host_sleep_s,
            facts={"executor": self.name, "isolated": False, "seconds": seconds, "setup_seconds": 0.0,
                   "cost_micro": 0, "environment": self._environment(report), "relay": relay, "selfcheck": "passed",
                   "pgid": state["stub"].pgid if state["stub"] is not None else None})

    # ------------------------------------------------------------ the two modes
    def _stub_argv(self, box: Path, *tail: str) -> list[str]:
        return [self.python, str(box / "bin" / "mhf_relay_stub.py"), *tail]

    def _run_stdio(self, box, stub_env, log, session, watch, deadline, on_handle, state) -> None:
        argv = [*self.command_prefix, *self._stub_argv(box, "serve", "--job", str(box / "job.json"), "--uplink",
                                                       "stdio")]
        try:
            proc = subprocess.Popen(argv, env=stub_env, cwd=str(box), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=log, bufsize=0, start_new_session=True)
        except OSError as exc:
            raise ExecutorError(f"relay stub could not start: {type(exc).__name__}", kind="exec") from None
        handle = self._handle(proc.pid, purpose="stub")
        state["stub"] = stub = _Stub(proc=proc, proc_start=handle["proc_start"])
        on_handle(handle)
        bridge = session.attach(self.channel_factory(proc))

        def poll():
            if not bridge.alive:
                return "eof" if bridge.dead_reason == "eof" else "error"
            return None

        def over() -> bool:          # the one exec's stream (the SDK keeps all of it) or the job's whole stream
            return bridge.chars_in > self.max_stream_chars or session.total_chars_in() > self.max_job_stream_chars

        self._wait(session, watch, deadline, stub, state, poll, over)

    def _run_daemon(self, box, stub_env, log, session, watch, deadline, on_handle, state) -> None:
        sock_dir = tempfile.mkdtemp(prefix="mhf-", dir="/tmp")    # state paths exceed the AF_UNIX limit [L1]
        state["sock_dir"] = sock_dir
        uplink = "unix:" + os.path.join(sock_dir, "u.sock")
        argv = [*self.command_prefix, *self._stub_argv(box, "serve", "--job", str(box / "job.json"), "--uplink",
                                                       uplink, "--daemonize")]
        try:
            done = subprocess.run(argv, env=stub_env, cwd=str(box), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                  timeout=SERVE_START_S)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExecutorError(f"relay stub could not start: {type(exc).__name__}", kind="exec") from None
        try:
            ready = util.read_json(box / "run" / "stub.ready")
            pid = int(ready["pid"])
            pgid = os.getpgid(pid)
        except (OSError, ValueError, KeyError, TypeError, MhforgeError):
            raise ExecutorError(f"relay stub did not become ready (exit {done.returncode})", kind="exec") from None
        try:
            own_session = os.getsid(pid) == os.getsid(0)
        except OSError:
            own_session = True
        if pid <= 1 or pgid <= 1 or pgid == os.getpgrp() or own_session:
            # the daemon runs in a session of its own (setsid); any other pid is not a group this executor may signal
            raise ExecutorError("relay stub's ready file names no process group of its own", kind="exec")
        handle = dict(self._handle(pid, purpose="stub"), pgid=pgid, sock_dir=sock_dir)
        state["stub"] = stub = _Stub(pid=pid, pgid=pgid, proc_start=handle["proc_start"])
        on_handle(handle)
        bridge_env = {k: v for k, v in stub_env.items() if k != "MHF_STUB_BEARER"}
        bridge_argv = self._stub_argv(box, "bridge", "--uplink", uplink)

        def start_bridge() -> Bridge:
            try:
                proc = subprocess.Popen(bridge_argv, env=bridge_env, cwd=str(box), stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=log, bufsize=0, start_new_session=True)
            except OSError as exc:
                raise ExecutorError(f"relay bridge could not start: {type(exc).__name__}", kind="exec") from None
            state["bridges"].append(proc)
            return session.attach(self.channel_factory(proc))

        cur = {"bridge": start_bridge(), "fails": 0}

        def poll():
            b = cur["bridge"]
            if b.hello is not None:
                cur["fails"] = 0                              # an attach that worked ends a run of failures
            if not b.alive:
                if cur["fails"] >= self.reattach_attempts:
                    return "error"                            # reattach exhausted: the worker has no uplink
                cur["fails"] += 1
                self.clock.sleep(self.rng.uniform(*self.reattach_backoff_s))
                cur["bridge"] = start_bridge()
                return None
            if b.chars_in > self.rotate_chars:
                cur["bridge"] = start_bridge()
                session.note_rotation()
                b.detach()                                    # closes the old bridge's stdin; it leaves by itself
            return None

        def over() -> bool:
            return session.total_chars_in() > self.max_job_stream_chars

        self._wait(session, watch, deadline, stub, state, poll, over)

    # ------------------------------------------------------------ the wait loop (3.16)
    def _wait(self, session: RelaySession, watch: _Watch, deadline: float, stub: _Stub, state: dict,
              poll: Callable[[], str | None], over: Callable[[], bool]) -> None:
        """Until EXIT, a dead bridge, the deadline, a stream budget, a kill request or the end of a halt's grace.
        A stream that passed its budget marks the attempt stream_budget even when EXIT won the race."""
        kill_sent_at: float | None = None
        kill_note = ""

        def on_exit() -> None:
            if state["relay_end"] is None:
                state["relay_end"] = "stream_budget" if over() else "exit"

        while True:
            if session.wait_exit(self.poll_s) is not None:
                on_exit()
                return
            watch.beat()
            if kill_sent_at is not None:                      # a KILL went out: the stub must end the worker
                if time.monotonic() - kill_sent_at >= KILL_WAIT_S or not stub.alive():
                    if session.wait_exit(0.5) is not None:
                        on_exit()
                        return
                    stub.kill(self.term_wait_s)
                    if state["relay_end"] is None:
                        state["relay_end"] = "error"
                    return
                if self.mode == "daemon":
                    # A queued KILL dies with its bridge (relay.py): keep reattaching, and resend it on a new one.
                    before = session.current
                    if poll() == "error":
                        stub.kill(self.term_wait_s)
                        if state["relay_end"] is None:
                            state["relay_end"] = "error"
                        return
                    if session.current is not before:
                        session.send_kill(kill_note)
                continue
            end = poll()
            if end in ("eof", "error"):
                if end == "error" and self.mode == "daemon":
                    stub.kill(self.term_wait_s)               # reattach exhausted: do not wait for the deadline
                state["relay_end"] = end
                return
            if over():
                state["relay_end"] = kill_note = "stream_budget"
                kill_sent_at = self._send_kill(session, stub, kill_note)
                if kill_sent_at is None:
                    return
                continue
            reason = watch.kill_due()
            if reason is not None:
                state["kill_reason"] = kill_note = reason
                kill_sent_at = self._send_kill(session, stub, reason)
                if kill_sent_at is None:
                    state["relay_end"] = "error"
                    return
                continue
            if self.clock.monotonic() >= deadline:
                state["relay_end"], state["timed_out"] = "deadline", True
                stub.kill(self.term_wait_s)
                return

    def _send_kill(self, session: RelaySession, stub: _Stub, reason: str) -> float | None:
        """A KILL frame to the stub; without a live bridge the stub's group is killed at once (returns None)."""
        if session.send_kill(reason):
            return time.monotonic()
        stub.kill(self.term_wait_s)
        return None

    @staticmethod
    def _reap(proc: subprocess.Popen) -> None:
        for stream in (proc.stdin, proc.stdout):
            try:
                if stream is not None:
                    stream.close()
            except (OSError, ValueError):
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            bundle.kill_group(proc.pid, term_wait_s=2.0)
            proc.wait()
