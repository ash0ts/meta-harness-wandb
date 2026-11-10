# SPDX-License-Identifier: Apache-2.0
# Adapted from ash0ts/harness2-wandb@31b0f82 harness2/showcase/runtime_v4.py (L42-L111, the operation-journal pattern of
# StudyRuntime.run_agent). Changes: one append-only JSONL file with a strictly increasing seq instead of one rewritten JSON
# file per operation; numbered attempts that are never reused; recover() closes every started-without-terminal attempt
# with "interrupted" (an interrupted job is relaunched, because the gateway replays finished calls) instead of refusing to
# continue; executor handles for orphan cleanup; candidate-budget counting with operator resets.
"""Append-only operation journal (spec 2.7, 3.16). Standard library only.

One writer (the runner); one ``os.write`` of whole lines per record on an ``O_APPEND`` descriptor, ``fsync`` per line.
The file is never rewritten or truncated. A torn last line (a crash in the middle of a write) is ignored by readers; when
the writer appends after one it first terminates the torn bytes with a newline followed by a ``("journal", "torn_tail",
"terminated")`` marker that names the torn line's length and hash, so :func:`read_journal` can tell that line from
corruption. Other modules read the journal with :func:`read_journal`, or with ``util.read_marked_jsonl`` (the same
parsing without the entry checks) when they may not import this module; ``util.read_jsonl`` would refuse such a file.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Mapping

from . import util
from .errors import StateError

__all__ = ["Journal", "read_journal", "SCHEMA", "NON_TERMINAL", "MAX_DETAIL_BYTES"]

SCHEMA = "mhforge.journal/1"
NON_TERMINAL = frozenset({"started", "handle"})     # "handle": an executor handle reported after "started" (on_handle)
MAX_DETAIL_BYTES = 65_536                            # detail holds hashes, counters and short text, never payloads
HANDLE_OPS = ("job", "import_check")
_MARKER = util.JOURNAL_TORN_MARKER


def _check_entry(row: object, number: int, previous_seq: int) -> dict:
    if not isinstance(row, dict):
        raise StateError(f"journal line {number} is not an object")
    schema = row.get("schema")
    if not isinstance(schema, str) or not schema.startswith("mhforge.journal/"):
        raise StateError(f"journal line {number}: not a mhforge.journal record")
    if schema != SCHEMA:
        raise StateError(f"journal line {number}: unknown schema version {schema!r}")
    seq = row.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq <= previous_seq:
        raise StateError(f"journal line {number}: seq does not increase")
    for name in ("op", "key", "status"):
        if not isinstance(row.get(name), str):
            raise StateError(f"journal line {number}: {name} is not a string")
    attempt = row.get("attempt", 0)
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
        raise StateError(f"journal line {number}: bad attempt")
    if not isinstance(row.get("detail", {}), dict):
        raise StateError(f"journal line {number}: detail is not an object")
    return row


def _parse(data: bytes) -> tuple[list[dict], bytes | None]:
    """(entries, torn region bytes or None). The torn region is the run of bad lines after the last valid entry (a crash
    can also tear the write that terminates an earlier torn tail, leaving two bad lines or a bare newline); a run of bad
    lines elsewhere must be followed by the marker that names exactly those bytes, else StateError. The parsing is
    util.parse_marked_jsonl (shared with readers that may not import this module); this adds the 2.7 entry checks."""
    previous = [0]

    def check(row: dict, number: int) -> dict:
        entry = _check_entry(row, number, previous[0])
        previous[0] = entry["seq"]
        return entry

    return util.parse_marked_jsonl(data, check, name="journal")


def read_journal(path: Path) -> list[dict]:
    """Entries of a journal file ([] when absent). A torn last line is ignored; a bad line elsewhere is StateError."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return []
    return _parse(data)[0]


def _copy_detail(detail: Mapping | None) -> dict:
    if detail is None:
        return {}
    if not isinstance(detail, Mapping):
        raise TypeError("journal detail must be a mapping")
    try:
        encoded = util.canonical_json(dict(detail))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"journal detail is not plain JSON ({type(exc).__name__})") from None
    if len(encoded) > MAX_DETAIL_BYTES:
        raise ValueError(f"journal detail larger than {MAX_DETAIL_BYTES} bytes: the journal stores no payloads")
    return json.loads(encoded.decode("utf-8"))


class Journal:
    """The operation journal of 2.7. Methods are thread-safe within one process."""

    def __init__(self, path: Path, *, clock: util.Clock | None = None) -> None:
        self.path = Path(path)
        self.clock = clock or util.SystemClock()
        self._lock = threading.RLock()
        try:
            with open(self.path, "rb") as fh:
                data = fh.read()
        except FileNotFoundError:
            data = b""
        self._entries, self._torn = _parse(data)
        self._needs_newline = bool(data) and not data.endswith(b"\n")
        self._seq = self._entries[-1]["seq"] if self._entries else 0
        self._own_from = self._seq + 1                       # entries with seq >= this were written by this instance

    # ------------------------------------------------------------ writing

    def _entry(self, op: str, key: str, status: str, attempt: int, detail: dict) -> dict:
        self._seq += 1
        return {"schema": SCHEMA, "seq": self._seq, "utc": util.utc_now(self.clock), "pid": os.getpid(),
                "op": op, "key": key, "status": status, "attempt": attempt, "detail": detail}

    def _append(self, data: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            written = os.write(fd, data)
            if written != len(data):
                raise StateError(f"journal: short write ({written} of {len(data)} bytes)")
            os.fsync(fd)
        finally:
            os.close(fd)

    def record(self, op: str, key: str, status: str, *, attempt: int = 0, detail: Mapping | None = None) -> dict:
        for name, value in (("op", op), ("key", key), ("status", status)):
            if not isinstance(value, str) or not value or len(value) > 256 or "\n" in value:
                raise ValueError(f"journal {name} must be a short non-empty string")
        if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 0:
            raise ValueError("journal attempt must be a non-negative integer")
        body = _copy_detail(detail)
        with self._lock:
            prefix = b""
            pending: list[dict] = []
            if self._needs_newline:
                prefix = b"\n"
            if self._torn is not None:
                marker = self._entry(*_MARKER, 0, {"bytes": len(self._torn),
                                                   "sha256": hashlib.sha256(self._torn).hexdigest()})
                prefix += self._line(marker)
                pending.append(marker)
            entry = self._entry(op, key, status, attempt, body)
            try:
                self._append(prefix + self._line(entry))
            except BaseException:
                self._seq -= 1 + len(pending)
                raise
            self._torn = None
            self._needs_newline = False
            self._entries.extend(pending)
            self._entries.append(entry)
            return json.loads(json.dumps(entry))

    @staticmethod
    def _line(entry: dict) -> bytes:
        return (json.dumps(entry, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")

    def add_handle(self, op: str, key: str, attempt: int, handle: Mapping) -> dict:
        """Journal an executor handle for a started attempt (what on_handle reports): status "handle", detail.handle."""
        return self.record(op, key, "handle", attempt=attempt, detail={"handle": dict(handle)})

    # ------------------------------------------------------------ pairing

    def _instances(self) -> list[dict]:
        """Every started instance in order: {"op","key","attempt","started","entries":[...],"terminal": entry|None}."""
        instances: list[dict] = []
        open_by_triple: dict[tuple, list[dict]] = {}
        by_seq: dict[int, dict] = {}
        for entry in self._entries:
            triple = (entry["op"], entry["key"], entry.get("attempt", 0))
            status = entry["status"]
            if status == "started":
                inst = {"op": triple[0], "key": triple[1], "attempt": triple[2], "started": entry,
                        "entries": [entry], "terminal": None}
                instances.append(inst)
                open_by_triple.setdefault(triple, []).append(inst)
                by_seq[entry["seq"]] = inst
                continue
            stack = open_by_triple.get(triple) or []
            target = None
            named = entry.get("detail", {}).get("started_seq") if status not in NON_TERMINAL else None
            if isinstance(named, int) and not isinstance(named, bool) and named in by_seq:
                cand = by_seq[named]
                if cand["terminal"] is None and cand in stack:
                    target = cand
            if target is None and stack:
                target = stack[-1]
            if target is None:
                continue
            target["entries"].append(entry)
            if status not in NON_TERMINAL:
                target["terminal"] = entry
                stack.remove(target)
        return instances

    # ------------------------------------------------------------ recovery

    def recover(self) -> list[dict]:
        """Append "interrupted" for every started attempt with no terminal entry that an earlier process wrote.

        Idempotent; returns the entries it appended. Attempts this instance started itself are live and left alone.
        """
        with self._lock:
            appended = []
            for inst in self._instances():
                if inst["terminal"] is None and inst["started"]["seq"] < self._own_from:
                    appended.append(self.record(inst["op"], inst["key"], "interrupted", attempt=inst["attempt"],
                                                detail={"started_seq": inst["started"]["seq"], "outcome": "interrupted",
                                                        "infra": True, "failure_class": None}))
            return appended

    # ------------------------------------------------------------ queries

    def last(self, op: str, key: str, *, attempt: int | None = None) -> dict | None:
        with self._lock:
            for entry in reversed(self._entries):
                if entry["status"] == "handle":                  # internal row of add_handle, not a 2.7 status
                    continue
                if entry["op"] == op and entry["key"] == key and (attempt is None or entry.get("attempt", 0) == attempt):
                    return json.loads(json.dumps(entry))
        return None

    def attempts(self, op: str, key: str) -> list[dict]:
        """One summary per attempt number that reached "started", in the order of its first start:
        {"attempt", "seq" (of the first start), "status" and "detail" (of the latest entry), "started_detail"}."""
        with self._lock:
            summaries: dict[int, dict] = {}
            for entry in self._entries:
                if entry["op"] != op or entry["key"] != key:
                    continue
                n = entry.get("attempt", 0)
                if entry["status"] == "started" and n not in summaries:
                    summaries[n] = {"attempt": n, "seq": entry["seq"], "status": "started",
                                    "detail": entry.get("detail", {}), "started_detail": entry.get("detail", {})}
                elif n in summaries and entry["status"] != "handle":
                    summaries[n]["status"] = entry["status"]
                    summaries[n]["detail"] = entry.get("detail", {})
            return json.loads(json.dumps(list(summaries.values())))

    def counted_attempts(self, job_key: str, candidate_sha256: str) -> list[dict]:
        """Job attempts started after the latest ("job", job_key, "reset") entry whose started detail carries this
        candidate_sha256: the attempts that count against the candidate budget (3.18)."""
        with self._lock:
            reset_seq = 0
            for entry in self._entries:
                if entry["op"] == "job" and entry["key"] == job_key and entry["status"] == "reset":
                    reset_seq = entry["seq"]
            return [a for a in self.attempts("job", job_key)
                    if a["seq"] > reset_seq and a["started_detail"].get("candidate_sha256") == candidate_sha256]

    def next_attempt(self, op: str, key: str) -> int:
        """1 + the highest attempt number any entry of (op, key) carries; an attempt number is never reused."""
        with self._lock:
            highest = 0
            for entry in self._entries:
                if entry["op"] == op and entry["key"] == key:
                    highest = max(highest, entry.get("attempt", 0))
            return highest + 1

    def entries(self, op: str | None = None, *, since_seq: int = 0) -> list[dict]:
        """Entries with seq > since_seq (all by default), optionally of one op, in order."""
        with self._lock:
            rows = [e for e in self._entries if e["seq"] > since_seq and (op is None or e["op"] == op)]
            return json.loads(json.dumps(rows))

    def session_start_seq(self) -> int:
        with self._lock:
            for entry in reversed(self._entries):
                if entry["op"] == "run" and entry["status"] == "started":
                    return entry["seq"]
        return 0

    def open_handles(self) -> list[dict]:
        """detail.handle of every job and import_check attempt that was started and has no terminal entry, in order."""
        with self._lock:
            handles: list[dict] = []
            for inst in self._instances():
                if inst["op"] not in HANDLE_OPS or inst["terminal"] is not None:
                    continue
                for entry in inst["entries"]:
                    handle = entry.get("detail", {}).get("handle")
                    if isinstance(handle, dict) and handle and handle not in handles:
                        handles.append(handle)
            return json.loads(json.dumps(handles))
