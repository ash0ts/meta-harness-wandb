# SPDX-License-Identifier: MIT
"""The two adapters against the installed SDKs (wandb 0.31.0, coreweave 0.1.0b0, cwevals 0.0.1b1, OpenTelemetry 1.45.2).

Skipped as a whole when the SDKs are not importable (the system python3). Under .venv it runs
every SDK operation the adapters make with the adapters' own code, against the real classes and signatures:

- Agent Lens span objects, messages, parts and usage are the real pydantic models (they validate every plan of the test
  campaign); every span is entered and ended with no tracing session, so each call is a silent no-op and nothing is
  exported. `tracing.init` and the session methods are only bound against their real signatures, never called.
- W&B `Settings`, `EvalTable` and `Table` are the real classes; `wandb.init`, `wandb.Api`, `wandb.Artifact` and every run,
  artifact, API and service-API method are bound with `inspect.signature(...).bind` against the real definitions and
  checked to exist; their results are stand-ins. No login, run, request or service is started.
- The CES limits the EvalTable writer enforces at `run.log` are applied to every table plan with the writer's own
  name, primitive-type and type-merge functions and its constants.

Run it isolated:  cd <empty dir> && env -i HOME=<empty> PATH=/usr/bin:/bin WANDB_MODE=disabled \
    .venv/bin/python -I -m unittest discover -s <showcase>/tests -t <showcase> -p test_sdk_signatures.py
"""

from __future__ import annotations

import inspect
import os
import types
import unittest
from pathlib import Path
from unittest import mock

_SDK_MISSING = None
try:
    import coreweave_evaluations  # noqa: F401 - the CES client wandb imports lazily for backend "ces"
    import wandb
    from coreweave.forge.agentlens import tracing
    from opentelemetry import trace as otel_trace
    from wandb.apis.public import Api as _PublicApi
    from wandb.apis.public.runs import Run as _PublicRun
    from wandb.apis.public.service_api import ServiceApi as _ServiceApi
    from wandb.proto import wandb_internal_pb2 as _pb
    from wandb.sdk.artifacts.artifact import Artifact as _Artifact
    from wandb.sdk.data_types.eval_table import _writer_ces
    from wandb.sdk.wandb_run import Run as _Run
    from wandb.sdk.wandb_summary import Summary as _Summary
except ImportError as _exc:                                    # the system python3: nothing to check against
    _SDK_MISSING = f"the Forge SDKs are not importable ({type(_exc).__name__})"

from mhforge import util
from mhforge.errors import ExportError
from mhforge.telemetry import adapter_agentlens, adapter_wandb, plan_tables
from mhforge.telemetry.adapter_agentlens import CoreweaveSink
from mhforge.telemetry.adapter_wandb import WandbSdkSink
from tests import helpers
from tests.test_exporter import Campaign

KEY = "mhf-signature-check-key-0123456789abcdefghij"
_SELF = object()                                               # the placeholder bound to `self`


def _bind(fn, *args, **kwargs) -> inspect.BoundArguments:
    """inspect.signature(fn).bind: TypeError for a parameter name or arity the real definition does not take."""
    return inspect.signature(fn).bind(*args, **kwargs)


class _Bound:
    """A stand-in for an SDK object: every method the adapter calls must exist on the real class and accept the exact
    arguments (bound with a `self` placeholder); every property must be a real property; instance attributes must be
    assigned in the real __init__. Calls are recorded; results come from `returns`."""

    def __init__(self, calls: list, cls: type, label: str, *, instance: dict | None = None,
                 props: dict | None = None, returns: dict | None = None) -> None:
        object.__setattr__(self, "_s", (calls, cls, label, instance or {}, props or {}, returns or {}))

    def __getattr__(self, name):
        calls, cls, label, instance, props, returns = object.__getattribute__(self, "_s")
        if name in instance:
            src = inspect.getsource(cls)
            assert f"self.{name} =" in src or f"self.{name}:" in src, f"{cls.__name__}.{name} is never assigned"
            return instance[name]
        real = inspect.getattr_static(cls, name)                # AttributeError: the SDK has no such member
        if isinstance(real, property):
            return props[name]
        fn = getattr(cls, name)

        def call(*args, **kwargs):
            _bind(fn, _SELF, *args, **kwargs)
            calls.append((f"{label}.{name}", args, kwargs))
            result = returns.get(name)
            return result() if callable(result) and not isinstance(result, _Bound) else result
        return call


@unittest.skipIf(_SDK_MISSING is not None, _SDK_MISSING or "")
class SdkCase(unittest.TestCase):
    def setUp(self) -> None:
        self._env = mock.patch.dict(os.environ)
        self._env.start()
        for name in [n for n in os.environ if n.startswith(("WANDB_", "CES_", "OTEL_"))]:
            os.environ.pop(name, None)
        self._ctx = helpers.tmp_state(manifest=True)
        self.st = self._ctx.__enter__()
        self.m = self.st.manifest
        self.environ = {"MHFORGE_EXPORT_API_KEY": KEY}
        self.calls: list = []

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)
        self._env.stop()

    def plans(self, name: str) -> list[dict]:
        """Every <name>.json plan of a full dry-run export of the test campaign (jobs, evaluations, proposer, loop)."""
        if not getattr(self, "_planned", False):
            c = Campaign(self.st)
            c.everything()
            c.exporter(dry_run=True, ledger=False).export_all()
            self._planned = True
        out =[util.read_json(p) for p in sorted(self.st.paths.export_root.glob(f"plans/*/{name}.json"))]
        self.assertTrue(out, name)
        return out


# ---------------------------------------------------------------- Agent Lens

@unittest.skipIf(_SDK_MISSING is not None, _SDK_MISSING or "")
class AgentLensSignatures(SdkCase):
    def test_public_names_used_exist(self):
        for name in ("init", "force_flush", "shutdown", "TracingSession", "Conversation", "Turn", "LLM", "Tool",
                     "SubAgent", "Message", "TextPart", "ReasoningPart", "Usage"):
            self.assertTrue(hasattr(tracing, name), name)
        for name in ("end_tool", "end_subagent", "log_llm", "log_tool"):            # what the adapter must not rely on
            self.assertFalse(hasattr(tracing, name), name)
        for cls in (tracing.Turn, tracing.LLM, tracing.Tool, tracing.SubAgent):
            for method in ("set_attributes", "record_error", "__enter__", "__exit__", "start", "end"):
                self.assertTrue(callable(getattr(cls, method, None)), f"{cls.__name__}.{method}")
            self.assertIn("started_at", cls.model_fields)
            self.assertIn("ended_at", cls.model_fields)
        self.assertTrue(callable(getattr(tracing.Conversation, "__enter__", None)))
        self.assertEqual(adapter_agentlens.ADAPTER["version"], tracing.__version__)

    def test_init_binds_exact_arguments(self):
        rec = {}

        def init(*args, **kwargs):
            rec["bound"] = _bind(tracing.init, *args, **kwargs)
            return _Bound(self.calls, tracing.TracingSession, "session")
        proxy = types.SimpleNamespace(init=init)
        session = adapter_agentlens._sdk_init(proxy, "example-entity/mh-forge-test", KEY, "mhforge-showcase")
        args = rec["bound"].arguments
        self.assertEqual(args, {"project_name": "example-entity/mh-forge-test", "api_key": KEY,
                                "base_url": adapter_agentlens.TRACE_BASE_URL, "service_name": "mhforge-showcase",
                                "autopatch_integrations": False})
        adapter_agentlens._sdk_flush(session)
        adapter_agentlens._sdk_shutdown(session)
        self.assertEqual([c[0] for c in self.calls], ["session.force_flush", "session.shutdown"])
        # the endpoint the SDK derives from base_url, and the netrc lookup it skips when a key is passed
        src = inspect.getsource(tracing.init)
        self.assertIn("api_key or os.environ.get(\"WANDB_API_KEY\") or _netrc_api_key()", src)
        from coreweave.forge.agentlens.tracing import tracing as tracing_mod
        self.assertEqual(tracing_mod._trace_server_url(adapter_agentlens.TRACE_BASE_URL), "https://trace.wandb.ai")
        self.assertEqual(tracing_mod.OTLP_AGENT_TRACES_PATH, "/agents/otel/v1/traces")

    def test_every_planned_span_builds_and_runs_against_the_real_classes(self):
        sink = CoreweaveSink(self.m, environ=self.environ)
        n = 0
        for plan in self.plans("spans"):
            sink._check_plan(tracing, plan)                     # every real object built: pydantic accepts the plan
            self.assertEqual(adapter_agentlens._limit_problems(plan, sink.batch_size, sink.byte_factor), [])
            for conv in plan["conversations"]:
                c = adapter_agentlens._sdk_conversation(tracing, conv)
                self.assertIsInstance(c, tracing.Conversation)
                with c:
                    for turn in conv["turns"]:
                        t = adapter_agentlens._sdk_turn(tracing, turn)
                        self.assertEqual(t.started_at.utcoffset().total_seconds(), 0)     # tz-aware UTC
                        with t:                         # no tracing session: every call below is a silent no-op
                            adapter_agentlens._sdk_set_attributes(t, turn.get("attributes") or {})
                            if turn.get("error"):
                                adapter_agentlens._sdk_record_error(t, turn["error"])
                            for s in turn.get("spans") or ():
                                sink._span(tracing, s)
                                n += 1
        self.assertGreater(n, 0)

    def test_planned_values_reach_the_real_fields(self):
        s = {"type": "llm", "key": "c-1", "model": "m", "provider_name": "openrouter", "response_model": "m2",
             "response_id": "r", "request_max_tokens": 16384, "request_temperature": 0.0,
             "system_instructions": ["sys"], "finish_reasons": ["stop"],
             "input_messages": [{"role": "user", "parts": [{"type": "text", "content": "q"}]}],
             "output_messages": [{"role": "assistant", "parts": [{"type": "reasoning", "content": "r"},
                                                                 {"type": "text", "content": "a"}]}],
             "started_utc": "2026-11-10T03:15:02.000000Z", "ended_utc": "2026-11-10T03:15:03.000000Z",
             "usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7, "reasoning_tokens": None,
                       "cache_read_input_tokens": 3, "cache_creation_input_tokens": 4}}
        llm = CoreweaveSink._build(tracing, s)
        self.assertIsInstance(llm.usage, tracing.Usage)
        self.assertEqual((llm.usage.input_tokens, llm.usage.cache_creation_input_tokens, llm.usage.reasoning_tokens),
                         (5, 4, 0))
        self.assertEqual((llm.request_temperature, llm.request_max_tokens, llm.finish_reasons), (0.0, 16384, ["stop"]))
        self.assertEqual([type(p).__name__ for p in llm.output_messages[0].parts], ["ReasoningPart", "TextPart"])
        self.assertEqual(llm.started_at.timestamp(), util.parse_utc(s["started_utc"]))
        tool = CoreweaveSink._build(tracing, {"type": "tool", "name": "bash", "tool_call_id": "t1",
                                              "arguments": '{"a": 1}', "result": "ok",
                                              "started_utc": s["started_utc"], "ended_utc": s["ended_utc"]})
        self.assertEqual((tool.arguments, tool.result), ('{"a": 1}', "ok"))         # a str passes through as is
        sub = CoreweaveSink._build(tracing, {"type": "subagent", "name": "subagent", "model": "m",
                                             "started_utc": s["started_utc"], "ended_utc": s["ended_utc"]})
        self.assertIsInstance(sub, tracing.SubAgent)
        with self.assertRaises(Exception):                      # Message.role is a Literal: the reason for _check_plan
            tracing.Message(role="developer", parts=[])
        _bind(tracing.Turn.set_attributes, _SELF, {"metaharness.x": 1})
        _bind(tracing.Turn.record_error, _SELF, adapter_agentlens._error_for({"type": "ToolError"}))

    def test_trace_id_of_the_real_noop_tracer_is_refused(self):
        self.assertEqual(adapter_agentlens._sdk_trace_id(otel_trace), adapter_agentlens._TRACE_ZERO)
        plan, seen = self.plans("spans")[0], []
        sink = CoreweaveSink(self.m, environ=self.environ)
        with mock.patch.object(adapter_agentlens, "_sdk_init",
                               lambda *a: _Bound(self.calls, tracing.TracingSession, "session")):
            with self.assertRaises(ExportError):
                sink.emit(plan, lambda c, t: seen.append(t))
        self.assertEqual(seen, [])
        self.assertEqual([c[0] for c in self.calls], ["session.shutdown"])

    def test_otel_limits_are_the_sdk_defaults(self):
        from opentelemetry.exporter.otlp.proto.http import _common
        from opentelemetry.sdk.trace import SpanLimits
        self.assertEqual(SpanLimits().max_span_attributes, adapter_agentlens.OTEL_MAX_ATTRIBUTES)
        self.assertEqual(_common._DEFAULT_MAX_REQUEST_SIZE, adapter_agentlens.OTLP_MAX_REQUEST_BYTES)
        from coreweave.forge.agentlens.tracing import capture_info
        llm_attrs = inspect.signature(
            __import__("coreweave.forge.agentlens.tracing.conversation_otel", fromlist=["x"]).llm_attributes)
        self.assertLessEqual(24 + len(capture_info.get_capture_info()) + 1, adapter_agentlens.SDK_ATTRIBUTES)
        self.assertIn("request_choice_count", llm_attrs.parameters)


# ---------------------------------------------------------------- W&B

@unittest.skipIf(_SDK_MISSING is not None, _SDK_MISSING or "")
class WandbSignatures(SdkCase):
    def _wandb_proxy(self):
        calls = self.calls

        def artifact(*args, **kwargs):
            _bind(wandb.Artifact, *args, **kwargs)
            calls.append(("wandb.Artifact", args, kwargs))
            return _Bound(calls, _Artifact, "artifact")

        def run():
            return _Bound(calls, _Run, "run", instance={"summary": _Bound(calls, _Summary, "summary")},
                          props={"url": "https://forge.coreweave.com/wandb/e/p/runs/r",
                                 "settings": wandb.Settings(base_url=adapter_wandb.DEFAULT_BASE_URL)},
                          returns={"log_artifact": lambda: _Bound(calls, _Artifact, "logged")})

        def init(*args, **kwargs):
            _bind(wandb.init, *args, **kwargs)
            calls.append(("wandb.init", args, kwargs))
            return run()

        def api(*args, **kwargs):
            _bind(_PublicApi, *args, **kwargs)
            calls.append(("wandb.Api", args, kwargs))
            summary = {"val/USPTO": {"_type": adapter_wandb.CES_MARKER}}
            return _Bound(calls, _PublicApi, "api",
                          instance={"_service_api": _Bound(calls, _ServiceApi, "service",
                                                           returns={"feature_enabled": True,
                                                                    "execute_graphql": {"project": None}})},
                          returns={"run": lambda: _Bound(calls, _PublicRun, "public_run",
                                                         props={"summary_metrics": summary}),
                                   "artifact": object})
        return types.SimpleNamespace(Settings=wandb.Settings, init=init, Artifact=artifact, Api=api,
                                     EvalTable=wandb.EvalTable, Table=wandb.Table)

    def sink(self) -> WandbSdkSink:
        proxy = self._wandb_proxy()
        sink = WandbSdkSink(self.m, environ=self.environ, paths=self.st.paths)
        self._patch = mock.patch.object(adapter_wandb, "_import_wandb", lambda: proxy)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        return sink

    def test_versions_and_absent_names(self):
        self.assertEqual(adapter_wandb.ADAPTER["version"], wandb.__version__)
        self.assertFalse(hasattr(_PublicApi, "client"))        # the old api.client.execute path does not exist
        import importlib.util
        self.assertIsNone(importlib.util.find_spec("wandb_gql"))
        self.assertIsNone(importlib.util.find_spec("weave"))   # backend "weave" cannot work here; "ces" is used
        self.assertEqual(wandb.Settings.model_fields["base_url"].default, adapter_wandb.DEFAULT_BASE_URL)
        self.assertEqual(_pb.ServerFeature.Name(_pb.ServerFeature.Value("EVAL_TABLES_CES")), "EVAL_TABLES_CES")

    def test_settings_are_real_and_valid(self):
        s = adapter_wandb._sdk_settings(wandb, KEY)
        self.assertIsInstance(s, wandb.Settings)
        self.assertEqual((s.api_key, s.base_url, s.console, s.disable_git, s.save_code),
                         (KEY, adapter_wandb.DEFAULT_BASE_URL, "off", True, False))
        self.assertTrue(s.x_disable_meta and s.x_disable_stats and s.x_disable_machine_info)
        self.assertFalse(s.x_save_requirements)

    def test_log_run_binds_every_call_against_the_real_sdk(self):
        sink = self.sink()
        runs = self.plans("runs")
        table_plans = [t for p in self.plans("tables") for t in p.get("tables") or ()]
        self.assertTrue(table_plans)
        for plan in runs:
            self.calls.clear()
            out = sink.log_run(plan, [], self.st.paths.root)
            names = [c[0] for c in self.calls]
            self.assertEqual(names[0], "wandb.init")
            self.assertEqual(names[-1], "run.finish")
            self.assertEqual(out["base_url"], adapter_wandb.DEFAULT_BASE_URL)
            settings = self.calls[0][2]["settings"]
            self.assertIsInstance(settings, wandb.Settings)
            self.assertEqual(settings.api_key, KEY)
        self.calls.clear()
        run = next(p for p in runs if any(a.get("op") == "log" for a in p["run"].get("artifacts") or ()))
        sink.log_run(run, [{**t, "backend": "ces"} for t in table_plans[:1]], self.st.paths.root)
        names = [c[0] for c in self.calls]
        for want in ("wandb.Artifact", "artifact.add_file", "run.log_artifact", "logged.wait", "run.log"):
            self.assertIn(want, names)
        logged = [c for c in self.calls if c[0] == "run.log" and isinstance(next(iter(c[1][0].values())), wandb.EvalTable)]
        self.assertEqual(len(logged), 1)
        self.calls.clear()
        sink.log_run(run, [{**t, "backend": "wandb_table"} for t in table_plans[:1]], self.st.paths.root)
        self.assertTrue(any(c[0] == "run.log" and isinstance(next(iter(c[1][0].values())), wandb.Table)
                            and not isinstance(next(iter(c[1][0].values())), wandb.EvalTable) for c in self.calls))
        uses = [p for p in runs for a in p["run"].get("artifacts") or () if a.get("op") != "log"]
        if uses:
            self.calls.clear()
            sink.log_run(uses[0], [], self.st.paths.root)
            self.assertIn("run.use_artifact", [c[0] for c in self.calls])
        metric = adapter_wandb._sdk_define_metric
        r = self._wandb_proxy().init(project="p")
        metric(r, "iteration", None)
        metric(r, "search/*", "iteration")
        adapter_wandb._sdk_summary_update(r, {"a": 1})
        self.assertIn("summary.update", [c[0] for c in self.calls])

    def test_api_calls_bind_against_the_real_sdk(self):
        sink = self.sink()
        self.assertTrue(sink.feature_ces())
        self.assertIsNone(sink.project_access("example-entity", "mh-forge-test"))
        self.assertEqual(sink.confirm("e/p/r", ["val/USPTO"], []), [])
        api = self._wandb_proxy().Api(overrides={"base_url": adapter_wandb.DEFAULT_BASE_URL}, api_key=KEY)
        self.assertIs(adapter_wandb._sdk_artifact_exists(api, "e/p/candidate-x:c"), True)
        names = [c[0] for c in self.calls]
        for want in ("wandb.Api", "service.feature_enabled", "service.execute_graphql", "api.run", "api.artifact"):
            self.assertIn(want, names)
        apis = [c for c in self.calls if c[0] == "wandb.Api"]
        self.assertTrue(all(c[2] == {"overrides": {"base_url": adapter_wandb.DEFAULT_BASE_URL}, "api_key": KEY}
                            for c in apis))
        feature = next(c for c in self.calls if c[0] == "service.feature_enabled")
        self.assertEqual(feature[1], ("EVAL_TABLES_CES",))

    def test_table_plans_pass_the_ces_writer_checks(self):
        writer = _writer_ces.CESWriter()
        self.assertEqual(_writer_ces._MAX_ROWS_PER_TABLE, plan_tables.MAX_ROWS)
        n = 0
        for plan in self.plans("tables"):
            for t in plan.get("tables") or ():
                n += 1
                self.assertTrue(all(plan_tables.check_table(t).values()), t["key"])
                table = adapter_wandb._sdk_eval_table(wandb, t)             # the real constructor and Table typing
                self.assertIsInstance(table, wandb.EvalTable)
                adapter_wandb._sdk_table(wandb, t)
                writer._validate_name("EvalTable", t["key"], max_length=_writer_ces._MAX_EVAL_TABLE_NAME_LENGTH)
                self.assertTrue(1 <= len(t["rows"]) <= _writer_ces._MAX_ROWS_PER_TABLE)
                self.assertLessEqual(len(t["score_columns"]), _writer_ces._MAX_SCORERS)
                self.assertLessEqual(len(t["input_columns"]) + len(t["output_columns"]), _writer_ces._MAX_DATASET_FIELDS)
                for i, col in enumerate(t["columns"]):
                    limit = (_writer_ces._MAX_SCORER_NAME_LENGTH if col in t["score_columns"]
                             else _writer_ces._MAX_DATASET_FIELD_NAME_LENGTH)
                    writer._validate_name("column", col, max_length=limit)
                    kind = None
                    for row in t["rows"]:
                        _value, observed = writer._normalize_primitive(row[i], col)   # UsageError: not primitive
                        if observed is not None:
                            kind = writer._merge_type(col, kind, observed)           # UsageError: mixed types
                    self.assertIsNotNone(kind, f"{t['key']}:{col} is null in every row")
        self.assertGreater(n, 0)


if __name__ == "__main__":
    unittest.main()
