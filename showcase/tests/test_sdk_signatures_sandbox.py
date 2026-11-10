# SPDX-License-Identifier: MIT
"""executors/sandbox_client.py against the installed cwsandbox SDK (design/sdk-conformance-cwsandbox.md).

The real CwSandboxClient is driven through a recording stand-in for the ``cwsandbox`` module. Every SDK call it makes
is recorded with its exact positional and keyword arguments, and then bound against ``inspect.signature`` of the real
SDK callable it stands for. Return values are real SDK objects where they can be built offline (``Process``,
``OperationRef``, ``ProcessResult``, ``AuthHeaders``, ``WandbAuth``, ``NetworkOptions``), so the client's use of
their methods and attributes runs against the real code. Exception classes, attributes and the names the client
matches on are checked against the real module. Nothing here starts a sandbox, resolves a credential or opens a
connection: ``Sandbox.run``, ``list``, ``from_id`` and every instance method are recorded, never called.

Skipped when cwsandbox is not importable (the standard test run has no live extra). Run it under the SDK
environment, isolated: ``env -i HOME=<empty> PATH=/usr/bin:/bin WANDB_MODE=disabled <venv>/bin/python -I ...``.
"""

from __future__ import annotations

import concurrent.futures
import dataclasses
import inspect
import os
import sys
import unittest
from unittest import mock

try:
    import cwsandbox as cw
    from cwsandbox import _cleanup as cw_cleanup, _defaults as cw_defaults
except ImportError:                                # the live extra is not installed
    cw = cw_cleanup = cw_defaults = None

from mhforge.errors import ChannelClosed, ExecutorError
from mhforge.executors import sandbox_client as sc
from mhforge.executors.sandbox_client import CwSandboxClient, SandboxSpec, map_sdk_error
from tests.helpers import FakeClock, make_manifest

KEY = "sbx-signature-test-key-0123456789"
NAME = "MHFORGE_SANDBOX_API_KEY"
DIGEST = "sha256:" + "0" * 64
PINNED = "1.27.0"


def spec(**over) -> SandboxSpec:
    base = dict(image="registry.example.com/w@" + DIGEST, fallback_image=None, cpu="2", memory="4Gi",
                max_lifetime_s=8250, tags=("mhf", "mhf-c-mh-test"), env={"WANDB_API_KEY": ""}, deny_egress=True,
                dns_egress="deny", working_dir="/mhf", entity="example-entity", project="mh-forge-test")
    base.update(over)
    return SandboxSpec(**base)


def _done(value) -> concurrent.futures.Future:
    fut: concurrent.futures.Future = concurrent.futures.Future()
    fut.set_result(value)
    return fut


class Recorder:
    """A stand-in for the cwsandbox module. Each call is logged as (sdk name, args, kwargs); the binding against the
    real signature happens in the test, so the stand-in itself accepts the arguments verbatim and proves nothing on
    its own. Constructors of plain SDK value types are the real ones."""

    def __init__(self, real) -> None:
        self.real = real
        self.log: list[tuple[str, tuple, dict]] = []
        self.run_errors: list[BaseException] = []
        rec = self

        class RecOp(real.OperationRef):
            def __init__(self, what: str, value) -> None:
                super().__init__(_done(value))
                self.what = what

            def result(self, *args, **kwargs):
                rec.log.append(("OperationRef.result", args, kwargs))
                return super().result(*args, **kwargs)

        class RecProcess(real.Process):
            def result(self, *args, **kwargs):
                rec.log.append(("Process.result", args, kwargs))
                return super().result(*args, **kwargs)

        class RecWriter:                           # StreamWriter needs the SDK's event loop; its methods are bound
            def write(self, *args, **kwargs):
                rec.log.append(("StreamWriter.write", args, kwargs))
                return RecOp("stdin.write", None)

            def close(self, *args, **kwargs):
                rec.log.append(("StreamWriter.close", args, kwargs))
                return RecOp("stdin.close", None)

        class RecSandbox:
            def __init__(self, sandbox_id: str) -> None:
                self._id = sandbox_id

            @property
            def sandbox_id(self) -> str:           # a property on the real Sandbox too
                return self._id

            @classmethod
            def run(cls, *args, **kwargs):
                rec.log.append(("Sandbox.run", args, kwargs))
                if rec.run_errors:
                    raise rec.run_errors.pop(0)
                return cls(f"sbx-{sum(1 for e in rec.log if e[0] == 'Sandbox.run')}")

            @classmethod
            def list(cls, *args, **kwargs):
                rec.log.append(("Sandbox.list", args, kwargs))
                return RecOp("list", [cls("sbx-a"), cls("sbx-b")])

            @classmethod
            def from_id(cls, *args, **kwargs):
                rec.log.append(("Sandbox.from_id", args, kwargs))
                return RecOp("from_id", cls(args[0] if args else kwargs["sandbox_id"]))

            def wait(self, *args, **kwargs):
                rec.log.append(("Sandbox.wait", args, kwargs))
                return self

            def exec(self, *args, **kwargs):
                rec.log.append(("Sandbox.exec", args, kwargs))
                command = list(args[0] if args else kwargs["command"])
                result = real.ProcessResult(stdout="out\n", stderr="err\n", returncode=0, command=command)
                stdin = RecWriter() if kwargs.get("stdin") else None
                return RecProcess(_done(result), command, iter(["frame\n"]), iter([]), stdin)

            def write_file(self, *args, **kwargs):
                rec.log.append(("Sandbox.write_file", args, kwargs))
                return RecOp("write_file", None)

            def read_file(self, *args, **kwargs):
                rec.log.append(("Sandbox.read_file", args, kwargs))
                return RecOp("read_file", b"{}")

            def stop(self, *args, **kwargs):
                rec.log.append(("Sandbox.stop", args, kwargs))
                return RecOp("stop", None)

        def value_type(name):
            def make(*args, **kwargs):
                rec.log.append((name, args, kwargs))
                return getattr(real, name)(*args, **kwargs)
            return make

        self.Sandbox = RecSandbox
        self.AuthHeaders, self.WandbAuth = value_type("AuthHeaders"), value_type("WandbAuth")
        self.NetworkOptions = value_type("NetworkOptions")

    def names(self) -> list[str]:
        return [e[0] for e in self.log]


def real_signature(name: str) -> inspect.Signature:
    """The signature of the real SDK callable for a recorded name; instance methods keep their `self`."""
    owner, _, attr = name.partition(".")
    obj = getattr(cw, owner)
    if not attr:
        return inspect.signature(obj)
    static = inspect.getattr_static(obj, attr)
    if isinstance(static, (classmethod, staticmethod)):
        return inspect.signature(getattr(obj, attr))           # cls already bound
    return inspect.signature(static)


@unittest.skipIf(cw is None, "cwsandbox is not importable (install the live extra to run the SDK signature checks)")
class SdkSignatures(unittest.TestCase):
    def setUp(self):
        self.rec = Recorder(cw)
        self.penv: dict = {}
        for p in (mock.patch.dict(sys.modules, {"cwsandbox": self.rec}), mock.patch.object(os, "environ", self.penv)):
            p.start()
            self.addCleanup(p.stop)

    def client(self, **over) -> CwSandboxClient:
        return CwSandboxClient(make_manifest(over), environ={NAME: KEY}, clock=FakeClock())

    def drive(self, client: CwSandboxClient) -> None:
        """Every SDK use of sandbox_client.py: create, wait, exec, exec_stream (write, close, result), write_file,
        read_file, stop, list_ids, stop_id."""
        handle = client.create(spec())
        handle.wait_running(180.0)
        res = handle.exec(["python3", "-c", "print(1)"], cwd="/mhf", timeout_s=30.0)
        self.assertEqual((res.returncode, res.stdout, res.stderr, res.timed_out), (0, "out\n", "err\n", False))
        stream = handle.exec_stream(["python3", "stub.py"], cwd=None, timeout_s=60.0)
        self.assertEqual(stream.read_text(), "frame\n")
        stream.write(b"PING\n")
        self.assertEqual(stream.result(5.0).returncode, 0)
        stream.close()
        handle.write_file("/mhf/job.json", b"{}", timeout_s=120.0)
        self.assertEqual(handle.read_file("/mhf/run/exit.json", timeout_s=30.0, max_bytes=1 << 20), b"{}")
        handle.stop(timeout_s=60.0)
        self.assertEqual(client.list_ids(["mhf", "mhf-c-mh-test"]), ["sbx-a", "sbx-b"])
        client.stop_id("sbx-9")

    def assertBinds(self, entries) -> None:
        for name, args, kwargs in entries:
            with self.subTest(call=name, kwargs=sorted(kwargs)):
                sig = real_signature(name)
                params = list(sig.parameters.values())
                self_arg = (object(),) if params and params[0].name == "self" else ()
                try:
                    bound = sig.bind(*self_arg, *args, **kwargs)
                except TypeError as exc:
                    self.fail(f"{name}{args}{sorted(kwargs)} does not bind to cwsandbox {sig}: {exc}")
                for pname in kwargs:                 # every keyword lands on a named parameter, not on **kwargs
                    self.assertIn(pname, sig.parameters, name)
                    self.assertIsNot(sig.parameters[pname].kind, inspect.Parameter.VAR_KEYWORD, name)
                for pname, value in bound.arguments.items():
                    if sig.parameters[pname].kind is inspect.Parameter.VAR_POSITIONAL:
                        self.assertEqual(value, (), f"{name}: no positional {pname} (a command would replace "
                                                    "the default keep-alive)")

    # ------------------------------------------------------------ the pin
    def test_installed_sdk_is_the_pinned_version(self):
        self.assertEqual(cw.__version__, PINNED, "sandbox_client.py was checked against cwsandbox 1.27.0 only")

    # ------------------------------------------------------------ every call binds
    def test_every_call_binds_to_the_real_signature_in_mode_headers(self):
        self.drive(self.client())
        self.assertEqual(self.rec.names(), [
            "AuthHeaders", "NetworkOptions", "Sandbox.run", "Sandbox.wait",
            "Sandbox.exec", "Process.result",
            "Sandbox.exec", "StreamWriter.write", "OperationRef.result", "Process.result",
            "StreamWriter.close", "OperationRef.result",
            "Sandbox.write_file", "OperationRef.result", "Sandbox.read_file", "OperationRef.result",
            "Sandbox.stop", "OperationRef.result",
            "AuthHeaders", "Sandbox.list", "OperationRef.result",
            "AuthHeaders", "Sandbox.from_id", "OperationRef.result", "Sandbox.stop", "OperationRef.result"])
        self.assertBinds(self.rec.log)

    def test_every_call_binds_to_the_real_signature_in_mode_wandb_auth(self):
        self.drive(self.client(**{"sandbox.auth_mode": "wandb_auth"}))
        self.assertEqual([n for n in self.rec.names() if n in ("AuthHeaders", "WandbAuth")], ["WandbAuth"] * 3)
        self.assertBinds(self.rec.log)
        for name, args, kwargs in self.rec.log:
            if name == "WandbAuth":
                self.assertEqual((args, kwargs), ((), {"entity": "example-entity", "project": "mh-forge-test"}))

    def test_create_fallback_rerun_binds(self):
        primary, fallback = "registry.example.com/w@" + DIGEST, "docker.io/library/python@" + DIGEST
        self.rec.run_errors = [cw.SandboxFailedError("failed", status_reason="ErrImagePull: x")]
        self.client().create(spec(image=primary, fallback_image=fallback))
        runs = [e for e in self.rec.log if e[0] == "Sandbox.run"]
        self.assertEqual([kw["container_image"] for _, _, kw in runs], [primary, fallback])
        self.assertBinds(runs)

    def test_run_keywords_and_values(self):
        self.client().create(spec())
        (_, args, kwargs), = [e for e in self.rec.log if e[0] == "Sandbox.run"]
        self.assertEqual(args, ())
        self.assertEqual(sorted(kwargs), ["auth", "container_image", "environment_variables", "max_lifetime_seconds",
                                          "network", "resources", "tags", "working_dir"])
        self.assertIsInstance(kwargs["auth"], cw.AuthHeaders)
        self.assertEqual(kwargs["auth"].strategy, "wandb_api_key")
        self.assertEqual(sorted(kwargs["auth"].headers), ["x-entity-id", "x-project-name", "x-wandb-api-key",
                                                          "x-wandb-sdk-version"])
        self.assertIsInstance(kwargs["network"], cw.NetworkOptions)
        self.assertIs(kwargs["network"].deny_egress, True)
        self.assertEqual(kwargs["network"].dns_egress, cw.DNSEgressMode.DENY)    # "deny" coerced by the real class
        self.assertIsInstance(kwargs["tags"], list)
        self.assertIsInstance(kwargs["max_lifetime_seconds"], int)
        client = self.client()
        client.list_ids(["mhf"])
        client.stop_id("sbx-1")
        for name, _, kw in self.rec.log:
            if name in ("Sandbox.list", "Sandbox.from_id"):
                self.assertEqual(kw["base_url"], sc.SANDBOX_BASE_URL, name)

    # ------------------------------------------------------------ attributes, methods, classes
    def test_module_attributes_and_methods_the_client_uses_exist(self):
        for name in ("Sandbox", "AuthHeaders", "WandbAuth", "NetworkOptions", "Process", "OperationRef",
                     "StreamWriter", "StreamReader", "ProcessResult", "DNSEgressMode"):
            self.assertTrue(hasattr(cw, name), name)
        for name in ("run", "list", "from_id"):
            self.assertIsInstance(inspect.getattr_static(cw.Sandbox, name), classmethod, name)
        for name in ("wait", "exec", "write_file", "read_file", "stop"):
            self.assertTrue(inspect.isfunction(inspect.getattr_static(cw.Sandbox, name)), name)
        self.assertIsInstance(inspect.getattr_static(cw.Sandbox, "sandbox_id"), property)
        self.assertTrue(callable(cw.OperationRef.result) and callable(cw.Process.result))
        self.assertTrue(issubclass(cw.Process, cw.OperationRef))
        self.assertTrue(callable(cw.StreamWriter.write) and callable(cw.StreamWriter.close))
        self.assertTrue(callable(getattr(cw.StreamReader, "__iter__")) and callable(getattr(cw.StreamReader, "__next__")))
        # Process.stdout and .stdin are instance attributes set by __init__; build one offline and read them
        out, err = iter(["a"]), iter([])
        proc = cw.Process(_done(cw.ProcessResult(stdout="o", stderr="e", returncode=3)), ["x"], out, err, None)
        self.assertIs(proc.stdout, out)
        self.assertIsNone(proc.stdin)
        self.assertEqual(sc._exec_result(proc.result(timeout=1.0)), sc.ExecResult(3, "o", "e", False))
        fields = {f.name for f in dataclasses.fields(cw.ProcessResult)}
        self.assertLessEqual({"stdout", "stderr", "returncode"}, fields)
        self.assertEqual([f.name for f in dataclasses.fields(cw.AuthHeaders)], ["headers", "strategy"])

    def test_exception_classes_and_attributes_the_client_uses(self):
        base = cw.CWSandboxError
        for name in ("SandboxFailedError", "SandboxTimeoutError", "SandboxCommandTimeoutError",
                     "SandboxRequestTimeoutError", "SandboxNotFoundError", "SandboxUnavailableError",
                     "SandboxResourceExhaustedError", "CWSandboxAuthenticationError", "SandboxFileError"):
            with self.subTest(name=name):
                self.assertTrue(issubclass(getattr(cw, name), base), name)
        exc = base("x", reason="R", metadata={"k": "v"})
        self.assertEqual((exc.reason, exc.metadata, exc.retry_delay), ("R", {"k": "v"}, None))
        failed = cw.SandboxFailedError("x", sandbox_id="sbx-1", status_reason="ErrImagePull: y")
        self.assertEqual((failed.sandbox_id, failed.status_reason), ("sbx-1", "ErrImagePull: y"))
        # every class name the client matches exists (TimeoutError is the builtin, as concurrent.futures' is)
        for name in sc._TIMEOUT_CLASSES - {"TimeoutError"}:
            self.assertTrue(issubclass(getattr(cw, name), base), name)
        self.assertIs(concurrent.futures.TimeoutError, TimeoutError)
        self.assertIn("NotFound", cw.SandboxNotFoundError.__name__)          # stop_id's "NotFound" match
        self.assertTrue(issubclass(cw.SandboxCommandTimeoutError, cw.SandboxTimeoutError))
        self.assertTrue(issubclass(cw.DNSEgressUnavailableError, cw.SandboxUnavailableError))

    def test_names_the_client_matches_that_1_27_0_does_not_define(self):
        # dead but harmless branches, recorded in sdk-conformance-cwsandbox.md (rows 19, 25, 34); a new SDK that
        # defines one of them needs a fresh conformance check
        for name in ("SandboxFileNotFoundError", "PermissionDeniedError", "SandboxPermissionDeniedError",
                     "UnauthenticatedError", "ImagePullError", "SandboxImagePullError"):
            self.assertFalse(hasattr(cw, name), name)
        exception_names = [n for n in dir(cw) if isinstance(getattr(cw, n), type)
                           and issubclass(getattr(cw, n), BaseException)]
        self.assertGreater(len(exception_names), 10)
        self.assertFalse([n for n in exception_names
                          if "ImagePull" in n or "PermissionDenied" in n or "Unauthenticated" in n])

    def test_client_classification_of_real_sdk_exceptions(self):
        import grpc

        class RpcError(grpc.RpcError):
            def __init__(self, code):
                self._code = code

            def code(self):
                return self._code

        def chained(exc, code):
            try:
                raise exc from RpcError(code)
            except Exception as e:                 # noqa: BLE001
                return e

        cases = (
            (cw.SandboxResourceExhaustedError("exhausted"), ("billing", False)),
            (cw.SandboxResourceExhaustedError("busy", reason="CWSANDBOX_OCCUPANCY_QUOTA_EXCEEDED"), ("quota", True)),
            (cw.SandboxUnavailableError("down"), ("create", True)),
            (chained(cw.CWSandboxAuthenticationError("Permission denied: x"), grpc.StatusCode.PERMISSION_DENIED),
             ("permission", False)),
            (chained(cw.CWSandboxAuthenticationError("Unauthenticated: x"), grpc.StatusCode.UNAUTHENTICATED),
             ("auth", False)),
            (cw.SandboxFailedError("failed", status_reason="ErrImagePull: x"), ("create", True)),
        )
        for exc, want in cases:
            with self.subTest(cls=type(exc).__name__, reason=exc.reason):
                err = map_sdk_error(exc, during="create")
                self.assertEqual((err.kind, err.retryable), want)
        self.assertTrue(sc._is_pull_failure(cw.SandboxFailedError("f", status_reason="ErrImagePull: x")))
        self.assertTrue(sc._is_pull_failure(cw.SandboxFailedError("f", status_reason="ImagePullBackOff: x")))
        self.assertFalse(sc._is_pull_failure(cw.SandboxFailedError("f", status_reason="OOMKilled: x")))
        self.assertTrue(sc._is_timeout(cw.SandboxCommandTimeoutError("t")))
        self.assertTrue(sc._is_timeout(cw.SandboxTimeoutError("t")))
        missing = cw.SandboxFileError("missing", filepath="/mhf/x", reason="CWSANDBOX_FILE_NOT_FOUND")
        self.assertIn(sc._reason(missing), sc.FILE_NOT_FOUND_REASONS)

    def test_environment_names_and_default_host(self):
        self.assertEqual(cw_cleanup._DISABLE_SIGNAL_HANDLERS_ENV, "CWSANDBOX_DISABLE_SIGNAL_HANDLERS")
        self.assertEqual(cw_defaults.DEFAULT_BASE_URL, sc.SANDBOX_BASE_URL)
        self.client().create(spec())
        self.assertEqual(self.penv.get("CWSANDBOX_DISABLE_SIGNAL_HANDLERS"), "1")

    def test_stream_write_failure_and_read_errors_keep_their_mapping(self):
        client = self.client()
        handle = client.create(spec())
        stream = handle.exec_stream(["python3", "stub.py"], cwd=None, timeout_s=60.0)
        stream.close()
        with self.assertRaises(ChannelClosed):
            stream.write(b"x")
        with mock.patch.object(self.rec.Sandbox, "read_file",
                               side_effect=cw.SandboxFileError("gone", reason="CWSANDBOX_FILE_NOT_FOUND")):
            with self.assertRaises(FileNotFoundError):
                handle.read_file("/mhf/x", timeout_s=1.0, max_bytes=10)
        with mock.patch.object(self.rec.Sandbox, "wait", side_effect=cw.SandboxTimeoutError("t")):
            with self.assertRaises(ExecutorError) as cm:
                handle.wait_running(1.0)
        self.assertEqual(cm.exception.kind, "create")


if __name__ == "__main__":
    unittest.main()
