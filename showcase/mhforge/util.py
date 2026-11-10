# SPDX-License-Identifier: Apache-2.0
# Adapted from ash0ts/harness2-wandb@31b0f82 harness2/showcase/gateway.py (write_record), protocol.py (tree_hashes,
# changed_files), budget.py (micros). Changes: atomic temp-file write with optional fsync; tree hashes with posix keys,
# include patterns, excluded directories and WorkspaceError on symlinks; changed_files split into added/removed/modified;
# micros typed and float-free; plus new helpers (clock, ids, UTC, canonical JSON, JSONL, SQLite read-only open,
# loopback guard, environment scrub, key digests).
"""Shared helpers (spec 3.2). Standard library only."""

from __future__ import annotations

import base64
import contextlib
import ctypes
import datetime as _dt
import fcntl
import fnmatch
import hashlib
import ipaddress
import json
import os
import random
import re
import secrets
import socket
import sqlite3
import tempfile
import threading
import time
from decimal import ROUND_CEILING, Decimal, InvalidOperation, localcontext
from pathlib import Path
from typing import Iterator, Mapping, Protocol, Sequence

from .errors import StateError, WorkspaceError

__all__ = [
    "Clock", "SystemClock", "utc_iso", "utc_now", "parse_utc", "IdSource", "canonical_json", "sha256_bytes", "sha256_file",
    "write_json", "read_json", "append_jsonl", "read_jsonl", "tree_hashes", "tree_digest", "changed_files", "micros", "usd",
    "lookup", "safe_component", "open_sqlite_ro", "sleep_gap", "loopback_guard", "NonLoopbackConnect", "scrub_process_env",
    "append_key_digest", "JOURNAL_TORN_MARKER", "parse_marked_jsonl", "read_marked_jsonl",
]


# ---------------------------------------------------------------- time

class Clock(Protocol):
    def now(self) -> float: ...                    # seconds since the Unix epoch (UTC)
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)
_UTC_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?Z")


def utc_iso(epoch: float) -> str:
    """'2026-11-10T01:02:03.000001Z' for seconds since the epoch; always six fractional digits."""
    moment = _EPOCH + _dt.timedelta(microseconds=round(epoch * 1_000_000))
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def utc_now(clock: Clock | None = None) -> str:
    return utc_iso((clock or SystemClock()).now())


def parse_utc(text: str) -> float:
    """Inverse of utc_iso. ValueError unless the text is a UTC timestamp ending with 'Z'."""
    if not isinstance(text, str):
        raise ValueError("timestamp must be a string")
    m = _UTC_RE.fullmatch(text)
    if not m:
        raise ValueError(f"not a UTC timestamp ending with Z: {text[:40]!r}")
    y, mo, d, h, mi, s, frac = m.groups()
    us = int((frac or "0").ljust(6, "0"))
    moment = _dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(s), us, tzinfo=_dt.timezone.utc)
    return (moment - _EPOCH).total_seconds()


def _compact_stamp(epoch: float) -> str:
    return (_EPOCH + _dt.timedelta(seconds=int(epoch // 1))).strftime("%Y%m%dT%H%M%S")


class IdSource:
    """call ids, ledger ids and job tokens. A seeded rng makes them deterministic."""

    def __init__(self, clock: Clock | None = None, rng: random.Random | None = None) -> None:
        self.clock = clock or SystemClock()
        self.rng = rng
        self._lock = threading.Lock()

    def _hex12(self) -> str:
        if self.rng is None:
            return secrets.token_hex(6)
        with self._lock:
            return "%012x" % self.rng.getrandbits(48)

    def call_id(self) -> str:
        return f"c-{_compact_stamp(self.clock.now())}-{self._hex12()}"

    def ledger_id(self) -> str:
        return f"l-{_compact_stamp(self.clock.now())}-{self._hex12()}"

    def token(self) -> str:
        if self.rng is None:
            return "mhfjob_" + secrets.token_urlsafe(32)
        with self._lock:
            raw = bytes(self.rng.getrandbits(8) for _ in range(32))
        return "mhfjob_" + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------- JSON and hashes

def canonical_json(value: object) -> bytes:
    """2.1: sorted keys, compact, Unicode kept, ValueError on NaN or Infinity."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _fsync_dir(directory: Path) -> None:
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_bytes(path: Path, data: bytes, *, fsync: bool, mode: int = 0o600) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    if fsync:
        _fsync_dir(path.parent)


def write_json(path: Path, value: object, *, fsync: bool = False) -> None:
    """Temp file in the same directory, then os.replace: never a partial file at `path`. Mode 0600."""
    data = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")   # serialise before touching disk
    _atomic_write_bytes(Path(path), data, fsync=fsync)


def read_json(path: Path, *, max_bytes: int = 64 * 1024 * 1024) -> object:
    """StateError over max_bytes or on bad JSON; FileNotFoundError when absent."""
    with open(path, "rb") as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise StateError(f"{Path(path).name}: larger than {max_bytes} bytes")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise StateError(f"{Path(path).name}: not valid JSON ({type(exc).__name__})") from None


def append_jsonl(path: Path, row: dict, *, fsync: bool = False) -> None:
    """One os.write of one whole line on an O_APPEND descriptor."""
    if not isinstance(row, dict):
        raise TypeError("a JSONL row must be a dict")
    data = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        written = os.write(fd, data)
        if written != len(data):
            raise StateError(f"{path.name}: short write ({written} of {len(data)} bytes)")
        if fsync:
            os.fsync(fd)
    finally:
        os.close(fd)


def read_jsonl(path: Path) -> list[dict]:
    """Rows of a JSONL file ([] when absent). A torn last line is ignored; a bad line elsewhere is StateError."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return []
    lines = data.split(b"\n")
    tail = lines.pop()                              # b"" when the file ends with a newline
    rows: list[dict] = []
    for number, line in enumerate(lines, 1):
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise StateError(f"{Path(path).name}: bad JSONL line {number}") from None
        if not isinstance(row, dict):
            raise StateError(f"{Path(path).name}: line {number} is not an object")
        rows.append(row)
    if tail:
        try:
            row = json.loads(tail.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            row = None                              # torn last line
        if isinstance(row, dict):
            rows.append(row)
    return rows


# (op, key, status) of the row the journal writer appends after a torn last line; detail names the torn bytes (2.7)
JOURNAL_TORN_MARKER = ("journal", "torn_tail", "terminated")


def _marker_names(row: dict, bad: bytes) -> bool:
    if (row.get("op"), row.get("key"), row.get("status")) != JOURNAL_TORN_MARKER:
        return False
    detail = row.get("detail")
    return (isinstance(detail, dict) and detail.get("bytes") == len(bad)
            and detail.get("sha256") == hashlib.sha256(bad).hexdigest())


def parse_marked_jsonl(data: bytes, check=None, *, name: str = "journal") -> tuple[list[dict], bytes | None]:
    """(rows, torn region bytes or None) of journal-style JSONL that is never truncated (2.7).

    A line that does not parse as an object is bad. The run of bad lines after the last good row is the torn region and
    is ignored (a crash can also tear the write that terminates an earlier torn tail, leaving two bad lines or a bare
    newline). A run of bad lines elsewhere must be followed by the JOURNAL_TORN_MARKER row whose detail names exactly
    those bytes ({"bytes", "sha256"}), else StateError. `check(row, line_number)` may validate (or raise on) each good
    row and returns the row to keep."""
    lines = data.split(b"\n")
    if lines[-1] == b"":
        lines.pop()                                          # the data ends with a newline (or is empty)
    rows: list[dict] = []
    run: list[bytes] = []
    for index, line in enumerate(lines):
        try:
            row = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            row = None
        if not isinstance(row, dict):                        # a torn write never parses (a line ends with its "}")
            run.append(line)
            continue
        if check is not None:
            row = check(row, index + 1)
        if run:
            if not _marker_names(row, b"\n".join(run)):
                raise StateError(f"{name}: bad line {index + 1 - len(run)}")
            run = []
        rows.append(row)
    return rows, (b"\n".join(run) if run else None)


def read_marked_jsonl(path: Path) -> list[dict]:
    """Rows of a journal-style JSONL file ([] when absent), by parse_marked_jsonl: a torn tail is ignored, a bad line
    terminated by its torn-tail marker is skipped, any other bad line is StateError. No schema check."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return []
    return parse_marked_jsonl(data, name=Path(path).name)[0]


# ---------------------------------------------------------------- trees

def tree_hashes(root: Path, *, include: Sequence[str] | None = None,
                exclude_dirs: frozenset[str] = frozenset({".venv", "__pycache__", "logs", ".empty_plugins"})) -> dict[str, str]:
    """Relative posix path -> sha256 of every regular file under root.

    Directories whose name is in exclude_dirs are skipped at any depth without being looked into. `include` holds fnmatch
    patterns matched against the relative posix path. Any symlink outside an excluded directory raises WorkspaceError.
    Other non-regular entries (sockets, fifos) are skipped.
    """
    root = Path(root)
    if root.is_symlink():
        raise WorkspaceError(f"tree root is a symlink: {root.name}")
    def _walk_error(exc: OSError) -> None:         # an unreadable directory must not drop out of the hash silently
        if not (isinstance(exc, FileNotFoundError) and os.path.abspath(exc.filename or "") == os.path.abspath(root)):
            raise WorkspaceError(f"cannot read tree entry ({type(exc).__name__})")   # a missing root stays an empty tree
    result: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root, onerror=_walk_error, followlinks=False):
        base = Path(dirpath)
        keep = []
        for name in sorted(dirnames):
            if (base / name).is_symlink():          # checked before the exclusion: a symlink named "logs" is still refused
                raise WorkspaceError(f"symlink in tree: {(base / name).relative_to(root).as_posix()}")
            if name in exclude_dirs:
                continue
            keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            full = base / name
            rel = full.relative_to(root).as_posix()
            if full.is_symlink():
                raise WorkspaceError(f"symlink in tree: {rel}")
            if not full.is_file():
                continue
            if include is not None and not any(fnmatch.fnmatchcase(rel, pat) for pat in include):
                continue
            result[rel] = sha256_file(full)
    return dict(sorted(result.items()))


def tree_digest(hashes: Mapping[str, str]) -> str:
    """sha256 over the sorted lines 'path\\0sha256\\n'."""
    h = hashlib.sha256()
    for rel in sorted(hashes):
        h.update(f"{rel}\0{hashes[rel]}\n".encode("utf-8"))
    return h.hexdigest()


def changed_files(before: Mapping[str, str], after: Mapping[str, str]) -> dict[str, list[str]]:
    return {
        "added": sorted(k for k in after.keys() - before.keys()),
        "removed": sorted(k for k in before.keys() - after.keys()),
        "modified": sorted(k for k in before.keys() & after.keys() if before[k] != after[k]),
    }


# ---------------------------------------------------------------- money and lookups

def micros(value: str | int | Decimal) -> int:
    """USD -> integer micro-dollars, rounded up (ROUND_CEILING). Floats are refused: no float arithmetic on money."""
    if isinstance(value, bool) or isinstance(value, float) or not isinstance(value, (str, int, Decimal)):
        raise TypeError(f"micros takes a decimal string, int or Decimal, not {type(value).__name__}")
    try:
        amount = Decimal(value.strip()) if isinstance(value, str) else Decimal(value)
    except InvalidOperation:
        raise ValueError(f"not a decimal amount: {value!r}") from None
    if not amount.is_finite():
        raise ValueError(f"not a finite amount: {value!r}")
    with localcontext(prec=max(28, len(amount.as_tuple().digits) + 8)):    # exact product, rounded only by the ceiling
        return int((amount * 1_000_000).to_integral_value(rounding=ROUND_CEILING))


def usd(micro: int) -> str:
    """Integer micro-dollars -> '0.006480'."""
    if isinstance(micro, bool) or not isinstance(micro, int):
        raise TypeError("usd takes integer micro-dollars")
    sign = "-" if micro < 0 else ""
    whole, frac = divmod(abs(micro), 1_000_000)
    return f"{sign}{whole}.{frac:06d}"


_NO_DEFAULT = object()


def lookup(mapping: Mapping, dotted: str, default: object = _NO_DEFAULT) -> object:
    """mapping['a']['b']['c'] for 'a.b.c'; KeyError when absent and no default is given."""
    node: object = mapping
    for part in dotted.split("."):
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        else:
            if default is _NO_DEFAULT:
                raise KeyError(dotted)
            return default
    return node


_SAFE_COMPONENT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def safe_component(text: str) -> str:
    if not isinstance(text, str) or not _SAFE_COMPONENT_RE.fullmatch(text):
        raise ValueError(f"not a safe path component: {str(text)[:60]!r}")
    return text


# ---------------------------------------------------------------- SQLite read-only open

def open_sqlite_ro(path: Path) -> tuple[sqlite3.Connection, bool]:
    """The read-only open of 2.10: (connection, possibly_stale). Never creates -wal or -shm; works on a read-only copy."""
    abspath = os.path.abspath(path)
    if not os.path.isfile(abspath):
        raise FileNotFoundError(abspath)
    uri = Path(abspath).as_uri()

    def _open(query: str) -> sqlite3.Connection:
        conn = sqlite3.connect(uri + query, uri=True, timeout=30, isolation_level=None)
        try:
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except BaseException:
            conn.close()
            raise
        return conn

    if os.path.exists(abspath + "-wal") and os.path.exists(abspath + "-shm"):
        try:
            return _open("?mode=ro"), False
        except sqlite3.OperationalError:
            return _open("?mode=ro&immutable=1"), True
    # no log, or a log without -shm (a mode=ro open would create -shm): the main file only, stale when a log exists
    return _open("?mode=ro&immutable=1"), os.path.exists(abspath + "-wal")


def sleep_gap(wall_delta_s: float, monotonic_delta_s: float) -> float:
    """How long the host was asleep between two heartbeats (the monotonic clock stops while a Mac sleeps)."""
    return max(0.0, wall_delta_s - monotonic_delta_s)


# ---------------------------------------------------------------- loopback guard

class NonLoopbackConnect(PermissionError):
    """Raised by a socket connect of this process to a non-loopback address while loopback_guard() is active."""


_guard_lock = threading.Lock()
_guard_depth = 0
_guard_saved: dict[str, object] = {}


def _is_loopback_address(family: int, address: object) -> bool:
    if family == getattr(socket, "AF_UNIX", object()):
        return True
    if family not in (socket.AF_INET, socket.AF_INET6):
        return False
    if not isinstance(address, tuple) or not address:
        return False
    host = address[0]
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host.lower() == "localhost":
        return True
    try:
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_loopback


def _guarded(name: str, original, addr_index: int):
    def method(self, *args):
        if not _is_loopback_address(self.family, args[addr_index] if args else None):
            raise NonLoopbackConnect(f"loopback_guard: {name} to a non-loopback address refused")
        return original(self, *args)
    method.__name__ = name
    return method


@contextlib.contextmanager
def loopback_guard() -> Iterator[None]:
    """While active, any socket connect (or datagram send) of THIS process to a non-loopback address raises
    NonLoopbackConnect. Re-entrant; child processes are not covered."""
    global _guard_depth
    with _guard_lock:
        if _guard_depth == 0:
            cls = socket.socket
            for name, idx in (("connect", 0), ("connect_ex", 0), ("sendto", -1)):
                _guard_saved[name] = cls.__dict__.get(name, None)
                setattr(cls, name, _guarded(name, getattr(cls, name), idx))
        _guard_depth += 1
    try:
        yield
    finally:
        with _guard_lock:
            _guard_depth -= 1
            if _guard_depth == 0:
                cls = socket.socket
                for name, saved in _guard_saved.items():
                    if saved is None:
                        with contextlib.suppress(AttributeError):
                            delattr(cls, name)
                    else:
                        setattr(cls, name, saved)
                _guard_saved.clear()


# ---------------------------------------------------------------- process environment scrub

def scrub_process_env(name: str) -> bool:
    """Overwrite the value bytes of NAME in this process's environment block in place, then delete NAME from os.environ.

    Returns False when the in-place overwrite was not possible (the variable is still deleted). The value is never
    returned, logged or copied anywhere else.
    """
    value = os.environ.get(name)
    if value is None:
        return False
    overwritten = False
    try:
        value_bytes = os.fsencode(value)
        libc = ctypes.CDLL(None)
        getenv = libc.getenv
        getenv.restype = ctypes.c_void_p
        getenv.argtypes = [ctypes.c_char_p]
        ptr = getenv(os.fsencode(name))
        if ptr and value_bytes and ctypes.string_at(ptr, len(value_bytes)) == value_bytes:
            ctypes.memset(ptr, ord("x"), len(value_bytes))
            overwritten = True
        del value_bytes
    except (OSError, AttributeError, TypeError, ValueError):
        overwritten = False
    finally:
        value = None
        os.environ.pop(name, None)
    return overwritten


# ---------------------------------------------------------------- key digests

def append_key_digest(path: Path, name: str, value: str, *, clock: Clock | None = None) -> dict:
    """Add {"name", "len", "sha256", "utc"} to the keydigests file of 2.1 (mode 0600, one entry per digest).

    Returns the entry (the existing one when this digest is already recorded). The value itself is never stored.
    """
    if not isinstance(value, str) or not value:
        raise ValueError("a key digest needs a non-empty value")
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", name):
        raise ValueError("a key digest needs a variable name")
    path = Path(path)
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    length = len(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    dir_fd = os.open(path.parent, os.O_RDONLY)      # read-modify-write under an exclusive lock on the directory, so two
    try:                                            # processes (runner, exporter) recording at once never drop an entry
        fcntl.flock(dir_fd, fcntl.LOCK_EX)
        doc = read_json(path) if path.exists() else {"schema": "mhforge.keydigests/1", "keys": []}
        if not isinstance(doc, dict) or doc.get("schema") != "mhforge.keydigests/1" or not isinstance(doc.get("keys"), list):
            raise StateError(f"{path.name}: not a mhforge.keydigests/1 file")
        for entry in doc["keys"]:
            if isinstance(entry, dict) and entry.get("sha256") == digest:
                os.chmod(path, 0o600)
                return dict(entry)
        entry = {"name": name, "len": length, "sha256": digest, "utc": utc_now(clock)}
        doc["keys"].append(entry)
        data = (json.dumps(doc, indent=2) + "\n").encode("utf-8")
        _atomic_write_bytes(path, data, fsync=True, mode=0o600)
        return dict(entry)
    finally:
        os.close(dir_fd)                            # releases the lock
