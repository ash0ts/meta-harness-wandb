# SPDX-License-Identifier: Apache-2.0
# Adapted from ash0ts/harness2-wandb@31b0f82 harness2/showcase/gateway.py L25-L54 and transport_v4.py L25-L32. Changes: SSE line parser, error-chunk preservation folded into merge_stream, empty choices without a choice, last non-null finish_reason, both reasoning names merged into "reasoning", reasoning_field, completion-body builder.
"""Server-sent-event parsing and stream merging for the provider path. Pure; no I/O."""

from __future__ import annotations

import copy
import json
from typing import Iterable, Iterator, Mapping, Sequence

__all__ = ["iter_sse", "merge_stream", "to_completion_body", "reasoning_field", "REASONING_NAMES"]

# The two names under which a provider streams reasoning text, in the order they are tried.
REASONING_NAMES = ("reasoning", "reasoning_content")


def _split_lines(lines: Iterable[bytes]) -> Iterator[bytes]:
    for item in lines:
        if isinstance(item, str):
            item = item.encode("utf-8")
        if b"\n" in item.rstrip(b"\r\n"):
            parts = item.split(b"\n")
            for part in parts:
                yield part.rstrip(b"\r")
        else:
            yield item.rstrip(b"\r\n")


def iter_sse(lines: Iterable[bytes]) -> Iterator[tuple[str, object]]:
    """Yield ("chunk", dict), ("done", None) or ("bad", bytes) for each data line.

    Comments (": ..."), blank lines and other fields (event:, id:, retry:) are skipped.
    A data line whose payload is not a JSON object is "bad"; the bytes are the line
    without its line terminator. An item that holds several lines is split on b"\\n".
    """
    for line in _split_lines(lines):
        if not line.startswith(b"data:"):
            continue
        payload = line[5:]
        if payload.startswith(b" "):
            payload = payload[1:]
        stripped = payload.strip()
        if stripped == b"[DONE]":
            yield ("done", None)
            continue
        try:
            obj = json.loads(stripped.decode("utf-8"), parse_constant=_reject_constant)
        except (UnicodeDecodeError, ValueError):
            yield ("bad", bytes(line))
            continue
        if not isinstance(obj, dict):
            yield ("bad", bytes(line))
            continue
        yield ("chunk", obj)


def _reject_constant(name: str) -> object:
    # NaN, Infinity and -Infinity are not JSON (RFC 8259); a line carrying one is "bad".
    raise ValueError("non-JSON constant " + name)


def _choice0(chunk: Mapping) -> Mapping | None:
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return None
    for choice in choices:
        if isinstance(choice, Mapping) and choice.get("index", 0) == 0:
            return choice
    return None


def merge_stream(chunks: Sequence[Mapping]) -> dict:
    """Merge streamed chunks into one completion-shaped dict.

    Content and reasoning (either wire name; merged under "reasoning") of choice index 0
    are concatenated; the last non-null id, model, usage (never summed) and finish_reason
    are kept; the last non-null "error" is kept; choices is [] when no chunk carried a
    choice of index 0. Tool-call deltas are merged as in Harness2.
    """
    result: dict = {"choices": []}
    content: list[str] = []
    reasoning: list[str] = []
    tools: dict = {}
    finish_reason = None
    seen_choice = False
    for chunk in chunks:
        if not isinstance(chunk, Mapping):
            continue
        for key in ("id", "model", "usage"):
            if chunk.get(key) is not None:
                result[key] = copy.deepcopy(chunk[key])
        if chunk.get("error") is not None:
            result["error"] = copy.deepcopy(chunk["error"])
        choice = _choice0(chunk)
        if choice is None:
            continue
        seen_choice = True
        delta = choice.get("delta")
        if not isinstance(delta, Mapping):
            delta = choice.get("message") if isinstance(choice.get("message"), Mapping) else {}
        text = delta.get("content")
        if isinstance(text, str) and text:
            content.append(text)
        # delta.reasoning or delta.reasoning_content: one per delta, so a provider that
        # sends the same text under both names does not double it.
        for name in REASONING_NAMES:
            value = delta.get(name)
            if isinstance(value, str) and value:
                reasoning.append(value)
                break
        tool_calls = delta.get("tool_calls")
        for tool in tool_calls if isinstance(tool_calls, list) else []:
            if not isinstance(tool, Mapping):
                continue
            tool_index = tool.get("index", 0)
            if not isinstance(tool_index, (int, str)) or isinstance(tool_index, bool):
                continue
            item = tools.setdefault(
                tool_index,
                {"type": "function", "id": "", "function": {"name": "", "arguments": ""}},
            )
            if tool.get("id"):
                item["id"] = tool["id"]
            function = tool.get("function")
            if isinstance(function, Mapping):
                for key, value in function.items():
                    if isinstance(value, str) and value:
                        item["function"][key] = item["function"].get(key, "") + value
        if choice.get("finish_reason") is not None:
            finish_reason = choice["finish_reason"]
    if seen_choice:
        message: dict = {"role": "assistant", "content": "".join(content)}
        if reasoning:
            message["reasoning"] = "".join(reasoning)
        if tools:
            message["tool_calls"] = [tools[k] for k in sorted(tools, key=str)]
        result["choices"] = [{"index": 0, "message": message, "finish_reason": finish_reason}]
    return result


def reasoning_field(chunks: Sequence[Mapping]) -> str | None:
    """The wire name of the first non-empty reasoning delta of choice 0, or None."""
    for chunk in chunks:
        if not isinstance(chunk, Mapping):
            continue
        choice = _choice0(chunk)
        if choice is None:
            continue
        delta = choice.get("delta")
        if not isinstance(delta, Mapping):
            continue
        for name in REASONING_NAMES:
            value = delta.get(name)
            if isinstance(value, str) and value:
                return name
    return None


def to_completion_body(merged: Mapping, *, request_model: str, created: int) -> dict:
    """Build the non-streaming chat.completion body delivered to the worker.

    model is the provider's echoed model, else request_model; content is always a string;
    reasoning appears only when non-empty; usage only when the provider sent it.
    """
    choices = merged.get("choices") or []
    choice = choices[0] if choices and isinstance(choices[0], Mapping) else {}
    message_in = choice.get("message") if isinstance(choice.get("message"), Mapping) else {}
    content = message_in.get("content")
    message: dict = {"role": "assistant", "content": content if isinstance(content, str) else ""}
    reasoning = message_in.get("reasoning")
    if not reasoning:
        reasoning = message_in.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        message["reasoning"] = reasoning
    model = merged.get("model")
    body: dict = {
        "id": merged.get("id"),
        "object": "chat.completion",
        "created": created,
        "model": model if model else request_model,
        "choices": [{"index": 0, "message": message, "finish_reason": choice.get("finish_reason")}],
    }
    if merged.get("usage") is not None:
        body["usage"] = copy.deepcopy(merged["usage"])
    return body
