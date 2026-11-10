# Synthetic proposer fixtures

Test inputs for the Claude Code transcript and stream converter. All content is invented. The structure (record types, key names, the join rule, usage repetition) follows what was measured on real 2.1.317 transcripts and the Agent SDK reference.

Generator: `../pr-scripts/make_fixtures.py <this dir>`. Checker: `../pr-scripts/check_fixtures.py <this dir> [<old copy>]`. Regenerate with the generator; do not hand-edit the files.

## Files

| File | What it is |
|---|---|
| `transcript/5e55104e-0000-4000-8000-00000000abcd.jsonl` | Main on-disk transcript, 15 records: 2 `queue-operation`, user prompt, `attachment`, 3 `api-request`, 4 `assistant` (3 API messages; `msg_synthetic_A` is split over 2 records), 2 tool-result `user` records (Bash, Agent), 1 unknown type `synthetic-unknown-record`, `cost-state`. |
| `transcript/5e55104e-.../subagents/agent-a1b2c3d4e5f607182.jsonl` | Subagent transcript, 6 records: prompt, 2 `api-request`, 2 `assistant` (Haiku), 1 Read tool result. Every message record has `agentId` and `isSidechain: true`. |
| `transcript/5e55104e-.../subagents/agent-a1b2c3d4e5f607182.meta.json` | Subagent sidecar. |
| `stream/events.jsonl` | The same session as 13 stream-json events, as upstream's wrapper saves them. Assistant `usage.output_tokens` is the placeholder `4`. |
| `stream/events.usage-limit.INFERRED.jsonl` | 5 events: the expected shape of a usage-limit stop. Never observed. |
| `EXPECTED.json` | Expected converter output (see below). |

## Join and expected values

Each assistant record carries `requestRef`, equal to the `id` of one `api-request` record in the same file. There is one `api-request` per API message (per `message.id`), placed before and timestamped earlier than that message's first assistant record.

| message id | requestRef | request start | effort | model |
|---|---|---|---|---|
| msg_synthetic_A | apireq_synthetic_0001 | 01:00:00.040Z | max | claude-opus-5-5 |
| msg_synthetic_B | apireq_synthetic_0002 | 01:00:15.060Z | max | claude-opus-5-5 |
| msg_synthetic_C | apireq_synthetic_0003 | 01:00:41.040Z | max | claude-opus-5-5 |
| msg_synthetic_S1 | apireq_synthetic_0004 | 01:00:23.330Z | high | claude-haiku-5-5 |
| msg_synthetic_S2 | apireq_synthetic_0005 | 01:00:30.190Z | high | claude-haiku-5-5 |

`EXPECTED.json` keys a test uses: `api_messages`, `transcript_assistant_records`, `api_request_records` (3 main, 2 subagent, 5 total), `request_join` (per message id: ref, start time, model, effort, last record timestamp as span end), `transcript_records_by_type`, `records_to_skip`, `pinned_version`, `tool_calls`, `tokens_from_transcript_deduped`, `tokens_if_NOT_deduped_main_wrong`, `cost_usd_list_price` (0.196784 + 0.001075 = 0.197859 USD), `prices_usd_per_mtok`, `usage_limit_stream`.

## Measured structure versus invented

Measured on real 2.1.317 transcripts (11 local files, entrypoints `claude-desktop` and `sdk-ts`; no `sdk-cli` session exists locally):

- Record type names and their key names, including `requestRef`, `requestId`, `requestedModel`, `effort`, `perTurnEffort` (a string), `apiBlockIndex`, `sourceToolAssistantUUID`, `sandboxed`, `permissionDecision`, and the `api-request` and `cost-state` key sets.
- `assistant.requestRef == api-request.id` (1,549 of 1,564); one message id per ref; `api-request.timestamp` never later than the assistant record; `params.model == message.model`.
- One API message is written as several assistant records (one per content block) with the same `message.id` and identical usage. Transcript `output_tokens` is the final value; stream `output_tokens` is a placeholder.
- `queue-operation` and `cost-state` records have no `version` key; every other record here has `version: "2.1.317"`.
- The type set is open; unknown types occur and must be skipped.

Invented:

- All text, ids, timestamps, token counts and costs.
- `entrypoint: "sdk-cli"` on records (taken from the binary, not seen in a transcript).
- The `api-request` placement for subagent messages: the refs resolve in the same file, as measured, but no local subagent file was inspected for this specifically.
- `querySource: "agent:builtin:general-purpose"` on subagent requests (only `sdk`, `agent:builtin:Explore`, `agent:builtin:Plan` were seen).
- Subagent effort `high`, chosen to differ from the main `max` so a test can tell the joins apart. Subagent assistant records carry no `effort` key, so the effort is available only through the join.
- `perTurnEffort` value `max` (only its type, string, was measured).
- Inner keys of `attachment` (only `type` was catalogued); `base`, `keep`, `tail`, `requestDigest`, `shapeHash` values (opaque locally); `sandboxed: true` (only `false` was seen).
- The record type `synthetic-unknown-record`.
- The whole usage-limit stream.

A converter should not depend on any invented value.

## Checks a converter test should make

From `preflight-readiness.md` L485:

1. `msg_synthetic_A` is counted once, not twice (compare `tokens_from_transcript_deduped` with `tokens_if_NOT_deduped_main_wrong`).
2. The subagent's two messages are attributed to `claude-haiku-5-5`.
3. De-duplicated token totals per model and list-price cost equal `EXPECTED.json`.
4. The `requestRef` join gives each message's start time and effective effort equal to `request_join`; the span ends at `last_record_timestamp`. 5 `api-request` records are read.
5. Stream assistant `output_tokens` are ignored; output counts come from the transcript (or `result.modelUsage`).
6. Unknown record types (`synthetic-unknown-record`) and `attachment`, `queue-operation` records are counted and skipped without error.
7. A record with another `version` fails the gate: copy the fixture, change one record's `version` (for example to `2.1.318`), and expect a loud failure. Records without a `version` key (`queue-operation`, `cost-state`) are as measured and must not trip the gate.
8. The usage-limit stream is classified as a limit stop with a wait until `1794286800`.
