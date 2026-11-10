# Harness controls: calculation download

Run `python3 reproduce.py` in this directory. Only the Python standard library is required; no network calls or model requests are made.

`test-scores.csv` contains the same 412 paired test outcomes. `task` is a one-based ordinal inside the fixed evaluation order; it is not a source-dataset ID. `overlap` marks the one exact validation/test overlap. Columns record native scorer outcomes, not semantic validity. `correct_samples` and `vote_count` are retained measurements from the five saved outputs. Their derivation requires the protected outputs; this download can recompute their aggregates but cannot independently regrade answers.

`validation-scores.csv` contains five repeats of 130 tasks under two baselines (1,300 rows). `normalized_label_changed` and `identical_requests` are verified measurements from the protected request/output audit; no request or label hashes are included here.

The script uses the same paired stratified bootstrap as the saved analysis: 10,000 draws, seed 20261110, equal dataset weights, percentile interval with linear interpolation. The intervals condition on the fixed datasets and saved model outputs. They exclude model resampling and search uncertainty.

`aggregates.json` preserves cost scopes and the secondary comparison with the first temperature-one sample. Prices were frozen at $0.03 per million input tokens (including cache) and $0.17 per million output tokens. Costs are marginal test inference estimates, not invoices or total project spending. Complete usage and bounded unknowns remain separate.

These files contain no raw task text, target labels, responses, private traces, credentials or filesystem locations. Full source records, exact scorer versions, host regrading, telemetry readbacks and failed attempts are retained privately. No new inference or scoring was performed to create the download.
