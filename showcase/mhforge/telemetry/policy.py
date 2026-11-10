# SPDX-License-Identifier: Apache-2.0
# Adapted from ash0ts/harness2-wandb@31b0f82 harness2/showcase/report_v4.py (portable). Changes: named roots instead of
# the runtime's state and checkout paths, path-boundary matching, the user name dropped from any remaining home path, a
# walk over dict, list and str instead of a JSON round trip; plus new content rules, byte counting, hex masking and
# attribute checks (spec 3.24).
"""Content policy, attribute rules, byte counting, hex masking and portable paths (spec 3.24). Pure: no file, no SDK."""

from __future__ import annotations

import hashlib
import json
import math
import re
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Mapping, Sequence

from .. import scan, util, wire

__all__ = [
    "CREDENTIAL_SUFFIXES", "MAX_ATTRIBUTES", "apply_text", "strictest", "rules_for", "attribute_dataset", "guard_text",
    "mask_hex40", "span_bytes", "planned_bytes", "check_attributes", "portable", "parse_rule",
]

CREDENTIAL_SUFFIXES = ("apikey", "apitoken", "accesstoken", "secretkey", "privatekey", "clientsecret", "refreshtoken",
                       "sessiontoken")
MAX_ATTRIBUTES = 64
DEFAULT_RULE = "hash"

_TRUNCATE_RE = re.compile(r"truncate:([1-9][0-9]{0,9})")
_HEX40_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{40}(?![0-9a-fA-F])")
_HOME_RE = re.compile(r"/(Users|home)/[A-Za-z0-9._-]+(?![A-Za-z0-9._-])")


# ---------------------------------------------------------------- content rules

def parse_rule(rule: str) -> tuple[str, int | None]:
    """'hash' -> ('hash', None); 'full' -> ('full', None); 'truncate:N' -> ('truncate', N). ValueError otherwise."""
    if rule in ("hash", "full"):
        return rule, None
    m = _TRUNCATE_RE.fullmatch(rule) if isinstance(rule, str) else None
    if not m:
        raise ValueError(f"unknown content rule {str(rule)[:40]!r}")
    return "truncate", int(m.group(1))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _hash_form(text: str) -> str:
    return f"[sha256 {_sha(text)} chars {len(text)}]"


def apply_text(text: str | None, rule: str) -> tuple[str | None, bool]:
    """(text after the rule, whether anything was removed). None stays None under every rule."""
    kind, n = parse_rule(rule)
    if text is None:
        return None, False
    if not isinstance(text, str):
        raise TypeError("apply_text takes a str or None")
    if kind == "full":
        return text, False
    if kind == "hash":
        return _hash_form(text), True
    half = n // 2
    if len(text) <= 2 * half:
        return text, False
    cut = len(text) - 2 * half
    marker = f"\n...[mhforge cut {cut} chars, sha256 {_sha(text)[:12]}]...\n"
    head = text[:half]
    tail = text[len(text) - half:] if half else ""
    return head + marker + tail, True


def _strictness(rule: str) -> tuple[int, int]:
    kind, n = parse_rule(rule)
    if kind == "hash":
        return (0, 0)
    if kind == "truncate":
        return (1, n)
    return (2, 0)


def strictest(rules: Sequence[str]) -> str:
    """'hash' before any 'truncate:N' (the smaller N first) before 'full'. No rule at all gives 'hash'."""
    rules = list(rules)
    if not rules:
        return DEFAULT_RULE
    return min(rules, key=_strictness)


def _datasets(manifest: Mapping) -> list[str]:
    names: list[str] = []
    content = util.lookup(manifest, "telemetry.content", {}) or {}
    expected = util.lookup(manifest, "upstream.expect.datasets", []) or []
    for name in list(expected) + list(content):
        if isinstance(name, str) and name not in names:
            names.append(name)
    return names


def rules_for(manifest: dict, dataset: str | None) -> dict[str, str]:
    """The four rules of telemetry.content[dataset] ('hash' where one is absent); for None, field by field the strictest
    rule over all datasets."""
    content = util.lookup(manifest, "telemetry.content", {}) or {}
    if dataset is not None:
        rules = content.get(dataset) if isinstance(content, Mapping) else None
        rules = rules if isinstance(rules, Mapping) else {}
        out = {}
        for field in wire.CONTENT_FIELDS:
            rule = rules.get(field, DEFAULT_RULE)
            parse_rule(rule)
            out[field] = rule
        return out
    per = [rules_for(manifest, name) for name in _datasets(manifest)]
    return {field: strictest([r[field] for r in per]) for field in wire.CONTENT_FIELDS}


def attribute_dataset(text: str, manifest: dict) -> str | None:
    """The one dataset whose name, or whose upstream.expect.dataset_dirs value, occurs in the text (not inside a longer
    run of letters and digits); None when none or several do."""
    if not isinstance(text, str) or not text:
        return None
    dirs = util.lookup(manifest, "upstream.expect.dataset_dirs", {}) or {}
    found = []
    for name in _datasets(manifest):
        needles = [name]
        d = dirs.get(name) if isinstance(dirs, Mapping) else None
        if isinstance(d, str) and d:
            needles.append(d)
        for needle in needles:
            if re.search(r"(?<![A-Za-z0-9])" + re.escape(needle) + r"(?![A-Za-z0-9])", text):
                found.append(name)
                break
    return found[0] if len(found) == 1 else None


def guard_text(text: str, index: "scan.VerbatimIndex | None") -> tuple[str, bool]:
    """The text unchanged, or its `hash` form when scan.scan_verbatim finds dataset text in it."""
    if index is None or not isinstance(text, str):
        return text, False
    if scan.scan_verbatim(text, index):
        return _hash_form(text), True
    return text, False


def mask_hex40(text: str) -> tuple[str, int]:
    """Every run of exactly 40 hex characters that is not part of a longer hex run -> '[hex40 sha256 <12 hex>]'."""
    if not isinstance(text, str):
        return text, 0
    count = 0

    def _sub(m: re.Match) -> str:
        nonlocal count
        count += 1
        return f"[hex40 sha256 {_sha(m.group(0))[:12]}]"

    return _HEX40_RE.sub(_sub, text), count


# ---------------------------------------------------------------- byte counting (2.13)

def span_bytes(span: Mapping) -> int:
    """len(json.dumps(<span without its 'spans' list>)) with Python's default separators and ASCII escaping."""
    return len(json.dumps({k: v for k, v in span.items() if k != "spans"}))


def _walk_spans(spans) -> int:
    total = 0
    for s in spans or ():
        total += span_bytes(s) + _walk_spans(s.get("spans"))
    return total


def planned_bytes(conversations: Sequence[Mapping], factor: Decimal) -> int:
    """ceil(factor * sum of span_bytes over every turn and every span at every depth)."""
    if isinstance(factor, float) or isinstance(factor, bool):
        raise TypeError("factor must be a Decimal, int or decimal string")
    factor = Decimal(factor) if not isinstance(factor, Decimal) else factor
    raw = 0
    for conv in conversations:
        for turn in conv.get("turns") or ():
            raw += span_bytes(turn) + _walk_spans(turn.get("spans"))
    return int((factor * raw).to_integral_value(rounding=ROUND_CEILING))


# ---------------------------------------------------------------- attributes

def _credential_shaped(leaf: str) -> bool:
    alnum = re.sub(r"[^a-z0-9]", "", leaf.lower())
    return alnum == "authorization" or any(alnum.endswith(s) for s in CREDENTIAL_SUFFIXES)


def check_attributes(attrs: Mapping[str, object], prefix: str) -> list[str]:
    """Problems with an attribute map: keys must be leaves '<prefix>.<leaf>', values str, int, float or bool (finite,
    non-empty), no credential-shaped key or value, at most 64 keys."""
    problems: list[str] = []
    if not isinstance(attrs, Mapping):
        return ["attributes are not a mapping"]
    if len(attrs) > MAX_ATTRIBUTES:
        problems.append(f"{len(attrs)} attributes, more than {MAX_ATTRIBUTES}")
    head = prefix + "."
    for key, value in attrs.items():
        if not isinstance(key, str):
            problems.append(f"non-string key {type(key).__name__}")
            continue
        shown = key[:80]
        leaf = key[len(head):] if key.startswith(head) else None
        if not leaf or "." in leaf:
            problems.append(f"{shown}: not a leaf key under {prefix!r}")
        elif _credential_shaped(leaf):
            problems.append(f"{shown}: credential-shaped key")
        if isinstance(value, bool) or isinstance(value, int):
            continue
        if isinstance(value, float):
            if not math.isfinite(value):
                problems.append(f"{shown}: non-finite float")
            continue
        if isinstance(value, str):
            if value == "":
                problems.append(f"{shown}: empty string")
            elif scan.scan_bytes(value.encode("utf-8", "surrogatepass")):
                problems.append(f"{shown}: credential-shaped value")
            continue
        problems.append(f"{shown}: value of type {type(value).__name__}")
    return problems


# ---------------------------------------------------------------- portable paths

def _root_patterns(roots: Mapping[str, Path]) -> list[tuple[re.Pattern[str], str]]:
    items = []
    for name, root in roots.items():
        text = str(root).rstrip("/")
        if not isinstance(name, str) or not text.startswith("/") or len(text) < 2:
            continue
        items.append((text, f"<{name}>"))
    items.sort(key=lambda kv: len(kv[0]), reverse=True)    # the workspace before the state directory that holds it
    return [(re.compile(re.escape(text) + r"(?![A-Za-z0-9._-])"), label) for text, label in items]


def _portable_str(text: str, patterns) -> str:
    for pattern, label in patterns:
        text = pattern.sub(lambda _m, _l=label: _l, text)
    return _HOME_RE.sub(lambda m: f"/{m.group(1)}/<user>", text)


def _portable(value: object, patterns) -> object:
    if isinstance(value, str):
        return _portable_str(value, patterns)
    if isinstance(value, Mapping):
        return {(_portable_str(k, patterns) if isinstance(k, str) else k): _portable(v, patterns) for k, v in value.items()}
    if isinstance(value, list):
        return [_portable(v, patterns) for v in value]
    if isinstance(value, tuple):
        return tuple(_portable(v, patterns) for v in value)
    return value


def portable(value: object, roots: Mapping[str, Path]) -> object:
    """Absolute host paths -> '<state>', '<workspace>', '<example>', '<home>' (one label per key of `roots`, the longest
    root first); any other /Users/<name> or /home/<name> loses the name. Walks dict (keys too), list, tuple and str."""
    return _portable(value, _root_patterns(roots))
