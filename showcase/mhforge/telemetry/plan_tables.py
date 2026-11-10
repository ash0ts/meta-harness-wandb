# SPDX-License-Identifier: MIT
"""Example rows -> EvalTable plans with the CES rules (spec 2.14, 3.23). Pure: no SDK, no clock, no file, no network.

One table per succeeded job of an evaluate unit, key `<split>/<dataset>`. Inputs are identifiers and hashes only, so a
row aligns across every candidate's table and a table never holds question text. Where predictions are shown as
labels, a prediction is shown only when it equals a target of the same table (LD5); any other is hashed. A table whose
rows disagree with the
result file (the sidecar is untrusted), or that fails any other check, is dropped from `tables` and recorded under
`dropped` with the reason.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Mapping, Sequence

from .. import util, wire

__all__ = [
    "TABLEPLAN_SCHEMA", "TABLEPLANSET_SCHEMA", "LABEL_MAX_CHARS", "MAX_ROWS", "SCORE_METRICS", "INPUT_COLUMNS",
    "CHECK_NAMES", "PLACEHOLDER_PROJECT", "CONVERSATION_BASE", "plan_tables", "check_table", "table_backend",
]

TABLEPLAN_SCHEMA = "mhforge.tableplan/1"
TABLEPLANSET_SCHEMA = "mhforge.tableplanset/1"
LABEL_MAX_CHARS = 200
MAX_ROWS = 10000
SCORE_METRICS = ("jaccard_similarity", "f1", "precision", "recall")     # the only metrics ever promoted to scores
INPUT_COLUMNS = ("dataset", "split", "example_id", "question_sha256")
CHECK_NAMES = ("rows_1_to_10000", "primitive_cells", "single_type_columns", "no_all_null_columns",
               "no_null_or_nan_scores", "inputs_are_strings", "matches_result_file")
PLACEHOLDER_PROJECT = "unset-entity/unset-project"
CONVERSATION_BASE = "https://forge.coreweave.com/agent-lens/"

_TAIL_OUTPUTS = (("n_calls", "int"), ("input_tokens", "int"), ("output_tokens", "int"), ("latency_s", "float"),
                 ("finish_reason", "str"), ("truncated_empty", "bool"), ("row_complete", "bool"),
                 ("metrics_json", "str"), ("trace_url", "str"))
_HEX64 = re.compile(r"[0-9a-f]{64}")
_CONV_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")
_EXAMPLE_ID = re.compile(r"[0-9]{4,}-[0-9a-f]{0,12}")
_METRIC_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_MAX_EXPORT_ATTEMPTS = 9


# ---------------------------------------------------------------- small helpers

def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _finite(v: object) -> bool:
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return False
    try:
        return math.isfinite(v)
    except OverflowError:                              # an int too large for a float (untrusted sidecar)
        return False


def _type_name(v: object) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    return "other"


def _project(manifest: Mapping) -> tuple[str, bool]:
    entity = util.lookup(manifest, "decisions.entity", None)
    project = util.lookup(manifest, "decisions.project", None)
    if isinstance(entity, str) and entity and isinstance(project, str) and project:
        return f"{entity}/{project}", False
    return PLACEHOLDER_PROJECT, True


def table_backend(manifest: Mapping) -> str:
    """decisions.evaltables_backend as a plan states it: 'ces', 'wandb_table', 'off', or 'unset' (null or 'unset')."""
    value = util.lookup(manifest, "decisions.evaltables_backend", None)
    return value if value in ("ces", "wandb_table", "off") else "unset"


def _datasets_in_order(manifest: Mapping, keys) -> list[str]:
    configured = [d for d in (util.lookup(manifest, "upstream.expect.datasets", []) or []) if isinstance(d, str)]
    extra = sorted(k for k in keys if isinstance(k, str) and k not in configured)
    return [d for d in configured if d in keys] + extra


def _succeeded_job_id(record: Mapping) -> str | None:
    attempts = [a for a in (record.get("attempts") or []) if isinstance(a, Mapping)]
    ok = [a for a in attempts if a.get("outcome") == "ok"]
    chosen = (ok or attempts or [None])[-1]
    if chosen is None:
        return None
    job_id = chosen.get("job_id")
    if isinstance(job_id, str) and job_id:
        return job_id
    if isinstance(record.get("job_key"), str) and _is_int(chosen.get("attempt")):
        return f"{record['job_key']}.a{chosen['attempt']}"
    return None


def _trace_url(url: object, project: str, campaign: str, job_id: str | None) -> str:
    """The URL when it is the Agent Lens conversation URL of exactly this job attempt (any export attempt), else ''."""
    if not isinstance(url, str) or job_id is None:
        return ""
    head = f"{CONVERSATION_BASE}{project}/conversations/"
    if not url.startswith(head):
        return ""
    conv = url[len(head):]
    if not _CONV_ID.fullmatch(conv):
        return ""
    base = f"{campaign}.{job_id}"
    allowed = {wire.bounded_id(base, 128)}
    allowed.update(wire.bounded_id(f"{base}.e{k}", 128) for k in range(2, _MAX_EXPORT_ATTEMPTS + 1))
    return url if conv in allowed else ""


def _label(value: object, sha: object) -> str | None:
    """A short label as it is; a value over LABEL_MAX_CHARS is not a label and becomes '[sha256 <64 hex> chars <n>]'
    (the whole-text hash from the row when it has one, the hash of the held text otherwise)."""
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    if len(text) <= LABEL_MAX_CHARS:
        return text
    digest = sha if isinstance(sha, str) and _HEX64.fullmatch(sha) else \
        hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()
    return f"[sha256 {digest} chars {len(text)}]"


def _withheld(value: object, sha: object) -> str:
    """A prediction that is not a known label (LD5), in the hash form of a gated cell: the row's whole-text hash when it
    has one, the hash of the held text otherwise."""
    if isinstance(sha, str) and _HEX64.fullmatch(sha):
        return sha
    text = value if isinstance(value, str) else str(value)
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _hash_cell(value: object) -> str | None:
    """A 64-hex hash as it is; anything else (the sidecar is untrusted) is not a hash and never reaches a table."""
    return value if isinstance(value, str) and _HEX64.fullmatch(value) else None


def _json_safe(value: object) -> object:
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Mapping):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


def _metrics_json(metrics: object) -> str:
    """Evaluator metrics are numbers under identifier keys; any other key or value (text an untrusted sidecar put
    there) is left out, so the column can never carry dataset text."""
    metrics = metrics if isinstance(metrics, Mapping) else {}
    safe = _json_safe({k: v for k, v in metrics.items() if isinstance(k, str) and _METRIC_KEY.fullmatch(k)
                       and (v is None or isinstance(v, (bool, int, float)))})
    return json.dumps(safe, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


# ---------------------------------------------------------------- one table

def _build_table(*, unit: str, candidate: str, split: str, dataset: str, job_id: str, rows_in: Sequence[Mapping],
                 result: Mapping | None, gated: bool, trace_url: str, backend: str) -> dict:
    rows_in = [r for r in rows_in if isinstance(r, Mapping)]
    out_specs = ([("prediction_sha256", "str"), ("target_sha256", "str")] if gated
                 else [("prediction", "str"), ("target", "str")]) + list(_TAIL_OUTPUTS)

    metric_maps = [r.get("metrics") if isinstance(r.get("metrics"), Mapping) else {} for r in rows_in]
    promoted, demoted = [], []
    for name in SCORE_METRICS:
        present = any(name in m for m in metric_maps)
        if rows_in and all(_finite(m.get(name)) for m in metric_maps):
            promoted.append(name)
        elif present:
            demoted.append(name)
    score_specs = [("was_correct", "bool")] + [(name, "float") for name in promoted]

    # LD5: a prediction is shown in clear only when it equals a target string as this table shows it; any other
    # prediction (a model that echoes the question, for one) is written in the gated hash form and counted
    targets = [None if gated else _label(r.get("target"), r.get("target_sha256")) for r in rows_in]
    known = {t for t in targets if t is not None}
    withheld = 0
    table_rows = []
    for row, metrics, targ in zip(rows_in, metric_maps, targets):
        if gated:
            pred, targ = _hash_cell(row.get("prediction_sha256")), _hash_cell(row.get("target_sha256"))
        else:
            pred = _label(row.get("prediction"), row.get("prediction_sha256"))
            if pred is not None and pred not in known:
                pred = _withheld(row.get("prediction"), row.get("prediction_sha256"))
                withheld += 1
        latency = row.get("latency_s")
        finish = row.get("finish_reason")
        eid = row.get("example_id")
        inputs = [dataset, split, eid if isinstance(eid, str) and _EXAMPLE_ID.fullmatch(eid) else None,
                  _hash_cell(row.get("question_sha256"))]
        outputs = [pred, targ,
                   row.get("n_calls") if _is_int(row.get("n_calls")) else None,
                   row.get("input_tokens") if _is_int(row.get("input_tokens")) else None,
                   row.get("output_tokens") if _is_int(row.get("output_tokens")) else None,
                   float(latency) if _finite(latency) else None,
                   finish if isinstance(finish, str) else None,
                   row.get("truncated_empty") is True,
                   row.get("matched") is True,
                   _metrics_json(metrics),
                   trace_url]
        was = row.get("was_correct")
        scores = [was if isinstance(was, bool) else None] + [float(metrics[name]) for name in promoted]
        table_rows.append(inputs + outputs + scores)

    n_in = len(INPUT_COLUMNS)
    dropped = [name for i, (name, _t) in enumerate(out_specs)
               if table_rows and all(r[n_in + i] is None for r in table_rows)]
    keep = [i for i, (name, _t) in enumerate(out_specs) if name not in dropped]
    out_specs = [out_specs[i] for i in keep]
    table_rows = [r[:n_in] + [r[n_in + i] for i in keep] + r[n_in + len(_TAIL_OUTPUTS) + 2:] for r in table_rows]

    input_columns = list(INPUT_COLUMNS)
    output_columns = [n for n, _t in out_specs]
    score_columns = [n for n, _t in score_specs]
    column_types = {c: "str" for c in input_columns}
    column_types.update({n: t for n, t in out_specs})
    column_types.update({n: t for n, t in score_specs})
    result = result if isinstance(result, Mapping) else {}
    plan = {
        "schema": TABLEPLAN_SCHEMA, "unit": unit, "job_id": job_id, "run_ref": unit, "key": f"{split}/{dataset}",
        "step": 0, "backend": backend,
        "columns": input_columns + output_columns + score_columns,
        "input_columns": input_columns, "output_columns": output_columns, "score_columns": score_columns,
        "column_types": column_types,
        "rows": table_rows, "row_count": len(table_rows),
        "summary": {"accuracy": result.get("accuracy"), "correct": result.get("correct"), "total": result.get("total")},
        "demoted_scores": demoted, "dropped_columns": dropped, "predictions_withheld": withheld,
    }
    plan["checks"] = check_table(plan)
    return plan


# ---------------------------------------------------------------- public

def plan_tables(candidate: str, split: str, job_records: Mapping[str, Mapping], examples: Mapping[str, Sequence[Mapping]],
                results: Mapping[str, Mapping], manifest: dict, *, span_urls: Mapping[str, str], backend: str,
                export_attempt: int = 1) -> dict:
    """The tables.json object of 2.14 for unit eval:<split>.<candidate>. Keys of the mappings are datasets.

    `span_urls`: dataset -> conversation URL of that job's verified Agent Lens receipt (anything else gives '').
    `backend`: the value of decisions.evaltables_backend ('ces', 'wandb_table', 'off', or 'unset' / None)."""
    if not isinstance(candidate, str) or not wire.CANDIDATE_RE.fullmatch(candidate):
        raise ValueError(f"bad candidate name {str(candidate)[:60]!r}")
    if split not in wire.SPLITS:
        raise ValueError(f"bad split {str(split)[:20]!r}")
    if not _is_int(export_attempt) or export_attempt < 1:
        raise ValueError("export_attempt must be a positive integer")
    backend = backend if backend in ("ces", "wandb_table", "off") else "unset"
    unit = f"eval:{split}.{candidate}"
    project, placeholder = _project(manifest)
    campaign = util.lookup(manifest, "campaign", "")
    out = {"schema": TABLEPLANSET_SCHEMA, "unit": unit, "placeholder_project": placeholder, "backend": backend,
           "export_attempt": export_attempt, "tables": [], "dropped": []}
    if backend == "off":
        return out
    job_records = job_records if isinstance(job_records, Mapping) else {}
    examples = examples if isinstance(examples, Mapping) else {}
    results = results if isinstance(results, Mapping) else {}
    span_urls = span_urls if isinstance(span_urls, Mapping) else {}
    for dataset in _datasets_in_order(manifest, list(job_records)):
        record = job_records.get(dataset)
        if not isinstance(record, Mapping) or record.get("status") != "succeeded":
            continue                                   # a failed (or skipped, or halted) job has no table
        job_id = _succeeded_job_id(record)
        key = f"{split}/{dataset}"
        if job_id is None:
            out["dropped"].append({"key": key, "job_id": None, "reason": "no_attempt", "failed_checks": []})
            continue
        decision = wire.CLEARANCE_GATED.get(dataset)
        gated = decision is not None and util.lookup(manifest, decision, None) is not True
        table = _build_table(unit=unit, candidate=candidate, split=split, dataset=dataset, job_id=job_id,
                             rows_in=list(examples.get(dataset) or []), result=results.get(dataset), gated=gated,
                             trace_url=_trace_url(span_urls.get(dataset), project, campaign, job_id), backend=backend)
        failed = [name for name in CHECK_NAMES if table["checks"].get(name) is not True]
        if failed:
            reason = "mismatch_with_result_file" if "matches_result_file" in failed else "failed_checks"
            out["dropped"].append({"key": key, "job_id": job_id, "reason": reason, "failed_checks": failed})
            continue
        out["tables"].append(table)
    return out


def check_table(table_plan: Mapping) -> dict:
    """The 'checks' object of 2.14 for a table plan. Raises nothing: a malformed plan fails the checks it cannot pass."""
    checks = {name: False for name in CHECK_NAMES}
    try:
        checks.update(_checks(table_plan))
    except Exception:                                  # noqa: BLE001 - the contract is "raises nothing"
        pass
    return checks


def _checks(plan: Mapping) -> dict:
    out: dict[str, bool] = {}
    columns = list(plan["columns"])
    inputs, outputs, scores = list(plan["input_columns"]), list(plan["output_columns"]), list(plan["score_columns"])
    types = plan["column_types"]
    rows = plan["rows"]
    if not isinstance(rows, list):
        return out
    shaped = (columns == inputs + outputs + scores and len(set(columns)) == len(columns)
              and isinstance(types, Mapping) and set(types) == set(columns) and "was_correct" in scores)
    out["rows_1_to_10000"] = 1 <= len(rows) <= MAX_ROWS and plan.get("row_count") == len(rows)
    rows_ok = shaped and all(isinstance(r, list) and len(r) == len(columns) for r in rows)
    out["primitive_cells"] = rows_ok and all(
        c is None or isinstance(c, (str, bool, int)) or (isinstance(c, float) and math.isfinite(c))
        for r in rows for c in r)
    if not rows_ok:
        return out
    col = {name: [r[i] for r in rows] for i, name in enumerate(columns)}
    single = True
    for name, cells in col.items():
        kinds = {_type_name(c) for c in cells if c is not None}
        if len(kinds) > 1 or (kinds and kinds != {types.get(name)}):
            single = False
    out["single_type_columns"] = single
    out["no_all_null_columns"] = bool(rows) and all(any(c is not None for c in cells) for cells in col.values())
    score_ok = True
    for name in scores:
        for c in col[name]:
            if name == "was_correct":
                score_ok = score_ok and isinstance(c, bool)
            else:
                score_ok = score_ok and _finite(c)
    out["no_null_or_nan_scores"] = score_ok
    out["inputs_are_strings"] = inputs == list(INPUT_COLUMNS) and all(isinstance(c, str) and c
                                                                     for name in inputs for c in col[name])
    summary = plan.get("summary") if isinstance(plan.get("summary"), Mapping) else {}
    correct, total = summary.get("correct"), summary.get("total")
    n_true = sum(1 for c in col["was_correct"] if c is True)
    out["matches_result_file"] = _is_int(correct) and _is_int(total) and n_true == correct and len(rows) == total
    return out
