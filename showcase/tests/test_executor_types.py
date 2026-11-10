# SPDX-License-Identifier: MIT
"""executors/__init__.py: the attempt classification table, the selfcheck verdict and the sandbox price (spec 3.16)."""

from __future__ import annotations

import dataclasses
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from mhforge import bundle, procenv, util
from mhforge.executors import (TAINT_CODES, ImportResult, JobResult, JobSpec, check_selfcheck, classify_attempt,
                               merge_refusal_codes, positive_codes, sandbox_cost_micro)
from mhforge.wire import ATTEMPT_OUTCOMES, FAILURE_CLASSES, INFRA_OUTCOMES, JobIdentity
from tests.helpers import child_env, make_manifest

WORKER = """\
import json, sys, urllib.error, urllib.request
from os import environ
out = sys.argv[sys.argv.index("--val-output") + 1]
req = urllib.request.Request(environ["OPENROUTER_API_BASE"] + "/chat/completions", method="POST",
                             data=json.dumps({"model": "m", "messages": []}).encode(),
                             headers={"Content-Type": "application/json"})
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    opener.open(req, timeout=30).read()
except urllib.error.HTTPError as exc:
    sys.stderr.write("refused %d\\n" % exc.code)
with open(out, "w") as fh:
    json.dump({"accuracy": 0.5}, fh)
"""

EXIT_OK = {"schema": "mhforge.exit/1", "returncode": 0, "reason": "exit", "timed_out": False, "t_start": 1.0,
           "t_end": 2.0, "local_refusals": {"relay_unavailable": 0, "request_too_large": 0}, "unanswered_at_exit": 0}


def result(**over) -> JobResult:
    base = dict(returncode=0, timed_out=False, exit_file=dict(EXIT_OK), relay_end="exit", kill_reason=None,
                started_utc="2026-11-10T00:00:00.000000Z", ended_utc="2026-11-10T00:01:00.000000Z", outputs={},
                gateway={}, refusal_codes={}, stderr_tail="", host_sleep_s=0.0, facts={})
    base.update(over)
    return JobResult(**base)


def exit_file(**over) -> dict:
    doc = dict(EXIT_OK)
    doc.update(over)
    return doc


class ClassifyTable(unittest.TestCase):
    def check(self, res, expected, *, usable=True, halted=False):
        got = classify_attempt(res, result_usable=usable, halted=halted)
        self.assertEqual(got, expected)
        self.assertIn(got[0], ATTEMPT_OUTCOMES)
        self.assertTrue(got[2] is None or got[2] in FAILURE_CLASSES)
        return got

    def test_classify_attempt_table(self):
        # one case per row, in order; each case would also match a later row, so the order is what is tested
        rows = [
            # 1 halted: a budget refusal while halted, even with exit 0 and a usable result
            (result(refusal_codes={"budget_exhausted": 3}), True, ("halted", True, None)),
            # 2 stream budget beats a relay gap and a taint
            (result(relay_end="stream_budget", refusal_codes={"provider_unavailable": 1}), False,
             ("request_ceiling", False, "stream_budget")),
            # 3 relay gap beats a taint
            (result(relay_end="eof", refusal_codes={"provider_unavailable": 1}), False, ("relay_lost", True, None)),
            # 4 taint beats the job's own ceilings
            (result(refusal_codes={"gateway_internal": 1, "job_request_ceiling": 1}), False,
             ("provider_unavailable", True, None)),
            # 5 request ceiling beats relay_overloaded
            (result(refusal_codes={"job_request_ceiling": 2, "relay_overloaded": 1}), False,
             ("request_ceiling", False, "request_ceiling")),
            # 6 relay_overloaded, whatever the return code says
            (result(refusal_codes={"relay_overloaded": 4}), False, ("request_ceiling", False, "relay_overloaded")),
            # 7 ok
            (result(), False, ("ok", False, None)),
            # 8 host sleep turns a timeout into an interruption
            (result(returncode=124, timed_out=True, exit_file=exit_file(reason="timeout", returncode=124),
                    host_sleep_s=900.0), False, ("interrupted", True, "host_sleep")),
            # 9 timeout
            (result(returncode=124, timed_out=True, exit_file=exit_file(reason="timeout", returncode=124)), False,
             ("timeout", False, None)),
            # 10 no exit file, whatever the return code
            (result(exit_file=None), False, ("executor_error", True, None)),
            # 11 return code 0 without a usable result
            ("unusable", False, ("no_result", False, None)),
            # 12 anything else
            (result(returncode=1, exit_file=exit_file(returncode=1), stderr_tail="Traceback (most recent call last)"),
             False, ("worker_error", False, "worker_exception")),
        ]
        for i, (res, halted, expected) in enumerate(rows, 1):
            with self.subTest(row=i):
                if res == "unusable":
                    self.check(result(), expected, usable=False)
                else:
                    self.check(res, expected, halted=halted)

    def test_row_twelve_failure_classes(self):
        cases = [("ModuleNotFoundError: No module named 'x'", "dependency_missing"),
                 ("No module named litellm", "dependency_missing"),
                 ("ImportError: cannot import name 'y'", "import_error"),
                 ("Traceback ...\nopenai_harmony.HarmonyError: vocab", "harmony_vocab"),
                 ("Traceback (most recent call last):\nValueError", "worker_exception"),
                 ("killed", "unknown")]
        for tail, cls in cases:
            with self.subTest(tail=tail):
                self.check(result(returncode=1, exit_file=exit_file(returncode=1), stderr_tail=tail),
                           ("worker_error", False, cls))
        self.check(result(returncode=1, exit_file=exit_file(returncode=1), stderr_tail="Traceback",
                          refusal_codes={"context_length_exceeded": 1}), ("worker_error", False, "context_limit"))

    def test_exit_zero_with_infra_refusals_is_not_ok(self):
        self.assertEqual(TAINT_CODES, {"halted", "budget_exhausted", "provider_unavailable", "gateway_internal",
                                       "relay_unavailable"})
        for code in sorted(TAINT_CODES):
            with self.subTest(code=code):
                outcome, infra, _ = classify_attempt(result(refusal_codes={code: 1}), result_usable=True, halted=False)
                self.assertNotEqual(outcome, "ok")
                self.assertTrue(infra)
                self.assertIn(outcome, INFRA_OUTCOMES)

    def test_timeout_with_provider_unavailable_is_infra(self):
        res = result(returncode=124, timed_out=True, exit_file=exit_file(reason="timeout", returncode=124),
                     refusal_codes={"provider_unavailable": 7})
        self.assertEqual(classify_attempt(res, result_usable=False, halted=False), ("provider_unavailable", True, None))

    def test_stub_local_refusals_reach_classification(self):
        # end to end: a job file from bundle.build_job_file, the real stub in stdio mode with a host that never answers,
        # a worker that gets the stub's relay_unavailable refusal, exits 0 and writes a usable result anyway
        with tempfile.TemporaryDirectory(prefix="mhf-cls-") as tmp:
            box = Path(tmp) / "box"
            pkg = box / "pkg" / "text_classification"
            pkg.mkdir(parents=True)
            for d in bundle.BOX_DIRS:
                (box / d).mkdir()
            (pkg / "__init__.py").write_text("")
            (pkg / "inner_loop.py").write_text(WORKER)
            env = procenv.worker_env(path="/usr/bin:/bin", home=str(box / "home"), tmp=str(box / "tmp"),
                                     pythonpath=str(box / "pkg"), cache_dir=str(box / "cache"),
                                     tokenizer_dir=str(box / "tmp"))
            manifest = make_manifest({"relay.reply_timeout_s": 1})
            doc = bundle.build_job_file(JobIdentity("mh-test", "val", "cand", "USPTO", 1, "search", 1),
                                        python=sys.executable, box=str(box), inner_argv=("--val-output", "{out}/val.json"),
                                        env=env, route=2, timeout_s=60, expect_outputs=("val.json",), manifest=manifest,
                                        vocab_file=None, capture=False)
            util.write_json(box / "job.json", doc)
            stub = Path(bundle.__file__).parent / "insandbox" / "mhf_relay_stub.py"
            proc = subprocess.Popen([sys.executable, str(stub), "serve", "--job", str(box / "job.json"), "--uplink",
                                     "stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, env=child_env(), bufsize=0)
            drain = threading.Thread(target=lambda: proc.stdout.read(), daemon=True)
            drain.start()
            try:
                self.assertEqual(proc.wait(timeout=60), 0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                proc.stdin.close()
                drain.join(5)
                proc.stdout.close()
            ef = util.read_json(box / "run" / "exit.json")
            self.assertEqual((ef["reason"], ef["returncode"]), ("exit", 0))
            outputs = bundle.collect_outputs(box, Path(tmp) / "a1")
            usable = bundle.result_is_usable(Path(tmp) / "a1" / "out" / "val.json")
            self.assertTrue(usable)
            codes = merge_refusal_codes({}, ef, 0)
            self.assertEqual(codes, {"relay_unavailable": 1})
            res = result(exit_file=ef, outputs=outputs, refusal_codes=codes)
            self.assertEqual(classify_attempt(res, result_usable=usable, halted=False), ("provider_unavailable", True, None))

    def test_merge_drops_zero_counts(self):
        # the stub's own relay_unavailable refusals exist only in the exit file; the merge brings them to the table
        ef = exit_file(local_refusals={"relay_unavailable": 2, "request_too_large": 0})
        codes = merge_refusal_codes({}, ef, 0)
        self.assertEqual(codes, {"relay_unavailable": 2})
        self.assertEqual(classify_attempt(result(exit_file=ef, refusal_codes=codes), result_usable=True, halted=False),
                         ("provider_unavailable", True, None))
        # the zero counts of a clean exit file are no refusal
        clean = merge_refusal_codes({"provider_rejected": 0}, dict(EXIT_OK), 0)
        self.assertEqual(clean, {})
        self.assertEqual(classify_attempt(result(refusal_codes=clean), result_usable=True, halted=False)[0], "ok")
        # an unmerged zero count passed straight through is ignored too
        self.assertEqual(classify_attempt(result(refusal_codes={"relay_unavailable": 0}), result_usable=True,
                                          halted=False)[0], "ok")

    def test_merge_refusal_codes_sums_three_sources(self):
        codes = merge_refusal_codes({"request_too_large": 1, "halted": 2},
                                    exit_file(local_refusals={"request_too_large": 3}), 5)
        self.assertEqual(codes, {"halted": 2, "relay_overloaded": 5, "request_too_large": 4})
        self.assertEqual(merge_refusal_codes(None, None, 0), {})
        self.assertEqual(positive_codes({"a": True, "b": -1, "c": "3", "d": 2, "e": None, "f": False, "g": 0}),
                         {"a": 1, "c": 1, "d": 2})

    def test_malformed_taint_counts_are_never_ok(self):
        # review regression: a tainted code with a non-integer count, or a malformed unanswered_at_exit, was read as zero
        for value in ("1", 1.0, True, [1]):
            with self.subTest(value=value):
                res = result(refusal_codes={"provider_unavailable": value})
                self.assertEqual(classify_attempt(res, result_usable=True, halted=False), ("provider_unavailable", True, None))
        res = result(exit_file=exit_file(unanswered_at_exit="2"))
        self.assertEqual(classify_attempt(res, result_usable=True, halted=False), ("relay_lost", True, None))
        self.assertEqual(merge_refusal_codes({}, exit_file(local_refusals={"relay_unavailable": "1"}), 0),
                         {"relay_unavailable": 1})

    def test_reattach_exhausted_is_relay_lost_not_worker_error(self):
        # the worker gave up after the uplink was gone and exited 1 by itself
        res = result(returncode=1, relay_end="error", exit_file=exit_file(returncode=1, unanswered_at_exit=0),
                     stderr_tail="Traceback (most recent call last):\nopenai.APIConnectionError")
        self.assertEqual(classify_attempt(res, result_usable=False, halted=False), ("relay_lost", True, None))
        res = result(returncode=-9, relay_end="exit", exit_file=exit_file(returncode=-9, reason="uplink_lost"))
        self.assertEqual(classify_attempt(res, result_usable=False, halted=False), ("relay_lost", True, None))
        for end in ("eof", "error", "deadline"):
            self.assertEqual(classify_attempt(result(relay_end=end), result_usable=True, halted=False)[0], "relay_lost")

    def test_unanswered_at_exit_is_relay_gap_only_for_self_exit(self):
        res = result(returncode=0, exit_file=exit_file(unanswered_at_exit=2))
        self.assertEqual(classify_attempt(res, result_usable=True, halted=False), ("relay_lost", True, None))
        res = result(returncode=124, timed_out=True, exit_file=exit_file(reason="timeout", returncode=124,
                                                                          unanswered_at_exit=3))
        self.assertEqual(classify_attempt(res, result_usable=False, halted=False), ("timeout", False, None))
        res = result(returncode=-9, exit_file=exit_file(reason="killed", returncode=-9, unanswered_at_exit=3))
        self.assertEqual(classify_attempt(res, result_usable=False, halted=False), ("worker_error", False, "unknown"))
        # the loopback ingress (relay_end None) with no unanswered requests is no gap
        self.assertEqual(classify_attempt(result(relay_end=None), result_usable=True, halted=False)[0], "ok")

    def test_stream_budget_relay_overloaded_and_refusal_flood_are_the_jobs_ceiling(self):
        cases = [(result(relay_end="stream_budget", returncode=-9, exit_file=None), "stream_budget"),
                 (result(refusal_codes={"relay_overloaded": 64}), "relay_overloaded"),
                 (result(kill_reason="refusal_flood", returncode=-9, exit_file=exit_file(reason="killed", returncode=-9)),
                  "refusal_flood")]
        for res, cls in cases:
            with self.subTest(cls=cls):
                self.assertEqual(classify_attempt(res, result_usable=False, halted=False),
                                 ("request_ceiling", False, cls))
                self.assertNotIn("request_ceiling", INFRA_OUTCOMES)

    def test_halt_kill_is_halted(self):
        res = result(returncode=-9, kill_reason="halt", exit_file=exit_file(reason="killed", returncode=-9))
        self.assertEqual(classify_attempt(res, result_usable=False, halted=True), ("halted", True, None))
        # a halted taint while the ledger is halted, even after a clean exit with a usable result
        res = result(refusal_codes={"halted": 1})
        self.assertEqual(classify_attempt(res, result_usable=True, halted=True), ("halted", True, None))
        # the same taint without a halt is still never ok
        self.assertEqual(classify_attempt(res, result_usable=True, halted=False), ("provider_unavailable", True, None))

    def test_host_sleep_makes_timeout_interrupted(self):
        res = result(returncode=None, timed_out=True, exit_file=None, host_sleep_s=120.5)
        self.assertEqual(classify_attempt(res, result_usable=False, halted=False), ("interrupted", True, "host_sleep"))
        res = result(returncode=None, timed_out=False, exit_file=None, host_sleep_s=61.0)
        self.assertEqual(classify_attempt(res, result_usable=False, halted=False), ("interrupted", True, "host_sleep"))
        # a sleep during an attempt that ended well changes nothing
        self.assertEqual(classify_attempt(result(host_sleep_s=300.0), result_usable=True, halted=False)[0], "ok")
        # a worker error during which the host slept stays the candidate's
        res = result(returncode=1, exit_file=exit_file(returncode=1), host_sleep_s=300.0, stderr_tail="Traceback")
        self.assertEqual(classify_attempt(res, result_usable=False, halted=False)[0], "worker_error")


GOOD_REPORT = {"schema": "mhforge.selfcheck/1", "credential_names": [], "wandb_api_key": "empty",
               "egress": {"tcp": "blocked", "dns": "blocked",
                          "probes": [{"name": "metadata", "target": "169.254.169.254:80", "result": "blocked"}]},
               "vocab_file_present": True, "cache_writable": True, "expect_problems": []}


class Selfcheck(unittest.TestCase):
    def report(self, **over):
        doc = {k: (dict(v) if isinstance(v, dict) else v) for k, v in GOOD_REPORT.items()}
        doc.update(over)
        return doc

    def test_check_selfcheck(self):
        self.assertEqual(check_selfcheck(self.report(), isolated=True), ([], []))
        iso, env = check_selfcheck(self.report(credential_names=["MHFORGE_X_KEY"], wandb_api_key="set",
                                               egress={"tcp": "open", "dns": "open", "probes": []}), isolated=True)
        self.assertEqual(iso, ["credential_names: MHFORGE_X_KEY", "wandb_api_key: set", "egress.tcp: open",
                               "egress.dns: open"])
        self.assertEqual(env, [])
        # a local executor is not isolated: the same report has no isolation problem
        self.assertEqual(check_selfcheck(self.report(credential_names=["A_TOKEN"], wandb_api_key="set"),
                                         isolated=False), ([], []))
        iso, env = check_selfcheck(self.report(vocab_file_present=False, cache_writable=False), isolated=True)
        self.assertEqual((iso, env), ([], ["vocab_file_present: false", "cache_writable: false"]))
        # vocab_file null in the job file: the selfcheck skips the check (True); None is accepted as skipped too
        self.assertEqual(check_selfcheck(self.report(vocab_file_present=None), isolated=True), ([], []))
        # skipped probes (first_job mode) are no problem
        self.assertEqual(check_selfcheck(self.report(egress={"tcp": "skipped", "dns": "skipped", "probes": []}),
                                         isolated=True), ([], []))
        # a malformed report proves nothing
        iso, env = check_selfcheck({}, isolated=True)
        self.assertIn("credential_names: missing", iso)
        self.assertIn("egress: missing", iso)
        self.assertIn("expect_problems: missing", env)
        self.assertEqual(check_selfcheck("x", isolated=True), (["report: not an object"], ["report: not an object"]))

    def test_selfcheck_expect_problems_are_environment_problems(self):
        problems = ["machine: expected x86_64, got aarch64", "python: expected 3.13, got 3.12.1",
                    "freeze_sha256: expected " + "a" * 64 + ", got " + "b" * 64]
        iso, env = check_selfcheck(self.report(expect_problems=problems), isolated=True)
        self.assertEqual(iso, [])
        self.assertEqual(env, problems)

    def test_recorded_probes_are_never_isolation_problems(self):
        probes = [{"name": n, "target": t, "result": "open"} for n, t in
                  (("metadata", "169.254.169.254:80"), ("resolver", "10.0.0.10:53"), ("cluster", "10.96.0.1:443"),
                   ("ipv6", "[2606:4700:4700::1111]:443"), ("extra:objects.example:443", "objects.example:443"))]
        iso, env = check_selfcheck(self.report(egress={"tcp": "blocked", "dns": "blocked", "probes": probes}),
                                   isolated=True)
        self.assertEqual((iso, env), ([], []))


class Price(unittest.TestCase):
    def test_sandbox_cost_micro(self):
        m = make_manifest()
        # draft prices: 2 CPU x 0.128 + 4 GiB x 0.0212 = 0.3408 USD per hour; 8,250 s = 0.781000 USD
        self.assertEqual(sandbox_cost_micro(8_250_000, "2", "4Gi", m), 781_000)
        self.assertEqual(sandbox_cost_micro(3_600_000, "2", "4Gi", m), 340_800)
        self.assertEqual(sandbox_cost_micro(0, "2", "4Gi", m), 0)
        self.assertEqual(sandbox_cost_micro(1, "2", "4Gi", m), 1)            # rounded up, never down to zero
        self.assertEqual(sandbox_cost_micro(3_600_000, "500m", "512Mi", m), 64_000 + 10_600)
        self.assertEqual(sandbox_cost_micro(3_600_000, "1", str(2 * 1024 ** 3), m), 128_000 + 42_400)
        m2 = make_manifest({"sandbox.price_cpu_hour": "0.000001", "sandbox.price_gib_hour": "0"})
        self.assertEqual(sandbox_cost_micro(1, "1", "1Gi", m2), 1)
        for bad in ((-1, "2", "4Gi"), (1.5, "2", "4Gi"), (True, "2", "4Gi"), (1, "two", "4Gi"), (1, "2", "4GB")):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                sandbox_cost_micro(bad[0], bad[1], bad[2], m)


class Types(unittest.TestCase):
    def test_dataclass_fields_match_the_contract(self):
        self.assertEqual([f.name for f in dataclasses.fields(JobSpec)],
                         ["identity", "inner_argv", "route", "timeout_s", "inputs", "expect_outputs", "capture",
                          "bundle_tar", "bundle_sha256", "expect_freeze_sha256"])
        self.assertEqual([f.name for f in dataclasses.fields(JobResult)],
                         ["returncode", "timed_out", "exit_file", "relay_end", "kill_reason", "started_utc", "ended_utc",
                          "outputs", "gateway", "refusal_codes", "stderr_tail", "host_sleep_s", "facts"])
        self.assertEqual([f.name for f in dataclasses.fields(ImportResult)],
                         ["ok", "returncode", "timed_out", "stdout_tail", "stderr_tail", "reason"])
        job = JobIdentity("mh-test", "val", "no_memory", "USPTO", 1, "baseline", None)
        spec = JobSpec(job, ("--val-output", "{out}/val.json"), 2, 7200, (), ("val.json",), True, Path("/x.tar"), "0" * 64)
        self.assertIsNone(spec.expect_freeze_sha256)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            spec.route = 1
        self.assertIsNone(ImportResult(True, 0, False, "OK", "").reason)


if __name__ == "__main__":
    unittest.main()
