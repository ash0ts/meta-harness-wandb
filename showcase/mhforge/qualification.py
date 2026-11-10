# SPDX-License-Identifier: Apache-2.0
# Adapted from ash0ts/harness2-wandb@31b0f82 harness2/showcase/qualification.py L23-L68 (restart rule) and
# harness2/showcase/v4.py L175-L255 (engine_snapshot) and L258-L352 (forecast pattern). Changes: engine snapshot over the mhforge package with a fixed exclusion list; an
# offline gate before spend; observations and checks of one qualification job; a forecast of solver, sandbox and proposer
# budgets with every constant from the manifest; numbered attempts and passed.json with the environment identity and the
# import record; the import rule between a preflight and a measured campaign; noise statistics; restart reasons as a list.
"""Qualification (spec 3.22): engine snapshot, offline gate, observations, checks, forecast, numbered attempts, import.

Everything here is offline. `forecast`, `observations`, `checks` and `noise_stats` are pure; the rest read state files
and write only under STATE/qualification/.
"""

from __future__ import annotations

import math
import os
import re
import statistics
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Mapping, Sequence

from . import ledger as ledger_mod
from . import manifest as manifest_mod
from . import util, workspace
from .errors import CampaignStop, StateError, WorkspaceError
from .executors import sandbox_cost_micro
from .paths import StatePaths
from .telemetry.join import parse_sidecar

__all__ = [
    "engine_snapshot", "offline_gate", "gate_problems", "protocol_stage", "observations", "checks", "forecast", "require_fits",
    "next_attempt", "record", "import_problems", "verify_qualified", "qualified_environment", "calibration_verdict",
    "noise_stats", "restart_allowed", "quantile", "TEMPLATE_MARKER", "STAGE2_MARKER", "ENGINE_EXCLUDED",
]

TEMPLATE_MARKER = "TEMPLATE-UNFILLED"
STAGE2_MARKER = "STAGE2-UNSIGNED"
ENGINE_EXCLUDED = ("audit.py", "publish.py")             # top-level files of the package outside the engine
ENGINE_TELEMETRY_INCLUDED = ("telemetry/transcript.py",)  # the one telemetry file the proposer runner uses
QUALIFICATION_SCHEMA = "mhforge.qualification/1"
FORECAST_SCHEMA = "mhforge.forecast/1"
PASSED_SCHEMA = "mhforge.passed/1"
_ATTEMPT_RE = re.compile(r"attempt-([1-9][0-9]{0,5})")
_OUTCOMES = ("completed", "replayed", "coalesced", "refused", "provider_rejected", "failed", "interrupted")
_IMPORT_CHECK_SECONDS = 300                               # the import-check sandbox of one iteration (3.22)


# ================================================================ engine

def engine_snapshot(package_dir: Path) -> dict:
    """{"engine_sha256", "files": {"mhforge/<rel>": sha256}} over package_dir/**/*.py, except audit.py, publish.py and
    the telemetry package (telemetry/transcript.py is included)."""
    package_dir = Path(package_dir)
    hashes = util.tree_hashes(package_dir, include=["*.py"])
    files = {}
    for rel, digest in hashes.items():
        if rel in ENGINE_EXCLUDED:
            continue
        if rel.startswith("telemetry/") and rel not in ENGINE_TELEMETRY_INCLUDED:
            continue
        files[f"{package_dir.name}/{rel}"] = digest
    return {"engine_sha256": util.tree_digest(files), "files": files}


def _package_dir() -> Path:
    import mhforge                                         # located through mhforge.__file__, never through sc_dir
    return Path(mhforge.__file__).resolve().parent


# ================================================================ offline gate

def protocol_stage(sc_dir: Path) -> int:
    """0: protocol.md missing or TEMPLATE-UNFILLED present; 1: stage 1 signed, STAGE2-UNSIGNED present; 2: both signed."""
    try:
        text = (Path(sc_dir) / "protocol.md").read_text("utf-8", "replace")
    except OSError:
        return 0
    if TEMPLATE_MARKER in text:
        return 0
    return 1 if STAGE2_MARKER in text else 2


def _abs(sc_dir: Path, value: str) -> Path:
    return Path(os.path.abspath(os.path.join(sc_dir, value)))  # 2.3 path rule: lexical, no symlink resolution


def _doc(path: Path) -> dict | None:
    """A JSON object read from a state file, or None when it is absent, unreadable or not an object."""
    try:
        doc = util.read_json(path)
    except (OSError, StateError):
        return None
    return doc if isinstance(doc, dict) else None


def _gate(out: list, name: str, ok: bool, detail: str) -> None:
    out.append({"name": name, "ok": bool(ok), "detail": detail})


def _sha_or_none(path: Path) -> str | None:
    try:
        return util.sha256_file(path) if path.is_file() and not path.is_symlink() else None
    except OSError:
        return None


def _unknown_rows(paths: StatePaths) -> int | None:
    if not paths.ledger.exists():
        return 0
    try:
        conn, stale = util.open_sqlite_ro(paths.ledger)
        try:
            if stale:                                      # a log without its index: rows may be missing from the view
                return None
            return int(conn.execute("SELECT COUNT(*) FROM calls WHERE status = 'unknown'").fetchone()[0])
        finally:
            conn.close()
    except Exception:                                      # noqa: BLE001 - an unreadable ledger fails the check
        return None


def offline_gate(manifest: dict, paths: StatePaths, sc_dir: Path, *, command: str) -> list[dict]:
    """The qualification gate before spend: [{"name", "ok", "detail"}], one entry per check; a check that does not apply
    to this command or executor is listed as ok with a detail saying so. Costs nothing and touches no network."""
    if command not in manifest_mod.COMMANDS:
        raise ValueError(f"unknown live command {command!r}")
    sc_dir = Path(sc_dir)
    out: list[dict] = []
    up, ex, sb = manifest["upstream"], manifest["executor"], manifest["sandbox"]
    # 1 the manifest is frozen and unchanged
    frozen = _doc(paths.manifest)
    digest = manifest_mod.manifest_sha256(manifest)
    if frozen is None:
        _gate(out, "manifest_frozen", False, "no frozen copy: run validate-manifest --freeze")
    elif manifest_mod.manifest_sha256(frozen) != digest:
        _gate(out, "manifest_frozen", False, manifest_mod.MANIFEST_CHANGED)
    else:
        _gate(out, "manifest_frozen", True, digest)
    # 2 the pinned source tree is the one the manifest names (works before any workspace exists)
    try:
        tree = workspace.source_digest(_abs(sc_dir, up["example_dir"]))[0]
        _gate(out, "upstream_source", tree == up["tree_sha256"],
              "matches upstream.tree_sha256" if tree == up["tree_sha256"] else "differs from upstream.tree_sha256")
    except WorkspaceError as exc:
        _gate(out, "upstream_source", False, str(exc))
    # 3 the working copy, when it exists, still holds the pinned files
    if paths.ws.exists() or paths.pristine.exists():
        try:
            changed = workspace.verify_pinned(paths)
            bad = changed["modified"] + changed["removed"]
            _gate(out, "workspace_pinned", not bad, "unchanged" if not bad else f"changed: {', '.join(bad[:5])}")
        except WorkspaceError as exc:
            _gate(out, "workspace_pinned", False, str(exc))
    else:
        _gate(out, "workspace_pinned", True, "no workspace yet")
    # 4 the worker lock file (sandbox executor)
    if ex["kind"] == "sandbox":
        got = _sha_or_none(sc_dir / "worker-image" / "uv.lock")
        _gate(out, "worker_lock", got is not None and got == up["worker_lock_sha256"],
              "matches upstream.worker_lock_sha256" if got is not None and got == up["worker_lock_sha256"]
              else ("worker-image/uv.lock missing" if got is None else "differs from upstream.worker_lock_sha256"))
    else:
        _gate(out, "worker_lock", True, f"not applicable to executor {ex['kind']}")
    # 5 a passing offline smoke for this exact engine
    receipt = _doc(sc_dir / "state" / "_smoke" / "receipt.json")
    engine = engine_snapshot(_package_dir())["engine_sha256"]
    if receipt is None:
        _gate(out, "smoke_receipt", False, "no offline-smoke receipt: run offline-smoke")
    else:
        same = receipt.get("engine_sha256") == engine
        passed = receipt.get("passed") is True
        _gate(out, "smoke_receipt", same and passed,
              ("passed for this engine" if passed else "no passing smoke result: run offline-smoke") if same
              else "made for another engine: run offline-smoke")
    # 6 the proposer binary (search only)
    if command == "search":
        pp = manifest["proposer"]
        got = _sha_or_none(Path(pp["cli_path"])) if pp["cli_path"] else None
        ok = got is not None and got == pp["cli_sha256"]
        _gate(out, "proposer_cli", ok, "pinned hash" if ok else ("binary missing" if got is None else "hash differs"))
    else:
        _gate(out, "proposer_cli", True, "not applicable to " + command)
    # 7 no unknown ledger row
    unknown = _unknown_rows(paths)
    _gate(out, "ledger_unknown", unknown == 0,
          "none" if unknown == 0 else ("ledger unreadable or possibly stale" if unknown is None else
                                       f"{unknown} unknown row(s): reconcile with status --reconcile"))
    # 8 the protocol stage that can exist
    stage = protocol_stage(sc_dir)
    need = 2 if command == "search" and manifest["campaign_kind"] == "measured" else 1
    _gate(out, "protocol", stage >= need, f"stage {stage} signed, {need} needed")
    # 9 the wheelhouse tar and the hashed requirements file
    if ex["kind"] == "sandbox" and sb["env_mode"] == "wheelhouse":
        wh = sb["wheelhouse"]
        tar = _abs(sc_dir, wh["tar_path"])
        got = _sha_or_none(tar)
        size = tar.stat().st_size if got is not None else None
        ok = got is not None and got == wh["tar_sha256"] and size == wh["tar_bytes"]
        _gate(out, "wheelhouse_tar", ok, "hash and size match" if ok else
              ("tar missing" if got is None else "hash or size differs from sandbox.wheelhouse"))
        req = _sha_or_none(sc_dir / "worker-image" / "requirements.lock.txt")
        ok = req is not None and req == wh["requirements_sha256"]
        _gate(out, "requirements_lock", ok, "matches" if ok else
              ("requirements.lock.txt missing" if req is None else "differs from sandbox.wheelhouse.requirements_sha256"))
    else:
        _gate(out, "wheelhouse_tar", True, "not applicable (no wheelhouse mode)")
        _gate(out, "requirements_lock", True, "not applicable (no wheelhouse mode)")
    return out


def gate_problems(gates: Sequence[Mapping]) -> list[str]:
    """The failed checks of `offline_gate` as the strings `manifest.live_blockers(gate_problems=...)` takes:
    "<name>: <detail>", in gate order."""
    return [f"{g['name']}: {g['detail']}" for g in gates if g.get("ok") is not True]


# ================================================================ observations and checks

def quantile(values: Sequence[float], q: float | str | Decimal) -> float | None:
    """Nearest-rank quantile (the smallest value with at least q of the values at or below it); None when empty."""
    vals = sorted(values)
    if not vals:
        return None
    rank = math.ceil(Decimal(str(q)) * len(vals))
    return vals[min(len(vals), max(1, rank)) - 1]


def _dist(values: Sequence[float]) -> dict:
    return {"n": len(values), "p50": quantile(values, "0.5"), "p90": quantile(values, "0.9"),
            "p95": quantile(values, "0.95"), "max": max(values) if values else None}


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _span(a: object, b: object) -> float | None:
    try:
        return round(util.parse_utc(b) - util.parse_utc(a), 6)
    except (TypeError, ValueError):
        return None


def _admission_only(rec: Mapping) -> bool:
    return not rec.get("outcome") or rec.get("ended_utc") is None or rec.get("reply") is None


def observations(call_records: Sequence[Mapping], *, job_record: Mapping | None = None) -> dict:
    """What one qualification job shows (3.22). `job_record` (optional) gives job_seconds: the executor's wall time of
    its last attempt; without it the span of the call records is used."""
    outcomes = {k: 0 for k in _OUTCOMES}
    completion, latency, ttfb, queue = [], [], [], []
    models, usage_keys, reasoning = set(), set(), set()
    no_usage = truncated = limit_exceeded = 0
    ratio = 0.0
    first, last = None, None
    for rec in call_records:
        outcome = "interrupted" if _admission_only(rec) else rec.get("outcome")
        outcomes[outcome if outcome in outcomes else "failed"] += 1
        for key, pick in (("received_utc", min), ("ended_utc", max)):
            try:
                t = util.parse_utc(rec.get(key))
            except (TypeError, ValueError):
                continue
            if key == "received_utc":
                first = t if first is None else pick(first, t)
            else:
                last = t if last is None else pick(last, t)
        if isinstance(rec.get("queue_s"), (int, float)) and not isinstance(rec.get("queue_s"), bool):
            queue.append(float(rec["queue_s"]))
        flags = rec.get("flags") if isinstance(rec.get("flags"), Mapping) else {}
        truncated += bool(flags.get("truncated_empty"))
        if outcome != "completed":
            continue
        span = _span(rec.get("received_utc"), rec.get("ended_utc"))
        if span is not None:
            latency.append(span)
        atts = [a for a in rec.get("attempts") or [] if isinstance(a, Mapping)]
        if atts:
            fb = _span(atts[-1].get("started_utc"), atts[-1].get("first_byte_utc"))
            if fb is not None:
                ttfb.append(fb)
        if rec.get("response_model"):
            models.add(str(rec["response_model"]))
        response = rec.get("response") if isinstance(rec.get("response"), Mapping) else {}
        raw = response.get("usage") if isinstance(response.get("usage"), Mapping) else None
        for k, v in (raw or {}).items():
            usage_keys.add(str(k))
            if isinstance(v, Mapping):
                usage_keys.update(f"{k}.{kk}" for kk in v)
        for choice in response.get("choices") or []:
            message = choice.get("message") if isinstance(choice, Mapping) else None
            if isinstance(message, Mapping):
                reasoning.update(k for k in ("reasoning", "reasoning_content") if message.get(k))
        usage = rec.get("usage") if isinstance(rec.get("usage"), Mapping) else None
        pt, ct = (usage or {}).get("prompt_tokens"), (usage or {}).get("completion_tokens")
        if not _is_int(pt) or not _is_int(ct):
            no_usage += 1
            continue
        completion.append(ct)
        il, ol = rec.get("input_limit"), rec.get("output_limit")
        if _is_int(il) and il > 0:
            ratio = max(ratio, pt / il)
        judged = raw if raw is not None else usage         # the usage the ledger settled, as returned
        if (_is_int(il) and pt > il) or (_is_int(ol) and ct > ol) or (
                _is_int(il) and _is_int(ol) and not ledger_mod.usage_within_limits(dict(judged), il, ol)):
            limit_exceeded += 1                            # the ledger's limit_exceeded rule (ledger.judge_usage)
    calls = sum(outcomes.values())
    job_seconds = None
    attempts = (job_record or {}).get("attempts") or []
    if attempts and isinstance(attempts[-1], Mapping):
        exe = attempts[-1].get("executor") if isinstance(attempts[-1].get("executor"), Mapping) else {}
        sec = exe.get("seconds")
        if isinstance(sec, (int, float)) and not isinstance(sec, bool):
            job_seconds = float(sec)
        else:
            job_seconds = _span(attempts[-1].get("started_utc"), attempts[-1].get("ended_utc"))
    if job_seconds is None and first is not None and last is not None:
        job_seconds = round(last - first, 6)
    return {"calls": calls, "outcomes": outcomes,
            "completion_tokens": _dist(completion), "completion_tokens_sorted": sorted(completion),
            "latency_s": _dist(latency), "first_byte_s": _dist(ttfb), "queue_s": _dist(queue),
            "latency_p95_s": quantile(latency, "0.95"),
            "truncated_empty": truncated, "truncated_empty_rate": (truncated / calls) if calls else 0.0,
            "models": sorted(models), "usage_keys": sorted(usage_keys), "reasoning_fields": sorted(reasoning),
            "calls_without_usage": no_usage, "max_prompt_ratio": round(ratio, 6), "limit_exceeded": limit_exceeded,
            "job_seconds": job_seconds}


def checks(manifest: dict, job_record: Mapping, obs: Mapping, audit_problems: Sequence[str],
           example_rows: Sequence[Mapping]) -> list[dict]:
    """The qualification checks of 3.22 as [{"name", "ok", "detail"}]."""
    out: list[dict] = []
    calls, outcomes = obs.get("calls", 0), obs.get("outcomes") or {}
    done = outcomes.get("completed", 0)
    _gate(out, "all_completed", calls > 0 and done == calls, f"{done} of {calls} calls completed")
    nu = obs.get("calls_without_usage", 0)
    _gate(out, "usage_every_call", calls > 0 and nu == 0, f"{nu} call(s) without usage")
    le, ratio = obs.get("limit_exceeded", 0), obs.get("max_prompt_ratio", 0.0)
    _gate(out, "within_input_limit", le == 0 and ratio <= 1, f"{le} limit_exceeded; largest prompt/limit {ratio}")
    rate, cap = obs.get("truncated_empty_rate", 0.0), util.lookup(manifest, "qualification.max_truncated_empty_rate")
    _gate(out, "truncated_empty_rate", rate <= cap, f"{rate:.4f} against at most {cap}")
    models = list(obs.get("models") or [])
    _gate(out, "one_model", len(models) == 1, f"returned model ids: {models}")
    attempts = job_record.get("attempts") or [] if isinstance(job_record, Mapping) else []
    outcome = attempts[-1].get("outcome") if attempts and isinstance(attempts[-1], Mapping) else None
    _gate(out, "job_outcome_ok", outcome == "ok", f"last attempt outcome {outcome}")
    unmatched = sum(1 for r in example_rows if r.get("matched") is not True)
    _gate(out, "examples_matched", bool(example_rows) and unmatched == 0,
          f"{len(example_rows) - unmatched} of {len(example_rows)} example rows matched")
    _gate(out, "audit_clean", not audit_problems, "clean" if not audit_problems else
          f"{len(audit_problems)} problem(s): {audit_problems[0]}")
    return out


# ================================================================ forecast

def _ceil(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_CEILING))


def forecast(manifest: dict, obs: Mapping) -> dict:
    """Pure: does every budget that can end the search fit (3.22)? Every constant comes from the manifest."""
    f = manifest["forecast"]
    contingency = Decimal(util.lookup(manifest, "accounting.forecast_contingency"))
    prices = manifest_mod.prices(manifest)                 # USD per million tokens = micro-dollars per token
    q = quantile(obs.get("completion_tokens_sorted") or [], f["output_tokens_quantile"])
    if q is None:
        q = util.lookup(manifest, "gateway.max_output_tokens")    # nothing observed: the worst case
    datasets = list(util.lookup(manifest, "upstream.expect.datasets"))
    baselines = list(util.lookup(manifest, "upstream.expect.baselines"))
    cal_systems = util.lookup(manifest, "calibration.systems") or baselines
    iterations = int(util.lookup(manifest, "search.iterations"))
    repeats = int(util.lookup(manifest, "calibration.noise_repeats"))
    per_job = {split: {ds: _ceil(prices["input"] * f["input_tokens_per_job"][split][ds]
                                 + prices["output"] * f["calls_per_job"][split][ds] * Decimal(q))
                       for ds in datasets} for split in ("val", "test")}
    sums = {split: sum(per_job[split].values()) for split in per_job}
    n_val = len(baselines) + iterations * int(f["candidates_per_iteration"])
    n_test = int(f["test_systems"])
    n_cal_test, n_noise_val = len(cal_systems), (repeats - 1) * len(baselines)
    validation_total = _ceil(contingency * n_val * sums["val"])
    test_total = _ceil(contingency * n_test * sums["test"])
    calibration_total = _ceil(contingency * (n_cal_test * sums["test"] + n_noise_val * sums["val"]))
    from .controls import PLAN as controls_plan
    n_controls = controls_plan['k']
    controls_total = _ceil(contingency * n_controls * sums['test'])
    caps = manifest_mod.phase_caps_micro(manifest)
    fits_solver = (validation_total <= caps["baseline"] + caps["search"] and test_total <= caps["test"]
                   and calibration_total <= caps["calibration"] and controls_total <= caps['controls'])
    problems: list[str] = []
    # sandbox time
    sandbox_total, job_s = 0, {}
    if manifest["executor"]["kind"] == "sandbox":
        sb = manifest["sandbox"]
        setup = int(sb["setup_allowance_s"]) if sb["env_mode"] == "wheelhouse" else 0
        seen, ncalls = obs.get("job_seconds"), obs.get("calls") or 0
        for split in ("val", "test"):
            job_s[split] = {}
            for ds in datasets:
                scaled = seen * f["calls_per_job"][split][ds] / ncalls if seen is not None and ncalls else 0.0
                job_s[split][ds] = max(float(f["job_seconds"][split][ds]), float(scaled))

        def cost(seconds: float) -> int:
            return sandbox_cost_micro(math.ceil(seconds * 1000), sb["cpu"], sb["memory"], manifest)

        per_set = {split: sum(cost(job_s[split][ds] + setup) for ds in datasets) for split in job_s}
        raw = ((n_val + n_noise_val) * per_set["val"] + (n_test + n_cal_test + n_controls) * per_set["test"]
               + iterations * cost(_IMPORT_CHECK_SECONDS + setup))
        sandbox_total = _ceil(contingency * raw)
    fits_sandbox = sandbox_total <= util.micros(util.lookup(manifest, "budgets.sandbox_usd"))
    # proposer sessions on a usage-billed seat
    estimate = util.lookup(manifest, "proposer.session_estimate_usd")
    cap = util.lookup(manifest, "budgets.proposer_list_usd")
    sessions = math.ceil(Decimal(iterations) * Decimal("1.5"))
    if sessions == 0:
        proposer_total, fits_proposer = 0, True
    elif estimate is None or cap is None:
        proposer_total, fits_proposer = None, False
        problems.append("proposer.session_estimate_usd and budgets.proposer_list_usd are needed for the forecast")
    else:
        proposer_total = _ceil(contingency * sessions * util.micros(estimate))
        fits_proposer = proposer_total <= util.micros(cap)
    p95 = obs.get("latency_p95_s")
    queue_bound = None if p95 is None else math.ceil(94 / int(manifest["gateway"]["max_in_flight"])) * p95
    return {"per_job": per_job, "output_tokens_q": q, "validation_total_micro": validation_total,
            "test_total_micro": test_total, "sandbox_total_micro": sandbox_total,
            "proposer_total_micro": proposer_total, "calibration_total_micro": calibration_total,
            "controls_total_micro": controls_total,
            "job_seconds": job_s, "proposer_sessions": sessions,
            "queue_wait_bound_s": queue_bound,
            "parallel_validation_safe": None if queue_bound is None else queue_bound < 600,
            "fits_solver": fits_solver, "fits_sandbox": fits_sandbox, "fits_proposer": fits_proposer,
            "fits": bool(fits_solver and fits_sandbox and fits_proposer), "problems": problems}


def require_fits(fc: Mapping) -> None:
    """CampaignStop("forecast_exceeds_budget") unless every budget fits: stop, do not shrink the design."""
    if not fc.get("fits"):
        failed = [k[5:] for k in ("fits_solver", "fits_sandbox", "fits_proposer") if not fc.get(k)]
        raise CampaignStop(reason="forecast_exceeds_budget", detail="does not fit: " + ", ".join(failed),
                           phase="qualification")


# ================================================================ attempts and records

def _attempt_numbers(paths: StatePaths) -> list[int]:
    try:
        names = [p.name for p in paths.qualification_root.iterdir() if p.is_dir()]
    except OSError:
        return []
    return sorted(int(m.group(1)) for m in map(_ATTEMPT_RE.fullmatch, names) if m)


def next_attempt(paths: StatePaths) -> int:
    """1 + the highest qualification/attempt-<n> present; 1 when there is none."""
    numbers = _attempt_numbers(paths)
    return numbers[-1] + 1 if numbers else 1


def record(paths: StatePaths, attempt: int, *, engine: Mapping, manifest_sha256: str, check_list: Sequence[Mapping],
           obs: Mapping, fc: Mapping, environment: Mapping | None = None, imported_from: Mapping | None = None,
           clock: util.Clock | None = None) -> dict:
    """Writes attempt-<n>/qualification.json and forecast.json; passed.json when every check passed and the forecast fits
    (an imported qualification brings no forecast of its own). Returns the qualification record."""
    utc = util.utc_now(clock)
    ok = bool(check_list) and all(c.get("ok") is True for c in check_list)
    fits = fc.get("fits") is True or (imported_from is not None and not fc)
    passed = ok and fits
    directory = paths.qualification_dir(attempt)
    doc = {"schema": QUALIFICATION_SCHEMA, "attempt": attempt, "utc": utc, "engine_sha256": engine["engine_sha256"],
           "engine": {"engine_sha256": engine["engine_sha256"], "files": dict(engine.get("files") or {})},
           "manifest_sha256": manifest_sha256, "checks": [dict(c) for c in check_list], "observations": dict(obs),
           "forecast_fits": fc.get("fits"), "passed": passed,
           "imported_from": None if imported_from is None else dict(imported_from)}
    util.write_json(directory / "qualification.json", doc, fsync=True)
    util.write_json(directory / "forecast.json", {"schema": FORECAST_SCHEMA, "attempt": attempt, "utc": utc, **fc},
                    fsync=True)
    if passed and environment is None and imported_from is not None:   # an import carries the source's environment
        try:
            source = paths.sibling(str(imported_from.get("campaign")))
        except ValueError:
            source = None
        environment = qualified_environment(source) if source is not None else None
    if passed:
        util.write_json(paths.qualification_passed, {
            "schema": PASSED_SCHEMA, "engine_sha256": engine["engine_sha256"], "manifest_sha256": manifest_sha256,
            "attempt": attempt, "utc": utc, "environment": None if environment is None else dict(environment),
            "imported_from": None if imported_from is None else dict(imported_from)}, fsync=True)
    return doc




def import_problems(manifest: dict, paths: StatePaths, engine: Mapping) -> list[str]:
    """Why this campaign may not import the qualification of qualification.import_from; [] = it may (3.22)."""
    source = util.lookup(manifest, "qualification.import_from")
    out: list[str] = []
    if manifest.get("campaign_kind") != "measured":
        out.append("campaign_kind: only a measured campaign imports a qualification")
    if source is None:
        return out + ["qualification.import_from: null (this campaign qualifies itself)"]
    try:
        src = paths.sibling(source)
    except ValueError:
        return out + ["qualification.import_from: not a campaign name"]
    src_manifest = _doc(src.manifest)
    if src_manifest is None:
        return out + [f"{source}: no readable frozen manifest"]
    if src_manifest.get("campaign_kind") != "preflight":
        out.append(f"{source}: not a preflight campaign")
    passed = _doc(src.qualification_passed)
    if passed is None:
        out.append(f"{source}: qualification/passed.json missing")
    else:
        if passed.get("manifest_sha256") != manifest_mod.manifest_sha256(src_manifest):
            out.append(f"{source}: passed.json manifest_sha256 differs from its frozen manifest")
        if passed.get("engine_sha256") != engine.get("engine_sha256"):
            out.append(f"{source}: qualified with another engine (engine_sha256 differs from the engine now)")
    if util.lookup(src_manifest, "decisions.project", None) == util.lookup(manifest, "decisions.project"):
        out.append("decisions.project: the measured campaign needs a fresh project")
    numbers = _attempt_numbers(src)
    rec = _doc(src.qualification_dir(numbers[-1]) / "reconciliation.json") if numbers else None
    verdict = (rec or {}).get("verdict")
    if verdict not in ("pass", "waived"):
        out.append(f"{source}: reconciliation of its last preflight attempt is {verdict or 'missing'} "
                   "(needs pass or waived)")
    for path in manifest_mod.diff_paths(src_manifest, manifest):
        if not manifest_mod.path_allowed(path, manifest_mod.IMPORT_DIFF_ALLOWED):
            out.append(f"{path}: differs from {source} and is not an allowed import difference")
    return out


def verify_qualified(paths: StatePaths, manifest: dict, engine: Mapping) -> None:
    """CampaignStop("qualification_failed") unless passed.json matches this manifest and this engine (and, for an
    imported qualification, the import conditions still hold)."""
    passed = _doc(paths.qualification_passed)
    reasons = []
    if passed is None:
        reasons.append("qualification/passed.json missing")
    else:
        if passed.get("manifest_sha256") != manifest_mod.manifest_sha256(manifest):
            reasons.append("manifest_sha256 differs")
        if passed.get("engine_sha256") != engine.get("engine_sha256"):
            reasons.append("engine changed: a new qualification attempt is needed")
        if passed.get("imported_from") is not None:
            reasons += import_problems(manifest, paths, engine)
    if reasons:
        raise CampaignStop(reason="qualification_failed", detail="; ".join(reasons)[:500], phase="qualification")


def qualified_environment(paths: StatePaths) -> dict | None:
    """passed.json["environment"], or None."""
    passed = _doc(paths.qualification_passed)
    env = (passed or {}).get("environment")
    return dict(env) if isinstance(env, Mapping) else None


def calibration_verdict(manifest: dict, paths: StatePaths) -> tuple[str | None, str]:
    """(verdict, where) of the calibration record that stands for this campaign (2.3, LD3): this campaign's own; else,
    with qualification.import_from, the source's, unless passed.json's import record lists a calibration. difference."""
    own = _doc(paths.calibration)
    if own is not None or paths.calibration.exists():
        return (own or {}).get("verdict"), "this campaign"
    source = util.lookup(manifest, "qualification.import_from")
    if source is None:
        return None, "this campaign"
    imported = (_doc(paths.qualification_passed) or {}).get("imported_from")
    if not isinstance(imported, Mapping) or not isinstance(imported.get("diff_paths"), list):
        return None, "this campaign (no import record)"
    if any(isinstance(p, str) and p.startswith("calibration.") for p in imported["diff_paths"]):
        return None, "this campaign (calibration fields differ from the imported campaign)"
    return (_doc(paths.sibling(source).calibration) or {}).get("verdict"), f"imported campaign {source}"


# ================================================================ noise floor

def _final_evals(rows: Sequence[Mapping]) -> dict[int, Mapping]:
    """index -> the eval row of the wanted split with the highest pass (then the latest seq), as the join keeps it."""
    sidecar = parse_sidecar(rows)
    wanted = ((sidecar.get("splits") or {}).get("wanted") or ["val"])
    split = wanted[0] if isinstance(wanted, list) and wanted else "val"
    keep: dict[int, Mapping] = {}
    for ev in sidecar["evals"]:
        if ev.get("split") != split or not _is_int(ev.get("index")):
            continue
        prev = keep.get(ev["index"])
        if prev is None or (ev["pass"], ev["seq"]) > (prev["pass"], prev["seq"]):
            keep[ev["index"]] = ev
    return keep


def _answer(ev: Mapping) -> object:
    return ev.get("prediction_sha256") or ev.get("prediction")


def noise_stats(sidecars: Mapping[str, Mapping[str, Mapping[int, Sequence[Mapping]]]], results: Mapping) -> dict:
    """Pure: system -> dataset -> repeat -> sidecar rows, and results (system -> dataset -> repeat -> result file) ->
    the body of noise.json (2.4) without its utc. Correct counts come from the sidecars; a result file's `total` is the
    denominator when present (else the number of evaluated examples); a result whose `correct` disagrees is a problem."""
    per_dataset: dict = {}
    macro: dict[str, list[float]] = {}
    problems: list[str] = []
    identical = True
    all_repeats: set[int] = set()
    for system in sorted(sidecars):
        by_ds = sidecars[system]
        repeats = sorted({k for ds in by_ds for k in by_ds[ds]})
        all_repeats.update(repeats)
        acc: dict[int, list[float]] = {k: [] for k in repeats}
        for ds in sorted(by_ds):
            finals = {k: _final_evals(by_ds[ds][k]) for k in sorted(by_ds[ds])}
            keys = sorted(finals)
            if keys != repeats:                            # the macro of a missing repeat would cover fewer datasets
                identical = False
                problems.append(f"{system}/{ds}: repeats {keys} of {repeats}")
            union = sorted(set().union(*[set(v) for v in finals.values()])) if finals else []
            common = [i for i in union if all(i in finals[k] for k in keys)]
            correct, totals = [], []
            for k in keys:
                n_ok = sum(1 for ev in finals[k].values() if ev.get("was_correct") is True)
                res = (((results.get(system) or {}).get(ds) or {}).get(k)) if isinstance(results, Mapping) else None
                total = res.get("total") if isinstance(res, Mapping) and _is_int(res.get("total")) else len(finals[k])
                if isinstance(res, Mapping) and _is_int(res.get("correct")) and res["correct"] != n_ok:
                    problems.append(f"{system}/{ds}/r{k}: result correct {res['correct']} != sidecar {n_ok}")
                correct.append(n_ok)
                totals.append(total)
                acc.setdefault(k, []).append(100.0 * n_ok / total if total else 0.0)
            agree = sum(1 for i in common if len({finals[k][i].get("was_correct") is True for k in keys}) == 1)
            same = len(common) == len(union) and all(len({_answer(finals[k][i]) for k in keys}) == 1 for i in common)
            identical = identical and same and agree == len(union)
            per_dataset.setdefault(ds, {})[system] = {
                "correct": correct, "total": max(totals) if totals else 0, "totals": totals,
                "flip_rate": round((len(common) - agree) / len(common), 6) if common else 0.0,
                "all_agree": agree, "examples": len(union), "examples_in_every_repeat": len(common)}
        macro[system] = [round(sum(v) / len(v), 6) if v else 0.0 for _, v in sorted(acc.items())]
    return {"schema": "mhforge.noise/1", "repeats": len(all_repeats), "systems": sorted(sidecars),
            "per_dataset": per_dataset, "macro_validation_accuracy": macro,
            "macro_std": {s: round(statistics.stdev(v), 6) if len(v) >= 2 else 0.0 for s, v in macro.items()},
            "identical_all_repeats": bool(identical and sidecars), "problems": problems}


# ================================================================ restart

def restart_allowed(paths: StatePaths, ledger_snapshot: Mapping, engine_now: Mapping) -> list[str]:
    """Reasons a new qualification attempt is refused; [] = allowed (H2 rule: a recorded stop in the qualification
    phase, nothing pending or unknown in the ledger, at least one engine file changed, the cap not yet used up)."""
    out: list[str] = []
    stopped = _doc(paths.stopped)
    if stopped is None:
        out.append("no recorded stop (stopped.json)")
    elif stopped.get("phase") != "qualification":
        out.append(f"the recorded stop is in phase {stopped.get('phase')!r}, not qualification")
    if paths.qualification_passed.exists():
        out.append("the campaign is already qualified")
    pending = ledger_snapshot.get("pending_requests", 0) or 0
    if pending or (ledger_snapshot.get("in_flight_micro") or 0) > 0:
        out.append("pending ledger rows: wait for the drain or restart the runner")
    if ledger_snapshot.get("unknown_requests"):
        out.append("unknown ledger rows: reconcile them with status --reconcile")
    committed = (ledger_snapshot.get("committed_by_phase") or {}).get("qualification", 0)
    cap = (ledger_snapshot.get("caps_micro") or {}).get("qualification")
    if cap is not None and committed >= cap:
        out.append("the qualification cap is used up (it covers all attempts together)")
    numbers = _attempt_numbers(paths)
    before = None
    for n in reversed(numbers):
        doc = _doc(paths.qualification_dir(n) / "qualification.json")
        if doc is not None and isinstance((doc.get("engine") or {}).get("files"), Mapping):
            before = doc["engine"]["files"]
            break
    if before is None:
        frozen = _doc(paths.engine)
        before = (frozen or {}).get("files") if isinstance((frozen or {}).get("files"), Mapping) else None
    if before is None:
        out.append("no earlier engine snapshot to compare with")
    else:
        changed = util.changed_files(before, engine_now.get("files") or {})
        if not any(changed.values()):
            out.append("no engine file changed since the last attempt: a restart needs a repair")
    return out
