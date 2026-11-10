# Forge showcase research contract

STAGE1-RECORDED-2026-11-10
STAGE2-UNSIGNED

## Question and scope

Can the pinned Meta-Harness loop run through Forge with complete accounting, recoverable execution and trace-to-result evidence while preserving the upstream task protocol? Treat the live qualification, smoke, calibration and measured campaign as separate attempts. This is an instrumentation study until measured runs and independent grading support stronger claims.

## Frozen inputs

Upstream: stanford-iris-lab/meta-harness at f805ad3aa4ada290458c1e0deadbe132fbbdea4e. The manifest pins source-tree and worker-lock hashes. The engine snapshot records executable Python files. Every dataset split retains upstream task membership and order. Model, temperature, effort, seed, parser, candidate budget and aggregation come from the frozen manifest. Paths resolve relative to that manifest.

The solver is openai/gpt-oss-120b with temperature 0 and the upstream Reasoning: medium system prompt. Baselines are no_memory and fewshot_all. Datasets and train/validation/test counts are USPTO 50/30/100, Symptom2Disease 200/50/212 and LawBench 200/50/100. A repeated occurrence or noise trial is not an independent new task.

## Measures

Task correctness: numerator is the number of final sidecar evaluations whose `was_correct` is true; denominator is the declared split size. Missing results contribute zero to the all-dataset macro but remain explicitly missing. Preserve upstream's reported average separately. Compare candidate improvement with the frontier before its proposal. Record provider prompt tokens separately from memory characters and the labelled characters/4 approximation.

Costs: observed inference usage, bounded unknown reservations, sandbox time, proposer list-price usage and telemetry meters remain separate. Missing usage is unknown, never free. Stub call counts demonstrate integration only. Inspect source task, output, judge input, criterion, final result, aggregation and exported value before making a quality claim.

## Calibration before search

G1 (hard, no_memory): macro test accuracy 23.4–33.3%; correct counts Symptom2Disease 118–156, USPTO 0–21, LawBench 0–15. Require content on every call and a nonempty prediction for each test example.

G2 (soft, fewshot_all): macro test accuracy 35.9–47.3%; correct counts Symptom2Disease 166–194, USPTO 6–24, LawBench 13–37. This is a gross-fault diagnostic against the released baseline, not a claim to match the paper's few-shot configuration.

Keep calibration results outside the proposer-visible logs. Reuse identical calibration attempts during final test scoring. Finalization permanently ends search for that campaign. Noise trials 2–5 have fresh job/cache identities; report their agreement without treating them as independent datasets.

## Stop, retry and privacy policy

Use manifest ceilings and the durable ledger. Stop for unknown spend, provider identity drift, failed isolation, failed cleanup, a hard calibration gate or exhausted infrastructure attempts. Preserve failed attempts. Resume only work proven not to have executed; unknown remote writes require readback. Reconcile at every search block boundary.

No raw dataset text is public by default. Export only guarded plans and allowlisted records. Never publish raw streams, call bodies, sidecars, transcripts, memory.json, log.jsonl, account facts, key digests or SQLite files. Stage publication privately, scan it and require explicit publication approval and project ownership evidence.

## Stage 1 decisions to record before any live qualification

Team and private preflight project; dedicated key provisioning; budget owner and service ceilings; proposer allowance/model/permission mode; package and image hashes; dataset notices; live-check scope and cleanup protocol. Replace STAGE1-RECORDED-2026-11-10 only when those decisions are recorded in the campaign manifest and this contract.

## Stage 2 decisions after qualification

Record the actual receipts, calibration verdict, price reconciliation, resolved model, observed forecast, remaining budget, measured campaign/project identity and any deviations before removing STAGE2-UNSIGNED. This document is intentionally unsigned at the offline continuation checkpoint.

Reproduce the local integration with `python -m mhforge offline-smoke --executor local_relay --keep`. A measured result must also retain its exact manifest, engine snapshot, journal, attempts, export receipts and research revision.

## Delegated stage 1 decisions, 2026-11-10

The user authorized useful article experiments, delegated the Forge team/project and service cap choices, and designated an existing local credential file. Use team `wandb` and private project `mh-forge-preflight-20261110`, whose PRIVATE visibility and write permission were read back. Credential provenance is `user_supplied`, not a claim of dedicated provisioning. Pass it only over the host descriptor channel; workers receive no user API key, and the proposer must pass restricted-mode access probes before search. The source credential file is explicitly denied in proposer settings.

Preserve ceilings of $60 solver inference and $40 sandbox compute. Proposer cost is a separate metered allowance of at most $25 per session and $300 for the full 20-iteration campaign; no proposal starts without a passing cost forecast, resolved-model check and permission proof. These are maximum reservations, not forecasts or reported costs. Stop on any service/account limit; do not change the account's billing limits. Qualification and calibration come first; no proposer spend is needed to diagnose those paths.

Use the pinned amd64 Python image and the deterministic CPython 3.13 wheelhouse recorded in the manifest. All dataset inputs, model settings and released parsers stay pinned. Export identifier/hash-only data to private Forge Agent Lens, CES and W&B; no public project or article publication is authorized here.

The planned article compares no_memory, released fewshot_all, validation-selected Meta-Harness candidates, and a K=5 temperature-1 fewshot_all majority-vote control. Resolve ties with the earliest sample among the tied labels. Keep sample identities independent, count all five calls, and judge votes with the released dataset evaluator. Baseline validation repeats 1–5 measure provider variation. Report task-paired gains/losses and stratified bootstrap uncertainty; five repeats of one task are not five independent tasks. Search never sees calibration/test outputs. Test only the validation-selected frontier after search is ended.

The paired analysis uses equal dataset weighting and 10,000 paired task bootstrap resamples within each dataset (seed 20261110), with percentile 95% intervals. The primary comparisons are the validation-selected harness versus released fewshot_all, and the five-vote control versus released fewshot_all. Report gains, losses, both-correct and shared-wrong counts. These intervals condition on the selected harness and fixed datasets; they do not capture search variability or repeated model-sampling uncertainty. Missing task outcomes stop this complete-case comparison instead of silently dropping tasks. `analyze_campaign.py` emits task hashes and evidence digests, never raw text.

PF6 requires pinned imports, egress denial, a 600-second synthetic relay soak (32 concurrent requests per batch, multibyte payloads, forced disconnect and stream rotation), automatic expiration, and two consecutive empty campaign sandbox lists. Synthetic relay traffic is not model evaluation. Every failed check remains an attempt. Each code revision gets a new engine digest and offline receipt before live use. Stage 2 remains unsigned until observed qualification, calibration, export readback and spend evidence support the measured campaign.

Observed sandbox limitation: the platform mounts an identity-token file. The probe records its presence without reading its contents. All tested external, DNS, metadata, cluster and IPv6 network paths were blocked. Do not describe this as a credential-free filesystem or infer untested isolation properties. The public Python image also sets a release-verification GPG fingerprint; the runner clears that unused variable and retains the general credential-environment check.

Before live quality results, exact question-plus-instruction audit found one validation/test overlap in Symptom2Disease and none across the three splits of USPTO or LawBench. Preserve the released 412-task primary score; additionally report the predeclared sensitivity excluding the identified test occurrence, with all three datasets still equally weighted. The hash-only audit and file digests are in `validation/dataset-splits-20261110.json`. This is exact overlap detection, not a semantic near-duplicate search.

Continuation B, before further live baselines: preserve the original campaign with its non-monotonic journal as failed instrumentation evidence. A fresh campaign uses file-locked journal allocation and the observed response-model ID. The full 20-iteration, three-candidate design remains intact. Set the separate proposer ceiling to $400 to cover the $375 forecast (30 possible sessions at $10 estimated list cost, with 25% contingency); the per-session cap remains $25. The $60 inference and $40 sandbox service ceilings are unchanged; prior diagnostic spend is reported separately. This changes a delegated campaign planning ceiling, never the account billing settings. Stage 2 remains unsigned.
