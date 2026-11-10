# SPDX-License-Identifier: MIT
"""stubprovider.py (spec 3.9): the offline stand-in for Serverless Inference."""

from __future__ import annotations

import http.client
import io
import json
import re
import threading
import unittest

from mhforge import provider, sse, util
from mhforge import stubprovider as sp
from mhforge.provider import classify
from mhforge.stubprovider import Fault, StubProvider
from tests import helpers

MODEL = "openai/gpt-oss-120b"


def _body(user, system="Reasoning: medium", max_tokens=16384):
    return {"model": MODEL, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.0, "max_tokens": max_tokens}


LAW = _body("案情：某人深夜入室窃取财物。请给出罪名，格式 [罪名]...<eoa>")
USPTO = _body("Predict the reactants for this product. Answer with SMILES only: CCOC(C)=O")
USPTO_LOWER = _body("Give the reactants of the product CCO")
SYMPTOM = _body("I have a runny nose and a sore throat.")


def _merge(raw: bytes):
    events = list(sse.iter_sse(raw.splitlines(keepends=True)))
    chunks = [p for e, p in events if e == "chunk"]
    return events, chunks, sse.merge_stream(chunks)


class DefaultReply(unittest.TestCase):
    def test_default_reply_per_dataset_is_deterministic(self):
        cases = [(LAW, sp.LAWBENCH_ANSWER), (USPTO, sp.USPTO_ANSWER), (USPTO_LOWER, sp.USPTO_ANSWER),
                 (SYMPTOM, sp.SYMPTOM_ANSWER)]
        for body, answer in cases:
            with self.subTest(answer=answer, user=body["messages"][1]["content"][:20]):
                first, second = sp.default_reply(body), sp.default_reply(json.loads(json.dumps(body)))
                self.assertEqual(first, second)
                self.assertEqual(first["content"], answer)
                self.assertEqual(first["finish_reason"], "stop")
                self.assertTrue(first["reasoning"])
                nbytes = sum(len(m["content"].encode("utf-8")) for m in body["messages"])
                self.assertEqual(first["usage"]["prompt_tokens"], -(-nbytes // 4))
                self.assertEqual(first["usage"]["completion_tokens"], 16)
                self.assertEqual(first["usage"]["total_tokens"], first["usage"]["prompt_tokens"] + 16)
        # parseable by the rules upstream's evaluators apply (data/evaluators.py)
        self.assertRegex(sp.SYMPTOM_ANSWER, r"\[DIAGNOSIS\](.*?)\[/DIAGNOSIS\]")
        self.assertEqual(re.search(r"\[罪名\](.*?)(?:<eoa>|$)", sp.LAWBENCH_ANSWER).group(1), "盗窃")
        self.assertTrue(all(re.fullmatch(r"[A-Za-z0-9()=#@+\-\[\]]+", part) for part in sp.USPTO_ANSWER.split(".")))
        # the rule looks at the last user message only
        mixed = {"model": MODEL, "messages": [{"role": "user", "content": "罪名"}, {"role": "assistant", "content": "a"},
                                              {"role": "user", "content": "fever and cough"}]}
        self.assertEqual(sp.default_reply(mixed)["content"], sp.SYMPTOM_ANSWER)
        parts = _body([{"type": "text", "text": "list the reactants"}])
        self.assertEqual(sp.default_reply(parts)["content"], sp.USPTO_ANSWER)


class RenderSse(unittest.TestCase):
    def test_render_sse_round_trips_through_iter_sse(self):
        reply = {"content": "x" * 150, "reasoning": "r" * 70, "finish_reason": "stop",
                 "usage": {"prompt_tokens": 7, "completion_tokens": 16, "total_tokens": 23}}
        raw = sp.render_sse(reply, model=MODEL, chunk_chars=64)
        self.assertEqual(raw, sp.render_sse(reply, model=MODEL, chunk_chars=64))      # pure
        events, chunks, merged = _merge(raw)
        self.assertEqual([e for e, _ in events if e != "chunk"], ["done"])
        self.assertEqual(events[-1], ("done", None))
        self.assertEqual(merged["choices"][0]["message"]["content"], reply["content"])
        self.assertEqual(merged["choices"][0]["message"]["reasoning"], reply["reasoning"])
        self.assertEqual(merged["choices"][0]["finish_reason"], "stop")
        self.assertEqual(merged["usage"], reply["usage"])
        self.assertEqual(merged["model"], MODEL)
        self.assertEqual(chunks[-1]["choices"], [])                                     # usage in the last chunk
        self.assertEqual(chunks[-1]["usage"], reply["usage"])
        for chunk in chunks[:-1]:
            delta = chunk["choices"][0]["delta"]
            self.assertLessEqual(len(delta.get("content", "")), 64)
            self.assertLessEqual(len(delta.get("reasoning", "")), 64)
        self.assertEqual(sse.reasoning_field(chunks), "reasoning")
        # the same bytes in the shape of the fixture: "data: <json>\n\n"
        self.assertTrue(all(line.startswith(b"data: ") for line in raw.split(b"\n\n") if line))
        # options: the other reasoning name, no [DONE], no model, an error chunk, no usage
        alt = sp.render_sse(dict(reply, reasoning_field="reasoning_content", done=False), model=None)
        events, chunks, merged = _merge(alt)
        self.assertNotIn(("done", None), events)
        self.assertNotIn("model", merged)
        self.assertEqual(sse.reasoning_field(chunks), "reasoning_content")
        err = sp.render_sse({"error": {"code": 400, "message": "maximum context length"}}, model=MODEL)
        events, chunks, merged = _merge(err)
        self.assertEqual(merged["choices"], [])
        self.assertEqual(merged["error"]["code"], 400)
        self.assertNotIn("usage", merged)
        empty = sp.render_sse({"content": "", "finish_reason": "length"}, model=MODEL)
        merged = _merge(empty)[2]
        self.assertEqual(merged["choices"][0]["message"]["content"], "")
        self.assertEqual(merged["choices"][0]["finish_reason"], "length")
        with self.assertRaises(ValueError):
            sp.render_sse(reply, model=MODEL, chunk_chars=0)


class Faults(unittest.TestCase):
    def run_one(self, fault, body=SYMPTOM, deadline_s=30.0, clock=None):
        stub = StubProvider(faults=[fault], clock=clock)
        sink = io.BytesIO()
        result = stub.complete(body, sse_sink=sink, deadline_s=deadline_s)
        return stub, result, sink.getvalue()

    def test_each_fault_kind(self):
        checked = set()
        # http: the status and the error object
        _, r, raw = self.run_one(Fault("http", (400, {"message": "maximum context length is 131072", "code": None})))
        self.assertEqual((r.http_status, r.error.kind), (400, "http"))
        self.assertEqual(json.loads(raw)["error"]["message"], "maximum context length is 131072")
        self.assertEqual(classify(r), "context_limit")
        _, r, _ = self.run_one(Fault("http", 503))
        self.assertEqual(classify(r), "retry_transport")
        _, r, _ = self.run_one(Fault("http", {"status": 302}))
        self.assertEqual(r.http_status, 302)
        checked.add("http")
        # error_stream: an error chunk and no choice
        _, r, _ = self.run_one(Fault("error_stream"))
        self.assertEqual(r.error.kind, "stream_error")
        self.assertEqual(r.merged["choices"], [])
        self.assertEqual(classify(r), "retry_transport")
        _, r, _ = self.run_one(Fault("error_stream", {"code": 400, "message": "maximum context length exceeded"}))
        self.assertEqual(classify(r), "context_limit")
        checked.add("error_stream")
        # truncate: cut in the middle of a line, no usage, no [DONE]
        _, r, raw = self.run_one(Fault("truncate"))
        self.assertNotIn(b"[DONE]", raw)
        self.assertFalse(raw.endswith(b"\n"))
        self.assertFalse(r.done)
        self.assertNotIn("usage", r.merged)
        self.assertEqual(r.error.kind, "bad_json")
        self.assertEqual(classify(r), "retry_transport")
        checked.add("truncate")
        # no_usage: complete, [DONE], no usage block
        _, r, _ = self.run_one(Fault("no_usage"))
        self.assertTrue(r.done)
        self.assertNotIn("usage", r.merged)
        self.assertEqual(classify(r), "ok")
        self.assertEqual(provider.settle_basis(r, "ok"), "bounded_unknown")
        checked.add("no_usage")
        # length_empty: finish_reason length, empty content, the whole budget used
        _, r, _ = self.run_one(Fault("length_empty"), body=dict(SYMPTOM, max_tokens=512))
        choice = r.merged["choices"][0]
        self.assertEqual((choice["finish_reason"], choice["message"]["content"]), ("length", ""))
        self.assertTrue(choice["message"]["reasoning"])
        self.assertEqual(r.merged["usage"]["completion_tokens"], 512)
        checked.add("length_empty")
        # wrong_model and no_model
        _, r, _ = self.run_one(Fault("wrong_model"))
        self.assertEqual(r.merged["model"], sp.DEFAULT_WRONG_MODEL)
        _, r, _ = self.run_one(Fault("wrong_model", "other/model"))
        self.assertEqual(r.merged["model"], "other/model")
        checked.add("wrong_model")
        _, r, _ = self.run_one(Fault("no_model"))
        self.assertNotIn("model", r.merged)
        self.assertEqual(classify(r), "ok")
        checked.add("no_model")
        # hang: on the injected clock; beyond the deadline it is a timeout after the status line
        clock = helpers.FakeClock()
        _, r, raw = self.run_one(Fault("hang", 100.0), deadline_s=5.0, clock=clock)
        self.assertEqual(clock.sleeps, [5.0])
        self.assertEqual((r.http_status, r.error.kind, raw), (200, "timeout", b""))
        self.assertEqual(classify(r), "retry_transport")
        clock = helpers.FakeClock()
        _, r, _ = self.run_one(Fault("hang", 2.0), deadline_s=5.0, clock=clock)
        self.assertEqual(clock.sleeps, [2.0])
        self.assertEqual(classify(r), "ok")
        clock = helpers.FakeClock()
        _, r, raw = self.run_one(Fault("hang", (100.0, 2)), deadline_s=5.0, clock=clock)
        self.assertEqual(raw.count(b"data:"), 2)
        self.assertEqual(r.error.kind, "timeout")
        self.assertIsNotNone(r.first_byte_s)
        checked.add("hang")
        # rate_limit: 429 with retry-after
        _, r, _ = self.run_one(Fault("rate_limit"))
        self.assertEqual((r.http_status, r.headers.get("retry-after")), (429, "1"))
        self.assertEqual(classify(r, terminal_429_patterns=["quota"]), "retry_rate_limited")
        checked.add("rate_limit")
        # contradictory_usage: cached above prompt (the ledger's 'unknown' case)
        _, r, _ = self.run_one(Fault("contradictory_usage"))
        usage = r.merged["usage"]
        self.assertGreater(usage["prompt_tokens_details"]["cached_tokens"], usage["prompt_tokens"])
        checked.add("contradictory_usage")
        # reasoning_content: the other wire name
        _, r, raw = self.run_one(Fault("reasoning_content"))
        self.assertEqual(r.reasoning_field, "reasoning_content")
        self.assertIn(b'"reasoning_content"', raw)
        self.assertTrue(r.merged["choices"][0]["message"]["reasoning"])
        checked.add("reasoning_content")
        self.assertEqual(checked, set(sp.FAULT_KINDS))
        with self.assertRaises(ValueError):
            Fault("nonsense")
        with self.assertRaises(ValueError):
            Fault("http", 500, when=0)

    def test_fault_when_and_times(self):
        stub = StubProvider(faults=[Fault("http", 500, when=2, times=2)])
        statuses = [stub.complete(SYMPTOM, sse_sink=None, deadline_s=5).http_status for _ in range(5)]
        self.assertEqual(statuses, [200, 500, 500, 200, 200])
        always = StubProvider(faults=[Fault("no_usage")])
        for _ in range(3):
            self.assertNotIn("usage", always.complete(SYMPTOM, sse_sink=None, deadline_s=5).merged)
        self.assertEqual(len(always.calls), 3)
        self.assertEqual(always.calls[0], SYMPTOM)

    def test_complete_parses_with_production_parser(self):
        stub = StubProvider()
        sink = io.BytesIO()
        r = stub.complete(LAW, sse_sink=sink, deadline_s=5)
        self.assertEqual(sink.getvalue(), sp.render_sse(sp.default_reply(LAW), model=MODEL)[:len(sink.getvalue())])
        self.assertEqual(r.merged, sse.merge_stream([p for e, p in sse.iter_sse(sink.getvalue().splitlines(True))
                                                     if e == "chunk"]))
        self.assertEqual(r.merged["choices"][0]["message"]["content"], sp.LAWBENCH_ANSWER)
        self.assertTrue(r.done)
        self.assertIsNone(r.error)
        self.assertEqual(classify(r), "ok")
        # latency on the injected clock
        clock = helpers.FakeClock()
        StubProvider(clock=clock, latency_s=1.5).complete(LAW, sse_sink=None, deadline_s=5)
        self.assertEqual(clock.sleeps, [1.5])
        # custom rules
        r = StubProvider(rules=lambda body: {"content": "fixed", "finish_reason": "stop"}).complete(
            LAW, sse_sink=None, deadline_s=5)
        self.assertEqual(r.merged["choices"][0]["message"]["content"], "fixed")
        self.assertNotIn("usage", r.merged)


class Server(unittest.TestCase):
    def setUp(self):
        self.enterContext(util.loopback_guard())

    def test_serve_stub_binds_loopback_and_records_requests(self):
        server, url = sp.serve_stub(faults=[Fault("http", 401, when=2)])
        self.addCleanup(server.close)
        host, port = server.server_address[:2]
        self.assertEqual(host, "127.0.0.1")
        self.assertEqual(url, f"http://127.0.0.1:{port}/v1")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        payload = json.dumps(SYMPTOM).encode()
        conn.request("POST", "/v1/chat/completions", body=payload,
                     headers={"Authorization": "Bearer sk-server-secret-42", "Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("content-type"), "text/event-stream")
        raw = resp.read()
        conn.close()
        self.assertEqual(raw, sp.render_sse(sp.default_reply(SYMPTOM), model=MODEL))   # the same bytes as in process
        rec = server.requests[0]
        self.assertEqual((rec["method"], rec["path"], rec["body"], rec["body_bytes"]),
                         ("POST", "/v1/chat/completions", SYMPTOM, len(payload)))
        self.assertIn("authorization", rec["header_names"])
        self.assertEqual(rec["headers"]["authorization"], "[redacted]")
        self.assertNotIn("sk-server-secret-42", json.dumps(server.requests))
        self.assertEqual(server.calls, [SYMPTOM])
        # the fault plan counts requests
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/v1/chat/completions", body=payload)
        resp = conn.getresponse()
        self.assertEqual(resp.status, 401)
        self.assertIn("error", json.loads(resp.read()))
        conn.close()
        # other paths and methods: 404, recorded
        for method in ("POST", "GET"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            conn.request(method, "/v1/models", body=b"{}" if method == "POST" else None)
            self.assertEqual(conn.getresponse().status, 404)
            conn.close()
        self.assertEqual(len(server.requests), 4)
        self.assertEqual(len(server.calls), 2)

    def test_close_releases_a_hanging_handler(self):
        server, url = sp.serve_stub(faults=[Fault("hang", 60.0)])
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("POST", "/v1/chat/completions", body=json.dumps(SYMPTOM).encode())
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        done = threading.Event()
        threading.Thread(target=lambda: (server.close(), done.set()), daemon=True).start()
        self.assertTrue(done.wait(10))
        self.assertEqual(resp.read(), b"")
        conn.close()

    def test_peak_in_flight_counted(self):
        n = 6
        barrier = threading.Barrier(n, timeout=10)

        def rules(body):
            barrier.wait()                          # every call is inside complete at the same time
            return sp.default_reply(body)

        stub = StubProvider(rules=rules)
        threads = [threading.Thread(target=stub.complete, args=(SYMPTOM,),
                                    kwargs={"sse_sink": None, "deadline_s": 10}) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(stub.peak_in_flight, n)
        self.assertEqual(stub.in_flight, 0)
        self.assertEqual(len(stub.calls), n)
        sequential = StubProvider()
        for _ in range(4):                          # sequential calls do not raise the peak
            sequential.complete(SYMPTOM, sse_sink=None, deadline_s=5)
        self.assertEqual(sequential.peak_in_flight, 1)
        # the server counts the same way
        barrier2 = threading.Barrier(3, timeout=10)
        server, url = sp.serve_stub(rules=lambda body: (barrier2.wait(), sp.default_reply(body))[1])
        self.addCleanup(server.close)
        manifest = helpers.make_manifest(**{"provider.kind": "stub", "provider.base_url": url})
        client = provider.ForgeProviderClient(manifest, environ={})
        results = []
        threads = [threading.Thread(target=lambda: results.append(
            client.complete(SYMPTOM, sse_sink=None, deadline_s=10))) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        self.assertEqual([classify(r) for r in results], ["ok"] * 3)
        self.assertEqual(server.peak_in_flight, 3)


if __name__ == "__main__":
    unittest.main()
