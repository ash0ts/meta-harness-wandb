# SPDX-License-Identifier: MIT
"""Claude Code on-disk transcript, or our stamped stream, -> NormalizedSession (spec 3.21).

The transcript format is internal to the CLI and free to change in any release, so everything that knows its key names
lives in this one module and parsing is gated on the CLI version. Pure except for `read_session_dir`, the one function
that reads files. `attachment` records are never read beyond their `type` and `version` (they hold account ids).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .. import util
from ..errors import TranscriptError

__all__ = ["ToolUse", "ApiMessage", "NormalizedSession", "parse_transcript", "normalize_transcript", "normalize_stream",
           "read_session_dir", "usage_by_model", "price_usage", "bash_results", "request_efforts",
           "WARNING_RECORD", "STREAM_PLACEHOLDER_WARNING", "USED_TYPES"]

USED_TYPES = ("assistant", "user", "api-request", "cost-state")
WARNING_RECORD = "mhforge-warning"                 # what parse_transcript makes of a line it cannot parse
STREAM_PLACEHOLDER_WARNING = "stream_output_tokens_are_placeholders"
PRICE_FIELDS = {"input": "input_per_million", "output": "output_per_million", "cache_read": "cache_read_per_million",
                "cache_creation_5m": "cache_write_5m_per_million", "cache_creation_1h": "cache_write_1h_per_million"}
_AGENT_FILE_RE = re.compile(r"agent-([A-Za-z0-9_-]{1,128})\.jsonl")


@dataclass(frozen=True)
class ToolUse:
    id: str
    name: str
    input: dict
    result_text: str | None                        # None only when no result was found; a found result is a str
    is_error: bool
    started_utc: str | None
    ended_utc: str | None
    sandboxed: bool | None                         # the tool-result record's `sandboxed` flag; None when the key is absent


@dataclass(frozen=True)
class ApiMessage:
    message_id: str
    request_id: str | None
    request_ref: str | None
    model: str
    effort: str | None                             # params.output_config.effort of the joined api-request record
    started_utc: str | None
    ended_utc: str | None
    input_tokens_total: int
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_creation: int
    cache_creation_5m: int
    cache_creation_1h: int
    text: str
    thinking_chars: int
    tool_uses: tuple[ToolUse, ...]
    stop_reason: str | None
    sidechain: bool
    agent_id: str | None


@dataclass(frozen=True)
class NormalizedSession:
    session_id: str | None
    source: str                                    # "transcript" | "stream"
    cli_version: str | None
    prompt_text: str | None
    messages: tuple[ApiMessage, ...]
    started_utc: str | None
    ended_utc: str | None
    cost_state: dict | None                        # {"total_cost_micro": int, "model_usage": {...}} from the LAST record
    skipped_types: dict[str, int]
    warnings: tuple[str, ...]
    agent_links: dict[str, str] = field(default_factory=dict)   # subagent id -> id of the Agent tool use that started it


# ---------------------------------------------------------------- small readers

def _int(v: object) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else 0


def _epoch(text: object) -> float | None:
    if not isinstance(text, str):
        return None
    try:
        return util.parse_utc(text)
    except ValueError:
        return None


def _iso(t: float | None) -> str | None:
    return None if t is None else util.utc_iso(t)


def _str(v: object) -> str | None:
    return v if isinstance(v, str) else None


def _result_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content
                         if isinstance(b, Mapping) and b.get("type") == "text" and isinstance(b.get("text"), str))
    return ""


def _content_blocks(message: Mapping) -> list:
    content = message.get("content")
    if isinstance(content, list):
        return [b for b in content if isinstance(b, Mapping)]
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return []


# ---------------------------------------------------------------- parsing

def parse_transcript(lines: Iterable[str]) -> list[dict]:
    """Tolerant JSONL: one dict per line; a line that is not a JSON object becomes a warning record
    {"type": "mhforge-warning", "warning": "bad_json_line", "line": n}. Blank lines are skipped."""
    out: list[dict] = []
    for n, raw in enumerate(lines, start=1):
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            value = None
        if isinstance(value, dict):
            out.append(value)
        else:
            out.append({"type": WARNING_RECORD, "warning": "bad_json_line", "line": n})
    return out


class _Builder:
    """Shared by both sources: groups assistant records by message.id, joins tool results and requests."""

    def __init__(self) -> None:
        self.groups: dict[str, list[tuple[Mapping, float | None, Mapping]]] = {}   # id -> [(message, t, meta)]
        self.results: dict[str, tuple[str, bool, float | None, bool | None]] = {}
        self.requests: dict[str, tuple[float | None, str | None, str | None]] = {}  # id -> (t, model, effort)
        self.agent_links: dict[str, str] = {}
        self.skipped: dict[str, int] = {}
        self.warnings: list[str] = []
        self.times: list[float] = []
        self.prompt_text: str | None = None
        self.session_id: str | None = None

    def skip(self, kind: object) -> None:
        key = kind if isinstance(kind, str) else "<none>"
        self.skipped[key] = self.skipped.get(key, 0) + 1

    def seen(self, t: float | None) -> None:
        if t is not None:
            self.times.append(t)

    def assistant(self, message: object, t: float | None, meta: Mapping) -> None:
        if not isinstance(message, Mapping) or not isinstance(message.get("id"), str):
            self.warnings.append("assistant_without_message_id")
            return
        self.groups.setdefault(message["id"], []).append((message, t, meta))
        self.seen(t)

    def user(self, message: object, t: float | None, *, sandboxed: bool | None, link: object, main: bool) -> None:
        self.seen(t)
        if not isinstance(message, Mapping):
            return
        content = message.get("content")
        if isinstance(content, str):
            if main and self.prompt_text is None:
                self.prompt_text = content
            return
        ids = []
        for block in _content_blocks(message):
            if block.get("type") == "tool_result" and isinstance(block.get("tool_use_id"), str):
                ids.append(block["tool_use_id"])
                self.results[block["tool_use_id"]] = (_result_text(block.get("content")),
                                                     block.get("is_error") is True, t, sandboxed)
        if isinstance(link, Mapping) and isinstance(link.get("agentId"), str) and len(ids) == 1:
            self.agent_links[link["agentId"]] = ids[0]

    def build(self, *, stream: bool) -> tuple[ApiMessage, ...]:
        out = []
        for mid, items in self.groups.items():
            msg = self._message(mid, items, stream=stream)
            if msg is not None:
                out.append(msg)
        if not out and self.groups:
            self.warnings.append("no_usable_timestamps")
        for aid in sorted({m.agent_id for m in out if m.sidechain and m.agent_id}):
            if aid not in self.agent_links:
                self.warnings.append(f"subagent_not_attached:{aid}")
        return tuple(out)

    def _message(self, mid: str, items, *, stream: bool) -> ApiMessage | None:
        times = [t for _, t, _ in items if t is not None]
        if not times:
            self.warnings.append(f"message_without_timestamps:{mid}")
            return None
        last = items[-1][0]
        ended = max(times)
        ref = next((m["request_ref"] for _, _, m in items if m.get("request_ref")), None)
        req = self.requests.get(ref) if ref else None
        effort = None if req is None else req[2]         # the request's effort even when its timestamp is unusable
        if req is not None and req[0] is not None:
            started = req[0]
        else:
            started = times[0]
            if not stream:
                self.warnings.append(f"no_request_record:{mid}")
        started = min(started, ended)
        usage = last.get("usage") if isinstance(last.get("usage"), Mapping) else {}
        inp, cr, cc = _int(usage.get("input_tokens")), _int(usage.get("cache_read_input_tokens")), \
            _int(usage.get("cache_creation_input_tokens"))
        detail = usage.get("cache_creation")
        if isinstance(detail, Mapping):
            c5, c1 = _int(detail.get("ephemeral_5m_input_tokens")), _int(detail.get("ephemeral_1h_input_tokens"))
        else:
            c5, c1 = 0, cc                           # no split given: the whole creation at the dearer 1-hour price
        text, thinking, tools = [], 0, []
        for message, t, _ in items:
            for block in _content_blocks(message):
                kind = block.get("type")
                if kind == "text" and isinstance(block.get("text"), str):
                    text.append(block["text"])
                elif kind == "thinking" and isinstance(block.get("thinking"), str):
                    thinking += len(block["thinking"])
                elif kind == "tool_use" and isinstance(block.get("id"), str):
                    res = self.results.get(block["id"])
                    raw_input = block.get("input")
                    tools.append(ToolUse(
                        id=block["id"], name=str(block.get("name") or ""),
                        input=dict(raw_input) if isinstance(raw_input, Mapping) else {},
                        result_text=None if res is None else res[0], is_error=bool(res and res[1]),
                        started_utc=_iso(t), ended_utc=None if res is None else _iso(res[2]),
                        sandboxed=None if res is None else res[3]))
        meta = items[-1][2]
        model = next((m.get("model") for m, _, _ in reversed(items) if isinstance(m.get("model"), str)), "")
        return ApiMessage(
            message_id=mid, request_id=next((m["request_id"] for _, _, m in items if m.get("request_id")), None),
            request_ref=ref, model=model, effort=effort, started_utc=_iso(started), ended_utc=_iso(ended),
            input_tokens_total=inp + cr + cc, input_tokens=inp, output_tokens=0 if stream else _int(usage.get("output_tokens")),
            cache_read=cr, cache_creation=cc, cache_creation_5m=c5, cache_creation_1h=c1,
            text="".join(text), thinking_chars=thinking, tool_uses=tuple(tools), stop_reason=_str(last.get("stop_reason")),
            sidechain=bool(meta.get("sidechain")), agent_id=meta.get("agent_id"))

    def session(self, *, source: str, cli_version: str | None, cost_state: dict | None, messages) -> NormalizedSession:
        return NormalizedSession(
            session_id=self.session_id, source=source, cli_version=cli_version, prompt_text=self.prompt_text,
            messages=messages, started_utc=_iso(min(self.times)) if self.times else None,
            ended_utc=_iso(max(self.times)) if self.times else None, cost_state=cost_state,
            skipped_types=dict(self.skipped), warnings=tuple(self.warnings), agent_links=dict(self.agent_links))


# ---------------------------------------------------------------- transcript

def _version_gate(records: Sequence[Mapping], expect_version: str | None, where: str) -> None:
    if expect_version is None:
        return
    for n, rec in enumerate(records):
        if isinstance(rec, Mapping) and "version" in rec and rec["version"] != expect_version:
            raise TranscriptError(f"transcript {where} record {n} was written by another CLI version", kind="version")


def _cost_state(rec: Mapping, warnings: list[str]) -> dict | None:
    value = rec.get("totalCostUSD")
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError
        micro = util.micros(str(value))
    except (ValueError, TypeError, InvalidOperation):
        warnings.append("cost_state_without_total")
        return None
    usage = rec.get("modelUsage")
    return {"total_cost_micro": micro, "model_usage": json.loads(json.dumps(usage)) if isinstance(usage, Mapping) else {}}


def normalize_transcript(records: Sequence[Mapping], *, expect_version: str | None,
                         subagents: Mapping[str, Sequence[Mapping]] | None = None) -> NormalizedSession:
    """TranscriptError(kind="version") when a record's `version` differs from expect_version (None switches the gate
    off; tests only). Records without a `version` key (queue-operation, cost-state) pass the gate."""
    files: list[tuple[str | None, Sequence[Mapping]]] = [(None, records)]
    for aid in sorted(subagents or {}):
        files.append((aid, subagents[aid]))
    for aid, recs in files:
        _version_gate(recs, expect_version, "main" if aid is None else f"subagent {aid}")
    b = _Builder()
    cli_version = None
    cost_state = None
    for aid, recs in files:                      # api-request records first: a ref may precede or follow its message
        for rec in recs:
            if isinstance(rec, Mapping) and rec.get("type") == "api-request" and isinstance(rec.get("id"), str):
                params = rec.get("params") if isinstance(rec.get("params"), Mapping) else {}
                oc = params.get("output_config") if isinstance(params.get("output_config"), Mapping) else {}
                b.requests[rec["id"]] = (_epoch(rec.get("timestamp")), _str(params.get("model")), _str(oc.get("effort")))
    for aid, recs in files:
        for rec in recs:
            if not isinstance(rec, Mapping):
                b.warnings.append("record_not_an_object")
                continue
            kind = rec.get("type")
            if kind == WARNING_RECORD:
                b.warnings.append(f"{rec.get('warning')}:{'main' if aid is None else aid}:{rec.get('line')}")
                continue
            if kind not in USED_TYPES:
                b.skip(kind)                     # attachment, queue-operation, system, unknown types: never read further
                continue
            if b.session_id is None and isinstance(rec.get("sessionId"), str):
                b.session_id = rec["sessionId"]
            if cli_version is None and isinstance(rec.get("version"), str):
                cli_version = rec["version"]
            t = _epoch(rec.get("timestamp"))
            if kind == "api-request":
                b.seen(t)
            elif kind == "cost-state":
                cost_state = _cost_state(rec, b.warnings) or cost_state
            else:
                side = aid is not None or rec.get("isSidechain") is True
                agent = _str(rec.get("agentId")) or aid if side else None
                if kind == "assistant":
                    b.assistant(rec.get("message"), t, {"request_ref": _str(rec.get("requestRef")),
                                                        "request_id": _str(rec.get("requestId")),
                                                        "sidechain": side, "agent_id": agent})
                else:
                    sandboxed = rec.get("sandboxed")
                    b.user(rec.get("message"), t, sandboxed=sandboxed if isinstance(sandboxed, bool) else None,
                           link=rec.get("toolUseResult"), main=not side)
    messages = b.build(stream=False)
    return b.session(source="transcript", cli_version=cli_version, cost_state=cost_state, messages=messages)


# ---------------------------------------------------------------- stream fallback

def normalize_stream(rows: Sequence[Mapping]) -> NormalizedSession:
    """The same structure from stream.jsonl rows {"t_utc", "stream", "line"}: times are arrival times, output_tokens
    stay 0 (the stream carries placeholders), no request join, cost_state None."""
    b = _Builder()
    cli_version = None
    for n, row in enumerate(rows):
        if not isinstance(row, Mapping) or row.get("stream") != "stdout" or not isinstance(row.get("line"), str):
            continue
        t = _epoch(row.get("t_utc"))
        try:
            ev = json.loads(row["line"])
        except ValueError:
            ev = None
        if not isinstance(ev, dict):
            b.warnings.append(f"bad_stream_line:{n}")
            continue
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            b.session_id = b.session_id or _str(ev.get("session_id"))
            cli_version = cli_version or _str(ev.get("claude_code_version"))
            b.seen(t)
        elif kind == "assistant":
            parent = _str(ev.get("parent_tool_use_id"))
            agent = _str(ev.get("agent_id")) or parent if parent else None
            if parent and agent:
                b.agent_links.setdefault(agent, parent)
            b.assistant(ev.get("message"), t, {"request_ref": None, "request_id": _str(ev.get("request_id")),
                                               "sidechain": parent is not None, "agent_id": agent})
        elif kind == "user":
            parent = _str(ev.get("parent_tool_use_id"))
            b.user(ev.get("message"), t, sandboxed=ev["sandboxed"] if isinstance(ev.get("sandboxed"), bool) else None,
                   link=ev.get("tool_use_result"), main=parent is None)
        elif kind == "result":
            b.session_id = b.session_id or _str(ev.get("session_id"))
            b.seen(t)
        else:
            b.skip(kind)
    messages = b.build(stream=True)
    if messages:
        b.warnings.append(STREAM_PLACEHOLDER_WARNING)
    return b.session(source="stream", cli_version=cli_version, cost_state=None, messages=messages)


# ---------------------------------------------------------------- files

def read_session_dir(transcript_dir: Path, session_id: str, *, max_bytes: int) -> tuple[list[dict], dict[str, list[dict]]]:
    """(<session_id>.jsonl parsed, {agent id: parsed <session_id>/subagents/agent-<id>.jsonl}). The .meta.json and
    .prefix.json files are not read. FileNotFoundError without the main file; TranscriptError(kind="shape") when the
    files together exceed max_bytes or one is a symlink."""
    sid = util.safe_component(session_id)
    root = Path(transcript_dir)
    main = root / f"{sid}.jsonl"
    budget = [max_bytes]

    def read(path: Path) -> list[dict]:
        if path.is_symlink():
            raise TranscriptError(f"transcript file {path.name} is a symlink", kind="shape")
        size = path.stat().st_size
        if size > budget[0]:
            raise TranscriptError(f"transcript files exceed {max_bytes} bytes", kind="shape")
        budget[0] -= size
        with open(path, "rb") as fh:
            data = fh.read(size + 1)
        if len(data) > size:
            raise TranscriptError(f"transcript file {path.name} grew while read", kind="shape")
        return parse_transcript(data.decode("utf-8", "replace").splitlines())

    if not main.is_file():
        raise FileNotFoundError(str(main))
    records = read(main)
    subagents: dict[str, list[dict]] = {}
    sub_dir = root / sid / "subagents"
    if sub_dir.is_dir() and not sub_dir.is_symlink():
        for path in sorted(sub_dir.iterdir()):
            m = _AGENT_FILE_RE.fullmatch(path.name)
            if m and path.is_file() and not path.is_symlink():
                subagents[m.group(1)] = read(path)
    return records, subagents


# ---------------------------------------------------------------- derived figures

def usage_by_model(session: NormalizedSession) -> dict[str, dict[str, int]]:
    """model -> {"input", "output", "cache_read", "cache_creation_5m", "cache_creation_1h"}; each message.id once, main
    thread and subagents together."""
    out: dict[str, dict[str, int]] = {}
    seen: set[str] = set()
    for m in session.messages:
        if m.message_id in seen:
            continue
        seen.add(m.message_id)
        u = out.setdefault(m.model, {k: 0 for k in PRICE_FIELDS})
        u["input"] += m.input_tokens
        u["output"] += m.output_tokens
        u["cache_read"] += m.cache_read
        u["cache_creation_5m"] += m.cache_creation_5m
        u["cache_creation_1h"] += m.cache_creation_1h
    return out


def price_usage(usage: Mapping[str, Mapping[str, int]], list_prices: Mapping) -> int | None:
    """Micro-dollars at proposer.list_prices (USD per million tokens), rounded up once over the total; None when a
    model in the usage has no price entry, or a counter that is not zero has no price."""
    total = Decimal(0)
    for model, counts in usage.items():
        prices = list_prices.get(model) if isinstance(list_prices, Mapping) else None
        if not isinstance(prices, Mapping):
            return None
        for key, price_key in PRICE_FIELDS.items():
            n = _int(counts.get(key))
            if not n:
                continue
            price = prices.get(price_key)
            if price is None or isinstance(price, bool):
                return None
            try:
                p = Decimal(str(price))
            except InvalidOperation:
                return None
            if not p.is_finite():
                return None
            total += p * n                           # USD per million tokens == micro-dollars per token
    return int(total.to_integral_value(rounding=ROUND_CEILING))


def bash_results(session: NormalizedSession) -> list[dict]:
    """[{"tool_use_id", "sandboxed": bool | None, "sidechain": bool}] for every Bash tool use that has a result."""
    return [{"tool_use_id": t.id, "sandboxed": t.sandboxed, "sidechain": m.sidechain}
            for m in session.messages for t in m.tool_uses if t.name == "Bash" and t.result_text is not None]


def request_efforts(session: NormalizedSession) -> list[str]:
    """The effort of each main-thread message's request, in order (messages whose request gave none are left out)."""
    return [m.effort for m in session.messages if not m.sidechain and m.effort is not None]
