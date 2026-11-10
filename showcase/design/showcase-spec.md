# `showcase/` build spec (lead architect, merged, revised after adversarial review, amended after the second research round)

Date: 2026-11-09 (UTC 2026-11-10). Upstream pin: `f805ad3aa4ada290458c1e0deadbe132fbbdea4e`. This is the one spec the implementers build from. It merges three drafts (fidelity, failure, testability); Appendix A records every choice between them. It was then revised against 64 defects reported by three reviews (upstream compatibility, money and failure, security and buildability): Appendix E summarises what changed, and `<scratchpad>/design/revision-log.md` gives the decision for each defect. A second research round (proposer runtime, account and plan limits, sandbox image, time limits, paper facts, dataset licences, and a second critic) was then absorbed: Appendix F lists every amendment by item and every new name, Appendix G holds the document text the round fixed, and `<scratchpad>/design/amendments-round2.md` is the same log as a file.

An implementer gets this spec, the upstream code and the list of files they own. Sections 2 and 3 are the contract between owners: record shapes, signatures, error classes and environment variable names there are exact and must not drift. Section 6 says who owns which file and what each file may import.

## 0. How to read this

**Path shorthand**

| Tag | Path |
|---|---|
| `REPO` | `/Users/ashah/rsi/meta-harness-wandb` |
| `EX` | `REPO/reference_examples/text_classification` (upstream example at `f805ad3`; never edited) |
| `SC` | `REPO/showcase` (everything this build creates) |
| `STATE` | `SC/state/<campaign>` (git-ignored run state) |
| `WS` | `STATE/workspace/text_classification` (byte-identical working copy of the pinned upstream files) |
| `LOGS` | `WS/logs/<campaign>` (upstream's own run directory, written by upstream code) |
| `H2` | `ash0ts/harness2-wandb@31b0f82`, `harness2/showcase/` |

`Lnnn` is a line of the named upstream file at `f805ad3`.

In code, `sc_dir` is always the directory that holds the manifest file (`manifest_path.resolve().parent`). For the real package that is `REPO/showcase`; tests and the offline smoke use other directories of the same shape (2.3 rule 2). The engine code is located through `mhforge.__file__`, never through `sc_dir`.

**Status tags on claims**

| Tag | Meaning |
|---|---|
| [src] | I read the cited upstream line myself while merging |
| [L1] | I ran an offline experiment while merging (below) |
| [F-En], [X-En], [T-En] | confirmed by experiment n of the fidelity, failure or testability draft; scripts under `<scratchpad>/design/scratch-fidelity`, `scratch-failure`, `scratch-testability` |
| [A-U], [A-M], [A-S] | confirmed by an experiment of the upstream-compatibility, money-and-failure or security-and-buildability review; scripts under `<scratchpad>/design/scratch-upstream-compat`, `scratch-money-failure`, `scratch-secbuild` |
| [R1], [R1b], [R2], [R3] | I ran an offline experiment while revising (below); scripts under `<scratchpad>/design/scratch-revise` |
| [rep] | taken from a research report or its verification; not re-checked |
| [r2] | taken from a second-round report under `<scratchpad>/research2` (`preflight-readiness.md`, `critic2.md`, `proposer-runtime.md` and its verification, `sandbox-image.md`, `time-and-limits.md`, `forge-plan-limits.md`, `paper-facts.md`, `dataset-licences.md`); not re-checked by me |
| [inf] | inferred; nothing observed |
| [live] | can only be settled by the live preflight (section 8); `[live: PF<n>]` names the preflight step of 8.2 that settles it |

**What I checked while merging** (offline, loopback or pipe only, no model call, nothing written under `REPO`)

- Re-read the upstream lines every conflict between drafts turned on: `meta_harness.py` L20-L60, L95-L215, L286-L300, L360-L580; `benchmark.py` L14-L48, L56-L178, L290-L312, L464-L612, L705-L732, L780-L962; `llm.py` L10-L36, L88-L126, L155-L290, L296-L425; `inner_loop.py` L479-L500, L516-L665; `claude_wrapper.py` L10-L22, L115-L170, L490-L670.
- [L1] `<scratchpad>/design/scratch-lead/l1_daemon_bridge.py` (output `l1_out.txt`). Three results:
  1. The frame format of 2.9 survives seeded random re-chunking (1 byte to 64 KiB) with per-chunk `decode("utf-8", errors="replace")`: 20 of 20 frames with 108,000-byte Chinese-plus-emoji bodies byte-identical; a 108 KB body is 541 bytes on the wire when it compresses well.
  2. A stub daemon that owns the HTTP server and a Unix socket survives the death of its bridge. Eight concurrent requests, bridge killed with SIGKILL 0.2 s in, second bridge attached 0.5 s later: 8 of 8 HTTP replies correct, every request handled exactly once on the host (dedup by request id against an in-flight set and a done cache), `HELLO` pending lists of 5 and 8. No draft had run the daemon-plus-bridge path; this is the evidence for relay mode `daemon`.
  3. On this macOS, `bind()` of an `AF_UNIX` socket fails with "AF_UNIX path too long" for a 169-byte path. State-directory paths are longer than the limit (104 bytes on macOS), so the local daemon-mode socket must live under a short directory (3.13).
  A side observation: a buffered `Popen.stdin` to a killed bridge raises `BrokenPipeError` at interpreter exit; host-side bridge pipes are opened unbuffered and closed with errors ignored (3.12).

**What I checked while revising** (same limits: offline, no model call, nothing written under `REPO`)

- [R1] `scratch-revise/r1_load.py` (output `r1_out.txt`), run with the example venv's Python against a copy of the 39 tracked files. The three upstream modules load with `importlib.util.spec_from_file_location` in the order `claude_wrapper`, `benchmark`, `meta_harness`, with `sys.path` unchanged: `EVOLVE_DIR` is the copy, `meta_harness.claude_wrapper` is the module we loaded, a `yaml.py` planted in the copy is not imported, and no `__pycache__` appears. The passthrough command without `-P` runs a planted `random.py` (return code 98); with `-P` the real `benchmark.py --results` returns 0.
- [R1b] `scratch-revise/r1b_load_bytes.py` (output `r1b_out.txt`): with a poisoned `__pycache__/claude_wrapper.cpython-313.pyc` that carries the source's mtime and size, the stock source loader executes the poisoned code (return code 96); compiling the bytes we read and executing them in a module made with `module_from_spec` loads all three modules correctly and ignores the cache.
- [R2] `scratch-revise/r2_regex.py`: the `keyed_hex40` rule of 3.24 does not match the job-file field (under the old or the new name), a 64-hex value after `token_sha256`, the `tiktoken` line of `uv.lock` or a git commit id; it matches `MHFORGE_FORGE_API_KEY=<40 hex>`, `WANDB_API_KEY: <40 hex>`, `"api_key": "<40 hex>"`, `password <40 hex>` and `"token": "<40 hex>"`.
- [R3] `scratch-revise/r3_scrub.py`: on this macOS, after `del os.environ[NAME]` a sibling `ps eww -p <pid>` still shows the value; after overwriting the bytes in place through `libc.getenv` and `ctypes.memset` it does not.
- Re-read for the citation fixes: `meta_harness.py` L20-L28, L136-L161, L192-L215 (the import timeout is L206), L286-L357, L395-L402, L480-L492; `benchmark.py` L171-L215 (`load_results` uses `rglob`); `inner_loop.py` L175-L200 and L740-L760 (the first validation pass is L178-L181); `llm.py` L320-L326; `SKILL.md` L70-L78. Read in the downloaded SDK sources (never executed): `cwsandbox` `_auth.py` L32-L40, L167-L195, L262-L263 (`AuthHeaders` is accepted as it is); `coreweave` `tracing.py` L68, L180-L188 (`base_url` wins over `WF_TRACE_SERVER_URL`); `wandb` 0.31.0 `wandb_init.py` L207-L216 (a key given through settings is not persisted).
- `git check-ignore --no-index` on a sample of the planned file names: only `worker-image/uv.lock` is hit by the root `.gitignore`, which the negation in `showcase/.gitignore` covers.

**Still unverified.** Nothing here has touched a live service. Every runtime statement about Serverless Inference, Sandboxes, Agent Lens, EvalTables, W&B and the real `claude` CLI is [rep] or [inf] until the preflight of section 8. The proposer-runtime report now exists (`<scratchpad>/research2/proposer-runtime.md`, with a verification); its statements come from the CLI's documentation, the pinned binary's strings and eleven local transcripts, none of them a `-p` session, so the stream and transcript shapes of a real proposer session are [live] until PF9 and PF10. The revision added live-only assumptions of its own, each tied to a preflight step of 8.2: the hand-built sandbox auth headers (PF3), `wandb.Settings(api_key=...)` and the settings that switch off machine metadata (PF8), the explicit trace `base_url` (PF8), that exec streams can stall without closing (PF6), the per-message template cost on the server (PF5), whether complete streams end with `[DONE]` (PF5), and whether a `result` event follows SIGINT (PF9). The second round added more: the generated restricted settings as a combination (PF9), `sandboxed: true` on Bash tool results (PF9), `--max-budget-usd` on this login (PF9), the wheelhouse path end to end (PF6), the sandbox lifetime being enforced (PF6), the byte estimator against stored spans (PF8).

**Rules for every file**

- `mhforge/**` imports only the standard library and `mhforge` at module import time. `yaml`, `wandb`, `cwevals`, `coreweave`, `cwsandbox`, `wandb_workspaces`, `opentelemetry` are imported only inside functions of `workspace.py` (`yaml`, through upstream's own import), `executors/sandbox_client.py`, `telemetry/adapter_agentlens.py`, `telemetry/adapter_wandb.py`, `preflight.py` and `publish.py`.
- `mhforge/insandbox/*.py` import nothing from `mhforge`. They are shipped verbatim into the executor as sibling files. `mhf_relay_stub.py` imports its sibling with `import mhf_frames`; the host imports the codec as `mhforge.insandbox.mhf_frames`.
- No module calls `os.environ.copy()`, reads `~/.netrc`, prints or logs the value of an environment variable, or puts a secret in an argv. Every child process is started with an explicit `env=` built by `procenv.py`. Only `cli.py`, `smoke.py` and `tests/helpers.py` read `os.environ`; every other module takes an environment mapping as an argument with no `None` default (the table at the head of section 3 lists the few modules that set a variable).
- Every identifier check uses `re.fullmatch`; the patterns are written without `^` and `$` (a `$` accepts a trailing newline under `re.match` [A-S]).
- Time, sleeping, randomness and ids are injected (`util.Clock`, `util.IdSource`, `random.Random`) wherever a test needs to control them.
- Tests use `unittest` and the standard library only. Run from `SC`: `python3 -m unittest discover -s tests -t .`.
- File names avoid the patterns of the repository's root `.gitignore`: no directory named `data`, `build`, `lib`, `dist`, `jobs`, `env`, `venv`, `var`, `parts` or `target`, no `*.log` file [T-E5]. `test_no_file_name_hits_root_gitignore` runs `git check-ignore --no-index` over every file under `SC` outside `state/` when `git` is present, so the rule does not depend on this list.
- Target size: 500 lines or less per file.

---

## 1. Layout

Every file under `SC`. (P) marks a file that ports Harness2 code: it starts with `# SPDX-License-Identifier: Apache-2.0` and `# Adapted from ash0ts/harness2-wandb@31b0f82 <path>. Changes: <summary>`. Every other Python file starts with `# SPDX-License-Identifier: MIT`. (U) marks a thin live adapter that is UNVERIFIED until the preflight.

```
showcase/
  .gitignore                      state/ ; __pycache__/ ; *.pyc ; .venv/ ; *.egg-info/ ; worker-image/wheelhouse/ ; !uv.lock
  README.md                       what this is; how to run the tests and offline-smoke; why live commands refuse; live checklist
  PROVENANCE.md                   exists today; gains the ported-file table (Appendix C) and the workspace-copy note
  LICENSE-APACHE-2.0              verbatim Apache License 2.0 text, for the (P) files
  pyproject.toml                  project mhforge-showcase, requires-python >=3.11, no dependencies; extras `driver` and `live`
  protocol.md                     predeclared protocol TEMPLATE, signed in two stages: claim, claims not made, selection rule, gates G0 to G2,
                                  noise floor, matched-budget control, fidelity tables, dataset attribution (Appendix G)
  THIRD_PARTY_DATA.md             dataset sources, licences, the nine data-file hashes, attribution text (Appendix G)
  manifest.draft.json             the filled draft manifest of 2.3; every user decision refuses live
  worker-image/Dockerfile     (U) recipe for the linux/amd64 worker image; never built offline
  worker-image/README.md          build command, digest rule, how the wheelhouse is made
  worker-image/requirements.lock.txt   hash-pinned requirements for Linux x86_64 (62 pins); its hash is sandbox.wheelhouse.requirements_sha256
  worker-image/wheelhouse/        git-ignored; wheels/*.whl, SHA256SUMS and wheelhouse.tar, made by the README's commands (never committed)
  worker-image/uv.lock            copy of EX/uv.lock (upstream git-ignores it); its hash is upstream.worker_lock_sha256
  mhforge/
    __init__.py                   __version__ = "0.1.0"
    __main__.py                   calls cli.main()
    errors.py                     exception taxonomy, halt reasons, exit codes
    util.py                   (P) Clock, IdSource, UTC helpers, canonical JSON, atomic JSON and JSONL, hashes, tree hashes, micros,
                                  read-only SQLite open, loopback guard, in-place scrub of an environment value, key digests
    wire.py                       JobIdentity, GatewayReply, refusal table, envelope helpers, bounded ids, HandlesRequests protocol
    paths.py                      StatePaths: path arithmetic for the state directory; unit slugs
    scan.py                       scanner (pure): secret patterns, known values, key digests; privacy patterns (local paths, a host name
                                  given as data); verbatim dataset-text index
    manifest.py               (P) schema, validation, live gates, freeze and drift check, amendments, import diff
    state.py                  (P) StateLock (runner and export locks), read-only status
    procenv.py                    the only place child environments are built: worker, proposer (closed list and deny-list), passthrough,
                                  exporter, minimal
    ledger.py                 (P) SQLite ledger: reserve, settle, reconcile, phases, per-job ceilings, halt flag, job registry, meters
    replay.py                     reply reuse store and in-flight coalescing
    sse.py                    (P) SSE parsing, merge_stream, to_completion_body
    circuit.py                (P) persisted circuit breaker
    provider.py                   ProviderClient protocol, ForgeProviderClient (U against the service), classify()
    stubprovider.py               StubProvider (in process), render_sse, serve_stub (loopback SSE server), default_reply
    gateway.py                (P) transport-agnostic core: (job identity, request bytes) -> (status, headers, body)
    ingress_http.py               loopback HTTP ingress on 127.0.0.1, one listener per job
    relay.py                      host side of the exec-stream relay: FrameChannel, PipeChannel, RelaySession
    insandbox/
      __init__.py                 empty
      mhf_frames.py               MHF1 frame codec (single source; shipped into the executor)
      mhf_relay_stub.py           in-executor stub: HTTP server, uplink (stdio or Unix socket), worker supervisor, bridge
      mhf_selfcheck.py            in-executor check: env names, credential-shaped names, egress probes, versions, vocabulary file
      mhf_capture_entry.py        per-example capture wrapper around python -m text_classification.inner_loop
    workspace.py                  verified working copy of EX, drift detection and restore, upstream import context
    journal.py                (P) append-only operation journal
    bundle.py                     job payload tar, job file, argv placeholders, worker command, output collection
    executors/
      __init__.py                 Executor protocol, JobSpec, JobResult, ImportResult, classify_attempt, check_selfcheck, sandbox_cost_micro
      local.py                    LocalExecutor (host subprocess, loopback ingress; NOT ISOLATED)
      local_relay.py              LocalRelayExecutor (stub and worker in a subprocess, relay over pipes; NOT ISOLATED)
      sandbox_client.py       (U) SandboxClient protocol; CwSandboxClient adapter (lazy import of cwsandbox)
      sandbox.py                  SandboxExecutor: call sequence over SandboxClient
    jobs.py                       UpstreamFacade, plan_jobs, JobRunner, run_benchmark dispatch, publish and its verification, launcher logs
    proposer_shim.py              subprocess shim for claude_wrapper: pinned binary, per-mode argv, allow-list env, tee, monotonic deadline,
                                  graceful timeout, group reaping
    proposer.py                   ProposerRunner: propose_claude replacement, generated settings file, limit classifier, session checks,
                                  session record, proposal store and replay
    driver.py                     loads upstream, installs replacements, make_executor, runs baselines, search (in blocks), calibration,
                                  noise repeats and test
    qualification.py          (P) engine snapshot, offline gate, observations, forecast, numbered attempts
    preflight.py              (U) account check and the ordered live steps as data; offline only describe() and the offline checks run
    audit.py                  (P) ledger versus call records versus receipts
    publish.py                (P)(U) allow-listed bundle (scrubbed with policy.portable), ownership check, visibility skeleton
    telemetry/
      __init__.py                 empty
      policy.py               (P) content policy, attribute rules, byte counting, hex masking, portable paths (pure)
      join.py                     sidecar rows + call records -> example rows and call roles (pure)
      transcript.py               on-disk Claude Code transcript or stamped stream -> NormalizedSession (pure)
      plan_spans.py               records -> span plans: solver job, proposer session, outer loop (pure)
      plan_tables.py              example rows -> EvalTable plans with the CES rules (pure)
      plan_runs.py                records -> W&B run and Artifact plans, our search metrics (pure)
      readback.py             (P) read-back query bodies and verification (pure)
      exporter.py                 the one exporter step: plan, scan, meter, emit, read back, receipt
      spawn.py                    ExportSpawner: one bounded exporter child at a time (export_mode after_unit)
      adapter_agentlens.py    (P)(U) coreweave SDK emit and HTTP read-back (lazy imports)
      adapter_wandb.py        (U) wandb run, EvalTable, Artifact (lazy imports)
    smoke.py                      offline-smoke orchestration
    cli.py                        argparse front end, the --live gate, key sources, exit codes
  tests/
    __init__.py                   required by discovery [T-E5]
    helpers.py                    tmp_state(), FakeClock, SeqIds, make_manifest(), write_manifest(), FakeGateway, skip decorators, assert_neutral()
    fake_channel.py               FaultChannel: re-chunking, replace-decoding, retaining, dropping, noise
    fake_sandbox.py               FakeSandboxClient: sandboxes as temp dirs, execs as local subprocesses, scripted faults
    fake_sinks.py                 FakeLensSink, FakeWandbSink
    fake_claude_cli.py            writes a fake `claude` executable: scripted stream-json, signal behaviours, limit and model events,
                                  synthetic transcript in the 2.1.317 shape
    fake_claude_wrapper.py        module-like stand-in that uses `subprocess` the way claude_wrapper.run does (L590-L654)
    fake_facade.py                FakeFacade (UpstreamFacade) and FakeExecutor
    fake_meta_harness.py          module-like stand-in for meta_harness with the four globals and upstream's call order
    fixtures/
      manifest_valid.json         a manifest that passes validation with every decision made (fake entity)
      fake_pkg/text_classification/   stdlib-only stand-in package exposing the hooked seams (llm, data, inner_loop)
      sse_ok.txt  sse_error_only.txt  sse_truncated.txt  sse_usage_missing.txt  sse_reasoning_only.txt  sse_reasoning_content.txt
      call_records/               six call records: completed, replayed, refused, provider_rejected, failed, truncated_empty
      sidecar_uspto_val.jsonl     synthetic sidecar: 4 examples, 2 passes
      transcript_synthetic/       byte copy of the corrected synthetic fixture (5.1): <session>.jsonl, <session>/subagents/agent-<id>.jsonl
                                  and .meta.json, EXPECTED.json, README.md
      stream_synthetic.jsonl      byte copy of the fixture's stream/events.jsonl: raw stream-json events, one per line
      stream_limit_synthetic.jsonl   byte copy of the fixture's events.usage-limit.INFERRED.jsonl (a limit stop; inferred, never observed)
    test_<module>.py              one per source module; names and cases in section 5
```

`SC/state/` is created at run time and is git-ignored. Nothing is created outside `SC`, with two exceptions. The daemon-mode Unix socket of a local executor or of `tests/fake_sandbox.py` lives in a directory made with `tempfile.mkdtemp(prefix="mhf-", dir="/tmp")` and removed when the attempt ends, because state-directory paths exceed the `AF_UNIX` path limit [L1]. And each proposer session gets a scratch directory `<proposer.scratch_root>/<campaign>/<session_key>` (default root `/private/tmp/mhf-proposer`; 3.19): it must lie outside the workspace and outside `STATE`, it holds only what the proposer writes while prototyping and the `uv` cache, it is left in place after the session and removed by the teardown of 8.2. No driver state, key material or gateway material is ever placed under `/private/tmp` or `/tmp`.

`pyproject.toml`: `[project] name = "mhforge-showcase"`, `version = "0.1.0"`, `requires-python = ">=3.11"`, `dependencies = []`; `[project.optional-dependencies] driver = ["pyyaml>=6"]`, `live = ["wandb==0.31.0", "cwevals==0.0.1b1", "coreweave==0.1.0b0", "cwsandbox[wandb]>=1.19.6", "wandb-workspaces==0.4.13", "pyyaml>=6"]`; `[tool.setuptools.packages.find] include = ["mhforge*"]`. The dry resolution of the `live` set is [rep].

**Non-code files, exact content required.**

- `.gitignore`: the lines `state/`, `__pycache__/`, `*.pyc`, `.venv/`, `*.egg-info/`, `worker-image/wheelhouse/`, `!uv.lock` (the root `.gitignore` ignores `uv.lock`; this negation in `showcase/.gitignore` re-includes ours [T-E5]).
- `README.md`: what the package is (one paragraph, with the upstream pin and the Harness2 lineage); the layout in ten lines; how to run the unit tests and `offline-smoke` (with the interpreter rule below); what each live command needs and why it refuses; the live checklist in the order of section 8.2, including which steps must be started from the user's own terminal (every step that starts `claude`, and the search itself) and the teardown list; how keys are given (`--key-source prompt` by default; for `--key-source env`, read each key with `read -s NAME && export NAME` so it never enters the shell history, and never type `export NAME=<key>`; never on a command line); the two-campaign rule (a preflight campaign in a scratch project, the measured campaign in a fresh project that imports the qualification); the statement of what is enforced about the keys and what is not (the last part of section 7), including the stored key digests. No secret, no key value, no internal source.
- `protocol.md`: a template in two stages. Its first line is `<!-- TEMPLATE-UNFILLED -->` (stage 1; the offline gate refuses every live command while that marker is present) and its section "Stage 2" starts with the line `<!-- STAGE2-UNSIGNED -->` (`search` in a measured campaign refuses while that marker is present; 2.3). The headings and the fixed text are in Appendix G; the owner copies them verbatim and leaves the bracketed values for the user. Stage 1, filled before any paid call: Claim (the reproduction-claim wording); Claims not made (the table); Fixed parameters (the `upstream.expect` block); Selection rule and reported effect; Calibration gates G0, G1 and G2 with their numeric bands (run with `evaluate --calibration`, which never finalises the run; the bands are mirrored in the manifest's `calibration.gates` so the verdict is computed, not judged; no gate uses a figure from the paper's few-shot baseline); Noise floor (four layers; `calibration.noise_repeats` baseline repeats run with `evaluate --noise`); Matched-budget control (parallel sampling with K = 5; its runner is deferred, 8.3); Budgets and stop rules (the manifest caps; "stop, do not shrink the design"); Missingness (how failed jobs and incomplete candidates are reported); Predeclared choices (`routing.reply_reuse_scope`, `search.partial_candidate_policy`, the permission mode, the per-dataset content rules); Fidelity table A (paper against the release) and table B (the release against this run: the table of section 7, copied); Dataset attribution. Stage 2, signed after qualification and the first real proposer session: the job limit (`executor.job_timeout_s`), dataset-job parallelism (`executor.parallel_dataset_jobs`), the proposer model and CLI version, the proposer ceilings.
- `PROVENANCE.md` (exists): append the ported-file table of Appendix C, the note that upstream runs from a hash-verified copy under `showcase/state/`, and the licence note (ported files Apache-2.0, everything else MIT).
- `THIRD_PARTY_DATA.md`: the licence table, the publication rule, the attribution text and the `mce-artifact` licence evidence of Appendix G, plus a table of the SHA-256 of the nine data files `data/{uspto,symptom_diagnosis,crime_prediction}/{train,val,test}.jsonl` at `f805ad3`, computed by the owner from `EX` (`test_third_party_data_hashes_match_example` re-computes them). It names no person and quotes no dataset row.
- `worker-image/Dockerfile` (U): `FROM` the Python 3.13 slim-bookworm image pinned by its **amd64 manifest digest** (`sandbox.base_image`; never the multi-arch index digest, so a wrong-architecture node fails loudly [r2]); `--platform=linux/amd64`; `ENV LITELLM_LOCAL_MODEL_COST_MAP=True` placed **above** the `RUN` that installs and imports LiteLLM; install from `worker-image/requirements.lock.txt` with `pip install --require-hashes` into the system interpreter, so that `python3` on the job file's default `PATH` (`/usr/local/bin:/usr/bin:/bin`) is the interpreter that has the packages (no separate venv); the image may also set `TIKTOKEN_RS_CACHE_DIR` to LiteLLM's bundled tokenizers directory (the selfcheck does not count that name as a credential, 3.13); create `/mhf`; no `mhforge` code, no dataset and no key baked in. `worker-image/README.md`: the build and push commands (`docker buildx build --platform linux/amd64 --push`, then read the registry's manifest and pin the amd64 manifest digest in `sandbox.image`; a plain `docker push` on Apple Silicon can ship arm64); and how the wheelhouse is made: download exactly the 62 wheels that `requirements.lock.txt` names into `wheelhouse/wheels/` (Linux x86_64, glibc 2.28 or later, CPython 3.13; for example `pip download --only-binary=:all: --platform manylinux_2_28_x86_64 --python-version 3.13 --no-deps --require-hashes -r requirements.lock.txt -d wheelhouse/wheels`; four of the packages have no wheel below glibc 2.28 [r2]), check that there are 62 files and that every file's hash is in the requirements file, write `wheelhouse/SHA256SUMS` over `wheels/*`, and pack `wheels/`, `SHA256SUMS` and `requirements.lock.txt` into `wheelhouse/wheelhouse.tar` (about 122 MiB); then put the tar's SHA-256 and byte count and the SHA-256 of `SHA256SUMS` into `sandbox.wheelhouse.*`. These are public downloads, made by the user on their word; nothing in the package or its tests runs them.
- `worker-image/requirements.lock.txt`: the hash-pinned requirements file for Linux x86_64 (62 pins, every wheel hash). It is a **byte copy** of the research artefact `/private/tmp/claude-502/-Users-ashah-Library-Application-Support-Claude-scratch-workspaces-4fae85b1-8a8d-4e26-975c-aa802946e255-09c4c5cb-f508-4d7e-841e-046add0921c8-scratch-2026-11-09-790576/d83951c7-b145-4d4e-9594-03f7dd065c09/scratchpad/research2/work/sandbox-image/artefacts/requirements-linux-x86_64.hashed.txt`, which was resolved from the lock at `f805ad3` (`uv export --frozen --no-hashes --no-emit-project --format requirements-txt`, then `uv pip compile --python-platform x86_64-unknown-linux-gnu --python-version 3.13 --only-binary :all:` with hashes) and is byte-identical for Python 3.12 and 3.13 [r2]. Its SHA-256 is `sandbox.wheelhouse.requirements_sha256` (`4698f162b4a77cb251f492b930029976b6f573478faa69663f56a73bd73a7ade`); `test_requirements_lock_hash_matches_manifest_draft` checks the committed file against the draft manifest, so the owner copies the file and does not regenerate it.
- `worker-image/uv.lock`: a byte copy of `EX/uv.lock`.

**Interpreter rule.** Upstream `meta_harness.py` imports `yaml` (L21) and `benchmark.py` reads `config.yaml` at import (L27) [src]. The commands that import upstream (`offline-smoke`, `search`, `evaluate`, `preflight`) therefore need an interpreter with PyYAML: offline the example venv's Python (`PYTHONPATH=SC EX/.venv/bin/python -m mhforge offline-smoke`), live an environment with the `live` extra. Nothing is installed into the example venv: its packages must match the worker environment. The `live` environment must **not** contain the `weave` package: `wandb.init` starts Weave tracing when it is importable, which would log every model call a second time into the same meter [r2], so the exporter refuses to start when `importlib.util.find_spec("weave")` finds it (3.25). `account-check --live` needs the `live` extra too. `status`, `validate-manifest`, `amend`, `export --dry-run` and the whole unit-test suite run on a bare Python 3.11+.

---
## 2. Shared records and schemas

### 2.1 Conventions (bind every module)

- **Encoding.** UTF-8 JSON. Whole files are written with `util.write_json` (temp file in the same directory, then `os.replace`). JSONL files are append-only, one `os.write` per row on an `O_APPEND` descriptor; readers ignore a torn last line and raise `StateError` on a bad line elsewhere.
- **Schema tag.** Every record has `"schema": "mhforge.<kind>/1"`. Readers ignore unknown keys and raise on an unknown major version.
- **Time.** UTC only. Host records use strings `YYYY-MM-DDTHH:MM:SS.ffffffZ` in fields ending `_utc`, produced only by `util.utc_iso`. The in-executor files (sidecar, `exit.json`) use `t`, `t_start`, `t_end` = float seconds since the Unix epoch, because they must stay dependency-free and tiny. Durations are float seconds in fields ending `_s`.
- **Money.** Integer micro-dollars in fields ending `_micro`. Decimal strings only in the manifest. No float arithmetic on money.
- **Hashes.** Lower-case hex SHA-256. `util.canonical_json(obj)` is `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")`.
- **Never in any record:** an `Authorization` header, a key, a job token, the value of an environment variable named in the manifest as a key variable. Environments are recorded as lists of names.
- **Key digests.** The one thing about a key that is stored: `STATE/keydigests.json` (mode 0600; `{"schema": "mhforge.keydigests/1", "keys": [{"name", "len", "sha256", "utc"}]}`) holds, for each key a component read, the variable name, the value's length and the SHA-256 of the value, so that the scanner can find a bare key that fits no pattern (3.24). It is never planned, exported or bundled. This is a reading of F14 ("the key is never written to the state directory"): a digest is written, the key is not; section 7 states it.
- **Identifier checks** use `re.fullmatch` everywhere.

### 2.2 Identifiers

| Name | Shape | Notes |
|---|---|---|
| `campaign` | `[a-z0-9][a-z0-9-]{2,39}` (full match) | manifest. Also upstream's `--run-name`, the W&B group and the prefix of every conversation and run id. |
| `candidate` | `[a-z][a-z0-9_]{0,47}` (full match; at most 48 characters) | upstream memory-system name (a Python module name under `agents/`). Names from `pending_eval.json` that fail the pattern are rejected before any use. |
| `dataset` | one of `upstream.expect.datasets` | `USPTO`, `Symptom2Disease`, `LawBench` |
| `split` | `val` or `test` | which result file the job writes |
| `phase` | `qualification`, `calibration`, `baseline`, `search`, `test`, `controls` for a job; plus `recovery` as a ledger booking phase | `wire.JOB_PHASES` and `wire.LEDGER_PHASES` (3.2). `calibration` is the phase of the calibration test jobs and of the noise repeats (3.20); `controls` is reserved for the matched-budget control, whose runner is deferred (8.3). |
| `job_key` | `<split>.<candidate>.<dataset>`; with the prefix `qual.` when the job's phase is `qualification`; with the suffix `.r<k>` when the job has a repeat index `k` (a noise repeat, 3.20) and `.s<k>` when it has a sample index (the deferred sampling control) | no attempt number. The per-job ceilings and the replay scopes `job` and `repeat` use it. Safe as a path component (at most 84 characters). |
| `job_id` | `<job_key>.a<attempt>` | `attempt` starts at 1 and is never reused |
| `call_id` | `c-<YYYYMMDDTHHMMSS>-<12 hex>` | one per request that reached `Gateway.handle`. Never placed in a reply to a worker (an id can contain the digits `429`, which upstream's retry test matches, `llm.py` L98-L108 [src]). |
| `ledger_id` | `l-<YYYYMMDDTHHMMSS>-<12 hex>` | one per provider attempt |
| `session_key` | `iter<NNN>.s<k>` | proposer session `k` (1-based) of upstream iteration `NNN` (zero-padded to 3) |
| job token | `mhfjob_` + 43 URL-safe characters (`secrets.token_urlsafe(32)`) | loopback ingress, Route 2 only; also the per-attempt bearer of the relay stub when it runs on the host (`LocalRelayExecutor`, 3.13). Never in a sandbox. Stored only as its SHA-256 (the stub bearer is not stored at all). The scanner knows the pattern. |
| `unit` | `job:<job_id>`, `proposer:<session_key>`, `eval:<split>.<candidate>`, `propose:<candidate>`, `loop:iter<NNN>` | one export unit. `paths.unit_slug` replaces `:` with `__`. |
| `example_id` | `"%04d-%s" % (index, example_sha256[:12])` | index in the split list the loader returned; `example_sha256 = sha256(raw_question + "\0" + target)` |
| conversation id | `<campaign>.<job_id>[.learn][.p<n>][.e<k>]`, `<campaign>.proposer.<session_key>[.p<n>][.e<k>]`, `<campaign>.search.p<n>[.e<k>]` | characters `[A-Za-z0-9._-]` only, at most 128. At the pattern maxima an id reaches 133 characters [A-S], so every id is passed through `wire.bounded_id(text, 128)`: an id over the limit becomes its first 119 characters, `-`, and the first 8 hex of the SHA-256 of the full id. The outer loop's `n` is `1 + NNN // telemetry.agent_lens.loop_iterations_per_conversation` (3.23). |
| W&B run id | `<campaign>-search`, `<campaign>-propose-<candidate>[-e<k>]`, `<campaign>-eval-<split>-<candidate>[-e<k>]`, `<campaign>-publication` | `-e<k>` only for export attempt `k >= 2`; bounded the same way at 128 |

### 2.3 Manifest

One JSON file per campaign (a preflight campaign and the measured campaign each have their own; `campaign_kind`). Top-level keys are closed: an unknown key anywhere in the schema is a validation error (a typo must not fall back to a default), except the free-text `notes`. `validate-manifest` checks it; `validate-manifest --freeze` writes the canonical copy to `STATE/manifest.json`, its hash to `STATE/manifest.sha256` and the engine snapshot to `STATE/engine.json`. After that any difference between the given file and the frozen copy is refused with `ManifestError("Manifest changed: start a new campaign instead of mixing attempts")` (rule from H2 `runtime.py` L55-L62 [rep]). Two things soften that rule without weakening it. Values learned during the preflight go into the **next** campaign's manifest: a measured campaign imports the qualification of a preflight campaign when the two frozen manifests differ only in a named set of fields (3.22). And the export-only fields can be amended after the freeze through an append-only, hash-chained amendments file (rule A below); the frozen manifest and its hash never change.

Fields marked **gate** are the user's decisions (F16). Their draft value makes live commands refuse, naming the field.

| Path | Type | Meaning and rule |
|---|---|---|
| `schema` | `"mhforge.manifest/1"` | |
| `notes` | str | ignored |
| `campaign` | str | 2.2 |
| `campaign_kind` | `"preflight"` or `"measured"` | A preflight campaign writes to a scratch project, is never published and is where the whole preflight of 8.2 runs. A measured campaign writes to a fresh project and may import the qualification of a preflight campaign (`qualification.import_from`). Each campaign has its own manifest file and its own `STATE`. |
| `state_dir` | str | must equal the literal `state/<campaign>`; it is taken relative to the manifest's own directory (rule 2) |
| `decisions.live_spend_approved` | bool, **gate** (`false`) | U1, U5: any paid call, sandbox or remote write |
| `decisions.key_provisioning` | `"unset"` or `"dedicated"`, **gate** (`"unset"`) | U3: the user confirms that the keys given to live commands are dedicated, revocable keys used only by this run. How a key reaches a command is the `--key-source` flag (3.27), not a manifest field. |
| `decisions.entity`, `decisions.project` | str or null, **gate** (`null`) | U4. When set, each fully matches `[A-Za-z0-9][A-Za-z0-9_.-]{0,127}`. |
| `decisions.proposer_permission_mode` | `"unset"`, `"upstream"`, `"restricted"`, `"contained"`, **gate** (`"unset"`) | U6; the three modes are defined in 3.19 |
| `decisions.evaltables_backend` | `"unset"`, `"ces"`, `"wandb_table"`, `"off"`, **gate** (`"unset"`) | U8. `ces` falls back to `wandb_table` when the CES feature check fails three times, and the receipt says so. |
| `decisions.publish_approved` | bool, **gate** (`false`) | U11 |
| `decisions.dataset_text_publication` | `"unset"`, `"identifiers_only"`, `"per_dataset"`, **gate** for `export` and `publish` (`"unset"`) | The user's sign-off on what dataset text leaves the machine. `identifiers_only`: every rule under `telemetry.content` must be `hash`. `per_dataset`: the rules under `telemetry.content` apply as written (rule 16 still binds). Amendable after the freeze (rule A below). |
| `decisions.uspto_text_cleared` | bool (`false`) | true only after Legal has answered on the USPTO-50k licence label; without it every USPTO content rule must be `hash` and USPTO table rows carry hashes of prediction and target. Amendable after the freeze. |
| `upstream.repo`, `upstream.commit` | str | commit is 40 hex |
| `upstream.example_dir` | str | path to `EX`, relative to the manifest file |
| `upstream.tree_sha256` | str or null | digest of the pinned upstream file set (3.14). `null` refuses `--freeze`; `validate-manifest --pin-upstream` prints it. |
| `upstream.worker_lock_sha256` | str or null | SHA-256 of `SC/worker-image/uv.lock`; `null` refuses live commands that run jobs |
| `upstream.worker_python` | str | interpreter for local executors and for `benchmark.py --frontier/--results`; relative to the manifest file; Python 3.11 or later (the passthrough uses `-P`); see the path rule below the table |
| `upstream.expect.*` | object | fixed parameters asserted against `WS/config.yaml` and the first planned job: `model`, `model_short`, `mode`, `num_epochs`, `temperature`, `seed`, `max_tokens`, `system_prompt`, `datasets`, `dataset_dirs`, `baselines`, `sizes` |
| `search.iterations` | int >= 0 | the last upstream iteration number (3.20) |
| `search.propose_timeout_s` | int | upstream default 2400 (`meta_harness.py` L164). Counted on a monotonic clock and paused while the CLI reports a rate-limit wait (3.19) |
| `search.max_candidates_per_iteration` | int >= 1 | candidates beyond this are rejected in `validate_candidates` (deviation V17) |
| `search.partial_candidate_policy` | `"upstream"` or `"quarantine"` | 3.18 |
| `routing.route` | `2` or `1` | F2 |
| `routing.route1_model` | str | `openai/openai/gpt-oss-120b` |
| `routing.reply_reuse_scope` | `"search"` or `"job"` | F5: the scope of measured jobs. Two more scope kinds exist and are not chosen here: `qualification` (every qualification job) and `repeat` (every job that carries a repeat index); `wire.REUSE_SCOPES` (3.7) |
| `provider.kind` | `"forge"` or `"stub"` | `stub` is refused by every live command |
| `provider.base_url` | str | `forge`: exactly `https://api.inference.wandb.ai/v1`. `stub`: `http://127.0.0.1:<port>/v1`. |
| `provider.wire_model` | str | the only accepted `model` value; `openai/gpt-oss-120b` |
| `provider.api_key_env` | str | NAME of the variable holding the Forge key in the runner process; fully matches `[A-Z][A-Z0-9_]{2,63}`; must not be `WANDB_API_KEY`, `OPENROUTER_API_KEY` or `ANTHROPIC_API_KEY`; must differ from the other two key variable names (rule 8) |
| `provider.openai_project` | str or null | sent as `OpenAI-Project`; `null` means `<decisions.entity>/<decisions.project>`; printable ASCII |
| `provider.user_agent` | str | non-empty printable ASCII; must not start with `Python-urllib` or `python-requests` |
| `provider.connect_timeout_s`, `first_byte_timeout_s`, `idle_timeout_s`, `attempt_deadline_s` | int | 20, 420, 120, 900; provisional until qualification |
| `provider.max_response_bytes` | int | abort a stream beyond this |
| `provider.expected_response_model` | str or null | model-identity gate; `null` records only |
| `provider.retry.max_transport_retries` | int | 2 (H2) |
| `provider.retry.backoff_429_s`, `backoff_5xx_s`, `backoff_other_s` | `[lo, hi]` | uniform jitter ranges in seconds |
| `provider.circuit.failures`, `provider.circuit.cooldown_s` | int | 3, 60 (H2) |
| `provider.terminal_429_patterns` | [str] | lower-cased substrings; a 429 whose `error.code` or message contains one is terminal (the quota is exhausted), not a rate limit (3.9). `["quota"]` |
| `prices.model`, `input_per_million`, `cached_per_million`, `output_per_million` | str | all three prices required; `prices.model == provider.wire_model`; `cached_per_million <= input_per_million` (otherwise a cost could exceed its reservation) |
| `prices.source_url`, `prices.checked_utc` | str | |
| `gateway.max_in_flight` | int 1..64 | global semaphore (D9: 16) |
| `gateway.max_request_bytes` | int >= 2750000 | raw HTTP body bound. On Route 2 LiteLLM ASCII-escapes the body: 6 bytes per BMP character (1,344,212 bytes observed for a maximal CJK prompt) and 12 per astral character [A-U E7] |
| `gateway.max_content_bytes` | int >= 900000 | unescaped UTF-8 bytes of all message text. Upstream's truncated prompt is 224,023 characters (the cap plus the cut marker [A-U E7]; `llm.py` L53), at up to 4 bytes per character |
| `gateway.max_output_tokens` | int | must equal `upstream.expect.max_tokens` or more |
| `gateway.model_context_tokens`, `gateway.template_overhead_tokens` | int | 131072, 8192 |
| `gateway.max_messages` | int >= 2 | 8. More messages in one request are refused (upstream sends 2); bounds the per-message template cost in the reservation |
| `gateway.per_message_overhead_tokens` | int | 16. Template tokens allowed per message in `input_limit` (5 measured offline with the Harmony encoding for a one-byte message [A-M, A-S]; the server's figure is measured in PF5) |
| `gateway.max_requests_per_job` | int | F6: worker requests forwarded to the provider per job attempt (retries by the gateway and rejected attempts do not count). Hard bound across all attempts of a `job_key`: twice this number of charged provider attempts (3.6) |
| `gateway.max_job_usd` | decimal str | F6: cap on committed spend per `job_key`, all attempts together; at least `2 * max_in_flight * R` |
| `gateway.max_refused_records_per_job` | int | 200. Refusals beyond this per job attempt are counted but write no call record; at ten times this number the worker is killed (3.10) |
| `gateway.loopback_backlog` | int | 128 |
| `budgets.solver_usd.total` | decimal str | |
| `budgets.solver_usd.phases.{qualification,calibration,baseline,search,test,controls,recovery}` | decimal str | sum must not exceed the total |
| `budgets.sandbox_usd` | decimal str | cap on the sandbox meter |
| `budgets.lens_ingest_mb` | int | cap on planned Agent Lens bytes; 0 refuses Agent Lens export. No server-side cap exists [r2] |
| `budgets.lens_ingest_split_mb.{main,preflight,recovery}` | int | the three buckets of that cap; they sum to it. An export draws from `recovery` when it is export attempt 2 or later, else from `preflight` in a preflight campaign and from `main` in a measured one (3.25) |
| `budgets.lens_proposer_session_mb` | int | 4. A `proposer:` unit whose planned bytes exceed this is not exported (`skipped_budget`) |
| `budgets.wandb_upload_gb` | int | 2. Cap on bytes the exporter uploads to W&B (Artifact files and table payloads) |
| `budgets.eval_tables_max` | int | 250. Cap on the number of EvalTables logged (186 validation tables plus 18 to 30 test tables are expected [r2]) |
| `budgets.proposer_list_usd` | decimal str or null | cumulative cap on proposer spend. The login is a usage-billed seat, so the figure is an estimate of **real** spend at list prices, not a notional one [r2]. The driver enforces it from each session's `result.total_cost_usd`, with the fallbacks of 3.19; `null` refuses `search` |
| `accounting.missing_usage` | `"charge_reservation"` | only value |
| `accounting.forecast_contingency` | decimal str | `"1.25"` |
| `accounting.reconcile_tolerance` | decimal str | `"0.10"`. The reconciliation gate of 8.2 passes when our ledger and the platform's usage figure differ by at most this fraction, or by at most $0.05 |
| `forecast.*` | object | planning constants read by `qualification.forecast` (3.22): `candidates_per_iteration`, `test_systems`, `output_tokens_quantile`, `calls_per_job`, `input_tokens_per_job`, `job_seconds` |
| `calibration.systems` | [str] or null | systems whose test jobs `evaluate --calibration` runs; `null` = `upstream.expect.baselines` |
| `calibration.gates.<name>` | object | one calibration gate. `system`: the system it judges (one of the calibration systems). `hard`: a failed hard gate makes the verdict `fail`; a failed soft gate adds a flag. `macro_test_accuracy`: `[lo, hi]` in percent, macro average over the datasets. `correct`: `{dataset: [lo, hi]}`, bounds on the number of correct test examples. `content_on_every_call`: require that every call of the system's test jobs returned content (no empty reply, no `finish_reason: length`, no empty prediction). The draft holds the protocol's G1 (hard, `no_memory`) and G2 (soft, released `fewshot_all`) with the bands of Appendix G. No gate uses a figure from the paper's few-shot baseline: the paper's few-shot (all) carries 49.3 K characters and the released one 22.9 K [r2] |
| `calibration.gate_search` | bool | when true, `search` refuses until a calibration record with verdict `pass` exists (in this `STATE`, or in the imported campaign's, 3.22) |
| `calibration.noise_repeats` | int >= 1 | 5. How many times each baseline's validation jobs are run for the noise floor; repeat 1 is the campaign's published baseline run, `evaluate --noise` runs repeats 2 to this number (3.20) |
| `executor.kind` | `"sandbox"`, `"local_relay"`, `"local"` | |
| `executor.allow_unisolated_live` | bool | live commands refuse `local` and `local_relay` unless true |
| `executor.max_attempts` | int >= 1 | 1. Candidate-attributable attempts per `(job_key, candidate_sha256)` (3.18): a job-limit timeout or a candidate error is deterministic at temperature 0, and with gateway replay a rerun would silently extend the limit [r2]. Upstream gives every call three (`max_retries=2`, `benchmark.py` L79 [src]); deviation V7 |
| `executor.max_infra_attempts` | int >= 1 | 3. Infrastructure-caused attempts per runner invocation (3.18), `env_setup_failed` included |
| `executor.job_timeout_s` | int | 7200 (upstream, `benchmark.py` L80 [src]). Worker wall-clock measured inside the executor and enforced by ending the worker (in a sandbox, by stopping it); a fidelity parameter signed in protocol stage 2 |
| `executor.parallel_dataset_jobs.{validation,test}` | int | `{"validation": 1, "test": 1}`. How many dataset jobs of one candidate run at once, per split. `test` is always 1 (rule 19); `validation` may be raised to 3 (upstream's value) after qualification shows the queue wait stays under the client's 600 s (8.2 PF7). Deviation V7 |
| `executor.import_check_timeout_s` | int | 30 (upstream, `meta_harness.py` L206 [src]); per name inside the executor |
| `sandbox.env_mode` | `"image"` or `"wheelhouse"` | how a sandbox gets the worker's packages (3.17). `image`: a prebuilt worker image. `wheelhouse`: a stock base image plus the wheelhouse tar uploaded and installed in each sandbox. The values `snapshot` and `install_then_deny` are named and rejected by validation: they are deferred (8.3). No mode mounts a scratch volume |
| `sandbox.image` | str or null | mode `image`: the worker image; must fully match `[a-z0-9.-]+(:[0-9]+)?/.+@sha256:[0-9a-f]{64}` (the amd64 manifest digest) and must not be on `docker.io`; `null` refuses mode `image` |
| `sandbox.base_image` | str or null | mode `wheelhouse`: the stock image, by amd64 manifest digest; same pattern, not on `docker.io`; `null` refuses mode `wheelhouse` |
| `sandbox.base_image_fallback` | str or null | the same digest on another registry host, tried once when the pull of `sandbox.base_image` fails. The registry host is transport, the digest is the identity, so `docker.io` is allowed here and only here; its digest must equal that of `sandbox.base_image` (rule 10) |
| `sandbox.wheelhouse.tar_path` | str | path of the wheelhouse tar, relative to the manifest file |
| `sandbox.wheelhouse.tar_sha256`, `tar_bytes`, `sha256sums_sha256` | str, int, str, or null | SHA-256 and size of the tar, and SHA-256 of its `SHA256SUMS` member; `null` refuses mode `wheelhouse` |
| `sandbox.wheelhouse.requirements_sha256` | str | SHA-256 of `SC/worker-image/requirements.lock.txt` |
| `sandbox.expect.machine`, `sandbox.expect.python` | str | `x86_64`, `3.13`: what every sandbox must prove before the worker starts (`uname -m` and the prefix of the Python version) |
| `sandbox.cpu`, `sandbox.memory` | str | `"2"`, `"4Gi"` |
| `sandbox.auth_env` | str | NAME under which the W&B credential for the sandbox API is read with `--key-source env` (and the name it is recorded under for every key source). `CwSandboxClient` reads it once and passes it to the SDK explicitly (3.17). Same name pattern and reserved names as `provider.api_key_env` |
| `sandbox.auth_mode` | `"headers"` or `"wandb_auth"` | `headers` (default): explicit `cwsandbox.AuthHeaders`, so the SDK imports no `wandb` and consults neither the environment nor a netrc file [live: PF3]. `wandb_auth`: the fallback if PF3 rejects the header set (3.17) |
| `sandbox.price_cpu_hour`, `sandbox.price_gib_hour` | decimal str | `0.128`, `0.0212` |
| `sandbox.lifetime_margin_s`, `sandbox.setup_allowance_s`, `sandbox.create_wait_s` | int | 900, 150, 180. A job sandbox's `max_lifetime_seconds` is `executor.job_timeout_s + lifetime_margin_s`, plus `setup_allowance_s` in mode `wheelhouse` (upload 40 to 90 s and install 30 to 60 s per sandbox, estimates [r2]); it cannot be extended after create |
| `sandbox.work_dir`, `sandbox.python` | str | `/mhf`, `python3` |
| `sandbox.tiktoken_cache_dir` | str or null | `null` (default, every mode): `TIKTOKEN_RS_CACHE_DIR` is the tokenizers directory the selfcheck finds inside the sandbox from where LiteLLM is installed; a string overrides it |
| `sandbox.object_store_endpoint` | str or null | `host:port` of the platform's object-store endpoint when known; the selfcheck probes it and records the result (3.13) |
| `sandbox.copy_back_cache` | bool | whether the worker's reply cache is copied back |
| `sandbox.selfcheck` | `"every_job"` or `"first_job"` | |
| `relay.mode` | `"daemon"` or `"stdio"` | sandbox relay mode (3.12, 3.13) |
| `relay.rotate_after_mb` | int | daemon mode: start a new bridge exec after this much stdout |
| `relay.max_stream_mb` | int | stdio mode: fail the attempt beyond this much stdout of the one exec (the SDK retains it in memory) |
| `relay.max_job_stream_mb` | int | both modes: fail the attempt beyond this much stub-to-host stream volume in total; classified as the job's own ceiling (3.16) |
| `relay.reply_timeout_s` | int | how long the stub waits for a reply before answering `relay_unavailable` |
| `relay.ping_interval_s`, `relay.pong_timeout_s` | int | 20, 60. Liveness of a bridge (3.12); `pong_timeout_s > ping_interval_s` |
| `relay.max_workers`, `relay.write_chunk_bytes`, `relay.done_cache_max` | int | 64, 32768, 256 |
| `relay.reattach_attempts`, `relay.reattach_backoff_s` | int, `[lo, hi]` | daemon mode |
| `capture.enabled` | bool | per-example wrapper on, or plain `python -m text_classification.inner_loop` |
| `capture.max_text_chars` | int | cut for `prediction` and `target` in the sidecar |
| `proposer.cli_path`, `proposer.cli_sha256` | str or null | pinned binary; the hash is verified before each session; `null` refuses `search` |
| `proposer.cli_version` | str or null | the pinned CLI's version (for example `2.1.317`). `init.claude_code_version` must equal it, and the transcript converter refuses records of any other version (3.21); `null` refuses `search` |
| `proposer.model`, `proposer.effort` | str | `opus`, `max` (upstream L171, L178 [src]). `model` may be a pinned id (`claude-opus-4-6`, `claude-opus-5-5`); the alias floats (it is Opus 5.5 today [r2]) |
| `proposer.expected_resolved_model` | str or null | when set, the concrete model id of every main-thread reply must equal it; `null` = the id of the first reply is recorded and every later one must equal that (3.19) |
| `proposer.max_budget_usd_per_session` | decimal str or null | passed as `--max-budget-usd` in every mode; the CLI stops after the response that crosses it. `null` refuses `search` and the live proposer steps of the preflight |
| `proposer.grace_s` | `[int, int]` | seconds after SIGINT before SIGTERM, then before SIGKILL |
| `proposer.env_allow` | [str] | names copied from the runner's environment when present |
| `proposer.env_set` | {str: str} | names set explicitly; placeholders `{example_venv}`, `{workspace_root}`, `{uv_dir}`. The closed list of 3.5 (`procenv.PROPOSER_FIXED_ENV`) is added by code and may not be named here |
| `proposer.path_dirs` | [str] | `PATH` entries, same placeholders |
| `proposer.scratch_root` | str | absolute path, in resolved form, outside `SC`; each session's scratch directory is `<scratch_root>/<campaign>/<session_key>` (3.19). `/private/tmp/mhf-proposer` |
| `proposer.restricted.settings` | object or null | optional override, deep-merged over the settings file that 3.19 generates for modes `restricted` and `contained`; `null` = the generated file as it is |
| `proposer.usage_limit_patterns`, `proposer.auth_patterns` | [str] | lower-cased substrings matched against stderr and the `result` event's text. They are the **fallback** of the limit classifier, used only for a session that does not qualify as `ok` and that carried no structured limit signal (3.19) |
| `proposer.transient_retries`, `proposer.transient_backoff_s` | int, `[lo, hi]` | 2, `[60, 300]`: reruns of the same iteration after a transient failure, and the wait before each |
| `proposer.max_limit_wait_s` | int | 21600. The longest wait for a plan-window reset the driver sits out; a later reset stops the campaign and asks |
| `proposer.session_estimate_usd` | decimal str or null | estimate of one session's real cost; used by the forecast and as the last fallback when a session's cost cannot be measured; `null` refuses `search`. The preflight campaign carries a provisional figure (the forecast of PF7 and the `search` of PF10 need one); the measured campaign's figure is the cost PF10 measured |
| `proposer.list_prices` | {model id: {`input_per_million`, `output_per_million`, `cache_read_per_million`, `cache_write_5m_per_million`, `cache_write_1h_per_million`}} | decimal strings; used only to price a session that left neither a `result` event nor a `cost-state` record (3.19) |
| `proposer.max_transcript_bytes` | int | copy cap |
| `telemetry.api_key_env` | str | NAME of the variable the exporter process reads; same pattern and reserved names as `provider.api_key_env` |
| `telemetry.export_mode` | `"manual"` or `"after_unit"` | 3.25 |
| `telemetry.export_timeout_s` | int | limit for one exporter child |
| `telemetry.attribute_prefix` | str | `metaharness`; fully matches `[a-z][a-z0-9_]*` |
| `telemetry.agent_lens.enabled` | bool | |
| `telemetry.agent_lens.service_name` | str | printable ASCII |
| `telemetry.agent_lens.max_spans_per_conversation` | int <= 199 | 180 |
| `telemetry.agent_lens.loop_iterations_per_conversation` | int >= 1 | 15. The outer loop starts a new conversation every this many iterations; `loop_iterations_per_conversation * (3 + 2 * search.max_candidates_per_iteration) <= max_spans_per_conversation` (rule 13) |
| `telemetry.agent_lens.readback_timeout_s`, `readback_poll_s`, `export_batch_size` | int | 60, 2, 32 |
| `telemetry.agent_lens.byte_estimate_factor` | decimal str | `"1.00"`. Multiplier on the planned bytes of a span plan; set from PF8 when the stored spans differ from the estimate by more than 10% |
| `telemetry.content.<dataset>.{span_prompt, span_reply, span_reasoning, tool_result}` | str rules | one content rule set per dataset of `upstream.expect.datasets` (3.24). Each value is `hash`, `truncate:N` or `full`. Default `hash` everywhere. Amendable after the freeze (rule A below) |
| `telemetry.verbatim_scan.run_chars`, `exact_datasets` | int, [str] | 40, `["USPTO"]`: the verbatim dataset-text scan of 3.24 flags a run of this many characters of a protected dataset's question text; for the listed datasets it flags any whole question or target string instead (92 of 180 USPTO product strings are shorter than 40 characters [r2]) |
| `telemetry.wandb_group` | str or null | `null` = the campaign |
| `qualification.system`, `qualification.dataset` | str | `no_memory`, `USPTO` |
| `qualification.max_truncated_empty_rate` | number | 0.05 |
| `qualification.import_from` | str or null | measured campaigns only: the campaign name of a preflight campaign under the same `SC` whose qualification this campaign imports (3.22); `null` = this campaign qualifies itself |
| `runner.keep_awake` | bool | live only: `caffeinate -is -w <pid>` on macOS (`-s` holds off system sleep on mains power) |
| `runner.stall_alarm_s` | int | 10800. `status` raises a stall alarm when the journal has not advanced for this long while a runner holds the lock |
| `runner.sleep_gap_s` | int | 60. A wall-clock step that exceeds the monotonic step by more than this, between two heartbeats of a wait loop, is a host sleep (3.16) |

**Path rule for `upstream.worker_python`** (five modules use this path; they all follow this rule). Make it absolute with `os.path.abspath(os.path.join(manifest_dir, value))`. Never call `Path.resolve()` or `os.path.realpath` on it: `EX/.venv/bin/python` is a symlink to the uv-managed base interpreter, which has no `litellm` [A-U E8]. The venv root is `Path(abs).parent.parent` (lexical) and must contain `pyvenv.cfg`. `procenv.find_tokenizer_dir` globs `<venv root>/lib/python*/site-packages/litellm/litellm_core_utils/tokenizers`; the `{example_venv}` placeholder, `workspace.env_fingerprint` and `procenv.passthrough_env` use the same venv root. `procenv.venv_root(python)` is the one function that computes it. `upstream.example_dir` is made absolute the same way (`abspath`, no symlink resolution needed but none applied).

**Filled draft** (`SC/manifest.draft.json`). It validates. It cannot be frozen until `upstream.tree_sha256` is filled. `offline-smoke` builds its own manifest from it. Every live command refuses it (`account-check --live` too: entity and project are unset).

```json
{
  "schema": "mhforge.manifest/1",
  "notes": "DRAFT. Every decision that belongs to the user is unset, so every live command refuses. Budget figures are the second-round recommendation and take effect only when decisions.live_spend_approved is true.",
  "campaign": "mh-forge-draft",
  "campaign_kind": "preflight",
  "state_dir": "state/mh-forge-draft",
  "decisions": {
    "live_spend_approved": false,
    "key_provisioning": "unset",
    "entity": null,
    "project": null,
    "proposer_permission_mode": "unset",
    "evaltables_backend": "unset",
    "publish_approved": false,
    "dataset_text_publication": "unset",
    "uspto_text_cleared": false
  },
  "upstream": {
    "repo": "https://github.com/stanford-iris-lab/meta-harness",
    "commit": "f805ad3aa4ada290458c1e0deadbe132fbbdea4e",
    "example_dir": "../reference_examples/text_classification",
    "tree_sha256": null,
    "worker_lock_sha256": null,
    "worker_python": "../reference_examples/text_classification/.venv/bin/python",
    "expect": {
      "model": "openrouter/openai/gpt-oss-120b",
      "model_short": "gpt-oss-120b",
      "mode": "offline", "num_epochs": 1, "temperature": 0.0, "seed": 42,
      "max_tokens": 16384, "system_prompt": "Reasoning: medium",
      "datasets": ["USPTO", "Symptom2Disease", "LawBench"],
      "dataset_dirs": {"USPTO": "uspto", "Symptom2Disease": "symptom_diagnosis", "LawBench": "crime_prediction"},
      "baselines": ["no_memory", "fewshot_all"],
      "sizes": {"USPTO": [50, 30, 100], "Symptom2Disease": [200, 50, 212], "LawBench": [200, 50, 100]}
    }
  },
  "search": {"iterations": 20, "propose_timeout_s": 2400, "max_candidates_per_iteration": 3, "partial_candidate_policy": "upstream"},
  "routing": {"route": 2, "route1_model": "openai/openai/gpt-oss-120b", "reply_reuse_scope": "search"},
  "provider": {
    "kind": "forge",
    "base_url": "https://api.inference.wandb.ai/v1",
    "wire_model": "openai/gpt-oss-120b",
    "api_key_env": "MHFORGE_FORGE_API_KEY",
    "openai_project": null,
    "user_agent": "mhforge-gateway/0.1 (meta-harness forge showcase)",
    "connect_timeout_s": 20, "first_byte_timeout_s": 420, "idle_timeout_s": 120, "attempt_deadline_s": 900,
    "max_response_bytes": 2097152,
    "expected_response_model": null,
    "retry": {"max_transport_retries": 2, "backoff_429_s": [20, 40], "backoff_5xx_s": [30, 60], "backoff_other_s": [2, 8]},
    "circuit": {"failures": 3, "cooldown_s": 60},
    "terminal_429_patterns": ["quota"]
  },
  "prices": {
    "model": "openai/gpt-oss-120b",
    "input_per_million": "0.03", "cached_per_million": "0.03", "output_per_million": "0.17",
    "source_url": "https://coreweave.com/forge-pricing/inference-agents",
    "checked_utc": "2026-11-10T00:03:00.000000Z"
  },
  "gateway": {
    "max_in_flight": 16,
    "max_request_bytes": 4000000,
    "max_content_bytes": 1048576,
    "max_output_tokens": 16384,
    "model_context_tokens": 131072,
    "template_overhead_tokens": 8192,
    "max_messages": 8,
    "per_message_overhead_tokens": 16,
    "max_requests_per_job": 1000,
    "max_job_usd": "1.00",
    "max_refused_records_per_job": 200,
    "loopback_backlog": 128
  },
  "budgets": {
    "solver_usd": {"total": "60.00",
                   "phases": {"qualification": "3.00", "calibration": "8.00", "baseline": "3.00", "search": "28.00",
                              "test": "5.00", "controls": "3.00", "recovery": "10.00"}},
    "sandbox_usd": "40.00",
    "lens_ingest_mb": 175,
    "lens_ingest_split_mb": {"main": 150, "preflight": 5, "recovery": 20},
    "lens_proposer_session_mb": 4,
    "wandb_upload_gb": 2,
    "eval_tables_max": 250,
    "proposer_list_usd": null
  },
  "accounting": {"missing_usage": "charge_reservation", "forecast_contingency": "1.25", "reconcile_tolerance": "0.10"},
  "forecast": {
    "candidates_per_iteration": 3, "test_systems": 6, "output_tokens_quantile": 0.95,
    "calls_per_job": {"val": {"USPTO": 30, "Symptom2Disease": 50, "LawBench": 50},
                      "test": {"USPTO": 100, "Symptom2Disease": 212, "LawBench": 100}},
    "input_tokens_per_job": {"val": {"USPTO": 140494, "Symptom2Disease": 369420, "LawBench": 1213069},
                             "test": {"USPTO": 468289, "Symptom2Disease": 1566127, "LawBench": 2469033}},
    "job_seconds": {"val": {"USPTO": 600, "Symptom2Disease": 600, "LawBench": 600},
                    "test": {"USPTO": 600, "Symptom2Disease": 600, "LawBench": 600}}
  },
  "calibration": {
    "systems": null,
    "gates": {
      "G1": {"system": "no_memory", "hard": true, "macro_test_accuracy": [23.4, 33.3],
             "correct": {"Symptom2Disease": [118, 156], "USPTO": [0, 21], "LawBench": [0, 15]},
             "content_on_every_call": true},
      "G2": {"system": "fewshot_all", "hard": false, "macro_test_accuracy": [35.9, 47.3],
             "correct": {"Symptom2Disease": [166, 194], "USPTO": [6, 24], "LawBench": [13, 37]},
             "content_on_every_call": false}
    },
    "gate_search": true,
    "noise_repeats": 5
  },
  "executor": {"kind": "sandbox", "allow_unisolated_live": false, "max_attempts": 1, "max_infra_attempts": 3,
               "job_timeout_s": 7200, "parallel_dataset_jobs": {"validation": 1, "test": 1}, "import_check_timeout_s": 30},
  "sandbox": {
    "env_mode": "wheelhouse",
    "image": null,
    "base_image": "public.ecr.aws/docker/library/python@sha256:f3dac913ab53b78c64f26d5518fcd7043d5a1a46522d6a493b598c3ed12f62a6",
    "base_image_fallback": "docker.io/library/python@sha256:f3dac913ab53b78c64f26d5518fcd7043d5a1a46522d6a493b598c3ed12f62a6",
    "wheelhouse": {"tar_path": "worker-image/wheelhouse/wheelhouse.tar", "tar_sha256": null, "tar_bytes": null,
                   "sha256sums_sha256": null,
                   "requirements_sha256": "4698f162b4a77cb251f492b930029976b6f573478faa69663f56a73bd73a7ade"},
    "expect": {"machine": "x86_64", "python": "3.13"},
    "cpu": "2", "memory": "4Gi", "auth_env": "MHFORGE_SANDBOX_API_KEY", "auth_mode": "headers",
    "price_cpu_hour": "0.128", "price_gib_hour": "0.0212",
    "lifetime_margin_s": 900, "setup_allowance_s": 150, "create_wait_s": 180,
    "work_dir": "/mhf", "python": "python3", "tiktoken_cache_dir": null, "object_store_endpoint": null,
    "copy_back_cache": false, "selfcheck": "every_job"
  },
  "relay": {"mode": "daemon", "rotate_after_mb": 64, "max_stream_mb": 512, "max_job_stream_mb": 1024,
            "reply_timeout_s": 3600, "ping_interval_s": 20, "pong_timeout_s": 60,
            "max_workers": 64, "write_chunk_bytes": 32768, "done_cache_max": 256,
            "reattach_attempts": 8, "reattach_backoff_s": [1, 30]},
  "capture": {"enabled": true, "max_text_chars": 20000},
  "proposer": {
    "cli_path": null, "cli_sha256": null, "cli_version": null,
    "model": "opus", "expected_resolved_model": null, "effort": "max",
    "max_budget_usd_per_session": null,
    "grace_s": [60, 20],
    "env_allow": ["HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TERM"],
    "env_set": {"VIRTUAL_ENV": "{example_venv}", "UV_PROJECT_ENVIRONMENT": "{example_venv}", "UV_NO_SYNC": "1", "UV_FROZEN": "1"},
    "path_dirs": ["{example_venv}/bin", "{uv_dir}", "/usr/bin", "/bin", "/usr/sbin", "/sbin"],
    "scratch_root": "/private/tmp/mhf-proposer",
    "restricted": {"settings": null},
    "usage_limit_patterns": ["spend limit", "usage limit", "budget", "limit reached", "resets at"],
    "auth_patterns": ["not logged in", "please run /login", "login expired", "authentication_failed"],
    "transient_retries": 2, "transient_backoff_s": [60, 300], "max_limit_wait_s": 21600,
    "session_estimate_usd": null,
    "list_prices": {
      "claude-opus-5-5": {"input_per_million": "4", "output_per_million": "20", "cache_read_per_million": "0.20",
                          "cache_write_5m_per_million": "5", "cache_write_1h_per_million": "8"},
      "claude-opus-4-6": {"input_per_million": "5", "output_per_million": "25", "cache_read_per_million": "0.50",
                          "cache_write_5m_per_million": "6.25", "cache_write_1h_per_million": "10"}
    },
    "max_transcript_bytes": 268435456
  },
  "telemetry": {
    "api_key_env": "MHFORGE_EXPORT_API_KEY",
    "export_mode": "manual",
    "export_timeout_s": 900,
    "attribute_prefix": "metaharness",
    "agent_lens": {"enabled": true, "service_name": "mhforge-showcase", "max_spans_per_conversation": 180,
                   "loop_iterations_per_conversation": 15,
                   "readback_timeout_s": 60, "readback_poll_s": 2, "export_batch_size": 32,
                   "byte_estimate_factor": "1.00"},
    "content": {
      "USPTO": {"span_prompt": "hash", "span_reply": "hash", "span_reasoning": "hash", "tool_result": "hash"},
      "Symptom2Disease": {"span_prompt": "hash", "span_reply": "hash", "span_reasoning": "hash", "tool_result": "hash"},
      "LawBench": {"span_prompt": "hash", "span_reply": "hash", "span_reasoning": "hash", "tool_result": "hash"}
    },
    "verbatim_scan": {"run_chars": 40, "exact_datasets": ["USPTO"]},
    "wandb_group": null
  },
  "qualification": {"system": "no_memory", "dataset": "USPTO", "max_truncated_empty_rate": 0.05, "import_from": null},
  "runner": {"keep_awake": true, "stall_alarm_s": 10800, "sleep_gap_s": 60}
}
```

Why these numbers.

- Solver caps follow the second-round budget table ($60 = 3 qualification + 8 calibration and noise + 3 baseline + 28 search + 5 test + 3 controls + 10 recovery) [r2]. Expected spend is far lower (search $1 to $6 at 1,000 to 2,000 output tokens per call; gates and noise about $1; the sampling control about $1.20); the caps cover the case of every reply at 16,384 tokens. Re-derive them after PF7.
- `budgets.sandbox_usd` $40: the central estimate is $7.50, a slow, learning-heavy search about $30, and wheelhouse setup adds $1.3 to $2.6 over 186 sandboxes [r2]; the $25 of round one did not cover the slow case.
- `budgets.lens_ingest_mb` 175 (150 main, 5 preflight, 20 recovery): with every dataset on rule `hash` the solver conversations are about 28 MB, the test phase 3 MB, the proposer 20 to 60 MB and the outer loop under 1 MB [r2]; full prompts would be 92 to 664 MB. `budgets.lens_proposer_session_mb` 4 bounds one session. Ingestion is billed per megabyte beyond the plan's allowance and there is no server-side cap, so the exporter's meter is the only guard.
- `budgets.wandb_upload_gb` 2 and `budgets.eval_tables_max` 250: under 1 GB of uploads is expected; 200 tables would be passed by the test tables [r2]. The billing treatment of CES tables is unknown.
- `executor.max_attempts` 1 and `executor.max_infra_attempts` 3: a deterministic failure repeats, and with gateway replay a rerun of a timed-out job would silently extend the job limit [r2].
- The largest possible reservation is `R = price(model_context_tokens input, max_output_tokens output)` = 0.03 x 131,072 / 1e6 + 0.17 x 16,384 / 1e6 = $0.006718. A reservation is made only while a semaphore slot is held (3.10), so at most 16 are open: $0.11. Validation requires every non-zero phase cap to be at least `2 * max_in_flight * R` ($0.215), so open reservations alone can never exhaust a cap.
- `max_requests_per_job` 1000 covers 200 learning calls plus two validation passes of 50 examples at several calls each [inf]. It counts worker requests per job attempt, so a burst of provider 429s or a rerun cannot use it up; the hard bounds across attempts are 2,000 charged provider attempts and `max_job_usd`.
- `max_job_usd` $1.00 is about ten times the largest forecast job (LawBench test: 2.47 M input tokens at $0.03 per million plus 100 replies) and well above 16 open reservations ($0.11); 1,000 maximal reservations would otherwise allow $6.72 for one job [A-M].
- `max_messages` 8 and `per_message_overhead_tokens` 16: upstream sends two messages; a request of a few thousand one-byte messages renders to more prompt tokens than its bytes plus 8,192 [A-M m3, A-S e_tokens], which would have contradicted the reservation.
- `forecast.job_seconds` 600 is the findings' planning figure (186 ten-minute sandboxes) [rep]; the forecast takes the larger of this and a figure scaled from the qualification job.
- `relay.max_job_stream_mb` 1024 is about 200 times the stream volume of the largest baseline job (roughly 5 MB of request text) [inf].
- `forecast.input_tokens_per_job` holds the measured `fewshot_all` volumes [rep CM, CM-V]; `forecast.test_systems` is the assumed number of systems upstream picks for the test pass (`meta_harness.py` L301-L308).
- Provider timeouts: 420 s to first byte covers the 276 s P90 time to first token at concurrency 32 on 100,000-token prompts; 900 s covers 380 s for a full 16,384-token reply plus that wait [rep SI-V]. All four are provisional until the qualification job.
- Every content rule is `hash` by default: ingestion is billed and a few-shot candidate carries 7 to 12 MB of prompt text [rep critic gap 4], LawBench case text is not cleared for publication at all and USPTO text waits for Legal (Appendix G) [r2]. The user may open Symptom2Disease after signing `decisions.dataset_text_publication`, also after the freeze (rule A).

**Validation rules** (`manifest.validate`; each failure is one message that names the path).

1. Types, enums and required keys per the table; unknown keys rejected.
2. `campaign` pattern; `state_dir` equals the literal `state/<campaign>`. `SC` in every path rule is `manifest_path.resolve().parent`, the directory that holds the manifest file, so a manifest in a temp tree validates and its state lands beside it. `paths.resolve_state` applies the same rule; no other module computes the state path.
3. `provider.kind == "forge"` requires the exact base URL; `"stub"` requires host `127.0.0.1` and scheme `http`.
4. `prices.model == provider.wire_model`; all three prices parse as non-negative decimals; `cached_per_million <= input_per_million`.
5. Phase caps sum to at most the total; every non-zero phase cap, and `gateway.max_job_usd`, is at least `2 * gateway.max_in_flight * R`.
6. `gateway.max_output_tokens >= upstream.expect.max_tokens`; `gateway.max_content_bytes >= 900000`; `gateway.max_request_bytes >= 2750000`; `gateway.max_messages >= 2`.
7. `telemetry.agent_lens.max_spans_per_conversation <= 199`; attribute prefix pattern.
8. The three key variable names (`provider.api_key_env`, `telemetry.api_key_env`, `sandbox.auth_env`) match the name pattern, are pairwise distinct (with `--key-source env` each component deletes its own variable, which another would otherwise need), are not one of the three reserved names, and appear in neither `proposer.env_allow` nor `proposer.env_set`.
9. `proposer.env_allow` and `proposer.env_set` names: none is credential-shaped under `procenv.is_credential_name` (the rule of 3.13); none is `SSH_AUTH_SOCK`, `NETRC`, `BASH_ENV`, `ENV`, `NODE_OPTIONS` or `KUBECONFIG`; none ends in `_PROXY` (any letter case); none starts with `OPENROUTER_`, `OPENAI_`, `WANDB_`, `ANTHROPIC_`, `CLAUDE`, `OTEL_`, `TEXT_CLASSIFICATION_`, `MHFORGE_`, `LITELLM_`, `DYLD_`, `LD_`, `AWS_`, `GOOGLE_`, `AZURE_`, `GH_`, `HF_` or `DOCKER_`; none is on `procenv.PROPOSER_DENY_NAMES`; and none is a name of `procenv.PROPOSER_FIXED_ENV` (3.5). The rule stays as strict as it was: the two `CLAUDE_CODE_*` names and `DISABLE_AUTOUPDATER` that a proposer session needs are added by `procenv.proposer_env` from that closed list, never through the manifest.
10. `sandbox.env_mode` is `image` or `wheelhouse`; `snapshot` and `install_then_deny` are rejected with the message `deferred (8.3)`. `sandbox.image` and `sandbox.base_image`, when not null, match the digest pattern and are not on `docker.io`. `sandbox.base_image_fallback`, when not null, matches the digest pattern, may be on `docker.io`, and carries the same `sha256:` digest as `sandbox.base_image`. `sandbox.wheelhouse.tar_sha256`, `sha256sums_sha256` and `requirements_sha256`, when not null, are 64 hex; `tar_bytes`, when not null, is a positive integer.
11. No manifest string value matches a `mhforge.scan` pattern (a key pasted into the manifest is refused).
12. `decisions.entity` and `decisions.project`, when set, match their pattern; `provider.user_agent`, `provider.openai_project` and `telemetry.agent_lens.service_name` are printable ASCII (0x20 to 0x7E): a control character in a header value makes `http.client` raise on every request.
13. `telemetry.agent_lens.loop_iterations_per_conversation * (3 + 2 * search.max_candidates_per_iteration) <= telemetry.agent_lens.max_spans_per_conversation`.
14. `relay.pong_timeout_s > relay.ping_interval_s > 0`; `relay.max_job_stream_mb >= relay.rotate_after_mb`.
15. Each `calibration.gates.<name>`: `system` is one of the calibration systems; `macro_test_accuracy` is `[lo, hi]` with `0 <= lo <= hi <= 100`; every key of `correct` is one of `upstream.expect.datasets` and its value is `[lo, hi]` with `0 <= lo <= hi <=` that dataset's test size. `calibration.gate_search` requires at least one gate with `hard: true`. `calibration.noise_repeats >= 1`.
16. `telemetry.content` has exactly the keys of `upstream.expect.datasets`, each with exactly the four fields of `wire.CONTENT_FIELDS`, each `hash`, `full` or `truncate:N` with `N >= 200`. Every rule of a dataset named in `wire.NEVER_PUBLISH_TEXT` (`LawBench`) is `hash`. Every rule of a dataset named in `wire.CLEARANCE_GATED` (`USPTO`) is `hash` unless the decision that table names for it (`decisions.uspto_text_cleared`) is true. With `decisions.dataset_text_publication == "identifiers_only"` every rule is `hash`. `telemetry.verbatim_scan.run_chars >= 20`; `exact_datasets` is a subset of the datasets.
17. `campaign_kind == "preflight"` requires `qualification.import_from` null. `qualification.import_from`, when set, fully matches the campaign pattern and differs from `campaign`.
18. `budgets.lens_ingest_split_mb` has exactly the keys `main`, `preflight`, `recovery`, non-negative, summing to `budgets.lens_ingest_mb`; `budgets.lens_proposer_session_mb >= 1`; `budgets.wandb_upload_gb >= 1`; `budgets.eval_tables_max >= 1`; `accounting.reconcile_tolerance` parses as a decimal in `0..1`.
19. `executor.parallel_dataset_jobs` has exactly the keys `validation` and `test`; `test == 1`; `1 <= validation <= 3`.
20. `proposer.max_budget_usd_per_session`, when set, is a positive decimal and, when `budgets.proposer_list_usd` is set, not above it. `proposer.scratch_root` is an absolute path without `..` that is not inside the manifest's directory. `proposer.cli_version`, when set, fully matches `[0-9]+(\.[0-9]+){1,3}`. `proposer.restricted.settings`, when set, is a JSON object. `proposer.transient_retries >= 0`; `proposer.max_limit_wait_s >= 0`.
21. `telemetry.agent_lens.byte_estimate_factor` parses as a decimal in `0.5..4`. `runner.stall_alarm_s > 0`; `runner.sleep_gap_s > 0`.

**Rule A: amendments after the freeze** (`manifest.append_amendment`, 3.3; command `amend`, 3.27). The fields that affect only what is exported can change without a new campaign and without re-qualification: every path under `telemetry.content.`, `decisions.dataset_text_publication` and `decisions.uspto_text_cleared` (`manifest.AMENDABLE`). An amendment is one row appended to `STATE/amendments.jsonl`:

```json
{"schema": "mhforge.amendment/1", "seq": 1, "utc": "...", "prev_sha256": "<manifest_sha256 of the frozen manifest for seq 1; else row_sha256 of the previous row>",
 "set": {"decisions.dataset_text_publication": "per_dataset", "telemetry.content.Symptom2Disease.span_prompt": "full"},
 "note": "signed off by <who>, <date>", "row_sha256": "<sha256 of canonical_json of this row without row_sha256>"}
```

The file is append-only and hash-chained to the frozen manifest, so a row cannot be changed, removed or moved to another campaign without the chain breaking (`ManifestError`). `manifest.effective(manifest, amendments)` applies the rows in order to a copy of the frozen manifest and validates the result with rules 1 to 21 (LawBench cannot be opened by an amendment either). Only `export` and `publish` use the effective manifest; the runner commands, the qualification record and every `manifest_sha256` comparison use the frozen one. Each receipt records the `seq` and `row_sha256` of the latest amendment it was made under (2.16). Loosening a rule after spans were sent does not rewrite them: the changed plan makes the receipt stale and `export --supersede` sends a new copy under new conversation ids (3.25); whether Agent Lens spans can be deleted is unknown [r2].

**Live gates** (`manifest.live_blockers(manifest, command, environ, paths, ...)`, signature in 3.3; it writes nothing and reads only state files; returns `[(field, message)]`; empty = allowed). Key rows: with `--key-source env` a key is "supplied" when the variable named in the manifest is present and non-empty in `environ` (the test is `bool(environ.get(name))`; the value is never copied or printed). With `--key-source prompt` or `fd:N` the key rows are skipped here; the CLI reads the keys after every other gate has passed and refuses, naming the role, when one is empty (3.27). No command falls back to an SDK's own credential chain: a missing key refuses.

| Command | Blockers checked |
|---|---|
| `account-check` | `--live` given; the manifest validates (a frozen copy is **not** required, so the account can be checked before any freeze); `decisions.entity` and `decisions.project` non-null; the `provider` key supplied, and with `executor.kind == "sandbox"` the `sandbox` key (PF3 lists sandboxes); `--create-project` also needs `decisions.live_spend_approved` (it is the first remote write) |
| every other live command | `--live` given; a frozen copy exists and equals the file; `decisions.live_spend_approved` true; `decisions.key_provisioning == "dedicated"`; `decisions.entity` and `decisions.project` non-null; `provider.kind == "forge"`; `upstream.tree_sha256` non-null |
| `preflight`, `search`, `evaluate` | no halt row in the ledger (cleared only by `status --clear-halt`); the `provider` key supplied; `executor.kind == "sandbox"` or `executor.allow_unisolated_live`; with `sandbox`: the `sandbox` key supplied, `upstream.worker_lock_sha256` non-null, in mode `image` `sandbox.image` non-null, in mode `wheelhouse` `sandbox.base_image` and the three `sandbox.wheelhouse` hashes and size non-null, and in auth mode `wandb_auth` also `WANDB_BASE_URL`, `WANDB_IDENTITY_TOKEN_FILE`, `WANDB_ENTITY`, `WANDB_PROJECT` absent; with `telemetry.export_mode == "after_unit"` the `export` key supplied and the `export` rows below; `qualification.offline_gate` passes (3.22) |
| `search`, `evaluate` | `STATE/qualification/passed.json` exists and its `engine_sha256` and `manifest_sha256` equal the current ones (in a campaign with `qualification.import_from` that file is written by the per-project preflight after the import conditions of 3.22 held) |
| `search` | `decisions.proposer_permission_mode` set; `proposer.cli_path`, `proposer.cli_sha256` and `proposer.cli_version` non-null and the file's hash equal; `proposer.max_budget_usd_per_session`, `budgets.proposer_list_usd` and `proposer.session_estimate_usd` non-null; `CLAUDECODE` and `CLAUDE_CODE_ENTRYPOINT` absent from `environ` (a live proposer must be started from the user's own terminal, not from inside a Claude Code session: such a shell carries about 34 `CLAUDE*` and `ANTHROPIC*` variables [r2], and although none reaches the proposer through the allow-list, the driver would be running under another agent's control); `LOGS/finalized.json` does not exist in any state and the journal holds no `test` entry (upstream refuses a search after a completed finalisation, `meta_harness.py` L395-L402, and an unfinished one leaves test results where the proposer reads [A-U E6]); with `calibration.gate_search`: `STATE/qualification/calibration.json`, or the imported campaign's when this `STATE` has none, has verdict `pass`; in a measured campaign: `SC/protocol.md` no longer contains `STAGE2-UNSIGNED` |
| `evaluate --test` | the journal holds `run: complete` for `search`, or `--end-search` is given (journaled in the `test: started` entry; the search cannot continue afterwards) |
| `evaluate --calibration` | every system of `calibration.systems` has a published usable `val.json` and `memory.json` for every dataset |
| `evaluate --noise` | every system of `upstream.expect.baselines` has a published usable `val.json` for every dataset (repeat 1) |
| `export` | the `export` key supplied; `decisions.evaltables_backend` set; `decisions.dataset_text_publication` set (read from the effective manifest, rule A); `WANDB_BASE_URL`, `CES_BASE_URL`, `WANDB_APP_URL`, `WANDB_MODE`, `OTEL_RESOURCE_ATTRIBUTES`, `WF_TRACE_SERVER_URL`, `WANDB_IDENTITY_TOKEN_FILE`, `OTEL_EXPORTER_OTLP_ENDPOINT` absent from `environ`; `budgets.lens_ingest_mb > 0` when `telemetry.agent_lens.enabled`. (`NETRC` is not on the list: every live command overwrites it with the empty state file, 3.27.) |
| `publish` | the `export` gates; `decisions.publish_approved` true; `campaign_kind == "measured"` (a preflight campaign's scratch project is never published) |

A halt blocks the runner commands only. `export` and `publish` run with a halt set: a budget halt is the expected end of a campaign and its evidence must be exportable ("negative results reported as results"); `publish` copies `stopped.json` and the `halt_history` rows into the bundle. `status`, `halt`, `amend`, `validate-manifest` and `offline-smoke` are not live commands. The live-proposer row (`CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT`) applies to `search --live` and to the proposer steps of `preflight --live`; unit tests and the offline smoke, which run the fake CLI, never pass through it.

### 2.4 State directory

```
SC/state/
  _smoke/receipt.json             last passing offline-smoke receipt: {"schema","engine_sha256","executor","worker_network_guard","utc","invariants":{...}}
  _smoke/sc/                      the offline smoke's own SC: manifest.json and state/smoke-<utc>/ (removed on success unless --keep)
  _probe-<utc>/                   throwaway state directory of the proposer probes of PF9 (its own workspace/, a canary file); removed after the probes
  <campaign>/
    runner.lock                   flock target; body {"pid","host","command","started_utc"} (informational)
    manifest.json  manifest.sha256  engine.json
    amendments.jsonl              append-only, hash-chained amendments of the export-only fields (2.3 rule A); absent until the first one
    account/account.json          written by account-check (3.22): {"schema":"mhforge.account/1","utc","steps":{name:{"passed",...}}}; may exist
                                  before the freeze; holds plan, flags and headroom figures; never planned, exported, bundled or put in a run config
    account/pricing-<utc>-<n>.html    the two public pricing pages as fetched by account-check
    runtime.json                  written by each runner command at start: {"schema":"mhforge.runtime/1","pid","command","started_utc",
                                  "key_source":"prompt"|"fd"|"env","key_scrubbed":{<variable name>: bool|null}}; names only
    keydigests.json               mode 0600; 2.1
    stopped.json                  last stop: {"schema":"mhforge.stop/1","reason","detail","phase","at_utc","exit_code"}
    ledger.sqlite (+ -wal, -shm)  2.10
    replay.sqlite                 2.10
    circuit.json                  {"failures": int, "until_epoch": float}
    journal.jsonl                 2.7
    workspace/
      pristine.json               {"schema":"mhforge.pristine/1","source","tree_sha256","copied_utc","files":{rel: sha256}}
      text_classification/        WS: copy of the pinned upstream files; upstream writes agents/<candidate>.py,
                                  logs/<campaign>/ (LOGS) and .empty_plugins/ here
    calls/<job_id>/<call_id>.json           2.5
    calls/<job_id>/<call_id>.a<N>.sse       raw provider stream of provider attempt N, as received
    calls/_unknown/<call_id>.json           requests that carried no valid job identity
    jobs/<job_key>/job.json                 2.6
    jobs/<job_key>/a<N>/jobfile.json        the job file handed to the executor (2.6); holds no secret
    jobs/<job_key>/a<N>/bundle.json         {"sha256","bytes","members":{path: sha256}}
    jobs/<job_key>/a<N>/box/                local executors only: the extracted work tree (same layout as /mhf in a sandbox)
    jobs/<job_key>/a<N>/out/                val.json | test.json, memory.json, log.jsonl, sidecar.jsonl
    jobs/<job_key>/a<N>/run/                stdout.txt, stderr.txt, exit.json, stub.txt, selfcheck.json
    jobs/<job_key>/a<N>/cache/              worker reply cache when copied back
    jobs/<job_key>/a<N>/executor.json       executor facts: sandbox id, timings, relay counters, selfcheck summary
    quarantine/<utc>/<relative path>        files moved out of WS: result files no job record published (3.18), and files a proposer
                                            session added outside agents/ and logs/ (3.19)
    import_checks/iter-<NNN>/a<N>/check.json    one per import-check call: probe result, per-candidate results, sandbox id
    proposer/iter-<NNN>/s<k>/session.json   2.11
    proposer/iter-<NNN>/s<k>/stream.jsonl   tee of the CLI's stdout with arrival times
    proposer/iter-<NNN>/s<k>/stderr.txt  env.json  command.json  tree-before.json  tree-after.json
    proposer/iter-<NNN>/s<k>/settings.json  the generated --settings file of the session (every mode; 3.19)
    proposer/iter-<NNN>/s<k>/evolution_summary.before.jsonl   copy taken before the session (restored if the session changed the file)
    proposer/iter-<NNN>/s<k>/pending_eval.json  proposal.json  candidates/<name>.py
    proposer/iter-<NNN>/s<k>/transcript/<session_id>.jsonl  transcript/<session_id>/...
                                            also <session_id>.orphaned-* and <session_id>.jsonl.superseded-* when the CLI left them
    iterations/iter-<NNN>.json              2.11
    qualification/attempt-<n>/<check>.json  qualification.json  forecast.json
    qualification/passed.json               {"schema":"mhforge.passed/1","engine_sha256","manifest_sha256","attempt","utc","environment":{...},
                                            "imported_from": null | {"campaign","manifest_sha256","diff_paths":[...]}}  (3.22)
    qualification/calibration.json          {"schema":"mhforge.calibration/1","systems","results":{dataset:{system:{"accuracy","correct","total","job_id",
                                            "content_on_every_call"}}},"macro_test_accuracy":{system: percent},
                                            "g0":{dataset:{system:{"correct","total"}}},   validation counts of the published baselines, record only
                                            "gates":{name:{"system","hard","passed","failures":[str]}},"flags":[str],
                                            "verdict":"pass"|"fail"|"recorded","utc"}
    qualification/noise.json                {"schema":"mhforge.noise/1","repeats","systems","per_dataset":{dataset:{system:{"correct":[per repeat],"total",
                                            "flip_rate","all_agree"}}},"macro_validation_accuracy":{system:[per repeat]},"macro_std":{system: float},
                                            "identical_all_repeats": bool,"utc"}  (3.20)
    scratch/                                empty logs and results directories handed to upstream's builders for qualification and calibration planning
    export/export.lock
    export/empty.netrc                      empty file, mode 0600; NETRC of every live command's process and of the exporter child points here (3.27)
    export/wandb-local/                     WANDB_DIR, WANDB_CACHE_DIR, WANDB_DATA_DIR of the exporter process, and the dir= of every wandb.init
    export/plans/<unit-slug>/spans.json  tables.json  runs.json
    export/scan/<unit-slug>.json
    export/receipts/<unit-slug>.<surface>.json        2.16
    export/stored-spans/<unit-slug>.json
    export/attempts/<unit-slug>.<surface>.e<k>.json   superseded receipts, never overwritten
    publish/bundle/  publish/publication.json
```

Upstream's own files stay where upstream puts them, inside `WS`: `LOGS/{frontier_val.json, frontier.json, evolution_summary.jsonl, summary.json, pending_eval.json, finalized.json, reports/, claude_sessions/, .launcher/, <dataset>/<system>/<model_short>/{val.json, memory.json, log.jsonl}, results/<dataset>/<system>/<model_short>/test.json}` and `WS/agents/<candidate>.py`. Nothing under `state/` is ever uploaded as a directory. Before a campaign's first freeze, `account-check` may already have created `SC/state/<campaign>/account/` and `export/empty.netrc`; `status` treats that as a state directory without a manifest.

### 2.5 Gateway call record (`mhforge.call/1`)

`STATE/calls/<job_id>/<call_id>.json`. One per request that reached `Gateway.handle`, whatever happened to it. Written once at admission (without the result fields) and replaced when the request ends, so a crash leaves evidence (H2 `transport_v4.py` L166-L178 [rep]).

```json
{
  "schema": "mhforge.call/1",
  "call_id": "c-20261110T031502-9f2c41d07a6b",
  "job": {"campaign": "mh-forge-01", "split": "val", "candidate": "fewshot_all", "dataset": "LawBench", "attempt": 1,
          "phase": "baseline", "iteration": null, "repeat": null, "sample": null,
          "job_key": "val.fewshot_all.LawBench", "job_id": "val.fewshot_all.LawBench.a1"},
  "ingress": "relay",
  "outcome": "completed",
  "refusal": null,
  "received_utc": "2026-11-10T03:15:02.417000Z",
  "admitted_utc": "2026-11-10T03:15:02.420000Z",
  "ended_utc": "2026-11-10T03:15:41.873000Z",
  "queue_s": 6.682,
  "request": {"model": "openai/gpt-oss-120b",
              "messages": [{"role": "system", "content": "Reasoning: medium"}, {"role": "user", "content": "..."}],
              "temperature": 0.0, "max_tokens": 16384},
  "dropped_keys": ["usage"],
  "request_sha256": "<64 hex>", "user_sha256": "<64 hex>",
  "content_bytes": 68173, "n_messages": 2, "input_limit": 76397, "output_limit": 16384,
  "conformance": {"temperature": true, "max_tokens": true, "system_prompt": true},
  "replay": null, "reuse_salt": null,
  "attempts": [
    {"n": 1, "ledger_id": "l-20261110T031509-0c11aa90be21", "ledger_phase": "baseline",
     "started_utc": "2026-11-10T03:15:09.102000Z", "first_byte_utc": "2026-11-10T03:15:11.540000Z",
     "ended_utc": "2026-11-10T03:15:41.870000Z",
     "http_status": 200, "disposition": "ok", "done": true, "error": null,
     "response_headers": {"content-type": "text/event-stream", "x-request-id": "..."},
     "sse_file": "c-20261110T031502-9f2c41d07a6b.a1.sse", "sse_bytes": 48211,
     "reserved_micro": 5078, "spent_micro": 972, "ledger_status": "complete", "backoff_s": 0.0}
  ],
  "response": {"id": "chatcmpl-...", "object": "chat.completion", "created": 1794270000, "model": "openai/gpt-oss-120b",
               "choices": [{"index": 0, "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "...", "reasoning": "..."}}],
               "usage": {"prompt_tokens": 24310, "completion_tokens": 1422, "total_tokens": 25732}},
  "usage": {"prompt_tokens": 24310, "completion_tokens": 1422, "total_tokens": 25732, "cached_tokens": null, "reasoning_tokens": null},
  "finish_reason": "stop", "response_model": "openai/gpt-oss-120b", "response_id": "chatcmpl-...",
  "flags": {"truncated_empty": false, "empty_content": false, "reasoning_present": true, "model_mismatch": false},
  "cost_micro": 972, "cost_basis": "usage",
  "reply": {"status": 200, "code": null, "source": "provider"}
}
```

Field rules.

- `outcome`: `completed` (forwarded, 200 returned), `replayed` (answered from the store), `coalesced` (waited on an identical in-flight request), `refused` (never forwarded), `provider_rejected` (forwarded, the provider said no, not charged), `failed` (forwarded, gateway attempts exhausted or a fatal fault). A record that has no `outcome`, `ended_utc` or `reply` is an admission-only record: the runner died before the request ended. Readers (the join, the planners, `audit`) treat it as outcome `interrupted` with the end time of its job attempt, `usage: null` and an error (3.23).
- `refusal`: `null`, or `{"code": <code of the table in 3.2>, "status": 402|403, "detail": str}`. `detail` holds provider text (with any occurrence of the key replaced, 3.9) or the internal reason (for example `repeatable_provider_failure`, `job_closed`); it stays in the record and never reaches the worker.
- `request` is the admitted body, after dropping `usage` and `stream: false`. For every refused request (refused by validation, or after admission) it is replaced by `"request_excerpt": {"bytes": n, "sha256": "...", "head": "<first 2048 characters>"}`, and after `gateway.max_refused_records_per_job` refusals in one job attempt no further record is written for a refusal (they are counted in the job record as `refused_unrecorded`), so a caller that loops on refusals cannot fill the disk.
- `attempts[].done`: whether `[DONE]` was seen on that attempt's stream.
- `user_sha256`: SHA-256 of the UTF-8 content of the last `user` message. It is the join key to the sidecar (2.8).
- `n_messages`: the number of messages admitted; with `content_bytes` it gives `input_limit` (3.10).
- `conformance`: whether `temperature`, `max_tokens` and the system message equal `upstream.expect`. Recorded, not enforced.
- `replay`: `null`, or `{"scope": "search"|"job"|"qualification"|"repeat", "source_call_id": str, "kind": "stored"|"coalesced"}` (the scope is one of `wire.REUSE_SCOPES`). `reuse_salt`: `null`, or `"s<k>"` for a job with a sample index; it is part of the reuse key, not of `request_sha256` (3.7).
- `attempts[].disposition`: `ok`, `retry_transport`, `retry_rate_limited`, `provider_rejected`, `context_limit`, `config_fault`, `quota_exhausted`, `fatal`. `error`: `null`, or `{"kind": "http"|"stream_error"|"truncated"|"connect"|"timeout"|"bad_json"|"too_large", "http_status": int|null, "code": str|null, "body_excerpt": str}`.
- `response` is the merged non-streaming body exactly as returned for `completed`; `null` otherwise. For `replayed` and `coalesced` the record carries `usage`, `finish_reason`, `response_model` and `flags` copied from the source call, and `cost_micro: 0`, `cost_basis: "none"`.
- `flags.truncated_empty`: `finish_reason == "length"` and empty content (the reasoning used the whole budget). It is delivered unchanged, as upstream would score and cache it [rep CM-V M5].
- `cost_basis`: `usage`, `reservation` (200 without usable usage: the reservation stays charged and is reported separately), `none`.
- `reply`: what the worker was told. `source` is `provider`, `replay`, `coalesced` or `gateway`.
- `response_headers` uses the allow-list `content-type, date, retry-after, request-id, x-request-id, cf-ray, x-ratelimit-*`.

### 2.6 Job record, job file, exit file

**Job record** (`mhforge.job/1`), `STATE/jobs/<job_key>/job.json`, rewritten after each attempt.

```json
{
  "schema": "mhforge.job/1",
  "campaign": "mh-forge-01", "job_key": "val.no_memory.USPTO",
  "split": "val", "candidate": "no_memory", "dataset": "USPTO",
  "phase": "baseline", "iteration": null, "repeat": null, "sample": null, "seed": 42,
  "candidate_sha256": "<64 hex>", "route": 2, "executor": "sandbox", "isolated": true,
  "upstream_desc": "val/USPTO/no_memory/gpt-oss-120b",
  "upstream_command": "env PYTHONPATH=<WS parent> uv run --project <WS> python -m text_classification.inner_loop --memory agents/no_memory.py ...",
  "inner_argv": ["--memory", "agents/no_memory.py", "--dataset", "USPTO", "--seed", "42",
                 "--model", "openrouter/openai/gpt-oss-120b", "--mode", "offline",
                 "--val-output", "{out}/val.json", "--save-memory", "{out}/memory.json", "--log", "{out}/log.jsonl",
                 "--num-train", "50", "--num-val", "30", "--num-test", "100", "--temperature", "0.0"],
  "status": "succeeded",
  "attempts": [
    {"attempt": 1, "job_id": "val.no_memory.USPTO.a1", "started_utc": "...", "ended_utc": "...",
     "candidate_sha256": "<64 hex>", "candidate_restored": false, "inputs": {},
     "returncode": 0, "timed_out": false, "kill_reason": null, "outcome": "ok", "infra": false, "failure_class": null,
     "exit_file": {"present": true, "returncode": 0, "reason": "exit", "local_refusals": {}, "unanswered_at_exit": 0},
     "refusal_codes": {},
     "result_usable": true, "bundle_sha256": "<64 hex>",
     "outputs": {"val.json": "<sha256>", "memory.json": "<sha256>", "log.jsonl": "<sha256>", "sidecar.jsonl": "<sha256>"},
     "gateway": {"completed": 30, "replayed": 0, "coalesced": 0, "refused": 0, "refused_unrecorded": 0,
                 "provider_rejected": 0, "failed": 0, "refusal_codes": {}, "repeatable_provider_failures": 0,
                 "truncated_empty": 0, "cost_micro": 6480, "bounded_unknown_micro": 0, "in_flight_at_close": 0,
                 "usage_calls": 30, "prompt_tokens_sum": 11130, "prompt_tokens_max": 402, "completion_tokens_sum": 36120},
     "host_sleep_s": 0.0,
     "executor": {"sandbox_id": "sbx-...", "seconds": 412.5, "setup_seconds": 96.0, "cost_micro": 39059,
                  "environment": {"env_mode": "wheelhouse", "image_digest": "sha256:<64 hex>", "requirements_sha256": "<64 hex>",
                                  "sha256sums_sha256": "<64 hex>", "freeze_sha256": "<64 hex>", "machine": "x86_64", "python": "3.13.18"},
                  "relay": {"mode": "daemon", "attaches": 1, "rotations": 0, "frames_in": 31, "frames_out": 30,
                            "chars_in": 181204, "noise_chars": 0, "frame_errors": 0, "duplicate_requests": 0,
                            "cancelled": 0, "pings": 20, "flood_refusals": 0},
                  "selfcheck": "passed"}}
  ],
  "published": {"at_utc": "...",
                "files": {"logs/mh-forge-01/USPTO/no_memory/gpt-oss-120b/val.json": "<sha256>",
                          "logs/mh-forge-01/USPTO/no_memory/gpt-oss-120b/memory.json": "<sha256>",
                          "logs/mh-forge-01/USPTO/no_memory/gpt-oss-120b/log.jsonl": "<sha256>"},
                "launcher_log": "logs/mh-forge-01/.launcher/00_val_USPTO_no_memory_gpt-oss-120b.log"},
  "result": {"accuracy": 0.1, "correct": 3, "total": 30, "memory_context_chars": 191},
  "name_collision": false,
  "export_unit": "job:val.no_memory.USPTO.a1"
}
```

- `inner_argv` is upstream's argv after `text_classification.inner_loop` (the output of upstream's own builder, 3.18), with the values of the five path flags (`--val-output`, `--save-memory`, `--log`, `--load-memory`, `--test-output`) replaced by `{out}/<name>` or `{in}/<name>`, `--force` dropped, and on Route 1 the `--model` value replaced and `--api-base {api_base}` appended.
- `status`: `succeeded`, `failed`, `skipped_usable`, `skipped_no_memory`, `halted` (`wire.JOB_STATUSES`).
- `attempts[].outcome`: `ok`, `worker_error`, `no_result`, `timeout`, `request_ceiling`, `relay_lost`, `provider_unavailable`, `executor_error`, `env_setup_failed`, `halted`, `interrupted` (`wire.ATTEMPT_OUTCOMES`). `infra` is true for the last six (`wire.INFRA_OUTCOMES`), and for every failed attempt of a pinned system (a baseline) except `request_ceiling` (3.16 and 3.18 give the rules). `env_setup_failed`: the sandbox's environment could not be made ready (a wheelhouse upload, checksum or install failed, or the sandbox's proof of its environment did not match); it is an infrastructure fault, never a candidate failure. `interrupted` with `failure_class: "host_sleep"`: the host slept during the attempt (`host_sleep_s > 0`), which is not a timeout.
- `gateway.usage_calls`, `prompt_tokens_sum`, `prompt_tokens_max`, `completion_tokens_sum`: over the attempt's completed calls that carried usage; they feed the per-candidate context figures of the iteration record (2.11). `result.memory_context_chars` is upstream's own figure from the result file (`inner_loop.py` L808 [src]).
- `attempts[].refusal_codes`: the merged count of refusal codes for the attempt: the gateway's, the stub's local ones (`exit_file.local_refusals`) and the relay session's flood refusals (`relay_overloaded`). `classify_attempt` reads this, not `gateway.refusal_codes`.
- `attempts[].kill_reason`: `null`, `"halt"` (the executor killed the worker after a halt) or `"refusal_flood"` (3.10).
- `attempts[].inputs`: name to SHA-256 of each file uploaded under `{in}` (test jobs: `memory.json`). `candidate_restored` is true when the bundle took the candidate file from the stored proposal copy because the workspace file had changed (3.18).
- `gateway.in_flight_at_close`: provider attempts still running when the job was closed; they settle later and `audit` accounts for them. `gateway.refused_unrecorded`: refusals beyond the record cap.
- `failure_class` (best effort, from stderr and the counters): `import_error`, `dependency_missing`, `context_limit`, `request_ceiling`, `stream_budget`, `relay_overloaded`, `refusal_flood`, `harmony_vocab`, `worker_exception`, `host_sleep`, `unknown`, or `null`.
- `executor.environment`: the identity of the environment the worker ran in, as the sandbox proved it before the worker started (3.17): the image digest, the hashed requirements file, the wheelhouse `SHA256SUMS` (mode `wheelhouse`; `null` in mode `image`), the measured freeze hash, the machine and the Python version. `setup_seconds` is the wheelhouse upload and install time (0 in mode `image`).
- `published` is `null` until results are copied into `LOGS`; it stays `null` for a quarantined candidate and for jobs run with `publish=False` (qualification, calibration, noise repeats).

**Job file** (`mhforge.jobfile/1`), written by `bundle.build_job_file`, read by `mhf_relay_stub.py` and `mhf_selfcheck.py` inside the executor (`mhf_capture_entry.py` reads no job-file field: everything it needs is on its command line). Paths are the executor's own (`/mhf/...` in a sandbox, `<attempt_dir>/box/...` locally).

```json
{
  "schema": "mhforge.jobfile/1",
  "job_id": "val.no_memory.USPTO.a1",
  "argv": ["python3", "/mhf/bin/mhf_capture_entry.py", "--sidecar", "/mhf/out/sidecar.jsonl", "--max-text-chars", "20000", "--",
           "--memory", "agents/no_memory.py", "--dataset", "USPTO", "--seed", "42", "--model", "openrouter/openai/gpt-oss-120b",
           "--mode", "offline", "--val-output", "/mhf/out/val.json", "--save-memory", "/mhf/out/memory.json",
           "--log", "/mhf/out/log.jsonl", "--num-train", "50", "--num-val", "30", "--num-test", "100", "--temperature", "0.0"],
  "env": {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/mhf/home", "TMPDIR": "/mhf/tmp", "PYTHONPATH": "/mhf/pkg",
          "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "TEXT_CLASSIFICATION_LLM_CACHE_DIR": "/mhf/cache",
          "LITELLM_LOCAL_MODEL_COST_MAP": "True", "LITELLM_MODE": "PRODUCTION",
          "TIKTOKEN_RS_CACHE_DIR": "<tokenizers dir>", "NO_PROXY": "127.0.0.1,localhost"},
  "cwd": "/mhf/pkg",
  "route": 2,
  "port": 0,
  "timeout_s": 7200,
  "reply_timeout_s": 3600,
  "ping_interval_s": 20,
  "pong_timeout_s": 60,
  "max_request_bytes": 4000000,
  "run_dir": "/mhf/run",
  "expect_outputs": ["/mhf/out/val.json"],
  "vocab_file": "fb374d419588a4632f3f557e76b4b70aebbca790",
  "repeat": null,
  "sample": null,
  "probe_endpoints": [],
  "expect": {"machine": "x86_64", "python": "3.13", "freeze_sha256": null}
}
```

`vocab_file` is the name of the tokenizer vocabulary file that must exist in `TIKTOKEN_RS_CACHE_DIR`. The field is deliberately not called `tokenizer_file`: a 40-hex value after a name containing `token` would look like a keyed secret to a loose scanner rule, and under the first draft of the rule every job file was a finding [A-S a_scan]. It may be `null` (the selfcheck then skips the check; used only by tests against the stand-in package). The stub builds the worker's environment from `env` only, then adds `OPENROUTER_API_BASE=http://127.0.0.1:<port>/v1` and `OPENROUTER_API_KEY=mhf-relay` on Route 2; on Route 1 it replaces the literal argument `{api_base}` with that URL. `repeat` and `sample` are the job's repeat and sample index (`null` for an ordinary job); the worker's upstream cache directory (`TEXT_CLASSIFICATION_LLM_CACHE_DIR`, `<box>/cache`) is a fresh directory per job attempt, and a repeat or a sample is its own job key, so no two repeats or samples ever share a worker cache. `probe_endpoints` are extra `host:port` targets for the selfcheck's recorded egress probes (`sandbox.object_store_endpoint` when set). `expect` is what the selfcheck's proof is compared with: `sandbox.expect.*` and the freeze hash recorded at qualification (`null` until one exists). No job file ever holds a token or a key. An import-check job file (`bundle.build_check_file`) has `job_id: "import_check"`, an empty `argv` and empty `expect_outputs`; the stub is never started with it.

**Exit file** (`mhforge.exit/1`), `<run_dir>/exit.json`, written atomically by the stub's supervisor (or by `LocalExecutor`) when the worker has ended. A missing exit file is a failed attempt whatever any exit code says.

```json
{"schema": "mhforge.exit/1", "returncode": 0, "reason": "exit", "timed_out": false, "t_start": 1794273591.1, "t_end": 1794273700.0,
 "local_refusals": {"relay_unavailable": 0, "request_too_large": 0}, "unanswered_at_exit": 0}
```

`reason`: `exit`, `timeout` (return code 124), `uplink_lost`, `killed` (a `KILL` frame, or the executor's kill). `local_refusals` counts the refusals the stub issued itself, which the host gateway never sees. `unanswered_at_exit` counts requests that had been sent to the host and had received no reply when the worker ended (requests whose client had already gone are not counted; 3.13). Both fields are absent from an exit file written by `LocalExecutor` and are read as zero. They can make an attempt an infrastructure failure, never a success (3.16).

### 2.7 Journal entry (`mhforge.journal/1`)

`STATE/journal.jsonl`, append-only, one writer (the runner), `fsync` per line.

```json
{"schema": "mhforge.journal/1", "seq": 41, "utc": "2026-11-10T01:02:03.000001Z", "pid": 51234,
 "op": "job", "key": "val.no_memory.USPTO", "status": "started", "attempt": 1,
 "detail": {"executor": "sandbox", "phase": "baseline", "candidate_sha256": "<64 hex>"}}
```

`seq` increases strictly across restarts (the writer reads the last valid line at open). A `started` entry with no later terminal entry for the same `(op, key, attempt)` is closed with `interrupted` by `Journal.recover()` at the next runner start.

| `op` | `key` | `status` values | `detail` |
|---|---|---|---|
| `run` | command name (`search`, `baselines`, `calibration`, `noise`, `test`, `preflight`) | `started`, `complete`, `paused`, `stopped` | `{"manifest_sha256", "engine_sha256", "protocol_sha256", "argv"}`; `paused` (a `search` block that ended at `--max-iterations-this-run` before `search.iterations`; exit 0) adds `{"last_iteration"}`; `stopped` adds `{"reason", "detail", "undrained"}` (`undrained` = provider attempts still open when the gateway's drain timed out). Only `complete` unlocks `evaluate --test`. |
| `job` | `job_key` | `started`, `complete`, `failed`, `interrupted`, `skipped`, `reset` | `started`: `{"executor", "phase", "candidate_sha256", "inputs", "handle"}` (`handle` is what orphan cleanup needs: `{"sandbox_id"}` or `{"pgid", "t_start"}`); terminal: `{"outcome", "infra", "failure_class", "result_sha256", "cost_micro"}`; `skipped`: `{"why": "usable"\|"no_memory"}`; `reset` (operator, `status --reset-job`): `{"note"}`; attempts before the latest `reset` no longer count against the candidate budget |
| `benchmark` | `<split>.<candidate>` | `started`, `complete` | `complete`: `{"returncode", "jobs": {dataset: status}, "published": bool, "quarantined": bool}`. A `complete` entry makes the `eval:` unit ready. |
| `import_check` | `iter<NNN>` | `started`, `complete`, `failed`, `interrupted` | `attempt` = call ordinal. `started`: `{"executor", "handle"}`; `complete`: `{"probe_ok": true, "results": {candidate: bool}}`; `failed` (the probe failed or the executor raised; an infrastructure attempt): `{"kind", "stderr_tail"}` |
| `proposer` | `iter<NNN>` | `started`, `complete`, `interrupted`, `replayed` | `attempt` = session ordinal `k`. `complete`: `{"outcome", "session_id", "candidates": [{"name", "sha256", "base_system"}], "list_price_micro", "duration_s", "wait_until_utc"}` (`wait_until_utc` only for outcome `limit_window`); `replayed`: `{"from": "s<k>", "restored": [...]}` |
| `iteration` | `iter<NNN>` | `settled` | `{"kind": "baselines"\|"scored"\|"zero_valid"\|"proposer_failed"\|"recovered"}`. Makes the `loop:` unit ready. |
| `workspace` | `ensure`, `restore`, `quarantine`, `republish` | `complete` | lists of relative paths |
| `halt` | reason | `set`, `cleared`, `reconciled` | `{"detail", "call_id", "note"}`; `reconciled`: `{"ledger_id", "spent_micro", "note"}`. A halt set by another process (`halt` command, the exporter) is journaled `set` by the runner when it sees it. |
| `export` | unit | `requested`, `complete`, `failed` | `{"exit_code", "statuses"}` |
| `test` | campaign | `started`, `complete`, `incomplete` | `{"systems", "end_search": bool}`. Written only by the finalisation (`evaluate --test`). Any `test` entry blocks `search` for the rest of the campaign. |
| `calibration` | campaign | `started`, `complete` | `{"systems", "verdict"}`. Never blocks `search`. |
| `noise` | campaign | `started`, `complete` | `{"repeats", "identical_all_repeats"}`. Never blocks `search`. |
| `amendment` | `seq` as text | `appended` | `{"paths": [...], "row_sha256"}`. Written by the `amend` command (it takes `export/export.lock`, not the runner lock). |

### 2.8 Sidecar rows (`mhforge.sidecar/1`)

`<out>/sidecar.jsonl`, written by `mhf_capture_entry.py` inside the worker. One JSON object per line, `ensure_ascii`, flushed per line, each with an increasing `seq`, so a killed worker leaves a usable prefix. The host treats it as untrusted: the candidate shares the process that writes it.

```json
{"kind": "meta", "seq": 1, "schema": "mhforge.sidecar/1", "t": 1794273591.10, "pid": 4711, "argv": ["--memory", "agents/no_memory.py", "..."], "python": "3.13.14", "main_thread": 8596717056, "hooks": {"generate_one": true, "batch": true, "loader": true}}
{"kind": "splits", "seq": 2, "t": 1794273591.80, "dataset": "USPTO", "sizes": {"train": 50, "val": 30, "test": 100}, "wanted": ["val"], "duplicate_keys": 0}
{"kind": "call", "seq": 3, "t_start": 1794273592.01, "t_end": 1794273619.44, "thread": 6123456, "user_sha256": "<64 hex>", "prompt_chars": 1608, "system_sha256": "<64 hex>", "kwargs": {"max_tokens": 16384, "temperature": 0.0}, "cache_hit": false, "content_sha256": "<64 hex>", "content_chars": 412, "error": null}
{"kind": "eval", "seq": 4, "t": 1794273619.45, "thread": 6123456, "split": "val", "index": 7, "pass": 1, "example_sha256": "<64 hex>", "question_sha256": "<64 hex>", "prediction": "CCO.CC(=O)Cl", "prediction_chars": 12, "prediction_sha256": "<64 hex>", "target": "CC(C)...", "target_sha256": "<64 hex>", "was_correct": false, "metrics": {"jaccard_similarity": 0.0}, "call_seqs": [3], "batch_user_sha256": [], "ambiguous": false}
{"kind": "done", "seq": 125, "t": 1794273700.02, "exit": 0, "exception": null, "capture_errors": 0}
```

- `call`: one per `ProviderLLM._generate_one` invocation, cache hit or miss. `user_sha256` is the SHA-256 of the UTF-8 prompt as passed to `_generate_one`, which is the text after `LLM._truncate` (`llm.py` L369-L375, L252-L260 [src]) and therefore equals the gateway's `user_sha256` [F-E1, T-E2]. `error` is the exception's class name when the original raised. No prompt or reply text is stored.
- `eval`: one per evaluator call. `split` is `train`, `val`, `test` or `null` (key not found). `pass` counts evaluator calls for that `(split, index)`; a validation job has passes 1 and 2, a test job pass 1 [F-E1, T-E2]. `call_seqs` are the `call` rows made on the same thread since that thread's previous `eval`. `batch_user_sha256` are the prompt hashes of `LLM.batch` calls made on that thread since its previous `eval` (`batch` runs `_generate_one` on other threads, so thread adjacency does not hold for it). `prediction` and `target` are cut at `--max-text-chars`; `prediction_chars` is the full length. `question_sha256`, `prediction_sha256` and `target_sha256` are the SHA-256 of the UTF-8 bytes of the whole `raw_question`, of the whole prediction (`str(pred)`) and of the whole target, before any cut: a reader who holds the upstream data at `f805ad3` can recompute `question_sha256` and so join a published row to its example without the text being published, and the table planner uses the other two where a dataset's prediction and target may not be shown (2.14). `ambiguous` is true when the example key maps to more than one `(split, index)`.
- `index` is the position in the split list the loader returned, which is what upstream's prediction lists are aligned to (`inner_loop.py` L460-L465).

### 2.9 Relay frame (`MHF1`)

The wire unit of the exec-stream relay, both directions. ASCII only. One message is one frame. [L1, T-E1]

```
#MHF1 <TYPE> <rid> <flags> <raw_len> <b64_len> <crc32hex>\n
<base64 payload, exactly b64_len characters>\n
```

- `raw = json(meta, ensure_ascii, separators (",", ":")) + b"\n" + body`. `payload = base64(zlib.compress(raw, 6))` with `flags == "z"` when that is shorter than `raw`, else `base64(raw)` with `flags == "-"`. `crc32hex` is 8 lower-case hex digits of `zlib.crc32(raw) & 0xffffffff`. `raw_len` is `len(raw)`.
- `rid` fully matches `[a-z0-9]{1,16}`. Requests use `r<counter>` chosen by the stub; control frames use `0`. `RES` and `CAN` carry the `rid` of their `REQ`.
- Limits: header line at most 256 characters; `b64_len` at most 16 MiB; decompression is bounded to `raw_len` and 16 MiB (a compression bomb is rejected).

| `TYPE` | Direction | `meta` | `body` |
|---|---|---|---|
| `HELLO` | stub to host; first frame on every attach | `{"proto": 1, "mode": "stdio"\|"daemon", "pid": int, "port": int, "pending": [rid, ...], "worker": {"state": "running"\|"exited", "returncode": int\|null}}` | empty |
| `REQ` | stub to host | `{"method": "POST", "path": "/v1/chat/completions"}` | the raw HTTP request body |
| `RES` | host to stub | `{"status": int, "headers": [[name, value], ...]}` | the response body |
| `CAN` | stub to host | `{}` | empty. The worker's client for this `rid` has gone (its 600 s timeout fired, or the worker died); the stub has dropped the request |
| `EXIT` | stub to host; repeated on each attach after the worker ended | `{"returncode": int, "reason": str, "timed_out": bool, "local_refusals": {code: int}, "unanswered_at_exit": int}` | empty |
| `LOG` | stub to host | `{"level": str, "msg": str}` | empty |
| `PING` | host to stub | `{"t": float}` | empty |
| `PONG` | stub to host | `{"t": float}` (the `t` of the `PING`) | empty |
| `KILL` | host to stub | `{"reason": str}` | empty. The stub kills the worker's process group, writes the exit file with reason `killed` and sends `EXIT` |
| `BYE` | host to stub | `{}` | empty |

Decoder rule: it is fed text chunks of any size that were decoded chunk by chunk with replacement. It searches for the magic `#MHF1 `, validates header, lengths, terminator, base64, decompression and CRC, and on any failure discards the magic and resynchronises. A payload character outside the base64 alphabet rejects the frame at once, without waiting for `b64_len` characters to arrive. Discarded characters are counted in `noise_chars`, rejected frames in `errors`. It holds at most one partial frame. **Each attached channel (each bridge exec, each socket connection of the stub) gets its own decoder; a decoder is never reused across channels.** With one decoder per session, a frame cut by a dying bridge swallows the next bridge's `HELLO` until as many characters arrive as the cut frame still wanted; when only `HELLO` and `EXIT` follow, the host waits for the deadline [A-M m4]. Because every byte of a frame is ASCII, splitting anywhere and decoding with replacement cannot alter a frame; un-armoured UTF-8 through the same path is corrupted [T-E1, X-E3].

Liveness rule: the host sends `PING` on the current bridge every `relay.ping_interval_s`, whatever other traffic there is (about 60 bytes each way per interval); the stub answers every `PING` with `PONG`. A bridge from which the host has received no frame of any type for `relay.pong_timeout_s` is dead (3.12). The stub, in stdio mode, treats `3 * ping_interval_s + pong_timeout_s` without any frame from the host as end of file (3.13). An exec stream that goes quiet without closing is therefore noticed within about a minute, not at the job deadline.

Writer rule: a frame is written under one lock in slices of at most `relay.write_chunk_bytes` (the SDK's stdin queue is 16 chunks of 64 KiB with backpressure [rep, critic gap 1]).

### 2.10 SQLite DDL

Both files: every operation opens its own connection (`sqlite3.connect(path, timeout=30, isolation_level=None)`), runs inside `BEGIN IMMEDIATE` ... `COMMIT` (H2 `budget.py` L75-L87 [rep]) and sets `PRAGMA journal_mode=WAL` once at creation. Every connection runs `PRAGMA synchronous=FULL` and, on macOS (`sys.platform == "darwin"`), `PRAGMA fullfsync=ON`: the default there is `fullfsync=0`, with which a power loss can drop the last commits, that is, reservations of calls already sent [A-M m1]. That makes the ledger safe for several processes (the runner, the exporter, which writes only `meter_events`, and the `halt` command, which writes only the halt row). No operation on the request path scans `calls`.

**Read-only readers** (`state.status`, the halt check in `manifest.live_blockers`, `audit`) use `util.open_sqlite_ro(path)`, which opens the file this way: if `<path>-wal` exists, `file:<path>?mode=ro` (a writer is active or a crash left a log; this open creates nothing and sees the log); otherwise `file:<path>?mode=ro&immutable=1` (creates nothing and works on a read-only copy). If the `mode=ro` open or its first query raises `sqlite3.OperationalError`, it retries once with `immutable=1` and reports `possibly_stale = True`, which `status` shows as `"ledger_view": "possibly_stale"`. A plain `mode=ro` open of an idle WAL database creates `-wal` and `-shm` files and fails outright on a read-only copy [A-M m1, m1b], which would break "status never creates a file".

**Totals rows exist before they are updated.** The seven `phase_totals` rows (one per `wire.LEDGER_PHASES` entry) are inserted when the file is created. `job_counters` is written with `INSERT ... ON CONFLICT(job_key) DO UPDATE`. `reserve` asserts `cursor.rowcount == 1` after the totals update and raises `StateError` otherwise: an `UPDATE` on a missing row changes nothing and raises nothing, and a cap check that then reads zero would silently switch the cap off [A-M m1].

**`ledger.sqlite`**

```sql
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- rows: schema_version=1, campaign, manifest_sha256, created_utc

CREATE TABLE IF NOT EXISTS jobs (                       -- job registrations (one per job attempt)
  job_id         TEXT PRIMARY KEY,
  job_key        TEXT NOT NULL,
  identity_json  TEXT NOT NULL,                         -- JobIdentity.to_dict()
  token_sha256   TEXT,                                  -- loopback ingress on Route 2 only; never the token
  registered_utc TEXT NOT NULL,
  closed_utc     TEXT,
  close_reason   TEXT                                   -- 'closed' | 'runner interrupted'
);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_token ON jobs(token_sha256) WHERE token_sha256 IS NOT NULL;

CREATE TABLE IF NOT EXISTS calls (                      -- one row per provider attempt (from H2 budget.py L66-L73)
  id             TEXT PRIMARY KEY,                      -- ledger_id
  call_id        TEXT NOT NULL,                         -- gateway call record
  n              INTEGER NOT NULL CHECK (n >= 1),       -- provider attempt within the request
  job_id         TEXT NOT NULL,
  job_key        TEXT NOT NULL,
  phase          TEXT NOT NULL CHECK (phase IN ('qualification','calibration','baseline','search','test','controls','recovery')),
  origin_phase   TEXT NOT NULL,                         -- the job's own phase when phase = 'recovery'
  model          TEXT NOT NULL,
  input_limit    INTEGER NOT NULL CHECK (input_limit > 0),
  output_limit   INTEGER NOT NULL CHECK (output_limit > 0),
  reserved_micro INTEGER NOT NULL CHECK (reserved_micro >= 0),
  spent_micro    INTEGER,                               -- NULL until settled from usage
  status         TEXT NOT NULL CHECK (status IN ('pending','complete','rejected','bounded_unknown','unknown','reconciled')),
  usage_json     TEXT,                                  -- usage exactly as returned, or NULL
  reason         TEXT,                                  -- e.g. 'limit_exceeded', 'runner interrupted', a rejection kind
  created_utc    TEXT NOT NULL,
  settled_utc    TEXT
);
CREATE INDEX IF NOT EXISTS calls_status ON calls(status);
CREATE INDEX IF NOT EXISTS calls_job    ON calls(job_key);
CREATE INDEX IF NOT EXISTS calls_call   ON calls(call_id);

CREATE TABLE IF NOT EXISTS reconcile_history (          -- operator reconciliation of 'unknown' rows; rows are never deleted
  ledger_id TEXT NOT NULL, old_status TEXT NOT NULL, spent_micro INTEGER NOT NULL,
  below_reservation INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL, utc TEXT NOT NULL
);

-- running totals: committed = sum over non-rejected rows of COALESCE(spent_micro, reserved_micro)
CREATE TABLE IF NOT EXISTS phase_totals (               -- the seven rows are inserted at creation
  phase           TEXT PRIMARY KEY,
  committed_micro INTEGER NOT NULL DEFAULT 0 CHECK (committed_micro >= 0)
);

CREATE TABLE IF NOT EXISTS job_counters (               -- what the per-job ceilings count; upserted
  job_key          TEXT PRIMARY KEY,
  job_id           TEXT NOT NULL,                       -- the job attempt `requests` belongs to
  requests         INTEGER NOT NULL DEFAULT 0,          -- worker requests forwarded (first provider attempt reserved) in that job attempt
  charged_attempts INTEGER NOT NULL DEFAULT 0,          -- provider attempts of all job attempts that are not settled 'rejected'
  committed_micro  INTEGER NOT NULL DEFAULT 0,          -- COALESCE(spent, reserved) over non-rejected rows of this job_key
  prior_charged    INTEGER NOT NULL DEFAULT 0           -- 1 when an earlier job attempt of this job_key has a charged provider attempt
);

CREATE TABLE IF NOT EXISTS failed_requests (            -- requests whose gateway attempts were exhausted (3.10, repeatable failures)
  job_key        TEXT NOT NULL,
  request_sha256 TEXT NOT NULL,
  exhausted      INTEGER NOT NULL DEFAULT 0,            -- number of job attempts in which this digest ended provider_unavailable
  peer_ok        INTEGER NOT NULL DEFAULT 0,            -- 1 when another request was served while this one was failing, in any of them
  last_job_id    TEXT NOT NULL,
  last_call_id   TEXT NOT NULL,
  utc            TEXT NOT NULL,
  PRIMARY KEY (job_key, request_sha256)
);

CREATE TABLE IF NOT EXISTS halt (                       -- at most one row: the halt flag
  id      INTEGER PRIMARY KEY CHECK (id = 1),
  reason  TEXT NOT NULL,
  detail  TEXT NOT NULL DEFAULT '',
  call_id TEXT,
  set_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS halt_history (
  set_utc TEXT NOT NULL, reason TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
  cleared_utc TEXT, cleared_note TEXT
);

CREATE TABLE IF NOT EXISTS meter_events (               -- the three meters of F6, plus the export counters of the second round
  meter       TEXT NOT NULL CHECK (meter IN ('sandbox_seconds','lens_bytes','lens_spans','proposer_list_price',
                                             'wandb_upload_bytes','eval_tables')),
  key         TEXT NOT NULL,                            -- sandbox id | '<unit>#e<k>' (lens_bytes, lens_spans, wandb_upload_bytes)
                                                        -- | '<unit>#e<k>#<table key>' (eval_tables) | session_key
  bucket      TEXT NOT NULL DEFAULT '',                 -- lens_bytes and lens_spans: 'main' | 'preflight' | 'recovery'; '' otherwise
  status      TEXT NOT NULL CHECK (status IN ('open','final')),
  quantity    INTEGER NOT NULL CHECK (quantity >= 0),   -- milliseconds | bytes | spans | tables | micro-dollars; the worst case while 'open'
  unit        TEXT NOT NULL,                            -- 'ms' | 'bytes' | 'spans' | 'tables' | 'micro_usd'
  micro_usd   INTEGER,                                  -- priced estimate where a price exists; the worst case while 'open'
  detail_json TEXT,                                     -- sandbox rows: {"purpose": "job"|"import_check", "job_id"|"iteration"}
  utc         TEXT NOT NULL,                            -- when the row was opened
  closed_utc  TEXT,
  PRIMARY KEY (meter, key)                              -- one sandbox, export attempt or session is metered once
);
```

An `open` row counts at its worst case in every total and cap check, so a runner crash between opening and closing a row can only over-count. A sandbox is metered under its own id, so the first sandbox of "stop, create once more", an import-check sandbox and a sandbox left behind by a crash each have a row.

**`replay.sqlite`**

```sql
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
-- rows: schema_version=1, scope=search|job  (the campaign's routing.reply_reuse_scope; a change against an existing file is refused)

CREATE TABLE IF NOT EXISTS replies (
  scope_key         TEXT NOT NULL,       -- 'search' | 'job:<job_key>' | 'q:<job_key>' (qualification jobs, always)
                                         -- | 'repeat:<job_key>' (jobs with a repeat index, always)
  request_sha256    TEXT NOT NULL,       -- the reuse key: sha256 of canonical_json of the admitted body, salted for a job with a sample index (3.7)
  source_call_id    TEXT NOT NULL,       -- the call that paid
  source_job_id     TEXT NOT NULL,
  body_zlib         BLOB NOT NULL,       -- the merged chat-completion body exactly as first returned, zlib level 6
  body_sha256       TEXT NOT NULL,       -- of the uncompressed body; a mismatch on read is a miss
  finish_reason     TEXT,
  prompt_tokens     INTEGER,
  completion_tokens INTEGER,
  created_utc       TEXT NOT NULL,
  hits              INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (scope_key, request_sha256)
);
```

In-flight coalescing is in memory (one gateway per state directory, enforced by the runner lock).

### 2.11 Proposer session, proposal and iteration records

**Session record** (`mhforge.session/1`), `STATE/proposer/iter-<NNN>/s<k>/session.json`.

```json
{
  "schema": "mhforge.session/1",
  "campaign": "mh-forge-01", "iteration": 3, "ordinal": 1, "session_key": "iter003.s1",
  "permission_mode": "restricted",
  "cli": {"path": "<pinned path>", "sha256": "<64 hex>", "version": "2.1.317"},
  "argv_sha256": "<64 hex>", "argv_flags": ["-p", "--output-format", "..."],
  "settings_sha256": "<64 hex>", "settings_override": false, "addendum_sha256": "<64 hex>",
  "scratch_dir": "<proposer.scratch_root>/mh-forge-01/iter003.s1",
  "env_names": ["CLAUDE_CODE_DISABLE_AUTO_MEMORY", "CLAUDE_CODE_STARTUP_FAILURE_RESULTS", "DISABLE_AUTOUPDATER", "HOME", "LANG", "PATH",
                "PYTHONDONTWRITEBYTECODE", "PYTHONPATH", "UV_CACHE_DIR", "UV_FROZEN", "UV_NO_SYNC", "UV_OFFLINE",
                "UV_PROJECT_ENVIRONMENT", "UV_PYTHON_DOWNLOADS", "VIRTUAL_ENV"],
  "started_utc": "...", "ended_utc": "...", "duration_s": 1375.4, "timeout_s": 2400, "paused_s": 0.0,
  "exit_code": 0, "timed_out": false, "signals": [], "outcome": "ok", "outcome_detail": null,
  "session_id": "<CLI session id>", "requested_model": "opus", "resolved_model": "claude-opus-5-5", "effort": "max",
  "model_check": {"init_model": "claude-opus-5-5", "main_thread_models": ["claude-opus-5-5"], "subagent_models": ["claude-haiku-5-5"],
                  "fallback_events": [], "init_effort": "max", "request_efforts": ["max"], "mismatch": null},
  "init": {"tools": ["Read", "..."], "plugins": [], "plugin_errors": [], "skills": [], "mcp_servers": [], "memory_paths": [],
           "permission_mode": "dontAsk", "claude_code_version": "2.1.317"},
  "result_event": {"present": true, "subtype": "success", "is_error": false, "api_error_status": null, "total_cost_usd": "12.31",
                   "num_turns": 87, "usage": {}, "model_usage": {}, "permission_denials": 2, "startup_failure_reason": null},
  "limit": {"signal": null, "status": null, "rate_limit_type": null, "resets_at_utc": null, "error_code": null,
            "overage_status": null, "overage_disabled_reason": null},
  "sandbox_check": {"transcript_checked": true, "bash_results": 14, "unsandboxed": 0, "unknown": 0},
  "integrity": {"violations": []},
  "list_price_micro": 12310000, "list_price_basis": "result_event",
  "pending_eval": {"present": true, "sha256": "<64 hex>",
                   "candidates": [{"name": "x", "base_system": "fewshot_all", "valid_name": true, "file_sha256": "<64 hex>"}]},
  "transcript": {"found": true, "files": 3, "bytes": 1048576},
  "drift": {"agents_added": ["agents/x.py"], "agents_modified": [], "upstream_modified": [], "upstream_removed": [],
            "other_added": ["agents/__notes__"], "quarantined": ["scratch_notes.md"], "logs_modified": [], "restored": []},
  "env_fingerprint_before": "<64 hex>", "env_fingerprint_after": "<64 hex>", "auto_memory_dir_changed": false,
  "solver_attempt_signals": [], "state_access_signals": [],
  "wrapper_log_dir": "logs/mh-forge-01/claude_sessions/20261110_010203_iter3"
}
```

`drift` covers the workspace outside `logs/` (the snapshot of 3.14 excludes `logs/`, which upstream and the proposer both write on purpose). `other_added` lists non-regular or non-`.py` entries left under `agents/` (never bundled; `__pycache__` is ignored and not listed); `quarantined` lists files and directories the session added outside `agents/` and `logs/`, which are moved out of the workspace (3.19); `logs_modified` lists guarded files under `LOGS` the session changed (today only `evolution_summary.jsonl`, restored from the pre-session copy). `list_price_basis` is `result_event`, `cost_state`, `usage_sum` or `estimate` (3.19 step 13). `settings_sha256` is the hash of the generated settings file, `settings_override` whether `proposer.restricted.settings` was merged into it, `addendum_sha256` the hash of the restricted-mode addendum as rendered for this session (`null` in the other modes). `paused_s` is how long the session clock stood still for reported rate-limit waits. `model_check`, `limit`, `sandbox_check` and `integrity` are the results of the session checks of 3.19; `result_event.permission_denials` is the length of `result.permission_denials` (the list itself is in `stream.jsonl`). `state_access_signals` lists tool calls that named a path under `STATE` outside `WS` or under the `mhforge` package (3.19).

`outcome` (`wire.SESSION_OUTCOMES`; the classifier is in 3.19): `ok` (a `result` event with `is_error` false, not timed out, `pending_eval.json` present and accepted; the exit code alone is never success), `no_candidates` (a successful result, no usable `pending_eval.json`), `timeout`, `failed` (any other unsuccessful session, including one that stopped at its own `--max-budget-usd`), `spend_limit` (a spend or usage limit that waiting will not lift: halt and ask), `limit_window` (a plan window with a reset time: wait, then the same iteration), `auth` (login missing or expired: halt and ask), `transient` (overload, server error, network: a bounded rerun of the same iteration), `model_mismatch` (the model or effort that ran is not the one requested: halt), `sandbox_unavailable` (the Bash sandbox could not start: halt), `settings_ignored` (in mode `restricted` or `contained`, a Bash tool result without `sandboxed: true`, or no transcript to prove it: halt), `cli_missing`, `interrupted` (the runner died mid-session). `outcome_detail` is a short machine-readable reason (for example `max_budget_usd`, `no_result_event`, `transcript_missing`). The prompt, the skill text and the full argv are not stored (hashes and flag names only). `session_id` comes from the first `system` event with subtype `init` in the tee, falling back to the `result` event; a killed session has no `result` event [rep].

**Proposal record** (`mhforge.proposal/1`), `.../s<k>/proposal.json`, written only for outcome `ok`, together with `pending_eval.json` (verbatim copy) and `candidates/<name>.py` (verbatim copies). It is what `ProposerRunner` replays when upstream asks for the same iteration again (3.19).

```json
{"schema": "mhforge.proposal/1", "iteration": 3, "session_key": "iter003.s1", "pending_eval_sha256": "<64 hex>",
 "frontier_before": {"system": "fewshot_all", "val_accuracy": 41.3},
 "candidates": [{"name": "x", "file": "candidates/x.py", "sha256": "<64 hex>", "base_system": "fewshot_all",
                 "axis": "exploration", "hypothesis": "...", "components": ["..."], "valid_name": true}],
 "stored_utc": "..."}
```

`frontier_before` is read once, when the original session is entered, and reused when the proposal is replayed: upstream re-runs `--frontier` in Phase 0 on every start (`meta_harness.py` L428), so a frontier read at replay time would already contain this iteration's published results. The stored candidate copies are also the reference for every later bundle: a job of candidate `x` runs the bytes of `candidates/x.py`, whatever the workspace file holds by then (3.18).

**Iteration record** (`mhforge.iteration/1`), `STATE/iterations/iter-<NNN>.json`, written by the summary wrapper (3.20). Iteration 0 is the baselines. Upstream's `evolution_summary.jsonl` row is left exactly as upstream writes it, including its never-positive `delta` [rep]; ours is separate (decision D14).

```json
{"schema": "mhforge.iteration/1", "iteration": 3, "kind": "scored", "session_key": "iter003.s1",
 "frontier_before": {"system": "fewshot_all", "val_accuracy": 41.3},
 "candidates": [
   {"name": "x", "valid": true, "rejected": null, "base_system": "fewshot_all", "axis": "exploration", "hypothesis": "...",
    "components": ["..."], "candidate_sha256": "<64 hex>",
    "datasets": {"USPTO": {"status": "succeeded", "accuracy": 0.4}, "LawBench": {"status": "failed", "outcome": "worker_error", "failure_class": "context_limit"}},
    "upstream_avg_val": 60.0, "avg_val_all_datasets": 40.0, "complete": false, "quarantined": false,
    "context": {"memory_context_chars": {"USPTO": 45510}, "memory_context_chars_avg": 45510, "chars_div_4": 11378,
                "prompt_tokens_mean": 12480.5, "prompt_tokens_max": 14102},
    "delta_vs_previous_best": -1.3, "name_collision": false}],
 "best_complete_before": 41.3, "best_complete_after": 41.3, "improvement": 0.0,
 "evidence_exposed": false,
 "timing_s": {"propose": 1375.4, "bench": 412.1, "wall": 1801.9},
 "solver_cost_micro": 81234, "sandbox_cost_micro": 117177, "proposer_list_price_micro": 12310000,
 "started_utc": "...", "ended_utc": "..."}
```

`kind`: `baselines` (iteration 0), `scored`, `zero_valid` (no candidate passed validation), `proposer_failed` (the proposer returned nothing; upstream writes no row for it), `recovered` (written at start-up for an iteration that has upstream rows but no record of ours). `frontier_before` comes from the proposal record (the value read when the original session was entered, before this iteration's benchmarks rewrote `frontier_val.json`); for `proposer_failed` it is the value read at that call. `evidence_exposed` is true when the session's `state_access_signals` is not empty: the proposer may have seen records upstream does not offer (per-example predictions, raw replies), so the iteration's evidence is not exactly upstream's (V20). `solver_cost_micro` and `sandbox_cost_micro` are sums over the job records of this iteration's candidates at the time the record is written; they are what the search run reports (3.23). `avg_val_all_datasets` divides by the number of configured datasets, counting a missing one as 0. `rejected` is `null`, `"name"`, `"import"`, `"loaded_class"` (the class the loader would pick is not defined in the candidate's own module, 3.13) or `"over_limit"`. `context` reports the candidate's context size three ways, because the paper's "four times fewer context tokens" is a ratio against a baseline the release does not contain and must not be repeated from characters alone [r2]: `memory_context_chars` per dataset is upstream's own figure from each succeeded validation job's result file; `memory_context_chars_avg` is their mean over the datasets that succeeded; `chars_div_4` is that mean divided by four (the usual rough token estimate, labelled as such); `prompt_tokens_mean` and `prompt_tokens_max` are the provider's real prompt-token counts over the completed calls of those jobs (`gateway.prompt_tokens_sum / usage_calls`, and the largest `prompt_tokens_max`), `null` when no call carried usage. `name_collision` is true when a usable result existed for this name under a different file hash (upstream then silently reuses the old result; we follow upstream and flag it).

### 2.12 Example row (`mhforge.example/1`), output of the pure join

```json
{"schema": "mhforge.example/1", "dataset": "USPTO", "split": "val", "index": 7, "example_id": "0007-e3b0c44298fc",
 "example_sha256": "<64 hex>", "question_sha256": "<64 hex>", "prediction": "...", "prediction_sha256": "<64 hex>",
 "target": "...", "target_sha256": "<64 hex>", "was_correct": false,
 "metrics": {"jaccard_similarity": 0.0}, "passes": 2, "ambiguous": false, "matched": true,
 "calls": [{"user_sha256": "<64 hex>", "call_id": "c-...", "outcome": "completed", "input_tokens": 367,
            "output_tokens": 912, "latency_s": 20.411, "finish_reason": "stop", "truncated_empty": false}],
 "n_calls": 1, "input_tokens": 367, "output_tokens": 912, "latency_s": 20.411, "finish_reason": "stop", "truncated_empty": false}
```

`matched` is false when the example's calls could not be joined to gateway records (tokens and latency are then 0 and the table marks the row incomplete). For a `replayed` call the source call's usage is reported with its outcome, so a row shows what the answer cost when it was first generated.

### 2.13 Span plan (`mhforge.spanplan/1`)

`STATE/export/plans/<unit-slug>/spans.json`. Pure data; nothing in it is an SDK object; the adapter turns it into SDK calls one to one.

```json
{
  "schema": "mhforge.spanplan/1", "unit": "job:val.fewshot_all.LawBench.a1", "kind": "solver_job",
  "project": "<entity>/<project>", "placeholder_project": false, "export_attempt": 1,
  "conversations": [
    {
      "conversation_id": "mh-forge-01.val.fewshot_all.LawBench.a1",
      "conversation_name": "fewshot_all / LawBench / val",
      "attributes": {"metaharness.run": "mh-forge-01", "metaharness.role": "solver", "metaharness.candidate": "fewshot_all",
                     "metaharness.dataset": "LawBench", "metaharness.split": "val", "metaharness.attempt": 1,
                     "metaharness.wandb_run_path": "<entity>/<project>/mh-forge-01-eval-val-fewshot_all"},
      "turns": [
        {
          "turn_key": "validate", "agent_name": "metaharness-solver", "agent_version": "5d41402abc4b",
          "started_utc": "...", "ended_utc": "...",
          "input_messages": [], "output_messages": [],
          "attributes": {"metaharness.examples": 50, "metaharness.correct": 21,
                         "metaharness.replayed_calls": 0, "metaharness.refused_calls": 0},
          "error": null,
          "spans": [
            {
              "type": "llm", "key": "c-20261110T031502-9f2c41d07a6b",
              "model": "openai/gpt-oss-120b", "provider_name": "wandb",
              "response_model": "openai/gpt-oss-120b", "response_id": "chatcmpl-...",
              "system_instructions": ["Reasoning: medium"],
              "input_messages": [{"role": "user", "parts": [{"type": "text", "content": "<policy applied>"}]}],
              "output_messages": [{"role": "assistant", "parts": [{"type": "reasoning", "content": "<policy applied>"},
                                                                  {"type": "text", "content": "..."}]}],
              "request_max_tokens": 16384, "request_temperature": 0.0, "finish_reasons": ["stop"],
              "usage": {"input_tokens": 24310, "output_tokens": 1422, "total_tokens": 25732,
                        "reasoning_tokens": null, "cache_read_input_tokens": null, "cache_creation_input_tokens": null},
              "started_utc": "...", "ended_utc": "...",
              "attributes": {"metaharness.call_id": "c-20261110T031502-9f2c41d07a6b", "metaharness.example_id": "0007-e3b0c44298fc",
                             "metaharness.outcome": "completed", "metaharness.usage_available": true,
                             "metaharness.provider_attempts": 1, "metaharness.truncated_empty": false,
                             "metaharness.content_truncated": true},
              "error": null
            }
          ]
        }
      ]
    }
  ],
  "expected_spans": 51, "planned_bytes": 96412, "truncated_fields": 150,
  "content_policy": {"dataset": "LawBench", "span_prompt": "hash", "span_reply": "hash", "span_reasoning": "hash", "tool_result": "hash"},
  "sources": {"job_sha256": "<64 hex>", "calls": 50, "examples": 50}, "problems": []
}
```

- `kind`: `solver_job`, `proposer_session`, `outer_loop`.
- Span `type`: `llm`, `tool`, `subagent`.
  - `tool`: `{"type": "tool", "key": "toolu_...", "name": "Bash", "tool_call_id": "toolu_...", "arguments": "<JSON text, policy applied>", "result": "<text, policy applied>", "started_utc", "ended_utc", "attributes": {...}, "error": null | {"type": "ToolError", "message": "..."}}`.
  - `subagent`: `{"type": "subagent", "key": str, "name": str, "model": str, "started_utc", "ended_utc", "attributes": {...}, "error": null, "spans": [...]}`.
- Messages are `{"role": str, "parts": [{"type": "text"|"reasoning", "content": str}]}`, the shape the SDK's `Message(role, parts=[TextPart | ReasoningPart])` takes [rep AL].
- `usage` is `null` unless both token counters are integers (never zeros; H2 `complete_usage` [rep]). It appears only on `llm` spans, and only on spans whose call was paid for (`outcome` `completed`); replayed, coalesced, refused, rejected and failed calls carry `usage: null`, so nothing is counted twice.
- `error`: `null` or `{"type": str, "message": str}`; the adapter calls `record_error`.
- **Turns carry no content.** A turn is the `invoke_agent` span; its `input_messages` and `output_messages` are always empty lists. What a turn is about goes into its attributes as numbers and short identifiers (`examples`, `correct`; for a proposer turn the prompt's `prompt_sha256` and `prompt_chars`). A turn that carried the prompt would store every byte twice [r2].
- `content_policy` records the rule set the plan was made under: the dataset it belongs to (`null` for a plan that is not attributable to one dataset, which uses the strictest rule of all datasets, 3.24) and the four rules.
- `expected_spans` = number of turns + number of spans at every depth. It is the exact count the read-back must find, and the figure charged to the `lens_spans` meter.
- `planned_bytes` = `ceil(telemetry.agent_lens.byte_estimate_factor * sum(policy.span_bytes(s)))` over every turn and every span at every depth, where `policy.span_bytes(s)` is `len(json.dumps(s))` of that turn or span **without** its nested `spans` list, with Python's default separators and ASCII escaping (so a Chinese character counts six bytes, as it does in the stored span) [r2; the estimator against stored spans is live: PF8]. It is the figure charged to the `lens_bytes` meter.
- Attribute keys are leaves under one prefix; integers stay integers; no key extends another with a dot; no key or value is credential-shaped (3.24).
- `placeholder_project` (span, table and run plans alike): when `decisions.entity` or `decisions.project` is null the planners use the project `unset-entity/unset-project` and set this flag. Such a plan can only receive a `dry_run` receipt; a live export of it is `skipped_policy` (it cannot arise live, because the export gate requires both decisions). This is what lets the offline smoke, whose decisions stay unset, plan every unit.

### 2.14 Table plan (`mhforge.tableplan/1`)

`STATE/export/plans/<unit-slug>/tables.json` is the wrapper `{"schema": "mhforge.tableplanset/1", "unit": "eval:val.no_memory", "placeholder_project": false, "tables": [<table plan>, ...]}`; each element is one table plan (`mhforge.tableplan/1`):

```json
{
  "schema": "mhforge.tableplan/1", "unit": "eval:val.no_memory", "job_id": "val.no_memory.USPTO.a1",
  "run_ref": "eval:val.no_memory", "key": "val/USPTO", "step": 0, "backend": "ces",
  "columns": ["dataset", "split", "example_id", "question_sha256", "prediction_sha256", "target_sha256", "n_calls", "input_tokens",
              "output_tokens", "latency_s", "finish_reason", "truncated_empty", "row_complete", "metrics_json", "trace_url",
              "was_correct", "jaccard_similarity"],
  "input_columns": ["dataset", "split", "example_id", "question_sha256"],
  "output_columns": ["prediction_sha256", "target_sha256", "n_calls", "input_tokens", "output_tokens", "latency_s", "finish_reason",
                     "truncated_empty", "row_complete", "metrics_json", "trace_url"],
  "score_columns": ["was_correct", "jaccard_similarity"],
  "column_types": {"dataset": "str", "split": "str", "example_id": "str", "question_sha256": "str", "prediction_sha256": "str",
                   "target_sha256": "str", "n_calls": "int", "input_tokens": "int", "output_tokens": "int", "latency_s": "float",
                   "finish_reason": "str", "truncated_empty": "bool", "row_complete": "bool", "metrics_json": "str",
                   "trace_url": "str", "was_correct": "bool", "jaccard_similarity": "float"},
  "rows": [["USPTO", "val", "0000-9a0364b9e99b", "<64 hex>", "<64 hex>", "<64 hex>", 1, 371, 1204, 27.43, "stop", false, true,
            "{\"jaccard_similarity\":0.0}", "https://forge.coreweave.com/agent-lens/<entity>/<project>/conversations/mh-forge-01.val.no_memory.USPTO.a1",
            false, 0.0]],
  "row_count": 30,
  "summary": {"accuracy": 0.1, "correct": 3, "total": 30},
  "demoted_scores": [], "dropped_columns": [],
  "checks": {"rows_1_to_10000": true, "primitive_cells": true, "single_type_columns": true, "no_all_null_columns": true,
             "no_null_or_nan_scores": true, "inputs_are_strings": true, "matches_result_file": true}
}
```

- Inputs are identifiers and hashes only (no dataset text), all strings, identical for the same example in every candidate's table, because rows align across runs by a hash of the whole input [rep ET]: `dataset`, `split`, `example_id` and `question_sha256` (2.8). A reader who holds the upstream data at `f805ad3` can recompute `question_sha256` and so find the example; nobody else learns its text.
- **Prediction and target follow the dataset.** For a dataset whose targets are category names (Symptom2Disease, LawBench) the output columns are `prediction` and `target`, each a short label: a value longer than 200 characters is not a label and is replaced by `[sha256 <64 hex> chars <n>]`. For a dataset named in `wire.CLEARANCE_GATED` (USPTO, whose target is a unique reactant string per row, so that a correct prediction publishes the gold string [r2]) the columns are `prediction_sha256` and `target_sha256` instead, until the decision named there (`decisions.uspto_text_cleared`) is true. The example above is a USPTO table.
- A table counts against `budgets.eval_tables_max` when it is logged (3.25).
- A plan with any failed check is invalid and is never handed to an adapter.
- Score columns are `was_correct` (always) and, of the metrics, exactly `jaccard_similarity`, `f1`, `precision` and `recall`, each only if every row has a finite number; otherwise that metric stays only in `metrics_json` and is named in `demoted_scores`. Every other metric (LawBench's evaluator also returns the integers `tp`, `fp`, `fn`, `data/evaluators.py` L170-L182 [A-S]) stays in `metrics_json` and is never a column. Output columns that are entirely null are dropped and listed in `dropped_columns`.
- `backend` is `ces`, `wandb_table`, or `unset` when `decisions.evaltables_backend` is unset; an `unset` plan can only receive a `dry_run` receipt.

### 2.15 Run plan (`mhforge.runplan/1`)

`STATE/export/plans/<unit-slug>/runs.json`.

```json
{
  "schema": "mhforge.runplan/1", "unit": "eval:val.no_memory", "placeholder_project": false, "export_attempt": 1,
  "run": {
    "ref": "eval:val.no_memory", "id": "mh-forge-01-eval-val-no_memory",
    "entity": "<entity>", "project": "<project>", "group": "mh-forge-01", "job_type": "evaluate",
    "name": "no_memory (val)", "tags": ["baseline"], "resume": false,
    "config": {"campaign": "mh-forge-01", "candidate": "no_memory", "split": "val", "iteration": null, "base_system": null,
               "candidate_sha256": "<64 hex>", "upstream_commit": "f805ad3...", "manifest_sha256": "<64 hex>"},
    "summary": {"val/USPTO/accuracy": 0.1, "val/macro_accuracy_all_datasets": 0.2, "datasets_complete": 3, "datasets_failed": 0,
                "solver_cost_usd": 0.00648, "truncated_empty_calls": 0,
                "conversation_urls": "{\"USPTO\": \"https://...\"}",
                "evidence_hashes": "{\"USPTO\": {\"memory_json_sha256\": \"<64 hex>\", \"memory_json_bytes\": 2, \"log_jsonl_sha256\": \"<64 hex>\", \"log_jsonl_bytes\": 5120}}"},
    "define_metrics": [],
    "history": [],
    "tables": ["val/USPTO", "val/Symptom2Disease", "val/LawBench"],
    "artifacts": [
      {"op": "log", "name": "candidate-no_memory", "type": "harness-candidate", "aliases": ["mh-forge-01"],
       "files": [{"source": "workspace/text_classification/agents/no_memory.py", "name": "no_memory.py", "sha256": "<64 hex>", "bytes": 1408}],
       "metadata": {"iteration": null, "axis": null, "hypothesis": null, "components": null, "base_system": null,
                    "file_sha256": "<64 hex>", "upstream_commit": "f805ad3..."}}
    ]
  },
  "expect": {"summary_markers": ["val/USPTO", "val/Symptom2Disease", "val/LawBench"], "artifacts": ["candidate-no_memory"]}
}
```

| `job_type` | Unit | Run id | Content |
|---|---|---|---|
| `search` | `loop:iter<NNN>` | `<campaign>-search` (`resume: true` for `NNN > 0`) | `define_metrics: [["iteration", null], ["search/*", "iteration"]]` (the adapter calls `run.define_metric("iteration")` and `run.define_metric("search/*", step_metric="iteration")`), and one history row `{"iteration": NNN, "search/...": ...}` logged **without** an explicit `step`: with an explicit step, a row retried after a later iteration was logged would be dropped [rep ET; inf]. The row holds upstream's frontier value, our best complete average, candidates proposed / valid / complete, failed jobs, cumulative solver cost, cumulative proposer list price, cumulative sandbox cost, timings. Cumulative figures are sums over the iteration records up to `NNN`, never a live ledger snapshot, so the plan of a settled iteration does not change when export runs again after more spend. The one run id is resumed by every block of a search that runs in several invocations (`--max-iterations-this-run`), and `iteration`, not the step counter, is the x axis, so the chart is the same however the search was cut into blocks. Config = the allow-listed subset of the frozen manifest (below; iteration 0 only). |
| `propose` | `propose:<candidate>` | `<campaign>-propose-<candidate>` | `log` the candidate file Artifact with `base_system`, iteration, axis, hypothesis, components and file hash in its metadata; `use` the base candidate's Artifact when it exists (best effort; a failed `use` is a receipt problem, not a failure) |
| `evaluate` | `eval:<split>.<candidate>` | `<campaign>-eval-<split>-<candidate>` | one table per dataset that succeeded; summary; `use` the candidate Artifact (baselines `log` it here, because they have no propose run) |
| `publication` | (publish only) | `<campaign>-publication` | bundle Artifact |

`source` paths in `files` are relative to `STATE`. Artifact `op` is `log` or `use`.

**Run config is an allow-list.** A run's `config` is built only from the keys of `plan_runs.RUN_CONFIG_ALLOW` (3.23) and then passed through `policy.portable`. It never holds a local path (`upstream.example_dir`, `upstream.worker_python`, `state_dir`, `sandbox.wheelhouse.tar_path`, `proposer.scratch_root`), the CLI path (`proposer.cli_path`), a key variable name, the `notes` field, or anything read by `account-check` (plan, privileges, flags, headroom): account facts stay in `STATE/account/` and enter no plan. A private project is readable by every member of the team [r2], so this matters from the first export, not only at publication.

**Artifacts are candidate files only.** The only file a run plan ever names is a candidate file under `agents/`, and only after the verbatim-text scan of 3.24 found no dataset text in it; a file with a finding is withheld (the Artifact is logged with its metadata and the file's hash and size, without the file, and the receipt lists it under `withheld_files`). `memory.json`, `log.jsonl`, the worker's reply cache and proposer transcripts are never uploaded in any form: `log.jsonl` is treated exactly like `memory.json` everywhere, because its last checkpoint row is the candidate's whole memory state, which for `fewshot_all` is the training split verbatim [r2]. The evaluate run's summary carries their SHA-256 and size per dataset (`evidence_hashes`), nothing more. The same scan covers Artifact metadata (`hypothesis`, `axis`, `components`) and every outer-loop span argument and result; a string with a finding is replaced by its `hash` form.

### 2.16 Receipt (`mhforge.receipt/1`)

`STATE/export/receipts/<unit-slug>.<surface>.json`, one per unit and surface (`agent_lens`, `wandb`), replaced atomically at each status change.

```json
{
  "schema": "mhforge.receipt/1", "unit": "job:val.fewshot_all.LawBench.a1", "surface": "agent_lens", "export_attempt": 1,
  "status": "verified",
  "plan_sha256": "<64 hex>", "stale_plan_sha256": null,
  "scan": {"clean": true, "findings": 0, "scanned_bytes": 412338, "files": 53, "rules": 16, "classes": ["secret", "privacy"]},
  "amendment": {"seq": 0, "row_sha256": null},
  "bucket": "main", "withheld_files": [],
  "conversation_ids": ["mh-forge-01.val.fewshot_all.LawBench.a1"],
  "trace_ids": ["4bf92f3577b34da6a3ce929d0e0e4736"],
  "expected_spans": 51,
  "readback": {"spans": 51, "checked_utc": "...", "waited_s": 6.0, "mismatches": []},
  "remote": {"run_path": null, "table_keys": [], "artifact_names": [], "backend": null, "base_url": null},
  "urls": ["https://forge.coreweave.com/agent-lens/<entity>/<project>/conversations/mh-forge-01.val.fewshot_all.LawBench.a1"],
  "planned_bytes": 96412, "planned_spans": 51,
  "adapter": {"name": "coreweave", "version": "0.1.0b0", "unverified": true},
  "created_utc": "...", "updated_utc": "...", "error": null,
  "history": [{"status": "planned", "utc": "..."}, {"status": "scanned", "utc": "..."}, {"status": "emitting", "utc": "..."}, {"status": "emitted", "utc": "..."}]
}
```

`status` moves only forward: `planned` -> `scanned` -> `emitting` -> `emitted` -> `verified`. Terminal alternatives: `refused_secret` (the scan found something in any class, a secret, a local path or the host name; nothing was sent), `skipped_budget` (a bucket of the ingest cap is empty, the per-session cap, the upload cap or the table cap), `skipped_policy` (surface off, or an invalid table plan), `dry_run` (planned and scanned only), `partial` (read-back found fewer than expected, or more), `failed` (nothing was sent). A receipt in `emitting` or `emitted` is never re-emitted: the next step is a read-back (H2 `telemetry.py` L327-L336, L373-L374 [rep]). For the `wandb` surface, `remote` holds the run path, the table keys, the Artifact names, the table backend actually used (`ces` or `wandb_table`) and the service's base URL. `amendment` is the latest row of `STATE/amendments.jsonl` the plan was made under (`seq` 0 and `null` when there was none). `bucket` is the ingest bucket the bytes were charged to (`null` for the `wandb` surface). `withheld_files` lists candidate files the verbatim scan kept out of an Artifact (name, SHA-256, size). `stale_plan_sha256` is `null`, or the hash of a newer plan that differs from the one this receipt was made for (the records changed after the export, or an amendment changed the content rules); it is an annotation on a final receipt and changes nothing remote (3.25 step 1). A `dry_run` receipt is final only for dry runs: a live export replaces it.

---
## 3. Per-module contracts

Signatures are Python 3.11. `manifest: dict` always means the frozen manifest of 2.3; modules read it with `util.lookup(manifest, "a.b.c")`. "Never" lists are test targets.

**Environment access, complete list.** Names only are ever recorded. `test_environment_access_only_in_listed_modules` checks the source against exactly this table: the name `os.environ` (or `os.getenv`, `os.putenv`) may appear only in the modules of the first two blocks.

*Modules that read the process environment.* They pass it on as a mapping; no other module has a default that falls back to `os.environ`.

| Module | What it does with `os.environ` |
|---|---|
| `cli.py` | passes it as `environ` to `manifest.live_blockers`, to `Driver` (`base_env`) and to `ExportSpawner` (`base_env`); with `--key-source env` it is also the key mapping handed to the provider client, the sandbox client and the adapters (with `prompt` or `fd:N` those get a private dict keyed by the same manifest names, 3.27) |
| `smoke.py` | reads `MHFORGE_EXAMPLE_DIR`; sets and removes the canary key variable |
| `tests/helpers.py` | reads `MHFORGE_EXAMPLE_DIR` |

*Modules that set or scrub a variable in their own process* (never read a value they did not just set).

| Module | Variables |
|---|---|
| `util.py` (`scrub_process_env`) | overwrites in place and removes the one key variable it is given (3.9) |
| `cli.py` | sets `NETRC` to the empty file `STATE/export/empty.netrc` at the start of every live command, before any SDK can be imported (3.27) |
| `executors/sandbox_client.py` | sets `CWSANDBOX_DISABLE_SIGNAL_HANDLERS=1`; in auth mode `wandb_auth` also `WANDB_API_KEY`, set inside the process from the key it was given (a variable set after start is not in the start-up environment block that `ps eww` shows [r2]) |
| `telemetry/adapter_agentlens.py` | removes `OTEL_RESOURCE_ATTRIBUTES`; sets `OTEL_BSP_MAX_EXPORT_BATCH_SIZE`, `OTEL_EXPORTER_OTLP_TRACES_COMPRESSION` |
| `telemetry/adapter_wandb.py` | sets `NETRC` (the same empty file), `WANDB_DIR`, `WANDB_CACHE_DIR`, `WANDB_DATA_DIR`, `WANDB_CONSOLE`, `WANDB_DISABLE_GIT`, `WANDB_DISABLE_WEAVE` |
| `insandbox/mhf_selfcheck.py` (inside the executor) | lists the names in its own environment and whether a credential-shaped one is non-empty; never a value |
| `insandbox/mhf_relay_stub.py` (inside the executor) | reads `MHF_STUB_BEARER` from its own environment |

*Variables, by the mapping they are read from.*

| Variable | Read by (from the mapping it is given) | Purpose |
|---|---|---|
| the name in `provider.api_key_env` | `provider.ForgeProviderClient.__init__` in the runner process, once, from the key mapping; with `--key-source env` the variable is then overwritten in place and deleted | Forge key for inference (F14) |
| the name in `sandbox.auth_env` | `executors/sandbox_client.CwSandboxClient.__init__` in the runner process, once, from the key mapping; with `--key-source env` then overwritten in place and deleted | W&B credential for the sandbox API |
| the name in `telemetry.api_key_env` | `telemetry/adapter_agentlens.py`, `telemetry/adapter_wandb.py`, in the exporter process, from the key mapping. An exporter child never gets the key in its environment: the spawner hands it over an inherited pipe (`--key-source fd:N`, 3.25) | W&B and Agent Lens key for the exporter |
| names in `proposer.env_allow` | `procenv.proposer_env` | copied into the proposer's environment |
| `PATH` | `procenv.proposer_env` (to locate `uv` for `{uv_dir}`), `procenv.exporter_env`, `procenv.minimal_env` | child environments |
| `MHF_STUB_BEARER` | `insandbox/mhf_relay_stub.py` (its own environment; set only by `LocalRelayExecutor`) | bearer the stub requires on its loopback listener when it runs on the host (3.13) |
| `WANDB_BASE_URL`, `CES_BASE_URL`, `WANDB_APP_URL`, `WANDB_MODE`, `OTEL_RESOURCE_ATTRIBUTES`, `WF_TRACE_SERVER_URL`, `WANDB_IDENTITY_TOKEN_FILE`, `OTEL_EXPORTER_OTLP_ENDPOINT` | presence check in `manifest.live_blockers` for `export` | must be unset |
| `CLAUDECODE`, `CLAUDE_CODE_ENTRYPOINT` | presence check in `manifest.live_blockers` for `search`, and in the proposer steps of `preflight` | must be unset: a live proposer is started from the user's own terminal |
| `MHFORGE_EXAMPLE_DIR` | `tests/helpers.py` and `smoke.py` only | override the example directory |

### 3.1 `errors.py`

```python
class MhforgeError(Exception): ...
class ManifestError(MhforgeError):                 # invalid, unfrozen, or differs from the frozen copy
    problems: list[str]
class LiveGateError(MhforgeError):                 # a live command refused
    blockers: list[tuple[str, str]]                # (manifest path or prerequisite, message)
class StateLockError(MhforgeError):                # another process holds the lock
    holder: dict
class StateError(MhforgeError): ...                # a state file is malformed
class UpstreamContractError(MhforgeError): ...     # a hook this design relies on is not where it was
class WorkspaceError(MhforgeError): ...
class BudgetError(MhforgeError):
    kind: str                                      # "invalid" | "halted" | "unreconciled" | "ceiling" | "total" | "cap" | "settled"
    phase: str | None
    detail: str | None                             # for "ceiling": "requests" | "attempts" | "job_usd"
class MeterCapExceeded(MhforgeError):
    meter: str
    bucket: str | None                             # the ingest bucket that is empty, for lens_bytes
class ProviderConfigFault(MhforgeError): ...       # missing key, wrong base URL
class RelayProtocolError(MhforgeError): ...
class ChannelClosed(MhforgeError): ...
class ExecutorError(MhforgeError):
    kind: str                                      # "create" | "quota" | "upload" | "exec" | "selfcheck" | "credential_visible" | "egress_open"
                                                   # | "image_mismatch" | "auth" | "env_setup" | "billing" | "permission"
    retryable: bool                                # False for image_mismatch, auth, billing, permission: the campaign stops at once (3.18)
class PlanError(MhforgeError):
    problems: list[str]
class ExportError(MhforgeError): ...
class SecretFound(ExportError):
    findings: list                                 # scan.Finding; never the matched text
class AdapterUnavailable(ExportError): ...         # an SDK is not importable, or `weave` is importable (3.25)
class TranscriptError(MhforgeError):               # the transcript converter refuses its input (3.21)
    kind: str                                      # "version" | "shape"
class SmokeFailed(MhforgeError):
    invariants: list[str]
class CampaignStop(BaseException):                 # BaseException on purpose: no upstream `except Exception` can swallow it [X-E4]
    reason: str; detail: str; phase: str | None

HALT_REASONS = frozenset({
    "budget:qualification", "budget:calibration", "budget:baseline", "budget:search", "budget:test", "budget:controls",
    "budget:recovery", "budget:total", "budget:sandbox", "budget:proposer", "usage_contradiction", "ledger_fault",
    "model_identity", "provider_config", "provider_quota", "isolation", "operator"})
STOP_REASONS = HALT_REASONS | frozenset({
    "proposer_spend_limit", "proposer_limit_wait", "proposer_auth", "proposer_transient", "proposer_model", "proposer_sandbox",
    "proposer_settings", "proposer_integrity", "proposer_cli", "proposer_environment", "infrastructure",
    "baseline_incomplete", "qualification_failed", "forecast_exceeds_budget", "upstream_contract", "internal_error",
    "interrupted"})

EXIT_OK, EXIT_ERROR, EXIT_STOPPED, EXIT_REFUSED, EXIT_LOCKED, EXIT_SMOKE_FAILED = 0, 1, 2, 3, 4, 5
```

Every exception takes its fields as constructor keyword arguments and puts a one-line message in `args[0]`. Exit codes: 0 success; 1 unexpected error (after `stopped.json` is written with reason `internal_error` when it happened inside a runner command); 2 `CampaignStop` (after `STATE/stopped.json` is written); 3 refused (`LiveGateError`, `ManifestError`, a missing prerequisite such as the example venv); 4 `StateLockError`; 5 an offline-smoke invariant failed.

`CampaignStop` is the only way the search stops early on purpose. It is raised only on the runner's main thread, from the replaced upstream globals or from the driver itself. Any other exception that escapes upstream's loop is caught by the driver, journaled as `run: stopped` with reason `upstream_contract` (an `UpstreamContractError`) or `internal_error` (anything else) and the last 2,000 characters of the traceback, written to `stopped.json`, and turned into exit code 1; it is never swallowed and never left as a bare crash that the next start would repeat without a record (3.20).

`ledger_fault` is the halt for a ledger that could not be written (a settle that kept failing, 3.10). `provider_quota` is the halt for a 429 that says the quota is exhausted (3.9). `baseline_incomplete` stops the campaign after Phase 0 when a baseline has no usable result on some dataset (3.20). The proposer stops map one to one to the session outcomes that end a campaign (3.19): `proposer_spend_limit` (`spend_limit`), `proposer_limit_wait` (a `limit_window` whose reset is further away than `proposer.max_limit_wait_s`), `proposer_auth` (`auth`), `proposer_transient` (`transient`, reruns used up), `proposer_model` (`model_mismatch`), `proposer_sandbox` (`sandbox_unavailable`), `proposer_settings` (`settings_ignored`), `proposer_integrity` (a failed integrity check after a session), `proposer_cli` (binary missing, changed, or not the pinned version), `proposer_environment` (the Python environment changed, the scratch directory is unusable, or the generated settings failed their own check). A `search` block that ends at `--max-iterations-this-run` is not a stop: it is journaled `run: paused` and exits 0.

### 3.2 `util.py` (P), `wire.py`, `paths.py`

```python
# util.py  (P: write_json from H2 gateway.py L19-L22; tree_hashes, changed_files from protocol.py; micros from budget.py)
class Clock(Protocol):
    def now(self) -> float: ...                    # seconds since the Unix epoch (UTC)
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...
class SystemClock: ...                             # time.time, time.monotonic, time.sleep
def utc_iso(epoch: float) -> str                   # "2026-11-10T01:02:03.000001Z"
def utc_now(clock: Clock | None = None) -> str
def parse_utc(text: str) -> float                  # ValueError unless the text ends with "Z"
class IdSource:
    def __init__(self, clock: Clock | None = None, rng: random.Random | None = None) -> None
    def call_id(self) -> str                       # "c-<YYYYMMDDTHHMMSS>-<12 hex>"
    def ledger_id(self) -> str                     # "l-<YYYYMMDDTHHMMSS>-<12 hex>"
    def token(self) -> str                         # "mhfjob_" + secrets.token_urlsafe(32)
def canonical_json(value: object) -> bytes         # 2.1; ValueError on NaN or Infinity
def sha256_bytes(data: bytes) -> str
def sha256_file(path: Path) -> str
def write_json(path: Path, value: object, *, fsync: bool = False) -> None      # temp file in the same directory, os.replace
def read_json(path: Path, *, max_bytes: int = 64 * 1024 * 1024) -> object      # StateError over max_bytes or on bad JSON
def append_jsonl(path: Path, row: dict, *, fsync: bool = False) -> None        # one os.write on an O_APPEND descriptor
def read_jsonl(path: Path) -> list[dict]           # ignores a torn last line; StateError on a bad line elsewhere
def tree_hashes(root: Path, *, include: Sequence[str] | None = None,
                exclude_dirs: frozenset[str] = frozenset({".venv", "__pycache__", "logs", ".empty_plugins"})) -> dict[str, str]
                                                   # relative posix path -> sha256; `include` are fnmatch patterns; WorkspaceError on a symlink
def tree_digest(hashes: Mapping[str, str]) -> str  # sha256 over sorted "path\0sha256\n"
def changed_files(before: Mapping[str, str], after: Mapping[str, str]) -> dict[str, list[str]]   # keys: added, removed, modified
def micros(value: str | int | Decimal) -> int      # USD -> micro-dollars, ROUND_CEILING
def usd(micro: int) -> str                         # "0.006480"
def lookup(mapping: Mapping, dotted: str, default: object = ...) -> object     # KeyError when absent and no default
def safe_component(text: str) -> str               # ValueError unless re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", text)
def open_sqlite_ro(path: Path) -> tuple[sqlite3.Connection, bool]
    # the read-only open of 2.10: (connection, possibly_stale); never creates -wal or -shm; works on a read-only copy
def sleep_gap(wall_delta_s: float, monotonic_delta_s: float) -> float
    # max(0.0, wall_delta_s - monotonic_delta_s): how long the host was asleep between two heartbeats. time.monotonic() does not
    # advance while a Mac sleeps and the wall clock does, so a gap above runner.sleep_gap_s means the host slept (3.16)
def loopback_guard() -> ContextManager[None]
    # while active, any socket connect of THIS process to a non-loopback address raises; children are not covered (5.3)
def scrub_process_env(name: str) -> bool
    # overwrite the value bytes of NAME in the process environment block in place
    # (ctypes.CDLL(None).getenv with restype c_void_p, then ctypes.memset(ptr, ord("x"), len(value_bytes))), then delete NAME
    # from os.environ; returns False when the overwrite was not possible (the variable is still deleted) [R3, A-S d2_scrub]
def append_key_digest(path: Path, name: str, value: str, *, clock: Clock | None = None) -> dict
    # adds {"name", "len", "sha256", "utc"} to the keydigests file of 2.1 (mode 0600, one entry per digest); returns the entry
```

`scrub_process_env` exists because deleting a variable from `os.environ` does not remove it from the environment block the process was started with: any process of the same user can read that block (`ps eww`) for the life of the process [R3]. After the in-place overwrite the listing shows `x` characters. The function is the only code besides `cli.py` and `smoke.py` that touches the real process environment, and it is passed into the components that read a key (`scrub=util.scrub_process_env`), so they stay testable with a plain dict.

```python
# wire.py
JOB_PHASES = ("qualification", "calibration", "baseline", "search", "test", "controls")
LEDGER_PHASES = JOB_PHASES + ("recovery",)        # seven; the CHECK constraint and the phase_totals rows of 2.10, the caps of 2.3
JOB_STATUSES = ("succeeded", "failed", "skipped_usable", "skipped_no_memory", "halted")
ATTEMPT_OUTCOMES = ("ok", "worker_error", "no_result", "timeout", "request_ceiling", "relay_lost", "provider_unavailable",
                    "executor_error", "env_setup_failed", "halted", "interrupted")
INFRA_OUTCOMES = frozenset({"relay_lost", "provider_unavailable", "executor_error", "env_setup_failed", "halted", "interrupted"})
REUSE_SCOPES = ("search", "job", "qualification", "repeat")      # replay.scope of a call record (2.5); the first two are manifest values
SESSION_OUTCOMES = ("ok", "no_candidates", "timeout", "failed", "spend_limit", "limit_window", "auth", "transient",
                    "model_mismatch", "sandbox_unavailable", "settings_ignored", "cli_missing", "interrupted")     # 2.11
CONTENT_FIELDS = ("span_prompt", "span_reply", "span_reasoning", "tool_result")                                   # 2.3 telemetry.content
NEVER_PUBLISH_TEXT = ("LawBench",)                 # datasets whose content rules must stay "hash" (2.3 rule 16)
CLEARANCE_GATED = {"USPTO": "decisions.uspto_text_cleared"}     # dataset -> the decision that must be true before its text or
                                                                # its prediction and target strings may be shown (2.3 rule 16, 2.14)
CAMPAIGN_RE = re.compile(r"[a-z0-9][a-z0-9-]{2,39}")          # always used with .fullmatch
CANDIDATE_RE = re.compile(r"[a-z][a-z0-9_]{0,47}")            # always used with .fullmatch; "x\n" must be rejected
CREDENTIAL_NAME_PATTERN = r"(?:^|_)(API_?KEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)(?:_|$)"   # re.search on an upper-cased name
CREDENTIAL_NAME_EXEMPT = ("OPENROUTER_API_KEY", "TIKTOKEN_RS_CACHE_DIR")   # exact names the in-executor check ignores
def bounded_id(text: str, limit: int = 128) -> str
    # text when len(text) <= limit; else text[:limit - 9] + "-" + sha256(text)[:8]

@dataclass(frozen=True)
class JobIdentity:                                 # the one identity type; used by ledger, gateway, relay, executors
    campaign: str; split: str; candidate: str; dataset: str; attempt: int
    phase: str                                     # one of JOB_PHASES, never "recovery"
    iteration: int | None                          # upstream iteration for phase "search", else None
    repeat: int | None = None                      # 2..99: a noise repeat (3.20); None for an ordinary job
    sample: int | None = None                      # 1..99: a sample of the deferred sampling control; None otherwise
    @property
    def job_key(self) -> str                       # f"{split}.{candidate}.{dataset}", prefixed "qual." when phase == "qualification",
                                                   # suffixed f".r{repeat}" and f".s{sample}" when those are set (2.2)
    @property
    def job_id(self) -> str                        # f"{job_key}.a{attempt}"
    def to_dict(self) -> dict                      # the nine fields plus job_key and job_id (the "job" object of 2.5)
    @classmethod
    def from_dict(cls, d: Mapping) -> "JobIdentity"     # validates every field; ValueError otherwise

def ledger_phase(job: JobIdentity, provider_attempt: int, *, prior_charged: bool) -> str
    # "qualification" stays; else "recovery" when provider_attempt > 1, or when job.attempt > 1 and prior_charged
    # (an earlier attempt of this job_key already has a charged provider attempt); else job.phase.
    # A job whose first attempt died before any call (for example at sandbox create) is therefore still booked to its own phase.

@dataclass(frozen=True)
class GatewayReply:
    status: int; headers: tuple[tuple[str, str], ...]; body: bytes

RETRY_TRIGGER_STRINGS = ("429", "rate limit", "too many requests", "retry in", "timed out", "timeout")   # llm.py L98-L108 [src]
SOURCE_HEADER = "x-mhforge-source"                 # provider | replay | coalesced | gateway
RELAY_API_KEY_PLACEHOLDER = "mhf-relay"
REFUSALS: dict[str, tuple[int, str]]               # code -> (status, message); the closed table below
def refusal_body(code: str) -> bytes               # {"error": {"message": <message>, "type": "mhforge_refusal", "code": <code>}}
def refusal_reply(code: str) -> GatewayReply       # headers: content-type application/json, x-should-retry false, x-mhforge-source gateway
def make_envelope(method: str, path: str, body: bytes) -> bytes        # (method + " " + path + "\n").encode("ascii") + body
def parse_envelope(request: bytes) -> tuple[str, str, bytes]           # ValueError on a malformed first line

class HandlesRequests(Protocol):                   # what ingresses and executors need from the gateway; tests use helpers.FakeGateway
    def register_job(self, job: JobIdentity, *, token: str | None = None) -> None: ...
    def close_job(self, job_id: str, *, drain_timeout_s: float | None = None) -> dict: ...
    def handle(self, job: JobIdentity | None, request: bytes, *, ingress: str = "loopback",
               no_replay: bool = False) -> GatewayReply: ...
    def halted(self) -> object | None: ...         # the halt state, or None; executors poll it once a second while a worker runs
    def kill_requested(self, job_id: str) -> str | None: ...   # "refusal_flood" when the worker should be killed (3.10), else None
```

**Closed table of refusals.** Every reply the gateway, the relay session or the stub can give besides a completion. Status is 402 or 403 only (F4). Every one carries `content-type: application/json` and `x-should-retry: false`. No message contains a digit, an id, provider text, or (in any letter case) one of `RETRY_TRIGGER_STRINGS`. No message or code matches, in any letter case, the patterns of LiteLLM's own rate-limit mapping: `\b429\b`, `rate[\s_\-]*limit`, and the phrase `request too large` (LiteLLM 1.84.10 `exception_mapping_utils.py` L51, L57, and the case-sensitive `Request too large` at L505 [A-U]); a match would turn the refusal into a `RateLimitError`, which upstream retries six times. The message of the code `request_too_large` therefore does not use that phrase. `tests/helpers.assert_neutral` checks all of this for the message, and `test_real_client_one_request_per_refusal_both_routes` is the authority for every row. A plain 402 or 403 costs exactly one request on both routes and is not retried by tenacity or the OpenAI SDK [rep CM-V C2; F-E3; X-E2]; the review confirmed that for the thirteen rows of the earlier table with the real client on both routes [A-U E4]. The reworded `request_too_large` message and the new `relay_overloaded` row have not been run against the real client; the authority test covers them at build time.

| Code | Status | Message |
|---|---|---|
| `halted` | 402 | `mhforge gateway: campaign halted` |
| `budget_exhausted` | 402 | `mhforge gateway: budget exhausted` |
| `job_request_ceiling` | 403 | `mhforge gateway: job request ceiling reached` |
| `unknown_job` | 403 | `mhforge gateway: unknown job` |
| `not_allowed` | 403 | `mhforge gateway: only chat completions are served` |
| `invalid_request` | 403 | `mhforge gateway: request not accepted` |
| `model_not_allowed` | 403 | `mhforge gateway: model not in manifest` |
| `request_too_large` | 403 | `mhforge gateway: request exceeds the size bound` |
| `provider_rejected` | 403 | `mhforge gateway: provider rejected the request` |
| `context_length_exceeded` | 403 | `mhforge gateway: provider rejected the prompt length` |
| `provider_unavailable` | 403 | `mhforge gateway: provider unavailable after gateway attempts` |
| `relay_unavailable` | 403 | `mhforge relay: host link unavailable` (issued by the in-executor stub, which carries a copy of this row) |
| `relay_overloaded` | 403 | `mhforge relay: request queue full` (issued by the host relay session when one job has more live requests waiting than `4 * relay.max_workers`; the wording avoids the trigger string `too many requests`) |
| `gateway_internal` | 403 | `mhforge gateway: internal fault` |

The second round added no row to this table. Its new stops (a configuration fault on every 401, 402 and 403, an exhausted quota, the new budget phases) all reach a worker as the existing `halted` or `budget_exhausted` replies, and an environment-setup failure happens before any worker exists.

```python
# paths.py   (path arithmetic only; no I/O)
@dataclass(frozen=True)
class StatePaths:
    root: Path                                     # SC/state/<campaign>
    # properties: runner_lock, manifest, manifest_sha256, engine, amendments, account_root, account_file, runtime, keydigests,
    #             stopped, ledger, replay, circuit, journal,
    #             workspace_root (root/"workspace"), ws (workspace_root/"text_classification"), pristine,
    #             calls_root, jobs_root, quarantine_root, scratch_root, qualification_root, qualification_passed, calibration, noise,
    #             export_root, export_lock, export_netrc (export_root/"empty.netrc"), export_wandb_local, publish_root,
    #             smoke_receipt (root.parent/"_smoke"/"receipt.json")
    def logs_dir(self, campaign: str) -> Path                  # ws / "logs" / campaign
    def calls_dir(self, job_id: str | None) -> Path            # calls/<job_id>, or calls/_unknown for None
    def job_dir(self, job_key: str) -> Path
    def attempt_dir(self, job_key: str, attempt: int) -> Path  # jobs/<job_key>/a<attempt>
    def import_check_dir(self, iteration: int, attempt: int) -> Path   # import_checks/iter-<NNN>/a<attempt>
    def session_dir(self, iteration: int, ordinal: int) -> Path    # proposer/iter-<NNN>/s<k>
    def iteration_file(self, iteration: int) -> Path
    def qualification_dir(self, attempt: int) -> Path
    def plan_dir(self, unit: str) -> Path
    def scan_file(self, unit: str) -> Path
    def receipt_file(self, unit: str, surface: str) -> Path
    def stored_spans_file(self, unit: str) -> Path
    def sibling(self, campaign: str) -> "StatePaths"           # StatePaths(root.parent / campaign): another campaign under the same SC (3.22)
def unit_slug(unit: str) -> str                                # ":" -> "__"; then safe_component
def proposer_scratch(scratch_root: str, campaign: str, session_key: str) -> Path   # Path(scratch_root) / campaign / session_key (3.19)
def resolve_state(manifest: dict, manifest_path: Path) -> StatePaths
    # StateError unless manifest["state_dir"] == "state/" + manifest["campaign"]; root = manifest_path.resolve().parent / "state" / campaign
```

Invariants: `write_json` never leaves a partial file at `path`; `canonical_json` is independent of key order and keeps Unicode; `JobIdentity.from_dict(x.to_dict()) == x`; `bounded_id` is deterministic and never returns more than `limit` characters. Never: local time; floats for money; a non-atomic write to a file another process reads; an identifier check with `re.match` or `re.search`.

### 3.3 `manifest.py` (P: freeze rule from H2 `runtime.py` L55-L62, read-only read from `study.py` L35-L40)

```python
def read_manifest(path: Path) -> dict                          # read-only: no mkdir, no credentials, no network
def validate(manifest: dict, manifest_path: Path) -> list[str] # problems; the rules of 2.3; empty = valid
def require_valid(manifest: dict, manifest_path: Path) -> None # ManifestError(problems)
def manifest_sha256(manifest: dict) -> str                     # sha256(canonical_json(manifest))
def freeze(manifest: dict, paths: StatePaths, engine: dict) -> str
    # writes manifest.json, manifest.sha256, engine.json when absent; no-op when equal;
    # ManifestError("Manifest changed: ...") when a different frozen copy exists; refuses a null upstream.tree_sha256
def load_frozen(manifest_path: Path) -> tuple[dict, StatePaths]            # ManifestError unless a frozen copy exists and is identical
def live_blockers(manifest: dict, command: str, environ: Mapping[str, str], paths: StatePaths, *,
                  live_flag: bool, engine_sha256: str | None = None,
                  gate_problems: Sequence[str] | None = None, key_source: str = "env",
                  mode: str | None = None, end_search: bool = False, create_project: bool = False) -> list[tuple[str, str]]   # the table of 2.3
    # command: "account-check" | "preflight" | "search" | "evaluate" | "export" | "publish";
    # mode (evaluate only): "baselines" | "calibration" | "noise" | "test"; key_source: "env" checks the key variables in environ,
    # "prompt" and "fd" skip the key rows (the CLI checks the keys it read)
def require_live(manifest: dict, command: str, environ: Mapping[str, str], paths: StatePaths, *,
                 live_flag: bool, engine_sha256: str | None = None,
                 gate_problems: Sequence[str] | None = None, key_source: str = "env",
                 mode: str | None = None, end_search: bool = False, create_project: bool = False) -> None    # LiveGateError(blockers)
def prices(manifest: dict) -> dict[str, Decimal]               # {"input", "cached", "output"} per million
def phase_caps_micro(manifest: dict) -> dict[str, int]         # the seven phases of wire.LEDGER_PHASES plus "total"
AMENDABLE: tuple[str, ...] = ("telemetry.content.", "decisions.dataset_text_publication", "decisions.uspto_text_cleared")
    # a path is amendable when it equals an entry or starts with an entry that ends in "." (2.3 rule A)
IMPORT_DIFF_ALLOWED: tuple[str, ...]                           # the named set of 3.22, same matching rule
def diff_paths(a: Mapping, b: Mapping) -> list[str]            # sorted dotted leaf paths whose values differ; a list is one leaf
def read_amendments(paths: StatePaths, frozen_sha256: str) -> list[dict]
    # the rows of STATE/amendments.jsonl, [] when absent; ManifestError when seq is not 1..n, a prev_sha256 or row_sha256 does
    # not match, or a path is not amendable
def append_amendment(manifest: dict, paths: StatePaths, changes: Mapping[str, object], note: str, *,
                     clock: Clock | None = None) -> dict
    # validates effective(manifest, existing + this row) with the rules of 2.3, then appends the row (fsync); requires a frozen
    # copy and a non-empty note; returns the row
def effective(manifest: dict, amendments: Sequence[Mapping]) -> dict       # a deep copy with every row's "set" applied in order
def run_config(manifest: dict, allow: Sequence[str]) -> dict   # the sub-mapping of the manifest under the allowed dotted paths (2.15)
def max_reservation_micro(manifest: dict) -> int               # R of 2.3
def openai_project(manifest: dict) -> str                      # provider.openai_project or "<entity>/<project>"
```

`qualification.offline_gate` and `qualification.engine_snapshot` live in a later layer, so the CLI computes them and passes their results in: `gate_problems` (the names of failed gate checks; each becomes a blocker) and `engine_sha256` (compared with `qualification/passed.json`). With `gate_problems=None` the gate rows are skipped (used by `validate-manifest`). The halt check (runner commands only) reads the ledger file through `util.open_sqlite_ro` with SQL from 2.10. The `search` and `evaluate` rows read state files without writing: `LOGS/finalized.json` (existence only), the journal (`util.read_jsonl`, looking for `test` entries and for `run: complete` with key `search`), `STATE/qualification/calibration.json`, and the published result files for `evaluate --calibration`. Environment: none directly; `environ` is passed in, and only whether a name is present and non-empty is tested. The `search` row that reads a calibration record uses `paths.sibling(qualification.import_from)` when this `STATE` has no `calibration.json` and an import is configured. Never: copies, hashes or prints the value of a key variable; fills a default for a user decision; accepts an unknown key; writes anything except in `freeze` and `append_amendment`; blocks `export` or `publish` because a halt is set; changes the frozen manifest or its hash because of an amendment.

### 3.4 `state.py` (P: status from H2 `study.py` L67-L108, lock rule from `v4.py` L496-L500)

```python
class StateLock:                                               # exclusive lock; used for runner.lock and export/export.lock
    def __init__(self, lock_file: Path, command: str, clock: Clock | None = None) -> None
    def __enter__(self) -> "StateLock"       # fcntl.flock(LOCK_EX | LOCK_NB); writes {"pid","host","command","started_utc"}; StateLockError(holder) otherwise
    def __exit__(self, *exc: object) -> None
def status(manifest: dict, paths: StatePaths, *, clock: Clock | None = None) -> dict          # read-only
```

`status` returns:

```json
{"schema": "mhforge.status/1", "campaign": "...", "campaign_kind": "measured", "state_exists": true, "manifest_sha256": "...",
 "amendments": 0, "stall": {"journal_age_s": 12.5, "alarm": false},
 "lock": {"held": true, "pid": 4711, "command": "search", "started_utc": "..."},
 "halt": null, "stopped": null, "ledger_view": "current",
 "runtime": {"pid": 4711, "command": "search", "key_source": "prompt", "key_scrubbed": {"MHFORGE_FORGE_API_KEY": null}},
 "solver": {"observed_usd": "0.412000", "bounded_unknown_usd": "0.000000", "in_flight_usd": "0.049280",
            "committed_by_phase_usd": {"baseline": "0.110000"}, "caps_usd": {"baseline": "3.00", "total": "60.00"},
            "requests": 260, "unknown_requests": 0, "bounded_unknown_requests": 0, "reconciled_requests": 0},
 "meters": {"sandbox_seconds": 2480.0, "sandbox_usd": "0.234000", "sandbox_open_rows": 0, "lens_planned_mb": 6.1,
            "lens_planned_mb_by_bucket": {"main": 6.1, "preflight": 0.0, "recovery": 0.0}, "lens_planned_spans": 1840,
            "wandb_upload_mb": 0.4, "eval_tables": 6,
            "proposer_list_usd": "0.000000", "proposer_open_rows": 0},
 "jobs": {"succeeded": 6, "failed": 0, "running": 0, "skipped_usable": 0, "not_isolated": 0},
 "iterations": {"settled": 0, "last": null},
 "proposer": {"sessions": 0, "last_outcome": null},
 "qualification": {"passed": false},
 "export": {"verified": 0, "pending": 6, "partial": 0, "failed": 0, "refused_secret": 0, "skipped": 0, "dry_run": 0, "stale": 0},
 "circuit": {"failures": 0, "until_epoch": 0.0}}
```

It reads the ledger through `util.open_sqlite_ro` with SQL written from the DDL of 2.10 (it does not import `ledger.py`), the journal with `util.read_jsonl`, `runtime.json`, and the receipt files. `ledger_view` is `current`, or `possibly_stale` when the fallback open of 2.10 was used. `runtime.key_source` is how the last runner process got its keys; `runtime.key_scrubbed` shows, per key variable name, whether the in-place overwrite worked (`--key-source env` only; `null` for `prompt` and `fd`, where the key was never in the environment); `false` means a same-user process could read that key with `ps` while the runner ran (3.9). `stall.journal_age_s` is the time since the last journal entry and `stall.alarm` is true when a runner holds the lock and that age exceeds `runner.stall_alarm_s` (three hours by default): a multi-day run that has stopped advancing is shown as such, and the CLI prints a warning line to stderr. `amendments` is the number of rows in `STATE/amendments.jsonl`. `export.stale` counts final receipts whose plan has changed since (3.25). It never creates a directory or any file (including `-wal` and `-shm`), takes a lock, opens SQLite read-write, reads a credential or touches the network; it tolerates every file being absent or half-written, and it works on a read-only copy of the state directory. With no state directory it returns `{"state_exists": false, ...}`. `lock.held` is found by trying a non-blocking shared `flock` on an existing lock file opened read-only and releasing it at once.

### 3.5 `procenv.py`

```python
def worker_env(*, path: str, home: str, tmp: str, pythonpath: str, cache_dir: str, tokenizer_dir: str,
               extra: Mapping[str, str] | None = None) -> dict[str, str]
def route_env(route: int, *, base_url: str, api_key: str) -> dict[str, str]
PROPOSER_FIXED_ENV: tuple[str, ...] = ("PYTHONPATH", "UV_CACHE_DIR", "UV_OFFLINE", "UV_PYTHON_DOWNLOADS", "PYTHONDONTWRITEBYTECODE",
                                       "CLAUDE_CODE_DISABLE_AUTO_MEMORY", "DISABLE_AUTOUPDATER", "CLAUDE_CODE_STARTUP_FAILURE_RESULTS")
    # the closed list: names proposer_env sets by itself, in every mode; never taken from the manifest or from `base`
PROPOSER_DENY_NAMES: frozenset[str]                # exact names that must never be in a proposer environment (the list below)
PROPOSER_DENY_PREFIXES: tuple[str, ...] = ("OTEL_", "WANDB_", "OPENROUTER_", "OPENAI_", "LITELLM_", "ANTHROPIC_", "MHFORGE_")
def proposer_env(manifest: dict, base: Mapping[str, str], *, workspace_root: Path, example_venv: Path,
                 scratch_dir: Path) -> dict[str, str]
def assert_proposer_env(env: Mapping[str, str]) -> None
    # ValueError naming the first variable that is on PROPOSER_DENY_NAMES, starts with a PROPOSER_DENY_PREFIXES entry, starts with
    # "CLAUDE" without being one of the two CLAUDE_CODE_* names of PROPOSER_FIXED_ENV, or whose value contains "http://127.0.0.1:"
def passthrough_env(*, home: str, python_bin_dir: str) -> dict[str, str]
def exporter_env(manifest: dict, base: Mapping[str, str], *, home: str, package_root: Path, netrc_file: Path) -> dict[str, str]
def minimal_env(base: Mapping[str, str], *, home: str) -> dict[str, str]     # PATH=/usr/bin:/bin and HOME; for caffeinate and `ps`
def is_credential_name(name: str) -> bool                       # re.search(wire.CREDENTIAL_NAME_PATTERN, name.upper())
def assert_no_secret_names(env: Mapping[str, str], *, allow: Sequence[str] = ()) -> None
    # ValueError naming the first variable for which is_credential_name holds and that is not in `allow`
def venv_root(python: Path) -> Path
    # the path rule of 2.3: Path(os.path.abspath(python)).parent.parent, which must contain pyvenv.cfg (WorkspaceError otherwise);
    # never Path.resolve() or os.path.realpath
def find_tokenizer_dir(python: Path, vocab_file: str) -> Path
```

- `worker_env` returns exactly: `PATH`, `HOME`, `TMPDIR`, `PYTHONPATH`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONUTF8=1`, `TEXT_CLASSIFICATION_LLM_CACHE_DIR`, `LITELLM_LOCAL_MODEL_COST_MAP=True`, `LITELLM_MODE=PRODUCTION`, `TIKTOKEN_RS_CACHE_DIR`, `NO_PROXY=127.0.0.1,localhost`, plus `extra`. It is the `env` object of the job file. The worker ran under `env -i` with this set in two experiments [F-E1, T-E2]. Without `TIKTOKEN_RS_CACHE_DIR` a no-egress worker pays for every call and then dies in `llm.py` L62 with `HarmonyError` [rep CM-V C4]; `LITELLM_MODE=PRODUCTION` stops LiteLLM loading a `.env` file [rep].
- `route_env`: Route 2 returns `OPENROUTER_API_BASE=<base_url>` and `OPENROUTER_API_KEY=<api_key>` (the per-job token under the loopback ingress, the placeholder `mhf-relay` under the relay; LiteLLM `main.py` L3284 [rep]); Route 1 returns `{}` (the URL travels as `--api-base`).
- `proposer_env` starts from an empty mapping and adds, in this order: each name in `proposer.env_allow` that is present in `base`; `PATH` from `proposer.path_dirs`; each entry of `proposer.env_set`; then the closed list `PROPOSER_FIXED_ENV`, which no manifest can change: `PYTHONPATH=<workspace_root>` (the workspace's parent: the skill's own import line fails with `ModuleNotFoundError` without it, and a `PYTHONPATH=..` prefix on a command is not auto-allowed in mode `restricted` [r2, measured twice]), `UV_CACHE_DIR=<scratch_dir>/.uv-cache` (`uv run` fails when its cache is not writable, even with `--no-sync --frozen` [r2, measured twice]), `UV_OFFLINE=1`, `UV_PYTHON_DOWNLOADS=never`, `PYTHONDONTWRITEBYTECODE=1` (no network from the sandbox; no `__pycache__` in `agents/`), `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` (auto memory is a state channel outside `logs/`, shared by every campaign in one git repository, and it loads in `-p` mode whatever `--setting-sources` says [r2]), `DISABLE_AUTOUPDATER=1` and `CLAUDE_CODE_STARTUP_FAILURE_RESULTS=1` (the binary sits in a folder another application manages; a startup refusal then arrives as a `result` event). Placeholders: `{example_venv}` (the caller passes `venv_root(upstream.worker_python)`, never a resolved path), `{workspace_root}`, `{uv_dir}` (the directory of the first `uv` on `base["PATH"]`; when none is found the entry is dropped). Before it returns it calls `assert_proposer_env` on the result. `PROPOSER_DENY_NAMES` is exactly: `CLAUDE_CODE_EFFORT_LEVEL` (it outranks `--effort`), `CLAUDE_CODE_ENTRYPOINT`, `CLAUDECODE`, `CLAUDE_CODE_CHILD_SESSION`, `CLAUDE_CODE_RETRY_WATCHDOG` (it waits out plan windows inside the session; on a usage-billed seat a spend limit fails at once and the driver decides about waiting), `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CONFIG_DIR`, `CLAUDE_CODE_TRANSCRIPT_LOCAL_GC`, `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`, `API_TIMEOUT_MS`, `DISABLE_MICROCOMPACT`, `MAX_THINKING_TOKENS`, `NODE_OPTIONS`, `TRACEPARENT`, `TEXT_CLASSIFICATION_LLM_CACHE_DIR`; together with the prefixes `OTEL_`, `WANDB_`, `OPENROUTER_`, `OPENAI_`, `LITELLM_`, `ANTHROPIC_`, `MHFORGE_` and any value that holds a loopback gateway address. A prefix guard alone misses several of those names [r2]; building from nothing covers them, and the assertion proves it for every session. Upstream removes only `CLAUDECODE` and `ANTHROPIC_API_KEY` (`meta_harness.py` L166-L168) and the wrapper copies everything else (`claude_wrapper.py` L573) [src]. The CLI therefore uses the login stored in the keychain; on this account that is a usage-billed seat, so every proposer token is real spend (3.19).
- `passthrough_env` (for `benchmark.py --frontier/--results`, and for the stub process of `LocalRelayExecutor`): `PATH=<python_bin_dir>:/usr/bin:/bin`, `HOME`, `PYTHONDONTWRITEBYTECODE=1`, `PYTHONUTF8=1` [T-E6]. `python_bin_dir` is `venv_root(python) / "bin"`.
- `exporter_env`: `PATH`, `HOME`, `LANG` when present, `PYTHONPATH=<package_root>` (the directory that contains the `mhforge` package, so a child started from any working directory can import it), `NETRC=<netrc_file>` (the empty file `STATE/export/empty.netrc`: the SDK honours `NETRC`, so a missed call can neither read nor write the user's real file [r2]) and `WANDB_DISABLE_WEAVE=true`. Nothing else, and **no key**: the spawner passes the export key to the child over an inherited pipe (3.25), so it is never in a start-up environment that `ps eww` shows.
- `minimal_env`: `PATH=/usr/bin:/bin` and `HOME`; used for `caffeinate` and for test helpers that run `ps`.
- `is_credential_name` is the one credential-name rule on the host. It is used by `assert_no_secret_names` and by validation rule 9; the in-executor selfcheck carries a copy of the pattern and of `wire.CREDENTIAL_NAME_EXEMPT`, and `test_selfcheck.py` compares both with `wire`. `TIKTOKEN_RS_CACHE_DIR` does not match it (`TIKTOKEN` is not the whole word `TOKEN`); the first draft's substring rule did [A-S].
- `find_tokenizer_dir`: globs `<venv_root(python)>/lib/python*/site-packages/litellm/litellm_core_utils/tokenizers` and requires `vocab_file` inside it; `WorkspaceError` otherwise. It does not import LiteLLM. (The first draft's glob started at the interpreter file itself and matched nothing [A-U E8].)

Environment: only the names listed above, from the `base` mapping the caller passes; no function here has a default that reads `os.environ`. Never: `os.environ.copy()`; a key or token in a returned mapping other than the Route 2 job token; `Path.resolve()` on an interpreter path; a proposer environment that was not passed through `assert_proposer_env`.

### 3.6 `ledger.py` (P: H2 `budget.py`)

```python
def reservation_micro(prices: Mapping[str, Decimal], input_limit: int, output_limit: int) -> int
    # ceil(prices["input"] * input_limit + prices["output"] * output_limit); no cache discount (H2 budget.py L105-L112)
def cost_micro(prices: Mapping[str, Decimal], usage: Mapping) -> int
    # ceil((prompt - cached) * input + cached * cached_price + completion * output); cached from prompt_tokens_details.cached_tokens
def usage_within_limits(usage: object, input_limit: int, output_limit: int) -> bool      # ported verbatim, budget.py L21-L49

@dataclass(frozen=True)
class Settlement:
    status: str                                    # complete | rejected | bounded_unknown | unknown
    spent_micro: int | None
    basis: str                                     # usage | reservation | none
@dataclass(frozen=True)
class HaltState:
    reason: str; detail: str; set_utc: str; call_id: str | None

class Ledger:
    def __init__(self, path: Path, manifest: dict, *, clock: Clock | None = None, ids: IdSource | None = None) -> None
    def interrupt_pending(self) -> int             # startup: pending -> bounded_unknown, reason 'runner interrupted' (H2)
    def register_job(self, job: JobIdentity, *, token_sha256: str | None = None) -> None
        # idempotent; a new job_id for a job_key resets job_counters.requests and sets prior_charged = (charged_attempts > 0)
    def close_job(self, job_id: str, reason: str = "closed") -> None
    def close_open_jobs(self, reason: str = "runner interrupted") -> int                      # startup
    def identity_for_token(self, token: str) -> JobIdentity | None                            # open registrations only; compares sha256
    def is_open(self, job_id: str) -> bool
    def prior_charged(self, job_key: str) -> bool  # an earlier job attempt of this job_key has a charged provider attempt
    def reserve(self, *, call_id: str, n: int, job: JobIdentity, phase: str, model: str,
                input_limit: int, output_limit: int) -> str                                   # returns ledger_id
    def settle(self, ledger_id: str, usage: Mapping | None, *, rejection: str | None = None,
               reason: str = "") -> Settlement
    def reconcile(self, ledger_id: str, spent_micro: int, note: str, *, below_reservation: bool = False) -> None
        # operator action (status --reconcile): 'unknown' -> 'reconciled' with the given spend; note required
    def note_exhausted(self, job: JobIdentity, request_sha256: str, call_id: str, *, peer_ok: bool) -> bool
        # records in failed_requests that this digest used up its gateway attempts in this job attempt;
        # True when it had already done so in an EARLIER job attempt of the same job_key and peer_ok holds now or held then
    def halted(self) -> HaltState | None           # SELECT from halt only
    def set_halt(self, reason: str, detail: str = "", call_id: str | None = None) -> bool     # first reason wins; True for the call that set it
    def clear_halt(self, note: str) -> None        # operator action; refuses while any 'unknown' row exists
    def job_requests(self, job_key: str) -> int    # job_counters.requests of the current job attempt
    def committed(self, phase: str | None = None) -> int
    def meter(self, meter: str, key: str, quantity: int, unit: str, *, micro_usd: int | None = None,
              detail: Mapping | None = None, bucket: str = "") -> bool                       # one-shot 'final' row; False when (meter, key) already exists
    def meter_open(self, meter: str, key: str, worst_quantity: int, unit: str, *, worst_micro: int | None = None,
                   detail: Mapping | None = None) -> bool                                     # 'open' row at the worst case; False when the key exists
    def meter_close(self, meter: str, key: str, quantity: int, *, micro_usd: int | None = None) -> bool
        # 'open' -> 'final' with the measured figures, once per key; False when the row is absent or already final
    def open_meter_rows(self, meter: str) -> list[dict]                                       # {"key", "quantity", "micro_usd", "utc", "detail"}
    def meter_total(self, meter: str, *, bucket: str | None = None) -> tuple[int, int]        # (quantity, micro_usd); open rows at their worst case
    def meter_max(self, meter: str) -> int                                                    # largest micro_usd of a 'final' row; 0 when none
    def check_meter_cap(self, meter: str, *, extra_quantity: int = 0, extra_micro: int = 0,
                        bucket: str = "") -> None                                              # MeterCapExceeded(meter, bucket)
    def snapshot(self) -> dict
    def verify_totals(self) -> list[str]           # recompute sums from calls; discrepancies as text
```

`reserve` runs these checks in one `BEGIN IMMEDIATE` transaction, in this order, and raises `BudgetError(kind, phase)`:

1. `halted`: a row exists in `halt`.
2. `invalid`: `model != prices.model`, `phase` not in `LEDGER_PHASES`, or limits outside `1..gateway.model_context_tokens` / `1..gateway.max_output_tokens`.
3. `unreconciled`: `SELECT 1 FROM calls WHERE status='unknown' LIMIT 1` (indexed).
4. `ceiling` (the three per-job bounds of F6; `BudgetError.detail` says which):
   - `requests`: when `n == 1`, `job_counters.requests >= gateway.max_requests_per_job` for the current job attempt. Only the first provider attempt of a worker request counts, so the gateway's own retries, provider 429s and 5xx answers cannot use the ceiling up, and a rerun starts from zero (its finished calls are replays, which are not rows at all).
   - `attempts`: `job_counters.charged_attempts >= 2 * gateway.max_requests_per_job`, all job attempts together. A provider attempt settled `rejected` (zero cost) is not charged.
   - `job_usd`: `job_counters.committed_micro + reservation > micros(gateway.max_job_usd)`, all job attempts together.
5. `total`: `SUM(phase_totals.committed_micro) + reservation > caps["total"]`.
6. `cap`: `phase_totals[phase] + reservation > caps[phase]`.

Then it inserts a `pending` row (`origin_phase = job.phase`), adds the reservation to `phase_totals[phase]` (asserting one row changed) and to `job_counters.committed_micro`, increments `job_counters.charged_attempts`, and increments `job_counters.requests` when `n == 1`.

`settle`, exactly once per row (a second call raises `BudgetError("settled")`):

| Input | Row status | `spent_micro` | `phase_totals[phase]` and `job_counters.committed_micro` |
|---|---|---|---|
| `rejection` given (the provider answered with a status other than 200, or nothing was sent) | `rejected` | 0 | minus the reservation; `charged_attempts` minus one |
| usage well-formed (both counters non-negative integers, `cached <= prompt`) and cost at most the reservation | `complete` | cost | plus cost minus reservation |
| as above, but a counter is over its limit (`usage_within_limits` false) while the cost is still at most the reservation | `complete`, `reason = "limit_exceeded"` | cost | plus cost minus reservation; **no halt** |
| usage missing or partial, nothing contradictory | `bounded_unknown` | NULL | unchanged: the reservation keeps counting and is reported separately |
| usage malformed (a negative or non-integer counter, `cached > prompt`), or cost above the reservation | `unknown` | NULL | unchanged; `halt("usage_contradiction")` in the same transaction |

The third row is new. The reservation is the money bound; a prompt-token count above `input_limit` whose cost still fits the reservation has cost nothing that was not reserved, so it is recorded and reported (`limit_exceeded` rows are listed by `audit` and counted by qualification) but does not stop the campaign. Under the first draft one request of a few thousand tiny messages from candidate code produced such a count and ended the campaign for good [A-M m3, A-S e_tokens]. With validation rule 4 (`cached <= input` price) the cost of a well-formed usage within both limits can never exceed the reservation.

`reconcile(ledger_id, spent_micro, note, below_reservation=False)` is the way out of an `unknown` row, which otherwise blocks every later reservation, `clear_halt` and a qualification restart for ever. It requires the row to be `unknown`, a non-empty note, `spent_micro >= 0`, and `spent_micro >= reserved_micro` unless `below_reservation` is true (the operator asserts the figure comes from the provider's billing export; the flag is stored). It sets the row to `reconciled` with that spend, adjusts `phase_totals` and `job_counters.committed_micro` by `spent_micro - reserved_micro`, and appends to `reconcile_history`. It never deletes a row. The CLI journals `halt: reconciled`; `audit` lists every reconciled row. After the last `unknown` row is reconciled, `status --clear-halt` can clear a `usage_contradiction` halt.

`note_exhausted` supports the repeatable-failure rule of 3.10: it upserts `failed_requests` for `(job.job_key, request_sha256)`. If a row exists whose `last_job_id` differs from `job.job_id` and (`peer_ok` argument or the stored `peer_ok`), it updates the row and returns `True`. Otherwise it inserts or updates the row (`exhausted` plus one when `last_job_id` changes, `peer_ok` OR-ed in) and returns `False`.

`snapshot` returns `{"observed_micro", "bounded_unknown_micro", "in_flight_micro", "committed_by_phase", "caps_micro", "requests", "unknown_requests", "bounded_unknown_requests", "reconciled_requests", "limit_exceeded_requests", "halt", "meters": {meter: {"quantity", "micro_usd", "open_rows"}}}`. It never lists rows. `check_meter_cap` compares `sandbox_seconds` in micro-dollars with `budgets.sandbox_usd`; `lens_bytes` in bytes, for the given bucket, with `budgets.lens_ingest_split_mb[bucket] * 1048576`, and over all buckets with `budgets.lens_ingest_mb * 1048576` (either can refuse; the exception names the bucket); `proposer_list_price` in micro-dollars with `budgets.proposer_list_usd`; `wandb_upload_bytes` in bytes with `budgets.wandb_upload_gb * 2**30`; `eval_tables` as a count with `budgets.eval_tables_max`. `lens_spans` has no cap: it is the span-count meter kept beside the byte meter, because the platform plans to meter Agent Lens by spans [r2], and `status` reports it. In each total an `open` row counts at its worst case.

Meters as ceilings. A meter that is written once, after the fact, is not a ceiling: a runner crash skips the write, and a session or sandbox whose final figure never arrives counts as zero [A-M]. So anything that runs for a while is metered in two steps: `meter_open` with the worst case before it starts (a sandbox: its maximum lifetime at the manifest prices, keyed by the sandbox id; a proposer session: its `--max-budget-usd` cap, keyed by `session_key`), and `meter_close` with the measured figure when it ends. `startup_recovery` closes what a crash left open (3.20). `meter` stays for one-shot quantities known in advance (`lens_bytes`, `lens_spans`, `wandb_upload_bytes`, `eval_tables`).

Changes from H2, each from the reuse report [rep]: explicit per-job ceilings in place of `max_steps * 2`; no `schema_version` switch (bounded-missing behaviour only); status index, targeted halt query and running totals in place of a full-table read per call; a provider rejection books zero; halt flag in the database; job registry; six meters with open and final rows and ingest buckets; phases renamed and extended; `reconcile`; `limit_exceeded` settles instead of halting.

Invariants (each has a test): `phase_totals[phase] == SUM(COALESCE(spent_micro, reserved_micro)) over non-rejected rows of that phase` at every commit, and the same per `job_key` for `job_counters.committed_micro`; with N concurrent `reserve` calls against a cap that fits one, exactly one succeeds; a restart keeps every reservation; a halt makes every later `reserve` fail; the seven `phase_totals` rows exist from creation. Never: floats for money; deletes a row; reads a credential.

### 3.7 `replay.py`

```python
def request_sha256(admitted: Mapping) -> str       # sha256(canonical_json({"model", "messages", "temperature", "max_tokens"}))
def reuse_key(request_sha256: str, salt: str | None) -> str
    # request_sha256 when salt is None; else sha256((request_sha256 + "\0" + salt).encode()): the key the store and the
    # in-flight table use. The gateway passes salt = f"s{job.sample}" for a job with a sample index, else None
def zero_usage(body: bytes) -> bytes               # usage := {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}; all else unchanged

@dataclass(frozen=True)
class StoredReply:
    body: bytes; source_call_id: str; source_job_id: str
    finish_reason: str | None; prompt_tokens: int | None; completion_tokens: int | None

class Lease:                                       # returned by ReplayStore.begin
    role: str                                      # "hit" | "follower" | "leader"
    stored: StoredReply | None                     # set when role == "hit"
    leader_call_id: str | None                     # set when role == "follower"
    def wait(self, timeout: float | None = None) -> GatewayReply | None
        # follower: the leader's reply; None when the leader abandoned, failed without sharing, or the timeout passed
    def commit(self, reply: GatewayReply, *, job_id: str, finish_reason: str | None,
               prompt_tokens: int | None, completion_tokens: int | None) -> None     # leader, status 200: store (unless bypass) and wake followers
    def fail(self, reply: GatewayReply, *, share: bool) -> None                      # leader: store nothing; followers get the reply iff share
    def abandon(self) -> None                                                         # leader crashed: followers get None

class ReplayStore:
    def __init__(self, path: Path, scope_mode: str, *, clock: Clock | None = None) -> None   # StateError if the file was created with another scope
    def scope_key(self, job: JobIdentity) -> str
    def begin(self, job: JobIdentity, digest: str, *, call_id: str, bypass: bool = False) -> Lease
    def stats(self) -> dict                        # {"stored": n, "hits": n, "in_flight": n}
```

- `scope_key`, first match: `"q:" + job.job_key` when `job.phase == "qualification"` (qualification replies are never reused by the measured phases, so a baseline never reports replayed tokens from the gate); `"repeat:" + job.job_key` when `job.repeat` is set (scope kind `repeat`: a noise repeat must generate its own replies, so nothing is shared between repeats or with the campaign's other jobs, while a rerun of the same repeat's job still replays its own finished calls; the job key carries the repeat index, 2.2); otherwise `"search"` under scope `search`, `"job:" + job.job_key` under scope `job`. The scope kind written to a call record is one of `wire.REUSE_SCOPES`.
- **Reuse salt.** `begin` takes the reuse key, `reuse_key(request_sha256, salt)`. An ordinary job has no salt, and its reuse key is the request digest. A job with a sample index (the deferred sampling control, which asks for K different samples of one prompt at a non-zero temperature) is salted with `s<k>`, so identical requests of different samples neither replay nor coalesce onto one another, under either campaign scope; identical requests within one sample still do. Together with the per-attempt worker cache directory (2.6) this is what lets a control obtain distinct samples through a gateway whose whole point is to answer an identical request once. Job attempts share `job_key`, so a rerun replays its own earlier calls under either scope. That matters because a dead sandbox's worker cache cannot be recovered.
- Scope `search` reproduces the reach of upstream's single shared cache directory (`llm.py` L28-L35, key at L164-L178 [src]) within this campaign. The key holds exactly what upstream's own cache key holds besides the address: model, system prompt, prompt, `temperature`, `max_tokens`.
- Stored: a 200 merged body with at least one choice, including an empty reply with `finish_reason: length`, because upstream caches that empty string too [F-E3; rep CM-V M5]. Never stored: refusals, provider failures, replies withheld by the identity gate, anything under `bypass`.
- A `hit` is answered with `zero_usage(stored.body)`: an upstream cache hit adds nothing to the worker's token counters (`llm.py` L255-L258 [src]), and with a zeroed usage block the worker also adds nothing [F-E3], so `val.json` token fields keep upstream's meaning. A follower gets the leader's reply unchanged (it is usually the same worker's own retry of a call whose first connection timed out).
- `fail(share=True)` is for outcomes that would be the same for any caller (provider rejection, provider unavailable, a halt). `fail(share=False)` is for refusals that belong to the leader's job (its request ceiling); followers then get `None` and call `begin` again.
- `begin(bypass=True)` always returns a leader and `commit` then does not store; the gateway passes it only for phase `qualification` (the temperature-0 repeatability check).
- A leader that exits without `commit`, `fail` or `abandon` is a bug; `Gateway.handle` calls `abandon` in a `finally`.
- `commit` writes the reply to the store before it returns, and the gateway calls it **before** `ledger.settle` (3.10). The two files cannot be written atomically; in this order a crash between them leaves a stored reply and a `pending` ledger row (the rerun replays for free and the row is booked at its reservation), never a settled row without a stored reply (which would make the rerun pay again without the row saying so).
- A negative result is never stored (Appendix A item 23): a request that the provider rejects for its length is forwarded again when it is repeated, at zero cost.

Never: returns a body for a different digest or scope; coalesces across processes.

### 3.8 `sse.py` (P: H2 `gateway.py` L25-L54 `merge_stream`, `transport_v4.py` L25-L32) and `circuit.py` (P: `transport_v4.py` L91-L124)

```python
def iter_sse(lines: Iterable[bytes]) -> Iterator[tuple[str, object]]
    # ("chunk", dict) for each "data: {json}" line; ("done", None) for "data: [DONE]";
    # ("bad", bytes) for a data line that is not JSON; other lines (comments, blank) are skipped
def merge_stream(chunks: Sequence[Mapping]) -> dict
def to_completion_body(merged: Mapping, *, request_model: str, created: int) -> dict
def reasoning_field(chunks: Sequence[Mapping]) -> str | None           # "reasoning" | "reasoning_content" | None, as seen on the wire
```

- `merge_stream`: concatenates `delta.content`, and `delta.reasoning` or `delta.reasoning_content` (both names are read, as H2 `gateway.py` L37 already does [A-U]) of choice index 0; keeps the last non-null `id`, `model`, `usage` (never sums usage) and `finish_reason`; if any chunk has an `error` key the result has `error` set to the last one; `choices` is empty when no chunk carried a choice.
- `to_completion_body` returns `{"id", "object": "chat.completion", "created", "model", "choices": [{"index": 0, "message": {"role": "assistant", "content": str, "reasoning": str (only if non-empty)}, "finish_reason"}], "usage" (only if present)}`. `model` is the provider's echoed model, or `request_model` when the provider sent none. `content` is always a string (empty when the model produced none). It never invents usage. The unmodified upstream `LLM` class accepts this body with a `reasoning` field on both routes [F-E3].
- Pure; no I/O.

```python
class Circuit:
    def __init__(self, path: Path, *, threshold: int, cooldown_s: float, clock: Clock | None = None) -> None
    def enter(self, *, max_wait_s: float) -> float                     # seconds waited; TimeoutError past max_wait_s
    def finish(self, outcome: str) -> None                             # "success" | "failure" | "neutral"
    def state(self) -> dict                                            # {"failures": int, "until_epoch": float, "probing": bool}
```

Ported behaviour: blocks while `failures >= threshold`, admits exactly one probe after the cool-down, a success resets, state survives a restart (`circuit.json`; the probe flag is not persisted and starts false). A 4xx other than 408 and 429 does not count as a failure (H2 `test_transport_v4.py` L162-L183 [rep]).

Changed from H2, because the ported shape could hang the gateway for ever [A-M m2]. In H2 `enter()` sets a probe flag that only `finish()` clears, and `finish` takes a bool. A probe that left the attempt between `enter` and `finish` (a `BudgetError` at the reservation: ceiling, cap, halt) never cleared the flag, so every later request blocked in `enter()` with no limit; and a candidate's 400 could be expressed neither as "no change" (not calling `finish` hangs), nor as `finish(False)` (which closes an open circuit), nor as `finish(True)` (three candidate 400s would open it). So:

- `finish(outcome)` takes one of three values. `success` resets the failure count. `failure` increments it and starts the cool-down when the threshold is reached. `neutral` changes neither the count nor the cool-down. **All three clear the probe flag and wake the waiters.**
- Every `enter()` is paired with exactly one `finish()` by the caller, in a `try/finally` that starts on the line after `enter()` returns (3.10 step 9).
- `enter` always takes `max_wait_s` (no default). The gateway passes `provider.circuit.cooldown_s + provider.attempt_deadline_s + 60`; a `TimeoutError` there is an internal fault (`gateway_internal`), not a hang.

### 3.9 `provider.py` and `stubprovider.py`

```python
# provider.py
@dataclass(frozen=True)
class ProviderError:
    kind: str                                      # http | stream_error | truncated | connect | timeout | bad_json | too_large
    http_status: int | None; code: str | None; body_excerpt: str      # at most 2,000 characters; never a header
@dataclass(frozen=True)
class ProviderResult:
    http_status: int | None                        # None when no status line was received
    headers: dict[str, str]                        # the allow-list of 2.5
    merged: dict | None                            # merge_stream output
    done: bool                                     # [DONE] seen
    error: ProviderError | None
    first_byte_s: float | None; total_s: float; received_bytes: int
    reasoning_field: str | None

class ProviderClient(Protocol):
    def complete(self, body: Mapping, *, sse_sink: BinaryIO | None, deadline_s: float) -> ProviderResult: ...

class ForgeProviderClient:                         # the live path; UNVERIFIED against the service; tested against stubprovider.serve_stub
    def __init__(self, manifest: dict, *, environ: MutableMapping[str, str],
                 scrub: Callable[[str], bool] | None = None, clock: Clock | None = None) -> None
    key_facts: dict                                # {"name", "len", "sha256", "scrubbed": bool | None}; never the value
    def complete(self, body: Mapping, *, sse_sink: BinaryIO | None, deadline_s: float) -> ProviderResult

def classify(result: ProviderResult, *, terminal_429_patterns: Sequence[str] = ()) -> str      # pure; the table below
def is_context_limit(error: ProviderError) -> bool # http 400 (or a 400-coded stream error) whose text names the context length
```

`ForgeProviderClient`:

- `__init__` reads `environ[provider.api_key_env]` once (the CLI passes the key mapping of 3.27, which is `os.environ` with `--key-source env` and a private dict with `prompt` or `fd:N`; the smoke passes `os.environ`; tests pass a dict), keeps it in a name-mangled attribute, then calls `scrub(name)` when a scrub function was given (with `--key-source env` the CLI passes `util.scrub_process_env`, as the smoke does; it **overwrites the value bytes in the process environment block in place** and deletes the variable. With the other key sources the key was never in the environment block and no scrub function is passed), and in every case deletes the name from `environ`, so no later child can inherit it. Deleting alone is not enough: the start-up environment block of the runner stays readable by any process of the same user for the life of the process, value included [R3]. `key_facts` records the variable name, the value's length and SHA-256, and `scrubbed` (`True`, `False` when the overwrite was not possible, `None` when no scrub function was given); the CLI writes `scrubbed` into `STATE/runtime.json` (shown by `status`) and the digest into `STATE/keydigests.json` with `util.append_key_digest` (for the scanner, 3.24). With `provider.kind == "forge"` a missing or empty variable raises `ProviderConfigFault`; with `"stub"` the key is optional (`"stub-key"` is used when absent, and no digest is recorded for it). It refuses (`ProviderConfigFault`) a `base_url` that the kind does not allow (2.3 rule 3). `repr()` shows the variable name, never the value.
- Any occurrence of the key in a `ProviderError.body_excerpt`, and in a raw non-200 body before it is written to the `.sse` evidence file, is replaced by `[key]`: a provider 401 body could echo part of the credential [inf; the provider's text is unknown until the preflight].
- `complete` sends `POST <base_url>/chat/completions` over `http.client.HTTPSConnection` (`HTTPConnection` for the loopback stub) with exactly these headers: `Authorization: Bearer <key>`, `Content-Type: application/json`, `Accept: text/event-stream`, `OpenAI-Project: <manifest.openai_project(manifest)>`, `User-Agent: <provider.user_agent>` (never a library default: the edge has blocked `urllib`'s [rep SI-V]). Body: the admitted body plus `"stream": true` and `"stream_options": {"include_usage": true}`. It never adds `response_format` (it breaks gpt-oss on this runtime [rep]) and never sends a `usage` field.
- Timeouts: socket timeout `connect_timeout_s` to connect, `first_byte_timeout_s` until the first data line, `idle_timeout_s` between lines; the whole attempt ends at `min(deadline_s, attempt_deadline_s)`. Every raw line is written to `sse_sink` (with any occurrence of the key replaced by `[key]`) and flushed before it is parsed, so an interrupted stream leaves evidence. The sink holds response lines only. More than `provider.max_response_bytes` ends the attempt with `too_large`.
- It follows no redirect (a 3xx is an `http` error) and takes nothing about the destination from the request.
- It never raises for a provider or network condition; it returns a `ProviderResult` with `error` set.

`classify`:

| Result | Disposition | Ledger settle | `circuit.finish` |
|---|---|---|---|
| 200, no `error`, at least one choice, and either `[DONE]` seen, or a `finish_reason` together with a well-formed usage block | `ok` | from usage | `success` |
| 200 with an `error` chunk whose `code` is an integer 400..499 other than 408 and 429 | `context_limit` if `is_context_limit`, else `provider_rejected` | `rejected` | `neutral` |
| 200 with any other `error` chunk | `retry_transport` | from usage when a well-formed usage block arrived, else `bounded_unknown` | `failure` |
| any other 200: no choice; no `[DONE]` and no `finish_reason`-plus-usage; `bad_json`; or `timeout` after the status line | `retry_transport` | from usage when a well-formed usage block arrived, else `bounded_unknown` | `failure` |
| 200, `too_large` | `provider_rejected` | `bounded_unknown` | `neutral` |
| 400 for which `is_context_limit` | `context_limit` | `rejected` | `neutral` |
| 400 (other), 413, 422 | `provider_rejected` | `rejected` | `neutral` |
| 401, 402, 403 (every one: a missing or wrong key, a billing stop, a disabled organisation, a blocked client) | `config_fault` | `rejected` | `neutral` |
| 429 whose `error.code` is `insufficient_quota` or `quota_exceeded`, or whose `error.code` or message contains a `provider.terminal_429_patterns` entry (lower-cased) | `quota_exhausted` | `rejected` | `neutral` |
| any other 429 | `retry_rate_limited` | `rejected` | `failure` |
| 404, 408, 409, 5xx, 3xx | `retry_transport` | `rejected` | `failure` |
| `connect` (nothing was sent) | `retry_transport` | `rejected` | `failure` |
| `timeout` with no status line (the request was sent) | `retry_transport` | `bounded_unknown` | `failure` |

**Order of evidence.** An error is classified on the HTTP status first, then on `error.code` of the JSON body, and only then on a substring of the message [r2]: the status and the code are the provider's contract, the wording is not. 401, 402 and 403 are configuration faults and halt the campaign (`provider_config`): they are a missing key, a spend stop or a switch that is off, and no retry fixes them. A misspelt entity looks exactly like a disabled organisation, and a negative answer can be cached by the service for up to a minute [r2], so a halt that was fixed may repeat once if the runner is restarted at once. A 429 whose body says the quota is exhausted is terminal too (`provider_quota`): the pricing page says 429 where the documentation and the code say 402 [r2]. Every other 429 is a rate limit and stays with the gateway's own retry.

Two rules behind the first rows. A stream that carried a choice, a `finish_reason` and a consistent usage block and then closed without `[DONE]` is complete: it is `ok`, and `done: false` is kept in the call record. Booking it at the reservation and paying for a retry (about five times the cost for a few-shot prompt, by the example of 2.5) would be waste [inf; whether the service ever does this is unknown until the preflight]. And whenever a well-formed usage block is present the row is settled from it, whatever the disposition: the provider has told us what the attempt cost.

A non-200 status is booked at zero because the provider reports usage only for status 200 [rep SI-V correction 4]. 404 is retried because the serving front end answers 404 when a model has no registered workers [rep SI-V]. `is_context_limit` looks for `context length`, `context_length`, `maximum context` or `input tokens` in the lower-cased excerpt [inf; the provider's exact text is unknown until the preflight].

```python
# stubprovider.py   (offline stand-in for Serverless Inference; deterministic)
def default_reply(body: Mapping) -> dict           # {"content": str, "reasoning": str, "finish_reason": "stop", "usage": {...}}
@dataclass(frozen=True)
class Fault:
    kind: str                                      # http | error_stream | truncate | no_usage | length_empty | wrong_model | no_model
                                                   # | hang | rate_limit | contradictory_usage | reasoning_content
    arg: object = None                             # status for http, seconds for hang, model for wrong_model
    when: int | str = "always"                     # 1-based request number, or "always"
    times: int = 1
class StubProvider:                                # a ProviderClient, in process
    def __init__(self, *, rules: Callable[[Mapping], dict] = default_reply, faults: Sequence[Fault] = (),
                 clock: Clock | None = None, latency_s: float = 0.0) -> None
    calls: list[dict]                              # every body received
    peak_in_flight: int
    def complete(self, body: Mapping, *, sse_sink: BinaryIO | None, deadline_s: float) -> ProviderResult
def render_sse(reply: Mapping, *, model: str, chunk_chars: int = 64) -> bytes      # pure; the provider's wire format, usage in the last chunk
def serve_stub(*, rules: Callable[[Mapping], dict] = default_reply, faults: Sequence[Fault] = (),
               port: int = 0) -> tuple[ThreadingHTTPServer, str]                   # binds 127.0.0.1 only; returns (server, base_url ending /v1)
```

- `default_reply` returns a parseable answer per dataset, chosen by a fixed rule on the last user message: a `[罪名]盗窃<eoa>` answer when the prompt contains `罪名`, a SMILES string when it contains `reactants` or `SMILES`, a `[DIAGNOSIS]common cold[/DIAGNOSIS]` answer otherwise; `prompt_tokens = ceil(utf8_bytes / 4)`, `completion_tokens = 16`, a non-empty `reasoning`.
- `StubProvider.complete` renders real SSE bytes, writes them to `sse_sink` and parses them back with `sse.iter_sse`, so unit tests run the production parser.
- `serve_stub` speaks the same bytes over loopback HTTP and records each request's headers and body (`server.requests`); `ForgeProviderClient` is tested against it and the offline smoke uses it, so the real HTTP and streaming path runs with no network. It never logs the `Authorization` value.

### 3.10 `gateway.py` (P: H2 `gateway.py` L57-L217 and `transport_v4.py` L70-L88, L146-L308)

```python
def admit(raw: bytes, manifest: dict) -> tuple[dict | None, list[str], str | None, int]
    # pure: (admitted body or None, dropped keys, refusal code or None, content_bytes)
def input_limit(content_bytes: int, n_messages: int, manifest: dict) -> int
    # min(content_bytes + gateway.template_overhead_tokens + n_messages * gateway.per_message_overhead_tokens,
    #     gateway.model_context_tokens)
def backoff_s(disposition: str, http_status: int | None, manifest: dict, rng: random.Random) -> float     # pure given rng
def model_identity_problem(response_model: str | None, has_output: bool, expected: str | None) -> str | None   # ported, transport_v4.py L70-L88
def conformance(admitted: Mapping, manifest: dict) -> dict[str, bool]

class Gateway:                                     # implements wire.HandlesRequests
    def __init__(self, manifest: dict, paths: StatePaths, *, provider: ProviderClient, ledger: Ledger,
                 replay: ReplayStore, circuit: Circuit, clock: Clock | None = None, ids: IdSource | None = None,
                 rng: random.Random | None = None) -> None
    def register_job(self, job: JobIdentity, *, token: str | None = None) -> None
    def close_job(self, job_id: str, *, drain_timeout_s: float | None = None) -> dict
        # marks the job closed, waits for its provider attempts in flight, returns the "gateway" object of the job record (2.6)
    def handle(self, job: JobIdentity | None, request: bytes, *, ingress: str = "loopback",
               no_replay: bool = False) -> GatewayReply
    def in_flight(self) -> int
    def halted(self) -> HaltState | None           # the ledger's halt row, or the in-memory ledger_fault state
    def kill_requested(self, job_id: str) -> str | None       # "refusal_flood" once a job passed ten times the refused-record cap
    def abort_queued(self) -> None                 # followers and semaphore waiters reply `halted` at once; no new attempt starts
    def drain(self, timeout_s: float) -> int       # waits for every provider attempt in flight; returns how many are still open
```

`handle` is the transport-agnostic core of F3: job identity and request bytes (the envelope of `wire.make_envelope`) in; status, headers and body bytes out. It blocks, is callable from any thread, and never raises: an internal fault becomes the `gateway_internal` reply and a record. Steps:

1. Assign `call_id`; stamp `received_utc`.
2. **Envelope.** `parse_envelope`; method `POST` and a path ending in `/chat/completions` (any prefix), else `not_allowed`.
3. **Identity.** `job is None`, or a job that is not registered or already closed: `unknown_job`, recorded under `calls/_unknown/`. This is also the log of any attempt by a stray process to use a listener (critic gap 13).
4. **Halt.** `self.halted()` (the ledger's halt row, or the in-memory `ledger_fault` state of step 9) gives `halted`.

   Refusal records are capped. Steps 2 to 6 and step 9.2 can refuse. The first `gateway.max_refused_records_per_job` refusals of a job attempt each write a call record with a `request_excerpt` in place of the body (2.5). Later ones are answered the same way but only counted (`refused_unrecorded` in the job's counters, and in `refusal_codes`). When the count reaches ten times the cap, `kill_requested(job_id)` starts returning `"refusal_flood"`; the executor's wait loop sees it within a second and kills the worker. A worker that catches the error and keeps calling after a ceiling or a halt can therefore neither fill the disk nor run to its 7200 s limit.
5. **Admit** (`admit`): the body is 1 to `gateway.max_request_bytes` bytes and a JSON object (`ValueError`, `RecursionError` give `invalid_request`; oversize gives `request_too_large`). Top-level keys must be a subset of `model, messages, temperature, max_tokens, usage, stream`. `usage` is dropped (Route 2 adds `{"include": true}`; the provider must not get it [F-E3]); `stream` must be `false` when present and is dropped; any other key (`response_format`, `tools`, `max_completion_tokens`, ...) gives `invalid_request`. `model` must equal `provider.wire_model`, else `model_not_allowed`. `max_tokens` must be an integer in `1..gateway.max_output_tokens`; `temperature` a number (not a bool) in `0..2`; `messages` a list of 1 to `gateway.max_messages` objects, each with exactly the keys `role` and `content` (any other key, such as `name`, `tool_calls` or `reasoning`, is refused), `role` in `system`, `user`, `assistant` and `content` a string or a list of `{"type": "text", "text": str}` parts (those two keys only); else `invalid_request`. `content_bytes` (UTF-8 bytes of all message text, unescaped: `json.dumps` counts a Chinese character as six bytes [rep H2-V]) above `gateway.max_content_bytes` gives `request_too_large`. `temperature`, `max_tokens` and the messages are forwarded unchanged. Conformance with `upstream.expect` is recorded, not enforced.
6. Compute `request_sha256`, the reuse key `replay.reuse_key(request_sha256, salt)` (3.7; `salt` is `s<k>` for a job with a sample index, else none), `user_sha256`, `input_limit(content_bytes, len(messages), manifest)`, `output_limit = max_tokens`. A token is at least one UTF-8 byte, so `content_bytes` bounds the text tokens; the chat template costs a fixed part (`template_overhead_tokens`) plus a part per message (`per_message_overhead_tokens`; five tokens were measured offline for a one-byte message [A-M m3, A-S e_tokens]), and the number of messages is bounded by step 5, so the bound holds for any admitted request [inf for the server's own template; PF5 sends one request with `gateway.max_messages` one-byte messages and qualification checks `prompt_tokens <= input_limit` on every call]. If the provider still reports more prompt tokens than `input_limit`, the ledger settles the row as `limit_exceeded` without a halt as long as the cost fits the reservation (3.6). Write the admission record.
7. **Reuse.** `lease = replay.begin(job, <reuse key>, call_id=call_id, bypass=no_replay and job.phase == "qualification")`. `hit`: reply 200 with `zero_usage(stored.body)`, header `x-mhforge-source: replay`, outcome `replayed`. `follower`: loop on `lease.wait(1.0)`; each second it re-checks the halt and `abort_queued` (reply `halted`) and whether its own job was closed (reply `unknown_job`, detail `job_closed`); a reply from the leader ends as outcome `coalesced` (header `coalesced` for a 200); `None` after the leader is gone returns to this step. `leader`: continue. From here to the end of `handle` a `try/finally` calls `lease.abandon()` if the lease was neither committed nor failed.
8. **Queue.** Acquire the global semaphore, a `threading.BoundedSemaphore(gateway.max_in_flight)`, with a one-second timeout in a loop; each second re-check the halt and `abort_queued` (reply `halted`, shared) and whether the job was closed (reply `unknown_job`, not shared, so a follower from another job takes over as leader). The wait is `queue_s`. `handle` keeps a local flag `holding`, set to true only when an acquire succeeded. **The slot is released exactly when `holding` is true, in a `finally` that wraps steps 8 to 11.** An unconditional release after a halt during the re-acquire of step 9.6 would return a slot that is not held (17 permits for a limit of 16, or a `ValueError` from the bounded semaphore), and an exception between acquire and release without a `finally` would leak one; sixteen leaks stop the gateway [A-M m2].
9. **Attempt loop**, `n` in `1 .. 1 + provider.retry.max_transport_retries`:
   1. `circuit.enter(max_wait_s=provider.circuit.cooldown_s + provider.attempt_deadline_s + 60)`. A `TimeoutError` replies `gateway_internal`. **On the line after `enter()` returns, a `try/finally` begins that calls `circuit.finish(outcome)` exactly once for this attempt.** `outcome` starts as `"neutral"` and is set from the `classify` table only after the provider call returned; so a `BudgetError` at 9.2, and any exception before or during the provider call, finish `neutral` and release a half-open probe (3.8).
   2. `ledger.reserve(...)` with `phase = wire.ledger_phase(job, n, prior_charged=p)`, where `p` is `ledger.prior_charged(job.job_key)` read once in `register_job`. `BudgetError`: `total` or `cap` makes the gateway call `ledger.set_halt("budget:total" | "budget:<phase>")` and reply `budget_exhausted` (shared); `halted` or `unreconciled` reply `halted` (shared); `ceiling` (any of its three details) replies `job_request_ceiling` (not shared, no halt: the job fails, the campaign goes on); `invalid` replies `gateway_internal`.
   3. `provider.complete(admitted + stream fields, sse_sink=<call_id>.a<n>.sse, deadline_s=provider.attempt_deadline_s)`. Nothing is sent to the worker until the stream is complete, so every failure up to here is retryable. On return, set the circuit `outcome` from the `classify` table.
   4. Disposition `ok`, in this order:
      - the identity gate (`model_identity_problem` with `provider.expected_response_model`). On a problem: settle the row from usage (it was paid), `ledger.set_halt("model_identity")`, reply `halted`; nothing is delivered or stored.
      - build the worker body with `to_completion_body`;
      - `lease.commit(...)`: the reply is stored and the followers are woken (store first, 3.7);
      - `ledger.settle(ledger_id, usage)`. A settlement of `unknown` has halted the ledger in the same transaction; the reply is still delivered, because it was paid for and is already stored.
      - leave the loop.
   5. Any other disposition: `ledger.settle(...)` as the `classify` table says, then:
      - `context_limit`: reply `context_length_exceeded` (shared). `provider_rejected`: reply `provider_rejected` (shared). Neither is retried; both are the candidate's failure, not a transport failure.
      - `config_fault`: `ledger.set_halt("provider_config", <excerpt>)`; reply `halted` (shared). `quota_exhausted`: `ledger.set_halt("provider_quota", <excerpt>)`; reply `halted` (shared). Neither is retried.
      - `retry_transport` or `retry_rate_limited`: step 9.6 when `n` is not the last attempt, else step 9.7.
   6. **Backoff.** Release the slot and set `holding = False`; `clock.sleep(backoff_s(...))`; re-acquire as in step 8 and set `holding = True` only when the acquire succeeded (a halt, an abort or a closed job while waiting ends the request with `holding` still false); continue with `n + 1`.
   7. **Attempts exhausted.** `repeatable = ledger.note_exhausted(job, request_sha256, call_id, peer_ok=q)`, where `q` is true when a provider attempt of another request ended `ok` since this request's first provider attempt started (the gateway keeps the monotonic time of the last `ok`). When `repeatable` is false the reply is `provider_unavailable` (shared): an infrastructure failure. When it is true the reply is the refusal `provider_rejected` (shared) with `refusal.detail = "repeatable_provider_failure"`, and the job's counter `repeatable_provider_failures` is incremented: the same request has now used up the gateway's attempts in two job attempts while the provider was serving other requests, so it is treated as the request's own failure, which is candidate-attributable and not retried. Without this rule a provider fault that is deterministic for one prompt (the reports name chat-template failures answered as 500 and a streaming parser that drops the rest of a stream [rep SI, SI-V]) would use up the infrastructure attempts and stop the campaign at every restart, with the search unable to move past the candidate [inf that such a fault recurs for the same prompt at temperature 0]. The residual risk is deviation V25: in a long brown-out in which the provider serves some requests and fails this one six times in a row, a candidate is charged an attempt it did not deserve; the record says `repeatable_provider_failure`, so such cases can be counted.

   **Settle failures.** `ledger.settle` is retried up to three times over 90 s on `sqlite3.OperationalError` (another connection can hold the write lock past the busy timeout [A-M m1]). If it still fails, or raises anything else: the row stays `pending` (the next start books it as `bounded_unknown` at its reservation); the gateway sets its in-memory `ledger_fault` state, which `halted()` reports from then on; it tries `ledger.set_halt("ledger_fault")` (which may fail for the same reason; the runner's `halt_check` then retries it); and the request ends as follows: a reply that was already committed (`ok`) is still delivered, any other request ends `failed` with `gateway_internal`. A busy ledger is never reported as `usage_contradiction` and never throws away a paid reply.
10. **Reply.** Success: one non-streaming JSON body, `content-type: application/json`, `x-mhforge-source: provider` (the lease was committed in 9.4). Anything else: `lease.fail(reply, share=...)`.
11. **Finish**, in the `finally` blocks: release the slot if `holding`; `lease.abandon()` if the lease is still open; replace the call record with the outcome (or count the refusal, step 4); update the job's in-memory counters and the gateway's time of the last `ok`; wake `close_job` and `drain` waiters.

`close_job(job_id, drain_timeout_s=None)`: marks the job closed in the ledger and in memory (new requests for it get `unknown_job`; its followers and queue waiters leave within a second, steps 7 and 8); waits until no `handle` call of that job is between steps 8 and 11, for at most `drain_timeout_s` (default `provider.attempt_deadline_s + 60`); returns the counters with `"in_flight_at_close": n`, the number still open when it returned, and with `usage_calls`, `prompt_tokens_sum`, `prompt_tokens_max` and `completion_tokens_sum` over the job attempt's completed calls that carried usage (2.6). Attempts that are still open finish, settle and store on their own (G5, G9), and `audit` accounts for them. `abort_queued()` makes every follower and queue waiter of every job reply `halted` within a second and stops new attempts from starting. `drain(timeout_s)` waits for every provider attempt in flight and returns the number still open. The runner's stop path is `abort_queued()`, `drain(provider.attempt_deadline_s + 60)`, `executor.close()` (3.20), so a clean stop leaves no `pending` row, and the interpreter never has to decide how long to wait for a pool thread [A-M m5].

`backoff_s`: uniform in `backoff_429_s` for `retry_rate_limited` (tens of seconds, so the edge never sees a fast retry; retrying 429s quickly triggers a temporary block [rep INF-1101]); uniform in `backoff_5xx_s` when `http_status` is 5xx or 408; uniform in `backoff_other_s` otherwise.

Invariants (each has a test):

- G1. Every request gets exactly one reply, and exactly one call record up to the per-job cap on refused records; beyond the cap a refusal is counted, not recorded.
- G2. Every provider attempt has one ledger row that is opened before the connection and settled afterwards, whatever happens in between (a settle that cannot be written leaves the row `pending` and halts with `ledger_fault`).
- G3. The reply status is one of 200, 402, 403. Never 429, never 5xx, never a partial body, never provider text.
- G4. The forward body holds only the validated keys plus the two stream fields.
- G5. After a halt no new provider attempt starts; attempts in flight finish and settle, and `drain` waits for them.
- G6. A request identical to one in flight, or to one already answered with a 200 within its scope, never causes a second provider attempt. (A request the provider rejected is forwarded again when it is repeated: rejections are not stored, and cost nothing.)
- G7. Single retry owner: one worker request causes at most `1 + max_transport_retries` provider attempts.
- G8. At most `gateway.max_in_flight` reservations are pending at any time, because a reservation is made only while a semaphore slot is held.
- G9. A worker that disconnects changes nothing: `handle` does not know about the connection, finishes, settles and stores; the worker's retry of the same request follows the in-flight leader or reads the store.
- G10. Every `circuit.enter()` is followed by exactly one `circuit.finish()`, and the number of free semaphore permits never exceeds `gateway.max_in_flight`, whatever exception or refusal occurs in between.

**Timeout position** (findings section 5 item 4, left open by the sources). One provider attempt may run for 900 s because the stream keeps the edge connection active. The worker's own 600 s client timeout cannot be changed without an upstream edit; when it fires, upstream's tenacity retries the identical request (its error text contains "timeout", `llm.py` L98-L108, up to 6 requests on Route 2 [rep]) and each retry coalesces onto the same in-flight call (G6, G9). No provider request is ever duplicated by a client retry; the cost is wall-clock only. A whitespace heartbeat that would stop the client timing out at all was confirmed by the failure draft [X-E1] and is deferred (Appendix A, item 4; section 8.3).

Never: reads a key; writes an `Authorization` value to a record or log; binds a socket; follows a URL or takes a model from a request; raises for a request-level problem.

### 3.11 `ingress_http.py`

```python
@dataclass(frozen=True)
class Endpoint:
    host: str; port: int; base_url: str            # "http://127.0.0.1:<port>/v1"
class LoopbackIngress:
    def __init__(self, gateway: HandlesRequests, manifest: dict) -> None
    def open(self, job: JobIdentity, token: str | None) -> Endpoint    # binds ("127.0.0.1", 0); one listener per job
    def close(self, job_id: str) -> None
    def open_ports(self) -> list[int]
```

- `ThreadingHTTPServer` with `request_queue_size = gateway.loopback_backlog` (128; H2 inherited 5, which costs about a second of SYN retry at 94 connections [X-E5]), `daemon_threads = True`, HTTP/1.1 with a `Content-Length` on every response.
- The job is identified by the listening socket. With a token (Route 2) the request must also carry `Authorization: Bearer <token>`, compared with `hmac.compare_digest`; a mismatch is handled as `job=None` (`unknown_job`). On Route 1 the bearer is always `local` (`llm.py` L213-L215 [src]), so the port alone identifies the job; that is acceptable only because the loopback ingress is used by `LocalExecutor`, which is development-only and not isolated.
- `POST` only; `Content-Length` required; a body over `gateway.max_request_bytes` is refused with `request_too_large` without being read; any other method or a missing length gets `not_allowed`. The handler builds the envelope from the request line and body and calls `gateway.handle(job, envelope, ingress="loopback")`; it ignores `BrokenPipeError` when the worker has gone.
- The listener exists only between `open` and `close` of a job. No listener exists while the proposer runs.

Never: binds anything but `127.0.0.1` (the host is not a parameter); logs headers or the token; puts the token in a URL; offers any other endpoint.

### 3.12 `insandbox/mhf_frames.py` and `relay.py` (host side)

```python
# insandbox/mhf_frames.py   (standard library only: base64, json, zlib; shipped into the executor; imported by the host too)
MAGIC = "#MHF1 "
TYPES = frozenset({"HELLO", "REQ", "RES", "CAN", "EXIT", "LOG", "PING", "PONG", "KILL", "BYE"})
MAX_B64_LEN = 16 * 1024 * 1024
MAX_HEADER_CHARS = 256
class FrameError(ValueError): ...
def encode_frame(ftype: str, rid: str, meta: dict, body: bytes = b"", *, compress: bool = True) -> bytes   # FrameError on a bad type, rid or size
class FrameDecoder:
    def __init__(self, max_b64_len: int = MAX_B64_LEN) -> None
    noise_chars: int
    errors: int
    def feed(self, text: str) -> list[tuple[str, str, dict, bytes]]      # (type, rid, meta, body); never raises on bad input
```

The format and the decoder rule are in 2.9. `feed` holds at most one partial frame; it rejects a frame as soon as a payload character outside the base64 alphabet arrives (it does not wait for `b64_len` characters); it decompresses with `zlib.decompressobj().decompress(data, raw_len + 1)` and rejects a frame whose decompressed size differs from `raw_len`. A decoder belongs to one channel: the host creates one per attached bridge and the stub one per socket connection.

```python
# relay.py
class FrameChannel(Protocol):
    def read_text(self) -> str | None: ...         # next chunk of text as the transport delivers it; None at end of stream
    def write(self, data: bytes) -> None: ...      # may block (backpressure) for at most relay.pong_timeout_s; ChannelClosed when
                                                   # the far end is gone or the write did not return in that time
    def close(self) -> None: ...

class PipeChannel:                                 # local subprocess pipes standing in for the exec stream
    def __init__(self, proc: subprocess.Popen, *, mimic_sdk: bool = True, chunk: int = 65536) -> None
    retained_bytes: int                            # with mimic_sdk: every stdout byte is counted as retained, as the SDK retains it

class Bridge:                                      # one attached channel, with its own FrameDecoder
    chars_in: int
    alive: bool                                    # false after end of stream, a write failure, eight frame errors, or pong_timeout_s without a frame
    hello: dict | None
    def detach(self) -> None                       # stop reading; close the channel

class RelaySession:                                # one per job attempt
    def __init__(self, gateway: HandlesRequests, job: JobIdentity, manifest: dict, *, clock: Clock | None = None) -> None
    def attach(self, channel: FrameChannel) -> Bridge                    # new decoder, one reader thread; the newest bridge is the write target
    def wait_exit(self, timeout: float | None) -> dict | None           # the stub's EXIT meta, or None at the timeout
    def send_kill(self, reason: str) -> bool                            # queues a KILL frame; False when no live bridge exists
    def total_chars_in(self) -> int                                     # stub-to-host stream volume of all bridges of this attempt
    def counters(self) -> dict
        # mode, attaches, rotations, frames_in, frames_out, chars_in, bytes_out, noise_chars, frame_errors, duplicate_requests,
        # resent_replies, cancelled, flood_refusals, pings, logs
    def note_rotation(self) -> None                                     # called by the executor when it replaces a live bridge on purpose
    def close(self, *, send_bye: bool = True) -> None
```

- `PipeChannel(mimic_sdk=True)` reads `os.read(fd, chunk)` and decodes each chunk alone with `decode("utf-8", errors="replace")`, the same call the SDK makes on every stdout frame (`cwsandbox` `_sandbox.py` L8266-L8274 [rep, critic gap 1]). Offline runs therefore exercise the same decoding path as a sandbox. It writes with `os.write` on the unbuffered stdin descriptor in a loop; `BrokenPipeError` becomes `ChannelClosed`. Processes it wraps must be started with `bufsize=0`; `close` ignores errors [L1].
- `chunks` reach the session exactly as the exec stream delivers them: arbitrary boundaries, already decoded with replacement. Each bridge feeds its own decoder (2.9).
- **Reader threads never write to the channel.** Everything the host sends (`RES`, `PING`, a re-sent reply, a refusal, `KILL`, `BYE`) goes through one writer queue served by one writer thread, which slices a frame into pieces of at most `relay.write_chunk_bytes` and writes it to the current bridge. A reader that answered a frame itself would stop reading while it was blocked in a write, which over a pair of pipes is one `PING` away from a deadlock [inf]. A write that raises `ChannelClosed` (far end gone, or no return within `relay.pong_timeout_s`) marks the bridge dead; the frame stays queued for the next bridge when it is a `RES` (daemon mode) and is dropped otherwise.
- `REQ`: a `rid` that is in flight is counted (`duplicate_requests`) and ignored; a `rid` whose reply is in the done cache gets that `RES` again (`resent_replies`); anything else is added to the in-flight set and submitted to a pool of `relay.max_workers` **daemon** threads that call `gateway.handle(job, make_envelope(meta["method"], meta["path"], body), ingress="relay")` and queue the `RES`. The job identity is the session's; nothing inside a frame can change it. The pool's threads are daemon threads so that the gateway's `drain`, not the interpreter's exit, decides how long to wait for an attempt [A-M m5].
- `CAN <rid>`: the worker's client for that request has gone. The `rid` leaves the session's live set. If its gateway call is a follower it is of no further use, but it is left to finish (it costs nothing); a leader keeps running in any case (G9), and its reply is stored for the retry. A `HELLO` has the same effect for every in-flight `rid` that is not in its `pending` list.
- **Flood rule.** The session counts live requests: in flight on the host and not cancelled. A well-behaved worker has at most 64 (32 predict threads plus a batch pool of 4 on Route 2 or 32 on Route 1, `llm.py` L324 and `inner_loop.py` L231, L461 [rep]), because a request abandoned by the client's 600 s timeout is cancelled by `CAN`. With more than `4 * relay.max_workers` live requests a new `REQ` is answered at once with the refusal `relay_overloaded` and counted in `flood_refusals`; nothing is forwarded. Because only live requests count, reaching the limit is the worker's own doing, and 3.16 classifies it as the job's own ceiling, not as infrastructure.
- **Done cache (daemon mode only).** A finished `RES` frame is kept by `rid`, at most `relay.done_cache_max` entries, oldest evicted. On a `HELLO` the cache is pruned to the `pending` list; cached replies for pending `rid`s are written again. The stub re-sends every pending `REQ` after each `HELLO`, so a request whose `REQ` or `RES` was lost in a dropped stream is completed without a second provider call: a duplicate of an in-flight request is ignored, a duplicate of an answered request is re-answered from the cache, and one that fell out of the cache goes to `gateway.handle` again, where the replay store answers a success for free (G6). [L1] ran this path. In stdio mode (`HELLO.mode == "stdio"`) no reattach is possible and nothing is cached.
- **Liveness.** A timer thread queues a `PING` for the current bridge every `relay.ping_interval_s`. Every frame received on a bridge (a `PONG` included) refreshes its last-seen time. A bridge with no frame for `relay.pong_timeout_s` is marked dead, exactly as at end of stream. In daemon mode the executor then attaches a new bridge; in stdio mode the attempt ends `relay_lost`. Without this an exec stream that goes quiet without closing (an idle timeout during a 300 s provider call, a partition) was never replaced: the reply went into the SDK's stdin queue and was lost, and the attempt ran to the job deadline [A-M; that exec streams can stall silently is inferred, and the relay part of PF6 tests it].
- `LOG` frames are counted and the first 50 kept. `EXIT` sets the exit event and its meta (with `local_refusals` and `unanswered_at_exit`) is what `wait_exit` returns. A frame error is counted; eight in one bridge mark it dead. End of stream or `ChannelClosed` marks the bridge dead; gateway calls in flight keep running and their replies wait in the done cache.
- Host memory: the session holds one decoder with at most one partial frame per live bridge, the in-flight set, the writer queue and the done cache. The SDK's own unbounded `stdout_buffer` (`_sandbox.py` L8273, L8448 [rep]) is bounded by the executor: it rotates the bridge exec after `relay.rotate_after_mb` of `Bridge.chars_in` in daemon mode, and fails the attempt with `stream_budget` past `relay.max_stream_mb` in stdio mode. In both modes the executor also fails the attempt with `stream_budget` when `total_chars_in()` passes `relay.max_job_stream_mb`: stream volume is produced by the worker's own requests, so this is the job's ceiling, not an infrastructure failure (3.16).

Never: trusts a field of `HELLO`, `EXIT` or `LOG` (they are type-checked and advisory; the executor still reads the exit file, and the two counters of `EXIT` can only make an attempt worse, never a success); keeps a request body after dispatch; writes anything but frames to the channel; writes from a reader thread; reuses a decoder for a second channel.

### 3.13 `insandbox/mhf_relay_stub.py` and `insandbox/mhf_selfcheck.py`

Both are single files, standard library only, and import nothing from `mhforge`. The stub imports its sibling `mhf_frames`.

```
python mhf_relay_stub.py serve  --job JOB.json --uplink stdio|unix:PATH [--daemonize] [--linger 30]
python mhf_relay_stub.py bridge --uplink unix:PATH
python mhf_selfcheck.py --job JOB.json [--probe-egress]
python mhf_selfcheck.py --job JOB.json --import-check NAME
```

```python
# mhf_relay_stub.py
LOCAL_REFUSALS: dict[str, tuple[int, bytes]]       # copies of the wire.REFUSALS rows relay_unavailable and request_too_large; a test compares them
def main(argv: list[str] | None = None) -> int
```

The stub reads these job-file fields: `job_id`, `argv`, `env`, `cwd`, `route`, `port`, `timeout_s`, `reply_timeout_s`, `ping_interval_s`, `pong_timeout_s`, `max_request_bytes`, `run_dir`.

`serve` (the job file is 2.6):

1. Binds an HTTP server to `127.0.0.1:<job.port>` (0 picks a free port), `request_queue_size = 128`, HTTP/1.1, one thread per request. When the stub's own environment has `MHF_STUB_BEARER` (set only by `LocalRelayExecutor`, where the stub runs on the host; never present in a sandbox and never in a job file), every request must carry `Authorization: Bearer <that value>`, compared with `hmac.compare_digest`; a request without it gets the `relay_unavailable` refusal, is not relayed, and is counted as `unauthorized` in the stub's diagnostics, not in `local_refusals` (so a stray local process cannot make an attempt look like an infrastructure failure).
2. Uplink `stdio`: frames are read from fd 0 with `os.read(0, 65536)` and written to fd 1 with `os.write` by one writer thread fed from a queue, in slices of 32,768 bytes; `HELLO` is sent at once. Fd 1 carries frames only: the worker's output goes to `<run_dir>/stdout.txt` and `<run_dir>/stderr.txt`, the stub's diagnostics to `<run_dir>/stub.txt`. Uplink `unix:PATH` (daemon mode): the stub listens on a Unix socket (it unlinks a stale one first); the newest connection replaces the previous one and gets a new `FrameDecoder`; on each attach it sends `HELLO` with its pending request ids, then every pending `REQ` again, then `EXIT` if the worker has ended. Requests wait across a gap [L1]. `PATH` must be at most 90 bytes [L1]. **One lock covers the connection swap, the `HELLO` snapshot and every `REQ` write, and the stub records for each pending request the connection it was last sent on; after a swap every pending request whose last connection is not the current one is sent again.** Without that, a `REQ` written to the old connection in the instant the new one replaced it was in neither the `HELLO` resend nor the new stream, and waited for the worker's 600 s timeout [A-M]. `PONG` is queued to the writer in answer to every `PING`; the thread that reads the uplink never writes.
3. `--daemonize` (daemon mode): double fork and `setsid`; standard streams to `<run_dir>/stub.txt`; the parent returns 0 as soon as `<run_dir>/stub.ready` (`{"pid", "port"}`) exists, or 2 after 20 s.
4. Starts the worker: `job.argv` (on Route 1 the literal argument `{api_base}` replaced by `http://127.0.0.1:<port>/v1`), environment = `job.env` plus on Route 2 `OPENROUTER_API_BASE=http://127.0.0.1:<port>/v1` and `OPENROUTER_API_KEY=mhf-relay` (or the value of `MHF_STUB_BEARER` when that is set, so the worker presents the bearer of step 1), cwd `job.cwd`, `stdin=DEVNULL`, `start_new_session=True`. Nothing else of the stub's own environment is passed on. On Route 1 the worker's bearer is always `local` (`llm.py` L213-L215), so with `MHF_STUB_BEARER` set on Route 1 the stub accepts `local` as well and the port alone identifies the job, as under the loopback ingress.
5. Each `POST` whose path ends in `/chat/completions`: a body over `job.max_request_bytes` is answered locally with `request_too_large` without being relayed. Otherwise the stub allocates `rid = "r<counter>"`, keeps the request pending, sends `REQ` (if an uplink is attached) and waits for the matching `RES` up to `job.reply_timeout_s`; then it writes the HTTP response with the status and body of the frame, the headers `content-type`, `x-should-retry` and `x-mhforge-source` from the frame, and a `Content-Length`. While it waits it polls the client socket once a second (`select` for readability and a zero-byte peek); when the client has gone (its 600 s timeout fired, or the worker died) it drops the pending entry and sends `CAN <rid>`. No reply in time, or any other method or path: the `relay_unavailable` refusal. Every refusal the stub issues itself is counted in `local_refusals` by code. The stub forwards only method, path and body, so a worker-supplied `Authorization` value never leaves the executor.
6. Supervisor: waits for the worker up to `job.timeout_s`; on expiry SIGTERM to the process group, 10 s, SIGKILL, return code 124, reason `timeout`. A `KILL` frame at any time (either mode): SIGTERM to the process group, 10 s, SIGKILL, reason `killed`. It writes `<run_dir>/exit.json` (2.6) atomically, with `local_refusals` and `unanswered_at_exit` (the number of entries still pending, each sent to the host and not answered, at the moment the worker ended; entries dropped by the client-gone rule of step 5 are not counted), sends `EXIT` with the same fields, and then: stdio mode leaves with `os._exit(0)`; daemon mode stays for `--linger` seconds or until `BYE`, so a late bridge can still fetch `EXIT`, then exits 0.
7. Stdio mode only: end of file on fd 0, `BYE` before the worker ended, or no frame of any type from the host for `3 * job.ping_interval_s + job.pong_timeout_s` seconds means the link is gone: the stub kills the worker's process group, writes `exit.json` with reason `uplink_lost` (or `killed` for `BYE`) and leaves with `os._exit(75)`. In daemon mode a bridge that goes away is not a loss.

Rules: the uplink is read with `os.read` and the process leaves through `os._exit` (a stub that reads `sys.stdin.buffer` from a daemon thread aborts at interpreter exit with return code -6 [T-E1]). The stub reads one environment variable by name (`MHF_STUB_BEARER`), never writes its value to a file or a frame, holds no credential of any service, writes no request body to disk, and listens on nothing but `127.0.0.1` and its Unix socket.

`bridge`: connects to the Unix socket and copies fd 0 to the socket and the socket to fd 1 with `os.read` / `os.write` until either side closes; exits 0 through `os._exit`; exits 3 if the socket does not exist. It holds no state, so any number of bridges can come and go.

Why both modes exist. The SDK does not resume an exec stream after a drop and cannot reattach to a running command [rep SB-V 1.5], and it keeps every stdout byte of an exec in memory [rep]. The findings' binding constraint is to run the worker detached with a result file. Daemon mode makes the worker's life independent of any one stream and lets the host rotate streams; stdio mode is the smallest thing that works and is what the unit tests and `LocalRelayExecutor` use by default. The preflight decides which one the sandbox uses (`relay.mode`).

```python
# mhf_selfcheck.py
def main(argv: list[str] | None = None) -> int     # prints one JSON line; exit 0 always (the host decides)
```

The selfcheck reads these job-file fields: `env`, `cwd`, `vocab_file`, `probe_endpoints`, `expect`.

Report (`mhforge.selfcheck/1`): `{"schema", "python", "machine", "freeze_sha256", "distributions": int, "env_names": [...], "credential_names": [...], "wandb_api_key": "absent"|"empty"|"set", "identity_token_file": bool, "egress": {"tcp": "blocked"|"open"|"skipped", "dns": "blocked"|"open"|"skipped", "probes": [{"name", "target", "result": "blocked"|"open"|"skipped"}]}, "versions": {"litellm", "openai", "openai-harmony", "datasets", "tenacity"}, "tokenizer_dir": str|null, "vocab_file_present": bool, "cache_writable": bool, "expect_problems": [str]}`.

- **Proof of the environment.** `machine` is `platform.machine()` (what `uname -m` prints), `python` is `platform.python_version()`, and `freeze_sha256` is the SHA-256 of the lines `<name>==<version>` for every installed distribution (`importlib.metadata.distributions()`; names lower-cased with runs of `-`, `_` and `.` turned into one `-`), sorted and each ended by a newline. It is the same information as `pip list --format=freeze`, computed without starting `pip`. `expect_problems` lists what differs from `job.expect`: the machine, the Python version prefix, and the freeze hash when `job.expect.freeze_sha256` is not null. The host refuses to start the worker on any of them (3.16), so every sandbox proves its architecture, interpreter and package set before a paid call.

- `env_names`: names in the selfcheck's own environment. `credential_names`: names for which `re.search(r"(?:^|_)(API_?KEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)(?:_|$)", name.upper())` matches and whose value is non-empty, minus the exact names `OPENROUTER_API_KEY` and `TIKTOKEN_RS_CACHE_DIR`. Never a value. The pattern and the two exempt names are copies of `wire.CREDENTIAL_NAME_PATTERN` and `wire.CREDENTIAL_NAME_EXEMPT`; a test compares them. The first draft matched `(KEY|TOKEN|...)` as substrings, so `TIKTOKEN_RS_CACHE_DIR`, which the findings tell the image to set, counted as a credential and the first sandbox job would have stopped the campaign with a false isolation halt [A-S].
- `identity_token_file`: whether `/var/run/secrets/sandbox/identity-token/token` exists.
- Egress probes run only with `--probe-egress` (never in local executors or tests), each with a 3 s limit, and **by literal address**: with DNS denied a by-name probe fails at the lookup and proves nothing about packets [r2]. Two results gate: `tcp` is a TCP connect to `1.1.1.1:443`, and `dns` is one lookup of `api.inference.wandb.ai` (it tests `dns_egress="deny"`); either one `open` is an isolation failure. The rest are recorded in `probes` and gate nothing, because the platform's own documentation says deny-egress is not complete network isolation [r2] and what answers there is a fact to report, not a reason to stop: `metadata` (`169.254.169.254:80`), `resolver` (the first `nameserver` of `/etc/resolv.conf`, port 53), `cluster` (`KUBERNETES_SERVICE_HOST` with `KUBERNETES_SERVICE_PORT`, default 443; `skipped` when the variable is absent), `ipv6` (`[2606:4700:4700::1111]:443`), and one `extra:<target>` per entry of `job.probe_endpoints` (the object-store endpoint when the manifest names it).
- `versions` come from `importlib.metadata` (no import of LiteLLM). `tokenizer_dir` is located with `importlib.util.find_spec("litellm")`; `vocab_file_present` checks `job.vocab_file` inside it (true when `job.vocab_file` is null). A worker without that file makes every call and then exits 1 with `HarmonyError`, after the calls were paid for [rep CM-V C4], so the host refuses to start the worker when it is missing.
- `--import-check NAME`: requires `re.fullmatch(r"[a-z][a-z0-9_]{0,47}", NAME)` (exit 2 otherwise), then `os.execve`s `python -c <code>` with `job.env` and cwd `job.cwd`, where `<code>` (the constant `IMPORT_CHECK_CODE` with the name filled in) does two things. First upstream's own line, `from text_classification.agents.<NAME> import *` (`meta_harness.py` L197-L208 [src]), with the worker's environment and no gateway variables. Then the **loaded-class assertion**: it imports the module, walks `inspect.getmembers(module, inspect.isclass)` exactly as upstream's loader does (`inner_loop.py` L500-L502: the first class, in alphabetical order, that is a subclass of `MemorySystem` other than `MemorySystem` itself [src]) and requires that class's `__module__` to be the candidate's own module. It prints `OK` and exits 0 when both hold; it prints `FOREIGN_CLASS <qualified name>` and exits 3 when the class the loader would pick was imported from elsewhere (a candidate that imports a base class whose name sorts first would silently be evaluated as that other system; upstream PR #22 [r2]); `NO_CLASS` and exit 4 when there is none. The job file here is the one of `bundle.build_check_file`.

### 3.14 `workspace.py`

```python
PINNED_FILES: tuple[str, ...]      # the 39 files git tracks under EX at f805ad3, relative to EX:
    # .claude/skills/meta-harness/SKILL.md, README.md, __init__.py, agents/{__init__,fewshot_all,fewshot_memory,no_memory}.py,
    # benchmark.py, claude_wrapper.py, config.yaml, data/README.md, data/{__init__,api,constants,evaluators,loaders}.py,
    # data/{aegis2,crime_prediction,finer,symptom_diagnosis,uspto}/{train,val|valid,test}.jsonl (as tracked),
    # inner_loop.py, llm.py, memory_system.py, meta_harness.py, pyproject.toml, tests/test_{benchmark,data,evolution_contracts}.py
PINNED_AGENTS = ("__init__", "no_memory", "fewshot_memory", "fewshot_all")

def source_digest(example_dir: Path) -> tuple[str, dict[str, str]]      # (tree_digest, {rel: sha256}); WorkspaceError on a missing file or a symlink
def ensure_workspace(manifest: dict, paths: StatePaths, example_dir: Path, lock_file: Path | None) -> Path     # returns WS
def verify_pinned(paths: StatePaths) -> dict[str, list[str]]             # {"modified": [...], "removed": [...]} against pristine.json
def restore_pinned(paths: StatePaths, example_dir: Path, changed: Mapping[str, list[str]]) -> list[str]
def snapshot(paths: StatePaths) -> dict[str, str]                        # tree_hashes of WS without logs/, .venv, __pycache__, .empty_plugins
def classify_drift(before: Mapping[str, str], after: Mapping[str, str]) -> dict
    # {"agents_added", "agents_modified", "upstream_modified", "upstream_removed", "other_added"}
def unknown_entries(paths: StatePaths) -> list[str]
    # relative paths of files and directories in WS that this build does not expect (the rule is below)
def quarantine_entries(paths: StatePaths, entries: Sequence[str], utc: str) -> list[str]
    # moves them to STATE/quarantine/<utc>/workspace/<relative path>; returns what was moved; never follows a symlink
def candidate_sha256(paths: StatePaths, name: str) -> str | None
def validate_candidate_file(paths: StatePaths, name: str) -> str | None   # None = ok; else the reason
def read_pending_eval(path: Path, *, max_bytes: int = 65536) -> list[dict]      # shape check; WorkspaceError on a bad file
def env_fingerprint(python: Path) -> str
    # sha256 over the sorted *.dist-info directory names under procenv.venv_root(python)/lib/python*/site-packages (2.3 path rule)
def memory_dir_fingerprint(home: Path, cwd: Path) -> str | None           # names and sizes only, hashed; never contents
def load_config(ws: Path) -> dict                   # lazy `import yaml`; MhforgeError naming the two supported interpreters when missing

@dataclass
class Upstream:
    meta_harness: ModuleType; benchmark: ModuleType; claude_wrapper: ModuleType
@contextmanager
def load_upstream(paths: StatePaths) -> Iterator[Upstream]                # import context; checks the contract below
```

Why a working copy. Upstream writes candidates to `EVOLVE_DIR/agents` and logs to `EVOLVE_DIR/logs/<run>` (`meta_harness.py` L26-L28, L380-L388 [src]); neither path is git-ignored in `REPO`; `agents/` is shared by every run [rep]; and in upstream permission mode the proposer can edit any file in its working directory. `EVOLVE_DIR` is `Path(__file__).parent` (L26), so importing `meta_harness` from a copy moves every upstream path into the copy with no patching of path globals [F-E2, X-E4, T-E6]. The copy keeps `REPO` clean (F1, F13), gives each campaign its own `agents/`, and makes edits to pinned files detectable and reversible. The copy directory is named `text_classification` because the code imports that package name (`validate_candidates` imports `text_classification.agents.<name>` with cwd `EVOLVE_DIR.parent`, L203-L210 [src]).

- `ensure_workspace`, first call: copy `PINNED_FILES` from `EX` to `WS` (files are copied, never linked); require `source_digest(WS)[0] == upstream.tree_sha256` (`WorkspaceError` otherwise); write `pristine.json`; when `lock_file` exists copy it to `WS/uv.lock` (so a `uv run` typed by the proposer finds a lock and, through `UV_PROJECT_ENVIRONMENT`, `UV_NO_SYNC` and `UV_FROZEN`, uses the example venv instead of creating one [inf, live]). Later calls: `verify_pinned`, then `restore_pinned` for anything that differs.
- `validate_candidate_file`: the name fully matches `CANDIDATE_RE`; it is not in `PINNED_AGENTS`; `agents/<name>.py` exists, is a regular file, is not a symlink and is at most 262,144 bytes; `agents/` holds at most 400 `*.py` files. **Other entries in `agents/` do not reject a candidate.** `__pycache__/` is ignored everywhere (never hashed, never listed, never bundled); any other entry that is not a regular `.py` file is listed in `drift.other_added` and is never bundled. The first draft required `agents/` to hold only regular files. `SKILL.md` L78 tells the proposer to validate each candidate with `python -c "from text_classification.agents.<name> import *"`, and the proposer's environment has no `PYTHONDONTWRITEBYTECODE`, so that import leaves `agents/__pycache__/` behind; under the old rule every candidate of that iteration and of every later one would have been rejected while proposer sessions were still paid for [A-U X1].
- `classify_drift` (over the snapshot, which excludes `logs/`): `agents_added` and `agents_modified` are `agents/*.py` outside `PINNED_AGENTS`; `upstream_modified` and `upstream_removed` are pinned files; `other_added` is anything else under `agents/` (`__pycache__` excepted).
- `unknown_entries`: every file or directory in `WS` that is none of: a pinned file or a directory on the path to one; `agents/` and what is under it; `logs/` and what is under it; `.empty_plugins/`; `uv.lock`; a directory named `__pycache__`. These are what a proposer session (or a background child of one) added to the workspace outside the two places it is meant to write. `ProposerRunner` moves them out with `quarantine_entries` after each session (3.19). A stray `random.py` or `yaml.py` in the workspace root would otherwise sit in the working directory of the `benchmark.py` child.
- `read_pending_eval`: a JSON object with a list `candidates` of objects, each with a string `name`; `hypothesis`, `axis` and `base_system`, when present, are strings; `components`, when present, is a list of strings; no `name` occurs twice. Anything else is `WorkspaceError`, which the proposer runner treats as `no_candidates`. Upstream prints `hyp[:80]` for each candidate before validating (`meta_harness.py` L486-L488 [src]); a `hypothesis` that is `null`, a number or an object raises there [A-S], and because the session had already been stored as a usable proposal, every resume would have replayed the same file into the same crash.
- `load_upstream` **does not change `sys.path`**. For each of `claude_wrapper`, `benchmark`, `meta_harness`, in that order, it reads `WS/<name>.py`, checks the bytes against the pinned SHA-256, compiles exactly those bytes (`compile(source, str(path), "exec")`; no bytecode cache is consulted, so a planted `__pycache__/*.pyc` with matching metadata cannot stand in for the checked source, which the stock loader would execute [R1b]), creates the module with `importlib.util.spec_from_file_location(name, path)` and `module_from_spec`, registers it in `sys.modules` under its bare name and executes the code in it. Registering `claude_wrapper` and `benchmark` first is what satisfies `import claude_wrapper` and `from benchmark import ...` in `meta_harness.py` L23-L24 without any path entry [R1]. `sys.dont_write_bytecode` is set to true for the duration. On exit the three modules are removed from `sys.modules`. The three modules have no function-level import [A-U], so nothing needs a path entry later; `import yaml` resolves to the interpreter's own package, never to a `yaml.py` in the workspace [R1]. The first draft put `WS` and its parent at the front of `sys.path` for the whole run: a file the proposer wrote in the workspace root would then have run inside the runner, the process that holds the Forge key [A-S h_shadow]. Before yielding it checks the **upstream contract**; any failure is `UpstreamContractError`:
  - each module's `__file__` is under `WS` and its SHA-256 equals the pinned one; `meta_harness.EVOLVE_DIR == WS`;
  - `meta_harness` has `run_benchmark(args)`, `propose_claude(task_prompt, iteration, timeout)`, `validate_candidates(candidates)`, `update_evolution_summary(...)`, `run_evolve(args)`, `finalize_run(baselines, datasets, model_short)`, `count_iterations_from_summary()`, `PROPOSER_ALLOWED_TOOLS`, `PENDING_EVAL`, `LOGS_DIR`, `RESULTS_DIR`, `FRONTIER_VAL`;
  - `benchmark` has `build_val_runs`, `build_test_runs`, `_inner_loop_command`, `run_dir`, `update_summary`, `load_results`, `discover_all_memory_systems`, `get_model_short_name`, `DATASETS`, `MODELS`, `_CONFIG`, and `SEEDS == [42]`;
  - `claude_wrapper` has `run`, `build_command`, a module attribute `subprocess`, and `_EMPTY_PLUGIN_DIR`.

Never: writes under `EX` or anywhere in `REPO` outside `SC/state`; follows a symlink when hashing, copying, restoring or quarantining; restores or deletes anything under `agents/` other than the four pinned agent files; inserts a workspace directory into `sys.path`; imports upstream code through a bytecode cache.

### 3.15 `insandbox/mhf_capture_entry.py` (F9)

```
python mhf_capture_entry.py --sidecar PATH [--max-text-chars N] -- <text_classification.inner_loop argv...>
```

Requires `PYTHONPATH` to contain the parent of the package. Standard library only; reads no environment variable and no job-file field (everything it needs is on its command line). Hooks, each a plain attribute replacement on an imported module before the worker's `__main__` runs:

| Hook | Seam | Why it holds |
|---|---|---|
| H1 | `text_classification.llm.ProviderLLM._generate_one(self, prompt, system_prompt, kwargs)` (`llm.py` L252-L268) | `inner_loop.__main__` imports `LLM` from this module at L637, after the patch; `LLM.__call__` (L369-L383) and `LLM.batch` (L406-L422) both reach `_generate_one` through `ProviderLLM.generate` (L270-L288) [src]. It runs on cache hits and misses, so it sees both validation passes [F-E1, T-E2]. Cache-hit status is `self._load_cache(self._cache_path(prompt, system_prompt, kwargs)) is not None`, read before calling through (L255-L258). |
| H2 | `text_classification.llm.LLM.batch(self, prompts)` (L406-L422) | records the SHA-256 of each prompt after the same truncation (`self._truncate`, L356-L367) in a thread-local list, then calls the original. `batch` runs `_generate_one` on pool threads, so thread adjacency does not hold for it; the hashes carry the join. |
| H3 | `text_classification.data.load_dataset_splits_3way` and `load_dataset_splits` (`data/api.py` L76-L153; bound by `__main__` at `inner_loop.py` L519, called at L614-L634) | the wrapper returns the same values with the evaluator wrapped. Every scoring site calls `check_answer(pred, ex["target"], **_get_eval_kwargs(ex))` (L226, L353, L446), so the wrapper receives `pred`, `target` and the keyword `raw_question` [src; F-E1]. |
| Run | `runpy.run_module("text_classification.inner_loop", run_name="__main__", alter_sys=True)` with `sys.argv = ["text_classification.inner_loop", *rest]` | functions defined in `inner_loop.py` are re-created in `__main__`, so they cannot be patched; the seams above live in other modules and can [rep CM-V finding 4; F-E1]. |

`text_classification.llm.litellm_completion` (L15, L220) is deliberately not patched: the gateway already records request, response, usage and timing for every miss, and leaving the completion call untouched keeps the worker's network path exactly upstream's.

Behaviour:

- Rows as in 2.8, in order `meta`, `splits`, then `call` and `eval` rows as they happen, then `done`. Each `eval` row carries `question_sha256`, `prediction_sha256` and `target_sha256`: the SHA-256 of the UTF-8 bytes of the whole `raw_question` keyword, of `str(pred)` and of `str(target)`, taken before the `--max-text-chars` cut.
- Example identity: `sha256(raw_question + "\0" + target)`, looked up in an index `key -> [(split, index), ...]` built from the lists the loader returned. Among several matches the wanted split wins (`--val-output` makes `val` wanted, `--test-output` makes `test` wanted); one Symptom2Disease question is in both the validation and the test split, which this rule resolves [F-E2]. If a key still maps to several indices of one split they are assigned in rotation (the k-th call takes the k-th index) and the rows are marked `ambiguous`.
- The recording evaluator calls the real one, normalises its result as `inner_loop._unpack_eval_result` does (L51-L60: a dict gives `was_correct` and `metrics`, anything else gives `bool(raw)` and `{}`), drains the thread-local lists and writes the `eval` row. It returns the real evaluator's return value unchanged.
- Call-to-example association is by thread: `_generate_one` appends its `seq` to a thread-local list; the evaluator wrapper, which runs on the same thread right after `memory.predict` (L440-L446), takes the list.
- `SystemExit` from the worker (including upstream's `exit(0)` at L588) and any exception are passed through after a `done` row; the exit code is upstream's.

Invariants: the wrappers return exactly what the wrapped callables return and re-raise exactly what they raise; any exception inside the recording code is swallowed and counted in `capture_errors` (capture must not fail a job; a sidecar that cannot be written costs one line on stderr); a missing patch target sets `hooks.<name>` to false and the job still runs; the hooks add no model call and change no argument.

Never: stores prompt or reply text in the sidecar; patches anything in `inner_loop` or the completion call; imports a third-party package itself.

### 3.16 `journal.py` (P), `bundle.py`, `executors/__init__.py`

```python
# journal.py   (P: operation-journal pattern from H2 runtime_v4.py L42-L111)
class Journal:
    def __init__(self, path: Path, *, clock: Clock | None = None) -> None       # reads the file; continues seq
    def record(self, op: str, key: str, status: str, *, attempt: int = 0, detail: Mapping | None = None) -> dict
    def recover(self) -> list[dict]                 # appends "interrupted" for every started-without-terminal; idempotent; returns them
    def last(self, op: str, key: str, *, attempt: int | None = None) -> dict | None
    def attempts(self, op: str, key: str) -> list[dict]      # one summary per attempt that reached "started": {"attempt", "seq", "status", "detail", "started_detail"}
    def counted_attempts(self, job_key: str, candidate_sha256: str) -> list[dict]
        # the job attempts that count against a candidate budget: started after the latest ("job", job_key, "reset") entry
        # and with this candidate_sha256 in their started detail
    def next_attempt(self, op: str, key: str) -> int         # 1 + highest attempt ever started; an attempt number is never reused
    def entries(self, op: str | None = None, *, since_seq: int = 0) -> list[dict]
    def session_start_seq(self) -> int              # seq of the latest ("run", *, "started") entry; 0 when none
    def open_handles(self) -> list[dict]            # detail.handle of job and import_check attempts started and not terminal
```

Difference from H2: H2 refused to relaunch an operation whose state was uncertain. Here a relaunched job cannot pay twice for a finished call, because the gateway replays it (both reuse scopes), so an interrupted job is relaunched while attempts remain. Never: rewrites or truncates the file; stores payloads (only hashes, counters and short text).

```python
# bundle.py
MAX_AGENT_FILE_BYTES = 262_144
MAX_AGENT_FILES = 400
OUTPUT_ALLOW = ("val.json", "test.json", "memory.json", "log.jsonl", "sidecar.jsonl")
RUN_ALLOW = ("stdout.txt", "stderr.txt", "exit.json", "stub.txt", "selfcheck.json")
VOCAB_FILE = "fb374d419588a4632f3f557e76b4b70aebbca790"     # the tokenizer vocabulary file LiteLLM's Harmony encoding needs (2.6 explains the name)
EXTRACT_SNIPPET: str        # python -c source: safe-extract <box>/bundle.tar into <box>, create in/ out/ run/ cache/ home/ tmp/
PACK_SNIPPET: str           # python -c source: tar OUTPUT_ALLOW from out/ and RUN_ALLOW from run/ (and cache/ on request) into <box>/collect.tar
WHEELHOUSE_SNIPPET: str     # python -c source: safe-extract <box>/wheelhouse.tar into <box>/wheelhouse (members: wheels/*.whl, SHA256SUMS,
                            # requirements.lock.txt only), run `sha256sum -c SHA256SUMS` there, print the SHA-256 of SHA256SUMS; exit 0 only if all hold

def build_bundle(example_dir: Path, ws: Path, dataset: str, manifest: dict, dest: Path, *,
                 agent_overrides: Mapping[str, Path] | None = None) -> dict
    # writes dest (tar); returns {"sha256", "bytes", "members": {path: sha256}, "overridden": [name, ...]}
    # agent_overrides: candidate name -> file whose bytes are bundled as agents/<name>.py instead of the workspace file
def extract_bundle(tar_path: Path, box: Path) -> None                    # host-side safe extraction (same checks as EXTRACT_SNIPPET)
def worker_command(python: str, box: str, inner_argv: Sequence[str], *, capture: bool, max_text_chars: int) -> list[str]
def build_job_file(job: JobIdentity, *, python: str, box: str, inner_argv: Sequence[str], env: Mapping[str, str],
                   route: int, timeout_s: int, expect_outputs: Sequence[str], manifest: dict,
                   vocab_file: str | None = VOCAB_FILE, expect_freeze_sha256: str | None = None) -> dict   # the job file of 2.6
    # repeat and sample come from the JobIdentity; probe_endpoints from sandbox.object_store_endpoint; expect from sandbox.expect
    # and expect_freeze_sha256 (the freeze hash recorded at qualification, None until one exists)
def build_check_file(*, python: str, box: str, env: Mapping[str, str], manifest: dict,
                     vocab_file: str | None = VOCAB_FILE, expect_freeze_sha256: str | None = None) -> dict
    # the job file for an import check: job_id "import_check", empty argv and expect_outputs, the worker's env and cwd;
    # it needs no JobIdentity, and the stub is never started with it
def collect_outputs(source: Path | bytes, attempt_dir: Path, *, max_file_bytes: int = 268_435_456,
                    max_total_bytes: int = 536_870_912) -> dict[str, str]          # name -> sha256 of files now under out/
def kill_group(pid: int, *, term_wait_s: float = 10.0) -> None           # SIGTERM to the group, wait, SIGKILL
def result_is_usable(path: Path) -> bool                                 # the test of benchmark._has_usable_result, L171-L176
```

- **Bundle.** One deterministic tar (sorted members, mtime 0, uid and gid 0, modes 0644) with exactly: `pkg/text_classification/{__init__.py, inner_loop.py, llm.py, memory_system.py, config.yaml}`; `pkg/text_classification/data/{__init__.py, api.py, constants.py, evaluators.py, loaders.py}`; `pkg/text_classification/data/<dataset_dirs[dataset]>/*.jsonl`; `pkg/text_classification/agents/*.py`; `bin/{mhf_frames.py, mhf_relay_stub.py, mhf_selfcheck.py, mhf_capture_entry.py}`. Pinned files are taken from `EX` (hash-checked against `pristine.json`), not from `WS`, so a proposer that edits `inner_loop.py`, `llm.py`, `data/` or `config.yaml` in the workspace cannot change an evaluation. Only non-pinned `agents/*.py` come from `WS`, within the two limits above: regular files only, no symlinks, nothing from `__pycache__` and no other entry of `agents/`. For a name in `agent_overrides` the bytes of the given file are bundled instead of the workspace file (3.18 uses this to run the stored copy of a candidate when the workspace file changed after it was proposed). Nothing else: no `benchmark.py`, no `meta_harness.py`, no `claude_wrapper.py`, no `logs/`, no other dataset. The same inputs give the same bytes, so `sha256` is stable across attempts.
- **Work tree** (`<box>` is `sandbox.work_dir` in a sandbox and `<attempt_dir>/box` locally; the layout is the same for all three executors):

  ```
  <box>/bundle.tar  job.json
  <box>/pkg/text_classification/...        PYTHONPATH=<box>/pkg ; worker cwd=<box>/pkg
  <box>/bin/mhf_*.py
  <box>/in/memory.json                     test jobs only
  <box>/out/                               val.json | test.json, memory.json, log.jsonl, sidecar.jsonl
  <box>/run/                               stdout.txt, stderr.txt, exit.json, stub.txt, stub.ready, u.sock
  <box>/cache/  <box>/home/  <box>/tmp/
  ```

  The worker's cwd does not matter to upstream: `--memory agents/<name>.py` is resolved as a module of the package (`inner_loop.py` L479-L498 [src]).
- `worker_command`: `[python, <box>/bin/mhf_capture_entry.py, "--sidecar", <box>/out/sidecar.jsonl, "--max-text-chars", N, "--", *argv]` with capture, else `[python, "-m", "text_classification.inner_loop", *argv]`; `{out}` and `{in}` in `inner_argv` are replaced by `<box>/out` and `<box>/in`.
- `collect_outputs` accepts a directory (`<box>`) or tar bytes; copies only `OUTPUT_ALLOW` names into `attempt_dir/out`, `RUN_ALLOW` names into `attempt_dir/run`, and `cache/<64 hex>.json` into `attempt_dir/cache`; rejects absolute names, `..`, links and devices; enforces the size caps (an oversize file is skipped and reported); re-hashes on the host. Nothing copied back is executed or used as a path.

```python
# executors/__init__.py
@dataclass(frozen=True)
class JobSpec:
    identity: JobIdentity
    inner_argv: tuple[str, ...]                    # with {out}/ and {in}/ placeholders, and {api_base} on Route 1
    route: int
    timeout_s: int
    inputs: tuple[tuple[str, Path], ...]           # (name under {in}, host source); test jobs: ("memory.json", <the validated copy in an attempt directory>)
    expect_outputs: tuple[str, ...]                # ("val.json",) or ("test.json",)
    capture: bool
    bundle_tar: Path
    bundle_sha256: str
    expect_freeze_sha256: str | None = None        # the qualified environment's freeze hash, for the job file's "expect" (2.6)
@dataclass
class JobResult:
    returncode: int | None
    timed_out: bool
    exit_file: dict | None                         # the exit file, or None when absent
    relay_end: str | None                          # exit | eof | error | stream_budget | deadline; None under the loopback ingress
    kill_reason: str | None                        # None | "halt" | "refusal_flood": why the executor killed the worker
    started_utc: str; ended_utc: str
    outputs: dict[str, str]                        # name -> sha256 of files under attempt_dir/out
    gateway: dict                                  # from HandlesRequests.close_job
    refusal_codes: dict[str, int]                  # merged: gateway["refusal_codes"] + exit_file["local_refusals"]
                                                   # + {"relay_overloaded": <session flood_refusals>}
    stderr_tail: str                               # at most 2,000 characters
    host_sleep_s: float                            # total host sleep the wait loop saw during the attempt (0.0 when none)
    facts: dict                                    # executor-specific; written to executor.json (includes "environment" and "setup_seconds")
@dataclass(frozen=True)
class ImportResult:
    ok: bool; returncode: int | None; timed_out: bool; stdout_tail: str; stderr_tail: str
    reason: str | None = None                      # None | "foreign_class" (exit 3) | "no_class" (exit 4): the loaded-class assertion of 3.13

class Executor(Protocol):
    name: str
    isolated: bool
    def run(self, spec: JobSpec, attempt_dir: Path, on_handle: Callable[[dict], None]) -> JobResult: ...
    def import_check(self, names: Sequence[str], bundle_tar: Path, work_root: Path, on_handle: Callable[[dict], None], *,
                     timeouts_s: Sequence[float]) -> dict[str, ImportResult]: ...
    def cleanup_orphans(self, handles: Sequence[dict]) -> int: ...
    def close(self) -> None: ...

TAINT_CODES = frozenset({"halted", "budget_exhausted", "provider_unavailable", "gateway_internal", "relay_unavailable"})
def classify_attempt(result: JobResult, *, result_usable: bool, halted: bool) -> tuple[str, bool, str | None]
    # pure: (outcome, infra, failure_class)
def check_selfcheck(report: Mapping, *, isolated: bool) -> tuple[list[str], list[str]]
    # pure: (isolation problems, environment problems)
def sandbox_cost_micro(billable_ms: int, cpu: str, memory: str, manifest: dict) -> int
    # pure: ceil(ms / 3.6e6 * (cpu * sandbox.price_cpu_hour + GiB(memory) * sandbox.price_gib_hour)) in micro-dollars;
    # lives here so that the sandbox executor and the forecast (parallel owners in layer 3) can both use it
```

Contract for all executors:

- `run` never raises for a job-level problem; it returns a `JobResult`. It raises `CampaignStop` only for `isolation` and `budget:sandbox`, raises `ExecutorError` for infrastructure it could not get past (create, upload, environment setup, an environment that does not prove itself, authentication, billing), and lets `KeyboardInterrupt` through.
- `on_handle` is called as soon as a sandbox id (`{"sandbox_id": ...}`) or a process group (`{"pgid": ..., "t_start": ...}`) exists, before anything else, so the journal knows what to clean up after a crash. This holds for `import_check` as well as for `run`.
- The executor registers the job with the gateway before the worker starts and closes it in a `finally` after the worker has ended; `JobResult.gateway` is the dict from `close_job` (which waits for the job's provider attempts in flight, 3.10).
- **Wait loop.** While the worker runs, the executor checks once a second: the exit condition of its mode; `gateway.kill_requested(job_id)` (kill the worker at once, `kill_reason = "refusal_flood"`); `gateway.halted()`; and the **host-sleep heartbeat**: it keeps the wall-clock and the monotonic reading of the previous beat and adds `util.sleep_gap(wall delta, monotonic delta)` to `host_sleep_s` whenever that gap exceeds `runner.sleep_gap_s`. A sandbox keeps running and its own clock keeps counting while the laptop sleeps, so a job that then ends at its limit, loses its relay or misses its deadline was interrupted by the host, not timed out by the candidate [r2]. The executor's own deadlines are counted on the monotonic clock. A stop request (a signal) is not looked at here: it is honoured at an iteration boundary only (3.20); a halt is. Once a halt is set it gives the worker at most 60 s to end by itself, then kills it (`kill_reason = "halt"`). Without this a job ran on after a budget halt until its own 7200 s limit, with every call refused. The kill is `bundle.kill_group` locally, a `KILL` frame under the relay, and a unary `exec` of `kill` on the stub's recorded process group in a sandbox when no bridge is alive.
- A missing exit file is a failure whatever any exec returned (`cwsandbox` before 1.19.6 could report 0 for an interrupted command [rep SB-V 1.5]).
- `import_check` runs upstream's import line and the loaded-class assertion through `mhf_selfcheck.py --import-check` in the executor, once per name and in the given order, with the worker's environment (the job file of `bundle.build_check_file`) and no gateway variables, each under its own limit from `timeouts_s`. `ok` requires exit 0 and `OK` in stdout, as upstream at L208; exit 3 and exit 4 set `reason` (`foreign_class`, `no_class`). It raises `ExecutorError` when the executor itself could not be brought up.

`classify_attempt`, first match wins. `codes` is `result.refusal_codes`; `taint = set(codes) & TAINT_CODES`; `exit` is `result.exit_file or {}`; `relay_gap` is true when `result.relay_end` is not `None`, `exit` or `stream_budget`, or the exit reason is `uplink_lost`, or the exit reason is `exit` and `exit["unanswered_at_exit"] > 0` (the worker ended by itself while the host still owed it replies). Rows 1 to 6 apply whatever the return code and the result file say. `slept` is `result.host_sleep_s > 0`.

| # | Condition | Outcome | `infra` | `failure_class` |
|---|---|---|---|---|
| 1 | `halted`, and `taint` has `halted` or `budget_exhausted`, or `kill_reason == "halt"` | `halted` | true | null |
| 2 | `relay_end == "stream_budget"` | `request_ceiling` | false | `stream_budget` |
| 3 | `relay_gap` | `relay_lost` | true | null |
| 4 | `taint` is not empty | `provider_unavailable` | true | null |
| 5 | `codes` has `job_request_ceiling` | `request_ceiling` | false | `request_ceiling` |
| 6 | `codes` has `relay_overloaded`, or `kill_reason == "refusal_flood"` | `request_ceiling` | false | `relay_overloaded` or `refusal_flood` |
| 7 | return code 0, exit file present with reason `exit`, `result_usable` | `ok` | false | null |
| 8 | `slept`, and any of: `timed_out`, exit reason `timeout`, no exit file | `interrupted` | true | `host_sleep` |
| 9 | `timed_out`, or exit reason `timeout` | `timeout` | false | null |
| 10 | no exit file | `executor_error` | true | null |
| 11 | return code 0 | `no_result` | false | null |
| 12 | anything else | `worker_error` | false | `context_limit` if `codes` has `context_length_exceeded`; else from `stderr_tail`: `dependency_missing` (`ModuleNotFoundError`, `No module named`), `import_error` (`ImportError`), `harmony_vocab` (`HarmonyError`), `worker_exception` (`Traceback`), else `unknown` |

Why this order. In the first draft the success row came first, so a model-written candidate that wraps `call_llm` in `try/except` could turn `halted`, `budget_exhausted`, `provider_unavailable` or `gateway_internal` refusals into wrong answers, exit 0 and be scored; after a halt the record said `succeeded`, and the next start reused and published it. A timeout came before the provider-unavailable row, so a job that timed out because the provider was down counted against the candidate. `relay_unavailable` is issued by the stub, which the host gateway never sees, so it could not match at all; and a worker that gave up after reattach was exhausted exited 1 by itself and fell through to `worker_error` [A-M, by reading; upstream's own loop does not swallow call errors, `inner_loop.py` L231-L234, L340-L345, L461-L464, so the two baselines were never affected]. Now any tainted attempt is an infrastructure attempt whatever it wrote, and the stub's own counters reach the classification through `refusal_codes` and the exit file.

Rows 2, 5 and 6 are the job's own ceilings (stream volume, request count or spend, live requests, a refusal loop). They are candidate-attributable and never retried. `relay_overloaded` is not an infrastructure outcome, although the review proposed that: with the `CAN` rule of 3.12 only requests whose client is still waiting count, a well-behaved worker has at most 64 of them, and an infrastructure classification would let a candidate that opens hundreds of connections use up the infrastructure attempts and stop the campaign.

A timeout during which the provider was merely slow, with no refusal issued, is still the candidate's (row 9): upstream's 7200 s limit also applies however slow the provider is. A timeout during which the host slept is not (row 8); a relay lost to a sleep is already an infrastructure attempt (row 3). `env_setup_failed` is not a row of this table: it is the outcome `JobRunner` gives an attempt whose executor raised `ExecutorError("env_setup")` before any worker existed (3.18).

`check_selfcheck`: isolation problems (only when `isolated`): any `credential_names`, `wandb_api_key == "set"`, `egress.tcp == "open"` or `egress.dns == "open"` (the entries of `egress.probes` are recorded and never a problem). Environment problems: `vocab_file_present` false, `cache_writable` false, and every entry of `expect_problems` (machine, Python version, freeze hash).

### 3.17 Executors: `local.py`, `local_relay.py`, `sandbox_client.py`, `sandbox.py`

```python
class LocalExecutor:             # name = "local"; isolated = False; development and offline tests only
    def __init__(self, manifest: dict, paths: StatePaths, *, gateway: HandlesRequests, ingress: LoopbackIngress,
                 tokenizer_dir: Path | None = None, command_prefix: Sequence[str] = (),
                 clock: Clock | None = None, ids: IdSource | None = None) -> None
class LocalRelayExecutor:        # name = "local_relay"; isolated = False; proves the relay path offline
    def __init__(self, manifest: dict, paths: StatePaths, *, gateway: HandlesRequests, mode: str = "stdio",
                 tokenizer_dir: Path | None = None, command_prefix: Sequence[str] = (),
                 clock: Clock | None = None, ids: IdSource | None = None) -> None
```

`tokenizer_dir`, when given, is used as `TIKTOKEN_RS_CACHE_DIR` as it is, the job file's `vocab_file` is `null` and the selfcheck skips that check; tests against `fake_pkg` (which has no LiteLLM) pass a temp directory. Without it the directory is `procenv.find_tokenizer_dir(upstream.worker_python, bundle.VOCAB_FILE)`. The interpreter is `upstream.worker_python` made absolute by the path rule of 2.3 and never resolved. `command_prefix` is put in front of the command that starts the worker (`LocalExecutor`) or the stub (`LocalRelayExecutor`); the offline smoke uses it to run workers under a loopback-only network profile where the platform has one (5.3), and nothing else sets it.

**`LocalExecutor.run`.** Extract the bundle into `<attempt_dir>/box`; copy inputs into `box/in`. On Route 2 draw a token with `ids.token()`; `gateway.register_job(job, token=token)`; `endpoint = ingress.open(job, token)`. Environment: `procenv.worker_env(path="<venv root>/bin:/usr/bin:/bin", home=box/home, tmp=box/tmp, pythonpath=box/pkg, cache_dir=box/cache, tokenizer_dir=<the tokenizer directory>)` plus `procenv.route_env(route, base_url=endpoint.base_url, api_key=token)`; on Route 1 `{api_base}` in the argv becomes `endpoint.base_url`. Run `mhf_selfcheck.py` (no egress probe) into `run/selfcheck.json` and refuse to start on an image problem. Start the worker (`cwd=box/pkg`, `stdin=DEVNULL`, output to `run/stdout.txt` and `run/stderr.txt`, `start_new_session=True`); `on_handle({"pgid": pid, "t_start": ...})`; run the wait loop of 3.16 up to `timeout_s`, then `kill_group`; write `run/exit.json` itself (without the two stub counters). In a `finally`: `ingress.close`, `gateway.close_job`. `collect_outputs(box, attempt_dir)`. `refusal_codes` is the gateway's. The copy of the job file kept as `jobfile.json` never holds the token.

**`LocalRelayExecutor.run`.** The same work tree and job file; `gateway.register_job(job)` with no token; no gateway listener is opened on the host. The stub itself does listen on `127.0.0.1` on the host here (in a sandbox it listens inside the sandbox), so the executor draws a bearer with `ids.token()` and passes it to the stub as `MHF_STUB_BEARER` in the stub's environment (`procenv.passthrough_env` plus that one variable; never in the job file, the argv or a record): on Route 2 the stub then refuses any request that does not carry it (3.13). `mode="stdio"`: start `[*command_prefix, python, box/bin/mhf_relay_stub.py, "serve", "--job", box/job.json, "--uplink", "stdio"]` with `stdin=PIPE`, `stdout=PIPE`, `stderr` to `run/stub.txt`, `bufsize=0`, `start_new_session=True`; wrap it in `PipeChannel(mimic_sdk=True)`; `RelaySession.attach`; run the wait loop: `wait_exit(1.0)` until `EXIT`, a dead bridge (including one that missed its pongs), the deadline (`timeout_s + 60`), `bridge.chars_in` past `relay.max_stream_mb` or `session.total_chars_in()` past `relay.max_job_stream_mb` (both `relay_end = "stream_budget"`). `mode="daemon"`: the Unix socket lives at `<tempfile.mkdtemp(prefix="mhf-", dir="/tmp")>/u.sock` (state-directory paths exceed the `AF_UNIX` limit [L1]); start `serve --uplink unix:<sock> --daemonize`, then a `bridge` subprocess per attach; rotate the bridge when `chars_in` passes `relay.rotate_after_mb` (`session.note_rotation()`); on a dead bridge reattach with a jittered wait from `relay.reattach_backoff_s`, at most `relay.reattach_attempts` times in a row; with reattach exhausted, kill the stub's group and return `relay_end = "error"`. In a `finally`: `session.close()`, kill the stub's group if it is still alive, remove the socket directory, `gateway.close_job`. `collect_outputs(box, attempt_dir)`. `refusal_codes` merges the gateway's codes, the exit file's `local_refusals` and the session's `flood_refusals` (as `relay_overloaded`).

Both run `import_check` as `[python, box/bin/mhf_selfcheck.py, "--job", <check file>, "--import-check", <name>]`, one process per name, each with its limit from `timeouts_s`, with `on_handle({"pgid": ..., "t_start": ...})` for each. This executes candidate code on the host, which is why both report `isolated = False`, every record they write carries `"isolated": false`, `status` prints `NOT ISOLATED` beside them, and live commands refuse them unless the manifest says otherwise. `cleanup_orphans` kills a recorded process group only after checking that the process start time still matches the handle.

```python
# executors/sandbox_client.py   (U)
@dataclass(frozen=True)
class SandboxSpec:
    image: str; fallback_image: str | None; cpu: str; memory: str; max_lifetime_s: int; tags: tuple[str, ...]
    env: dict[str, str]; deny_egress: bool; dns_egress: str; working_dir: str; entity: str; project: str
    # no mount field exists: no mode mounts a scratch volume or a snapshot (a sandbox with either gets an object-store egress rule
    # under every network posture, deny_egress included [r2])
@dataclass(frozen=True)
class ExecResult:
    returncode: int | None; stdout: str; stderr: str; timed_out: bool
class ExecStream(Protocol):                        # a relay.FrameChannel plus a result
    def read_text(self) -> str | None: ...
    def write(self, data: bytes) -> None: ...      # ChannelClosed when the write does not return within relay.pong_timeout_s
    def close(self) -> None: ...
    def result(self, timeout_s: float) -> ExecResult: ...
class SandboxHandle(Protocol):
    id: str
    def wait_running(self, timeout_s: float) -> None: ...
    def exec(self, argv: Sequence[str], *, cwd: str | None, timeout_s: float) -> ExecResult: ...
    def exec_stream(self, argv: Sequence[str], *, cwd: str | None, timeout_s: float) -> ExecStream: ...
    def write_file(self, path: str, data: bytes, *, timeout_s: float) -> None: ...
    def read_file(self, path: str, *, timeout_s: float, max_bytes: int) -> bytes: ...
    def stop(self, *, timeout_s: float) -> None: ...
class SandboxClient(Protocol):
    def create(self, spec: SandboxSpec) -> SandboxHandle: ...
    def list_ids(self, tags: Sequence[str]) -> list[str]: ...
    def stop_id(self, sandbox_id: str) -> None: ...
class QuotaExceeded(ExecutorError): ...            # kind = "quota"; retryable: occupancy or create throttling
class CwSandboxClient:                             # the only code that imports cwsandbox; imports inside methods
    def __init__(self, manifest: dict, *, environ: MutableMapping[str, str],
                 scrub: Callable[[str], bool] | None = None,
                 clock: Clock | None = None) -> None
    key_facts: dict                                # {"name", "len", "sha256", "scrubbed": bool | None}; never the value
```

**Entity and project are explicit on every call.** Every SDK call that takes them gets the manifest's entity and project, through the auth object: a sandbox create with no project creates one named `sandbox`, and a create that is then refused for quota has already created it [r2].

**Authentication.** `WandbAuth(entity, project)` takes no key and no variable name. It imports `wandb` and follows wandb's own chain: `WANDB_API_KEY`, then the netrc file, then settings, then an interactive prompt that saves to the netrc file; an empty `WANDB_API_KEY` falls through to the netrc file (`cwsandbox` `_auth.py` L126-L189; wandb 0.31.0 `wbauth/authenticate.py` L171-L260 [A-S, read]). With any other variable name, or an empty value, the first draft's "presence check" would have passed and sandboxes would silently have been created under the personal key in `~/.netrc`, the read F14 forbids. So, by `sandbox.auth_mode`:

- `headers` (default). `__init__` reads `environ[sandbox.auth_env]` once from the key mapping it is given (missing or empty: `ExecutorError("auth", retryable=False)`; there is no fallback to any SDK default), records `key_facts`, calls `scrub(name)` when given and deletes the name from the mapping, exactly as the provider client does (3.9). Every SDK call gets `auth=cwsandbox.AuthHeaders(headers={"x-wandb-api-key": <key>, "x-wandb-sdk-version": "0.31.0", "x-entity-id": <entity>, "x-project-name": <project>}, strategy="wandb_api_key")`. These are the headers `WandbAuth` itself builds (`_auth.py` L167-L195), and the SDK uses an `AuthHeaders` object as it is (L262-L263) [read while revising; whether the service accepts a hand-built set is live: PF3]. The SDK then never imports `wandb` and never consults the environment or a netrc file.
- `wandb_auth` (fallback, only if PF3 rejects the header set). `__init__` reads the key from the key mapping exactly as in mode `headers` (missing or empty: `ExecutorError("auth", retryable=False)`; with `--key-source env` the variable is scrubbed and deleted). It then sets `WANDB_API_KEY` in its own process to that key, because `WandbAuth` takes no key argument; a variable set after start is not in the start-up environment block [r2], and no child inherits it (every child gets an explicit environment). `NETRC` already points at the empty state file (3.27), so there is no netrc fall-through, and a key is always present, so there is no prompt. Every SDK call gets `auth=WandbAuth(entity=<entity>, project=<project>)`. The runner gates require `WANDB_BASE_URL`, `WANDB_IDENTITY_TOKEN_FILE`, `WANDB_ENTITY` and `WANDB_PROJECT` to be absent from the environment (2.3). `runtime.json` records the mode.

`CwSandboxClient` mapping, from the sandboxes report (SDK 1.27.0) [rep]; all of it is UNVERIFIED until the preflight. `A` is the auth object of the mode in use.

| Protocol call | SDK call |
|---|---|
| `create(spec)` | `Sandbox.run(container_image=spec.image, auth=A, max_lifetime_seconds=..., tags=[...], resources={"cpu": ..., "memory": ...}, network=NetworkOptions(deny_egress=True, dns_egress="deny"), working_dir=..., environment_variables=spec.env)` with no positional argument (positional arguments are the command, not the image), no `Session` (its metrics would advance a W&B run's step) and no mount of any kind. When the create fails because the image could not be pulled and `spec.fallback_image` is set, the same call is made once with that reference (the same digest on another registry host) |
| `wait_running(t)` | `sandbox.wait(timeout=t)` |
| `exec(argv, cwd, t)` | `sandbox.exec(argv, cwd=cwd, timeout_seconds=t).result(timeout=t + 30)` |
| `exec_stream(argv, cwd, t)` | `sandbox.exec(argv, cwd=cwd, timeout_seconds=t, stdin=True)`; `read_text` takes the next item of `process.stdout` (iterated synchronously in a thread); `write` calls `process.stdin.write(data).result(timeout=relay.pong_timeout_s)` and raises `ChannelClosed` on a timeout (a write into a stalled stream would otherwise block the writer for ever); `close` calls `process.stdin.close()` |
| `write_file`, `read_file` | `sandbox.write_file(path, data, timeout_seconds=t).result()`, `sandbox.read_file(path, timeout_seconds=t).result()` |
| `stop(t)` | `sandbox.stop(missing_ok=True).result()`, retried once after 20 s on `UNAVAILABLE` |
| `list_ids(tags)` | `Sandbox.list(tags=list(tags), auth=A).result()` |
| `stop_id(id)` | `Sandbox.from_id(id, auth=A)` then `stop` |

The adapter sets `CWSANDBOX_DISABLE_SIGNAL_HANDLERS=1` in its own process before the first import (the runner owns signals), never imports `wandb.sandbox`, never passes a key in an exec argument or a sandbox variable, and maps SDK exceptions to `ExecutorError` by class name and by the error's **reason** field, not by its message [r2]:

| Error | Mapped to | What the runner does |
|---|---|---|
| reason `CWSANDBOX_OCCUPANCY_QUOTA_EXCEEDED`, or a create-throttle reason | `QuotaExceeded` (kind `quota`, retryable) | waits and creates again (step 2) |
| status `RESOURCE_EXHAUSTED` with **no** reason | `ExecutorError("billing", retryable=False)` | terminal: it is the account's billing stop, not occupancy; the campaign stops (3.18) |
| status `PERMISSION_DENIED` | `ExecutorError("permission", retryable=False)` | terminal: sandboxes are not enabled for the organisation, or the entity is wrong |
| an authentication error | `ExecutorError("auth", retryable=False)` | terminal |
| an image pull failure after the fallback reference was tried | `ExecutorError("create", retryable=True)` | an infrastructure attempt |
| anything else during create | `ExecutorError("create", retryable=True)` | an infrastructure attempt |

```python
# executors/sandbox.py
class SandboxExecutor:           # name = "sandbox"; isolated = True; the call sequence is tested against tests/fake_sandbox.py
    def __init__(self, manifest: dict, paths: StatePaths, *, client: SandboxClient, gateway: HandlesRequests,
                 ledger: Ledger, clock: Clock | None = None) -> None
def sandbox_tags(job: JobIdentity) -> tuple[str, ...]          # pure; 5 tags, each at most 63 characters
```

`sandbox_cost_micro` is imported from `executors/__init__.py` (3.16).

Call sequence for one job attempt:

1. **Budget.** `L = timeout_s + sandbox.lifetime_margin_s`, plus `sandbox.setup_allowance_s` in mode `wheelhouse` (7,200 + 900 + 150 = 8,250 s with the draft values). `ledger.check_meter_cap("sandbox_seconds", extra_micro=<price of L>)`; `MeterCapExceeded` sets the halt `budget:sandbox` and raises `CampaignStop("budget:sandbox")`. The cap is a reservation: what is spent plus the requested lifetimes of all live sandboxes must fit before a create. It rests on the platform ending a sandbox at its lifetime, which PF6 proves once with a sandbox that is left alone.
2. **Create.** `client.create(SandboxSpec(image=<sandbox.image in mode image, sandbox.base_image in mode wheelhouse>, fallback_image=<sandbox.base_image_fallback in mode wheelhouse, else None>, cpu, memory, max_lifetime_s=L, tags=sandbox_tags(job), env={"WANDB_API_KEY": ""}, deny_egress=True, dns_egress="deny", working_dir=sandbox.work_dir, entity, project))`; `on_handle({"sandbox_id": id})` at once; then `ledger.meter_open("sandbox_seconds", <sandbox id>, L * 1000, "ms", worst_micro=sandbox_cost_micro(L * 1000, ...), detail={"purpose": "job", "job_id": job_id})`. From here on the sandbox counts at its whole lifetime until it is closed with the measured time. Tags: `mhf`, `mhf-c-<campaign>`, `mhf-j-<first 16 hex of sha256(job_key)>`, `mhf-a-<attempt>`, `mhf-p-<phase>`. The empty `WANDB_API_KEY` suppresses the injected identity token on the primary container [rep SB-V 1.1, live]. `QuotaExceeded`: wait 5 s, 10 s, 20 s, then `ExecutorError("quota", retryable=True)`. A non-retryable `ExecutorError` (`billing`, `permission`, `auth`) is raised as it is.
3. `handle.wait_running(sandbox.create_wait_s)`. On failure or a stuck create: stop that sandbox and `meter_close` its row with the time since it was opened; create once more (a new sandbox id, a new `on_handle`, a new meter row); then `ExecutorError("create", retryable=True)`.
4. **Upload and environment.** `write_file(<box>/bundle.tar)`, `write_file(<box>/job.json)`, for a test job `write_file(<box>/in/memory.json)`; `exec([python, "-c", EXTRACT_SNIPPET, <box>], timeout_s=120)`. In mode `image` the packages are in the image and nothing more happens here. In mode `wheelhouse` the sandbox is a stock Python image, so the environment is made now, and timed (`setup_seconds`):
   1. `write_file(<box>/wheelhouse.tar, <the tar's bytes>, timeout_s=900)`. `write_file`, not a streamed write: the streamed call waits for an end of input that can hang, and streamed writes are not atomic [r2].
   2. `exec(["sh", "-c", "wc -c < <box>/wheelhouse.tar"], timeout_s=30)` must print `sandbox.wheelhouse.tar_bytes` (a short upload is caught before anything is unpacked).
   3. `exec([python, "-c", WHEELHOUSE_SNIPPET, <box>], timeout_s=120)`: extract the tar into `<box>/wheelhouse/` with the same member checks as the bundle, then run `sha256sum -c SHA256SUMS` there; the snippet also prints the SHA-256 of `SHA256SUMS`, which must equal `sandbox.wheelhouse.sha256sums_sha256`.
   4. `exec([python, "-m", "pip", "install", "--no-index", "--find-links", "<box>/wheelhouse/wheels", "--require-hashes", "--no-deps", "-r", "<box>/wheelhouse/requirements.lock.txt"], timeout_s=600)` (the file pins the whole set, so no dependency is resolved).
   Any failure of 1 to 4 raises `ExecutorError("env_setup", retryable=True)`: the attempt is `env_setup_failed`, an infrastructure fault, and the next attempt starts from zero in a new sandbox (nothing is retried inside a sandbox whose state is unknown). No worker has started and no call was paid.
5. **Selfcheck and proof** (the egress probes run on every job, or on the first job of the runner invocation, per `sandbox.selfcheck`; the selfcheck itself, with the proof of machine, Python version, freeze hash and tokenizer file, runs in every sandbox in every mode, without `--probe-egress` on the jobs that skip the probes, because no worker may start in a sandbox that has not proved its environment): `exec([python, <box>/bin/mhf_selfcheck.py, "--job", <box>/job.json, "--probe-egress"], timeout_s=60)`; save the report as `run/selfcheck.json`. An isolation problem sets the halt `isolation` and raises `CampaignStop("isolation")`. An environment problem (machine, Python version, freeze hash, tokenizer file, cache directory; `check_selfcheck`, 3.16) raises before any paid call: `ExecutorError("image_mismatch", retryable=False)` in mode `image`, `ExecutorError("env_setup", retryable=True)` in mode `wheelhouse`. `TIKTOKEN_RS_CACHE_DIR` in the job file is set from the report's `tokenizer_dir` (the path depends on where LiteLLM was installed, so it is discovered inside the sandbox in every mode; `sandbox.tiktoken_cache_dir` overrides it) and `job.json` is written again; `LITELLM_LOCAL_MODEL_COST_MAP=True` is already in the job file's `env` (3.5). The environment identity goes into `executor.json` (2.6): the image digest that was used, `sandbox.wheelhouse.requirements_sha256`, `sha256sums_sha256` (mode `wheelhouse`), and the report's `freeze_sha256`, `machine` and `python`. The freeze hash a job is compared with is the one recorded at qualification (`passed.json`, 3.22); until one exists (the preflight sandbox step and the qualification job itself) it is recorded, not compared.
6. **Relay.** `gateway.register_job(job)`; `session = RelaySession(gateway, job, manifest)`.
   - `relay.mode == "daemon"`: `exec([python, <box>/bin/mhf_relay_stub.py, "serve", "--job", <box>/job.json, "--uplink", "unix:<box>/run/u.sock", "--daemonize"], timeout_s=30)`, then `stream = handle.exec_stream([python, <box>/bin/mhf_relay_stub.py, "bridge", "--uplink", "unix:<box>/run/u.sock"], timeout_s=<remaining>)` and `session.attach(stream)`. When `Bridge.chars_in` passes `relay.rotate_after_mb` (`session.note_rotation()`), or the bridge is dead (end of stream, a failed or timed-out write, eight frame errors, or no frame for `relay.pong_timeout_s`), start a new bridge exec, attach it, and close the old one's stdin; reattach failures are bounded by `relay.reattach_attempts` in a row with waits from `relay.reattach_backoff_s`.
   - `relay.mode == "stdio"`: one `exec_stream([... "serve", "--job", ..., "--uplink", "stdio"], timeout_s=timeout_s + 60)`.
   Every exec has an explicit timeout below the sandbox's remaining lifetime (the SDK's fallback is 300 s [rep]). The stdout of an exec is read only by the bridge's reader thread, which the executor can cancel (iterating the stdout of a stopped sandbox hangs [rep SB-V 1.3]). No key or token exists in the sandbox: the relay identifies the job by its channel, `OPENROUTER_API_KEY` is the placeholder `mhf-relay`, and `MHF_STUB_BEARER` is never set there.
7. **Wait**, with the wait loop of 3.16, for the `EXIT` frame up to the job deadline. The attempt ends early with `relay_end = "stream_budget"` when `session.total_chars_in()` passes `relay.max_job_stream_mb` (either mode) or a stdio bridge passes `relay.max_stream_mb`. In daemon mode with reattach exhausted the worker's calls have no uplink: the executor does **not** wait for the deadline. It kills the stub's process group with a unary `exec` (`kill` on the pid in `<box>/run/stub.ready`), reads `<box>/run/exit.json` with `read_file`, and returns `relay_end = "error"`, which 3.16 classifies as `relay_lost`. (The first draft polled the exit file until the deadline; the worker's client would have given up after six 600 s timeouts, exited 1 by itself, and the attempt would have been recorded as the candidate's `worker_error` [A-M].)
8. **Collect.** `read_file(<box>/run/exit.json)`; absence is recorded and the attempt fails. `exec([python, "-c", PACK_SNIPPET, <box>, <"cache" when sandbox.copy_back_cache>], timeout_s=120)`, `read_file(<box>/collect.tar, max_bytes=536870912)`, `collect_outputs(bytes, attempt_dir)`. `refusal_codes` merges the gateway's codes, the exit file's `local_refusals` and the session's `flood_refusals`.
9. In a `finally`: `session.close()`, `gateway.close_job`, `handle.stop()`, then `ledger.meter_close("sandbox_seconds", <sandbox id>, billable_ms, micro_usd=sandbox_cost_micro(billable_ms, ...))` from the runner's own clock between running and stopped (Forge-key users have no per-sandbox usage API [rep]). A crash before this line leaves the row open at the worst case.

`import_check(names, bundle_tar, work_root, on_handle, timeouts_s=...)`: the budget check of step 1 for a lifetime of 300 s plus `sandbox.lifetime_margin_s` (plus `sandbox.setup_allowance_s` in mode `wheelhouse`); one short-lived sandbox with the bundle and the check file of `bundle.build_check_file`, the same create arguments, `on_handle`, environment setup (step 4) and selfcheck; `meter_open` with `detail={"purpose": "import_check"}`; one `exec` per name of `mhf_selfcheck.py --import-check <name>` in the given order with its own limit; then stop and `meter_close`. A sandbox that cannot be created or checked raises `ExecutorError`.

`cleanup_orphans(handles)`: `stop_id` for every handle; then `list_ids(["mhf", "mhf-c-<campaign>"])` and stop what is left, repeated until two consecutive empty lists or 10 rounds [rep SB section 9]. Then every row of `ledger.open_meter_rows("sandbox_seconds")` is closed with `min(worst case, time since the row was opened)`: a sandbox that was stopped now is charged what it ran, and one that had already died at its lifetime is charged its whole lifetime. (At the draft prices a sandbox that ran to its 8,250 s lifetime is about $0.78 that the first draft's cap never saw [A-M].)

Never (any executor): passes a key, the Forge key variable or the runner's environment to a worker; puts a token on a command line; treats exit code 0 as success without an exit file and a usable result; treats an attempt with a tainted refusal code as a success; reuses a sandbox for a second job; iterates the stdout of a stopped sandbox; lets a sandbox run without an open meter row; mounts a scratch volume or a snapshot; starts a worker in a sandbox that has not proved its machine, Python version and package set; calls the sandbox API without the explicit entity and project.

### 3.18 `jobs.py`

```python
class UpstreamFacade(Protocol):                    # the slice of upstream that job planning needs; tests supply tests/fake_facade.py
    datasets: list[str]
    def discover(self) -> list[tuple[str, str]]                         # benchmark.discover_all_memory_systems, L39-L48
    def inner_loop_prefix(self) -> list[str]                            # benchmark._inner_loop_command(), L296-L309 (9 tokens)
    def build_val_runs(self, logs_dir: Path, systems: list[tuple[str, str]]) -> list[tuple[str, list[str]]]       # L464-L531
    def build_test_runs(self, logs_dir: Path, results_dir: Path, systems: list[tuple[str, str]]) -> list[tuple[str, list[str]]]   # L534-L608
    def run_dir(self, base: Path, dataset: str, memory: str, model_short: str) -> Path      # L139-L148
def real_facade(benchmark: ModuleType) -> UpstreamFacade

@dataclass(frozen=True)
class PlannedJob:
    desc: str                                      # upstream's own description: "val/<dataset>/<candidate>/<model_short>"
    split: str; dataset: str; candidate: str
    upstream_command: str
    inner_argv: tuple[str, ...]
    host_outputs: dict[str, Path]                  # "val.json" | "test.json" | "memory.json" | "log.jsonl" -> the path upstream named
    inputs: tuple[tuple[str, Path], ...]

def plan_jobs(facade: UpstreamFacade, memory: str, *, test: bool, logs_dir: Path, results_dir: Path,
              manifest: dict) -> tuple[list[PlannedJob], str | None, list[str]]
    # (jobs in config.yaml dataset order, upstream's "not found" message or None, datasets skipped for a missing memory.json)

@dataclass
class PhaseTracker:
    phase: str = "baseline"
    iteration: int | None = None

class JobRunner:
    def __init__(self, manifest: dict, paths: StatePaths, *, journal: Journal, ledger: Ledger, executor: Executor,
                 facade: UpstreamFacade, tracker: PhaseTracker, example_dir: Path, logs_dir: Path, results_dir: Path,
                 passthrough: Callable[[Sequence[str]], subprocess.CompletedProcess],
                 candidate_reference: Callable[[str, str | None], tuple[Path, str] | None] | None = None,
                     # (name, wanted sha256 or None) -> (stored copy, sha256): the copy with that hash, or with None the copy of
                     # the latest proposal that lists the candidate; None for a baseline or when no such copy exists
                     # (proposer.candidate_reference, bound to the state paths by the driver)
                 bundle_builder: Callable[..., dict] | None = None,      # default bundle.build_bundle, same keyword arguments
                 on_unit: Callable[[str], None] | None = None,
                 expect_freeze_sha256: Callable[[], str | None] | None = None,     # the qualified environment's freeze hash (3.22)
                 clock: Clock | None = None, rng: random.Random | None = None) -> None
    def run_benchmark(self, args: Sequence[str]) -> subprocess.CompletedProcess
    def run_job(self, planned: PlannedJob, *, phase: str, iteration: int | None, publish: bool = True,
                repeat: int | None = None, sample: int | None = None) -> dict                                    # the job record
    def import_check(self, candidates: Sequence[str], *, iteration: int) -> dict[str, ImportResult]
    def verify_published(self) -> dict             # verify_published_results for this runner's directories; journals what it did
    def halt_check(self) -> None                   # CampaignStop when the ledger is halted (a stop request is the driver's business, 3.20)

def write_launcher_log(logs_dir: Path, index: int, desc: str, upstream_command: str,
                       attempts: Sequence[tuple[bytes, int]], *, path_map: Mapping[str, str]) -> Path   # upstream's file format
def publish_outputs(attempt_out: Path, planned: PlannedJob, *, include_result: bool) -> dict[str, str]
def verify_published_results(paths: StatePaths, journal: Journal, logs_dir: Path, results_dir: Path) -> dict
    # {"quarantined": [relative path, ...], "republished": [relative path, ...]}
def resolve_test_input(paths: StatePaths, candidate: str, dataset: str) -> tuple[Path, str] | None
    # the validated memory.json of val.<candidate>.<dataset>: (file in the attempt directory that was published, sha256); None when absent
```

`real_facade` passes upstream's own values to the builders, as `benchmark.main` does at L887-L906: `benchmark.DATASETS`, `benchmark.MODELS`, and `mode`, `num_epochs`, `temperature` from `_CONFIG["inner_loop"]`, which are `main`'s argparse defaults (L797-L810) [src]. The builders return a 3-tuple; the facade returns its first element.

`plan_jobs` calls upstream's own builders, so the job list, the skip-if-usable rule (L487-L489, L559-L561), the `--force` rule (L527-L528, L604-L605), the missing-`memory.json` warning (L564-L570) and every worker argument are upstream's code, not a reimplementation [F-E2, X-E9]. It mirrors `benchmark.main` L865-L906: filter `discover()` by `Path(memory).stem`; if nothing matches return the message `Error: '<memory>' not found on disk.` (upstream then exits 0 [F-E2]). For each `(desc, cmd)`: `cmd` must start with exactly `facade.inner_loop_prefix()` (else `UpstreamContractError`); the argv after those nine tokens becomes `inner_argv` with the values of the five path flags turned into placeholders and recorded in `host_outputs` or `inputs`, and `--force` dropped. This keeps `--temperature 0.0`, the split sizes, `--seed 42`, `--mode offline` and the model string identical to upstream by construction, including the trap that a hand-built command would run gpt-oss at temperature 1.0 (`llm.py` L306-L311 [src]). On Route 1 the value after `--model` becomes `routing.route1_model` and `--api-base {api_base}` is appended. Jobs are sorted into `config.yaml` dataset order (upstream shuffles, L530). For `test`, a dataset that upstream's builder did not return and that has no usable `test.json` is reported as skipped for a missing `memory.json`.

`run_benchmark` dispatch (F8). Upstream issues exactly five argument forms [F-E2, T-E6; A-U E1]:

| `args` | Called at | Action | Return |
|---|---|---|---|
| `["--memory", n]` | L421, L519 | validation jobs for `n` (below), then `passthrough(["--results"])`, which makes the real `benchmark.py` rewrite `summary.json` as upstream does at L923 and L956 | `CompletedProcess(args, 0 if every pending job succeeded else 1, stdout=<one line per job>, stderr=<first failure line>)`; code 0 with upstream's message when the system is not found or nothing is pending |
| `["--memory", n, "--test"]` | L325 | test jobs for `n` (inputs: the validated `memory.json`, see "Test inputs"); a dataset skipped for a missing `memory.json` counts as failed | same rule |
| `["--frontier", "--model", m]`, `["--frontier", "--test", "--model", m]`, `["--results", "--test"]` | L428, L527; L330; L296, L333 | `passthrough(args)`: the real `benchmark.py` (below) | upstream's `CompletedProcess` |
| anything else | | `UpstreamContractError("unknown run_benchmark form")` | |

`passthrough` (built by the driver) runs `[<worker python>, "-P", "benchmark.py", "--logs-dir", <LOGS>, "--results-dir", <LOGS>/results, *args]` with cwd `WS`, `procenv.passthrough_env`, captured output and a 600 s limit. `<worker python>` is `upstream.worker_python` made absolute by the path rule of 2.3 (never resolved). `-P` keeps the script's directory, which is the proposer's working directory, off `sys.path`: without it a `random.py` or `yaml.py` in the workspace root runs in this child [R1; A-S h_shadow]; `benchmark.py` imports no sibling module (L3-L12), so nothing is lost. Before every passthrough call the runner calls `workspace.verify_pinned` (restoring a pinned file that changed, so the child never runs an edited `benchmark.py`) and `verify_published()`. Those branches of `benchmark.main` (L835-L863) only read result files and write `frontier_val.json`, `frontier.json` and `summary.json`, and return before any job is built [src]. The venv Python replaces upstream's `uv run` so nothing resolves or syncs an environment inside the workspace [T-E6].

**`verify_published_results`** runs before `plan_jobs` and before every passthrough call. `benchmark.load_results` reads every `val.json` under `LOGS` and every `test.json` under `LOGS/results` at any depth (`rglob`, L186) and accepts any three-part path [src], so checking only the candidate being benchmarked is not enough: a result file planted for any other name, or a changed file of an earlier candidate or of a baseline, reaches `frontier_val.json` at the next `--frontier` call. In the review's experiment a `val.json` planted for a system with no agent file became `_pareto[0]` and `best_system` for all three datasets [A-U X2]; such a name then enters upstream's test systems (`meta_harness.py` L301-L308), its test call returns 0 "not found", and finalisation can never complete (L338-L351). The function lists exactly what `load_results` reads (`logs_dir.rglob("val.json")`, `results_dir.rglob("test.json")`) and compares each file with the hash in `published.files` of the job record that owns that path:

- a file that no job record published, for any system name, at any depth, is moved to `STATE/quarantine/<utc>/...`;
- a file whose hash differs from the published one is moved to quarantine and published again from its attempt directory;
- both are journaled (`workspace: quarantine`, `workspace: republish`).

Upstream's own rule (`_has_usable_result`, L171-L176) only asks for a numeric `accuracy`.

**Which candidate bytes run.** For a candidate that came from a proposer session, the reference of a validation job is the stored copy of the latest proposal that lists the name (`candidate_reference(name, None)`, 2.11). `run_job` compares `workspace.candidate_sha256(paths, name)` with the reference hash immediately before it builds a bundle; on a difference (the proposer, or a background child of an earlier session, changed the file after it was proposed and import-checked) the bundle is built with `agent_overrides={name: <stored copy>}` and the attempt records `candidate_restored: true`. The `candidate_sha256` of an attempt is the hash of the bytes that were bundled. A baseline has no reference; its file is pinned and comes from `EX`.

**Test inputs.** A test job does not take `memory.json` from `LOGS`, which every later proposer session can write. `resolve_test_input` returns the file in the attempt directory from which `val.<candidate>.<dataset>` was published, after checking its hash against `published.files` of that job record; a `LOGS` copy that differs has already been quarantined and published again by `verify_published_results`. The hash is recorded in the test attempt's `inputs`. The candidate bytes of a test job are the bytes that were validated on that dataset: the reference is `candidate_reference(name, <candidate_sha256 of the published attempt of val.<candidate>.<dataset>>)`, and the bundle is built with `agent_overrides` whenever the workspace file has a different hash (`candidate_restored: true`). If no stored copy with that hash exists, the test job is not run and counts as failed with the message `validated candidate file not found` (a test number must belong to code that was validated). Upstream has the same weakness for both inputs [inf from the spec and `benchmark.py` L563-L586; not run]; this build closes it as it closes it for result files (V16) and pinned files (V12). Deviation V26. One limit is stated there too: other candidates' files that a candidate imports are bundled from the workspace as they are at that time, with the same per-file rule applied to each stored candidate, so a helper module that is not itself a proposed candidate is not pinned.

A `--memory` call, step by step:

1. `halt_check()`. Journal `benchmark: started`.
2. `verify_published()`.
3. `plan_jobs`. A job upstream's builder did not return (usable result present) is journaled `skipped` with status `skipped_usable`.
4. For each planned job in dataset order, `executor.parallel_dataset_jobs.validation` at a time for a validation call and `executor.parallel_dataset_jobs.test` (always 1) for a test call: if the journal has an attempt of this `job_key` whose outcome is `ok` under the table of 3.16, whose `candidate_sha256` and `inputs` equal the ones this job would run with, and whose recorded result hash equals the file in its attempt directory, reuse it (no executor run). This covers a crash between a job's end and its publication, and it is how the finalisation reuses the test jobs a calibration run already paid for (3.20). An attempt that was tainted, or that ended in any outcome other than `ok`, is never reused. Otherwise `run_job`.
5. **Publication.** With `search.partial_candidate_policy == "upstream"`: after each job, `publish_outputs` copies `memory.json` and `log.jsonl` from the last attempt whatever its outcome (upstream leaves partial files from failed attempts too, and the proposer reads that directory), and the result file only from an attempt with outcome `ok`. With `"quarantine"`: nothing of the candidate is published until every dataset has succeeded; if any failed, none of its results reach `LOGS`, upstream finds no result, scores it 0 and writes its own `failed` row (`meta_harness.py` L540 [src]). Copies are atomic, and every published path and hash goes into `published.files`. The sidecar, the call records and the worker cache are never published: the proposer is given exactly what upstream would have written (decision U9a; whether it also reads our records is monitored, 3.19).
6. Launcher logs: `LOGS/.launcher/<NN>_<sanitized desc>.log` in upstream's format (`command: <upstream_command>`, a blank line, the worker's stdout then stderr, `exit=<code>`, with upstream's `retry N` separator between attempts; `benchmark.py` L61-L62, L82-L102, L121 [src]). `NN` is the job's position in this call. `write_launcher_log` replaces the executor's path prefixes with the directories upstream named, through `path_map`: `<box>/out/` becomes the directory of the job's upstream output files and `<box>/in/` the directory upstream would have read `memory.json` from. The worker prints lines such as `Saved val results to <box>/out/val.json` [A-U E3], which would otherwise show sandbox paths to the proposer. Frames of `mhf_capture_entry.py` in a traceback are left as they are (V15 says so).
7. Journal `benchmark: complete`; `on_unit("eval:<split>.<candidate>")`; `halt_check()`.

`run_job`:

1. `attempt = journal.next_attempt("job", job_key)`. Determine the candidate bytes and their hash (above). Count the earlier attempts:
   - candidate-attributable ones (`infra` false) among `journal.counted_attempts(job_key, candidate_sha256)` against `executor.max_attempts` (1 by default; upstream: 3, `benchmark.py` L79). One attempt, because a job-limit timeout and a candidate error are deterministic at temperature 0, and with gateway replay a rerun starts where the first attempt stopped, so three attempts of 7,200 s would be one job with a 21,600 s limit [r2]. The count is **per `(job_key, candidate_sha256)`** and restarts after a `job: reset` entry. Upstream gives every `benchmark.py` call three fresh attempts; counting per `job_key` for the life of the campaign created dead ends upstream does not have: a candidate re-proposed under the same name with a fixed file was never run again on the dataset it had failed, and a test job that had used up its attempts made `evaluate --test` permanently incomplete although upstream says "Fix failures, then rerun --test" (L350). `status --reset-job JOB_KEY NOTE` (3.27) is the operator's way to give a job a fresh budget.
   - infrastructure ones started since `journal.session_start_seq()` against `executor.max_infra_attempts`.
   - Outcome `request_ceiling` is never retried: the ceilings are deterministic for the same code.
2. `halt_check()`. `workspace.verify_pinned` (restore on a difference); build the bundle (with `agent_overrides` when the rule above says so), the `JobSpec` and the attempt directory. Journal `started` with `candidate_sha256` and `inputs` (the handle is added by `on_handle`).
3. `executor.run(spec, attempt_dir, on_handle)`. An `ExecutorError` becomes an attempt with outcome `env_setup_failed` when its kind is `env_setup` and `executor_error` otherwise; both are infrastructure attempts. When the error is not retryable (`billing`, `permission`, `auth`, `image_mismatch`) the record is written and `CampaignStop("infrastructure")` is raised at once, with the kind as detail: another try would fail the same way and a billing stop must not be probed three times.
4. `classify_attempt`. **Jobs of a pinned system (a name in `workspace.PINNED_AGENTS`, that is, a baseline) are never candidate-attributable: every failed attempt except `request_ceiling` gets `infra = true`.** A baseline that cannot finish is a problem of the set-up, not a result, and if its failures counted against the candidate budget, a baseline job that timed out would stay failed while the search went on to paid proposer sessions that finalisation could then never complete (upstream requires every baseline on every dataset, L338-L351). Write `job.json` and `executor.json`; journal `complete` or `failed`; `on_unit("job:<job_id>")`.
5. `ledger.halted()` set: raise `CampaignStop(<halt reason>)` after the record is written.
6. Outcome `ok`: return. Candidate-attributable failure with attempts left: go to 1. Infrastructure failure with infrastructure attempts left: sleep `min(60 * 2**k, 900)` seconds, `k` = infrastructure attempts of this job so far in this invocation (0, 1, 2, ...), then go to 1; without the wait a ten-minute outage would use every attempt within minutes. With the draft values that is up to three infrastructure attempts (`env_setup_failed` included) and one candidate attempt. Infrastructure attempts exhausted: raise `CampaignStop("infrastructure")` (an infrastructure failure must not be recorded as the candidate's result; a later runner invocation has a fresh infrastructure budget; a provider fault that is specific to one request is turned into a candidate failure by the gateway after its second job attempt, 3.10 step 9.7, so the campaign cannot be stopped by one prompt for ever). Candidate attempts exhausted: status `failed`.

`import_check(candidates, iteration)`: builds a bundle for the first dataset and calls `executor.import_check(("no_memory", *candidates), bundle_tar, work_root, on_handle, timeouts_s=(4 * T, T, T, ...))` with `T = executor.import_check_timeout_s`. The first name is a **probe**: importing a pinned baseline in the same executor instance, with a longer limit because it also pays for the cold start (the import takes 2.4 s warm on this machine [A-U]). If `executor.import_check` raises `ExecutorError`, or the probe is not `ok`, the call is an infrastructure attempt: it is journaled `import_check: failed`, retried after the same backoff as a job, at most `executor.max_infra_attempts` times in this invocation, and then `CampaignStop("infrastructure")` is raised; **no candidate is rejected**. A candidate is rejected only when the probe passed and the candidate's own exec failed: `rejected: "loaded_class"` when its `ImportResult.reason` is `foreign_class` or `no_class` (the class upstream's loader would pick is not defined in the candidate's own module, 3.13), `rejected: "import"` when it ended with any other non-zero exit, without `OK`, or timed out. `run` already separated infrastructure from candidate failures; the first draft's import check did not, so a sandbox that could not be created crashed the runner through upstream's loop with no `stopped.json`, and a cold-start timeout was recorded as an invalid candidate, for which upstream then writes a `failed` row [A-U, by reading]. The call writes `import_checks/iter-<NNN>/a<N>/check.json` and journals `import_check` (`started`, then `complete` or `failed`).

Never: reimplements upstream's skip or argument logic; writes a job token, key or gateway address into `job.json`, the journal or a launcher log; publishes the sidecar, the worker cache or our records into `LOGS`; runs candidate code on the host outside an executor; lets a result file that no job record published stay where `load_results` reads; reuses an attempt whose outcome is not `ok`; counts an infrastructure failure, an environment-setup failure, a host sleep, or any failure of a baseline, against a candidate; reruns a candidate-attributable timeout.

### 3.19 `proposer_shim.py` and `proposer.py` (F11)

**The seam.** `claude_wrapper.run` uses the module-level name `subprocess` only at L590-L598 (`subprocess.Popen`, `subprocess.PIPE`, `subprocess.DEVNULL`) and uses the returned object through `proc.stdout` and `proc.stderr` (handed to `_enqueue_lines`, L496-L502, which calls only `readline` and `close`), `proc.poll()` (L626), `proc.kill()` (L616), `proc.wait()` (L649) and `proc.returncode` (L651) [src]. It builds its command with the module-level function `build_command` (L115-L168) [src]. So `claude_wrapper.subprocess = shim` and `claude_wrapper.build_command = <wrapper>` for the duration of one call give five things with no upstream edit and without touching the runner's own `os.environ` [T-E3, with a fake CLI]:

- **Environment.** `Popen` ignores the `env` the wrapper passes (a copy of the runner's environment, L573) and uses the allow-listed one.
- **Pinned binary and the mode's argv.** The `build_command` wrapper calls upstream's function and passes the result through `session_argv`, so the command that runs, and the command the wrapper writes into its own session log, are the exact argv of the permission mode (below), with `cmd[0]` = `proposer.cli_path`. `Popen` refuses a command whose first element is not that path (`UpstreamContractError`: the wrapper was bypassed).
- **Tee with arrival times.** Each stdout line is passed through and appended to `stream.jsonl`. The wrapper's own `events.jsonl` has no timestamps; the tee keeps reading until the pipe closes, so a `result` event emitted during shutdown is kept [T-E3].
- **Deadline on a monotonic clock, graceful timeout.** The process is started with `start_new_session=True`, so it leads its own process group. The shim counts the session's deadline on `clock.monotonic()`, which does not advance while the host sleeps (upstream uses the wall clock, so a laptop that slept kills the session on wake [r2]). The deadline stands still while the CLI reports a rate-limit wait: for every stdout line that is a `system` event with subtype `api_retry` whose `error` is `rate_limit` or whose `error_status` is 429, the deadline moves later by `retry_delay_ms` (`retry_pause_s`); the total extension is capped at the session timeout itself, and is recorded as `paused_s`. At the deadline the shim sends SIGINT to the process group, SIGTERM after `grace_s[0]`, SIGKILL after `grace_s[1]`. The group covers Bash children, which upstream's `proc.kill()` leaves behind. The wrapper's own wall-clock deadline is switched off (`timeout_seconds=None`, the wrapper's default), so only the shim ends a session.
- **Group reaping at every exit.** When the CLI process has ended, by any path (a normal exit included), `ProcProxy.reap_group()` sends SIGTERM to the session's process group and, after `grace_s[1]`, SIGKILL, and records in `signals` whether any process was still alive (`("reap_term", utc)`, `("reap_kill", utc)`). A background child of a session that ended normally could otherwise edit `benchmark.py` after the restore, change a candidate file between its import check and its jobs, or reach a local executor's listener [inf]. `ProposerRunner` calls it right after `claude_wrapper.run` returns, before it snapshots the tree.

```python
# proposer_shim.py
class Tee:                                         # file-like for claude_wrapper's reader threads
    def __init__(self, pipe: TextIO, sink_path: Path | None, stream: str, clock: Clock | None = None,
                 on_line: Callable[[str], None] | None = None) -> None
    def readline(self) -> str                      # appends {"t_utc", "stream", "line"} to the sink for each line, calls on_line; "" at end of file
    def close(self) -> None
    eof: threading.Event
class ProcProxy:                                   # what claude_wrapper.run uses of a Popen object
    stdout: Tee; stderr: Tee
    returncode: int | None
    timed_out: bool                                # true once the deadline SIGINT was sent
    paused_s: float                                # how far reported rate-limit waits moved the deadline
    signals: list[tuple[str, str]]                 # (signal name, utc)
    def poll(self) -> int | None
    def wait(self, timeout: float | None = None) -> int
    def kill(self) -> None                         # SIGKILL to the process group (the wrapper calls it only if it has a deadline of its own)
    def reap_group(self) -> list[str]              # after the CLI ended: SIGTERM, then SIGKILL after grace_s[1], to what is left of the group
class SubprocessShim:                              # installed as claude_wrapper.subprocess for one session
    PIPE = subprocess.PIPE; DEVNULL = subprocess.DEVNULL
    def __init__(self, *, env: Mapping[str, str], cli_path: str, stream_path: Path, stderr_path: Path,
                 timeout_s: float, grace_s: tuple[float, float], clock: Clock | None = None) -> None
    def Popen(self, cmd: list[str], **kwargs: object) -> ProcProxy     # FileNotFoundError propagates (the wrapper maps it to exit 127)
    last: ProcProxy | None
def retry_pause_s(line: str) -> float              # pure: retry_delay_ms / 1000 of a rate-limit api_retry event line; 0.0 for any other line

CREDENTIAL_READ_DENIES = ("Read(~/.netrc)", "Read(~/.ssh/**)", "Read(~/.config/gh/**)", "Read(~/.config/wandb/**)",
                          "Read(~/Library/Keychains/**)")
@dataclass(frozen=True)
class ArgvSpec:
    mode: str                                      # "upstream" | "restricted" | "contained"
    cli_path: str; model: str; effort: str
    max_budget_usd: str                            # proposer.max_budget_usd_per_session, as the manifest writes it
    settings_file: Path                            # <session dir>/settings.json (every mode)
    scratch_dir: Path | None                       # restricted and contained
    addendum: str | None                           # restricted only: the rendered addendum
def parse_upstream_argv(cmd: list[str]) -> dict
    # {"prompt", "model", "tools", "allowed_tools": [...], "plugin_dir", "system_prompt", "effort"} read from upstream's command
    # (claude_wrapper.build_command, L127-L168); UpstreamContractError when a flag upstream always emits is missing
def session_argv(cmd: list[str], spec: ArgvSpec) -> list[str]          # pure: the exact argv of the mode, below
def command_fingerprint(cmd: list[str]) -> dict    # {"argv_sha256", "argv_flags": [...]}; the values after -p and --append-system-prompt become {"sha256", "chars"}
@contextmanager
def installed(claude_wrapper: ModuleType, shim: SubprocessShim, *,
              build_command: Callable[..., list[str]]) -> Iterator[None]      # sets both attributes; restores both in a finally
```

`stream.jsonl` rows: `{"t_utc": "...", "stream": "stdout", "line": "<raw line without the newline>"}`. Stderr lines go to `stderr.txt` as they are. `stream.jsonl` is never truncated or pruned: it is where the raw events of every error result are kept.

**The three permission modes** (`decisions.proposer_permission_mode`). `session_argv` returns exactly these lists. `<prompt>`, `<tools>` (the comma-joined list), `<allowed tools>`, `<plugin dir>`, `<skill text>` and the model and effort are taken from upstream's own command through `parse_upstream_argv`; everything else is ours. In every mode: `cmd[0]` is the pinned binary, whose SHA-256 is verified before each session; `--model` is `proposer.model` (the alias `opus`, or a pinned id); `--max-budget-usd` is `proposer.max_budget_usd_per_session`; and `--settings` names a generated file that holds at least `{"autoMemoryEnabled": false, "switchModelsOnFlag": false, "disableAllHooks": true}`. Auto memory loads in `-p` mode whatever `--setting-sources` says, and Opus 5.5 can switch to another model in the middle of a session when a request is flagged for biology or cybersecurity; the datasets are medical and chemical, so with `switchModelsOnFlag: false` such a request ends the session with an error instead of silently changing the proposer [r2].

Mode `upstream`: upstream's command (the first eight lines, `claude_wrapper.py` L127-L168 and `meta_harness.py` L39-L47, L169-L179 [src]) plus the last line.

```
<CLI> --dangerously-skip-permissions -p <prompt> --output-format stream-json --verbose
  --model <MODEL> --setting-sources ""
  --tools <tools>
  --allowedTools <allowed tools>
  --disable-slash-commands --strict-mcp-config
  --plugin-dir <plugin dir>
  --append-system-prompt <skill text>
  --effort <EFFORT>
  --settings <session dir>/settings.json --max-budget-usd <CAP>
```

Mode `restricted`: no `--dangerously-skip-permissions` and **no `--allowedTools`**.

```
<CLI> -p <prompt> --output-format stream-json --verbose
  --model <MODEL> --effort <EFFORT> --setting-sources ""
  --tools <tools>
  --disable-slash-commands --strict-mcp-config
  --plugin-dir <plugin dir>
  --permission-mode dontAsk --permission-prompts none
  --settings <session dir>/settings.json
  --add-dir <SCRATCH>
  --disallowedTools "Read(~/.netrc)" "Read(~/.ssh/**)" "Read(~/.config/gh/**)" "Read(~/.config/wandb/**)" "Read(~/Library/Keychains/**)"
  --append-system-prompt <skill text + two newlines + the restricted-mode addendum>
  --max-budget-usd <CAP>
```

Mode `contained`: mode `upstream`'s list with `--add-dir <SCRATCH>` inserted after the `--settings` pair; the settings file carries the sandbox block and the deny rules (below). The system prompt is upstream's, without the addendum.

Why `restricted` looks like this [r2]:

| Point | Reason |
|---|---|
| No `--allowedTools` | Under `dontAsk`, anything an `--allowedTools` entry covers still runs. With the list left in, Write and Edit could change any workspace file the deny rules do not name, an allowed Bash command would also have its unsandboxed retry approved, and if the settings file were ignored Bash would run with no sandbox at all. With it removed, the only thing that approves a Bash command is the sandbox's own auto-allow, and the only thing that approves an edit is an `Edit(...)` allow rule in the settings file. |
| `--permission-mode dontAsk --permission-prompts none` | Every call that would prompt is denied, the tools that need a human answer are removed, and the model is told not to retry a denied call. |
| `--add-dir <SCRATCH>` | Under the two block keys of the settings file no allow rule opens a path outside the working directories; only a working directory does. |
| Credential denies repeated with `--disallowedTools` | In `-p` mode a settings file that fails validation is ignored without an error. Command-line rules do not pass through settings validation, so the five credential read denies hold even then. |
| What "fail closed" means, and no more | If the settings file is dropped: writes, and recognised reads outside the working directories, are denied. The built-in read-only commands still run, unsandboxed. The `sandboxed: true` check below is what catches that case. |
| The addendum | The skill tells the proposer to write test scripts in `/tmp/`, pull examples and delete the scripts (`SKILL.md` L66-L69 [src]). In this mode `/tmp` is not writable, and the sandbox does not auto-allow heredocs, sub-commands that start with `time`, `timeout`, `nohup` or `env`, `rm`, `rmdir`, `awk`, `jq`, `eval`, `source`, or a leading variable assignment (from the pinned binary's code [r2; live: PF9]); with no Bash allow rule those are denied. Telling the model once is cheaper than letting it find out by denial. |

Mode `contained` is the fallback between the two: fewer denials and the smallest command difference, but it is **not** fail-closed. Deny rules apply in bypass mode too; if the settings file is ignored the session is mode `upstream`, and the `sandboxed: true` check is the only guard.

**The restricted-mode addendum** (`proposer.RESTRICTED_ADDENDUM`; `{scratch_dir}` is its only placeholder). The skill file is not edited. The SHA-256 of the rendered text is recorded in the session record (`addendum_sha256`), and the addendum is deviation V32.

```
## Run environment (added by the harness; not part of the skill)

This session runs with restricted permissions. Where the skill mentions /tmp, these rules apply instead.

- Scratch directory: {scratch_dir}
  Use it wherever the skill says /tmp/. /tmp itself is not writable.
- Create and change files with the Write and Edit tools, not with shell redirection or heredocs.
- Run a script as: uv run python <file>
  Run the skill's import check exactly as the skill gives it.
- Shell commands that use a heredoc, timeout, time, env, nohup, eval, source, rm, rmdir, awk, jq, or a leading
  VAR=value assignment are refused. Do not retry a refused command; do the step another way.
- Leave scratch files in place. Do not delete them.
- You can create new files under agents/ and write pending_eval.json at the path the task prompt gives.
  Existing files under agents/, every other project file and the reports directory are read-only in this
  session. If a step of the skill would write a report, skip that step.
- Shell commands have no network access.
```

**The generated settings file.** `--settings` never points at hand-written JSON. `generate_settings` is a pure function of the manifest-derived paths and of directory listings that `take_settings_inputs` reads just before the session; `ProposerRunner` writes its result to `<session dir>/settings.json` (canonical JSON, mode 0600) and records its hash. The session directory is under `STATE`, so the sandbox cannot write the file and the file tools treat it as protected.

```python
# proposer.py (settings)
COMMON_SETTINGS = {"autoMemoryEnabled": False, "switchModelsOnFlag": False, "disableAllHooks": True}
CREDENTIAL_PATHS = ("~/.netrc", "~/.ssh", "~/.gnupg", "~/.aws", "~/.config/gh", "~/.config/gcloud", "~/.config/wandb", "~/.docker",
                    "~/.kube", "~/.npmrc", "~/.pypirc", "~/.git-credentials", "~/.cache/huggingface", "~/Library/Keychains",
                    "~/.claude/.credentials.json")
RESTRICTED_ADDENDUM: str                            # the text above
@dataclass(frozen=True)
class SettingsInputs:                               # everything generate_settings needs, as data
    mode: str                                       # "upstream" | "restricted" | "contained"
    ws: Path; logs_dir: Path; state_root: Path; package_dir: Path
    scratch_dir: Path; scratch_root: Path; example_venv: Path; uv_dir: Path | None; home: Path; uid: int
    state_children: tuple[str, ...]                 # names directly under STATE
    state_siblings: tuple[str, ...]                 # names directly under SC/state other than this campaign's directory
    ws_top: tuple[str, ...]                         # names directly under WS
    agent_files: tuple[str, ...]                    # names of the entries of WS/agents at session start
    logs_entries: tuple[str, ...]                   # names directly under LOGS
    claude_tmp_projects: tuple[str, ...]            # names directly under /private/tmp/claude-<uid>/
    scratch_others: tuple[str, ...]                 # other session directories under scratch_root, as "<campaign>/<session_key>"
def take_settings_inputs(manifest: dict, paths: StatePaths, *, mode: str, logs_dir: Path, scratch_dir: Path, home: Path,
                         example_venv: Path, uv_dir: Path | None, uid: int) -> SettingsInputs     # the only I/O: os.listdir
def generate_settings(inputs: SettingsInputs, override: Mapping | None = None) -> dict           # pure
def merge_settings(base: Mapping, override: Mapping) -> dict
    # pure deep merge: objects merge key by key, lists are concatenated without duplicates (base first), anything else is replaced
def check_settings(settings: Mapping, mode: str) -> list[str]                                    # pure; the invariants below
def render_addendum(scratch_dir: Path) -> str
def transcript_slug(cwd: Path) -> str               # every character outside [A-Za-z0-9] replaced by "-"
```

What `generate_settings` returns, by mode. Paths are written in the CLI's rule syntax: `//<absolute path>` in permission rules, plain absolute paths in the sandbox lists.

| Part | `upstream` | `restricted` | `contained` |
|---|---|---|---|
| `COMMON_SETTINGS` (the three keys) | yes | yes | yes |
| `permissions.defaultMode: "dontAsk"` | no | yes | no (the session is in bypass mode; allow rules have no effect there) |
| `permissions.allow`: `Edit(//<WS>/agents/**)`, `Edit(//<LOGS>/pending_eval.json)`, `Edit(//<SCRATCH>/**)` | no | yes | no |
| `permissions.deny`, edit rules: `Edit(//<WS>/agents/<name>)` for every entry of `agent_files` (an existing agent file cannot be changed; a deny beats the allow above, so only **new** files under `agents/` can be written), and `Edit(//<WS>/<name>)` and `Edit(//<WS>/<name>/**)` for every entry of `ws_top` other than `agents` and `logs` (the pinned files and directories, `uv.lock`, `.claude`, `.empty_plugins`) | no | yes | yes |
| `permissions.deny`, read rules: `Read(<p>)` and `Read(<p>/**)` for every `p` of `CREDENTIAL_PATHS`; `Read(//<STATE>/<child>)` and `Read(//<STATE>/<child>/**)` for **every entry of `state_children` other than `workspace`**; the same pair for every entry of `state_siblings` under `SC/state`; the same pair for `package_dir` | no | yes | yes |
| `permissions.blockReadsOutsideWorkingDirectories: true`, `blockWritesOutsideWorkingDirectories: true` | no | yes | yes |
| `sandbox`: `enabled: true`, `failIfUnavailable: true`, `allowUnsandboxedCommands: false`, `autoAllowBashIfSandboxed: true` | no | yes | yes |
| `sandbox.filesystem.allowRead`: `~/.local/share/uv/python` (the venv interpreter is a link into it), the workspace's parent (`STATE/workspace`), the example venv root, and `uv_dir` when it lies under the home directory | no | yes | yes |
| `sandbox.filesystem.denyRead`: `/private/tmp/claude-<uid>/<name>` for every entry of `claude_tmp_projects` other than `transcript_slug(<WS>)` (every Claude Code session's scratchpad lives there, and the read block leaves `/private/tmp` readable); `<scratch_root>/<entry>` for every entry of `scratch_others` | no | yes | yes |
| `sandbox.filesystem.denyWrite`: `<WS>/<name>` for every entry of `ws_top` other than `agents` and `logs`; `<WS>/agents/<name>` for every entry of `agent_files`; `<LOGS>/<name>` for every entry of `logs_entries` other than `pending_eval.json` (results, reports, the wrapper's session logs, `frontier_val.json`, `evolution_summary.jsonl`) | no | yes | yes |
| `sandbox.credentials.files`: `{"path": <p>, "mode": "deny"}` for every `p` of `CREDENTIAL_PATHS` | no | yes | yes |
| `sandbox.network: {"allowedDomains": [], "strictAllowlist": true}` (an empty allow-list: no network from Bash, and no prompt can widen it) | no | yes | yes |

Three rules of construction that the tests pin:

- **No blanket deny on the state directory.** The workspace lives inside `STATE`, and a deny beats every allow, so `Read(//<STATE>/**)` would block the session's own working directory [r2]. The generator emits one deny pair per child of `STATE` except `workspace`, from the listing. A child created during the session (none is: the runner is blocked in `claude_wrapper.run`) would be covered by the read block.
- **Other sessions' scratch is denied by name.** One `denyRead` entry per existing project folder under `/private/tmp/claude-<uid>/` other than the proposer's own, and one per other proposer scratch directory.
- **Reports are read-only in mode `restricted`.** The allow list is exactly the three rules above: new files under `agents/`, `pending_eval.json`, the scratch directory. The skill's Step 0 asks the proposer to write missing post-eval reports under `LOGS/reports/` (`SKILL.md` L46-L48 [src]); in this mode that write is denied and the addendum says to skip it. This is part of deviation V32. A user who wants reports adds `Edit(//<LOGS>/reports/**)` through the override.

`proposer.restricted.settings` is an optional override: when it is not null, `generate_settings` deep-merges it over the generated settings of modes `restricted` and `contained` with `merge_settings` (it is ignored in mode `upstream`), and the session record says so (`settings_override`). The override exists so that what the probes of PF9 find (a path `uv` needs, a command form to allow) can be fixed in a manifest instead of in code. It cannot switch off what the mode rests on: `check_settings` returns a problem, and the session does not start (`CampaignStop("proposer_environment")`), unless the merged settings still have the three common keys with their values and, in modes `restricted` and `contained`, `sandbox.enabled` true, `sandbox.failIfUnavailable` true, `sandbox.allowUnsandboxedCommands` false, `sandbox.network.allowedDomains` empty with `strictAllowlist` true, both block keys true, no `Bash` or `Bash(...)` allow rule, and in mode `restricted` `permissions.defaultMode == "dontAsk"`.

Status of all this: every key is confirmed against the CLI's documentation one by one; the combination has never run [r2; live: PF9].

```python
# proposer.py
PLAN_WINDOW_TYPES = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "seven_day_overage_included")
FALLBACK_SUBTYPES = ("model_refusal_fallback", "model_refusal_no_fallback", "model_fallback")
def parse_stream(rows: Sequence[Mapping]) -> dict
    # {"init": dict | None, "result": dict | None, "events": int, "assistant": [event, ...], "system": [event, ...],
    #  "rate_limits": [event, ...], "api_retries": [event, ...], "bash_tool_uses": int}; unknown types and subtypes are ignored
def limit_signal(parsed: Mapping) -> dict            # the "limit" object of 2.11 from the last rate_limit_event with status "rejected"
def check_model(parsed: Mapping, *, requested_model: str, expected_resolved: str | None, requested_effort: str,
                request_efforts: Sequence[str] | None) -> dict                       # the "model_check" object of 2.11
def check_sandboxed(bash_results: Sequence[Mapping] | None, bash_tool_uses: int) -> dict   # the "sandbox_check" object of 2.11
def integrity_violations(init: Mapping | None, drift: Mapping, *, mode: str, ws: Path, cli_version: str,
                         memory_dir_changed: bool, memory_saved_events: int) -> list[str]
@dataclass(frozen=True)
class SessionFacts:
    mode: str; exit_code: int | None; timed_out: bool; result: Mapping | None; pending_ok: bool
    stderr_text: str; limit: Mapping; model_check: Mapping; sandbox_check: Mapping; api_retry_errors: tuple[str, ...]
def classify_session(facts: SessionFacts, manifest: dict) -> tuple[str, str | None]     # (outcome of 2.11, outcome_detail)
def locate_transcript(home: Path, session_id: str) -> Path | None
def copy_transcript(src: Path, dest_dir: Path, *, max_bytes: int) -> dict       # {"found", "files", "bytes"}; never follows a symlink
def solver_attempt_signals(rows: Sequence[Mapping]) -> list[str]   # Bash inputs that name the solver path (openrouter, api.inference.wandb.ai, litellm, 127.0.0.1)
def state_access_signals(rows: Sequence[Mapping], *, state_root: Path, ws: Path, package_dir: Path) -> list[str]
    # tool calls (Read, Grep, Glob, Bash, Edit, Write ...) whose path arguments or command text name a path that lies under
    # state_root but outside ws, or under package_dir; relative paths are taken against ws; one short line per call, no content
def session_cost(result_event: Mapping | None, session: "NormalizedSession | None", manifest: dict, fallback_micro: int) -> tuple[int, str]
    # (micro-dollars, basis), first that applies: the result event's total_cost_usd ("result_event"); the transcript's last
    # cost-state record ("cost_state"); the transcript's usage, once per message.id, main thread and subagents, at
    # proposer.list_prices ("usage_sum"; only when every model in it has a price); fallback_micro ("estimate"). Never zero:
    # a figure of zero from any source falls through to the next
def find_usable_proposal(paths: StatePaths, iteration: int) -> tuple[int, dict] | None      # (ordinal, proposal record) of the latest "ok" session
def settled_failure(paths: StatePaths, iteration: int) -> dict | None                        # the latest session record when its outcome is timeout | failed | no_candidates
def candidate_reference(paths: StatePaths, name: str, sha256: str | None = None) -> tuple[Path, str] | None
    # the stored copy of a proposed candidate: with a hash, the copy with that hash; without, the copy in the latest
    # proposal (highest iteration, then highest ordinal) that lists the name; None when there is none

class ProposerRunner:
    def __init__(self, manifest: dict, paths: StatePaths, *, journal: Journal, ledger: Ledger, upstream: Upstream,
                 example_dir: Path, lock_file: Path | None, base_env: Mapping[str, str], home: Path,
                 on_unit: Callable[[str], None] | None = None, halt_check: Callable[[], None] | None = None,
                 clock: Clock | None = None, rng: random.Random | None = None) -> None
    frontier_before: dict | None                             # of the proposal in use (stored, or read at this call); used by the iteration record
    last_session_key: str | None
    def propose(self, task_prompt: str, iteration: int, timeout: int = 2400) -> bool     # replaces meta_harness.propose_claude (L164-L189)
```

`propose` steps. Steps 3 to 15 are one session; step 15 may run them again for the same iteration.

1. `halt_check()`. (The driver's wrapper has already set the phase tracker to `search` and this iteration.)
2. **Stored proposal.** `find_usable_proposal(paths, iteration)`: restore `pending_eval.json` to `meta_harness.PENDING_EVAL` (upstream unlinked it at L467-L468 just before), restore any candidate file whose hash differs from the stored copy, set `frontier_before` from the proposal record (not from the file: upstream re-ran `--frontier` at this start, L428, so the file may already hold this iteration's results), journal `proposer: replayed`, return `True`. `settled_failure(paths, iteration)`: return `False`. No session starts in either case. On resume upstream asks for the same iteration again because an unfinished iteration has no summary row; without this a second paid session would run and produce different candidates [X-E4]. Otherwise read `FRONTIER_VAL` into `frontier_before`.
3. **Budget.** `W = micros(proposer.max_budget_usd_per_session)`, the most one session can cost (the CLI stops after the response that crosses the cap, so the overshoot is one response). `ledger.check_meter_cap("proposer_list_price", extra_micro=W)`; over the cap: halt `budget:proposer` and `CampaignStop`. This is the cumulative proposer cap: the meter holds each finished session's real cost and counts an open session at `W`.
4. **Binary.** `proposer.cli_path` exists, is executable and its SHA-256 equals `proposer.cli_sha256`; otherwise `CampaignStop("proposer_cli")` (the pinned path lives in a folder the desktop app manages and can change or vanish; critic gap 5).
5. `k = journal.next_attempt("proposer", "iter<NNN>")`; create the session directory. `workspace.ensure_workspace` (restores pinned files); snapshot the tree (`tree-before.json`), `env_fingerprint(upstream.worker_python)`, `memory_dir_fingerprint`; copy `LOGS/evolution_summary.jsonl` (when it exists) to `<session dir>/evolution_summary.before.jsonl` and note its hash.
6. **Scratch and environment.** `scratch = paths.proposer_scratch(proposer.scratch_root, campaign, session_key)`; create it with mode 0700. It must be given in resolved form (`os.path.realpath(scratch) == str(scratch)`; on macOS `/tmp` is a link to `/private/tmp`, and an allow rule applies only when the requested and the resolved path both match) and lie outside `STATE`; otherwise `CampaignStop("proposer_environment")`. Build the environment with `procenv.proposer_env(manifest, base_env, workspace_root=..., example_venv=procenv.venv_root(<worker python>), scratch_dir=scratch)`; write `env.json` (names only).
7. **Settings and argv.** `inputs = take_settings_inputs(...)`; `settings = generate_settings(inputs, override=proposer.restricted.settings)`; `check_settings(settings, mode)` (any problem: `CampaignStop("proposer_environment")`); write `<session dir>/settings.json`. Build the `ArgvSpec` (the addendum rendered with `render_addendum(scratch)` in mode `restricted`).
8. Journal `started`; `ledger.meter_open("proposer_list_price", session_key, W, "micro_usd", worst_micro=W)`. From here the session counts at `W` until it is closed with its real figure, so a session that is killed, or a runner that dies, cannot be free.
9. Install the shim and the `build_command` wrapper (`installed(...)`; both are restored in a `finally`). Call `claude_wrapper.run(prompt=task_prompt, model=<proposer.model>, allowed_tools=meta_harness.PROPOSER_ALLOWED_TOOLS, skills=[str(EVOLVE_DIR / ".claude/skills/meta-harness")], cwd=str(EVOLVE_DIR), log_dir=str(LOGS_DIR / "claude_sessions"), name=f"iter{iteration}", timeout_seconds=None, effort=<proposer.effort>)`. These are upstream's arguments (L169-L179 [src]) except the timeout, which the shim owns (`timeout_s=timeout`, on the monotonic clock). The wrapper keeps reading until the process exits, so its own `claude_sessions/` log also holds the trailing `result` event. Write `command.json` (the fingerprint of the command the shim ran). When the wrapper returns: `shim.last.reap_group()`.
10. Wait up to 5 s for the tee's `eof`. Copy `PENDING_EVAL` to the session directory at once if it exists (upstream deletes it at the next iteration and never keeps `base_system` [rep]); check it with `workspace.read_pending_eval` (a `WorkspaceError` means `pending_ok = False`).
11. Take the session id from the `system/init` event; `locate_transcript`; `copy_transcript`; snapshot the tree again; `classify_drift`; `restore_pinned`. Then the guards on what the session left behind:
    - `workspace.unknown_entries` are moved out with `workspace.quarantine_entries` and listed in `drift.quarantined` (journal `workspace: quarantine`);
    - if `LOGS/evolution_summary.jsonl` differs from the copy of step 5 (changed, created or removed), it is restored from that copy and the path goes into `drift.logs_modified`. Upstream alone writes that file, after the benchmarks; an extra row would move the start iteration upstream computes from it (`meta_harness.py` L149-L161), and `logs/` is outside the drift snapshot;
    - `solver_attempt_signals`, `state_access_signals`, and the comparison of the environment fingerprint.
12. **Session checks** (below): `parse_stream`; `limit_signal`; the transcript is normalised with `telemetry.transcript` (a `TranscriptError` of kind `version` is `CampaignStop("proposer_cli")`); `check_model` (with the main-thread request efforts from the transcript); `check_sandboxed`; `integrity_violations`.
13. **Cost.** `micro, basis = session_cost(result_event, session, manifest, E)` with `E = max(ledger.meter_max("proposer_list_price"), micros(proposer.session_estimate_usd))`; `ledger.meter_close("proposer_list_price", session_key, micro, micro_usd=micro)`.
14. `classify_session`; write `session.json`. Outcome `ok`: write `proposal.json` (with `frontier_before`), the copy of `pending_eval.json` and `candidates/<name>.py`. Journal `complete`. `on_unit("proposer:<session_key>")`, and `on_unit("propose:<name>")` for each stored candidate.
15. **Reaction**, by the table below. A non-empty `integrity.violations` raises `CampaignStop("proposer_integrity")` whatever the outcome, after the record is written (an `ok` session's proposal stays stored and is replayed after the user has looked).
16. Return value: `True` only for outcome `ok`. For a session that ended with a successful result the runner calls `result.show()` as upstream does at L188; otherwise it prints upstream's failure lines (L183-L186). A timed-out session yields nothing even if the CLI then exited 0 and wrote candidates (decision D16 keeps upstream's rule).

**Success is a `result` event, never an exit code.** In stream-json mode the CLI exits 0 when no `result` event was written at all, and its SIGINT handler exits 0 [r2, from the pinned binary]. Upstream accepts exit code 0 and then only checks that `pending_eval.json` exists (L183-L189), so a cancelled or cut-off session that had already written the file would be evaluated. Here a session is successful only when the stream holds a `result` event whose `is_error` is false. `subtype` is not the test: an API error that ends the turn is reported as `subtype: success` with `is_error: true` [r2].

**Session checks.**

- *Model and effort* (`check_model`). `R` is `message.model` of the first main-thread `assistant` event (`parent_tool_use_id` null). A mismatch, any of: `proposer.expected_resolved_model` is set and differs from `R`; `proposer.model` is a pinned id (it starts with `claude-`) and `R` is neither that id nor that id followed by `-` and a date; `proposer.model` is an alias and the alias is not one of the `-`-separated parts of `R`; any main-thread `assistant` event carries another model than `R`; `init.model` is neither `proposer.model` nor `R` [live: PF10, which of the two `init` shows]; the stream holds a `system` event whose subtype is one of `FALLBACK_SUBTYPES`; `init.effort` is present and differs from `proposer.effort`; a main-thread `api-request` record of the transcript has `params.output_config.effort` other than `proposer.effort` (an organisation's effort cap applies silently in stream-json mode [r2]). The check runs on `init`, on every main-thread `assistant` event and on those `system` events, not on the first event only: a blocked or flagged model is substituted without the session failing. Subagent models (events with `parent_tool_use_id` set) are recorded in `subagent_models` and never asserted. Any mismatch is outcome `model_mismatch`.
- *Sandbox proof* (`check_sandboxed`; modes `restricted` and `contained`). Every tool result of a Bash call in the copied transcript, main thread and subagents, must carry `sandboxed: true` (`transcript.bash_results`, 3.21). One result with `false`, one without the key, or no transcript to read while the stream shows a Bash call, is outcome `settings_ignored`. A session with no Bash call passes. In mode `upstream` the counts are recorded and nothing is asserted. [That the flag means "ran inside the Bash sandbox" is inferred from its name and from `false` in eleven unsandboxed local transcripts; live: PF9.]
- *Integrity* (`integrity_violations`), after every session, in every mode: `init.plugins` holds an entry other than upstream's own empty plugin directory (`<WS>/.empty_plugins`); `init.skills`, `init.mcp_servers` or `init.memory_paths` is non-empty; `init.claude_code_version` differs from `proposer.cli_version`; a `system` event with subtype `memory_saved` appeared, or the auto-memory directory's fingerprint changed; and the tree comparison. For the tree, what counts depends on the mode. In every mode: a pinned upstream file modified or removed (`drift.upstream_modified`, `upstream_removed`) or a guarded log changed (`drift.logs_modified`). In modes `restricted` and `contained` also: an existing agent file modified (`drift.agents_modified`) or anything left outside `agents/` and `logs/` (`drift.quarantined`), because the generated rules were meant to make those impossible. In mode `upstream` those two are restored or quarantined and recorded, as before (V12): nothing denied them. `result.permission_denials` is recorded and is never a violation; in mode `restricted` denials are expected.

**The limit classifier** (`classify_session`), first match wins. Structured fields come first; text patterns are the fallback.

| # | Condition | Outcome (detail) |
|---|---|---|
| 1 | exit code 127 | `cli_missing` |
| 2 | `result.startup_failure_reason == "sandbox_required_unavailable"` | `sandbox_unavailable` |
| 3 | `model_check.mismatch` is not null | `model_mismatch` |
| 4 | mode `restricted` or `contained`, and the sandbox proof failed | `settings_ignored` |
| 5 | a `result` event with `is_error` false, not timed out, `pending_ok` | `ok` |
| 6 | a `result` event with `is_error` false, not timed out | `no_candidates` |
| 7 | the shim timed the session out | `timeout` |
| 8 | `result.subtype` is `error_max_budget_usd` or `error_max_turns` | `failed` (`max_budget_usd`, `max_turns`) |
| 9 | `limit.signal == "rate_limit_event"` (a `rate_limit_event` with `status: rejected`): `rate_limit_type == "overage"` with an `overage_disabled_reason`, or `error_code == "credits_required"` | `spend_limit` |
| 10 | the same signal with `resets_at_utc` set and `rate_limit_type` in `PLAN_WINDOW_TYPES` | `limit_window` |
| 11 | the same signal in any other shape | `spend_limit` (`unrecognised_limit`) |
| 12 | `result.api_error_status == 401`, or an `auth_patterns` entry in the lower-cased stderr or `result` text | `auth` |
| 13 | `result.api_error_status` is 402 or 429, or a `usage_limit_patterns` entry in the lower-cased stderr or `result` text | `spend_limit` (`text` or `status`) |
| 14 | `result.api_error_status` is 408 or at least 500; or the last `api_retry` event's `error` is `overloaded`, `server_error` or a network category; or there is no `result` event at all and the session was not timed out | `transient` (`no_result_event` for the last case) |
| 15 | anything else | `failed` |

What each outcome does:

| Outcome | Reaction |
|---|---|
| `ok` | the proposal is stored; `propose` returns `True` |
| `no_candidates`, `timeout`, `failed` | a settled failure: upstream's rule, the iteration number is used up, nothing runs again after a resume |
| `spend_limit` | `CampaignStop("proposer_spend_limit")`: halt and ask the user. This is the case to expect on a usage-billed seat: a spend limit does not reset on a five-hour timer, and a wait-and-retry loop would wait for nothing [r2] |
| `limit_window` | wait until `resets_at_utc` plus 60 s, in slices of 30 s with `halt_check()` in each, then run a new session (`s<k+1>`) for the **same** iteration. A reset further away than `proposer.max_limit_wait_s`, or a third wait in one `propose` call, is `CampaignStop("proposer_limit_wait")` |
| `auth` | `CampaignStop("proposer_auth")`: halt and ask (the user logs in again) |
| `transient` | wait a uniform time from `proposer.transient_backoff_s`, then a new session for the same iteration, at most `proposer.transient_retries` times in one `propose` call; then `CampaignStop("proposer_transient")` |
| `model_mismatch` | `CampaignStop("proposer_model")` |
| `sandbox_unavailable` | `CampaignStop("proposer_sandbox")` |
| `settings_ignored` | `CampaignStop("proposer_settings")` |
| `cli_missing` | `CampaignStop("proposer_cli")` |

A stop is not a settled failure: the session is on record and paid for, and after a resume the same iteration runs a new session. Upstream would treat each of these like any failure and, after a limit, burn every remaining iteration in seconds (critic gap 12). A changed environment fingerprint still raises `CampaignStop("proposer_environment")`.

Patterns are the fallback only. They are never matched against anything from a session that qualifies as `ok`, and never against assistant text other than the `result` event's own text. The sessions are about memory systems with character and token limits (upstream's own agent says "respecting global char limit", `agents/fewshot_memory.py` L54), so a normal final message such as "falls back when the char limit reached" would match a limit pattern; it is a successful result, row 5 comes first, and the patterns are never consulted [A-U, by reading]. The default lists hold `spend limit`, `usage limit` and `budget` because on an organisation with usage-based billing the message says "usage limit", not "spend limit" [r2]. Text alone never leads to a wait: a wait needs a machine-readable reset time. Any billing or rate-limit failure the classifier does not recognise ends in `spend_limit`: halt and ask. The exact live shape of a limit stop has never been observed; `stream.jsonl` keeps the raw events of every error result, so the first one can be read back.

**Cost of a session** (`session_cost`). The proposer runs on a usage-billed seat, so `budgets.proposer_list_usd` is an estimate of real spend and the meter is a real ceiling. With a `result` event the figure is its `total_cost_usd` (a client-side estimate from the CLI's bundled price table, which includes subagents [r2]). Without one, a session was killed or cut off, and it still cost money: the figure is the last `cost-state` record of the copied transcript (written only on a normal exit), else the transcript's usage counted once per `message.id` at `proposer.list_prices`, else the estimate `E`. It is never zero and never the wrapper's own `meta.json` counters, which are multiplied by the number of content blocks per message [r2].

`state_access_signals`. The workspace sits inside the state directory, so this build's own records are two directories above the proposer's working directory: `../../jobs/*/out/sidecar.jsonl` holds per-example predictions and targets, `../../calls/` holds raw prompts and replies, and the ledger, the replay store and the journal are there too. Upstream offers none of these. In mode `upstream` nothing prevents a read or a write, so access is **detected from the transcript, not prevented**: a non-empty list is stored in `session.json` and sets `evidence_exposed: true` on the iteration record. Decision U9a ("the proposer's evidence is exactly upstream's") is therefore monitored, not enforced, in mode `upstream` (V20). In modes `restricted` and `contained` the generated settings deny reads of every child of `STATE` other than `workspace/`, of every other campaign's state and of the `mhforge` package directory; PF9 checks with a canary file that such a read is denied.

`locate_transcript`: the first match of `<home>/.claude/projects/*/<session_id>.jsonl`. The folder name is not computed: a project folder name over 200 characters is truncated and hashed by the CLI [r2]. `copy_transcript` copies that file, the sibling directory `<session_id>/` (it holds `subagents/` and `tool-results/` [F-E5, T-E4]) and any set-aside copies `<session_id>.orphaned-*` and `<session_id>.jsonl.superseded-*`, within `proposer.max_transcript_bytes`. It reads nothing else under the home directory.

Environment: the names in `proposer.env_allow`, and `PATH`, from the `base_env` mapping the caller passes (the CLI passes `os.environ`), through `procenv.proposer_env`. Never: passes the runner's environment, a key variable, the gateway address or the cache-directory variable to the CLI; reads `os.environ` itself; leaves the shim or the `build_command` wrapper installed after the call; stores the prompt or the skill text (records hold hashes); deletes anything the proposer wrote under `agents/`; starts a session while a job listener is open; leaves a process of the session's group alive after the session; lets a session end without closing its meter row; treats an exit code as success; passes `--allowedTools` in mode `restricted`; writes a settings file by hand; waits on a limit without a structured reset time.

**Residual risk, stated as it is** (critic gap 8). In mode `upstream` the proposer runs as the same OS user with unrestricted Bash. It could read `~/.netrc` or `gh auth token` output; the key in `~/.netrc` is valid for Serverless Inference, so a proposer that prototypes against the model with it spends outside the ledger (the transcript is scanned for calls to the inference host, `solver_attempt_signals`, and the reconciliation gate of 8.2 compares organisation usage); it can read and write this campaign's state directory (the ledger, where it could clear a halt row; the replay store; the journal; our per-example records); and with `--key-source env`, where the in-place overwrite of 3.9 is unavailable, it could read a key from the runner's start-up environment with `ps` (with `--key-source prompt` or `fd:N` no key is ever in that block). Mode `contained` narrows this only while the settings file is honoured. This build keeps keys out of the proposer's environment, its argv and every file this package writes, keeps no listener open while it runs, reaps its process group, and records every tool call that names a state path; it does not make those files unreadable. Mode `restricted` (once PF9 has shown it works as a combination) or a dedicated revocable key plus billing reconciliation narrows that. Running the whole CLI inside a container with a setup token was not evaluated by anyone. The article must claim only what is enforced.

### 3.20 `driver.py` (F8)

```python
@dataclass
class Runtime:
    manifest: dict; paths: StatePaths; manifest_path: Path; example_dir: Path; clock: Clock; ids: IdSource
    ledger: Ledger; replay: ReplayStore; circuit: Circuit; gateway: Gateway; ingress: LoopbackIngress
    executor: Executor; journal: Journal; tracker: PhaseTracker

def make_executor(manifest: dict, paths: StatePaths, *, gateway: Gateway, ingress: LoopbackIngress, ledger: Ledger,
                  clock: Clock, ids: IdSource, environ: MutableMapping[str, str],
                  scrub: Callable[[str], bool] | None = None, sandbox_client: SandboxClient | None = None,
                  command_prefix: Sequence[str] = ()) -> Executor
def build_runtime(manifest: dict, manifest_path: Path, *, provider: ProviderClient, environ: MutableMapping[str, str],
                  scrub: Callable[[str], bool] | None = None, executor: Executor | None = None,
                  sandbox_client: SandboxClient | None = None, command_prefix: Sequence[str] = (),
                  clock: Clock | None = None, ids: IdSource | None = None) -> Runtime
def startup_recovery(rt: Runtime) -> dict
def count_iterations(summary_path: Path) -> int                # highest "iteration" in LOGS/evolution_summary.jsonl; 0 when absent
def remaining_iterations(total: int, start_iteration: int) -> int      # max(0, total - start_iteration + 1)
def install(up: Upstream, *, run_benchmark: Callable, propose: Callable | None, validate: Callable,
            summary: Callable) -> Callable[[], None]            # returns uninstall
class Driver:
    def __init__(self, rt: Runtime, *, base_env: Mapping[str, str], home: Path,
                 export_hook: Callable[[str], None] | None = None) -> None
    def run_baselines(self) -> dict                             # upstream's --iterations 0
    def run_search(self, iterations: int | None = None, *, max_iterations_this_run: int | None = None) -> dict
    def run_calibration(self) -> dict                           # test jobs of the calibration systems; never finalises the run
    def run_noise(self) -> dict                                 # repeats 2..calibration.noise_repeats of the baselines' validation jobs
    def run_test(self, *, end_search: bool = False) -> dict     # upstream's finalisation
    def run_qualification_job(self) -> dict                     # one job in ledger phase "qualification"; never published
    def shutdown(self) -> dict                                  # the stop path: abort queued requests, drain, close the executor
```

`make_executor` lives here and nowhere else (it needs the gateway, the ingress and the ledger, which are later layers than `executors/__init__.py`): `local` gives `LocalExecutor`, `local_relay` gives `LocalRelayExecutor(mode="stdio")`, `sandbox` gives `SandboxExecutor` with the given client or a `CwSandboxClient(manifest, environ=environ, scrub=scrub)`. `environ` is the key mapping the CLI passes (3.27; the smoke passes `os.environ`); this module never reads the process environment itself.

`startup_recovery` (the CLI already holds `runner.lock`): `ledger.interrupt_pending()`; `ledger.close_open_jobs()`; `journal.recover()`; `executor.cleanup_orphans(journal.open_handles())` (which also closes open sandbox meter rows, 3.17); every open `proposer_list_price` meter row is closed at its worst case (the session was cut off; its cost is unknown, so its `--max-budget-usd` cap stands); `workspace.ensure_workspace(...)`; `runner.verify_published()`; a halt row that the journal does not know (set by the `halt` command or the exporter) is journaled `halt: set`; an iteration that has rows in `evolution_summary.jsonl` but no `iteration: settled` journal entry gets one with kind `recovered`. Returns the counts.

`count_iterations` follows upstream's `count_iterations_from_summary` (`meta_harness.py` L149-L161) line for line: 0 when the file is absent; blank lines skipped; a line that is not JSON skipped; `json.loads(line).get("iteration", 0)`; the maximum. A difference would change how many iterations run, so `test_count_iterations_equals_upstream` compares the two on the same file, including a bad line and a row without `iteration`.

`run_search`:

1. `startup_recovery`; journal `run: started` (key `search`; the detail carries the SHA-256 of `SC/protocol.md`, so the protocol a run started under is on record).
2. `with workspace.load_upstream(paths) as up:` assert `upstream.expect` against `up.benchmark._CONFIG` (datasets, baselines, sizes, seed, mode, epochs, temperature, first model; `UpstreamContractError` on a difference); build the `JobRunner` (with `real_facade(up.benchmark)` and `candidate_reference` bound to `proposer.candidate_reference`) and the `ProposerRunner`; `install(...)`.
3. Signal handlers: the first SIGINT or SIGTERM sets a stop flag; a second raises `KeyboardInterrupt`. **A stop request is honoured at an iteration boundary only**: the `propose_claude` replacement looks at the flag before it does anything else and raises `CampaignStop("interrupted")` there, that is, after the previous iteration was settled and before the next one has spent anything. The jobs of the running iteration finish first. (During `evaluate` commands, which have no iterations, the flag is honoured after the running `run_benchmark` call.) Upstream's own flag, checked in the middle of the benchmarks, writes `failed` rows for candidates that never ran and uses up the iteration number [r2; X-E4]; `meta_harness._interrupted` is never set and upstream's handler (`_handle_signal`, L101-L104) is not installed. To stop spend at once there is the `halt` command (3.27), which ends the running job within about a minute.
4. `S = count_iterations(LOGS / "evolution_summary.jsonl") + 1`, which is the start iteration upstream will compute at L444 [src]. `n = remaining_iterations(search.iterations, S)`, cut to `max_iterations_this_run` when that is given and smaller. The driver passes `iterations = n`, so over all invocations the last iteration number is always `search.iterations`, as in an uninterrupted upstream run where a failed proposer consumes its iteration number (L476-L480). A search is meant to run in **resumable blocks** of four or five iterations (`search --max-iterations-this-run N`): a block is hours, the whole search is days [r2]. At each block boundary the user reconciles the ledger against the platform's figures, reads the Claude seat's spend, and the next start re-checks the CLI hash and the gates.
5. Call `up.meta_harness.run_evolve(argparse.Namespace(model=<upstream.expect.model>, run_name=<campaign>, fresh=False, skip_baseline=False, iterations=<n>, propose_timeout=<search.propose_timeout_s>, test=False))`. This is upstream's loop, unmodified (L360-L579). Those are the fields `run_evolve` reads; upstream's tests build the same object (`tests/test_evolution_contracts.py` L20-L32 [rep]; the review re-read L360-L579 [A-U]). `skip_baseline` stays false: the skip-if-usable rule makes finished baselines free. `main()` is not called.
6. How it ends:
   - `CampaignStop` unwinds `run_evolve` with no summary row for the unfinished iteration [X-E4]; the driver writes `stopped.json`, journals `run: stopped` and returns `{"status": "stopped", "reason": ...}` (exit code 2).
   - `SystemExit` from L371 or L402 becomes `{"status": "error"}`.
   - **Any other exception** that escapes `run_evolve` (an `ExecutorError` that no layer turned into an attempt, a `WorkspaceError`, a `TypeError` inside upstream's loop caused by a value we handed it, a bug of ours) is caught here: `stopped.json` and `run: stopped` are written with reason `upstream_contract` for an `UpstreamContractError` and `internal_error` for anything else, with the last 2,000 characters of the traceback, and the result is `{"status": "error", "reason": ...}` (exit code 1). The crash is then on record, `status` shows it, and a resume does not silently repeat it. `KeyboardInterrupt` (the second signal) is recorded as `interrupted` and re-raised after step 7.
   - A normal return journals `run: complete` when the last iteration that ran is `search.iterations`, and `run: paused` (detail `last_iteration`) when the block ended earlier because of `max_iterations_this_run`. A paused run exits 0 and returns `{"status": "paused", "last_iteration": ...}`; only `complete` unlocks the finalisation (2.3).
7. In a `finally`: uninstall, then `shutdown()`: `gateway.abort_queued()`; `undrained = gateway.drain(provider.attempt_deadline_s + 60)`; `executor.close()`; `undrained` goes into the `run: stopped` or `run: complete` detail. After a second signal the drain is skipped (the attempts are cut, the rows stay `pending`, and the next start books them as `bounded_unknown`); in every other case no provider attempt is left open and no `pending` row remains [A-M m5].

**Baselines must be complete before the search spends on proposals.** After Phase 0, that is at the first `--frontier` call in phase `baseline`, the driver requires every system of `upstream.expect.baselines` to have a published usable `val.json` on every dataset; otherwise it raises `CampaignStop("baseline_incomplete")` before the first proposer session. Upstream would go on with an incomplete baseline, and its finalisation, which requires every baseline on every dataset (L338-L351), could then never complete.

On Route 1 `args.model` is still `upstream.expect.model`: upstream's check at L368-L371 needs a string present in `config.yaml`, both strings give the directory leaf `gpt-oss-120b`, and only the worker command carries `routing.route1_model`.

`run_baselines` is `run_search` with `iterations=0` and the journal key `baselines`.

`run_calibration` (`evaluate --calibration`). The protocol's calibration gates need the baselines on the **test** split before the search. Upstream's only test path is `finalize_run`, which writes `finalized.json`; once that says `complete`, upstream refuses every later search under the run name (L395-L402): in the review's experiment baselines, then `test=True`, then a search ended with `SystemExit(1)` "run 'calib' is finalized" [A-U E6]. So calibration does not use `finalize_run`. It journals `calibration: started`; for each system of `calibration.systems` it plans the test jobs with `facade.build_test_runs(<LOGS>, <STATE/scratch/results>, [system])` (the scratch results directory is empty, so upstream's builder returns a job for every dataset whose validated `memory.json` exists) and runs each with `runner.run_job(planned, phase="calibration", iteration=None, publish=False)`, with the test inputs of 3.18; the calls are booked to the ledger phase `calibration`. Nothing is written under `LOGS/results` and no `finalized.json` is created, so the proposer never sees a test number and the search is not blocked. It writes `STATE/qualification/calibration.json` (2.4): per system and dataset the test result; per system the macro average over all datasets of the test accuracy in percent; `g0`, the validation counts of the published baselines (recorded for the protocol's free check G0, never judged: the bands are too wide to gate on); and one entry per gate of `calibration.gates`. A gate passes when its system's macro average lies in `macro_test_accuracy`, each dataset's number of correct test examples lies in its `correct` band, and, with `content_on_every_call`, every call record of the system's test attempts has an outcome of `completed`, `replayed` or `coalesced` with `flags.empty_content` false and a `finish_reason` other than `length`, and every `eval` row of the test split has `prediction_chars > 0`; a dataset whose test job failed fails the gate (no band is judged over a subset). The verdict is `fail` when a hard gate failed, `pass` when every hard gate passed, `recorded` when there is no hard gate; a failed soft gate adds its name to `flags` and stops nothing. With the draft manifest that is the protocol's G1 (hard, zero-shot: the only baseline whose configuration is the same in the paper and in the release) and G2 (soft, the released few-shot baseline against an independent re-run of the release; the one that exercises 24,000-token Chinese prompts) [r2]. The bands are 1.96 standard errors of a difference between two independent runs; they catch gross faults (routing, effort, truncation, parsing), nothing finer. It journals `calibration: complete`. A later finalisation in the same campaign reuses these attempts through the journal-backed reuse rule of 3.18 step 4 (same candidate bytes, same `memory.json`), so the test calls are paid once.

`run_noise` (`evaluate --noise`). The noise floor's first layer asks whether the same harness gives the same answers on the same examples. Repeat 1 is the campaign's published baseline run. For `k` in `2 .. calibration.noise_repeats` and each system of `upstream.expect.baselines`, it plans the validation jobs against the empty scratch logs directory (as the qualification job does) and runs each with `runner.run_job(planned, phase="calibration", iteration=None, publish=False, repeat=k)`. A repeat job has its own job key (`.r<k>`), so it has a fresh worker cache directory and its replies are reused only within itself (scope kind `repeat`, 3.7): every repeat pays for its own calls, which is the point. It then calls `qualification.noise_stats` over the sidecars of all repeats and writes `STATE/qualification/noise.json` (2.4): per dataset and system the correct count of each repeat, the flip rate (the share of examples whose correctness differs between any two repeats) and the number of examples on which all repeats agree; per system the macro validation accuracy of each repeat and its standard deviation; and `identical_all_repeats`. It journals `noise: started` and `noise: complete`, never publishes anything and never blocks the search. If every repeat is identical the article says the solver was repeatable at temperature 0 on the day and drops this layer from the error budget; the other three layers (sampling, selection, search) need no calls and are computed in the write-up (Appendix G).

`run_test(end_search=False)` (`evaluate --test`) is `run_search` with `test=True` and the journal key `test`, which makes upstream run `finalize_run` (L391-L393, L286-L357). It journals `test: started` (with `end_search`) before calling upstream and `test: complete` or `test: incomplete` after. `SystemExit(1)` ("Test finalization incomplete", or no frontier at L288-L290) is returned as `{"status": "incomplete"}`; upstream supports running `--test` again while `finalized.json` says `in_progress`, and a test job that used up its attempts can be given a fresh budget with `status --reset-job`. The gates of 2.3 guarantee the order: finalisation runs only after the search is complete (or was ended on purpose with `--end-search`), and once any `test` entry exists the search cannot run again, so no proposer session ever runs while `LOGS/results/**/test.json` exists in the directory its task prompt names.

`run_qualification_job`: plans the validation jobs of `qualification.system` with the facade against the empty scratch logs directory `STATE/scratch/logs` (so upstream's builder always returns them), takes the job for `qualification.dataset`, and calls `runner.run_job(planned, phase="qualification", iteration=None, publish=False)`.

Replacements installed on the loaded module. All are module globals looked up at call time; upstream's own tests patch `run_benchmark` the same way (`tests/test_evolution_contracts.py` L71-L77 [rep]), and the review confirmed with the real module that `run_evolve` and `finalize_run` call the replacements [A-U E1].

| Global | Upstream lines | Replacement |
|---|---|---|
| `meta_harness.run_benchmark` | L119-L133; called at L296, L325, L330, L333, L421, L428, L519, L527 | `runner.run_benchmark(args)`. The phase comes from the tracker (`baseline` before the first `propose`, then `search` with the iteration; any `--test` form is phase `test`). In a command other than `search`, a pending stop request raises `CampaignStop("interrupted")` after the call has returned. After the first `--frontier` call in phase `baseline` the driver writes the iteration-0 record and applies the baseline-complete rule above. |
| `meta_harness.propose_claude` | L164-L189; called at L473 | a wrapper that first honours a pending stop request (the iteration boundary, step 3), then sets the tracker to phase `search` and the iteration, then calls `proposer.propose(task_prompt, iteration, timeout)`. When it returns `False` (upstream then skips the iteration and writes no row, L476-L480) the wrapper writes our iteration record with kind `proposer_failed`, journals `iteration: settled` and calls `on_unit("loop:iter<NNN>")`. |
| `meta_harness.validate_candidates` | L192-L215; called at L492 | `validate` below |
| `meta_harness.update_evolution_summary` | L218-L254; called at L498-L500, L553-L560 | a wrapper that calls the original unchanged (the proposer reads its file), then writes our iteration record (with `frontier_before` from `proposer.frontier_before`, `evidence_exposed` from the session record, and each candidate's `context` figures from its job records, 2.11), journals `iteration: settled` and calls `on_unit("loop:iter<NNN>")` |
| `claude_wrapper.subprocess` and `claude_wrapper.build_command` | module import L12, used at L590-L598; defined at L115, called at L559 | both replaced only for the duration of one session, in every permission mode (3.19) |

`validate(candidates)`: entries beyond `search.max_candidates_per_iteration` are rejected (`over_limit`); `workspace.validate_candidate_file` rejects bad names and files (`name`), printed as upstream prints a failure (L212-L214); the rest go to `runner.import_check` (in the executor; upstream imports candidate code on the host and interpolates the unchecked name into `python -c`, L197-L207 [src]), which also applies the loaded-class assertion (a candidate whose loader-picked class is not defined in its own module is rejected with `loaded_class` and printed as a failure). An infrastructure failure of the import check never rejects a candidate: `runner.import_check` retries and then raises `CampaignStop("infrastructure")` (3.18), which unwinds upstream's loop like any other stop. `validate` prints upstream's `OK` / `FAIL` lines and returns the sublist of the same dict objects in the same order, which is what the call site at L492 expects.

Never: edits an upstream file; changes upstream path globals; calls `fresh_start` or passes `fresh=True` (it deletes candidates and results); swallows `CampaignStop`; lets any other exception leave a runner command without a `stopped.json`; continues after a halt; depends on `uv`; holds a gateway listener open while the proposer runs; runs upstream's `finalize_run` for a calibration; reads `os.environ`; honours a stop request in the middle of an iteration; journals a block that ended early as `complete`.

### 3.21 `telemetry/join.py` and `telemetry/transcript.py` (pure)

```python
# join.py
def parse_sidecar(rows: Sequence[Mapping]) -> dict
    # {"meta": row | None, "splits": row | None, "calls": {seq: row}, "evals": [row, ...], "done": row | None, "problems": [str]}
def join_examples(sidecar: Mapping, call_records: Sequence[Mapping], *, dataset: str, split: str) -> list[dict]     # rows of 2.12, in index order
def call_roles(sidecar: Mapping, call_records: Sequence[Mapping], *, split: str) -> dict[str, tuple[str, str | None]]
    # call_id -> (role, example_id); role is "learn" | "validate" | "test" | "other"
def check_examples(rows: Sequence[Mapping], result: Mapping) -> list[str]      # count and correct must equal the result file
```

Join (deterministic):

1. Group the attempt's call records with `outcome` in `completed`, `replayed`, `coalesced` by `user_sha256`, in `received_utc` order.
2. Walk sidecar `call` rows in `seq` order. A row with `cache_hit: false` and no `error` takes the next unused record with the same `user_sha256`. A row with `cache_hit: true` takes none; it refers to the record first taken for that hash.
3. For each `(split, index)` of the wanted split keep the `eval` row with the highest `pass` for prediction and correctness (it is what the result file reports [F-E1, T-E2]). Its calls are the records reached through the `call_seqs` and `batch_user_sha256` of all its passes (the misses are in pass 1; pass 2 is cache hits). An example with no reachable record, or an `ambiguous` one, gets `matched: false`.
4. Roles: a record attached to an example takes that example's split (`validate` or `test`); an unattached record received before the first wanted-split `eval` row is `learn`; anything else is `other`.
5. `problems`: a call record no sidecar row claimed, a miss with no call record, a `done` row absent (killed worker), an admission-only call record (no `outcome`: the runner died while the request was open; it is never joined as a completed call and its role is `other`).

With no sidecar (capture off, or a worker killed before its first row) `join_examples` returns `[]` and every role is `other`. Never: reads a file; mutates a record.

```python
# transcript.py
@dataclass(frozen=True)
class ToolUse:
    id: str; name: str; input: dict; result_text: str | None; is_error: bool
    started_utc: str | None; ended_utc: str | None
    sandboxed: bool | None                               # the tool-result record's `sandboxed` flag; None when the key is absent
@dataclass(frozen=True)
class ApiMessage:
    message_id: str; request_id: str | None; request_ref: str | None; model: str
    effort: str | None                                   # params.output_config.effort of the joined api-request record
    started_utc: str | None; ended_utc: str | None
    input_tokens_total: int; input_tokens: int; output_tokens: int; cache_read: int; cache_creation: int
    cache_creation_5m: int; cache_creation_1h: int
    text: str; thinking_chars: int; tool_uses: tuple[ToolUse, ...]; stop_reason: str | None
    sidechain: bool; agent_id: str | None
@dataclass(frozen=True)
class NormalizedSession:
    session_id: str | None; source: str                  # "transcript" | "stream"
    cli_version: str | None
    prompt_text: str | None; messages: tuple[ApiMessage, ...]
    started_utc: str | None; ended_utc: str | None
    cost_state: dict | None                              # {"total_cost_micro": int, "model_usage": {...}} from the LAST cost-state record
    skipped_types: dict[str, int]; warnings: tuple[str, ...]

def parse_transcript(lines: Iterable[str]) -> list[dict]                 # tolerant JSONL; a bad line becomes a warning record
def normalize_transcript(records: Sequence[Mapping], *, expect_version: str | None,
                         subagents: Mapping[str, Sequence[Mapping]] | None = None) -> NormalizedSession
    # TranscriptError("version") when a record's `version` differs from expect_version (None switches the gate off; tests only)
def normalize_stream(rows: Sequence[Mapping]) -> NormalizedSession       # fallback from stream.jsonl rows {"t_utc", "stream", "line"}
def read_session_dir(transcript_dir: Path, session_id: str, *, max_bytes: int) -> tuple[list[dict], dict[str, list[dict]]]
def usage_by_model(session: NormalizedSession) -> dict[str, dict[str, int]]
    # model -> {"input", "output", "cache_read", "cache_creation_5m", "cache_creation_1h"}, each message.id counted once,
    # main thread and subagents together
def price_usage(usage: Mapping[str, Mapping[str, int]], list_prices: Mapping) -> int | None
    # micro-dollars at proposer.list_prices (rounded up); None when a model in the usage has no price entry
def bash_results(session: NormalizedSession) -> list[dict]
    # [{"tool_use_id", "sandboxed": bool | None, "sidechain": bool}] for every Bash tool use that has a result
def request_efforts(session: NormalizedSession) -> list[str]             # the effort of each main-thread message's request, in order
```

Rules. Key names are those measured on eleven on-disk transcripts written by CLI 2.1.317 and 2.1.286, none of them a `-p` (`sdk-cli`) session, and the format is documented as internal and free to change in any release [r2], so the shape of a real proposer session is [live: PF10]. That is why parsing is gated on the version and why everything about the format is isolated in this one module.

| Input | Mapping |
|---|---|
| version gate | Every record that has a `version` key must carry `expect_version` (`proposer.cli_version`). One that does not raises `TranscriptError("version")`: the converter fails loudly instead of guessing at another release's shape. The exporter turns that into a `failed` receipt for the session's unit; the proposer runner stops the campaign (`proposer_cli`). |
| records with `type` `assistant`, `user`, `api-request`, `cost-state` | used. Every other type (`attachment`, `api-request-blob`, `api-request-shape`, `queue-operation`, `system`, `file-history-*`, `last-prompt`, `custom-title`, any unknown type) is counted in `skipped_types` and skipped. The set of types is open. **`attachment` records are never read and never exported**: they hold account identifiers (an email address and an organisation id [T-E4]). |
| `assistant` records sharing one `message.id` | one `ApiMessage`. One API response is written as one record per content block, all with the same `message.id` and the same usage; content blocks are concatenated in record order, and usage is taken **once per `message.id`**, from the last record, never summed (measured: 584 of 584 ids with more than one record carry identical counters [r2]). |
| `api-request` records | read for four keys only: `id`, `timestamp`, `params.model`, `params.output_config.effort`. A message is joined to its request by `assistant.requestRef == api-request.id` (1,549 of 1,564 refs resolve in the sample [r2]). The request's `timestamp` is the message's `started_utc`; its effort is the message's `effort`. A message whose `requestRef` is absent or resolves to no record gets `started_utc` = the `timestamp` of its own first record, and a warning. This join replaces the first draft's assumption that the start of a message is the record named by `parentUuid`. |
| `message.model` | `model` (a concrete id; the alias `opus` is not priceable [rep]) |
| `message.usage` | `input_tokens`, `output_tokens`, `cache_read` (`cache_read_input_tokens`), `cache_creation` (`cache_creation_input_tokens`), `cache_creation_5m` and `cache_creation_1h` (`cache_creation.ephemeral_5m_input_tokens`, `ephemeral_1h_input_tokens`; when that object is absent the whole creation count is taken as 1-hour, the dearer price); `input_tokens_total = input_tokens + cache_read + cache_creation` (Agent Lens expects the total [rep AL-V A7]). The transcript's `output_tokens` is the only per-message output count there is: in the stream it is a placeholder. |
| times | `ended_utc` = latest `timestamp` of the group; `started_utc` from the request (above), never later than the end. Transcript timestamps (`YYYY-MM-DDTHH:MM:SS.mmmZ`) are re-formatted with `util.utc_iso`. |
| `text` blocks | appended to `text`. `thinking` blocks add their length to `thinking_chars`; their text is never kept. |
| `tool_use` block (`id`, `name`, `input`) | a `ToolUse` with `started_utc` = the record's `timestamp`. A `user` record's `tool_result` block (`tool_use_id`, `content`, `is_error`) completes it: `result_text`, `is_error`, `ended_utc` = that record's `timestamp`, and `sandboxed` = that record's `sandboxed` key (`None` when the record has none). |
| the first `user` record with string content | `prompt_text` |
| `isSidechain: true` records and files under `<session>/subagents/agent-<id>.jsonl` | messages with `sidechain: true` and `agent_id` = the record's `agentId`, else the file's `<id>`; attached to the `Agent` tool use that started them when the id is resolvable (the tool result's `toolUseResult.agentId`), otherwise listed in `warnings`. The `.meta.json` and `.prefix.json` sidecar files are not read. |
| `cost-state` records | the **last** one gives `cost_state` (`totalCostUSD` as micro-dollars, `modelUsage` as it is). It is written only when the CLI exits normally; a resumed session has several. |

`normalize_stream` builds the same structure from our stamped stream when no transcript was found: `assistant` events with the same `message.id` are grouped the same way, all times are arrival times, `output_tokens` is left at 0 with the warning `stream_output_tokens_are_placeholders` (the real output counts are only on the `result` event), and `cost_state` is `None`. A transcript with records but no usable timestamps yields no messages and a warning; times are never invented.

### 3.22 `qualification.py` (P), `preflight.py` (U), `audit.py` (P)

```python
# qualification.py   (P: H2 qualification.py L23-L68; forecast pattern of v4.py L258-L352; engine snapshot v4.py L175-L255)
def engine_snapshot(package_dir: Path) -> dict
    # {"engine_sha256", "files": {rel: sha256}} over mhforge/**/*.py except audit.py, publish.py and the telemetry package;
    # telemetry/transcript.py IS included: the proposer runner uses it for the sandbox proof and the cost fallback
def offline_gate(manifest: dict, paths: StatePaths, sc_dir: Path, *, command: str) -> list[dict]     # [{"name", "ok", "detail"}]
def protocol_stage(sc_dir: Path) -> int            # 0: TEMPLATE-UNFILLED present; 1: stage 1 signed, STAGE2-UNSIGNED present; 2: both signed
def observations(call_records: Sequence[Mapping]) -> dict
def checks(manifest: dict, job_record: Mapping, obs: Mapping, audit_problems: Sequence[str],
           example_rows: Sequence[Mapping]) -> list[dict]
def forecast(manifest: dict, obs: Mapping) -> dict             # pure
def next_attempt(paths: StatePaths) -> int
def record(paths: StatePaths, attempt: int, *, engine: Mapping, manifest_sha256: str, check_list: Sequence[Mapping],
           obs: Mapping, fc: Mapping, environment: Mapping | None = None,
           imported_from: Mapping | None = None) -> dict        # writes qualification.json, forecast.json; passed.json when everything passed
def import_problems(manifest: dict, paths: StatePaths, engine: Mapping) -> list[str]
    # why this campaign may not import the qualification of the campaign named by qualification.import_from; [] = it may
def verify_qualified(paths: StatePaths, manifest: dict, engine: Mapping) -> None        # CampaignStop("qualification_failed")
def qualified_environment(paths: StatePaths) -> dict | None                             # passed.json["environment"], or None
def noise_stats(sidecars: Mapping[str, Mapping[str, Mapping[int, Sequence[Mapping]]]], results: Mapping) -> dict
    # pure: system -> dataset -> repeat -> sidecar rows, plus the result files; returns the body of noise.json (2.4)
def restart_allowed(paths: StatePaths, ledger_snapshot: Mapping, engine_now: Mapping) -> list[str]   # reasons a restart is refused
```

- `offline_gate` is the qualification gate before spend. It costs nothing and touches no network. `sc_dir` is the manifest's directory. Checks: the manifest is frozen and unchanged; `workspace.source_digest(EX)[0] == upstream.tree_sha256` (the pinned source tree is what the manifest says; this can be evaluated before any runner command has created the workspace), and, when `WS` already exists, `workspace.verify_pinned(paths)` is empty; `sha256(sc_dir/worker-image/uv.lock)` equals `upstream.worker_lock_sha256` (sandbox executor); `sc_dir/state/_smoke/receipt.json` exists and its `engine_sha256` equals `engine_snapshot` now (a passing offline smoke for this exact engine); the proposer binary is present with the pinned hash (`search` only); no `unknown` ledger row (reconcile it first, 3.6); `protocol_stage(sc_dir) >= 1` (`sc_dir/protocol.md` exists and no longer contains the marker `TEMPLATE-UNFILLED`), and for `search` in a measured campaign `protocol_stage(sc_dir) == 2` (the marker `STAGE2-UNSIGNED` is gone too: the job limit, the parallelism and the proposer model are decided only after qualification and the first real proposer session, so the protocol is signed in two stages and the gate checks the stage that can exist [r2]); with the sandbox executor in mode `wheelhouse`: the file at `sandbox.wheelhouse.tar_path` exists and its SHA-256 and size equal `tar_sha256` and `tar_bytes`, and the SHA-256 of `sc_dir/worker-image/requirements.lock.txt` equals `sandbox.wheelhouse.requirements_sha256`. Any failed check refuses the live command (it is passed to `manifest.live_blockers` as `gate_problems`).
- `observations` (from the qualification job's call records and job record): completion-token quantiles, latency quantiles, time to first byte, the rate of `truncated_empty`, the set of returned model ids, the usage keys seen, the reasoning field name seen, the count of calls without usage, the largest `prompt_tokens / input_limit` ratio, the number of `limit_exceeded` ledger rows, the number of calls, `job_seconds` (the qualification job's wall time in its executor), the queue-wait quantiles, and the p95 call latency (what the decision about `executor.parallel_dataset_jobs.validation` needs: three validation jobs at once are safe only if `ceil(94 / gateway.max_in_flight) * p95 latency` stays under the client's 600 s [r2]).
- `checks`: every call `completed`; usage on every call; `prompt_tokens <= input_limit` on every call (no `limit_exceeded` row); `truncated_empty` rate at most `qualification.max_truncated_empty_rate`; exactly one returned model id; the job's outcome is `ok`; every example row is `matched`; `audit` is clean.
- `forecast` checks every budget that can end the search, not only the solver's:
  - Solver. For each split and dataset, `cost_job = ceil(p_in * forecast.input_tokens_per_job[split][dataset] + p_out * forecast.calls_per_job[split][dataset] * q)` micro-dollars, with `q` the `forecast.output_tokens_quantile` of observed completion tokens. `validation_total = (len(baselines) + search.iterations * forecast.candidates_per_iteration) * sum(cost_job[val])`; `test_total = forecast.test_systems * sum(cost_job[test])`; both multiplied by `accounting.forecast_contingency`. `calibration_total = contingency * (len(calibration systems) * sum(cost_job[test]) + (calibration.noise_repeats - 1) * len(baselines) * sum(cost_job[val]))`. Fits when `validation_total <= cap(baseline) + cap(search)`, `test_total <= cap(test)` and `calibration_total <= cap(calibration)`. The `controls` cap is not forecast (its runner is deferred).
  - Sandbox (only with `executor.kind == "sandbox"`). `job_s[split][dataset] = max(forecast.job_seconds[split][dataset], obs.job_seconds * forecast.calls_per_job[split][dataset] / obs.calls)`; `setup_s` is `sandbox.setup_allowance_s` in mode `wheelhouse`, else 0. `sandbox_total_micro = contingency * (sum over the same job multiset of executors.sandbox_cost_micro((job_s + setup_s) * 1000, ...) + search.iterations * sandbox_cost_micro((300 + setup_s) * 1000, ...))` (the last term is the import-check sandboxes). Fits when `sandbox_total_micro <= micros(budgets.sandbox_usd)`. The central estimate is $7.50 of sandbox time for 186 sandboxes, the slow case about $30, against the $40 cap [r2]; with no gate, a slow search would end at the cap with nothing having said so [A-M].
  - Proposer. `proposer_total_micro = contingency * ceil(1.5 * search.iterations) * micros(proposer.session_estimate_usd)` (25 to 35 sessions are expected for 20 iterations, counting reruns [r2]); fits when it is at most `micros(budgets.proposer_list_usd)`. This is real spend on a usage-billed seat.
  - It returns `{"per_job", "validation_total_micro", "test_total_micro", "sandbox_total_micro", "proposer_total_micro", "calibration_total_micro", "fits_solver", "fits_sandbox", "fits_proposer", "fits": bool}`; the run may proceed only if all three fit. Otherwise the caller writes `forecast.json` and raises `CampaignStop("forecast_exceeds_budget")`: stop, do not shrink the design [rep H2]. Every constant comes from the manifest (H2 had them as literals).
- `verify_qualified`: `passed.json` exists and its `manifest_sha256` and `engine_sha256` equal the current ones; when it has `imported_from`, `import_problems` must still return none. A changed engine needs a new qualification attempt.
- **Qualification import** (`import_problems`). Values learned during the preflight (the gateway cap, the timeouts, the job limit, the parallelism, the budgets, the proposer model and its per-session cap) cannot be written into the manifest that was frozen before the preflight, and re-running the whole preflight, paid proposer session included, inside the measured project would be waste and would leave preflight spans in a project whose publish check requires every conversation to be ours [r2]. So the preflight runs in a preflight campaign and a scratch project, and the measured campaign, in a fresh project, imports its result. The import is allowed when all of these hold; each failure is one message:
  - this campaign is `measured` and `qualification.import_from` names a campaign under the same `SC` whose frozen manifest is readable and is `preflight`;
  - the source's `qualification/passed.json` exists, its `manifest_sha256` equals the source's frozen manifest hash, and its `engine_sha256` equals the engine hash **now** (the code that will spend is the code that was qualified);
  - `decisions.project` differs between the two manifests (a fresh project);
  - the source's reconciliation record (`reconciliation.json` of its last preflight attempt, PF-R) has verdict `pass` or `waived`;
  - every path of `manifest.diff_paths(source manifest, this manifest)` is allowed by `manifest.IMPORT_DIFF_ALLOWED`. That named set is: `notes`, `campaign`, `campaign_kind`, `state_dir`, `qualification.import_from`; `decisions.project`, `decisions.publish_approved`, `decisions.evaltables_backend`, `decisions.dataset_text_publication`, `decisions.uspto_text_cleared`; everything under `budgets.`; `accounting.reconcile_tolerance`; `gateway.max_in_flight`; the time limits `search.propose_timeout_s`, `executor.job_timeout_s`, `executor.import_check_timeout_s`, `provider.connect_timeout_s`, `provider.first_byte_timeout_s`, `provider.idle_timeout_s`, `provider.attempt_deadline_s`, `sandbox.create_wait_s`, `sandbox.lifetime_margin_s`, `sandbox.setup_allowance_s`, `relay.reply_timeout_s`, `telemetry.export_timeout_s`; `search.iterations`; everything under `executor.parallel_dataset_jobs.`; `provider.expected_response_model`; `proposer.model`, `proposer.expected_resolved_model`, `proposer.max_budget_usd_per_session`, `proposer.session_estimate_usd`, `proposer.max_limit_wait_s`, `proposer.transient_retries`, `proposer.transient_backoff_s` and everything under `proposer.list_prices.`; everything under `telemetry.content.`; `telemetry.agent_lens.byte_estimate_factor`, `telemetry.export_mode`, `telemetry.wandb_group`; everything under `calibration.`, `forecast.` and `runner.`. Anything else that differs (the upstream pin, the route, the provider, the prices, the gateway's request bounds, the executor kind, the sandbox image or wheelhouse, the relay mode, the permission mode, the CLI binary and version, the settings override, the entity) means a different experiment and needs its own qualification.
  When the import is allowed, `preflight --live` in the measured campaign runs only the checks marked `per_project` (the account reads and the project creation are `account-check`; then one inference call, one sandbox with the selfcheck, and the 3-row table), and `record` writes this campaign's own `passed.json` with `imported_from: {"campaign", "manifest_sha256", "diff_paths"}` and the source's `environment`. The calibration verdict is read from the source when this campaign has none (2.3).
- `passed.json["environment"]` is the environment identity recorded at qualification (2.6: image digest, hashed requirements file, wheelhouse `SHA256SUMS`, measured freeze hash, machine, Python version). `qualified_environment` returns it; the job runner hands its freeze hash to every later job file, so a sandbox whose package set differs from the qualified one never starts a worker (3.17).
- `noise_stats` is pure: for each system and dataset it takes, per repeat, the `eval` rows of the wanted split with the highest pass (as the join does), and computes the correct count per repeat, the flip rate, the count of examples on which all repeats agree, the macro average per repeat and its standard deviation.
- `restart_allowed` (H2 rule): only after a recorded stop in the qualification phase, with no `pending` or `unknown` ledger row (an `unknown` row is closed with `status --reconcile`), with at least one engine file changed; the same qualification cap applies to all attempts together.

```python
# preflight.py   (U: every live check is UNVERIFIED; offline only describe(), the pure builders and the offline checks are exercised)
@dataclass(frozen=True)
class Check:
    step: str                                       # "PF0" .. "PF11", "PF-R": the step of 8.2 the check belongs to
    name: str; live: bool; required: bool
    per_project: bool                               # run again, cheaply, by a campaign that imports its qualification
    needs: str                                      # what it creates or spends
    describe: str
    run: Callable[["Context"], dict]                # returns a receipt body {"passed": bool, ...}
class AccountReader(Protocol):                      # the account reads of PF1 to PF4
    def server_features(self) -> dict[str, bool]: ...                       # anonymous
    def default_entity(self) -> str | None: ...
    def entity_org(self, entity: str) -> dict | None: ...                   # {"org", "is_team", "members": int | None, "default_access": str | None}
    def org_flags(self, org: str, names: Sequence[str]) -> dict[str, bool | None]: ...
    def subscription(self, org: str) -> dict | None: ...                    # {"plan", "privileges", "usage"}; None when the role may not read it
    def project(self, entity: str, name: str) -> dict | None: ...           # {"access", "read_only", "can_write"}; None when it does not exist
    def create_project_private(self, entity: str, name: str) -> None: ...   # upsertModel with access "PRIVATE"
class WandbAccountReader:                           # (U) the live AccountReader: lazy `import wandb`, wandb.Api(api_key=<key>) and its
    def __init__(self, *, api_key: str | None, user_agent: str) -> None     # GraphQL client; api_key None = anonymous reads only; never wandb.login
@dataclass
class Context:                                      # built by the CLI; preflight.py imports nothing from driver.py
    manifest: dict; paths: StatePaths; environ: Mapping[str, str]; sc_dir: Path; attempt: int
    gateway: HandlesRequests | None; ledger: Ledger | None; executor: Executor | None      # None for account-check
    sandbox_client: SandboxClient | None; lens: LensSink | None; wandb: WandbSink | None
    account: AccountReader | None
    run_qualification_job: Callable[[], dict] | None    # Driver.run_qualification_job
    run_baselines: Callable[[], dict] | None            # Driver.run_baselines
    run_calibration: Callable[[], dict] | None          # Driver.run_calibration
    proposer_session: Callable[..., dict] | None
        # (prompt, timeout_s, *, model, effort, max_budget_usd, workspace, break_settings=False) -> session record; one real session
        # through ProposerRunner against a throwaway state directory SC/state/_probe-<utc>/ (below)
    user_figures: Mapping[str, str]                 # figures the user typed on the command line (Claude headroom, platform usage)
    hostname: str                                   # read by the CLI and passed in as data, for the privacy scan of PF8
ACCOUNT_STEPS: tuple[Check, ...]                    # PF1 .. PF4; run by `account-check`, which needs no frozen manifest
CHECKS: tuple[Check, ...]                           # PF0, PF5 .. PF9, `rehearsal_review` (PF11; it also judges PF10 from the records), PF-R;
                                                    # ordered; section 8.2 lists them
TEARDOWN: tuple[dict, ...]                          # the teardown list of 8.2 as data: {"item", "how", "automated": bool}
def describe() -> list[dict]                        # [{"step", "name", "live", "required", "per_project", "needs", "describe"}]; account steps first
def account_check(ctx: Context, *, create_project: bool) -> dict
    # runs ACCOUNT_STEPS in order, rewrites STATE/account/account.json after each step, stops at the first failed step
def run_checks(ctx: Context, only: Sequence[str] | None = None) -> dict
    # runs in order, writes qualification/attempt-<n>/<name>.json, stops at the first failed required check. In a campaign with
    # qualification.import_from it first requires qualification.import_problems(...) == [] and then runs only the per_project checks
def build_probe_script(*, canary_denied: Path, credential_paths: Sequence[str], scratch: Path, loopback_port: int) -> str
    # pure: the one shell script of PF9; every probe line ends in `; echo "<name> $?"`, so the session reports exit codes and nothing else
def probe_expectations(mode: str) -> dict[str, str]     # probe name -> "zero" | "nonzero" | "record", per permission mode
def reconciliation(ledger_snapshot: Mapping, figures: Mapping[str, str], manifest: dict) -> dict      # pure; the verdict of PF-R
```

`account_check` is what `account-check --live` runs (3.27). It runs before any freeze, creates nothing except, with `--create-project`, the project, and writes what it learns to `STATE/account/account.json`: the feature switches, the entity's organisation and whether the entity is a team, the plan and its allowances when the role may read them (unreadable is recorded as such and is not a failure), the team's member count and default project visibility, whether the project exists, the result of the sandbox list, the Claude login method and the headroom figures the user typed. **Account facts stay there.** They are never copied into the manifest, a run config, a plan, a span attribute or the publication bundle: a private project is readable by every member of the team, and plan and privilege details are not ours to publish.

`build_probe_script` exists because the first plan of the proposer probes told the session to Read, `cat` and Python-`open` the user's real credential files and expect a denial; had the denial failed, which is what the probe is there to find out, the user's real key would have entered the model context, the transcript and the copied evidence [r2]. So the probes are **status-only**. A probe session runs through the real `ProposerRunner`, but against a throwaway state directory `SC/state/_probe-<utc>/` with its own `workspace/text_classification` copy: a sibling of the campaign's state directory, so the settings generated for the probe are the real generator's output and deny the real campaign state as a sibling. The driver writes one script into that workspace; each line tries one thing in a way that produces no content (`test -r <path>`, `wc -c < <path> > /dev/null`, `python3 -c "open('<path>').close()"`, `touch`, `curl -s -o /dev/null`, `security find-generic-password -s "Claude Code-credentials" > /dev/null`) and prints only the probe's name and exit code. The Read tool, which cannot be made status-only, is tested against a **canary file** the driver creates under a path that the same generated rule list denies (a child of `STATE` other than `workspace`), never against a real credential. Afterwards the driver removes what the probes may have created (`~/mhf-probe`, the scratch files, the probe agent file) and deletes the throwaway state directory. Probe sessions are metered in the campaign's own ledger (`proposer_list_price`, keyed `probe.<n>`), so their spend counts against the proposer ceiling.

```python
# audit.py   (P: H2 audit.py L46-L115)
def audit(manifest: dict, paths: StatePaths) -> dict
    # {"ok": bool, "problems": [str], "calls": int, "observed_micro": int, "bounded_unknown_micro": int}
```

`audit` is read-only (it opens the ledger with `util.open_sqlite_ro`) and checks: the totals recomputed from `calls` equal `phase_totals` and `job_counters.committed_micro` (the same sums `Ledger.verify_totals()` computes, written here as SQL from 2.10); every `complete` ledger row's cost recomputed from `usage_json` and the manifest prices equals `spent_micro`; every ledger row has an attempt in a call record and the reverse; every provider attempt has its raw `.sse` file; no `pending` row (after a clean stop there is none, because the stop path drains; after a second signal or a crash the next start has turned them into `bounded_unknown`); every `replayed` or `coalesced` record points at an existing paying call and has no ledger row; per job attempt, the outcome counts in `job.json` plus `in_flight_at_close` (attempts that settled after the job was closed) equal the counts from the call records, and the refused count equals the refused records plus `refused_unrecorded`; every succeeded job has a usable result whose `correct` and `total` equal the joined examples when a sidecar exists, and no tainted refusal code in its attempt; no call record has a reply status outside 200, 402, 403; no record under `calls/` or `jobs/` matches a `mhforge.scan` pattern of class `secret` or a key digest (the `privacy` class is not applied to state records, which hold local paths by design); every `verified` receipt has stored spans whose count equals `expected_spans`. It lists, without failing: every `reconciled` row, every `limit_exceeded` row, every call record with `refusal.detail == "repeatable_provider_failure"`, every admission-only call record, and every meter row still `open`.

### 3.23 `telemetry/plan_spans.py`, `plan_tables.py`, `plan_runs.py`, `readback.py` (pure)

No SDK import, no clock, no network, no environment; deterministic for the same inputs (the same inputs give the same bytes).

```python
# plan_spans.py
def conversation_url(project: str, conversation_id: str) -> str
    # https://forge.coreweave.com/agent-lens/<entity>/<project>/conversations/<id>
def plan_solver_job(job_record: Mapping, attempt: int, call_records: Sequence[Mapping], sidecar_rows: Sequence[Mapping],
                    manifest: dict, *, run_path: str | None = None, export_attempt: int = 1) -> dict
def plan_proposer(session_record: Mapping, session: NormalizedSession, manifest: dict, *, roots: Mapping[str, Path],
                  export_attempt: int = 1) -> dict
def plan_outer_iteration(iteration_record: Mapping, journal_entries: Sequence[Mapping], manifest: dict, *,
                         verbatim: "scan.VerbatimIndex | None" = None, export_attempt: int = 1) -> dict
def validate_plan(plan: Mapping, manifest: dict) -> list[str]
```

- **Solver job** (decision D5). One conversation per job attempt, `<campaign>.<job_id>`. Turns by `join.call_roles`: `learn` (if any), `validate` or `test`, `other` (if any); a turn carries no content, only the counts `examples` and `correct` as attributes (2.13). One `llm` span per call record of the attempt, every outcome. Prompt, reply and reasoning text pass through the content rules of the **job's dataset** (`policy.rules_for(manifest, dataset)`; `hash` by default). Usage only on `completed` spans. `replayed`, `coalesced` and `refused` spans carry `usage: null` and their text under the rule `hash` whatever the policy (they made no provider request for this job; their bytes should not be billed twice; a refused record holds only a `request_excerpt`, whose hash and length are used); `refused`, `provider_rejected` and `failed` spans carry `error`. An admission-only call record (no `outcome`; the runner died while the request was open) is planned as outcome `interrupted`: start = `received_utc`, end = the end of its job attempt, `usage: null`, `error = {"type": "interrupted", "message": "runner stopped while the request was open"}`, text under the rule `hash`. Per-span attributes: `call_id`, `example_id` (when attached), `outcome`, `usage_available`, `provider_attempts`, `truncated_empty`, `content_truncated`. `reasoning` from the merged message becomes a `reasoning` part. Span times are the record's `received_utc` and `ended_utc`; a turn spans its children.
- **Proposer session.** Conversation `<campaign>.proposer.<session_key>` with one turn that carries no content (the task prompt's hash and length as the attributes `prompt_sha256` and `prompt_chars`, never its text: the record does not hold it). One `llm` span per main-thread `ApiMessage` with `response_model` = the concrete model and `usage.input_tokens` = `input_tokens_total`, `cache_read_input_tokens`, `cache_creation_input_tokens`; one `tool` span per `ToolUse`; sidechain messages grouped by `agent_id` as one nested `subagent` each. **An `llm` span never carries cumulative context**: its `input_messages` is empty and its output parts are that one message's own text; what the model was given is the tool results before it, each of which has its own `tool` span. Repeating the conversation so far in every span grows quadratically, to the order of a gigabyte for twenty sessions [r2]. Tool arguments and results (megabyte log files; `log.jsonl`, which the skill tells the proposer to read, holds training text) are prepared in this order: `policy.portable` with `roots` (so no `/Users/<name>/` path survives a non-`hash` rule), `policy.mask_hex40` (3.24), then the content rule `tool_result` of the dataset the tool call is attributable to (`policy.attribute_dataset` on the call's arguments), or the strictest rule of all datasets when it names none or more than one. Message text uses `span_reply` and `span_reasoning` of the strictest rule set: a proposer message is not attributable to one dataset. A session with no `result` event carries the turn error `{"type": "killed"}`. Parts as above.
- **Outer loop.** Conversation `<campaign>.search.p<n>` with `n = 1 + NNN // telemetry.agent_lens.loop_iterations_per_conversation`; one unit and one turn per iteration (iteration 0 = baselines); `tool` spans `propose`, `import_check:<candidate>`, `benchmark:<candidate>`, `frontier_update`, timed from the journal; arguments and results are small JSON summaries (scores, statuses, cost), never file contents; each string in them (a hypothesis, an axis, a component tag: text the proposer wrote) passes through `policy.guard_text` with the verbatim index, so a string that quotes dataset text is replaced by its `hash` form, and then through the strictest `tool_result` rule. A resumed search adds turns by reusing the conversation id [rep AL]. An iteration has at most `3 + 2 * search.max_candidates_per_iteration` spans including its turn (candidates beyond the limit are rejected before any import check or benchmark), and validation rule 13 makes `loop_iterations_per_conversation` such iterations fit one conversation. The outer-loop conversation is shared by many units, each of which sees only its own turn, so no per-unit check can enforce its limit; the roll-over is what does. With the draft numbers one unsplit conversation would have held 4 + 20 x 9 = 184 spans, above the manifest's own 180, and 202 at 22 iterations, where the findings report HTTP 413 [A-S b_misc].
- **Split rule, every plan kind.** A conversation holds at most `telemetry.agent_lens.max_spans_per_conversation` spans including turns (the service shows 200 per conversation and long conversations hit an open 413 bug [rep]).
  - Solver job: the `learn` turn moves to its own conversation `...learn` when the total would exceed the limit; a turn that alone exceeds it is split, in call order, into conversations `.p2`, `.p3`, each with one turn of the same key.
  - Proposer session: a real session is one turn with a span per API message and per tool call plus nested subagent spans (the example of 2.11 has 87 turns of the CLI), so it normally exceeds the limit [inf from that example]. It is cut in message order into parts `<id>`, `<id>.p2`, `<id>.p3`, ...; each part is its own conversation with one turn (turn attributes carry `part` and `parts`). A message's `llm` span and the `tool` spans of its tool uses stay in one part. A `subagent` span and its children stay in one part; a subagent that alone exceeds the limit becomes its own run of parts in which its children are flattened to top-level spans with the attribute `subagent` = its agent id.
  - Every part's id passes through `wire.bounded_id`.
- `validate_plan`: `policy.check_attributes` on every attribute set; usage only on `llm` spans; the span limit for every conversation of the plan; conversation ids URL-safe and at most 128 characters; every span inside its turn's time range and `started_utc <= ended_utc`; `expected_spans` equals turns plus spans; no span text longer than the policy allows; an outer-loop plan has exactly one turn with at most `3 + 2 * search.max_candidates_per_iteration` spans including the turn.
- **Unset decisions.** When `decisions.entity` or `decisions.project` is null every planner uses the project `unset-entity/unset-project` and sets `placeholder_project: true` (2.13). It never raises for that.

```python
# plan_tables.py
def plan_tables(candidate: str, split: str, job_records: Mapping[str, Mapping], examples: Mapping[str, Sequence[Mapping]],
                results: Mapping[str, Mapping], manifest: dict, *, span_urls: Mapping[str, str], backend: str,
                export_attempt: int = 1) -> dict                # the tables.json object of 2.14; keys of the mappings are datasets
def check_table(table_plan: Mapping) -> dict                    # returns the "checks" object; raises nothing
```

One table per succeeded job, key `<split>/<dataset>`, inside the candidate's evaluate run (decision D8). Inputs `dataset`, `split`, `example_id`, `question_sha256`. Scores: `was_correct` (bool) always, so the panel's fraction true equals upstream's per-dataset accuracy; and exactly these four metrics, each when every row has a finite number: `jaccard_similarity` (USPTO) and `f1`, `precision`, `recall` (LawBench) (the evaluators return them as floats, `data/evaluators.py` L62-L76, L99-L129 [rep]). No other metric is promoted, whatever its type; LawBench's integer `tp`, `fp`, `fn` stay in `metrics_json`. `backend` is the value of `decisions.evaltables_backend` (`ces` or `wandb_table`), or `"unset"` when the decision is unset; with `off` no table plan is made. Outputs: `prediction` and `target` as short labels, or `prediction_sha256` and `target_sha256` for a dataset of `wire.CLEARANCE_GATED` whose decision is not true (the rule of 2.14; `LABEL_MAX_CHARS = 200`), then `n_calls`, `input_tokens`, `output_tokens`, `latency_s`, `finish_reason`, `truncated_empty`, `row_complete` (the example's `matched`), `metrics_json`, `trace_url` (the conversation URL of the job's verified Agent Lens receipt, else `""`). `checks.matches_result_file` is false, and the table is dropped with the reason recorded, when the count of true `was_correct` rows differs from `correct` in the result file: the sidecar is untrusted. A failed job has no table. No value of `decisions.dataset_text_publication` adds an input-text column: a table never holds question text.

```python
# plan_runs.py
def run_id(campaign: str, kind: str, *, candidate: str | None = None, split: str | None = None, export_attempt: int = 1) -> str
RUN_CONFIG_ALLOW: tuple[str, ...]                               # the dotted manifest paths a run config may hold (below)
def plan_evaluate_run(candidate: str, split: str, job_records: Mapping[str, Mapping], table_keys: Sequence[str], manifest: dict, *,
                      proposal_entry: Mapping | None, conversation_urls: Mapping[str, str], is_baseline: bool,
                      evidence: Mapping[str, Mapping], withheld: Mapping[str, Mapping] | None = None,
                      verbatim: "scan.VerbatimIndex | None" = None, export_attempt: int = 1) -> dict
    # evidence: dataset -> {"memory_json_sha256", "memory_json_bytes", "log_jsonl_sha256", "log_jsonl_bytes"} (hashes and sizes only)
    # withheld: candidate file name -> {"sha256", "bytes"} for files the verbatim scan kept out (the Artifact then has no file)
def plan_propose_run(candidate_entry: Mapping, session_record: Mapping, manifest: dict, *,
                     withheld: Mapping[str, Mapping] | None = None, verbatim: "scan.VerbatimIndex | None" = None,
                     export_attempt: int = 1) -> dict
def plan_search_step(iteration_record: Mapping, earlier_records: Sequence[Mapping], manifest: dict, *,
                     export_attempt: int = 1) -> dict
    # earlier_records: the iteration records with a lower iteration number; cumulative figures are sums over them and this one
def search_metrics(iteration_records: Sequence[Mapping], manifest: dict) -> list[dict]
```

Run kinds, ids and content are the table of 2.15 (decision D7). `run_id` returns `wire.bounded_id(...)` of the shapes of 2.2. Every run `config` is `manifest.run_config(manifest, RUN_CONFIG_ALLOW)` passed through `policy.portable`. `RUN_CONFIG_ALLOW` is: `campaign`, `campaign_kind`, `upstream.repo`, `upstream.commit`, `upstream.tree_sha256`, `upstream.worker_lock_sha256`, `upstream.expect`, `search`, `routing`, `provider.base_url`, `provider.wire_model`, `provider.expected_response_model`, `prices`, `gateway.max_in_flight`, `gateway.max_output_tokens`, `budgets`, `accounting`, `calibration`, `executor.kind`, `executor.max_attempts`, `executor.max_infra_attempts`, `executor.job_timeout_s`, `executor.parallel_dataset_jobs`, `sandbox.env_mode`, `sandbox.image`, `sandbox.base_image`, `sandbox.wheelhouse.requirements_sha256`, `sandbox.wheelhouse.sha256sums_sha256`, `sandbox.cpu`, `sandbox.memory`, `capture`, `decisions.proposer_permission_mode`, `decisions.evaltables_backend`, `decisions.dataset_text_publication`, `proposer.cli_sha256`, `proposer.cli_version`, `proposer.model`, `proposer.effort`, `proposer.max_budget_usd_per_session`, `telemetry.content`. Nothing else: no path on this machine, no key variable name, no account fact (2.15). Artifact metadata strings (`hypothesis`, `axis`, `components`) pass through `policy.guard_text` with the verbatim index. `plan_search_step` takes **no ledger snapshot**: its cumulative solver, sandbox and proposer figures are sums of `solver_cost_micro`, `sandbox_cost_micro` and `proposer_list_price_micro` over the iteration records up to this one, so the plan of a settled iteration is the same bytes whenever export runs. With a live ledger snapshot the plan of `loop:iter003` changed every time export ran again after more spend, and what to do with a final receipt whose plan hash had changed was undefined [A-S]. `search_metrics` computes our own figures (decision D14): per candidate the average over all manifest datasets with a missing dataset counted as 0 and the candidate marked incomplete; the improvement over the best complete system before the iteration; the list of failed (candidate, dataset) jobs. Search-run history keys: `search/upstream_frontier_val`, `search/best_complete_avg_val`, `search/improvement`, `search/candidates_proposed`, `search/candidates_valid`, `search/candidates_complete`, `search/jobs_failed`, `search/solver_cost_usd`, `search/sandbox_usd`, `search/proposer_list_usd` (the three cost keys are cumulative), `search/context_chars_best` and `search/prompt_tokens_max_best` (the `context` figures of the best complete candidate of the iteration, 2.11), `search/evidence_exposed` (0 or 1), `search/propose_s`, `search/bench_s`, logged together with the `iteration` key that is their step metric (2.15). Bounded-unknown spend is a property of the ledger at a point in time, not of an iteration; it is reported by `status`, `audit` and the publication bundle, not in this run.

```python
# readback.py   (P: H2 telemetry.py L137-L170, L201-L299)
READBACK_URL = "https://trace.wandb.ai/agents/spans/query"
def spans_query_body(project: str, trace_id: str, *, limit: int = 200, offset: int = 0) -> dict
    # {"project_id": project, "query": {"$expr": {"$eq": [{"$getField": "trace_id"}, {"$literal": trace_id}]}},
    #  "include_details": true, "include_costs": true, "limit": limit, "offset": offset}
def conversation_ids_body(project: str, *, limit: int = 10000, offset: int = 0) -> dict     # include_details false; for the ownership check
def verify_spans(plan: Mapping, rows: Sequence[Mapping]) -> list[str]
```

`verify_spans` matches rows to planned spans on the custom attribute `<prefix>.call_id` (or the tool call id, or the turn's operation name) and compares models, token counts, finish reasons, times within 1 ms and message digests, allowing the two known normalisations (empty arguments become `""`, a missing response model becomes `""` [rep]). The request shapes are pinned by tests: the `/agents/*` endpoints are not yet an official public API [rep].

### 3.24 `scan.py` (layer 0) and `telemetry/policy.py` (P: `portable` from H2 `report_v4.py` L505-L511) (pure)

`scan.py` is `mhforge/scan.py`, owned by `L0-core`: manifest validation (layer 1), the exporter, `audit`, `publish` and the smoke all use it, so it cannot live in the telemetry package of a parallel or later owner.

```python
# scan.py
@dataclass(frozen=True)
class Finding:
    pattern: str; path: str; offset: int; length: int           # never the matched text
SCAN_CLASSES = ("secret", "privacy")
PATTERNS: dict[str, re.Pattern[bytes]]                           # class "secret": the eleven rules below
PRIVACY_PATTERNS: dict[str, re.Pattern[bytes]]                   # class "privacy": the two local-path rules below
def load_digests(path: Path) -> list[tuple[int, str]]            # (length, sha256) pairs from STATE/keydigests.json; [] when the file is absent
def scan_bytes(data: bytes, *, known: Sequence[bytes] = (), digests: Sequence[tuple[int, str]] = (), path: str = "",
               classes: Sequence[str] = ("secret",), hostnames: Sequence[str] = ()) -> list[Finding]
def scan_obj(value: object, *, known: Sequence[bytes] = (), digests: Sequence[tuple[int, str]] = (), path: str = "",
             classes: Sequence[str] = ("secret",), hostnames: Sequence[str] = ()) -> list[Finding]
    # walks dict, list, str
def scan_paths(paths: Iterable[Path], *, known: Sequence[bytes] = (), digests: Sequence[tuple[int, str]] = (),
               classes: Sequence[str] = ("secret",), hostnames: Sequence[str] = ()) -> list[Finding]

@dataclass(frozen=True)
class VerbatimIndex:                                             # dataset text to look for; built from data the caller passes in
    run_chars: int
    run_hashes: frozenset[bytes]         # first 8 bytes of the SHA-256 of every run_chars-character window of every run text
    exact: tuple[str, ...]               # whole strings looked for as they are (each at least 8 characters)
def build_verbatim_index(run_texts: Iterable[str], exact_texts: Iterable[str] = (), *, run_chars: int = 40) -> VerbatimIndex
def scan_verbatim(text: str, index: VerbatimIndex, *, path: str = "") -> list[Finding]
    # pattern "dataset_text_run": a window of the text is in run_hashes; "dataset_text_exact": an exact string occurs in the text
```

`scan.py` reads nothing from the machine: no host name, no user name, no dataset file. Whatever it needs beyond its own patterns (key values, key digests, the host name, the dataset text to index) is passed in as data by a caller.

`PATTERNS`, class `secret` (eleven rules):

- `job_token`: `mhfjob_[A-Za-z0-9_-]{43}`
- `bearer`: `[Bb]earer\s+[A-Za-z0-9._~+/-]{20,}`
- `authorization_header`: `(?i)authorization\s*[:=]\s*\S{8,}`
- `wandb_key`: `wandb_v1_[A-Za-z0-9_-]{20,}`
- `keyed_hex40`, exactly: `(?i)(?<![a-z0-9])(?:api[_-]?key|password|passwd|secret|token)["']?\s*[:=\s]\s*["']?(?<![0-9a-f])[0-9a-f]{40}(?![0-9a-f])`. The key name must end the word (`tokenizer_file`, `token_sha256` and `tiktoken` do not match), it may be in any letter case and may be the tail of a longer name (`MHFORGE_FORGE_API_KEY=`, `WANDB_API_KEY:`), and the value is exactly 40 hex characters (a 64-hex digest does not match). The first draft said "40 hex within 40 characters after `token`": the job file's own field `"tokenizer_file": "<40 hex>"` matched it, so every `jobfile.json` was a finding, `audit` and the smoke's scan failed, no smoke receipt could be written and every live command would have refused [A-S a_scan]. The rule above was run against those cases [R2]. A bare 40-hex rule would flag every git commit id; bare keys are found by digest instead.
- `anthropic_key`: `sk-ant-[A-Za-z0-9_-]{20,}`
- `openrouter_key`: `sk-or-[A-Za-z0-9_-]{20,}`
- `github_token`: `gh[pousr]_[A-Za-z0-9]{36}`
- `jwt`: `eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}`
- `private_key_block`: `-----BEGIN [A-Z ]*PRIVATE KEY-----`
- `netrc_line`: `machine\s+\S+\s+login\s+\S+\s+password\s+\S+`

`PRIVACY_PATTERNS`, class `privacy` (two rules), and the host name:

- `local_path_users`: `/Users/[A-Za-z0-9._-]+/`
- `local_path_home`: `/home/[A-Za-z0-9._-]+/`
- `hostname` (no pattern): each entry of `hostnames` of at least 4 characters is matched case-insensitively where it is not part of a longer run of letters, digits, `-` or `.`.

The `privacy` class is asked for only where something is about to leave the machine: the exporter's scan of plans and Artifact files, and `publish.bundle` (both after `policy.portable` has rewritten the paths it knows, so a finding there is a path it did not know). W&B switches for machine metadata are off (3.25), but a local path or the host name can still sit in a tool argument, a traceback or a candidate file. The default class is `secret` alone: state records hold local paths by design (`job.json` has `upstream_command`), and manifest rule 11, `audit` and the smoke's scan of the state directory use the default.

**The verbatim dataset-text scan** is separate from the pattern scan: it answers "does this text quote a dataset" for the things that are published as text whatever the content rules say (candidate files, Artifact metadata, outer-loop span arguments). `build_verbatim_index` hashes every `run_chars`-character window of each run text (the question text of a protected dataset's rows) and keeps each exact text (for the datasets of `telemetry.verbatim_scan.exact_datasets`: every whole question string and every whole target string, because a window rule cannot see strings shorter than the window; 92 of the 180 USPTO product strings are [r2]). `scan_verbatim` slides the same window over the text and looks each exact string up as a substring. It never returns the matched text. The exporter builds the index once per process from the pinned data files (3.25); a dataset all four of whose content rules are `full` is left out of it, since its text may be shown.

Two more finding kinds do not come from a pattern:

- `known_value`: `known` values of length 8 or more are matched by value. Only a process that holds a key can pass it (the exporter passes its own key).
- `known_digest`: for each `(length, sha256)` in `digests`, every window of that length inside a run of `[A-Za-z0-9_-]{20,}` is hashed and compared. This finds a key no process holds any more and that fits no pattern: a legacy W&B key is 40 bare hex characters, and printed alone it matched nothing in the first draft, where only the exporter's own key was known by value [A-S a_scan, a2_fix]. Each component that reads a key records its digest (2.1, 3.9, 3.17, 3.25); the exporter, `audit`, `publish.bundle` and the smoke load them with `load_digests`. Cost: one SHA-256 per window; megabytes of ordinary records scan in well under a second, and a megabyte of unbroken base64 costs about a second [A-S measured 2.2 MB in 0.01 s for records; the second figure is an estimate].

The proposer has Bash and can print anything it can read, and ingest redacts only by field name [rep], so every plan and every attachment is scanned before anything is emitted.

```python
# policy.py
CREDENTIAL_SUFFIXES = ("apikey", "apitoken", "accesstoken", "secretkey", "privatekey", "clientsecret", "refreshtoken", "sessiontoken")
def apply_text(text: str | None, rule: str) -> tuple[str | None, bool]      # (text after the rule, whether anything was removed)
def strictest(rules: Sequence[str]) -> str           # "hash" before any "truncate:N" (the smaller N first) before "full"
def rules_for(manifest: dict, dataset: str | None) -> dict[str, str]
    # the four rules of telemetry.content[dataset]; for None, field by field the strictest rule over all datasets
def attribute_dataset(text: str, manifest: dict) -> str | None
    # the one dataset whose name, or whose upstream.expect.dataset_dirs value, occurs in the text; None when none or several do
def guard_text(text: str, index: "scan.VerbatimIndex | None") -> tuple[str, bool]
    # the text unchanged, or its `hash` form when scan.scan_verbatim finds dataset text in it; (text, whether it was replaced)
def mask_hex40(text: str) -> tuple[str, int]         # every standalone 40-hex run -> "[hex40 sha256 <12 hex>]"; (text, number masked)
def span_bytes(span: Mapping) -> int                 # len(json.dumps(<span without its "spans" list>)): default separators, ASCII escaping
def planned_bytes(conversations: Sequence[Mapping], factor: Decimal) -> int
    # ceil(factor * sum of span_bytes over every turn and every span at every depth) (2.13)
def check_attributes(attrs: Mapping[str, object], prefix: str) -> list[str]
def portable(value: object, roots: Mapping[str, Path]) -> object
    # absolute host paths -> "<state>", "<workspace>", "<example>", "<home>"; drops the user name; walks dict, list, str
```

Rules: `full`; `truncate:N` (the first and last `N // 2` characters around `\n...[mhforge cut <chars> chars, sha256 <12 hex>]...\n`); `hash` (the text becomes `[sha256 <64 hex> chars <n>]`). `hash` is the default for every field of every dataset.

**One rule set per dataset.** `telemetry.content.<dataset>` gives the rules for prompts (`span_prompt`), replies (`span_reply`), reasoning (`span_reasoning`) and tool results (`tool_result`) of text that belongs to that dataset. A solver job belongs to its dataset. A proposer tool call belongs to a dataset when its arguments name exactly one (`attribute_dataset`: a path under `logs/<run>/<dataset>/` or `data/<dataset dir>/`); its arguments and its result then use that dataset's `tool_result` rule. Everything that cannot be attributed to one dataset (a proposer message, a tool call that names none or several, every outer-loop span argument) uses `rules_for(manifest, None)`, the strictest rule of all datasets, field by field. So opening Symptom2Disease never opens a `cat` of all three logs. `log.jsonl` is treated exactly like `memory.json` in every rule of this package (content rules, Artifacts, publication): in the configured mode its last checkpoint row is the candidate's whole memory state, which for `fewshot_all` is the training split verbatim [r2]. `check_attributes` rejects: a key that does not start with `<prefix>.` or has a further dot (leaf keys only); a value that is not `str`, `int`, `float` or `bool`; a non-finite float; an empty string (the server drops them); a key whose lower-cased alphanumerics end with a credential suffix or equal `authorization`; more than 64 keys. `mask_hex40` is applied by `plan_spans` to proposer tool arguments and tool results after `portable` and before the content rule: there every run of exactly 40 hex characters that is not part of a longer hex run is replaced by `[hex40 sha256 <first 12 hex of its SHA-256>]`. Tool output is where a key printed by a Bash command would appear, a legacy key is 40 hex characters, and the cost is that git commit ids in tool output are masked too. `portable` lives here (not in `publish.py`) because the planners of layer 2 apply it to every run config and to proposer tool text; `publish.py` uses the same function. Policy and byte counting live here, in the planning layer, so they are tested without any SDK (F10).

### 3.25 `telemetry/exporter.py`, `spawn.py`, `adapter_agentlens.py` (P)(U), `adapter_wandb.py` (U)

```python
# exporter.py
@dataclass(frozen=True)
class ExportUnit:
    unit: str; kind: str                             # job | proposer | loop | eval | propose
    surfaces: tuple[str, ...]                        # subset of ("agent_lens", "wandb"), in emit order
    depends_on: tuple[str, ...]                      # units whose receipts must be final first

class LensSink(Protocol):
    def emit(self, plan: Mapping, on_trace: Callable[[str, str], None]) -> None: ...       # on_trace(conversation_id, trace_id) before any span of that trace ends
    def read_back(self, project: str, trace_ids: Sequence[str]) -> list[dict]: ...
class WandbSink(Protocol):
    def feature_ces(self) -> bool: ...
    def project_access(self, entity: str, project: str) -> dict | None: ...     # {"access", "read_only", "can_write"}; None = no such project
    def log_run(self, run_plan: Mapping, table_plans: Sequence[Mapping], files_root: Path) -> dict: ...    # {"run_path", "url", "table_keys", "backend", "base_url", "problems"}
    def confirm(self, run_path: str, table_keys: Sequence[str], artifact_names: Sequence[str]) -> list[str]: ...   # problems; empty = confirmed

class Exporter:
    def __init__(self, manifest: dict, paths: StatePaths, *, ledger: Ledger, lens: LensSink | None, wandb: WandbSink | None,
                 known_secrets: Sequence[bytes] = (), hostname: str = "", example_dir: Path | None = None,
                 amendment: Mapping | None = None, dry_run: bool = False, clock: Clock | None = None) -> None
        # manifest: the EFFECTIVE manifest (frozen copy plus amendments, 2.3 rule A); amendment: its latest row, or None
    def verbatim_index(self) -> "scan.VerbatimIndex | None"     # built once from example_dir's data files; None without example_dir
    def discover(self) -> list[ExportUnit]
    def export_unit(self, unit: ExportUnit, *, retry_partial: bool = False, supersede: bool = False) -> dict[str, dict]   # surface -> receipt
    def export_all(self, *, only: Sequence[str] | None = None, retry_partial: bool = False,
                   supersede: Sequence[str] = ()) -> dict
def final_statuses() -> frozenset[str]               # verified, refused_secret, skipped_budget, skipped_policy, partial, failed, dry_run
```

The exporter is the only code that emits anything, and it runs after the fact, from files (F10). One exporter at a time behind `STATE/export/export.lock`. It never touches work in progress, so `export` may run while a search runs, and it runs with a halt set (2.3): the evidence of a halted campaign is exported like any other. It loads the key digests with `scan.load_digests(paths.keydigests)` for every scan. It works from the effective manifest, so the content rules and the publication decision it applies are the latest amended ones, and every receipt names the amendment it was made under. A live export never creates a project: the first W&B step is `wandb.project_access(entity, project)`, and a project that does not exist, or whose access is not `PRIVATE` before publication was approved, is an `ExportError` (the project is created once, PRIVATE, by `account-check --create-project`, before any surface can create it implicitly with another visibility [r2]).

`verbatim_index`: for every dataset of `upstream.expect.datasets` that is not fully open (not all four rules `full`), read `example_dir/data/<dataset dir>/{train,val,test}.jsonl` (hash-checked against `pristine.json`) and index the `question` string of every row as run text; for a dataset in `telemetry.verbatim_scan.exact_datasets` index every `question` string and every target string (`target`, else `answer`) as exact text instead. The index lives in memory only.

`discover` lists only finished things, from the journal and the state directory:

| Unit | Ready when | Surfaces | Depends on |
|---|---|---|---|
| `job:<job_id>` | the journal has a terminal entry for that job attempt (`complete`, `failed` or `interrupted`) | `agent_lens` | none |
| `proposer:<session_key>` | `session.json` exists | `agent_lens` | none |
| `propose:<candidate>` | a `proposal.json` lists the candidate | `wandb` | none |
| `eval:<split>.<candidate>` | the journal has `benchmark: complete` for it | `wandb` | its `job:` units (for `trace_url`); `propose:<candidate>` when the candidate is not a baseline |
| `loop:iter<NNN>` | the journal has `iteration: settled` | `agent_lens`, `wandb` | none |

A job attempt that the runner did not finish (`interrupted`) may hold admission-only call records; they are planned as `interrupted` spans (3.23), so its plan validates.

`export_unit`, for each surface of the unit, in the F10 order:

1. **Existing receipt.** A receipt in `emitting` or `emitted` (an earlier export was interrupted): do **not** emit; go to step 6. Otherwise build the plan (step 2) to learn its hash, then:
   - a receipt with a final status and the same plan hash: return it (idempotent);
   - a receipt with a final status and a **different** plan hash: return it too, with `stale_plan_sha256` set to the new hash; nothing is emitted, and `status` counts the unit under `export.stale`. This is routine, not an error: a re-proposed candidate name changes a `propose:` plan, and `--retry-partial` on a job unit changes the `trace_url` in a table plan. Under an immutable id nothing can be corrected in place (EvalTables return 409 on changed content [rep]), so a changed plan is never emitted under an existing id;
   - a `dry_run` receipt during a live export: it is archived under `export/attempts/` and the export proceeds as attempt 1 with the plain ids (nothing was ever sent);
   - `retry_partial` and a `partial` or `failed` receipt, or `supersede` and any final receipt whose plan is stale: start export attempt `k + 1` (below).
2. **Plan.** Load the records. For a unit with a W&B surface, first decide what may be uploaded: each candidate file the run plan would name is read and passed through `scan.scan_verbatim`; a file with a finding is **withheld** (the plan gets its hash and size, not the file), and `memory.json`, `log.jsonl`, the worker cache and transcripts are only ever hashed and measured (`evidence`). Build the plan with the pure functions (passing the verbatim index and the withheld set); `validate_plan` or `check_table`; write it under `export/plans/<unit-slug>/`. Problems raise `PlanError` (receipt `failed`, nothing sent); an invalid table plan gives `skipped_policy` for that table. A plan with `placeholder_project: true` or a table plan with `backend: "unset"` can only become a `dry_run` receipt: in a live export it is `skipped_policy`. Receipt `planned`.
3. **Scan.** `scan_obj` over the plan and `scan_paths` over every file an Artifact would upload, with `known_secrets`, the key digests, `classes=("secret", "privacy")` and `hostnames=(hostname,)`; write `export/scan/<unit-slug>.json`. Any finding, of either class: receipt `refused_secret`; nothing is sent; stop for this unit until a person resolves it. Receipt `scanned`. With `dry_run`: receipt `dry_run`; stop.
4. **Meter.** Every check comes before the first byte is sent, and the exporter refuses when a bucket is empty: no server-side cap exists, so this is the only guard [r2].
   - Agent Lens. The bucket is `recovery` for export attempt 2 or later, else `preflight` in a preflight campaign and `main` in a measured one. A `proposer:` unit whose `planned_bytes` exceed `budgets.lens_proposer_session_mb * 1048576` is `skipped_budget`. `ledger.check_meter_cap("lens_bytes", extra_quantity=planned_bytes, bucket=<bucket>)`; over the bucket or over the total: receipt `skipped_budget`. Otherwise `ledger.meter("lens_bytes", "<unit>#e<k>", planned_bytes, "bytes", bucket=<bucket>)` and `ledger.meter("lens_spans", "<unit>#e<k>", expected_spans, "spans", bucket=<bucket>)`.
   - W&B. `upload = ` the sizes of the files the run plan names plus `len(json.dumps(rows))` of each table plan. `ledger.check_meter_cap("wandb_upload_bytes", extra_quantity=upload)` and, for each table, `ledger.check_meter_cap("eval_tables", extra_quantity=1)`; over either cap: receipt `skipped_budget`. Otherwise `ledger.meter("wandb_upload_bytes", "<unit>#e<k>", upload, "bytes")` and one `ledger.meter("eval_tables", "<unit>#e<k>#<table key>", 1, "tables")` per table.
5. **Emit.** Write the receipt with status `emitting`, the conversation ids and `expected_spans` **before the first SDK call**. During `emit`, `on_trace` rewrites the receipt with each trace id before any span of that trace ends (H2 `telemetry.py` L365-L374 [rep]). Then status `emitted`. The return value of `force_flush()` is ignored; it proves nothing [rep AL-V A3].
6. **Read back.** Agent Lens: poll `read_back` every `readback_poll_s` up to `readback_timeout_s` until exactly `expected_spans` rows exist; run `readback.verify_spans`; save the rows to `export/stored-spans/<unit-slug>.json`. Exactly the expected count and no mismatch: `verified`. Fewer after the timeout, more (duplicates), or mismatches: `partial`. W&B: `log_run`, then `confirm` (the run summary carries the marker `_type: "eval-table-ces"` for every table key, the Artifacts exist); `verified` or `partial`.
7. **Receipt** with URLs and remote ids.

Resume rules. `verified`, `refused_secret`, `skipped_*` are final; `dry_run` is final for dry runs only. A new export attempt is started only by `--retry-partial` (for `partial` and `failed`) or by `--supersede UNIT` (for a final receipt whose plan is stale, on the operator's word), at most three attempts per unit and surface: the old receipt is archived under `export/attempts/` and attempt `k` uses conversation ids with the suffix `.e<k>` and run ids with `-e<k>`, so a late first copy cannot mix with the second and nothing is ever logged twice at one location. The one shared run, `<campaign>-search`, keeps its id: a retried `loop:` unit logs its row again with the same `iteration` value and `search/export_attempt = k`; because the plan of a settled iteration is stable (3.23), the two rows are equal. A table plan gets `trace_url` only from a `verified` Agent Lens receipt.

```python
# spawn.py
class ExportSpawner:                                 # export_mode "after_unit": one bounded exporter child at a time
    def __init__(self, manifest: dict, manifest_path: Path, paths: StatePaths, *, journal: Journal,
                 base_env: Mapping[str, str], package_root: Path, export_key: Callable[[], str],
                 python: str = sys.executable, clock: Clock | None = None) -> None
    def submit(self, unit: str) -> None              # queues; returns at once
    def drain(self, timeout_s: float | None = None) -> None
    def backlog(self) -> int
    def close(self) -> None
```

One background thread runs `[python, "-m", "mhforge", "export", "--manifest", <absolute path>, "--live", "--key-source", "fd:<n>", "--unit", <unit>]` with `cwd=package_root` (the directory that contains the `mhforge` package; the CLI passes `Path(mhforge.__file__).parent.parent`), `procenv.exporter_env(manifest, base_env, home=..., package_root=package_root, netrc_file=paths.export_netrc)` (which sets `PYTHONPATH` to the same directory, so the child imports the package whatever the runner's own working directory was; the first draft started the child with no `PYTHONPATH` and no stated cwd, a failure that would have been silent by design) and `telemetry.export_timeout_s`, one child at a time, and journals `export: requested / complete / failed`. **The export key is not in the child's environment or argv.** The spawner opens a pipe, starts the child with the read end inherited (`pass_fds`) and the flag `--key-source fd:<n>`, writes the one line `export=<key>` and closes its end; the child reads the key from that descriptor (3.27). The key reaches the spawner through `export_key`, a callable the CLI builds from the runner's own key source, so the runner holds the export key in memory only when `telemetry.export_mode` is `after_unit`. `run_search` drains the queue before it returns. An export failure never fails the search; it is a receipt. A halt does not stop the child (2.3). With `export_mode: "manual"` (the default) nothing is spawned and the user runs `export`.

Adapters (UNVERIFIED; the only telemetry modules that import an SDK, and only inside functions; each under 250 lines). Both take `environ: MutableMapping[str, str]` (the key mapping the CLI built from the key source, 3.27), read `environ[telemetry.api_key_env]` once, raise `ExportError` when it is missing or empty (there is no fallback to an SDK's own credential chain), and expose `key_facts` (name, length, SHA-256; the CLI appends the digest to `STATE/keydigests.json`).

- `adapter_agentlens.CoreweaveSink(manifest, *, environ)` (P: H2 `telemetry.py` L82-L170, L302-L408): `tracing.init("<entity>/<project>", api_key=<key>, base_url="https://trace.wandb.ai", service_name=<telemetry.agent_lens.service_name>, autopatch_integrations=False)`. The explicit `base_url` is the same constant as the host of `readback.READBACK_URL`; without it the SDK takes the trace server from `WF_TRACE_SERVER_URL` or `WANDB_BASE_URL` (`coreweave` 0.1.0b0 `tracing.py` L180-L188 [read]), which would send the spans and the key header to another host. It removes `OTEL_RESOURCE_ATTRIBUTES` from its process and sets `OTEL_BSP_MAX_EXPORT_BATCH_SIZE=<export_batch_size>` and `OTEL_EXPORTER_OTLP_TRACES_COMPRESSION=gzip`; one module lock around every export; `with tracing.Conversation(conversation_id=..., conversation_name=..., attributes=...)`, `with tracing.Turn(agent_name=..., agent_version=..., messages=..., started_at=..., ended_at=...)`, read the trace id from the current OpenTelemetry span and call `on_trace`; then each pre-built `tracing.LLM(...)` / `tracing.Tool(...)` / `tracing.SubAgent(...)` is entered with `with span:` and `span.set_attributes(...)`, `span.record_error(...)` inside, with `llm.usage = tracing.Usage(...)` only when the plan has usage and messages as `tracing.Message(role, parts=[TextPart | ReasoningPart])`; `force_flush()` per conversation; `shutdown()` in a `finally`. `read_back` posts `readback.spans_query_body` to `READBACK_URL` with Basic auth user `api` over `urllib` with an explicit User-Agent, paging by `offset` until `total_count`.
- `adapter_wandb.WandbSdkSink(manifest, *, environ, paths)`:
  - **It never calls `wandb.login`.** `wandb.login(key=...)` writes the key to the system netrc file, replacing the user's existing entry for the host (W&B's own reference page for `login`: an explicit key "is used and written to the system .netrc file"; wandb 0.31.0 `wbauth/wbnetrc.py` L96-L170 [A-S, read]). That would put the export key in plain text in `~/.netrc`, against F14, and in a place a proposer in `upstream` mode can read. The key is passed as `wandb.init(..., settings=wandb.Settings(api_key=<key>, ...))`, which the SDK does not persist (`wandb_init.py` L207-L216: `update_api_key=False` when the key comes from settings [read while revising]), and as `wandb.Api(api_key=<key>)`.
  - **Before the first SDK import** it refuses to go on (`AdapterUnavailable`) when `importlib.util.find_spec("weave")` finds the `weave` package, and sets `WANDB_DISABLE_WEAVE=true`: `wandb.init` starts Weave tracing when `weave` is importable, which would log every model call a second time into the same billed meter (`wandb` 0.31.0 `wandb/integration/weave/weave.py` L83-L100 [r2]). It creates the empty file `STATE/export/empty.netrc` (mode 0600) if the CLI has not already, and sets `NETRC` to it, so no code path of the SDK can read or write the user's netrc file (`wandb` honours `NETRC`, `wbauth/wbnetrc.py` L199-L201 [r2]); it sets `WANDB_DIR`, `WANDB_CACHE_DIR` and `WANDB_DATA_DIR` to directories under `STATE/export/wandb-local/` (the SDK otherwise creates `./wandb` in the working directory and uses `~/.cache/wandb` and a platform data directory [A-S, from the W&B environment-variable reference]), `WANDB_CONSOLE=off` (the exporter's own output is not captured into the run) and `WANDB_DISABLE_GIT=true`.
  - It refuses to run if `WANDB_BASE_URL`, `CES_BASE_URL`, `WANDB_APP_URL` or `WANDB_MODE` is set, and returns the effective base URL with `log_run`, for the receipt's `remote.base_url` (not for the run config, which is the allow-list of 2.15 and nothing else).
  - `wandb.init(entity, project, id=<plan id>, group, job_type, name, tags, config, dir=<STATE/export/wandb-local>, resume="allow" if plan.resume else None, settings=wandb.Settings(api_key=<key>, disable_git=True, save_code=False, console="off", x_disable_meta=True, x_disable_stats=True, x_disable_machine_info=True, x_save_requirements=False))`. Those are the switches of `wandb` 0.31.0 for git capture, code saving, console capture, machine metadata, system statistics, machine information and the requirements file (`wandb_settings.py` L168, L321, L409, L511, L608, L622-L634, L868 [r2]); with the defaults a run would carry the host name, the user name, the program path and a `requirements.txt` of the driver's environment. That the switches exist is confirmed in source; what each one uploads by default, and that with all of them off the stored run holds no host name, user name or local path, is [live: PF8], which reads the run's metadata back and fails on any of the three. `config` is the allow-listed subset of 2.15, never the whole manifest.
  - For a plan with `define_metrics`, `run.define_metric(name)` or `run.define_metric(name, step_metric=...)` for each pair before any row; history rows are logged with `run.log(row)` and **no** `step` argument.
  - Artifacts with `wandb.Artifact(...)`, `add_file`, `run.log_artifact(...).wait()` or `run.use_artifact(...)`; tables with `wandb.EvalTable(columns=..., data=..., input_columns=..., output_columns=..., score_columns=..., backend="ces", unsupported_media_mode="raise")` logged once under the plan's key, or `wandb.Table` when the backend is `wandb_table`; `feature_ces` retries the CES probe three times because it also returns false on a timeout [rep ET-V]; summary; `run.finish()`; `confirm` reads the run summary through `wandb.Api(api_key=<key>)`.

Reading of F14 used here: the Forge key that pays for inference is read only by the gateway's provider client. The exporter is a separate process that needs a W&B credential for the SDKs and the read-back; it gets one from its own key source (a prompt, an inherited descriptor, or with `--key-source env` a manifest-named variable, a different one from the other two by validation rule 8), the value is passed explicitly to both SDKs on every call, given to the scanner as a known value, recorded only as a digest, and never written to a record, to a netrc file, to a child's environment or to the terminal.

Never: emits anything while the job or session it describes is still running; re-emits under an id that may already exist; emits a changed plan under an existing id; emits before the scan; logs a table whose check failed; uploads the state directory or any file not named in a plan; treats an SDK call returning as delivery; calls `wandb.login`; lets an SDK write outside `STATE`; lets an SDK find a credential by itself (environment, netrc, settings file, prompt); starts with `weave` importable; creates a project; uploads `memory.json`, `log.jsonl`, a reply cache or a transcript; uploads a candidate file the verbatim scan flagged; puts turn content, cumulative context or an `attachment` record into a span; sends anything while a bucket of the ingest cap is empty; puts the whole manifest, a local path or an account fact into a run config.

### 3.26 `publish.py` (P: H2 `report_v4.py` L847-L905) (U)

```python
def allowed_files(paths: StatePaths, manifest: dict) -> list[Path]          # allow-list; never "everything under state"
def bundle(manifest: dict, paths: StatePaths, dest: Path, *, known_secrets: Sequence[bytes], hostname: str = "") -> dict
    # scans first (classes secret and privacy, the host name passed in as data); SecretFound aborts
def foreign_conversations(recorded: set[str], remote: Iterable[str]) -> list[str]     # pure; ids in the project that no receipt recorded
def publish(manifest: dict, paths: StatePaths, *, wandb: WandbSink, remote_conversation_ids: Callable[[], Iterable[str]]) -> dict
```

`allowed_files`: the frozen manifest, `protocol.md`, the journal, job records, iteration records, session records (not transcripts), receipts, stored read-back spans, plans, upstream's `evolution_summary.jsonl` and frontier files, candidate files, qualification, calibration and forecast records, the audit result, `stopped.json` when present, and an export of the ledger's `halt_history` and `reconcile_history` rows (`publish/bundle/halt_history.json`), so that a halted campaign is published as what it is. Also `amendments.jsonl` when it exists (it documents which content rules applied when). Never call bodies, raw streams, transcripts, sidecars, `memory.json`, `log.jsonl` or worker caches, whatever `decisions.dataset_text_publication` says: the plans in the bundle already carry text under the per-dataset rules, and nothing else carries dataset text. Never `keydigests.json`, `runtime.json`, `account/` (plan, privileges and headroom are not ours to publish), the SQLite files or `export/wandb-local/`. `bundle` copies the allow-list, scrubs paths with `policy.portable` (3.24), scans every file with `known_secrets` and the key digests, and refuses on any finding. `publish` works from the effective manifest (2.3 rule A) and refuses unless the campaign is `measured`, `decisions.publish_approved` is true and `foreign_conversations(...)` is empty (every conversation id in the remote project is one this run recorded; H2 `report_v4.py` L849-L880 [rep]); each outward step (visibility, Report, saved views) is a separate, receipted, UNVERIFIED skeleton call. It runs with a halt set (2.3).

### 3.27 `smoke.py` and `cli.py` (F15)

```python
# smoke.py
def run_offline_smoke(sc_dir: Path, *, executor: str = "local_relay", example_dir: Path | None = None, keep: bool = False) -> dict
    # the receipt of section 5.3; SmokeFailed(invariants) when an invariant fails; MhforgeError when the example venv is missing
def network_guard_prefix() -> tuple[tuple[str, ...], str]
    # (command prefix, name): on macOS, when `sandbox-exec -p <loopback-only profile> /usr/bin/true` returns 0, the prefix that runs a
    # child under that profile and "sandbox-exec"; otherwise ((), "none"). Best effort: inside an outer sandbox the probe fails and no guard is used.
# cli.py
KEY_ROLES = ("provider", "sandbox", "export")      # manifest names: provider.api_key_env, sandbox.auth_env, telemetry.api_key_env
def parse_key_source(text: str) -> tuple[str, int | None]     # "prompt" -> ("prompt", None); "env" -> ("env", None); "fd:7" -> ("fd", 7)
def roles_needed(command: str, manifest: dict, *, mode: str | None = None) -> tuple[str, ...]
def read_keys(source: tuple[str, int | None], roles: Sequence[str], manifest: dict, environ: MutableMapping[str, str], *,
              getpass_fn: Callable[[str], str] = getpass.getpass,
              read_fd: Callable[[int], bytes] | None = None) -> MutableMapping[str, str]
    # the key mapping {<manifest variable name>: <key>} for the given roles; LiveGateError naming the role when one is missing
def main(argv: list[str] | None = None) -> int
```

**Key sources** (`--key-source {prompt, fd:N, env}`, on every live command). A key passed in a process's start-up environment stays readable by every process of the same user through `ps eww` for the life of that process, whatever the process does to the variable afterwards unless it overwrites the bytes in place [R3; r2, measured again]; and `export NAME=<key>` typed at a prompt lands in the shell history. So:

- `prompt`, the default when standard input is a terminal: one `getpass.getpass` prompt per key role the command needs, in the order provider, sandbox, export. The prompt names the role and the manifest's variable name, never a value, and nothing is echoed. **Enter alone reuses the previous answer** (the usual case is one dedicated key for all three roles); Enter at the first prompt refuses.
- `fd:N`: the descriptor `N`, inherited from the parent, is read once to its end and closed. Each non-empty line is `<role>=<key>`, the role being `provider`, `sandbox`, `export` or `all`. This is how a wrapper script, a password manager or the export spawner (3.25) hands keys over without a terminal.
- `env`, the default when standard input is not a terminal: the variables named in the manifest, read from the environment as before; each reader then overwrites its variable in place in the environment block and deletes it (`util.scrub_process_env`, 3.9), and `runtime.json` records whether that worked. The README says how to set such a variable without it entering the shell history (`read -s NAME && export NAME`).
- **Never in argv.** No flag takes a key; a key on a command line is visible in the process list and in the shell history.

`read_keys` returns the key mapping that the provider client, the sandbox client and the adapters receive as their `environ`: with `env` it is `os.environ` itself; with `prompt` and `fd` it is a private dict keyed by the same manifest names, and the keys never enter `os.environ`. Every reader deletes its entry after reading it, so nothing holds a key longer than its component does. `roles_needed`: `account-check` needs `provider`, plus `sandbox` with the sandbox executor (PF3 lists sandboxes with the configured auth); `preflight`, `search` and `evaluate` need `provider`, plus `sandbox` with the sandbox executor, plus `export` when `telemetry.export_mode` is `after_unit` (and `preflight` always, for PF8); `export` and `publish` need `export`. A role whose key is missing or empty is a refusal (exit 3) that names the role: no command goes on to let an SDK look for a credential of its own.

`util.loopback_guard` (3.2) is the in-process guard; `smoke.py` and the tests both import it from `util.py`. The `offline-smoke` command passes `Path(mhforge.__file__).parent.parent` as `sc_dir` (the directory that contains the package, `REPO/showcase`): the receipt is written to `<sc_dir>/state/_smoke/receipt.json`, which is where the offline gate of a manifest in that directory looks for it. Live manifests therefore live in that directory.

| Command | Flags | Behaviour |
|---|---|---|
| `status` | `--manifest M`; one of `--clear-halt NOTE`, `--reconcile LEDGER_ID --spent-usd X --note NOTE [--from-billing-export]`, `--reset-job JOB_KEY --note NOTE` | prints `state.status(...)` as JSON; exit 0 even with no state; takes no lock and writes nothing. The three flags are operator writes; each takes `runner.lock` (exit 4 when a runner holds it), needs a note and is journaled. `--clear-halt` refuses while any `unknown` row exists, writes `halt_history` and journals `halt: cleared`. `--reconcile` calls `Ledger.reconcile` (3.6; `--from-billing-export` is `below_reservation=True`) and journals `halt: reconciled`. `--reset-job` journals `job: reset`, which gives that job a fresh candidate-attempt budget (3.18). |
| `halt` | `--manifest M`, `--note NOTE` | the operator's stop: writes the halt row (reason `operator`, the note as detail) through its own ledger connection. It takes **no** runner lock, so it works while a search runs: new calls are refused at once, calls in flight settle, the running job is ended within about a minute (3.16 wait loop) and the runner stops with exit 2 and journals `halt: set` when it sees the row. Exit 3 when the state directory or the ledger does not exist. Not a live command: it needs no `--live` and spends nothing. Without it the only ways to stop spend would be a signal, which is honoured only at the next iteration boundary (3.20), or a second signal, which cuts attempts. |
| `validate-manifest` | `--manifest M`, `--pin-upstream`, `--freeze` | prints every problem; exit 3 if any. `--pin-upstream` prints the digests to put into `upstream.tree_sha256` and `upstream.worker_lock_sha256` (it does not edit the file). `--freeze` freezes into the state directory. |
| `offline-smoke` | `--executor local\|local_relay`, `--keep`, `--example-dir` | `run_offline_smoke`; exit 5 on a failed invariant; exit 3 if the example venv is missing; refuses `--live` |
| `account-check` | `--manifest M`, `--live`, `--key-source S`, `--create-project`, `--claude-spent-usd X`, `--claude-limit-usd Y`, `--claude-reserve-usd Z` | `preflight.account_check`: steps PF1 to PF4 of 8.2. It runs with an **unfrozen** manifest (the manifest must validate and name the entity and the project; nothing else is required), so the account can be checked before anything is frozen. It reads only, with one exception: `--create-project` looks the project up, creates it with `access: "PRIVATE"` when it does not exist, and reads the access back. It writes `STATE/account/account.json` and the pricing snapshots (2.4); those facts stay in local state and never enter a run config or telemetry. Without `--live` it prints the steps and exits 3. |
| `preflight` | `--manifest M`, `--live`, `--key-source S`, `--only NAME ...`, `--teardown`, `--platform-inference-usd X`, `--platform-sandbox-usd Y`, `--waive-reconciliation NOTE` | without `--live`: prints `preflight.describe()` (with `--teardown`: the teardown list of 8.2) and exits 3. With it: the checks of 8.2 from PF5 on, including the qualification job, the forecast and the calibration gate; in a campaign that imports its qualification, only the per-project checks (3.22). The two `--platform-*` figures (or the waiver) are what the reconciliation check PF-R compares with the ledger. `--teardown --live` runs the one automated teardown item (the sandbox sweep) and prints the rest. |
| `search` | `--manifest M`, `--live`, `--key-source S`, `--max-iterations-this-run N` | `require_live`, lock, `Driver.run_search(max_iterations_this_run=N)`. With `N`, at most `N` iterations run in this invocation and the run is journaled `paused` (exit 0) unless it reached `search.iterations`; the next invocation continues. The total is always `search.iterations` from the manifest; no flag changes it. |
| `evaluate` | `--manifest M`, `--live`, `--key-source S`, exactly one of `--baselines`, `--calibration`, `--noise`, `--test [--end-search]` | `Driver.run_baselines`, `Driver.run_calibration`, `Driver.run_noise` or `Driver.run_test(end_search=...)`. `--calibration` runs the test jobs of the calibration systems without finalising the run and judges the gates (3.20); `--noise` runs the baseline repeats of the noise floor; both can be used before the search. `--test` is upstream's finalisation; it is refused until the search is complete unless `--end-search` is given, and after it the search can never run again under this campaign (2.3). |
| `export` | `--manifest M`, `--live` or `--dry-run`, `--key-source S`, `--unit U ...`, `--retry-partial`, `--supersede U ...` | `--dry-run` needs neither `--live` nor a key and writes plans, scans and `dry_run` receipts only. Runs with a halt set. Works from the effective manifest (frozen copy plus amendments). |
| `amend` | `--manifest M`, `--set PATH=JSON ...`, `--note NOTE` | `manifest.append_amendment` (2.3 rule A): appends one hash-chained row to `STATE/amendments.jsonl` for the export-only fields, after validating the result. Needs a frozen copy and a note; takes `export/export.lock` (exit 4 when an exporter holds it); journals `amendment: appended`; prints the row's `seq` and hash. Not a live command: it spends nothing and sends nothing. It never changes the frozen manifest and needs no re-qualification. |
| `publish` | `--manifest M`, `--live`, `--key-source S` | bundle, ownership check, then the UNVERIFIED visibility skeleton. Runs with a halt set. Refused for a preflight campaign. |

Every live command: without `--live` it prints the blockers it would have and exits 3; with `--live` it calls `manifest.load_frozen` (`account-check`: `manifest.read_manifest` and `require_valid`) and `manifest.require_live` first and exits 3 with the list of blockers when refused. Then, in this order: it creates the empty file `STATE/export/empty.netrc` (mode 0600) and sets `NETRC` to it in its own process, so that from here on no SDK code path in this process or in a child can read or write the user's netrc file; it reads the keys from the key source (`read_keys`; a missing role refuses); and only then builds anything that imports an SDK. Runner commands (`preflight`, `search`, `evaluate`) take `runner.lock` (exit 4 when held); build the provider with `ForgeProviderClient(manifest, environ=<key mapping>, scrub=<util.scrub_process_env with --key-source env, else None>)` and, for the sandbox executor, the sandbox client the same way; write the key digests (`util.append_key_digest`) and `STATE/runtime.json` with the key source and the `key_scrubbed` result per variable; and with `runner.keep_awake` on macOS start `caffeinate -is -w <pid>` with `procenv.minimal_env` (critic gap 12; `-s` also holds off system sleep on mains power, and a long run wants mains power and an open or docked lid). `search --live` and the proposer steps of `preflight --live` refuse when `CLAUDECODE` or `CLAUDE_CODE_ENTRYPOINT` is in the environment (2.3): they must be started from the user's own terminal. `status` prints a warning line on stderr when the stall alarm is set (3.4). `CampaignStop` is turned into `stopped.json`, a journal entry and exit 2; any other exception inside a runner command into `stopped.json` with reason `internal_error` (or `upstream_contract`), a journal entry and exit 1 (3.20). On every exit path of a runner command the CLI calls `Driver.shutdown()` (abort queued requests, drain, close the executor). `cli.py` is, with `smoke.py`, the only module of the package that reads `os.environ`; it passes the mapping on, and it is the only module that reads the host name (`socket.gethostname()`), which it passes as data to the exporter, the publisher and the preflight for the privacy scan. No command prints an environment value or a key; the CLI prints key variable names and role names only.

---
## 4. Data flow

### 4.1 One solver call, end to end (Route 2, sandbox, relay)

1. Candidate code calls `self.call_llm(prompt)`, which reaches `LLM.__call__` (`llm.py` L369): truncate, `ProviderLLM.generate`, `_generate_one` (L252). Our H1 wrapper notes the start time and whether the worker's cache holds the reply, then calls the original.
2. Cache miss: `_call_completion` (L203) calls `litellm_completion(model="openrouter/openai/gpt-oss-120b", messages=[system "Reasoning: medium", user prompt], temperature=0.0, max_tokens=16384, timeout=600.0)` (L206-L220 [src]). LiteLLM's OpenRouter route posts to `$OPENROUTER_API_BASE/chat/completions` with `Authorization: Bearer $OPENROUTER_API_KEY` and body keys `model, messages, temperature, max_tokens, usage` [F-E3].
3. `OPENROUTER_API_BASE` is `http://127.0.0.1:<port>/v1`, the stub inside the sandbox. The stub assigns `rid`, keeps the request pending and writes a `REQ` frame (meta `{"method": "POST", "path": "/v1/chat/completions"}`, body = the request body) to its uplink. A 68 KB LawBench few-shot request is about 32 to 45 KB of ASCII on the wire [X-E3, T-E1].
4. Host: the exec stream delivers stdout in fragments, each decoded with replacement. `FrameDecoder` reassembles the frame; `RelaySession` submits `Gateway.handle(job, make_envelope(...), ingress="relay")` to its pool. The identity is the session's.
5. Gateway: envelope and identity checks; no halt; admission (drops `usage`; two messages, within `gateway.max_messages`); `request_sha256`, `user_sha256`, `input_limit`; admission record; no stored reply, so this call is the leader; a semaphore slot (the call may queue here behind 16 others); `circuit.enter(max_wait_s=...)`, from here paired with exactly one `circuit.finish`; `ledger.reserve(...)`.
6. Provider client: a streaming `POST https://api.inference.wandb.ai/v1/chat/completions` with the Forge key, `OpenAI-Project`, the explicit `User-Agent` and `stream_options.include_usage`. Raw lines go to `<call_id>.a1.sse` as they arrive; chunks are merged; `[DONE]` ends the stream.
7. Gateway: the identity gate; one `chat.completion` body; `lease.commit` stores it in the replay store and wakes any followers; then `ledger.settle(...)` gives `complete` and the cost in micro-dollars (store before settle, 3.7); `circuit.finish("success")`; flags; the call record is replaced; the slot is released.
8. `RelaySession` writes a `RES` frame (status 200, `content-type`, `x-mhforge-source: provider`, the body) to the bridge's stdin in slices; in daemon mode it also keeps the frame in the done cache.
9. Stub: matches `rid`, answers the worker's HTTP request with a `Content-Length` body, drops the pending entry.
10. LiteLLM returns; `llm.py` takes `message.content` (L112-L125), counts `usage.prompt_tokens` and `completion_tokens` (L223-L235), writes its own cache file (L261), and `parse_harmony_response` leaves plain content unchanged [rep]. H1 writes the `call` row.
11. Back in `predict_one` (`inner_loop.py` L440-L446) the evaluator wrapper H3 scores the prediction on the same thread and writes the `eval` row with this call's `seq`.
12. The second validation pass (L753-L756) hits the worker's cache: a `call` row with `cache_hit: true`, no gateway request.

Variations. With `LocalExecutor`, steps 3, 4, 8 and 9 are one HTTP request to the job's loopback listener, which checks the bearer token. On Route 1 the worker sends `Bearer local` and no `usage` key; nothing else changes. On a replay, steps 5 to 7 collapse to "store hit, zero-usage body, record `outcome: replayed`".

Failure branches of the same call:

| What goes wrong | What happens | Cost to the worker | Money |
|---|---|---|---|
| Provider 429 | ledger `rejected`, circuit failure, the slot is released, a wait of 20 to 40 s, retry inside the gateway (up to two retries) | waits | zero |
| Provider 5xx, reset, a truncated stream, or a 200 stream carrying an error | retry inside the gateway after its backoff | waits | non-200: zero; no status or no usage: `bounded_unknown` at the reservation |
| All gateway attempts fail | `provider_unavailable` (403, one worker request, no client retry). The worker raises and exits 1; the attempt is `provider_unavailable` (infrastructure, whatever the worker did with the error) and the job runs again after a wait, with finished calls replayed. If the same request uses up the gateway's attempts again in the next job attempt while other requests are being served, the reply is `provider_rejected` with detail `repeatable_provider_failure`: a candidate-attributable failure, so one prompt cannot stop the campaign for ever (3.10 step 9.7). | attempt lost | as above |
| Provider 400 for the prompt length | `context_length_exceeded`; the job's attempts end `worker_error` / `context_limit` | job lost | zero |
| Provider 401, 402 or 403; a 429 whose body says the quota is exhausted | halt `provider_config` (`provider_quota` for the 429); `halted`; the executor ends the job within about a minute and the driver stops | job lost | zero |
| A job ceiling reached (requests in this attempt, charged attempts or spend across attempts) | `job_request_ceiling`; outcome `request_ceiling`; the campaign goes on. A worker that keeps calling is killed after ten times the refused-record cap | job lost | nothing reserved |
| Phase or total cap reached | halt `budget:<phase>`; `budget_exhausted`; calls in flight finish and settle; the executor gives the worker 60 s and then kills it; the attempt is `halted`; the driver stops and drains | job lost | nothing reserved |
| The exec stream drops between steps 3 and 9 (daemon mode) | the gateway call keeps running; the host attaches a new bridge; the stub re-sends the pending `REQ`; the session ignores it (in flight) or re-answers from the done cache. One provider call in total [L1]. | none | charged once |
| The exec stream drops (stdio mode) | the stub kills the worker and exits 75; the attempt is `relay_lost`; the next attempt's identical calls are replayed | attempt lost | charged once |
| The exec stream goes quiet without closing | the bridge misses its pongs and is marked dead after `relay.pong_timeout_s`; a write into it times out the same way. Daemon mode: a new bridge is attached and pending requests are re-sent. Stdio mode: the attempt is `relay_lost` (the stub, hearing nothing from the host, kills the worker) | none, or attempt lost | charged once |
| The worker's 600 s client timeout fires | the stub notices the client has gone, drops the request and sends `CAN`; tenacity retries the identical request; each copy coalesces onto the in-flight call or reads the store | waits | charged once |
| The runner crashes between storing a reply and settling its row | the reply is in the replay store, the row is `pending` and becomes `bounded_unknown`; the rerun replays the call for free | attempt lost | charged once; the row is booked at its reservation |
| Empty reply with `finish_reason: length` | delivered unchanged, flagged `truncated_empty`; upstream scores the example wrong and caches the empty string | none | charged normally |
| The runner crashes during step 6 | the row is `pending`; `interrupt_pending` makes it `bounded_unknown`; the attempt is `interrupted`; a rerun pays this one call again if the provider had answered | attempt lost | at most one reservation per in-flight call |

### 4.2 One (candidate, dataset) job, per executor

Common prologue (`JobRunner.run_job`): next attempt number from the journal; the candidate bytes and their hash (a candidate file that changed since it was proposed is replaced by its stored copy); attempt budgets per `(job_key, candidate_sha256)`; halt check; pinned files verified; `bundle.build_bundle` from the pristine `EX` files plus the workspace's `agents/*.py`; journal `started`.

**LocalExecutor** (development only, NOT ISOLATED). Extract the bundle into `box/`; register the job with a fresh token; open a loopback listener for it; selfcheck without egress probes; start the worker (`mhf_capture_entry.py -- <argv>`, cwd `box/pkg`, the built-from-nothing environment with `OPENROUTER_API_BASE` = the listener and `OPENROUTER_API_KEY` = the token); wait up to `job_timeout_s`; kill the group on overrun; write `exit.json`; close the listener and the registration; collect.

**LocalRelayExecutor** (proves the relay offline, NOT ISOLATED). The same work tree; start `mhf_relay_stub.py serve --job ... --uplink stdio` with pipes; `PipeChannel(mimic_sdk=True)`; `RelaySession.attach`; the stub starts the worker, relays its calls, answers pings, writes `exit.json` with its own counters, sends `EXIT`, exits; collect. No gateway listener exists on the host; the stub's own loopback listener requires a per-attempt bearer that only its worker has. With `mode="daemon"` the stub is daemonized on a Unix socket under a short `/tmp` directory and bridges are rotated and replaced, so the sandbox's default path runs offline too.

**SandboxExecutor** (the measured run; adapter UNVERIFIED). Sandbox budget check; create (the worker image or, in mode `wheelhouse`, the stock base image, by amd64 manifest digest; no mount; egress and DNS denied; `WANDB_API_KEY=""`; lifetime = job limit + margin + setup allowance; 5 tags; explicit auth with entity and project); `on_handle`; open the sandbox's meter row at its whole lifetime; wait for running; upload `bundle.tar` and `job.json` (and `in/memory.json` for a test job); extract; in mode `wheelhouse` upload the wheelhouse tar, check its size and checksums and install it with `pip --no-index --require-hashes` (any failure: the attempt is `env_setup_failed`, an infrastructure fault); selfcheck with literal-address egress probes and the proof of machine, Python version and package set (stop the campaign on a visible credential or open egress; refuse to start the worker, before any paid call, if the proof or the tokenizer file is missing); start the stub daemon; bridge exec with `stdin=True` attached to a `RelaySession`, rotated by byte count or replaced after a stream error; ping the bridge every 20 s; wait for `EXIT` while watching for a halt and a kill request; read `exit.json` and the collect tar; stop the sandbox in a `finally`; close the meter row with the measured seconds. A crash at any point leaves a sandbox that dies at its lifetime and a meter row that counts the whole lifetime; the next start runs `cleanup_orphans`, which closes the row.

Common epilogue: `classify_attempt` (a baseline's failure is always infrastructure); write `job.json` and `executor.json`; journal `complete` or `failed`; queue the `job:` unit; halt check; retry while the attempt budgets allow (by default one candidate-attributable attempt and up to three infrastructure attempts), with a growing wait between infrastructure attempts; a host sleep during the attempt makes it `interrupted`, not `timeout`. Outcome `ok` needs return code 0, an exit file with reason `exit`, a usable result file, and no tainted refusal code, relay gap or ceiling in the attempt. Publication into `LOGS` and the launcher log happen in the enclosing `run_benchmark` call (3.18).

### 4.3 One search iteration

1. Upstream's loop (`meta_harness.py` L445-L571) computes the iteration number, reads the frontier, renders the task prompt (L136-L146, paths under `LOGS`) and deletes `pending_eval.json` (L467-L468).
2. `propose_claude` (L473), ours: a pending stop request is honoured here, at the iteration boundary; tracker to `search`; `halt_check`; no stored proposal; budget and binary checks; workspace verify and snapshots; the scratch directory; the allow-listed environment with its closed list; the generated settings file and the mode's argv; shim and `build_command` wrapper installed; `claude_wrapper.run(...)` (upstream's wrapper writes `claude_sessions/<ts>_iter<N>/` as always); tee to `stream.jsonl`; a deadline on the monotonic clock, paused during reported rate-limit waits, then a graceful timeout; the session's process group reaped; `pending_eval.json` copied and shape-checked; transcript and its directory copied; drift check and restore; files added outside `agents/` and `logs/` quarantined; `evolution_summary.jsonl` restored if the session changed it; state-access signals computed; the session checks (model and effort, `sandboxed: true` on every Bash result in modes `restricted` and `contained`, integrity); the session's meter row closed with its cost (the `result` event, else the transcript's cost record, else its usage at list prices; never zero); the limit classifier; session and proposal records; units queued. A spend limit, a login failure, a model mismatch, an unavailable sandbox or ignored settings stop the campaign; a plan-window limit is waited out and a transient failure rerun, both for the same iteration. Returns `True` only for a session whose `result` event says it succeeded and whose `pending_eval.json` was accepted.
3. Upstream reads the candidates (L482) and calls `validate_candidates` (L492), ours: the per-iteration limit, name and file checks on the host (no execution), then in the executor a probe import of a baseline followed by one import check per candidate, each with the loaded-class assertion. Candidates that fail their own import, or whose loader-picked class is not their own, are dropped, as upstream drops a failed import; a failed probe or an executor that cannot start is an infrastructure attempt and rejects nobody. Zero valid candidates: upstream calls `update_evolution_summary` with empty scores (L498-L500); our wrapper writes the iteration record with kind `zero_valid`.
4. For each valid candidate upstream calls `run_benchmark(["--memory", name])` (L519), ours: verification of every published result file (anything no job record published is quarantined), three jobs from upstream's `build_val_runs` in dataset order (4.2), publication, launcher logs with upstream's paths, `benchmark.py -P --results` for `summary.json`, the `eval:` unit queued, halt check. A halt raises `CampaignStop` here and unwinds upstream with no summary row.
5. `run_benchmark(["--frontier", "--model", "gpt-oss-120b"])` (L527): pinned files and published results are verified again, then the real `benchmark.py` (started with `-P`) rewrites `frontier_val.json` from the published results.
6. Upstream computes scores (L530-L550) and calls `update_evolution_summary` (L553-L560): the original appends its rows; our wrapper writes `iterations/iter-<NNN>.json`, journals `iteration: settled` and queues the `loop:` unit.
7. Export (`manual`: whenever the user runs `export`; `after_unit`: one bounded child per queued unit): plan, scan, meter, emit, read back, receipt.

### 4.4 The test phase

There are two test paths. `evaluate --calibration --live` (`Driver.run_calibration`, 3.20) runs the test jobs of the calibration systems before the search without finalising anything: no `finalized.json`, nothing under `LOGS/results`, ledger phase `calibration`, the verdict of the gates G1 and G2 in `STATE/qualification/calibration.json`. It normally runs in the preflight campaign, directly after qualification and before any proposer spend (8.2 PF7), and the measured campaign reads its verdict through the qualification import. `evaluate --test --live` is the finalisation; the gates of 2.3 allow it only after the search is complete (or ended with `--end-search`) and block the search for ever after it. It calls `Driver.run_test`, which calls `run_evolve(test=True)` and so upstream's `finalize_run` (L286-L357):

1. Upstream builds the test systems from the baselines, every `_pareto` entry and each dataset's `best_system` (L301-L308) and writes `finalized.json` as `in_progress`.
2. `run_benchmark(["--memory", name, "--test"])` per system (L325): `plan_jobs(test=True)` uses `build_test_runs`, which returns a job per dataset whose `test.json` is not usable and whose `memory.json` exists (L564-L570). `inner_argv` carries `--load-memory {in}/memory.json --test-output {out}/test.json` and no `--log`. The executor uploads the validated `memory.json` (the copy in the attempt directory it was published from, hash-checked; not the `LOGS` copy), and the bundle carries the candidate bytes that were validated on that dataset (3.18); the worker loads state and evaluates the test split once [F-E1, T-E2]. A test job that calibration already ran in this campaign with the same code and memory is reused, not run again. Ledger phase `test`; test jobs of one candidate always run one at a time (`executor.parallel_dataset_jobs.test`). Results are published to `LOGS/results/<dataset>/<name>/<model_short>/test.json`. A dataset whose `memory.json` is missing counts as failed and the driver reports which validation job to rerun.
3. `run_benchmark(["--frontier", "--test", "--model", m])` (L330) and `run_benchmark(["--results", "--test"])` (L333) run the real `benchmark.py`.
4. Upstream checks completeness (L338-L351). Incomplete: `SystemExit(1)`, returned as `incomplete`; the command can be run again and finished jobs are skipped (a test job that used up its attempts needs `status --reset-job` first). Complete: `finalized.json` becomes `complete`; upstream then refuses further evolution for this campaign (L395-L402).

Telemetry: test jobs export like validation jobs, with turn `test`, table key `test/<dataset>` and unit `eval:test.<candidate>`.

### 4.5 Resume after a crash

State that survives: the frozen manifest, the ledger (with open reservations), the replay store, the journal, call records, job attempt directories, proposer session directories with stored proposals, export receipts, upstream's files in `WS`.

Start-up of every runner command: `runner.lock` (an `flock`, released by the kernel when the old holder died); compare the manifest with the frozen copy; refuse while a halt is set (`export` and `publish` do not refuse); `startup_recovery` (3.20).

| Crash point | State left behind | Resume does | Paid twice? |
|---|---|---|---|
| During a provider call | `pending` ledger rows; admission-only call records; a `job: started` entry; a sandbox or process group | rows become `bounded_unknown`; the attempt becomes `interrupted` (an infrastructure attempt); the sandbox is stopped by id and by tag; the job runs again as a new attempt booked to `recovery` | Completed calls replay for free. Calls in flight at the crash may be paid again: at most one reservation each, reported as bounded unknown. |
| After a job ended, before its journal entry | result files in the attempt directory, no terminal entry | attempt `interrupted`; the job runs again; every call replays | No |
| After the journal entry, before publication | journal says succeeded; nothing in `LOGS` | the journal-backed result is reused and published | No |
| During a proposer session | `proposer: started` only; an open meter row at the session's `--max-budget-usd` cap; a partial `stream.jsonl`; possibly candidate files | journal `interrupted`; the meter row is closed at that cap; upstream asks for the same iteration; a new session runs as `s<k+1>` | The interrupted session is paid for and counts at its cap against the proposer ceiling |
| After the proposer finished, before or during its jobs | a stored proposal | upstream asks again; the proposal is replayed; no session | No |
| After a proposer failure that upstream counts as a loop turn | a settled failure | upstream asks again; `propose` returns `False` at once; upstream moves to the next iteration number | No |
| Between upstream's summary rows and our iteration record | rows on disk, no journal entry | `startup_recovery` journals `iteration: settled` with kind `recovered`; upstream starts at the next iteration | No |
| After a halt | the ledger `halt` row, `stopped.json` | runner commands refuse until `status --clear-halt NOTE` (after `status --reconcile` for any `unknown` row); `export` and `publish` still run; a budget halt would trip again at once, so it ends the campaign (section 8.3) | No |
| During an export | a receipt in `emitting` or `emitted` | read back; never re-emit under the same id | Ingest bytes are metered once per export attempt |
| During test finalisation | `finalized.json` `in_progress`; a `test: started` journal entry | run `evaluate --test` again; finished test jobs are skipped; `search` stays blocked | No |
| During a sandbox job or import check | an open sandbox meter row at the whole lifetime; a sandbox that may still run | `cleanup_orphans` stops it and closes the row with the time since it was opened | The sandbox time is counted, never zero |
| While the host sleeps during a job | the sandbox keeps running and billing; relay pongs are missed | the wait loop sees the gap between the wall clock and the monotonic clock; the attempt is `interrupted` (`host_sleep`), an infrastructure attempt, and runs again with finished calls replayed | No |
| Between two blocks of a search (`--max-iterations-this-run`) | `run: paused`; every iteration so far settled | the next `search` continues at the next iteration number; the W&B search run is resumed under the same id | No |
| After a proposer stop (spend limit, login, model mismatch, ignored settings) | a session record whose outcome is not a settled failure; `stopped.json` | the user fixes the cause; the next `search` runs a new session for the same iteration | The stopped session was paid for once |

---
## 5. Test plan

All tests are `unittest.TestCase`, standard library only, run from `SC` with `python3 -m unittest discover -s tests -t .`. Tests ported from Harness2 are rewritten from pytest to unittest and keep the SPDX header. Every test uses a temp state directory and `PYTHONDONTWRITEBYTECODE=1` for children. No test opens a non-loopback socket: test case classes that start a server or a child install `util.loopback_guard()` (it lives in `mhforge/util.py`, layer 0, so that both `tests/helpers.py` and `smoke.py` can import it). The guard covers the test process only; children are held to loopback by their environment (no key, the stub or a loopback URL as the only endpoint).

`tests/helpers.py` provides: `tmp_state()` (a temp `SC`-shaped tree with `state/<campaign>`), `FakeClock` (manual `now`, `monotonic`, recorded `sleep`), `SeqIds` (deterministic `IdSource`), `make_manifest(**dotted_overrides)` (returns a dict built from `fixtures/manifest_valid.json`), `write_manifest(root, manifest)` (writes the dict to `<root>/manifest.json` with `upstream.example_dir` and `upstream.worker_python` rewritten to absolute paths, and returns the path; `root` is then that test's `SC`), `FakeGateway` (a `HandlesRequests` with scripted replies, a call log, a settable halt state and kill request), `stamp_rows(lines, start_utc, step_s)` (wraps raw stream-json lines into the `{"t_utc", "stream", "line"}` rows of `stream.jsonl`, for the stream fixtures), `assert_neutral(reply)` (status 402 or 403; the two headers; no digit; none of `RETRY_TRIGGER_STRINGS` in any letter case; no match in any letter case for `\b429\b`, `rate[\s_\-]*limit` or the phrase `request too large`), `example_dir()` (`MHFORGE_EXAMPLE_DIR` or `SC/../reference_examples/text_classification`), and three skip decorators:

- `needs_example_dir`: skips unless the example directory exists (tests that read upstream files).
- `needs_example_venv`: skips unless `<example>/.venv/bin/python` and the bundled tokenizer file exist. Such tests spawn the venv Python as a subprocess; the test process itself stays on the standard library. Children get `LITELLM_LOCAL_MODEL_COST_MAP=True`, `TIKTOKEN_RS_CACHE_DIR` = LiteLLM's bundled tokenizers directory and a scratch cache directory, so nothing is downloaded and nothing is written under the repository.
- `needs_yaml`: skips unless `yaml` imports (tests that import upstream `meta_harness` or `benchmark` from a workspace copy in process).

`tests/fixtures/manifest_valid.json` is the draft of 2.3 with exactly these changes, so every owner can rely on its values: `campaign: "mh-test"`, `state_dir: "state/mh-test"`; `campaign_kind: "measured"`; `decisions`: `live_spend_approved: true`, `key_provisioning: "dedicated"`, `entity: "example-entity"`, `project: "mh-forge-test"`, `proposer_permission_mode: "upstream"`, `evaltables_backend: "ces"`, `publish_approved: true`, `dataset_text_publication: "identifiers_only"`; `upstream.tree_sha256` and `upstream.worker_lock_sha256`: 64 zeros; `sandbox.env_mode: "image"`, `sandbox.image: "registry.example.com/mhforge/worker@sha256:<64 zeros>"`; `proposer.cli_path: "/nonexistent/claude"`, `proposer.cli_sha256`: 64 zeros, `proposer.cli_version: "2.1.317"`; `proposer.max_budget_usd_per_session: "25.00"`; `budgets.proposer_list_usd: "100.00"`; `proposer.session_estimate_usd: "10.00"`; `proposer.list_prices` gains a `claude-haiku-5-5` entry with the synthetic fixture's prices (`EXPECTED.json`, `prices_usd_per_mtok`); `executor.kind: "local_relay"`. `make_manifest` applies dotted overrides on a deep copy (tests set `provider.kind: "stub"`, small caps, `upstream.worker_python: sys.executable` and so on). **No test validates the fixture at its own path**: under rule 2 of 2.3 the directory of a manifest file is its `SC`, so a manifest is always written into a temp root with `write_manifest` before it is validated, frozen or resolved to a state directory. (The fixture sits in `tests/fixtures/`, where `state/mh-test` and the relative example paths would resolve to the wrong places.)

### 5.1 Fakes

| Fake | File | Stands in for | What it can inject |
|---|---|---|---|
| `StubProvider`, `serve_stub` | `mhforge/stubprovider.py` | Serverless Inference | every `Fault` kind of 3.9; records calls and peak in-flight |
| `FakeClock`, `SeqIds`, `FakeGateway` | `tests/helpers.py` | time, ids, the gateway | instant backoff and timeout tests; scripted replies |
| `FaultChannel` | `tests/fake_channel.py` | the exec stream | re-chunks at seeded random boundaries, decodes each chunk with `errors="replace"`, counts retained bytes, drops the stream after N frames, cuts a frame in the middle at the drop, goes silent without closing (reads block, writes are swallowed or block), injects noise text, delays writes |
| `FakeSandboxClient` | `tests/fake_sandbox.py` | `cwsandbox` | sandboxes as directories under `tempfile.mkdtemp(prefix="mhf-", dir="/tmp")` (the sandbox work directory is mapped there; a long temp path would push the daemon socket past the 90-byte limit of 3.13); execs as local subprocesses with path rewriting; `exec_stream` as a `FaultChannel` over the real stub; scripted create failure, a create error with a given status and reason (occupancy, throttle, `RESOURCE_EXHAUSTED` without a reason, `PERMISSION_DENIED`), a pull failure of the first image reference, quota error N times, stuck create, a short or failing wheelhouse upload, a checksum failure, a failing `pip install`, a wrong machine, Python version or freeze hash in the selfcheck report, stream drop, silent stall, missing exit file, a credential or open egress in the selfcheck report, a slow or failing probe import; records every call with its arguments in order (the wheelhouse `pip install` exec is recorded and answered, not run) |
| `FakeLensSink`, `FakeWandbSink` | `tests/fake_sinks.py` | Agent Lens, W&B and CES | store spans, runs, tables, artifacts; return fewer or more rows on read-back; delay ingestion; fail before or after sending; reject null scores and changed content at the same location as CES does; CES flag off or flaky; a project that does not exist or whose access is not `PRIVATE` |
| fake `claude` executable | `tests/fake_claude_cli.py` | Claude Code CLI | modes `ok`, `graceful`, `ignore_int`, `ignore_all`, `limit_text` (an error result whose text says "usage limit", with no structured event: the fallback case), `not_logged_in`, `writes_pending` (which also creates `agents/__pycache__/x.cpython-313.pyc`, as the skill's own import line does), `mentions_limit` (an `ok` session whose result text says "falls back when the char limit reached"), `bad_hypothesis` (a `pending_eval.json` whose `hypothesis` is a number), `writes_stray_file` (a `yaml.py` in the workspace root), `edits_summary` (appends a row to `evolution_summary.jsonl`), `reads_state` (a tool call naming `../../jobs/...`), `edits_upstream_file`, `exit_nonzero`, `spawns_child` (a background child that outlives a normal exit), `no_result_exit0` (exits 0 without a `result` event), `spend_limit` and `limit_window` (a `rate_limit_event` with `status: rejected` in the two shapes of 3.19, then an error result), `transient` (an overloaded API error), `max_budget` (`subtype: error_max_budget_usd`), `model_fallback` (a main-thread reply from another model, and a `system/model_fallback` event), `wrong_effort`, `plugins_loaded` (an `init` that lists a plugin, a skill, an MCP server or `memory_paths`), `api_retry_wait` (rate-limit `api_retry` events before a late result), `unsandboxed_bash` (a transcript whose Bash tool result has `sandboxed: false`), `sandbox_unavailable` (the startup failure result); emits `system/init`, `assistant`, `user`, `result`; records the argv and the environment names it was started with; can write a synthetic transcript under a fake home, in the shape of the corrected fixture (every assistant record with a `requestRef`, one `api-request` record per message, a `cost-state` record on a normal exit, `version` set to a given value) |
| `fake_claude_wrapper` | `tests/fake_claude_wrapper.py` | `claude_wrapper` | a module-like object whose `run` uses `subprocess` exactly as L590-L654 does and has `build_command` |
| `FakeFacade`, `FakeExecutor` | `tests/fake_facade.py` | upstream's builders, an executor | canned `(desc, cmd)` lists with upstream's prefix; scripted `JobResult`s and output files; fail N times |
| `FakeMetaHarness` | `tests/fake_meta_harness.py` | `meta_harness` | the four globals, `LOGS_DIR`, `PENDING_EVAL`, and a `run_evolve` that issues upstream's call order [F-E2, T-E6] through the replaceable globals |
| `fake_pkg/text_classification` | `tests/fixtures/fake_pkg/text_classification/{__init__.py, llm.py, data.py, inner_loop.py, memory_system.py, agents/__init__.py, agents/ok_agent.py, agents/bad_import.py, agents/foreign_class.py}` | the upstream package | stdlib-only modules with the same seams: `memory_system.MemorySystem` (the base class the loader looks for; `ok_agent.py` defines its own subclass, `foreign_class.py` imports a subclass from `ok_agent` under a name that sorts before its own), `llm.ProviderLLM` (`_generate_one`, `_cache_path`, `_load_cache`, a file cache under `TEXT_CLASSIFICATION_LLM_CACHE_DIR`), `llm.LLM` (`__call__`, `batch`, `_truncate`), `data.load_dataset_splits_3way` and `load_dataset_splits` (`data.py` is a module, not a directory: the root `.gitignore` ignores directories named `data`), and `inner_loop` with the same argparse flags as upstream and a `__main__` block that learns, makes two validation passes (or one test pass with `--load-memory`) over a thread pool, posts to `OPENROUTER_API_BASE` with `urllib`, and writes `val.json` / `test.json`, `memory.json` and `log.jsonl` in upstream's shapes |

Fixtures are synthetic and no real transcript is ever committed. `tests/fixtures/transcript_synthetic/` and `tests/fixtures/stream_synthetic.jsonl` are **byte copies of the corrected synthetic fixture** of the proposer-runtime research (invented content, measured structure), taken by the owner from `/private/tmp/claude-502/-Users-ashah-Library-Application-Support-Claude-scratch-workspaces-4fae85b1-8a8d-4e26-975c-aa802946e255-09c4c5cb-f508-4d7e-841e-046add0921c8-scratch-2026-11-09-790576/d83951c7-b145-4d4e-9594-03f7dd065c09/scratchpad/research2/proposer-fixtures`: `transcript/5e55104e-0000-4000-8000-00000000abcd.jsonl` and `transcript/5e55104e-0000-4000-8000-00000000abcd/subagents/agent-a1b2c3d4e5f607182.jsonl` with its `.meta.json` go under `transcript_synthetic/` with the same relative names; `EXPECTED.json` goes beside them; `stream/events.jsonl` becomes `stream_synthetic.jsonl`; `stream/events.usage-limit.INFERRED.jsonl` becomes `stream_limit_synthetic.jsonl` (its shape is inferred and was never observed; the tests that use it say so). The fixture's own `README.md` is copied into `transcript_synthetic/` as well. "Corrected" means: every assistant record carries `requestRef`, and there is one `api-request` record per message, main thread and subagent; the first version of the fixture had neither, so the join the converter relies on could not be tested with it [r2]. The repaired fixture was on disk when this spec was amended (checked: 15 main records with 4 assistant records over 3 messages and 3 `api-request` records; 6 subagent records with 2 messages and 2 `api-request` records; every `requestRef` resolves in its own file; 13 stream events; 5 events in the limit stream). If the copy the owner finds lacks any of that, the owner stops and reports it; the files are never edited by hand (they are regenerated by the research generator). The fixture's `cwd` values are under `/Users/example/`, its session id is fixed, and its content strings are marked `SYNTHETIC`. `EXPECTED.json` gives the message ids, the tool calls, the de-duplicated token totals per model (`claude-opus-5-5`, `claude-haiku-5-5`), the list-price cost (0.196784 + 0.001075 = 0.197859 USD), `request_join` (per message: the request reference, the request start time, the model, the effort and the last record's timestamp; the subagent's effort is `high` and the main thread's `max`, so a test can tell the joins apart), `records_to_skip` (`attachment`, `queue-operation` and the unknown type `synthetic-unknown-record`), `pinned_version` (`2.1.317`) and, for the limit stream, the time to wait until (`usage_limit_stream.wait_until_epoch_s`). `queue-operation` and `cost-state` records carry no `version` key, as measured, and must not trip the version gate. The stream fixture holds raw stream-json events; a test turns them into `stream.jsonl` rows with `helpers.stamp_rows`. What the fixture invents beyond the measured structure (the inner keys of `attachment`, the opaque values of `api-request`, and `sandboxed: true`, of which only `false` was ever seen) the converter must not depend on, except for `sandboxed`, which is what PF9 settles.

### 5.2 Named cases per file

A case name states what it proves; a note follows only where it does not.

**Layer 0**

- `test_errors.py`: `test_exceptions_take_keyword_fields_and_one_line_message`; `test_campaign_stop_is_base_exception_not_exception`; `test_reason_sets_and_exit_codes` (`HALT_REASONS` is a subset of `STOP_REASONS`; `ledger_fault`, `baseline_incomplete`, `internal_error`, `budget:calibration`, `budget:controls`, `provider_quota` and the ten proposer stops are present; `proposer_usage_limit` is gone; the six exit codes are distinct); `test_executor_error_kinds_and_transcript_error`.
- `test_scan.py` (the scanner is layer 0): `test_each_pattern_found_without_returning_text`; `test_known_value_found_anywhere`; `test_bare_key_found_by_digest` (a 40-hex value alone on a line, found through `digests` with no key in the process); `test_upper_case_key_names_found` (`MHFORGE_FORGE_API_KEY=<40 hex>`, `WANDB_API_KEY: <40 hex>`); `test_git_hash_and_sha256_are_not_findings`; `test_spec_records_are_clean` (the section 2 examples as JSON in indented, compact and canonical form: job file, job record, call record, session record, receipt; and `EX/uv.lock` when present: no finding); `test_scan_obj_walks_nested`; `test_load_digests_absent_file_is_empty`. Second round: `test_privacy_patterns_only_when_class_requested` (a `/Users/<name>/` path and a `/home/<name>/` path are findings with class `privacy` and not under the default class); `test_hostname_passed_as_data_whole_word_only`; `test_verbatim_run_and_exact_match` (a 40-character run of an indexed text is found and a 39-character one is not; a short exact string is found whole); `test_scan_reads_nothing_from_the_machine` (source check: no `socket`, no `getpass`, no `os.environ`, no file read outside `scan_paths` and `load_digests`).
- `test_util.py`: `test_open_sqlite_ro_creates_nothing_and_reads_wal`; `test_scrub_process_env_hides_value_from_ps` (a child process with a canary; a sibling `ps eww` shows no canary; skipped when `ps` is unavailable); `test_append_key_digest_mode_0600_no_value`; `test_loopback_guard_blocks_non_loopback_connect`; `test_write_json_is_atomic_on_failure`; `test_append_jsonl_concurrent_rows_stay_whole` (16 threads, 200 rows each); `test_read_jsonl_ignores_torn_tail_only`; `test_canonical_json_is_stable_keeps_unicode_rejects_nan`; `test_tree_hashes_refuse_symlink_and_digest_changes`; `test_changed_files`; `test_micros_rounds_up` (ported); `test_utc_iso_round_trip_microseconds`; `test_ids_shape`; `test_lookup_and_safe_component`. Second round: `test_sleep_gap`.
- `test_wire.py`: `test_job_key_and_id_format` (including the `qual.` prefix); `test_identity_roundtrip_and_validation`; `test_ledger_phase_rules`; `test_refusals_are_402_or_403_and_neutral` (every row through `assert_neutral`: status, no trigger string in any letter case, none of LiteLLM's rate-limit patterns, no digit, `x-should-retry: false`); `test_envelope_round_trip_and_bad_first_line`; `test_candidate_and_campaign_patterns` (a name with a trailing newline is rejected; 49 characters are rejected); `test_ledger_phase_first_attempt_without_charge_stays_in_phase`; `test_bounded_id`; `test_credential_name_pattern` (`TIKTOKEN_RS_CACHE_DIR` and `MONKEY` do not match; `FOO_API_KEY`, `HF_TOKEN`, `DB_PASSWORD` do). Second round: `test_phases_statuses_outcomes_and_scopes_are_the_spec_lists` (the constants equal the lists of 2.2, 2.6, 2.11 and 3.7; `LEDGER_PHASES` has seven entries); `test_job_key_with_repeat_and_sample`; `test_clearance_and_never_publish_constants`.
- `test_paths.py`: `test_layout_matches_spec`; `test_unit_slug`; `test_resolve_state_is_beside_the_manifest_and_requires_literal_state_dir`. Second round: `test_sibling_and_proposer_scratch`.
- `test_frames.py`: `test_roundtrip_all_types_binary_body`; `test_feed_any_chunking` (every split point of a 3-frame stream; seeded random chunking of 200 frames); `test_multibyte_payload_survives_split_and_replace_decoding`; `test_unarmoured_utf8_is_corrupted_by_the_same_path` (negative control: why the armour exists); `test_noise_between_frames_is_skipped_and_counted`; `test_corrupt_crc_length_or_base64_resyncs`; `test_oversize_header_and_payload_rejected`; `test_compression_bomb_rejected`; `test_decoder_holds_one_partial_frame`; `test_non_base64_character_rejects_frame_at_once` (a cut 60 KB frame followed by `HELLO` and `EXIT`: both are delivered [A-M m4]); `test_can_and_kill_frames_round_trip`.
- `test_capture_entry.py` (against `fake_pkg`, as a subprocess of `sys.executable`): `test_rows_for_two_validation_passes_keep_last_matches_result`; `test_calls_join_to_examples_by_thread`; `test_cache_hit_flag`; `test_batch_calls_recorded_with_hashes`; `test_wrapper_does_not_change_return_values_or_exceptions`; `test_recording_error_is_swallowed_and_counted`; `test_system_exit_code_passes_through`; `test_unwritable_sidecar_does_not_fail_the_job`; `test_duplicate_example_keys_marked_ambiguous`; `test_wanted_split_wins_for_shared_question`; `test_sidecar_holds_no_prompt_text`; `test_missing_hook_target_sets_flag_false`. With `needs_example_venv`: `test_real_worker_val_job_rows_equal_val_json` (USPTO `no_memory` against a 30-line loopback completion server defined in the test file, so this layer-0 test needs no later module: 30 examples, 2 passes [F-E1, T-E2]); `test_real_worker_test_job_load_memory`; `test_capture_does_not_change_results` (`val.json` equal, apart from time fields, with and without the wrapper). Second round: `test_eval_rows_carry_question_prediction_and_target_hashes` (of the whole strings, taken before the cut).
- `test_sse.py`: `test_merge_concatenates_content_and_both_reasoning_names`; `test_merge_keeps_last_usage_not_sum` (ported); `test_error_chunk_is_preserved`; `test_stream_without_choices_gives_empty_choices`; `test_completion_body_shape_and_empty_content_is_string`; `test_completion_body_never_invents_usage_or_model`; `test_iter_sse_done_bad_and_comment_lines`.

**Layer 1**

- `test_manifest.py`: `test_draft_manifest_validates`; `test_draft_refuses_every_live_command_naming_the_field`; `test_each_user_decision_blocks_alone` (from `manifest_valid.json`, unset one F16 field at a time); `test_unknown_key_and_bad_enum_rejected`; `test_phase_caps_must_cover_open_reservations`; `test_lower_max_output_tokens_rejected`; `test_image_must_be_digest_and_not_docker_hub`; `test_docker_hub_allowed_only_as_fallback_with_same_digest`; `test_deferred_env_modes_rejected` (`snapshot`, `install_then_deny`); `test_key_env_names_rejected_in_proposer_env`; `test_env_allow_rejects_secret_like_names`; `test_pasted_key_in_manifest_rejected`; `test_freeze_then_change_is_refused` (ported); `test_freeze_is_idempotent_and_needs_tree_hash`; `test_read_manifest_creates_nothing`; `test_require_live_checks_presence_and_never_echoes_a_value` (an empty value counts as absent); `test_halt_blocks_runner_commands_not_export` (with a halt row, `preflight`, `search` and `evaluate` are blocked; `export` and `publish` are not); `test_gate_problems_become_blockers`; `test_state_dir_must_be_literal_and_manifest_in_temp_tree_validates`; `test_three_key_env_names_must_differ`; `test_entity_project_pattern_and_printable_headers`; `test_env_allow_rejects_proxy_ssh_loader_and_cloud_prefixes`; `test_floors_for_content_and_request_bytes`; `test_cached_price_above_input_price_rejected`; `test_loop_conversation_rule`; `test_search_blocked_by_finalized_json_or_test_entry`; `test_evaluate_test_needs_complete_search_or_end_search`; `test_session_estimate_null_blocks_search`. Second round: `test_content_rules_per_dataset` (LawBench must be `hash`; USPTO needs the clearance decision; `identifiers_only` needs every rule `hash`; an unknown dataset key is rejected); `test_budget_phases_sum_and_lens_split` (the draft's seven phases sum to its total; the three ingest buckets sum to the cap); `test_calibration_gate_rules`; `test_parallel_test_jobs_must_be_one`; `test_campaign_kind_and_import_from_rules`; `test_amendment_chain_and_effective_manifest` (append, read back, effective value; a changed or removed row breaks the chain; a non-amendable path and an amendment that opens LawBench are refused; the frozen hash is unchanged); `test_diff_paths_and_import_allow_list`; `test_export_gate_needs_dataset_text_decision`; `test_search_refuses_inside_a_claude_session` (`CLAUDECODE` or `CLAUDE_CODE_ENTRYPOINT` set); `test_account_check_needs_no_frozen_copy`; `test_key_rows_skipped_for_prompt_and_fd_sources`; `test_search_needs_cli_version_and_session_budget`; `test_preflight_campaign_cannot_publish_gate`; `test_run_config_selects_allowed_paths`; `test_fixed_env_names_rejected_in_env_set`.
- `test_state.py`: `test_second_runner_gets_lock_error_with_holder` (subprocess); `test_lock_released_when_holder_dies`; `test_status_is_read_only` (directory mtime and file list unchanged; works on a read-only copy); `test_status_creates_no_wal_or_shm_on_idle_ledger` (a WAL-mode ledger created in the test from the DDL text of 2.10, written once and then idle [A-M m1]); `test_status_after_killed_writer_sees_wal_rows` (a leftover `-wal`: the rows in it are counted [A-M m1b]); `test_status_without_state`; `test_status_tolerates_half_written_files`; `test_status_marks_not_isolated_jobs`; `test_status_shows_key_scrubbed_and_stale_exports`. Second round: `test_status_stall_alarm` (`FakeClock`: a held lock and a journal older than `runner.stall_alarm_s`); `test_status_shows_key_source_buckets_and_amendments`.
- `test_procenv.py`: `test_worker_env_exact_names`; `test_route_env`; `test_proposer_env_is_allow_list` (a base environment polluted with 40 `CLAUDE_*`, key and `OTEL_*` variables yields only allowed names); `test_proposer_placeholders`; `test_exporter_env_has_no_key_sets_netrc_and_weave_off` (`PATH`, `HOME`, `LANG`, `PYTHONPATH`, `NETRC` = the empty state file, `WANDB_DISABLE_WEAVE=true`, and nothing else); `test_minimal_env`; `test_find_tokenizer_dir`; `test_assert_no_secret_names`; `test_tiktoken_cache_dir_is_not_a_credential_name`. With `needs_example_venv`: `test_worker_python_is_never_resolved` (`venv_root` of the example venv's `python` contains `pyvenv.cfg`; the resolved path does not; `find_tokenizer_dir` finds the bundled directory [A-U E8]). Second round: `test_proposer_fixed_env_is_closed_list` (the eight names with their values, in every permission mode); `test_proposer_deny_list_never_survives` (a base environment holding every name of `PROPOSER_DENY_NAMES` and one name per denied prefix yields none of them; `assert_proposer_env` raises for each when added by hand, and for a value holding a loopback gateway address).
- `test_ledger.py` (ported from H2 `test_budget.py`, `test_study_v4.py` L61-L127, `test_safety.py` L13-L23, plus new): `test_reservation_formula_and_rounding`; `test_concurrent_reservations_one_fits` (20 threads against a cap that fits one); `test_phase_cap_and_total_cap_are_distinct_errors`; `test_job_ceiling_counts_all_attempts_and_does_not_halt`; `test_settle_table`; `test_contradictory_usage_halts_in_same_transaction`; `test_cost_above_reservation_is_unknown`; `test_rejection_books_zero_and_releases_reservation`; `test_second_settle_raises`; `test_totals_equal_sums_after_random_operations` (2,000 seeded operations); `test_halt_blocks_reserve_first_reason_wins`; `test_clear_halt_needs_note_and_refuses_unknown`; `test_interrupt_pending_keeps_reservation_committed`; `test_token_lookup_uses_hash_and_only_open_jobs` (the database file is scanned for the token); `test_close_open_jobs`; `test_meter_once_per_key_and_caps`; `test_two_processes_can_write`; `test_snapshot_never_lists_rows`; `test_phase_totals_rows_exist_at_creation_and_missing_row_raises`; `test_connections_set_synchronous_full`; `test_rejected_attempts_do_not_count_toward_ceiling`; `test_ceiling_resets_per_job_attempt_hard_bound_does_not`; `test_job_usd_cap`; `test_limit_exceeded_within_reservation_settles_without_halt`; `test_reconcile_unknown_row_then_clear_halt` (history row written; a figure below the reservation needs the flag; nothing deleted); `test_prior_charged_only_after_a_charged_attempt`; `test_note_exhausted_needs_two_job_attempts_and_peer_ok`; `test_open_meter_row_counts_at_worst_case_until_closed`; `test_orphan_sandbox_is_metered_at_worst_case`; `test_meter_max_ignores_open_rows`. Second round: `test_seven_phase_rows_and_new_phases_reserve`; `test_lens_buckets_cap_separately_and_in_total`; `test_span_upload_and_table_meters`.
- `test_replay.py`: `test_scope_search_shares_across_jobs`; `test_scope_job_isolates_jobs_but_not_attempts`; `test_qualification_scope_is_always_private`; `test_only_200_is_stored_and_empty_length_reply_is_stored`; `test_hit_has_zero_usage_and_nothing_else_changes`; `test_followers_get_leaders_reply_once` (30 identical requests, one leader); `test_shared_failure_reaches_followers_and_is_not_stored`; `test_unshared_failure_promotes_a_follower`; `test_abandoned_leader_promotes_a_follower`; `test_bypass_does_not_store`; `test_body_hash_mismatch_is_a_miss`; `test_scope_change_on_existing_file_refused`; `test_request_hash_ignores_key_order_and_dropped_fields`; `test_store_survives_restart`. Second round: `test_repeat_scope_isolates_repeats_but_not_attempts`; `test_reuse_salt_separates_samples`.
- `test_circuit.py`: `test_opens_probes_and_resets` (ported); `test_state_survives_restart`; `test_enter_times_out`; `test_probe_refused_at_reserve_releases_probe` (a probe that finishes `neutral` lets the next request probe; nothing blocks [A-M m2]); `test_neutral_keeps_count_and_cooldown`; `test_failure_then_success_resets`.
- `test_provider.py` (against `serve_stub`): `test_classify_table` (one case per row); `test_request_is_exact` (the five headers; stream fields added; no `response_format`, no `usage`); `test_key_removed_from_mapping_after_init_and_absent_from_repr`; `test_scrub_function_called_with_variable_name_and_result_in_key_facts`; `test_key_not_visible_in_process_listing` (a child that builds the client with `util.scrub_process_env`; a sibling `ps eww` shows no canary); `test_key_in_error_body_is_replaced`; `test_stream_without_done_but_with_finish_reason_and_usage_is_ok`; `test_missing_key_is_config_fault`; `test_base_url_must_match_kind`; `test_first_byte_and_idle_timeouts`; `test_raw_sse_saved_and_holds_no_request_header`; `test_oversize_stream_aborted`; `test_redirect_is_an_error`; `test_never_raises_on_network_error`; `test_context_limit_detection`. Second round: `test_every_401_402_403_is_config_fault`; `test_quota_429_is_terminal_other_429_retries` (by `error.code` and by a `provider.terminal_429_patterns` entry in the message); `test_classification_order_status_code_message`.
- `test_stubprovider.py`: `test_default_reply_per_dataset_is_deterministic`; `test_render_sse_round_trips_through_iter_sse`; `test_each_fault_kind`; `test_serve_stub_binds_loopback_and_records_requests`; `test_peak_in_flight_counted`.
- `test_relay_stub.py` (the real stub as a subprocess of `sys.executable`; a scripted host over pipes): `test_hello_then_exit_with_child_code`; `test_relays_94_concurrent_posts` [T-E1]; `test_child_stdout_never_on_frame_channel`; `test_route1_api_base_substitution`; `test_worker_env_is_only_job_file`; `test_stdin_eof_kills_child_writes_exit_file_exits_75`; `test_stub_exits_cleanly_no_abort` (return code 0, not -6); `test_reply_timeout_gives_relay_unavailable`; `test_oversize_body_refused_locally`; `test_local_refusals_equal_wire_table`; `test_job_timeout_kills_process_group_rc_124`; `test_daemon_survives_bridge_replacement_and_resends_pending` [L1]; `test_request_written_during_connection_swap_is_resent`; `test_requests_wait_while_no_bridge_is_attached`; `test_daemonize_returns_when_ready`; `test_bridge_exits_3_without_socket`; `test_only_method_path_body_leave_the_stub`; `test_pong_answers_ping_and_reader_never_writes`; `test_stdio_silence_from_host_is_treated_as_eof`; `test_client_gone_sends_can_and_drops_request`; `test_kill_frame_ends_worker_reason_killed`; `test_stub_local_refusals_and_unanswered_in_exit_file`; `test_bearer_required_when_env_set_and_worker_gets_it`.
- `test_selfcheck.py`: `test_reports_names_not_values`; `test_credential_names_exclude_openrouter_key`; `test_tiktoken_cache_dir_is_not_a_credential`; `test_pattern_and_exempt_names_equal_wire`; `test_egress_skipped_without_flag`; `test_tokenizer_presence`; `test_import_check_ok_and_fail` (`fake_pkg`); `test_import_check_rejects_bad_name`. Second round: `test_literal_address_probes_recorded_not_gated` (the probe list and its targets; a fake resolver file; `cluster` skipped without the variable); `test_freeze_hash_is_stable_and_order_independent`; `test_expect_problems_for_machine_python_and_freeze`; `test_import_check_rejects_foreign_loaded_class` (`fake_pkg` `agents/foreign_class.py`: exit 3); `test_import_check_no_class_exits_4`.
- `test_journal.py`: `test_seq_continues_after_reopen`; `test_torn_tail_tolerated`; `test_recover_marks_started_as_interrupted_once`; `test_attempt_numbers_never_reused`; `test_open_handles` (job and import-check attempts); `test_session_start_seq`; `test_counted_attempts_per_candidate_hash_and_after_reset`.
- `test_workspace.py`: `test_candidate_name_and_file_rejections` (including a name that carries Python code, a trailing newline and a symlink); `test_pycache_in_agents_does_not_reject_candidates` [A-U X1]; `test_pending_eval_shape_check` (a `hypothesis` that is null, a number or an object, and a repeated name, are refused); `test_unknown_entries_and_quarantine`; `test_drift_classification`; `test_env_and_memory_fingerprints_change_detected`; `test_contract_fails_on_missing_attribute` (fake modules). With `needs_example_dir`: `test_pinned_files_are_the_tracked_set`; `test_copy_matches_tree_hash_and_excludes_venv`; `test_digest_mismatch_refused`; `test_edit_to_pinned_file_is_detected_and_restored`; `test_new_agent_file_is_kept`; `test_symlink_is_refused`; `test_nothing_written_outside_state`. With `needs_yaml`: `test_load_upstream_contract_holds_and_paths_stay_in_copy` [F-E2, X-E4, T-E6]; `test_load_upstream_leaves_sys_path_unchanged`; `test_planted_module_in_workspace_is_not_imported` (a `yaml.py` in the workspace root [R1]); `test_poisoned_pycache_is_ignored` [R1b].
- `test_policy.py`: `test_truncate_keeps_head_tail_hash_and_length` (Chinese text); `test_hash_rule`; `test_span_bytes_is_ascii_escaped_json_length` (a Chinese character counts six bytes; the nested `spans` list is not counted); `test_planned_bytes_applies_factor`; `test_attribute_rules`; `test_mask_hex40_only_standalone_runs`; `test_portable_scrubs_home_and_user`. Second round: `test_strictest_and_rules_for_none`; `test_attribute_dataset_one_none_several`; `test_guard_text_replaces_quoted_dataset_text`.
- `test_join.py` (fixtures `sidecar_uspto_val.jsonl`, `call_records/`): `test_misses_join_by_user_hash_in_order`; `test_keep_last_pass_matches_result_file`; `test_batch_hashes_join`; `test_roles_learn_validate_test_other`; `test_replayed_call_reports_source_usage`; `test_unmatched_and_ambiguous_marked`; `test_problems_listed`; `test_no_sidecar_gives_empty_examples`; `test_check_examples_against_result`; `test_admission_only_record_is_a_problem_not_a_call`.
- `test_transcript.py` (synthetic fixture): `test_message_counted_once_per_id` (`msg_synthetic_A` has two records and is one message); `test_subagent_messages_use_their_own_model` (the subagent's two messages go to `claude-haiku-5-5`); `test_totals_equal_expected_json` (de-duplicated tokens per model, and the list-price cost 0.197859 USD with the fixture's prices); `test_request_ref_join_gives_start_times_and_effort` (equal to `request_join` in `EXPECTED.json`; five `api-request` records are read; a span ends at the message's last record); `test_message_without_request_record_warns_and_uses_own_timestamp`; `test_stream_output_tokens_are_ignored`; `test_unknown_types_skipped_and_counted_attachment_never_read`; `test_other_version_fails_the_gate`; `test_cost_state_last_record_wins`; `test_bash_results_carry_sandboxed_flag`; `test_price_usage_none_without_a_price`; `test_input_tokens_include_cache_counters`; `test_model_is_concrete_id`; `test_tool_use_joined_to_result_with_times_and_error`; `test_thinking_text_not_kept`; `test_subagent_file_attached`; `test_no_timestamps_means_no_messages`; `test_stream_fallback_gives_same_structure`; `test_fixture_is_synthetic` (the session id is the fixture's; every `cwd` is under `/Users/example/`; every assistant record, main thread and subagent, has a `requestRef` that resolves to an `api-request` record in the same file, which is what makes it the corrected fixture).

**Layer 2**

- `test_gateway.py` (`StubProvider`, real ledger and replay store, `FakeClock`). Ported: `test_transport_owner_counts_every_attempt`; `test_error_only_stream_retries_before_replying`; `test_provider_error_limit_and_circuit`; `test_model_identity_gate_off_by_default_and_halts_when_on`. New: `test_admit_exact_keys_drops_usage`; `test_admit_rejects_response_format_tools_stream_true`; `test_model_must_equal_wire_model`; `test_size_limit_uses_unescaped_utf8` (a 224,000-character Chinese prompt is accepted); `test_non_streaming_worker_gets_one_merged_body` (the branch Harness2 never tested); `test_temperature_max_tokens_and_system_message_unchanged_upstream`; `test_reply_status_only_200_402_403` (property over every fault kind and refusal); `test_non_200_is_neutral_with_x_should_retry_false`; `test_provider_text_never_reaches_worker` (a provider error containing `timeout 429` is replaced); `test_429_is_retried_with_long_backoff_never_returned`; `test_retries_exhausted_gives_provider_unavailable_costing_nothing`; `test_semaphore_bounds_open_reservations` (94 threads, `peak_in_flight == 16`, never more than 16 pending rows); `test_semaphore_released_during_backoff`; `test_identical_in_flight_requests_coalesce`; `test_replayed_request_costs_nothing_has_zero_usage_and_is_recorded`; `test_budget_cap_sets_halt_and_replies_402`; `test_job_ceiling_replies_403_without_halt_and_is_not_shared`; `test_halt_refuses_before_any_reservation`; `test_halt_lets_in_flight_calls_finish_and_settle`; `test_missing_usage_is_bounded_unknown_and_reply_still_200`; `test_context_limit_is_candidate_failure_not_retried_not_charged`; `test_config_faults_halt` (401, 402 and every 403); `test_quota_429_halts_provider_quota`; `test_sample_salt_prevents_replay_and_coalescing_across_samples`; `test_truncated_empty_is_flagged_delivered_and_stored`; `test_settle_and_circuit_finish_each_run_when_the_other_raises`; `test_client_disconnect_changes_nothing`; `test_unknown_job_recorded_under_unknown`; `test_refused_oversize_request_stores_excerpt_only`; `test_record_written_for_every_outcome_and_has_no_authorization`; `test_internal_fault_becomes_gateway_internal`; `test_close_job_counters`; `test_no_replay_only_in_qualification`; `test_ceiling_refusal_during_half_open_circuit_does_not_block_others` (the probe is refused at the reservation; four later requests proceed [A-M m2]); `test_candidate_400_is_neutral_for_the_circuit`; `test_enter_timeout_is_gateway_internal`; `test_halt_during_backoff_does_not_change_permit_count` and `test_exception_in_attempt_releases_slot` (the bounded semaphore has exactly `max_in_flight` permits afterwards); `test_many_small_messages_refused_or_within_limit` (nine messages are `invalid_request`; eight one-byte messages have `input_limit` at least the Harmony token count when the example venv is present); `test_message_with_extra_key_refused`; `test_limit_exceeded_usage_does_not_halt`; `test_store_before_settle_crash_replays_free` (a fault injected between commit and settle: the rerun replays, the row is `bounded_unknown`); `test_busy_ledger_at_settle_delivers_reply_and_halts_ledger_fault` [A-M m1]; `test_same_request_failing_in_two_attempts_becomes_candidate_failure` (and stays `provider_unavailable` when nothing else was served); `test_close_job_waits_for_in_flight_and_counts_them`; `test_closed_job_followers_and_waiters_leave`; `test_abort_queued_and_drain`; `test_refused_records_capped_and_kill_requested`; `test_rejected_attempts_do_not_use_up_the_request_ceiling`; `test_recovery_phase_only_after_a_charged_attempt`; `test_close_job_reports_prompt_token_counters`.
- `test_ingress_http.py`: `test_binds_loopback_only`; `test_route2_requires_job_token`; `test_wrong_token_is_unknown_job`; `test_route1_port_identifies_job`; `test_only_post_with_length`; `test_oversize_body_refused_unread`; `test_94_simultaneous_connections_all_answered`; `test_listener_closed_after_job`; `test_token_never_logged`. With `needs_example_venv`: `test_real_client_one_request_per_refusal_both_routes` (the unmodified upstream `LLM` class against every refusal code; request count 1; `llm._is_retryable` false [F-E3, X-E2]); `test_real_client_accepts_merged_body_and_zero_usage_replay` [F-E3].
- `test_relay.py` (`FakeGateway`; the real stub through `PipeChannel` and `FaultChannel`): `test_silent_stall_detected_and_reattached` (a channel that goes quiet without closing is marked dead after `pong_timeout_s`; the second bridge completes the request with one gateway call); `test_write_timeout_marks_bridge_dead`; `test_each_bridge_has_its_own_decoder` (a frame cut by the dying bridge does not swallow the next bridge's `HELLO`); `test_reader_threads_never_write` (a slow reader on the far side cannot deadlock a `PING`); `test_can_removes_live_request_and_flood_counts_live_only`; `test_flood_refusal_is_relay_overloaded`; `test_send_kill`; `test_total_chars_in_across_bridges`; `test_pool_threads_are_daemon`; `test_identity_comes_from_constructor`; `test_one_request_through_stub_and_session`; `test_32_interleaved_100kb_requests_over_one_pipe`; `test_out_of_order_replies`; `test_duplicate_in_flight_request_ignored`; `test_done_cache_resends_and_is_pruned_by_hello`; `test_bridge_killed_mid_request_completes_with_one_gateway_call` [L1]; `test_rotation_loses_nothing`; `test_stdio_mode_caches_nothing`; `test_flooding_job_gets_refusals_without_forwarding`; `test_rotation_counted`; `test_eight_frame_errors_kill_the_bridge`; `test_hostile_frames_do_not_crash_the_session`; `test_end_of_stream_marks_bridge_dead`; `test_backpressure_slow_reader`; `test_pipe_channel_mimics_sdk_decoding_and_counts_retained_bytes`; `test_ping_pong`.
- `test_bundle.py`: `test_agent_file_limits` (a `__pycache__` directory and a non-regular entry under `agents/` are skipped, not an error); `test_agent_override_bundles_stored_copy`; `test_check_file_shape`; `test_job_file_uses_vocab_file_and_ping_fields`; `test_extract_rejects_traversal_links_and_wrong_members`; `test_snippets_extract_and_pack` (runs both `python -c` sources); `test_worker_command_with_and_without_capture`; `test_job_file_shape_and_no_secret`; `test_collect_outputs_allow_list_caps_and_rehash`; `test_result_is_usable_matches_upstream_rule`; `test_kill_group`. With `needs_example_dir`: `test_bundle_is_deterministic_and_minimal` (no `benchmark.py`, one dataset); `test_bundle_ignores_workspace_edits_to_pinned_files`. Second round: `test_job_file_carries_repeat_sample_probe_endpoints_and_expect`; `test_wheelhouse_snippet_verifies_and_rejects` (runs the snippet on a small tar: good checksums pass, a changed wheel fails, an unexpected member is refused).
- `test_executor_types.py`: `test_classify_attempt_table` (one case per row, in order); `test_exit_zero_with_infra_refusals_is_not_ok` (return code 0 and a usable result with `provider_unavailable`, `gateway_internal`, `relay_unavailable`, `halted` or `budget_exhausted` in the codes); `test_timeout_with_provider_unavailable_is_infra`; `test_stub_local_refusals_reach_classification`; `test_reattach_exhausted_is_relay_lost_not_worker_error`; `test_unanswered_at_exit_is_relay_gap_only_for_self_exit`; `test_stream_budget_relay_overloaded_and_refusal_flood_are_the_jobs_ceiling`; `test_halt_kill_is_halted`; `test_check_selfcheck`; `test_sandbox_cost_micro`. Second round: `test_host_sleep_makes_timeout_interrupted`; `test_selfcheck_expect_problems_are_environment_problems`; `test_recorded_probes_are_never_isolation_problems`.
- `test_sandbox_client.py`: `test_cw_adapter_imports_lazily`; `test_missing_or_empty_auth_variable_is_executor_error`; `test_auth_headers_passed_explicitly_and_wandb_not_imported` (the fake `cwsandbox` module records `auth` on every call: an `AuthHeaders` with the four headers; an import hook fails on `wandb`); `test_auth_key_scrubbed_and_digest_in_key_facts`; `test_wandb_auth_mode_sets_key_in_process_with_explicit_entity_and_project`; `test_stdin_write_has_timeout`; `test_create_arguments_mapping` and `test_exception_mapping` (a fake `cwsandbox` module injected into `sys.modules` records keyword arguments; no positional image, no `Session`). Second round: `test_error_reason_mapping` (occupancy and create throttling retry; `RESOURCE_EXHAUSTED` without a reason is `billing` and not retryable; `PERMISSION_DENIED` is `permission`); `test_fallback_image_tried_once_on_pull_failure`; `test_entity_and_project_on_every_call`; `test_no_mount_argument_ever`.
- `test_plan_spans.py`: `test_one_llm_span_per_call_record_with_original_times`; `test_turns_by_role`; `test_usage_only_on_completed_spans`; `test_partial_usage_does_not_invent_zero_tokens` (ported); `test_replayed_and_refused_spans_are_hashed`; `test_failed_calls_carry_error`; `test_reasoning_part`; `test_learn_turn_moves_to_own_conversation_over_limit`; `test_parts_split` (250 calls give parts within the limit); `test_expected_spans_and_planned_bytes`; `test_plan_is_deterministic`; `test_proposer_plan_uses_concrete_model_and_total_input`; `test_proposer_tool_and_subagent_spans`; `test_killed_session_marked`; `test_outer_iteration_plan`; `test_outer_loop_conversation_rolls_over` (iterations 0 to 14 in `.p1`, 15 in `.p2` with the draft numbers); `test_proposer_parts_split_with_subagents` (a 400-span session; a subagent kept whole; one oversize subagent flattened into its own parts); `test_admission_only_record_planned_as_interrupted`; `test_proposer_tool_text_hex40_masked`; `test_unset_decisions_give_placeholder_project`; `test_validate_plan_catches_each_rule`; `test_conversation_ids_are_url_safe_and_bounded` (ids at the pattern maxima are shortened to 128). Second round: `test_turns_carry_no_content`; `test_solver_text_uses_the_jobs_dataset_rule`; `test_proposer_llm_span_has_no_cumulative_context`; `test_tool_result_attributed_or_strictest`; `test_proposer_tool_text_is_portable`; `test_outer_loop_arguments_guarded_by_verbatim_index`; `test_default_rules_hash_everything`.
- `test_readback.py`: `test_query_bodies_pinned`; `test_verify_exact_match`; `test_verify_reports_each_mismatch`; `test_verify_tolerates_known_normalisations` (ported).
- `test_plan_tables.py`: `test_inputs_are_strings_and_identical_across_candidates`; `test_no_null_or_nan_scores_ever`; `test_metric_promoted_only_when_complete`; `test_all_null_output_column_dropped`; `test_mismatch_with_result_file_drops_table`; `test_failed_job_has_no_table`; `test_fraction_true_equals_result_accuracy`; `test_trace_url_only_from_verified_receipt`; `test_check_table_catches_each_rule`; `test_only_the_four_named_metrics_become_scores` (LawBench's integer `tp`, `fp`, `fn` stay in `metrics_json`); `test_backend_unset_when_decision_unset`. Second round: `test_question_sha256_is_an_input_column`; `test_uspto_logs_hashes_until_cleared`; `test_long_label_is_hashed`.
- `test_plan_runs.py`: `test_run_ids_are_deterministic`; `test_candidate_artifact_metadata_carries_base_system`; `test_baseline_eval_run_logs_artifact`; `test_search_step_row` (the row carries `iteration`, the plan has `define_metrics`, no `step`); `test_search_step_plan_is_stable_without_ledger` (the same records give the same bytes); `test_run_config_is_allow_list_without_paths_or_account_facts`; `test_search_metrics_count_missing_dataset_as_incomplete`; `test_improvement_positive_for_new_best`. Second round: `test_withheld_candidate_file_logged_by_hash_only`; `test_evidence_is_hashes_and_sizes_only`; `test_artifact_metadata_guarded`; `test_search_run_resumes_one_id_with_iteration_axis`.
- `test_proposer_shim.py` (`fake_claude_cli`, `fake_claude_wrapper`): `test_cmd0_replaced_with_pinned_path`; `test_env_is_exactly_the_given_mapping`; `test_tee_stamps_every_line_utc`; `test_sigint_then_term_then_kill`; `test_graceful_timeout_keeps_result_event`; `test_process_group_is_gone_after_timeout` (a grandchild); `test_wrapper_kill_is_group_sigkill`; `test_group_reaped_after_normal_exit` (mode `spawns_child`: the background child is gone and `signals` says so); `test_session_argv_exact_per_mode` (the three lists of 3.19, token for token); `test_restricted_argv_has_no_allowed_tools_and_repeats_credential_denies`; `test_settings_and_budget_flags_in_every_mode`; `test_parse_upstream_argv_contract`; `test_popen_refuses_unpinned_argv0`; `test_deadline_is_monotonic_and_pauses_for_rate_limit_waits` (`FakeClock`: `api_retry` rate-limit lines move the deadline, capped at one session timeout); `test_wall_clock_jump_does_not_time_out`; `test_command_fingerprint_has_no_prompt_text`; `test_installed_restores_attributes_after_exception`; `test_file_not_found_propagates`.
- `test_proposer.py`: `test_classification_table`; `test_ok_session_mentioning_limit_reached_stays_ok`; `test_text_patterns_are_fallback_only`; `test_bad_hypothesis_is_no_candidates_not_a_stored_proposal`; `test_stray_workspace_file_quarantined`; `test_evolution_summary_change_restored`; `test_state_access_signals_set_evidence_exposed`; `test_frontier_before_stored_and_reused_on_replay`; `test_cost_fallback_chain_never_zero` (the `result` event; else the last `cost-state` record; else usage once per `message.id` at list prices; else the estimate); `test_budget_check_uses_session_cap_before_the_session`; `test_candidate_reference_latest_and_by_hash`; `test_timed_out_session_returns_false_even_with_pending_eval`; `test_pending_eval_copied_with_base_system`; `test_session_id_from_init_event_without_result`; `test_list_price_metered_once`; `test_binary_hash_mismatch_stops_campaign`; `test_spend_limit_and_auth_stop_campaign_and_are_not_settled`; `test_stored_proposal_replayed_without_a_session` [X-E4]; `test_settled_failure_returns_false_without_a_session`; `test_transcript_and_session_directory_copied_no_symlinks`; `test_pinned_file_edit_restored_and_recorded`; `test_env_fingerprint_change_stops`; `test_session_record_has_no_prompt_or_skill_text`; `test_solver_attempt_signals`; `test_proposer_budget_cap_halts`. With `needs_example_dir`: `test_real_wrapper_pins_path_and_env` (the real `claude_wrapper` from a workspace copy with the fake CLI [T-E3]). Second round: `test_exit_zero_without_result_event_is_not_success`; `test_structured_limit_fields_before_text` (the inferred limit fixture `stream_limit_synthetic.jsonl` is `limit_window` with a wait until the epoch in `EXPECTED.json`); `test_limit_window_waits_then_same_iteration` (`FakeClock`); `test_transient_reruns_bounded_then_stops`; `test_unrecognised_limit_halts`; `test_max_budget_result_is_a_settled_failure`; `test_model_checked_on_every_main_thread_event_and_fallback_events`; `test_subagent_models_recorded_not_asserted`; `test_effort_mismatch_from_init_or_transcript`; `test_unsandboxed_bash_result_is_settings_ignored`; `test_missing_transcript_in_restricted_mode_is_settings_ignored`; `test_integrity_violations_by_mode`; `test_init_with_plugins_skills_mcp_or_memory_paths_halts`; `test_generated_settings_restricted` (exactly three allow rules; a deny per existing agent file and per pinned entry; the sandbox block; an empty network allow-list); `test_no_blanket_state_deny_one_per_child_except_workspace`; `test_other_scratch_folders_denied_by_name`; `test_contained_and_upstream_settings`; `test_generated_settings_parse_as_json_with_common_keys_in_every_mode`; `test_override_merges_and_cannot_switch_off_the_sandbox`; `test_addendum_text_and_hash_recorded`; `test_scratch_dir_must_be_resolved_and_outside_state`; `test_transcript_located_by_glob_and_set_aside_copies_copied`; `test_cli_version_mismatch_stops`; `test_raw_events_of_error_results_are_kept`.

**Layer 3**

- `test_local_executors.py`: `test_local_timeout_kills_process_group`; `test_exit_zero_without_result_is_no_result`; `test_job_token_in_env_not_argv_or_job_file`; `test_no_gateway_listener_in_relay_mode`; `test_stub_listener_refuses_without_bearer`; `test_halt_ends_job_within_grace` (a halt set during a job: the worker is killed after the 60 s grace, on a `FakeClock`; the attempt is `halted`); `test_kill_requested_ends_job`; `test_job_stream_budget_is_the_jobs_ceiling`; `test_command_prefix_is_used`; `test_local_relay_with_fake_pkg_end_to_end`; `test_local_relay_daemon_mode_with_rotation` (`fake_pkg`; the socket under a short `/tmp` directory); `test_cleanup_kills_only_matching_process`. With `needs_example_venv`: `test_local_job_ok` (USPTO `no_memory` against `StubProvider` over the loopback ingress); `test_local_relay_job_same_result`; `test_local_relay_stream_drop_then_rerun_is_free` (attempt 1 dropped after 10 requests, attempt 2 ok, provider calls total 30); `test_import_check_ok_and_fail`. Second round: `test_host_sleep_detected_by_heartbeat` (a `FakeClock` whose wall clock jumps while the monotonic clock does not).
- `test_sandbox_executor.py` (`FakeSandboxClient`): `test_call_sequence` (the call log equals the list of 3.17, in order); `test_create_spec` (image, egress and DNS denied, `WANDB_API_KEY` empty, lifetime, 5 tags of at most 63 characters); `test_no_secret_in_any_exec_argv_or_environment`; `test_every_exec_has_timeout`; `test_open_egress_or_visible_credential_stops_the_campaign`; `test_missing_tokenizer_fails_before_worker_start`; `test_missing_exit_file_is_failure_even_with_exit_0`; `test_stream_drop_reattaches_in_daemon_mode`; `test_silent_stall_reattaches_in_daemon_mode`; `test_reattach_exhausted_kills_stub_and_is_relay_lost` (no wait for the deadline); `test_stream_drop_fails_attempt_in_stdio_mode`; `test_create_retried_once_then_fails`; `test_quota_backoff`; `test_stop_always_called_and_meter_row_closed`; `test_meter_row_opened_at_create_with_worst_case` (keyed by the sandbox id); `test_recreated_sandbox_has_its_own_meter_row`; `test_cleanup_closes_open_meter_rows`; `test_sandbox_budget_checked_before_create`; `test_cleanup_until_two_empty_lists`; `test_import_check_shares_one_sandbox_and_is_metered`; `test_import_check_create_failure_raises_executor_error`. With `needs_example_venv`: `test_sandbox_job_end_to_end_over_fake` (a real worker inside `FakeSandboxClient`). Second round: `test_wheelhouse_setup_sequence` (upload, size check, checksum check, install, in that order, each with a timeout); `test_wheelhouse_failure_is_env_setup_and_next_attempt_starts_from_zero`; `test_lifetime_includes_margin_and_setup_allowance`; `test_environment_proof_before_worker_start` (a wrong machine or freeze hash: no worker exec happens); `test_environment_identity_in_executor_json`; `test_billing_error_is_terminal`; `test_image_mode_skips_setup`; `test_fallback_reference_used_in_wheelhouse_mode`.
- `test_exporter.py` (`FakeLensSink`, `FakeWandbSink`): `test_order_plan_scan_meter_emit_readback_receipt`; `test_receipt_written_before_first_sdk_call`; `test_trace_id_recorded_before_span_end`; `test_secret_blocks_before_anything_is_sent`; `test_ingest_cap_skips_unit`; `test_interrupted_export_reads_back_never_reemits` (ported); `test_short_or_long_readback_is_partial`; `test_retry_partial_uses_new_ids_and_archives`; `test_verified_export_is_idempotent`; `test_dry_run_needs_no_sink_and_no_key`; `test_units_only_for_finished_work`; `test_eval_waits_for_job_units`; `test_table_logged_once_per_location`; `test_ces_fallback_recorded`; `test_bytes_metered_once_per_attempt`; `test_dry_run_with_unset_decisions` (placeholder project and backend `unset`: every unit gets a `dry_run` receipt; live they would be `skipped_policy`); `test_changed_plan_returns_final_receipt_marked_stale`; `test_supersede_starts_new_attempt_with_new_ids`; `test_live_export_replaces_dry_run_receipt`; `test_interrupted_job_unit_plans`; `test_export_runs_with_halt_set`; `test_scan_uses_key_digests`. Second round: `test_bucket_by_campaign_kind_and_recovery_for_retries`; `test_empty_bucket_refuses_before_sending`; `test_proposer_unit_over_session_cap_skipped`; `test_span_count_metered`; `test_upload_and_table_caps`; `test_privacy_finding_refuses`; `test_candidate_file_with_dataset_text_is_withheld`; `test_memory_log_cache_and_transcript_never_uploaded`; `test_receipt_records_amendment`; `test_amended_rule_marks_receipt_stale`; `test_export_does_not_create_a_project`; `test_verbatim_index_exact_for_listed_datasets`.
- `test_adapters.py`: `test_wandb_login_is_never_called` (the fake `wandb` module's `login` raises); `test_netrc_points_into_state` (`NETRC` is `STATE/export/empty.netrc`, mode 0600, before the fake module is imported); `test_wandb_dirs_point_into_state_and_console_off`; `test_api_key_passed_through_settings_and_api`; `test_define_metric_and_no_step`; `test_tracing_init_has_explicit_base_url`; `test_adapters_import_lazily`; `test_agentlens_call_order_against_fake_tracing` (init, conversation, turn, `on_trace` before any span ends, spans, attributes, errors, flush, shutdown); `test_flush_result_is_ignored`; `test_wandb_refuses_when_base_url_env_set`; `test_wandb_evaltable_arguments_against_fake_module`; `test_key_passed_explicitly_never_logged`. Second round: `test_refuses_when_weave_importable`; `test_weave_disabled_in_environment`; `test_init_settings_switch_off_metadata_and_set_dir` (the seven settings and `dir=` under `STATE`, against the fake module); `test_fake_home_netrc_byte_identical_after_export` (a fake home holding a `.netrc`: after a whole export against the fake `wandb` module the file's bytes are unchanged); `test_missing_key_refuses_without_default_chain`.
- `test_spawn.py`: `test_one_child_at_a_time`; `test_child_env_is_exporter_env`; `test_child_gets_key_over_inherited_pipe_not_env_or_argv`; `test_child_can_import_package_from_foreign_cwd` (the spawner is created while the working directory is a temp directory; the child still runs `python -m mhforge`); `test_timeout_kills_child_and_journals_failed`; `test_drain`.
- `test_jobs.py` (`FakeFacade`, `FakeExecutor`): `test_dispatch_forms` (the five upstream forms; an unknown form raises); `test_not_found_returns_zero_with_upstream_message`; `test_skip_is_decided_by_upstream_builder`; `test_prefix_mismatch_raises_contract_error`; `test_planted_result_is_quarantined_and_job_runs`; `test_planted_result_for_other_system_never_reaches_frontier` (a `val.json` for a name with no agent file, and one at an odd depth, are quarantined before the passthrough [A-U X2]); `test_changed_published_result_is_republished`; `test_journal_backed_result_reused_after_crash`; `test_tainted_or_failed_attempt_is_never_reused`; `test_one_candidate_attempt_then_failed`; `test_timeout_is_not_rerun`; `test_attempts_counted_per_candidate_hash` (a re-proposed file gets a fresh budget); `test_reset_job_gives_fresh_budget`; `test_baseline_failures_are_infrastructure`; `test_attempts_persist_across_restart`; `test_infra_attempts_are_separate_and_stop_the_campaign`; `test_infra_attempts_wait_between_tries` (60 s, 120 s, ... on a `FakeClock`); `test_request_ceiling_not_retried`; `test_import_check_probe_failure_is_infra_and_rejects_nobody`; `test_import_check_candidate_failure_after_good_probe_rejects`; `test_changed_candidate_file_runs_stored_copy`; `test_test_job_uses_validated_memory_and_code`; `test_passthrough_uses_dash_P_and_verifies_first`; `test_planted_module_in_workspace_is_not_imported` (with `needs_example_dir` and `needs_yaml`: the real passthrough command with a `random.py` in the workspace root returns 0 [R1]); `test_launcher_log_paths_are_upstreams`; `test_publish_upstream_policy`; `test_quarantine_policy_publishes_nothing_for_partial_candidate`; `test_route1_rewrites_model_and_api_base`; `test_halt_after_job_raises_campaign_stop`; `test_test_jobs_get_memory_input`; `test_missing_memory_counts_as_failed`; `test_launcher_log_format_and_name`; `test_return_code_mirrors_upstream`; `test_job_record_has_no_token_or_address`; `test_jobs_run_in_config_dataset_order`. With `needs_yaml` and `needs_example_dir`: `test_plan_jobs_against_real_benchmark_builders` (`--temperature 0.0` present; the nine-token prefix [X-E9]). Second round: `test_env_setup_failure_is_infrastructure_up_to_three`; `test_non_retryable_executor_error_stops_at_once`; `test_parallel_dataset_jobs_per_split_test_always_one`; `test_loaded_class_rejection`; `test_repeat_job_has_own_key_and_is_never_published`; `test_host_sleep_attempt_is_interrupted`; `test_job_record_has_context_counters`.
- `test_qualification.py`: `test_engine_snapshot_scope`; `test_offline_gate_lists_every_failed_check`; `test_gate_workspace_check_works_before_workspace_exists`; `test_gate_requires_smoke_receipt_for_this_engine`; `test_gate_requires_filled_protocol`; `test_observations`; `test_checks_table`; `test_forecast_arithmetic_and_stop`; `test_forecast_checks_sandbox_and_proposer_budgets`; `test_qualified_only_for_same_engine_and_manifest`; `test_restart_rules`. Second round: `test_gate_two_stage_protocol`; `test_gate_checks_wheelhouse_tar_and_requirements_hash`; `test_import_allowed_only_for_named_differences` (each allowed path alone passes; a changed `routing.route`, `sandbox.base_image` or `decisions.proposer_permission_mode` fails; a changed engine fails; the same project fails); `test_import_needs_reconciliation`; `test_passed_json_records_environment_and_import`; `test_noise_stats`; `test_forecast_includes_calibration_and_setup_time`.
- `test_audit.py`: `test_clean_state_passes`; `test_detects_each_tamper` (changed usage, missing ledger row, missing raw file, a reply status outside the three, a secret in a record, a bare key found by digest, a succeeded job with a tainted code); `test_in_flight_at_close_and_unrecorded_refusals_are_accounted`; `test_lists_reconciled_limit_exceeded_and_repeatable_rows`; `test_audit_creates_no_file`.

**Layer 4**

- `test_driver.py` (`FakeMetaHarness`): `test_install_and_uninstall_restore_globals`; `test_call_order_baselines_iteration_test`; `test_phase_tracking_baseline_search_test`; `test_campaign_stop_escapes_run_evolve_and_writes_stopped_json`; `test_interrupted_flag_untouched`; `test_stop_request_honoured_at_iteration_boundary_only`; `test_iteration_arithmetic`; `test_invalid_candidate_rejected_before_import_check`; `test_over_limit_candidates_rejected`; `test_summary_wrapper_calls_original_then_writes_iteration_record`; `test_iteration_zero_record`; `test_delta_uses_frontier_before_iteration`; `test_missing_dataset_counts_as_zero`; `test_route_1_keeps_config_model`; `test_startup_recovery_order`; `test_startup_closes_open_proposer_meter_rows`; `test_no_listener_open_during_propose`; `test_other_exception_writes_stopped_json_internal_error`; `test_baseline_incomplete_stops_before_first_proposal`; `test_stop_leaves_no_pending_row` (the stop path aborts, drains and journals `undrained`); `test_calibration_runs_test_jobs_without_finalized_json_or_published_results`; `test_finalisation_reuses_calibration_attempts`; `test_test_entry_journaled_and_blocks_search_gate`; `test_make_executor_kinds`. With `needs_yaml`: `test_count_iterations_equals_upstream` (compares `driver.count_iterations` with `meta_harness.count_iterations_from_summary` on the same file, including a bad line, a blank line and a row without `iteration`). With `needs_yaml` and `needs_example_dir`: `test_contract_holds_for_pinned_upstream`; `test_calibration_then_search_is_not_blocked_by_upstream` (the real `meta_harness`: baselines, calibration, then a search starts; the control, upstream's own `test=True`, blocks it [A-U E6]); `test_real_upstream_baselines_only` (the real `meta_harness` from a workspace copy with a fake executor); `test_campaign_stop_leaves_no_summary_row` [X-E4]; `test_resume_replays_proposal_and_skips_jobs` [X-E4]. Second round: `test_max_iterations_this_run_pauses_and_resumes`; `test_paused_run_does_not_unlock_test`; `test_calibration_gate_bands_and_verdict` (G1 hard, G2 a soft flag, content on every call); `test_calibration_books_phase_calibration`; `test_noise_repeats_use_repeat_scope_and_fresh_cache`; `test_iteration_record_has_context_figures`; `test_run_started_records_protocol_hash`.
- `test_preflight.py` (a fake `AccountReader` and a scripted `proposer_session` callable defined in the test file): `test_checks_in_declared_order`; `test_describe_lists_needs`; `test_offline_checks_run`; `test_failed_required_check_stops`; `test_receipts_written`. Second round: `test_steps_in_declared_order_with_old_number_mapping`; `test_account_check_runs_unfrozen_and_writes_local_state_only`; `test_create_project_private_and_read_back` (a project that reads back `USER_READ` fails); `test_probe_script_is_status_only` (every line reports a name and an exit code; no line can print what it read); `test_probe_expectations_per_mode`; `test_reconciliation_verdicts`; `test_imported_campaign_runs_only_per_project_checks`; `test_teardown_list`; `test_trial_plans_stop_and_ask`.
- `test_publish.py` (ported from H2 `test_report.py` L12-L44): `test_bundle_is_allow_list_only_and_scrubs_paths`; `test_bundle_refuses_on_secret` (by pattern and by key digest); `test_bundle_includes_stopped_json_and_halt_history`; `test_bundle_excludes_keydigests_and_runtime`; `test_foreign_conversation_blocks_publish`; `test_publish_refuses_by_default`. Second round: `test_bundle_excludes_account_dir`; `test_preflight_campaign_cannot_publish`; `test_bundle_includes_amendments`; `test_bundle_refuses_on_local_path_or_hostname`.

**Layer 5**

- `test_cli.py`: `test_status_and_validate_work_offline`; `test_each_live_command_refuses_without_live_flag`; `test_each_live_command_refuses_draft_manifest_with_live_flag`; `test_exit_codes`; `test_cli_never_prints_key_value` (a canary value in the key variable never appears in output); `test_clear_halt_needs_note`; `test_export_dry_run_needs_no_key`; `test_halt_command_sets_operator_halt_without_lock` (while another process holds `runner.lock`); `test_reconcile_and_reset_job_take_lock_and_journal`; `test_evaluate_modes_are_exclusive`; `test_runtime_json_records_key_scrubbed`. Second round: `test_key_source_prompt_one_prompt_per_role_enter_reuses`; `test_key_source_fd_lines_and_missing_role_refuses`; `test_key_source_env_scrubs_and_is_default_without_tty`; `test_no_flag_accepts_a_key_value`; `test_live_command_sets_netrc_to_empty_state_file_before_sdk_import`; `test_account_check_unfrozen_manifest`; `test_amend_appends_chain_row_and_takes_export_lock`; `test_search_refuses_when_started_inside_claude_code` (and so do the proposer steps of `preflight --live`; the offline smoke with the fake CLI does not); `test_max_iterations_this_run_flag`; `test_status_prints_stall_warning`.
- `test_static_rules.py` (AST and text checks over `SC/mhforge`): `test_top_level_imports_are_standard_library_or_mhforge`; `test_module_imports_no_third_party` (imports every `mhforge` module in a subprocess with an import hook that fails on `yaml`, `wandb`, `cwevals`, `coreweave`, `cwsandbox`, `wandb_workspaces`, `opentelemetry`, `httpx`); `test_no_netrc_path_literal_outside_scan_patterns` (no source file names `~/.netrc` or opens a netrc file; allowed: setting the `NETRC` variable to the empty state file in `cli.py` and the two adapters, the deny-rule literals of `proposer.CREDENTIAL_PATHS` and `proposer_shim.CREDENTIAL_READ_DENIES`, and the `os.stat`-only size and mtime check of PF8 in `preflight.py`); `test_no_wandb_login_call`; `test_no_environ_copy_and_no_envless_popen`; `test_environment_access_only_in_listed_modules` (the table at the head of section 3); `test_no_path_resolve_on_worker_python` (no `.resolve()` or `realpath` in the five modules that handle the interpreter path); `test_no_re_match_for_identifiers`; `test_insandbox_files_self_contained`; `test_layer_imports` (each module imports only the layers section 6 allows it); `test_spdx_headers_match_provenance_table`; `test_no_file_name_hits_root_gitignore` (no directory named `data`, `build`, `lib`, `dist`, `jobs`, `env`, `venv`, `var`, `parts`, `target`; no `*.log`; and, when `git` is present, `git check-ignore --no-index` over every file under `SC` outside `state/` reports nothing); `test_worker_lock_equals_example_lock` (`needs_example_dir`). Second round: `test_no_sdk_default_credential_chain` (text check: every `wandb.Api(` has `api_key=`, every `wandb.init(` has `settings=`, every `tracing.init(` has `api_key=`, every `WandbAuth(` has `entity=` and `project=`); `test_third_party_data_hashes_match_example` (`needs_example_dir`); `test_requirements_lock_hash_matches_manifest_draft`; `test_protocol_template_markers_and_bands` (`protocol.md` has both stage markers, its G1 and G2 bands equal the draft manifest's `calibration.gates`, and no gate cites the paper's few-shot figure).
- `test_smoke.py` (`needs_example_venv`): `test_offline_smoke_local_relay`; `test_offline_smoke_local`; `test_offline_smoke_resume_runs_nothing`.
- `test_e2e_offline.py` (`needs_example_venv`; each runs the venv Python as a subprocess):
  - `test_crash_resume_pays_once`: the runner killed with SIGKILL in the middle of a baseline job; restarted; the run completes; provider calls total 260 plus at most the calls that were open at the kill; those rows are `bounded_unknown` (a call whose reply was stored before the kill is replayed and not paid again); no orphan process remains.
  - `test_halt_stops_the_outer_loop`: a cap that the second baseline exceeds; exit 2 with `budget:baseline`; the job ends within the grace period, not at its timeout; `evolution_summary.jsonl` has no stray row; no `pending` ledger row remains; later runner commands refuse until the halt is cleared, while `export --dry-run` still works.
  - `test_search_with_fake_proposer`: the fake CLI proposes three candidates in iteration 1: one sound, one that fails its import, one that raises on one dataset. Expect the import failure dropped, the partial candidate recorded as incomplete in our iteration record, the sound one scored, our improvement metric recorded. Then a kill during iteration 2's jobs and a resume: one proposer session per iteration in total. The search is run in two blocks (`--max-iterations-this-run 1`): the first ends `paused` with exit 0 and does not unlock `evaluate --test`.
  - `test_proposer_stop_and_resume_same_iteration`: the fake CLI answers iteration 1 with a spend-limit stop, then (after the restart) with a normal session. Expect exit 2 with `proposer_spend_limit`, a session record that is not a settled failure, both sessions metered, and exactly one iteration-1 proposal after the restart; then the same with mode `no_result_exit0` followed by `ok` inside one invocation (a transient rerun, no stop).
  - `test_hostile_candidate`: a candidate that prints 50 MB, forks children that outlive it, dumps its environment into its output, calls the model 2,000 times with distinct prompts, catches every error and keeps calling, sends one oversize request and one request of 3,000 one-byte messages. Expect: outputs within the collection caps; no orphan; no key in any copied file; `request_ceiling` reached and classified; at most `gateway.max_refused_records_per_job` refusal records for the attempt and the rest counted; the oversize request refused with an excerpt-only record; the many-message request refused as `invalid_request` with **no halt**; the host process and the campaign intact.

### 5.3 The offline end-to-end smoke

`PYTHONPATH=SC EX/.venv/bin/python -m mhforge offline-smoke [--executor local_relay|local] [--keep]`. No network, no credentials, no spend. Needs the example venv (exit 3 with a clear message otherwise).

Steps:

1. Start `stubprovider.serve_stub` on `127.0.0.1`. Build a smoke manifest from `manifest.draft.json`: campaign `smoke-<utc>`, `state_dir` `state/smoke-<utc>`, `provider.kind = "stub"` with the stub's base URL, the chosen local executor, `search.iterations = 0`, `telemetry.export_mode = "manual"`, the upstream digest computed on the spot, `upstream.example_dir` and `upstream.worker_python` as absolute paths (made absolute by the path rule of 2.3, never resolved). The live decisions stay unset: the smoke is not a live command. **Write it to `SC/state/_smoke/sc/manifest.json`** and use that directory as the smoke's own `SC` (rule 2 of 2.3: the state lands beside the manifest, at `SC/state/_smoke/sc/state/smoke-<utc>/`, which is inside the git-ignored `state/`). Freeze it there.
2. Set a canary value in the variable named by `provider.api_key_env`. Install `util.loopback_guard()`. Compute `network_guard_prefix()`: when the platform offers a loopback-only profile the workers run under it (`command_prefix`), otherwise without; the receipt records which (`worker_network_guard`).
3. `driver.build_runtime` with `ForgeProviderClient(manifest, environ=os.environ, scrub=util.scrub_process_env)` pointed at the stub (the real HTTP and streaming code path; the canary is overwritten in the environment block and removed), then `Driver.run_baselines()`: upstream's `run_evolve` runs Phase 0 for `no_memory` and `fewshot_all`. Six real workers (the example venv's Python, real `inner_loop`, real LiteLLM) run under the capture entry; every solver call goes through LiteLLM, the stub or the loopback ingress, the gateway core, the ledger and the streaming merge. Then the real `benchmark.py --frontier`.
4. `Exporter(dry_run=True).export_all()`: with the decisions unset the planners use the placeholder project and backend `unset` (2.13, 2.14), and every unit ends as a `dry_run` receipt. Then `audit`.
5. `Driver.run_baselines()` a second time on the same state.

Invariants (any failure is `SmokeFailed`, exit 5):

| # | Invariant |
|---|---|
| 1 | six job records `succeeded`; each published `val.json` is usable; `frontier_val.json` has a `_pareto` list; `summary.json` exists; six launcher logs in upstream's format |
| 2 | exactly 260 call records with outcome `completed` (2 x (30 + 50 + 50)), none refused or failed; every reply status 200; 260 raw stream files; no gateway request during the second validation pass |
| 3 | `ledger.verify_totals()` is empty; committed spend equals the sum of call costs; only phase `baseline` has spend; no halt |
| 4 | each sidecar has `2 x n_val` `eval` rows for the wanted split; joined examples number 30, 50, 50 per baseline, all matched, and their correct counts equal the `val.json` files |
| 5 | six solver span plans, the iteration-0 outer plan, two evaluate run plans with six table plans validate (all with `placeholder_project: true`, tables with backend `unset`); table row counts 30, 50, 50; no null score; every discovered unit has a `dry_run` receipt and no other status; with the draft's rules every prompt, reply and reasoning part of every solver span is in `hash` form, no turn carries a message, every table has `question_sha256` and the USPTO tables carry `prediction_sha256` and `target_sha256`, not text; no run plan names a file other than a candidate file |
| 6 | the secret scan of the whole state directory (patterns, the canary as a known value, and the key digests) has no finding; in particular no `jobfile.json` is a finding; the canary appears in no file; no job file or record names the key variable; after the provider client was built, `ps eww` of the runner (when `ps` is available) does not show the canary |
| 7 | `audit` returns no problem |
| 8 | the second run plans no job, makes no provider call and leaves the ledger unchanged |
| 9 | the tree hash of `EX` is the same before and after; `git status --short` of the repository (when `git` is present) shows nothing new outside `showcase/` |
| 10 | with `--executor local_relay`, the runner process opened no listening socket other than the stub provider's (the relay stub, a child process, listens on `127.0.0.1` behind a per-attempt bearer) |
| 11 | no `pending` ledger row, no `open` meter row, no file created outside `SC/state/` (and the `/tmp/mhf-*` socket directories of daemon mode are gone); `sys.path` is the same before and after; the workspace has no file in its root that is not pinned (apart from `uv.lock`) |
| 12 | the six job records carry `gateway.prompt_tokens_max > 0` and `result.memory_context_chars`; the iteration-0 record's candidates carry `context` |

On success the smoke writes `SC/state/_smoke/receipt.json` with the engine digest (the offline gate requires it) and `worker_network_guard`, and removes `SC/state/_smoke/sc/` unless `--keep`.

---
## 6. Build order

Six layers. A file imports only the standard library and files of earlier layers (plus files of its own owner). Within a layer every owner has a disjoint set of files (source, its tests, its fakes and fixtures), so the owners of one layer work at the same time, each seeing only this spec, the upstream code and the earlier layers. Where an owner needs something from a parallel or later owner it codes against the protocol named in section 3 and its own fake. Every source file has a test file with the same owner. Line counts are estimates of source only.

| Layer | Owner | Files (source; tests and support) | Imports from | Est. lines |
|---|---|---|---|---|
| 0 | `L0-core` | `mhforge/__init__.py`, `errors.py`, `util.py`, `wire.py`, `paths.py`, `scan.py`; `tests/__init__.py`, `tests/helpers.py`, `tests/fixtures/manifest_valid.json`, `test_errors.py`, `test_util.py`, `test_wire.py`, `test_paths.py`, `test_scan.py`; `pyproject.toml`, `.gitignore`, `LICENSE-APACHE-2.0` | standard library | 1,090 |
| 0 | `L0-frames` | `insandbox/__init__.py`, `insandbox/mhf_frames.py`; `test_frames.py` | standard library | 190 |
| 0 | `L0-capture` | `insandbox/mhf_capture_entry.py`; `tests/fixtures/fake_pkg/**`, `test_capture_entry.py` | standard library (upstream at test time) | 320 |
| 0 | `L0-sse` | `sse.py`; `tests/fixtures/sse_*.txt`, `test_sse.py` | standard library | 150 |
| 0 | `L0-docs` | `README.md`, `protocol.md`, `THIRD_PARTY_DATA.md`, `PROVENANCE.md`, `worker-image/Dockerfile`, `worker-image/README.md`, `worker-image/uv.lock`, `worker-image/requirements.lock.txt` | none (documents; Appendix C is the ported-file table, Appendix G the protocol and data-notice text) | n/a |
| 1 | `L1-manifest` | `manifest.py`, `state.py`, `procenv.py`, `manifest.draft.json`; `test_manifest.py`, `test_state.py`, `test_procenv.py` | L0-core (including `scan`) | 1,200 |
| 1 | `L1-ledger` | `ledger.py`, `replay.py`, `circuit.py`; `test_ledger.py`, `test_replay.py`, `test_circuit.py` | L0-core | 1,010 |
| 1 | `L1-provider` | `provider.py`, `stubprovider.py`; `test_provider.py`, `test_stubprovider.py` | L0-core, L0-sse | 690 |
| 1 | `L1-stub` | `insandbox/mhf_relay_stub.py`, `insandbox/mhf_selfcheck.py`; `test_relay_stub.py`, `test_selfcheck.py` | L0-frames (L0-core in tests only) | 820 |
| 1 | `L1-workspace` | `journal.py`, `workspace.py`; `test_journal.py`, `test_workspace.py` | L0-core | 630 |
| 1 | `L1-telemetry-pure` | `telemetry/__init__.py`, `telemetry/policy.py`, `telemetry/join.py`, `telemetry/transcript.py`; `tests/fixtures/{call_records/, sidecar_uspto_val.jsonl, transcript_synthetic/, stream_synthetic.jsonl, stream_limit_synthetic.jsonl}`, `test_policy.py`, `test_join.py`, `test_transcript.py` | L0-core | 930 |
| 2 | `L2-gateway` | `gateway.py`, `ingress_http.py`; `test_gateway.py`, `test_ingress_http.py` | L0-core, L0-sse, L1-ledger, L1-provider | 800 |
| 2 | `L2-relay` | `relay.py`; `tests/fake_channel.py`, `test_relay.py` | L0-core, L0-frames (L1-stub in tests) | 460 |
| 2 | `L2-bundle` | `bundle.py`, `executors/__init__.py`, `executors/sandbox_client.py`; `test_bundle.py`, `test_executor_types.py`, `test_sandbox_client.py` | L0-core, L0-frames, L0-capture, L1-stub (as files to pack) | 950 |
| 2 | `L2-spans` | `telemetry/plan_spans.py`, `telemetry/readback.py`; `test_plan_spans.py`, `test_readback.py` | L0-core, L1-telemetry-pure | 780 |
| 2 | `L2-tables` | `telemetry/plan_tables.py`, `telemetry/plan_runs.py`; `test_plan_tables.py`, `test_plan_runs.py` | L0-core, L1-telemetry-pure, L1-manifest (`manifest.run_config`) | 700 |
| 2 | `L2-proposer` | `proposer_shim.py`, `proposer.py`; `tests/fake_claude_cli.py`, `tests/fake_claude_wrapper.py`, `test_proposer_shim.py`, `test_proposer.py` | L0-core, L1-manifest (`procenv`), L1-ledger, L1-workspace, L1-telemetry-pure (`transcript`) | 1,260 |
| 3 | `L3-exec-local` | `executors/local.py`, `executors/local_relay.py`; `test_local_executors.py` | L0, L1-manifest (`procenv`), L2-gateway (`ingress_http`), L2-relay, L2-bundle | 570 |
| 3 | `L3-exec-sandbox` | `executors/sandbox.py`; `tests/fake_sandbox.py`, `test_sandbox_executor.py` | L0, L1-ledger, L1-manifest (`procenv`), L2-relay, L2-bundle | 640 |
| 3 | `L3-jobs` | `jobs.py`; `tests/fake_facade.py`, `test_jobs.py` | L0, L1-ledger, L1-workspace, L1-manifest (`procenv`), L2-bundle (the qualified environment's freeze hash is injected, not imported) | 710 |
| 3 | `L3-export` | `telemetry/exporter.py`, `telemetry/spawn.py`, `telemetry/adapter_agentlens.py`, `telemetry/adapter_wandb.py`; `tests/fake_sinks.py`, `test_exporter.py`, `test_spawn.py`, `test_adapters.py` | L0 (including `scan`), L1-ledger, L1-workspace (`journal`), L1-manifest (`procenv`, `state`), L1-telemetry-pure, L2-spans, L2-tables | 1,230 |
| 3 | `L3-qualification` | `qualification.py`, `audit.py`; `test_qualification.py`, `test_audit.py` | L0 (including `scan`), L1-manifest, L1-ledger, L1-workspace, L1-telemetry-pure, L2-bundle (`executors.sandbox_cost_micro`) | 760 |
| 4 | `L4-driver` | `driver.py`; `tests/fake_meta_harness.py`, `test_driver.py` | everything in layers 0 to 3 except the telemetry package (it uses `qualification.noise_stats` and `qualified_environment`) | 700 |
| 4 | `L4-preflight` | `preflight.py`; `test_preflight.py` | L0, L1, L2-bundle (`executors` types), L3-export (sink protocols), L3-qualification | 820 |
| 4 | `L4-publish` | `publish.py`; `test_publish.py` | L0 (including `scan`), L1-telemetry-pure (`policy.portable`), L2-spans (`readback`), L3-export | 290 |
| 5 | `L5-integration` | `smoke.py`, `cli.py`, `__main__.py`; `test_cli.py`, `test_smoke.py`, `test_e2e_offline.py`, `test_static_rules.py` | everything | 1,040 |

The estimates total about 18,700 lines of source; the largest owners (`L2-proposer` at 1,260, `L3-export` at 1,230, `L1-manifest` at 1,200) are under the limit of about 1,300 lines per owner. The 500-line figure for a file is a target, not a limit. These files are expected above it, and their owners should split helper functions into private sections rather than new modules (a new module needs a new owner entry and a test file): `proposer.py` (about 920), `preflight.py` (820), `manifest.py` (760), `jobs.py` (710), `driver.py` (700), `cli.py` (700), `ledger.py` (660), `sandbox.py` (640), `gateway.py` (630), `exporter.py` (570), `mhf_relay_stub.py` (560), `plan_spans.py` (560), `qualification.py` (560). The second round added no source module: every new function went into an existing file of its owner, so the owner list and the list of test files are the ones of the first revision. The new files are documents and fixtures (`THIRD_PARTY_DATA.md`, `worker-image/requirements.lock.txt`, `stream_limit_synthetic.jsonl`, three `fake_pkg` modules).

Layering changes made by the revision, each because the first draft needed something from the same or a later layer [A-S]: `scan.py` moved from `telemetry/` (layer 1, a parallel owner of `manifest.py`, which must apply its patterns) to `mhforge/scan.py` in `L0-core`; `loopback_guard` moved from `smoke.py` (layer 5) to `util.py` (layer 0), because `tests/helpers.py` is layer 0; `make_executor` is defined only in `driver.py` (layer 4) and is no longer listed in `executors/__init__.py` (layer 2); `sandbox_cost_micro` moved from `executors/sandbox.py` to `executors/__init__.py` (layer 2), because the forecast in `qualification.py` is a parallel owner of the sandbox executor in layer 3; `portable` moved from `publish.py` (layer 4) to `telemetry/policy.py` (layer 1), because the run planners of layer 2 apply it. `test_layer_imports` checks the import rule mechanically.

**Contracts between owners that must not drift**, each pinned by a test owned by the later owner:

| Contract | Written by | Read by | Pinned by |
|---|---|---|---|
| frame format (2.9) | `L0-frames` | `L1-stub`, `L2-relay` | both import the one codec; `test_relays_94_concurrent_posts`, `test_32_interleaved_100kb_requests_over_one_pipe` |
| refusal table (3.2) | `L0-core` | `L1-stub` (two rows copied), `L2-relay` (`relay_overloaded`) | `test_local_refusals_equal_wire_table`; `test_flood_refusal_is_relay_overloaded` |
| credential-name pattern (3.2) | `L0-core` | `L1-manifest` (`procenv`), `L1-stub` (copy in the selfcheck) | `test_pattern_and_exempt_names_equal_wire` |
| sidecar rows (2.8) | `L0-capture` | `L1-telemetry-pure` | the fixture `sidecar_uspto_val.jsonl` follows 2.8; `test_local_job_ok` joins a real sidecar |
| call record (2.5) | `L2-gateway` | `L1-telemetry-pure`, `L2-spans`, `L3-qualification` | the fixtures `call_records/` follow 2.5; `test_record_written_for_every_outcome_and_has_no_authorization` compares the keys of a real record with the fixture's |
| ledger DDL (2.10) | `L1-ledger` | `L1-manifest` (`status`, halt check, by SQL), `L3-qualification` (`audit`, by SQL), `L4-publish` (history export), `L5-integration` (`halt`) | `test_status_and_validate_work_offline` runs `status` on a real ledger; `test_audit_creates_no_file` |
| job file and exit file (2.6) | `L2-bundle`, `L1-stub` | `L1-stub`, executors | `test_local_relay_with_fake_pkg_end_to_end`; `test_stub_local_refusals_reach_classification` |
| key digests file (2.1) | `L0-core` (`util.append_key_digest`), written by `L5-integration` | `L0-core` (`scan.load_digests`), `L3-export`, `L3-qualification`, `L4-publish` | `test_bare_key_found_by_digest`; `test_scan_uses_key_digests` |
| stored proposals and candidate copies (2.11) | `L2-proposer` | `L3-jobs` (through the injected `candidate_reference`), `L3-export` | `test_changed_candidate_file_runs_stored_copy`; `test_resume_replays_proposal_and_skips_jobs` |
| phase, status, outcome, scope and content-field constants (3.2) | `L0-core` | every later owner | `test_phases_statuses_outcomes_and_scopes_are_the_spec_lists`; `test_seven_phase_rows_and_new_phases_reserve` |
| transcript converter and its fixture (3.21, 5.1) | `L1-telemetry-pure` | `L2-proposer` (sandbox proof, cost fallback, request efforts), `L2-spans` | `test_totals_equal_expected_json`; `test_unsandboxed_bash_result_is_settings_ignored`; `test_cost_fallback_chain_never_zero` |
| verbatim index and privacy scan classes (3.24) | `L0-core` | `L1-telemetry-pure` (`policy.guard_text`), `L2-spans`, `L2-tables`, `L3-export`, `L4-publish` | `test_verbatim_run_and_exact_match`; `test_candidate_file_with_dataset_text_is_withheld` |
| amendments file and effective manifest (2.3 rule A) | `L1-manifest` | `L3-export`, `L4-publish`, `L5-integration` (`amend`) | `test_amendment_chain_and_effective_manifest`; `test_receipt_records_amendment` |
| `passed.json` with the environment identity and the import record (3.22) | `L3-qualification` | `L3-jobs` and `L3-exec-sandbox` (the freeze hash, injected by `L4-driver`), `L1-manifest` (the `search` gate, by reading the file) | `test_passed_json_records_environment_and_import`; `test_environment_proof_before_worker_start` |
| meter names, buckets and caps (2.10, 3.6) | `L1-ledger` | `L3-export`, `L3-exec-sandbox`, `L2-proposer`, `L1-manifest` (`status`, by SQL) | `test_lens_buckets_cap_separately_and_in_total`; `test_bucket_by_campaign_kind_and_recovery_for_retries` |
| key mapping and key sources (3.27) | `L5-integration` | `L1-provider`, `L2-bundle` (`sandbox_client`), `L3-export` (adapters, spawner) | `test_key_source_prompt_one_prompt_per_role_enter_reuses`; `test_child_gets_key_over_inherited_pipe_not_env_or_argv` |
| journal ops (2.7) | `L3-jobs`, `L2-proposer`, `L4-driver` | `L3-export` (`discover`), `L1-manifest` (`status`) | `test_units_only_for_finished_work`; the offline smoke |
| span, table, run plans and receipts (2.13 to 2.16) | `L2-spans`, `L2-tables` | `L3-export`, `L4-publish` | `test_order_plan_scan_meter_emit_readback_receipt` |

**Gates between layers.** After layer 2: `test_gateway.py` green, including the neutral-refusal and the 94-concurrent cases (the money path), `test_relay.py` green with real stub subprocesses including the bridge-kill and rotation cases, and `test_proposer.py` and `test_proposer_shim.py` green, including the exact argv of each permission mode, the generated settings and the limit classifier (the proposer is the other place where money is spent). After layer 3: one baseline job through `LocalExecutor` and through `LocalRelayExecutor` with identical `val.json` (venv tests). After layer 4: `test_real_upstream_baselines_only`. After layer 5: `offline-smoke` green for both local executors, plus the crash-resume and hostile-candidate tests. Every layer ends with the full suite green from `SC`.

---

## 7. Deviations from upstream behaviour

For the article's fidelity statement. "Upstream" means running `uv run python meta_harness.py` in `EX` at `f805ad3` as its README says. No upstream file is edited.

| # | Deviation | Reason |
|---|---|---|
| V1 | Four `meta_harness` module globals (`run_benchmark`, `propose_claude`, `validate_candidates`, `update_evolution_summary`) and, for one session at a time, `claude_wrapper.subprocess` and `claude_wrapper.build_command` are replaced at run time. | The only way to route jobs through executors and the proposer through an allow-listed environment without an upstream edit. Upstream's own tests replace `run_benchmark` the same way. |
| V2 | The loop runs from a byte-identical copy of the example's tracked files under `showcase/state/<campaign>/workspace`. Absolute paths in the task prompt (`meta_harness.py` L136-L146) differ accordingly. The three upstream modules are loaded from that copy by file path, from hash-checked bytes, without putting the copy on `sys.path`; `benchmark.py` children are started with `-P`. The copy sits inside this build's state directory, so our own records are two directories above the proposer's working directory (V20). | Upstream writes `agents/` and `logs/` into the example directory, neither is git-ignored, and `agents/` is shared across runs. A workspace on `sys.path` would let a file the proposer wrote run inside the runner. |
| V3 | Workers start as `python mhf_capture_entry.py ... -- <same arguments>`, which replaces three attributes for recording (`ProviderLLM._generate_one`, `LLM.batch`, the dataset loaders' evaluator) and then runs `inner_loop` with `runpy`, instead of `env PYTHONPATH=.. uv run --project .. python -m text_classification.inner_loop`. Same interpreter, packages and arguments (the arguments come from upstream's own builders). | Per-example predictions exist only in memory upstream. The wrappers return and raise exactly what the originals do. |
| V4 | The worker environment is built from nothing and adds `LITELLM_LOCAL_MODEL_COST_MAP`, `TIKTOKEN_RS_CACHE_DIR`, `LITELLM_MODE=PRODUCTION`, a per-attempt cache directory, `HOME` and `TMPDIR`. Upstream workers inherit the user's whole environment. | No credential may reach candidate code; a no-egress worker needs the two offline variables or it dies after paying. |
| V5 | Solver calls go to Serverless Inference through a gateway, not to OpenRouter. The gateway drops the `usage` request field, calls the provider with streaming and returns one merged non-streaming body, sends its own `User-Agent`, queues at 16 in flight, owns all retries and never returns 429 or 5xx. Every refusal or failure reaches the worker as 402 or 403 with fixed text, including a provider 400 for an over-long prompt. The string `openrouter/openai/gpt-oss-120b` in `val.json` does not mean OpenRouter served the call. | The Forge key stays on the host; one ledger, one concurrency cap and one record of every call; non-streaming calls can hit an edge timeout; a 429 passed to the worker becomes 6 to 18 fast retries. The worker fails the same way upstream would: an exception that ends the job. |
| V6 | Reply reuse is a per-attempt worker cache plus the gateway's replay store (scope `search` by default), not one persistent directory shared by every run on the machine. Replayed replies carry a zeroed usage block. Replies are not reused across campaigns, and qualification replies are never reused by the measured phases. | A fresh sandbox per job has no shared directory. Scope `search` gives the same reach inside one campaign; zeroed usage keeps `val.json` token counters meaning "tokens generated for this job", as an upstream cache hit does. |
| V7 | A candidate's dataset jobs run one at a time by default (upstream: all three at once); validation jobs may be raised to three at once after qualification, test jobs always run one at a time. The job limit (7,200 s by default) is worker time measured inside the executor and is enforced by ending the worker, in a sandbox by stopping it. Upstream's two timers kill only the parent `uv run`; the worker keeps running, so an upstream "missing" dataset often arrives late and joins a later frontier [r2, measured on a replica]. Here a timeout is permanent. A candidate-attributable failure (a job-limit timeout, a candidate error) gets **one** attempt, not upstream's three. Attempts are counted across restarts, per job and candidate file hash (a re-proposed file gets a new attempt; an operator can reset a job); infrastructure-caused attempts, which include a failed environment setup and a host that slept during the job, are counted separately, up to three per invocation, with a growing wait between them; every failure of a baseline job counts as infrastructure; a job stopped by one of its ceilings is not retried. Upstream gives every `benchmark.py` call three fresh attempts whatever the cause. | The provider limit is about 20 in flight, so parallel jobs lengthen queue waits against the client's fixed 600 s timeout; a job limit that stops nothing is not a limit; a deterministic failure repeats, and with gateway replay a rerun starts where the last attempt stopped, so three attempts would be one job with three times the limit; a resumed search must not pay again without limit, and must not be left with a job that can never run again. |
| V8 | With `SandboxExecutor`, candidates run with egress and DNS denied, with no mounted volume, with only their own dataset present, in a fixed environment: a prebuilt image, or a stock CPython 3.13 image plus a hash-checked wheelhouse of the 62 locked packages installed in each sandbox. Both are pinned by digest, and every sandbox proves its architecture, Python version and package set before the worker starts. A candidate that installs a package, downloads something or reads another dataset fails; upstream would let it. | Isolation of model-written code and of the key. Such failures get their own `failure_class`. The skill has no rule against new packages, so this is a real restriction. Python 3.13 matches the host venv the proposer prototypes in. |
| V9 | The import check runs in the executor with the worker's environment, not on the host with the runner's. Candidate names must fully match `[a-z][a-z0-9_]{0,47}` and candidate files are size-checked; others are rejected before use. A baseline is imported first as a probe; if the probe fails, the failure is treated as infrastructure and no candidate is rejected. A candidate is also rejected when the class upstream's loader would pick from its module (the first `MemorySystem` subclass in alphabetical order) is not defined in that module. | Upstream imports candidate code on the host and interpolates the name into `python -c` code; an import check that cannot tell a cold sandbox from a broken candidate would record infrastructure trouble as an invalid candidate; a candidate that imports a base class whose name sorts first would be evaluated as that other system without anyone noticing. |
| V10 | The proposer's environment is an allow-list plus a closed list set by code. It has no solver endpoint or key in it (upstream's proposer inherits `OPENROUTER_API_KEY` and could call the solver from Bash); `VIRTUAL_ENV`, `PATH` and `PYTHONPATH` (the workspace's parent, which the skill's own import line needs) are set explicitly; `UV_PROJECT_ENVIRONMENT`, `UV_NO_SYNC`, `UV_FROZEN`, `UV_OFFLINE`, `UV_PYTHON_DOWNLOADS=never` and a `uv` cache under the session's scratch directory stop `uv` from changing the environment or using the network; `PYTHONDONTWRITEBYTECODE` is set; auto memory is disabled; the CLI's auto-updater is off and startup failures arrive as result events. | The skill says the proposer does not run benchmarks; an agent session's or a desktop app's ambient variables must not leak in; the Python environment must match the worker's; auto memory is a state channel outside `logs/` that every campaign in the repository would share. Attempted solver use is visible as Bash tool calls in the transcript and as `unknown_job` records. "No solver key" is true of the proposer's environment; in modes `upstream` and `contained` a key in the user's own `~/.netrc` stays readable (last part of this section). |
| V11 | The proposer binary is pinned by path, hash and version. In every mode the command gains `--max-budget-usd` and a generated `--settings` file (auto memory off, no model switch on a content flag, no hooks), and `--model` may be a pinned id instead of the alias. The session runs in its own process group; its deadline is counted on a monotonic clock and stands still during reported rate-limit waits (upstream: the wall clock, so a laptop that slept kills the session on wake); at the deadline it gets SIGINT, then SIGTERM, then SIGKILL (upstream: SIGKILL of the one process at once). Whatever is left of the process group is terminated when the session ends, also after a normal exit (upstream leaves background children running). A timed-out session still yields no candidates, as upstream. | Keeps the `result` event and cleans up Bash children without changing search semantics; proposer spend is real, so it has a cap; the model and effort that the paper names should be the ones that run. |
| V12 | After each proposer session, pinned upstream files that changed in the workspace are restored and the change is recorded; evaluation bundles are built from the pristine tree; files and directories the session added outside `agents/` and `logs/` are moved to quarantine; `evolution_summary.jsonl` is restored if the session changed it; pinned files are verified again before every `benchmark.py` call and every bundle. Upstream would run on with the edited file and keep whatever the session left. | The manifest pins the evaluation code; a search that edits its own evaluator is not the declared experiment; a stray module in the working directory must not be importable by anything we start; an extra summary row would change the iteration count. |
| V13 | The search stops (exit 2, `stopped.json`) on a gateway halt, an exhausted budget (a solver phase, the sandbox meter, the proposer ceiling), a provider configuration fault (401, 402, 403) or exhausted quota, a failed isolation check, exhausted infrastructure attempts or a terminal sandbox account error, a baseline without a usable result on some dataset after Phase 0, a proposer spend limit or login failure, a proposer model or effort other than the one requested, an unavailable or ignored Bash sandbox, a failed integrity check after a session, used-up transient reruns, a changed proposer binary or version, a changed Python environment, and any unexpected exception (recorded, exit 1). A plan-window limit with a reset time is waited out and a transient failure is rerun, both for the same iteration. Upstream prints a failure and continues (L519-L524) or skips the iteration (L476-L480). | A budget ceiling and a halt are the point of the design; an infrastructure failure must not be recorded as a candidate's result; after a limit upstream would burn every remaining iteration in seconds. |
| V14 | The gateway accepts only the request fields upstream sends (`model`, `messages`, `temperature`, `max_tokens`, and `usage`, which it drops), at most 8 messages with only `role` and `content`, bounds `max_tokens` at 16,384, and caps each job: worker requests per attempt, charged provider attempts and dollars across attempts, stream volume, live requests, and refusals in a row. A candidate can fail with `request_ceiling`. | Candidate code can reach the gateway address directly; the bound protects the budget. Upstream-shaped requests are unaffected. |
| V15 | `.launcher/*.log` files are rebuilt in upstream's format with the upstream-equivalent command line and the worker's stdout followed by its stderr (upstream interleaves them); the executor's `out/` and `in/` path prefixes in that output are replaced by the directories upstream named. A Python traceback in the log still shows frames of `mhf_capture_entry.py`. `summary.json` is refreshed by a separate `benchmark.py --results` call. | The driver owns launching; the proposer should see what upstream would have written, not our wrapper command or sandbox paths. The wrapper frames are a visible difference we do not hide. |
| V16 | Before jobs are planned and before every `--frontier` or `--results` call, every `val.json` under the run directory and every `test.json` under its results directory, for any system name and at any depth, is compared with what our job records published: a file nobody published is moved to quarantine, a changed one is replaced from its attempt directory. A result file is published only from an attempt that succeeded with no infrastructure refusal in it. Upstream accepts any `val.json` with a numeric `accuracy` (`benchmark.py` L171-L176) and reads them all with `rglob` (L186). | A planted or stale file must not count; one planted for a name without an agent file tops the frontier and then makes finalisation impossible [A-U X2]. |
| V17 | At most `search.max_candidates_per_iteration` (3) candidates are evaluated per iteration; upstream evaluates everything listed. A malformed `pending_eval.json` counts as "no candidates" (upstream would raise at L482), and so does one whose `hypothesis` is not a string or that lists a name twice (upstream would raise at L488 or evaluate the name twice). | Bounded spend and a forecast that holds. The paper reports two per iteration; the released skill demands three. |
| V18 | `benchmark.py --frontier` and `--results` are started with the example venv's Python and the flag `-P` instead of `uv run python`. | Same interpreter and code; nothing resolves or syncs an environment inside the workspace; `-P` keeps the proposer's working directory off the import path of that child. |
| V19 | When a worker's 600 s client timeout fires, the gateway keeps the provider call, and the worker's retry of the same request joins it instead of starting a new provider request. | The client timeout cannot be changed without an upstream edit; paying twice for one answer is waste, not fidelity. |
| V20 | Extra records exist beside upstream's (sidecar, call records, iteration records with an improvement over the previous best and an all-dataset average that counts a missing dataset as zero). None is placed where the proposer is told to look, but they sit in the state directory two levels above its working directory. In mode `upstream` reading them is not prevented; it is detected from the transcript (tool calls that name a state path) and the iteration is marked `evidence_exposed`. In modes `restricted` and `contained` the generated settings deny those reads. Several W&B runs (search, one propose and one evaluate per candidate) replace the brief's "one W&B run". | The proposer's evidence is meant to be exactly upstream's (decision U9a); in mode `upstream` that is monitored, not enforced. Upstream's `delta` is never positive and its average rewards a crash on a dataset. The EvalTable compare view compares the same key across runs. |
| V21 | A finished proposal is stored; if the search stops during its jobs and is resumed, the stored proposal is replayed instead of running a second session. A proposer failure that upstream counts as a loop turn is not re-run after a restart. The last iteration number is always `search.iterations`. | Upstream would ask for the iteration again and pay for a second, different proposal. |
| V22 | Upstream's signal handlers and its `_interrupted` flag are not used; `--fresh` is never used. Timestamps the worker writes in local time are in the container's zone under `SandboxExecutor`; our own records are UTC. | `_interrupted` writes false `failed` rows for candidates that never ran [X-E4]. Nothing reads the worker's time fields. |
| V23 | Only with `search.partial_candidate_policy: "quarantine"` (not the default): a candidate with any failed dataset has none of its results published, so upstream scores it 0. | Upstream averages the datasets that finished, so a candidate that crashes on its hardest dataset can top the frontier [rep CM-V M4]. The default keeps upstream's rule and reports the candidate as incomplete in our own metrics. |
| V24 | Only on Route 1: the worker gets `--model openai/openai/gpt-oss-120b --api-base ...`, `val.json` records that string, the OpenAI SDK layer is in the path, and `LLM.batch` runs 32 calls at once instead of 4 (with `api_base` set, `llm.py` L324-L325 does not cap `max_workers` at 4 [src]). | Manifest switch (F2). |
| V25 | When the same request has used up the gateway's attempts in two attempts of a job while the provider was serving other requests, the worker is told the provider rejected it, and the attempt counts against the candidate. Upstream would see a provider error each time and retry the job three times in all. | A provider fault that is specific to one prompt must not stop the campaign at every restart. In a long brown-out a candidate can be charged an attempt it did not deserve; such calls are marked `repeatable_provider_failure` and are counted in the article. |
| V26 | A test job runs the candidate bytes that were validated on that dataset and loads the `memory.json` that validation produced, both taken from this build's stored copies after a hash check. Upstream takes whatever is in `agents/` and in the run directory at test time. | A test number must belong to the code and memory state that were validated; every later proposer session can write both places. A helper module that is not itself a proposed candidate is not pinned. |
| V27 | The calibration run (`evaluate --calibration`) evaluates the calibration systems on the test split with upstream's own test job arguments, but outside upstream's finalisation: no `finalized.json`, nothing published under the results directory. It judges two gates: G1 (hard), the zero-shot baseline against bands around the paper's and an independent re-run's figures; G2 (soft), the released few-shot baseline against the independent re-run. The noise run (`evaluate --noise`) repeats the baselines' validation jobs with no reply shared between repeats. Upstream has neither step; its only test path finalises the run. | The protocol needs a check of routing, effort, truncation and parsing before the search spends; zero-shot is the only baseline whose configuration is the same in the paper and in the release (the paper's few-shot baseline is a different one, so it is not a gate) [r2]; upstream's finalisation, once complete, refuses every later search under the run name (L395-L402) [A-U E6]. |
| V28 | The finalisation (`evaluate --test`) is allowed only after the search is complete or explicitly ended, and after it has started the search cannot run again. Upstream allows a search while `finalized.json` says `in_progress`. | While a finalisation is unfinished, test results sit in the directory the proposer's task prompt names. |
| V29 | A halt ends the running job within about a minute (the worker is killed after a 60 s grace); upstream has no halt. A worker that keeps calling after it was refused is killed after 2,000 refusals. | A job must not run to its 7200 s limit with every call refused. |
| V30 | A proposer session counts as successful only with a `result` event whose `is_error` is false. Upstream accepts exit code 0 and an existing `pending_eval.json` (L183-L189). | In stream-json mode the CLI exits 0 when it wrote no result at all, and after SIGINT; a cancelled or cut-off session that had already written the file would be evaluated [r2]. |
| V31 | The model and the effort that ran are compared with the request on `init`, on every main-thread reply and on fallback events, and the settings file forbids switching model on a content flag; a mismatch stops the campaign. Subagent models are recorded only. Upstream checks nothing. | A blocked or flagged model is substituted without the session failing, and an organisation's effort cap applies silently; the datasets are medical and chemical, where a content flag is plausible [r2]. |
| V32 | Permission modes `restricted` and `contained` (the user's choice; mode `upstream` is upstream's command plus V11). In `restricted`: no bypass flag and no `--allowedTools`; permission mode `dontAsk`; Bash runs only inside the CLI's sandbox, with no network; file writes are limited to **new** files under `agents/`, to `pending_eval.json` and to a per-session scratch directory that stands in for `/tmp`; existing agent files, every pinned file and the reports directory are read-only, so the skill's Step 0 (writing missing post-eval reports) cannot run; some command forms (heredocs, `timeout`, `rm` and others) are refused; a short addendum to the system prompt says all this (its text is in 3.19, its hash is recorded per session, the skill file is unchanged). In `contained`: upstream's command with the same sandbox and deny rules; it is not fail-closed. In both, every Bash tool result must be marked `sandboxed`, or the campaign stops. | Model-written Bash runs on the user's machine beside the user's credentials. How far to restrict it is the user's decision; what the restriction changes about the proposer's working conditions is stated here so the article can say it. |
| V33 | Proposer spend is treated as real and is capped: every session runs under `--max-budget-usd`, and the driver keeps a cumulative ceiling from each session's reported cost (for a session without a result: the transcript's cost record, else its usage at list prices; never zero). Upstream's comment assumes subscription auth and has no cap. | The login is a usage-billed seat, billed per token against a monthly limit [r2]. |
| V34 | The search runs in resumable blocks over several days (`--max-iterations-this-run`); a stop request is honoured at an iteration boundary only; the dates, the CLI version and the resolved model id are recorded per session, and one W&B search run is resumed across blocks with the iteration as its x axis. Upstream runs in one go, and its stop flag acts in the middle of an iteration. | Twenty iterations are 14 to 43 hours at the planning figures [r2]; a stop in the middle of the benchmarks writes `failed` rows for candidates that never ran. |
| V35 | What leaves the machine is governed per dataset. By default only identifiers and hashes do: prompts, replies, reasoning and tool results as SHA-256 and length; table rows with `question_sha256`, labels for Symptom2Disease and LawBench, hashes of prediction and target for USPTO. LawBench text is never published; USPTO text only after clearance. Candidate files are published only when they quote no dataset text; `memory.json`, `log.jsonl`, reply caches and transcripts never are. Run metadata carries no host name, user name, local path or account fact. Upstream publishes nothing. | The datasets' licences (Appendix G); a reader who holds the upstream data can still reproduce every join from `question_sha256`. |

Mirrored on purpose, so not deviations: `run_benchmark` returns code 0 for a system that is not on disk; `evolution_summary.jsonl` `delta` is never positive; upstream's averages cover only datasets that produced a result (default policy); an empty reply with `finish_reason: length` is scored wrong and cached; a re-proposed name with an existing usable result is not re-evaluated (flagged `name_collision`); a timed-out proposer session yields no candidates; a 7200 s job limit; `temperature 0.0`, seed 42, offline mode, one epoch, `max_tokens` 16,384, system message `Reasoning: medium`; the stored Claude login for the proposer, with no API key in its environment.

Differences from the paper that are not this build's doing, to be stated in the article (fidelity table A of Appendix G lists them all): a 131,072-token window, where the release's 16,384 output budget on the README's 32,768-token server leaves 16,384 prompt tokens and the paper states neither; a different engine build with multi-replica routing; effort "medium on both" without a claim of identical prompt rendering; one epoch (`config.yaml` L15 says "was 5"); the released skill demands three candidates per iteration where the paper reports two; the `opus` alias resolves to today's model, not the paper's Opus 4.6 (recorded per session); upstream's default traces hold no per-example predictions; one Symptom2Disease question appears in both the validation and the test split [F-E2].

What is enforced about the keys, and what is not (critic gap 8).

- Enforced: no key is ever on a command line or in a file this package writes. With `--key-source prompt` (the default at a terminal) or `fd:N`, a key is never in any process's start-up environment either: it is read into memory, handed to the one component that needs it and passed explicitly on every SDK call. With `--key-source env`, each key is read once from the runner's environment by its component, overwritten in place in the runner's environment block and removed from it. No key is passed to a child in its environment: the exporter child gets the export key over an inherited pipe. No SDK is ever left to find a credential by itself: `wandb.login` is never called, `NETRC` points at an empty file inside the state directory in every live process and in the exporter child, the sandbox SDK gets explicit headers (auth mode `headers`), and a missing key refuses the command. With `SandboxExecutor` no gateway listener exists on the host; no listener exists while the proposer runs, and the proposer's process group is terminated when its session ends. Every record, plan and bundle is scanned by pattern, by the exporter's key value and by the digests of all three keys; what is exported is also scanned for local paths and the host name.
- Stored about a key: its variable name, its length and its SHA-256, in `STATE/keydigests.json` (mode 0600), for the scanner. F14 says the key is never written to the state directory; a digest is, the key is not. If the user reads F14 more strictly, the file can be dropped at the cost of the scanner no longer finding a bare key.
- Not enforced: with `--key-source env`, where the in-place overwrite is unavailable (`status` shows `key_scrubbed: false`), a process of the same user can read the key from the runner's start-up environment with `ps` for the life of the runner; and a key that was typed as `export NAME=<key>` is in the shell history whatever this package does. In auth mode `wandb_auth` the sandbox credential is set as `WANDB_API_KEY` inside the runner process (not in its start-up environment). In modes `upstream` and `contained` the proposer runs as the same OS user: in `upstream` with unrestricted Bash, in `contained` restricted only while its settings file is honoured. It can then read `~/.netrc` and other local files; a key there is valid for Serverless Inference, so "the proposer has no solver key" describes its environment, not the machine, and a proposer that called the model with it would spend outside the ledger (the transcript scan and the usage reconciliation of 8.2 are the checks). It can also read and write this campaign's state directory, including the ledger. With `LocalRelayExecutor` (development only) the relay stub listens on the host's loopback interface, behind a per-attempt bearer on Route 2 and identified by its port alone on Route 1.

---

## 8. Deferred

### 8.1 Waiting for a user decision

Each is a manifest gate; the build is complete without the answer and refuses to run live until it is given.

| Decision | Gate | Unblocked by |
|---|---|---|
| U1, U5 live spend and caps | `decisions.live_spend_approved`, `budgets.*` | the user sets it true and confirms the caps |
| U3 key provisioning | `decisions.key_provisioning`; the `--key-source` flag; `provider.api_key_env`, `telemetry.api_key_env`, `sandbox.auth_env` (three different names, used by `--key-source env` and in records) | the user confirms a dedicated, revocable key and chooses how to give it: at a prompt (the default), over an inherited descriptor, or in the environment |
| whether a key digest may be stored (the reading of F14 in section 7) | none; `STATE/keydigests.json` is written by default | the user's word; without the file the scanner finds keys by pattern and, in the exporter, by value only |
| U4 entity and two projects | `decisions.entity`, `decisions.project`, `campaign_kind` | the user names a scratch project for the preflight campaign and a fresh project, used only by this run, for the measured campaign; both are created PRIVATE by `account-check --create-project` on the user's word |
| U6 proposer permission mode | `decisions.proposer_permission_mode` | the user picks `restricted`, `contained` or `upstream` after reading what each enforces (3.19, V32); PF9 shows whether the pick works, and the fall-back order is `restricted`, `contained`, `upstream` |
| proposer model and CLI version | `proposer.model`, `proposer.cli_path`, `proposer.cli_sha256`, `proposer.cli_version` | decided **before** PF0: the binary hash, the transcript version gate and the findings the restricted mode rests on all belong to one binary. The model (the alias, or a pinned id such as the paper's Opus 4.6) is confirmed by PF9 part 6 |
| U7 proposer spend | `proposer.max_budget_usd_per_session`, `budgets.proposer_list_usd`, `proposer.session_estimate_usd` (all null in the draft; each refuses `search`), `proposer.list_prices`; the headroom and reserve figures of PF2 | the user approves real spend on the Claude seat: a per-session cap before PF9; a provisional campaign ceiling and session estimate in the preflight manifest (the second round suggests $300 for Opus 5.5 or $400 for Opus 4.6, and $2 to $18 per session), which PF7's forecast and PF10's `search` need; the measured campaign's ceiling after PF10 (measured cost x 30 x 1.25); and a reserve for their own work |
| one all-in ceiling | none (the sum of `budgets.solver_usd.total`, `budgets.sandbox_usd`, the ingestion at the plan's price, and `budgets.proposer_list_usd`) | the user states one figure for the whole run; the caps are then set under it. "As much budget as needed" is not a cap and raises none |
| U8 CES EvalTables | `decisions.evaltables_backend` | the user picks `ces`, `wandb_table` or `off`, before PF8 (which needs it) |
| U11 publishing | `decisions.publish_approved` | the user's word, per publish run |
| dataset text that leaves the machine (critic gap 15) | `decisions.dataset_text_publication` (gate for `export` and `publish`), `decisions.uspto_text_cleared`, `telemetry.content.<dataset>.*`; all amendable after the freeze | the user signs the per-dataset rule: `identifiers_only`, or `per_dataset` with Symptom2Disease opened; USPTO opens only after Legal answers; LawBench never opens (Appendix G) |
| what counts as reproduced (critic gap 10) | `protocol.md`, signed in two stages (the offline gate checks stage 1, `search` in a measured campaign checks stage 2); `calibration.gates`, `calibration.gate_search`, `calibration.noise_repeats` | stage 1 before the first paid call: the claim, the claims not made, the selection rule, gates G0 to G2 as numbers in the manifest, the noise plan, the control. Stage 2 after PF7 and PF10: the job limit, the parallelism, the proposer model |
| how a sandbox gets its packages (critic gap 9) | `sandbox.env_mode`; `sandbox.base_image` and `sandbox.wheelhouse.*` (mode `wheelhouse`), or `sandbox.image` (mode `image`) | the user agrees to download one public image and 62 public wheels (mode `wheelhouse`, the draft's default and the preflight's path), or picks a registry and has an image built with `docker buildx --platform linux/amd64`, its manifest architecture checked and its amd64 manifest digest written to the manifest (mode `image`: no per-sandbox setup, better for the full run) |
| the job limit after PF7 | `executor.job_timeout_s` (protocol stage 2) | if learning-heavy candidates cannot finish in 7,200 s: keep the limit and report that they fail, or 14,400 s as a stated deviation |
| what to do if both relay modes fail in PF6 | none | build the mailbox ingress (8.3), or put the key in the remote container (the options this design rejected); the user's call |
| the matched-budget controls | none; the ledger phase `controls` and its cap exist | parallel sampling with K = 5 is recommended and its runner deferred (8.3); a Best-of-N arm doubles the proposer bill and is run only if the article wants that claim |
| licence of the ported files (U10) | none | Apache-2.0 kept with headers and the table; only the copyright holder can change it |
| messages to other teams (U12) | none | nothing is sent by this package |

### 8.2 Waiting for the live preflight

The preflight has twelve steps, PF0 to PF11, then a reconciliation gate (PF-R) and a teardown list. It runs in a **preflight campaign**: a manifest with `campaign_kind: "preflight"` and a scratch project. The measured campaign is a second manifest with a fresh project; it imports the result (3.22) and repeats only the checks marked "per project".

- PF1 to PF4 run through `python -m mhforge account-check --live [--create-project]`, with an unfrozen manifest. They write `STATE/account/account.json`.
- PF5 to PF9 run through `python -m mhforge preflight --live`, in this order; each check writes `STATE/qualification/attempt-<n>/<name>.json`. A failed required check stops the preflight; `passed.json` is written when every required check through PF9 has passed.
- PF10 and PF11 use the ordinary commands in the preflight campaign (`search --live --max-iterations-this-run 1`, `export --live`, `evaluate --test --end-search --live`) and are then judged from the records by the check `rehearsal_review`.
- Every step that starts `claude` (PF2, PF9, PF10, PF11), and later the search itself, is started from the user's own terminal, never from inside an agent session (2.3). No step is run with ad-hoc scripts outside the gateway and the ledger: the code that spends is the code that passed offline.
- Cost [r2]: PF0 to PF3 are free and create nothing. PF4 is the first remote write. PF5 to PF8 spend under $1 of Forge money in all (inference under $0.50 against the qualification, baseline and calibration caps; sandboxes under $1; ingestion under 2 MB against the 5 MB preflight bucket). PF9 and PF10 spend Claude money on the user's seat ($4 to $36 expected; each session is capped by `--max-budget-usd`). PF11 spends about $0.50 more of Forge money.

| Step | Check | What it does | Settles | Stop if | Per project |
|---|---|---|---|---|---|
| PF0 | `offline` (not live) | The offline gate of 3.22: a passing offline smoke for this exact engine, the manifest frozen, protocol stage 1 signed, the CLI hash, the wheelhouse tar and the hashed requirements file. The converter's fixture tests and the generated-settings tests are part of the suite the smoke receipt stands for. Two manual items the user ticks before going on: the offline amd64 container run of the real job bundle (8.3), and a 30-minute stub run under `caffeinate -is` that shows no sleep in `pmset -g log`. | the code that spends is the code that passed offline; the wheels install and the worker runs on Linux x86_64 with no network; sleep prevention works | any test fails; a hash differs | yes |
| PF1 | `account_anonymous` | No credentials: `POST https://api.wandb.ai/graphql` with `{ serverInfo { features { name isEnabled } } }`; fetch `https://coreweave.com/forge-pricing` and `/forge-pricing/inference-agents` into `STATE/account/`. | the server feature `EVAL_TABLES_CES` is on; the price snapshot the ledger uses | the feature is off (choose backend `wandb_table`); the gpt-oss-120b price differs from `prices.*` (fix the manifest before freezing) | no |
| PF2 | `account_claude` | From the proposer environment of 3.5: `<proposer.cli_path> auth status` (prints the auth method as JSON, exits 1 when not logged in, starts no session). While `proposer.cli_path` is null, or when `account-check` itself was started inside a Claude Code session, the step is recorded as `skipped`; PF9 requires it passed. The user opens one interactive session **outside the repository** (so it creates no memory directory and no transcript in a project the proposer's checks watch), reads `/usage`, asks an admin whether a pooled group budget applies, and passes `--claude-spent-usd`, `--claude-limit-usd` and `--claude-reserve-usd`. Passes when logged in and `limit - spent >= budgets.proposer_list_usd + reserve`; while that budget is still null the figures are recorded and the check is repeated before PF9. | the stored login is usable from the clean environment; the billing regime (a usage-billed seat with a per-user monthly limit [r2]); headroom that leaves a reserve for the user's other work | not logged in (the user runs `claude auth login` once); headroom below the proposer ceiling plus the reserve | no |
| PF3 | `account_forge` | With the dedicated key, reads only: `wandb.Api(api_key=...).default_entity` (not `.viewer`, whose query also selects API-key names); the entity's organisation, whether the entity is a team, its member count and default project visibility; the organisation's feature flags `inference-catalog`, `sandboxes`, `eval_tables_ces`; subscription type, privileges and usage when the role may read them (unreadable is recorded, not a failure); `project(name:, entityName:)`; `SandboxClient.list_ids` with the explicit auth of the configured mode (expect an empty list); then the project query again (a non-null answer now would show that a read created it). | the key is valid; the entity is a team inside an organisation; the three switches; plan, allowances and caps; the project name is free; the sandbox API accepts the key and the hand-built headers (else switch `sandbox.auth_mode` to `wandb_auth`) | the viewer is null; the entity is personal or the organisation-level entity; a switch is false (organisation admin or support); the plan is Free, Academic or a trial (`USER_LED_TRIAL` and `MANUAL_TRIAL` are their own case: stop and ask) with an inference allowance below the solver cap and pay-as-you-go inactive; the project exists and is not ours; the sandbox list raises `PERMISSION_DENIED` (a misspelt team name returns the same 403 as a disabled organisation) | yes |
| PF4 | `account_project` (`--create-project`) | Look the project up; when it does not exist send `upsertModel` with `access: "PRIVATE"`; read back `access`, `readOnly` and `weavePermissions.canWrite`. Or the user creates it in the UI and the step only verifies. No Tags are defined on the project. | one project with known visibility exists before any surface can create it implicitly (inference would create it with the team's default access, the trace endpoint with none [r2]); a missing project returns 401 on inference [rep] | access reads back as `USER_READ` or `USER_WRITE`; we cannot write | yes |
| PF5 | `inference` | `GET https://api.inference.wandb.ai/v1/models` with the key, `OpenAI-Project` and the explicit `User-Agent`. One completion through the gateway with a one-line prompt in the worker's exact request shape, raw stream saved. One with `gateway.max_messages` one-byte messages (`prompt_tokens` must be at most `input_limit`). The same request twice with `no_replay=True`, reply hashes compared (informational). `audit` is clean. No check calls `ProviderClient.complete` directly: that would be a forward without a reservation. | key, project, switch and quota are accepted; the echoed model id (then set `provider.expected_response_model` in the next manifest); the usage keys; the reasoning field name; `finish_reason`; whether a complete stream ends with `[DONE]`; the User-Agent is not blocked; `ForgeProviderClient` verified | 401, 402 or 403; a 429 whose message says the quota was exceeded; empty content; no usage block | yes (the first completion only) |
| PF6 | `sandbox` | One 2-CPU, 4-GiB sandbox from `sandbox.base_image` (the fallback reference once if the pull fails), egress and DNS denied, `WANDB_API_KEY=""`, explicit entity and project, `max_lifetime_seconds=3600` (the work takes about 25 minutes and the upload rate is an estimate), run tags, no mount. Inside, in order: (1) facts: `uname -m`, `python -V`, `pip --version`, the glibc version, the image reference the platform stored; (2) isolation: `mhf_selfcheck.py --probe-egress` shows no credential-shaped variable, `wandb_api_key` not `set`, the listing of `/var/run/secrets/sandbox`, both gating probes `blocked`, and the recorded literal-address probes (metadata address, resolver on port 53, cluster service address, one IPv6 literal, the object-store endpoint when known); (3) environment: the wheelhouse path of 3.17, timed (upload, size check, checksums, install), the six packages import, the harmony encoding loads with the two offline variables, the freeze hash is recorded; (4) relay: a job file whose worker is an echo client, 32 interleaved 100 KB multi-byte messages for ten minutes, one bridge rotation, one killed bridge, ten idle minutes and then one request, one reply sent after the bridge exec was killed from outside without the host side closing (the missed pongs must trigger a reattach), the daemon still alive after its starting exec ended, an exec timeout honoured, a write into a dead stream returns within `relay.pong_timeout_s`, a detached worker that leaves a result file, zero lost or duplicated replies; (5) stop it, confirm the terminal state, list by tag until two empty lists. **Two more sandboxes.** `lifetime`: 1 CPU, `max_lifetime_seconds=120`, left alone; it must read terminal within five minutes. `control`: one sandbox **without** the empty `WANDB_API_KEY`, for the record; without it a clean result cannot tell "suppression works" from "nothing is injected under that name". Before a long measured search the relay soak is run once more for two hours (about $0.70). | sandboxes are enabled for the organisation; token suppression; deny at create is accepted and effective, DNS deny included; a digest reference on `public.ecr.aws` is accepted; the architecture; the wheelhouse path end to end with real times (then `sandbox.setup_allowance_s`); compiled wheels import on the real guest; `relay.mode` (`daemon`, else `stdio`); **the platform ends a sandbox at its lifetime**, which is what the sandbox cost cap rests on; `CwSandboxClient` verified | create is refused (`PERMISSION_DENIED`: not enabled; `RESOURCE_EXHAUSTED` with no reason: billing, terminal); not `x86_64`; a credential-shaped variable is visible; a gating probe connects; a hash differs; a relay reply is lost or duplicated in both modes (then the mailbox ingress of 8.3 or the user's decision of 8.1); the 120 s sandbox is still running after five minutes | yes (one sandbox with the selfcheck only) |
| PF7 | `qualification`, then `calibration_gate` | In this order. `qualification_job`: `qualification.system` on `qualification.dataset` (30 calls) in the configured executor, ledger phase `qualification`; `qualification.checks`. `baselines`: `Driver.run_baselines()` in this campaign (the six baseline validation jobs, 260 calls; they are repeat 1 of the noise run and the validation pass the gates need). `learning_probe`: 12 sequential calls with a learning-shaped prompt (a lesson-summary request over one training example) through the gateway. `long_reply`: one request that asks for a reply at the token cap. `forecast`. Then, **directly after qualification and before any proposer spend**, `calibration_gate`: `Driver.run_calibration()`, which records G0 and judges G1 (hard) and G2 (soft) (3.20). The gate is the cheap check that routing, effort, truncation and parsing are right; running it after the proposer steps would spend Claude money against a solver configuration nobody has gated [r2]. | output tokens per call; decode speed and time to first token by prompt size, including the 24,000-token LawBench few-shot case; queue wait against the client's 600 s; whether 16 in flight draws a 429 (the message names the bucket; lower `gateway.max_in_flight` in the next manifest; no deliberate ramp is needed); the second validation pass is free; sandbox overhead per job; the sidecar join on live data; learning-call latency, which decides the job limit (if `203 x median x 1.25 > 7,200 s`, the user chooses: keep 7,200 s and report that learning-heavy candidates fail, or 14,400 s as a stated deviation); whether validation jobs may run three at once; the provider's behaviour on a 380 s reply; `provider.*_timeout_s`; the cost and wall-clock forecasts; the test path (`memory.json` upload, `--load-memory`) | the forecast times 1.25 does not fit the caps; the truncated-empty rate is above 5%; LawBench few-shot is near zero while Symptom2Disease is normal (a context or encoding fault); G1 fails; the 20-iteration wall-clock figure is more than the user will give. Stop and change the plan; do not shrink the design mid-run | no |
| PF8 | `telemetry` | Needs `decisions.dataset_text_publication` and `decisions.evaltables_backend`. Export the qualification conversations with every dataset on rule `hash` (with the explicit trace `base_url`); read back the exact span count with `include_details: true` and compare the stored `raw_span_dump` lengths with `planned_bytes` (set `telemetry.agent_lens.byte_estimate_factor` in the next manifest when they differ by more than 10%). One 3-row CES EvalTable of identifiers and hashes from two scratch runs; the summary marker appears; a second row logged to a resumed run with `define_metric` and no explicit step, then an earlier iteration's row after it (it must not be dropped). One small Artifact logged, used and fetched. The key passed through `wandb.Settings(api_key=...)` is accepted. **Run hygiene**: the stored run's metadata is read back and must hold no host name, no user name, no program path, no git state and no requirements file (the settings of 3.25). Size and mtime of `~/.netrc` (`os.stat` only; the file is never opened) are the same before and after, and nothing was created outside `STATE`. A person opens the compare panel. | Agent Lens delivery and delay; the byte estimator against stored spans; CES accepts the project; the organisation's ramp shows the panels (if it is off, writes succeed and nothing renders); the Artifact path; both telemetry adapters verified; the setting names of 3.25 | the span count differs; CES refuses the write (fall back to `wandb_table`); the run metadata holds a host name, a user name or a local path | yes (the 3-row table only) |
| PF9 | `proposer_probes` | From the user's own terminal, in a throwaway workspace, with a small model, low effort and `--max-budget-usd 0.50` per session; needs the permission mode, the CLI version and `proposer.max_budget_usd_per_session`. (1) `env`, no spend: in the workspace with the proposer environment, `uv run python -c "print(1)"` works without touching the venv (fingerprint unchanged), and so does the skill's import line. (2) `allowed_and_denied`: one session that runs the status-only probe script (3.22) and a few tool calls. Must work: a new file under `agents/`; `pending_eval.json`; a file under the scratch directory; the skill's import line; reading back a spilled tool result; one subagent Bash call. Must be refused: an edit of `benchmark.py`; each status-only credential read; the Read tool on the canary file; `touch ~/mhf-probe`; `curl` to a public host and to a loopback port; the keychain query; a write to `/tmp`; a status-only read of an older transcript under `~/.claude/projects` and of another session's scratch under `/private/tmp/claude-<uid>/`. Recorded only: a heredoc, `timeout 60 uv run ...`, `rm <scratch file>`, `cd .. && uv run ...`, a `PYTHONPATH=..` prefix. (3) `broken_settings`: the same session with a deliberately invalid settings file. (4) `signals`: a prompt that runs `sleep 300`; SIGINT, and in a second session SIGTERM; the stdout tail, the exit code, surviving children. (5) `budget_flag`: one session with a cap of a few cents and a multi-turn prompt; expect `subtype: error_max_budget_usd`, `costBasis` `list`, no unknown-model cost. (6) `model_effort`: one trivial session with the intended model and effort. (7) `ps_canary`: with a canary key given through the chosen key source, `ps eww -p <runner pid>` shows 0 matches while a probe session runs. Then read back: `system/init` (model, plugins, skills, MCP servers, `memory_paths`, effort, version), `result.permission_denials`, `sandboxed` on every Bash tool result, the transcript's location and record types for an `sdk-cli` session, the `cost-state` record, the tree hash, the names (never the values) of the variables a Bash call sees. Cleanup: `~/mhf-probe`, the scratch files, the probe agent file and the throwaway workspace are removed. In mode `upstream` only parts 1, 4, 5, 6, 7 and the read-back apply. | the permission mode as a combination; whether a rejected settings file leaves the session unable to write or to run anything but read-only commands; whether the keychain is readable from sandboxed Bash; whether a `result` event follows SIGINT; whether `--max-budget-usd` is enforced on this login (PF10 relies on it as its hard bound); whether the intended model and effort are granted; the live stream and transcript shapes; the `uv` assumptions | a write outside the allowed set succeeds; a credential read succeeds; a Bash command runs with `sandboxed` false; the broken settings file yields a session that can write; the budget cap is not enforced. Then fall back one mode (`restricted`, `contained`, `upstream`: the user's decision) or stop | no |
| PF10 | `proposer_session` | One real session: iteration 1 of `search --live --max-iterations-this-run 1` in the preflight campaign, with the intended model, `--effort max`, the real task prompt and `--max-budget-usd` at the per-session cap. The session checks of 3.19 assert the model on `init` and on every main-thread reply and the effort from the transcript. Recorded: duration, turns, `result.modelUsage`, every `rate_limit_event`. The converter runs on the real transcript and the proposer unit's Agent Lens bytes are planned. | whether the organisation restricts the model or caps the effort; session length against 2,400 s; proposer cost per iteration (`proposer.session_estimate_usd`, and `budgets.proposer_list_usd` as measured cost x 30 x 1.25, for the measured manifest); the converter on a real `sdk-cli` transcript; the proposer ingestion ceiling | the model or the effort differs from the request; measured cost x 30 x 1.25 exceeds the ceiling the user approved; the session reaches 2,400 s; the converter fails | no |
| PF11 | `rehearsal`, judged by `rehearsal_review` | The rest of that iteration: the import check in a sandbox, nine jobs, the frontier, the summary, `export --live`. The driver is killed with SIGKILL once in the middle of a job and started again. Then **the finalisation is rehearsed**: `evaluate --test --end-search --live` in the preflight campaign, which runs upstream's `--frontier --test` and `--results --test` paths and its lock for the first time before the end of the measured search, where a fault would cost the most. `preflight --live --only rehearsal_review` then asserts from the records: `audit` is clean and no call was paid twice after the resume; the proposal was replayed, not run again; **no sandbox with the campaign tag is left running** after the SIGKILL resume (two empty lists); `finalized.json` says `complete`; every receipt is `verified`. A learning-heavy candidate can run to the job limit, and the preflight caps then halt the rehearsal: that is a result, not a fault. | the whole loop on live services; a resume pays for nothing twice; proposal replay; the journal against the ledger; finalisation | any call is paid twice after the resume; the audit is not clean; a tagged sandbox is left | no |
| PF-R | `reconciliation` | `preflight --live --only reconciliation --platform-inference-usd X --platform-sandbox-usd Y`: the user reads the platform's own usage figures for the preflight project and the check compares them with the ledger's observed solver spend and the sandbox meter. It passes when each pair differs by at most `accounting.reconcile_tolerance` or by at most $0.05. `--waive-reconciliation NOTE` records `waived` when the user's role cannot read the figure. The user also reads the Claude seat's spend and compares it with the proposer meter. | the price constants; whether an interrupted call is billed; that nothing spent outside the ledger (in modes `upstream` and `contained` a proposer could have used a key from `~/.netrc`) | the figures differ by more than the tolerance: find out why before any measured spend | no |

**Teardown** (`preflight --teardown` prints it; `--live` runs the first item):

1. Stop every sandbox with the campaign tag and list by tag until two empty lists (automated).
2. Revoke the dedicated key if the measured run will use another one.
3. Delete the scratch project, on the user's word. Agent Lens spans cannot be removed one by one, and whether deleting the project removes them is unknown [r2]; that is why the preflight has a project of its own.
4. Remove the pulled base image and the wheelhouse if they are no longer needed.
5. Remove a CLI binary that was installed only for the run.
6. Remove the proposer scratch root (`proposer.scratch_root`) and the throwaway probe workspace.
7. With `--key-source env`: unset the key variables in the shell that held them.

To abort at any point: `halt --manifest M --note ...`, then `status`, then this list.

**After the preflight: the measured campaign.** A new manifest with `campaign_kind: "measured"`, a fresh project, `qualification.import_from` set to the preflight campaign, and the values the preflight measured (`gateway.max_in_flight`, the timeouts, the job limit, the parallelism, the budgets, the proposer model, its per-session cap and estimate, `provider.expected_response_model`, `telemetry.agent_lens.byte_estimate_factor`). Then: `account-check --live --create-project` on the fresh project; freeze; `preflight --live` (the per-project checks only; it writes this campaign's `passed.json`); protocol stage 2 signed; the noise run (`evaluate --noise`, in the preflight campaign, any time before the search); `evaluate --baselines`; `search` in blocks of four or five iterations, with the ledger reconciled and the Claude spend read at each block boundary against a written stop threshold; `evaluate --test`; `export`; `publish`.

**The old check numbers.** The first revision numbered the preflight checks P0 to P15. They map to the steps above as follows; no other part of this spec uses the old numbers.

| Old | Old name | Now |
|---|---|---|
| P0 | `offline` | PF0 |
| P1 | `project` | PF4 (`account_project`: look up, create PRIVATE, read back) |
| P2 | `inference_one` | PF5 |
| P3 | `inference_repeat` | PF5 (the repeat pair) |
| P4 | `inference_concurrency` | dropped: PF7 runs 16 in flight and a 429 names the bucket, so no deliberate ramp is needed; `Gateway(overrides=...)` and `Context.gateway_factory` are gone with it |
| P5 | `sandbox_list` | PF3 (the sandbox list among the account reads) |
| P6 | `sandbox_selfcheck` | PF6 (isolation part, with the control sandbox and the lifetime sandbox) |
| P7 | `sandbox_image` | PF6 (facts and environment parts) |
| P8 | `sandbox_relay` | PF6 (relay part; the long soak before a long search) |
| P9 | `qualification_job` | PF7 |
| P10 | `proposer_env` | PF9 part 1 |
| P11 | `proposer_smoke` | PF9 parts 4 to 6 and PF10 |
| P12 | `proposer_restricted` | PF9 parts 2 and 3 |
| P13 | `lens_roundtrip` | PF8 |
| P14 | `evaltable_roundtrip` | PF8 |
| P15 | `artifact_roundtrip` | PF8 |

Until PF6 passes, `SandboxExecutor` and `CwSandboxClient` are UNVERIFIED. Until PF8 passes, the two telemetry adapters are UNVERIFIED. Until PF9 and PF10 pass, the proposer's permission modes, the stream and transcript shapes and the limit classifier are UNVERIFIED. Manual checks beside the preflight (a person looks): the compare panel and URL cell rendering; the signed-out view of a public project; the monthly billing export for the run's project.

### 8.3 Deferred work, with the reason and the trigger

| Item | Reason | Trigger |
|---|---|---|
| Whitespace heartbeat in the ingress and the stub (keeps the worker's client alive past 600 s; a refusal after the heartbeat started would reach the worker as HTTP 200 with an error body) | Confirmed offline for the pinned client stack [X-E1, X-E2], but the committed-200 refusal departs from F4's "402 or 403", and coalescing already makes client retries free | PF7 shows calls or queue waits near 600 s causing many client retries (visible as `coalesced` records); it matters more once validation jobs run three at once |
| Mailbox ingress over the file API (critic gap 1) | F3 fixes two ingresses; no offline evidence favours it. The core takes `(job, request bytes)` from any ingress and `RelaySession` takes any `FrameChannel`, so it is one new channel class | the relay part of PF6 fails for both relay modes |
| Raising a cap after a budget halt (a recorded amendment) | A predeclared ceiling that can be raised mid-run is the user's call (F16); today a budget halt ends the campaign and is reported as the result (and can be exported and published as such) | the first budget halt in a long run, on the user's word |
| Pinning helper modules that candidates import (V26) | Only proposed candidate files have a stored reference copy; a helper file the proposer adds beside them is bundled as it is at the time | a candidate that imports a non-candidate module is observed |
| Telling a slow provider from a slow candidate at a job timeout | A timeout with no refusal issued is attributed to the candidate, as upstream's 7200 s limit is; the gateway's per-job retry counts are recorded for a later rule | PF7 or the search shows timeouts that coincide with provider retries |
| A network guard for worker children on every platform | The smoke uses `sandbox-exec` on macOS when it works and records when it does not; inside an outer sandbox it does not | a platform where the smoke runs without a guard and that matters to the user |
| Waiting on a proposer limit that has no structured reset time | a plan window with a `resetsAt` is waited out (3.19); a limit known only from text is not, because nothing says when it lifts; stopping is safe | the first observed limit stop whose raw events show a reliable reset field the classifier does not yet read |
| The matched-budget control: parallel sampling (the released `fewshot_all` with K = 5 samples per test example at a non-zero temperature, majority vote; 2,060 calls, about $1.20) | **specified, not built.** What exists for it: the job phase and ledger phase `controls` with its cap, the sample index in the job identity and job file, the reuse salt, the per-attempt worker cache. What is missing is the runner: a way to change the worker's temperature without an upstream edit, and the vote | the user asks for the control after the measured search; the article must then say that the vote is this build's adaptation and that K = 5 is more generous than the selected harness's calls per prediction |
| A Best-of-N arm (N independent proposer sessions that see only the seed baselines and their scores) | the paper's own matched control; the release has no such mode; it needs a second workspace and as many proposer sessions as the main arm | a user request, and approval of a doubled proposer bill |
| Sandbox environment modes `snapshot` and `install_then_deny` | named in the manifest schema and rejected by validation. A snapshot mount gives the sandbox an object-store egress rule under every network posture, and snapshot storage is billed separately with no public price; install-then-deny needs the network open at start [r2] | the user declines both a public base image and a prebuilt image |
| The offline amd64 container run of the real job bundle (Route 2: `mhf_capture_entry.py`, the relay stub and the selfcheck inside a stock container with `--network none`, the host relay attached over `docker exec -i`, the base image pinned by its amd64 manifest digest, the request count asserted) | it needs Docker and a public image pull, so it is a documented manual step of PF0, not a test; the research script that exists covers Route 1 only and prints the count without asserting it [r2] | before PF6: otherwise the first time the in-sandbox files run on Linux is in a paid sandbox |
| Worker image build and push (mode `image`) | cannot be built or tested offline (base image pull); arm64 host, amd64 guests; no registry chosen. The wheelhouse mode needs no registry and is what the preflight uses | the image decision of 8.1, for the full run: build with `docker buildx build --platform linux/amd64 --push`, verify the registry's manifest, pin the amd64 manifest digest, push to two registries |
| Saved views, the Report text, the visibility change | remote writes behind `publish` | U11 |
| A scores-only arm; the Claude Code plugin route; Harbor | outside the fixed decisions; U9 chose arm (a) | a user request |
| Layers 2 to 4 of the noise floor (sampling, selection and search noise) | they need no calls: bootstrap and McNemar over the recorded per-example results, the predeclared selection rule, and a quoted spread for a second search (Appendix G); they are computed in the write-up, not by this package | the article draft |
| Wall-clock schedule (critic gap 12) | operational: `runner.keep_awake` starts `caffeinate -is`, the search runs in blocks, `status` raises a stall alarm, a host sleep is detected | PF7 and PF10 timings |
| Reading organisation usage from the platform instead of from the user | Forge-key users have no per-sandbox usage API [rep], and whether the role can read organisation usage is account-specific; PF-R takes the figures from the user | PF3 shows the role can read usage |

---
## Appendix A. Choices between drafts

This appendix records the merge of the three drafts as it was made. Where the adversarial review later changed a choice (items 14, 19, 21 and 33 among them), Appendix E and the sections it names are the current rule. The second research round changed more of them, and there Appendix F and the sections it names are the current rule: item 14 (one candidate attempt and up to three infrastructure attempts), item 19 (six meters, with ingest buckets), item 21 and item 42 (keys come from a key source; the exporter child gets its key over a pipe, not by variable name), item 39 (`PYTHONPATH` is set and auto memory is disabled by code; the two manifest fields are gone), item 41 (a wheelhouse mode beside the image).

F = fidelity draft, X = failure draft, T = testability draft. Where the three agreed (workspace copy, upstream streaming with a merged body, reservation after the semaphore slot, retrospective export, `CampaignStop(BaseException)`, usage-limit stop, allow-listed proposer environment) there is no row.

| # | Topic | F | X | T | Chosen | Why |
|---|---|---|---|---|---|---|
| 1 | Capture seams | `_generate_one` + evaluator | `litellm_completion`, `LLM.__call__`, `LLM.batch`, loaders, `MemorySystem.__init__` | `_generate_one`, `LLM.batch`, evaluator | T | Confirmed twice on the real worker [F-E1, T-E2]; fewest patches; the completion call stays upstream's; `batch` hashes cover the one case thread adjacency misses |
| 2 | Join key sidecar to gateway | `user_sha256` | `prompt_key` over messages, temperature, max_tokens | `user_sha256` | `user_sha256` | One hash of one string on both sides; confirmed equal after truncation [F-E1] |
| 3 | Sidecar path | environment variable | flag | flag | flag | The worker then reads no variable of ours |
| 4 | Client timeout (600 s) | coalesce retries | whitespace heartbeat [X-E1] | coalesce retries | coalesce; heartbeat deferred (8.3) | The findings' binding constraint is coalescing; a refusal after a committed 200 departs from F4's "402 or 403"; coalescing already makes the retries free |
| 5 | Statuses the gateway returns | 400, 402, 403, 502 | 402, 403 (and 200 after commit) | 402, 403 | 402, 403 | F4. F measured all four as one request [F-E3], so nothing is lost |
| 6 | Relay shape | stdio and daemon modes in one stub | daemon, bridge, `boot.py`, forced rotation, heartbeats | stdio only; a drop fails the attempt | F's two modes; X's resend-after-`HELLO` rule plus a done cache; T's `os.read` / `os._exit` rule | The findings bind "run the worker detached"; the SDK cannot reattach and retains stdout, which rotation bounds; stdio stays for simple tests. Daemon path run offline in [L1] |
| 7 | Frame format | header line + base64 body, envelope payloads | multi-part lines with per-part CRC | one frame, JSON meta + body | T | Carried 94 concurrent multi-byte requests [T-E1]; one uniform payload shape; re-run in [L1] |
| 8 | How the stub gets its job | flags | `job.json` | flags | job file | One place for argv, environment and limits; nothing job-specific in an exec argv |
| 9 | In-executor helper | none | `boot.py` (unpack, selfcheck, start, collect, stop) | none | `mhf_selfcheck.py` plus two `python -c` snippets | The stub already supervises; the selfcheck (tokenizer file, credentials, egress) is the part worth a file |
| 10 | Job identity | hash of upstream's description | `<split>.<candidate>.<dataset>.a<n>` | `<phase>.i<NNN>.<candidate>.<dataset>.<split>.a<n>` | X, plus a `qual.` prefix | Readable, path-safe, independent of phase and iteration (which can differ on a rerun) |
| 11 | Worker argv | upstream's builders | upstream's builders + prefix check | own builder + comparison test | upstream's builders behind a facade, with X's prefix check | `--temperature 0.0` and the split sizes are right by construction [F-E2, X-E9] |
| 12 | Skip-if-usable | upstream's rule | journal-backed results only | upstream's rule | upstream's builder, after quarantining result files the journal does not know | Keeps upstream's code in charge and still refuses a planted score |
| 13 | A candidate with a failed dataset | publish whatever exists | quarantine (default) | publish only `ok` jobs | manifest switch, default `upstream`; result files only from successful attempts | Decision U9a: the proposer and the frontier see what upstream would; our own metrics mark it incomplete; the switch exists for the distortion X describes |
| 14 | Job attempts | 3, persisted | 2, infrastructure only | 3, persisted; infrastructure on the last attempt stops | 3 candidate-attributable (persisted) plus a separate infrastructure budget per runner invocation; exhausting it stops the campaign | Upstream reruns any failure 3 times; an infrastructure failure must not become a candidate's result or use up its attempts for ever |
| 15 | Jobs of one candidate at once | 1 | 3 | 1 | 1 (manifest field) | Findings D10; without the heartbeat, parallel jobs only lengthen queue waits |
| 16 | Ledger connections | one per operation | one shared connection, separate exporter database | one per operation | one per operation, one file | The H2 pattern; safe for the exporter process too; fast enough at 16 in flight |
| 17 | A cap reached by open reservations | validation rule (cap >= 2 x 16 x R) | wait instead of halt | halt | validation rule | No waiting logic in the money path; a halt then means the cap is really within 16 reservations |
| 18 | Raising a cap after a halt | no | recorded amendment | no | deferred (8.3) | A predeclared ceiling; the user's call |
| 19 | Meters | three typed tables | generic table, once per reference | generic table | generic `meter_events`, primary key `(meter, key)` | One mechanism; idempotent by construction |
| 20 | Job tokens | derived by HMAC from a state-directory secret | random, memory only | random `mhfjob_...`, hash stored, pattern scanned | T | No secret file for the proposer to read; the scanner still finds any leaked token |
| 21 | Who reads the Forge key | a `KeyHolder` in the driver | the provider client, then deleted | the provider client, then deleted | the provider client | F14's letter |
| 22 | Usage in a replayed reply | zeroed | original | original | zeroed for stored hits, original for coalesced followers | A stored hit is upstream's cache hit (0 tokens) [F-E3]; a follower is usually the same worker's own retry |
| 23 | Negative caching of context-limit rejections | yes | no | no | no | A repeat costs nothing at the provider; one less kind in the store |
| 24 | Call record granularity | one per provider attempt | one per request with `attempts[]` | one per request with `attempts[]` | one per request | One record per span and per sidecar call |
| 25 | Provider client | stream protocol, merge in the gateway | `complete()` returning a result | `complete()` + pure `classify` + loopback SSE stub | T, with X's three timeouts | The table is testable without I/O; the real HTTP path runs offline |
| 26 | Concurrency limit | fixed semaphore | adaptive limiter | fixed semaphore | fixed semaphore | F4 says "sized from the manifest"; the preflight sets the number |
| 27 | Proposer seam | shim on `claude_wrapper.subprocess` | own process loop + upstream's builder, parser and logger | shim | shim, with F's timer (SIGINT at the timeout, wrapper deadline as backstop) | `claude_wrapper.run` stays in charge [T-E3]; the wrapper's own log then also gets the trailing `result` event |
| 28 | Proposer again after a stop | new session | stored proposal replayed [X-E4] | new session | replay; X's iteration arithmetic | A halt in the middle of an iteration is an expected event here; a second session is paid for and proposes something else |
| 29 | Signals | upstream's handler | own stop flag | set `_interrupted` | own stop flag | `_interrupted` writes false `failed` rows [X-E4] |
| 30 | Workspace path and `uv` | `workspace/reference_examples/text_classification`, `.venv` symlink | `workspace/text_classification`, `UV_*` variables | same as X, plus a lock copy | X and T | Nothing depends on the parent's name [src]; no symlink out of a tree we hash |
| 31 | Bundle source | the workspace | pristine repository files + workspace `agents/` | the workspace | X | A proposer edit to pinned files cannot change an evaluation even before the restore runs |
| 32 | Exporter process | child after each unit | separate process, inline child by default | after the search | separate command; `manual` default, `after_unit` optional | The runner never imports an SDK; live export is useful for a multi-day run but untested, so opt-in |
| 33 | Replayed calls in spans | counted, not emitted | spans with `usage: null` | spans with `usage: null` | spans with `usage: null` and hashed text | Every example has a span in its own job's conversation; almost no billed bytes |
| 34 | Receipt | one file, forward-only statuses | one file with history | intent file + receipt | one file with history | One thing to read on resume; `partial` covers "ambiguous" |
| 35 | Forecast | H2 rule: largest observed call cost | observed output quantile x manifest volumes | largest observed per dataset | X | The qualification job has tiny prompts; input volume must come from the measured few-shot volumes |
| 36 | Gate before spend | qualification receipts | qualification | offline gate: smoke receipt for this engine, protocol filled | T's offline gate plus qualification | Free, and it ties live spend to a passing offline smoke of the exact code |
| 37 | Engine snapshot | excludes telemetry, audit, publish | everything | everything | F | An exporter fix should not force a new qualification |
| 38 | User decisions in the manifest | scattered fields | zero budgets | a `decisions` block | `decisions` block; draft budgets hold the recommended numbers | One place to read what the user has not yet decided |
| 39 | Proposer `PYTHONPATH` / auto-memory | unset / not disabled | set / a gate | set / disabled | unset / not disabled; both manifest fields | Upstream's behaviour by default [rep CM-V M9]; fingerprints record the memory directory either way |
| 40 | Injected clock and ids | partly | no | everywhere | T | Backoff and timeout tests run instantly |
| 41 | Worker image files | deferred | `Dockerfile` | `Dockerfile` + lock copy | T | Findings D15: the lock is committed under `showcase/` |
| 42 | Trace of F14 for the exporter | key handed to a child by the driver | exporter reads its own variable | exporter reads its own variable | exporter reads its own variable; `procenv.exporter_env` copies it by name for a spawned child | The inference key is still read only by the provider client |
| 43 | Reading of F2 (`OPENROUTER_API_KEY` "carrying a per-job token") | job token on loopback, placeholder under the relay | job token everywhere, checked by the stub | job token on loopback, literal under the relay | a per-job token on the loopback ingress, where it is the identity; the placeholder `mhf-relay` under the relay | Under the relay the channel is the identity (findings Q3), so no credential-shaped value has to exist in the sandbox, in a job file or in platform logs |

## Appendix B. The completeness critic's 15 gaps

| Gap | Blocking | Where it is handled |
|---|---|---|
| 1 Relay stream behaviour | yes | resolved: ASCII frame 2.9; transport-agnostic core 3.10; `FrameChannel` 3.12; detached stub with replaceable bridges, rotation against the SDK's stdout buffer, stdio mode as fallback 3.13; offline run [L1]; live soak in PF6; mailbox deferred 8.3 |
| 2 Streaming to the provider | yes | resolved: stream upstream with usage, merge, one body to the worker (3.8 to 3.10); error-in-200 handled; timeouts in the manifest |
| 3 Reply-cache scope | yes | resolved: `routing.reply_reuse_scope`, replay store and coalescing 3.7; zero-usage hits; V6 |
| 4 Agent Lens ingestion cost | yes | resolved: `lens_bytes` meter, `budgets.lens_ingest_mb`, content policy in the planning layer (3.24, 3.25 step 4); second round: ingest buckets, a per-session cap, a span-count meter, `hash` by default, turns without content; the estimator is calibrated in PF8 |
| 5 No proposer-runtime report, no fixture | yes | resolved in the second round: the report and its verification exist (`<scratchpad>/research2/proposer-runtime.md`); the converter reads the measured 2.1.317 shape and is tested against the corrected synthetic fixture (3.21, 5.1); the version gate refuses any other CLI version; what only a real `-p` session can show is checked in PF9 and PF10 |
| 6 Emission model | yes | resolved: everything retrospective, one exporter, scan before emit, nothing held open (3.25) |
| 7 Proposer environment contamination | no | designed: allow-list 3.5; `env.json` records names |
| 8 Key readable on this machine | no | designed as far as it can be enforced: keys overwritten in the runner's environment block (3.9), explicit sandbox auth headers (3.17), no `wandb.login` and an empty `NETRC` for the exporter (3.25), digest-based scanning (3.24), proposer process group reaped (3.19); residual risk stated (3.19, section 7); no listener while the proposer runs; second round: keys from a prompt or a descriptor (3.27), `NETRC` empty in every live process, no SDK default chain, the generated restricted and contained modes (3.19), checked in PF9 |
| 9 Evaluation image | no | resolved for the preflight in the second round: mode `wheelhouse` needs no registry (a stock image by amd64 manifest digest plus 62 hash-checked wheels, 3.17); a prebuilt image for the full run stays the user's decision (8.1, 8.3) |
| 10 Definition of "reproduces" | no | resolved in the second round: the protocol template with the claim wording, the claims not made, the selection rule, gates G0 to G2 with numeric bands, the noise plan and the control (Appendix G), signed in two stages; `evaluate --calibration` and `evaluate --noise` (3.20); the gate bands in `calibration.gates` |
| 11 Test finalisation | no | designed: 3.18 dispatch and test inputs, 3.20 (`run_calibration`, `run_test`), the search and test gates of 2.3, 4.4, ledger phase `test`, table key `test/<dataset>` |
| 12 Wall-clock, limits, usage limits | no | designed: `executor.job_timeout_s`, sandbox lifetime, V7, V13, `runner.keep_awake`; second round: one candidate attempt, a job limit that stops the sandbox, resumable blocks, a stall alarm, sleep detection, the limit classifier (3.18 to 3.20); timings from PF7 and PF10 |
| 13 Deviations presented as neutral | no | designed: V8, V10, V20; `unknown_job` records, `solver_attempt_signals` and `state_access_signals` (`evidence_exposed`); auto memory disabled, with fingerprints; second round: V30 to V35 and fidelity table A (Appendix G) |
| 14 Entity, plan, enablement, order | no | resolved in the second round: `account-check` (PF1 to PF4) reads the entity, the organisation's switches and the plan before any freeze and creates the project PRIVATE before the first inference call (PF5); configuration faults (401, 402, every 403, a quota 429) classified and halted (3.9, 3.10); entity and project are gates |
| 15 Dataset licence | no | resolved in the second round: one content rule set per dataset, `hash` by default; LawBench never opens, USPTO only after clearance; `decisions.dataset_text_publication` gates `export` and `publish`; the verbatim scan, hashed table columns, `THIRD_PARTY_DATA.md` and the attribution text (3.24, 2.14, Appendix G) |

## Appendix C. Ported-file table (for `PROVENANCE.md`)

Source: `ash0ts/harness2-wandb@31b0f82b02e433545dc95b42fb2393996636ff6f`, `harness2/showcase/`, Apache-2.0. Each file below starts with `# SPDX-License-Identifier: Apache-2.0` and `# Adapted from ash0ts/harness2-wandb@31b0f82 <path>. Changes: <summary>`. Every other file is new and MIT. `test_spdx_headers_match_provenance_table` keeps the table and the headers in step. Line references are [rep].

| New file | Harness2 source | What is kept | What changes |
|---|---|---|---|
| `mhforge/util.py` | `gateway.py` L19-L22 (`write_record`); `protocol.py` (`tree_hashes`, `changed_files`); `budget.py` (`micros`) | the helpers | UTC helpers, clock and id injection, symlink refusal, read-only SQLite open, loopback guard, environment scrub, key digests |
| `mhforge/manifest.py` | `runtime.py` L55-L62; `study.py` L35-L40 | freeze rule, read-only read | new schema, `decisions` block, live gates |
| `mhforge/state.py` | `study.py` L67-L108; `v4.py` L496-L500 | read-only status, runner lock | new fields; export lock |
| `mhforge/ledger.py` | `budget.py` L13-L255 | reservation model, `usage_within_limits`, settle rules, `interrupt_pending` | explicit per-job ceilings (requests per attempt, charged attempts, dollars), phases, halt table, running totals, indexes, meters with open and final rows, job registry, zero-cost rejections, `limit_exceeded` settles without a halt, `reconcile`, repeatable-failure table, no version switch |
| `mhforge/sse.py` | `gateway.py` L25-L54; `transport_v4.py` L25-L32 | `merge_stream` (which already reads both reasoning names, `gateway.py` L37) | completion-body builder |
| `mhforge/circuit.py` | `transport_v4.py` L91-L124 | breaker | `finish` takes `success`, `failure` or `neutral` and always clears the probe flag; `max_wait_s` is required; injected clock |
| `mhforge/gateway.py` | `gateway.py` L57-L217; `transport_v4.py` L70-L88, L146-L308 | admission checks, text-only rule, record-before-forward, single retry owner, identity check, error-in-200 handling | transport-agnostic core, identity by channel or token, closed 402/403 table, bounded semaphore with a holding flag, replay and coalescing, no partial forwarding, breaker paired by `try/finally`, store before settle, message-count bound, refused-record cap, repeatable-failure rule, drain |
| `mhforge/journal.py` | `runtime_v4.py` L42-L111 | operation keys and statuses | one append-only file; relaunch allowed |
| `mhforge/qualification.py` | `qualification.py` L23-L68; `v4.py` L175-L388 | numbered attempts, engine snapshot, forecast gate, restart rule | constants from the manifest; offline gate; new checks |
| `mhforge/audit.py` | `audit.py` L46-L115 | recompute and compare | new record shapes |
| `mhforge/telemetry/readback.py` | `telemetry.py` L137-L170, L201-L299 | query body, verification | pure functions; new attribute prefix |
| `mhforge/telemetry/adapter_agentlens.py` | `telemetry.py` L82-L134, L302-L408; `telemetry_v4.py` L49-L68 | span construction from records, trace id before any span ends, serialised exports | driven by span plans; flush result ignored |
| `mhforge/publish.py` | `report_v4.py` L847-L905 | allow-list bundle, ownership check | scan covers known values, key digests and job tokens; refuses by default; halt and reconcile history in the bundle |
| `mhforge/telemetry/policy.py` | `report_v4.py` L505-L511 | path scrubbing (`portable`) | moved here so the run planners can apply it; the content rules, attribute rules and hex masking in this file are new |
| `tests/test_ledger.py` | `tests/showcase/test_budget.py`; `test_study_v4.py` L61-L127; `test_safety.py` L13-L23 | the cases named as ported | pytest to unittest |
| `tests/test_gateway.py`, `tests/test_sse.py`, `tests/test_circuit.py` | `test_gateway.py`; `test_transport_v4.py` (not the child-session case) | the cases named as ported | non-streaming worker side added |
| `tests/test_exporter.py`, `tests/test_readback.py`, `tests/test_plan_spans.py` | `test_telemetry.py`, `test_telemetry_v2.py` (model-call cases); `test_safety.py` L26-L44 | the cases named as ported | fakes instead of dict fixtures |
| `tests/test_manifest.py` | `test_safety.py` L55-L61 | freeze-then-change | |
| `tests/test_publish.py` | `test_report.py` L12-L44 | two visibility cases | |

Nothing is taken from Harness2's `third_party/` or from its non-showcase modules.

## Appendix D. Upstream hook index

All lines at `f805ad3`. Rows marked [src] were re-read while merging; the rest are as the drafts read them.

| What we rely on | File and lines |
|---|---|
| `EVOLVE_DIR`, `AGENTS_DIR` from the module's own path; run-scoped path globals rebound per run | `meta_harness.py` L26-L37; L380-L388 [src] |
| `run_benchmark` definition and call sites | `meta_harness.py` L119-L133; L296, L325, L330, L333, L421, L428, L519, L527 [src] |
| `count_iterations_from_summary`; loop start; failed proposer `continue` | `meta_harness.py` L149-L161; L444-L449; L476-L480 [src] |
| `propose_claude`: environment pops, wrapper arguments, return rule | `meta_harness.py` L164-L189 [src] |
| `validate_candidates` (host import, cwd `EVOLVE_DIR.parent`) | `meta_harness.py` L192-L215 [src] |
| `update_evolution_summary` and its call sites | `meta_harness.py` L218-L254; L498-L500, L553-L560 |
| pending-eval deletion; candidates read; `_interrupted` checks | `meta_harness.py` L467-L468; L482; L417, L446, L512, L573 [src] |
| finalisation | `meta_harness.py` L286-L357, entered at L391-L393 |
| job builders, skip rule, force rule, shuffle | `benchmark.py` L464-L531, L534-L608; L487-L489, L559-L561; L527-L528, L604-L605; L530, L607 [src] |
| worker command prefix (9 tokens) | `benchmark.py` L296-L309 [src] |
| discovery, `run_dir`, usable-result test, summary | `benchmark.py` L39-L48; L139-L148; L171-L176; L713-L730 [src] |
| `main` flow we mirror; file-only branches; three attempts; exit code | `benchmark.py` L865-L906; L835-L863; L79-L107, L936; L958 [src] |
| launcher log format and name | `benchmark.py` L61-L62, L82-L102, L121 [src] |
| wrapper process handling | `claude_wrapper.py` L573 (environment), L590-L598 (`Popen`), L496-L502 (`readline`, `close`), L615-L621 (deadline kill), L626, L649-L651 [src] |
| wrapper command; plugin directory | `claude_wrapper.py` L115-L168; L20, L159-L160 [src] |
| worker entry, loader binding, `LLM` import, memory loading | `inner_loop.py` L516-L825; L519; L614-L634; L637; L479-L498, L659 [src] |
| evaluator call sites; result normalisation | `inner_loop.py` L226, L353, L446; L43-L60 [src] |
| two validation passes, test pass; result files | `inner_loop.py` L177-L199 (the pass after the learning epoch) and L748-L760 (the final evaluation); L795-L825 [src] |
| result-file discovery (`rglob`, any depth, three-part paths) | `benchmark.py` L180-L196 [src] |
| finalisation: test systems, `finalized.json` states, completeness, the refusal of a later search | `meta_harness.py` L292-L357; L395-L402 [src] |
| candidate list printing (`hyp[:80]`) before validation; import timeout | `meta_harness.py` L486-L488; L206 [src] |
| iteration count from the summary file | `meta_harness.py` L149-L161 [src] |
| sibling imports of the three driver modules | `meta_harness.py` L21-L24; `benchmark.py` L3-L12; `claude_wrapper.py` L8-L17 [src] |
| `LLM.batch` worker cap only without `api_base` | `llm.py` L324-L325 [src] |
| provider layer seam; cache path and load; cache directory | `llm.py` L252-L268; L164-L185; L28-L35 [src] |
| completion call, timeout, bearer; retry predicate and decorator | `llm.py` L15, L203-L220; L90-L109, L197-L202 [src] |
| temperature default for gpt-oss; system prompt; truncation; `batch` | `llm.py` L306-L311; L322; L356-L367; L406-L422 [src] |
| loader; evaluators and their metrics | `data/api.py` L107-L153 [src]; `data/evaluators.py` L62-L76, L99-L129 |
| dataset directories | `data/loaders.py` L88, L133, L163 [src] |
| skill rules cited (L78 is the import line that leaves `agents/__pycache__`) | `.claude/skills/meta-harness/SKILL.md` L10, L14-L17, L64-L69, L78 |

## Appendix E. What the adversarial review changed

This appendix records the first revision as it was made; the second research round came afterwards (Appendix F), and where the two differ (the preflight order and its check numbers, the attempt counts, key handling, the content defaults, the calibration gate, the proposer's restricted mode) Appendix F and the sections it names are the current rule. Three reviews reported 64 defects: upstream compatibility (18), money and failure (20), security and buildability (26). `<scratchpad>/design/revision-log.md` lists each with the decision and the place it was fixed. None was rejected outright; several were accepted with a different fix from the one proposed, and the second table below and the log say why. The changes by theme:

| Theme | What changed | Sections |
|---|---|---|
| A live search that evaluated nothing | `__pycache__` and other stray entries in `agents/` no longer reject candidates | 3.14 |
| Calibration and finalisation | a calibration path that never finalises; search blocked after any finalisation; finalisation only after the search | 2.3, 3.20, 3.27, 4.4, V27, V28 |
| Planted and stale files | every result file `load_results` reads is verified; test jobs take validated memory and code; stray workspace files quarantined; `evolution_summary.jsonl` guarded | 3.18, 3.19, V12, V16, V26 |
| Model-written code reaching the host | upstream loaded by path from hash-checked bytes with `sys.path` untouched; `benchmark.py` children with `-P`; no bytecode cache | 3.14, 3.18, V2, V18 |
| Attempts and attribution | per candidate-hash attempt budget, baselines always infrastructure, operator reset, waits between infrastructure attempts, import-check probe, taint-first attempt classification, repeatable provider failures | 3.16, 3.18, 3.10, V7, V25 |
| The gateway could hang or mis-book | three-valued circuit `finish` paired by `try/finally`; bounded semaphore with a holding flag; store before settle; busy-ledger handling; drain at close and at stop; complete streams without `[DONE]` | 3.8, 3.9, 3.10 |
| One request ending the campaign | message-count bound in admission and in the reservation; `limit_exceeded` settles without a halt; `reconcile` for `unknown` rows; cached price bound | 2.3, 3.6, 3.10 |
| Ceilings that were not ceilings | request ceiling per job attempt with hard bounds across attempts and a dollar cap; meters opened at the worst case and closed with the measurement; forecast covers sandbox and proposer budgets | 2.10, 3.6, 3.17, 3.19, 3.22 |
| Relay liveness and races | ping schedule and pong timeout; write timeout; one decoder per channel; early base64 rejection; writer queue; `CAN` and `KILL` frames; connection-swap lock; stream budget per job | 2.9, 3.12, 3.13, 3.17 |
| Keys | no `wandb.login`; empty `NETRC`; in-place overwrite of keys in the environment block; explicit sandbox auth headers; three distinct key variables; digest-based scanning; exact `keyed_hex40` rule and the `vocab_file` rename; key redaction in provider error text | 2.6, 3.9, 3.17, 3.24, 3.25, section 7 |
| Halt handling | `halt` command; a halt ends the running job within a minute; `export` and `publish` run with a halt set | 2.3, 3.16, 3.27 |
| Proposer session handling | success is decided before limit patterns are tried; stricter `pending_eval.json`; process group reaped at every exit; state access detected; sessions never free | 3.14, 3.19, V20 |
| Export | final receipts survive a changed plan (`stale`), `--supersede`, dry-run receipts replaced by a live export, stable search-step plans, `define_metric` instead of an explicit step, split rule for every plan kind, outer-loop roll-over, placeholder project for unset decisions, admission-only records | 2.13 to 2.16, 3.23, 3.25 |
| Buildability | `SC` defined as the manifest's directory; layering fixes (`scan.py`, `loopback_guard`, `make_executor`, `sandbox_cost_micro`, `portable`); complete environment-access table; `fullmatch` for identifiers; bounded ids; path rule for the worker interpreter; read-only SQLite open; import-check job file | 0, 2.2, 2.3, 2.10, 3, 3.2, 3.5, 6 |

Where this revision did not take the reviewer's fix as written:

| Defect | Reviewer's fix | What the spec does instead, and why |
|---|---|---|
| request ceiling and flood refusal (money and failure) | the flood refusal `relay_overloaded` is infrastructure | it is the job's own ceiling. With the `CAN` rule only requests whose client is still waiting count, a well-behaved worker has at most 64, and an infrastructure classification would let a flooding candidate stop the campaign |
| attempt classification (money and failure) | any request unanswered at worker exit is a relay gap | only when the worker ended by itself. A worker killed at its timeout always has requests in flight; counting those would turn every slow candidate into an infrastructure stop |
| key visibility (security) | record `key_scrubbed` in `engine.json` | in `STATE/runtime.json`. `engine.json` is the frozen engine snapshot that qualification compares by hash; a run-time flag must not change it |
| scanner (security) | every component stores a key digest | done, and stated as a reading of F14 that the user may narrow (section 7, 8.1) |
| import-check metering (security) | meter key `import_check:<iteration>:<n>` | the sandbox id, as for every sandbox, with the purpose in the row's detail (one rule for all sandboxes, from the meter defect) |
| span limits (security) | `loop_iterations_per_conversation * (2 + 2 * candidates) + 4` | `loop_iterations_per_conversation * (3 + 2 * candidates)`: an iteration has a turn, a propose span, an import check and a benchmark per candidate and a frontier span |
| small drifts (security) | an over-long id becomes `<first 40>~<8 hex>` | `<first 119>-<8 hex>`: `~` is outside the conversation-id character set, and 119 keeps the readable prefix |
| unknown ledger rows (money and failure) | `spent >= reservation unless the note names a billing-export line` | an explicit flag (`--from-billing-export`), stored with the row; free text in a note cannot be checked |
| other exceptions from the loop (security) | journal reason `upstream_contract` | `internal_error` unless the exception is an `UpstreamContractError`; most such exceptions are ours, not upstream's |
| test inputs (upstream compatibility) | restore from the proposal copy when the workspace hash differs | the same, generalised: every job runs the stored copy of its candidate, and a test job runs the copy with the hash that was validated on that dataset; a missing copy fails the test job rather than testing unvalidated code |
| smoke network guard (security) | run workers under `sandbox-exec` when the tool exists | when a probe of the tool succeeds (inside an outer sandbox it fails), and the receipt records which was used |

## Appendix F. Second-round amendments

One row per item of the second round. The item ids (P1 to P12, K1 to K3, J1 to J4, C1 to C7, W1 to W3, S1 to S10, B1 to B4, Q1 to Q6, F1, F2) are the round's own; they are not preflight check numbers (those are PF0 to PF11, 8.2). Inputs: `<scratchpad>/research2/preflight-readiness.md` (PLAN), `critic2.md`, `proposer-runtime.md` and its verification.

| Item | Status | Sections changed | What was done |
|---|---|---|---|
| P1 | applied | 2.3, 3.19, 7 (V11, V33) | Common argv in every mode: pinned binary with hash check, `--model`, `--max-budget-usd` from `proposer.max_budget_usd_per_session` (null refuses live search and the live proposer steps), `--settings` with the three common keys. |
| P2 | applied | 2.11, 3.19, 7 (V30) | Success is a `result` event with `is_error` false; the exit code is never success. |
| P3 | applied | 2.3, 2.11, 3.19, 7 (V31) | `check_model` on `init`, every main-thread reply and the three fallback subtypes; subagent models recorded; outcome `model_mismatch` stops the campaign. Added `proposer.expected_resolved_model`. |
| P4 | applied | 2.3, 2.11, 3.19, 3.21 | `session_cost`: result event, else the last `cost-state` record, else usage once per `message.id` at list prices, else the estimate; never zero. |
| P5 | applied | 2.3, 2.11, 3.19, 7 (V11) | Own process group; SIGINT, SIGTERM, SIGKILL; deadline on the monotonic clock, moved by rate-limit `api_retry` waits (capped at one timeout); the wrapper's own wall-clock deadline is switched off (`timeout_seconds=None`). |
| P6 | applied | 2.11, 3.1, 3.19 | `integrity_violations` after every session; `permission_denials` recorded; a violation raises `CampaignStop("proposer_integrity")`. Which tree changes count is defined per mode (see choices). |
| P7 | applied | 1, 2.3, 2.4, 2.11, 3.19, 5.1, 5.2, 7 (V32) | Three modes with the exact argv; no `--allowedTools` in `restricted`; generated settings (`generate_settings`), optional override merged over it, the addendum text and its hash, the `sandboxed: true` check with outcome `settings_ignored`. |
| P8 | applied | 2.3 (rules 9, live gates), 3 head, 3.5, 7 (V10) | Closed list `PROPOSER_FIXED_ENV`, the full deny-list asserted for every session, `CLAUDE_CODE_RETRY_WATCHDOG` on the deny-list, live proposer start refused inside a Claude Code session. `proposer.pythonpath` and `proposer.disable_auto_memory` removed. |
| P9 | applied | 2.3, 2.11, 3.1, 3.2, 3.19, 7 (V13), 8.3 | Limit classifier with structured fields first and text as fallback; the outcome list; reactions; raw events kept in `stream.jsonl`. |
| P10 | applied | 2.3, 3.1, 3.19, 3.21, 3.23 | Converter reads `api-request` and joins on `requestRef`; usage once per `message.id`; `cost-state`; version gate on `proposer.cli_version`; transcript found by glob; orphaned and superseded files copied; `attachment` never exported; no cumulative context in an `llm` span. |
| P11 | applied | 1, 5.1, 5.2, 6 | Fixtures are byte copies of the corrected research fixture (checked on disk: repaired); test cases per PLAN L485. |
| P12 | applied | 2.3, 3.6, 3.19, 3.22, 7 (V33), 8.1 | `budgets.proposer_list_usd` is real spend; cumulative cap enforced from `result.total_cost_usd` with the P4 fallbacks; an open session counts at its per-session cap. |
| K1 | applied | 1, 2.3, 2.4, 3 head, 3.4, 3.9, 3.17, 3.25, 3.27, 7, 8.1 | `--key-source {prompt, fd:N, env}`; prompt default on a TTY, one prompt per role, Enter reuses; env keeps the in-place scrub; never in argv; the exporter child gets its key over a pipe. |
| K2 | applied | 2.3, 2.4, 3 head, 3.5, 3.25, 3.27, 5.2 | `NETRC` set to `STATE/export/empty.netrc` in every live process and in the exporter child; `test_fake_home_netrc_byte_identical_after_export`; `wandb.login` still never called. |
| K3 | applied | 2.3, 3.17, 3.25, 3.27, 5.2 | Every SDK call gets the key explicitly; a missing key refuses; `test_no_sdk_default_credential_chain`. Mode `wandb_auth` sets `WANDB_API_KEY` in-process from the given key. |
| J1 | applied | 2.3, 3.4, 3.25, 8.1, 8.2 | `campaign_kind`; two projects; each campaign has its own manifest and `STATE`. |
| J2 | applied | 2.3, 2.4, 3.3, 3.22, 3.27, 8.2 (PF1 to PF4) | `account-check --live` with an unfrozen manifest; reads only, plus `--create-project` (look up, create PRIVATE, read back); account facts stay in `STATE/account/`. |
| J3 | applied | 2.3, 2.4, 3.3, 3.22, 8.2 | `import_problems` and `manifest.IMPORT_DIFF_ALLOWED`; the named set is written out in 3.22; the measured campaign re-runs only the per-project checks. |
| J4 | applied | 2.3 (rule A), 2.4, 2.7, 2.16, 3.3, 3.25, 3.26, 3.27 | `STATE/amendments.jsonl`, hash-chained to the frozen manifest; `amend` command; the exporter uses the effective manifest and receipts record the amendment. |
| C1 | applied | 2.3 (rule 16), 2.13, 3.2, 3.23, 3.24, 8.1 | `telemetry.content.<dataset>.*`, default `hash`; LawBench stays `hash`; USPTO needs `decisions.uspto_text_cleared`; `export --live` refuses while the decision is unset; strictest rule when not attributable. |
| C2 | applied | 2.8, 2.12, 2.14, 3.15, 3.23 | `question_sha256` input column; labels for Symptom2Disease and LawBench; `prediction_sha256` and `target_sha256` for USPTO until cleared. `split` kept as an input column (see choices). |
| C3 | applied | 2.3, 2.15, 2.16, 3.23, 3.24, 3.25 | Candidate files only, after the verbatim scan (exact strings for USPTO); never `memory.json`, `log.jsonl`, the cache or a transcript (hash and size logged); the scan covers Artifact metadata and outer-loop arguments. |
| C4 | applied | 2.13, 3.23 | Turns carry no content; counts and hashes as attributes. |
| C5 | applied | 2.15, 3.24, 3.26 | `log.jsonl` treated like `memory.json` in every rule. |
| C6 | applied | 1, 6, Appendix G | `showcase/THIRD_PARTY_DATA.md`, owner `L0-docs`. |
| C7 | applied | 3.24, 3.25, 3.26, 3.27 | `PRIVACY_PATTERNS` (`/Users/<name>/`, `/home/<name>/`) and a host name passed in as data; `scan.py` reads nothing from the machine. |
| W1 | applied | 3.25, 8.2 (PF8) | The seven `wandb.Settings` switches and `dir=` under `STATE`, marked [live: PF8]. |
| W2 | applied | 2.15, 3.3, 3.23 | `plan_runs.RUN_CONFIG_ALLOW`; no local path, CLI path or account fact. |
| W3 | applied | 1, 3 head, 3.5, 3.25 | `WANDB_DISABLE_WEAVE=true`; the exporter refuses when `weave` is importable. |
| S1 | applied | 1, 2.3 (rule 10), 2.6, 3.1, 3.2, 3.16, 3.17, 3.18, 3.22, 8.3 | `sandbox.env_mode`; wheelhouse path; `env_setup_failed`; no mount; `snapshot` and `install_then_deny` rejected and deferred. |
| S2 | applied | 2.4, 2.6, 3.17, 3.22 | Environment identity per job (`executor.json`) and at qualification (`passed.json`). |
| S3 | applied | 2.3, 3.17 | `LITELLM_LOCAL_MODEL_COST_MAP=True` and a `TIKTOKEN_RS_CACHE_DIR` discovered inside the sandbox. |
| S4 | applied | 2.6, 3.13, 3.16, 3.17 | Every sandbox proves machine, Python version and freeze hash before the worker starts. |
| S5 | applied | 2.3, 3.17 | Lifetime = job limit + 900 s + setup allowance (150 s for wheelhouse); entity and project explicit; `dns_egress="deny"`. |
| S6 | applied | 2.3, 2.6, 3.13, 3.16 | Literal-address probes; `1.1.1.1:443` gates; the others recorded. The DNS lookup probe stays as a second gate (see choices). |
| S7 | applied | 2.3, 3.1, 3.17, 3.18 | `budgets.sandbox_usd` "40.00"; create errors mapped on the error reason; `RESOURCE_EXHAUSTED` without a reason is `billing` and terminal. |
| S8 | applied | 2.3, 3.18, 7 (V7) | `executor.max_attempts` 1, `executor.max_infra_attempts` 3 (`env_setup_failed` included). |
| S9 | applied | 2.3 (rule 19), 3.18 | `executor.parallel_dataset_jobs` `{"validation": 1, "test": 1}`; test always 1. |
| S10 | applied | 2.3, 2.7, 2.15, 3.2, 3.4, 3.16, 3.20, 3.27, 7 (V34) | Stop at an iteration boundary; stall alarm; sleep detection; `--max-iterations-this-run`; one search run id across blocks. |
| B1 | applied | 2.2, 2.3, 2.10, 3.1, 3.2, 3.3, 3.6, 3.20, 8.3 | Phases `calibration` and `controls`; solver defaults 3, 8, 3, 28, 5, 3, 10 (sum 60). |
| B2 | applied | 2.3 (rule 18), 2.10, 2.13, 3.6, 3.24, 3.25 | 175 MB split 150, 5, 20; 4 MB per proposer session; `lens_spans` meter; `policy.span_bytes`; refusal when a bucket is empty. |
| B3 | applied | 2.3, 2.10, 3.6, 3.25 | `budgets.wandb_upload_gb` 2; `budgets.eval_tables_max` 250. |
| B4 | applied | 2.3, 2.5, 3.1, 3.9, 3.10, 4.1 | Status, then `error.code`, then message substring; 401, 402, 403 halt; a quota 429 is terminal (`provider_quota`). |
| Q1 | applied | 1, 2.3, 2.4, 3.20, 7 (V27), 8.1, Appendix G | Protocol template content; `calibration.gates` holds G1 and G2; no gate cites the paper's few-shot figure. |
| Q2 | applied | 1, 2.3, 3.22, 8.1 | Two-stage sign-off: `TEMPLATE-UNFILLED` (stage 1, offline gate) and `STAGE2-UNSIGNED` (stage 2, `search` in a measured campaign). |
| Q3 | applied | 2.11, 3.13, 3.16, 3.18, 3.20, 5.1, 7 (V9) | Loaded-class assertion in the import check; `rejected: "loaded_class"`. |
| Q4 | applied | 2.6, 2.11, 3.10, 3.20, 3.23 | Per-candidate `context`: `memory_context_chars`, characters divided by 4, real prompt tokens, largest prompt. |
| Q5 | applied | 2.2, 2.5, 2.6, 2.10, 3.2, 3.7, 3.10, 3.20 | Scope kind `repeat` keyed by the repeat index; reuse salt for samples; per-attempt worker cache. `repeat` is a scope kind chosen by the job, not a manifest value (see choices). |
| Q6 | applied | 7 | The sentence about the paper's server is corrected. |
| F1 | applied | 3.22, 3.27, 8.2, Appendix B | Twelve steps PF0 to PF11 with corrections (a) to (f), the reconciliation gate PF-R, the teardown list and the mapping from P0 to P15. |
| F2 | applied | 3.10, 3.22, 5.2, 8.2 | The concurrency ramp is dropped, with `Gateway(overrides=...)` and `Context.gateway_factory`. |

**New names.**

| Name | Kind | Defined in |
|---|---|---|
| `campaign_kind` | manifest field | 2.3 |
| `decisions.uspto_text_cleared` | manifest field | 2.3 |
| `decisions.key_provisioning = "dedicated"` | manifest value (was `dedicated_env`) | 2.3 |
| `decisions.proposer_permission_mode = "contained"` | manifest value | 2.3, 3.19 |
| `decisions.dataset_text_publication = "per_dataset"` | manifest value (was `full_text`) | 2.3 |
| `provider.terminal_429_patterns` | manifest field | 2.3, 3.9 |
| `budgets.solver_usd.phases.calibration, .controls` | manifest fields | 2.3 |
| `budgets.lens_ingest_split_mb, budgets.lens_proposer_session_mb` | manifest fields | 2.3 |
| `budgets.wandb_upload_gb, budgets.eval_tables_max` | manifest fields | 2.3 |
| `accounting.reconcile_tolerance` | manifest field | 2.3 |
| `calibration.gates, calibration.noise_repeats` | manifest fields (replace `gate_system`, `expected_macro_test_accuracy`, `tolerance`) | 2.3 |
| `executor.parallel_dataset_jobs` | manifest field (replaces `executor.max_parallel_jobs`) | 2.3 |
| `sandbox.env_mode, sandbox.base_image, sandbox.base_image_fallback, sandbox.wheelhouse.*, sandbox.expect.*, sandbox.setup_allowance_s, sandbox.object_store_endpoint` | manifest fields | 2.3 |
| `proposer.cli_version, proposer.expected_resolved_model, proposer.max_budget_usd_per_session, proposer.scratch_root, proposer.transient_retries, proposer.transient_backoff_s, proposer.max_limit_wait_s` | manifest fields | 2.3 |
| `telemetry.content.<dataset>.*, telemetry.verbatim_scan.*, telemetry.agent_lens.byte_estimate_factor` | manifest fields | 2.3 |
| `qualification.import_from` | manifest field | 2.3 |
| `runner.stall_alarm_s, runner.sleep_gap_s` | manifest fields | 2.3 |
| `mhforge.amendment/1, STATE/amendments.jsonl` | record and file | 2.3 rule A, 2.4 |
| `mhforge.account/1, STATE/account/` | record and directory | 2.4, 3.22 |
| `mhforge.passed/1 (environment, imported_from), mhforge.noise/1` | records | 2.4 |
| `JOB_PHASES (six), LEDGER_PHASES (seven), JOB_STATUSES, ATTEMPT_OUTCOMES, INFRA_OUTCOMES, REUSE_SCOPES, SESSION_OUTCOMES, CONTENT_FIELDS, NEVER_PUBLISH_TEXT, CLEARANCE_GATED` | constants in `wire.py` | 3.2 |
| JobIdentity.repeat, JobIdentity.sample; job key suffixes `.r<k>`, `.s<k>` | fields | 2.2, 3.2 |
| `env_setup_failed` | attempt outcome | 2.6, 3.2 |
| `host_sleep` | failure class; JobResult.host_sleep_s | 2.6, 3.16 |
| TranscriptError; ExecutorError kinds `env_setup`, `billing`, `permission`; MeterCapExceeded.bucket | errors | 3.1 |
| `budget:calibration, budget:controls, provider_quota` | halt reasons | 3.1 |
| `proposer_spend_limit, proposer_limit_wait, proposer_transient, proposer_model, proposer_sandbox, proposer_settings, proposer_integrity` | stop reasons (`proposer_usage_limit` removed) | 3.1 |
| `util.sleep_gap` | function | 3.2 |
| `StatePaths.sibling, paths.proposer_scratch; properties amendments, account_root, account_file, noise` | functions and properties | 3.2 |
| `manifest.AMENDABLE, IMPORT_DIFF_ALLOWED, diff_paths, read_amendments, append_amendment, effective, run_config` | constants and functions | 3.3 |
| `procenv.PROPOSER_FIXED_ENV, PROPOSER_DENY_NAMES, PROPOSER_DENY_PREFIXES, assert_proposer_env` | constants and function | 3.5 |
| `meters lens_spans, wandb_upload_bytes, eval_tables; column meter_events.bucket` | ledger meters and DDL | 2.10, 3.6 |
| replay.reuse_key; scope key `repeat:<job_key>` | function and scope | 3.7 |
| `quota_exhausted` | provider disposition | 2.5, 3.9 |
| `selfcheck fields freeze_sha256, distributions, egress.probes, expect_problems; IMPORT_CHECK_CODE` | report fields and constant | 3.13 |
| `job file fields repeat, sample, probe_endpoints, expect` | job file | 2.6 |
| `sidecar eval fields question_sha256, prediction_sha256, target_sha256` | sidecar | 2.8, 3.15 |
| `bundle.WHEELHOUSE_SNIPPET; JobSpec.expect_freeze_sha256; ImportResult.reason; SandboxSpec.fallback_image` | constant and fields | 3.16, 3.17 |
| `proposer_shim: ArgvSpec, session_argv, parse_upstream_argv, retry_pause_s, CREDENTIAL_READ_DENIES` | functions and constants (`upstream_argv`, `restricted_argv` removed) | 3.19 |
| `proposer: COMMON_SETTINGS, CREDENTIAL_PATHS, RESTRICTED_ADDENDUM, SettingsInputs, take_settings_inputs, generate_settings, merge_settings, check_settings, render_addendum, transcript_slug, limit_signal, check_model, check_sandboxed, integrity_violations, SessionFacts, session_cost, PLAN_WINDOW_TYPES, FALLBACK_SUBTYPES` | functions and constants (`session_list_price` removed) | 3.19 |
| Driver.run_noise; run_search(max_iterations_this_run=...); journal status `run: paused`; ops `noise`, `amendment` | methods and journal | 2.7, 3.20 |
| `transcript: usage_by_model, price_usage, bash_results, request_efforts; ApiMessage.request_ref, .effort; ToolUse.sandboxed; NormalizedSession.cost_state, .cli_version` | functions and fields | 3.21 |
| `qualification: protocol_stage, import_problems, qualified_environment, noise_stats` | functions | 3.22 |
| `preflight: Check.step, .required, .per_project; AccountReader, WandbAccountReader, ACCOUNT_STEPS, TEARDOWN, account_check, build_probe_script, probe_expectations, reconciliation` | types and functions (`gateway_factory` removed) | 3.22 |
| `PF0 .. PF11, PF-R` | preflight step ids (replace P0 .. P15) | 8.2 |
| `plan_runs.RUN_CONFIG_ALLOW` | constant | 3.23 |
| `scan: SCAN_CLASSES, PRIVACY_PATTERNS, VerbatimIndex, build_verbatim_index, scan_verbatim; parameters classes, hostnames` | constants, type, functions | 3.24 |
| `policy: strictest, rules_for, attribute_dataset, guard_text, span_bytes, planned_bytes` | functions (`plan_bytes`, `content_rules` and the rule `off` removed) | 3.24 |
| `WandbSink.project_access; Exporter.verbatim_index; receipt fields amendment, bucket, withheld_files, planned_spans` | protocol, method, fields (`ensure_project` removed) | 2.16, 3.25 |
| `cli: KEY_ROLES, parse_key_source, roles_needed, read_keys; commands account-check, amend; flags --key-source, --max-iterations-this-run, --noise, --teardown, --platform-inference-usd, --platform-sandbox-usd, --waive-reconciliation, --claude-spent-usd, --claude-limit-usd, --claude-reserve-usd, --create-project` | functions, commands, flags | 3.27 |
| `V30 .. V35` | deviations | 7 |
| `THIRD_PARTY_DATA.md, worker-image/requirements.lock.txt, worker-image/wheelhouse/, tests/fixtures/stream_limit_synthetic.jsonl, fake_pkg memory_system.py and agents/foreign_class.py` | files | 1, 5.1, 6 |

**Choices made where the inputs were silent or disagreed.**

- `reply_reuse_scope` "gains `repeat`": applied as a scope **kind** that every job with a repeat index uses (as qualification jobs always use their own), not as a third manifest value. One campaign holds ordinary jobs and noise repeats, and the replay file refuses a change of the campaign scope.
- The restricted allow list is exactly the three rules the decision names. The skill's Step 0 writes post-eval reports under `LOGS/reports/` (`SKILL.md` L46-L48), which is therefore denied in mode `restricted`. Applied as decided: the addendum says to skip that step, V32 states it, and `proposer.restricted.settings` can add the rule. The lead may want to revisit this.
- PLAN L430 says "the three `CLAUDE_CODE_*` and `DISABLE_AUTOUPDATER` names"; its own table (L427-L428) has two `CLAUDE_CODE_*` names and `DISABLE_AUTOUPDATER`. The table was followed.
- The proposer-runtime sketch (Q3) has a blanket `Read(//<STATE>/**)` deny, a settings path `settings.iter<N>.json` and a scratch under `/private/tmp/mh-proposer`. Per the PLAN: one deny per child of `STATE` except `workspace`; the file is `<session dir>/settings.json`; the scratch root is the manifest field `proposer.scratch_root` (default `/private/tmp/mhf-proposer`).
- The sketch assumed the venv inside the working directory. Here the example venv is outside the workspace, so `allowRead` also names the example venv root and `uv`'s directory [live: PF9].
- PLAN step 7 runs the six baseline validation jobs unpublished in phase `qualification`; calibration needs published baseline results. PF7 therefore runs `Driver.run_baselines()` in the preflight campaign (phase `baseline`, published there, repeat 1 of the noise run). The 30 USPTO zero-shot calls are paid twice (about one cent).
- PLAN L185 puts the gates after the preflight; decision F1(c) puts G1 directly after qualification. Applied: `calibration_gate` is part of PF7 and judges G1 and G2 together; the noise run stays outside the preflight.
- C2 names the inputs `dataset` and `example_id`. `split` was kept as an input column as well (it was one before, and the table key already fixes it).
- K3 against auth mode `wandb_auth` (`WandbAuth` takes no key): the adapter sets `WANDB_API_KEY` inside its own process from the key it was given (critic2 L71); the rule that `sandbox.auth_env` must be `WANDB_API_KEY` in that mode is gone.
- Renamed values: `decisions.key_provisioning` `dedicated_env` to `dedicated` (keys are no longer necessarily in the environment); `decisions.dataset_text_publication` `full_text` to `per_dataset`.
- P6 "a violation halts": the `init` checks halt in every mode. For the tree, a pinned-file or guarded-log change halts in every mode; a modified existing agent file or a stray entry halts in modes `restricted` and `contained` and is restored or quarantined and recorded in mode `upstream`, where nothing denied it.
- S6: `1.1.1.1:443` gates, and the DNS lookup of the inference host was kept as a second gate because it tests `dns_egress="deny"`; the five literal-address probes are recorded only.
- A session with no `result` event that was not timed out is `transient` (bounded rerun, then stop and ask), not a settled failure.
- Text patterns never lead to a wait; a 402 or 429 status without a recognised structure is `spend_limit`.
- Where the proposer-runtime report and its verification disagree the verification was followed: `init.effort` is documented and asserted; "fail closed" is stated for writes and recognised outside reads only; the limit text is `usage limit` on usage-billed organisations; `VIRTUAL_ENV` is set.
- The reconciliation gate can be waived with a note when the user's role cannot read the platform figure; the rehearsal (PF11) is recommended, not required, for a qualification import.
- `evaluate --noise` and `Driver.run_noise` were added so that the noise-floor plan of Q1 is runnable; the sampling-control runner, a Best-of-N arm, `snapshot` and `install_then_deny`, and the offline amd64 container run are deferred in 8.3.
- Not taken from critic2: copying the CLI bundle to a path the run controls (the hash check still stops the search if the desktop app removes it), and a list of host-side installs for PF0.

## Appendix G. Document text fixed by the second round (for `L0-docs`)

The owner of `protocol.md` and `THIRD_PARTY_DATA.md` copies the text below. Square brackets are left for the user. Nothing that a research report marks as internal goes into any file under `SC`: no account detail, no company policy, no internal source.

### G.1 `protocol.md`

The file starts with the line `<!-- TEMPLATE-UNFILLED -->`. Its last section, "Stage 2", starts with the line `<!-- STAGE2-UNSIGNED -->`. The user removes each marker when signing that stage.

**Claim.**

> We ran the Meta-Harness text-classification example as released (upstream `f805ad3`), with gpt-oss-120b on CoreWeave Serverless Inference and Claude Code [CLI version, resolved model] as the proposer. Starting from the two released baselines, [N] iterations of three candidates. The harness chosen on validation scored [X]% on the held-out test sets, against [Y]% for the released few-shot baseline and [Z]% zero-shot in the same run. The paper reports 48.6% for its selected harness; an independent three-search re-run of the released code reports 48.6 ± 0.8%. Ours is one search.

**Claims not made.**

| Claim | Why not |
|---|---|
| "Reproduces the paper" | A different serving stack, a different proposer model unless pinned, three candidates per iteration instead of two, two starting harnesses instead of four, a different few-shot baseline, one search |
| Any comparison with ACE or MCE | Neither is in the release; the paper's rows for them come from an unpublished reproduction |
| "Four times fewer context tokens" | That ratio is against ACE. Against the released few-shot baseline (22.9 K characters) a harness near the paper's 45.5 K uses about twice as much. Report characters, characters divided by four, and real tokens (the `context` figures of the iteration record) |
| Anything about trace access mattering | The released default logs hold no per-example record |
| "Matches the paper's learning curve" | The paper's search-set scale is not the released validation scale |
| Wall-clock parity | "A few hours" is said of search runs in general, on a local server |
| Out-of-distribution transfer | Only if the nine datasets are run for our own harness |

Expect the best validation score to overstate test by several points (the paper: 56.7 on the search set, 48.6 on test). That gap is the selection effect.

**Fixed before the run.**

- Selection rule: the highest validation macro average among candidates complete on all three datasets; ties go to the lower context, then to the earlier iteration. Report validation and test for the selected harness, for the whole validation frontier and for the top five. Test scores for all candidates are a labelled post-hoc analysis only.
- Candidate count: the release's 20 iterations of 3. Also report the best so far at 39 or 40 evaluations.
- Reported effect: the test difference between the selected harness and the released few-shot baseline, with a paired bootstrap interval over the 412 test examples.
- Loaded-class check: a candidate is invalid if the class the loader picks is not defined in its own module.

**Calibration gates.** Run with `evaluate --calibration` in a campaign the proposer never sees. Upstream's test path needs `memory.json` from a validation run first, so each gate costs its validation pass too.

| Gate | What | Calls | Pass condition |
|---|---|---|---|
| G0, free | Phase-0 validation counts of both baselines against the references below | already run | record only; the bands are too wide to gate on |
| **G1, hard** | `no_memory` on the test split | 130 + 412 | macro average 23.4 to 33.3; Symptom2Disease 118 to 156 of 212; USPTO at most 21 of 100; LawBench at most 15 of 100; every call returned content (no empty reply, no `finish_reason: length`, no unparsed answer) |
| G2, soft | released `fewshot_all` on the test split | 130 + 412 | expect macro 35.9 to 47.3; Symptom2Disease 166 to 194; LawBench 13 to 37; USPTO 6 to 24. A flag, not a stop |

Reference values:

| Baseline | Source | Validation (USPTO, S2D, Law) | Test (USPTO, S2D, Law) | Test average |
|---|---|---|---|---|
| Zero-shot | the paper | 2/30, 27/50, 2/50 | 12/100, 134/212, 7/100 | 27.4 |
| Zero-shot | an independent re-run of the release (arXiv:2608.20169) | 1/30, 31/50, 3/50 | 13/100, 140/212, 9/100 | 29.3 |
| Few-shot, released code | the same re-run | 4/30, 40/50, 11/50 | 15/100, 180/212, 25/100 | 41.6 |

- Why zero-shot: it is the only baseline whose configuration is the same in the paper and in the release (191 added characters on every example).
- No gate uses the paper's few-shot figure. The paper's few-shot (all) carries 49.3 K characters; the released one carries 22.9 K.
- The bands are 1.96 standard errors of a difference between two independent runs. They catch gross faults only. After the noise run, flag any result more than 3 points from both references.
- G1 is mostly a Symptom2Disease check. G2 is the one that exercises 24,000-token Chinese prompts, and it rests on one preprint whose serving stack is not stated.

**Noise floor.** One USPTO validation example moves the macro average by 1.11 points, one Symptom2Disease or LawBench example by 0.67.

| Layer | Question | How | Calls |
|---|---|---|---|
| 1. Rerun noise | Same harness, same examples, same answers? | Both baselines on validation 5 times, a fresh cache and gateway replay scoped to the repeat (`evaluate --noise`). Per-example agreement, flip rate per dataset, standard deviation of the macro average. Repeat 1 doubles as the gates' validation pass | 1,300 |
| 2. Sampling noise | How far could this score move with other examples? | No calls. Bootstrap within each dataset; paired bootstrap and exact McNemar for each candidate against the incumbent | 0 |
| 3. Selection noise | How much of the winner's lead is luck over 60 tries? | The predeclared selection rule | the test calls of the reported systems |
| 4. Search noise | Would a second search land in the same place? | One search is one sample. Quote the independent re-run's spread (± 0.8 on the test average, ± 3.8 on LawBench) and say ours is single | 0 |

- The sampling standard error of one validation macro average is 2.9 to 3.9 points; on test about 2.2.
- A single paired comparison on validation needs a net lead of 6-0, 8-1 or 10-2 to pass at 0.05; corrected over 60 candidates, 12-0. Validation differences of a few points between candidates are not evidence.
- If layer 1 shows identical answers on every repeat, say the solver was repeatable at temperature 0 on the day and drop it from the error budget.

**Matched-budget control.**

| Control | What it answers | Decision |
|---|---|---|
| The initial harnesses at one call per example on test | The floor | always (it is G1 and G2) |
| Parallel sampling: `fewshot_all` with K = 5 samples per test example at a non-zero temperature, majority vote | Would spending the solver budget on more samples do as well? | recommended; 2,060 calls. Its runner is deferred (8.3). State that the vote is our adaptation and that K = 5 is more generous than the selected harness's calls per prediction; report tokens per example for both |
| Best-of-N: N independent proposer sessions that see only the seed baselines and their scores | Does the search structure matter? | only if the article wants that claim; it doubles the proposer bill |

Budget accounting in the article: count candidate evaluations and solver tokens; report proposer cost separately.

**Fidelity table A: the paper against the released example** (not this build's doing).

| # | Aspect | Paper | Release at `f805ad3` |
|---|---|---|---|
| A1 | Solver serving | not stated in the paper; the README: a local server, a 32,768-token window | OpenRouter by default (`config.yaml`) |
| A2 | Output budget and prompt limit | unknown | 16,384 output tokens for gpt-oss. Against the README's server that leaves 16,384 prompt tokens; the released LawBench few-shot prompt is 24,690 tokens on average and would not fit |
| A3 | Proposer | Claude Code, Opus 4.6, maximum reasoning; CLI version not given | alias `opus`, effort `max`; CLI unpinned |
| A4 | Candidates per iteration | two | the skill demands three |
| A5 | Starting population | zero-shot, few-shot, ACE, MCE | `no_memory`, `fewshot_all` |
| A6 | Few-shot baseline | about 1,000 characters per demonstration, 49.3 K for "all" | raw-question demonstrations, a 30,000-character cap, 22.9 K |
| A7 | Zero-shot baseline | 191 added characters | the same |
| A8 | Epochs | not stated | 1; the config comment says "was 5" |
| A9 | Mode | not stated | offline: the label is passed as the prediction and every example marked correct, so error-driven designs are unreachable without extra model calls |
| A10 | Proposer evidence | "execution traces" | no per-example record in the configured mode |
| A11 | Selection | test on the Pareto frontier | consistent; but the paper's figures show a test score for every candidate |
| A12 | Variance | none; one search | none |
| A13 | Test data | not stated | sits in the proposer's workspace |
| A14 | Data | not stated | one Symptom2Disease question is in both validation and test |

**Fidelity table B: the released example against this run.** The table of section 7 (V1 to V35), copied.

**Dataset attribution** (for the article):

> Datasets. The run uses the three datasets shipped with the Meta-Harness text-classification example (Lee et al., 2026; MIT), which took them, with the task prompt templates, from the Meta Context Engineering artifact repository (Ye et al., 2026; MIT as declared in its README). Symptom2Disease: 462 examples from Gretel AI's `symptom_to_diagnosis` dataset, licensed under Apache-2.0 and adapted from the Kaggle dataset Symptom2Disease by Niyar R Barman (CC0); the MCE authors sampled 200 training and 50 validation examples from the original training split and renamed the fields, and the test split is unchanged. LawBench: 350 examples of task 3-3, charge prediction (Fei et al., EMNLP 2024), derived from CAIL2018 (Xiao et al., 2018; Zhong et al., 2018); this work uses the CAIL2018 dataset, whose case descriptions come from criminal judgments published on China Judgments Online. USPTO-50k: 180 reactions from the set of Schneider, Stiefl and Landrum (J. Chem. Inf. Model. 2016, 56, 2336-2346), built on Daniel Lowe's reactions text-mined from United States patents (released under CC0 in 2014); source of the underlying documents: United States Patent and Trademark Office. The public project shows example text for Symptom2Disease only. For LawBench and USPTO-50k it shows example identifiers, SHA-256 hashes of the inputs, labels, predictions and scores.

Where Symptom2Disease text is shown, add the Apache-2.0 notice with the licence link and the statement of changes. (With the default rules no dataset's text is shown; the sentence "shows example text for Symptom2Disease only" is used only after the user has opened Symptom2Disease, and is otherwise replaced by "shows no example text".)

**Stage 2** (`<!-- STAGE2-UNSIGNED -->`; signed after qualification and the first real proposer session): the job limit [`executor.job_timeout_s`] and what was decided about learning-heavy candidates; dataset-job parallelism [`executor.parallel_dataset_jobs`]; the proposer model, the resolved model id and the CLI version; the per-session cap and the campaign ceiling; the measured noise floor.

### G.2 `THIRD_PARTY_DATA.md`

**Licence position.** This is a reading of public sources, not legal advice.

| Dataset | Source and licence | Publishing example text |
|---|---|---|
| Symptom2Disease (462 rows) | `gretelai/symptom_to_diagnosis`, Apache-2.0; adapted from a CC0 Kaggle dataset | permitted with an Apache-2.0 notice and a statement of changes |
| USPTO (180 rows) | USPTO-50k (Schneider et al. 2016); its supporting information is labelled CC BY-NC 4.0; Lowe's underlying extraction is CC0; patent text is not copyright-restricted | not published until the licence label has been cleared; hashes only |
| LawBench (350 rows) | LawBench task 3-3 (an Apache-2.0 repository that defers to each source), from CAIL2018 (no data licence), from China Judgments Online | **not cleared; the case text is never published.** The rows are real criminal cases; a pattern count found nine rows with what looks like a complete plate number and one with an unmasked name |

The repository the example took the data from (`metaevo-ai/mce-artifact`) has no LICENSE file but declares MIT in its README and in its `pyproject.toml`. That covers its authors' code and prompt templates, not the data.

**The publication rule this package enforces.**

1. One content rule set per dataset, applied to prompts, replies, reasoning and proposer tool results. The default is `hash` for all three. The user may open Symptom2Disease after signing off. USPTO opens only after clearance. LawBench never opens.
2. The rule binds at export. `export --live` refuses while the publication decision is unset. Loosening later is a re-export under new conversation ids.
3. `log.jsonl` is treated exactly like `memory.json`: its last checkpoint row is the candidate's whole memory state, which for `fewshot_all` is the training split verbatim.
4. Tables carry `dataset`, `example_id` and `question_sha256` as inputs; `prediction`, `was_correct` and the metrics; `target` for Symptom2Disease and LawBench (category names); for USPTO the hashes of prediction and target.
5. Artifacts are candidate files only, after a verbatim-text scan; for USPTO the scan matches whole product and target strings exactly. No `memory.json`, `log.jsonl`, reply cache or transcript is ever uploaded; their hash and size are logged.
6. The scan also covers Artifact metadata and outer-loop span arguments.
7. Screenshots and exported spans for the article come from Symptom2Disease conversations only.
8. A private project is readable by every member of its team. Full LawBench text does not go into the tracking service even privately.

**Data files.** A table of the SHA-256 of the nine files `data/{uspto,symptom_diagnosis,crime_prediction}/{train,val,test}.jsonl` at `f805ad3`, computed by the owner from the pinned example.

**Attribution.** The dataset paragraph of G.1.

Open questions for a lawyer (the user's to ask; this package sends nothing): the CC BY-NC label on the 180 USPTO rows; LawBench case text; whether a licence link meets Apache-2.0 section 4(a) on a web page.


Export recovery revision (2026-11-10): `export --retry-budget` explicitly retries a `skipped_budget` unit as a new export attempt. The old skipped receipt is archived. Agent Lens attempt2+ uses the existing recovery bucket, and total/bucket caps are checked before any upload. It does not raise limits, change content policy, retry refused-secret/policy receipts, or resend verified units. The flag is separate from `--retry-partial`; budget deferrals remain terminal without this explicit request. No solver calls or rescoring are involved.
