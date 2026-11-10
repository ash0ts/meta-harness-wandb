# SPDX-License-Identifier: MIT
"""Records -> span plans (spec 2.13, 3.23): a solver job attempt, a proposer session, one outer-loop iteration.

Pure: no SDK, no clock, no network, no environment, no file. The same inputs give the same bytes. Every text that can
hold dataset text passes through a content rule of telemetry/policy.py before it is placed in a plan; turns never carry
content; usage appears only on paid `llm` spans.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal
from pathlib import Path
from typing import Mapping, Sequence

from .. import scan, util, wire
from . import join, policy
from .transcript import ApiMessage, NormalizedSession, ToolUse

__all__ = ["SCHEMA", "PLACEHOLDER_PROJECT", "LENS_URL", "VERBATIM_INDEX_MISSING", "conversation_url", "plan_solver_job",
           "plan_proposer", "plan_outer_iteration", "validate_plan"]

SCHEMA = "mhforge.spanplan/1"
PLACEHOLDER_PROJECT = "unset-entity/unset-project"
LENS_URL = "https://forge.coreweave.com/agent-lens"
_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,128}")
_IDENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_HASH_ALLOW, _CUT_ALLOW = 100, 80                 # longest hash form; longest cut marker (policy.apply_text)
_GUARDED_KEYS = ("hypothesis", "axis", "components")
_OURS_KEYS = ("candidate", "base_system", "system", "status", "outcome", "failure_class", "rejected", "kind",
              "dataset", "session_key", "split")
HASHED_OUTCOMES = ("replayed", "coalesced", "refused", "interrupted")
ERROR_OUTCOMES = ("refused", "provider_rejected", "failed", "interrupted")
_EPOCH0 = "1970-01-01T00:00:00.000000Z"


# ---------------------------------------------------------------- small helpers

def conversation_url(project: str, conversation_id: str) -> str:
    """https://forge.coreweave.com/agent-lens/<entity>/<project>/conversations/<id>"""
    return f"{LENS_URL}/{project}/conversations/{conversation_id}"


def _project(manifest: Mapping) -> tuple[str, bool]:
    entity = util.lookup(manifest, "decisions.entity", None)
    project = util.lookup(manifest, "decisions.project", None)
    if not entity or not project:
        return PLACEHOLDER_PROJECT, True
    return f"{entity}/{project}", False


def _prefix(manifest: Mapping) -> str:
    return util.lookup(manifest, "telemetry.attribute_prefix", "metaharness") or "metaharness"


def _limit(manifest: Mapping) -> int:
    return int(util.lookup(manifest, "telemetry.agent_lens.max_spans_per_conversation", 180))


def _cid(base: str, *, part: int = 1, export_attempt: int = 1) -> str:
    text = base + (f".p{part}" if part > 1 else "") + (f".e{export_attempt}" if export_attempt > 1 else "")
    return wire.bounded_id(text, 128)


def _t(text: object) -> float | None:
    try:
        return util.parse_utc(text)               # type: ignore[arg-type]
    except ValueError:
        return None


def _earliest(*texts: object) -> str | None:
    good = [(t, s) for s in texts if (t := _t(s)) is not None]
    return min(good)[1] if good else None


def _latest(*texts: object) -> str | None:
    good = [(t, s) for s in texts if (t := _t(s)) is not None]
    return max(good)[1] if good else None


def _sha(obj: object) -> str:
    try:
        data = util.canonical_json(obj)
    except (TypeError, ValueError):
        data = json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _count(spans: Sequence[Mapping]) -> int:
    return sum(1 + _count(s.get("spans") or ()) for s in spans)


def _attrs(prefix: str, **values: object) -> dict:
    return {f"{prefix}.{k}": v for k, v in values.items() if v is not None and v != ""}


class _Text:
    """Applies content rules and counts the fields that lost text."""

    def __init__(self) -> None:
        self.truncated = 0

    def apply(self, text: str, rule: str) -> tuple[str, bool]:
        out, changed = policy.apply_text(text, rule)
        if changed:
            self.truncated += 1
        return out, changed


def _turn(key: str, agent: str, version: str, started: str, ended: str, attrs: dict, spans: list,
          error: dict | None = None) -> dict:
    return {"turn_key": key, "agent_name": agent, "agent_version": version, "started_utc": started, "ended_utc": ended,
            "input_messages": [], "output_messages": [], "attributes": attrs, "error": error, "spans": spans}


def _span_range(spans: Sequence[Mapping]) -> tuple[str | None, str | None]:
    return _earliest(*(s["started_utc"] for s in spans)), _latest(*(s["ended_utc"] for s in spans))


def _finish(kind: str, unit: str, manifest: Mapping, conversations: list, *, export_attempt: int, dataset: str | None,
            text: _Text, sources: dict, problems: list) -> dict:
    project, placeholder = _project(manifest)
    rules = policy.rules_for(manifest, dataset)   # type: ignore[arg-type]
    factor = Decimal(str(util.lookup(manifest, "telemetry.agent_lens.byte_estimate_factor", "1.00")))
    expected = sum(len(c["turns"]) + sum(_count(t["spans"]) for t in c["turns"]) for c in conversations)
    return {"schema": SCHEMA, "unit": unit, "kind": kind, "project": project, "placeholder_project": placeholder,
            "export_attempt": export_attempt, "conversations": conversations, "expected_spans": expected,
            "planned_bytes": policy.planned_bytes(conversations, factor), "truncated_fields": text.truncated,
            "content_policy": {"dataset": dataset, **rules}, "sources": sources, "problems": problems}


# ---------------------------------------------------------------- solver job

def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content
                       if isinstance(p, Mapping) and p.get("type") == "text" and isinstance(p.get("text"), str))
    return ""


def _admission_only(record: Mapping) -> bool:
    """No outcome, end time or reply: the runner died while the request was open (2.5)."""
    return not record.get("outcome") or record.get("ended_utc") is None or record.get("reply") is None


def _usage(record: Mapping) -> dict | None:
    u = record.get("usage")
    if not isinstance(u, Mapping):
        return None
    pt, ct = u.get("prompt_tokens"), u.get("completion_tokens")
    if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (pt, ct)):
        return None                               # partial usage: never invent zeros (H2 complete_usage)

    def opt(v: object) -> int | None:
        return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None

    return {"input_tokens": pt, "output_tokens": ct, "total_tokens": opt(u.get("total_tokens")),
            "reasoning_tokens": opt(u.get("reasoning_tokens")), "cache_read_input_tokens": opt(u.get("cached_tokens")),
            "cache_creation_input_tokens": None}


def _llm_span(record: Mapping, outcome: str, example_id: str | None, end_utc: str, rules: Mapping[str, str],
              manifest: Mapping, prefix: str, text: _Text) -> dict:
    hashed = outcome in HASHED_OUTCOMES
    request = record.get("request") if isinstance(record.get("request"), Mapping) else None
    changed = False

    def rule(field: str) -> str:
        return "hash" if hashed else rules[field]

    system_prompt = util.lookup(manifest, "upstream.expect.system_prompt", None)
    system, inputs = [], []
    if request is not None:
        for m in request.get("messages") or ():
            if not isinstance(m, Mapping):
                continue
            body = _message_text(m.get("content"))
            if m.get("role") == "system":
                if body == system_prompt:         # upstream's fixed system prompt is not dataset text
                    system.append(body)
                else:
                    out, c = text.apply(body, rule("span_prompt"))
                    system.append(out)
                    changed |= c
                continue
            out, c = text.apply(body, rule("span_prompt"))
            changed |= c
            inputs.append({"role": str(m.get("role") or "user"), "parts": [{"type": "text", "content": out}]})
    elif isinstance(record.get("request_excerpt"), Mapping):
        ex = record["request_excerpt"]            # a refused record: only the hash and the length are used
        inputs.append({"role": "user", "parts": [{"type": "text",
                                                  "content": f"[sha256 {ex.get('sha256')} bytes {ex.get('bytes')}]"}]})
        changed = True
        text.truncated += 1
    outputs = []
    response = record.get("response") if isinstance(record.get("response"), Mapping) else None
    if response is not None and not hashed:
        choices = response.get("choices") or []
        msg = choices[0].get("message") if choices and isinstance(choices[0], Mapping) else None
        if isinstance(msg, Mapping):
            parts = []
            reasoning = msg.get("reasoning") or msg.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                out, c = text.apply(reasoning, rules["span_reasoning"])
                changed |= c
                parts.append({"type": "reasoning", "content": out})
            out, c = text.apply(_message_text(msg.get("content")), rules["span_reply"])
            changed |= c
            parts.append({"type": "text", "content": out})
            outputs.append({"role": str(msg.get("role") or "assistant"), "parts": parts})
    usage = _usage(record) if outcome == "completed" else None
    flags = record.get("flags") if isinstance(record.get("flags"), Mapping) else {}
    error = None
    if outcome == "interrupted":
        error = {"type": "interrupted", "message": "runner stopped while the request was open"}
    elif outcome in ERROR_OUTCOMES:
        code = (record.get("refusal") or {}).get("code") if isinstance(record.get("refusal"), Mapping) else None
        error = {"type": outcome, "message": str(code or outcome)}
    req = request or {}
    model = req.get("model") if isinstance(req.get("model"), str) else util.lookup(manifest, "provider.wire_model", "")
    finish = record.get("finish_reason")
    return {
        "type": "llm", "key": record["call_id"], "model": model or "", "provider_name": "wandb",
        "response_model": record.get("response_model") or "", "response_id": record.get("response_id") or "",
        "system_instructions": system, "input_messages": inputs, "output_messages": outputs,
        "request_max_tokens": req.get("max_tokens"), "request_temperature": req.get("temperature"),
        "finish_reasons": [finish] if isinstance(finish, str) and finish else [], "usage": usage,
        "started_utc": record.get("received_utc") or end_utc,
        "ended_utc": _latest(end_utc if outcome == "interrupted" else record.get("ended_utc") or end_utc,
                             record.get("received_utc")) or end_utc,
        "attributes": _attrs(prefix, call_id=record["call_id"], example_id=example_id, outcome=outcome,
                             usage_available=usage is not None, provider_attempts=len(record.get("attempts") or ()),
                             truncated_empty=bool(flags.get("truncated_empty", False)), content_truncated=changed),
        "error": error,
    }


def _pack(segments: Sequence[tuple[str, list]], capacity: int) -> list[list[tuple[str, list]]]:
    """Pack (turn key, spans) segments in order into conversations of at most `capacity` (turns + spans); a turn
    that does not fit is split in call order and continues as a turn of the same key in the next conversation."""
    capacity = max(capacity, 2)
    convs: list[list[tuple[str, list]]] = [[]]
    used = 0
    for key, spans in segments:
        rest = list(spans)
        if used and used + 1 + len(rest) > capacity:
            convs.append([])                      # a turn that fits a conversation of its own is not cut
            used = 0
        while True:
            room = capacity - used - 1
            take, rest = rest[:room], rest[room:]
            convs[-1].append((key, take))
            used += 1 + len(take)
            if not rest:
                break
            convs.append([])
            used = 0
    return [c for c in convs if c]


def plan_solver_job(job_record: Mapping, attempt: int, call_records: Sequence[Mapping], sidecar_rows: Sequence[Mapping],
                    manifest: dict, *, run_path: str | None = None, export_attempt: int = 1) -> dict:
    prefix, limit = _prefix(manifest), _limit(manifest)
    campaign = job_record.get("campaign") or manifest.get("campaign")
    dataset, split, candidate = job_record.get("dataset"), job_record.get("split"), job_record.get("candidate")
    att = next((a for a in job_record.get("attempts") or () if isinstance(a, Mapping) and a.get("attempt") == attempt), {})
    job_id = att.get("job_id") or f"{job_record.get('job_key')}.a{attempt}"
    problems: list[str] = []
    records = []
    for r in call_records:
        if not isinstance(r, Mapping) or not r.get("call_id"):
            problems.append("call_record_without_id")
            continue
        rj = r.get("job") if isinstance(r.get("job"), Mapping) else {}
        if rj.get("job_id") not in (None, job_id):
            problems.append(f"foreign_call:{r['call_id']}")
            continue
        records.append(r)
    records.sort(key=lambda r: (_t(r.get("received_utc")) or 0.0, str(r["call_id"])))
    sidecar = join.parse_sidecar(sidecar_rows) if sidecar_rows else {}
    roles = join.call_roles(sidecar, records, split=split)
    examples = join.join_examples(sidecar, records, dataset=dataset, split=split) if sidecar_rows else []
    problems += [p for p in (join.join_problems(sidecar, records, split=split) if sidecar_rows else []) if p != "no_sidecar"]
    rules = policy.rules_for(manifest, dataset)
    text = _Text()
    att_end = _latest(att.get("ended_utc"), *(r.get("ended_utc") for r in records)) or att.get("started_utc") or _EPOCH0
    by_role: dict[str, list] = {}
    for r in records:
        role, example_id = roles.get(r["call_id"], ("other", None))
        outcome = "interrupted" if _admission_only(r) else r.get("outcome")
        by_role.setdefault(role, []).append(_llm_span(r, outcome, example_id, att_end, rules, manifest, prefix, text))
    main_role = join.ROLE_BY_SPLIT.get(split, "other")
    order = ["learn", main_role] + (["other"] if main_role != "other" else [])
    segments = [(k, by_role.get(k, [])) for k in order if by_role.get(k) or k == main_role]
    total = sum(1 + len(s) for _, s in segments)
    groups = [("", segments)]
    if total > limit and by_role.get("learn"):
        groups = [("", [s for s in segments if s[0] != "learn"]), (".learn", [s for s in segments if s[0] == "learn"])]
    result = job_record.get("result") if isinstance(job_record.get("result"), Mapping) else {}
    if examples:
        n_ex, n_ok = len(examples), sum(1 for e in examples if e.get("was_correct") is True)
    elif att.get("outcome") == "ok" and isinstance(result.get("total"), int):
        n_ex, n_ok = result.get("total"), result.get("correct") if isinstance(result.get("correct"), int) else 0
    else:
        n_ex, n_ok = 0, 0
    version = str(att.get("candidate_sha256") or job_record.get("candidate_sha256") or "unknown")[:12]
    base = f"{campaign}.{job_id}"
    conversations = []
    for suffix, segs in groups:
        packed = _pack(segs, limit)
        for i, conv in enumerate(packed, start=1):
            turns = []
            for key, spans in conv:
                start, end = _span_range(spans)
                ex = (n_ex, n_ok) if key == main_role else (0, 0)
                attrs = _attrs(prefix, turn_key=key, examples=ex[0], correct=ex[1],
                               replayed_calls=sum(1 for s in spans if s["attributes"].get(f"{prefix}.outcome")
                                                  in ("replayed", "coalesced")),
                               refused_calls=sum(1 for s in spans
                                                 if s["attributes"].get(f"{prefix}.outcome") == "refused"))
                if len(packed) > 1:
                    attrs.update(_attrs(prefix, part=i, parts=len(packed)))
                turns.append(_turn(key, "metaharness-solver", version, start or att.get("started_utc") or att_end,
                                   end or att_end, attrs, spans))
            name = f"{candidate} / {dataset} / {split}" + (" / learn" if suffix else "") \
                + (f" / part {i}" if len(packed) > 1 else "")
            conversations.append({
                "conversation_id": _cid(base + suffix, part=i, export_attempt=export_attempt),
                "conversation_name": name,
                "attributes": _attrs(prefix, run=campaign, role="solver", candidate=candidate, dataset=dataset,
                                     split=split, attempt=attempt, phase=job_record.get("phase"), job_id=job_id,
                                     wandb_run_path=run_path),
                "turns": turns})
    return _finish("solver_job", f"job:{job_id}", manifest, conversations, export_attempt=export_attempt,
                   dataset=dataset, text=text,
                   sources={"job_sha256": _sha(job_record), "calls": len(records), "examples": len(examples)},
                   problems=problems)


# ---------------------------------------------------------------- proposer session

VERBATIM_INDEX_MISSING = "verbatim_index_missing"


def _dataset_names(manifest: Mapping) -> list[str]:
    names: list[str] = []
    content = util.lookup(manifest, "telemetry.content", {}) or {}
    for name in list(util.lookup(manifest, "upstream.expect.datasets", []) or []) + list(content):
        if isinstance(name, str) and name not in names:
            names.append(name)
    return names


def _proposer_rules(manifest: Mapping) -> list[str]:
    """Every content rule a proposer span can apply: the strictest reply and tool rules, and the tool rule of each
    dataset (a tool call attributed to one dataset uses that dataset's rule)."""
    strict = policy.rules_for(manifest, None)                            # type: ignore[arg-type]
    return [strict["span_reply"], strict["tool_result"]] + \
        [policy.rules_for(manifest, d)["tool_result"] for d in _dataset_names(manifest)]   # type: ignore[arg-type]


class _ToolText:
    """Proposer text: portable paths and hex masking for tool text, the verbatim guard (LD4) and the content rule.

    With a verbatim index, a text placed under a rule other than `hash` is first passed through `policy.guard_text`
    whenever some dataset has a stricter rule for that field: the index does not say whose text it found, so any
    finding withholds the text (its `hash` form). Without an index and with any proposer rule open, every text gets
    `hash` (`closed`)."""

    def __init__(self, manifest: Mapping, roots: Mapping[str, Path], text: _Text,
                 verbatim: "scan.VerbatimIndex | None" = None, closed: bool = False) -> None:
        self.manifest, self.roots, self.text = manifest, roots, text
        self.verbatim, self.closed = verbatim, closed
        self.strict = policy.rules_for(manifest, None)["tool_result"]   # type: ignore[arg-type]
        self._per_field = {f: [policy.rules_for(manifest, d)[f] for d in _dataset_names(manifest)]  # type: ignore[arg-type]
                           for f in wire.CONTENT_FIELDS}

    def _stricter_exists(self, field: str, rule: str) -> bool:
        return any(r != rule and policy.strictest([r, rule]) == r for r in self._per_field.get(field, ()))

    def apply(self, value: str, rule: str, field: str) -> tuple[str, bool]:
        if self.closed:
            return self.text.apply(value, "hash")
        if self.verbatim is not None and policy.parse_rule(rule)[0] != "hash" and self._stricter_exists(field, rule):
            guarded, replaced = policy.guard_text(value, self.verbatim)
            if replaced:
                self.text.truncated += 1
                return guarded, True
        return self.text.apply(value, rule)

    def prepare(self, raw: str, rule: str) -> tuple[str, bool, int]:
        portable = policy.portable(raw, self.roots)
        masked, n = policy.mask_hex40(portable)   # type: ignore[arg-type]
        out, changed = self.apply(masked, rule, "tool_result")          # type: ignore[arg-type]
        return out, changed or n > 0 or portable != raw, n


def _tool_span(tool: ToolUse, fallback_utc: str, tt: _ToolText, prefix: str, extra: dict) -> dict:
    raw_args = json.dumps(tool.input, sort_keys=True, ensure_ascii=False)
    dataset = policy.attribute_dataset(raw_args, tt.manifest)            # type: ignore[arg-type]
    rule = policy.rules_for(tt.manifest, dataset)["tool_result"] if dataset else tt.strict   # type: ignore[arg-type]
    args, c1, n1 = tt.prepare(raw_args, rule)
    result, c2, n2 = tt.prepare(tool.result_text or "", rule)
    started = tool.started_utc or fallback_utc
    ended = _latest(tool.ended_utc, started) or started
    return {"type": "tool", "key": tool.id, "name": tool.name, "tool_call_id": tool.id, "arguments": args,
            "result": result, "started_utc": started, "ended_utc": ended,
            "attributes": _attrs(prefix, dataset=dataset, result_found=tool.result_text is not None,
                                 hex40_masked=n1 + n2, content_truncated=c1 or c2, **extra),
            "error": {"type": "ToolError", "message": "the tool reported an error"} if tool.is_error else None}


def _message_spans(m: ApiMessage, source: str, rules: Mapping[str, str], tt: _ToolText, prefix: str,
                   extra: dict) -> list[dict]:
    usage = None
    if source == "transcript":                    # the stream's output counts are placeholders: no usage
        usage = {"input_tokens": m.input_tokens_total, "output_tokens": m.output_tokens,
                 "total_tokens": m.input_tokens_total + m.output_tokens, "reasoning_tokens": None,
                 "cache_read_input_tokens": m.cache_read, "cache_creation_input_tokens": m.cache_creation}
    outputs, changed = [], False
    if m.text:
        out, changed = tt.apply(m.text, rules["span_reply"], "span_reply")
        outputs = [{"role": "assistant", "parts": [{"type": "text", "content": out}]}]
    started = m.started_utc or m.ended_utc or _EPOCH0
    llm = {"type": "llm", "key": m.message_id, "model": m.model, "provider_name": "anthropic",
           "response_model": m.model, "response_id": m.message_id, "system_instructions": [], "input_messages": [],
           "output_messages": outputs, "request_max_tokens": None, "request_temperature": None,
           "finish_reasons": [m.stop_reason] if m.stop_reason else [], "usage": usage,
           "started_utc": started, "ended_utc": m.ended_utc or started,
           "attributes": _attrs(prefix, call_id=m.message_id, outcome="completed", usage_available=usage is not None,
                                effort=m.effort, thinking_chars=m.thinking_chars, content_truncated=changed, **extra),
           "error": None}
    return [llm] + [_tool_span(t, llm["ended_utc"], tt, prefix, extra) for t in m.tool_uses]


def _session_blocks(session: NormalizedSession, rules, tt: _ToolText, prefix: str) -> list[tuple[str, list]]:
    """[(kind, spans)] in message order: 'msg' blocks (an llm span and its tool spans) and 'sub' blocks (one subagent
    span, kept after the message that started it)."""
    main = [m for m in session.messages if not m.sidechain]
    subs: dict[str, list[ApiMessage]] = {}
    for m in session.messages:
        if m.sidechain:
            subs.setdefault(m.agent_id or "unknown", []).append(m)
    by_tool = {tool_id: aid for aid, tool_id in session.agent_links.items() if aid in subs}
    blocks: list[tuple[str, list]] = []
    placed: set[str] = set()

    def sub_block(aid: str) -> tuple[str, list]:
        children = [s for m in subs[aid] for s in _message_spans(m, session.source, rules, tt, prefix, {})]
        start, end = _span_range(children)
        return ("sub", [{"type": "subagent", "key": aid, "name": "subagent", "model": subs[aid][0].model,
                         "started_utc": start, "ended_utc": end,
                         "attributes": _attrs(prefix, agent_id=aid, tool_call_id=session.agent_links.get(aid),
                                              messages=len(subs[aid])),
                         "error": None, "spans": children}])

    for m in main:
        blocks.append(("msg", _message_spans(m, session.source, rules, tt, prefix, {})))
        for t in m.tool_uses:
            aid = by_tool.get(t.id)
            if aid and aid not in placed:
                placed.add(aid)
                blocks.append(sub_block(aid))
    for aid in sorted(subs):
        if aid not in placed:
            blocks.append(sub_block(aid))
    return blocks


def _flatten_subagent(sub: Mapping, prefix: str) -> list[list]:
    """The children of an oversize subagent as message blocks with the attribute `subagent` = its agent id."""
    out: list[list] = []
    for child in sub["spans"]:
        child = {**child, "attributes": {**child["attributes"], f"{prefix}.subagent": sub["key"]}}
        if child["type"] == "llm" or not out:
            out.append([child])
        else:
            out[-1].append(child)
    return out


def _parts(blocks: Sequence[tuple[str, list]], capacity: int, prefix: str, problems: list) -> list[list]:
    parts: list[list] = [[]]

    def add(spans: list) -> None:
        if parts[-1] and _count(parts[-1]) + _count(spans) > capacity:
            parts.append([])
        parts[-1].extend(spans)

    for kind, spans in blocks:
        size = _count(spans)
        if size <= capacity:
            add(spans)
        elif kind == "sub":                       # its own run of parts, children flattened
            if parts[-1]:
                parts.append([])
            for b in _flatten_subagent(spans[0], prefix):
                add(b)
            parts.append([])
        else:
            problems.append(f"message_over_span_limit:{spans[0]['key']}")
            for i in range(0, len(spans), capacity):
                add(spans[i:i + capacity])
    return [p for p in parts if p] or [[]]


def plan_proposer(session_record: Mapping, session: NormalizedSession, manifest: dict, *, roots: Mapping[str, Path],
                  verbatim: "scan.VerbatimIndex | None" = None, export_attempt: int = 1) -> dict:
    """`verbatim` is the export's verbatim index (LD4). Without it, while any rule a proposer span can apply is not
    `hash`, every text is hashed and the plan records the problem `verbatim_index_missing` (fail closed)."""
    prefix, limit = _prefix(manifest), _limit(manifest)
    campaign = session_record.get("campaign") or manifest.get("campaign")
    key = session_record.get("session_key")
    problems: list[str] = list(f"session_warning:{w}" for w in session.warnings)
    text = _Text()
    rules = policy.rules_for(manifest, None)
    closed = verbatim is None and any(r != "hash" for r in _proposer_rules(manifest))
    if closed:
        problems.append(VERBATIM_INDEX_MISSING)
    tt = _ToolText(manifest, roots, text, verbatim, closed)
    parts = _parts(_session_blocks(session, rules, tt, prefix), limit - 1, prefix, problems)
    result_event = session_record.get("result_event") if isinstance(session_record.get("result_event"), Mapping) else {}
    error = None if result_event.get("present") else {"type": "killed", "message": "session ended without a result event"}
    prompt = session.prompt_text
    version = str((session_record.get("cli") or {}).get("version") or session.cli_version or "unknown")
    rec_start = _earliest(session_record.get("started_utc"), session.started_utc)
    rec_end = _latest(session_record.get("ended_utc"), session.ended_utc)
    conversations = []
    for i, spans in enumerate(parts, start=1):
        start, end = _span_range(spans)
        if i == 1:
            start = _earliest(start, rec_start)
        if i == len(parts):
            end = _latest(end, rec_end)
        start = start or end or _EPOCH0
        end = end or start
        if not spans and not (rec_start or rec_end):
            problems.append("session_without_times")
        attrs = _attrs(prefix, turn_key=key, session_key=key, iteration=session_record.get("iteration"),
                       outcome=session_record.get("outcome"), part=i, parts=len(parts),
                       prompt_sha256=hashlib.sha256(prompt.encode("utf-8", "surrogatepass")).hexdigest()
                       if isinstance(prompt, str) else None,
                       prompt_chars=len(prompt) if isinstance(prompt, str) else None)
        conversations.append({
            "conversation_id": _cid(f"{campaign}.proposer.{key}", part=i, export_attempt=export_attempt),
            "conversation_name": f"proposer / {key}" + (f" / part {i}" if len(parts) > 1 else ""),
            "attributes": _attrs(prefix, run=campaign, role="proposer", session_key=key,
                                 iteration=session_record.get("iteration"),
                                 permission_mode=session_record.get("permission_mode"),
                                 model=session_record.get("resolved_model")),
            "turns": [_turn(str(key), "metaharness-proposer", version, start, end, attrs, spans, error)]})
    return _finish("proposer_session", f"proposer:{key}", manifest, conversations, export_attempt=export_attempt,
                   dataset=None, text=text,
                   sources={"session_sha256": _sha(session_record), "messages": len(session.messages),
                            "tool_uses": sum(len(m.tool_uses) for m in session.messages)},
                   problems=problems)


# ---------------------------------------------------------------- outer loop

def _guard(value: object, key: str | None, verbatim, rule: str, text: _Text) -> object:
    if isinstance(value, Mapping):
        # inside a guarded key (proposer text) every nested string stays guarded, whatever its own key
        return {k: _guard(v, key if key in _GUARDED_KEYS else k, verbatim, rule, text) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_guard(v, key, verbatim, rule, text) for v in value]
    if not isinstance(value, str):
        return value
    if key in _OURS_KEYS and key not in _GUARDED_KEYS and _IDENT_RE.fullmatch(value):
        return value                              # our own identifiers and enums
    guarded, replaced = policy.guard_text(value, verbatim)
    if replaced:
        text.truncated += 1
        return guarded
    return text.apply(value, rule)[0]


def _journal_span(entries: Sequence[Mapping], op: str, key: str) -> tuple[str | None, str | None]:
    mine = sorted((e for e in entries if isinstance(e, Mapping) and e.get("op") == op and e.get("key") == key),
                  key=lambda e: (e.get("seq") if isinstance(e.get("seq"), int) else 0))
    starts = [e for e in mine if e.get("status") == "started"]
    start = starts[-1] if starts else None
    after = [e for e in mine if e.get("status") not in ("started", "handle")
             and (start is None or (e.get("seq") or 0) > (start.get("seq") or 0))]
    end = after[0] if after else None
    s = start.get("utc") if start else (end.get("utc") if end else None)
    return s, (end.get("utc") if end else None)


def plan_outer_iteration(iteration_record: Mapping, journal_entries: Sequence[Mapping], manifest: dict, *,
                         verbatim: "scan.VerbatimIndex | None" = None, export_attempt: int = 1) -> dict:
    prefix = _prefix(manifest)
    campaign = manifest.get("campaign")
    nnn = int(iteration_record.get("iteration") or 0)
    it_key = f"iter{nnn:03d}"
    per_conv = int(util.lookup(manifest, "telemetry.agent_lens.loop_iterations_per_conversation", 15))
    max_cands = int(util.lookup(manifest, "search.max_candidates_per_iteration", 3))
    n = 1 + nnn // per_conv
    text = _Text()
    rule = policy.rules_for(manifest, None)["tool_result"]
    problems: list[str] = []
    rec_start, rec_end = iteration_record.get("started_utc"), iteration_record.get("ended_utc")
    cursor = [rec_start]

    def span(name: str, op: str | None, jkey: str | None, args: dict, result: dict, error: dict | None = None) -> dict:
        s, e = _journal_span(journal_entries, op, jkey) if op else (None, None)
        if s is None:
            problems.append(f"no_journal_timing:{name}")
            s = cursor[0] or rec_start or rec_end or _EPOCH0
        e = _latest(e, s) or s
        cursor[0] = _latest(cursor[0], e)
        return {"type": "tool", "key": f"{it_key}.{name}", "name": name, "tool_call_id": f"{it_key}.{name}",
                "arguments": json.dumps(_guard(args, None, verbatim, rule, text), sort_keys=True,
                                        separators=(",", ":"), ensure_ascii=False),
                "result": json.dumps(_guard(result, None, verbatim, rule, text), sort_keys=True,
                                     separators=(",", ":"), ensure_ascii=False),
                "started_utc": s, "ended_utc": e, "attributes": _attrs(prefix, iteration=nnn), "error": error}

    kind = iteration_record.get("kind")
    cands = [c for c in iteration_record.get("candidates") or () if isinstance(c, Mapping)]
    spans = []
    if kind != "baselines":
        done = next((e for e in journal_entries if isinstance(e, Mapping) and e.get("op") == "proposer"
                     and e.get("key") == it_key and e.get("status") in ("complete", "replayed")), None)
        detail = done.get("detail") if done and isinstance(done.get("detail"), Mapping) else {}
        spans.append(span("propose", "proposer", it_key, {"iteration": nnn, "session_key": iteration_record.get("session_key")},
                          {"outcome": detail.get("outcome"), "candidates": len(cands),
                           "proposer_list_usd": util.usd(int(iteration_record.get("proposer_list_price_micro") or 0))},
                          {"type": "ToolError", "message": "the proposer returned no candidates"}
                          if kind == "proposer_failed" else None))
    checked = [c for c in cands if c.get("rejected") in (None, "import", "loaded_class")][:max_cands]
    for c in checked if kind != "baselines" else ():
        rejected = c.get("rejected")
        spans.append(span(f"import_check:{c.get('name')}", "import_check", it_key,
                          {"candidate": c.get("name"), "base_system": c.get("base_system")},
                          {"valid": bool(c.get("valid")), "rejected": rejected},
                          {"type": "ToolError", "message": f"candidate rejected: {rejected}"} if rejected else None))
    for c in [c for c in cands if c.get("valid") or kind == "baselines"][:max_cands if kind != "baselines" else None]:
        datasets = c.get("datasets") if isinstance(c.get("datasets"), Mapping) else {}
        spans.append(span(f"benchmark:{c.get('name')}", "benchmark", f"val.{c.get('name')}",
                          {"candidate": c.get("name"), "base_system": c.get("base_system"), "axis": c.get("axis"),
                           "hypothesis": c.get("hypothesis"), "components": c.get("components")},
                          {"datasets": {k: dict(v) for k, v in sorted(datasets.items()) if isinstance(v, Mapping)},
                           "avg_val_all_datasets": c.get("avg_val_all_datasets"),
                           "upstream_avg_val": c.get("upstream_avg_val"), "complete": c.get("complete"),
                           "delta_vs_previous_best": c.get("delta_vs_previous_best")}))
    if kind not in ("proposer_failed",):
        settled = _journal_span(journal_entries, "iteration", it_key)[1]
        start = cursor[0] or rec_start or settled or rec_end or _EPOCH0
        end = _latest(settled or rec_end, start) or start
        spans.append({"type": "tool", "key": f"{it_key}.frontier_update", "name": "frontier_update",
                      "tool_call_id": f"{it_key}.frontier_update",
                      "arguments": json.dumps(_guard({"frontier_before": iteration_record.get("frontier_before")},
                                                     None, verbatim, rule, text), sort_keys=True,
                                              separators=(",", ":"), ensure_ascii=False),
                      "result": json.dumps({k: iteration_record.get(k) for k in
                                            ("best_complete_before", "best_complete_after", "improvement",
                                             "evidence_exposed")}, sort_keys=True, separators=(",", ":")),
                      "started_utc": start, "ended_utc": end, "attributes": _attrs(prefix, iteration=nnn),
                      "error": None})
    s, e = _span_range(spans)
    t_start = _earliest(rec_start, s) or _EPOCH0
    t_end = _latest(rec_end, e, t_start) or t_start
    version = str(util.lookup(manifest, "upstream.commit", "") or "unknown")[:12]
    turn = _turn(it_key, "metaharness-search", version, t_start, t_end,
                 _attrs(prefix, turn_key=it_key, iteration=nnn, kind=kind, candidates=len(cands),
                        candidates_valid=sum(1 for c in cands if c.get("valid")),
                        evidence_exposed=bool(iteration_record.get("evidence_exposed"))), spans)
    first, last = (n - 1) * per_conv, n * per_conv - 1
    conv = {"conversation_id": _cid(f"{campaign}.search.p{n}", export_attempt=export_attempt),
            "conversation_name": f"search / iterations {first}-{last}",
            "attributes": _attrs(prefix, run=campaign, role="search", part=n), "turns": [turn]}
    return _finish("outer_loop", f"loop:{it_key}", manifest, [conv], export_attempt=export_attempt, dataset=None,
                   text=text, sources={"iteration_sha256": _sha(iteration_record),
                                       "journal_entries": len(journal_entries)}, problems=problems)


# ---------------------------------------------------------------- validation

def _allow(rule: str) -> int | None:
    kind, n = policy.parse_rule(rule)
    return None if kind == "full" else _HASH_ALLOW if kind == "hash" else n + _CUT_ALLOW


def _field_limits(plan: Mapping, manifest: dict) -> dict[str, int | None]:
    dataset = (plan.get("content_policy") or {}).get("dataset")
    rules = policy.rules_for(manifest, dataset)
    limits = {f: _allow(r) for f, r in rules.items()}
    if dataset is None:                           # a proposer tool call may use the rule of the dataset it names
        names = list(util.lookup(manifest, "upstream.expect.datasets", []) or [])
        allows = [_allow(policy.rules_for(manifest, d)["tool_result"]) for d in names] + [limits["tool_result"]]
        limits["tool_result"] = None if None in allows else max(allows)
    return limits


def _summary_strings(text: object) -> list:
    """The strings inside an outer-loop JSON summary (the rule applies to each of them, 3.23); the text itself when it
    is not JSON."""
    try:
        value = json.loads(text)                  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return [text]
    out: list = []

    def walk(v: object) -> None:
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, Mapping):
            for k, x in v.items():
                out.append(k)
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(value)
    return out


def _walk(spans: Sequence[Mapping]):
    for s in spans:
        yield s
        yield from _walk(s.get("spans") or ())


def validate_plan(plan: Mapping, manifest: dict) -> list[str]:
    """Problems with a span plan ([] when it may be handed to an adapter)."""
    out: list[str] = []
    prefix, limit = _prefix(manifest), _limit(manifest)
    limits = _field_limits(plan, manifest)
    system_prompt = util.lookup(manifest, "upstream.expect.system_prompt", None)
    convs = plan.get("conversations") or []
    counted = 0

    def text_ok(where: str, field: str, value: object) -> None:
        cap = limits.get(field)
        if isinstance(value, str) and cap is not None and len(value) > cap:
            out.append(f"{where}: {field} text of {len(value)} chars exceeds the policy")

    for conv in convs:
        cid = conv.get("conversation_id")
        if not isinstance(cid, str) or not _ID_RE.fullmatch(cid):
            out.append(f"conversation id {str(cid)[:140]!r} is not URL-safe or longer than 128")
        out += [f"{cid}: {p}" for p in policy.check_attributes(conv.get("attributes") or {}, prefix)]
        turns = conv.get("turns") or []
        n = len(turns) + sum(_count(t.get("spans") or ()) for t in turns)
        counted += n
        if n > limit:
            out.append(f"{cid}: {n} spans exceed the limit of {limit}")
        for turn in turns:
            tk = f"{cid}/{turn.get('turn_key')}"
            out += [f"{tk}: {p}" for p in policy.check_attributes(turn.get("attributes") or {}, prefix)]
            if turn.get("input_messages") or turn.get("output_messages"):
                out.append(f"{tk}: a turn carries content")
            if turn.get("usage") is not None:
                out.append(f"{tk}: usage on a turn")
            t0, t1 = _t(turn.get("started_utc")), _t(turn.get("ended_utc"))
            if t0 is None or t1 is None or t0 > t1:
                out.append(f"{tk}: bad turn times")
            for s in _walk(turn.get("spans") or ()):
                sk = f"{tk}/{s.get('key')}"
                out += [f"{sk}: {p}" for p in policy.check_attributes(s.get("attributes") or {}, prefix)]
                if s.get("type") not in ("llm", "tool", "subagent"):
                    out.append(f"{sk}: unknown span type")
                if s.get("type") != "llm" and s.get("usage") is not None:
                    out.append(f"{sk}: usage on a {s.get('type')} span")
                s0, s1 = _t(s.get("started_utc")), _t(s.get("ended_utc"))
                if s0 is None or s1 is None or s0 > s1:
                    out.append(f"{sk}: bad span times")
                elif t0 is not None and t1 is not None and (s0 < t0 or s1 > t1):
                    out.append(f"{sk}: span outside its turn")
                for m in s.get("input_messages") or ():
                    for p in m.get("parts") or ():
                        text_ok(sk, "span_prompt", p.get("content"))
                for m in s.get("output_messages") or ():
                    for p in m.get("parts") or ():
                        text_ok(sk, "span_reasoning" if p.get("type") == "reasoning" else "span_reply", p.get("content"))
                for sysmsg in s.get("system_instructions") or ():
                    if sysmsg != system_prompt:
                        text_ok(sk, "span_prompt", sysmsg)
                if s.get("type") == "tool":
                    for value in (s.get("arguments"), s.get("result")):
                        for piece in _summary_strings(value) if plan.get("kind") == "outer_loop" else [value]:
                            text_ok(sk, "tool_result", piece)
    for name in wire.NEVER_PUBLISH_TEXT:          # 2.3 rule 16: a plan is never made under rules that open LawBench
        if any(r != "hash" for r in policy.rules_for(manifest, name).values()):
            out.append(f"content rules open {name} text, which is never published")
    if plan.get("expected_spans") != counted:
        out.append(f"expected_spans {plan.get('expected_spans')} != {counted} turns and spans")
    if plan.get("kind") == "outer_loop":
        turns = [t for c in convs for t in c.get("turns") or ()]
        cap = 3 + 2 * int(util.lookup(manifest, "search.max_candidates_per_iteration", 3))
        if len(turns) != 1:
            out.append(f"outer-loop plan has {len(turns)} turns, not 1")
        elif 1 + _count(turns[0].get("spans") or ()) > cap:
            out.append(f"outer-loop iteration has more than {cap} spans")
    return out
