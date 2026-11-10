# SPDX-License-Identifier: MIT
"""SandboxExecutor (spec 3.17): the call sequence of one job attempt, one import check and the orphan cleanup, over
the SandboxClient protocol. The adapter behind the protocol (CwSandboxClient) is UNVERIFIED until the live preflight;
this sequence is tested against tests/fake_sandbox.py.

One sandbox per job attempt, created with egress and DNS denied, an empty WANDB_API_KEY and no mount, metered at its
whole lifetime from the moment it exists until it is stopped and closed with the measured time. No key, token or
bearer ever reaches the sandbox: the relay identifies the job by its channel.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import posixpath
import random
import re
import shlex
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from .. import bundle, procenv, util, wire
from ..errors import CampaignStop, ExecutorError, MeterCapExceeded
from ..relay import RelaySession
from ..wire import JobIdentity
from . import (STDERR_TAIL_CHARS, ImportResult, JobResult, JobSpec, check_selfcheck, merge_refusal_codes, positive_codes,
               sandbox_cost_micro)
from .sandbox_client import ExecResult, QuotaExceeded, SandboxSpec
from .sandbox_client import _is_pull_failure as _client_pull_failure

__all__ = ["SandboxExecutor", "sandbox_tags", "KILL_SNIPPET", "QUOTA_WAITS_S", "IMPORT_CHECK_LIFETIME_S"]

MIB = 1048576
QUOTA_WAITS_S = (5.0, 10.0, 20.0)
IMPORT_CHECK_LIFETIME_S = 300
WORKER_PATH = "/usr/local/bin:/usr/bin:/bin"
UPLOAD_TIMEOUT_S = 120.0
EXTRACT_TIMEOUT_S = 120.0
WHEEL_UPLOAD_TIMEOUT_S = 900.0
WHEEL_SIZE_TIMEOUT_S = 30.0
WHEEL_CHECK_TIMEOUT_S = 120.0
PIP_TIMEOUT_S = 600.0
SELFCHECK_TIMEOUT_S = 60.0
SERVE_TIMEOUT_S = 30.0
KILL_TIMEOUT_S = 30.0
PACK_TIMEOUT_S = 120.0
READ_TIMEOUT_S = 120.0
EXIT_READ_TIMEOUT_S = 30.0
STOP_TIMEOUT_S = 60.0
EXIT_MAX_BYTES = MIB
COLLECT_MAX_BYTES = 536_870_912
STDIO_EXTRA_S = 60.0
CLEANUP_ROUNDS = 10
CLEANUP_PAUSE_S = 2.0
_INPUT_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")

# Unary kill of the stub daemon (python -c source, standard library only): SIGTERM to the stub's process group (the
# stub's handler then SIGKILLs the worker's group, which lives in its own session), wait up to 5 s, then SIGKILL. The
# daemonized stub is not its group's leader (the double fork leaves it in the group of the intermediate child), so the
# group is looked up with getpgid rather than taken to be the pid.
KILL_SNIPPET = r'''
import json, os, signal, sys, time
try:
    with open(os.path.join(sys.argv[1], "run", "stub.ready"), "rb") as fh:
        pid = int(json.loads(fh.read().decode("utf-8"))["pid"])
except Exception:
    sys.exit(3)
if pid <= 1 or pid == os.getpid():
    sys.exit(3)
def _send(sig):
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        raise
    except OSError:
        pgid = None
    if pgid is not None and pgid > 1 and pgid != os.getpgid(0):
        try:
            os.killpg(pgid, sig)
            return
        except PermissionError:
            pass
    os.kill(pid, sig)
try:
    _send(signal.SIGTERM)
except ProcessLookupError:
    sys.exit(0)
for _ in range(50):
    time.sleep(0.1)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        sys.exit(0)
try:
    _send(signal.SIGKILL)
except ProcessLookupError:
    pass
'''


def sandbox_tags(job: JobIdentity) -> tuple[str, ...]:
    """Pure: mhf, mhf-c-<campaign>, mhf-j-<first 16 hex of sha256(job_key)>, mhf-a-<attempt>, mhf-p-<phase>."""
    digest = hashlib.sha256(job.job_key.encode("utf-8")).hexdigest()[:16]
    tags = ("mhf", f"mhf-c-{job.campaign}", f"mhf-j-{digest}", f"mhf-a-{job.attempt}", f"mhf-p-{job.phase}")
    if any(len(t) > 63 for t in tags):
        raise ValueError("a sandbox tag would exceed 63 characters")
    return tags


def _pull_failed(exc: BaseException) -> bool:
    """LD8: whether a wait_running failure is an image pull failure, by the client's own classification
    (sandbox_client._is_pull_failure). The adapter raises its mapped ExecutorError inside the except block that caught
    the SDK error (`raise ... from None`), so that SDK error is the mapped error's __context__."""
    return any(obj is not None and _client_pull_failure(obj) for obj in (exc, exc.__cause__, exc.__context__))


@dataclass
class _Sbx:
    handle: object
    id: str
    worst_ms: int
    opened: float                                  # monotonic, when the meter row was opened
    image: str = ""                                # the image this sandbox was created with (spec.image)
    running: float | None = None                   # monotonic, when wait_running returned
    metered: bool = False
    billable_ms: int = 0
    cost_micro: int = 0
    stop_error: str | None = None


@dataclass
class _Run:
    session: RelaySession | None = None
    registered: bool = False
    gateway: dict = field(default_factory=dict)
    relay_end: str | None = None
    kill_reason: str | None = None
    exit_meta: dict | None = None
    exit_file: dict | None = None
    outputs: dict = field(default_factory=dict)
    host_sleep_s: float = 0.0
    setup_seconds: float = 0.0
    environment: dict | None = None
    probe: bool = False
    t0: float = 0.0
    deadline: float = 0.0
    unary_kill: bool = False
    attach_errors: int = 0


class SandboxExecutor:
    name = "sandbox"
    isolated = True

    def __init__(self, manifest: dict, paths, *, client, gateway, ledger, clock: util.Clock | None = None) -> None:
        self.manifest, self.paths, self.client, self.gateway, self.ledger = manifest, paths, client, gateway, ledger
        self.clock = clock or util.SystemClock()
        get = lambda key: util.lookup(manifest, key)  # noqa: E731
        self.env_mode = get("sandbox.env_mode")
        if self.env_mode not in ("image", "wheelhouse"):
            raise ValueError("sandbox.env_mode must be image or wheelhouse")
        self.relay_mode = get("relay.mode")
        if self.relay_mode not in ("daemon", "stdio"):
            raise ValueError("relay.mode must be daemon or stdio")
        self.box = str(get("sandbox.work_dir"))
        self.python = str(get("sandbox.python"))
        self.cpu, self.memory = str(get("sandbox.cpu")), str(get("sandbox.memory"))
        self.campaign = get("campaign")
        self.entity, self.project = util.lookup(manifest, "decisions.entity", None), util.lookup(manifest, "decisions.project", None)
        self.poll_s = 1.0                          # the wait loop's beat (3.16: once a second)
        self.halt_grace_s = 60.0                   # a halted worker gets this long to end by itself
        self.kill_grace_s = 30.0                   # after a kill, how long to wait for EXIT before giving up
        self._rng = random.Random()
        self._lock = threading.Lock()
        self._budget_lock = threading.Lock()
        self._pending_micro = 0                    # worst cases checked against the cap whose meter row is not open yet
        self._probed = False
        self._live: dict[str, _Sbx] = {}
        self._wheelhouse: bytes | None = None
        self._import_checks = 0

    # ------------------------------------------------------------------ small helpers
    def _p(self, *parts: str) -> str:
        return posixpath.join(self.box, *parts)

    def lifetime_s(self, timeout_s: float) -> int:
        """timeout + sandbox.lifetime_margin_s, plus sandbox.setup_allowance_s in mode wheelhouse."""
        extra = int(util.lookup(self.manifest, "sandbox.setup_allowance_s")) if self.env_mode == "wheelhouse" else 0
        return int(timeout_s) + int(util.lookup(self.manifest, "sandbox.lifetime_margin_s")) + extra

    def _price(self, ms: int) -> int:
        return sandbox_cost_micro(int(ms), self.cpu, self.memory, self.manifest)

    def _check_budget(self, lifetime_s: int) -> int:
        """The cap check of step 1. Parallel dataset jobs share this executor, so the worst case of every sandbox
        that passed the check but has no meter row yet (it is being created) counts too; the caller releases the
        returned reservation once the row is open or the create failed."""
        price = self._price(lifetime_s * 1000)
        with self._budget_lock:
            try:
                self.ledger.check_meter_cap("sandbox_seconds", extra_micro=price + self._pending_micro)
            except MeterCapExceeded:
                detail = f"a sandbox of {lifetime_s} s does not fit budgets.sandbox_usd"
                self.ledger.set_halt("budget:sandbox", detail)
                raise CampaignStop(reason="budget:sandbox", detail=detail) from None
            self._pending_micro += price
        return price

    def _release_budget(self, price: int) -> None:
        with self._budget_lock:
            self._pending_micro -= price

    def _image(self) -> tuple[str, str | None]:
        sb = self.manifest["sandbox"]
        image = sb.get("image") if self.env_mode == "image" else sb.get("base_image")
        if not isinstance(image, str) or not image:
            raise ExecutorError(f"no image for sandbox mode {self.env_mode}", kind="create", retryable=False)
        fallback = sb.get("base_image_fallback") if self.env_mode == "wheelhouse" else None
        return image, fallback if isinstance(fallback, str) and fallback else None

    def _spec(self, lifetime_s: int, tags: tuple[str, ...]) -> SandboxSpec:
        if not isinstance(self.entity, str) or not isinstance(self.project, str):
            raise ExecutorError("decisions.entity and decisions.project must be set", kind="auth", retryable=False)
        image, fallback = self._image()
        return SandboxSpec(image=image, fallback_image=fallback, cpu=self.cpu, memory=self.memory,
                           max_lifetime_s=int(lifetime_s), tags=tuple(tags), env={"WANDB_API_KEY": "", "GPG_KEY": ""},
                           deny_egress=True, dns_egress="deny", working_dir=self.box, entity=self.entity,
                           project=self.project)

    def _exec(self, handle, argv: Sequence[str], timeout_s: float, *, cwd: str | None = None) -> ExecResult:
        try:
            return handle.exec(list(argv), cwd=cwd, timeout_s=float(timeout_s))
        except ExecutorError:
            raise
        except Exception as exc:                   # noqa: BLE001 - a protocol object should not raise anything else
            raise ExecutorError(f"sandbox exec failed ({type(exc).__name__})", kind="exec") from None

    def _upload(self, handle, rel: str, data: bytes, timeout_s: float = UPLOAD_TIMEOUT_S) -> None:
        try:
            handle.write_file(self._p(rel), bytes(data), timeout_s=float(timeout_s))
        except ExecutorError:
            raise
        except Exception as exc:                   # noqa: BLE001
            raise ExecutorError(f"sandbox upload failed ({type(exc).__name__})", kind="upload") from None

    # ------------------------------------------------------------------ create, meter, stop
    def _create_one(self, spec: SandboxSpec, lifetime_s: int, on_handle: Callable[[dict], None], detail: dict) -> _Sbx:
        for i in range(len(QUOTA_WAITS_S) + 1):
            try:
                handle = self.client.create(spec)
                break
            except QuotaExceeded:
                if i == len(QUOTA_WAITS_S):
                    raise ExecutorError("sandbox quota still exceeded after backoff", kind="quota", retryable=True) from None
                self.clock.sleep(QUOTA_WAITS_S[i])
            except ExecutorError:
                raise
            except Exception as exc:               # noqa: BLE001
                raise ExecutorError(f"sandbox create failed ({type(exc).__name__})", kind="create") from None
        sbx = _Sbx(handle=handle, id=str(handle.id), worst_ms=lifetime_s * 1000, opened=self.clock.monotonic(),
                   image=spec.image)
        with self._lock:
            self._live[sbx.id] = sbx
        try:
            on_handle({"sandbox_id": sbx.id})
            sbx.opened = self.clock.monotonic()
            self.ledger.meter_open("sandbox_seconds", sbx.id, sbx.worst_ms, "ms", worst_micro=self._price(sbx.worst_ms),
                                   detail=detail)
            sbx.metered = True
        except BaseException:
            self._dispose(sbx)
            raise
        return sbx

    def _create_running(self, lifetime_s: int, tags: tuple[str, ...], on_handle, detail: dict, created: list) -> _Sbx:
        """Steps 1 to 3: budget, create (quota backoff), on_handle, meter row, wait for running; one re-create.

        LD8: cwsandbox's create returns on acceptance and a pull failure surfaces from the wait. When the wait fails
        with the client's pull-failure classification and the spec has a fallback image, the one re-create uses the
        fallback image (and no further fallback). Every failed sandbox is stopped and its meter row closed (_dispose)
        before the next create; when that stop fails no second sandbox is created, and the row stays open at the
        worst case for cleanup_orphans, so the meter can only over-count."""
        spec = self._spec(lifetime_s, tags)
        for round_ in (1, 2):
            price = self._check_budget(lifetime_s)
            try:
                sbx = self._create_one(spec, lifetime_s, on_handle, detail)
            finally:
                self._release_budget(price)        # the open meter row (or no sandbox at all) now carries it
            created.append(sbx.id)
            try:
                sbx.handle.wait_running(float(util.lookup(self.manifest, "sandbox.create_wait_s")))
            except ExecutorError as exc:
                self._dispose(sbx)
                if not exc.retryable:
                    raise
                if round_ == 2:
                    raise ExecutorError("sandbox did not reach running twice", kind="create", retryable=True) from None
                spec = self._next_spec(spec, sbx, exc)
                continue
            except Exception as exc:               # noqa: BLE001
                self._dispose(sbx)
                if round_ == 2:
                    raise ExecutorError(f"sandbox did not reach running twice ({type(exc).__name__})",
                                        kind="create", retryable=True) from None
                spec = self._next_spec(spec, sbx, exc)
                continue
            except BaseException:
                self._dispose(sbx)
                raise
            sbx.running = self.clock.monotonic()
            return sbx
        raise AssertionError("unreachable")

    @staticmethod
    def _next_spec(spec: SandboxSpec, failed: _Sbx, exc: BaseException) -> SandboxSpec:
        """The spec of the re-create after a failed wait: refused while the failed sandbox may still run; the
        fallback image (once, with no further fallback) after a pull failure; else the same spec."""
        if failed.stop_error is not None:
            raise ExecutorError(f"the sandbox that did not reach running could not be stopped ({failed.stop_error}); "
                                "no second sandbox is created", kind="create", retryable=True) from None
        if spec.fallback_image and _pull_failed(exc):
            return dataclasses.replace(spec, image=spec.fallback_image, fallback_image=None)
        return spec

    def _dispose(self, sbx: _Sbx) -> None:
        """Stop the sandbox, then close its meter row with the time from running (or from opening) to stopped. A stop
        that fails leaves the row open at the worst case (cleanup_orphans closes it later)."""
        try:
            sbx.handle.stop(timeout_s=STOP_TIMEOUT_S)
        except Exception as exc:                   # noqa: BLE001 - never mask the attempt's own outcome
            sbx.stop_error = type(exc).__name__
            self.ledger.set_halt("cleanup_unverified", "sandbox stop failed; worst-case reservation retained")
        stopped = self.clock.monotonic()
        with self._lock:
            self._live.pop(sbx.id, None)
        if sbx.metered and sbx.stop_error is None:
            start = sbx.running if sbx.running is not None else sbx.opened
            sbx.billable_ms = max(0, min(sbx.worst_ms, int(round((stopped - start) * 1000))))
            sbx.cost_micro = self._price(sbx.billable_ms)
            self.ledger.meter_close("sandbox_seconds", sbx.id, sbx.billable_ms, micro_usd=sbx.cost_micro)
            sbx.metered = False

    # ------------------------------------------------------------------ steps 4 and 5
    def _job_env(self, tokenizer_dir: str) -> dict[str, str]:
        return procenv.worker_env(path=WORKER_PATH, home=self._p("home"), tmp=self._p("tmp"), pythonpath=self._p("pkg"),
                                  cache_dir=self._p("cache"), tokenizer_dir=tokenizer_dir)

    @staticmethod
    def _jobfile_bytes(doc: dict) -> bytes:
        return json.dumps(doc, sort_keys=True, ensure_ascii=False).encode("utf-8")

    def _extract(self, handle) -> None:
        r = self._exec(handle, [self.python, "-c", bundle.EXTRACT_SNIPPET, self.box], EXTRACT_TIMEOUT_S, cwd="/")
        if r.timed_out or r.returncode != 0:
            raise ExecutorError("the bundle could not be extracted in the sandbox", kind="exec")

    def _wheelhouse_bytes(self) -> bytes:
        if self._wheelhouse is None:
            wh = self.manifest["sandbox"]["wheelhouse"]
            sc_dir = Path(self.paths.root).parent.parent
            path = Path(wh["tar_path"]) if Path(wh["tar_path"]).is_absolute() else sc_dir / wh["tar_path"]
            try:
                data = path.read_bytes()
            except OSError:
                raise ExecutorError("the wheelhouse tar is not readable on the host", kind="env_setup") from None
            if len(data) != wh.get("tar_bytes") or util.sha256_bytes(data) != wh.get("tar_sha256"):
                raise ExecutorError("the wheelhouse tar does not match sandbox.wheelhouse", kind="env_setup")
            self._wheelhouse = data
        return self._wheelhouse

    def _wheelhouse_setup(self, handle) -> float:
        """Step 4 in mode wheelhouse: upload, size check, checksum check, install. Any failure is env_setup."""
        wh = self.manifest["sandbox"]["wheelhouse"]
        t0 = self.clock.monotonic()
        data = self._wheelhouse_bytes()
        try:
            self._upload(handle, "wheelhouse.tar", data, WHEEL_UPLOAD_TIMEOUT_S)
            r = self._exec(handle, ["sh", "-c", "wc -c < " + shlex.quote(self._p("wheelhouse.tar"))], WHEEL_SIZE_TIMEOUT_S)
            if r.timed_out or r.returncode != 0 or r.stdout.strip() != str(wh["tar_bytes"]):
                raise ExecutorError("the uploaded wheelhouse has the wrong size", kind="env_setup")
            r = self._exec(handle, [self.python, "-c", bundle.WHEELHOUSE_SNIPPET, self.box], WHEEL_CHECK_TIMEOUT_S, cwd="/")
            if r.timed_out or r.returncode != 0 or r.stdout.strip() != wh["sha256sums_sha256"]:
                raise ExecutorError("the wheelhouse checksums do not hold", kind="env_setup")
            r = self._exec(handle, [self.python, "-m", "pip", "install", "--no-index", "--find-links",
                                    self._p("wheelhouse", "wheels"), "--require-hashes", "--no-deps", "-r",
                                    self._p("wheelhouse", "requirements.lock.txt")], PIP_TIMEOUT_S, cwd="/")
            if r.timed_out or r.returncode != 0:
                raise ExecutorError("pip install from the wheelhouse failed", kind="env_setup")
        except ExecutorError as exc:
            if not exc.retryable or exc.kind == "env_setup":
                raise
            raise ExecutorError(f"wheelhouse setup failed ({exc.kind})", kind="env_setup") from None
        return max(0.0, self.clock.monotonic() - t0)

    def _take_probe(self) -> bool:
        if util.lookup(self.manifest, "sandbox.selfcheck") == "every_job":
            return True
        with self._lock:
            return not self._probed

    def _selfcheck(self, handle, report_path: Path, *, expect_freeze: str | None, probe: bool) -> dict:
        """Step 5: run the selfcheck, save it, stop the campaign on an isolation problem, refuse on an environment one."""
        fail_kind = "env_setup" if self.env_mode == "wheelhouse" else "selfcheck"
        argv = [self.python, self._p("bin", "mhf_selfcheck.py"), "--job", self._p("job.json")]
        r = self._exec(handle, argv + (["--probe-egress"] if probe else []), SELFCHECK_TIMEOUT_S)
        report = None
        lines = [ln for ln in (r.stdout or "").splitlines() if ln.strip()]
        if not r.timed_out and r.returncode == 0 and lines:
            try:
                report = json.loads(lines[-1])
            except ValueError:
                report = None
        if not isinstance(report, dict) or report.get("schema") != "mhforge.selfcheck/1":
            raise ExecutorError("the sandbox selfcheck gave no report", kind=fail_kind)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        util.write_json(report_path, report)
        isolation, environment = check_selfcheck(report, isolated=True)
        if isolation:
            detail = "; ".join(isolation)[:500]
            self.ledger.set_halt("isolation", detail)
            raise CampaignStop(reason="isolation", detail=detail)
        environment += [p for p in self._host_proof(report, expect_freeze)
                        if not any(e.split(":")[0] == p.split(":")[0] for e in environment)]
        if environment:
            message = "the sandbox did not prove its environment: " + "; ".join(environment)[:400]
            if self.env_mode == "image":
                raise ExecutorError(message, kind="image_mismatch", retryable=False)
            raise ExecutorError(message, kind="env_setup", retryable=True)
        if probe:
            with self._lock:
                self._probed = True
        return report

    def _host_proof(self, report: dict, expect_freeze: str | None) -> list[str]:
        """The proof re-checked on the host (the report's own expect_problems are computed inside the sandbox)."""
        out = []
        machine, python = report.get("machine"), report.get("python")
        want_machine = util.lookup(self.manifest, "sandbox.expect.machine")
        want_python = str(util.lookup(self.manifest, "sandbox.expect.python"))
        if machine != want_machine:
            out.append(f"machine: expected {want_machine}, got {machine}")
        prefix = want_python.split(".")
        if not isinstance(python, str) or python.split(".")[:len(prefix)] != prefix:
            out.append(f"python: expected {want_python}, got {python}")
        if expect_freeze is not None and report.get("freeze_sha256") != expect_freeze:
            out.append("freeze_sha256: differs from the qualified environment")
        return out

    def _identity(self, report: dict) -> dict:
        image, _ = self._image()
        m = _DIGEST_RE.search(image)
        wh = self.manifest["sandbox"]["wheelhouse"]
        return {"env_mode": self.env_mode, "image_digest": m.group(0) if m else None,
                "requirements_sha256": wh.get("requirements_sha256"),
                "sha256sums_sha256": wh.get("sha256sums_sha256") if self.env_mode == "wheelhouse" else None,
                "freeze_sha256": report.get("freeze_sha256"), "machine": report.get("machine"),
                "python": report.get("python")}

    def _tokenizer_dir(self, report: dict) -> str:
        override = util.lookup(self.manifest, "sandbox.tiktoken_cache_dir", None)
        if isinstance(override, str) and override:
            return override
        tok = report.get("tokenizer_dir")
        return tok if isinstance(tok, str) else ""

    def _prepare(self, handle, uploads: list[tuple[str, bytes]], report_path: Path, expect_freeze: str | None,
                 rebuild: Callable[[str], dict], run: _Run) -> dict:
        """Steps 4 and 5 for a job or an import check; returns the final job file (also written to the sandbox)."""
        for rel, data in uploads:
            self._upload(handle, rel, data)
        self._extract(handle)
        if self.env_mode == "wheelhouse":
            run.setup_seconds = self._wheelhouse_setup(handle)
        run.probe = self._take_probe()
        report = self._selfcheck(handle, report_path, expect_freeze=expect_freeze, probe=run.probe)
        run.environment = self._identity(report)
        doc = rebuild(self._tokenizer_dir(report))
        self._upload(handle, "job.json", self._jobfile_bytes(doc))
        return doc

    # ------------------------------------------------------------------ run
    def run(self, spec: JobSpec, attempt_dir: Path, on_handle: Callable[[dict], None]) -> JobResult:
        job = spec.identity
        attempt_dir = Path(attempt_dir)
        (attempt_dir / "run").mkdir(parents=True, exist_ok=True)
        lifetime = self.lifetime_s(spec.timeout_s)
        started_utc = util.utc_now(self.clock)
        created: list[str] = []
        sbx = self._create_running(lifetime, sandbox_tags(job), on_handle, {"purpose": "job", "job_id": job.job_id},
                                   created)
        run = _Run()
        try:
            try:
                self._run_in(sbx, spec, attempt_dir, run, lifetime)
            finally:
                try:
                    if run.session is not None:
                        run.session.close()
                finally:
                    if run.registered:
                        run.gateway = dict(self.gateway.close_job(job.job_id) or {})
        finally:
            self._dispose(sbx)
        counters = run.session.counters() if run.session is not None else {}
        bridges = [b.dead_reason for b in run.session.bridges] if run.session is not None else []
        exit_file = run.exit_file
        returncode = (exit_file or {}).get("returncode") if exit_file is not None else (run.exit_meta or {}).get("returncode")
        timed_out = bool(exit_file is not None and (exit_file.get("timed_out") is True or exit_file.get("reason") == "timeout"))
        facts = {"executor": self.name, "isolated": True, "sandbox_id": sbx.id, "sandbox_ids": created,
                 "image": sbx.image, "image_fallback": sbx.image != self._image()[0],      # LD8: the image that ran
                 "lifetime_s": lifetime, "tags": list(sandbox_tags(job)),
                 "seconds": sbx.billable_ms / 1000.0, "cost_micro": sbx.cost_micro, "setup_seconds": run.setup_seconds,
                 "environment": run.environment, "selfcheck": "passed", "probe_egress": run.probe,
                 "relay": dict(counters, mode=self.relay_mode, bridge_ends=bridges, attach_errors=run.attach_errors),
                 "exit_file": "present" if exit_file is not None else "absent", "unary_kill": run.unary_kill,
                 "stop_error": sbx.stop_error}
        return JobResult(returncode=returncode if isinstance(returncode, int) and not isinstance(returncode, bool) else None,
                         timed_out=timed_out, exit_file=exit_file, relay_end=run.relay_end, kill_reason=run.kill_reason,
                         started_utc=started_utc, ended_utc=util.utc_now(self.clock), outputs=dict(run.outputs),
                         gateway=run.gateway,
                         refusal_codes=merge_refusal_codes(run.gateway.get("refusal_codes"), exit_file,
                                                           int(counters.get("flood_refusals", 0) or 0)),
                         stderr_tail=_tail(attempt_dir / "run" / "stderr.txt"), host_sleep_s=float(run.host_sleep_s),
                         facts=facts)

    def _run_in(self, sbx: _Sbx, spec: JobSpec, attempt_dir: Path, run: _Run, lifetime: int) -> None:
        job, h = spec.identity, sbx.handle

        def jobfile(tokenizer_dir: str) -> dict:
            return bundle.build_job_file(job, python=self.python, box=self.box, inner_argv=spec.inner_argv,
                                         env=self._job_env(tokenizer_dir), route=spec.route, timeout_s=spec.timeout_s,
                                         expect_outputs=spec.expect_outputs, manifest=self.manifest,
                                         vocab_file=bundle.VOCAB_FILE, expect_freeze_sha256=spec.expect_freeze_sha256,
                                         capture=spec.capture)

        uploads = [("bundle.tar", Path(spec.bundle_tar).read_bytes()),
                   ("job.json", self._jobfile_bytes(jobfile(self._tokenizer_dir({}))))]
        for name, src in spec.inputs:
            if not isinstance(name, str) or not _INPUT_NAME_RE.fullmatch(name) or ".." in name:
                raise ValueError(f"input name is not a plain file name: {str(name)[:60]!r}")
            uploads.append(("in/" + name, Path(src).read_bytes()))
        doc = self._prepare(h, uploads, attempt_dir / "run" / "selfcheck.json", spec.expect_freeze_sha256, jobfile, run)
        util.write_json(attempt_dir / "jobfile.json", dict(doc, executor=self.name, isolated=True))
        # step 6: relay
        self.gateway.register_job(job)
        run.registered = True
        run.session = RelaySession(self.gateway, job, self.manifest, clock=self.clock)
        run.t0 = self.clock.monotonic()
        run.deadline = run.t0 + float(spec.timeout_s) + STDIO_EXTRA_S
        stub = self._p("bin", "mhf_relay_stub.py")
        if self.relay_mode == "daemon":
            r = self._exec(h, [self.python, stub, "serve", "--job", self._p("job.json"), "--uplink",
                               "unix:" + self._p("run", "u.sock"), "--daemonize"], SERVE_TIMEOUT_S)
            if r.timed_out or r.returncode != 0:
                raise ExecutorError("the relay stub did not start in the sandbox", kind="exec")
            self._attach_bridge(sbx, run, lifetime)
        else:
            stream = self._stream(h, [self.python, stub, "serve", "--job", self._p("job.json"), "--uplink", "stdio"],
                                  self._remaining(sbx, run, lifetime, float(spec.timeout_s) + STDIO_EXTRA_S))
            run.session.attach(stream)
        self._wait(sbx, spec, run, lifetime)
        # step 8: collect
        run.exit_file = self._read_exit(h, run.exit_meta)
        argv = [self.python, "-c", bundle.PACK_SNIPPET, self.box]
        if util.lookup(self.manifest, "sandbox.copy_back_cache", False):
            argv.append("cache")
        r = self._exec(h, argv, PACK_TIMEOUT_S, cwd="/")
        if r.timed_out or r.returncode != 0:
            raise ExecutorError("the outputs could not be packed in the sandbox", kind="exec")
        try:
            data = h.read_file(self._p("collect.tar"), timeout_s=READ_TIMEOUT_S, max_bytes=COLLECT_MAX_BYTES)
        except FileNotFoundError:
            raise ExecutorError("the packed outputs are missing", kind="exec") from None
        run.outputs = bundle.collect_outputs(data, attempt_dir)

    def _remaining(self, sbx: _Sbx, run: _Run, lifetime: int, want: float) -> float:
        """An exec timeout of at most `want`, kept 30 s below the sandbox's remaining lifetime."""
        left = lifetime - (self.clock.monotonic() - sbx.opened) - 30.0
        return max(1.0, min(float(want), left))

    def _stream(self, handle, argv: list[str], timeout_s: float):
        try:
            return handle.exec_stream(argv, cwd=None, timeout_s=float(timeout_s))
        except ExecutorError:
            raise
        except Exception as exc:                   # noqa: BLE001
            raise ExecutorError(f"sandbox exec stream failed ({type(exc).__name__})", kind="exec") from None

    def _attach_bridge(self, sbx: _Sbx, run: _Run, lifetime: int) -> None:
        argv = [self.python, self._p("bin", "mhf_relay_stub.py"), "bridge", "--uplink", "unix:" + self._p("run", "u.sock")]
        want = run.deadline - self.clock.monotonic() + STDIO_EXTRA_S
        run.session.attach(self._stream(sbx.handle, argv, self._remaining(sbx, run, lifetime, want)))

    def _backoff(self, n: int) -> float:
        lo, hi = (float(x) for x in util.lookup(self.manifest, "relay.reattach_backoff_s"))
        top = max(lo, min(hi, lo * (2 ** (n - 1)) if lo > 0 else hi))
        return self._rng.uniform(lo, top)

    def _exec_kill(self, handle, run: _Run) -> None:
        run.unary_kill = True
        try:
            self._exec(handle, [self.python, "-c", KILL_SNIPPET, self.box], KILL_TIMEOUT_S, cwd="/")
        except ExecutorError:
            pass

    def _kill(self, handle, run: _Run, reason: str) -> None:
        """A KILL frame when a bridge is alive, else (daemon mode) a unary exec of kill on the stub's group."""
        if run.session is not None and run.session.send_kill(reason):
            return
        if self.relay_mode == "daemon":
            self._exec_kill(handle, run)

    def _wait(self, sbx: _Sbx, spec: JobSpec, run: _Run, lifetime: int) -> None:
        """Step 7: the wait loop of 3.16 with the relay's exit conditions."""
        s, h, job = run.session, sbx.handle, spec.identity
        get = lambda key: util.lookup(self.manifest, key)  # noqa: E731
        rotate, max_stream = int(get("relay.rotate_after_mb")) * MIB, int(get("relay.max_stream_mb")) * MIB
        max_job, attempts = int(get("relay.max_job_stream_mb")) * MIB, int(get("relay.reattach_attempts"))
        sleep_gap_s = float(get("runner.sleep_gap_s"))
        fails, halt_seen, killed_at = 0, None, None
        last_wall, last_mono = self.clock.now(), self.clock.monotonic()
        while True:
            meta = s.wait_exit(self.poll_s)
            wall, mono = self.clock.now(), self.clock.monotonic()
            gap = util.sleep_gap(wall - last_wall, mono - last_mono)
            if gap > sleep_gap_s:
                run.host_sleep_s += gap
            last_wall, last_mono = wall, mono
            if meta is not None:
                run.exit_meta, run.relay_end = meta, "exit"
                return
            if run.kill_reason is None and self.gateway.kill_requested(job.job_id):
                run.kill_reason, killed_at = "refusal_flood", mono
                self._kill(h, run, "refusal_flood")
            if self.gateway.halted() is not None:
                if halt_seen is None:
                    halt_seen = mono
                if run.kill_reason is None and mono - halt_seen >= self.halt_grace_s:
                    run.kill_reason, killed_at = "halt", mono
                    self._kill(h, run, "halt")
            if s.total_chars_in() > max_job:
                run.relay_end = "stream_budget"
                self._kill(h, run, "stream_budget")
                return
            cur = s.current
            if self.relay_mode == "daemon":
                if cur is not None and cur.alive:
                    if cur.hello is not None:
                        fails = 0
                    if cur.chars_in > rotate:
                        try:
                            self._attach_bridge(sbx, run, lifetime)
                        except ExecutorError as exc:
                            if not exc.retryable:
                                raise
                            run.attach_errors += 1     # the old bridge still serves; try again after a jittered wait
                            self.clock.sleep(self._backoff(run.attach_errors))
                        else:
                            s.note_rotation()
                            cur.detach()           # closes the old bridge's stdin
                else:
                    if fails >= attempts:
                        run.relay_end = "error"    # reattach exhausted: no uplink, so do not wait for the deadline
                        self._exec_kill(h, run)
                        return
                    fails += 1
                    self.clock.sleep(self._backoff(fails))
                    try:
                        self._attach_bridge(sbx, run, lifetime)
                    except ExecutorError as exc:
                        if not exc.retryable:
                            raise
                        run.attach_errors += 1
            else:
                if cur is None or not cur.alive:
                    run.relay_end = "eof"
                    return
                if cur.chars_in > max_stream:
                    run.relay_end = "stream_budget"
                    self._kill(h, run, "stream_budget")
                    return
            if killed_at is not None and mono - killed_at >= self.kill_grace_s:
                run.relay_end = "deadline"
                if self.relay_mode == "daemon":
                    self._exec_kill(h, run)
                return
            if mono >= run.deadline:
                run.relay_end = "deadline"
                self._kill(h, run, "deadline")
                if self.relay_mode == "daemon" and not run.unary_kill:
                    self._exec_kill(h, run)
                return

    def _read_exit(self, handle, meta: dict | None) -> dict | None:
        """The exit file, or None when it is absent or unreadable. The EXIT frame's two counters can only make it worse."""
        try:
            data = handle.read_file(self._p("run", "exit.json"), timeout_s=EXIT_READ_TIMEOUT_S, max_bytes=EXIT_MAX_BYTES)
            doc = json.loads(bytes(data).decode("utf-8"))
        except (FileNotFoundError, ExecutorError, ValueError):
            return None
        if not isinstance(doc, dict) or doc.get("schema") != "mhforge.exit/1":
            return None
        if isinstance(meta, dict):
            local = dict(doc.get("local_refusals") or {}) if isinstance(doc.get("local_refusals"), dict) else {}
            for code, n in (meta.get("local_refusals") or {}).items():
                # the file's own count as classify_attempt reads it (a malformed value counts as 1, never raises)
                mine = positive_codes({code: local.get(code)}).get(code, 0)
                if isinstance(n, int) and not isinstance(n, bool) and n > mine:
                    local[code] = n
            doc["local_refusals"] = local
            mine, theirs = doc.get("unanswered_at_exit"), meta.get("unanswered_at_exit")
            if isinstance(theirs, int) and (not isinstance(mine, int) or theirs > mine):
                doc["unanswered_at_exit"] = theirs
        return doc

    # ------------------------------------------------------------------ import check
    def import_check(self, names: Sequence[str], bundle_tar: Path, work_root: Path, on_handle: Callable[[dict], None], *,
                     timeouts_s: Sequence[float]) -> dict[str, ImportResult]:
        names, timeouts = list(names), [float(t) for t in timeouts_s]
        default_t = float(util.lookup(self.manifest, "executor.import_check_timeout_s"))
        work_root = Path(work_root)
        work_root.mkdir(parents=True, exist_ok=True)
        lifetime = self.lifetime_s(IMPORT_CHECK_LIFETIME_S)
        with self._lock:
            self._import_checks += 1
            n = self._import_checks
        tags = ("mhf", f"mhf-c-{self.campaign}", "mhf-j-" + hashlib.sha256(b"import_check").hexdigest()[:16],
                f"mhf-a-{n}", "mhf-p-import-check")
        sbx = self._create_running(lifetime, tags, on_handle, {"purpose": "import_check"}, [])
        results: dict[str, ImportResult] = {}
        try:
            def checkfile(tokenizer_dir: str) -> dict:
                return bundle.build_check_file(python=self.python, box=self.box, env=self._job_env(tokenizer_dir),
                                               manifest=self.manifest, vocab_file=bundle.VOCAB_FILE)

            uploads = [("bundle.tar", Path(bundle_tar).read_bytes()),
                       ("job.json", self._jobfile_bytes(checkfile(self._tokenizer_dir({}))))]
            doc = self._prepare(sbx.handle, uploads, work_root / "selfcheck.json", None, checkfile, _Run())
            util.write_json(work_root / "checkfile.json", dict(doc, executor=self.name, isolated=True))
            for i, name in enumerate(names):
                t = timeouts[i] if i < len(timeouts) else (timeouts[-1] if timeouts else default_t)
                if not isinstance(name, str) or not wire.CANDIDATE_RE.fullmatch(name):
                    results[str(name)] = ImportResult(False, 2, False, "", "bad candidate name")
                    continue
                r = self._exec(sbx.handle, [self.python, self._p("bin", "mhf_selfcheck.py"), "--job", self._p("job.json"),
                                            "--import-check", name], t)
                reason = {3: "foreign_class", 4: "no_class"}.get(r.returncode) if not r.timed_out else None
                results[name] = ImportResult(ok=(not r.timed_out and r.returncode == 0 and "OK" in (r.stdout or "")),
                                             returncode=r.returncode, timed_out=bool(r.timed_out),
                                             stdout_tail=(r.stdout or "")[-STDERR_TAIL_CHARS:],
                                             stderr_tail=(r.stderr or "")[-STDERR_TAIL_CHARS:], reason=reason)
        finally:
            self._dispose(sbx)
        return results

    # ------------------------------------------------------------------ cleanup
    def cleanup_orphans(self, handles: Sequence[dict]) -> int:
        """stop_id for every recorded sandbox, then list by the campaign's tags and stop what is left until two
        consecutive empty lists. Exhausting 10 rounds halts without releasing reservations."""
        stopped = 0
        for h in handles or ():
            sid = h.get("sandbox_id") if isinstance(h, dict) else None
            if isinstance(sid, str) and sid:
                try:
                    self.client.stop_id(sid)
                    stopped += 1
                except ExecutorError:
                    pass
        empties = 0
        for round_ in range(CLEANUP_ROUNDS):
            ids = list(self.client.list_ids(["mhf", f"mhf-c-{self.campaign}"]) or [])
            if not ids:
                empties += 1
                if empties >= 2:
                    break
            else:
                empties = 0
                for sid in ids:
                    try:
                        self.client.stop_id(sid)
                        stopped += 1
                    except ExecutorError:
                        pass
            if round_ + 1 < CLEANUP_ROUNDS:
                self.clock.sleep(CLEANUP_PAUSE_S)
        if empties < 2:
            self.ledger.set_halt("cleanup_unverified", "campaign sandboxes still listed after cleanup")
            raise CampaignStop(reason="cleanup_unverified", detail="two consecutive empty sandbox lists required")
        now = self.clock.now()
        with self._lock:
            live = set(self._live)
        for row in self.ledger.open_meter_rows("sandbox_seconds"):
            if row["key"] in live:
                continue                           # a sandbox of this process that is still running
            try:
                elapsed_ms = int(round((now - util.parse_utc(row["utc"])) * 1000))
            except ValueError:
                elapsed_ms = int(row["quantity"])
            ms = max(0, min(int(row["quantity"]), elapsed_ms))
            self.ledger.meter_close("sandbox_seconds", row["key"], ms, micro_usd=self._price(ms))
        return stopped

    def close(self) -> None:
        with self._lock:
            live = list(self._live.values())
        for sbx in live:
            self._dispose(sbx)


def _tail(path: Path) -> str:
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    return data[-4 * STDERR_TAIL_CHARS:].decode("utf-8", "replace")[-STDERR_TAIL_CHARS:]
