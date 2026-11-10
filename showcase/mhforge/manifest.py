# SPDX-License-Identifier: Apache-2.0
# Adapted from ash0ts/harness2-wandb@31b0f82 harness2/showcase/runtime.py L55-L62 (freeze rule) and study.py L35-L40
# (read-only read). Changes: closed schema with path-naming problems instead of the first ValueError; the freeze writes a
# canonical copy, its hash and the engine snapshot and refuses a null upstream.tree_sha256; read-only read and frozen
# load are separate; plus the live gates, amendments chain, import diff and run-config selection of spec 2.3 and 3.3.
"""Manifest schema, validation, live gates, freeze and drift check, amendments, import diff (spec 2.3, 3.3)."""

from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import urlsplit

from . import procenv, scan, util, wire
from .errors import LiveGateError, ManifestError, StateError
from .paths import StatePaths, resolve_state
from .util import Clock

__all__ = [
    "read_manifest", "validate", "require_valid", "manifest_sha256", "freeze", "load_frozen", "live_blockers",
    "require_live", "prices", "phase_caps_micro", "AMENDABLE", "IMPORT_DIFF_ALLOWED", "diff_paths", "read_amendments",
    "append_amendment", "effective", "path_allowed", "run_config", "max_reservation_micro", "openai_project", "COMMANDS", "EVALUATE_MODES",
]

MANIFEST_CHANGED = "Manifest changed: start a new campaign instead of mixing attempts"
COMMANDS = ("account-check", "preflight", "search", "evaluate", "export", "publish")
RUNNER_COMMANDS = ("preflight", "search", "evaluate")
EVALUATE_MODES = ("baselines", "calibration", "noise", "test")
FORGE_BASE_URL = "https://api.inference.wandb.ai/v1"
RESERVED_KEY_NAMES = ("WANDB_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY")
EXPORT_MUST_BE_UNSET = ("WANDB_BASE_URL", "CES_BASE_URL", "WANDB_APP_URL", "WANDB_MODE", "OTEL_RESOURCE_ATTRIBUTES",
                        "WF_TRACE_SERVER_URL", "WANDB_IDENTITY_TOKEN_FILE", "OTEL_EXPORTER_OTLP_ENDPOINT")
WANDB_AUTH_MUST_BE_UNSET = ("WANDB_BASE_URL", "WANDB_IDENTITY_TOKEN_FILE", "WANDB_ENTITY", "WANDB_PROJECT")
# LD7: cwsandbox 1.27.0 WandbAuth resolves through wandb's own chain (_auth.py:126-195), so a live sandbox would not
# be pinned to the key this package was given. Validation still accepts the mode (offline tests and fakes use it).
WANDB_AUTH_REFUSED = ("wandb_auth is refused live: cwsandbox's WandbAuth takes W&B's session credential and host from "
                      "wandb's own settings, not the key given here, so sandboxes could run under another identity; "
                      "set headers (explicit AuthHeaders with the given key)")
CLAUDE_SESSION_NAMES = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")
UPSTREAM_DEFAULT_SEED = 42                                   # benchmark.py L35: run_dir adds _seed<N> otherwise

AMENDABLE: tuple[str, ...] = ("telemetry.content.", "decisions.dataset_text_publication", "decisions.uspto_text_cleared")
IMPORT_DIFF_ALLOWED: tuple[str, ...] = (
    "notes", "campaign", "campaign_kind", "state_dir", "qualification.import_from",
    "decisions.project", "decisions.publish_approved", "decisions.evaltables_backend",
    "decisions.dataset_text_publication", "decisions.uspto_text_cleared",
    "budgets.", "accounting.reconcile_tolerance", "gateway.max_in_flight",
    "search.propose_timeout_s", "executor.job_timeout_s", "executor.import_check_timeout_s",
    "provider.connect_timeout_s", "provider.first_byte_timeout_s", "provider.idle_timeout_s",
    "provider.attempt_deadline_s", "sandbox.create_wait_s", "sandbox.lifetime_margin_s", "sandbox.setup_allowance_s",
    "relay.reply_timeout_s", "telemetry.export_timeout_s", "search.iterations", "executor.parallel_dataset_jobs.",
    "provider.expected_response_model", "proposer.model", "proposer.expected_resolved_model",
    "proposer.max_budget_usd_per_session", "proposer.session_estimate_usd", "proposer.max_limit_wait_s",
    "proposer.transient_retries", "proposer.transient_backoff_s", "proposer.list_prices.", "telemetry.content.",
    "telemetry.agent_lens.byte_estimate_factor", "telemetry.export_mode", "telemetry.wandb_group",
    "calibration.", "forecast.", "runner.")

_KEY_ENV_RE = re.compile(r"[A-Z][A-Z0-9_]{2,63}")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_ENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_IMAGE_RE = re.compile(r"[a-z0-9.-]+(:[0-9]+)?/.+@sha256:[0-9a-f]{64}")
_PREFIX_RE = re.compile(r"[a-z][a-z0-9_]*")
_VERSION_RE = re.compile(r"[0-9]+(\.[0-9]+){1,3}")
_TRUNCATE_RE = re.compile(r"truncate:([0-9]{1,9})")
_PRINTABLE_RE = re.compile(r"[\x20-\x7e]*")
_ENV_DENY_EXACT = ("SSH_AUTH_SOCK", "NETRC", "BASH_ENV", "ENV", "NODE_OPTIONS", "KUBECONFIG")
_ENV_DENY_PREFIX = ("OPENROUTER_", "OPENAI_", "WANDB_", "ANTHROPIC_", "CLAUDE", "OTEL_", "TEXT_CLASSIFICATION_",
                    "MHFORGE_", "LITELLM_", "DYLD_", "LD_", "AWS_", "GOOGLE_", "AZURE_", "GH_", "HF_", "DOCKER_")


# ================================================================ schema (rule 1)

def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: object) -> bool:
    return (_is_int(v) or isinstance(v, float)) and v == v and v not in (float("inf"), float("-inf"))


def _dec(v: object) -> Decimal | None:
    if not isinstance(v, str):
        return None
    try:
        d = Decimal(v.strip())
    except InvalidOperation:
        return None
    return d if d.is_finite() else None


def _leaf(pred, what: str, null: bool = False):
    def check(v, path, out):
        if v is None and null:
            return
        if not pred(v):
            out.append(f"{path}: must be {what}" + (" or null" if null else ""))
    return check


def _int(lo: int | None = None, hi: int | None = None, null: bool = False):
    what = "an integer" + (f" >= {lo}" if lo is not None else "") + (f" <= {hi}" if hi is not None else "")
    return _leaf(lambda v: _is_int(v) and (lo is None or v >= lo) and (hi is None or v <= hi), what, null)


def _enum(*choices):
    return _leaf(lambda v: any(type(v) is type(c) and v == c for c in choices), "one of " + ", ".join(map(repr, choices)))


def _map(sub):
    def check(v, path, out):
        if not isinstance(v, dict):
            out.append(f"{path}: must be an object")
            return
        for k, item in v.items():
            _walk(sub, item, f"{path}.{k}", out)
    return check


STR = _leaf(lambda v: isinstance(v, str), "a string")
STR_N = _leaf(lambda v: isinstance(v, str), "a string", null=True)
BOOL = _leaf(lambda v: isinstance(v, bool), "true or false")
NUM = _leaf(_is_num, "a number")
DEC = _leaf(lambda v: _dec(v) is not None and _dec(v) >= 0, "a non-negative decimal string")
DEC_N = _leaf(lambda v: _dec(v) is not None and _dec(v) >= 0, "a non-negative decimal string", null=True)
HEX64_N = _leaf(lambda v: isinstance(v, str) and bool(_HEX64_RE.fullmatch(v)), "64 lower-case hex", null=True)
INT0, INT1 = _int(0), _int(1)
INT1_N = _int(1, null=True)
STRLIST = _leaf(lambda v: isinstance(v, list) and all(isinstance(i, str) for i in v), "a list of strings")
STRLIST_N = _leaf(lambda v: isinstance(v, list) and all(isinstance(i, str) for i in v), "a list of strings", null=True)
RANGE = _leaf(lambda v: isinstance(v, list) and len(v) == 2 and all(_is_num(i) for i in v) and 0 <= v[0] <= v[1],
              "[lo, hi] with 0 <= lo <= hi")
INTPAIR = _leaf(lambda v: isinstance(v, list) and len(v) == 2 and all(_is_int(i) and i >= 0 for i in v),
                "[int, int] of non-negative integers")
OBJ_N = _leaf(lambda v: isinstance(v, dict), "a JSON object", null=True)
SIZES = _leaf(lambda v: isinstance(v, list) and len(v) == 3 and all(_is_int(i) and i >= 0 for i in v),
              "[train, val, test] non-negative integers")
PER_SPLIT = {"val": _map(INT0), "test": _map(INT0)}

SCHEMA: dict = {
    "schema": _enum("mhforge.manifest/1"), "notes": STR, "campaign": STR, "campaign_kind": _enum(*wire.CAMPAIGN_KINDS),
    "state_dir": STR,
    "decisions": {
        "live_spend_approved": BOOL, "key_provisioning": _enum("unset", "dedicated"), "entity": STR_N, "project": STR_N,
        "proposer_permission_mode": _enum("unset", "upstream", "restricted", "contained"),
        "evaltables_backend": _enum("unset", "ces", "wandb_table", "off"), "publish_approved": BOOL,
        "dataset_text_publication": _enum("unset", "identifiers_only", "per_dataset"), "uspto_text_cleared": BOOL},
    "upstream": {
        "repo": STR, "commit": _leaf(lambda v: isinstance(v, str) and bool(re.fullmatch(r"[0-9a-f]{40}", v)), "40 hex"),
        "example_dir": STR, "tree_sha256": HEX64_N, "worker_lock_sha256": HEX64_N, "worker_python": STR,
        "expect": {"model": STR, "model_short": STR, "mode": STR, "num_epochs": INT1, "temperature": NUM, "seed": INT0,
                   "max_tokens": INT1, "system_prompt": STR, "datasets": STRLIST, "dataset_dirs": _map(STR),
                   "baselines": STRLIST, "sizes": _map(SIZES)}},
    "search": {"iterations": INT0, "propose_timeout_s": INT1, "max_candidates_per_iteration": INT1,
               "partial_candidate_policy": _enum("upstream", "quarantine")},
    "routing": {"route": _enum(2, 1), "route1_model": STR, "reply_reuse_scope": _enum("search", "job")},
    "provider": {
        "kind": _enum("forge", "stub"), "base_url": STR, "wire_model": STR, "api_key_env": STR, "openai_project": STR_N,
        "user_agent": STR, "connect_timeout_s": INT1, "first_byte_timeout_s": INT1, "idle_timeout_s": INT1,
        "attempt_deadline_s": INT1, "max_response_bytes": INT1, "expected_response_model": STR_N,
        "retry": {"max_transport_retries": INT0, "backoff_429_s": RANGE, "backoff_5xx_s": RANGE, "backoff_other_s": RANGE},
        "circuit": {"failures": INT1, "cooldown_s": INT0}, "terminal_429_patterns": STRLIST},
    "prices": {"model": STR, "input_per_million": STR, "cached_per_million": STR, "output_per_million": STR,
               "source_url": STR, "checked_utc": STR},
    "gateway": {"max_in_flight": _int(1, 64), "max_request_bytes": INT1, "max_content_bytes": INT1,
                "max_output_tokens": INT1, "model_context_tokens": INT1, "template_overhead_tokens": INT0,
                "max_messages": INT1, "per_message_overhead_tokens": INT0, "max_requests_per_job": INT1,
                "max_job_usd": DEC, "max_refused_records_per_job": INT0, "loopback_backlog": INT1},
    "budgets": {"solver_usd": {"total": DEC, "phases": {p: DEC for p in wire.LEDGER_PHASES}}, "sandbox_usd": DEC,
                "lens_ingest_mb": INT0, "lens_ingest_split_mb": _map(INT0), "lens_proposer_session_mb": INT0,
                "wandb_upload_gb": INT0, "eval_tables_max": INT0, "proposer_list_usd": DEC_N},
    "accounting": {"missing_usage": _enum("charge_reservation"), "forecast_contingency": DEC, "reconcile_tolerance": STR},
    "forecast": {"candidates_per_iteration": INT1, "test_systems": INT0,
                 "output_tokens_quantile": _leaf(lambda v: _is_num(v) and 0 < v <= 1, "a number in (0, 1]"),
                 "calls_per_job": PER_SPLIT, "input_tokens_per_job": PER_SPLIT, "job_seconds": PER_SPLIT},
    "calibration": {"systems": STRLIST_N,
                    "gates": _map({"system": STR, "hard": BOOL, "macro_test_accuracy": RANGE, "correct": _map(RANGE),
                                   "content_on_every_call": BOOL}),
                    "gate_search": BOOL, "noise_repeats": INT1},
    "executor": {"kind": _enum("sandbox", "local_relay", "local"), "allow_unisolated_live": BOOL,
                 "max_attempts": INT1, "max_infra_attempts": INT1, "job_timeout_s": INT1,
                 "parallel_dataset_jobs": _map(_int()), "import_check_timeout_s": INT1},
    "sandbox": {
        "env_mode": STR, "image": STR_N, "base_image": STR_N, "base_image_fallback": STR_N,
        "wheelhouse": {"tar_path": STR, "tar_sha256": HEX64_N, "tar_bytes": INT1_N, "sha256sums_sha256": HEX64_N,
                       "requirements_sha256": HEX64_N},
        "expect": {"machine": STR, "python": STR}, "cpu": STR, "memory": STR, "auth_env": STR,
        "auth_mode": _enum("headers", "wandb_auth"), "price_cpu_hour": DEC, "price_gib_hour": DEC,
        "lifetime_margin_s": INT0, "setup_allowance_s": INT0, "create_wait_s": INT1, "work_dir": STR, "python": STR,
        "tiktoken_cache_dir": STR_N, "object_store_endpoint": STR_N, "copy_back_cache": BOOL,
        "selfcheck": _enum("every_job", "first_job")},
    "relay": {"mode": _enum("daemon", "stdio"), "rotate_after_mb": INT1, "max_stream_mb": INT1, "max_job_stream_mb": INT1,
              "reply_timeout_s": INT1, "ping_interval_s": _int(), "pong_timeout_s": _int(), "max_workers": INT1,
              "write_chunk_bytes": INT1, "done_cache_max": INT1, "reattach_attempts": INT0, "reattach_backoff_s": RANGE},
    "capture": {"enabled": BOOL, "max_text_chars": INT1},
    "proposer": {
        "cli_path": STR_N, "cli_sha256": HEX64_N, "cli_version": STR_N, "model": STR, "expected_resolved_model": STR_N,
        "effort": STR, "max_budget_usd_per_session": DEC_N, "grace_s": INTPAIR, "env_allow": STRLIST,
        "env_set": _map(STR), "path_dirs": STRLIST, "scratch_root": STR, "restricted": {"settings": OBJ_N},
        "usage_limit_patterns": STRLIST, "auth_patterns": STRLIST, "transient_retries": INT0,
        "transient_backoff_s": RANGE, "max_limit_wait_s": INT0, "session_estimate_usd": DEC_N,
        "list_prices": _map({k: DEC for k in ("input_per_million", "output_per_million", "cache_read_per_million",
                                              "cache_write_5m_per_million", "cache_write_1h_per_million")}),
        "max_transcript_bytes": INT1},
    "telemetry": {
        "api_key_env": STR, "export_mode": _enum("manual", "after_unit"), "export_timeout_s": INT1,
        "attribute_prefix": STR,
        "agent_lens": {"enabled": BOOL, "service_name": STR, "max_spans_per_conversation": INT1,
                       "loop_iterations_per_conversation": INT1, "readback_timeout_s": INT1, "readback_poll_s": INT1,
                       "export_batch_size": INT1, "byte_estimate_factor": STR},
        "content": _map({f: STR for f in wire.CONTENT_FIELDS}),
        "verbatim_scan": {"run_chars": INT1, "exact_datasets": STRLIST}, "wandb_group": STR_N},
    "qualification": {"system": STR, "dataset": STR,
                      "max_truncated_empty_rate": _leaf(lambda v: _is_num(v) and 0 <= v <= 1, "a number in [0, 1]"),
                      "import_from": STR_N},
    "runner": {"keep_awake": BOOL, "stall_alarm_s": _int(), "sleep_gap_s": _int()},
}
_OPTIONAL = frozenset({"notes"})


def _walk(spec, value, path: str, out: list[str]) -> None:
    if not isinstance(spec, dict):
        spec(value, path, out)
        return
    if not isinstance(value, dict):
        out.append(f"{path or 'manifest'}: must be an object")
        return
    for key in value:
        if key not in spec:
            out.append(f"{path + '.' if path else ''}{key}: unknown key")
    for key, sub in spec.items():
        where = f"{path}.{key}" if path else key
        if key not in value:
            if not (path == "" and key in _OPTIONAL):
                out.append(f"{where}: required")
            continue
        _walk(sub, value[key], where, out)


# ================================================================ semantic rules (2.3 rules 1 to 21)

def _l(m: Mapping, dotted: str):
    return util.lookup(m, dotted)


def _dataset_size(m: Mapping, ds: str, split: int) -> int:
    return _l(m, "upstream.expect.sizes")[ds][split]


def _rules(m: dict, sc_dir: Path) -> list[str]:
    out: list[str] = []
    up, prov, dec = m["upstream"], m["provider"], m["decisions"]
    datasets = list(_l(m, "upstream.expect.datasets"))
    dset = set(datasets)
    # rule 1, cross-field shape rules
    if not datasets or len(dset) != len(datasets) or not all(wire.DATASET_RE.fullmatch(d) for d in datasets):
        out.append("upstream.expect.datasets: must be distinct dataset names")
    for where in ("upstream.expect.dataset_dirs", "upstream.expect.sizes", "forecast.calls_per_job.val",
                  "forecast.calls_per_job.test", "forecast.input_tokens_per_job.val", "forecast.input_tokens_per_job.test",
                  "forecast.job_seconds.val", "forecast.job_seconds.test"):
        if set(_l(m, where)) != dset:
            out.append(f"{where}: keys must be exactly upstream.expect.datasets")
    baselines = _l(m, "upstream.expect.baselines")
    if not baselines or not all(wire.CANDIDATE_RE.fullmatch(b) for b in baselines):
        out.append("upstream.expect.baselines: must be candidate names")
    if _l(m, "qualification.system") not in baselines:
        out.append("qualification.system: must be one of upstream.expect.baselines")
    if _l(m, "qualification.dataset") not in dset:
        out.append("qualification.dataset: must be one of upstream.expect.datasets")
    for i, entry in enumerate(_l(m, "proposer.path_dirs")):
        out += _placeholder_problems(entry, f"proposer.path_dirs[{i}]")
    for name, value in _l(m, "proposer.env_set").items():
        out += _placeholder_problems(value, f"proposer.env_set.{name}")
    # rule 2
    if not wire.CAMPAIGN_RE.fullmatch(m["campaign"]):
        out.append("campaign: must fully match [a-z0-9][a-z0-9-]{2,39}")
    if m["state_dir"] != "state/" + m["campaign"]:
        out.append(f"state_dir: must be the literal 'state/{m['campaign']}'")
    # rule 3
    if prov["kind"] == "forge" and prov["base_url"] != FORGE_BASE_URL:
        out.append(f"provider.base_url: must be exactly {FORGE_BASE_URL} for kind forge")
    if prov["kind"] == "stub":
        try:
            parts = urlsplit(prov["base_url"])
            ok = parts.scheme == "http" and parts.hostname == "127.0.0.1" and parts.port is not None
        except ValueError:
            ok = False
        if not ok:
            out.append("provider.base_url: kind stub needs http://127.0.0.1:<port>/v1")
    # rule 4
    pr = m["prices"]
    if pr["model"] != prov["wire_model"]:
        out.append("prices.model: must equal provider.wire_model")
    parsed = {k: _dec(pr[k]) for k in ("input_per_million", "cached_per_million", "output_per_million")}
    for k, d in parsed.items():
        if d is None or d < 0:
            out.append(f"prices.{k}: must be a non-negative decimal string")
    if None not in parsed.values() and parsed["cached_per_million"] > parsed["input_per_million"]:
        out.append("prices.cached_per_million: must not exceed prices.input_per_million")
    # rule 5
    phases = _l(m, "budgets.solver_usd.phases")
    total = util.micros(_l(m, "budgets.solver_usd.total"))
    if sum(util.micros(v) for v in phases.values()) > total:
        out.append("budgets.solver_usd.phases: the phase caps sum to more than budgets.solver_usd.total")
    if None not in parsed.values():
        floor = 2 * _l(m, "gateway.max_in_flight") * max_reservation_micro(m)
        for p, v in phases.items():
            if 0 < util.micros(v) < floor:
                out.append(f"budgets.solver_usd.phases.{p}: a non-zero cap must be at least "
                           f"2 * gateway.max_in_flight * R = {util.usd(floor)} USD")
        if util.micros(_l(m, "gateway.max_job_usd")) < floor:
            out.append(f"gateway.max_job_usd: must be at least 2 * gateway.max_in_flight * R = {util.usd(floor)} USD")
    # rule 6
    gw = m["gateway"]
    if gw["max_output_tokens"] < _l(m, "upstream.expect.max_tokens"):
        out.append("gateway.max_output_tokens: must be at least upstream.expect.max_tokens")
    if gw["max_content_bytes"] < 900000:
        out.append("gateway.max_content_bytes: must be at least 900000")
    if gw["max_request_bytes"] < 2750000:
        out.append("gateway.max_request_bytes: must be at least 2750000")
    if gw["max_messages"] < 2:
        out.append("gateway.max_messages: must be at least 2")
    # rule 7
    lens = _l(m, "telemetry.agent_lens")
    if lens["max_spans_per_conversation"] > 199:
        out.append("telemetry.agent_lens.max_spans_per_conversation: must be at most 199")
    if not _PREFIX_RE.fullmatch(_l(m, "telemetry.attribute_prefix")):
        out.append("telemetry.attribute_prefix: must fully match [a-z][a-z0-9_]*")
    # rule 8
    env_allow, env_set = _l(m, "proposer.env_allow"), _l(m, "proposer.env_set")
    key_paths = ("provider.api_key_env", "telemetry.api_key_env", "sandbox.auth_env")
    names = [_l(m, p) for p in key_paths]
    for p, name in zip(key_paths, names):
        if not _KEY_ENV_RE.fullmatch(name):
            out.append(f"{p}: must fully match [A-Z][A-Z0-9_]{{2,63}}")
        if name in RESERVED_KEY_NAMES:
            out.append(f"{p}: must not be one of {', '.join(RESERVED_KEY_NAMES)}")
        if not procenv.is_credential_name(name) or name in wire.CREDENTIAL_NAME_EXEMPT:
            out.append(f"{p}: a key variable must have a credential-shaped name (wire.CREDENTIAL_NAME_PATTERN) and must"
                       f" not be in wire.CREDENTIAL_NAME_EXEMPT")
        if name in env_allow or name in env_set:
            out.append(f"{p}: a key variable must not appear in proposer.env_allow or proposer.env_set")
    if len(set(names)) != len(names):
        out.append("provider.api_key_env: the three key variable names must be pairwise distinct")
    # rule 9
    for where, name in [(f"proposer.env_allow[{i}]", n) for i, n in enumerate(env_allow)] + \
                       [(f"proposer.env_set.{n}", n) for n in env_set]:
        reason = _env_name_problem(name)
        if reason:
            out.append(f"{where}: {reason}")
    # rule 10
    sb = m["sandbox"]
    if sb["env_mode"] in ("snapshot", "install_then_deny"):
        out.append(f"sandbox.env_mode: {sb['env_mode']} is deferred (8.3)")
    elif sb["env_mode"] not in ("image", "wheelhouse"):
        out.append("sandbox.env_mode: must be image or wheelhouse")
    for key in ("image", "base_image"):
        if sb[key] is not None:
            if not _IMAGE_RE.fullmatch(sb[key]):
                out.append(f"sandbox.{key}: must be <registry>/<name>@sha256:<64 hex> (the amd64 manifest digest)")
            elif _on_docker_hub(sb[key]):
                out.append(f"sandbox.{key}: must not be on docker.io")
    if sb["base_image_fallback"] is not None:
        fb = sb["base_image_fallback"]
        if not _IMAGE_RE.fullmatch(fb):
            out.append("sandbox.base_image_fallback: must be <registry>/<name>@sha256:<64 hex>")
        elif sb["base_image"] is None or fb.rsplit("@", 1)[1] != sb["base_image"].rsplit("@", 1)[1]:
            out.append("sandbox.base_image_fallback: must carry the same sha256 digest as sandbox.base_image")
    # rule 12
    for key in ("entity", "project"):
        if dec[key] is not None and not _ENTITY_RE.fullmatch(dec[key]):
            out.append(f"decisions.{key}: must fully match [A-Za-z0-9][A-Za-z0-9_.-]{{0,127}}")
    for where in ("provider.user_agent", "provider.openai_project", "telemetry.agent_lens.service_name"):
        v = _l(m, where)
        if v is not None and not _PRINTABLE_RE.fullmatch(v):
            out.append(f"{where}: must be printable ASCII (0x20 to 0x7E)")
    ua = prov["user_agent"]
    if not ua or ua.startswith(("Python-urllib", "python-requests")):
        out.append("provider.user_agent: must be non-empty and not start with Python-urllib or python-requests")
    # rule 13
    if lens["loop_iterations_per_conversation"] * (3 + 2 * _l(m, "search.max_candidates_per_iteration")) > \
            lens["max_spans_per_conversation"]:
        out.append("telemetry.agent_lens.loop_iterations_per_conversation: loop_iterations_per_conversation * "
                   "(3 + 2 * search.max_candidates_per_iteration) must not exceed max_spans_per_conversation")
    # rule 14
    rl = m["relay"]
    if not rl["pong_timeout_s"] > rl["ping_interval_s"] > 0:
        out.append("relay.pong_timeout_s: must satisfy pong_timeout_s > ping_interval_s > 0")
    if rl["max_job_stream_mb"] < rl["rotate_after_mb"]:
        out.append("relay.max_job_stream_mb: must be at least relay.rotate_after_mb")
    # rule 15
    cal = m["calibration"]
    systems = cal["systems"] if cal["systems"] is not None else baselines
    if not all(wire.CANDIDATE_RE.fullmatch(s) for s in systems):
        out.append("calibration.systems: must be candidate names")
    for name, gate in cal["gates"].items():
        if gate["system"] not in systems:
            out.append(f"calibration.gates.{name}.system: must be one of the calibration systems")
        if gate["macro_test_accuracy"][1] > 100:
            out.append(f"calibration.gates.{name}.macro_test_accuracy: must lie within 0..100")
        for ds, band in gate["correct"].items():
            if ds not in dset:
                out.append(f"calibration.gates.{name}.correct.{ds}: not one of upstream.expect.datasets")
            elif band[1] > _dataset_size(m, ds, 2):
                out.append(f"calibration.gates.{name}.correct.{ds}: upper bound exceeds the test size")
    if cal["gate_search"] and not any(g["hard"] for g in cal["gates"].values()):
        out.append("calibration.gate_search: needs at least one gate with hard: true")
    # rule 16
    out += _content_problems(m, dset)
    vs = _l(m, "telemetry.verbatim_scan")
    if vs["run_chars"] < 20:
        out.append("telemetry.verbatim_scan.run_chars: must be at least 20")
    if not set(vs["exact_datasets"]) <= dset:
        out.append("telemetry.verbatim_scan.exact_datasets: must be a subset of upstream.expect.datasets")
    # rule 17
    imp = _l(m, "qualification.import_from")
    if m["campaign_kind"] == "preflight" and imp is not None:
        out.append("qualification.import_from: a preflight campaign must not import a qualification")
    if imp is not None and (not wire.CAMPAIGN_RE.fullmatch(imp) or imp == m["campaign"]):
        out.append("qualification.import_from: must be another campaign name")
    # rule 18
    bud = m["budgets"]
    split = bud["lens_ingest_split_mb"]
    if set(split) != set(wire.LENS_BUCKETS):
        out.append("budgets.lens_ingest_split_mb: keys must be exactly main, preflight, recovery")
    elif sum(split.values()) != bud["lens_ingest_mb"]:
        out.append("budgets.lens_ingest_split_mb: the buckets must sum to budgets.lens_ingest_mb")
    for key in ("lens_proposer_session_mb", "wandb_upload_gb", "eval_tables_max"):
        if bud[key] < 1:
            out.append(f"budgets.{key}: must be at least 1")
    tol = _dec(_l(m, "accounting.reconcile_tolerance"))
    if tol is None or not 0 <= tol <= 1:
        out.append("accounting.reconcile_tolerance: must be a decimal in 0..1")
    # rule 19
    pdj = _l(m, "executor.parallel_dataset_jobs")
    if set(pdj) != {"validation", "test"}:
        out.append("executor.parallel_dataset_jobs: keys must be exactly validation and test")
    else:
        if pdj["test"] != 1:
            out.append("executor.parallel_dataset_jobs.test: must be 1")
        if not 1 <= pdj["validation"] <= 3:
            out.append("executor.parallel_dataset_jobs.validation: must be 1 to 3")
    # rule 20
    pp = m["proposer"]
    per_session = _dec(pp["max_budget_usd_per_session"]) if pp["max_budget_usd_per_session"] is not None else None
    if per_session is not None:
        if per_session <= 0:
            out.append("proposer.max_budget_usd_per_session: must be positive")
        elif bud["proposer_list_usd"] is not None and per_session > _dec(bud["proposer_list_usd"]):
            out.append("proposer.max_budget_usd_per_session: must not exceed budgets.proposer_list_usd")
    sr = pp["scratch_root"]
    if not os.path.isabs(sr) or ".." in Path(sr).parts or os.path.normpath(sr) != sr or os.path.realpath(sr) != sr:
        out.append("proposer.scratch_root: must be an absolute path in resolved form without ..")
    elif Path(sr) == sc_dir or sc_dir in Path(sr).parents:
        out.append("proposer.scratch_root: must not be inside the manifest's directory")
    if pp["cli_version"] is not None and not _VERSION_RE.fullmatch(pp["cli_version"]):
        out.append("proposer.cli_version: must fully match [0-9]+(\\.[0-9]+){1,3}")
    # rule 21
    bef = _dec(lens["byte_estimate_factor"])
    if bef is None or not Decimal("0.5") <= bef <= Decimal("4"):
        out.append("telemetry.agent_lens.byte_estimate_factor: must be a decimal in 0.5..4")
    for key in ("stall_alarm_s", "sleep_gap_s"):
        if m["runner"][key] <= 0:
            out.append(f"runner.{key}: must be positive")
    return out


def _placeholder_problems(text: str, where: str) -> list[str]:
    bad = [p for p in re.findall(r"\{[^{}]*\}", text) if p not in procenv.PLACEHOLDERS]
    return [f"{where}: unknown placeholder {bad[0]}"] if bad else []


def _env_name_problem(name: str) -> str | None:
    if not _ENV_NAME_RE.fullmatch(name):
        return "not a variable name"
    if procenv.is_credential_name(name):
        return "credential-shaped name"
    if name in _ENV_DENY_EXACT or name.upper().endswith("_PROXY") or name.startswith(_ENV_DENY_PREFIX):
        return "name that must not reach a proposer"
    if name in procenv.PROPOSER_DENY_NAMES:
        return "name on procenv.PROPOSER_DENY_NAMES"
    if name in procenv.PROPOSER_FIXED_ENV:
        return "name of the closed list procenv.PROPOSER_FIXED_ENV (set by code, never by the manifest)"
    return None


def _on_docker_hub(image: str) -> bool:
    first = image.split("/", 1)[0]
    if "." not in first and ":" not in first and first != "localhost":
        return True                                          # no registry host: a Docker Hub namespace (docker's rule)
    host = first.split(":", 1)[0]
    return host == "docker.io" or host.endswith(".docker.io")


def _content_problems(m: dict, dset: set[str]) -> list[str]:
    out: list[str] = []
    content = _l(m, "telemetry.content")
    if set(content) != dset:
        out.append("telemetry.content: keys must be exactly upstream.expect.datasets")
    ident_only = _l(m, "decisions.dataset_text_publication") == "identifiers_only"
    for ds, rules in content.items():
        must_hash = None
        if ds in wire.NEVER_PUBLISH_TEXT:
            must_hash = "never published"
        elif ds in wire.CLEARANCE_GATED and _l(m, wire.CLEARANCE_GATED[ds]) is not True:
            must_hash = f"needs {wire.CLEARANCE_GATED[ds]}"
        elif ident_only:
            must_hash = "decisions.dataset_text_publication is identifiers_only"
        for field, rule in rules.items():
            where = f"telemetry.content.{ds}.{field}"
            t = _TRUNCATE_RE.fullmatch(rule)
            if rule not in ("hash", "full") and not (t and int(t.group(1)) >= 200):
                out.append(f"{where}: must be hash, full or truncate:N with N >= 200")
            elif must_hash and rule != "hash":
                out.append(f"{where}: must be hash ({must_hash})")
    return out


# ================================================================ read, validate, freeze

def read_manifest(path: Path) -> dict:
    """Read-only: no mkdir, no credentials, no network."""
    try:
        doc = util.read_json(Path(path))
    except FileNotFoundError:
        raise ManifestError("manifest file not found", problems=["manifest: file not found"]) from None
    except StateError as exc:
        raise ManifestError(str(exc), problems=[f"manifest: {exc}"]) from None
    if not isinstance(doc, dict):
        raise ManifestError(problems=["manifest: must be a JSON object"])
    return doc


def validate(manifest: dict, manifest_path: Path) -> list[str]:
    """Every problem under the rules of 2.3, each naming its path; [] = valid."""
    out: list[str] = []
    _walk(SCHEMA, manifest, "", out)
    if not out:
        try:
            out += _rules(manifest, Path(manifest_path).resolve().parent)
        except (KeyError, TypeError, ValueError, InvalidOperation, IndexError) as exc:   # defensive: never crash a check
            out.append(f"manifest: could not be checked ({type(exc).__name__})")
    try:                                                     # rule 11 needs no types
        findings = scan.scan_obj(manifest, classes=("secret",))
    except (TypeError, ValueError):
        findings = []
    for f in findings:
        where = f.path.lstrip("/").replace("/", ".") or "manifest"
        out.append(f"{where}: holds a value that matches the secret pattern {f.pattern}")
    if findings:                                             # a message may name a key or echo a value: never the secret
        secrets = sorted(_secret_texts(manifest), key=len, reverse=True)
        for i, problem in enumerate(out):
            for text in secrets:
                problem = problem.replace(text, "<redacted>")
            out[i] = problem
    return out


def _secret_texts(value: object) -> set[str]:
    """The matched text of every secret-pattern finding in any key or string value of the manifest."""
    found: set[str] = set()
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.keys())
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, str):
            data = node.encode("utf-8")
            for f in scan.scan_bytes(data, classes=("secret",)):
                text = data[f.offset:f.offset + f.length].decode("utf-8", "ignore")
                if text:
                    found.add(text)
    return found


def require_valid(manifest: dict, manifest_path: Path) -> None:
    problems = validate(manifest, manifest_path)
    if problems:
        raise ManifestError(problems=problems)


def manifest_sha256(manifest: dict) -> str:
    return util.sha256_bytes(util.canonical_json(manifest))


def _sorted(obj):
    if isinstance(obj, dict):
        return {k: _sorted(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return [_sorted(v) for v in obj]
    return obj


def _sc_manifest_path(paths: StatePaths) -> Path:
    return Path(paths.root).parent.parent / "manifest.json"


def _write_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("ascii"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def freeze(manifest: dict, paths: StatePaths, engine: dict) -> str:
    """Writes manifest.json, manifest.sha256 and engine.json when absent; a no-op when the frozen copy is equal."""
    require_valid(manifest, _sc_manifest_path(paths))
    if util.lookup(manifest, "upstream.tree_sha256") is None:
        raise ManifestError(problems=["upstream.tree_sha256: null refuses --freeze (validate-manifest --pin-upstream)"])
    digest = manifest_sha256(manifest)
    if paths.manifest.exists():
        try:
            frozen = util.read_json(paths.manifest)
        except StateError as exc:
            raise ManifestError(MANIFEST_CHANGED, problems=[f"manifest: frozen copy unreadable ({exc})"]) from None
        if not isinstance(frozen, dict) or manifest_sha256(frozen) != digest:
            raise ManifestError(MANIFEST_CHANGED, problems=[MANIFEST_CHANGED])
    else:
        paths.root.mkdir(parents=True, exist_ok=True)
        util.write_json(paths.manifest, _sorted(manifest), fsync=True)
    if not paths.manifest_sha256.exists():
        _write_text(paths.manifest_sha256, digest + "\n")
    if not paths.engine.exists():
        util.write_json(paths.engine, engine, fsync=True)
    return digest


def load_frozen(manifest_path: Path) -> tuple[dict, StatePaths]:
    """(manifest, paths); ManifestError unless a frozen copy exists and is identical to the file."""
    manifest = read_manifest(manifest_path)
    require_valid(manifest, manifest_path)
    try:
        paths = resolve_state(manifest, Path(manifest_path))
    except StateError as exc:
        raise ManifestError(problems=[f"state_dir: {exc}"]) from None
    if not paths.manifest.exists():
        raise ManifestError(problems=["manifest: not frozen (run validate-manifest --freeze)"])
    try:
        frozen = util.read_json(paths.manifest)
    except StateError as exc:
        raise ManifestError(problems=[f"manifest: frozen copy unreadable ({exc})"]) from None
    digest = manifest_sha256(manifest)
    if not isinstance(frozen, dict) or manifest_sha256(frozen) != digest:
        raise ManifestError(MANIFEST_CHANGED, problems=[MANIFEST_CHANGED])
    if paths.manifest_sha256.exists() and paths.manifest_sha256.read_text("ascii", "replace").strip() != digest:
        raise ManifestError(problems=["manifest.sha256: does not match the frozen manifest"])
    return manifest, paths


# ================================================================ prices, caps, small accessors

def prices(manifest: dict) -> dict[str, Decimal]:
    p = manifest["prices"]
    return {"input": Decimal(p["input_per_million"]), "cached": Decimal(p["cached_per_million"]),
            "output": Decimal(p["output_per_million"])}


def phase_caps_micro(manifest: dict) -> dict[str, int]:
    phases = util.lookup(manifest, "budgets.solver_usd.phases")
    caps = {p: util.micros(phases[p]) for p in wire.LEDGER_PHASES}
    caps["total"] = util.micros(util.lookup(manifest, "budgets.solver_usd.total"))
    return caps


def max_reservation_micro(manifest: dict) -> int:
    """R = price(model_context_tokens input, max_output_tokens output), rounded up to a micro-dollar."""
    p = prices(manifest)
    gw = manifest["gateway"]
    usd = (p["input"] * gw["model_context_tokens"] + p["output"] * gw["max_output_tokens"]) / Decimal(1_000_000)
    return util.micros(usd)


def openai_project(manifest: dict) -> str:
    explicit = util.lookup(manifest, "provider.openai_project")
    if explicit is not None:
        return explicit
    return f"{manifest['decisions']['entity']}/{manifest['decisions']['project']}"


def run_config(manifest: dict, allow: Sequence[str]) -> dict:
    """The sub-mapping of the manifest under the allowed dotted paths (absent paths are left out)."""
    out: dict = {}
    for dotted in allow:
        try:
            value = util.lookup(manifest, dotted)
        except KeyError:
            continue
        _set(out, dotted, copy.deepcopy(value))
    return out


# ================================================================ diff, amendments, effective manifest

def path_allowed(path: str, entries: Sequence[str]) -> bool:
    """The matching rule of AMENDABLE and IMPORT_DIFF_ALLOWED: equal to an entry, or under an entry ending in '.'."""
    return any(path == e or (e.endswith(".") and path.startswith(e)) for e in entries)


def _flatten(value, prefix: str, out: dict) -> None:
    if isinstance(value, dict) and value:
        for k, v in value.items():
            _flatten(v, f"{prefix}.{k}" if prefix else str(k), out)
    else:
        out[prefix] = value


def diff_paths(a: Mapping, b: Mapping) -> list[str]:
    """Sorted dotted leaf paths whose values differ (a list is one leaf; a path present on one side only differs)."""
    fa: dict = {}
    fb: dict = {}
    _flatten(dict(a), "", fa)
    _flatten(dict(b), "", fb)
    missing = object()
    return sorted(p for p in fa.keys() | fb.keys()
                  if util.canonical_json([fa.get(p, missing) is missing, fa.get(p)]) !=
                  util.canonical_json([fb.get(p, missing) is missing, fb.get(p)]))


def _set(doc: dict, dotted: str, value: object) -> None:
    parts = dotted.split(".")
    node = doc
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value


def effective(manifest: dict, amendments: Sequence[Mapping]) -> dict:
    """A deep copy of the frozen manifest with every row's "set" applied in order."""
    doc = copy.deepcopy(manifest)
    for row in amendments:
        for dotted, value in row["set"].items():
            _set(doc, dotted, copy.deepcopy(value))
    return doc


def _row_sha256(row: Mapping) -> str:
    return util.sha256_bytes(util.canonical_json({k: v for k, v in row.items() if k != "row_sha256"}))


def read_amendments(paths: StatePaths, frozen_sha256: str) -> list[dict]:
    """The rows of STATE/amendments.jsonl ([] when absent); ManifestError when the chain is broken."""
    try:
        rows = util.read_jsonl(paths.amendments)
    except StateError as exc:
        raise ManifestError(problems=[f"amendments.jsonl: {exc}"]) from None
    prev = frozen_sha256
    for n, row in enumerate(rows, 1):
        where = f"amendments.jsonl row {n}"
        if row.get("schema") != "mhforge.amendment/1" or row.get("seq") != n:
            raise ManifestError(problems=[f"{where}: seq is not {n} or the schema tag is wrong"])
        if row.get("prev_sha256") != prev:
            raise ManifestError(problems=[f"{where}: prev_sha256 does not match the chain"])
        if row.get("row_sha256") != _row_sha256(row):
            raise ManifestError(problems=[f"{where}: row_sha256 does not match the row"])
        changes = row.get("set")
        if not isinstance(changes, dict) or not changes:
            raise ManifestError(problems=[f"{where}: set must be a non-empty object"])
        bad = [p for p in changes if not path_allowed(p, AMENDABLE)]
        if bad:
            raise ManifestError(problems=[f"{where}: {bad[0]} is not amendable"])
        prev = row["row_sha256"]
    return rows


def _repair_tail(path: Path) -> None:
    """A crash can leave a last line without its newline. Readers ignore a torn tail, so drop it; a whole row that only
    lacks the newline gets it. Otherwise the next row would be glued to it and break the chain for good."""
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return
    try:
        size = os.fstat(fd).st_size
        if not size or os.pread(fd, 1, size - 1) == b"\n":
            return
        data = os.pread(fd, size, 0)
        cut = data.rfind(b"\n") + 1
        try:
            whole = isinstance(json.loads(data[cut:].decode("utf-8")), dict)
        except (UnicodeDecodeError, ValueError):
            whole = False
        if whole:
            os.pwrite(fd, b"\n", size)
        else:
            os.ftruncate(fd, cut)
        os.fsync(fd)
    finally:
        os.close(fd)


def append_amendment(manifest: dict, paths: StatePaths, changes: Mapping[str, object], note: str, *,
                     clock: Clock | None = None) -> dict:
    """Validates effective(manifest, existing + this row) with the rules of 2.3, then appends the row (fsync)."""
    if not paths.manifest.exists():
        raise ManifestError(problems=["manifest: an amendment needs a frozen copy"])
    digest = manifest_sha256(manifest)
    frozen = util.read_json(paths.manifest)
    if not isinstance(frozen, dict) or manifest_sha256(frozen) != digest:
        raise ManifestError(MANIFEST_CHANGED, problems=[MANIFEST_CHANGED])
    if not isinstance(note, str) or not note.strip():
        raise ManifestError(problems=["amendment: a non-empty note is required"])
    if not isinstance(changes, Mapping) or not changes:
        raise ManifestError(problems=["amendment: no change given"])
    bad = [p for p in changes if not isinstance(p, str) or not path_allowed(p, AMENDABLE)]
    if bad:
        raise ManifestError(problems=[f"{bad[0]}: not amendable after the freeze"])
    _repair_tail(paths.amendments)
    existing = read_amendments(paths, digest)
    row = {"schema": "mhforge.amendment/1", "seq": len(existing) + 1, "utc": util.utc_now(clock),
           "prev_sha256": existing[-1]["row_sha256"] if existing else digest,
           "set": {k: copy.deepcopy(v) for k, v in changes.items()}, "note": note.strip()}
    try:
        row["row_sha256"] = _row_sha256(row)
    except (TypeError, ValueError):
        raise ManifestError(problems=["amendment: values must be JSON"]) from None
    problems = validate(effective(manifest, existing + [row]), _sc_manifest_path(paths))
    if problems:
        raise ManifestError(problems=problems)
    util.append_jsonl(paths.amendments, row, fsync=True)
    return row


# ================================================================ live gates

def _present(environ: Mapping[str, str], name: str) -> bool:
    return bool(environ.get(name))                           # never copied, printed or hashed


def _halt_row(paths: StatePaths) -> tuple[dict | None, str | None]:
    if not paths.ledger.exists():
        return None, None
    query = "SELECT reason, detail, set_utc FROM halt WHERE id = 1"
    try:
        conn, stale = util.open_sqlite_ro(paths.ledger)
        try:
            row = None if stale else conn.execute(query).fetchone()
        finally:
            conn.close()
        if stale:                                            # the halt row may be only in the log: a normal connection
            conn = sqlite3.connect(paths.ledger, timeout=30)   # (2.10 writer open) applies it
            try:
                row = conn.execute(query).fetchone()
            finally:
                conn.close()
    except Exception as exc:                                 # sqlite3.Error, OSError: an unreadable ledger blocks
        return None, type(exc).__name__
    return ({"reason": row[0], "detail": row[1], "set_utc": row[2]} if row else None), None


def _read_doc(path: Path) -> dict | None:
    try:
        doc = util.read_json(path)
    except (FileNotFoundError, StateError, OSError):
        return None
    return doc if isinstance(doc, dict) else None


def _journal(paths: StatePaths) -> list[dict]:
    try:
        return util.read_marked_jsonl(paths.journal)        # a torn tail terminated by its marker is not corruption
    except StateError:
        return [{"op": "_unreadable"}]


def _result_dir(manifest: dict, paths: StatePaths, dataset: str, system: str) -> Path:
    seed = util.lookup(manifest, "upstream.expect.seed")
    leaf = util.lookup(manifest, "upstream.expect.model_short")
    if seed != UPSTREAM_DEFAULT_SEED:
        leaf = f"{leaf}_seed{seed}"
    return paths.logs_dir(manifest["campaign"]) / dataset / system / leaf


def _usable(path: Path) -> bool:                             # benchmark.py L171-L176
    doc = _read_doc(path)
    return doc is not None and isinstance(doc.get("accuracy"), (int, float))


def _key_rows(manifest: dict, environ: Mapping[str, str], key_source: str, roles: Sequence[str]) -> list[tuple[str, str]]:
    if key_source != "env":
        return []                                            # prompt and fd: the CLI checks the keys it read
    where = {"provider": "provider.api_key_env", "sandbox": "sandbox.auth_env", "export": "telemetry.api_key_env"}
    out = []
    for role in roles:
        name = util.lookup(manifest, where[role])
        if not _present(environ, name):
            out.append((where[role], f"the {role} key is not supplied: {name} is unset or empty"))
    return out


def _export_rows(manifest: dict, environ: Mapping[str, str], paths: StatePaths, key_source: str) -> list[tuple[str, str]]:
    out = _key_rows(manifest, environ, key_source, ("export",))
    try:
        rows = read_amendments(paths, manifest_sha256(manifest))
        eff = effective(manifest, rows)
    except ManifestError as exc:
        out.append(("amendments", exc.problems[0] if exc.problems else "amendments chain broken"))
        eff = manifest
    else:
        if rows:                                             # rule A: the effective manifest must pass rules 1 to 21
            problems = validate(eff, _sc_manifest_path(paths))
            out += [("amendments", f"effective manifest: {p}") for p in problems]
            if problems:
                eff = manifest
    if eff["decisions"]["evaltables_backend"] == "unset":
        out.append(("decisions.evaltables_backend", "unset: choose ces, wandb_table or off"))
    if eff["decisions"]["dataset_text_publication"] == "unset":
        out.append(("decisions.dataset_text_publication", "unset: sign off what dataset text may leave the machine"))
    for name in EXPORT_MUST_BE_UNSET:
        if name in environ:
            out.append((f"environ.{name}", "must be unset for export"))
    if util.lookup(manifest, "telemetry.agent_lens.enabled") and util.lookup(manifest, "budgets.lens_ingest_mb") <= 0:
        out.append(("budgets.lens_ingest_mb", "0 refuses Agent Lens export while telemetry.agent_lens.enabled"))
    return out


def _calibration_verdict(manifest: dict, paths: StatePaths) -> tuple[str | None, str]:
    if paths.calibration.exists():
        doc = _read_doc(paths.calibration)
        return (doc or {}).get("verdict"), "this campaign"
    source = util.lookup(manifest, "qualification.import_from")
    if source is None:
        return None, "this campaign"
    passed = _read_doc(paths.qualification_passed) or {}
    imported = passed.get("imported_from")
    if not isinstance(imported, dict) or not isinstance(imported.get("diff_paths"), list):
        return None, "this campaign (no import record in qualification/passed.json)"
    if any(isinstance(p, str) and p.startswith("calibration.") for p in imported["diff_paths"]):
        return None, "this campaign (calibration fields differ from the imported campaign, LD3)"
    doc = _read_doc(paths.sibling(source).calibration)
    return (doc or {}).get("verdict"), f"imported campaign {source}"


def live_blockers(manifest: dict, command: str, environ: Mapping[str, str], paths: StatePaths, *,
                  live_flag: bool, engine_sha256: str | None = None,
                  gate_problems: Sequence[str] | None = None, key_source: str = "env",
                  mode: str | None = None, end_search: bool = False,
                  create_project: bool = False) -> list[tuple[str, str]]:
    """[(field, message)] of the live-gate table of 2.3; [] = allowed. Writes nothing; reads only state files."""
    if command not in COMMANDS:
        raise ValueError(f"unknown live command {command!r}")
    if command == "evaluate" and mode not in EVALUATE_MODES:
        raise ValueError("evaluate needs a mode: baselines, calibration, noise or test")
    if key_source not in ("env", "prompt", "fd") and not str(key_source).startswith("fd:"):
        raise ValueError("key_source must be env, prompt or fd")
    out: list[tuple[str, str]] = []
    if not live_flag:
        out.append(("--live", "not given"))
    problems = validate(manifest, _sc_manifest_path(paths))
    out += [("manifest", p) for p in problems]
    if problems:
        return out                                           # the rows below need a well-formed manifest
    dec, ex = manifest["decisions"], manifest["executor"]
    for key in ("entity", "project"):
        if dec[key] is None:
            out.append((f"decisions.{key}", "unset"))
    if manifest["sandbox"]["auth_mode"] != "headers":         # LD7: every live command, whatever the executor
        out.append(("sandbox.auth_mode", WANDB_AUTH_REFUSED))
    if command == "account-check":
        out += _key_rows(manifest, environ, key_source, ("provider",) + (("sandbox",) if ex["kind"] == "sandbox" else ()))
        if create_project and dec["live_spend_approved"] is not True:
            out.append(("decisions.live_spend_approved", "false: --create-project is a remote write"))
        out += [("offline_gate", str(g)) for g in (gate_problems or ())]
        return out
    digest = manifest_sha256(manifest)
    frozen = _read_doc(paths.manifest)
    if frozen is None:
        out.append(("frozen", "no frozen copy: run validate-manifest --freeze"))
    elif manifest_sha256(frozen) != digest:
        out.append(("frozen", MANIFEST_CHANGED))
    if dec["live_spend_approved"] is not True:
        out.append(("decisions.live_spend_approved", "false: no paid call, sandbox or remote write is approved"))
    if dec["key_provisioning"] != "dedicated":
        out.append(("decisions.key_provisioning", "unset: confirm dedicated, revocable keys"))
    if manifest["provider"]["kind"] != "forge":
        out.append(("provider.kind", "stub is refused by every live command"))
    if util.lookup(manifest, "upstream.tree_sha256") is None:
        out.append(("upstream.tree_sha256", "null"))
    out += [("offline_gate", str(g)) for g in (gate_problems or ())]
    if command in RUNNER_COMMANDS:
        out += _runner_rows(manifest, environ, paths, key_source)
    if command in ("search", "evaluate"):
        passed = _read_doc(paths.qualification_passed)
        if passed is None:
            out.append(("qualification/passed.json", "missing: the campaign is not qualified"))
        else:
            if passed.get("manifest_sha256") != digest:
                out.append(("qualification/passed.json", "manifest_sha256 differs from the frozen manifest"))
            if engine_sha256 is None:
                out.append(("qualification/passed.json", "engine_sha256 not given: the engine cannot be compared"))
            elif passed.get("engine_sha256") != engine_sha256:
                out.append(("qualification/passed.json", "engine_sha256 differs from the engine now"))
    if command == "search":
        out += _search_rows(manifest, environ, paths)
    if command == "evaluate":
        out += _evaluate_rows(manifest, paths, mode, end_search)
    if command in ("export", "publish"):
        out += _export_rows(manifest, environ, paths, key_source)
    if command == "publish":
        if dec["publish_approved"] is not True:
            out.append(("decisions.publish_approved", "false"))
        if manifest["campaign_kind"] != "measured":
            out.append(("campaign_kind", "a preflight campaign is never published"))
    return out


def _runner_rows(manifest: dict, environ: Mapping[str, str], paths: StatePaths, key_source: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    halt, error = _halt_row(paths)
    if error:
        out.append(("halt", f"the ledger could not be read ({error})"))
    elif halt is not None:
        out.append(("halt", f"campaign halted ({halt['reason']}); clear with status --clear-halt"))
    ex, sb = manifest["executor"], manifest["sandbox"]
    roles = ("provider",) + (("sandbox",) if ex["kind"] == "sandbox" else ())
    out += _key_rows(manifest, environ, key_source, roles)
    if ex["kind"] != "sandbox" and ex["allow_unisolated_live"] is not True:
        out.append(("executor.kind", f"{ex['kind']} is not isolated (set executor.allow_unisolated_live to allow it)"))
    if ex["kind"] == "sandbox":
        if util.lookup(manifest, "upstream.worker_lock_sha256") is None:
            out.append(("upstream.worker_lock_sha256", "null"))
        if sb["env_mode"] == "image" and sb["image"] is None:
            out.append(("sandbox.image", "null refuses mode image"))
        if sb["env_mode"] == "wheelhouse":
            for key in ("base_image",):
                if sb[key] is None:
                    out.append((f"sandbox.{key}", "null refuses mode wheelhouse"))
            for key in ("tar_sha256", "tar_bytes", "sha256sums_sha256"):
                if sb["wheelhouse"][key] is None:
                    out.append((f"sandbox.wheelhouse.{key}", "null refuses mode wheelhouse"))
        if sb["auth_mode"] == "wandb_auth":
            for name in WANDB_AUTH_MUST_BE_UNSET:
                if name in environ:
                    out.append((f"environ.{name}", "must be unset in auth mode wandb_auth"))
    if util.lookup(manifest, "telemetry.export_mode") == "after_unit":
        out += _export_rows(manifest, environ, paths, key_source)
    return out


def _search_rows(manifest: dict, environ: Mapping[str, str], paths: StatePaths) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pp, dec = manifest["proposer"], manifest["decisions"]
    if dec["proposer_permission_mode"] == "unset":
        out.append(("decisions.proposer_permission_mode", "unset"))
    for key in ("cli_path", "cli_sha256", "cli_version"):
        if pp[key] is None:
            out.append((f"proposer.{key}", "null refuses search"))
    if pp["cli_path"] is not None and pp["cli_sha256"] is not None:
        cli = Path(pp["cli_path"])
        if not cli.is_file():
            out.append(("proposer.cli_path", "the pinned binary is not present"))
        elif util.sha256_file(cli) != pp["cli_sha256"]:
            out.append(("proposer.cli_sha256", "the binary's hash differs from the pin"))
    for where in ("proposer.max_budget_usd_per_session", "budgets.proposer_list_usd", "proposer.session_estimate_usd"):
        if util.lookup(manifest, where) is None:
            out.append((where, "null refuses search"))
    for name in CLAUDE_SESSION_NAMES:
        if name in environ:
            out.append((f"environ.{name}", "set: start a live proposer from your own terminal, not inside Claude Code"))
    finalized = paths.logs_dir(manifest["campaign"]) / "finalized.json"
    if finalized.exists():
        out.append(("finalized.json", "upstream finalisation exists: no search after a test phase"))
    if any(r.get("op") in ("test", "_unreadable") for r in _journal(paths)):
        out.append(("journal", "holds a test entry (or is unreadable): no search after a test phase"))
    if util.lookup(manifest, "calibration.gate_search"):
        verdict, where = _calibration_verdict(manifest, paths)
        if verdict != "pass":
            out.append(("calibration.gate_search", f"no calibration verdict pass in {where}: run evaluate --calibration"))
    if manifest["campaign_kind"] == "measured":
        protocol = Path(paths.root).parent.parent / "protocol.md"
        try:
            text = protocol.read_text("utf-8", "replace")
        except OSError:
            text = None
        if text is None or "STAGE2-UNSIGNED" in text:
            out.append(("protocol.md", "stage 2 is not signed (STAGE2-UNSIGNED present or file missing)"))
    return out


def _evaluate_rows(manifest: dict, paths: StatePaths, mode: str, end_search: bool) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    datasets = util.lookup(manifest, "upstream.expect.datasets")
    if mode == "test":
        done = any(r.get("op") == "run" and r.get("key") == "search" and r.get("status") == "complete"
                   for r in _journal(paths))
        if not done and not end_search:
            out.append(("journal", "search is not complete: finish it or pass --end-search"))
    if mode == "calibration":
        systems = util.lookup(manifest, "calibration.systems") or util.lookup(manifest, "upstream.expect.baselines")
        for system in systems:
            for ds in datasets:
                d = _result_dir(manifest, paths, ds, system)
                if not _usable(d / "val.json") or not (d / "memory.json").is_file():
                    out.append((f"results.{ds}.{system}", "no published usable val.json and memory.json"))
    if mode == "noise":
        for system in util.lookup(manifest, "upstream.expect.baselines"):
            for ds in datasets:
                if not _usable(_result_dir(manifest, paths, ds, system) / "val.json"):
                    out.append((f"results.{ds}.{system}", "no published usable val.json (repeat 1)"))
    return out


def require_live(manifest: dict, command: str, environ: Mapping[str, str], paths: StatePaths, *,
                 live_flag: bool, engine_sha256: str | None = None,
                 gate_problems: Sequence[str] | None = None, key_source: str = "env",
                 mode: str | None = None, end_search: bool = False, create_project: bool = False) -> None:
    blockers = live_blockers(manifest, command, environ, paths, live_flag=live_flag, engine_sha256=engine_sha256,
                             gate_problems=gate_problems, key_source=key_source, mode=mode, end_search=end_search,
                             create_project=create_project)
    if blockers:
        raise LiveGateError(blockers=blockers)
