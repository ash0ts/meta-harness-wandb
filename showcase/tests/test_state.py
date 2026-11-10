# SPDX-License-Identifier: MIT
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from mhforge import util
from mhforge.errors import StateLockError
from mhforge.paths import StatePaths
from mhforge.state import StateLock, status
from tests import helpers

# the ledger DDL of spec 2.10, as text (state.py reads it by SQL and never imports ledger.py)
LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY, job_key TEXT NOT NULL, identity_json TEXT NOT NULL, token_sha256 TEXT,
  registered_utc TEXT NOT NULL, closed_utc TEXT, close_reason TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_token ON jobs(token_sha256) WHERE token_sha256 IS NOT NULL;
CREATE TABLE IF NOT EXISTS calls (
  id TEXT PRIMARY KEY, call_id TEXT NOT NULL, n INTEGER NOT NULL CHECK (n >= 1), job_id TEXT NOT NULL,
  job_key TEXT NOT NULL,
  phase TEXT NOT NULL CHECK (phase IN ('qualification','calibration','baseline','search','test','controls','recovery')),
  origin_phase TEXT NOT NULL, model TEXT NOT NULL, input_limit INTEGER NOT NULL CHECK (input_limit > 0),
  output_limit INTEGER NOT NULL CHECK (output_limit > 0), reserved_micro INTEGER NOT NULL CHECK (reserved_micro >= 0),
  spent_micro INTEGER,
  status TEXT NOT NULL CHECK (status IN ('pending','complete','rejected','bounded_unknown','unknown','reconciled')),
  usage_json TEXT, reason TEXT, created_utc TEXT NOT NULL, settled_utc TEXT);
CREATE INDEX IF NOT EXISTS calls_status ON calls(status);
CREATE INDEX IF NOT EXISTS calls_job ON calls(job_key);
CREATE INDEX IF NOT EXISTS calls_call ON calls(call_id);
CREATE TABLE IF NOT EXISTS reconcile_history (
  ledger_id TEXT NOT NULL, old_status TEXT NOT NULL, spent_micro INTEGER NOT NULL,
  below_reservation INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL, utc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS phase_totals (
  phase TEXT PRIMARY KEY, committed_micro INTEGER NOT NULL DEFAULT 0 CHECK (committed_micro >= 0));
CREATE TABLE IF NOT EXISTS job_counters (
  job_key TEXT PRIMARY KEY, job_id TEXT NOT NULL, requests INTEGER NOT NULL DEFAULT 0,
  charged_attempts INTEGER NOT NULL DEFAULT 0, committed_micro INTEGER NOT NULL DEFAULT 0,
  prior_charged INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS failed_requests (
  job_key TEXT NOT NULL, request_sha256 TEXT NOT NULL, exhausted INTEGER NOT NULL DEFAULT 0,
  peer_ok INTEGER NOT NULL DEFAULT 0, last_job_id TEXT NOT NULL, last_call_id TEXT NOT NULL, utc TEXT NOT NULL,
  PRIMARY KEY (job_key, request_sha256));
CREATE TABLE IF NOT EXISTS halt (
  id INTEGER PRIMARY KEY CHECK (id = 1), reason TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', call_id TEXT,
  set_utc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS halt_history (
  set_utc TEXT NOT NULL, reason TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', cleared_utc TEXT, cleared_note TEXT);
CREATE TABLE IF NOT EXISTS meter_events (
  meter TEXT NOT NULL CHECK (meter IN ('sandbox_seconds','lens_bytes','lens_spans','proposer_list_price',
                                       'wandb_upload_bytes','eval_tables')),
  key TEXT NOT NULL, bucket TEXT NOT NULL DEFAULT '', status TEXT NOT NULL CHECK (status IN ('open','final')),
  quantity INTEGER NOT NULL CHECK (quantity >= 0), unit TEXT NOT NULL, micro_usd INTEGER, detail_json TEXT,
  utc TEXT NOT NULL, closed_utc TEXT, PRIMARY KEY (meter, key));
"""
PHASES = ("qualification", "calibration", "baseline", "search", "test", "controls", "recovery")
UTC = "2026-11-10T00:00:00.000000Z"
MIB = 1048576

WRITER_SCRIPT = r"""
import sqlite3, sys, time
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("BEGIN IMMEDIATE")
for i in range(3):
    conn.execute("INSERT INTO calls (id, call_id, n, job_id, job_key, phase, origin_phase, model, input_limit, output_limit,"
                 " reserved_micro, spent_micro, status, created_utc) VALUES (?, 'c', 1, 'j.a1', 'j', 'search', 'search',"
                 " 'm', 10, 10, 100, 40, 'complete', 'u')", ("wal-%d" % i,))
conn.execute("COMMIT")
print("ready", flush=True)
time.sleep(60)
"""

HOLDER_SCRIPT = r"""
import sys
from pathlib import Path
from mhforge.state import StateLock
with StateLock(Path(sys.argv[1]), sys.argv[2]):
    print("ready", flush=True)
    sys.stdin.read()
"""


def make_ledger(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(LEDGER_DDL)
    for p in PHASES:
        conn.execute("INSERT INTO phase_totals (phase, committed_micro) VALUES (?, 0)", (p,))
    return conn


def add_call(conn, ledger_id, status, reserved, spent, phase="baseline"):
    conn.execute("INSERT INTO calls (id, call_id, n, job_id, job_key, phase, origin_phase, model, input_limit, output_limit,"
                 " reserved_micro, spent_micro, status, created_utc) VALUES (?, 'c-1', 1, 'val.x.USPTO.a1', 'val.x.USPTO',"
                 " ?, ?, 'openai/gpt-oss-120b', 100, 100, ?, ?, ?, ?)", (ledger_id, phase, phase, reserved, spent, status, UTC))


def add_meter(conn, meter, key, quantity, unit, micro=None, status="final", bucket=""):
    conn.execute("INSERT INTO meter_events (meter, key, bucket, status, quantity, unit, micro_usd, utc)"
                 " VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (meter, key, bucket, status, quantity, unit, micro, UTC))


def journal(paths, *rows):
    for i, row in enumerate(rows, 1):
        util.append_jsonl(paths.journal, dict({"schema": "mhforge.journal/1", "seq": i, "utc": UTC, "pid": 1}, **row))


def snapshot(root: Path) -> dict:
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            st = p.stat()
            out[str(p.relative_to(root))] = (st.st_mtime_ns, st.st_size)
    out["."] = (root.stat().st_mtime_ns, 0)
    return out


def populate(paths: StatePaths) -> None:
    """A state directory with something in every file status reads; the ledger is left idle in WAL mode."""
    conn = make_ledger(paths.ledger)
    add_call(conn, "l-1", "complete", 6718, 6480)
    add_call(conn, "l-2", "pending", 6718, None)
    add_call(conn, "l-3", "bounded_unknown", 5000, None, "search")
    add_call(conn, "l-4", "unknown", 6718, None, "search")
    add_call(conn, "l-5", "reconciled", 6718, 7000, "search")
    add_call(conn, "l-6", "rejected", 6718, 0)
    conn.execute("UPDATE phase_totals SET committed_micro = 13198 WHERE phase = 'baseline'")
    conn.execute("UPDATE phase_totals SET committed_micro = 18718 WHERE phase = 'search'")
    conn.execute("INSERT INTO halt (id, reason, detail, call_id, set_utc) VALUES (1, 'budget:search', 'cap', 'c-1', ?)",
                 (UTC,))
    add_meter(conn, "sandbox_seconds", "sbx-1", 412500, "ms", 39059)
    add_meter(conn, "sandbox_seconds", "sbx-2", 900000, "ms", 50000, status="open")
    add_meter(conn, "lens_bytes", "job:a#e1", 3 * MIB, "bytes", bucket="main")
    add_meter(conn, "lens_bytes", "job:b#e1", MIB // 2, "bytes", bucket="preflight")
    add_meter(conn, "lens_bytes", "job:b#e2", MIB, "bytes", bucket="recovery")
    add_meter(conn, "lens_spans", "job:a#e1", 51, "spans", bucket="main")
    add_meter(conn, "wandb_upload_bytes", "eval:val.x#e1", MIB, "bytes")
    add_meter(conn, "eval_tables", "eval:val.x#e1#val/USPTO", 1, "tables")
    add_meter(conn, "proposer_list_price", "iter001.s1", 12310000, "micro_usd", 12310000)
    add_meter(conn, "proposer_list_price", "iter002.s1", 25000000, "micro_usd", 25000000, status="open")
    conn.close()
    assert not Path(str(paths.ledger) + "-wal").exists()
    paths.runner_lock.write_text(json.dumps({"pid": 1, "command": "search"}))
    util.write_json(paths.runtime, {"schema": "mhforge.runtime/1", "pid": 4711, "command": "search",
                                    "started_utc": UTC, "key_source": "env",
                                    "key_scrubbed": {"MHFORGE_FORGE_API_KEY": False, "MHFORGE_SANDBOX_API_KEY": True}})
    paths.manifest_sha256.write_text("a" * 64 + "\n")
    util.write_json(paths.qualification_passed, {"manifest_sha256": "a" * 64, "engine_sha256": "e" * 64})
    util.write_json(paths.stopped, {"schema": "mhforge.stop/1", "reason": "budget:search", "detail": "", "phase": "search",
                                    "at_utc": UTC, "exit_code": 2})
    util.write_json(paths.circuit, {"failures": 2, "until_epoch": 5.5})
    for key, st, iso in (("val.no_memory.USPTO", "succeeded", True), ("val.fewshot_all.USPTO", "failed", False),
                         ("val.no_memory.LawBench", "skipped_usable", True)):
        util.write_json(paths.job_dir(key) / "job.json", {"schema": "mhforge.job/1", "job_key": key, "status": st,
                                                          "isolated": iso})
    journal(paths,
            {"op": "job", "key": "val.no_memory.USPTO", "status": "started", "attempt": 1},
            {"op": "job", "key": "val.no_memory.USPTO", "status": "complete", "attempt": 1},
            {"op": "job", "key": "val.fewshot_all.USPTO", "status": "started", "attempt": 1},
            {"op": "job", "key": "val.fewshot_all.USPTO", "status": "failed", "attempt": 1},
            {"op": "job", "key": "val.fewshot_all.Symptom2Disease", "status": "started", "attempt": 1},
            {"op": "benchmark", "key": "val.no_memory", "status": "complete"},
            {"op": "iteration", "key": "iter000", "status": "settled", "detail": {"kind": "baselines"}},
            {"op": "proposer", "key": "iter001", "status": "complete", "attempt": 1,
             "detail": {"outcome": "ok", "candidates": [{"name": "cand_a", "sha256": "1" * 64}]}},
            {"op": "iteration", "key": "iter001", "status": "settled", "detail": {"kind": "scored"}})
    receipts = paths.export_root / "receipts"
    for unit, surface, st, stale in (("job:val.no_memory.USPTO.a1", "agent_lens", "verified", None),
                                     ("job:val.no_memory.USPTO.a1", "wandb", "verified", "b" * 64),
                                     ("eval:val.no_memory", "wandb", "partial", None),
                                     ("job:val.fewshot_all.USPTO.a1", "agent_lens", "emitting", None),
                                     ("proposer:iter001.s1", "agent_lens", "skipped_budget", None),
                                     ("loop:iter000", "agent_lens", "refused_secret", None),
                                     ("loop:iter001", "agent_lens", "dry_run", "c" * 64)):
        util.write_json(receipts / f"{unit.replace(':', '__')}.{surface}.json",
                        {"schema": "mhforge.receipt/1", "unit": unit, "surface": surface, "status": st,
                         "stale_plan_sha256": stale})
    for n in (1, 2):
        util.append_jsonl(paths.amendments, {"schema": "mhforge.amendment/1", "seq": n})


class LockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mhf-lock-")
        self.lock = Path(self.tmp.name) / "state" / "runner.lock"

    def tearDown(self):
        self.tmp.cleanup()

    def _holder(self, command="search"):
        child = subprocess.Popen([sys.executable, "-c", HOLDER_SCRIPT, str(self.lock), command], stdin=subprocess.PIPE,
                                 stdout=subprocess.PIPE, env=helpers.child_env(), cwd=self.tmp.name)
        self.assertEqual(child.stdout.readline().strip(), b"ready")
        return child

    def test_second_runner_gets_lock_error_with_holder(self):
        child = self._holder("search")
        try:
            with self.assertRaises(StateLockError) as ctx:
                with StateLock(self.lock, "evaluate"):
                    self.fail("acquired a held lock")
            holder = ctx.exception.holder
            self.assertEqual(holder["pid"], child.pid)
            self.assertEqual(holder["command"], "search")
            util.parse_utc(holder["started_utc"])
            self.assertIn(str(child.pid), str(ctx.exception))
        finally:
            child.stdin.close()
            child.wait(timeout=10)
            child.stdout.close()
        with StateLock(self.lock, "evaluate", helpers.FakeClock()) as lock:
            body = json.loads(self.lock.read_text())
            self.assertEqual((body["pid"], body["command"], body["started_utc"]),
                             (os.getpid(), "evaluate", helpers.FakeClock().utc()))
            self.assertIn("host", body)
            with self.assertRaises(StateLockError):          # a second holder in the same process is refused too
                StateLock(self.lock, "export").__enter__()
        self.assertIsNone(lock._fd)

    def test_lock_released_when_holder_dies(self):
        child = self._holder("search")
        paths = StatePaths(self.lock.parent)
        view = status(helpers.make_manifest(), paths)["lock"]
        self.assertEqual((view["held"], view["pid"], view["command"]), (True, child.pid, "search"))
        child.send_signal(signal.SIGKILL)
        child.wait(timeout=10)
        child.stdin.close()
        child.stdout.close()
        self.assertFalse(status(helpers.make_manifest(), paths)["lock"]["held"])
        with StateLock(self.lock, "search"):
            pass

    def test_lock_never_follows_a_symlink(self):
        target = Path(self.tmp.name) / "precious.txt"
        target.write_text("precious data\n")
        self.lock.parent.mkdir(parents=True)
        os.symlink(target, self.lock)
        with self.assertRaises(OSError):
            with StateLock(self.lock, "search"):
                self.fail("locked through a symlink")
        self.assertEqual(target.read_text(), "precious data\n")

    def test_export_lock_creates_its_directory(self):
        lock = Path(self.tmp.name) / "state" / "export" / "export.lock"
        with StateLock(lock, "amend", host="h1"):
            self.assertEqual(json.loads(lock.read_text())["host"], "h1")


class StatusTest(unittest.TestCase):
    def test_status_without_state(self):
        with helpers.tmp_state(create=False) as st:
            out = status(helpers.make_manifest(), st.paths)
            self.assertFalse(out["state_exists"])
            self.assertEqual(out["schema"], "mhforge.status/1")
            self.assertEqual(out["campaign"], "mh-test")
            self.assertFalse(st.paths.root.exists())
            self.assertFalse((st.sc / "state").exists())

    def test_status_reports_ledger_and_journal(self):
        with helpers.tmp_state() as st:
            populate(st.paths)
            out = status(helpers.make_manifest(), st.paths, clock=helpers.FakeClock())
            s = out["solver"]
            self.assertEqual(s["observed_usd"], "0.013480")
            self.assertEqual(s["bounded_unknown_usd"], "0.005000")
            self.assertEqual(s["in_flight_usd"], "0.006718")
            self.assertEqual(s["committed_by_phase_usd"], {"baseline": "0.013198", "search": "0.018718"})
            self.assertEqual(s["caps_usd"]["baseline"], "3.00")
            self.assertEqual(s["caps_usd"]["total"], "60.00")
            self.assertEqual((s["requests"], s["unknown_requests"], s["bounded_unknown_requests"],
                              s["reconciled_requests"]), (6, 1, 1, 1))
            self.assertEqual(out["halt"], {"reason": "budget:search", "detail": "cap", "call_id": "c-1", "set_utc": UTC})
            self.assertEqual(out["ledger_view"], "current")
            mt = out["meters"]
            self.assertEqual(mt["sandbox_seconds"], 1312.5)
            self.assertEqual(mt["sandbox_usd"], "0.089059")
            self.assertEqual(mt["sandbox_open_rows"], 1)
            self.assertEqual(mt["lens_planned_mb"], 4.5)
            self.assertEqual(mt["lens_planned_spans"], 51)
            self.assertEqual(mt["wandb_upload_mb"], 1.0)
            self.assertEqual(mt["eval_tables"], 1)
            self.assertEqual(mt["proposer_list_usd"], "37.310000")
            self.assertEqual(mt["proposer_open_rows"], 1)
            self.assertEqual(out["jobs"], {"succeeded": 1, "failed": 1, "running": 1, "skipped_usable": 1, "not_isolated": 1})
            self.assertEqual(out["iterations"], {"settled": 2, "last": 1})
            self.assertEqual(out["proposer"], {"sessions": 1, "last_outcome": "ok"})
            self.assertEqual(out["qualification"], {"passed": True})
            self.assertEqual(out["stopped"]["reason"], "budget:search")
            self.assertEqual(out["circuit"], {"failures": 2, "until_epoch": 5.5})
            self.assertEqual(out["manifest_sha256"], "a" * 64)
            self.assertFalse(out["lock"]["held"])
            self.assertEqual(out["stall"], {"journal_age_s": 0.0, "alarm": False})
            json.dumps(out)

    def test_status_is_read_only(self):
        with helpers.tmp_state() as st:
            populate(st.paths)
            before = snapshot(st.paths.root)
            first = status(helpers.make_manifest(), st.paths, clock=helpers.FakeClock())
            self.assertEqual(snapshot(st.paths.root), before)
            copy_root = st.sc / "copy" / "mh-test"
            shutil.copytree(st.paths.root, copy_root)
            for dirpath, dirnames, filenames in os.walk(copy_root, topdown=False):
                for name in filenames:
                    os.chmod(Path(dirpath) / name, 0o444)
                os.chmod(dirpath, 0o555)
            try:
                ro = status(helpers.make_manifest(), StatePaths(copy_root), clock=helpers.FakeClock())
                self.assertEqual(ro, first)
                self.assertEqual(sorted(os.listdir(copy_root)), sorted(os.listdir(st.paths.root)))
            finally:
                for dirpath, dirnames, filenames in os.walk(copy_root):
                    os.chmod(dirpath, 0o755)
                    for name in filenames:
                        os.chmod(Path(dirpath) / name, 0o644)

    def test_status_creates_no_wal_or_shm_on_idle_ledger(self):
        with helpers.tmp_state() as st:
            conn = make_ledger(st.paths.ledger)
            add_call(conn, "l-1", "complete", 100, 90)
            conn.close()
            header = st.paths.ledger.read_bytes()[:20]
            self.assertEqual((header[18], header[19]), (2, 2))        # file format: WAL mode (read and write version 2)
            before = sorted(os.listdir(st.paths.root))
            out = status(helpers.make_manifest(), st.paths)
            self.assertEqual(sorted(os.listdir(st.paths.root)), before)
            self.assertFalse(Path(str(st.paths.ledger) + "-wal").exists())
            self.assertFalse(Path(str(st.paths.ledger) + "-shm").exists())
            self.assertEqual((out["solver"]["requests"], out["solver"]["observed_usd"], out["ledger_view"]),
                             (1, "0.000090", "current"))

    def test_status_after_killed_writer_sees_wal_rows(self):
        with helpers.tmp_state() as st:
            make_ledger(st.paths.ledger).close()
            child = subprocess.Popen([sys.executable, "-c", WRITER_SCRIPT, str(st.paths.ledger)], stdout=subprocess.PIPE,
                                     env=helpers.child_env())
            try:
                self.assertEqual(child.stdout.readline().strip(), b"ready")
                child.send_signal(signal.SIGKILL)
                child.wait(timeout=10)
            finally:
                child.stdout.close()
            self.assertTrue(Path(str(st.paths.ledger) + "-wal").exists())
            before = sorted(os.listdir(st.paths.root))
            out = status(helpers.make_manifest(), st.paths)
            self.assertEqual(out["solver"]["requests"], 3)
            self.assertEqual(out["solver"]["observed_usd"], "0.000120")
            self.assertIn(out["ledger_view"], ("current", "possibly_stale"))
            self.assertEqual(sorted(os.listdir(st.paths.root)), before)

    def test_status_reads_the_journal_through_the_marker_aware_reader(self):
        import hashlib
        from unittest import mock
        with helpers.tmp_state() as st:
            populate(st.paths)
            p = st.paths
            p.journal.unlink()
            torn = b'{"schema":"mhforge.journal/1","seq":2,"op":"jo'
            marker = {"schema": "mhforge.journal/1", "seq": 2, "utc": UTC, "pid": 1, "op": "journal", "key": "torn_tail",
                      "status": "terminated", "attempt": 0,
                      "detail": {"bytes": len(torn), "sha256": hashlib.sha256(torn).hexdigest()}}
            rows = [{"schema": "mhforge.journal/1", "seq": 1, "utc": UTC, "pid": 1, "op": "job", "key": "k",
                     "status": "started", "attempt": 1, "detail": {}}, marker,
                    {"schema": "mhforge.journal/1", "seq": 3, "utc": UTC, "pid": 1, "op": "iteration", "key": "iter002",
                     "status": "settled", "attempt": 0, "detail": {}}]
            p.journal.write_bytes(json.dumps(rows[0]).encode() + b"\n" + torn + b"\n"
                                  + b"".join(json.dumps(r).encode() + b"\n" for r in rows[1:]))
            with mock.patch.object(util, "read_marked_jsonl", wraps=util.read_marked_jsonl) as reader:
                out = status(helpers.make_manifest(), p, clock=helpers.FakeClock())
            reader.assert_called_once_with(p.journal)
            self.assertEqual(out["jobs"]["running"], 1)
            self.assertEqual(out["iterations"], {"settled": 1, "last": 2})

    def test_status_tolerates_half_written_files(self):
        with helpers.tmp_state() as st:
            populate(st.paths)
            p = st.paths
            p.runtime.write_text('{"pid": 47')
            (p.job_dir("val.no_memory.USPTO") / "job.json").write_text('{"schema": "mhforge.job/1", "sta')
            p.journal.write_text('{"op": "job", "key": "k", "status": "started", "attempt": 1, "utc": "' + UTC +
                                 '"}\n{bad line\n{"op": "iteration", "key": "iter004", "status": "settled"}\n{"op": "jo')
            p.amendments.write_text('{"seq": 1}\n{"seq": 2')
            p.stopped.write_text("")
            p.circuit.write_text("[1, 2")
            p.manifest_sha256.write_text("ab")
            p.qualification_passed.write_bytes(b"\xff\xfe")
            receipt = next((p.export_root / "receipts").iterdir())
            receipt.write_text('{"status": "verif')
            (p.export_root / "receipts" / "notes.txt").write_text("x")
            out = status(helpers.make_manifest(), p, clock=helpers.FakeClock())
            self.assertEqual(out["jobs"]["running"], 1)
            self.assertEqual(out["iterations"], {"settled": 1, "last": 4})
            self.assertEqual(out["amendments"], 1)
            self.assertIsNone(out["stopped"])
            self.assertIsNone(out["manifest_sha256"])
            self.assertEqual(out["runtime"]["pid"], None)
            self.assertFalse(out["qualification"]["passed"])
            p.ledger.write_bytes(b"not a database" * 100)
            out = status(helpers.make_manifest(), p, clock=helpers.FakeClock())
            self.assertEqual(out["ledger_view"], "unreadable")
            self.assertEqual(out["solver"]["requests"], 0)
            json.dumps(out)

    def test_status_marks_not_isolated_jobs(self):
        with helpers.tmp_state() as st:
            for key, iso in (("val.a.USPTO", False), ("val.b.USPTO", False), ("val.c.USPTO", True)):
                util.write_json(st.paths.job_dir(key) / "job.json", {"status": "succeeded", "isolated": iso})
            jobs = status(helpers.make_manifest(), st.paths)["jobs"]
            self.assertEqual((jobs["succeeded"], jobs["not_isolated"]), (3, 2))

    def test_status_shows_key_scrubbed_and_stale_exports(self):
        with helpers.tmp_state() as st:
            populate(st.paths)
            out = status(helpers.make_manifest(), st.paths)
            self.assertEqual(out["runtime"], {"pid": 4711, "command": "search", "key_source": "env",
                                              "key_scrubbed": {"MHFORGE_FORGE_API_KEY": False,
                                                               "MHFORGE_SANDBOX_API_KEY": True}})
            # receipts: 2 verified (one stale), 1 partial, 1 emitting, 1 skipped, 1 refused, 1 dry run (stale);
            # pending = the emitting receipt + the one ready unit with no receipt at all (propose:cand_a)
            self.assertEqual(out["export"], {"verified": 2, "pending": 2, "partial": 1, "failed": 0, "refused_secret": 1,
                                             "skipped": 1, "dry_run": 1, "stale": 2})

    def test_status_stall_alarm(self):
        clock = helpers.FakeClock()
        m = helpers.make_manifest()
        with helpers.tmp_state() as st:
            journal(st.paths, {"op": "run", "key": "search", "status": "started", "utc": clock.utc()})
            clock.advance(10801)
            self.assertEqual(status(m, st.paths, clock=clock)["stall"], {"journal_age_s": 10801.0, "alarm": False})
            with StateLock(st.paths.runner_lock, "search", clock):
                self.assertEqual(status(m, st.paths, clock=clock)["stall"], {"journal_age_s": 10801.0, "alarm": True})
                quiet = helpers.make_manifest(**{"runner.stall_alarm_s": 20000})
                self.assertFalse(status(quiet, st.paths, clock=clock)["stall"]["alarm"])
                journal(st.paths, {"op": "job", "key": "k", "status": "started", "attempt": 1, "utc": clock.utc()})
                self.assertEqual(status(m, st.paths, clock=clock)["stall"], {"journal_age_s": 0.0, "alarm": False})

    def test_status_shows_key_source_buckets_and_amendments(self):
        with helpers.tmp_state() as st:
            populate(st.paths)
            util.write_json(st.paths.runtime, {"pid": 5, "command": "evaluate", "key_source": "prompt",
                                               "key_scrubbed": {"MHFORGE_FORGE_API_KEY": None}})
            out = status(helpers.make_manifest(), st.paths)
            self.assertEqual(out["runtime"]["key_source"], "prompt")
            self.assertEqual(out["runtime"]["key_scrubbed"], {"MHFORGE_FORGE_API_KEY": None})
            self.assertEqual(out["meters"]["lens_planned_mb_by_bucket"], {"main": 3.0, "preflight": 0.5, "recovery": 1.0})
            self.assertEqual(out["amendments"], 2)
            self.assertEqual(out["campaign_kind"], "measured")

    def test_status_source_reads_no_environment_and_opens_no_writer(self):
        src = Path(sys.modules[status.__module__].__file__).read_text()
        for forbidden in ("os.environ", "os.getenv", "sqlite3.connect", "gethostname"):
            self.assertFalse(forbidden in src, forbidden)


if __name__ == "__main__":
    unittest.main()
