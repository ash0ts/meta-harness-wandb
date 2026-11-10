# SPDX-License-Identifier: Apache-2.0
# Adapted from ash0ts/harness2-wandb@31b0f82 tests/showcase/test_telemetry_v2.py
# (test_interrupted_grouped_export_reads_both_existing_traces_without_reemission). Changes: unittest instead of pytest;
# the receipt of spec 2.16 and the fake sinks of tests/fake_sinks.py instead of monkeypatched SDK functions; every other
# case in this file is new.
"""The exporter of spec 3.25: plan, scan, meter, emit, read back, receipt (against FakeLensSink and FakeWandbSink)."""

from __future__ import annotations

import copy
import glob
import json
import shutil
import unittest
from pathlib import Path

from mhforge import journal as journal_mod
from mhforge import util
from mhforge.errors import ExportError, StateLockError
from mhforge.ledger import MIB, Ledger
from mhforge.paths import unit_slug
from mhforge.state import StateLock
from mhforge.telemetry import exporter as exporter_mod
from mhforge.telemetry.exporter import Exporter, ExportUnit, final_statuses
from tests import helpers
from tests.fake_sinks import PRIVATE, FakeLensSink, FakeWandbSink

FIX = helpers.FIXTURES
SESSION_ID = "5e55104e-0000-4000-8000-00000000abcd"
CANDIDATE_TEXT = "# SPDX-License-Identifier: MIT\nfrom memory_system import MemorySystem\n\n\nclass X(MemorySystem):\n    pass\n"
JOB = "job:val.no_memory.USPTO.a1"
SECRET = "mhfsecretvalue0123456789abcdefXYZ"
S2D_QUESTION = "I have been having a persistent dry cough and a mild fever for three days running now."
USPTO_TARGET = "CC(=O)Oc1ccccc1C(=O)O"
USPTO_QUESTION = "Predict the reactants that produce the product O=C(O)c1ccccc1O under these conditions."
_DEFAULT = object()
LAW_QUESTION = "The defendant entered the premises at night and removed property belonging to another person."


# ---------------------------------------------------------------- campaign scaffold

class Campaign:
    """Writes the finished records of a small campaign (2.4 layout) and builds exporters over them."""

    def __init__(self, st: helpers.TmpState, clock: helpers.FakeClock | None = None) -> None:
        self.st, self.paths = st, st.paths
        self.clock = clock or helpers.FakeClock()
        self.journal = journal_mod.Journal(self.paths.journal, clock=self.clock)
        self._example: Path | None = None

    def job(self, candidate="no_memory", dataset="USPTO", split="val", attempt=1, *, status="succeeded",
            journal_status="complete", outcome="ok", calls=True, record=True, response_id=None) -> str:
        key = f"{split}.{candidate}.{dataset}"
        job_id = f"{key}.a{attempt}"
        self.journal.record("job", key, "started", attempt=attempt, detail={"executor": "local_relay"})
        if record:
            util.write_json(self.paths.job_dir(key) / "job.json", {
                "schema": "mhforge.job/1", "campaign": self.st.campaign, "job_key": key, "split": split,
                "candidate": candidate, "dataset": dataset, "phase": "baseline", "candidate_sha256": "ab" * 32,
                "status": status,
                "attempts": [{"attempt": attempt, "job_id": job_id, "started_utc": "2026-11-10T03:14:00.000000Z",
                              "ended_utc": "2026-11-10T03:17:00.000000Z", "outcome": outcome,
                              "gateway": {"cost_micro": 100, "truncated_empty": 0}}],
                "result": {"accuracy": 0.5, "total": 4, "correct": 2}})
        if calls:
            d = self.paths.calls_dir(job_id)
            d.mkdir(parents=True, exist_ok=True)
            for p in sorted(glob.glob(str(FIX / "call_records" / "*.json"))):
                rec = json.loads(Path(p).read_text())
                rec["job"].update(job_id=job_id, job_key=key, dataset=dataset, candidate=candidate, split=split)
                if response_id and rec["outcome"] == "completed" and rec.get("response_id"):
                    rec["response_id"] = response_id
                    rec["response"]["id"] = response_id
                util.write_json(d / f"{rec['call_id']}.json", rec)
        out = self.paths.attempt_dir(key, attempt) / "out"
        out.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIX / "sidecar_uspto_val.jsonl", out / "sidecar.jsonl")
        (out / "memory.json").write_text('{"train": "TRAINING SPLIT VERBATIM"}')
        (out / "log.jsonl").write_text('{"checkpoint": "TRAINING SPLIT VERBATIM"}\n')
        (out / "val.json").write_text('{"accuracy": 0.5}')
        cache = self.paths.attempt_dir(key, attempt) / "cache"
        cache.mkdir(exist_ok=True)
        (cache / "reply.json").write_text('{"reply": "CACHED REPLY TEXT"}')
        if journal_status:
            self.journal.record("job", key, journal_status, attempt=attempt, detail={"outcome": outcome})
        return f"job:{job_id}"

    def baseline_file(self, candidate="no_memory", text=CANDIDATE_TEXT) -> Path:
        p = self.paths.ws / "agents" / f"{candidate}.py"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def benchmark(self, split="val", candidate="no_memory", *, complete=True) -> str:
        self.journal.record("benchmark", f"{split}.{candidate}", "started")
        if complete:
            self.journal.record("benchmark", f"{split}.{candidate}", "complete", detail={"returncode": 0})
        return f"eval:{split}.{candidate}"

    def session(self, iteration=1, ordinal=1, candidates=("x",), text=CANDIDATE_TEXT, *, hypothesis="retrieve more",
                stream_rows=None, record=True) -> str:
        sdir = self.paths.session_dir(iteration, ordinal)
        key = f"iter{iteration:03d}.s{ordinal}"
        self.journal.record("proposer", f"iter{iteration:03d}", "started", attempt=ordinal)
        sdir.mkdir(parents=True, exist_ok=True)
        if stream_rows is None:
            shutil.copytree(FIX / "transcript_synthetic", sdir / "transcript")
        else:
            for row in stream_rows:
                util.append_jsonl(sdir / "stream.jsonl", row)
        if record:
            util.write_json(sdir / "session.json", {
                "schema": "mhforge.session/1", "campaign": self.st.campaign, "iteration": iteration,
                "ordinal": ordinal, "session_key": key, "permission_mode": "upstream", "cli": {"version": "2.1.317"},
                "started_utc": "2026-11-10T04:00:00.000000Z", "ended_utc": "2026-11-10T04:30:00.000000Z",
                "outcome": "ok", "session_id": SESSION_ID if stream_rows is None else None,
                "resolved_model": "claude-opus-5-5", "result_event": {"present": True}})
        entries = []
        for name in candidates:
            (sdir / "candidates").mkdir(exist_ok=True)
            (sdir / "candidates" / f"{name}.py").write_text(text)
            entries.append({"name": name, "file": f"candidates/{name}.py", "sha256": util.sha256_bytes(text.encode()),
                            "base_system": "no_memory", "axis": "exploration", "hypothesis": hypothesis,
                            "components": ["retriever"], "valid_name": True})
        if candidates:
            util.write_json(sdir / "proposal.json", {"schema": "mhforge.proposal/1", "iteration": iteration,
                                                     "session_key": key, "candidates": entries})
        self.journal.record("proposer", f"iter{iteration:03d}", "complete", attempt=ordinal, detail={"outcome": "ok"})
        return f"proposer:{key}"

    def iteration(self, n=0, names=("no_memory",), *, settled=True) -> str:
        cands = [{"name": c, "valid": True, "rejected": None, "base_system": None, "axis": None, "hypothesis": None,
                  "components": None, "candidate_sha256": "d" * 64,
                  "datasets": {"USPTO": {"status": "succeeded", "accuracy": 0.5}}, "upstream_avg_val": 50.0,
                  "quarantined": False, "context": {"memory_context_chars_avg": 10, "prompt_tokens_max": 5}}
                 for c in names]
        util.write_json(self.paths.iteration_file(n), {
            "schema": "mhforge.iteration/1", "iteration": n, "kind": "baselines" if n == 0 else "scored",
            "frontier_before": None, "candidates": cands, "evidence_exposed": False,
            "timing_s": {"propose": 1.0, "bench": 2.0, "wall": 3.0}, "solver_cost_micro": 10,
            "sandbox_cost_micro": 20, "proposer_list_price_micro": 0,
            "started_utc": "2026-11-10T05:00:00.000000Z", "ended_utc": "2026-11-10T05:10:00.000000Z"})
        if settled:
            self.journal.record("iteration", f"iter{n:03d}", "settled", detail={"kind": "baselines"})
        return f"loop:iter{n:03d}"

    def everything(self) -> None:
        self.job()
        self.baseline_file()
        self.benchmark()
        self.session()
        self.iteration(0)

    def example_dir(self, *, extra_s2d: str = S2D_QUESTION) -> Path:
        """A stand-in example directory with the nine data files, and pristine.json holding their hashes."""
        ex = self.st.sc / "example"
        rows = {"uspto": [{"question": USPTO_QUESTION, "target": USPTO_TARGET}],
                "symptom_diagnosis": [{"question": extra_s2d, "answer": "Bronchitis"}],
                "crime_prediction": [{"question": LAW_QUESTION, "target": "Theft"}]}
        files = {}
        for d, data in rows.items():
            for split in ("train", "val", "test"):
                rel = f"data/{d}/{split}.jsonl"
                (ex / rel).parent.mkdir(parents=True, exist_ok=True)
                body = "".join(json.dumps(r) + "\n" for r in data).encode()
                (ex / rel).write_bytes(body)
                files[rel] = util.sha256_bytes(body)
        util.write_json(self.paths.pristine, {"schema": "mhforge.pristine/1", "files": files})
        self._example = ex
        return ex

    def ledger(self, manifest=None) -> Ledger:
        return Ledger(self.paths.ledger, manifest or self.st.manifest, clock=self.clock)

    def exporter(self, *, manifest=None, lens=None, wandb=None, dry_run=False, ledger=True, example_dir=_DEFAULT,
                 **kw) -> Exporter:
        """example_dir defaults to the stand-in example directory, so candidate files can be scanned and uploaded."""
        m = manifest or self.st.manifest
        if example_dir is _DEFAULT:
            example_dir = self._example or self.example_dir()
        return Exporter(m, self.paths, ledger=self.ledger(m) if ledger else None, lens=lens, wandb=wandb,
                        dry_run=dry_run, clock=self.clock, example_dir=example_dir, **kw)

    def unit(self, exporter: Exporter, name: str) -> ExportUnit:
        return next(u for u in exporter.discover() if u.unit == name)

    def receipt(self, unit: str, surface: str) -> dict | None:
        p = self.paths.receipt_file(unit, surface)
        return util.read_json(p) if p.exists() else None

    def meter_rows(self, meter: str) -> list[tuple]:
        if not self.paths.ledger.exists():
            return []
        import sqlite3
        db = sqlite3.connect(self.paths.ledger)
        try:
            return db.execute("SELECT key, quantity, bucket FROM meter_events WHERE meter=? ORDER BY key",
                              (meter,)).fetchall()
        finally:
            db.close()


def big_stream(n: int) -> list[dict]:
    """A stamped proposer stream with n assistant turns, each with one Bash tool use and its result."""
    lines = [json.dumps({"type": "system", "subtype": "init", "session_id": SESSION_ID, "model": "claude-opus-5-5",
                         "claude_code_version": "2.1.317", "tools": ["Bash"]})]
    for i in range(n):
        tid = f"toolu_big_{i:05d}"
        lines.append(json.dumps({"type": "assistant", "parent_tool_use_id": None, "message": {
            "id": f"msg_big_{i:05d}", "role": "assistant", "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": f"SYNTHETIC STEP {i}"},
                        {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": f"echo {i}"}}],
            "usage": {"input_tokens": 1, "output_tokens": 1}}}))
        lines.append(json.dumps({"type": "user", "parent_tool_use_id": None, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": f"SYNTHETIC OUTPUT {i}"}]}}))
    lines.append(json.dumps({"type": "result", "session_id": SESSION_ID, "is_error": False}))
    return helpers.stamp_rows(lines, "2026-11-10T04:00:00.000000Z", 0.01)


def statuses(receipts: dict) -> dict:
    return {s: r["status"] for s, r in receipts.items()}


class ExporterCase(unittest.TestCase):
    def setUp(self) -> None:
        self._ctx = helpers.tmp_state(manifest=True)
        self.st = self._ctx.__enter__()
        self.c = Campaign(self.st)
        self.m = self.st.manifest

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)

    def manifest(self, **overrides) -> dict:
        m = copy.deepcopy(self.m)
        for key, value in overrides.items():
            helpers._set_dotted(m, key.replace("__", "."), value)
        return m

    def export(self, unit: str, *, lens=None, wandb=None, manifest=None, **kw) -> dict:
        ex = self.c.exporter(lens=lens, wandb=wandb if wandb is not None else FakeWandbSink(), manifest=manifest,
                             **{k: v for k, v in kw.items() if k in ("dry_run", "known_secrets", "hostname",
                                                                    "example_dir", "amendment", "ledger")})
        opts = {k: v for k, v in kw.items() if k in ("retry_partial", "supersede")}
        return ex.export_unit(self.c.unit(ex, unit), **opts)


# ---------------------------------------------------------------- the F10 order

class Order(ExporterCase):
    def test_order_plan_scan_meter_emit_readback_receipt(self):
        self.c.job()
        seen = {}

        def probe(event):
            if event == "emit":
                seen["receipt"] = self.c.receipt(JOB, "agent_lens")
                seen["plan"] = (self.c.paths.plan_dir(JOB) / "spans.json").exists()
                seen["scan"] = self.c.paths.scan_file(JOB).exists()
                seen["meters"] = self.c.meter_rows("lens_bytes")
                seen["stored"] = self.c.paths.stored_spans_file(JOB).exists()

        lens = FakeLensSink(probe=probe)
        r = self.export(JOB, lens=lens)["agent_lens"]
        self.assertEqual([h["status"] for h in r["history"]], ["planned", "scanned", "emitting", "emitted", "verified"])
        self.assertTrue(seen["plan"] and seen["scan"])
        self.assertEqual(seen["meters"], [(f"{JOB}#e1", r["planned_bytes"], "main")])
        self.assertFalse(seen["stored"])                              # read back only after the emit
        self.assertTrue(self.c.paths.stored_spans_file(JOB).exists())
        self.assertEqual(r["readback"]["spans"], r["expected_spans"])
        self.assertEqual(r["readback"]["mismatches"], [])
        self.assertEqual(r["urls"], [f"https://forge.coreweave.com/agent-lens/example-entity/mh-forge-test/"
                                     f"conversations/mh-test.val.no_memory.USPTO.a1"])
        self.assertEqual(r["scan"]["classes"], ["secret", "privacy"])
        self.assertTrue(r["scan"]["clean"])

    def test_receipt_written_before_first_sdk_call(self):
        self.c.job()
        self.c.baseline_file()
        self.c.benchmark()
        seen = {}
        lens = FakeLensSink(probe=lambda e: seen.setdefault("lens", self.c.receipt(JOB, "agent_lens")) if e == "emit"
                            else None)

        class ProbeWandb(FakeWandbSink):
            def log_run(inner, run_plan, table_plans, files_root):
                seen["wandb"] = self.c.receipt("eval:val.no_memory", "wandb")
                return super().log_run(run_plan, table_plans, files_root)

        ex = self.c.exporter(lens=lens, wandb=ProbeWandb())
        ex.export_all()
        self.assertEqual(seen["lens"]["status"], "emitting")
        self.assertEqual(seen["lens"]["conversation_ids"], ["mh-test.val.no_memory.USPTO.a1"])
        self.assertIsInstance(seen["lens"]["expected_spans"], int)
        self.assertEqual(seen["lens"]["trace_ids"], [])
        self.assertEqual(seen["wandb"]["status"], "emitting")
        self.assertEqual(seen["wandb"]["remote"]["run_path"], "example-entity/mh-forge-test/mh-test-eval-val-no_memory")

    def test_trace_id_recorded_before_span_end(self):
        self.c.job()
        checks = []

        def probe(event):
            if event == "span_end":
                current = [e for e in lens.events if e[0] == "trace"][-1][2]
                checks.append(current in self.c.receipt(JOB, "agent_lens")["trace_ids"])

        lens = FakeLensSink(probe=probe)
        r = self.export(JOB, lens=lens)["agent_lens"]
        self.assertTrue(checks and all(checks))
        self.assertEqual(r["status"], "verified")
        self.assertEqual(r["traces"], {"mh-test.val.no_memory.USPTO.a1": r["trace_ids"]})


# ---------------------------------------------------------------- scan

class Scan(ExporterCase):
    def test_secret_blocks_before_anything_is_sent(self):
        self.c.session(text=CANDIDATE_TEXT + f"# {SECRET}\n")
        self.c.job(response_id=SECRET)
        lens, wb = FakeLensSink(), FakeWandbSink()
        ex = self.c.exporter(lens=lens, wandb=wb, known_secrets=[SECRET.encode()])
        out = ex.export_all(only=["propose:x", JOB])
        self.assertEqual(out["statuses"], {"propose:x": {"wandb": "refused_secret"}, JOB: {"agent_lens": "refused_secret"}})
        self.assertEqual(lens.events, [])
        self.assertEqual(wb.calls, [])                                # not even the project check
        self.assertEqual(self.c.meter_rows("lens_bytes") + self.c.meter_rows("wandb_upload_bytes"), [])
        report = util.read_json(self.c.paths.scan_file("propose:x"))["surfaces"]["wandb"]
        self.assertGreaterEqual(report["findings"], 1)
        self.assertIn("known_value", {d["pattern"] for d in report["detail"]})
        for p in self.c.paths.export_root.rglob("*.json"):
            if "plans" not in p.parts:
                self.assertNotIn(SECRET, p.read_text(), p.name)      # receipts and scan reports never hold it

    def test_scan_uses_key_digests(self):
        self.c.job(response_id=SECRET)
        util.append_key_digest(self.c.paths.keydigests, "MHFORGE_EXPORT_API_KEY", SECRET, clock=self.c.clock)
        lens = FakeLensSink()
        r = self.export(JOB, lens=lens)["agent_lens"]
        self.assertEqual(r["status"], "refused_secret")
        report = util.read_json(self.c.paths.scan_file(JOB))["surfaces"]["agent_lens"]
        self.assertIn("known_digest", {d["pattern"] for d in report["detail"]})
        self.assertEqual(lens.events, [])

    def test_error_text_never_keeps_part_of_a_secret(self):
        # regression: the error text was cut at 300 characters before the scan, so a key straddling the cut left
        # its prefix in the receipt; a key known only by its digest was not looked for at all
        key = "Zq7Lm2Xp9Rt4Vw8Yb3Nc6Hd1Jf5Kg0Ps"
        ex = self.c.exporter(lens=FakeLensSink(), wandb=FakeWandbSink(), known_secrets=[key.encode()])
        for pad in (250, 285, 299):
            text = ex._error(RuntimeError("x" * pad + " " + key))
            self.assertNotIn(key[:8], text)
            self.assertIn("withheld", text)
        other = "Pw4Tr8Ys2Ux6Iz0Oa3Sb7Dc1Fe5Gh9Jk"
        util.append_key_digest(self.c.paths.keydigests, "MHFORGE_EXPORT_API_KEY", other, clock=self.c.clock)
        self.assertNotIn(other, ex._error(RuntimeError(f"refused {other}")))
        self.assertEqual(ex._error(RuntimeError("plain failure")), "RuntimeError: plain failure")

    def test_privacy_finding_refuses(self):
        self.c.session(text=CANDIDATE_TEXT + "# data in /Users/alice/private/notes.txt\n")
        wb = FakeWandbSink()
        r = self.export("propose:x", wandb=wb)["wandb"]
        self.assertEqual(r["status"], "refused_secret")
        self.assertEqual(wb.uploads, [])
        self.c.session(iteration=2, candidates=("y",), text=CANDIDATE_TEXT + "# built on host build-box-7\n")
        r = self.export("propose:y", wandb=wb, hostname="build-box-7")["wandb"]
        self.assertEqual(r["status"], "refused_secret")
        report = util.read_json(self.c.paths.scan_file("propose:y"))["surfaces"]["wandb"]
        self.assertIn("hostname", {d["pattern"] for d in report["detail"]})
        self.assertNotIn(("log_run",), [c[:1] for c in wb.calls])


# ---------------------------------------------------------------- meters

class Meters(ExporterCase):
    def test_ingest_cap_skips_unit(self):
        self.c.job()
        led = self.c.ledger()
        led.meter("lens_bytes", "earlier#e1", 150 * MIB - 10, "bytes", bucket="main")
        lens = FakeLensSink()
        r = self.export(JOB, lens=lens)["agent_lens"]
        self.assertEqual(r["status"], "skipped_budget")
        self.assertEqual(lens.events, [])
        self.assertEqual([k for k, _q, _b in self.c.meter_rows("lens_bytes")], ["earlier#e1"])

    def test_empty_bucket_refuses_before_sending(self):
        m = self.manifest(**{"budgets.lens_ingest_split_mb": {"main": 0, "preflight": 5, "recovery": 170}})
        self.c.job()
        lens = FakeLensSink()
        r = self.export(JOB, lens=lens, manifest=m)["agent_lens"]
        self.assertEqual((r["status"], r["bucket"]), ("skipped_budget", "main"))
        self.assertEqual(lens.events, [])
        self.assertEqual(self.c.meter_rows("lens_bytes") + self.c.meter_rows("lens_spans"), [])

    def test_bucket_by_campaign_kind_and_recovery_for_retries(self):
        self.c.job()
        r = self.export(JOB, lens=FakeLensSink(drop=1))["agent_lens"]
        self.assertEqual((r["status"], r["bucket"]), ("partial", "main"))
        r = self.export(JOB, lens=FakeLensSink(), retry_partial=True)["agent_lens"]
        self.assertEqual((r["status"], r["bucket"], r["export_attempt"]), ("verified", "recovery", 2))
        self.assertEqual(self.c.meter_rows("lens_bytes"),
                         [(f"{JOB}#e1", r["planned_bytes"], "main"), (f"{JOB}#e2", r["planned_bytes"], "recovery")])
        with helpers.tmp_state(manifest=helpers.make_manifest(campaign_kind="preflight")) as st:
            c = Campaign(st)
            c.job()
            ex = c.exporter(lens=FakeLensSink(), wandb=FakeWandbSink())
            r = ex.export_unit(c.unit(ex, JOB))["agent_lens"]
            self.assertEqual((r["status"], r["bucket"]), ("verified", "preflight"))
            self.assertEqual(c.meter_rows("lens_bytes")[0][2], "preflight")

    def test_bytes_metered_once_per_attempt(self):
        self.c.job()

        class Crash(FakeLensSink):
            def emit(inner, plan, on_trace):
                raise KeyboardInterrupt                               # the process dies inside the SDK call

        with self.assertRaises(KeyboardInterrupt):
            self.export(JOB, lens=Crash())
        self.assertEqual(self.c.receipt(JOB, "agent_lens")["status"], "emitting")
        rows = self.c.meter_rows("lens_bytes")
        self.assertEqual(len(rows), 1)
        self.export(JOB, lens=FakeLensSink())                         # read back: partial, nothing was stored
        self.export(JOB, lens=FakeLensSink())                         # final: returned as it is
        self.assertEqual(self.c.meter_rows("lens_bytes"), rows)
        self.export(JOB, lens=FakeLensSink(), retry_partial=True)
        self.assertEqual([k for k, _q, _b in self.c.meter_rows("lens_bytes")], [f"{JOB}#e1", f"{JOB}#e2"])

    def test_span_count_metered(self):
        self.c.job()
        r = self.export(JOB, lens=FakeLensSink())["agent_lens"]
        plan = util.read_json(self.c.paths.plan_dir(JOB) / "spans.json")
        self.assertEqual(self.c.meter_rows("lens_spans"), [(f"{JOB}#e1", plan["expected_spans"], "main")])
        self.assertEqual(r["planned_spans"], plan["expected_spans"])
        self.assertEqual(r["planned_bytes"], plan["planned_bytes"])

    def test_proposer_unit_over_session_cap_skipped(self):
        m = self.manifest(**{"budgets.lens_proposer_session_mb": 1, "telemetry.agent_lens.byte_estimate_factor": "4"})
        unit = self.c.session(stream_rows=big_stream(700))
        lens = FakeLensSink()
        r = self.export(unit, lens=lens, manifest=m)["agent_lens"]
        self.assertGreater(r["planned_bytes"], MIB)
        self.assertEqual(r["status"], "skipped_budget")
        self.assertIn("lens_proposer_session_mb", r["error"])
        self.assertEqual(lens.events, [])
        self.assertEqual(self.c.meter_rows("lens_bytes"), [])

    def test_upload_and_table_caps(self):
        self.c.job()
        self.c.baseline_file()
        self.c.benchmark()
        led = self.c.ledger()
        led.meter("eval_tables", "earlier#e1#val/x", 250, "tables")
        wb = FakeWandbSink()
        ex = self.c.exporter(lens=FakeLensSink(), wandb=wb)
        ex.export_all()
        r = self.c.receipt("eval:val.no_memory", "wandb")
        self.assertEqual(r["status"], "skipped_budget")
        self.assertEqual([c for c in wb.calls if c[0] == "log_run"], [])
        self.c.session()
        led.meter("wandb_upload_bytes", "earlier#e1", 2 * 2 ** 30 - 5, "bytes")
        r = self.export("propose:x", wandb=wb)["wandb"]
        self.assertEqual(r["status"], "skipped_budget")
        self.assertEqual(wb.uploads, [])
        self.assertEqual([k for k, _q, _b in self.c.meter_rows("wandb_upload_bytes")], ["earlier#e1"])


# ---------------------------------------------------------------- emit and read back

class EmitAndReadBack(ExporterCase):
    def test_interrupted_export_reads_back_never_reemits(self):
        self.c.job()

        class DiesAfterSending(FakeLensSink):
            def emit(inner, plan, on_trace):
                super().emit(plan, on_trace)
                raise KeyboardInterrupt                               # killed before 'emitted' was written

        first = DiesAfterSending()
        with self.assertRaises(KeyboardInterrupt):
            self.export(JOB, lens=first)
        r = self.c.receipt(JOB, "agent_lens")
        self.assertEqual(r["status"], "emitting")
        turns = util.read_json(self.c.paths.plan_dir(JOB) / "spans.json")["conversations"][0]["turns"]
        self.assertEqual(len(r["trace_ids"]), len(turns))            # one trace per turn, each recorded
        second = FakeLensSink()
        second.store = first.store                                    # the same service
        r = self.export(JOB, lens=second)["agent_lens"]
        self.assertEqual(r["status"], "verified")
        self.assertEqual(second.emitted, [])                          # never emitted twice
        self.assertEqual(len(second.read_calls), 1)

    def test_short_or_long_readback_is_partial(self):
        self.c.job()
        r = self.export(JOB, lens=FakeLensSink(drop=1))["agent_lens"]
        self.assertEqual(r["status"], "partial")
        self.assertTrue(r["readback"]["mismatches"][0].startswith("count:"))
        timeout = self.m["telemetry"]["agent_lens"]["readback_timeout_s"]
        self.assertGreaterEqual(r["readback"]["waited_s"], timeout)   # polled until the timeout
        with helpers.tmp_state(manifest=True) as st:
            c = Campaign(st)
            c.job()
            ex = c.exporter(lens=FakeLensSink(extra=1), wandb=FakeWandbSink())
            r = ex.export_unit(c.unit(ex, JOB))["agent_lens"]
            self.assertEqual(r["status"], "partial")
            self.assertTrue(any(x.startswith("unexpected:") for x in r["readback"]["mismatches"]))
        with helpers.tmp_state(manifest=True) as st:                  # a slow store that catches up is verified
            c = Campaign(st)
            c.job()
            ex = c.exporter(lens=FakeLensSink(delay_polls=2), wandb=FakeWandbSink())
            r = ex.export_unit(c.unit(ex, JOB))["agent_lens"]
            self.assertEqual(r["status"], "verified")
            self.assertEqual(c.clock.sleeps, [2.0, 2.0])

    def test_retry_partial_uses_new_ids_and_archives(self):
        self.c.job()
        first = self.export(JOB, lens=FakeLensSink(drop=1))["agent_lens"]
        self.assertEqual(self.export(JOB, lens=FakeLensSink())["agent_lens"]["status"], "partial")   # final
        lens = FakeLensSink()
        r = self.export(JOB, lens=lens, retry_partial=True)["agent_lens"]
        self.assertEqual((r["status"], r["export_attempt"]), ("verified", 2))
        self.assertEqual(r["conversation_ids"], ["mh-test.val.no_memory.USPTO.a1.e2"])
        self.assertNotEqual(r["trace_ids"], first["trace_ids"])
        archived = self.c.paths.export_root / "attempts" / f"{unit_slug(JOB)}.agent_lens.e1.json"
        self.assertEqual(util.read_json(archived)["status"], "partial")
        self.assertEqual([c["conversation_id"] for c in lens.emitted[0]["conversations"]],
                         ["mh-test.val.no_memory.USPTO.a1.e2"])

    def test_verified_export_is_idempotent(self):
        self.c.everything()
        lens, wb = FakeLensSink(), FakeWandbSink()
        ex = self.c.exporter(lens=lens, wandb=wb)
        first = ex.export_all()
        meters = {m: self.c.meter_rows(m) for m in ("lens_bytes", "lens_spans", "wandb_upload_bytes", "eval_tables")}
        n_emits, n_logs = len(lens.emitted), len([c for c in wb.calls if c[0] == "log_run"])
        second = self.c.exporter(lens=lens, wandb=wb).export_all()
        self.assertEqual(first["receipts"], second["receipts"])
        self.assertTrue(all(s == "verified" for st in second["statuses"].values() for s in st.values()))
        self.assertEqual((len(lens.emitted), len([c for c in wb.calls if c[0] == "log_run"])), (n_emits, n_logs))
        self.assertEqual({m: self.c.meter_rows(m) for m in meters}, meters)

    def test_table_logged_once_per_location(self):
        self.c.job()
        self.c.baseline_file()
        self.c.benchmark()
        self.export(JOB, lens=FakeLensSink())
        wb = FakeWandbSink(drop_markers=True)
        r = self.export("eval:val.no_memory", wandb=wb)["wandb"]
        self.assertEqual(r["status"], "partial")
        self.export("eval:val.no_memory", wandb=wb)
        wb.drop_markers = False
        r = self.export("eval:val.no_memory", wandb=wb, retry_partial=True)["wandb"]
        self.assertEqual(r["status"], "verified")
        locations = [(p, k) for p, k, _b in wb.table_logs]
        self.assertEqual(len(locations), len(set(locations)))         # never twice at one location
        self.assertEqual(locations, [("example-entity/mh-forge-test/mh-test-eval-val-no_memory", "val/USPTO"),
                                     ("example-entity/mh-forge-test/mh-test-eval-val-no_memory-e2", "val/USPTO")])
        self.assertEqual(len(self.c.meter_rows("eval_tables")), 2)

    def test_delayed_summary_is_rechecked_without_another_upload_or_meter_charge(self):
        self.c.job()
        self.c.baseline_file()
        self.c.benchmark()
        self.export(JOB, lens=FakeLensSink())
        wb = FakeWandbSink(drop_markers=True)
        first = self.export("eval:val.no_memory", wandb=wb)["wandb"]
        path = first['remote']['run_path']
        wb.runs[path]['summary']['val/USPTO'] = {'_type': 'eval-table-ces'}
        second = self.export("eval:val.no_memory", wandb=wb, retry_partial=True)["wandb"]
        self.assertEqual(second['status'], 'verified')
        self.assertEqual(second['export_attempt'], 1)
        self.assertEqual(len(wb.table_logs), 1)
        self.assertEqual(len(self.c.meter_rows('eval_tables')), 1)
        self.assertTrue(second['readback_history'][0]['mismatches'])

    def test_finished_private_calibration_discovers_test_eval_runs(self):
        util.write_json(self.st.paths.calibration, {'systems': {
            'no_memory': {'complete': True}, 'fewshot_all': {'complete': False},
            'unrecognized': {'complete': True}}})
        ex = self.c.exporter(dry_run=True, ledger=False)
        self.assertNotIn('eval:test.no_memory', [u.unit for u in ex.discover()])
        self.c.journal.record('calibration', self.st.campaign, 'complete')
        self.assertEqual([u.unit for u in ex.discover()], ['eval:test.no_memory'])

    def test_ces_fallback_recorded(self):
        self.c.job()
        self.c.baseline_file()
        self.c.benchmark()
        self.export(JOB, lens=FakeLensSink())
        wb = FakeWandbSink(ces=False)
        r = self.export("eval:val.no_memory", wandb=wb)["wandb"]
        self.assertEqual(r["status"], "verified")
        self.assertEqual(r["remote"]["backend"], "wandb_table")
        self.assertIn("ces_unavailable: tables logged with backend wandb_table", r["problems"])
        self.assertEqual([b for _p, _k, b in wb.table_logs], ["wandb_table"])
        with helpers.tmp_state(manifest=True) as st:                  # one flaky probe answer is not a fallback here:
            c = Campaign(st)                                          # the adapter retries; the exporter asks once
            c.job()
            c.baseline_file()
            c.benchmark()
            wb = FakeWandbSink(ces=[True])
            c.exporter(lens=FakeLensSink(), wandb=wb).export_all()
            self.assertEqual(c.receipt("eval:val.no_memory", "wandb")["remote"]["backend"], "ces")

    def test_eval_table_gets_trace_url_only_from_verified_receipt(self):
        self.c.job()
        self.c.baseline_file()
        self.c.benchmark()
        self.export(JOB, lens=FakeLensSink(drop=1))                   # partial: no trace_url
        ex = self.c.exporter(lens=FakeLensSink(), wandb=FakeWandbSink(), dry_run=True, ledger=False)
        ex.export_unit(self.c.unit(ex, "eval:val.no_memory"))
        table = util.read_json(self.c.paths.plan_dir("eval:val.no_memory") / "tables.json")["tables"][0]
        i = table["columns"].index("trace_url")
        self.assertEqual({row[i] for row in table["rows"]}, {""})


# ---------------------------------------------------------------- dry runs and resume rules

class DryRunAndResume(ExporterCase):
    def test_dry_run_needs_no_sink_and_no_key(self):
        self.c.everything()
        ex = Exporter(self.m, self.c.paths, ledger=None, lens=None, wandb=None, dry_run=True, clock=self.c.clock)
        out = ex.export_all()
        self.assertEqual({s for st in out["statuses"].values() for s in st.values()}, {"dry_run"})
        self.assertEqual(len(out["statuses"]), 5)
        self.assertFalse(self.c.paths.ledger.exists())
        for unit in out["statuses"]:
            self.assertTrue(self.c.paths.plan_dir(unit).is_dir())
            self.assertTrue(self.c.paths.scan_file(unit).exists())
        with self.assertRaises(ValueError):                           # a live export needs the ledger
            Exporter(self.m, self.c.paths, ledger=None, lens=FakeLensSink(), wandb=FakeWandbSink())

    def test_dry_run_with_unset_decisions(self):
        m = self.manifest(**{"decisions.entity": None, "decisions.project": None,
                             "decisions.evaltables_backend": "unset"})
        self.c.everything()
        out = self.c.exporter(manifest=m, dry_run=True, ledger=False).export_all()
        self.assertEqual({s for st in out["statuses"].values() for s in st.values()}, {"dry_run"})
        spans = util.read_json(self.c.paths.plan_dir(JOB) / "spans.json")
        self.assertTrue(spans["placeholder_project"])
        self.assertEqual(spans["project"], "unset-entity/unset-project")
        tables = util.read_json(self.c.paths.plan_dir("eval:val.no_memory") / "tables.json")
        self.assertEqual(tables["backend"], "unset")
        lens, wb = FakeLensSink(), FakeWandbSink()
        out = self.c.exporter(manifest=m, lens=lens, wandb=wb).export_all()
        self.assertEqual({s for st in out["statuses"].values() for s in st.values()}, {"skipped_policy"})
        self.assertEqual((lens.events, wb.calls), ([], []))

    def test_live_export_replaces_dry_run_receipt(self):
        self.c.job()
        dry = self.export(JOB, dry_run=True, ledger=False)["agent_lens"]
        self.assertEqual(dry["status"], "dry_run")
        r = self.export(JOB, lens=FakeLensSink())["agent_lens"]
        self.assertEqual((r["status"], r["export_attempt"]), ("verified", 1))
        self.assertEqual(r["conversation_ids"], ["mh-test.val.no_memory.USPTO.a1"])
        archived = self.c.paths.export_root / "attempts" / f"{unit_slug(JOB)}.agent_lens.e1.json"
        self.assertEqual(util.read_json(archived)["status"], "dry_run")
        self.assertEqual(self.export(JOB, dry_run=True, ledger=False)["agent_lens"]["status"], "verified")

    def test_changed_plan_returns_final_receipt_marked_stale(self):
        self.c.session()
        wb = FakeWandbSink()
        r1 = self.export("propose:x", wandb=wb)["wandb"]
        self.assertEqual((r1["status"], r1["stale_plan_sha256"]), ("verified", None))
        prop = self.c.paths.session_dir(1, 1) / "proposal.json"
        doc = util.read_json(prop)
        doc["candidates"][0]["hypothesis"] = "a re-proposed hypothesis"
        util.write_json(prop, doc)
        r2 = self.export("propose:x", wandb=wb)["wandb"]
        self.assertEqual(r2["status"], "verified")
        self.assertEqual(r2["plan_sha256"], r1["plan_sha256"])
        self.assertIsNotNone(r2["stale_plan_sha256"])
        self.assertNotEqual(r2["stale_plan_sha256"], r1["plan_sha256"])
        self.assertEqual(len([c for c in wb.calls if c[0] == "log_run"]), 1)
        r3 = self.export("propose:x", wandb=wb, retry_partial=True)["wandb"]      # not partial: still not re-sent
        self.assertEqual(r3["export_attempt"], 1)

    def test_supersede_starts_new_attempt_with_new_ids(self):
        self.c.session()
        wb = FakeWandbSink()
        self.export("propose:x", wandb=wb)
        prop = self.c.paths.session_dir(1, 1) / "proposal.json"
        doc = util.read_json(prop)
        doc["candidates"][0]["hypothesis"] = "a re-proposed hypothesis"
        util.write_json(prop, doc)
        r = self.export("propose:x", wandb=wb, supersede=True)["wandb"]
        self.assertEqual((r["status"], r["export_attempt"], r["stale_plan_sha256"]), ("verified", 2, None))
        self.assertEqual(r["remote"]["run_path"], "example-entity/mh-forge-test/mh-test-propose-x-e2")
        self.assertTrue((self.c.paths.export_root / "attempts" / f"{unit_slug('propose:x')}.wandb.e1.json").exists())
        out = self.c.exporter(wandb=wb, lens=FakeLensSink()).export_all(supersede=["propose:x"])   # not stale now
        self.assertEqual(out["receipts"]["propose:x"]["wandb"]["export_attempt"], 2)

    def test_attempts_are_bounded(self):
        self.c.job()
        for _ in range(5):
            r = self.export(JOB, lens=FakeLensSink(drop=1), retry_partial=True)["agent_lens"]
        self.assertEqual((r["status"], r["export_attempt"]), ("partial", exporter_mod.MAX_EXPORT_ATTEMPTS))

    def test_receipt_records_amendment(self):
        self.c.job()
        amendment = {"seq": 2, "row_sha256": "ab" * 32, "set": {"x": 1}}
        r = self.export(JOB, lens=FakeLensSink(), amendment=amendment)["agent_lens"]
        self.assertEqual(r["amendment"], {"seq": 2, "row_sha256": "ab" * 32})
        with helpers.tmp_state(manifest=True) as st:
            c = Campaign(st)
            c.job()
            ex = c.exporter(lens=FakeLensSink(), wandb=FakeWandbSink())
            self.assertEqual(ex.export_unit(c.unit(ex, JOB))["agent_lens"]["amendment"], {"seq": 0, "row_sha256": None})

    def test_amended_rule_marks_receipt_stale(self):
        self.c.job()
        r1 = self.export(JOB, lens=FakeLensSink())["agent_lens"]
        amended = self.manifest(**{"decisions.dataset_text_publication": "per_dataset",
                                   "decisions.uspto_text_cleared": True,
                                   "telemetry.content.USPTO.span_prompt": "full"})
        lens = FakeLensSink()
        r2 = self.export(JOB, lens=lens, manifest=amended, amendment={"seq": 1, "row_sha256": "cd" * 32})["agent_lens"]
        self.assertEqual(r2["status"], "verified")
        self.assertEqual(r2["plan_sha256"], r1["plan_sha256"])
        self.assertIsNotNone(r2["stale_plan_sha256"])
        self.assertEqual(r2["amendment"], {"seq": 0, "row_sha256": None})   # the receipt names what it was made under
        self.assertEqual(lens.emitted, [])


# ---------------------------------------------------------------- discover

class Discover(ExporterCase):
    def test_units_only_for_finished_work(self):
        self.c.job(journal_status=None)                               # still running
        self.c.benchmark(complete=False)
        self.c.session(record=False)                                  # no session.json yet, but a proposal
        self.c.iteration(1, settled=False)
        units = {u.unit for u in self.c.exporter(dry_run=True, ledger=False).discover()}
        self.assertEqual(units, {"propose:x"})
        for status in ("complete", "failed", "interrupted"):
            with self.subTest(status=status):
                with helpers.tmp_state(manifest=True) as st:
                    c = Campaign(st)
                    c.job(journal_status=status)
                    self.assertEqual([u.unit for u in c.exporter(dry_run=True, ledger=False).discover()], [JOB])

    def test_eval_waits_for_job_units(self):
        self.c.job()
        self.c.baseline_file()
        self.c.benchmark()
        self.c.session(candidates=("x",))
        self.c.job(candidate="x")
        self.c.benchmark(candidate="x")
        ex = self.c.exporter(lens=FakeLensSink(), wandb=FakeWandbSink())
        units = {u.unit: u for u in ex.discover()}
        self.assertEqual(units["eval:val.no_memory"].depends_on, (JOB,))
        self.assertEqual(units["eval:val.x"].depends_on, ("job:val.x.USPTO.a1", "propose:x"))
        out = ex.export_all(only=["eval:val.no_memory", "eval:val.x"])
        self.assertEqual(sorted(out["deferred"]), ["eval:val.no_memory", "eval:val.x"])
        self.assertIsNone(self.c.receipt("eval:val.no_memory", "wandb"))
        out = ex.export_all()
        self.assertEqual(out["deferred"], [])
        self.assertEqual(out["statuses"]["eval:val.x"], {"wandb": "verified"})
        order = list(out["statuses"])
        self.assertLess(order.index(JOB), order.index("eval:val.no_memory"))

    def test_interrupted_job_unit_plans(self):
        unit = self.c.job(journal_status="interrupted", record=False)
        d = self.c.paths.calls_dir("val.no_memory.USPTO.a1")
        for p in d.glob("*.json"):                                    # admission-only: no outcome, no end, no reply
            rec = util.read_json(p)
            for k in ("outcome", "ended_utc", "reply", "response", "usage"):
                rec.pop(k, None)
            util.write_json(p, rec)
        lens = FakeLensSink()
        r = self.export(unit, lens=lens)["agent_lens"]
        self.assertEqual(r["status"], "verified")
        plan = util.read_json(self.c.paths.plan_dir(unit) / "spans.json")
        outcomes = {s["attributes"]["metaharness.outcome"] for t in plan["conversations"][0]["turns"] for s in t["spans"]}
        self.assertEqual(outcomes, {"interrupted"})

    def test_export_runs_with_halt_set(self):
        self.c.everything()
        self.c.ledger().set_halt("operator", "stopped by hand")
        out = self.c.exporter(lens=FakeLensSink(), wandb=FakeWandbSink()).export_all()
        self.assertEqual({s for st in out["statuses"].values() for s in st.values()}, {"verified"})


# ---------------------------------------------------------------- what may leave the machine

class Content(ExporterCase):
    def test_candidate_file_with_dataset_text_is_withheld(self):
        ex_dir = self.c.example_dir()
        unit = self.c.session(text=CANDIDATE_TEXT + f'EXAMPLE = "{S2D_QUESTION}"\n')
        wb = FakeWandbSink()
        r = self.export(unit.replace("proposer:iter001.s1", "propose:x"), wandb=wb, example_dir=ex_dir)["wandb"]
        self.assertEqual(r["status"], "verified")
        body = (self.c.paths.session_dir(1, 1) / "candidates" / "x.py").read_bytes()
        self.assertEqual(r["withheld_files"], [{"name": "x.py", "sha256": util.sha256_bytes(body), "bytes": len(body)}])
        self.assertEqual(wb.uploads, [])
        self.assertEqual(wb.artifacts["candidate-x"][0]["files"], [])
        self.assertEqual(wb.artifacts["candidate-x"][0]["metadata"]["file_sha256"], util.sha256_bytes(body))
        self.c.session(iteration=2, candidates=("y",))                # a clean file is uploaded
        self.export("propose:y", wandb=wb, example_dir=ex_dir)
        self.assertEqual([s for s, _b in wb.uploads], ["proposer/iter-002/s1/candidates/y.py"])
        self.c.job()                                                  # no index to scan with: withheld (fail closed)
        self.c.baseline_file()
        self.c.benchmark()
        r = self.export("eval:val.no_memory", wandb=wb, example_dir=None)["wandb"]
        self.assertEqual((r["status"], [w["name"] for w in r["withheld_files"]]), ("verified", ["no_memory.py"]))
        self.assertEqual(len(wb.uploads), 1)

    def test_live_propose_without_verbatim_index_is_refused(self):
        # regression: without example_dir the proposer's Artifact metadata (hypothesis, axis, components) was sent
        # unscanned, so a hypothesis quoting USPTO question text left the machine in clear
        leak = f"try examples like: {USPTO_QUESTION}"
        self.c.session(hypothesis=leak)
        wb = FakeWandbSink()
        r = self.export("propose:x", wandb=wb, example_dir=None)["wandb"]
        self.assertEqual(r["status"], "failed")
        self.assertTrue(any("verbatim index" in p for p in r["problems"]))
        self.assertEqual((wb.runs, wb.artifacts, wb.uploads), ({}, {}, []))
        self.assertEqual(self.c.meter_rows("wandb_upload_bytes"), [])
        r = self.export("propose:x", wandb=wb, example_dir=self.c.example_dir(), retry_partial=True)["wandb"]
        self.assertEqual(r["status"], "verified")
        self.assertNotIn(leak, json.dumps(wb.artifacts))                  # with the index it is hashed
        with helpers.tmp_state(manifest=True) as st:                      # a dry run still plans (nothing is sent)
            c = Campaign(st)
            c.session(hypothesis=leak)
            ex = c.exporter(dry_run=True, ledger=False, example_dir=None)
            self.assertEqual(ex.export_unit(c.unit(ex, "propose:x"))["wandb"]["status"], "dry_run")

    def test_memory_log_cache_and_transcript_never_uploaded(self):
        self.c.everything()
        lens, wb = FakeLensSink(), FakeWandbSink()
        self.c.exporter(lens=lens, wandb=wb).export_all()
        self.assertEqual(sorted(s for s, _b in wb.uploads), ["proposer/iter-001/s1/candidates/x.py",
                                                            "workspace/text_classification/agents/no_memory.py"])
        sent = json.dumps([wb.runs, lens.all_rows(), [b.decode() for _s, b in wb.uploads]], default=str)
        for text in ("TRAINING SPLIT VERBATIM", "CACHED REPLY TEXT", "SYNTHETIC THINKING", "SYNTHETIC SUBAGENT REPORT"):
            self.assertNotIn(text, sent)
        summary = wb.runs["example-entity/mh-forge-test/mh-test-eval-val-no_memory"]["summary"]
        evidence = json.loads(summary["evidence_hashes"])["USPTO"]
        memory = self.c.paths.attempt_dir("val.no_memory.USPTO", 1) / "out" / "memory.json"
        self.assertEqual(evidence["memory_json_sha256"], util.sha256_file(memory))
        self.assertEqual(evidence["memory_json_bytes"], memory.stat().st_size)

    def test_verbatim_index_exact_for_listed_datasets(self):
        from mhforge import scan
        ex_dir = self.c.example_dir()
        ex = self.c.exporter(dry_run=True, ledger=False, example_dir=ex_dir)
        index = ex.verbatim_index()
        self.assertIs(ex.verbatim_index(), index)                     # built once
        self.assertIn(USPTO_TARGET, index.exact)
        self.assertIn(USPTO_QUESTION, index.exact)
        self.assertNotIn(S2D_QUESTION, index.exact)
        self.assertEqual({f.pattern for f in scan.scan_verbatim(f"x {USPTO_TARGET} y", index)}, {"dataset_text_exact"})
        self.assertEqual({f.pattern for f in scan.scan_verbatim(f"say: {S2D_QUESTION[3:60]}", index)},
                         {"dataset_text_run"})
        self.assertEqual({f.pattern for f in scan.scan_verbatim(f"law: {LAW_QUESTION[:50]}", index)},
                         {"dataset_text_run"})
        opened = self.manifest(**{"decisions.dataset_text_publication": "per_dataset",
                                  **{f"telemetry.content.Symptom2Disease.{f}": "full"
                                     for f in ("span_prompt", "span_reply", "span_reasoning", "tool_result")}})
        index = self.c.exporter(manifest=opened, dry_run=True, ledger=False, example_dir=ex_dir).verbatim_index()
        self.assertEqual(scan.scan_verbatim(S2D_QUESTION, index), [])  # a fully open dataset is not indexed
        self.assertIsNone(self.c.exporter(dry_run=True, ledger=False, example_dir=None).verbatim_index())
        (ex_dir / "data" / "uspto" / "val.jsonl").write_text('{"question": "changed", "target": "changed"}\n')
        with self.assertRaises(ExportError):                          # hash-checked against pristine.json
            self.c.exporter(dry_run=True, ledger=False, example_dir=ex_dir).verbatim_index()

    def test_live_proposer_without_verbatim_index_fails_closed(self):
        opened = self.manifest(**{"decisions.dataset_text_publication": "per_dataset",
                                  "telemetry.content.Symptom2Disease.tool_result": "full"})
        unit = self.c.session()
        lens = FakeLensSink()
        r = self.export(unit, lens=lens, manifest=opened, example_dir=None)["agent_lens"]
        self.assertEqual(r["status"], "failed")
        self.assertTrue(any("verbatim index" in p for p in r["problems"]))
        self.assertEqual(lens.events, [])
        r = self.export(unit, lens=FakeLensSink(), manifest=opened, example_dir=self.c.example_dir(),
                        retry_partial=True)["agent_lens"]
        self.assertEqual(r["status"], "verified")


# ---------------------------------------------------------------- project and lock

class ProjectAndLock(ExporterCase):
    def test_export_does_not_create_a_project(self):
        self.c.everything()
        lens, wb = FakeLensSink(), FakeWandbSink(projects={})
        with self.assertRaises(ExportError):
            self.c.exporter(lens=lens, wandb=wb).export_all()
        self.assertEqual([c[0] for c in wb.calls], ["project_access"])
        self.assertEqual((wb.created_projects, lens.events), ([], []))
        self.assertEqual(self.c.meter_rows("lens_bytes"), [])
        public = {("example-entity", "mh-forge-test"): {"access": "USER_READ", "read_only": False, "can_write": True}}
        m = self.manifest(**{"decisions.publish_approved": False})
        with self.assertRaises(ExportError):
            self.c.exporter(manifest=m, lens=lens, wandb=FakeWandbSink(projects=public)).export_all()
        read_only = {("example-entity", "mh-forge-test"): {**PRIVATE, "read_only": True}}
        with self.assertRaises(ExportError):
            self.c.exporter(lens=lens, wandb=FakeWandbSink(projects=read_only)).export_all()
        self.assertEqual(lens.events, [])
        out = self.c.exporter(manifest=m, lens=lens, wandb=FakeWandbSink()).export_all()   # PRIVATE: allowed
        self.assertEqual({s for st in out["statuses"].values() for s in st.values()}, {"verified"})

    def test_one_exporter_at_a_time(self):
        self.c.job()
        with StateLock(self.c.paths.export_lock, "export"):
            with self.assertRaises(StateLockError):
                self.c.exporter(lens=FakeLensSink(), wandb=FakeWandbSink()).export_all()
        self.assertIsNone(self.c.receipt(JOB, "agent_lens"))

    def test_final_statuses(self):
        self.assertEqual(final_statuses(), frozenset({"verified", "refused_secret", "skipped_budget", "skipped_policy",
                                                      "partial", "failed", "dry_run"}))

    def test_sink_failure_before_sending_is_failed_after_sending_is_read_back(self):
        self.c.job()
        r = self.export(JOB, lens=FakeLensSink(fail_before=True))["agent_lens"]
        self.assertEqual(r["status"], "failed")
        self.assertIn("RuntimeError", r["error"])
        with helpers.tmp_state(manifest=True) as st:
            c = Campaign(st)
            c.job()
            ex = c.exporter(lens=FakeLensSink(fail_after=True), wandb=FakeWandbSink())
            r = ex.export_unit(c.unit(ex, JOB))["agent_lens"]
            self.assertEqual(r["status"], "verified")                 # everything was stored; the read-back decides
        self.c.session()
        r = self.export("propose:x", wandb=FakeWandbSink(fail_before=True))["wandb"]
        self.assertEqual(r["status"], "failed")


if __name__ == "__main__":
    unittest.main()
