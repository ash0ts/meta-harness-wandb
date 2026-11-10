# SPDX-License-Identifier: MIT
"""Tests for ``mhforge/insandbox/mhf_capture_entry.py`` (spec 3.15, sidecar rows 2.8, named cases 5.2).

Workers run as subprocesses of ``sys.executable`` against ``tests/fixtures/fake_pkg`` and a loopback completion
server defined here (bound to 127.0.0.1 only; children get no key and only that URL). The ``needs_example_venv``
cases run upstream's real worker with the example's venv Python. Standard library only.
"""

import glob
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SC = Path(__file__).resolve().parents[1]
ENTRY = SC / "mhforge" / "insandbox" / "mhf_capture_entry.py"
FAKE_PKG = SC / "tests" / "fixtures" / "fake_pkg"
EXAMPLE_DIR = SC.parent / "reference_examples" / "text_classification"
VENV_PY = EXAMPLE_DIR / ".venv" / "bin" / "python"
VOCAB_FILE = "fb374d419588a4632f3f557e76b4b70aebbca790"
MODEL = "openrouter/openai/gpt-oss-120b"
REASONING_MARKER = "SYNTHETIC-REASONING-MARKER"
TIME_FIELDS = ("timestamp", "runtime_seconds")


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _tokenizer_dir():
    hits = sorted(glob.glob(str(EXAMPLE_DIR / ".venv/lib/python3*/site-packages/litellm/litellm_core_utils/tokenizers")))
    return Path(hits[-1]) if hits else None


def needs_example_venv(fn):
    tok = _tokenizer_dir()
    ok = VENV_PY.exists() and tok is not None and (tok / VOCAB_FILE).exists()
    return unittest.skipUnless(ok, "example venv or bundled tokenizer file missing")(fn)


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ce = _load("mhf_capture_entry_under_test", ENTRY)
fake_data = _load("fake_tc_data_under_test", FAKE_PKG / "text_classification" / "data.py")


# --------------------------------------------------------------------------------------------------------------
class CompletionServer:
    """Loopback chat-completions server: ``rule(user_text) -> (status, content)``; records every user message."""

    def __init__(self, rule):
        self.rule, self.requests, self._lock = rule, [], threading.Lock()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                user = [m["content"] for m in body.get("messages", []) if m.get("role") == "user"][-1]
                with server._lock:
                    server.requests.append(user)
                status, content = server.rule(user)
                reply = {"id": "x", "object": "chat.completion", "created": 0, "model": body.get("model"),
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                                      "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
                data = json.dumps(reply if status == 200 else {"error": {"message": "bad request"}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.base = "http://127.0.0.1:%d/v1" % self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def hint_rule(user):
    if "PLEASE_FAIL" in user:
        return 400, ""
    found = re.findall(r"hint: ([^\n]*)", user)
    answer = found[-1].strip() if found else "unknown"
    return 200, json.dumps({"reasoning": REASONING_MARKER, "final_answer": answer})


def rows_of(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


AGENTS = {
    "probe_agent.py": '''
import json, threading
from ..memory_system import MemorySystem, extract_json_field
PROMPT = "Probe.\\n\\n{input}\\n"
_LOCK = threading.Lock()
class ProbeAgent(MemorySystem):
    def predict(self, input):
        r1 = self.call_llm(PROMPT.format(input=input))
        try:
            self._llm("PLEASE_FAIL " + input)
            err = None
        except Exception as exc:
            err = "%s: %s" % (type(exc).__name__, exc)
        b = self._llm.batch([PROMPT.format(input=input) + "b1", PROMPT.format(input=input) + "b2"])
        with _LOCK, open("probe.jsonl", "a") as f:
            f.write(json.dumps({"input": input, "r1": r1, "err": err, "batch": b}, sort_keys=True) + "\\n")
        return extract_json_field(r1, "final_answer"), {}
    def learn_from_batch(self, batch_results): pass
    def get_state(self): return "{}"
    def set_state(self, state): pass
''',
    "batch_agent.py": '''
from ..memory_system import MemorySystem, extract_json_field
PROMPT = "Batch.\\n\\n{input}\\n"
class BatchAgent(MemorySystem):
    def predict(self, input):
        outs = self._llm.batch([PROMPT.format(input=input), "x" * 230000 + PROMPT.format(input=input)])
        return extract_json_field(outs[0], "final_answer"), {}
    def learn_from_batch(self, batch_results): pass
    def get_state(self): return "{}"
    def set_state(self, state): pass
''',
    "opaque_agent.py": '''
from ..memory_system import MemorySystem, extract_json_field
class Opaque(str):
    def __str__(self):
        raise RuntimeError("str() refused")
class OpaqueAgent(MemorySystem):
    def predict(self, input):
        return Opaque(extract_json_field(self.call_llm(input), "final_answer")), {}
    def learn_from_batch(self, batch_results): pass
    def get_state(self): return "{}"
    def set_state(self, state): pass
''',
    "exit_agent.py": '''
from ..memory_system import MemorySystem
class ExitAgent(MemorySystem):
    def predict(self, input):
        raise SystemExit(7)
    def learn_from_batch(self, batch_results): pass
    def get_state(self): return "{}"
    def set_state(self, state): pass
''',
    "raise_agent.py": '''
from ..memory_system import MemorySystem
class RaiseAgent(MemorySystem):
    def predict(self, input):
        raise RuntimeError("synthetic failure")
    def learn_from_batch(self, batch_results): pass
    def get_state(self): return "{}"
    def set_state(self, state): pass
''',
}


class WorkerCase(unittest.TestCase):
    """Shared set-up: a temp tree, a copy of fake_pkg with the test agents, a loopback server."""

    rule = staticmethod(hint_rule)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mhf-cap-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.pkg_parent = self.tmp / "pkg"
        shutil.copytree(FAKE_PKG, self.pkg_parent, ignore=shutil.ignore_patterns("__pycache__"))
        for name, text in AGENTS.items():
            (self.pkg_parent / "text_classification" / "agents" / name).write_text(text)
        self.server = CompletionServer(self.rule)
        self.addCleanup(self.server.close)
        self.runs = 0

    def env(self, cache, pythonpath=None):
        home = self.tmp / "home"
        home.mkdir(exist_ok=True)
        return {"PATH": "/usr/bin:/bin", "HOME": str(home), "TMPDIR": str(self.tmp),
                "PYTHONPATH": str(pythonpath or self.pkg_parent), "PYTHONDONTWRITEBYTECODE": "1",
                "TEXT_CLASSIFICATION_LLM_CACHE_DIR": str(cache), "OPENROUTER_API_BASE": self.server.base,
                "OPENROUTER_API_KEY": "mhf-test-placeholder"}

    def job_args(self, out, *, dataset="USPTO", memory="agents/ok_agent.py", n_val=5, n_test=6, test=False,
                 load_memory=None):
        args = ["--memory", memory, "--dataset", dataset, "--seed", "42", "--model", MODEL, "--mode", "offline"]
        if test:
            args += ["--load-memory", str(load_memory), "--test-output", str(out / "test.json")]
        else:
            args += ["--val-output", str(out / "val.json"), "--save-memory", str(out / "memory.json"),
                     "--log", str(out / "log.jsonl")]
        return args + ["--num-train", "4", "--num-val", str(n_val), "--num-test", str(n_test),
                       "--temperature", "0.0"]

    def run_worker(self, args, *, wrapped=True, sidecar=None, cache=None, max_chars=None, python=None,
                   pythonpath=None, extra_env=None, timeout=300):
        self.runs += 1
        cwd = self.tmp / ("run%d" % self.runs)
        cwd.mkdir()
        cache = cache or (cwd / "cache")
        if wrapped:
            own = ["--sidecar", str(sidecar or (cwd / "sidecar.jsonl"))]
            if max_chars is not None:
                own += ["--max-text-chars", str(max_chars)]
            argv = [str(python or sys.executable), str(ENTRY), *own, "--", *args]
        else:
            argv = [str(python or sys.executable), "-m", "text_classification.inner_loop", *args]
        env = self.env(cache, pythonpath)
        env.update(extra_env or {})
        proc = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                              timeout=timeout)
        proc.cwd = cwd
        proc.sidecar = Path(sidecar or (cwd / "sidecar.jsonl"))
        return proc

    def val_job(self, **kw):
        out = self.tmp / ("out%d" % (self.runs + 1))
        opts = {k: kw.pop(k) for k in ("dataset", "memory", "n_val") if k in kw}
        proc = self.run_worker(self.job_args(out, **opts), **kw)
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        return proc, out, rows_of(proc.sidecar) if proc.sidecar.exists() else []

    @staticmethod
    def kinds(rows, kind):
        return [r for r in rows if r["kind"] == kind]


# --------------------------------------------------------------------------------------------------------------
class TestCaptureWithFakePkg(WorkerCase):

    def test_rows_for_two_validation_passes_keep_last_matches_result(self):
        proc, out, rows = self.val_job(n_val=5)
        self.assertEqual([r["seq"] for r in rows], list(range(1, len(rows) + 1)))
        meta, splits, done = rows[0], rows[1], rows[-1]
        self.assertEqual((meta["kind"], meta["schema"]), ("meta", "mhforge.sidecar/1"))
        self.assertEqual(meta["hooks"], {"generate_one": True, "batch": True, "loader": True})
        self.assertEqual(meta["argv"], self.job_args(out, n_val=5))
        self.assertIsInstance(meta["main_thread"], int)
        self.assertEqual(meta["python"], "%d.%d.%d" % sys.version_info[:3])
        self.assertEqual(splits["kind"], "splits")
        self.assertEqual((splits["dataset"], splits["sizes"], splits["wanted"]),
                         ("USPTO", {"train": 4, "val": 5, "test": 6}, ["val"]))
        self.assertEqual(done, {**done, "kind": "done", "exit": 0, "exception": None, "capture_errors": 0})
        evals = self.kinds(rows, "eval")
        self.assertEqual(len(evals), 10)
        self.assertTrue(all(e["split"] == "val" and not e["ambiguous"] for e in evals))
        self.assertEqual(sorted((e["index"], e["pass"]) for e in evals),
                         sorted((i, p) for i in range(5) for p in (1, 2)))
        last = {e["index"]: e for e in evals if e["pass"] == 2}
        result = json.loads((out / "val.json").read_text())
        self.assertEqual(sum(e["was_correct"] for e in last.values()), result["correct"])
        self.assertEqual(len(last), result["total"])
        self.assertGreater(result["correct"], 0)
        self.assertLess(result["correct"], 5)
        self.assertTrue(all("jaccard_similarity" in e["metrics"] for e in evals))

    def test_calls_join_to_examples_by_thread(self):
        proc, out, rows = self.val_job(dataset="Symptom2Disease", n_val=12)
        calls = {r["seq"]: r for r in self.kinds(rows, "call")}
        by_hash = {sha(text): text for text in self.server.requests}
        val = fake_data.load_dataset_splits_3way("Symptom2Disease", 4, 12, 6)[1]
        threads = set()
        for ev in self.kinds(rows, "eval"):
            self.assertEqual(len(ev["call_seqs"]), 1, ev)
            call = calls[ev["call_seqs"][0]]
            self.assertEqual(call["thread"], ev["thread"])
            self.assertLess(call["seq"], ev["seq"])
            threads.add(ev["thread"])
            text = by_hash[call["user_sha256"]]  # every hit's hash was first sent as a miss
            self.assertIn(val[ev["index"]]["raw_question"], text)
        self.assertGreater(len(threads), 1, "the passes ran on a thread pool")
        claimed = [s for ev in self.kinds(rows, "eval") for s in ev["call_seqs"]]
        self.assertEqual(sorted(claimed), sorted(calls))

    def test_cache_hit_flag(self):
        cache = self.tmp / "shared-cache"
        proc, out, rows = self.val_job(n_val=5, cache=cache)
        calls = {r["seq"]: r for r in self.kinds(rows, "call")}
        by_pass = {1: [], 2: []}
        for ev in self.kinds(rows, "eval"):
            by_pass[ev["pass"]].extend(calls[s] for s in ev["call_seqs"])
        self.assertTrue(by_pass[1] and all(c["cache_hit"] is False for c in by_pass[1]))
        self.assertTrue(by_pass[2] and all(c["cache_hit"] is True for c in by_pass[2]))
        self.assertEqual(len(self.server.requests), len(by_pass[1]))
        self.assertEqual({c["user_sha256"] for c in by_pass[2]}, {c["user_sha256"] for c in by_pass[1]})
        self.assertEqual({c["kwargs"]["temperature"] for c in calls.values()}, {0.0})
        self.assertTrue(all(c["error"] is None and c["system_sha256"] == sha("Reasoning: medium")
                            for c in calls.values()))
        before = len(self.server.requests)
        again = self.run_worker(self.job_args(out, n_val=5) + ["--force"], cache=cache)
        self.assertEqual(again.returncode, 0, again.stderr[-2000:])
        rerun_calls = self.kinds(rows_of(again.sidecar), "call")
        self.assertEqual(len(rerun_calls), 10)
        self.assertTrue(all(c["cache_hit"] is True for c in rerun_calls))
        self.assertEqual(len(self.server.requests), before)

    def test_batch_calls_recorded_with_hashes(self):
        proc, out, rows = self.val_job(memory="agents/batch_agent.py", n_val=3)
        calls = self.kinds(rows, "call")
        call_hashes = {c["user_sha256"] for c in calls}
        val = fake_data.load_dataset_splits_3way("USPTO", 4, 3, 6)[1]
        self.assertIn("Prompt truncated", proc.stderr)
        for ev in self.kinds(rows, "eval"):
            prompt = "Batch.\n\n%s\n" % val[ev["index"]]["input"]
            long_prompt = "x" * 230000 + prompt
            truncated = long_prompt[:112000] + "\n\n... [TRUNCATED] ...\n\n" + long_prompt[-112000:]
            self.assertEqual(ev["batch_user_sha256"], [sha(prompt), sha(truncated)])
            self.assertEqual(ev["call_seqs"], [], "batch runs _generate_one on pool threads")
            self.assertTrue(set(ev["batch_user_sha256"]) <= call_hashes)
            pool_threads = {c["thread"] for c in calls if c["user_sha256"] in ev["batch_user_sha256"]}
            self.assertNotIn(ev["thread"], pool_threads)
        self.assertEqual(rows[-1]["capture_errors"], 0)

    def test_wrapper_does_not_change_return_values_or_exceptions(self):
        plain_out, wrapped_out = self.tmp / "plain", self.tmp / "wrapped"
        plain = self.run_worker(self.job_args(plain_out, memory="agents/probe_agent.py", n_val=4), wrapped=False)
        n_plain = len(self.server.requests)
        wrapped = self.run_worker(self.job_args(wrapped_out, memory="agents/probe_agent.py", n_val=4))
        self.assertEqual((plain.returncode, wrapped.returncode), (0, 0), wrapped.stderr[-2000:])
        self.assertEqual(len(self.server.requests) - n_plain, n_plain, "the hooks add no model call")

        def probes(proc):
            return sorted((proc.cwd / "probe.jsonl").read_text().splitlines())

        self.assertEqual(probes(plain), probes(wrapped))
        self.assertTrue(all(json.loads(p)["err"].startswith("ProviderError: HTTP 400") for p in probes(wrapped)))

        def strip(path):
            data = json.loads(path.read_text())
            return {k: v for k, v in data.items() if k not in TIME_FIELDS}

        self.assertEqual(strip(plain_out / "val.json"), strip(wrapped_out / "val.json"))
        self.assertEqual((plain_out / "memory.json").read_text(), (wrapped_out / "memory.json").read_text())
        errors = [c for c in self.kinds(rows_of(wrapped.sidecar), "call") if c["error"]]
        self.assertEqual({c["error"] for c in errors}, {"ProviderError"})
        self.assertTrue(all(c["content_sha256"] is None for c in errors))

    def test_recording_error_is_swallowed_and_counted(self):
        proc, out, rows = self.val_job(memory="agents/opaque_agent.py", n_val=4)
        self.assertEqual(rows[-1]["kind"], "done")
        self.assertEqual(rows[-1]["exit"], 0)
        self.assertEqual(rows[-1]["capture_errors"], 8, "one per evaluator call of the two passes")
        self.assertEqual(self.kinds(rows, "eval"), [])
        self.assertEqual(json.loads((out / "val.json").read_text())["total"], 4)

    def test_system_exit_code_passes_through(self):
        bad = self.run_worker(["--memory", "agents/ok_agent.py"])  # argparse: --dataset is required
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(rows_of(bad.sidecar)[-1], {**rows_of(bad.sidecar)[-1], "exit": 2, "exception": None})
        agent_exit = self.run_worker(self.job_args(self.tmp / "o7", memory="agents/exit_agent.py", n_val=2))
        self.assertEqual(agent_exit.returncode, 7)
        self.assertEqual(rows_of(agent_exit.sidecar)[-1]["exit"], 7)
        out = self.tmp / "done-out"
        out.mkdir()
        (out / "val.json").write_text("{}")
        skip = self.run_worker(self.job_args(out, n_val=2))
        self.assertEqual(skip.returncode, 0)
        self.assertIn("Already complete", skip.stdout)
        self.assertEqual([r["kind"] for r in rows_of(skip.sidecar)], ["meta", "done"])
        self.assertEqual(rows_of(skip.sidecar)[-1]["exit"], 0)

    def test_exception_passes_through_after_done_row(self):
        proc = self.run_worker(self.job_args(self.tmp / "oerr", memory="agents/raise_agent.py", n_val=2))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("RuntimeError: synthetic failure", proc.stderr)
        self.assertIn("mhf_capture_entry.py", proc.stderr)  # V15: the wrapper frames stay visible
        done = rows_of(proc.sidecar)[-1]
        self.assertEqual((done["kind"], done["exit"], done["exception"]), ("done", 1, "RuntimeError"))

    def test_unwritable_sidecar_does_not_fail_the_job(self):
        missing = self.tmp / "no-such-dir" / "sidecar.jsonl"
        out = self.tmp / "out-unwritable"
        proc = self.run_worker(self.job_args(out, n_val=4), sidecar=missing)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertFalse(missing.exists())
        self.assertEqual(len([ln for ln in proc.stderr.splitlines() if "mhf_capture_entry" in ln]), 1)
        plain = self.run_worker(self.job_args(self.tmp / "out-plain", n_val=4), wrapped=False)
        self.assertEqual(json.loads((out / "val.json").read_text())["correct"],
                         json.loads((self.tmp / "out-plain" / "val.json").read_text())["correct"])

    def test_duplicate_example_keys_marked_ambiguous(self):
        proc, out, rows = self.val_job(dataset="DupKeys", n_val=4)
        self.assertEqual(self.kinds(rows, "splits")[0]["duplicate_keys"], 1)
        evals = self.kinds(rows, "eval")
        val =fake_data.load_dataset_splits_3way("DupKeys", 4, 4, 6)[1]
        key = ce.example_key(val[0]["raw_question"], val[0]["target"])
        dups = [e for e in evals if e["example_sha256"] == key]
        self.assertEqual(len(dups), 4)
        self.assertTrue(all(e["ambiguous"] for e in dups))
        self.assertEqual(sorted((e["index"], e["pass"]) for e in dups), [(0, 1), (0, 2), (2, 1), (2, 2)])
        others = [e for e in evals if e["example_sha256"] != key]
        self.assertEqual(sorted(e["index"] for e in others), [1, 1, 3, 3])
        self.assertFalse(any(e["ambiguous"] for e in others))

    def test_wanted_split_wins_for_shared_question(self):
        _, out, val_rows = self.val_job(dataset="Symptom2Disease", n_val=3)
        shared = fake_data.make_example("Symptom2Disease", "val", 0)
        key = ce.example_key(shared["raw_question"], shared["target"])
        self.assertEqual(self.kinds(val_rows, "splits")[0]["duplicate_keys"], 1)
        hits = [e for e in self.kinds(val_rows, "eval") if e["example_sha256"] == key]
        self.assertEqual([(e["split"], e["index"], e["ambiguous"]) for e in hits], [("val", 0, False)] * 2)
        test = self.run_worker(self.job_args(self.tmp / "otest", dataset="Symptom2Disease", n_val=3, test=True,
                                             load_memory=out / "memory.json"))
        self.assertEqual(test.returncode, 0, test.stderr[-2000:])
        rows = rows_of(test.sidecar)
        self.assertEqual(self.kinds(rows, "splits")[0]["wanted"], ["test"])
        hits = [e for e in self.kinds(rows, "eval") if e["example_sha256"] == key]
        self.assertEqual([(e["split"], e["index"], e["pass"], e["ambiguous"]) for e in hits],
                         [("test", 1, 1, False)])
        self.assertEqual(sorted(e["index"] for e in self.kinds(rows, "eval")), list(range(6)))

    def test_sidecar_holds_no_prompt_text(self):
        proc, out, rows = self.val_job(memory="agents/probe_agent.py", n_val=4)
        text = proc.sidecar.read_text()
        self.assertTrue(self.server.requests)
        for prompt in self.server.requests:
            for line in prompt.splitlines():
                if len(line) >= 16 and not line.startswith("hint: "):
                    self.assertNotIn(line, text)
        self.assertNotIn(REASONING_MARKER, text)
        self.assertNotIn("SYNTHETIC USPTO", text)
        self.assertNotIn("Probe.", text)
        self.assertNotIn("Reasoning: medium", text)

    def test_missing_hook_target_sets_flag_false(self):
        with open(self.pkg_parent / "text_classification" / "llm.py", "a") as f:
            f.write("\ndel LLM.batch\n")
        proc, out, rows = self.val_job(n_val=3)
        self.assertEqual(rows[0]["hooks"], {"generate_one": True, "batch": False, "loader": True})
        self.assertEqual(json.loads((out / "val.json").read_text())["total"], 3)
        self.assertEqual(len(self.kinds(rows, "eval")), 6)

    def test_eval_rows_carry_question_prediction_and_target_hashes(self):
        proc, out, rows = self.val_job(n_val=4, max_chars=5)
        val = fake_data.load_dataset_splits_3way("USPTO", 4, 4, 6)[1]
        for ev in self.kinds(rows, "eval"):
            ex = val[ev["index"]]
            hint = re.findall(r"hint: ([^\n]*)", ex["raw_question"])[-1]
            self.assertEqual(ev["question_sha256"], sha(ex["raw_question"]))
            self.assertEqual(ev["example_sha256"], sha(ex["raw_question"] + "\0" + ex["target"]))
            self.assertEqual((ev["prediction"], ev["prediction_chars"], ev["prediction_sha256"]),
                             (hint[:5], len(hint), sha(hint)))
            self.assertEqual((ev["target"], ev["target_sha256"]), (ex["target"][:5], sha(ex["target"])))
            self.assertLess(len(ev["target"]), len(ex["target"]))


# --------------------------------------------------------------------------------------------------------------
class _Writer:
    def __init__(self):
        self.lines = []

    def write(self, data):
        self.lines.append(json.loads(data.decode("ascii")))


class TestCaptureInProcess(unittest.TestCase):
    """Identity checks of the wrappers themselves, with an injected clock."""

    def setUp(self):
        self.writer = _Writer()
        self.cap = ce.Capture(self.writer, wanted=["val"], max_text_chars=100, clock=lambda: 42.0)

    def test_wrappers_return_and_raise_the_same_objects(self):
        sentinel, boom = object(), ValueError("boom")

        class Provider:
            def _cache_path(self, prompt, system_prompt, kwargs):
                return prompt

            def _load_cache(self, path):
                return {} if path == "hit" else None

            def _generate_one(self, prompt, system_prompt, kwargs):
                if prompt == "fail":
                    raise boom
                return sentinel

        Provider._generate_one = self.cap.wrap_generate_one(Provider._generate_one)
        self.assertIs(Provider()._generate_one("hit", None, {"temperature": 0.0, "messages": ["x"]}), sentinel)
        with self.assertRaises(ValueError) as ctx:
            Provider()._generate_one("fail", "sys", {})
        self.assertIs(ctx.exception, boom)
        ok_row, err_row = self.writer.lines
        self.assertEqual((ok_row["cache_hit"], ok_row["t_start"], ok_row["t_end"]), (True, 42.0, 42.0))
        self.assertEqual(ok_row["kwargs"], {"temperature": 0.0, "messages": "<list>"})
        self.assertEqual((err_row["cache_hit"], err_row["error"], err_row["content_sha256"]),
                         (False, "ValueError", None))

        received = []

        class Llm:
            def _truncate(self, p):
                return p

            def batch(self, prompts):
                received.append(prompts)
                return sentinel

        Llm.batch = self.cap.wrap_batch(Llm.batch)
        gen = (p for p in ["a", "b"])
        self.assertIs(Llm().batch(gen), sentinel)
        self.assertIs(received[0], gen)
        self.assertEqual(list(gen), ["a", "b"], "an iterator is passed on unconsumed")
        self.assertIs(Llm().batch(["a"]), sentinel)

        train, val, test = [], [{"raw_question": "q", "target": "t"}], []
        result_dict = {"was_correct": True, "metrics": {"m": 1.0}}

        def evaluator(pred, target, **kw):
            if pred == "raise":
                raise boom
            return result_dict

        loader = self.cap.wrap_loader(lambda *a, **k: (train, val, test, evaluator), True)
        got = loader("USPTO")
        self.assertIs(got[0], train)
        self.assertIs(got[1], val)
        self.assertIs(got[2], test)
        self.assertIs(got[3]("p", "t", raw_question="q"), result_dict)
        with self.assertRaises(ValueError) as ctx:
            got[3]("raise", "t", raw_question="q")
        self.assertIs(ctx.exception, boom)
        ev = [r for r in self.writer.lines if r["kind"] == "eval"]
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["split"], ev[0]["index"], ev[0]["batch_user_sha256"]), ("val", 0, [sha("a")]))
        self.assertEqual(self.cap.capture_errors, 0)

    def test_recording_error_in_process_is_counted_not_raised(self):
        odd = {"no_was_correct_key": 1}
        loader = self.cap.wrap_loader(lambda *a, **k: ([], [], [], lambda p, t, **kw: odd), True)
        self.assertIs(loader("X")[3]("p", "t", raw_question="q"), odd)
        self.assertEqual(self.cap.capture_errors, 1)
        weird = ("not", "a", "loader", "result")
        self.assertIs(self.cap.wrap_loader(lambda: weird, False)(), weird)

    def test_str_subclass_prediction_cannot_pass_the_text_limit(self):
        long_text = "DATASET-TEXT-" * 50

        class Liar(str):
            def __getitem__(self, key):
                return long_text

            def __len__(self):
                return 3

        class Pred:
            def __str__(self):
                return Liar(long_text)

        loader = self.cap.wrap_loader(lambda *a, **k: ([], [{"raw_question": "q", "target": "t"}], [],
                                                        lambda p, t, **kw: True), True)
        self.cap._max = 5
        self.assertIs(loader("X")[3](Pred(), "t", raw_question="q"), True)
        ev = [r for r in self.writer.lines if r["kind"] == "eval"][0]
        self.assertEqual((ev["prediction"], ev["prediction_chars"]), (long_text[:5], len(long_text)))
        self.assertEqual(ev["prediction_sha256"], sha(long_text))

    def test_parse_args_and_wanted_splits(self):
        self.assertEqual(ce.parse_args(["--sidecar", "s", "--", "--dataset", "X"]),
                         ("s", ce.DEFAULT_MAX_TEXT_CHARS, ["--dataset", "X"]))
        self.assertEqual(ce.parse_args(["--sidecar=s", "--max-text-chars", "7", "--"]), ("s", 7, []))
        for bad in (["--sidecar", "s"], ["--", "x"], ["--sidecar", "s", "--max-text-chars", "-1", "--"],
                    ["--bogus", "--"]):
            with self.assertRaises(SystemExit) as ctx:
                ce.parse_args(bad)
            self.assertEqual(ctx.exception.code, 2)
        self.assertEqual(ce.wanted_splits(["--val-output", "v", "--test-output=t"]), ["val", "test"])
        self.assertEqual(ce.wanted_splits(["--load-memory", "m", "--test-output", "t"]), ["test"])
        self.assertEqual([ce.exit_code(c) for c in (None, 0, 3, "msg", True)], [0, 0, 3, 1, 1])

    def test_capture_source_imports_only_the_standard_library(self):
        source = ENTRY.read_text()
        imported = set(re.findall(r"^(?:import|from) ([a-zA-Z_][\w]*)", source, re.M))
        self.assertLessEqual(imported, set(sys.stdlib_module_names))
        self.assertIsNone(re.search(r"\benviron\b|getenv|putenv|(?:import|from)\s+mhforge", source),
                          "no environment access and no mhforge import")


# --------------------------------------------------------------------------------------------------------------
def _uspto_rule_factory():
    targets = {}
    for split in ("train", "val", "test"):
        for line in (EXAMPLE_DIR / "data" / "uspto" / ("%s.jsonl" % split)).read_text().splitlines():
            row = json.loads(line)
            targets[row["question"]] = row["target"]

    def rule(user):
        for question, target in targets.items():
            if question in user:
                answer = target if sha(question)[0] in "01234567" else "C"
                return 200, json.dumps({"reasoning": "r", "final_answer": answer})
        return 200, json.dumps({"reasoning": "r", "final_answer": "C"})

    return rule


class TestCaptureRealWorker(WorkerCase):
    """Upstream's real worker (USPTO, no_memory) through the example venv; Route 2 to the loopback server."""

    @classmethod
    def setUpClass(cls):
        cls.rule = staticmethod(_uspto_rule_factory()) if (EXAMPLE_DIR / "data" / "uspto").exists() else hint_rule

    def real_env(self):
        return {"LITELLM_LOCAL_MODEL_COST_MAP": "True", "TIKTOKEN_RS_CACHE_DIR": str(_tokenizer_dir())}

    def real_args(self, out, *, test=False, memory_file=None):
        args = ["--memory", "agents/no_memory.py", "--dataset", "USPTO", "--seed", "42", "--model", MODEL,
                "--mode", "offline"]
        if test:
            args += ["--load-memory", str(memory_file), "--test-output", str(out / "test.json")]
        else:
            args += ["--val-output", str(out / "val.json"), "--save-memory", str(out / "memory.json"),
                     "--log", str(out / "log.jsonl")]
        return args + ["--num-train", "50", "--num-val", "30", "--num-test", "100", "--temperature", "0.0"]

    def run_real(self, args, **kw):
        proc = self.run_worker(args, python=VENV_PY, pythonpath=EXAMPLE_DIR.parent, extra_env=self.real_env(), **kw)
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        return proc

    @needs_example_venv
    def test_real_worker_val_job_rows_equal_val_json(self):
        out = self.tmp / "real-val"
        proc = self.run_real(self.real_args(out))
        rows = rows_of(proc.sidecar)
        self.assertEqual(rows[0]["hooks"], {"generate_one": True, "batch": True, "loader": True})
        self.assertEqual(rows[1]["sizes"], {"train": 50, "val": 30, "test": 100})
        self.assertEqual((rows[-1]["kind"], rows[-1]["exit"], rows[-1]["capture_errors"]), ("done", 0, 0))
        evals = self.kinds(rows, "eval")
        self.assertEqual(sorted((e["split"], e["index"], e["pass"]) for e in evals),
                         sorted(("val", i, p) for i in range(30) for p in (1, 2)))
        result = json.loads((out / "val.json").read_text())
        last = [e for e in evals if e["pass"] == 2]
        self.assertEqual(sum(e["was_correct"] for e in last), result["correct"])
        self.assertEqual(result["total"], 30)
        calls = self.kinds(rows, "call")
        misses = [c for c in calls if c["cache_hit"] is False]
        self.assertEqual((len(calls), len(misses), len(self.server.requests)), (60, 30, 30))
        self.assertEqual({c["user_sha256"] for c in misses}, {sha(t) for t in self.server.requests})
        self.assertEqual(misses[0]["kwargs"], {"temperature": 0.0, "max_tokens": 16384})

    @needs_example_venv
    def test_real_worker_test_job_load_memory(self):
        memory_file = self.tmp / "memory.json"
        memory_file.write_text("{}")
        out = self.tmp / "real-test"
        proc = self.run_real(self.real_args(out, test=True, memory_file=memory_file))
        rows = rows_of(proc.sidecar)
        self.assertEqual(rows[1]["wanted"], ["test"])
        evals = self.kinds(rows, "eval")
        self.assertEqual(sorted((e["split"], e["index"], e["pass"]) for e in evals),
                         [("test", i, 1) for i in range(100)])
        result = json.loads((out / "test.json").read_text())
        self.assertEqual(sum(e["was_correct"] for e in evals), result["correct"])
        self.assertEqual(len(self.server.requests), 100)

    @needs_example_venv
    def test_capture_does_not_change_results(self):
        plain_out, wrapped_out = self.tmp / "plain", self.tmp / "wrapped"
        self.run_real(self.real_args(plain_out), wrapped=False)
        n_plain = len(self.server.requests)
        self.run_real(self.real_args(wrapped_out))
        self.assertEqual(len(self.server.requests) - n_plain, n_plain)

        def strip(path):
            return {k: v for k, v in json.loads(path.read_text()).items() if k not in TIME_FIELDS}

        self.assertEqual(strip(plain_out / "val.json"), strip(wrapped_out / "val.json"))
        self.assertEqual((plain_out / "memory.json").read_text(), (wrapped_out / "memory.json").read_text())


if __name__ == "__main__":
    unittest.main()
