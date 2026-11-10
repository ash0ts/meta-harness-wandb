# Meta-Harness on Forge

This package instruments the pinned Stanford Meta-Harness text-classification example with a budgeted inference gateway, isolated job execution, a durable journal, and independently verified telemetry exports. The upstream source remains unchanged.

The driver, controls, CLI and real-worker offline smoke are implemented. Live evidence includes a passing 600-second reconnect/rotation soak, complete validation baselines, and 412-task calibration for each baseline. Calibration passed its frozen gates: no-memory macro accuracy was 28.97%, and full few-shot was 43.16%. The original 130-task pilot remains failed instrumentation evidence because its journal had sequence collisions; the fresh engine fixes allocation and resume. Native CES creation returned HTTP 500, so an explicit export-only amendment selected W&B Tables. Their contents are read back cell by cell. Proposer isolation and full qualification still need passing live receipts before measured search. Public visibility, Report and saved-view operations remain an explicitly unverified publication skeleton.

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

Use a separate private project such as `mh-forge-preflight-20261110` under the chosen Forge team. The draft preserves the previous service ceilings: $60 solver inference and $40 sandbox compute, plus separate telemetry limits. Those are campaign ceilings, not a forecast. The first inference probe makes two capped requests. Record proposer headroom and session limits separately before search.

Create a **new** manifest beside `manifest.draft.json`. Set team/project, key provenance, approved limits and the preflight decisions before freezing. Use `key_provisioning: user_supplied` only for a credential source explicitly designated by the operator; it requires restricted proposer mode for search. `account-check` can run before a freeze:

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

Final test scoring uses `evaluate --test`; calibration uses `evaluate --calibration`. `evaluate --noise` runs fresh validation repeats. `evaluate --controls` runs five independent temperature-1 fewshot_all samples on each test task, chooses the most frequent normalized label (earliest sample breaks ties), and regrades the selected raw prediction with the pinned evaluator. All 15 sample jobs use the normal gateway, ledger, sandbox, sidecar and export path. Private `controls/` records retain the plan, task identities and selected-sample evidence. This is a five-sample control; its measured costs must be compared before calling it cost matched. Finalization blocks future search in that campaign. Export plans apply dataset content policy; publication bundles exclude raw prompts, transcripts, sidecars, memory, account facts and credentials, and scan final bytes before exposing a bundle. Publication approval remains false by default.

See `design/showcase-spec.md`, `design/lead-decisions.md` and `design/continuation.md` for the contracts and remaining work. `protocol.md` records the delegated stage 1 choices; stage 2 remains gated on live evidence.


Independent diagnostics can continue while Claude authentication is unavailable:

```sh
python -m mhforge preflight --live --manifest manifest.preflight.json --only baseline_noise sampling_controls
python -m mhforge preflight --live --manifest manifest.preflight.json --only telemetry
```

Run baselines before baseline noise. Selected diagnostics do not qualify a campaign, waive calibration, or authorize search. Live keys use the same credential channel as other commands. Proposer settings disable optional built-ins; the exact hash-pinned privacy component stays enabled and runtime permission probes remain mandatory.


If CES advertises support but table creation fails, preserve the partial receipt. After the active runner has stopped, an explicit export-only amendment can select `decisions.evaltables_backend="wandb_table"` with a failure note, followed by `export --retry-partial`. Native CES remains unverified. The hash-chained amendment cannot turn tables off or change solver/scoring inputs.

For a control measured in a separate campaign, `analyze_campaign.py --right-state STATE` checks frozen worker/data/solver compatibility and rebuilds the five-vote results from all sample sidecars before comparison.

Finished noise and voting studies export aggregate tables and an evidence artifact containing source hashes. The exporter recomputes the aggregates from successful, hash-checked sidecars and requires verified Agent Lens links for their source jobs. Finished private calibration also discovers test evaluation runs; those scores remain outside the proposer's upstream results directory.

An export receipt can be partial while W&B's summary is still becoming visible. Re-running `export` first reads back the existing immutable plan. If it verifies, no new upload, export attempt, or meter charge is made; previous readback mismatches remain in the receipt history. `--retry-partial` only uploads again if readback still fails. W&B Table verification compares every column and cell, including row order and JSON types. Agent Lens projects absent token fields as zero on replay spans; that representation is accepted only with matching explicit replay, zero-provider-attempt and unavailable-usage attributes. It never changes the inference ledger or treats missing paid usage as zero.
