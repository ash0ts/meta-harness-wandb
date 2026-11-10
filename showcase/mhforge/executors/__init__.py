# SPDX-License-Identifier: MIT
"""Executor protocol and the types every executor shares (spec 3.16): JobSpec, JobResult, ImportResult, the pure
attempt classification, the selfcheck verdict and the sandbox price."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Mapping, Protocol, Sequence

from .. import util
from ..wire import JobIdentity

__all__ = [
    "JobSpec", "JobResult", "ImportResult", "Executor", "TAINT_CODES", "classify_attempt", "check_selfcheck",
    "sandbox_cost_micro", "merge_refusal_codes", "positive_codes", "STDERR_TAIL_CHARS",
]

STDERR_TAIL_CHARS = 2000


@dataclass(frozen=True)
class JobSpec:
    identity: JobIdentity
    inner_argv: tuple[str, ...]                    # with {out}/ and {in}/ placeholders, and {api_base} on Route 1
    route: int
    timeout_s: int
    inputs: tuple[tuple[str, Path], ...]           # (name under {in}, host source)
    expect_outputs: tuple[str, ...]                # ("val.json",) or ("test.json",)
    capture: bool
    bundle_tar: Path
    bundle_sha256: str
    expect_freeze_sha256: str | None = None        # the qualified environment's freeze hash, for the job file's "expect"


@dataclass
class JobResult:
    returncode: int | None
    timed_out: bool
    exit_file: dict | None                         # the exit file, or None when absent
    relay_end: str | None                          # exit | eof | error | stream_budget | deadline; None under the loopback ingress
    kill_reason: str | None                        # None | "halt" | "refusal_flood"
    started_utc: str
    ended_utc: str
    outputs: dict[str, str]                        # name -> sha256 of files under attempt_dir/out
    gateway: dict                                  # from HandlesRequests.close_job
    refusal_codes: dict[str, int]                  # merge_refusal_codes(gateway codes, exit file, flood refusals)
    stderr_tail: str                               # at most STDERR_TAIL_CHARS characters
    host_sleep_s: float                            # total host sleep the wait loop saw (0.0 when none)
    facts: dict                                    # executor-specific; written to executor.json


@dataclass(frozen=True)
class ImportResult:
    ok: bool
    returncode: int | None
    timed_out: bool
    stdout_tail: str
    stderr_tail: str
    reason: str | None = None                      # None | "foreign_class" (exit 3) | "no_class" (exit 4)


class Executor(Protocol):
    name: str
    isolated: bool

    def run(self, spec: JobSpec, attempt_dir: Path, on_handle: Callable[[dict], None]) -> JobResult: ...

    def import_check(self, names: Sequence[str], bundle_tar: Path, work_root: Path, on_handle: Callable[[dict], None], *,
                     timeouts_s: Sequence[float]) -> dict[str, ImportResult]: ...

    def cleanup_orphans(self, handles: Sequence[dict]) -> int: ...

    def close(self) -> None: ...


TAINT_CODES = frozenset({"halted", "budget_exhausted", "provider_unavailable", "gateway_internal", "relay_unavailable"})


def _count(value: object) -> int:
    """A positive int counts as itself; 0, a negative int, None and False count as nothing. Any other value (a
    malformed count such as "1", 1.0 or True) counts as 1, so a malformed tainted code can never let an attempt pass."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value > 0 else 0
    return 0 if value is None or value is False else 1


def positive_codes(codes: Mapping[str, object] | None) -> dict[str, int]:
    """The codes with a positive integer count (a zero count, as in an exit file's local_refusals, is no refusal)."""
    return {str(k): _count(v) for k, v in sorted((codes or {}).items()) if _count(v)}


def merge_refusal_codes(gateway_codes: Mapping[str, object] | None, exit_file: Mapping | None,
                        flood_refusals: int = 0) -> dict[str, int]:
    """JobResult.refusal_codes: the gateway's codes + exit_file["local_refusals"] + {"relay_overloaded": flood_refusals}.
    Only positive counts are kept."""
    merged: dict[str, int] = {}
    local = exit_file.get("local_refusals") if isinstance(exit_file, Mapping) else None
    for source in (gateway_codes, local if isinstance(local, Mapping) else None, {"relay_overloaded": flood_refusals}):
        for code, n in positive_codes(source).items():
            merged[code] = merged.get(code, 0) + n
    return dict(sorted(merged.items()))


_FAILURE_PATTERNS = (("dependency_missing", ("ModuleNotFoundError", "No module named")),
                     ("import_error", ("ImportError",)),
                     ("harmony_vocab", ("HarmonyError",)),
                     ("worker_exception", ("Traceback",)))


def classify_attempt(result: JobResult, *, result_usable: bool, halted: bool) -> tuple[str, bool, str | None]:
    """Pure: (outcome, infra, failure_class) by the first matching row of the table in 3.16."""
    codes = positive_codes(result.refusal_codes)
    taint = set(codes) & TAINT_CODES
    exit_file = result.exit_file if isinstance(result.exit_file, Mapping) else {}
    present = isinstance(result.exit_file, Mapping)
    reason = exit_file.get("reason")
    relay_gap = ((result.relay_end is not None and result.relay_end not in ("exit", "stream_budget"))
                 or reason == "uplink_lost"
                 or (reason == "exit" and _count(exit_file.get("unanswered_at_exit")) > 0))
    timed_out = bool(result.timed_out) or reason == "timeout"
    # 1
    if (halted and taint & {"halted", "budget_exhausted"}) or result.kill_reason == "halt":
        return "halted", True, None
    # 2
    if result.relay_end == "stream_budget":
        return "request_ceiling", False, "stream_budget"
    # 3
    if relay_gap:
        return "relay_lost", True, None
    # 4
    if taint:
        return "provider_unavailable", True, None
    # 5
    if "job_request_ceiling" in codes:
        return "request_ceiling", False, "request_ceiling"
    # 6
    if "relay_overloaded" in codes:
        return "request_ceiling", False, "relay_overloaded"
    if result.kill_reason == "refusal_flood":
        return "request_ceiling", False, "refusal_flood"
    # 7
    if result.returncode == 0 and present and reason == "exit" and result_usable:
        return "ok", False, None
    # 8
    if float(result.host_sleep_s or 0.0) > 0 and (timed_out or not present):
        return "interrupted", True, "host_sleep"
    # 9
    if timed_out:
        return "timeout", False, None
    # 10
    if not present:
        return "executor_error", True, None
    # 11
    if result.returncode == 0:
        return "no_result", False, None
    # 12
    if "context_length_exceeded" in codes:
        return "worker_error", False, "context_limit"
    tail = result.stderr_tail or ""
    for failure_class, needles in _FAILURE_PATTERNS:
        if any(n in tail for n in needles):
            return "worker_error", False, failure_class
    return "worker_error", False, "unknown"


def check_selfcheck(report: Mapping, *, isolated: bool) -> tuple[list[str], list[str]]:
    """Pure: (isolation problems, environment problems) of a selfcheck report. Isolation problems are looked for only
    when isolated. The recorded probes (egress.probes) are never a problem."""
    if not isinstance(report, Mapping):
        return (["report: not an object"] if isolated else []), ["report: not an object"]
    isolation: list[str] = []
    if isolated:
        names = report.get("credential_names")
        if not isinstance(names, list):
            isolation.append("credential_names: missing")
        elif names:
            isolation.append("credential_names: " + ",".join(sorted(str(n) for n in names)))
        if report.get("wandb_api_key") == "set":
            isolation.append("wandb_api_key: set")
        egress = report.get("egress")
        if not isinstance(egress, Mapping):
            isolation.append("egress: missing")
        else:
            for gate in ("tcp", "dns"):
                if egress.get(gate) == "open":
                    isolation.append(f"egress.{gate}: open")
    environment: list[str] = []
    if "vocab_file_present" not in report or report.get("vocab_file_present") is False:
        environment.append("vocab_file_present: false")
    if report.get("cache_writable") is not True:
        environment.append("cache_writable: false")
    problems = report.get("expect_problems")
    if not isinstance(problems, list):
        environment.append("expect_problems: missing")
    else:
        environment.extend(str(p) for p in problems)
    return isolation, environment


_CPU_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)(m?)")
_MEM_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)(Ki|Mi|Gi|Ti|K|M|G|T)?")
_MEM_FACTOR = {None: Decimal(1), "Ki": Decimal(1024), "Mi": Decimal(1024) ** 2, "Gi": Decimal(1024) ** 3,
               "Ti": Decimal(1024) ** 4, "K": Decimal(10) ** 3, "M": Decimal(10) ** 6, "G": Decimal(10) ** 9,
               "T": Decimal(10) ** 12}


def _cpu(cpu: str) -> Decimal:
    m = _CPU_RE.fullmatch(str(cpu).strip())
    if not m:
        raise ValueError(f"not a CPU quantity: {str(cpu)[:20]!r}")
    value = Decimal(m.group(1))
    return value / 1000 if m.group(2) else value


def _gib(memory: str) -> Decimal:
    m = _MEM_RE.fullmatch(str(memory).strip())
    if not m:
        raise ValueError(f"not a memory quantity: {str(memory)[:20]!r}")
    return Decimal(m.group(1)) * _MEM_FACTOR[m.group(2)] / Decimal(1024) ** 3


def sandbox_cost_micro(billable_ms: int, cpu: str, memory: str, manifest: dict) -> int:
    """ceil(ms / 3.6e6 * (cpu * sandbox.price_cpu_hour + GiB(memory) * sandbox.price_gib_hour)) in micro-dollars,
    computed in decimals (no float arithmetic on money). cpu: "2", "0.5" or "500m"; memory: "4Gi", "512Mi", bytes."""
    if isinstance(billable_ms, bool) or not isinstance(billable_ms, int) or billable_ms < 0:
        raise ValueError("billable_ms must be a non-negative integer")
    try:
        cpu_hour = Decimal(str(util.lookup(manifest, "sandbox.price_cpu_hour")))
        gib_hour = Decimal(str(util.lookup(manifest, "sandbox.price_gib_hour")))
    except InvalidOperation:
        raise ValueError("sandbox prices must be decimal strings") from None
    usd_per_hour = _cpu(cpu) * cpu_hour + _gib(memory) * gib_hour
    micro = Decimal(billable_ms) * usd_per_hour * 1_000_000 / Decimal(3_600_000)
    return int(micro.to_integral_value(rounding=ROUND_CEILING))
