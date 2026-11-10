# SPDX-License-Identifier: Apache-2.0
# Adapted from ash0ts/harness2-wandb@31b0f82 harness2/showcase/telemetry.py L82-L170, L302-L408 (spans_from_records,
# readback, export_session). Changes: spans are built from a pure span plan of spec 2.13 instead of call records; the
# key is passed explicitly to tracing.init and to the read-back (never taken from the environment), with an explicit
# base_url; one module lock around every export; the trace id is handed to an on_trace callback before any span ends
# instead of writing the receipt here; the force_flush result is ignored; the read-back posts with urllib (no proxies,
# explicit User-Agent) and returns rows instead of verifying them; the SDK is imported only inside functions, and each
# SDK operation sits in one small function so it can be checked against the installed SDK before the first live call.
"""Agent Lens sink (UNVERIFIED until the live preflight): coreweave SDK emit and HTTP read-back (spec 3.25)."""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import os
import re
import threading
import urllib.parse
import urllib.request
from typing import Callable, Mapping, MutableMapping, Sequence

from decimal import Decimal

from .. import util
from ..errors import AdapterUnavailable, ExportError
from . import policy, readback

__all__ = ["CoreweaveSink", "TRACE_BASE_URL", "ADAPTER", "USER_AGENT", "USAGE_FIELDS", "key_facts"]

TRACE_BASE_URL = "https://trace.wandb.ai"                 # the host of readback.READBACK_URL; explicit, never from env
ADAPTER = {"name": "coreweave", "version": "0.1.0b0", "unverified": True}
USER_AGENT = "mhforge-exporter/0.1 (meta-harness forge showcase)"
PAGE_LIMIT = 200
USAGE_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "cache_read_input_tokens", "reasoning_tokens",
                "cache_creation_input_tokens")                     # every usage field of a 2.13 span plan
_EXPORT_LOCK = threading.Lock()                           # the SDK owns a process-global exporter (H2)
_ERROR_NAME_RE = re.compile(r"[^A-Za-z0-9_]")
# OpenTelemetry 1.45.2 SpanLimits: 128 attributes per span, and a full span evicts its OLDEST attribute (the conversation
# and plan attributes, set at start) for every SDK attribute set at end (opentelemetry/attributes/__init__.py:202-212).
OTEL_MAX_ATTRIBUTES = 128
SDK_ATTRIBUTES = 31                                       # at most 24 gen_ai.* (chat span) + 6 weave.* + error.type
OTLP_MAX_REQUEST_BYTES = 64 * 1024 * 1024                 # a larger batch is dropped whole (trace_exporter :186-199)
_TRACE_ZERO = "0" * 32                                    # the invalid trace id of a no-op tracer (OTEL_SDK_DISABLED)


def key_facts(name: str, value: str) -> dict:
    """What may be recorded about a key: its variable name, its length and the SHA-256 of its value."""
    return {"name": name, "len": len(value), "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest()}


def _read_key(manifest: Mapping, environ: Mapping[str, str]) -> tuple[str, str]:
    """environ[telemetry.api_key_env], read once; ExportError when missing or empty (no SDK credential chain)."""
    name = util.lookup(manifest, "telemetry.api_key_env")
    value = environ.get(name) if hasattr(environ, "get") else None
    if not isinstance(value, str) or not value:
        raise ExportError(f"the export key ({name}) is missing or empty; no SDK credential chain is consulted")
    return name, value


def _dt_utc(text: str) -> _dt.datetime:
    return _dt.datetime.fromtimestamp(util.parse_utc(text), _dt.timezone.utc)


class _SpanError(Exception):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):   # with no proxy: the key goes to the trace host only; urllib
    def redirect_request(self, req, fp, code, msg, headers, newurl):     # would copy it onto any redirect: a 3xx fails
        return None


def _error_for(error: Mapping) -> Exception:
    """An exception whose class name is the plan's error type. record_error sets error.type to the class __qualname__,
    prefixed by its __module__ unless that is "builtins" (conversation.py:270-281), so the class claims "builtins"."""
    name = _ERROR_NAME_RE.sub("_", str(error.get("type") or "Error"))[:64] or "Error"
    if not name[0].isalpha():
        name = "E" + name
    cls = type(name, (_SpanError,), {"__module__": "builtins", "__qualname__": name})
    return cls(str(error.get("message") or ""))


def _walk(spans):
    for s in spans or ():
        yield s
        yield from _walk(s.get("spans"))


def _limit_problems(plan: Mapping, batch_size: int, factor: Decimal) -> list[str]:
    """What the OpenTelemetry SDK would drop silently: attributes past 128 on a span, and a batch over 64 MiB."""
    out, sizes = [], []
    for conv in plan.get("conversations") or ():
        conv_keys = set(conv.get("attributes") or {})
        for turn in conv.get("turns") or ():
            for item in [turn, *_walk(turn.get("spans"))]:
                sizes.append(policy.span_bytes(item))
                n = len(conv_keys | set(item.get("attributes") or {})) + SDK_ATTRIBUTES
                if n > OTEL_MAX_ATTRIBUTES:
                    out.append(f"{str(item.get('key') or item.get('turn_key'))[:80]}: {n} attributes exceed "
                               f"the {OTEL_MAX_ATTRIBUTES} a span keeps")
    worst = factor * sum(sorted(sizes, reverse=True)[:max(1, batch_size)])
    if worst > OTLP_MAX_REQUEST_BYTES:
        out.append(f"the largest batch of {batch_size} spans is about {int(worst)} bytes, over the 64 MiB the OTLP "
                   "exporter sends")
    return out


# ---------------------------------------------------------------- the SDK operations, one function each (UNVERIFIED)

def _import_sdk():
    try:
        from coreweave.forge.agentlens import tracing
        from opentelemetry import trace as otel_trace
    except ImportError as exc:
        raise AdapterUnavailable(f"the Agent Lens SDK is not importable ({type(exc).__name__})") from None
    return tracing, otel_trace


def _sdk_init(tracing, project: str, key: str, service_name: str):
    return tracing.init(project, api_key=key, base_url=TRACE_BASE_URL, service_name=service_name,
                        autopatch_integrations=False)


def _sdk_conversation(tracing, conv: Mapping):
    return tracing.Conversation(conversation_id=conv["conversation_id"], conversation_name=conv["conversation_name"],
                                attributes=dict(conv.get("attributes") or {}))


def _sdk_turn(tracing, turn: Mapping):
    return tracing.Turn(agent_name=turn["agent_name"], agent_version=turn["agent_version"], messages=[],
                        started_at=_dt_utc(turn["started_utc"]), ended_at=_dt_utc(turn["ended_utc"]))


def _sdk_trace_id(otel_trace) -> str:
    return format(otel_trace.get_current_span().get_span_context().trace_id, "032x")


def _sdk_part(tracing, part: Mapping):
    if part.get("type") == "reasoning":
        return tracing.ReasoningPart(content=part["content"])
    return tracing.TextPart(content=part["content"])


def _sdk_message(tracing, message: Mapping):
    return tracing.Message(role=message["role"], parts=[_sdk_part(tracing, p) for p in message.get("parts") or ()])


def _sdk_llm(tracing, s: Mapping):
    return tracing.LLM(model=s.get("model") or "", provider_name=s.get("provider_name") or "",
                       response_model=s.get("response_model") or "", response_id=s.get("response_id") or "",
                       input_messages=[_sdk_message(tracing, m) for m in s.get("input_messages") or ()],
                       system_instructions=list(s.get("system_instructions") or []),
                       output_messages=[_sdk_message(tracing, m) for m in s.get("output_messages") or ()],
                       request_max_tokens=s.get("request_max_tokens"), request_temperature=s.get("request_temperature"),
                       finish_reasons=list(s.get("finish_reasons") or []),
                       started_at=_dt_utc(s["started_utc"]), ended_at=_dt_utc(s["ended_utc"]))


def _sdk_usage(tracing, usage: Mapping):
    return tracing.Usage(**{k: usage[k] for k in USAGE_FIELDS if usage.get(k) is not None})


def _sdk_tool(tracing, s: Mapping):
    return tracing.Tool(name=s["name"], tool_call_id=s["tool_call_id"], arguments=s.get("arguments") or "",
                        result=s.get("result") or "", started_at=_dt_utc(s["started_utc"]),
                        ended_at=_dt_utc(s["ended_utc"]))


def _sdk_subagent(tracing, s: Mapping):
    return tracing.SubAgent(name=s.get("name") or "subagent", model=s.get("model") or "",
                            started_at=_dt_utc(s["started_utc"]), ended_at=_dt_utc(s["ended_utc"]))


def _sdk_set_attributes(span, attributes: Mapping) -> None:
    span.set_attributes(dict(attributes or {}))


def _sdk_record_error(span, error: Mapping) -> None:
    span.record_error(_error_for(error))


def _sdk_flush(client) -> None:
    client.force_flush()                                  # its return value proves nothing; the read-back decides


def _sdk_shutdown(client) -> None:
    client.shutdown()


# ---------------------------------------------------------------- the sink

class CoreweaveSink:
    adapter = ADAPTER

    def __init__(self, manifest: dict, *, environ: MutableMapping[str, str],
                 opener: Callable | None = None) -> None:
        name, self._key = _read_key(manifest, environ)
        self.key_facts = key_facts(name, self._key)
        self.service_name = str(util.lookup(manifest, "telemetry.agent_lens.service_name"))
        self.batch_size = int(util.lookup(manifest, "telemetry.agent_lens.export_batch_size", 32))
        self.byte_factor = Decimal(str(util.lookup(manifest, "telemetry.agent_lens.byte_estimate_factor", "1")))
        if urllib.parse.urlsplit(readback.READBACK_URL).netloc != urllib.parse.urlsplit(TRACE_BASE_URL).netloc:
            raise ExportError("the read-back host and the trace base URL differ")
        self._open = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open

    def __repr__(self) -> str:                            # never the key
        return f"CoreweaveSink(service_name={self.service_name!r})"

    # ------------------------------------------------------------ emit

    def _prepare_process(self) -> None:
        # Every OTEL_* is read when tracing.init builds the provider: _HEADERS add request headers, _SDK_DISABLED and
        # _TRACES_SAMPLER turn tracing off silently, _CREDENTIAL_PROVIDER loads a session, _RESOURCE_* add attributes.
        for name in [n for n in os.environ if n.startswith("OTEL_")]:
            os.environ.pop(name, None)
        os.environ["OTEL_BSP_MAX_EXPORT_BATCH_SIZE"] = str(self.batch_size)
        os.environ["OTEL_EXPORTER_OTLP_TRACES_COMPRESSION"] = "gzip"
        if not os.environ.get("NETRC"):            # requests (the OTLP transport) adds Basic auth from a netrc entry
            os.environ["NETRC"] = os.devnull       # for the trace host; the export child already names an empty file

    def _check_plan(self, tracing, plan: Mapping) -> None:
        """Every SDK object of the plan built once (pydantic validates role, types) and the OTel limits checked, before
        tracing.init: a plan the SDK would reject or truncate halfway fails before any span starts."""
        problems = _limit_problems(plan, self.batch_size, self.byte_factor)
        if problems:
            raise ExportError(f"the span plan exceeds what the SDK sends intact: {problems[0]}")
        try:
            for conv in plan["conversations"]:
                _sdk_conversation(tracing, conv)
                for turn in conv["turns"]:
                    _sdk_turn(tracing, turn)
                    for s in _walk(turn.get("spans")):
                        self._build(tracing, s)
        except Exception as exc:                  # noqa: BLE001 - pydantic ValidationError, KeyError, ...
            raise ExportError(f"the Agent Lens SDK rejects the span plan ({type(exc).__name__})") from None

    def emit(self, plan: Mapping, on_trace: Callable[[str, str], None]) -> None:
        with _EXPORT_LOCK:
            self._prepare_process()
            tracing, otel_trace = _import_sdk()
            self._check_plan(tracing, plan)
            client = _sdk_init(tracing, plan["project"], self._key, self.service_name)
            try:
                for conv in plan["conversations"]:
                    with _sdk_conversation(tracing, conv):
                        for turn in conv["turns"]:
                            self._turn(tracing, otel_trace, conv["conversation_id"], turn, on_trace)
                    _sdk_flush(client)
            finally:
                _sdk_shutdown(client)

    def _turn(self, tracing, otel_trace, conversation_id: str, turn: Mapping, on_trace) -> None:
        t = _sdk_turn(tracing, turn)
        with t:
            trace_id = _sdk_trace_id(otel_trace)
            if trace_id == _TRACE_ZERO:            # a no-op tracer: nothing is recorded or sent
                raise ExportError("the OpenTelemetry tracer is disabled (invalid trace id); nothing was sent")
            on_trace(conversation_id, trace_id)                        # before any span of this trace ends
            _sdk_set_attributes(t, turn.get("attributes") or {})
            if turn.get("error"):
                _sdk_record_error(t, turn["error"])
            for span in turn.get("spans") or ():
                self._span(tracing, span)

    @staticmethod
    def _build(tracing, s: Mapping):
        if s["type"] == "llm":
            span = _sdk_llm(tracing, s)
            if isinstance(s.get("usage"), Mapping):
                span.usage = _sdk_usage(tracing, s["usage"])
            return span
        if s["type"] == "tool":
            return _sdk_tool(tracing, s)
        return _sdk_subagent(tracing, s)

    def _span(self, tracing, s: Mapping) -> None:
        span = self._build(tracing, s)
        with span:
            _sdk_set_attributes(span, s.get("attributes") or {})
            if s.get("error"):
                _sdk_record_error(span, s["error"])
            for child in s.get("spans") or ():
                self._span(tracing, child)

    # ------------------------------------------------------------ read back

    def _post(self, body: Mapping) -> dict:
        auth = base64.b64encode(f"api:{self._key}".encode("utf-8")).decode("ascii")
        req = urllib.request.Request(readback.READBACK_URL, data=json.dumps(body).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json",
                                              "User-Agent": USER_AGENT, "Authorization": f"Basic {auth}"})
        with self._open(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, dict):
            raise ExportError("read-back answer is not a JSON object")
        return data

    def read_back(self, project: str, trace_ids: Sequence[str]) -> list[dict]:
        """Every stored span of the given traces, paging by offset until total_count."""
        rows: list[dict] = []
        for trace_id in trace_ids:
            got, offset = 0, 0
            while True:
                data = self._post(readback.spans_query_body(project, trace_id, limit=PAGE_LIMIT, offset=offset))
                page = [r for r in data.get("spans") or () if isinstance(r, dict)]
                rows.extend(page)
                got += len(page)
                total = data.get("total_count")
                if not page or not isinstance(total, int) or got >= total:
                    break
                offset += len(page)
        return rows
