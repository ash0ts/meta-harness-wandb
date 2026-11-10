# SPDX-License-Identifier: MIT
"""LocalExecutor (spec 3.17): the worker runs as a host subprocess and reaches the gateway over a loopback listener.

NOT ISOLATED. Development and offline tests only: the worker, its import checks and the candidate code they load run
on the host as the runner's user, so every record written here carries ``"isolated": false`` and live commands refuse
this executor unless the manifest says otherwise.

This module also holds the work shared with ``local_relay.py`` (``_LocalBase``): the work tree, the job file, the
selfcheck, the wait-loop watches (host-sleep heartbeat, halt grace, kill requests), the import check and the orphan
cleanup. No value of a key or token is ever written to a file, an argv or a record.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .. import bundle, procenv, util
from ..errors import ExecutorError, MhforgeError
from ..ingress_http import LoopbackIngress
from ..paths import StatePaths
from ..wire import HandlesRequests, JobIdentity
from . import STDERR_TAIL_CHARS, ImportResult, JobResult, JobSpec, check_selfcheck, merge_refusal_codes

__all__ = ["LocalExecutor", "HALT_GRACE_S", "SELFCHECK_TIMEOUT_S"]

HALT_GRACE_S = 60.0                 # after a halt the worker may end by itself for this long, then it is killed (3.16)
SELFCHECK_TIMEOUT_S = 60.0
EXIT_SCHEMA = "mhforge.exit/1"
MIB = 1024 * 1024
_INPUT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_PS_TIME_FORMAT = "%a %b %d %H:%M:%S %Y"


# ---------------------------------------------------------------- small helpers

def _tail(path: Path, chars: int = STDERR_TAIL_CHARS) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - chars * 4))
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    return text[-chars:]


def _read_exit(path: Path) -> dict | None:
    """The exit file as a dict, or None when it is absent or not an object."""
    try:
        doc = util.read_json(path)
    except (OSError, ValueError, MhforgeError):
        return None
    return doc if isinstance(doc, dict) else None


def _killpg_quiet(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _process_start(pid: object, *, home: str) -> float | None:
    """The OS start time of pid (epoch seconds, 1 s resolution) from `ps -o lstart=`; None when it is gone."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return None
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], env=procenv.minimal_env({}, home=home),
                             stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    text = " ".join(out.stdout.decode("ascii", errors="replace").split())
    if out.returncode != 0 or not text:
        return None
    try:
        return time.mktime(time.strptime(text, _PS_TIME_FORMAT))
    except (ValueError, OverflowError):
        return None


def _remove_sock_dir(path: object) -> None:
    """Remove a daemon-mode socket directory, but only one of ours (/tmp/mhf-*)."""
    if not isinstance(path, str):
        return
    p = Path(path)
    if p.parent in (Path("/tmp"), Path("/private/tmp")) and p.name.startswith("mhf-") and not p.is_symlink():
        shutil.rmtree(p, ignore_errors=True)


class _Watch:
    """The per-poll checks of the wait loop (3.16): host-sleep heartbeat, kill requests and the halt grace."""

    def __init__(self, clock: util.Clock, gateway: HandlesRequests, job_id: str, sleep_gap_s: float) -> None:
        self.clock, self.gateway, self.job_id = clock, gateway, job_id
        self.sleep_gap_s = float(sleep_gap_s)
        self.host_sleep_s = 0.0
        self.halt_since: float | None = None
        self._wall, self._mono = clock.now(), clock.monotonic()

    def beat(self) -> None:
        wall, mono = self.clock.now(), self.clock.monotonic()
        gap = util.sleep_gap(wall - self._wall, mono - self._mono)
        if gap > self.sleep_gap_s:
            self.host_sleep_s += gap
        self._wall, self._mono = wall, mono

    def kill_due(self) -> str | None:
        """'refusal_flood' when the gateway asks for a kill; 'halt' once a halt has stood for HALT_GRACE_S."""
        if self.gateway.kill_requested(self.job_id):
            return "refusal_flood"
        if self.gateway.halted() is not None:
            now = self.clock.monotonic()
            if self.halt_since is None:
                self.halt_since = now
            elif now - self.halt_since >= HALT_GRACE_S:
                return "halt"
        return None


# ---------------------------------------------------------------- shared base

class _LocalBase:
    name = "local"
    isolated = False
    poll_s = 1.0                    # the wait loop's check interval (tests lower it)
    term_wait_s = 10.0              # SIGTERM to SIGKILL wait of bundle.kill_group

    def __init__(self, manifest: dict, paths: StatePaths, *, gateway: HandlesRequests,
                 tokenizer_dir: Path | None = None, command_prefix: Sequence[str] = (),
                 clock: util.Clock | None = None, ids: util.IdSource | None = None) -> None:
        self.manifest, self.paths, self.gateway = manifest, paths, gateway
        self.clock = clock or util.SystemClock()
        self.ids = ids or util.IdSource(self.clock)
        self.command_prefix = tuple(str(a) for a in command_prefix)
        self._tokenizer_dir = None if tokenizer_dir is None else str(tokenizer_dir)
        sc_dir = Path(paths.root).parent.parent                     # 2.3 rule 2: the directory of the manifest
        # 2.3 path rule: absolute, never resolved (the venv's python is a symlink to an interpreter without litellm)
        self.python = os.path.abspath(os.path.join(str(sc_dir), str(util.lookup(manifest, "upstream.worker_python"))))
        self.python_bin_dir = os.path.dirname(self.python)          # == procenv.venv_root(python) / "bin" in a venv
        self.sleep_gap_s = float(util.lookup(manifest, "runner.sleep_gap_s", 60))

    # ------------------------------------------------------------ environment and work tree
    def _tokenizer(self) -> tuple[str, str | None]:
        """(TIKTOKEN_RS_CACHE_DIR, the job file's vocab_file)."""
        if self._tokenizer_dir is not None:
            return self._tokenizer_dir, None
        try:
            return str(procenv.find_tokenizer_dir(Path(self.python), bundle.VOCAB_FILE)), bundle.VOCAB_FILE
        except MhforgeError as exc:
            raise ExecutorError(f"worker interpreter has no tokenizer directory: {exc}", kind="image_mismatch") from None

    def _job_manifest(self) -> dict:
        """The manifest the job file is built from. sandbox.expect.machine and .python describe the sandbox image;
        on the host they would refuse every Mac, so the local job file leaves them null (the selfcheck skips a null
        expectation). The freeze hash expectation stays."""
        view = dict(self.manifest)
        view["sandbox"] = dict(self.manifest.get("sandbox") or {}, expect={"machine": None, "python": None})
        return view

    def _worker_env(self, box: Path, tok: str) -> dict[str, str]:
        return procenv.worker_env(path=f"{self.python_bin_dir}:/usr/bin:/bin", home=str(box / "home"),
                                  tmp=str(box / "tmp"), pythonpath=str(box / "pkg"), cache_dir=str(box / "cache"),
                                  tokenizer_dir=tok)

    @staticmethod
    def _fresh_box(bundle_tar: Path, box: Path) -> None:
        if os.path.lexists(box):
            shutil.rmtree(box)
        box.parent.mkdir(parents=True, exist_ok=True)
        try:
            bundle.extract_bundle(Path(bundle_tar), box)
        except (ValueError, OSError) as exc:
            raise ExecutorError(f"bundle could not be extracted: {exc}", kind="upload") from None

    def _prepare(self, spec: JobSpec, attempt_dir: Path) -> tuple[Path, dict[str, str], dict]:
        """Work tree, inputs, worker environment and job file; returns (box, job env, job file)."""
        attempt_dir = Path(attempt_dir)
        box = attempt_dir / "box"
        self._fresh_box(spec.bundle_tar, box)
        for name, source in spec.inputs:
            if not isinstance(name, str) or not _INPUT_NAME_RE.fullmatch(name):
                raise ExecutorError(f"input name is not a plain file name: {str(name)[:60]!r}", kind="upload")
            try:
                shutil.copyfile(source, box / "in" / name)
            except OSError as exc:
                raise ExecutorError(f"input {name} could not be copied: {type(exc).__name__}", kind="upload") from None
        tok, vocab = self._tokenizer()
        env = self._worker_env(box, tok)
        try:
            jobfile = bundle.build_job_file(spec.identity, python=self.python, box=str(box), inner_argv=spec.inner_argv,
                                            env=env, route=spec.route, timeout_s=spec.timeout_s,
                                            expect_outputs=spec.expect_outputs, manifest=self._job_manifest(),
                                            vocab_file=vocab, expect_freeze_sha256=spec.expect_freeze_sha256,
                                            capture=spec.capture)
        except ValueError as exc:
            raise ExecutorError(f"job file could not be built: {exc}", kind="upload") from None
        util.write_json(box / "job.json", jobfile)
        util.write_json(attempt_dir / "jobfile.json", dict(jobfile, executor=self.name, isolated=False))
        return box, env, jobfile

    def _selfcheck(self, box: Path, env: Mapping[str, str], attempt_dir: Path) -> dict:
        """mhf_selfcheck.py without egress probes into run/selfcheck.json; ExecutorError on an image problem."""
        argv = [self.python, str(box / "bin" / "mhf_selfcheck.py"), "--job", str(box / "job.json")]
        try:
            out = subprocess.run(argv, env=dict(env), cwd=str(box / "pkg"), stdin=subprocess.DEVNULL,
                                 capture_output=True, timeout=SELFCHECK_TIMEOUT_S, start_new_session=True)
        except subprocess.TimeoutExpired:
            raise ExecutorError("selfcheck did not finish in time", kind="selfcheck") from None
        except OSError as exc:
            raise ExecutorError(f"selfcheck could not start: {type(exc).__name__}", kind="selfcheck") from None
        report = None
        lines = [ln for ln in out.stdout.decode("utf-8", errors="replace").splitlines() if ln.strip()]
        if lines:
            try:
                report = json.loads(lines[-1])
            except ValueError:
                report = None
        if not isinstance(report, dict):
            raise ExecutorError("selfcheck printed no report", kind="selfcheck")
        util.write_json(box / "run" / "selfcheck.json", dict(report, isolated=False))
        _isolation, environment = check_selfcheck(report, isolated=False)
        if environment:
            bundle.collect_outputs(box, attempt_dir)
            raise ExecutorError("worker environment did not prove itself: " + "; ".join(environment)[:400],
                                kind="image_mismatch")
        return report

    @staticmethod
    def _environment(report: Mapping) -> dict:
        return {"env_mode": "local", "image_digest": None, "requirements_sha256": None, "sha256sums_sha256": None,
                "freeze_sha256": report.get("freeze_sha256"), "machine": report.get("machine"),
                "python": report.get("python")}

    def _handle(self, pid: int, **extra) -> dict:
        handle = {"pgid": pid, "pid": pid, "t_start": self.clock.now(),
                  "proc_start": _process_start(pid, home=str(self.paths.root)), "executor": self.name}
        handle.update(extra)
        return handle

    def _finish(self, attempt_dir: Path, box: Path) -> tuple[dict[str, str], dict | None, str]:
        """collect_outputs, then the collected exit file and the worker's stderr tail."""
        outputs = bundle.collect_outputs(box, attempt_dir)
        run = Path(attempt_dir) / "run"
        return outputs, _read_exit(run / "exit.json"), _tail(run / "stderr.txt")

    # ------------------------------------------------------------ Executor protocol: import check, cleanup, close
    def import_check(self, names: Sequence[str], bundle_tar: Path, work_root: Path, on_handle: Callable[[dict], None],
                     *, timeouts_s: Sequence[float]) -> dict[str, ImportResult]:
        """Upstream's import line and the loaded-class assertion, one host process per name, in order (3.16)."""
        names, limits = list(names), list(timeouts_s)
        if len(names) != len(limits):
            raise ValueError("import_check needs one timeout per name")
        work_root = Path(work_root)
        box = work_root / "box"
        self._fresh_box(bundle_tar, box)
        tok, vocab = self._tokenizer()
        env = self._worker_env(box, tok)
        try:
            check = bundle.build_check_file(python=self.python, box=str(box), env=env, manifest=self._job_manifest(),
                                            vocab_file=vocab)
        except ValueError as exc:
            raise ExecutorError(f"check file could not be built: {exc}", kind="upload") from None
        check_path = box / "check.json"
        util.write_json(check_path, check)
        util.write_json(work_root / "checkfile.json", dict(check, executor=self.name, isolated=False))
        results: dict[str, ImportResult] = {}
        for name, limit in zip(names, limits):
            argv = [self.python, str(box / "bin" / "mhf_selfcheck.py"), "--job", str(check_path), "--import-check",
                    str(name)]
            try:
                proc = subprocess.Popen(argv, env=dict(env), cwd=str(box / "pkg"), stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            except OSError as exc:
                raise ExecutorError(f"import check could not start: {type(exc).__name__}", kind="exec") from None
            timed_out = False
            try:
                on_handle(self._handle(proc.pid, purpose="import_check"))
                try:
                    out, err = proc.communicate(timeout=float(limit))
                except subprocess.TimeoutExpired:
                    timed_out = True
                    bundle.kill_group(proc.pid, term_wait_s=self.term_wait_s)
                    out, err = proc.communicate()
            finally:
                if proc.poll() is None:
                    bundle.kill_group(proc.pid, term_wait_s=self.term_wait_s)
                    proc.wait()
                _killpg_quiet(proc.pid, signal.SIGKILL)
            rc = None if timed_out else proc.returncode
            stdout = out.decode("utf-8", errors="replace")
            reason = {3: "foreign_class", 4: "no_class"}.get(rc) if rc is not None else None
            results[str(name)] = ImportResult(ok=(rc == 0 and "OK" in stdout), returncode=rc, timed_out=timed_out,
                                              stdout_tail=stdout[-STDERR_TAIL_CHARS:],
                                              stderr_tail=err.decode("utf-8", errors="replace")[-STDERR_TAIL_CHARS:],
                                              reason=reason)
        return results

    def cleanup_orphans(self, handles: Sequence[dict]) -> int:
        """Kill each recorded process group whose process start time still matches the handle; returns how many."""
        killed = 0
        home = str(self.paths.root)
        for handle in handles:
            if not isinstance(handle, Mapping):
                continue
            _remove_sock_dir(handle.get("sock_dir"))
            pgid = handle.get("pgid")
            if isinstance(pgid, bool) or not isinstance(pgid, int) or pgid <= 1:
                continue
            pid = handle.get("pid", pgid)
            current = _process_start(pid, home=home)
            if current is None:
                continue
            recorded, tolerance = handle.get("proc_start"), 1.0
            if not isinstance(recorded, (int, float)) or isinstance(recorded, bool):
                recorded, tolerance = handle.get("t_start"), 2.0
            if not isinstance(recorded, (int, float)) or isinstance(recorded, bool):
                continue
            if abs(current - float(recorded)) > tolerance:
                continue                                    # the pid was reused by another process: leave it alone
            bundle.kill_group(pgid, term_wait_s=self.term_wait_s)
            killed += 1
        return killed

    def close(self) -> None:
        return None


# ---------------------------------------------------------------- LocalExecutor

class LocalExecutor(_LocalBase):
    name = "local"
    isolated = False

    def __init__(self, manifest: dict, paths: StatePaths, *, gateway: HandlesRequests, ingress: LoopbackIngress,
                 tokenizer_dir: Path | None = None, command_prefix: Sequence[str] = (),
                 clock: util.Clock | None = None, ids: util.IdSource | None = None) -> None:
        super().__init__(manifest, paths, gateway=gateway, tokenizer_dir=tokenizer_dir, command_prefix=command_prefix,
                         clock=clock, ids=ids)
        self.ingress = ingress

    def run(self, spec: JobSpec, attempt_dir: Path, on_handle: Callable[[dict], None]) -> JobResult:
        job: JobIdentity = spec.identity
        attempt_dir = Path(attempt_dir)
        started_utc = util.utc_now(self.clock)
        box, env, jobfile = self._prepare(spec, attempt_dir)
        report = self._selfcheck(box, env, attempt_dir)
        token = self.ids.token() if spec.route == 2 else None
        self.gateway.register_job(job, token=token)
        proc = None
        timed_out, kill_reason, rc = False, None, None
        watch = _Watch(self.clock, self.gateway, job.job_id, self.sleep_gap_s)
        t_start = self.clock.now()
        mono0 = self.clock.monotonic()
        seconds = 0.0
        try:
            endpoint = self.ingress.open(job, token)
            popen_env = dict(env)
            popen_env.update(procenv.route_env(spec.route, base_url=endpoint.base_url, api_key=token or ""))
            argv = [endpoint.base_url if a == "{api_base}" else a for a in jobfile["argv"]]
            run_dir = box / "run"
            with open(run_dir / "stdout.txt", "wb") as out, open(run_dir / "stderr.txt", "wb") as err:
                try:
                    proc = subprocess.Popen([*self.command_prefix, *argv], env=popen_env, cwd=jobfile["cwd"],
                                            stdin=subprocess.DEVNULL, stdout=out, stderr=err, start_new_session=True)
                except OSError as exc:
                    raise ExecutorError(f"worker could not start: {type(exc).__name__}", kind="exec") from None
            del popen_env, token
            on_handle(self._handle(proc.pid, purpose="worker"))
            while True:
                try:
                    rc = proc.wait(timeout=self.poll_s)
                    break
                except subprocess.TimeoutExpired:
                    pass
                watch.beat()
                kill_reason = watch.kill_due()
                if kill_reason is not None:
                    break
                if self.clock.monotonic() - mono0 >= spec.timeout_s:
                    timed_out = True
                    break
            if proc.poll() is None:
                bundle.kill_group(proc.pid, term_wait_s=self.term_wait_s)
            rc = proc.wait()
            _killpg_quiet(proc.pid, signal.SIGKILL)         # whatever the worker left in its group
            watch.beat()
            seconds = max(0.0, self.clock.monotonic() - mono0)
            reason = "timeout" if timed_out else ("killed" if kill_reason is not None else "exit")
            util.write_json(run_dir / "exit.json", {
                "schema": EXIT_SCHEMA, "returncode": 124 if timed_out else int(rc), "reason": reason,
                "timed_out": timed_out, "t_start": t_start, "t_end": self.clock.now(), "isolated": False})
        finally:
            if proc is not None and proc.poll() is None:
                bundle.kill_group(proc.pid, term_wait_s=self.term_wait_s)
                proc.wait()
            try:
                self.ingress.close(job.job_id)
            finally:
                gw = self.gateway.close_job(job.job_id)
        outputs, exit_file, stderr_tail = self._finish(attempt_dir, box)
        return JobResult(
            returncode=(exit_file or {}).get("returncode") if exit_file else None,
            timed_out=timed_out, exit_file=exit_file, relay_end=None, kill_reason=kill_reason,
            started_utc=started_utc, ended_utc=util.utc_now(self.clock), outputs=outputs, gateway=gw,
            refusal_codes=merge_refusal_codes((gw or {}).get("refusal_codes"), exit_file, 0),
            stderr_tail=stderr_tail, host_sleep_s=watch.host_sleep_s,
            facts={"executor": self.name, "isolated": False, "pgid": proc.pid if proc is not None else None,
                   "seconds": seconds, "setup_seconds": 0.0, "cost_micro": 0,
                   "environment": self._environment(report), "selfcheck": "passed"})
