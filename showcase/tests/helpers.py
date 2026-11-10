# SPDX-License-Identifier: MIT
"""Shared test helpers (spec 5): temp state trees, a fake clock and id source, the manifest fixture, a fake gateway,
stream-row stamping, the neutral-refusal check, the example directory and skip decorators.

This module is one of the three that may read os.environ, and it reads only MHFORGE_EXAMPLE_DIR.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import glob
import importlib.util
import json
import os
import re
import tempfile
import threading
import time
import unittest
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping

from mhforge import util, wire
from mhforge.paths import StatePaths
from mhforge.wire import GatewayReply, JobIdentity

SC = Path(__file__).resolve().parent.parent          # the showcase directory (the real package's SC)
TESTS = Path(__file__).resolve().parent
FIXTURES = TESTS / "fixtures"
MANIFEST_FIXTURE = FIXTURES / "manifest_valid.json"
VOCAB_FILE = "fb374d419588a4632f3f557e76b4b70aebbca790"   # the tokenizer vocabulary the workers need (2.6 vocab_file)
DEFAULT_EPOCH = 1794268800.0                           # 2026-11-10T00:00:00Z
DELETE = object()                                      # make_manifest override value that removes the key

_DEFAULT_EXAMPLE_REL = "../reference_examples/text_classification"
_DEFAULT_PYTHON_REL = "../reference_examples/text_classification/.venv/bin/python"


# ---------------------------------------------------------------- example directory and skip decorators

def example_dir() -> Path:
    """MHFORGE_EXAMPLE_DIR, or SC/../reference_examples/text_classification (absolute, symlinks not resolved)."""
    override = os.environ.get("MHFORGE_EXAMPLE_DIR")
    if override:
        return Path(os.path.abspath(override))
    return Path(os.path.abspath(os.path.join(SC, _DEFAULT_EXAMPLE_REL)))


def example_python() -> Path:
    """<example>/.venv/bin/python, lexical (never resolved: the symlink target has no litellm)."""
    return example_dir() / ".venv" / "bin" / "python"


def tokenizer_dir() -> Path | None:
    """LiteLLM's bundled tokenizers directory inside the example venv, or None."""
    pattern = str(example_dir() / ".venv" / "lib" / "python*" / "site-packages" / "litellm" / "litellm_core_utils"
                  / "tokenizers")
    for hit in sorted(glob.glob(pattern)):
        return Path(hit)
    return None


def _has_example_venv() -> bool:
    tok = tokenizer_dir()
    return example_python().exists() and tok is not None and (tok / VOCAB_FILE).is_file()


def _has_yaml() -> bool:
    try:
        return importlib.util.find_spec("yaml") is not None
    except (ImportError, ValueError):
        return False


def needs_example_dir(obj):
    return unittest.skipUnless(example_dir().is_dir(), "example directory not present")(obj)


def needs_example_venv(obj):
    return unittest.skipUnless(_has_example_venv(), "example venv or bundled tokenizer file not present")(obj)


def needs_yaml(obj):
    return unittest.skipUnless(_has_yaml(), "yaml is not importable")(obj)


def child_env(**extra: str) -> dict[str, str]:
    """A minimal explicit environment for a test child process (never a copy of os.environ)."""
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8",
           "PYTHONPATH": str(SC)}
    env.update(extra)
    return env


def venv_child_env(scratch: Path, **extra: str) -> dict[str, str]:
    """Environment for a child on the example venv's Python: no download, no write under the repository."""
    scratch = Path(scratch)
    cache = scratch / "llm-cache"
    cache.mkdir(parents=True, exist_ok=True)
    env = child_env(LITELLM_LOCAL_MODEL_COST_MAP="True", TEXT_CLASSIFICATION_LLM_CACHE_DIR=str(cache),
                    HOME=str(scratch), TMPDIR=str(scratch))
    tok = tokenizer_dir()
    if tok is not None:
        env["TIKTOKEN_RS_CACHE_DIR"] = str(tok)
    env.update(extra)
    return env


# ---------------------------------------------------------------- manifest fixture

def _set_dotted(doc: dict, dotted: str, value: object) -> None:
    parts = dotted.split(".")
    node = doc
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            if value is DELETE:
                return
            nxt = {}
            node[part] = nxt
        node = nxt
    if value is DELETE:
        node.pop(parts[-1], None)
    else:
        node[parts[-1]] = copy.deepcopy(value)


def make_manifest(overrides: Mapping[str, object] | None = None, /, **dotted_overrides: object) -> dict:
    """A deep copy of fixtures/manifest_valid.json with dotted overrides applied.

    make_manifest({"provider.kind": "stub"}) and make_manifest(**{"provider.kind": "stub"}) are the same; a keyword may
    also spell the dot as a double underscore (provider__kind="stub"). The value DELETE removes the key.
    """
    with open(MANIFEST_FIXTURE, "rb") as fh:
        doc = json.loads(fh.read().decode("utf-8"))
    merged: dict[str, object] = dict(overrides or {})
    for key, value in dotted_overrides.items():
        merged[key.replace("__", ".")] = value
    for key, value in merged.items():
        _set_dotted(doc, key, value)
    return doc


def _absolute_example_path(value: object, default_rel: str, replacement: Path) -> object:
    if not isinstance(value, str) or os.path.isabs(value):
        return value
    if value == default_rel:
        return str(replacement)
    return os.path.abspath(os.path.join(SC, value))


def write_manifest(root: Path, manifest: dict) -> Path:
    """Write the manifest to <root>/manifest.json with upstream.example_dir and upstream.worker_python made absolute
    (relative values are taken against the real SC, never resolved). Returns the path; root is then the test's SC."""
    doc = copy.deepcopy(manifest)
    up = doc.get("upstream")
    if isinstance(up, dict):
        if "example_dir" in up:
            up["example_dir"] = _absolute_example_path(up["example_dir"], _DEFAULT_EXAMPLE_REL, example_dir())
        if "worker_python" in up:
            up["worker_python"] = _absolute_example_path(up["worker_python"], _DEFAULT_PYTHON_REL, example_python())
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "manifest.json"
    util.write_json(path, doc)
    return path


@dataclass
class TmpState:
    sc: Path                                           # the temp SC (holds manifest.json when one was written)
    campaign: str
    paths: StatePaths                                  # sc/state/<campaign>
    manifest_path: Path | None = None
    manifest: dict | None = None

    @property
    def root(self) -> Path:
        return self.paths.root


@contextlib.contextmanager
def tmp_state(campaign: str | None = None, *, manifest: dict | bool | None = None,
              create: bool = True) -> Iterator[TmpState]:
    """A temp SC-shaped tree with state/<campaign> (created unless create=False), removed afterwards.

    manifest=True writes make_manifest() (with campaign and state_dir set to `campaign` when given); a dict is
    written as it is. The yielded TmpState carries sc, campaign, paths, manifest_path and manifest.
    """
    doc: dict | None = None
    if manifest is True:
        doc = make_manifest()
        if campaign is not None:
            doc["campaign"] = campaign
            doc["state_dir"] = "state/" + campaign
    elif isinstance(manifest, dict):
        doc = manifest
    if campaign is None:
        campaign = doc["campaign"] if doc else "mh-test"
    with tempfile.TemporaryDirectory(prefix="mhf-test-") as tmp:
        sc = Path(tmp).resolve() / "sc"
        sc.mkdir()
        paths = StatePaths(sc / "state" / campaign)
        if create:
            paths.root.mkdir(parents=True)
        state = TmpState(sc=sc, campaign=campaign, paths=paths)
        if doc is not None:
            state.manifest_path = write_manifest(sc, doc)
            state.manifest = doc
        yield state


# ---------------------------------------------------------------- clock and ids

class FakeClock:
    """Manual wall and monotonic clocks; sleep() records the duration and advances both."""

    def __init__(self, start: float = DEFAULT_EPOCH, monotonic_start: float = 1000.0) -> None:
        self._now = float(start)
        self._mono = float(monotonic_start)
        self.sleeps: list[float] = []
        self._lock = threading.Lock()

    def now(self) -> float:
        with self._lock:
            return self._now

    def monotonic(self) -> float:
        with self._lock:
            return self._mono

    def sleep(self, seconds: float) -> None:
        with self._lock:
            self.sleeps.append(float(seconds))
            if seconds > 0:
                self._now += seconds
                self._mono += seconds

    def advance(self, seconds: float) -> None:
        with self._lock:
            self._now += seconds
            self._mono += seconds

    def advance_wall(self, seconds: float) -> None:
        """Wall clock only (the host slept: the monotonic clock stood still)."""
        with self._lock:
            self._now += seconds

    def utc(self) -> str:
        return util.utc_iso(self.now())


class SeqIds(util.IdSource):
    """Deterministic IdSource: the 12-hex part is a counter, tokens are derived from the counter."""

    def __init__(self, clock: util.Clock | None = None, start: int = 1) -> None:
        super().__init__(clock=clock or FakeClock())
        self._n = start - 1

    def _next(self) -> int:
        with self._lock:
            self._n += 1
            return self._n

    def _hex12(self) -> str:
        return "%012x" % self._next()

    def token(self) -> str:
        n = self._next()
        raw = n.to_bytes(32, "big")
        return "mhfjob_" + base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------- fake gateway

def completion_body(content: str = "ok", *, model: str = "openai/gpt-oss-120b", finish_reason: str = "stop",
                    prompt_tokens: int = 10, completion_tokens: int = 2, reasoning: str | None = None) -> bytes:
    message: dict = {"role": "assistant", "content": content}
    if reasoning is not None:
        message["reasoning"] = reasoning
    return json.dumps({"id": "chatcmpl-fake", "object": "chat.completion", "created": int(DEFAULT_EPOCH), "model": model,
                       "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
                       "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                                 "total_tokens": prompt_tokens + completion_tokens}}).encode("utf-8")


def completion_reply(content: str = "ok", **kw) -> GatewayReply:
    return GatewayReply(status=200, headers=(("content-type", "application/json"), (wire.SOURCE_HEADER, "provider")),
                        body=completion_body(content, **kw))


Scripted = GatewayReply | BaseException | Callable[[JobIdentity | None, bytes], GatewayReply]


class FakeGateway:
    """A wire.HandlesRequests with scripted replies, a call log, a settable halt state and kill requests.

    Each handle() takes the next scripted item (a GatewayReply, an exception to raise, or a callable(job, request));
    when the script is empty it answers `default` (a callable or reply; a small completion by default). A request for
    a job that is None or not registered gets the unknown_job refusal (unless require_registration=False); while a
    halt state is set every request gets the halted refusal.
    """

    def __init__(self, replies: Iterable[Scripted] = (), *, default: Scripted | None = None,
                 require_registration: bool = True, delay_s: float = 0.0) -> None:
        self._lock = threading.Lock()
        self.script: list[Scripted] = list(replies)
        self.default = default if default is not None else completion_reply()
        self.require_registration = require_registration
        self.delay_s = delay_s
        self.registered: dict[str, JobIdentity] = {}
        self.tokens: dict[str, str | None] = {}
        self.closed: list[str] = []
        self.calls: list[dict] = []
        self.halt_state: object | None = None
        self.kills: dict[str, str] = {}
        self.in_flight = 0
        self.peak_in_flight = 0

    def add(self, *replies: Scripted) -> None:
        with self._lock:
            self.script.extend(replies)

    def register_job(self, job: JobIdentity, *, token: str | None = None) -> None:
        with self._lock:
            self.registered[job.job_id] = job
            self.tokens[job.job_id] = token

    def close_job(self, job_id: str, *, drain_timeout_s: float | None = None) -> dict:
        with self._lock:
            self.registered.pop(job_id, None)
            self.closed.append(job_id)
            n = sum(1 for c in self.calls if c["job_id"] == job_id)
        return {"job_id": job_id, "calls": n, "in_flight_at_close": 0}

    def set_halt(self, state: object | None = "operator") -> None:
        self.halt_state = state

    def halted(self) -> object | None:
        return self.halt_state

    def request_kill(self, job_id: str, reason: str = "refusal_flood") -> None:
        with self._lock:
            self.kills[job_id] = reason

    def kill_requested(self, job_id: str) -> str | None:
        with self._lock:
            return self.kills.get(job_id)

    def handle(self, job: JobIdentity | None, request: bytes, *, ingress: str = "loopback",
               no_replay: bool = False) -> GatewayReply:
        try:
            method, path, body = wire.parse_envelope(request)
        except ValueError:
            method = path = None
            body = request
        with self._lock:
            entry = {"job_id": job.job_id if job is not None else None, "job": job, "request": bytes(request),
                     "method": method, "path": path, "body": bytes(body), "ingress": ingress, "no_replay": no_replay,
                     "t": time.monotonic()}
            self.calls.append(entry)
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
            if self.halt_state is not None:
                item: Scripted = wire.refusal_reply("halted")
            elif self.require_registration and (job is None or job.job_id not in self.registered):
                item = wire.refusal_reply("unknown_job")
            elif self.script:
                item = self.script.pop(0)
            else:
                item = self.default
        try:
            if self.delay_s:
                time.sleep(self.delay_s)
            if isinstance(item, BaseException):
                raise item
            reply = item(job, request) if callable(item) else item
            entry["status"] = reply.status
            return reply
        finally:
            with self._lock:
                self.in_flight -= 1


# ---------------------------------------------------------------- stream rows and neutral refusals

def stamp_rows(lines: Iterable[str | bytes], start_utc: str, step_s: float, *, stream: str = "stdout") -> list[dict]:
    """Raw stream-json lines -> the {"t_utc", "stream", "line"} rows of stream.jsonl (blank lines dropped)."""
    t0 = util.parse_utc(start_utc)
    rows = []
    for raw in lines:
        line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        line = line.rstrip("\r\n")
        if not line:
            continue
        rows.append({"t_utc": util.utc_iso(t0 + len(rows) * step_s), "stream": stream, "line": line})
    return rows


LITELLM_RATE_LIMIT_PATTERNS = (re.compile(r"\b429\b", re.I), re.compile(r"rate[\s_\-]*limit", re.I),
                               re.compile(r"request too large", re.I))


def neutral_problems(reply) -> list[str]:
    """Why a reply is not a neutral refusal ([] when it is)."""
    problems = []
    status = getattr(reply, "status", None)
    if status not in (402, 403):
        problems.append(f"status {status} is not 402 or 403")
    headers = {str(k).lower(): str(v) for k, v in (getattr(reply, "headers", ()) or ())}
    if headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        problems.append("content-type is not application/json")
    if headers.get("x-should-retry", "").lower() != "false":
        problems.append("x-should-retry is not false")
    try:
        err = json.loads(bytes(getattr(reply, "body", b"")).decode("utf-8"))["error"]
        message, code = err["message"], err.get("code", "")
    except (ValueError, KeyError, TypeError):
        return problems + ["body is not {'error': {'message': ...}}"]
    if not isinstance(message, str) or not message:
        return problems + ["message is empty"]
    if re.search(r"\d", message):
        problems.append("message contains a digit")
    for text, what in ((message, "message"), (str(code or ""), "code")):
        low = text.lower()
        for trigger in wire.RETRY_TRIGGER_STRINGS:
            if trigger in low:
                problems.append(f"{what} contains the retry trigger {trigger!r}")
        for pat in LITELLM_RATE_LIMIT_PATTERNS:
            if pat.search(text):
                problems.append(f"{what} matches LiteLLM pattern {pat.pattern!r}")
    return problems


def assert_neutral(reply) -> None:
    """AssertionError unless the reply is a neutral refusal: 402/403, the two headers, no digit, no retry trigger and
    no LiteLLM rate-limit pattern in any letter case."""
    problems = neutral_problems(reply)
    if problems:
        raise AssertionError("reply is not neutral: " + "; ".join(problems))
