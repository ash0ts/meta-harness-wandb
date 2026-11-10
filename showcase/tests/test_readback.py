# SPDX-License-Identifier: Apache-2.0
# Adapted from ash0ts/harness2-wandb@31b0f82 tests/showcase/test_telemetry.py (read-back cases). Changes: unittest
# instead of pytest; rows are checked against a span plan of spec 2.13 and mismatches are returned, not raised.
"""Read-back query bodies and verify_spans (spec 3.23)."""

from __future__ import annotations

import copy
import datetime as dt
import json
import unittest

from mhforge import scan, util
from mhforge.telemetry import plan_spans as ps
from mhforge.telemetry import readback as rb
from tests import helpers
from tests.test_plan_spans import (ROOTS, BASE, fixture_records, fixture_session, full_rules, iteration_record,
                                   job_record, journal, msg, session_of, session_record, sidecar_fixture, tool)

ALL = ("USPTO", "Symptom2Disease", "LawBench")


def naive(utc: str) -> str:
    """The service's stored time: naive UTC ISO text (H2 read-back)."""
    return dt.datetime.fromtimestamp(util.parse_utc(utc), dt.timezone.utc).replace(tzinfo=None).isoformat()


def nest(attrs: dict) -> dict:
    out: dict = {}
    for key, value in attrs.items():
        node = out
        *heads, leaf = key.split(".")
        for h in heads:
            node = node.setdefault(h, {})
        node[leaf] = value
    return out


def stored_messages(messages) -> list:
    return [{"role": m["role"], "content": json.dumps([{k: v for k, v in p.items() if v != ""} for p in m["parts"]])}
            for m in messages]


def stored_rows(plan: dict) -> list[dict]:
    """What a faithful store returns for a plan, in the row shape the H2 read-back saw."""
    rows = []

    def common(item):
        return {"started_at": naive(item["started_utc"]), "ended_at": naive(item["ended_utc"]),
                "status_code": "ERROR" if item.get("error") else "OK",
                "raw_span_dump": json.dumps({"attributes": nest(item.get("attributes") or {})})}

    def walk(spans):
        for s in spans:
            row = common(s)
            if s["type"] == "llm":
                u = s["usage"] or {}
                row.update({"operation_name": "chat", "request_model": s["model"],
                            "response_model": s["response_model"] or None, "response_id": s["response_id"],
                            "input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
                            "cache_read_input_tokens": u.get("cache_read_input_tokens"),
                            "reasoning_tokens": u.get("reasoning_tokens"),
                            "request_max_tokens": s["request_max_tokens"], "finish_reasons": s["finish_reasons"],
                            "system_instructions": s["system_instructions"],
                            "input_messages": stored_messages(s["input_messages"]),
                            "output_messages": stored_messages(s["output_messages"])})
            elif s["type"] == "tool":
                row.update({"operation_name": "execute_tool", "tool_call_id": s["tool_call_id"], "tool_name": s["name"],
                            "tool_call_arguments": "" if s["arguments"] == "{}" else s["arguments"],
                            "tool_call_result": s["result"]})
            else:
                row.update({"operation_name": "invoke_agent", "agent_name": s["name"]})
            rows.append(row)
            walk(s.get("spans") or ())

    for conv in plan["conversations"]:
        for turn in conv["turns"]:
            rows.append({**common(turn), "operation_name": "invoke_agent", "agent_name": turn["agent_name"]})
            walk(turn["spans"])
    return rows


class QueryBodyTest(unittest.TestCase):
    def test_query_bodies_pinned(self):
        self.assertEqual(rb.READBACK_URL, "https://trace.wandb.ai/agents/spans/query")
        body = rb.spans_query_body("ent/proj", "4bf92f3577b34da6a3ce929d0e0e4736")
        self.assertEqual(body, {"project_id": "ent/proj",
                                "query": {"$expr": {"$eq": [{"$getField": "trace_id"},
                                                            {"$literal": "4bf92f3577b34da6a3ce929d0e0e4736"}]}},
                                "include_details": True, "include_costs": True, "limit": 200, "offset": 0})
        self.assertEqual(json.dumps(rb.spans_query_body("e/p", "t", limit=50, offset=400), sort_keys=True),
                         '{"include_costs": true, "include_details": true, "limit": 50, "offset": 400, '
                         '"project_id": "e/p", "query": {"$expr": {"$eq": [{"$getField": "trace_id"}, '
                         '{"$literal": "t"}]}}}')
        self.assertEqual(rb.conversation_ids_body("ent/proj"),
                         {"project_id": "ent/proj", "include_details": False, "include_costs": False,
                          "limit": 10000, "offset": 0})
        self.assertEqual(rb.conversation_ids_body("e/p", limit=5, offset=10)["offset"], 10)


class VerifyTest(unittest.TestCase):
    def test_rejected_usage_projection_requires_absent_raw_counts_and_explicit_unknown(self):
        attrs={'metaharness.outcome':'provider_rejected','metaharness.provider_attempts':1,
               'metaharness.usage_available':False}
        time='2026-11-10T00:00:00Z'
        planned={'started_utc':time,'ended_utc':time,'usage':None,'attributes':attrs}
        stored={'started_at':time,'ended_at':time,'input_tokens':0,'output_tokens':0,
                'raw_span_dump':json.dumps({'attributes':attrs})}
        self.assertEqual(rb._compare('llm','c',planned,stored),[])
        stored['input_tokens']=1
        self.assertIn('llm:c:input_tokens',rb._compare('llm','c',planned,stored))
        stored['input_tokens']=0
        stored['raw_span_dump']=json.dumps({'attributes':{**attrs,'gen_ai.usage.input_tokens':0}})
        self.assertIn('llm:c:input_tokens',rb._compare('llm','c',planned,stored))
        stored['raw_span_dump']=json.dumps({'attributes':{**attrs,'metaharness.usage_available':True}})
        self.assertIn('llm:c:input_tokens',rb._compare('llm','c',planned,stored))

    def setUp(self):
        self.manifest = helpers.make_manifest(full_rules(*ALL))
        self.solver = ps.plan_solver_job(job_record(), 1, fixture_records(), sidecar_fixture(), self.manifest)
        self.proposer = ps.plan_proposer(session_record(), fixture_session(), self.manifest, roots=ROOTS,
                                          verbatim=scan.build_verbatim_index([]))

    def test_verify_exact_match(self):
        for plan in (self.solver, self.proposer):
            rows = stored_rows(plan)
            self.assertEqual(len(rows), plan["expected_spans"])
            self.assertEqual(rb.verify_spans(plan, rows), [], plan["kind"])
            self.assertEqual(rb.verify_spans(plan, list(reversed(rows))), [])     # order does not matter
        outer = ps.plan_outer_iteration(iteration_record(), journal(), self.manifest)
        self.assertEqual(rb.verify_spans(outer, stored_rows(outer)), [])
        flat = []                                       # attributes given flat on the row instead of a raw dump
        for row in stored_rows(self.solver):
            attrs = json.loads(row.pop("raw_span_dump"))["attributes"]
            row["attributes"] = {f"metaharness.{k}": v for k, v in attrs.get("metaharness", {}).items()}
            flat.append(row)
        self.assertEqual(rb.verify_spans(self.solver, flat), [])
        epoch = []                                      # epoch-millisecond times and Z-suffixed text are read too
        for row in stored_rows(self.solver):
            row["started_at"] = row["started_at"] + "Z"
            row["ended_at"] = dt.datetime.fromisoformat(row["ended_at"]).replace(tzinfo=dt.timezone.utc).timestamp() * 1000
            epoch.append(row)
        self.assertEqual(rb.verify_spans(self.solver, epoch), [])

    def _rows(self):
        rows = stored_rows(self.solver)
        by_id = {json.loads(r["raw_span_dump"])["attributes"]["metaharness"]["call_id"]: r
                 for r in rows if r["operation_name"] == "chat"}
        ids = {r["outcome"]: r["call_id"] for r in fixture_records() if not r["flags"]["truncated_empty"]}
        return rows, {outcome: by_id[cid] for outcome, cid in ids.items()}

    def test_verify_reports_each_mismatch(self):
        done = next(r for r in fixture_records() if r["outcome"] == "completed" and not r["flags"]["truncated_empty"])
        cid = done["call_id"]
        cases = {
            "request_model": ("request_model", "other/model"),
            "response_model": ("response_model", "other/model"),
            "input_tokens": ("input_tokens", 1),
            "output_tokens": ("output_tokens", 2),
            "finish_reasons": ("finish_reasons", ["length"]),
            "request_max_tokens": ("request_max_tokens", 7),
            "system_instructions": ("system_instructions", ["changed"]),
            "started_at": ("started_at", naive(util.utc_iso(util.parse_utc(done["received_utc"]) + 0.002))),
            "ended_at": ("ended_at", None),
            "error_lost": None,
        }
        for field, change in cases.items():
            rows, llm = self._rows()
            row = llm["completed"] if field != "error_lost" else llm["failed"]
            if change is None:
                row["status_code"] = "OK"
            else:
                row[change[0]] = change[1]
            target = cid if field != "error_lost" else next(r["call_id"] for r in fixture_records()
                                                             if r["outcome"] == "failed")
            self.assertIn(f"llm:{target}:{field}", rb.verify_spans(self.solver, rows), field)
        rows, llm = self._rows()                         # message digests: changed input and output content
        llm["completed"]["input_messages"] = stored_messages([{"role": "user", "parts": [{"type": "text", "content": "x"}]}])
        parts = json.loads(llm["completed"]["output_messages"][0]["content"])
        parts[0]["content"] = "OTHER REASONING"
        llm["completed"]["output_messages"][0]["content"] = json.dumps(parts)
        found = rb.verify_spans(self.solver, rows)
        self.assertIn(f"llm:{cid}:input_messages", found)
        self.assertIn(f"llm:{cid}:output_messages", found)
        rows, llm = self._rows()                         # a count stored for a call that has no usage
        llm["replayed"]["input_tokens"] = 1
        rep = next(r["call_id"] for r in fixture_records() if r["outcome"] == "replayed")
        self.assertIn(f"llm:{rep}:input_tokens", rb.verify_spans(self.solver, rows))
        llm["replayed"]["input_tokens"] = 0
        self.assertNotIn(f"llm:{rep}:input_tokens", rb.verify_spans(self.solver, rows))
        raw = json.loads(llm["replayed"]["raw_span_dump"])
        raw['attributes']['metaharness']['usage_available'] = True
        llm["replayed"]["raw_span_dump"] = json.dumps(raw)
        self.assertIn(f"llm:{rep}:input_tokens", rb.verify_spans(self.solver, rows))
        rows, _ = self._rows()                           # a missing row and a duplicate row
        dropped = rows.pop(next(i for i, r in enumerate(rows) if r["operation_name"] == "chat"))
        self.assertEqual([m for m in rb.verify_spans(self.solver, rows) if m.startswith("missing:")],
                         [f"missing:llm:{json.loads(dropped['raw_span_dump'])['attributes']['metaharness']['call_id']}"])
        rows, _ = self._rows()
        rows.append(copy.deepcopy(rows[1]))
        self.assertTrue(any(m.startswith("unexpected:") for m in rb.verify_spans(self.solver, rows)))
        rows, _ = self._rows()
        rows.append({"operation_name": "embeddings"})
        self.assertIn("unexpected:unknown:embeddings", rb.verify_spans(self.solver, rows))
        rows, _ = self._rows()                           # a time inside 1 ms is not a mismatch
        rows[1]["started_at"] = naive(util.utc_iso(util.parse_utc(self.solver["conversations"][0]["turns"][0]
                                                                   ["spans"][0]["started_utc"]) + 0.0005))
        self.assertEqual(rb.verify_spans(self.solver, rows), [])
        rows = stored_rows(self.proposer)                # tool, turn and subagent fields
        bash = next(r for r in rows if r.get("tool_call_id") == "toolu_synthetic_bash_01")
        bash["tool_name"] = "Read"
        bash["tool_call_result"] = "changed"
        bash["tool_call_arguments"] = '{"command": "rm"}'
        turn = next(r for r in rows if r.get("agent_name") == "metaharness-proposer")
        turn["agent_name"] = "someone-else"
        sub = next(r for r in rows if r.get("agent_name") == "subagent")
        sub["ended_at"] = naive("2026-11-10T09:00:00.000000Z")
        found = rb.verify_spans(self.proposer, rows)
        for needle in ("tool:toolu_synthetic_bash_01:tool_name", "tool:toolu_synthetic_bash_01:result",
                       "tool:toolu_synthetic_bash_01:arguments", "subagent:a1b2c3d4e5f607182:ended_at"):
            self.assertIn(needle, found)
        self.assertIn("turn:iter007.s1:agent_name", found)

    def test_verify_reports_content_on_turns_and_subagents(self):
        # review regression: turns and subagents are planned content-free, so stored content there is a mismatch
        rows = stored_rows(self.proposer)
        turn = next(r for r in rows if r.get("agent_name") == "metaharness-proposer")
        turn["input_messages"] = stored_messages([{"role": "user", "parts": [{"type": "text", "content": "TASK"}]}])
        sub = next(r for r in rows if r.get("agent_name") == "subagent")
        sub["output_messages"] = stored_messages([{"role": "assistant", "parts": [{"type": "text", "content": "X"}]}])
        found = rb.verify_spans(self.proposer, rows)
        self.assertIn("turn:iter007.s1:input_messages", found)
        self.assertIn("subagent:a1b2c3d4e5f607182:output_messages", found)
        rows = stored_rows(self.proposer)
        for r in rows:
            if r["operation_name"] == "invoke_agent":
                r["input_messages"], r["output_messages"] = [], ""      # empty forms are no content
        self.assertEqual(rb.verify_spans(self.proposer, rows), [])

    def test_verify_tolerates_known_normalisations(self):
        manifest = helpers.make_manifest(full_rules(*ALL))
        session = session_of([msg("m1", BASE, tools=[tool("empty", BASE, name="Skill", inp={}, result="missing name")])])
        plan = ps.plan_proposer(session_record(), session, manifest, roots=ROOTS,
                                verbatim=scan.build_verbatim_index([]))
        rows = stored_rows(plan)
        row = next(r for r in rows if r.get("tool_call_id") == "empty")
        self.assertEqual(row["tool_call_arguments"], "")                 # an empty object is stored as ""
        self.assertEqual(rb.verify_spans(plan, rows), [])
        present = ps.plan_proposer(session_record(), session_of([msg("m1", BASE, tools=[
            tool("empty", BASE, name="Skill", inp={"name": "actually-present"}, result="missing name")])]),
            manifest, roots=ROOTS, verbatim=scan.build_verbatim_index([]))
        self.assertIn("tool:empty:arguments", rb.verify_spans(present, rows))
        # a missing response model is stored as ""
        solver = ps.plan_solver_job(job_record(), 1, fixture_records(), sidecar_fixture(), manifest)
        rows = stored_rows(solver)
        failed = [r for r in rows if r["operation_name"] == "chat" and r["response_model"] is None]
        self.assertTrue(failed)
        for r in failed:
            r["response_model"] = ""
        self.assertEqual(rb.verify_spans(solver, rows), [])
        for r in failed:
            del r["response_model"]
        self.assertEqual(rb.verify_spans(solver, rows), [])
        done = next(r for r in rows if r["operation_name"] == "chat" and r.get("response_model"))
        done["response_model"] = ""                      # but a planned model that is stored empty is a mismatch
        self.assertTrue(any(m.endswith(":response_model") for m in rb.verify_spans(solver, rows)))


if __name__ == "__main__":
    unittest.main()
