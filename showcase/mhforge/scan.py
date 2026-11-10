# SPDX-License-Identifier: MIT
"""Pure scanner (spec 3.24): secret patterns, known values, key digests; privacy patterns and a host name given as data;
the verbatim dataset-text index.

It reads nothing from the machine: no host name, no user name, no environment, no dataset file. Whatever it needs beyond
its own patterns is passed in by the caller. The only file reads are in scan_paths and load_digests. A Finding never
carries the matched text.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .errors import StateError


@dataclass(frozen=True)
class Finding:
    pattern: str
    path: str
    offset: int
    length: int                                    # never the matched text


SCAN_CLASSES = ("secret", "privacy")

PATTERNS: dict[str, re.Pattern[bytes]] = {         # class "secret"
    "job_token": re.compile(rb"mhfjob_[A-Za-z0-9_-]{43}"),
    "bearer": re.compile(rb"[Bb]earer\s+[A-Za-z0-9._~+/-]{20,}"),
    # 'authorization: <value>', or the JSON form '"Authorization": "[<scheme> ]<value>"' (quotes may be escaped)
    "authorization_header": re.compile(
        rb"(?i)authorization(?:\s*[:=]\s*\S{8,}|\\*[\"']\s*:\s*\\*[\"'](?:[a-z]+\s+)?[^\s\"'\\]{8,})"),
    "wandb_key": re.compile(rb"wandb_v1_[A-Za-z0-9_-]{20,}"),
    # name then separator then 40 hex; quotes may be escaped (a double-encoded JSON string) and a comma separates an argv
    # pair (["--api-key", "<40 hex>"])
    "keyed_hex40": re.compile(
        rb"(?i)(?<![a-z0-9])(?:api[_-]?key|password|passwd|secret|token)(?:\\*[\"'])?\s*[:=\s,]\s*(?:\\*[\"'])?"
        rb"(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])"),
    "anthropic_key": re.compile(rb"sk-ant-[A-Za-z0-9_-]{20,}"),
    "openrouter_key": re.compile(rb"sk-or-[A-Za-z0-9_-]{20,}"),
    "github_token": re.compile(rb"gh[pousr]_[A-Za-z0-9]{36}"),
    "jwt": re.compile(rb"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    "private_key_block": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "netrc_line": re.compile(rb"machine\s+\S+\s+login\s+\S+\s+password\s+\S+"),
}

PRIVACY_PATTERNS: dict[str, re.Pattern[bytes]] = {  # class "privacy"
    "local_path_users": re.compile(rb"/Users/[A-Za-z0-9._-]+/"),
    "local_path_home": re.compile(rb"/home/[A-Za-z0-9._-]+/"),
}

# every rule name a finding can carry, by class (11 + 3 + 2 = 16 rules; a receipt's scan.rules)
SECRET_RULES = tuple(PATTERNS) + ("known_value", "known_digest")
PRIVACY_RULES = tuple(PRIVACY_PATTERNS) + ("hostname",)
ALL_RULES = SECRET_RULES + PRIVACY_RULES

MIN_KNOWN_LEN = 8
MIN_HOSTNAME_LEN = 4
_DIGEST_RUN = re.compile(rb"[A-Za-z0-9_-]{20,}")
_DIGEST_RUN_WIDE = re.compile(rb"[A-Za-z0-9_.+/=-]{20,}")   # also keys with . + / = (base64, dotted tokens)
_WIDE_ONLY = re.compile(rb"[.+/=]")


def _classes(classes: Sequence[str]) -> frozenset[str]:
    if isinstance(classes, str):
        classes = (classes,)
    chosen = frozenset(classes)
    unknown = chosen - frozenset(SCAN_CLASSES)
    if unknown:
        raise ValueError(f"unknown scan class(es): {sorted(unknown)}")
    return chosen


def _as_bytes(value: bytes | str) -> bytes:
    return value.encode("utf-8") if isinstance(value, str) else bytes(value)


def _hostname_pattern(host: str) -> re.Pattern[bytes]:
    return re.compile(rb"(?i)(?<![A-Za-z0-9.-])" + re.escape(host.encode("utf-8")) + rb"(?![A-Za-z0-9.-])")


def _digest_findings(data: bytes, digests: Sequence[tuple[int, str]], path: str) -> list[Finding]:
    by_len: dict[int, set[str]] = {}
    for length, digest in digests:
        if isinstance(length, int) and length > 0 and isinstance(digest, str):
            by_len.setdefault(length, set()).add(digest.lower())
    if not by_len:
        return []
    out = []
    seen: set[tuple[int, int]] = set()
    sha = hashlib.sha256
    runs = list(_DIGEST_RUN.finditer(data))
    runs += [r for r in _DIGEST_RUN_WIDE.finditer(data) if _WIDE_ONLY.search(r.group())]   # else the same as a narrow run
    for run in runs:
        chunk = run.group()
        for length, wanted in by_len.items():
            for i in range(0, len(chunk) - length + 1):
                if (run.start() + i, length) not in seen and sha(chunk[i:i + length]).hexdigest() in wanted:
                    seen.add((run.start() + i, length))
                    out.append(Finding("known_digest", path, run.start() + i, length))
    return out


def scan_bytes(data: bytes, *, known: Sequence[bytes] = (), digests: Sequence[tuple[int, str]] = (), path: str = "",
               classes: Sequence[str] = ("secret",), hostnames: Sequence[str] = ()) -> list[Finding]:
    """Findings in one byte string, sorted by offset. Offsets and lengths are in bytes."""
    chosen = _classes(classes)
    data = bytes(data)
    if isinstance(known, (str, bytes, bytearray)):  # one value, not a sequence of its characters
        known = (known,)
    if isinstance(hostnames, str):
        hostnames = (hostnames,)
    out: list[Finding] = []
    if "secret" in chosen:
        for name, pat in PATTERNS.items():
            out.extend(Finding(name, path, m.start(), m.end() - m.start()) for m in pat.finditer(data))
        for value in known:
            needle = _as_bytes(value)
            if len(needle) < MIN_KNOWN_LEN:
                continue
            start = data.find(needle)
            while start >= 0:
                out.append(Finding("known_value", path, start, len(needle)))
                start = data.find(needle, start + 1)
        out.extend(_digest_findings(data, digests, path))
    if "privacy" in chosen:
        for name, pat in PRIVACY_PATTERNS.items():
            out.extend(Finding(name, path, m.start(), m.end() - m.start()) for m in pat.finditer(data))
        for host in hostnames:
            if isinstance(host, str) and len(host) >= MIN_HOSTNAME_LEN:
                out.extend(Finding("hostname", path, m.start(), m.end() - m.start())
                           for m in _hostname_pattern(host).finditer(data))
    out.sort(key=lambda f: (f.offset, f.pattern, f.length))
    return out


def _keyed_pair(key: str, value: object, path: str) -> list[Finding]:
    """keyed_hex40 across a dict entry: {"WANDB_API_KEY": "<40 hex>"} is the record form of 'WANDB_API_KEY: <40 hex>',
    which scanning the key and the value apart cannot see. The finding sits in the value (offset of the hex)."""
    if isinstance(value, str):
        value = value.encode("utf-8")
    elif not isinstance(value, (bytes, bytearray)):
        return []
    head = key.encode("utf-8") + b"="
    out = []
    for m in PATTERNS["keyed_hex40"].finditer(head + bytes(value)):
        if m.start() < len(head) - 1 and m.end() > len(head):      # the name is in the key, the hex in the value
            out.append(Finding("keyed_hex40", path, m.end() - 40 - len(head), 40))
    # likewise authorization_header across {"Authorization": "<value>"}, in its JSON form; the finding is the value part
    head = key.encode("utf-8") + b'": "'
    for m in PATTERNS["authorization_header"].finditer(head + bytes(value)):
        if m.start() < len(head) - 3 and m.end() > len(head):
            out.append(Finding("authorization_header", path, 0, m.end() - len(head)))
    return out


def scan_obj(value: object, *, known: Sequence[bytes] = (), digests: Sequence[tuple[int, str]] = (), path: str = "",
             classes: Sequence[str] = ("secret",), hostnames: Sequence[str] = ()) -> list[Finding]:
    """Walks dict (keys and values), list and tuple; scans every str (and bytes). A finding's path is
    '<path>/<key>' or '<path>[<index>]'; a finding in a dict key ends with '#key'."""
    chosen = _classes(classes)
    kw = dict(known=known, digests=digests, classes=classes, hostnames=hostnames)
    out: list[Finding] = []
    stack: list[tuple[str, object]] = [(path, value)]
    while stack:
        where, node = stack.pop()
        if isinstance(node, dict):
            for k in node:
                child = f"{where}/{k}"
                if isinstance(k, str):
                    out.extend(scan_bytes(k.encode("utf-8"), path=child + "#key", **kw))
                    if "secret" in chosen:
                        out.extend(_keyed_pair(k, node[k], child))
                stack.append((child, node[k]))
        elif isinstance(node, (list, tuple)):
            for i, item in enumerate(node):
                stack.append((f"{where}[{i}]", item))
                if "secret" in chosen and i and isinstance(node[i - 1], str):   # an argv pair: ["--api-key", "<hex>"]
                    out.extend(f for f in _keyed_pair(node[i - 1], item, f"{where}[{i}]") if f.pattern == "keyed_hex40")
        elif isinstance(node, str):
            out.extend(scan_bytes(node.encode("utf-8"), path=where, **kw))
        elif isinstance(node, (bytes, bytearray)):
            out.extend(scan_bytes(bytes(node), path=where, **kw))
    out.sort(key=lambda f: (f.path, f.offset, f.pattern))
    return out


def scan_paths(paths: Iterable[Path], *, known: Sequence[bytes] = (), digests: Sequence[tuple[int, str]] = (),
               classes: Sequence[str] = ("secret",), hostnames: Sequence[str] = ()) -> list[Finding]:
    """Scans each regular file; a directory is walked recursively (symlinks are neither followed nor read).
    A finding's path is the file's path as given (joined with the walked relative path)."""
    _classes(classes)
    files: list[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_symlink():
            continue
        if p.is_dir():
            for dirpath, dirnames, filenames in os.walk(p, followlinks=False):
                dirnames.sort()
                for name in sorted(filenames):
                    full = Path(dirpath) / name
                    if not full.is_symlink() and full.is_file():
                        files.append(full)
        elif p.is_file():
            files.append(p)
    out: list[Finding] = []
    for f in files:
        with open(f, "rb") as fh:
            data = fh.read()
        out.extend(scan_bytes(data, known=known, digests=digests, path=str(f), classes=classes, hostnames=hostnames))
    return out


def load_digests(path: Path) -> list[tuple[int, str]]:
    """(length, sha256) pairs from STATE/keydigests.json; [] when the file is absent. StateError when malformed."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return []
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise StateError(f"{Path(path).name}: not valid JSON") from None
    if not isinstance(doc, dict) or doc.get("schema") != "mhforge.keydigests/1" or not isinstance(doc.get("keys"), list):
        raise StateError(f"{Path(path).name}: not a mhforge.keydigests/1 file")
    out: list[tuple[int, str]] = []
    for entry in doc["keys"]:
        length = entry.get("len") if isinstance(entry, dict) else None
        digest = entry.get("sha256") if isinstance(entry, dict) else None
        if (not isinstance(length, int) or isinstance(length, bool) or length < 1 or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)):
            raise StateError(f"{Path(path).name}: malformed key digest entry")
        out.append((length, digest))
    return out


# ---------------------------------------------------------------- verbatim dataset text

@dataclass(frozen=True)
class VerbatimIndex:                               # dataset text to look for; built from data the caller passes in
    run_chars: int
    run_hashes: frozenset[bytes]                   # first 8 bytes of the SHA-256 of every run_chars-character window
    exact: tuple[str, ...]                         # whole strings looked for as they are (each at least 8 characters)


MIN_EXACT_CHARS = 8


def _window_hash(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()[:8]


def build_verbatim_index(run_texts: Iterable[str], exact_texts: Iterable[str] = (), *, run_chars: int = 40) -> VerbatimIndex:
    if not isinstance(run_chars, int) or isinstance(run_chars, bool) or run_chars < 1:
        raise ValueError("run_chars must be a positive integer")
    hashes: set[bytes] = set()
    for text in run_texts:
        if not isinstance(text, str):
            continue
        for i in range(0, len(text) - run_chars + 1):
            hashes.add(_window_hash(text[i:i + run_chars]))
    exact = sorted({t for t in exact_texts if isinstance(t, str) and len(t) >= MIN_EXACT_CHARS})
    return VerbatimIndex(run_chars=run_chars, run_hashes=frozenset(hashes), exact=tuple(exact))


def scan_verbatim(text: str, index: VerbatimIndex, *, path: str = "") -> list[Finding]:
    """'dataset_text_run': a window of the text is in run_hashes (overlapping hit windows are merged into one finding);
    'dataset_text_exact': an exact string occurs in the text. Offsets and lengths are in characters."""
    out: list[Finding] = []
    n = index.run_chars
    if index.run_hashes and len(text) >= n:
        span_start = span_end = -1
        for i in range(0, len(text) - n + 1):
            if _window_hash(text[i:i + n]) in index.run_hashes:
                if span_start >= 0 and i <= span_end:
                    span_end = i + n
                else:
                    if span_start >= 0:
                        out.append(Finding("dataset_text_run", path, span_start, span_end - span_start))
                    span_start, span_end = i, i + n
        if span_start >= 0:
            out.append(Finding("dataset_text_run", path, span_start, span_end - span_start))
    for needle in index.exact:
        start = text.find(needle)
        while start >= 0:
            out.append(Finding("dataset_text_exact", path, start, len(needle)))
            start = text.find(needle, start + 1)
    out.sort(key=lambda f: (f.offset, f.pattern))
    return out
