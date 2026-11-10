# SPDX-License-Identifier: Apache-2.0
# Adapted from ash0ts/harness2-wandb@31b0f82 harness2/showcase/qualification.py L23-L68 and v4.py L175-L352 (the restart
# rule, the engine snapshot and the forecast they test). Changes: rewritten as unittest cases over temp state trees; offline gate, import,
# noise and calibration-verdict cases added.
"""Tests for mhforge.qualification (spec 5.2 and LD3)."""

from __future__ import annotations

import json
import math
import sqlite3
import unittest
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

import mhforge
from mhforge import ledger as ledger_mod
from mhforge import manifest as manifest_mod
from mhforge import qualification as q
from mhforge import util, workspace
from mhforge.errors import CampaignStop
from mhforge.executors import sandbox_cost_micro
from mhforge.ledger import Ledger
from tests import helpers

PACKAGE = Path(mhforge.__file__).resolve().parent
ZEROS = "0" * 64
_saved_fullfsync = None


def setUpModule():                                 # durability pragma only (test_ledger checks it); keeps tests fast
    global _saved_fullfsync
    _saved_fullfsync = ledger_mod.FULLFSYNC
    ledger_mod.FULLFSYNC = False


def tearDownModule():
    ledger_mod.FULLFSYNC = _saved_fullfsync


def fake_example(root: Path) -> Path:
    """A synthetic example directory holding the 39 pinned files."""
    ex = root / "example"
    for rel in workspace.PINNED_FILES:
        (ex / rel).parent.mkdir(parents=True, exist_ok=True)
        (ex / rel).write_text(f"SYNTHETIC {rel}\n", "utf-8")
    return ex


def write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")
    return path


class Gate:
    """A temp SC in which every check of the offline gate passes (sandbox executor in wheelhouse mode)."""

    def __init__(self, tc: unittest.TestCase, **overrides) -> None:
        ctx = helpers.tmp_state()
        st = ctx.__enter__()
        tc.addCleanup(ctx.__exit__, None, None, None)
        self.sc, self.paths = st.sc, st.paths
        self.ex = fake_example(self.sc.parent)
        lock = write_text(self.sc / "worker-image" / "uv.lock", "SYNTHETIC lock\n")
        req = write_text(self.sc / "worker-image" / "requirements.lock.txt", "SYNTHETIC requirements\n")
        tar = write_text(self.sc / "worker-image" / "wheelhouse" / "wheelhouse.tar", "SYNTHETIC tar\n")
        cli = write_text(self.sc.parent / "bin" / "claude", "SYNTHETIC cli\n")
        write_text(self.sc / "protocol.md", "# Protocol\n\nSigned.\n\n## Stage 2\n\nSigned.\n")
        util.write_json(self.sc / "state" / "_smoke" / "receipt.json",
                        {"schema": "mhforge.smoke/1", "passed": True,
                         "engine_sha256": q.engine_snapshot(PACKAGE)["engine_sha256"]})
        base = {"upstream.example_dir": str(self.ex), "upstream.tree_sha256": workspace.source_digest(self.ex)[0],
                "upstream.worker_lock_sha256": util.sha256_file(lock), "executor.kind": "sandbox",
                "sandbox.env_mode": "wheelhouse", "sandbox.wheelhouse.tar_sha256": util.sha256_file(tar),
                "sandbox.wheelhouse.tar_bytes": tar.stat().st_size, "sandbox.wheelhouse.sha256sums_sha256": ZEROS,
                "sandbox.wheelhouse.requirements_sha256": util.sha256_file(req),
                "proposer.cli_path": str(cli), "proposer.cli_sha256": util.sha256_file(cli)}
        base.update(overrides)
        self.manifest = helpers.make_manifest(base)
        self.freeze()

    def freeze(self) -> None:
        util.write_json(self.paths.manifest, self.manifest)

    def run(self, command: str = "search") -> dict:
        return {g["name"]: g for g in q.offline_gate(self.manifest, self.paths, self.sc, command=command)}

    def failed(self, command: str = "search") -> list[str]:
        return [n for n, g in self.run(command).items() if not g["ok"]]


def add_unknown_row(paths, manifest) -> None:
    Ledger(paths.ledger, manifest)
    conn = sqlite3.connect(paths.ledger)
    try:
        conn.execute("INSERT INTO calls (id, call_id, n, job_id, job_key, phase, origin_phase, model, input_limit, "
                     "output_limit, reserved_micro, status, created_utc) VALUES ('l-x', 'c-x', 1, 'qual.val.no_memory."
                     "USPTO.a1', 'qual.val.no_memory.USPTO', 'qualification', 'qualification', 'openai/gpt-oss-120b', "
                     "100, 100, 10, 'unknown', '2026-11-10T00:00:00.000000Z')")
        conn.commit()
    finally:
        conn.close()


# ================================================================ engine and gate

class EngineSnapshot(unittest.TestCase):
    def test_engine_snapshot_scope(self):
        snap = q.engine_snapshot(PACKAGE)
        files = snap["files"]
        for included in ("mhforge/qualification.py", "mhforge/gateway.py", "mhforge/ledger.py",
                         "mhforge/insandbox/mhf_frames.py", "mhforge/executors/__init__.py",
                         "mhforge/telemetry/transcript.py"):
            self.assertIn(included, files)
        for excluded in ("mhforge/audit.py", "mhforge/publish.py", "mhforge/telemetry/join.py",
                         "mhforge/telemetry/policy.py", "mhforge/telemetry/__init__.py"):
            self.assertNotIn(excluded, files)
        self.assertTrue(all(k.endswith(".py") for k in files))
        self.assertEqual(snap["engine_sha256"], util.tree_digest(files))
        with helpers.tmp_state() as st:
            pkg = st.sc / "mhforge"
            for rel in ("__init__.py", "gateway.py", "audit.py", "publish.py", "sub/x.py", "telemetry/__init__.py",
                        "telemetry/join.py", "telemetry/transcript.py", "notes.txt", "__pycache__/gateway.py"):
                write_text(pkg / rel, f"# {rel}\n")
            snap = q.engine_snapshot(pkg)
            self.assertEqual(sorted(snap["files"]), ["mhforge/__init__.py", "mhforge/gateway.py", "mhforge/sub/x.py",
                                                     "mhforge/telemetry/transcript.py"])
            h = snap["engine_sha256"]
            for rel in ("audit.py", "publish.py", "telemetry/join.py", "notes.txt"):
                write_text(pkg / rel, "# changed\n")
                self.assertEqual(q.engine_snapshot(pkg)["engine_sha256"], h, rel)
            for rel in ("gateway.py", "telemetry/transcript.py", "sub/x.py"):
                write_text(pkg / rel, f"# changed {rel}\n")
                self.assertNotEqual(q.engine_snapshot(pkg)["engine_sha256"], h, rel)
                h = q.engine_snapshot(pkg)["engine_sha256"]


class OfflineGate(unittest.TestCase):
    def test_all_checks_pass_in_a_prepared_tree(self):
        g = Gate(self)
        gates = q.offline_gate(g.manifest, g.paths, g.sc, command="search")
        self.assertEqual([x["name"] for x in gates],
                         ["manifest_frozen", "upstream_source", "workspace_pinned", "worker_lock", "smoke_receipt",
                          "proposer_cli", "ledger_unknown", "protocol", "wheelhouse_tar", "requirements_lock"])
        self.assertEqual([x["name"] for x in gates if not x["ok"]], [], gates)
        self.assertTrue(all(set(x) == {"name", "ok", "detail"} for x in gates))
        with self.assertRaises(ValueError):
            q.offline_gate(g.manifest, g.paths, g.sc, command="frobnicate")

    def test_offline_gate_lists_every_failed_check(self):
        with helpers.tmp_state() as st:
            m = helpers.make_manifest({"upstream.example_dir": str(st.sc / "missing-example"),
                                       "executor.kind": "sandbox", "sandbox.env_mode": "wheelhouse",
                                       "sandbox.wheelhouse.tar_sha256": ZEROS, "sandbox.wheelhouse.tar_bytes": 10,
                                       "proposer.cli_path": str(st.sc / "no-claude")})
            add_unknown_row(st.paths, m)
            failed = [x["name"] for x in q.offline_gate(m, st.paths, st.sc, command="search") if not x["ok"]]
            self.assertEqual(failed, ["manifest_frozen", "upstream_source", "worker_lock", "smoke_receipt",
                                      "proposer_cli", "ledger_unknown", "protocol", "wheelhouse_tar",
                                      "requirements_lock"])
        g = Gate(self)
        g.manifest["notes"] = "changed after the freeze"
        self.assertEqual(g.failed(), ["manifest_frozen"])
        self.assertIn("Manifest changed", g.run()["manifest_frozen"]["detail"])

    def test_gate_workspace_check_works_before_workspace_exists(self):
        g = Gate(self)
        self.assertFalse(g.paths.ws.exists())
        res = g.run("preflight")
        self.assertTrue(res["upstream_source"]["ok"])
        self.assertTrue(res["workspace_pinned"]["ok"])
        self.assertIn("no workspace", res["workspace_pinned"]["detail"])
        workspace.ensure_workspace(g.manifest, g.paths, g.ex, None)
        self.assertEqual(g.failed("preflight"), [])
        write_text(g.paths.ws / "benchmark.py", "SYNTHETIC edited\n")
        self.assertEqual(g.failed("preflight"), ["workspace_pinned"])
        self.assertIn("benchmark.py", g.run("preflight")["workspace_pinned"]["detail"])
        write_text(g.ex / "llm.py", "SYNTHETIC changed upstream\n")
        self.assertEqual(g.failed("preflight"), ["upstream_source", "workspace_pinned"])

    def test_gate_requires_smoke_receipt_for_this_engine(self):
        g = Gate(self)
        receipt = g.sc / "state" / "_smoke" / "receipt.json"
        self.assertTrue(g.run()["smoke_receipt"]["ok"])
        util.write_json(receipt, {"schema": "mhforge.smoke/1", "engine_sha256": ZEROS})
        self.assertEqual(g.failed(), ["smoke_receipt"])
        receipt.unlink()
        self.assertEqual(g.failed(), ["smoke_receipt"])

    def test_gate_requires_filled_protocol(self):
        g = Gate(self, campaign_kind="preflight")
        write_text(g.sc / "protocol.md", "<!-- TEMPLATE-UNFILLED -->\n# Protocol\n")
        g.freeze()
        for command in ("preflight", "evaluate", "search", "export"):
            self.assertEqual(g.failed(command), ["protocol"], command)
        (g.sc / "protocol.md").unlink()
        self.assertEqual(g.failed("preflight"), ["protocol"])
        write_text(g.sc / "protocol.md", "# Protocol\n<!-- STAGE2-UNSIGNED -->\n")
        self.assertEqual(g.failed("preflight"), [])
        self.assertEqual(g.failed("search"), [])          # a preflight campaign needs stage 1 only

    def test_gate_rejects_failed_or_missing_smoke_verdict(self):
        g = Gate(self)
        receipt = g.sc / 'state' / '_smoke' / 'receipt.json'
        for value in (False, None):
            util.write_json(receipt, {'schema': 'mhforge.smoke/1', 'passed': value,
                                     'engine_sha256': q.engine_snapshot(PACKAGE)['engine_sha256']})
            self.assertEqual(g.failed(), ['smoke_receipt'])

    def test_gate_two_stage_protocol(self):
        g = Gate(self)                                    # measured campaign (fixture)
        cases = [("<!-- TEMPLATE-UNFILLED -->\nx\n<!-- STAGE2-UNSIGNED -->\n", 0),
                 ("<!-- TEMPLATE-UNFILLED -->\nx\n", 0), ("x\n<!-- STAGE2-UNSIGNED -->\n", 1), ("x\n", 2)]
        for text, stage in cases:
            write_text(g.sc / "protocol.md", text)
            self.assertEqual(q.protocol_stage(g.sc), stage, text)
        (g.sc / "protocol.md").unlink()
        self.assertEqual(q.protocol_stage(g.sc), 0)
        write_text(g.sc / "protocol.md", "x\n<!-- STAGE2-UNSIGNED -->\n")
        self.assertEqual(g.failed("preflight"), [])
        self.assertEqual(g.failed("evaluate"), [])
        self.assertEqual(g.failed("search"), ["protocol"])
        self.assertIn("stage 1 signed, 2 needed", g.run("search")["protocol"]["detail"])
        write_text(g.sc / "protocol.md", "x\n")
        self.assertEqual(g.failed("search"), [])

    def test_gate_checks_wheelhouse_tar_and_requirements_hash(self):
        g = Gate(self)
        tar = g.sc / "worker-image" / "wheelhouse" / "wheelhouse.tar"
        size = tar.stat().st_size
        g.manifest["sandbox"]["wheelhouse"]["tar_bytes"] = size + 1
        g.freeze()
        self.assertEqual(g.failed(), ["wheelhouse_tar"])
        g.manifest["sandbox"]["wheelhouse"]["tar_bytes"] = size
        g.freeze()
        self.assertEqual(g.failed(), [])
        tar.write_text("SYNTHETIC tar!\n", "utf-8")       # same length, other bytes
        g.manifest["sandbox"]["wheelhouse"]["tar_bytes"] = tar.stat().st_size
        g.freeze()
        self.assertEqual(g.failed(), ["wheelhouse_tar"])
        tar.unlink()
        self.assertEqual(g.failed(), ["wheelhouse_tar"])
        self.assertIn("missing", g.run()["wheelhouse_tar"]["detail"])
        write_text(g.sc / "worker-image" / "requirements.lock.txt", "SYNTHETIC other requirements\n")
        self.assertEqual(g.failed(), ["wheelhouse_tar", "requirements_lock"])
        g.manifest["sandbox"]["env_mode"] = "image"       # image mode: neither applies
        g.freeze()
        self.assertEqual(g.failed(), [])
        g.manifest["executor"]["kind"] = "local_relay"    # and the worker lock applies to the sandbox only
        (g.sc / "worker-image" / "uv.lock").unlink()
        g.freeze()
        self.assertEqual(g.failed(), [])

    def test_gate_problems_are_the_failed_checks_as_strings(self):
        g = Gate(self)
        self.assertEqual(q.gate_problems(q.offline_gate(g.manifest, g.paths, g.sc, command="search")), [])
        (g.sc / "state" / "_smoke" / "receipt.json").unlink()
        write_text(g.sc / "protocol.md", "<!-- TEMPLATE-UNFILLED -->\n")
        gates = q.offline_gate(g.manifest, g.paths, g.sc, command="search")
        problems = q.gate_problems(gates)
        self.assertEqual([p.split(":")[0] for p in problems], ["smoke_receipt", "protocol"])
        detail = {x["name"]: x["detail"] for x in gates}
        self.assertEqual(problems, [f"smoke_receipt: {detail['smoke_receipt']}", f"protocol: {detail['protocol']}"])
        self.assertTrue(all(isinstance(p, str) for p in problems))

    def test_gate_checks_proposer_binary_for_search_only(self):
        g = Gate(self)
        Path(g.manifest["proposer"]["cli_path"]).write_text("SYNTHETIC other binary\n", "utf-8")
        self.assertEqual(g.failed("search"), ["proposer_cli"])
        self.assertEqual(g.failed("preflight"), [])
        add_unknown_row(g.paths, g.manifest)
        self.assertEqual(g.failed("preflight"), ["ledger_unknown"])


# ================================================================ observations, checks, forecast

def rec(i: int, *, outcome="completed", pt=100, ct=50, il=1000, ol=16384, model="openai/gpt-oss-120b", lat=2.0,
        queue=0.5, truncated=False, usage=True, reasoning_key="reasoning") -> dict:
    t0 = helpers.DEFAULT_EPOCH + 10 * i
    usage_block = {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct} if usage else None
    return {"call_id": f"c-{i}", "outcome": outcome, "received_utc": util.utc_iso(t0),
            "ended_utc": util.utc_iso(t0 + lat), "reply": {"status": 200, "code": None, "source": "provider"},
            "queue_s": queue, "input_limit": il, "output_limit": ol, "response_model": model,
            "attempts": [{"n": 1, "started_utc": util.utc_iso(t0 + queue), "first_byte_utc": util.utc_iso(t0 + 1.0)}],
            "response": {"choices": [{"message": {"content": "x", reasoning_key: "why"}}],
                         "usage": None if usage_block is None else
                         dict(usage_block, completion_tokens_details={"reasoning_tokens": 3})},
            "usage": usage_block, "flags": {"truncated_empty": truncated}}


def good_obs() -> dict:
    return q.observations([rec(i, ct=10 * (i + 1)) for i in range(20)],
                          job_record={"attempts": [{"outcome": "ok", "executor": {"seconds": 400.0}}]})


class Observations(unittest.TestCase):
    def test_observations(self):
        recs = [rec(i, ct=10 * (i + 1), lat=1.0 + i) for i in range(20)]
        recs.append(rec(20, ct=0, truncated=True, lat=5.0))
        recs.append(rec(21, usage=False, reasoning_key="reasoning_content"))
        recs.append(rec(22, pt=1500, model="openai/other"))
        recs.append(rec(23, outcome="replayed"))
        recs.append(dict(rec(24), outcome=None, ended_utc=None, reply=None))        # admission-only
        obs = q.observations(recs)
        self.assertEqual(obs["calls"], 25)
        self.assertEqual(obs["outcomes"]["completed"], 23)
        self.assertEqual(obs["outcomes"]["replayed"], 1)
        self.assertEqual(obs["outcomes"]["interrupted"], 1)
        tokens = sorted([10 * (i + 1) for i in range(20)] + [0, 50])
        self.assertEqual(obs["completion_tokens_sorted"], tokens)
        self.assertEqual(obs["completion_tokens"]["n"], 22)
        self.assertEqual(obs["completion_tokens"]["max"], 200)
        self.assertEqual(obs["completion_tokens"]["p50"], q.quantile(tokens, 0.5))
        self.assertEqual(obs["truncated_empty"], 1)
        self.assertAlmostEqual(obs["truncated_empty_rate"], 1 / 25)
        self.assertEqual(obs["models"], ["openai/gpt-oss-120b", "openai/other"])
        self.assertEqual(obs["usage_keys"], ["completion_tokens", "completion_tokens_details",
                                             "completion_tokens_details.reasoning_tokens", "prompt_tokens",
                                             "total_tokens"])
        self.assertEqual(obs["reasoning_fields"], ["reasoning", "reasoning_content"])
        self.assertEqual(obs["calls_without_usage"], 1)
        self.assertEqual(obs["limit_exceeded"], 2)        # rec 22 (prompt over the limit) and rec 20 (3 reasoning
        self.assertEqual(obs["max_prompt_ratio"], 1.5)    # tokens in 0 completion tokens), as the ledger judges them
        self.assertEqual(obs["latency_s"]["max"], 20.0)
        self.assertEqual(obs["latency_p95_s"], q.quantile([1.0 + i for i in range(20)] + [5.0, 2.0, 2.0], 0.95))
        self.assertEqual(obs["first_byte_s"]["p50"], 0.5)
        self.assertEqual(obs["queue_s"]["p95"], 0.5)
        self.assertEqual(obs["job_seconds"], 232.0)        # no job record: first received to last ended
        obs = q.observations(recs, job_record={"attempts": [{"executor": {"seconds": 412.5}}]})
        self.assertEqual(obs["job_seconds"], 412.5)
        obs = q.observations([], job_record=None)
        self.assertEqual(obs["calls"], 0)
        self.assertIsNone(obs["latency_p95_s"])
        fixtures = [json.loads(p.read_text("utf-8")) for p in sorted((helpers.FIXTURES / "call_records").glob("*.json"))]
        obs = q.observations(fixtures)
        self.assertEqual(obs["calls"], 6)
        self.assertEqual(obs["truncated_empty"], 1)
        self.assertEqual(obs["models"], ["openai/gpt-oss-120b"])

    def test_quantile_is_nearest_rank(self):
        vals = list(range(1, 21))
        self.assertEqual(q.quantile(vals, 0.95), 19)
        self.assertEqual(q.quantile(vals, 1), 20)
        self.assertEqual(q.quantile(vals, 0), 1)
        self.assertEqual(q.quantile([7], "0.5"), 7)
        self.assertIsNone(q.quantile([], 0.5))

    def test_checks_table(self):
        m = helpers.make_manifest()
        job = {"attempts": [{"outcome": "ok"}]}
        rows = [{"matched": True}] * 4
        names = ["all_completed", "usage_every_call", "within_input_limit", "truncated_empty_rate", "one_model",
                 "job_outcome_ok", "examples_matched", "audit_clean"]
        table = q.checks(m, job, good_obs(), [], rows)
        self.assertEqual([c["name"] for c in table], names)
        self.assertTrue(all(c["ok"] for c in table), table)

        def failing(obs=None, job_record=job, problems=(), example_rows=rows):
            return [c["name"] for c in q.checks(m, job_record, obs or good_obs(), list(problems), example_rows)
                    if not c["ok"]]

        recs = [rec(i) for i in range(19)]
        self.assertEqual(failing(q.observations(recs + [rec(19, outcome="failed")])), ["all_completed"])
        self.assertEqual(failing(q.observations(recs + [rec(19, usage=False)])), ["usage_every_call"])
        self.assertEqual(failing(q.observations(recs + [rec(19, pt=2000)])), ["within_input_limit"])
        self.assertEqual(failing(q.observations(recs[:18] + [rec(18, truncated=True), rec(19, truncated=True)])),
                         ["truncated_empty_rate"])          # 2 of 20 = 0.10 > 0.05
        self.assertEqual(failing(q.observations(recs[:19] + [rec(19, truncated=True)])), [])   # 0.05 is allowed
        self.assertEqual(failing(q.observations(recs + [rec(19, model="openai/other")])), ["one_model"])
        self.assertEqual(failing(job_record={"attempts": [{"outcome": "timeout"}]}), ["job_outcome_ok"])
        self.assertEqual(failing(job_record={"attempts": []}), ["job_outcome_ok"])
        self.assertEqual(failing(example_rows=rows + [{"matched": False}]), ["examples_matched"])
        self.assertEqual(failing(example_rows=[]), ["examples_matched"])
        self.assertEqual(failing(problems=["ledger row x: pending"]), ["audit_clean"])
        self.assertEqual(failing(q.observations([])),
                         ["all_completed", "usage_every_call", "one_model"])


def ceil(d: Decimal) -> int:
    return int(d.to_integral_value(rounding=ROUND_CEILING))


class Forecast(unittest.TestCase):
    def test_forecast_arithmetic_and_stop(self):
        m = helpers.make_manifest({"budgets.proposer_list_usd": "400.00"})
        obs = good_obs()                                   # completion tokens 10..200: q(0.95) = 190
        fc = q.forecast(m, obs)
        self.assertEqual(fc["output_tokens_q"], 190)
        self.assertEqual(fc["per_job"]["val"]["USPTO"], ceil(Decimal("0.03") * 140494 + Decimal("0.17") * 30 * 190))
        self.assertEqual(fc["per_job"]["val"]["USPTO"], 5184)
        self.assertEqual(fc["per_job"]["test"]["LawBench"], ceil(Decimal("0.03") * 2469033 + Decimal("0.17") * 100 * 190))
        val = sum(fc["per_job"]["val"].values())
        test = sum(fc["per_job"]["test"].values())
        self.assertEqual(fc["validation_total_micro"], ceil(Decimal("1.25") * (2 + 20 * 3) * val))
        self.assertEqual(fc["test_total_micro"], ceil(Decimal("1.25") * 6 * test))
        self.assertEqual(fc['controls_total_micro'], ceil(Decimal('1.25') * 5 * test))
        no_control_budget = json.loads(json.dumps(m))
        no_control_budget['budgets']['solver_usd']['phases']['controls'] = '0'
        self.assertFalse(q.forecast(no_control_budget, obs)['fits_solver'])
        self.assertEqual(fc["sandbox_total_micro"], 0)     # local_relay executor: no sandbox meter
        self.assertTrue(fc["fits_solver"] and fc["fits_sandbox"] and fc["fits_proposer"] and fc["fits"], fc)
        self.assertEqual(set(fc) >= {"per_job", "validation_total_micro", "test_total_micro", "sandbox_total_micro",
                                     "proposer_total_micro", "calibration_total_micro", "fits_solver", "fits_sandbox",
                                     "fits_proposer", "fits"}, True)
        q.require_fits(fc)
        # nothing observed: every reply is taken at the token cap, which does not fit the search cap
        worst = q.forecast(m, q.observations([]))
        self.assertEqual(worst["output_tokens_q"], 16384)
        self.assertFalse(worst["fits_solver"])
        self.assertFalse(worst["fits"])
        with self.assertRaises(CampaignStop) as cm:
            q.require_fits(worst)
        self.assertEqual(cm.exception.reason, "forecast_exceeds_budget")
        self.assertIn("solver", cm.exception.detail)
        # every constant comes from the manifest
        m2 = helpers.make_manifest({"budgets.proposer_list_usd": "400.00", "accounting.forecast_contingency": "2",
                                    "forecast.candidates_per_iteration": 1, "search.iterations": 2})
        fc2 = q.forecast(m2, obs)
        self.assertEqual(fc2["validation_total_micro"], ceil(Decimal(2) * (2 + 2 * 1) * val))
        # the caller writes forecast.json and no passed.json when it does not fit
        with helpers.tmp_state() as st:
            engine = q.engine_snapshot(PACKAGE)
            q.record(st.paths, 1, engine=engine, manifest_sha256=ZEROS, check_list=[{"name": "x", "ok": True}],
                     obs=obs, fc=worst)
            self.assertFalse(st.paths.qualification_passed.exists())
            doc = util.read_json(st.paths.qualification_dir(1) / "forecast.json")
            self.assertEqual(doc["schema"], "mhforge.forecast/1")
            self.assertFalse(doc["fits"])

    def test_forecast_checks_sandbox_and_proposer_budgets(self):
        m = helpers.make_manifest({"executor.kind": "sandbox", "sandbox.env_mode": "image",
                                   "budgets.proposer_list_usd": "400.00"})
        obs = dict(good_obs(), job_seconds=40.0)           # 2 s per call: every scaled job stays under 600 s
        fc = q.forecast(m, obs)
        one = sandbox_cost_micro(600_000, "2", "4Gi", m)    # a 600 s job: job_seconds of the manifest wins
        check = sandbox_cost_micro(300_000, "2", "4Gi", m)
        jobs = (62 + 4 * 2) * 3 + (6 + 2 + 5) * 3          # validation/noise; test/calibration/five-sample control
        self.assertEqual(fc["sandbox_total_micro"], ceil(Decimal("1.25") * (jobs * one + 20 * check)))
        self.assertTrue(fc["fits_sandbox"])
        tight = q.forecast(helpers.make_manifest({"executor.kind": "sandbox", "sandbox.env_mode": "image",
                                                  "budgets.proposer_list_usd": "400.00",
                                                  "budgets.sandbox_usd": "5.00"}), obs)
        self.assertFalse(tight["fits_sandbox"])
        self.assertTrue(tight["fits_solver"])
        self.assertFalse(tight["fits"])
        # proposer: ceil(1.5 * 20) sessions at the estimate, times the contingency
        self.assertEqual(fc["proposer_sessions"], 30)
        self.assertEqual(fc["proposer_total_micro"], ceil(Decimal("1.25") * 30 * 10_000_000))
        self.assertTrue(fc["fits_proposer"])
        low = q.forecast(helpers.make_manifest(), obs)      # fixture cap $100 < $375
        self.assertFalse(low["fits_proposer"])
        self.assertFalse(low["fits"])
        unset = q.forecast(helpers.make_manifest({"proposer.session_estimate_usd": None}), obs)
        self.assertIsNone(unset["proposer_total_micro"])
        self.assertFalse(unset["fits_proposer"])
        none = q.forecast(helpers.make_manifest({"search.iterations": 0, "budgets.proposer_list_usd": None}), obs)
        self.assertEqual(none["proposer_total_micro"], 0)
        self.assertTrue(none["fits_proposer"])
        with self.assertRaises(CampaignStop):
            q.require_fits(low)
        # queue wait of three validation jobs at once: ceil(94 / max_in_flight) * p95 latency
        self.assertEqual(fc["queue_wait_bound_s"], math.ceil(94 / 16) * obs["latency_p95_s"])
        self.assertTrue(fc["parallel_validation_safe"])

    def test_forecast_includes_calibration_and_setup_time(self):
        base = {"budgets.proposer_list_usd": "400.00"}
        obs = good_obs()
        fc = q.forecast(helpers.make_manifest(base), obs)
        val, test = sum(fc["per_job"]["val"].values()), sum(fc["per_job"]["test"].values())
        self.assertEqual(fc["calibration_total_micro"], ceil(Decimal("1.25") * (2 * test + 4 * 2 * val)))
        one = q.forecast(helpers.make_manifest(base, **{"calibration.noise_repeats": 1}), obs)
        self.assertEqual(one["calibration_total_micro"], ceil(Decimal("1.25") * 2 * test))
        three = q.forecast(helpers.make_manifest(base, **{"calibration.systems": ["no_memory"]}), obs)
        self.assertEqual(three["calibration_total_micro"], ceil(Decimal("1.25") * (1 * test + 4 * 2 * val)))
        tight = q.forecast(helpers.make_manifest(base, **{"budgets.solver_usd.phases.calibration": "0.01"}), obs)
        self.assertFalse(tight["fits_solver"])
        # setup time: in mode wheelhouse every sandbox carries setup_allowance_s; job seconds scale from the observed job
        sb = {"executor.kind": "sandbox", **base}
        obs = dict(obs, job_seconds=40.0)
        image = q.forecast(helpers.make_manifest(sb, **{"sandbox.env_mode": "image"}), obs)
        wheel = q.forecast(helpers.make_manifest(sb, **{"sandbox.env_mode": "wheelhouse"}), obs)
        m = helpers.make_manifest(sb, **{"sandbox.env_mode": "wheelhouse"})
        cost = lambda s: sandbox_cost_micro(math.ceil(s * 1000), "2", "4Gi", m)   # noqa: E731
        jobs = (62 + 8) * 3 + (6 + 2 + 5) * 3
        self.assertEqual(wheel["sandbox_total_micro"], ceil(Decimal("1.25") * (jobs * cost(750) + 20 * cost(450))))
        self.assertGreater(wheel["sandbox_total_micro"], image["sandbox_total_micro"])
        slow = dict(obs, job_seconds=3000.0, calls=30)       # 100 s per call: a 30-call USPTO job takes 3,000 s
        fc = q.forecast(m, slow)
        self.assertEqual(fc["job_seconds"]["val"]["USPTO"], 3000.0)
        self.assertEqual(fc["job_seconds"]["test"]["Symptom2Disease"], 3000.0 * 212 / 30)
        self.assertEqual(fc["job_seconds"]["test"]["USPTO"], 10000.0)


# ================================================================ records, qualification, import, restart

def engine_with(files: dict) -> dict:
    return {"engine_sha256": util.tree_digest(files), "files": dict(files)}


class Records(unittest.TestCase):
    def test_passed_json_records_environment_and_import(self):
        env = {"env_mode": "wheelhouse", "image_digest": "sha256:" + ZEROS, "requirements_sha256": ZEROS,
               "sha256sums_sha256": ZEROS, "freeze_sha256": ZEROS, "machine": "x86_64", "python": "3.13.18"}
        engine = q.engine_snapshot(PACKAGE)
        with helpers.tmp_state() as st:
            self.assertEqual(q.next_attempt(st.paths), 1)
            self.assertIsNone(q.qualified_environment(st.paths))
            clock = helpers.FakeClock()
            doc = q.record(st.paths, 1, engine=engine, manifest_sha256="a" * 64,
                           check_list=[{"name": "c", "ok": False, "detail": ""}], obs={}, fc={"fits": True},
                           environment=env, clock=clock)
            self.assertFalse(doc["passed"])
            self.assertFalse(st.paths.qualification_passed.exists())
            self.assertTrue((st.paths.qualification_dir(1) / "qualification.json").exists())
            self.assertEqual(q.next_attempt(st.paths), 2)
            q.record(st.paths, 2, engine=engine, manifest_sha256="a" * 64, check_list=[{"name": "c", "ok": True}],
                     obs={"calls": 1}, fc={"fits": True}, environment=env, clock=clock)
            passed = util.read_json(st.paths.qualification_passed)
            self.assertEqual(passed, {"schema": "mhforge.passed/1", "engine_sha256": engine["engine_sha256"],
                                      "manifest_sha256": "a" * 64, "attempt": 2, "utc": clock.utc(),
                                      "environment": env, "imported_from": None})
            self.assertEqual(q.qualified_environment(st.paths), env)
            imported = {"campaign": "mh-pre", "manifest_sha256": "b" * 64, "diff_paths": ["campaign", "budgets.x"]}
            q.record(st.paths, 3, engine=engine, manifest_sha256="a" * 64, check_list=[{"name": "c", "ok": True}],
                     obs={}, fc={}, environment=env, imported_from=imported, clock=clock)
            passed = util.read_json(st.paths.qualification_passed)
            self.assertEqual(passed["imported_from"], imported)
            self.assertEqual(passed["environment"], env)
            self.assertEqual(passed["attempt"], 3)

    def test_qualified_only_for_same_engine_and_manifest(self):
        engine = q.engine_snapshot(PACKAGE)
        with helpers.tmp_state() as st:
            m = helpers.make_manifest()
            digest = manifest_mod.manifest_sha256(m)
            with self.assertRaises(CampaignStop) as cm:
                q.verify_qualified(st.paths, m, engine)
            self.assertEqual(cm.exception.reason, "qualification_failed")
            q.record(st.paths, 1, engine=engine, manifest_sha256=digest, check_list=[{"name": "c", "ok": True}],
                     obs={}, fc={"fits": True})
            q.verify_qualified(st.paths, m, engine)
            other = helpers.make_manifest({"search.iterations": 3})
            with self.assertRaises(CampaignStop) as cm:
                q.verify_qualified(st.paths, other, engine)
            self.assertIn("manifest_sha256", cm.exception.detail)
            changed = engine_with(dict(engine["files"], **{"mhforge/gateway.py": ZEROS}))
            with self.assertRaises(CampaignStop) as cm:
                q.verify_qualified(st.paths, m, changed)
            self.assertIn("engine changed", cm.exception.detail)

    def test_restart_rules(self):
        old = {"mhforge/gateway.py": "a" * 64, "mhforge/ledger.py": "b" * 64}
        new = dict(old, **{"mhforge/gateway.py": "c" * 64})
        clean = {"in_flight_micro": 0, "unknown_requests": 0, "committed_by_phase": {"qualification": 100},
                 "caps_micro": {"qualification": 3_000_000}}
        with helpers.tmp_state() as st:
            q.record(st.paths, 1, engine=engine_with(old), manifest_sha256=ZEROS,
                     check_list=[{"name": "c", "ok": False}], obs={}, fc={})
            self.assertTrue(any("no recorded stop" in r for r in q.restart_allowed(st.paths, clean, engine_with(new))))
            util.write_json(st.paths.stopped, {"schema": "mhforge.stop/1", "reason": "qualification_failed",
                                               "phase": "qualification"})
            self.assertEqual(q.restart_allowed(st.paths, clean, engine_with(new)), [])
            self.assertEqual(len(q.restart_allowed(st.paths, clean, engine_with(old))), 1)
            self.assertIn("no engine file changed", q.restart_allowed(st.paths, clean, engine_with(old))[0])
            removed = {"mhforge/gateway.py": "a" * 64}
            self.assertEqual(q.restart_allowed(st.paths, clean, engine_with(removed)), [])
            pending = dict(clean, in_flight_micro=50)
            self.assertIn("pending", q.restart_allowed(st.paths, pending, engine_with(new))[0])
            unknown = dict(clean, unknown_requests=1)
            self.assertIn("unknown", q.restart_allowed(st.paths, unknown, engine_with(new))[0])
            spent = dict(clean, committed_by_phase={"qualification": 3_000_000})
            self.assertIn("cap", q.restart_allowed(st.paths, spent, engine_with(new))[0])
            util.write_json(st.paths.stopped, {"schema": "mhforge.stop/1", "reason": "budget:search", "phase": "search"})
            self.assertIn("phase 'search'", q.restart_allowed(st.paths, clean, engine_with(new))[0])
        with helpers.tmp_state() as st:                    # no attempt yet: the engine frozen at --freeze is the base
            util.write_json(st.paths.stopped, {"schema": "mhforge.stop/1", "phase": "qualification"})
            util.write_json(st.paths.engine, engine_with(old))
            self.assertEqual(q.restart_allowed(st.paths, clean, engine_with(new)), [])
            st.paths.engine.unlink()
            self.assertIn("no earlier engine snapshot", q.restart_allowed(st.paths, clean, engine_with(new))[0])


class Import(unittest.TestCase):
    """A preflight campaign `mh-pre` and a measured campaign `mh-test` under one SC."""

    def setUp(self):
        ctx = helpers.tmp_state()
        st = ctx.__enter__()
        self.addCleanup(ctx.__exit__, None, None, None)
        self.paths = st.paths
        self.engine = q.engine_snapshot(PACKAGE)
        self.source = helpers.make_manifest({"campaign": "mh-pre", "state_dir": "state/mh-pre",
                                             "campaign_kind": "preflight", "decisions.project": "mh-scratch"})
        self.src = self.paths.sibling("mh-pre")
        util.write_json(self.src.manifest, self.source)
        q.record(self.src, 1, engine=self.engine, manifest_sha256=manifest_mod.manifest_sha256(self.source),
                 check_list=[{"name": "c", "ok": True}], obs={}, fc={"fits": True}, environment={"machine": "x86_64"})
        self.reconcile("pass")
        self.measured = helpers.make_manifest({"qualification.import_from": "mh-pre"})

    def reconcile(self, verdict: str, attempt: int = 1) -> None:
        util.write_json(self.src.qualification_dir(attempt) / "reconciliation.json",
                        {"schema": "mhforge.reconciliation/1", "verdict": verdict})

    def problems(self, manifest=None, engine=None) -> list[str]:
        return q.import_problems(manifest or self.measured, self.paths, engine or self.engine)

    @staticmethod
    def leaf(manifest: dict, entry: str) -> str:
        """A concrete leaf path for an IMPORT_DIFF_ALLOWED entry (an entry ending in '.' names a subtree)."""
        if not entry.endswith("."):
            return entry
        node = util.lookup(manifest, entry[:-1])
        path = entry[:-1]
        while isinstance(node, dict) and node:
            key = sorted(node)[0]
            path, node = f"{path}.{key}", node[key]
        return path

    def test_import_allowed_only_for_named_differences(self):
        self.assertEqual(self.problems(), [])
        for entry in manifest_mod.IMPORT_DIFF_ALLOWED:
            path = self.leaf(self.measured, entry)
            if path in ("campaign", "state_dir", "campaign_kind", "qualification.import_from", "decisions.project"):
                continue                                   # these already differ in every import
            m = json.loads(json.dumps(self.measured))
            helpers._set_dotted(m, path, "SYNTHETIC-CHANGED")
            self.assertEqual(self.problems(m), [], path)
        for path, value in (("routing.route", 1), ("sandbox.base_image", "registry.example.com/x@sha256:" + "1" * 64),
                            ("decisions.proposer_permission_mode", "restricted")):
            m = json.loads(json.dumps(self.measured))
            helpers._set_dotted(m, path, value)
            got = self.problems(m)
            self.assertEqual(len(got), 1, got)
            self.assertTrue(got[0].startswith(path + ":"), got)
        changed = engine_with(dict(self.engine["files"], **{"mhforge/gateway.py": ZEROS}))
        got = self.problems(engine=changed)
        self.assertEqual(len(got), 1)
        self.assertIn("another engine", got[0])
        same = helpers.make_manifest({"qualification.import_from": "mh-pre", "decisions.project": "mh-scratch"})
        got = self.problems(same)
        self.assertEqual(len(got), 1)
        self.assertIn("fresh project", got[0])
        self.assertIn("only a measured campaign", self.problems(dict(self.measured, campaign_kind="preflight"))[0])
        self.assertIn("null", self.problems(helpers.make_manifest())[-1])
        missing = helpers.make_manifest({"qualification.import_from": "mh-gone"})
        self.assertIn("no readable frozen manifest", self.problems(missing)[0])
        util.write_json(self.src.manifest, dict(self.source, campaign_kind="measured"))
        got = self.problems()
        self.assertTrue(any("not a preflight campaign" in p for p in got), got)
        self.assertTrue(any("manifest_sha256 differs" in p for p in got), got)

    def test_import_needs_reconciliation(self):
        self.reconcile("fail")
        self.assertIn("reconciliation", self.problems()[0])
        self.reconcile("waived")
        self.assertEqual(self.problems(), [])
        (self.src.qualification_dir(1) / "reconciliation.json").unlink()
        self.assertIn("missing", self.problems()[0])
        self.reconcile("pass")
        self.src.qualification_dir(2).mkdir(parents=True)  # a later attempt without PF-R: the last attempt counts
        self.assertIn("reconciliation", self.problems()[0])
        self.reconcile("pass", attempt=2)
        self.assertEqual(self.problems(), [])

    def test_verify_qualified_rechecks_an_imported_qualification(self):
        diff = manifest_mod.diff_paths(self.source, self.measured)
        q.record(self.paths, 1, engine=self.engine, manifest_sha256=manifest_mod.manifest_sha256(self.measured),
                 check_list=[{"name": "inference", "ok": True}], obs={}, fc={}, environment={"machine": "x86_64"},
                 imported_from={"campaign": "mh-pre", "manifest_sha256": manifest_mod.manifest_sha256(self.source),
                                "diff_paths": diff})
        q.verify_qualified(self.paths, self.measured, self.engine)
        self.reconcile("fail")
        with self.assertRaises(CampaignStop) as cm:
            q.verify_qualified(self.paths, self.measured, self.engine)
        self.assertIn("reconciliation", cm.exception.detail)

    def test_calibration_verdict_not_borrowed_across_different_gates(self):
        util.write_json(self.src.calibration, {"schema": "mhforge.calibration/1", "verdict": "pass"})

        def passed_with(diff):
            util.write_json(self.paths.qualification_passed, {
                "schema": "mhforge.passed/1", "engine_sha256": self.engine["engine_sha256"],
                "manifest_sha256": ZEROS, "attempt": 1, "utc": "x", "environment": None,
                "imported_from": {"campaign": "mh-pre", "manifest_sha256": ZEROS, "diff_paths": diff}})

        passed_with(["campaign", "budgets.sandbox_usd"])
        self.assertEqual(q.calibration_verdict(self.measured, self.paths), ("pass", "imported campaign mh-pre"))
        passed_with(["campaign", "calibration.gates.G1.macro_test_accuracy"])
        verdict, where = q.calibration_verdict(self.measured, self.paths)
        self.assertIsNone(verdict)
        self.assertIn("calibration fields differ", where)
        m = json.loads(json.dumps(self.measured))
        m["calibration"]["gates"]["G1"]["macro_test_accuracy"] = [20.0, 30.0]
        self.assertEqual(self.problems(m), [])            # the difference is allowed for the import itself (LD3)
        util.write_json(self.paths.calibration, {"schema": "mhforge.calibration/1", "verdict": "fail"})
        self.assertEqual(q.calibration_verdict(self.measured, self.paths), ("fail", "this campaign"))
        self.paths.calibration.unlink()
        self.paths.qualification_passed.unlink()
        self.assertIsNone(q.calibration_verdict(self.measured, self.paths)[0])
        self.assertEqual(q.calibration_verdict(helpers.make_manifest(), self.paths), (None, "this campaign"))
        # the live gate of manifest.py reads the verdict by the same rule
        passed_with(["campaign", "calibration.noise_repeats"])
        self.assertEqual(manifest_mod._calibration_verdict(self.measured, self.paths)[0],
                         q.calibration_verdict(self.measured, self.paths)[0])
        passed_with(["campaign"])
        self.assertEqual(manifest_mod._calibration_verdict(self.measured, self.paths)[0],
                         q.calibration_verdict(self.measured, self.paths)[0])


# ================================================================ noise floor

def sidecar(correct: dict[int, bool], *, answers: dict[int, str] | None = None, extra_pass_wrong=()) -> list[dict]:
    rows = [{"kind": "meta", "seq": 1, "schema": "mhforge.sidecar/1"},
            {"kind": "splits", "seq": 2, "wanted": ["val"]}]
    seq = 3
    for p in (1, 2):
        for i, ok in sorted(correct.items()):
            was = ok if p == 2 else (not ok if i in extra_pass_wrong else ok)   # pass 2 is the one that counts
            pred = (answers or {}).get(i, "A" if ok else "B")
            rows.append({"kind": "eval", "seq": seq, "split": "val", "index": i, "pass": p, "call_seqs": [],
                         "batch_user_sha256": [], "was_correct": was, "prediction": pred, "target": "A",
                         "prediction_sha256": util.sha256_bytes(pred.encode()), "metrics": {}})
            seq += 1
    rows.append({"kind": "eval", "seq": seq, "split": "train", "index": 0, "pass": 1, "call_seqs": [],
                 "batch_user_sha256": [], "was_correct": False, "prediction": "x", "target": "y", "metrics": {}})
    rows.append({"kind": "done", "seq": seq + 1})
    return rows


class Noise(unittest.TestCase):
    def test_noise_stats(self):
        a = {0: True, 1: True, 2: False, 3: False}
        flipped = {0: True, 1: False, 2: False, 3: True}
        sidecars = {"no_memory": {"USPTO": {1: sidecar(a, extra_pass_wrong=(0,)), 2: sidecar(a), 3: sidecar(flipped)},
                                  "LawBench": {1: sidecar({0: True, 1: False}), 2: sidecar({0: True, 1: False}),
                                               3: sidecar({0: True, 1: False})}},
                    "fewshot_all": {"USPTO": {1: sidecar(a), 2: sidecar(a), 3: sidecar(a)}}}
        results = {"no_memory": {"USPTO": {1: {"accuracy": 0.5, "correct": 2, "total": 4},
                                           3: {"accuracy": 0.5, "correct": 2, "total": 5}}}}
        out = q.noise_stats(sidecars, results)
        self.assertEqual(out["schema"], "mhforge.noise/1")
        self.assertEqual(out["repeats"], 3)
        self.assertEqual(out["systems"], ["fewshot_all", "no_memory"])
        us = out["per_dataset"]["USPTO"]["no_memory"]
        self.assertEqual(us["correct"], [2, 2, 2])         # the highest pass counts, not pass 1
        self.assertEqual(us["totals"], [4, 4, 5])          # a result file's total is the denominator when present
        self.assertEqual(us["total"], 5)
        self.assertEqual(us["all_agree"], 2)               # examples 0 and 2 agree; 1 and 3 flip
        self.assertEqual(us["flip_rate"], 0.5)
        self.assertEqual(out["per_dataset"]["LawBench"]["no_memory"]["flip_rate"], 0.0)
        self.assertEqual(out["per_dataset"]["LawBench"]["no_memory"]["all_agree"], 2)
        macro = out["macro_validation_accuracy"]["no_memory"]
        self.assertEqual(macro, [50.0, 50.0, round((40.0 + 50.0) / 2, 6)])
        self.assertAlmostEqual(out["macro_std"]["no_memory"], round(__import__("statistics").stdev(macro), 6))
        self.assertEqual(out["macro_std"]["fewshot_all"], 0.0)
        self.assertFalse(out["identical_all_repeats"])
        self.assertEqual(out["problems"], [])
        same = q.noise_stats({"no_memory": {"USPTO": {1: sidecar(a), 2: sidecar(a)}}}, {})
        self.assertTrue(same["identical_all_repeats"])
        self.assertEqual(same["per_dataset"]["USPTO"]["no_memory"]["flip_rate"], 0.0)
        # same correctness but another answer is not identical
        other = q.noise_stats({"no_memory": {"USPTO": {1: sidecar(a), 2: sidecar(a, answers={2: "C"})}}}, {})
        self.assertFalse(other["identical_all_repeats"])
        self.assertEqual(other["per_dataset"]["USPTO"]["no_memory"]["all_agree"], 4)
        # a result file whose correct count disagrees with its sidecar is reported
        bad = q.noise_stats({"no_memory": {"USPTO": {1: sidecar(a)}}}, {"no_memory": {"USPTO": {1: {"correct": 3}}}})
        self.assertEqual(len(bad["problems"]), 1)
        self.assertEqual(q.noise_stats({}, {})["identical_all_repeats"], False)


class ReviewRegressions(unittest.TestCase):
    def test_limit_exceeded_follows_the_ledger_rule(self):
        r = rec(0, pt=100, ct=50, il=1000, ol=100)          # both counters within, total_tokens above il + ol
        r["usage"]["total_tokens"] = r["response"]["usage"]["total_tokens"] = 5000
        prices = ledger_mod.prices_from_manifest(helpers.make_manifest())
        self.assertEqual(ledger_mod.judge_usage(r["response"]["usage"], prices, 1000, 100, 10**9)[2], "limit_exceeded")
        obs = q.observations([r])
        self.assertEqual(obs["limit_exceeded"], 1)
        self.assertIn("within_input_limit",
                      [c["name"] for c in q.checks(helpers.make_manifest(), {"attempts": [{"outcome": "ok"}]}, obs, [],
                                                   [{"matched": True}]) if not c["ok"]])

    def test_imported_passed_json_carries_source_environment(self):
        env = {"machine": "x86_64", "freeze_sha256": "f" * 64}
        engine = q.engine_snapshot(PACKAGE)
        with helpers.tmp_state() as st:
            src = st.paths.sibling("mh-pre")
            q.record(src, 1, engine=engine, manifest_sha256="b" * 64, check_list=[{"name": "c", "ok": True}],
                     obs={}, fc={"fits": True}, environment=env)
            q.record(st.paths, 1, engine=engine, manifest_sha256="a" * 64, check_list=[{"name": "c", "ok": True}],
                     obs={}, fc={}, imported_from={"campaign": "mh-pre", "manifest_sha256": "b" * 64,
                                                   "diff_paths": ["campaign"]})
            self.assertEqual(q.qualified_environment(st.paths), env)

    def test_gate_refuses_possibly_stale_ledger(self):
        import os
        import shutil
        g = Gate(self)
        Ledger(g.paths.ledger, g.manifest)
        led = str(g.paths.ledger)
        conn = sqlite3.connect(led)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        shutil.copy(led, led + ".main")
        conn.execute("INSERT INTO calls (id, call_id, n, job_id, job_key, phase, origin_phase, model, input_limit, "
                     "output_limit, reserved_micro, status, created_utc) VALUES ('l-x', 'c-x', 1, 'j.a1', 'j', "
                     "'qualification', 'qualification', 'm', 1, 1, 1, 'unknown', 'x')")
        conn.commit()
        shutil.copy(led + "-wal", led + ".wal")
        conn.close()
        os.replace(led + ".main", led)                   # a crash left the log, whose index is gone
        os.replace(led + ".wal", led + "-wal")
        if os.path.exists(led + "-shm"):
            os.unlink(led + "-shm")
        self.assertEqual(g.failed("preflight"), ["ledger_unknown"])
        self.assertIn("possibly stale", g.run("preflight")["ledger_unknown"]["detail"])

    def test_noise_stats_reports_missing_repeat(self):
        a = {0: True, 1: False}
        out = q.noise_stats({"s": {"USPTO": {1: sidecar(a), 2: sidecar(a)}, "LawBench": {1: sidecar(a)}}}, {})
        self.assertEqual(out["problems"], ["s/LawBench: repeats [1] of [1, 2]"])
        self.assertFalse(out["identical_all_repeats"])


if __name__ == "__main__":
    unittest.main()
