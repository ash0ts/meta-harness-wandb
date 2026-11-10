# SPDX-License-Identifier: MIT
"""Tests for ``mhforge/insandbox/mhf_relay_stub.py`` (spec 3.13, frames 2.9, exit file 2.6, named cases 5.2).

The real stub runs as a subprocess of ``sys.executable``; a scripted host talks MHF1 to it over its stdio pipes, over
the daemon's Unix socket, or through the stub's own ``bridge`` command. The worker is a small stdlib script written
into each test's temp directory and driven by a JSON plan. Temp directories live under ``/tmp`` so that the daemon
socket path stays under the 90-byte limit. The test process runs under ``util.loopback_guard()``.
"""

import importlib.util
import json
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mhforge import util, wire
from mhforge.insandbox import mhf_frames
from tests import helpers

INSANDBOX = helpers.SC / "mhforge" / "insandbox"
STUB = INSANDBOX / "mhf_relay_stub.py"
CHAT = "/v1/chat/completions"
PLATFORM_NAMES = {"__CF_USER_TEXT_ENCODING"} if sys.platform == "darwin" else set()


def load_stub_module():
    sys.path.insert(0, str(INSANDBOX))
    try:
        spec = importlib.util.spec_from_file_location("mhf_relay_stub_under_test", STUB)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(INSANDBOX))


stub_mod = load_stub_module()

WORKER_SRC = r'''
import http.client, json, os, socket, subprocess, sys, threading, time, urllib.parse
plan, out = json.loads(sys.argv[1]), sys.argv[2]
argv = sys.argv[3:]
base = argv[argv.index("--api-base") + 1] if "--api-base" in argv else os.environ.get("OPENROUTER_API_BASE", "")
state = {"pid": os.getpid(), "argv": sys.argv, "env": dict(os.environ), "results": [], "pids": [os.getpid()]}
with open(out + ".pid", "w") as fh:
    fh.write(str(os.getpid()))
lock = threading.Lock()

def save():
    with lock:
        tmp = out + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh)
        os.replace(tmp, out)

def bearer(step):
    auth = step.get("auth", "env")
    if auth == "env":
        return "Bearer " + os.environ.get("OPENROUTER_API_KEY", "local")
    return auth

def post(step, i):
    u = urllib.parse.urlsplit(base)
    body = json.dumps({"i": i, "pad": "x" * step.get("size", 10)}).encode()
    headers = {"Content-Type": "application/json"}
    if bearer(step):
        headers["Authorization"] = bearer(step)
    headers.update(step.get("headers", {}))
    try:
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=step.get("timeout", 60))
        if step.get("chunked"):
            parts = [body[k:k + 7] for k in range(0, len(body), 7)]
            conn.request(step.get("method", "POST"), u.path + step.get("suffix", "/chat/completions"),
                         body=iter(parts), headers=headers, encode_chunked=True)
        else:
            conn.request(step.get("method", "POST"), u.path + step.get("suffix", "/chat/completions"), body=body,
                         headers=headers)
        r = conn.getresponse()
        data = r.read()
        res = {"i": i, "status": r.status, "body": data.decode(), "headers": {k.lower(): v for k, v in r.getheaders()}}
        conn.close()
    except Exception as exc:
        res = {"i": i, "error": type(exc).__name__}
    with lock:
        state["results"].append(res)

save()
for step in plan:
    op = step["op"]
    if op == "exit":
        save()
        sys.exit(step.get("code", 0))
    elif op == "sleep":
        time.sleep(step["s"])
    elif op == "spam":
        for k in range(step.get("n", 1000)):
            print("spam-line %d #MHF1 HELLO 0 - 1 4 00000000" % k)
            sys.stderr.write("spam-err %d\n" % k)
        sys.stdout.flush()
    elif op == "group":
        child = subprocess.Popen(["/bin/sleep", "60"])
        state["pids"].append(child.pid)
        save()
    elif op == "post":
        threads = []
        for k in range(step.get("n", 1)):
            t = threading.Thread(target=post, args=(step, step.get("start", 0) + k))
            t.start()
            threads.append(t)
            if step.get("stagger"):
                time.sleep(step["stagger"])
        for t in threads:
            t.join()
    elif op == "hold":
        u = urllib.parse.urlsplit(base)
        s = socket.create_connection((u.hostname, u.port))
        s.sendall(b"POST " + u.path.encode() + b"/chat/completions HTTP/1.1\r\nHost: x\r\nAuthorization: "
                  + bearer(step).encode() + b"\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
        holder = subprocess.Popen(["/bin/sleep", "30"], pass_fds=[s.fileno()])
        state["pids"].append(holder.pid)
        save()
        time.sleep(1.5)
        os._exit(0)
    save()
save()
'''


def echo_responder(frame):
    _, rid, meta, body = frame
    try:
        i = json.loads(body)["i"]
    except (ValueError, KeyError, TypeError):
        i = None
    return 200, [["content-type", "application/json"], ["x-mhforge-source", "provider"]], \
        helpers.completion_body("echo-%s" % i)


class Host:
    """A scripted host: decodes MHF1 from the stub, records every frame, answers REQ through a responder."""

    def __init__(self, read_fn, write_fn, close_fn, responder=None):
        self._read, self._write, self._close = read_fn, write_fn, close_fn
        self.decoder = mhf_frames.FrameDecoder()
        self.frames = []
        self.cond = threading.Condition()
        self.responder = responder
        self.wlock = threading.Lock()
        self.ended = False

    def start(self):
        threading.Thread(target=self._reader, daemon=True).start()
        return self

    def _reader(self):
        while True:
            try:
                data = self._read(65536)
            except OSError:
                data = b""
            if not data:
                with self.cond:
                    self.ended = True
                    self.cond.notify_all()
                return
            text = data.decode("utf-8", errors="replace")
            for frame in self.decoder.feed(text):
                with self.cond:
                    self.frames.append(frame)
                    self.cond.notify_all()
                if frame[0] == "REQ" and self.responder is not None:
                    threading.Thread(target=self._answer, args=(frame,), daemon=True).start()

    def _answer(self, frame):
        reply = self.responder(frame)
        if reply is not None:
            status, headers, body = reply
            try:
                self.send("RES", frame[1], {"status": status, "headers": headers}, body)
            except OSError:
                pass

    def send(self, ftype, rid="0", meta=None, body=b""):
        data = mhf_frames.encode_frame(ftype, rid, meta or {}, body)
        with self.wlock:
            self._write(data)

    def close(self):
        try:
            self._close()
        except OSError:
            pass

    def expect(self, ftype, rid=None, timeout=20.0, nth=1):
        end = time.monotonic() + timeout
        while True:
            hits = self.of_type(ftype, rid)
            if len(hits) >= nth:
                return hits[nth - 1]
            if time.monotonic() > end:
                raise AssertionError("no %s #%d (rid %s); got %s" % (ftype, nth, rid, [(f[0], f[1]) for f in self.frames]))
            with self.cond:
                self.cond.wait(0.1)

    def of_type(self, ftype, rid=None):
        with self.cond:
            return [f for f in self.frames if f[0] == ftype and (rid is None or f[1] == rid)]


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


def pipe_host(proc, responder=None, start=True):
    out_fd, in_fd = proc.stdout.fileno(), proc.stdin.fileno()
    host = Host(lambda n: os.read(out_fd, n), lambda b: _write_all(in_fd, b), proc.stdin.close, responder)
    return host.start() if start else host


def socket_host(path, responder=None):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(str(path))

    def close():
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    host = Host(sock.recv, sock.sendall, close, responder)
    host.sock = sock
    return host.start()


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        return done == 0
    except ChildProcessError:
        return True


def wait_dead(pid, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


class StubCase(unittest.TestCase):
    def setUp(self):
        guard = util.loopback_guard()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        self.tmp = Path(tempfile.mkdtemp(prefix="mhf-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(self._kill_all)
        self.run_dir = self.tmp / "run"
        self.out = self.tmp / "out.json"
        self.worker = self.tmp / "worker.py"
        self.worker.write_text(WORKER_SRC, encoding="utf-8")
        self.procs = []
        self.daemon_pids = []

    def _kill_all(self):
        for pid in self.daemon_pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        for proc in self.procs:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait(5)
            for stream in (proc.stdin, proc.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        pidfile = Path(str(self.out) + ".pid")
        if pidfile.exists():
            try:
                os.killpg(int(pidfile.read_text()), signal.SIGKILL)
            except (OSError, ValueError):
                pass

    # ------------------------------------------------------------------ builders

    def job(self, plan, *, route=2, timeout_s=60, reply_timeout_s=30, ping=60, pong=120, max_request_bytes=4000000,
            argv_extra=(), port=0):
        job = {"schema": "mhforge.jobfile/1", "job_id": "val.no_memory.USPTO.a1",
               "argv": [sys.executable, str(self.worker), json.dumps(plan), str(self.out), *argv_extra],
               "env": {"PATH": "/usr/bin:/bin", "HOME": str(self.tmp), "TMPDIR": str(self.tmp),
                       "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "LANG": "C.UTF-8",
                       "NO_PROXY": "127.0.0.1,localhost", "JOB_ONLY_MARKER": "yes"},
               "cwd": str(self.tmp), "route": route, "port": port, "timeout_s": timeout_s,
               "reply_timeout_s": reply_timeout_s, "ping_interval_s": ping, "pong_timeout_s": pong,
               "max_request_bytes": max_request_bytes, "run_dir": str(self.run_dir), "expect_outputs": [],
               "vocab_file": None, "repeat": None, "sample": None, "probe_endpoints": [],
               "expect": {"machine": None, "python": None, "freeze_sha256": None}}
        path = self.tmp / "job.json"
        path.write_text(json.dumps(job), encoding="utf-8")
        self.job_doc = job
        return path

    def stub_env(self, bearer=None, **extra):
        env = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8", "STUB_ONLY_MARKER": "leak"}
        if bearer is not None:
            env["MHF_STUB_BEARER"] = bearer
        env.update(extra)
        return env

    def start_stdio(self, job, responder=None, bearer=None, start_host=True):
        self.run_dir.mkdir(exist_ok=True)
        err = open(self.run_dir / "stub-stderr.txt", "wb")
        self.addCleanup(err.close)
        proc = subprocess.Popen([sys.executable, str(STUB), "serve", "--job", str(job), "--uplink", "stdio"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, bufsize=0,
                                env=self.stub_env(bearer), start_new_session=True)
        self.procs.append(proc)
        return proc, pipe_host(proc, responder, start=start_host)

    def sock_path(self):
        return self.tmp / "u.sock"

    def sock_host(self, responder=None):
        host = socket_host(self.sock_path(), responder=responder)
        self.addCleanup(host.close)
        return host

    def start_daemon(self, job, *, daemonize=False, linger=30):
        args = [sys.executable, str(STUB), "serve", "--job", str(job), "--uplink", "unix:%s" % self.sock_path(),
                "--linger", str(linger)]
        if daemonize:
            args.append("--daemonize")
        self.run_dir.mkdir(exist_ok=True)
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                env=self.stub_env(), start_new_session=True)
        self.procs.append(proc)
        if daemonize:
            self.assertEqual(proc.wait(25), 0)
        ready = self.wait_file(self.run_dir / "stub.ready")
        doc = json.loads(ready.read_text())
        self.daemon_pids.append(doc["pid"])
        return proc, doc

    def start_bridge(self, responder=None):
        proc = subprocess.Popen([sys.executable, str(STUB), "bridge", "--uplink", "unix:%s" % self.sock_path()],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                                env=self.stub_env(), start_new_session=True)
        self.procs.append(proc)
        return proc, pipe_host(proc, responder)

    def wait_file(self, path, timeout=20.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if Path(path).exists():
                return Path(path)
            time.sleep(0.05)
        self.fail("file %s did not appear" % path)

    def results(self):
        return json.loads(self.out.read_text())

    def exit_file(self):
        return json.loads((self.run_dir / "exit.json").read_text())

    def worker_pids(self, n=2, timeout=20.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.out.exists():
                pids = self.results().get("pids", [])
                if len(pids) >= n:
                    return pids
            time.sleep(0.05)
        self.fail("worker did not record its pids")


# ======================================================================================================== stdio

class StdioTests(StubCase):
    def test_hello_then_exit_with_child_code(self):
        proc, host = self.start_stdio(self.job([{"op": "exit", "code": 7}]))
        hello = host.expect("HELLO")
        self.assertIs(host.frames[0], hello, "HELLO is the first frame")
        meta = hello[2]
        self.assertEqual(meta["proto"], 1)
        self.assertEqual(meta["mode"], "stdio")
        self.assertEqual(meta["pid"], proc.pid)
        self.assertGreater(meta["port"], 0)
        self.assertEqual(meta["pending"], [])
        self.assertEqual(meta["worker"], {"state": "running", "returncode": None})
        ex = host.expect("EXIT")[2]
        self.assertEqual(ex, {"returncode": 7, "reason": "exit", "timed_out": False,
                              "local_refusals": {"relay_unavailable": 0, "request_too_large": 0},
                              "unanswered_at_exit": 0})
        self.assertEqual(proc.wait(20), 0)
        record = self.exit_file()
        self.assertEqual(record["schema"], "mhforge.exit/1")
        for key, value in ex.items():
            self.assertEqual(record[key], value)
        self.assertLessEqual(record["t_start"], record["t_end"])

    def test_relays_94_concurrent_posts(self):
        n = 94
        all_in = threading.Event()

        def responder(frame):
            if len(host.of_type("REQ")) >= n:
                all_in.set()
            all_in.wait(40)
            time.sleep(random.random() * 0.05)
            return echo_responder(frame)

        proc, host = self.start_stdio(self.job([{"op": "post", "n": n, "timeout": 60}]), responder=responder)
        ex = host.expect("EXIT", timeout=60)[2]
        self.assertEqual(ex["returncode"], 0)
        self.assertTrue(all_in.is_set(), "all 94 requests were pending at the host at once")
        reqs = host.of_type("REQ")
        self.assertEqual(len({f[1] for f in reqs}), n)
        results = self.results()["results"]
        self.assertEqual(len(results), n)
        for res in results:
            self.assertEqual(res.get("status"), 200, res)
            self.assertEqual(json.loads(res["body"])["choices"][0]["message"]["content"], "echo-%d" % res["i"])
        self.assertEqual(ex["unanswered_at_exit"], 0)
        self.assertEqual(proc.wait(20), 0)

    def test_child_stdout_never_on_frame_channel(self):
        proc, host = self.start_stdio(self.job([{"op": "spam", "n": 3000}, {"op": "exit", "code": 0}]))
        host.expect("EXIT", timeout=30)
        self.assertEqual(proc.wait(20), 0)
        time.sleep(0.1)
        self.assertEqual(host.decoder.noise_chars, 0)
        self.assertEqual(host.decoder.errors, 0)
        self.assertEqual([f[0] for f in host.frames], ["HELLO", "EXIT"])
        out = (self.run_dir / "stdout.txt").read_text()
        err = (self.run_dir / "stderr.txt").read_text()
        self.assertIn("spam-line 2999", out)
        self.assertIn("spam-err 2999", err)

    def test_route1_api_base_substitution(self):
        proc, host = self.start_stdio(self.job([{"op": "post", "n": 1, "auth": "Bearer local"}], route=1,
                                               argv_extra=["--api-base", "{api_base}"]), responder=echo_responder)
        port = host.expect("HELLO")[2]["port"]
        host.expect("EXIT")
        self.assertEqual(proc.wait(20), 0)
        state = self.results()
        base = "http://127.0.0.1:%d/v1" % port
        self.assertEqual(state["argv"][-2:], ["--api-base", base])
        self.assertNotIn("{api_base}", state["argv"])
        self.assertNotIn("OPENROUTER_API_BASE", state["env"])
        self.assertNotIn("OPENROUTER_API_KEY", state["env"])
        self.assertEqual(state["results"][0]["status"], 200)
        self.assertEqual(host.expect("REQ")[2], {"method": "POST", "path": CHAT})

    def test_worker_env_is_only_job_file(self):
        proc, host = self.start_stdio(self.job([{"op": "exit", "code": 0}], argv_extra=["{api_base}"]))
        port = host.expect("HELLO")[2]["port"]
        host.expect("EXIT")
        self.assertEqual(proc.wait(20), 0)
        state = self.results()
        env = {k: v for k, v in state["env"].items() if k not in PLATFORM_NAMES}
        want = dict(self.job_doc["env"])
        want["OPENROUTER_API_BASE"] = "http://127.0.0.1:%d/v1" % port
        want["OPENROUTER_API_KEY"] = wire.RELAY_API_KEY_PLACEHOLDER
        self.assertEqual(env, want)
        self.assertNotIn("STUB_ONLY_MARKER", state["env"])
        self.assertEqual(state["argv"][-1], "{api_base}", "Route 2 leaves the literal argument alone")

    def test_stdin_eof_kills_child_writes_exit_file_exits_75(self):
        proc, host = self.start_stdio(self.job([{"op": "group"}, {"op": "sleep", "s": 60}]))
        host.expect("HELLO")
        pids = self.worker_pids(2)
        host.close()
        self.assertEqual(proc.wait(30), 75)
        self.assertEqual(self.exit_file()["reason"], "uplink_lost")
        for pid in pids:
            self.assertTrue(wait_dead(pid), "pid %d still alive" % pid)
        self.assertEqual(host.of_type("EXIT"), [])

    def test_stub_exits_cleanly_no_abort(self):
        proc, host = self.start_stdio(self.job([{"op": "post", "n": 5}, {"op": "exit", "code": 0}]),
                                      responder=echo_responder)
        host.expect("EXIT", timeout=30)
        rc = proc.wait(20)
        self.assertEqual(rc, 0)
        self.assertNotEqual(rc, -6)
        err = (self.run_dir / "stub-stderr.txt").read_bytes()
        self.assertNotIn(b"Fatal Python error", err)
        self.assertNotIn(b"Traceback", err)

    def test_reply_timeout_gives_relay_unavailable(self):
        proc, host = self.start_stdio(self.job([{"op": "post", "n": 1}], reply_timeout_s=1))
        rid = host.expect("REQ")[1]
        host.expect("CAN", rid=rid, timeout=10)
        ex = host.expect("EXIT", timeout=20)[2]
        res = self.results()["results"][0]
        self.assertEqual(res["status"], 403)
        self.assertEqual(json.loads(res["body"])["error"]["code"], "relay_unavailable")
        helpers.assert_neutral(wire.GatewayReply(res["status"], tuple(res["headers"].items()), res["body"].encode()))
        self.assertEqual(ex["local_refusals"]["relay_unavailable"], 1)
        self.assertEqual(ex["unanswered_at_exit"], 0)
        self.assertEqual(proc.wait(20), 0)

    def test_oversize_body_refused_locally(self):
        plan = [{"op": "post", "n": 1, "size": 5000}, {"op": "post", "n": 1, "size": 10, "start": 1}]
        proc, host = self.start_stdio(self.job(plan, max_request_bytes=1000), responder=echo_responder)
        ex = host.expect("EXIT", timeout=20)[2]
        results = sorted(self.results()["results"], key=lambda r: r["i"])
        self.assertEqual(results[0]["status"], 403)
        self.assertEqual(json.loads(results[0]["body"])["error"]["code"], "request_too_large")
        self.assertEqual(results[1]["status"], 200)
        reqs = host.of_type("REQ")
        self.assertEqual(len(reqs), 1, "the oversize body was never relayed")
        self.assertEqual(json.loads(reqs[0][3])["i"], 1)
        self.assertEqual(ex["local_refusals"], {"relay_unavailable": 0, "request_too_large": 1})
        self.assertEqual(proc.wait(20), 0)

    def test_local_refusals_equal_wire_table(self):
        self.assertEqual(set(stub_mod.LOCAL_REFUSALS), {"relay_unavailable", "request_too_large"})
        for code, (status, body) in stub_mod.LOCAL_REFUSALS.items():
            self.assertEqual(status, wire.REFUSALS[code][0])
            self.assertEqual(body, wire.refusal_body(code))
            self.assertEqual(tuple(stub_mod.REFUSAL_HEADERS), wire.refusal_reply(code).headers)
            helpers.assert_neutral(wire.GatewayReply(status, tuple(stub_mod.REFUSAL_HEADERS), body))
        self.assertEqual(stub_mod.RELAY_API_KEY_PLACEHOLDER, wire.RELAY_API_KEY_PLACEHOLDER)
        self.assertIn(wire.SOURCE_HEADER, stub_mod.FORWARD_HEADERS)

    def test_job_timeout_kills_process_group_rc_124(self):
        proc, host = self.start_stdio(self.job([{"op": "group"}, {"op": "sleep", "s": 60}], timeout_s=2))
        pids = self.worker_pids(2)
        ex = host.expect("EXIT", timeout=30)[2]
        self.assertEqual(ex["returncode"], 124)
        self.assertEqual(ex["reason"], "timeout")
        self.assertTrue(ex["timed_out"])
        for pid in pids:
            self.assertTrue(wait_dead(pid), "pid %d of the worker's group still alive" % pid)
        record = self.exit_file()
        self.assertEqual((record["returncode"], record["reason"], record["timed_out"]), (124, "timeout", True))
        self.assertGreaterEqual(record["t_end"] - record["t_start"], 1.5)
        self.assertEqual(proc.wait(20), 0)

    def test_only_method_path_body_leave_the_stub(self):
        plan = [{"op": "post", "n": 1, "auth": "Bearer worker-secret-xyz", "suffix": "/chat/completions?x=query-zz",
                 "headers": {"X-Custom-Secret": "header-secret-abc", "Cookie": "cookie-secret-qq"}}]
        seen = []

        def responder(frame):
            seen.append(frame)
            return echo_responder(frame)

        proc, host = self.start_stdio(self.job(plan), responder=responder)
        host.expect("EXIT", timeout=20)
        self.assertEqual(proc.wait(20), 0)
        req = host.expect("REQ")
        self.assertEqual(req[2], {"method": "POST", "path": CHAT})
        self.assertEqual(json.loads(req[3]), {"i": 0, "pad": "x" * 10})
        blob = json.dumps([[f[0], f[1], f[2], f[3].decode("latin-1")] for f in host.frames])
        for needle in ("worker-secret-xyz", "header-secret-abc", "cookie-secret-qq", "query-zz"):
            self.assertNotIn(needle, blob)
            for name in ("stub.txt", "exit.json"):
                self.assertNotIn(needle, (self.run_dir / name).read_text())
        self.assertEqual(self.results()["results"][0]["status"], 200)

    def test_chunked_body_is_relayed_and_bounded(self):
        plan = [{"op": "post", "n": 1, "size": 50, "chunked": True},
                {"op": "post", "n": 1, "size": 5000, "chunked": True, "start": 1}]
        proc, host = self.start_stdio(self.job(plan, max_request_bytes=1000), responder=echo_responder)
        ex = host.expect("EXIT", timeout=20)[2]
        results = sorted(self.results()["results"], key=lambda r: r["i"])
        self.assertEqual([r["status"] for r in results], [200, 403])
        reqs = host.of_type("REQ")
        self.assertEqual(len(reqs), 1)
        self.assertEqual(json.loads(reqs[0][3]), {"i": 0, "pad": "x" * 50})
        self.assertEqual(ex["local_refusals"]["request_too_large"], 1)
        self.assertEqual(proc.wait(20), 0)

    def test_res_frame_status_body_and_three_headers_reach_worker(self):
        def responder(frame):
            return 201, [["Content-Type", "application/json"], ["x-should-retry", "false"],
                         ["x-mhforge-source", "replay"], ["x-other", "dropped"], ["set-cookie", "no"]], b'{"ok":1}'

        proc, host = self.start_stdio(self.job([{"op": "post", "n": 1}]), responder=responder)
        host.expect("EXIT", timeout=20)
        res = self.results()["results"][0]
        self.assertEqual(res["status"], 201)
        self.assertEqual(res["body"], '{"ok":1}')
        self.assertEqual(res["headers"]["content-type"], "application/json")
        self.assertEqual(res["headers"]["x-should-retry"], "false")
        self.assertEqual(res["headers"]["x-mhforge-source"], "replay")
        self.assertEqual(res["headers"]["content-length"], "8")
        self.assertNotIn("x-other", res["headers"])
        self.assertNotIn("set-cookie", res["headers"])
        self.assertEqual(proc.wait(20), 0)

    def test_pong_answers_ping_and_reader_never_writes(self):
        proc, host = self.start_stdio(self.job([{"op": "sleep", "s": 60}]), start_host=False)
        n = 4000
        wrote = threading.Event()

        def writer():
            for k in range(n):
                host.send("PING", "0", {"t": k + 0.5})
            wrote.set()

        threading.Thread(target=writer, daemon=True).start()
        # Nobody reads the stub's stdout yet: its PONGs (about 300 KB) fill the pipe. A stub whose reader wrote the
        # PONG itself would stop reading stdin, and these writes would block.
        self.assertTrue(wrote.wait(30), "the stub stopped reading while its output pipe was full")
        host.start()
        end = time.monotonic() + 30
        while len(host.of_type("PONG")) < n and time.monotonic() < end:
            time.sleep(0.05)
        pongs = host.of_type("PONG")
        self.assertEqual(len(pongs), n)
        self.assertEqual([f[2]["t"] for f in pongs], [k + 0.5 for k in range(n)])
        self.assertEqual(host.frames[0][0], "HELLO")
        host.send("KILL", "0", {"reason": "test"})
        host.expect("EXIT", timeout=20)
        self.assertEqual(proc.wait(20), 0)

    def test_stdio_silence_from_host_is_treated_as_eof(self):
        t0 = time.monotonic()
        proc, host = self.start_stdio(self.job([{"op": "sleep", "s": 60}], ping=1, pong=2))
        host.expect("HELLO")
        self.assertEqual(proc.wait(30), 75)
        elapsed = time.monotonic() - t0
        self.assertGreaterEqual(elapsed, 4.5)        # 3 * 1 + 2 s without any frame from the host
        self.assertLess(elapsed, 20)
        self.assertEqual(self.exit_file()["reason"], "uplink_lost")

    def test_stdio_pings_keep_link_alive(self):
        proc, host = self.start_stdio(self.job([{"op": "sleep", "s": 60}], ping=1, pong=2))
        host.expect("HELLO")
        for k in range(8):
            host.send("PING", "0", {"t": float(k)})
            time.sleep(1.0)
        self.assertIsNone(proc.poll(), "a host that pings is not silent")
        host.send("KILL", "0", {"reason": "done"})
        self.assertEqual(host.expect("EXIT")[2]["reason"], "killed")
        self.assertEqual(proc.wait(20), 0)

    def test_bye_before_worker_end_is_link_loss_reason_killed(self):
        proc, host = self.start_stdio(self.job([{"op": "sleep", "s": 60}]))
        host.expect("HELLO")
        self.worker_pids(1)
        host.send("BYE")
        self.assertEqual(proc.wait(30), 75)
        self.assertEqual(self.exit_file()["reason"], "killed")

    def test_client_gone_sends_can_and_drops_request(self):
        plan = [{"op": "post", "n": 1, "timeout": 1.5}, {"op": "sleep", "s": 4}, {"op": "exit", "code": 0}]
        proc, host = self.start_stdio(self.job(plan, reply_timeout_s=30))
        rid = host.expect("REQ")[1]
        t0 = time.monotonic()
        host.expect("CAN", rid=rid, timeout=10)
        self.assertLess(time.monotonic() - t0, 5.0, "client-gone is noticed within about a second of the poll")
        ex = host.expect("EXIT", timeout=20)[2]
        self.assertEqual(ex["unanswered_at_exit"], 0)
        self.assertEqual(ex["local_refusals"], {"relay_unavailable": 0, "request_too_large": 0})
        self.assertEqual(self.results()["results"][0]["error"], "TimeoutError")
        # a late RES for the dropped rid is ignored
        host.send("RES", rid, {"status": 200, "headers": []}, b"{}")
        self.assertEqual(proc.wait(20), 0)

    def test_kill_frame_ends_worker_reason_killed(self):
        proc, host = self.start_stdio(self.job([{"op": "group"}, {"op": "sleep", "s": 60}]))
        pids = self.worker_pids(2)
        host.send("KILL", "0", {"reason": "halt"})
        ex = host.expect("EXIT", timeout=20)[2]
        self.assertEqual(ex["reason"], "killed")
        self.assertFalse(ex["timed_out"])
        self.assertNotEqual(ex["returncode"], 0)
        for pid in pids:
            self.assertTrue(wait_dead(pid))
        self.assertEqual(self.exit_file()["reason"], "killed")
        self.assertEqual(proc.wait(20), 0)

    def test_stub_local_refusals_and_unanswered_in_exit_file(self):
        plan = [{"op": "post", "n": 1, "size": 2000},
                {"op": "post", "n": 1, "method": "GET", "start": 1},
                {"op": "post", "n": 1, "suffix": "/models", "start": 2},
                {"op": "hold"}]
        proc, host = self.start_stdio(self.job(plan, max_request_bytes=500))
        ex = host.expect("EXIT", timeout=30)[2]
        self.assertEqual(ex["local_refusals"], {"relay_unavailable": 2, "request_too_large": 1})
        self.assertEqual(ex["unanswered_at_exit"], 1)
        self.assertEqual(len(host.of_type("REQ")), 1)
        record = self.exit_file()
        self.assertEqual(record["local_refusals"], ex["local_refusals"])
        self.assertEqual(record["unanswered_at_exit"], 1)
        codes = sorted(json.loads(r["body"])["error"]["code"] for r in self.results()["results"])
        self.assertEqual(codes, ["relay_unavailable", "relay_unavailable", "request_too_large"])
        self.assertEqual(proc.wait(20), 0)

    def test_bearer_required_when_env_set_and_worker_gets_it(self):
        bearer = "bearer-test-value-0123"
        plan = [{"op": "post", "n": 1}, {"op": "post", "n": 1, "auth": "Bearer wrong", "start": 1},
                {"op": "post", "n": 1, "auth": "", "start": 2}, {"op": "post", "n": 1, "auth": "Bearer local",
                                                                 "start": 3}]
        proc, host = self.start_stdio(self.job(plan), responder=echo_responder, bearer=bearer)
        ex = host.expect("EXIT", timeout=20)[2]
        state = self.results()
        self.assertEqual(state["env"]["OPENROUTER_API_KEY"], bearer)
        results = sorted(state["results"], key=lambda r: r["i"])
        self.assertEqual([r["status"] for r in results], [200, 403, 403, 403])
        for r in results[1:]:
            self.assertEqual(json.loads(r["body"])["error"]["code"], "relay_unavailable")
        self.assertEqual(len(host.of_type("REQ")), 1)
        self.assertEqual(ex["local_refusals"]["relay_unavailable"], 0, "unauthorized is not a local refusal")
        self.assertEqual(proc.wait(20), 0)
        diag = (self.run_dir / "stub.txt").read_text()
        summary = [json.loads(line) for line in diag.splitlines() if '"summary"' in line][-1]
        self.assertEqual(summary["unauthorized"], 3)
        blob = diag + (self.run_dir / "exit.json").read_text() + json.dumps(
            [[f[0], f[2], f[3].decode("latin-1")] for f in host.frames])
        self.assertNotIn(bearer, blob)

    def test_worker_start_value_error_still_writes_exit_file(self):
        # Popen raises ValueError (not OSError) for an illegal env name; the stub must still end with an exit file.
        job = self.job([{"op": "exit", "code": 0}])
        doc = json.loads(job.read_text())
        doc["env"]["BAD=NAME"] = "1"
        job.write_text(json.dumps(doc))
        proc, host = self.start_stdio(job)
        ex = host.expect("EXIT", timeout=20)[2]
        self.assertEqual((ex["returncode"], ex["reason"]), (127, "exit"))
        self.assertEqual(proc.wait(20), 0)
        self.assertEqual(self.exit_file()["returncode"], 127)

    def test_body_too_large_for_one_frame_refused_locally(self):
        # max_request_bytes above what one MHF1 frame carries: refused as request_too_large, never a dropped socket.
        plan = [{"op": "post", "n": 1, "size": 17 * 1024 * 1024}]
        proc, host = self.start_stdio(self.job(plan, max_request_bytes=20 * 1024 * 1024), responder=echo_responder)
        ex = host.expect("EXIT", timeout=30)[2]
        res = self.results()["results"][0]
        self.assertEqual(res.get("status"), 403, res)
        self.assertEqual(json.loads(res["body"])["error"]["code"], "request_too_large")
        self.assertEqual(host.of_type("REQ"), [])
        self.assertEqual(ex["local_refusals"]["request_too_large"], 1)
        self.assertEqual(proc.wait(20), 0)

    def test_route1_with_bearer_accepts_local(self):
        plan = [{"op": "post", "n": 1, "auth": "Bearer local"}, {"op": "post", "n": 1, "auth": "Bearer x", "start": 1}]
        proc, host = self.start_stdio(self.job(plan, route=1, argv_extra=["--api-base", "{api_base}"]),
                                      responder=echo_responder, bearer="bearer-route1-0123")
        host.expect("EXIT", timeout=20)
        results = sorted(self.results()["results"], key=lambda r: r["i"])
        self.assertEqual([r["status"] for r in results], [200, 403])
        self.assertNotIn("OPENROUTER_API_KEY", self.results()["env"])
        self.assertEqual(proc.wait(20), 0)


# ======================================================================================================== daemon

class FakeConn:
    def __init__(self, name):
        self.name, self.alive, self.sent = name, True, []
        self.decoder = mhf_frames.FrameDecoder()

    def start(self):
        pass

    def send(self, frame):
        if self.alive:
            self.sent.append(frame)

    def close(self):
        self.alive = False

    def flush(self, timeout):
        return True

    def frames(self):
        dec = mhf_frames.FrameDecoder()
        return [f for data in self.sent for f in dec.feed(data.decode("ascii"))]


class DaemonTests(StubCase):
    def test_daemon_survives_bridge_replacement_and_resends_pending(self):
        job = self.job([{"op": "post", "n": 1}])
        _, ready = self.start_daemon(job, daemonize=True)
        b1, h1 = self.start_bridge()
        self.assertEqual(h1.expect("HELLO")[2]["mode"], "daemon")
        req1 = h1.expect("REQ")
        b1.kill()
        b1.wait(5)
        b2, h2 = self.start_bridge(responder=echo_responder)
        hello2 = h2.expect("HELLO")[2]
        self.assertEqual(hello2["pending"], [req1[1]])
        self.assertEqual(hello2["worker"]["state"], "running")
        req2 = h2.expect("REQ")
        self.assertEqual((req2[1], req2[2], req2[3]), (req1[1], req1[2], req1[3]))
        ex = h2.expect("EXIT", timeout=20)[2]
        self.assertEqual(ex["returncode"], 0)
        self.assertEqual(self.results()["results"][0]["status"], 200)
        self.assertTrue(alive(ready["pid"]), "the daemon lingers after EXIT")
        # a late bridge still fetches EXIT
        b3, h3 = self.start_bridge()
        self.assertEqual(h3.expect("HELLO")[2]["worker"]["state"], "exited")
        self.assertEqual(h3.expect("EXIT")[2], ex)
        h3.send("BYE")
        self.assertTrue(wait_dead(ready["pid"]), "BYE ends the lingering daemon")
        self.assertEqual(b3.wait(10), 0)
        self.assertFalse(self.sock_path().exists())

    def test_request_written_during_connection_swap_is_resent(self):
        # In process: a REQ submitted on the old connection while a new one attaches is on the new one afterwards.
        job = json.loads(self.job([]).read_text())
        for trial in range(200):
            stub = stub_mod.Stub(job, "daemon", None, lambda *a, **k: None)
            old = FakeConn("old")
            stub.attach(old)
            new = FakeConn("new")
            go = threading.Barrier(2)
            subs = []

            def submit():
                go.wait()
                subs.append(stub.submit(CHAT, b'{"n":%d}' % trial))

            t = threading.Thread(target=submit)
            t.start()
            go.wait()
            stub.attach(new)
            t.join()
            rid = subs[0].rid
            self.assertIs(subs[0].conn, new)
            frames = new.frames()
            self.assertEqual(frames[0][0], "HELLO")
            self.assertIn(rid, [f[1] for f in frames if f[0] == "REQ"], "trial %d lost %s" % (trial, rid))
            self.assertFalse(old.alive)
        # Over the real socket: connections swapped every 0.1 s while 30 requests are in the air; none is lost.
        job = self.job([{"op": "post", "n": 30, "stagger": 0.03, "timeout": 60}], reply_timeout_s=25)
        t0 = time.monotonic()
        self.start_daemon(job)
        current = {"host": None}

        def responder(frame):
            time.sleep(0.05)
            h = current["host"]
            return echo_responder(frame) if h is not None and frame in h.frames else None

        hosts = []
        while time.monotonic() - t0 < 40:
            h = self.sock_host(responder=responder)
            hosts.append(h)
            current["host"] = h
            if h.of_type("EXIT") or self._wait_exit(h, 0.1):
                break
        final = hosts[-1]
        final.expect("EXIT", timeout=30)
        for h in hosts:
            if h is not final:
                h.close()
        self.assertGreater(len(hosts), 3, "the connection was swapped several times")
        results = self.results()["results"]
        self.assertEqual(len(results), 30)
        self.assertEqual([r.get("status") for r in results], [200] * 30)
        self.assertLess(time.monotonic() - t0, 25, "no request waited for the reply timeout")
        final.send("BYE")

    def _wait_exit(self, host, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if host.of_type("EXIT"):
                return True
            time.sleep(0.01)
        return False

    def test_requests_wait_while_no_bridge_is_attached(self):
        job = self.job([{"op": "post", "n": 1, "timeout": 60}])
        self.start_daemon(job)
        time.sleep(2.5)
        self.assertEqual(self.results()["results"] if self.out.exists() else [], [])
        h = self.sock_host(responder=echo_responder)
        hello = h.expect("HELLO")[2]
        self.assertEqual(hello["pending"], ["r1"])
        self.assertEqual(h.expect("REQ")[1], "r1")
        ex = h.expect("EXIT", timeout=20)[2]
        self.assertEqual(ex["returncode"], 0)
        self.assertEqual(self.results()["results"][0]["status"], 200)
        h.send("BYE")

    def test_daemonize_returns_when_ready(self):
        job = self.job([{"op": "sleep", "s": 30}])
        t0 = time.monotonic()
        proc, ready = self.start_daemon(job, daemonize=True)
        self.assertLess(time.monotonic() - t0, 20)
        self.assertEqual(set(ready), {"pid", "port"})
        self.assertNotEqual(ready["pid"], proc.pid)
        self.assertTrue(alive(ready["pid"]))
        # double fork: the first child called setsid, the daemon is in that new session but does not lead it
        self.assertNotEqual(os.getsid(ready["pid"]), os.getsid(0))
        self.assertNotEqual(os.getsid(ready["pid"]), ready["pid"])
        with socket.create_connection(("127.0.0.1", ready["port"]), timeout=5):
            pass
        h = self.sock_host()
        self.assertEqual(h.expect("HELLO")[2]["port"], ready["port"])
        self.assertEqual(h.expect("HELLO")[2]["pid"], ready["pid"])
        h.send("KILL", "0", {"reason": "test"})
        self.assertEqual(h.expect("EXIT", timeout=20)[2]["reason"], "killed")
        h.send("BYE")
        self.assertTrue(wait_dead(ready["pid"]))
        self.assertIn('"serving"', (self.run_dir / "stub.txt").read_text())

    def test_daemonize_returns_2_when_never_ready(self):
        busy = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(busy.close)
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        job = self.job([{"op": "exit"}], port=busy.getsockname()[1])      # the daemon cannot bind its port
        t0 = time.monotonic()
        proc = subprocess.run([sys.executable, str(STUB), "serve", "--job", str(job), "--uplink",
                               "unix:%s" % self.sock_path(), "--daemonize"], env=self.stub_env(), capture_output=True,
                              timeout=40)
        self.assertEqual(proc.returncode, 2)
        self.assertGreaterEqual(time.monotonic() - t0, stub_mod.READY_WAIT_S - 1)
        self.assertFalse((self.run_dir / "stub.ready").exists())
        self.assertIn('"bind_failed"', (self.run_dir / "stub.txt").read_text())

    def test_unix_path_over_90_bytes_refused(self):
        job = self.job([{"op": "exit"}])
        path = "/tmp/" + "x" * 90 + ".sock"
        proc = subprocess.run([sys.executable, str(STUB), "serve", "--job", str(job), "--uplink", "unix:" + path,
                               "--daemonize"], env=self.stub_env(), capture_output=True, timeout=10)
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(Path(path).exists())

    def test_bridge_exits_3_without_socket(self):
        proc = subprocess.run([sys.executable, str(STUB), "bridge", "--uplink", "unix:%s" % (self.tmp / "none.sock")],
                              env=self.stub_env(), capture_output=True, timeout=10, stdin=subprocess.DEVNULL)
        self.assertEqual(proc.returncode, 3)

    def test_daemon_bridge_loss_is_not_a_loss(self):
        job = self.job([{"op": "sleep", "s": 3}, {"op": "exit", "code": 5}], ping=1, pong=1)
        _, ready = self.start_daemon(job)
        h1 = self.sock_host()
        h1.expect("HELLO")
        h1.close()
        time.sleep(6)          # far past 3 * ping + pong: a daemon has no silence rule
        h2 = self.sock_host()
        h2.expect("HELLO")
        self.assertEqual(h2.expect("EXIT", timeout=20)[2]["returncode"], 5)
        self.assertEqual(self.exit_file()["reason"], "exit")
        h2.send("BYE")
        self.assertTrue(wait_dead(ready["pid"]))


class SourceRules(unittest.TestCase):
    def test_stub_imports_only_stdlib_and_its_sibling(self):
        import re
        text = STUB.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"^\s*(?:from|import)\s+(?:mhforge|\.)", text, re.M))
        self.assertRegex(text, r"(?m)^import mhf_frames$")
        self.assertNotIn("environ.copy(", text)
        self.assertNotIn("sys.stdin", text)
        self.assertEqual(re.findall(r"os\.environ\.get\(([A-Z_]+)\)", text), ["BEARER_ENV"])


if __name__ == "__main__":
    unittest.main()
