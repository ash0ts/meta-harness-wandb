# SPDX-License-Identifier: MIT
"""The two SDK adapters of spec 3.25 against the fake SDK modules of tests/fake_sinks.py (FakeSdk).

No SDK is installed here and no service is contacted: FakeSdk installs `wandb`, `wandb_gql`,
`coreweave.forge.agentlens.tracing` and `opentelemetry.trace` as a meta-path finder, records the environment at the
moment each is imported and logs every call with its keyword arguments. The read-back goes to an opener stand-in.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from mhforge import util
from mhforge.errors import AdapterUnavailable, ExportError
from mhforge.telemetry import adapter_agentlens, adapter_wandb, readback
from mhforge.telemetry.adapter_agentlens import CoreweaveSink
from mhforge.telemetry.adapter_wandb import WandbSdkSink
from tests import helpers
from tests.fake_sinks import FakeLensSink, FakeSdk
from tests.test_exporter import JOB, Campaign

KEY = "mhf-wandb-export-key-0123456789abcdefghij"
SDK_ENV = ("NETRC", "WANDB_DIR", "WANDB_CACHE_DIR", "WANDB_DATA_DIR", "WANDB_CONSOLE", "WANDB_DISABLE_GIT",
           "WANDB_DISABLE_WEAVE", "OTEL_RESOURCE_ATTRIBUTES", "OTEL_BSP_MAX_EXPORT_BATCH_SIZE",
           "OTEL_EXPORTER_OTLP_TRACES_COMPRESSION", "WANDB_API_KEY") + adapter_wandb.REFUSED_ENV


class AdapterCase(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ)                       # every variable an adapter sets is undone
        self._env.start()
        for name in [n for n in os.environ if n in SDK_ENV or n.startswith(("WANDB_", "CES_", "OTEL_"))]:
            os.environ.pop(name, None)                                # LD9: the adapters refuse or drop these
        self._ctx = helpers.tmp_state(manifest=True)
        self.st = self._ctx.__enter__()
        self.c = Campaign(self.st)
        self.m = self.st.manifest
        self.sdk = FakeSdk()
        self.environ = {"MHFORGE_EXPORT_API_KEY": KEY}

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)
        self._env.stop()

    def wandb_sink(self, environ=None) -> WandbSdkSink:
        return WandbSdkSink(self.m, environ=self.environ if environ is None else environ, paths=self.st.paths)

    def lens_sink(self, environ=None, **kw) -> CoreweaveSink:
        return CoreweaveSink(self.m, environ=self.environ if environ is None else environ,
                             opener=self.sdk.opener(**kw))

    def full_export(self, *, lens=None) -> dict:
        self.c.everything()
        with self.sdk.installed():
            ex = self.c.exporter(lens=lens or self.lens_sink(), wandb=self.wandb_sink())
            return ex.export_all()

    def plan(self, unit: str) -> dict:
        if unit == JOB and not self.st.paths.job_dir("val.no_memory.USPTO").exists():
            self.c.job()
        ex = self.c.exporter(dry_run=True, ledger=False)
        ex.export_unit(self.c.unit(ex, unit))
        return util.read_json(self.st.paths.plan_dir(unit) / "spans.json")


# ---------------------------------------------------------------- W&B

class Wandb(AdapterCase):
    def test_wandb_login_is_never_called(self):
        out = self.full_export()
        self.assertEqual({s for st in out["statuses"].values() for s in st.values()}, {"verified"})
        self.assertEqual(self.sdk.calls("wandb.login"), [])
        self.assertTrue(self.sdk.calls("wandb.init"))
        self.assertNotIn("login(", Path(adapter_wandb.__file__).read_text())

    def test_netrc_points_into_state(self):
        self.full_export()
        at_import = self.sdk.imports["wandb"]
        self.assertEqual(at_import["NETRC"], str(self.st.paths.export_netrc))
        self.assertEqual(self.st.paths.export_netrc, self.st.paths.root / "export" / "empty.netrc")
        self.assertEqual(self.st.paths.export_netrc.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.st.paths.export_netrc.read_bytes(), b"")
        self.assertEqual(set(self.sdk.netrc_touched), {str(self.st.paths.export_netrc)})

    def test_wandb_dirs_point_into_state_and_console_off(self):
        self.full_export()
        env = self.sdk.imports["wandb"]
        local = self.st.paths.export_wandb_local
        self.assertEqual(env["WANDB_DIR"], str(local))
        for name in ("WANDB_DIR", "WANDB_CACHE_DIR", "WANDB_DATA_DIR"):
            self.assertTrue(Path(env[name]).is_dir())
            self.assertIn(self.st.paths.root, Path(env[name]).parents)
        self.assertEqual((env["WANDB_CONSOLE"], env["WANDB_DISABLE_GIT"]), ("off", "true"))
        self.assertFalse((self.st.sc / "wandb").exists())

    def test_api_key_passed_through_settings_and_api(self):
        self.full_export()
        self.assertTrue(self.sdk.calls("wandb.Settings"))
        self.assertEqual({c[1]["api_key"] for c in self.sdk.calls("wandb.Settings")}, {KEY})
        self.assertTrue(self.sdk.calls("wandb.Api"))
        self.assertEqual({c[1]["api_key"] for c in self.sdk.calls("wandb.Api")}, {KEY})
        for call in self.sdk.calls("wandb.init"):
            self.assertEqual(call[1]["settings"].kwargs["api_key"], KEY)

    def test_define_metric_and_no_step(self):
        self.full_export()
        log = self.sdk.log
        init = next(i for i, e in enumerate(log) if e[0] == "wandb.init" and e[1]["job_type"] == "search")
        finish = next(i for i, e in enumerate(log) if i > init and e[0] == "run.finish")
        run_calls = log[init:finish]
        metrics = [(e[1], e[2]) for e in run_calls if e[0] == "run.define_metric"]
        self.assertEqual(metrics, [("iteration", None), ("search/*", "iteration")])
        logs = [i for i, e in enumerate(run_calls) if e[0] == "run.log"]
        defines = [i for i, e in enumerate(run_calls) if e[0] == "run.define_metric"]
        self.assertTrue(logs and max(defines) < min(logs))           # before any row
        for e in self.sdk.calls("run.log"):
            self.assertEqual((e[2], e[3]), (None, None))              # no step and no commit
        for e in self.sdk.calls("run.define_metric"):
            self.assertEqual(e[3], {})
        history = self.sdk.runs["example-entity/mh-forge-test/mh-test-search"]["history"]
        self.assertEqual(history[0]["iteration"], 0)

    def test_wandb_refuses_when_base_url_env_set(self):
        for name in adapter_wandb.REFUSED_ENV:
            with self.subTest(name=name):
                with self.assertRaises(ExportError):
                    self.wandb_sink(environ={**self.environ, name: "https://elsewhere.example"})
                sink = self.wandb_sink()
                os.environ[name] = "https://elsewhere.example"
                try:
                    with self.sdk.installed(), self.assertRaises(ExportError):
                        sink.project_access("example-entity", "mh-forge-test")
                finally:
                    del os.environ[name]
        self.assertNotIn("wandb", self.sdk.imports)
        self.c.everything()
        with self.sdk.installed():
            ex = self.c.exporter(lens=FakeLensSink(), wandb=self.wandb_sink())
            ex.export_all()
            r = self.c.receipt("propose:x", "wandb")
        self.assertEqual(r["remote"]["base_url"], adapter_wandb.DEFAULT_BASE_URL)

    def test_wandb_evaltable_arguments_against_fake_module(self):
        self.full_export()
        table = util.read_json(self.st.paths.plan_dir("eval:val.no_memory") / "tables.json")["tables"][0]
        calls = self.sdk.calls("wandb.EvalTable")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], {"columns": table["columns"], "data": table["rows"],
                                       "input_columns": table["input_columns"],
                                       "output_columns": table["output_columns"],
                                       "score_columns": table["score_columns"], "backend": "ces",
                                       "unsupported_media_mode": "raise"})
        logged = [e for e in self.sdk.calls("run.log") if e[1] == ["val/USPTO"]]
        self.assertEqual(len(logged), 1)                              # once under the plan's key
        self.assertEqual(self.sdk.calls("wandb.Table"), [])
        summary = self.sdk.runs["example-entity/mh-forge-test/mh-test-eval-val-no_memory"]["summary"]
        self.assertEqual(summary["val/USPTO"], {"_type": "eval-table-ces"})

    def test_wandb_table_backend_when_ces_probe_fails(self):
        self.sdk = FakeSdk(ces_features=(False, False, False))
        self.full_export()
        self.assertEqual(self.sdk.calls("wandb.EvalTable"), [])
        self.assertEqual(len(self.sdk.calls("wandb.Table")), 1)
        r = self.c.receipt("eval:val.no_memory", "wandb")
        self.assertEqual((r["status"], r["remote"]["backend"]), ("verified", "wandb_table"))
        probes = self.sdk.calls("api.feature_enabled")
        self.assertEqual(len(probes), 3)
        self.assertEqual({e[1] for e in probes}, {"EVAL_TABLES_CES"})

    def test_feature_ces_retries_three_times(self):
        self.sdk = FakeSdk(ces_features=(False, RuntimeError("timeout"), True))
        with self.sdk.installed():
            self.assertTrue(self.wandb_sink().feature_ces())
        self.assertEqual(len(self.sdk.calls("api.feature_enabled")), 3)
        self.sdk = FakeSdk(ces_features=(False, False, False, True))
        with self.sdk.installed():
            self.assertFalse(self.wandb_sink().feature_ces())
        self.assertEqual(len(self.sdk.calls("api.feature_enabled")), 3)

    def test_project_access_reads_only(self):
        with self.sdk.installed():
            sink = self.wandb_sink()
            self.assertEqual(sink.project_access("example-entity", "mh-forge-test"),
                             {"access": "PRIVATE", "read_only": False, "can_write": True})
            self.assertIsNone(sink.project_access("example-entity", "no-such-project"))
        texts = [e[1] for e in self.sdk.calls("api.execute_graphql")]
        self.assertTrue(all(t.lstrip().startswith("query") and "mutation" not in t for t in texts))
        self.assertEqual(self.sdk.calls("wandb.init"), [])

    def test_init_settings_switch_off_metadata_and_set_dir(self):
        self.full_export()
        self.assertEqual({json.dumps(c[1], sort_keys=True) for c in self.sdk.calls("wandb.Settings")},
                         {json.dumps({"api_key": KEY, "base_url": adapter_wandb.DEFAULT_BASE_URL, "disable_git": True,
                                      "save_code": False, "console": "off", "x_disable_meta": True, "x_disable_stats": True,
                                      "x_disable_machine_info": True, "x_save_requirements": False}, sort_keys=True)})
        for call in self.sdk.calls("wandb.init"):
            kw = call[1]
            self.assertEqual(kw["dir"], str(self.st.paths.export_wandb_local))
            self.assertIn(self.st.paths.root, Path(kw["dir"]).parents)
            plan = next(util.read_json(p) for p in self.st.paths.export_root.glob("plans/*/runs.json")
                        if util.read_json(p)["run"]["id"] == kw["id"])["run"]
            self.assertEqual(kw["config"], plan["config"])            # the allow-listed subset, as planned
            self.assertEqual(kw["resume"], "allow" if plan["resume"] else None)
            self.assertNotIn(str(self.st.sc), json.dumps(kw["config"]))

    def test_confirm_reports_missing_markers_and_artifacts(self):
        self.full_export()
        with self.sdk.installed():
            sink = self.wandb_sink()
            path = "example-entity/mh-forge-test/mh-test-eval-val-no_memory"
            self.assertEqual(sink.confirm(path, ["val/USPTO"], ["candidate-no_memory"]), [])
            self.assertEqual(sink.confirm(path, ["val/LawBench"], ["candidate-nothing"]),
                             ["table_marker_missing:val/LawBench", "artifact_missing:candidate-nothing"])
            self.assertEqual(sink.confirm("example-entity/mh-forge-test/missing", [], []),
                             ["run_not_found:RuntimeError"])

    def test_fake_home_netrc_byte_identical_after_export(self):
        home = self.st.sc / "fakehome"
        home.mkdir()
        body = b"machine api.wandb.ai\n  login user\n  password the-users-own-key-0123456789\n"
        (home / ".netrc").write_bytes(body)
        os.environ["HOME"] = str(home)
        out = self.full_export()
        self.assertEqual({s for st in out["statuses"].values() for s in st.values()}, {"verified"})
        self.assertEqual((home / ".netrc").read_bytes(), body)
        self.assertNotIn(str(home / ".netrc"), self.sdk.netrc_touched)
        self.assertEqual(sorted(p.name for p in home.iterdir()), [".netrc"])

    def test_refuses_when_weave_importable(self):
        self.sdk = FakeSdk(weave_importable=True)
        with self.sdk.installed():
            sink = self.wandb_sink()
            for call in (lambda: sink.project_access("example-entity", "mh-forge-test"), sink.feature_ces,
                         lambda: sink.log_run({"run": {}}, [], self.st.paths.root),
                         lambda: sink.confirm("a/b/c", [], [])):
                with self.assertRaises(AdapterUnavailable):
                    call()
        self.assertNotIn("wandb", self.sdk.imports)
        self.assertEqual(self.sdk.log, [])

    def test_weave_disabled_in_environment(self):
        self.full_export()
        self.assertEqual(self.sdk.imports["wandb"]["WANDB_DISABLE_WEAVE"], "true")

    def test_artifact_files_only_from_plan_and_under_state(self):
        self.full_export()
        added = [e[1] for e in self.sdk.calls("artifact.add_file")]
        self.assertEqual(sorted(a["name"] for a in added), ["no_memory.py", "x.py"])
        for a in added:
            self.assertIn(self.st.paths.root.resolve(), Path(a["local_path"]).resolve().parents)
        with self.sdk.installed():
            sink = self.wandb_sink()
            run = {"entity": "example-entity", "project": "mh-forge-test", "id": "mh-test-propose-q", "group": "g",
                   "job_type": "propose", "name": "q", "tags": [], "config": {}, "resume": False, "summary": {},
                   "artifacts": [{"op": "log", "name": "candidate-q", "type": "harness-candidate", "aliases": [],
                                  "files": [{"source": "../../outside.py", "name": "q.py"}], "metadata": {}}]}
            (self.st.sc / "outside.py").write_text("x = 1\n")
            with self.assertRaises(ExportError):
                sink.log_run({"run": run}, [], self.st.paths.root)
        # refused before any SDK call: no run was created, so the exporter's 'failed' (nothing sent) is true
        self.assertNotIn("mh-test-propose-q", [c[1]["id"] for c in self.sdk.calls("wandb.init")])

    def test_symlinked_artifact_dir_refused_before_init(self):
        # regression: an agents/ directory that leaves STATE was refused only after wandb.init had created the run,
        # and the receipt then said 'failed' (nothing was sent)
        self.c.job()
        outside = self.st.sc / "outside-agents"
        outside.mkdir()
        (outside / "no_memory.py").write_text("x = 1\n")
        self.st.paths.ws.mkdir(parents=True, exist_ok=True)
        os.symlink(outside, self.st.paths.ws / "agents")
        self.c.benchmark()
        with self.sdk.installed():
            ex = self.c.exporter(lens=FakeLensSink(), wandb=self.wandb_sink())
            ex.export_all()
        r = self.c.receipt("eval:val.no_memory", "wandb")
        self.assertEqual(r["status"], "failed")
        self.assertIn("not a regular file under the state directory", r["error"])
        self.assertEqual(self.sdk.calls("wandb.init"), [])
        self.assertEqual(self.sdk.calls("artifact.add_file"), [])

    def test_base_url_reported_is_the_runs_own(self):
        # regression: the receipt named api.wandb.ai although wandb 0.31.0 defaults to api.forge.coreweave.com
        self.assertEqual(adapter_wandb.DEFAULT_BASE_URL, "https://api.forge.coreweave.com")
        run = type("Run", (), {"url": "https://x/run", "settings": type("S", (), {"base_url": "https://moved.example"})})
        self.assertEqual(adapter_wandb._sdk_urls(run), ("https://x/run", "https://moved.example"))
        self.assertEqual(adapter_wandb._sdk_urls(object()), (None, adapter_wandb.DEFAULT_BASE_URL))

    def test_graphql_and_ces_probe_go_through_service_api(self):
        # regression: wandb 0.31.0 ships no wandb_gql and its Api has no .client, so every probe raised
        # AdapterUnavailable; the CES check is now the one EvalTable.bind_to_run makes (ServiceApi.feature_enabled)
        text = Path(adapter_wandb.__file__).read_text()
        self.assertNotIn("from wandb_gql", text)
        self.assertNotIn("api.client", text)
        with self.sdk.installed():
            sink = self.wandb_sink()
            self.assertTrue(sink.feature_ces())
            self.assertIsNotNone(sink.project_access("example-entity", "mh-forge-test"))
        self.assertEqual([e[1] for e in self.sdk.calls("api.feature_enabled")], ["EVAL_TABLES_CES"])
        (q,) = self.sdk.calls("api.execute_graphql")
        self.assertEqual((q[1], q[2]), (adapter_wandb.PROJECT_QUERY, {"name": "mh-forge-test", "entity": "example-entity"}))
        self.assertNotIn("wandb_gql", self.sdk.imports)

    def test_confirm_reads_summary_metrics(self):
        # regression: Run.summary of the public API nests SummarySubDict objects, which are not Mappings, so every
        # table was reported as table_marker_missing; summary_metrics is a plain dict
        self.full_export()
        with self.sdk.installed():
            sink = self.wandb_sink()
            path = "example-entity/mh-forge-test/mh-test-eval-val-no_memory"
            self.assertEqual(sink.confirm(path, ["val/USPTO"], []), [])
            run = sys.modules["wandb"].Api().run(path)
            self.assertNotIsInstance(run.summary["val/USPTO"], dict)  # what the old read saw

    def test_base_url_pinned_in_settings_and_api(self):
        # LD9 belt and braces: a settings file or variable can no longer move the host the key is sent to
        self.full_export()
        self.assertEqual({c[1]["base_url"] for c in self.sdk.calls("wandb.Settings")}, {adapter_wandb.DEFAULT_BASE_URL})
        self.assertEqual([c[1]["overrides"] for c in self.sdk.calls("wandb.Api")],
                         [{"base_url": adapter_wandb.DEFAULT_BASE_URL}] * len(self.sdk.calls("wandb.Api")))

    def test_refuses_foreign_wandb_and_ces_variables(self):
        # LD9: any WANDB_<FIELD> / WANDB__<X> sets a Settings field; CES_CUSTOM_HEADERS overrides the CES auth header;
        # an inherited WANDB_SERVICE attaches to another wandb-core
        for name in ("WANDB_API_KEY", "WANDB__EXTRA_HTTP_HEADERS", "WANDB_X_EXTRA_HTTP_HEADERS", "WANDB_HTTPS_PROXY",
                     "WANDB_INSECURE_DISABLE_SSL", "WANDB_IDENTITY_TOKEN_FILE", "WANDB_SETTINGS_SYSTEM",
                     "WANDB_SERVICE", "CES_CUSTOM_HEADERS", "CES_LOG"):
            with self.subTest(name=name):
                os.environ[name] = "x"
                try:
                    with self.sdk.installed():
                        with self.assertRaises(ExportError) as ctx:
                            self.wandb_sink().project_access("example-entity", "mh-forge-test")
                finally:
                    del os.environ[name]
                self.assertIn(name, str(ctx.exception))
                self.assertNotIn("'x'", str(ctx.exception))
        self.assertNotIn("wandb", self.sdk.imports)

    def test_config_dir_is_empty_state_dir(self):
        self.full_export()
        cfg = self.sdk.imports["wandb"]["WANDB_CONFIG_DIR"]
        self.assertEqual(Path(cfg), adapter_wandb.config_dir(self.st.paths))
        self.assertIn(self.st.paths.root, Path(cfg).parents)
        self.assertEqual(Path(cfg).stat().st_mode & 0o777, 0o700)
        self.assertEqual(list(Path(cfg).iterdir()), [])
        outside = self.st.sc / "realconfig"
        outside.mkdir()
        for value, setup in ((str(outside), None), (cfg, lambda: (Path(cfg) / "settings").write_text("[default]\n"))):
            with self.subTest(value=value):
                if setup:
                    setup()
                os.environ["WANDB_CONFIG_DIR"] = value
                with self.sdk.installed(), self.assertRaises(ExportError):
                    self.wandb_sink().feature_ces()

    def test_workspace_settings_file_in_cwd_refused(self):
        for sub in ("wandb", ".wandb"):
            with self.subTest(sub=sub):
                work = self.st.sc / f"work-{sub}"
                (work / sub).mkdir(parents=True)
                (work / sub / "settings").write_text("[default]\nbase_url = https://elsewhere.example\n")
                here = os.getcwd()
                os.chdir(work)
                try:
                    with self.sdk.installed(), self.assertRaises(ExportError):
                        self.wandb_sink().feature_ces()
                finally:
                    os.chdir(here)
        self.assertNotIn("wandb", self.sdk.imports)


# ---------------------------------------------------------------- Agent Lens

class AgentLens(AdapterCase):
    def test_tracing_init_has_explicit_base_url(self):
        plan = self.plan(JOB)
        with self.sdk.installed():
            self.lens_sink().emit(plan, lambda c, t: None)
        (call,) = self.sdk.calls("tracing.init")
        self.assertEqual(call[1], {"project": "example-entity/mh-forge-test", "api_key": KEY,
                                   "base_url": "https://trace.wandb.ai", "service_name": "mhforge-showcase",
                                   "autopatch_integrations": False})
        self.assertEqual(readback.READBACK_URL.split("/")[2], call[1]["base_url"].split("/")[2])

    def test_agentlens_call_order_against_fake_tracing(self):
        plans = [self.plan(JOB), self.plan(self.c.session())]
        traces = []

        def on_trace(conversation_id, trace_id):
            self.sdk.log.append(("on_trace", conversation_id, trace_id))
            traces.append(trace_id)

        for plan in plans:
            self.sdk.log.clear()
            with self.sdk.installed():
                self.lens_sink().emit(plan, on_trace)
            first = [e[0] for e in self.sdk.log].index("tracing.init")
            self.assertEqual({e[0] for e in self.sdk.log[:first]}, {"new"})      # the plan is built once, unstarted
            log = self.sdk.log[first:]
            kinds = [(e[0], e[1] if len(e) > 1 and isinstance(e[1], str) else None) for e in log]
            self.assertEqual(kinds[0], ("tracing.init", None))
            self.assertEqual(kinds[-1], ("shutdown", None))
            self.assertEqual(kinds[-2], ("force_flush", None))
            self.assertEqual(kinds[1:3], [("new", "Conversation"), ("enter", "Conversation")])
            n_turns = sum(len(c["turns"]) for c in plan["conversations"])
            turn_enters = [i for i, k in enumerate(kinds) if k == ("enter", "Turn")]
            on_traces = [i for i, k in enumerate(kinds) if k[0] == "on_trace"]
            self.assertEqual((len(turn_enters), len(on_traces)), (n_turns, n_turns))
            for t_i, o_i in zip(turn_enters, on_traces):
                first_exit = next(i for i in range(t_i, len(kinds)) if kinds[i][0] == "exit")
                self.assertLess(o_i, first_exit)                      # before any span of the trace ends
                self.assertEqual(kinds[o_i + 1], ("set_attributes", "Turn"))
            ended = {"LLM": 0, "Tool": 0, "SubAgent": 0}
            for k in kinds:
                if k[0] == "exit" and k[1] in ended:
                    ended[k[1]] += 1
            planned = {"llm": 0, "tool": 0, "subagent": 0}

            def walk(spans):
                for s in spans:
                    planned[s["type"]] += 1
                    walk(s.get("spans") or ())
            for conv in plan["conversations"]:
                for turn in conv["turns"]:
                    walk(turn["spans"])
            self.assertEqual((ended["LLM"], ended["Tool"], ended["SubAgent"]),
                             (planned["llm"], planned["tool"], planned["subagent"]))
            self.assertEqual(sum(ended.values()) + n_turns, plan["expected_spans"])
            errors = [e for e in log if e[0] == "record_error"]
            want = [s["error"] for conv in plan["conversations"] for t in conv["turns"]
                    for s in t["spans"] if s.get("error")]
            self.assertEqual(sorted((e[2], e[3]) for e in errors if e[1] != "Turn"),
                             sorted((w["type"], w["message"]) for w in want))
            for e in log:
                if e[0] == "set_attributes":
                    self.assertTrue(all(k.startswith("metaharness.") for k in e[2]))
        job_plan = plans[0]
        llm = [s for t in job_plan["conversations"][0]["turns"] for s in t["spans"]]
        self.assertIn("subagent", {s["type"] for t in plans[1]["conversations"][0]["turns"] for s in t["spans"]})
        self.assertTrue(any(s.get("usage") for s in llm))
        with_usage = [r for r in self.sdk.ended if r.kind == "LLM"]
        self.assertTrue(all(set(r.kwargs) <= {"model", "provider_name", "response_model", "response_id",
                                              "input_messages", "system_instructions", "output_messages",
                                              "request_max_tokens", "request_temperature", "finish_reasons",
                                              "started_at", "ended_at"}
                            for r in with_usage))
        self.assertEqual(len(traces), len(set(traces)))

    def test_llm_span_plan_fields_passed_one_to_one(self):
        # regression: request_temperature and usage.cache_creation_input_tokens of the plan (2.13) were dropped
        s = {"type": "llm", "key": "c-1", "model": "m", "request_max_tokens": 16384, "request_temperature": 0.0,
             "started_utc": "2026-11-10T03:15:02.000000Z", "ended_utc": "2026-11-10T03:15:03.000000Z",
             "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7, "reasoning_tokens": None,
                       "cache_read_input_tokens": 3, "cache_creation_input_tokens": 4}, "attributes": {}}
        with self.sdk.installed():
            tracing, _otel = adapter_agentlens._import_sdk()
            self.lens_sink()._span(tracing, s)
        (llm,) = [r for r in self.sdk.ended if r.kind == "LLM"]
        self.assertEqual((llm.kwargs["request_temperature"], llm.kwargs["request_max_tokens"]), (0.0, 16384))
        self.assertEqual(llm.usage["cache_creation_input_tokens"], 4)
        self.assertEqual(llm.usage["cache_read_input_tokens"], 3)
        self.assertIsNone(llm.usage["reasoning_tokens"])                  # a null counter is not sent

    def test_flush_result_is_ignored(self):
        self.sdk = FakeSdk(flush_result=False)
        self.c.job()
        with self.sdk.installed():
            ex = self.c.exporter(lens=self.lens_sink(), wandb=self.wandb_sink())
            r = ex.export_unit(self.c.unit(ex, JOB))["agent_lens"]
        self.assertEqual(self.sdk.calls("force_flush"), [("force_flush",)])
        self.assertEqual(r["status"], "verified")                     # the read-back decided, not the flush
        self.assertEqual(r["readback"]["spans"], r["expected_spans"])

    def test_readback_pages_by_offset_with_basic_auth_and_user_agent(self):
        plan = self.plan(JOB)
        traces = []
        with self.sdk.installed():
            sink = self.lens_sink(page_limit=3)
            sink.emit(plan, lambda c, t: traces.append(t))
            rows = sink.read_back(plan["project"], traces)
        self.assertEqual(len(rows), plan["expected_spans"])
        self.assertEqual(readback.verify_spans(plan, rows), [])
        req = self.sdk.requests
        self.assertTrue(len(req) > len(traces))                       # several pages per trace
        auth = "Basic " + base64.b64encode(f"api:{KEY}".encode()).decode()
        for r in req:
            self.assertEqual((r["url"], r["method"], r["timeout"]), (readback.READBACK_URL, "POST", 30))
            self.assertEqual(r["headers"]["Authorization"], auth)
            self.assertEqual(r["headers"]["User-agent"], adapter_agentlens.USER_AGENT)
            self.assertEqual(r["body"]["project_id"], "example-entity/mh-forge-test")
        offsets = {t: [r["body"]["offset"] for r in req if r["body"]["query"]["$expr"]["$eq"][1]["$literal"] == t]
                   for t in traces}
        longest = max(offsets.values(), key=len)
        self.assertEqual(longest, list(range(0, 3 * len(longest), 3)))   # offset advances by the page it got

    def test_readback_never_follows_a_redirect(self):
        # regression: urllib's default redirect handler re-sent the Basic auth key to whatever host a 3xx named
        import http.server
        import threading
        seen = []

        class Target(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"spans": [], "total_count": 0}')
            do_POST = do_GET

            def log_message(self, *a):
                pass

        servers = [http.server.HTTPServer(("127.0.0.1", 0), Target)]
        port = servers[0].server_port

        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{port}/elsewhere")
                self.end_headers()

            def log_message(self, *a):
                pass

        servers.append(http.server.HTTPServer(("127.0.0.1", 0), Redirect))
        with util.loopback_guard():
            for s in servers:
                threading.Thread(target=s.serve_forever, daemon=True).start()
            try:
                sink = CoreweaveSink(self.m, environ=self.environ)            # the real urllib opener
                with mock.patch.object(readback, "READBACK_URL", f"http://127.0.0.1:{servers[1].server_port}/q"):
                    with self.assertRaises(Exception):
                        sink.read_back("example-entity/mh-forge-test", ["0" * 31 + "1"])
            finally:
                for s in servers:
                    s.shutdown()
                    s.server_close()
        self.assertEqual(seen, [])                                        # the redirect target never saw the key

    def test_otel_environment_prepared_before_import(self):
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = "service.name=elsewhere"
        plan = self.plan(JOB)
        with self.sdk.installed():
            self.lens_sink().emit(plan, lambda c, t: None)
        env = self.sdk.imports["coreweave.forge.agentlens.tracing"]
        self.assertIsNone(env["OTEL_RESOURCE_ATTRIBUTES"])
        self.assertEqual((env["OTEL_BSP_MAX_EXPORT_BATCH_SIZE"], env["OTEL_EXPORTER_OTLP_TRACES_COMPRESSION"]),
                         ("32", "gzip"))

    def test_every_otel_variable_removed_before_init(self):
        # regression: only OTEL_RESOURCE_ATTRIBUTES was removed; _HEADERS adds request headers, _SDK_DISABLED and
        # _TRACES_SAMPLER=always_off drop every span while init succeeds
        for name, value in (("OTEL_SDK_DISABLED", "true"), ("OTEL_TRACES_SAMPLER", "always_off"),
                            ("OTEL_EXPORTER_OTLP_HEADERS", "x-extra=1"), ("OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT", "4"),
                            ("OTEL_PYTHON_EXPORTER_OTLP_HTTP_CREDENTIAL_PROVIDER", "elsewhere")):
            os.environ[name] = value
        plan = self.plan(JOB)
        with self.sdk.installed():
            self.lens_sink().emit(plan, lambda c, t: None)
        self.assertFalse([n for n in os.environ if n.startswith("OTEL_") and n not in
                          ("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "OTEL_EXPORTER_OTLP_TRACES_COMPRESSION")])
        env = self.sdk.imports["coreweave.forge.agentlens.tracing"]
        self.assertEqual((env["OTEL_SDK_DISABLED"], env["OTEL_EXPORTER_OTLP_HEADERS"]), (None, None))

    def test_netrc_closed_for_the_trace_transport(self):
        # requests (the OTLP transport) adds Basic auth from a netrc entry for the trace host
        os.environ.pop("NETRC", None)
        plan = self.plan(JOB)
        with self.sdk.installed():
            self.lens_sink().emit(plan, lambda c, t: None)
        self.assertEqual(self.sdk.imports["coreweave.forge.agentlens.tracing"]["NETRC"], os.devnull)
        os.environ["NETRC"] = str(self.st.paths.export_netrc)          # the export child's empty file is kept
        with self.sdk.installed():
            self.lens_sink().emit(plan, lambda c, t: None)
        self.assertEqual(os.environ["NETRC"], str(self.st.paths.export_netrc))

    def test_disabled_tracer_fails_before_any_trace_is_reported(self):
        self.sdk = FakeSdk(zero_trace=True)
        plan, seen = self.plan(JOB), []
        with self.sdk.installed(), self.assertRaises(ExportError):
            self.lens_sink().emit(plan, lambda c, t: seen.append(t))
        self.assertEqual(seen, [])
        self.assertEqual(self.sdk.calls("shutdown"), [("shutdown",)])

    def test_plan_the_sdk_rejects_fails_before_init(self):
        # regression: Message.role is a Literal and the span classes are pydantic models; a bad value raised after
        # the first trace had started, leaving a partial trace
        plan = self.plan(JOB)
        llm = next(s for t in plan["conversations"][0]["turns"] for s in t["spans"] if s["type"] == "llm")
        llm["input_messages"] = [{"role": "developer", "parts": [{"type": "text", "content": "x"}]}]
        with self.sdk.installed(), self.assertRaises(ExportError) as ctx:
            self.lens_sink().emit(plan, lambda c, t: None)
        self.assertIn("rejects the span plan", str(ctx.exception))
        self.assertEqual(self.sdk.calls("tracing.init"), [])

    def test_otel_limits_refused_before_init(self):
        plan = self.plan(JOB)
        conv = plan["conversations"][0]
        conv["attributes"] = {f"metaharness.c{i}": 1 for i in range(64)}
        conv["turns"][0]["spans"][0]["attributes"] = {f"metaharness.s{i}": 1 for i in range(40)}
        with self.sdk.installed(), self.assertRaises(ExportError) as ctx:
            self.lens_sink().emit(plan, lambda c, t: None)
        self.assertIn("attributes exceed", str(ctx.exception))
        plan = self.plan(JOB)
        with mock.patch.object(adapter_agentlens, "OTLP_MAX_REQUEST_BYTES", 1000):
            with self.sdk.installed(), self.assertRaises(ExportError) as ctx:
                self.lens_sink().emit(plan, lambda c, t: None)
        self.assertIn("64 MiB", str(ctx.exception))
        self.assertEqual(self.sdk.calls("tracing.init"), [])
        self.assertEqual(adapter_agentlens._limit_problems(self.plan(JOB), 32, adapter_agentlens.Decimal("1.5")), [])

    def test_error_type_is_the_plans_type(self):
        # regression: record_error prefixes error.type with the class module unless it is "builtins", so the stored
        # type read "mhforge.telemetry.adapter_agentlens.ToolError"
        exc = adapter_agentlens._error_for({"type": "ToolError", "message": "m"})
        cls = type(exc)
        error_type = cls.__qualname__ if cls.__module__ == "builtins" else f"{cls.__module__}.{cls.__qualname__}"
        self.assertEqual((error_type, str(exc)), ("ToolError", "m"))
        self.assertIsInstance(exc, adapter_agentlens._SpanError)

    def test_sdk_missing_is_adapter_unavailable(self):
        plan = self.plan(JOB)
        with mock.patch.dict(sys.modules, {"coreweave": None, "coreweave.forge": None,
                                           "coreweave.forge.agentlens": None}):
            with self.assertRaises(AdapterUnavailable):
                self.lens_sink().emit(plan, lambda c, t: None)


# ---------------------------------------------------------------- both

class Both(AdapterCase):
    def test_adapters_import_lazily(self):
        code = ("import json, sys\n"
                "import mhforge.telemetry.adapter_agentlens, mhforge.telemetry.adapter_wandb\n"
                "import mhforge.telemetry.exporter, mhforge.telemetry.spawn\n"
                "print(json.dumps(sorted(m for m in sys.modules if m.split('.')[0] in "
                "('wandb', 'wandb_gql', 'coreweave', 'opentelemetry', 'weave', 'cwevals', 'wandb_workspaces'))))\n")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(helpers.SC), env=helpers.child_env(),
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [])
        with self.sdk.installed():
            self.wandb_sink()
            self.lens_sink()
        self.assertEqual(self.sdk.imports, {})                        # constructing a sink imports nothing
        for mod in (adapter_wandb, adapter_agentlens):
            top = [line for line in Path(mod.__file__).read_text().splitlines()
                   if line.startswith(("import ", "from "))]
            self.assertFalse([t for t in top if any(n in t for n in ("wandb", "coreweave", "opentelemetry", "weave"))],
                             mod.__name__)

    def test_key_passed_explicitly_never_logged(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = self.full_export()
        self.assertEqual({s for st in result["statuses"].values() for s in st.values()}, {"verified"})
        self.assertNotIn(KEY, out.getvalue() + err.getvalue())
        allowed = {"tracing.init", "wandb.Settings", "wandb.Api", "wandb.init"}   # init carries the Settings object
        for entry in self.sdk.log:
            text = json.dumps(entry, default=lambda o: getattr(o, "kwargs", str(o)))
            if KEY in text:
                self.assertIn(entry[0], allowed, entry[0])
        auth = "Basic " + base64.b64encode(f"api:{KEY}".encode()).decode()
        self.assertEqual({r["headers"]["Authorization"] for r in self.sdk.requests}, {auth})
        self.assertNotIn(KEY, json.dumps(dict(os.environ)))
        self.assertNotIn(KEY, json.dumps(self.sdk.imports))
        for p in self.st.paths.root.rglob("*"):
            if p.is_file():
                self.assertNotIn(KEY.encode(), p.read_bytes(), p.name)
        for sink in (self.wandb_sink(), self.lens_sink()):
            self.assertNotIn(KEY, repr(sink))
            self.assertEqual(sink.key_facts, {"name": "MHFORGE_EXPORT_API_KEY", "len": len(KEY),
                                              "sha256": util.sha256_bytes(KEY.encode())})

    def test_missing_key_refuses_without_default_chain(self):
        os.environ["WANDB_API_KEY"] = "a-key-the-sdk-would-find-by-itself-0123"
        os.environ["MHFORGE_EXPORT_API_KEY"] = KEY                    # the process env is not the key mapping
        with self.sdk.installed():
            for environ in ({}, {"MHFORGE_EXPORT_API_KEY": ""}, {"WANDB_API_KEY": KEY}):
                with self.subTest(environ=sorted(environ)):
                    with self.assertRaises(ExportError):
                        self.wandb_sink(environ=environ)
                    with self.assertRaises(ExportError):
                        self.lens_sink(environ=environ)
        self.assertEqual((self.sdk.imports, self.sdk.log), ({}, []))


if __name__ == "__main__":
    unittest.main()
