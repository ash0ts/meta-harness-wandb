# SPDX-License-Identifier: MIT
"""The only place child environments are built (spec 3.5): worker, route, proposer (closed list and deny-list),
passthrough, exporter, minimal; the host's credential-name rule; the venv-root and tokenizer-directory path rules.

Every function takes the mapping it reads from as an argument; none reads the process environment, and no interpreter
path is ever made canonical by symlink resolution (the example venv's python is a symlink to a base interpreter that
has no litellm).
"""

from __future__ import annotations

import glob
import os
import re
from pathlib import Path
from typing import Mapping, Sequence

from . import wire
from .errors import WorkspaceError

__all__ = [
    "worker_env", "route_env", "PROPOSER_FIXED_ENV", "PROPOSER_DENY_NAMES", "PROPOSER_DENY_PREFIXES", "PLACEHOLDERS",
    "proposer_env", "assert_proposer_env", "passthrough_env", "exporter_env", "minimal_env", "is_credential_name",
    "assert_no_secret_names", "venv_root", "find_tokenizer_dir",
]

PROPOSER_FIXED_ENV: tuple[str, ...] = ("PYTHONPATH", "UV_CACHE_DIR", "UV_OFFLINE", "UV_PYTHON_DOWNLOADS",
                                       "PYTHONDONTWRITEBYTECODE", "CLAUDE_CODE_DISABLE_AUTO_MEMORY", "DISABLE_AUTOUPDATER",
                                       "CLAUDE_CODE_STARTUP_FAILURE_RESULTS")
PROPOSER_DENY_NAMES: frozenset[str] = frozenset({
    "CLAUDE_CODE_EFFORT_LEVEL", "CLAUDE_CODE_ENTRYPOINT", "CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_RETRY_WATCHDOG", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_TRANSCRIPT_LOCAL_GC",
    "ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "API_TIMEOUT_MS", "DISABLE_MICROCOMPACT", "MAX_THINKING_TOKENS", "NODE_OPTIONS", "TRACEPARENT",
    "TEXT_CLASSIFICATION_LLM_CACHE_DIR"})
PROPOSER_DENY_PREFIXES: tuple[str, ...] = ("OTEL_", "WANDB_", "OPENROUTER_", "OPENAI_", "LITELLM_", "ANTHROPIC_", "MHFORGE_")
_FIXED_CLAUDE_NAMES = frozenset(n for n in PROPOSER_FIXED_ENV if n.startswith("CLAUDE"))
_LOOPBACK_GATEWAY = "http://127.0.0.1:"
PLACEHOLDERS = ("{example_venv}", "{workspace_root}", "{uv_dir}")
_PLACEHOLDER_RE = re.compile(r"\{[^{}]*\}")
_VOCAB_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


# ---------------------------------------------------------------- worker and route

def worker_env(*, path: str, home: str, tmp: str, pythonpath: str, cache_dir: str, tokenizer_dir: str,
               extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The `env` object of the job file (2.6): exactly these names, plus `extra`."""
    env = {
        "PATH": str(path), "HOME": str(home), "TMPDIR": str(tmp), "PYTHONPATH": str(pythonpath),
        "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "TEXT_CLASSIFICATION_LLM_CACHE_DIR": str(cache_dir),
        "LITELLM_LOCAL_MODEL_COST_MAP": "True", "LITELLM_MODE": "PRODUCTION", "TIKTOKEN_RS_CACHE_DIR": str(tokenizer_dir),
        "NO_PROXY": "127.0.0.1,localhost",
    }
    for name, value in (extra or {}).items():
        env[str(name)] = str(value)
    assert_no_secret_names(env, allow=("OPENROUTER_API_KEY",))   # only the Route 2 job token or the relay placeholder
    return env


def route_env(route: int, *, base_url: str, api_key: str) -> dict[str, str]:
    """Route 2: OPENROUTER_API_BASE and OPENROUTER_API_KEY (the per-job token, or the relay placeholder).
    Route 1: {} (the URL travels as --api-base)."""
    if route == 2 and not isinstance(route, bool):
        return {"OPENROUTER_API_BASE": str(base_url), "OPENROUTER_API_KEY": str(api_key)}
    if route == 1 and not isinstance(route, bool):
        return {}
    raise ValueError("route must be 1 or 2")


# ---------------------------------------------------------------- proposer

def _uv_dir(base: Mapping[str, str]) -> str | None:
    for entry in str(base.get("PATH", "")).split(os.pathsep):
        if not entry:
            continue
        candidate = os.path.join(entry, "uv")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return os.path.abspath(entry)
    return None


def _substitute(text: str, values: Mapping[str, str | None]) -> str | None:
    """Placeholders replaced; None when a placeholder has no value (the entry is dropped); ValueError for an unknown one."""
    for found in _PLACEHOLDER_RE.findall(text):
        if found not in values:
            raise ValueError(f"unknown placeholder {found} in a proposer environment entry")
        if values[found] is None:
            return None
        text = text.replace(found, str(values[found]))
    return text


def proposer_env(manifest: dict, base: Mapping[str, str], *, workspace_root: Path, example_venv: Path,
                 scratch_dir: Path) -> dict[str, str]:
    """Built from an empty mapping: proposer.env_allow names present in `base`, PATH from proposer.path_dirs,
    proposer.env_set, then the closed list PROPOSER_FIXED_ENV. Passed through assert_proposer_env before it returns."""
    prop = manifest["proposer"]
    values: dict[str, str | None] = {"{example_venv}": str(example_venv), "{workspace_root}": str(workspace_root),
                                     "{uv_dir}": _uv_dir(base)}
    env: dict[str, str] = {}
    for name in prop["env_allow"]:
        if name in base:
            env[name] = str(base[name])
    dirs = []
    for entry in prop["path_dirs"]:
        value = _substitute(entry, values)
        if value:
            dirs.append(value)
    env["PATH"] = os.pathsep.join(dirs)
    for name, raw in prop["env_set"].items():
        value = _substitute(raw, values)
        if value is not None:
            env[name] = value
    env.update({
        "PYTHONPATH": str(workspace_root),
        "UV_CACHE_DIR": str(Path(scratch_dir) / ".uv-cache"),
        "UV_OFFLINE": "1",
        "UV_PYTHON_DOWNLOADS": "never",
        "PYTHONDONTWRITEBYTECODE": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_STARTUP_FAILURE_RESULTS": "1",
    })
    assert_proposer_env(env)
    assert_no_secret_names(env)                          # never a key or token, whatever the manifest allows
    return env


def assert_proposer_env(env: Mapping[str, str]) -> None:
    """ValueError naming the first variable that must never reach a proposer (the value is never named)."""
    for name in sorted(env):
        if name in PROPOSER_DENY_NAMES:
            raise ValueError(f"proposer environment holds the denied variable {name}")
        for prefix in PROPOSER_DENY_PREFIXES:
            if name.startswith(prefix):
                raise ValueError(f"proposer environment holds {name} (denied prefix {prefix})")
        if name.startswith("CLAUDE") and name not in _FIXED_CLAUDE_NAMES:
            raise ValueError(f"proposer environment holds {name} (a CLAUDE* name outside the closed list)")
        if _LOOPBACK_GATEWAY in str(env[name]):
            raise ValueError(f"proposer environment variable {name} holds a loopback gateway address")


# ---------------------------------------------------------------- other children

def passthrough_env(*, home: str, python_bin_dir: str) -> dict[str, str]:
    """For benchmark.py --frontier/--results and the stub process of LocalRelayExecutor."""
    return {"PATH": f"{python_bin_dir}:/usr/bin:/bin", "HOME": str(home), "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1"}


def exporter_env(manifest: dict, base: Mapping[str, str], *, home: str, package_root: Path,
                 netrc_file: Path) -> dict[str, str]:
    """PATH, HOME, LANG when present, PYTHONPATH, NETRC (the empty state file), WANDB_CONFIG_DIR (= HOME),
    WANDB_DISABLE_WEAVE=true. No key: the spawner hands the export key over an inherited pipe.

    LD9: `home` is an empty directory under STATE, never the caller's HOME (Agent Lens reads $HOME/.netrc whatever NETRC
    says; wandb reads $WANDB_CONFIG_DIR/settings or ~/.config/wandb/settings). Nothing else of `base` is carried, so no
    OTEL_*, CES_*, CWSANDBOX_*, proxy or other WANDB_* variable reaches the child."""
    if base.get("HOME") and os.path.abspath(str(home)) == os.path.abspath(str(base["HOME"])):
        raise ValueError("the exporter's HOME must be its own empty directory, not the caller's HOME")
    env = {"PATH": str(base.get("PATH") or "/usr/bin:/bin"), "HOME": str(home)}
    if "LANG" in base:
        env["LANG"] = str(base["LANG"])
    env["PYTHONPATH"] = str(package_root)
    env["NETRC"] = str(netrc_file)
    env["WANDB_CONFIG_DIR"] = str(home)
    env["WANDB_DISABLE_WEAVE"] = "true"
    key_names = {manifest["provider"]["api_key_env"], manifest["telemetry"]["api_key_env"], manifest["sandbox"]["auth_env"]}
    leaked = sorted(key_names & set(env))
    if leaked:
        raise ValueError(f"exporter environment would hold the key variable {leaked[0]}")
    assert_no_secret_names(env)
    return env


def minimal_env(base: Mapping[str, str], *, home: str) -> dict[str, str]:
    """PATH=/usr/bin:/bin and HOME; for caffeinate and `ps`."""
    return {"PATH": "/usr/bin:/bin", "HOME": str(home)}


# ---------------------------------------------------------------- credential names

def is_credential_name(name: str) -> bool:
    """The one credential-name rule on the host: re.search(wire.CREDENTIAL_NAME_PATTERN, name.upper())."""
    return re.search(wire.CREDENTIAL_NAME_PATTERN, str(name).upper()) is not None


def assert_no_secret_names(env: Mapping[str, str], *, allow: Sequence[str] = ()) -> None:
    allowed = frozenset(allow)
    for name in sorted(env):
        if name not in allowed and is_credential_name(name):
            raise ValueError(f"environment holds the credential-shaped variable {name}")


# ---------------------------------------------------------------- interpreter path rule (2.3)

def venv_root(python: Path) -> Path:
    """Path(os.path.abspath(python)).parent.parent, which must contain pyvenv.cfg. Lexical: never resolved."""
    root = Path(os.path.abspath(os.fspath(python))).parent.parent
    if not (root / "pyvenv.cfg").is_file():
        raise WorkspaceError(f"not a virtual environment interpreter (no pyvenv.cfg beside {root.name}/bin)")
    return root


def find_tokenizer_dir(python: Path, vocab_file: str) -> Path:
    """<venv_root>/lib/python*/site-packages/litellm/litellm_core_utils/tokenizers holding vocab_file. Imports nothing."""
    if not isinstance(vocab_file, str) or not _VOCAB_RE.fullmatch(vocab_file):
        raise WorkspaceError("vocab_file must be a plain file name")
    root = venv_root(python)
    pattern = os.path.join(glob.escape(str(root)), "lib", "python*", "site-packages", "litellm", "litellm_core_utils",
                           "tokenizers")
    for hit in sorted(glob.glob(pattern)):
        if (Path(hit) / vocab_file).is_file():
            return Path(hit)
    raise WorkspaceError("LiteLLM's bundled tokenizers directory with the vocabulary file was not found in the venv")
