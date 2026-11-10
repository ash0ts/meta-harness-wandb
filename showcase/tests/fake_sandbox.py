# SPDX-License-Identifier: MIT
"""FakeSandboxClient (spec 5.1): a SandboxClient whose sandboxes are temp directories and whose execs are local
subprocesses, for tests/test_sandbox_executor.py. No live sandbox is ever created.

Each sandbox is a directory made with ``tempfile.mkdtemp(prefix="mhf-", dir="/tmp")``; the sandbox work directory
(``/mhf`` by default) is mapped onto it, so the daemon socket path stays far below the 90-byte limit. Paths are
rewritten in exec arguments, in uploaded job files (``mhforge.jobfile/1``) and back again in exec output. The python
name of the manifest (``python3``) runs ``python`` (the test interpreter by default). A tiny stand-in ``litellm``
package with the tokenizer vocabulary file sits on the container's PYTHONPATH (``fake_litellm``) so the selfcheck finds
a tokenizer directory, as it would in the worker image.

``exec_stream`` is a ``tests.fake_channel.FaultChannel`` over the real process, so stream drops and silent stalls are
the channel faults of the relay tests. The selfcheck runs for real but never probes egress: a ``--probe-egress`` run
is answered as a deny-egress sandbox would answer it (``blocked``), unless ``selfcheck_patch`` says otherwise. The
wheelhouse ``pip install`` exec is recorded and answered, not run. Every call is recorded in order in ``calls``.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from mhforge import bundle
from mhforge.errors import ExecutorError
from mhforge.executors.sandbox_client import ExecResult, SandboxSpec, map_sdk_error
from tests import helpers
from tests.fake_channel import FaultChannel

INSANDBOX = helpers.SC / "mhforge" / "insandbox"
FAKE_PKG = helpers.FIXTURES / "fake_pkg" / "text_classification"
MAX_EXEC_S = 120.0


# ---------------------------------------------------------------- SDK-shaped errors

def sdk_error(*, status: str | None = None, reason: str | None = None, cls: str = "SandboxError") -> Exception:
    """An exception shaped like a cwsandbox error: a class name, a gRPC status in `code`, a structured `reason`."""
    exc = type(cls, (Exception,), {})("synthetic sandbox error")
    exc.code, exc.reason = status, reason
    return exc


def mapped_create_error(*, status: str | None = None, reason: str | None = None, cls: str = "SandboxError"):
    """What the real adapter raises for that SDK error (the mapping of sandbox_client.map_sdk_error)."""
    return map_sdk_error(sdk_error(status=status, reason=reason, cls=cls), during="create")


class SandboxFailedError(Exception):
    """cwsandbox 1.27.0 SandboxFailedError (exceptions.py:111-134): the sandbox reached FAILED; an image pull failure
    carries a kubelet waiting reason in status_reason ("ErrImagePull: ..."). Same name and keyword-only signature."""

    def __init__(self, message: str, *, sandbox_id: str | None = None, status_reason: str | None = None,
                 reason: str | None = None, metadata=None, retry_delay=None) -> None:
        super().__init__(message)
        self.sandbox_id, self.status_reason = sandbox_id, status_reason
        self.reason, self.metadata, self.retry_delay = reason, dict(metadata) if metadata else {}, retry_delay


def raise_pull_failure_at_wait(sandbox_id: str, image: str) -> None:
    """What the real adapter's wait_running raises when Sandbox.wait() raises a pull failure: the mapped ExecutorError,
    raised inside the except block (`raise map_sdk_error(exc, during="create") from None`, as _CwHandle does)."""
    try:
        raise SandboxFailedError(f"Sandbox {sandbox_id} failed", sandbox_id=sandbox_id,
                                 status_reason=f'ErrImagePull: failed to pull image "{image}"')
    except Exception as exc:                       # noqa: BLE001
        raise map_sdk_error(exc, during="create") from None


# ---------------------------------------------------------------- the fake bundle

def build_fake_bundle(dest: Path) -> dict:
    """A job bundle in the layout of 3.16 built from tests/fixtures/fake_pkg and the real in-sandbox files. fake_pkg's
    data.py (a module, so that no directory named data exists in the repository) becomes data/__init__.py in the tar."""
    members: dict[str, bytes] = {}
    for rel in ("__init__.py", "inner_loop.py", "llm.py", "memory_system.py"):
        members["pkg/text_classification/" + rel] = (FAKE_PKG / rel).read_bytes()
    members["pkg/text_classification/data/__init__.py"] = (FAKE_PKG / "data.py").read_bytes()
    for path in sorted((FAKE_PKG / "agents").glob("*.py")):
        members["pkg/text_classification/agents/" + path.name] = path.read_bytes()
    for name in bundle.INSANDBOX_FILES:
        members["bin/" + name] = (INSANDBOX / name).read_bytes()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        for name in sorted(members):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(members[name]), 0o644, 0
            tf.addfile(info, io.BytesIO(members[name]))
    data = buf.getvalue()
    Path(dest).write_bytes(data)
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def build_wheelhouse(dest: Path, *, corrupt: bool = False) -> dict:
    """A small wheelhouse tar (wheels/*.whl, SHA256SUMS, requirements.lock.txt). corrupt: SHA256SUMS lists a wrong
    hash, so `sha256sum -c` fails in the sandbox."""
    wheels = {"wheels/fakepkg_a-1.0-py3-none-any.whl": b"wheel a\n", "wheels/fakepkg_b-2.0-py3-none-any.whl": b"wheel b\n"}
    members = dict(wheels, **{"requirements.lock.txt": b"fakepkg-a==1.0\nfakepkg-b==2.0\n"})
    sums = "".join("%s  %s\n" % ("0" * 64 if corrupt else hashlib.sha256(d).hexdigest(), n) for n, d in sorted(members.items()))
    members['SHA256SUMS'] = sums.encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        for name in sorted(members):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(members[name]), 0o644, 0
            tf.addfile(info, io.BytesIO(members[name]))
    data = buf.getvalue()
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    Path(dest).write_bytes(data)
    return {"tar_sha256": hashlib.sha256(data).hexdigest(), "tar_bytes": len(data),
            "sha256sums_sha256": hashlib.sha256(sums.encode()).hexdigest()}


# ---------------------------------------------------------------- call log

@dataclass
class Call:
    op: str
    sandbox_id: str | None
    args: dict = field(default_factory=dict)
    label: str = ""
    t: float = 0.0

    @property
    def name(self) -> str:
        return f"{self.op}:{self.label}" if self.label else self.op


def exec_label(argv: Sequence[str]) -> str:
    argv = [str(a) for a in argv]
    if argv[:2] == ["sh", "-c"]:
        return "wheelhouse_size" if len(argv) > 2 and "wc -c" in argv[2] else "sh"
    if len(argv) >= 3 and argv[1] == "-c":
        code = argv[2]
        if code == bundle.EXTRACT_SNIPPET:
            return "extract"
        if code == bundle.WHEELHOUSE_SNIPPET:
            return "wheelhouse_check"
        if code == bundle.PACK_SNIPPET:
            return "pack"
        if "stub.ready" in code and "killpg" in code:
            return "kill"
        return "python_c"
    if argv[1:4] == ["-m", "pip", "install"]:
        return "pip_install"
    if len(argv) >= 2 and argv[1].endswith("/mhf_selfcheck.py"):
        if "--import-check" in argv:
            return "import_check:" + argv[argv.index("--import-check") + 1]
        return "selfcheck"
    if len(argv) >= 3 and argv[1].endswith("/mhf_relay_stub.py"):
        return argv[2]                             # serve | bridge
    return "other"


# ---------------------------------------------------------------- streams

class FakeExecStream(FaultChannel):
    """An ExecStream: a FaultChannel over the process, plus result() and the exec's own timeout."""

    def __init__(self, proc: subprocess.Popen, *, timeout_s: float, **faults) -> None:
        super().__init__(proc, **faults)
        self.proc = proc
        self._timer = threading.Timer(max(0.1, float(timeout_s)), _killpg_quiet, args=(proc.pid, signal.SIGKILL))
        self._timer.daemon = True
        self._timer.start()

    def result(self, timeout_s: float) -> ExecResult:
        try:
            rc = self.proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return ExecResult(None, "", "", True)
        return ExecResult(rc, "", "", False)

    def kill(self) -> None:
        self._timer.cancel()
        _killpg_quiet(self.proc.pid, signal.SIGKILL)
        self.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def _killpg_quiet(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _merge(dst: dict, patch: dict) -> dict:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(dst.get(key), dict):
            _merge(dst[key], value)
        else:
            dst[key] = value
    return dst


# ---------------------------------------------------------------- sandboxes

class FakeSandbox:
    """A SandboxHandle: a directory standing in for the sandbox's work directory, processes started on the host."""

    def __init__(self, client: "FakeSandboxClient", sid: str, spec: SandboxSpec, image: str) -> None:
        self.client, self.id, self.spec, self.image = client, sid, spec, image
        self.work_dir = spec.working_dir
        self.root = tempfile.mkdtemp(prefix="mhf-", dir="/tmp")
        self._real = os.path.realpath(self.root)
        self.stopped = False
        self.procs: list[subprocess.Popen] = []
        self.streams: list[FakeExecStream] = []
        self.exec_envs: list[dict] = []
        self._lock = threading.Lock()
        fake = Path(self.root) / ".fake"
        (fake / "home").mkdir(parents=True)
        self.site = None
        if client.fake_litellm:
            tok = fake / "site" / "litellm" / "litellm_core_utils" / "tokenizers"
            tok.mkdir(parents=True)
            (fake / "site" / "litellm" / "__init__.py").write_text("# stand-in for the image's litellm\n")
            (tok / bundle.VOCAB_FILE).write_bytes(b"vocab\n")
            self.site = str(fake / "site")

    # -- path mapping
    def to_host(self, text: str) -> str:
        text = str(text)
        if text == self.work_dir:
            return self.root
        return text.replace(self.work_dir + "/", self.root + "/")

    def to_sandbox(self, text: str) -> str:
        for root in sorted({self._real, self.root}, key=len, reverse=True):
            text = re.sub(re.escape(root) + r"(?=/|\"|'|\s|$)", self.work_dir, text)
        return text

    def host_argv(self, argv: Sequence[str]) -> list[str]:
        out = [self.to_host(a) for a in argv]
        if out and out[0] == self.client.python_name:
            out[0] = self.client.python
        return out

    def _map_json(self, value):
        if isinstance(value, str):
            return self.to_host(value)
        if isinstance(value, list):
            return [self._map_json(v) for v in value]
        if isinstance(value, dict):
            return {k: self._map_json(v) for k, v in value.items()}
        return value

    def host_path(self, path: str) -> Path:
        if path != self.work_dir and not str(path).startswith(self.work_dir + "/"):
            raise ValueError(f"fake sandbox: path outside the work directory: {path}")
        return Path(self.to_host(path))

    def env(self) -> dict[str, str]:
        env = {"PATH": f"{os.path.dirname(self.client.python)}:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
               "HOME": str(Path(self.root) / ".fake" / "home"), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1"}
        if self.site:
            env["PYTHONPATH"] = self.site
        env.update(self.spec.env)
        env.update(self.client.container_env)
        return env

    def _check(self) -> None:
        if self.stopped:
            raise ExecutorError("fake sandbox is stopped", kind="exec")

    # -- protocol
    def wait_running(self, timeout_s: float) -> None:
        self.client._record("wait_running", self.id, timeout_s=timeout_s, image=self.image)
        if self.client.pull_fail_first or self.image in self.client.pull_fail_images:
            self.client.pull_fail_first = False
            raise_pull_failure_at_wait(self.id, self.image)
        if self.client.stuck_creates > 0:
            self.client.stuck_creates -= 1
            raise ExecutorError("fake sandbox stuck in creating", kind="create")

    def exec(self, argv: Sequence[str], *, cwd: str | None, timeout_s: float) -> ExecResult:
        label = exec_label(argv)
        self.client._record("exec", self.id, label=label, argv=list(argv), cwd=cwd, timeout_s=timeout_s, env=self.env())
        self._check()
        answer = self.client.answers.get(label)
        if answer is not None:
            return answer(self, list(argv)) if callable(answer) else answer
        if label == "pip_install":                 # recorded and answered, not run
            return ExecResult(self.client.pip_returncode, "", "" if self.client.pip_returncode == 0 else "pip failed\n",
                              False)
        host = self.host_argv(argv)
        probe = False
        if label == "selfcheck" and "--probe-egress" in host:
            probe = True
            host.remove("--probe-egress")        # never a real egress probe in a test
        if label.startswith("import_check:"):
            delay = self.client.import_delay_s.get(label.split(":", 1)[1])
            if delay:
                host = [host[0], "-c", "import os, sys, time; time.sleep(float(sys.argv[1])); "
                        "os.execv(sys.argv[2], sys.argv[2:])", str(delay)] + host
        cwd_host = self.to_host(cwd) if cwd else self.root
        env = self.env()
        self.exec_envs.append(env)
        proc = subprocess.Popen(host, cwd=cwd_host, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
        with self._lock:
            self.procs.append(proc)
        try:
            out, err = proc.communicate(timeout=min(float(timeout_s), self.client.max_exec_s))
        except subprocess.TimeoutExpired:
            _killpg_quiet(proc.pid, signal.SIGKILL)
            proc.communicate()
            return ExecResult(None, "", "", True)
        out_text = self.to_sandbox(out.decode("utf-8", "replace"))
        err_text = self.to_sandbox(err.decode("utf-8", "replace"))
        if label == "selfcheck":
            out_text = self._patch_report(out_text, probe)
        return ExecResult(proc.returncode, out_text, err_text, False)

    def _patch_report(self, text: str, probe: bool) -> str:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            return text
        try:
            report = json.loads(lines[-1])
        except ValueError:
            return text
        if probe:                                  # what a deny-egress sandbox answers
            report["egress"]["tcp"] = report["egress"]["dns"] = "blocked"
            for p in report["egress"]["probes"]:
                if p["result"] == "skipped" and p["target"]:
                    p["result"] = "blocked"
        _merge(report, json.loads(json.dumps(self.client.selfcheck_patch)))
        return json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n"

    def exec_stream(self, argv: Sequence[str], *, cwd: str | None, timeout_s: float) -> FakeExecStream:
        label = exec_label(argv)
        self.client._record("exec_stream", self.id, label=label, argv=list(argv), cwd=cwd, timeout_s=timeout_s,
                            env=self.env())
        self._check()
        fault = self.client.stream_faults.pop(0) if self.client.stream_faults else {}
        if fault == "fail":
            raise ExecutorError("fake exec stream refused", kind="exec")
        err = open(Path(self.root) / ".fake" / f"stream-{len(self.streams) + 1}.err", "wb")
        try:
            proc = subprocess.Popen(self.host_argv(argv), cwd=self.to_host(cwd) if cwd else self.root, env=self.env(),
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, bufsize=0,
                                    start_new_session=True)
        finally:
            err.close()
        stream = FakeExecStream(proc, timeout_s=min(float(timeout_s), self.client.max_exec_s), **dict(fault))
        with self._lock:
            self.procs.append(proc)
            self.streams.append(stream)
        return stream

    def write_file(self, path: str, data: bytes, *, timeout_s: float) -> None:
        data = bytes(data)
        self.client._record("write_file", self.id, label=path, path=path, bytes=len(data),
                            sha256=hashlib.sha256(data).hexdigest(), timeout_s=timeout_s,
                            data=data if len(data) <= 1 << 20 else None)
        self._check()
        if path in self.client.failing_uploads:
            raise ExecutorError("fake upload failed", kind="upload")
        if path in self.client.short_uploads:
            data = data[: len(data) // 2]
        if path.endswith(".json"):
            try:
                doc = json.loads(data.decode("utf-8"))
            except ValueError:
                doc = None
            if isinstance(doc, dict) and doc.get("schema") == "mhforge.jobfile/1":
                doc = self._map_json(doc)
                argv = doc.get("argv")
                if argv and argv[0] == self.client.python_name:
                    argv[0] = self.client.python
                data = json.dumps(doc, sort_keys=True).encode("utf-8")
        target = self.host_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def read_file(self, path: str, *, timeout_s: float, max_bytes: int) -> bytes:
        self.client._record("read_file", self.id, label=path, path=path, timeout_s=timeout_s, max_bytes=max_bytes)
        self._check()
        target = self.host_path(path)
        if path in self.client.missing_files:
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            raise FileNotFoundError(path)
        data = target.read_bytes()                 # FileNotFoundError when absent, as the adapter raises
        if len(data) > max_bytes:
            raise ExecutorError("fake sandbox file too large", kind="exec")
        return data

    def stop(self, *, timeout_s: float) -> None:
        self.client._record("stop", self.id, timeout_s=timeout_s)
        if self.client.failing_stops > 0:          # the sandbox is left as it was
            self.client.failing_stops -= 1
            raise ExecutorError("fake stop failed", kind="exec")
        self.terminate()

    # -- teardown
    def stub_pid(self) -> int | None:
        try:
            return int(json.loads((Path(self.root) / "run" / "stub.ready").read_text())["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def worker_pids(self) -> list[int]:
        pids = []
        try:
            for line in (Path(self.root) / "run" / "stub.txt").read_text(errors="replace").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("event") == "worker_started" and isinstance(row.get("pid"), int):
                    pids.append(row["pid"])
        except OSError:
            pass
        return pids

    def terminate(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        pid = self.stub_pid()
        if pid is not None and pid > 1 and _alive(pid):
            _killpg_quiet(pid, signal.SIGTERM)     # the stub's handler kills the worker's group
            end = time.monotonic() + 3.0
            while time.monotonic() < end and _alive(pid):
                time.sleep(0.05)
            _killpg_quiet(pid, signal.SIGKILL)
        for wpid in self.worker_pids():
            _killpg_quiet(wpid, signal.SIGKILL)
        with self._lock:
            streams, procs = list(self.streams), list(self.procs)
        for stream in streams:
            stream.kill()
        for proc in procs:
            _killpg_quiet(proc.pid, signal.SIGKILL)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        if not self.client.keep:
            shutil.rmtree(self.root, ignore_errors=True)


class FakeSandboxClient:
    """A SandboxClient over FakeSandbox. Faults are plain attributes a test sets before the run."""

    def __init__(self, *, python: str = sys.executable, python_name: str = "python3", fake_litellm: bool = True,
                 container_env: dict | None = None, keep: bool = False, max_exec_s: float = MAX_EXEC_S) -> None:
        self.python, self.python_name = str(python), python_name
        self.fake_litellm = fake_litellm
        self.container_env = dict(container_env or {})
        self.keep, self.max_exec_s = keep, max_exec_s
        self.calls: list[Call] = []
        self.sandboxes: dict[str, FakeSandbox] = {}
        self._n = 0
        self._lock = threading.Lock()
        # scripted faults
        self.create_errors: list[BaseException] = []      # raised by successive create() calls
        # cwsandbox 1.27.0: Sandbox.run returns on acceptance, so a pull failure surfaces from wait_running, never create
        self.pull_fail_first = False                       # the next sandbox's wait fails with a pull failure (once)
        self.pull_fail_images: set[str] = set()            # every sandbox of these images fails its wait so
        self.stuck_creates = 0                            # wait_running raises for the next N sandboxes
        self.failing_stops = 0                             # stop raises (and leaves the sandbox) for the next N stops
        self.failing_uploads: set[str] = set()           # sandbox paths whose write_file raises
        self.short_uploads: set[str] = set()               # sandbox paths whose write_file stores half the bytes
        self.missing_files: set[str] = set()               # sandbox paths that read_file reports absent
        self.pip_returncode = 0
        self.selfcheck_patch: dict = {}                    # deep-merged into every selfcheck report
        self.import_delay_s: dict[str, float] = {}         # candidate name -> delay before its import check starts
        self.stream_faults: list = []                      # per exec_stream: FaultChannel kwargs, or "fail"
        self.answers: dict[str, object] = {}               # exec label -> ExecResult or callable(handle, argv)
        self.list_script: list[list[str]] | None = None    # successive list_ids answers
        self.hooks: dict[str, Callable[[Call], None]] = {}

    def _record(self, op: str, sandbox_id: str | None, *, label: str = "", **args) -> Call:
        call = Call(op, sandbox_id, args, label, time.monotonic())
        with self._lock:
            self.calls.append(call)
        hook = self.hooks.get(op)
        if hook is not None:
            hook(call)
        return call

    def names(self, *, ops: Sequence[str] | None = None) -> list[str]:
        return [c.name for c in self.calls if ops is None or c.op in ops]

    def create(self, spec: SandboxSpec) -> FakeSandbox:
        self._record("create", None, spec=spec)
        if self.create_errors:
            raise self.create_errors.pop(0)
        image = spec.image
        with self._lock:
            self._n += 1
            sid = "sbx-fake-%04d" % self._n
        sandbox = FakeSandbox(self, sid, spec, image)
        self.sandboxes[sid] = sandbox
        return sandbox

    def list_ids(self, tags: Sequence[str]) -> list[str]:
        self._record("list_ids", None, tags=list(tags))
        if self.list_script is not None:
            return list(self.list_script.pop(0)) if self.list_script else []
        return [sid for sid, sb in self.sandboxes.items() if not sb.stopped and set(tags) <= set(sb.spec.tags)]

    def stop_id(self, sandbox_id: str) -> None:
        self._record("stop_id", sandbox_id)
        sandbox = self.sandboxes.get(sandbox_id)
        if sandbox is not None:
            sandbox.terminate()

    def close(self) -> None:
        for sandbox in list(self.sandboxes.values()):
            keep, self.keep = self.keep, False
            sandbox.terminate()
            shutil.rmtree(sandbox.root, ignore_errors=True)
            self.keep = keep
