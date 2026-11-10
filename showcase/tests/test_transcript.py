# SPDX-License-Identifier: MIT
"""The transcript converter against the corrected synthetic fixture (invented content, measured structure)."""
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from mhforge import util
from mhforge.errors import TranscriptError
from mhforge.telemetry import transcript as T
from tests import helpers

FIX = helpers.FIXTURES / "transcript_synthetic"
SID = "5e55104e-0000-4000-8000-00000000abcd"
AGENT = "a1b2c3d4e5f607182"
MAIN_FILE = FIX / f"{SID}.jsonl"
SUB_FILE = FIX / SID / "subagents" / f"agent-{AGENT}.jsonl"
EXPECTED = json.loads((FIX / "EXPECTED.json").read_text())
VERSION = EXPECTED["pinned_version"]
STREAM = helpers.FIXTURES / "stream_synthetic.jsonl"
LIMIT_STREAM = helpers.FIXTURES / "stream_limit_synthetic.jsonl"   # shape inferred, never observed


def load():
    return T.read_session_dir(FIX, SID, max_bytes=10 * 1024 * 1024)


def session(records=None, subagents=None, version=VERSION):
    main, subs = load()
    return T.normalize_transcript(main if records is None else records, expect_version=version,
                                  subagents=subs if subagents is None else subagents)


def stream_session(path=STREAM):
    return T.normalize_stream(helpers.stamp_rows(path.read_text().splitlines(), "2026-11-10T01:00:00Z", 0.5))


def msg(s, mid):
    return next(m for m in s.messages if m.message_id == mid)


def tool(s, tid):
    return next(t for m in s.messages for t in m.tool_uses if t.id == tid)


def same_time(a, b):
    return util.parse_utc(a) == util.parse_utc(b)


class Tracking(dict):
    """A record that remembers which keys were looked at."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.touched = set()

    def __getitem__(self, k):
        self.touched.add(k)
        return super().__getitem__(k)

    def get(self, k, default=None):
        self.touched.add(k)
        return super().get(k, default)

    def __contains__(self, k):
        self.touched.add(k)
        return super().__contains__(k)

    def __iter__(self):
        self.touched.add("*")
        return super().__iter__()

    def keys(self):
        self.touched.add("*")
        return super().keys()

    def items(self):
        self.touched.add("*")
        return super().items()

    def values(self):
        self.touched.add("*")
        return super().values()


class Fixture(unittest.TestCase):
    def test_fixture_is_synthetic(self):
        for path in (MAIN_FILE, SUB_FILE):
            records = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
            refs = {r["id"] for r in records if r.get("type") == "api-request"}
            assistants = [r for r in records if r.get("type") == "assistant"]
            self.assertTrue(assistants)
            for r in records:
                self.assertEqual(r.get("sessionId"), SID)
                if "cwd" in r:
                    self.assertTrue(r["cwd"].startswith("/Users/example/"), r["cwd"])
            for r in assistants:
                self.assertIn("requestRef", r)
                self.assertIn(r["requestRef"], refs)               # resolves in the same file
        self.assertEqual(EXPECTED["session_id"], SID)
        self.assertEqual(len(MAIN_FILE.read_text().splitlines()), 15)
        self.assertEqual(len(SUB_FILE.read_text().splitlines()), 6)
        self.assertEqual(len(STREAM.read_text().splitlines()), 13)
        self.assertEqual(len(LIMIT_STREAM.read_text().splitlines()), 5)
        self.assertIn("SYNTHETIC", MAIN_FILE.read_text())
        self.assertTrue((FIX / SID / "subagents" / f"agent-{AGENT}.meta.json").is_file())
        self.assertTrue((FIX / "README.md").is_file())


class Messages(unittest.TestCase):
    def test_message_counted_once_per_id(self):
        s = session()
        ids = [m.message_id for m in s.messages]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual([m.message_id for m in s.messages if not m.sidechain], EXPECTED["api_messages"]["main"])
        self.assertEqual([m.message_id for m in s.messages if m.sidechain], EXPECTED["api_messages"]["subagent"])
        a = msg(s, "msg_synthetic_A")                                # two records, one message, usage taken once
        self.assertEqual((a.input_tokens, a.output_tokens, a.cache_creation), (12, 900, 18000))
        opus = T.usage_by_model(s)["claude-opus-5-5"]
        wrong = EXPECTED["tokens_if_NOT_deduped_main_wrong"]
        self.assertNotEqual(opus["input"], wrong["input"])
        self.assertEqual(opus["input"], EXPECTED["tokens_from_transcript_deduped"]["claude-opus-5-5"]["input"])
        self.assertEqual(len(a.tool_uses), 1)                        # the blocks of both records are concatenated
        self.assertGreater(a.thinking_chars, 0)

    def test_subagent_messages_use_their_own_model(self):
        s = session()
        subs = [m for m in s.messages if m.sidechain]
        self.assertEqual([m.model for m in subs], ["claude-haiku-5-5", "claude-haiku-5-5"])
        self.assertEqual({m.model for m in s.messages if not m.sidechain}, {"claude-opus-5-5"})
        self.assertEqual(set(T.usage_by_model(s)), {"claude-opus-5-5", "claude-haiku-5-5"})

    def test_totals_equal_expected_json(self):
        s = session()
        usage = T.usage_by_model(s)
        for model, want in EXPECTED["tokens_from_transcript_deduped"].items():
            got = usage[model]
            self.assertEqual((got["input"], got["output"], got["cache_read"]),
                             (want["input"], want["output"], want["cache_read"]), model)
            self.assertEqual(got["cache_creation_5m"] + got["cache_creation_1h"], want["cache_creation"], model)
        prices = helpers.make_manifest()["proposer"]["list_prices"]
        cost = EXPECTED["cost_usd_list_price"]
        self.assertEqual(T.price_usage(usage, prices), util.micros(str(cost["total"])))
        for model in ("claude-opus-5-5", "claude-haiku-5-5"):
            self.assertEqual(T.price_usage({model: usage[model]}, prices), util.micros(str(cost[model])))
        self.assertEqual(s.cost_state["total_cost_micro"], util.micros(str(cost["total"])))

    def test_request_ref_join_gives_start_times_and_effort(self):
        s = session()
        self.assertEqual(len(s.messages), EXPECTED["api_request_records"]["total"])     # five requests read and joined
        for mid, want in EXPECTED["request_join"].items():
            m = msg(s, mid)
            self.assertEqual(m.request_ref, want["request_ref"], mid)
            self.assertTrue(same_time(m.started_utc, want["request_start"]), mid)
            self.assertEqual(m.model, want["model"], mid)
            self.assertEqual(m.effort, want["effort"], mid)
            self.assertTrue(same_time(m.ended_utc, want["last_record_timestamp"]), mid)   # span ends at its last record
            self.assertLessEqual(util.parse_utc(m.started_utc), util.parse_utc(m.ended_utc))
            self.assertRegex(m.started_utc, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z")
        self.assertEqual(T.request_efforts(s), ["max", "max", "max"])
        self.assertEqual(s.warnings, ())

    def test_message_without_request_record_warns_and_uses_own_timestamp(self):
        main, subs = load()
        main = [r for r in main if r.get("id") != "apireq_synthetic_0002"]
        s = T.normalize_transcript(main, expect_version=VERSION, subagents=subs)
        b = msg(s, "msg_synthetic_B")
        self.assertTrue(same_time(b.started_utc, "2026-11-10T01:00:22.800Z"))
        self.assertIsNone(b.effort)
        self.assertIn("no_request_record:msg_synthetic_B", s.warnings)
        self.assertEqual(T.request_efforts(s), ["max", "max"])
        # a message with no requestRef at all behaves the same way
        main, subs = load()
        for r in main:
            if r.get("type") == "assistant" and r["message"]["id"] == "msg_synthetic_C":
                del r["requestRef"]
        s = T.normalize_transcript(main, expect_version=VERSION, subagents=subs)
        self.assertTrue(same_time(msg(s, "msg_synthetic_C").started_utc, "2026-11-10T01:00:52.640Z"))
        self.assertIn("no_request_record:msg_synthetic_C", s.warnings)

    def test_input_tokens_include_cache_counters(self):
        s = session()
        a, b = msg(s, "msg_synthetic_A"), msg(s, "msg_synthetic_B")
        self.assertEqual(a.input_tokens_total, 12 + 0 + 18000)
        self.assertEqual(b.input_tokens_total, 8 + 18000 + 1400)
        self.assertEqual((a.cache_creation_5m, a.cache_creation_1h), (0, 18000))
        self.assertEqual((msg(s, "msg_synthetic_S1").cache_creation_5m, msg(s, "msg_synthetic_S1").cache_creation_1h),
                         (5000, 0))
        # without the cache_creation object the whole creation count is priced as 1-hour
        main, subs = load()
        for r in main:
            if r.get("type") == "assistant":
                del r["message"]["usage"]["cache_creation"]
        a = msg(T.normalize_transcript(main, expect_version=VERSION, subagents=subs), "msg_synthetic_A")
        self.assertEqual((a.cache_creation_5m, a.cache_creation_1h), (0, 18000))

    def test_model_is_concrete_id(self):
        s = session()
        self.assertEqual({m.model for m in s.messages}, {"claude-opus-5-5", "claude-haiku-5-5"})
        self.assertEqual(tool(s, "toolu_synthetic_agent_01").input["model"], "haiku")   # the alias stays an input
        self.assertNotIn("haiku", T.usage_by_model(s))
        self.assertNotIn("opus", T.usage_by_model(s))

    def test_thinking_text_not_kept(self):
        s = session()
        thinking = "SYNTHETIC THINKING: read the frontier first."
        self.assertEqual(msg(s, "msg_synthetic_A").thinking_chars, len(thinking))
        self.assertNotIn("SYNTHETIC THINKING", repr(s))
        self.assertEqual(msg(s, "msg_synthetic_C").text,
                         "SYNTHETIC FINAL ANSWER: wrote three candidates and pending_eval.json.")
        self.assertEqual(msg(s, "msg_synthetic_A").text, "")


class Records(unittest.TestCase):
    def test_unknown_types_skipped_and_counted_attachment_never_read(self):
        main, subs = load()
        tracked = []
        for i, r in enumerate(main):
            if r.get("type") == "attachment":
                main[i] = Tracking(r)
                tracked.append(main[i])
        self.assertEqual(len(tracked), 1)
        s = T.normalize_transcript(main, expect_version=VERSION, subagents=subs)
        self.assertEqual(s.skipped_types, EXPECTED["records_to_skip"])
        self.assertLessEqual(tracked[0].touched, {"type", "version"})
        self.assertNotIn("environment", repr(s))                    # the attachment's content went nowhere
        self.assertEqual(len(s.messages), 5)
        self.assertIsNotNone(s.prompt_text)
        self.assertTrue(s.prompt_text.startswith("SYNTHETIC PROMPT"))

    def test_other_version_fails_the_gate(self):
        main, subs = load()
        self.assertTrue(any("version" not in r for r in main if r.get("type") in ("queue-operation", "cost-state")))
        session()                                                   # records without `version` pass the gate
        bad = copy.deepcopy(main)
        next(r for r in bad if r.get("type") == "user")["version"] = "2.1.318"
        with self.assertRaises(TranscriptError) as cm:
            T.normalize_transcript(bad, expect_version=VERSION, subagents=subs)
        self.assertEqual(cm.exception.kind, "version")
        bad_subs = copy.deepcopy(subs)
        bad_subs[AGENT][2]["version"] = "2.1.286"
        with self.assertRaises(TranscriptError) as cm:
            T.normalize_transcript(main, expect_version=VERSION, subagents=bad_subs)
        self.assertEqual(cm.exception.kind, "version")
        # an unknown record type is gated too; None switches the gate off
        bad = copy.deepcopy(main)
        next(r for r in bad if r.get("type") == "synthetic-unknown-record")["version"] = "9.9.9"
        with self.assertRaises(TranscriptError):
            T.normalize_transcript(bad, expect_version=VERSION, subagents=subs)
        self.assertEqual(len(T.normalize_transcript(bad, expect_version=None, subagents=subs).messages), 5)

    def test_cost_state_last_record_wins(self):
        main, subs = load()
        s = T.normalize_transcript(main, expect_version=VERSION, subagents=subs)
        self.assertEqual(s.cost_state["model_usage"]["claude-haiku-5-5"]["costUSD"], 0.001075)
        resumed = dict(next(r for r in main if r.get("type") == "cost-state"), totalCostUSD=0.5, modelUsage={"m": {}})
        s = T.normalize_transcript(main + [resumed], expect_version=VERSION, subagents=subs)
        self.assertEqual(s.cost_state, {"total_cost_micro": 500000, "model_usage": {"m": {}}})
        none = T.normalize_transcript([r for r in main if r.get("type") != "cost-state"], expect_version=VERSION,
                                      subagents=subs)
        self.assertIsNone(none.cost_state)

    def test_bash_results_carry_sandboxed_flag(self):
        s = session()
        self.assertEqual(T.bash_results(s), [{"tool_use_id": "toolu_synthetic_bash_01", "sandboxed": True,
                                              "sidechain": False}])
        main, subs = load()
        rec = next(r for r in main if r.get("sandboxed") is True)
        rec["sandboxed"] = False
        self.assertEqual(T.bash_results(T.normalize_transcript(main, expect_version=VERSION, subagents=subs))[0]
                         ["sandboxed"], False)
        del rec["sandboxed"]
        self.assertIsNone(T.bash_results(T.normalize_transcript(main, expect_version=VERSION, subagents=subs))[0]
                          ["sandboxed"])
        # a Bash call without a result is not listed; a subagent's Bash result is, with sidechain True
        no_result = [r for r in load()[0] if not (r.get("type") == "user" and "sourceToolAssistantUUID" in r
                                                  and r.get("sandboxed") is True)]
        self.assertEqual(T.bash_results(T.normalize_transcript(no_result, expect_version=VERSION, subagents={})), [])
        main, subs = load()
        for r in subs[AGENT]:
            if r.get("type") == "assistant" and r["message"]["content"][0].get("name") == "Read":
                r["message"]["content"][0]["name"] = "Bash"
        res = T.bash_results(T.normalize_transcript(main, expect_version=VERSION, subagents=subs))
        self.assertIn({"tool_use_id": "toolu_synthetic_read_01", "sandboxed": None, "sidechain": True}, res)

    def test_price_usage_none_without_a_price(self):
        s = session()
        prices = copy.deepcopy(helpers.make_manifest()["proposer"]["list_prices"])
        del prices["claude-haiku-5-5"]
        self.assertIsNone(T.price_usage(T.usage_by_model(s), prices))
        prices = copy.deepcopy(helpers.make_manifest()["proposer"]["list_prices"])
        del prices["claude-haiku-5-5"]["cache_write_5m_per_million"]          # a counter in use has no price
        self.assertIsNone(T.price_usage(T.usage_by_model(s), prices))
        self.assertEqual(T.price_usage({}, prices), 0)

    def test_tool_use_joined_to_result_with_times_and_error(self):
        s = session()
        bash = tool(s, "toolu_synthetic_bash_01")
        self.assertEqual(bash.name, "Bash")
        self.assertEqual(bash.input, {"command": "ls agents | head -5", "description": "List agent files"})
        self.assertEqual(bash.result_text, "__init__.py\nexample_memory.py\nfewshot_all.py\nno_memory.py")
        self.assertFalse(bash.is_error)
        self.assertTrue(same_time(bash.started_utc, "2026-11-10T01:00:14.350Z"))
        self.assertTrue(same_time(bash.ended_utc, "2026-11-10T01:00:15.020Z"))
        agent = tool(s, "toolu_synthetic_agent_01")                         # list content, no is_error key
        self.assertEqual(agent.result_text, "SYNTHETIC SUBAGENT REPORT: predict, learn_from_batch, get_state, set_state.")
        self.assertFalse(agent.is_error)
        self.assertTrue(same_time(agent.ended_utc, "2026-11-10T01:00:41.005Z"))
        self.assertEqual([(t["name"], t["id"]) for t in EXPECTED["tool_calls"]],
                         [(t.name, t.id) for m in s.messages for t in m.tool_uses])
        main, subs = load()
        for r in main:
            for b in (r.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("tool_use_id") == "toolu_synthetic_bash_01":
                    b["is_error"] = True
        self.assertTrue(tool(T.normalize_transcript(main, expect_version=VERSION, subagents=subs),
                             "toolu_synthetic_bash_01").is_error)
        main = [r for r in load()[0] if r.get("uuid") != "00000000-0000-4000-8000-000000000005"]
        bash = tool(T.normalize_transcript(main, expect_version=VERSION, subagents={}), "toolu_synthetic_bash_01")
        self.assertEqual((bash.result_text, bash.ended_utc, bash.sandboxed), (None, None, None))

    def test_subagent_file_attached(self):
        s = session()
        subs = [m for m in s.messages if m.sidechain]
        self.assertEqual({m.agent_id for m in subs}, {AGENT})
        self.assertEqual(s.agent_links, {AGENT: "toolu_synthetic_agent_01"})
        self.assertFalse(any(w.startswith("subagent_not_attached") for w in s.warnings))
        self.assertEqual(tool(s, "toolu_synthetic_read_01").result_text.splitlines()[0], "     1→class ExampleMemory:")
        main, subs_recs = load()
        main = [r for r in main if r.get("uuid") != "00000000-0000-4000-8000-000000000007"]   # Agent result gone
        s = T.normalize_transcript(main, expect_version=VERSION, subagents=subs_recs)
        self.assertIn(f"subagent_not_attached:{AGENT}", s.warnings)
        self.assertEqual(len([m for m in s.messages if m.sidechain]), 2)
        self.assertEqual(s.prompt_text, "SYNTHETIC PROMPT: propose three new memory systems for iteration 7 and write "
                                        "pending_eval.json.")

    def test_no_timestamps_means_no_messages(self):
        main, subs = load()
        for r in main + subs[AGENT]:
            r.pop("timestamp", None)
        s = T.normalize_transcript(main, expect_version=VERSION, subagents=subs)
        self.assertEqual(s.messages, ())
        self.assertIn("no_usable_timestamps", s.warnings)
        self.assertIsNone(s.started_utc)
        self.assertIsNone(s.ended_utc)

    def test_parse_and_read_session_dir(self):
        main, subs = load()
        self.assertEqual(len(main), 15)
        self.assertEqual(list(subs), [AGENT])
        self.assertEqual(len(subs[AGENT]), 6)
        recs = T.parse_transcript(['{"type": "user"}', "", "not json", "[1]", '{"type": "assi'])
        self.assertEqual([r["type"] for r in recs], ["user", "mhforge-warning", "mhforge-warning", "mhforge-warning"])
        self.assertEqual([r.get("line") for r in recs[1:]], [3, 4, 5])
        s = T.normalize_transcript(main + recs[1:], expect_version=VERSION, subagents=subs)
        self.assertIn("bad_json_line:main:3", s.warnings)
        with self.assertRaises(TranscriptError) as cm:
            T.read_session_dir(FIX, SID, max_bytes=1000)
        self.assertEqual(cm.exception.kind, "shape")
        with self.assertRaises(FileNotFoundError):
            T.read_session_dir(FIX, "5e55104e-0000-4000-8000-000000000000", max_bytes=10 ** 7)
        with self.assertRaises(ValueError):
            T.read_session_dir(FIX, "../x", max_bytes=10 ** 7)
        tmp = Path(tempfile.mkdtemp(prefix="mhf-transcript-"))
        try:
            shutil.copytree(FIX, tmp / "t")
            (tmp / "t" / SID / "subagents" / "agent-zz.jsonl").symlink_to(SUB_FILE)
            self.assertEqual(list(T.read_session_dir(tmp / "t", SID, max_bytes=10 ** 7)[1]), [AGENT])
        finally:
            shutil.rmtree(tmp)


class Stream(unittest.TestCase):
    def test_stream_output_tokens_are_ignored(self):
        s = stream_session()
        self.assertEqual(s.source, "stream")
        self.assertTrue(s.messages)
        self.assertEqual({m.output_tokens for m in s.messages}, {0})               # never the placeholder 4
        self.assertIn(T.STREAM_PLACEHOLDER_WARNING, s.warnings)
        self.assertEqual({u["output"] for u in T.usage_by_model(s).values()}, {0})
        self.assertIsNone(s.cost_state)
        # the transcript carries the real output counts
        self.assertEqual(T.usage_by_model(session())["claude-opus-5-5"]["output"], 1420)

    def test_stream_fallback_gives_same_structure(self):
        st, tr = stream_session(), session()
        self.assertIsInstance(st, T.NormalizedSession)
        self.assertEqual((st.session_id, st.cli_version), (tr.session_id, tr.cli_version))
        self.assertEqual([m.message_id for m in st.messages if not m.sidechain],
                         [m.message_id for m in tr.messages if not m.sidechain])
        for m in st.messages:
            t = msg(tr, m.message_id)
            self.assertEqual((m.model, m.sidechain, m.agent_id, m.input_tokens_total, m.cache_read, m.cache_creation),
                             (t.model, t.sidechain, t.agent_id, t.input_tokens_total, t.cache_read, t.cache_creation))
            self.assertEqual([(u.id, u.name, u.input, u.result_text) for u in m.tool_uses],
                             [(u.id, u.name, u.input, u.result_text) for u in t.tool_uses])
            self.assertEqual(m.thinking_chars, t.thinking_chars)
            self.assertIsNone(m.effort)
            self.assertLessEqual(util.parse_utc(m.started_utc), util.parse_utc(m.ended_utc))
        self.assertEqual(st.agent_links, tr.agent_links)
        a = msg(st, "msg_synthetic_A")                            # arrival times of its two events (rows 2 and 3)
        self.assertTrue(same_time(a.started_utc, "2026-11-10T01:00:00.500Z"))
        self.assertTrue(same_time(a.ended_utc, "2026-11-10T01:00:01.000Z"))
        self.assertEqual(T.bash_results(st), [{"tool_use_id": "toolu_synthetic_bash_01", "sandboxed": None,
                                               "sidechain": False}])
        self.assertEqual(st.skipped_types, {"rate_limit_event": 1, "system": 1})

    def test_limit_stream_is_parsed_without_inventing_usage(self):
        # the limit stream's shape is inferred and was never observed
        s = stream_session(LIMIT_STREAM)
        self.assertEqual(s.session_id, SID)
        self.assertEqual([m.message_id for m in s.messages], ["msg_synthetic_error"])
        self.assertEqual(T.usage_by_model(s), {"<synthetic>": {"input": 0, "output": 0, "cache_read": 0,
                                                               "cache_creation_5m": 0, "cache_creation_1h": 0}})
        self.assertEqual(s.skipped_types, {"system": 1, "rate_limit_event": 1})

    def test_stream_ignores_stderr_and_bad_lines(self):
        rows = helpers.stamp_rows(STREAM.read_text().splitlines(), "2026-11-10T01:00:00Z", 0.5)
        rows.insert(1, {"t_utc": rows[0]["t_utc"], "stream": "stderr", "line": '{"type": "assistant"}'})
        rows.insert(2, {"t_utc": rows[0]["t_utc"], "stream": "stdout", "line": "{not json"})
        s = T.normalize_stream(rows)
        self.assertIn("bad_stream_line:2", s.warnings)
        self.assertEqual(len(s.messages), 4)
        for r in rows:
            r["t_utc"] = "not a time"
        s = T.normalize_stream(rows)
        self.assertEqual(s.messages, ())
        self.assertIn("no_usable_timestamps", s.warnings)


class ReviewRegressions(unittest.TestCase):
    def test_non_bool_sandboxed_flag_is_unknown(self):
        main, subs = load()
        main = copy.deepcopy(main)
        for r in main:
            if r.get("type") == "user" and "sandboxed" in r:
                r["sandboxed"] = "false"                             # truthy text must never read as sandboxed
        s = T.normalize_transcript(main, expect_version=VERSION, subagents=subs)
        self.assertEqual([b["sandboxed"] for b in T.bash_results(s)], [None])

    def test_request_effort_kept_when_its_timestamp_is_unusable(self):
        main, subs = load()
        want = T.request_efforts(T.normalize_transcript(main, expect_version=VERSION, subagents=subs))
        main = copy.deepcopy(main)
        for r in main:
            if r.get("type") == "api-request":
                r["timestamp"] = "not a time"
        s = T.normalize_transcript(main, expect_version=VERSION, subagents=subs)
        self.assertTrue(want)
        self.assertEqual(T.request_efforts(s), want)
        self.assertTrue(any(w.startswith("no_request_record:") for w in s.warnings))

    def test_price_usage_none_for_non_finite_price(self):
        for bad in ("NaN", "Infinity", "-Infinity"):
            self.assertIsNone(T.price_usage({"m": {"input": 5}}, {"m": {"input_per_million": bad}}))


if __name__ == "__main__":
    unittest.main()
