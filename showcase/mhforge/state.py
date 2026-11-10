# SPDX-License-Identifier: Apache-2.0
# Adapted from ash0ts/harness2-wandb@31b0f82 harness2/showcase/study.py L67-L108 (read-only status) and v4.py L496-L500
# (runner lock). Changes: the lock is a context manager with a holder body and StateLockError instead of SystemExit; the
# ledger is opened through util.open_sqlite_ro (no -wal/-shm, read-only copies) with sums in SQL over the 2.10 DDL
# instead of a full-table read; integer micro-dollars; plus journal, runtime, meters, jobs, receipts, stall alarm.
"""StateLock (runner and export locks) and the read-only status of a campaign (spec 3.4)."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Iterable

from . import util, wire
from .errors import StateError, StateLockError
from .paths import StatePaths
from .util import Clock

__all__ = ["StateLock", "status"]

_MIB = 1048576
_FINAL_RECEIPT = {"verified", "partial", "failed", "refused_secret", "skipped_budget", "skipped_policy", "dry_run"}
_PENDING_RECEIPT = {"planned", "scanned", "emitting", "emitted"}
_JOB_TERMINAL = {"complete", "failed", "interrupted", "skipped", "reset"}


class StateLock:
    """Exclusive, non-blocking flock on runner.lock or export/export.lock. The body is informational."""

    def __init__(self, lock_file: Path, command: str, clock: Clock | None = None, *, host: str | None = None) -> None:
        self.lock_file = Path(lock_file)
        self.command = command
        self.clock = clock
        self.host = host                                     # passed in by the CLI; this module never reads a host name
        self._fd: int | None = None

    def __enter__(self) -> "StateLock":
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lock_file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)   # never truncate a symlink's target
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = _read_holder(fd)
            os.close(fd)
            raise StateLockError(holder=holder) from None
        body = json.dumps({"pid": os.getpid(), "host": self.host, "command": self.command,
                           "started_utc": util.utc_now(self.clock)}).encode("utf-8") + b"\n"
        os.ftruncate(fd, 0)
        os.pwrite(fd, body, 0)
        os.fsync(fd)
        self._fd = fd
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


def _read_holder(fd: int) -> dict:
    try:
        data = os.pread(fd, 65536, 0)
        doc = json.loads(data.decode("utf-8"))
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError, UnicodeDecodeError):
        return {}


def _lock_view(lock_file: Path) -> dict:
    """held via a non-blocking shared flock on the file opened read-only, released at once."""
    view = {"held": False, "pid": None, "command": None, "started_utc": None}
    try:
        fd = os.open(lock_file, os.O_RDONLY)
    except OSError:
        return view
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            view["held"] = True
            holder = _read_holder(fd)
            view.update({k: holder.get(k) for k in ("pid", "command", "started_utc")})
    finally:
        os.close(fd)
    return view


def _doc(path: Path) -> dict | None:
    try:
        doc = util.read_json(path)
    except (OSError, StateError):
        return None
    return doc if isinstance(doc, dict) else None


def _rows(path: Path, reader=util.read_jsonl) -> list[dict]:
    """reader (util.read_jsonl, or util.read_marked_jsonl for the journal); on a bad line in the middle, every parseable
    row (status tolerates half-written files)."""
    try:
        return reader(path)
    except StateError:
        pass
    except OSError:
        return []
    out = []
    try:
        data = Path(path).read_bytes()
    except OSError:
        return []
    for line in data.split(b"\n"):
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if isinstance(row, dict):
            out.append(row)
    return out


def _empty(manifest: dict, exists: bool) -> dict:
    caps = {}
    try:
        phases = util.lookup(manifest, "budgets.solver_usd.phases")
        caps = {p: phases[p] for p in wire.LEDGER_PHASES if p in phases}
        caps["total"] = util.lookup(manifest, "budgets.solver_usd.total")
    except (KeyError, TypeError):
        pass
    zero = util.usd(0)
    return {
        "schema": "mhforge.status/1", "campaign": manifest.get("campaign"), "campaign_kind": manifest.get("campaign_kind"),
        "state_exists": exists, "manifest_sha256": None, "amendments": 0,
        "stall": {"journal_age_s": None, "alarm": False},
        "lock": {"held": False, "pid": None, "command": None, "started_utc": None},
        "halt": None, "stopped": None, "ledger_view": "absent",
        "runtime": {"pid": None, "command": None, "key_source": None, "key_scrubbed": {}},
        "solver": {"observed_usd": zero, "bounded_unknown_usd": zero, "in_flight_usd": zero, "committed_by_phase_usd": {},
                   "caps_usd": caps, "requests": 0, "unknown_requests": 0, "bounded_unknown_requests": 0,
                   "reconciled_requests": 0},
        "meters": {"sandbox_seconds": 0.0, "sandbox_usd": zero, "sandbox_open_rows": 0, "lens_planned_mb": 0.0,
                   "lens_planned_mb_by_bucket": {b: 0.0 for b in wire.LENS_BUCKETS}, "lens_planned_spans": 0,
                   "wandb_upload_mb": 0.0, "eval_tables": 0, "proposer_list_usd": zero, "proposer_open_rows": 0},
        "jobs": {"succeeded": 0, "failed": 0, "running": 0, "skipped_usable": 0, "not_isolated": 0},
        "iterations": {"settled": 0, "last": None},
        "proposer": {"sessions": 0, "last_outcome": None},
        "qualification": {"passed": False},
        "export": {"verified": 0, "pending": 0, "partial": 0, "failed": 0, "refused_secret": 0, "skipped": 0,
                   "dry_run": 0, "stale": 0},
        "circuit": {"failures": 0, "until_epoch": 0.0},
    }


def _ledger(paths: StatePaths, out: dict) -> None:
    if not paths.ledger.exists():
        return
    try:
        conn, stale = util.open_sqlite_ro(paths.ledger)
    except Exception:                                        # sqlite3.Error, OSError: a half-written or foreign file
        out["ledger_view"] = "unreadable"
        return
    out["ledger_view"] = "possibly_stale" if stale else "current"
    solver, meters = out["solver"], out["meters"]
    try:
        q = conn.execute
        row = q("SELECT COALESCE(SUM(CASE WHEN status IN ('complete','reconciled') THEN spent_micro END), 0),"
                " COALESCE(SUM(CASE WHEN status = 'bounded_unknown' THEN reserved_micro END), 0),"
                " COALESCE(SUM(CASE WHEN status = 'pending' THEN reserved_micro END), 0), COUNT(*),"
                " COALESCE(SUM(status = 'unknown'), 0), COALESCE(SUM(status = 'bounded_unknown'), 0),"
                " COALESCE(SUM(status = 'reconciled'), 0) FROM calls").fetchone()
        solver.update(observed_usd=util.usd(row[0]), bounded_unknown_usd=util.usd(row[1]),
                      in_flight_usd=util.usd(row[2]), requests=row[3], unknown_requests=row[4],
                      bounded_unknown_requests=row[5], reconciled_requests=row[6])
        solver["committed_by_phase_usd"] = {p: util.usd(c) for p, c in
                                            q("SELECT phase, committed_micro FROM phase_totals ORDER BY phase") if c}
        halt = q("SELECT reason, detail, call_id, set_utc FROM halt WHERE id = 1").fetchone()
        out["halt"] = dict(zip(("reason", "detail", "call_id", "set_utc"), halt)) if halt else None
        for meter, bucket, quantity, micro, open_rows in q(
                "SELECT meter, bucket, COALESCE(SUM(quantity), 0), COALESCE(SUM(micro_usd), 0),"
                " COALESCE(SUM(status = 'open'), 0) FROM meter_events GROUP BY meter, bucket"):
            if meter == "sandbox_seconds":
                meters["sandbox_seconds"] = round(meters["sandbox_seconds"] + quantity / 1000.0, 3)
                meters["sandbox_usd"] = util.usd(util.micros(meters["sandbox_usd"]) + micro)
                meters["sandbox_open_rows"] += open_rows
            elif meter == "lens_bytes":
                meters["lens_planned_mb"] = round(meters["lens_planned_mb"] + quantity / _MIB, 3)
                if bucket in meters["lens_planned_mb_by_bucket"]:
                    meters["lens_planned_mb_by_bucket"][bucket] = round(quantity / _MIB, 3)
            elif meter == "lens_spans":
                meters["lens_planned_spans"] += quantity
            elif meter == "wandb_upload_bytes":
                meters["wandb_upload_mb"] = round(meters["wandb_upload_mb"] + quantity / _MIB, 3)
            elif meter == "eval_tables":
                meters["eval_tables"] += quantity
            elif meter == "proposer_list_price":
                meters["proposer_list_usd"] = util.usd(util.micros(meters["proposer_list_usd"]) + (micro or quantity))
                meters["proposer_open_rows"] += open_rows
    except Exception:                                        # a ledger mid-creation: report what was read
        out["ledger_view"] = "unreadable"
    finally:
        conn.close()


def _job_records(paths: StatePaths, out: dict) -> None:
    jobs = out["jobs"]
    try:
        entries = sorted(os.listdir(paths.jobs_root))
    except OSError:
        return
    for name in entries:
        doc = _doc(paths.jobs_root / name / "job.json")
        if doc is None:
            continue
        if doc.get("status") in ("succeeded", "failed", "skipped_usable"):
            jobs[doc["status"]] += 1
        if doc.get("isolated") is False:
            jobs["not_isolated"] += 1


def _journal(rows: list[dict], out: dict) -> set[str]:
    """Running jobs, iterations, proposer sessions; returns the export units the journal makes ready."""
    open_jobs: dict[tuple, bool] = {}
    units: set[str] = set()
    settled: list[int] = []
    for r in rows:
        op, key, st, attempt, detail = r.get("op"), r.get("key"), r.get("status"), r.get("attempt"), r.get("detail")
        detail = detail if isinstance(detail, dict) else {}
        if op == "job":
            if st == "started":
                open_jobs[(key, attempt)] = True
            elif st in _JOB_TERMINAL:
                open_jobs.pop((key, attempt), None)
                if st in ("complete", "failed") and attempt is not None:
                    units.add(f"job:{key}.a{attempt}")
        elif op == "benchmark" and st == "complete":
            units.add(f"eval:{key}")
        elif op == "proposer" and st == "complete":
            out["proposer"]["sessions"] += 1
            out["proposer"]["last_outcome"] = detail.get("outcome")
            units.add(f"proposer:{key}.s{attempt}")
            for cand in detail.get("candidates") or ():
                if isinstance(cand, dict) and cand.get("name"):
                    units.add(f"propose:{cand['name']}")
        elif op == "iteration" and st == "settled" and isinstance(key, str) and key.startswith("iter"):
            try:
                settled.append(int(key[4:]))
            except ValueError:
                continue
            units.add(f"loop:{key}")
    out["jobs"]["running"] = len(open_jobs)
    out["iterations"] = {"settled": len(set(settled)), "last": max(settled) if settled else None}
    return units


def _receipts(paths: StatePaths, ready_units: Iterable[str], out: dict) -> None:
    exp = out["export"]
    seen_units: set[str] = set()
    root = paths.export_root / "receipts"
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".json"):
            continue
        doc = _doc(root / name)
        if doc is None:
            continue
        if isinstance(doc.get("unit"), str):
            seen_units.add(doc["unit"])
        st = doc.get("status")
        if st in ("verified", "partial", "failed", "refused_secret", "dry_run"):
            exp[st] += 1
        elif st in ("skipped_budget", "skipped_policy"):
            exp["skipped"] += 1
        elif st in _PENDING_RECEIPT:
            exp["pending"] += 1
        if st in _FINAL_RECEIPT and doc.get("stale_plan_sha256"):
            exp["stale"] += 1
    exp["pending"] += sum(1 for u in set(ready_units) if u not in seen_units)


def status(manifest: dict, paths: StatePaths, *, clock: Clock | None = None) -> dict:
    """Read-only: creates no directory or file, takes no lock, opens SQLite read-only, reads no credential."""
    clock = clock or util.SystemClock()
    out = _empty(manifest, Path(paths.root).is_dir())
    if not out["state_exists"]:
        return out
    try:
        digest = paths.manifest_sha256.read_text("ascii").strip()
        out["manifest_sha256"] = digest if len(digest) == 64 else None
    except (OSError, UnicodeDecodeError):
        pass
    out["amendments"] = len(_rows(paths.amendments))
    out["lock"] = _lock_view(paths.runner_lock)
    out["stopped"] = _doc(paths.stopped)
    runtime = _doc(paths.runtime) or {}
    scrubbed = runtime.get("key_scrubbed")
    out["runtime"] = {"pid": runtime.get("pid"), "command": runtime.get("command"),
                      "key_source": runtime.get("key_source"),
                      "key_scrubbed": dict(scrubbed) if isinstance(scrubbed, dict) else {}}
    _ledger(paths, out)
    _job_records(paths, out)
    journal = _rows(paths.journal, util.read_marked_jsonl)
    ready = _journal(journal, out)
    for row in reversed(journal):
        try:
            age = clock.now() - util.parse_utc(row.get("utc"))
        except ValueError:
            continue
        out["stall"]["journal_age_s"] = round(age, 3)
        break
    age = out["stall"]["journal_age_s"]
    try:
        limit = util.lookup(manifest, "runner.stall_alarm_s")
    except KeyError:
        limit = 10800
    out["stall"]["alarm"] = bool(out["lock"]["held"] and age is not None and age > limit)
    passed = _doc(paths.qualification_passed)
    out["qualification"]["passed"] = bool(passed and out["manifest_sha256"]
                                         and passed.get("manifest_sha256") == out["manifest_sha256"])
    _receipts(paths, ready, out)
    circuit = _doc(paths.circuit) or {}
    if isinstance(circuit.get("failures"), int) and isinstance(circuit.get("until_epoch"), (int, float)):
        out["circuit"] = {"failures": circuit["failures"], "until_epoch": float(circuit["until_epoch"])}
    return out
