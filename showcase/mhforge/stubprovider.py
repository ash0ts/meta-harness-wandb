# SPDX-License-Identifier: MIT
"""Offline stand-in for Serverless Inference (spec 3.9). Deterministic.

StubProvider is a ProviderClient that runs in process; serve_stub speaks the same bytes over loopback HTTP so that
ForgeProviderClient's real HTTP and streaming path runs with no network. Both render the provider's SSE wire format
with render_sse and parse it back with the production parser (sse.iter_sse through provider.StreamAccumulator).
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import socketserver
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import BinaryIO, Callable, Mapping, Sequence

from . import util
from .provider import ProviderError, ProviderResult, StreamAccumulator, error_from_body, filter_headers

__all__ = ["default_reply", "Fault", "FAULT_KINDS", "StubProvider", "render_sse", "serve_stub", "StubServer",
           "STUB_CREATED", "LAWBENCH_ANSWER", "USPTO_ANSWER", "SYMPTOM_ANSWER"]

FAULT_KINDS = ("http", "error_stream", "truncate", "no_usage", "length_empty", "wrong_model", "no_model", "hang",
               "rate_limit", "contradictory_usage", "reasoning_content")
STUB_CREATED = 1794270000
LAWBENCH_ANSWER = "[罪名]盗窃<eoa>"
USPTO_ANSWER = "CCO.CC(=O)O"
SYMPTOM_ANSWER = "[DIAGNOSIS]common cold[/DIAGNOSIS]"
STUB_REASONING = "Stub reasoning: the answer follows the fixed rule for this prompt."
DEFAULT_WRONG_MODEL = "openai/gpt-oss-20b"
DEFAULT_HANG_S = 3600.0
MAX_STUB_REQUEST_BYTES = 64 * 1024 * 1024


# ---------------------------------------------------------------- replies

def _text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, Mapping) and isinstance(p.get("text"), str))
    return ""


def default_reply(body: Mapping) -> dict:
    """A parseable answer per dataset, chosen by a fixed rule on the last user message; prompt_tokens is
    ceil(UTF-8 bytes of all message text / 4), completion_tokens 16."""
    messages = body.get("messages") if isinstance(body.get("messages"), list) else []
    last_user = ""
    for message in reversed(messages):
        if isinstance(message, Mapping) and message.get("role") == "user":
            last_user = _text(message.get("content"))
            break
    if "罪名" in last_user:
        content = LAWBENCH_ANSWER
    elif "reactants" in last_user or "SMILES" in last_user:
        content = USPTO_ANSWER
    else:
        content = SYMPTOM_ANSWER
    nbytes = sum(len(_text(m.get("content")).encode("utf-8")) for m in messages if isinstance(m, Mapping))
    prompt_tokens = (nbytes + 3) // 4
    return {"content": content, "reasoning": STUB_REASONING, "finish_reason": "stop",
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 16, "total_tokens": prompt_tokens + 16}}


def _event(obj: Mapping) -> bytes:
    return b"data: " + json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n\n"


def _pieces(text: str, size: int) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] if text else []


def render_sse(reply: Mapping, *, model: str | None, chunk_chars: int = 64) -> bytes:
    """The provider's wire format for a reply: a role chunk, reasoning deltas (under reply["reasoning_field"],
    default "reasoning"), content deltas (the last carries finish_reason), an "error" chunk when reply["error"] is set,
    usage in the last chunk (choices []), then [DONE] unless reply["done"] is False. model None omits the model key."""
    if chunk_chars < 1:
        raise ValueError("chunk_chars must be at least 1")
    seed = json.dumps(reply, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    head: dict = {"id": "chatcmpl-stub-" + hashlib.sha256(seed).hexdigest()[:12], "object": "chat.completion.chunk",
                  "created": STUB_CREATED}
    if model is not None:
        head["model"] = model

    def chunk(delta: Mapping, finish: object = None) -> bytes:
        return _event({**head, "choices": [{"index": 0, "delta": dict(delta), "finish_reason": finish}]})

    out: list[bytes] = []
    if "content" in reply or "reasoning" in reply:
        field_name = reply.get("reasoning_field") or "reasoning"
        finish = reply.get("finish_reason", "stop")
        out.append(chunk({"role": "assistant", "content": ""}))
        for piece in _pieces(str(reply.get("reasoning") or ""), chunk_chars):
            out.append(chunk({field_name: piece}))
        content = _pieces(str(reply.get("content") or ""), chunk_chars)
        for i, piece in enumerate(content):
            out.append(chunk({"content": piece}, finish if i == len(content) - 1 else None))
        if not content:
            out.append(chunk({}, finish))
    if reply.get("error") is not None:
        out.append(_event({"error": reply["error"]}))
    if reply.get("usage") is not None:
        out.append(_event({**head, "choices": [], "usage": reply["usage"]}))
    if reply.get("done", True) is not False:
        out.append(b"data: [DONE]\n\n")
    return b"".join(out)


# ---------------------------------------------------------------- faults

@dataclass(frozen=True)
class Fault:
    kind: str                                      # one of FAULT_KINDS
    arg: object = None                             # http: status, (status, error) or {"status", "error", "headers"};
    #                                                hang: seconds or (seconds, data lines sent first);
    #                                                wrong_model: model; error_stream: the error object
    when: int | str = "always"                     # 1-based request number, or "always"
    times: int = 1                                 # consecutive requests from `when` (ignored for "always")

    def __post_init__(self) -> None:
        if self.kind not in FAULT_KINDS:
            raise ValueError(f"unknown stub fault kind {self.kind!r}")
        if self.when != "always" and (isinstance(self.when, bool) or not isinstance(self.when, int) or self.when < 1):
            raise ValueError("Fault.when must be a 1-based request number or 'always'")
        if isinstance(self.times, bool) or not isinstance(self.times, int) or self.times < 1:
            raise ValueError("Fault.times must be a positive integer")

    def applies(self, n: int) -> bool:
        return self.when == "always" or self.when <= n < self.when + self.times   # type: ignore[operator]


@dataclass
class _Plan:
    status: int
    headers: list[tuple[str, str]]
    body: bytes
    hang_s: float = 0.0
    hang_after: int = 0                            # data lines sent before the hang
    cut: bool = False                              # truncate: the stream stops without [DONE]


def _http_parts(arg: object) -> tuple[int, dict, dict]:
    status, error, headers = 500, None, {}
    if isinstance(arg, Mapping):
        status = int(arg.get("status", 500))
        error, headers = arg.get("error"), dict(arg.get("headers") or {})
    elif isinstance(arg, tuple):
        status, error = int(arg[0]), (arg[1] if len(arg) > 1 else None)
    elif arg is not None:
        status = int(arg)
    if error is None:
        error = {"message": f"stub http fault {status}", "type": "stub_error", "code": str(status)}
    if 300 <= status <= 399:
        headers.setdefault("location", "http://127.0.0.1:9/elsewhere")
    return status, dict(error), headers


def _truncate(raw: bytes) -> bytes:
    events = [e for e in raw.split(b"\n\n") if e and e != b"data: [DONE]" and b'"usage"' not in e]
    if len(events) < 2:
        return events[0][: max(1, len(events[0]) // 2)] if events else b""
    keep = max(1, len(events) // 2)
    return b"\n\n".join(events[:keep]) + b"\n\n" + events[keep][: len(events[keep]) // 2]


class _Core:
    """Request counting, in-flight accounting and the fault plan, shared by StubProvider and the loopback server."""

    def __init__(self, rules: Callable[[Mapping], dict], faults: Sequence[Fault]) -> None:
        self.rules = rules
        self.faults = tuple(faults)
        self.calls: list[dict] = []
        self.peak_in_flight = 0
        self.in_flight = 0
        self._count = 0
        self._lock = threading.Lock()

    def begin(self, body: Mapping) -> int:
        with self._lock:
            self._count += 1
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
            self.calls.append(copy.deepcopy(dict(body)) if isinstance(body, Mapping) else {})
            return self._count

    def end(self) -> None:
        with self._lock:
            self.in_flight -= 1

    def plan(self, body: Mapping, n: int) -> _Plan:
        active = [f for f in self.faults if f.applies(n)]
        for f in active:
            if f.kind in ("http", "rate_limit"):
                if f.kind == "rate_limit":
                    status, error = 429, {"message": "Rate limit exceeded: too many requests, slow down",
                                          "type": "rate_limit_error", "code": "rate_limit_exceeded"}
                    headers = {"retry-after": "1"}
                else:
                    status, error, headers = _http_parts(f.arg)
                payload = json.dumps({"error": error}, ensure_ascii=False).encode("utf-8")
                return _Plan(status, [("content-type", "application/json"), *headers.items()], payload)
        reply = copy.deepcopy(dict(self.rules(body)))
        model: str | None = body.get("model") if isinstance(body.get("model"), str) else None
        plan = _Plan(200, [("content-type", "text/event-stream"), ("x-request-id", f"stub-{n}")], b"")
        for f in active:
            if f.kind == "error_stream":
                reply = {"error": dict(f.arg) if isinstance(f.arg, Mapping) else
                         {"message": "stub stream error", "type": "server_error", "code": 500}}
            elif f.kind == "truncate":
                plan.cut = True
            elif f.kind == "no_usage":
                reply.pop("usage", None)
            elif f.kind == "length_empty":
                limit = body.get("max_tokens") if isinstance(body.get("max_tokens"), int) else 16384
                prompt = (reply.get("usage") or {}).get("prompt_tokens", 0)
                reply.update(content="", finish_reason="length", reasoning=(reply.get("reasoning") or "x") * 8,
                             usage={"prompt_tokens": prompt, "completion_tokens": limit,
                                    "total_tokens": prompt + limit})
            elif f.kind == "wrong_model":
                model = str(f.arg) if f.arg else DEFAULT_WRONG_MODEL
            elif f.kind == "no_model":
                model = None
            elif f.kind == "hang":
                if isinstance(f.arg, tuple):
                    plan.hang_s, plan.hang_after = float(f.arg[0]), int(f.arg[1])
                else:
                    plan.hang_s = float(f.arg) if f.arg is not None else DEFAULT_HANG_S
            elif f.kind == "contradictory_usage":
                prompt = (reply.get("usage") or {}).get("prompt_tokens", 10)
                reply["usage"] = {"prompt_tokens": prompt, "completion_tokens": 16, "total_tokens": prompt + 16,
                                  "prompt_tokens_details": {"cached_tokens": prompt + 1000}}
            elif f.kind == "reasoning_content":
                reply["reasoning_field"] = "reasoning_content"
        raw = render_sse(reply, model=model)
        plan.body = _truncate(raw) if plan.cut else raw
        return plan


# ---------------------------------------------------------------- in process

class StubProvider:
    """A ProviderClient, in process. Renders real SSE bytes, writes them to sse_sink and parses them back."""

    def __init__(self, *, rules: Callable[[Mapping], dict] = default_reply, faults: Sequence[Fault] = (),
                 clock: util.Clock | None = None, latency_s: float = 0.0) -> None:
        self._core = _Core(rules, faults)
        self._clock = clock or util.SystemClock()
        self.latency_s = float(latency_s)

    @property
    def calls(self) -> list[dict]:
        return self._core.calls

    @property
    def peak_in_flight(self) -> int:
        return self._core.peak_in_flight

    @property
    def in_flight(self) -> int:
        return self._core.in_flight

    def complete(self, body: Mapping, *, sse_sink: BinaryIO | None, deadline_s: float) -> ProviderResult:
        clock = self._clock
        t0 = clock.monotonic()
        n = self._core.begin(body)
        try:
            if self.latency_s > 0:
                clock.sleep(self.latency_s)
            plan = self._core.plan(body, n)
            headers = filter_headers(plan.headers)

            def elapsed() -> float:
                return max(0.0, clock.monotonic() - t0)

            def sink(data: bytes) -> None:
                if sse_sink is not None:
                    sse_sink.write(data)
                    sse_sink.flush()

            if plan.status != 200:
                sink(plan.body)
                return ProviderResult(plan.status, headers, None, False, error_from_body(plan.status, plan.body),
                                      elapsed(), elapsed(), len(plan.body), None)
            acc = StreamAccumulator()
            received, first, cut = 0, None, None
            pending_hang = plan.hang_s > 0
            for line in io.BytesIO(plan.body).readlines():
                if pending_hang and line.startswith(b"data:") and acc.data_lines >= plan.hang_after:
                    pending_hang = False
                    remaining = float(deadline_s) - elapsed()
                    if plan.hang_s >= remaining:
                        clock.sleep(max(0.0, remaining))
                        cut = ProviderError("timeout", 200, None, "stream timed out")
                        break
                    clock.sleep(plan.hang_s)
                sink(line)
                received += len(line)
                if acc.feed(line) and first is None:
                    first = elapsed()
                if acc.done:
                    break
            if cut is None and pending_hang:        # hang after the last line: the stream stalls before closing
                remaining = float(deadline_s) - elapsed()
                if plan.hang_s >= remaining:
                    clock.sleep(max(0.0, remaining))
                    cut = ProviderError("timeout", 200, None, "stream timed out")
                else:
                    clock.sleep(plan.hang_s)
            merged, error, reasoning = acc.finish(200, cut)
            return ProviderResult(200, headers, merged, acc.done, error, first, elapsed(), received, reasoning)
        finally:
            self._core.end()


# ---------------------------------------------------------------- loopback HTTP

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "mhforge-stub/0.1"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:    # never logs anything (no Authorization value)
        return

    def _record(self, raw: bytes, body: object) -> None:
        names, headers, auth_sha = [], {}, None
        for name, value in self.headers.items():
            low = name.lower()
            names.append(low)
            if low == "authorization":
                auth_sha = hashlib.sha256(value.encode("latin-1", "replace")).hexdigest()
                headers[low] = "[redacted]"
            else:
                headers[low] = value
        self.server.record({"method": self.command, "path": self.path, "header_names": names, "headers": headers,
                            "authorization_sha256": auth_sha, "body": body, "body_bytes": len(raw)})

    def _send_json(self, status: int, obj: Mapping) -> None:
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._record(b"", None)
        self._send_json(404, {"error": {"message": "not found", "code": "not_found"}})

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_STUB_REQUEST_BYTES:
            self._record(b"", None)
            self._send_json(400, {"error": {"message": "bad content length", "code": "bad_request"}})
            return
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            body = None
        self._record(raw, body)
        if not self.path.split("?", 1)[0].endswith("/chat/completions"):
            self._send_json(404, {"error": {"message": "not found", "code": "not_found"}})
            return
        if not isinstance(body, dict):
            self._send_json(400, {"error": {"message": "body is not a JSON object", "code": "bad_request"}})
            return
        core: _Core = self.server.core
        n = core.begin(body)
        try:
            plan = core.plan(body, n)
            if plan.status != 200:
                self.send_response(plan.status)
                for name, value in plan.headers:
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(plan.body)))
                self.end_headers()
                self.wfile.write(plan.body)
                return
            self.send_response(200)
            for name, value in plan.headers:
                self.send_header(name, value)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.flush()
            sent_data, pending_hang = 0, plan.hang_s > 0
            for line in io.BytesIO(plan.body).readlines():
                if pending_hang and line.startswith(b"data:") and sent_data >= plan.hang_after:
                    pending_hang = False
                    if self.server.stop_event.wait(plan.hang_s):
                        return
                self.wfile.write(line)
                self.wfile.flush()
                if line.startswith(b"data:"):
                    sent_data += 1
            if pending_hang and self.server.stop_event.wait(plan.hang_s):
                return
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            pass
        finally:
            core.end()


class StubServer(ThreadingHTTPServer):
    """The loopback server of serve_stub. requests: every request's method, path, header names, headers (the
    Authorization value redacted; its SHA-256 kept), body and size."""

    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port: int, core: _Core) -> None:
        self.core = core
        self.requests: list[dict] = []
        self.stop_event = threading.Event()
        self._rec_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        super().__init__(("127.0.0.1", port), _Handler)

    def server_bind(self) -> None:                 # no reverse lookup of the address (HTTPServer calls getfqdn)
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]

    def record(self, entry: dict) -> None:
        with self._rec_lock:
            self.requests.append(entry)

    @property
    def calls(self) -> list[dict]:
        return self.core.calls

    @property
    def peak_in_flight(self) -> int:
        return self.core.peak_in_flight

    def close(self) -> None:
        """Release hanging handlers, stop serving and close the listener."""
        self.stop_event.set()
        if self._thread is not None:
            self.shutdown()
            self._thread.join(timeout=10)
            self._thread = None
        self.server_close()


def serve_stub(*, rules: Callable[[Mapping], dict] = default_reply, faults: Sequence[Fault] = (),
               port: int = 0) -> tuple[StubServer, str]:
    """Start the loopback SSE server on 127.0.0.1 in a daemon thread; returns (server, base_url ending /v1)."""
    server = StubServer(port, _Core(rules, faults))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, name="mhf-stub",
                              daemon=True)
    server._thread = thread
    thread.start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1"
