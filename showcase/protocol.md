# Forge showcase research contract

TEMPLATE-UNFILLED
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

Team and private preflight project; dedicated key provisioning; budget owner and service ceilings; proposer allowance/model/permission mode; package and image hashes; dataset notices; live-check scope and cleanup protocol. Replace TEMPLATE-UNFILLED only when those decisions are recorded in the campaign manifest and this contract.

## Stage 2 decisions after qualification

Record the actual receipts, calibration verdict, price reconciliation, resolved model, observed forecast, remaining budget, measured campaign/project identity and any deviations before removing STAGE2-UNSIGNED. This document is intentionally unsigned at the offline continuation checkpoint.

Reproduce the local integration with `python -m mhforge offline-smoke --executor local_relay --keep`. A measured result must also retain its exact manifest, engine snapshot, journal, attempts, export receipts and research revision.
