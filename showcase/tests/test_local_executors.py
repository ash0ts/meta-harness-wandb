# SPDX-License-Identifier: MIT
"""Tests for ``mhforge/executors/local.py`` and ``local_relay.py`` (spec 3.17, wait loop 3.16, named cases 5.2).

Most cases run a real worker process from a small bundle built here: either a scripted ``inner_loop.py`` (driven by a
JSON plan on its command line: post requests, sleep, spawn a grandchild, write val.json) or the stdlib stand-in
package ``tests/fixtures/fake_pkg``. The gateway is ``helpers.FakeGateway``; LocalExecutor reaches it through a real
``LoopbackIngress``, LocalRelayExecutor through the real relay stub. The ``needs_example_venv`` cases run upstream's
real worker on the example venv against a real Gateway with an in-process StubProvider.
"""

import hashlib
import inspect
import io
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from mhforge import bundle, util, wire
from mhforge import ledger as ledger_mod
from mhforge.errors import ExecutorError
from mhforge.executors import JobSpec, classify_attempt
from mhforge.executors.local import LocalExecutor
from mhforge.executors.local_relay import LocalRelayExecutor
from mhforge.ingress_http import LoopbackIngress
from mhforge.relay import PipeChannel
from tests import helpers
from tests.fake_channel import FaultChannel

INSANDBOX = helpers.SC / "mhforge" / "insandbox"
FAKE_PKG = helpers.FIXTURES / "fake_pkg" / "text_classification"

WORKER = r'''
import base64, hashlib, http.client, json, os, subprocess, sys, time, urllib.parse
args = sys.argv[1:]
def opt(name):
    return args[args.index(name) + 1] if name in args else None
plan, out, api_base, inp = json.loads(opt("--plan")), opt("--out"), opt("--api-base"), opt("--in")
base = api_base or os.environ.get("OPENROUTER_API_BASE", "")
key = "local" if api_base else os.environ.get("OPENROUTER_API_KEY", "")
state = {"argv": args, "env_names": sorted(os.environ), "results": [], "children": [], "inputs": {},
         "key_sha256": hashlib.sha256(key.encode()).hexdigest() if key else None}
def save():
    with open(os.path.join(out, "memory.json.tmp"), "w") as fh:
        json.dump(state, fh)
    os.replace(os.path.join(out, "memory.json.tmp"), os.path.join(out, "memory.json"))
def post(step, i):
    u = urllib.parse.urlsplit(base)
    size = step.get("size", 10)
    pad = base64.b64encode(os.urandom(size)).decode()[:size] if step.get("random") else "x" * size
    body = json.dumps({"model": "m", "messages": [{"role": "user", "content": "q%d %s" % (i, pad)}]}).encode()
    auth = {"key": "Bearer " + key, "none": None, "wrong": "Bearer mhfjob_wrong"}[step.get("auth", "key")]
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = auth
    res = {"i": i}
    try:
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=60)
        conn.request("POST", u.path + "/chat/completions", body=body, headers=headers)
        r = conn.getresponse()
        data = r.read()
        conn.close()
        res["status"] = r.status
        if r.status != 200:
            res["code"] = json.loads(data)["error"].get("code")
    except Exception as exc:
        res["error"] = type(exc).__name__
    state["results"].append(res)
save()
for step in plan:
    op = step["op"]
    if op == "post":
        for k in range(step.get("n", 1)):
            post(step, len(state["results"]))
            time.sleep(step.get("gap", 0))
    elif op == "burst":
        import threading
        base_i = len(state["results"])
        threads = [threading.Thread(target=post, args=(step, base_i + k)) for k in range(step["n"])]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    elif op == "sleep":
        time.sleep(step["s"])
    elif op == "spawn":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        state["children"].append(child.pid)
    elif op == "inputs":
        for name in sorted(os.listdir(inp)):
            with open(os.path.join(inp, name), "rb") as fh:
                state["inputs"][name] = hashlib.sha256(fh.read()).hexdigest()
    elif op == "val":
        with open(os.path.join(out, "val.json"), "w") as fh:
            json.dump({"accuracy": 0.5, "correct": 1, "total": 2}, fh)
    elif op == "exit":
        save()
        sys.exit(step["code"])
    save()
'''

FAKE_ARGV = ("--memory", "agents/ok_agent.py", "--dataset", "USPTO", "--seed", "42", "--mode", "offline",
             "--val-output", "{out}/val.json", "--save-memory", "{out}/memory.json", "--log", "{out}/log.jsonl",
             "--num-train", "4", "--num-val", "5", "--num-test", "6")
HANG = [{"op": "sleep", "s": 300}]
OK_PLAN = [{"op": "post", "n": 2}, {"op": "val"}]


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


def fake_pkg_files(**extra_agents: str) -> dict:
    files = {"__init__.py": b"", "data/__init__.py": (FAKE_PKG / "data.py").read_bytes()}
    for name in ("inner_loop.py", "llm.py", "memory_system.py"):
        files[name] = (FAKE_PKG / name).read_bytes()
    for path in sorted((FAKE_PKG / "agents").glob("*.py")):
        files["agents/" + path.name] = path.read_bytes()
    for name, src in extra_agents.items():
        files[f"agents/{name}.py"] = src.encode()
    return files


def wait_until(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def gone(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    return False


class Base(unittest.TestCase):
    overrides: dict = {}

    def setUp(self):
        guard = util.loopback_guard()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        ctx = helpers.tmp_state()
        self.state = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.tmp = self.state.sc / "t"
        (self.tmp / "tok").mkdir(parents=True)
        self.manifest = helpers.make_manifest({"upstream.worker_python": sys.executable, "relay.ping_interval_s": 5,
                                               "relay.pong_timeout_s": 30, **self.overrides})
        self.gateway = helpers.FakeGateway()
        self.script_tar = write_tar(self.tmp / "script.tar", {"__init__.py": b"", "inner_loop.py": WORKER.encode()})
        self.handles: list[dict] = []

    def make(self, kind, *, clock=None, **kw):
        common = dict(gateway=self.gateway, tokenizer_dir=self.tmp / "tok", clock=clock,
                      ids=helpers.SeqIds(clock or helpers.FakeClock()), **kw)
        if kind == "local":
            ingress = LoopbackIngress(self.gateway, self.manifest)
            self.addCleanup(ingress.close_all)
            ex = LocalExecutor(self.manifest, self.state.paths, ingress=ingress, **common)
        else:
            ex = LocalRelayExecutor(self.manifest, self.state.paths, **common)
        ex.poll_s, ex.term_wait_s = 0.05, 2.0
        return ex

    def spec(self, plan=None, *, attempt=1, timeout_s=120, route=2, capture=False, inner_argv=None, tar=None,
             inputs=()):
        job = wire.JobIdentity("mh-test", "val", "no_memory", "USPTO", attempt, "baseline", None)
        if inner_argv is None:
            inner_argv = ("--plan", json.dumps(plan or []), "--out", "{out}", "--in", "{in}")
            if route == 1:
                inner_argv += ("--api-base", "{api_base}")
        tar = tar or self.script_tar
        return JobSpec(identity=job, inner_argv=tuple(inner_argv), route=route, timeout_s=timeout_s,
                       inputs=tuple(inputs), expect_outputs=("val.json",), capture=capture, bundle_tar=tar,
                       bundle_sha256=util.sha256_file(tar))

    def run_job(self, ex, spec, on_started=None):
        attempt_dir = self.state.paths.attempt_dir(spec.identity.job_key, spec.identity.attempt)

        def on_handle(h):
            self.handles.append(h)
            if on_started is not None and h.get("purpose") in ("worker", "stub"):
                threading.Thread(target=on_started, daemon=True).start()

        result = ex.run(spec, attempt_dir, on_handle)
        return result, attempt_dir

    @staticmethod
    def worker_state(attempt_dir):
        return json.loads((attempt_dir / "out" / "memory.json").read_text())

    @staticmethod
    def classify(result, attempt_dir, halted=False):
        usable = bundle.result_is_usable(attempt_dir / "out" / "val.json")
        return classify_attempt(result, result_usable=usable, halted=halted)


class LocalExecutorCases(Base):
    def test_local_timeout_kills_process_group(self):
        ex = self.make("local")
        result, adir = self.run_job(ex, self.spec([{"op": "spawn"}] + HANG, timeout_s=2))
        child = self.worker_state(adir)["children"][0]
        self.assertTrue(result.timed_out)
        self.assertEqual((result.exit_file["reason"], result.exit_file["returncode"]), ("timeout", 124))
        self.assertTrue(wait_until(lambda: gone(child)), "the worker's grandchild survived the timeout")
        with self.assertRaises(ProcessLookupError):
            os.killpg(result.facts["pgid"], 0)
        self.assertEqual(self.classify(result, adir), ("timeout", False, None))

    def test_job_token_in_env_not_argv_or_job_file(self):
        ex = self.make("local")
        result, adir = self.run_job(ex, self.spec(OK_PLAN))
        job_id = "val.no_memory.USPTO.a1"
        token = self.gateway.tokens[job_id]
        self.assertRegex(token, r"mhfjob_[A-Za-z0-9_-]{43}")
        st = self.worker_state(adir)
        self.assertEqual(st["key_sha256"], hashlib.sha256(token.encode()).hexdigest())   # the worker had it, in env
        self.assertIn("OPENROUTER_API_KEY", st["env_names"])
        self.assertFalse(any(token in a for a in st["argv"]))
        self.assertEqual([r["status"] for r in st["results"]], [200, 200])
        jobfile = json.loads((adir / "jobfile.json").read_text())
        self.assertNotIn("OPENROUTER_API_KEY", jobfile["env"])
        self.assertFalse(jobfile["isolated"])
        for path in [p for p in self.state.root.rglob("*") if p.is_file()]:
            self.assertNotIn(token.encode(), path.read_bytes(), str(path))
        self.assertNotIn(token, json.dumps(self.handles))
        self.assertEqual(self.classify(result, adir), ("ok", False, None))

    def test_inputs_are_copied_and_route1_gets_the_endpoint(self):
        src = self.tmp / "memory.json"
        src.write_text('{"m": 1}')
        ex = self.make("local")
        result, adir = self.run_job(ex, self.spec([{"op": "inputs"}] + OK_PLAN, route=1,
                                                  inputs=(("memory.json", src),)))
        st = self.worker_state(adir)
        self.assertEqual(st["inputs"], {"memory.json": util.sha256_file(src)})
        self.assertEqual([r["status"] for r in st["results"]], [200, 200])   # Bearer local, the port is the job
        self.assertIsNone(self.gateway.tokens["val.no_memory.USPTO.a1"])
        self.assertNotIn("{api_base}", " ".join(st["argv"]))
        self.assertEqual(self.classify(result, adir), ("ok", False, None))

    def test_host_sleep_detected_by_heartbeat(self):
        self.manifest["runner"]["sleep_gap_s"] = 60
        clock = helpers.FakeClock()
        ex = self.make("local", clock=clock)

        def scenario():
            time.sleep(0.4)
            clock.advance_wall(3600)                  # the laptop slept: the monotonic clock stood still
            time.sleep(0.4)
            clock.advance(101)                        # then the job's own limit passes
        result, adir = self.run_job(ex, self.spec(HANG, timeout_s=100), on_started=scenario)
        self.assertTrue(result.timed_out)
        self.assertAlmostEqual(result.host_sleep_s, 3600.0, delta=1.0)
        self.assertEqual(self.classify(result, adir), ("interrupted", True, "host_sleep"))

    def test_image_problem_refuses_before_any_worker(self):
        ingress = LoopbackIngress(self.gateway, self.manifest)
        ex = LocalExecutor(self.manifest, self.state.paths, gateway=self.gateway, ingress=ingress)  # no tokenizer dir
        with self.assertRaises(ExecutorError) as cm:
            ex.run(self.spec(OK_PLAN), self.tmp / "a", self.handles.append)
        self.assertEqual((cm.exception.kind, cm.exception.retryable), ("image_mismatch", False))
        self.assertEqual((self.handles, self.gateway.registered, self.gateway.calls), ([], {}, []))


class BothExecutors(Base):
    overrides = {"relay.ping_interval_s": 500, "relay.pong_timeout_s": 1000}

    def test_exit_zero_without_result_is_no_result(self):
        for kind in ("local", "local_relay"):
            with self.subTest(kind):
                self.gateway = helpers.FakeGateway()
                result, adir = self.run_job(self.make(kind), self.spec([{"op": "post"}, {"op": "exit", "code": 0}]))
                self.assertEqual((result.returncode, result.exit_file["reason"]), (0, "exit"))
                self.assertNotIn("val.json", result.outputs)
                self.assertEqual(self.classify(result, adir), ("no_result", False, None))
                shutil.rmtree(self.state.paths.jobs_root)

    def test_halt_ends_job_within_grace(self):
        for kind in ("local", "local_relay"):
            with self.subTest(kind):
                self.gateway = helpers.FakeGateway()
                clock = helpers.FakeClock()
                ex = self.make(kind, clock=clock)
                done, seen = threading.Event(), {}

                def scenario():
                    time.sleep(0.4)
                    self.gateway.set_halt("budget:search")
                    time.sleep(0.5)                   # the loop has seen the halt at fake time 0
                    clock.advance(30)
                    time.sleep(0.6)
                    seen["ended_inside_grace"] = done.is_set()
                    clock.advance(31)
                result, adir = self.run_job(ex, self.spec(HANG, timeout_s=600), on_started=scenario)
                done.set()
                self.assertFalse(seen["ended_inside_grace"])
                self.assertEqual(result.kill_reason, "halt")
                self.assertFalse(result.timed_out)
                self.assertEqual(self.classify(result, adir, halted=True), ("halted", True, None))
                shutil.rmtree(self.state.paths.jobs_root)

    def test_kill_requested_ends_job(self):
        for kind in ("local", "local_relay"):
            with self.subTest(kind):
                self.gateway = helpers.FakeGateway()
                ex = self.make(kind)

                def scenario():
                    time.sleep(0.4)
                    self.gateway.request_kill("val.no_memory.USPTO.a1")
                t0 = time.monotonic()
                result, adir = self.run_job(ex, self.spec([{"op": "post"}] + HANG), on_started=scenario)
                self.assertLess(time.monotonic() - t0, 60)
                self.assertEqual(result.kill_reason, "refusal_flood")
                self.assertEqual(result.exit_file["reason"], "killed")
                self.assertEqual(self.classify(result, adir), ("request_ceiling", False, "refusal_flood"))
                shutil.rmtree(self.state.paths.jobs_root)

    def test_command_prefix_is_used(self):
        wrapper = self.tmp / "wrap.py"
        wrapper.write_text("import os, sys\nwith open(sys.argv[1], 'a') as fh:\n"
                           "    fh.write(' '.join(sys.argv[2:]) + '\\n')\nos.execv(sys.argv[2], sys.argv[2:])\n")
        for kind, needle in (("local", "-m text_classification.inner_loop"), ("local_relay", "mhf_relay_stub.py serve")):
            with self.subTest(kind):
                marker = self.tmp / f"marker-{kind}.txt"
                ex = self.make(kind, command_prefix=(sys.executable, str(wrapper), str(marker)))
                self.gateway = ex.gateway = helpers.FakeGateway()
                if kind == "local":
                    ex.ingress = LoopbackIngress(self.gateway, self.manifest)
                result, adir = self.run_job(ex, self.spec(OK_PLAN))
                self.assertIn(needle, marker.read_text())
                self.assertEqual(self.classify(result, adir), ("ok", False, None))
                shutil.rmtree(self.state.paths.jobs_root)

    def test_cleanup_kills_only_matching_process(self):
        ex = self.make("local_relay")
        env = helpers.child_env()
        procs = [subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], env=env,
                                  start_new_session=True) for _ in range(2)]
        try:
            match = ex._handle(procs[0].pid)
            reused = ex._handle(procs[1].pid)
            reused["proc_start"] = reused["proc_start"] - 120.0      # recorded for an older process of that pid
            sock_dir = tempfile.mkdtemp(prefix="mhf-", dir="/tmp")
            dead = {"pgid": 999_999_9, "t_start": 0.0, "sock_dir": sock_dir}
            self.assertEqual(ex.cleanup_orphans([match, reused, dead, {"pgid": True}, "junk"]), 1)
            self.assertEqual(procs[0].wait(timeout=10), -15)
            self.assertIsNone(procs[1].poll())
            self.assertFalse(os.path.exists(sock_dir))
        finally:
            for p in procs:
                if p.poll() is None:
                    p.kill()
                    p.wait()


class RelayCases(Base):
    def test_no_gateway_listener_in_relay_mode(self):
        self.assertNotIn("ingress", inspect.signature(LocalRelayExecutor.__init__).parameters)
        listens = []
        real_listen = socket.socket.listen

        def spy(sock, *a):
            listens.append(sock.getsockname())
            return real_listen(sock, *a)
        ex = self.make("local_relay")
        with mock.patch.object(socket.socket, "listen", spy):
            result, adir = self.run_job(ex, self.spec(OK_PLAN))
        self.assertEqual(listens, [])
        self.assertIsNone(self.gateway.tokens["val.no_memory.USPTO.a1"])
        self.assertEqual([c["ingress"] for c in self.gateway.calls], ["relay", "relay"])
        self.assertEqual(self.classify(result, adir), ("ok", False, None))

    def test_stub_listener_refuses_without_bearer(self):
        ex = self.make("local_relay")
        plan = [{"op": "post", "auth": "none"}, {"op": "post", "auth": "wrong"}, {"op": "post"}, {"op": "val"}]
        result, adir = self.run_job(ex, self.spec(plan))
        st = self.worker_state(adir)
        self.assertEqual([(r["status"], r.get("code")) for r in st["results"]],
                         [(403, "relay_unavailable"), (403, "relay_unavailable"), (200, None)])
        self.assertEqual(len(self.gateway.calls), 1)
        self.assertEqual(result.exit_file["local_refusals"].get("relay_unavailable"), 0)   # not counted against it
        summary = [json.loads(ln) for ln in (adir / "run" / "stub.txt").read_text().splitlines()
                   if '"summary"' in ln][-1]
        self.assertEqual(summary["unauthorized"], 2)
        bearer = helpers.SeqIds().token()                       # the executor's first (and only) token draw
        self.assertEqual(st["key_sha256"], hashlib.sha256(bearer.encode()).hexdigest())
        for path in [p for p in self.state.root.rglob("*") if p.is_file()]:
            self.assertNotIn(bearer.encode(), path.read_bytes(), str(path))
        self.assertEqual(self.classify(result, adir), ("ok", False, None))

    def test_job_stream_budget_is_the_jobs_ceiling(self):
        self.manifest["relay"].update(max_job_stream_mb=0.05, rotate_after_mb=0.05)
        plan = [{"op": "post", "n": 30, "size": 8000, "random": True, "gap": 0.1}, {"op": "val"}]
        for mode in ("stdio", "daemon"):
            with self.subTest(mode):
                self.gateway = helpers.FakeGateway()
                result, adir = self.run_job(self.make("local_relay", mode=mode), self.spec(plan))
                self.assertEqual(result.relay_end, "stream_budget")
                self.assertEqual(result.exit_file["reason"], "killed")
                self.assertLess(len(self.gateway.calls), 30)
                self.assertEqual(self.classify(result, adir), ("request_ceiling", False, "stream_budget"))
                shutil.rmtree(self.state.paths.jobs_root)

    def test_local_relay_with_fake_pkg_end_to_end(self):
        tar = write_tar(self.tmp / "fake.tar", fake_pkg_files())
        result, adir = self.run_job(self.make("local_relay"), self.spec(inner_argv=FAKE_ARGV, capture=True, tar=tar))
        self.assertEqual(set(result.outputs), {"val.json", "memory.json", "log.jsonl", "sidecar.jsonl"})
        self.assertEqual(json.loads((adir / "out" / "val.json").read_text())["total"], 5)
        self.assertGreaterEqual(len(self.gateway.calls), 5)
        self.assertTrue(all(c["ingress"] == "relay" and c["status"] == 200 for c in self.gateway.calls))
        self.assertEqual((result.relay_end, result.exit_file["reason"], result.exit_file["unanswered_at_exit"]),
                         ("exit", "exit", 0))
        self.assertEqual((result.facts["relay"]["mode"], result.facts["relay"]["attaches"]), ("stdio", 1))
        self.assertFalse(json.loads((adir / "run" / "selfcheck.json").read_text())["isolated"])
        self.assertEqual(self.classify(result, adir), ("ok", False, None))

    def test_local_relay_daemon_mode_with_rotation(self):
        self.manifest["relay"].update(rotate_after_mb=0.001, reattach_backoff_s=[0, 0])
        tar = write_tar(self.tmp / "fake.tar", fake_pkg_files())
        ex = self.make("local_relay", mode="daemon")
        result, adir = self.run_job(ex, self.spec(inner_argv=FAKE_ARGV, capture=True, tar=tar))
        relay = result.facts["relay"]
        self.assertEqual(relay["mode"], "daemon")
        self.assertGreaterEqual(relay["rotations"], 1)
        self.assertGreaterEqual(relay["attaches"], relay["rotations"] + 1)
        self.assertEqual(json.loads((adir / "out" / "val.json").read_text())["total"], 5)
        self.assertEqual(self.classify(result, adir), ("ok", False, None))
        stub = [h for h in self.handles if h.get("purpose") == "stub"][0]
        self.assertTrue(stub["sock_dir"].startswith("/tmp/mhf-") and len(stub["sock_dir"]) < 40)
        self.assertFalse(os.path.exists(stub["sock_dir"]))
        self.assertTrue(wait_until(lambda: gone(stub["pid"])))

    def test_import_check_with_fake_pkg(self):
        tar = write_tar(self.tmp / "fake.tar", fake_pkg_files(slow="import time\ntime.sleep(60)\n"))
        for kind in ("local", "local_relay"):
            with self.subTest(kind):
                handles = []
                ex = self.make(kind)
                out = ex.import_check(["ok_agent", "bad_import", "foreign_class", "slow"], tar, self.tmp / kind,
                                      handles.append, timeouts_s=[30, 30, 30, 1])
                self.assertEqual([out[n].ok for n in ("ok_agent", "bad_import", "foreign_class", "slow")],
                                 [True, False, False, False])
                self.assertEqual(out["foreign_class"].reason, "foreign_class")
                self.assertTrue(out["slow"].timed_out)
                self.assertIsNone(out["slow"].returncode)
                self.assertEqual(len(handles), 4)
                self.assertTrue(all(isinstance(h["pgid"], int) and "t_start" in h for h in handles))


class RelayContractCases(Base):
    """Contracts of 3.17 that no named case covers: the per-exec stdio budget, daemon reattach, flood refusals."""

    def test_stdio_exec_stream_budget_is_the_jobs_ceiling(self):
        self.manifest["relay"].update(max_stream_mb=0.05, max_job_stream_mb=1024)
        plan = [{"op": "post", "n": 30, "size": 8000, "random": True, "gap": 0.1}, {"op": "val"}]
        result, adir = self.run_job(self.make("local_relay"), self.spec(plan))
        self.assertEqual(result.relay_end, "stream_budget")
        self.assertEqual(result.exit_file["reason"], "killed")
        self.assertLess(len(self.gateway.calls), 30)
        self.assertEqual(self.classify(result, adir), ("request_ceiling", False, "stream_budget"))

    def test_daemon_reattaches_after_a_dropped_bridge(self):
        self.manifest["relay"].update(reattach_backoff_s=[0, 0])
        tar = write_tar(self.tmp / "fake.tar", fake_pkg_files())
        ex = self.make("local_relay", mode="daemon")
        made = []

        def factory(proc):
            made.append(proc)
            if len(made) == 1:
                return FaultChannel(proc, drop_after_frames=3)       # HELLO and two requests, then cut
            return PipeChannel(proc, mimic_sdk=True)
        ex.channel_factory = factory
        result, adir = self.run_job(ex, self.spec(inner_argv=FAKE_ARGV, capture=True, tar=tar))
        self.assertGreaterEqual(len(made), 2)
        self.assertEqual(result.facts["relay"]["rotations"], 0)
        self.assertGreaterEqual(result.facts["relay"]["attaches"], 2)
        self.assertEqual(result.relay_end, "exit")
        self.assertEqual(json.loads((adir / "out" / "val.json").read_text())["total"], 5)
        self.assertEqual(self.classify(result, adir), ("ok", False, None))
        self.assertTrue(all(p.poll() is not None for p in made))      # every bridge process was reaped

    def test_daemon_reattach_exhausted_is_relay_lost_without_waiting(self):
        self.manifest["relay"].update(reattach_attempts=2, reattach_backoff_s=[0, 0])
        ex = self.make("local_relay", mode="daemon")
        made = []

        def factory(proc):
            made.append(proc)
            return FaultChannel(proc, drop_after_frames=0)           # every bridge dies inside its HELLO
        ex.channel_factory = factory
        t0 = time.monotonic()
        result, adir = self.run_job(ex, self.spec(HANG, timeout_s=600))
        self.assertLess(time.monotonic() - t0, 60)                     # not the 600 s job limit
        self.assertEqual(len(made), 3)                                 # the first attach and two reattaches
        self.assertEqual(result.relay_end, "error")
        self.assertFalse(result.timed_out)
        self.assertEqual(self.classify(result, adir), ("relay_lost", True, None))
        stub = [h for h in self.handles if h.get("purpose") == "stub"][0]
        self.assertTrue(wait_until(lambda: gone(stub["pid"])), "the stub outlived an exhausted reattach")
        self.assertFalse(os.path.exists(stub["sock_dir"]))

    def test_daemon_kill_is_resent_on_a_new_bridge(self):
        self.manifest["relay"].update(ping_interval_s=1, pong_timeout_s=2, reattach_backoff_s=[0, 0])
        ex = self.make("local_relay", mode="daemon")
        made = []

        def factory(proc):
            ch = FaultChannel(proc)
            made.append(ch)
            return ch
        ex.channel_factory = factory

        def scenario():
            time.sleep(0.6)
            made[0].block_writes = True                    # the KILL written to bridge 1 never arrives
            self.gateway.request_kill("val.no_memory.USPTO.a1")
        t0 = time.monotonic()
        result, adir = self.run_job(ex, self.spec([{"op": "post"}] + HANG), on_started=scenario)
        self.assertLess(time.monotonic() - t0, 25)             # not the 30 s wait for an unconfirmed KILL
        self.assertGreaterEqual(len(made), 2)
        self.assertEqual(result.kill_reason, "refusal_flood")
        self.assertEqual(result.exit_file["reason"], "killed")  # the stub ended the worker on the resent KILL
        self.assertEqual(self.classify(result, adir), ("request_ceiling", False, "refusal_flood"))

    def test_stub_killed_abnormally_leaves_no_worker(self):
        # review regression: the stub starts its worker in a session of its own, so a stub that dies by SIGKILL
        # used to leave the worker (and its children) running after the job had ended
        import signal
        ex = self.make("local_relay")

        def scenario():
            time.sleep(1.0)
            stub = [h for h in self.handles if h.get("purpose") == "stub"][0]
            os.kill(stub["pid"], signal.SIGKILL)
        result, adir = self.run_job(ex, self.spec([{"op": "spawn"}] + HANG, timeout_s=600), on_started=scenario)
        worker = [json.loads(ln)["pid"] for ln in (adir / "run" / "stub.txt").read_text().splitlines()
                  if '"worker_started"' in ln]
        child = self.worker_state(adir)["children"]
        self.assertEqual((len(worker), len(child)), (1, 1))
        try:
            self.assertTrue(wait_until(lambda: gone(worker[0]) and gone(child[0]), 5),
                            "the worker or its child outlived a stub that died by SIGKILL")
        finally:
            for pid in worker + child:
                if not gone(pid):
                    os.kill(pid, signal.SIGKILL)
        self.assertEqual(result.relay_end, "eof")
        self.assertEqual(self.classify(result, adir), ("relay_lost", True, None))

    def test_flood_refusals_count_as_relay_overloaded(self):
        self.manifest["relay"]["max_workers"] = 1                        # at most 4 live requests per session
        self.gateway = helpers.FakeGateway(delay_s=0.5)
        plan = [{"op": "burst", "n": 10}, {"op": "val"}]
        result, adir = self.run_job(self.make("local_relay"), self.spec(plan))
        codes = [r.get("code") for r in self.worker_state(adir)["results"]]
        refused = codes.count("relay_overloaded")
        self.assertGreaterEqual(refused, 1)
        self.assertEqual(result.facts["relay"]["flood_refusals"], refused)
        self.assertEqual(result.refusal_codes.get("relay_overloaded"), refused)
        self.assertEqual(len(self.gateway.calls), 10 - refused)       # a refused request never reached the gateway
        self.assertEqual(self.classify(result, adir), ("request_ceiling", False, "relay_overloaded"))

    def test_local_exit_file_is_written_by_the_executor(self):
        result, adir = self.run_job(self.make("local"), self.spec([{"op": "exit", "code": 3}]))
        doc = json.loads((adir / "run" / "exit.json").read_text())
        self.assertEqual((doc["schema"], doc["returncode"], doc["reason"], doc["timed_out"]),
                         ("mhforge.exit/1", 3, "exit", False))
        self.assertNotIn("local_refusals", doc)
        self.assertNotIn("unanswered_at_exit", doc)
        self.assertIs(doc["isolated"], False)
        self.assertEqual((result.relay_end, result.facts["isolated"]), (None, False))
        self.assertIs(json.loads((adir / "run" / "selfcheck.json").read_text())["isolated"], False)
        self.assertEqual(self.gateway.closed, ["val.no_memory.USPTO.a1"])
        self.assertEqual(self.classify(result, adir)[0], "worker_error")


@helpers.needs_example_venv
class VenvCases(unittest.TestCase):
    """Upstream's real worker (USPTO no_memory) on the example venv against StubProvider."""

    def setUp(self):
        from mhforge import workspace
        guard = util.loopback_guard()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        saved = ledger_mod.FULLFSYNC
        ledger_mod.FULLFSYNC = False
        self.addCleanup(setattr, ledger_mod, "FULLFSYNC", saved)
        self.ex = helpers.example_dir()
        self.digest, _ = workspace.source_digest(self.ex)
        self.workspace = workspace

    def rig(self):
        from mhforge.circuit import Circuit
        from mhforge.gateway import Gateway
        from mhforge.replay import ReplayStore
        from mhforge.stubprovider import StubProvider
        ctx = helpers.tmp_state()
        st = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        m = helpers.make_manifest({"provider.kind": "stub", "provider.base_url": "http://127.0.0.1:9/v1",
                                   "upstream.tree_sha256": self.digest, "upstream.example_dir": str(self.ex),
                                   "upstream.worker_python": str(helpers.example_python())})
        ws = self.workspace.ensure_workspace(m, st.paths, self.ex, None)
        stub = StubProvider()
        c = m["provider"]["circuit"]
        gw = Gateway(m, st.paths, provider=stub, ledger=ledger_mod.Ledger(st.paths.ledger, m),
                     replay=ReplayStore(st.paths.replay, m["routing"]["reply_reuse_scope"]),
                     circuit=Circuit(st.paths.circuit, threshold=c["failures"], cooldown_s=c["cooldown_s"]),
                     rng=random.Random(1))
        gw.poll_s = 0.05
        tar = st.sc / "bundle.tar"
        info = bundle.build_bundle(self.ex, ws, "USPTO", m, tar)
        sizes = m["upstream"]["expect"]["sizes"]["USPTO"]
        argv = ("--memory", "agents/no_memory.py", "--dataset", "USPTO", "--seed", "42",
                "--model", "openrouter/openai/gpt-oss-120b", "--mode", "offline", "--val-output", "{out}/val.json",
                "--save-memory", "{out}/memory.json", "--log", "{out}/log.jsonl", "--num-train", str(sizes[0]),
                "--num-val", str(sizes[1]), "--num-test", str(sizes[2]), "--temperature", "0.0")

        def spec(attempt=1):
            job = wire.JobIdentity("mh-test", "val", "no_memory", "USPTO", attempt, "baseline", None)
            return JobSpec(job, argv, 2, 1800, (), ("val.json",), True, tar, info["sha256"])
        return st, m, gw, stub, spec, ws, ReplayStore

    def run_job(self, ex, st, spec):
        adir = st.paths.attempt_dir(spec.identity.job_key, spec.identity.attempt)
        result = ex.run(spec, adir, lambda h: None)
        usable = bundle.result_is_usable(adir / "out" / "val.json")
        return result, adir, classify_attempt(result, result_usable=usable, halted=False)

    @staticmethod
    def comparable(adir):
        doc = json.loads((adir / "out" / "val.json").read_text())
        return {k: v for k, v in doc.items() if k not in ("timestamp", "runtime_seconds")}

    def local(self, st, m, gw):
        ingress = LoopbackIngress(gw, m)
        self.addCleanup(ingress.close_all)
        return LocalExecutor(m, st.paths, gateway=gw, ingress=ingress)

    def test_local_job_ok(self):
        st, m, gw, stub, spec, _, _ = self.rig()
        result, adir, verdict = self.run_job(self.local(st, m, gw), st, spec())
        self.assertEqual(verdict, ("ok", False, None), result.stderr_tail)
        self.assertEqual(self.comparable(adir)["total"], 30)
        self.assertEqual(len(stub.calls), 30)              # the second validation pass is the worker's own cache
        self.assertEqual(result.gateway["completed"], 30)
        self.assertIn("sidecar.jsonl", result.outputs)

    def test_local_relay_job_same_result(self):
        st1, m1, gw1, _, spec1, _, _ = self.rig()
        _, adir1, verdict1 = self.run_job(self.local(st1, m1, gw1), st1, spec1())
        st2, m2, gw2, stub2, spec2, _, _ = self.rig()
        result2, adir2, verdict2 = self.run_job(LocalRelayExecutor(m2, st2.paths, gateway=gw2), st2, spec2())
        self.assertEqual((verdict1, verdict2), (("ok", False, None),) * 2, result2.stderr_tail)
        self.assertEqual(self.comparable(adir1), self.comparable(adir2))
        self.assertEqual(len(stub2.calls), 30)

    def test_local_relay_stream_drop_then_rerun_is_free(self):
        st, m, gw, stub, spec, _, _ = self.rig()
        ex = LocalRelayExecutor(m, st.paths, gateway=gw)
        ex.channel_factory = lambda proc: FaultChannel(proc, drop_after_frames=11)   # HELLO + 10 requests
        result1, _, verdict1 = self.run_job(ex, st, spec(1))
        self.assertEqual(verdict1, ("relay_lost", True, None))
        self.assertEqual(result1.exit_file["reason"], "uplink_lost")
        self.assertLessEqual(len(stub.calls), 10)
        del ex.channel_factory
        _, _, verdict2 = self.run_job(ex, st, spec(2))
        self.assertEqual(verdict2, ("ok", False, None))
        self.assertEqual(len(stub.calls), 30)              # attempt 2 replayed what attempt 1 had paid for

    def test_import_check_ok_and_fail(self):
        st, m, gw, _, _, ws, _ = self.rig()
        (ws / "agents" / "broken_cand.py").write_text("raise ImportError('broken on purpose')\n")
        tar = st.sc / "check.tar"
        bundle.build_bundle(self.ex, ws, "USPTO", m, tar)
        handles = []
        out = self.local(st, m, gw).import_check(["no_memory", "broken_cand"], tar, st.paths.import_check_dir(1, 1),
                                                 handles.append, timeouts_s=[120, 120])
        self.assertTrue(out["no_memory"].ok, out["no_memory"].stderr_tail)
        self.assertFalse(out["broken_cand"].ok)
        self.assertIn("ImportError", out["broken_cand"].stderr_tail)
        self.assertEqual(len(handles), 2)


if __name__ == "__main__":
    unittest.main()
