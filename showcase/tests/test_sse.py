# SPDX-License-Identifier: Apache-2.0
# Adapted from ash0ts/harness2-wandb@31b0f82 tests/showcase/test_budget.py (merge_stream usage test). Changes: rewritten for unittest; usage-not-summed check added; new cases for the parser, errors, reasoning names, the completion body and the fixtures.
import json
import unittest
from pathlib import Path

from mhforge import sse

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _chunk(content=None, reasoning=None, reasoning_content=None, finish=None, **top):
    delta = {}
    if content is not None:
        delta["content"] = content
    if reasoning is not None:
        delta["reasoning"] = reasoning
    if reasoning_content is not None:
        delta["reasoning_content"] = reasoning_content
    out = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    out.update(top)
    return out


def _fixture_events(name):
    with open(FIXTURES / name, "rb") as fh:
        return list(sse.iter_sse(fh))


def _fixture_chunks(name):
    return [obj for kind, obj in _fixture_events(name) if kind == "chunk"]


class MergeStreamTests(unittest.TestCase):
    def test_merge_concatenates_content_and_both_reasoning_names(self):
        chunks = [
            _chunk(content="", reasoning="ab", id="x1", model="m"),
            _chunk(reasoning_content="cd"),
            _chunk(content="hel"),
            _chunk(content="lo", reasoning="ef", finish="stop"),
            {"choices": [{"index": 1, "delta": {"content": "OTHER", "reasoning": "OTHER"}}]},
        ]
        merged = sse.merge_stream(chunks)
        msg = merged["choices"][0]["message"]
        self.assertEqual(msg["content"], "hello")
        self.assertEqual(msg["reasoning"], "abcdef")
        self.assertEqual(merged["choices"][0]["finish_reason"], "stop")
        self.assertEqual(len(merged["choices"]), 1)
        # reasoning_content alone is read too, and reported by its wire name
        only_rc = [_chunk(content="", reasoning_content="r1"), _chunk(reasoning_content="r2", finish="stop")]
        self.assertEqual(sse.merge_stream(only_rc)["choices"][0]["message"]["reasoning"], "r1r2")
        self.assertEqual(sse.reasoning_field(only_rc), "reasoning_content")
        self.assertEqual(sse.reasoning_field(chunks), "reasoning")
        self.assertIsNone(sse.reasoning_field([_chunk(content="x", reasoning="")]))

    def test_merge_keeps_last_usage_not_sum(self):
        # Ported: tool-call deltas are merged and the usage-only last chunk is kept.
        chunks = [
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "t", "function": {"name": "read", "arguments": '{"'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'x":1}'}}]},
                          "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
        ]
        result = sse.merge_stream(chunks)
        self.assertEqual(result["usage"]["prompt_tokens"], 100)
        self.assertEqual(result["usage"]["completion_tokens"], 20)
        self.assertEqual(result["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"], '{"x":1}')
        # Two usage blocks: the last one wins; they are never summed. A later null usage does not erase it.
        two = [
            _chunk(content="a", usage={"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8}),
            _chunk(content="b", finish="stop", usage={"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}),
            {"choices": [], "usage": None},
        ]
        self.assertEqual(sse.merge_stream(two)["usage"], {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9})

    def test_merge_does_not_double_reasoning_sent_under_both_names(self):
        # Regression: a delta carrying the same text as reasoning and reasoning_content counts once.
        chunks = [{"choices": [{"index": 0, "delta": {"reasoning": "abc", "reasoning_content": "abc"}}]},
                  {"choices": [{"index": 0, "delta": {"reasoning_content": "d", "content": "x"}}]}]
        self.assertEqual(sse.merge_stream(chunks)["choices"][0]["message"]["reasoning"], "abcd")
        self.assertEqual(sse.reasoning_field(chunks), "reasoning")

    def test_merge_survives_malformed_tool_calls(self):
        # Regression: hostile tool_calls shapes are skipped, not a crash.
        for bad in (5, "abc", [{"index": [1]}], [{"index": {"a": 1}}]):
            merged = sse.merge_stream([{"choices": [{"index": 0, "delta": {"content": "ok", "tool_calls": bad}}]}])
            self.assertEqual(merged["choices"][0]["message"]["content"], "ok")
            self.assertNotIn("tool_calls", merged["choices"][0]["message"])

    def test_merge_keeps_last_non_null_id_model_finish_reason(self):
        chunks = [
            _chunk(content="a", id="i1", model="m1", finish="length"),
            _chunk(content="b", id="i2", model="m2"),
            _chunk(content="c", id=None, model=None, finish=None),
        ]
        merged = sse.merge_stream(chunks)
        self.assertEqual(merged["id"], "i2")
        self.assertEqual(merged["model"], "m2")
        self.assertEqual(merged["choices"][0]["finish_reason"], "length")

    def test_merge_does_not_alias_input(self):
        usage = {"prompt_tokens": 1, "completion_tokens": 1}
        merged = sse.merge_stream([_chunk(content="a", usage=usage)])
        merged["usage"]["prompt_tokens"] = 99
        self.assertEqual(usage["prompt_tokens"], 1)

    def test_error_chunk_is_preserved(self):
        chunks = [
            _chunk(content="partial", id="e1", model="m"),
            {"error": {"message": "first", "code": 500}},
            {"error": {"message": "second", "code": 400}},
        ]
        merged = sse.merge_stream(chunks)
        self.assertEqual(merged["error"], {"message": "second", "code": 400})
        self.assertEqual(merged["choices"][0]["message"]["content"], "partial")
        self.assertNotIn("error", sse.merge_stream([_chunk(content="ok", finish="stop")]))
        only = sse.merge_stream(_fixture_chunks("sse_error_only.txt"))
        self.assertEqual(only["error"]["code"], 400)
        self.assertIn("context length", only["error"]["message"])
        self.assertEqual(only["choices"], [])

    def test_stream_without_choices_gives_empty_choices(self):
        self.assertEqual(sse.merge_stream([]), {"choices": []})
        merged = sse.merge_stream([
            {"id": "x", "model": "m", "choices": []},
            {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 0}},
        ])
        self.assertEqual(merged["choices"], [])
        self.assertEqual(merged["usage"]["prompt_tokens"], 3)
        self.assertEqual(merged["model"], "m")
        # A choice only at index 1 is not a choice of index 0.
        self.assertEqual(sse.merge_stream([{"choices": [{"index": 1, "delta": {"content": "x"}}]}])["choices"], [])


class CompletionBodyTests(unittest.TestCase):
    def test_completion_body_shape_and_empty_content_is_string(self):
        merged = sse.merge_stream(_fixture_chunks("sse_ok.txt"))
        body = sse.to_completion_body(merged, request_model="openai/gpt-oss-120b", created=1794270001)
        self.assertEqual(set(body), {"id", "object", "created", "model", "choices", "usage"})
        self.assertEqual(body["object"], "chat.completion")
        self.assertEqual(body["created"], 1794270001)
        self.assertEqual(body["id"], "chatcmpl-ok1")
        self.assertEqual(len(body["choices"]), 1)
        choice = body["choices"][0]
        self.assertEqual(set(choice), {"index", "message", "finish_reason"})
        self.assertEqual(choice["index"], 0)
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertEqual(choice["message"], {
            "role": "assistant", "content": "[DIAGNOSIS]common cold[/DIAGNOSIS]", "reasoning": "Think about it."})
        json.dumps(body, allow_nan=False)
        # Reasoning only: content is the empty string, reasoning is kept, finish_reason "length".
        ro = sse.to_completion_body(sse.merge_stream(_fixture_chunks("sse_reasoning_only.txt")),
                                    request_model="r", created=1)
        self.assertIsInstance(ro["choices"][0]["message"]["content"], str)
        self.assertEqual(ro["choices"][0]["message"]["content"], "")
        self.assertEqual(ro["choices"][0]["message"]["reasoning"], "I keep thinking and never answer.")
        self.assertEqual(ro["choices"][0]["finish_reason"], "length")
        # No reasoning at all: no reasoning key; no choices at all: content is still "".
        plain = sse.to_completion_body(sse.merge_stream([_chunk(content="hi", finish="stop")]), request_model="r", created=1)
        self.assertNotIn("reasoning", plain["choices"][0]["message"])
        empty = sse.to_completion_body({"choices": []}, request_model="r", created=1)
        self.assertEqual(empty["choices"][0]["message"], {"role": "assistant", "content": ""})
        self.assertIsNone(empty["choices"][0]["finish_reason"])
        # reasoning_content on the wire reaches the worker as "reasoning".
        rc = sse.to_completion_body(sse.merge_stream(_fixture_chunks("sse_reasoning_content.txt")), request_model="r", created=1)
        self.assertEqual(rc["choices"][0]["message"], {
            "role": "assistant", "content": "CCO", "reasoning": "Reactants first, then product."})
        self.assertNotIn("reasoning_content", rc["choices"][0]["message"])

    def test_completion_body_never_invents_usage_or_model(self):
        merged = sse.merge_stream(_fixture_chunks("sse_usage_missing.txt"))
        body = sse.to_completion_body(merged, request_model="requested/model", created=5)
        self.assertNotIn("usage", body)
        self.assertEqual(body["model"], "openai/gpt-oss-120b")  # the provider's echo wins
        self.assertEqual(body["choices"][0]["message"]["content"], "[罪名]盗窃<eoa>")
        no_model = sse.merge_stream([_chunk(content="x", finish="stop")])
        body2 = sse.to_completion_body(no_model, request_model="requested/model", created=5)
        self.assertEqual(body2["model"], "requested/model")
        self.assertNotIn("usage", body2)
        self.assertIsNone(body2["id"])
        echoed = sse.to_completion_body({"model": "other/model", "choices": []}, request_model="requested/model", created=5)
        self.assertEqual(echoed["model"], "other/model")
        with_usage = sse.to_completion_body(sse.merge_stream(_fixture_chunks("sse_ok.txt")), request_model="r", created=1)
        self.assertEqual(with_usage["usage"], {"prompt_tokens": 120, "completion_tokens": 16, "total_tokens": 136})


class IterSseTests(unittest.TestCase):
    def test_iter_sse_done_bad_and_comment_lines(self):
        lines = [
            b": keep-alive\n",
            b"\n",
            b"event: message\r\n",
            b"id: 7\n",
            b'data: {"a": 1}\n',
            b'data:{"b": 2}\r\n',
            b"data: {not json}\n",
            b"data: [1, 2]\n",
            b"data: \xff\xfe\n",
            b"retry: 100\n",
            b"data: [DONE]\n",
        ]
        events = list(sse.iter_sse(lines))
        self.assertEqual(events, [
            ("chunk", {"a": 1}),
            ("chunk", {"b": 2}),
            ("bad", b"data: {not json}"),
            ("bad", b"data: [1, 2]"),
            ("bad", b"data: \xff\xfe"),
            ("done", None),
        ])

    def test_iter_sse_rejects_nan_and_infinity(self):
        # Regression: NaN/Infinity are not JSON; a usage carrying them must not reach the ledger.
        lines = [b'data: {"usage": {"prompt_tokens": NaN}}', b'data: {"usage": {"completion_tokens": Infinity}}',
                 b'data: {"x": -Infinity}']
        self.assertEqual([k for k, _ in sse.iter_sse(lines)], ["bad", "bad", "bad"])

    def test_iter_sse_splits_a_multi_line_item(self):
        blob = b'data: {"a": 1}\n\ndata: [DONE]\n\n'
        self.assertEqual(list(sse.iter_sse([blob])), [("chunk", {"a": 1}), ("done", None)])

    def test_fixtures_parse_as_expected(self):
        ok = _fixture_events("sse_ok.txt")
        self.assertEqual([k for k, _ in ok], ["chunk"] * 5 + ["done"])
        tr = _fixture_events("sse_truncated.txt")
        self.assertEqual([k for k, _ in tr], ["chunk", "chunk", "bad"])
        merged = sse.merge_stream([o for k, o in tr if k == "chunk"])
        self.assertIsNone(merged["choices"][0]["finish_reason"])
        self.assertNotIn("usage", merged)
        self.assertEqual(merged["choices"][0]["message"]["content"], "[DIAGNOSIS]comm")
        um = _fixture_events("sse_usage_missing.txt")
        self.assertEqual(um[-1], ("done", None))
        self.assertNotIn("usage", sse.merge_stream([o for k, o in um if k == "chunk"]))
        self.assertEqual(sse.reasoning_field(_fixture_chunks("sse_reasoning_content.txt")), "reasoning_content")
        self.assertEqual(sse.reasoning_field(_fixture_chunks("sse_reasoning_only.txt")), "reasoning")
        self.assertIsNone(sse.reasoning_field(_fixture_chunks("sse_usage_missing.txt")))
        eo = _fixture_events("sse_error_only.txt")
        self.assertEqual([k for k, _ in eo], ["chunk", "done"])


if __name__ == "__main__":
    unittest.main()
