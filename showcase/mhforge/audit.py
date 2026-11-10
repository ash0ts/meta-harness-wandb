# SPDX-License-Identifier: Apache-2.0
# Adapted from ash0ts/harness2-wandb@31b0f82 harness2/showcase/audit.py (audit, L46-L115). Changes: read-only over the
# SQLite ledger of 2.10 (no write, no file created); totals recomputed in SQL; ledger rows against call records both ways;
# raw stream files, replay sources, per-job-attempt counters, result files against the joined examples, tainted refusal
# codes, reply statuses, a secret and key-digest scan of state records and stored-span counts of verified receipts; every
# finding is a problem string instead of an assertion; informational lists (reconciled, limit_exceeded, repeatable
# failures, admission-only records, open meter rows) that never fail the audit.
"""Ledger versus call records versus receipts (spec 3.22). Read-only: it opens the ledger with util.open_sqlite_ro,
writes nothing and creates no file."""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Mapping

from . import ledger as ledger_mod
from . import scan, util, wire
from .bundle import result_is_usable
from .errors import StateError
from .executors import TAINT_CODES, positive_codes
from .paths import StatePaths
from .telemetry.join import check_examples, join_examples, parse_sidecar

__all__ = ["audit", "REPLY_STATUSES", "OUTCOMES", "SCAN_SKIP_DIRS"]

REPLY_STATUSES = (200, 402, 403)
OUTCOMES = ("completed", "replayed", "coalesced", "refused", "provider_rejected", "failed")
SCAN_SKIP_DIRS = frozenset({"box", "cache"})       # a job attempt's extracted work tree and worker cache are not records
_SUMS = "COALESCE(SUM(CASE WHEN status != 'rejected' THEN COALESCE(spent_micro, reserved_micro) ELSE 0 END), 0)"


def _admission_only(rec: Mapping) -> bool:
    return not rec.get("outcome") or rec.get("ended_utc") is None or rec.get("reply") is None


def _read(path: Path, problems: list[str], what: str) -> dict | None:
    try:
        doc = util.read_json(path)
    except (OSError, StateError) as exc:
        problems.append(f"{what}: unreadable ({type(exc).__name__})")
        return None
    if not isinstance(doc, dict):
        problems.append(f"{what}: not a JSON object")
        return None
    return doc


def _records(paths: StatePaths, problems: list[str]) -> list[tuple[Path, dict]]:
    out = []
    if not paths.calls_root.is_dir():
        return out
    for path in sorted(paths.calls_root.glob("*/*.json")):
        rec = _read(path, problems, f"call record {path.parent.name}/{path.name}")
        if rec is not None:
            out.append((path, rec))
    return out


def _ledger(paths: StatePaths, problems: list[str]) -> dict:
    view = {"rows": [], "phase_totals": {}, "phase_sums": {}, "job_counters": {}, "job_sums": {}, "open_meters": []}
    if not paths.ledger.exists():
        return view
    try:
        conn, stale = util.open_sqlite_ro(paths.ledger)
    except (OSError, sqlite3.Error) as exc:
        problems.append(f"ledger: unreadable ({type(exc).__name__})")
        return view
    try:
        conn.row_factory = sqlite3.Row
        view["rows"] = [dict(r) for r in conn.execute("SELECT * FROM calls ORDER BY created_utc, id")]
        view["phase_totals"] = {r["phase"]: int(r["committed_micro"]) for r in conn.execute("SELECT * FROM phase_totals")}
        view["phase_sums"] = {r[0]: int(r[1]) for r in conn.execute(f"SELECT phase, {_SUMS} FROM calls GROUP BY phase")}
        view["job_counters"] = {r["job_key"]: (int(r["committed_micro"]), int(r["charged_attempts"]))
                                for r in conn.execute("SELECT * FROM job_counters")}
        view["job_sums"] = {r[0]: (int(r[1]), int(r[2] or 0)) for r in conn.execute(
            f"SELECT job_key, {_SUMS}, SUM(status != 'rejected') FROM calls GROUP BY job_key")}
        view["open_meters"] = [f"{r['meter']}:{r['key']}" for r in conn.execute(
            "SELECT meter, key FROM meter_events WHERE status = 'open' ORDER BY meter, key")]
        view["read"] = True
    except sqlite3.Error as exc:
        problems.append(f"ledger: query failed ({type(exc).__name__})")
    finally:
        conn.close()
    if stale:
        problems.append("ledger: possibly stale view (a write-ahead log without its index); audit after the runner ends")
    return view


def _check_totals(view: dict, problems: list[str]) -> None:
    phases, sums = view["phase_totals"], view["phase_sums"]
    if view.get("read"):                               # as Ledger.verify_totals: a missing row would switch a cap off
        problems.extend(f"phase_totals row missing: {p}" for p in wire.LEDGER_PHASES if p not in phases)
    for phase in sorted(set(phases) | set(sums)):
        if phases.get(phase, 0) != sums.get(phase, 0):
            problems.append(f"phase {phase}: phase_totals {phases.get(phase, 0)} != sum of calls {sums.get(phase, 0)}")
    jobs, per_job = view["job_counters"], view["job_sums"]
    for key in sorted(set(jobs) | set(per_job)):
        committed, charged = per_job.get(key, (0, 0))
        if key not in jobs:
            problems.append(f"job {key}: calls without a job_counters row")
        elif jobs[key] != (committed, charged):
            problems.append(f"job {key}: job_counters {jobs[key]} != sum of calls {(committed, charged)}")


def _check_rows(manifest: dict, view: dict, records: list[tuple[Path, dict]], problems: list[str],
                listed: dict) -> None:
    prices = ledger_mod.prices_from_manifest(manifest)
    attempts: dict[str, tuple[str, Path, Mapping]] = {}   # ledger_id -> (call_id, record path, attempt)
    for path, rec in records:
        for att in rec.get("attempts") or []:
            if isinstance(att, Mapping) and att.get("ledger_id"):
                attempts[str(att["ledger_id"])] = (str(rec.get("call_id")), path, att)
    rows = {r["id"]: r for r in view["rows"]}
    for lid, (cid, path, att) in sorted(attempts.items()):
        row = rows.get(lid)
        if row is None:
            problems.append(f"call {cid} attempt {att.get('n')}: ledger row {lid} missing")
        elif row["call_id"] != cid:
            problems.append(f"ledger row {lid}: call_id {row['call_id']} != call record {cid}")
        sse = att.get("sse_file")
        if not isinstance(sse, str) or not sse or os.path.basename(sse) != sse or not (path.parent / sse).is_file():
            problems.append(f"call {cid} attempt {att.get('n')}: raw stream file missing")
    for lid, row in rows.items():
        if lid not in attempts:
            problems.append(f"ledger row {lid} (call {row['call_id']}): no attempt in a call record")
        status = row["status"]
        if status == "pending":
            problems.append(f"ledger row {lid}: pending (a stop should have drained it)")
        if status == "reconciled":
            listed["reconciled"].append(lid)
        if (row["reason"] or "").startswith("limit_exceeded"):
            listed["limit_exceeded"].append(lid)
        if status == "complete":
            try:
                usage = json.loads(row["usage_json"])
                cost = ledger_mod.cost_micro(prices, usage)
            except (TypeError, ValueError, KeyError, AttributeError, ArithmeticError):
                problems.append(f"ledger row {lid}: complete without usable usage_json")
                continue
            if cost != row["spent_micro"]:
                problems.append(f"ledger row {lid}: spent {row['spent_micro']} != cost {cost} recomputed from usage")


def _check_records(view: dict, records: list[tuple[Path, dict]], problems: list[str], listed: dict) -> None:
    by_id = {str(rec.get("call_id")): rec for _, rec in records}
    with_rows = Counter(r["call_id"] for r in view["rows"])
    for path, rec in records:
        cid = str(rec.get("call_id"))
        if _admission_only(rec):
            listed["admission_only"].append(cid)
        else:
            status = (rec.get("reply") or {}).get("status") if isinstance(rec.get("reply"), Mapping) else None
            if status not in REPLY_STATUSES:
                problems.append(f"call {cid}: reply status {status} is not 200, 402 or 403")
        refusal = rec.get("refusal") if isinstance(rec.get("refusal"), Mapping) else {}
        if refusal.get("detail") == "repeatable_provider_failure":
            listed["repeatable_provider_failure"].append(cid)
        if rec.get("outcome") in ("replayed", "coalesced"):
            replay = rec.get("replay") if isinstance(rec.get("replay"), Mapping) else {}
            src = by_id.get(str(replay.get("source_call_id")))
            if src is None or src.get("outcome") != "completed" or not src.get("attempts"):
                problems.append(f"call {cid}: {rec['outcome']} from {replay.get('source_call_id')}, which is not a "
                                "recorded paying call")
            if rec.get("attempts") or with_rows.get(cid):
                problems.append(f"call {cid}: {rec['outcome']} but has a provider attempt or a ledger row")


def _check_jobs(paths: StatePaths, records: list[tuple[Path, dict]], problems: list[str]) -> None:
    by_job: dict[str, list[dict]] = {}
    for path, rec in records:
        by_job.setdefault(path.parent.name, []).append(rec)
    if not paths.jobs_root.is_dir():
        return
    for job_file in sorted(paths.jobs_root.glob("*/job.json")):
        job = _read(job_file, problems, f"job record {job_file.parent.name}")
        if job is None:
            continue
        key = job.get("job_key")
        ok_attempt = None
        for att in job.get("attempts") or []:
            if not isinstance(att, Mapping) or not isinstance(att.get("job_id"), str):
                problems.append(f"job {key}: attempt without a job_id")
                continue
            if att.get("outcome") == "ok":
                ok_attempt = att
            gw = att.get("gateway")
            if isinstance(gw, Mapping):
                _check_counters(att["job_id"], gw, by_job.get(att["job_id"], []), problems)
        if job.get("status") == "succeeded":
            _check_succeeded(paths, job, ok_attempt, by_job, problems)


def _int(v: object) -> int:
    return v if isinstance(v, int) and not isinstance(v, bool) else 0


def _check_counters(job_id: str, gw: Mapping, recs: list[dict], problems: list[str]) -> None:
    """The outcome counts of job.json plus in_flight_at_close (requests that ended after the job was closed) equal the
    counts of the call records; refusals counted equal refusal records plus refused_unrecorded."""
    seen = Counter("interrupted" if _admission_only(r) else str(r.get("outcome")) for r in recs)
    in_flight, unrecorded = _int(gw.get("in_flight_at_close")), _int(gw.get("refused_unrecorded"))
    extra = seen.get("interrupted", 0) + sum(n for o, n in seen.items() if o not in OUTCOMES and o != "interrupted")
    for outcome in OUTCOMES:
        diff = seen.get(outcome, 0) - _int(gw.get(outcome))
        if diff < 0:
            problems.append(f"job {job_id}: job.json counts {_int(gw.get(outcome))} {outcome}, records show "
                            f"{seen.get(outcome, 0)}")
        extra += diff
    if extra != in_flight:
        problems.append(f"job {job_id}: {extra} call record(s) beyond the job counters, in_flight_at_close is {in_flight}")
    coded = sum(1 for r in recs if isinstance(r.get("refusal"), Mapping) and r["refusal"].get("code"))
    counted = sum(positive_codes(gw.get("refusal_codes") if isinstance(gw.get("refusal_codes"), Mapping) else {})
                  .values())
    if not 0 <= coded + unrecorded - counted <= in_flight:
        problems.append(f"job {job_id}: {counted} refusal(s) counted, {coded} refusal record(s) plus {unrecorded} "
                        "unrecorded")


def _check_succeeded(paths: StatePaths, job: Mapping, att: Mapping | None, by_job: dict, problems: list[str]) -> None:
    key, split = job.get("job_key"), job.get("split")
    if att is None:
        problems.append(f"job {key}: succeeded without an ok attempt")
        return
    codes = dict(positive_codes(att.get("refusal_codes") if isinstance(att.get("refusal_codes"), Mapping) else {}))
    gw = att.get("gateway") if isinstance(att.get("gateway"), Mapping) else {}
    codes.update(positive_codes(gw.get("refusal_codes") if isinstance(gw.get("refusal_codes"), Mapping) else {}))
    tainted = sorted(set(codes) & TAINT_CODES)
    if tainted:
        problems.append(f"job {key}: succeeded with tainted refusal code(s) {', '.join(tainted)}")
    try:
        out = paths.attempt_dir(str(key), int(att.get("attempt"))) / "out"
    except (TypeError, ValueError):
        problems.append(f"job {key}: ok attempt has no usable attempt number")
        return
    result_path = out / f"{split}.json"
    if att.get("result_usable") is not True or split not in ("val", "test") or not result_is_usable(result_path):
        problems.append(f"job {key}: succeeded without a usable {split}.json")
        return
    sidecar_path = out / "sidecar.jsonl"
    if not sidecar_path.is_file():
        return
    try:
        sidecar = parse_sidecar(util.read_jsonl(sidecar_path))
        result = util.read_json(result_path)
    except (OSError, StateError) as exc:
        problems.append(f"job {key}: sidecar or result unreadable ({type(exc).__name__})")
        return
    rows = join_examples(sidecar, by_job.get(str(att["job_id"]), []), dataset=str(job.get("dataset")), split=split)
    for p in check_examples(rows, result if isinstance(result, Mapping) else {}):
        problems.append(f"job {key}: result against the joined examples: {p}")


def _scan_files(paths: StatePaths) -> list[Path]:
    files: list[Path] = []
    for top in (paths.calls_root, paths.jobs_root):
        if not top.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d not in SCAN_SKIP_DIRS)
            files.extend(Path(dirpath) / n for n in sorted(filenames))
    return files


def _check_scan(paths: StatePaths, problems: list[str]) -> None:
    try:
        digests = scan.load_digests(paths.keydigests)
    except StateError as exc:
        problems.append(f"keydigests.json: {exc}")
        digests = []
    root = str(paths.root) + os.sep
    for f in scan.scan_paths(_scan_files(paths), digests=digests, classes=("secret",)):
        where = f.path[len(root):] if f.path.startswith(root) else f.path
        problems.append(f"{where}: matches the secret rule {f.pattern} at byte {f.offset}")


def _check_receipts(paths: StatePaths, problems: list[str]) -> None:
    root = paths.export_root / "receipts"
    if not root.is_dir():
        return
    for path in sorted(root.glob("*.json")):
        rec = _read(path, problems, f"receipt {path.name}")
        if rec is None or rec.get("status") != "verified":
            continue
        expected = rec.get("expected_spans")
        if rec.get("surface") != "agent_lens" and not isinstance(expected, int):
            continue
        try:
            stored_path = paths.stored_spans_file(str(rec.get("unit")))
        except ValueError:
            problems.append(f"receipt {path.name}: bad unit")
            continue
        stored = _read_any(stored_path)
        spans = stored.get("spans") if isinstance(stored, Mapping) else stored
        if not isinstance(spans, list):
            problems.append(f"receipt {path.name}: verified without stored spans")
        elif len(spans) != expected:
            problems.append(f"receipt {path.name}: {len(spans)} stored spans, expected_spans {expected}")


def _read_any(path: Path) -> object:
    try:
        return util.read_json(path)
    except (OSError, StateError):
        return None


def audit(manifest: dict, paths: StatePaths) -> dict:
    """{"ok", "problems", "calls", "observed_micro", "bounded_unknown_micro", "listed"}. `calls` is the number of call
    records; `listed` holds what is reported without failing (reconciled and limit_exceeded rows, repeatable provider
    failures, admission-only records, open meter rows)."""
    problems: list[str] = []
    listed: dict[str, list[str]] = {"reconciled": [], "limit_exceeded": [], "repeatable_provider_failure": [],
                                    "admission_only": [], "open_meters": []}
    records = _records(paths, problems)
    view = _ledger(paths, problems)
    listed["open_meters"] = list(view["open_meters"])
    _check_totals(view, problems)
    _check_rows(manifest, view, records, problems, listed)
    _check_records(view, records, problems, listed)
    _check_jobs(paths, records, problems)
    _check_scan(paths, problems)
    _check_receipts(paths, problems)
    observed = sum(_int(r["spent_micro"]) for r in view["rows"] if r["status"] in ("complete", "reconciled"))
    unknown = sum(_int(r["reserved_micro"]) for r in view["rows"] if r["status"] == "bounded_unknown")
    return {"ok": not problems, "problems": problems, "calls": len(records), "observed_micro": observed,
            "bounded_unknown_micro": unknown, "listed": listed}
