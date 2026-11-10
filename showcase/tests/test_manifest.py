# SPDX-License-Identifier: Apache-2.0
# Adapted from ash0ts/harness2-wandb@31b0f82 tests/showcase/test_safety.py (test_manifest_cannot_drift_after_first_attempt).
# Changes: rewritten for unittest as test_freeze_then_change_is_refused against manifest.freeze/load_frozen; every other
# case in this file is new.
import contextlib
import copy
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from mhforge import manifest as M
from mhforge import util
from mhforge.errors import LiveGateError, ManifestError
from mhforge.paths import StatePaths, resolve_state
from tests import helpers

ENGINE = {"engine_sha256": "e" * 64, "files": {"mhforge/util.py": "f" * 64}}
KEY_CANARY = "canary-key-value-7f3a9c"
ENV = {"MHFORGE_FORGE_API_KEY": KEY_CANARY, "MHFORGE_SANDBOX_API_KEY": KEY_CANARY, "MHFORGE_EXPORT_API_KEY": KEY_CANARY,
       "PATH": "/usr/bin"}
CASES = [("account-check", None), ("preflight", None), ("search", None), ("evaluate", "baselines"),
         ("evaluate", "calibration"), ("evaluate", "noise"), ("evaluate", "test"), ("export", None), ("publish", None)]
DRAFT = helpers.SC / "manifest.draft.json"


def fields(blockers):
    return {f for f, _ in blockers}


@dataclass
class Ready:
    sc: Path
    manifest: dict
    path: Path
    paths: StatePaths

    def blockers(self, command, mode=None, environ=ENV, manifest=None, **kw):
        kw.setdefault("live_flag", True)
        kw.setdefault("engine_sha256", ENGINE["engine_sha256"])
        return M.live_blockers(manifest or self.manifest, command, environ, self.paths, mode=mode, **kw)


@contextlib.contextmanager
def ready(overrides=None, *, freeze=True, qualified=True):
    """A temp SC in which every live command passes for the fixture manifest (plus `overrides`)."""
    with tempfile.TemporaryDirectory(prefix="mhf-man-") as tmp:
        sc = Path(tmp).resolve() / "sc"
        sc.mkdir()
        cli = sc / "claude-bin"
        cli.write_bytes(b"fake claude binary")
        base = {"executor.allow_unisolated_live": True, "proposer.cli_path": str(cli),
                "proposer.cli_sha256": util.sha256_file(cli)}
        base.update(overrides or {})
        path = helpers.write_manifest(sc, helpers.make_manifest(base))
        m = M.read_manifest(path)
        paths = resolve_state(m, path)
        if freeze:
            M.freeze(m, paths, ENGINE)
        if qualified:
            util.write_json(paths.qualification_passed, {"schema": "mhforge.passed/1", "manifest_sha256": M.manifest_sha256(m),
                                                         "engine_sha256": ENGINE["engine_sha256"], "imported_from": None})
            util.write_json(paths.calibration, {"schema": "mhforge.calibration/1", "verdict": "pass"})
        (sc / "protocol.md").write_text("# Protocol\nsigned in both stages\n")
        util.append_jsonl(paths.journal, {"schema": "mhforge.journal/1", "seq": 1, "op": "run", "key": "search",
                                          "status": "complete", "attempt": None, "detail": {}})
        for ds in m["upstream"]["expect"]["datasets"]:
            for system in m["upstream"]["expect"]["baselines"]:
                d = paths.logs_dir(m["campaign"]) / ds / system / m["upstream"]["expect"]["model_short"]
                util.write_json(d / "val.json", {"accuracy": 0.5})
                util.write_json(d / "memory.json", {})
        yield Ready(sc, m, path, paths)


def set_halt(paths, reason="operator"):
    paths.root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(paths.ledger)
    conn.execute("CREATE TABLE IF NOT EXISTS halt (id INTEGER PRIMARY KEY CHECK (id = 1), reason TEXT NOT NULL,"
                 " detail TEXT NOT NULL DEFAULT '', call_id TEXT, set_utc TEXT NOT NULL)")
    conn.execute("INSERT INTO halt (id, reason, detail, set_utc) VALUES (1, ?, '', ?)", (reason, helpers.FakeClock().utc()))
    conn.commit()
    conn.close()


class ValidationTest(unittest.TestCase):
    def problems(self, overrides=None, *, manifest=None):
        with tempfile.TemporaryDirectory(prefix="mhf-val-") as tmp:
            doc = manifest if manifest is not None else helpers.make_manifest(overrides or {})
            path = helpers.write_manifest(Path(tmp) / "sc", doc)
            return M.validate(M.read_manifest(path), path)

    def assertProblem(self, overrides, field, *, manifest=None):
        found = self.problems(overrides, manifest=manifest)
        self.assertTrue(any(p.startswith(field) for p in found), f"no problem for {field}: {found}")
        return found

    def test_fixture_validates(self):
        self.assertEqual(self.problems(), [])

    def test_draft_manifest_validates(self):
        draft = json.loads(DRAFT.read_text("utf-8"))
        self.assertEqual(self.problems(manifest=draft), [])
        d = draft["decisions"]
        self.assertEqual((d["live_spend_approved"], d["key_provisioning"], d["entity"], d["project"],
                          d["proposer_permission_mode"], d["evaltables_backend"], d["publish_approved"],
                          d["dataset_text_publication"], d["uspto_text_cleared"]),
                         (False, "unset", None, None, "unset", "unset", False, "unset", False))
        self.assertIsNone(draft["upstream"]["tree_sha256"])
        for key in ("cli_path", "cli_sha256", "cli_version", "max_budget_usd_per_session", "session_estimate_usd"):
            self.assertIsNone(draft["proposer"][key], key)
        self.assertIsNone(draft["budgets"]["proposer_list_usd"])

    def test_unknown_key_and_bad_enum_rejected(self):
        self.assertProblem({"provider.retry.max_retries": 2}, "provider.retry.max_retries: unknown key")
        self.assertProblem({"extra_top": 1}, "extra_top: unknown key")
        self.assertProblem({"routing.route": 3}, "routing.route")
        self.assertProblem({"routing.route": True}, "routing.route")
        self.assertProblem({"decisions.key_provisioning": "yes"}, "decisions.key_provisioning")
        self.assertProblem({"gateway.max_in_flight": helpers.DELETE}, "gateway.max_in_flight: required")
        self.assertProblem({"executor.max_attempts": 1.0}, "executor.max_attempts")
        self.assertEqual(self.problems({"notes": helpers.DELETE}), [])

    def test_phase_caps_must_cover_open_reservations(self):
        self.assertEqual(M.max_reservation_micro(helpers.make_manifest()), 6718)
        self.assertProblem({"budgets.solver_usd.phases.controls": "0.20"}, "budgets.solver_usd.phases.controls")
        self.assertEqual(self.problems({"budgets.solver_usd.phases.controls": "0.215"}), [])
        self.assertEqual(self.problems({"budgets.solver_usd.phases.controls": "0"}), [])
        self.assertProblem({"gateway.max_job_usd": "0.10"}, "gateway.max_job_usd")
        self.assertProblem({"gateway.max_in_flight": 64, "gateway.max_job_usd": "0.50"}, "gateway.max_job_usd")

    def test_lower_max_output_tokens_rejected(self):
        self.assertProblem({"gateway.max_output_tokens": 16383}, "gateway.max_output_tokens")

    def test_floors_for_content_and_request_bytes(self):
        self.assertProblem({"gateway.max_content_bytes": 899999}, "gateway.max_content_bytes")
        self.assertProblem({"gateway.max_request_bytes": 2749999}, "gateway.max_request_bytes")
        self.assertProblem({"gateway.max_messages": 1}, "gateway.max_messages")
        self.assertEqual(self.problems({"gateway.max_content_bytes": 900000, "gateway.max_request_bytes": 2750000}), [])

    def test_cached_price_above_input_price_rejected(self):
        self.assertProblem({"prices.cached_per_million": "0.04"}, "prices.cached_per_million")
        self.assertProblem({"prices.input_per_million": "-1"}, "prices.input_per_million")
        self.assertProblem({"prices.model": "other/model"}, "prices.model")
        self.assertEqual(self.problems({"prices.cached_per_million": "0.015"}), [])

    def test_image_must_be_digest_and_not_docker_hub(self):
        digest = "@sha256:" + "a" * 64
        self.assertProblem({"sandbox.image": "registry.example.com/x:latest"}, "sandbox.image")
        self.assertProblem({"sandbox.image": "registry.example.com/x@sha256:" + "a" * 63}, "sandbox.image")
        self.assertProblem({"sandbox.image": "docker.io/library/python" + digest}, "sandbox.image: must not be on docker.io")
        self.assertProblem({"sandbox.base_image": "registry-1.docker.io/library/python" + digest,
                            "sandbox.base_image_fallback": None}, "sandbox.base_image")
        self.assertEqual(self.problems({"sandbox.image": "ghcr.io:443/org/worker" + digest}), [])

    def test_image_without_registry_host_is_docker_hub(self):
        digest = "@sha256:" + "a" * 64
        for image in ("mhforge/worker" + digest, "library/python" + digest):
            self.assertProblem({"sandbox.image": image}, "sandbox.image: must not be on docker.io")
            self.assertProblem({"sandbox.base_image": image, "sandbox.base_image_fallback": None},
                               "sandbox.base_image: must not be on docker.io")
        self.assertEqual(self.problems({"sandbox.image": "localhost:5000/worker" + digest}), [])
        self.assertEqual(self.problems({"sandbox.image": "registry:5000/worker" + digest}), [])

    def test_docker_hub_allowed_only_as_fallback_with_same_digest(self):
        self.assertEqual(self.problems(), [])                 # the fixture's fallback is on docker.io with the same digest
        self.assertProblem({"sandbox.base_image_fallback": "docker.io/library/python@sha256:" + "b" * 64},
                           "sandbox.base_image_fallback")
        self.assertProblem({"sandbox.base_image": None}, "sandbox.base_image_fallback")
        self.assertEqual(self.problems({"sandbox.base_image": None, "sandbox.base_image_fallback": None}), [])

    def test_deferred_env_modes_rejected(self):
        for mode in ("snapshot", "install_then_deny"):
            found = self.assertProblem({"sandbox.env_mode": mode}, "sandbox.env_mode")
            self.assertTrue(any("deferred (8.3)" in p for p in found))
        self.assertProblem({"sandbox.env_mode": "volume"}, "sandbox.env_mode")

    def test_key_env_names_rejected_in_proposer_env(self):
        m = helpers.make_manifest()
        m["proposer"]["env_allow"].append("MHFORGE_FORGE_API_KEY")
        self.assertProblem(None, "provider.api_key_env", manifest=m)
        m = helpers.make_manifest()
        m["proposer"]["env_set"]["MHFORGE_EXPORT_API_KEY"] = "x"
        self.assertProblem(None, "telemetry.api_key_env", manifest=m)
        self.assertProblem({"sandbox.auth_env": "WANDB_API_KEY"}, "sandbox.auth_env")
        self.assertProblem({"provider.api_key_env": "lower_case"}, "provider.api_key_env")

    def test_key_env_names_must_be_credential_shaped_and_not_exempt(self):
        for path in ("provider.api_key_env", "telemetry.api_key_env", "sandbox.auth_env"):
            for name in ("PATH", "HOME_DIR", "MHFORGE_FORGE", "KEYRING_BACKEND", "OPENROUTER_API_KEY",
                         "TIKTOKEN_RS_CACHE_DIR"):
                with self.subTest(path=path, name=name):
                    found = self.assertProblem({path: name}, path)
                    self.assertTrue(any("credential-shaped" in p for p in found), found)
        for name in ("MHFORGE_ALT_TOKEN", "MY_SECRET", "SVC_APIKEY", "DB_PASSWORD"):
            with self.subTest(ok=name):
                self.assertEqual(self.problems({"sandbox.auth_env": name}), [])

    def test_three_key_env_names_must_differ(self):
        self.assertProblem({"telemetry.api_key_env": "MHFORGE_FORGE_API_KEY"}, "provider.api_key_env")
        self.assertProblem({"sandbox.auth_env": "MHFORGE_EXPORT_API_KEY"}, "provider.api_key_env")

    def test_env_allow_rejects_secret_like_names(self):
        for name in ("GITHUB_TOKEN", "MY_SECRET", "DB_PASSWORD", "SERVICE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS"):
            m = helpers.make_manifest()
            m["proposer"]["env_allow"].append(name)
            self.assertProblem(None, "proposer.env_allow", manifest=m)
        self.assertEqual(self.problems(), [])

    def test_env_allow_rejects_proxy_ssh_loader_and_cloud_prefixes(self):
        names = ["HTTPS_PROXY", "http_proxy", "SSH_AUTH_SOCK", "NETRC", "BASH_ENV", "ENV", "NODE_OPTIONS", "KUBECONFIG",
                 "DYLD_INSERT_LIBRARIES", "LD_PRELOAD", "AWS_PROFILE", "GOOGLE_CLOUD_PROJECT", "AZURE_CONFIG_DIR",
                 "GH_HOST", "HF_HOME", "DOCKER_HOST", "CLAUDE_CONFIG_DIR", "OTEL_SERVICE_NAME", "LITELLM_LOG",
                 "TEXT_CLASSIFICATION_LLM_CACHE_DIR", "WANDB_DIR", "OPENAI_BASE_URL", "TRACEPARENT"]
        for name in names:
            for where in ("allow", "set"):
                m = helpers.make_manifest()
                if where == "allow":
                    m["proposer"]["env_allow"].append(name)
                else:
                    m["proposer"]["env_set"][name] = "x"
                self.assertProblem(None, f"proposer.env_{where}", manifest=m)

    def test_fixed_env_names_rejected_in_env_set(self):
        from mhforge import procenv
        for name in procenv.PROPOSER_FIXED_ENV:
            m = helpers.make_manifest()
            m["proposer"]["env_set"][name] = "1"
            self.assertProblem(None, f"proposer.env_set.{name}", manifest=m)
            m = helpers.make_manifest()
            m["proposer"]["env_allow"].append(name)
            self.assertProblem(None, "proposer.env_allow", manifest=m)
        self.assertProblem({"proposer.path_dirs": ["{nope}/bin"]}, "proposer.path_dirs[0]: unknown placeholder")

    def test_pasted_key_in_manifest_rejected(self):
        secret = "sk-ant-" + "Q" * 32
        for where in ("notes", "provider.user_agent", "proposer.env_set.UV_NO_SYNC"):
            found = self.assertProblem({where: secret}, where)
            self.assertFalse(any(secret in p for p in found))
        found = self.assertProblem({"provider.retry.backoff_429_s": [1, 2], "notes": "token=" + "a" * 40}, "notes")
        self.assertTrue(any("keyed_hex40" in p for p in found))

    def test_problems_never_echo_a_pasted_key(self):
        secret = "sk-ant-" + "Q" * 32
        for overrides in ({"proposer.env_set.FOO": "{" + secret + "}"}, {"proposer.path_dirs": ["/bin", "{" + secret + "}"]},
                          {"campaign": secret}, {"proposer.env_set": {secret: "x"}},
                          {"calibration.gates.G1.correct": {secret: [0, 1]}}):
            found = self.problems(overrides)
            self.assertTrue(any("secret pattern" in p for p in found), overrides)
            self.assertFalse(any(secret in p or "Q" * 16 in p for p in found), found)

    def test_state_dir_must_be_literal_and_manifest_in_temp_tree_validates(self):
        self.assertProblem({"state_dir": "state/other"}, "state_dir")
        self.assertProblem({"state_dir": "./state/mh-test"}, "state_dir")
        self.assertProblem({"campaign": "MH_bad"}, "campaign")
        with tempfile.TemporaryDirectory() as tmp:
            path = helpers.write_manifest(Path(tmp) / "deep" / "sc", helpers.make_manifest())
            m = M.read_manifest(path)
            self.assertEqual(M.validate(m, path), [])
            self.assertEqual(resolve_state(m, path).root, path.resolve().parent / "state" / "mh-test")

    def test_entity_project_pattern_and_printable_headers(self):
        self.assertProblem({"decisions.entity": "-bad"}, "decisions.entity")
        self.assertProblem({"decisions.project": "a b"}, "decisions.project")
        self.assertProblem({"decisions.project": "x" * 129}, "decisions.project")
        self.assertProblem({"provider.user_agent": "agent\r\nX-Evil: 1"}, "provider.user_agent")
        self.assertProblem({"provider.user_agent": "python-requests/2.0"}, "provider.user_agent")
        self.assertProblem({"provider.user_agent": ""}, "provider.user_agent")
        self.assertProblem({"provider.openai_project": "ent/pré"}, "provider.openai_project")
        self.assertProblem({"telemetry.agent_lens.service_name": "svc\t"}, "telemetry.agent_lens.service_name")
        self.assertEqual(self.problems({"decisions.entity": "Team_1.x-y", "provider.openai_project": "a/b"}), [])

    def test_loop_conversation_rule(self):
        self.assertProblem({"search.max_candidates_per_iteration": 6}, "telemetry.agent_lens.loop_iterations_per_conversation")
        self.assertEqual(self.problems({"telemetry.agent_lens.loop_iterations_per_conversation": 20}), [])   # 20*9=180
        self.assertProblem({"telemetry.agent_lens.loop_iterations_per_conversation": 21},
                           "telemetry.agent_lens.loop_iterations_per_conversation")
        self.assertProblem({"telemetry.agent_lens.max_spans_per_conversation": 200},
                           "telemetry.agent_lens.max_spans_per_conversation")
        self.assertProblem({"telemetry.attribute_prefix": "Meta"}, "telemetry.attribute_prefix")

    def test_relay_and_runner_rules(self):
        self.assertProblem({"relay.pong_timeout_s": 20}, "relay.pong_timeout_s")
        self.assertProblem({"relay.ping_interval_s": 0, "relay.pong_timeout_s": 5}, "relay.pong_timeout_s")
        self.assertProblem({"relay.max_job_stream_mb": 32}, "relay.max_job_stream_mb")
        self.assertProblem({"runner.sleep_gap_s": 0}, "runner.sleep_gap_s")
        self.assertProblem({"telemetry.agent_lens.byte_estimate_factor": "4.5"}, "telemetry.agent_lens.byte_estimate_factor")
        self.assertProblem({"provider.kind": "stub"}, "provider.base_url")
        self.assertEqual(self.problems({"provider.kind": "stub", "provider.base_url": "http://127.0.0.1:8123/v1"}), [])
        self.assertProblem({"provider.kind": "stub", "provider.base_url": "http://localhost:8123/v1"}, "provider.base_url")
        self.assertProblem({"provider.base_url": "https://api.inference.wandb.ai/v2"}, "provider.base_url")

    def test_content_rules_per_dataset(self):
        open_s2d = {"decisions.dataset_text_publication": "per_dataset",
                    "telemetry.content.Symptom2Disease.span_prompt": "full",
                    "telemetry.content.Symptom2Disease.span_reply": "truncate:200"}
        self.assertEqual(self.problems(open_s2d), [])
        self.assertProblem(dict(open_s2d, **{"telemetry.content.Symptom2Disease.span_reply": "truncate:199"}),
                           "telemetry.content.Symptom2Disease.span_reply")
        self.assertProblem(dict(open_s2d, **{"telemetry.content.LawBench.tool_result": "full"}),
                           "telemetry.content.LawBench.tool_result")
        self.assertProblem(dict(open_s2d, **{"telemetry.content.USPTO.span_prompt": "full"}),
                           "telemetry.content.USPTO.span_prompt")
        self.assertEqual(self.problems(dict(open_s2d, **{"telemetry.content.USPTO.span_prompt": "full",
                                                         "decisions.uspto_text_cleared": True})), [])
        self.assertProblem({"telemetry.content.Symptom2Disease.span_prompt": "full"},     # identifiers_only in the fixture
                           "telemetry.content.Symptom2Disease.span_prompt")
        self.assertProblem({"telemetry.content.Other": {f: "hash" for f in ("span_prompt", "span_reply", "span_reasoning",
                                                                             "tool_result")}}, "telemetry.content")
        self.assertProblem({"telemetry.content.USPTO.extra": "hash"}, "telemetry.content.USPTO.extra")
        self.assertProblem({"telemetry.verbatim_scan.run_chars": 19}, "telemetry.verbatim_scan.run_chars")
        self.assertProblem({"telemetry.verbatim_scan.exact_datasets": ["Nope"]}, "telemetry.verbatim_scan.exact_datasets")

    def test_budget_phases_sum_and_lens_split(self):
        draft = json.loads(DRAFT.read_text("utf-8"))
        phases = draft["budgets"]["solver_usd"]["phases"]
        self.assertEqual(sum(Decimal(v) for v in phases.values()), Decimal(draft["budgets"]["solver_usd"]["total"]))
        self.assertEqual(sum(draft["budgets"]["lens_ingest_split_mb"].values()), draft["budgets"]["lens_ingest_mb"])
        self.assertProblem({"budgets.solver_usd.phases.search": "29.00"}, "budgets.solver_usd.phases")
        self.assertProblem({"budgets.lens_ingest_split_mb.main": 149}, "budgets.lens_ingest_split_mb")
        self.assertProblem({"budgets.lens_ingest_split_mb.extra": 0}, "budgets.lens_ingest_split_mb")
        self.assertProblem({"budgets.solver_usd.phases.other": "1.00"}, "budgets.solver_usd.phases.other")
        self.assertProblem({"budgets.eval_tables_max": 0}, "budgets.eval_tables_max")
        self.assertProblem({"accounting.reconcile_tolerance": "1.5"}, "accounting.reconcile_tolerance")
        caps = M.phase_caps_micro(helpers.make_manifest())
        self.assertEqual(set(caps), set(phases) | {"total"})
        self.assertEqual(caps["search"], 28_000_000)
        self.assertEqual(caps["total"], 60_000_000)

    def test_calibration_gate_rules(self):
        self.assertProblem({"calibration.gates.G1.system": "other"}, "calibration.gates.G1.system")
        self.assertProblem({"calibration.gates.G1.macro_test_accuracy": [40, 30]}, "calibration.gates.G1.macro_test_accuracy")
        self.assertProblem({"calibration.gates.G1.macro_test_accuracy": [40, 101]}, "calibration.gates.G1.macro_test_accuracy")
        self.assertProblem({"calibration.gates.G1.correct.USPTO": [0, 101]}, "calibration.gates.G1.correct.USPTO")
        self.assertEqual(self.problems({"calibration.gates.G1.correct.USPTO": [0, 100]}), [])
        self.assertProblem({"calibration.gates.G1.correct.Nope": [0, 1]}, "calibration.gates.G1.correct.Nope")
        self.assertProblem({"calibration.gates.G1.hard": False}, "calibration.gate_search")
        self.assertEqual(self.problems({"calibration.gates.G1.hard": False, "calibration.gate_search": False}), [])
        self.assertProblem({"calibration.noise_repeats": 0}, "calibration.noise_repeats")
        self.assertProblem({"calibration.systems": ["no_memory"]}, "calibration.gates.G2.system")

    def test_parallel_test_jobs_must_be_one(self):
        self.assertProblem({"executor.parallel_dataset_jobs.test": 2}, "executor.parallel_dataset_jobs.test")
        self.assertProblem({"executor.parallel_dataset_jobs.validation": 4}, "executor.parallel_dataset_jobs.validation")
        self.assertProblem({"executor.parallel_dataset_jobs.other": 1}, "executor.parallel_dataset_jobs")
        self.assertEqual(self.problems({"executor.parallel_dataset_jobs.validation": 3}), [])

    def test_campaign_kind_and_import_from_rules(self):
        self.assertEqual(self.problems({"qualification.import_from": "mh-pre"}), [])          # measured may import
        self.assertProblem({"campaign_kind": "preflight", "qualification.import_from": "mh-pre"}, "qualification.import_from")
        self.assertProblem({"qualification.import_from": "mh-test"}, "qualification.import_from")
        self.assertProblem({"qualification.import_from": "MH pre"}, "qualification.import_from")
        self.assertProblem({"campaign_kind": "scratch"}, "campaign_kind")

    def test_proposer_rules(self):
        self.assertProblem({"proposer.max_budget_usd_per_session": "101.00"}, "proposer.max_budget_usd_per_session")
        self.assertProblem({"proposer.max_budget_usd_per_session": "0"}, "proposer.max_budget_usd_per_session")
        self.assertProblem({"proposer.scratch_root": "relative/dir"}, "proposer.scratch_root")
        self.assertProblem({"proposer.scratch_root": "/private/tmp/../tmp/x"}, "proposer.scratch_root")
        self.assertProblem({"proposer.cli_version": "2.1.317-beta"}, "proposer.cli_version")
        self.assertProblem({"proposer.restricted.settings": []}, "proposer.restricted.settings")
        self.assertProblem({"proposer.transient_retries": -1}, "proposer.transient_retries")
        with tempfile.TemporaryDirectory() as tmp:
            sc = Path(tmp).resolve() / "sc"
            path = helpers.write_manifest(sc, helpers.make_manifest(**{"proposer.scratch_root": str(sc / "scratch")}))
            self.assertTrue(any(p.startswith("proposer.scratch_root") for p in M.validate(M.read_manifest(path), path)))


class FreezeTest(unittest.TestCase):
    def test_freeze_then_change_is_refused(self):
        with helpers.tmp_state(manifest=True, create=False) as st:
            m = M.read_manifest(st.manifest_path)
            M.freeze(m, st.paths, ENGINE)
            self.assertEqual(M.load_frozen(st.manifest_path)[0], m)
            changed = copy.deepcopy(m)
            changed["upstream"]["expect"]["model"] = "openrouter/other"
            util.write_json(st.manifest_path, changed)
            with self.assertRaises(ManifestError) as ctx:
                M.load_frozen(st.manifest_path)
            self.assertIn("Manifest changed", str(ctx.exception))
            with self.assertRaises(ManifestError) as ctx:
                M.freeze(changed, st.paths, ENGINE)
            self.assertIn("Manifest changed", str(ctx.exception))

    def test_freeze_is_idempotent_and_needs_tree_hash(self):
        with helpers.tmp_state(manifest=True, create=False) as st:
            m = M.read_manifest(st.manifest_path)
            digest = M.freeze(m, st.paths, ENGINE)
            self.assertEqual(digest, M.manifest_sha256(m))
            self.assertEqual(st.paths.manifest_sha256.read_text().strip(), digest)
            self.assertEqual(util.read_json(st.paths.engine), ENGINE)
            before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in st.paths.root.iterdir()}
            reordered = json.loads(json.dumps(m, sort_keys=True))
            self.assertEqual(M.freeze(reordered, st.paths, {"engine_sha256": "0" * 64}), digest)
            after = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in st.paths.root.iterdir()}
            self.assertEqual(before, after)
        with helpers.tmp_state(manifest=helpers.make_manifest(**{"upstream.tree_sha256": None}), create=False) as st:
            m = M.read_manifest(st.manifest_path)
            self.assertEqual(M.validate(m, st.manifest_path), [])
            with self.assertRaises(ManifestError) as ctx:
                M.freeze(m, st.paths, ENGINE)
            self.assertIn("upstream.tree_sha256", ctx.exception.problems[0])
            self.assertFalse(st.paths.manifest.exists())
        draft = json.loads(DRAFT.read_text("utf-8"))
        with helpers.tmp_state(manifest=draft, create=False) as st:
            with self.assertRaises(ManifestError):
                M.freeze(M.read_manifest(st.manifest_path), st.paths, ENGINE)

    def test_freeze_over_unreadable_frozen_copy_is_manifest_error(self):
        with helpers.tmp_state(manifest=True, create=False) as st:
            st.paths.root.mkdir(parents=True)
            st.paths.manifest.write_text("{torn")
            with self.assertRaises(ManifestError) as ctx:
                M.freeze(M.read_manifest(st.manifest_path), st.paths, ENGINE)
            self.assertIn("Manifest changed", str(ctx.exception))
            self.assertEqual(st.paths.manifest.read_text(), "{torn")
            self.assertFalse(st.paths.manifest_sha256.exists())

    def test_freeze_refuses_an_invalid_manifest_and_load_needs_a_frozen_copy(self):
        with helpers.tmp_state(manifest=helpers.make_manifest(**{"gateway.max_messages": 1}), create=False) as st:
            with self.assertRaises(ManifestError):
                M.freeze(M.read_manifest(st.manifest_path), st.paths, ENGINE)
        with helpers.tmp_state(manifest=True, create=False) as st:
            with self.assertRaises(ManifestError) as ctx:
                M.load_frozen(st.manifest_path)
            self.assertIn("not frozen", ctx.exception.problems[0])

    def test_read_manifest_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            sc = Path(tmp) / "sc"
            path = helpers.write_manifest(sc, helpers.make_manifest())
            before = sorted(str(p) for p in Path(tmp).rglob("*"))
            m = M.read_manifest(path)
            M.validate(m, path)
            M.live_blockers(m, "search", ENV, resolve_state(m, path), live_flag=True)
            self.assertEqual(sorted(str(p) for p in Path(tmp).rglob("*")), before)
            self.assertFalse((sc / "state").exists())
            with self.assertRaises(ManifestError):
                M.read_manifest(sc / "absent.json")
            (sc / "bad.json").write_text("{not json")
            with self.assertRaises(ManifestError):
                M.read_manifest(sc / "bad.json")


class LiveGateTest(unittest.TestCase):
    def test_ready_state_passes_every_command(self):
        with ready() as r:
            for command, mode in CASES:
                self.assertEqual(r.blockers(command, mode), [], (command, mode))

    def test_draft_refuses_every_live_command_naming_the_field(self):
        draft = json.loads(DRAFT.read_text("utf-8"))
        with helpers.tmp_state(manifest=draft, create=False) as st:
            m = M.read_manifest(st.manifest_path)
            for command, mode in CASES:
                found = fields(M.live_blockers(m, command, ENV, st.paths, live_flag=True, mode=mode))
                self.assertTrue({"decisions.entity", "decisions.project"} <= found, (command, found))
                if command != "account-check":
                    self.assertTrue({"decisions.live_spend_approved", "decisions.key_provisioning",
                                     "upstream.tree_sha256", "frozen"} <= found, (command, found))
                if command == "search":
                    self.assertTrue({"decisions.proposer_permission_mode", "proposer.cli_path", "proposer.cli_sha256",
                                     "proposer.cli_version", "proposer.max_budget_usd_per_session",
                                     "budgets.proposer_list_usd", "proposer.session_estimate_usd"} <= found, found)
                if command in ("export", "publish"):
                    self.assertTrue({"decisions.evaltables_backend", "decisions.dataset_text_publication"} <= found)
                if command == "publish":
                    self.assertTrue({"decisions.publish_approved", "campaign_kind"} <= found)
                if command in ("preflight", "search", "evaluate"):
                    self.assertTrue({"sandbox.wheelhouse.tar_sha256", "upstream.worker_lock_sha256"} <= found, found)

    def test_each_user_decision_blocks_alone(self):
        unset = [("decisions.live_spend_approved", False, ["preflight", "search", "export", "publish"]),
                 ("decisions.key_provisioning", "unset", ["preflight", "search", "evaluate", "export"]),
                 ("decisions.entity", None, ["account-check", "preflight", "search", "publish"]),
                 ("decisions.project", None, ["account-check", "search", "export"]),
                 ("decisions.proposer_permission_mode", "unset", ["search"]),
                 ("decisions.evaltables_backend", "unset", ["export", "publish"]),
                 ("decisions.publish_approved", False, ["publish"]),
                 ("decisions.dataset_text_publication", "unset", ["export", "publish"])]
        for field, value, commands in unset:
            with ready({field: value}) as r:
                for command in commands:
                    mode = "baselines" if command == "evaluate" else None
                    self.assertEqual(fields(r.blockers(command, mode)), {field}, (field, command))
                if commands == ["search"]:
                    self.assertEqual(r.blockers("preflight"), [])

    def test_user_supplied_key_requires_restricted_search(self):
        with ready({'decisions.key_provisioning': 'user_supplied', 'decisions.proposer_permission_mode': 'restricted'}) as r:
            self.assertEqual(r.blockers('preflight'), [])
            self.assertEqual(r.blockers('search'), [])
        with ready({'decisions.key_provisioning': 'user_supplied', 'decisions.proposer_permission_mode': 'upstream'}) as r:
            self.assertEqual(fields(r.blockers('search')), {'decisions.proposer_permission_mode'})

    def test_require_live_checks_presence_and_never_echoes_a_value(self):
        with ready() as r:
            env = dict(ENV, MHFORGE_FORGE_API_KEY="")
            with self.assertRaises(LiveGateError) as ctx:
                M.require_live(r.manifest, "preflight", env, r.paths, live_flag=True)
            self.assertEqual(fields(ctx.exception.blockers), {"provider.api_key_env"})
            self.assertIn("MHFORGE_FORGE_API_KEY", str(ctx.exception.blockers))
            M.require_live(r.manifest, "preflight", ENV, r.paths, live_flag=True)
            env = {k: v for k, v in ENV.items() if k != "MHFORGE_SANDBOX_API_KEY"}
            env["CLAUDECODE"] = KEY_CANARY
            text = ""
            for command, mode in CASES:
                text += repr(r.blockers(command, mode, environ=env))
            text += repr(r.blockers("search", environ=dict(env, WANDB_BASE_URL=KEY_CANARY)))
            self.assertNotIn(KEY_CANARY, text)
            with self.assertRaises(LiveGateError) as ctx:
                M.require_live(r.manifest, "search", {}, r.paths, live_flag=False)
            self.assertIn("--live", fields(ctx.exception.blockers))

    def test_key_rows_skipped_for_prompt_and_fd_sources(self):
        with ready({"executor.kind": "sandbox", "telemetry.export_mode": "after_unit"}) as r:
            self.assertEqual(fields(r.blockers("preflight", environ={})),
                             {"provider.api_key_env", "sandbox.auth_env", "telemetry.api_key_env"})
            for source in ("prompt", "fd", "fd:3"):
                for command, mode in CASES:
                    self.assertEqual(r.blockers(command, mode, environ={}, key_source=source), [], (source, command))
            with self.assertRaises(ValueError):
                r.blockers("preflight", key_source="keychain")

    def test_halt_blocks_runner_commands_not_export(self):
        with ready() as r:
            set_halt(r.paths, "budget:search")
            for command, mode in CASES:
                found = fields(r.blockers(command, mode))
                if command in ("preflight", "search", "evaluate"):
                    self.assertEqual(found, {"halt"}, command)
                else:
                    self.assertEqual(found, set(), command)

    def test_halt_only_in_the_write_ahead_log_still_blocks(self):
        with ready() as r, tempfile.TemporaryDirectory(prefix="mhf-wal-") as tmp:
            stage = Path(tmp) / "ledger.sqlite3"
            writer = sqlite3.connect(stage)
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE halt (id INTEGER PRIMARY KEY CHECK (id = 1), reason TEXT NOT NULL,"
                           " detail TEXT NOT NULL DEFAULT '', call_id TEXT, set_utc TEXT NOT NULL)")
            writer.commit()
            writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")    # the table is in the main file
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("INSERT INTO halt (id, reason, set_utc) VALUES (1, 'budget:search', ?)",
                           (helpers.FakeClock().utc(),))
            writer.commit()                                      # the halt row is only in the log
            r.paths.root.mkdir(parents=True, exist_ok=True)
            ledger = Path(r.paths.ledger)
            ledger.write_bytes(stage.read_bytes())
            Path(str(ledger) + "-wal").write_bytes(Path(str(stage) + "-wal").read_bytes())   # a log without -shm
            writer.close()
            view, stale = util.open_sqlite_ro(ledger)
            view.close()
            self.assertTrue(stale)                               # the read-only view is possibly stale
            found = r.blockers("search")
            self.assertEqual(fields(found), {"halt"})
            self.assertIn("campaign halted (budget:search)", found[0][1])
            self.assertEqual(r.blockers("export"), [])

    def test_halt_on_a_stale_view_that_cannot_be_reread_blocks(self):
        from unittest import mock
        with ready() as r:
            set_halt(r.paths, "operator")
            conn = sqlite3.connect(r.paths.ledger)
            conn.execute("DELETE FROM halt")
            conn.commit()
            conn.close()
            real, real_connect = util.open_sqlite_ro, sqlite3.connect

            def stale(path):
                ro, _ = real(path)
                return ro, True

            def refuse(*args, **kwargs):                         # the read-only (uri) open works, a normal one fails
                if kwargs.get("uri"):
                    return real_connect(*args, **kwargs)
                raise sqlite3.OperationalError("unable to open database file")

            with mock.patch.object(util, "open_sqlite_ro", stale), mock.patch.object(M.sqlite3, "connect", refuse):
                found = r.blockers("search")
            self.assertEqual(fields(found), {"halt"})
            self.assertIn("the ledger could not be read", found[0][1])

    def test_repaired_torn_journal_tail_does_not_block_search(self):
        import hashlib
        with ready() as r:
            torn = b'{"schema":"mhforge.journal/1","seq":2,"op":"jo'
            marker = {"schema": "mhforge.journal/1", "seq": 2, "utc": helpers.FakeClock().utc(), "pid": 1,
                      "op": "journal", "key": "torn_tail", "status": "terminated", "attempt": 0,
                      "detail": {"bytes": len(torn), "sha256": hashlib.sha256(torn).hexdigest()}}
            later = {"schema": "mhforge.journal/1", "seq": 3, "utc": helpers.FakeClock().utc(), "pid": 1,
                     "op": "run", "key": "search", "status": "complete", "attempt": 0, "detail": {}}
            with open(r.paths.journal, "ab") as fh:
                fh.write(torn + b"\n" + json.dumps(marker).encode() + b"\n" + json.dumps(later).encode() + b"\n")
            self.assertEqual(r.blockers("search"), [])
            self.assertEqual(r.blockers("evaluate", "test"), [])
            # a bad middle line without its marker is still an unreadable journal
            data = r.paths.journal.read_bytes()
            r.paths.journal.write_bytes(data.replace(json.dumps(marker).encode() + b"\n", b""))
            self.assertEqual(fields(r.blockers("search")), {"journal"})

    def test_gate_problems_become_blockers(self):
        with ready() as r:
            found = r.blockers("search", gate_problems=["smoke_receipt", "protocol_stage"])
            self.assertEqual(found, [("offline_gate", "smoke_receipt"), ("offline_gate", "protocol_stage")])
            self.assertEqual(r.blockers("search", gate_problems=None), [])
            self.assertEqual(r.blockers("search", gate_problems=[]), [])

    def test_engine_and_manifest_hash_of_passed_json(self):
        with ready() as r:
            self.assertEqual(fields(r.blockers("search", engine_sha256="d" * 64)), {"qualification/passed.json"})
            for command, mode in (("search", None), ("evaluate", "baselines")):     # no engine given: cannot compare
                self.assertEqual(fields(r.blockers(command, mode, engine_sha256=None)), {"qualification/passed.json"})
            self.assertEqual(r.blockers("preflight", engine_sha256=None), [])
            r.paths.qualification_passed.unlink()
            self.assertEqual(fields(r.blockers("evaluate", "baselines")), {"qualification/passed.json"})
            self.assertEqual(r.blockers("preflight"), [])

    def test_search_blocked_by_finalized_json_or_test_entry(self):
        with ready() as r:
            finalized = r.paths.logs_dir("mh-test") / "finalized.json"
            util.write_json(finalized, {"state": "complete"})
            self.assertEqual(fields(r.blockers("search")), {"finalized.json"})
            finalized.unlink()
            util.append_jsonl(r.paths.journal, {"schema": "mhforge.journal/1", "seq": 2, "op": "test", "key": "mh-test",
                                                "status": "started", "detail": {"systems": [], "end_search": True}})
            self.assertEqual(fields(r.blockers("search")), {"journal"})
            self.assertEqual(r.blockers("evaluate", "test"), [])

    def test_evaluate_test_needs_complete_search_or_end_search(self):
        with ready() as r:
            r.paths.journal.unlink()
            util.append_jsonl(r.paths.journal, {"op": "run", "key": "search", "status": "paused"})
            self.assertEqual(fields(r.blockers("evaluate", "test")), {"journal"})
            self.assertEqual(r.blockers("evaluate", "test", end_search=True), [])
            util.append_jsonl(r.paths.journal, {"op": "run", "key": "search", "status": "complete"})
            self.assertEqual(r.blockers("evaluate", "test"), [])
            with self.assertRaises(ValueError):
                r.blockers("evaluate", None)

    def test_evaluate_calibration_and_noise_need_published_results(self):
        with ready() as r:
            d = r.paths.logs_dir("mh-test") / "LawBench" / "fewshot_all" / "gpt-oss-120b"
            (d / "memory.json").unlink()
            self.assertEqual(fields(r.blockers("evaluate", "calibration")), {"results.LawBench.fewshot_all"})
            self.assertEqual(r.blockers("evaluate", "noise"), [])
            util.write_json(d / "val.json", {"accuracy": "high"})
            self.assertEqual(fields(r.blockers("evaluate", "noise")), {"results.LawBench.fewshot_all"})

    def test_session_estimate_null_blocks_search(self):
        with ready({"proposer.session_estimate_usd": None}) as r:
            self.assertEqual(fields(r.blockers("search")), {"proposer.session_estimate_usd"})
            self.assertEqual(r.blockers("evaluate", "baselines"), [])

    def test_search_needs_cli_version_and_session_budget(self):
        with ready({"proposer.cli_version": None, "proposer.max_budget_usd_per_session": None}) as r:
            self.assertEqual(fields(r.blockers("search")), {"proposer.cli_version", "proposer.max_budget_usd_per_session"})
        with ready({"budgets.proposer_list_usd": None}) as r:
            self.assertEqual(fields(r.blockers("search")), {"budgets.proposer_list_usd"})
        with ready({"proposer.cli_sha256": "1" * 64}) as r:
            self.assertEqual(fields(r.blockers("search")), {"proposer.cli_sha256"})
        with ready({"proposer.cli_path": "/nonexistent/claude"}) as r:
            self.assertEqual(fields(r.blockers("search")), {"proposer.cli_path"})

    def test_search_refuses_inside_a_claude_session(self):
        with ready() as r:
            for name in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
                self.assertEqual(fields(r.blockers("search", environ=dict(ENV, **{name: "1"}))), {f"environ.{name}"})
                self.assertEqual(r.blockers("evaluate", "baselines", environ=dict(ENV, **{name: "1"})), [])

    def test_calibration_gate_and_protocol_stage_two_for_search(self):
        with ready() as r:
            util.write_json(r.paths.calibration, {"verdict": "fail"})
            self.assertEqual(fields(r.blockers("search")), {"calibration.gate_search"})
            r.paths.calibration.unlink()
            self.assertEqual(fields(r.blockers("search")), {"calibration.gate_search"})
            (r.sc / "protocol.md").write_text("x\n<!-- STAGE2-UNSIGNED -->\n")
            self.assertEqual(fields(r.blockers("search")), {"calibration.gate_search", "protocol.md"})
        with ready({"calibration.gate_search": False, "campaign_kind": "preflight"}) as r:
            r.paths.calibration.unlink()
            (r.sc / "protocol.md").write_text("<!-- STAGE2-UNSIGNED -->\n")
            self.assertEqual(r.blockers("search"), [])

    def test_imported_calibration_verdict_refused_when_calibration_fields_differ(self):
        with ready({"qualification.import_from": "mh-pre"}) as r:
            r.paths.calibration.unlink()
            source = r.paths.sibling("mh-pre")
            util.write_json(source.calibration, {"schema": "mhforge.calibration/1", "verdict": "pass"})
            passed = util.read_json(r.paths.qualification_passed)
            passed["imported_from"] = {"campaign": "mh-pre", "manifest_sha256": "a" * 64,
                                       "diff_paths": ["campaign", "decisions.project", "budgets.sandbox_usd"]}
            util.write_json(r.paths.qualification_passed, passed)
            self.assertEqual(r.blockers("search"), [])
            passed["imported_from"]["diff_paths"].append("calibration.gates.G1.correct.USPTO")
            util.write_json(r.paths.qualification_passed, passed)
            found = r.blockers("search")
            self.assertEqual(fields(found), {"calibration.gate_search"})
            util.write_json(r.paths.calibration, {"schema": "mhforge.calibration/1", "verdict": "pass"})
            self.assertEqual(r.blockers("search"), [])
            r.paths.calibration.unlink()
            passed["imported_from"] = None
            util.write_json(r.paths.qualification_passed, passed)
            self.assertEqual(fields(r.blockers("search")), {"calibration.gate_search"})

    def test_executor_and_sandbox_rows(self):
        with ready({"executor.allow_unisolated_live": False}) as r:
            self.assertEqual(fields(r.blockers("preflight")), {"executor.kind"})
            self.assertEqual(r.blockers("export"), [])
        with ready({"executor.kind": "sandbox", "upstream.worker_lock_sha256": None, "sandbox.image": None}) as r:
            self.assertEqual(fields(r.blockers("preflight")), {"upstream.worker_lock_sha256", "sandbox.image"})
        with ready({"executor.kind": "sandbox", "sandbox.env_mode": "wheelhouse"}) as r:
            self.assertEqual(fields(r.blockers("evaluate", "baselines")),
                             {"sandbox.wheelhouse.tar_sha256", "sandbox.wheelhouse.tar_bytes",
                              "sandbox.wheelhouse.sha256sums_sha256"})
        with ready({"executor.kind": "sandbox", "sandbox.auth_mode": "wandb_auth"}) as r:
            self.assertEqual(fields(r.blockers("preflight", environ=dict(ENV, WANDB_ENTITY="e", WANDB_PROJECT="p"))),
                             {"sandbox.auth_mode", "environ.WANDB_ENTITY", "environ.WANDB_PROJECT"})
            self.assertEqual(fields(r.blockers("preflight")), {"sandbox.auth_mode"})     # LD7
        with ready({"provider.kind": "stub", "provider.base_url": "http://127.0.0.1:9/v1"}) as r:
            self.assertEqual(fields(r.blockers("export")), {"provider.kind"})

    def test_wandb_auth_refused_by_every_live_command_but_still_validates(self):
        # LD7: cwsandbox 1.27.0 WandbAuth is not pinned to the given key, so only explicit headers go live
        for kind in ("sandbox", "local_relay"):
            with ready({"executor.kind": kind, "sandbox.auth_mode": "wandb_auth"}) as r:
                self.assertEqual(M.validate(r.manifest, r.path), [])            # a live gate, not a validation error
                for command, mode in CASES:
                    with self.subTest(kind=kind, command=command, mode=mode):
                        found = r.blockers(command, mode)
                        self.assertEqual(fields(found), {"sandbox.auth_mode"})
                        self.assertEqual(dict(found)["sandbox.auth_mode"], M.WANDB_AUTH_REFUSED)
                with self.assertRaises(LiveGateError) as cm:
                    M.require_live(r.manifest, "preflight", ENV, r.paths, live_flag=True,
                                   engine_sha256=ENGINE["engine_sha256"])
                self.assertIn("sandbox.auth_mode", str(cm.exception))
        message = M.WANDB_AUTH_REFUSED
        for why in ("wandb_auth", "session credential", "host", "headers"):
            self.assertIn(why, message)
        with ready({"executor.kind": "sandbox"}) as r:
            self.assertEqual(r.manifest["sandbox"]["auth_mode"], "headers")
            for command, mode in CASES:
                self.assertEqual(r.blockers(command, mode), [], command)
        for path in (helpers.FIXTURES / "manifest_valid.json", DRAFT):          # both still validate as before
            doc = json.loads(path.read_text("utf-8"))
            self.assertEqual(doc["sandbox"]["auth_mode"], "headers")
            with tempfile.TemporaryDirectory(prefix="mhf-val-") as tmp:
                written = helpers.write_manifest(Path(tmp) / "sc", doc)
                self.assertEqual(M.validate(M.read_manifest(written), written), [], path.name)

    def test_export_gate_needs_dataset_text_decision(self):
        with ready({"decisions.dataset_text_publication": "unset"}) as r:
            self.assertEqual(fields(r.blockers("export")), {"decisions.dataset_text_publication"})
            M.append_amendment(r.manifest, r.paths, {"decisions.dataset_text_publication": "identifiers_only"},
                               "signed off by the user", clock=helpers.FakeClock())
            self.assertEqual(r.blockers("export"), [])
            self.assertEqual(r.blockers("publish"), [])
            for name in M.EXPORT_MUST_BE_UNSET:
                self.assertEqual(fields(r.blockers("export", environ=dict(ENV, **{name: ""}))), {f"environ.{name}"})
        with ready({"budgets.lens_ingest_mb": 0, "budgets.lens_ingest_split_mb": {"main": 0, "preflight": 0,
                                                                                  "recovery": 0}}) as r:
            self.assertEqual(fields(r.blockers("export")), {"budgets.lens_ingest_mb"})

    def test_account_check_needs_no_frozen_copy(self):
        with ready(freeze=False, qualified=False) as r:
            self.assertEqual(r.blockers("account-check"), [])
            self.assertIn("frozen", fields(r.blockers("preflight")))
            self.assertEqual(fields(r.blockers("account-check", environ={})), {"provider.api_key_env"})
            self.assertEqual(fields(r.blockers("account-check", create_project=True)), set())
        with ready({"decisions.live_spend_approved": False, "executor.kind": "sandbox"}, freeze=False) as r:
            self.assertEqual(r.blockers("account-check"), [])
            self.assertEqual(fields(r.blockers("account-check", create_project=True)), {"decisions.live_spend_approved"})
            self.assertEqual(fields(r.blockers("account-check", environ={"MHFORGE_FORGE_API_KEY": "k"})),
                             {"sandbox.auth_env"})
            self.assertIn("--live", fields(r.blockers("account-check", live_flag=False)))
        with ready({"gateway.max_messages": 1}, freeze=False, qualified=False) as r:
            self.assertIn("manifest", fields(r.blockers("account-check")))

    def test_preflight_campaign_cannot_publish_gate(self):
        with ready({"campaign_kind": "preflight"}) as r:
            self.assertEqual(fields(r.blockers("publish")), {"campaign_kind"})
            self.assertEqual(r.blockers("export"), [])

    def test_frozen_copy_must_equal_the_file(self):
        with ready() as r:
            changed = copy.deepcopy(r.manifest)
            changed["search"]["iterations"] = 21
            self.assertIn("frozen", fields(r.blockers("export", manifest=changed)))


class AmendmentTest(unittest.TestCase):
    def test_explicit_table_fallback_preserves_frozen_scoring_and_requires_tables(self):
        with ready() as r:
            original = r.paths.manifest.read_bytes()
            row = M.append_amendment(r.manifest, r.paths,
                {"decisions.evaltables_backend": "wandb_table"}, "CES creation returned HTTP 500")
            effective = M.effective(r.manifest, M.read_amendments(r.paths, M.manifest_sha256(r.manifest)))
            self.assertEqual(effective['decisions']['evaltables_backend'], 'wandb_table')
            self.assertEqual(effective['upstream'], r.manifest['upstream'])
            self.assertEqual(r.paths.manifest.read_bytes(), original)
            for value in ('off', 'unset', None):
                with self.assertRaises(ManifestError):
                    M.append_amendment(r.manifest, r.paths, {'decisions.evaltables_backend': value}, 'no')
            row['set']['decisions.evaltables_backend'] = 'off'
            row['row_sha256'] = M._row_sha256(row)
            r.paths.amendments.write_text(json.dumps(row)+'\n')
            with self.assertRaises(ManifestError):
                M.read_amendments(r.paths, M.manifest_sha256(r.manifest))

    def test_amendment_chain_and_effective_manifest(self):
        clock = helpers.FakeClock()
        with ready() as r:
            digest = M.manifest_sha256(r.manifest)
            frozen_bytes = r.paths.manifest.read_bytes()
            self.assertEqual(M.read_amendments(r.paths, digest), [])
            row1 = M.append_amendment(r.manifest, r.paths, {"decisions.dataset_text_publication": "per_dataset",
                                                            "telemetry.content.Symptom2Disease.span_prompt": "full"},
                                      "signed off by the user, 2026-11-10", clock=clock)
            self.assertEqual((row1["seq"], row1["prev_sha256"], row1["utc"]), (1, digest, clock.utc()))
            row2 = M.append_amendment(r.manifest, r.paths, {"telemetry.content.Symptom2Disease.span_reply": "truncate:500"},
                                      "second", clock=clock)
            self.assertEqual(row2["prev_sha256"], row1["row_sha256"])
            rows = M.read_amendments(r.paths, digest)
            self.assertEqual(rows, [row1, row2])
            eff = M.effective(r.manifest, rows)
            self.assertEqual(eff["telemetry"]["content"]["Symptom2Disease"]["span_prompt"], "full")
            self.assertEqual(eff["telemetry"]["content"]["Symptom2Disease"]["span_reply"], "truncate:500")
            self.assertEqual(r.manifest["telemetry"]["content"]["Symptom2Disease"]["span_prompt"], "hash")
            self.assertEqual(M.validate(eff, r.path), [])
            # the frozen copy and its hash never change
            self.assertEqual(r.paths.manifest.read_bytes(), frozen_bytes)
            self.assertEqual(r.paths.manifest_sha256.read_text().strip(), digest)
            self.assertEqual(M.load_frozen(r.path)[0], r.manifest)
            # refused: a non-amendable path, LawBench opened, USPTO without clearance, no note
            for changes in ({"gateway.max_in_flight": 8}, {"decisions.entity": "x"},
                            {"telemetry.content.LawBench.span_prompt": "full"},
                            {"telemetry.content.USPTO.span_prompt": "full"}):
                with self.assertRaises(ManifestError, msg=str(changes)):
                    M.append_amendment(r.manifest, r.paths, changes, "note", clock=clock)
            with self.assertRaises(ManifestError):
                M.append_amendment(r.manifest, r.paths, {"decisions.uspto_text_cleared": True}, "  ", clock=clock)
            M.append_amendment(r.manifest, r.paths, {"decisions.uspto_text_cleared": True,
                                                     "telemetry.content.USPTO.span_prompt": "full"}, "legal ok",
                               clock=clock)
            self.assertEqual(len(M.read_amendments(r.paths, digest)), 3)
            # another campaign's frozen hash: the chain does not start there
            with self.assertRaises(ManifestError):
                M.read_amendments(r.paths, "0" * 64)
            # a changed row breaks the chain
            lines = r.paths.amendments.read_text().splitlines()
            tampered = json.loads(lines[0])
            tampered["note"] = "signed off by someone else"
            r.paths.amendments.write_text("\n".join([json.dumps(tampered)] + lines[1:]) + "\n")
            with self.assertRaises(ManifestError):
                M.read_amendments(r.paths, digest)
            # a removed row breaks the chain
            r.paths.amendments.write_text("\n".join(lines[1:]) + "\n")
            with self.assertRaises(ManifestError):
                M.read_amendments(r.paths, digest)
            # a forged row that re-hashes itself but names a non-amendable path is refused
            forged = {"schema": "mhforge.amendment/1", "seq": 1, "utc": clock.utc(), "prev_sha256": digest,
                      "set": {"gateway.max_in_flight": 8}, "note": "x"}
            forged["row_sha256"] = util.sha256_bytes(util.canonical_json(forged))
            r.paths.amendments.write_text(json.dumps(forged) + "\n")
            with self.assertRaises(ManifestError):
                M.read_amendments(r.paths, digest)

    def test_export_gate_validates_the_effective_manifest(self):
        with ready({"decisions.dataset_text_publication": "per_dataset"}) as r:
            digest = M.manifest_sha256(r.manifest)
            forged = {"schema": "mhforge.amendment/1", "seq": 1, "utc": helpers.FakeClock().utc(), "prev_sha256": digest,
                      "set": {"telemetry.content.LawBench.span_prompt": "full"}, "note": "hand-written row"}
            forged["row_sha256"] = util.sha256_bytes(util.canonical_json(forged))
            util.append_jsonl(r.paths.amendments, forged)
            self.assertEqual(len(M.read_amendments(r.paths, digest)), 1)         # the chain itself is intact
            for command in ("export", "publish"):
                found = r.blockers(command)
                self.assertEqual(fields(found), {"amendments"}, command)
                self.assertTrue(any("LawBench" in msg for _, msg in found))
            self.assertEqual(r.blockers("preflight"), [])                        # runner commands use the frozen copy

    def test_amendment_after_a_torn_last_line_keeps_the_chain(self):
        with ready({"decisions.dataset_text_publication": "per_dataset"}) as r:
            digest = M.manifest_sha256(r.manifest)
            row1 = M.append_amendment(r.manifest, r.paths, {"decisions.uspto_text_cleared": True}, "one")
            with open(r.paths.amendments, "ab") as fh:
                fh.write(b'{"schema": "mhforge.amendm')                       # a crash in the middle of a row
            row2 = M.append_amendment(r.manifest, r.paths, {"telemetry.content.Symptom2Disease.span_prompt": "full"},
                                      "two")
            self.assertEqual(M.read_amendments(r.paths, digest), [row1, row2])
            row3 = M.append_amendment(r.manifest, r.paths, {"telemetry.content.USPTO.span_prompt": "full"}, "three")
            line = (json.dumps(row3, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
            data = r.paths.amendments.read_bytes()
            r.paths.amendments.write_bytes(data[:-1])                         # a whole row without its newline
            row4 = M.append_amendment(r.manifest, r.paths, {"telemetry.content.USPTO.span_reply": "full"}, "four")
            rows = M.read_amendments(r.paths, digest)
            self.assertEqual([x["seq"] for x in rows], [1, 2, 3, 4])
            self.assertEqual(rows[-1], row4)
            self.assertIn(line, r.paths.amendments.read_bytes())

    def test_amendment_needs_frozen_copy(self):
        with ready(freeze=False) as r:
            with self.assertRaises(ManifestError):
                M.append_amendment(r.manifest, r.paths, {"decisions.uspto_text_cleared": True}, "note")
            self.assertFalse(r.paths.amendments.exists())

    def test_diff_paths_and_import_allow_list(self):
        a = helpers.make_manifest(**{"campaign": "mh-pre", "state_dir": "state/mh-pre", "campaign_kind": "preflight"})
        b = helpers.make_manifest(**{"qualification.import_from": "mh-pre", "decisions.project": "fresh-project",
                                     "budgets.sandbox_usd": "50.00", "executor.job_timeout_s": 14400,
                                     "calibration.gates.G1.correct.USPTO": [1, 20], "proposer.list_prices.new-model":
                                     {"input_per_million": "1", "output_per_million": "1", "cache_read_per_million": "1",
                                      "cache_write_5m_per_million": "1", "cache_write_1h_per_million": "1"},
                                     "executor.parallel_dataset_jobs.validation": 3,
                                     "provider.retry.backoff_429_s": [10, 40]})
        diffs = M.diff_paths(a, b)
        self.assertEqual(diffs, sorted(diffs))
        self.assertIn("calibration.gates.G1.correct.USPTO", diffs)          # a list is one leaf
        self.assertNotIn("calibration.gates.G1.correct.USPTO.0", diffs)
        self.assertIn("proposer.list_prices.new-model.input_per_million", diffs)   # present on one side only
        self.assertIn("provider.retry.backoff_429_s", diffs)
        self.assertEqual(M.diff_paths(a, copy.deepcopy(a)), [])
        not_allowed = [p for p in diffs if not M.path_allowed(p, M.IMPORT_DIFF_ALLOWED)]
        self.assertEqual(not_allowed, ["provider.retry.backoff_429_s"])
        for p in ("sandbox.image", "provider.base_url", "routing.route", "decisions.entity", "prices.input_per_million",
                  "executor.kind", "relay.mode", "decisions.proposer_permission_mode", "proposer.cli_sha256",
                  "proposer.cli_version", "proposer.restricted.settings", "gateway.max_request_bytes", "upstream.commit"):
            self.assertFalse(M.path_allowed(p, M.IMPORT_DIFF_ALLOWED), p)
        for p in ("notes", "budgets.solver_usd.phases.search", "runner.keep_awake", "forecast.test_systems",
                  "telemetry.content.USPTO.span_prompt", "gateway.max_in_flight"):
            self.assertTrue(M.path_allowed(p, M.IMPORT_DIFF_ALLOWED), p)
        self.assertFalse(M.path_allowed("budgets", M.IMPORT_DIFF_ALLOWED))
        self.assertFalse(M.path_allowed("notesx", M.IMPORT_DIFF_ALLOWED))
        self.assertTrue(M.path_allowed("telemetry.content.X.y", M.AMENDABLE))
        self.assertFalse(M.path_allowed("decisions.dataset_text_publication.x", M.AMENDABLE))


class AccessorTest(unittest.TestCase):
    def test_run_config_selects_allowed_paths(self):
        m = helpers.make_manifest()
        allow = ["campaign", "upstream.expect", "sandbox.wheelhouse.requirements_sha256", "missing.path", "telemetry.content"]
        cfg = M.run_config(m, allow)
        self.assertEqual(set(cfg), {"campaign", "upstream", "sandbox", "telemetry"})
        self.assertEqual(set(cfg["upstream"]), {"expect"})
        self.assertEqual(cfg["sandbox"], {"wheelhouse": {"requirements_sha256": m["sandbox"]["wheelhouse"]["requirements_sha256"]}})
        self.assertEqual(cfg["upstream"]["expect"], m["upstream"]["expect"])
        self.assertNotIn("notes", cfg)
        self.assertNotIn("example_dir", cfg["upstream"])
        cfg["upstream"]["expect"]["datasets"].append("X")
        self.assertNotIn("X", m["upstream"]["expect"]["datasets"])
        self.assertEqual(M.run_config(m, []), {})

    def test_prices_reservation_and_openai_project(self):
        m = helpers.make_manifest()
        self.assertEqual(M.prices(m), {"input": Decimal("0.03"), "cached": Decimal("0.03"), "output": Decimal("0.17")})
        self.assertEqual(M.max_reservation_micro(m), 6718)
        self.assertEqual(M.openai_project(m), "example-entity/mh-forge-test")
        self.assertEqual(M.openai_project(helpers.make_manifest(**{"provider.openai_project": "a/b"})), "a/b")

    def test_manifest_sha256_is_order_independent(self):
        m = helpers.make_manifest()
        self.assertEqual(M.manifest_sha256(m), M.manifest_sha256(json.loads(json.dumps(m, sort_keys=True))))
        self.assertEqual(M.manifest_sha256(m), util.sha256_bytes(util.canonical_json(m)))


if __name__ == "__main__":
    unittest.main()
