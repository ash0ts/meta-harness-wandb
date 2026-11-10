# SPDX-License-Identifier: MIT
"""The one exporter step (spec 3.25): plan, scan, meter, emit, read back, receipt.

The exporter is the only code that sends anything, and it works after the fact, from files. It lists only finished
work (`discover`), builds every plan with the pure planners of layer 2, scans each plan and every file an Artifact would
upload (secret and privacy classes, known values and key digests), charges the export meters before the first byte is
sent, writes the receipt before the first SDK call, and reads every surface back before it calls anything verified.
A receipt in `emitting` or `emitted` is never emitted again; a changed plan is never emitted under an existing id.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from .. import journal as journal_mod
from .. import ledger as ledger_mod
from .. import manifest as manifest_mod
from .. import scan, util, wire
from ..errors import ExportError, ManifestError, MeterCapExceeded, PlanError, StateError, TranscriptError
from ..paths import StatePaths, unit_slug
from ..state import StateLock
from . import join, plan_runs, plan_spans, plan_tables, plan_study, policy, readback, transcript

__all__ = ["ExportUnit", "LensSink", "WandbSink", "Exporter", "final_statuses", "RECEIPT_SCHEMA", "SURFACES",
           "MAX_EXPORT_ATTEMPTS"]

RECEIPT_SCHEMA = "mhforge.receipt/1"
SCAN_SCHEMA = "mhforge.exportscan/1"
SURFACES = ("agent_lens", "wandb")
MAX_EXPORT_ATTEMPTS = 3
_FINAL = frozenset({"verified", "refused_secret", "skipped_budget", "skipped_policy", "partial", "failed", "dry_run"})
_IN_FLIGHT = ("emitting", "emitted")
_KIND_ORDER = {"job": 0, "proposer": 1, "propose": 2, "loop": 3, "eval": 4, "study": 5}
_JOB_ID_RE = re.compile(r"(.+)\.a([1-9][0-9]{0,5})")
_JOB_KEY_RE = re.compile(r"(?:qual\.)?(val|test)\.([a-z][a-z0-9_]{0,47})\.([A-Za-z][A-Za-z0-9_-]{0,47})"
                         r"(?:\.r[0-9]{1,4})?(?:\.s[0-9]{1,4})?")
_SESSION_RE = re.compile(r"iter([0-9]{3,6})\.s([1-9][0-9]{0,3})")
_ITER_RE = re.compile(r"iter([0-9]{3,6})")
_EVAL_RE = re.compile(r"(val|test)\.([a-z][a-z0-9_]{0,47})")
_TRACE_RE = re.compile(r"[0-9a-f]{32}")
_SOURCE_RE = re.compile(r"(?:workspace/text_classification/agents|proposer/iter-[0-9]{3,6}/s[1-9][0-9]{0,3}/candidates)"
                        r"/[a-z][a-z0-9_]{0,47}\.py")
_UNKNOWN_ADAPTER = {"name": None, "version": None, "unverified": True}


def final_statuses() -> frozenset[str]:
    """verified, refused_secret, skipped_budget, skipped_policy, partial, failed, dry_run (dry_run: dry runs only)."""
    return _FINAL


@dataclass(frozen=True)
class ExportUnit:
    unit: str
    kind: str                                        # job | proposer | loop | eval | propose
    surfaces: tuple[str, ...]                        # subset of ("agent_lens", "wandb"), in emit order
    depends_on: tuple[str, ...]                      # units whose receipts must be final first


class LensSink(Protocol):
    def emit(self, plan: Mapping, on_trace: Callable[[str, str], None]) -> None: ...
    def read_back(self, project: str, trace_ids: Sequence[str]) -> list[dict]: ...


class WandbSink(Protocol):
    def feature_ces(self) -> bool: ...
    def project_access(self, entity: str, project: str) -> dict | None: ...
    def log_run(self, run_plan: Mapping, table_plans: Sequence[Mapping], files_root: Path) -> dict: ...
    def confirm(self, run_path: str, table_keys: Sequence[str], artifact_names: Sequence[str]) -> list[str]: ...


@dataclass
class _Built:
    sha: str | None
    spans: dict | None = None
    runs: dict | None = None
    tables: dict | None = None
    tables_sent: list | None = None
    skipped_tables: list | None = None
    withheld: list | None = None
    upload: list | None = None                        # [(absolute path, sha256, bytes)]


def _sha(obj: object) -> str:
    try:
        data = util.canonical_json(obj)
    except (TypeError, ValueError):
        data = json.dumps(obj, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


class Exporter:
    def __init__(self, manifest: dict, paths: StatePaths, *, ledger: "ledger_mod.Ledger | None", lens: LensSink | None,
                 wandb: WandbSink | None, known_secrets: Sequence[bytes] = (), hostname: str = "",
                 example_dir: Path | None = None, amendment: Mapping | None = None, dry_run: bool = False,
                 clock: util.Clock | None = None) -> None:
        problems = manifest_mod.validate(manifest, Path(paths.root).parent.parent / "manifest.json")
        if problems:
            raise ManifestError(problems=problems)
        if ledger is None and not dry_run:
            raise ValueError("a live export needs the ledger (its meters are the only ingest guard)")
        self.manifest, self.paths, self.ledger = manifest, paths, ledger
        self.lens, self.wandb = lens, wandb
        self.known = tuple(bytes(k) if isinstance(k, (bytes, bytearray)) else str(k).encode("utf-8")
                           for k in known_secrets)
        self.hostname = hostname if isinstance(hostname, str) else ""
        self.example_dir = Path(example_dir) if example_dir is not None else None
        self.amendment = ({"seq": int(amendment.get("seq") or 0), "row_sha256": amendment.get("row_sha256")}
                          if amendment else {"seq": 0, "row_sha256": None})
        self.dry_run = bool(dry_run)
        self.clock = clock or util.SystemClock()
        self.campaign = manifest["campaign"]
        self._index: object = self
        self._project_ok = False
        self._ces: bool | None = None
        self._lock_depth = 0

    # ------------------------------------------------------------ verbatim index (3.24)

    def verbatim_index(self) -> "scan.VerbatimIndex | None":
        """Built once from example_dir's pinned data files (hash-checked against pristine.json); None without one."""
        if self._index is not self:
            return self._index                                       # type: ignore[return-value]
        if self.example_dir is None:
            self._index = None
            return None
        try:
            pristine = util.read_json(self.paths.pristine)
        except (FileNotFoundError, StateError):
            raise ExportError("workspace/pristine.json is missing or unreadable: the data files cannot be checked") from None
        files = pristine.get("files") if isinstance(pristine, dict) else None
        if not isinstance(files, dict):
            raise ExportError("workspace/pristine.json has no file hashes")
        exact = set(util.lookup(self.manifest, "telemetry.verbatim_scan.exact_datasets", []) or [])
        dirs = util.lookup(self.manifest, "upstream.expect.dataset_dirs", {}) or {}
        runs: list[str] = []
        exacts: list[str] = []
        for ds in util.lookup(self.manifest, "upstream.expect.datasets", []) or []:
            if all(r == "full" for r in policy.rules_for(self.manifest, ds).values()):
                continue                                             # a fully open dataset may be shown
            for split in ("train", "val", "test"):
                rel = f"data/{dirs.get(ds)}/{split}.jsonl"
                try:
                    data = (self.example_dir / rel).read_bytes()
                except OSError:
                    raise ExportError(f"{rel}: dataset file not readable") from None
                if files.get(rel) != util.sha256_bytes(data):
                    raise ExportError(f"{rel}: does not match its pinned hash in pristine.json")
                for line in data.decode("utf-8", "replace").splitlines():
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(row, dict):
                        continue
                    question = row.get("question")
                    target = row.get("target", row.get("answer"))
                    if ds in exact:
                        exacts += [t for t in (question, target) if isinstance(t, str)]
                    elif isinstance(question, str):
                        runs.append(question)
        self._index = scan.build_verbatim_index(
            runs, exacts, run_chars=int(util.lookup(self.manifest, "telemetry.verbatim_scan.run_chars", 40)))
        return self._index                                           # type: ignore[return-value]

    def _protected_datasets(self) -> bool:
        return any(any(r != "full" for r in policy.rules_for(self.manifest, ds).values())
                   for ds in util.lookup(self.manifest, "upstream.expect.datasets", []) or [])

    # ------------------------------------------------------------ records

    def _journal(self) -> list[dict]:
        return journal_mod.read_journal(self.paths.journal) if self.paths.journal.exists() else []

    @staticmethod
    def _doc(path: Path) -> dict | None:
        try:
            doc = util.read_json(path)
        except FileNotFoundError:
            return None
        return doc if isinstance(doc, dict) else None

    def _job_record(self, job_key: str) -> dict | None:
        return self._doc(self.paths.job_dir(job_key) / "job.json")

    def _calls(self, job_id: str) -> list[dict]:
        root = self.paths.calls_dir(job_id)
        out = []
        for p in sorted(root.glob("*.json")) if root.is_dir() else ():
            doc = self._doc(p)
            if doc is not None:
                out.append(doc)
        return out

    def _out_dir(self, job_key: str, attempt: int) -> Path:
        return self.paths.attempt_dir(job_key, attempt) / "out"

    def _sessions(self) -> list[tuple[int, int, Path]]:
        out = []
        for d in sorted(self.paths.proposer_root.glob("iter-*/s*")) if self.paths.proposer_root.is_dir() else ():
            m = re.fullmatch(r"iter-([0-9]{3,6})", d.parent.name)
            n = re.fullmatch(r"s([1-9][0-9]{0,3})", d.name)
            if m and n and d.is_dir() and not d.is_symlink():
                out.append((int(m.group(1)), int(n.group(1)), d))
        return sorted(out)

    def _proposal_for(self, candidate: str) -> tuple[dict, dict, Path] | None:
        """(candidate entry, proposal record, session dir) of the latest proposal that lists the candidate."""
        found = None
        for _it, _k, d in self._sessions():
            prop = self._doc(d / "proposal.json")
            for entry in (prop or {}).get("candidates") or ():
                if isinstance(entry, dict) and entry.get("name") == candidate:
                    found = (dict(entry), prop, d)
        return found

    def _iterations(self) -> list[dict]:
        root = self.paths.iterations_root
        out = [self._doc(p) for p in sorted(root.glob("iter-*.json"))] if root.is_dir() else []
        return [r for r in out if r is not None and _is_int(r.get("iteration"))]

    @staticmethod
    def _ok_attempt(record: Mapping) -> tuple[str, int] | None:
        attempts = [a for a in record.get("attempts") or () if isinstance(a, Mapping) and _is_int(a.get("attempt"))]
        ok = [a for a in attempts if a.get("outcome") == "ok"]
        chosen = (ok or attempts or [None])[-1]
        if chosen is None:
            return None
        return str(chosen.get("job_id") or f"{record.get('job_key')}.a{chosen['attempt']}"), chosen["attempt"]

    # ------------------------------------------------------------ discover

    def discover(self) -> list[ExportUnit]:
        """Finished things only, from the journal and the state directory, in dependency order."""
        entries = self._journal()
        units: dict[str, ExportUnit] = {}

        def add(u: ExportUnit) -> None:
            units.setdefault(u.unit, u)

        for e in entries:
            if e.get("op") == "job" and e.get("status") in ("complete", "failed", "interrupted") \
                    and _is_int(e.get("attempt")) and e["attempt"] >= 1 and isinstance(e.get("key"), str):
                add(ExportUnit(f"job:{e['key']}.a{e['attempt']}", "job", ("agent_lens",), ()))
        for _it, _k, d in self._sessions():
            rec = self._doc(d / "session.json")
            key = (rec or {}).get("session_key")
            if isinstance(key, str) and _SESSION_RE.fullmatch(key):
                add(ExportUnit(f"proposer:{key}", "proposer", ("agent_lens",), ()))
            for entry in (self._doc(d / "proposal.json") or {}).get("candidates") or ():
                name = entry.get("name") if isinstance(entry, dict) else None
                if isinstance(name, str) and wire.CANDIDATE_RE.fullmatch(name):
                    add(ExportUnit(f"propose:{name}", "propose", ("wandb",), ()))
        for e in entries:
            if e.get("op") == "iteration" and e.get("status") == "settled" and isinstance(e.get("key"), str) \
                    and _ITER_RE.fullmatch(e["key"]):
                add(ExportUnit(f"loop:{e['key']}", "loop", ("agent_lens", "wandb"), ()))
        baselines = set(util.lookup(self.manifest, "upstream.expect.baselines", []) or [])
        eval_keys = {str(e.get("key")) for e in entries
                     if e.get("op") == "benchmark" and e.get("status") == "complete"}
        # Calibration is deliberately unpublished to the upstream proposer
        # workspace, but its private Forge evidence still needs evaluation runs.
        if any(e.get("op") == "calibration" and e.get("status") == "complete" for e in entries):
            calibration = self._doc(self.paths.calibration) or {}
            eval_keys.update(f"test.{name}" for name, result in calibration.get("systems", {}).items()
                             if name in baselines and result.get("complete") is True)
        for eval_key in sorted(eval_keys):
            m = _EVAL_RE.fullmatch(eval_key)
            if not m:
                continue
            split, candidate = m.groups()
            deps = []
            for ds in util.lookup(self.manifest, "upstream.expect.datasets", []) or []:
                rec = self._job_record(f"{split}.{candidate}.{ds}")
                ok = self._ok_attempt(rec) if rec and rec.get("status") == "succeeded" else None
                if ok and f"job:{ok[0]}" in units:
                    deps.append(f"job:{ok[0]}")
            if candidate not in baselines and f"propose:{candidate}" in units:
                deps.append(f"propose:{candidate}")
            add(ExportUnit(f"eval:{split}.{candidate}", "eval", ("wandb",), tuple(deps)))
        for op, name, path in (("noise", "noise", self.paths.noise),
                              ("controls", "vote5", self.paths.root / "controls/result.json")):
            if path.exists() and any(e.get("op") == op and e.get("status") == "complete" for e in entries):
                deps = tuple(u.unit for u in units.values() if u.kind == "job")
                add(ExportUnit(f"study:{name}", "study", ("wandb",), deps))
        return sorted(units.values(), key=lambda u: (_KIND_ORDER[u.kind], u.unit))

    # ------------------------------------------------------------ planning

    def _project(self) -> tuple[str | None, str | None]:
        return util.lookup(self.manifest, "decisions.entity", None), util.lookup(self.manifest, "decisions.project", None)

    def _lens_url(self, job_id: str) -> str | None:
        rec = self._doc(self.paths.receipt_file(f"job:{job_id}", "agent_lens"))
        if rec and rec.get("status") == "verified" and rec.get("urls"):
            return rec["urls"][0]
        return None

    def _candidate_file(self, path: Path) -> tuple[str, int, bool]:
        """(sha256, bytes, withheld) of a candidate file; withheld when the verbatim scan finds dataset text, or when
        there is no index to scan with while some dataset is protected (fail closed)."""
        if path.is_symlink() or not path.is_file():
            raise PlanError(problems=[f"candidate file {path.name} is missing or not a regular file"])
        data = path.read_bytes()
        index = self.verbatim_index()
        if index is None:
            withheld = self._protected_datasets()
        else:
            withheld = bool(scan.scan_verbatim(data.decode("utf-8", "replace"), index, path=path.name))
        return util.sha256_bytes(data), len(data), withheld

    def _plan_lens(self, u: ExportUnit, k: int) -> dict:
        m = self.manifest
        if u.kind == "job":
            jm = _JOB_ID_RE.fullmatch(u.unit[4:])
            if not jm:
                raise PlanError(problems=[f"bad job unit {u.unit[:80]!r}"])
            job_key, attempt = jm.group(1), int(jm.group(2))
            record = self._job_record(job_key)
            if record is None:                                       # the runner died before it wrote the record
                km = _JOB_KEY_RE.fullmatch(job_key)
                split, candidate, dataset = km.groups() if km else (None, None, None)
                record = {"schema": "mhforge.job/1", "campaign": self.campaign, "job_key": job_key, "split": split,
                          "candidate": candidate, "dataset": dataset, "attempts": []}
            run_path = None
            entity, project = self._project()
            split, cand = record.get("split"), record.get("candidate")
            if entity and project and split in wire.SPLITS and isinstance(cand, str) and wire.CANDIDATE_RE.fullmatch(cand):
                run_path = f"{entity}/{project}/{plan_runs.run_id(self.campaign, 'evaluate', candidate=cand, split=split)}"
            sidecar = util.read_jsonl(self._out_dir(job_key, attempt) / "sidecar.jsonl")
            plan = plan_spans.plan_solver_job(record, attempt, self._calls(u.unit[4:]), sidecar, m, run_path=run_path,
                                              export_attempt=k)
        elif u.kind == "proposer":
            sm = _SESSION_RE.fullmatch(u.unit[9:])
            sdir = self.paths.session_dir(int(sm.group(1)), int(sm.group(2))) if sm else None
            record = self._doc(sdir / "session.json") if sdir else None
            if record is None:
                raise PlanError(problems=[f"{u.unit}: session.json missing"])
            roots = {"state": self.paths.root, "workspace": self.paths.ws}
            if self.example_dir is not None:
                roots["example"] = self.example_dir
            if isinstance(record.get("scratch_dir"), str) and record["scratch_dir"].startswith("/"):
                roots["scratch"] = Path(record["scratch_dir"])
            plan = plan_spans.plan_proposer(record, self._session(sdir, record), m, roots=roots,
                                            verbatim=self.verbatim_index(), export_attempt=k)
        else:
            nnn = int(u.unit[9:])
            rec = next((r for r in self._iterations() if r["iteration"] == nnn), None)
            if rec is None:
                raise PlanError(problems=[f"{u.unit}: iteration record missing"])
            plan = plan_spans.plan_outer_iteration(rec, self._journal(), m, verbatim=self.verbatim_index(),
                                                   export_attempt=k)
        problems = plan_spans.validate_plan(plan, m)
        if not self.dry_run and plan_spans.VERBATIM_INDEX_MISSING in (plan.get("problems") or ()):
            problems.append("verbatim index not built: a live export needs example_dir while a proposer rule is open")
        if problems:
            raise PlanError(problems=problems)
        return plan

    def _session(self, sdir: Path, record: Mapping) -> "transcript.NormalizedSession":
        sid = record.get("session_id")
        if isinstance(sid, str) and sid:
            try:
                records, subs = transcript.read_session_dir(
                    sdir / "transcript", sid, max_bytes=int(util.lookup(self.manifest, "proposer.max_transcript_bytes")))
                return transcript.normalize_transcript(
                    records, expect_version=util.lookup(self.manifest, "proposer.cli_version", None), subagents=subs)
            except (FileNotFoundError, ValueError):
                pass                                                 # no transcript copy: the stamped stream instead
        return transcript.normalize_stream(util.read_jsonl(sdir / "stream.jsonl"))

    def _plan_wandb(self, u: ExportUnit, k: int) -> _Built:
        m = self.manifest
        tables_doc, sent, skipped = None, [], []
        withheld: dict[str, dict] = {}
        if u.kind == "study":
            runs, tables_doc = plan_study.plan(u.unit[6:], self.paths, m, self._lens_url, export_attempt=k)
            sent = tables_doc["tables"]
        elif u.kind == "propose":
            cand = u.unit[8:]
            found = self._proposal_for(cand)
            if found is None:
                raise PlanError(problems=[f"{u.unit}: no proposal lists the candidate"])
            entry, prop, sdir = found
            if not self.dry_run and self.verbatim_index() is None and self._protected_datasets():
                raise PlanError(problems=["verbatim index not built: a live export needs example_dir to scan the "
                                          "proposer's Artifact metadata (hypothesis, axis, components)"])
            sha, nbytes, held = self._candidate_file(sdir / "candidates" / f"{cand}.py")
            entry["bytes"] = nbytes
            if held:
                withheld[f"{cand}.py"] = {"sha256": sha, "bytes": nbytes}
            session = self._doc(sdir / "session.json") or {}
            session = {**{"iteration": prop.get("iteration"), "session_key": prop.get("session_key"),
                          "ordinal": int(sdir.name[1:])}, **session}
            runs = plan_runs.plan_propose_run(entry, session, m, withheld=withheld, verbatim=self.verbatim_index(),
                                              export_attempt=k)
        elif u.kind == "eval":
            split, cand = _EVAL_RE.fullmatch(u.unit[5:]).groups()   # type: ignore[union-attr]
            datasets = list(util.lookup(m, "upstream.expect.datasets", []) or [])
            records, examples, results, evidence, urls = {}, {}, {}, {}, {}
            for ds in datasets:
                rec = self._job_record(f"{split}.{cand}.{ds}")
                if rec is None:
                    continue
                records[ds] = rec
                ok = self._ok_attempt(rec)
                if ok is None:
                    continue
                job_id, attempt = ok
                out = self._out_dir(f"{split}.{cand}.{ds}", attempt)
                evidence[ds] = {}
                for name, stem in (("memory.json", "memory_json"), ("log.jsonl", "log_jsonl")):
                    p = out / name
                    if p.is_file() and not p.is_symlink():
                        evidence[ds][f"{stem}_sha256"], evidence[ds][f"{stem}_bytes"] = util.sha256_file(p), p.stat().st_size
                if rec.get("status") != "succeeded":
                    continue
                calls = self._calls(job_id)
                rows = util.read_jsonl(out / "sidecar.jsonl")
                examples[ds] = join.join_examples(join.parse_sidecar(rows), calls, dataset=ds, split=split) if rows else []
                results[ds] = rec.get("result") if isinstance(rec.get("result"), Mapping) else {}
                url = self._lens_url(job_id)
                if url:
                    urls[ds] = url
            tables_doc = plan_tables.plan_tables(cand, split, records, examples, results, m, span_urls=urls,
                                                 backend=plan_tables.table_backend(m), export_attempt=k)
            skipped = [dict(d) for d in tables_doc.get("dropped") or ()]
            for t in tables_doc.get("tables") or ():
                failed = [n for n, ok in plan_tables.check_table(t).items() if ok is not True]
                if failed:                                           # a table whose check failed is never logged
                    skipped.append({"key": t.get("key"), "job_id": t.get("job_id"), "reason": "failed_checks",
                                    "failed_checks": failed})
                else:
                    sent.append(t)
            baseline = cand in (util.lookup(m, "upstream.expect.baselines", []) or [])
            entry = None
            if baseline:
                sha, nbytes, held = self._candidate_file(self.paths.ws / "agents" / f"{cand}.py")
                entry = {"sha256": sha, "bytes": nbytes}
                if held:
                    withheld[f"{cand}.py"] = {"sha256": sha, "bytes": nbytes}
            else:
                found = self._proposal_for(cand)
                entry = found[0] if found else None
            runs = plan_runs.plan_evaluate_run(cand, split, records, [t["key"] for t in sent], m, proposal_entry=entry,
                                               conversation_urls=urls, is_baseline=baseline, evidence=evidence,
                                               withheld=withheld, verbatim=self.verbatim_index(), export_attempt=k)
        else:
            nnn = int(u.unit[9:])
            recs = self._iterations()
            rec = next((r for r in recs if r["iteration"] == nnn), None)
            if rec is None:
                raise PlanError(problems=[f"{u.unit}: iteration record missing"])
            runs = plan_runs.plan_search_step(rec, [r for r in recs if r["iteration"] < nnn], m, export_attempt=k)
        upload = []
        for art in runs["run"].get("artifacts") or ():
            for f in art.get("files") or ():
                src = f.get("source")
                if not isinstance(src, str) or not _SOURCE_RE.fullmatch(src):
                    raise PlanError(problems=[f"run plan names a file that is not a candidate file: {str(src)[:80]!r}"])
                path = self.paths.root / src
                sha, nbytes, _held = self._candidate_file(path)
                if sha != f.get("sha256"):
                    raise PlanError(problems=[f"{src}: changed since it was planned"])
                upload.append((path, sha, nbytes))
        doc = {"runs": runs, "tables": tables_doc}
        return _Built(sha=_sha(doc), runs=runs, tables=tables_doc, tables_sent=sent, skipped_tables=skipped,
                      withheld=[{"name": n, **v} for n, v in sorted(withheld.items())], upload=upload)

    def _build(self, u: ExportUnit, surface: str, k: int) -> _Built:
        if surface == "agent_lens":
            plan = self._plan_lens(u, k)
            return _Built(sha=_sha(plan), spans=plan, upload=[], withheld=[])
        return self._plan_wandb(u, k)

    def _write_plans(self, u: ExportUnit, b: _Built) -> None:
        d = self.paths.plan_dir(u.unit)
        d.mkdir(parents=True, exist_ok=True)
        if b.spans is not None:
            util.write_json(d / "spans.json", b.spans)
        if b.runs is not None:
            util.write_json(d / "runs.json", b.runs)
        if b.tables is not None:
            util.write_json(d / "tables.json", b.tables)

    # ------------------------------------------------------------ receipts

    def _now(self) -> str:
        return util.utc_iso(self.clock.now())

    def _receipt(self, u: ExportUnit, surface: str, k: int) -> dict:
        sink = self.lens if surface == "agent_lens" else self.wandb
        adapter = getattr(sink, "adapter", None)
        now = self._now()
        return {"schema": RECEIPT_SCHEMA, "unit": u.unit, "surface": surface, "export_attempt": k, "status": None,
                "plan_sha256": None, "stale_plan_sha256": None, "scan": None, "amendment": dict(self.amendment),
                "bucket": None, "withheld_files": [], "skipped_tables": [], "conversation_ids": [], "trace_ids": [],
                "traces": {}, "expected_spans": None, "readback": None,
                "remote": {"run_path": None, "table_keys": [], "artifact_names": [], "backend": None, "base_url": None},
                "urls": [], "planned_bytes": None, "planned_spans": None,
                "adapter": dict(adapter) if isinstance(adapter, Mapping) else dict(_UNKNOWN_ADAPTER),
                "problems": [], "created_utc": now, "updated_utc": now, "error": None, "history": []}

    def _save(self, r: dict, status: str | None = None) -> dict:
        if status is not None and status != r.get("status"):
            r["status"] = status
            r["history"].append({"status": status, "utc": self._now()})
        r["updated_utc"] = self._now()
        path = self.paths.receipt_file(r["unit"], r["surface"])
        path.parent.mkdir(parents=True, exist_ok=True)
        util.write_json(path, r)
        return r

    def _archive(self, r: Mapping) -> None:
        root = self.paths.export_root / "attempts"
        root.mkdir(parents=True, exist_ok=True)
        base = f"{unit_slug(r['unit'])}.{r['surface']}.e{r.get('export_attempt') or 1}"
        path, n = root / f"{base}.json", 1
        while path.exists():                                         # superseded receipts are never overwritten
            n += 1
            path = root / f"{base}.{n}.json"
        util.write_json(path, dict(r))

    def _error(self, exc: BaseException) -> str:
        whole = " ".join(str(exc).split())             # scanned before the cut, so no part of a secret survives it
        try:
            digests = scan.load_digests(self.paths.keydigests)
        except (StateError, OSError):
            digests = []
        text = whole[:300]
        if scan.scan_bytes(whole.encode("utf-8"), known=self.known, digests=digests):
            text = "<message withheld: it matched a secret pattern>"
        return f"{type(exc).__name__}: {text}" if text else type(exc).__name__

    # ------------------------------------------------------------ one unit

    @contextlib.contextmanager
    def _locked(self):
        if self._lock_depth:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        with StateLock(self.paths.export_lock, "export", self.clock):
            self._lock_depth = 1
            try:
                yield
            finally:
                self._lock_depth = 0

    def export_unit(self, unit: ExportUnit, *, retry_partial: bool = False, supersede: bool = False) -> dict[str, dict]:
        with self._locked():
            return {s: self._surface(unit, s, retry_partial, supersede) for s in unit.surfaces}

    def _surface(self, u: ExportUnit, surface: str, retry_partial: bool, supersede: bool) -> dict:
        old = self._doc(self.paths.receipt_file(u.unit, surface))
        if old is not None and old.get("status") in _IN_FLIGHT:
            return old if self.dry_run else self._read_back(old, None)   # never emitted twice: read back
        k = int(old.get("export_attempt") or 1) if old else 1
        if old is not None and old.get("status") in _FINAL:
            if old["status"] == "dry_run" and not self.dry_run:
                self._archive(old)                                   # nothing was sent: attempt 1, plain ids
                return self._attempt(u, surface, 1, None)
            try:
                built = self._build(u, surface, k)
            except (PlanError, StateError, TranscriptError, ValueError, OSError):
                built = _Built(sha=None)
            stale = built.sha is not None and built.sha != old.get("plan_sha256")
            # Eventual consistency can repair a partial readback without another
            # upload. Re-read the same immutable plan before spending a retry.
            if old["status"] == "partial" and built.sha == old.get("plan_sha256") and not self.dry_run:
                old = self._read_back(old, None)
                if old["status"] == "verified":
                    return old
            restart = (retry_partial and old["status"] in ("partial", "failed")) or (supersede and stale)
            if not restart or self.dry_run or k >= MAX_EXPORT_ATTEMPTS:
                want = built.sha if stale else None
                if old.get("stale_plan_sha256") != want:
                    old["stale_plan_sha256"] = want
                    self._save(old)
                return old
            self._archive(old)
            return self._attempt(u, surface, k + 1, None)
        return self._attempt(u, surface, k, old)

    def _off(self, surface: str) -> str | None:
        if surface == "agent_lens":
            if not util.lookup(self.manifest, "telemetry.agent_lens.enabled", True):
                return "telemetry.agent_lens.enabled is false"
            if self.lens is None and not self.dry_run:
                return "no Agent Lens sink"
        elif self.wandb is None and not self.dry_run:
            return "no W&B sink"
        return None

    def _attempt(self, u: ExportUnit, surface: str, k: int, previous: Mapping | None) -> dict:
        r = self._receipt(u, surface, k)
        if previous is not None and previous.get("export_attempt") == k:
            r["created_utc"], r["history"] = previous.get("created_utc", r["created_utc"]), list(previous.get("history") or [])
        off = self._off(surface)
        if off:
            r["error"] = off
            return self._save(r, "skipped_policy")
        # 2. plan
        try:
            b = self._build(u, surface, k)
        except PlanError as exc:
            r["problems"], r["error"] = list(exc.problems)[:50], self._error(exc)
            return self._save(r, "failed")
        except (StateError, TranscriptError, ValueError, OSError) as exc:
            r["error"] = self._error(exc)
            return self._save(r, "failed")
        self._write_plans(u, b)
        r["plan_sha256"], r["withheld_files"] = b.sha, list(b.withheld or [])
        if b.spans is not None:
            plan = b.spans
            r["conversation_ids"] = [c["conversation_id"] for c in plan["conversations"]]
            r["expected_spans"] = r["planned_spans"] = plan["expected_spans"]
            r["planned_bytes"] = plan["planned_bytes"]
            r["problems"] = list(plan.get("problems") or [])[:50]
            placeholder = plan.get("placeholder_project")
        else:
            r["skipped_tables"] = b.skipped_tables or []
            r["remote"]["table_keys"] = [t["key"] for t in b.tables_sent or ()]
            r["remote"]["artifact_names"] = list(b.runs["expect"]["artifacts"])
            r["planned_bytes"] = self._upload_bytes(b)
            placeholder = b.runs.get("placeholder_project") or (b.tables or {}).get("placeholder_project")
        self._save(r, "planned")
        if not self.dry_run:
            unset = b.tables is not None and b.tables.get("backend") == "unset"
            if placeholder or unset:
                r["error"] = "placeholder project" if placeholder else "decisions.evaltables_backend is unset"
                return self._save(r, "skipped_policy")
        # 3. scan
        report = self._scan(u, surface, b)
        r["scan"] = {k2: report[k2] for k2 in ("clean", "findings", "scanned_bytes", "files", "rules", "classes")}
        if not report["clean"]:
            return self._save(r, "refused_secret")
        self._save(r, "scanned")
        if self.dry_run:
            return self._save(r, "dry_run")
        # 4. meter (every check before the first byte is sent)
        self._check_project()
        if not self._meter(u, surface, k, b, r):
            return self._save(r, "skipped_budget")
        # 5. emit
        return self._emit(r, b)

    # ------------------------------------------------------------ scan, project, meter

    def _scan(self, u: ExportUnit, surface: str, b: _Built) -> dict:
        kw = {"known": self.known, "digests": scan.load_digests(self.paths.keydigests), "classes": ("secret", "privacy"),
              "hostnames": (self.hostname,) if self.hostname else ()}
        docs = {"spans": b.spans} if b.spans is not None else {"runs": b.runs, "tables": b.tables}
        findings = list(scan.scan_obj(docs, **kw))
        findings += scan.scan_paths([p for p, _s, _n in b.upload or ()], **kw)
        report = {"schema": SCAN_SCHEMA, "unit": u.unit, "surface": surface, "utc": self._now(),
                  "clean": not findings, "findings": len(findings),
                  "detail": [{"pattern": f.pattern, "path": policy.portable(f.path, {"state": self.paths.root}),
                              "offset": f.offset, "length": f.length} for f in findings[:200]],
                  "scanned_bytes": len(json.dumps(docs, default=str)) + sum(n for _p, _s, n in b.upload or ()),
                  "files": len(docs) + len(b.upload or ()), "rules": len(scan.ALL_RULES), "classes": ["secret", "privacy"]}
        path = self.paths.scan_file(u.unit)
        path.parent.mkdir(parents=True, exist_ok=True)
        whole = self._doc(path) or {"schema": SCAN_SCHEMA, "unit": u.unit, "surfaces": {}}
        whole.setdefault("surfaces", {})[surface] = report
        util.write_json(path, whole)
        return report

    def _check_project(self) -> None:
        """A live export never creates a project: the project must exist, be writable and be PRIVATE until publication
        was approved. Checked once, before anything is metered or sent on any surface."""
        if self._project_ok:
            return
        entity, project = self._project()
        if self.wandb is None:
            raise ExportError("no W&B sink to check the project's access with; a live export never creates a project")
        info = self.wandb.project_access(str(entity), str(project))
        if info is None:
            raise ExportError(f"project {entity}/{project} does not exist: create it PRIVATE with account-check "
                              "--create-project")
        if info.get("read_only") or info.get("can_write") is False:
            raise ExportError(f"project {entity}/{project} is read-only for this key")
        if util.lookup(self.manifest, "decisions.publish_approved", False) is not True and \
                str(info.get("access") or "").upper() != "PRIVATE":
            raise ExportError(f"project {entity}/{project} is not PRIVATE and publication is not approved")
        self._project_ok = True

    def _metered(self, meter: str, key: str) -> bool:
        with ledger_mod.transaction(self.ledger.path) as db:                 # type: ignore[union-attr]
            return db.execute("SELECT 1 FROM meter_events WHERE meter=? AND key=?", (meter, key)).fetchone() is not None

    @staticmethod
    def _upload_bytes(b: _Built) -> int:
        return sum(n for _p, _s, n in b.upload or ()) + sum(len(json.dumps(t["rows"])) for t in b.tables_sent or ())

    def _meter(self, u: ExportUnit, surface: str, k: int, b: _Built, r: dict) -> bool:
        led, key = self.ledger, f"{u.unit}#e{k}"
        try:
            if surface == "agent_lens":
                kind = util.lookup(self.manifest, "campaign_kind", "measured")
                bucket = "recovery" if k >= 2 else ("preflight" if kind == "preflight" else "main")
                r["bucket"] = bucket
                planned, spans = int(b.spans["planned_bytes"]), int(b.spans["expected_spans"])
                cap = int(util.lookup(self.manifest, "budgets.lens_proposer_session_mb")) * ledger_mod.MIB
                if u.kind == "proposer" and planned > cap:
                    r["error"] = f"planned bytes exceed budgets.lens_proposer_session_mb ({planned} > {cap})"
                    return False
                if not self._metered("lens_bytes", key):
                    led.check_meter_cap("lens_bytes", extra_quantity=planned, bucket=bucket)
                    led.meter("lens_bytes", key, planned, "bytes", bucket=bucket)
                if not self._metered("lens_spans", key):
                    led.meter("lens_spans", key, spans, "spans", bucket=bucket)
            else:
                upload = self._upload_bytes(b)
                todo = [t["key"] for t in b.tables_sent or () if not self._metered("eval_tables", f"{key}#{t['key']}")]
                if not self._metered("wandb_upload_bytes", key):
                    led.check_meter_cap("wandb_upload_bytes", extra_quantity=upload)
                if todo:
                    led.check_meter_cap("eval_tables", extra_quantity=len(todo))
                led.meter("wandb_upload_bytes", key, upload, "bytes")
                for tkey in todo:
                    led.meter("eval_tables", f"{key}#{tkey}", 1, "tables")
        except MeterCapExceeded as exc:
            r["error"] = self._error(exc)
            return False
        return True

    # ------------------------------------------------------------ emit and read back

    def _emit(self, r: dict, b: _Built) -> dict:
        if r["surface"] == "agent_lens":
            project = b.spans["project"]
            r["urls"] = [plan_spans.conversation_url(project, c) for c in r["conversation_ids"]]
            self._save(r, "emitting")                                # before the first SDK call

            def on_trace(conversation_id: str, trace_id: str) -> None:
                if not isinstance(trace_id, str) or not _TRACE_RE.fullmatch(trace_id):
                    raise ExportError("the sink reported a malformed trace id")
                if trace_id not in r["trace_ids"]:
                    r["trace_ids"].append(trace_id)
                r["traces"].setdefault(str(conversation_id), [])
                if trace_id not in r["traces"][str(conversation_id)]:
                    r["traces"][str(conversation_id)].append(trace_id)
                self._save(r)                                        # before any span of the trace ends

            try:
                self.lens.emit(b.spans, on_trace)                    # type: ignore[union-attr]
            except Exception as exc:                                 # noqa: BLE001 - the receipt says what happened
                r["error"] = self._error(exc)
                if not r["trace_ids"]:
                    return self._save(r, "failed")                   # no trace was started: no span can have ended
                return self._read_back(r, b.spans)                   # something may be stored: read it back
            self._save(r, "emitted")
            return self._read_back(r, b.spans)
        run = b.runs["run"]
        r["remote"]["run_path"] = f"{run['entity']}/{run['project']}/{run['id']}"
        tables = list(b.tables_sent or ())
        backend = (b.tables or {}).get("backend")
        r["remote"]["backend"] = backend if tables else None
        if tables and backend == "ces" and not self._feature_ces():
            tables = [{**t, "backend": "wandb_table"} for t in tables]
            r["remote"]["backend"] = "wandb_table"
            r["problems"].append("ces_unavailable: tables logged with backend wandb_table")
        self._save(r, "emitting")                                    # before the first SDK call
        try:
            result = self.wandb.log_run(b.runs, tables, self.paths.root)     # type: ignore[union-attr]
        except ExportError as exc:                                   # the adapter refused before any SDK call
            r["error"] = self._error(exc)
            return self._save(r, "failed")
        except Exception as exc:                                     # noqa: BLE001 - unknown: confirm what exists
            r["error"] = self._error(exc)
            return self._read_back(r, None)
        result = result if isinstance(result, Mapping) else {}
        rem = r["remote"]
        rem["run_path"] = result.get("run_path") or rem["run_path"]
        if isinstance(result.get("table_keys"), list):
            rem["table_keys"] = list(result["table_keys"])
        rem["backend"] = result.get("backend") or rem["backend"]
        rem["base_url"] = result.get("base_url")
        r["urls"] = [result["url"]] if isinstance(result.get("url"), str) else []
        r["problems"] += [str(p)[:200] for p in result.get("problems") or ()]
        self._save(r, "emitted")
        return self._read_back(r, None)

    def _feature_ces(self) -> bool:
        if self._ces is None:
            self._ces = bool(self.wandb.feature_ces())               # type: ignore[union-attr]
        return self._ces

    def _read_back(self, r: dict, plan: Mapping | None) -> dict:
        if r.get("readback") is not None:
            r.setdefault("readback_history", []).append(dict(r["readback"]))
        if r["surface"] == "wandb":
            rem = r["remote"]
            try:
                problems = list(self.wandb.confirm(rem["run_path"], rem["table_keys"], rem["artifact_names"]))  # type: ignore[union-attr]
                content_checked = False
                check_contents = getattr(self.wandb, "confirm_table_contents", None)
                if rem.get("backend") == "wandb_table" and rem["table_keys"] and callable(check_contents):
                    doc = self._doc(self.paths.plan_dir(r["unit"]) / "tables.json") or {}
                    tables = [t for t in doc.get("tables", []) if t.get("key") in rem["table_keys"]]
                    if {t["key"] for t in tables} != set(rem["table_keys"]):
                        problems.append("table_content_plan_missing")
                    else:
                        problems += list(check_contents(rem["run_path"], tables))
                        content_checked = True
            except Exception as exc:                                 # noqa: BLE001 - stays in flight; read back later
                r["error"] = self._error(exc)
                return self._save(r)
            r["readback"] = {"checked_utc": self._now(), "table_contents_checked": content_checked,
                             "mismatches": [str(p)[:200] for p in problems][:200]}
            return self._save(r, "partial" if problems else "verified")
        if plan is None:
            plan = self._doc(self.paths.plan_dir(r["unit"]) / "spans.json")
        expected = int(r.get("expected_spans") or 0)
        if plan is None or not r["trace_ids"]:
            r["readback"] = {"spans": 0, "checked_utc": self._now(), "waited_s": 0.0,
                             "mismatches": ["plan_missing" if plan is None else "no_trace_ids"]}
            return self._save(r, "partial")
        poll = float(util.lookup(self.manifest, "telemetry.agent_lens.readback_poll_s", 2))
        timeout = float(util.lookup(self.manifest, "telemetry.agent_lens.readback_timeout_s", 60))
        t0 = self.clock.monotonic()
        while True:
            try:
                rows = list(self.lens.read_back(plan["project"], list(r["trace_ids"])))   # type: ignore[union-attr]
            except Exception as exc:                                 # noqa: BLE001 - stays in flight; read back later
                r["error"] = self._error(exc)
                return self._save(r)
            if len(rows) >= expected or self.clock.monotonic() - t0 >= timeout:
                break
            self.clock.sleep(poll)
        out = self.paths.stored_spans_file(r["unit"])
        out.parent.mkdir(parents=True, exist_ok=True)
        util.write_json(out, rows)
        mismatches = readback.verify_spans(plan, rows)
        if len(rows) != expected:
            mismatches = [f"count:{len(rows)}!={expected}"] + mismatches
        r["readback"] = {"spans": len(rows), "checked_utc": self._now(),
                         "waited_s": round(self.clock.monotonic() - t0, 3), "mismatches": mismatches[:200]}
        r["error"] = None if not mismatches else r.get("error")
        return self._save(r, "verified" if not mismatches else "partial")

    # ------------------------------------------------------------ all units

    def _final(self, unit: str, surfaces: Sequence[str]) -> bool:
        for s in surfaces:
            rec = self._doc(self.paths.receipt_file(unit, s))
            if rec is None or rec.get("status") not in _FINAL or (rec["status"] == "dry_run" and not self.dry_run):
                return False
        return True

    def export_all(self, *, only: Sequence[str] | None = None, retry_partial: bool = False,
                   supersede: Sequence[str] = ()) -> dict:
        """{"receipts": {unit: {surface: receipt}}, "statuses": {unit: {surface: status}}, "deferred": [unit],
        "not_ready": [unit]}. A unit whose dependencies have no final receipts yet is deferred, not exported."""
        with self._locked():
            units = self.discover()
            by_name = {u.unit: u for u in units}
            wanted = None if only is None else set(only)
            out: dict = {"receipts": {}, "statuses": {}, "deferred": [],
                         "not_ready": sorted(wanted - set(by_name)) if wanted is not None else []}
            for u in units:
                if wanted is not None and u.unit not in wanted:
                    continue
                if not all(d in by_name and self._final(d, by_name[d].surfaces) for d in u.depends_on):
                    out["deferred"].append(u.unit)
                    continue
                receipts = self.export_unit(u, retry_partial=retry_partial, supersede=u.unit in set(supersede))
                out["receipts"][u.unit] = receipts
                out["statuses"][u.unit] = {s: rec.get("status") for s, rec in receipts.items()}
            return out
