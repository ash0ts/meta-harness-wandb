# Context and sampling controls · completed 2026-11-10 local time

The frozen follow-up completed 72 jobs: 520 preparation outputs and 8,240 test outputs on 412 previously inspected released tasks. All 74 export units were verified (73 Agent Lens job attempts and one W&B study); the native table has 1,648 task/arm rows. Fixed reviewed workers ran locally with the macOS loopback network guard. Forge handled inference and private telemetry. No cloud isolation, untouched evaluation or optimized-harness result is claimed.

Common instructions at temperature zero gave mean dataset accuracy of 30.96% with zero examples, 33.57% with five, and 41.09% with the larger capped pool. Large minus zero is +10.13 percentage points [7.39, 13.01]. At fixed temperature zero, the first one/three/five answers scored 40.92%/42.40%/43.53%; five minus one is +2.61 points [0.61, 4.79], at roughly five times the inference cost. Temperature one is a separate supporting arm.

The 95% intervals are pointwise paired task bootstraps within datasets, with all five draws for a task kept together. They condition on saved outputs, exclude future model rerun and search uncertainty, and have no multiplicity adjustment. Context scores average five individual answers; sampling scores use the predetermined first one/three/five draws with plurality and earliest-sample tie breaking. All five draws were executed and paid for; prefix costs are attributed estimates.

The numeric ledger retains all 8,826 provider attempts: 8,760 complete, 65 with bounded unknown usage, and one rejected. Completed-follow-up inference was $2.714997 observed plus at most $0.201578 unknown, excluding local hardware, telemetry and development. Earlier B/D preparation costs remain separate in engineering-aggregates.json. A failed job was recovered by reusing its 97 completed responses and requesting only the three missing outcomes. Recovery source identity is checked by the analyzer; no cross-arm or cross-draw response reuse occurred.

## Reproduce

From showcase/, run `python articles/meta-harness-controls/evidence/reproduce.py` for the article's earlier and new estimates: 23 paired intervals and every follow-up provider-attempt cost. This uses the standard library, without inference, network access or private text. The article's analysis.zip contains the same allowlisted evidence and reproduction scripts. Public rows cannot independently regrade the withheld answer text.

For an audit using authorized private state, run `analyze_engineering.py --state STATE --output PRIVATE_ANALYSIS`, then `export_engineering_download.py --state STATE --analysis PRIVATE_ANALYSIS --metrics-receipt PRIVATE_NATIVE_READBACK --output SAFE_DOWNLOAD`. The latter verifies campaign lineage and exports only the fixed score schema, numeric usage and allowlisted methods/aggregates. Never copy the private analysis directory wholesale. Immutable raw attempts, exact requests, answers and private trace locators remain in protected campaign state.

The runtime manifest, source, grader and prompt identities are recorded in verification.json. The code was frozen before measured tests; post-run changes resolve replay provenance and recover/verify telemetry without changing scores or estimators. Completion, transport, export-budget, failed-usage and native-metric receipts are versioned under validation/.
