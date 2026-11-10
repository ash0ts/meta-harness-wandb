# SPDX-License-Identifier: MIT
"""Fakes for the export surfaces (spec 5.1).

FakeLensSink and FakeWandbSink stand in for Agent Lens, W&B and CES at the exporter's protocol (LensSink, WandbSink):
they store spans, runs, tables and artifacts, return fewer or more rows on read-back, delay ingestion, fail before or
after sending, reject null scores and changed content at one location as CES does, flip the CES flag, and answer for a
project that does not exist or is not PRIVATE.

FakeSdk stands in for the SDK modules the two adapters import (`wandb`, `coreweave.forge.agentlens.tracing`,
`opentelemetry.trace`, optionally `weave`). It is installed as a meta-path finder, so a module is created only when the
adapter imports it, and the environment at that moment is recorded. Every SDK call is logged in order with its keyword
arguments; the constructors and methods take only parameter names the installed SDKs have (wandb 0.31.0, coreweave
0.1.0b0; tests/test_sdk_signatures.py binds the same calls against the real signatures), so a misspelt one fails.
wandb 0.31.0 has no `wandb_gql` and no `Api.client`: GraphQL and the server-feature probe go through `Api._service_api`.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import importlib.abc
import importlib.util
import json
import os
import sys
import threading
import types
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence

from mhforge import util
from mhforge.errors import ExportError

PRIVATE = {"access": "PRIVATE", "read_only": False, "can_write": True}
DEFAULT_PROJECT = ("example-entity", "mh-forge-test")


def _naive(utc: str) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(util.parse_utc(utc), dt.timezone.utc).replace(tzinfo=None).isoformat()


def _stored_messages(messages) -> list:
    return [{"role": m["role"], "content": json.dumps([dict(p) for p in m.get("parts") or ()])} for m in messages or ()]


def stored_rows(turn: Mapping) -> list[dict]:
    """What a faithful store returns for one turn of a span plan, in the row shape the H2 read-back saw."""
    rows: list[dict] = []

    def common(item: Mapping) -> dict:
        return {"started_at": _naive(item["started_utc"]), "ended_at": _naive(item["ended_utc"]),
                "status_code": "ERROR" if item.get("error") else "OK", "attributes": dict(item.get("attributes") or {})}

    def walk(spans) -> None:
        for s in spans or ():
            row = common(s)
            if s["type"] == "llm":
                u = s.get("usage") or {}
                row.update({"operation_name": "chat", "request_model": s.get("model"),
                            "response_model": s.get("response_model") or None, "response_id": s.get("response_id"),
                            "input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
                            "cache_read_input_tokens": u.get("cache_read_input_tokens"),
                            "reasoning_tokens": u.get("reasoning_tokens"),
                            "request_max_tokens": s.get("request_max_tokens"),
                            "finish_reasons": list(s.get("finish_reasons") or []),
                            "system_instructions": list(s.get("system_instructions") or []),
                            "input_messages": _stored_messages(s.get("input_messages")),
                            "output_messages": _stored_messages(s.get("output_messages"))})
            elif s["type"] == "tool":
                row.update({"operation_name": "execute_tool", "tool_call_id": s.get("tool_call_id"),
                            "tool_name": s.get("name"), "tool_call_result": s.get("result"),
                            "tool_call_arguments": "" if s.get("arguments") == "{}" else s.get("arguments")})
            else:
                row.update({"operation_name": "invoke_agent", "agent_name": s.get("name")})
            rows.append(row)
            walk(s.get("spans"))

    rows.append({**common(turn), "operation_name": "invoke_agent", "agent_name": turn["agent_name"]})
    walk(turn.get("spans"))
    return rows


# ---------------------------------------------------------------- protocol-level fakes for the exporter

class FakeLensSink:
    """LensSink: stores the rows of every emitted turn under a trace id of its own."""

    adapter = {"name": "fake-lens", "version": "0", "unverified": False}

    def __init__(self, *, drop: int = 0, extra: int = 0, delay_polls: int = 0, fail_before: bool = False,
                 fail_after: bool = False, probe: Callable[[str], None] | None = None) -> None:
        self.drop, self.extra, self.delay_polls = drop, extra, delay_polls
        self.fail_before, self.fail_after = fail_before, fail_after
        self.probe = probe or (lambda event: None)
        self.store: dict[str, list[dict]] = {}
        self.emitted: list[dict] = []
        self.events: list[tuple] = []
        self.read_calls: list[tuple[str, tuple[str, ...]]] = []

    def emit(self, plan: Mapping, on_trace: Callable[[str, str], None]) -> None:
        self.events.append(("emit", plan["unit"]))
        self.probe("emit")
        if self.fail_before:
            raise RuntimeError("fake lens: failed before sending")
        self.emitted.append(copy.deepcopy(dict(plan)))
        for conv in plan["conversations"]:
            for i, turn in enumerate(conv["turns"]):
                seed = f"{conv['conversation_id']}/{i}/{len(self.emitted)}"
                trace_id = hashlib.sha256(seed.encode()).hexdigest()[:32]
                on_trace(conv["conversation_id"], trace_id)
                self.events.append(("trace", conv["conversation_id"], trace_id))
                for row in stored_rows(turn):
                    self.events.append(("span_end", row["operation_name"]))
                    self.probe("span_end")
                    self.store.setdefault(trace_id, []).append(row)
        if self.fail_after:
            raise RuntimeError("fake lens: failed after sending")

    def read_back(self, project: str, trace_ids: Sequence[str]) -> list[dict]:
        self.read_calls.append((project, tuple(trace_ids)))
        if len(self.read_calls) <= self.delay_polls:
            return []
        rows = [copy.deepcopy(r) for t in trace_ids for r in self.store.get(t, [])]
        if self.drop:
            rows = rows[:-self.drop]
        if self.extra:
            rows = rows + copy.deepcopy(rows[:self.extra])
        return rows

    def all_rows(self) -> list[dict]:
        return [r for rows in self.store.values() for r in rows]


class FakeWandbSink:
    """WandbSink: runs, tables (one content per location, null scores refused as CES does), artifacts, uploads."""

    adapter = {"name": "fake-wandb", "version": "0", "unverified": False}

    def __init__(self, *, ces: bool | Sequence[bool] = True, projects: Mapping | None = None,
                 fail_before: bool = False, fail_after: bool = False, drop_markers: bool = False) -> None:
        self._ces = list(ces) if isinstance(ces, (list, tuple)) else None
        self._ces_flag = bool(ces) if self._ces is None else None
        self.projects = dict(projects) if projects is not None else {DEFAULT_PROJECT: dict(PRIVATE)}
        self.fail_before, self.fail_after, self.drop_markers = fail_before, fail_after, drop_markers
        self.runs: dict[str, dict] = {}
        self.tables: dict[tuple[str, str], str] = {}          # (run path, key) -> content hash (the CES location)
        self.table_logs: list[tuple[str, str, str]] = []      # (run path, key, backend) per logged table
        self.artifacts: dict[str, list[dict]] = {}
        self.uploads: list[tuple[str, bytes]] = []            # (source, bytes) of every uploaded file
        self.created_projects: list[tuple[str, str]] = []
        self.calls: list[tuple] = []

    def feature_ces(self) -> bool:
        self.calls.append(("feature_ces",))
        if self._ces is not None:
            return bool(self._ces.pop(0)) if self._ces else False
        return bool(self._ces_flag)

    def project_access(self, entity: str, project: str) -> dict | None:
        self.calls.append(("project_access", entity, project))
        info = self.projects.get((entity, project))
        return dict(info) if info is not None else None

    def log_run(self, run_plan: Mapping, table_plans: Sequence[Mapping], files_root: Path) -> dict:
        run = run_plan["run"]
        path = f"{run['entity']}/{run['project']}/{run['id']}"
        self.calls.append(("log_run", path, tuple(t["key"] for t in table_plans)))
        if self.fail_before:
            raise ExportError("fake wandb: refused before any SDK call")
        if (run["entity"], run["project"]) not in self.projects:
            self.created_projects.append((run["entity"], run["project"]))      # what an SDK would do implicitly
            self.projects[(run["entity"], run["project"])] = {"access": "USER_READ", "read_only": False,
                                                              "can_write": True}
        doc = self.runs.setdefault(path, {"plan": copy.deepcopy(dict(run_plan)), "history": [], "summary": {}})
        doc["history"] += copy.deepcopy(list(run.get("history") or []))
        for art in run.get("artifacts") or ():
            if art.get("op") == "log":
                files = []
                for f in art.get("files") or ():
                    data = (Path(files_root) / f["source"]).read_bytes()
                    self.uploads.append((f["source"], data))
                    files.append({"name": f["name"], "sha256": util.sha256_bytes(data)})
                self.artifacts.setdefault(art["name"], []).append({"files": files, "metadata": art.get("metadata")})
        keys, backend = [], None
        for t in table_plans:
            backend = t.get("backend")
            if backend == "ces":
                idx = [t["columns"].index(c) for c in t["score_columns"]]
                if any(row[i] is None for row in t["rows"] for i in idx):
                    raise ValueError("fake CES: a score cell is null")
            content = util.sha256_bytes(util.canonical_json({"columns": t["columns"], "rows": t["rows"]}))
            if self.tables.get((path, t["key"]), content) != content:
                raise RuntimeError("fake CES: 409 changed content at an existing location")
            self.tables[(path, t["key"])] = content
            self.table_logs.append((path, t["key"], backend))
            if not self.drop_markers:
                doc["summary"][t["key"]] = {"_type": "eval-table-ces" if backend == "ces" else "table-file"}
            keys.append(t["key"])
        doc["summary"].update(copy.deepcopy(dict(run.get("summary") or {})))
        if self.fail_after:
            raise RuntimeError("fake wandb: failed after sending")
        return {"run_path": path, "url": f"https://wandb.ai/{path}", "table_keys": keys, "backend": backend,
                "base_url": "https://api.wandb.ai", "problems": []}

    def confirm(self, run_path: str, table_keys: Sequence[str], artifact_names: Sequence[str]) -> list[str]:
        self.calls.append(("confirm", run_path))
        doc = self.runs.get(run_path)
        if doc is None:
            return ["run_not_found"]
        problems = [f"table_marker_missing:{k}" for k in table_keys if k not in doc["summary"]]
        problems += [f"artifact_missing:{n}" for n in artifact_names if n not in self.artifacts]
        return problems


# ---------------------------------------------------------------- fake SDK modules for the adapters

class _Recorder:
    def __init__(self, sdk: "FakeSdk", kind: str, kwargs: Mapping) -> None:
        self._sdk, self.kind, self.kwargs = sdk, kind, dict(kwargs)
        self.attributes: dict = {}
        self.errors: list[BaseException] = []
        self.usage = None
        self.trace_id: int | None = None

    def __enter__(self):
        self._sdk.log.append(("enter", self.kind, self.kwargs))
        self._sdk.stack.append(self)
        if self.kind == "Turn":
            self._sdk.trace_counter += 1
            self.trace_id = int(hashlib.sha256(f"trace{self._sdk.trace_counter}".encode()).hexdigest()[:32], 16)
        else:
            turn = next((r for r in reversed(self._sdk.stack[:-1]) if r.kind == "Turn"), None)
            self.trace_id = turn.trace_id if turn is not None else None
        return self

    def __exit__(self, *exc) -> None:
        self._sdk.stack.pop()
        self._sdk.log.append(("exit", self.kind, self.kwargs))
        self._sdk.ended.append(self)

    def set_attributes(self, attributes: Mapping) -> None:
        self._sdk.log.append(("set_attributes", self.kind, dict(attributes)))
        self.attributes.update(attributes)

    def record_error(self, error: BaseException) -> None:
        self._sdk.log.append(("record_error", self.kind, type(error).__name__, str(error)))
        self.errors.append(error)


class FakeSdk:
    """The fake SDK modules and the fake server behind them."""

    MODULES = ("wandb", "coreweave", "coreweave.forge", "coreweave.forge.agentlens",
               "coreweave.forge.agentlens.tracing", "opentelemetry", "opentelemetry.trace")
    ENV_NAMES = ("NETRC", "WANDB_DIR", "WANDB_CACHE_DIR", "WANDB_DATA_DIR", "WANDB_CONSOLE", "WANDB_DISABLE_GIT",
                 "WANDB_DISABLE_WEAVE", "WANDB_CONFIG_DIR", "OTEL_RESOURCE_ATTRIBUTES", "OTEL_BSP_MAX_EXPORT_BATCH_SIZE",
                 "OTEL_EXPORTER_OTLP_TRACES_COMPRESSION", "OTEL_SDK_DISABLED", "OTEL_EXPORTER_OTLP_HEADERS", "HOME")

    def __init__(self, *, weave_importable: bool = False, ces_features: Sequence[bool] = (True,),
                 projects: Mapping | None = None, flush_result: bool = False, zero_trace: bool = False) -> None:
        self.weave_importable = weave_importable
        self.zero_trace = zero_trace                       # the no-op tracer of OTEL_SDK_DISABLED: trace id 0
        self.ces_features = list(ces_features)
        self.projects = dict(projects) if projects is not None else {DEFAULT_PROJECT: {
            "name": DEFAULT_PROJECT[1], "access": "PRIVATE", "readOnly": False, "weavePermissions": {"canWrite": True}}}
        self.flush_result = flush_result
        self.log: list[tuple] = []
        self.imports: dict[str, dict] = {}                # module name -> the ENV_NAMES values when it was imported
        self.stack: list[_Recorder] = []
        self.ended: list[_Recorder] = []
        self.trace_counter = 0
        self.runs: dict[str, dict] = {}                    # run path -> {"summary", "history", ...}
        self.artifacts: dict[str, dict] = {}               # "<entity>/<project>/<name>:<alias>" -> {"files"}
        self.netrc_touched: list[str] = []
        self.requests: list[dict] = []                     # read-back HTTP requests: url, headers, body, timeout
        self._lock = threading.Lock()

    # ------------------------------------------------------------ module factories

    def _build(self, name: str) -> types.ModuleType:
        mod = types.ModuleType(name)
        if name in ("coreweave", "coreweave.forge", "coreweave.forge.agentlens", "opentelemetry"):
            mod.__path__ = []                              # a package
        if name == "coreweave.forge.agentlens.tracing":
            self._tracing(mod)
        elif name == "opentelemetry.trace":
            mod.get_current_span = self._current_span
        elif name == "wandb":
            self._wandb(mod)
        return mod

    def _current_span(self):
        sdk = self
        turn = next((r for r in reversed(sdk.stack) if r.kind == "Turn"), None)
        trace_id = turn.trace_id if turn is not None and not sdk.zero_trace else 0
        ctx = types.SimpleNamespace(trace_id=trace_id)
        return types.SimpleNamespace(get_span_context=lambda: ctx)

    def _tracing(self, mod: types.ModuleType) -> None:
        sdk = self

        class Client:
            def force_flush(self):
                sdk.log.append(("force_flush",))
                return sdk.flush_result

            def shutdown(self):
                sdk.log.append(("shutdown",))

        def init(project, *, api_key, base_url, service_name, autopatch_integrations):
            sdk.log.append(("tracing.init", {"project": project, "api_key": api_key, "base_url": base_url,
                                             "service_name": service_name,
                                             "autopatch_integrations": autopatch_integrations}))
            return Client()

        def span_class(kind, fields):
            def factory(**kwargs):
                unknown = set(kwargs) - set(fields)
                if unknown:
                    raise TypeError(f"{kind}() got unexpected keyword arguments {sorted(unknown)}")
                rec = _Recorder(sdk, kind, kwargs)
                sdk.log.append(("new", kind, dict(kwargs)))
                return rec
            factory.__name__ = kind
            return factory

        mod.init = init
        mod.Conversation = span_class("Conversation", ("conversation_id", "conversation_name", "attributes"))
        mod.Turn = span_class("Turn", ("agent_name", "agent_version", "messages", "started_at", "ended_at"))
        mod.LLM = span_class("LLM", ("model", "provider_name", "response_model", "response_id", "input_messages",
                                     "system_instructions", "output_messages", "request_max_tokens",
                                     "request_temperature", "finish_reasons", "started_at", "ended_at"))
        mod.Tool = span_class("Tool", ("name", "tool_call_id", "arguments", "result", "started_at", "ended_at"))
        mod.SubAgent = span_class("SubAgent", ("name", "model", "started_at", "ended_at"))
        def message(*, role, content="", tool_call_id="", tool_name="", parts=()):
            if role not in ("user", "assistant", "system", "tool"):        # Message.role is a Literal (types.py:156)
                raise ValueError(f"Message role {role!r} is not one of user, assistant, system, tool")
            return types.SimpleNamespace(role=role, parts=list(parts), tool_call_id=tool_call_id)
        mod.Message = message
        mod.TextPart = lambda *, content: types.SimpleNamespace(type="text", content=content)
        mod.ReasoningPart = lambda *, content: types.SimpleNamespace(type="reasoning", content=content)

        def usage(*, input_tokens=None, output_tokens=None, total_tokens=None, cache_read_input_tokens=None,
                  reasoning_tokens=None, cache_creation_input_tokens=None):
            return {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": total_tokens,
                    "cache_read_input_tokens": cache_read_input_tokens, "reasoning_tokens": reasoning_tokens,
                    "cache_creation_input_tokens": cache_creation_input_tokens}
        mod.Usage = usage

    def _netrc_path(self) -> Path:
        return Path(os.environ.get("NETRC") or (Path(os.environ.get("HOME", "/nonexistent")) / ".netrc"))

    def _wandb(self, mod: types.ModuleType) -> None:
        sdk = self

        def login(*args, **kwargs):
            sdk.log.append(("wandb.login",))
            raise AssertionError("wandb.login must never be called")

        class Settings:
            FIELDS = ("api_key", "base_url", "disable_git", "save_code", "console", "x_disable_meta", "x_disable_stats",
                      "x_disable_machine_info", "x_save_requirements")

            def __init__(self, **kwargs):
                unknown = set(kwargs) - set(self.FIELDS)
                if unknown:
                    raise TypeError(f"Settings() got unexpected keyword arguments {sorted(unknown)}")
                self.kwargs = dict(kwargs)
                sdk.log.append(("wandb.Settings", dict(kwargs)))

        class Summary(dict):
            pass

        class Handle:
            def __init__(self, artifact):
                self.artifact = artifact

            def wait(self):
                sdk.log.append(("artifact.wait", self.artifact.name))
                return self

        class Artifact:
            def __init__(self, name, *, type, metadata=None):
                self.name, self.type, self.metadata, self.files = name, type, dict(metadata or {}), []
                sdk.log.append(("wandb.Artifact", {"name": name, "type": type, "metadata": self.metadata}))

            def add_file(self, local_path, *, name=None):
                data = Path(local_path).read_bytes()
                self.files.append({"name": name, "bytes": data, "local_path": str(local_path)})
                sdk.log.append(("artifact.add_file", {"local_path": str(local_path), "name": name}))

        class EvalTable:
            def __init__(self, *, columns, data, input_columns, output_columns, score_columns, backend,
                         unsupported_media_mode):
                idx = [columns.index(c) for c in score_columns]
                if any(row[i] is None for row in data for i in idx):
                    raise ValueError("fake CES: null score")
                self.kwargs = {"columns": columns, "data": data, "input_columns": input_columns,
                               "output_columns": output_columns, "score_columns": score_columns, "backend": backend,
                               "unsupported_media_mode": unsupported_media_mode}
                self.marker = "eval-table-ces"
                sdk.log.append(("wandb.EvalTable", dict(self.kwargs)))

        class Table:
            def __init__(self, *, columns, data):
                self.kwargs, self.marker = {"columns": columns, "data": data}, "table-file"
                sdk.log.append(("wandb.Table", dict(self.kwargs)))

        class Run:
            def __init__(self, kwargs):
                self.kwargs = kwargs
                self.path = f"{kwargs['entity']}/{kwargs['project']}/{kwargs['id']}"
                self.url = f"https://wandb.ai/{self.path}"
                self.doc = sdk.runs.setdefault(self.path, {"summary": Summary(), "history": [], "metrics": []})
                self.summary = self.doc["summary"]

            def define_metric(self, name, step_metric=None, step_sync=None, hidden=None, summary=None, overwrite=None):
                extra = {k: v for k, v in (("step_sync", step_sync), ("hidden", hidden), ("summary", summary),
                                           ("overwrite", overwrite)) if v is not None}
                sdk.log.append(("run.define_metric", name, step_metric, extra))
                self.doc["metrics"].append((name, step_metric))

            def log(self, data, step=None, commit=None):
                sdk.log.append(("run.log", sorted(data), step, commit))
                plain = {}
                for k, v in data.items():
                    if isinstance(v, (EvalTable, Table)):
                        self.summary[k] = {"_type": v.marker}
                    else:
                        plain[k] = v
                if plain:
                    self.doc["history"].append(plain)

            def log_artifact(self, artifact_or_path, *, aliases=None):
                artifact = artifact_or_path
                sdk.log.append(("run.log_artifact", artifact.name, list(aliases or [])))
                project = f"{self.kwargs['entity']}/{self.kwargs['project']}"
                for alias in list(aliases or []) + ["latest"]:
                    sdk.artifacts[f"{project}/{artifact.name}:{alias}"] = {"files": artifact.files,
                                                                           "metadata": artifact.metadata}
                return Handle(artifact)

            def use_artifact(self, artifact_or_name):
                ref = artifact_or_name
                sdk.log.append(("run.use_artifact", ref))
                project = f"{self.kwargs['entity']}/{self.kwargs['project']}"
                if f"{project}/{ref}" not in sdk.artifacts:
                    raise RuntimeError(f"artifact {ref} not found")

            def finish(self):
                sdk.log.append(("run.finish", self.path))

        def init(*, entity, project, id, group, job_type, name, tags, config, dir, resume, settings):
            kwargs = {"entity": entity, "project": project, "id": id, "group": group, "job_type": job_type,
                      "name": name, "tags": tags, "config": config, "dir": dir, "resume": resume, "settings": settings}
            sdk.log.append(("wandb.init", dict(kwargs)))
            netrc = sdk._netrc_path()                       # what the real SDK consults for a credential
            sdk.netrc_touched.append(str(netrc))
            if not (isinstance(settings, Settings) and settings.kwargs.get("api_key")):
                with open(netrc, "a", encoding="utf-8") as fh:   # emulates a key that is persisted
                    fh.write("machine api.wandb.ai login user password fake-persisted\n")
            return Run(kwargs)

        class ServiceApi:                          # wandb.apis.public.service_api.ServiceApi (Api._service_api)
            def execute_graphql(self, query, variables=None, timeout=None):
                sdk.log.append(("api.execute_graphql", query, dict(variables or {})))
                if "project(" in query:
                    v = variables or {}
                    return {"project": copy.deepcopy(sdk.projects.get((v.get("entity"), v.get("name"))))}
                raise AssertionError(f"unexpected GraphQL document {query[:60]!r}")

            def feature_enabled(self, feature, *, timeout=10):
                sdk.log.append(("api.feature_enabled", feature))
                flag = sdk.ces_features.pop(0) if sdk.ces_features else False
                if isinstance(flag, BaseException):
                    raise flag
                return feature == "EVAL_TABLES_CES" and bool(flag)

        class SubDict:                             # wandb.apis.public.summary.SummarySubDict: not a Mapping
            def __init__(self, items):
                self._items = dict(items)

            def keys(self):
                return self._items.keys()

            def get(self, k, default=None):
                return self._items.get(k, default)

            def __getitem__(self, k):
                return self._items[k]

        class PublicRun:
            def __init__(self, summary):
                self.summary_metrics = copy.deepcopy(dict(summary))
                self.summary = {k: SubDict(v) if isinstance(v, dict) else v for k, v in summary.items()}

        class Api:
            def __init__(self, *, overrides=None, timeout=None, api_key=None):
                sdk.log.append(("wandb.Api", {"overrides": overrides, "api_key": api_key}))
                self._service_api = ServiceApi()

            def run(self, path=""):
                sdk.log.append(("api.run", path))
                if path not in sdk.runs:
                    raise RuntimeError("run not found")
                return PublicRun(sdk.runs[path]["summary"])

            def artifact(self, name, type=None):
                ref = name
                sdk.log.append(("api.artifact", ref))
                if ref not in sdk.artifacts:
                    raise RuntimeError("artifact not found")
                return sdk.artifacts[ref]

        mod.login, mod.Settings, mod.init, mod.Api = login, Settings, init, Api
        mod.Artifact, mod.EvalTable, mod.Table = Artifact, EvalTable, Table

    # ------------------------------------------------------------ the read-back endpoint

    def rows_for(self, trace_hex: str) -> list[dict]:
        """The rows the trace store would hold for one trace: every ended span of that trace, in the H2 row shape."""
        def iso(moment) -> str:
            return moment.astimezone(__import__("datetime").timezone.utc).replace(tzinfo=None).isoformat()

        def messages(items) -> list:
            return [{"role": m.role, "content": json.dumps([{"type": p.type, "content": p.content} for p in m.parts])}
                    for m in items or ()]

        rows = []
        for rec in self.ended:
            if rec.kind not in ("Turn", "LLM", "Tool", "SubAgent") or rec.trace_id is None:
                continue
            if format(rec.trace_id, "032x") != trace_hex:
                continue
            k = rec.kwargs
            row = {"started_at": iso(k["started_at"]), "ended_at": iso(k["ended_at"]),
                   "status_code": "ERROR" if rec.errors else "OK", "attributes": dict(rec.attributes)}
            if rec.kind == "LLM":
                u = rec.usage or {}
                row.update({"operation_name": "chat", "request_model": k["model"],
                            "response_model": k["response_model"] or None, "response_id": k["response_id"],
                            "input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens"),
                            "cache_read_input_tokens": u.get("cache_read_input_tokens"),
                            "reasoning_tokens": u.get("reasoning_tokens"), "request_max_tokens": k["request_max_tokens"],
                            "finish_reasons": k["finish_reasons"], "system_instructions": k["system_instructions"],
                            "input_messages": messages(k["input_messages"]),
                            "output_messages": messages(k["output_messages"])})
            elif rec.kind == "Tool":
                row.update({"operation_name": "execute_tool", "tool_call_id": k["tool_call_id"], "tool_name": k["name"],
                            "tool_call_arguments": "" if k["arguments"] == "{}" else k["arguments"],
                            "tool_call_result": k["result"]})
            else:
                row.update({"operation_name": "invoke_agent", "agent_name": k.get("agent_name") or k.get("name")})
            rows.append(row)
        return rows

    def opener(self, *, page_limit: int | None = None) -> Callable:
        """An urllib opener stand-in for the read-back endpoint, paging by offset as the service does."""
        sdk = self

        class _Resp:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self) -> bytes:
                return self._data

            def __enter__(self):
                return self

            def __exit__(self, *exc) -> None:
                return None

        def open_(req, timeout=None):
            body = json.loads(req.data.decode("utf-8"))
            sdk.requests.append({"url": req.full_url, "method": req.get_method(), "headers": dict(req.header_items()),
                                 "body": body, "timeout": timeout})
            trace = body["query"]["$expr"]["$eq"][1]["$literal"]
            rows = sdk.rows_for(trace)
            n = min(body["limit"], page_limit or body["limit"])
            page = rows[body["offset"]:body["offset"] + n]
            return _Resp(json.dumps({"spans": page, "total_count": len(rows)}).encode("utf-8"))

        return open_

    # ------------------------------------------------------------ installation

    def calls(self, name: str) -> list[tuple]:
        return [entry for entry in self.log if entry[0] == name]

    @contextlib.contextmanager
    def installed(self) -> Iterator["FakeSdk"]:
        names = self.MODULES + ("weave",)
        saved = {n: sys.modules.pop(n) for n in names if n in sys.modules}
        finder = _Finder(self)
        sys.meta_path.insert(0, finder)
        try:
            yield self
        finally:
            sys.meta_path.remove(finder)
            for n in names:
                sys.modules.pop(n, None)
            sys.modules.update(saved)


class _Finder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def __init__(self, sdk: FakeSdk) -> None:
        self.sdk = sdk

    def find_spec(self, fullname, path=None, target=None):
        if fullname in FakeSdk.MODULES or (fullname == "weave" and self.sdk.weave_importable):
            is_pkg = fullname in ("coreweave", "coreweave.forge", "coreweave.forge.agentlens", "opentelemetry")
            return importlib.util.spec_from_loader(fullname, self, is_package=is_pkg)
        return None

    def create_module(self, spec):
        self.sdk.imports[spec.name] = {n: os.environ.get(n) for n in FakeSdk.ENV_NAMES}
        if spec.name == "weave":
            return types.ModuleType("weave")
        return self.sdk._build(spec.name)

    def exec_module(self, module) -> None:
        return None
