# SPDX-License-Identifier: MIT
"""Fakes for the job runner (spec 5.1): FakeFacade (an UpstreamFacade whose builders follow benchmark.py L464-L608 with
upstream's nine-token prefix, its skip-if-usable and --force rules and its shuffle), FakeExecutor (scripted JobResults
and output files, scripted import checks) and CandidateStore (stored candidate copies, as proposer.candidate_reference
serves them). Standard library and mhforge only."""

from __future__ import annotations

import hashlib
import json
import random
import tarfile
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from mhforge import util
from mhforge.executors import ImportResult, JobResult, JobSpec, merge_refusal_codes

DATASETS = ("USPTO", "Symptom2Disease", "LawBench")
MODEL = "openrouter/openai/gpt-oss-120b"
SIZES = {"USPTO": (50, 30, 100), "Symptom2Disease": (200, 50, 212), "LawBench": (200, 50, 100)}
_SKIP_MEMORY_FILES = {"__init__", "fewshot_memory"}


def upstream_prefix(ws: Path) -> list[str]:
    """benchmark._inner_loop_command() for a workspace (L296-L309)."""
    ws = Path(ws)
    return ["env", f"PYTHONPATH={ws.parent}", "uv", "run", "--project", str(ws), "python", "-m",
            "text_classification.inner_loop"]


def _usable(path: Path) -> bool:
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and isinstance(data.get("accuracy"), (int, float))


class FakeFacade:
    """UpstreamFacade over a workspace. skip: datasets the builder leaves out as if a usable result existed;
    force: datasets it returns (with --force) although a usable result exists; cmd_prefix: the prefix its commands
    carry (default: inner_loop_prefix()); canned: {split: [(desc, cmd)]} returned as they are."""

    def __init__(self, ws: Path, *, datasets: Sequence[str] = DATASETS, model: str = MODEL, model_short: str | None = None,
                 temperature: float | None = 0.0, mode: str = "offline", api_base: str | None = None,
                 shuffle_seed: int = 7, skip: Iterable[str] = (), force: Iterable[str] = (),
                 cmd_prefix: Sequence[str] | None = None, canned: Mapping[str, list] | None = None) -> None:
        self.ws = Path(ws)
        self.datasets = list(datasets)
        self.model = model
        self.model_short = model_short or model.split("/")[-1].lower()
        self.temperature, self.mode, self.api_base = temperature, mode, api_base
        self.rng = random.Random(shuffle_seed)
        self.skip, self.force = set(skip), set(force)
        self.cmd_prefix = list(cmd_prefix) if cmd_prefix is not None else None
        self.canned = dict(canned or {})
        self.calls: list[tuple] = []
        self.warnings: list[str] = []

    def discover(self) -> list[tuple[str, str]]:
        return [(f.stem, f"agents/{f.stem}.py") for f in sorted((self.ws / "agents").glob("*.py"))
                if f.stem not in _SKIP_MEMORY_FILES]

    def inner_loop_prefix(self) -> list[str]:
        return upstream_prefix(self.ws)

    def run_dir(self, base: Path, dataset: str, memory: str, model_short: str) -> Path:
        return Path(base) / dataset / memory / model_short

    def _prefix(self) -> list[str]:
        return list(self.cmd_prefix) if self.cmd_prefix is not None else self.inner_loop_prefix()

    def _tail(self, dataset: str) -> list[str]:
        n_train, n_val, n_test = SIZES.get(dataset, (4, 5, 6))
        cmd = ["--num-train", str(n_train), "--num-val", str(n_val), "--num-test", str(n_test)]
        if self.api_base:
            cmd += ["--api-base", self.api_base]
        if self.temperature is not None:
            cmd += ["--temperature", str(self.temperature)]
        return cmd

    def build_val_runs(self, logs_dir: Path, systems: list[tuple[str, str]]) -> list[tuple[str, list[str]]]:
        self.calls.append(("val", Path(logs_dir), list(systems)))
        if "val" in self.canned:
            return list(self.canned["val"])
        runs = []
        for dataset in self.datasets:
            for mem_name, mem_path in systems:
                rd = self.run_dir(logs_dir, dataset, mem_name, self.model_short)
                val_file = rd / "val.json"
                if dataset in self.skip or (val_file.exists() and _usable(val_file) and dataset not in self.force):
                    continue
                rd.mkdir(parents=True, exist_ok=True)
                cmd = self._prefix() + ["--memory", mem_path, "--dataset", dataset, "--seed", "42", "--model",
                                        self.model, "--mode", self.mode, "--val-output", str(val_file),
                                        "--save-memory", str(rd / "memory.json"), "--log", str(rd / "log.jsonl")]
                cmd += self._tail(dataset)
                if val_file.exists():
                    cmd.append("--force")
                runs.append((f"val/{dataset}/{mem_name}/{self.model_short}", cmd))
        self.rng.shuffle(runs)
        return runs

    def build_test_runs(self, logs_dir: Path, results_dir: Path,
                        systems: list[tuple[str, str]]) -> list[tuple[str, list[str]]]:
        self.calls.append(("test", Path(logs_dir), Path(results_dir), list(systems)))
        if "test" in self.canned:
            return list(self.canned["test"])
        runs = []
        for dataset in self.datasets:
            for mem_name, mem_path in systems:
                rd_results = self.run_dir(results_dir, dataset, mem_name, self.model_short)
                test_file = rd_results / "test.json"
                if dataset in self.skip or (test_file.exists() and _usable(test_file) and dataset not in self.force):
                    continue
                memory_file = self.run_dir(logs_dir, dataset, mem_name, self.model_short) / "memory.json"
                if not memory_file.exists():
                    self.warnings.append(f"no memory.json for {dataset}/{mem_name}/{self.model_short}")
                    continue
                rd_results.mkdir(parents=True, exist_ok=True)
                cmd = self._prefix() + ["--memory", mem_path, "--dataset", dataset, "--seed", "42", "--model",
                                        self.model, "--mode", self.mode, "--load-memory", str(memory_file),
                                        "--test-output", str(test_file)]
                n_train, n_val, n_test = SIZES.get(dataset, (4, 5, 6))
                cmd += ["--num-train", str(n_train), "--num-val", str(n_val), "--num-test", str(n_test)]
                if self.api_base:
                    cmd += ["--api-base", self.api_base]
                if self.temperature is not None:
                    cmd += ["--temperature", str(self.temperature)]
                if test_file.exists():
                    cmd.append("--force")
                runs.append((f"test/{dataset}/{mem_name}/{self.model_short}", cmd))
        self.rng.shuffle(runs)
        return runs


# ---------------------------------------------------------------- executor

def imp_ok() -> ImportResult:
    return ImportResult(ok=True, returncode=0, timed_out=False, stdout_tail="OK\n", stderr_tail="")


def imp_fail(returncode: int | None = 1, *, reason: str | None = None, timed_out: bool = False,
             stderr: str = "Traceback (most recent call last):\nImportError: boom\n") -> ImportResult:
    return ImportResult(ok=False, returncode=returncode, timed_out=timed_out, stdout_tail="", stderr_tail=stderr,
                        reason=reason)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


DEFAULT_GATEWAY = {"completed": 3, "replayed": 0, "coalesced": 0, "refused": 0, "refused_unrecorded": 0,
                   "provider_rejected": 0, "failed": 0, "refusal_codes": {}, "repeatable_provider_failures": 0,
                   "truncated_empty": 0, "cost_micro": 120, "bounded_unknown_micro": 0, "in_flight_at_close": 0,
                   "usage_calls": 3, "prompt_tokens_sum": 330, "prompt_tokens_max": 120, "completion_tokens_sum": 36}

# kind -> (returncode, timed_out, exit reason or None for no exit file, relay_end, kill_reason, refusal codes,
#          writes a usable result, host_sleep_s, stderr)
KINDS: dict[str, tuple] = {
    "ok": (0, False, "exit", None, None, {}, True, 0.0, ""),
    "worker_error": (1, False, "exit", None, None, {}, False, 0.0,
                     "Traceback (most recent call last):\nValueError: boom\n"),
    "no_result": (0, False, "exit", None, None, {}, False, 0.0, ""),
    "timeout": (124, True, "timeout", None, None, {}, False, 0.0, ""),
    "request_ceiling": (1, False, "exit", None, None, {"job_request_ceiling": 1}, False, 0.0, ""),
    "tainted_ok": (0, False, "exit", None, None, {"provider_unavailable": 2}, True, 0.0, ""),
    "relay_lost": (None, False, "uplink_lost", "eof", None, {}, False, 0.0, ""),
    "no_exit": (0, False, None, None, None, {}, False, 0.0, ""),
    "host_sleep": (124, True, "timeout", None, None, {}, False, 42.0, ""),
    "halt": (-9, False, "killed", None, "halt", {"halted": 3}, False, 0.0, ""),
}


class FakeExecutor:
    """An Executor whose run() writes scripted outputs under attempt_dir and returns a JobResult.

    A script item is a kind of KINDS, a dict {"kind", "accuracy", "correct", "total", "stdout", "stderr", "gateway",
    "facts", "refusal_codes", "memory_context_chars"}, a JobResult, an exception to raise, or a callable
    (spec, attempt_dir) -> one of those. `runs` records each call: spec, attempt_dir, the bundled candidate bytes and
    the bytes of each input."""

    name = "fake"
    isolated = False

    def __init__(self, script: Iterable = (), *, default: object = "ok", delay_s: float = 0.0,
                 import_script: Iterable = ()) -> None:
        self.script = list(script)
        self.default = default
        self.delay_s = delay_s
        self.import_script = list(import_script)
        self.runs: list[dict] = []
        self.import_calls: list[dict] = []
        self._lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.closed = False

    def _next(self, script: list, default: object) -> object:
        with self._lock:
            return script.pop(0) if script else default

    def run(self, spec: JobSpec, attempt_dir: Path, on_handle: Callable[[dict], None]) -> JobResult:
        on_handle({"pgid": 40000 + len(self.runs), "t_start": 1794268800.0})
        with self._lock:
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
            entry = {"spec": spec, "attempt_dir": Path(attempt_dir), "candidate": _bundled(spec),
                     "inputs": {n: Path(p).read_bytes() for n, p in spec.inputs}}
            self.runs.append(entry)
        try:
            if self.delay_s:
                time.sleep(self.delay_s)
            item = self._next(self.script, self.default)
            if callable(item) and not isinstance(item, type):
                item = item(spec, Path(attempt_dir))
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, JobResult):
                return item
            return make_result(item if isinstance(item, dict) else {"kind": item}, spec, Path(attempt_dir))
        finally:
            with self._lock:
                self.in_flight -= 1

    def import_check(self, names: Sequence[str], bundle_tar: Path, work_root: Path, on_handle, *,
                     timeouts_s: Sequence[float]) -> dict[str, ImportResult]:
        on_handle({"sandbox_id": f"sbx-fake-{len(self.import_calls)}"})
        self.import_calls.append({"names": list(names), "timeouts_s": list(timeouts_s), "bundle_tar": Path(bundle_tar),
                                  "work_root": Path(work_root)})
        item = self._next(self.import_script, None)
        if callable(item):
            item = item(list(names))
        if isinstance(item, BaseException):
            raise item
        results = {n: imp_ok() for n in names}
        results.update(item or {})
        return results

    def cleanup_orphans(self, handles: Sequence[dict]) -> int:
        return 0

    def close(self) -> None:
        self.closed = True


def _bundled(spec: JobSpec) -> bytes | None:
    member = f"pkg/text_classification/agents/{spec.identity.candidate}.py"
    try:
        with tarfile.open(spec.bundle_tar, "r:") as tf:
            fh = tf.extractfile(member)
            return fh.read() if fh is not None else None
    except (KeyError, OSError, tarfile.TarError):
        return None


def make_result(item: dict, spec: JobSpec, attempt_dir: Path) -> JobResult:
    """Write the files an executor would leave for one scripted attempt and return its JobResult."""
    kind = item.get("kind", "ok")
    rc, timed_out, reason, relay_end, kill_reason, codes, usable, slept, stderr = KINDS[kind]
    out, run = attempt_dir / "out", attempt_dir / "run"
    out.mkdir(parents=True, exist_ok=True)
    run.mkdir(parents=True, exist_ok=True)
    box = attempt_dir / "box"
    util.write_json(attempt_dir / "jobfile.json", {"schema": "mhforge.jobfile/1", "job_id": spec.identity.job_id,
                                                   "run_dir": f"{box}/run"})
    result_name = spec.expect_outputs[0]
    argv = list(spec.inner_argv)
    if "--save-memory" in argv:
        (out / "memory.json").write_text(json.dumps({"memory": item.get("memory", "learned"), "kind": kind}))
    if "--log" in argv:
        (out / "log.jsonl").write_text(json.dumps({"event": "done", "kind": kind}) + "\n")
    if usable:
        total = item.get("total", 30)
        correct = item.get("correct", 3)
        (out / result_name).write_text(json.dumps({
            "accuracy": item.get("accuracy", correct / total), "correct": correct, "total": total,
            "memory_context_chars": item.get("memory_context_chars", 191)}))
    stdout = item.get("stdout", f"Saved {spec.identity.split} results to {box}/out/{result_name}\n" if usable else "")
    (run / "stdout.txt").write_text(stdout)
    (run / "stderr.txt").write_text(item.get("stderr", stderr))
    exit_file = None
    if reason is not None:
        exit_file = {"schema": "mhforge.exit/1", "returncode": rc, "reason": reason, "timed_out": timed_out,
                     "t_start": 1.0, "t_end": 2.0}
        util.write_json(run / "exit.json", exit_file)
    gateway = dict(DEFAULT_GATEWAY, **(item.get("gateway") or {}))
    refusal = dict(item.get("refusal_codes", codes))
    gateway["refusal_codes"] = dict(refusal)
    outputs = {p.name: _sha(p.read_bytes()) for p in sorted(out.iterdir()) if p.is_file()}
    return JobResult(returncode=rc, timed_out=timed_out, exit_file=exit_file, relay_end=relay_end,
                     kill_reason=kill_reason, started_utc="2026-11-10T00:00:00.000000Z",
                     ended_utc="2026-11-10T00:01:00.000000Z", outputs=outputs, gateway=gateway,
                     refusal_codes=merge_refusal_codes(refusal, exit_file), stderr_tail=item.get("stderr", stderr)[-2000:],
                     host_sleep_s=float(item.get("host_sleep_s", slept)),
                     facts=dict(item.get("facts") or {"seconds": 60.0, "setup_seconds": 0.0, "cost_micro": 0}))


# ---------------------------------------------------------------- stored candidate copies

class CandidateStore:
    """Stored proposal copies under root, with proposer.candidate_reference's (name, sha256 or None) contract: the copy
    with that hash, or with None the latest copy of the name; None when there is no such copy."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.copies: dict[str, list[tuple[Path, str]]] = {}
        self.calls: list[tuple[str, str | None]] = []

    def add(self, name: str, data: bytes) -> tuple[Path, str]:
        n = sum(len(v) for v in self.copies.values()) + 1
        path = self.root / f"s{n}" / "candidates" / f"{name}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        entry = (path, _sha(data))
        self.copies.setdefault(name, []).append(entry)
        return entry

    def __call__(self, name: str, sha256: str | None) -> tuple[Path, str] | None:
        self.calls.append((name, sha256))
        copies = self.copies.get(name, [])
        if sha256 is None:
            return copies[-1] if copies else None
        for path, sha in reversed(copies):
            if sha == sha256:
                return path, sha
        return None


def tar_member(tar_path: Path, member: str) -> bytes | None:
    with tarfile.open(tar_path, "r:") as tf:
        try:
            fh = tf.extractfile(member)
        except KeyError:
            return None
        return fh.read() if fh is not None else None


__all__ = ["FakeFacade", "FakeExecutor", "CandidateStore", "upstream_prefix", "make_result", "imp_ok", "imp_fail",
           "KINDS", "DATASETS", "MODEL", "tar_member"]
