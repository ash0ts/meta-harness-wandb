# SPDX-License-Identifier: MIT
"""Records -> W&B run and Artifact plans, and our own search metrics (spec 2.15, 3.23, decisions D7 and D14).

Pure: no SDK, no clock, no file, no network, no ledger. The same inputs give the same bytes; in particular a search-step
plan sums the cost figures of the iteration records up to its own, so the plan of a settled iteration never changes.
A run config is the allow-listed subset of the manifest (RUN_CONFIG_ALLOW) passed through policy.portable, plus the
run's own identifiers; the only file a plan ever names is a candidate file that the verbatim scan did not withhold.
"""

from __future__ import annotations

import json
import math
import re
from typing import Mapping, Sequence

from .. import manifest as manifest_mod
from .. import util, wire
from . import policy

__all__ = [
    "RUNPLAN_SCHEMA", "RUN_CONFIG_ALLOW", "RUN_KINDS", "ARTIFACT_TYPE", "SEARCH_HISTORY_KEYS", "run_id",
    "plan_evaluate_run", "plan_propose_run", "plan_search_step", "search_metrics",
]

RUNPLAN_SCHEMA = "mhforge.runplan/1"
ARTIFACT_TYPE = "harness-candidate"
RUN_KINDS = ("search", "propose", "evaluate", "publication")
PLACEHOLDER_ENTITY, PLACEHOLDER_PROJECT = "unset-entity", "unset-project"
CONVERSATION_BASE = "https://forge.coreweave.com/agent-lens/"

RUN_CONFIG_ALLOW: tuple[str, ...] = (
    "campaign", "campaign_kind", "upstream.repo", "upstream.commit", "upstream.tree_sha256", "upstream.worker_lock_sha256",
    "upstream.expect", "search", "routing", "provider.base_url", "provider.wire_model", "provider.expected_response_model",
    "prices", "gateway.max_in_flight", "gateway.max_output_tokens", "budgets", "accounting", "calibration", "executor.kind",
    "executor.max_attempts", "executor.max_infra_attempts", "executor.job_timeout_s", "executor.parallel_dataset_jobs",
    "sandbox.env_mode", "sandbox.image", "sandbox.base_image", "sandbox.wheelhouse.requirements_sha256",
    "sandbox.wheelhouse.sha256sums_sha256", "sandbox.cpu", "sandbox.memory", "capture", "decisions.proposer_permission_mode",
    "decisions.evaltables_backend", "decisions.dataset_text_publication", "proposer.cli_sha256", "proposer.cli_version",
    "proposer.model", "proposer.effort", "proposer.max_budget_usd_per_session", "telemetry.content",
)

SEARCH_HISTORY_KEYS = (
    "search/upstream_frontier_val", "search/best_complete_avg_val", "search/improvement", "search/candidates_proposed",
    "search/candidates_valid", "search/candidates_complete", "search/jobs_failed", "search/solver_cost_usd",
    "search/sandbox_usd", "search/proposer_list_usd", "search/context_chars_best", "search/prompt_tokens_max_best",
    "search/evidence_exposed", "search/propose_s", "search/bench_s",
)

_EVIDENCE_PAIRS = (("memory_json_sha256", "memory_json_bytes"), ("log_jsonl_sha256", "log_jsonl_bytes"))
_HEX64 = re.compile(r"[0-9a-f]{64}")
_CANDIDATE_FILE = re.compile(r"candidates/([a-z][a-z0-9_]{0,47})\.py")


# ---------------------------------------------------------------- small helpers

def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _finite(v: object) -> bool:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return False
    try:
        return math.isfinite(v)
    except OverflowError:                              # an int too large for a float
        return False


def _num(v: object) -> float | int | None:
    return v if _finite(v) else None


def _hex(v: object) -> str | None:
    return v if isinstance(v, str) and _HEX64.fullmatch(v) else None


def _candidate(name: object) -> str:
    if not isinstance(name, str) or not wire.CANDIDATE_RE.fullmatch(name):
        raise ValueError(f"bad candidate name {str(name)[:60]!r}")
    return name


def _attempt(k: object) -> int:
    if not _is_int(k) or k < 1:
        raise ValueError("export_attempt must be a positive integer")
    return k


def _usd(micro: int) -> float:
    return float(util.usd(micro))


def _campaign(manifest: Mapping) -> str:
    campaign = util.lookup(manifest, "campaign", None)
    if not isinstance(campaign, str) or not wire.CAMPAIGN_RE.fullmatch(campaign):
        raise ValueError("manifest campaign is missing or malformed")
    return campaign


def _datasets(manifest: Mapping) -> list[str]:
    return [d for d in (util.lookup(manifest, "upstream.expect.datasets", []) or []) if isinstance(d, str)]


def _target(manifest: Mapping) -> tuple[str, str, bool]:
    entity = util.lookup(manifest, "decisions.entity", None)
    project = util.lookup(manifest, "decisions.project", None)
    if isinstance(entity, str) and entity and isinstance(project, str) and project:
        return entity, project, False
    return PLACEHOLDER_ENTITY, PLACEHOLDER_PROJECT, True


def _group(manifest: Mapping, campaign: str) -> str:
    group = util.lookup(manifest, "telemetry.wandb_group", None)
    return group if isinstance(group, str) and group else campaign


def _config(manifest: Mapping, identity: Mapping | None = None) -> dict:
    config = policy.portable(manifest_mod.run_config(dict(manifest), RUN_CONFIG_ALLOW), {})
    if identity:
        config.update(policy.portable(dict(identity), {}))
    return config


def _guard(value: object, verbatim) -> object:
    """Proposer-written metadata: dataset text becomes its hash form (policy.guard_text), then no home path survives."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return [_guard(v, verbatim) for v in value]
    text = value if isinstance(value, str) else str(value)
    text, _hit = policy.guard_text(text, verbatim)
    return policy.portable(text, {})


def _run(manifest: Mapping, *, unit: str, rid: str, job_type: str, name: str, tags: list, resume: bool, config: dict,
         summary: dict, define_metrics: list, history: list, tables: list, artifacts: list, export_attempt: int) -> dict:
    campaign = _campaign(manifest)
    entity, project, placeholder = _target(manifest)
    expect_artifacts = [a["name"] for a in artifacts if not a.get("best_effort")]
    return {
        "schema": RUNPLAN_SCHEMA, "unit": unit, "placeholder_project": placeholder, "export_attempt": export_attempt,
        "run": {"ref": unit, "id": rid, "entity": entity, "project": project, "group": _group(manifest, campaign),
                "job_type": job_type, "name": name, "tags": tags, "resume": resume, "config": config,
                "summary": summary, "define_metrics": define_metrics, "history": history, "tables": list(tables),
                "artifacts": artifacts},
        "expect": {"summary_markers": list(tables), "artifacts": expect_artifacts},
    }


def _metadata(entry: Mapping | None, *, iteration, sha: str | None, nbytes, upstream_commit, verbatim) -> dict:
    entry = entry if isinstance(entry, Mapping) else {}
    base = entry.get("base_system")
    return {"iteration": iteration if _is_int(iteration) else None,
            "axis": _guard(entry.get("axis"), verbatim),
            "hypothesis": _guard(entry.get("hypothesis"), verbatim),
            "components": _guard(entry.get("components"), verbatim),
            "base_system": base if isinstance(base, str) and wire.CANDIDATE_RE.fullmatch(base) else None,
            "file_sha256": sha, "file_bytes": nbytes if _is_int(nbytes) else None,
            "upstream_commit": upstream_commit}


def _log_artifact(campaign: str, candidate: str, *, source: str, sha: str | None, nbytes, metadata: dict,
                  withheld: Mapping | None) -> dict:
    file_name = f"{candidate}.py"
    held = None
    if isinstance(withheld, Mapping):
        for key in (file_name, source, f"candidates/{file_name}", f"agents/{file_name}"):
            if isinstance(withheld.get(key), Mapping):
                held = withheld[key]
                break
    art = {"op": "log", "name": f"candidate-{candidate}", "type": ARTIFACT_TYPE, "aliases": [campaign],
           "files": [], "withheld_files": [], "metadata": metadata}
    if held is not None:
        h_sha, h_bytes = _hex(held.get("sha256")), held.get("bytes") if _is_int(held.get("bytes")) else None
        metadata["file_sha256"], metadata["file_bytes"] = h_sha or sha, h_bytes
        art["withheld_files"] = [{"name": file_name, "sha256": h_sha or sha, "bytes": h_bytes}]
    else:
        art["files"] = [{"source": source, "name": file_name, "sha256": sha, "bytes": nbytes if _is_int(nbytes) else None}]
    return art


def _use_artifact(campaign: str, candidate: str, *, best_effort: bool) -> dict:
    art = {"op": "use", "name": f"candidate-{candidate}", "type": ARTIFACT_TYPE, "alias": campaign}
    if best_effort:
        art["best_effort"] = True
    return art


# ---------------------------------------------------------------- ids

def run_id(campaign: str, kind: str, *, candidate: str | None = None, split: str | None = None,
           export_attempt: int = 1) -> str:
    """The W&B run id of 2.2, through wire.bounded_id. '-e<k>' only for export attempt k >= 2 of a propose or evaluate
    run; the search run and the publication run keep one id."""
    if not isinstance(campaign, str) or not wire.CAMPAIGN_RE.fullmatch(campaign):
        raise ValueError(f"bad campaign {str(campaign)[:60]!r}")
    k = _attempt(export_attempt)
    if kind == "search":
        text = f"{campaign}-search"
    elif kind == "publication":
        text = f"{campaign}-publication"
    elif kind == "propose":
        text = f"{campaign}-propose-{_candidate(candidate)}"
    elif kind in ("evaluate", "eval"):
        if split not in wire.SPLITS:
            raise ValueError(f"bad split {str(split)[:20]!r}")
        text = f"{campaign}-eval-{split}-{_candidate(candidate)}"
    else:
        raise ValueError(f"unknown run kind {str(kind)[:30]!r}")
    if kind in ("propose", "evaluate", "eval") and k >= 2:
        text += f"-e{k}"
    return wire.bounded_id(text, 128)


# ---------------------------------------------------------------- evaluate and propose runs

def plan_evaluate_run(candidate: str, split: str, job_records: Mapping[str, Mapping], table_keys: Sequence[str],
                      manifest: dict, *, proposal_entry: Mapping | None, conversation_urls: Mapping[str, str],
                      is_baseline: bool, evidence: Mapping[str, Mapping], withheld: Mapping[str, Mapping] | None = None,
                      verbatim: "scan.VerbatimIndex | None" = None, export_attempt: int = 1) -> dict:  # noqa: F821
    candidate, k = _candidate(candidate), _attempt(export_attempt)
    if split not in wire.SPLITS:
        raise ValueError(f"bad split {str(split)[:20]!r}")
    campaign = _campaign(manifest)
    datasets = _datasets(manifest)
    job_records = job_records if isinstance(job_records, Mapping) else {}
    entry = proposal_entry if isinstance(proposal_entry, Mapping) else None
    commit = util.lookup(manifest, "upstream.commit", None)

    summary: dict = {}
    acc_sum, complete, failed, cost, trunc = 0.0, 0, 0, 0, 0
    job_sha = None
    for ds in datasets + sorted(d for d in job_records if d not in datasets):
        rec = job_records.get(ds)
        if not isinstance(rec, Mapping):
            continue
        job_sha = job_sha or _hex(rec.get("candidate_sha256"))
        for att in rec.get("attempts") or []:
            gw = att.get("gateway") if isinstance(att, Mapping) and isinstance(att.get("gateway"), Mapping) else {}
            cost += gw.get("cost_micro") if _is_int(gw.get("cost_micro")) else 0
            trunc += gw.get("truncated_empty") if _is_int(gw.get("truncated_empty")) else 0
        status = rec.get("status")
        res = rec.get("result") if isinstance(rec.get("result"), Mapping) else {}
        if status == "succeeded" and _finite(res.get("accuracy")):
            summary[f"{split}/{ds}/accuracy"] = res["accuracy"]
            complete += 1
            if ds in datasets:
                acc_sum += res["accuracy"]
        elif status in ("failed", "halted"):
            failed += 1
    summary[f"{split}/macro_accuracy_all_datasets"] = round(acc_sum / len(datasets), 6) if datasets else None
    summary.update({"datasets_complete": complete, "datasets_failed": failed, "solver_cost_usd": _usd(cost),
                    "truncated_empty_calls": trunc})
    urls = {d: u for d, u in (conversation_urls or {}).items()
            if isinstance(d, str) and isinstance(u, str) and u.startswith(CONVERSATION_BASE)}
    summary["conversation_urls"] = json.dumps(urls, sort_keys=True)
    hashes = {}
    for ds, ev in sorted((evidence or {}).items()):
        if isinstance(ds, str) and isinstance(ev, Mapping):
            hashes[ds] = {}
            for sha_key, bytes_key in _EVIDENCE_PAIRS:
                hashes[ds][sha_key] = _hex(ev.get(sha_key))
                hashes[ds][bytes_key] = ev.get(bytes_key) if _is_int(ev.get(bytes_key)) else None
    summary["evidence_hashes"] = json.dumps(hashes, sort_keys=True)

    sha = (_hex(entry.get("sha256")) or _hex(entry.get("file_sha256"))) if entry else None
    sha = sha or job_sha
    iteration = entry.get("iteration") if entry else None
    base = entry.get("base_system") if entry else None
    identity = {"campaign": campaign, "candidate": candidate, "split": split,
                "iteration": iteration if _is_int(iteration) else None,
                "base_system": base if isinstance(base, str) and wire.CANDIDATE_RE.fullmatch(base) else None,
                "candidate_sha256": sha, "upstream_commit": commit,
                "manifest_sha256": manifest_mod.manifest_sha256(manifest)}
    if is_baseline:
        md = _metadata(entry, iteration=iteration, sha=sha, nbytes=entry.get("bytes") if entry else None,
                       upstream_commit=commit, verbatim=verbatim)
        artifacts = [_log_artifact(campaign, candidate, source=f"workspace/text_classification/agents/{candidate}.py",
                                   sha=sha, nbytes=entry.get("bytes") if entry else None, metadata=md,
                                   withheld=withheld)]
    else:
        artifacts = [_use_artifact(campaign, candidate, best_effort=False)]
    return _run(manifest, unit=f"eval:{split}.{candidate}",
                rid=run_id(campaign, "evaluate", candidate=candidate, split=split, export_attempt=k),
                job_type="evaluate", name=f"{candidate} ({split})", tags=["baseline" if is_baseline else "candidate"],
                resume=False, config=_config(manifest, identity), summary=summary, define_metrics=[], history=[],
                tables=[t for t in table_keys if isinstance(t, str)], artifacts=artifacts, export_attempt=k)


def plan_propose_run(candidate_entry: Mapping, session_record: Mapping, manifest: dict, *,
                     withheld: Mapping[str, Mapping] | None = None, verbatim: "scan.VerbatimIndex | None" = None,  # noqa: F821
                     export_attempt: int = 1) -> dict:
    k = _attempt(export_attempt)
    candidate = _candidate(candidate_entry.get("name"))
    campaign = _campaign(manifest)
    iteration, ordinal = session_record.get("iteration"), session_record.get("ordinal")
    if not _is_int(iteration) or iteration < 1 or not _is_int(ordinal) or ordinal < 1:
        raise ValueError("session record needs a positive iteration and ordinal")
    file_rel = candidate_entry.get("file")
    m = _CANDIDATE_FILE.fullmatch(file_rel) if isinstance(file_rel, str) else None
    if not m or m.group(1) != candidate:
        file_rel = f"candidates/{candidate}.py"
    source = f"proposer/iter-{iteration:03d}/s{ordinal}/{file_rel}"
    commit = util.lookup(manifest, "upstream.commit", None)
    sha = _hex(candidate_entry.get("sha256")) or _hex(candidate_entry.get("file_sha256"))
    md = _metadata(candidate_entry, iteration=iteration, sha=sha, nbytes=candidate_entry.get("bytes"),
                   upstream_commit=commit, verbatim=verbatim)
    artifacts = [_log_artifact(campaign, candidate, source=source, sha=sha, nbytes=candidate_entry.get("bytes"),
                               metadata=md, withheld=withheld)]
    base = md["base_system"]
    if base is not None and base != candidate:
        artifacts.append(_use_artifact(campaign, base, best_effort=True))
    session_key = session_record.get("session_key")
    session_key = session_key if isinstance(session_key, str) and len(session_key) <= 32 else None
    identity = {"campaign": campaign, "candidate": candidate, "iteration": iteration, "session_key": session_key,
                "base_system": base, "candidate_sha256": sha, "upstream_commit": commit,
                "manifest_sha256": manifest_mod.manifest_sha256(manifest)}
    outcome = session_record.get("outcome")
    summary = {"iteration": iteration, "session_key": session_key,
               "session_outcome": outcome if outcome in wire.SESSION_OUTCOMES else None}
    return _run(manifest, unit=f"propose:{candidate}",
                rid=run_id(campaign, "propose", candidate=candidate, export_attempt=k), job_type="propose",
                name=f"{candidate} (iter {iteration})", tags=["candidate"], resume=False,
                config=_config(manifest, identity), summary=summary, define_metrics=[], history=[], tables=[],
                artifacts=artifacts, export_attempt=k)


# ---------------------------------------------------------------- search metrics and the search run

def _eligible(c: Mapping) -> bool:
    return c.get("valid") is not False and c.get("rejected") is None and c.get("quarantined") is not True


def _candidate_figures(c: Mapping, datasets: Sequence[str]) -> dict:
    per = c.get("datasets") if isinstance(c.get("datasets"), Mapping) else {}
    total, missing, failed = 0.0, [], []
    for ds in datasets:
        d = per.get(ds) if isinstance(per.get(ds), Mapping) else {}
        if d.get("status") == "succeeded" and _finite(d.get("accuracy")):
            total += d["accuracy"] * 100.0
        else:
            missing.append(ds)
    for ds, d in sorted(per.items()):
        if isinstance(d, Mapping) and d.get("status") in ("failed", "halted"):
            failed.append(ds)
    avg = round(total / len(datasets), 6) if datasets else None
    return {"name": c.get("name") if isinstance(c.get("name"), str) else None, "valid": _eligible(c),
            "avg_val_all_datasets": avg, "complete": bool(datasets) and not missing, "missing": missing,
            "failed": failed}


def search_metrics(iteration_records: Sequence[Mapping], manifest: dict) -> list[dict]:
    """Our own figures (D14), one dict per iteration record in iteration order: per candidate the average over all
    manifest datasets in percent (a missing dataset counts as 0 and marks the candidate incomplete); the best complete
    average before and after the iteration and the improvement over the one before; the failed (candidate, dataset)
    jobs; upstream's frontier value; cumulative solver, sandbox and proposer figures (sums over the records so far)."""
    datasets = _datasets(manifest)
    records = sorted((r for r in iteration_records if isinstance(r, Mapping) and _is_int(r.get("iteration"))),
                     key=lambda r: r["iteration"])
    out, best, frontier = [], None, None
    cum = {"solver_cost_micro": 0, "sandbox_cost_micro": 0, "proposer_list_price_micro": 0}
    for rec in records:
        cands = [c for c in (rec.get("candidates") or []) if isinstance(c, Mapping)]
        figs = [_candidate_figures(c, datasets) for c in cands]
        fb = rec.get("frontier_before") if isinstance(rec.get("frontier_before"), Mapping) else {}
        for value in [fb.get("val_accuracy")] + [c.get("upstream_avg_val") for c in cands if _eligible(c)]:
            if _finite(value):
                frontier = value if frontier is None else max(frontier, value)
        complete = [(f, c) for f, c in zip(figs, cands) if f["valid"] and f["complete"]]
        top = max(complete, key=lambda fc: fc[0]["avg_val_all_datasets"], default=None)
        before = best
        if top is not None:
            best = top[0]["avg_val_all_datasets"] if best is None else max(best, top[0]["avg_val_all_datasets"])
        ctx = top[1].get("context") if top is not None and isinstance(top[1].get("context"), Mapping) else {}
        for key in cum:
            cum[key] += rec.get(key) if _is_int(rec.get(key)) else 0
        timing = rec.get("timing_s") if isinstance(rec.get("timing_s"), Mapping) else {}
        out.append({
            "iteration": rec["iteration"], "kind": rec.get("kind"), "candidates": figs,
            "candidates_proposed": len(cands), "candidates_valid": sum(1 for f in figs if f["valid"]),
            "candidates_complete": len(complete), "best_candidate": top[0]["name"] if top else None,
            "best_complete_before": before, "best_complete_after": best,
            "improvement": round(best - before, 6) if before is not None and best is not None else 0.0,
            "failed_jobs": [[f["name"], ds] for f in figs for ds in f["failed"]],
            "upstream_frontier_val": frontier,
            "context_chars_best": _num(ctx.get("memory_context_chars_avg")),
            "prompt_tokens_max_best": _num(ctx.get("prompt_tokens_max")),
            "evidence_exposed": 1 if rec.get("evidence_exposed") is True else 0,
            "propose_s": _num(timing.get("propose")), "bench_s": _num(timing.get("bench")),
            "cumulative": dict(cum),
        })
    return out


def plan_search_step(iteration_record: Mapping, earlier_records: Sequence[Mapping], manifest: dict, *,
                     export_attempt: int = 1) -> dict:
    """The run plan of unit loop:iter<NNN>: one history row on the one search run, no ledger snapshot."""
    k = _attempt(export_attempt)
    n = iteration_record.get("iteration")
    if not _is_int(n) or n < 0:
        raise ValueError("iteration record needs a non-negative iteration")
    campaign = _campaign(manifest)
    earlier = [r for r in earlier_records if isinstance(r, Mapping) and _is_int(r.get("iteration")) and r["iteration"] < n]
    m = search_metrics(earlier + [iteration_record], manifest)[-1]
    cum = m["cumulative"]
    values = (m["upstream_frontier_val"], m["best_complete_after"], m["improvement"], m["candidates_proposed"],
              m["candidates_valid"], m["candidates_complete"], len(m["failed_jobs"]), _usd(cum["solver_cost_micro"]),
              _usd(cum["sandbox_cost_micro"]), _usd(cum["proposer_list_price_micro"]), m["context_chars_best"],
              m["prompt_tokens_max_best"], m["evidence_exposed"], m["propose_s"], m["bench_s"])
    row: dict = {"iteration": n}
    row.update({key: value for key, value in zip(SEARCH_HISTORY_KEYS, values) if value is not None})
    if k >= 2:
        row["search/export_attempt"] = k
    return _run(manifest, unit=f"loop:iter{n:03d}", rid=run_id(campaign, "search", export_attempt=k), job_type="search",
                name=f"{campaign} search", tags=["search"], resume=n > 0,
                config=_config(manifest) if n == 0 else {}, summary={},
                define_metrics=[["iteration", None], ["search/*", "iteration"]], history=[row], tables=[],
                artifacts=[], export_attempt=k)
