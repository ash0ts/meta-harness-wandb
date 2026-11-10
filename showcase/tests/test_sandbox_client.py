# SPDX-License-Identifier: MIT
"""executors/sandbox_client.py: the CwSandboxClient adapter against a fake `cwsandbox` module injected into sys.modules
(spec 3.17). The real SDK is never imported; the adapter stays UNVERIFIED until the preflight."""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.abc
import json
import os
import pickle
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from mhforge.errors import ChannelClosed, ExecutorError
from mhforge.executors import sandbox_client as sc
from mhforge.executors.sandbox_client import (CwSandboxClient, ExecResult, QuotaExceeded, SandboxSpec, map_sdk_error)
from tests.helpers import FakeClock, child_env, make_manifest

KEY = "sbx-test-key-0123456789abcdef"
NAME = "MHFORGE_SANDBOX_API_KEY"
DIGEST = "sha256:" + "0" * 64


# ---------------------------------------------------------------- the fake SDK

class FakeOp:
    """cwsandbox.OperationRef: result(timeout=None); a client-side timeout raises TimeoutError (in 3.11+ the same class
    as concurrent.futures.TimeoutError, which OperationRef.result raises)."""

    def __init__(self, log, what, value=None, exc=None, block=False):
        self.log, self.what, self.value, self.exc, self.block = log, what, value, exc, block

    def result(self, timeout=None):
        self.log.append(("result", self.what, timeout))
        if self.block:
            time.sleep(timeout or 0)
            raise TimeoutError("operation did not complete")
        if self.exc is not None:
            raise self.exc
        return self.value


# The parameter names of the cwsandbox 1.27.0 calls the fake stands in for (inspect.signature of the installed SDK,
# self/cls dropped). test_fake_matches_recorded_sdk_signatures holds the fake to them; update both on an SDK bump.
SDK_1_27_0_PARAMS = {
    "Sandbox.run": ("args", "container_image", "defaults", "auth", "request_timeout_seconds",
                    "create_retry_budget_seconds", "poll_retry_budget_seconds", "poll_rpc_timeout_seconds",
                    "max_lifetime_seconds", "tags", "profile_ids", "profile_names", "runner_ids", "resources",
                    "mounted_files", "s3_mount", "ports", "network", "network_config_id", "file_system_snapshot",
                    "max_timeout_seconds", "placement_mode", "placement_spillover", "services", "volumes", "template_id",
                    "image_pull_credentials", "runtime_class", "security_context", "working_dir",
                    "object_storage_access", "environment_variables", "annotations", "secrets", "data_plane_mode",
                    "containers"),
    "Sandbox.list": ("tags", "status", "profile_ids", "profile_names", "runner_ids", "show_terminated", "base_url",
                     "auth", "timeout_seconds", "poll_retry_budget_seconds", "poll_rpc_timeout_seconds",
                     "data_plane_mode", "volume_ids"),
    "Sandbox.from_id": ("sandbox_id", "base_url", "auth", "timeout_seconds", "poll_retry_budget_seconds",
                        "poll_rpc_timeout_seconds", "data_plane_mode"),
    "Sandbox.wait": ("timeout",),
    "Sandbox.exec": ("command", "cwd", "check", "timeout_seconds", "stdin", "container"),
    "Sandbox.write_file": ("filepath", "contents", "timeout_seconds", "container"),
    "Sandbox.read_file": ("filepath", "timeout_seconds", "container"),
    "Sandbox.stop": ("snapshot_on_stop", "graceful_shutdown_seconds", "missing_ok", "wait_for_ready", "request_id"),
    "AuthHeaders": ("headers", "strategy"),
    "WandbAuth": ("entity", "project"),
    "NetworkOptions": ("deny_egress", "deny_ingress", "egress", "ingress", "dns_egress"),
    "Process.result": ("timeout",),
    "StreamWriter.write": ("data",),
    "StreamWriter.close": (),
    "CWSandboxError": ("message", "reason", "metadata", "retry_delay"),
    "SandboxFailedError": ("message", "sandbox_id", "status_reason", "reason", "metadata", "retry_delay"),
}


class FakeRpcError(Exception):
    """grpc.aio.AioRpcError as cwsandbox leaves it in __cause__: code() returns a StatusCode-like enum with .name."""

    def __init__(self, status: str):
        super().__init__(status)
        self._status = status

    def code(self):
        return types.SimpleNamespace(name=self._status)


def from_rpc(exc: BaseException, status: str) -> BaseException:
    """The SDK error as the SDK raises it: `raise _translate_rpc_error(e) from e` sets __cause__ to the RpcError."""
    exc.__cause__ = FakeRpcError(status)
    return exc


def make_fake():
    """A module object with the cwsandbox 1.27.0 names the adapter uses, keyword-only where the SDK is, with no
    catch-all **kwargs (a wrong keyword is a TypeError, as in the SDK); every call is appended to mod.log."""
    mod = types.ModuleType("cwsandbox")
    mod.__version__ = "1.27.0"
    log: list = []
    mod.log = log
    mod.script = {"run": [], "exec": [], "stop": [], "write": [], "read": {}, "stdout": [], "stdin_block": False}

    class CWSandboxError(Exception):                       # exceptions.py: no `code` or `status` attribute
        def __init__(self, message, *, reason=None, metadata=None, retry_delay=None):
            super().__init__(message)
            self.reason, self.metadata, self.retry_delay = reason, dict(metadata or {}), retry_delay

    class CWSandboxAuthenticationError(CWSandboxError): ...   # UNAUTHENTICATED and PERMISSION_DENIED (_network.py)
    class SandboxError(CWSandboxError): ...
    class SandboxResourceExhaustedError(SandboxError): ...
    class SandboxNotRunningError(SandboxError): ...
    class SandboxUnavailableError(SandboxNotRunningError): ...
    class SandboxTimeoutError(SandboxError): ...
    class SandboxCommandTimeoutError(SandboxTimeoutError): ...
    class SandboxRequestTimeoutError(SandboxTimeoutError): ...
    class SandboxFileError(SandboxError): ...

    class SandboxFailedError(SandboxError):
        def __init__(self, message, *, sandbox_id=None, status_reason=None, reason=None, metadata=None,
                     retry_delay=None):
            super().__init__(message, reason=reason, metadata=metadata, retry_delay=retry_delay)
            self.sandbox_id, self.status_reason = sandbox_id, status_reason

    class SandboxNotFoundError(SandboxError):
        def __init__(self, message, *, sandbox_id=None, reason=None, metadata=None, retry_delay=None):
            super().__init__(message, reason=reason, metadata=metadata, retry_delay=retry_delay)
            self.sandbox_id = sandbox_id

    for cls in (CWSandboxError, CWSandboxAuthenticationError, SandboxError, SandboxResourceExhaustedError,
                SandboxNotRunningError, SandboxUnavailableError, SandboxTimeoutError, SandboxCommandTimeoutError,
                SandboxRequestTimeoutError, SandboxFileError, SandboxFailedError, SandboxNotFoundError):
        setattr(mod, cls.__name__, cls)

    @dataclasses.dataclass(frozen=True)
    class AuthHeaders:                                     # _auth.py: both fields required
        headers: dict
        strategy: str

    @dataclasses.dataclass(frozen=True)
    class WandbAuth:
        entity: str | None = None
        project: str | None = None

        def __post_init__(self):
            for name in ("entity", "project"):
                value = getattr(self, name)
                if value is not None and not value.strip():
                    raise ValueError(f"WandbAuth {name} must be a non-empty string or None")

    @dataclasses.dataclass(frozen=True, kw_only=True)
    class NetworkOptions:                                  # _types.py: keyword-only, dns_egress coerced to the enum
        deny_egress: bool | None = None
        deny_ingress: bool | None = None
        egress: object = None
        ingress: object = None
        dns_egress: str | None = None

        def __post_init__(self):
            if isinstance(self.dns_egress, str):
                if self.dns_egress.lower() not in ("unspecified", "allow", "deny"):
                    raise ValueError(f"{self.dns_egress!r} is not a valid DNSEgressMode")
                object.__setattr__(self, "dns_egress", self.dns_egress.lower())
            if self.deny_egress is True and self.egress:
                raise ValueError("NetworkOptions.deny_egress cannot be combined with egress rules")

    class Session:
        def __init__(self, *a, **kw):
            log.append(("Session", a, kw))

    class StreamWriter:
        def write(self, data):
            log.append(("stdin.write", len(data)))
            return FakeOp(log, "stdin.write", block=mod.script["stdin_block"])

        def close(self):
            log.append(("stdin.close",))
            return FakeOp(log, "stdin.close")

    class Process:                                         # an OperationRef[ProcessResult] with stdout/stderr/stdin
        def __init__(self, value=None, exc=None, stdin=False):
            self.stdout = iter(list(mod.script["stdout"]))    # StreamReader[str]: str chunks only
            self.stderr = iter([])
            self.stdin = StreamWriter() if stdin else None
            self._value, self._exc = value, exc

        def result(self, timeout=None):
            log.append(("result", "exec", timeout))
            if self._exc is not None:
                raise self._exc
            return self._value

    def process_result(returncode, stdout, stderr):       # _types.ProcessResult: stdout/stderr are decoded str
        return types.SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode,
                                     stdout_bytes=stdout.encode(), stderr_bytes=stderr.encode(), command=[])

    class Sandbox:
        def __init__(self, sandbox_id):
            self._sandbox_id = sandbox_id

        @property
        def sandbox_id(self):
            return self._sandbox_id

        @classmethod
        def run(cls, *args, container_image=None, defaults=None, auth=None, request_timeout_seconds=None,
                create_retry_budget_seconds=None, poll_retry_budget_seconds=None, poll_rpc_timeout_seconds=None,
                max_lifetime_seconds=None, tags=None, profile_ids=None, profile_names=None, runner_ids=None,
                resources=None, mounted_files=None, s3_mount=None, ports=None, network=None, network_config_id=None,
                file_system_snapshot=None, max_timeout_seconds=None, placement_mode=None, placement_spillover=None,
                services=None, volumes=None, template_id=None, image_pull_credentials=None, runtime_class=None,
                security_context=None, working_dir=None, object_storage_access=None, environment_variables=None,
                annotations=None, secrets=None, data_plane_mode=None, containers=None):
            passed = locals()
            given = {k: passed[k] for k in SDK_1_27_0_PARAMS["Sandbox.run"][1:] if passed[k] is not None}
            log.append(("run", args, given))
            if network is not None and not isinstance(network, (NetworkOptions, dict)):
                raise TypeError("network must be NetworkOptions, dict, or None")
            if isinstance(tags, str):
                raise TypeError("tags must be a sequence of strings, not a bare string")
            if mod.script["run"]:
                exc = mod.script["run"].pop(0)
                if exc is not None:
                    raise exc
            return cls(f"sbx-{sum(1 for e in log if e[0] == 'run')}")

        @classmethod
        def list(cls, *, tags=None, status=None, profile_ids=None, profile_names=None, runner_ids=None,
                 show_terminated=False, base_url=None, auth=None, timeout_seconds=None, poll_retry_budget_seconds=None,
                 poll_rpc_timeout_seconds=None, data_plane_mode="auto", volume_ids=None):
            log.append(("list", (), {"tags": tags, "base_url": base_url, "auth": auth,
                                     "show_terminated": show_terminated}))
            return FakeOp(log, "list", value=[cls("sbx-a"), cls("sbx-b")])

        @classmethod
        def from_id(cls, sandbox_id, *, base_url=None, auth=None, timeout_seconds=None, poll_retry_budget_seconds=None,
                    poll_rpc_timeout_seconds=None, data_plane_mode="auto"):
            log.append(("from_id", (sandbox_id,), {"base_url": base_url, "auth": auth}))
            return FakeOp(log, "from_id", value=cls(sandbox_id))

        def wait(self, timeout=None):
            log.append(("wait", timeout))
            return self

        def exec(self, command, *, cwd=None, check=False, timeout_seconds=None, stdin=False, container=None):
            log.append(("exec", (list(command),), {"cwd": cwd, "timeout_seconds": timeout_seconds, "stdin": stdin}))
            if stdin:
                return Process(value=process_result(0, "", ""), stdin=True)
            exc = mod.script["exec"].pop(0) if mod.script["exec"] else None
            return Process(value=process_result(3, "out", "err�"), exc=exc)

        def write_file(self, filepath, contents, *, timeout_seconds=None, container=None):
            log.append(("write_file", (filepath, len(contents)), {"timeout_seconds": timeout_seconds}))
            exc = mod.script["write"].pop(0) if mod.script["write"] else None
            return FakeOp(log, "write_file", exc=exc)

        def read_file(self, filepath, *, timeout_seconds=None, container=None):
            log.append(("read_file", (filepath,), {"timeout_seconds": timeout_seconds}))
            item = mod.script["read"].get(filepath, b"{}")
            return FakeOp(log, "read_file", value=None if isinstance(item, BaseException) else item,
                          exc=item if isinstance(item, BaseException) else None)

        def stop(self, *, snapshot_on_stop=False, graceful_shutdown_seconds=10.0, missing_ok=False,
                 wait_for_ready=True, request_id=None):
            log.append(("stop", self.sandbox_id, {"missing_ok": missing_ok}))
            exc = mod.script["stop"].pop(0) if mod.script["stop"] else None
            return FakeOp(log, "stop", exc=exc)

    mod.AuthHeaders, mod.WandbAuth, mod.NetworkOptions, mod.Session, mod.Sandbox = (
        AuthHeaders, WandbAuth, NetworkOptions, Session, Sandbox)
    mod.Process, mod.StreamWriter = Process, StreamWriter
    return mod


class BlockImport(importlib.abc.MetaPathFinder):
    """Fails any import of the named top-level packages, and records the attempts."""

    def __init__(self, *names):
        self.names, self.attempts = set(names), []

    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in self.names:
            self.attempts.append(fullname)
            raise ImportError(f"blocked in test: {fullname}")
        return None


def spec(**over) -> SandboxSpec:
    base = dict(image="registry.example.com/w@" + DIGEST, fallback_image=None, cpu="2", memory="4Gi",
                max_lifetime_s=8250, tags=("mhf", "mhf-c-mh-test"), env={"WANDB_API_KEY": ""}, deny_egress=True,
                dns_egress="deny", working_dir="/mhf", entity="example-entity", project="mh-forge-test")
    base.update(over)
    return SandboxSpec(**base)


class AdapterCase(unittest.TestCase):
    """Each test gets a fake SDK in sys.modules, a private process-environment dict and a wandb import blocker."""

    def setUp(self):
        self.fake = make_fake()
        self.penv: dict = {}
        self.blocker = BlockImport("wandb")
        patches = [mock.patch.dict(sys.modules, {"cwsandbox": self.fake}),
                   mock.patch.object(os, "environ", self.penv)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        sys.meta_path.insert(0, self.blocker)
        self.addCleanup(sys.meta_path.remove, self.blocker)
        self.clock = FakeClock()

    def client(self, manifest=None, **kw) -> CwSandboxClient:
        environ = kw.pop("environ", {NAME: KEY})
        return CwSandboxClient(manifest or make_manifest(), environ=environ, clock=self.clock, **kw)

    def calls(self, kind):
        return [e for e in self.fake.log if e[0] == kind]

    # ------------------------------------------------------------ auth
    def test_missing_or_empty_auth_variable_is_executor_error(self):
        for environ in ({}, {NAME: ""}, {"WANDB_API_KEY": KEY}):
            scrubbed = []
            with self.subTest(environ=sorted(environ)):
                with self.assertRaises(ExecutorError) as cm:
                    CwSandboxClient(make_manifest(), environ=environ, scrub=lambda n: scrubbed.append(n) or True)
                self.assertEqual((cm.exception.kind, cm.exception.retryable), ("auth", False))
                self.assertNotIn(NAME, environ)
                self.assertEqual(scrubbed, [NAME])           # scrubbed and removed even when absent or empty
        for override in ({"decisions.entity": None}, {"decisions.project": None}):
            with self.subTest(override=override), self.assertRaises(ExecutorError) as cm:
                CwSandboxClient(make_manifest(override), environ={NAME: KEY})
            self.assertEqual(cm.exception.kind, "auth")
        self.assertEqual(self.fake.log, [])                  # no SDK call was made, no fallback to an SDK default

    def test_auth_headers_passed_explicitly_and_wandb_not_imported(self):
        before = "wandb" in sys.modules
        client = self.client()
        handle = client.create(spec())
        self.assertEqual(client.list_ids(["mhf"]), ["sbx-a", "sbx-b"])
        client.stop_id("sbx-9")
        auths = [e[2]["auth"] for e in self.fake.log if e[0] in ("run", "list", "from_id")]
        self.assertEqual(len(auths), 3)
        for auth in auths:
            self.assertIsInstance(auth, self.fake.AuthHeaders)
            self.assertEqual(auth.headers, {"x-wandb-api-key": KEY, "x-wandb-sdk-version": "0.31.0",
                                            "x-entity-id": "example-entity", "x-project-name": "mh-forge-test"})
            self.assertEqual(auth.strategy, "wandb_api_key")
        self.assertEqual(self.blocker.attempts, [])
        self.assertEqual("wandb" in sys.modules, before)
        self.assertNotIn("WANDB_API_KEY", self.penv)         # mode headers never sets the variable
        self.assertEqual(self.penv.get("CWSANDBOX_DISABLE_SIGNAL_HANDLERS"), "1")
        self.assertEqual(handle.id, "sbx-1")

    def test_auth_key_scrubbed_and_digest_in_key_facts(self):
        scrubbed = []
        environ = {NAME: KEY, "OTHER": "kept"}
        client = self.client(environ=environ, scrub=lambda n: scrubbed.append(n) or True)
        self.assertEqual(scrubbed, [NAME])
        self.assertEqual(environ, {"OTHER": "kept"})
        self.assertEqual(client.key_facts, {"name": NAME, "len": len(KEY),
                                            "sha256": hashlib.sha256(KEY.encode()).hexdigest(), "scrubbed": True})
        self.assertNotIn(KEY, json.dumps(client.key_facts))
        self.assertNotIn(KEY, repr(client))
        with self.assertRaises(TypeError):
            pickle.dumps(client)
        self.assertIsNone(self.client().key_facts["scrubbed"])
        self.assertFalse(self.client(scrub=lambda n: False).key_facts["scrubbed"])
        # never in an exec argument or a sandbox variable
        handle = client.create(spec())
        with self.assertRaises(ValueError):
            handle.exec(["echo", KEY], cwd=None, timeout_s=5)
        with self.assertRaises(ValueError):
            handle.exec_stream(["sh", "-c", f"x={KEY}"], cwd=None, timeout_s=5)
        with self.assertRaises(ValueError):
            client.create(spec(env={"WANDB_API_KEY": KEY}))
        self.assertEqual(len(self.calls("run")), 1)

    def test_wandb_auth_mode_sets_key_in_process_with_explicit_entity_and_project(self):
        environ = {NAME: KEY}
        client = self.client(make_manifest({"sandbox.auth_mode": "wandb_auth"}), environ=environ,
                             scrub=lambda n: True)
        self.assertEqual(self.penv.get("WANDB_API_KEY"), KEY)    # set in this process; no child inherits it
        self.assertNotIn(NAME, environ)
        client.create(spec())
        client.list_ids(["mhf", "mhf-c-mh-test"])
        client.stop_id("sbx-3")
        auths = [e[2]["auth"] for e in self.fake.log if e[0] in ("run", "list", "from_id")]
        self.assertEqual(len(auths), 3)
        for auth in auths:
            self.assertIsInstance(auth, self.fake.WandbAuth)
            self.assertEqual((auth.entity, auth.project), ("example-entity", "mh-forge-test"))
        self.assertNotIn(KEY, repr(self.fake.log))               # the key reaches the SDK only through the variable
        with self.assertRaises(ExecutorError):
            self.client(make_manifest({"sandbox.auth_mode": "netrc"}))

    # ------------------------------------------------------------ calls
    def test_create_arguments_mapping(self):
        client = self.client()
        handle = client.create(spec())
        (_, args, kwargs), = self.calls("run")
        self.assertEqual(args[:2], ("/bin/sh", "-c"))
        self.assertIn('mkdir -p "$1"', args[2])
        self.assertEqual(args[-2:], ("mhforge", "/mhf"))
        self.assertEqual(set(kwargs), {"container_image", "auth", "max_lifetime_seconds", "tags", "resources", "network",
                                       "working_dir", "environment_variables"})
        self.assertEqual(kwargs["container_image"], "registry.example.com/w@" + DIGEST)
        self.assertEqual(kwargs["max_lifetime_seconds"], 8250)
        self.assertEqual(kwargs["tags"], ["mhf", "mhf-c-mh-test"])
        self.assertEqual(kwargs["resources"], {"cpu": "2", "memory": "4Gi"})
        self.assertIsInstance(kwargs["network"], self.fake.NetworkOptions)
        self.assertEqual((kwargs["network"].deny_egress, kwargs["network"].dns_egress, kwargs["network"].egress),
                         (True, "deny", None))
        self.assertEqual((kwargs["working_dir"], kwargs["environment_variables"]), ("/", {"WANDB_API_KEY": ""}))
        self.assertEqual(self.calls("Session"), [])
        # review regression: deny-egress is always set; a spec that asks for anything else is refused, no SDK call
        for over in ({"deny_egress": False}, {"dns_egress": "allow"}, {"deny_egress": 1}):
            with self.subTest(over=over), self.assertRaises(ValueError):
                client.create(spec(**over))
        self.assertEqual(len(self.calls("run")), 1)
        handle.wait_running(180)
        self.assertEqual(self.calls("wait"), [("wait", 180)])
        res = handle.exec(["python3", "-c", "print(1)"], cwd="/mhf", timeout_s=10)
        self.assertEqual(res, ExecResult(3, "out", "err�", False))
        self.assertEqual(self.calls("exec")[-1], ("exec", (["python3", "-c", "print(1)"],),
                                                  {"cwd": "/mhf", "timeout_seconds": 10, "stdin": False}))
        self.assertIn(("result", "exec", 40), self.fake.log)
        handle.write_file("/mhf/job.json", b"{}", timeout_s=30)
        self.assertEqual(self.calls("write_file"), [("write_file", ("/mhf/job.json", 2), {"timeout_seconds": 30})])
        self.fake.script["read"]["/mhf/run/exit.json"] = b'{"reason": "exit"}'
        self.assertEqual(handle.read_file("/mhf/run/exit.json", timeout_s=30, max_bytes=100), b'{"reason": "exit"}')
        with self.assertRaises(ExecutorError):
            handle.read_file("/mhf/run/exit.json", timeout_s=30, max_bytes=5)
        self.fake.script["read"]["/mhf/run/gone.json"] = self.fake.SandboxFileError("x", reason="CWSANDBOX_FILE_NOT_FOUND")
        with self.assertRaises(FileNotFoundError):
            handle.read_file("/mhf/run/gone.json", timeout_s=30, max_bytes=100)
        handle.stop(timeout_s=60)
        self.assertEqual(self.calls("stop"), [("stop", "sbx-1", {"missing_ok": True})])
        client.stop_id("sbx-7")
        self.assertEqual(self.calls("from_id")[0][1], ("sbx-7",))
        self.assertEqual(self.calls("stop")[-1][1], "sbx-7")
        self.assertEqual(self.calls("list"), [])
        client.list_ids(("mhf", "mhf-c-mh-test"))
        self.assertEqual(self.calls("list")[0][2]["tags"], ["mhf", "mhf-c-mh-test"])
        self.assertFalse(self.calls("list")[0][2]["show_terminated"])     # active sandboxes only (the SDK default)
        for kind in ("list", "from_id"):
            self.assertEqual({e[2]["base_url"] for e in self.calls(kind)}, {sc.SANDBOX_BASE_URL})

    def test_stdin_write_has_timeout(self):
        client = self.client(make_manifest({"relay.pong_timeout_s": 0.3}))
        handle = client.create(spec())
        # cwsandbox 1.27.0 decodes each gRPC chunk itself (errors="replace") and queues str only
        self.fake.script["stdout"] = ["#MHF1 a", "bc €", "tail"]
        stream = handle.exec_stream(["python3", "/mhf/bin/mhf_relay_stub.py", "bridge"], cwd="/mhf", timeout_s=900)
        self.assertEqual(self.calls("exec")[-1][2], {"cwd": "/mhf", "timeout_seconds": 900, "stdin": True})
        texts = []
        while True:
            item = stream.read_text()
            if item is None:
                break
            texts.append(item)
        self.assertEqual("".join(texts), "#MHF1 abc €tail")
        self.assertIsNone(stream.read_text())
        stream.write(b"frame")                                     # a write that completes
        self.assertIn(("result", "stdin.write", 0.3), self.fake.log)
        self.fake.script["stdin_block"] = True                     # a stalled stream: the write's result never comes
        t0 = time.monotonic()
        with self.assertRaises(ChannelClosed):
            stream.write(b"frame")
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertEqual(self.fake.log[-1], ("result", "stdin.write", 0.3))
        with self.assertRaises(ChannelClosed):                     # a broken stream refuses at once
            stream.write(b"more")
        stream.close()
        self.assertIn(("stdin.close",), self.fake.log)
        self.assertIsNone(stream.read_text())
        self.assertEqual(stream.result(5), ExecResult(0, "", "", False))

    def test_exception_mapping(self):
        f = self.fake
        client = self.client()
        f.script["run"] = [f.SandboxError("boom")]
        with self.assertRaises(ExecutorError) as cm:
            client.create(spec())
        self.assertEqual((cm.exception.kind, cm.exception.retryable), ("create", True))
        f.script["run"] = [f.CWSandboxAuthenticationError("bad key")]
        with self.assertRaises(ExecutorError) as cm:
            client.create(spec())
        self.assertEqual((cm.exception.kind, cm.exception.retryable), ("auth", False))
        f.script["run"] = [RuntimeError("anything else")]
        with self.assertRaises(ExecutorError) as cm:
            client.create(spec())
        self.assertEqual(cm.exception.kind, "create")
        handle = client.create(spec())
        f.script["exec"] = [f.SandboxCommandTimeoutError("t", reason="CWSANDBOX_COMMAND_TIMEOUT"), f.SandboxError("x")]
        self.assertEqual(handle.exec(["true"], cwd=None, timeout_s=1), ExecResult(None, "", "", True))
        with self.assertRaises(ExecutorError) as cm:
            handle.exec(["true"], cwd=None, timeout_s=1)
        self.assertEqual((cm.exception.kind, cm.exception.retryable), ("exec", True))
        f.script["write"] = [f.SandboxError("upload failed")]
        with self.assertRaises(ExecutorError) as cm:
            handle.write_file("/mhf/bundle.tar", b"x", timeout_s=900)
        self.assertEqual(cm.exception.kind, "upload")
        # stop: retried once after 20 s on UNAVAILABLE, then given up
        f.script["stop"] = [f.SandboxUnavailableError("busy")]
        handle.stop(timeout_s=30)
        self.assertEqual(len(self.calls("stop")), 2)
        self.assertEqual(self.clock.sleeps, [20.0])
        f.script["stop"] = [f.SandboxUnavailableError("busy"), f.SandboxUnavailableError("busy")]
        with self.assertRaises(ExecutorError):
            handle.stop(timeout_s=30)
        f.script["stop"] = [f.SandboxError("other")]
        with self.assertRaises(ExecutorError):
            handle.stop(timeout_s=30)
        self.assertEqual(self.clock.sleeps, [20.0, 20.0])         # only UNAVAILABLE is retried
        # the message text never decides: a quota-sounding message without the reason is not a quota error
        self.assertEqual(map_sdk_error(f.SandboxError("concurrent sandbox quota exceeded"), during="create").kind,
                         "create")

    def test_error_reason_mapping(self):
        f = self.fake
        cases = [
            (f.SandboxResourceExhaustedError("x", reason="CWSANDBOX_OCCUPANCY_QUOTA_EXCEEDED"), "quota", True),
            (f.CWSandboxError("x", reason="CWSANDBOX_SERVERLESS_CREATE_THROTTLED", metadata={"limit_kind": "rate"}),
             "quota", True),
            (f.SandboxResourceExhaustedError("x"), "billing", False),
            (f.SandboxResourceExhaustedError("x", reason="CWSANDBOX_SOMETHING_ELSE"), "create", True),
            # the SDK raises CWSandboxAuthenticationError for both; only the grpc status in __cause__ tells them apart
            (from_rpc(f.CWSandboxAuthenticationError("Permission denied: x"), "PERMISSION_DENIED"), "permission", False),
            (from_rpc(f.CWSandboxAuthenticationError("Authentication failed: x"), "UNAUTHENTICATED"), "auth", False),
            (from_rpc(f.SandboxResourceExhaustedError("x"), "RESOURCE_EXHAUSTED"), "billing", False),
            (from_rpc(f.SandboxUnavailableError("x"), "UNAVAILABLE"), "create", True),
            (f.CWSandboxAuthenticationError("x"), "auth", False),
            (f.SandboxError("x"), "create", True),
        ]
        for exc, kind, retryable in cases:
            with self.subTest(exc=type(exc).__name__, reason=exc.reason, code=getattr(exc, "code", None)):
                err = map_sdk_error(exc, during="create")
                self.assertEqual((err.kind, err.retryable), (kind, retryable))
                self.assertEqual(isinstance(err, QuotaExceeded), kind == "quota")
        grpc_like = Exception("x")
        grpc_like.code = lambda: types.SimpleNamespace(name="PERMISSION_DENIED")
        self.assertEqual(map_sdk_error(grpc_like, during="create").kind, "permission")
        # through the adapter: a quota refusal at create is QuotaExceeded for the executor to wait on
        client = self.client()
        f.script["run"] = [cases[0][0]]
        with self.assertRaises(QuotaExceeded) as cm:
            client.create(spec())
        self.assertTrue(cm.exception.retryable)
        f.script["run"] = [cases[2][0]]
        with self.assertRaises(ExecutorError) as cm:
            client.create(spec())
        self.assertEqual((cm.exception.kind, cm.exception.retryable), ("billing", False))
        self.assertEqual(QuotaExceeded().kind, "quota")
        with self.assertRaises(ValueError):
            QuotaExceeded(kind="create")

    def test_fallback_image_tried_once_on_pull_failure(self):
        f = self.fake
        primary, fallback = "public.ecr.aws/docker/library/python@" + DIGEST, "docker.io/library/python@" + DIGEST
        client = self.client()
        pull = lambda: f.SandboxError("pull", reason="CWSANDBOX_IMAGE_PULL_FAILED")   # noqa: E731
        f.script["run"] = [pull()]
        handle = client.create(spec(image=primary, fallback_image=fallback))
        self.assertEqual([e[2]["container_image"] for e in self.calls("run")], [primary, fallback])
        self.assertEqual(handle.id, "sbx-2")
        f.log.clear()
        f.script["run"] = [pull(), pull()]
        with self.assertRaises(ExecutorError) as cm:
            client.create(spec(image=primary, fallback_image=fallback))
        self.assertEqual((cm.exception.kind, cm.exception.retryable), ("create", True))
        self.assertEqual(len(self.calls("run")), 2)                # once, never a third time
        f.log.clear()
        f.script["run"] = [pull()]
        with self.assertRaises(ExecutorError):
            client.create(spec(image=primary, fallback_image=None))
        self.assertEqual(len(self.calls("run")), 1)
        f.log.clear()
        f.script["run"] = [f.SandboxError("not a pull failure")]
        with self.assertRaises(ExecutorError):
            client.create(spec(image=primary, fallback_image=fallback))
        self.assertEqual(len(self.calls("run")), 1)
        f.log.clear()
        f.script["run"] = [pull(), f.SandboxResourceExhaustedError("x", reason="CWSANDBOX_OCCUPANCY_QUOTA_EXCEEDED")]
        with self.assertRaises(QuotaExceeded):
            client.create(spec(image=primary, fallback_image=fallback))
        # the shape cwsandbox 1.27.0 gives a pull failure: SandboxFailedError with a kubelet status_reason
        failed = f.SandboxFailedError("Sandbox sbx failed to start: ErrImagePull: x", sandbox_id="sbx",
                                      status_reason="ErrImagePull: Failed to pull image")
        self.assertTrue(sc._is_pull_failure(failed))
        self.assertFalse(sc._is_pull_failure(f.SandboxFailedError("x", status_reason="OOMKilled: x")))
        f.log.clear()
        f.script["run"] = [failed]
        client.create(spec(image=primary, fallback_image=fallback))
        self.assertEqual([e[2]["container_image"] for e in self.calls("run")], [primary, fallback])

    def test_pull_failure_at_wait_is_a_retryable_create_error(self):
        # cwsandbox 1.27.0: Sandbox.run returns once the create is accepted; the pull failure surfaces from wait().
        # The adapter cannot swap images there without changing handle.id (reported in sdk-conformance-cwsandbox.md).
        client = self.client()
        handle = client.create(spec(fallback_image="docker.io/library/python@" + DIGEST))
        failed = self.fake.SandboxFailedError("failed", sandbox_id=handle.id, status_reason="ImagePullBackOff: x")
        with mock.patch.object(type(handle._sb), "wait", side_effect=failed):
            with self.assertRaises(ExecutorError) as cm:
                handle.wait_running(180)
        self.assertEqual((cm.exception.kind, cm.exception.retryable), ("create", True))
        self.assertEqual(len(self.calls("run")), 1)

    def test_base_url_pinned_and_env_override_refused(self):
        for value in ("https://elsewhere.example", "http://127.0.0.1:9"):
            self.penv["CWSANDBOX_BASE_URL"] = value
            client = self.client()
            for call in (lambda: client.create(spec()), lambda: client.list_ids(["mhf"]),
                         lambda: client.stop_id("sbx-1")):
                with self.subTest(value=value), self.assertRaises(ExecutorError) as cm:
                    call()
                self.assertEqual((cm.exception.kind, cm.exception.retryable), ("auth", False))
        self.assertEqual([e for e in self.fake.log if e[0] in ("run", "list", "from_id")], [])
        for value in ("", sc.SANDBOX_BASE_URL, sc.SANDBOX_BASE_URL + "/"):
            self.penv["CWSANDBOX_BASE_URL"] = value
            self.client().create(spec())
        self.assertEqual(len(self.calls("run")), 3)

    def test_fake_matches_recorded_sdk_signatures(self):
        """A fake that accepts anything proves nothing: hold its parameter names to the recorded 1.27.0 ones."""
        import inspect
        f = self.fake
        objs = {"Sandbox.run": f.Sandbox.run, "Sandbox.list": f.Sandbox.list, "Sandbox.from_id": f.Sandbox.from_id,
                "Sandbox.wait": f.Sandbox.wait, "Sandbox.exec": f.Sandbox.exec,
                "Sandbox.write_file": f.Sandbox.write_file, "Sandbox.read_file": f.Sandbox.read_file,
                "Sandbox.stop": f.Sandbox.stop, "AuthHeaders": f.AuthHeaders, "WandbAuth": f.WandbAuth,
                "NetworkOptions": f.NetworkOptions, "Process.result": f.Process.result,
                "StreamWriter.write": f.StreamWriter.write, "StreamWriter.close": f.StreamWriter.close,
                "CWSandboxError": f.CWSandboxError.__init__, "SandboxFailedError": f.SandboxFailedError.__init__}
        self.assertEqual(set(objs), set(SDK_1_27_0_PARAMS))
        for name, obj in objs.items():
            with self.subTest(name=name):
                params = tuple(p for p in inspect.signature(obj).parameters if p not in ("self", "cls"))
                self.assertEqual(params, SDK_1_27_0_PARAMS[name])
        with self.assertRaises(TypeError):
            f.Sandbox.run(container_image="x", image="x")              # a wrong keyword fails, as in the SDK
        with self.assertRaises(TypeError):
            f.AuthHeaders(headers={})                                   # strategy is required
        with self.assertRaises(ValueError):
            f.NetworkOptions(deny_egress=True, dns_egress="block")
        with self.assertRaises(TypeError):
            f.NetworkOptions(True)                                      # keyword-only

    def test_entity_and_project_on_every_call(self):
        client = self.client()
        # review regression: a spec with another pair would create a sandbox that cleanup_orphans (which lists the
        # manifest's pair) never finds; it is refused before any SDK call
        for over in ({"entity": "other-entity"}, {"project": "other-project"}, {"entity": ""}):
            with self.subTest(over=over), self.assertRaises(ValueError):
                client.create(spec(**over))
        self.assertEqual(self.calls("run"), [])
        client.create(spec())
        client.list_ids(["mhf"])
        client.stop_id("sbx-1")
        seen = [(e[0], e[2]["auth"].headers["x-entity-id"], e[2]["auth"].headers["x-project-name"])
                for e in self.fake.log if e[0] in ("run", "list", "from_id")]
        self.assertEqual(seen, [("run", "example-entity", "mh-forge-test"),
                                ("list", "example-entity", "mh-forge-test"),
                                ("from_id", "example-entity", "mh-forge-test")])
        for e in self.fake.log:
            if e[0] in ("run", "list", "from_id"):
                self.assertIn("auth", e[2])

    def test_no_mount_argument_ever(self):
        self.assertEqual([fl.name for fl in dataclasses.fields(SandboxSpec)],
                         ["image", "fallback_image", "cpu", "memory", "max_lifetime_s", "tags", "env", "deny_egress",
                          "dns_egress", "working_dir", "entity", "project"])
        client = self.client()
        self.fake.script["run"] = [self.fake.SandboxError("pull", reason="CWSANDBOX_IMAGE_PULL_FAILED")]
        client.create(spec(fallback_image="docker.io/library/python@" + DIGEST))
        client.create(spec())
        runs = self.calls("run")
        self.assertEqual(len(runs), 3)
        for _, args, kwargs in runs:
            for name in kwargs:
                self.assertNotRegex(name.lower(), r"mount|volume|snapshot|session|defaults")
            self.assertEqual(args[:2], ("/bin/sh", "-c"))
            self.assertEqual(args[-2:], ("mhforge", "/mhf"))
        self.assertEqual(self.calls("Session"), [])


class Lazy(unittest.TestCase):
    CHILD = r"""
import importlib.abc, json, sys
from os import environ
seen = []
class Recorder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in ("cwsandbox", "wandb"):
            seen.append([fullname, environ.get("CWSANDBOX_DISABLE_SIGNAL_HANDLERS")])
            raise ImportError("not available in this test")
        return None
sys.meta_path.insert(0, Recorder())
from mhforge.executors import sandbox_client as m
from mhforge.executors.sandbox_client import SandboxSpec
from mhforge.errors import ExecutorError
from tests.helpers import make_manifest
after_import = list(seen)
client = m.CwSandboxClient(make_manifest(), environ={"MHFORGE_SANDBOX_API_KEY": "k" * 24})
after_init = list(seen)
try:
    client.create(SandboxSpec("r.example/x@sha256:" + "0" * 64, None, "2", "4Gi", 10, ("mhf",), {}, True, "deny",
                              "/mhf", "example-entity", "mh-forge-test"))
    err = None
except ExecutorError as exc:
    err = [exc.kind, exc.retryable]
print(json.dumps({"after_import": after_import, "after_init": after_init, "after_create": seen, "err": err,
                  "loaded": sorted(n for n in sys.modules if n.split(".")[0] in ("cwsandbox", "wandb"))}))
"""

    def test_cw_adapter_imports_lazily(self):
        proc = subprocess.run([sys.executable, "-c", self.CHILD], env=child_env(), capture_output=True, text=True,
                              timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["after_import"], [])                # importing the module imports no SDK
        self.assertEqual(out["after_init"], [])                  # nor does building a client
        self.assertEqual(out["after_create"], [["cwsandbox", "1"]])   # the first call; signal handlers disabled first
        self.assertEqual(out["err"], ["create", False])
        self.assertEqual(out["loaded"], [])
        source = Path(sc.__file__).read_text(encoding="utf-8")
        self.assertNotRegex(source, r"(?m)^\s*(import|from)\s+(cwsandbox|wandb)\b")
        self.assertNotIn("wandb.sandbox", source)


if __name__ == "__main__":
    unittest.main()
