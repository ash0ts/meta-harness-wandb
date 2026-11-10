# SPDX-License-Identifier: MIT
import ast
import dataclasses
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from mhforge import scan, util
from tests import helpers

# Secret-shaped samples are assembled at run time so that no file of the repository holds one literally.
HEX40 = "".join("0123456789abcdef"[(i * 7 + 3) % 16] for i in range(40))
HEX64 = "".join("0123456789abcdef"[(i * 5 + 1) % 16] for i in range(64))
GIT_COMMIT = "f805ad3aa4ada290458c1e0deadbe132fbbdea4e"

SAMPLES = {
    "job_token": "mhf" + "job_" + "Ab3_-" * 8 + "xyz",
    "bearer": "Bea" + "rer " + "abcdefghij0123456789KLMN",
    "authorization_header": "author" + "ization=" + "Zm9vYmFyYmF6cXV4",
    "wandb_key": "wandb" + "_v1_" + "q" * 24,
    "keyed_hex40": "api" + "_key = '" + HEX40 + "'",
    "anthropic_key": "sk-" + "ant-" + "a1" * 12,
    "openrouter_key": "sk-" + "or-" + "v1-" + "b2" * 12,
    "github_token": "gh" + "p_" + "C3" * 18,
    "jwt": "ey" + "J" + "a" * 12 + ".ey" + "J" + "b" * 12 + "." + "c" * 12,
    "private_key_block": "-----BEGIN RSA PRIV" + "ATE KEY-----",
    "netrc_line": "machine api.example.com login someone pass" + "word hunter2xyz",
}


def _spec_records() -> dict[str, dict]:
    """The section 2 examples (job file, job record, call record, session record, receipt) as the spec prints them."""
    h = "<64 hex>"
    jobfile = {
        "schema": "mhforge.jobfile/1", "job_id": "val.no_memory.USPTO.a1",
        "argv": ["python3", "/mhf/bin/mhf_capture_entry.py", "--sidecar", "/mhf/out/sidecar.jsonl", "--max-text-chars",
                 "20000", "--", "--memory", "agents/no_memory.py", "--dataset", "USPTO", "--seed", "42", "--model",
                 "openrouter/openai/gpt-oss-120b", "--mode", "offline", "--val-output", "/mhf/out/val.json",
                 "--save-memory", "/mhf/out/memory.json", "--log", "/mhf/out/log.jsonl", "--num-train", "50",
                 "--num-val", "30", "--num-test", "100", "--temperature", "0.0"],
        "env": {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/mhf/home", "TMPDIR": "/mhf/tmp", "PYTHONPATH": "/mhf/pkg",
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1", "TEXT_CLASSIFICATION_LLM_CACHE_DIR": "/mhf/cache",
                "LITELLM_LOCAL_MODEL_COST_MAP": "True", "LITELLM_MODE": "PRODUCTION",
                "TIKTOKEN_RS_CACHE_DIR": "<tokenizers dir>", "NO_PROXY": "127.0.0.1,localhost"},
        "cwd": "/mhf/pkg", "route": 2, "port": 0, "timeout_s": 7200, "reply_timeout_s": 3600, "ping_interval_s": 20,
        "pong_timeout_s": 60, "max_request_bytes": 4000000, "run_dir": "/mhf/run", "expect_outputs": ["/mhf/out/val.json"],
        "vocab_file": "fb374d419588a4632f3f557e76b4b70aebbca790", "repeat": None, "sample": None, "probe_endpoints": [],
        "expect": {"machine": "x86_64", "python": "3.13", "freeze_sha256": None},
    }
    jobrec = {
        "schema": "mhforge.job/1", "campaign": "mh-forge-01", "job_key": "val.no_memory.USPTO", "split": "val",
        "candidate": "no_memory", "dataset": "USPTO", "phase": "baseline", "iteration": None, "repeat": None,
        "sample": None, "seed": 42, "candidate_sha256": h, "route": 2, "executor": "sandbox", "isolated": True,
        "upstream_desc": "val/USPTO/no_memory/gpt-oss-120b",
        "upstream_command": "env PYTHONPATH=<WS parent> uv run --project <WS> python -m text_classification.inner_loop "
                            "--memory agents/no_memory.py ...",
        "inner_argv": ["--memory", "agents/no_memory.py", "--dataset", "USPTO", "--seed", "42", "--model",
                       "openrouter/openai/gpt-oss-120b", "--mode", "offline", "--val-output", "{out}/val.json",
                       "--save-memory", "{out}/memory.json", "--log", "{out}/log.jsonl", "--num-train", "50",
                       "--num-val", "30", "--num-test", "100", "--temperature", "0.0"],
        "status": "succeeded",
        "attempts": [{
            "attempt": 1, "job_id": "val.no_memory.USPTO.a1", "started_utc": "...", "ended_utc": "...",
            "candidate_sha256": h, "candidate_restored": False, "inputs": {}, "returncode": 0, "timed_out": False,
            "kill_reason": None, "outcome": "ok", "infra": False, "failure_class": None,
            "exit_file": {"present": True, "returncode": 0, "reason": "exit", "local_refusals": {}, "unanswered_at_exit": 0},
            "refusal_codes": {}, "result_usable": True, "bundle_sha256": h,
            "outputs": {"val.json": "<sha256>", "memory.json": "<sha256>", "log.jsonl": "<sha256>",
                        "sidecar.jsonl": "<sha256>"},
            "gateway": {"completed": 30, "replayed": 0, "coalesced": 0, "refused": 0, "refused_unrecorded": 0,
                        "provider_rejected": 0, "failed": 0, "refusal_codes": {}, "repeatable_provider_failures": 0,
                        "truncated_empty": 0, "cost_micro": 6480, "bounded_unknown_micro": 0, "in_flight_at_close": 0,
                        "usage_calls": 30, "prompt_tokens_sum": 11130, "prompt_tokens_max": 402,
                        "completion_tokens_sum": 36120},
            "host_sleep_s": 0.0,
            "executor": {"sandbox_id": "sbx-...", "seconds": 412.5, "setup_seconds": 96.0, "cost_micro": 39059,
                         "environment": {"env_mode": "wheelhouse", "image_digest": "sha256:" + h, "requirements_sha256": h,
                                         "sha256sums_sha256": h, "freeze_sha256": h, "machine": "x86_64",
                                         "python": "3.13.18"},
                         "relay": {"mode": "daemon", "attaches": 1, "rotations": 0, "frames_in": 31, "frames_out": 30,
                                   "chars_in": 181204, "noise_chars": 0, "frame_errors": 0, "duplicate_requests": 0,
                                   "cancelled": 0, "pings": 20, "flood_refusals": 0},
                         "selfcheck": "passed"}}],
        "published": {"at_utc": "...",
                      "files": {"logs/mh-forge-01/USPTO/no_memory/gpt-oss-120b/val.json": "<sha256>",
                                "logs/mh-forge-01/USPTO/no_memory/gpt-oss-120b/memory.json": "<sha256>",
                                "logs/mh-forge-01/USPTO/no_memory/gpt-oss-120b/log.jsonl": "<sha256>"},
                      "launcher_log": "logs/mh-forge-01/.launcher/00_val_USPTO_no_memory_gpt-oss-120b.log"},
        "result": {"accuracy": 0.1, "correct": 3, "total": 30, "memory_context_chars": 191},
        "name_collision": False, "export_unit": "job:val.no_memory.USPTO.a1",
    }
    call = {
        "schema": "mhforge.call/1", "call_id": "c-20261110T031502-9f2c41d07a6b",
        "job": {"campaign": "mh-forge-01", "split": "val", "candidate": "fewshot_all", "dataset": "LawBench", "attempt": 1,
                "phase": "baseline", "iteration": None, "repeat": None, "sample": None,
                "job_key": "val.fewshot_all.LawBench", "job_id": "val.fewshot_all.LawBench.a1"},
        "ingress": "relay", "outcome": "completed", "refusal": None, "received_utc": "2026-11-10T03:15:02.417000Z",
        "admitted_utc": "2026-11-10T03:15:02.420000Z", "ended_utc": "2026-11-10T03:15:41.873000Z", "queue_s": 6.682,
        "request": {"model": "openai/gpt-oss-120b",
                    "messages": [{"role": "system", "content": "Reasoning: medium"}, {"role": "user", "content": "..."}],
                    "temperature": 0.0, "max_tokens": 16384},
        "dropped_keys": ["usage"], "request_sha256": h, "user_sha256": h, "content_bytes": 68173, "n_messages": 2,
        "input_limit": 76397, "output_limit": 16384,
        "conformance": {"temperature": True, "max_tokens": True, "system_prompt": True}, "replay": None, "reuse_salt": None,
        "attempts": [{"n": 1, "ledger_id": "l-20261110T031509-0c11aa90be21", "ledger_phase": "baseline",
                      "started_utc": "2026-11-10T03:15:09.102000Z", "first_byte_utc": "2026-11-10T03:15:11.540000Z",
                      "ended_utc": "2026-11-10T03:15:41.870000Z", "http_status": 200, "disposition": "ok", "done": True,
                      "error": None, "response_headers": {"content-type": "text/event-stream", "x-request-id": "..."},
                      "sse_file": "c-20261110T031502-9f2c41d07a6b.a1.sse", "sse_bytes": 48211, "reserved_micro": 5078,
                      "spent_micro": 972, "ledger_status": "complete", "backoff_s": 0.0}],
        "response": {"id": "chatcmpl-...", "object": "chat.completion", "created": 1794270000,
                     "model": "openai/gpt-oss-120b",
                     "choices": [{"index": 0, "finish_reason": "stop",
                                  "message": {"role": "assistant", "content": "...", "reasoning": "..."}}],
                     "usage": {"prompt_tokens": 24310, "completion_tokens": 1422, "total_tokens": 25732}},
        "usage": {"prompt_tokens": 24310, "completion_tokens": 1422, "total_tokens": 25732, "cached_tokens": None,
                  "reasoning_tokens": None},
        "finish_reason": "stop", "response_model": "openai/gpt-oss-120b", "response_id": "chatcmpl-...",
        "flags": {"truncated_empty": False, "empty_content": False, "reasoning_present": True, "model_mismatch": False},
        "cost_micro": 972, "cost_basis": "usage", "reply": {"status": 200, "code": None, "source": "provider"},
    }
    session = {
        "schema": "mhforge.session/1", "campaign": "mh-forge-01", "iteration": 3, "ordinal": 1, "session_key": "iter003.s1",
        "permission_mode": "restricted", "cli": {"path": "<pinned path>", "sha256": h, "version": "2.1.317"},
        "argv_sha256": h, "argv_flags": ["-p", "--output-format", "..."], "settings_sha256": h,
        "settings_override": False, "addendum_sha256": h, "scratch_dir": "<proposer.scratch_root>/mh-forge-01/iter003.s1",
        "env_names": ["CLAUDE_CODE_DISABLE_AUTO_MEMORY", "CLAUDE_CODE_STARTUP_FAILURE_RESULTS", "DISABLE_AUTOUPDATER",
                      "HOME", "LANG", "PATH", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH", "UV_CACHE_DIR", "UV_FROZEN",
                      "UV_NO_SYNC", "UV_OFFLINE", "UV_PROJECT_ENVIRONMENT", "UV_PYTHON_DOWNLOADS", "VIRTUAL_ENV"],
        "started_utc": "...", "ended_utc": "...", "duration_s": 1375.4, "timeout_s": 2400, "paused_s": 0.0,
        "exit_code": 0, "timed_out": False, "signals": [], "outcome": "ok", "outcome_detail": None,
        "session_id": "<CLI session id>", "requested_model": "opus", "resolved_model": "claude-opus-5-5", "effort": "max",
        "model_check": {"init_model": "claude-opus-5-5", "main_thread_models": ["claude-opus-5-5"],
                        "subagent_models": ["claude-haiku-5-5"], "fallback_events": [], "init_effort": "max",
                        "request_efforts": ["max"], "mismatch": None},
        "init": {"tools": ["Read", "..."], "plugins": [], "plugin_errors": [], "skills": [], "mcp_servers": [],
                 "memory_paths": [], "permission_mode": "dontAsk", "claude_code_version": "2.1.317"},
        "result_event": {"present": True, "subtype": "success", "is_error": False, "api_error_status": None,
                         "total_cost_usd": "12.31", "num_turns": 87, "usage": {}, "model_usage": {},
                         "permission_denials": 2, "startup_failure_reason": None},
        "limit": {"signal": None, "status": None, "rate_limit_type": None, "resets_at_utc": None, "error_code": None,
                  "overage_status": None, "overage_disabled_reason": None},
        "sandbox_check": {"transcript_checked": True, "bash_results": 14, "unsandboxed": 0, "unknown": 0},
        "integrity": {"violations": []}, "list_price_micro": 12310000, "list_price_basis": "result_event",
        "pending_eval": {"present": True, "sha256": h,
                         "candidates": [{"name": "x", "base_system": "fewshot_all", "valid_name": True, "file_sha256": h}]},
        "transcript": {"found": True, "files": 3, "bytes": 1048576},
        "drift": {"agents_added": ["agents/x.py"], "agents_modified": [], "upstream_modified": [], "upstream_removed": [],
                  "other_added": ["agents/__notes__"], "quarantined": ["scratch_notes.md"], "logs_modified": [],
                  "restored": []},
        "env_fingerprint_before": h, "env_fingerprint_after": h, "auto_memory_dir_changed": False,
        "solver_attempt_signals": [], "state_access_signals": [],
        "wrapper_log_dir": "logs/mh-forge-01/claude_sessions/20261110_010203_iter3",
    }
    receipt = {
        "schema": "mhforge.receipt/1", "unit": "job:val.fewshot_all.LawBench.a1", "surface": "agent_lens",
        "export_attempt": 1, "status": "verified", "plan_sha256": h, "stale_plan_sha256": None,
        "scan": {"clean": True, "findings": 0, "scanned_bytes": 412338, "files": 53, "rules": 16,
                 "classes": ["secret", "privacy"]},
        "amendment": {"seq": 0, "row_sha256": None}, "bucket": "main", "withheld_files": [],
        "conversation_ids": ["mh-forge-01.val.fewshot_all.LawBench.a1"], "trace_ids": ["4bf92f3577b34da6a3ce929d0e0e4736"],
        "expected_spans": 51, "readback": {"spans": 51, "checked_utc": "...", "waited_s": 6.0, "mismatches": []},
        "remote": {"run_path": None, "table_keys": [], "artifact_names": [], "backend": None, "base_url": None},
        "urls": ["https://forge.coreweave.com/agent-lens/<entity>/<project>/conversations/"
                 "mh-forge-01.val.fewshot_all.LawBench.a1"],
        "planned_bytes": 96412, "planned_spans": 51, "adapter": {"name": "coreweave", "version": "0.1.0b0", "unverified": True},
        "created_utc": "...", "updated_utc": "...", "error": None,
        "history": [{"status": "planned", "utc": "..."}, {"status": "scanned", "utc": "..."},
                    {"status": "emitting", "utc": "..."}, {"status": "emitted", "utc": "..."}],
    }
    # the example values with the placeholders filled by realistic digests and a git commit
    filled = json.loads(json.dumps(jobrec).replace("<64 hex>", HEX64).replace("<sha256>", HEX64))
    filled["upstream_desc"] = "val/USPTO/no_memory/gpt-oss-120b @ " + GIT_COMMIT
    return {"jobfile": jobfile, "job": jobrec, "job_filled": filled, "call": call, "session": session, "receipt": receipt}


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mhf-scan-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_each_pattern_found_without_returning_text(self):
        self.assertEqual(set(SAMPLES), set(scan.PATTERNS))
        self.assertEqual(len(scan.PATTERNS), 11)
        self.assertEqual({f.name for f in dataclasses.fields(scan.Finding)}, {"pattern", "path", "offset", "length"})
        for name, sample in SAMPLES.items():
            with self.subTest(pattern=name):
                data = ("prefix line\n  " + sample + "  \nsuffix").encode()
                findings = scan.scan_bytes(data, path="rec.json")
                hits = [f for f in findings if f.pattern == name]
                self.assertTrue(hits, f"{name} not found")
                start = data.index(sample.encode())
                self.assertTrue(any(f.offset <= start + 4 and f.offset + f.length >= start + 8 for f in hits))
                self.assertTrue(all(f.path == "rec.json" for f in findings))
                for f in findings:
                    self.assertNotIn(sample, repr(f))
                    for value in dataclasses.astuple(f):
                        self.assertFalse(isinstance(value, (bytes, str)) and sample[4:] in str(value))
        self.assertEqual(scan.scan_bytes(b"plain text, nothing secret here: 1234 abcd"), [])
        self.assertEqual(len(scan.ALL_RULES), 16)

    def test_known_value_found_anywhere(self):
        key = "zz-legacy-" + "Q" * 6
        data = ("a" + key + "b|" + key).encode()
        found = scan.scan_bytes(data, known=[key.encode()])
        self.assertEqual([(f.pattern, f.offset, f.length) for f in found],
                         [("known_value", 1, len(key)), ("known_value", len(key) + 3, len(key))])
        self.assertEqual(scan.scan_bytes(data, known=[key]), found)                  # str accepted too
        self.assertEqual(scan.scan_bytes(b"abcdefg", known=[b"abcdefg"]), [])       # shorter than 8: not matched
        nested = {"x": [{"y": "pre " + key}]}
        self.assertEqual([f.path for f in scan.scan_obj(nested, known=[key.encode()], path="doc")], ["doc/x[0]/y"])
        self.assertEqual(scan.scan_bytes(data, known=[key.encode()], classes=("privacy",)), [])

    def test_bare_key_found_by_digest(self):
        key = HEX40                                         # a legacy W&B key shape: 40 bare hex characters
        text = f"line one\n{key}\nline three\n".encode()
        self.assertEqual(scan.scan_bytes(text), [], "a bare 40-hex value must not match any pattern")
        digests_file = self.tmp / "keydigests.json"
        util.append_key_digest(digests_file, "MHFORGE_EXPORT_API_KEY", key)
        del key                                             # no process holds the key any more: only its digest
        digests = scan.load_digests(digests_file)
        self.assertEqual(len(digests), 1)
        self.assertEqual(digests[0][0], 40)
        found = scan.scan_bytes(text, digests=digests)
        self.assertEqual([(f.pattern, f.offset, f.length) for f in found], [("known_digest", 9, 40)])
        # inside a longer run of key characters, and inside files and objects
        longer = b"xx_" + HEX40.encode() + b"_yy"
        self.assertEqual([f.offset for f in scan.scan_bytes(longer, digests=digests)], [3])
        f = self.tmp / "out.txt"
        f.write_bytes(text)
        self.assertEqual([x.pattern for x in scan.scan_paths([self.tmp], digests=digests)], ["known_digest"])
        self.assertEqual([x.path for x in scan.scan_obj({"log": [HEX40]}, digests=digests)], ["/log[0]"])
        self.assertEqual(scan.scan_bytes(b"0" * 40, digests=digests), [])

    def test_upper_case_key_names_found(self):
        for line in ("MHFORGE_FORGE_API_KEY=" + HEX40, "WANDB_API_KEY: " + HEX40, "export SOME_TOKEN=" + HEX40,
                     '{"password": "' + HEX40 + '"}', "Api-Key " + HEX40, "secret='" + HEX40.upper() + "'"):
            with self.subTest(line=line[:24]):
                self.assertEqual([f.pattern for f in scan.scan_bytes(line.encode())], ["keyed_hex40"])

    def test_keyed_hex40_double_encoded_and_argv_pair_forms(self):
        name = "api" + "_key"
        double = json.dumps({"arguments": json.dumps({name: HEX40})})
        argv = ["python3", "run.py", "--api-" + "key", HEX40, "--seed", "42"]
        for text in (double, json.dumps(argv), json.dumps(json.dumps(argv)), json.dumps(argv, indent=2)):
            with self.subTest(text=text[:30]):
                found = scan.scan_bytes(text.encode(), path="r")
                self.assertEqual([f.pattern for f in found], ["keyed_hex40"])
                self.assertNotIn(HEX40, repr(found))
        found = scan.scan_obj({"argv": argv}, path="job")
        self.assertEqual([(f.pattern, f.path, f.offset, f.length) for f in found], [("keyed_hex40", "job/argv[3]", 0, 40)])
        self.assertEqual(len(scan.scan_obj({"arguments": json.dumps({name: HEX40})})), 1)
        for clean in (["--commit", GIT_COMMIT], ["--vocab-file", HEX40], ["--api-" + "key", HEX64], ["token", 3, HEX40]):
            with self.subTest(clean=clean[0]):
                self.assertEqual(scan.scan_obj(clean), [])
                self.assertEqual(scan.scan_bytes(json.dumps(clean).encode()), [])

    def test_authorization_header_json_form(self):
        header = "Author" + "ization"
        value = "Zm9vYmFyYmF6cXV4"
        for text in (json.dumps({header: value}), json.dumps({"headers": {header: "Basic " + value}}),
                     json.dumps(json.dumps({header: value})), "{'" + header + "': '" + value + "'}"):
            with self.subTest(text=text[:30]):
                found = scan.scan_bytes(text.encode())
                self.assertEqual([f.pattern for f in found], ["authorization_header"])
                self.assertNotIn(value, repr(found))
        found = scan.scan_obj({"headers": {header: value}}, path="rec")
        self.assertEqual([(f.pattern, f.path, f.offset, f.length) for f in found],
                         [("authorization_header", "rec/headers/" + header, 0, len(value))])
        self.assertEqual(scan.scan_obj({header: "short"}), [])
        self.assertEqual(scan.scan_bytes(json.dumps({header: "short"}).encode()), [])

    def test_key_with_wider_characters_found_by_digest(self):
        key = "AbC.def+Ghi/jkl=MNO-pqr_stu.vwx"
        digests_file = self.tmp / "keydigests.json"
        util.append_key_digest(digests_file, "MHFORGE_EXPORT_API_KEY", key)
        digests = scan.load_digests(digests_file)
        text = ("token was " + key + " end").encode()
        found = scan.scan_bytes(text, digests=digests)
        self.assertEqual([(f.pattern, f.offset, f.length) for f in found], [("known_digest", 10, len(key))])
        self.assertNotIn(key, repr(found))
        # a key without the wider characters is still found once, not twice
        plain = HEX40
        util.append_key_digest(digests_file, "MHFORGE_FORGE_API_KEY", plain)
        digests = scan.load_digests(digests_file)
        self.assertEqual(len(scan.scan_bytes(("a/" + plain + "/b").encode(), digests=digests)), 1)

    def test_git_hash_and_sha256_are_not_findings(self):
        clean = [
            "commit " + GIT_COMMIT, '"upstream": {"commit": "' + GIT_COMMIT + '"}',
            '"tokenizer_file": "' + HEX40 + '"', '"vocab_file": "' + HEX40 + '"', '"token_sha256": "' + HEX64 + '"',
            "tiktoken " + HEX40, '"sha256": "' + HEX64 + '"', "token: " + HEX64, "api_key = " + HEX64,
            "secret_id=" + HEX40, "monkey_token_count: " + HEX40[:39], '"request_sha256": "' + HEX64 + '"',
            "sha256:" + HEX64,
        ]
        for text in clean:
            with self.subTest(text=text[:30]):
                self.assertEqual(scan.scan_bytes(text.encode()), [])

    def test_spec_records_are_clean(self):
        records = _spec_records()
        for name, rec in records.items():
            for form, data in (("indented", json.dumps(rec, indent=2).encode()),
                               ("compact", json.dumps(rec, separators=(",", ":")).encode()),
                               ("canonical", util.canonical_json(rec)),
                               ("ascii", json.dumps(rec).encode())):
                with self.subTest(record=name, form=form):
                    self.assertEqual(scan.scan_bytes(data), [])
            self.assertEqual(scan.scan_obj(rec), [])
        lock = helpers.example_dir() / "uv.lock"
        if lock.is_file():
            self.assertEqual(scan.scan_paths([lock]), [])

    def test_manifest_fixture_has_no_finding(self):
        # validation rule 11 (layer 1) refuses any manifest string that matches a scan pattern
        doc = helpers.make_manifest()
        self.assertEqual(scan.scan_obj(doc), [])
        self.assertEqual(scan.scan_paths([helpers.MANIFEST_FIXTURE]), [])

    def test_scan_obj_walks_nested(self):
        doc = {"a": {"b": [1, None, True, {"c": "x " + SAMPLES["github_token"]}]}, "t": ("y", SAMPLES["wandb_key"]),
               "raw": SAMPLES["jwt"].encode(), "n": 3.5, SAMPLES["anthropic_key"]: "key in a key"}
        found = scan.scan_obj(doc, path="rec")
        by_path = {f.path: f.pattern for f in found}
        self.assertEqual(by_path, {"rec/a/b[3]/c": "github_token", "rec/t[1]": "wandb_key", "rec/raw": "jwt",
                                   "rec/" + SAMPLES["anthropic_key"] + "#key": "anthropic_key"})
        self.assertEqual(scan.scan_obj({"x": [1, 2.0, None, False]}), [])
        with self.assertRaises(ValueError):
            scan.scan_obj({}, classes=("nope",))

    def test_scan_obj_finds_keyed_hex40_across_key_and_value(self):
        # the record form of "WANDB_API_KEY: <40 hex>": the name is a dict key, the hex its value
        for key in ("WANDB_API_KEY", "password", "MHFORGE_FORGE_API_KEY", "token"):
            with self.subTest(key=key):
                found = scan.scan_obj({"env": {key: HEX40}}, path="rec")
                self.assertEqual([(f.pattern, f.path, f.offset, f.length) for f in found],
                                 [("keyed_hex40", "rec/env/" + key, 0, 40)])
                self.assertEqual(scan.scan_obj({key: " '" + HEX40 + "'"})[0].offset, 2)
                self.assertEqual(scan.scan_obj({key: HEX40}, classes=("privacy",)), [])
        for key, value in (("tokenizer_file", HEX40), ("vocab_file", HEX40), ("token_sha256", HEX64),
                           ("secret_id", HEX40), ("commit", GIT_COMMIT), ("api_key", HEX64), ("api_key", 3)):
            with self.subTest(clean=key):
                self.assertEqual(scan.scan_obj({key: value}), [])
        # a value that carries its own name is found once, by the value alone
        self.assertEqual(len(scan.scan_obj({"token": "api_key=" + HEX40})), 1)

    def test_single_known_value_and_hostname_are_not_split_into_characters(self):
        key = "zz-legacy-" + "Q" * 6
        for known in (key, key.encode()):
            self.assertEqual([f.pattern for f in scan.scan_bytes(b"x" + key.encode(), known=known)], ["known_value"])
        found = scan.scan_bytes(b"on devbox-17 ok", classes=("privacy",), hostnames="devbox-17")
        self.assertEqual([f.pattern for f in found], ["hostname"])

    def test_load_digests_absent_file_is_empty(self):
        self.assertEqual(scan.load_digests(self.tmp / "keydigests.json"), [])
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps({"schema": "other/1", "keys": []}))
        from mhforge.errors import StateError
        with self.assertRaises(StateError):
            scan.load_digests(bad)
        bad.write_text(json.dumps({"schema": "mhforge.keydigests/1", "keys": [{"len": "40", "sha256": HEX64}]}))
        with self.assertRaises(StateError):
            scan.load_digests(bad)

    def test_privacy_patterns_only_when_class_requested(self):
        text = b"open /Users/example/rsi/x.py and /home/someone/work/y.py"
        self.assertEqual(scan.scan_bytes(text), [])
        found = scan.scan_bytes(text, classes=("privacy",))
        self.assertEqual([(f.pattern, f.offset) for f in found],
                         [("local_path_users", 5), ("local_path_home", text.index(b"/home/"))])
        both = scan.scan_bytes(text + b" " + SAMPLES["github_token"].encode(), classes=("secret", "privacy"))
        self.assertEqual(sorted(f.pattern for f in both), ["github_token", "local_path_home", "local_path_users"])
        self.assertEqual(scan.scan_obj({"p": "/Users/example/"}, classes=["privacy"])[0].pattern, "local_path_users")
        self.assertEqual(scan.scan_bytes(b"/Users/ alone and /usr/local/bin"), [])
        self.assertEqual(scan.SCAN_CLASSES, ("secret", "privacy"))

    def test_hostname_passed_as_data_whole_word_only(self):
        host = "devbox-17"
        hits = {"ran on devbox-17: ok": 7, "DEVBOX-17 says": 0, "user@devbox-17 $": 5, "(devbox-17)": 1}
        for text, offset in hits.items():
            found = scan.scan_bytes(text.encode(), classes=("privacy",), hostnames=[host])
            self.assertEqual([(f.pattern, f.offset, f.length) for f in found], [("hostname", offset, len(host))], text)
        for text in ("devbox-170", "xdevbox-17", "devbox-17.local", "my-devbox-17", "devbox_17"):
            self.assertEqual(scan.scan_bytes(text.encode(), classes=("privacy",), hostnames=[host]), [], text)
        self.assertEqual(scan.scan_bytes(b"ran on devbox-17", hostnames=[host]), [])       # default class: secret only
        self.assertEqual(scan.scan_bytes(b"on abc here", classes=("privacy",), hostnames=["abc"]), [])  # < 4 chars

    def test_verbatim_run_and_exact_match(self):
        question = ("SYNTHETIC patient reports a persistent dry cough, mild fever and a sore throat for three days, "
                    "worse at night.")
        index = scan.build_verbatim_index([question], ["CCO>>c1ccccc1", "short"], run_chars=40)
        self.assertEqual(index.run_chars, 40)
        self.assertEqual(index.exact, ("CCO>>c1ccccc1",))                 # strings under 8 characters are dropped
        self.assertTrue(all(len(h) == 8 for h in index.run_hashes))
        quote40 = question[17:57]
        quote39 = question[17:56]
        found = scan.scan_verbatim("The reply quoted: " + quote40 + " and stopped.", index, path="cand.py")
        self.assertEqual([(f.pattern, f.offset, f.length, f.path) for f in found],
                         [("dataset_text_run", 18, 40, "cand.py")])
        self.assertEqual(scan.scan_verbatim("The reply quoted: " + quote39 + " and stopped.", index), [])
        whole = scan.scan_verbatim("<<" + question + ">>", index)
        self.assertEqual([(f.pattern, f.offset, f.length) for f in whole], [("dataset_text_run", 2, len(question))])
        exact = scan.scan_verbatim("x = 'CCO>>c1ccccc1'  # CCO>>c1ccccc1", index)
        self.assertEqual([(f.pattern, f.offset) for f in exact], [("dataset_text_exact", 5), ("dataset_text_exact", 23)])
        self.assertEqual(scan.scan_verbatim("CCO>>c1ccccc", index), [])
        for f in found + exact:
            self.assertNotIn(quote40, repr(f))
        empty = scan.build_verbatim_index([], [])
        self.assertEqual(scan.scan_verbatim(question, empty), [])

    def test_scan_reads_nothing_from_the_machine(self):
        src = Path(scan.__file__).read_text("utf-8")
        tree = ast.parse(src)
        banned_modules = {"socket", "getpass", "platform", "pwd", "subprocess", "urllib", "http", "shutil"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotIn(alias.name.split(".")[0], banned_modules)
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn((node.module or "").split(".")[0], banned_modules)
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr, {"environ", "getenv", "uname", "gethostname", "getlogin", "home",
                                             "expanduser", "getuser"})
        self.assertNotIn("os.environ", src)
        readers = {"open", "read_bytes", "read_text", "read_json", "read_jsonl", "fdopen"}
        allowed = {"scan_paths", "load_digests"}

        def visit(node, func):
            for child in ast.iter_child_nodes(node):
                name = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
                if isinstance(child, ast.Call):
                    callee = child.func
                    called = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
                    if called in readers:
                        self.assertIn(name, allowed, f"file read in {name}")
                visit(child, name)

        visit(tree, None)
        # and at run time: with file access cut off, the pure functions still work
        index = scan.build_verbatim_index(["x" * 50])
        with mock.patch("builtins.open", side_effect=AssertionError("scan opened a file")):
            self.assertEqual(scan.scan_bytes(b"x" * 10, digests=[(10, HEX64)], hostnames=["devbox-17"],
                                             classes=("secret", "privacy")), [])
            self.assertEqual(scan.scan_obj({"a": "b"}), [])
            self.assertTrue(scan.scan_verbatim("y" + "x" * 45, index))


if __name__ == "__main__":
    unittest.main()
