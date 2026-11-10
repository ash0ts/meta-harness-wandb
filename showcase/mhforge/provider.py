# SPDX-License-Identifier: MIT
"""The provider client (spec 3.9): ProviderClient protocol, ForgeProviderClient (the live path; UNVERIFIED against the
service, tested against stubprovider.serve_stub) and the pure classify() table.

The client never raises for a provider or network condition: it returns a ProviderResult with `error` set. The key is
read once from the mapping it is given, removed from that mapping, kept in a name-mangled attribute and replaced by
"[key]" in every byte it writes or reports.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
import ssl
import threading
from dataclasses import dataclass
from typing import BinaryIO, Callable, Iterable, Mapping, MutableMapping, Protocol, Sequence
from urllib.parse import urlsplit

from . import sse, util
from .errors import ProviderConfigFault

__all__ = ["ProviderError", "ProviderResult", "ProviderClient", "ForgeProviderClient", "classify", "is_context_limit",
           "settle_basis", "circuit_outcome", "usage_well_formed", "filter_headers", "error_from_body", "mask_key",
           "StreamAccumulator", "FORGE_BASE_URL", "DISPOSITIONS", "ERROR_KINDS"]

FORGE_BASE_URL = "https://api.inference.wandb.ai/v1"
ERROR_KINDS = ("http", "stream_error", "truncated", "connect", "timeout", "bad_json", "too_large")
DISPOSITIONS = ("ok", "retry_transport", "retry_rate_limited", "provider_rejected", "context_limit", "config_fault",
                "quota_exhausted")
HEADER_ALLOW = frozenset({"content-type", "date", "retry-after", "request-id", "x-request-id", "cf-ray"})
HEADER_ALLOW_PREFIXES = ("x-ratelimit-",)
EXCERPT_CHARS = 2000
CONTEXT_LIMIT_PHRASES = ("context length", "context_length", "maximum context", "input tokens")
QUOTA_CODES = frozenset({"insufficient_quota", "quota_exceeded"})
STUB_KEY = "stub-key"
KEY_MASK = b"[key]"
_FORBIDDEN_UA_PREFIXES = ("python-urllib", "python-requests")


# ---------------------------------------------------------------- records

@dataclass(frozen=True)
class ProviderError:
    kind: str                                      # one of ERROR_KINDS
    http_status: int | None
    code: str | None
    body_excerpt: str                              # at most 2,000 characters; never a header


@dataclass(frozen=True)
class ProviderResult:
    http_status: int | None                        # None when no status line was received
    headers: dict[str, str]                        # the allow-list of 2.5, lower-cased names
    merged: dict | None                            # sse.merge_stream output (None for a non-200)
    done: bool                                     # [DONE] seen
    error: ProviderError | None
    first_byte_s: float | None                     # 200: first data line; other status: the status line
    total_s: float
    received_bytes: int
    reasoning_field: str | None


class ProviderClient(Protocol):
    def complete(self, body: Mapping, *, sse_sink: BinaryIO | None, deadline_s: float) -> ProviderResult: ...


# ---------------------------------------------------------------- shared pure helpers

def _excerpt(text: str) -> str:
    return text[:EXCERPT_CHARS]


def mask_key(data: bytes, key: bytes | None) -> bytes:
    """Every occurrence of the key replaced by [key]."""
    if key:
        return data.replace(key, KEY_MASK)
    return data


def filter_headers(pairs: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Response headers on the allow-list of 2.5 (content-type, date, retry-after, request-id, x-request-id, cf-ray,
    x-ratelimit-*), names lower-cased."""
    out: dict[str, str] = {}
    for name, value in pairs:
        low = str(name).lower()
        if low in HEADER_ALLOW or low.startswith(HEADER_ALLOW_PREFIXES):
            out[low] = str(value)
    return out


def usage_well_formed(usage: object) -> bool:
    """A usage block with non-negative integer prompt_tokens and completion_tokens (what classify needs; the ledger
    judges the rest, such as cached > prompt)."""
    if not isinstance(usage, Mapping):
        return False
    for name in ("prompt_tokens", "completion_tokens"):
        value = usage.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return False
    return True


def _int_code(code: object) -> int | None:
    if isinstance(code, bool):
        return None
    if isinstance(code, int):
        return code
    if isinstance(code, str) and code.isascii() and code.isdigit() and len(code) <= 4:
        return int(code)
    return None


def _stream_error(err: object, status: int | None) -> ProviderError:
    if isinstance(err, Mapping):
        code = (None if err.get("code") is None else str(err["code"]))
        text = json.dumps(err, ensure_ascii=False, default=str)
    else:
        code, text = None, str(err)
    return ProviderError("stream_error", status, code, _excerpt(text))


def error_from_body(status: int, raw: bytes, *, kind: str = "http") -> ProviderError:
    """ProviderError for a non-200 response body (already masked): code from error.code of the JSON body."""
    text = raw.decode("utf-8", "replace")
    code = None
    try:
        obj = json.loads(text)
    except ValueError:
        obj = None
    if isinstance(obj, Mapping):
        err = obj.get("error")
        if isinstance(err, Mapping):
            code = (None if err.get("code") is None else str(err["code"]))
    return ProviderError(kind, status, code, _excerpt(text))


def _message_text(error: ProviderError) -> str:
    """error.message of the excerpt's JSON body when it parses, else the whole excerpt."""
    try:
        obj = json.loads(error.body_excerpt)
    except ValueError:
        return error.body_excerpt
    if isinstance(obj, Mapping):
        err = obj.get("error")
        if isinstance(err, Mapping) and isinstance(err.get("message"), str):
            return err["message"]
        if isinstance(obj.get("message"), str):
            return obj["message"]
    return error.body_excerpt


def _complete_without_done(merged: Mapping) -> bool:
    choices = merged.get("choices") or []
    return bool(choices) and choices[0].get("finish_reason") is not None and usage_well_formed(merged.get("usage"))


class StreamAccumulator:
    """Feeds raw SSE lines (one line each, terminator kept or not) through sse.iter_sse and builds the stream part of a
    ProviderResult. Shared by ForgeProviderClient and the stub so both run the production parser."""

    def __init__(self) -> None:
        self.chunks: list[dict] = []
        self.done = False
        self.data_lines = 0
        self.bad: bytes | None = None

    def feed(self, line: bytes) -> bool:
        """Returns True when the line was a data line."""
        was_data = line.lstrip().startswith(b"data:")
        if was_data:
            self.data_lines += 1
        for event, payload in sse.iter_sse([line]):
            if event == "chunk":
                self.chunks.append(payload)        # type: ignore[arg-type]
            elif event == "done":
                self.done = True
            elif self.bad is None:
                self.bad = payload                 # type: ignore[assignment]
        return was_data

    def finish(self, status: int, cut: ProviderError | None) -> tuple[dict, ProviderError | None, str | None]:
        """(merged, error, reasoning_field). Precedence: too_large, an error chunk, a bad line, then a cut stream
        (timeout or read failure) or an end without [DONE], unless the stream already carried a choice, a finish_reason
        and a well-formed usage block (complete without [DONE]: no error)."""
        merged = sse.merge_stream(self.chunks)
        error: ProviderError | None = None
        if cut is not None and cut.kind == "too_large":
            error = cut
        elif merged.get("error") is not None:
            error = _stream_error(merged["error"], status)
        elif self.bad is not None:
            error = ProviderError("bad_json", status, None, _excerpt(self.bad.decode("utf-8", "replace")))
        elif self.done or _complete_without_done(merged):
            error = None
        elif cut is not None:
            error = cut
        else:
            error = ProviderError("truncated", status, None, "stream ended without [DONE]")
        return merged, error, sse.reasoning_field(self.chunks)


# ---------------------------------------------------------------- classification (pure)

def is_context_limit(error: ProviderError) -> bool:
    """HTTP 400 (or a 400-coded stream error) whose text names the context length."""
    text = (error.body_excerpt or "").lower()
    if not any(phrase in text for phrase in CONTEXT_LIMIT_PHRASES):
        return False
    if error.kind == "http":
        return error.http_status == 400
    if error.kind == "stream_error":
        return _int_code(error.code) == 400
    return False


def classify(result: ProviderResult, *, terminal_429_patterns: Sequence[str] = ()) -> str:
    """The disposition of 3.9. Evidence order: HTTP status, then error.code, then a substring of the message."""
    status, error = result.http_status, result.error
    if status is None:
        return "retry_transport"                   # connect (nothing sent) or timeout before the status line
    if status == 200:
        if error is not None and error.kind == "too_large":
            return "provider_rejected"
        merged = result.merged or {}
        if merged.get("error") is not None:
            stream_err = error if error is not None and error.kind == "stream_error" \
                else _stream_error(merged["error"], status)
            raw = merged["error"].get("code") if isinstance(merged["error"], Mapping) else None
            code = raw if isinstance(raw, int) and not isinstance(raw, bool) else None   # an integer, as the table says
            if code is not None and 400 <= code <= 499 and code not in (408, 429):
                return "context_limit" if is_context_limit(stream_err) else "provider_rejected"
            return "retry_transport"
        if error is None and (merged.get("choices") or []) and (result.done or _complete_without_done(merged)):
            return "ok"
        return "retry_transport"
    if status == 400:
        if error is not None and is_context_limit(error):
            return "context_limit"
        return "provider_rejected"
    if status in (413, 422):
        return "provider_rejected"
    if status in (401, 402, 403):
        return "config_fault"
    if status == 429:
        if error is None:
            return "retry_rate_limited"
        code = (error.code or "").lower()
        if code in QUOTA_CODES:
            return "quota_exhausted"
        patterns = [str(p).lower() for p in terminal_429_patterns if str(p)]
        if any(p in code for p in patterns):
            return "quota_exhausted"
        message = _message_text(error).lower()
        if any(p in message for p in patterns):
            return "quota_exhausted"
        return "retry_rate_limited"
    return "retry_transport"                       # 404, 408, 409, 5xx, 3xx and any status the table does not name


def settle_basis(result: ProviderResult, disposition: str) -> str:
    """The 'Ledger settle' column: 'usage', 'rejected' or 'bounded_unknown'. A well-formed usage block always wins."""
    if result.http_status == 200 and usage_well_formed((result.merged or {}).get("usage")):
        return "usage"
    if result.http_status is None:
        return "rejected" if result.error is not None and result.error.kind == "connect" else "bounded_unknown"
    if result.http_status != 200:
        return "rejected"
    if disposition in ("context_limit", "provider_rejected") and not (
            result.error is not None and result.error.kind == "too_large"):
        return "rejected"
    return "bounded_unknown"


def circuit_outcome(disposition: str) -> str:
    """The 'circuit.finish' column: 'success', 'neutral' or 'failure'."""
    if disposition == "ok":
        return "success"
    if disposition in ("retry_transport", "retry_rate_limited"):
        return "failure"
    return "neutral"


# ---------------------------------------------------------------- the live client

def _printable_ascii(text: object) -> bool:
    return isinstance(text, str) and bool(text) and all(0x20 <= ord(c) <= 0x7E for c in text)


def _check_base_url(kind: object, base_url: object) -> None:
    if kind == "forge":
        if base_url != FORGE_BASE_URL:
            raise ProviderConfigFault(f"provider.base_url must be {FORGE_BASE_URL} for kind forge")
        return
    if kind == "stub":
        parts = urlsplit(base_url) if isinstance(base_url, str) else None
        try:
            port = parts.port if parts is not None else None
        except ValueError:
            port = None
        if (parts is None or parts.scheme != "http" or parts.hostname != "127.0.0.1" or port is None
                or parts.username is not None or parts.password is not None or parts.query or parts.fragment
                or parts.netloc != f"127.0.0.1:{port}"):
            raise ProviderConfigFault("provider.base_url must be http://127.0.0.1:<port>/... for kind stub")
        return
    raise ProviderConfigFault(f"provider.kind must be forge or stub, not {str(kind)[:20]!r}")


class ForgeProviderClient:
    """POST <base_url>/chat/completions with stream: true; returns a ProviderResult. UNVERIFIED against the service."""

    def __init__(self, manifest: dict, *, environ: MutableMapping[str, str],
                 scrub: Callable[[str], bool] | None = None, clock: util.Clock | None = None) -> None:
        def p(name: str) -> object:
            return util.lookup(manifest, "provider." + name)

        name = p("api_key_env")
        if not isinstance(name, str) or not name:
            raise ProviderConfigFault("provider.api_key_env is not a variable name")
        value = environ.get(name)
        scrubbed: bool | None = None
        try:
            if scrub is not None:
                scrubbed = bool(scrub(name))
        finally:
            environ.pop(name, None)                # in every case: no later child can inherit it
        self.kind = p("kind")
        self.base_url = p("base_url")
        self.key_env = name
        _check_base_url(self.kind, self.base_url)
        if not isinstance(value, str) or not value:
            if self.kind == "forge":
                raise ProviderConfigFault(f"{name} is missing or empty")
            value = None
        self.__key = value or STUB_KEY             # with kind stub and no key: "stub-key", and no digest
        self.key_facts = {"name": name, "len": len(value) if value else None, "scrubbed": scrubbed,
                          "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest() if value else None}
        value = None
        self._clock = clock or util.SystemClock()
        project = p("openai_project")
        if project is None:
            entity, proj = util.lookup(manifest, "decisions.entity", None), util.lookup(manifest, "decisions.project", None)
            if entity and proj:
                project = f"{entity}/{proj}"
            elif self.kind == "stub":
                project = "mhforge-offline/stub"
            else:
                raise ProviderConfigFault("OpenAI-Project unknown: decisions.entity and decisions.project are unset")
        user_agent = p("user_agent")
        if not _printable_ascii(project):
            raise ProviderConfigFault("provider.openai_project must be printable ASCII")
        if not _printable_ascii(user_agent) or str(user_agent).lower().startswith(_FORBIDDEN_UA_PREFIXES):
            raise ProviderConfigFault("provider.user_agent must be printable ASCII and not a library default")
        self._project, self._user_agent = str(project), str(user_agent)
        parts = urlsplit(str(self.base_url))
        self._https = parts.scheme == "https"
        self._host, self._port = parts.hostname or "", parts.port
        self._path = parts.path.rstrip("/") + "/chat/completions"
        self._connect_timeout_s, self._first_byte_timeout_s, self._idle_timeout_s, self._attempt_deadline_s = (
            float(p(n)) for n in ("connect_timeout_s", "first_byte_timeout_s", "idle_timeout_s", "attempt_deadline_s"))
        self._max_bytes = int(p("max_response_bytes"))

    def __repr__(self) -> str:
        return f"ForgeProviderClient(kind={self.kind!r}, base_url={self.base_url!r}, key_env={self.key_env!r})"

    def __reduce__(self):
        raise TypeError("ForgeProviderClient holds a key and cannot be pickled")

    def _mask(self, data: bytes) -> bytes:
        return mask_key(data, self.__key.encode("utf-8"))

    def _mask_text(self, text: str) -> str:
        return text.replace(self.__key, "[key]") if self.__key else text

    def _connection(self, timeout: float) -> http.client.HTTPConnection:
        if self._https:
            return http.client.HTTPSConnection(self._host, self._port, timeout=timeout,
                                               context=ssl.create_default_context())
        return http.client.HTTPConnection(self._host, self._port, timeout=timeout)

    def _headers(self, length: int) -> list[tuple[str, str]]:
        return [("Authorization", "Bearer " + self.__key), ("Content-Type", "application/json"),
                ("Accept", "text/event-stream"), ("OpenAI-Project", self._project),
                ("User-Agent", self._user_agent), ("Content-Length", str(length))]

    def _err(self, kind: str, status: int | None, text: str) -> ProviderError:
        return ProviderError(kind, status, None, _excerpt(self._mask_text(text)))

    def complete(self, body: Mapping, *, sse_sink: BinaryIO | None, deadline_s: float) -> ProviderResult:
        clock = self._clock
        t0 = clock.monotonic()
        end = t0 + max(0.0, min(float(deadline_s), self._attempt_deadline_s))
        payload = {k: v for k, v in body.items() if k not in ("usage", "response_format", "stream", "stream_options")}
        payload.update(stream=True, stream_options={"include_usage": True})
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")

        def left(limit: float) -> float:
            return max(0.001, min(limit, end - clock.monotonic()))

        def result(status, headers, merged, done, error, first, received, reasoning=None) -> ProviderResult:
            return ProviderResult(http_status=status, headers=headers, merged=merged, done=done, error=error,
                                  first_byte_s=first, total_s=max(0.0, clock.monotonic() - t0),
                                  received_bytes=received, reasoning_field=reasoning)

        def sink(line: bytes) -> None:
            if sse_sink is not None:
                sse_sink.write(line)
                sse_sink.flush()

        conn = self._connection(left(self._connect_timeout_s))
        resp = None
        watchdog: threading.Timer | None = None
        fired = threading.Event()
        try:
            try:
                conn.connect()
            except (OSError, http.client.HTTPException) as exc:
                return result(None, {}, None, False, self._err("connect", None, f"connect failed: {type(exc).__name__}"),
                              None, 0)
            sock = conn.sock

            def cut_at_deadline() -> None:     # a peer that trickles bytes defeats per-recv socket timeouts
                fired.set()
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            watchdog = threading.Timer(max(0.0, end - clock.monotonic()), cut_at_deadline)
            watchdog.daemon = True
            watchdog.start()
            try:
                sock.settimeout(left(self._first_byte_timeout_s))
                conn.putrequest("POST", self._path, skip_accept_encoding=True)
                for hname, hvalue in self._headers(len(data)):
                    conn.putheader(hname, hvalue)
                conn.endheaders(data)
                first_deadline = min(end, clock.monotonic() + self._first_byte_timeout_s)
                sock.settimeout(max(0.001, first_deadline - clock.monotonic()))
                resp = conn.getresponse()
            except TimeoutError:
                return result(None, {}, None, False, self._err("timeout", None, "no status line before the timeout"),
                              None, 0)
            except (OSError, http.client.HTTPException) as exc:
                # the request may have reached the provider: booked like a timeout with no status line
                return result(None, {}, None, False,
                              self._err("timeout", None, f"no status line: {type(exc).__name__}"), None, 0)
            status = resp.status
            headers = filter_headers(resp.getheaders())
            if status != 200:
                first = clock.monotonic() - t0
                raw, received = self._read_body(resp, sock, left)
                masked = self._mask(raw)
                sink(masked)
                return result(status, headers, None, False, error_from_body(status, masked), first, received)
            return self._read_stream(resp, sock, end, first_deadline, sink, result, t0, headers, fired)
        finally:
            if watchdog is not None:
                watchdog.cancel()
            try:
                if resp is not None:
                    resp.close()
            finally:
                conn.close()

    def _read_body(self, resp, sock, left) -> tuple[bytes, int]:
        parts: list[bytes] = []
        received = 0
        try:
            while received <= self._max_bytes:
                sock.settimeout(left(self._idle_timeout_s))
                block = resp.read1(min(65536, self._max_bytes + 1 - received))
                if not block:
                    break
                parts.append(block)
                received += len(block)
        except (OSError, http.client.HTTPException):
            pass
        return b"".join(parts)[:self._max_bytes], received

    def _read_stream(self, resp, sock, end, first_deadline, sink, result, t0, headers,
                     fired: threading.Event | None = None) -> ProviderResult:
        clock = self._clock
        acc = StreamAccumulator()
        received = 0
        first: float | None = None
        cut: ProviderError | None = None
        while not acc.done:
            now = clock.monotonic()
            limit = first_deadline if acc.data_lines == 0 else now + self._idle_timeout_s
            limit = min(limit, end)
            if limit - now <= 0:
                cut = self._err("timeout", 200, "stream timed out")
                break
            try:
                sock.settimeout(limit - now)
                line = resp.readline(self._max_bytes - received + 1)
            except TimeoutError:
                cut = self._err("timeout", 200, "stream timed out")
                break
            except (OSError, http.client.HTTPException) as exc:
                cut = self._err("truncated", 200, f"stream read failed: {type(exc).__name__}")
                break
            if not line:
                break
            received += len(line)
            if received > self._max_bytes:
                cut = self._err("too_large", 200, f"response exceeded {self._max_bytes} bytes")
                break
            masked = self._mask(line)
            sink(masked)                           # written and flushed before it is parsed
            if acc.feed(masked) and first is None:
                first = clock.monotonic() - t0
        if fired is not None and fired.is_set() and not acc.done and (cut is None or cut.kind == "truncated"):
            cut = self._err("timeout", 200, "attempt deadline reached")
        merged, error, reasoning = acc.finish(200, cut)
        return result(200, headers, merged, acc.done, error, first, received, reasoning)
