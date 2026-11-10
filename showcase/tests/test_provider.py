# SPDX-License-Identifier: MIT
"""provider.py (spec 3.9), against stubprovider.serve_stub on loopback."""

from __future__ import annotations

import hashlib
import io
import json
import pickle
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mhforge import provider, sse, stubprovider as sp, util
from mhforge.errors import ProviderConfigFault
from mhforge.provider import (ForgeProviderClient, ProviderError, ProviderResult, circuit_outcome, classify,
                              is_context_limit, settle_basis)
from mhforge.stubprovider import Fault
from tests import helpers

KEY_ENV = "MHFORGE_FORGE_API_KEY"
MODEL = "openai/gpt-oss-120b"
BODY = {"model": MODEL, "messages": [{"role": "system", "content": "Reasoning: medium"},
                                     {"role": "user", "content": "Patient reports a sore throat."}],
        "temperature": 0.0, "max_tokens": 16384}
USAGE = {"prompt_tokens": 10, "completion_tokens": 16, "total_tokens": 26}


def _choice(content="ok", finish="stop"):
    return [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish}]


def _res(status=200, *, merged=None, done=True, error=None):
    if merged is None and status == 200:
        merged = {"choices": _choice(), "usage": dict(USAGE)}
    return ProviderResult(http_status=status, headers={}, merged=merged, done=done, error=error, first_byte_s=None,
                          total_s=0.0, received_bytes=0, reasoning_field=None)


def _http(status, code=None, message="x"):
    body = {"error": {"message": message, "code": code}}
    return _res(status, merged=None, done=False, error=ProviderError("http", status, code, json.dumps(body)))


def _stream_err(code, message="boom", usage=None):
    merged = {"choices": [], "error": {"code": code, "message": message}}
    if usage is not None:
        merged["usage"] = usage
    return _res(200, merged=merged, done=True,
                error=ProviderError("stream_error", 200, str(code), json.dumps(merged["error"])))


class _Server(unittest.TestCase):
    """Starts serve_stub per test under the loopback guard."""

    rules = staticmethod(sp.default_reply)
    faults: tuple = ()

    def setUp(self):
        self.enterContext(util.loopback_guard())
        self.server, self.base_url = sp.serve_stub(rules=self.rules, faults=self.faults)
        self.addCleanup(self.server.close)

    def start(self, faults=(), rules=sp.default_reply):
        server, url = sp.serve_stub(rules=rules, faults=faults)
        self.addCleanup(server.close)
        return server, url

    def client(self, url=None, key="sk-test-0123456789abcdef", **overrides):
        manifest = helpers.make_manifest(**{"provider.kind": "stub", "provider.base_url": url or self.base_url,
                                            **overrides})
        env = {KEY_ENV: key} if key is not None else {}
        return ForgeProviderClient(manifest, environ=env)

    def call(self, client, body=BODY, deadline_s=30.0):
        sink = io.BytesIO()
        result = client.complete(body, sse_sink=sink, deadline_s=deadline_s)
        return result, sink.getvalue()


class OpenAIProjectRule(unittest.TestCase):
    """provider.py may not import manifest.py, so it repeats manifest.openai_project: the two must agree."""

    def test_header_value_matches_manifest_openai_project(self):
        from mhforge import manifest
        url = "http://127.0.0.1:8080/v1"
        cases = [{}, {"provider.openai_project": "team/proj-x"}, {"provider.openai_project": "solo"},
                 {"decisions.entity": "ent", "decisions.project": "proj"},
                 {"decisions.entity": "ent-2", "decisions.project": "p.q_r", "provider.openai_project": None},
                 {"provider.kind": "stub", "provider.base_url": url},
                 {"provider.kind": "stub", "provider.base_url": url, "provider.openai_project": "a/b"}]
        for overrides in cases:
            with self.subTest(overrides=overrides):
                m = helpers.make_manifest(**overrides)
                client = ForgeProviderClient(m, environ={KEY_ENV: "sk-project-0123456789"})
                self.assertEqual(client._project, manifest.openai_project(m))


class ClassifyTable(unittest.TestCase):
    def test_classify_table(self):
        too_large = _res(200, merged={"choices": _choice()}, done=False,
                         error=ProviderError("too_large", 200, None, "big"))
        rows = [
            ("200 done", _res(), "ok", "usage", "success"),
            ("200 finish+usage no done", _res(done=False), "ok", "usage", "success"),
            ("200 error chunk 400 context", _stream_err(400, "maximum context length is 131072"),
             "context_limit", "rejected", "neutral"),
            ("200 error chunk 422", _stream_err(422, "bad"), "provider_rejected", "rejected", "neutral"),
            ("200 error chunk 500", _stream_err(500), "retry_transport", "bounded_unknown", "failure"),
            ("200 error chunk 429", _stream_err(429), "retry_transport", "bounded_unknown", "failure"),
            ("200 error chunk 408", _stream_err(408), "retry_transport", "bounded_unknown", "failure"),
            ("200 error chunk 500 with usage", _stream_err(500, usage=dict(USAGE)), "retry_transport", "usage",
             "failure"),
            ("200 no choice", _res(merged={"choices": []}, done=True), "retry_transport", "bounded_unknown",
             "failure"),
            ("200 no done no finish", _res(merged={"choices": _choice(finish=None), "usage": dict(USAGE)}, done=False,
                                           error=ProviderError("truncated", 200, None, "cut")),
             "retry_transport", "usage", "failure"),
            ("200 bad_json", _res(merged={"choices": _choice()}, done=True,
                                  error=ProviderError("bad_json", 200, None, "data: {")),
             "retry_transport", "bounded_unknown", "failure"),
            ("200 timeout", _res(merged={"choices": []}, done=False, error=ProviderError("timeout", 200, None, "t")),
             "retry_transport", "bounded_unknown", "failure"),
            ("200 too_large", too_large, "provider_rejected", "bounded_unknown", "neutral"),
            ("400 context", _http(400, None, "This model's maximum context length is 131072 tokens"),
             "context_limit", "rejected", "neutral"),
            ("400 other", _http(400, None, "bad request"), "provider_rejected", "rejected", "neutral"),
            ("413", _http(413), "provider_rejected", "rejected", "neutral"),
            ("422", _http(422), "provider_rejected", "rejected", "neutral"),
            ("401", _http(401), "config_fault", "rejected", "neutral"),
            ("402", _http(402), "config_fault", "rejected", "neutral"),
            ("403", _http(403), "config_fault", "rejected", "neutral"),
            ("429 quota", _http(429, "insufficient_quota"), "quota_exhausted", "rejected", "neutral"),
            ("429 other", _http(429, "rate_limit_exceeded", "slow down"), "retry_rate_limited", "rejected",
             "failure"),
            ("404", _http(404), "retry_transport", "rejected", "failure"),
            ("408", _http(408), "retry_transport", "rejected", "failure"),
            ("409", _http(409), "retry_transport", "rejected", "failure"),
            ("500", _http(500), "retry_transport", "rejected", "failure"),
            ("503", _http(503), "retry_transport", "rejected", "failure"),
            ("302", _http(302), "retry_transport", "rejected", "failure"),
            ("connect", _res(None, merged=None, done=False, error=ProviderError("connect", None, None, "refused")),
             "retry_transport", "rejected", "failure"),
            ("timeout no status", _res(None, merged=None, done=False, error=ProviderError("timeout", None, None, "t")),
             "retry_transport", "bounded_unknown", "failure"),
        ]
        for label, result, disposition, settle, circuit in rows:
            with self.subTest(label):
                got = classify(result, terminal_429_patterns=["quota"])
                self.assertEqual(got, disposition)
                self.assertIn(got, provider.DISPOSITIONS)
                self.assertEqual(settle_basis(result, got), settle)
                self.assertEqual(circuit_outcome(got), circuit)

    def test_context_limit_detection(self):
        for phrase in ("context length", "context_length", "maximum context", "input tokens"):
            with self.subTest(phrase=phrase):
                self.assertTrue(is_context_limit(ProviderError("http", 400, None, f"Too many {phrase.upper()} here")))
                self.assertTrue(is_context_limit(ProviderError("stream_error", 200, "400", f"x {phrase} y")))
        self.assertFalse(is_context_limit(ProviderError("http", 400, None, "invalid temperature")))
        self.assertFalse(is_context_limit(ProviderError("http", 413, None, "maximum context length")))
        self.assertFalse(is_context_limit(ProviderError("http", 500, None, "maximum context length")))
        self.assertFalse(is_context_limit(ProviderError("stream_error", 200, "500", "maximum context length")))
        self.assertFalse(is_context_limit(ProviderError("timeout", None, None, "maximum context length")))
        # the fixture's error-only stream, through the production parser
        acc = provider.StreamAccumulator()
        for line in (helpers.FIXTURES / "sse_error_only.txt").read_bytes().splitlines(keepends=True):
            acc.feed(line)
        merged, error, _ = acc.finish(200, None)
        result = _res(200, merged=merged, done=acc.done, error=error)
        self.assertEqual(error.kind, "stream_error")
        self.assertEqual(error.code, "400")
        self.assertTrue(is_context_limit(error))
        self.assertEqual(classify(result), "context_limit")

    def test_classification_order_status_code_message(self):
        pats = ["quota"]
        # status first: the text of a 500 or a 401 does not matter
        self.assertEqual(classify(_http(500, None, "maximum context length exceeded"), terminal_429_patterns=pats),
                         "retry_transport")
        self.assertEqual(classify(_http(401, "insufficient_quota", "quota"), terminal_429_patterns=pats),
                         "config_fault")
        self.assertEqual(classify(_http(503, "insufficient_quota"), terminal_429_patterns=pats), "retry_transport")
        # then the code: a quota code wins over a message that reads like a rate limit
        self.assertEqual(classify(_http(429, "insufficient_quota", "rate limit, retry in 2s"), terminal_429_patterns=()),
                         "quota_exhausted")
        self.assertEqual(classify(_http(429, "quota_exceeded", "slow down")), "quota_exhausted")
        # the message only when status and code say nothing more
        self.assertEqual(classify(_http(429, "rate_limit_exceeded", "monthly quota reached"),
                                  terminal_429_patterns=pats), "quota_exhausted")
        self.assertEqual(classify(_http(429, "rate_limit_exceeded", "monthly quota reached")), "retry_rate_limited")
        self.assertEqual(classify(_http(429, "rate_limit_exceeded", "please retry"), terminal_429_patterns=pats),
                         "retry_rate_limited")
        # a 200 error chunk: the integer code decides, not the wording
        self.assertEqual(classify(_stream_err(500, "maximum context length")), "retry_transport")
        self.assertEqual(classify(_stream_err("server_error", "maximum context length")), "retry_transport")
        # review regression: only an integer code counts; a digit string books bounded_unknown, not rejected
        for raw in ("400", "422", True):
            with self.subTest(code=raw):
                result = _stream_err(raw, "maximum context length")
                self.assertEqual(classify(result), "retry_transport")
                self.assertEqual(settle_basis(result, "retry_transport"), "bounded_unknown")


class Requests(_Server):
    def test_request_is_exact(self):
        key = "sk-exact-" + secrets.token_hex(8)
        client = self.client(key=key)
        body = dict(BODY, usage={"include": True}, response_format={"type": "json_object"})
        result, _ = self.call(client, body)
        self.assertEqual(classify(result), "ok")
        req = self.server.requests[-1]
        self.assertEqual(req["method"], "POST")
        self.assertEqual(req["path"], "/v1/chat/completions")
        self.assertEqual(sorted(req["header_names"]), sorted(
            ["host", "authorization", "content-type", "accept", "openai-project", "user-agent", "content-length"]))
        self.assertEqual(len(req["header_names"]), 7)
        self.assertEqual(req["authorization_sha256"], hashlib.sha256(("Bearer " + key).encode()).hexdigest())
        self.assertEqual(req["headers"]["content-type"], "application/json")
        self.assertEqual(req["headers"]["accept"], "text/event-stream")
        self.assertEqual(req["headers"]["openai-project"], "example-entity/mh-forge-test")
        self.assertEqual(req["headers"]["user-agent"], "mhforge-gateway/0.1 (meta-harness forge showcase)")
        expected = {k: v for k, v in BODY.items()}
        expected.update(stream=True, stream_options={"include_usage": True})
        self.assertEqual(req["body"], expected)
        self.assertNotIn("response_format", req["body"])
        self.assertNotIn("usage", req["body"])
        # an explicit provider.openai_project is sent as it is
        self.call(self.client(**{"provider.openai_project": "team/proj-x"}))
        self.assertEqual(self.server.requests[-1]["headers"]["openai-project"], "team/proj-x")

    def test_raw_sse_saved_and_holds_no_request_header(self):
        key = "sk-raw-" + secrets.token_hex(8)
        result, raw = self.call(self.client(key=key))
        self.assertTrue(result.done)
        rendered = sp.render_sse(sp.default_reply(BODY), model=MODEL)
        self.assertEqual(raw, rendered[:len(raw)])
        self.assertTrue(raw.endswith(b"data: [DONE]\n"))
        self.assertEqual(result.received_bytes, len(raw))
        for needle in (b"Authorization", b"authorization", b"Bearer", key.encode(), b"OpenAI-Project",
                       b"User-Agent", b"mhforge-gateway", b"example-entity", b"stream_options"):
            self.assertNotIn(needle, raw)
        self.assertEqual(result.headers.get("content-type"), "text/event-stream")
        self.assertNotIn("server", result.headers)      # not on the allow-list
        self.assertEqual(result.reasoning_field, "reasoning")

    def test_stream_without_done_but_with_finish_reason_and_usage_is_ok(self):
        server, url = self.start(rules=lambda body: dict(sp.default_reply(body), done=False))
        result, raw = self.call(self.client(url))
        self.assertNotIn(b"[DONE]", raw)
        self.assertFalse(result.done)
        self.assertIsNone(result.error)
        self.assertEqual(classify(result), "ok")
        self.assertEqual(settle_basis(result, "ok"), "usage")
        # the same without usage is not complete
        server2, url2 = self.start(rules=lambda body: {k: v for k, v in sp.default_reply(body).items()
                                                       if k != "usage"} | {"done": False})
        result2, _ = self.call(self.client(url2))
        self.assertEqual(result2.error.kind, "truncated")
        self.assertEqual(classify(result2), "retry_transport")
        self.assertEqual(settle_basis(result2, "retry_transport"), "bounded_unknown")

    def test_oversize_stream_aborted(self):
        server, url = self.start(rules=lambda body: dict(sp.default_reply(body), content="y" * 5000))
        result, raw = self.call(self.client(url, **{"provider.max_response_bytes": 1500}))
        self.assertEqual(result.error.kind, "too_large")
        self.assertLessEqual(len(raw), 1500)
        self.assertLessEqual(result.received_bytes, 1501)
        self.assertEqual(classify(result), "provider_rejected")
        self.assertEqual(settle_basis(result, "provider_rejected"), "bounded_unknown")

    def test_redirect_is_an_error(self):
        for status in (301, 302, 307, 308):
            with self.subTest(status=status):
                server, url = self.start(faults=[Fault("http", status)])
                result, _ = self.call(self.client(url))
                self.assertEqual(result.http_status, status)
                self.assertEqual(result.error.kind, "http")
                self.assertEqual(len(server.requests), 1)          # never followed
                self.assertEqual(classify(result), "retry_transport")

    def test_first_byte_and_idle_timeouts(self):
        fast = {"provider.first_byte_timeout_s": 0.3, "provider.idle_timeout_s": 0.3}
        server, url = self.start(faults=[Fault("hang", 5.0)])
        t0 = time.monotonic()
        result, raw = self.call(self.client(url, **fast))
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.error.kind, "timeout")
        self.assertIsNone(result.first_byte_s)
        self.assertEqual(raw, b"")
        self.assertEqual(classify(result), "retry_transport")
        self.assertEqual(settle_basis(result, "retry_transport"), "bounded_unknown")
        # idle: two data lines, then silence
        server, url = self.start(faults=[Fault("hang", (5.0, 2))])
        t0 = time.monotonic()
        result, raw = self.call(self.client(url, **{"provider.first_byte_timeout_s": 5, "provider.idle_timeout_s": 0.3}))
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertEqual(result.error.kind, "timeout")
        self.assertIsNotNone(result.first_byte_s)
        self.assertEqual(raw.count(b"data:"), 2)
        self.assertEqual(classify(result), "retry_transport")
        # the attempt deadline bounds a stream whose own timeouts are long
        server, url = self.start(faults=[Fault("hang", 5.0)])
        t0 = time.monotonic()
        result, _ = self.call(self.client(url), deadline_s=0.4)
        self.assertLess(time.monotonic() - t0, 3.0)
        self.assertEqual(result.error.kind, "timeout")

    def test_trickling_peer_cannot_outlive_the_attempt_deadline(self):
        # review regression: a byte every 0.1 s resets each socket timeout; the attempt must still end at deadline_s
        for mode in ("stream", "headers"):
            with self.subTest(mode=mode):
                listener = socket.socket()
                listener.bind(("127.0.0.1", 0))
                listener.listen(4)
                self.addCleanup(listener.close)
                stop = threading.Event()
                self.addCleanup(stop.set)

                def serve(mode=mode, listener=listener, stop=stop):
                    conn, _ = listener.accept()
                    try:
                        conn.recv(65536)
                        if mode == "stream":
                            conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: text/event-stream\r\n\r\n"
                                         b'data: {"choices":[{"index":0,"delta":{"content":"a"}}]}\n\ndata: ')
                        else:
                            conn.sendall(b"HTTP/1.0 200 OK\r\nX-Slow: ")
                        while not stop.is_set():
                            conn.sendall(b"x")
                            time.sleep(0.1)
                    except OSError:
                        pass
                    finally:
                        conn.close()

                threading.Thread(target=serve, daemon=True).start()
                client = self.client(f"http://127.0.0.1:{listener.getsockname()[1]}/v1",
                                     **{"provider.first_byte_timeout_s": 1, "provider.idle_timeout_s": 1,
                                        "provider.max_response_bytes": 10_000_000})
                out = {}
                worker = threading.Thread(target=lambda: out.update(r=self.call(client, deadline_s=1.0)[0]),
                                          daemon=True)
                t0 = time.monotonic()
                worker.start()
                worker.join(6)
                self.assertIn("r", out, "complete() still blocked past the attempt deadline")
                self.assertLess(time.monotonic() - t0, 4.0)
                self.assertEqual(classify(out["r"]), "retry_transport")
                self.assertNotEqual(settle_basis(out["r"], "retry_transport"), "rejected")

    def test_never_raises_on_network_error(self):
        # nothing listens: connect
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        result, _ = self.call(self.client(f"http://127.0.0.1:{port}/v1"))
        self.assertIsNone(result.http_status)
        self.assertEqual(result.error.kind, "connect")
        self.assertEqual(settle_basis(result, classify(result)), "rejected")
        # a listener that closes without a status line: booked as a timeout with no status line
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        self.addCleanup(listener.close)

        def accept_and_close():
            conn, _ = listener.accept()
            conn.recv(65536)
            conn.close()

        threading.Thread(target=accept_and_close, daemon=True).start()
        result, _ = self.call(self.client(f"http://127.0.0.1:{listener.getsockname()[1]}/v1"))
        self.assertIsNone(result.http_status)
        self.assertEqual(result.error.kind, "timeout")
        self.assertEqual(classify(result), "retry_transport")
        self.assertEqual(settle_basis(result, "retry_transport"), "bounded_unknown")
        # a stream cut in the middle of a line
        server, url = self.start(faults=[Fault("truncate")])
        result, _ = self.call(self.client(url))
        self.assertEqual(result.http_status, 200)
        self.assertIn(result.error.kind, ("bad_json", "truncated"))
        self.assertEqual(classify(result), "retry_transport")

    def test_reasoning_field_and_headers_allow_list(self):
        server, url = self.start(faults=[Fault("reasoning_content"), Fault("rate_limit", when=2)])
        client = self.client(url)
        result, _ = self.call(client)
        self.assertEqual(result.reasoning_field, "reasoning_content")
        self.assertTrue(result.merged["choices"][0]["message"]["reasoning"])
        limited, _ = self.call(client)
        self.assertEqual(limited.http_status, 429)
        self.assertEqual(limited.headers.get("retry-after"), "1")
        self.assertEqual(set(limited.headers) - {"content-type", "date", "retry-after"}, set())


class Keys(_Server):
    def test_key_removed_from_mapping_after_init_and_absent_from_repr(self):
        key = "sk-repr-" + secrets.token_hex(12)
        env = {KEY_ENV: key, "OTHER": "kept"}
        manifest = helpers.make_manifest(**{"provider.kind": "stub", "provider.base_url": self.base_url})
        client = ForgeProviderClient(manifest, environ=env)
        self.assertEqual(env, {"OTHER": "kept"})
        self.assertNotIn(key, repr(client))
        self.assertIn(KEY_ENV, repr(client))
        self.assertNotIn(key, json.dumps(client.key_facts))
        self.assertEqual(client.key_facts["len"], len(key))
        self.assertEqual(client.key_facts["sha256"], hashlib.sha256(key.encode()).hexdigest())
        self.assertNotIn(key, json.dumps(manifest))
        with self.assertRaises(TypeError):
            pickle.dumps(client)
        # the key reaches the server as the bearer
        self.call(client)
        self.assertEqual(self.server.requests[-1]["authorization_sha256"],
                         hashlib.sha256(("Bearer " + key).encode()).hexdigest())

    def test_scrub_function_called_with_variable_name_and_result_in_key_facts(self):
        manifest = helpers.make_manifest(**{"provider.kind": "stub", "provider.base_url": self.base_url})
        for outcome in (True, False):
            with self.subTest(outcome=outcome):
                seen = []

                def scrub(name, _outcome=outcome, _env=None):
                    seen.append((name, name in env))
                    return _outcome

                env = {KEY_ENV: "sk-scrub-1234567890"}
                client = ForgeProviderClient(manifest, environ=env, scrub=scrub)
                self.assertEqual(seen, [(KEY_ENV, True)])        # called with the name, before the delete
                self.assertNotIn(KEY_ENV, env)
                self.assertIs(client.key_facts["scrubbed"], outcome)
                self.assertEqual(client.key_facts["name"], KEY_ENV)
        client = ForgeProviderClient(manifest, environ={KEY_ENV: "sk-noscrub-123456"})
        self.assertIsNone(client.key_facts["scrubbed"])

    def test_key_in_error_body_is_replaced(self):
        key = "sk-echo-" + secrets.token_hex(10)
        server, url = self.start(faults=[Fault("http", (401, {"message": f"invalid api key {key} for project",
                                                             "code": "invalid_api_key"}), when=1)],
                                 rules=lambda body: dict(sp.default_reply(body), content=f"echo {key} done"))
        client = self.client(url, key=key)
        result, raw = self.call(client)
        self.assertEqual(result.http_status, 401)
        self.assertNotIn(key, result.error.body_excerpt)
        self.assertIn("[key]", result.error.body_excerpt)
        self.assertNotIn(key.encode(), raw)
        self.assertIn(b"[key]", raw)
        self.assertEqual(classify(result), "config_fault")
        # in a 200 stream as well, both in the evidence and in what was parsed
        result, raw = self.call(client)
        self.assertNotIn(key.encode(), raw)
        self.assertEqual(result.merged["choices"][0]["message"]["content"], "echo [key] done")

    def test_missing_key_is_config_fault(self):
        forge = helpers.make_manifest()
        for env in ({}, {KEY_ENV: ""}):
            with self.subTest(env=env):
                with self.assertRaises(ProviderConfigFault):
                    ForgeProviderClient(forge, environ=env)
                self.assertNotIn(KEY_ENV, env)
        # stub: optional, "stub-key" is used and no digest is recorded
        client = self.client(key=None)
        self.assertIsNone(client.key_facts["sha256"])
        self.assertIsNone(client.key_facts["len"])
        self.call(client)
        self.assertEqual(self.server.requests[-1]["authorization_sha256"],
                         hashlib.sha256(b"Bearer stub-key").hexdigest())

    def test_base_url_must_match_kind(self):
        env = {KEY_ENV: "sk-url-1234567890"}
        ForgeProviderClient(helpers.make_manifest(), environ=dict(env))          # forge, exact URL
        bad = [
            ("forge", "http://127.0.0.1:8080/v1"), ("forge", "https://api.inference.wandb.ai/v1/"),
            ("forge", "https://evil.example.com/v1"), ("stub", "https://api.inference.wandb.ai/v1"),
            ("stub", "http://localhost:8080/v1"), ("stub", "http://10.0.0.1:8080/v1"),
            ("stub", "https://127.0.0.1:8080/v1"), ("stub", "http://user@127.0.0.1:8080/v1"),
            ("stub", "http://127.0.0.1/v1"), ("other", "http://127.0.0.1:8080/v1"),
        ]
        for kind, url in bad:
            with self.subTest(kind=kind, url=url):
                manifest = helpers.make_manifest(**{"provider.kind": kind, "provider.base_url": url})
                e = dict(env)
                with self.assertRaises(ProviderConfigFault):
                    ForgeProviderClient(manifest, environ=e)
                self.assertNotIn(KEY_ENV, e)                       # removed even when refused
        # a library-default User-Agent is refused
        with self.assertRaises(ProviderConfigFault):
            self.client(**{"provider.user_agent": "Python-urllib/3.13"})
        # forge without entity and project has no OpenAI-Project to send
        with self.assertRaises(ProviderConfigFault):
            ForgeProviderClient(helpers.make_manifest(**{"decisions.entity": None}), environ=dict(env))

    @unittest.skipUnless(shutil.which("ps"), "ps is not available")
    def test_key_not_visible_in_process_listing(self):
        canary = "mhfcanary" + secrets.token_hex(12)
        tmp = Path(tempfile.mkdtemp(prefix="mhf-provider-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        manifest_file = tmp / "manifest.json"
        manifest_file.write_text(json.dumps(helpers.make_manifest(
            **{"provider.kind": "stub", "provider.base_url": "http://127.0.0.1:9/v1"})))
        child = (
            "import json, sys\n"
            "from os import environ as process_env\n"
            "from mhforge import provider, util\n"
            "manifest = json.loads(open(sys.argv[1]).read())\n"
            "scrub = util.scrub_process_env if sys.argv[2] == 'scrub' else None\n"
            "if scrub is None:\n"
            "    process_env.pop('" + KEY_ENV + "', None)\n"
            "client = provider.ForgeProviderClient(manifest, environ=process_env, scrub=scrub)\n"
            "print(json.dumps(client.key_facts['scrubbed']), flush=True)\n"
            "sys.stdin.read()\n")

        def listing(mode):
            proc = subprocess.Popen([sys.executable, "-c", child, str(manifest_file), mode],
                                    env=helpers.child_env(**{KEY_ENV: canary}), stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                scrubbed = json.loads(proc.stdout.readline())
                ps = subprocess.run(["ps", "eww", "-p", str(proc.pid)], capture_output=True, text=True,
                                    env=helpers.child_env(), timeout=30)
                return scrubbed, ps.stdout
            finally:
                proc.stdin.close()
                proc.wait(timeout=30)
                proc.stdout.close()
                proc.stderr.close()

        control_scrubbed, control = listing("delete-only")
        self.assertIn(str(manifest_file), control)               # ps saw the child
        if canary not in control:
            self.skipTest("ps on this platform does not show a process's start-up environment")
        scrubbed, out = listing("scrub")
        self.assertIn(str(manifest_file), out)
        self.assertIs(scrubbed, True)
        self.assertNotIn(canary, out)


class Dispositions(_Server):
    def test_every_401_402_403_is_config_fault(self):
        messages = [("invalid_api_key", "Incorrect API key provided"), ("billing_hard_limit", "spend limit reached"),
                    ("org_disabled", "organization disabled"), (None, "blocked client: quota"),
                    ("rate_limit_exceeded", "rate limit exceeded")]
        for status in (401, 402, 403):
            for code, message in messages:
                with self.subTest(status=status, code=code):
                    server, url = self.start(faults=[Fault("http", (status, {"message": message, "code": code}))])
                    result, _ = self.call(self.client(url))
                    self.assertEqual(result.http_status, status)
                    self.assertEqual(classify(result, terminal_429_patterns=["quota"]), "config_fault")
                    self.assertEqual(circuit_outcome("config_fault"), "neutral")

    def test_quota_429_is_terminal_other_429_retries(self):
        cases = [
            ({"message": "You exceeded your current quota", "code": "insufficient_quota"}, (), "quota_exhausted"),
            ({"message": "limit", "code": "quota_exceeded"}, (), "quota_exhausted"),
            ({"message": "Monthly QUOTA reached for this org", "code": "too_many"}, ["quota"], "quota_exhausted"),
            ({"message": "Monthly QUOTA reached for this org", "code": "too_many"}, (), "retry_rate_limited"),
            ({"message": "slow down", "code": "org_quota_hit"}, ["quota"], "quota_exhausted"),
            ({"message": "Too many requests", "code": "rate_limit_exceeded"}, ["quota"], "retry_rate_limited"),
        ]
        for error, patterns, expected in cases:
            with self.subTest(error=error, patterns=patterns):
                server, url = self.start(faults=[Fault("http", (429, error))])
                result, _ = self.call(self.client(url))
                self.assertEqual(result.http_status, 429)
                self.assertEqual(classify(result, terminal_429_patterns=patterns), expected)
        server, url = self.start(faults=[Fault("rate_limit")])
        result, _ = self.call(self.client(url))
        self.assertEqual(classify(result, terminal_429_patterns=["quota"]), "retry_rate_limited")
        self.assertEqual(circuit_outcome("retry_rate_limited"), "failure")

    def test_every_stub_fault_classified_over_http(self):
        expected = {
            "http": ("retry_transport", "rejected"), "error_stream": ("retry_transport", "bounded_unknown"),
            "truncate": ("retry_transport", "bounded_unknown"), "no_usage": ("ok", "bounded_unknown"),
            "length_empty": ("ok", "usage"), "wrong_model": ("ok", "usage"), "no_model": ("ok", "usage"),
            "rate_limit": ("retry_rate_limited", "rejected"), "contradictory_usage": ("ok", "usage"),
            "reasoning_content": ("ok", "usage"), "hang": ("retry_transport", "bounded_unknown"),
        }
        self.assertEqual(set(expected), set(sp.FAULT_KINDS))
        for kind, (disposition, settle) in expected.items():
            with self.subTest(kind=kind):
                arg = 5.0 if kind == "hang" else None
                server, url = self.start(faults=[Fault(kind, arg)])
                result, _ = self.call(self.client(url, **{"provider.first_byte_timeout_s": 0.3}))
                got = classify(result, terminal_429_patterns=["quota"])
                self.assertEqual(got, disposition)
                self.assertEqual(settle_basis(result, got), settle)


if __name__ == "__main__":
    unittest.main()
