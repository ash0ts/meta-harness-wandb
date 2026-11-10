# SPDX-License-Identifier: MIT
"""Exception taxonomy, halt and stop reasons, exit codes (spec 3.1).

Every exception takes its fields as constructor keyword arguments and carries a one-line
message in ``args[0]``.
"""

from __future__ import annotations

HALT_REASONS = frozenset({
    "budget:qualification", "budget:calibration", "budget:baseline", "budget:search", "budget:test", "budget:controls",
    "budget:recovery", "budget:total", "budget:sandbox", "budget:proposer", "usage_contradiction", "ledger_fault",
    "model_identity", "provider_config", "provider_quota", "isolation", "cleanup_unverified", "operator"})
STOP_REASONS = HALT_REASONS | frozenset({
    "proposer_spend_limit", "proposer_limit_wait", "proposer_auth", "proposer_transient", "proposer_model", "proposer_sandbox",
    "proposer_settings", "proposer_integrity", "proposer_cli", "proposer_environment", "infrastructure",
    "baseline_incomplete", "qualification_failed", "forecast_exceeds_budget", "upstream_contract", "internal_error",
    "interrupted"})

EXIT_OK, EXIT_ERROR, EXIT_STOPPED, EXIT_REFUSED, EXIT_LOCKED, EXIT_SMOKE_FAILED = 0, 1, 2, 3, 4, 5

BUDGET_KINDS = frozenset({"invalid", "halted", "unreconciled", "ceiling", "total", "cap", "settled"})
CEILING_DETAILS = frozenset({"requests", "attempts", "job_usd"})
EXECUTOR_KINDS = frozenset({"create", "quota", "upload", "exec", "selfcheck", "credential_visible", "egress_open",
                            "image_mismatch", "auth", "env_setup", "billing", "permission"})
NON_RETRYABLE_EXECUTOR_KINDS = frozenset({"image_mismatch", "auth", "billing", "permission"})
TRANSCRIPT_KINDS = frozenset({"version", "shape"})

_MISSING = object()


def _one_line(text: object) -> str:
    return " ".join(str(text).split())


def _copy_default(value):
    if isinstance(value, list):
        return list(value)
    if isinstance(value, dict):
        return dict(value)
    return value


class _Fields:
    """Shared keyword-field machinery for both Exception and BaseException subclasses."""

    FIELDS: dict[str, object] = {}

    def _init_fields(self, message, kwargs) -> str:
        fields = {}
        for name, default in type(self).FIELDS.items():
            if name in kwargs:
                fields[name] = kwargs.pop(name)
            else:
                fields[name] = _copy_default(default)
        if kwargs:
            raise TypeError(f"{type(self).__name__} got unexpected fields: {sorted(kwargs)}")
        for name, value in fields.items():
            setattr(self, name, value)
        self._validate()
        if message is None or message == "":
            message = self._default_message()
        return _one_line(message)

    def _validate(self) -> None:
        pass

    def _default_message(self) -> str:
        parts = [f"{k}={getattr(self, k)!r}" for k in type(self).FIELDS]
        return type(self).__name__ + (": " + ", ".join(parts) if parts else "")

    @property
    def fields(self) -> dict:
        return {k: getattr(self, k) for k in type(self).FIELDS}

    def __reduce__(self):
        return (_rebuild, (type(self), self.args[0] if self.args else "", self.fields))


def _rebuild(cls, message, fields):
    return cls(message, **fields)


class MhforgeError(_Fields, Exception):
    def __init__(self, message: str | None = None, **kwargs) -> None:
        Exception.__init__(self, self._init_fields(message, kwargs))


class ManifestError(MhforgeError):                 # invalid, unfrozen, or differs from the frozen copy
    FIELDS = {"problems": []}

    def _default_message(self) -> str:
        n = len(self.problems)
        return f"manifest invalid ({n} problem{'s' if n != 1 else ''})" + (f": {self.problems[0]}" if n else "")


class LiveGateError(MhforgeError):                 # a live command refused
    FIELDS = {"blockers": []}

    def _default_message(self) -> str:
        n = len(self.blockers)
        first = f": {self.blockers[0][0]}: {self.blockers[0][1]}" if n else ""
        return f"live command refused ({n} blocker{'s' if n != 1 else ''}){first}"


class StateLockError(MhforgeError):                # another process holds the lock
    FIELDS = {"holder": {}}

    def _default_message(self) -> str:
        return f"state is locked by pid {self.holder.get('pid')} ({self.holder.get('command')})"


class StateError(MhforgeError): ...                # a state file is malformed


class UpstreamContractError(MhforgeError): ...     # a hook this design relies on is not where it was


class WorkspaceError(MhforgeError): ...


class BudgetError(MhforgeError):
    FIELDS = {"kind": "invalid", "phase": None, "detail": None}

    def _validate(self) -> None:
        if self.kind not in BUDGET_KINDS:
            raise ValueError(f"unknown BudgetError kind {self.kind!r}")
        if self.kind == "ceiling" and self.detail not in CEILING_DETAILS:
            raise ValueError(f"BudgetError kind 'ceiling' needs detail in {sorted(CEILING_DETAILS)}")

    def _default_message(self) -> str:
        tail = "".join(f" {k}={v}" for k, v in (("phase", self.phase), ("detail", self.detail)) if v is not None)
        return f"budget refused: {self.kind}{tail}"


class MeterCapExceeded(MhforgeError):
    FIELDS = {"meter": "", "bucket": None}

    def _default_message(self) -> str:
        return f"meter cap exceeded: {self.meter}" + (f" (bucket {self.bucket})" if self.bucket else "")


class ProviderConfigFault(MhforgeError): ...       # missing key, wrong base URL


class RelayProtocolError(MhforgeError): ...


class ChannelClosed(MhforgeError): ...


class ExecutorError(MhforgeError):
    FIELDS = {"kind": "exec", "retryable": _MISSING}

    def _validate(self) -> None:
        if self.kind not in EXECUTOR_KINDS:
            raise ValueError(f"unknown ExecutorError kind {self.kind!r}")
        if self.retryable is _MISSING:
            self.retryable = self.kind not in NON_RETRYABLE_EXECUTOR_KINDS
        elif self.kind in NON_RETRYABLE_EXECUTOR_KINDS and self.retryable:
            raise ValueError(f"ExecutorError kind {self.kind!r} is never retryable")
        self.retryable = bool(self.retryable)

    def _default_message(self) -> str:
        return f"executor error: {self.kind}" + ("" if self.retryable else " (not retryable)")


class PlanError(MhforgeError):
    FIELDS = {"problems": []}

    def _default_message(self) -> str:
        return f"plan invalid ({len(self.problems)} problems)" + (f": {self.problems[0]}" if self.problems else "")


class ExportError(MhforgeError): ...


class SecretFound(ExportError):
    FIELDS = {"findings": []}                      # scan.Finding; never the matched text

    def _default_message(self) -> str:
        pats = sorted({getattr(f, "pattern", "?") for f in self.findings})
        return f"scan found {len(self.findings)} finding(s): {', '.join(pats)}"


class AdapterUnavailable(ExportError): ...         # an SDK is not importable, or `weave` is importable (3.25)


class TranscriptError(MhforgeError):               # the transcript converter refuses its input (3.21)
    FIELDS = {"kind": "shape"}

    def _validate(self) -> None:
        if self.kind not in TRANSCRIPT_KINDS:
            raise ValueError(f"unknown TranscriptError kind {self.kind!r}")


class SmokeFailed(MhforgeError):
    FIELDS = {"invariants": []}

    def _default_message(self) -> str:
        return f"offline smoke failed: {', '.join(str(i) for i in self.invariants) or 'unknown'}"


class CampaignStop(_Fields, BaseException):        # BaseException on purpose: no upstream `except Exception` can swallow it
    FIELDS = {"reason": "", "detail": "", "phase": None}

    def __init__(self, message: str | None = None, **kwargs) -> None:
        BaseException.__init__(self, self._init_fields(message, kwargs))

    def _validate(self) -> None:
        if self.reason not in STOP_REASONS:
            raise ValueError(f"unknown stop reason {self.reason!r}")

    def _default_message(self) -> str:
        return f"campaign stopped: {self.reason}" + (f": {self.detail}" if self.detail else "")


def exit_code_for(exc: BaseException | None) -> int:
    """The exit code of 3.1 for an exception that reached the command line (None = success)."""
    if exc is None:
        return EXIT_OK
    if isinstance(exc, CampaignStop):
        return EXIT_STOPPED
    if isinstance(exc, (LiveGateError, ManifestError)):
        return EXIT_REFUSED
    if isinstance(exc, StateLockError):
        return EXIT_LOCKED
    if isinstance(exc, SmokeFailed):
        return EXIT_SMOKE_FAILED
    return EXIT_ERROR
