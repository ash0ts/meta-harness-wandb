# SPDX-License-Identifier: MIT
"""Tests for ``mhforge/insandbox/mhf_selfcheck.py`` (spec 3.13, named cases 5.2).

The selfcheck runs as a subprocess of ``sys.executable`` with an explicit environment; the import checks run against
a temp copy of ``tests/fixtures/fake_pkg``. Egress probes are exercised in process with injected connect and resolve
functions under ``util.loopback_guard()``: no test opens a non-loopback socket.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mhforge import util, wire
from tests import helpers

SELFCHECK = helpers.SC / "mhforge" / "insandbox" / "mhf_selfcheck.py"
FAKE_PKG = helpers.FIXTURES / "fake_pkg"


def _load():
    spec = importlib.util.spec_from_file_location("mhf_selfcheck_under_test", SELFCHECK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sc = _load()


# Names the platform adds to every process environment by itself (macOS CoreFoundation), not by the selfcheck.
PLATFORM_NAMES = {"__CF_USER_TEXT_ENCODING"} if sys.platform == "darwin" else set()


def base_env(**extra):
    env = {"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8"}
    env.update(extra)
    return env


class SelfcheckCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mhf-sc-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cache = self.tmp / "cache"
        self.pkg = self.tmp / "pkg"
        shutil.copytree(FAKE_PKG, self.pkg, ignore=shutil.ignore_patterns("__pycache__"))

    def job(self, **over):
        job = {"schema": "mhforge.jobfile/1", "job_id": "import_check", "argv": [],
               "env": {"PATH": "/usr/bin:/bin", "HOME": str(self.tmp), "TMPDIR": str(self.tmp),
                       "PYTHONPATH": str(self.pkg), "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8",
                       "TEXT_CLASSIFICATION_LLM_CACHE_DIR": str(self.cache)},
               "cwd": str(self.pkg), "vocab_file": None, "probe_endpoints": [],
               "expect": {"machine": None, "python": None, "freeze_sha256": None}}
        job.update(over)
        path = self.tmp / ("job-%d.json" % random.randrange(1 << 30))
        path.write_text(json.dumps(job), encoding="utf-8")
        return path

    def run_check(self, *args, env=None, job=None):
        job = job or self.job()
        proc = subprocess.run([sys.executable, str(SELFCHECK), "--job", str(job), *args], env=env or base_env(),
                              capture_output=True, timeout=60)
        return proc

    def report(self, *args, env=None, job=None):
        proc = self.run_check(*args, env=env, job=job)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        lines = proc.stdout.decode().splitlines()
        self.assertEqual(len(lines), 1, "exactly one JSON line")
        return json.loads(lines[0]), proc.stdout.decode()

    # ------------------------------------------------------------------ names, credentials

    def test_reports_names_not_values(self):
        env = base_env(MY_SERVICE_TOKEN="value-aaaa-1111", DB_PASSWORD="value-bbbb-2222", EMPTY_SECRET="",
                       PLAIN_SETTING="value-cccc-3333", WANDB_API_KEY="value-dddd-4444")
        rep, raw = self.report(env=env)
        self.assertEqual(rep["schema"], "mhforge.selfcheck/1")
        self.assertEqual([n for n in rep["env_names"] if n not in PLATFORM_NAMES], sorted(env))
        self.assertEqual(rep["credential_names"], ["DB_PASSWORD", "MY_SERVICE_TOKEN", "WANDB_API_KEY"])
        self.assertNotIn("EMPTY_SECRET", rep["credential_names"])
        self.assertEqual(rep["wandb_api_key"], "set")
        for value in ("value-aaaa-1111", "value-bbbb-2222", "value-cccc-3333", "value-dddd-4444"):
            self.assertNotIn(value, raw)
        for key in ("python", "machine", "freeze_sha256", "distributions", "identity_token_file", "egress", "versions",
                    "tokenizer_dir", "vocab_file_present", "cache_writable", "expect_problems"):
            self.assertIn(key, rep)
        self.assertEqual(set(rep["versions"]), {"litellm", "openai", "openai-harmony", "datasets", "tenacity"})
        self.assertIsInstance(rep["identity_token_file"], bool)
        self.assertTrue(rep["cache_writable"])
        rep_empty, _ = self.report(env=base_env(WANDB_API_KEY=""))
        self.assertEqual(rep_empty["wandb_api_key"], "empty")
        rep_absent, _ = self.report()
        self.assertEqual(rep_absent["wandb_api_key"], "absent")

    def test_credential_names_exclude_openrouter_key(self):
        rep, raw = self.report(env=base_env(OPENROUTER_API_KEY="mhf-relay", OPENROUTER_API_BASE="http://127.0.0.1:1/v1",
                                            OTHER_API_KEY="x-other-value"))
        self.assertNotIn("OPENROUTER_API_KEY", rep["credential_names"])
        self.assertIn("OTHER_API_KEY", rep["credential_names"])
        self.assertIn("OPENROUTER_API_KEY", rep["env_names"])
        self.assertNotIn("x-other-value", raw)

    def test_tiktoken_cache_dir_is_not_a_credential(self):
        rep, _ = self.report(env=base_env(TIKTOKEN_RS_CACHE_DIR=str(self.tmp), TOKENIZERS_PARALLELISM="false",
                                          KEYBOARD="us", MONKEY_BUSINESS="1"))
        self.assertEqual(rep["credential_names"], [])
        # the whole-word rule: TOKEN, KEY, SECRET as words, not substrings
        self.assertTrue(sc.is_credential_name("GITHUB_TOKEN"))
        self.assertTrue(sc.is_credential_name("api_key"))
        self.assertTrue(sc.is_credential_name("AWS_SECRET_ACCESS_KEY"))
        self.assertTrue(sc.is_credential_name("CREDENTIALS"))
        self.assertFalse(sc.is_credential_name("TIKTOKEN_RS_CACHE_DIR"))
        self.assertFalse(sc.is_credential_name("TOKENIZERS_PARALLELISM"))
        self.assertFalse(sc.is_credential_name("MONKEY_BUSINESS"))

    def test_pattern_and_exempt_names_equal_wire(self):
        self.assertEqual(sc.CREDENTIAL_NAME_PATTERN, wire.CREDENTIAL_NAME_PATTERN)
        self.assertEqual(tuple(sc.CREDENTIAL_NAME_EXEMPT), tuple(wire.CREDENTIAL_NAME_EXEMPT))
        self.assertEqual(sc.CANDIDATE_RE, wire.CANDIDATE_RE.pattern)

    def test_insandbox_file_imports_nothing_from_mhforge(self):
        text = SELFCHECK.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"^\s*(?:from|import)\s+(?:mhforge|\.)", text, re.M))
        self.assertNotIn("environ.copy(", text)

    # ------------------------------------------------------------------ egress

    def test_egress_skipped_without_flag(self):
        rep, _ = self.report(env=base_env(KUBERNETES_SERVICE_HOST="10.0.0.1"),
                             job=self.job(probe_endpoints=["objects.example.com:443"]))
        self.assertEqual(rep["egress"]["tcp"], "skipped")
        self.assertEqual(rep["egress"]["dns"], "skipped")
        self.assertTrue(rep["egress"]["probes"])
        self.assertEqual({p["result"] for p in rep["egress"]["probes"]}, {"skipped"})
        # in process: without probe no connect or resolve is attempted at all
        calls = []
        out = sc.egress_report({}, [], probe=False, connect=lambda *a: calls.append(a) or "open",
                               resolve=lambda *a: calls.append(a) or "open")
        self.assertEqual(calls, [])
        self.assertEqual((out["tcp"], out["dns"]), ("skipped", "skipped"))

    def test_literal_address_probes_recorded_not_gated(self):
        resolv = self.tmp / "resolv.conf"
        resolv.write_text("# synthetic\nsearch example.internal\nnameserver 10.96.0.10\nnameserver 10.96.0.11\n")
        connects, lookups = [], []

        def connect(host, port):
            connects.append((host, port))
            return "open" if host == "169.254.169.254" else "blocked"

        def resolve(name):
            lookups.append(name)
            return "blocked"

        with util.loopback_guard():
            out = sc.egress_report({}, ["objects.example.com:443", "[fd00::1]:9000"], probe=True,
                                   resolv_conf=str(resolv), connect=connect, resolve=resolve)
        # the two gating probes: a literal-address TCP connect and one lookup
        self.assertEqual(out["tcp"], "blocked")
        self.assertEqual(out["dns"], "blocked")
        self.assertEqual(connects[0], ("1.1.1.1", 443))
        self.assertEqual(lookups, ["api.inference.wandb.ai"])
        probes = {p["name"]: p for p in out["probes"]}
        self.assertEqual(list(probes), ["metadata", "resolver", "cluster", "ipv6", "extra:objects.example.com:443",
                                        "extra:[fd00::1]:9000"])
        self.assertEqual(probes["metadata"], {"name": "metadata", "target": "169.254.169.254:80", "result": "open"})
        self.assertEqual(probes["resolver"]["target"], "10.96.0.10:53")
        self.assertEqual(probes["cluster"]["result"], "skipped")      # no KUBERNETES_SERVICE_HOST
        self.assertEqual(probes["ipv6"]["target"], "[2606:4700:4700::1111]:443")
        self.assertEqual(probes["extra:[fd00::1]:9000"]["target"], "[fd00::1]:9000")
        # an open recorded probe does not change the gating results
        self.assertNotIn("open", (out["tcp"], out["dns"]))
        self.assertNotIn(("10.96.0.11", 53), connects)
        with util.loopback_guard():
            out2 = sc.egress_report({"KUBERNETES_SERVICE_HOST": "10.96.0.1", "KUBERNETES_SERVICE_PORT": "6443"}, [],
                                    probe=True, resolv_conf=str(self.tmp / "missing.conf"),
                                    connect=lambda h, p: "blocked", resolve=lambda n: "open")
        probes2 = {p["name"]: p for p in out2["probes"]}
        self.assertEqual(probes2["cluster"], {"name": "cluster", "target": "10.96.0.1:6443", "result": "blocked"})
        self.assertEqual(probes2["resolver"]["result"], "skipped")    # no resolver file
        self.assertEqual(out2["dns"], "open")

    def test_malformed_probe_inputs_still_exit_zero(self):
        # an out-of-range cluster port is a blocked probe, not an OverflowError (loopback host only)
        with util.loopback_guard():
            out = sc.egress_report({"KUBERNETES_SERVICE_HOST": "127.0.0.1", "KUBERNETES_SERVICE_PORT": "99999"}, [],
                                   probe=True, resolv_conf=str(self.tmp / "missing.conf"), resolve=lambda n: "blocked",
                                   connect=lambda h, p: sc.tcp_connect(h, p) if h == "127.0.0.1" else "blocked")
        self.assertEqual({p["name"]: p["result"] for p in out["probes"]}["cluster"], "blocked")
        # probe_endpoints of the wrong type: still one JSON line and exit 0
        rep, _ = self.report(job=self.job(probe_endpoints=5))
        self.assertEqual([p["name"] for p in rep["egress"]["probes"]], ["metadata", "resolver", "cluster", "ipv6"])

    def test_real_probe_functions_time_out_on_loopback(self):
        # tcp_connect against a closed loopback port reports blocked (the real function, loopback only)
        with util.loopback_guard():
            self.assertEqual(sc.tcp_connect("127.0.0.1", 9, timeout=1.0), "blocked")
        self.assertIsNone(sc._split_endpoint("no-port"))
        self.assertEqual(sc._split_endpoint("host.example:8443"), ("host.example", 8443))

    # ------------------------------------------------------------------ proof

    def test_freeze_hash_is_stable_and_order_independent(self):
        pairs = [("Foo_Bar.baz", "1.0"), ("requests", "2.32.3"), ("zz-top", "0.1"), ("A__B", "3")]
        expected_text = "a-b==3\nfoo-bar-baz==1.0\nrequests==2.32.3\nzz-top==0.1\n"
        digest, count = sc.freeze_sha256(pairs)
        self.assertEqual(digest, hashlib.sha256(expected_text.encode()).hexdigest())
        self.assertEqual(count, 4)
        for seed in range(5):
            shuffled = list(pairs)
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(sc.freeze_sha256(shuffled), (digest, 4))
        self.assertEqual(sc.freeze_sha256(pairs + [("foo-bar-baz", "1.0")]), (digest, 4))
        self.assertNotEqual(sc.freeze_sha256([("requests", "2.32.4")] + pairs[:1])[0], digest)
        # the subprocess reports the hash of its own installed distributions, the same on every run
        rep1, _ = self.report()
        rep2, _ = self.report()
        self.assertEqual(rep1["freeze_sha256"], rep2["freeze_sha256"])
        self.assertRegex(rep1["freeze_sha256"], r"[0-9a-f]{64}")
        own = sc.freeze_sha256(sc.installed_pairs())
        self.assertEqual((rep1["freeze_sha256"], rep1["distributions"]), own)

    def test_expect_problems_for_machine_python_and_freeze(self):
        self.assertEqual(sc.expect_problems({"machine": "x86_64", "python": "3.13", "freeze_sha256": None},
                                            "x86_64", "3.13.18", "f" * 64), [])
        problems = sc.expect_problems({"machine": "x86_64", "python": "3.13", "freeze_sha256": "a" * 64},
                                      "arm64", "3.12.4", "b" * 64)
        self.assertEqual([p.split(":")[0] for p in problems], ["machine", "python", "freeze_sha256"])
        self.assertEqual(sc.expect_problems({"machine": None, "python": "3.1", "freeze_sha256": None},
                                            "x86_64", "3.13.0", "c"), ["python: expected 3.1, got 3.13.0"])
        import platform
        rep, _ = self.report(job=self.job(expect={"machine": "not-a-machine", "python": "2.7",
                                                  "freeze_sha256": "0" * 64}))
        self.assertEqual([p.split(":")[0] for p in rep["expect_problems"]], ["machine", "python", "freeze_sha256"])
        ok, _ = self.report(job=self.job(expect={"machine": platform.machine(),
                                                 "python": ".".join(platform.python_version().split(".")[:2]),
                                                 "freeze_sha256": rep["freeze_sha256"]}))
        self.assertEqual(ok["expect_problems"], [])

    def test_tokenizer_presence(self):
        fake_site = self.tmp / "site"
        tok = fake_site / "litellm" / "litellm_core_utils" / "tokenizers"
        tok.mkdir(parents=True)
        (fake_site / "litellm" / "__init__.py").write_text("raise RuntimeError('must not be imported')\n")
        env = base_env(PYTHONPATH=str(fake_site))
        rep, _ = self.report(env=env, job=self.job(vocab_file=helpers.VOCAB_FILE))
        self.assertEqual(rep["tokenizer_dir"], str(tok))
        self.assertFalse(rep["vocab_file_present"])
        (tok / helpers.VOCAB_FILE).write_bytes(b"synthetic")
        rep, _ = self.report(env=env, job=self.job(vocab_file=helpers.VOCAB_FILE))
        self.assertTrue(rep["vocab_file_present"])
        # no LiteLLM at all: no directory; a null vocab_file skips the check
        rep, _ = self.report(job=self.job(vocab_file=helpers.VOCAB_FILE))
        self.assertIsNone(rep["tokenizer_dir"])
        self.assertFalse(rep["vocab_file_present"])
        rep, _ = self.report(job=self.job(vocab_file=None))
        self.assertTrue(rep["vocab_file_present"])

    @helpers.needs_example_venv
    def test_tokenizer_presence_in_example_venv(self):
        env = base_env()
        proc = subprocess.run([str(helpers.example_python()), str(SELFCHECK), "--job",
                               str(self.job(vocab_file=helpers.VOCAB_FILE))], env=env, capture_output=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        rep = json.loads(proc.stdout)
        self.assertEqual(rep["tokenizer_dir"], str(helpers.tokenizer_dir()))
        self.assertTrue(rep["vocab_file_present"])
        self.assertIsNotNone(rep["versions"]["litellm"])

    def test_cache_not_writable_reported(self):
        blocker = self.tmp / "afile"
        blocker.write_text("x")
        job = self.job()
        doc = json.loads(job.read_text())
        doc["env"]["TEXT_CLASSIFICATION_LLM_CACHE_DIR"] = str(blocker / "sub")
        job.write_text(json.dumps(doc))
        rep, _ = self.report(job=job)
        self.assertFalse(rep["cache_writable"])

    def test_unreadable_job_file_still_exits_zero(self):
        rep, _ = self.report(job=self.tmp / "missing.json")
        self.assertIn("job_file: unreadable", rep["expect_problems"])

    # ------------------------------------------------------------------ import check

    def test_import_check_ok_and_fail(self):
        ok = self.run_check("--import-check", "ok_agent")
        self.assertEqual(ok.returncode, 0, ok.stderr.decode())
        self.assertEqual(ok.stdout.decode().strip(), "OK")
        bad = self.run_check("--import-check", "bad_import")
        self.assertNotEqual(bad.returncode, 0)
        self.assertNotIn(b"OK", bad.stdout)
        self.assertIn(b"mhf_fake_module_that_does_not_exist", bad.stderr)
        missing = self.run_check("--import-check", "no_such_agent")
        self.assertNotEqual(missing.returncode, 0)
        self.assertNotIn(b"OK", missing.stdout)
        self.assertFalse(list(self.pkg.rglob("__pycache__")), "the job env keeps bytecode off")

    def test_import_check_uses_job_env_not_own(self):
        marker = self.pkg / "text_classification" / "agents" / "env_probe.py"
        marker.write_text("import os\nfrom ..memory_system import MemorySystem\n"
                          "assert 'SELFCHECK_OWN_ONLY' not in os.environ\n"
                          "assert os.environ['TEXT_CLASSIFICATION_LLM_CACHE_DIR']\n"
                          "assert os.getcwd() == %r\n"
                          "class EnvProbe(MemorySystem):\n    pass\n" % str(self.pkg.resolve()))
        proc = self.run_check("--import-check", "env_probe", env=base_env(SELFCHECK_OWN_ONLY="1"))
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertEqual(proc.stdout.decode().strip(), "OK")

    def test_import_check_rejects_bad_name(self):
        for name in ("Bad", "1abc", "a-b", "a.b", "x" * 49, "ok_agent\n", "../x", ""):
            proc = self.run_check("--import-check", name)
            self.assertEqual(proc.returncode, 2, name)
            self.assertNotIn(b"OK", proc.stdout)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(sc.import_check({"env": {}, "cwd": "/"}, "Bad"), 2)
        self.assertIn("bad candidate name", err.getvalue())

    def test_import_check_rejects_foreign_loaded_class(self):
        proc = self.run_check("--import-check", "foreign_class")
        self.assertEqual(proc.returncode, 3, proc.stderr.decode())
        self.assertEqual(proc.stdout.decode().strip(), "FOREIGN_CLASS text_classification.agents.ok_agent.OkAgent")

    def test_import_check_no_class_exits_4(self):
        (self.pkg / "text_classification" / "agents" / "no_class.py").write_text(
            "# SPDX-License-Identifier: MIT\nVALUE = 1\n\nclass NotAMemorySystem:\n    pass\n")
        proc = self.run_check("--import-check", "no_class")
        self.assertEqual(proc.returncode, 4, proc.stderr.decode())
        self.assertEqual(proc.stdout.decode().strip(), "NO_CLASS")

    def test_import_check_code_survives_star_shadowing(self):
        (self.pkg / "text_classification" / "agents" / "shadow.py").write_text(
            "from ..memory_system import MemorySystem\n__all__ = ['sys', 'inspect', 'importlib', 'Shadow']\n"
            "sys = None\ninspect = None\nimportlib = None\n"
            "class Shadow(MemorySystem):\n    pass\n")
        proc = self.run_check("--import-check", "shadow")
        self.assertEqual(proc.returncode, 0, proc.stderr.decode())
        self.assertEqual(proc.stdout.decode().strip(), "OK")
        self.assertIn("{name}", sc.IMPORT_CHECK_CODE)
        self.assertTrue(sc.IMPORT_CHECK_CODE.startswith("from text_classification.agents.{name} import *\n"))


if __name__ == "__main__":
    unittest.main()
