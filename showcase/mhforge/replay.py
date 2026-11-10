# SPDX-License-Identifier: MIT
"""Reply reuse store and in-flight coalescing (spec 3.7, DDL 2.10 replay.sqlite).

The store persists 200 replies per (scope key, reuse key); coalescing of identical requests in flight is in memory and
never crosses processes (one gateway per state directory, enforced by the runner lock).
"""

from __future__ import annotations

import hashlib
import json
import threading
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from . import util
from .errors import StateError
from .ledger import create_file, transaction
from .wire import GatewayReply, JobIdentity

SCOPE_MODES = ("search", "job")
DIGEST_KEYS = ("model", "messages", "temperature", "max_tokens")
ZERO_USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

DDL = [
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS replies (scope_key TEXT NOT NULL, request_sha256 TEXT NOT NULL,
       source_call_id TEXT NOT NULL, source_job_id TEXT NOT NULL, body_zlib BLOB NOT NULL, body_sha256 TEXT NOT NULL,
       finish_reason TEXT, prompt_tokens INTEGER, completion_tokens INTEGER, created_utc TEXT NOT NULL,
       hits INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (scope_key, request_sha256))""",
]


def request_sha256(admitted: Mapping) -> str:
    """sha256(canonical_json) of the admitted body's model, messages, temperature and max_tokens (those present)."""
    return util.sha256_bytes(util.canonical_json({k: admitted[k] for k in DIGEST_KEYS if k in admitted}))


def reuse_key(request_sha256: str, salt: str | None) -> str:
    if salt is None:
        return request_sha256
    return hashlib.sha256((request_sha256 + "\0" + salt).encode("utf-8")).hexdigest()


def zero_usage(body: bytes) -> bytes:
    """The body with usage replaced by zero counters; every other key and value unchanged (key order kept)."""
    doc = json.loads(bytes(body).decode("utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("a completion body must be a JSON object")
    doc["usage"] = dict(ZERO_USAGE)
    try:
        return json.dumps(doc, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:                     # a lone surrogate escape (a cut emoji): keep it escaped
        return json.dumps(doc, ensure_ascii=True).encode("ascii")


def _storable(reply: GatewayReply) -> bool:
    """A 200 merged body with at least one choice (an empty reply with finish_reason 'length' included)."""
    if reply.status != 200:
        return False
    try:
        doc = json.loads(bytes(reply.body).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return isinstance(doc, dict) and isinstance(doc.get("choices"), list) and len(doc["choices"]) >= 1


@dataclass(frozen=True)
class StoredReply:
    body: bytes
    source_call_id: str
    source_job_id: str
    finish_reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None


class _Flight:
    """One in-flight leader and the followers waiting on it."""

    def __init__(self, call_id: str) -> None:
        self.call_id = call_id
        self.done = threading.Event()
        self.reply: GatewayReply | None = None     # set only when the outcome is shared


class Lease:                                       # returned by ReplayStore.begin
    def __init__(self, store: "ReplayStore", role: str, *, stored: StoredReply | None = None,
                 flight: _Flight | None = None, key: tuple[str, str] | None = None, call_id: str = "",
                 bypass: bool = False) -> None:
        self.role = role                           # "hit" | "follower" | "leader"
        self.stored = stored
        self.leader_call_id = flight.call_id if role == "follower" and flight else None
        self._store, self._flight, self._key, self._call_id, self._bypass = store, flight, key, call_id, bypass
        self._finished = False

    @property
    def leader_done(self) -> bool:
        """Follower: the leader committed, failed or abandoned (a None from wait() then means: call begin again)."""
        return self._flight is not None and self._flight.done.is_set()

    def wait(self, timeout: float | None = None) -> GatewayReply | None:
        if self.role != "follower":
            if self.role == "leader":
                raise RuntimeError("a leader does not wait")
            return None
        if not self._flight.done.wait(timeout):
            return None
        return self._flight.reply

    def _finish(self, shared: GatewayReply | None) -> None:
        if self.role != "leader":
            raise RuntimeError("only a leader can finish a lease")
        if self._finished:
            raise RuntimeError("this lease is already finished")
        self._finished = True
        if self._flight is not None:
            self._flight.reply = shared
            self._store._land(self._key, self._flight)

    def commit(self, reply: GatewayReply, *, job_id: str, finish_reason: str | None,
               prompt_tokens: int | None, completion_tokens: int | None) -> None:
        if self.role != "leader":
            raise RuntimeError("only a leader can commit")
        if reply.status != 200:
            raise ValueError("commit takes a 200 reply; use fail() for anything else")
        try:
            if not self._bypass and _storable(reply):
                self._store._put(self._key, reply.body, call_id=self._call_id, job_id=job_id,
                                 finish_reason=finish_reason, prompt_tokens=prompt_tokens,
                                 completion_tokens=completion_tokens)
        finally:
            self._finish(reply)

    def fail(self, reply: GatewayReply, *, share: bool) -> None:
        self._finish(reply if share else None)

    def abandon(self) -> None:
        """Leader crashed or left: followers get None. A no-op for a finished lease and for hits and followers."""
        if self.role == "leader" and not self._finished:
            self._finish(None)


class ReplayStore:
    def __init__(self, path: Path, scope_mode: str, *, clock: util.Clock | None = None) -> None:
        if scope_mode not in SCOPE_MODES:
            raise ValueError(f"scope_mode must be one of {SCOPE_MODES}")
        self.path = Path(path)
        self.scope_mode = scope_mode
        self.clock = clock or util.SystemClock()
        self._lock = threading.Lock()
        self._flights: dict[tuple[str, str], _Flight] = {}
        create_file(self.path, DDL)
        with transaction(self.path) as db:
            meta = {r["key"]: r["value"] for r in db.execute("SELECT key, value FROM meta")}
            if not meta:
                db.execute("INSERT INTO meta (key, value) VALUES ('schema_version', '1')")
                db.execute("INSERT INTO meta (key, value) VALUES ('scope', ?)", (scope_mode,))
            elif meta.get("schema_version") != "1":
                raise StateError(f"replay store schema_version {meta.get('schema_version')!r} is not supported")
            elif meta.get("scope") != scope_mode:
                raise StateError(f"replay store was created with scope {meta.get('scope')!r}, not {scope_mode!r}")

    def scope_key(self, job: JobIdentity) -> str:
        if job.phase == "qualification":
            return "q:" + job.job_key
        if job.repeat is not None:
            return "repeat:" + job.job_key
        return "search" if self.scope_mode == "search" else "job:" + job.job_key

    def begin(self, job: JobIdentity, digest: str, *, call_id: str, bypass: bool = False) -> Lease:
        if bypass:
            return Lease(self, "leader", call_id=call_id, bypass=True)
        key = (self.scope_key(job), digest)
        with self._lock:                           # held across the store read: no leader can land in between
            flight = self._flights.get(key)
            if flight is not None:
                return Lease(self, "follower", flight=flight)
            stored = self._get(key)
            if stored is None:
                flight = _Flight(call_id)
                self._flights[key] = flight
                return Lease(self, "leader", flight=flight, key=key, call_id=call_id)
        self._count_hit(key)
        return Lease(self, "hit", stored=stored)

    def stats(self) -> dict:
        with transaction(self.path) as db:
            stored, hits = db.execute("SELECT COUNT(*), COALESCE(SUM(hits), 0) FROM replies").fetchone()
        with self._lock:
            in_flight = len(self._flights)
        return {"stored": int(stored), "hits": int(hits), "in_flight": in_flight}

    # ------------------------------------------------------------ internals

    def _land(self, key: tuple[str, str], flight: _Flight) -> None:
        with self._lock:
            if self._flights.get(key) is flight:
                del self._flights[key]
        flight.done.set()

    def _get(self, key: tuple[str, str]) -> StoredReply | None:
        with transaction(self.path) as db:
            row = db.execute("SELECT * FROM replies WHERE scope_key=? AND request_sha256=?", key).fetchone()
        if row is None:
            return None
        try:
            body = zlib.decompress(row["body_zlib"])
        except zlib.error:
            return None
        if util.sha256_bytes(body) != row["body_sha256"]:
            return None                            # a mismatch on read is a miss
        return StoredReply(body=body, source_call_id=row["source_call_id"], source_job_id=row["source_job_id"],
                           finish_reason=row["finish_reason"], prompt_tokens=row["prompt_tokens"],
                           completion_tokens=row["completion_tokens"])

    def _count_hit(self, key: tuple[str, str]) -> None:
        with transaction(self.path) as db:
            db.execute("UPDATE replies SET hits = hits + 1 WHERE scope_key=? AND request_sha256=?", key)

    def _put(self, key: tuple[str, str], body: bytes, *, call_id: str, job_id: str, finish_reason: str | None,
             prompt_tokens: int | None, completion_tokens: int | None) -> None:
        body = bytes(body)
        with transaction(self.path) as db:
            db.execute("INSERT OR REPLACE INTO replies (scope_key, request_sha256, source_call_id, source_job_id, "
                       "body_zlib, body_sha256, finish_reason, prompt_tokens, completion_tokens, created_utc, hits) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                       (key[0], key[1], call_id, job_id, zlib.compress(body, 6), util.sha256_bytes(body),
                        finish_reason, prompt_tokens, completion_tokens, util.utc_iso(self.clock.now())))
