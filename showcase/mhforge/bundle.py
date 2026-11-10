# SPDX-License-Identifier: MIT
"""Job payload tar, job file, argv placeholders, worker command, output collection (spec 3.16).

The three ``python -c`` snippets run inside the executor (a sandbox or a local box) and are standard library only.
The host-side :func:`extract_bundle` executes the very same extraction source, so the checks cannot drift apart.
"""

from __future__ import annotations

import io
import json
import os
import posixpath
import re
import signal
import stat
import tarfile
import time
from pathlib import Path
from typing import Mapping, Sequence

from . import scan, util, wire
from .errors import WorkspaceError
from .wire import JobIdentity

__all__ = [
    "MAX_AGENT_FILE_BYTES", "MAX_AGENT_FILES", "OUTPUT_ALLOW", "RUN_ALLOW", "VOCAB_FILE", "EXTRACT_SNIPPET",
    "PACK_SNIPPET", "WHEELHOUSE_SNIPPET", "build_bundle", "extract_bundle", "worker_command", "build_job_file",
    "build_check_file", "collect_outputs", "kill_group", "result_is_usable",
]

MAX_AGENT_FILE_BYTES = 262_144
MAX_AGENT_FILES = 400
OUTPUT_ALLOW = ("val.json", "test.json", "memory.json", "log.jsonl", "sidecar.jsonl")
RUN_ALLOW = ("stdout.txt", "stderr.txt", "exit.json", "stub.txt", "selfcheck.json")
VOCAB_FILE = "fb374d419588a4632f3f557e76b4b70aebbca790"     # the tokenizer vocabulary file LiteLLM's Harmony encoding needs
BOX_DIRS = ("in", "out", "run", "cache", "home", "tmp")
INSANDBOX_FILES = ("mhf_frames.py", "mhf_relay_stub.py", "mhf_selfcheck.py", "mhf_capture_entry.py")
PKG_FILES = ("__init__.py", "inner_loop.py", "llm.py", "memory_system.py", "config.yaml",
             "data/__init__.py", "data/api.py", "data/constants.py", "data/evaluators.py", "data/loaders.py")
PINNED_AGENT_FILES = ("agents/__init__.py", "agents/no_memory.py", "agents/fewshot_memory.py", "agents/fewshot_all.py")
JOBFILE_SCHEMA = "mhforge.jobfile/1"
COLLECT_SCHEMA = "mhforge.collect/1"
MAX_FILE_BYTES = 268_435_456
MAX_TOTAL_BYTES = 536_870_912
_AGENT_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\.py")
_CACHE_NAME_RE = re.compile(r"[0-9a-f]{64}\.json")
_MAX_AGENT_NAME_CHARS = 100                         # USTAR splits at the last '/': the file name itself has 100 bytes
_INSANDBOX_DIR = Path(__file__).resolve().parent / "insandbox"

# ---------------------------------------------------------------- in-executor snippets (standard library only)

_EXTRACT_CORE = r'''
import os, re, stat, sys, tarfile
_MHF_ALLOWED = (
    re.compile(r"pkg/text_classification/(?:__init__|inner_loop|llm|memory_system)\.py"),
    re.compile(r"pkg/text_classification/config\.yaml"),
    re.compile(r"pkg/text_classification/data/(?:__init__|api|constants|evaluators|loaders)\.py"),
    re.compile(r"pkg/text_classification/data/[a-z0-9_]{1,64}/[A-Za-z0-9_.-]{1,64}\.jsonl"),
    re.compile(r"pkg/text_classification/agents/[A-Za-z_][A-Za-z0-9_]{0,127}\.py"),
    re.compile(r"bin/mhf_(?:frames|relay_stub|selfcheck|capture_entry)\.py"),
)
_MHF_WHEEL_ALLOWED = (re.compile(r"wheels/[A-Za-z0-9_.+-]{1,200}\.whl"), re.compile(r"SHA256SUMS"),
                      re.compile(r"requirements\.lock\.txt"))
_MHF_BOX_DIRS = ("in", "out", "run", "cache", "home", "tmp")
_MHF_MAX_MEMBER = 268435456
_MHF_MAX_TOTAL = 1073741824

def _mhf_check_members(tf, allowed):
    members, seen, total, datadirs = tf.getmembers(), set(), 0, set()
    for m in members:
        name = m.name
        if not isinstance(name, str) or name.startswith("/") or "\\" in name or "\0" in name:
            raise ValueError("bad member name")
        if any(p in ("", ".", "..") for p in name.split("/")):
            raise ValueError("member path is not normalised: %r" % name[:80])
        if not m.isreg():
            raise ValueError("member is not a regular file: %r" % name[:80])
        if not any(p.fullmatch(name) for p in allowed):
            raise ValueError("unexpected member: %r" % name[:80])
        if name in seen:
            raise ValueError("duplicate member: %r" % name[:80])
        seen.add(name)
        if m.size < 0 or m.size > _MHF_MAX_MEMBER:
            raise ValueError("member too large: %r" % name[:80])
        total += m.size
        if total > _MHF_MAX_TOTAL:
            raise ValueError("archive too large")
        parts = name.split("/")
        if parts[:3] == ["pkg", "text_classification", "data"] and len(parts) == 5:
            datadirs.add(parts[3])
    if len(datadirs) > 1:
        raise ValueError("more than one dataset directory")
    return members

def _mhf_dir(root, rel_parts):
    node = root
    for part in rel_parts:
        node = os.path.join(node, part)
        try:
            st = os.lstat(node)
        except FileNotFoundError:
            os.mkdir(node, 0o755)
            continue
        if not stat.S_ISDIR(st.st_mode):
            raise ValueError("not a directory on the extraction path: %r" % part)
    return node

def _mhf_extract(tar_path, dest, allowed):
    dest = os.path.abspath(dest)
    os.makedirs(dest, exist_ok=True)
    if os.path.islink(dest) or not os.path.isdir(dest):
        raise ValueError("extraction target is not a directory")
    try:
        tf = tarfile.open(tar_path, "r:")
    except (OSError, tarfile.TarError) as exc:
        raise ValueError("cannot open archive: %s" % type(exc).__name__)
    with tf:
        try:
            members = _mhf_check_members(tf, allowed)
        except tarfile.TarError as exc:
            raise ValueError("bad archive: %s" % type(exc).__name__)
        for m in members:
            parts = m.name.split("/")
            parent = _mhf_dir(dest, parts[:-1])
            fd = os.open(os.path.join(parent, parts[-1]),
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
            src = tf.extractfile(m)
            left = m.size
            with os.fdopen(fd, "wb") as out:
                while left > 0:
                    block = src.read(min(left, 1 << 20))
                    if not block:
                        raise ValueError("archive member truncated")
                    out.write(block)
                    left -= len(block)
    return [m.name for m in members]

def mhf_extract_bundle(tar_path, box):
    names = _mhf_extract(tar_path, box, _MHF_ALLOWED)
    for d in _MHF_BOX_DIRS:
        _mhf_dir(os.path.abspath(box), [d])
    return names
'''

EXTRACT_SNIPPET: str = _EXTRACT_CORE + r'''
try:
    _n = mhf_extract_bundle(os.path.join(sys.argv[1], "bundle.tar"), sys.argv[1])
except (ValueError, OSError) as _e:
    sys.stderr.write("mhf_extract: refused: %s\n" % _e)
    sys.exit(1)
sys.stdout.write("extracted %d\n" % len(_n))
'''

WHEELHOUSE_SNIPPET: str = _EXTRACT_CORE + r'''
import hashlib, subprocess
def _mhf_wheelhouse(box):
    dest = os.path.join(box, "wheelhouse")
    names = _mhf_extract(os.path.join(box, "wheelhouse.tar"), dest, _MHF_WHEEL_ALLOWED)
    if "SHA256SUMS" not in names or "requirements.lock.txt" not in names:
        raise ValueError("SHA256SUMS or requirements.lock.txt missing")
    with open(os.path.join(dest, "SHA256SUMS"), "rb") as fh:
        sums = fh.read()
    listed = set()
    for line in sums.decode("utf-8").splitlines():
        if not line.strip():
            continue
        m = re.fullmatch(r"[0-9a-f]{64} [ *](wheels/[A-Za-z0-9_.+-]{1,200}\.whl)", line)
        if m is None:
            raise ValueError("bad SHA256SUMS line")
        listed.add(m.group(1))
    wheels = set(n for n in names if n.startswith("wheels/"))
    if listed != wheels or not wheels:
        raise ValueError("SHA256SUMS does not list exactly the wheels in the archive")
    rc = subprocess.call(["sha256sum", "-c", "SHA256SUMS"], cwd=dest, stdout=subprocess.DEVNULL)
    if rc != 0:
        raise ValueError("sha256sum -c failed")
    return hashlib.sha256(sums).hexdigest()
try:
    _h = _mhf_wheelhouse(sys.argv[1])
except (ValueError, OSError) as _e:
    sys.stderr.write("mhf_wheelhouse: refused: %s\n" % _e)
    sys.exit(1)
sys.stdout.write(_h + "\n")
'''

PACK_SNIPPET: str = r'''
import io, json, os, re, stat, sys, tarfile
OUTPUT_ALLOW = %r
RUN_ALLOW = %r
MAX_FILE, MAX_TOTAL = %d, %d
def _mhf_pack(box, with_cache):
    packed, skipped, total = [], [], 0
    names = [("out/" + n) for n in OUTPUT_ALLOW] + [("run/" + n) for n in RUN_ALLOW]
    if with_cache:
        try:
            names += ["cache/" + n for n in sorted(os.listdir(os.path.join(box, "cache")))
                      if re.fullmatch(r"[0-9a-f]{64}\.json", n)]
        except OSError:
            pass
    tmp = os.path.join(box, ".collect.tar.tmp")
    with tarfile.open(tmp, "w", format=tarfile.USTAR_FORMAT) as tf:
        for rel in names:
            path = os.path.join(box, *rel.split("/"))
            try:
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            except OSError:
                if os.path.islink(path):
                    skipped.append({"name": rel, "reason": "link"})
                continue
            with os.fdopen(fd, "rb") as fh:
                st = os.fstat(fh.fileno())
                if not stat.S_ISREG(st.st_mode):
                    skipped.append({"name": rel, "reason": "not_regular"})
                    continue
                if st.st_size > MAX_FILE or total + st.st_size > MAX_TOTAL:
                    skipped.append({"name": rel, "reason": "too_large", "bytes": st.st_size})
                    continue
                data = fh.read(MAX_FILE + 1)
            if len(data) > MAX_FILE or total + len(data) > MAX_TOTAL:
                skipped.append({"name": rel, "reason": "too_large", "bytes": len(data)})
                continue
            info = tarfile.TarInfo(rel)
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            tf.addfile(info, io.BytesIO(data))
            total += len(data)
            packed.append(rel)
    os.replace(tmp, os.path.join(box, "collect.tar"))
    return {"packed": packed, "skipped": skipped}
try:
    _r = _mhf_pack(sys.argv[1], len(sys.argv) > 2 and sys.argv[2] == "cache")
except (OSError, tarfile.TarError) as _e:
    sys.stderr.write("mhf_pack: failed: %%s\n" %% type(_e).__name__)
    sys.exit(1)
sys.stdout.write(json.dumps(_r, sort_keys=True) + "\n")
''' % (OUTPUT_ALLOW, RUN_ALLOW, MAX_FILE_BYTES, MAX_TOTAL_BYTES)


def _extract_namespace() -> dict:
    ns: dict = {"__name__": "mhforge_bundle_extract"}
    exec(compile(_EXTRACT_CORE, "<mhforge.bundle extract>", "exec"), ns)   # our own constant: the in-executor checks
    return ns


def extract_bundle(tar_path: Path, box: Path) -> None:
    """Host-side safe extraction of a job bundle into box, with the checks of EXTRACT_SNIPPET (ValueError on a refusal);
    creates in/ out/ run/ cache/ home/ tmp/."""
    _extract_namespace()["mhf_extract_bundle"](os.fspath(tar_path), os.fspath(box))


# ---------------------------------------------------------------- bundle

def _write_atomic(path: Path, data: bytes, mode: int) -> None:
    """Temp file in the same directory, then os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise


def _read_regular(path: Path, *, max_bytes: int | None = None) -> bytes:
    """Bytes of a regular file opened without following a symlink; WorkspaceError otherwise."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        raise WorkspaceError(f"file missing: {Path(path).name}") from None
    except OSError as exc:
        raise WorkspaceError(f"cannot open {Path(path).name}: {type(exc).__name__}") from None
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise WorkspaceError(f"not a regular file: {Path(path).name}")
        data = fh.read() if max_bytes is None else fh.read(max_bytes + 1)
    if max_bytes is not None and len(data) > max_bytes:
        raise WorkspaceError(f"{Path(path).name}: larger than {max_bytes} bytes")
    return data


def _pristine_files(ws: Path) -> dict[str, str]:
    path = Path(ws).parent / "pristine.json"
    try:
        doc = util.read_json(path)
    except FileNotFoundError:
        raise WorkspaceError("workspace has no pristine.json: run ensure_workspace first") from None
    files = doc.get("files") if isinstance(doc, dict) else None
    if not isinstance(doc, dict) or doc.get("schema") != "mhforge.pristine/1" or not isinstance(files, dict):
        raise WorkspaceError("pristine.json is not a mhforge.pristine/1 record")
    if util.tree_digest(files) != doc.get("tree_sha256"):
        raise WorkspaceError("pristine.json file hashes do not match its tree_sha256")
    return files


def _workspace_agents(ws: Path, skipped: list[dict]) -> dict[str, Path]:
    """Non-pinned agents/*.py of WS: regular files with a module name, within MAX_AGENT_FILE_BYTES; others skipped."""
    agents_dir = Path(ws) / "agents"
    if os.path.islink(agents_dir) or not agents_dir.is_dir():
        raise WorkspaceError("workspace agents/ is missing or not a directory")
    out: dict[str, Path] = {}
    pinned = {p.split("/")[1] for p in PINNED_AGENT_FILES}
    for entry in sorted(os.scandir(agents_dir), key=lambda e: e.name):
        name = entry.name
        if name in pinned:
            continue
        reason = None
        if name == "__pycache__":
            reason = "pycache"
        elif entry.is_symlink():
            reason = "symlink"
        elif not entry.is_file(follow_symlinks=False):
            reason = "not_regular"
        elif not _AGENT_NAME_RE.fullmatch(name):
            reason = "not_a_module_file"
        elif len(name) > _MAX_AGENT_NAME_CHARS:
            reason = "name_too_long"               # agents/<name> must fit the USTAR name field of the bundle
        elif entry.stat(follow_symlinks=False).st_size > MAX_AGENT_FILE_BYTES:
            reason = "too_large"
        if reason is not None:
            skipped.append({"name": "agents/" + name, "reason": reason})
            continue
        out[name[:-3]] = Path(entry.path)
    return out


def build_bundle(example_dir: Path, ws: Path, dataset: str, manifest: dict, dest: Path, *,
                 agent_overrides: Mapping[str, Path] | None = None) -> dict:
    """Write the deterministic job tar to dest; returns {"sha256", "bytes", "members": {path: sha256}, "overridden": [...],
    "skipped": [{"name", "reason"}]}. Pinned files come from example_dir, hash-checked against WS/../pristine.json."""
    dirs = util.lookup(manifest, "upstream.expect.dataset_dirs")
    if not isinstance(dataset, str) or dataset not in dirs:
        raise ValueError(f"unknown dataset: {str(dataset)[:60]!r}")
    files = _pristine_files(ws)
    data_prefix = f"data/{dirs[dataset]}/"
    data_files = sorted(r for r in files if r.startswith(data_prefix) and r.endswith(".jsonl") and r.count("/") == 2)
    if not data_files:
        raise WorkspaceError(f"no pinned data files under {data_prefix}")
    contents: dict[str, bytes] = {}
    for rel in PKG_FILES + PINNED_AGENT_FILES + tuple(data_files):
        if rel not in files:
            raise WorkspaceError(f"not a pinned file: {rel}")
        data = _read_regular(Path(example_dir) / rel)
        if util.sha256_bytes(data) != files[rel]:
            raise WorkspaceError(f"example file differs from the pinned hash: {rel}")
        contents["pkg/text_classification/" + rel] = data
    skipped: list[dict] = []
    agents = _workspace_agents(ws, skipped)
    overrides = dict(agent_overrides or {})
    for name in overrides:
        if not isinstance(name, str) or not wire.CANDIDATE_RE.fullmatch(name) or f"agents/{name}.py" in PINNED_AGENT_FILES:
            raise ValueError(f"agent override is not a candidate name: {str(name)[:60]!r}")
    if len(set(agents) | set(overrides)) + len(PINNED_AGENT_FILES) > MAX_AGENT_FILES:
        raise WorkspaceError(f"more than {MAX_AGENT_FILES} agent files")
    for name in sorted(set(agents) | set(overrides)):
        if name in overrides:
            data = _read_regular(Path(overrides[name]), max_bytes=MAX_AGENT_FILE_BYTES)
        else:
            data = _read_regular(agents[name], max_bytes=MAX_AGENT_FILE_BYTES)
        contents[f"pkg/text_classification/agents/{name}.py"] = data
    for fname in INSANDBOX_FILES:
        contents["bin/" + fname] = _read_regular(_INSANDBOX_DIR / fname)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tf:
        for name in sorted(contents):
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(contents[name]), 0o644, 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tf.addfile(info, io.BytesIO(contents[name]))
    blob = buf.getvalue()
    _write_atomic(Path(dest), blob, 0o644)
    return {"sha256": util.sha256_bytes(blob), "bytes": len(blob),
            "members": {name: util.sha256_bytes(contents[name]) for name in sorted(contents)},
            "overridden": sorted(overrides), "skipped": skipped}


# ---------------------------------------------------------------- worker command and job files

def _substitute(arg: str, box: str) -> str:
    return str(arg).replace("{out}", posixpath.join(box, "out")).replace("{in}", posixpath.join(box, "in"))


def worker_command(python: str, box: str, inner_argv: Sequence[str], *, capture: bool, max_text_chars: int) -> list[str]:
    """The worker's argv: the capture entry (with its sidecar) or python -m text_classification.inner_loop; {out} and {in}
    in inner_argv become <box>/out and <box>/in."""
    box = str(box)
    argv = [_substitute(a, box) for a in inner_argv]
    if capture:
        if isinstance(max_text_chars, bool) or not isinstance(max_text_chars, int) or max_text_chars < 1:
            raise ValueError("max_text_chars must be a positive integer")
        return [str(python), posixpath.join(box, "bin", "mhf_capture_entry.py"),
                "--sidecar", posixpath.join(box, "out", "sidecar.jsonl"), "--max-text-chars", str(max_text_chars),
                "--", *argv]
    return [str(python), "-m", "text_classification.inner_loop", *argv]


def _checked_env(env: Mapping[str, str]) -> dict[str, str]:
    out = {}
    for name, value in env.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise ValueError("job file env must map strings to strings")
        if re.search(wire.CREDENTIAL_NAME_PATTERN, name.upper()):
            raise ValueError(f"job file env holds the credential-shaped variable {name}")   # never a key or token
        out[name] = value
    return dict(sorted(out.items()))


def _no_secret(doc: dict) -> dict:
    findings = scan.scan_obj(doc)
    if findings:
        raise ValueError(f"job file would hold a secret ({findings[0].pattern} at {findings[0].path})")
    return doc


def _common(job_id: str, *, argv: list[str], box: str, env: Mapping[str, str], route: int, timeout_s: int | float,
            expect_outputs: Sequence[str], manifest: dict, vocab_file: str | None, expect_freeze_sha256: str | None,
            repeat: int | None, sample: int | None) -> dict:
    if route not in (1, 2) or isinstance(route, bool):
        raise ValueError("route must be 1 or 2")
    if vocab_file is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", vocab_file):
        raise ValueError("vocab_file must be a plain file name")
    if expect_freeze_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", expect_freeze_sha256):
        raise ValueError("expect_freeze_sha256 must be 64 lower-case hex")
    endpoint = util.lookup(manifest, "sandbox.object_store_endpoint", None)
    box = str(box)
    doc = {
        "schema": JOBFILE_SCHEMA, "job_id": job_id, "argv": argv, "env": _checked_env(env),
        "cwd": posixpath.join(box, "pkg"), "route": route, "port": 0, "timeout_s": timeout_s,
        "reply_timeout_s": util.lookup(manifest, "relay.reply_timeout_s"),
        "ping_interval_s": util.lookup(manifest, "relay.ping_interval_s"),
        "pong_timeout_s": util.lookup(manifest, "relay.pong_timeout_s"),
        "max_request_bytes": util.lookup(manifest, "gateway.max_request_bytes"),
        "run_dir": posixpath.join(box, "run"),
        "expect_outputs": [posixpath.join(box, "out", posixpath.basename(str(n))) for n in expect_outputs],
        "vocab_file": vocab_file, "repeat": repeat, "sample": sample,
        "probe_endpoints": [endpoint] if isinstance(endpoint, str) and endpoint else [],
        "expect": {"machine": util.lookup(manifest, "sandbox.expect.machine"),
                   "python": util.lookup(manifest, "sandbox.expect.python"),
                   "freeze_sha256": expect_freeze_sha256},
    }
    return _no_secret(doc)


def build_job_file(job: JobIdentity, *, python: str, box: str, inner_argv: Sequence[str], env: Mapping[str, str],
                   route: int, timeout_s: int, expect_outputs: Sequence[str], manifest: dict,
                   vocab_file: str | None = VOCAB_FILE, expect_freeze_sha256: str | None = None,
                   capture: bool | None = None) -> dict:
    """The job file of 2.6. capture=None takes capture.enabled from the manifest. ValueError when env holds a
    credential-shaped name or the file would carry anything the secret scanner flags."""
    if capture is None:
        capture = bool(util.lookup(manifest, "capture.enabled"))
    argv = worker_command(python, box, inner_argv, capture=capture,
                          max_text_chars=util.lookup(manifest, "capture.max_text_chars"))
    return _common(job.job_id, argv=argv, box=box, env=env, route=route, timeout_s=timeout_s,
                   expect_outputs=expect_outputs, manifest=manifest, vocab_file=vocab_file,
                   expect_freeze_sha256=expect_freeze_sha256, repeat=job.repeat, sample=job.sample)


def build_check_file(*, python: str, box: str, env: Mapping[str, str], manifest: dict,
                     vocab_file: str | None = VOCAB_FILE, expect_freeze_sha256: str | None = None) -> dict:
    """The job file of an import check: job_id "import_check", empty argv and expect_outputs, the worker's env and cwd.
    The stub is never started with it (it refuses an empty argv)."""
    return _common("import_check", argv=[], box=box, env=env, route=util.lookup(manifest, "routing.route"),
                   timeout_s=util.lookup(manifest, "executor.import_check_timeout_s"), expect_outputs=(),
                   manifest=manifest, vocab_file=vocab_file, expect_freeze_sha256=expect_freeze_sha256,
                   repeat=None, sample=None)


# ---------------------------------------------------------------- output collection

def _allowed_collect_name(name: str) -> str | None:
    """'out', 'run' or 'cache' for an allow-listed name; None otherwise."""
    parts = name.split("/")
    if len(parts) != 2:
        return None
    kind, base = parts
    if kind == "out" and base in OUTPUT_ALLOW or kind == "run" and base in RUN_ALLOW:
        return kind
    if kind == "cache" and _CACHE_NAME_RE.fullmatch(base):
        return kind
    return None


def _dir_sources(box: Path, skipped: list[dict]) -> list[tuple[str, object]]:
    real = set()
    for kind in ("out", "run", "cache"):            # a linked subdirectory is never followed
        try:
            st = os.lstat(box / kind)
        except OSError:
            continue
        if stat.S_ISDIR(st.st_mode):
            real.add(kind)
        else:
            skipped.append({"name": kind, "reason": "link" if stat.S_ISLNK(st.st_mode) else "not_regular"})
    names = ([f"out/{n}" for n in OUTPUT_ALLOW] if "out" in real else []) + \
            ([f"run/{n}" for n in RUN_ALLOW] if "run" in real else [])
    if "cache" in real:
        try:
            names += [f"cache/{n}" for n in sorted(os.listdir(box / "cache")) if _CACHE_NAME_RE.fullmatch(n)]
        except OSError:
            pass
    return [(n, box / n) for n in names]


def collect_outputs(source: Path | bytes, attempt_dir: Path, *, max_file_bytes: int = MAX_FILE_BYTES,
                    max_total_bytes: int = MAX_TOTAL_BYTES) -> dict[str, str]:
    """Copy OUTPUT_ALLOW names into attempt_dir/out, RUN_ALLOW names into attempt_dir/run and cache/<64 hex>.json into
    attempt_dir/cache, from a box directory or from collect.tar bytes. Rejects absolute names, '..', links and devices;
    skips oversize files; re-hashes on the host. Writes attempt_dir/collect.json (copied and skipped names).
    Returns name -> sha256 of the files now under attempt_dir/out."""
    attempt_dir = Path(attempt_dir)
    copied: dict[str, str] = {}
    skipped: list[dict] = []
    total = 0

    def store(name: str, data: bytes) -> None:
        nonlocal total
        if len(data) > max_file_bytes or total + len(data) > max_total_bytes:
            skipped.append({"name": name, "reason": "too_large", "bytes": len(data)})
            return
        target = attempt_dir / name
        _write_atomic(target, data, 0o600)
        copied[name] = util.sha256_file(target)                  # re-hashed from what is now on the host
        total += len(data)

    if isinstance(source, (bytes, bytearray, memoryview)):
        try:
            tf = tarfile.open(fileobj=io.BytesIO(bytes(source)), mode="r:")
        except tarfile.TarError:
            raise ValueError("collected archive is not a tar") from None
        with tf:
            for m in tf.getmembers():
                name = m.name
                if name.startswith("/") or ".." in name.split("/") or "\\" in name:
                    skipped.append({"name": name[:120], "reason": "unsafe_name"})
                elif not m.isreg():
                    skipped.append({"name": name[:120], "reason": "not_regular"})
                elif _allowed_collect_name(name) is None:
                    skipped.append({"name": name[:120], "reason": "not_allowed"})
                elif name in copied:
                    skipped.append({"name": name, "reason": "duplicate"})
                elif m.size > max_file_bytes or total + m.size > max_total_bytes:
                    skipped.append({"name": name, "reason": "too_large", "bytes": m.size})
                else:
                    fh = tf.extractfile(m)
                    store(name, fh.read(max_file_bytes + 1) if fh is not None else b"")
    else:
        for name, path in _dir_sources(Path(source), skipped):
            try:
                st = os.lstat(path)
            except (FileNotFoundError, NotADirectoryError):
                continue
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                skipped.append({"name": name, "reason": "link" if stat.S_ISLNK(st.st_mode) else "not_regular"})
                continue
            if st.st_size > max_file_bytes or total + st.st_size > max_total_bytes:
                skipped.append({"name": name, "reason": "too_large", "bytes": st.st_size})
                continue
            try:
                data = _read_regular(path, max_bytes=max_file_bytes)
            except WorkspaceError:
                skipped.append({"name": name, "reason": "unreadable"})
                continue
            store(name, data)
    util.write_json(attempt_dir / "collect.json", {"schema": COLLECT_SCHEMA, "copied": dict(sorted(copied.items())),
                                                   "skipped": skipped})
    return {n.split("/", 1)[1]: h for n, h in sorted(copied.items()) if n.startswith("out/")}


# ---------------------------------------------------------------- process groups and results

def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:          # macOS answers EPERM for a group whose only members are zombies
        return False
    return True


def kill_group(pid: int, *, term_wait_s: float = 10.0) -> None:
    """SIGTERM to the process group of pid (a session leader started with start_new_session=True), wait up to
    term_wait_s for it to vanish, then SIGKILL. Never raises for a group that is already gone."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        raise ValueError("kill_group needs a process group id above 1")
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + max(0.0, float(term_wait_s))
    while time.monotonic() < deadline:
        if not _group_alive(pid):
            return
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def result_is_usable(path: Path) -> bool:
    """benchmark._has_usable_result (L171-L176): a JSON object whose "accuracy" is an int or float (bool included, as
    upstream). Unlike upstream, undecodable bytes give False instead of raising."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):   # a hostile, deeply nested file must not crash the runner
        return False
    return isinstance(data, dict) and isinstance(data.get("accuracy"), (int, float))
