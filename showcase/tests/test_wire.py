# SPDX-License-Identifier: MIT
import dataclasses
import json
import re
import unittest

from mhforge import wire
from mhforge.wire import JobIdentity
from tests import helpers


def ident(**kw) -> JobIdentity:
    base = dict(campaign="mh-forge-01", split="val", candidate="fewshot_all", dataset="LawBench", attempt=1,
                phase="baseline", iteration=None)
    base.update(kw)
    return JobIdentity(**base)


class WireTest(unittest.TestCase):
    def test_job_key_and_id_format(self):
        j = ident()
        self.assertEqual(j.job_key, "val.fewshot_all.LawBench")
        self.assertEqual(j.job_id, "val.fewshot_all.LawBench.a1")
        q = ident(phase="qualification", candidate="no_memory", dataset="USPTO", attempt=3)
        self.assertEqual(q.job_key, "qual.val.no_memory.USPTO")
        self.assertEqual(q.job_id, "qual.val.no_memory.USPTO.a3")
        s = ident(split="test", phase="search", iteration=4, candidate="x")
        self.assertEqual(s.job_key, "test.x.LawBench")
        d = j.to_dict()
        self.assertEqual(d, {"campaign": "mh-forge-01", "split": "val", "candidate": "fewshot_all", "dataset": "LawBench",
                             "attempt": 1, "phase": "baseline", "iteration": None, "repeat": None, "sample": None,
                             "job_key": "val.fewshot_all.LawBench", "job_id": "val.fewshot_all.LawBench.a1"})
        # the longest key is still a safe path component of at most 84 characters (2.2)
        longest = ident(phase="qualification", candidate="a" * 48, dataset="Symptom2Disease", repeat=99, sample=99)
        self.assertLessEqual(len(longest.job_key), 84)
        self.assertTrue(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", longest.job_id))

    def test_job_key_with_repeat_and_sample(self):
        self.assertEqual(ident(phase="calibration", repeat=2).job_key, "val.fewshot_all.LawBench.r2")
        self.assertEqual(ident(phase="calibration", repeat=5, attempt=2).job_id, "val.fewshot_all.LawBench.r5.a2")
        self.assertEqual(ident(phase="controls", sample=3).job_key, "val.fewshot_all.LawBench.s3")
        self.assertEqual(ident(phase="controls", repeat=2, sample=1).job_key, "val.fewshot_all.LawBench.r2.s1")
        self.assertEqual(ident(phase="qualification", repeat=2).job_key, "qual.val.fewshot_all.LawBench.r2")
        for bad in ({"repeat": 1}, {"repeat": 100}, {"sample": 0}, {"sample": 100}, {"repeat": True}):
            with self.assertRaises(ValueError, msg=bad):
                ident(**bad).validate()

    def test_identity_roundtrip_and_validation(self):
        samples = [ident(), ident(phase="search", iteration=12, candidate="mem_v2", split="test", attempt=7),
                   ident(phase="qualification", dataset="USPTO"), ident(phase="calibration", repeat=3),
                   ident(phase="controls", sample=4)]
        for j in samples:
            self.assertEqual(JobIdentity.from_dict(j.to_dict()), j)
            self.assertEqual(JobIdentity.from_dict(json.loads(json.dumps(j.to_dict()))), j)
        d = ident().to_dict()
        for k in ("repeat", "sample", "job_key", "job_id"):
            d.pop(k)
        self.assertEqual(JobIdentity.from_dict(d), ident())         # optional fields default to None
        bad_fields = {"campaign": "MH", "split": "train", "candidate": "Bad", "dataset": "a/b", "attempt": 0,
                      "phase": "recovery", "iteration": 3}
        for key, value in bad_fields.items():
            d = ident().to_dict()
            d[key] = value
            d.pop("job_key"), d.pop("job_id")
            with self.assertRaises(ValueError, msg=key):
                JobIdentity.from_dict(d)
        for key, value in (("attempt", True), ("attempt", "1"), ("campaign", None)):
            d = ident().to_dict()
            d[key] = value
            d.pop("job_key"), d.pop("job_id")
            with self.assertRaises(ValueError, msg=(key, value)):
                JobIdentity.from_dict(d)
        d = ident(phase="search", iteration=1).to_dict()
        d["iteration"] = None
        d.pop("job_key"), d.pop("job_id")
        with self.assertRaises(ValueError):
            JobIdentity.from_dict(d)                                # search jobs carry their iteration
        d = ident().to_dict()
        d["job_id"] = "val.fewshot_all.LawBench.a2"
        with self.assertRaises(ValueError):
            JobIdentity.from_dict(d)                                # a stated id must match the fields
        d = ident().to_dict()
        del d["phase"]
        with self.assertRaises(ValueError):
            JobIdentity.from_dict(d)
        with self.assertRaises(ValueError):
            JobIdentity.from_dict(["not", "a", "mapping"])
        with self.assertRaises(dataclasses.FrozenInstanceError):
            ident().attempt = 2

    def test_ledger_phase_rules(self):
        j = ident(phase="search", iteration=2)
        self.assertEqual(wire.ledger_phase(j, 1, prior_charged=False), "search")
        self.assertEqual(wire.ledger_phase(j, 2, prior_charged=False), "recovery")
        j2 = ident(phase="search", iteration=2, attempt=2)
        self.assertEqual(wire.ledger_phase(j2, 1, prior_charged=True), "recovery")
        self.assertEqual(wire.ledger_phase(j2, 3, prior_charged=True), "recovery")
        q = ident(phase="qualification", attempt=2)
        for n, prior in ((1, False), (2, False), (1, True), (5, True)):
            self.assertEqual(wire.ledger_phase(q, n, prior_charged=prior), "qualification")
        for phase in ("calibration", "baseline", "test", "controls"):
            self.assertEqual(wire.ledger_phase(ident(phase=phase), 1, prior_charged=False), phase)
            self.assertEqual(wire.ledger_phase(ident(phase=phase), 2, prior_charged=False), "recovery")

    def test_ledger_phase_first_attempt_without_charge_stays_in_phase(self):
        # attempt 1 died before any call (for example at sandbox create): attempt 2 is still booked to its own phase
        j = ident(phase="baseline", attempt=2)
        self.assertEqual(wire.ledger_phase(j, 1, prior_charged=False), "baseline")
        self.assertEqual(wire.ledger_phase(ident(attempt=1), 1, prior_charged=True), "baseline")

    def test_refusals_are_402_or_403_and_neutral(self):
        expected_codes = {"halted", "budget_exhausted", "job_request_ceiling", "unknown_job", "not_allowed",
                          "invalid_request", "model_not_allowed", "request_too_large", "provider_rejected",
                          "context_length_exceeded", "provider_unavailable", "relay_unavailable", "relay_overloaded",
                          "gateway_internal"}
        self.assertEqual(set(wire.REFUSALS), expected_codes)
        self.assertEqual(wire.REFUSALS["halted"][0], 402)
        self.assertEqual(wire.REFUSALS["budget_exhausted"][0], 402)
        self.assertEqual(wire.REFUSALS["relay_overloaded"], (403, "mhforge relay: request queue full"))
        for code, (status, message) in wire.REFUSALS.items():
            with self.subTest(code=code):
                self.assertIn(status, (402, 403))
                reply = wire.refusal_reply(code)
                self.assertEqual(reply.status, status)
                helpers.assert_neutral(reply)
                headers = dict(reply.headers)
                self.assertEqual(headers["content-type"], "application/json")
                self.assertEqual(headers["x-should-retry"], "false")
                self.assertEqual(headers[wire.SOURCE_HEADER], "gateway")
                body = json.loads(reply.body)
                self.assertEqual(body, {"error": {"message": message, "type": "mhforge_refusal", "code": code}})
                self.assertEqual(wire.refusal_body(code), reply.body)
                self.assertFalse(re.search(r"\d", message))
                for trigger in wire.RETRY_TRIGGER_STRINGS:
                    self.assertNotIn(trigger, message.lower())
                    self.assertNotIn(trigger, code.lower())
                for pat in (r"\b429\b", r"rate[\s_\-]*limit", r"request too large"):
                    self.assertIsNone(re.search(pat, message, re.I))
                    self.assertIsNone(re.search(pat, code, re.I))
        # the checker itself rejects what it must reject
        for bad in (wire.GatewayReply(429, (("content-type", "application/json"), ("x-should-retry", "false")),
                                      wire.refusal_body("halted")),
                    wire.GatewayReply(403, (("content-type", "application/json"),), wire.refusal_body("halted")),
                    wire.GatewayReply(403, (("content-type", "application/json"), ("x-should-retry", "false")),
                                      b'{"error": {"message": "Rate-Limit hit", "code": "x"}}'),
                    wire.GatewayReply(403, (("content-type", "application/json"), ("x-should-retry", "false")),
                                      b'{"error": {"message": "Request too large", "code": "x"}}'),
                    wire.GatewayReply(403, (("content-type", "application/json"), ("x-should-retry", "false")),
                                      b'{"error": {"message": "retry in 5 s", "code": "x"}}'),
                    wire.GatewayReply(403, (("content-type", "application/json"), ("x-should-retry", "false")),
                                      b'{"error": {"message": "Request TIMED OUT", "code": "x"}}')):
            with self.assertRaises(AssertionError):
                helpers.assert_neutral(bad)
        with self.assertRaises(KeyError):
            wire.refusal_reply("no_such_code")

    def test_envelope_round_trip_and_bad_first_line(self):
        body = json.dumps({"model": "openai/gpt-oss-120b", "messages": [{"role": "user", "content": "a\nb 中"}]}).encode()
        env = wire.make_envelope("POST", "/v1/chat/completions", body)
        self.assertTrue(env.startswith(b"POST /v1/chat/completions\n"))
        self.assertEqual(wire.parse_envelope(env), ("POST", "/v1/chat/completions", body))
        self.assertEqual(wire.parse_envelope(wire.make_envelope("GET", "/v1/models", b"")), ("GET", "/v1/models", b""))
        for bad in (b"POST /v1/chat/completions", b"POST\n{}", b"POST  /x\n", b"post /x\n", b" /x\n", b"POST x\n",
                    b"POST /a b\n", "POST /é\n".encode("utf-8"), b"\n{}", b""):
            with self.assertRaises(ValueError, msg=bad):
                wire.parse_envelope(bad)
        for method, path in (("post", "/x"), ("POST", "x"), ("POST", "/a b"), ("POST", "/a\nb")):
            with self.assertRaises(ValueError):
                wire.make_envelope(method, path, b"")

    def test_candidate_and_campaign_patterns(self):
        for ok in ("x", "no_memory", "fewshot_all", "a" * 48, "mem_v2"):
            self.assertTrue(wire.CANDIDATE_RE.fullmatch(ok), ok)
        for bad in ("x\n", "a" * 49, "1abc", "_x", "Abc", "a-b", "a.b", "", "x y"):
            self.assertIsNone(wire.CANDIDATE_RE.fullmatch(bad), repr(bad))
        self.assertIsNotNone(wire.CANDIDATE_RE.match("x\n"))   # why fullmatch is required
        for ok in ("mh-forge-01", "abc", "a" * 40, "smoke-20261110t010203"):
            self.assertTrue(wire.CAMPAIGN_RE.fullmatch(ok), ok)
        for bad in ("ab", "a" * 41, "-abc", "MH-forge", "mh_forge", "mh-forge\n", "mh.forge"):
            self.assertIsNone(wire.CAMPAIGN_RE.fullmatch(bad), repr(bad))

    def test_bounded_id(self):
        self.assertEqual(wire.bounded_id("abc"), "abc")
        exact = "a" * 128
        self.assertEqual(wire.bounded_id(exact), exact)
        long = "mh-forge-01." + "x" * 121
        out = wire.bounded_id(long)
        self.assertEqual(len(out), 128)
        self.assertEqual(out, long[:119] + "-" + __import__("hashlib").sha256(long.encode()).hexdigest()[:8])
        self.assertEqual(wire.bounded_id(long), out)                  # deterministic
        self.assertNotEqual(wire.bounded_id(long + "y"), out)
        for n in range(0, 400, 7):
            self.assertLessEqual(len(wire.bounded_id("z" * n, 64)), 64)
        self.assertTrue(re.fullmatch(r"[A-Za-z0-9._-]+", out))

    def test_credential_name_pattern(self):
        def matches(name):
            return re.search(wire.CREDENTIAL_NAME_PATTERN, name.upper()) is not None
        for name in ("FOO_API_KEY", "HF_TOKEN", "DB_PASSWORD", "WANDB_API_KEY", "APIKEY", "MY_SECRET", "AWS_CREDENTIALS",
                     "GOOGLE_CREDENTIAL", "ssh_passwd", "KEY", "X_TOKEN_FILE"):
            self.assertTrue(matches(name), name)
        for name in ("TIKTOKEN_RS_CACHE_DIR", "MONKEY", "HOME", "KEYBOARD", "TOKENIZERS_PARALLELISM", "PATH"):
            self.assertFalse(matches(name), name)
        self.assertEqual(wire.CREDENTIAL_NAME_EXEMPT, ("OPENROUTER_API_KEY", "TIKTOKEN_RS_CACHE_DIR"))

    def test_phases_statuses_outcomes_and_scopes_are_the_spec_lists(self):
        self.assertEqual(wire.JOB_PHASES, ("qualification", "calibration", "baseline", "search", "test", "controls"))
        self.assertEqual(wire.LEDGER_PHASES,
                         ("qualification", "calibration", "baseline", "search", "test", "controls", "recovery"))
        self.assertEqual(len(wire.LEDGER_PHASES), 7)
        self.assertEqual(wire.JOB_STATUSES, ("succeeded", "failed", "skipped_usable", "skipped_no_memory", "halted"))
        self.assertEqual(wire.ATTEMPT_OUTCOMES, ("ok", "worker_error", "no_result", "timeout", "request_ceiling",
                                                 "relay_lost", "provider_unavailable", "executor_error",
                                                 "env_setup_failed", "halted", "interrupted"))
        self.assertEqual(wire.INFRA_OUTCOMES, frozenset(wire.ATTEMPT_OUTCOMES[-6:]))
        self.assertEqual(wire.REUSE_SCOPES, ("search", "job", "qualification", "repeat"))
        self.assertEqual(wire.SESSION_OUTCOMES, ("ok", "no_candidates", "timeout", "failed", "spend_limit",
                                                 "limit_window", "auth", "transient", "model_mismatch",
                                                 "sandbox_unavailable", "settings_ignored", "cli_missing", "interrupted"))
        self.assertEqual(wire.CONTENT_FIELDS, ("span_prompt", "span_reply", "span_reasoning", "tool_result"))
        self.assertEqual(wire.RETRY_TRIGGER_STRINGS,
                         ("429", "rate limit", "too many requests", "retry in", "timed out", "timeout"))
        self.assertEqual(wire.SOURCE_HEADER, "x-mhforge-source")
        self.assertEqual(wire.RELAY_API_KEY_PLACEHOLDER, "mhf-relay")

    def test_round_two_constant_lists(self):
        self.assertEqual(wire.CAMPAIGN_KINDS, ("preflight", "measured"))
        self.assertEqual(wire.METERS, ("sandbox_seconds", "lens_bytes", "lens_spans", "proposer_list_price",
                                       "wandb_upload_bytes", "eval_tables"))
        self.assertEqual(wire.LENS_BUCKETS, ("main", "preflight", "recovery"))
        self.assertEqual(wire.FAILURE_CLASSES, ("import_error", "dependency_missing", "context_limit", "request_ceiling",
                                                "stream_budget", "relay_overloaded", "refusal_flood", "harmony_vocab",
                                                "worker_exception", "host_sleep", "unknown"))

    def test_clearance_and_never_publish_constants(self):
        self.assertEqual(wire.NEVER_PUBLISH_TEXT, ("LawBench",))
        self.assertEqual(wire.CLEARANCE_GATED, {"USPTO": "decisions.uspto_text_cleared"})
        manifest = helpers.make_manifest()
        datasets = manifest["upstream"]["expect"]["datasets"]
        for name in wire.NEVER_PUBLISH_TEXT + tuple(wire.CLEARANCE_GATED):
            self.assertIn(name, datasets)
        for decision in wire.CLEARANCE_GATED.values():
            from mhforge.util import lookup
            self.assertIs(lookup(manifest, decision), False)   # the gate exists in the manifest and starts closed

    def test_handles_requests_protocol_is_met_by_fake_gateway(self):
        gw = helpers.FakeGateway([wire.refusal_reply("budget_exhausted")])
        self.assertIsInstance(gw, wire.HandlesRequests)
        j = ident()
        env = wire.make_envelope("POST", "/v1/chat/completions", b"{}")
        self.assertEqual(gw.handle(j, env).body, wire.refusal_body("unknown_job"))
        gw.register_job(j, token=None)
        self.assertEqual(gw.handle(j, env).status, 402)
        self.assertEqual(gw.handle(j, env).status, 200)            # script empty: default completion
        gw.set_halt("operator")
        self.assertEqual(gw.handle(j, env).body, wire.refusal_body("halted"))
        self.assertEqual(gw.halted(), "operator")
        self.assertIsNone(gw.kill_requested(j.job_id))
        gw.request_kill(j.job_id)
        self.assertEqual(gw.kill_requested(j.job_id), "refusal_flood")
        self.assertEqual(gw.close_job(j.job_id)["calls"], 4)
        self.assertEqual([c["path"] for c in gw.calls], ["/v1/chat/completions"] * 4)


if __name__ == "__main__":
    unittest.main()
