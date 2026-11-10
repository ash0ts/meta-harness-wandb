# SPDX-License-Identifier: MIT
"""Tests for mhforge.telemetry.plan_runs (spec 2.15, 3.23, 5.2)."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from mhforge import manifest as manifest_mod
from mhforge import scan, util
from mhforge.telemetry import plan_runs
from tests import helpers

DATASETS = ("USPTO", "Symptom2Disease", "LawBench")
QUOTE = "A patient reports a persistent dry cough and mild fever for three days running now."


def job_record(candidate, dataset, *, status="succeeded", accuracy=0.5, cost=1000, truncated=0):
    key = f"val.{candidate}.{dataset}"
    return {"schema": "mhforge.job/1", "job_key": key, "candidate": candidate, "dataset": dataset, "status": status,
            "candidate_sha256": "c" * 64,
            "attempts": [{"attempt": 1, "job_id": f"{key}.a1", "outcome": "ok" if status == "succeeded" else
                          "worker_error", "gateway": {"cost_micro": cost, "truncated_empty": truncated}}],
            "result": {"accuracy": accuracy, "correct": 1, "total": 2} if status == "succeeded" else None}


def cand(name, accs, *, valid=True, upstream=None, quarantined=False, ctx=None):
    datasets = {}
    for ds, acc in accs.items():
        datasets[ds] = {"status": "succeeded", "accuracy": acc} if acc is not None else \
            {"status": "failed", "outcome": "worker_error", "failure_class": "context_limit"}
    return {"name": name, "valid": valid, "rejected": None if valid else "import", "base_system": "fewshot_all",
            "axis": "exploration", "hypothesis": "h", "components": ["c"], "candidate_sha256": "d" * 64,
            "datasets": datasets, "upstream_avg_val": upstream, "quarantined": quarantined,
            "context": ctx or {"memory_context_chars_avg": 100, "prompt_tokens_max": 50}}


def iteration(n, cands, *, solver=1000, sandbox=2000, proposer=3000, frontier=None, exposed=False):
    return {"schema": "mhforge.iteration/1", "iteration": n, "kind": "baselines" if n == 0 else "scored",
            "frontier_before": {"system": "fewshot_all", "val_accuracy": frontier} if frontier is not None else None,
            "candidates": cands, "evidence_exposed": exposed,
            "timing_s": {"propose": 10.5, "bench": 20.25, "wall": 31.0},
            "solver_cost_micro": solver, "sandbox_cost_micro": sandbox, "proposer_list_price_micro": proposer}


def baselines():
    return iteration(0, [cand("no_memory", {d: 0.2 for d in DATASETS}, upstream=20.0),
                         cand("fewshot_all", {d: 0.4 for d in DATASETS}, upstream=40.0)], proposer=0)


def entry(name="x", **kw):
    e = {"name": name, "file": f"candidates/{name}.py", "sha256": "e" * 64, "base_system": "fewshot_all",
         "axis": "exploration", "hypothesis": "retrieve more examples", "components": ["retriever"],
         "valid_name": True, "bytes": 1408}
    e.update(kw)
    return e


def session(iteration_n=3, ordinal=1):
    return {"schema": "mhforge.session/1", "campaign": "mh-test", "iteration": iteration_n, "ordinal": ordinal,
            "session_key": f"iter{iteration_n:03d}.s{ordinal}", "outcome": "ok", "cli": {"path": "/Users/someone/claude"}}


def eval_plan(candidate="no_memory", *, is_baseline=True, manifest=None, records=None, **kw):
    manifest = manifest or helpers.make_manifest()
    records = records if records is not None else {d: job_record(candidate, d) for d in DATASETS}
    args = dict(proposal_entry=None, conversation_urls={}, is_baseline=is_baseline, evidence={})
    args.update(kw)
    return plan_runs.plan_evaluate_run(candidate, "val", records, [f"val/{d}" for d in DATASETS], manifest, **args)


def flat_paths(value, prefix=""):
    if isinstance(value, dict) and value:
        out = []
        for k, v in value.items():
            out += flat_paths(v, f"{prefix}.{k}" if prefix else k)
        return out
    return [prefix]


class RunIds(unittest.TestCase):
    def test_run_ids_are_deterministic(self):
        rid = plan_runs.run_id
        self.assertEqual(rid("mh-test", "search"), "mh-test-search")
        self.assertEqual(rid("mh-test", "search", export_attempt=3), "mh-test-search")
        self.assertEqual(rid("mh-test", "publication"), "mh-test-publication")
        self.assertEqual(rid("mh-test", "propose", candidate="x"), "mh-test-propose-x")
        self.assertEqual(rid("mh-test", "propose", candidate="x", export_attempt=2), "mh-test-propose-x-e2")
        self.assertEqual(rid("mh-test", "evaluate", candidate="no_memory", split="val"), "mh-test-eval-val-no_memory")
        self.assertEqual(rid("mh-test", "evaluate", candidate="no_memory", split="test", export_attempt=3),
                         "mh-test-eval-test-no_memory-e3")
        self.assertEqual(rid("mh-test", "evaluate", candidate="x", split="val"),
                         rid("mh-test", "evaluate", candidate="x", split="val"))
        long_c, long_camp = "a" * 48, "c" * 40
        long_id = rid(long_camp, "evaluate", candidate=long_c, split="test", export_attempt=3)
        self.assertLessEqual(len(long_id), 128)
        self.assertEqual(long_id, rid(long_camp, "evaluate", candidate=long_c, split="test", export_attempt=3))
        for bad in [dict(kind="evaluate", candidate="x", split="train"), dict(kind="propose", candidate="X"),
                    dict(kind="propose", candidate="x\n"), dict(kind="nope")]:
            kind = bad.pop("kind")
            with self.assertRaises(ValueError):
                rid("mh-test", kind, **bad)
        with self.assertRaises(ValueError):
            rid("MH", "search")
        with self.assertRaises(ValueError):
            rid("mh-test", "search", export_attempt=0)


class EvaluateAndPropose(unittest.TestCase):
    def test_candidate_artifact_metadata_carries_base_system(self):
        p = plan_runs.plan_propose_run(entry(), session(), helpers.make_manifest())
        self.assertEqual(p["unit"], "propose:x")
        self.assertEqual(p["run"]["id"], "mh-test-propose-x")
        self.assertEqual(p["run"]["job_type"], "propose")
        log = p["run"]["artifacts"][0]
        self.assertEqual((log["op"], log["name"], log["type"]), ("log", "candidate-x", "harness-candidate"))
        md = log["metadata"]
        self.assertEqual(md["base_system"], "fewshot_all")
        self.assertEqual((md["iteration"], md["axis"], md["hypothesis"], md["components"], md["file_sha256"]),
                         (3, "exploration", "retrieve more examples", ["retriever"], "e" * 64))
        self.assertEqual(md["upstream_commit"], helpers.make_manifest()["upstream"]["commit"])
        self.assertEqual(log["files"], [{"source": "proposer/iter-003/s1/candidates/x.py", "name": "x.py",
                                         "sha256": "e" * 64, "bytes": 1408}])
        use = p["run"]["artifacts"][1]
        self.assertEqual((use["op"], use["name"], use.get("best_effort")), ("use", "candidate-fewshot_all", True))
        self.assertEqual(p["expect"]["artifacts"], ["candidate-x"])     # a failed best-effort use is not a failure
        self.assertEqual(p["run"]["config"]["base_system"], "fewshot_all")
        self.assertNotIn("/Users/", json.dumps(p))
        p = plan_runs.plan_propose_run(entry(base_system=None), session(), helpers.make_manifest())
        self.assertEqual(len(p["run"]["artifacts"]), 1)

    def test_baseline_eval_run_logs_artifact(self):
        p = eval_plan("no_memory", is_baseline=True)
        run = p["run"]
        self.assertEqual((p["unit"], run["id"], run["job_type"], run["tags"], run["resume"]),
                         ("eval:val.no_memory", "mh-test-eval-val-no_memory", "evaluate", ["baseline"], False))
        art = run["artifacts"][0]
        self.assertEqual((art["op"], art["name"], art["aliases"]), ("log", "candidate-no_memory", ["mh-test"]))
        self.assertEqual(art["files"][0]["source"], "workspace/text_classification/agents/no_memory.py")
        self.assertEqual(art["files"][0]["sha256"], "c" * 64)
        self.assertIsNone(art["metadata"]["base_system"])
        self.assertEqual(p["expect"], {"summary_markers": [f"val/{d}" for d in DATASETS],
                                       "artifacts": ["candidate-no_memory"]})
        self.assertEqual(run["tables"], [f"val/{d}" for d in DATASETS])
        cand_plan = eval_plan("x", is_baseline=False, proposal_entry=entry())
        arts = cand_plan["run"]["artifacts"]
        self.assertEqual([(a["op"], a["name"]) for a in arts], [("use", "candidate-x")])
        self.assertEqual(cand_plan["run"]["tags"], ["candidate"])
        self.assertEqual(cand_plan["run"]["config"]["candidate_sha256"], "e" * 64)

    def test_eval_summary_figures(self):
        recs = {"USPTO": job_record("x", "USPTO", accuracy=0.1, cost=6480, truncated=1),
                "Symptom2Disease": job_record("x", "Symptom2Disease", accuracy=0.5, cost=20),
                "LawBench": job_record("x", "LawBench", status="failed", cost=500)}
        s = eval_plan("x", is_baseline=False, records=recs)["run"]["summary"]
        self.assertEqual(s["val/USPTO/accuracy"], 0.1)
        self.assertNotIn("val/LawBench/accuracy", s)
        self.assertEqual(s["val/macro_accuracy_all_datasets"], 0.2)
        self.assertEqual((s["datasets_complete"], s["datasets_failed"]), (2, 1))
        self.assertEqual((s["solver_cost_usd"], s["truncated_empty_calls"]), (0.007, 1))
        urls = {"USPTO": "https://forge.coreweave.com/agent-lens/e/p/conversations/mh-test.val.x.USPTO.a1",
                "LawBench": "file:///Users/someone/x"}
        s = eval_plan("x", is_baseline=False, records=recs, conversation_urls=urls)["run"]["summary"]
        self.assertEqual(json.loads(s["conversation_urls"]), {"USPTO": urls["USPTO"]})

    def test_huge_int_accuracy_is_not_a_finite_number(self):
        recs = {"USPTO": job_record("x", "USPTO", accuracy=10 ** 400)}
        s = eval_plan("x", is_baseline=False, records=recs)["run"]["summary"]
        self.assertNotIn("val/USPTO/accuracy", s)
        self.assertEqual(s["datasets_complete"], 0)

    def test_run_config_is_allow_list_without_paths_or_account_facts(self):
        m = helpers.make_manifest(**{"upstream.example_dir": "/Users/someone/rsi/reference_examples/tc",
                                     "upstream.worker_python": "/Users/someone/rsi/.venv/bin/python",
                                     "state_dir": "/Users/someone/state", "proposer.cli_path": "/Users/someone/claude",
                                     "account": {"plan": "enterprise", "headroom_usd": "123"},
                                     "telemetry.content.USPTO.span_prompt": "hash",
                                     "sandbox.image": "registry.example.com/x/worker@sha256:" + "0" * 64})
        m["notes"] = "personal note /Users/someone"
        plans = [eval_plan("no_memory", manifest=m), plan_runs.plan_propose_run(entry(), session(), m),
                 plan_runs.plan_search_step(baselines(), [], m)]
        identity = {"candidate", "split", "iteration", "base_system", "candidate_sha256", "upstream_commit",
                    "manifest_sha256", "session_key"}
        for p in plans:
            config = p["run"]["config"]
            for path in flat_paths(config):
                allowed = any(path == a or path.startswith(a + ".") for a in plan_runs.RUN_CONFIG_ALLOW)
                self.assertTrue(path in identity or allowed, path)
            text = json.dumps(config)
            for forbidden in ("/Users/", "someone", "example_dir", "worker_python", "state_dir", "tar_path",
                              "scratch_root", "cli_path", "api_key_env", "auth_env", "MHFORGE_", "enterprise",
                              "headroom", "notes", "personal"):
                self.assertNotIn(forbidden, text, forbidden)
        cfg = plans[2]["run"]["config"]
        self.assertEqual(cfg["campaign"], "mh-test")
        self.assertEqual(cfg["upstream"]["expect"], m["upstream"]["expect"])
        self.assertEqual(cfg["telemetry"], {"content": m["telemetry"]["content"]})
        self.assertEqual(cfg["sandbox"]["wheelhouse"], {"requirements_sha256": m["sandbox"]["wheelhouse"][
            "requirements_sha256"], "sha256sums_sha256": None})
        self.assertEqual(plans[0]["run"]["config"]["manifest_sha256"], manifest_mod.manifest_sha256(m))
        leaky = helpers.make_manifest(**{"routing.note": "/Users/someone/secret-dir"})   # inside an allowed subtree
        self.assertNotIn("someone", json.dumps(plan_runs.plan_search_step(baselines(), [], leaky)["run"]["config"]))

    def test_withheld_candidate_file_logged_by_hash_only(self):
        held = {"x.py": {"sha256": "f" * 64, "bytes": 999}}
        p = plan_runs.plan_propose_run(entry(), session(), helpers.make_manifest(), withheld=held)
        art = p["run"]["artifacts"][0]
        self.assertEqual(art["files"], [])
        self.assertEqual(art["withheld_files"], [{"name": "x.py", "sha256": "f" * 64, "bytes": 999}])
        self.assertEqual((art["metadata"]["file_sha256"], art["metadata"]["file_bytes"]), ("f" * 64, 999))
        self.assertEqual(p["expect"]["artifacts"], ["candidate-x"])
        self.assertNotIn("candidates/x.py", json.dumps(p))
        base = eval_plan("no_memory", withheld={"no_memory.py": {"sha256": "1" * 64, "bytes": 5}})
        art = base["run"]["artifacts"][0]
        self.assertEqual((art["files"], art["withheld_files"][0]["sha256"]), ([], "1" * 64))
        clean = plan_runs.plan_propose_run(entry(), session(), helpers.make_manifest(), withheld={"y.py": {}})
        self.assertEqual(len(clean["run"]["artifacts"][0]["files"]), 1)

    def test_evidence_is_hashes_and_sizes_only(self):
        evidence = {"USPTO": {"memory_json_sha256": "2" * 64, "memory_json_bytes": 2, "log_jsonl_sha256": "3" * 64,
                              "log_jsonl_bytes": 5120, "memory_json": "{\"train\": \"verbatim row\"}",
                              "path": "/Users/someone/state/x/memory.json"},
                    "LawBench": {"memory_json_sha256": "not-a-hash", "memory_json_bytes": "12",
                                 "log_jsonl_sha256": "4" * 64, "log_jsonl_bytes": 7}}
        p = eval_plan(evidence=evidence)
        got = json.loads(p["run"]["summary"]["evidence_hashes"])
        for values in got.values():
            self.assertEqual(values.pop('sidecar_jsonl_sha256'), None)
            self.assertEqual(values.pop('sidecar_jsonl_bytes'), None)
            self.assertEqual(values.pop('result_json_sha256'), None)
            self.assertEqual(values.pop('result_json_bytes'), None)
        self.assertEqual(got["USPTO"], {"memory_json_sha256": "2" * 64, "memory_json_bytes": 2,
                                        "log_jsonl_sha256": "3" * 64, "log_jsonl_bytes": 5120})
        self.assertEqual(got["LawBench"], {"memory_json_sha256": None, "memory_json_bytes": None,
                                           "log_jsonl_sha256": "4" * 64, "log_jsonl_bytes": 7})
        text = json.dumps(p)
        for forbidden in ("verbatim row", "/Users/", "memory.json", "log.jsonl"):
            self.assertNotIn(forbidden, text)
        names = [f["name"] for a in p["run"]["artifacts"] for f in a.get("files", [])]
        self.assertEqual(names, ["no_memory.py"])                 # candidate files only, never evidence files

    def test_evaluation_history_exposes_scores_and_empty_content_without_raw_text(self):
        evidence={ds:{'empty_content_calls':n, 'sidecar_jsonl_sha256':'a'*64, 'sidecar_jsonl_bytes':128,
                      'result_json_sha256':'b'*64,'result_json_bytes':64}
                  for ds,n in [('USPTO',0),('Symptom2Disease',0),('LawBench',5)]}
        p=eval_plan(evidence=evidence);run=p['run']
        self.assertEqual(run['summary']['empty_content_calls'],5)
        self.assertEqual(run['history'][0]['empty_content_calls'],5)
        self.assertEqual(run['history'][0]['val/macro_accuracy_all_datasets'],run['summary']['val/macro_accuracy_all_datasets'])
        self.assertNotIn('evidence_hashes',run['history'][0])
        self.assertEqual(json.loads(run['summary']['evidence_hashes'])['USPTO']['sidecar_jsonl_sha256'],'a'*64)
        self.assertNotIn('empty_content_calls',eval_plan()['run']['summary'])

    def test_artifact_metadata_guarded(self):
        index = scan.build_verbatim_index([QUOTE], run_chars=40)
        e = entry(hypothesis=f"model sees '{QUOTE[:60]}' and learns", axis="plain axis",
                  components=["retriever", QUOTE[5:70]])
        p = plan_runs.plan_propose_run(e, session(), helpers.make_manifest(), verbatim=index)
        md = p["run"]["artifacts"][0]["metadata"]
        self.assertRegex(md["hypothesis"], r"\[sha256 [0-9a-f]{64} chars \d+\]")
        self.assertEqual(md["axis"], "plain axis")
        self.assertEqual(md["components"][0], "retriever")
        self.assertRegex(md["components"][1], r"\[sha256 [0-9a-f]{64} chars \d+\]")
        self.assertNotIn(QUOTE[:40], json.dumps(p))
        unguarded = plan_runs.plan_propose_run(e, session(), helpers.make_manifest())
        self.assertIn(QUOTE[:40], unguarded["run"]["artifacts"][0]["metadata"]["hypothesis"])
        homed = plan_runs.plan_propose_run(entry(hypothesis="see /Users/someone/notes"), session(),
                                           helpers.make_manifest(), verbatim=index)
        self.assertEqual(homed["run"]["artifacts"][0]["metadata"]["hypothesis"], "see /Users/<user>/notes")

    def test_placeholder_project_when_decisions_unset(self):
        m = helpers.make_manifest(**{"decisions.project": None})
        p = eval_plan(manifest=m)
        self.assertTrue(p["placeholder_project"])
        self.assertEqual((p["run"]["entity"], p["run"]["project"]), ("unset-entity", "unset-project"))
        p = eval_plan()
        self.assertEqual((p["placeholder_project"], p["run"]["entity"], p["run"]["project"], p["run"]["group"]),
                         (False, "example-entity", "mh-forge-test", "mh-test"))


class Search(unittest.TestCase):
    def test_search_step_row(self):
        it1 = iteration(1, [cand("x", {d: 0.5 for d in DATASETS}, upstream=50.0)], frontier=40.0)
        p = plan_runs.plan_search_step(it1, [baselines()], helpers.make_manifest())
        run = p["run"]
        self.assertEqual(run["define_metrics"], [["iteration", None], ["search/*", "iteration"]])
        self.assertEqual(len(run["history"]), 1)
        row = run["history"][0]
        self.assertEqual(row["iteration"], 1)
        self.assertNotIn("step", row)
        self.assertNotIn("_step", row)
        self.assertNotIn("step", run)
        self.assertTrue(all(k == "iteration" or k.startswith("search/") for k in row))
        self.assertEqual(row["search/best_complete_avg_val"], 50.0)
        self.assertEqual(row["search/upstream_frontier_val"], 50.0)
        self.assertEqual(row["search/improvement"], 10.0)
        self.assertEqual((row["search/candidates_proposed"], row["search/candidates_valid"],
                          row["search/candidates_complete"], row["search/jobs_failed"]), (1, 1, 1, 0))
        self.assertEqual((row["search/solver_cost_usd"], row["search/sandbox_usd"], row["search/proposer_list_usd"]),
                         (0.002, 0.004, 0.003))
        self.assertEqual((row["search/propose_s"], row["search/bench_s"], row["search/evidence_exposed"]),
                         (10.5, 20.25, 0))
        self.assertEqual((row["search/context_chars_best"], row["search/prompt_tokens_max_best"]), (100, 50))
        self.assertEqual((p["unit"], run["job_type"], run["tables"], run["artifacts"]), ("loop:iter001", "search", [],
                                                                                         []))
        self.assertEqual(run["config"], {})                      # config only on iteration 0
        retried = plan_runs.plan_search_step(it1, [baselines()], helpers.make_manifest(), export_attempt=2)
        self.assertEqual(retried["run"]["history"][0]["search/export_attempt"], 2)
        self.assertEqual({k: v for k, v in retried["run"]["history"][0].items() if k != "search/export_attempt"}, row)

    def test_search_step_plan_is_stable_without_ledger(self):
        m = helpers.make_manifest()
        it0 = baselines()
        it1 = iteration(1, [cand("x", {d: 0.5 for d in DATASETS})], solver=10, sandbox=20, proposer=30)
        it2 = iteration(2, [cand("y", {d: 0.6 for d in DATASETS})], solver=999, sandbox=999, proposer=999)
        a = util.canonical_json(plan_runs.plan_search_step(it1, [it0], m))
        self.assertEqual(a, util.canonical_json(plan_runs.plan_search_step(copy.deepcopy(it1), [copy.deepcopy(it0)], m)))
        self.assertEqual(a, util.canonical_json(plan_runs.plan_search_step(it1, [it2, it0, it1], m)))   # later ignored
        row = json.loads(a)["run"]["history"][0]
        self.assertEqual(row["search/solver_cost_usd"], 0.00101)    # 1000 + 10 micro-dollars, the records' own sums
        self.assertEqual(row["search/proposer_list_usd"], 0.00003)
        source = Path(plan_runs.__file__).read_text(encoding="utf-8")
        imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
        self.assertFalse([line for line in imports if "ledger" in line or "time" in line or "os" in line.split()])

    def test_module_is_pure(self):
        from mhforge.telemetry import plan_tables
        for mod in (plan_runs, plan_tables):
            source = Path(mod.__file__).read_text(encoding="utf-8")
            self.assertTrue(source.startswith("# SPDX-License-Identifier: MIT\n"))
            for word in ("os.environ", "os.getenv", "import wandb", "import coreweave", "open(", "time.", "random",
                         "socket", "urllib"):
                self.assertNotIn(word, source, (mod.__name__, word))

    def test_search_metrics_count_missing_dataset_as_incomplete(self):
        part = cand("p", {"USPTO": 0.9, "Symptom2Disease": 0.9, "LawBench": None}, upstream=90.0)
        absent = cand("q", {"USPTO": 0.9})
        m = plan_runs.search_metrics([baselines(), iteration(1, [part, absent], frontier=40.0)],
                                     helpers.make_manifest())
        self.assertEqual([x["iteration"] for x in m], [0, 1])
        figs = {c["name"]: c for c in m[1]["candidates"]}
        self.assertEqual(figs["p"]["avg_val_all_datasets"], 60.0)
        self.assertFalse(figs["p"]["complete"])
        self.assertEqual(figs["p"]["missing"], ["LawBench"])
        self.assertEqual(figs["q"]["avg_val_all_datasets"], 30.0)
        self.assertEqual(figs["q"]["missing"], ["Symptom2Disease", "LawBench"])
        self.assertEqual(m[1]["candidates_complete"], 0)
        self.assertEqual(m[1]["failed_jobs"], [["p", "LawBench"]])
        self.assertEqual((m[1]["best_complete_before"], m[1]["best_complete_after"], m[1]["improvement"]),
                         (40.0, 40.0, 0.0))
        self.assertEqual(m[1]["upstream_frontier_val"], 90.0)    # upstream averages only what produced a result
        row = plan_runs.plan_search_step(iteration(1, [part, absent], frontier=40.0), [baselines()],
                                         helpers.make_manifest())["run"]["history"][0]
        self.assertEqual((row["search/candidates_complete"], row["search/jobs_failed"]), (0, 1))
        self.assertNotIn("search/context_chars_best", row)        # no complete candidate: no context figures

    def test_improvement_positive_for_new_best(self):
        better = cand("b", {d: 0.7 for d in DATASETS}, ctx={"memory_context_chars_avg": 4000, "prompt_tokens_max": 900})
        worse = cand("w", {d: 0.1 for d in DATASETS})
        bad = cand("i", {d: 0.99 for d in DATASETS}, valid=False)
        quarantined = cand("z", {d: 0.95 for d in DATASETS}, quarantined=True)
        m = plan_runs.search_metrics([baselines(), iteration(1, [worse, better, bad, quarantined])],
                                     helpers.make_manifest())[1]
        self.assertEqual(m["best_candidate"], "b")
        self.assertGreater(m["improvement"], 0)
        self.assertEqual(m["improvement"], 30.0)
        self.assertEqual((m["best_complete_before"], m["best_complete_after"]), (40.0, 70.0))
        self.assertEqual((m["context_chars_best"], m["prompt_tokens_max_best"]), (4000, 900))
        self.assertEqual((m["candidates_proposed"], m["candidates_valid"], m["candidates_complete"]), (4, 2, 2))
        m2 = plan_runs.search_metrics([baselines(), iteration(1, [worse])], helpers.make_manifest())[1]
        self.assertEqual(m2["improvement"], 0.0)
        self.assertEqual(plan_runs.search_metrics([baselines()], helpers.make_manifest())[0]["improvement"], 0.0)

    def test_search_run_resumes_one_id_with_iteration_axis(self):
        m = helpers.make_manifest()
        p0 = plan_runs.plan_search_step(baselines(), [], m)
        p3 = plan_runs.plan_search_step(iteration(3, [cand("x", {d: 0.5 for d in DATASETS})]), [baselines()], m)
        p3b = plan_runs.plan_search_step(iteration(3, [cand("x", {d: 0.5 for d in DATASETS})]), [baselines()], m,
                                         export_attempt=2)
        self.assertEqual({p0["run"]["id"], p3["run"]["id"], p3b["run"]["id"]}, {"mh-test-search"})
        self.assertEqual((p0["run"]["resume"], p3["run"]["resume"]), (False, True))
        self.assertEqual((p0["unit"], p3["unit"]), ("loop:iter000", "loop:iter003"))
        for p in (p0, p3, p3b):
            self.assertEqual(p["run"]["define_metrics"], [["iteration", None], ["search/*", "iteration"]])
        self.assertEqual((p0["run"]["history"][0]["iteration"], p3["run"]["history"][0]["iteration"]), (0, 3))
        self.assertEqual(p0["run"]["config"]["campaign"], "mh-test")
        self.assertEqual(p3b["export_attempt"], 2)


if __name__ == "__main__":
    unittest.main()
