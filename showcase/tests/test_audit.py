# SPDX-License-Identifier: Apache-2.0
# Adapted from ash0ts/harness2-wandb@31b0f82 harness2/showcase/audit.py (the reconciliation it asserts). Changes: rewritten
# as unittest cases over a state directory produced by the real gateway, ledger and stub provider; one tamper per case.
"""Tests for mhforge.audit (spec 5.2): a clean state passes; each tamper is found; in-flight and unrecorded refusals are
accounted; informational rows are listed without failing; the audit creates no file."""

from __future__ import annotations

import json
import os
import random
import sqlite3
import unittest
from pathlib import Path

from mhforge import audit as audit_mod
from mhforge import ledger as ledger_mod
from mhforge import util, wire
from mhforge.circuit import Circuit
from mhforge.gateway import Gateway
from mhforge.ledger import Ledger
from mhforge.replay import ReplayStore
from mhforge.stubprovider import Fault, StubProvider, default_reply
from mhforge.wire import JobIdentity
from tests import helpers

_saved_fullfsync = None


def setUpModule():                                 # durability pragma only (test_ledger checks it); keeps tests fast
    global _saved_fullfsync
    _saved_fullfsync = ledger_mod.FULLFSYNC
    ledger_mod.FULLFSYNC = False


def tearDownModule():
    ledger_mod.FULLFSYNC = _saved_fullfsync


def body(text: str, **extra) -> bytes:
    doc = {"model": "openai/gpt-oss-120b", "messages": [{"role": "system", "content": "Reasoning: medium"},
                                                        {"role": "user", "content": text}],
           "max_tokens": 16384, "temperature": 0.0}
    doc.update(extra)
    return wire.make_envelope("POST", "/v1/chat/completions", json.dumps(doc).encode("utf-8"))


def prompt(i: int) -> str:
    return f"SYNTHETIC USPTO PROMPT {i}"


def sha(text: str) -> str:
    return util.sha256_bytes(text.encode("utf-8"))


class World:
    """A state directory made by the real gateway: one validation job attempt with four examples (two correct), one
    replayed call, its job record, result file and sidecar."""

    def __init__(self, tc: unittest.TestCase, *, faults=(), rules=default_reply, extra_requests=(), **overrides):
        ctx = helpers.tmp_state()
        state = ctx.__enter__()
        tc.addCleanup(ctx.__exit__, None, None, None)
        self.paths = state.paths
        self.manifest = helpers.make_manifest({"provider.kind": "stub", **overrides})
        self.clock = helpers.FakeClock()
        self.ids = helpers.SeqIds(self.clock)
        self.ledger = Ledger(self.paths.ledger, self.manifest, clock=self.clock, ids=self.ids)
        replay = ReplayStore(self.paths.replay, "search", clock=self.clock)
        circuit = Circuit(self.paths.circuit, threshold=3, cooldown_s=60, clock=self.clock)
        self.stub = StubProvider(rules=rules, faults=faults, clock=self.clock)
        self.gw = Gateway(self.manifest, self.paths, provider=self.stub, ledger=self.ledger, replay=replay,
                          circuit=circuit, clock=self.clock, ids=self.ids, rng=random.Random(7))
        self.gw.poll_s = 0.05
        self.job = JobIdentity("mh-test", "val", "no_memory", "USPTO", 1, "baseline", None, None, None)
        self.gw.register_job(self.job)
        self.replies = []
        for i in range(4):
            self.clock.advance(1.0)
            self.replies.append(self.gw.handle(self.job, body(prompt(i)), ingress="relay"))
        self.clock.advance(1.0)
        self.replies.append(self.gw.handle(self.job, body(prompt(0)), ingress="relay"))   # replayed from the store
        for raw in extra_requests:
            self.clock.advance(1.0)
            self.replies.append(self.gw.handle(self.job, raw, ingress="relay"))
        self.counters = self.gw.close_job(self.job.job_id, drain_timeout_s=5)
        self.write_job()

    # ------------------------------------------------------------ job record, outputs, sidecar
    def attempt_dir(self) -> Path:
        return self.paths.attempt_dir(self.job.job_key, 1)

    def write_job(self) -> None:
        out = self.attempt_dir() / "out"
        util.write_json(out / "val.json", {"accuracy": 0.5, "correct": 2, "total": 4})
        rows = [{"kind": "meta", "seq": 1, "schema": "mhforge.sidecar/1", "t": 1.0},
                {"kind": "splits", "seq": 2, "t": 1.0, "dataset": "USPTO", "wanted": ["val"]}]
        seq = 3
        for p in (1, 2):
            for i in range(4):
                hit = p == 2 and i != 0                    # pass 2 of example 0 is the replayed gateway call
                rows.append({"kind": "call", "seq": seq, "user_sha256": sha(prompt(i)), "cache_hit": hit,
                             "error": None})
                rows.append({"kind": "eval", "seq": seq + 1, "t": 2.0 + seq, "split": "val", "index": i, "pass": p,
                             "call_seqs": [seq], "batch_user_sha256": [], "was_correct": i < 2,
                             "prediction": "SYNTHETIC", "target": "SYNTHETIC", "metrics": {}})
                seq += 2
        rows.append({"kind": "done", "seq": seq, "t": 99.0, "exit": 0})
        (out / "sidecar.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), "utf-8")
        run = self.attempt_dir() / "run"
        run.mkdir(parents=True, exist_ok=True)
        (run / "stderr.txt").write_text("SYNTHETIC worker stderr\n", "utf-8")
        self.job_doc = {
            "schema": "mhforge.job/1", "campaign": "mh-test", "job_key": self.job.job_key, "split": "val",
            "candidate": "no_memory", "dataset": "USPTO", "phase": "baseline", "status": "succeeded",
            "attempts": [{"attempt": 1, "job_id": self.job.job_id, "outcome": "ok", "infra": False,
                          "result_usable": True, "refusal_codes": dict(self.counters["refusal_codes"]),
                          "gateway": dict(self.counters)}],
            "result": {"accuracy": 0.5, "correct": 2, "total": 4}}
        self.save_job()

    def save_job(self) -> None:
        util.write_json(self.paths.job_dir(self.job.job_key) / "job.json", self.job_doc)

    # ------------------------------------------------------------ tamper helpers
    def record_paths(self) -> list[Path]:
        return sorted(self.paths.calls_dir(self.job.job_id).glob("*.json"))

    def records(self) -> list[dict]:
        return [json.loads(p.read_text("utf-8")) for p in self.record_paths()]

    def edit_record(self, pred, fn) -> None:
        for p in self.record_paths():
            rec = json.loads(p.read_text("utf-8"))
            if pred(rec):
                fn(rec)
                util.write_json(p, rec)
                return
        raise AssertionError("no record matched")

    def sql(self, statement: str, args=()) -> None:
        conn = sqlite3.connect(self.paths.ledger, timeout=30)
        try:
            conn.execute(statement, args)
            conn.commit()
        finally:
            conn.close()

    def run(self) -> dict:
        return audit_mod.audit(self.manifest, self.paths)


def completed(rec: dict) -> bool:
    return rec.get("outcome") == "completed"


class CleanState(unittest.TestCase):
    def test_clean_state_passes(self):
        w = World(self)
        self.assertEqual([r.status for r in w.replies], [200] * 5)
        outcomes = sorted(r["outcome"] for r in w.records())
        self.assertEqual(outcomes, ["completed"] * 4 + ["replayed"])
        res = w.run()
        self.assertEqual(res["problems"], [])
        self.assertTrue(res["ok"])
        self.assertEqual(res["calls"], 5)
        conn = sqlite3.connect(w.paths.ledger)
        try:
            spent = conn.execute("SELECT SUM(spent_micro) FROM calls WHERE status='complete'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(res["observed_micro"], spent)
        self.assertGreater(spent, 0)
        self.assertEqual(res["bounded_unknown_micro"], 0)
        self.assertEqual(set(res), {"ok", "problems", "calls", "observed_micro", "bounded_unknown_micro", "listed"})

    def test_empty_state_passes(self):
        with helpers.tmp_state() as st:
            res = audit_mod.audit(helpers.make_manifest(), st.paths)
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(res["calls"], 0)


class Tampers(unittest.TestCase):
    def assertFinds(self, w: World, needle: str) -> None:
        res = w.run()
        self.assertFalse(res["ok"])
        self.assertTrue(any(needle in p for p in res["problems"]), res["problems"])

    def test_detects_each_tamper(self):
        with self.subTest("changed usage"):
            w = World(self)
            lid = w.records()[0]["attempts"][0]["ledger_id"]
            w.sql("UPDATE calls SET usage_json=? WHERE id=?",
                  (json.dumps({"prompt_tokens": 9999, "completion_tokens": 16, "total_tokens": 10015}), lid))
            self.assertFinds(w, f"ledger row {lid}: spent")
        with self.subTest("missing ledger row"):
            w = World(self)
            lid = w.records()[1]["attempts"][0]["ledger_id"]
            w.sql("DELETE FROM calls WHERE id=?", (lid,))
            self.assertFinds(w, f"ledger row {lid} missing")
            self.assertFinds(w, "phase_totals")
        with self.subTest("ledger row without a call record"):
            w = World(self)
            rec_path = next(p for p in w.record_paths() if completed(json.loads(p.read_text())))
            lid = json.loads(rec_path.read_text())["attempts"][0]["ledger_id"]
            rec_path.unlink()
            self.assertFinds(w, f"ledger row {lid} (call")
        with self.subTest("missing raw file"):
            w = World(self)
            rec = w.records()[2]
            (w.paths.calls_dir(w.job.job_id) / rec["attempts"][0]["sse_file"]).unlink()
            self.assertFinds(w, f"call {rec['call_id']} attempt 1: raw stream file missing")
        with self.subTest("reply status outside 200, 402, 403"):
            w = World(self)
            w.edit_record(completed, lambda r: r["reply"].update(status=429))
            self.assertFinds(w, "reply status 429")
        with self.subTest("secret in a record"):
            w = World(self)
            w.edit_record(completed, lambda r: r.update(note="mhfjob_" + "Ab3" * 14 + "x"))
            self.assertFinds(w, "secret rule job_token")
        with self.subTest("bare key found by digest"):
            w = World(self)
            key = "q7Zr2xVb9LmN4kTp8WcY3hJd6sQe"                 # fits no pattern
            util.append_key_digest(w.paths.keydigests, "MHFORGE_FORGE_API_KEY", key)
            (w.attempt_dir() / "run" / "stderr.txt").write_text(f"SYNTHETIC {key} leaked\n", "utf-8")
            self.assertFinds(w, "secret rule known_digest")
        with self.subTest("succeeded job with a tainted code"):
            w = World(self)
            w.job_doc["attempts"][0]["refusal_codes"] = {"relay_unavailable": 1}
            w.save_job()
            self.assertFinds(w, "tainted refusal code(s) relay_unavailable")
        with self.subTest("result disagrees with the joined examples"):
            w = World(self)
            util.write_json(w.attempt_dir() / "out" / "val.json", {"accuracy": 0.75, "correct": 3, "total": 4})
            self.assertFinds(w, "correct 2 != result correct 3")
        with self.subTest("succeeded job without a usable result"):
            w = World(self)
            (w.attempt_dir() / "out" / "val.json").unlink()
            self.assertFinds(w, "without a usable val.json")
        with self.subTest("replay source is not a paying call"):
            w = World(self)
            w.edit_record(lambda r: r["outcome"] == "replayed",
                          lambda r: r["replay"].update(source_call_id="c-20261110T000000-ffffffffffff"))
            self.assertFinds(w, "not a recorded paying call")
        with self.subTest("pending ledger row"):
            w = World(self)
            lid = w.records()[0]["attempts"][0]["ledger_id"]
            w.sql("UPDATE calls SET status='pending', spent_micro=NULL WHERE id=?", (lid,))
            self.assertFinds(w, f"ledger row {lid}: pending")
        with self.subTest("job counters disagree with the records"):
            w = World(self)
            w.job_doc["attempts"][0]["gateway"]["completed"] -= 1
            w.save_job()
            self.assertFinds(w, "beyond the job counters")

    def test_missing_phase_totals_row_is_found(self):
        w = World(self)
        w.sql("DELETE FROM phase_totals WHERE phase='search'")      # no call in that phase: the sums still agree
        self.assertFinds(w, "phase_totals row missing: search")

    def test_verified_receipt_needs_matching_stored_spans(self):
        w = World(self)
        unit = "job:" + w.job.job_id
        receipt = {"schema": "mhforge.receipt/1", "unit": unit, "surface": "agent_lens", "status": "verified",
                   "expected_spans": 3}
        util.write_json(w.paths.receipt_file(unit, "agent_lens"), receipt)
        util.write_json(w.paths.stored_spans_file(unit), [{"span": i} for i in range(3)])
        self.assertTrue(w.run()["ok"], w.run()["problems"])
        util.write_json(w.paths.stored_spans_file(unit), [{"span": i} for i in range(2)])
        self.assertFinds(w, "2 stored spans, expected_spans 3")
        w.paths.stored_spans_file(unit).unlink()
        self.assertFinds(w, "verified without stored spans")
        receipt["status"] = "emitted"                     # only verified receipts are held to the count
        util.write_json(w.paths.receipt_file(unit, "agent_lens"), receipt)
        self.assertTrue(w.run()["ok"])


class Accounting(unittest.TestCase):
    def test_in_flight_at_close_and_unrecorded_refusals_are_accounted(self):
        wrong = [body("SYNTHETIC REFUSED", model="not-the-wire-model") for _ in range(3)]
        w = World(self, extra_requests=wrong, **{"gateway.max_refused_records_per_job": 1})
        self.assertEqual(w.counters["refused"], 1)
        self.assertEqual(w.counters["refused_unrecorded"], 2)
        self.assertEqual(sum(w.counters["refusal_codes"].values()), 3)
        self.assertEqual(sum(1 for r in w.records() if r["outcome"] == "refused"), 1)
        res = w.run()
        self.assertTrue(res["ok"], res["problems"])
        # unrecorded refusals must be counted: without them the counters do not add up
        w.job_doc["attempts"][0]["gateway"]["refused_unrecorded"] = 0
        w.save_job()
        res = w.run()
        self.assertTrue(any("refusal(s) counted" in p for p in res["problems"]), res["problems"])
        # a request that settled after the job was closed is in the records but not in the counters
        w.job_doc["attempts"][0]["gateway"]["refused_unrecorded"] = 2
        w.job_doc["attempts"][0]["gateway"]["completed"] -= 1
        w.save_job()
        self.assertFalse(w.run()["ok"])
        w.job_doc["attempts"][0]["gateway"]["in_flight_at_close"] = 1
        w.save_job()
        res = w.run()
        self.assertTrue(res["ok"], res["problems"])

    def test_lists_reconciled_limit_exceeded_and_repeatable_rows(self):
        calls = {"n": 0}

        def rules(b):
            calls["n"] += 1
            reply = default_reply(b)
            if calls["n"] == 5:                            # a reply above the output limit: limit_exceeded, still paid
                reply["usage"] = {"prompt_tokens": 20, "completion_tokens": 16385, "total_tokens": 16405}
            return reply

        w = World(self, rules=rules, faults=[Fault("contradictory_usage", when=6)],
                  extra_requests=[body(prompt(5)), body(prompt(6))])
        rows = {}
        conn = sqlite3.connect(w.paths.ledger)
        try:
            for lid, status, reason, reserved in conn.execute("SELECT id, status, reason, reserved_micro FROM calls"):
                rows[lid] = (status, reason or "", reserved)
        finally:
            conn.close()
        unknown = [lid for lid, (s, _, _) in rows.items() if s == "unknown"]
        limit = [lid for lid, (_, r, _) in rows.items() if r.startswith("limit_exceeded")]
        self.assertEqual(len(unknown), 1, rows)
        self.assertEqual(len(limit), 1, rows)
        w.sql("DELETE FROM halt")                         # the contradiction halted the campaign; reconcile needs none
        reserved = rows[unknown[0]][2]
        w.ledger.reconcile(unknown[0], reserved, "SYNTHETIC operator figure")
        # a repeatable provider failure is recorded in the refusal detail
        w.edit_record(lambda r: r["user_sha256"] == sha(prompt(3)),
                      lambda r: r.update(refusal={"code": None, "status": 200, "detail": "repeatable_provider_failure"}))
        # an admission-only record (the runner died before the request ended), accounted as in flight at close
        adm = dict(w.records()[0], outcome=None, ended_utc=None, reply=None, call_id="c-20261110T000009-00000000aaaa",
                   attempts=[])
        util.write_json(w.paths.calls_dir(w.job.job_id) / f"{adm['call_id']}.json", adm)
        w.job_doc["attempts"][0]["gateway"]["in_flight_at_close"] += 1
        w.save_job()
        w.ledger.meter_open("sandbox_seconds", "sbx-synthetic", 60000, "ms", worst_micro=10)
        res = w.run()
        self.assertTrue(res["ok"], res["problems"])
        listed = res["listed"]
        self.assertEqual(listed["reconciled"], unknown)
        self.assertEqual(listed["limit_exceeded"], limit)
        self.assertEqual(len(listed["repeatable_provider_failure"]), 1)
        self.assertEqual(listed["admission_only"], [adm["call_id"]])
        self.assertEqual(listed["open_meters"], ["sandbox_seconds:sbx-synthetic"])
        conn = sqlite3.connect(w.paths.ledger)
        try:
            complete = conn.execute("SELECT SUM(spent_micro) FROM calls WHERE status='complete'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(res["observed_micro"], complete + reserved)   # observed includes the reconciled spend


class ReadOnly(unittest.TestCase):
    def tree(self, root: Path) -> dict:
        out = {}
        for dirpath, dirnames, filenames in os.walk(root):
            for n in dirnames + filenames:
                p = Path(dirpath) / n
                st = p.lstat()
                out[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
        return out

    def test_audit_creates_no_file(self):
        w = World(self)
        before = self.tree(w.paths.root)
        self.assertFalse(os.path.exists(str(w.paths.ledger) + "-wal"))
        res = w.run()
        self.assertTrue(res["ok"], res["problems"])
        self.assertEqual(self.tree(w.paths.root), before)
        # also with a write-ahead log left open by a live writer
        conn = sqlite3.connect(w.paths.ledger, timeout=30)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO meta (key, value) VALUES ('synthetic', 'x')")
            conn.execute("COMMIT")
            before = self.tree(w.paths.root)
            w.run()
            self.assertEqual(self.tree(w.paths.root), before)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
