# SPDX-License-Identifier: Apache-2.0
# Adapted from ash0ts/harness2-wandb@31b0f82 harness2/showcase/budget.py. Changes: explicit per-job ceilings in place of
# max_steps * 2; no schema_version switch (bounded-missing behaviour only); status index, targeted halt query and running
# totals in place of a full-table read per call; a provider rejection books zero; halt flag in the database; job registry;
# six meters with open and final rows and ingest buckets; phases renamed and extended; reconcile; limit_exceeded settles
# instead of halting; synchronous=FULL (and fullfsync on macOS) on every connection.
"""SQLite ledger (spec 3.6, DDL 2.10): reserve, settle, reconcile, phases, per-job ceilings, halt flag, job registry,
meters. Every operation opens its own connection and runs inside BEGIN IMMEDIATE ... COMMIT. Money is integer
micro-dollars; no float ever touches it."""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Iterator, Mapping

from . import util
from .errors import HALT_REASONS, BudgetError, MeterCapExceeded, StateError
from .wire import LEDGER_PHASES, LENS_BUCKETS, METERS, JobIdentity

FULLFSYNC = sys.platform == "darwin"               # PRAGMA fullfsync=ON on macOS (2.10)
SCHEMA_VERSION = "1"
MIB = 1048576
CALL_STATUSES = ("pending", "complete", "rejected", "bounded_unknown", "unknown", "reconciled")
METER_UNITS = {"sandbox_seconds": "ms", "lens_bytes": "bytes", "lens_spans": "spans",
               "proposer_list_price": "micro_usd", "wandb_upload_bytes": "bytes", "eval_tables": "tables"}
BUCKETED_METERS = ("lens_bytes", "lens_spans")


def _sql_list(values) -> str:
    return ",".join("'%s'" % v for v in values)


DDL = [
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS jobs (job_id TEXT PRIMARY KEY, job_key TEXT NOT NULL, identity_json TEXT NOT NULL,
       token_sha256 TEXT, registered_utc TEXT NOT NULL, closed_utc TEXT, close_reason TEXT)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS jobs_token ON jobs(token_sha256) WHERE token_sha256 IS NOT NULL",
    f"""CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, call_id TEXT NOT NULL, n INTEGER NOT NULL CHECK (n >= 1),
       job_id TEXT NOT NULL, job_key TEXT NOT NULL,
       phase TEXT NOT NULL CHECK (phase IN ({_sql_list(LEDGER_PHASES)})), origin_phase TEXT NOT NULL,
       model TEXT NOT NULL, input_limit INTEGER NOT NULL CHECK (input_limit > 0),
       output_limit INTEGER NOT NULL CHECK (output_limit > 0), reserved_micro INTEGER NOT NULL CHECK (reserved_micro >= 0),
       spent_micro INTEGER, status TEXT NOT NULL CHECK (status IN ({_sql_list(CALL_STATUSES)})),
       usage_json TEXT, reason TEXT, created_utc TEXT NOT NULL, settled_utc TEXT)""",
    "CREATE INDEX IF NOT EXISTS calls_status ON calls(status)",
    "CREATE INDEX IF NOT EXISTS calls_job ON calls(job_key)",
    "CREATE INDEX IF NOT EXISTS calls_call ON calls(call_id)",
    """CREATE TABLE IF NOT EXISTS reconcile_history (ledger_id TEXT NOT NULL, old_status TEXT NOT NULL,
       spent_micro INTEGER NOT NULL, below_reservation INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL, utc TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS phase_totals (phase TEXT PRIMARY KEY,
       committed_micro INTEGER NOT NULL DEFAULT 0 CHECK (committed_micro >= 0))""",
    """CREATE TABLE IF NOT EXISTS job_counters (job_key TEXT PRIMARY KEY, job_id TEXT NOT NULL,
       requests INTEGER NOT NULL DEFAULT 0, charged_attempts INTEGER NOT NULL DEFAULT 0,
       committed_micro INTEGER NOT NULL DEFAULT 0, prior_charged INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS failed_requests (job_key TEXT NOT NULL, request_sha256 TEXT NOT NULL,
       exhausted INTEGER NOT NULL DEFAULT 0, peer_ok INTEGER NOT NULL DEFAULT 0, last_job_id TEXT NOT NULL,
       last_call_id TEXT NOT NULL, utc TEXT NOT NULL, PRIMARY KEY (job_key, request_sha256))""",
    """CREATE TABLE IF NOT EXISTS halt (id INTEGER PRIMARY KEY CHECK (id = 1), reason TEXT NOT NULL,
       detail TEXT NOT NULL DEFAULT '', call_id TEXT, set_utc TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS halt_history (set_utc TEXT NOT NULL, reason TEXT NOT NULL,
       detail TEXT NOT NULL DEFAULT '', cleared_utc TEXT, cleared_note TEXT)""",
    f"""CREATE TABLE IF NOT EXISTS meter_events (meter TEXT NOT NULL CHECK (meter IN ({_sql_list(METERS)})),
       key TEXT NOT NULL, bucket TEXT NOT NULL DEFAULT '', status TEXT NOT NULL CHECK (status IN ('open','final')),
       quantity INTEGER NOT NULL CHECK (quantity >= 0), unit TEXT NOT NULL, micro_usd INTEGER, detail_json TEXT,
       utc TEXT NOT NULL, closed_utc TEXT, PRIMARY KEY (meter, key))""",
]


# ---------------------------------------------------------------- connections

def connect(path: Path) -> sqlite3.Connection:
    """One connection with the pragmas of 2.10 (used by the ledger and the replay store)."""
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=FULL")
    if FULLFSYNC:
        conn.execute("PRAGMA fullfsync=ON")
    return conn


@contextlib.contextmanager
def transaction(path: Path) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()


def create_file(path: Path, ddl: list[str]) -> bool:
    """Create the tables (WAL set once, at creation). True when the file was new."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists() or path.stat().st_size == 0
    if new:
        conn = connect(path)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        finally:
            conn.close()
    with transaction(path) as db:
        for statement in ddl:
            db.execute(statement)
    return new


# ---------------------------------------------------------------- pure money

def _ceil_int(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def prices_from_manifest(manifest: Mapping) -> dict[str, Decimal]:
    """{"input", "cached", "output"}: USD per million tokens, which is micro-dollars per token."""
    return {"input": Decimal(util.lookup(manifest, "prices.input_per_million")),
            "cached": Decimal(util.lookup(manifest, "prices.cached_per_million")),
            "output": Decimal(util.lookup(manifest, "prices.output_per_million"))}


def reservation_micro(prices: Mapping[str, Decimal], input_limit: int, output_limit: int) -> int:
    """ceil(input * input_limit + output * output_limit); no cache discount (H2 budget.py L105-L112)."""
    return _ceil_int(Decimal(prices["input"]) * input_limit + Decimal(prices["output"]) * output_limit)


def cost_micro(prices: Mapping[str, Decimal], usage: Mapping) -> int:
    prompt, completion = usage["prompt_tokens"], usage["completion_tokens"]
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    return _ceil_int((prompt - cached) * Decimal(prices["input"]) + cached * Decimal(prices["cached"])
                     + completion * Decimal(prices["output"]))


def usage_within_limits(usage, input_limit, output_limit):
    """Every supplied counter must be consistent, even when others are absent."""
    if usage is None:
        return True
    if not isinstance(usage, dict):
        return False
    limits = {
        "prompt_tokens": input_limit,
        "completion_tokens": output_limit,
        "total_tokens": input_limit + output_limit,
    }
    for key, limit in limits.items():
        value = usage.get(key)
        if value is not None and (type(value) is not int or not 0 <= value <= limit):
            return False
    for parent, key, count, maximum in (
        ("prompt_tokens_details", "cached_tokens", "prompt_tokens", input_limit),
        ("completion_tokens_details", "reasoning_tokens", "completion_tokens", output_limit),
    ):
        details = usage.get(parent)
        if details is None:
            continue
        if not isinstance(details, dict):
            return False
        value = details.get(key)
        ceiling = usage[count] if usage.get(count) is not None else maximum
        if value is not None and (type(value) is not int or not 0 <= value <= ceiling):
            return False
    return True


def _counter_ok(value: object) -> bool:
    return value is None or (type(value) is int and value >= 0)


def judge_usage(usage: object, prices: Mapping[str, Decimal], input_limit: int, output_limit: int,
                reserved: int) -> tuple[str, int | None, str | None]:
    """(status, cost or None, reason) per the settle table of 3.6 (rejection excluded)."""
    if usage is None:
        return "bounded_unknown", None, "usage missing"
    if not isinstance(usage, Mapping):
        return "unknown", None, "usage malformed"
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if not _counter_ok(usage.get(key)):
            return "unknown", None, f"usage malformed: {key}"
    for parent, key in (("prompt_tokens_details", "cached_tokens"), ("completion_tokens_details", "reasoning_tokens")):
        details = usage.get(parent)
        if details is not None and (not isinstance(details, Mapping) or not _counter_ok(details.get(key))):
            return "unknown", None, f"usage malformed: {parent}"
    prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    if prompt is not None and cached > prompt:
        return "unknown", None, "usage malformed: cached exceeds prompt"
    if prompt is None or completion is None:
        floor = cost_micro(prices, {"prompt_tokens": prompt if prompt is not None else cached,
                                    "completion_tokens": completion or 0,
                                    "prompt_tokens_details": {"cached_tokens": cached}})
        if floor > reserved:
            return "unknown", None, "partial usage above the reservation"
        return "bounded_unknown", None, "usage partial"
    cost = cost_micro(prices, usage)
    if cost > reserved:
        return "unknown", None, "cost above the reservation"
    plain = json.loads(json.dumps(usage, default=str))
    if not usage_within_limits(plain, input_limit, output_limit):
        return "complete", cost, "limit_exceeded"
    return "complete", cost, None


# ---------------------------------------------------------------- records

@dataclass(frozen=True)
class Settlement:
    status: str                                    # complete | rejected | bounded_unknown | unknown
    spent_micro: int | None
    basis: str                                     # usage | reservation | none


@dataclass(frozen=True)
class HaltState:
    reason: str
    detail: str
    set_utc: str
    call_id: str | None


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _attempt_of(job_id: str) -> int:
    try:
        return int(job_id.rsplit(".a", 1)[1])
    except (IndexError, ValueError):
        return 0


class Ledger:
    def __init__(self, path: Path, manifest: dict, *, clock: util.Clock | None = None,
                 ids: util.IdSource | None = None) -> None:
        self.path = Path(path)
        self.clock = clock or util.SystemClock()
        self.ids = ids or util.IdSource(self.clock)
        self.prices = prices_from_manifest(manifest)
        self.model = util.lookup(manifest, "prices.model")
        self.campaign = util.lookup(manifest, "campaign")
        self.max_input = int(util.lookup(manifest, "gateway.model_context_tokens"))
        self.max_output = int(util.lookup(manifest, "gateway.max_output_tokens"))
        self.max_requests = int(util.lookup(manifest, "gateway.max_requests_per_job"))
        self.max_job_micro = util.micros(util.lookup(manifest, "gateway.max_job_usd"))
        self.caps = {"total": util.micros(util.lookup(manifest, "budgets.solver_usd.total"))}
        for phase in LEDGER_PHASES:
            self.caps[phase] = util.micros(util.lookup(manifest, f"budgets.solver_usd.phases.{phase}"))
        self.budgets = util.lookup(manifest, "budgets")
        create_file(self.path, DDL)
        with self._tx() as db:
            meta = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM meta")}
            if not meta:
                for key, value in (("schema_version", SCHEMA_VERSION), ("campaign", self.campaign),
                                   ("manifest_sha256", util.sha256_bytes(util.canonical_json(manifest))),
                                   ("created_utc", self._utc())):
                    db.execute("INSERT INTO meta (key, value) VALUES (?, ?)", (key, value))
                for phase in LEDGER_PHASES:
                    db.execute("INSERT INTO phase_totals (phase, committed_micro) VALUES (?, 0)", (phase,))
            elif meta.get("schema_version") != SCHEMA_VERSION:
                raise StateError(f"ledger schema_version {meta.get('schema_version')!r} is not supported")
            elif meta.get("campaign") != self.campaign:
                raise StateError("ledger belongs to another campaign")

    def _tx(self):
        return transaction(self.path)

    def _one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        with self._tx() as db:
            return db.execute(sql, args).fetchone()

    def _write(self, sql: str, args: tuple = ()) -> int:
        with self._tx() as db:
            return db.execute(sql, args).rowcount

    def _utc(self) -> str:
        return util.utc_iso(self.clock.now())

    # ------------------------------------------------------------ jobs

    def _counters(self, db: sqlite3.Connection, job: JobIdentity) -> sqlite3.Row:
        """The job_counters row, (re)set for job.job_id when this is a newer job attempt (register_job semantics)."""
        row = db.execute("SELECT * FROM job_counters WHERE job_key=?", (job.job_key,)).fetchone()
        if row is None:
            db.execute("INSERT INTO job_counters (job_key, job_id) VALUES (?, ?)", (job.job_key, job.job_id))
        elif row["job_id"] != job.job_id and job.attempt > _attempt_of(row["job_id"]):
            db.execute("UPDATE job_counters SET job_id=?, requests=0, prior_charged=? WHERE job_key=?",
                       (job.job_id, 1 if row["charged_attempts"] > 0 else 0, job.job_key))
        else:
            return row
        return db.execute("SELECT * FROM job_counters WHERE job_key=?", (job.job_key,)).fetchone()

    def register_job(self, job: JobIdentity, *, token_sha256: str | None = None) -> None:
        job.validate()
        if token_sha256 is not None and (not isinstance(token_sha256, str)
                                         or not re.fullmatch(r"[0-9a-f]{64}", token_sha256)):
            raise ValueError("token_sha256 must be 64 lower-case hex")
        with self._tx() as db:
            db.execute("INSERT OR IGNORE INTO jobs (job_id, job_key, identity_json, token_sha256, registered_utc) "
                       "VALUES (?, ?, ?, ?, ?)", (job.job_id, job.job_key, json.dumps(job.to_dict(), sort_keys=True),
                                                   token_sha256, self._utc()))
            self._counters(db, job)

    def close_job(self, job_id: str, reason: str = "closed") -> None:
        self._write("UPDATE jobs SET closed_utc=?, close_reason=? WHERE job_id=? AND closed_utc IS NULL",
                    (self._utc(), reason, job_id))

    def close_open_jobs(self, reason: str = "runner interrupted") -> int:
        return self._write("UPDATE jobs SET closed_utc=?, close_reason=? WHERE closed_utc IS NULL", (self._utc(), reason))

    def identity_for_token(self, token: str) -> JobIdentity | None:
        if not isinstance(token, str) or not token:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        row = self._one("SELECT identity_json FROM jobs WHERE token_sha256=? AND closed_utc IS NULL", (digest,))
        return None if row is None else JobIdentity.from_dict(json.loads(row["identity_json"]))

    def is_open(self, job_id: str) -> bool:
        return self._one("SELECT 1 FROM jobs WHERE job_id=? AND closed_utc IS NULL", (job_id,)) is not None

    def prior_charged(self, job_key: str) -> bool:
        row = self._one("SELECT prior_charged FROM job_counters WHERE job_key=?", (job_key,))
        return bool(row and row["prior_charged"])

    def job_requests(self, job_key: str) -> int:
        row = self._one("SELECT requests FROM job_counters WHERE job_key=?", (job_key,))
        return int(row["requests"]) if row else 0

    def interrupt_pending(self) -> int:
        return self._write("UPDATE calls SET status='bounded_unknown', reason='runner interrupted', settled_utc=? "
                           "WHERE status='pending'", (self._utc(),))

    # ------------------------------------------------------------ reserve and settle

    def reserve(self, *, call_id: str, n: int, job: JobIdentity, phase: str, model: str,
                input_limit: int, output_limit: int) -> str:
        ledger_id = self.ids.ledger_id()
        with self._tx() as db:
            if db.execute("SELECT 1 FROM halt LIMIT 1").fetchone():
                raise BudgetError(kind="halted", phase=phase)
            if model != self.model:
                raise BudgetError("budget refused: invalid (model not in manifest)", kind="invalid", phase=phase)
            if phase not in LEDGER_PHASES or job.problems() or not isinstance(call_id, str) or not call_id:
                raise BudgetError("budget refused: invalid (phase, job or call id)", kind="invalid", phase=phase)
            if not (_is_int(n) and n >= 1 and _is_int(input_limit) and 1 <= input_limit <= self.max_input
                    and _is_int(output_limit) and 1 <= output_limit <= self.max_output):
                raise BudgetError("budget refused: invalid (attempt number or limits)", kind="invalid", phase=phase)
            if db.execute("SELECT 1 FROM calls WHERE status='unknown' LIMIT 1").fetchone():
                raise BudgetError(kind="unreconciled", phase=phase)
            reservation = reservation_micro(self.prices, input_limit, output_limit)
            counters = self._counters(db, job)
            current = counters["job_id"] == job.job_id
            if n == 1 and counters["requests"] >= self.max_requests:
                raise BudgetError(kind="ceiling", phase=phase, detail="requests")
            if counters["charged_attempts"] >= 2 * self.max_requests:
                raise BudgetError(kind="ceiling", phase=phase, detail="attempts")
            if counters["committed_micro"] + reservation > self.max_job_micro:
                raise BudgetError(kind="ceiling", phase=phase, detail="job_usd")
            total = db.execute("SELECT COALESCE(SUM(committed_micro), 0) FROM phase_totals").fetchone()[0]
            if total + reservation > self.caps["total"]:
                raise BudgetError(kind="total", phase=phase)
            row = db.execute("SELECT committed_micro FROM phase_totals WHERE phase=?", (phase,)).fetchone()
            if row is None:
                raise StateError(f"ledger phase_totals row missing for {phase}")
            if row[0] + reservation > self.caps[phase]:
                raise BudgetError(kind="cap", phase=phase)
            db.execute("INSERT INTO calls (id, call_id, n, job_id, job_key, phase, origin_phase, model, input_limit, "
                       "output_limit, reserved_micro, status, created_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,'pending',?)",
                       (ledger_id, call_id, n, job.job_id, job.job_key, phase, job.phase, model, input_limit,
                        output_limit, reservation, self._utc()))
            self._book(db, phase, job.job_key, reservation, charged=1, requests=1 if (n == 1 and current) else 0)
        return ledger_id

    @staticmethod
    def _book(db: sqlite3.Connection, phase: str, job_key: str, delta: int, *, charged: int = 0, requests: int = 0) -> None:
        cur = db.execute("UPDATE phase_totals SET committed_micro = committed_micro + ? WHERE phase=?", (delta, phase))
        if cur.rowcount != 1:
            raise StateError(f"ledger phase_totals row missing for {phase}")
        cur = db.execute("UPDATE job_counters SET committed_micro = committed_micro + ?, "
                         "charged_attempts = charged_attempts + ?, requests = requests + ? WHERE job_key=?",
                         (delta, charged, requests, job_key))
        if cur.rowcount != 1:
            raise StateError(f"ledger job_counters row missing for {job_key}")

    def settle(self, ledger_id: str, usage: Mapping | None, *, rejection: str | None = None,
               reason: str = "") -> Settlement:
        with self._tx() as db:
            row = db.execute("SELECT * FROM calls WHERE id=?", (ledger_id,)).fetchone()
            if row is None:
                raise StateError("settle: unknown ledger id")
            if row["status"] != "pending":
                raise BudgetError(kind="settled", phase=row["phase"])
            reserved, now = row["reserved_micro"], self._utc()
            usage_json = None if usage is None else json.dumps(usage, ensure_ascii=False, default=str)
            if rejection:
                self._book(db, row["phase"], row["job_key"], -reserved, charged=-1)
                db.execute("UPDATE calls SET status='rejected', spent_micro=0, usage_json=?, reason=?, settled_utc=? "
                           "WHERE id=?", (usage_json, f"{rejection}: {reason}" if reason else rejection, now, ledger_id))
                return Settlement("rejected", 0, "none")
            status, cost, why = judge_usage(usage, self.prices, row["input_limit"], row["output_limit"], reserved)
            text = "; ".join(t for t in (why, reason) if t) or None
            if status == "complete":
                self._book(db, row["phase"], row["job_key"], cost - reserved)
            elif status == "unknown":
                self._set_halt(db, "usage_contradiction", f"{ledger_id}: {why}", row["call_id"])
            db.execute("UPDATE calls SET status=?, spent_micro=?, usage_json=?, reason=?, settled_utc=? WHERE id=?",
                       (status, cost, usage_json, text, now, ledger_id))
            return Settlement(status, cost, "usage" if status == "complete" else "reservation")

    def reconcile(self, ledger_id: str, spent_micro: int, note: str, *, below_reservation: bool = False) -> None:
        if not isinstance(note, str) or not note.strip():
            raise ValueError("reconcile needs a note")
        if not _is_int(spent_micro) or spent_micro < 0:
            raise ValueError("reconcile needs a non-negative integer spend in micro-dollars")
        with self._tx() as db:
            row = db.execute("SELECT * FROM calls WHERE id=?", (ledger_id,)).fetchone()
            if row is None or row["status"] != "unknown":
                raise StateError("reconcile: the row is absent or not 'unknown'")
            if spent_micro < row["reserved_micro"] and not below_reservation:
                raise ValueError("a spend below the reservation needs below_reservation (a billing-export figure)")
            self._book(db, row["phase"], row["job_key"], spent_micro - row["reserved_micro"])
            now = self._utc()
            db.execute("UPDATE calls SET status='reconciled', spent_micro=?, settled_utc=? WHERE id=?",
                       (spent_micro, now, ledger_id))
            db.execute("INSERT INTO reconcile_history (ledger_id, old_status, spent_micro, below_reservation, note, utc) "
                       "VALUES (?, 'unknown', ?, ?, ?, ?)", (ledger_id, spent_micro, 1 if below_reservation else 0,
                                                            note.strip(), now))

    def note_exhausted(self, job: JobIdentity, request_sha256: str, call_id: str, *, peer_ok: bool) -> bool:
        now = self._utc()
        with self._tx() as db:
            row = db.execute("SELECT * FROM failed_requests WHERE job_key=? AND request_sha256=?",
                             (job.job_key, request_sha256)).fetchone()
            if row is None:
                db.execute("INSERT INTO failed_requests (job_key, request_sha256, exhausted, peer_ok, last_job_id, "
                           "last_call_id, utc) VALUES (?, ?, 1, ?, ?, ?, ?)",
                           (job.job_key, request_sha256, 1 if peer_ok else 0, job.job_id, call_id, now))
                return False
            new_attempt = row["last_job_id"] != job.job_id
            db.execute("UPDATE failed_requests SET exhausted = exhausted + ?, peer_ok = ?, last_job_id=?, "
                       "last_call_id=?, utc=? WHERE job_key=? AND request_sha256=?",
                       (1 if new_attempt else 0, 1 if (peer_ok or row["peer_ok"]) else 0, job.job_id, call_id, now,
                        job.job_key, request_sha256))
            return new_attempt and bool(peer_ok or row["peer_ok"])

    # ------------------------------------------------------------ halt

    def halted(self) -> HaltState | None:
        row = self._one("SELECT reason, detail, set_utc, call_id FROM halt WHERE id=1")
        return None if row is None else HaltState(row["reason"], row["detail"], row["set_utc"], row["call_id"])

    def _set_halt(self, db: sqlite3.Connection, reason: str, detail: str, call_id: str | None) -> bool:
        if reason not in HALT_REASONS:
            raise ValueError(f"unknown halt reason {reason!r}")
        now = self._utc()
        cur = db.execute("INSERT OR IGNORE INTO halt (id, reason, detail, call_id, set_utc) VALUES (1, ?, ?, ?, ?)",
                         (reason, detail or "", call_id, now))
        if cur.rowcount != 1:
            return False
        db.execute("INSERT INTO halt_history (set_utc, reason, detail) VALUES (?, ?, ?)", (now, reason, detail or ""))
        return True

    def set_halt(self, reason: str, detail: str = "", call_id: str | None = None) -> bool:
        with self._tx() as db:
            return self._set_halt(db, reason, detail, call_id)

    def clear_halt(self, note: str) -> None:
        if not isinstance(note, str) or not note.strip():
            raise ValueError("clear_halt needs a note")
        with self._tx() as db:
            if db.execute("SELECT 1 FROM calls WHERE status='unknown' LIMIT 1").fetchone():
                raise BudgetError("budget refused: unreconciled rows exist; reconcile them first", kind="unreconciled")
            if db.execute("DELETE FROM halt WHERE id=1").rowcount:
                db.execute("UPDATE halt_history SET cleared_utc=?, cleared_note=? WHERE cleared_utc IS NULL",
                           (self._utc(), note.strip()))

    # ------------------------------------------------------------ totals

    def committed(self, phase: str | None = None) -> int:
        if phase is None:
            return int(self._one("SELECT COALESCE(SUM(committed_micro), 0) FROM phase_totals")[0])
        row = self._one("SELECT committed_micro FROM phase_totals WHERE phase=?", (phase,))
        if row is None:
            raise StateError(f"ledger phase_totals row missing for {phase}")
        return int(row[0])

    # ------------------------------------------------------------ meters

    @staticmethod
    def _meter_args(meter: str, key: str, quantity: int, unit: str | None, micro: int | None, bucket: str) -> None:
        if meter not in METERS:
            raise ValueError(f"unknown meter {meter!r}")
        if not isinstance(key, str) or not key:
            raise ValueError("a meter key must be a non-empty string")
        if not _is_int(quantity) or quantity < 0 or (micro is not None and (not _is_int(micro) or micro < 0)):
            raise ValueError("meter figures must be non-negative integers")
        if meter == "sandbox_seconds" and micro is None:   # its cap is in micro-dollars: a NULL would count as $0
            raise ValueError("meter sandbox_seconds needs its priced figure in micro-dollars")
        if unit is not None and unit != METER_UNITS[meter]:
            raise ValueError(f"meter {meter} is counted in {METER_UNITS[meter]}, not {unit!r}")
        if meter in BUCKETED_METERS:
            if bucket not in LENS_BUCKETS:
                raise ValueError(f"meter {meter} needs a bucket in {LENS_BUCKETS}")
        elif bucket != "":
            raise ValueError(f"meter {meter} has no buckets")

    def _insert_meter(self, meter, key, quantity, unit, micro, detail, bucket, status) -> bool:
        if micro is None and meter == "proposer_list_price":
            micro = quantity
        self._meter_args(meter, key, quantity, unit, micro, bucket)
        now = self._utc()
        return self._write(
            "INSERT OR IGNORE INTO meter_events (meter, key, bucket, status, quantity, unit, micro_usd, detail_json, "
            "utc, closed_utc) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (meter, key, bucket, status, quantity, unit, micro,
             None if detail is None else json.dumps(dict(detail), sort_keys=True, ensure_ascii=False),
             now, now if status == "final" else None)) == 1

    def meter(self, meter: str, key: str, quantity: int, unit: str, *, micro_usd: int | None = None,
              detail: Mapping | None = None, bucket: str = "") -> bool:
        return self._insert_meter(meter, key, quantity, unit, micro_usd, detail, bucket, "final")

    def meter_open(self, meter: str, key: str, worst_quantity: int, unit: str, *, worst_micro: int | None = None,
                   detail: Mapping | None = None) -> bool:
        if meter in BUCKETED_METERS:
            raise ValueError(f"meter {meter} is one-shot; use meter()")
        return self._insert_meter(meter, key, worst_quantity, unit, worst_micro, detail, "", "open")

    def meter_close(self, meter: str, key: str, quantity: int, *, micro_usd: int | None = None) -> bool:
        if micro_usd is None and meter == "proposer_list_price":
            micro_usd = quantity
        self._meter_args(meter, key, quantity, None, micro_usd, "main" if meter in BUCKETED_METERS else "")
        return self._write("UPDATE meter_events SET status='final', quantity=?, micro_usd=?, closed_utc=? "
                           "WHERE meter=? AND key=? AND status='open'", (quantity, micro_usd, self._utc(), meter, key)) == 1

    def open_meter_rows(self, meter: str) -> list[dict]:
        with self._tx() as db:
            rows = db.execute("SELECT key, quantity, micro_usd, utc, detail_json FROM meter_events "
                              "WHERE meter=? AND status='open' ORDER BY utc, key", (meter,)).fetchall()
        return [{"key": r["key"], "quantity": r["quantity"], "micro_usd": r["micro_usd"], "utc": r["utc"],
                 "detail": None if r["detail_json"] is None else json.loads(r["detail_json"])} for r in rows]

    def meter_total(self, meter: str, *, bucket: str | None = None) -> tuple[int, int]:
        sql = "SELECT COALESCE(SUM(quantity), 0), COALESCE(SUM(micro_usd), 0) FROM meter_events WHERE meter=?"
        args: tuple = (meter,)
        if bucket is not None:
            sql, args = sql + " AND bucket=?", (meter, bucket)
        q, m = self._one(sql, args)
        return int(q), int(m)

    def meter_max(self, meter: str) -> int:
        return int(self._one("SELECT MAX(micro_usd) FROM meter_events WHERE meter=? AND status='final'", (meter,))[0] or 0)

    def check_meter_cap(self, meter: str, *, extra_quantity: int = 0, extra_micro: int = 0, bucket: str = "") -> None:
        if meter not in METERS:
            raise ValueError(f"unknown meter {meter!r}")
        if not (_is_int(extra_quantity) and extra_quantity >= 0 and _is_int(extra_micro) and extra_micro >= 0):
            raise ValueError("meter cap extras must be non-negative integers")
        b = self.budgets
        if meter == "lens_spans":
            return                                 # no cap: reported by status only
        if meter == "lens_bytes":
            if bucket not in LENS_BUCKETS:
                raise ValueError(f"lens_bytes needs a bucket in {LENS_BUCKETS}")
            if self.meter_total(meter, bucket=bucket)[0] + extra_quantity > int(b["lens_ingest_split_mb"][bucket]) * MIB:
                raise MeterCapExceeded(f"meter cap exceeded: lens_bytes (bucket {bucket})", meter=meter, bucket=bucket)
            if self.meter_total(meter)[0] + extra_quantity > int(b["lens_ingest_mb"]) * MIB:
                raise MeterCapExceeded(f"meter cap exceeded: lens_bytes (total, from bucket {bucket})",
                                       meter=meter, bucket=bucket)
            return
        quantity, micro = self.meter_total(meter)
        if meter == "sandbox_seconds":
            over = micro + extra_micro > util.micros(b["sandbox_usd"])
        elif meter == "proposer_list_price":
            cap = b.get("proposer_list_usd")
            over = cap is None or micro + extra_micro > util.micros(cap)
        elif meter == "wandb_upload_bytes":
            over = quantity + extra_quantity > int(b["wandb_upload_gb"]) * 2 ** 30
        else:                                      # eval_tables
            over = quantity + extra_quantity > int(b["eval_tables_max"])
        if over:
            raise MeterCapExceeded(meter=meter, bucket=None)

    # ------------------------------------------------------------ reports

    def snapshot(self) -> dict:
        with self._tx() as db:
            by_status = {r["status"]: r for r in db.execute(
                "SELECT status, COUNT(*) AS n, COALESCE(SUM(reserved_micro), 0) AS reserved, "
                "COALESCE(SUM(spent_micro), 0) AS spent FROM calls GROUP BY status")}
            limit_exceeded = db.execute("SELECT COUNT(*) FROM calls WHERE reason LIKE 'limit_exceeded%'").fetchone()[0]
            committed = {r["phase"]: int(r["committed_micro"]) for r in db.execute("SELECT * FROM phase_totals")}
            meters = {m: {"quantity": 0, "micro_usd": 0, "open_rows": 0} for m in METERS}
            for r in db.execute("SELECT meter, COALESCE(SUM(quantity), 0) AS q, COALESCE(SUM(micro_usd), 0) AS m, "
                                "SUM(status='open') AS o FROM meter_events GROUP BY meter"):
                meters[r["meter"]] = {"quantity": int(r["q"]), "micro_usd": int(r["m"]), "open_rows": int(r["o"] or 0)}
            halt = db.execute("SELECT reason, detail, set_utc, call_id FROM halt WHERE id=1").fetchone()

        def get(status: str, field: str) -> int:
            return int(by_status[status][field]) if status in by_status else 0

        return {"observed_micro": get("complete", "spent") + get("reconciled", "spent"),
                "bounded_unknown_micro": get("bounded_unknown", "reserved"),
                "in_flight_micro": get("pending", "reserved"),
                "committed_by_phase": committed,
                "caps_micro": dict(self.caps),
                "requests": sum(int(r["n"]) for r in by_status.values()),
                "unknown_requests": get("unknown", "n"),
                "bounded_unknown_requests": get("bounded_unknown", "n"),
                "reconciled_requests": get("reconciled", "n"),
                "limit_exceeded_requests": int(limit_exceeded),
                "halt": None if halt is None else asdict(HaltState(halt["reason"], halt["detail"], halt["set_utc"],
                                                                   halt["call_id"])),
                "meters": meters}

    def verify_totals(self) -> list[str]:
        problems: list[str] = []
        sums = "COALESCE(SUM(CASE WHEN status != 'rejected' THEN COALESCE(spent_micro, reserved_micro) ELSE 0 END), 0)"
        with self._tx() as db:
            phases = {r["phase"]: int(r["committed_micro"]) for r in db.execute("SELECT * FROM phase_totals")}
            actual = {r[0]: int(r[1]) for r in db.execute(f"SELECT phase, {sums} FROM calls GROUP BY phase")}
            jobs = {r["job_key"]: r for r in db.execute("SELECT * FROM job_counters")}
            per_job = {r[0]: (int(r[1]), int(r[2])) for r in db.execute(
                f"SELECT job_key, {sums}, SUM(status != 'rejected') FROM calls GROUP BY job_key")}
        for phase in LEDGER_PHASES:
            if phase not in phases:
                problems.append(f"phase_totals row missing: {phase}")
        for phase in sorted(set(phases) | set(actual)):
            if phases.get(phase, 0) != actual.get(phase, 0):
                problems.append(f"phase {phase}: running total {phases.get(phase, 0)} != sum of calls {actual.get(phase, 0)}")
        for key in sorted(set(jobs) | set(per_job)):
            committed, charged = per_job.get(key, (0, 0))
            row = jobs.get(key)
            if row is None:
                problems.append(f"job {key}: calls without a job_counters row")
                continue
            if int(row["committed_micro"]) != committed:
                problems.append(f"job {key}: committed {row['committed_micro']} != sum of calls {committed}")
            if int(row["charged_attempts"]) != charged:
                problems.append(f"job {key}: charged_attempts {row['charged_attempts']} != non-rejected rows {charged}")
        return problems
