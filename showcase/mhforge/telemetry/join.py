# SPDX-License-Identifier: MIT
"""Sidecar rows + gateway call records -> example rows and call roles (spec 3.21). Pure: never reads a file, never
mutates a record.

The sidecar (2.8) is written inside the worker, next to candidate code, so every row is treated as untrusted: a row of
the wrong shape is listed in `problems` and left out, never trusted for a join.
"""

from __future__ import annotations

import re
from typing import Mapping, Sequence

from .. import util
from ..errors import StateError

__all__ = ["SUCCESS_OUTCOMES", "parse_sidecar", "join_examples", "call_roles", "join_problems", "check_examples"]

SIDECAR_SCHEMA = "mhforge.sidecar/1"
EXAMPLE_SCHEMA = "mhforge.example/1"
SUCCESS_OUTCOMES = ("completed", "replayed", "coalesced")
ROLE_BY_SPLIT = {"val": "validate", "test": "test"}
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_SCHEMA_RE = re.compile(r"mhforge\.sidecar/([0-9]+)")


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _hash_or_none(v: object) -> bool:
    return v is None or (isinstance(v, str) and bool(_HEX64_RE.fullmatch(v)))


# ---------------------------------------------------------------- sidecar

def parse_sidecar(rows: Sequence[Mapping]) -> dict:
    """{"meta": row | None, "splits": row | None, "calls": {seq: row}, "evals": [row, ...], "done": row | None,
    "problems": [str]}. evals are in seq order. StateError on an unknown major version of the sidecar schema."""
    out: dict = {"meta": None, "splits": None, "calls": {}, "evals": [], "done": None, "problems": []}
    problems = out["problems"]
    seen: set[int] = set()
    for n, row in enumerate(rows):
        if not isinstance(row, Mapping) or not _is_int(row.get("seq")):
            problems.append(f"sidecar_bad_row:{n}")
            continue
        seq, kind = row["seq"], row.get("kind")
        if seq in seen:
            problems.append(f"sidecar_duplicate_seq:{seq}")
            continue
        seen.add(seq)
        if kind == "meta":
            m = _SCHEMA_RE.fullmatch(str(row.get("schema", "")))
            if not m:
                problems.append(f"sidecar_bad_schema:{seq}")
            elif m.group(1) != "1":
                raise StateError(f"sidecar schema version {m.group(1)} is not supported")
            if out["meta"] is None:
                out["meta"] = row
            else:
                problems.append(f"sidecar_duplicate_meta:{seq}")
        elif kind in ("splits", "done"):
            if out[kind] is None:
                out[kind] = row
            else:
                problems.append(f"sidecar_duplicate_{kind}:{seq}")
        elif kind == "call":
            if (not _hash_or_none(row.get("user_sha256")) or row.get("cache_hit") not in (True, False, None)
                    or not (row.get("error") is None or isinstance(row.get("error"), str))):
                problems.append(f"sidecar_bad_call:{seq}")
                continue
            out["calls"][seq] = row
        elif kind == "eval":
            seqs, batch = row.get("call_seqs", []), row.get("batch_user_sha256", [])
            if (row.get("split") is not None and not isinstance(row.get("split"), str)) \
                    or not (row.get("index") is None or _is_int(row.get("index"))) or not _is_int(row.get("pass")) \
                    or not isinstance(seqs, list) or not all(_is_int(s) for s in seqs) \
                    or not isinstance(batch, list) or not all(_hash_or_none(h) and h for h in batch) \
                    or not isinstance(row.get("was_correct"), bool) \
                    or not all(isinstance(row.get(k), str) for k in ("prediction", "target")) \
                    or not all(_hash_or_none(row.get(k)) for k in ("example_sha256", "question_sha256",
                                                                   "prediction_sha256", "target_sha256")) \
                    or not isinstance(row.get("metrics", {}), Mapping):
                problems.append(f"sidecar_bad_eval:{seq}")
                continue
            out["evals"].append(row)
        else:
            problems.append(f"sidecar_unknown_kind:{seq}")
    out["evals"].sort(key=lambda r: r["seq"])
    if seen and out["done"] is None:
        problems.append("no_done_row")                   # a killed worker leaves a prefix without its done row
    return out


def _has_sidecar(sidecar: Mapping | None) -> bool:
    if not sidecar:
        return False
    return any(sidecar.get(k) for k in ("meta", "splits", "calls", "evals", "done"))


# ---------------------------------------------------------------- the join

def _received_key(record: Mapping) -> tuple:
    text = record.get("received_utc")
    try:
        t = util.parse_utc(text)
    except ValueError:
        t = float("inf")
    return (t, str(record.get("call_id", "")))


def _admission_only(record: Mapping) -> bool:
    return not record.get("outcome") or record.get("ended_utc") is None or record.get("reply") is None


class _Join:
    """One pass over a sidecar and the call records of one job attempt."""

    def __init__(self, sidecar: Mapping, call_records: Sequence[Mapping], split: str) -> None:
        self.split = split
        self.records = sorted((r for r in call_records if isinstance(r, Mapping) and r.get("call_id")),
                              key=_received_key)
        self.problems: list[str] = []
        self.success: dict[str, list[Mapping]] = {}      # user_sha256 -> success records in received order
        self.other: dict[str, list[Mapping]] = {}        # user_sha256 -> refused / rejected / failed records
        self.admission: list[Mapping] = []
        for r in self.records:
            if _admission_only(r):
                self.admission.append(r)
            elif r.get("outcome") in SUCCESS_OUTCOMES:
                self.success.setdefault(r.get("user_sha256"), []).append(r)
            else:
                self.other.setdefault(r.get("user_sha256"), []).append(r)
        self.calls: dict[int, Mapping] = dict(sidecar.get("calls") or {})
        self.evals: list[Mapping] = list(sidecar.get("evals") or [])
        self.row_records: dict[int, list[Mapping]] = {}  # call seq -> records it reached
        self.claimed_by: dict[str, int] = {}             # call_id -> seq of the row that claimed it
        self._claim()
        self.examples = self._examples()
        self._roles()

    def _claim(self) -> None:
        used: set[str] = set()
        first_taken: dict[str, Mapping] = {}
        last_error_row: dict[str, int] = {}

        def take(pool: dict[str, list[Mapping]], h: str) -> Mapping | None:
            for r in pool.get(h, ()):
                if r["call_id"] not in used:
                    used.add(r["call_id"])
                    return r
            return None

        for seq in sorted(self.calls):
            row = self.calls[seq]
            h = row.get("user_sha256")
            got: list[Mapping] = []
            if h is None:
                pass
            elif row.get("error") is not None:
                r = take(self.other, h)
                if r is not None:
                    got.append(r)
                last_error_row[h] = seq
            elif row.get("cache_hit") is True:
                if h in first_taken:
                    got.append(first_taken[h])
            else:
                r = take(self.success, h)
                if r is not None:
                    got.append(r)
                    first_taken.setdefault(h, r)
                elif h in first_taken:                   # cache_hit unknown: the reply came from the worker's cache
                    got.append(first_taken[h])
                elif row.get("cache_hit") is False:
                    self.problems.append(f"miss_without_record:{seq}")
            self.row_records[seq] = got
            for r in got:
                self.claimed_by.setdefault(r["call_id"], seq)
        for h, records in self.other.items():           # gateway retries behind one failed call: its last error row
            for r in records:
                if r["call_id"] not in used and h in last_error_row:
                    used.add(r["call_id"])
                    self.row_records[last_error_row[h]].append(r)
                    self.claimed_by.setdefault(r["call_id"], last_error_row[h])
        for r in self.records:
            if r["call_id"] not in used and not _admission_only(r):
                self.problems.append(f"unclaimed_call:{r['call_id']}")
        for r in self.admission:
            self.problems.append(f"admission_only:{r['call_id']}")

    def _examples(self) -> list[dict]:
        in_seqs = {s for ev in self.evals for s in ev.get("call_seqs", [])}
        by_hash: dict[str, list[int]] = {}
        for seq, row in self.calls.items():
            if seq not in in_seqs and row.get("user_sha256"):
                by_hash.setdefault(row["user_sha256"], []).append(seq)
        groups: dict[int, list[Mapping]] = {}
        for ev in self.evals:
            if ev.get("split") == self.split and _is_int(ev.get("index")):
                groups.setdefault(ev["index"], []).append(ev)
        self.first_wanted = next((ev for ev in self.evals if ev.get("split") == self.split), None)
        out = []
        for index in sorted(groups):
            passes = groups[index]
            keep = max(passes, key=lambda e: (e["pass"], e["seq"]))
            seqs: list[int] = []
            for ev in sorted(passes, key=lambda e: e["seq"]):
                seqs.extend(s for s in ev.get("call_seqs", []) if s not in seqs)
                for h in ev.get("batch_user_sha256", []):
                    seqs.extend(s for s in sorted(by_hash.get(h, ())) if s not in seqs)
            reached: list[Mapping] = []
            for s in seqs:
                for r in self.row_records.get(s, ()):
                    if all(r["call_id"] != x["call_id"] for x in reached):
                        reached.append(r)
            ambiguous = any(bool(e.get("ambiguous")) for e in passes)
            matched = bool(reached) and not ambiguous
            out.append({"index": index, "keep": keep, "passes": passes, "reached": reached,
                        "ambiguous": ambiguous, "matched": matched})
        return out

    def _roles(self) -> None:
        role = ROLE_BY_SPLIT.get(self.split, "other")
        self.roles: dict[str, tuple[str, str | None]] = {}
        for ex in self.examples:
            eid = _example_id(ex["index"], ex["keep"].get("example_sha256"))
            for r in ex["reached"]:
                self.roles.setdefault(r["call_id"], (role, None if ex["ambiguous"] else eid))
        first = self.first_wanted
        first_t = first.get("t") if first is not None else None
        for r in self.records:
            cid = r["call_id"]
            if cid in self.roles:
                continue
            if _admission_only(r):
                self.roles[cid] = ("other", None)
            elif cid in self.claimed_by:
                early = first is None or self.claimed_by[cid] < first["seq"]
                self.roles[cid] = ("learn" if early else "other", None)
            else:
                t = _received_key(r)[0]
                early = isinstance(first_t, (int, float)) and not isinstance(first_t, bool) and t < first_t
                self.roles[cid] = ("learn" if early else "other", None)


def _example_id(index: int, example_sha256: object) -> str:
    return "%04d-%s" % (index, str(example_sha256 or "")[:12])


def _latency(record: Mapping) -> float | None:
    try:
        return round(util.parse_utc(record["ended_utc"]) - util.parse_utc(record["received_utc"]), 6)
    except (KeyError, TypeError, ValueError):
        return None


def _call_entry(record: Mapping) -> dict:
    usage = record.get("usage") if isinstance(record.get("usage"), Mapping) else {}
    flags = record.get("flags") if isinstance(record.get("flags"), Mapping) else {}
    pt, ct = usage.get("prompt_tokens"), usage.get("completion_tokens")
    return {"user_sha256": record.get("user_sha256"), "call_id": record.get("call_id"), "outcome": record.get("outcome"),
            "input_tokens": pt if _is_int(pt) else None, "output_tokens": ct if _is_int(ct) else None,
            "latency_s": _latency(record), "finish_reason": record.get("finish_reason"),
            "truncated_empty": bool(flags.get("truncated_empty", False))}


def join_examples(sidecar: Mapping, call_records: Sequence[Mapping], *, dataset: str, split: str) -> list[dict]:
    """Rows of 2.12 for every evaluated example of `split`, in index order. [] without a sidecar."""
    if not _has_sidecar(sidecar):
        return []
    j = _Join(sidecar, call_records, split)
    rows = []
    for ex in j.examples:
        keep = ex["keep"]
        calls = [_call_entry(r) for r in ex["reached"]] if ex["matched"] else []
        rows.append({
            "schema": EXAMPLE_SCHEMA, "dataset": dataset, "split": split, "index": ex["index"],
            "example_id": _example_id(ex["index"], keep.get("example_sha256")),
            "example_sha256": keep.get("example_sha256"), "question_sha256": keep.get("question_sha256"),
            "prediction": keep.get("prediction"), "prediction_sha256": keep.get("prediction_sha256"),
            "target": keep.get("target"), "target_sha256": keep.get("target_sha256"),
            "was_correct": bool(keep.get("was_correct")),
            "metrics": dict(keep.get("metrics")) if isinstance(keep.get("metrics"), Mapping) else {},
            "passes": max(e["pass"] for e in ex["passes"]), "ambiguous": ex["ambiguous"], "matched": ex["matched"],
            "calls": calls, "n_calls": len(calls),
            "input_tokens": sum(c["input_tokens"] or 0 for c in calls),
            "output_tokens": sum(c["output_tokens"] or 0 for c in calls),
            "latency_s": round(sum(c["latency_s"] or 0.0 for c in calls), 6),
            "finish_reason": calls[-1]["finish_reason"] if calls else None,
            "truncated_empty": any(c["truncated_empty"] for c in calls),
        })
    return rows


def call_roles(sidecar: Mapping, call_records: Sequence[Mapping], *, split: str) -> dict[str, tuple[str, str | None]]:
    """call_id -> (role, example_id); role is 'learn' | 'validate' | 'test' | 'other'. Without a sidecar every role is
    'other'."""
    if not _has_sidecar(sidecar):
        return {r["call_id"]: ("other", None) for r in call_records if isinstance(r, Mapping) and r.get("call_id")}
    return dict(_Join(sidecar, call_records, split).roles)


def join_problems(sidecar: Mapping, call_records: Sequence[Mapping], *, split: str) -> list[str]:
    """The problems of 3.21 step 5 (plus the sidecar's own): unclaimed records, misses without a record, a missing
    `done` row, admission-only records."""
    if not _has_sidecar(sidecar):
        return ["no_sidecar"]
    j = _Join(sidecar, call_records, split)
    return list(sidecar.get("problems") or []) + j.problems


def check_examples(rows: Sequence[Mapping], result: Mapping) -> list[str]:
    """Count and correct of the example rows must equal the result file's `total` and `correct`."""
    problems = []
    total, correct = (result.get("total"), result.get("correct")) if isinstance(result, Mapping) else (None, None)
    n, k = len(rows), sum(1 for r in rows if r.get("was_correct") is True)
    if not _is_int(total):
        problems.append("result_total_missing")
    elif n != total:
        problems.append(f"count {n} != result total {total}")
    if not _is_int(correct):
        problems.append("result_correct_missing")
    elif k != correct:
        problems.append(f"correct {k} != result correct {correct}")
    return problems
