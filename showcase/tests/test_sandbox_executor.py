# SPDX-License-Identifier: MIT
"""Tests for ``mhforge/executors/sandbox.py`` (spec 3.17, the wait loop of 3.16, named cases of 5.2).

Every case runs over ``tests/fake_sandbox.FakeSandboxClient``: each sandbox is a directory under /tmp, each exec a
local subprocess, each stream a ``FaultChannel`` over the real relay stub, so the in-sandbox files run for real. No
live sandbox is ever created. The worker is a scripted ``inner_loop.py`` (a JSON plan on its command line), or the
stdlib stand-in package ``tests/fixtures/fake_pkg``; with ``needs_example_venv`` upstream's real worker runs inside the
fake against a real Gateway with an in-process StubProvider. The gateway is otherwise ``helpers.FakeGateway`` and the
ledger is a real ``Ledger`` (its calls are recorded into the client's call log, so the order of metering and sandbox
calls can be checked together).
"""

import dataclasses
import hashlib
import io
import json
import os
import platform
import random
import tarfile
import threading
import time
import unittest
from pathlib import Path

from mhforge import bundle, procenv, scan, util, wire
from mhforge import ledger as ledger_mod
from mhforge.errors import CampaignStop, ExecutorError
from mhforge.executors import JobSpec, classify_attempt, sandbox_cost_micro
from mhforge.executors.sandbox import SandboxExecutor, sandbox_tags
from mhforge.executors.sandbox_client import ExecResult, QuotaExceeded
from tests import helpers
from tests.fake_sandbox import FakeSandboxClient, build_fake_bundle, build_wheelhouse, mapped_create_error

def dataclasses_fields_except(a, b) -> set[str]:
    """The names of the dataclass fields in which a and b differ."""
    return {f.name for f in dataclasses.fields(a) if getattr(a, f.name) != getattr(b, f.name)}


INSANDBOX = helpers.SC / "mhforge" / "insandbox"
PY_PREFIX = ".".join(platform.python_version_tuple()[:2])
BASE = "public.ecr.aws/docker/library/python@sha256:" + "ab" * 32
FALLBACK = "docker.io/library/python@sha256:" + "ab" * 32
IMAGE = "registry.example.com/mhforge/worker@sha256:" + "0" * 64

WORKER = r'''
import hashlib, http.client, json, os, sys, time, urllib.parse
args = sys.argv[1:]
def opt(name):
    return args[args.index(name) + 1] if name in args else None
plan, out, inp = json.loads(opt("--plan")), opt("--out"), opt("--in")
base = os.environ.get("OPENROUTER_API_BASE", "")
key = os.environ.get("OPENROUTER_API_KEY", "")
state = {"env_names": sorted(os.environ), "results": [], "inputs": {},
         "key_sha256": hashlib.sha256(key.encode()).hexdigest() if key else None}
def save():
    with open(os.path.join(out, "memory.json.tmp"), "w") as fh:
        json.dump(state, fh)
    os.replace(os.path.join(out, "memory.json.tmp"), os.path.join(out, "memory.json"))
def post(i):
    u = urllib.parse.urlsplit(base)
    body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "q%d" % i}]}).encode()
    res = {"i": i}
    try:
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=60)
        conn.request("POST", u.path + "/chat/completions", body=body,
                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
        r = conn.getresponse()
        r.read()
        conn.close()
        res["status"] = r.status
    except Exception as exc:
        res["error"] = type(exc).__name__
    state["results"].append(res)
save()
for step in plan:
    if step["op"] == "post":
        for k in range(step.get("n", 1)):
            post(len(state["results"]))
            time.sleep(step.get("gap", 0))
    elif step["op"] == "sleep":
        time.sleep(step["s"])
    elif step["op"] == "inputs":
        for name in sorted(os.listdir(inp)):
            with open(os.path.join(inp, name), "rb") as fh:
                state["inputs"][name] = hashlib.sha256(fh.read()).hexdigest()
    elif step["op"] == "val":
        with open(os.path.join(out, "val.json"), "w") as fh:
            json.dump({"accuracy": 0.5, "correct": 1, "total": 2}, fh)
    save()
'''

OK_PLAN = [{"op": "post", "n": 2}, {"op": "val"}]
SLOW_PLAN = [{"op": "post", "n": 6, "gap": 0.4}, {"op": "val"}]
HANG_PLAN = [{"op": "post", "n": 1}, {"op": "sleep", "s": 300}]


def write_tar(dest: Path, pkg: dict) -> Path:
    members = {"pkg/text_classification/" + k: v for k, v in pkg.items()}
    for name in bundle.INSANDBOX_FILES:
        members["bin/" + name] = (INSANDBOX / name).read_bytes()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        for name in sorted(members):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(members[name]), 0o644
            tf.addfile(info, io.BytesIO(members[name]))
    dest.write_bytes(buf.getvalue())
    return dest


class FastClock:
    """Real wall and monotonic time; sleep() is recorded and cut to at most 10 ms (backoffs, cleanup pauses)."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        time.sleep(min(max(0.0, float(seconds)), 0.01))


class RecordingLedger:
    """A real Ledger whose meter, cap and halt calls are also recorded into the client's call log."""

    RECORDED = ("check_meter_cap", "meter_open", "meter_close", "set_halt", "open_meter_rows")

    def __init__(self, real: ledger_mod.Ledger, client: FakeSandboxClient) -> None:
        self._real, self._client = real, client

    def __getattr__(self, name):
        attr = getattr(self._real, name)
        if name not in self.RECORDED:
            return attr

        def call(*args, **kwargs):
            self._client._record("ledger", None, label=name, args=args, kwargs=kwargs)
            return attr(*args, **kwargs)
        return call


class RecordingGateway(helpers.FakeGateway):
    def __init__(self, client: FakeSandboxClient, **kw) -> None:
        super().__init__(**kw)
        self.client = client

    def register_job(self, job, *, token=None):
        self.client._record("gateway", None, label="register_job", token=token)
        super().register_job(job, token=token)

    def close_job(self, job_id, *, drain_timeout_s=None):
        self.client._record("gateway", None, label="close_job")
        return super().close_job(job_id, drain_timeout_s=drain_timeout_s)


class Base(unittest.TestCase):
    overrides: dict = {}

    def setUp(self):
        guard = util.loopback_guard()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        saved = ledger_mod.FULLFSYNC
        ledger_mod.FULLFSYNC = False
        self.addCleanup(setattr, ledger_mod, "FULLFSYNC", saved)
        ctx = helpers.tmp_state()
        self.state = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.tmp = self.state.sc / "t"
        self.tmp.mkdir(parents=True)
        self.manifest = helpers.make_manifest({
            "sandbox.expect.machine": platform.machine(), "sandbox.expect.python": PY_PREFIX,
            "relay.mode": "daemon", "relay.ping_interval_s": 5, "relay.pong_timeout_s": 30,
            "relay.reattach_backoff_s": [0, 0], "relay.reattach_attempts": 3, **self.overrides})
        self.client = FakeSandboxClient()
        self.addCleanup(self.client.close)
        self.gateway = RecordingGateway(self.client)
        self.script_tar = write_tar(self.tmp / "script.tar", {"__init__.py": b"", "inner_loop.py": WORKER.encode()})
        self.handles: list[dict] = []
        self.clock = FastClock()

    def use_wheelhouse(self, *, corrupt: bool = False) -> dict:
        info = build_wheelhouse(self.state.sc / "worker-image" / "wheelhouse" / "wheelhouse.tar", corrupt=corrupt)
        sb = self.manifest["sandbox"]
        sb.update(env_mode="wheelhouse", image=None, base_image=BASE, base_image_fallback=FALLBACK)
        sb["wheelhouse"].update(tar_sha256=info["tar_sha256"], tar_bytes=info["tar_bytes"],
                                sha256sums_sha256=info["sha256sums_sha256"])
        return info

    def make(self, clock=None) -> SandboxExecutor:
        clock = clock or self.clock
        self.ledger = ledger_mod.Ledger(self.state.paths.ledger, self.manifest, clock=clock)
        ex = SandboxExecutor(self.manifest, self.state.paths, client=self.client, gateway=self.gateway,
                             ledger=RecordingLedger(self.ledger, self.client), clock=clock)
        ex.poll_s = 0.05
        self.addCleanup(ex.close)
        return ex

    def spec(self, plan=None, *, attempt=1, split="val", timeout_s=120, inputs=(), freeze=None, tar=None,
             inner_argv=None, capture=False):
        job = wire.JobIdentity("mh-test", split, "no_memory", "USPTO", attempt, "baseline", None)
        if inner_argv is None:
            inner_argv = ("--plan", json.dumps(OK_PLAN if plan is None else plan), "--out", "{out}", "--in", "{in}")
        tar = tar or self.script_tar
        return JobSpec(identity=job, inner_argv=tuple(inner_argv), route=2, timeout_s=timeout_s, inputs=tuple(inputs),
                       expect_outputs=(("test.json",) if split == "test" else ("val.json",)), capture=capture,
                       bundle_tar=tar, bundle_sha256=util.sha256_file(tar), expect_freeze_sha256=freeze)

    def run_job(self, ex, spec):
        adir = self.state.paths.attempt_dir(spec.identity.job_key, spec.identity.attempt)

        def on_handle(h):
            self.client._record("on_handle", h.get("sandbox_id"))
            self.handles.append(h)
        result = ex.run(spec, adir, on_handle)
        usable = bundle.result_is_usable(adir / "out" / spec.expect_outputs[0])
        return result, adir, classify_attempt(result, result_usable=usable, halted=False)

    # -- call-log helpers
    def names(self, ops=("create", "wait_running", "exec", "exec_stream", "write_file", "read_file", "stop")):
        return [c.name for c in self.client.calls if c.op in ops]

    def calls(self, op, label=None):
        return [c for c in self.client.calls if c.op == op and (label is None or c.label == label)]

    def meter_rows(self):
        with ledger_mod.transaction(self.state.paths.ledger) as db:
            return [dict(r) for r in db.execute("SELECT * FROM meter_events WHERE meter='sandbox_seconds' ORDER BY utc")]

    def worker_state(self, adir):
        return json.loads((adir / "out" / "memory.json").read_text())


# ======================================================================================== the call sequence


class CallSequence(Base):
    def test_call_sequence(self):
        ex = self.make()
        result, adir, verdict = self.run_job(ex, self.spec())
        self.assertEqual(verdict, ("ok", False, None), result.stderr_tail)
        sid = result.facts["sandbox_id"]
        sandbox_ops = ("create", "wait_running", "exec", "exec_stream", "write_file", "read_file", "stop", "ledger",
                       "gateway", "on_handle")
        log = [c.name for c in self.client.calls if c.op in sandbox_ops and c.name != "ledger:open_meter_rows"]
        self.assertEqual(log, [
            "ledger:check_meter_cap", "create", "on_handle", "ledger:meter_open", "wait_running",
            "write_file:/mhf/bundle.tar", "write_file:/mhf/job.json", "exec:extract", "exec:selfcheck",
            "write_file:/mhf/job.json", "gateway:register_job", "exec:serve", "exec_stream:bridge",
            "read_file:/mhf/run/exit.json", "exec:pack", "read_file:/mhf/collect.tar", "gateway:close_job", "stop",
            "ledger:meter_close"])
        self.assertTrue(all(c.sandbox_id == sid for c in self.client.calls if c.op in ("exec", "write_file", "stop")))
        self.assertEqual(self.worker_state(adir)["results"], [{"i": 0, "status": 200}, {"i": 1, "status": 200}])
        self.assertEqual(len(self.gateway.calls), 2)
        self.assertTrue(all(c["ingress"] == "relay" for c in self.gateway.calls))
        spec_id = "val.no_memory.USPTO.a1"
        self.assertEqual(self.gateway.tokens, {spec_id: None})     # registered with no token, closed again
        self.assertEqual(self.gateway.closed, [spec_id])
        self.assertIn("val.json", result.outputs)
        self.assertEqual(result.relay_end, "exit")
        self.assertEqual(result.exit_file["reason"], "exit")
        self.assertTrue((adir / "run" / "selfcheck.json").is_file())
        jobfile = json.loads((adir / "jobfile.json").read_text())
        self.assertEqual(jobfile["job_id"], spec_id)
        self.assertTrue(jobfile["env"]["TIKTOKEN_RS_CACHE_DIR"].startswith("/mhf/"))

    def test_call_sequence_of_a_test_job_uploads_its_input(self):
        memory = self.tmp / "memory.json"
        memory.write_text('{"memory": 1}')
        ex = self.make()
        result, adir, verdict = self.run_job(ex, self.spec([{"op": "inputs"}, {"op": "post"}, {"op": "val"}],
                                                           split="test", inputs=(("memory.json", memory),),
                                                           inner_argv=None))
        names = self.names(ops=("write_file", "exec"))
        self.assertEqual(names[:4], ["write_file:/mhf/bundle.tar", "write_file:/mhf/job.json",
                                     "write_file:/mhf/in/memory.json", "exec:extract"])
        self.assertEqual(self.worker_state(adir)["inputs"],
                         {"memory.json": hashlib.sha256(b'{"memory": 1}').hexdigest()})

    def test_input_name_must_be_a_plain_file_name(self):
        bad = self.tmp / "x"
        bad.write_text("{}")
        ex = self.make()
        with self.assertRaises(ValueError):
            self.run_job(ex, self.spec(inputs=(("../memory.json", bad),)))
        self.assertEqual(self.names(ops=("stop",)), ["stop"])      # the sandbox was still stopped

    def test_create_spec(self):
        ex = self.make()
        spec = self.spec()
        self.run_job(ex, spec)
        (create,) = self.calls("create")
        s = create.args["spec"]
        self.assertEqual(s.image, IMAGE)
        self.assertIsNone(s.fallback_image)
        self.assertIs(s.deny_egress, True)
        self.assertEqual(s.dns_egress, "deny")
        self.assertEqual(s.env, {"WANDB_API_KEY": ""})
        self.assertEqual(s.max_lifetime_s, 120 + 900)
        self.assertEqual((s.cpu, s.memory, s.working_dir), ("2", "4Gi", "/mhf"))
        self.assertEqual((s.entity, s.project), ("example-entity", "mh-forge-test"))
        self.assertEqual(len(s.tags), 5)
        self.assertTrue(all(len(t) <= 63 for t in s.tags))
        digest = hashlib.sha256(b"val.no_memory.USPTO").hexdigest()[:16]
        self.assertEqual(s.tags, ("mhf", "mhf-c-mh-test", "mhf-j-" + digest, "mhf-a-1", "mhf-p-baseline"))
        self.assertEqual(s.tags, sandbox_tags(spec.identity))
        self.assertFalse(any("mount" in f.lower() or "volume" in f.lower() or "snapshot" in f.lower()
                             for f in type(s).__dataclass_fields__))

    def test_sandbox_tags_at_the_pattern_maxima(self):
        job = wire.JobIdentity("c" * 40, "test", "a" * 48, "LawBench", 99, "qualification", None, repeat=99)
        tags = sandbox_tags(job)
        self.assertEqual(len(tags), 5)
        self.assertTrue(all(len(t) <= 63 for t in tags), tags)
        self.assertEqual(tags[3:], ("mhf-a-99", "mhf-p-qualification"))

    def test_no_secret_in_any_exec_argv_or_environment(self):
        ex = self.make()
        result, adir, verdict = self.run_job(ex, self.spec())
        self.assertEqual(verdict[0], "ok", result.stderr_tail)
        seen_env = 0
        for c in self.client.calls:
            if c.op in ("exec", "exec_stream"):
                self.assertEqual(scan.scan_obj(c.args["argv"]), [], c.name)
                self.assertFalse(any("mhfjob_" in a for a in c.args["argv"]), c.name)
                env = c.args["env"]
                seen_env += 1
                self.assertEqual(env.get("WANDB_API_KEY"), "")
                self.assertNotIn("MHF_STUB_BEARER", env)
                self.assertNotIn("OPENROUTER_API_KEY", env)
                self.assertEqual([n for n, v in env.items() if v and procenv.is_credential_name(n)], [])
            if c.op == "write_file" and c.args.get("data") is not None and c.label.endswith(".json"):
                data = c.args["data"]
                self.assertEqual(scan.scan_bytes(data), [], c.name)
                doc = json.loads(data)
                self.assertNotIn("OPENROUTER_API_KEY", doc["env"])
                self.assertNotIn("MHF_STUB_BEARER", doc["env"])
                self.assertNotIn(b"mhfjob_", data)
        self.assertGreaterEqual(seen_env, 5)
        self.assertEqual(self.gateway.tokens, {"val.no_memory.USPTO.a1": None})
        # the worker saw only the relay placeholder as its key, and no credential-shaped name of the host
        st = self.worker_state(adir)
        self.assertEqual(st["key_sha256"], hashlib.sha256(wire.RELAY_API_KEY_PLACEHOLDER.encode()).hexdigest())
        self.assertNotIn("MHF_STUB_BEARER", st["env_names"])
        self.assertEqual(scan.scan_paths([adir]), [])

    def test_every_exec_has_timeout(self):
        self.use_wheelhouse()
        ex = self.make()
        memory = self.tmp / "memory.json"
        memory.write_text("{}")
        result, _, verdict = self.run_job(ex, self.spec(inputs=(("memory.json", memory),)))
        self.assertEqual(verdict[0], "ok", result.stderr_tail)
        lifetime = result.facts["lifetime_s"]
        timed = [c for c in self.client.calls if c.op in ("exec", "exec_stream", "write_file", "read_file", "stop",
                                                           "wait_running")]
        self.assertGreater(len(timed), 12)
        for c in timed:
            t = c.args.get("timeout_s")
            self.assertIsInstance(t, float, c.name)
            self.assertGreater(t, 0, c.name)
            self.assertLess(t, lifetime, c.name)                  # below the sandbox's lifetime (the SDK falls back to 300 s)
        for c in self.calls("exec_stream"):
            self.assertLessEqual(c.args["timeout_s"], lifetime - 30)


# ======================================================================================== selfcheck and proof


class Selfcheck(Base):
    def assert_no_worker(self):
        names = self.names()
        self.assertFalse(any(n in ("exec:serve", "exec_stream:serve", "exec_stream:bridge") for n in names), names)
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(names[-1], "stop")
        self.assertEqual([r["status"] for r in self.meter_rows()], ["final"])

    def test_open_egress_or_visible_credential_stops_the_campaign(self):
        for label, patch in (("tcp", {"egress": {"tcp": "open"}}), ("dns", {"egress": {"dns": "open"}}),
                             ("credential", {"credential_names": ["SOME_API_KEY"]}),
                             ("wandb_key", {"wandb_api_key": "set"})):
            with self.subTest(label):
                self.client.calls.clear()
                self.client.selfcheck_patch = patch
                ex = self.make()
                with self.assertRaises(CampaignStop) as cm:
                    self.run_job(ex, self.spec())
                self.assertEqual(cm.exception.reason, "isolation")
                self.assertEqual(self.ledger.snapshot()["halt"]["reason"], "isolation")
                self.assert_no_worker()
                self.assertIn("--probe-egress", self.calls("exec", "selfcheck")[0].args["argv"])
                self.ledger.clear_halt("test")
                for row in self.ledger.open_meter_rows("sandbox_seconds"):
                    self.fail(f"open row left: {row}")
                with ledger_mod.transaction(self.state.paths.ledger) as db:
                    db.execute("DELETE FROM meter_events")

    def test_visible_credential_in_the_container_environment_is_found_by_the_real_selfcheck(self):
        self.client.container_env = {"LEAKED_API_KEY": "x" * 12}
        ex = self.make()
        with self.assertRaises(CampaignStop) as cm:
            self.run_job(ex, self.spec())
        self.assertEqual(cm.exception.reason, "isolation")
        self.assertIn("LEAKED_API_KEY", cm.exception.detail)
        self.assert_no_worker()

    def test_missing_tokenizer_fails_before_worker_start(self):
        self.client = FakeSandboxClient(fake_litellm=False)
        self.addCleanup(self.client.close)
        self.gateway = RecordingGateway(self.client)
        ex = self.make()
        with self.assertRaises(ExecutorError) as cm:
            self.run_job(ex, self.spec())
        self.assertEqual((cm.exception.kind, cm.exception.retryable), ("image_mismatch", False))
        self.assertIn("vocab_file_present", str(cm.exception))
        self.assert_no_worker()

    def test_environment_proof_before_worker_start(self):
        cases = (("machine", {"machine": "aarch64" if platform.machine() != "aarch64" else "x86_64"}, None),
                 ("python", {"python": "2.7.18"}, None),
                 ("freeze", {}, "f" * 64))
        for mode in ("image", "wheelhouse"):
            for label, patch, freeze in cases:
                with self.subTest(mode=mode, case=label):
                    self.client.calls.clear()
                    self.client.selfcheck_patch = patch
                    if mode == "wheelhouse":
                        self.use_wheelhouse()
                    ex = self.make()
                    with self.assertRaises(ExecutorError) as cm:
                        self.run_job(ex, self.spec(freeze=freeze))
                    want = ("image_mismatch", False) if mode == "image" else ("env_setup", True)
                    self.assertEqual((cm.exception.kind, cm.exception.retryable), want)
                    self.assertIn(label if label != "freeze" else "freeze_sha256", str(cm.exception))
                    self.assert_no_worker()
                    with ledger_mod.transaction(self.state.paths.ledger) as db:
                        db.execute("DELETE FROM meter_events")

    def test_freeze_hash_recorded_not_compared_before_qualification(self):
        ex = self.make()
        result, adir, verdict = self.run_job(ex, self.spec(freeze=None))
        self.assertEqual(verdict[0], "ok", result.stderr_tail)
        report = json.loads((adir / "run" / "selfcheck.json").read_text())
        self.assertEqual(result.facts["environment"]["freeze_sha256"], report["freeze_sha256"])
        # the same freeze hash, now expected, passes too
        result2, _, verdict2 = self.run_job(ex, self.spec(attempt=2, freeze=report["freeze_sha256"]))
        self.assertEqual(verdict2[0], "ok", result2.stderr_tail)

    def test_probe_only_on_first_job_when_configured(self):
        self.manifest["sandbox"]["selfcheck"] = "first_job"
        ex = self.make()
        self.run_job(ex, self.spec(attempt=1))
        self.run_job(ex, self.spec(attempt=2))
        checks = self.calls("exec", "selfcheck")
        self.assertEqual(["--probe-egress" in c.args["argv"] for c in checks], [True, False])

    def test_environment_identity_in_executor_json(self):
        info = self.use_wheelhouse()
        ex = self.make()
        result, adir, verdict = self.run_job(ex, self.spec())
        self.assertEqual(verdict[0], "ok", result.stderr_tail)
        report = json.loads((adir / "run" / "selfcheck.json").read_text())
        env = result.facts["environment"]
        self.assertEqual(env["env_mode"], "wheelhouse")
        self.assertEqual(env["image_digest"], "sha256:" + "ab" * 32)
        self.assertEqual(env["requirements_sha256"], self.manifest["sandbox"]["wheelhouse"]["requirements_sha256"])
        self.assertEqual(env["sha256sums_sha256"], info["sha256sums_sha256"])
        self.assertEqual((env["freeze_sha256"], env["machine"], env["python"]),
                         (report["freeze_sha256"], report["machine"], report["python"]))
        self.assertGreaterEqual(result.facts["setup_seconds"], 0.0)
        self.assertEqual(result.facts["selfcheck"], "passed")
        self.assertIs(result.facts["isolated"], True)
        json.dumps(result.facts)                                   # executor.json is written from these facts as they are
        # image mode: no wheelhouse hash, the worker image's digest
        self.manifest["sandbox"].update(env_mode="image", image=IMAGE)
        ex2 = self.make()
        result2, _, _ = self.run_job(ex2, self.spec(attempt=2))
        env2 = result2.facts["environment"]
        self.assertEqual((env2["env_mode"], env2["image_digest"], env2["sha256sums_sha256"]),
                         ("image", "sha256:" + "0" * 64, None))
        self.assertEqual(result2.facts["setup_seconds"], 0.0)


# ======================================================================================== exit file and relay


class Relay(Base):
    def test_missing_exit_file_is_failure_even_with_exit_0(self):
        self.client.missing_files = {"/mhf/run/exit.json"}
        ex = self.make()
        result, adir, verdict = self.run_job(ex, self.spec())
        self.assertEqual(result.returncode, 0)                     # the EXIT frame said 0
        self.assertIsNone(result.exit_file)
        self.assertTrue(bundle.result_is_usable(adir / "out" / "val.json"))
        self.assertEqual(verdict, ("executor_error", True, None))
        self.assertEqual(result.facts["exit_file"], "absent")

    def test_malformed_exit_file_counts_do_not_crash_the_run(self):
        # review regression: an exit file rewritten inside the sandbox after the stub wrote it (a worker's escaped
        # child can do that) used to raise ValueError or TypeError out of run(); it must read as a failed attempt
        for label, local in (("string", {"relay_unavailable": "x"}), ("list", {"relay_unavailable": [1]})):
            with self.subTest(label):
                doc = {"schema": "mhforge.exit/1", "returncode": 0, "reason": "exit", "timed_out": False,
                       "local_refusals": local, "unanswered_at_exit": 0}

                def on_read(call, doc=doc):
                    if call.label == "/mhf/run/exit.json":
                        sb = self.client.sandboxes[call.sandbox_id]
                        (Path(sb.root) / "run" / "exit.json").write_text(json.dumps(doc))
                self.client.hooks["read_file"] = on_read
                ex = self.make()
                result, _, verdict = self.run_job(ex, self.spec(attempt=1 if label == "string" else 2))
                self.assertEqual(result.refusal_codes, {"relay_unavailable": 1})
                self.assertEqual(verdict, ("provider_unavailable", True, None))
                self.assertEqual(self.names()[-1], "stop")

    def test_stream_drop_reattaches_in_daemon_mode(self):
        self.client.stream_faults = [{"drop_after_frames": 3, "seed": 3}]
        ex = self.make()
        result, adir, verdict = self.run_job(ex, self.spec(SLOW_PLAN))
        self.assertEqual(verdict, ("ok", False, None), result.stderr_tail)
        self.assertEqual(len(self.calls("exec_stream", "bridge")), 2)
        self.assertEqual(result.facts["relay"]["attaches"], 2)
        self.assertEqual(result.facts["relay"]["bridge_ends"][0], "eof")
        self.assertEqual([r.get("status") for r in self.worker_state(adir)["results"]], [200] * 6)
        self.assertEqual(len(self.calls("exec", "serve")), 1)     # the stub and the worker were not restarted

    def test_silent_stall_reattaches_in_daemon_mode(self):
        self.manifest["relay"].update(ping_interval_s=1, pong_timeout_s=2)
        self.client.stream_faults = [{"stall_after_frames": 2, "stall_writes": "swallow"}]
        ex = self.make()
        plan = [{"op": "post", "n": 1}, {"op": "sleep", "s": 0.5}, {"op": "post", "n": 3, "gap": 1.0}, {"op": "val"}]
        result, adir, verdict = self.run_job(ex, self.spec(plan))
        self.assertEqual(verdict, ("ok", False, None), result.stderr_tail)
        self.assertGreaterEqual(len(self.calls("exec_stream", "bridge")), 2)
        self.assertEqual(result.facts["relay"]["bridge_ends"][0], "silent")
        self.assertEqual([r.get("status") for r in self.worker_state(adir)["results"]], [200] * 4)

    def test_failed_rotation_keeps_the_old_bridge(self):
        # review regression: a bridge exec that fails at rotation time used to end the attempt with ExecutorError
        # although the old bridge was alive; it is a bounded reattach failure and the old bridge keeps serving
        self.manifest["relay"]["rotate_after_mb"] = 0
        self.client.stream_faults = [{}, "fail"]
        ex = self.make()
        result, adir, verdict = self.run_job(ex, self.spec(SLOW_PLAN))
        self.assertEqual(verdict, ("ok", False, None), result.stderr_tail)
        self.assertGreaterEqual(result.facts["relay"]["attach_errors"], 1)
        self.assertGreaterEqual(result.facts["relay"]["rotations"], 1)
        self.assertEqual([r.get("status") for r in self.worker_state(adir)["results"]], [200] * 6)

    def test_reattach_exhausted_kills_stub_and_is_relay_lost(self):
        self.manifest["relay"]["reattach_attempts"] = 2
        self.client.stream_faults = [{"drop_after_frames": 2}, {"drop_after_frames": 0}, "fail"]
        alive_at_exit_read = []

        def on_read(call):
            if call.label == "/mhf/run/exit.json":
                sb = self.client.sandboxes[call.sandbox_id]
                pid = sb.stub_pid()
                alive_at_exit_read.append(pid is not None and _alive(pid))
        self.client.hooks["read_file"] = on_read
        ex = self.make()
        t0 = time.monotonic()
        result, _, verdict = self.run_job(ex, self.spec(HANG_PLAN, timeout_s=600))
        self.assertLess(time.monotonic() - t0, 60)                 # no wait for the 600 s deadline
        self.assertEqual(result.relay_end, "error")
        self.assertEqual(verdict, ("relay_lost", True, None))
        names = self.names()
        self.assertIn("exec:kill", names)
        self.assertLess(names.index("exec:kill"), names.index("read_file:/mhf/run/exit.json"))
        self.assertEqual(alive_at_exit_read, [False])              # the unary kill had ended the stub
        self.assertTrue(result.facts["unary_kill"])
        self.assertEqual(len(self.calls("exec_stream", "bridge")), 3)
        self.assertEqual(self.clock.sleeps.count(0.0), 2)          # one jittered wait per reattach

    def test_stream_drop_fails_attempt_in_stdio_mode(self):
        self.manifest["relay"]["mode"] = "stdio"
        self.client.stream_faults = [{"drop_after_frames": 2}]
        ex = self.make()
        t0 = time.monotonic()
        result, _, verdict = self.run_job(ex, self.spec(HANG_PLAN, timeout_s=600))
        self.assertLess(time.monotonic() - t0, 60)
        self.assertEqual(result.relay_end, "eof")
        self.assertEqual(verdict, ("relay_lost", True, None))
        streams = self.calls("exec_stream")
        self.assertEqual([c.label for c in streams], ["serve"])   # stdio never reattaches
        self.assertNotIn("exec:serve", self.names())
        self.assertLessEqual(streams[0].args["timeout_s"], 600 + 60)

    def test_stdio_mode_job_ok(self):
        self.manifest["relay"]["mode"] = "stdio"
        ex = self.make()
        result, _, verdict = self.run_job(ex, self.spec())
        self.assertEqual(verdict, ("ok", False, None), result.stderr_tail)
        self.assertEqual([c.label for c in self.calls("exec_stream")], ["serve"])
        self.assertEqual(self.calls("exec_stream")[0].args["timeout_s"], 120.0 + 60)

    def test_job_stream_budget_ends_the_attempt(self):
        self.manifest["relay"].update(max_job_stream_mb=0, rotate_after_mb=0)
        ex = self.make()
        result, _, verdict = self.run_job(ex, self.spec(HANG_PLAN, timeout_s=600))
        self.assertEqual(result.relay_end, "stream_budget")
        self.assertEqual(verdict, ("request_ceiling", False, "stream_budget"))

    def test_refusal_flood_kill_request_kills_the_worker(self):
        ex = self.make()
        self.gateway.request_kill("val.no_memory.USPTO.a1")
        t0 = time.monotonic()
        result, _, verdict = self.run_job(ex, self.spec(HANG_PLAN, timeout_s=600))
        self.assertLess(time.monotonic() - t0, 60)
        self.assertEqual(result.kill_reason, "refusal_flood")
        self.assertEqual(result.exit_file["reason"], "killed")
        self.assertEqual(verdict, ("request_ceiling", False, "refusal_flood"))

    def test_halt_gives_the_worker_grace_then_kills_it(self):
        ex = self.make()
        ex.halt_grace_s = 0.5
        self.gateway.set_halt("operator")
        result, _, _ = self.run_job(ex, self.spec(HANG_PLAN, timeout_s=600))
        self.assertEqual(result.kill_reason, "halt")
        self.assertEqual(classify_attempt(result, result_usable=False, halted=True)[0], "halted")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ======================================================================================== create, quota, budget, meter


class CreateAndMeter(Base):
    def test_create_retried_once_then_fails(self):
        self.client.stuck_creates = 2
        ex = self.make()
        with self.assertRaises(ExecutorError) as cm:
            self.run_job(ex, self.spec())
        self.assertEqual((cm.exception.kind, cm.exception.retryable), ("create", True))
        self.assertEqual(self.names(), ["create", "wait_running", "stop", "create", "wait_running", "stop"])
        rows = self.meter_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["status"] for r in rows], ["final", "final"])
        self.assertEqual(len({r["key"] for r in rows}), 2)

    def test_create_retried_once_then_succeeds(self):
        self.client.stuck_creates = 1
        ex = self.make()
        result, _, verdict = self.run_job(ex, self.spec())
        self.assertEqual(verdict[0], "ok", result.stderr_tail)
        self.assertEqual(len(result.facts["sandbox_ids"]), 2)
        self.assertEqual(result.facts["sandbox_id"], result.facts["sandbox_ids"][1])

    def test_quota_backoff(self):
        clock = helpers.FakeClock()
        self.client.create_errors = [QuotaExceeded("busy") for _ in range(3)]
        ex = self.make(clock)
        ex.poll_s = 0.05
        result, _, verdict = self.run_job(ex, self.spec())
        self.assertEqual(verdict[0], "ok", result.stderr_tail)
        self.assertEqual(clock.sleeps[:3], [5.0, 10.0, 20.0])
        self.assertEqual(self.names(ops=("create",)), ["create"] * 4)
        self.assertEqual(len(self.meter_rows()), 1)                # a refused create has no sandbox and no row
        # four refusals: quota, retryable, after exactly three waits
        self.client.calls.clear()
        clock.sleeps.clear()
        self.client.create_errors = [mapped_create_error(reason="CWSANDBOX_OCCUPANCY_QUOTA_EXCEEDED") for _ in range(4)]
        with self.assertRaises(ExecutorError) as cm:
            self.run_job(ex, self.spec(attempt=2))
        self.assertEqual((cm.exception.kind, cm.exception.retryable), ("quota", True))
        self.assertEqual(clock.sleeps, [5.0, 10.0, 20.0])
        self.assertEqual(self.names(), ["create"] * 4)
        self.assertEqual(len(self.meter_rows()), 1)

    def test_billing_error_is_terminal(self):
        for status, kind in (("RESOURCE_EXHAUSTED", "billing"), ("PERMISSION_DENIED", "permission"),
                             ("UNAUTHENTICATED", "auth")):
            with self.subTest(kind):
                self.client.calls.clear()
                self.client.create_errors = [mapped_create_error(status=status)]
                ex = self.make(helpers.FakeClock())
                with self.assertRaises(ExecutorError) as cm:
                    self.run_job(ex, self.spec())
                self.assertEqual((cm.exception.kind, cm.exception.retryable), (kind, False))
                self.assertEqual(self.names(), ["create"])         # no backoff, no second create
                self.assertEqual(self.meter_rows(), [])

    def test_throttle_reason_is_quota_not_billing(self):
        self.client.create_errors = [mapped_create_error(status="RESOURCE_EXHAUSTED",
                                                         reason="CWSANDBOX_SERVERLESS_CREATE_THROTTLED")]
        clock = helpers.FakeClock()
        ex = self.make(clock)
        result, _, verdict = self.run_job(ex, self.spec())
        self.assertEqual(verdict[0], "ok", result.stderr_tail)
        self.assertEqual(clock.sleeps[:1], [5.0])

    def test_stop_always_called_and_meter_row_closed(self):
        faults = (("pack", {"answers": {"pack": ExecResult(1, "", "boom", False)}}, ExecutorError),
                  ("extract", {"answers": {"extract": ExecResult(1, "", "refused", False)}}, ExecutorError),
                  ("upload", {"failing_uploads": {"/mhf/bundle.tar"}}, ExecutorError),
                  ("serve", {"answers": {"serve": ExecResult(2, "", "bad", False)}}, ExecutorError),
                  ("isolation", {"selfcheck_patch": {"egress": {"tcp": "open"}}}, CampaignStop))
        for label, faults_, exc in faults:
            with self.subTest(label):
                self.client.calls.clear()
                for k, v in faults_.items():
                    setattr(self.client, k, v)
                ex = self.make()
                with self.assertRaises(exc):
                    self.run_job(ex, self.spec())
                self.assertEqual(self.names()[-1], "stop", label)
                self.assertEqual(self.calls("ledger")[-1].label, "meter_close", label)
                self.assertEqual(self.ledger.open_meter_rows("sandbox_seconds"), [], label)
                self.assertTrue(all(sb.stopped for sb in self.client.sandboxes.values()))
                self.client.answers, self.client.failing_uploads, self.client.selfcheck_patch = {}, set(), {}
                if self.ledger.snapshot()["halt"]:
                    self.ledger.clear_halt("test")

    def test_meter_row_opened_at_create_with_worst_case(self):
        clock = helpers.FakeClock()
        seen = {}

        def at_handle(call):
            if call.op == "on_handle":
                seen["rows_at_handle"] = self.ledger.open_meter_rows("sandbox_seconds")

        def at_running(call):
            seen["rows_at_running"] = self.ledger.open_meter_rows("sandbox_seconds")
            seen["sid"] = call.sandbox_id
            clock.advance(12.5)                                    # the sandbox runs 12.5 s on the runner's clock
        self.client.hooks.update(on_handle=at_handle, wait_running=at_running)
        ex = self.make(clock)
        result, _, verdict = self.run_job(ex, self.spec())
        self.assertEqual(verdict[0], "ok", result.stderr_tail)
        self.assertEqual(seen["rows_at_handle"], [])                # on_handle first, at once
        (row,) = seen["rows_at_running"]
        lifetime_ms = (120 + 900) * 1000
        self.assertEqual(row["key"], seen["sid"])
        self.assertEqual(row["quantity"], lifetime_ms)
        self.assertEqual(row["micro_usd"], sandbox_cost_micro(lifetime_ms, "2", "4Gi", self.manifest))
        self.assertEqual(row["detail"], {"purpose": "job", "job_id": "val.no_memory.USPTO.a1"})
        (final,) = self.meter_rows()
        self.assertEqual(final["status"], "final")
        self.assertEqual(final["key"], seen["sid"])
        self.assertEqual(final["unit"], "ms")
        self.assertEqual(final["quantity"], result.facts["seconds"] * 1000)
        self.assertEqual(final["micro_usd"], sandbox_cost_micro(final["quantity"], "2", "4Gi", self.manifest))
        self.assertEqual(result.facts["cost_micro"], final["micro_usd"])

    def test_recreated_sandbox_has_its_own_meter_row(self):
        self.client.stuck_creates = 1
        ex = self.make()
        result, _, verdict = self.run_job(ex, self.spec())
        self.assertEqual(verdict[0], "ok", result.stderr_tail)
        ids = result.facts["sandbox_ids"]
        self.assertEqual([h["sandbox_id"] for h in self.handles], ids)
        rows = self.meter_rows()
        self.assertEqual(sorted(r["key"] for r in rows), sorted(ids))
        self.assertTrue(all(r["status"] == "final" for r in rows))
        self.assertEqual(self.names(ops=("stop",)), ["stop", "stop"])

    def test_sandbox_budget_checked_before_create(self):
        # a lifetime of 1,020 s at 2 CPU and 4 GiB is priced at 96,560 micro-dollars
        price = sandbox_cost_micro(1020 * 1000, "2", "4Gi", self.manifest)
        self.manifest["budgets"]["sandbox_usd"] = util.usd(price - 1)
        ex = self.make()
        with self.assertRaises(CampaignStop) as cm:
            self.run_job(ex, self.spec())
        self.assertEqual(cm.exception.reason, "budget:sandbox")
        self.assertEqual(self.names(), [])
        self.assertEqual(self.ledger.snapshot()["halt"]["reason"], "budget:sandbox")
        # spent plus the reservation of live sandboxes counts: exactly enough room passes once, not twice
        self.ledger.clear_halt("test")
        self.manifest["budgets"]["sandbox_usd"] = util.usd(price + 10)
        ex = self.make()
        self.ledger.meter_open("sandbox_seconds", "sbx-other", 1020 * 1000, "ms", worst_micro=price)
        with self.assertRaises(CampaignStop):
            self.run_job(ex, self.spec())
        self.assertEqual(self.names(), [])

    def test_parallel_runs_share_the_sandbox_budget(self):
        # review regression: two dataset jobs of one candidate run at once (executor.parallel_dataset_jobs); with room
        # for one lifetime only, the second must refuse even though the first has no meter row yet while it is created
        price = sandbox_cost_micro(1020 * 1000, "2", "4Gi", self.manifest)
        self.manifest["budgets"]["sandbox_usd"] = util.usd(price + price // 2)
        ex = self.make()
        create = self.client.create

        def slow_create(spec):
            time.sleep(0.5)
            return create(spec)
        self.client.create = slow_create
        out = {}

        def go(attempt):
            try:
                out[attempt] = self.run_job(ex, self.spec(attempt=attempt))[2][0]
            except CampaignStop as exc:
                out[attempt] = "stop:" + exc.reason
        threads = [threading.Thread(target=go, args=(a,)) for a in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        self.assertEqual(sorted(out.values()), ["ok", "stop:budget:sandbox"])
        self.assertEqual(len(self.meter_rows()), 1)
        self.assertEqual(ex._pending_micro, 0)

    def test_lifetime_includes_margin_and_setup_allowance(self):
        ex = self.make()
        self.assertEqual(ex.lifetime_s(7200), 7200 + 900)
        self.run_job(ex, self.spec(timeout_s=100))
        self.assertEqual(self.calls("create")[0].args["spec"].max_lifetime_s, 1000)
        self.use_wheelhouse()
        ex = self.make()
        self.assertEqual(ex.lifetime_s(7200), 8250)
        self.client.calls.clear()
        result, _, _ = self.run_job(ex, self.spec(attempt=2, timeout_s=100))
        self.assertEqual(self.calls("create")[0].args["spec"].max_lifetime_s, 100 + 900 + 150)
        row = [r for r in self.calls("ledger", "meter_open")][0]
        self.assertEqual(row.args["args"][2], (100 + 900 + 150) * 1000)
        self.assertEqual(result.facts["lifetime_s"], 1150)


# ======================================================================================== cleanup


class Cleanup(Base):
    def test_cleanup_closes_open_meter_rows(self):
        clock = helpers.FakeClock()
        ex = self.make(clock)
        worst = 1020 * 1000
        price = sandbox_cost_micro(worst, "2", "4Gi", self.manifest)
        self.ledger.meter_open("sandbox_seconds", "sbx-old", worst, "ms", worst_micro=price)   # died at its lifetime
        clock.advance(2000)
        self.ledger.meter_open("sandbox_seconds", "sbx-new", worst, "ms", worst_micro=price)   # still running
        clock.advance(30)
        n = ex.cleanup_orphans([{"sandbox_id": "sbx-old"}, {"sandbox_id": "sbx-new"}, {"pgid": 5}])
        self.assertEqual(n, 2)
        rows = {r["key"]: r for r in self.meter_rows()}
        self.assertEqual(rows["sbx-old"]["status"], "final")
        self.assertEqual(rows["sbx-old"]["quantity"], worst)
        self.assertEqual(rows["sbx-old"]["micro_usd"], price)
        ran_ms = int(round((30 + sum(clock.sleeps)) * 1000))      # opened 30 s before cleanup began, plus its pauses
        self.assertLess(ran_ms, worst)
        self.assertEqual(rows["sbx-new"]["quantity"], ran_ms)
        self.assertEqual(rows["sbx-new"]["micro_usd"], sandbox_cost_micro(ran_ms, "2", "4Gi", self.manifest))
        self.assertEqual(self.ledger.open_meter_rows("sandbox_seconds"), [])
        self.assertEqual([c.sandbox_id for c in self.calls("stop_id")], ["sbx-old", "sbx-new"])

    def test_cleanup_until_two_empty_lists(self):
        clock = helpers.FakeClock()
        ex = self.make(clock)
        self.client.list_script = [["a"], [], ["b", "c"], [], []]
        ex.cleanup_orphans([])
        lists = self.calls("list_ids")
        self.assertEqual(len(lists), 5)
        self.assertTrue(all(c.args["tags"] == ["mhf", "mhf-c-mh-test"] for c in lists))
        self.assertEqual([c.sandbox_id for c in self.calls("stop_id")], ["a", "b", "c"])
        # never two empty lists in a row: at most 10 rounds
        self.client.calls.clear()
        self.client.list_script = [["x"]] * 20
        ex.cleanup_orphans([])
        self.assertEqual(len(self.calls("list_ids")), 10)

    def test_cleanup_stops_a_real_leftover_sandbox(self):
        ex = self.make()
        sb = self.client.create(ex._spec(1020, ("mhf", "mhf-c-mh-test", "mhf-j-x", "mhf-a-1", "mhf-p-baseline")))
        other = self.client.create(ex._spec(1020, ("mhf", "mhf-c-other-campaign", "mhf-j-x", "mhf-a-1", "mhf-p-baseline")))
        ex.cleanup_orphans([])
        self.assertTrue(sb.stopped)
        self.assertFalse(other.stopped)


# ======================================================================================== import check


class ImportCheck(Base):
    def setUp(self):
        super().setUp()
        self.fake_tar = self.tmp / "fake_pkg.tar"
        build_fake_bundle(self.fake_tar)

    def test_import_check_shares_one_sandbox_and_is_metered(self):
        ex = self.make()
        work = self.state.paths.import_check_dir(1, 1)
        out = ex.import_check(["ok_agent", "foreign_class", "bad_import"], self.fake_tar, work, self.handles.append,
                              timeouts_s=[30, 31, 32])
        self.assertTrue(out["ok_agent"].ok, out["ok_agent"].stderr_tail)
        self.assertFalse(out["foreign_class"].ok)
        self.assertEqual(out["foreign_class"].reason, "foreign_class")
        self.assertFalse(out["bad_import"].ok)
        self.assertEqual(len(self.calls("create")), 1)
        self.assertEqual(len(self.handles), 1)
        checks = [c for c in self.calls("exec") if c.label.startswith("import_check:")]
        self.assertEqual([c.label for c in checks], ["import_check:ok_agent", "import_check:foreign_class",
                                                     "import_check:bad_import"])
        self.assertEqual([c.args["timeout_s"] for c in checks], [30.0, 31.0, 32.0])
        names = self.names()
        self.assertLess(names.index("exec:selfcheck"), names.index("exec:import_check:ok_agent"))
        self.assertEqual(names[-1], "stop")
        self.assertFalse(any(n in ("exec:serve", "exec_stream:bridge") for n in names))
        spec = self.calls("create")[0].args["spec"]
        self.assertEqual(spec.max_lifetime_s, 300 + 900)
        self.assertEqual((spec.deny_egress, spec.dns_egress, spec.env), (True, "deny", {"WANDB_API_KEY": ""}))
        self.assertEqual(len(spec.tags), 5)
        (row,) = self.meter_rows()
        self.assertEqual(row["status"], "final")
        self.assertEqual(json.loads(row["detail_json"]), {"purpose": "import_check"})
        opened = self.calls("ledger", "meter_open")[0]
        self.assertEqual(opened.args["args"][2], (300 + 900) * 1000)
        self.assertTrue((work / "selfcheck.json").is_file())
        checkfile = json.loads((work / "checkfile.json").read_text())
        self.assertEqual((checkfile["job_id"], checkfile["argv"]), ("import_check", []))

    def test_import_check_respects_each_limit(self):
        self.client.import_delay_s = {"ok_agent": 5.0}             # the check of ok_agent starts 5 s late
        ex = self.make()
        out = ex.import_check(["ok_agent", "foreign_class"], self.fake_tar, self.state.paths.import_check_dir(1, 1),
                              self.handles.append, timeouts_s=[1, 30])
        self.assertTrue(out["ok_agent"].timed_out)
        self.assertFalse(out["ok_agent"].ok)
        self.assertIsNone(out["ok_agent"].reason)
        self.assertEqual(out["foreign_class"].reason, "foreign_class")
        self.assertEqual(out["foreign_class"].returncode, 3)
        self.assertEqual(len(self.calls("create")), 1)

    def test_import_check_create_failure_raises_executor_error(self):
        for label, setup in (("create", lambda: setattr(self.client, "create_errors", [mapped_create_error()])),
                             ("stuck", lambda: setattr(self.client, "stuck_creates", 2)),
                             ("billing", lambda: setattr(self.client, "create_errors",
                                                         [mapped_create_error(status="RESOURCE_EXHAUSTED")]))):
            with self.subTest(label):
                self.client.calls.clear()
                setup()
                ex = self.make()
                with self.assertRaises(ExecutorError):
                    ex.import_check(["ok_agent"], self.fake_tar, self.state.paths.import_check_dir(1, 1),
                                    self.handles.append, timeouts_s=[30])
                self.assertFalse(any(c.label.startswith("import_check:") for c in self.calls("exec")))
                self.assertEqual(self.ledger.open_meter_rows("sandbox_seconds"), [])

    def test_import_check_budget_checked_before_create(self):
        price = sandbox_cost_micro((300 + 900) * 1000, "2", "4Gi", self.manifest)
        self.manifest["budgets"]["sandbox_usd"] = util.usd(price - 1)
        ex = self.make()
        with self.assertRaises(CampaignStop) as cm:
            ex.import_check(["ok_agent"], self.fake_tar, self.state.paths.import_check_dir(1, 1), self.handles.append,
                            timeouts_s=[30])
        self.assertEqual(cm.exception.reason, "budget:sandbox")
        self.assertEqual(self.calls("create"), [])


# ======================================================================================== wheelhouse


class Wheelhouse(Base):
    def test_wheelhouse_setup_sequence(self):
        info = self.use_wheelhouse()
        ex = self.make()
        result, _, verdict = self.run_job(ex, self.spec())
        self.assertEqual(verdict[0], "ok", result.stderr_tail)
        names = self.names(ops=("write_file", "exec"))
        i = names.index("exec:extract")
        self.assertEqual(names[i:i + 6], ["exec:extract", "write_file:/mhf/wheelhouse.tar", "exec:wheelhouse_size",
                                          "exec:wheelhouse_check", "exec:pip_install", "exec:selfcheck"])
        upload = self.calls("write_file", "/mhf/wheelhouse.tar")[0]
        self.assertEqual((upload.args["bytes"], upload.args["sha256"]), (info["tar_bytes"], info["tar_sha256"]))
        timeouts = {c.label: c.args["timeout_s"] for c in self.client.calls if c.op in ("exec", "write_file")}
        self.assertEqual(timeouts["/mhf/wheelhouse.tar"], 900.0)
        self.assertEqual(timeouts["wheelhouse_size"], 30.0)
        self.assertEqual(timeouts["wheelhouse_check"], 120.0)
        self.assertEqual(timeouts["pip_install"], 600.0)
        pip = self.calls("exec", "pip_install")[0].args["argv"]
        self.assertEqual(pip, ["python3", "-m", "pip", "install", "--no-index", "--find-links", "/mhf/wheelhouse/wheels",
                               "--require-hashes", "--no-deps", "-r", "/mhf/wheelhouse/requirements.lock.txt"])
        spec = self.calls("create")[0].args["spec"]
        self.assertEqual((spec.image, spec.fallback_image), (BASE, FALLBACK))
        self.assertGreaterEqual(result.facts["setup_seconds"], 0.0)

    def test_wheelhouse_failure_is_env_setup_and_next_attempt_starts_from_zero(self):
        cases = (("short_upload", {"short_uploads": {"/mhf/wheelhouse.tar"}}, False),
                 ("failing_upload", {"failing_uploads": {"/mhf/wheelhouse.tar"}}, False),
                 ("checksum", {}, True),
                 ("pip", {"pip_returncode": 1}, False))
        for label, faults, corrupt in cases:
            with self.subTest(label):
                self.client.calls.clear()
                self.gateway.calls.clear()
                self.use_wheelhouse(corrupt=corrupt)
                for k, v in faults.items():
                    setattr(self.client, k, v)
                ex = self.make()
                with self.assertRaises(ExecutorError) as cm:
                    self.run_job(ex, self.spec(attempt=1))
                self.assertEqual((cm.exception.kind, cm.exception.retryable), ("env_setup", True), label)
                names = self.names()
                self.assertNotIn("exec:selfcheck", names)
                self.assertNotIn("exec:serve", names)
                self.assertEqual(names[-1], "stop")
                self.assertEqual(self.gateway.calls, [])
                # the next attempt starts from zero in a new sandbox
                self.client.calls.clear()
                self.client.short_uploads, self.client.failing_uploads, self.client.pip_returncode = set(), set(), 0
                self.use_wheelhouse()
                ex = self.make()
                result, _, verdict = self.run_job(ex, self.spec(attempt=2))
                self.assertEqual(verdict[0], "ok", result.stderr_tail)
                names = self.names()
                self.assertEqual(names[:2], ["create", "wait_running"])
                self.assertEqual(self.calls("create")[0].args["spec"].tags[3], "mhf-a-2")
                for step in ("write_file:/mhf/wheelhouse.tar", "exec:wheelhouse_size", "exec:wheelhouse_check",
                             "exec:pip_install"):
                    self.assertEqual(names.count(step), 1, step)
                with ledger_mod.transaction(self.state.paths.ledger) as db:
                    db.execute("DELETE FROM meter_events")

    def test_wheelhouse_tar_on_host_must_match_the_manifest(self):
        self.use_wheelhouse()
        self.manifest["sandbox"]["wheelhouse"]["tar_sha256"] = "1" * 64
        ex = self.make()
        with self.assertRaises(ExecutorError) as cm:
            self.run_job(ex, self.spec())
        self.assertEqual(cm.exception.kind, "env_setup")
        self.assertNotIn("write_file:/mhf/wheelhouse.tar", self.names())

    def test_image_mode_skips_setup(self):
        ex = self.make()
        result, _, verdict = self.run_job(ex, self.spec())
        self.assertEqual(verdict[0], "ok", result.stderr_tail)
        names = self.names()
        for step in ("write_file:/mhf/wheelhouse.tar", "exec:wheelhouse_size", "exec:wheelhouse_check",
                     "exec:pip_install"):
            self.assertNotIn(step, names)
        self.assertEqual(names[names.index("exec:extract") + 1], "exec:selfcheck")
        self.assertEqual(result.facts["setup_seconds"], 0.0)
        spec = self.calls("create")[0].args["spec"]
        self.assertEqual((spec.image, spec.fallback_image), (IMAGE, None))

    def test_fallback_reference_used_in_wheelhouse_mode(self):
        # LD8: the pull failure surfaces from the wait (cwsandbox 1.27.0); the failed sandbox is stopped and its row
        # closed before the one re-create, which uses the fallback image
        self.use_wheelhouse()
        self.client.pull_fail_first = True
        ex = self.make()
        result, _, verdict = self.run_job(ex, self.spec())
        self.assertEqual(verdict[0], "ok", result.stderr_tail)
        first, second = self.calls("create")
        self.assertEqual((first.args["spec"].image, first.args["spec"].fallback_image), (BASE, FALLBACK))
        self.assertEqual((second.args["spec"].image, second.args["spec"].fallback_image), (FALLBACK, None))
        self.assertEqual(dataclasses_fields_except(first.args["spec"], second.args["spec"]), {"image", "fallback_image"})
        failed_id, ran_id = result.facts["sandbox_ids"]
        self.assertEqual(result.facts["sandbox_id"], ran_id)
        self.assertEqual(self.client.sandboxes[ran_id].image, FALLBACK)
        self.assertEqual((result.facts["image"], result.facts["image_fallback"]), (FALLBACK, True))
        self.assertEqual(result.facts["environment"]["image_digest"], "sha256:" + "ab" * 32)
        log = [(c.name, c.sandbox_id if c.op != "ledger" else (c.args["args"][1] if len(c.args["args"]) > 1 else None))
               for c in self.client.calls if c.op in ("create", "wait_running", "stop", "ledger")
               and c.name in ("create", "wait_running", "stop", "ledger:meter_open", "ledger:meter_close")]
        self.assertEqual(log[:7], [("create", None), ("ledger:meter_open", failed_id), ("wait_running", failed_id),
                                   ("stop", failed_id), ("ledger:meter_close", failed_id), ("create", None),
                                   ("ledger:meter_open", ran_id)])
        rows = {r["key"]: r for r in self.meter_rows()}
        self.assertEqual(set(rows), {failed_id, ran_id})
        self.assertTrue(all(r["status"] == "final" for r in rows.values()))
        self.assertTrue(self.client.sandboxes[failed_id].stopped)
        self.assertEqual([h["sandbox_id"] for h in self.handles], [failed_id, ran_id])
        # in mode image there is no fallback: the one re-create uses the same image, and a second failure is a
        # retryable create error, as before
        self.client.calls.clear()
        self.manifest["sandbox"].update(env_mode="image", image=IMAGE)
        self.client.pull_fail_images = {IMAGE}
        ex = self.make()
        with self.assertRaises(ExecutorError) as cm:
            self.run_job(ex, self.spec(attempt=2))
        self.assertEqual((cm.exception.kind, cm.exception.retryable), ("create", True))
        self.assertEqual([(c.args["spec"].image, c.args["spec"].fallback_image) for c in self.calls("create")],
                         [(IMAGE, None), (IMAGE, None)])
        self.assertEqual(self.names(), ["create", "wait_running", "stop", "create", "wait_running", "stop"])

    def test_second_pull_failure_on_the_fallback_is_an_infrastructure_failure(self):
        self.use_wheelhouse()
        self.client.pull_fail_images = {BASE, FALLBACK}
        ex = self.make()
        with self.assertRaises(ExecutorError) as cm:
            self.run_job(ex, self.spec())
        self.assertEqual((cm.exception.kind, cm.exception.retryable), ("create", True))
        self.assertEqual([c.args["spec"].image for c in self.calls("create")], [BASE, FALLBACK])
        self.assertEqual(self.names(), ["create", "wait_running", "stop", "create", "wait_running", "stop"])
        rows = self.meter_rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["status"] == "final" for r in rows))
        self.assertTrue(all(sb.stopped for sb in self.client.sandboxes.values()))
        self.assertEqual(ex._live, {})

    def test_a_wait_failure_that_is_not_a_pull_failure_recreates_the_same_image(self):
        self.use_wheelhouse()
        self.client.stuck_creates = 1
        ex = self.make()
        result, _, verdict = self.run_job(ex, self.spec())
        self.assertEqual(verdict[0], "ok", result.stderr_tail)
        self.assertEqual([(c.args["spec"].image, c.args["spec"].fallback_image) for c in self.calls("create")],
                         [(BASE, FALLBACK), (BASE, FALLBACK)])
        self.assertEqual((result.facts["image"], result.facts["image_fallback"]), (BASE, False))

    def test_no_fallback_create_while_the_failed_sandbox_may_still_run(self):
        self.use_wheelhouse()
        self.client.pull_fail_first = True
        self.client.failing_stops = 1
        ex = self.make()
        with self.assertRaises(ExecutorError) as cm:
            self.run_job(ex, self.spec())
        self.assertEqual((cm.exception.kind, cm.exception.retryable), ("create", True))
        self.assertIn("could not be stopped", str(cm.exception))
        self.assertEqual(self.names(), ["create", "wait_running", "stop"])           # no second sandbox
        (row,) = self.meter_rows()
        lifetime_ms = ex.lifetime_s(120) * 1000
        self.assertEqual((row["status"], row["quantity"]), ("open", lifetime_ms))   # held at the worst case
        self.assertEqual([h["sandbox_id"] for h in self.handles], [row["key"]])     # recorded for cleanup_orphans
        ex.cleanup_orphans(self.handles)
        (row,) = self.meter_rows()
        self.assertEqual(row["status"], "final")
        self.assertTrue(self.client.sandboxes[row["key"]].stopped)

    def test_pull_failure_classified_through_the_real_adapter_raise_path(self):
        # the executor reads the classification from what sandbox_client._CwHandle.wait_running really raises
        from mhforge.executors import sandbox as sandbox_mod
        from mhforge.executors import sandbox_client as sc
        from tests.fake_sandbox import SandboxFailedError

        class Sdk:
            sandbox_id = "sbx-1"

            def __init__(self, exc):
                self.exc = exc

            def wait(self, timeout=None):
                raise self.exc

        cases = ((SandboxFailedError("failed", sandbox_id="sbx-1", status_reason="ErrImagePull: x"), True),
                 (SandboxFailedError("failed", sandbox_id="sbx-1", status_reason="ImagePullBackOff: x"), True),
                 (SandboxFailedError("failed", sandbox_id="sbx-1", status_reason="OOMKilled: x"), False),
                 (SandboxFailedError("failed", sandbox_id="sbx-1"), False),
                 (TimeoutError("wait timed out"), False))
        for exc, pull in cases:
            with self.subTest(status_reason=getattr(exc, "status_reason", None), cls=type(exc).__name__):
                with self.assertRaises(ExecutorError) as cm:
                    sc._CwHandle(Sdk(exc), None).wait_running(180)
                self.assertEqual((cm.exception.kind, cm.exception.retryable), ("create", True))
                self.assertIs(sandbox_mod._pull_failed(cm.exception), pull)
        self.assertFalse(sandbox_mod._pull_failed(ExecutorError("fake sandbox stuck in creating", kind="create")))


# ======================================================================================== a real worker


@helpers.needs_example_venv
class VenvEndToEnd(unittest.TestCase):
    """Upstream's real worker (USPTO no_memory) inside FakeSandboxClient, through the relay, against StubProvider."""

    def test_sandbox_job_end_to_end_over_fake(self):
        from mhforge import workspace
        from mhforge.circuit import Circuit
        from mhforge.gateway import Gateway
        from mhforge.replay import ReplayStore
        from mhforge.stubprovider import StubProvider
        guard = util.loopback_guard()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        saved = ledger_mod.FULLFSYNC
        ledger_mod.FULLFSYNC = False
        self.addCleanup(setattr, ledger_mod, "FULLFSYNC", saved)
        ex_dir = helpers.example_dir()
        digest, _ = workspace.source_digest(ex_dir)
        ctx = helpers.tmp_state()
        st = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        m = helpers.make_manifest({"provider.kind": "stub", "provider.base_url": "http://127.0.0.1:9/v1",
                                   "upstream.tree_sha256": digest, "upstream.example_dir": str(ex_dir),
                                   "upstream.worker_python": str(helpers.example_python()),
                                   "sandbox.expect.machine": platform.machine(), "sandbox.expect.python": "3.13"})
        ws = workspace.ensure_workspace(m, st.paths, ex_dir, None)
        stub = StubProvider()
        c = m["provider"]["circuit"]
        ledger = ledger_mod.Ledger(st.paths.ledger, m)
        gw = Gateway(m, st.paths, provider=stub, ledger=ledger,
                     replay=ReplayStore(st.paths.replay, m["routing"]["reply_reuse_scope"]),
                     circuit=Circuit(st.paths.circuit, threshold=c["failures"], cooldown_s=c["cooldown_s"]),
                     rng=random.Random(1))
        gw.poll_s = 0.05
        tar = st.sc / "bundle.tar"
        info = bundle.build_bundle(ex_dir, ws, "USPTO", m, tar)
        sizes = m["upstream"]["expect"]["sizes"]["USPTO"]
        argv = ("--memory", "agents/no_memory.py", "--dataset", "USPTO", "--seed", "42",
                "--model", "openrouter/openai/gpt-oss-120b", "--mode", "offline", "--val-output", "{out}/val.json",
                "--save-memory", "{out}/memory.json", "--log", "{out}/log.jsonl", "--num-train", str(sizes[0]),
                "--num-val", str(sizes[1]), "--num-test", str(sizes[2]), "--temperature", "0.0")
        job = wire.JobIdentity("mh-test", "val", "no_memory", "USPTO", 1, "baseline", None)
        spec = JobSpec(job, argv, 2, 1800, (), ("val.json",), True, tar, info["sha256"])
        client = FakeSandboxClient(python=str(helpers.example_python()), fake_litellm=False, max_exec_s=900)
        self.addCleanup(client.close)
        ex = SandboxExecutor(m, st.paths, client=client, gateway=gw, ledger=ledger)
        ex.poll_s = 0.2
        adir = st.paths.attempt_dir(job.job_key, 1)
        result = ex.run(spec, adir, lambda h: None)
        usable = bundle.result_is_usable(adir / "out" / "val.json")
        self.assertEqual(classify_attempt(result, result_usable=usable, halted=False), ("ok", False, None),
                         result.stderr_tail)
        self.assertEqual(json.loads((adir / "out" / "val.json").read_text())["total"], sizes[1])
        self.assertEqual(len(stub.calls), sizes[1])
        self.assertEqual(result.gateway["completed"], sizes[1])
        self.assertIn("sidecar.jsonl", result.outputs)
        self.assertEqual(result.facts["environment"]["python"].split(".")[:2], ["3", "13"])
        self.assertEqual(ledger.open_meter_rows("sandbox_seconds"), [])
        self.assertEqual(ledger.verify_totals(), [])


if __name__ == "__main__":
    unittest.main()
