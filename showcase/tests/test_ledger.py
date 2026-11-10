# SPDX-License-Identifier: Apache-2.0
# Adapted from ash0ts/harness2-wandb@31b0f82 tests/showcase/test_budget.py, test_study_v4.py L61-L127 and test_safety.py
# L13-L23. Changes: rewritten for unittest against the per-job ceilings, phase totals, halt flag, job registry, meters
# and reconcile of spec 3.6; new cases for every contract of 3.6 and 2.10.
import hashlib
import json
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from mhforge import ledger as ledger_mod
from mhforge import wire
from mhforge.errors import BudgetError, MeterCapExceeded, StateError
from mhforge.ledger import (HaltState, Ledger, Settlement, cost_micro, prices_from_manifest, reservation_micro,
                            usage_within_limits)
from mhforge.wire import LEDGER_PHASES, METERS, JobIdentity
from tests.helpers import SC, FakeClock, SeqIds, child_env, make_manifest

MODEL = "openai/gpt-oss-120b"
PRICES = {"input": Decimal("0.03"), "cached": Decimal("0.03"), "output": Decimal("0.17")}
MIB = 1048576


def J(attempt=1, phase="baseline", candidate="no_memory", dataset="USPTO", split="val", iteration=None, repeat=None):
    if phase == "search" and iteration is None:
        iteration = 1
    return JobIdentity("mh-test", split, candidate, dataset, attempt, phase, iteration, repeat)


def U(prompt=100, completion=50, cached=None, **extra):
    usage = {"prompt_tokens": prompt, "completion_tokens": completion}
    if prompt is not None and completion is not None and isinstance(prompt, int) and isinstance(completion, int):
        usage["total_tokens"] = prompt + completion
    usage = {k: v for k, v in usage.items() if v is not None}
    if cached is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    usage.update(extra)
    return usage


class LedgerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="mhf-ledger-")
        self.dir = Path(self._tmp.name)
        self.clock = FakeClock()
        self.ids = SeqIds(self.clock)
        self.n = 0

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, name="ledger.sqlite", **overrides):
        self.manifest = make_manifest(overrides)
        self.path = self.dir / name
        return Ledger(self.path, self.manifest, clock=self.clock, ids=self.ids)

    def res(self, led, job=None, *, n=1, phase=None, inp=1000, out=1000):
        job = job or J()
        return led.reserve(call_id=self.ids.call_id(), n=n, job=job, phase=phase or job.phase, model=MODEL,
                           input_limit=inp, output_limit=out)

    def rows(self, sql, args=()):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(sql, args)]
        finally:
            conn.close()

    def kind(self, ctx):
        return ctx.exception.kind


class TestMoney(LedgerCase):
    def test_reservation_formula_and_rounding(self):
        self.assertEqual(reservation_micro(PRICES, 131072, 16384), 6718)   # R of 2.3: $0.006718
        self.assertEqual(reservation_micro(PRICES, 1, 1), 1)               # 0.2 micro rounds up
        self.assertEqual(reservation_micro(PRICES, 1000, 1000), 200)
        self.assertIsInstance(reservation_micro(PRICES, 7, 3), int)
        cheap_cache = dict(PRICES, cached=Decimal("0.015"))
        self.assertEqual(cost_micro(cheap_cache, U(1000, 10, cached=400)), 26)   # 18 + 6 + 1.7 -> 26
        self.assertEqual(cost_micro(PRICES, U(100, 50)), 12)                     # 3 + 8.5 -> 12
        led = self.make()
        self.assertEqual(prices_from_manifest(self.manifest), PRICES)
        lid = self.res(led, inp=131072, out=16384)
        self.assertEqual(self.rows("SELECT reserved_micro FROM calls WHERE id=?", (lid,))[0]["reserved_micro"], 6718)
        self.assertEqual(led.committed("baseline"), 6718)

    def test_usage_within_limits_ported(self):
        self.assertTrue(usage_within_limits(None, 10, 10))
        self.assertTrue(usage_within_limits({"prompt_tokens": 10, "completion_tokens": 10}, 10, 10))
        self.assertFalse(usage_within_limits({"prompt_tokens": 11}, 10, 10))
        self.assertFalse(usage_within_limits({"prompt_tokens": 5, "prompt_tokens_details": {"cached_tokens": 6}}, 10, 10))
        self.assertFalse(usage_within_limits({"completion_tokens": True}, 10, 10))
        self.assertFalse(usage_within_limits([], 10, 10))


class TestReserve(LedgerCase):
    def test_concurrent_reservations_one_fits(self):
        led = self.make(**{"budgets.solver_usd.phases.baseline": "0.000300"})   # 200 fits once, 400 does not
        barrier = threading.Barrier(20)
        results = []

        def worker(i):
            other = Ledger(self.path, self.manifest, clock=self.clock, ids=self.ids)
            barrier.wait()
            try:
                results.append(self.res(other, J(dataset=f"D{i}")))
            except BudgetError as exc:
                results.append(exc.kind)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sum(1 for r in results if r.startswith("l-")), 1)
        self.assertEqual(sorted(r for r in results if not r.startswith("l-")), ["cap"] * 19)
        self.assertEqual(led.committed("baseline"), 200)
        self.assertEqual(led.verify_totals(), [])

    def test_phase_cap_and_total_cap_are_distinct_errors(self):
        led = self.make(**{"budgets.solver_usd.total": "0.000500", "budgets.solver_usd.phases.baseline": "0.000300",
                           "budgets.solver_usd.phases.search": "1.00"})
        self.res(led)
        with self.assertRaises(BudgetError) as cap:
            self.res(led)
        self.assertEqual((cap.exception.kind, cap.exception.phase), ("cap", "baseline"))
        self.res(led, J(phase="search"))
        with self.assertRaises(BudgetError) as total:
            self.res(led, J(phase="search"))
        self.assertEqual((total.exception.kind, total.exception.phase), ("total", "search"))
        self.assertIsNone(led.halted())
        self.assertEqual(led.committed(), 400)

    def test_invalid_reservations(self):
        led = self.make()
        for kw in ({"model": "other/model"}, {"phase": "bogus"}, {"input_limit": 0}, {"input_limit": 131073},
                   {"output_limit": 16385}, {"output_limit": True}, {"n": 0}):
            args = dict(call_id="c-1", n=1, job=J(), phase="baseline", model=MODEL, input_limit=10, output_limit=10)
            args.update(kw)
            with self.subTest(kw=kw), self.assertRaises(BudgetError) as ctx:
                led.reserve(**args)
            self.assertEqual(ctx.exception.kind, "invalid")
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM calls")[0]["n"], 0)

    def test_seven_phase_rows_and_new_phases_reserve(self):
        led = self.make()
        self.assertEqual(sorted(r["phase"] for r in self.rows("SELECT phase FROM phase_totals")), sorted(LEDGER_PHASES))
        self.assertEqual(len(LEDGER_PHASES), 7)
        for phase in ("calibration", "controls", "qualification", "test"):
            self.res(led, J(phase=phase, split="test"))
        lid = self.res(led, J(attempt=2, phase="search"), n=2, phase="recovery")
        row = self.rows("SELECT phase, origin_phase FROM calls WHERE id=?", (lid,))[0]
        self.assertEqual(row, {"phase": "recovery", "origin_phase": "search"})
        for phase in ("calibration", "controls", "qualification", "test", "recovery"):
            self.assertEqual(led.committed(phase), 200)
        # LD2: the CHECK lists of the DDL are the wire tuples
        sql = " ".join(r["sql"] for r in self.rows("SELECT sql FROM sqlite_master WHERE type='table'"))
        phases = re.search(r"phase IN \(([^)]*)\)", sql).group(1)
        meters = re.search(r"meter IN \(([^)]*)\)", sql).group(1)
        self.assertEqual(tuple(re.findall(r"'([a-z_]+)'", phases)), LEDGER_PHASES)
        self.assertEqual(tuple(re.findall(r"'([a-z_]+)'", meters)), METERS)

    def test_phase_totals_rows_exist_at_creation_and_missing_row_raises(self):
        led = self.make()
        conn = sqlite3.connect(self.path)
        conn.execute("DELETE FROM phase_totals WHERE phase='search'")
        conn.commit()
        conn.close()
        reopened = Ledger(self.path, self.manifest, clock=self.clock, ids=self.ids)   # a reopen does not heal it
        for which in (led, reopened):
            with self.assertRaises(StateError):
                self.res(which, J(phase="search"))
        self.assertIn("phase_totals row missing: search", reopened.verify_totals())
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM calls")[0]["n"], 0)
        self.res(led)                              # other phases are unaffected

    def test_connections_set_synchronous_full(self):
        seen = []
        real = ledger_mod.connect

        def spy(path):
            conn = real(path)
            seen.append((conn.execute("PRAGMA synchronous").fetchone()[0], conn.execute("PRAGMA fullfsync").fetchone()[0]))
            return conn

        with mock.patch.object(ledger_mod, "connect", spy):
            led = self.make()
            lid = self.res(led)
            led.settle(lid, U())
            led.meter("eval_tables", "u#e1#t", 1, "tables")
            led.snapshot()
        self.assertGreaterEqual(len(seen), 5)
        expect_full = 1 if sys.platform == "darwin" else 0
        for synchronous, fullfsync in seen:
            self.assertEqual(synchronous, 2)       # FULL
            self.assertEqual(fullfsync, expect_full)
        self.assertEqual(self.rows("PRAGMA journal_mode")[0]["journal_mode"], "wal")

    def test_meta_rows_and_campaign_check(self):
        self.make()
        meta = {r["key"]: r["value"] for r in self.rows("SELECT * FROM meta")}
        self.assertEqual(set(meta), {"schema_version", "campaign", "manifest_sha256", "created_utc"})
        self.assertEqual(meta["campaign"], "mh-test")
        with self.assertRaises(StateError):
            Ledger(self.path, make_manifest(campaign="mh-other"), clock=self.clock)


class TestCeilings(LedgerCase):
    def test_job_ceiling_counts_all_attempts_and_does_not_halt(self):
        led = self.make(**{"gateway.max_requests_per_job": 2})          # hard bound: 4 charged attempts
        a1 = J()
        led.register_job(a1)
        for n in (1, 2, 3, 4):                     # one worker request, four provider attempts
            self.res(led, a1, n=n, phase="baseline" if n == 1 else "recovery")
        with self.assertRaises(BudgetError) as ctx:
            self.res(led, a1)
        self.assertEqual((ctx.exception.kind, ctx.exception.detail), ("ceiling", "attempts"))
        a2 = J(attempt=2)
        led.register_job(a2)
        with self.assertRaises(BudgetError) as ctx:
            self.res(led, a2, phase="recovery")
        self.assertEqual(ctx.exception.detail, "attempts")
        self.assertIsNone(led.halted())
        self.res(led, J(dataset="LawBench"))       # another job key is not affected

    def test_rejected_attempts_do_not_count_toward_ceiling(self):
        led = self.make(**{"gateway.max_requests_per_job": 2})
        job = J()
        led.register_job(job)
        for n in range(1, 11):                     # ten provider attempts, every one rejected (zero cost)
            led.settle(self.res(led, job, n=n, phase="baseline" if n == 1 else "recovery"), None, rejection="provider_429")
        self.assertEqual(self.rows("SELECT charged_attempts FROM job_counters")[0]["charged_attempts"], 0)
        self.assertEqual(led.job_requests(job.job_key), 1)               # only n == 1 counts as a request
        self.res(led, job)                         # the ceiling was not used up
        self.assertEqual(led.verify_totals(), [])

    def test_ceiling_resets_per_job_attempt_hard_bound_does_not(self):
        led = self.make(**{"gateway.max_requests_per_job": 2})
        a1 = J()
        led.register_job(a1)
        self.res(led, a1)
        self.res(led, a1)
        with self.assertRaises(BudgetError) as ctx:
            self.res(led, a1)
        self.assertEqual(ctx.exception.detail, "requests")
        self.assertEqual(led.job_requests(a1.job_key), 2)
        a2 = J(attempt=2)
        led.register_job(a2)
        self.assertEqual(led.job_requests(a2.job_key), 0)
        self.res(led, a2, phase="recovery")
        self.res(led, a2, phase="recovery")
        a3 = J(attempt=3)
        led.register_job(a3)
        with self.assertRaises(BudgetError) as ctx:
            self.res(led, a3, phase="recovery")
        self.assertEqual(ctx.exception.detail, "attempts")
        led.register_job(a2)                       # registering an older attempt again changes nothing
        self.assertEqual(led.job_requests(a3.job_key), 0)

    def test_job_usd_cap(self):
        led = self.make(**{"gateway.max_job_usd": "0.000500"})
        job = J()
        first = self.res(led, job)
        self.res(led, job)
        with self.assertRaises(BudgetError) as ctx:
            self.res(led, job)
        self.assertEqual((ctx.exception.kind, ctx.exception.detail), ("ceiling", "job_usd"))
        led.settle(first, U(100, 50))              # 200 reserved -> 12 spent: 212 committed
        self.res(led, job)
        led.register_job(J(attempt=2))             # the bound holds over all attempts together
        with self.assertRaises(BudgetError):
            self.res(led, J(attempt=2), phase="recovery")
        self.res(led, J(dataset="LawBench"))
        self.assertIsNone(led.halted())


class TestSettle(LedgerCase):
    def test_settle_table(self):
        cases = [
            ("rejected", dict(usage=None, rejection="provider_rejected"), Settlement("rejected", 0, "none"), 0),
            ("complete", dict(usage=U(100, 50)), Settlement("complete", 12, "usage"), 12),
            ("limit", dict(usage=U(1500, 10)), Settlement("complete", 47, "usage"), 47),
            ("missing", dict(usage=None), Settlement("bounded_unknown", None, "reservation"), 200),
            ("partial", dict(usage={"prompt_tokens": 5}), Settlement("bounded_unknown", None, "reservation"), 200),
            ("negative", dict(usage=U(-1, 5)), Settlement("unknown", None, "reservation"), 200),
            ("float", dict(usage=U(1.5, 5)), Settlement("unknown", None, "reservation"), 200),
            ("bool", dict(usage=U(True, 5)), Settlement("unknown", None, "reservation"), 200),
            ("cached", dict(usage=U(10, 5, cached=11)), Settlement("unknown", None, "reservation"), 200),
            ("not-a-dict", dict(usage=["x"]), Settlement("unknown", None, "reservation"), 200),
        ]
        for i, (name, kwargs, expected, committed) in enumerate(cases):
            with self.subTest(name):
                led = self.make(f"l{i}.sqlite")
                lid = self.res(led)
                usage = kwargs.pop("usage")
                self.assertEqual(led.settle(lid, usage, **kwargs), expected)
                row = self.rows("SELECT * FROM calls WHERE id=?", (lid,))[0]
                self.assertEqual((row["status"], row["spent_micro"]), (expected.status, expected.spent_micro))
                self.assertEqual(led.committed("baseline"), committed)
                self.assertEqual(led.verify_totals(), [])
                self.assertEqual(led.halted() is not None, expected.status == "unknown")
                if name == "limit":
                    self.assertEqual(row["reason"], "limit_exceeded")
                if usage is not None:
                    self.assertEqual(json.loads(row["usage_json"]), usage)

    def test_contradictory_usage_halts_in_same_transaction(self):
        led = self.make()
        lid = self.res(led)
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TRIGGER boom BEFORE INSERT ON halt_history BEGIN SELECT RAISE(ABORT, 'boom'); END")
        conn.commit()
        with self.assertRaises(sqlite3.DatabaseError):
            led.settle(lid, U(-5, 5))
        # the halt could not be written, so the row did not become 'unknown' either: one transaction
        self.assertEqual(self.rows("SELECT status FROM calls WHERE id=?", (lid,))[0]["status"], "pending")
        self.assertIsNone(led.halted())
        conn.execute("DROP TRIGGER boom")
        conn.commit()
        conn.close()
        self.assertEqual(led.settle(lid, U(-5, 5)).status, "unknown")
        halt = led.halted()
        self.assertEqual(halt.reason, "usage_contradiction")
        call_id = self.rows("SELECT call_id FROM calls WHERE id=?", (lid,))[0]["call_id"]
        self.assertEqual(halt.call_id, call_id)
        self.assertEqual(len(self.rows("SELECT * FROM halt_history")), 1)

    def test_cost_above_reservation_is_unknown(self):
        led = self.make(**{"prices.cached_per_million": "0.05"})   # cached above input: a cost can pass the reservation
        lid = self.res(led)                        # 200 reserved
        self.assertEqual(led.settle(lid, U(1000, 1000, cached=1000)).status, "unknown")    # 50 + 170 = 220
        self.assertEqual(led.halted().reason, "usage_contradiction")
        led2 = self.make("b.sqlite")
        lid = self.res(led2)
        self.assertEqual(led2.settle(lid, U(5000, 1000)).status, "unknown")               # 150 + 170 = 320
        self.assertEqual(led2.committed("baseline"), 200)          # the reservation keeps counting
        with self.assertRaises(BudgetError) as ctx:
            self.res(led2)
        self.assertEqual(ctx.exception.kind, "halted")

    def test_rejection_books_zero_and_releases_reservation(self):
        led = self.make(**{"budgets.solver_usd.phases.baseline": "0.000300"})
        job = J()
        lid = self.res(led, job)
        self.assertEqual(led.committed("baseline"), 200)
        self.assertEqual(led.settle(lid, None, rejection="provider_rejected", reason="400").spent_micro, 0)
        row = self.rows("SELECT * FROM calls WHERE id=?", (lid,))[0]
        self.assertEqual((row["status"], row["spent_micro"], row["reason"]), ("rejected", 0, "provider_rejected: 400"))
        counters = self.rows("SELECT * FROM job_counters")[0]
        self.assertEqual((counters["committed_micro"], counters["charged_attempts"]), (0, 0))
        self.assertEqual(led.committed(), 0)
        self.res(led, job)                         # the released reservation fits again

    def test_second_settle_raises(self):
        led = self.make()
        for first in (dict(usage=U()), dict(usage=None), dict(usage=None, rejection="x")):
            lid = self.res(led)
            led.settle(lid, first.pop("usage"), **first)
            with self.assertRaises(BudgetError) as ctx:
                led.settle(lid, U())
            self.assertEqual(ctx.exception.kind, "settled")
        with self.assertRaises(StateError):
            led.settle("l-nope", U())
        self.assertEqual(led.verify_totals(), [])

    def test_limit_exceeded_within_reservation_settles_without_halt(self):
        led = self.make()
        lid = self.res(led)                        # input_limit 1000
        settlement = led.settle(lid, U(1500, 10))  # 1,500 prompt tokens, but 45 + 1.7 fits 200
        self.assertEqual(settlement, Settlement("complete", 47, "usage"))
        self.assertIsNone(led.halted())
        self.assertEqual(led.snapshot()["limit_exceeded_requests"], 1)
        self.res(led)

    def test_interrupt_pending_keeps_reservation_committed(self):
        led = self.make()
        a, b = self.res(led), self.res(led)
        done = self.res(led)
        led.settle(done, U())
        restarted = Ledger(self.path, self.manifest, clock=self.clock, ids=self.ids)
        self.assertEqual(restarted.committed("baseline"), 412)       # a restart keeps every reservation
        self.assertEqual(restarted.interrupt_pending(), 2)
        rows = self.rows("SELECT status, reason FROM calls WHERE id IN (?, ?)", (a, b))
        self.assertEqual(rows, [{"status": "bounded_unknown", "reason": "runner interrupted"}] * 2)
        self.assertEqual(restarted.committed("baseline"), 412)
        self.assertEqual(restarted.snapshot()["bounded_unknown_micro"], 400)
        with self.assertRaises(BudgetError):
            restarted.settle(a, U())
        self.assertEqual(restarted.interrupt_pending(), 0)


class TestHalt(LedgerCase):
    def test_halt_blocks_reserve_first_reason_wins(self):
        led = self.make()
        self.assertTrue(led.set_halt("operator", "stop now"))
        self.assertFalse(led.set_halt("budget:total"))
        self.assertEqual(led.halted(), HaltState("operator", "stop now", self.clock.utc(), None))
        with self.assertRaises(BudgetError) as ctx:
            self.res(led)
        self.assertEqual(ctx.exception.kind, "halted")
        restarted = Ledger(self.path, self.manifest, clock=self.clock, ids=self.ids)
        self.assertEqual(restarted.halted().reason, "operator")
        self.assertEqual([r["reason"] for r in self.rows("SELECT reason FROM halt_history")], ["operator"])
        with self.assertRaises(ValueError):
            led.set_halt("not-a-reason")

    def test_clear_halt_needs_note_and_refuses_unknown(self):
        led = self.make()
        lid = self.res(led)
        led.settle(lid, U(-1, 1))                  # unknown, halts
        with self.assertRaises(ValueError):
            led.clear_halt("  ")
        with self.assertRaises(BudgetError) as ctx:
            led.clear_halt("operator checked")
        self.assertEqual(ctx.exception.kind, "unreconciled")
        led.reconcile(lid, 200, "billing export says 200")
        led.clear_halt("operator checked")
        self.assertIsNone(led.halted())
        history = self.rows("SELECT * FROM halt_history")
        self.assertEqual((history[0]["reason"], history[0]["cleared_note"]), ("usage_contradiction", "operator checked"))
        self.res(led)

    def test_reconcile_unknown_row_then_clear_halt(self):
        led = self.make()
        a, b = self.res(led), self.res(led)
        led.settle(a, U(-1, 1))
        led.settle(b, U(1.5, 1))
        with self.assertRaises(BudgetError) as ctx:
            self.res(led)
        self.assertEqual(ctx.exception.kind, "halted")
        with self.assertRaises(ValueError):
            led.reconcile(a, 250, "")
        with self.assertRaises(ValueError):
            led.reconcile(a, -1, "note")
        led.reconcile(a, 250, "billing export")
        with self.assertRaises(ValueError):        # below the reservation needs the flag
            led.reconcile(b, 100, "billing export")
        led.reconcile(b, 100, "billing export", below_reservation=True)
        with self.assertRaises(StateError):
            led.reconcile(a, 300, "again")
        history = self.rows("SELECT ledger_id, old_status, spent_micro, below_reservation FROM reconcile_history")
        self.assertEqual(history, [{"ledger_id": a, "old_status": "unknown", "spent_micro": 250, "below_reservation": 0},
                                   {"ledger_id": b, "old_status": "unknown", "spent_micro": 100, "below_reservation": 1}])
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM calls")[0]["n"], 2)          # nothing deleted
        self.assertEqual(led.committed("baseline"), 350)
        self.assertEqual(led.verify_totals(), [])
        snap = led.snapshot()
        self.assertEqual((snap["reconciled_requests"], snap["unknown_requests"], snap["observed_micro"]), (2, 0, 350))
        led.clear_halt("reconciled both")
        self.res(led)


class TestJobs(LedgerCase):
    def test_token_lookup_uses_hash_and_only_open_jobs(self):
        led = self.make()
        token = self.ids.token()
        job = J()
        led.register_job(job, token_sha256=hashlib.sha256(token.encode()).hexdigest())
        led.register_job(job, token_sha256=hashlib.sha256(token.encode()).hexdigest())      # idempotent
        self.assertEqual(led.identity_for_token(token), job)
        self.assertIsNone(led.identity_for_token(token + "x"))
        self.assertIsNone(led.identity_for_token(""))
        for f in self.dir.iterdir():               # the database files hold the digest, never the token
            data = f.read_bytes()
            self.assertNotIn(token.encode(), data, f.name)
            self.assertNotIn(token[7:].encode(), data, f.name)
        self.assertTrue(led.is_open(job.job_id))
        led.close_job(job.job_id)
        self.assertFalse(led.is_open(job.job_id))
        self.assertIsNone(led.identity_for_token(token))
        with self.assertRaises(ValueError):
            led.register_job(J(attempt=2), token_sha256=token)

    def test_close_open_jobs(self):
        led = self.make()
        jobs = [J(dataset=d) for d in ("USPTO", "LawBench", "Symptom2Disease")]
        for job in jobs:
            led.register_job(job)
        led.close_job(jobs[0].job_id)
        self.assertEqual(led.close_open_jobs(), 2)
        self.assertEqual(led.close_open_jobs(), 0)
        self.assertFalse(any(led.is_open(j.job_id) for j in jobs))
        reasons = {r["job_id"]: r["close_reason"] for r in self.rows("SELECT job_id, close_reason FROM jobs")}
        self.assertEqual(reasons, {jobs[0].job_id: "closed", jobs[1].job_id: "runner interrupted",
                                   jobs[2].job_id: "runner interrupted"})

    def test_prior_charged_only_after_a_charged_attempt(self):
        led = self.make()
        self.assertFalse(led.prior_charged("val.no_memory.USPTO"))
        led.register_job(J(attempt=1))
        led.register_job(J(attempt=2))             # attempt 1 reserved nothing
        self.assertFalse(led.prior_charged("val.no_memory.USPTO"))
        led.settle(self.res(led, J(attempt=2)), None, rejection="provider_rejected")
        led.register_job(J(attempt=3))             # attempt 2's only provider attempt was rejected: not charged
        self.assertFalse(led.prior_charged("val.no_memory.USPTO"))
        led.settle(self.res(led, J(attempt=3)), U())
        led.register_job(J(attempt=4))
        self.assertTrue(led.prior_charged("val.no_memory.USPTO"))

    def test_note_exhausted_needs_two_job_attempts_and_peer_ok(self):
        led = self.make()
        d1, d2 = "a" * 64, "b" * 64
        self.assertFalse(led.note_exhausted(J(attempt=1), d1, "c-1", peer_ok=False))
        self.assertFalse(led.note_exhausted(J(attempt=1), d1, "c-2", peer_ok=True))    # same job attempt
        self.assertTrue(led.note_exhausted(J(attempt=2), d1, "c-3", peer_ok=False))    # stored peer_ok holds
        self.assertFalse(led.note_exhausted(J(attempt=1), d2, "c-4", peer_ok=False))
        self.assertFalse(led.note_exhausted(J(attempt=2), d2, "c-5", peer_ok=False))   # no peer served, ever
        self.assertTrue(led.note_exhausted(J(attempt=3), d2, "c-6", peer_ok=True))     # peer_ok holds now
        rows = {r["request_sha256"]: r for r in self.rows("SELECT * FROM failed_requests")}
        self.assertEqual((rows[d1]["exhausted"], rows[d1]["peer_ok"], rows[d1]["last_call_id"]), (2, 1, "c-3"))
        self.assertEqual((rows[d2]["exhausted"], rows[d2]["peer_ok"], rows[d2]["last_job_id"]), (3, 1, J(attempt=3).job_id))
        self.assertFalse(led.note_exhausted(J(dataset="LawBench"), d1, "c-7", peer_ok=True))   # per job_key


class TestTotals(LedgerCase):
    def test_totals_equal_sums_after_random_operations(self):
        rng = random.Random(20261110)
        with mock.patch.object(ledger_mod, "FULLFSYNC", False):      # durability pragma only; logic unchanged
            led = self.make(**{"gateway.max_requests_per_job": 40})
            pending, unknown, attempts, refused = [], [], {}, {}
            keys = [("USPTO", "val"), ("LawBench", "val"), ("Symptom2Disease", "test")]
            for op in range(2000):
                roll = rng.random()
                if unknown and (roll >= 0.93 or not pending):
                    for lid in unknown:
                        led.reconcile(lid, rng.randint(0, 400), "seeded", below_reservation=True)
                    unknown.clear()
                    led.clear_halt("seeded test")
                elif roll < 0.45 or not pending:
                    dataset, split = rng.choice(keys)
                    attempt = attempts.setdefault(dataset, 1)
                    job = J(attempt=attempt, dataset=dataset, split=split, phase=rng.choice(wire.JOB_PHASES))
                    n = rng.choice((1, 1, 2, 3))
                    try:
                        pending.append(self.res(led, job, n=n, phase=wire.ledger_phase(job, n, prior_charged=False),
                                                inp=rng.randint(1, 5000), out=rng.randint(1, 3000)))
                    except BudgetError as exc:
                        self.assertIn(exc.kind, ("ceiling", "halted", "unreconciled", "cap", "total"))
                        refused[exc.kind] = refused.get(exc.kind, 0) + 1
                elif roll < 0.85:
                    lid = pending.pop(rng.randrange(len(pending)))
                    kind = rng.choice(("ok", "ok", "ok", "limit", "missing", "partial", "reject", "bad"))
                    usage = {"ok": U(rng.randint(0, 900), rng.randint(0, 900), cached=rng.randint(0, 0)),
                             "limit": U(rng.randint(5000, 6000), 1), "missing": None,
                             "partial": {"completion_tokens": rng.randint(0, 10)}, "reject": None,
                             "bad": U(-1, 1)}[kind]
                    status = led.settle(lid, usage, rejection="provider_rejected" if kind == "reject" else None).status
                    if status == "unknown":
                        unknown.append(lid)
                elif roll < 0.9:
                    dataset = rng.choice(keys)[0]
                    attempts[dataset] = attempts.get(dataset, 1) + 1
                    led.register_job(J(attempt=attempts[dataset], dataset=dataset))
                elif roll < 0.93:
                    led.interrupt_pending()
                    pending.clear()
                if op % 250 == 0:
                    self.assertEqual(led.verify_totals(), [], f"after op {op}")
            self.assertEqual(led.verify_totals(), [])
            # an independent recomputation from the raw rows, not through verify_totals
            rows = self.rows("SELECT * FROM calls")
            self.assertGreater(len(rows), 400, refused)
            self.assertIn("ceiling", refused)
            for phase in LEDGER_PHASES:
                expect = sum(r["spent_micro"] if r["spent_micro"] is not None else r["reserved_micro"]
                             for r in rows if r["phase"] == phase and r["status"] != "rejected")
                self.assertEqual(led.committed(phase), expect, phase)
            for counters in self.rows("SELECT * FROM job_counters"):
                mine = [r for r in rows if r["job_key"] == counters["job_key"] and r["status"] != "rejected"]
                self.assertEqual(counters["committed_micro"], sum(
                    r["spent_micro"] if r["spent_micro"] is not None else r["reserved_micro"] for r in mine))
                self.assertEqual(counters["charged_attempts"], len(mine))
            self.assertFalse(any(isinstance(v, float) for r in rows for v in r.values()))

    def test_snapshot_never_lists_rows(self):
        led = self.make()
        for _ in range(3):
            led.settle(self.res(led), U())
        self.res(led)
        led.settle(self.res(led), None)
        led.meter_open("sandbox_seconds", "sbx-1", 1000, "ms", worst_micro=50)
        snap = led.snapshot()
        self.assertEqual(set(snap), {"observed_micro", "bounded_unknown_micro", "in_flight_micro", "committed_by_phase",
                                     "caps_micro", "requests", "unknown_requests", "bounded_unknown_requests",
                                     "reconciled_requests", "limit_exceeded_requests", "halt", "meters"})

        def no_lists(value):
            self.assertNotIsInstance(value, (list, tuple))
            if isinstance(value, dict):
                for v in value.values():
                    no_lists(v)

        no_lists(snap)
        self.assertEqual((snap["observed_micro"], snap["in_flight_micro"], snap["bounded_unknown_micro"]), (36, 200, 200))
        self.assertEqual(snap["requests"], 5)
        self.assertEqual(snap["committed_by_phase"]["baseline"], 436)
        self.assertEqual(snap["caps_micro"]["total"], 60_000_000)
        self.assertEqual(set(snap["meters"]), set(METERS))
        self.assertEqual(snap["meters"]["sandbox_seconds"], {"quantity": 1000, "micro_usd": 50, "open_rows": 1})
        self.assertIsNone(snap["halt"])
        json.dumps(snap)

    def test_two_processes_can_write(self):
        led = self.make()
        manifest_file = self.dir / "manifest.json"
        manifest_file.write_text(json.dumps(self.manifest))
        script = (
            "import json, sys\n"
            "from pathlib import Path\n"
            "from mhforge.ledger import Ledger\n"
            "from mhforge.wire import JobIdentity\n"
            "m = json.loads(Path(sys.argv[2]).read_text())\n"
            "led = Ledger(Path(sys.argv[1]), m)\n"
            "job = JobIdentity('mh-test', 'val', 'no_memory', 'LawBench', 1, 'baseline', None)\n"
            "for i in range(40):\n"
            "    lid = led.reserve(call_id='c-child', n=1, job=job, phase='baseline', model='openai/gpt-oss-120b',"
            " input_limit=1000, output_limit=1000)\n"
            "    led.settle(lid, {'prompt_tokens': 100, 'completion_tokens': 50})\n"
            "    led.meter('eval_tables', 'child#e1#%d' % i, 1, 'tables')\n"
        )
        child = subprocess.Popen([sys.executable, "-c", script, str(self.path), str(manifest_file)],
                                 env=child_env(), cwd=str(SC), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        for i in range(40):
            led.settle(self.res(led), U(100, 50))
            led.meter("eval_tables", f"parent#e1#{i}", 1, "tables")
        out, err = child.communicate(timeout=120)
        self.assertEqual(child.returncode, 0, err.decode(errors="replace"))
        self.assertEqual(led.committed("baseline"), 80 * 12)
        self.assertEqual(led.meter_total("eval_tables"), (80, 0))
        self.assertEqual(led.verify_totals(), [])


class TestMeters(LedgerCase):
    def test_meter_once_per_key_and_caps(self):
        led = self.make(**{"budgets.eval_tables_max": 2, "budgets.sandbox_usd": "0.001000",
                           "budgets.wandb_upload_gb": 1, "budgets.proposer_list_usd": None})
        self.assertTrue(led.meter("eval_tables", "eval:val.x#e1#USPTO", 1, "tables"))
        self.assertFalse(led.meter("eval_tables", "eval:val.x#e1#USPTO", 1, "tables"))
        led.check_meter_cap("eval_tables", extra_quantity=1)
        led.meter("eval_tables", "eval:val.x#e1#LawBench", 1, "tables")
        with self.assertRaises(MeterCapExceeded) as ctx:
            led.check_meter_cap("eval_tables", extra_quantity=1)
        self.assertEqual(ctx.exception.meter, "eval_tables")
        led.meter("sandbox_seconds", "sbx-a", 60000, "ms", micro_usd=800)
        led.check_meter_cap("sandbox_seconds", extra_micro=200)
        with self.assertRaises(MeterCapExceeded):
            led.check_meter_cap("sandbox_seconds", extra_micro=201)
        led.meter("wandb_upload_bytes", "eval:val.x#e1", 2 ** 30 - 10, "bytes")
        led.check_meter_cap("wandb_upload_bytes", extra_quantity=10)
        with self.assertRaises(MeterCapExceeded):
            led.check_meter_cap("wandb_upload_bytes", extra_quantity=11)
        with self.assertRaises(MeterCapExceeded):  # no proposer cap configured: refuse
            led.check_meter_cap("proposer_list_price", extra_micro=1)
        for bad in (dict(meter="nope", key="k", quantity=1, unit="ms"), dict(meter="eval_tables", key="", quantity=1,
                    unit="tables"), dict(meter="eval_tables", key="k", quantity=-1, unit="tables"),
                    dict(meter="eval_tables", key="k", quantity=1, unit="bytes"),
                    dict(meter="sandbox_seconds", key="k", quantity=1.5, unit="ms")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                led.meter(**bad)
        with self.assertRaises(ValueError):
            led.check_meter_cap("nope")

    def test_open_meter_row_counts_at_worst_case_until_closed(self):
        led = self.make(**{"budgets.sandbox_usd": "1.00"})
        self.assertTrue(led.meter_open("sandbox_seconds", "sbx-1", 8_250_000, "ms", worst_micro=780_000,
                                       detail={"purpose": "job", "job_id": "val.no_memory.USPTO.a1"}))
        self.assertFalse(led.meter_open("sandbox_seconds", "sbx-1", 1, "ms", worst_micro=1))
        self.assertEqual(led.meter_total("sandbox_seconds"), (8_250_000, 780_000))
        with self.assertRaises(MeterCapExceeded):
            led.check_meter_cap("sandbox_seconds", extra_micro=300_000)
        self.assertTrue(led.meter_close("sandbox_seconds", "sbx-1", 100_000, micro_usd=9_000))
        self.assertFalse(led.meter_close("sandbox_seconds", "sbx-1", 1, micro_usd=1))
        self.assertFalse(led.meter_close("sandbox_seconds", "sbx-unknown", 1, micro_usd=1))
        self.assertEqual(led.meter_total("sandbox_seconds"), (100_000, 9_000))
        led.check_meter_cap("sandbox_seconds", extra_micro=300_000)
        self.assertEqual(led.open_meter_rows("sandbox_seconds"), [])
        with self.assertRaises(ValueError):
            led.meter_open("lens_bytes", "u#e1", 10, "bytes")

    def test_orphan_sandbox_is_metered_at_worst_case(self):
        led = self.make()
        opened_utc = self.clock.utc()
        led.meter_open("sandbox_seconds", "sbx-orphan", 8_250_000, "ms", worst_micro=780_000,
                       detail={"purpose": "import_check", "iteration": 3})
        crashed = Ledger(self.path, self.manifest, clock=self.clock, ids=self.ids)     # the runner died here
        rows = crashed.open_meter_rows("sandbox_seconds")
        self.assertEqual(rows, [{"key": "sbx-orphan", "quantity": 8_250_000, "micro_usd": 780_000, "utc": opened_utc,
                                 "detail": {"purpose": "import_check", "iteration": 3}}])
        self.assertEqual(crashed.meter_total("sandbox_seconds"), (8_250_000, 780_000))
        self.assertEqual(crashed.snapshot()["meters"]["sandbox_seconds"]["open_rows"], 1)
        self.clock.advance(600)                    # cleanup: min(worst case, time since the row was opened)
        crashed.meter_close("sandbox_seconds", "sbx-orphan", min(8_250_000, 600_000), micro_usd=56_727)
        self.assertEqual(crashed.meter_total("sandbox_seconds"), (600_000, 56_727))
        self.assertEqual(crashed.snapshot()["meters"]["sandbox_seconds"]["open_rows"], 0)

    def test_sandbox_rows_need_micro_and_cap_extras_are_non_negative(self):
        led = self.make(**{"budgets.sandbox_usd": "0.000010", "budgets.eval_tables_max": 1})
        with self.assertRaises(ValueError):        # a NULL micro_usd would count as $0 against budgets.sandbox_usd
            led.meter_open("sandbox_seconds", "sbx-1", 8_000_000, "ms")
        with self.assertRaises(ValueError):
            led.meter("sandbox_seconds", "sbx-1", 8_000_000, "ms")
        led.meter_open("sandbox_seconds", "sbx-1", 8_000_000, "ms", worst_micro=900)
        with self.assertRaises(ValueError):        # closing without the priced figure would erase the cost
            led.meter_close("sandbox_seconds", "sbx-1", 7_000_000)
        self.assertEqual(led.meter_total("sandbox_seconds"), (8_000_000, 900))
        led.meter("eval_tables", "a#e1#t", 5, "tables")
        for extra in (dict(extra_quantity=-10), dict(extra_micro=-1), dict(extra_quantity=1.0), dict(extra_micro=True)):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                led.check_meter_cap("eval_tables", **extra)
        with self.assertRaises(MeterCapExceeded):
            led.check_meter_cap("eval_tables")

    def test_meter_max_ignores_open_rows(self):
        led = self.make()
        self.assertEqual(led.meter_max("proposer_list_price"), 0)
        led.meter_open("proposer_list_price", "iter001.s1", 25_000_000, "micro_usd", worst_micro=25_000_000)
        self.assertEqual(led.meter_max("proposer_list_price"), 0)
        led.meter_close("proposer_list_price", "iter001.s1", 3_000_000, micro_usd=3_000_000)
        led.meter_open("proposer_list_price", "iter002.s1", 25_000_000, "micro_usd")    # micro defaults to quantity
        self.assertEqual(led.meter_max("proposer_list_price"), 3_000_000)
        self.assertEqual(led.meter_total("proposer_list_price"), (28_000_000, 28_000_000))
        led.check_meter_cap("proposer_list_price", extra_micro=72_000_000)            # cap 100.00
        with self.assertRaises(MeterCapExceeded):
            led.check_meter_cap("proposer_list_price", extra_micro=72_000_001)

    def test_lens_buckets_cap_separately_and_in_total(self):
        led = self.make(**{"budgets.lens_ingest_mb": 3,
                           "budgets.lens_ingest_split_mb": {"main": 2, "preflight": 1, "recovery": 1}})
        led.meter("lens_bytes", "job:a#e1", 2 * MIB, "bytes", bucket="main")
        with self.assertRaises(MeterCapExceeded) as ctx:
            led.check_meter_cap("lens_bytes", extra_quantity=1, bucket="main")
        self.assertEqual(ctx.exception.bucket, "main")
        led.check_meter_cap("lens_bytes", extra_quantity=MIB, bucket="preflight")
        led.meter("lens_bytes", "job:b#e1", MIB, "bytes", bucket="preflight")
        with self.assertRaises(MeterCapExceeded) as ctx:       # recovery has room; the total does not
            led.check_meter_cap("lens_bytes", extra_quantity=1, bucket="recovery")
        self.assertEqual((ctx.exception.meter, ctx.exception.bucket), ("lens_bytes", "recovery"))
        self.assertIn("total", str(ctx.exception))
        self.assertEqual(led.meter_total("lens_bytes", bucket="main"), (2 * MIB, 0))
        self.assertEqual(led.meter_total("lens_bytes"), (3 * MIB, 0))
        for bad in ("", "other"):
            with self.assertRaises(ValueError):
                led.check_meter_cap("lens_bytes", extra_quantity=1, bucket=bad)
            with self.assertRaises(ValueError):
                led.meter("lens_bytes", f"job:c{bad}#e1", 1, "bytes", bucket=bad)
        with self.assertRaises(ValueError):
            led.meter("eval_tables", "x#e1#t", 1, "tables", bucket="main")

    def test_span_upload_and_table_meters(self):
        led = self.make()
        self.assertTrue(led.meter("lens_spans", "job:a#e1", 120, "spans", bucket="main"))
        self.assertTrue(led.meter("lens_spans", "job:a#e2", 120, "spans", bucket="recovery"))
        led.check_meter_cap("lens_spans", extra_quantity=10 ** 12, bucket="main")      # spans have no cap
        self.assertEqual(led.meter_total("lens_spans"), (240, 0))
        self.assertEqual(led.meter_total("lens_spans", bucket="recovery"), (120, 0))
        self.assertTrue(led.meter("wandb_upload_bytes", "eval:val.x#e1", 5000, "bytes"))
        self.assertTrue(led.meter("eval_tables", "eval:val.x#e1#USPTO", 1, "tables"))
        self.assertFalse(led.meter("eval_tables", "eval:val.x#e1#USPTO", 1, "tables"))
        self.assertTrue(led.meter("eval_tables", "eval:val.x#e2#USPTO", 1, "tables"))
        with self.assertRaises(ValueError):
            led.meter("lens_spans", "job:a#e3", 1, "spans")    # spans are bucketed like bytes
        meters = led.snapshot()["meters"]
        self.assertEqual(meters["lens_spans"]["quantity"], 240)
        self.assertEqual(meters["wandb_upload_bytes"]["quantity"], 5000)
        self.assertEqual(meters["eval_tables"]["quantity"], 2)
        rows = self.rows("SELECT meter, key, bucket, status, unit FROM meter_events WHERE meter='lens_spans' ORDER BY key")
        self.assertEqual([r["bucket"] for r in rows], ["main", "recovery"])
        self.assertTrue(all(r["status"] == "final" and r["unit"] == "spans" for r in rows))


if __name__ == "__main__":
    unittest.main()
