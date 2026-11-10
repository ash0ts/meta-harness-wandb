# SPDX-License-Identifier: MIT
import copy
import json
import unittest

from mhforge import util
from mhforge.errors import StateError
from mhforge.telemetry import join
from tests import helpers

SIDECAR = helpers.FIXTURES / "sidecar_uspto_val.jsonl"
RECORDS = helpers.FIXTURES / "call_records"
OUTCOMES = ("completed", "replayed", "refused", "provider_rejected", "failed", "truncated_empty")
RESULT = {"accuracy": 0.5, "correct": 2, "total": 4, "dataset": "USPTO"}      # what upstream's val.json reports
EXAMPLE_KEYS = {"schema", "dataset", "split", "index", "example_id", "example_sha256", "question_sha256", "prediction",
                "prediction_sha256", "target", "target_sha256", "was_correct", "metrics", "passes", "ambiguous",
                "matched", "calls", "n_calls", "input_tokens", "output_tokens", "latency_s", "finish_reason",
                "truncated_empty"}
CALL_KEYS = {"user_sha256", "call_id", "outcome", "input_tokens", "output_tokens", "latency_s", "finish_reason",
             "truncated_empty"}


def load_rows():
    return util.read_jsonl(SIDECAR)


def load_records():
    return {name: json.loads((RECORDS / f"{name}.json").read_text()) for name in OUTCOMES}


def by_kind(rows, kind):
    return [r for r in rows if r["kind"] == kind]


def evals_of(rows, index):
    return [r for r in rows if r["kind"] == "eval" and r["split"] == "val" and r["index"] == index]


def join_all(rows, records, split="val"):
    sc = join.parse_sidecar(rows)
    recs = list(records.values()) if isinstance(records, dict) else list(records)
    return (sc, join.join_examples(sc, recs, dataset="USPTO", split=split), join.call_roles(sc, recs, split=split),
            join.join_problems(sc, recs, split=split))


class Fixtures(unittest.TestCase):
    def test_fixtures_follow_the_records_of_2_5_and_2_8(self):
        recs = load_records()
        self.assertEqual({r["outcome"] for r in recs.values()},
                         {"completed", "replayed", "refused", "provider_rejected", "failed"})
        self.assertTrue(recs["truncated_empty"]["flags"]["truncated_empty"])
        for name, r in recs.items():
            self.assertEqual(r["schema"], "mhforge.call/1")
            self.assertRegex(r["call_id"], r"c-\d{8}T\d{6}-[0-9a-f]{12}")
            self.assertNotIn("authorization", json.dumps(r).lower())
            self.assertIn("request_excerpt" if name == "refused" else "request", r)
        rows = load_rows()
        self.assertEqual(rows[0]["schema"], "mhforge.sidecar/1")
        self.assertEqual([r["seq"] for r in rows], list(range(1, len(rows) + 1)))
        val = [r for r in by_kind(rows, "eval") if r["split"] == "val"]
        self.assertEqual(sorted({r["index"] for r in val}), [0, 1, 2, 3])
        self.assertEqual(sorted({r["pass"] for r in val}), [1, 2])


class Join(unittest.TestCase):
    def test_misses_join_by_user_hash_in_order(self):
        sc, ex, roles, problems = join_all(load_rows(), load_records())
        self.assertEqual(problems, [])
        self.assertEqual([r["index"] for r in ex], [0, 1, 2, 3])
        recs = load_records()
        self.assertEqual([r["calls"][0]["call_id"] for r in ex],
                         [recs[n]["call_id"] for n in ("completed", "truncated_empty", "replayed", "refused")])
        for row in ex:
            self.assertEqual(set(row), EXAMPLE_KEYS)
            self.assertEqual(row["schema"], "mhforge.example/1")
            self.assertEqual(row["example_id"], "%04d-%s" % (row["index"], row["example_sha256"][:12]))
            self.assertEqual(row["n_calls"], 1)                    # pass 2 is a cache hit on the same record
            for c in row["calls"]:
                self.assertEqual(set(c), CALL_KEYS)
        self.assertEqual(ex[0]["input_tokens"], 367)
        self.assertEqual(ex[0]["output_tokens"], 912)
        self.assertAlmostEqual(ex[0]["latency_s"], 20.411)
        # two misses on one prompt hash take the records of that hash in received order
        rows, recs = load_rows(), load_records()
        h0 = recs["completed"]["user_sha256"]
        for r in rows:
            if r["kind"] == "call" and r["user_sha256"] == recs["truncated_empty"]["user_sha256"]:
                r["user_sha256"] = h0
        later = copy.deepcopy(recs["completed"])
        later["call_id"] = "c-20261110T031531-0000000000b1"
        later["received_utc"], later["ended_utc"] = "2026-11-10T03:15:31.000000Z", "2026-11-10T03:15:40.000000Z"
        later["usage"]["prompt_tokens"] = 999
        recs["truncated_empty"] = later
        _, ex, _, problems = join_all(rows, list(reversed(list(recs.values()))))   # input order does not matter
        self.assertEqual(problems, [])
        self.assertEqual(ex[0]["calls"][0]["call_id"], recs["completed"]["call_id"])
        self.assertEqual(ex[1]["calls"][0]["call_id"], later["call_id"])
        self.assertEqual(ex[1]["calls"][0]["input_tokens"], 999)
        # its pass-2 cache hit refers to the record first taken for that hash
        self.assertEqual([c["call_id"] for c in ex[1]["calls"]], [later["call_id"], recs["completed"]["call_id"]])

    def test_keep_last_pass_matches_result_file(self):
        _, ex, _, _ = join_all(load_rows(), load_records())
        self.assertEqual(join.check_examples(ex, RESULT), [])
        self.assertEqual([r["passes"] for r in ex], [2, 2, 2, 2])
        rows = load_rows()
        last = max(evals_of(rows, 0), key=lambda r: r["pass"])
        last.update(was_correct=False, prediction="SYNTHETIC-WRONG", prediction_sha256="0" * 64,
                    metrics={"jaccard_similarity": 0.25})
        _, ex, _, _ = join_all(rows, load_records())
        self.assertFalse(ex[0]["was_correct"])
        self.assertEqual(ex[0]["prediction"], "SYNTHETIC-WRONG")
        self.assertEqual(ex[0]["metrics"], {"jaccard_similarity": 0.25})
        self.assertEqual(join.check_examples(ex, dict(RESULT, correct=1)), [])
        self.assertEqual(len(join.check_examples(ex, RESULT)), 1)

    def test_batch_hashes_join(self):
        rows, recs = load_rows(), load_records()
        _, ex, roles, _ = join_all(rows, recs)
        self.assertEqual(evals_of(rows, 3)[0]["call_seqs"], [])
        self.assertTrue(ex[3]["matched"])
        self.assertEqual([c["call_id"] for c in ex[3]["calls"]], [recs["refused"]["call_id"]])
        self.assertEqual(ex[3]["calls"][0]["outcome"], "refused")
        self.assertIsNone(ex[3]["calls"][0]["input_tokens"])
        self.assertEqual(roles[recs["refused"]["call_id"]], ("validate", ex[3]["example_id"]))
        for r in evals_of(rows, 3):
            r["batch_user_sha256"] = []
        _, ex, roles, _ = join_all(rows, recs)
        self.assertFalse(ex[3]["matched"])
        self.assertEqual(ex[3]["calls"], [])
        self.assertEqual(roles[recs["refused"]["call_id"]][0], "other")

    def test_roles_learn_validate_test_other(self):
        recs = load_records()
        _, ex, roles, _ = join_all(load_rows(), recs)
        ids = {r["index"]: r["example_id"] for r in ex}
        self.assertEqual(roles, {
            recs["provider_rejected"]["call_id"]: ("learn", None),
            recs["failed"]["call_id"]: ("learn", None),
            recs["completed"]["call_id"]: ("validate", ids[0]),
            recs["truncated_empty"]["call_id"]: ("validate", ids[1]),
            recs["replayed"]["call_id"]: ("validate", ids[2]),
            recs["refused"]["call_id"]: ("validate", ids[3]),
        })
        # an unclaimed record received after the first validation row is "other"
        stray = copy.deepcopy(recs["completed"])
        stray.update(call_id="c-20261110T040000-0000000000c1", user_sha256="f" * 64,
                     received_utc="2026-11-10T04:00:00.000000Z", ended_utc="2026-11-10T04:00:01.000000Z")
        early = copy.deepcopy(stray)
        early.update(call_id="c-20261110T000000-0000000000c2", received_utc="2026-11-10T00:00:00.000000Z",
                     ended_utc="2026-11-10T00:00:01.000000Z")
        _, _, roles, _ = join_all(load_rows(), list(recs.values()) + [stray, early])
        self.assertEqual(roles[stray["call_id"]], ("other", None))
        self.assertEqual(roles[early["call_id"]], ("learn", None))     # before the first validation row
        # the same job as a test job: attached records take the role "test"
        rows = load_rows()
        for r in rows:
            if r["kind"] == "eval" and r["split"] == "val":
                r["split"] = "test"
        _, ex, roles, _ = join_all(rows, recs, split="test")
        self.assertEqual([r["split"] for r in ex], ["test"] * 4)
        self.assertEqual(roles[recs["completed"]["call_id"]][0], "test")
        self.assertEqual(roles[recs["failed"]["call_id"]][0], "learn")
        # asking for a split the job did not evaluate gives no rows
        self.assertEqual(join_all(load_rows(), recs, split="test")[1], [])

    def test_replayed_call_reports_source_usage(self):
        recs = load_records()
        self.assertEqual(recs["replayed"]["cost_micro"], 0)
        _, ex, _, _ = join_all(load_rows(), recs)
        call = ex[2]["calls"][0]
        self.assertEqual(call["outcome"], "replayed")
        self.assertEqual((call["input_tokens"], call["output_tokens"]), (380, 640))
        self.assertEqual((ex[2]["input_tokens"], ex[2]["output_tokens"]), (380, 640))
        self.assertEqual(ex[2]["finish_reason"], "stop")
        self.assertTrue(ex[1]["truncated_empty"])
        self.assertEqual(ex[1]["finish_reason"], "length")
        self.assertFalse(ex[0]["truncated_empty"])

    def test_unmatched_and_ambiguous_marked(self):
        rows, recs = load_rows(), load_records()
        for r in evals_of(rows, 1):
            r["ambiguous"] = True
        del recs["completed"]
        _, ex, roles, problems = join_all(rows, recs)
        self.assertFalse(ex[0]["matched"])                         # no reachable record
        self.assertEqual((ex[0]["calls"], ex[0]["n_calls"], ex[0]["input_tokens"], ex[0]["latency_s"]), ([], 0, 0, 0))
        self.assertIn("miss_without_record:6", problems)
        self.assertTrue(ex[1]["ambiguous"])
        self.assertFalse(ex[1]["matched"])
        self.assertEqual((ex[1]["calls"], ex[1]["input_tokens"], ex[1]["output_tokens"]), ([], 0, 0))
        self.assertEqual(roles[recs["truncated_empty"]["call_id"]], ("validate", None))
        self.assertTrue(ex[2]["matched"] and ex[3]["matched"])

    def test_problems_listed(self):
        rows, recs = load_rows(), load_records()
        rows = [r for r in rows if r["kind"] != "done"]                          # killed worker
        stray = copy.deepcopy(recs["completed"])
        stray.update(call_id="c-20261110T040000-0000000000d1", user_sha256="e" * 64)
        open_rec = copy.deepcopy(recs["completed"])
        open_rec["call_id"] = "c-20261110T040001-0000000000d2"
        for k in ("outcome", "ended_utc", "reply"):
            del open_rec[k]
        del recs["replayed"]                                                     # a miss with no call record
        sc, ex, roles, problems = join_all(rows, list(recs.values()) + [stray, open_rec])
        self.assertIn("no_done_row", sc["problems"])
        self.assertEqual(sorted(problems), sorted([
            "no_done_row", f"unclaimed_call:{stray['call_id']}", "miss_without_record:10",
            f"admission_only:{open_rec['call_id']}"]))
        bad = load_rows() + [{"kind": "call", "seq": 3}, "not a row", {"kind": "weird", "seq": 99},
                             {"kind": "eval", "seq": 98, "pass": "x"}]
        sc = join.parse_sidecar(bad)
        self.assertEqual(sorted(sc["problems"]), sorted(["sidecar_duplicate_seq:3", "sidecar_bad_row:23",
                                                         "sidecar_unknown_kind:99", "sidecar_bad_eval:98"]))
        meta = dict(load_rows()[0], schema="mhforge.sidecar/2")
        with self.assertRaises(StateError):
            join.parse_sidecar([meta])

    def test_no_sidecar_gives_empty_examples(self):
        recs = list(load_records().values())
        for sidecar in (join.parse_sidecar([]), None, {}):
            self.assertEqual(join.join_examples(sidecar, recs, dataset="USPTO", split="val"), [])
            roles = join.call_roles(sidecar, recs, split="val")
            self.assertEqual(roles, {r["call_id"]: ("other", None) for r in recs})
        self.assertEqual(join.parse_sidecar([])["problems"], [])
        self.assertEqual(join.join_problems(None, recs, split="val"), ["no_sidecar"])

    def test_check_examples_against_result(self):
        _, ex, _, _ = join_all(load_rows(), load_records())
        self.assertEqual(join.check_examples(ex, RESULT), [])
        self.assertEqual(join.check_examples(ex, dict(RESULT, total=5)), ["count 4 != result total 5"])
        self.assertEqual(join.check_examples(ex, dict(RESULT, correct=3)), ["correct 2 != result correct 3"])
        self.assertEqual(join.check_examples(ex[:3], RESULT), ["count 3 != result total 4"])
        self.assertEqual(sorted(join.check_examples(ex, {})), ["result_correct_missing", "result_total_missing"])

    def test_admission_only_record_is_a_problem_not_a_call(self):
        rows, recs = load_rows(), load_records()
        open_rec = recs.pop("completed")
        for k in ("outcome", "ended_utc", "reply"):
            open_rec.pop(k)
        before = copy.deepcopy(open_rec)
        sc, ex, roles, problems = join_all(rows, list(recs.values()) + [open_rec])
        self.assertIn(f"admission_only:{open_rec['call_id']}", problems)
        self.assertIn("miss_without_record:6", problems)               # the miss is not joined to it
        self.assertFalse(ex[0]["matched"])
        self.assertNotIn(open_rec["call_id"], [c["call_id"] for r in ex for c in r["calls"]])
        self.assertEqual(roles[open_rec["call_id"]], ("other", None))
        self.assertEqual(open_rec, before)

    def test_join_never_mutates_its_inputs(self):
        rows, recs = load_rows(), load_records()
        sc = join.parse_sidecar(rows)
        frozen_rows, frozen_recs, frozen_sc = copy.deepcopy(rows), copy.deepcopy(recs), copy.deepcopy(sc)
        ex = join.join_examples(sc, list(recs.values()), dataset="USPTO", split="val")
        join.call_roles(sc, list(recs.values()), split="val")
        ex[0]["metrics"]["x"] = 1
        self.assertEqual((rows, recs, sc), (frozen_rows, frozen_recs, frozen_sc))

    def test_eval_row_of_the_wrong_shape_is_left_out(self):
        for field, value in (("was_correct", "false"), ("prediction", {"x": 1}), ("target", 7),
                             ("example_sha256", {"a": 1}), ("prediction_sha256", "zz"), ("metrics", [1])):
            rows = load_rows()
            bad = evals_of(rows, 1)[-1]
            bad[field] = value
            sc = join.parse_sidecar(rows)
            self.assertIn(f"sidecar_bad_eval:{bad['seq']}", sc["problems"], field)
            ex = join.join_examples(sc, list(load_records().values()), dataset="USPTO", split="val")
            self.assertEqual(ex[1]["passes"], 1, field)            # the bad pass-2 row is not trusted
            self.assertIsInstance(ex[1]["prediction"], str)
            self.assertRegex(ex[1]["example_id"], r"[0-9]{4}-[0-9a-f]{12}")


if __name__ == "__main__":
    unittest.main()
