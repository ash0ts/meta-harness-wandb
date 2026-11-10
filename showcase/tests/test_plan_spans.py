# SPDX-License-Identifier: MIT
"""Span planners of spec 3.23 (solver job, proposer session, outer loop) and validate_plan.

test_partial_usage_does_not_invent_zero_tokens is rewritten from H2 tests/showcase/test_telemetry.py
(test_partial_usage_does_not_invent_zero_output_tokens) to unittest against the plan instead of an SDK span."""

from __future__ import annotations

import copy
import glob
import hashlib
import json
import math
import random
import re
import unittest
from decimal import Decimal
from pathlib import Path

from mhforge import scan, util, wire
from mhforge.telemetry import plan_spans as ps
from mhforge.telemetry import policy, transcript
from mhforge.telemetry.transcript import ApiMessage, NormalizedSession, ToolUse
from tests import helpers

FIX = helpers.FIXTURES
HASH_RE = re.compile(r"\[sha256 [0-9a-f]{64} (chars|bytes) [0-9]+\]")
SESSION_ID = "5e55104e-0000-4000-8000-00000000abcd"
BASE = 1794280000.0
JOB_ID = "val.no_memory.USPTO.a1"


def fixture_records() -> list[dict]:
    return [json.loads(Path(p).read_text()) for p in sorted(glob.glob(str(FIX / "call_records" / "*.json")))]


def by_outcome(records) -> dict:
    return {r["outcome"]: r for r in records if r["outcome"] != "completed"} | \
        {"completed": next(r for r in records if r["outcome"] == "completed" and not r["flags"]["truncated_empty"])}


def sidecar_fixture() -> list[dict]:
    return util.read_jsonl(FIX / "sidecar_uspto_val.jsonl")


def job_record(dataset="USPTO", split="val", candidate="no_memory", attempt=1, ended="2026-11-10T03:17:00.000000Z"):
    key = f"{split}.{candidate}.{dataset}"
    return {"schema": "mhforge.job/1", "campaign": "mh-test", "job_key": key, "split": split, "candidate": candidate,
            "dataset": dataset, "phase": "baseline", "candidate_sha256": "ab" * 32,
            "attempts": [{"attempt": attempt, "job_id": f"{key}.a{attempt}", "started_utc": "2026-11-10T03:14:00.000000Z",
                          "ended_utc": ended, "outcome": "ok", "candidate_sha256": "ab" * 32}],
            "result": {"total": 4, "correct": 2}}


def full_rules(*datasets, field_rule="full") -> dict:
    return {f"telemetry.content.{d}.{f}": field_rule for d in datasets for f in wire.CONTENT_FIELDS}


def synth_record(i: int, *, outcome="completed", t0=BASE, job_id=JOB_ID, prompt=None) -> dict:
    prompt = prompt or f"SYNTHETIC PROMPT {i}"
    received = util.utc_iso(t0 + 2 * i)
    ended = util.utc_iso(t0 + 2 * i + 1)
    return {"schema": "mhforge.call/1", "call_id": f"c-20261110T000000-{i:012x}",
            "job": {"job_id": job_id}, "outcome": outcome, "refusal": None,
            "received_utc": received, "ended_utc": ended,
            "request": {"model": "openai/gpt-oss-120b", "max_tokens": 16384, "temperature": 0.0,
                        "messages": [{"role": "system", "content": "Reasoning: medium"},
                                     {"role": "user", "content": prompt}]},
            "user_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "attempts": [{"n": 1}],
            "response": {"id": f"r{i}", "model": "openai/gpt-oss-120b",
                         "choices": [{"index": 0, "finish_reason": "stop",
                                      "message": {"role": "assistant", "content": f"REPLY {i}"}}]},
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, "finish_reason": "stop",
            "response_model": "openai/gpt-oss-120b", "response_id": f"r{i}",
            "flags": {"truncated_empty": False}, "reply": {"status": 200, "code": None, "source": "provider"}}


def synth_sidecar(records, n_learn: int) -> list[dict]:
    """A sidecar whose first n_learn call rows precede every eval (role learn); each later call is one val example."""
    rows = [{"kind": "meta", "seq": 1, "schema": "mhforge.sidecar/1", "t": BASE},
            {"kind": "splits", "seq": 2, "t": BASE, "dataset": "USPTO", "wanted": ["val"]}]
    seq = 3
    for i, r in enumerate(records):
        rows.append({"kind": "call", "seq": seq, "user_sha256": r["user_sha256"], "cache_hit": False, "error": None})
        seq += 1
        if i >= n_learn:
            h = hashlib.sha256(str(i).encode()).hexdigest()
            rows.append({"kind": "eval", "seq": seq, "t": BASE + 2 * i + 1.5, "split": "val", "index": i - n_learn,
                         "pass": 1, "call_seqs": [seq - 1], "batch_user_sha256": [], "was_correct": i % 2 == 0,
                         "prediction": "p", "target": "t", "example_sha256": h, "question_sha256": h,
                         "prediction_sha256": h, "target_sha256": h, "metrics": {}, "ambiguous": False})
            seq += 1
    rows.append({"kind": "done", "seq": seq, "exit": 0})
    return rows


def all_spans(plan):
    def walk(spans):
        for s in spans:
            yield s
            yield from walk(s.get("spans") or ())
    for c in plan["conversations"]:
        for t in c["turns"]:
            yield from walk(t["spans"])


def content_strings(plan):
    for s in all_spans(plan):
        for field in ("input_messages", "output_messages"):
            for m in s.get(field) or ():
                for p in m["parts"]:
                    yield p["content"]
        if s["type"] == "tool":
            yield s["arguments"]
            yield s["result"]


def fixture_session() -> NormalizedSession:
    records, subs = transcript.read_session_dir(FIX / "transcript_synthetic", SESSION_ID, max_bytes=10 ** 7)
    return transcript.normalize_transcript(records, expect_version="2.1.317", subagents=subs)


def session_record(*, present=True, key="iter007.s1") -> dict:
    return {"schema": "mhforge.session/1", "campaign": "mh-test", "iteration": 7, "ordinal": 1, "session_key": key,
            "permission_mode": "upstream", "cli": {"version": "2.1.317"}, "outcome": "ok" if present else "interrupted",
            "started_utc": "2026-11-10T01:00:00.000000Z", "ended_utc": "2026-11-10T01:01:00.000000Z",
            "resolved_model": "claude-opus-5-5", "result_event": {"present": present}}


EMPTY_INDEX = scan.build_verbatim_index([], run_chars=40)               # an export's index that holds no text
USPTO_QUESTION = ("SYNTHETIC PATENT QUESTION: name the reactants that give CCOC(=O)c1ccccc1 under acid catalysis "
                  "in ethanol at reflux for six hours")

ROOTS = {"workspace": Path("/Users/example/work/meta-harness/reference_examples/text_classification"),
         "state": Path("/Users/example/work/meta-harness/showcase/state/mh-test")}


def msg(mid, t, *, tools=(), sidechain=False, agent=None, text="", model="claude-opus-5-5") -> ApiMessage:
    return ApiMessage(message_id=mid, request_id=None, request_ref=None, model=model, effort="max",
                      started_utc=util.utc_iso(t), ended_utc=util.utc_iso(t + 0.5), input_tokens_total=100,
                      input_tokens=10, output_tokens=5, cache_read=60, cache_creation=30, cache_creation_5m=0,
                      cache_creation_1h=30, text=text, thinking_chars=0, tool_uses=tuple(tools), stop_reason="end_turn",
                      sidechain=sidechain, agent_id=agent)


def tool(tid, t, *, name="Bash", inp=None, result="ok") -> ToolUse:
    return ToolUse(id=tid, name=name, input=inp if inp is not None else {"command": "ls"}, result_text=result,
                   is_error=False, started_utc=util.utc_iso(t + 0.5), ended_utc=util.utc_iso(t + 0.6), sandboxed=None)


def session_of(messages, links=None, source="transcript") -> NormalizedSession:
    return NormalizedSession(session_id="s", source=source, cli_version="2.1.317", prompt_text="SYNTHETIC TASK PROMPT",
                             messages=tuple(messages), started_utc=messages[0].started_utc if messages else None,
                             ended_utc=messages[-1].ended_utc if messages else None, cost_state=None,
                             skipped_types={}, warnings=(), agent_links=dict(links or {}))


def iteration_record(nnn=3) -> dict:
    return {"schema": "mhforge.iteration/1", "iteration": nnn, "kind": "scored", "session_key": f"iter{nnn:03d}.s1",
            "frontier_before": {"system": "fewshot_all", "val_accuracy": 41.3},
            "candidates": [
                {"name": "x", "valid": True, "rejected": None, "base_system": "fewshot_all", "axis": "exploration",
                 "hypothesis": "Keep a short list of hard examples", "components": ["retrieval", "notes"],
                 "datasets": {"USPTO": {"status": "succeeded", "accuracy": 0.4},
                              "LawBench": {"status": "failed", "outcome": "worker_error", "failure_class": "context_limit"}},
                 "avg_val_all_datasets": 40.0, "upstream_avg_val": 60.0, "complete": False, "delta_vs_previous_best": -1.3},
                {"name": "y", "valid": True, "rejected": None, "base_system": "no_memory", "axis": "exploitation",
                 "hypothesis": "Summaries", "components": [], "datasets": {}, "complete": True},
                {"name": "z", "valid": False, "rejected": "import", "base_system": "no_memory", "axis": "a",
                 "hypothesis": "h", "components": []}],
            "best_complete_before": 41.3, "best_complete_after": 41.3, "improvement": 0.0, "evidence_exposed": False,
            "proposer_list_price_micro": 12310000, "solver_cost_micro": 1, "sandbox_cost_micro": 2,
            "started_utc": "2026-11-10T01:00:00.000000Z", "ended_utc": "2026-11-10T02:00:00.000000Z"}


def journal(nnn=3) -> list[dict]:
    k = f"iter{nnn:03d}"
    rows = [("proposer", k, "started", "01:00:01"), ("proposer", k, "complete", "01:20:00"),
            ("import_check", k, "started", "01:20:01"), ("import_check", k, "complete", "01:21:00"),
            ("benchmark", "val.x", "started", "01:21:01"), ("benchmark", "val.x", "complete", "01:40:00"),
            ("benchmark", "val.y", "started", "01:40:01"), ("benchmark", "val.y", "complete", "01:58:00"),
            ("iteration", k, "settled", "01:59:00")]
    return [{"schema": "mhforge.journal/1", "seq": i + 1, "utc": f"2026-11-10T{t}.000000Z", "op": op, "key": key,
             "status": st, "attempt": 1, "detail": {"outcome": "ok"} if op == "proposer" and st == "complete" else {}}
            for i, (op, key, st, t) in enumerate(rows)]


class SolverJobTest(unittest.TestCase):
    def setUp(self):
        self.manifest = helpers.make_manifest()
        self.records = fixture_records()

    def plan(self, records=None, sidecar=None, manifest=None, job=None, **kw):
        return ps.plan_solver_job(job or job_record(), 1, self.records if records is None else records,
                                  sidecar_fixture() if sidecar is None else sidecar, manifest or self.manifest, **kw)

    def spans(self, plan):
        return {s["key"]: s for s in all_spans(plan)}

    def test_one_llm_span_per_call_record_with_original_times(self):
        spans = self.spans(self.plan())
        self.assertEqual(sorted(spans), sorted(r["call_id"] for r in self.records))
        for r in self.records:
            s = spans[r["call_id"]]
            self.assertEqual(s["type"], "llm")
            self.assertEqual((s["started_utc"], s["ended_utc"]), (r["received_utc"], r["ended_utc"]))
            self.assertEqual(s["attributes"]["metaharness.outcome"], r["outcome"])
            self.assertEqual(s["attributes"]["metaharness.provider_attempts"], len(r["attempts"]))

    def test_turns_by_role(self):
        plan = self.plan()
        turns = plan["conversations"][0]["turns"]
        self.assertEqual([t["turn_key"] for t in turns], ["learn", "validate"])
        learn = {s["attributes"]["metaharness.outcome"] for s in turns[0]["spans"]}
        self.assertEqual(learn, {"provider_rejected", "failed"})
        self.assertEqual(turns[1]["attributes"]["metaharness.examples"], 4)
        self.assertEqual(turns[1]["attributes"]["metaharness.correct"], 2)
        self.assertEqual(turns[1]["attributes"]["metaharness.replayed_calls"], 1)
        self.assertEqual(turns[1]["attributes"]["metaharness.refused_calls"], 1)
        # a record no sidecar row claims, after the first evaluation, is an "other" call
        extra = synth_record(99, t0=util.parse_utc("2026-11-10T03:16:30.000000Z") - 198)
        extra["job"] = {"job_id": JOB_ID}
        turns = self.plan(records=self.records + [extra])["conversations"][0]["turns"]
        self.assertEqual([t["turn_key"] for t in turns], ["learn", "validate", "other"])
        self.assertEqual([s["key"] for s in turns[2]["spans"]], [extra["call_id"]])
        # a test job's main turn is "test"
        test_recs = [synth_record(i, job_id="test.no_memory.USPTO.a1") for i in range(3)]
        side = [{**r, "split": "test"} if r.get("kind") == "eval" else r for r in synth_sidecar(test_recs, 1)]
        plan = ps.plan_solver_job(job_record(split="test"), 1, test_recs, side, self.manifest)
        self.assertEqual([t["turn_key"] for t in plan["conversations"][0]["turns"]], ["learn", "test"])

    def test_usage_only_on_completed_spans(self):
        for r in self.records:
            s = self.spans(self.plan())[r["call_id"]]
            if r["outcome"] == "completed":
                self.assertEqual(s["usage"]["input_tokens"], r["usage"]["prompt_tokens"])
                self.assertEqual(s["usage"]["output_tokens"], r["usage"]["completion_tokens"])
                self.assertTrue(s["attributes"]["metaharness.usage_available"])
            else:                                 # the replayed record carries usage, and still none is planned
                self.assertIsNone(s["usage"], r["outcome"])
                self.assertFalse(s["attributes"]["metaharness.usage_available"])
        for t in self.plan()["conversations"][0]["turns"]:
            self.assertNotIn("usage", t)

    def test_partial_usage_does_not_invent_zero_tokens(self):
        rec = synth_record(1)
        rec["usage"] = {"prompt_tokens": 80}
        span = next(all_spans(self.plan(records=[rec], sidecar=[])))
        self.assertIsNone(span["usage"])
        self.assertFalse(span["attributes"]["metaharness.usage_available"])
        rec["usage"] = {"prompt_tokens": 80, "completion_tokens": True}
        self.assertIsNone(next(all_spans(self.plan(records=[rec], sidecar=[])))["usage"])
        rec["usage"] = {"prompt_tokens": 80, "completion_tokens": 3}
        usage = next(all_spans(self.plan(records=[rec], sidecar=[])))["usage"]
        self.assertEqual((usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]), (80, 3, None))

    def test_replayed_and_refused_spans_are_hashed(self):
        manifest = helpers.make_manifest(full_rules("USPTO"))
        spans = self.spans(self.plan(manifest=manifest))
        recs = by_outcome(self.records)
        done = spans[recs["completed"]["call_id"]]
        self.assertEqual(done["input_messages"][0]["parts"][0]["content"], "SYNTHETIC USPTO PROMPT 0")
        rep = spans[recs["replayed"]["call_id"]]
        self.assertEqual(rep["input_messages"][0]["parts"][0]["content"],
                         policy.apply_text("SYNTHETIC USPTO PROMPT 2", "hash")[0])
        self.assertEqual(rep["output_messages"], [])
        self.assertTrue(rep["attributes"]["metaharness.content_truncated"])
        ref = spans[recs["refused"]["call_id"]]
        ex = recs["refused"]["request_excerpt"]
        self.assertEqual(ref["input_messages"][0]["parts"][0]["content"], f"[sha256 {ex['sha256']} bytes {ex['bytes']}]")
        self.assertNotIn("SYNTHETIC USPTO PROMPT 3", json.dumps(self.plan(manifest=manifest)))

    def test_failed_calls_carry_error(self):
        spans = self.spans(self.plan())
        recs = by_outcome(self.records)
        for outcome in ("refused", "provider_rejected", "failed"):
            err = spans[recs[outcome]["call_id"]]["error"]
            self.assertEqual(err, {"type": outcome, "message": recs[outcome]["refusal"]["code"]})
        for outcome in ("completed", "replayed"):
            self.assertIsNone(spans[recs[outcome]["call_id"]]["error"])
        self.assertNotIn("SYNTHETIC provider said", json.dumps(self.plan()))   # refusal detail never planned

    def test_reasoning_part(self):
        spans = self.spans(self.plan(manifest=helpers.make_manifest(full_rules("USPTO"))))
        done = spans[by_outcome(self.records)["completed"]["call_id"]]
        self.assertEqual(done["output_messages"], [{"role": "assistant", "parts": [
            {"type": "reasoning", "content": "SYNTHETIC REASONING"}, {"type": "text", "content": "SYNTHETIC REPLY 0"}]}])
        trunc = next(r for r in self.records if r["flags"]["truncated_empty"])
        s = spans[trunc["call_id"]]
        self.assertEqual([p["type"] for p in s["output_messages"][0]["parts"]], ["reasoning", "text"])
        self.assertTrue(s["attributes"]["metaharness.truncated_empty"])
        self.assertFalse(s["attributes"]["metaharness.content_truncated"])

    def test_learn_turn_moves_to_own_conversation_over_limit(self):
        recs = [synth_record(i) for i in range(10 + 100)]
        plan = self.plan(records=recs, sidecar=synth_sidecar(recs, 10))
        self.assertEqual([c["conversation_id"] for c in plan["conversations"]], ["mh-test." + JOB_ID])
        self.assertEqual([t["turn_key"] for t in plan["conversations"][0]["turns"]], ["learn", "validate"])
        recs = [synth_record(i) for i in range(10 + 175)]                # 2 turns + 185 spans > 180
        plan = self.plan(records=recs, sidecar=synth_sidecar(recs, 10))
        ids = [c["conversation_id"] for c in plan["conversations"]]
        self.assertEqual(ids, ["mh-test." + JOB_ID, f"mh-test.{JOB_ID}.learn"])
        main, learn = plan["conversations"]
        self.assertEqual([(t["turn_key"], len(t["spans"])) for t in main["turns"]], [("validate", 175)])
        self.assertEqual([(t["turn_key"], len(t["spans"])) for t in learn["turns"]], [("learn", 10)])
        self.assertEqual(ps.validate_plan(plan, self.manifest), [])

    def test_parts_split(self):
        recs = [synth_record(i) for i in range(250)]
        plan = self.plan(records=recs, sidecar=synth_sidecar(recs, 0))
        convs = plan["conversations"]
        self.assertEqual([c["conversation_id"] for c in convs], ["mh-test." + JOB_ID, f"mh-test.{JOB_ID}.p2"])
        for c in convs:
            self.assertEqual([t["turn_key"] for t in c["turns"]], ["validate"])
            self.assertLessEqual(len(c["turns"]) + sum(len(t["spans"]) for t in c["turns"]), 180)
            self.assertEqual(c["turns"][0]["attributes"]["metaharness.parts"], 2)
        keys = [s["key"] for s in all_spans(plan)]
        self.assertEqual(keys, [r["call_id"] for r in recs])             # call order kept across the parts
        self.assertEqual(plan["expected_spans"], 252)
        self.assertEqual(ps.validate_plan(plan, self.manifest), [])

    def test_expected_spans_and_planned_bytes(self):
        plan = self.plan()
        turns = [t for c in plan["conversations"] for t in c["turns"]]
        spans = list(all_spans(plan))
        self.assertEqual(plan["expected_spans"], len(turns) + len(spans))
        raw = sum(len(json.dumps({k: v for k, v in x.items() if k != "spans"})) for x in turns + spans)
        self.assertEqual(plan["planned_bytes"], raw)
        manifest = helpers.make_manifest(**{"telemetry.agent_lens.byte_estimate_factor": "1.37"})
        self.assertEqual(self.plan(manifest=manifest)["planned_bytes"], math.ceil(Decimal("1.37") * raw))

    def test_plan_is_deterministic(self):
        a = self.plan()
        shuffled = list(reversed(self.records))
        random.Random(7).shuffle(shuffled)
        b = self.plan(records=copy.deepcopy(shuffled))
        self.assertEqual(util.canonical_json(a), util.canonical_json(b))
        self.assertEqual(json.dumps(a), json.dumps(b))
        s1 = ps.plan_proposer(session_record(), fixture_session(), self.manifest, roots=ROOTS)
        s2 = ps.plan_proposer(session_record(), fixture_session(), self.manifest, roots=ROOTS)
        self.assertEqual(json.dumps(s1), json.dumps(s2))
        o1 = ps.plan_outer_iteration(iteration_record(), journal(), self.manifest)
        o2 = ps.plan_outer_iteration(iteration_record(), list(reversed(journal())), self.manifest)
        self.assertEqual(json.dumps(o1), json.dumps(o2))

    def test_admission_only_record_planned_as_interrupted(self):
        rec = by_outcome(self.records)["completed"]
        for k in ("outcome", "ended_utc", "reply", "response", "usage"):
            rec.pop(k)
        manifest = helpers.make_manifest(full_rules("USPTO"))
        job = job_record(ended="2026-11-10T03:20:00.000000Z")
        s = self.spans(self.plan(records=[rec], sidecar=[], manifest=manifest, job=job))[rec["call_id"]]
        self.assertEqual(s["attributes"]["metaharness.outcome"], "interrupted")
        self.assertEqual((s["started_utc"], s["ended_utc"]), (rec["received_utc"], "2026-11-10T03:20:00.000000Z"))
        self.assertIsNone(s["usage"])
        self.assertEqual(s["error"], {"type": "interrupted", "message": "runner stopped while the request was open"})
        self.assertRegex(s["input_messages"][0]["parts"][0]["content"], HASH_RE)   # hash whatever the rules

    def test_solver_text_uses_the_jobs_dataset_rule(self):
        manifest = helpers.make_manifest(full_rules("Symptom2Disease"))
        uspto = self.plan(manifest=manifest)
        self.assertEqual(uspto["content_policy"]["dataset"], "USPTO")
        self.assertTrue(all(HASH_RE.fullmatch(t) for t in content_strings(uspto)))
        recs = [synth_record(i, job_id="val.no_memory.Symptom2Disease.a1") for i in range(3)]
        plan = ps.plan_solver_job(job_record(dataset="Symptom2Disease"), 1, recs, synth_sidecar(recs, 0), manifest)
        self.assertEqual(plan["content_policy"]["span_prompt"], "full")
        texts = list(content_strings(plan))
        self.assertIn("SYNTHETIC PROMPT 1", texts)
        self.assertIn("REPLY 2", texts)
        cut = helpers.make_manifest({"telemetry.content.Symptom2Disease.span_prompt": "truncate:200"})
        long = [synth_record(0, job_id="val.no_memory.Symptom2Disease.a1", prompt="Q" * 1000)]
        plan = ps.plan_solver_job(job_record(dataset="Symptom2Disease"), 1, long, [], cut)
        prompt = next(all_spans(plan))["input_messages"][0]["parts"][0]["content"]
        self.assertTrue(prompt.startswith("Q" * 100) and "mhforge cut 800 chars" in prompt)
        self.assertEqual(plan["truncated_fields"], 2)                    # the prompt and the hashed reply

    def test_conversation_ids_are_url_safe_and_bounded(self):
        campaign = "c" * 40
        cand = "k" * 48
        job_id = f"qual.test.{cand}.Symptom2Disease.r99.s99.a999"
        job = {**job_record(dataset="Symptom2Disease", split="test", candidate=cand, attempt=999), "campaign": campaign}
        job["attempts"][0]["job_id"] = job_id
        plan = ps.plan_solver_job(job, 999, [synth_record(0, job_id=job_id)], [], self.manifest, export_attempt=3)
        cid = plan["conversations"][0]["conversation_id"]
        full = f"{campaign}.{job_id}.e3"
        self.assertGreater(len(full), 128)
        self.assertEqual(cid, wire.bounded_id(full, 128))
        self.assertEqual(len(cid), 128)
        self.assertRegex(cid, r"\A[A-Za-z0-9._-]{1,128}\Z")
        prop = ps.plan_proposer({**session_record(key="iter999.s99"), "campaign": campaign}, fixture_session(),
                                self.manifest, roots=ROOTS, export_attempt=3)
        self.assertEqual(prop["conversations"][0]["conversation_id"], f"{campaign}.proposer.iter999.s99.e3")
        self.assertEqual(self.plan(export_attempt=2)["conversations"][0]["conversation_id"], f"mh-test.{JOB_ID}.e2")
        for p in (plan, prop):
            self.assertEqual(ps.validate_plan(p, self.manifest), [])


class ProposerTest(unittest.TestCase):
    def setUp(self):
        self.manifest = helpers.make_manifest()
        self.session = fixture_session()

    def plan(self, session=None, manifest=None, record=None, roots=ROOTS, verbatim=EMPTY_INDEX):
        return ps.plan_proposer(record or session_record(), session or self.session, manifest or self.manifest,
                                roots=roots, verbatim=verbatim)

    def test_proposer_plan_uses_concrete_model_and_total_input(self):
        plan = self.plan()
        llms = {s["key"]: s for s in all_spans(plan) if s["type"] == "llm"}
        self.assertEqual(sorted(llms), sorted(m.message_id for m in self.session.messages))
        for m in self.session.messages:
            s = llms[m.message_id]
            self.assertEqual((s["model"], s["response_model"]), (m.model, m.model))
            self.assertEqual(s["usage"]["input_tokens"], m.input_tokens_total)
            self.assertEqual(s["usage"]["output_tokens"], m.output_tokens)
            self.assertEqual(s["usage"]["cache_read_input_tokens"], m.cache_read)
            self.assertEqual(s["usage"]["cache_creation_input_tokens"], m.cache_creation)
        self.assertEqual(llms["msg_synthetic_A"]["usage"]["input_tokens"], 18012)
        self.assertEqual(llms["msg_synthetic_S1"]["response_model"], "claude-haiku-5-5")
        self.assertEqual(plan["unit"], "proposer:iter007.s1")
        self.assertEqual(plan["content_policy"]["dataset"], None)

    def test_stream_session_has_no_usage(self):
        stream = session_of([msg("m1", BASE)], source="stream")
        span = next(all_spans(self.plan(session=stream)))
        self.assertIsNone(span["usage"])                                 # stream output counts are placeholders

    def test_proposer_tool_and_subagent_spans(self):
        plan = self.plan()
        turn = plan["conversations"][0]["turns"][0]
        top = [(s["type"], s["key"]) for s in turn["spans"]]
        self.assertEqual(top, [("llm", "msg_synthetic_A"), ("tool", "toolu_synthetic_bash_01"),
                               ("llm", "msg_synthetic_B"), ("tool", "toolu_synthetic_agent_01"),
                               ("subagent", "a1b2c3d4e5f607182"), ("llm", "msg_synthetic_C")])
        sub = turn["spans"][4]
        self.assertEqual([(s["type"], s["key"]) for s in sub["spans"]],
                         [("llm", "msg_synthetic_S1"), ("tool", "toolu_synthetic_read_01"), ("llm", "msg_synthetic_S2")])
        self.assertEqual(sub["attributes"]["metaharness.tool_call_id"], "toolu_synthetic_agent_01")
        self.assertEqual(sub["model"], "claude-haiku-5-5")
        bash = turn["spans"][1]
        self.assertEqual((bash["name"], bash["tool_call_id"]), ("Bash", "toolu_synthetic_bash_01"))
        self.assertEqual((bash["started_utc"], bash["ended_utc"]),
                         ("2026-11-10T01:00:14.350000Z", "2026-11-10T01:00:15.020000Z"))
        self.assertEqual(plan["expected_spans"], 1 + 9)
        self.assertEqual(ps.validate_plan(plan, self.manifest), [])

    def test_killed_session_marked(self):
        killed = self.plan(record=session_record(present=False))
        self.assertEqual(killed["conversations"][0]["turns"][0]["error"]["type"], "killed")
        self.assertIsNone(self.plan()["conversations"][0]["turns"][0]["error"])

    def test_proposer_parts_split_with_subagents(self):
        msgs, links, t = [], {}, BASE
        for i in range(100):                                             # 200 main-thread spans
            tools = [tool(f"tu{i}", t)]
            if i == 30:
                tools.append(tool("agent_small", t, name="Agent"))
                links["small"] = "agent_small"
            if i == 60:
                tools.append(tool("agent_big", t, name="Agent"))
                links["big"] = "agent_big"
            msgs.append(msg(f"m{i:03d}", t, tools=tools))
            t += 1
        for i in range(20):                                              # a 41-span subagent, kept whole
            msgs.append(msg(f"s{i:03d}", BASE + 30.5 + i * 0.01, tools=[tool(f"ts{i}", BASE + 30.5)],
                            sidechain=True, agent="small", model="claude-haiku-5-5"))
        for i in range(90):                                              # a 181-span subagent: over the limit
            msgs.append(msg(f"b{i:03d}", BASE + 60.5 + i * 0.001, tools=[tool(f"tb{i}", BASE + 60.5)],
                            sidechain=True, agent="big", model="claude-haiku-5-5"))
        plan = self.plan(session=session_of(msgs, links))
        self.assertGreaterEqual(plan["expected_spans"], 400)
        convs = plan["conversations"]
        n = len(convs)
        self.assertEqual(convs[0]["conversation_id"], "mh-test.proposer.iter007.s1")
        self.assertEqual([c["conversation_id"] for c in convs[1:]],
                         [f"mh-test.proposer.iter007.s1.p{i}" for i in range(2, n + 1)])
        for i, c in enumerate(convs, start=1):
            (turn,) = c["turns"]
            self.assertLessEqual(1 + sum(1 + len(s.get("spans") or ()) for s in turn["spans"]), 180)
            self.assertEqual((turn["attributes"]["metaharness.part"], turn["attributes"]["metaharness.parts"]), (i, n))
            for j, s in enumerate(turn["spans"]):                       # a message's tool spans stay with it
                if s["type"] == "tool":
                    self.assertGreater(j, 0, "a part starts with a tool span")
                    self.assertIn(turn["spans"][j - 1]["type"], ("llm", "tool"))
        subs = [s for s in all_spans(plan) if s["type"] == "subagent"]
        self.assertEqual([s["key"] for s in subs], ["small"])
        self.assertEqual(len(subs[0]["spans"]), 40)
        flat = [s for s in all_spans(plan) if s["attributes"].get("metaharness.subagent") == "big"]
        self.assertEqual(len(flat), 180)
        big_convs = [c for c in convs if any(s["attributes"].get("metaharness.subagent") == "big"
                                             for s in c["turns"][0]["spans"])]
        self.assertGreaterEqual(len(big_convs), 2)
        for c in big_convs:                                             # its own run of parts
            self.assertTrue(all(s["attributes"].get("metaharness.subagent") == "big" for s in c["turns"][0]["spans"]))
        self.assertEqual(ps.validate_plan(plan, self.manifest), [])

    def test_proposer_tool_text_hex40_masked(self):
        manifest = helpers.make_manifest(full_rules("Symptom2Disease"))
        hex40, hex64 = "a" * 39 + "f", "b" * 64
        s = session_of([msg("m1", BASE, tools=[tool("t1", BASE, inp={"command": "cat logs/r/Symptom2Disease/x"},
                                                    result=f"key={hex40} digest={hex64}")])])
        span = next(x for x in all_spans(self.plan(session=s, manifest=manifest)) if x["type"] == "tool")
        mask = "[hex40 sha256 " + hashlib.sha256(hex40.encode()).hexdigest()[:12] + "]"
        self.assertEqual(span["result"], f"key={mask} digest={hex64}")
        self.assertEqual(span["attributes"]["metaharness.hex40_masked"], 1)
        self.assertNotIn(hex40, json.dumps(self.plan(session=s, manifest=manifest)))

    def test_proposer_llm_span_has_no_cumulative_context(self):
        plan = self.plan(manifest=helpers.make_manifest(full_rules(*helpers.make_manifest()["upstream"]["expect"]["datasets"])))
        llms = {s["key"]: s for s in all_spans(plan) if s["type"] == "llm"}
        for m in self.session.messages:
            s = llms[m.message_id]
            self.assertEqual(s["input_messages"], [])
            self.assertEqual(s["system_instructions"], [])
            texts = [p["content"] for o in s["output_messages"] for p in o["parts"]]
            self.assertEqual(texts, [m.text] if m.text else [])
        self.assertEqual(json.dumps(plan).count("SYNTHETIC FINAL ANSWER"), 1)

    def test_tool_result_attributed_or_strictest(self):
        manifest = helpers.make_manifest(full_rules("Symptom2Disease"))
        cases = {"one": {"command": "cat logs/r/Symptom2Disease/val.json"},
                 "dir": {"file_path": "data/symptom_diagnosis/val.jsonl"},
                 "none": {"command": "ls agents"},
                 "two": {"command": "cat logs/r/Symptom2Disease/x logs/r/USPTO/x"}}
        s = session_of([msg("m1", BASE, tools=[tool(k, BASE, inp=v, result="RESULT TEXT") for k, v in cases.items()])])
        tools = {x["key"]: x for x in all_spans(self.plan(session=s, manifest=manifest)) if x["type"] == "tool"}
        for k in ("one", "dir"):
            self.assertEqual(tools[k]["result"], "RESULT TEXT")
            self.assertEqual(tools[k]["attributes"]["metaharness.dataset"], "Symptom2Disease")
        for k in ("none", "two"):
            self.assertEqual(tools[k]["result"], policy.apply_text("RESULT TEXT", "hash")[0])
            self.assertNotIn("metaharness.dataset", tools[k]["attributes"])

    def test_proposer_tool_text_is_portable(self):
        manifest = helpers.make_manifest(full_rules("Symptom2Disease"))
        ws = str(ROOTS["workspace"])
        s = session_of([msg("m1", BASE, tools=[tool("t1", BASE,
                                                    inp={"file_path": f"{ws}/logs/r/Symptom2Disease/val.json"},
                                                    result="/Users/alice/notes and /home/bob/x")])])
        plan = self.plan(session=s, manifest=manifest)
        span = next(x for x in all_spans(plan) if x["type"] == "tool")
        self.assertEqual(json.loads(span["arguments"]), {"file_path": "<workspace>/logs/r/Symptom2Disease/val.json"})
        self.assertEqual(span["result"], "/Users/<user>/notes and /home/<user>/x")
        self.assertEqual(scan.scan_obj(plan, classes=("privacy",)), [])
        self.assertTrue(span["attributes"]["metaharness.content_truncated"])


    # ---- LD4: proposer span text passes the verbatim guard

    def test_loosened_tool_result_holding_another_datasets_text_is_withheld(self):
        # reviewer case: a tool call attributed to Symptom2Disease (rules opened) whose result holds USPTO text
        manifest = helpers.make_manifest(full_rules("Symptom2Disease"))
        index = scan.build_verbatim_index([USPTO_QUESTION], run_chars=40)
        leaked = "logs/r/Symptom2Disease/a.json:1:" + USPTO_QUESTION[10:90]
        s = session_of([msg("m1", BASE, tools=[
            tool("glob", BASE, inp={"command": "grep -r reactants logs/r/Symptom2Disease/*"}, result=leaked),
            tool("argq", BASE, inp={"command": "grep '" + USPTO_QUESTION[5:60] + "' logs/r/Symptom2Disease/x"},
                 result="no match"),
            tool("clean", BASE, inp={"command": "cat logs/r/Symptom2Disease/val.json"}, result="CLEAN RESULT")])])
        plan = self.plan(session=s, manifest=manifest, verbatim=index)
        tools = {x["key"]: x for x in all_spans(plan) if x["type"] == "tool"}
        self.assertEqual(tools["glob"]["attributes"]["metaharness.dataset"], "Symptom2Disease")
        self.assertEqual(tools["glob"]["result"], policy.apply_text(leaked, "hash")[0])
        self.assertTrue(tools["glob"]["attributes"]["metaharness.content_truncated"])
        self.assertRegex(tools["argq"]["arguments"], HASH_RE.pattern + r"\Z")
        self.assertEqual(tools["argq"]["result"], "no match")
        self.assertEqual(tools["clean"]["result"], "CLEAN RESULT")      # clean text keeps the loosened rule
        dumped = json.dumps(plan)
        self.assertNotIn(USPTO_QUESTION[10:90], dumped)
        self.assertNotIn(USPTO_QUESTION[5:60], dumped)
        self.assertNotIn(ps.VERBATIM_INDEX_MISSING, plan["problems"])
        self.assertEqual(ps.validate_plan(plan, manifest), [])

    def test_open_rules_without_verbatim_index_fail_closed(self):
        manifest = helpers.make_manifest(full_rules("Symptom2Disease"))
        s = session_of([msg("m1", BASE, text="SYNTHETIC REPLY TEXT", tools=[
            tool("t1", BASE, inp={"command": "cat logs/r/Symptom2Disease/val.json"}, result="RESULT TEXT")])])
        plan = self.plan(session=s, manifest=manifest, verbatim=None)
        self.assertIn(ps.VERBATIM_INDEX_MISSING, plan["problems"])
        texts = list(content_strings(plan))
        self.assertTrue(texts)
        for t in texts:
            self.assertRegex(t, HASH_RE.pattern + r"\Z")
        self.assertNotIn("RESULT TEXT", json.dumps(plan))
        self.assertNotIn("cat logs", json.dumps(plan))
        guarded = self.plan(session=s, manifest=manifest)              # the same session with an index
        self.assertNotIn(ps.VERBATIM_INDEX_MISSING, guarded["problems"])
        self.assertIn("RESULT TEXT", json.dumps(guarded))

    def test_all_hash_rules_need_no_verbatim_index(self):
        plan = self.plan(verbatim=None)                                 # default rules: everything is hashed anyway
        self.assertNotIn(ps.VERBATIM_INDEX_MISSING, plan["problems"])
        self.assertEqual(plan, self.plan(verbatim=EMPTY_INDEX))

    def test_text_under_the_strictest_rule_is_not_withheld(self):
        # no dataset has a rule stricter than the strictest one, so the guard does not apply (LD4)
        names = helpers.make_manifest()["upstream"]["expect"]["datasets"]
        manifest = helpers.make_manifest(full_rules(*names, field_rule="truncate:400"))
        index = scan.build_verbatim_index([USPTO_QUESTION], run_chars=40)
        s = session_of([msg("m1", BASE, text="Reply quoting " + USPTO_QUESTION, tools=[
            tool("t1", BASE, inp={"command": "ls agents"}, result=USPTO_QUESTION)])])
        plan = self.plan(session=s, manifest=manifest, verbatim=index)
        self.assertEqual(json.dumps(plan).count(USPTO_QUESTION), 2)


class OuterLoopTest(unittest.TestCase):
    def setUp(self):
        self.manifest = helpers.make_manifest()

    def test_outer_iteration_plan(self):
        plan = ps.plan_outer_iteration(iteration_record(), journal(), self.manifest)
        self.assertEqual(plan["unit"], "loop:iter003")
        (conv,) = plan["conversations"]
        self.assertEqual(conv["conversation_id"], "mh-test.search.p1")
        (turn,) = conv["turns"]
        self.assertEqual(turn["turn_key"], "iter003")
        names = [s["name"] for s in turn["spans"]]
        self.assertEqual(names, ["propose", "import_check:x", "import_check:y", "import_check:z", "benchmark:x",
                                 "benchmark:y", "frontier_update"])
        self.assertLessEqual(1 + len(names), 3 + 2 * 3)
        spans = {s["name"]: s for s in turn["spans"]}
        self.assertEqual((spans["propose"]["started_utc"], spans["propose"]["ended_utc"]),
                         ("2026-11-10T01:00:01.000000Z", "2026-11-10T01:20:00.000000Z"))
        self.assertEqual(spans["benchmark:y"]["started_utc"], "2026-11-10T01:40:01.000000Z")
        self.assertEqual((spans["frontier_update"]["started_utc"], spans["frontier_update"]["ended_utc"]),
                         ("2026-11-10T01:58:00.000000Z", "2026-11-10T01:59:00.000000Z"))
        self.assertEqual(spans["import_check:z"]["error"]["type"], "ToolError")
        result = json.loads(spans["benchmark:x"]["result"])
        self.assertEqual(result["datasets"]["USPTO"], {"status": "succeeded", "accuracy": 0.4})
        self.assertEqual(result["datasets"]["LawBench"]["failure_class"], "context_limit")
        self.assertEqual(json.loads(spans["frontier_update"]["result"])["best_complete_after"], 41.3)
        self.assertEqual(plan["problems"], [])
        self.assertEqual(ps.validate_plan(plan, self.manifest), [])
        base = ps.plan_outer_iteration({**iteration_record(0), "kind": "baselines"}, journal(0), self.manifest)
        self.assertEqual([s["name"] for s in base["conversations"][0]["turns"][0]["spans"]],
                         ["benchmark:x", "benchmark:y", "benchmark:z", "frontier_update"])

    def test_outer_loop_conversation_rolls_over(self):
        ids = {n: ps.plan_outer_iteration(iteration_record(n), journal(n), self.manifest)["conversations"][0]
               ["conversation_id"] for n in range(0, 31)}
        for n in range(0, 15):
            self.assertEqual(ids[n], "mh-test.search.p1")
        for n in range(15, 30):
            self.assertEqual(ids[n], "mh-test.search.p2")
        self.assertEqual(ids[30], "mh-test.search.p3")
        e2 = ps.plan_outer_iteration(iteration_record(15), journal(15), self.manifest, export_attempt=2)
        self.assertEqual(e2["conversations"][0]["conversation_id"], "mh-test.search.p2.e2")

    def test_outer_loop_arguments_guarded_by_verbatim_index(self):
        question = "SYNTHETIC QUESTION: which reactants give CCOC(=O)c1ccccc1 under acid catalysis today?"
        index = scan.build_verbatim_index([question], run_chars=40)
        rec = iteration_record()
        rec["candidates"][0]["hypothesis"] = "Quote: " + question[5:70]
        manifest = helpers.make_manifest(full_rules(*self.manifest["upstream"]["expect"]["datasets"]))
        plan = ps.plan_outer_iteration(rec, journal(), manifest, verbatim=index)
        bench = next(s for s in all_spans(plan) if s["name"] == "benchmark:x")
        args = json.loads(bench["arguments"])
        self.assertEqual(args["hypothesis"], policy.apply_text(rec["candidates"][0]["hypothesis"], "hash")[0])
        self.assertEqual(args["axis"], "exploration")                   # clean text stays under the full rule
        self.assertNotIn(question[5:70], json.dumps(plan))
        plain = ps.plan_outer_iteration(rec, journal(), manifest)       # without an index nothing is guarded
        self.assertIn(question[5:70], json.dumps(plain))

    def test_outer_loop_nested_proposer_text_stays_guarded(self):
        # review regression: a key of ours nested inside proposer text must not let that text through unhashed
        rec = iteration_record()
        rec["candidates"][0]["hypothesis"] = {"status": "CCOCC1", "outcome": "PROPOSERTOKEN"}
        rec["candidates"][0]["components"] = [{"kind": "OTHERTOKEN"}]
        dumped = json.dumps(ps.plan_outer_iteration(rec, journal(), self.manifest))
        for text in ("CCOCC1", "PROPOSERTOKEN", "OTHERTOKEN"):
            self.assertNotIn(text, dumped)
        self.assertIn("succeeded", dumped)                              # our own statuses stay readable


class SharedRulesTest(unittest.TestCase):
    def setUp(self):
        self.manifest = helpers.make_manifest()

    def plans(self, manifest):
        return [ps.plan_solver_job(job_record(), 1, fixture_records(), sidecar_fixture(), manifest),
                ps.plan_proposer(session_record(), fixture_session(), manifest, roots=ROOTS, verbatim=EMPTY_INDEX),
                ps.plan_outer_iteration(iteration_record(), journal(), manifest)]

    def test_conversation_url(self):
        self.assertEqual(ps.conversation_url("ent/proj", "mh-test.x.a1"),
                         "https://forge.coreweave.com/agent-lens/ent/proj/conversations/mh-test.x.a1")

    def test_turns_carry_no_content(self):
        for plan in self.plans(helpers.make_manifest(full_rules("USPTO", "Symptom2Disease"))):
            for c in plan["conversations"]:
                for t in c["turns"]:
                    self.assertEqual((t["input_messages"], t["output_messages"]), ([], []))
                    self.assertNotIn("usage", t)
        prop = self.plans(self.manifest)[1]["conversations"][0]["turns"][0]
        prompt = fixture_session().prompt_text
        self.assertEqual(prop["attributes"]["metaharness.prompt_sha256"], hashlib.sha256(prompt.encode()).hexdigest())
        self.assertEqual(prop["attributes"]["metaharness.prompt_chars"], len(prompt))
        self.assertNotIn(prompt, json.dumps(self.plans(self.manifest)[1]))

    def test_default_rules_hash_everything(self):
        system = self.manifest["upstream"]["expect"]["system_prompt"]
        for plan in self.plans(self.manifest):
            texts = list(content_strings(plan))
            if plan["kind"] != "outer_loop":
                self.assertTrue(texts)
                for t in texts:
                    self.assertRegex(t, HASH_RE.pattern + r"\Z")
            for s in all_spans(plan):
                self.assertTrue(all(x == system for x in s.get("system_instructions") or ()))
            self.assertEqual(set(v for k, v in plan["content_policy"].items() if k != "dataset"), {"hash"})
        outer = self.plans(self.manifest)[2]
        dumped = json.dumps(outer)
        for text in ("Keep a short list of hard examples", "exploration", "retrieval"):
            self.assertNotIn(text, dumped)
        self.assertIn("succeeded", dumped)                              # our own statuses stay readable

    def test_unset_decisions_give_placeholder_project(self):
        for key in ("decisions.entity", "decisions.project"):
            manifest = helpers.make_manifest({key: None})
            for plan in self.plans(manifest):
                self.assertEqual((plan["project"], plan["placeholder_project"]), ("unset-entity/unset-project", True))
        for plan in self.plans(self.manifest):
            self.assertEqual((plan["project"], plan["placeholder_project"]), ("example-entity/mh-forge-test", False))

    def test_every_plan_kind_validates(self):
        for manifest in (self.manifest, helpers.make_manifest(full_rules("Symptom2Disease"))):
            for plan in self.plans(manifest):
                self.assertEqual(ps.validate_plan(plan, manifest), [], plan["kind"])

    def test_validate_plan_catches_each_rule(self):
        base = self.plans(self.manifest)[0]
        self.assertEqual(ps.validate_plan(base, self.manifest), [])

        def broken(mutate):
            plan = copy.deepcopy(base)
            mutate(plan)
            return " | ".join(ps.validate_plan(plan, self.manifest))

        turn = lambda p: p["conversations"][0]["turns"][1]                # noqa: E731
        span = lambda p: turn(p)["spans"][0]                              # noqa: E731
        cases = {
            "not a leaf key": lambda p: span(p)["attributes"].update({"metaharness.a.b": 1}),
            "credential-shaped key": lambda p: span(p)["attributes"].update({"metaharness.api_key": "x"}),
            "usage on a tool span": lambda p: turn(p)["spans"].append(
                {**span(p), "type": "tool", "name": "x", "arguments": "", "result": "", "usage": {"input_tokens": 1}}),
            "usage on a turn": lambda p: turn(p).update({"usage": {"input_tokens": 1}}),
            "exceed the limit": lambda p: turn(p)["spans"].extend(copy.deepcopy(span(p)) for _ in range(200)),
            "not URL-safe": lambda p: p["conversations"][0].update({"conversation_id": "bad id/with space"}),
            "longer than 128": lambda p: p["conversations"][0].update({"conversation_id": "a" * 129}),
            "span outside its turn": lambda p: span(p).update({"ended_utc": "2030-01-01T00:00:00.000000Z"}),
            "bad span times": lambda p: span(p).update({"started_utc": span(p)["ended_utc"],
                                                         "ended_utc": span(p)["started_utc"]}),
            "bad turn times": lambda p: turn(p).update({"ended_utc": "2000-01-01T00:00:00.000000Z"}),
            "expected_spans": lambda p: p.update({"expected_spans": p["expected_spans"] + 1}),
            "exceeds the policy": lambda p: span(p)["input_messages"][0]["parts"][0].update({"content": "x" * 500}),
            "a turn carries content": lambda p: turn(p).update({"input_messages": [{"role": "user", "parts": []}]}),
        }
        for needle, mutate in cases.items():
            self.assertIn(needle, broken(mutate), needle)
        outer = self.plans(self.manifest)[2]
        two = copy.deepcopy(outer)
        two["conversations"][0]["turns"].append(copy.deepcopy(two["conversations"][0]["turns"][0]))
        two["expected_spans"] *= 2
        self.assertIn("2 turns, not 1", " | ".join(ps.validate_plan(two, self.manifest)))
        many = copy.deepcopy(outer)
        t = many["conversations"][0]["turns"][0]
        t["spans"] += copy.deepcopy(t["spans"])
        many["expected_spans"] = 1 + len(t["spans"])
        self.assertIn("more than 9 spans", " | ".join(ps.validate_plan(many, self.manifest)))

    def test_validate_plan_refuses_rules_that_open_lawbench(self):
        # review regression: an unvalidated (effective) manifest that opens LawBench must not yield a valid plan
        opened = helpers.make_manifest(full_rules("LawBench"))
        recs = [synth_record(0, job_id="val.no_memory.LawBench.a1", prompt="LAWBENCH CASE TEXT")]
        plan = ps.plan_solver_job(job_record(dataset="LawBench"), 1, recs, [], opened)
        self.assertIn("content rules open LawBench text", " | ".join(ps.validate_plan(plan, opened)))
        for plan in self.plans(opened):
            self.assertTrue(any("LawBench" in p for p in ps.validate_plan(plan, opened)), plan["kind"])


if __name__ == "__main__":
    unittest.main()
