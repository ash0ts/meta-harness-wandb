# SDK conformance: `mhforge/executors/sandbox_client.py` against cwsandbox 1.27.0

Checked on 2026-11-10 against the installed source in `.venv/lib/python3.13/site-packages/cwsandbox` (`__version__ = "1.27.0"`, `__init__.py:9`), with wandb 0.31.0 and grpcio 1.85.1 beside it. I read the source and ran an isolated, offline introspection (`python -I`, `env -i`, sockets blocked). That run covered `inspect.signature` on every call we make, `resolve_auth` on our `AuthHeaders`, and `NetworkOptions` and `normalize_resources` on our values. It also built a `Sandbox(...)` object locally (not started) and printed its `_build_create_request` protobuf. It made no network call, created no sandbox and read no credential.

SDK paths are relative to `site-packages/cwsandbox/`. "Ours" line numbers refer to `sandbox_client.py` after the fixes.

## 1. One row per SDK use

| # | Our call (line) | SDK definition | Verdict |
|---|---|---|---|
| 1 | `importlib.import_module("cwsandbox")`, lazily in `_sdk()` (375) | package `__init__.py`. Exports `Sandbox`, `AuthHeaders`, `WandbAuth`, `NetworkOptions` and all exception classes (`__init__.py:14-151`) | OK |
| 2 | env `CWSANDBOX_DISABLE_SIGNAL_HANDLERS="1"` set before the first import (371) | `_cleanup.py:49,120-123`. Read once, at the first owned-sandbox activation (`_cleanup.py:146-172`, called from `Sandbox.__init__`, `_sandbox.py:2572-2575`). `atexit` cleanup is still registered | OK. Name and truthy value are correct |
| 3 | env `CWSANDBOX_BASE_URL`, which we assumed was not read | Read on every entry point: `Sandbox.__init__` (`_sandbox.py:2272-2274`, where env beats `defaults.base_url`), `list` (3435-3437), `from_id` (3595-3597), `delete` (3711-3713). `Sandbox.run` has no `base_url` parameter (signature, `_sandbox.py:2578-2620`) | **MISMATCH, fixed (M1)** |
| 4 | `cw.AuthHeaders(headers={x-wandb-api-key, x-wandb-sdk-version, x-entity-id, x-project-name}, strategy="wandb_api_key")` (383) | `_auth.py:31-40`: frozen dataclass `(headers: dict[str,str], strategy: str)`, both required. `resolve_auth` returns an `AuthHeaders` unchanged (`_auth.py:262-263`). The introspection confirmed `resolve_auth(h) is h` and that wandb was not imported. The header names match what `WandbAuth` builds (`_auth.py:166-195`) | OK. The key is passed explicitly. No netrc, no `CWSANDBOX_API_KEY`, no wandb chain |
| 5 | `SDK_VERSION_HEADER = "0.31.0"` | `_auth.py:168` sends `wandb.__version__`. The pinned wandb is 0.31.0 | OK |
| 6 | (implicit) no `x-wandb-host` header | `_auth.py:181-185` adds it only for a non-SaaS W&B host | OK for SaaS W&B. Wrong for a dedicated or self-hosted W&B (see L4) |
| 7 | `cw.WandbAuth(entity=, project=)` (386), mode `wandb_auth` only | `_auth.py:58-80`. Resolution is `_auth.py:126-195`: wandb `session_credentials`, then env `WANDB_API_KEY`, then netrc, then settings (`wandb/sdk/lib/wbauth/authenticate.py:19-28,198-207,232-244`). Host and entity fallback come from wandb settings | Names OK. **Not key-pinned**: an in-process wandb session credential wins over the `WANDB_API_KEY` we set (R1, reported) |
| 8 | Auth on every call: `run(auth=)`, `list(auth=)`, `from_id(auth=)` | `Sandbox.__init__` keeps `auth` (2214) and `_ensure_client` resolves it once (4844-4861). Sandboxes returned by `list` and `from_id` inherit `auth` (3511, 3636). With auth always set, the `CWSANDBOX_API_KEY` path (`_auth.py:94-103`) is never reached | OK. No fall-back |
| 9 | Direct data plane (implicit, `data_plane_mode` left at `auto`) | `_sandbox.py:7756-7766`: direct mTLS leases send `metadata=()`, so the key headers go only to the control-plane host | OK |
| 10 | `cw.Sandbox.run(container_image=, auth=, max_lifetime_seconds=int, tags=list, resources={"cpu","memory"}, network=, working_dir=, environment_variables=)`, no positional args (405-410) | `_sandbox.py:2578-2775`. Every keyword exists. No args means the default keep-alive `/bin/sh -c 'trap ... sleep infinity & wait'` (`_defaults.py:39-40`). The local `_build_create_request` printed: image, `working_dir`, env, resources requests=limits 2/4Gi, `max_lifetime_seconds: 8250`, tags, `network { deny_egress: true dns_egress: DNS_EGRESS_MODE_DENY }` | OK |
| 11 | lifetime `max_lifetime_seconds=spec.max_lifetime_s` | `_sandbox.py:5776-5777` sends `int(...)`. `None` would let the backend choose (`_defaults.py:127`) | OK. Server-side cap unknown (L5) |
| 12 | tags `list(spec.tags)` | `_defaults.py:226-241` rejects a bare str. Tags are merged with the defaults (empty) at `_sandbox.py:2317` | OK |
| 13 | `resources={"cpu": "2", "memory": "4Gi"}` | `_resources.py:18-45`: a flat dict means Guaranteed QoS (requests = limits) | OK |
| 14 | `cw.NetworkOptions(deny_egress=True, dns_egress="deny")` (409) | `_types.py:768-818`: keyword-only dataclass. `"deny"` is coerced to `DNSEgressMode.DENY` (`_types.py:493-504`). `deny_egress` cannot be combined with egress rules | OK |
| 15 | Create returns after acceptance: `run(...)` returns a `Sandbox` | `_sandbox.py:2774` calls `sandbox.start().result()`. This blocks until `CreateSandbox` is accepted, not until RUNNING. Create RPC errors are translated and raised here (`_sandbox.py:5432-5476`) | OK |
| 16 | Throttling on create | `create_retry_budget_seconds` defaults to 0 (`_defaults.py:370`), so the SDK does not retry quota, capacity or transport errors (`_sandbox.py:5344-5398`). Spillover is STRICT by default (5400-5476). Poll retries UNAVAILABLE, DEADLINE_EXCEEDED and RESOURCE_EXHAUSTED for 30 s (`_defaults.py:112`) | OK. Our quota backoff is the only create retry |
| 17 | `_sandbox_id(obj)` reads `.sandbox_id`, then `.id` | `Sandbox.sandbox_id` property (`_sandbox.py:4207-4212`). Set after `start()` | OK |
| 18 | `sandbox.wait(timeout=)` (272) | `_sandbox.py:6661-6684`. Returns `self`. Raises `SandboxFailedError` (FAILED, carrying `status_reason`), `SandboxTerminatedError` or `SandboxTimeoutError` (6349-6352, 6431) | OK |
| 19 | Image pull failure: `_is_pull_failure` (143-147), tested at `create()` only (411-418) | No pull-failure reason exists in `_error_info.py:46-247`. A pull failure is `SandboxFailedError` with `status_reason` set to `"ErrImagePull: ..."`, raised from `wait()`. While CREATING, `status_reason` may read `ImagePullBackOff` (`exceptions.py:111-134`, `_sandbox.py:4384-4392`, 4524-4529) | **MISMATCH**. Predicate fixed (M3). Call site cannot be fixed without an interface change (R2) |
| 20 | `sandbox.exec(argv, cwd=, timeout_seconds=)` then `.result(timeout=t+30)` (279) | `_sandbox.py:8376-8479`: `exec(command, *, cwd, check=False, timeout_seconds, stdin=False, container)` returns `Process`. `Process.result(timeout)` (`_types.py:2117-2135`) returns `ProcessResult(stdout: str, stderr: str, returncode: int, ...)` (`_types.py:1587-1616`). `cwd` becomes `/bin/sh -c "cd <q> && exec <q...>"` (`_sandbox.py:827-844`). `check=False` returns a nonzero code instead of raising. `exec` first waits for RUNNING, outside `timeout_seconds` (8388-8390) | OK |
| 21 | Command time limit: exceptions named in `_TIMEOUT_CLASSES` (40) | `SandboxCommandTimeoutError` (`_sandbox.py:1079-1085`) and `SandboxTimeoutError("Command ... timed out after Ns")` (around 8206). `concurrent.futures.TimeoutError is TimeoutError` holds on 3.13 (introspected) | OK. `SandboxRequestTimeoutError` (gRPC DEADLINE_EXCEEDED) is also reported as `timed_out=True` (L6) |
| 22 | `exec(..., stdin=True)` returns a `Process`. We iterate `process.stdout`, call `process.stdin.write(bytes).result(timeout=)` and `process.stdin.close().result(timeout=)` (213, 236, 249) | `StreamReader` iterates synchronously (`_types.py:1669-1696`). `StreamWriter.write(data)` and `close()` return `OperationRef` (`_types.py:1927-1989`). The stdin queue is bounded at 16, so a stalled consumer blocks `write().result()` and our timeout fires | OK |
| 23 | stdout items: we decode bytes incrementally, or take str as is (214-216) | The SDK decodes each gRPC chunk itself with `errors="replace"` and queues str only (`_sandbox.py:8266-8284`). stdout and stderr queues are unbounded (8446-8449) | OK. A multi-byte character split across chunks is replaced by U+FFFD inside the SDK (L7). The old fake fed bytes, which never happens |
| 24 | `sandbox.write_file(path, bytes, timeout_seconds=)` then `.result(timeout=t+30)` (295) | `_sandbox.py:9495-9539`: `(filepath, contents: bytes, *, timeout_seconds, container)` returns `OperationRef[None]`. Payloads up to 32 MiB go unary, up to 256 MiB through streaming fallback | OK |
| 25 | `sandbox.read_file(path, timeout_seconds=)` then `.result(timeout=t+30)` (301) | `_sandbox.py:9059-9110` returns `OperationRef[bytes]`. A missing file raises `SandboxFileError(reason="CWSANDBOX_FILE_NOT_FOUND")` when the ErrorInfo domain is trusted (`_sandbox.py:1060-1070`, `_error_info.py:46`). It can buffer up to 256 MiB before our `max_bytes` check | OK. The reason matches. The names `FileNotFoundError` and `SandboxFileNotFoundError` never occur (harmless) |
| 26 | `sandbox.stop(missing_ok=True).result(timeout=)` (443) | `_sandbox.py:7124-7209` resolves at terminal state. It already retries transient UNAVAILABLE (around 6868) and treats NOT_FOUND on a retry as stopped | OK. Our one extra retry after 20 s on `SandboxUnavailableError` is redundant but harmless |
| 27 | `cw.Sandbox.list(tags=, base_url=, auth=).result()` for orphan cleanup (425) | `_sandbox.py:3157-3248` returns `OperationRef[list[Sandbox]]`. Every page is followed (3485-3493). Sandboxes must have ALL tags. Only active ones are listed (`show_terminated=False`) | OK. `base_url` added (M1) |
| 28 | `cw.Sandbox.from_id(id, base_url=, auth=).result()`, with `NotFound` meaning gone (433-436) | `_sandbox.py:3521-3640`. Raises `SandboxNotFoundError` (`_sandbox.py:1071-1078,1157-1163`) | OK. `base_url` added (M1) |
| 29 | Delete | `Sandbox.delete(id, *, base_url, auth, timeout_seconds, missing_ok)` exists (`_sandbox.py:3643-3698`). We use `from_id` + `stop`, which also works | OK (not used) |
| 30 | `exc.reason` (`_reason`, 137-139) | `CWSandboxError.reason`, `.metadata` and `.retry_delay` (`exceptions.py:19-49`) | OK |
| 31 | `exc.code` / `exc.status` (`_status`, 108-135) | No SDK exception has either attribute. The SDK raises its own class `from` the `grpc.RpcError`, whose `code()` returns the `StatusCode` (`_sandbox.py:3501,3627,5476`) | **MISMATCH, fixed (M2)** |
| 32 | Class `SandboxResourceExhaustedError`, read as RESOURCE_EXHAUSTED | `_sandbox.py:1186-1192` | OK |
| 33 | Class `SandboxUnavailableError`, read as UNAVAILABLE | `_sandbox.py:1086-1092,1179-1185`. `DNSEgressUnavailableError` subclasses it (`exceptions.py:745`) | OK |
| 34 | `"PermissionDenied" in class name` | No such class. PERMISSION_DENIED raises `CWSandboxAuthenticationError("Permission denied: ...")` (`_network.py:165-171`) | **MISMATCH** (dead test). Fixed by M2 |
| 35 | `CWSandboxAuthenticationError` read as UNAUTHENTICATED | `_network.py:158-164`. Also raised for PERMISSION_DENIED (165) | OK after M2 |
| 36 | `QUOTA_REASONS` = `CWSANDBOX_OCCUPANCY_QUOTA_EXCEEDED`, `CWSANDBOX_SERVERLESS_CREATE_THROTTLED` | Neither exists anywhere in the SDK (`_error_info.py:46-247`). The SDK forwards any server reason verbatim (`exceptions.py:31-35`) | Cannot settle offline (L1) |
| 37 | RESOURCE_EXHAUSTED with no reason treated as a billing stop (non-retryable) | The SDK attaches no meaning to it | Cannot settle offline (L2) |
| 38 | Unused `Session` | not called | OK |

## 2. Mismatches and changes

**M1. `CWSANDBOX_BASE_URL` decided where the key went (blocker).** Every entry point reads this variable, and in `Sandbox.run` it wins over any default. `Sandbox.run` also takes no `base_url`. If that variable were set in the runner's environment, the W&B key headers would go to whatever host it named. The docstring's claim that the SDK "reads no environment" was false.
- Change: a `SANDBOX_BASE_URL = "https://api.cwsandbox.com"` constant (equal to `_defaults.py:41`).
- `list` and `from_id` now pass `base_url=SANDBOX_BASE_URL`.
- `_sdk()`, which every `create`/`list_ids`/`stop_id` goes through, refuses with `ExecutorError(kind="auth", retryable=False)` before any SDK call if the variable is set to anything else. An empty value or the default is accepted.
- Handles returned by `create` keep the URL fixed at construction.

**M2. Status was read from attributes the SDK never sets (major).** SDK exceptions carry no `code` or `status`. So PERMISSION_DENIED, which the SDK raises as `CWSandboxAuthenticationError`, was classed as `auth`, and the `permission` kind was unreachable. Both are non-retryable, so only the label was wrong.
- Change: `_status` now also reads `exc.__cause__.code()`, the `grpc.RpcError` the SDK chains with `raise ... from e`.
- The old attribute path is kept, because `tests/fake_sandbox.py` (another engineer's file) uses it.

**M3. Pull-failure predicate named nothing real (major).** The guessed reasons and the `ImagePull` class-name test match no 1.27.0 name.
- Change: `_is_pull_failure` also accepts a `status_reason` whose kubelet prefix is in `IMAGE_PULL_STATUS_REASONS` (`ErrImagePull`, `ImagePullBackOff`, `ErrImageNeverPull`, `InvalidImageName`).
- The guessed reason names are kept and marked as guesses.
- On its own this does not make the fallback work (see R2).

**Docstrings.** The module and class docstrings now say what was checked and what the SDK reads.

**Tests (`tests/test_sandbox_client.py`).** The fake now has the real names, keyword-only parameters and defaults, with no catch-all `**kwargs`:
- `AuthHeaders` requires `strategy`. `WandbAuth` validates empty strings.
- `NetworkOptions` is a keyword-only frozen dataclass that validates `dns_egress`.
- `CWSandboxError(message, *, reason, metadata, retry_delay)` takes no `code`. `SandboxFailedError` has `status_reason`.
- `exec` always returns a `Process` whose stdin is `None` unless `stdin=True`. stdout yields str only.
- `run`, `list`, `from_id`, `stop`, `read_file` and `write_file` have the full real signatures.

New tests:
- `test_fake_matches_recorded_sdk_signatures` holds the fake's parameter names to the 1.27.0 signatures, which are recorded in `SDK_1_27_0_PARAMS`.
- `test_base_url_pinned_and_env_override_refused` covers M1.
- `test_pull_failure_at_wait_is_a_retryable_create_error` covers R2.
- Error cases now carry the status in `__cause__`, as the real SDK does.

**Pin.** `pyproject.toml` live extra changed from `cwsandbox[wandb]>=1.19.6` to `cwsandbox[wandb]==1.27.0`.

### Reported, not changed (would change the interface or another module)

**R1. `wandb_auth` mode is not key-pinned.** `WandbAuth` resolves through wandb's chain. An existing in-process session credential wins over the `WANDB_API_KEY` we set, and the host comes from wandb settings (env or settings files). Under a long-running telemetry `wandb.init` with a different key, sandboxes could bill to that identity. The default `headers` mode has no such exposure. Recommendation: keep `headers` the only mode the live plan allows.

**R2. The fallback image can never be used.** `Sandbox.run` returns once the create is accepted, so a pull failure surfaces later from `wait()` (inside `wait_running`). Alternatively the sandbox sits in CREATING with `ImagePullBackOff` until `create_wait_s` and then times out. `create()` never sees a pull failure.
- What happens today: `SandboxExecutor._create_running` (`executors/sandbox.py:243-269`) disposes the sandbox and re-creates it once with the same spec, so the same image is tried twice.
- Why the adapter cannot fix it: swapping images inside `wait_running` would change `handle.id` after the executor has recorded and metered it (`sandbox.py:229-237`).
- Fix needed at the executor/spec level: on a pull-failure `ExecutorError`, re-create with `fallback_image`. That needs a distinguishable error, such as a flag or kind on the error, which is an interface change. This is for the engineer building `sandbox.py`.

**R3. Duplicate sandboxes on an ambiguous create.** A retried create can leave a duplicate. Each `Sandbox.run` builds a new object with a fresh `request_id`. After an ambiguous create failure (timeout or UNAVAILABLE after the backend accepted it), our retry can create a second sandbox. Tags make it visible to orphan cleanup, so the risk is cost, not a leak.

## 3. What cannot be settled without a live call

- **L1. Quota reason names.** Whether the backend emits `CWSANDBOX_OCCUPANCY_QUOTA_EXCEEDED` and `CWSANDBOX_SERVERLESS_CREATE_THROTTLED`, under which gRPC code, and with which ErrorInfo domain.
- **L2. RESOURCE_EXHAUSTED with no reason.** Whether it is really a billing stop, as opposed to transient pressure.
- **L3. Pull-failure text.** The exact `status_reason` text for a pull failure, and whether a bad digest goes to FAILED or stays in CREATING/`ImagePullBackOff` until our wait times out (PF6).
- **L4. AuthHeaders without `x-wandb-host`.** Whether the sandbox API accepts our AuthHeaders without `x-wandb-host`, for the account's W&B host. This is correct only for SaaS W&B. Also whether `x-entity-id` and `x-project-name` are honored as sent (PF3).
- **L5. Lifetime cap and DNS deny.** The server-side cap on `max_lifetime_seconds` (we send 8250), and whether `dns_egress=DENY` is supported on the placement used. A refusal would come back as `DNSEgressUnavailableError`, which we treat as a retryable create error.
- **L6. Timeouts.** Whether a command that exceeds `timeout_seconds` arrives as `SandboxCommandTimeoutError` (server reason) or as the client-side `SandboxTimeoutError`. Both map to `timed_out=True`. So, today, does a transport `SandboxRequestTimeoutError`.
- **L7. Chunking and the relay frames.** How the backend chunks stdout. The relay framing must stay ASCII-safe, because the SDK decodes each chunk separately.
- **L8. Stop and delete.** Whether `stop()` on a sandbox that failed to start releases billing without a `delete`.

## 4. Decisions applied

Applied on 2026-11-10. `sandbox_client.py` is unchanged, and so is its public interface.

- **LD7 (R1 closed).** `manifest.live_blockers` adds the row `("sandbox.auth_mode", WANDB_AUTH_REFUSED)` to every live command whenever `sandbox.auth_mode` is not `headers`, whatever the executor kind. The message says why: `WandbAuth` takes W&B's session credential and host from wandb's own settings, not the key given. Validation still accepts `wandb_auth`, so `tests/fixtures/manifest_valid.json` and `manifest.draft.json` validate as before (both use `headers`). Test: `test_wandb_auth_refused_by_every_live_command_but_still_validates`. The `environ.WANDB_*` rows for that mode are kept.
- **LD8 (R2 closed at the executor).** `SandboxExecutor._create_running` classifies a failed `wait_running` with the client's own `_is_pull_failure`. The function is applied to the mapped error's `__context__`, which holds the SDK exception, because `_CwHandle.wait_running` raises `map_sdk_error(...) from None` inside its `except` block. On a pull failure with `spec.fallback_image` set, the failed sandbox is stopped and its row closed (`_dispose`), and the one re-create uses `image=fallback_image, fallback_image=None`. A second failure is `ExecutorError("create", retryable=True)`, as before. `JobResult.facts` gains `image` and `image_fallback`.
  - If the stop of the failed sandbox fails, no second sandbox is created, for any re-create and not only the fallback one. Its row stays open at the worst case for `cleanup_orphans`, so the meter can only over-count.
  - A wait failure that is not a pull failure re-creates the same spec, as before.
  - `tests/fake_sandbox.py` now raises a pull failure from `wait_running` the way the adapter does (a `SandboxFailedError` with the real keyword-only signature and `status_reason="ErrImagePull: ..."`, mapped inside an `except`). It no longer fakes a create-time fallback. It also gained `pull_fail_images` and `failing_stops`.
  - Tests: `test_fallback_reference_used_in_wheelhouse_mode` (rewritten), `test_second_pull_failure_on_the_fallback_is_an_infrastructure_failure`, `test_a_wait_failure_that_is_not_a_pull_failure_recreates_the_same_image`, `test_no_fallback_create_while_the_failed_sandbox_may_still_run`, and `test_pull_failure_classified_through_the_real_adapter_raise_path`. The last one drives the real `_CwHandle.wait_running`.
- **Signature check.** `tests/test_sdk_signatures_sandbox.py` drives the real `CwSandboxClient` (modes `headers` and `wandb_auth`) through a recording stand-in for the module. It binds every recorded call with its exact arguments against `inspect.signature` of the real 1.27.0 callable: `Sandbox.run`, `list`, `from_id`, `wait`, `exec`, `write_file`, `read_file` and `stop`; `Process.result`; `OperationRef.result`; `StreamWriter.write` and `close`; and `AuthHeaders`, `WandbAuth` and `NetworkOptions`. Every keyword must land on a named parameter, and `run` must get no positional arguments. The value types and `Process`/`OperationRef` are the real classes, built offline.
  - It also checks every attribute, method and exception class the client uses. It records as absent the class names the client matches that 1.27.0 does not define. It runs the client's mapping on real SDK exceptions, including a `grpc.RpcError` cause for PERMISSION_DENIED.
  - The module is skipped when `cwsandbox` is not importable. Run isolated under `.venv` (`env -i`, `-I`, sockets blocked): 11 tests, all pass, no mismatch, so no change to `sandbox_client.py`.
- **Still open.**
  - L3: if a bad image sits in CREATING with `ImagePullBackOff` until `create_wait_s`, the wait raises `SandboxTimeoutError`, which carries no `status_reason`. That case is not classified as a pull failure, so the re-create uses the same image.
  - The client's create-time fallback (unreachable under 1.27.0, because `run` returns on acceptance) would make `facts.image` name the primary image even though the fallback ran.
