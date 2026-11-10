# SPDX-License-Identifier: MIT
"""Tests for mhforge.journal (spec 3.16, 5.2)."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from mhforge import util
from mhforge.errors import StateError
from mhforge.journal import Journal, read_journal
from tests.helpers import FakeClock

SHA_A = "a" * 64
SHA_B = "b" * 64


class JournalTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mhf-journal-")
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "state" / "journal.jsonl"
        self.clock = FakeClock()

    def open(self) -> Journal:
        return Journal(self.path, clock=self.clock)

    def raw(self) -> bytes:
        return self.path.read_bytes()


class TestJournal(JournalTestCase):
    def test_seq_continues_after_reopen(self) -> None:
        j = self.open()
        first = j.record("run", "search", "started", detail={"argv": ["search"]})
        j.record("job", "val.no_memory.USPTO", "started", attempt=1)
        self.assertEqual(first["seq"], 1)
        self.assertEqual(first["schema"], "mhforge.journal/1")
        self.assertEqual(first["utc"], util.utc_iso(self.clock.now()))
        self.assertEqual(first["pid"], os.getpid())
        j2 = self.open()
        third = j2.record("job", "val.no_memory.USPTO", "complete", attempt=1)
        self.assertEqual(third["seq"], 3)
        self.assertEqual([e["seq"] for e in read_journal(self.path)], [1, 2, 3])
        self.assertEqual([e["seq"] for e in self.open().entries()], [1, 2, 3])
        self.assertEqual(oct(self.path.stat().st_mode & 0o777), "0o600")

    def test_torn_tail_tolerated(self) -> None:
        j = self.open()
        j.record("run", "search", "started")
        j.record("job", "k", "started", attempt=1)
        before = self.raw()
        torn = b'{"schema": "mhforge.journal/1", "seq": 3, "op": "jo'
        with open(self.path, "ab") as fh:
            fh.write(torn)
        j2 = self.open()                                  # reading tolerates the torn tail
        self.assertEqual([e["seq"] for e in j2.entries()], [1, 2])
        self.assertEqual(read_journal(self.path)[-1]["seq"], 2)
        self.assertEqual(util.read_jsonl(self.path)[-1]["seq"], 2)
        self.assertEqual(self.raw(), before + torn)       # opening writes nothing
        entry = j2.record("job", "k", "complete", attempt=1)
        self.assertEqual(entry["seq"], 4)                  # seq 3 is the marker that terminates the torn bytes
        data = self.raw()
        self.assertTrue(data.startswith(before + torn + b"\n"))   # appended only, never rewritten or truncated
        entries = read_journal(self.path)
        self.assertEqual([e["seq"] for e in entries], [1, 2, 3, 4])
        self.assertEqual((entries[2]["op"], entries[2]["key"], entries[2]["status"]), ("journal", "torn_tail", "terminated"))
        self.assertEqual(entries[2]["detail"]["bytes"], len(torn))
        self.assertEqual(self.open().last("job", "k")["status"], "complete")
        self.assertEqual(util.read_marked_jsonl(self.path), entries)    # the shared reader of other modules agrees
        # a bad line that is not a terminated torn tail is corruption
        corrupt = Path(self._tmp.name) / "corrupt.jsonl"
        later = json.dumps(dict(entries[3], seq=3)).encode() + b"\n"
        corrupt.write_bytes(before + b"not json\n" + later)
        with self.assertRaises(StateError):
            read_journal(corrupt)
        with self.assertRaises(StateError):
            Journal(corrupt)
        with self.assertRaises(StateError):
            util.read_marked_jsonl(corrupt)

    def test_torn_tail_survives_a_crash_during_its_repair(self) -> None:
        torn = b'{"schema":"mhforge.journal/1","seq":2,"op":"jo'
        half_marker = b'{"schema":"mhforge.journal/1","seq":2,"utc":"x","op":"jour'
        for landed in (b"\n", b"\n" + half_marker, b"\n\n"):      # what reached disk of the "\n" + marker + entry write
            with self.subTest(landed=landed):
                if self.path.exists():
                    self.path.unlink()
                self.open().record("run", "search", "started")
                with open(self.path, "ab") as fh:
                    fh.write(torn + landed)
                j = self.open()
                self.assertEqual([e["seq"] for e in j.entries()], [1])
                j.record("job", "k", "started", attempt=1)
                self.assertEqual([e["op"] for e in read_journal(self.path)], ["run", "journal", "job"])
                with open(self.path, "ab") as fh:
                    fh.write(b'{"sch')                                # and a third crash
                self.open().record("job", "k", "complete", attempt=1)
                self.assertEqual(self.open().last("job", "k")["status"], "complete")

    def test_valid_tail_without_newline_is_kept_and_terminated(self) -> None:
        j = self.open()
        j.record("run", "search", "started")
        data = self.raw()
        self.path.write_bytes(data[:-1])                  # the last line lost only its newline
        j2 = self.open()
        self.assertEqual(len(j2.entries()), 1)
        j2.record("run", "search", "complete")
        self.assertEqual([e["seq"] for e in util.read_jsonl(self.path)], [1, 2])

    def test_recover_marks_started_as_interrupted_once(self) -> None:
        j = self.open()
        j.record("run", "search", "started")
        j.record("job", "val.a.USPTO", "started", attempt=1, detail={"handle": {"pgid": 10, "t_start": 1.0}})
        j.record("job", "val.b.USPTO", "started", attempt=1)
        j.record("job", "val.b.USPTO", "complete", attempt=1, detail={"outcome": "ok"})
        j.record("proposer", "iter001", "started", attempt=1)
        j.record("benchmark", "val.a", "started")
        j2 = self.open()                                   # the next runner start
        live = j2.record("run", "search", "started")       # written by this instance before recover(): stays live
        got = j2.recover()
        keys = sorted((e["op"], e["key"], e["attempt"]) for e in got)
        self.assertEqual(keys, [("benchmark", "val.a", 0), ("job", "val.a.USPTO", 1), ("proposer", "iter001", 1),
                                ("run", "search", 0)])
        self.assertTrue(all(e["status"] == "interrupted" for e in got))
        run_entry = [e for e in got if e["op"] == "run"][0]
        self.assertEqual(run_entry["detail"]["started_seq"], 1)
        self.assertNotEqual(run_entry["detail"]["started_seq"], live["seq"])
        self.assertEqual(j2.recover(), [])                 # idempotent in the same process
        j3 = self.open()
        again = j3.recover()                               # and in the next one: only the live run of j2 is left open
        self.assertEqual([(e["op"], e["detail"]["started_seq"]) for e in again], [("run", live["seq"])])
        self.assertEqual(self.open().recover(), [])
        self.assertEqual(self.open().last("job", "val.b.USPTO")["status"], "complete")

    def test_recovered_attempt_is_an_infra_terminal(self) -> None:
        j = self.open()
        j.record("job", "val.a.USPTO", "started", attempt=1, detail={"candidate_sha256": SHA_A})
        got = self.open().recover()
        self.assertEqual((got[0]["detail"]["outcome"], got[0]["detail"]["infra"]), ("interrupted", True))
        counted = self.open().counted_attempts("val.a.USPTO", SHA_A)
        self.assertEqual((counted[0]["status"], counted[0]["detail"]["infra"]), ("interrupted", True))

    def test_handle_rows_do_not_change_attempt_status(self) -> None:
        j = self.open()
        j.record("job", "k", "started", attempt=1, detail={"candidate_sha256": SHA_A})
        j.add_handle("job", "k", 1, {"pgid": 5, "t_start": 1.0})
        self.assertEqual(j.attempts("job", "k")[0]["status"], "started")
        self.assertEqual(j.attempts("job", "k")[0]["detail"], {"candidate_sha256": SHA_A})
        self.assertEqual(j.last("job", "k")["status"], "started")
        self.assertEqual(j.last("job", "k", attempt=1)["status"], "started")
        self.assertEqual(j.open_handles(), [{"pgid": 5, "t_start": 1.0}])

    def test_attempt_numbers_never_reused(self) -> None:
        j = self.open()
        self.assertEqual(j.next_attempt("job", "k"), 1)
        j.record("job", "k", "started", attempt=1)
        j.record("job", "k", "failed", attempt=1)
        self.assertEqual(j.next_attempt("job", "k"), 2)
        j.record("job", "k", "started", attempt=2)
        j.record("job", "k", "reset", detail={"note": "operator"})
        self.assertEqual(self.open().next_attempt("job", "k"), 3)        # a reset does not reset the numbering
        j.record("job", "k", "started", attempt=3)
        j2 = self.open()
        j2.recover()                                                     # attempt 3 becomes interrupted
        self.assertEqual(j2.next_attempt("job", "k"), 4)
        self.assertEqual(j2.next_attempt("job", "other"), 1)
        self.assertEqual([a["attempt"] for a in j2.attempts("job", "k")], [1, 2, 3])
        self.assertEqual([a["status"] for a in j2.attempts("job", "k")], ["failed", "interrupted", "interrupted"])
        # attempt 2 was never closed before the reopen either
        self.assertEqual(j2.last("job", "k", attempt=2)["status"], "interrupted")

    def test_open_handles(self) -> None:
        j = self.open()
        j.record("job", "val.a.USPTO", "started", attempt=1, detail={"executor": "sandbox", "handle": {"sandbox_id": "sbx-1"}})
        j.add_handle("job", "val.a.USPTO", 1, {"sandbox_id": "sbx-2"})                  # a second create
        j.record("job", "val.b.USPTO", "started", attempt=1)
        j.add_handle("job", "val.b.USPTO", 1, {"pgid": 4242, "t_start": 1794268800.5})
        j.record("job", "val.b.USPTO", "complete", attempt=1)                            # closed: its handle is gone
        j.record("import_check", "iter002", "started", attempt=1, detail={"executor": "local"})
        j.add_handle("import_check", "iter002", 1, {"pgid": 77, "t_start": 2.0})
        j.record("import_check", "iter001", "started", attempt=1, detail={"handle": {"pgid": 5, "t_start": 1.0}})
        j.record("import_check", "iter001", "failed", attempt=1, detail={"kind": "exec", "stderr_tail": ""})
        j.record("proposer", "iter002", "started", attempt=1, detail={"handle": {"pgid": 9}})  # not an executor op
        expect = [{"sandbox_id": "sbx-1"}, {"sandbox_id": "sbx-2"}, {"pgid": 77, "t_start": 2.0}]
        self.assertEqual(j.open_handles(), expect)
        self.assertEqual(self.open().open_handles(), expect)
        j2 = self.open()
        j2.recover()
        self.assertEqual(j2.open_handles(), [])

    def test_session_start_seq(self) -> None:
        j = self.open()
        self.assertEqual(j.session_start_seq(), 0)
        j.record("job", "k", "started", attempt=1)
        self.assertEqual(j.session_start_seq(), 0)
        first = j.record("run", "baselines", "started")
        j.record("run", "baselines", "complete")
        self.assertEqual(j.session_start_seq(), first["seq"])
        second = j.record("run", "search", "started")
        j.record("job", "k", "started", attempt=2)
        j.record("run", "search", "stopped", detail={"reason": "interrupted"})
        self.assertEqual(self.open().session_start_seq(), second["seq"])
        self.assertEqual([e["key"] for e in j.entries("job", since_seq=second["seq"])], ["k"])
        self.assertEqual([e["seq"] for e in j.entries(since_seq=second["seq"])], [second["seq"] + 1, second["seq"] + 2])

    def test_counted_attempts_per_candidate_hash_and_after_reset(self) -> None:
        j = self.open()
        key = "val.cand.USPTO"
        j.record("job", key, "started", attempt=1, detail={"candidate_sha256": SHA_A})
        j.record("job", key, "failed", attempt=1, detail={"outcome": "worker_error", "infra": False})
        j.record("job", key, "started", attempt=2, detail={"candidate_sha256": SHA_B})
        j.record("job", key, "failed", attempt=2, detail={"outcome": "timeout", "infra": False})
        j.record("job", "val.cand.LawBench", "started", attempt=1, detail={"candidate_sha256": SHA_A})
        self.assertEqual([a["attempt"] for a in j.counted_attempts(key, SHA_A)], [1])
        self.assertEqual([a["attempt"] for a in j.counted_attempts(key, SHA_B)], [2])
        self.assertEqual(j.counted_attempts(key, SHA_A)[0]["detail"]["outcome"], "worker_error")
        self.assertEqual(j.counted_attempts(key, SHA_A)[0]["started_detail"], {"candidate_sha256": SHA_A})
        j.record("job", key, "reset", detail={"note": "status --reset-job"})
        self.assertEqual(j.counted_attempts(key, SHA_A), [])
        self.assertEqual(j.counted_attempts(key, SHA_B), [])
        j.record("job", key, "started", attempt=3, detail={"candidate_sha256": SHA_A})
        self.assertEqual([a["attempt"] for a in self.open().counted_attempts(key, SHA_A)], [3])
        self.assertEqual(j.counted_attempts("val.cand.LawBench", SHA_A)[0]["attempt"], 1)


class TestJournalContract(JournalTestCase):
    def test_detail_is_copied_bounded_and_plain_json(self) -> None:
        j = self.open()
        detail = {"paths": ["a"]}
        entry = j.record("workspace", "ensure", "complete", detail=detail)
        detail["paths"].append("b")
        self.assertEqual(j.last("workspace", "ensure")["detail"], {"paths": ["a"]})
        entry["detail"]["paths"].append("c")
        self.assertEqual(j.last("workspace", "ensure")["detail"], {"paths": ["a"]})
        with self.assertRaises(ValueError):
            j.record("job", "k", "started", attempt=1, detail={"x": float("nan")})
        with self.assertRaises(ValueError):
            j.record("job", "k", "started", attempt=1, detail={"blob": "x" * 70_000})     # payloads are not journaled
        with self.assertRaises(ValueError):
            j.record("job", "", "started")
        with self.assertRaises(ValueError):
            j.record("job", "k", "started", attempt=-1)
        self.assertEqual(len(read_journal(self.path)), 1)  # refused records wrote nothing
        self.assertEqual(j.record("job", "k", "started", attempt=1)["seq"], 2)

    def test_unknown_major_version_and_non_increasing_seq_refused(self) -> None:
        self.path.parent.mkdir(parents=True)
        row = {"schema": "mhforge.journal/2", "seq": 1, "op": "run", "key": "x", "status": "started", "attempt": 0,
               "detail": {}}
        self.path.write_text(json.dumps(row) + "\n")
        with self.assertRaises(StateError):
            self.open()
        row["schema"] = "mhforge.journal/1"
        self.path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")
        with self.assertRaises(StateError):
            self.open()
        row2 = dict(row, seq=2, extra_key="ignored")
        self.path.write_text(json.dumps(row) + "\n" + json.dumps(row2) + "\n")
        self.assertEqual(len(self.open().entries()), 2)    # unknown keys are ignored

    def test_concurrent_records_get_distinct_seqs(self) -> None:
        j = self.open()

        def work(n: int) -> None:
            for i in range(25):
                j.record("job", f"k{n}", "started", attempt=i + 1)

        threads = [threading.Thread(target=work, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        seqs = [e["seq"] for e in read_journal(self.path)]
        self.assertEqual(seqs, list(range(1, 101)))


if __name__ == "__main__":
    unittest.main()
