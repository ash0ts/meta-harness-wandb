# SPDX-License-Identifier: Apache-2.0
# Adapted from ash0ts/harness2-wandb@31b0f82 harness2/showcase/gateway.py (Gateway request checks and call records) and
# transport_v4.py (forward, model_identity_problem). Changes: transport-agnostic core with no socket (job identity and
# request bytes in, status, headers and body out); closed admission of exact keys with no rewriting; one non-streaming
# merged body to the worker; neutral 402/403 refusals only; the classify table, ledger reservation per attempt, reuse
# store and in-flight coalescing; bounded semaphore with a held-slot flag; three-valued circuit outcome; refusal-record
# cap and kill request; repeatable-failure rule; job registry, close and drain; identity gate on a configured model.
"""Gateway core (spec 3.10): (job identity, request envelope) -> GatewayReply. Blocks; never raises."""

from __future__ import annotations

import json
import math
import random
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from . import util, wire
from .circuit import Circuit
from .errors import BudgetError
from .ledger import HaltState, Ledger, Settlement, reservation_micro
from .paths import StatePaths
from .provider import ProviderClient, ProviderResult, circuit_outcome, classify, settle_basis
from .replay import ReplayStore, request_sha256 as _digest, reuse_key, zero_usage
from .sse import to_completion_body
from .wire import GatewayReply, JobIdentity

__all__ = ["admit", "input_limit", "backoff_s", "model_identity_problem", "conformance", "Gateway", "ALLOWED_KEYS"]

SCHEMA = "mhforge.call/1"
ALLOWED_KEYS = frozenset({"model", "messages", "temperature", "max_tokens", "usage", "stream"})
FORWARD_KEYS = ("model", "messages", "temperature", "max_tokens")
ROLES = frozenset({"system", "user", "assistant"})
EXCERPT_HEAD_CHARS = 2048
DETAIL_CHARS = 2000
POLL_S = 1.0                                       # follower, queue and drain re-check interval (real seconds)
SETTLE_RETRIES = 3                                 # retries of a settle on sqlite3.OperationalError ...
SETTLE_RETRY_WAIT_S = 30.0                         # ... over 90 s in all
KILL_FACTOR = 10                                   # kill_requested once refusals reach this times the record cap
COUNTER_KEYS = ("completed", "replayed", "coalesced", "refused", "refused_unrecorded", "provider_rejected", "failed",
                "repeatable_provider_failures", "truncated_empty", "cost_micro", "bounded_unknown_micro",
                "in_flight_at_close", "usage_calls", "prompt_tokens_sum", "prompt_tokens_max", "completion_tokens_sum")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _reject_constant(name: str) -> object:
    raise ValueError("non-JSON constant " + name)


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    return "".join(p["text"] for p in content) if isinstance(content, list) else ""


# ---------------------------------------------------------------- pure helpers

def admit(raw: bytes, manifest: dict) -> tuple[dict | None, list[str], str | None, int]:
    """(admitted body or None, dropped keys, refusal code or None, content_bytes) per 3.10 step 5."""
    def g(name: str) -> int:
        return int(util.lookup(manifest, "gateway." + name))

    raw = bytes(raw)
    if not raw:
        return None, [], "invalid_request", 0
    if len(raw) > g("max_request_bytes"):
        return None, [], "request_too_large", 0
    try:
        doc = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None, [], "invalid_request", 0
    if not isinstance(doc, dict) or set(doc) - ALLOWED_KEYS:
        return None, [], "invalid_request", 0
    dropped = []
    if "usage" in doc:
        dropped.append("usage")
    if "stream" in doc:
        if doc["stream"] is not False:
            return None, [], "invalid_request", 0
        dropped.append("stream")
    if doc.get("model") != util.lookup(manifest, "provider.wire_model"):
        return None, sorted(dropped), "model_not_allowed", 0
    max_tokens = doc.get("max_tokens")
    if not _is_int(max_tokens) or not 1 <= max_tokens <= g("max_output_tokens"):
        return None, sorted(dropped), "invalid_request", 0
    if "temperature" in doc:
        t = doc["temperature"]
        if not isinstance(t, (int, float)) or isinstance(t, bool) or not math.isfinite(t) or not 0 <= t <= 2:
            return None, sorted(dropped), "invalid_request", 0
    messages = doc.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= g("max_messages"):
        return None, sorted(dropped), "invalid_request", 0
    content_bytes = 0
    try:
        for m in messages:
            if not isinstance(m, dict) or set(m) != {"role", "content"} or m["role"] not in ROLES:
                return None, sorted(dropped), "invalid_request", 0
            content = m["content"]
            if isinstance(content, str):
                content_bytes += len(content.encode("utf-8"))
            elif isinstance(content, list):
                for part in content:
                    if (not isinstance(part, dict) or set(part) != {"type", "text"} or part["type"] != "text"
                            or not isinstance(part["text"], str)):
                        return None, sorted(dropped), "invalid_request", 0
                    content_bytes += len(part["text"].encode("utf-8"))
            else:
                return None, sorted(dropped), "invalid_request", 0
    except UnicodeEncodeError:                     # a lone surrogate cannot be forwarded as UTF-8
        return None, sorted(dropped), "invalid_request", 0
    if content_bytes > g("max_content_bytes"):
        return None, sorted(dropped), "request_too_large", content_bytes
    return {k: doc[k] for k in FORWARD_KEYS if k in doc}, sorted(dropped), None, content_bytes


def input_limit(content_bytes: int, n_messages: int, manifest: dict) -> int:
    g = lambda name: int(util.lookup(manifest, "gateway." + name))   # noqa: E731
    return min(content_bytes + g("template_overhead_tokens") + n_messages * g("per_message_overhead_tokens"),
               g("model_context_tokens"))


def backoff_s(disposition: str, http_status: int | None, manifest: dict, rng: random.Random) -> float:
    retry = util.lookup(manifest, "provider.retry")
    if disposition == "retry_rate_limited":
        lo, hi = retry["backoff_429_s"]
    elif http_status is not None and (500 <= http_status <= 599 or http_status == 408):
        lo, hi = retry["backoff_5xx_s"]
    else:
        lo, hi = retry["backoff_other_s"]
    return rng.uniform(float(lo), float(hi))


def model_identity_problem(response_model: str | None, has_output: bool, expected: str | None) -> str | None:
    """None when the gate is off (expected None), the model matches, or a failed reply carries no output."""
    if expected is None:
        return None
    if response_model:
        return None if response_model == expected else "Returned model differs from the expected response model"
    return "Provider completion is missing its model identity" if has_output else None


def conformance(admitted: Mapping, manifest: dict) -> dict[str, bool]:
    expect = util.lookup(manifest, "upstream.expect")
    system = next((_text_of(m["content"]) for m in admitted.get("messages", []) if m.get("role") == "system"), None)
    return {"temperature": "temperature" in admitted and admitted["temperature"] == expect.get("temperature"),
            "max_tokens": admitted.get("max_tokens") == expect.get("max_tokens"),
            "system_prompt": system is not None and system == expect.get("system_prompt")}


def _dump(doc: object) -> bytes:
    try:
        return json.dumps(doc, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:                     # a lone surrogate from the provider: keep it escaped
        return json.dumps(doc, ensure_ascii=True).encode("ascii")


def _safe_strings(value: object) -> object:
    if isinstance(value, str):
        return value.encode("utf-8", "backslashreplace").decode("utf-8")
    if isinstance(value, dict):
        return {_safe_strings(k): _safe_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_safe_strings(v) for v in value]
    return value


def _body_facts(body: Mapping) -> dict:
    """usage, finish_reason, response_model, response_id and flags of a completion body (2.5)."""
    choices = body.get("choices") or [{}]
    choice = choices[0] if isinstance(choices[0], Mapping) else {}
    message = choice.get("message") if isinstance(choice.get("message"), Mapping) else {}
    content = message.get("content") if isinstance(message.get("content"), str) else ""
    usage = body.get("usage")
    flat = None
    if isinstance(usage, Mapping):
        def detail(parent: str, key: str) -> object:
            d = usage.get(parent)
            return d.get(key) if isinstance(d, Mapping) else None
        flat = {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")}
        flat["cached_tokens"] = detail("prompt_tokens_details", "cached_tokens")
        flat["reasoning_tokens"] = detail("completion_tokens_details", "reasoning_tokens")
    finish = choice.get("finish_reason")
    return {"usage": flat, "finish_reason": finish, "response_model": body.get("model"), "response_id": body.get("id"),
            "flags": {"truncated_empty": finish == "length" and content == "", "empty_content": content == "",
                      "reasoning_present": bool(message.get("reasoning")), "model_mismatch": False}}


def _source(reply: GatewayReply) -> str:
    return next((v for k, v in reply.headers if k.lower() == wire.SOURCE_HEADER), "gateway")


def _with_source(reply: GatewayReply, source: str) -> GatewayReply:
    headers = tuple((k, source if k.lower() == wire.SOURCE_HEADER else v) for k, v in reply.headers)
    return GatewayReply(reply.status, headers, reply.body)


def _new_counters() -> dict:
    out: dict = {k: 0 for k in COUNTER_KEYS}
    out["refusal_codes"] = {}
    return out


# ---------------------------------------------------------------- per-job and per-request state

@dataclass
class _JobState:
    job: JobIdentity
    prior_charged: bool
    closed: bool = False
    active: int = 0                                # handle calls between steps 8 and 11
    refusals: int = 0                              # refused outcomes, recorded or not
    counters: dict = field(default_factory=_new_counters)


@dataclass
class _Call:
    call_id: str
    rec: dict
    raw: bytes
    st: _JobState | None = None
    path: Path | None = None
    admission_written: bool = False
    done: bool = False
    extra: dict = field(default_factory=dict)


@dataclass
class _End:                                        # a terminal result of the attempt loop
    reply: GatewayReply
    outcome: str
    code: str | None = None
    detail: str = ""
    share: bool = True


class Gateway:                                     # implements wire.HandlesRequests
    def __init__(self, manifest: dict, paths: StatePaths, *, provider: ProviderClient, ledger: Ledger,
                 replay: ReplayStore, circuit: Circuit, clock: util.Clock | None = None,
                 ids: util.IdSource | None = None, rng: random.Random | None = None) -> None:
        self.manifest, self.paths = manifest, paths
        self.provider, self.ledger, self.replay, self.circuit = provider, ledger, replay, circuit
        self.clock = clock or util.SystemClock()
        self.ids = ids or util.IdSource(self.clock)
        self.rng = rng or random.Random()
        p = lambda name: util.lookup(manifest, "provider." + name)   # noqa: E731
        self.wire_model = p("wire_model")
        self.expected_model = p("expected_response_model")
        self.deadline_s = float(p("attempt_deadline_s"))
        self.max_attempts = 1 + int(p("retry.max_transport_retries"))
        self.circuit_wait_s = float(p("circuit.cooldown_s")) + self.deadline_s + 60
        self.terminal_429 = tuple(p("terminal_429_patterns") or ())
        self.max_in_flight = int(util.lookup(manifest, "gateway.max_in_flight"))
        self.record_cap = int(util.lookup(manifest, "gateway.max_refused_records_per_job"))
        self.poll_s = POLL_S
        self._sem = threading.BoundedSemaphore(self.max_in_flight)
        self._cond = threading.Condition()
        self._jobs: dict[str, _JobState] = {}
        self._unknown_refusals = 0
        self._aborted = False
        self._ledger_fault: HaltState | None = None
        self._attempts_open = 0
        self._ok_seq = 0                           # provider attempts that ended ok (the peer_ok rule of 9.7)

    # ------------------------------------------------------------ registry

    def register_job(self, job: JobIdentity, *, token: str | None = None) -> None:
        job.validate()
        if token is not None and (not isinstance(token, str) or not token):
            raise ValueError("a job token must be a non-empty string")
        with self._cond:
            existing = self._jobs.get(job.job_id)
            if existing is not None:
                if existing.job != job or existing.closed:
                    raise ValueError("job id already registered with another identity or closed")
                return
        self.ledger.register_job(job, token_sha256=None if token is None else util.sha256_bytes(token.encode()))
        prior = self.ledger.prior_charged(job.job_key)
        with self._cond:
            self._jobs.setdefault(job.job_id, _JobState(job=job, prior_charged=prior))

    def close_job(self, job_id: str, *, drain_timeout_s: float | None = None) -> dict:
        timeout = self.deadline_s + 60 if drain_timeout_s is None else float(drain_timeout_s)
        try:
            self.ledger.close_job(job_id)
        except (sqlite3.Error, OSError):
            pass                                   # the in-memory close is what refuses new requests
        end = time.monotonic() + max(0.0, timeout)
        with self._cond:
            st = self._jobs.get(job_id)
            if st is None:
                return _new_counters()
            st.closed = True
            self._cond.notify_all()
            while st.active > 0 and time.monotonic() < end:
                self._cond.wait(min(self.poll_s, max(0.001, end - time.monotonic())))
            out = dict(st.counters)
            out["refusal_codes"] = dict(st.counters["refusal_codes"])
            out["in_flight_at_close"] = st.active
            return out

    def _state(self, job: JobIdentity | None) -> _JobState | None:
        if job is None:
            return None
        with self._cond:
            st = self._jobs.get(job.job_id) if isinstance(job, JobIdentity) else None
            return st if st is not None and not st.closed and st.job == job else None

    # ------------------------------------------------------------ halt, abort, drain

    def halted(self) -> HaltState | None:
        with self._cond:
            if self._ledger_fault is not None:
                return self._ledger_fault
        try:
            return self.ledger.halted()
        except (sqlite3.Error, OSError):
            return None                            # reserve() re-checks the halt row inside its own transaction

    def _stopped(self) -> bool:
        return self._aborted or self.halted() is not None

    def kill_requested(self, job_id: str) -> str | None:
        with self._cond:
            st = self._jobs.get(job_id)
            return "refusal_flood" if st is not None and st.refusals >= KILL_FACTOR * self.record_cap else None

    def abort_queued(self) -> None:
        with self._cond:
            self._aborted = True
            self._cond.notify_all()

    def in_flight(self) -> int:
        with self._cond:
            return self._attempts_open

    def drain(self, timeout_s: float) -> int:
        end = time.monotonic() + max(0.0, float(timeout_s))
        with self._cond:
            while self._attempts_open > 0 and time.monotonic() < end:
                self._cond.wait(min(self.poll_s, max(0.001, end - time.monotonic())))
            return self._attempts_open

    def _fault(self, call_id: str, detail: str) -> None:
        with self._cond:
            if self._ledger_fault is None:
                self._ledger_fault = HaltState("ledger_fault", detail, util.utc_iso(self.clock.now()), call_id)
        try:
            self.ledger.set_halt("ledger_fault", detail, call_id)
        except Exception:                          # noqa: BLE001 - the runner's halt_check retries it
            pass

    def _halt(self, reason: str, detail: str, call_id: str) -> None:
        try:
            self.ledger.set_halt(reason, detail[:DETAIL_CHARS], call_id)
        except Exception:                          # noqa: BLE001
            self._fault(call_id, f"set_halt {reason} failed")

    # ------------------------------------------------------------ records

    def _write(self, path: Path, doc: dict) -> None:
        try:
            util.write_json(path, doc)
        except UnicodeEncodeError:
            util.write_json(path, _safe_strings(doc))

    def _excerpt(self, call: _Call) -> None:
        call.rec.pop("request", None)
        call.rec["request_excerpt"] = {"bytes": len(call.raw), "sha256": util.sha256_bytes(call.raw),
                                       "head": call.raw.decode("utf-8", "replace")[:EXCERPT_HEAD_CHARS]}

    def _finish(self, call: _Call, end: _End) -> GatewayReply:
        """Steps 10 and 11 for the record: fill the result fields, count, write (or count a capped refusal)."""
        rec, reply = call.rec, end.reply
        call.done = True
        rec["outcome"] = end.outcome
        rec["ended_utc"] = util.utc_iso(self.clock.now())
        if end.code is not None:
            rec["refusal"] = {"code": end.code, "status": reply.status, "detail": end.detail[:DETAIL_CHARS]}
        rec["reply"] = {"status": reply.status, "code": end.code, "source": _source(reply)}
        attempts = rec["attempts"]
        spent = sum(a["spent_micro"] or 0 for a in attempts if a.get("ledger_status") == "complete")
        unknown = sum(a["reserved_micro"] for a in attempts if a.get("ledger_status") in ("bounded_unknown", "pending",
                                                                                          "unknown"))
        if end.outcome in ("replayed", "coalesced") or not attempts:
            rec["cost_micro"], rec["cost_basis"] = 0, "none"
        else:
            rec["cost_micro"] = spent
            rec["cost_basis"] = "reservation" if unknown else ("usage" if spent or any(
                a.get("ledger_status") == "complete" for a in attempts) else "none")
        recorded = True
        with self._cond:
            st = call.st
            if end.outcome == "refused":
                if st is None:
                    self._unknown_refusals += 1
                    recorded = self._unknown_refusals <= self.record_cap
                else:
                    st.refusals += 1
                    recorded = st.refusals <= self.record_cap
            if st is not None:
                c = st.counters
                if end.outcome == "refused" and not recorded:
                    c["refused_unrecorded"] += 1
                else:
                    c[end.outcome] += 1
                if end.code is not None:
                    c["refusal_codes"][end.code] = c["refusal_codes"].get(end.code, 0) + 1
                if call.extra.get("repeatable"):
                    c["repeatable_provider_failures"] += 1
                if rec["flags"]["truncated_empty"]:
                    c["truncated_empty"] += 1
                c["cost_micro"] += rec["cost_micro"]
                c["bounded_unknown_micro"] += unknown
                usage = rec["usage"]
                if end.outcome == "completed" and usage and _is_int(usage.get("prompt_tokens")):
                    c["usage_calls"] += 1
                    c["prompt_tokens_sum"] += usage["prompt_tokens"]
                    c["prompt_tokens_max"] = max(c["prompt_tokens_max"], usage["prompt_tokens"])
                    c["completion_tokens_sum"] += usage.get("completion_tokens") or 0
        if end.outcome == "refused":
            self._excerpt(call)
        try:
            if recorded:
                self._write(call.path, rec)
            elif call.admission_written:
                call.path.unlink(missing_ok=True)
        except OSError:
            pass                                   # a full disk must not turn a reply into an exception
        return reply

    def _refuse(self, call: _Call, code: str, detail: str, outcome: str = "refused") -> GatewayReply:
        return self._finish(call, _End(wire.refusal_reply(code), outcome, code, detail))

    # ------------------------------------------------------------ handle

    def handle(self, job: JobIdentity | None, request: bytes, *, ingress: str = "loopback",
               no_replay: bool = False) -> GatewayReply:
        call = None
        try:
            call = self._new_call(job, request, ingress)
            return self._handle(call, job, bytes(request), no_replay)
        except Exception:                          # noqa: BLE001 - an internal fault is a reply and a record
            if call is not None and not call.done:
                try:
                    return self._refuse(call, "gateway_internal", "internal fault",
                                        "failed" if call.rec.get("attempts") else "refused")
                except Exception:                  # noqa: BLE001
                    pass
            return wire.refusal_reply("gateway_internal")

    def _new_call(self, job: JobIdentity | None, request: bytes, ingress: str) -> _Call:
        call_id = self.ids.call_id()
        rec = {"schema": SCHEMA, "call_id": call_id, "job": None, "ingress": str(ingress), "outcome": None,
               "refusal": None, "received_utc": util.utc_iso(self.clock.now()), "admitted_utc": None,
               "ended_utc": None, "queue_s": 0.0, "request": None, "dropped_keys": [], "request_sha256": None,
               "user_sha256": None, "content_bytes": None, "n_messages": None, "input_limit": None,
               "output_limit": None, "conformance": None, "replay": None, "reuse_salt": None, "attempts": [],
               "response": None, "usage": None, "finish_reason": None, "response_model": None, "response_id": None,
               "flags": {"truncated_empty": False, "empty_content": False, "reasoning_present": False,
                         "model_mismatch": False},
               "cost_micro": 0, "cost_basis": "none", "reply": None}
        if isinstance(job, JobIdentity):
            rec["job"] = job.to_dict()
        try:
            raw = wire.parse_envelope(bytes(request))[2]
        except ValueError:
            raw = bytes(request)
        return _Call(call_id=call_id, rec=rec, raw=raw, path=self.paths.calls_dir(None) / f"{call_id}.json")

    def _handle(self, call: _Call, job: JobIdentity | None, request: bytes, no_replay: bool) -> GatewayReply:
        st = self._state(job)                      # looked up first so that a step-2 refusal counts for its job
        if st is not None:
            call.st = st
            call.path = self.paths.calls_dir(job.job_id) / f"{call.call_id}.json"
        try:                                       # step 2
            method, path, body = wire.parse_envelope(request)
        except ValueError:
            return self._refuse(call, "not_allowed", "malformed envelope")
        if method != "POST" or not path.endswith("/chat/completions"):
            return self._refuse(call, "not_allowed", f"{method} not served")
        if st is None:                             # step 3
            with self._cond:
                known = self._jobs.get(job.job_id) if isinstance(job, JobIdentity) else None
                closed = known is not None and known.closed and known.job == job
            return self._refuse(call, "unknown_job", "job_closed" if closed else "unknown job")
        if self._stopped():                        # step 4
            return self._refuse(call, "halted", "halted before admission")
        admitted, dropped, code, content_bytes = admit(body, self.manifest)   # step 5
        call.rec["dropped_keys"] = dropped
        if code is not None:
            call.rec["content_bytes"] = content_bytes or None
            return self._refuse(call, code, "refused at admission")
        rec = call.rec                             # step 6
        digest = _digest(admitted)
        salt = None if job.sample is None else f"s{job.sample}"
        last_user = next((_text_of(m["content"]) for m in reversed(admitted["messages"]) if m["role"] == "user"), None)
        rec.update(admitted_utc=util.utc_iso(self.clock.now()), request=admitted, request_sha256=digest,
                   user_sha256=None if last_user is None else util.sha256_bytes(last_user.encode("utf-8")),
                   content_bytes=content_bytes, n_messages=len(admitted["messages"]),
                   input_limit=input_limit(content_bytes, len(admitted["messages"]), self.manifest),
                   output_limit=admitted["max_tokens"], conformance=conformance(admitted, self.manifest),
                   reuse_salt=salt)
        self._write(call.path, {k: v for k, v in rec.items() if k not in ("outcome", "ended_utc", "reply")})
        call.admission_written = True
        bypass = no_replay and job.phase == "qualification"
        return self._serve(call, st, job, admitted, reuse_key(digest, salt), bypass)

    def _scope_kind(self, job: JobIdentity) -> str:
        if job.phase == "qualification":
            return "qualification"
        return "repeat" if job.repeat is not None else self.replay.scope_mode

    def _reused(self, call: _Call, reply: GatewayReply, source_body: bytes, kind: str, source_call_id: str | None,
                outcome: str) -> GatewayReply:
        try:
            facts = _body_facts(json.loads(bytes(source_body).decode("utf-8")))
            call.rec.update(facts)
        except (ValueError, UnicodeDecodeError, AttributeError, TypeError):
            pass
        call.rec["replay"] = {"scope": self._scope_kind(call.st.job), "source_call_id": source_call_id, "kind": kind}
        return self._finish(call, _End(reply, outcome))

    def _serve(self, call: _Call, st: _JobState, job: JobIdentity, admitted: dict, rkey: str,
               bypass: bool) -> GatewayReply:
        while True:                                # step 7
            lease = self.replay.begin(job, rkey, call_id=call.call_id, bypass=bypass)
            if lease.role == "hit":
                reply = GatewayReply(200, (("content-type", "application/json"), (wire.SOURCE_HEADER, "replay")),
                                     zero_usage(lease.stored.body))
                return self._reused(call, reply, lease.stored.body, "stored", lease.stored.source_call_id, "replayed")
            if lease.role == "leader":
                break
            while True:                            # follower
                reply = lease.wait(self.poll_s)
                if reply is not None:
                    if reply.status == 200:
                        return self._reused(call, _with_source(reply, "coalesced"), reply.body, "coalesced",
                                            lease.leader_call_id, "coalesced")
                    code = _refusal_code(reply)
                    call.rec["replay"] = {"scope": self._scope_kind(job), "source_call_id": lease.leader_call_id,
                                          "kind": "coalesced"}
                    return self._finish(call, _End(reply, "coalesced", code, "shared by the leader"))
                if lease.leader_done:
                    break                          # the leader left without sharing: begin again
                if self._stopped():
                    return self._refuse(call, "halted", "halted while following")
                if st.closed:
                    return self._refuse(call, "unknown_job", "job_closed")
        call.extra["lease_obj"] = lease
        with self._cond:
            st.active += 1                         # steps 8 to 11: close_job waits until the counters are updated
        try:
            end = self._lead(call, st, job, admitted)
            if not call.extra.get("committed"):    # step 10: store nothing; share what any caller would get
                lease.fail(end.reply, share=end.share and end.reply.status != 200)
            return self._finish(call, end)
        finally:
            lease.abandon()                        # a no-op once committed or failed
            with self._cond:
                st.active -= 1
                self._cond.notify_all()

    # ------------------------------------------------------------ steps 8 and 9

    def _acquire(self, st: _JobState) -> str | None:
        """None once a slot is held; 'halted' or 'unknown_job' when the wait ended without one."""
        while True:
            if self._stopped():
                return "halted"
            if st.closed:
                return "unknown_job"
            if self._sem.acquire(timeout=self.poll_s):
                return None

    def _lead(self, call: _Call, st: _JobState, job: JobIdentity, admitted: dict) -> _End:
        holding = False
        try:
            t0 = self.clock.monotonic()
            got = self._acquire(st)                # step 8
            call.rec["queue_s"] = round(max(0.0, self.clock.monotonic() - t0), 6)
            if got is not None:
                return self._wait_end(got, call)
            holding = True
            with self._cond:
                first_ok = self._ok_seq
            n = 1
            while True:                            # step 9
                result = self._attempt(call, st, job, admitted, n)
                if isinstance(result, _End):
                    return result
                disposition, status = result
                if n >= self.max_attempts:         # 9.7
                    return self._exhausted(call, job, disposition, first_ok)
                pause = backoff_s(disposition, status, self.manifest, self.rng)   # 9.6
                call.rec["attempts"][-1]["backoff_s"] = round(pause, 6)
                self._sem.release()
                holding = False
                self.clock.sleep(pause)
                got = self._acquire(st)
                if got is not None:
                    return self._wait_end(got, call)
                holding = True
                n += 1
        finally:
            if holding:
                self._sem.release()

    def _wait_end(self, got: str, call: _Call) -> _End:
        outcome = "failed" if call.rec["attempts"] else "refused"
        if got == "halted":
            return _End(wire.refusal_reply("halted"), outcome, "halted", "halted while queued", share=True)
        return _End(wire.refusal_reply("unknown_job"), outcome, "unknown_job", "job_closed", share=False)

    def _exhausted(self, call: _Call, job: JobIdentity, disposition: str, first_ok: int) -> _End:
        with self._cond:
            peer_ok = self._ok_seq > first_ok
        try:
            repeatable = self.ledger.note_exhausted(job, call.rec["request_sha256"], call.call_id, peer_ok=peer_ok)
        except Exception:                          # noqa: BLE001 - treated as an infrastructure failure
            repeatable = False
        if repeatable:
            call.extra["repeatable"] = True
            return _End(wire.refusal_reply("provider_rejected"), "failed", "provider_rejected",
                        "repeatable_provider_failure")
        return _End(wire.refusal_reply("provider_unavailable"), "failed", "provider_unavailable",
                    f"attempts exhausted: {disposition}")

    def _settle(self, call: _Call, att: dict, usage: object, *, rejection: str | None = None,
                reason: str = "") -> Settlement | None:
        """ledger.settle with the retries of 3.10; None (row left pending, ledger_fault set) when it cannot be done."""
        call.extra.setdefault("settle_tried", set()).add(att["ledger_id"])
        for i in range(SETTLE_RETRIES + 1):
            try:
                s = self.ledger.settle(att["ledger_id"], usage, rejection=rejection, reason=reason)
            except sqlite3.OperationalError:
                if i < SETTLE_RETRIES:
                    self.clock.sleep(SETTLE_RETRY_WAIT_S)
                    continue
                self._fault(call.call_id, "settle failed: ledger busy")
                return None
            except Exception as exc:               # noqa: BLE001
                self._fault(call.call_id, f"settle failed: {type(exc).__name__}")
                return None
            att["ledger_status"], att["spent_micro"] = s.status, s.spent_micro
            return s
        return None                                # pragma: no cover

    def _attempt(self, call: _Call, st: _JobState, job: JobIdentity, admitted: dict, n: int):
        """Steps 9.1 to 9.5 for provider attempt n: an _End, or (disposition, http_status) to retry."""
        forwarded = "failed" if call.rec["attempts"] else "refused"
        if self._aborted or self._ledger_fault is not None:
            return _End(wire.refusal_reply("halted"), forwarded, "halted", "halted before the attempt")
        try:
            self.circuit.enter(max_wait_s=self.circuit_wait_s)          # 9.1
        except TimeoutError:
            return _End(wire.refusal_reply("gateway_internal"), forwarded, "gateway_internal",
                        "circuit wait exceeded", share=False)
        box = ["neutral"]                          # the circuit outcome; set only after the provider call returned
        try:
            rec = call.rec
            phase = wire.ledger_phase(job, n, prior_charged=st.prior_charged)
            with self._cond:                       # an abort during circuit.enter: no attempt; else drain counts it
                if self._aborted or self._ledger_fault is not None:
                    return _End(wire.refusal_reply("halted"), forwarded, "halted", "halted before the attempt")
                self._attempts_open += 1
            try:
                try:                               # 9.2
                    lid = self.ledger.reserve(call_id=call.call_id, n=n, job=job, phase=phase,
                                              model=admitted["model"], input_limit=rec["input_limit"],
                                              output_limit=rec["output_limit"])
                except BudgetError as exc:
                    return self._budget_end(call, exc, forwarded)
                att = {"n": n, "ledger_id": lid, "ledger_phase": phase, "started_utc": util.utc_iso(self.clock.now()),
                       "first_byte_utc": None, "ended_utc": None, "http_status": None, "disposition": None,
                       "done": False, "error": None, "response_headers": {}, "sse_file": f"{call.call_id}.a{n}.sse",
                       "sse_bytes": 0, "reserved_micro": reservation_micro(self.ledger.prices, rec["input_limit"],
                                                                           rec["output_limit"]),
                       "spent_micro": None, "ledger_status": "pending", "backoff_s": 0.0}
                rec["attempts"].append(att)
                try:
                    return self._provider_step(call, att, admitted, n, box)
                finally:
                    if lid not in call.extra.get("settle_tried", ()):   # G2: an unexpected fault still settles
                        self._settle(call, att, None, reason="internal fault")
            finally:
                with self._cond:
                    self._attempts_open -= 1
                    self._cond.notify_all()
        finally:
            try:
                self.circuit.finish(box[0])
            except Exception:                      # noqa: BLE001 - finish is called exactly once either way
                call.extra["circuit_fault"] = True

    def _budget_end(self, call: _Call, exc: BudgetError, outcome: str) -> _End:
        if exc.kind in ("total", "cap"):
            self._halt("budget:total" if exc.kind == "total" else f"budget:{exc.phase}", str(exc), call.call_id)
            return _End(wire.refusal_reply("budget_exhausted"), outcome, "budget_exhausted", str(exc))
        if exc.kind in ("halted", "unreconciled"):
            return _End(wire.refusal_reply("halted"), outcome, "halted", str(exc))
        if exc.kind == "ceiling":
            return _End(wire.refusal_reply("job_request_ceiling"), outcome, "job_request_ceiling",
                        f"ceiling: {exc.detail}", share=False)
        return _End(wire.refusal_reply("gateway_internal"), outcome, "gateway_internal", str(exc), share=False)

    def _provider_step(self, call: _Call, att: dict, admitted: dict, n: int, box: list):
        """9.3 to 9.5. box[0] carries the circuit outcome back to the caller's finally."""
        forward = dict(admitted)
        forward.update(stream=True, stream_options={"include_usage": True})
        sse_path = call.path.parent / att["sse_file"]
        t0 = self.clock.now()
        try:
            sse_path.parent.mkdir(parents=True, exist_ok=True)
            with open(sse_path, "wb") as sink:
                result: ProviderResult = self.provider.complete(forward, sse_sink=sink, deadline_s=self.deadline_s)
        except Exception as exc:                   # noqa: BLE001 - booked at its reservation; never a hang
            att["ended_utc"] = util.utc_iso(self.clock.now())
            att["error"] = {"kind": "connect", "http_status": None, "code": None,
                            "body_excerpt": f"provider client raised {type(exc).__name__}"}
            self._settle(call, att, None, reason="provider client raised")
            return _End(wire.refusal_reply("gateway_internal"), "failed", "gateway_internal",
                        f"provider client raised {type(exc).__name__}", share=False)
        disposition = classify(result, terminal_429_patterns=self.terminal_429)
        box[0] = circuit_outcome(disposition)
        err = result.error
        att.update(http_status=result.http_status, disposition=disposition, done=bool(result.done),
                   ended_utc=util.utc_iso(self.clock.now()), response_headers=dict(result.headers),
                   first_byte_utc=None if result.first_byte_s is None else util.utc_iso(t0 + result.first_byte_s),
                   error=None if err is None else {"kind": err.kind, "http_status": err.http_status, "code": err.code,
                                                   "body_excerpt": err.body_excerpt})
        try:
            att["sse_bytes"] = sse_path.stat().st_size
        except OSError:
            pass
        merged = result.merged or {}
        if disposition == "ok":
            return self._deliver(call, att, merged)
        basis = settle_basis(result, disposition)
        reason = f"http {result.http_status}" if err is None else f"{err.kind} {result.http_status}"
        if basis == "usage":
            s = self._settle(call, att, merged.get("usage"), reason=disposition)
        elif basis == "rejected":
            s = self._settle(call, att, None, rejection=disposition, reason=reason)
        else:
            s = self._settle(call, att, None, reason=disposition)
        if s is None:
            return _End(wire.refusal_reply("gateway_internal"), "failed", "gateway_internal", "ledger fault",
                        share=False)
        text = (err.body_excerpt if err is not None else "") or disposition
        if disposition == "context_limit":
            return _End(wire.refusal_reply("context_length_exceeded"), "provider_rejected", "context_length_exceeded",
                        text)
        if disposition == "provider_rejected":
            return _End(wire.refusal_reply("provider_rejected"), "provider_rejected", "provider_rejected", text)
        if disposition in ("config_fault", "quota_exhausted"):
            self._halt("provider_config" if disposition == "config_fault" else "provider_quota", text, call.call_id)
            return _End(wire.refusal_reply("halted"), "failed", "halted", text)
        return disposition, result.http_status

    def _deliver(self, call: _Call, att: dict, merged: dict) -> _End:
        """9.4: identity gate, merged body, commit (store first), settle."""
        body = to_completion_body(merged, request_model=self.wire_model, created=int(self.clock.now()))
        facts = _body_facts(body)
        response_model = merged.get("model")
        message = body["choices"][0]["message"]
        has_output = bool(message.get("content") or message.get("reasoning") or body["choices"][0]["finish_reason"])
        problem = model_identity_problem(response_model, has_output, self.expected_model)
        facts["flags"]["model_mismatch"] = response_model is not None and response_model != (
            self.expected_model or self.wire_model)
        call.rec.update(facts)
        call.rec["response_model"] = response_model
        if problem is not None:
            self._settle(call, att, merged.get("usage"), reason="model_identity")
            self._halt("model_identity", problem, call.call_id)
            return _End(wire.refusal_reply("halted"), "failed", "halted", problem)
        reply = GatewayReply(200, (("content-type", "application/json"), (wire.SOURCE_HEADER, "provider")), _dump(body))
        usage = body.get("usage") if isinstance(body.get("usage"), Mapping) else {}
        lease = call.extra.get("lease_obj")
        try:
            lease.commit(reply, job_id=call.st.job.job_id, finish_reason=facts["finish_reason"],
                         prompt_tokens=usage.get("prompt_tokens") if _is_int(usage.get("prompt_tokens")) else None,
                         completion_tokens=usage.get("completion_tokens")
                         if _is_int(usage.get("completion_tokens")) else None)
        except Exception:                          # noqa: BLE001 - not stored: a rerun pays again, the reply stands
            pass
        call.extra["committed"] = True
        with self._cond:
            self._ok_seq += 1
        self._settle(call, att, merged.get("usage"))
        call.rec["response"] = body
        return _End(reply, "completed")


def _refusal_code(reply: GatewayReply) -> str | None:
    try:
        code = json.loads(bytes(reply.body).decode("utf-8"))["error"]["code"]
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return None
    return code if code in wire.REFUSALS else None
