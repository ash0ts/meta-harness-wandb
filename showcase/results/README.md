# Live evidence from 2026-11-10

These results cover frozen baselines and independent controls. No optimized-harness result or completed search is claimed. Exact request, output and grader records remain in private campaign state; portable analyses contain hashes and approved trace links.

- `calibration-20261110/`: 412 released test tasks per baseline, paired outcomes, the corrected overlap sensitivity, illustrative trace-to-score cases, inference costs and figures.
- `repeatability-20261110/`: five validation repeats of 130 unique tasks per baseline. The 1,300 outcomes are 260 task/system groups, not 1,300 independent tasks.
- `voting-20261110/`: 2,060 temperature-1 samples, 412 reconstructed votes, primary comparison with single few-shot and secondary diagnostics against sample one. The primary comparison changes temperature and sample count.
- `controls-study-20261110/`: reconciled comparison CSV, article evidence JSON and Forge read-back verification.

Reproduce the analyses with the scripts in `showcase/`, passing the original private campaign states. Run `analyze_campaign.py` with the frozen split audit and `--right-state` for the voting campaign, then `analyze_repeatability.py`, `analyze_voting.py` and `analyze_inference_cost.py`. Cost phases are `calibration` for the two baselines and `controls` for voting. Place their named JSON outputs in one directory and run `assemble_study_results.py --input-dir DIR --output-dir DIR`. Render figures with the separately pinned `requirements-figures.txt` environment. Source-file hashes are recorded inside the outputs; historical script versions are preserved by Git.

The official exact-label graders define these scores; they do not establish chemical, clinical or legal validity. Paired intervals condition on these tasks and runs, and exclude sampling and search uncertainty. Costs are usage-based estimates at frozen prices, not invoices or total project costs. Failed attempts and uncertain usage remain in the evidence. W&B Tables are an explicitly amended fallback after native CES HTTP 500 failures. API read-back passed; browser SSO app assignment prevented a fresh visual preview.
