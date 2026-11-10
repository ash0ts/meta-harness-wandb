# SPDX-License-Identifier: MIT
"""Shared wire-level types and constants (spec 3.2 and LD2): job identity, gateway reply, the closed refusal table,
request envelopes, bounded ids and the HandlesRequests protocol."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Mapping, Protocol, runtime_checkable

JOB_PHASES = ("qualification", "calibration", "baseline", "search", "test", "controls")
LEDGER_PHASES = JOB_PHASES + ("recovery",)        # seven; the CHECK constraint and the phase_totals rows of 2.10, the caps of 2.3
JOB_STATUSES = ("succeeded", "failed", "skipped_usable", "skipped_no_memory", "halted")
ATTEMPT_OUTCOMES = ("ok", "worker_error", "no_result", "timeout", "request_ceiling", "relay_lost", "provider_unavailable",
                    "executor_error", "env_setup_failed", "halted", "interrupted")
INFRA_OUTCOMES = frozenset({"relay_lost", "provider_unavailable", "executor_error", "env_setup_failed", "halted", "interrupted"})
REUSE_SCOPES = ("search", "job", "qualification", "repeat")      # replay.scope of a call record (2.5)
SESSION_OUTCOMES = ("ok", "no_candidates", "timeout", "failed", "spend_limit", "limit_window", "auth", "transient",
                    "model_mismatch", "sandbox_unavailable", "settings_ignored", "cli_missing", "interrupted")     # 2.11
CONTENT_FIELDS = ("span_prompt", "span_reply", "span_reasoning", "tool_result")                                   # 2.3
NEVER_PUBLISH_TEXT = ("LawBench",)                 # datasets whose content rules must stay "hash" (2.3 rule 16)
CLEARANCE_GATED = {"USPTO": "decisions.uspto_text_cleared"}

# LD2: round-two constant lists
CAMPAIGN_KINDS = ("preflight", "measured")                       # 2.3 campaign_kind
METERS = ("sandbox_seconds", "lens_bytes", "lens_spans", "proposer_list_price",
          "wandb_upload_bytes", "eval_tables")                    # 2.10 meter_events.meter
LENS_BUCKETS = ("main", "preflight", "recovery")                 # 2.10 meter_events.bucket for lens_bytes and lens_spans
FAILURE_CLASSES = ("import_error", "dependency_missing", "context_limit", "request_ceiling", "stream_budget",
                   "relay_overloaded", "refusal_flood", "harmony_vocab", "worker_exception", "host_sleep",
                   "unknown")                                     # 2.6 failure_class (or None)

SPLITS = ("val", "test")
CAMPAIGN_RE = re.compile(r"[a-z0-9][a-z0-9-]{2,39}")          # always used with .fullmatch
CANDIDATE_RE = re.compile(r"[a-z][a-z0-9_]{0,47}")            # always used with .fullmatch; "x\n" must be rejected
DATASET_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,47}")       # a dataset name as it appears in a job key
CREDENTIAL_NAME_PATTERN = r"(?:^|_)(API_?KEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)(?:_|$)"   # re.search, upper-cased name
CREDENTIAL_NAME_EXEMPT = ("OPENROUTER_API_KEY", "TIKTOKEN_RS_CACHE_DIR")   # exact names the in-executor check ignores


def bounded_id(text: str, limit: int = 128) -> str:
    """text when len(text) <= limit; else text[:limit - 9] + '-' + sha256(text)[:8]."""
    if limit < 10:
        raise ValueError("limit must be at least 10")
    if len(text) <= limit:
        return text
    return text[:limit - 9] + "-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class JobIdentity:                                 # the one identity type; used by ledger, gateway, relay, executors
    campaign: str
    split: str
    candidate: str
    dataset: str
    attempt: int
    phase: str                                     # one of JOB_PHASES, never "recovery"
    iteration: int | None                          # upstream iteration for phase "search", else None
    repeat: int | None = None                      # 2..99: a noise repeat (3.20); None for an ordinary job
    sample: int | None = None                      # 1..99: a sample of the deferred sampling control; None otherwise

    @property
    def job_key(self) -> str:
        key = f"{self.split}.{self.candidate}.{self.dataset}"
        if self.phase == "qualification":
            key = "qual." + key
        if self.repeat is not None:
            key += f".r{self.repeat}"
        if self.sample is not None:
            key += f".s{self.sample}"
        return key

    @property
    def job_id(self) -> str:
        return f"{self.job_key}.a{self.attempt}"

    def to_dict(self) -> dict:
        return {"campaign": self.campaign, "split": self.split, "candidate": self.candidate, "dataset": self.dataset,
                "attempt": self.attempt, "phase": self.phase, "iteration": self.iteration, "repeat": self.repeat,
                "sample": self.sample, "job_key": self.job_key, "job_id": self.job_id}

    def problems(self) -> list[str]:
        """Every field rule this identity breaks ([] when valid)."""
        out = []
        if not isinstance(self.campaign, str) or not CAMPAIGN_RE.fullmatch(self.campaign):
            out.append("campaign")
        if self.split not in SPLITS:
            out.append("split")
        if not isinstance(self.candidate, str) or not CANDIDATE_RE.fullmatch(self.candidate):
            out.append("candidate")
        if not isinstance(self.dataset, str) or not DATASET_RE.fullmatch(self.dataset):
            out.append("dataset")
        if not _is_int(self.attempt) or self.attempt < 1:
            out.append("attempt")
        if self.phase not in JOB_PHASES:
            out.append("phase")
        if self.phase == "search":
            if not _is_int(self.iteration) or self.iteration < 1:
                out.append("iteration")
        elif self.iteration is not None:
            out.append("iteration")
        if self.repeat is not None and (not _is_int(self.repeat) or not 2 <= self.repeat <= 99):
            out.append("repeat")
        if self.sample is not None and (not _is_int(self.sample) or not 1 <= self.sample <= 99):
            out.append("sample")
        return out

    def validate(self) -> "JobIdentity":
        bad = self.problems()
        if bad:
            raise ValueError("invalid job identity field(s): " + ", ".join(bad))
        return self

    @classmethod
    def from_dict(cls, d: Mapping) -> "JobIdentity":
        """Validates every field; ValueError otherwise. job_key and job_id, when present, must match."""
        if not isinstance(d, Mapping):
            raise ValueError("job identity must be a mapping")
        required = ("campaign", "split", "candidate", "dataset", "attempt", "phase", "iteration")
        missing = [k for k in required if k not in d]
        if missing:
            raise ValueError("job identity lacks: " + ", ".join(missing))
        ident = cls(campaign=d["campaign"], split=d["split"], candidate=d["candidate"], dataset=d["dataset"],
                    attempt=d["attempt"], phase=d["phase"], iteration=d["iteration"],
                    repeat=d.get("repeat"), sample=d.get("sample")).validate()
        for key in ("job_key", "job_id"):
            if key in d and d[key] != getattr(ident, key):
                raise ValueError(f"job identity {key} does not match its fields")
        return ident


def ledger_phase(job: JobIdentity, provider_attempt: int, *, prior_charged: bool) -> str:
    """'qualification' stays; else 'recovery' for a retry by the gateway (provider_attempt > 1) or for a later job attempt
    when an earlier attempt of the job_key was charged; else the job's own phase."""
    if job.phase == "qualification":
        return "qualification"
    if provider_attempt > 1 or (job.attempt > 1 and prior_charged):
        return "recovery"
    return job.phase


@dataclass(frozen=True)
class GatewayReply:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes


RETRY_TRIGGER_STRINGS = ("429", "rate limit", "too many requests", "retry in", "timed out", "timeout")   # llm.py L98-L108
SOURCE_HEADER = "x-mhforge-source"                 # provider | replay | coalesced | gateway
RELAY_API_KEY_PLACEHOLDER = "mhf-relay"

REFUSALS: dict[str, tuple[int, str]] = {
    "halted": (402, "mhforge gateway: campaign halted"),
    "budget_exhausted": (402, "mhforge gateway: budget exhausted"),
    "job_request_ceiling": (403, "mhforge gateway: job request ceiling reached"),
    "unknown_job": (403, "mhforge gateway: unknown job"),
    "not_allowed": (403, "mhforge gateway: only chat completions are served"),
    "invalid_request": (403, "mhforge gateway: request not accepted"),
    "model_not_allowed": (403, "mhforge gateway: model not in manifest"),
    "request_too_large": (403, "mhforge gateway: request exceeds the size bound"),
    "provider_rejected": (403, "mhforge gateway: provider rejected the request"),
    "context_length_exceeded": (403, "mhforge gateway: provider rejected the prompt length"),
    "provider_unavailable": (403, "mhforge gateway: provider unavailable after gateway attempts"),
    "relay_unavailable": (403, "mhforge relay: host link unavailable"),
    "relay_overloaded": (403, "mhforge relay: request queue full"),
    "gateway_internal": (403, "mhforge gateway: internal fault"),
}


def refusal_body(code: str) -> bytes:
    status, message = REFUSALS[code]
    return json.dumps({"error": {"message": message, "type": "mhforge_refusal", "code": code}},
                      separators=(",", ":")).encode("ascii")


def refusal_reply(code: str) -> GatewayReply:
    status, _ = REFUSALS[code]
    return GatewayReply(status=status,
                        headers=(("content-type", "application/json"), ("x-should-retry", "false"),
                                 (SOURCE_HEADER, "gateway")),
                        body=refusal_body(code))


_METHOD_RE = re.compile(r"[A-Z]{1,16}")
_PATH_RE = re.compile(r"/[\x21-\x7e]{0,4095}")


def make_envelope(method: str, path: str, body: bytes) -> bytes:
    """(method + ' ' + path + '\\n').encode('ascii') + body."""
    if not _METHOD_RE.fullmatch(method) or not _PATH_RE.fullmatch(path):
        raise ValueError("bad envelope method or path")
    return (method + " " + path + "\n").encode("ascii") + bytes(body)


def parse_envelope(request: bytes) -> tuple[str, str, bytes]:
    """(method, path, body). ValueError on a malformed first line."""
    newline = request.find(b"\n")
    if newline < 0:
        raise ValueError("envelope has no first line")
    try:
        first = request[:newline].decode("ascii")
    except UnicodeDecodeError:
        raise ValueError("envelope first line is not ASCII") from None
    method, sep, path = first.partition(" ")
    if not sep or not _METHOD_RE.fullmatch(method) or not _PATH_RE.fullmatch(path):
        raise ValueError("malformed envelope first line")
    return method, path, request[newline + 1:]


@runtime_checkable
class HandlesRequests(Protocol):                   # what ingresses and executors need from the gateway
    def register_job(self, job: JobIdentity, *, token: str | None = None) -> None: ...
    def close_job(self, job_id: str, *, drain_timeout_s: float | None = None) -> dict: ...
    def handle(self, job: JobIdentity | None, request: bytes, *, ingress: str = "loopback",
               no_replay: bool = False) -> GatewayReply: ...
    def halted(self) -> object | None: ...         # the halt state, or None; executors poll it once a second
    def kill_requested(self, job_id: str) -> str | None: ...   # "refusal_flood" when the worker should be killed (3.10)
