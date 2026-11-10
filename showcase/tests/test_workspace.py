# SPDX-License-Identifier: MIT
"""Tests for mhforge.workspace (spec 3.14, 5.2)."""

from __future__ import annotations

import importlib.util
import json
import os
import py_compile
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from mhforge import util, workspace
from mhforge.errors import MhforgeError, UpstreamContractError, WorkspaceError
from mhforge.workspace import PINNED_FILES
from tests import helpers
from tests.helpers import make_manifest, needs_example_dir, needs_yaml, tmp_state

FAKE_CLAUDE_WRAPPER = '''\
import subprocess
from pathlib import Path
_EMPTY_PLUGIN_DIR = Path(__file__).parent / ".empty_plugins"
def build_command(*args, **kwargs): return []
def run(*args, **kwargs): return None
'''
FAKE_BENCHMARK = '''\
from pathlib import Path
_CONFIG = {"datasets": ["USPTO"], "models": [{"model": "m/x"}]}
DATASETS = _CONFIG["datasets"]
MODELS = _CONFIG["models"]
SEEDS = [42]
def build_val_runs(*a, **k): return []
def build_test_runs(*a, **k): return []
def _inner_loop_command(): return []
def run_dir(*a, **k): return Path(".")
def update_summary(logs_dir): return None
def load_results(base_dir, filename="val.json"): return {}
def discover_all_memory_systems(): return []
def get_model_short_name(model_id): return model_id.split("/")[-1].lower()
'''
FAKE_META_HARNESS = '''\
from pathlib import Path
import claude_wrapper
from benchmark import get_model_short_name, load_results
EVOLVE_DIR = Path(__file__).parent
LOGS_DIR = EVOLVE_DIR / "logs"
PENDING_EVAL = LOGS_DIR / "pending_eval.json"
FRONTIER_VAL = LOGS_DIR / "frontier_val.json"
RESULTS_DIR = LOGS_DIR / "results"
PROPOSER_ALLOWED_TOOLS = ["Read"]
def run_benchmark(args): return None
def propose_claude(task_prompt, iteration, timeout=2400): return False
def validate_candidates(candidates): return []
def update_evolution_summary(*args, **kwargs): return None
def run_evolve(args): return None
def finalize_run(baselines, datasets, model_short): return None
def count_iterations_from_summary(): return 0
'''
FAKES = {"claude_wrapper.py": FAKE_CLAUDE_WRAPPER, "benchmark.py": FAKE_BENCHMARK, "meta_harness.py": FAKE_META_HARNESS}


def make_example(root: Path, overrides: dict[str, str] | None = None) -> Path:
    """A synthetic example directory holding every pinned file (the three driver modules as stdlib-only fakes)."""
    root = Path(root)
    for rel in PINNED_FILES:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text((overrides or {}).get(rel, FAKES.get(rel, f"# synthetic {rel}\n")))
    return root


def manifest_for(example: Path) -> dict:
    return make_manifest(**{"upstream.tree_sha256": workspace.source_digest(example)[0]})


class WorkspaceCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mhf-ws-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()
        ctx = tmp_state()
        self.st = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.paths = self.st.paths

    def synthetic_ws(self, overrides: dict[str, str] | None = None) -> Path:
        example = make_example(self.tmp / "example", overrides)
        return workspace.ensure_workspace(manifest_for(example), self.paths, example, None)


class TestCandidates(WorkspaceCase):
    def test_candidate_name_and_file_rejections(self) -> None:
        ws = self.synthetic_ws()
        agents = ws / "agents"
        (agents / "good_one.py").write_text("x = 1\n")
        self.assertIsNone(workspace.validate_candidate_file(self.paths, "good_one"))
        self.assertEqual(workspace.candidate_sha256(self.paths, "good_one"), util.sha256_bytes(b"x = 1\n"))
        for bad in ("x\n", "Good", "1abc", "a-b", "x; import os", "__import__('os').system('id')", "a" * 49, "", None, 7):
            self.assertEqual(workspace.validate_candidate_file(self.paths, bad), "invalid_name", repr(bad))
            self.assertIsNone(workspace.candidate_sha256(self.paths, bad))
        self.assertEqual(workspace.validate_candidate_file(self.paths, "a" * 48), "missing")   # longest valid name
        for pinned in workspace.PINNED_AGENTS[1:]:
            self.assertEqual(workspace.validate_candidate_file(self.paths, pinned), "pinned_name")
        self.assertEqual(workspace.validate_candidate_file(self.paths, "absent"), "missing")
        secret = self.tmp / "outside.py"
        secret.write_text("SECRET = 1\n")
        os.symlink(secret, agents / "linked.py")
        self.assertEqual(workspace.validate_candidate_file(self.paths, "linked"), "symlink")
        self.assertIsNone(workspace.candidate_sha256(self.paths, "linked"))
        (agents / "dir_one.py").mkdir()
        self.assertEqual(workspace.validate_candidate_file(self.paths, "dir_one"), "not_a_regular_file")
        (agents / "huge.py").write_bytes(b"#" * (workspace.MAX_AGENT_FILE_BYTES + 1))
        self.assertEqual(workspace.validate_candidate_file(self.paths, "huge"), "too_large")
        (agents / "edge.py").write_bytes(b"#" * workspace.MAX_AGENT_FILE_BYTES)
        self.assertIsNone(workspace.validate_candidate_file(self.paths, "edge"))
        for i in range(workspace.MAX_AGENT_FILES):
            (agents / f"filler_{i}.py").write_text("")
        self.assertEqual(workspace.validate_candidate_file(self.paths, "good_one"), "too_many_agent_files")

    def test_pycache_in_agents_does_not_reject_candidates(self) -> None:
        ws = self.synthetic_ws()
        before = workspace.snapshot(self.paths)
        agents = ws / "agents"
        (agents / "cand_a.py").write_text("A = 1\n")
        (agents / "__pycache__").mkdir()
        (agents / "__pycache__" / "cand_a.cpython-313.pyc").write_bytes(b"\x00pyc")
        (agents / "notes.txt").write_text("n\n")
        (agents / "sub").mkdir()
        (agents / "sub" / "helper.py").write_text("")
        self.assertIsNone(workspace.validate_candidate_file(self.paths, "cand_a"))
        drift = workspace.classify_drift(before, workspace.snapshot(self.paths))
        self.assertEqual(drift["agents_added"], ["agents/cand_a.py"])
        self.assertEqual(drift["other_added"], ["agents/notes.txt", "agents/sub/helper.py"])
        self.assertFalse(any("__pycache__" in p for v in drift.values() for p in v))
        self.assertEqual(workspace.unknown_entries(self.paths), [])        # agents/ is the proposer's to write

    def test_pending_eval_shape_check(self) -> None:
        path = self.tmp / "pending_eval.json"

        def write(doc: object) -> None:
            path.write_text(doc if isinstance(doc, str) else json.dumps(doc))

        good = {"candidates": [{"name": "a", "hypothesis": "h", "axis": "x", "base_system": "fewshot_all",
                                "components": ["c1"], "extra": 1}, {"name": "b"}], "note": "kept"}
        write(good)
        self.assertEqual([c["name"] for c in workspace.read_pending_eval(path)], ["a", "b"])
        write({"candidates": []})
        self.assertEqual(workspace.read_pending_eval(path), [])
        bad_docs = [
            {"candidates": [{"name": "a", "hypothesis": None}]},
            {"candidates": [{"name": "a", "hypothesis": 3}]},
            {"candidates": [{"name": "a", "hypothesis": {"x": 1}}]},
            {"candidates": [{"name": "a"}, {"name": "a"}]},
            {"candidates": [{"name": 1}]},
            {"candidates": [{"hypothesis": "h"}]},
            {"candidates": [{"name": "a", "axis": ["x"]}]},
            {"candidates": [{"name": "a", "base_system": None}]},
            {"candidates": [{"name": "a", "components": "c"}]},
            {"candidates": [{"name": "a", "components": [1]}]},
            {"candidates": ["a"]},
            {"candidates": {"name": "a"}},
            {"name": "a"},
            [{"name": "a"}],
            '{"candidates": [{"name": "a", "name": "b"}]}',
            '{"candidates": [{"name": "a", "hypothesis": NaN}]}',
            '{"candidates": [], "x": ' + "[" * 30000 + "]" * 30000 + "}",     # nesting deep enough for RecursionError
            "not json",
        ]
        for doc in bad_docs:
            write(doc)
            with self.assertRaises(WorkspaceError, msg=repr(doc)):
                workspace.read_pending_eval(path)
        write({"candidates": [{"name": "a", "hypothesis": "h" * 1000}]})
        with self.assertRaises(WorkspaceError):
            workspace.read_pending_eval(path, max_bytes=100)
        target = self.tmp / "elsewhere.json"
        target.write_text(json.dumps(good))
        path.unlink()
        os.symlink(target, path)
        with self.assertRaises(WorkspaceError):
            workspace.read_pending_eval(path)
        with self.assertRaises(WorkspaceError):
            workspace.read_pending_eval(self.tmp / "absent.json")


class TestDrift(WorkspaceCase):
    def test_unknown_entries_and_quarantine(self) -> None:
        ws = self.synthetic_ws()
        self.assertEqual(workspace.unknown_entries(self.paths), [])
        # expected entries
        (ws / "agents" / "cand.py").write_text("")
        (ws / "logs" / "mh-test").mkdir(parents=True)
        (ws / "logs" / "mh-test" / "stray.json").write_text("{}")
        (ws / ".empty_plugins").mkdir()
        (ws / "uv.lock").write_text("lock")
        (ws / "__pycache__").mkdir()
        (ws / "data" / "__pycache__").mkdir()
        self.assertEqual(workspace.unknown_entries(self.paths), [])
        # what a session may leave behind
        (ws / "yaml.py").write_text("raise SystemExit('planted')\n")
        (ws / "scratch").mkdir()
        (ws / "scratch" / "notes.md").write_text("n")
        (ws / "data" / "extra.py").write_text("")
        (ws / "tests" / "conftest.py").write_text("")
        (ws / ".venv").mkdir()
        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        os.symlink(outside, ws / "link_out")
        os.symlink(outside, ws / ".claude" / "skills" / "other")
        expected = sorted([".claude/skills/other", ".venv", "data/extra.py", "link_out", "scratch", "tests/conftest.py",
                           "yaml.py"])
        self.assertEqual(workspace.unknown_entries(self.paths), expected)
        moved = workspace.quarantine_entries(self.paths, expected, "2026-11-10T01:02:03.000001Z")
        self.assertEqual(moved, expected)
        self.assertEqual(workspace.unknown_entries(self.paths), [])
        qroot = self.paths.quarantine_root / "2026-11-10T01:02:03.000001Z" / "workspace"
        self.assertTrue((qroot / "scratch" / "notes.md").is_file())
        self.assertTrue((qroot / "link_out").is_symlink())          # moved as a link, never followed
        self.assertEqual((outside / "keep.txt").read_text(), "keep")
        self.assertTrue((qroot / "data" / "extra.py").is_file())
        self.assertEqual(workspace.verify_pinned(self.paths), {"modified": [], "removed": []})
        self.assertTrue((ws / "agents" / "cand.py").exists())
        # absent entries are skipped; protected entries are refused before anything moves
        (ws / "stray2").write_text("")
        self.assertEqual(workspace.quarantine_entries(self.paths, ["gone.py"], "s1"), [])
        for protected in ("agents/cand.py", "llm.py", "data", "logs", "agents", "../x", "/etc/passwd", "a//b"):
            with self.assertRaises(WorkspaceError, msg=protected):
                workspace.quarantine_entries(self.paths, ["stray2", protected], "s2")
        self.assertTrue((ws / "stray2").exists())
        self.assertEqual(workspace.quarantine_entries(self.paths, ["logs/mh-test/stray.json"], "s3"),
                         ["logs/mh-test/stray.json"])
        with self.assertRaises(WorkspaceError):
            workspace.quarantine_entries(self.paths, ["stray2"], "../up")
        # a pinned path replaced by a directory is unknown; once moved, restore puts the file back
        (ws / "llm.py").unlink()
        (ws / "llm.py").mkdir()
        self.assertEqual(workspace.unknown_entries(self.paths), ["llm.py", "stray2"])
        workspace.quarantine_entries(self.paths, ["llm.py"], "s4")
        changed = workspace.verify_pinned(self.paths)
        self.assertEqual(changed["removed"], ["llm.py"])
        self.assertEqual(workspace.restore_pinned(self.paths, self.tmp / "example", changed), ["llm.py"])

    def test_drift_classification(self) -> None:
        ws = self.synthetic_ws()
        (ws / "agents" / "old_cand.py").write_text("v1\n")
        (ws / "agents" / "removed_cand.py").write_text("r\n")
        before = workspace.snapshot(self.paths)
        (ws / "agents" / "new_cand.py").write_text("n\n")
        (ws / "agents" / "old_cand.py").write_text("v2\n")
        (ws / "agents" / "removed_cand.py").unlink()
        (ws / "agents" / "fewshot_all.py").write_text("edited\n")
        (ws / "llm.py").write_text("edited\n")
        (ws / "data" / "api.py").unlink()
        (ws / "agents" / "README").write_text("r")
        (ws / "agents" / "__pycache__").mkdir()
        (ws / "agents" / "__pycache__" / "new_cand.cpython-313.pyc").write_bytes(b"x")
        (ws / "logs").mkdir()
        (ws / "logs" / "anything.json").write_text("{}")
        (ws / "stray.py").write_text("")
        after = workspace.snapshot(self.paths)
        self.assertNotIn("logs/anything.json", after)
        drift = workspace.classify_drift(before, after)
        self.assertEqual(drift, {"agents_added": ["agents/new_cand.py"], "agents_modified": ["agents/old_cand.py"],
                                 "upstream_modified": ["agents/fewshot_all.py", "llm.py"],
                                 "upstream_removed": ["data/api.py"], "other_added": ["agents/README"]})
        self.assertEqual(workspace.classify_drift(after, after),
                         {k: [] for k in ("agents_added", "agents_modified", "upstream_modified", "upstream_removed",
                                          "other_added")})

    def test_venv_root_is_the_procenv_rule(self) -> None:
        from mhforge import procenv
        venv = self.tmp / "venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").write_text("")
        os.symlink(self.tmp / "nowhere" / "python3.13", venv / "bin" / "python3")
        bare = self.tmp / "bare"
        (bare / "bin").mkdir(parents=True)
        (bare / "bin" / "python").write_text("")
        (venv / "pyvenv.cfg").write_text("home = /x\n")
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.tmp)
        cases = [venv / "bin" / "python", venv / "bin" / "python3", str(venv / "bin" / "python"),
                 Path("venv/bin/python"), "venv/bin/../bin/python", venv / "bin" / "missing",
                 bare / "bin" / "python", Path("bare/bin/python"), self.tmp / "python"]
        for case in cases:
            with self.subTest(case=str(case)):
                try:
                    want = procenv.venv_root(case)
                except WorkspaceError:
                    with self.assertRaises(WorkspaceError):
                        workspace._venv_root(case)
                else:
                    self.assertEqual(workspace._venv_root(case), want)

    def test_env_and_memory_fingerprints_change_detected(self) -> None:
        venv = self.tmp / "venv"
        site = venv / "lib" / "python3.13" / "site-packages"
        (site / "alpha-1.0.dist-info").mkdir(parents=True)
        (venv / "bin").mkdir()
        (venv / "bin" / "python").write_text("")
        python = venv / "bin" / "python"
        with self.assertRaises(WorkspaceError):
            workspace.env_fingerprint(python)                       # no pyvenv.cfg
        (venv / "pyvenv.cfg").write_text("home = /x\n")
        first = workspace.env_fingerprint(python)
        self.assertRegex(first, r"[0-9a-f]{64}")
        (site / "alpha-1.0.dist-info" / "RECORD").write_text("changed contents only")
        (site / "alpha").mkdir()
        self.assertEqual(workspace.env_fingerprint(python), first)   # names of *.dist-info only
        (site / "beta-2.0.dist-info").mkdir()
        second = workspace.env_fingerprint(python)
        self.assertNotEqual(second, first)
        os.rename(site / "beta-2.0.dist-info", site / "beta-2.1.dist-info")
        self.assertNotEqual(workspace.env_fingerprint(python), second)
        # lexical venv root: a symlinked interpreter is not resolved to its target's directory
        link = venv / "bin" / "python3"
        os.symlink(self.tmp / "nowhere" / "python3.13", link)
        self.assertEqual(workspace.env_fingerprint(link), workspace.env_fingerprint(python))

        home = self.tmp / "home"
        cwd = Path("/Users/example/state/mh test/workspace/text_classification")
        self.assertIsNone(workspace.memory_dir_fingerprint(home, cwd))
        slug = "-Users-example-state-mh-test-workspace-text-classification"
        mem = home / ".claude" / "projects" / slug / "memory"
        mem.mkdir(parents=True)
        (home / ".claude" / "projects" / "-other-project" / "memory").mkdir(parents=True)
        empty = workspace.memory_dir_fingerprint(home, cwd)
        self.assertIsNotNone(empty)
        (mem / "MEMORY.md").write_text("abc")
        one = workspace.memory_dir_fingerprint(home, cwd)
        self.assertNotEqual(one, empty)
        (mem / "MEMORY.md").write_text("xyz")                       # same name and size: contents are never read
        self.assertEqual(workspace.memory_dir_fingerprint(home, cwd), one)
        (mem / "MEMORY.md").write_text("abcd")
        self.assertNotEqual(workspace.memory_dir_fingerprint(home, cwd), one)
        (home / ".claude" / "projects" / "-other-project" / "memory" / "x.md").write_text("x")
        before_other = workspace.memory_dir_fingerprint(home, cwd)
        (home / ".claude" / "projects" / "-other-project" / "memory" / "y.md").write_text("y")
        self.assertEqual(workspace.memory_dir_fingerprint(home, cwd), before_other)   # another project's memory


class TestUpstreamContract(WorkspaceCase):
    def test_contract_fails_on_missing_attribute(self) -> None:
        self.synthetic_ws()
        before_path = list(sys.path)
        with workspace.load_upstream(self.paths) as up:              # the fakes satisfy the contract
            self.assertIs(sys.modules["meta_harness"], up.meta_harness)
            self.assertIs(up.meta_harness.claude_wrapper, up.claude_wrapper)
            self.assertEqual(up.meta_harness.EVOLVE_DIR, self.paths.ws)
        self.assertEqual(sys.path, before_path)
        cases = {
            "meta_harness.py": [("FRONTIER_VAL = LOGS_DIR / \"frontier_val.json\"\n", "", "meta_harness.FRONTIER_VAL"),
                                ("def propose_claude(task_prompt, iteration, timeout=2400)",
                                 "def propose_claude(task_prompt, iteration)", "meta_harness.propose_claude"),
                                ("def run_evolve(args): return None\n", "", "meta_harness.run_evolve"),
                                ("LOGS_DIR = EVOLVE_DIR / \"logs\"", "LOGS_DIR = Path(\"/tmp/elsewhere\")",
                                 "meta_harness.LOGS_DIR is outside")],
            "benchmark.py": [("SEEDS = [42]", "SEEDS = [42, 123]", "benchmark.SEEDS"),
                             ("def run_dir(*a, **k): return Path(\".\")\n", "", "benchmark.run_dir")],
            "claude_wrapper.py": [("import subprocess\n", "subprocess = None\n", "claude_wrapper.subprocess"),
                                  ("_EMPTY_PLUGIN_DIR = ", "_OTHER = ", "claude_wrapper._EMPTY_PLUGIN_DIR")],
        }
        for rel, edits in cases.items():
            for old, new, needle in edits:
                with self.subTest(rel=rel, needle=needle):
                    shutil.rmtree(self.paths.workspace_root)
                    shutil.rmtree(self.tmp / "example")
                    self.assertIn(old, FAKES[rel])
                    self.synthetic_ws({rel: FAKES[rel].replace(old, new)})
                    with self.assertRaises(UpstreamContractError) as cm:
                        with workspace.load_upstream(self.paths):
                            self.fail("contract should not hold")
                    self.assertIn(needle, str(cm.exception))
                    for name in workspace.UPSTREAM_MODULES:
                        self.assertNotIn(name, sys.modules)

    def test_load_upstream_refuses_bytes_other_than_pinned(self) -> None:
        ws = self.synthetic_ws()
        (ws / "benchmark.py").write_text(FAKE_BENCHMARK + "PLANTED = True\n")
        with self.assertRaises(UpstreamContractError):
            with workspace.load_upstream(self.paths):
                pass
        self.assertNotIn("claude_wrapper", sys.modules)

    def test_tampered_pristine_record_is_refused(self) -> None:
        ws = self.synthetic_ws()
        evil = FAKE_BENCHMARK + "EVIL = True\n"
        (ws / "benchmark.py").write_text(evil)
        doc = util.read_json(self.paths.pristine)
        doc["files"]["benchmark.py"] = util.sha256_bytes(evil.encode())     # tree_sha256 left as pinned
        util.write_json(self.paths.pristine, doc)
        with self.assertRaises(WorkspaceError):
            workspace.verify_pinned(self.paths)
        with self.assertRaises(WorkspaceError):
            with workspace.load_upstream(self.paths):
                self.fail("tampered pinned bytes were loaded")
        self.assertNotIn("benchmark", sys.modules)

    def test_missing_yaml_names_the_supported_interpreters(self) -> None:
        ws = self.synthetic_ws({"benchmark.py": "import yaml\n" + FAKE_BENCHMARK, "config.yaml": "a: 1\n"})
        with mock.patch.dict(sys.modules, {"yaml": None}):
            with self.assertRaises(MhforgeError) as cm:
                workspace.load_config(ws)
            self.assertIn(".venv/bin/python", str(cm.exception))
            with self.assertRaises(MhforgeError) as cm:
                with workspace.load_upstream(self.paths):
                    pass
            self.assertIn("live", str(cm.exception))
        self.assertNotIn("benchmark", sys.modules)

    def test_ensure_workspace_repairs_a_copy_without_pristine(self) -> None:
        ws = self.synthetic_ws()
        (ws / "agents" / "kept.py").write_text("k\n")
        (ws / "llm.py").write_text("edited\n")
        self.paths.pristine.unlink()
        example = self.tmp / "example"
        workspace.ensure_workspace(manifest_for(example), self.paths, example, None)
        self.assertEqual(workspace.verify_pinned(self.paths), {"modified": [], "removed": []})
        self.assertTrue((ws / "agents" / "kept.py").exists())
        with self.assertRaises(WorkspaceError):
            workspace.ensure_workspace(make_manifest(**{"upstream.tree_sha256": None}), self.paths, example, None)
        with self.assertRaises(WorkspaceError):
            workspace.ensure_workspace(make_manifest(**{"upstream.tree_sha256": "1" * 64}), self.paths, example, None)


# ---------------------------------------------------------------- against the real example directory

def _ls_tree(ex: Path) -> list[str] | None:
    git = shutil.which("git")
    if git is None:
        return None
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "GIT_CONFIG_NOSYSTEM": "1",
           "HOME": tempfile.gettempdir()}
    top = subprocess.run([git, "-C", str(ex), "rev-parse", "--show-toplevel"], capture_output=True, text=True, env=env)
    if top.returncode != 0:
        return None
    prefix = os.path.relpath(os.path.realpath(ex), os.path.realpath(top.stdout.strip())).replace(os.sep, "/")
    commit = make_manifest()["upstream"]["commit"]
    out = subprocess.run([git, "-C", top.stdout.strip(), "ls-tree", "-r", "--name-only", commit, "--", prefix],
                         capture_output=True, text=True, env=env)
    if out.returncode != 0:
        return None
    return sorted(line[len(prefix) + 1:] for line in out.stdout.splitlines() if line)


def _tree_state(root: Path) -> dict[str, tuple]:
    state = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if Path(dirpath) == root and ".venv" in dirnames:
            dirnames.remove(".venv")
            st = os.lstat(root / ".venv")
            state[".venv"] = (st.st_mtime_ns,)
        for name in dirnames + filenames:
            st = os.lstat(Path(dirpath) / name)
            state[(Path(dirpath) / name).relative_to(root).as_posix()] = (st.st_mode, st.st_size, st.st_mtime_ns)
    return state


class _RealExampleCase(WorkspaceCase):
    def setUp(self) -> None:
        super().setUp()
        self.ex = helpers.example_dir()
        self.manifest = make_manifest(**{"upstream.tree_sha256": workspace.source_digest(self.ex)[0]})
        lock = self.ex / "uv.lock"
        if not lock.is_file():
            lock = self.tmp / "uv.lock"
            lock.write_text("version = 1\n")
        self.lock = lock

    def ensure(self) -> Path:
        return workspace.ensure_workspace(self.manifest, self.paths, self.ex, self.lock)


@needs_example_dir
class TestRealExample(_RealExampleCase):

    def test_pinned_files_are_the_tracked_set(self) -> None:
        self.assertEqual(len(PINNED_FILES), 39)
        self.assertEqual(len(set(PINNED_FILES)), 39)
        for rel in PINNED_FILES:
            self.assertTrue((self.ex / rel).is_file(), rel)
        tracked = _ls_tree(self.ex)
        if tracked is not None:
            self.assertEqual(sorted(PINNED_FILES), tracked)
        self.assertEqual(tuple(workspace.PINNED_AGENTS), ("__init__", "no_memory", "fewshot_memory", "fewshot_all"))
        self.assertTrue(all(f"agents/{a}.py" in PINNED_FILES for a in workspace.PINNED_AGENTS))

    def test_copy_matches_tree_hash_and_excludes_venv(self) -> None:
        ws = self.ensure()
        self.assertEqual(ws, self.paths.ws)
        self.assertEqual(workspace.source_digest(ws), workspace.source_digest(self.ex))
        files = sorted(p.relative_to(ws).as_posix() for p in ws.rglob("*") if p.is_file() or p.is_symlink())
        self.assertEqual(files, sorted(PINNED_FILES + ("uv.lock",)))
        self.assertFalse((ws / ".venv").exists())
        self.assertFalse(any(p.is_symlink() for p in ws.rglob("*")))
        self.assertEqual(util.sha256_file(ws / "uv.lock"), util.sha256_file(self.lock))
        pristine = util.read_json(self.paths.pristine)
        self.assertEqual(pristine["schema"], "mhforge.pristine/1")
        self.assertEqual(pristine["tree_sha256"], self.manifest["upstream"]["tree_sha256"])
        self.assertEqual(set(pristine["files"]), set(PINNED_FILES))
        util.parse_utc(pristine["copied_utc"])
        self.assertEqual(workspace.verify_pinned(self.paths), {"modified": [], "removed": []})
        self.assertEqual(workspace.unknown_entries(self.paths), [])
        self.assertEqual(self.ensure(), ws)                         # a later call is a no-op on a clean copy

    def test_digest_mismatch_refused(self) -> None:
        bad = make_manifest(**{"upstream.tree_sha256": "0" * 64})
        with self.assertRaises(WorkspaceError):
            workspace.ensure_workspace(bad, self.paths, self.ex, self.lock)
        self.assertFalse(os.path.lexists(self.paths.ws))
        self.assertFalse(self.paths.pristine.exists())
        self.ensure()
        with self.assertRaises(WorkspaceError):                     # an existing copy pinned to another tree
            workspace.ensure_workspace(bad, self.paths, self.ex, self.lock)

    def test_edit_to_pinned_file_is_detected_and_restored(self) -> None:
        ws = self.ensure()
        (ws / "llm.py").write_text("# edited by a proposer\n")
        (ws / "data" / "api.py").unlink()
        victim = self.tmp / "victim.txt"
        victim.write_text("untouched")
        (ws / "config.yaml").unlink()
        os.symlink(victim, ws / "config.yaml")                     # a pinned file replaced by a link
        (ws / "agents" / "no_memory.py").write_text("# edited\n")
        changed = workspace.verify_pinned(self.paths)
        self.assertEqual(changed, {"modified": ["agents/no_memory.py", "config.yaml", "llm.py"],
                                   "removed": ["data/api.py"]})
        restored = workspace.restore_pinned(self.paths, self.ex, changed)
        self.assertEqual(restored, ["agents/no_memory.py", "config.yaml", "data/api.py", "llm.py"])
        self.assertEqual(victim.read_text(), "untouched")            # restored over the link, never through it
        self.assertFalse((ws / "config.yaml").is_symlink())
        self.assertEqual(workspace.verify_pinned(self.paths), {"modified": [], "removed": []})
        with self.assertRaises(WorkspaceError):
            workspace.restore_pinned(self.paths, self.ex, {"modified": ["agents/cand.py"]})
        (ws / "inner_loop.py").write_text("x")
        self.ensure()                                               # later calls restore by themselves
        self.assertEqual(workspace.verify_pinned(self.paths), {"modified": [], "removed": []})

    def test_new_agent_file_is_kept(self) -> None:
        ws = self.ensure()
        (ws / "agents" / "my_cand.py").write_text("from text_classification.memory_system import MemorySystem\n")
        (ws / "agents" / "__pycache__").mkdir()
        (ws / "llm.py").write_text("edited")
        self.ensure()
        workspace.restore_pinned(self.paths, self.ex, {"modified": ["llm.py"], "removed": []})
        self.assertTrue((ws / "agents" / "my_cand.py").is_file())
        self.assertTrue((ws / "agents" / "__pycache__").is_dir())
        self.assertIsNone(workspace.validate_candidate_file(self.paths, "my_cand"))

    def test_symlink_is_refused(self) -> None:
        copy = self.tmp / "example_copy"
        for rel in PINNED_FILES:
            (copy / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.ex / rel, copy / rel)
        self.assertEqual(workspace.source_digest(copy), workspace.source_digest(self.ex))
        (copy / "llm.py").unlink()
        os.symlink(self.ex / "llm.py", copy / "llm.py")
        with self.assertRaises(WorkspaceError):
            workspace.source_digest(copy)
        with self.assertRaises(WorkspaceError):
            workspace.ensure_workspace(self.manifest, self.paths, copy, None)
        self.assertFalse(os.path.lexists(self.paths.ws))
        (copy / "llm.py").unlink()
        shutil.copyfile(self.ex / "llm.py", copy / "llm.py")
        shutil.move(copy / "data" / "uspto", self.tmp / "uspto_real")
        os.symlink(self.tmp / "uspto_real", copy / "data" / "uspto")   # a symlinked directory on the way
        with self.assertRaises(WorkspaceError):
            workspace.source_digest(copy)
        (copy / "data" / "uspto").unlink()
        with self.assertRaises(WorkspaceError):                      # and a missing file
            workspace.source_digest(copy)

    def test_nothing_written_outside_state(self) -> None:
        before = _tree_state(self.ex)
        sc_before = sorted(os.listdir(self.st.sc))
        ws = self.ensure()
        (ws / "llm.py").write_text("edited")
        self.ensure()
        workspace.snapshot(self.paths)
        workspace.unknown_entries(self.paths)
        (ws / "stray").write_text("")
        workspace.quarantine_entries(self.paths, ["stray"], "2026-11-10T00:00:00.000000Z")
        self.assertEqual(_tree_state(self.ex), before)
        self.assertEqual(sorted(os.listdir(self.st.sc)), sc_before)
        self.assertEqual(sorted(os.listdir(self.st.sc / "state")), [self.st.campaign])
        self.assertEqual(sorted(os.listdir(self.paths.root)), ["quarantine", "workspace"])
        self.assertEqual(sorted(os.listdir(self.paths.workspace_root)), ["pristine.json", "text_classification"])


@needs_example_dir
@needs_yaml
class TestRealUpstreamImport(_RealExampleCase):
    def test_load_upstream_contract_holds_and_paths_stay_in_copy(self) -> None:
        ws = self.ensure()
        with workspace.load_upstream(self.paths) as up:
            mh, bm, cw = up.meta_harness, up.benchmark, up.claude_wrapper
            self.assertEqual(mh.EVOLVE_DIR, ws)
            for attr in ("AGENTS_DIR", "LOGS_DIR", "PENDING_EVAL", "FRONTIER_VAL", "RESULTS_DIR", "CONFIG_PATH"):
                self.assertTrue(Path(getattr(mh, attr)).is_relative_to(ws), attr)
            for mod, name in ((mh, "meta_harness"), (bm, "benchmark"), (cw, "claude_wrapper")):
                self.assertEqual(Path(mod.__file__), ws / f"{name}.py")
                self.assertIs(sys.modules[name], mod)
            self.assertIs(mh.claude_wrapper, cw)
            self.assertIs(mh.load_results, bm.load_results)
            self.assertEqual(cw._EMPTY_PLUGIN_DIR, ws / ".empty_plugins")
            self.assertEqual(bm.SEEDS, [42])
            self.assertEqual(bm.DATASETS, self.manifest["upstream"]["expect"]["datasets"])
            self.assertEqual(workspace.load_config(ws), bm._CONFIG)
        for name in workspace.UPSTREAM_MODULES:
            self.assertNotIn(name, sys.modules)
        self.assertFalse(Path(self.ex, "logs").exists())

    def test_load_upstream_leaves_sys_path_unchanged(self) -> None:
        ws = self.ensure()
        path_before = list(sys.path)
        flag_before = sys.dont_write_bytecode
        with workspace.load_upstream(self.paths):
            self.assertEqual(sys.path, path_before)
            self.assertTrue(sys.dont_write_bytecode)
            self.assertFalse(any(str(ws) in p or str(ws.parent) == p for p in sys.path))
        self.assertEqual(sys.path, path_before)
        self.assertEqual(sys.dont_write_bytecode, flag_before)
        self.assertEqual(list(ws.rglob("__pycache__")), [])
        self.assertEqual(list(ws.rglob("*.pyc")), [])

    def test_planted_module_in_workspace_is_not_imported(self) -> None:
        ws = self.ensure()
        marker = self.tmp / "planted-ran"
        planted = f"open({str(marker)!r}, 'w').write('ran')\nraise SystemExit('planted module imported')\n"
        (ws / "yaml.py").write_text(planted)
        (ws / "random.py").write_text(planted)
        script = textwrap.dedent("""
            import importlib.machinery, json, sys
            from pathlib import Path
            from mhforge import workspace
            from mhforge.paths import StatePaths
            assert "yaml" not in sys.modules
            paths = StatePaths(Path(sys.argv[1]))
            with workspace.load_upstream(paths) as up:
                import random, yaml
                out = {"yaml": yaml.__file__, "random": random.__file__, "datasets": up.benchmark.DATASETS,
                       "findable": importlib.machinery.PathFinder.find_spec("yaml", [str(paths.ws)]).origin}
            print(json.dumps(out))
        """)
        run_dir = self.tmp / "cwd"
        run_dir.mkdir()
        proc = subprocess.run([sys.executable, "-c", script, str(self.paths.root)], cwd=run_dir, env=helpers.child_env(),
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        out = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertFalse(marker.exists())
        self.assertFalse(Path(out["yaml"]).is_relative_to(ws))
        self.assertFalse(Path(out["random"]).is_relative_to(ws))
        self.assertEqual(out["findable"], str(ws / "yaml.py"))      # the planted file was there to be found
        self.assertEqual(out["datasets"], self.manifest["upstream"]["expect"]["datasets"])

    def test_poisoned_pycache_is_ignored(self) -> None:
        ws = self.ensure()
        marker = self.tmp / "poison-ran"
        evil = self.tmp / "evil_benchmark.py"
        evil.write_text(f"POISONED = True\nopen({str(marker)!r}, 'w').write('ran')\n")
        cfile = importlib.util.cache_from_source(str(ws / "benchmark.py"))
        py_compile.compile(str(evil), cfile=cfile, dfile=str(ws / "benchmark.py"), doraise=True,
                           invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        # the stock loader would execute it in place of the checked source
        probe = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import benchmark; "
                                "print(getattr(benchmark, 'POISONED', False))", str(ws)],
                               cwd=self.tmp, env=helpers.child_env(), capture_output=True, text=True, timeout=60)
        self.assertEqual(probe.stdout.strip(), "True", probe.stderr[-1000:])
        marker.unlink()
        with workspace.load_upstream(self.paths) as up:
            self.assertFalse(hasattr(up.benchmark, "POISONED"))
            self.assertTrue(hasattr(up.benchmark, "build_val_runs"))
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
