# SPDX-License-Identifier: MIT
"""ingress_http.py (spec 3.11): loopback listener per job, token rule, method and length rules, backlog."""

from __future__ import annotations

import contextlib
import http.client
import inspect
import io
import json
import random
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mhforge import ingress_http, ledger as ledger_mod, util, wire
from mhforge.circuit import Circuit
from mhforge.gateway import Gateway
from mhforge.ingress_http import Endpoint, LoopbackIngress
from mhforge.ledger import Ledger
from mhforge.replay import ReplayStore
from mhforge.stubprovider import StubProvider
from mhforge.wire import JobIdentity
from tests import helpers

WIRE = "openai/gpt-oss-120b"
BODY = json.dumps({"model": WIRE, "messages": [{"role": "system", "content": "Reasoning: medium"},
                                               {"role": "user", "content": "Patient has a cough"}],
                   "temperature": 0.0, "max_tokens": 16384}).encode()


def job(candidate="no_memory", attempt=1) -> JobIdentity:
    return JobIdentity("mh-test", "val", candidate, "USPTO", attempt, "baseline", None)


def post(ep: Endpoint, body: bytes = BODY, *, token: str | None = None, path="/v1/chat/completions",
         method="POST") -> tuple[int, dict, bytes]:
    conn = http.client.HTTPConnection(ep.host, ep.port, timeout=30)
    try:
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        conn.request(method, path, body=body if method not in ("GET", "HEAD") else None, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
    finally:
        conn.close()


def raw_exchange(ep: Endpoint, data: bytes, timeout=5.0) -> bytes:
    s = socket.create_connection((ep.host, ep.port), timeout=timeout)
    try:
        s.sendall(data)
        chunks = []
        while True:
            try:
                block = s.recv(65536)
            except (socket.timeout, ConnectionResetError):
                break
            if not block:
                break
            chunks.append(block)
            if b"\r\n\r\n" in b"".join(chunks):
                head, _, rest = b"".join(chunks).partition(b"\r\n\r\n")
                length = next((int(l.split(b":", 1)[1]) for l in head.split(b"\r\n")
                               if l.lower().startswith(b"content-length:")), 0)
                if len(rest) >= length:
                    break
        return b"".join(chunks)
    finally:
        s.close()


def code_of(data: bytes) -> str | None:
    try:
        return json.loads(data)["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None


class _Base(unittest.TestCase):
    def setUp(self):
        self.enterContext(util.loopback_guard())
        self.manifest = helpers.make_manifest({"provider.kind": "stub"})

    def ingress(self, gateway) -> LoopbackIngress:
        ing = LoopbackIngress(gateway, self.manifest)
        self.addCleanup(ing.close_all)
        return ing

    def real_gateway(self) -> tuple[Gateway, helpers.TmpState]:
        saved = ledger_mod.FULLFSYNC
        ledger_mod.FULLFSYNC = False
        self.addCleanup(setattr, ledger_mod, "FULLFSYNC", saved)
        ctx = helpers.tmp_state()
        state = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        clock = helpers.FakeClock()
        ids = helpers.SeqIds(clock)
        p = state.paths
        gw = Gateway(self.manifest, p, provider=StubProvider(clock=clock),
                     ledger=Ledger(p.ledger, self.manifest, clock=clock, ids=ids),
                     replay=ReplayStore(p.replay, "search", clock=clock),
                     circuit=Circuit(p.circuit, threshold=3, cooldown_s=60, clock=clock), clock=clock, ids=ids,
                     rng=random.Random(3))
        gw.poll_s = 0.05
        return gw, state


class Listener(_Base):
    def test_binds_loopback_only(self):
        fake = helpers.FakeGateway()
        ing = self.ingress(fake)
        j = job()
        fake.register_job(j)
        ep = ing.open(j, None)
        self.assertEqual((ep.host, ep.base_url), ("127.0.0.1", f"http://127.0.0.1:{ep.port}/v1"))
        server, _ = ing._open[j.job_id]
        self.assertEqual(server.socket.getsockname()[0], "127.0.0.1")
        self.assertEqual(server.socket.family, socket.AF_INET)
        self.assertEqual(list(inspect.signature(LoopbackIngress.open).parameters), ["self", "job", "token"])
        self.assertEqual(server.request_queue_size, 128)
        src = inspect.getsource(ingress_http)
        self.assertNotIn("0.0.0.0", src)
        self.assertNotIn("os.environ", src)
        self.assertEqual(ing.open_ports(), [ep.port])
        self.assertEqual(post(ep)[0], 200)
        with self.assertRaises(ValueError):
            ing.open(j, None)                                        # one listener per job

    def test_route2_requires_job_token(self):
        fake = helpers.FakeGateway()
        ing = self.ingress(fake)
        j = job()
        token = helpers.SeqIds().token()
        fake.register_job(j, token=token)
        ep = ing.open(j, token)
        status, _, data = post(ep, token=None)
        self.assertEqual((status, code_of(data)), (403, "unknown_job"))
        status, _, data = post(ep, token="local")                    # the Route 1 bearer is not enough here
        self.assertEqual((status, code_of(data)), (403, "unknown_job"))
        status, headers, data = post(ep, token=token)
        self.assertEqual(status, 200)
        self.assertEqual([c["job_id"] for c in fake.calls], [None, None, j.job_id])
        self.assertEqual({c["ingress"] for c in fake.calls}, {"loopback"})
        self.assertEqual(fake.calls[-1]["body"], BODY)
        self.assertEqual(fake.calls[-1]["path"], "/v1/chat/completions")
        self.assertEqual(headers["content-length"], str(len(data)))

    def test_wrong_token_is_unknown_job(self):
        gw, state = self.real_gateway()
        ing = self.ingress(gw)
        j = job()
        token = helpers.SeqIds().token()
        gw.register_job(j, token=token)
        ep = ing.open(j, token)
        status, headers, data = post(ep, token=token[:-1] + ("A" if token[-1] != "A" else "B"))
        self.assertEqual((status, code_of(data)), (403, "unknown_job"))
        helpers.assert_neutral(wire.GatewayReply(status, tuple(headers.items()), data))
        recs = list((state.paths.calls_root / "_unknown").glob("*.json"))
        self.assertEqual(len(recs), 1)
        self.assertEqual(json.loads(recs[0].read_text())["refusal"]["code"], "unknown_job")
        self.assertEqual(post(ep, token=token)[0], 200)
        self.assertEqual(len(list(state.paths.calls_dir(j.job_id).glob("*.json"))), 1)

    def test_route1_port_identifies_job(self):
        fake = helpers.FakeGateway()
        ing = self.ingress(fake)
        a, b = job("a_sys"), job("b_sys")
        for j in (a, b):
            fake.register_job(j)
        ea, eb = ing.open(a, None), ing.open(b, None)
        self.assertNotEqual(ea.port, eb.port)
        for ep in (eb, ea, eb):
            self.assertEqual(post(ep, token="local")[0], 200)        # upstream's Route 1 bearer is always "local"
        self.assertEqual([c["job_id"] for c in fake.calls], [b.job_id, a.job_id, b.job_id])
        self.assertEqual(ing.open_ports(), sorted([ea.port, eb.port]))

    def test_only_post_with_length(self):
        fake = helpers.FakeGateway()
        ing = self.ingress(fake)
        j = job()
        fake.register_job(j)
        ep = ing.open(j, None)
        for method in ("GET", "PUT", "DELETE", "PATCH", "OPTIONS"):
            with self.subTest(method=method):
                status, headers, data = post(ep, method=method)
                self.assertEqual((status, code_of(data)), (403, "not_allowed"))
                self.assertEqual(headers["content-length"], str(len(data)))
                self.assertEqual(headers["x-should-retry"], "false")
        status, headers, data = post(ep, method="HEAD")
        self.assertEqual((status, data), (403, b""))
        chunked = raw_exchange(ep, b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\n"
                                   b"Transfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n")
        self.assertIn(b"403", chunked.split(b"\r\n", 1)[0])
        self.assertIn(b"not_allowed", chunked)
        nolen = raw_exchange(ep, b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n")
        self.assertIn(b"not_allowed", nolen)
        badlen = raw_exchange(ep, b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Length: ab\r\n\r\n")
        self.assertIn(b"not_allowed", badlen)
        for bad in ("²", "1¹"):                               # str.isdigit() is true, int() raises
            odd = raw_exchange(ep, ("POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Length: "
                                    + bad + "\r\n\r\nab").encode("latin-1"))
            self.assertIn(b"not_allowed", odd)
        self.assertEqual(fake.calls, [])
        self.assertEqual(post(ep)[0], 200)

    def test_oversize_body_refused_unread(self):
        fake = helpers.FakeGateway()
        ing = self.ingress(fake)
        j = job()
        fake.register_job(j)
        ep = ing.open(j, None)
        limit = int(util.lookup(self.manifest, "gateway.max_request_bytes"))
        started = time.monotonic()
        reply = raw_exchange(ep, b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                                 b"Content-Length: %d\r\n\r\n" % (limit + 1))     # the body is never sent
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(reply.startswith(b"HTTP/1.1 403"))
        self.assertIn(b"request_too_large", reply)
        self.assertIn(b"connection: close", reply.lower())
        self.assertEqual(fake.calls, [])
        exactly = b"x" * 10
        self.assertEqual(code_of(post(ep, exactly)[2]), None)               # within the bound: handed to the gateway
        self.assertEqual(len(fake.calls), 1)

    def test_94_simultaneous_connections_all_answered(self):
        fake = helpers.FakeGateway(delay_s=0.3)
        ing = self.ingress(fake)
        j = job()
        fake.register_job(j)
        ep = ing.open(j, None)
        barrier = threading.Barrier(94)
        results: list = []
        lock = threading.Lock()

        def one():
            barrier.wait()
            r = post(ep)
            with lock:
                results.append(r[0])

        threads = [threading.Thread(target=one) for _ in range(94)]
        started = time.monotonic()
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        elapsed = time.monotonic() - started
        self.assertEqual(results, [200] * 94)
        self.assertEqual(len(fake.calls), 94)
        self.assertGreaterEqual(fake.peak_in_flight, 90)
        self.assertLess(elapsed, 10)

    def test_listener_closed_after_job(self):
        fake = helpers.FakeGateway()
        ing = self.ingress(fake)
        j = job()
        fake.register_job(j)
        ep = ing.open(j, None)
        keep = http.client.HTTPConnection(ep.host, ep.port, timeout=10)    # a keep-alive connection that outlives it
        keep.request("POST", "/v1/chat/completions", body=BODY)
        first = keep.getresponse()
        first.read()
        self.assertEqual(first.status, 200)
        ing.close(j.job_id)
        self.assertEqual(ing.open_ports(), [])
        with self.assertRaises(ConnectionRefusedError):
            socket.create_connection((ep.host, ep.port), timeout=5).close()
        try:
            keep.request("POST", "/v1/chat/completions", body=BODY)
            late = keep.getresponse()
            data = late.read()
            self.assertEqual((late.status, code_of(data)), (403, "unknown_job"))
        except (ConnectionError, http.client.HTTPException):
            pass                                                         # or the connection is simply gone
        finally:
            keep.close()
        ing.close(j.job_id)                                              # idempotent

    def test_token_never_logged(self):
        gw, state = self.real_gateway()
        ing = self.ingress(gw)
        j = job()
        token = helpers.SeqIds().token()
        gw.register_job(j, token=token)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(err):
            ep = ing.open(j, token)
            self.assertEqual(post(ep, token=token)[0], 200)
            self.assertEqual(post(ep, token=token + "x")[0], 403)
            self.assertEqual(post(ep, token=token, method="GET")[0], 403)
            raw_exchange(ep, b"BROKEN\r\n\r\n")
        self.assertEqual(err.getvalue(), "")
        self.assertNotIn(token, repr(ing._open[j.job_id][0]))
        self.assertNotIn(token, ep.base_url)
        for path in state.paths.root.rglob("*"):
            if path.is_file() and path.suffix in (".json", ".sse"):
                text = path.read_bytes()
                self.assertNotIn(token.encode(), text)
                self.assertNotIn(b"authorization", text.lower())


CHILD = r"""
import json, sys
from text_classification import llm as L
mode, base, prompts = sys.argv[1], sys.argv[2], json.loads(sys.argv[3])
out = []
for p in prompts:
    if mode == "route2":
        client = L.LLM(model="openrouter/openai/gpt-oss-120b", temperature=0.0)
    else:
        client = L.LLM(model="openai/openai/gpt-oss-120b", api_base=base, temperature=0.0)
    try:
        content = client(p)
        out.append({"ok": True, "content": content, "usage": client.get_usage()})
    except Exception as exc:
        out.append({"ok": False, "type": type(exc).__name__, "retryable": L._is_retryable(exc)})
print("MHF-RESULT " + json.dumps(out))
"""


@helpers.needs_example_venv
class RealClient(_Base):
    def run_child(self, mode: str, ep: Endpoint, token: str | None, prompts: list[str], scratch: Path) -> list[dict]:
        extra = {"PYTHONPATH": str(helpers.example_dir().parent), "LITELLM_TELEMETRY": "False",
                 "NO_PROXY": "127.0.0.1,localhost"}
        if mode == "route2":
            extra.update(OPENROUTER_API_BASE=ep.base_url, OPENROUTER_API_KEY=token)
        env = helpers.venv_child_env(scratch, **extra)
        proc = subprocess.run([str(helpers.example_python()), "-c", CHILD, mode, ep.base_url, json.dumps(prompts)],
                              env=env, cwd=str(scratch), capture_output=True, text=True, timeout=300,
                              stdin=subprocess.DEVNULL)
        line = next((l for l in proc.stdout.splitlines() if l.startswith("MHF-RESULT ")), None)
        if line is None:
            raise AssertionError(f"child failed ({proc.returncode}): {proc.stderr[-2000:]}")
        return json.loads(line[len("MHF-RESULT "):])

    def test_real_client_one_request_per_refusal_both_routes(self):
        codes = list(wire.REFUSALS)
        for mode in ("route2", "route1"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix="mhf-ing-") as tmp:
                fake = helpers.FakeGateway([wire.refusal_reply(c) for c in codes])
                ing = self.ingress(fake)
                j = job(candidate=mode)
                token = helpers.SeqIds().token() if mode == "route2" else None
                fake.register_job(j, token=token)
                ep = ing.open(j, token)
                results = self.run_child(mode, ep, token, [f"refusal prompt {c}" for c in codes], Path(tmp))
                self.assertEqual(len(fake.calls), len(codes), "a refusal was retried by the client")
                self.assertEqual([c["job_id"] for c in fake.calls], [j.job_id] * len(codes))
                for code, r in zip(codes, results):
                    self.assertFalse(r["ok"], code)
                    self.assertFalse(r["retryable"], code)
                ing.close(j.job_id)

    def test_real_client_accepts_merged_body_and_zero_usage_replay(self):
        gw, state = self.real_gateway()
        ing = self.ingress(gw)
        for mode in ("route2", "route1"):
            with self.subTest(mode=mode):
                j = job(candidate=mode)
                token = helpers.SeqIds().token() if mode == "route2" else None
                gw.register_job(j, token=token)
                ep = ing.open(j, token)
                prompt = [f"Patient {mode} reports a sore throat"]
                with tempfile.TemporaryDirectory(prefix="mhf-ing-") as t1, \
                        tempfile.TemporaryDirectory(prefix="mhf-ing-") as t2:
                    first = self.run_child(mode, ep, token, prompt, Path(t1))[0]
                    second = self.run_child(mode, ep, token, prompt, Path(t2))[0]     # fresh worker cache
                self.assertTrue(first["ok"] and second["ok"], (first, second))
                self.assertIn("common cold", first["content"])
                self.assertEqual(first["content"], second["content"])
                self.assertGreater(first["usage"]["input_tokens"], 0)
                self.assertEqual((second["usage"]["input_tokens"], second["usage"]["output_tokens"]), (0, 0))
                recs = sorted((json.loads(p.read_text()) for p in state.paths.calls_dir(j.job_id).glob("*.json")),
                              key=lambda r: r["call_id"])
                self.assertEqual([r["outcome"] for r in recs], ["completed", "replayed"])
                self.assertTrue(recs[0]["flags"]["reasoning_present"])
                ing.close(j.job_id)


if __name__ == "__main__":
    unittest.main()
