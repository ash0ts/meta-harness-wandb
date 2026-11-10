# SPDX-License-Identifier: MIT
"""SandboxClient protocol and the CwSandboxClient adapter (spec 3.17). Names and signatures checked against the
cwsandbox 1.27.0 source (design/sdk-conformance-cwsandbox.md); behaviour UNVERIFIED until the live preflight (PF3, PF6).

This is the only module that imports ``cwsandbox``, and it does so inside methods: importing this module, or building a
client, imports no SDK. Entity and project are explicit on every SDK call that takes them, through the auth object. The
key is read once from the key mapping it is given, never from an SDK default, and never put in an argv or a sandbox
variable. SDK exceptions are mapped by class name and the structured ``reason`` field, never by message text.
"""

from __future__ import annotations

import codecs
import hashlib
import importlib
import os
import queue
import threading
from dataclasses import dataclass
from typing import Callable, MutableMapping, Protocol, Sequence

from .. import util
from ..errors import ChannelClosed, ExecutorError, MhforgeError

__all__ = [
    "SandboxSpec", "ExecResult", "ExecStream", "SandboxHandle", "SandboxClient", "QuotaExceeded", "CwSandboxClient",
    "map_sdk_error", "QUOTA_REASONS", "IMAGE_PULL_REASONS", "SDK_VERSION_HEADER",
]

SDK_VERSION_HEADER = "0.31.0"
# cwsandbox 1.27.0 reads CWSANDBOX_BASE_URL on every call and, in Sandbox.run, lets it win over any default; the key
# headers must reach only this host, so list/from_id get it explicitly and create refuses any other value in the env
SANDBOX_BASE_URL = "https://api.cwsandbox.com"
QUOTA_REASONS = frozenset({"CWSANDBOX_OCCUPANCY_QUOTA_EXCEEDED", "CWSANDBOX_SERVERLESS_CREATE_THROTTLED"})
# [inf] cwsandbox 1.27.0 defines no pull-failure reason; a pull failure is SandboxFailedError from wait() whose
# status_reason starts with a kubelet waiting reason. These reason names stay a guess until PF6.
IMAGE_PULL_REASONS = frozenset({"CWSANDBOX_IMAGE_PULL_FAILED", "CWSANDBOX_IMAGE_NOT_FOUND", "CWSANDBOX_IMAGE_PULL_BACKOFF"})
IMAGE_PULL_STATUS_REASONS = frozenset({"ErrImagePull", "ImagePullBackOff", "ErrImageNeverPull", "InvalidImageName"})
FILE_NOT_FOUND_REASONS = frozenset({"CWSANDBOX_FILE_NOT_FOUND"})
_TIMEOUT_CLASSES = frozenset({"SandboxCommandTimeoutError", "SandboxRequestTimeoutError", "SandboxTimeoutError",
                              "TimeoutError"})
STOP_RETRY_WAIT_S = 20.0


@dataclass(frozen=True)
class SandboxSpec:
    image: str
    fallback_image: str | None
    cpu: str
    memory: str
    max_lifetime_s: int
    tags: tuple[str, ...]
    env: dict[str, str]
    deny_egress: bool
    dns_egress: str
    working_dir: str
    entity: str
    project: str
    # no mount field exists: no mode mounts a scratch volume or a snapshot


@dataclass(frozen=True)
class ExecResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool


class ExecStream(Protocol):                        # a relay.FrameChannel plus a result
    def read_text(self) -> str | None: ...
    def write(self, data: bytes) -> None: ...      # ChannelClosed when the write does not return within relay.pong_timeout_s
    def close(self) -> None: ...
    def result(self, timeout_s: float) -> ExecResult: ...


class SandboxHandle(Protocol):
    id: str

    def wait_running(self, timeout_s: float) -> None: ...
    def exec(self, argv: Sequence[str], *, cwd: str | None, timeout_s: float) -> ExecResult: ...
    def exec_stream(self, argv: Sequence[str], *, cwd: str | None, timeout_s: float) -> ExecStream: ...
    def write_file(self, path: str, data: bytes, *, timeout_s: float) -> None: ...
    def read_file(self, path: str, *, timeout_s: float, max_bytes: int) -> bytes: ...   # FileNotFoundError when absent
    def stop(self, *, timeout_s: float) -> None: ...


class SandboxClient(Protocol):
    def create(self, spec: SandboxSpec) -> SandboxHandle: ...
    def list_ids(self, tags: Sequence[str]) -> list[str]: ...
    def stop_id(self, sandbox_id: str) -> None: ...


class QuotaExceeded(ExecutorError):                # kind = "quota"; retryable: occupancy or create throttling
    def __init__(self, message: str | None = None, **kwargs) -> None:
        kwargs.setdefault("kind", "quota")
        if kwargs["kind"] != "quota":
            raise ValueError("QuotaExceeded is always kind 'quota'")
        super().__init__(message, **kwargs)


# ---------------------------------------------------------------- error mapping (class name and reason, never message)

def _class_names(exc: BaseException) -> list[str]:
    return [c.__name__ for c in type(exc).__mro__]


def _status(exc: BaseException) -> str | None:
    """The gRPC status name an SDK exception carries: a `code`/`status` attribute (a str, an enum with .name, or a
    grpc-style code() method) on the exception or on its __cause__ (cwsandbox 1.27.0 sets no such attribute; it raises
    its own class `from` the grpc.RpcError, whose code() is the status), else the SDK class that stands for one status.
    The cause matters for PERMISSION_DENIED: the SDK raises CWSandboxAuthenticationError for it and for UNAUTHENTICATED."""
    for obj in (exc, exc.__cause__):
        if obj is None:
            continue
        for attr in ("code", "status"):
            value = getattr(obj, attr, None)
            if callable(value):
                try:
                    value = value()
                except Exception:                  # noqa: BLE001 - a foreign object; ignore it
                    value = None
            name = getattr(value, "name", value)
            if isinstance(name, str) and name.isupper():
                return name
    names = _class_names(exc)
    if "SandboxResourceExhaustedError" in names:
        return "RESOURCE_EXHAUSTED"
    if "SandboxUnavailableError" in names:
        return "UNAVAILABLE"
    if any("PermissionDenied" in n for n in names):
        return "PERMISSION_DENIED"
    if "CWSandboxAuthenticationError" in names or any("Unauthenticated" in n for n in names):
        return "UNAUTHENTICATED"
    return None


def _reason(exc: BaseException) -> str | None:
    reason = getattr(exc, "reason", None)
    return reason if isinstance(reason, str) and reason else None


def _is_pull_failure(exc: BaseException) -> bool:
    status_reason = getattr(exc, "status_reason", None)       # SandboxFailedError: "ErrImagePull: ..."
    if isinstance(status_reason, str) and status_reason.split(":", 1)[0].strip() in IMAGE_PULL_STATUS_REASONS:
        return True
    return _reason(exc) in IMAGE_PULL_REASONS or any("ImagePull" in n for n in _class_names(exc))


def _is_timeout(exc: BaseException) -> bool:
    return bool(_TIMEOUT_CLASSES & set(_class_names(exc)))


def map_sdk_error(exc: BaseException, *, during: str) -> ExecutorError:
    """The ExecutorError for an SDK exception (3.17 table). during: "create", "upload" or "exec"."""
    if isinstance(exc, ExecutorError):
        return exc
    reason, status, cls = _reason(exc), _status(exc), type(exc).__name__
    detail = f"{during}: {cls}" + (f" reason={reason}" if reason else "") + (f" status={status}" if status else "")
    if reason in QUOTA_REASONS:
        return QuotaExceeded(f"sandbox quota or create throttle ({detail})")
    if status == "RESOURCE_EXHAUSTED" and reason is None:
        return ExecutorError(f"billing stop ({detail})", kind="billing", retryable=False)
    if status == "PERMISSION_DENIED":
        return ExecutorError(f"permission denied ({detail})", kind="permission", retryable=False)
    if status == "UNAUTHENTICATED":
        return ExecutorError(f"authentication failed ({detail})", kind="auth", retryable=False)
    kind = during if during in ("create", "upload") else "exec"
    return ExecutorError(f"sandbox call failed ({detail})", kind=kind, retryable=True)


def _sandbox_id(obj: object) -> str:
    if isinstance(obj, str):
        return obj
    for attr in ("sandbox_id", "id"):
        value = getattr(obj, attr, None)
        if isinstance(value, str) and value:
            return value
    raise ExecutorError("sandbox has no id", kind="create")


def _text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", "replace")
    return str(value)


def _exec_result(res: object) -> ExecResult:
    rc = getattr(res, "returncode", None)
    return ExecResult(returncode=rc if isinstance(rc, int) and not isinstance(rc, bool) else None,
                      stdout=_text(getattr(res, "stdout", "")), stderr=_text(getattr(res, "stderr", "")),
                      timed_out=False)


# ---------------------------------------------------------------- stream and handle

class _CwStream:
    """process.stdout iterated synchronously in a reader thread; stdin writes bounded by relay.pong_timeout_s."""

    def __init__(self, process: object, *, write_timeout_s: float) -> None:
        self._process = process
        self._write_timeout_s = float(write_timeout_s)
        self._q: queue.Queue = queue.Queue()
        self._closed = threading.Event()
        self._broken = False
        threading.Thread(target=self._reader, name="mhf-cw-stdout", daemon=True).start()

    def _reader(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        try:
            for item in getattr(self._process, "stdout"):
                if self._closed.is_set():
                    break
                text = decoder.decode(bytes(item)) if isinstance(item, (bytes, bytearray)) else str(item)
                if text:
                    self._q.put(text)
        except Exception:                          # noqa: BLE001 - any stream fault is an end of stream
            pass
        finally:
            self._q.put(None)

    def read_text(self) -> str | None:
        if self._closed.is_set():
            return None
        item = self._q.get()
        if item is None:
            self._q.put(None)                      # every later read also sees the end
        return item

    def write(self, data: bytes) -> None:
        if self._closed.is_set() or self._broken:
            raise ChannelClosed("exec stream is closed")
        try:
            op = self._process.stdin.write(bytes(data))
            if op is not None and callable(getattr(op, "result", None)):
                op.result(timeout=self._write_timeout_s)
        except Exception as exc:                   # noqa: BLE001 - a timeout or a dead stream
            self._broken = True
            raise ChannelClosed(f"exec stream write failed ({type(exc).__name__})") from None

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._q.put(None)
        try:
            op = self._process.stdin.close()
            if op is not None and callable(getattr(op, "result", None)):
                op.result(timeout=self._write_timeout_s)
        except Exception:                          # noqa: BLE001 - closing a dead stream is not an error
            pass

    def result(self, timeout_s: float) -> ExecResult:
        try:
            return _exec_result(self._process.result(timeout=timeout_s))
        except Exception as exc:                   # noqa: BLE001
            if _is_timeout(exc):
                return ExecResult(None, "", "", True)
            raise map_sdk_error(exc, during="exec") from None


class _CwHandle:
    def __init__(self, sandbox: object, client: "CwSandboxClient") -> None:
        self._sb = sandbox
        self._client = client
        self.id = _sandbox_id(sandbox)

    def wait_running(self, timeout_s: float) -> None:
        try:
            self._sb.wait(timeout=timeout_s)
        except Exception as exc:                   # noqa: BLE001
            raise map_sdk_error(exc, during="create") from None

    def exec(self, argv: Sequence[str], *, cwd: str | None, timeout_s: float) -> ExecResult:
        argv = self._client._checked_argv(argv)
        try:
            return _exec_result(self._sb.exec(argv, cwd=cwd, timeout_seconds=timeout_s).result(timeout=timeout_s + 30))
        except Exception as exc:                   # noqa: BLE001
            if _is_timeout(exc):
                return ExecResult(None, "", "", True)
            raise map_sdk_error(exc, during="exec") from None

    def exec_stream(self, argv: Sequence[str], *, cwd: str | None, timeout_s: float) -> _CwStream:
        argv = self._client._checked_argv(argv)
        try:
            process = self._sb.exec(argv, cwd=cwd, timeout_seconds=timeout_s, stdin=True)
        except Exception as exc:                   # noqa: BLE001
            raise map_sdk_error(exc, during="exec") from None
        return _CwStream(process, write_timeout_s=self._client.write_timeout_s)

    def write_file(self, path: str, data: bytes, *, timeout_s: float) -> None:
        try:
            self._sb.write_file(path, bytes(data), timeout_seconds=timeout_s).result(timeout=timeout_s + 30)
        except Exception as exc:                   # noqa: BLE001
            raise map_sdk_error(exc, during="upload") from None

    def read_file(self, path: str, *, timeout_s: float, max_bytes: int) -> bytes:
        try:
            data = self._sb.read_file(path, timeout_seconds=timeout_s).result(timeout=timeout_s + 30)
        except Exception as exc:                   # noqa: BLE001
            if _reason(exc) in FILE_NOT_FOUND_REASONS or any(n in ("FileNotFoundError", "SandboxFileNotFoundError")
                                                             for n in _class_names(exc)):
                raise FileNotFoundError(path) from None
            raise map_sdk_error(exc, during="exec") from None
        data = data.encode("utf-8") if isinstance(data, str) else bytes(data or b"")
        if len(data) > max_bytes:
            raise ExecutorError(f"sandbox file larger than {max_bytes} bytes", kind="exec")
        return data

    def stop(self, *, timeout_s: float) -> None:
        self._client._stop(self._sb, timeout_s)


# ---------------------------------------------------------------- the adapter

class CwSandboxClient:
    """The cwsandbox adapter. Auth mode `headers` (default): explicit AuthHeaders with the four headers WandbAuth
    would build for a SaaS W&B host; cwsandbox 1.27.0 returns them verbatim (_auth.resolve_auth), imports no wandb and
    reads no netrc or credential env. The only environment it reads is CWSANDBOX_BASE_URL (refused unless it is the
    sandbox API) and CWSANDBOX_DISABLE_SIGNAL_HANDLERS. Mode `wandb_auth`: WANDB_API_KEY is set in this process from
    the given key and every call gets WandbAuth(entity, project); wandb's own chain then decides (an in-process session
    credential wins over the variable, and the host comes from wandb settings), so that mode is not key-pinned."""

    def __init__(self, manifest: dict, *, environ: MutableMapping[str, str],
                 scrub: Callable[[str], bool] | None = None, clock: util.Clock | None = None) -> None:
        name = util.lookup(manifest, "sandbox.auth_env")
        if not isinstance(name, str) or not name:
            raise ExecutorError("sandbox.auth_env is not a variable name", kind="auth", retryable=False)
        value = environ.get(name)
        scrubbed: bool | None = None
        try:
            if scrub is not None:
                scrubbed = bool(scrub(name))
        finally:
            environ.pop(name, None)                # in every case: no later child can inherit it
        if not isinstance(value, str) or not value:
            raise ExecutorError(f"{name} is missing or empty", kind="auth", retryable=False)
        self.auth_mode = util.lookup(manifest, "sandbox.auth_mode")
        if self.auth_mode not in ("headers", "wandb_auth"):
            raise ExecutorError("sandbox.auth_mode must be headers or wandb_auth", kind="auth", retryable=False)
        self.entity = util.lookup(manifest, "decisions.entity", None)
        self.project = util.lookup(manifest, "decisions.project", None)
        if not isinstance(self.entity, str) or not self.entity or not isinstance(self.project, str) or not self.project:
            raise ExecutorError("decisions.entity and decisions.project must be set for the sandbox API", kind="auth",
                                retryable=False)
        self.__key = value
        self.key_facts = {"name": name, "len": len(value), "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
                          "scrubbed": scrubbed}
        if self.auth_mode == "wandb_auth":         # WandbAuth takes no key; set after start, never inherited
            os.environ["WANDB_API_KEY"] = value
        value = None
        self.write_timeout_s = float(util.lookup(manifest, "relay.pong_timeout_s"))
        self._clock = clock or util.SystemClock()
        self._cw = None

    def __repr__(self) -> str:
        return f"CwSandboxClient(auth_mode={self.auth_mode!r}, entity={self.entity!r}, project={self.project!r})"

    def __reduce__(self):
        raise TypeError("CwSandboxClient holds a key and cannot be pickled")

    # --- SDK access
    def _sdk(self):
        env_url = os.environ.get("CWSANDBOX_BASE_URL")
        if env_url and env_url.rstrip("/") != SANDBOX_BASE_URL:   # Sandbox.run takes no base_url; the env would win
            raise ExecutorError("CWSANDBOX_BASE_URL points away from the sandbox API; refusing to send the key",
                                kind="auth", retryable=False)
        if self._cw is None:
            os.environ["CWSANDBOX_DISABLE_SIGNAL_HANDLERS"] = "1"   # the runner owns signals; before the first import
            try:
                self._cw = importlib.import_module("cwsandbox")
            except ImportError:
                raise ExecutorError("the cwsandbox SDK is not importable (install the live extra)", kind="create",
                                    retryable=False) from None
        return self._cw

    def _auth(self, entity: str | None = None, project: str | None = None):
        cw = self._sdk()
        entity, project = entity or self.entity, project or self.project
        if self.auth_mode == "headers":
            return cw.AuthHeaders(headers={"x-wandb-api-key": self.__key, "x-wandb-sdk-version": SDK_VERSION_HEADER,
                                           "x-entity-id": entity, "x-project-name": project},
                                  strategy="wandb_api_key")
        return cw.WandbAuth(entity=entity, project=project)

    def _checked_argv(self, argv: Sequence[str]) -> list[str]:
        argv = [str(a) for a in argv]
        if any(self.__key in a for a in argv):
            raise ValueError("refused: an exec argument would carry the sandbox key")
        return argv

    # --- protocol
    def create(self, spec: SandboxSpec) -> _CwHandle:
        if any(self.__key in str(v) for v in spec.env.values()):
            raise ValueError("refused: a sandbox variable would carry the sandbox key")
        if spec.deny_egress is not True or spec.dns_egress != "deny":
            raise ValueError("refused: every sandbox is created with deny_egress=True and dns_egress='deny'")
        if spec.entity != self.entity or spec.project != self.project:   # cleanup lists only the manifest's pair
            raise ValueError("refused: the sandbox entity and project must be the manifest's")
        cw = self._sdk()

        def run(image: str):
            return cw.Sandbox.run(
                container_image=image, auth=self._auth(spec.entity, spec.project),
                max_lifetime_seconds=spec.max_lifetime_s, tags=list(spec.tags),
                resources={"cpu": spec.cpu, "memory": spec.memory},
                network=cw.NetworkOptions(deny_egress=True, dns_egress="deny"),
                working_dir=spec.working_dir, environment_variables=dict(spec.env))
        try:
            sandbox = run(spec.image)
        except Exception as exc:                   # noqa: BLE001
            if not (spec.fallback_image and _is_pull_failure(exc)):
                raise map_sdk_error(exc, during="create") from None
            try:
                sandbox = run(spec.fallback_image)  # the same digest on another registry host, once
            except Exception as exc2:              # noqa: BLE001
                raise map_sdk_error(exc2, during="create") from None
        return _CwHandle(sandbox, self)

    def list_ids(self, tags: Sequence[str]) -> list[str]:
        cw = self._sdk()
        try:
            found = cw.Sandbox.list(tags=list(tags), base_url=SANDBOX_BASE_URL, auth=self._auth()).result()
        except Exception as exc:                   # noqa: BLE001
            raise map_sdk_error(exc, during="exec") from None
        return [_sandbox_id(item) for item in (found or [])]

    def stop_id(self, sandbox_id: str) -> None:
        cw = self._sdk()
        try:
            sandbox = cw.Sandbox.from_id(sandbox_id, base_url=SANDBOX_BASE_URL, auth=self._auth()).result()
        except Exception as exc:                   # noqa: BLE001
            if any("NotFound" in n for n in _class_names(exc)):
                return
            raise map_sdk_error(exc, during="exec") from None
        self._stop(sandbox, 60.0)

    def _stop(self, sandbox: object, timeout_s: float) -> None:
        for attempt in (1, 2):
            try:
                sandbox.stop(missing_ok=True).result(timeout=timeout_s)
                return
            except Exception as exc:               # noqa: BLE001
                if attempt == 1 and _status(exc) == "UNAVAILABLE":
                    self._clock.sleep(STOP_RETRY_WAIT_S)
                    continue
                if isinstance(exc, MhforgeError):
                    raise
                raise map_sdk_error(exc, during="exec") from None
