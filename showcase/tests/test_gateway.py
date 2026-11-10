# SPDX-License-Identifier: Apache-2.0
# Adapted from ash0ts/harness2-wandb@31b0f82 tests/showcase/test_transport_v4.py (transport owner, error-only stream,
# provider error limit and circuit, identity gate). Changes: rewritten from pytest to unittest; run against the
# in-process StubProvider, the real ledger, replay store and circuit and a FakeClock instead of a patched urlopen;
# non-200 provider answers are booked at zero; plus the new cases of spec 5.2.
"""gateway.py (spec 3.10)."""

from __future__ import annotations

import http.client
import inspect
import json
import random
import re
import socket
import sqlite3
import threading
import time
import unittest
from pathlib import Path

from mhforge import gateway as gw_mod, ledger as ledger_mod, util, wire
from mhforge.circuit import Circuit
from mhforge.gateway import Gateway, admit, backoff_s, conformance, input_limit, model_identity_problem
from mhforge.ingress_http import LoopbackIngress
from mhforge.ledger import Ledger
from mhforge.replay import ReplayStore
from mhforge.sse import iter_sse, merge_stream, to_completion_body
from mhforge.stubprovider import FAULT_KINDS, Fault, StubProvider, default_reply, render_sse
from mhforge.wire import GatewayReply, JobIdentity
from tests import helpers

WIRE = "openai/gpt-oss-120b"
RECORD_FIXTURES = helpers.FIXTURES / "call_records"
_saved_fullfsync = None


def setUpModule():                                 # durability pragma only (test_ledger checks it); keeps tests fast
    global _saved_fullfsync
    _saved_fullfsync = ledger_mod.FULLFSYNC
    ledger_mod.FULLFSYNC = False


def tearDownModule():
    ledger_mod.FULLFSYNC = _saved_fullfsync


def body(text="SYNTHETIC USPTO PROMPT 0", *, model=WIRE, max_tokens=16384, temperature=0.0, system="Reasoning: medium",
         messages=None, **extra) -> bytes:
    if messages is None:
        messages = ([{"role": "system", "content": system}] if system is not None else []) + \
                   [{"role": "user", "content": text}]
    doc = {"model": model, "messages": messages, "max_tokens": max_tokens, "usage": {"include": True}}
    if temperature is not None:
        doc["temperature"] = temperature
    doc.update(extra)
    return json.dumps(doc).encode("utf-8")


def env(raw: bytes, path="/v1/chat/completions", method="POST") -> bytes:
    return wire.make_envelope(method, path, raw)


def code_of(reply: GatewayReply) -> str | None:
    try:
        return json.loads(reply.body)["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None


def header(reply: GatewayReply, name: str) -> str | None:
    return next((v for k, v in reply.headers if k.lower() == name), None)


class HookClock(helpers.FakeClock):
    """FakeClock whose sleep() runs the queued hooks first (used to act during a backoff)."""

    def __init__(self) -> None:
        super().__init__()
        self.hooks: list = []

    def sleep(self, seconds: float) -> None:
        super().sleep(seconds)
        while self.hooks:
            self.hooks.pop(0)()


class GateProvider:
    """Blocks every call until `gate` is set, then delegates to the inner provider."""

    def __init__(self, inner) -> None:
        self.inner, self.gate, self.entered = inner, threading.Event(), threading.Semaphore(0)
        self.calls = []

    def complete(self, body, *, sse_sink, deadline_s):
        self.calls.append(dict(body))
        self.entered.release()
        if not self.gate.wait(30):
            raise RuntimeError("gate never opened")
        return self.inner.complete(body, sse_sink=sse_sink, deadline_s=deadline_s)


class RoutingProvider:
    """Prompts containing 'BAD' go to `bad`, every other prompt to `good`."""

    def __init__(self, good, bad) -> None:
        self.good, self.bad = good, bad

    def complete(self, body, *, sse_sink, deadline_s):
        target = self.bad if "BAD" in json.dumps(body["messages"]) else self.good
        return target.complete(body, sse_sink=sse_sink, deadline_s=deadline_s)


class RaisingProvider:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, body, *, sse_sink, deadline_s):
        self.calls += 1
        raise RuntimeError("client bug")


class SettleFaultLedger(Ledger):
    """The real ledger with settle() replaced by a raise of `exc` (an injected fault of the collaborator)."""
    exc: BaseException | None = None
    times = 10 ** 6

    def settle(self, *args, **kwargs):
        if self.exc is not None and self.times > 0:
            self.times -= 1
            raise self.exc
        return super().settle(*args, **kwargs)


class CountingCircuit(Circuit):
    raise_on_finish = False

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.enters, self.finishes = 0, []

    def enter(self, *, max_wait_s):
        waited = super().enter(max_wait_s=max_wait_s)
        self.enters += 1
        return waited

    def finish(self, outcome):
        self.finishes.append(outcome)
        super().finish(outcome)
        if self.raise_on_finish:
            raise RuntimeError("circuit file unwritable")


class Crash(BaseException):
    """Stands for the death of the runner process."""


class Rig:
    def __init__(self, tc: unittest.TestCase, *, faults=(), rules=default_reply, provider=None, clock=None,
                 ledger_cls=Ledger, circuit_cls=CountingCircuit, stub_clock="fake", paths=None, **overrides) -> None:
        if paths is None:
            ctx = helpers.tmp_state()
            state = ctx.__enter__()
            tc.addCleanup(ctx.__exit__, None, None, None)
            paths = state.paths
        self.paths = paths
        self.manifest = helpers.make_manifest({"provider.kind": "stub", **overrides})
        self.clock = clock or helpers.FakeClock()
        self.ids = helpers.SeqIds(self.clock)
        self.ledger = ledger_cls(paths.ledger, self.manifest, clock=self.clock, ids=self.ids)
        scope = util.lookup(self.manifest, "routing.reply_reuse_scope")
        self.replay = ReplayStore(paths.replay, scope, clock=self.clock)
        c = util.lookup(self.manifest, "provider.circuit")
        self.circuit = circuit_cls(paths.circuit, threshold=c["failures"], cooldown_s=c["cooldown_s"], clock=self.clock)
        self.stub = StubProvider(rules=rules, faults=faults, clock=self.clock if stub_clock == "fake" else None)
        self.provider = provider if provider is not None else self.stub
        self.gw = Gateway(self.manifest, paths, provider=self.provider, ledger=self.ledger, replay=self.replay,
                          circuit=self.circuit, clock=self.clock, ids=self.ids, rng=random.Random(7))
        self.gw.poll_s = 0.05

    def job(self, candidate="no_memory", dataset="USPTO", attempt=1, phase="baseline", iteration=None, repeat=None,
            sample=None, register=True, token=None) -> JobIdentity:
        job = JobIdentity("mh-test", "val", candidate, dataset, attempt, phase, iteration, repeat, sample)
        if register:
            self.gw.register_job(job, token=token)
        return job

    def send(self, job, raw=None, **kw) -> GatewayReply:
        return self.gw.handle(job, env(body() if raw is None else raw), **kw)

    def records(self, job_id: str | None = "*") -> list[dict]:
        root = self.paths.calls_root
        pattern = "*/*.json" if job_id == "*" else f"{'_unknown' if job_id is None else job_id}/*.json"
        return [json.loads(p.read_text()) for p in sorted(root.glob(pattern))]

    def rows(self) -> list[dict]:
        conn = sqlite3.connect(str(self.paths.ledger))
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM calls ORDER BY created_utc, id")]
        finally:
            conn.close()

    def pending(self) -> int:
        conn = sqlite3.connect(str(self.paths.ledger), timeout=30)
        try:
            return conn.execute("SELECT COUNT(*) FROM calls WHERE status='pending'").fetchone()[0]
        finally:
            conn.close()

    def permits(self) -> int:
        return self.gw._sem._value

    def in_thread(self, fn, *args, **kw) -> tuple[threading.Thread, list]:
        out: list = []
        t = threading.Thread(target=lambda: out.append(fn(*args, **kw)), daemon=True)
        t.start()
        return t, out


def wait_until(pred, timeout=10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


class PureHelpers(unittest.TestCase):
    def setUp(self):
        self.m = helpers.make_manifest()

    def test_admit_exact_keys_drops_usage(self):
        admitted, dropped, code, nbytes = admit(body("hello"), self.m)
        self.assertIsNone(code)
        self.assertEqual(dropped, ["usage"])
        self.assertEqual(set(admitted), {"model", "messages", "temperature", "max_tokens"})
        admitted, dropped, code, _ = admit(body("hello", stream=False), self.m)
        self.assertIsNone(code)
        self.assertEqual(dropped, ["stream", "usage"])
        self.assertNotIn("stream", admitted)
        self.assertEqual(nbytes, len("Reasoning: medium") + len("hello"))

    def test_admit_rejects_response_format_tools_stream_true(self):
        for extra in ({"response_format": {"type": "json_object"}}, {"tools": []}, {"stream": True},
                      {"max_completion_tokens": 10}, {"stream": "false"}):
            with self.subTest(extra=extra):
                self.assertEqual(admit(body("x", **extra), self.m)[2], "invalid_request")
        for raw in (b"", b"not json", b"[1, 2]", b'{"model": NaN}', b"[" * 100000):
            self.assertEqual(admit(raw, self.m)[2], "invalid_request")

    def test_model_must_equal_wire_model(self):
        self.assertEqual(admit(body("x", model="openrouter/openai/gpt-oss-120b"), self.m)[2], "model_not_allowed")
        self.assertEqual(admit(body("x", model="openai/gpt-oss-20b"), self.m)[2], "model_not_allowed")
        self.assertIsNone(admit(body("x"), self.m)[2])

    def test_message_with_extra_key_refused(self):
        for extra in ({"name": "a"}, {"tool_calls": []}, {"reasoning": "r"}):
            msgs = [{"role": "user", "content": "x", **extra}]
            with self.subTest(extra=extra):
                self.assertEqual(admit(body(messages=msgs), self.m)[2], "invalid_request")
        bad_parts = [[{"type": "image_url", "text": "x"}], [{"type": "text", "text": "x", "cache_control": {}}],
                     [{"type": "text", "text": 1}]]
        for parts in bad_parts:
            self.assertEqual(admit(body(messages=[{"role": "user", "content": parts}]), self.m)[2], "invalid_request")
        self.assertEqual(admit(body(messages=[{"role": "tool", "content": "x"}]), self.m)[2], "invalid_request")
        ok = admit(body(messages=[{"role": "user", "content": [{"type": "text", "text": "ab"}]}]), self.m)
        self.assertIsNone(ok[2])
        self.assertEqual(ok[3], 2)

    def test_admit_field_bounds(self):
        for kw in ({"max_tokens": 0}, {"max_tokens": 16385}, {"max_tokens": True}, {"max_tokens": 1.0},
                   {"temperature": 2.5}, {"temperature": True}, {"temperature": "0"}, {"temperature": -0.1}):
            with self.subTest(kw=kw):
                self.assertEqual(admit(body("x", **kw), self.m)[2], "invalid_request")
        self.assertIsNone(admit(body("x", temperature=None), self.m)[2])            # temperature is optional
        self.assertEqual(admit(body(messages=[]), self.m)[2], "invalid_request")
        lone = b'{"model": "%s", "messages": [{"role": "user", "content": "\\ud800"}], "max_tokens": 5}' % WIRE.encode()
        self.assertEqual(admit(lone, self.m)[2], "invalid_request")

    def test_size_limit_uses_unescaped_utf8(self):
        prompt = "罪" * 224_000
        raw = body(prompt)                             # json.dumps escapes each character as six bytes
        self.assertGreater(len(raw), 224_000 * 6)
        admitted, _, code, nbytes = admit(raw, self.m)
        self.assertIsNone(code)
        self.assertEqual(nbytes, 224_000 * 3 + len("Reasoning: medium"))
        self.assertEqual(admit(body("罪" * 400_000), self.m)[2], "request_too_large")   # 1.2 MB of text, 2.4 MB raw
        small = helpers.make_manifest({"gateway.max_request_bytes": 1000})
        self.assertEqual(admit(body("x" * 2000), small)[2], "request_too_large")
        rig = Rig(self)
        reply = rig.send(rig.job(), raw)
        self.assertEqual(reply.status, 200)
        self.assertEqual(rig.records()[0]["input_limit"], 131072)

    def test_many_small_messages_refused_or_within_limit(self):
        nine = [{"role": "user", "content": "x"}] * 9
        self.assertEqual(admit(body(messages=nine), self.m)[2], "invalid_request")
        eight = [{"role": "system", "content": "a"}] + [{"role": "user", "content": c} for c in "bcdefgh"]
        admitted, _, code, nbytes = admit(body(messages=eight), self.m)
        self.assertIsNone(code)
        limit = input_limit(nbytes, len(eight), self.m)
        self.assertEqual(limit, 8 + 8192 + 8 * 16)
        harmony = _harmony_tokens(eight)
        if harmony is not None:                        # only when the example venv is present
            self.assertGreaterEqual(limit, harmony)

    def test_input_limit_backoff_and_conformance(self):
        self.assertEqual(input_limit(10, 2, self.m), 10 + 8192 + 32)
        self.assertEqual(input_limit(10 ** 7, 2, self.m), 131072)
        rng = random.Random(1)
        for _ in range(50):
            self.assertTrue(20 <= backoff_s("retry_rate_limited", 429, self.m, rng) <= 40)
            self.assertTrue(30 <= backoff_s("retry_transport", 503, self.m, rng) <= 60)
            self.assertTrue(30 <= backoff_s("retry_transport", 408, self.m, rng) <= 60)
            self.assertTrue(2 <= backoff_s("retry_transport", None, self.m, rng) <= 8)
            self.assertTrue(2 <= backoff_s("retry_transport", 404, self.m, rng) <= 8)
        adm = admit(body("x"), self.m)[0]
        self.assertEqual(conformance(adm, self.m), {"temperature": True, "max_tokens": True, "system_prompt": True})
        adm = admit(body("x", temperature=1.0, max_tokens=10, system="other"), self.m)[0]
        self.assertEqual(conformance(adm, self.m), {"temperature": False, "max_tokens": False, "system_prompt": False})

    def test_model_identity_problem_table(self):
        self.assertIsNone(model_identity_problem("anything", True, None))
        self.assertIsNone(model_identity_problem(None, True, None))
        self.assertIsNone(model_identity_problem(WIRE, True, WIRE))
        self.assertIn("differs", model_identity_problem("openai/gpt-oss-20b", True, WIRE))
        self.assertIn("missing", model_identity_problem(None, True, WIRE))
        self.assertIsNone(model_identity_problem(None, False, WIRE))

    def test_gateway_is_handles_requests_and_binds_nothing(self):
        src = inspect.getsource(gw_mod)
        for forbidden in ("import socket", "os.environ", "getenv", "Authorization", "bind("):
            self.assertNotIn(forbidden, src)
        rig = Rig(self)
        self.assertIsInstance(rig.gw, wire.HandlesRequests)


def _harmony_tokens(messages) -> int | None:
    """The Harmony token count of the messages, from the example venv's openai_harmony (None without the venv)."""
    if not helpers._has_example_venv():
        return None
    import subprocess
    import tempfile
    code = ("import json,sys\nfrom openai_harmony import HarmonyEncodingName,load_harmony_encoding,Conversation,"
            "Message,Role\nenc=load_harmony_encoding(HarmonyEncodingName.HARMONY_GPT_OSS)\n"
            "R={'system':Role.SYSTEM,'user':Role.USER,'assistant':Role.ASSISTANT}\nm=json.loads(sys.argv[1])\n"
            "c=Conversation.from_messages([Message.from_role_and_content(R[x['role']],x['content']) for x in m])\n"
            "print(len(enc.render_conversation_for_completion(c,Role.ASSISTANT)))\n")
    with tempfile.TemporaryDirectory(prefix="mhf-harmony-") as tmp:
        proc = subprocess.run([str(helpers.example_python()), "-c", code, json.dumps(messages)],
                              env=helpers.venv_child_env(Path(tmp)), cwd=tmp, capture_output=True, text=True,
                              timeout=120, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise AssertionError("harmony count failed: " + proc.stderr[-500:])
    return int(proc.stdout.strip())


class Ported(unittest.TestCase):
    def test_transport_owner_counts_every_attempt(self):
        rig = Rig(self, faults=[Fault("http", 503, when=1, times=2)])
        reply = rig.send(rig.job())
        self.assertEqual(reply.status, 200)
        self.assertEqual(len(rig.stub.calls), 3)
        rows = rig.rows()
        self.assertEqual([r["status"] for r in rows], ["rejected", "rejected", "complete"])
        self.assertEqual([r["phase"] for r in rows], ["baseline", "recovery", "recovery"])
        self.assertEqual(len({r["call_id"] for r in rows}), 1)
        (rec,) = rig.records()
        self.assertEqual([a["n"] for a in rec["attempts"]], [1, 2, 3])
        self.assertEqual([a["http_status"] for a in rec["attempts"]], [503, 503, 200])
        self.assertEqual(rec["attempts"][2]["response_headers"]["x-request-id"], "stub-3")
        self.assertTrue(rec["response_id"].startswith("chatcmpl-stub-"))
        self.assertEqual(rec["cost_basis"], "usage")
        self.assertEqual(rig.ledger.snapshot()["bounded_unknown_requests"], 0)
        for a in rec["attempts"]:
            self.assertTrue((rig.paths.calls_dir(rec["job"]["job_id"]) / a["sse_file"]).is_file())

    def test_error_only_stream_retries_before_replying(self):
        err = {"message": "Provider failed", "code": 500}
        rig = Rig(self, faults=[Fault("error_stream", err, when=1, times=2)])
        reply = rig.send(rig.job())
        self.assertEqual(reply.status, 200)
        self.assertNotIn(b"Provider failed", reply.body)
        self.assertEqual(len(rig.stub.calls), 3)
        self.assertEqual(rig.ledger.snapshot()["bounded_unknown_requests"], 2)
        (rec,) = rig.records()
        self.assertEqual([a["disposition"] for a in rec["attempts"]], ["retry_transport", "retry_transport", "ok"])
        self.assertIsNone(rig.ledger.halted())
        sse = (rig.paths.calls_dir(rec["job"]["job_id"]) / rec["attempts"][0]["sse_file"]).read_bytes()
        self.assertIn(b"Provider failed", sse)                     # the evidence keeps it; the worker never sees it

    def test_provider_error_limit_and_circuit(self):
        for code, attempts, reply_code, failures, unknown in ((500, 3, "provider_unavailable", 3, 3),
                                                             (400, 1, "provider_rejected", 0, 0)):
            with self.subTest(code=code):
                rig = Rig(self, faults=[Fault("error_stream", {"message": "Provider failed", "code": code})])
                reply = rig.send(rig.job())
                self.assertEqual(reply.status, 403)
                self.assertEqual(code_of(reply), reply_code)
                helpers.assert_neutral(reply)
                self.assertEqual(len(rig.stub.calls), attempts)
                self.assertEqual(rig.ledger.snapshot()["bounded_unknown_requests"], unknown)
                self.assertEqual(rig.circuit.state()["failures"], failures)
                self.assertIsNone(rig.ledger.halted())

    def test_model_identity_gate_off_by_default_and_halts_when_on(self):
        off = Rig(self, faults=[Fault("wrong_model", "openai/gpt-oss-20b")])
        reply = off.send(off.job())
        self.assertEqual(reply.status, 200)
        self.assertIsNone(off.ledger.halted())
        self.assertTrue(off.records()[0]["flags"]["model_mismatch"])
        for fault, why in ((Fault("wrong_model", "openai/gpt-oss-20b"), "differs"), (Fault("no_model"), "missing")):
            with self.subTest(fault=fault.kind):
                on = Rig(self, faults=[fault], **{"provider.expected_response_model": WIRE})
                job = on.job()
                reply = on.send(job)
                self.assertEqual((reply.status, code_of(reply)), (402, "halted"))
                halt = on.ledger.halted()
                self.assertEqual(halt.reason, "model_identity")
                self.assertIn(why, halt.detail)
                self.assertEqual(on.replay.stats()["stored"], 0)
                self.assertEqual([r["status"] for r in on.rows()], ["complete"])     # it was paid: settled from usage
                self.assertEqual(code_of(on.send(job)), "halted")
                self.assertEqual(len(on.stub.calls), 1)
        good = Rig(self, **{"provider.expected_response_model": WIRE})
        self.assertEqual(good.send(good.job()).status, 200)


class Replies(unittest.TestCase):
    def test_non_streaming_worker_gets_one_merged_body(self):
        rig = Rig(self)
        reply = rig.send(rig.job(), body("Patient has a cough"))
        self.assertEqual(reply.status, 200)
        self.assertEqual(header(reply, "content-type"), "application/json")
        self.assertEqual(header(reply, "x-mhforge-source"), "provider")
        self.assertNotIn(b"data:", reply.body)
        doc = json.loads(reply.body)
        self.assertEqual(doc["object"], "chat.completion")
        self.assertEqual(len(doc["choices"]), 1)
        expect_reply = default_reply(rig.stub.calls[0])
        self.assertEqual(doc["choices"][0]["message"]["content"], expect_reply["content"])
        self.assertEqual(doc["choices"][0]["message"]["reasoning"], expect_reply["reasoning"])
        self.assertEqual(doc["usage"], expect_reply["usage"])
        chunks = [p for e, p in iter_sse(render_sse(expect_reply, model=WIRE).splitlines(True)) if e == "chunk"]
        self.assertEqual(doc, to_completion_body(merge_stream(chunks), request_model=WIRE, created=doc["created"]))
        self.assertEqual(rig.records()[0]["response"], doc)

    def test_temperature_max_tokens_and_system_message_unchanged_upstream(self):
        rig = Rig(self)
        raw = body("Patient has a cough", temperature=0.0, max_tokens=16384)
        self.assertEqual(rig.send(rig.job(), raw).status, 200)
        sent = rig.stub.calls[0]
        self.assertEqual(set(sent), {"model", "messages", "temperature", "max_tokens", "stream", "stream_options"})
        original = json.loads(raw)
        for key in ("model", "messages", "temperature", "max_tokens"):
            self.assertEqual(sent[key], original[key])
        self.assertEqual(type(sent["temperature"]), float)
        self.assertIs(sent["stream"], True)
        self.assertEqual(sent["stream_options"], {"include_usage": True})
        rec = rig.records()[0]
        self.assertEqual(rec["conformance"], {"temperature": True, "max_tokens": True, "system_prompt": True})
        self.assertEqual(rec["dropped_keys"], ["usage"])
        other = rig.send(rig.job(candidate="other"), body("cough", temperature=1.0, max_tokens=100))
        self.assertEqual(other.status, 200)                           # conformance is recorded, not enforced
        self.assertEqual((rig.stub.calls[1]["temperature"], rig.stub.calls[1]["max_tokens"]), (1.0, 100))

    def test_reply_status_only_200_402_403(self):
        args = {"http": 500, "hang": 5000.0, "wrong_model": "openai/gpt-oss-20b",
                "error_stream": {"message": "x", "code": 503}}
        for kind in FAULT_KINDS:
            for when in ("always", 1):
                with self.subTest(kind=kind, when=when):
                    rig = Rig(self, faults=[Fault(kind, args.get(kind), when=when)])
                    reply = rig.send(rig.job())
                    self.assertIn(reply.status, (200, 402, 403))
                    if reply.status != 200:
                        helpers.assert_neutral(reply)
                    else:
                        self.assertEqual(json.loads(reply.body)["object"], "chat.completion")
        for status in (301, 400, 401, 402, 403, 404, 408, 409, 413, 422, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                rig = Rig(self, faults=[Fault("http", status)])
                reply = rig.send(rig.job())
                self.assertIn(reply.status, (402, 403))
                helpers.assert_neutral(reply)

    def test_non_200_is_neutral_with_x_should_retry_false(self):
        seen = {}
        rig = Rig(self, **{"gateway.max_requests_per_job": 1})
        job = rig.job()
        seen["invalid_request"] = rig.send(job, json.dumps({"model": WIRE}).encode())
        seen["model_not_allowed"] = rig.send(job, body("x", model="m"))
        seen["request_too_large"] = rig.send(job, body("x" * 1_100_000))
        seen["not_allowed"] = rig.gw.handle(job, env(body(), method="GET"))
        seen["unknown_job"] = rig.send(None)
        self.assertEqual(rig.send(job, body("first")).status, 200)
        seen["job_request_ceiling"] = rig.send(job, body("second"))
        for fault, code in ((Fault("http", (400, {"message": "bad", "code": "x"})), "provider_rejected"),
                            (Fault("http", (400, {"message": "maximum context length", "code": "x"})),
                             "context_length_exceeded"),
                            (Fault("http", 503), "provider_unavailable"),
                            (Fault("http", 401), "halted")):
            r = Rig(self, faults=[fault])
            seen[code] = r.send(r.job())
        r = Rig(self, **{"budgets.solver_usd.total": "0.000001"})
        seen["budget_exhausted"] = r.send(r.job())
        r = Rig(self, provider=RaisingProvider())
        seen["gateway_internal"] = r.send(r.job())
        for code, reply in seen.items():
            with self.subTest(code=code):
                self.assertEqual(code_of(reply), code)
                helpers.assert_neutral(reply)
                self.assertEqual(header(reply, "x-should-retry"), "false")
        for code in wire.REFUSALS:
            helpers.assert_neutral(wire.refusal_reply(code))

    def test_provider_text_never_reaches_worker(self):
        nasty = "timeout 429 rate limit: too many requests, retry in 5s"
        for fault in (Fault("http", (400, {"message": nasty, "code": "bad_request"})),
                      Fault("error_stream", {"message": nasty, "code": 400})):
            with self.subTest(fault=fault.kind):
                rig = Rig(self, faults=[fault])
                reply = rig.send(rig.job())
                self.assertEqual(code_of(reply), "provider_rejected")
                helpers.assert_neutral(reply)
                self.assertNotIn(b"429", reply.body)
                self.assertNotIn(b"timeout", reply.body.lower())
                rec = rig.records()[0]
                self.assertIn("timeout 429", rec["refusal"]["detail"])          # kept in the record only
                self.assertEqual(rec["outcome"], "provider_rejected")

    def test_429_is_retried_with_long_backoff_never_returned(self):
        rig = Rig(self, faults=[Fault("rate_limit", when=1, times=1)])
        reply = rig.send(rig.job())
        self.assertEqual(reply.status, 200)
        self.assertEqual(len(rig.clock.sleeps), 1)
        self.assertTrue(20 <= rig.clock.sleeps[0] <= 40)
        self.assertEqual(rig.records()[0]["attempts"][0]["disposition"], "retry_rate_limited")
        always = Rig(self, faults=[Fault("rate_limit")])
        reply = always.send(always.job())
        self.assertEqual((reply.status, code_of(reply)), (403, "provider_unavailable"))
        helpers.assert_neutral(reply)
        self.assertEqual(len(always.stub.calls), 3)
        self.assertTrue(all(20 <= s <= 40 for s in always.clock.sleeps) and len(always.clock.sleeps) == 2)
        self.assertIsNone(always.ledger.halted())

    def test_retries_exhausted_gives_provider_unavailable_costing_nothing(self):
        rig = Rig(self, faults=[Fault("http", 503)])
        job = rig.job()
        reply = rig.send(job)
        self.assertEqual((reply.status, code_of(reply)), (403, "provider_unavailable"))
        self.assertEqual(len(rig.stub.calls), 3)
        self.assertEqual([r["status"] for r in rig.rows()], ["rejected"] * 3)
        self.assertEqual(rig.ledger.committed(), 0)
        rec = rig.records()[0]
        self.assertEqual((rec["outcome"], rec["cost_micro"], rec["cost_basis"]), ("failed", 0, "none"))
        self.assertEqual(rig.gw.close_job(job.job_id)["failed"], 1)


class Concurrency(unittest.TestCase):
    def test_semaphore_bounds_open_reservations(self):
        peak_pending = [0]
        lock = threading.Lock()
        holder: list = []

        def rules(b):
            n = holder[0].pending()
            with lock:
                peak_pending[0] = max(peak_pending[0], n)
            return default_reply(b)

        rig = Rig(self, rules=rules, stub_clock=None)
        holder.append(rig)
        rig.stub.latency_s = 0.25
        job = rig.job()
        barrier = threading.Barrier(94)
        results = []

        def one(i):
            barrier.wait()
            results.append(rig.send(job, body(f"prompt {i}")))

        threads = [threading.Thread(target=one, args=(i,)) for i in range(94)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(120)
        self.assertEqual(len(results), 94)
        self.assertTrue(all(r.status == 200 for r in results))
        self.assertEqual(rig.stub.peak_in_flight, 16)
        self.assertLessEqual(peak_pending[0], 16)
        self.assertEqual(rig.permits(), 16)
        self.assertEqual(rig.pending(), 0)

    def test_semaphore_released_during_backoff(self):
        clock = HookClock()
        rig = Rig(self, clock=clock, faults=[Fault("http", 503, when=1, times=1)], **{"gateway.max_in_flight": 1})
        job = rig.job()
        other: list = []

        def during_backoff():
            t, out = rig.in_thread(rig.send, job, body("second prompt"))
            t.join(10)
            other.extend(out)

        clock.hooks.append(during_backoff)
        reply = rig.send(job, body("first prompt"))
        self.assertEqual(reply.status, 200)
        self.assertEqual(len(other), 1, "the second request could not get the slot during the first one's backoff")
        self.assertEqual(other[0].status, 200)
        self.assertEqual(rig.permits(), 1)

    def test_identical_in_flight_requests_coalesce(self):
        gate = GateProvider(StubProvider())
        rig = Rig(self, provider=gate)
        job = rig.job()
        lead, lead_out = rig.in_thread(rig.send, job)
        self.assertTrue(gate.entered.acquire(timeout=10))
        followers = [rig.in_thread(rig.send, job) for _ in range(5)]
        time.sleep(0.3)
        gate.gate.set()
        for t, _ in [(lead, lead_out)] + followers:
            t.join(10)
        replies = lead_out + [out[0] for _, out in followers]
        self.assertEqual([r.status for r in replies], [200] * 6)
        self.assertEqual(len(gate.calls), 1)
        self.assertEqual(sorted(header(r, "x-mhforge-source") for r in replies), ["coalesced"] * 5 + ["provider"])
        self.assertEqual(len({r.body for r in replies}), 1)
        recs = rig.records()
        leader = next(r for r in recs if r["outcome"] == "completed")
        coalesced = [r for r in recs if r["outcome"] == "coalesced"]
        self.assertEqual(len(coalesced), 5)
        for r in coalesced:
            self.assertEqual(r["replay"], {"scope": "search", "source_call_id": leader["call_id"], "kind": "coalesced"})
            self.assertEqual((r["cost_micro"], r["cost_basis"], r["attempts"]), (0, "none", []))
            self.assertEqual(r["usage"], leader["usage"])
        self.assertEqual(len(rig.rows()), 1)

    def test_replayed_request_costs_nothing_has_zero_usage_and_is_recorded(self):
        rig = Rig(self)
        job = rig.job()
        first = rig.send(job)
        second = rig.send(rig.job(candidate="fewshot_all"))       # scope "search": another job of the campaign
        self.assertEqual((first.status, second.status), (200, 200))
        self.assertEqual(header(second, "x-mhforge-source"), "replay")
        doc1, doc2 = json.loads(first.body), json.loads(second.body)
        self.assertEqual(doc2["usage"], {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        strip = lambda d: {k: v for k, v in d.items() if k != "usage"}   # noqa: E731
        self.assertEqual(strip(doc2), strip(doc1))
        self.assertEqual(len(rig.stub.calls), 1)
        self.assertEqual(len(rig.rows()), 1)
        recs = {r["outcome"]: r for r in rig.records()}
        rep = recs["replayed"]
        self.assertEqual(rep["replay"], {"scope": "search", "source_call_id": recs["completed"]["call_id"],
                                         "kind": "stored"})
        self.assertEqual((rep["cost_micro"], rep["cost_basis"], rep["response"]), (0, "none", None))
        self.assertEqual(rep["usage"], recs["completed"]["usage"])
        self.assertEqual(rep["reply"], {"status": 200, "code": None, "source": "replay"})

    def test_sample_salt_prevents_replay_and_coalescing_across_samples(self):
        rig = Rig(self)
        s1, s2 = rig.job(sample=1), rig.job(sample=2)
        self.assertEqual(header(rig.send(s1), "x-mhforge-source"), "provider")
        self.assertEqual(header(rig.send(s2), "x-mhforge-source"), "provider")
        self.assertEqual(header(rig.send(s1), "x-mhforge-source"), "replay")
        self.assertEqual(len(rig.stub.calls), 2)
        self.assertEqual({r["reuse_salt"] for r in rig.records()}, {"s1", "s2"})
        gate = GateProvider(StubProvider())
        rig2 = Rig(self, provider=gate)
        a, b = rig2.job(sample=3), rig2.job(sample=4)
        ta, oa = rig2.in_thread(rig2.send, a)
        tb, ob = rig2.in_thread(rig2.send, b)
        self.assertTrue(gate.entered.acquire(timeout=10) and gate.entered.acquire(timeout=10))
        gate.gate.set()
        ta.join(10), tb.join(10)
        self.assertEqual(len(gate.calls), 2)
        self.assertEqual({header(oa[0], "x-mhforge-source"), header(ob[0], "x-mhforge-source")}, {"provider"})

    def test_no_replay_only_in_qualification(self):
        rig = Rig(self)
        base = rig.job()
        rig.send(base, no_replay=True)
        self.assertEqual(header(rig.send(base, no_replay=True), "x-mhforge-source"), "replay")
        qual = rig.job(phase="qualification")
        for _ in range(2):
            self.assertEqual(header(rig.send(qual, no_replay=True), "x-mhforge-source"), "provider")
        self.assertEqual(header(rig.send(qual), "x-mhforge-source"), "provider")   # bypass replies were not stored
        self.assertEqual(header(rig.send(qual), "x-mhforge-source"), "replay")
        self.assertEqual(len(rig.stub.calls), 4)
        self.assertEqual({r["ledger_phase"] for rec in rig.records() if rec["job"]["phase"] == "qualification"
                          for r in rec["attempts"]}, {"qualification"})


class Budget(unittest.TestCase):
    def test_budget_cap_sets_halt_and_replies_402(self):
        for overrides, reason in (({"budgets.solver_usd.phases.baseline": "0.001"}, "budget:baseline"),
                                  ({"budgets.solver_usd.total": "0.001"}, "budget:total")):
            with self.subTest(reason=reason):
                rig = Rig(self, **overrides)
                job = rig.job()
                reply = rig.send(job)
                self.assertEqual((reply.status, code_of(reply)), (402, "budget_exhausted"))
                helpers.assert_neutral(reply)
                self.assertEqual(rig.ledger.halted().reason, reason)
                self.assertEqual(rig.stub.calls, [])
                again = rig.send(job, body("another"))
                self.assertEqual((again.status, code_of(again)), (402, "halted"))
                self.assertEqual(rig.rows(), [])

    def test_job_ceiling_replies_403_without_halt_and_is_not_shared(self):
        gate = GateProvider(StubProvider())
        rig = Rig(self, provider=gate, **{"gateway.max_requests_per_job": 1, "gateway.max_in_flight": 1})
        a, b, c = rig.job("job_a"), rig.job("job_b"), rig.job("job_c")
        gate.gate.set()
        self.assertEqual(rig.send(a, body("X1")).status, 200)        # job a is now at its request ceiling
        gate.gate.clear()
        tz, oz = rig.in_thread(rig.send, c, body("Z"))                 # holds the only slot
        self.assertTrue(gate.entered.acquire(timeout=10) and gate.entered.acquire(timeout=10))
        ta, oa = rig.in_thread(rig.send, a, body("Y"))                 # leader for Y, queued
        self.assertTrue(wait_until(lambda: rig.replay.stats()["in_flight"] == 2))
        tb, ob = rig.in_thread(rig.send, b, body("Y"))                 # follower of a's Y
        time.sleep(0.3)
        gate.gate.set()
        for t in (tz, ta, tb):
            t.join(15)
        self.assertEqual((oa[0].status, code_of(oa[0])), (403, "job_request_ceiling"))
        helpers.assert_neutral(oa[0])
        self.assertEqual((ob[0].status, header(ob[0], "x-mhforge-source")), (200, "provider"))
        self.assertIsNone(rig.ledger.halted())
        rec_a = [r for r in rig.records(a.job_id) if r["outcome"] == "refused"]
        self.assertEqual(rec_a[0]["refusal"]["code"], "job_request_ceiling")
        self.assertIn("request_excerpt", rec_a[0])
        self.assertEqual(len(gate.calls), 3)

    def test_halt_refuses_before_any_reservation(self):
        rig = Rig(self)
        job = rig.job()
        rig.ledger.set_halt("operator", "test")
        reply = rig.send(job)
        self.assertEqual((reply.status, code_of(reply)), (402, "halted"))
        self.assertEqual(rig.rows(), [])
        self.assertEqual(rig.stub.calls, [])
        rec = rig.records()[0]
        self.assertEqual((rec["outcome"], rec["admitted_utc"]), ("refused", None))

    def test_halt_lets_in_flight_calls_finish_and_settle(self):
        gate = GateProvider(StubProvider())
        rig = Rig(self, provider=gate)
        job = rig.job()
        t, out = rig.in_thread(rig.send, job)
        self.assertTrue(gate.entered.acquire(timeout=10))
        rig.ledger.set_halt("operator", "stop")
        self.assertEqual(code_of(rig.send(job, body("new"))), "halted")
        self.assertEqual(len(gate.calls), 1)
        gate.gate.set()
        t.join(10)
        self.assertEqual(out[0].status, 200)
        self.assertEqual([r["status"] for r in rig.rows()], ["complete"])
        self.assertEqual(rig.gw.drain(5), 0)

    def test_missing_usage_is_bounded_unknown_and_reply_still_200(self):
        rig = Rig(self, faults=[Fault("no_usage")])
        job = rig.job()
        reply = rig.send(job)
        self.assertEqual(reply.status, 200)
        self.assertNotIn("usage", json.loads(reply.body))
        (row,) = rig.rows()
        self.assertEqual(row["status"], "bounded_unknown")
        rec = rig.records()[0]
        self.assertEqual((rec["outcome"], rec["cost_basis"], rec["usage"]), ("completed", "reservation", None))
        counters = rig.gw.close_job(job.job_id)
        self.assertEqual(counters["bounded_unknown_micro"], row["reserved_micro"])
        self.assertEqual(counters["usage_calls"], 0)

    def test_context_limit_is_candidate_failure_not_retried_not_charged(self):
        err = {"message": "This model's maximum context length is 131072 tokens", "code": "context_length_exceeded"}
        rig = Rig(self, faults=[Fault("http", (400, err))])
        job = rig.job()
        for _ in range(2):                              # never stored: a repeat is forwarded again
            reply = rig.send(job)
            self.assertEqual((reply.status, code_of(reply)), (403, "context_length_exceeded"))
        self.assertEqual(len(rig.stub.calls), 2)
        self.assertEqual([(r["status"], r["spent_micro"]) for r in rig.rows()], [("rejected", 0)] * 2)
        self.assertEqual(rig.ledger.committed(), 0)
        self.assertEqual(rig.circuit.state()["failures"], 0)
        self.assertEqual({r["outcome"] for r in rig.records()}, {"provider_rejected"})
        self.assertIsNone(rig.ledger.halted())

    def test_config_faults_halt(self):
        for status in (401, 402, 403):
            for message in ("Invalid API key", "Spend limit reached", "Organisation disabled"):
                with self.subTest(status=status, message=message):
                    rig = Rig(self, faults=[Fault("http", (status, {"message": message, "code": str(status)}))])
                    reply = rig.send(rig.job())
                    self.assertEqual((reply.status, code_of(reply)), (402, "halted"))
                    self.assertEqual(rig.ledger.halted().reason, "provider_config")
                    self.assertEqual(len(rig.stub.calls), 1)
                    self.assertEqual([r["status"] for r in rig.rows()], ["rejected"])

    def test_quota_429_halts_provider_quota(self):
        for err in ({"message": "You exceeded your quota", "code": "insufficient_quota"},
                    {"message": "monthly quota reached", "code": "limit"}):
            with self.subTest(err=err):
                rig = Rig(self, faults=[Fault("http", (429, err))])
                reply = rig.send(rig.job())
                self.assertEqual((reply.status, code_of(reply)), (402, "halted"))
                self.assertEqual(rig.ledger.halted().reason, "provider_quota")
                self.assertEqual(len(rig.stub.calls), 1)
                self.assertEqual(rig.clock.sleeps, [])

    def test_truncated_empty_is_flagged_delivered_and_stored(self):
        rig = Rig(self, faults=[Fault("length_empty")])
        job = rig.job()
        reply = rig.send(job)
        self.assertEqual(reply.status, 200)
        doc = json.loads(reply.body)
        self.assertEqual((doc["choices"][0]["message"]["content"], doc["choices"][0]["finish_reason"]), ("", "length"))
        rec = rig.records()[0]
        self.assertTrue(rec["flags"]["truncated_empty"] and rec["flags"]["empty_content"])
        self.assertEqual(rig.replay.stats()["stored"], 1)
        self.assertEqual(header(rig.send(job), "x-mhforge-source"), "replay")
        self.assertEqual(rig.gw.close_job(job.job_id)["truncated_empty"], 2)

    def test_limit_exceeded_usage_does_not_halt(self):
        def rules(b):
            r = default_reply(b)
            r["usage"] = {"prompt_tokens": 9000, "completion_tokens": 16, "total_tokens": 9016}
            return r
        rig = Rig(self, rules=rules)
        reply = rig.send(rig.job(), body("tiny"))
        self.assertEqual(reply.status, 200)
        (row,) = rig.rows()
        self.assertLess(row["input_limit"], 9000)
        self.assertEqual(row["status"], "complete")
        self.assertIn("limit_exceeded", row["reason"])
        self.assertIsNone(rig.ledger.halted())
        self.assertEqual(rig.send(rig.job(candidate="b"), body("tiny 2")).status, 200)

    def test_rejected_attempts_do_not_use_up_the_request_ceiling(self):
        faults = [Fault("http", 503, when=w, times=2) for w in (1, 4, 7)]
        rig = Rig(self, faults=faults, **{"gateway.max_requests_per_job": 3})
        job = rig.job()
        for i in range(3):
            self.assertEqual(rig.send(job, body(f"p{i}")).status, 200)
        self.assertEqual(len(rig.stub.calls), 9)
        self.assertEqual(rig.ledger.job_requests(job.job_key), 3)
        self.assertEqual(code_of(rig.send(job, body("p3"))), "job_request_ceiling")
        self.assertEqual(len(rig.stub.calls), 9)

    def test_recovery_phase_only_after_a_charged_attempt(self):
        rig = Rig(self, faults=[Fault("http", 400, when=1, times=1)])
        a1 = rig.job(attempt=1)
        self.assertEqual(code_of(rig.send(a1, body("one"))), "provider_rejected")
        rig.gw.close_job(a1.job_id)
        a2 = rig.job(attempt=2)                          # attempt 1 was charged nothing: its own phase
        self.assertEqual(rig.send(a2, body("two")).status, 200)
        rig.gw.close_job(a2.job_id)
        a3 = rig.job(attempt=3)                          # attempt 2 was charged: recovery
        self.assertEqual(rig.send(a3, body("three")).status, 200)
        self.assertEqual([(r["job_id"][-2:], r["phase"]) for r in rig.rows()],
                         [("a1", "baseline"), ("a2", "baseline"), ("a3", "recovery")])
        q = rig.job(candidate="q", phase="qualification", attempt=2)
        self.assertEqual(rig.send(q, body("qual")).status, 200)
        self.assertEqual(rig.rows()[-1]["phase"], "qualification")


class Faults(unittest.TestCase):
    def test_settle_and_circuit_finish_each_run_when_the_other_raises(self):
        rig = Rig(self, ledger_cls=SettleFaultLedger)
        rig.ledger.exc = RuntimeError("disk gone")
        reply = rig.send(rig.job())
        self.assertEqual(reply.status, 200)                                   # paid and stored: still delivered
        self.assertEqual(rig.circuit.finishes, ["success"])
        self.assertEqual([r["status"] for r in rig.rows()], ["pending"])
        self.assertEqual(rig.gw.halted().reason, "ledger_fault")
        self.assertEqual(rig.permits(), 16)
        rig2 = Rig(self)
        rig2.circuit.raise_on_finish = True
        job = rig2.job()
        self.assertEqual(rig2.send(job).status, 200)
        self.assertEqual(rig2.circuit.finishes, ["success"])
        self.assertEqual([r["status"] for r in rig2.rows()], ["complete"])
        self.assertEqual(rig2.permits(), 16)
        rig2.circuit.raise_on_finish = False
        self.assertEqual(rig2.send(job, body("next")).status, 200)
        self.assertEqual(rig2.circuit.enters, 2)

    def test_client_disconnect_changes_nothing(self):
        gate = GateProvider(StubProvider())
        rig = Rig(self, provider=gate)
        job = rig.job()
        ingress = LoopbackIngress(rig.gw, rig.manifest)
        self.addCleanup(ingress.close_all)
        with util.loopback_guard():
            ep = ingress.open(job, None)
            raw = body("disconnecting worker")
            s = socket.create_connection((ep.host, ep.port), timeout=10)
            s.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n"
                      b"Content-Length: %d\r\n\r\n" % len(raw) + raw)
            self.assertTrue(gate.entered.acquire(timeout=10))
            s.close()                                                          # the worker goes away
            gate.gate.set()
            self.assertTrue(wait_until(lambda: rig.gw.in_flight() == 0 and any(
                r.get("outcome") for r in rig.records())))
            rec = rig.records()[0]
            self.assertEqual(rec["outcome"], "completed")
            self.assertEqual([r["status"] for r in rig.rows()], ["complete"])
            self.assertEqual(rig.replay.stats()["stored"], 1)
            conn = http.client.HTTPConnection(ep.host, ep.port, timeout=10)
            conn.request("POST", "/v1/chat/completions", body=raw, headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            self.assertEqual((resp.status, resp.getheader("x-mhforge-source")), (200, "replay"))
            resp.read()
            conn.close()
        self.assertEqual(len(gate.calls), 1)

    def test_unknown_job_recorded_under_unknown(self):
        rig = Rig(self)
        closed = rig.job("closed_one")
        rig.gw.close_job(closed.job_id)
        for job in (None, rig.job("never_registered", register=False), closed):
            reply = rig.send(job)
            self.assertEqual((reply.status, code_of(reply)), (403, "unknown_job"))
        recs = rig.records(None)
        self.assertEqual(len(recs), 3)
        self.assertEqual([r["refusal"]["code"] for r in recs], ["unknown_job"] * 3)
        self.assertEqual(sorted(r["refusal"]["detail"] for r in recs), ["job_closed", "unknown job", "unknown job"])
        self.assertEqual(rig.stub.calls, [])

    def test_refused_oversize_request_stores_excerpt_only(self):
        rig = Rig(self, **{"gateway.max_request_bytes": 4096, "gateway.max_content_bytes": 1000})
        job = rig.job()
        for raw, code in ((body("x" * 10_000), "request_too_large"), (body("y" * 2000), "request_too_large")):
            self.assertEqual(code_of(rig.send(job, raw)), code)
        recs = rig.records(job.job_id)
        self.assertEqual(len(recs), 2)
        for rec, size in zip(sorted(recs, key=lambda r: -r["request_excerpt"]["bytes"]), (10_000, 2000)):
            self.assertNotIn("request", rec)
            ex = rec["request_excerpt"]
            self.assertGreater(ex["bytes"], size)
            self.assertEqual(len(ex["head"]), 2048)
            self.assertEqual(len(ex["sha256"]), 64)
        self.assertEqual(sorted(r["content_bytes"] for r in recs if r["content_bytes"]), [2000 + 17])

    def test_record_written_for_every_outcome_and_has_no_authorization(self):
        gate = GateProvider(StubProvider(faults=[Fault("http", (400, {"message": "no", "code": "x"}), when=2),
                                                 Fault("http", 503, when=3, times=3)]))
        rig = Rig(self, provider=gate)
        job = rig.job()
        t, out = rig.in_thread(rig.send, job, body("C"))               # completed
        self.assertTrue(gate.entered.acquire(timeout=10))
        tf, of = rig.in_thread(rig.send, job, body("C"))               # coalesced
        time.sleep(0.3)
        gate.gate.set()
        t.join(10), tf.join(10)
        rig.send(job, body("C"))                                       # replayed
        rig.send(job, body("C", model="other"))                        # refused
        rig.send(job, body("R"))                                       # provider_rejected
        rig.send(job, body("F"))                                       # failed
        recs = {r["outcome"]: r for r in rig.records(job.job_id)}
        self.assertEqual(set(recs), {"completed", "coalesced", "replayed", "refused", "provider_rejected", "failed"})
        self.assertEqual(len(rig.records()), 6)                         # exactly one record per request
        fixture = {k: json.loads((RECORD_FIXTURES / f"{k}.json").read_text()) for k in
                   ("completed", "replayed", "refused", "provider_rejected", "failed")}
        fixture["coalesced"] = fixture["replayed"]
        attempt_keys = set(fixture["completed"]["attempts"][0])
        for outcome, rec in recs.items():
            with self.subTest(outcome=outcome):
                self.assertEqual(set(rec), set(fixture[outcome]))
                self.assertEqual(rec["schema"], "mhforge.call/1")
                for a in rec["attempts"]:
                    self.assertEqual(set(a), attempt_keys)
                self.assertEqual(set(rec["usage"] or fixture["completed"]["usage"]), set(fixture["completed"]["usage"]))
                self.assertEqual(set(rec["flags"]), set(fixture["completed"]["flags"]))
        for path in rig.paths.calls_root.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"authorization", path.read_bytes().lower())

    def test_internal_fault_becomes_gateway_internal(self):
        prov = RaisingProvider()
        rig = Rig(self, provider=prov)
        job = rig.job()
        reply = rig.send(job)
        self.assertEqual((reply.status, code_of(reply)), (403, "gateway_internal"))
        helpers.assert_neutral(reply)
        self.assertEqual(prov.calls, 1)                                   # never retried
        self.assertEqual([r["status"] for r in rig.rows()], ["bounded_unknown"])
        rec = rig.records()[0]
        self.assertEqual((rec["outcome"], rec["refusal"]["code"]), ("failed", "gateway_internal"))
        self.assertEqual(rig.permits(), 16)
        self.assertEqual(rig.circuit.finishes, ["neutral"])
        broken = Rig(self)
        broken.gw.replay = None                          # any fault inside handle: a reply, not a raise
        reply = broken.send(broken.job())
        self.assertEqual(code_of(reply), "gateway_internal")
        self.assertEqual(len(broken.records()), 1)

    def test_candidate_400_is_neutral_for_the_circuit(self):
        rig = Rig(self, faults=[Fault("http", (400, {"message": "bad", "code": "bad"}))])
        job = rig.job()
        for i in range(5):
            self.assertEqual(code_of(rig.send(job, body(f"q{i}"))), "provider_rejected")
        self.assertEqual(rig.circuit.state(), {"failures": 0, "until_epoch": 0.0, "probing": False})
        for _ in range(3):
            rig.circuit.enter(max_wait_s=1)
            rig.circuit.finish("failure")
        rig.clock.advance(61)
        self.assertEqual(code_of(rig.send(job, body("probe"))), "provider_rejected")   # the probe was neutral
        state = rig.circuit.state()
        self.assertEqual((state["failures"], state["probing"]), (3, False))
        self.assertEqual(rig.circuit.finishes[-1], "neutral")

    def test_ceiling_refusal_during_half_open_circuit_does_not_block_others(self):
        rig = Rig(self, **{"gateway.max_requests_per_job": 1})
        x = rig.job("job_x")
        others = [rig.job(f"job_y{i}") for i in range(4)]
        self.assertEqual(rig.send(x, body("first")).status, 200)
        for _ in range(3):
            rig.circuit.enter(max_wait_s=1)
            rig.circuit.finish("failure")
        rig.clock.advance(61)                                             # cool-down over: one probe is admitted
        self.assertEqual(code_of(rig.send(x, body("probe"))), "job_request_ceiling")
        self.assertFalse(rig.circuit.state()["probing"])
        threads = [rig.in_thread(rig.send, y, body(f"later {i}")) for i, y in enumerate(others)]
        for t, _ in threads:
            t.join(15)
            self.assertFalse(t.is_alive(), "a later request hung behind the refused probe")
        self.assertEqual([out[0].status for _, out in threads], [200] * 4)
        self.assertEqual(rig.circuit.state()["failures"], 0)

    def test_enter_timeout_is_gateway_internal(self):
        rig = Rig(self, **{"provider.circuit.cooldown_s": 10, "provider.attempt_deadline_s": 10})
        job = rig.job()
        for _ in range(3):
            rig.circuit.enter(max_wait_s=1)
            rig.circuit.finish("failure")
        rig.clock.advance(11)
        rig.circuit.enter(max_wait_s=1)                                   # another caller holds the probe
        t, out = rig.in_thread(rig.send, job)
        time.sleep(0.2)
        rig.clock.advance(100)                                            # past cooldown + deadline + 60
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertEqual(code_of(out[0]), "gateway_internal")
        self.assertEqual(rig.rows(), [])
        self.assertEqual(rig.permits(), 16)
        rig.circuit.finish("success")

    def test_halt_during_backoff_does_not_change_permit_count(self):
        clock = HookClock()
        rig = Rig(self, clock=clock, faults=[Fault("http", 503)], **{"gateway.max_in_flight": 4})
        clock.hooks.append(lambda: rig.ledger.set_halt("operator", "during backoff"))
        reply = rig.send(rig.job())
        self.assertEqual(code_of(reply), "halted")
        self.assertEqual(len(rig.stub.calls), 1)
        self.assertEqual(rig.permits(), 4)
        rig.gw._sem.acquire()
        rig.gw._sem.release()
        with self.assertRaises(ValueError):                              # still exactly max_in_flight permits
            rig.gw._sem.release()
        self.assertEqual(rig.records()[0]["outcome"], "failed")

    def test_exception_in_attempt_releases_slot(self):
        rig = Rig(self, provider=RaisingProvider(), **{"gateway.max_in_flight": 2})
        job = rig.job()
        for i in range(5):
            self.assertEqual(code_of(rig.send(job, body(f"e{i}"))), "gateway_internal")
        self.assertEqual(rig.permits(), 2)
        with self.assertRaises(ValueError):
            rig.gw._sem.release()

    def test_store_before_settle_crash_replays_free(self):
        rig = Rig(self, ledger_cls=SettleFaultLedger)
        rig.ledger.exc = Crash()
        job = rig.job()
        with self.assertRaises(Crash):
            rig.send(job)
        self.assertEqual(rig.replay.stats()["stored"], 1)
        self.assertEqual([r["status"] for r in rig.rows()], ["pending"])
        rerun = Rig(self, paths=rig.paths)                              # a new runner on the same state
        self.assertEqual(rerun.ledger.interrupt_pending(), 1)
        rerun.ledger.close_open_jobs()
        job2 = rerun.job(attempt=2)
        reply = rerun.send(job2)
        self.assertEqual((reply.status, header(reply, "x-mhforge-source")), (200, "replay"))
        self.assertEqual(rerun.stub.calls, [])
        self.assertEqual([r["status"] for r in rerun.rows()], ["bounded_unknown"])

    def test_busy_ledger_at_settle_delivers_reply_and_halts_ledger_fault(self):
        rig = Rig(self, ledger_cls=SettleFaultLedger)
        rig.ledger.exc = sqlite3.OperationalError("database is locked")
        job = rig.job()
        reply = rig.send(job)
        self.assertEqual(reply.status, 200)
        self.assertEqual(rig.clock.sleeps, [30.0, 30.0, 30.0])           # three retries over 90 s
        self.assertEqual([r["status"] for r in rig.rows()], ["pending"])
        self.assertEqual(rig.gw.halted().reason, "ledger_fault")
        self.assertEqual(rig.ledger.halted().reason, "ledger_fault")
        rec = rig.records()[0]
        self.assertEqual((rec["outcome"], rec["cost_basis"]), ("completed", "reservation"))
        self.assertEqual(code_of(rig.send(job, body("next"))), "halted")
        rig2 = Rig(self, ledger_cls=SettleFaultLedger, faults=[Fault("http", 400)])
        rig2.ledger.exc = sqlite3.OperationalError("database is locked")
        reply = rig2.send(rig2.job())
        self.assertEqual(code_of(reply), "gateway_internal")             # never usage_contradiction
        self.assertEqual(rig2.gw.halted().reason, "ledger_fault")
        rig3 = Rig(self, ledger_cls=SettleFaultLedger)
        rig3.ledger.exc, rig3.ledger.times = sqlite3.OperationalError("database is locked"), 2
        self.assertEqual(rig3.send(rig3.job()).status, 200)
        self.assertEqual([r["status"] for r in rig3.rows()], ["complete"])
        self.assertIsNone(rig3.gw.halted())

    def test_same_request_failing_in_two_attempts_becomes_candidate_failure(self):
        clock = HookClock()
        good, bad = StubProvider(clock=clock), StubProvider(clock=clock, faults=[Fault("http", 500)])
        rig = Rig(self, clock=clock, provider=RoutingProvider(good, bad), **{"provider.circuit.failures": 100})
        a1 = rig.job(attempt=1)
        served: list = []
        clock.hooks.append(lambda: served.append(rig.send(a1, body("GOOD"))))     # the provider serves others meanwhile
        first = rig.send(a1, body("BAD"))
        self.assertEqual(served[0].status, 200)
        self.assertEqual(code_of(first), "provider_unavailable")
        rig.gw.close_job(a1.job_id)
        a2 = rig.job(attempt=2)
        second = rig.send(a2, body("BAD"))
        self.assertEqual(code_of(second), "provider_rejected")
        helpers.assert_neutral(second)
        rec = [r for r in rig.records(a2.job_id) if r["refusal"]][0]
        self.assertEqual((rec["outcome"], rec["refusal"]["detail"]), ("failed", "repeatable_provider_failure"))
        self.assertEqual(rig.gw.close_job(a2.job_id)["repeatable_provider_failures"], 1)
        lonely = Rig(self, faults=[Fault("http", 500)], **{"provider.circuit.failures": 100})   # nothing else served
        for attempt in (1, 2):
            j = lonely.job(attempt=attempt)
            self.assertEqual(code_of(lonely.send(j, body("BAD"))), "provider_unavailable")
            lonely.gw.close_job(j.job_id)


class Lifecycle(unittest.TestCase):
    def test_close_job_counters(self):
        rig = Rig(self, faults=[Fault("http", (400, {"message": "no", "code": "x"}), when=2),
                                Fault("http", 503, when=3, times=3)])
        job = rig.job()
        rig.send(job, body("a"))                 # completed
        rig.send(job, body("a"))                 # replayed
        rig.send(job, body("b"))                 # provider_rejected
        rig.send(job, body("c"))                 # failed
        rig.send(job, body("d", model="x"))      # refused
        counters = rig.gw.close_job(job.job_id)
        keys = ("completed", "replayed", "coalesced", "refused", "refused_unrecorded", "provider_rejected", "failed",
                "in_flight_at_close")
        self.assertEqual({k: counters[k] for k in keys},
                         {"completed": 1, "replayed": 1, "coalesced": 0, "refused": 1, "refused_unrecorded": 0,
                          "provider_rejected": 1, "failed": 1, "in_flight_at_close": 0})
        self.assertEqual(counters["refusal_codes"], {"provider_rejected": 1, "provider_unavailable": 1,
                                                     "model_not_allowed": 1})
        self.assertEqual(counters["cost_micro"], rig.ledger.committed())
        self.assertEqual(set(counters) - {"refusal_codes"}, set(gw_mod.COUNTER_KEYS))
        self.assertEqual(code_of(rig.send(job, body("late"))), "unknown_job")
        self.assertTrue(rig.ledger.is_open(job.job_id) is False)

    def test_close_job_reports_prompt_token_counters(self):
        rig = Rig(self, faults=[Fault("no_usage", when=3)])
        job = rig.job()
        texts = ["one", "three four five six seven", "x"]
        for t in texts:
            self.assertEqual(rig.send(job, body(t)).status, 200)
        rig.send(job, body("one"))                                          # replay: not a completed call
        c = rig.gw.close_job(job.job_id)
        prompts = [default_reply(json.loads(body(t)))["usage"]["prompt_tokens"] for t in texts[:2]]
        self.assertEqual((c["usage_calls"], c["prompt_tokens_sum"], c["prompt_tokens_max"], c["completion_tokens_sum"]),
                         (2, sum(prompts), max(prompts), 32))

    def test_close_job_waits_for_in_flight_and_counts_them(self):
        gate = GateProvider(StubProvider())
        rig = Rig(self, provider=gate)
        job = rig.job()
        t, out = rig.in_thread(rig.send, job)
        self.assertTrue(gate.entered.acquire(timeout=10))
        counters = rig.gw.close_job(job.job_id, drain_timeout_s=0.2)
        self.assertEqual(counters["in_flight_at_close"], 1)
        gate.gate.set()
        t.join(10)
        self.assertEqual(out[0].status, 200)                               # it finished, settled and stored on its own
        self.assertEqual([r["status"] for r in rig.rows()], ["complete"])
        gate2 = GateProvider(StubProvider())
        rig2 = Rig(self, provider=gate2)
        job2 = rig2.job()
        t2, out2 = rig2.in_thread(rig2.send, job2)
        self.assertTrue(gate2.entered.acquire(timeout=10))
        threading.Timer(0.3, gate2.gate.set).start()
        started = time.monotonic()
        counters = rig2.gw.close_job(job2.job_id, drain_timeout_s=10)
        self.assertGreaterEqual(time.monotonic() - started, 0.25)
        self.assertEqual((counters["in_flight_at_close"], counters["completed"]), (0, 1))
        t2.join(10)

    def test_closed_job_followers_and_waiters_leave(self):
        gate = GateProvider(StubProvider())
        rig = Rig(self, provider=gate, **{"gateway.max_in_flight": 1})
        job = rig.job()
        tl, ol = rig.in_thread(rig.send, job, body("lead"))
        self.assertTrue(gate.entered.acquire(timeout=10))
        tf, of = rig.in_thread(rig.send, job, body("lead"))                # follower
        tq, oq = rig.in_thread(rig.send, job, body("queued"))              # semaphore waiter
        time.sleep(0.3)
        started = time.monotonic()
        rig.gw.close_job(job.job_id, drain_timeout_s=0.1)
        tf.join(5), tq.join(5)
        self.assertLess(time.monotonic() - started, 3)
        for out in (of, oq):
            self.assertEqual(code_of(out[0]), "unknown_job")
        self.assertEqual({r["refusal"]["detail"] for r in rig.records(job.job_id) if r["refusal"]}, {"job_closed"})
        gate.gate.set()
        tl.join(10)
        self.assertEqual(ol[0].status, 200)
        self.assertEqual(len(gate.calls), 1)

    def test_abort_queued_and_drain(self):
        gate = GateProvider(StubProvider())
        rig = Rig(self, provider=gate, **{"gateway.max_in_flight": 1})
        job = rig.job()
        tl, ol = rig.in_thread(rig.send, job, body("lead"))
        self.assertTrue(gate.entered.acquire(timeout=10))
        tf, of = rig.in_thread(rig.send, job, body("lead"))
        tq, oq = rig.in_thread(rig.send, job, body("queued"))
        time.sleep(0.3)
        rig.gw.abort_queued()
        tf.join(5), tq.join(5)
        self.assertEqual([code_of(of[0]), code_of(oq[0])], ["halted", "halted"])
        self.assertEqual(rig.gw.drain(0.1), 1)
        self.assertEqual(code_of(rig.send(job, body("new"))), "halted")
        gate.gate.set()
        self.assertEqual(rig.gw.drain(10), 0)
        tl.join(10)
        self.assertEqual(ol[0].status, 200)
        self.assertEqual(rig.pending(), 0)
        self.assertEqual(len(gate.calls), 1)

    def test_abort_during_circuit_wait_starts_no_attempt_after_drain(self):
        rig = Rig(self)
        job = rig.job()
        for _ in range(3):
            rig.circuit.enter(max_wait_s=1)
            rig.circuit.finish("failure")                                  # open for 60 s on the fake clock
        t, out = rig.in_thread(rig.send, job)
        time.sleep(0.3)                                                    # the leader is blocked in circuit.enter
        rig.gw.abort_queued()
        self.assertEqual(rig.gw.drain(0.2), 0)
        rig.clock.advance(61)
        t.join(10)
        self.assertEqual(code_of(out[0]), "halted")
        self.assertEqual(rig.stub.calls, [])                               # drain said 0: nothing may start later
        self.assertEqual(rig.rows(), [])
        self.assertEqual(rig.permits(), 16)

    def test_close_job_counts_the_call_it_waited_for(self):
        gate = GateProvider(StubProvider())
        rig = Rig(self, provider=gate)
        orig = rig.gw._finish

        def slow_finish(call, end):                                        # widen the window before the counting
            time.sleep(0.3)
            return orig(call, end)
        rig.gw._finish = slow_finish
        job = rig.job()
        t, _ = rig.in_thread(rig.send, job)
        self.assertTrue(gate.entered.acquire(timeout=10))
        threading.Timer(0.2, gate.gate.set).start()
        c = rig.gw.close_job(job.job_id, drain_timeout_s=10)
        t.join(10)
        self.assertEqual((c["completed"], c["in_flight_at_close"], c["usage_calls"]), (1, 0, 1))

    def test_refused_records_capped_and_kill_requested(self):
        rig = Rig(self, **{"gateway.max_refused_records_per_job": 3})
        job = rig.job()
        for i in range(29):
            self.assertEqual(code_of(rig.send(job, body(f"x{i}", model="nope"))), "model_not_allowed")
            self.assertIsNone(rig.gw.kill_requested(job.job_id))
        self.assertEqual(code_of(rig.send(job, body("x29", model="nope"))), "model_not_allowed")
        self.assertEqual(rig.gw.kill_requested(job.job_id), "refusal_flood")
        self.assertEqual(len(rig.records(job.job_id)), 3)
        rig.ledger.set_halt("operator")                                     # refusals after admission are capped too
        rig.send(job, body("y"))
        self.assertEqual(len(rig.records(job.job_id)), 3)
        c = rig.gw.close_job(job.job_id)
        self.assertEqual((c["refused"], c["refused_unrecorded"]), (3, 28))
        self.assertEqual(c["refusal_codes"], {"model_not_allowed": 30, "halted": 1})
        other = rig.job("other")
        self.assertIsNone(rig.gw.kill_requested(other.job_id))


if __name__ == "__main__":
    unittest.main()
