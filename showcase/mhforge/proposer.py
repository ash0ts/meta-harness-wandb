# SPDX-License-Identifier: MIT
"""ProposerRunner: the replacement for meta_harness.propose_claude (spec 3.19, F11).

Generated settings file per permission mode, the session checks (model and effort, sandbox proof, integrity), the limit
classifier, the session cost with its fallback chain, the session record, and the proposal store that upstream's resume
replays. A session is successful only when its stream holds a `result` event with `is_error` false; an exit code is never
success. Records hold hashes of the prompt and the skill text, never the text.
"""

from __future__ import annotations

import glob
import json
import os
import random
import re
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Sequence

from . import procenv, util, workspace
from .errors import CampaignStop, MeterCapExceeded, TranscriptError, WorkspaceError
from .journal import Journal
from .ledger import Ledger
from .paths import StatePaths, proposer_scratch
from .proposer_shim import MODES, ArgvSpec, SubprocessShim, command_fingerprint, installed, session_argv
from .telemetry import transcript as tx
from .wire import CANDIDATE_RE

__all__ = ["COMMON_SETTINGS", "CREDENTIAL_PATHS", "RESTRICTED_ADDENDUM", "SettingsInputs", "take_settings_inputs",
           "generate_settings", "merge_settings", "check_settings", "KEPT_LISTS", "render_addendum", "transcript_slug",
           "PLAN_WINDOW_TYPES", "FALLBACK_SUBTYPES", "parse_stream", "limit_signal", "check_model", "check_sandboxed",
           "integrity_violations", "SessionFacts", "classify_session", "locate_transcript", "copy_transcript",
           "solver_attempt_signals", "state_access_signals", "session_cost", "find_usable_proposal",
           "settled_failure", "candidate_reference", "ProposerRunner"]

# ================================================================ generated settings

# Built-in privacy enforcement stays enabled. Optional built-ins must not add
# project instructions, skills, or analytics to the experiment.
COMMON_SETTINGS = {"autoMemoryEnabled": False, "switchModelsOnFlag": False, "disableAllHooks": True,
    "syncClaudeAiPlugins": False,
    "enabledPlugins": {name + "@builtin": False for name in
        ("agents-md", "telemetry", "plugin-authoring", "cc-plugin-agents-md",
         "cc-plugin-telemetry", "cc-plugin-plugin-authoring")}}
# This is part of the hash-pinned CLI, not an installed extension. A different
# binary, version, source, path, or extra plugin still fails closed.
PINNED_PRIVACY_PLUGIN = ("2.1.317", "577c8ac6411ef57dd741e3c491e89f57ff6f42bb835a5ac5d4f496c29f14c5d5")
CREDENTIAL_PATHS = ("~/.netrc", "~/.ssh", "~/.gnupg", "~/.aws", "~/.config/gh", "~/.config/gcloud", "~/.config/wandb",
                    "~/.docker", "~/.kube", "~/.npmrc", "~/.pypirc", "~/.git-credentials", "~/.cache/huggingface",
                    "~/Library/Keychains", "~/.claude/.credentials.json", "~/Documents/common_tools/.env")
RESTRICTED_ADDENDUM = """## Run environment (added by the harness; not part of the skill)

This session runs with restricted permissions. Where the skill mentions /tmp, these rules apply instead.

- Scratch directory: {scratch_dir}
  Use it wherever the skill says /tmp/. /tmp itself is not writable.
- Create and change files with the Write and Edit tools, not with shell redirection or heredocs.
- Run a script as: uv run python <file>
  Run the skill's import check exactly as the skill gives it.
- Shell commands that use a heredoc, timeout, time, env, nohup, eval, source, rm, rmdir, awk, jq, or a leading
  VAR=value assignment are refused. Do not retry a refused command; do the step another way.
- Leave scratch files in place. Do not delete them.
- You can create new files under agents/ and in the reports directory, and write pending_eval.json at the
  path the task prompt gives. Existing files under agents/ and in the reports directory, and every other
  project file, are read-only in this session.
- Shell commands have no network access."""
_CLAUDE_TMP = "/private/tmp/claude-{uid}"


@dataclass(frozen=True)
class SettingsInputs:                               # everything generate_settings needs, as data
    mode: str
    ws: Path
    logs_dir: Path
    state_root: Path
    package_dir: Path
    scratch_dir: Path
    scratch_root: Path
    example_venv: Path
    uv_dir: Path | None
    home: Path
    uid: int
    state_children: tuple[str, ...] = ()            # names directly under STATE
    state_siblings: tuple[str, ...] = ()            # names directly under SC/state other than this campaign's directory
    ws_top: tuple[str, ...] = ()                    # names directly under WS
    agent_files: tuple[str, ...] = ()               # names of the entries of WS/agents at session start
    logs_entries: tuple[str, ...] = ()              # names directly under LOGS
    claude_tmp_projects: tuple[str, ...] = ()       # names directly under /private/tmp/claude-<uid>/
    scratch_others: tuple[str, ...] = ()            # other session directories under scratch_root, "<campaign>/<key>"
    report_files: tuple[str, ...] = ()              # names of the entries of LOGS/reports at session start (LD1)


def _ls(path: Path) -> tuple[str, ...]:
    try:
        return tuple(sorted(os.listdir(path)))
    except (FileNotFoundError, NotADirectoryError):
        return ()


def _package_dir() -> Path:
    import mhforge                                  # the engine is located through mhforge.__file__, never sc_dir
    return Path(os.path.realpath(os.path.dirname(mhforge.__file__)))


def take_settings_inputs(manifest: dict, paths: StatePaths, *, mode: str, logs_dir: Path, scratch_dir: Path, home: Path,
                         example_venv: Path, uv_dir: Path | None, uid: int,
                         claude_tmp_root: Path | None = None) -> SettingsInputs:
    """The only I/O is os.listdir. `claude_tmp_root` defaults to /private/tmp/claude-<uid> (tests pass another)."""
    scratch_root = Path(util.lookup(manifest, "proposer.scratch_root"))
    scratch_dir = Path(scratch_dir)
    own = scratch_dir.relative_to(scratch_root).as_posix() if scratch_dir.is_relative_to(scratch_root) else None
    others = []
    for camp in _ls(scratch_root):
        for key in _ls(scratch_root / camp):
            if f"{camp}/{key}" != own:
                others.append(f"{camp}/{key}")
    state_root = Path(paths.root)
    tmp_root = Path(claude_tmp_root) if claude_tmp_root is not None else Path(_CLAUDE_TMP.format(uid=uid))
    return SettingsInputs(
        mode=mode, ws=Path(paths.ws), logs_dir=Path(logs_dir), state_root=state_root, package_dir=_package_dir(),
        scratch_dir=scratch_dir, scratch_root=scratch_root, example_venv=Path(example_venv),
        uv_dir=None if uv_dir is None else Path(uv_dir), home=Path(home), uid=int(uid),
        state_children=_ls(state_root), state_siblings=tuple(n for n in _ls(state_root.parent) if n != state_root.name),
        ws_top=_ls(paths.ws), agent_files=_ls(Path(paths.ws) / "agents"), logs_entries=_ls(logs_dir),
        claude_tmp_projects=_ls(tmp_root), scratch_others=tuple(others), report_files=_ls(Path(logs_dir) / "reports"))


def _rule_path(p: Path | str) -> str:
    """The CLI's permission-rule syntax for an absolute path: '//' then the path without its leading '/'."""
    return "/" + os.path.abspath(str(p))


def generate_settings(inputs: SettingsInputs, override: Mapping | None = None) -> dict:
    """Pure: the settings file of 3.19 (with LD1) for the mode; `override` is deep-merged in modes restricted and
    contained and ignored in mode upstream."""
    i = inputs
    if i.mode not in MODES:
        raise ValueError(f"unknown permission mode {i.mode!r}")
    settings: dict = json.loads(json.dumps(COMMON_SETTINGS))
    if i.mode == "upstream":
        return settings
    ws, logs, state = Path(i.ws), Path(i.logs_dir), Path(i.state_root)
    guarded_top = [n for n in i.ws_top if n not in ("agents", "logs")]
    deny: list[str] = []
    deny += [f"Edit({_rule_path(ws / 'agents' / n)})" for n in i.agent_files]
    for n in guarded_top:
        deny += [f"Edit({_rule_path(ws / n)})", f"Edit({_rule_path(ws / n)}/**)"]
    deny += [f"Edit({_rule_path(logs / 'reports' / n)})" for n in i.report_files]
    for p in CREDENTIAL_PATHS:
        deny += [f"Read({p})", f"Read({p}/**)"]
    for target in ([state / n for n in i.state_children if n != "workspace"]
                   + [state.parent / n for n in i.state_siblings] + [Path(i.package_dir)]):
        deny += [f"Read({_rule_path(target)})", f"Read({_rule_path(target)}/**)"]
    permissions: dict = {}
    if i.mode == "restricted":
        permissions["defaultMode"] = "dontAsk"
        permissions["allow"] = [f"Edit({_rule_path(ws / 'agents')}/**)", f"Edit({_rule_path(logs / 'pending_eval.json')})",
                                f"Edit({_rule_path(i.scratch_dir)}/**)", f"Edit({_rule_path(logs / 'reports')}/**)"]
    permissions["deny"] = deny
    permissions["blockReadsOutsideWorkingDirectories"] = True
    permissions["blockWritesOutsideWorkingDirectories"] = True
    allow_read = [str(Path(i.home) / ".local" / "share" / "uv" / "python"), str(ws.parent), str(i.example_venv)]
    if i.uv_dir is not None and Path(i.uv_dir).is_relative_to(Path(i.home)):
        allow_read.append(str(i.uv_dir))
    own_tmp = transcript_slug(ws)
    tmp_root = Path(_CLAUDE_TMP.format(uid=i.uid))
    deny_read = [str(tmp_root / n) for n in i.claude_tmp_projects if n != own_tmp]
    deny_read += [str(Path(i.scratch_root) / e) for e in i.scratch_others]
    deny_write = [str(ws / n) for n in guarded_top]
    deny_write += [str(ws / "agents" / n) for n in i.agent_files]
    deny_write += [str(logs / n) for n in i.logs_entries if n not in ("pending_eval.json", "reports")]
    deny_write += [str(logs / "reports" / n) for n in i.report_files]
    settings["permissions"] = permissions
    settings["sandbox"] = {
        "enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False, "autoAllowBashIfSandboxed": True,
        "filesystem": {"allowRead": allow_read, "denyRead": deny_read, "denyWrite": deny_write},
        "credentials": {"files": [{"path": p, "mode": "deny"} for p in CREDENTIAL_PATHS]},
        "network": {"allowedDomains": [], "strictAllowlist": True},
    }
    if override:
        settings = merge_settings(settings, override)
    return settings


def merge_settings(base: Mapping, override: Mapping) -> dict:
    """Pure deep merge: objects merge key by key, lists are concatenated without duplicates (base first), anything
    else is replaced."""
    out = json.loads(json.dumps(base))
    for key, value in override.items():
        mine = out.get(key)
        if isinstance(mine, dict) and isinstance(value, Mapping):
            out[key] = merge_settings(mine, value)
        elif isinstance(mine, list) and isinstance(value, list):
            merged = list(mine)
            for item in json.loads(json.dumps(value)):
                if item not in merged:
                    merged.append(item)
            out[key] = merged
        else:
            out[key] = json.loads(json.dumps(value))
    return out


KEPT_LISTS = ("permissions.deny", "sandbox.filesystem.denyRead", "sandbox.filesystem.denyWrite",
              "sandbox.credentials.files")                # every generated element must survive an override (LD6)


def _at(obj: object, dotted: str) -> object:
    for part in dotted.split("."):
        if not isinstance(obj, Mapping):
            return None
        obj = obj.get(part)
    return obj


def _kind(value: object) -> str:
    return "object" if isinstance(value, Mapping) else "list" if isinstance(value, list) else "other"


def _shape_problems(base: Mapping, merged: object, where: str) -> list[str]:
    """A key whose generated value is a list or an object and whose merged value has another type (LD6 a)."""
    out: list[str] = []
    merged = merged if isinstance(merged, Mapping) else {}
    for key, value in base.items():
        path = f"{where}.{key}" if where else str(key)
        if _kind(value) == "other":
            continue
        got = merged.get(key)
        if _kind(got) != _kind(value):
            out.append(f"{path} must stay {'an' if _kind(value) == 'object' else 'a'} {_kind(value)}")
        elif isinstance(value, Mapping):
            out += _shape_problems(value, got, path)
    return out


def _override_problems(settings: Mapping, base: Mapping) -> list[str]:
    """LD6: an override can only add to the generated settings."""
    problems = _shape_problems(base, settings, "")
    for dotted in KEPT_LISTS:
        generated = _at(base, dotted)
        if not isinstance(generated, list):
            continue
        merged = _at(settings, dotted)
        merged = merged if isinstance(merged, list) else []
        lost = sum(1 for item in generated if item not in merged)
        if lost:
            problems.append(f"{dotted} must keep every generated entry ({lost} missing)")
    excluded = _at(settings, "sandbox.excludedCommands")
    if excluded is not None and excluded != [] and excluded != {} and excluded != "":
        problems.append("sandbox.excludedCommands must be empty")
    if _at(settings, "permissions.additionalDirectories") != _at(base, "permissions.additionalDirectories"):
        problems.append("permissions.additionalDirectories must not change")
    return problems


def check_settings(settings: Mapping, mode: str, *, base: Mapping | None = None) -> list[str]:
    """Pure: what the mode rests on and an override must not switch off; [] when the settings may be used.

    `base` is the generated settings before an override was merged (LD6). With it, a generated list or object that
    became another type, a lost entry of a generated deny list (KEPT_LISTS), a non-empty `sandbox.excludedCommands` or
    a changed `permissions.additionalDirectories` is also a problem."""
    problems: list[str] = []
    if mode not in MODES:
        return [f"unknown permission mode {mode!r}"]
    for key, value in COMMON_SETTINGS.items():
        got = settings.get(key)
        same = (isinstance(got, Mapping) and got.keys() == value.keys()
                and all(got[k] is v for k, v in value.items())) if isinstance(value, Mapping) else got is value
        if not same:
            problems.append(f"{key} must be {json.dumps(value)}")
    if base is not None:
        problems += _override_problems(settings, base)
    if mode == "upstream":
        return problems
    sb = settings.get("sandbox") if isinstance(settings.get("sandbox"), Mapping) else {}
    perms = settings.get("permissions") if isinstance(settings.get("permissions"), Mapping) else {}
    for key, value in (("enabled", True), ("failIfUnavailable", True), ("allowUnsandboxedCommands", False)):
        if sb.get(key) is not value:
            problems.append(f"sandbox.{key} must be {json.dumps(value)}")
    net = sb.get("network") if isinstance(sb.get("network"), Mapping) else {}
    if net.get("allowedDomains") != [] or net.get("strictAllowlist") is not True:
        problems.append("sandbox.network must have an empty allowedDomains and strictAllowlist true")
    for key in ("blockReadsOutsideWorkingDirectories", "blockWritesOutsideWorkingDirectories"):
        if perms.get(key) is not True:
            problems.append(f"permissions.{key} must be true")
    allow = perms.get("allow") or []
    if not isinstance(allow, list):
        problems.append("permissions.allow must be a list")
        allow = []
    for rule in allow:
        if isinstance(rule, str) and (rule.strip() == "Bash" or rule.strip().startswith("Bash(")):
            problems.append("permissions.allow must hold no Bash rule")
            break
    if mode == "restricted" and perms.get("defaultMode") != "dontAsk":
        problems.append("permissions.defaultMode must be dontAsk in mode restricted")
    return problems


def render_addendum(scratch_dir: Path) -> str:
    return RESTRICTED_ADDENDUM.replace("{scratch_dir}", str(scratch_dir))


def transcript_slug(cwd: Path) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


# ================================================================ session checks

PLAN_WINDOW_TYPES = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "seven_day_overage_included")
FALLBACK_SUBTYPES = ("model_refusal_fallback", "model_refusal_no_fallback", "model_fallback")
SYNTHETIC_MODEL = "<synthetic>"                     # the CLI's own placeholder on locally generated error messages
_NETWORK_RETRY_ERRORS = ("overloaded", "server_error", "network", "network_error", "connection_error",
                         "api_connection_error", "timeout", "request_timeout")


def _events(rows: Sequence[Mapping]) -> list[dict]:
    """Stream events from stream.jsonl rows ({"t_utc", "stream", "line"}) or from events given directly."""
    out = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if "line" in row:
            if row.get("stream", "stdout") != "stdout" or not isinstance(row.get("line"), str):
                continue
            try:
                ev = json.loads(row["line"])
            except ValueError:
                continue
        else:
            ev = row
        if isinstance(ev, dict) and isinstance(ev.get("type"), str):
            out.append(ev)
    return out


def _tool_uses(ev: Mapping) -> list[dict]:
    msg = ev.get("message") if isinstance(ev.get("message"), Mapping) else {}
    content = msg.get("content") if isinstance(msg.get("content"), list) else []
    return [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]


def parse_stream(rows: Sequence[Mapping]) -> dict:
    events = _events(rows)
    out: dict = {"init": None, "result": None, "events": len(events), "assistant": [], "system": [], "rate_limits": [],
                 "api_retries": [], "bash_tool_uses": 0}
    for ev in events:
        kind = ev["type"]
        if kind == "system":
            out["system"].append(ev)
            if ev.get("subtype") == "init" and out["init"] is None:
                out["init"] = ev
            elif ev.get("subtype") == "api_retry":
                out["api_retries"].append(ev)
        elif kind == "assistant":
            out["assistant"].append(ev)
            out["bash_tool_uses"] += sum(1 for b in _tool_uses(ev) if b.get("name") == "Bash")
        elif kind == "rate_limit_event":
            out["rate_limits"].append(ev)
        elif kind == "result":
            out["result"] = ev
    return out


def _pick(info: Mapping, *names: str) -> object:
    for n in names:
        if info.get(n) is not None:
            return info[n]
    return None


def limit_signal(parsed: Mapping) -> dict:
    """The "limit" object of 2.11 from the last rate_limit_event whose status is "rejected"."""
    out = {"signal": None, "status": None, "rate_limit_type": None, "resets_at_utc": None, "error_code": None,
           "overage_status": None, "overage_disabled_reason": None}
    for ev in reversed(parsed.get("rate_limits") or []):
        info = ev.get("rate_limit_info") if isinstance(ev.get("rate_limit_info"), Mapping) else ev
        if info.get("status") != "rejected":
            continue
        resets = _pick(info, "resetsAt", "resets_at")
        utc = None
        if isinstance(resets, (int, float)) and not isinstance(resets, bool) and resets > 0:
            try:
                utc = util.utc_iso(resets / 1000.0 if resets > 1e11 else float(resets))
            except (OverflowError, ValueError, OSError):     # an out-of-range reset is no machine-readable reset
                utc = None
        elif isinstance(resets, str):
            try:
                utc = util.utc_iso(util.parse_utc(resets))
            except ValueError:
                utc = None
        out.update({"signal": "rate_limit_event", "status": "rejected",
                    "rate_limit_type": _pick(info, "rateLimitType", "rate_limit_type"), "resets_at_utc": utc,
                    "error_code": _pick(info, "errorCode", "error_code"),
                    "overage_status": _pick(info, "overageStatus", "overage_status"),
                    "overage_disabled_reason": _pick(info, "overageDisabledReason", "overage_disabled_reason")})
        break
    return out


def _model_ok(model: str, requested: str) -> bool:
    if requested.startswith("claude-"):
        return model == requested or bool(re.fullmatch(re.escape(requested) + r"-[0-9]{8}", model))
    return requested in model.split("-")


def check_model(parsed: Mapping, *, requested_model: str, expected_resolved: str | None, requested_effort: str,
                request_efforts: Sequence[str] | None) -> dict:
    """The "model_check" object of 2.11. Main-thread assistant events with the CLI's placeholder model "<synthetic>"
    (locally generated error messages) are not model replies and are skipped."""
    init = parsed.get("init") or {}
    main, subs = [], []
    for ev in parsed.get("assistant") or []:
        msg = ev.get("message") if isinstance(ev.get("message"), Mapping) else {}
        model = msg.get("model")
        if not isinstance(model, str) or model == SYNTHETIC_MODEL:
            continue
        (subs if ev.get("parent_tool_use_id") else main).append(model)
    fallback = [{"subtype": ev.get("subtype"), "from": ev.get("from"), "to": ev.get("to")}
                for ev in parsed.get("system") or [] if ev.get("subtype") in FALLBACK_SUBTYPES]
    init_model = init.get("model") if isinstance(init.get("model"), str) else None
    init_effort = init.get("effort") if isinstance(init.get("effort"), str) else None
    efforts = None if request_efforts is None else list(request_efforts)
    resolved = main[0] if main else None
    mismatch = None
    if expected_resolved and resolved is not None and resolved != expected_resolved:
        mismatch = "resolved_model_differs_from_expected"
    elif resolved is not None and not _model_ok(resolved, requested_model):
        mismatch = "resolved_model_differs_from_request"
    elif any(m != resolved for m in main):
        mismatch = "main_thread_model_changed"
    elif init_model is not None and init_model not in (requested_model, resolved) and not (
            resolved is None and _model_ok(init_model, requested_model)):
        mismatch = "init_model_differs"
    elif fallback:
        mismatch = "fallback_event"
    elif init_effort is not None and init_effort != requested_effort:
        mismatch = "init_effort_differs"
    elif efforts and any(e != requested_effort for e in efforts):
        mismatch = "request_effort_differs"
    return {"init_model": init_model, "main_thread_models": sorted(set(main), key=main.index),
            "subagent_models": sorted(set(subs)), "fallback_events": fallback, "init_effort": init_effort,
            "request_efforts": efforts, "resolved_model": resolved, "mismatch": mismatch}


def check_sandboxed(bash_results: Sequence[Mapping] | None, bash_tool_uses: int) -> dict:
    """The "sandbox_check" object of 2.11; "passed" is false when a Bash result is not marked sandboxed, when a stream
    Bash call has no result in the transcript, or when there is no transcript while the stream shows a Bash call."""
    if bash_results is None:
        return {"transcript_checked": False, "bash_results": 0, "unsandboxed": 0, "unknown": 0,
                "bash_tool_uses": bash_tool_uses, "passed": bash_tool_uses == 0}
    unsandboxed = sum(1 for r in bash_results if r.get("sandboxed") is False)
    unknown = sum(1 for r in bash_results if r.get("sandboxed") is not True and r.get("sandboxed") is not False)
    unknown += max(0, bash_tool_uses - len(bash_results))
    return {"transcript_checked": True, "bash_results": len(bash_results), "unsandboxed": unsandboxed, "unknown": unknown,
            "bash_tool_uses": bash_tool_uses, "passed": unsandboxed == 0 and unknown == 0}


def _plugin_path(entry: object) -> str | None:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, Mapping):
        value = entry.get("path")
        return value if isinstance(value, str) else None
    return None


def integrity_violations(init: Mapping | None, drift: Mapping, *, mode: str, ws: Path, cli_version: str,
                         memory_dir_changed: bool, memory_saved_events: int, cli_sha256: str | None = None) -> list[str]:
    out: list[str] = []
    if init:
        own = os.path.abspath(Path(ws) / ".empty_plugins")
        for entry in init.get("plugins") or []:
            if ((cli_version, cli_sha256) == PINNED_PRIVACY_PLUGIN and isinstance(entry, Mapping)
                    and dict(entry) == {"name": "cc-plugin-privacy", "path": "builtin",
                                        "source": "cc-plugin-privacy@builtin"}):
                continue
            path = _plugin_path(entry)
            if path is None or os.path.abspath(path) != own:
                out.append("init.plugins holds a plugin other than the empty plugin directory")
                break
        for key in ("skills", "mcp_servers", "memory_paths"):
            if init.get(key):
                out.append(f"init.{key} is not empty")
        if init.get("claude_code_version") != cli_version:
            out.append(f"init.claude_code_version is not {cli_version}")
    if memory_saved_events:
        out.append("a memory_saved event appeared")
    if memory_dir_changed:
        out.append("the auto-memory directory changed")
    keys = ["upstream_modified", "upstream_removed", "logs_modified"]
    if mode in ("restricted", "contained"):
        keys += ["agents_modified", "quarantined", "reports_modified"]
    for key in keys:
        for rel in drift.get(key) or []:
            out.append(f"drift.{key}: {rel}")
    for problem in drift.get("snapshot_errors") or []:
        out.append(f"drift.snapshot_errors: {problem}")
    return out


@dataclass(frozen=True)
class SessionFacts:
    mode: str
    exit_code: int | None
    timed_out: bool
    result: Mapping | None
    pending_ok: bool
    stderr_text: str
    limit: Mapping
    model_check: Mapping
    sandbox_check: Mapping
    api_retry_errors: tuple[str, ...] = ()


def _int_status(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def classify_session(facts: SessionFacts, manifest: dict) -> tuple[str, str | None]:
    """(outcome of 2.11, outcome_detail): the table of 3.19, first match wins; text patterns only as the fallback."""
    f = facts
    result = f.result if isinstance(f.result, Mapping) else None
    if f.exit_code == 127:
        return "cli_missing", None
    if result is not None and _pick(result, "startup_failure_reason", "startupFailureReason") == \
            "sandbox_required_unavailable":
        return "sandbox_unavailable", None
    if (f.model_check or {}).get("mismatch") is not None:
        return "model_mismatch", str(f.model_check["mismatch"])
    if f.mode in ("restricted", "contained") and not (f.sandbox_check or {}).get("passed", False):
        return "settings_ignored", ("transcript_missing" if not f.sandbox_check.get("transcript_checked")
                                    else "unsandboxed")
    success = result is not None and result.get("is_error") is False
    if success and not f.timed_out and f.pending_ok:
        return "ok", None
    if success and not f.timed_out:
        return "no_candidates", None
    if f.timed_out:
        return "timeout", None
    subtype = result.get("subtype") if result is not None else None
    if subtype == "error_max_budget_usd":
        return "failed", "max_budget_usd"
    if subtype == "error_max_turns":
        return "failed", "max_turns"
    lim = f.limit or {}
    if lim.get("signal") == "rate_limit_event":
        if (lim.get("rate_limit_type") == "overage" and lim.get("overage_disabled_reason")) or \
                lim.get("error_code") == "credits_required":
            return "spend_limit", "rate_limit_event"
        if lim.get("resets_at_utc") and lim.get("rate_limit_type") in PLAN_WINDOW_TYPES:
            return "limit_window", lim.get("rate_limit_type")
        return "spend_limit", "unrecognised_limit"
    status = _int_status(result.get("api_error_status")) if result is not None else None
    text = (f.stderr_text or "").lower() + "\n" + (str(result.get("result") or "").lower() if result else "")
    prop = manifest.get("proposer", {})
    if status == 401 or "oauth session expired" in text or "failed to authenticate" in text or any(
            p.lower() in text for p in prop.get("auth_patterns") or []):
        return "auth", "status" if status == 401 else "text"
    if status in (402, 429):
        return "spend_limit", "status"
    if any(p.lower() in text for p in prop.get("usage_limit_patterns") or []):
        return "spend_limit", "text"
    last_retry = f.api_retry_errors[-1] if f.api_retry_errors else None
    if status is not None and (status == 408 or status >= 500):
        return "transient", "status"
    if last_retry in _NETWORK_RETRY_ERRORS:
        return "transient", "api_retry"
    if result is None:
        return "transient", "no_result_event"
    return "failed", None


# ================================================================ transcripts and tool-call signals

def locate_transcript(home: Path, session_id: str) -> Path | None:
    """The first match of <home>/.claude/projects/*/<session_id>.jsonl (the folder name is never computed)."""
    try:
        sid = util.safe_component(session_id)
    except ValueError:
        return None
    hits = sorted(glob.glob(os.path.join(glob.escape(str(Path(home) / ".claude" / "projects")), "*",
                                         glob.escape(sid) + ".jsonl")))
    for hit in hits:
        if os.path.isfile(hit) and not os.path.islink(hit):
            return Path(hit)
    return None


def _copy_file(src: Path, dest: Path, budget: list[int]) -> int | None:
    """Copy one regular file with O_NOFOLLOW within the remaining budget; None when skipped."""
    try:
        st = os.lstat(src)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_size > budget[0]:
        return None
    try:
        fd = os.open(src, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(budget[0] + 1)
    if len(data) > budget[0]:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as out:
        out.write(data)
    os.chmod(dest, 0o600)
    budget[0] -= len(data)
    return len(data)


def copy_transcript(src: Path, dest_dir: Path, *, max_bytes: int) -> dict:
    """Copy <sid>.jsonl, the sibling directory <sid>/ and the set-aside copies <sid>.orphaned-* and
    <sid>.jsonl.superseded-*, within max_bytes. Never follows a symlink; reads nothing else under the home directory."""
    out = {"found": False, "files": 0, "bytes": 0, "skipped": 0}
    src = Path(src)
    if not src.is_file() or src.is_symlink():
        return out
    out["found"] = True
    sid = src.name[:-len(".jsonl")] if src.name.endswith(".jsonl") else src.stem
    dest_dir = Path(dest_dir)
    budget = [int(max_bytes)]
    jobs: list[tuple[Path, Path]] = [(src, dest_dir / src.name)]
    for pattern in (f"{glob.escape(sid)}.orphaned-*", f"{glob.escape(sid)}.jsonl.superseded-*"):
        for hit in sorted(src.parent.glob(pattern)):
            jobs.append((hit, dest_dir / hit.name))
    side = src.parent / sid
    if side.is_dir() and not side.is_symlink():
        for dirpath, dirnames, filenames in os.walk(side, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not os.path.islink(os.path.join(dirpath, d)))
            for name in sorted(filenames):
                full = Path(dirpath) / name
                jobs.append((full, dest_dir / full.relative_to(src.parent)))
    for source, target in jobs:
        n = _copy_file(source, target, budget)
        if n is None:
            out["skipped"] += 1
        else:
            out["files"] += 1
            out["bytes"] += n
    return out


_SOLVER_PATTERNS = ("openrouter", "api.inference.wandb.ai", "litellm", "127.0.0.1")
_PATH_KEYS = ("file_path", "path", "notebook_path")


def _all_tool_uses(rows: Sequence[Mapping]) -> list[dict]:
    return [b for ev in _events(rows) if ev["type"] == "assistant" for b in _tool_uses(ev)]


def solver_attempt_signals(rows: Sequence[Mapping]) -> list[str]:
    """Bash inputs that name the solver path; one short line per call, no command text."""
    out = []
    for block in _all_tool_uses(rows):
        if block.get("name") != "Bash":
            continue
        inp = block.get("input") if isinstance(block.get("input"), Mapping) else {}
        command = str(inp.get("command") or "").lower()
        hits = [p for p in _SOLVER_PATTERNS if p in command]
        if hits:
            out.append(f"Bash {block.get('id', '?')}: names {', '.join(hits)}")
    return out


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def state_access_signals(rows: Sequence[Mapping], *, state_root: Path, ws: Path, package_dir: Path) -> list[str]:
    """Tool calls whose path arguments or command text name a path under state_root but outside ws, or under
    package_dir; relative paths are taken against ws. One short line per call, no content."""
    state, wsp, pkg = (os.path.normpath(os.path.abspath(str(p))) for p in (state_root, ws, package_dir))
    out = []
    for block in _all_tool_uses(rows):
        inp = block.get("input") if isinstance(block.get("input"), Mapping) else {}
        candidates = [str(inp[k]) for k in _PATH_KEYS if isinstance(inp.get(k), str)]
        if isinstance(inp.get("pattern"), str) and "/" in inp["pattern"]:
            candidates.append(inp["pattern"])
        if isinstance(inp.get("command"), str):
            candidates += [t for t in re.split(r"[\s'\"=;|&<>()`,]+", inp["command"]) if "/" in t or t.startswith("..")]
        kinds = set()
        for text in candidates:
            text = text.strip()
            if not text or text.startswith("~"):
                continue
            full = os.path.normpath(text if os.path.isabs(text) else os.path.join(wsp, text))
            if _under(full, pkg):
                kinds.add("package")
            elif _under(full, state) and not _under(full, wsp):
                kinds.add("state")
        if kinds:
            out.append(f"{block.get('name', '?')} {block.get('id', '?')}: names a {' and '.join(sorted(kinds))} path")
    return out


# ================================================================ cost

def _usd_micro(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        micro = util.micros(str(value))
    except (TypeError, ValueError):
        return None
    return micro if micro > 0 else None


def session_cost(result_event: Mapping | None, session: "tx.NormalizedSession | None", manifest: dict,
                 fallback_micro: int) -> tuple[int, str]:
    """(micro-dollars, basis). Zero needs explicit evidence of a pre-API failure.

    A generic zero still falls through: some CLI versions report zero on paid
    failures. An authentication failure before any API time with all token
    counters present and zero is a distinct, observed no-inference outcome.
    """
    if isinstance(result_event, Mapping):
        usage = result_event.get("usage") or {}
        token_keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        text = str(result_event.get("result") or "").lower()
        if (result_event.get("is_error") is True and result_event.get("terminal_reason") == "api_error"
                and result_event.get("duration_api_ms") == 0 and result_event.get("total_cost_usd") == 0
                and ("oauth session expired" in text or "failed to authenticate" in text)
                and isinstance(usage, Mapping) and all(usage.get(k) == 0 for k in token_keys)
                and result_event.get("modelUsage") == {}
                and (session is None or not tx.usage_by_model(session))):
            return 0, "no_api_auth_failure"
        micro = _usd_micro(result_event.get("total_cost_usd"))
        if micro:
            return micro, "result_event"
    if session is not None:
        state = session.cost_state or {}
        micro = state.get("total_cost_micro")
        if isinstance(micro, int) and not isinstance(micro, bool) and micro > 0:
            return micro, "cost_state"
        priced = tx.price_usage(tx.usage_by_model(session), util.lookup(manifest, "proposer.list_prices", {}) or {})
        if priced:
            return priced, "usage_sum"
    return max(int(fallback_micro), 1), "estimate"


# ================================================================ the proposal store

def _iter_dir(paths: StatePaths, iteration: int) -> Path:
    return paths.session_dir(iteration, 1).parent


def _ordinals(paths: StatePaths, iteration: int) -> list[int]:
    out = []
    for name in _ls(_iter_dir(paths, iteration)):
        m = re.fullmatch(r"s([1-9][0-9]{0,5})", name)
        if m:
            out.append(int(m.group(1)))
    return sorted(out)


def _read_doc(path: Path) -> dict | None:
    try:
        doc = util.read_json(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    return doc if isinstance(doc, dict) else None


def find_usable_proposal(paths: StatePaths, iteration: int) -> tuple[int, dict] | None:
    for k in reversed(_ordinals(paths, iteration)):
        sdir = paths.session_dir(iteration, k)
        session = _read_doc(sdir / "session.json")
        proposal = _read_doc(sdir / "proposal.json")
        if session and proposal and session.get("outcome") == "ok" and (sdir / "pending_eval.json").is_file():
            return k, proposal
    return None


def settled_failure(paths: StatePaths, iteration: int) -> dict | None:
    for k in reversed(_ordinals(paths, iteration)):
        session = _read_doc(paths.session_dir(iteration, k) / "session.json")
        if session is None:
            continue
        return session if session.get("outcome") in ("timeout", "failed", "no_candidates") else None
    return None


def candidate_reference(paths: StatePaths, name: str, sha256: str | None = None) -> tuple[Path, str] | None:
    """The stored copy of a proposed candidate: with a hash, the copy with that hash; without, the copy in the latest
    proposal (highest iteration, then highest ordinal) that lists the name."""
    if not isinstance(name, str) or not CANDIDATE_RE.fullmatch(name):
        return None
    found: list[tuple[int, int, Path, str]] = []
    for it_name in _ls(paths.proposer_root):
        m = re.fullmatch(r"iter-([0-9]{3,})", it_name)
        if not m:
            continue
        iteration = int(m.group(1))
        for k in _ordinals(paths, iteration):
            sdir = paths.session_dir(iteration, k)
            proposal = _read_doc(sdir / "proposal.json")
            for cand in (proposal or {}).get("candidates") or []:
                if not isinstance(cand, dict) or cand.get("name") != name or not cand.get("sha256"):
                    continue
                copy = sdir / "candidates" / f"{name}.py"
                if copy.is_file() and not copy.is_symlink() and util.sha256_file(copy) == cand["sha256"]:
                    found.append((iteration, k, copy, cand["sha256"]))
    if sha256 is not None:
        found = [f for f in found if f[3] == sha256]
    if not found:
        return None
    _, _, copy, digest = max(found, key=lambda f: (f[0], f[1]))
    return copy, digest


# ================================================================ small file helpers

def _read_nofollow(path: Path, max_bytes: int) -> bytes | None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(os.dup(fd), "rb") as fh:
            data = fh.read(max_bytes + 1)
        return None if len(data) > max_bytes else data
    finally:
        os.close(fd)


def _replace_file(dest: Path, data: bytes, mode: int = 0o644) -> None:
    """Write through a temp file and os.replace, which replaces a symlink instead of writing through it."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / f".{dest.name}.mhf-tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), mode)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    os.replace(tmp, dest)


def _write_canonical(path: Path, obj: object) -> str:
    data = util.canonical_json(obj)
    _replace_file(path, data, 0o600)
    os.chmod(path, 0o600)
    return util.sha256_bytes(data)


def _dir_hashes(directory: Path) -> dict[str, str]:
    out = {}
    for name in _ls(directory):
        full = Path(directory) / name
        data = _read_nofollow(full, 64 * 1024 * 1024)
        out[name] = util.sha256_bytes(data) if data is not None else "<not a regular file>"
    return out


def _find_uv_dir(base_env: Mapping[str, str]) -> Path | None:
    for entry in str(base_env.get("PATH", "")).split(os.pathsep):
        if entry and os.path.isfile(os.path.join(entry, "uv")) and os.access(os.path.join(entry, "uv"), os.X_OK):
            return Path(os.path.abspath(entry))
    return None


def _frontier_summary(path: Path) -> dict | None:
    doc = _read_doc(Path(path)) if Path(path).is_file() else None
    pareto = (doc or {}).get("_pareto")
    if not isinstance(pareto, list) or not pareto or not isinstance(pareto[0], dict):
        return None
    return {"system": pareto[0].get("system"), "val_accuracy": pareto[0].get("val_accuracy")}


# ================================================================ the runner

_SETTLED = ("no_candidates", "timeout", "failed")
_STOPS = {"spend_limit": "proposer_spend_limit", "auth": "proposer_auth", "model_mismatch": "proposer_model",
          "sandbox_unavailable": "proposer_sandbox", "settings_ignored": "proposer_settings", "cli_missing": "proposer_cli"}


@dataclass
class _Session:
    outcome: str
    detail: str | None
    record: dict
    result: object = None
    stops: list = field(default_factory=list)       # (reason, detail) raised after the record is written


class ProposerRunner:
    def __init__(self, manifest: dict, paths: StatePaths, *, journal: Journal, ledger: Ledger, upstream: workspace.Upstream,
                 example_dir: Path, lock_file: Path | None, base_env: Mapping[str, str], home: Path,
                 on_unit: Callable[[str], None] | None = None, halt_check: Callable[[], None] | None = None,
                 clock: util.Clock | None = None, rng: random.Random | None = None) -> None:
        self.manifest = manifest
        self.paths = paths
        self.journal = journal
        self.ledger = ledger
        self.upstream = upstream
        self.example_dir = Path(example_dir)
        self.lock_file = lock_file
        self.base_env = base_env
        self.home = Path(home)
        self.on_unit = on_unit
        self.halt_check = halt_check
        self.clock = clock or util.SystemClock()
        self.rng = rng or random.Random()
        self.frontier_before: dict | None = None
        self.last_session_key: str | None = None
        self.prop = manifest["proposer"]

    # ------------------------------------------------------------ public
    def propose(self, task_prompt: str, iteration: int, timeout: int = 2400) -> bool:
        if self.halt_check:
            self.halt_check()
        mh = self.upstream.meta_harness
        found = find_usable_proposal(self.paths, iteration)
        if found is not None:
            return self._replay(iteration, *found)
        failure = settled_failure(self.paths, iteration)
        self.frontier_before = _frontier_summary(Path(mh.FRONTIER_VAL))
        if failure is not None:
            self.last_session_key = failure.get("session_key")
            return False
        waits = transients = 0
        while True:
            s = self._session(task_prompt, iteration, timeout)
            for reason, detail in s.stops:
                raise CampaignStop(reason=reason, detail=detail)
            if s.outcome == "ok":
                show = getattr(s.result, "show", None)
                if callable(show):
                    show()
                return True
            if s.outcome in _SETTLED:
                show = getattr(s.result, "show", None)
                if s.outcome == "no_candidates" and callable(show):
                    show()                                  # a successful result: upstream's L188
                else:
                    self._print_failure(s)
                return False
            if s.outcome in _STOPS:
                raise CampaignStop(reason=_STOPS[s.outcome], detail=f"{s.record['session_key']}: {s.outcome}"
                                   + (f" ({s.detail})" if s.detail else ""))
            if s.outcome == "limit_window":
                waits += 1
                if waits >= 3:
                    raise CampaignStop(reason="proposer_limit_wait", detail="a third plan-window wait in one call")
                target = util.parse_utc(s.record["limit"]["resets_at_utc"]) + 60.0
                if target - self.clock.now() > float(self.prop["max_limit_wait_s"]):
                    raise CampaignStop(reason="proposer_limit_wait", detail="the plan window resets too late")
                self._sleep_until(target)
                continue
            if s.outcome == "transient":
                transients += 1
                if transients > int(self.prop["transient_retries"]):
                    raise CampaignStop(reason="proposer_transient", detail=f"{transients - 1} reruns did not help")
                lo, hi = self.prop["transient_backoff_s"]
                self._sleep_until(self.clock.now() + self.rng.uniform(float(lo), float(hi)))
                continue
            raise CampaignStop(reason="proposer_cli", detail=f"unhandled session outcome {s.outcome}")

    # ------------------------------------------------------------ helpers
    def _sleep_until(self, target: float) -> None:
        while True:
            remaining = target - self.clock.now()
            if remaining <= 0:
                return
            if self.halt_check:
                self.halt_check()
            self.clock.sleep(min(30.0, remaining))

    def _print_failure(self, s: _Session) -> None:
        mh = self.upstream.meta_harness
        red = getattr(mh, "_red", lambda t: t)
        dim = getattr(mh, "_dim", lambda t: t)
        print(f"  {red('proposer failed')} exit={s.record.get('exit_code')}")
        stderr = getattr(s.result, "stderr", "") or ""
        if stderr:
            print(f"  {dim(stderr[:500])}")

    def _worker_python(self) -> Path:
        sc = self.paths.root.parent.parent
        return Path(os.path.abspath(os.path.join(str(sc), str(self.manifest["upstream"]["worker_python"]))))

    def _replay(self, iteration: int, ordinal: int, proposal: dict) -> bool:
        mh = self.upstream.meta_harness
        sdir = self.paths.session_dir(iteration, ordinal)
        restored = []
        data = _read_nofollow(sdir / "pending_eval.json", 1 << 20)
        if data is None:
            raise CampaignStop(reason="internal_error", detail="stored pending_eval.json is unreadable")
        _replace_file(Path(mh.PENDING_EVAL), data)
        restored.append("pending_eval.json")
        for cand in proposal.get("candidates") or []:
            name, digest = cand.get("name"), cand.get("sha256")
            if not isinstance(name, str) or not CANDIDATE_RE.fullmatch(name) or not digest:
                continue
            copy = _read_nofollow(sdir / "candidates" / f"{name}.py", 1 << 22)
            if copy is None or util.sha256_bytes(copy) != digest:
                raise CampaignStop(reason="internal_error", detail=f"stored copy of candidate {name} is damaged")
            if workspace.candidate_sha256(self.paths, name) != digest:
                _replace_file(self.paths.ws / "agents" / f"{name}.py", copy)
                restored.append(f"agents/{name}.py")
        self.frontier_before = proposal.get("frontier_before")
        self.last_session_key = proposal.get("session_key")
        self.journal.record("proposer", f"iter{iteration:03d}", "replayed", attempt=ordinal,
                            detail={"from": f"s{ordinal}", "restored": restored})
        return True

    # ------------------------------------------------------------ one session (steps 3 to 14)
    def _session(self, task_prompt: str, iteration: int, timeout: int) -> _Session:
        prop, m, paths = self.prop, self.manifest, self.paths
        mh, cw = self.upstream.meta_harness, self.upstream.claude_wrapper
        key = f"iter{iteration:03d}"
        # step 3: budget
        if prop.get("max_budget_usd_per_session") is None:
            raise CampaignStop(reason="proposer_environment", detail="proposer.max_budget_usd_per_session is not set")
        worst = util.micros(prop["max_budget_usd_per_session"])
        try:
            self.ledger.check_meter_cap("proposer_list_price", extra_micro=worst)
        except MeterCapExceeded:
            self.ledger.set_halt("budget:proposer", "proposer list-price cap would be exceeded by one more session")
            raise CampaignStop(reason="budget:proposer", detail="the next session could exceed the proposer cap") from None
        # step 4: binary
        cli = prop.get("cli_path")
        if not cli or not os.path.isfile(cli) or not os.access(cli, os.X_OK) or \
                util.sha256_file(Path(cli)) != prop.get("cli_sha256"):
            raise CampaignStop(reason="proposer_cli", detail="the pinned CLI is missing or its hash differs")
        # step 5: session directory, workspace, snapshots
        k = self.journal.next_attempt("proposer", key)
        skey = f"{key}.s{k}"
        self.last_session_key = skey
        sdir = paths.session_dir(iteration, k)
        sdir.mkdir(parents=True, exist_ok=True)
        os.chmod(sdir, 0o700)
        ws = workspace.ensure_workspace(m, paths, self.example_dir, self.lock_file)
        logs = Path(mh.LOGS_DIR)
        python = self._worker_python()
        try:
            before = workspace.snapshot(paths)
            fp_before = workspace.env_fingerprint(python)
            example_venv = procenv.venv_root(python)
        except WorkspaceError as exc:
            raise CampaignStop(reason="proposer_environment", detail=str(exc)) from None
        mem_before = workspace.memory_dir_fingerprint(self.home, ws)
        summary = Path(getattr(mh, "EVOLUTION_SUMMARY", logs / "evolution_summary.jsonl"))
        summary_before = _read_nofollow(summary, 1 << 28) if os.path.lexists(summary) else None
        if summary_before is not None:
            _replace_file(sdir / "evolution_summary.before.jsonl", summary_before, 0o600)
        # step 6: scratch and environment
        mode = util.lookup(m, "decisions.proposer_permission_mode")
        if mode not in MODES:
            raise CampaignStop(reason="proposer_environment", detail=f"permission mode {mode!r} is not usable")
        scratch = proposer_scratch(prop["scratch_root"], m["campaign"], skey)
        state_real = os.path.realpath(paths.root)
        if os.path.realpath(scratch) != str(scratch) or not os.path.isabs(str(scratch)) or \
                _under(os.path.realpath(scratch), state_real):
            raise CampaignStop(reason="proposer_environment",
                               detail="the scratch directory must be given in resolved form and lie outside STATE")
        scratch.mkdir(parents=True, exist_ok=True)
        os.chmod(scratch, 0o700)
        try:
            env = procenv.proposer_env(m, self.base_env, workspace_root=paths.workspace_root, example_venv=example_venv,
                                       scratch_dir=scratch)
        except ValueError as exc:
            raise CampaignStop(reason="proposer_environment", detail=str(exc)) from None
        util.write_json(sdir / "env.json", {"schema": "mhforge.proposer_env/1", "names": sorted(env)})
        # step 7: settings and argv
        reports = logs / "reports"
        if mode in ("restricted", "contained"):
            reports.mkdir(parents=True, exist_ok=True)
        reports_before = _dir_hashes(reports)
        inputs = take_settings_inputs(m, paths, mode=mode, logs_dir=logs, scratch_dir=scratch, home=self.home,
                                      example_venv=example_venv, uv_dir=_find_uv_dir(self.base_env), uid=os.getuid())
        override = util.lookup(m, "proposer.restricted.settings", None)
        settings = generate_settings(inputs, override=override)
        merged = bool(override) and mode != "upstream"      # generate_settings merged the override (LD6)
        problems = check_settings(settings, mode, base=generate_settings(inputs) if merged else None)
        if problems:
            raise CampaignStop(reason="proposer_environment", detail="; ".join(problems))
        settings_sha = _write_canonical(sdir / "settings.json", settings)
        addendum = render_addendum(scratch) if mode == "restricted" else None
        spec = ArgvSpec(mode=mode, cli_path=cli, model=prop["model"], effort=prop["effort"],
                        max_budget_usd=str(prop["max_budget_usd_per_session"]), settings_file=sdir / "settings.json",
                        scratch_dir=scratch if mode != "upstream" else None, addendum=addendum)
        # step 8: started; the session counts at its worst case from here on
        started_utc = util.utc_iso(self.clock.now())
        t0 = self.clock.monotonic()
        self.journal.record("proposer", key, "started", attempt=k, detail={"mode": mode, "session_key": skey})
        self.ledger.meter_open("proposer_list_price", skey, worst, "micro_usd", worst_micro=worst)
        shim = SubprocessShim(env=env, cli_path=cli, stream_path=sdir / "stream.jsonl", stderr_path=sdir / "stderr.txt",
                              timeout_s=float(timeout), grace_s=tuple(prop["grace_s"]), clock=self.clock)
        closed = False
        try:
            built: list[list[str]] = []
            original = cw.build_command

            def build_command(*args, **kwargs):
                cmd = session_argv(original(*args, **kwargs), spec)
                built.append(cmd)
                return cmd

            # step 9: the session
            with installed(cw, shim, build_command=build_command):
                result = cw.run(prompt=task_prompt, model=prop["model"], allowed_tools=mh.PROPOSER_ALLOWED_TOOLS,
                                skills=[str(Path(mh.EVOLVE_DIR) / ".claude/skills/meta-harness")],
                                cwd=str(mh.EVOLVE_DIR), log_dir=str(logs / "claude_sessions"), name=f"iter{iteration}",
                                timeout_seconds=None, effort=prop["effort"])
            proc = shim.last
            ran = list(proc.args) if proc is not None else (built[-1] if built else [cli])
            util.write_json(sdir / "command.json", command_fingerprint(ran))
            reaped = proc.reap_group() if proc is not None else []
            # step 10: the stream and pending_eval.json
            if proc is not None:
                proc.stdout.eof.wait(5.0)
            ended_utc = util.utc_iso(self.clock.now())
            duration = max(0.0, self.clock.monotonic() - t0)
            pending_ok, pending_info, candidates = self._take_pending(Path(mh.PENDING_EVAL), sdir)
            # step 11: transcript, drift, guards
            rows = util.read_jsonl(sdir / "stream.jsonl")
            parsed = parse_stream(rows)
            sid = (parsed["init"] or {}).get("session_id") or (parsed["result"] or {}).get("session_id")
            sid = sid if isinstance(sid, str) else None
            src = locate_transcript(self.home, sid) if sid else None
            tcopy = copy_transcript(src, sdir / "transcript", max_bytes=int(prop["max_transcript_bytes"])) if src \
                else {"found": False, "files": 0, "bytes": 0, "skipped": 0}
            drift = self._drift(before, summary, summary_before, reports_before, reports, logs)
            fp_after = workspace.env_fingerprint(python)
            mem_after = workspace.memory_dir_fingerprint(self.home, ws)
            solver = solver_attempt_signals(rows)
            access = state_access_signals(rows, state_root=paths.root, ws=ws, package_dir=_package_dir())
            # step 12: session checks
            stops: list[tuple[str, str]] = []
            session = None
            if tcopy["found"] and sid:
                try:
                    recs, subs = tx.read_session_dir(sdir / "transcript", sid, max_bytes=int(prop["max_transcript_bytes"]))
                    session = tx.normalize_transcript(recs, expect_version=prop.get("cli_version"), subagents=subs)
                except TranscriptError as exc:
                    if exc.kind == "version":
                        stops.append(("proposer_cli", f"{skey}: transcript written by another CLI version"))
                except FileNotFoundError:
                    session = None
            limit = limit_signal(parsed)
            model_check = check_model(parsed, requested_model=prop["model"],
                                      expected_resolved=prop.get("expected_resolved_model"),
                                      requested_effort=prop["effort"],
                                      request_efforts=tx.request_efforts(session) if session else None)
            sandbox_check = check_sandboxed(tx.bash_results(session) if session else None, parsed["bash_tool_uses"])
            memory_saved = sum(1 for ev in parsed["system"] if ev.get("subtype") == "memory_saved")
            violations = integrity_violations(parsed["init"], drift, mode=mode, ws=ws, cli_version=prop.get("cli_version"),
                                              memory_dir_changed=mem_before != mem_after,
                                              memory_saved_events=memory_saved, cli_sha256=prop.get("cli_sha256"))
            # step 13: cost
            estimate = max(self.ledger.meter_max("proposer_list_price"),
                           util.micros(prop["session_estimate_usd"]) if prop.get("session_estimate_usd") else 0)
            micro, basis = session_cost(parsed["result"], session, m, estimate)
            self.ledger.meter_close("proposer_list_price", skey, micro, micro_usd=micro)
            closed = True
            # step 14: classify and record
            stderr_text = (_read_nofollow(sdir / "stderr.txt", 1 << 20) or b"").decode("utf-8", "replace")
            facts = SessionFacts(mode=mode, exit_code=getattr(result, "exit_code", None),
                                 timed_out=bool(proc and proc.timed_out), result=parsed["result"], pending_ok=pending_ok,
                                 stderr_text=stderr_text, limit=limit, model_check=model_check,
                                 sandbox_check=sandbox_check,
                                 api_retry_errors=tuple(str(e.get("error")) for e in parsed["api_retries"]))
            outcome, detail = classify_session(facts, m)
            if fp_after != fp_before:
                stops.insert(0, ("proposer_environment", f"{skey}: the example venv changed during the session"))
            if violations:
                stops.append(("proposer_integrity", f"{skey}: " + "; ".join(violations[:3])))
            record = self._record(iteration, k, skey, mode, cli, ran, settings_sha, override, addendum, scratch, env,
                                  started_utc, ended_utc, duration, timeout, proc, reaped, result, outcome, detail,
                                  sid, model_check, parsed, limit, sandbox_check, violations, drift, micro, basis,
                                  pending_info, tcopy, fp_before, fp_after, mem_before != mem_after, solver, access, ws)
            util.write_json(sdir / "session.json", record)
            stored = []
            if outcome == "ok":
                stored = self._store_proposal(iteration, skey, sdir, candidates)
            jdetail = {"outcome": outcome, "session_id": sid,
                       "candidates": [{"name": c["name"], "sha256": c["sha256"], "base_system": c.get("base_system")}
                                      for c in stored],
                       "list_price_micro": micro, "duration_s": round(duration, 3)}
            if outcome == "limit_window":
                jdetail["wait_until_utc"] = limit.get("resets_at_utc")
            self.journal.record("proposer", key, "complete", attempt=k, detail=jdetail)
        except BaseException:
            if shim.last is not None:                       # the whole group, even when the CLI itself has ended
                shim.last.kill()
            if not closed:
                self.ledger.meter_close("proposer_list_price", skey, worst, micro_usd=worst)
                self.journal.record("proposer", key, "interrupted", attempt=k,
                                    detail={"outcome": "interrupted", "list_price_micro": worst})
            raise
        if self.on_unit:
            self.on_unit(f"proposer:{skey}")
            for c in stored:
                self.on_unit(f"propose:{c['name']}")
        return _Session(outcome=outcome, detail=detail, record=record, result=result, stops=stops)

    def _take_pending(self, pending: Path, sdir: Path) -> tuple[bool, dict, list[dict]]:
        info: dict = {"present": False, "sha256": None, "candidates": []}
        if not os.path.lexists(pending):
            return False, info, []
        info["present"] = True
        data = _read_nofollow(pending, workspace.PENDING_EVAL_MAX_BYTES)
        if data is None:
            return False, info, []
        _replace_file(sdir / "pending_eval.json", data, 0o600)
        info["sha256"] = util.sha256_bytes(data)
        try:
            cands = workspace.read_pending_eval(sdir / "pending_eval.json")
        except WorkspaceError:
            return False, info, []
        for c in cands:
            valid = bool(CANDIDATE_RE.fullmatch(c["name"]))
            info["candidates"].append({"name": c["name"][:64], "base_system": c.get("base_system"), "valid_name": valid,
                                       "file_sha256": workspace.candidate_sha256(self.paths, c["name"]) if valid
                                       else None})
        return True, info, cands

    def _drift(self, before: dict, summary: Path, summary_before: bytes | None, reports_before: dict,
               reports: Path, logs: Path) -> dict:
        paths = self.paths
        errors: list[str] = []
        stamp = util.utc_iso(self.clock.now())
        quarantined: list[str] = []
        try:
            stray = workspace.unknown_entries(paths)
            if stray:
                quarantined = workspace.quarantine_entries(paths, stray, stamp)
                self.journal.record("workspace", "quarantine", "complete", detail={"paths": quarantined})
        except WorkspaceError as exc:
            errors.append(f"quarantine: {exc}")
        try:
            after = workspace.snapshot(paths)
        except WorkspaceError as exc:
            errors.append(f"snapshot: {exc}")
            after = dict(before)
        drift = workspace.classify_drift(before, after)
        drift["quarantined"] = quarantined
        restored = []
        if drift["upstream_modified"] or drift["upstream_removed"]:
            restored = workspace.restore_pinned(paths, self.example_dir, {"modified": drift["upstream_modified"],
                                                                          "removed": drift["upstream_removed"]})
            if restored:
                self.journal.record("workspace", "restore", "complete", detail={"paths": restored})
        drift["restored"] = restored
        logs_modified = []
        now = _read_nofollow(summary, 1 << 28) if os.path.lexists(summary) else None
        if now != summary_before or (summary_before is None and os.path.lexists(summary)):
            rel = os.path.relpath(summary, paths.ws)
            if summary_before is None:
                if os.path.isdir(summary) and not os.path.islink(summary):
                    shutil.rmtree(summary)
                elif os.path.lexists(summary):
                    os.unlink(summary)
            else:
                _replace_file(summary, summary_before)
            logs_modified.append(rel)
        drift["logs_modified"] = logs_modified
        reports_after = _dir_hashes(reports)
        drift["reports_added"] = sorted(n for n in reports_after if n not in reports_before)
        drift["reports_modified"] = sorted(os.path.relpath(reports / n, paths.ws) for n, h in reports_before.items()
                                           if reports_after.get(n) != h)
        drift["snapshot_errors"] = errors
        return drift

    def _store_proposal(self, iteration: int, skey: str, sdir: Path, candidates: list[dict]) -> list[dict]:
        stored, entries = [], []
        for c in candidates:
            name = c["name"]
            valid = bool(CANDIDATE_RE.fullmatch(name))
            entry = {"name": name, "file": None, "sha256": None, "base_system": c.get("base_system"),
                     "axis": c.get("axis"), "hypothesis": c.get("hypothesis"), "components": c.get("components"),
                     "valid_name": valid}
            data = _read_nofollow(self.paths.ws / "agents" / f"{name}.py", workspace.MAX_AGENT_FILE_BYTES) \
                if valid else None
            if data is not None:
                _replace_file(sdir / "candidates" / f"{name}.py", data, 0o600)
                entry.update({"file": f"candidates/{name}.py", "sha256": util.sha256_bytes(data)})
                stored.append(entry)
            entries.append(entry)
        pending_sha = util.sha256_file(sdir / "pending_eval.json")
        util.write_json(sdir / "proposal.json", {
            "schema": "mhforge.proposal/1", "iteration": iteration, "session_key": skey,
            "pending_eval_sha256": pending_sha, "frontier_before": self.frontier_before, "candidates": entries,
            "stored_utc": util.utc_iso(self.clock.now())})
        return stored

    def _record(self, iteration, k, skey, mode, cli, ran, settings_sha, override, addendum, scratch, env, started_utc,
                ended_utc, duration, timeout, proc, reaped, result, outcome, detail, sid, model_check, parsed, limit,
                sandbox_check, violations, drift, micro, basis, pending_info, tcopy, fp_before, fp_after, mem_changed,
                solver, access, ws) -> dict:
        prop = self.prop
        fp = command_fingerprint(ran)
        init = parsed["init"] or {}
        res = parsed["result"]
        cost = res.get("total_cost_usd") if res else None
        log_dir = getattr(result, "log_dir", None)
        return {
            "schema": "mhforge.session/1", "campaign": self.manifest["campaign"], "iteration": iteration, "ordinal": k,
            "session_key": skey, "permission_mode": mode,
            "cli": {"path": cli, "sha256": prop.get("cli_sha256"), "version": prop.get("cli_version")},
            "argv_sha256": fp["argv_sha256"], "argv_flags": fp["argv_flags"],
            "settings_sha256": settings_sha, "settings_override": bool(override) and mode != "upstream",
            "addendum_sha256": util.sha256_bytes(addendum.encode("utf-8")) if addendum is not None else None,
            "scratch_dir": str(scratch), "env_names": sorted(env),
            "started_utc": started_utc, "ended_utc": ended_utc, "duration_s": round(duration, 3), "timeout_s": timeout,
            "paused_s": proc.paused_s if proc else 0.0,
            "exit_code": getattr(result, "exit_code", None), "timed_out": bool(proc and proc.timed_out),
            "signals": [list(s) for s in proc.signals] if proc else [], "reaped": reaped,
            "outcome": outcome, "outcome_detail": detail, "session_id": sid,
            "requested_model": prop["model"], "resolved_model": model_check.get("resolved_model"), "effort": prop["effort"],
            "model_check": model_check,
            "init": {"tools": init.get("tools") or [], "plugins": init.get("plugins") or [],
                     "plugin_errors": init.get("plugin_errors") or [], "skills": init.get("skills") or [],
                     "mcp_servers": init.get("mcp_servers") or [], "memory_paths": init.get("memory_paths") or [],
                     "permission_mode": init.get("permissionMode"),
                     "claude_code_version": init.get("claude_code_version")} if parsed["init"] else None,
            "result_event": {"present": res is not None, "subtype": (res or {}).get("subtype"),
                             "is_error": (res or {}).get("is_error"),
                             "api_error_status": (res or {}).get("api_error_status"),
                             "total_cost_usd": None if cost is None or isinstance(cost, bool) else str(cost),
                             "num_turns": (res or {}).get("num_turns"), "usage": (res or {}).get("usage") or {},
                             "model_usage": (res or {}).get("modelUsage") or {},
                             "permission_denials": len((res or {}).get("permission_denials") or []),
                             "startup_failure_reason": _pick(res or {}, "startup_failure_reason",
                                                             "startupFailureReason")},
            "limit": limit, "sandbox_check": sandbox_check,
            "integrity": {"violations": violations, "reports_added": drift.get("reports_added", [])},
            "list_price_micro": micro, "list_price_basis": basis,
            "pending_eval": pending_info, "transcript": tcopy,
            "drift": {k2: v for k2, v in drift.items() if k2 not in ("reports_added", "snapshot_errors")},
            "env_fingerprint_before": fp_before, "env_fingerprint_after": fp_after, "auto_memory_dir_changed": mem_changed,
            "solver_attempt_signals": solver, "state_access_signals": access, "evidence_exposed": bool(access),
            "wrapper_log_dir": os.path.relpath(log_dir, ws) if isinstance(log_dir, str) else None,
        }
