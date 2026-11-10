# Continuation on 2026-11-10

The prior build was recovered from the uncommitted showcase tree on `showcase/forge`, based on upstream f805ad3aa4ada290458c1e0deadbe132fbbdea4e. It was verified with the installed Python 3.13 SDK environment: 1,051 tests passed, with no skips. Commit eba168f preserves that checkpoint. SSE fixture terminal blank lines are intentional wire bytes and now have a Git whitespace attribute.

## Added and exercised

The driver binds the existing JobRunner and ProposerRunner to the pinned upstream loop; tracks phases; journals starts, stops and resumable blocks; keeps calibration test results outside proposer-visible logs; reuses calibration attempts during finalization; records all-dataset averages and prior-frontier deltas; and shuts down executors and gateway listeners. CLI commands expose the manifest gates, status, lock-safe operator changes, hidden/descriptor/environment credential entry, SDK home isolation, dry exports, audit and real-worker offline smoke.

Publication builds an immutable allowlisted bundle in a private staging directory, with portable paths, secret/digest/privacy scans and fail-closed handling of candidate text. Its outward visibility/Report/saved-view methods remain an unverified skeleton, as the original spec allowed; the CLI only stages the bundle and reports that publication did not occur.

The preflight framework records ordered account, inference, qualification and calibration checks; refuses unavailable evidence; stops on a failed required check; preserves private account facts; and prevents `--only` from issuing full qualification. Account/API reads and the two-call inference probe have executable adapters. The full sandbox lifetime/relay soak, telemetry hygiene/restart rehearsal, proposer permission probes and crash-resume rehearsal still require concrete probe runners. They return `unverified` and block qualification today; they are not passing placeholders.

## Resume correction to section 3.20

Upstream's summary omits failed proposer iterations. Using only that summary to resume one-iteration blocks can repeat a failed iteration forever. The driver preserves upstream's summary bytes, takes the maximum of its summary and settled iteration records, and temporarily binds upstream's iteration-counter function to that starting value. It restores the hook afterwards. Interrupted, unsettled proposals retain their iteration number. A regression test runs two failed one-iteration blocks and asserts iterations 1 then 2.

Recovery also preserves upstream's tolerance for an interrupted JSONL summary line. Iteration records read sandbox costs from the executor receipt and treat upstream frontier accuracy as a percentage, including values below 1%. Calibration selects the highest evaluation pass and reconciles sidecar correctness with the result count, denominator and accuracy. A qualification receipt carries the observed environment hash, and the offline gate requires an explicit passing smoke verdict. Initialization failures outside the driver receive a sanitized durable stop record.

## Evidence and limits

The full continuation suite passed 1,111 tests with no skips or failures. Both local and local-relay smoke runs each ran six real baseline workers and 260 stub inference calls, produced nine dry-run export units, passed the ledger/evidence audit and secret scan, and made zero calls on resume. These observations establish local integration only. The final validation receipt records the exact tested engine. No Forge request, paid sandbox, real proposer session, remote artifact or publication was made.

The default `python3` on this machine is Python 3.9, below the package minimum. Socket-based tests require permission to bind localhost. The validation uses Python 3.13 and local socket access; neither issue required changing experiment behavior.

## Remaining live prerequisites

No team/project or dedicated key was configured in the recovered manifest. Use a fresh private preflight project (suggested name `mh-forge-preflight-20261110`) under the key's owning team. The prior $60 inference/$40 sandbox ceilings are retained; no proposer allowance is inferred. The user requested continuation and delegated reasonable project choices, but that supplies neither a key nor evidence of sandbox isolation. Complete the probe runners, worker image/wheelhouse proof, protocol decisions and all required live receipts before starting a measured search. Article/supplement placement remains undecided.
