# Engineering controls research contract

Frozen before new live calls. User authorized article completion, useful experiments and budget on 2026-11-10. Campaign mh-engineering-c-20261110 is a prospective follow-up on previously inspected released test data, not an untouched transfer evaluation or an optimization result.

## Questions and conditions

How much does context size buy under common instructions? Compare 0, 5 and all available training examples, with a 30,000-character example-section ceiling. Each input deterministically shuffles the same training examples using its SHA-256 seed; five examples are a prefix of the full ordering. All arms use identical neutral instructions and JSON answer conventions. Prompt size is the treatment; tokens are measured, not held equal. This replaces the earlier two different prompt templates.

How much does additional inference buy? For full context, compare the first 1, 3 and 5 answers using plurality over the pinned evaluator's answer normalization. Ties select the earliest answer. Temperature zero is primary; temperature one is a supporting comparison. Sample indices are fixed before execution, never selected by score.

Four arms: context_0, context_5, context_all (temperature 0) and context_all_t1 (temperature 1). Each generates five independently identified fresh responses per task. Context comparisons average all five correctness outcomes per task; sample-budget comparisons use nested candidate pools. No response reuse across samples, arms or campaigns; retries may replay completed calls within their original sample. No API seed is set. Loader seed remains 42.

## Population and evidence

Pinned Meta-Harness source f805ad3aa4ada290458c1e0deadbe132fbbdea4e; unchanged loaders and graders. Training 50/200/200 and test 100/212/100 for USPTO/Symptom2Disease/LawBench: 412 test tasks, 8,240 test outputs, plus 520 validation preparation outputs. Validation creates and verifies training memory; all arms run regardless of score. The known validation/test duplicate stays in the primary result, with secondary exclusion of mapped Symptom2Disease evaluation index 11.

Primary metric: exact native correctness with equal dataset weights. JSON validity and empty answers are separate diagnostics. No chemistry, clinical, legal or coding-competence claim. One model/provider configuration and previously exposed tasks. Request/source hashes, candidate files, source-row identity, outputs, native regrading and job/call identities must reconcile.

## Order, uncertainty and cost

One sandbox job at a time, gateway concurrency 16, unchanged upstream workers. Randomize all 60 test jobs with seed 20261111 to spread conditions through time. Preparation happens first. Pair tasks across conditions; bootstrap tasks within datasets, keeping each task's five draws together, 10,000 resamples seed 20261111. Report draw dispersion separately. Repeats are not independent new tasks. No significance-driven stopping or arm selection.

Report observed tokens and frozen-price estimates, retaining bounded unknown usage and every retry. Report actual five-draw spend and prefix-attributed 1/3/5 estimates separately; unused saved draws were still paid for. Marginal inference excludes sandbox, telemetry and development; campaign totals are separate. Individual request durations and queue delay are descriptive. Separate sample batches cannot establish deployed 1/3/5-policy tail latency.

Ceilings: $100 solver overall, $30 controls phase, $40 sandbox; no proposer execution. These are protective limits, not expected charges. Previous usage suggests single-digit solver dollars, but new output lengths may differ. Reuse authorized account/project and secure credential source; no public publication.

## Failures, instrumentation and completion

Use the existing sandbox, gateway, replay ledger, sidecar, budgets and Agent Lens path. Preserve scored failures and transport attempts. Stop incomplete jobs; resume through validated evidence. No condition changes after results appear. Stop on source/engine drift, unknown write outcomes or unproven cleanup. Native CES previously failed; explicitly use W&B Tables from campaign creation and read back rows and metrics. Selected diagnostics never qualify proposer search.

Acceptance: 72 complete jobs (12 preparation,60 test), 8,760 scored outcomes; traces and aggregate tables verified remotely; scores regraded; task/sample identity verified; accounting audit clean; two empty sandbox listings; portable score-only reproduction bundle; canonical article updated and Git commit reviewed. Retain and explain partial evidence if any criterion cannot be met.

Pre-live revision: the inherited search-wide replay scope could share identical prompts between arms. Revision B freezes job-only replay. Original frozen manifest is retained as preparation evidence and performed no live inference.

Revision C: the full offline rehearsal exposed a resume-discovery defect. Upstream omits already-completed preparation jobs; the runner now verifies their saved evidence. B was halted during preparation before any measured test. All B attempts and cost remain retained. No scientific arm or setting changed.


## Execution amendment before measured tests

Campaign D (mh-engineering-d-20261110) completes the same scientific protocol with local relay workers because CoreWeave Sandboxes began rejecting list requests with PERMISSION_DENIED. C created no job, sandbox handle or inference call. The credential digest was unchanged; W&B project remained PRIVATE and writable, and the inference model-list endpoint returned HTTP200. No remote access restriction was bypassed.

D explicitly selects local execution of the fixed, reviewed candidates. The launcher is hash-bound in the frozen manifest and requires macOS sandbox-exec to deny worker network access outside loopback. It refuses proposer search and non-engineering diagnostics. This is a host process with a network guard, not cloud sandbox isolation. Provider calls and Agent Lens/W&B exports remain on the authorized Forge services. All compared D arms use the same worker environment; source, prompts, task splits, scoring, sample counts, temperature, gateway concurrency and randomized job order are unchanged. One local job runs at a time. Local hardware cost is not priced as cloud compute. The article must name this execution boundary.

## Transport amendment before measured tests

Campaign E (mh-engineering-e-20261110) raises the provider streaming-response byte ceiling from 2 MiB to 16 MiB. D preparation produced 289 complete provider calls and one stream exceeding the smaller ceiling; no test job ran. Its HTTP status was 200; the gateway generated the refusal after its own byte limit. The failed attempt has missing usage bounded by $0.003342; observed D inference was $0.053627. All seven D attempt traces were read back and its accounting audit passed with no open meters. D is retained as setup evidence. E starts fresh with no response reuse across campaigns. The 16,384-token generation ceiling, prompts, scorer, sample policy and job schedule are unchanged. This transport amendment was frozen before E calls and before any measured tests.
