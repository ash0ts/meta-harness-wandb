# SPDX-License-Identifier: MIT
"""Job planning and running for upstream's ``run_benchmark`` calls (spec 3.18): the UpstreamFacade, plan_jobs, the
JobRunner (dispatch of upstream's five argument forms, attempts and budgets, publication and its verification, import
checks), launcher logs in upstream's format and the test inputs. Standard library and ``mhforge`` only.

Upstream's own builders decide which jobs exist and with which arguments; this module only turns their path flags into
placeholders, runs each job in an executor and copies back exactly the files upstream would have written.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable, Mapping, Protocol, Sequence

from . import bundle, procenv, scan, util, workspace
from .errors import CampaignStop, ExecutorError, UpstreamContractError, WorkspaceError
from .executors import ImportResult, JobResult, JobSpec, classify_attempt
from .journal import Journal
from .ledger import Ledger
from .paths import StatePaths
from .wire import CANDIDATE_RE, JobIdentity

__all__ = [
    "UpstreamFacade", "real_facade", "PlannedJob", "plan_jobs", "PhaseTracker", "JobRunner", "write_launcher_log",
    "publish_outputs", "verify_published_results", "resolve_test_input", "make_passthrough", "import_rejection",
    "PATH_FLAGS", "JOB_SCHEMA",
]

JOB_SCHEMA = "mhforge.job/1"
CHECK_SCHEMA = "mhforge.importcheck/1"
PREFIX_TOKENS = 9
PATH_FLAGS = {"--val-output": "out", "--save-memory": "out", "--log": "out", "--test-output": "out",
              "--load-memory": "in"}
OUTPUT_NAMES = ("val.json", "test.json", "memory.json", "log.jsonl")
PROBE = "no_memory"
_SEPARATOR = "=" * 60
_LOOPBACK_URL = re.compile(rb"https?://(?:127\.0\.0\.1|localhost|\[::1\]):[0-9]{1,5}[^\s\"']*")
_FACT_KEYS = ("sandbox_id", "seconds", "setup_seconds", "cost_micro", "environment", "relay", "selfcheck", "stop_error")
_SECRET_KEY = re.compile(r"(?i)(?:^|_)(?:token|bearer|api_?key|secret|password|address|url|port|listener)(?:$|_)")


# ---------------------------------------------------------------- upstream facade

class UpstreamFacade(Protocol):                    # the slice of upstream that job planning needs
    datasets: list[str]

    def discover(self) -> list[tuple[str, str]]: ...
    def inner_loop_prefix(self) -> list[str]: ...
    def build_val_runs(self, logs_dir: Path, systems: list[tuple[str, str]]) -> list[tuple[str, list[str]]]: ...
    def build_test_runs(self, logs_dir: Path, results_dir: Path,
                        systems: list[tuple[str, str]]) -> list[tuple[str, list[str]]]: ...
    def run_dir(self, base: Path, dataset: str, memory: str, model_short: str) -> Path: ...


class _RealFacade:
    """benchmark.main's own values (L887-L906): DATASETS, MODELS, and mode, num_epochs, temperature from
    _CONFIG["inner_loop"] (main's argparse defaults, L797-L810). The builders' 3-tuple is cut to its first element."""

    def __init__(self, benchmark: ModuleType) -> None:
        self._b = benchmark
        self.datasets = list(benchmark.DATASETS)
        inner = benchmark._CONFIG["inner_loop"]
        self._mode = inner.get("mode", "online")
        self._epochs = inner.get("num_epochs", 1)
        self._temperature = inner.get("temperature")

    def discover(self) -> list[tuple[str, str]]:
        return list(self._b.discover_all_memory_systems())

    def inner_loop_prefix(self) -> list[str]:
        return list(self._b._inner_loop_command())

    def build_val_runs(self, logs_dir, systems):
        return self._b.build_val_runs(Path(logs_dir), systems, self._b.DATASETS, self._b.MODELS, self._mode,
                                      self._epochs, self._temperature)[0]

    def build_test_runs(self, logs_dir, results_dir, systems):
        return self._b.build_test_runs(Path(logs_dir), Path(results_dir), systems, self._b.DATASETS, self._b.MODELS,
                                       self._mode, self._temperature)[0]

    def run_dir(self, base, dataset, memory, model_short):
        return Path(self._b.run_dir(Path(base), dataset, memory, model_short))


def real_facade(benchmark: ModuleType) -> UpstreamFacade:
    return _RealFacade(benchmark)


# ---------------------------------------------------------------- planning

@dataclass(frozen=True)
class PlannedJob:
    desc: str                                      # upstream's own description: "val/<dataset>/<candidate>/<model_short>"
    split: str
    dataset: str
    candidate: str
    upstream_command: str
    inner_argv: tuple[str, ...]
    host_outputs: dict[str, Path]                  # "val.json" | "test.json" | "memory.json" | "log.jsonl" -> upstream's path
    inputs: tuple[tuple[str, Path], ...]

    @property
    def result_name(self) -> str:
        return "test.json" if self.split == "test" else "val.json"


def _plan_one(desc: str, cmd: list[str], prefix: list[str], *, test: bool, name: str, datasets: Sequence[str],
              manifest: dict) -> PlannedJob:
    if not isinstance(cmd, list) or cmd[:len(prefix)] != prefix:
        raise UpstreamContractError(f"worker command of {str(desc)[:80]!r} does not start with upstream's prefix")
    parts = str(desc).split("/")
    split = "test" if test else "val"
    if len(parts) != 4 or parts[0] != split or parts[1] not in datasets or parts[2] != name:
        raise UpstreamContractError(f"unexpected job description {str(desc)[:80]!r}")
    route1 = util.lookup(manifest, "routing.route") == 1
    argv: list[str] = []
    outputs: dict[str, Path] = {}
    inputs: list[tuple[str, Path]] = []
    rest = [str(t) for t in cmd[len(prefix):]]
    i = 0
    while i < len(rest):
        token = rest[i]
        if token == "--force":
            i += 1
            continue
        if token in PATH_FLAGS or token in ("--model", "--api-base", "--dataset", "--memory"):
            if i + 1 >= len(rest):
                raise UpstreamContractError(f"flag {token} has no value")
            value = rest[i + 1]
            if token in PATH_FLAGS:
                host = Path(value)
                if host.name not in OUTPUT_NAMES:
                    raise UpstreamContractError(f"unexpected file name after {token}")
                kind = PATH_FLAGS[token]
                (inputs.append((host.name, host)) if kind == "in" else outputs.__setitem__(host.name, host))
                value = "{%s}/%s" % (kind, host.name)
            elif token == "--model" and route1:
                value = str(util.lookup(manifest, "routing.route1_model"))
            elif token == "--api-base" and route1:
                value = "{api_base}"
            elif token == "--dataset" and value != parts[1] or token == "--memory" and Path(value).stem != name:
                raise UpstreamContractError(f"{token} disagrees with the job description")
            argv += [token, value]
            i += 2
            continue
        argv.append(token)
        i += 1
    if route1 and "--api-base" not in argv:
        argv += ["--api-base", "{api_base}"]
    want = "test.json" if test else "val.json"
    if want not in outputs or (test and not any(n == "memory.json" for n, _ in inputs)):
        raise UpstreamContractError(f"job {str(desc)[:80]!r} lacks its result or input path")
    return PlannedJob(desc=str(desc), split=split, dataset=parts[1], candidate=name, upstream_command=" ".join(cmd),
                      inner_argv=tuple(argv), host_outputs=outputs, inputs=tuple(inputs))


def plan_jobs(facade: UpstreamFacade, memory: str, *, test: bool, logs_dir: Path, results_dir: Path,
              manifest: dict) -> tuple[list[PlannedJob], str | None, list[str]]:
    """(jobs in config.yaml dataset order, upstream's "not found" message or None, datasets skipped for a missing
    memory.json). Mirrors benchmark.main L865-L906 with upstream's own builders."""
    name = Path(memory).stem
    systems = [(n, p) for n, p in facade.discover() if n == name]
    if not systems:
        return [], f"Error: '{memory}' not found on disk.", []
    prefix = list(facade.inner_loop_prefix())
    if len(prefix) != PREFIX_TOKENS or prefix[-2:] != ["-m", "text_classification.inner_loop"]:
        raise UpstreamContractError("upstream's worker prefix is not the nine-token inner_loop command")
    datasets = list(facade.datasets)
    if test:
        runs = facade.build_test_runs(Path(logs_dir), Path(results_dir), systems)
    else:
        runs = facade.build_val_runs(Path(logs_dir), systems)
    jobs = [_plan_one(desc, cmd, prefix, test=test, name=name, datasets=datasets, manifest=manifest)
            for desc, cmd in runs]
    jobs.sort(key=lambda j: datasets.index(j.dataset))
    skipped: list[str] = []
    if test:
        model_short = str(util.lookup(manifest, "upstream.expect.model_short"))
        planned = {j.dataset for j in jobs}
        for ds in datasets:
            if ds not in planned and not bundle.result_is_usable(
                    Path(facade.run_dir(Path(results_dir), ds, name, model_short)) / "test.json"):
                skipped.append(ds)
    return jobs, None, skipped


@dataclass
class PhaseTracker:
    phase: str = "baseline"
    iteration: int | None = None


# ---------------------------------------------------------------- file helpers

def _read_regular(path: Path) -> bytes | None:
    """Bytes of a regular file read without following a symlink; None when absent or not regular."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            return None
        return fh.read()


def _usable(path: Path) -> bool:
    """upstream's usable-result rule, for a regular file only (a symlink planted by the worker is not a result)."""
    return os.path.isfile(path) and not os.path.islink(path) and bundle.result_is_usable(path)


def _sha_file(path: Path) -> str | None:
    data = _read_regular(path)
    return None if data is None else util.sha256_bytes(data)


def _safe_dir(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    if os.path.realpath(directory) != os.path.abspath(directory):
        raise WorkspaceError(f"refused to write through a symlinked directory: {directory.name}")
    return directory


def _copy_atomic(data: bytes, dest: Path) -> None:
    _safe_dir(dest.parent)
    tmp = dest.parent / f".{dest.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise


def publish_outputs(attempt_out: Path, planned: PlannedJob, *, include_result: bool) -> dict[str, str]:
    """Copy memory.json and log.jsonl (and the result file when include_result) from attempt_out to the paths upstream
    named, atomically. Returns {host path: sha256} of what was copied."""
    copied: dict[str, str] = {}
    for name, dest in sorted(planned.host_outputs.items()):
        if name in ("val.json", "test.json") and not include_result:
            continue
        data = _read_regular(Path(attempt_out) / name)
        if data is None:
            continue
        _copy_atomic(data, Path(dest))
        copied[str(dest)] = util.sha256_bytes(data)
    return copied


def _redact(data: bytes) -> bytes:
    """Remove job tokens, keys and loopback gateway addresses from text a worker printed."""
    data = _LOOPBACK_URL.sub(b"[gateway]", data)
    spans: list[list[int]] = []
    for f in sorted(scan.scan_bytes(data), key=lambda f: f.offset):
        if spans and f.offset <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], f.offset + f.length)
        else:
            spans.append([f.offset, f.offset + f.length])
    for start, end in reversed(spans):
        data = data[:start] + b"[redacted]" + data[end:]
    return data


def write_launcher_log(logs_dir: Path, index: int, desc: str, upstream_command: str,
                       attempts: Sequence[tuple[bytes, int]], *, path_map: Mapping[str, str]) -> Path:
    """LOGS/.launcher/<NN>_<sanitized desc>.log in upstream's format (benchmark.py L61-L62, L82-L102, L121)."""
    path = Path(logs_dir) / ".launcher" / f"{int(index):02d}_{re.sub(r'[^\w\-.]', '_', desc)}.log"
    pairs = sorted(((k.encode("utf-8"), v.encode("utf-8")) for k, v in path_map.items()), key=lambda p: -len(p[0]))
    body = f"command: {upstream_command}\n\n".encode("utf-8")
    for n, (output, code) in enumerate(attempts):
        if n > 0:
            body += f"\n{_SEPARATOR}\nretry {n}\n{_SEPARATOR}\n".encode("utf-8")
        text = bytes(output)
        for old, new in pairs:
            text = text.replace(old, new)
        body += text + f"\nexit={code}\n".encode("utf-8")
    _copy_atomic(_redact(body), path)
    return path


# ---------------------------------------------------------------- records

def _record_path(paths: StatePaths, job_key: str) -> Path:
    return paths.job_dir(job_key) / "job.json"


def _load_record(paths: StatePaths, job_key: str) -> dict | None:
    try:
        doc = util.read_json(_record_path(paths, job_key))
    except FileNotFoundError:
        return None
    return doc if isinstance(doc, dict) and doc.get("schema") == JOB_SCHEMA else None


def _all_records(paths: StatePaths) -> list[dict]:
    out = []
    try:
        entries = sorted(os.scandir(paths.jobs_root), key=lambda e: e.name)
    except FileNotFoundError:
        return out
    for entry in entries:
        if entry.is_dir(follow_symlinks=False):
            rec = _load_record(paths, entry.name)
            if rec is not None:
                out.append(rec)
    return out


def _rel(paths: StatePaths, path: Path) -> str:
    return Path(os.path.relpath(os.path.abspath(path), os.path.abspath(paths.ws))).as_posix()


def _published_attempt(rec: dict, rel: str) -> dict | None:
    pub = rec.get("published") or {}
    n = (pub.get("attempts") or {}).get(rel)
    for a in rec.get("attempts", []):
        if a.get("attempt") == n:
            return a
    return None


def resolve_test_input(paths: StatePaths, candidate: str, dataset: str) -> tuple[Path, str] | None:
    """The validated memory.json of val.<candidate>.<dataset>: (file in the attempt directory it was published from,
    sha256), after a hash check against published.files; None when absent."""
    rec = _load_record(paths, f"val.{candidate}.{dataset}")
    pub = (rec or {}).get("published") or {}
    for rel, sha in sorted((pub.get("files") or {}).items()):
        if not rel.endswith("/memory.json"):
            continue
        att = _published_attempt(rec, rel)
        if att is None:
            continue
        source = paths.attempt_dir(rec["job_key"], att["attempt"]) / "out" / "memory.json"
        if _sha_file(source) == sha:
            return source, sha
    return None


def _validated_sha(paths: StatePaths, candidate: str, dataset: str) -> str | None:
    rec = _load_record(paths, f"val.{candidate}.{dataset}")
    pub = (rec or {}).get("published") or {}
    rels = sorted(pub.get("files") or {}, key=lambda r: (not r.endswith("/val.json"), r))
    for rel in rels:
        att = _published_attempt(rec, rel)
        if att is not None and att.get("candidate_sha256"):
            return att["candidate_sha256"]
    return None


def verify_published_results(paths: StatePaths, journal: Journal, logs_dir: Path, results_dir: Path) -> dict:
    """Every val.json under logs_dir and test.json under results_dir (any depth, any system name; what
    benchmark.load_results reads) against published.files: an unpublished file is quarantined, a changed one is
    quarantined and published again from its attempt directory. {"quarantined": [rel], "republished": [rel]}."""
    owners: dict[str, tuple[str, dict]] = {}
    for rec in sorted(_all_records(paths), key=lambda r: str((r.get("published") or {}).get("at_utc") or "")):
        for rel, sha in ((rec.get("published") or {}).get("files") or {}).items():
            owners[rel] = (sha, rec)
    found = sorted({*Path(logs_dir).rglob("val.json"), *Path(results_dir).rglob("test.json")})
    # A published memory.json that changed or went missing is put back too: it is what upstream's test builder checks
    # and what the proposer reads (3.18 "Test inputs"); an unpublished memory.json is left alone (load_results never
    # reads one, and a test job takes its input from the attempt directory, never from LOGS).
    memories = [paths.ws / rel for rel in sorted(owners) if rel.endswith("/memory.json")]
    stamp = util.utc_iso(journal.clock.now())
    quarantined, republished = [], []
    for path in found + [m for m in memories if m not in found]:
        rel = _rel(paths, path)
        if os.path.realpath(path.parent) != os.path.abspath(path.parent):
            continue                                    # never move or write a file through a symlinked directory
        owner = owners.get(rel)
        if not os.path.lexists(path):
            if owner is None or not rel.endswith("/memory.json"):
                continue
        else:
            current = None if os.path.islink(path) else _sha_file(path)
            if owner is not None and current == owner[0]:
                continue
            dest = paths.quarantine_root / stamp / rel
            n = 1
            while os.path.lexists(dest):
                dest = paths.quarantine_root / stamp / f"{rel}.dup{n}"
                n += 1
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.rename(path, dest)
            quarantined.append(rel)
        if owner is None:
            continue
        sha, rec = owner
        att = _published_attempt(rec, rel)
        if att is None:
            continue
        source = paths.attempt_dir(rec["job_key"], att["attempt"]) / "out" / Path(rel).name
        data = _read_regular(source)
        if data is not None and util.sha256_bytes(data) == sha:
            _copy_atomic(data, Path(path))
            republished.append(rel)
    if quarantined:
        journal.record("workspace", "quarantine", "complete", detail={"paths": quarantined})
    if republished:
        journal.record("workspace", "republish", "complete", detail={"paths": republished})
    return {"quarantined": quarantined, "republished": republished}


def _clean(value: object) -> object:
    """Drop keys that could carry a token, key or gateway address; redact such strings."""
    if isinstance(value, Mapping):
        return {str(k): _clean(v) for k, v in value.items() if not _SECRET_KEY.search(str(k))}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, str):
        return _redact(value.encode("utf-8")).decode("utf-8", "replace")
    return value


def import_rejection(result: ImportResult) -> str | None:
    """None for a passed import check; "loaded_class" for reason foreign_class or no_class; "import" otherwise."""
    if result.ok:
        return None
    return "loaded_class" if result.reason in ("foreign_class", "no_class") else "import"


def make_passthrough(python: str, ws: Path, logs_dir: Path, *, home: Path, timeout_s: float = 600,
                     run: Callable[..., subprocess.CompletedProcess] = subprocess.run
                     ) -> Callable[[Sequence[str]], subprocess.CompletedProcess]:
    """[python, -P, benchmark.py, --logs-dir LOGS, --results-dir LOGS/results, *args], cwd WS, procenv.passthrough_env,
    captured output, timeout_s (returncode 124 on a timeout, as upstream's run_cmd). python is used as given (never
    resolved)."""
    def passthrough(args: Sequence[str]) -> subprocess.CompletedProcess:
        argv = [str(python), "-P", "benchmark.py", "--logs-dir", str(logs_dir), "--results-dir",
                str(Path(logs_dir) / "results"), *[str(a) for a in args]]
        env = procenv.passthrough_env(home=str(home), python_bin_dir=os.path.dirname(str(python)))
        try:
            return run(argv, cwd=str(ws), env=env, capture_output=True, text=True, timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(argv, 124, stdout="", stderr=f"Timed out after {timeout_s}s")
    return passthrough


# ---------------------------------------------------------------- the runner

class _NotValidated(WorkspaceError):
    """A test job's candidate file changed while its bundle was built and no stored copy has the validated hash."""


@dataclass
class _Prep:
    candidate_sha256: str | None = None
    override: Path | None = None
    restored: bool = False
    inputs: dict | None = None
    input_files: tuple = ()
    problem: tuple[str, str] | None = None         # (status, message) when the job cannot run


class JobRunner:
    def __init__(self, manifest: dict, paths: StatePaths, *, journal: Journal, ledger: Ledger, executor,
                 facade: UpstreamFacade, tracker: PhaseTracker, example_dir: Path, logs_dir: Path, results_dir: Path,
                 passthrough: Callable[[Sequence[str]], subprocess.CompletedProcess],
                 candidate_reference: Callable[[str, str | None], tuple[Path, str] | None] | None = None,
                 bundle_builder: Callable[..., dict] | None = None,
                 on_unit: Callable[[str], None] | None = None,
                 expect_freeze_sha256: Callable[[], str | None] | None = None,
                 clock: util.Clock | None = None, rng=None) -> None:
        self.manifest, self.paths, self.journal, self.ledger = manifest, paths, journal, ledger
        self.executor, self.facade, self.tracker = executor, facade, tracker
        self.example_dir, self.logs_dir, self.results_dir = Path(example_dir), Path(logs_dir), Path(results_dir)
        self.passthrough = passthrough
        self.candidate_reference = candidate_reference
        self.bundle_builder = bundle_builder or bundle.build_bundle
        self.on_unit = on_unit or (lambda unit: None)
        self.expect_freeze_sha256 = expect_freeze_sha256 or (lambda: None)
        self.clock = clock or util.SystemClock()
        self.rng = rng
        self.campaign = str(util.lookup(manifest, "campaign"))
        self.max_attempts = int(util.lookup(manifest, "executor.max_attempts"))
        self.max_infra = int(util.lookup(manifest, "executor.max_infra_attempts"))

    # ------------------------------------------------------------ dispatch

    def run_benchmark(self, args: Sequence[str]) -> subprocess.CompletedProcess:
        args = [str(a) for a in args]
        if len(args) == 2 and args[0] == "--memory":
            return self._memory_call(args, args[1], test=False)
        if len(args) == 3 and args[0] == "--memory" and args[2] == "--test":
            return self._memory_call(args, args[1], test=True)
        if (len(args) == 3 and args[:2] == ["--frontier", "--model"]
                or len(args) == 4 and args[:3] == ["--frontier", "--test", "--model"]
                or args == ["--results", "--test"]):
            return self._passthrough(args)
        raise UpstreamContractError("unknown run_benchmark form")

    def _passthrough(self, args: Sequence[str]) -> subprocess.CompletedProcess:
        self._verify_pinned()
        self.verify_published()
        return self.passthrough(list(args))

    def _verify_pinned(self) -> None:
        changed = workspace.verify_pinned(self.paths)
        if changed["modified"] or changed["removed"]:
            restored = workspace.restore_pinned(self.paths, self.example_dir, changed)
            self.journal.record("workspace", "restore", "complete", detail={"paths": restored})

    def verify_published(self) -> dict:
        return verify_published_results(self.paths, self.journal, self.logs_dir, self.results_dir)

    def halt_check(self) -> None:
        halt = self.ledger.halted()
        if halt is not None:
            raise CampaignStop(reason=halt.reason, detail=halt.detail)

    def _identity(self, planned: PlannedJob, attempt: int, phase: str, iteration: int | None,
                  repeat: int | None = None, sample: int | None = None) -> JobIdentity:
        return JobIdentity(self.campaign, planned.split, planned.candidate, planned.dataset, attempt, phase,
                           iteration, repeat, sample).validate()

    def _memory_call(self, args: list[str], memory: str, *, test: bool) -> subprocess.CompletedProcess:
        self.halt_check()
        name = Path(memory).stem
        split = "test" if test else "val"
        if not CANDIDATE_RE.fullmatch(name):            # a name we would never use as a key or a module
            return subprocess.CompletedProcess(args, 0, stdout=f"Error: '{memory}' not found on disk.\n", stderr="")
        key = f"{split}.{name}"
        phase, iteration = ("test", None) if test else (self.tracker.phase,
                                                        self.tracker.iteration if self.tracker.phase == "search" else None)
        self.journal.record("benchmark", key, "started", detail={"phase": phase})
        self.verify_published()
        jobs, message, no_memory = plan_jobs(self.facade, memory, test=test, logs_dir=self.logs_dir,
                                             results_dir=self.results_dir, manifest=self.manifest)
        if message is not None:
            self.journal.record("benchmark", key, "complete", detail={"returncode": 0, "jobs": {}, "published": False,
                                                                     "quarantined": False})
            return subprocess.CompletedProcess(args, 0, stdout=message + "\n", stderr="")
        planned_ds = {j.dataset for j in jobs}
        statuses: dict[str, str] = {}
        for ds in self.facade.datasets:
            if ds in planned_ds:
                continue
            if ds in no_memory:
                statuses[ds] = "skipped_no_memory"
                self._skip(split, name, ds, phase, iteration, "no_memory")
            else:
                statuses[ds] = "skipped_usable"
                self._skip(split, name, ds, phase, iteration, "usable")
        policy = util.lookup(self.manifest, "search.partial_candidate_policy")
        parallel = 1 if test else max(1, int(util.lookup(self.manifest, "executor.parallel_dataset_jobs.validation")))
        records: list[dict | None] = [None] * len(jobs)
        errors: list[BaseException | None] = [None] * len(jobs)
        stopping = threading.Event()                    # a queued job never starts after another one raised

        def one(index: int) -> None:
            if stopping.is_set():
                return
            try:
                records[index] = self._benchmark_job(index, jobs[index], phase, iteration,
                                                     publish=(policy == "upstream"))
            except BaseException as exc:            # re-raised in dataset order after every running job ended
                errors[index] = exc
                stopping.set()

        if parallel == 1:
            for i in range(len(jobs)):
                one(i)
                if errors[i] is not None:
                    break
        else:
            with ThreadPoolExecutor(max_workers=parallel) as pool:
                try:
                    list(pool.map(one, range(len(jobs))))
                except BaseException:
                    stopping.set()
                    raise
        for exc in errors:
            if exc is not None:
                raise exc
        for planned, rec in zip(jobs, records):
            statuses[planned.dataset] = rec["status"]
        pending = [s for ds, s in statuses.items() if ds in planned_ds or s == "skipped_no_memory"]
        all_ok = all(s == "succeeded" for s in pending)
        quarantined = policy == "quarantine" and not all_ok
        published = any(rec.get("_published_now") for rec in records)
        if policy == "quarantine" and all_ok:
            for planned, rec in zip(jobs, records):
                published = self._publish(planned, rec, include_result=True) or published
        for rec in records:
            rec.pop("_published_now", None)
        ordered = {ds: statuses[ds] for ds in self.facade.datasets if ds in statuses}
        model_short = util.lookup(self.manifest, "upstream.expect.model_short")
        lines = [f"{split}/{ds}/{name}/{model_short}: {st}" for ds, st in ordered.items()
                 if ds in planned_ds or st == "skipped_no_memory"]
        failures = [f"FAILED: {split}/{ds}/{name}/{model_short} ({st})" for ds, st in ordered.items()
                    if (ds in planned_ds or st == "skipped_no_memory") and st != "succeeded"]
        code = 0 if all_ok else 1
        if not test:
            self._passthrough(["--results"])
        self.journal.record("benchmark", key, "complete", detail={"returncode": code, "jobs": ordered,
                                                                 "published": bool(published),
                                                                 "quarantined": quarantined})
        self.on_unit(f"eval:{split}.{name}")
        self.halt_check()
        stdout = "\n".join(lines) + "\n" if lines else "All done!\n"
        return subprocess.CompletedProcess(args, code, stdout=stdout, stderr=failures[0] + "\n" if failures else "")

    def _skip(self, split: str, name: str, dataset: str, phase: str, iteration: int | None, why: str) -> None:
        job_key = JobIdentity(self.campaign, split, name, dataset, 1, phase, iteration).job_key
        self.journal.record("job", job_key, "skipped", detail={"why": why})
        rec = _load_record(self.paths, job_key)
        if why == "no_memory":
            if rec is None:
                rec = self._blank_record_for(split, name, dataset, phase, iteration, job_key)
            rec["status"] = "skipped_no_memory"
            rec["message"] = "memory.json not found"
            self._save(rec)
        elif rec is None:                                # upstream found a usable result no record of ours holds
            rec = self._blank_record_for(split, name, dataset, phase, iteration, job_key)
            rec["status"] = "skipped_usable"
            self._save(rec)
        elif split == "val" and name not in workspace.PINNED_AGENTS:
            current = workspace.candidate_sha256(self.paths, name)
            ref = self.candidate_reference(name, None) if self.candidate_reference else None
            current = ref[1] if ref else current
            collision = bool(rec.get("candidate_sha256") and current and current != rec["candidate_sha256"])
            if collision != bool(rec.get("name_collision")):
                rec["name_collision"] = collision
                self._save(rec)

    def _blank_record_for(self, split, name, dataset, phase, iteration, job_key) -> dict:
        return {"schema": JOB_SCHEMA, "campaign": self.campaign, "job_key": job_key, "split": split,
                "candidate": name, "dataset": dataset, "phase": phase, "iteration": iteration, "repeat": None,
                "sample": None, "seed": None, "candidate_sha256": None, "route": util.lookup(self.manifest, "routing.route"),
                "executor": getattr(self.executor, "name", None), "isolated": bool(getattr(self.executor, "isolated", False)),
                "upstream_desc": f"{split}/{dataset}/{name}/{util.lookup(self.manifest, 'upstream.expect.model_short')}",
                "upstream_command": None, "inner_argv": [], "status": "failed", "attempts": [], "published": None,
                "result": None, "name_collision": False, "export_unit": None}

    def _save(self, rec: dict) -> None:
        util.write_json(_record_path(self.paths, rec["job_key"]), {k: v for k, v in rec.items() if not k.startswith("_")})

    # ------------------------------------------------------------ one job inside a --memory call

    def _benchmark_job(self, index: int, planned: PlannedJob, phase: str, iteration: int | None, *,
                       publish: bool) -> dict:
        job_key = self._identity(planned, 1, phase, iteration).job_key
        prep = self._prepare(planned)
        reuse = None if prep.problem else self._reusable(planned, job_key, prep)
        if reuse is not None:
            rec, ran = reuse, [reuse["_att"]]
            rec["status"] = "succeeded"
            self._save(rec)
            published_now = publish and self._publish(planned, rec, include_result=True)
        else:
            rec, ran = self._run_job(planned, phase=phase, iteration=iteration, publish=publish)
            published_now = rec.pop("_published_now", False)
        if ran:
            self._launcher_log(index, planned, rec, ran)
        rec["_published_now"] = published_now
        return rec

    def _reusable(self, planned: PlannedJob, job_key: str, prep: _Prep) -> dict | None:
        rec = _load_record(self.paths, job_key)
        if rec is None:
            return None
        by_number = {a.get("attempt"): a for a in rec.get("attempts", [])}
        for summary in reversed(self.journal.attempts("job", job_key)):
            detail, started = summary["detail"], summary["started_detail"]
            if summary["status"] != "complete" or detail.get("outcome") != "ok" or detail.get("infra"):
                continue
            if started.get("candidate_sha256") != prep.candidate_sha256 or (started.get("inputs") or {}) != prep.inputs:
                continue
            n = summary["attempt"]
            att = by_number.get(n)
            out = self.paths.attempt_dir(job_key, n) / "out" / planned.result_name
            if att is None or att.get("outcome") != "ok" or not detail.get("result_sha256"):
                continue
            if _sha_file(out) != detail["result_sha256"]:
                continue
            rec["_att"] = n
            rec["result"] = self._result_summary(out)
            return rec
        return None

    # ------------------------------------------------------------ candidate bytes and inputs

    def _prepare(self, planned: PlannedJob) -> _Prep:
        name, prep = planned.candidate, _Prep(inputs={})
        if planned.split == "test":
            found = resolve_test_input(self.paths, name, planned.dataset)
            if found is None:
                return _Prep(problem=("skipped_no_memory", "validated memory.json not found"))
            prep.input_files = (("memory.json", found[0]),)
            prep.inputs = {"memory.json": found[1]}
        if name in workspace.PINNED_AGENTS:
            prep.candidate_sha256 = util.read_json(self.paths.pristine)["files"].get(f"agents/{name}.py")
            return prep
        ws_sha = workspace.candidate_sha256(self.paths, name)
        if planned.split == "test":
            wanted = _validated_sha(self.paths, name, planned.dataset)
            if wanted is None:
                return _Prep(problem=("failed", "validated candidate file not found"))
            if ws_sha == wanted:
                prep.candidate_sha256 = wanted
                return prep
            ref = self.candidate_reference(name, wanted) if self.candidate_reference else None
            if ref is None or ref[1] != wanted:
                return _Prep(problem=("failed", "validated candidate file not found"))
        else:
            ref = self.candidate_reference(name, None) if self.candidate_reference else None
            if ref is None or ref[1] == ws_sha:
                if ws_sha is None:
                    return _Prep(problem=("failed", "candidate file not found"))
                prep.candidate_sha256 = ws_sha
                return prep
        prep.override, prep.candidate_sha256, prep.restored = Path(ref[0]), ref[1], True
        return prep

    # ------------------------------------------------------------ run_job

    def run_job(self, planned: PlannedJob, *, phase: str, iteration: int | None, publish: bool = True,
                repeat: int | None = None, sample: int | None = None) -> dict:
        """Attempts of one job until ok, a candidate failure with no attempt left, or an infrastructure stop. With
        publish and search.partial_candidate_policy "upstream" the outputs are published to the paths upstream named."""
        job_key = self._identity(planned, 1, phase, iteration, repeat, sample).job_key
        prep = self._prepare(planned)
        rec = None if prep.problem else self._reusable(planned, job_key, prep)
        if rec is not None and rec.get('inner_argv') == list(planned.inner_argv):
            att = next(a for a in rec['attempts'] if a['attempt'] == rec['_att'])
            out = self.paths.attempt_dir(job_key, att['attempt']) / 'out'
            if all(_sha_file(out / name) == digest for name, digest in att['outputs'].items()):
                rec['status'] = 'succeeded'
                self._save(rec)
                if publish:
                    self._publish(planned, rec, include_result=True)
                rec.pop('_att', None)
                return rec
        rec, _ = self._run_job(planned, phase=phase, iteration=iteration, publish=publish, repeat=repeat, sample=sample)
        rec.pop("_published_now", None)
        return rec

    def _infra_this_invocation(self, job_key: str) -> int:
        since = self.journal.session_start_seq()
        return sum(1 for a in self.journal.attempts("job", job_key)
                   if a["seq"] > since and a["status"] not in ("started",) and a["detail"].get("infra"))

    def _candidate_failures(self, job_key: str, sha: str | None) -> list[dict]:
        return [a for a in self.journal.counted_attempts(job_key, sha)
                if a["status"] == "failed" and a["detail"].get("infra") is False]

    def _run_job(self, planned: PlannedJob, *, phase: str, iteration: int | None, publish: bool,
                 repeat: int | None = None, sample: int | None = None) -> tuple[dict, list[int]]:
        ran: list[int] = []
        while True:
            attempt = self.journal.next_attempt("job", self._identity(planned, 1, phase, iteration, repeat,
                                                                      sample).job_key)
            ident = self._identity(planned, attempt, phase, iteration, repeat, sample)
            job_key = ident.job_key
            rec = _load_record(self.paths, job_key) or self._new_record(planned, ident)
            rec.update(phase=phase, iteration=iteration, repeat=repeat, sample=sample)
            prep = self._prepare(planned)
            if prep.problem is not None:
                rec["status"], rec["message"] = prep.problem
                if prep.problem[0] == "skipped_no_memory":
                    self.journal.record("job", job_key, "skipped", detail={"why": "no_memory"})
                self._save(rec)
                return rec, ran
            failures = self._candidate_failures(job_key, prep.candidate_sha256)
            final = any(a["detail"].get("outcome") in ("request_ceiling", "timeout") for a in failures)
            if len(failures) >= self.max_attempts or final:
                rec["status"] = "failed"
                self._save(rec)
                return rec, ran
            if self._infra_this_invocation(job_key) >= self.max_infra:
                raise CampaignStop(reason="infrastructure", detail=f"{job_key}: infrastructure attempts exhausted")
            self.halt_check()
            try:
                att = self._attempt(planned, ident, prep, rec)
            except _NotValidated:                       # no stored copy of the validated bytes: not run, failed
                rec["status"], rec["message"] = "failed", "validated candidate file not found"
                self._save(rec)
                return rec, ran
            ran.append(attempt)
            outcome, infra = att["outcome"], att["infra"]
            halt = self.ledger.halted()
            if halt is not None:
                raise CampaignStop(reason=halt.reason, detail=halt.detail)
            if att.get("_stop"):
                raise CampaignStop(reason="infrastructure", detail=att["_stop"])
            if outcome == "ok":
                break
            if not infra:
                if outcome in ("request_ceiling", "timeout") or len(failures) + 1 >= self.max_attempts:
                    break
                continue
            used = self._infra_this_invocation(job_key)
            if used >= self.max_infra:
                raise CampaignStop(reason="infrastructure", detail=f"{job_key}: infrastructure attempts exhausted")
            self.clock.sleep(min(60 * 2 ** (used - 1), 900))
        policy = util.lookup(self.manifest, "search.partial_candidate_policy")
        if publish and policy == "upstream":
            rec["_published_now"] = self._publish(planned, rec, include_result=rec["attempts"][-1]["outcome"] == "ok")
        return rec, ran

    def _new_record(self, planned: PlannedJob, ident: JobIdentity) -> dict:
        argv = list(planned.inner_argv)
        seed = int(argv[argv.index("--seed") + 1]) if "--seed" in argv[:-1] else None
        return {"schema": JOB_SCHEMA, "campaign": self.campaign, "job_key": ident.job_key, "split": planned.split,
                "candidate": planned.candidate, "dataset": planned.dataset, "phase": ident.phase,
                "iteration": ident.iteration, "repeat": ident.repeat, "sample": ident.sample, "seed": seed,
                "candidate_sha256": None, "route": util.lookup(self.manifest, "routing.route"),
                "executor": getattr(self.executor, "name", None),
                "isolated": bool(getattr(self.executor, "isolated", False)), "upstream_desc": planned.desc,
                "upstream_command": planned.upstream_command, "inner_argv": argv, "status": "failed", "attempts": [],
                "published": None, "result": None, "name_collision": False, "export_unit": None}

    def _build(self, planned: PlannedJob, prep: _Prep, tar: Path) -> dict:
        """Build the bundle; the attempt's candidate_sha256 is the hash of the bytes bundled. When the workspace file
        changed after _prepare, rebuild once from the stored copy with the expected hash; a test job without one
        refuses (it must run validated bytes), a validation job without one runs the bytes it bundled."""
        member = f"pkg/text_classification/agents/{planned.candidate}.py"
        others = {} if planned.candidate in workspace.PINNED_AGENTS else self._stored_others(planned.candidate)
        overrides = dict(others, **({planned.candidate: prep.override} if prep.override is not None else {}))
        info = self.bundle_builder(self.example_dir, self.paths.ws, planned.dataset, self.manifest, tar,
                                   agent_overrides=overrides or None)
        bundled = (info.get("members") or {}).get(member)
        if bundled is None or bundled == prep.candidate_sha256 or planned.candidate in workspace.PINNED_AGENTS:
            return info
        ref = self.candidate_reference(planned.candidate, prep.candidate_sha256) if self.candidate_reference else None
        if ref is not None and ref[1] == prep.candidate_sha256:
            prep.override, prep.restored = Path(ref[0]), True
            info = self.bundle_builder(self.example_dir, self.paths.ws, planned.dataset, self.manifest, tar,
                                       agent_overrides=dict(others, **{planned.candidate: prep.override}))
            if (info.get("members") or {}).get(member) == prep.candidate_sha256:
                return info
        if planned.split == "test":
            raise _NotValidated(f"candidate file of {planned.desc[:80]} changed while its bundle was built")
        prep.candidate_sha256 = bundled
        return info

    def _stored_others(self, own: str) -> dict[str, Path]:
        """The per-file rule for every other stored candidate in the bundle (3.18 "Test inputs", V26): a workspace
        file that differs from the latest stored copy of its name is bundled from that copy."""
        out: dict[str, Path] = {}
        if self.candidate_reference is None:
            return out
        for f in sorted((self.paths.ws / "agents").glob("*.py")):
            n = f.stem
            if n == own or n in workspace.PINNED_AGENTS or not CANDIDATE_RE.fullmatch(n):
                continue
            ref = self.candidate_reference(n, None)
            if ref is not None and ref[1] != workspace.candidate_sha256(self.paths, n):
                out[n] = Path(ref[0])
        return out

    def _attempt(self, planned: PlannedJob, ident: JobIdentity, prep: _Prep, rec: dict) -> dict:
        job_key, attempt = ident.job_key, ident.attempt
        attempt_dir = self.paths.attempt_dir(job_key, attempt)
        attempt_dir.mkdir(parents=True, exist_ok=True)
        self._verify_pinned()
        tar = attempt_dir / "bundle.tar"
        try:
            info = self._build(planned, prep, tar)
        except BaseException:
            if os.path.isfile(tar) and not os.path.islink(tar):
                os.unlink(tar)
            raise
        util.write_json(attempt_dir / "bundle.json", {"sha256": info["sha256"], "bytes": info["bytes"],
                                                      "members": info.get("members", {})})
        spec = JobSpec(identity=ident, inner_argv=tuple(planned.inner_argv),
                       route=int(util.lookup(self.manifest, "routing.route")),
                       timeout_s=int(util.lookup(self.manifest, "executor.job_timeout_s")),
                       inputs=tuple(prep.input_files), expect_outputs=(planned.result_name,),
                       capture=bool(util.lookup(self.manifest, "capture.enabled")), bundle_tar=tar,
                       bundle_sha256=info["sha256"], expect_freeze_sha256=self.expect_freeze_sha256())
        self.journal.record("job", job_key, "started", attempt=attempt,
                            detail={"executor": getattr(self.executor, "name", None), "phase": ident.phase,
                                    "candidate_sha256": prep.candidate_sha256, "inputs": dict(prep.inputs)})
        started = util.utc_now(self.clock)
        stop = None
        try:
            result = self.executor.run(spec, attempt_dir,
                                       lambda h: self.journal.add_handle("job", job_key, attempt, _clean(h)))
        except ExecutorError as exc:
            outcome = "env_setup_failed" if exc.kind == "env_setup" else "executor_error"
            result = JobResult(returncode=None, timed_out=False, exit_file=None, relay_end=None, kill_reason=None,
                               started_utc=started, ended_utc=util.utc_now(self.clock), outputs={}, gateway={},
                               refusal_codes={}, stderr_tail="", host_sleep_s=0.0, facts={"error_kind": exc.kind})
            classified = (outcome, True, None)
            stop = None if exc.retryable else exc.kind
        except BaseException:
            self.journal.record("job", job_key, "interrupted", attempt=attempt,
                                detail={"outcome": "interrupted", "infra": True, "failure_class": None})
            raise
        else:
            classified = classify_attempt(result, result_usable=_usable(attempt_dir / "out" / planned.result_name),
                                          halted=self.ledger.halted() is not None)
        finally:
            if os.path.isfile(tar) and not os.path.islink(tar):
                os.unlink(tar)                          # bundle.json keeps the member hashes; the tar is not kept
        outcome, infra, failure_class = classified
        if planned.candidate in workspace.PINNED_AGENTS and outcome != "ok" and outcome != "request_ceiling":
            infra = True                                # a baseline that cannot finish is a set-up problem
        att = self._attempt_entry(ident, prep, info, result, outcome, infra, failure_class, attempt_dir, planned)
        att["result_usable"] = _usable(attempt_dir / "out" / planned.result_name)
        rec["attempts"] = [a for a in rec.get("attempts", []) if a.get("attempt") != attempt] + [att]
        rec["candidate_sha256"] = prep.candidate_sha256
        rec["export_unit"] = f"job:{ident.job_id}"
        rec["status"] = "succeeded" if outcome == "ok" else ("halted" if outcome == "halted" else "failed")
        rec.pop("message", None)
        if outcome == "ok":
            rec["result"] = self._result_summary(attempt_dir / "out" / planned.result_name)
        self._save(rec)
        util.write_json(attempt_dir / "executor.json", _clean(dict(result.facts or {})))
        self.journal.record("job", job_key, "complete" if outcome == "ok" else "failed", attempt=attempt,
                            detail={"outcome": outcome, "infra": infra, "failure_class": failure_class,
                                    "result_sha256": att["outputs"].get(planned.result_name) if outcome == "ok" else None,
                                    "cost_micro": int((result.gateway or {}).get("cost_micro") or 0)})
        self.on_unit(f"job:{ident.job_id}")
        if stop is not None:
            att = dict(att, _stop=stop)
        return att

    def _attempt_entry(self, ident, prep, info, result: JobResult, outcome, infra, failure_class, attempt_dir,
                       planned) -> dict:
        ef = result.exit_file if isinstance(result.exit_file, Mapping) else None
        gateway = dict(_clean(result.gateway or {}))
        for name in ("usage_calls", "prompt_tokens_sum", "prompt_tokens_max", "completion_tokens_sum"):
            gateway.setdefault(name, 0)
        outputs = {}
        for name in bundle.OUTPUT_ALLOW:
            sha = _sha_file(attempt_dir / "out" / name)
            if sha is not None:
                outputs[name] = sha
        facts = result.facts or {}
        return {"attempt": ident.attempt, "job_id": ident.job_id, "started_utc": result.started_utc,
                "ended_utc": result.ended_utc, "candidate_sha256": prep.candidate_sha256,
                "candidate_restored": prep.restored, "inputs": dict(prep.inputs), "returncode": result.returncode,
                "timed_out": bool(result.timed_out), "kill_reason": result.kill_reason, "outcome": outcome,
                "infra": bool(infra), "failure_class": failure_class,
                "exit_file": {"present": ef is not None, "returncode": (ef or {}).get("returncode"),
                              "reason": (ef or {}).get("reason"),
                              "local_refusals": dict((ef or {}).get("local_refusals") or {}),
                              "unanswered_at_exit": (ef or {}).get("unanswered_at_exit", 0) if ef else 0},
                "refusal_codes": dict(result.refusal_codes or {}),
                "result_usable": False, "bundle_sha256": info["sha256"], "outputs": outputs,
                "gateway": gateway, "host_sleep_s": float(result.host_sleep_s or 0.0),
                "executor": _clean({k: facts[k] for k in _FACT_KEYS if k in facts})}

    @staticmethod
    def _result_summary(path: Path) -> dict | None:
        try:
            doc = util.read_json(path)
        except Exception:                               # the worker's file; any problem means no summary
            return None
        if not isinstance(doc, dict):
            return None
        return {k: doc.get(k) for k in ("accuracy", "correct", "total", "memory_context_chars")}

    # ------------------------------------------------------------ publication and launcher logs

    def _publish(self, planned: PlannedJob, rec: dict, *, include_result: bool) -> bool:
        if not rec.get("attempts"):
            return False
        att = rec["attempts"][-1]
        if rec.get("_att") is not None:                 # a reused attempt (journal-backed result)
            att = next((a for a in rec["attempts"] if a.get("attempt") == rec["_att"]), att)
        include = include_result and att.get("outcome") == "ok"
        out = self.paths.attempt_dir(rec["job_key"], att["attempt"]) / "out"
        copied = publish_outputs(out, planned, include_result=include)
        if not copied:
            return False
        pub = rec.get("published") or {"files": {}, "attempts": {}}
        for host, sha in copied.items():
            rel = _rel(self.paths, Path(host))
            pub.setdefault("files", {})[rel] = sha
            pub.setdefault("attempts", {})[rel] = att["attempt"]
        pub["at_utc"] = util.utc_now(self.clock)
        rec["published"] = pub
        self._save(rec)
        return True

    def _launcher_log(self, index: int, planned: PlannedJob, rec: dict, ran: list[int]) -> None:
        out_dir = Path(planned.host_outputs[planned.result_name]).parent
        in_dir = Path(planned.inputs[0][1]).parent if planned.inputs else out_dir
        attempts, path_map = [], {}
        by_number = {a.get("attempt"): a for a in rec.get("attempts", [])}
        for n in ran:
            attempt_dir = self.paths.attempt_dir(rec["job_key"], n)
            boxes = {str(attempt_dir / "box"), str(util.lookup(self.manifest, "sandbox.work_dir", "/mhf"))}
            try:
                run_dir = util.read_json(attempt_dir / "jobfile.json").get("run_dir")
                if isinstance(run_dir, str) and run_dir:
                    boxes.add(os.path.dirname(run_dir.rstrip("/")))
            except Exception:                           # no job file (fake or failed executor): the defaults only
                pass
            for box in boxes:
                path_map[f"{box}/out/"] = f"{out_dir}/"
                path_map[f"{box}/in/"] = f"{in_dir}/"
            text = b"".join(_read_regular(attempt_dir / "run" / f) or b"" for f in ("stdout.txt", "stderr.txt"))
            att = by_number.get(n, {})
            code = att.get("returncode")
            code = 124 if att.get("timed_out") else (code if isinstance(code, int) else 1)
            attempts.append((text, code))
        log = write_launcher_log(self.logs_dir, index, planned.desc, planned.upstream_command, attempts,
                                 path_map=path_map)
        if rec.get("published"):
            rec["published"]["launcher_log"] = _rel(self.paths, log)
            self._save(rec)

    # ------------------------------------------------------------ import checks

    def import_check(self, candidates: Sequence[str], *, iteration: int) -> dict[str, ImportResult]:
        """A probe import of a baseline, then one import check per candidate, in the executor. An executor that cannot
        start or a failed probe is an infrastructure attempt (retried, then CampaignStop); it rejects nobody."""
        key = f"iter{int(iteration):03d}"
        names = [str(c) for c in candidates]
        t = float(util.lookup(self.manifest, "executor.import_check_timeout_s"))
        failures = 0
        while True:
            self.halt_check()
            attempt = self.journal.next_attempt("import_check", key)
            work_root = self.paths.import_check_dir(int(iteration), attempt)
            work_root.mkdir(parents=True, exist_ok=True)
            self._verify_pinned()
            overrides = {}
            for name in names:
                ref = self.candidate_reference(name, None) if self.candidate_reference else None
                if ref is not None and ref[1] != workspace.candidate_sha256(self.paths, name):
                    overrides[name] = Path(ref[0])
            tar = work_root / "bundle.tar"
            dataset = list(self.facade.datasets)[0]
            info = self.bundle_builder(self.example_dir, self.paths.ws, dataset, self.manifest, tar,
                                       agent_overrides=overrides or None)
            self.journal.record("import_check", key, "started", attempt=attempt,
                                detail={"executor": getattr(self.executor, "name", None)})
            handles: list[dict] = []

            def on_handle(h: dict) -> None:
                handles.append(_clean(h))
                self.journal.add_handle("import_check", key, attempt, _clean(h))

            check = {"schema": CHECK_SCHEMA, "iteration": int(iteration), "attempt": attempt, "bundle_sha256":
                     info["sha256"], "names": [PROBE, *names], "handles": handles}
            problem, stop, results = None, None, {}
            try:
                results = self.executor.import_check((PROBE, *names), tar, work_root, on_handle,
                                                     timeouts_s=(4 * t, *([t] * len(names))))
            except ExecutorError as exc:
                problem = (exc.kind, "")
                stop = None if exc.retryable else exc.kind
            except BaseException:
                self.journal.record("import_check", key, "interrupted", attempt=attempt,
                                    detail={"kind": "interrupted", "stderr_tail": ""})
                raise
            finally:
                if os.path.isfile(tar) and not os.path.islink(tar):
                    os.unlink(tar)
            if problem is None:
                probe = results.get(PROBE)
                if probe is None or not probe.ok:
                    problem = ("probe", (probe.stderr_tail if probe else "")[-2000:])
                elif any(n not in results for n in names):
                    problem = ("missing_result", "")
            check["probe"] = _import_dict(results.get(PROBE)) if results.get(PROBE) else None
            check["results"] = {n: dict(_import_dict(results[n]), rejected=import_rejection(results[n]))
                                for n in names if n in results}
            check["infrastructure_failure"] = problem[0] if problem else None
            util.write_json(work_root / "check.json", _clean(check))
            if problem is None:
                self.journal.record("import_check", key, "complete", attempt=attempt,
                                    detail={"probe_ok": True, "results": {n: results[n].ok for n in names}})
                return {n: results[n] for n in names}
            self.journal.record("import_check", key, "failed", attempt=attempt,
                                detail={"kind": problem[0], "stderr_tail": _clean(problem[1])})
            failures += 1
            if stop is not None:
                raise CampaignStop(reason="infrastructure", detail=stop)
            since = self.journal.session_start_seq()
            used = sum(1 for a in self.journal.attempts("import_check", key)
                       if a["seq"] > since and a["status"] == "failed")
            if used >= self.max_infra:
                raise CampaignStop(reason="infrastructure", detail=f"import_check {key}: infrastructure attempts exhausted")
            self.clock.sleep(min(60 * 2 ** (used - 1), 900))


def _import_dict(r: ImportResult) -> dict:
    return {"ok": r.ok, "returncode": r.returncode, "timed_out": r.timed_out, "reason": r.reason,
            "stdout_tail": r.stdout_tail[-2000:], "stderr_tail": r.stderr_tail[-2000:]}
