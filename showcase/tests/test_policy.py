# SPDX-License-Identifier: MIT
import hashlib
import json
import re
import unittest
from decimal import Decimal
from pathlib import Path

from mhforge import scan
from mhforge.telemetry import policy
from tests import helpers


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hexrun(n: int, seed: int = 3) -> str:
    return "".join("0123456789abcdef"[(i * 7 + seed) % 16] for i in range(n))


class ContentRules(unittest.TestCase):
    def test_truncate_keeps_head_tail_hash_and_length(self):
        text = "".join(chr(0x4E00 + (i % 500)) for i in range(1000))      # Chinese characters
        out, removed = policy.apply_text(text, "truncate:200")
        self.assertTrue(removed)
        marker = f"\n...[mhforge cut 800 chars, sha256 {sha(text)[:12]}]...\n"
        self.assertEqual(out, text[:100] + marker + text[-100:])
        self.assertTrue(out.startswith(text[:100]) and out.endswith(text[-100:]))
        # odd N keeps N // 2 on each side; a text that fits is left alone
        out, removed = policy.apply_text(text, "truncate:201")
        self.assertTrue(out.startswith(text[:100] + "\n...[mhforge cut 800 chars"))
        self.assertEqual(policy.apply_text(text[:200], "truncate:200"), (text[:200], False))
        self.assertEqual(policy.apply_text(None, "truncate:200"), (None, False))

    def test_hash_rule(self):
        text = "SYNTHETIC reply 化学"
        out, removed = policy.apply_text(text, "hash")
        self.assertTrue(removed)
        self.assertEqual(out, f"[sha256 {sha(text)} chars {len(text)}]")
        self.assertNotIn("SYNTHETIC", out)
        self.assertEqual(policy.apply_text(text, "full"), (text, False))
        self.assertEqual(policy.apply_text(None, "hash"), (None, False))
        for bad in ("truncate:0", "truncate:-1", "truncate", "Hash", "truncate:10\n", "keep"):
            with self.assertRaises(ValueError, msg=bad):
                policy.apply_text("x", bad)

    def test_strictest_and_rules_for_none(self):
        self.assertEqual(policy.strictest(["full", "truncate:500", "truncate:300"]), "truncate:300")
        self.assertEqual(policy.strictest(["full", "truncate:300", "hash"]), "hash")
        self.assertEqual(policy.strictest(["full"]), "full")
        self.assertEqual(policy.strictest([]), "hash")
        m = helpers.make_manifest(**{
            "telemetry.content.Symptom2Disease": {"span_prompt": "full", "span_reply": "truncate:400",
                                                  "span_reasoning": "full", "tool_result": "full"},
            "telemetry.content.USPTO": {"span_prompt": "truncate:300", "span_reply": "full",
                                        "span_reasoning": "truncate:250", "tool_result": "full"},
            "telemetry.content.LawBench": {"span_prompt": "hash", "span_reply": "hash", "span_reasoning": "hash",
                                           "tool_result": "hash"}})
        self.assertEqual(policy.rules_for(m, "Symptom2Disease")["span_reply"], "truncate:400")
        self.assertEqual(policy.rules_for(m, None), {"span_prompt": "hash", "span_reply": "hash",
                                                     "span_reasoning": "hash", "tool_result": "hash"})
        m["telemetry"]["content"]["LawBench"] = dict(m["telemetry"]["content"]["Symptom2Disease"])
        self.assertEqual(policy.rules_for(m, None), {"span_prompt": "truncate:300", "span_reply": "truncate:400",
                                                     "span_reasoning": "truncate:250", "tool_result": "full"})
        # an unknown dataset, or a missing field, falls back to hash
        self.assertEqual(set(policy.rules_for(m, "Nope").values()), {"hash"})
        del m["telemetry"]["content"]["USPTO"]["tool_result"]
        self.assertEqual(policy.rules_for(m, "USPTO")["tool_result"], "hash")
        self.assertEqual(policy.rules_for(m, None)["tool_result"], "hash")

    def test_default_manifest_rules_are_hash_everywhere(self):
        m = helpers.make_manifest()
        for ds in (None, "USPTO", "Symptom2Disease", "LawBench"):
            self.assertEqual(set(policy.rules_for(m, ds).values()), {"hash"})


class Attribution(unittest.TestCase):
    def test_attribute_dataset_one_none_several(self):
        m = helpers.make_manifest()
        self.assertEqual(policy.attribute_dataset('{"command": "cat logs/mh-test/Symptom2Disease/val.json"}', m),
                         "Symptom2Disease")
        self.assertEqual(policy.attribute_dataset('{"file_path": "data/crime_prediction/train.jsonl"}', m), "LawBench")
        self.assertEqual(policy.attribute_dataset("head data/uspto/val.jsonl and logs/x/USPTO/", m), "USPTO")
        self.assertIsNone(policy.attribute_dataset('{"command": "ls agents"}', m))
        self.assertIsNone(policy.attribute_dataset("cat logs/r/USPTO/a logs/r/LawBench/b", m))
        self.assertIsNone(policy.attribute_dataset("", m))
        self.assertIsNone(policy.attribute_dataset("USPTOX and xuspto", m))     # not inside a longer word

    def test_guard_text_replaces_quoted_dataset_text(self):
        question = "SYNTHETIC reaction: CC(=O)OC1=CC=CC=C1C(=O)O with acetic anhydride under reflux for two hours"
        index = scan.build_verbatim_index([question], ["SYNTHETIC-TARGET-STRING"], run_chars=40)
        quoting = "notes: " + question[5:70] + " end"
        out, replaced = policy.guard_text(quoting, index)
        self.assertTrue(replaced)
        self.assertEqual(out, policy.apply_text(quoting, "hash")[0])
        out, replaced = policy.guard_text("the target was SYNTHETIC-TARGET-STRING here", index)
        self.assertTrue(replaced)
        self.assertEqual(policy.guard_text("nothing from the data", index), ("nothing from the data", False))
        self.assertEqual(policy.guard_text(quoting, None), (quoting, False))


class Masking(unittest.TestCase):
    def test_mask_hex40_only_standalone_runs(self):
        h40, h64 = hexrun(40), sha("SYNTHETIC digest")
        text = f"commit {h40} digest {h64} near{h40}x end {h40.upper()}"
        out, n = policy.mask_hex40(text)
        tag = f"[hex40 sha256 {sha(h40)[:12]}]"
        self.assertEqual(n, 3)
        self.assertIn(f"commit {tag} digest {h64} near{tag}x", out)
        self.assertNotIn(h40, out)
        self.assertIn(h64, out)                                   # a 64-hex run is not a 40-hex run
        self.assertEqual(policy.mask_hex40(hexrun(41)), (hexrun(41), 0))
        self.assertEqual(policy.mask_hex40(hexrun(39)), (hexrun(39), 0))


class Bytes(unittest.TestCase):
    def test_span_bytes_is_ascii_escaped_json_length(self):
        span = {"type": "llm", "content": "中", "spans": [{"type": "tool", "content": "x" * 5000}]}
        self.assertEqual(policy.span_bytes(span), len('{"type": "llm", "content": "\\u4e2d"}'))
        self.assertEqual(policy.span_bytes({"a": "中"}) - policy.span_bytes({"a": "x"}), 5)   # 6 bytes against 1
        self.assertEqual(policy.span_bytes(span), policy.span_bytes({"type": "llm", "content": "中"}))

    def test_planned_bytes_applies_factor(self):
        leaf = {"type": "tool", "key": "t1"}
        sub = {"type": "subagent", "key": "s", "spans": [leaf, leaf]}
        turn = {"turn_key": "validate", "spans": [{"type": "llm", "key": "c1"}, sub]}
        convs = [{"conversation_id": "a", "turns": [turn]}, {"conversation_id": "b", "turns": [turn]}]
        raw = 2 * (policy.span_bytes(turn) + policy.span_bytes({"type": "llm", "key": "c1"}) + policy.span_bytes(sub)
                   + 2 * policy.span_bytes(leaf))
        self.assertEqual(policy.planned_bytes(convs, Decimal("1.00")), raw)
        self.assertEqual(policy.planned_bytes(convs, Decimal("1.5")), -(-raw * 3 // 2))
        self.assertEqual(policy.planned_bytes(convs, Decimal("1.001")), -(-raw * 1001 // 1000))   # rounded up
        with self.assertRaises(TypeError):
            policy.planned_bytes(convs, 1.5)


class Attributes(unittest.TestCase):
    def test_attribute_rules(self):
        ok = {"metaharness.run": "mh-test", "metaharness.attempt": 1, "metaharness.rate": 0.5,
              "metaharness.matched": True, "metaharness.token_count": 3, "metaharness.tokenizer": "o200k"}
        self.assertEqual(policy.check_attributes(ok, "metaharness"), [])
        cases = {
            "other.run": "x",                      # wrong prefix
            "metaharness.a.b": "x",                # not a leaf
            "metaharness": "x",                    # no leaf at all
            "metaharness.none": None,              # not a str, int, float or bool
            "metaharness.list": [1],
            "metaharness.nan": float("nan"),
            "metaharness.inf": float("inf"),
            "metaharness.empty": "",
            "metaharness.api_key": "x",            # credential suffixes, any case and separators
            "metaharness.OpenAI-Api-Key": "x",
            "metaharness.access_token": "x",
            "metaharness.client_secret": "x",
            "metaharness.Authorization": "x",
            "metaharness.note": "Bea" + "rer " + "abcdefghij0123456789KLMN",   # credential-shaped value
        }
        for key, value in cases.items():
            problems = policy.check_attributes({key: value}, "metaharness")
            self.assertEqual(len(problems), 1, (key, problems))
        many = {f"metaharness.k{i}": i for i in range(65)}
        self.assertEqual(len(policy.check_attributes(many, "metaharness")), 1)
        self.assertEqual(policy.check_attributes(dict(list(many.items())[:64]), "metaharness"), [])


class Portable(unittest.TestCase):
    def test_portable_scrubs_home_and_user(self):
        roots = {"state": Path("/Users/someone/rsi/mh/showcase/state/mh-test"),
                 "workspace": Path("/Users/someone/rsi/mh/showcase/state/mh-test/workspace/text_classification"),
                 "example": Path("/Users/someone/rsi/mh/reference_examples/text_classification"),
                 "home": Path("/Users/someone")}
        value = {
            "cmd": "cd /Users/someone/rsi/mh/showcase/state/mh-test/workspace/text_classification && ls",
            "/Users/someone/rsi/mh/showcase/state/mh-test/calls": [
                "/Users/someone/rsi/mh/reference_examples/text_classification/data/uspto/val.jsonl",
                "/Users/someone/.claude/projects/x.jsonl", ("/home/other/file", 3)],
            "near": "/Users/someone/rsi/mh/showcase/state/mh-testX/a",
            "other": "see /Users/another.person/notes.txt",
            "n": 7,
        }
        out = policy.portable(value, roots)
        self.assertEqual(out["cmd"], "cd <workspace> && ls")
        self.assertEqual(out["<state>/calls"][0], "<example>/data/uspto/val.jsonl")
        self.assertEqual(out["<state>/calls"][1], "<home>/.claude/projects/x.jsonl")
        self.assertEqual(out["<state>/calls"][2], ("/home/<user>/file", 3))
        self.assertEqual(out["near"], "<home>/rsi/mh/showcase/state/mh-testX/a")      # a root only ends at a boundary
        self.assertEqual(out["other"], "see /Users/<user>/notes.txt")
        self.assertEqual(out["n"], 7)
        text = json.dumps(out)
        self.assertNotIn("someone", text)
        self.assertNotIn("another.person", text)
        self.assertEqual(scan.scan_obj(out, classes=("privacy",)), [])
        self.assertIn("someone", value["cmd"])                                    # the input is not changed


if __name__ == "__main__":
    unittest.main()
