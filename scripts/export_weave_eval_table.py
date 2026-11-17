#!/usr/bin/env python3
"""Log saved Meta-Harness Controls & Engineering benchmark rows as native Weave-backed
`wandb.EvalTable` runs in `wandb/mh-forge-preflight-20261110` (zero model calls).

Note: On Nov 10, 2026, passing the unreleased `backend="ces"` flag returned HTTP 500
(`showcase/validation/ces-live-failure-20261110.json`). Using the shipped default
`wandb.EvalTable` (`backend="weave"`) logs side-by-side EvalTables directly into the
shared Forge project.
"""
from __future__ import annotations

import argparse
import csv
import json
import tempfile
from pathlib import Path

import wandb

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTROLS_TEST_CSV = REPO_ROOT / "showcase" / "articles" / "meta-harness-controls" / "evidence" / "test-scores.csv"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="wandb/mh-forge-preflight-20261110")
    args = parser.parse_args()

    ent, proj = args.project.split("/", 1)
    with CONTROLS_TEST_CSV.open() as f:
        rows = list(csv.DictReader(f))

    arms = [
        ("direct-zero-shot", "direct_correct"),
        ("retrieved-fewshot", "examples_correct"),
        ("retrieved-vote-5", "vote_correct"),
        ("retrieved-sample-1", "first_sample_correct"),
    ]
    columns = ["dataset", "task", "overlap_flag", "correct", "correct_samples_of_5", "vote_count", "tied"]
    urls: dict[str, str] = {}
    with tempfile.TemporaryDirectory() as tmpdir:
        for arm_name, col_key in arms:
            table_rows = [
                [
                    r["dataset"],
                    int(r["task"]),
                    bool(int(r["overlap"])),
                    bool(int(r[col_key])),
                    int(r["correct_samples"]),
                    int(r["vote_count"]),
                    bool(int(r["tied"])),
                ]
                for r in rows
            ]
            with wandb.init(
                entity=ent,
                project=proj,
                name=f"mh-controls-eval-{arm_name}",
                job_type="controls-eval-table",
                dir=tmpdir,
                config={"arm": arm_name, "tasks": len(table_rows), "backend": "weave"},
            ) as run:
                et = wandb.EvalTable(
                    columns=columns,
                    input_columns=["dataset", "task", "overlap_flag"],
                    score_columns=["correct"],
                    data=table_rows,
                )
                run.log({"meta_harness_controls_test": et})
                urls[arm_name] = run.url

    out_path = REPO_ROOT / "showcase" / "validation" / "weave-eval-table-export-20261116.json"
    payload = {
        "project": args.project,
        "backend": "weave",
        "rows_per_arm": len(rows),
        "arms_logged": len(arms),
        "total_cells": len(rows) * len(arms),
        "eval_table_run_urls": urls,
    }
    out_path.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps(payload, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
