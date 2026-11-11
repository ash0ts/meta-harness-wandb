# SPDX-License-Identifier: Apache-2.0
# Adapted from ash0ts/harness2-wandb@31b0f82 harness2/showcase/telemetry.py (readback, verify_messages, verify_readback).
# Changes: pure functions only (no HTTP, no environment, no SDK); the query bodies are returned, not posted; rows are
# matched to a span plan of spec 2.13 on the custom attribute <prefix>.call_id, the tool call id or the turn's
# operation name; every difference is returned as a mismatch string instead of raising; the attribute prefix is ours.
"""Read-back query bodies and verification of stored spans against a span plan (spec 3.23). Pure.

The `/agents/*` endpoints are not yet an official public API, so the request shapes are pinned by tests. A stored row
is read in the shape the H2 read-back saw: `operation_name` (`chat`, `execute_tool`, `invoke_agent`), `attributes`
(flat dotted keys or nested) and/or `raw_span_dump` (JSON text whose `attributes` hold the same), `started_at`,
`ended_at`, `request_model`, `response_model`, `response_id`, `input_tokens`, `output_tokens`,
`cache_read_input_tokens`, `reasoning_tokens`, `request_max_tokens`, `finish_reasons`, `system_instructions`,
`input_messages` / `output_messages` (each `{"role", "content": <JSON text of the parts>}` or `{"role", "parts"}`),
`tool_call_id`, `tool_name`, `tool_call_arguments`, `tool_call_result`, `agent_name`, `status_code`.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import re
from typing import Mapping, Sequence

__all__ = ["READBACK_URL", "TIME_TOLERANCE_S", "spans_query_body", "conversation_ids_body", "verify_spans"]

READBACK_URL = "https://trace.wandb.ai/agents/spans/query"
TIME_TOLERANCE_S = 0.001
_FRACTION_RE = re.compile(r"(\.\d{6})\d+")
_KIND_BY_OPERATION = {"chat": "llm", "execute_tool": "tool", "invoke_agent": "agent"}


# ---------------------------------------------------------------- query bodies

def spans_query_body(project: str, trace_id: str, *, limit: int = 200, offset: int = 0) -> dict:
    """The body H2 posted to READBACK_URL for one trace."""
    return {"project_id": project,
            "query": {"$expr": {"$eq": [{"$getField": "trace_id"}, {"$literal": trace_id}]}},
            "include_details": True, "include_costs": True, "limit": limit, "offset": offset}


def conversation_ids_body(project: str, *, limit: int = 10000, offset: int = 0) -> dict:
    """Every span of a project without details: enough to list its conversation ids (the ownership check)."""
    return {"project_id": project, "include_details": False, "include_costs": False, "limit": limit, "offset": offset}


# ---------------------------------------------------------------- row readers

def _flatten(value: Mapping, head: str = "") -> dict:
    out: dict = {}
    for key, v in value.items():
        name = f"{head}.{key}" if head else str(key)
        if isinstance(v, Mapping):
            out.update(_flatten(v, name))
        else:
            out[name] = v
    return out


def _row_attrs(row: Mapping) -> dict:
    attrs: dict = {}
    raw = row.get("raw_span_dump")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = None
    if isinstance(raw, Mapping) and isinstance(raw.get("attributes"), Mapping):
        attrs.update(_flatten(raw["attributes"]))
    if isinstance(row.get("attributes"), Mapping):
        attrs.update(_flatten(row["attributes"]))
    return attrs


def _epoch(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        for scale in (1.0, 1e3, 1e6, 1e9):        # seconds, ms, us or ns since the epoch
            if v / scale < 1e11:
                return v / scale
        return None
    if not isinstance(value, str) or not value:
        return None
    text = _FRACTION_RE.sub(r"\1", value.strip().replace("Z", "+00:00").replace("z", "+00:00"))
    try:
        moment = _dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)   # the service stores naive UTC (H2 read-back)
    return moment.timestamp()


def _clean_parts(parts: object) -> list:
    if isinstance(parts, str):
        try:
            parts = json.loads(parts)
        except ValueError:
            return [{"type": "text", "content": parts}]
    if not isinstance(parts, list):
        return []
    return [{k: v for k, v in p.items() if v not in (None, "")} for p in parts if isinstance(p, Mapping)]


def _digest(messages: object) -> str:
    norm = []
    for m in messages if isinstance(messages, list) else ():
        if not isinstance(m, Mapping):
            continue
        parts = m.get("parts") if "parts" in m else m.get("content")
        norm.append({"role": m.get("role"), "parts": _clean_parts(parts)})
    return hashlib.sha256(json.dumps(norm, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _json_equal(a: object, b: object) -> bool:
    try:
        return json.loads(a) == json.loads(b)          # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------- plan side

def _plan_prefix(plan: Mapping) -> str:
    for conv in plan.get("conversations") or ():
        for key in (conv.get("attributes") or {}):
            if isinstance(key, str) and "." in key:
                return key.split(".", 1)[0]
    return "metaharness"


def _plan_items(plan: Mapping, prefix: str) -> list[tuple[str, str, Mapping]]:
    items: list[tuple[str, str, Mapping]] = []

    def walk(spans) -> None:
        for s in spans or ():
            kind = s.get("type")
            if kind == "llm":
                ident = (s.get("attributes") or {}).get(f"{prefix}.call_id") or s.get("key")
            elif kind == "tool":
                ident = s.get("tool_call_id") or s.get("key")
            else:
                ident = s.get("key")
            items.append((str(kind), str(ident), s))
            walk(s.get("spans"))

    for conv in plan.get("conversations") or ():
        for turn in conv.get("turns") or ():
            items.append(("turn", str(turn.get("turn_key") or turn.get("agent_name")), turn))
            walk(turn.get("spans"))
    return items


def _row_identity(row: Mapping, attrs: Mapping, prefix: str, response_ids: Mapping[str, str]) -> tuple[str, str]:
    op = row.get("operation_name")
    kind = _KIND_BY_OPERATION.get(op) if isinstance(op, str) else None
    if kind is None and row.get("tool_call_id"):
        kind = "tool"
    if kind == "llm":
        ident = attrs.get(f"{prefix}.call_id")
        if ident is None and row.get("response_id") in response_ids:
            ident = response_ids[row["response_id"]]          # H2 fallback: the provider's response id
        return "llm", str(ident)
    if kind == "tool":
        return "tool", str(row.get("tool_call_id"))
    if kind == "agent":
        if attrs.get(f"{prefix}.agent_id") is not None:
            return "subagent", str(attrs[f"{prefix}.agent_id"])
        return "turn", str(attrs.get(f"{prefix}.turn_key") or row.get("agent_name"))
    return "unknown", str(op)


# ---------------------------------------------------------------- comparisons

def _compare(kind: str, ident: str, planned: Mapping, row: Mapping) -> list[str]:
    out: list[str] = []
    tag = f"{kind}:{ident}"
    for field, source in (("started_at", "started_utc"), ("ended_at", "ended_utc")):
        stored, want = _epoch(row.get(field)), _epoch(planned.get(source))
        if stored is None or want is None or abs(stored - want) > TIME_TOLERANCE_S:
            out.append(f"{tag}:{field}")
    status = row.get("status_code")
    if planned.get("error") is not None and status != "ERROR":
        out.append(f"{tag}:error_lost")
    elif planned.get("error") is None and status == "ERROR":
        out.append(f"{tag}:unexpected_error")
    if kind == "llm":
        if (row.get("request_model") or "") != (planned.get("model") or ""):
            out.append(f"{tag}:request_model")
        if (row.get("response_model") or "") != (planned.get("response_model") or ""):   # missing becomes ""
            out.append(f"{tag}:response_model")
        usage = planned.get("usage")
        # Agent Lens projects absent token fields as zero. Live evidence covers
        # replay, rejected-provider and exhausted failed-call spans. Require matching outcome/attempt
        # attributes, explicit unavailable usage, and absent raw usage fields;
        # the ledger's missing-usage reservation is never changed here.
        pa, ra = planned.get("attributes") or {}, _row_attrs(row)
        absent_zero = usage is None and not any(k.startswith('gen_ai.usage.') for k in ra) and any(
            k.endswith(".outcome") and v in ("replayed", "provider_rejected", "failed")
            and type(pa.get(prefix + '.provider_attempts')) is int
            and pa[prefix + '.provider_attempts'] >= 0
            and (v != 'failed' or (pa[prefix + '.provider_attempts'] > 0
                                  and planned.get('error') is not None and status == 'ERROR'))
            and all(pa.get(prefix + suffix) == value and ra.get(prefix + suffix) == value
                    for suffix, value in ((".outcome", v), (".provider_attempts", pa[prefix + '.provider_attempts']),
                                          (".usage_available", False)))
            for k, v in pa.items() for prefix in [k.rsplit(".", 1)[0]])
        for stored, key in (("input_tokens", "input_tokens"), ("output_tokens", "output_tokens")):
            want = usage.get(key) if isinstance(usage, Mapping) else None
            if row.get(stored) != want and not (absent_zero and type(row.get(stored)) is int and row[stored] == 0):
                out.append(f"{tag}:{stored}")
        if isinstance(usage, Mapping):
            for key in ("cache_read_input_tokens", "reasoning_tokens"):
                if isinstance(usage.get(key), int) and row.get(key) != usage[key]:
                    out.append(f"{tag}:{key}")
        if list(row.get("finish_reasons") or []) != list(planned.get("finish_reasons") or []):
            out.append(f"{tag}:finish_reasons")
        if planned.get("request_max_tokens") is not None and row.get("request_max_tokens") != planned["request_max_tokens"]:
            out.append(f"{tag}:request_max_tokens")
        if list(row.get("system_instructions") or []) != list(planned.get("system_instructions") or []):
            out.append(f"{tag}:system_instructions")
        for field in ("input_messages", "output_messages"):
            if _digest(row.get(field) or []) != _digest(planned.get(field) or []):
                out.append(f"{tag}:{field}")
    elif kind == "tool":
        if row.get("tool_name") != planned.get("name"):
            out.append(f"{tag}:tool_name")
        stored, want = row.get("tool_call_arguments"), planned.get("arguments")
        empty = stored == "" and want in ("", "{}")            # an empty argument object is stored as ""
        if not empty and stored != want and not _json_equal(stored, want):
            out.append(f"{tag}:arguments")
        stored, want = row.get("tool_call_result"), planned.get("result")
        if stored != want and not _json_equal(stored, want) and not (stored in (None, "") and want == ""):
            out.append(f"{tag}:result")
    elif kind == "turn" and row.get("agent_name") is not None and row.get("agent_name") != planned.get("agent_name"):
        out.append(f"{tag}:agent_name")
    if kind in ("turn", "subagent"):              # invoke_agent spans are planned content-free (2.13)
        for field in ("input_messages", "output_messages"):
            if _digest(row.get(field) or []) != _digest(planned.get(field) or []):
                out.append(f"{tag}:{field}")
    return out


def verify_spans(plan: Mapping, rows: Sequence[Mapping]) -> list[str]:
    """Mismatches between the stored rows and the plan; [] for an exact match. Missing planned spans are
    `missing:<kind>:<id>`, rows with no planned span (duplicates included) `unexpected:<kind>:<id>`."""
    prefix = _plan_prefix(plan)
    items = _plan_items(plan, prefix)
    response_ids = {str(s.get("response_id")): ident for kind, ident, s in items
                    if kind == "llm" and s.get("response_id")}
    planned: dict[tuple[str, str], list[Mapping]] = {}
    for kind, ident, span in items:
        planned.setdefault((kind, ident), []).append(span)
    stored: dict[tuple[str, str], list[Mapping]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            stored.setdefault(("unknown", "row"), []).append({})
            continue
        stored.setdefault(_row_identity(row, _row_attrs(row), prefix, response_ids), []).append(row)
    out: list[str] = []
    for key in sorted(set(planned) | set(stored)):
        want = sorted(planned.get(key, []), key=lambda s: _epoch(s.get("started_utc")) or 0.0)
        have = sorted(stored.get(key, []), key=lambda r: _epoch(r.get("started_at")) or 0.0)
        for p, r in zip(want, have):
            out += _compare(key[0], key[1], p, r)
        out += [f"missing:{key[0]}:{key[1]}"] * max(len(want) - len(have), 0)
        out += [f"unexpected:{key[0]}:{key[1]}"] * max(len(have) - len(want), 0)
    return out
