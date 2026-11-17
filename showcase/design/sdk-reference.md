# Forge SDK reference (from installed source)

Read from `.venv/lib/python3.13/site-packages` (written `SP/` below): coreweave 0.1.0b0, wandb 0.31.0,
cwevals 0.0.1b1 (import name `coreweave_evaluations`), wandb-workspaces 0.4.13, opentelemetry-sdk 1.45.2, exporter-otlp-proto-http 1.45.2,
cwsandbox 1.27.0 (env vars only). Every statement cites `file:line`. Introspection ran isolated (`env -i`, `-I`, `WANDB_MODE=disabled`, empty HOME
and cwd); nothing logged in, started a run or sent a request. [live] marks what only a live call can settle.

Short paths: `AL/` = `SP/coreweave/forge/agentlens/tracing/`, `WB/` = `SP/wandb/`, `ET/` = `SP/wandb/sdk/data_types/eval_table/`,
`CE/` = `SP/coreweave_evaluations/`, `RP/` = `SP/wandb_workspaces/reports/v2/interface.py`, `OT/` = `SP/opentelemetry/`.

---

## 1. Agent Lens tracing (`coreweave.forge.agentlens.tracing`)

Public names: `AL/__init__.py:10-85`. There is **no** `end_tool`, `end_subagent`, `get_current_tool`, `get_current_subagent`, `log_tool`
or `log_llm`; a Tool or SubAgent is ended with its own `.end()` or a `with` block.

### 1.1 init, flush, shutdown (`AL/tracing.py`)

```python
init(project_name: str, *, api_key: str | None = None, base_url: str | None = None,
     service_name: str = "coreweave-forge-sdk", autopatch_integrations: bool = True) -> TracingSession   # :64
force_flush(timeout_millis: int = 30_000) -> bool                                                     # :148
shutdown() -> None                                                                                    # :155
TracingSession(provider: TracerProvider, endpoint: str, project_id: str)  .force_flush(ms) .shutdown()  # :37-61
```
- `project_name` must be `"entity/project"`; anything else is `ValueError` (`_parse_project_name`, :171-177). No entity lookup.
- Key: `api_key or os.environ["WANDB_API_KEY"] or _netrc_api_key()`; none found is `ValueError` (:85-87).
- `_netrc_api_key` (:191-200): stdlib `netrc.netrc()` with no path, i.e. `$HOME/.netrc` only (stdlib `netrc.py:82-83`); **`NETRC` is ignored**.
  Host looked up = netloc of `WANDB_BASE_URL` (default `api.wandb.ai`), not of the trace host. Only `FileNotFoundError` is caught (:196),
  so a malformed `~/.netrc` raises from `init`.
- Trace host (`_trace_server_url`, :180-188): `base_url` arg if truthy, else `WF_TRACE_SERVER_URL`, else `WANDB_BASE_URL` == `https://api.wandb.ai`
  (the default, :21) -> `https://trace.wandb.ai` (:20), else `f"{WANDB_BASE_URL}/traces"`.
  `base_url` is the **trace server**, not the W&B API URL.
- Endpoint = `f"{trace_url}/agents/otel/v1/traces"` (:22, :90). Headers `{"wandb-api-key": key, "project_id": "entity/project"}` (:93).
- Resource attrs `service.name`, `wandb.sdk.name="forge"`, `wandb.sdk.version`, `wandb.sdk.language`, `wandb.entity`, `wandb.project` (:96-104).
- One `BatchSpanProcessor(OTLPSpanExporter(...))` with default arguments (:107); the provider is private (no global OTel provider set).
- Re-`init`: new session becomes current; the previous one is force-flushed and kept open ("retired"); the one before that is shut down (:114-123).
- `autopatch_integrations=True` patches `anthropic`, `openai`, `google.genai` already imported and installs a `sys.meta_path` import hook for later
  imports (`AL/integrations/_autopatch.py:30-64`); a patched call opens its own Turn when none is current (`_provider_trace.py:43-68`).
- `force_flush` flushes all open sessions and returns `all(...)` (:148-152). It is **True even when exports failed**: the batch processor
  ignores the exporter result and returns True (`OT/sdk/_shared_internal/__init__.py:186-189, 232-237`); `timeout_millis` is unused there.
- `shutdown` shuts every session and removes the import hook (:155-163). Registered with `atexit` at import (:203); `TracerProvider` also
  registers its own `atexit` (`OT/sdk/trace/__init__.py:1283-1284`).
- Before `init` (or after `shutdown`) every span call is a silent no-op (`is_enabled`, :143; `conversation.py:202-203, 1657-1658, 1721-1722`).
- No network at `init`; spans leave from the background worker. Whether the server accepts a key/project pair is [live].

### 1.2 Span classes (`AL/conversation.py`) — pydantic models

All span classes share `_SpanBase` (:142): `.record_error(exc) -> Self` (:270, sets `error.type`, status ERROR, exception event, does not end),
`.set_attributes(dict) -> Self` (:282, only between start and end, else a logged warning and no-op), `.add_event(...)` (:298, deprecated).
`with obj:` = `obj.start()`; on exit records the exception, calls `.end()`, returns False (re-raises).

| Class | Fields (all default empty/None) | Methods | OTel span |
|---|---|---|---|
| `Conversation` :1174 | `conversation_id` (uuid4 if empty, :1204), `conversation_name`, `agent_name`, `model`, `agent_id`, `agent_description`, `agent_version`, `include_content=True`, `continue_parent_trace=False`, `attributes: dict` | `start_turn(*, user_message="", model="", agent_name="", agent_id="", agent_description="", agent_version="", system_instructions=None) -> Turn` :1208 (ends the open turn first); `end()` :1249; `with` sets the current-conversation contextvar :1262 | **none**; only the `gen_ai.conversation.id/name` attributes on children |
| `Turn` :918 | `agent_name, model, agent_id, agent_description, agent_version, system_instructions: list[str], messages: list[Message], output_messages: list[Message], spans: list[LLM\|Tool\|SubAgent]` (batch only), `continue_parent_trace=False`, `started_at` (now at construction, :944), `ended_at` | `user(str)` :948, `start_llm(*, model="", provider_name="", system_instructions=None)` :957, `start_tool(*, name, arguments="", tool_call_id="")` :977, `start_subagent(*, name, model="", system_instructions=None, set_current=True)` :983, `record(...)` :1033, `start()` :1141, `end()` :1114 | `invoke_agent <agent_name>`; new trace unless `continue_parent_trace` (:1152-1156) |
| `LLM` :420 | `model, provider_name, response_id, response_model, output_type, system_instructions, usage: Usage, reasoning: Reasoning, finish_reasons: list[str], input_messages, output_messages, media_attachments, request_temperature/max_tokens/top_p/top_k/frequency_penalty/presence_penalty/seed/stop_sequences/choice_count`, `started_at` (now at construction, :450), `ended_at` | `output(str)` :454, `think(str)` :459, `attach_media(*, content=b""\|str, uri="", file_id="", mime_type="", modality="")` :464, `attach_media_url(url, *, modality="")` :502, `record(*, input_messages, output_messages, media_attachments, usage, reasoning: Reasoning\|str, response_id, response_model, finish_reasons, output_type)` :515 (only non-None applied), `start()` :638, `end()` :616 | `chat <model>` |
| `Tool` :328 | `name, arguments: JSONString, result: JSONString, tool_call_id, tool_type, tool_description, tool_definitions, duration_ms` (computed at end, :384-386), `started_at` (now at `start()` if None, :399), `ended_at` | `start()` :395, `end()` :377 | `execute_tool <name>` |
| `SubAgent` :667 | `name, model, agent_id, agent_description, agent_version, system_instructions, input_messages, output_messages, tool_name, tool_call_id, tool_call_arguments: JSONString, tool_call_result: JSONString, started_at, ended_at` | `start_llm` :692, `start_tool` :714, `start_subagent(*, name, model="", set_current=True)` :724, `record(...)` :756, `start(*, set_current=True)` :875, `end()` :859 | `invoke_agent <name>` |

- Span names come from `agent_name`/`model`/`name` **at start** (`_span_name`, :111-119); `record(agent_name=...)` later changes only the attribute (:1056-1060).
- Children made by `turn.start_*` / `subagent.start_*` get an explicit parent context (:162-173); bare `LLM()/Tool()/SubAgent().start()` parent
  to the ambient OTel span (`_start_otel_span`, :205-214). Spans bind the current conversation at start if not bound (:200-201).
- Overlapping SubAgents: use `set_current=False`, else context detach corrupts the ambient stack when ended out of LIFO order (:875-886).
- Content gating: `include_content=False` on the conversation (or `log_*` arg) drops messages, system instructions, media, reasoning,
  tool arguments/results (:352-363, :577-588, :824-835, :1091-1098).
- `Conversation.attributes` are stamped on every span at start (:216-218); key collisions with `gen_ai.*`/`weave.*` are "unsupported" (:1305-1310).
- Every span also gets `weave.client_version, weave.source, weave.sys_version, weave.os_name, weave.os_version, weave.os_release`
  (`AL/capture_info.py:9-17`, `conversation.py:122-128`).

### 1.3 Module functions (`AL/conversation.py`)

```python
start_conversation(*, agent_name="", model="", conversation_id="", conversation_name="", include_content=True,
                   continue_parent_trace=False, attributes: Attributes = None) -> Conversation        # :1293, sets contextvar, no span
start_turn(*, user_message="", model="", agent_name="", system_instructions=None) -> Turn              # :1325 (via current conversation if any)
start_llm(*, model="", provider_name="", system_instructions=None) -> LLM                              # :1355 (via current turn if any)
start_tool(*, name: str, arguments: str = "", tool_call_id: str = "") -> Tool                          # :1384, parent = ambient OTel span
start_subagent(*, name: str, model="", system_instructions=None, set_current=True) -> SubAgent         # :1399, parent = ambient OTel span
end_conversation() -> None ; end_turn() -> None ; end_llm() -> None                                    # :1420, :1427, :1434 (current contextvar)
get_current_conversation() / get_current_turn() / get_current_llm()                                    # :1441-1453
log_turn(*, conversation_id: str, agent_name="", conversation_name="", model="", agent_id="", agent_description="",
         agent_version="", messages=None, output_messages=None, system_instructions=None, spans=None,
         started_at=None, ended_at=None, include_content=True, continue_parent_trace=False,
         attributes: Attributes = None) -> LogResult                                                   # :1621
log_conversation(*, turns: list[Turn], conversation_id="", conversation_name="", agent_name="", model="", agent_id="",
                 agent_description="", agent_version="", include_content=True, continue_parent_trace=False,
                 attributes: Attributes = None) -> LogResult                                           # :1692
```
- `start_llm` never infers `provider_name` (:1365-1368).
- `log_conversation` applies conversation defaults where the Turn's own value is empty, and its `continue_parent_trace` overrides every Turn (:1731-1741).

### 1.4 Types (`AL/types.py`)

| Type | Fields |
|---|---|
| `TextPart` :78 | `type="text"`, `content: str=""` |
| `ReasoningPart` :85 | `type="reasoning"`, `content: str=""` |
| `ToolCallPart` :92 | `type="tool_call"`, `id`, `name`, `arguments: JSONString` (dict/list/scalar JSON-encoded, None -> "", :18-34) |
| `ToolCallResponsePart` :109 | `type="tool_call_response"`, `id`, `response: JSONString` |
| `BlobPart` :123 / `UriPart` :132 / `FilePart` :141 | `mime_type`, `modality`, and `content` (base64) / `uri` / `file_id` |
| `MessagePart` :150 | discriminated union on `type` of the seven parts |
| `Message` :156 | `role: Literal["user","assistant","system","tool"]`, `content=""`, `tool_call_id=""`, `tool_name=""`, `parts: list[MessagePart]`; helpers `user(text)`, `system(text)`, `assistant(text="", *, tool_calls=None)`, `tool_result(call_id, output)` (:180-222) |
| `Usage` :225 | `input_tokens=0, output_tokens=0, total_tokens: int\|None=None, reasoning_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0` |
| `Reasoning` :236 | `content=""` |
| `MediaAttachment` :242 | `content, uri, file_id, modality, mime_type`; exactly one source or `ValueError` (:251-255) |
| `LogResult` :258 | `conversation_id`, `trace_ids: list[str]` (32 hex), `root_span_ids: list[str]` (16 hex), `span_count` |

Serialization (`AL/conversation_otel.py`): messages go to `gen_ai.input.messages` / `gen_ai.output.messages` as JSON strings of `{role, parts}`
(:68-130). Non-empty `parts` win over `content`; parts are dumped with `exclude_defaults=True`, so empty fields vanish (:84-88).
`reasoning` becomes a leading ReasoningPart on the last assistant output unless one exists (:146-167); `finish_reasons[0]` goes on the last output
message (:168-169). Usage zeros are omitted except `total_tokens` when not None (:306-318). Attribute names: :182-234, :237-330, :333-364.

### 1.5 Events that already happened (after-the-fact export)

- Times: every class takes `started_at` / `ended_at` (`datetime`); `start()` uses `started_at` as the OTel start (:401-402, :644-649, :891-895,
  :1147-1156) and `end()` uses `ended_at` if already set (:382, :622, :864, :1120). Converted by `dt.timestamp()` (:1456-1458): a **naive datetime is
  local time**; pass tz-aware UTC.
- `start_llm`/`start_turn`/`start_tool` factories accept no timestamps and start the span immediately; for history, construct the object with
  `started_at=`/`ended_at=` and then `with obj:` (or `.start()`/`.end()`). Nesting then follows the ambient OTel context (enter children inside the parent's `with`).
- Batch path `log_turn`/`log_conversation`: the turn span plus each `turn.spans` item as a **direct child of the turn** (:1595-1611); a SubAgent has no
  `spans` field, so batch export cannot nest below the turn. Missing turn times fall back to min/max child times, then now (:1504-1523);
  a child with no `started_at`/`ended_at` gets "now". Batch `attributes` are applied after the span's own attributes (`update`, :1581-1582, :1603-1604).
- Ids: `conversation_id` is caller-chosen. Trace and span ids come from the OTel random id generator (no `id_generator` passed, `tracing.py:95`);
  **they cannot be set**. Batch returns them in `LogResult`; streaming: `opentelemetry.trace.get_current_span().get_span_context()` inside the `with`
  (the span is attached as current, `conversation.py:213-214`).

### 1.6 Sending, errors, limits (OpenTelemetry 1.45.2)

- Batching (`OT/sdk/trace/export/__init__.py:163-180`, defaults verified by introspection): queue 2048 (`OTEL_BSP_MAX_QUEUE_SIZE`), delay 5000 ms
  (`OTEL_BSP_SCHEDULE_DELAY`), batch 512 (`OTEL_BSP_MAX_EXPORT_BATCH_SIZE`), export timeout 30000 ms (`OTEL_BSP_EXPORT_TIMEOUT`). Read once, at `init`.
- Queue full: the new span is kept and the **oldest** queued span is dropped, with a warning (`OT/sdk/_shared_internal/__init__.py:199-203`).
- Exporter (`OT/exporter/otlp/proto/http/trace_exporter/__init__.py:68-169`): protobuf over HTTP POST; compression none unless
  `OTEL_EXPORTER_OTLP[_TRACES]_COMPRESSION` (:140-142); timeout 10 s unless `OTEL_EXPORTER_OTLP[_TRACES]_TIMEOUT` (:157; `_common/__init__.py:152-163`).
- `max_request_size` 64 MiB before compression (`_common/__init__.py:50`): a larger **batch is dropped whole** and logged (trace_exporter :187-199).
- Retries (`OT/exporter/otlp/common/http.py:30-41, 178-239`): up to 6 attempts on 429/502/503/504 or connection errors, exponential backoff with
  ±20 % jitter, honours `Retry-After`, all within the one 10 s deadline. Any other status (400, 401, 403, 413, 500...) fails at once. Failures are
  logged via `logging` and the spans are gone; nothing is raised to the caller.
- Transport is a `requests.Session()` (`OT/exporter/http/transport/_requests.py:76`) with `trust_env`: `HTTPS_PROXY`, `REQUESTS_CA_BUNDLE`,
  and a `~/.netrc`/`NETRC` entry for the trace host (adds `Authorization: Basic`, `SP/requests/sessions.py:537-538`).
- Span limits (`SpanLimits()` defaults, verified): 128 attributes per span, 128 events, 128 links, attribute **value length unlimited**
  (`OT/sdk/trace/__init__.py:79-84, 590-690`); attributes past 128 are silently dropped. Env: `OTEL_ATTRIBUTE_COUNT_LIMIT`,
  `OTEL_SPAN_ATTRIBUTE_COUNT_LIMIT`, `OTEL_[SPAN_]ATTRIBUTE_VALUE_LENGTH_LIMIT`.
- The SDK itself enforces no size limit on messages or spans. Server-side per-span or per-request limits are [live].

---

## 2. W&B runs and artifacts (wandb 0.31.0)

### 2.1 Signatures (introspected; file:line of definition)

```python
wandb.init(entity=None, project=None, dir=None, id=None, name=None, notes=None, tags=None, config=None, config_exclude_keys=None,
           config_include_keys=None, allow_val_change=None, group=None, job_type=None, mode=None, force=None, reinit=None,
           resume=None, resume_from=None, fork_from=None, save_code=None, tensorboard=None, sync_tensorboard=None,
           monitor_gym=None, settings: Settings | dict | None = None) -> Run              # WB/sdk/wandb_init.py:1181
wandb.setup(settings: Settings | None = None)                                             # WB/sdk/wandb_setup.py:512
wandb.login(key=None, relogin=None, host=None, force=None, timeout=None, verify=True, referrer=None, prompt=True) -> bool  # WB/sdk/wandb_login.py:24
wandb.Api(overrides: dict | None = None, timeout: int | None = None, api_key: str | None = None)                         # WB/apis/public/api.py:113
Run.log(data: dict, step: int | None = None, commit: bool | None = None) -> None          # WB/sdk/wandb_run.py:1667
Run.define_metric(name, step_metric=None, step_sync=None, hidden=None, summary=None, overwrite=None) -> Metric  # :2818
Run.log_artifact(artifact_or_path, name=None, type=None, aliases=None, tags=None) -> Artifact                  # :3179
Run.use_artifact(artifact_or_name: str | Artifact, type=None, aliases=None, use_as=None) -> Artifact           # :3055
Run.finish(exit_code: int | None = None) -> None                                          # :2260
Artifact(name, type, description=None, metadata=None, incremental=False, use_as=None, storage_region=None,
         digest_algorithm: Literal["MD5","XXH128"] = "MD5")                               # WB/sdk/artifacts/artifact.py:172
Artifact.add_file(local_path, name=None, is_tmp=False, skip_cache=False, policy="mutable", overwrite=False)   # :1532
Artifact.add_dir(local_path, name=None, skip_cache=False, policy="mutable", merge=False)  # :1590
Artifact.add(obj: WBValue, name, overwrite=False) ; Artifact.new_file(name, mode="x", encoding=None, policy="mutable")  # :1748, :1478
Artifact.wait(timeout: int | None = None) -> Artifact                                     # :1277
```
- `Run.log`: the step must always increase; earlier steps cannot be written (:1727-1728).
- `define_metric`: `name` may only end in `*` (glob suffix) (:2894-2897); `summary` is comma-separated from
  `{"min","max","mean","last","copy","none","first"}` (:2901-2908); anything else raises `wandb.Error`.
- Artifact names: at most 128 characters and no `/` (`WB/sdk/artifacts/_validators.py:38-40, 100-118`).
- Lineage: `use_artifact("name:alias")` resolves through the run's public API and records the use (:3123-3139). `use_artifact(<draft Artifact>)` **logs it
  as this run's output and blocks on `artifact.wait()`** before marking it used (:3141-3155). A run that both logs and uses the same draft is the result.
- The run's public API is `wandb.Api(overrides={entity, project, base_url, run}, api_key=run.settings.api_key)` (`_public_api`, :3392-3406).

### 2.2 Settings fields that matter (`WB/sdk/wandb_settings.py`; `extra="forbid"`, :110-115)

| Field | Default | Line |
|---|---|---|
| `api_key: str \| None` | None; leading/trailing whitespace rejected (:1113-1120) | 144 |
| `base_url: str` | **`"https://api.forge.coreweave.com"`** (`WB/sdk/lib/urls.py:7`; introspected) | 159 |
| `app_url_override` / computed `app_url` | Forge or `api.wandb.ai` -> `https://forge.coreweave.com/wandb` (`WB/util.py:210-217`) | 150, 1731 |
| `console` | `"auto"`; `"off"` disables capture | 168 |
| `capture_loggers: dict \| None` | None | 223 |
| `credentials_file` | `~/.config/wandb/credentials.json` (token cache for JWT auth) | 253 |
| `disable_code` / `disable_git` | False / False | 260 / 263 |
| `entity`, `project` | None | 287, 423 |
| `http_proxy`, `https_proxy`, `x_proxies` | None | 324, 327, 854 |
| `identity_token_file` | None | 330 |
| `insecure_disable_ssl` | False | 376 |
| `mode` | `"online"` | 400 |
| `root_dir` | `os.getcwd()` at construction (`./wandb` or `./.wandb` under it, :1876-1886) | 481 |
| `save_code: bool \| None` | None (env `WANDB_SAVE_CODE`/`WANDB_DISABLE_CODE` may set it, :2024-2029) | 511 |
| `settings_system` | `~/.config/wandb/settings` or `$WANDB_CONFIG_DIR/settings` (set in `wandb_setup.py:218-225`) | 514 |
| `x_disable_meta`, `x_disable_stats`, `x_disable_machine_info` | False | 622, 625, 634 |
| `x_extra_http_headers: dict \| None` | None ("added to all outgoing HTTP requests") | 646 |
| `x_save_requirements` | True | 868 |

- `base_url` validator (:1124-1139): URL check; `https://forge.coreweave.com/api/wandb` -> `https://api.forge.coreweave.com`
  (`urls.py:67-84`); `wandb.ai` without `api.` or over http is rejected; trailing `/` stripped.
- Forge hosts map to W&B hosts: `forge.coreweave.com` -> `api.wandb.ai`, `qa.forge.coreweave.com` -> `api.qa.wandb.ai` (`urls.py:12-15`).
- Load order (`wandb_setup.py:189-250`): defaults -> settings files (global `settings_system`, then workspace `<cwd>/wandb/settings`) (:227)
  -> every `WANDB_*` env var (:232) -> system environment (:235) -> `wandb.setup`/`wandb.init` `settings` (:248). Done once per process;
  later env changes only print a warning (`wandb_setup.py:169-188`, `wandb_init.py:219-235`).
- Env mapping (`update_from_env_vars`, :1975-2016): `WANDB_<FIELD>` -> `field.lower()`, `WANDB__<X>` -> `x_<x>`, plus specials
  `WANDB_APP_URL`, `WANDB_DIR`(->root_dir), `WANDB_NAME`, `WANDB_NOTES`, `WANDB_TAGS`, `WANDB_JOB_TYPE`, `WANDB_HTTP_TIMEOUT`,
  `WANDB_SERVICE_TRANSPORT`, ... (:1982-1993). So **any** settings field (base_url, api_key, identity_token_file, credentials_file,
  settings_system, https_proxy, x_extra_http_headers, insecure_disable_ssl...) can be set from the environment.
- The workspace file `<cwd>/wandb/settings` is read with the cwd at load time; `WANDB_DIR` is applied after files are read, so only the
  process cwd decides it.

### 2.3 Where wandb finds a credential when none is passed

| Path | Order and source | Closed off by |
|---|---|---|
| `wandb.init` | if `settings.api_key` given: used as-is, not persisted (`wandb_init.py:207-216`, `update_api_key=False`); else `_login` -> `authenticate_session` | passing `Settings(api_key=...)` |
| `authenticate_session` (`WB/sdk/lib/wbauth/authenticate.py:68-143`) | 1. process session credentials (:114) 2. `_use_system_auth` (:171-223): `WANDB_API_KEY`, else `WANDB_IDENTITY_TOKEN_FILE` (both set = error) (:226-260); else netrc; else `settings.api_key` from files/env if host matches (:203-209) 3. interactive prompt (:130-143) | explicit key; `NETRC=<empty file>`; unset env; `prompt=False` only on `login` |
| netrc (`wbnetrc.py:45-93, 194-215`) | `$NETRC`, else existing `~/.netrc`, else `~/_netrc`; machine = base_url netloc, **then the upstream host for Forge** (`api.forge.coreweave.com` falls back to `api.wandb.ai`, :80-81) | `NETRC` pointing at an empty file |
| `wandb.Api()` | `api_key` arg, else `authenticate_session(update_session=False)`; no creds -> `UsageError` (`api.py:142-148, 191-205`); base_url = `overrides` > `WANDB_BASE_URL` > settings (:133-140); proxies also from `WANDB__PROXIES` (:164-166) | `api_key=` and `overrides={"base_url": ...}` |
| CES client | see 3.3; constructor falls back to `WANDB_API_KEY`/`WANDB_ACCESS_TOKEN`, then wandb overwrites both (`_writer_ces.py:743-751`) | wandb's override; also unset `CES_CUSTOM_HEADERS` |
| wandb-core | started with `env=os.environ` (`WB/sdk/lib/service/service_process.py:151`); whether the Go binary reads env/netrc itself is [live/unread] | a minimal process environment |

- Session credentials are process-global: an explicit key in `wandb.init` or `wandb.login` becomes the session auth and is copied into global
  settings (`authenticate.py:31-49, 146-168`; `wbauth/settings.py:46-53`), so later `wandb.Api()` calls and reports use it.
- `wandb.Api(...)` with an API key **always makes a verification request at construction** (`api.py:188-189` -> `wandb_login.py:317-362`).

What `wandb.login` writes:
- `login(key=...)`: the key to the netrc file as `machine <netloc> / login user / password <key>`, replacing that machine's entry,
  creating the file 0600 if missing (`wandb_login.py:260-267`; `wbnetrc.py:96-172`).
- Always (when it returns True or `prompt=True`): global settings file update: clears `anonymous`; with `host`, sets `base_url` (or clears it for
  the default) (`wandb_login.py:120-148`); written only if something changed (`settings_file.py:83-96`). A prompted key is written to netrc too
  (`WB/sdk/lib/wbauth/prompt.py:24, 66`).

### 2.4 Weave auto-start
`wandb.init` initializes Weave if `weave` is already imported, unless `WANDB_DISABLE_WEAVE` is truthy (`WB/sdk/wandb_init.py:1539`;
`WB/integration/weave/weave.py:27-38, 76-100`).
`weave` is **not installed** in the pinned venv.

---

## 3. EvalTables

### 3.1 Constructor (`ET/eval_table.py:52`)
```python
wandb.EvalTable(columns=None, data=None, rows=None, dataframe=None, dtype=None, optional: bool | list[bool] = True,
                allow_mixed_types=False, log_mode="IMMUTABLE", *, input_columns=None, output_columns=None, score_columns=None,
                backend: Literal["weave","ces"] | None = None, unsupported_media_mode: Literal["stub","raise"] = "stub")
```
- `backend` None -> `"weave"` (:142); other values -> `UsageError` (`ET/_writer_factory.py:29-31`).
- `"weave"` imports `weave>=0.52.41` **in the constructor** (`ET/_writer_weave.py:20, 130-133`). Without weave: `ImportError("EvalTable dependency
  error. Fix with: pip install wandb[\"eval-table\"]")` (introspected). In the pinned venv, `backend="ces"` is the only working backend.
- `"ces"` requires `allow_mixed_types=False` (`_writer_factory.py:24-25`). `log_mode` other than `"IMMUTABLE"` -> `UsageError` (:138-139).
- `columns` omitted but roles given -> columns = inputs + outputs + scores (:159-169). Unassigned columns are outputs (:340-342). No `input_columns`
  -> a synthetic input `"row"` = 1-based index is added per row (:348-354). Role names not in the table -> `ValueError` (:286-291); duplicate names
  after `str()` -> `ValueError` (:304-313).

### 3.2 Typing and logging
- Rows and columns are typed by `wandb.Table` (`SP/wandb/sdk/data_types/table.py:228`): `ColumnKey = str | int`, `InputRow = Iterable` (:41-44);
  each column's type is inferred from the data and later rows must be assignable, else `TypeError` (`add_data`, :600-631). With default
  `optional=True` columns are `None or <type>` (introspected: `{'a': None or String, 's': None or Number}`).
- CES types (`ET/_writer_ces.py:567-601`): bool -> boolean, int -> integer, float -> number (NaN sent as null, ±inf `UsageError`), str -> string; anything
  else `UsageError`. integer+number widen to number; other mixes `UsageError` (:637-680). A column that is null in every row -> `UsageError` (:385-400).
  Supported wandb media become JSON extension values (`_media_ces.py`); score columns take primitives only (:514-518).
- Logged only by `run.log({key: table})`: `val_to_json` calls `bind_to_run(run, key, step)` then `to_json(run)` (`SP/wandb/sdk/data_types/utils.py:131-147`).
  Not logged as a Table artifact (its `_log_type` is `"eval-table"`, :50). `to_json` refuses an Artifact (:223-224). Offline run -> `UsageError` (:196-200).
- The write happens **synchronously inside `run.log`**: for CES, a GraphQL project lookup via wandb-core, then HTTP to CES (create, columns, row batches,
  version) (`_writer_ces.py:222-307`). Second `run.log` of the same object is a no-op with a warning (:240-242).
- History value written: `{"_type": "eval-table-ces", "schema_version": 1, ncols, nrows, log_mode, evaluation_id, evaluation_version_id, dataset_id,
  dataset_version_id}` (:293-307); weave backend `{"_type": "eval-table", ..., "evaluate_call_id"}` (`_writer_weave.py:198-207`).

### 3.3 What `"ces"` needs (`ET/eval_table.py:202-209`, `ET/_writer_ces.py`)
- Online run with entity and project (:198-199); server feature `EVAL_TABLES_CES` checked in `bind_to_run`, else `UsageError` (`eval_table.py:205-209`).
- CES URL (:123-131): `CES_BASE_URL` env if set; else `https://evaluations.wandb.ai` when the run base URL is a Forge host or `api.wandb.ai`
  (`urls.forge_upstream_url`); else `f"{base_url}/evaluations"`.
- Scope: GraphQL `project(entityName, name){internalId}` (:45-51, :706-718). Credential: the run's `settings.api_key`; if none, an access token
  from identity-token auth (:719-724).
- Client: `coreweave_evaluations.Client(base_url, api_key, bearer_token)` then both re-assigned so env values cannot win (:743-751). Auth header
  Basic `api:<key>` or Bearer (`CE/_client.py:219-237`).
- Idempotency keys `wandb-eval-table-v1-<sha256(entity,project,run_id,key,step)>-<op>` (:202-217, :754-756): re-logging the same key at the same step
  replays; a different body under the same key is rejected by the server (:200-201).

### 3.4 Limits enforced in code (`ET/_writer_ces.py:52-68`)
16 MiB per request body (hard); row batches target 8 MiB and at most 10,000 rows; **at most 10,000 rows per table**; at least 1 row (:326-332);
at most 10,000 input+output columns and 256 score columns (:402-410); table name (the `run.log` key) 1-256 chars, column names 1-512, score names
1-256 (:603-608); one row whose encoded size reaches 16 MiB -> `UsageError` (:151-156); media cells over `_media_ces.CES_MAX_CELL_BYTES` become null
with a warning (:239-248). `wandb.Table.MAX_ROWS` (10,000) truncation does not apply to EvalTable (it bypasses `_to_table_json`).
Weave backend: no row limit in wandb code.

### 3.5 cwevals 0.0.1b1
- Distribution `cwevals`; `RECORD` lists only `coreweave_evaluations/...` (`SP/cwevals-0.0.1b1.dist-info/RECORD`); `import cwevals` fails,
  `import coreweave_evaluations` works (introspected, version 0.0.1b1).
- Stainless-generated REST client for the "Core Weave Evaluations API": `Client`/`AsyncClient` with resources `service, verification, datasets,
  comparisons, scorers, scores, evaluations, trajectories, eval_tables` (`CE/_client.py:150-203`).
- `Client(*, namespace="wandb", scope_id=None, bearer_token=None, username="api", api_key=None, base_url=None, timeout=NOT_GIVEN, max_retries=2,
  default_headers=None, default_query=None, http_client=None)` (`CE/_client.py:72-146`). Defaults: `WANDB_ACCESS_TOKEN`, `WANDB_API_KEY`,
  `CES_BASE_URL`, else `http://localhost:9300`; `CES_CUSTOM_HEADERS` (newline-separated `Name: value`) added to every request (:111-134).
  Timeout 60 s / connect 5 s, 2 retries (`CE/_constants.py`).
- wandb needs it for `"ces"`: imported lazily in `CESWriter.write` (`_writer_ces.py:234-235`), so a missing package fails at `run.log`, not at construction.

---

## 4. Reports (wandb-workspaces 0.4.13, `import wandb_workspaces.reports.v2 as wr`)

- `wr.Report(project: str, entity: str = <Api().default_entity>, title="Untitled Report" (max 128), description="", blocks=[], width="readable")`
  (`RP:3557-3583`). **No `api_key` or `base_url` parameter anywhere**: every server call goes through `_get_api()` = a fresh `wandb.Api()`
  with no arguments (`RP:3943-3947`), i.e. session credentials, then `WANDB_API_KEY`/netrc/settings (2.3), base URL from `WANDB_BASE_URL` or settings.
  Only way to pass a key explicitly in-process: make it the session credential first (`wandb.init(settings=Settings(api_key=...))` does this without
  writing netrc, 2.3); `wandb.login(key=...)` also works but writes netrc.
- Omitting `entity` calls the server at construction (`default_entity`, :3578).
- `Report.save(draft: bool = False, clone: bool = False) -> Report` (`RP:3671-3708`): lists the entity's projects and **creates the project if absent**
  (:3675-3684); `upsertView` with `type = "runs/draft" if draft else "runs"` (:3698); sets `self.id`; prints the URL. A draft is a view of type
  `runs/draft`; whether it is visible to others is [live]. `clone=True` saves as a new report.
- Each `_get_api()` is a new `wandb.Api()`, which re-verifies the key (2.3), so a save makes several requests.
- `Report.url` (:3648-3669): `scheme://netloc` of the app URL + `entity/project/reports/<title>--<id>`; the app URL's path is dropped, so on Forge
  (`https://forge.coreweave.com/wandb`, 2.2) it yields `https://forge.coreweave.com/<entity>/...` without `/wandb`. Whether that resolves is [live].
- Other: `Report.from_url(url, *, as_model=False)` :3904, `delete()` :3868, `enable_share_link()` :3769, `to_html(height=1024, hidden=False)` :3919.

Panels and blocks (pydantic; fields from `RP`):
- `PanelGrid(runsets=[Runset()], hide_run_sets=False, panels=[], active_runset=0, custom_run_colors={})` :1328-1350.
- `Runset(entity="", project="", name="Run set", query="", filters="", groupby=[], order=..., run_settings={}, pinned/visible/hidden_columns=[], ...)`
  :957-971. With entity and project set, `_to_model()` looks up the project id over GraphQL at save time (:1091-1107); with them empty no lookup
  is made and which project the runset shows is [live].
- Runs: `LinePlot(title=None, x="Step", y=[...], ...)` :2097, `ScalarChart(title=None, metric="", ...)` :2502, `BarPlot` :2399,
  `ParallelCoordinatesPlot` :2632, `RunComparer(diff_only=None)` :2726. Every panel has `layout=Layout(x=0, y=0, w=8, h=6)` (:209-213, :1311).
- Tables: `WeavePanelSummaryTable(table_name=...)` (keyword-only) shows `runs.summary[table_name]` of the grid's runsets (:3054);
  block form `WeaveBlockSummaryTable(entity, project, table_name)` (:1476-1497). No EvalTable-specific panel exists in the package (no match
  for "eval table"); whether a summary table panel renders an `eval-table-ces` history value is [live].
- Artifacts: `WeavePanelArtifact(artifact=..., tab="overview"|"metadata"|"usage"|"files"|"lineage")` in a PanelGrid (:3442);
  `WeaveBlockArtifact(entity, project, artifact, tab="overview")` (:1881); `WeavePanelArtifactVersionedFile` :3299.
- Text: `H1(text)` :296, `P` :411, `MarkdownBlock(text="")` :672, `TableOfContents` :1436. `save()` wraps blocks in empty `P()` at both ends (:3595-3606).

---

## 5. Environment variables that move data or pick a credential

| Variable | Read by | Effect | Where |
|---|---|---|---|
| `WANDB_API_KEY` | agentlens `init` | key if `api_key` not passed | `AL/tracing.py:85` |
| | wandb auth | first system credential | `WB/sdk/lib/wbauth/authenticate.py:232` |
| | wandb Settings | `settings.api_key` (via env mapping) | `WB/sdk/wandb_settings.py:1975-2016` |
| | cwevals `Client` | `api_key` default | `CE/_client.py:119-121` |
| `WANDB_IDENTITY_TOKEN_FILE` | wandb auth | JWT auth; error if `WANDB_API_KEY` also set | `authenticate.py:233-260` |
| `WANDB_CREDENTIALS_FILE` | Settings | access-token cache path | `wandb_settings.py:253`, mapping :1975 |
| `WANDB_ACCESS_TOKEN` | cwevals `Client` | bearer default (wandb overrides it) | `CE/_client.py:111-113` |
| `WANDB_BASE_URL` | agentlens | trace host (if no `base_url`/`WF_TRACE_SERVER_URL`) and netrc host | `AL/tracing.py:185, 192` |
| | Settings / `wandb.Api` | W&B host; Api reads env over settings | mapping :1975; `WB/apis/public/api.py:134`, `WB/env.py:307-313` |
| `WF_TRACE_SERVER_URL` | agentlens | trace host if no `base_url` | `AL/tracing.py:183` |
| `WANDB_APP_URL` | Settings | `app_url_override` (report URLs) | `wandb_settings.py:1983` |
| `WANDB_ENTITY`, `WANDB_PROJECT`, `WANDB_ORGANIZATION` | Settings; `wandb.Api` | default entity/project | mapping :1975; `WB/env.py:262-304`, `api.py:135-137` |
| `WANDB_MODE` | Settings | online/offline/disabled | mapping :1975 (field :400) |
| `WANDB_CONFIG_DIR` | setup | global settings file location | `WB/sdk/wandb_setup.py:220-222` |
| `WANDB_SETTINGS_SYSTEM` | Settings | global settings file path (env mapping; applied after files are read) | `wandb_settings.py:514`, :1975 |
| `WANDB_DIR` | Settings | `root_dir` (local run files) | `wandb_settings.py:1985` |
| `WANDB_DATA_DIR`, `WANDB_CACHE_DIR`, `WANDB_ARTIFACT_DIR` | wandb | local staging/cache dirs | `WB/env.py:74-77` (readers not traced) |
| `WANDB_HTTPS_PROXY`, `WANDB_HTTP_PROXY`, `WANDB__PROXIES` | Settings; `wandb.Api` | proxies for W&B traffic | mapping :1975; `api.py:164-166` |
| `WANDB_X_EXTRA_HTTP_HEADERS` / `WANDB__EXTRA_HTTP_HEADERS` | Settings | headers on all W&B requests | `wandb_settings.py:646`, :1999-2002 |
| `WANDB_INSECURE_DISABLE_SSL` | Settings | no TLS verification | `wandb_settings.py:376`, `WB/env.py:78` |
| `WANDB_SAVE_CODE`, `WANDB_DISABLE_CODE`, `WANDB_DISABLE_GIT` | Settings | code/git capture | `wandb_settings.py:2024-2031` |
| `WANDB_CONSOLE` | Settings | console capture | mapping :1975 |
| `WANDB_DISABLE_WEAVE` | wandb weave integration | stops Weave auto-init | `WB/integration/weave/weave.py:27-38` |
| `WANDB_ERROR_REPORTING` | service launcher | `--no-observability` to wandb-core when false | `WB/sdk/lib/service/service_process.py:131-132`, `WB/env.py:168-169` |
| `NETRC` | wandb netrc read/write | netrc path | `WB/sdk/lib/wbauth/wbnetrc.py:200-201` |
| | `requests` (OTLP transport) | netrc for the trace host | `SP/requests/utils.py:239-243` |
| | agentlens `_netrc_api_key` | **ignored** (always `$HOME/.netrc`) | `AL/tracing.py:195` |
| `HOME` | agentlens netrc, wandb `~` paths | `~/.netrc`, `~/.config/wandb` | `AL/tracing.py:195`; `wbnetrc.py:204` |
| `CES_BASE_URL` | wandb CES writer; cwevals | CES host | `ET/_writer_ces.py:41, 125`; `CE/_client.py:123-126` |
| `CES_CUSTOM_HEADERS` | cwevals `Client` | extra headers on every CES request; they override auth headers | `CE/_client.py:128-134`, `CE/_base_client.py:672-680` |
| `CES_LOG` | cwevals | client logging level | `CE/_utils/_logs.py:17` |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP exporter | ignored by agentlens (endpoint passed) | `OT/exporter/otlp/proto/http/trace_exporter/__init__.py:139` |
| `OTEL_EXPORTER_OTLP[_TRACES]_HEADERS` | OTLP exporter | **merged into the trace request** (explicit keys win) | `.../proto/http/_common/__init__.py:137-149` |
| `OTEL_EXPORTER_OTLP[_TRACES]_TIMEOUT`, `_COMPRESSION` | OTLP exporter | per-request timeout, gzip/deflate | `_common/__init__.py:152-175`; trace_exporter :140-142, :157 |
| `OTEL_EXPORTER_OTLP[_TRACES]_CERTIFICATE`, `_CLIENT_KEY`, `_CLIENT_CERTIFICATE` | OTLP exporter | TLS material | trace_exporter :143-150 |
| `OTEL_PYTHON_EXPORTER_OTLP_HTTP[_TRACES]_CREDENTIAL_PROVIDER` | OTLP exporter | loads a `requests.Session` from an entry point | `_common/__init__.py:72-108` |
| `OTEL_BSP_*` (4) | BatchSpanProcessor | queue/batch/delay/timeout | `OT/sdk/trace/export/__init__.py:26-30, 224-232` |
| `OTEL_SDK_DISABLED=true` | TracerProvider | no-op tracer: nothing recorded or sent, trace ids invalid | `OT/sdk/trace/__init__.py:1278-1279, 1342-1343` |
| `OTEL_TRACES_SAMPLER`, `_ARG` | TracerProvider | e.g. `always_off` drops every span | `OT/sdk/trace/sampling.py:466-473`, `trace/__init__.py:1275` |
| `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_SERVICE_NAME` | Resource | extra resource attrs; explicit agentlens attrs still win | `OT/sdk/resources/__init__.py:333-353, 199-201` |
| `OTEL_EXPERIMENTAL_RESOURCE_DETECTORS` | Resource | e.g. `process` adds process attributes to every export | `OT/sdk/resources/__init__.py:679-683` |
| `OTEL_[SPAN_]ATTRIBUTE_COUNT_LIMIT`, `..._VALUE_LENGTH_LIMIT` | SpanLimits | attribute caps | `OT/sdk/trace/__init__.py:659-690` |
| `HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `SSL_CERT_FILE` | requests (OTLP), httpx (cwevals) | proxy / CA | `SP/requests/sessions.py:763, 857-858`; `SP/httpx/_utils.py:30-54`, `_config.py:34-35` |
| `CWSANDBOX_API_KEY` | cwsandbox | Bearer key for builtin auth | `SP/cwsandbox/_auth.py:95-103` |
| `CWSANDBOX_BASE_URL` | cwsandbox | Sandbox API host (default `https://api.cwsandbox.com`, `_defaults.py:41`) | `SP/cwsandbox/_discovery.py:326, 413`; `_volume.py:657, 708`; `_network_config.py:551` |
| `CWSANDBOX_DISABLE_SIGNAL_HANDLERS` | cwsandbox | cleanup signal handlers | `SP/cwsandbox/_cleanup.py:49, 122` |
| `COREWEAVE_*` | — | none read by coreweave 0.1.0b0 (no `environ` reads outside `tracing.py`) | grep of `AL/` |

cwsandbox `WandbAuth` (`_auth.py:126-195`) uses the wandb session/`authenticate_session` chain of 2.3, and sends `x-wandb-api-key`,
`x-entity-id` (arg > active run > settings), `x-project-name`.

---

## 6. Surprises

1. wandb 0.31.0's default `base_url` is `https://api.forge.coreweave.com`, not `api.wandb.ai` (`urls.py:7`); the default app URL is
   `https://forge.coreweave.com/wandb`, and `api.wandb.ai` is treated as a Forge host for app URLs (`urls.py:46-52`, `util.py:216-217`).
2. Agent Lens defaults differ: with `WANDB_BASE_URL` unset it targets `https://trace.wandb.ai`; with `WANDB_BASE_URL=https://api.forge.coreweave.com` it
   targets `https://api.forge.coreweave.com/traces/agents/otel/v1/traces` (`tracing.py:180-188`); whether that path exists is [live].
3. `tracing.init(base_url=...)` is the trace server, and the path `/agents/otel/v1/traces` is appended (`tracing.py:90`).
4. Agent Lens netrc lookup ignores `NETRC`, reads `$HOME/.netrc`, and keys on the `WANDB_BASE_URL` host, not the trace host (`tracing.py:191-200`).
5. `tracing.force_flush()` returns True even when every export failed (`_shared_internal/__init__.py:186-189, 232-237`).
6. A failed export (401, 403, 413, 500) is logged and the spans are lost; only 429/502/503/504 and connection errors are retried, within 10 s.
7. A batch whose protobuf exceeds 64 MiB is dropped whole (not split); the OTel SDK sets no value-length limit by default, so one huge
   message attribute can drop up to 511 other spans with it.
8. More than 128 attributes on a span are dropped silently (OTel default).
9. `OTEL_EXPORTER_OTLP_HEADERS` is merged into the trace request; `OTEL_SDK_DISABLED=true` or `OTEL_TRACES_SAMPLER=always_off` turns Agent Lens off
   silently (init still succeeds, trace ids are zero for the former).
10. No `end_tool()` or `end_subagent()`; tools and sub-agents end only through their objects.
11. Trace and span ids cannot be chosen; only `conversation_id` can. Conversations emit no span.
12. Batch `log_turn` flattens every child under the turn; nested sub-agent trees need the streaming path with `started_at`/`ended_at`.
13. `LLM` and `Turn` take `started_at=now` at construction, so a factory-made span starts when it is created; naive datetimes are local time.
14. `autopatch_integrations` defaults to True and installs a global import hook that traces `anthropic`/`openai`/`google.genai` calls.
15. `wandb.EvalTable` defaults to the Weave backend, which needs the `weave` package at construction; with weave absent only `backend="ces"` works.
16. EvalTable CES writes run synchronously inside `run.log` and are limited to 10,000 rows, primitive score columns, no all-null columns, no offline mode.
17. `cwevals` installs as `coreweave_evaluations`; `CES_CUSTOM_HEADERS` can override the Authorization header on CES requests.
18. Reports take no API key or base URL; they use a fresh `wandb.Api()` per call, resolved from session credentials, env, netrc or settings files.
19. `Report.save()` creates the project if it does not exist; `Report.url` drops the `/wandb` path of the Forge app URL.
20. `wandb.Api(api_key=...)` makes a network verification call in its constructor.
21. `run.use_artifact(draft_artifact)` logs the artifact as an output of the same run and blocks until it is committed.
22. Netrc lookup for Forge falls back to the `api.wandb.ai` entry (`wbnetrc.py:80-81`), so an old W&B key in `~/.netrc` is picked up for Forge.
23. `wandb.login()` can write the global settings file as well as netrc; `wandb.init(settings=Settings(api_key=...))` writes neither.
24. Any `Settings` field can be set by a `WANDB_<FIELD>` or `WANDB__<X>` env var, including `x_extra_http_headers` and `insecure_disable_ssl`.
25. `<cwd>/wandb/settings` is read as a workspace settings file before env vars and regardless of `WANDB_DIR`.

Open [live] items: server acceptance of the trace endpoint per base URL; server-side span size limits; the Forge `/traces` path; whether
wandb-core reads env or netrc itself; draft report visibility; Runset with empty entity/project; Forge report URL resolution;
whether a summary-table panel renders an `eval-table-ces` value; CES billing.
