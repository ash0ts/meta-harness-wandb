# SPDX-License-Identifier: MIT
"""Tests for mhforge.telemetry.plan_tables (spec 2.14, 3.23, 5.2)."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import unittest

from mhforge import util
from mhforge.telemetry import join, plan_tables
from tests import helpers

PROJECT = "example-entity/mh-forge-test"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def example(index: int, *, dataset="Symptom2Disease", correct=True, metrics=None, prediction=None, target=None,
            matched=True, finish="stop", calls=1, latency=1.5):
    q = f"question {dataset} {index}"
    target = target if target is not None else f"label{index}"
    prediction = prediction if prediction is not None else (target if correct else "other")
    return {"schema": "mhforge.example/1", "dataset": dataset, "split": "val", "index": index,
            "example_id": "%04d-%s" % (index, sha(q + "\0" + target)[:12]), "example_sha256": sha(q + "\0" + target),
            "question_sha256": sha(q), "prediction": prediction, "prediction_sha256": sha(prediction),
            "target": target, "target_sha256": sha(target), "was_correct": correct,
            "metrics": {} if metrics is None else metrics, "passes": 2, "ambiguous": False, "matched": matched,
            "calls": [], "n_calls": calls, "input_tokens": 100 * calls, "output_tokens": 10 * calls,
            "latency_s": latency, "finish_reason": finish, "truncated_empty": False}


def job_record(candidate, dataset, *, status="succeeded", correct=1, total=2, attempt=1):
    key = f"val.{candidate}.{dataset}"
    return {"schema": "mhforge.job/1", "job_key": key, "split": "val", "candidate": candidate, "dataset": dataset,
            "status": status, "candidate_sha256": "a" * 64,
            "attempts": [{"attempt": a, "job_id": f"{key}.a{a}", "outcome": "ok" if (a == attempt and status ==
                                                                                    "succeeded") else "worker_error"}
                         for a in range(1, attempt + 1)],
            "result": {"accuracy": correct / total if total else 0.0, "correct": correct, "total": total}}


def result(correct, total):
    return {"accuracy": correct / total if total else 0.0, "correct": correct, "total": total}


def conv_url(job_id: str, suffix: str = "", project: str = PROJECT) -> str:
    return f"https://forge.coreweave.com/agent-lens/{project}/conversations/mh-test.{job_id}{suffix}"


def plan(candidate="no_memory", rows=None, *, dataset="Symptom2Disease", res=None, manifest=None, span_urls=None,
         backend="ces", status="succeeded"):
    rows = rows if rows is not None else [example(0), example(1, correct=False)]
    k = sum(1 for r in rows if r["was_correct"])
    res = res if res is not None else result(k, len(rows))
    manifest = manifest if manifest is not None else helpers.make_manifest()
    return plan_tables.plan_tables(candidate, "val", {dataset: job_record(candidate, dataset, status=status,
                                                                          correct=k, total=len(rows))},
                                   {dataset: rows}, {dataset: res}, manifest, span_urls=span_urls or {},
                                   backend=backend)


def column(table, name):
    i = table["columns"].index(name)
    return [r[i] for r in table["rows"]]


class PlanTables(unittest.TestCase):
    def test_inputs_are_strings_and_identical_across_candidates(self):
        rows = [example(i, correct=i % 2 == 0) for i in range(4)]
        a = plan("no_memory", copy.deepcopy(rows))["tables"][0]
        other = copy.deepcopy(rows)
        for r in other:                                # a different candidate predicts differently
            r["prediction"], r["was_correct"] = "x", False
        b = plan("fewshot_all", other)["tables"][0]
        self.assertEqual(a["input_columns"], list(plan_tables.INPUT_COLUMNS))
        n = len(a["input_columns"])
        self.assertEqual([r[:n] for r in a["rows"]], [r[:n] for r in b["rows"]])
        for r in a["rows"] + b["rows"]:
            self.assertTrue(all(isinstance(c, str) and c for c in r[:n]))
        self.assertTrue(a["checks"]["inputs_are_strings"] and b["checks"]["inputs_are_strings"])
        self.assertNotEqual(a["unit"], b["unit"])

    def test_no_null_or_nan_scores_ever(self):
        rows = [example(0, metrics={"jaccard_similarity": 1.0}), example(1, metrics={"jaccard_similarity": math.nan}),
                example(2, metrics={"jaccard_similarity": None}), example(3, correct=False, metrics={})]
        t = plan(rows=rows, dataset="USPTO")["tables"][0]
        self.assertEqual(t["score_columns"], ["was_correct"])
        self.assertIn("jaccard_similarity", t["demoted_scores"])
        for name in t["score_columns"]:
            self.assertTrue(all(c is not None and not (isinstance(c, float) and math.isnan(c)) for c in column(t, name)))
        self.assertTrue(t["checks"]["no_null_or_nan_scores"])
        self.assertIn('"jaccard_similarity":null', column(t, "metrics_json")[1])   # NaN kept only as null in the JSON
        util.canonical_json(t)                         # no NaN anywhere in the plan
        bad = [example(0), example(1)]
        bad[1]["was_correct"] = None                   # a sidecar row without a verdict
        out = plan(rows=bad, res=result(1, 2))
        self.assertEqual(out["tables"], [])
        self.assertIn("no_null_or_nan_scores", out["dropped"][0]["failed_checks"])

    def test_metric_promoted_only_when_complete(self):
        full = [example(0, metrics={"jaccard_similarity": 1.0}), example(1, correct=False,
                                                                        metrics={"jaccard_similarity": 0.25})]
        t = plan(rows=full, dataset="USPTO")["tables"][0]
        self.assertEqual(t["score_columns"], ["was_correct", "jaccard_similarity"])
        self.assertEqual(column(t, "jaccard_similarity"), [1.0, 0.25])
        self.assertEqual(t["column_types"]["jaccard_similarity"], "float")
        part = [example(0, metrics={"jaccard_similarity": 1.0}), example(1, correct=False, metrics={})]
        t = plan(rows=part, dataset="USPTO")["tables"][0]
        self.assertEqual(t["score_columns"], ["was_correct"])
        self.assertEqual(t["demoted_scores"], ["jaccard_similarity"])
        self.assertEqual(json.loads(column(t, "metrics_json")[0]), {"jaccard_similarity": 1.0})

    def test_all_null_output_column_dropped(self):
        rows = [example(0, finish=None, matched=False, calls=0), example(1, correct=False, finish=None)]
        t = plan(rows=rows)["tables"][0]
        self.assertEqual(t["dropped_columns"], ["finish_reason"])
        self.assertNotIn("finish_reason", t["columns"])
        self.assertNotIn("finish_reason", t["column_types"])
        self.assertTrue(all(len(r) == len(t["columns"]) for r in t["rows"]))
        self.assertTrue(t["checks"]["no_all_null_columns"])
        self.assertEqual(column(t, "row_complete"), [False, True])
        self.assertEqual(column(t, "trace_url"), ["", ""])          # empty string, never null, never dropped

    def test_mismatch_with_result_file_drops_table(self):
        rows = [example(0), example(1, correct=False)]
        out = plan(rows=rows, res=result(2, 2))        # the sidecar claims 1 correct, the result file says 2
        self.assertEqual(out["tables"], [])
        self.assertEqual(out["dropped"][0]["key"], "val/Symptom2Disease")
        self.assertEqual(out["dropped"][0]["reason"], "mismatch_with_result_file")
        self.assertEqual(out["dropped"][0]["failed_checks"], ["matches_result_file"])
        self.assertEqual(plan(rows=rows, res=result(1, 3))["dropped"][0]["reason"], "mismatch_with_result_file")
        self.assertEqual(len(plan(rows=rows, res=result(1, 2))["tables"]), 1)
        self.assertEqual(plan(rows=rows, res={})["dropped"][0]["reason"], "mismatch_with_result_file")

    def test_failed_job_has_no_table(self):
        m = helpers.make_manifest()
        recs = {"Symptom2Disease": job_record("x", "Symptom2Disease"),
                "LawBench": job_record("x", "LawBench", status="failed"),
                "USPTO": job_record("x", "USPTO", status="halted")}
        rows = {d: [example(0, dataset=d), example(1, dataset=d, correct=False)] for d in recs}
        out = plan_tables.plan_tables("x", "val", recs, rows, {d: result(1, 2) for d in recs}, m, span_urls={},
                                      backend="ces")
        self.assertEqual([t["key"] for t in out["tables"]], ["val/Symptom2Disease"])
        self.assertEqual(out["dropped"], [])

    def test_fraction_true_equals_result_accuracy(self):
        sidecar = join.parse_sidecar(util.read_jsonl(helpers.FIXTURES / "sidecar_uspto_val.jsonl"))
        records = [json.loads(p.read_text()) for p in sorted((helpers.FIXTURES / "call_records").glob("*.json"))]
        rows = join.join_examples(sidecar, records, dataset="USPTO", split="val")
        k = sum(1 for r in rows if r["was_correct"])
        res = result(k, len(rows))
        rec = job_record("no_memory", "USPTO", correct=k, total=len(rows))
        out = plan_tables.plan_tables("no_memory", "val", {"USPTO": rec}, {"USPTO": rows}, {"USPTO": res},
                                      helpers.make_manifest(), span_urls={}, backend="ces")
        t = out["tables"][0]
        was = column(t, "was_correct")
        self.assertEqual(sum(was) / len(was), res["accuracy"])
        self.assertEqual(t["summary"], {"accuracy": res["accuracy"], "correct": k, "total": len(rows)})
        self.assertTrue(all(t["checks"].values()))

    def test_trace_url_only_from_verified_receipt(self):
        job_id = "val.no_memory.Symptom2Disease.a1"
        good = conv_url(job_id)
        t = plan(span_urls={"Symptom2Disease": good})["tables"][0]
        self.assertEqual(set(column(t, "trace_url")), {good})
        self.assertEqual(set(column(plan(span_urls={"Symptom2Disease": conv_url(job_id, ".e2")})["tables"][0],
                                    "trace_url")), {conv_url(job_id, ".e2")})
        for bad in [None, "", "not a url", conv_url("val.no_memory.Symptom2Disease.a7"),     # another attempt
                    conv_url(job_id, project="other-entity/other"), conv_url(job_id, ".learn"),
                    "https://evil.example.com/agent-lens/" + PROJECT + "/conversations/mh-test." + job_id]:
            t = plan(span_urls={"Symptom2Disease": bad})["tables"][0]
            self.assertEqual(set(column(t, "trace_url")), {""}, bad)
        t = plan(span_urls={"LawBench": good})["tables"][0]        # a URL for another dataset is not this job's
        self.assertEqual(set(column(t, "trace_url")), {""})

    def test_check_table_catches_each_rule(self):
        base = plan(rows=[example(0, metrics={"jaccard_similarity": 1.0}),
                          example(1, correct=False, metrics={"jaccard_similarity": 0.0})], dataset="USPTO")["tables"][0]
        self.assertTrue(all(plan_tables.check_table(base).values()))
        self.assertEqual(set(plan_tables.check_table(base)), set(plan_tables.CHECK_NAMES))

        def mutated(fn):
            p = copy.deepcopy(base)
            fn(p)
            return plan_tables.check_table(p)

        idx = base["columns"].index
        cases = {
            "rows_1_to_10000": [lambda p: p.update(rows=[], row_count=0),
                                lambda p: p.update(rows=p["rows"] * 5001, row_count=10002),
                                lambda p: p.update(row_count=7)],
            "primitive_cells": [lambda p: p["rows"][0].__setitem__(idx("n_calls"), [1]),
                                lambda p: p["rows"][0].__setitem__(idx("latency_s"), math.inf),
                                lambda p: p["rows"][0].pop()],
            "single_type_columns": [lambda p: p["rows"][0].__setitem__(idx("n_calls"), "1"),
                                    lambda p: p["column_types"].__setitem__("latency_s", "int")],
            "no_all_null_columns": [lambda p: [r.__setitem__(idx("finish_reason"), None) for r in p["rows"]]],
            "no_null_or_nan_scores": [lambda p: p["rows"][0].__setitem__(idx("was_correct"), None),
                                      lambda p: p["rows"][1].__setitem__(idx("jaccard_similarity"), None),
                                      lambda p: p["rows"][1].__setitem__(idx("jaccard_similarity"), math.nan)],
            "inputs_are_strings": [lambda p: p["rows"][0].__setitem__(idx("question_sha256"), None),
                                   lambda p: p["rows"][0].__setitem__(idx("example_id"), 7)],
            "matches_result_file": [lambda p: p["summary"].update(correct=2), lambda p: p["summary"].update(total=3),
                                    lambda p: p["summary"].update(correct=None)],
        }
        for rule, fns in cases.items():
            for fn in fns:
                self.assertFalse(mutated(fn)[rule], rule)
        self.assertEqual(plan_tables.check_table({}), {name: False for name in plan_tables.CHECK_NAMES})
        self.assertFalse(any(plan_tables.check_table({"columns": 5, "rows": None}).values()))

    def test_only_the_four_named_metrics_become_scores(self):
        metrics = [{"f1": 1.0, "precision": 1.0, "recall": 1.0, "tp": 1, "fp": 0, "fn": 0, "accuracy": 1.0},
                   {"f1": 0.5, "precision": 0.5, "recall": 0.5, "tp": 1, "fp": 1, "fn": 1, "accuracy": 0.0}]
        rows = [example(0, dataset="LawBench", metrics=metrics[0]),
                example(1, dataset="LawBench", correct=False, metrics=metrics[1])]
        t = plan(rows=rows, dataset="LawBench")["tables"][0]
        self.assertEqual(t["score_columns"], ["was_correct", "f1", "precision", "recall"])
        for name in ("tp", "fp", "fn", "accuracy"):
            self.assertNotIn(name, t["columns"])
            self.assertNotIn(name, t["demoted_scores"])
        self.assertEqual(json.loads(column(t, "metrics_json")[1]), metrics[1])
        self.assertEqual(column(t, "metrics_json")[0], json.dumps(metrics[0], sort_keys=True, separators=(",", ":")))

    def test_backend_unset_when_decision_unset(self):
        for value in (None, "unset"):
            m = helpers.make_manifest(**{"decisions.evaltables_backend": value})
            self.assertEqual(plan_tables.table_backend(m), "unset")
            out = plan(manifest=m, backend=plan_tables.table_backend(m))
            self.assertEqual(out["backend"], "unset")
            self.assertEqual(out["tables"][0]["backend"], "unset")
        self.assertEqual(plan(backend="wandb_table")["tables"][0]["backend"], "wandb_table")
        off = plan(backend="off")
        self.assertEqual((off["backend"], off["tables"], off["dropped"]), ("off", [], []))
        self.assertEqual(plan_tables.table_backend(helpers.make_manifest()), "ces")

    # ---------------------------------------------------------------- second round

    def test_question_sha256_is_an_input_column(self):
        rows = [example(0), example(1, correct=False)]
        t = plan(rows=rows)["tables"][0]
        self.assertIn("question_sha256", t["input_columns"])
        self.assertEqual(column(t, "question_sha256"), [r["question_sha256"] for r in rows])
        text = json.dumps(t)
        self.assertNotIn("question Symptom2Disease", text)          # the question text itself is never in a table

    def test_uspto_logs_hashes_until_cleared(self):
        rows = [example(0, dataset="USPTO", target="CCO.CC(=O)Cl"),
                example(1, dataset="USPTO", correct=False, target="CC(C)N")]
        t = plan(rows=rows, dataset="USPTO")["tables"][0]
        self.assertIn("prediction_sha256", t["output_columns"])
        self.assertIn("target_sha256", t["output_columns"])
        self.assertNotIn("prediction", t["columns"])
        self.assertNotIn("target", t["columns"])
        self.assertNotIn("CCO.CC(=O)Cl", json.dumps(t))
        self.assertEqual(column(t, "target_sha256"), [sha("CCO.CC(=O)Cl"), sha("CC(C)N")])
        cleared = helpers.make_manifest(**{"decisions.uspto_text_cleared": True})
        t = plan(rows=rows, dataset="USPTO", manifest=cleared)["tables"][0]
        self.assertEqual(column(t, "target"), ["CCO.CC(=O)Cl", "CC(C)N"])
        self.assertNotIn("target_sha256", t["columns"])
        t = plan(rows=[example(0), example(1, correct=False)])["tables"][0]      # label datasets show labels
        self.assertEqual(column(t, "target"), ["label0", "label1"])

    def test_long_label_is_hashed(self):
        long_pred = "p" * 201
        rows = [example(0, target="t" * 200, prediction="t" * 200), example(1, correct=False, prediction=long_pred)]
        t = plan(rows=rows)["tables"][0]
        self.assertEqual(column(t, "target")[0], "t" * 200)
        self.assertEqual(column(t, "prediction")[1], sha(long_pred))           # not a known label: hashed (LD5)
        self.assertEqual(plan_tables.LABEL_MAX_CHARS, 200)
        long_target = "t" * 201
        rows = [example(0, target=long_target), example(1, correct=False)]
        t = plan(rows=rows)["tables"][0]
        self.assertEqual(column(t, "target")[0], f"[sha256 {sha(long_target)} chars 201]")
        self.assertEqual(column(t, "prediction")[0], f"[sha256 {sha(long_target)} chars 201]")   # equals its target

    # ---------------------------------------------------------------- LD5: a prediction is shown only as a known label

    def test_prediction_echoing_lawbench_question_is_withheld(self):
        # reviewer case: a model that fails to answer with a label echoes 200 characters of the case text
        case = ("SYNTHETIC LAWBENCH CASE: on the evening of the third the defendant entered the shop on Elm Street, "
                "took two phones from the counter while the clerk was away, and left by the back door before the "
                "police arrived; the phones were later found in his car.")
        echo = case[:200]
        rows = [example(0, dataset="LawBench", target="theft"),
                example(1, dataset="LawBench", correct=False, target="fraud", prediction=echo),
                example(2, dataset="LawBench", correct=False, target="robbery", prediction="theft")]
        out = plan(rows=rows, dataset="LawBench")
        t = out["tables"][0]
        self.assertEqual(len(echo), plan_tables.LABEL_MAX_CHARS)
        self.assertEqual(column(t, "prediction"), ["theft", sha(echo), "theft"])  # a wrong known label stays shown
        self.assertEqual(column(t, "target"), ["theft", "fraud", "robbery"])
        self.assertEqual(t["predictions_withheld"], 1)
        self.assertTrue(all(t["checks"].values()))
        dumped = json.dumps(out)
        self.assertNotIn(echo, dumped)
        self.assertNotIn("defendant entered the shop", dumped)

    def test_prediction_matches_only_targets_of_the_same_table(self):
        rows = [example(0, target="flu"), example(1, correct=False, target="cold", prediction="Flu"),
                example(2, correct=False, target="asthma", prediction="migraine"),
                example(3, correct=False, target="acne", prediction=" cold")]
        t = plan(rows=rows)["tables"][0]
        preds = column(t, "prediction")
        self.assertEqual(preds[0], "flu")                                       # correct predictions are unaffected
        self.assertEqual(preds[1:], [sha("Flu"), sha("migraine"), sha(" cold")])   # exact match only
        self.assertEqual(t["predictions_withheld"], 3)
        self.assertEqual(t["column_types"]["prediction"], "str")
        bad = [example(0), example(1, correct=False, prediction="question text echoed back")]
        bad[1]["prediction_sha256"] = "not a hash"                              # an untrusted sidecar hash
        t = plan(rows=bad)["tables"][0]
        self.assertEqual(column(t, "prediction")[1], sha("question text echoed back"))
        self.assertNotIn("echoed back", json.dumps(t))

    def test_gated_and_all_correct_tables_withhold_nothing(self):
        rows = [example(0, dataset="USPTO", target="CCO"), example(1, dataset="USPTO", correct=False, target="CCN")]
        t = plan(rows=rows, dataset="USPTO")["tables"][0]
        self.assertEqual(t["predictions_withheld"], 0)
        self.assertEqual(column(t, "prediction_sha256"), [sha("CCO"), sha("other")])
        t = plan(rows=[example(0), example(1)])["tables"][0]
        self.assertEqual((t["predictions_withheld"], column(t, "prediction")), (0, ["label0", "label1"]))

    # ---------------------------------------------------------------- further contracts

    def test_plan_shape_and_determinism(self):
        a, b = plan(), plan()
        self.assertEqual(util.canonical_json(a), util.canonical_json(b))
        self.assertEqual(a["schema"], "mhforge.tableplanset/1")
        self.assertEqual((a["unit"], a["placeholder_project"], a["export_attempt"]), ("eval:val.no_memory", False, 1))
        t = a["tables"][0]
        self.assertEqual(t["schema"], "mhforge.tableplan/1")
        self.assertEqual((t["job_id"], t["run_ref"], t["key"], t["step"]),
                         ("val.no_memory.Symptom2Disease.a1", "eval:val.no_memory", "val/Symptom2Disease", 0))
        self.assertEqual(t["columns"], t["input_columns"] + t["output_columns"] + t["score_columns"])
        self.assertEqual(t["output_columns"], ["prediction", "target", "n_calls", "input_tokens", "output_tokens",
                                               "latency_s", "finish_reason", "truncated_empty", "row_complete",
                                               "metrics_json", "trace_url"])
        self.assertEqual(t["row_count"], 2)

    def test_placeholder_project_when_entity_unset(self):
        m = helpers.make_manifest(**{"decisions.entity": None})
        url = conv_url("val.no_memory.Symptom2Disease.a1", project=plan_tables.PLACEHOLDER_PROJECT)
        out = plan(manifest=m, span_urls={"Symptom2Disease": url})
        self.assertTrue(out["placeholder_project"])
        self.assertEqual(set(column(out["tables"][0], "trace_url")), {url})

    def test_succeeded_attempt_names_the_table(self):
        rows = [example(0), example(1, correct=False)]
        rec = job_record("x", "LawBench", attempt=2)
        out = plan_tables.plan_tables("x", "val", {"LawBench": rec}, {"LawBench": rows}, {"LawBench": result(1, 2)},
                                      helpers.make_manifest(), span_urls={}, backend="ces")
        self.assertEqual(out["tables"][0]["job_id"], "val.x.LawBench.a2")

    def test_hostile_sidecar_values_never_reach_a_table(self):
        text = "A defendant stole a car with plate ABC123 on the night of the third"
        rows = [example(0, dataset="LawBench", metrics={"f1": 1.0, "note": text}),
                example(1, dataset="LawBench", correct=False, metrics={"f1": 0.0, text: 1})]
        t = plan(rows=rows, dataset="LawBench")["tables"][0]
        self.assertNotIn(text, json.dumps(t))
        self.assertEqual(json.loads(column(t, "metrics_json")[0]), {"f1": 1.0})
        rows = [example(0, dataset="USPTO"), example(1, dataset="USPTO", correct=False)]
        rows[0]["prediction_sha256"] = "CCO.CC(=O)Cl gold string"         # not a hash: never a hash cell
        t = plan(rows=rows, dataset="USPTO")["tables"][0]
        self.assertNotIn("gold string", json.dumps(t))
        self.assertIsNone(column(t, "prediction_sha256")[0])
        rows = [example(0), example(1, correct=False)]
        rows[0]["question_sha256"] = text                                  # an input that is not a hash drops it
        out = plan(rows=rows)
        self.assertEqual(out["tables"], [])
        self.assertIn("inputs_are_strings", out["dropped"][0]["failed_checks"])
        self.assertNotIn(text, json.dumps(out))
        rows = [example(0, dataset="USPTO", metrics={"jaccard_similarity": 10 ** 400}),
                example(1, dataset="USPTO", correct=False, metrics={"jaccard_similarity": 0.0})]
        t = plan(rows=rows, dataset="USPTO")["tables"][0]                  # no OverflowError; the metric is demoted
        self.assertEqual((t["score_columns"], t["demoted_scores"]), (["was_correct"], ["jaccard_similarity"]))

    def test_bad_identifiers_raise(self):
        with self.assertRaises(ValueError):
            plan("Bad-Name")
        with self.assertRaises(ValueError):
            plan_tables.plan_tables("x", "train", {}, {}, {}, helpers.make_manifest(), span_urls={}, backend="ces")


if __name__ == "__main__":
    unittest.main()
