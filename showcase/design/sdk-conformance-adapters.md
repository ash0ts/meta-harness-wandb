# SDK conformance: Forge telemetry adapters

Scope: `mhforge/telemetry/adapter_agentlens.py`, `mhforge/telemetry/adapter_wandb.py`, `mhforge/telemetry/spawn.py`,
`procenv.exporter_env`, checked against the installed SDKs in `.venv` (wandb 0.31.0,
coreweave 0.1.0b0, cwevals 0.0.1b1 as `coreweave_evaluations`, OpenTelemetry 1.45.2, pydantic 2.14.3). `SP/` =
`.venv/lib/python3.13/site-packages`; `AL/` = `SP/coreweave/forge/agentlens/tracing/`;
`WB/` = `SP/wandb/`; `OT/` = `SP/opentelemetry/`. Reference: `sdk-reference.md`; decisions: `lead-decisions.md` LD7-LD9.

Introspection ran isolated (`env -i`, `-I`, empty HOME and cwd, `WANDB_MODE=disabled`). Nothing logged in, started a run,
opened a tracing session, created a sandbox or sent a request.

Checked mechanically by `tests/test_sdk_signatures.py` (11 cases): the adapters' own `_sdk_*` functions and the sinks'
`emit`, `log_run`, `feature_ces`, `project_access` and `confirm` run against the real classes (pydantic models, `Settings`,
`EvalTable`, `Table`) or against stand-ins that `inspect.signature(...).bind` every call to the real definition and
check that each method, property and instance attribute exists. Every span, run and table plan of the test campaign is
built with the real classes. Result under the SDK venv (isolated): `Ran 11 tests ... OK`. Under the system python3 all
11 are skipped.

## 1. Every SDK use

Verdicts: **ok** = matches the installed source; **fixed** = mismatch found and changed here; **live** = only a live call can settle it.

### Agent Lens (`adapter_agentlens.py`)

| Our call | SDK definition | Verdict |
|---|---|---|
| `from coreweave.forge.agentlens import tracing`; `from opentelemetry import trace` | `AL/__init__.py:10-45`; `OT/trace/__init__.py` | ok |
| `except ImportError` -> `AdapterUnavailable` | import failure only | ok |
| `tracing.init(project, api_key=key, base_url="https://trace.wandb.ai", service_name=..., autopatch_integrations=False)` | `AL/tracing.py:64` `init(project_name, *, api_key, base_url, service_name, autopatch_integrations)`; key = `api_key or WANDB_API_KEY or netrc` (:85), so a passed key skips `~/.netrc`; endpoint `base_url + /agents/otel/v1/traces` (:89-90) | ok (bound in test); endpoint acceptance is live |
| return value `client.force_flush()`, result ignored | `TracingSession.force_flush(timeout_millis=30000) -> bool` (:46); True even when exports failed (`OT/sdk/_shared_internal/__init__.py:186-189, 232-237`) | ok: never treated as proof; the read-back decides |
| `client.shutdown()` in `finally` | `TracingSession.shutdown()` (:51); flushes the batch processor | ok |
| `tracing.Conversation(conversation_id=, conversation_name=, attributes=)`; `with conv:` | `AL/conversation.py:1174`; no span; `__enter__` sets the current-conversation contextvar (:1262), so spans started inside bind it and get `gen_ai.conversation.id` and the attributes (:200-218) | ok |
| `tracing.Turn(agent_name=, agent_version=, messages=[], started_at=, ended_at=)`; `with t:` | `:918`; `start()` uses `started_at` as OTel start and a new trace (:1141-1156); `end()` keeps the set `ended_at` (:1114-1130) | ok |
| `_dt_utc` -> tz-aware UTC `datetime` | `_to_ns(dt) = dt.timestamp()` (:1456); naive = local time | ok |
| `otel_trace.get_current_span().get_span_context().trace_id` inside `with t:` | the turn span is attached as current (:213-214); a no-op tracer gives trace id 0 | ok; **fixed**: id 0 now raises before `on_trace` |
| `tracing.LLM(model=, provider_name=, response_model=, response_id=, input_messages=, system_instructions=, output_messages=, request_max_tokens=, request_temperature=, finish_reasons=, started_at=, ended_at=)` | `:420` (all fields exist; keyword-only pydantic signature) | ok |
| `span.usage = tracing.Usage(**fields)` (None fields dropped) | `AL/types.py:225` `input_tokens, output_tokens, total_tokens, reasoning_tokens, cache_creation_input_tokens, cache_read_input_tokens`; ints, `total_tokens` may be None | ok |
| `tracing.Tool(name=, tool_call_id=, arguments=, result=, started_at=, ended_at=)` | `:328`; `arguments`/`result` are `JSONString`, a str passes through (`types.py:18-34`); `end()` uses `ended_at` and computes `duration_ms` (:377-393) | ok |
| `tracing.SubAgent(name=, model=, started_at=, ended_at=)`; `with` (set_current True, strictly nested) | `:667`; `start(*, set_current=True)` (:875); ends with its own `end()`/`with` (no `end_subagent`) | ok |
| `tracing.Message(role=, parts=[...])` | `types.py:156`; `role: Literal["user","assistant","system","tool"]` | ok; **fixed**: a plan role outside the Literal raised after the first trace started; now refused before `init` |
| `tracing.TextPart(content=)`, `tracing.ReasoningPart(content=)` | `types.py:78, 85` | ok |
| `span.set_attributes(dict)` between start and end | `conversation.py:282`; outside start..end a logged no-op | ok |
| `span.record_error(exc)` | `:270`; `error.type` = `__qualname__`, prefixed with `__module__` unless `"builtins"` | **fixed**: stored type was `mhforge.telemetry.adapter_agentlens.<Type>`; now the plan's type |
| pydantic models ignore unknown keywords (no `extra="forbid"` in `AL/`) | a misspelt keyword would be dropped silently at runtime | ok: now caught by `signature().bind` in the test |
| env `OTEL_BSP_MAX_EXPORT_BATCH_SIZE`, `OTEL_EXPORTER_OTLP_TRACES_COMPRESSION=gzip` set before `init` | read when `init` builds the processor and exporter (`OT/sdk/trace/export/__init__.py:163-180`; trace_exporter :140-142) | ok |
| env: only `OTEL_RESOURCE_ATTRIBUTES` was removed | `OTEL_EXPORTER_OTLP[_TRACES]_HEADERS` merged into the request; `OTEL_SDK_DISABLED`, `OTEL_TRACES_SAMPLER=always_off` drop all spans; `..._CREDENTIAL_PROVIDER`, `..._CERTIFICATE`, `OTEL_[SPAN_]ATTRIBUTE_COUNT_LIMIT` (reference 5) | **fixed**: every `OTEL_*` removed, then the two set |
| requests netrc for the trace host (OTLP transport, `trust_env`) | `SP/requests/sessions.py:537-538`, `SP/requests/utils.py:239-243` | **fixed**: `NETRC=/dev/null` when unset (the child already names the empty state file) |
| 128 attributes per span | `SpanLimits()` 128; a full span evicts its **oldest** attribute (`OT/attributes/__init__.py:202-212`), i.e. the conversation and plan attributes set at start | **fixed**: refused before `init` when conversation + span attributes + 31 SDK attributes (24 `gen_ai.*` on a chat span, 6 `weave.*`, `error.type`) exceed 128. `check_attributes` allows 64 + 64, so 128 + 31 was reachable; test-campaign plans use far fewer |
| 64 MiB request | `_DEFAULT_MAX_REQUEST_SIZE` (`OT/exporter/otlp/proto/http/_common/__init__.py:50`), checked before compression, batch dropped whole (trace_exporter :186-199) | **fixed**: refused before `init` when `byte_estimate_factor x` the `export_batch_size` largest `span_bytes` exceed 64 MiB (a `full` content rule allows unbounded text) |
| batch queue 2048 vs plan | `max_spans_per_conversation` <= 199 and a flush after each conversation | ok |
| HTTP read-back (urllib, Basic `api:<key>`, no proxy, no redirect) | not an SDK call; host equal to the trace host | ok; endpoint shape is live |

### W&B and EvalTables (`adapter_wandb.py`)

| Our call | SDK definition | Verdict |
|---|---|---|
| `import wandb`; `except ImportError` | | ok |
| `from wandb_gql import gql` | **not installed**; wandb 0.31.0 no longer ships it | **fixed** (blocker): removed |
| `api.client.execute(gql(q), variable_values=)` | `wandb.Api` has **no `client`** (`WB/apis/public/api.py:113-190`); GraphQL is `Api._service_api.execute_graphql(query, variables=None, timeout=None, *, ...)` (`WB/apis/public/service_api.py:135`) | **fixed** (blocker): `_sdk_graphql(api, query, variables)` uses `_service_api.execute_graphql` |
| CES probe: GraphQL `serverInfo { features }` | the SDK's own gate is `ServiceApi.feature_enabled(pb.ServerFeature.EVAL_TABLES_CES)` in `EvalTable.bind_to_run` (`WB/sdk/data_types/eval_table/eval_table.py:202-209`; `service_api.py:309`, False on error or timeout) | **fixed**: `_sdk_feature_enabled(api, "EVAL_TABLES_CES")`, still tried three times |
| `wandb.Settings(api_key=, disable_git=True, save_code=False, console="off", x_disable_meta=True, x_disable_stats=True, x_disable_machine_info=True, x_save_requirements=False)` | `WB/sdk/wandb_settings.py:73` (`extra="forbid"`), fields :144, :168, :263, :511, :622, :625, :634, :868 | ok (real object built in test) |
| `base_url` not passed | default `https://api.forge.coreweave.com` (:159), but a global or workspace settings file or `WANDB_BASE_URL` can move it | **fixed**: `Settings(base_url=DEFAULT_BASE_URL)` and `Api(overrides={"base_url": ...})` |
| `wandb.init(entity=, project=, id=, group=, job_type=, name=, tags=, config=, dir=, resume="allow"/None, settings=)` | `WB/sdk/wandb_init.py:1181`; `settings` applied last (`wandb_setup.py:248`); key used as-is, not persisted | ok (bound); behaviour of a fixed `id` with `resume=None` on an existing run is live |
| `run.define_metric(name[, step_metric=])` | `WB/sdk/wandb_run.py:2818`; names `iteration`, `search/*` (glob only at the end) | ok |
| `run.log(dict)` with no step | `:1667` `log(data, step=None, commit=None)` | ok |
| `wandb.Artifact(name, type=, metadata=)` | `WB/sdk/artifacts/artifact.py:172`; names <= 128 chars, no `/` (`_validators.py:38-40`) | ok (bound); names come from plans |
| `artifact.add_file(str(path), name=)` | `:1533` | ok |
| `run.log_artifact(artifact, aliases=[...]).wait()` | `wandb_run.py:3179` returns `Artifact`; `Artifact.wait(timeout=None)` (`artifact.py:1277`) | ok |
| `run.use_artifact("name:alias")` | `:3055` `use_artifact(artifact_or_name, type=None, aliases=None, use_as=None)`; a string is resolved, not logged | ok |
| `wandb.EvalTable(columns=, data=, input_columns=, output_columns=, score_columns=, backend="ces", unsupported_media_mode="raise")` | `eval_table.py:52`; `"ces"` needs `allow_mixed_types=False` (default) and `log_mode="IMMUTABLE"` (default) | ok (real object built for every table plan) |
| CES limits at `run.log`: 1..10,000 rows, <= 10,000 input+output and <= 256 score columns, names 1..256/512, primitive cells, int+float only mix, no all-null column (`_writer_ces.py:52-68, 326-410, 567-680`) | `plan_tables.check_table` enforces the same; the test applies the writer's own `_validate_name`, `_normalize_primitive`, `_merge_type` and constants to every table plan | ok |
| no offline run | `bind_to_run` refuses offline (`eval_table.py:196-200`); `WANDB_MODE` is refused | ok |
| without weave | default backend `"weave"` needs it (`_writer_weave.py:130-133`); we always pass `"ces"`, and refuse when weave is importable | ok |
| `wandb.Table(columns=, data=)` | `WB/sdk/data_types/table.py:228` | ok |
| `run.summary.update(dict)` | `Run.summary` assigned in `Run._init` (`wandb_run.py:534`); `Summary.update(d)` (`wandb_summary.py:58`) | ok |
| `run.url`, `run.settings.base_url` | properties `wandb_run.py:1119, 742` | ok |
| `run.finish()` | `:2262` | ok |
| `wandb.Api(api_key=key)` | `api.py:113` `Api(overrides=None, timeout=None, api_key=None)`; verifies the key over the network in the constructor (:188-189) | ok; now with `overrides`; acceptance is live |
| `dict(api.run(path).summary)` then `isinstance(v, Mapping)` | `api.run(path="")` (:1266); public `Run.summary` (`runs.py:1612`) decodes nested dicts as `SummarySubDict` (`apis/public/summary.py:276-293`), **not a Mapping** | **fixed** (major): every table reported `table_marker_missing`; now `summary_metrics` (`runs.py:1649`, plain dict) |
| `api.artifact(ref)`; any exception = missing | `api.py:1715` `artifact(name, type=None)`; `CommError` when not found | ok (a transport error also reads as missing: conservative) |
| `EvalTable` CES credential and host | run `settings.api_key`; host from `CES_BASE_URL` else derived (`_writer_ces.py:123-131, 719-751`) | ok; `CES_BASE_URL` refused |
| env set: `WANDB_DISABLE_WEAVE`, `NETRC` (empty state file), `WANDB_DIR`, `WANDB_CACHE_DIR`, `WANDB_DATA_DIR`, `WANDB_CONSOLE`, `WANDB_DISABLE_GIT` | env mapping (`wandb_settings.py:1975-2016`), `WB/env.py:74-77` | ok |
| env refused: `WANDB_BASE_URL`, `CES_BASE_URL`, `WANDB_APP_URL`, `WANDB_MODE` | | ok; **fixed**: now also `CES_CUSTOM_HEADERS` and any other `WANDB_*`/`CES_*` |

## 2. Mismatches and what changed

1. **Blocker** `feature_ces` / `project_access`: `wandb_gql` is not installed and `wandb.Api` has no `client` in 0.31.0, so
   both always raised `AdapterUnavailable`. Now `Api._service_api.execute_graphql(query, variables=)` and
   `Api._service_api.feature_enabled("EVAL_TABLES_CES")` (the check `EvalTable.bind_to_run` makes). `_import_gql` removed;
   `_sdk_graphql` lost its `gql` parameter (module-private; no other module calls it).
2. **Major** `confirm`: public-API `Run.summary` values are `SummarySubDict`, not `Mapping`, so every table marker was
   "missing". Now `summary_metrics`.
3. **Major** OTel environment: only `OTEL_RESOURCE_ATTRIBUTES` was removed; `OTEL_SDK_DISABLED`/`OTEL_TRACES_SAMPLER` silently
   disable tracing, `OTEL_EXPORTER_OTLP_HEADERS` adds headers. Every `OTEL_*` is removed now, and a zero trace id (no-op
   tracer) raises before any trace is reported, so the receipt says `failed`.
4. **Major** plan the SDK rejects or truncates: a `Message.role` outside the Literal or any field pydantic rejects raised
   mid-emit (partial trace); attributes past 128 evict the plan attributes; a batch over 64 MiB is dropped whole. Now
   `_check_plan` builds every SDK object and checks both limits before `tracing.init`.
5. **Major** LD9 export child: HOME was the caller's HOME (Agent Lens and requests read `$HOME/.netrc`, wandb
   `~/.config/wandb/settings`) and cwd was the package root. Now each child gets a new `STATE/export/homes/<unit>.<rand>`
   (mkdtemp, 0700) as `HOME`, `WANDB_CONFIG_DIR` and cwd, removed afterwards when still empty (kept, never deleted,
   when something wrote into it). `exporter_env` sets `WANDB_CONFIG_DIR = HOME` and refuses `home == base["HOME"]`.
6. **Major** LD9 in-process: the W&B adapter now refuses any `WANDB_*`/`CES_*` name it does not set itself
   (`WANDB_SERVICE` allowed only once wandb is imported, since wandb writes it), refuses `WANDB_CONFIG_DIR` outside STATE
   or holding `settings`, sets it to `STATE/export/wandb-config` (0700) when unset, refuses `<cwd>/wandb/settings` and
   `<cwd>/.wandb/settings`, and pins `base_url` in `Settings` and `Api`.
7. **Minor** `record_error`: `error.type` carried the adapter's module prefix; the synthetic class now has
   `__module__ = "builtins"`.
8. **Minor** Agent Lens netrc via requests: `NETRC=/dev/null` when unset.

Credential paths (reference 2.3, 5) for the export child, after the change:

| Path | Closed by |
|---|---|
| `~/.netrc` read by Agent Lens `_netrc_api_key` | explicit `api_key`; and HOME is an empty state dir |
| netrc read by requests for the trace host | `NETRC` = empty state file (child) or `/dev/null` |
| wandb netrc incl. the `api.wandb.ai` fall-back for Forge (`wbnetrc.py:80-81`) | explicit key in `Settings`/`Api`; `NETRC` = empty file; HOME empty |
| wandb global settings file | `WANDB_CONFIG_DIR` = empty state dir (child: its HOME) |
| wandb workspace settings `<cwd>/wandb/settings` | child cwd = its new empty home; in-process: refused |
| `WANDB_<FIELD>` / `WANDB__<X>` overrides | not carried by `exporter_env`; refused in-process |
| `OTEL_*` headers / disable | not carried; removed in-process |
| `CES_CUSTOM_HEADERS`, `CES_BASE_URL` | not carried; refused in-process |
| `CWSANDBOX_*` | not carried (the adapters do not import cwsandbox) |
| `wandb.Api()` without key (Reports) | not used by these adapters; LD9 for the publisher is another owner's |

## 3. What only a live call can settle

- Whether `https://trace.wandb.ai/agents/otel/v1/traces` accepts a Forge key and `entity/project`; whether the read-back
  endpoint returns the stored spans; server-side span or request size limits.
- Whether `api._service_api.execute_graphql` accepts `PROJECT_QUERY` (fields `access`, `readOnly`,
  `weavePermissions { canWrite }`) on Forge, and whether `EVAL_TABLES_CES` is enabled for the account. `_service_api` is
  a private attribute: a wandb upgrade can move it (the signature test will then fail).
- `wandb.Api(api_key=)` key verification, `wandb.init` with a fixed `id` and `resume=None` on a rerun, artifact upload
  and `wait()`, the CES write inside `run.log`, and the `eval-table-ces` value in `summary_metrics`.
- Whether wandb-core itself reads env or netrc (it inherits `os.environ`; in the child that holds no key).
- What the SDKs write into the child's HOME during a run (a home that is not empty afterwards is kept for inspection).
- Billing and quotas of the CES and trace services.
