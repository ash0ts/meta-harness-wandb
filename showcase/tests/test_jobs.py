# SPDX-License-Identifier: MIT
"""Tests for ``mhforge/jobs.py`` (spec 3.18, named cases of 5.2).

Every case runs the real JobRunner over a real workspace copied (by ``workspace.ensure_workspace``) from a synthetic
example tree with the pinned file set, the real bundle builder, a real Journal and Ledger, ``tests/fake_facade.FakeFacade``
for upstream's builders and ``FakeExecutor`` for the executor. The two ``needs_yaml``/``needs_example_dir`` cases load
upstream's real ``benchmark.py`` from a workspace copy of the example.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mhforge import jobs, procenv, util, workspace
from mhforge import ledger as ledger_mod
from mhforge.errors import CampaignStop, ExecutorError, UpstreamContractError
from mhforge.journal import Journal
from mhforge.ledger import Ledger
from mhforge.paths import StatePaths
from tests import helpers
from tests.fake_facade import (CandidateStore, FakeExecutor, FakeFacade, imp_fail, imp_ok, make_result,
                               upstream_prefix)

DATASETS = ["USPTO", "Symptom2Disease", "LawBench"]
MS = "gpt-oss-120b"
TOKEN = "mhfjob_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9t0U1v"     # job-token shaped (43 URL-safe characters)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_example(root: Path) -> Path:
    for rel in workspace.PINNED_FILES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# synthetic pinned file {rel}\n")
    return root


class Rig:
    """A campaign state with a workspace, journal, ledger, fakes and a JobRunner."""

    def __init__(self, tc: unittest.TestCase, *, overrides: dict | None = None, datasets=("USPTO",),
                 example: Path | None = None, facade_kw: dict | None = None, script=(), import_script=(),
                 delay_s: float = 0.0) -> None:
        self.tc = tc
        base = Path(tempfile.mkdtemp(prefix="mhf-jobs-")).resolve()
        tc.addCleanup(shutil.rmtree, base, True)
        self.base = base
        self.ex = example or write_example(base / "ex")
        digest, _ = workspace.source_digest(self.ex)
        self.manifest = helpers.make_manifest({"upstream.tree_sha256": digest, **(overrides or {})})
        self.campaign = self.manifest["campaign"]
        self.paths = StatePaths(base / "sc" / "state" / self.campaign)
        self.paths.root.mkdir(parents=True)
        self.ws = workspace.ensure_workspace(self.manifest, self.paths, self.ex, None)
        self.logs = self.paths.logs_dir(self.campaign)
        self.results = self.logs / "results"
        self.clock = helpers.FakeClock()
        self.ledger = Ledger(self.paths.ledger, self.manifest, clock=self.clock)
        self.store = CandidateStore(base / "store")
        self.facade = FakeFacade(self.ws, datasets=list(datasets), **(facade_kw or {}))
        self.executor = FakeExecutor(script, import_script=import_script, delay_s=delay_s)
        self.pt_calls: list[list[str]] = []
        self.pt_result = subprocess.CompletedProcess(["benchmark.py"], 0, stdout="frontier\n", stderr="")
        self.pt_hook = None
        self.units: list[str] = []
        self.new_runner()

    def passthrough(self, args):
        self.pt_calls.append(list(args))
        if self.pt_hook:
            self.pt_hook(list(args))
        return self.pt_result

    def new_runner(self, *, new_journal: bool = True, executor: FakeExecutor | None = None, **kw) -> jobs.JobRunner:
        if new_journal:
            self.journal = Journal(self.paths.journal, clock=self.clock)
        if executor is not None:
            self.executor = executor
        self.tracker = jobs.PhaseTracker()
        args = dict(journal=self.journal, ledger=self.ledger, executor=self.executor, facade=self.facade,
                    tracker=self.tracker, example_dir=self.ex, logs_dir=self.logs, results_dir=self.results,
                    passthrough=self.passthrough, candidate_reference=self.store, on_unit=self.units.append,
                    clock=self.clock)
        args.update(kw)
        self.runner = jobs.JobRunner(self.manifest, self.paths, **args)
        return self.runner

    def new_invocation(self) -> None:
        """A later runner invocation: a fresh Journal and a `run: started` entry (fresh infrastructure budget)."""
        self.new_runner()
        self.journal.record("run", "search", "started", detail={})

    def candidate(self, name: str, data: bytes | None = None, *, store: bool = True) -> bytes:
        data = data if data is not None else f"# candidate {name}\nclass X: pass\n".encode()
        (self.ws / "agents" / f"{name}.py").write_bytes(data)
        if store:
            self.store.add(name, data)
        return data

    def record(self, job_key: str) -> dict:
        return util.read_json(self.paths.job_dir(job_key) / "job.json")

    def run_dir(self, dataset: str, name: str, *, test: bool = False) -> Path:
        return (self.results if test else self.logs) / dataset / name / MS

    def journal_rows(self, op: str | None = None) -> list[dict]:
        return self.journal.entries(op)


class JobsBase(unittest.TestCase):
    def setUp(self):
        saved = ledger_mod.FULLFSYNC
        ledger_mod.FULLFSYNC = False
        self.addCleanup(setattr, ledger_mod, "FULLFSYNC", saved)

    def rig(self, **kw) -> Rig:
        return Rig(self, **kw)

    @staticmethod
    def bad_result_with_file(spec, attempt_dir):
        """A worker that exits 1 but leaves a usable result file (outcome worker_error)."""
        r = make_result({"kind": "ok"}, spec, attempt_dir)
        return dataclasses.replace(r, returncode=1, exit_file=dict(r.exit_file, returncode=1))


# ---------------------------------------------------------------- dispatch and planning

class DispatchTests(JobsBase):
    def test_dispatch_forms(self):
        r = self.rig(datasets=DATASETS)
        cp = r.runner.run_benchmark(["--memory", "no_memory"])
        self.assertEqual(cp.returncode, 0)
        self.assertEqual(len(r.executor.runs), 3)
        self.assertEqual(r.pt_calls, [["--results"]])           # summary.json is rewritten by the real benchmark.py
        cp = r.runner.run_benchmark(["--memory", "no_memory", "--test"])
        self.assertEqual(cp.returncode, 0)
        self.assertEqual([x["spec"].identity.split for x in r.executor.runs[3:]], ["test"] * 3)
        self.assertEqual(r.pt_calls, [["--results"]])           # the test form makes no --results call
        for form in (["--frontier", "--model", "m"], ["--frontier", "--test", "--model", "m"], ["--results", "--test"]):
            r.pt_calls.clear()
            self.assertIs(r.runner.run_benchmark(form), r.pt_result)
            self.assertEqual(r.pt_calls, [form])
        for bad in (["--results"], ["--memory"], ["--memory", "no_memory", "--force"], ["--frontier"],
                    ["--memory", "no_memory", "--test", "x"], ["--test", "--memory", "no_memory"], []):
            with self.assertRaises(UpstreamContractError, msg=bad):
                r.runner.run_benchmark(bad)
        self.assertEqual(len(r.executor.runs), 6)

    def test_not_found_returns_zero_with_upstream_message(self):
        r = self.rig()
        for memory in ("agents/nope.py", "fewshot_memory", "__init__", "Bad\nname"):
            cp = r.runner.run_benchmark(["--memory", memory])
            self.assertEqual(cp.returncode, 0)
            self.assertEqual(cp.stdout, f"Error: '{memory}' not found on disk.\n")
        self.assertEqual(r.executor.runs, [])
        self.assertEqual(r.pt_calls, [])
        jobs_, msg, skipped = jobs.plan_jobs(r.facade, "agents/nope.py", test=False, logs_dir=r.logs,
                                             results_dir=r.results, manifest=r.manifest)
        self.assertEqual((jobs_, msg, skipped), ([], "Error: 'agents/nope.py' not found on disk.", []))

    def test_skip_is_decided_by_upstream_builder(self):
        r = self.rig(datasets=DATASETS, facade_kw={"skip": {"USPTO"}})
        r.runner.run_benchmark(["--memory", "no_memory"])
        self.assertEqual([x["spec"].identity.dataset for x in r.executor.runs], ["Symptom2Disease", "LawBench"])
        skipped = [e for e in r.journal_rows("job") if e["status"] == "skipped"]
        self.assertEqual([(e["key"], e["detail"]) for e in skipped], [("val.no_memory.USPTO", {"why": "usable"})])
        self.assertEqual(r.record("val.no_memory.USPTO")["status"], "skipped_usable")
        # the builder, not the runner, decides: a forced dataset runs again although its published result is usable
        r.facade.skip, r.facade.force = set(), {"LawBench"}
        r.facade.datasets = ["LawBench"]
        cp = r.runner.run_benchmark(["--memory", "no_memory"])
        self.assertEqual(cp.returncode, 0)
        self.assertEqual(len(r.executor.runs), 2)               # reused from the journal: same bytes, same inputs
        self.assertNotIn("--force", r.record("val.no_memory.LawBench")["inner_argv"])
        self.assertEqual([c[0] for c in r.facade.calls], ["val", "val"])

    def test_prefix_mismatch_raises_contract_error(self):
        r = self.rig(facade_kw={"cmd_prefix": ["uv", "run", "python", "-m", "text_classification.inner_loop"]})
        with self.assertRaises(UpstreamContractError):
            jobs.plan_jobs(r.facade, "no_memory", test=False, logs_dir=r.logs, results_dir=r.results,
                           manifest=r.manifest)
        with self.assertRaises(UpstreamContractError):
            r.runner.run_benchmark(["--memory", "no_memory"])
        self.assertEqual(r.executor.runs, [])
        prefix = upstream_prefix(r.ws)
        r2 = self.rig(facade_kw={"cmd_prefix": prefix[:-1] + ["text_classification.other"]})
        with self.assertRaises(UpstreamContractError):
            jobs.plan_jobs(r2.facade, "no_memory", test=False, logs_dir=r2.logs, results_dir=r2.results,
                           manifest=r2.manifest)

    def test_jobs_run_in_config_dataset_order(self):
        for seed in range(4):
            r = self.rig(datasets=DATASETS, facade_kw={"shuffle_seed": seed})
            runs = r.facade.build_val_runs(r.logs, [("no_memory", "agents/no_memory.py")])
            planned, _, _ = jobs.plan_jobs(r.facade, "no_memory", test=False, logs_dir=r.logs,
                                           results_dir=r.results, manifest=r.manifest)
            self.assertEqual([p.dataset for p in planned], DATASETS)
            r.runner.run_benchmark(["--memory", "no_memory"])
            self.assertEqual([x["spec"].identity.dataset for x in r.executor.runs], DATASETS)
            if [d.split("/")[1] for d, _ in runs] != DATASETS:
                break                                           # the fake builder did shuffle: the order is ours
        else:
            self.fail("no seed made the fake builder shuffle")

    def test_route1_rewrites_model_and_api_base(self):
        r = self.rig(overrides={"routing.route": 1})
        planned, _, _ = jobs.plan_jobs(r.facade, "no_memory", test=False, logs_dir=r.logs, results_dir=r.results,
                                       manifest=r.manifest)
        argv = list(planned[0].inner_argv)
        self.assertEqual(argv[argv.index("--model") + 1], "openai/openai/gpt-oss-120b")
        self.assertEqual(argv[-2:], ["--api-base", "{api_base}"])
        self.assertEqual(argv.count("--api-base"), 1)
        r2 = self.rig(overrides={"routing.route": 1}, facade_kw={"api_base": "https://example.invalid/v1"})
        argv = list(jobs.plan_jobs(r2.facade, "no_memory", test=False, logs_dir=r2.logs, results_dir=r2.results,
                                   manifest=r2.manifest)[0][0].inner_argv)
        self.assertEqual(argv.count("--api-base"), 1)
        self.assertEqual(argv[argv.index("--api-base") + 1], "{api_base}")
        self.assertNotIn("https://example.invalid/v1", argv)
        # Route 2 keeps upstream's model string and adds nothing
        r3 = self.rig()
        argv = list(jobs.plan_jobs(r3.facade, "no_memory", test=False, logs_dir=r3.logs, results_dir=r3.results,
                                   manifest=r3.manifest)[0][0].inner_argv)
        self.assertEqual(argv[argv.index("--model") + 1], "openrouter/openai/gpt-oss-120b")
        self.assertNotIn("--api-base", argv)
        self.assertEqual(argv[argv.index("--temperature") + 1], "0.0")
        self.assertEqual(argv[argv.index("--val-output") + 1], "{out}/val.json")
        self.assertEqual(argv[argv.index("--save-memory") + 1], "{out}/memory.json")
        self.assertEqual(argv[argv.index("--log") + 1], "{out}/log.jsonl")


# ---------------------------------------------------------------- result files

class PublishedResultTests(JobsBase):
    def plant(self, path: Path, accuracy: float = 0.99) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"accuracy": accuracy}))
        return path

    def test_planted_result_is_quarantined_and_job_runs(self):
        r = self.rig()
        planted = self.plant(r.run_dir("USPTO", "no_memory") / "val.json")
        cp = r.runner.run_benchmark(["--memory", "no_memory"])
        self.assertEqual(cp.returncode, 0)
        self.assertEqual(len(r.executor.runs), 1)
        self.assertEqual(json.loads(planted.read_text())["accuracy"], 0.1)
        rel = jobs._rel(r.paths, planted)
        moved = list(r.paths.quarantine_root.rglob("val.json"))
        self.assertEqual(len(moved), 1)
        self.assertTrue(moved[0].as_posix().endswith(rel))
        self.assertEqual(json.loads(moved[0].read_text())["accuracy"], 0.99)
        q = [e for e in r.journal_rows("workspace") if e["key"] == "quarantine"]
        self.assertEqual(q[0]["detail"], {"paths": [rel]})

    def test_planted_result_for_other_system_never_reaches_frontier(self):
        r = self.rig()
        r.runner.run_benchmark(["--memory", "no_memory"])
        ghost = self.plant(r.run_dir("USPTO", "ghost") / "val.json")          # a name with no agent file
        odd = self.plant(r.logs / "a" / "b" / "c" / "d" / "val.json")          # an odd depth
        ghost_test = self.plant(r.run_dir("USPTO", "ghost", test=True) / "test.json")
        ours = r.run_dir("USPTO", "no_memory") / "val.json"
        seen = []
        r.pt_hook = lambda args: seen.append(sorted(str(p) for p in r.logs.rglob("*.json")
                                                    if p.name in ("val.json", "test.json")))
        r.runner.run_benchmark(["--frontier", "--model", "m"])
        self.assertEqual(seen, [[str(ours)]])
        for p in (ghost, odd, ghost_test):
            self.assertFalse(os.path.lexists(p))
        self.assertEqual(len([p for p in r.paths.quarantine_root.rglob("*") if p.is_file()]), 3)
        self.assertEqual(r.runner.verify_published(), {"quarantined": [], "republished": []})

    def test_changed_published_result_is_republished(self):
        r = self.rig()
        r.runner.run_benchmark(["--memory", "no_memory"])
        val = r.run_dir("USPTO", "no_memory") / "val.json"
        mem = r.run_dir("USPTO", "no_memory") / "memory.json"
        good, good_mem = val.read_bytes(), mem.read_bytes()
        val.write_text(json.dumps({"accuracy": 1.0}))
        mem.write_text("{}")
        out = r.runner.verify_published()
        rels = sorted([jobs._rel(r.paths, val), jobs._rel(r.paths, mem)])
        self.assertEqual(sorted(out["quarantined"]), rels)
        self.assertEqual(sorted(out["republished"]), rels)
        self.assertEqual(val.read_bytes(), good)
        self.assertEqual(mem.read_bytes(), good_mem)
        self.assertEqual(sorted(e["key"] for e in r.journal_rows("workspace")[-2:]), ["quarantine", "republish"])
        # a symlink in place of the published file is quarantined and replaced as well
        val.unlink()
        os.symlink(r.base / "elsewhere.json", val)
        r.runner.verify_published()
        self.assertFalse(val.is_symlink())
        self.assertEqual(val.read_bytes(), good)


# ---------------------------------------------------------------- reuse, budgets and attempts

class AttemptTests(JobsBase):
    def test_journal_backed_result_reused_after_crash(self):
        r = self.rig()
        r.runner.run_benchmark(["--memory", "no_memory"])
        val = r.run_dir("USPTO", "no_memory") / "val.json"
        good = val.read_bytes()
        val.unlink()                                            # the crash came before the publication
        r.new_invocation()
        r.new_runner(new_journal=False, executor=FakeExecutor())
        cp = r.runner.run_benchmark(["--memory", "no_memory"])
        self.assertEqual(cp.returncode, 0)
        self.assertEqual(r.executor.runs, [])
        self.assertEqual(val.read_bytes(), good)
        self.assertEqual(r.record("val.no_memory.USPTO")["status"], "succeeded")
        # a changed result file in the attempt directory is not reused
        val.unlink()
        (r.paths.attempt_dir("val.no_memory.USPTO", 1) / "out" / "val.json").write_text('{"accuracy": 1.0}')
        r.runner.run_benchmark(["--memory", "no_memory"])
        self.assertEqual(len(r.executor.runs), 1)

    def test_tainted_or_failed_attempt_is_never_reused(self):
        r = self.rig(overrides={"executor.max_infra_attempts": 1}, script=["tainted_ok"])
        r.candidate("cand")
        with self.assertRaises(CampaignStop) as cm:
            r.runner.run_benchmark(["--memory", "cand"])
        self.assertEqual(cm.exception.reason, "infrastructure")
        att = r.record("val.cand.USPTO")["attempts"][0]
        self.assertEqual((att["outcome"], att["infra"]), ("provider_unavailable", True))
        self.assertTrue(jobs._usable(r.paths.attempt_dir("val.cand.USPTO", 1) / "out" / "val.json"))
        self.assertFalse((r.run_dir("USPTO", "cand") / "val.json").exists())
        r.new_invocation()
        r.new_runner(new_journal=False, executor=FakeExecutor())
        r.runner.run_benchmark(["--memory", "cand"])
        self.assertEqual(len(r.executor.runs), 1)               # run again, not reused
        # a failed attempt that left a usable file is not reused either
        r2 = self.rig(overrides={"executor.max_attempts": 2}, script=[self.bad_result_with_file])
        r2.candidate("cand")
        cp = r2.runner.run_benchmark(["--memory", "cand"])
        self.assertEqual(len(r2.executor.runs), 2)
        self.assertEqual(r2.record("val.cand.USPTO")["attempts"][0]["outcome"], "worker_error")
        self.assertEqual(cp.returncode, 0)

    def test_one_candidate_attempt_then_failed(self):
        r = self.rig(script=["worker_error"])
        r.candidate("cand")
        cp = r.runner.run_benchmark(["--memory", "cand"])
        self.assertEqual(cp.returncode, 1)
        self.assertIn("FAILED: val/USPTO/cand/gpt-oss-120b", cp.stderr)
        rec = r.record("val.cand.USPTO")
        self.assertEqual(rec["status"], "failed")
        self.assertEqual([(a["outcome"], a["infra"]) for a in rec["attempts"]], [("worker_error", False)])
        self.assertEqual(r.clock.sleeps, [])
        cp = r.runner.run_benchmark(["--memory", "cand"])     # the budget of these bytes is spent
        self.assertEqual(cp.returncode, 1)
        self.assertEqual(len(r.executor.runs), 1)

    def test_timeout_is_not_rerun(self):
        r = self.rig(overrides={"executor.max_attempts": 3}, script=["timeout", "ok"])
        r.candidate("cand")
        cp = r.runner.run_benchmark(["--memory", "cand"])
        self.assertEqual(cp.returncode, 1)
        self.assertEqual(len(r.executor.runs), 1)
        rec = r.record("val.cand.USPTO")
        self.assertEqual([(a["outcome"], a["infra"]) for a in rec["attempts"]], [("timeout", False)])
        r.runner.run_benchmark(["--memory", "cand"])
        self.assertEqual(len(r.executor.runs), 1)

    def test_request_ceiling_not_retried(self):
        r = self.rig(overrides={"executor.max_attempts": 3}, script=["request_ceiling", "ok"])
        r.candidate("cand")
        self.assertEqual(r.runner.run_benchmark(["--memory", "cand"]).returncode, 1)
        self.assertEqual(len(r.executor.runs), 1)
        att = r.record("val.cand.USPTO")["attempts"][0]
        self.assertEqual((att["outcome"], att["infra"], att["failure_class"]),
                         ("request_ceiling", False, "request_ceiling"))
        # a baseline's request ceiling is not infrastructure and is not retried either
        r2 = self.rig(overrides={"executor.max_attempts": 3}, script=["request_ceiling", "ok"])
        self.assertEqual(r2.runner.run_benchmark(["--memory", "no_memory"]).returncode, 1)
        self.assertEqual(len(r2.executor.runs), 1)
        self.assertFalse(r2.record("val.no_memory.USPTO")["attempts"][0]["infra"])

    def test_attempts_counted_per_candidate_hash(self):
        r = self.rig(script=["worker_error"])
        v1 = r.candidate("cand", b"# v1\n")
        self.assertEqual(r.runner.run_benchmark(["--memory", "cand"]).returncode, 1)
        v2 = r.candidate("cand", b"# v2 fixed\n")                # re-proposed under the same name
        cp = r.runner.run_benchmark(["--memory", "cand"])
        self.assertEqual(cp.returncode, 0)
        self.assertEqual([x["candidate"] for x in r.executor.runs], [v1, v2])
        rec = r.record("val.cand.USPTO")
        self.assertEqual([a["candidate_sha256"] for a in rec["attempts"]], [sha(v1), sha(v2)])
        self.assertEqual([a["attempt"] for a in rec["attempts"]], [1, 2])
        self.assertEqual(rec["status"], "succeeded")

    def test_reset_job_gives_fresh_budget(self):
        r = self.rig(script=["worker_error"])
        r.candidate("cand")
        r.runner.run_benchmark(["--memory", "cand"])
        r.runner.run_benchmark(["--memory", "cand"])
        self.assertEqual(len(r.executor.runs), 1)
        r.journal.record("job", "val.cand.USPTO", "reset", detail={"note": "operator: fixed the dataset"})
        self.assertEqual(r.runner.run_benchmark(["--memory", "cand"]).returncode, 0)
        self.assertEqual(len(r.executor.runs), 2)
        self.assertEqual(r.executor.runs[1]["spec"].identity.attempt, 2)

    def test_baseline_failures_are_infrastructure(self):
        r = self.rig(script=["worker_error", "no_result", "timeout"])
        with self.assertRaises(CampaignStop) as cm:
            r.runner.run_benchmark(["--memory", "no_memory"])
        self.assertEqual(cm.exception.reason, "infrastructure")
        rec = r.record("val.no_memory.USPTO")
        self.assertEqual([(a["outcome"], a["infra"]) for a in rec["attempts"]],
                         [("worker_error", True), ("no_result", True), ("timeout", True)])
        self.assertEqual(r.clock.sleeps, [60, 120])
        self.assertEqual([e for e in r.journal.counted_attempts("val.no_memory.USPTO", rec["candidate_sha256"])
                          if e["detail"].get("infra") is False], [])
        self.assertFalse((r.run_dir("USPTO", "no_memory") / "val.json").exists())
        # a later invocation has a fresh infrastructure budget and finishes the baseline
        r.new_invocation()
        r.new_runner(new_journal=False, executor=FakeExecutor())
        self.assertEqual(r.runner.run_benchmark(["--memory", "no_memory"]).returncode, 0)

    def test_attempts_persist_across_restart(self):
        r = self.rig(script=["relay_lost", "worker_error"])
        r.candidate("cand")
        self.assertEqual(r.runner.run_benchmark(["--memory", "cand"]).returncode, 1)
        self.assertEqual(len(r.executor.runs), 2)
        r.new_invocation()
        r.new_runner(new_journal=False, executor=FakeExecutor())
        self.assertEqual(r.runner.run_benchmark(["--memory", "cand"]).returncode, 1)
        self.assertEqual(r.executor.runs, [])                   # the candidate attempt is still spent
        self.assertEqual(r.journal.next_attempt("job", "val.cand.USPTO"), 3)
        # an infrastructure attempt cut off by a crash does not reuse its number either
        r3 = self.rig(script=[KeyboardInterrupt()])
        r3.candidate("cand")
        with self.assertRaises(KeyboardInterrupt):
            r3.runner.run_benchmark(["--memory", "cand"])
        r3.new_invocation()
        r3.new_runner(new_journal=False, executor=FakeExecutor())
        r3.runner.run_benchmark(["--memory", "cand"])
        self.assertEqual(r3.executor.runs[0]["spec"].identity.attempt, 2)

    def test_infra_attempts_are_separate_and_stop_the_campaign(self):
        r = self.rig(script=["relay_lost", "no_exit", "ok"])
        r.candidate("cand")
        self.assertEqual(r.runner.run_benchmark(["--memory", "cand"]).returncode, 0)
        rec = r.record("val.cand.USPTO")
        self.assertEqual([(a["outcome"], a["infra"]) for a in rec["attempts"]],
                         [("relay_lost", True), ("executor_error", True), ("ok", False)])
        r2 = self.rig(script=["relay_lost", "tainted_ok", "relay_lost"])
        r2.candidate("cand")
        with self.assertRaises(CampaignStop) as cm:
            r2.runner.run_benchmark(["--memory", "cand"])
        self.assertEqual(cm.exception.reason, "infrastructure")
        rec = r2.record("val.cand.USPTO")
        self.assertTrue(all(a["infra"] for a in rec["attempts"]))
        self.assertEqual(len(rec["attempts"]), 3)
        self.assertEqual(r2.journal.counted_attempts("val.cand.USPTO", rec["candidate_sha256"])[0]["detail"]["infra"],
                         True)
        self.assertFalse((r2.run_dir("USPTO", "cand") / "val.json").exists())
        self.assertFalse(any(e["key"] == "val.cand" and e["status"] == "complete" for e in r2.journal_rows("benchmark")))

    def test_infra_attempts_wait_between_tries(self):
        for max_infra, waits in ((3, [60, 120]), (7, [60, 120, 240, 480, 900, 900])):
            r = self.rig(overrides={"executor.max_infra_attempts": max_infra}, script=["relay_lost"] * max_infra)
            r.candidate("cand")
            with self.assertRaises(CampaignStop):
                r.runner.run_benchmark(["--memory", "cand"])
            self.assertEqual(r.clock.sleeps, waits)
            self.assertEqual(len(r.executor.runs), max_infra)

    def test_env_setup_failure_is_infrastructure_up_to_three(self):
        r = self.rig(script=[ExecutorError(kind="env_setup")] * 3)
        r.candidate("cand")
        with self.assertRaises(CampaignStop) as cm:
            r.runner.run_benchmark(["--memory", "cand"])
        self.assertEqual(cm.exception.reason, "infrastructure")
        rec = r.record("val.cand.USPTO")
        self.assertEqual([(a["outcome"], a["infra"]) for a in rec["attempts"]], [("env_setup_failed", True)] * 3)
        self.assertEqual(len(r.executor.runs), 3)
        r2 = self.rig(script=[ExecutorError(kind="env_setup"), ExecutorError(kind="create"), "ok"])
        r2.candidate("cand")
        self.assertEqual(r2.runner.run_benchmark(["--memory", "cand"]).returncode, 0)
        self.assertEqual([a["outcome"] for a in r2.record("val.cand.USPTO")["attempts"]],
                         ["env_setup_failed", "executor_error", "ok"])

    def test_non_retryable_executor_error_stops_at_once(self):
        for kind in ("billing", "permission", "auth", "image_mismatch"):
            r = self.rig(script=[ExecutorError(kind=kind), "ok"])
            with self.assertRaises(CampaignStop) as cm:
                r.runner.run_benchmark(["--memory", "no_memory"])
            self.assertEqual((cm.exception.reason, cm.exception.detail), ("infrastructure", kind))
            self.assertEqual(len(r.executor.runs), 1)
            self.assertEqual(r.clock.sleeps, [])
            att = r.record("val.no_memory.USPTO")["attempts"]
            self.assertEqual([(a["outcome"], a["infra"]) for a in att], [("executor_error", True)])
            self.assertEqual(r.journal.last("job", "val.no_memory.USPTO")["status"], "failed")

    def test_host_sleep_attempt_is_interrupted(self):
        r = self.rig(script=["host_sleep", "ok"])
        r.candidate("cand")
        self.assertEqual(r.runner.run_benchmark(["--memory", "cand"]).returncode, 0)
        a1, a2 = r.record("val.cand.USPTO")["attempts"]
        self.assertEqual((a1["outcome"], a1["infra"], a1["failure_class"], a1["host_sleep_s"]),
                         ("interrupted", True, "host_sleep", 42.0))
        self.assertEqual(a2["outcome"], "ok")

    def test_halt_after_job_raises_campaign_stop(self):
        r = self.rig(datasets=DATASETS)

        def halting(spec, attempt_dir):
            r.ledger.set_halt("budget:baseline", "phase cap reached")
            return make_result({"kind": "ok"}, spec, attempt_dir)

        r.executor.script = [halting]
        with self.assertRaises(CampaignStop) as cm:
            r.runner.run_benchmark(["--memory", "no_memory"])
        self.assertEqual((cm.exception.reason, cm.exception.detail), ("budget:baseline", "phase cap reached"))
        self.assertEqual(len(r.executor.runs), 1)
        rec = r.record("val.no_memory.USPTO")
        self.assertEqual(len(rec["attempts"]), 1)
        self.assertEqual(r.journal.last("job", "val.no_memory.USPTO")["status"], "complete")
        self.assertIn("job:val.no_memory.USPTO.a1", r.units)
        with self.assertRaises(CampaignStop):                   # a halt also stops the next call before any job
            r.runner.run_benchmark(["--memory", "no_memory"])
        with self.assertRaises(CampaignStop):
            r.runner.halt_check()
        self.assertEqual(len(r.executor.runs), 1)


# ---------------------------------------------------------------- candidate bytes and test inputs

class CandidateBytesTests(JobsBase):
    def test_changed_candidate_file_runs_stored_copy(self):
        r = self.rig()
        v1 = r.candidate("cand", b"# proposed and import-checked\n")
        (r.ws / "agents" / "cand.py").write_bytes(b"# edited later by a background child\n")
        r.runner.run_benchmark(["--memory", "cand"])
        self.assertEqual(r.executor.runs[0]["candidate"], v1)
        att = r.record("val.cand.USPTO")["attempts"][0]
        self.assertEqual((att["candidate_sha256"], att["candidate_restored"]), (sha(v1), True))
        self.assertIn(("cand", None), r.store.calls)
        bundle_json = util.read_json(r.paths.attempt_dir("val.cand.USPTO", 1) / "bundle.json")
        self.assertEqual(bundle_json["members"]["pkg/text_classification/agents/cand.py"], sha(v1))
        # unchanged file: no override
        r2 = self.rig()
        v = r2.candidate("cand")
        r2.runner.run_benchmark(["--memory", "cand"])
        att = r2.record("val.cand.USPTO")["attempts"][0]
        self.assertEqual((att["candidate_sha256"], att["candidate_restored"]), (sha(v), False))
        self.assertEqual(set(r2.store.calls), {("cand", None)})
        # a baseline has no reference: its pinned bytes come from the example
        r2.store.calls.clear()
        r2.runner.run_benchmark(["--memory", "no_memory"])
        self.assertEqual(r2.store.calls, [])

    def test_test_job_uses_validated_memory_and_code(self):
        r = self.rig()
        v1 = r.candidate("cand", b"# validated\n")
        r.runner.run_benchmark(["--memory", "cand"])
        val_mem = r.paths.attempt_dir("val.cand.USPTO", 1) / "out" / "memory.json"
        r.candidate("cand", b"# a later proposal of the same name\n")
        (r.run_dir("USPTO", "cand") / "memory.json").write_text('{"memory": "rewritten by the proposer"}')
        r.runner.run_benchmark(["--memory", "cand", "--test"])
        run = r.executor.runs[-1]
        self.assertEqual(run["candidate"], v1)
        self.assertEqual(run["inputs"], {"memory.json": val_mem.read_bytes()})
        att = r.record("test.cand.USPTO")["attempts"][0]
        self.assertEqual(att["inputs"], {"memory.json": sha(val_mem.read_bytes())})
        self.assertEqual((att["candidate_sha256"], att["candidate_restored"]), (sha(v1), True))
        self.assertIn(("cand", sha(v1)), r.store.calls)
        self.assertTrue(any(p.name == "memory.json" for p in r.paths.quarantine_root.rglob("*")))
        self.assertEqual(jobs.resolve_test_input(r.paths, "cand", "USPTO"), (val_mem, sha(val_mem.read_bytes())))
        # a changed attempt-directory copy fails the hash check
        val_mem.write_text("{}")
        self.assertIsNone(jobs.resolve_test_input(r.paths, "cand", "USPTO"))
        self.assertIsNone(jobs.resolve_test_input(r.paths, "cand", "LawBench"))
        # without a stored copy of the validated bytes the test job is not run
        r2 = self.rig()
        r2.candidate("cand", b"# validated\n", store=False)
        r2.runner.run_benchmark(["--memory", "cand"])
        r2.candidate("cand", b"# changed\n")
        cp = r2.runner.run_benchmark(["--memory", "cand", "--test"])
        self.assertEqual(cp.returncode, 1)
        self.assertEqual(len(r2.executor.runs), 1)
        rec = r2.record("test.cand.USPTO")
        self.assertEqual((rec["status"], rec["message"]), ("failed", "validated candidate file not found"))

    def test_test_jobs_get_memory_input(self):
        r = self.rig()
        r.runner.run_benchmark(["--memory", "no_memory"])
        r.runner.run_benchmark(["--memory", "no_memory", "--test"])
        spec = r.executor.runs[-1]["spec"]
        val_mem = r.paths.attempt_dir("val.no_memory.USPTO", 1) / "out" / "memory.json"
        self.assertEqual(spec.inputs, (("memory.json", val_mem),))
        self.assertEqual(spec.expect_outputs, ("test.json",))
        argv = list(spec.inner_argv)
        self.assertEqual(argv[argv.index("--load-memory") + 1], "{in}/memory.json")
        self.assertEqual(argv[argv.index("--test-output") + 1], "{out}/test.json")
        self.assertEqual(spec.identity.phase, "test")
        test_json = r.run_dir("USPTO", "no_memory", test=True) / "test.json"
        self.assertTrue(jobs._usable(test_json))
        rec = r.record("test.no_memory.USPTO")
        self.assertEqual(rec["published"]["files"], {jobs._rel(r.paths, test_json): sha(test_json.read_bytes())})
        self.assertEqual(rec["attempts"][0]["inputs"], {"memory.json": sha(val_mem.read_bytes())})

    def test_missing_memory_counts_as_failed(self):
        r = self.rig(datasets=DATASETS)
        cp = r.runner.run_benchmark(["--memory", "no_memory", "--test"])
        self.assertEqual(cp.returncode, 1)
        self.assertEqual(r.executor.runs, [])
        done = r.journal.last("benchmark", "test.no_memory")["detail"]
        self.assertEqual(done["jobs"], {d: "skipped_no_memory" for d in DATASETS})
        self.assertEqual(r.record("test.no_memory.LawBench")["status"], "skipped_no_memory")
        # a memory.json in LOGS that no validation job published is not a test input
        r2 = self.rig()
        mem = r2.run_dir("USPTO", "no_memory") / "memory.json"
        mem.parent.mkdir(parents=True)
        mem.write_text('{"memory": "planted"}')
        cp = r2.runner.run_benchmark(["--memory", "no_memory", "--test"])
        self.assertEqual(cp.returncode, 1)
        self.assertEqual(r2.executor.runs, [])
        self.assertEqual(r2.record("test.no_memory.USPTO")["status"], "skipped_no_memory")

    def test_repeat_job_has_own_key_and_is_never_published(self):
        r = self.rig()
        scratch = r.paths.scratch_root / "logs"
        planned, _, _ = jobs.plan_jobs(r.facade, "no_memory", test=False, logs_dir=scratch,
                                       results_dir=scratch / "results", manifest=r.manifest)
        rec = r.runner.run_job(planned[0], phase="calibration", iteration=None, publish=False, repeat=2)
        self.assertEqual(rec["job_key"], "val.no_memory.USPTO.r2")
        self.assertEqual((rec["repeat"], rec["phase"], rec["published"], rec["status"]), (2, "calibration", None,
                                                                                         "succeeded"))
        self.assertEqual(r.executor.runs[0]["spec"].identity.job_id, "val.no_memory.USPTO.r2.a1")
        self.assertEqual([p for p in r.logs.rglob("*") if p.is_file()] if r.logs.exists() else [], [])
        self.assertEqual([p for p in scratch.rglob("*") if p.is_file()], [])
        # a qualification job has the qual. prefix and is not published either
        rec = r.runner.run_job(planned[0], phase="qualification", iteration=None, publish=False)
        self.assertEqual((rec["job_key"], rec["published"]), ("qual.val.no_memory.USPTO", None))

    def test_unpublished_resume_reuses_complete_outputs_but_not_another_sample(self):
        r = self.rig()
        scratch = r.paths.scratch_root / 'logs'
        planned, _, _ = jobs.plan_jobs(r.facade, 'no_memory', test=False, logs_dir=scratch,
                                       results_dir=scratch/'results', manifest=r.manifest)
        for phase, sample in [('qualification', None), ('controls', 1)]:
            first = r.runner.run_job(planned[0], phase=phase, iteration=None, publish=False, sample=sample)
            before = len(r.executor.runs)
            r.new_runner()
            again = r.runner.run_job(planned[0], phase=phase, iteration=None, publish=False, sample=sample)
            self.assertEqual(len(r.executor.runs),before)
            self.assertEqual(again['attempts'],first['attempts'])
            self.assertIsNone(again['published'])
        r.runner.run_job(planned[0], phase='controls', iteration=None, publish=False, sample=2)
        self.assertEqual(len(r.executor.runs),3)
        # A changed stored output cannot be treated as a successful resume.
        path=r.paths.attempt_dir('qual.val.no_memory.USPTO',1)/'out/memory.json'
        path.write_text('{"changed":true}')
        r.runner.run_job(planned[0],phase='qualification',iteration=None,publish=False)
        self.assertEqual(len(r.executor.runs),4)


# ---------------------------------------------------------------- publication and launcher logs

class PublicationTests(JobsBase):
    @staticmethod
    def three(kind2: str = "worker_error"):
        return [{"kind": "ok", "correct": 3}, {"kind": kind2}, {"kind": "ok", "correct": 5, "total": 50}]

    def test_publish_upstream_policy(self):
        r = self.rig(datasets=DATASETS, script=self.three())
        r.candidate("cand")
        cp = r.runner.run_benchmark(["--memory", "cand"])
        self.assertEqual(cp.returncode, 1)
        for ds in ("USPTO", "LawBench"):
            for name in ("val.json", "memory.json", "log.jsonl"):
                self.assertTrue((r.run_dir(ds, "cand") / name).is_file(), (ds, name))
        failed = r.run_dir("Symptom2Disease", "cand")
        self.assertFalse((failed / "val.json").exists())
        self.assertTrue((failed / "memory.json").is_file())     # partial files of a failed attempt, as upstream
        self.assertTrue((failed / "log.jsonl").is_file())
        rec = r.record("val.cand.Symptom2Disease")
        self.assertEqual(sorted(Path(p).name for p in rec["published"]["files"]), ["log.jsonl", "memory.json"])
        self.assertEqual(r.record("val.cand.USPTO")["result"], {"accuracy": 0.1, "correct": 3, "total": 30,
                                                                "memory_context_chars": 191})
        for p in r.logs.rglob("*"):
            self.assertNotIn(p.name, ("sidecar.jsonl", "stdout.txt", "job.json", "exit.json"))
        self.assertEqual(r.journal.last("benchmark", "val.cand")["detail"],
                         {"returncode": 1, "jobs": {"USPTO": "succeeded", "Symptom2Disease": "failed",
                                                    "LawBench": "succeeded"},
                          "published": True, "quarantined": False})
        self.assertEqual(r.units[-1], "eval:val.cand")
        self.assertEqual(sorted(u for u in r.units if u.startswith("job:")),
                         ["job:val.cand.LawBench.a1", "job:val.cand.Symptom2Disease.a1", "job:val.cand.USPTO.a1"])
        # publish_outputs copies exactly what it is told
        planned = jobs.plan_jobs(r.facade, "no_memory", test=False, logs_dir=r.base / "scratch",
                                 results_dir=r.base / "scratch" / "results", manifest=r.manifest)[0][0]
        out = r.paths.attempt_dir("val.cand.USPTO", 1) / "out"
        copied = jobs.publish_outputs(out, planned, include_result=False)
        self.assertEqual(sorted(Path(p).name for p in copied), ["log.jsonl", "memory.json"])

    def test_quarantine_policy_publishes_nothing_for_partial_candidate(self):
        r = self.rig(datasets=DATASETS, overrides={"search.partial_candidate_policy": "quarantine"}, script=self.three())
        r.candidate("cand")
        self.assertEqual(r.runner.run_benchmark(["--memory", "cand"]).returncode, 1)
        for ds in DATASETS:
            self.assertFalse(any(p.is_file() for p in r.logs.glob(f"{ds}/cand/*/*")))
            self.assertIsNone(r.record(f"val.cand.{ds}")["published"])
        self.assertTrue(r.journal.last("benchmark", "val.cand")["detail"]["quarantined"])
        r2 = self.rig(datasets=DATASETS, overrides={"search.partial_candidate_policy": "quarantine"})
        r2.candidate("cand")
        self.assertEqual(r2.runner.run_benchmark(["--memory", "cand"]).returncode, 0)
        for ds in DATASETS:
            self.assertTrue((r2.run_dir(ds, "cand") / "val.json").is_file())
        detail = r2.journal.last("benchmark", "val.cand")["detail"]
        self.assertEqual((detail["published"], detail["quarantined"]), (True, False))

    def test_launcher_log_format_and_name(self):
        r = self.rig(overrides={"executor.max_attempts": 2},
                     script=[{"kind": "worker_error", "stdout": "first\n", "stderr": "Traceback: boom\n"},
                             {"kind": "ok", "stdout": "second\n", "stderr": ""}])
        r.candidate("cand")
        r.runner.run_benchmark(["--memory", "cand"])
        log = r.logs / ".launcher" / "00_val_USPTO_cand_gpt-oss-120b.log"
        planned_cmd = r.record("val.cand.USPTO")["upstream_command"]
        sep = "=" * 60
        self.assertEqual(log.read_text(), f"command: {planned_cmd}\n\nfirst\nTraceback: boom\n\nexit=1\n"
                                          f"\n{sep}\nretry 1\n{sep}\nsecond\n\nexit=0\n")
        self.assertTrue(planned_cmd.startswith(" ".join(upstream_prefix(r.ws))))
        self.assertEqual(r.record("val.cand.USPTO")["published"]["launcher_log"], jobs._rel(r.paths, log))
        # position in the call gives NN; the name is upstream's _sanitize_filename
        p = jobs.write_launcher_log(r.logs, 7, "test/Law Bench/x:y/m", "cmd", [(b"out\n", 124)], path_map={})
        self.assertEqual(p.name, "07_test_Law_Bench_x_y_m.log")
        self.assertEqual(p.read_text(), "command: cmd\n\nout\n\nexit=124\n")

    def test_launcher_log_paths_are_upstreams(self):
        r = self.rig()

        def worker(spec, attempt_dir):
            b = str(attempt_dir / "box")
            res = make_result({"kind": "ok", "stdout": f"Saved {spec.identity.split} results to {b}/out/"
                                                       f"{spec.expect_outputs[0]}\nfrom /mhf/in/memory.json\n"},
                              spec, attempt_dir)
            return res

        r.executor.script = [worker, worker]
        r.runner.run_benchmark(["--memory", "no_memory"])
        r.runner.run_benchmark(["--memory", "no_memory", "--test"])
        val_log = (r.logs / ".launcher" / "00_val_USPTO_no_memory_gpt-oss-120b.log").read_text()
        test_log = (r.logs / ".launcher" / "00_test_USPTO_no_memory_gpt-oss-120b.log").read_text()
        self.assertIn(f"Saved val results to {r.run_dir('USPTO', 'no_memory')}/val.json", val_log)
        self.assertIn(f"Saved test results to {r.run_dir('USPTO', 'no_memory', test=True)}/test.json", test_log)
        self.assertIn(f"from {r.run_dir('USPTO', 'no_memory')}/memory.json", test_log)
        for text in (val_log, test_log):
            self.assertNotIn(str(r.paths.jobs_root), text)
            self.assertNotIn("/mhf/", text)

    def test_job_record_has_no_token_or_address(self):
        r = self.rig()
        addr = "http://127.0.0.1:54321/v1"
        r.executor.script = [{"kind": "ok", "stdout": f"OPENROUTER_API_BASE={addr}\nbearer {TOKEN}\n",
                              "stderr": f"Authorization: Bearer {TOKEN}\n",
                              "gateway": {"token": TOKEN, "listener": addr, "note": f"via {addr} {TOKEN}"},
                              "facts": {"seconds": 1.0, "relay": {"port": 54321, "address": addr},
                                        "selfcheck": f"ok {TOKEN}"}}]
        r.runner.run_benchmark(["--memory", "no_memory"])
        texts = [p.read_text() for p in (r.paths.jobs_root.rglob("job.json"))]
        texts += [p.read_text() for p in r.paths.jobs_root.rglob("executor.json")]
        texts += [r.paths.journal.read_text()]
        texts += [p.read_text() for p in (r.logs / ".launcher").iterdir()]
        self.assertGreaterEqual(len(texts), 4)
        for text in texts:
            self.assertNotIn(TOKEN, text)
            self.assertNotIn("127.0.0.1:54321", text)
            self.assertNotIn("54321", text)

    def test_job_record_has_context_counters(self):
        r = self.rig(script=[{"kind": "ok", "memory_context_chars": 45510,
                              "gateway": {"usage_calls": 30, "prompt_tokens_sum": 11130, "prompt_tokens_max": 402,
                                          "completion_tokens_sum": 36120}}])
        r.runner.run_benchmark(["--memory", "no_memory"])
        rec = r.record("val.no_memory.USPTO")
        g = rec["attempts"][0]["gateway"]
        self.assertEqual({k: g[k] for k in ("usage_calls", "prompt_tokens_sum", "prompt_tokens_max",
                                            "completion_tokens_sum")},
                         {"usage_calls": 30, "prompt_tokens_sum": 11130, "prompt_tokens_max": 402,
                          "completion_tokens_sum": 36120})
        self.assertEqual(rec["result"]["memory_context_chars"], 45510)
        # an executor that reports no counters gives zeros, never a missing key
        r2 = self.rig()

        def bare(spec, attempt_dir):
            return dataclasses.replace(make_result({"kind": "ok"}, spec, attempt_dir), gateway={"completed": 1})

        r2.executor.script = [bare]
        r2.runner.run_benchmark(["--memory", "no_memory"])
        g = r2.record("val.no_memory.USPTO")["attempts"][0]["gateway"]
        self.assertEqual([g[k] for k in ("usage_calls", "prompt_tokens_sum", "prompt_tokens_max",
                                         "completion_tokens_sum")], [0, 0, 0, 0])

    def test_return_code_mirrors_upstream(self):
        r = self.rig(datasets=DATASETS)
        cp = r.runner.run_benchmark(["--memory", "no_memory"])
        self.assertEqual((cp.returncode, cp.stderr), (0, ""))
        self.assertEqual(cp.stdout.splitlines(), [f"val/{d}/no_memory/{MS}: succeeded" for d in DATASETS])
        cp = r.runner.run_benchmark(["--memory", "no_memory"])     # nothing pending
        self.assertEqual((cp.returncode, cp.stdout), (0, "All done!\n"))
        r.executor.script = [{"kind": "ok"}, {"kind": "worker_error"}, {"kind": "ok"}]
        r.candidate("cand")
        cp = r.runner.run_benchmark(["--memory", "agents/cand.py"])
        self.assertEqual(cp.returncode, 1)
        self.assertEqual(cp.stderr, f"FAILED: val/Symptom2Disease/cand/{MS} (failed)\n")
        self.assertEqual(cp.args, ["--memory", "agents/cand.py"])


# ---------------------------------------------------------------- parallelism and phases

class ParallelTests(JobsBase):
    def test_parallel_dataset_jobs_per_split_test_always_one(self):
        r = self.rig(datasets=DATASETS, overrides={"executor.parallel_dataset_jobs.validation": 3}, delay_s=0.3)
        self.assertEqual(r.runner.run_benchmark(["--memory", "no_memory"]).returncode, 0)
        self.assertGreaterEqual(r.executor.peak, 2)
        r.executor.peak = 0
        self.assertEqual(r.runner.run_benchmark(["--memory", "no_memory", "--test"]).returncode, 0)
        self.assertEqual(r.executor.peak, 1)
        r2 = self.rig(datasets=DATASETS, delay_s=0.1)
        r2.runner.run_benchmark(["--memory", "no_memory"])
        self.assertEqual(r2.executor.peak, 1)                   # the draft value: one validation job at a time

    def test_phase_comes_from_tracker(self):
        r = self.rig()
        r.candidate("cand")
        r.tracker.phase, r.tracker.iteration = "search", 3
        r.runner.run_benchmark(["--memory", "cand"])
        ident = r.executor.runs[0]["spec"].identity
        self.assertEqual((ident.phase, ident.iteration), ("search", 3))
        self.assertEqual((r.record("val.cand.USPTO")["phase"], r.record("val.cand.USPTO")["iteration"]), ("search", 3))
        started = r.journal.attempts("job", "val.cand.USPTO")[0]["started_detail"]
        self.assertEqual(set(started), {"executor", "phase", "candidate_sha256", "inputs"})
        self.assertEqual(started["phase"], "search")
        r.runner.run_benchmark(["--memory", "cand", "--test"])
        self.assertEqual(r.executor.runs[1]["spec"].identity.phase, "test")
        self.assertIsNone(r.executor.runs[1]["spec"].identity.iteration)


# ---------------------------------------------------------------- import checks

class ImportCheckTests(JobsBase):
    def test_import_check_probe_failure_is_infra_and_rejects_nobody(self):
        r = self.rig(import_script=[{"no_memory": imp_fail()}] * 3)
        r.candidate("good")
        with self.assertRaises(CampaignStop) as cm:
            r.runner.import_check(["good"], iteration=4)
        self.assertEqual(cm.exception.reason, "infrastructure")
        self.assertEqual(len(r.executor.import_calls), 3)
        self.assertEqual(r.clock.sleeps, [60, 120])
        statuses = [(e["status"], e["attempt"]) for e in r.journal_rows("import_check") if e["status"] != "handle"]
        self.assertEqual(statuses, [("started", 1), ("failed", 1), ("started", 2), ("failed", 2), ("started", 3),
                                    ("failed", 3)])
        for n in (1, 2, 3):
            check = util.read_json(r.paths.import_check_dir(4, n) / "check.json")
            self.assertEqual(check["infrastructure_failure"], "probe")
            self.assertTrue(all(v["rejected"] is None for v in check["results"].values()))
        # an executor that cannot start is an infrastructure attempt too; the next try succeeds
        r2 = self.rig(import_script=[ExecutorError(kind="create"), None])
        r2.candidate("good")
        out = r2.runner.import_check(["good"], iteration=1)
        self.assertEqual(list(out), ["good"])
        self.assertTrue(out["good"].ok)
        self.assertEqual(r2.clock.sleeps, [60])
        failed = [e for e in r2.journal_rows("import_check") if e["status"] == "failed"]
        self.assertEqual(failed[0]["detail"]["kind"], "create")
        # a non-retryable executor error stops at once
        r3 = self.rig(import_script=[ExecutorError(kind="billing")])
        with self.assertRaises(CampaignStop):
            r3.runner.import_check(["good"], iteration=1)
        self.assertEqual(len(r3.executor.import_calls), 1)

    def test_import_check_candidate_failure_after_good_probe_rejects(self):
        r = self.rig(import_script=[{"bad": imp_fail(), "slow": imp_fail(None, timed_out=True)}])
        for name in ("good", "bad", "slow"):
            r.candidate(name)
        out = r.runner.import_check(["good", "bad", "slow"], iteration=2)
        call = r.executor.import_calls[0]
        self.assertEqual(call["names"], ["no_memory", "good", "bad", "slow"])
        self.assertEqual(call["timeouts_s"], [120.0, 30.0, 30.0, 30.0])
        self.assertEqual({n: jobs.import_rejection(v) for n, v in out.items()},
                         {"good": None, "bad": "import", "slow": "import"})
        check = util.read_json(r.paths.import_check_dir(2, 1) / "check.json")
        self.assertEqual({n: v["rejected"] for n, v in check["results"].items()},
                         {"good": None, "bad": "import", "slow": "import"})
        self.assertTrue(check["probe"]["ok"])
        done = r.journal.last("import_check", "iter002")
        self.assertEqual((done["status"], done["detail"]),
                         ("complete", {"probe_ok": True, "results": {"good": True, "bad": False, "slow": False}}))
        self.assertEqual(r.clock.sleeps, [])

    def test_loaded_class_rejection(self):
        r = self.rig(import_script=[{"foreign": imp_fail(3, reason="foreign_class"),
                                     "noclass": imp_fail(4, reason="no_class")}])
        for name in ("foreign", "noclass"):
            r.candidate(name)
        out = r.runner.import_check(["foreign", "noclass"], iteration=1)
        self.assertEqual({n: jobs.import_rejection(v) for n, v in out.items()},
                         {"foreign": "loaded_class", "noclass": "loaded_class"})
        self.assertIsNone(jobs.import_rejection(imp_ok()))

    def test_import_check_bundles_stored_copies(self):
        r = self.rig()
        v1 = r.candidate("cand", b"# stored\n")
        (r.ws / "agents" / "cand.py").write_bytes(b"# changed after the proposal\n")
        seen = []

        def capture(names):
            from tests.fake_facade import tar_member
            seen.append(tar_member(r.executor.import_calls[-1]["bundle_tar"], "pkg/text_classification/agents/cand.py"))
            return None

        r.executor.import_script = [capture]
        r.runner.import_check(["cand"], iteration=1)
        self.assertEqual(seen, [v1])


# ---------------------------------------------------------------- the passthrough

class PassthroughTests(JobsBase):
    def test_passthrough_uses_dash_P_and_verifies_first(self):
        r = self.rig()
        r.runner.run_benchmark(["--memory", "no_memory"])
        seen = {}

        def fake_run(argv, **kw):
            seen.update(argv=argv, **kw)
            seen["benchmark"] = (r.ws / "benchmark.py").read_text()
            seen["planted"] = os.path.lexists(r.run_dir("USPTO", "ghost") / "val.json")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        python = str(r.base / "venv" / "bin" / "python")
        pt = jobs.make_passthrough(python, r.ws, r.logs, home=r.base / "home", run=fake_run)
        r.new_runner(new_journal=False, passthrough=pt)
        (r.ws / "benchmark.py").write_text("raise SystemExit('edited by the proposer')\n")
        planted = r.run_dir("USPTO", "ghost") / "val.json"
        planted.parent.mkdir(parents=True)
        planted.write_text('{"accuracy": 1.0}')
        cp = r.runner.run_benchmark(["--frontier", "--model", "m"])
        self.assertEqual(cp.returncode, 0)
        self.assertEqual(seen["argv"], [python, "-P", "benchmark.py", "--logs-dir", str(r.logs), "--results-dir",
                                        str(r.logs / "results"), "--frontier", "--model", "m"])
        self.assertEqual(seen["cwd"], str(r.ws))
        self.assertEqual(seen["env"], procenv.passthrough_env(home=str(r.base / "home"),
                                                              python_bin_dir=str(r.base / "venv" / "bin")))
        self.assertEqual((seen["capture_output"], seen["timeout"]), (True, 600))
        self.assertEqual(seen["benchmark"], "# synthetic pinned file benchmark.py\n")     # restored before the call
        self.assertFalse(seen["planted"])
        self.assertTrue(any(e["key"] == "restore" for e in r.journal_rows("workspace")))

        def slow(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw["timeout"])

        cp = jobs.make_passthrough(python, r.ws, r.logs, home=r.base, timeout_s=5, run=slow)(["--results", "--test"])
        self.assertEqual(cp.returncode, 124)

    @helpers.needs_example_dir
    @helpers.needs_yaml
    def test_planted_module_in_workspace_is_not_imported(self):
        r = self.rig(example=helpers.example_dir())
        marker = r.base / "imported.txt"
        (r.ws / "random.py").write_text(f"open({str(marker)!r}, 'w').write('x')\nraise SystemExit(7)\n")
        (r.ws / "yaml.py").write_text(f"open({str(marker)!r}, 'w').write('y')\nraise SystemExit(8)\n")
        python = helpers.example_python()
        python = str(python) if os.path.exists(python) else sys.executable
        home = r.base / "home"
        home.mkdir()
        pt = jobs.make_passthrough(python, r.ws, r.logs, home=home)
        result = r.run_dir("USPTO", "no_memory") / "val.json"
        result.parent.mkdir(parents=True)
        result.write_text(json.dumps({"accuracy": 0.25, "memory_context_chars": 0}))
        cp = pt(["--results"])
        self.assertEqual(cp.returncode, 0, cp.stderr[-2000:])
        self.assertFalse(marker.exists())
        summary = json.loads((r.logs / "summary.json").read_text())      # benchmark.py ran to its end
        self.assertEqual(summary["USPTO"]["no_memory"]["accuracy"], 0.25)


# ---------------------------------------------------------------- review regressions

class ReviewRegressionTests(JobsBase):
    def test_parallel_stop_starts_no_queued_job(self):
        import time

        def billing(spec, attempt_dir):
            raise ExecutorError(kind="billing")

        def slow_ok(spec, attempt_dir):
            time.sleep(0.3)
            return make_result({"kind": "ok"}, spec, attempt_dir)

        r = self.rig(datasets=DATASETS, overrides={"executor.parallel_dataset_jobs.validation": 2},
                     script=[billing, slow_ok, slow_ok])
        with self.assertRaises(CampaignStop) as cm:
            r.runner.run_benchmark(["--memory", "no_memory"])
        self.assertEqual((cm.exception.reason, cm.exception.detail), ("infrastructure", "billing"))
        self.assertEqual(len(r.executor.runs), 2)               # the job running beside it ends; the third never starts

    def test_other_stored_candidates_bundled_from_their_copies(self):
        r = self.rig()
        helper = r.candidate("helper", b"# helper as proposed\n")
        (r.ws / "agents" / "helper.py").write_bytes(b"# helper edited later\n")
        loose = r.candidate("loose", b"# never proposed\n", store=False)
        r.candidate("cand", b"# cand imports helper\n")
        r.runner.run_benchmark(["--memory", "cand"])
        members = util.read_json(r.paths.attempt_dir("val.cand.USPTO", 1) / "bundle.json")["members"]
        self.assertEqual(members["pkg/text_classification/agents/helper.py"], sha(helper))
        self.assertEqual(members["pkg/text_classification/agents/loose.py"], sha(loose))   # not pinned (V26 limit)

    def test_test_job_bytes_changed_during_bundle_counts_as_failed(self):
        r = self.rig()
        r.candidate("cand", b"# v1\n", store=False)
        r.runner.run_benchmark(["--memory", "cand"])
        real = r.runner.bundle_builder

        def racing(*a, **kw):
            (r.ws / "agents" / "cand.py").write_bytes(b"# changed mid-build\n")
            return real(*a, **kw)

        r.new_runner(new_journal=False, bundle_builder=racing)
        cp = r.runner.run_benchmark(["--memory", "cand", "--test"])
        self.assertEqual(cp.returncode, 1)
        self.assertEqual(len(r.executor.runs), 1)
        rec = r.record("test.cand.USPTO")
        self.assertEqual((rec["status"], rec["message"]), ("failed", "validated candidate file not found"))
        self.assertEqual(list(r.paths.jobs_root.rglob("bundle.tar")), [])

    def test_symlinked_run_dir_moves_nothing_outside_workspace(self):
        r = self.rig()
        r.runner.run_benchmark(["--memory", "no_memory"])
        rd = r.run_dir("USPTO", "no_memory")
        outside = r.base / "outside"
        outside.mkdir()
        (outside / "memory.json").write_text("a file outside the workspace")
        (outside / "val.json").write_text('{"accuracy": 1.0}')
        shutil.rmtree(rd)
        os.symlink(outside, rd)
        r.runner.verify_published()
        self.assertEqual(sorted(p.name for p in outside.iterdir()), ["memory.json", "val.json"])
        self.assertEqual((outside / "memory.json").read_text(), "a file outside the workspace")


# ---------------------------------------------------------------- against upstream's real builders

@helpers.needs_example_dir
@helpers.needs_yaml
class RealBuilderTests(JobsBase):
    def test_plan_jobs_against_real_benchmark_builders(self):
        r = self.rig(example=helpers.example_dir())
        scratch = r.paths.scratch_root / "logs"
        with workspace.load_upstream(r.paths) as up:
            facade = jobs.real_facade(up.benchmark)
            prefix = facade.inner_loop_prefix()
            self.assertEqual(len(prefix), 9)
            self.assertEqual(prefix[-2:], ["-m", "text_classification.inner_loop"])
            self.assertEqual(facade.datasets, DATASETS)
            planned, msg, skipped = jobs.plan_jobs(facade, "no_memory", test=False, logs_dir=scratch,
                                                   results_dir=scratch / "results", manifest=r.manifest)
            self.assertIsNone(msg)
            self.assertEqual([p.dataset for p in planned], DATASETS)
            sizes = r.manifest["upstream"]["expect"]["sizes"]
            for p in planned:
                argv = list(p.inner_argv)
                self.assertEqual(argv[:4], ["--memory", "agents/no_memory.py", "--dataset", p.dataset])
                self.assertEqual(argv[argv.index("--temperature") + 1], "0.0")
                self.assertEqual(argv[argv.index("--seed") + 1], "42")
                self.assertEqual(argv[argv.index("--mode") + 1], "offline")
                self.assertEqual(argv[argv.index("--model") + 1], "openrouter/openai/gpt-oss-120b")
                self.assertEqual([int(argv[argv.index(f) + 1]) for f in ("--num-train", "--num-val", "--num-test")],
                                 sizes[p.dataset])
                self.assertEqual(p.desc, f"val/{p.dataset}/no_memory/{MS}")
                self.assertTrue(p.upstream_command.startswith(" ".join(prefix) + " --memory"))
                self.assertEqual(p.host_outputs["val.json"], scratch / p.dataset / "no_memory" / MS / "val.json")
                self.assertNotIn("--force", argv)
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):               # upstream warns once per missing memory.json
                planned, msg, skipped = jobs.plan_jobs(facade, "no_memory", test=True, logs_dir=scratch,
                                                       results_dir=scratch / "results", manifest=r.manifest)
            self.assertEqual((planned, msg, skipped), ([], None, DATASETS))
            self.assertEqual(printed.getvalue().count("WARNING: no memory.json"), 3)
            self.assertEqual(jobs.plan_jobs(facade, "ghost", test=False, logs_dir=scratch,
                                            results_dir=scratch / "results", manifest=r.manifest)[1],
                             "Error: 'ghost' not found on disk.")


if __name__ == "__main__":
    unittest.main()
