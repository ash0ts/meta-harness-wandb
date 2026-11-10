# SPDX-License-Identifier: MIT
"""Path arithmetic for the state directory of 2.4 (spec 3.2). No I/O except Path.resolve() in resolve_state."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import StateError
from .util import safe_component
from .wire import CAMPAIGN_RE


def _campaign(campaign: str) -> str:
    if not isinstance(campaign, str) or not CAMPAIGN_RE.fullmatch(campaign):
        raise ValueError(f"not a campaign name: {str(campaign)[:60]!r}")
    return campaign


def _positive(value: int, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{what} must be a positive integer")
    return value


def _iteration(value: int) -> str:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("iteration must be a non-negative integer")
    return f"iter-{value:03d}"


def unit_slug(unit: str) -> str:
    """':' -> '__', then safe_component."""
    if not isinstance(unit, str):
        raise ValueError("an export unit must be a string")
    return safe_component(unit.replace(":", "__"))


@dataclass(frozen=True)
class StatePaths:
    root: Path                                     # SC/state/<campaign>

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))

    # --- files and directories at the top of the campaign state
    runner_lock = property(lambda self: self.root / "runner.lock")
    manifest = property(lambda self: self.root / "manifest.json")
    manifest_sha256 = property(lambda self: self.root / "manifest.sha256")
    engine = property(lambda self: self.root / "engine.json")
    amendments = property(lambda self: self.root / "amendments.jsonl")
    account_root = property(lambda self: self.root / "account")
    account_file = property(lambda self: self.root / "account" / "account.json")
    runtime = property(lambda self: self.root / "runtime.json")
    keydigests = property(lambda self: self.root / "keydigests.json")
    stopped = property(lambda self: self.root / "stopped.json")
    ledger = property(lambda self: self.root / "ledger.sqlite")
    replay = property(lambda self: self.root / "replay.sqlite")
    circuit = property(lambda self: self.root / "circuit.json")
    journal = property(lambda self: self.root / "journal.jsonl")
    # --- workspace
    workspace_root = property(lambda self: self.root / "workspace")
    ws = property(lambda self: self.root / "workspace" / "text_classification")
    pristine = property(lambda self: self.root / "workspace" / "pristine.json")
    # --- record trees
    calls_root = property(lambda self: self.root / "calls")
    jobs_root = property(lambda self: self.root / "jobs")
    quarantine_root = property(lambda self: self.root / "quarantine")
    scratch_root = property(lambda self: self.root / "scratch")
    import_checks_root = property(lambda self: self.root / "import_checks")
    proposer_root = property(lambda self: self.root / "proposer")
    iterations_root = property(lambda self: self.root / "iterations")
    qualification_root = property(lambda self: self.root / "qualification")
    qualification_passed = property(lambda self: self.root / "qualification" / "passed.json")
    calibration = property(lambda self: self.root / "qualification" / "calibration.json")
    noise = property(lambda self: self.root / "qualification" / "noise.json")
    # --- export and publication
    export_root = property(lambda self: self.root / "export")
    export_lock = property(lambda self: self.root / "export" / "export.lock")
    export_netrc = property(lambda self: self.root / "export" / "empty.netrc")
    export_wandb_local = property(lambda self: self.root / "export" / "wandb-local")
    publish_root = property(lambda self: self.root / "publish")
    smoke_receipt = property(lambda self: self.root.parent / "_smoke" / "receipt.json")

    def logs_dir(self, campaign: str) -> Path:
        return self.ws / "logs" / _campaign(campaign)

    def calls_dir(self, job_id: str | None) -> Path:
        return self.calls_root / ("_unknown" if job_id is None else safe_component(job_id))

    def job_dir(self, job_key: str) -> Path:
        return self.jobs_root / safe_component(job_key)

    def attempt_dir(self, job_key: str, attempt: int) -> Path:
        return self.job_dir(job_key) / f"a{_positive(attempt, 'attempt')}"

    def import_check_dir(self, iteration: int, attempt: int) -> Path:
        return self.import_checks_root / _iteration(iteration) / f"a{_positive(attempt, 'attempt')}"

    def session_dir(self, iteration: int, ordinal: int) -> Path:
        return self.proposer_root / _iteration(iteration) / f"s{_positive(ordinal, 'ordinal')}"

    def iteration_file(self, iteration: int) -> Path:
        return self.iterations_root / f"{_iteration(iteration)}.json"

    def qualification_dir(self, attempt: int) -> Path:
        return self.qualification_root / f"attempt-{_positive(attempt, 'attempt')}"

    def plan_dir(self, unit: str) -> Path:
        return self.export_root / "plans" / unit_slug(unit)

    def scan_file(self, unit: str) -> Path:
        return self.export_root / "scan" / f"{unit_slug(unit)}.json"

    def receipt_file(self, unit: str, surface: str) -> Path:
        return self.export_root / "receipts" / f"{unit_slug(unit)}.{safe_component(surface)}.json"

    def stored_spans_file(self, unit: str) -> Path:
        return self.export_root / "stored-spans" / f"{unit_slug(unit)}.json"

    def sibling(self, campaign: str) -> "StatePaths":
        """Another campaign's state under the same SC (3.22)."""
        return StatePaths(self.root.parent / _campaign(campaign))


def proposer_scratch(scratch_root: str, campaign: str, session_key: str) -> Path:
    """Path(scratch_root) / campaign / session_key (3.19)."""
    return Path(scratch_root) / _campaign(campaign) / safe_component(session_key)


def resolve_state(manifest: dict, manifest_path: Path) -> StatePaths:
    """StateError unless manifest['state_dir'] == 'state/' + manifest['campaign'];
    root = manifest_path.resolve().parent / 'state' / campaign."""
    campaign = manifest.get("campaign") if isinstance(manifest, dict) else None
    if not isinstance(campaign, str) or not CAMPAIGN_RE.fullmatch(campaign):
        raise StateError("manifest campaign is not a valid campaign name")
    if manifest.get("state_dir") != "state/" + campaign:
        raise StateError(f"manifest state_dir must be the literal 'state/{campaign}'")
    return StatePaths(Path(manifest_path).resolve().parent / "state" / campaign)
