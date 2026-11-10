# SPDX-License-Identifier: MIT
"""Verified working copy of the pinned upstream example, drift detection and restore, upstream import context (spec 3.14).

Standard library only at import time; ``yaml`` is imported only inside :func:`load_config` and, through upstream's own
``import yaml``, inside :func:`load_upstream`. Nothing here follows a symlink, writes under the example directory or puts a
workspace directory on ``sys.path``.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import inspect
import json
import os
import re
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Iterator, Mapping, Sequence

from . import util
from .errors import MhforgeError, UpstreamContractError, WorkspaceError
from .paths import StatePaths
from .wire import CANDIDATE_RE

__all__ = [
    "PINNED_FILES", "PINNED_AGENTS", "MAX_AGENT_FILE_BYTES", "MAX_AGENT_FILES", "UPSTREAM_MODULES", "source_digest",
    "ensure_workspace", "verify_pinned", "restore_pinned", "snapshot", "classify_drift", "unknown_entries",
    "quarantine_entries", "candidate_sha256", "validate_candidate_file", "read_pending_eval", "env_fingerprint",
    "memory_dir_fingerprint", "load_config", "Upstream", "load_upstream", "check_upstream_contract",
]

# The 39 files git tracks under EX at f805ad3 (relative to EX, posix).
PINNED_FILES: tuple[str, ...] = (
    ".claude/skills/meta-harness/SKILL.md", "README.md", "__init__.py",
    "agents/__init__.py", "agents/fewshot_all.py", "agents/fewshot_memory.py", "agents/no_memory.py",
    "benchmark.py", "claude_wrapper.py", "config.yaml",
    "data/README.md", "data/__init__.py",
    "data/aegis2/test.jsonl", "data/aegis2/train.jsonl", "data/aegis2/valid.jsonl",
    "data/api.py", "data/constants.py",
    "data/crime_prediction/test.jsonl", "data/crime_prediction/train.jsonl", "data/crime_prediction/val.jsonl",
    "data/evaluators.py",
    "data/finer/test.jsonl", "data/finer/train.jsonl", "data/finer/val.jsonl",
    "data/loaders.py",
    "data/symptom_diagnosis/test.jsonl", "data/symptom_diagnosis/train.jsonl", "data/symptom_diagnosis/val.jsonl",
    "data/uspto/test.jsonl", "data/uspto/train.jsonl", "data/uspto/val.jsonl",
    "inner_loop.py", "llm.py", "memory_system.py", "meta_harness.py", "pyproject.toml",
    "tests/test_benchmark.py", "tests/test_data.py", "tests/test_evolution_contracts.py",
)
PINNED_AGENTS = ("__init__", "no_memory", "fewshot_memory", "fewshot_all")
MAX_AGENT_FILE_BYTES = 262_144                       # same values as bundle.py (a later layer)
MAX_AGENT_FILES = 400
UPSTREAM_MODULES = ("claude_wrapper", "benchmark", "meta_harness")     # load order (3.14)
PENDING_EVAL_MAX_BYTES = 65_536

_PINNED_SET = frozenset(PINNED_FILES)
_PINNED_DIRS = frozenset(str(Path(p).parents[i].as_posix()) for p in PINNED_FILES
                         for i in range(len(Path(p).parts) - 1))
_ALLOWED_TOP_DIRS = ("agents", "logs", ".empty_plugins")
_UTC_DIR_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z:._-]{0,63}")
_AGENT_PY_RE = re.compile(r"agents/([^/]+)\.py")
_YAML_MISSING = ("PyYAML is not importable in this interpreter: run this command with the example venv's Python "
                 "(PYTHONPATH=<showcase> <example>/.venv/bin/python -m mhforge ...) or with an environment that has the "
                 "`live` extra installed")


# ---------------------------------------------------------------- no-follow primitives

def _rel_ok(rel: object) -> str:
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel or "\0" in rel:
        raise WorkspaceError(f"not a relative workspace path: {str(rel)[:80]!r}")
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise WorkspaceError(f"not a normalised relative path: {rel[:80]!r}")
    return rel


def _check_parents(root: Path, rel: str) -> None:
    """Every directory between root and rel must be a real directory (never a symlink). FileNotFoundError when absent."""
    node = Path(root)
    for part in rel.split("/")[:-1]:
        node = node / part
        st = os.lstat(node)
        if stat.S_ISLNK(st.st_mode):
            raise WorkspaceError(f"symlink on the path to {rel}")
        if not stat.S_ISDIR(st.st_mode):
            raise WorkspaceError(f"not a directory on the path to {rel}")


def _read_nofollow(path: Path, *, max_bytes: int | None = None) -> bytes:
    """Bytes of a regular file opened with O_NOFOLLOW; WorkspaceError on a symlink or a non-regular file."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            raise
        if os.path.islink(path):
            raise WorkspaceError(f"symlink refused: {Path(path).name}") from None
        raise WorkspaceError(f"cannot open {Path(path).name}: {type(exc).__name__}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise WorkspaceError(f"not a regular file: {Path(path).name}")
        if max_bytes is not None and st.st_size > max_bytes:
            raise WorkspaceError(f"{Path(path).name}: larger than {max_bytes} bytes")
        chunks = []
        while True:
            block = os.read(fd, 1 << 20)
            if not block:
                break
            chunks.append(block)
            if max_bytes is not None and sum(map(len, chunks)) > max_bytes:
                raise WorkspaceError(f"{Path(path).name}: larger than {max_bytes} bytes")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _sha_nofollow(root: Path, rel: str) -> str | None:
    """sha256 of root/rel when it is a regular file reached through real directories; None otherwise."""
    try:
        _check_parents(root, rel)
        st = os.lstat(Path(root) / rel)
    except (FileNotFoundError, NotADirectoryError, WorkspaceError):
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    try:
        return util.sha256_bytes(_read_nofollow(Path(root) / rel))
    except (OSError, WorkspaceError):
        return None


def _make_parents(root: Path, rel: str, *, replace_links: bool) -> Path:
    """Create the directories of rel under root; a symlink on the way is removed (never followed) when replace_links."""
    node = Path(root)
    for part in rel.split("/")[:-1]:
        node = node / part
        try:
            st = os.lstat(node)
        except FileNotFoundError:
            os.mkdir(node, 0o755)
            continue
        if stat.S_ISLNK(st.st_mode):
            if not replace_links:
                raise WorkspaceError(f"symlink on the path to {rel}")
            os.unlink(node)                              # removes the link itself; its target is untouched
            os.mkdir(node, 0o755)
        elif not stat.S_ISDIR(st.st_mode):
            raise WorkspaceError(f"not a directory on the path to {rel}")
    return node


def _write_into(root: Path, rel: str, data: bytes, *, replace_links: bool = True) -> None:
    """Write data to root/rel through a temp file and os.replace (which replaces a symlink, never writes through it)."""
    parent = _make_parents(root, rel, replace_links=replace_links)
    dest = parent / rel.split("/")[-1]
    try:
        if stat.S_ISDIR(os.lstat(dest).st_mode):
            raise WorkspaceError(f"a directory stands where {rel} belongs")
    except FileNotFoundError:
        pass
    tmp = parent / f".{dest.name}.mhf-tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------- pinned source and the working copy

def source_digest(example_dir: Path) -> tuple[str, dict[str, str]]:
    """(tree_digest, {rel: sha256}) over PINNED_FILES; WorkspaceError on a missing file or a symlink."""
    root = Path(example_dir)
    hashes: dict[str, str] = {}
    for rel in PINNED_FILES:
        try:
            _check_parents(root, rel)
            st = os.lstat(root / rel)
        except (FileNotFoundError, NotADirectoryError):
            raise WorkspaceError(f"pinned file missing: {rel}") from None
        if stat.S_ISLNK(st.st_mode):
            raise WorkspaceError(f"pinned file is a symlink: {rel}")
        if not stat.S_ISREG(st.st_mode):
            raise WorkspaceError(f"pinned file is not a regular file: {rel}")
        hashes[rel] = util.sha256_bytes(_read_nofollow(root / rel))
    return util.tree_digest(hashes), hashes


def _pristine(paths: StatePaths) -> dict:
    try:
        doc = util.read_json(paths.pristine)
    except FileNotFoundError:
        raise WorkspaceError("workspace has no pristine.json: run ensure_workspace first") from None
    files = doc.get("files") if isinstance(doc, dict) else None
    if (not isinstance(doc, dict) or doc.get("schema") != "mhforge.pristine/1" or not isinstance(files, dict)
            or set(files) != _PINNED_SET or not all(isinstance(v, str) for v in files.values())):
        raise WorkspaceError("pristine.json is not a mhforge.pristine/1 record of the pinned file set")
    if util.tree_digest(files) != doc.get("tree_sha256"):    # the per-file hashes must be the ones the tree pin covers
        raise WorkspaceError("pristine.json file hashes do not match its tree_sha256")
    return doc


def verify_pinned(paths: StatePaths) -> dict[str, list[str]]:
    """{"modified": [...], "removed": [...]} of the pinned files in WS against pristine.json."""
    files = _pristine(paths)["files"]
    modified, removed = [], []
    for rel in PINNED_FILES:
        target = paths.ws / rel
        try:
            _check_parents(paths.ws, rel)
            present = True
            os.lstat(target)
        except (FileNotFoundError, NotADirectoryError):
            present = False
        except WorkspaceError:
            present = True                               # reached through a symlink or a file: not the pinned file
        if not present:
            removed.append(rel)
        elif _sha_nofollow(paths.ws, rel) != files[rel]:
            modified.append(rel)
    return {"modified": modified, "removed": removed}


def restore_pinned(paths: StatePaths, example_dir: Path, changed: Mapping[str, list[str]]) -> list[str]:
    """Copy the named pinned files back from EX (hash-checked against pristine.json). Returns what was restored."""
    files = _pristine(paths)["files"]
    wanted = sorted(set(changed.get("modified", [])) | set(changed.get("removed", [])))
    for rel in wanted:
        if rel not in files:
            raise WorkspaceError(f"not a pinned file, refused to restore: {str(rel)[:80]}")
    restored = []
    for rel in wanted:
        try:
            _check_parents(Path(example_dir), rel)
            data = _read_nofollow(Path(example_dir) / rel)
        except (FileNotFoundError, NotADirectoryError):
            raise WorkspaceError(f"example file missing: {rel}") from None
        if util.sha256_bytes(data) != files[rel]:
            raise WorkspaceError(f"example file differs from the pinned hash: {rel}")
        if _sha_nofollow(paths.ws, rel) == files[rel]:
            continue
        _write_into(paths.ws, rel, data, replace_links=True)
        restored.append(rel)
    return restored


def ensure_workspace(manifest: dict, paths: StatePaths, example_dir: Path, lock_file: Path | None) -> Path:
    """Create (first call) or repair (later calls) WS, the hash-verified copy of PINNED_FILES. Returns WS."""
    expected = util.lookup(manifest, "upstream.tree_sha256", None)
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise WorkspaceError("upstream.tree_sha256 is not set: pin the upstream tree first")
    example_dir = Path(example_dir)
    ws = paths.ws
    if paths.pristine.exists():
        doc = _pristine(paths)
        if doc.get("tree_sha256") != expected:
            raise WorkspaceError("workspace was copied from another upstream tree than the manifest pins")
        if not ws.is_dir() or ws.is_symlink():
            raise WorkspaceError("workspace directory is missing or not a directory")
        changed = verify_pinned(paths)
        if changed["modified"] or changed["removed"]:
            restore_pinned(paths, example_dir, changed)
    else:
        digest, hashes = source_digest(example_dir)
        if digest != expected:
            raise WorkspaceError("example directory does not match upstream.tree_sha256")
        paths.workspace_root.mkdir(parents=True, exist_ok=True)
        if not os.path.lexists(ws):
            tmp = paths.workspace_root / f".text_classification.tmp-{os.getpid()}"
            if os.path.lexists(tmp):
                shutil.rmtree(tmp)
            tmp.mkdir()
            for rel in PINNED_FILES:
                data = _read_nofollow(example_dir / rel)
                if util.sha256_bytes(data) != hashes[rel]:
                    raise WorkspaceError(f"example file changed while copying: {rel}")
                _write_into(tmp, rel, data, replace_links=False)
            os.rename(tmp, ws)
        elif not ws.is_dir() or ws.is_symlink():
            raise WorkspaceError("workspace path exists and is not a directory")
        else:                                            # a copy without pristine.json (crash before it was written)
            for rel in PINNED_FILES:
                if _sha_nofollow(ws, rel) != hashes[rel]:
                    _write_into(ws, rel, _read_nofollow(example_dir / rel), replace_links=True)
        if source_digest(ws)[0] != expected:
            raise WorkspaceError("workspace copy does not match upstream.tree_sha256")
        util.write_json(paths.pristine, {"schema": "mhforge.pristine/1", "source": os.path.abspath(example_dir),
                                         "tree_sha256": expected, "copied_utc": util.utc_now(), "files": hashes},
                        fsync=True)
    if lock_file is not None and os.path.isfile(lock_file) and not os.path.islink(lock_file):
        data = _read_nofollow(Path(lock_file))
        if _sha_nofollow(ws, "uv.lock") != util.sha256_bytes(data):
            _write_into(ws, "uv.lock", data, replace_links=True)
    return ws


# ---------------------------------------------------------------- drift

def snapshot(paths: StatePaths) -> dict[str, str]:
    """tree_hashes of WS without logs/, .venv, __pycache__ and .empty_plugins (WorkspaceError on a symlink)."""
    return util.tree_hashes(paths.ws)


def _agent_name(rel: str) -> str | None:
    m = _AGENT_PY_RE.fullmatch(rel)
    return m.group(1) if m else None


def classify_drift(before: Mapping[str, str], after: Mapping[str, str]) -> dict:
    out: dict[str, list[str]] = {k: [] for k in ("agents_added", "agents_modified", "upstream_modified",
                                                 "upstream_removed", "other_added")}
    for rel in sorted(set(before) | set(after)):
        if "__pycache__" in rel.split("/"):
            continue
        in_before, in_after = rel in before, rel in after
        changed = in_before and in_after and before[rel] != after[rel]
        if rel in _PINNED_SET:
            if changed:
                out["upstream_modified"].append(rel)
            elif in_before and not in_after:
                out["upstream_removed"].append(rel)
            continue
        if not rel.startswith("agents/"):
            continue
        name = _agent_name(rel)
        if name is not None:
            if in_after and not in_before:
                out["agents_added"].append(rel)
            elif changed:
                out["agents_modified"].append(rel)
        elif in_after and (not in_before or changed):
            out["other_added"].append(rel)
    return out


def unknown_entries(paths: StatePaths) -> list[str]:
    """Top-most files and directories of WS that this build does not expect (3.14), sorted; symlinks never followed."""
    found: list[str] = []

    def walk(directory: Path, prefix: str) -> None:
        with os.scandir(directory) as it:
            items = sorted(it, key=lambda e: e.name)
        for item in items:
            rel = prefix + item.name
            real_dir = item.is_dir(follow_symlinks=False)
            if item.name == "__pycache__" and real_dir:
                continue
            if not prefix and item.name in _ALLOWED_TOP_DIRS:
                if not real_dir:
                    found.append(rel)
                continue
            if not prefix and item.name == "uv.lock":
                if not item.is_file(follow_symlinks=False):
                    found.append(rel)
                continue
            if rel in _PINNED_SET:
                if real_dir:
                    found.append(rel)
                continue
            if rel in _PINNED_DIRS and real_dir:
                walk(Path(item.path), rel + "/")
                continue
            found.append(rel)

    ws = paths.ws
    if not ws.is_dir() or ws.is_symlink():
        raise WorkspaceError("workspace directory is missing or not a directory")
    walk(ws, "")
    return sorted(found)


def _protected(ws: Path, rel: str, st: os.stat_result) -> bool:
    if rel == "agents" or rel.startswith("agents/"):
        return stat.S_ISDIR(st.st_mode) or rel.startswith("agents/")
    if rel in _PINNED_SET and stat.S_ISREG(st.st_mode):
        return True
    if (rel in _PINNED_DIRS or rel == "logs") and stat.S_ISDIR(st.st_mode):
        return True
    return False


def quarantine_entries(paths: StatePaths, entries: Sequence[str], utc: str) -> list[str]:
    """Move the entries to STATE/quarantine/<utc>/workspace/<rel>; returns what was moved. Never follows a symlink and
    refuses (WorkspaceError, before moving anything) anything under agents/, a pinned file, a pinned directory or logs/."""
    if not isinstance(utc, str) or not _UTC_DIR_RE.fullmatch(utc):
        raise WorkspaceError("quarantine stamp is not a safe directory name")
    ws = paths.ws
    plan: list[str] = []
    for rel in entries:
        rel = _rel_ok(rel)
        try:
            _check_parents(ws, rel)
            st = os.lstat(ws / rel)
        except (FileNotFoundError, NotADirectoryError):
            continue
        if _protected(ws, rel, st):
            raise WorkspaceError(f"refused to quarantine a protected workspace entry: {rel}")
        if rel not in plan:
            plan.append(rel)
    moved = []
    base = paths.quarantine_root / utc / "workspace"
    for rel in plan:
        dest = base / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        n = 1
        while os.path.lexists(dest):
            dest = base / f"{rel}.dup{n}"
            n += 1
        os.rename(ws / rel, dest)                        # moves a symlink as a link and a directory as a whole
        moved.append(rel)
    return moved


# ---------------------------------------------------------------- candidates and proposals

def candidate_sha256(paths: StatePaths, name: str) -> str | None:
    """sha256 of WS/agents/<name>.py when the name is valid and the file is a regular file (no symlink); else None."""
    if not isinstance(name, str) or not CANDIDATE_RE.fullmatch(name):
        return None
    return _sha_nofollow(paths.ws, f"agents/{name}.py")


def validate_candidate_file(paths: StatePaths, name: str) -> str | None:
    """None when the candidate may be used; else the reason. Other entries of agents/ never reject a candidate."""
    if not isinstance(name, str) or not CANDIDATE_RE.fullmatch(name):
        return "invalid_name"
    if name in PINNED_AGENTS:
        return "pinned_name"
    agents = paths.ws / "agents"
    try:
        st = os.lstat(agents)
    except FileNotFoundError:
        return "agents_dir_missing"
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return "agents_dir_not_a_directory"
    target = agents / f"{name}.py"
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return "missing"
    if stat.S_ISLNK(st.st_mode):
        return "symlink"
    if not stat.S_ISREG(st.st_mode):
        return "not_a_regular_file"
    if st.st_size > MAX_AGENT_FILE_BYTES:
        return "too_large"
    with os.scandir(agents) as it:
        count = sum(1 for e in it if e.name.endswith(".py"))
    if count > MAX_AGENT_FILES:
        return "too_many_agent_files"
    return None


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise WorkspaceError("pending_eval.json: an object repeats a key")
    return dict(pairs)


def _no_constants(name: str) -> object:
    raise WorkspaceError(f"pending_eval.json: {name} is not JSON")


def read_pending_eval(path: Path, *, max_bytes: int = PENDING_EVAL_MAX_BYTES) -> list[dict]:
    """The candidate list of a pending_eval.json after the shape check of 3.14; WorkspaceError on a bad file."""
    try:
        data = _read_nofollow(Path(path), max_bytes=max_bytes)
    except FileNotFoundError:
        raise WorkspaceError("pending_eval.json is missing") from None
    try:
        doc = json.loads(data.decode("utf-8"), object_pairs_hook=_no_duplicate_keys, parse_constant=_no_constants)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise WorkspaceError("pending_eval.json is not valid JSON") from None
    if not isinstance(doc, dict) or not isinstance(doc.get("candidates"), list):
        raise WorkspaceError("pending_eval.json: expected an object with a list 'candidates'")
    seen: set[str] = set()
    out: list[dict] = []
    for index, cand in enumerate(doc["candidates"]):
        if not isinstance(cand, dict):
            raise WorkspaceError(f"pending_eval.json: candidate {index} is not an object")
        name = cand.get("name")
        if not isinstance(name, str):
            raise WorkspaceError(f"pending_eval.json: candidate {index} has no string name")
        for field in ("hypothesis", "axis", "base_system"):
            if field in cand and not isinstance(cand[field], str):
                raise WorkspaceError(f"pending_eval.json: candidate {index} {field} is not a string")
        if "components" in cand and (not isinstance(cand["components"], list)
                                     or not all(isinstance(c, str) for c in cand["components"])):
            raise WorkspaceError(f"pending_eval.json: candidate {index} components is not a list of strings")
        if name in seen:
            raise WorkspaceError(f"pending_eval.json: candidate name repeated: {name[:60]!r}")
        seen.add(name)
        out.append(cand)
    return out


# ---------------------------------------------------------------- fingerprints and config

def _venv_root(python: Path) -> Path:
    """The path rule of 2.3 (same rule as procenv.venv_root): lexical, never resolved; must hold pyvenv.cfg."""
    root = Path(os.path.abspath(python)).parent.parent
    if not (root / "pyvenv.cfg").is_file():
        raise WorkspaceError("interpreter is not inside a virtual environment (no pyvenv.cfg)")
    return root


def env_fingerprint(python: Path) -> str:
    """sha256 over the sorted *.dist-info directory names under <venv root>/lib/python*/site-packages."""
    root = _venv_root(python)
    names: set[str] = set()
    lib = root / "lib"
    if lib.is_dir():
        for pydir in sorted(lib.glob("python*")):
            site = pydir / "site-packages"
            if site.is_dir() and not site.is_symlink():
                with os.scandir(site) as it:
                    names.update(e.name for e in it if e.name.endswith(".dist-info") and e.is_dir(follow_symlinks=False))
    return util.sha256_bytes("".join(n + "\n" for n in sorted(names)).encode("utf-8"))


def _memory_dirs(home: Path, cwd: Path) -> list[Path]:
    projects = Path(home) / ".claude" / "projects"
    slug = re.sub(r"[^A-Za-z0-9]", "-", os.path.abspath(cwd))
    if not projects.is_dir() or projects.is_symlink():
        return []
    hits = []
    with os.scandir(projects) as it:
        for e in sorted(it, key=lambda x: x.name):
            if not e.is_dir(follow_symlinks=False):
                continue
            # The CLI truncates and hashes a folder name over 200 characters [inf]; match on the kept prefix then.
            if e.name == slug or (len(slug) > 200 and e.name.startswith(slug[:200])):
                mem = Path(e.path) / "memory"
                if mem.is_dir() and not mem.is_symlink():
                    hits.append(mem)
    return hits


def memory_dir_fingerprint(home: Path, cwd: Path) -> str | None:
    """Hash of the names and sizes (never the contents) under the CLI's auto-memory directory for cwd; None when absent."""
    dirs = _memory_dirs(home, cwd)
    if not dirs:
        return None
    rows = []
    for mem in dirs:
        for dirpath, dirnames, filenames in os.walk(mem, followlinks=False):
            dirnames.sort()
            for name in sorted(dirnames + filenames):
                full = Path(dirpath) / name
                st = os.lstat(full)
                kind = "l" if stat.S_ISLNK(st.st_mode) else "d" if stat.S_ISDIR(st.st_mode) else "f"
                size = st.st_size if kind == "f" else 0
                rows.append([mem.parent.name, full.relative_to(mem).as_posix(), kind, size])
    return util.sha256_bytes(util.canonical_json(rows))


def load_config(ws: Path) -> dict:
    """WS/config.yaml through yaml.safe_load (lazy import); MhforgeError naming the two supported interpreters when
    PyYAML is missing."""
    try:
        import yaml                                      # noqa: PLC0415 (lazy on purpose: 3.14)
    except ImportError:
        raise MhforgeError(_YAML_MISSING) from None
    data = _read_nofollow(Path(ws) / "config.yaml", max_bytes=1 << 20)
    doc = yaml.safe_load(data.decode("utf-8"))
    if not isinstance(doc, dict):
        raise WorkspaceError("config.yaml is not a mapping")
    return doc


# ---------------------------------------------------------------- upstream import context

@dataclass
class Upstream:
    meta_harness: ModuleType
    benchmark: ModuleType
    claude_wrapper: ModuleType


_CONTRACT_FUNCS = {
    "meta_harness": {"run_benchmark": ["args"], "propose_claude": ["task_prompt", "iteration", "timeout"],
                     "validate_candidates": ["candidates"], "update_evolution_summary": None, "run_evolve": ["args"],
                     "finalize_run": ["baselines", "datasets", "model_short"], "count_iterations_from_summary": []},
    "benchmark": {n: None for n in ("build_val_runs", "build_test_runs", "_inner_loop_command", "run_dir",
                                    "update_summary", "load_results", "discover_all_memory_systems",
                                    "get_model_short_name")},
    "claude_wrapper": {"run": None, "build_command": None},
}
_CONTRACT_ATTRS = {
    "meta_harness": ("PROPOSER_ALLOWED_TOOLS", "PENDING_EVAL", "LOGS_DIR", "RESULTS_DIR", "FRONTIER_VAL", "EVOLVE_DIR"),
    "benchmark": ("DATASETS", "MODELS", "_CONFIG", "SEEDS"),
    "claude_wrapper": ("subprocess", "_EMPTY_PLUGIN_DIR"),
}


def _under(path: object, root: Path) -> bool:
    try:
        return Path(os.path.abspath(str(path))).is_relative_to(Path(os.path.abspath(root)))
    except (TypeError, ValueError):
        return False


def check_upstream_contract(up: Upstream, ws: Path, pinned: Mapping[str, str]) -> None:
    """The upstream contract of 3.14; UpstreamContractError listing every failure."""
    ws = Path(ws)
    problems: list[str] = []
    for name in UPSTREAM_MODULES:
        mod = getattr(up, name)
        where = getattr(mod, "__file__", None)
        if not where or os.path.abspath(where) != os.path.abspath(ws / f"{name}.py"):
            problems.append(f"{name}.__file__ is not {name}.py in the workspace")
        elif _sha_nofollow(ws, f"{name}.py") != pinned.get(f"{name}.py"):
            problems.append(f"{name}.py does not have the pinned sha256")
        for attr, params in _CONTRACT_FUNCS[name].items():
            fn = getattr(mod, attr, None)
            if not callable(fn):
                problems.append(f"{name}.{attr} is missing or not callable")
                continue
            if params is not None:
                try:
                    got = list(inspect.signature(fn).parameters)
                except (TypeError, ValueError):
                    got = None
                if got != params:
                    problems.append(f"{name}.{attr} does not take ({', '.join(params)})")
        for attr in _CONTRACT_ATTRS[name]:
            if not hasattr(mod, attr):
                problems.append(f"{name}.{attr} is missing")
    mh, bm, cw = up.meta_harness, up.benchmark, up.claude_wrapper
    if hasattr(mh, "EVOLVE_DIR") and os.path.abspath(str(mh.EVOLVE_DIR)) != os.path.abspath(ws):
        problems.append("meta_harness.EVOLVE_DIR is not the workspace")
    for attr in ("LOGS_DIR", "PENDING_EVAL", "RESULTS_DIR", "FRONTIER_VAL"):
        if hasattr(mh, attr) and not _under(getattr(mh, attr), ws):
            problems.append(f"meta_harness.{attr} is outside the workspace")
    if hasattr(bm, "SEEDS") and bm.SEEDS != [42]:
        problems.append("benchmark.SEEDS is not [42]")
    if hasattr(cw, "subprocess") and not isinstance(cw.subprocess, ModuleType):
        problems.append("claude_wrapper.subprocess is not a module")
    if hasattr(cw, "_EMPTY_PLUGIN_DIR") and not _under(cw._EMPTY_PLUGIN_DIR, ws):
        problems.append("claude_wrapper._EMPTY_PLUGIN_DIR is outside the workspace")
    if problems:
        raise UpstreamContractError("upstream contract failed: " + "; ".join(problems))


@contextlib.contextmanager
def load_upstream(paths: StatePaths) -> Iterator[Upstream]:
    """Execute claude_wrapper, benchmark and meta_harness from the checked bytes of WS, registered under their bare names.

    sys.path is never changed and no bytecode cache is read or written; the three modules leave sys.modules on exit.
    """
    pinned = _pristine(paths)["files"]
    ws = paths.ws
    saved = {name: sys.modules.get(name) for name in UPSTREAM_MODULES}
    old_dont_write = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    loaded: dict[str, ModuleType] = {}
    try:
        for name in UPSTREAM_MODULES:
            path = ws / f"{name}.py"
            try:
                _check_parents(ws, f"{name}.py")
                source = _read_nofollow(path)
            except (FileNotFoundError, WorkspaceError) as exc:
                raise UpstreamContractError(f"{name}.py cannot be read from the workspace ({type(exc).__name__})") from None
            if hashlib.sha256(source).hexdigest() != pinned.get(f"{name}.py"):
                raise UpstreamContractError(f"{name}.py in the workspace does not have the pinned sha256")
            code = compile(source, str(path), "exec", dont_inherit=True)
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None:
                raise UpstreamContractError(f"no module spec for {name}.py")
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            loaded[name] = module
            try:
                exec(code, module.__dict__)              # exactly the checked bytes; no loader, no cache
            except ModuleNotFoundError as exc:
                if exc.name == "yaml":
                    raise MhforgeError(_YAML_MISSING) from None
                raise
        up = Upstream(meta_harness=loaded["meta_harness"], benchmark=loaded["benchmark"],
                      claude_wrapper=loaded["claude_wrapper"])
        check_upstream_contract(up, ws, pinned)
        yield up
    finally:
        for name in UPSTREAM_MODULES:
            if saved[name] is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved[name]
        sys.dont_write_bytecode = old_dont_write
