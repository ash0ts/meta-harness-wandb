# Meta-Harness on Forge

This package instruments the pinned Stanford Meta-Harness text-classification example with a budgeted inference gateway, isolated job execution, a durable journal, and independently verified telemetry exports. The upstream source remains unchanged.

The driver, CLI and real-worker offline smoke are implemented. **No live campaign has run.** The live preflight registry refuses missing probe implementations or evidence; sandbox lifetime/relay soak, the full telemetry rehearsal, proposer isolation probes and crash/recovery rehearsal still need their live probe runners. Public visibility, Report and saved-view operations remain an explicitly unverified publication skeleton. These gaps prevent qualification for a measured search.

## Local verification

Use Python 3.11 or later (development validation uses 3.13). The driver imports upstream's YAML loader lazily. SDKs are optional and never imported by an ordinary package import.

```sh
cd showcase
python3.13 -m venv .venv
.venv/bin/python -m pip install '.[driver]'
.venv/bin/python -m unittest discover -s tests -t .
.venv/bin/python -m mhforge validate-manifest --manifest manifest.draft.json
.venv/bin/python -m mhforge status --manifest manifest.draft.json
```

Tests that exercise real upstream workers need `reference_examples/text_classification/.venv` with the frozen dependencies. The original upstream release does not track a lock: on a fresh checkout, copy `worker-image/uv.lock` to `../reference_examples/text_classification/uv.lock`, then run `uv sync --frozen --project ../reference_examples/text_classification`. Preserve any existing local lock and check its hash before replacing it. For SDK conformance tests install `.[live]` in the driver environment. The pinned SDKs are cwsandbox 1.27.0, coreweave 0.1.0b0 and wandb 0.31.0.

Run the end-to-end offline experiment:

```sh
.venv/bin/python -m mhforge offline-smoke --executor local_relay --keep
.venv/bin/python -m mhforge offline-smoke --executor local --keep
```

The smoke uses the real upstream loop, six workers, LiteLLM, capture sidecars, gateway, SQLite ledger and export planners. A loopback HTTP stub serves all 260 inference calls. It audits the records, scans for the credential canary and reruns baselines to prove that resume adds no calls. Stub scores are plumbing checks, not model-quality measurements. A receipt is saved at `state/_smoke/receipt.json` for the exact engine hash. State, worker environments, caches and wheelhouse binaries are ignored by Git.

## First live check

Use a separate private project such as `mh-forge-preflight-20261110` under the team that owns the dedicated Forge key. The draft preserves the previous service ceilings: $60 solver inference and $40 sandbox compute, plus separate telemetry limits. Those are campaign ceilings, not a forecast. The first inference probe makes two capped requests. The proposer allowance remains unset until explicit headroom and session limits are recorded.

Create a **new** manifest beside `manifest.draft.json`. Set team/project, dedicated-key provisioning, approved limits and the preflight decisions before freezing. `account-check` can run before a freeze:

```sh
.venv/bin/python -m mhforge account-check --manifest manifest.preflight.json
.venv/bin/python -m mhforge account-check --manifest manifest.preflight.json --live --key-source prompt --create-project
.venv/bin/python -m mhforge validate-manifest --manifest manifest.preflight.json --pin-upstream
```

`--pin-upstream` prints hashes to copy into the manifest; it never edits it. Build the worker environment from `worker-image/`, complete the protocol's first stage and run the offline smoke before freezing. The preflight command lists the ordered checks and their current blockers without spending:

```sh
.venv/bin/python -m mhforge preflight --manifest manifest.preflight.json
.venv/bin/python -m mhforge preflight --manifest manifest.preflight.json --teardown
```

A selected `--only` check never writes `qualification/passed.json`. A missing probe cannot pass. Calibration runs test jobs in private campaign state, without upstream `finalized.json` or published test results. Once all required preflight checks have evidence, measured search uses a new campaign and project; it imports only compatible qualification evidence.

## Credentials and operator controls

Interactive commands default to hidden prompts. Enter at subsequent prompts reuses the previous credential. No option accepts a key value. Automation can pass `all=<key>` or `provider=`, `sandbox=`, `export=` lines over an inherited descriptor with `--key-source fd:N`. Noninteractive commands default to `--key-source env`; read a value with the shell's hidden-input facility before exporting it so it does not enter shell history. The CLI stores key digests only, scrubs supplied environment variables and isolates SDKs in an empty private home under campaign state.

```sh
.venv/bin/python -m mhforge search --manifest manifest.measured.json --live --max-iterations-this-run 4
.venv/bin/python -m mhforge halt --manifest manifest.measured.json --note 'pause for billing reconciliation'
.venv/bin/python -m mhforge status --manifest manifest.measured.json
.venv/bin/python -m mhforge export --manifest manifest.measured.json --dry-run
.venv/bin/python -m mhforge audit --manifest manifest.measured.json
```

The first signal requests a stop at the iteration boundary; the second interrupts immediately. `halt` works while a runner holds the lock. Clearing a halt, reconciling usage and resetting an attempt require a note and an exclusive runner lock. Search blocks journal `paused` until the declared total is reached. Completed failed proposals consume their iteration on resume even when upstream omitted its summary row; the correction is recorded in `design/continuation.md`.

Final test scoring uses `evaluate --test`; calibration uses `evaluate --calibration`. Finalization blocks future search in that campaign. Export plans apply dataset content policy; publication bundles exclude raw prompts, transcripts, sidecars, memory, account facts and credentials, and scan final bytes before exposing a bundle. Publication approval remains false by default.

See `design/showcase-spec.md`, `design/lead-decisions.md` and `design/continuation.md` for the contracts and remaining work. `protocol.md` contains the unsigned research contract; it is not evidence that a live experiment ran.
