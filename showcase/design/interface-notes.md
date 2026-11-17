# Interface notes from the owners of earlier layers

What the built code does where the spec was silent or contradicted itself. The code is the truth;
these notes point at what to look for. Choices listed under 'Spec rulings' are now binding.

## Layer 0

### L0-frames

- FrameDecoder also exposes pending_chars (chars held for the one partial frame); extra, read-only
- MAX_RAW_LEN = 16 MiB added; encoder raises FrameError when raw or b64 exceeds 16 MiB
- feed() returns [] for non-str input; the decoder also rejects bad header characters as soon as they arrive
- Decoder requires b64_len % 4 == 0, raw_len >= 1, zlib stream eof, meta a JSON object
- encode_frame uses allow_nan=False; NaN in meta raises FrameError

Spec rulings:

- 2.9 limits: Header limit does not say whether 256 counts the newline -> Header line without its newline may be at most 256 characters
- 2.9 decoder rule: Which discarded characters count as noise on a rejected frame is not stated -> Magic (header reject) or header plus received payload (payload reject) are added to noise_chars

### L0-sse

- merge_stream output: {'choices': [] or [{'index':0,'message':{'role','content',['reasoning'],['tool_calls']},'finish_reason'}], ['id'], ['model'], ['usage'], ['error']}
- Only choice index 0 counts as a choice; a usage-only chunk with choices [] is not a choice
- merge_stream keeps H2 tool_call merging in message['tool_calls']; to_completion_body never emits tool_calls (gateway text-only rule must check merged)
- iter_sse accepts bytes or str items and splits an item holding several lines on b'\n'; pass raw chunks only if they end on line boundaries
- to_completion_body emits id None when provider sent no id
- sse_truncated.txt ends with a cut JSON line (parses as chunk, chunk, bad); sse_error_only.txt is an error chunk with code 400 context-length text then [DONE]
- Fixtures use model openai/gpt-oss-120b; sse_ok usage 120/16/136 with reasoning 'Think about it.'

Spec rulings:

- 3.8 merge_stream: Spec does not say under which key merged reasoning is stored when both wire names appear -> Both names are concatenated in chunk order under message['reasoning']; reasoning_field() reports the first wire name seen
- 3.8 to_completion_body: No rule for 'id' when the provider sent none -> id is the merged id or None (not invented)
- 3.8 iter_sse: Payload of a 'bad' event and handling of 'data:' without space unspecified -> bad carries the full line minus terminator; 'data:' with or without one space accepted; non-object JSON and bad UTF-8 are bad
- 3.8 merge_stream error: 'has an error key' vs error: null -> error is kept only when the value is not None
- 3.8 merge_stream finish_reason: Placement in merged result unstated -> choices[0] always has finish_reason key (last non-null or None) when a choice was seen

### L0-capture

- Sidecar defaults: --max-text-chars 20000; file opened O_APPEND (not truncated); unwritable path -> one stderr line, job continues
- Capture replaces sys.path[0] (its own bin dir) with cwd, matching python -m; bin/ siblings are not importable by the worker
- call.kwargs keeps scalar values only; non-scalars become '<type>' so no text leaks
- splits.sizes always has train/val/test (val 0 for the two-way loader); duplicate_keys = keys with >1 (split,index)
- eval rows not found in index: split/index null; evaluator exceptions write no eval row but drain the thread lists
- fake_pkg uses os.environ (TEXT_CLASSIFICATION_LLM_CACHE_DIR, OPENROUTER_API_BASE/KEY); exclude tests/fixtures from the env-access source check
- fake_pkg data: datasets USPTO, Symptom2Disease, LawBench, DupKeys; any size; no shuffle; questions end 'hint: <answer>' (target 2 in 3)
- fake_pkg built-ins: Symptom2Disease test[1]==val[0]; DupKeys val[2]==val[0]; default sizes train 4, val 5, test 6
- fake_pkg llm posts JSON to --api-base or $OPENROUTER_API_BASE/chat/completions, bearer $OPENROUTER_API_KEY, no proxies; retries like upstream with sleeps <=0.5s
- fake_pkg default --memory is agents/ok_agent.py; ok_agent makes no call while learning; data.py loads standalone (no relative imports)
- Test file defines its own needs_example_venv and loopback server; it does not import tests/helpers.py or util.loopback_guard (same layer)

Spec rulings:

- 2.8 eval.ambiguous vs 3.15: 2.8 says ambiguous when the key maps to more than one (split, index); 3.15 says only when several indices remain after the wanted split wins -> Followed 3.15: the shared Symptom2Disease question is ambiguous=false in its wanted split
- 2.8 schema tag: 2.1 says every record carries schema; the 2.8 example shows it only on the meta row -> Followed 2.8: schema only on the meta row
- 3.15 H2 batch hashing: Calling self._truncate again duplicates upstream's truncation warning on stderr for prompts over 224k chars -> Kept the spec's self._truncate call; a non-list/tuple prompts iterable is passed on unhashed so it is not consumed
- 3.15 done row: No exit value given for a non-Exception BaseException (e.g. KeyboardInterrupt) -> exit null, exception = class name; SystemExit maps code as Python does (None->0, int->int, else 1)

### L0-core

- errors: fields are keyword-only and checked; an unknown kwarg raises TypeError; exceptions pickle; exit_code_for(exc) maps to the 3.1 exit codes
- errors: CampaignStop raises ValueError for a reason outside STOP_REASONS; BudgetError kind 'ceiling' needs detail in requests/attempts/job_usd
- errors: ExecutorError.retryable is set from kind (False for image_mismatch/auth/billing/permission); passing True for those raises
- util.loopback_guard raises util.NonLoopbackConnect (a PermissionError) on connect, connect_ex and sendto; it is re-entrant and allows AF_UNIX
- util.tree_hashes skips exclude_dirs by name at any depth, skips sockets and fifos, and raises WorkspaceError on any other symlink
- util.micros refuses float and bool with TypeError; usd(int) gives 6 decimals; write_json leaves files at mode 0600
- util.append_key_digest keeps one entry per sha256 and returns the existing entry for a repeat; the value must be non-empty
- wire also exports SPLITS, DATASET_RE, JobIdentity.problems() and .validate(); HandlesRequests is runtime_checkable
- paths also has import_checks_root, proposer_root and iterations_root; every method raises ValueError on an unsafe component
- scan: scan_bytes offsets are bytes and scan_verbatim offsets are characters; overlapping run hits merge into one finding
- scan: scan_obj paths look like '/key', '[i]' and '/key#key' (a hit inside a dict key); scan_paths walks directories and skips symlinks
- scan: known values and digests apply only under class 'secret'; scan.ALL_RULES lists the 16 rule names a receipt counts
- helpers.tmp_state(campaign=None, *, manifest=None|True|dict, create=True) yields TmpState(sc, campaign, paths, manifest_path, manifest)
- helpers.make_manifest takes a dict as its first argument or **{'a.b': v}; a__b means a.b; helpers.DELETE removes a key
- helpers.FakeGateway(replies, default=, require_registration=True, delay_s=): unregistered jobs get unknown_job and a halt gets halted
- helpers.FakeGateway: .calls log, .add(), .set_halt(), .request_kill(), peak_in_flight; a scripted item may be a reply, an exception or a callable
- helpers also has completion_reply, completion_body, child_env, venv_child_env, example_python, tokenizer_dir, neutral_problems, SC, FIXTURES

Spec rulings:

- 5 manifest_valid.json, claude-haiku-5-5 list price: EXPECTED.json prices_usd_per_mtok has no cache_write_1h for haiku, but 2.3 needs all five fields -> Set cache_write_1h_per_million to "0.2" (twice the input price, the same ratio as opus); other four copied from the fixture
- 5.2 test_util test_micros_rounds_up (ported): H2 at 31b0f82 has no micros test to port, only the function in budget.py -> Wrote a new unittest case and gave test_util.py the MIT header; util.py has the Apache header
- 3.2 util.write_json / append_jsonl: The spec does not give the file format or mode -> indent=2, ensure_ascii=False, NaN allowed, mode 0600; JSONL is compact with mode 0600
- 3.2 JobIdentity: Field rules for iteration and dataset are loose -> from_dict/validate need iteration >=1 for phase search and None otherwise; dataset fullmatches [A-Za-z][A-Za-z0-9_-]{0,47}; the constructor does not validate
- 3.2 read_jsonl, open_sqlite_ro: The spec does not say what happens when the file is absent -> read_jsonl returns []; open_sqlite_ro raises FileNotFoundError and creates nothing

## Layer 1

### L1-workspace

- Read STATE/journal.jsonl with mhforge.journal.read_journal(path), not util.read_jsonl (torn-tail marker lines)
- Journal non-terminal statuses are 'started' and 'handle'; every other status closes the attempt
- Journal.add_handle(op, key, attempt, handle) is how on_handle should journal a handle; open_handles returns the handle dicts
- Journal.recover() leaves attempts this Journal instance started alone; its interrupted entries have detail {started_seq}
- Journal.entries(since_seq=n) returns seq > n (exclusive); seq starts at 1
- Journal.attempts(): seq is the first start, status/detail are of the latest entry for that attempt number
- Journal.record refuses detail over 65536 bytes canonical JSON or non-plain JSON (ValueError)
- validate_candidate_file reasons: invalid_name, pinned_name, agents_dir_missing, agents_dir_not_a_directory, missing, symlink, not_a_regular_file, too_large, too_many_agent_files
- workspace.snapshot raises WorkspaceError on any symlink in WS (util.tree_hashes); run unknown_entries + quarantine_entries first
- unknown_entries returns top-most entries only; quarantine_entries refuses agents/*, pinned files, pinned dirs and logs/ (WorkspaceError, nothing moved)
- quarantine utc stamp may be a utc_iso string (colons allowed); duplicates get a .dup<n> suffix
- workspace.check_upstream_contract(up, ws, pinned) and UPSTREAM_MODULES are public; load_upstream raises MhforgeError when yaml is missing
- ensure_workspace recopies lock_file to WS/uv.lock whenever it differs; pristine copied_utc uses the system clock

Spec rulings:

- 3.14 env_fingerprint / 2.3 path rule: Spec says procenv.venv_root is the one function for the venv root, but procenv belongs to L1-manifest (same layer, cannot import) -> Private workspace._venv_root with the identical lexical rule (abspath parent.parent, requires pyvenv.cfg)
- 2.1 / 2.7 / 3.16 journal torn tail: Appending after a torn last line without truncating turns it into a bad middle line, which 2.1 readers must reject -> Writer prefixes a newline plus a (journal, torn_tail, terminated) marker naming the torn bytes; journal.read_journal skips exactly that line
- 3.16/3.18 on_handle: Spec puts handle in the started detail but on_handle fires after started is journaled; append-only file cannot amend it -> Added Journal.add_handle(op,key,attempt,handle) writing non-terminal status 'handle'; open_handles reads started and handle entries
- 3.16 Journal.recover: If a new run started is journaled before recover(), the live run would be marked interrupted -> recover() skips instances started by the same Journal object; interrupted carries detail.started_seq to close exactly that instance
- 1 Rules: target 500 lines: workspace.py is about 700 lines; the row allows no extra file to split into -> Kept one file; reported
- 3.14 memory_dir_fingerprint: Location of the auto-memory dir is not given -> Uses <home>/.claude/projects/<cwd with non-alnum as '-'>/memory (prefix match over 200 chars) [inf]; None when absent
- (review) Read STATE/journal.jsonl only with journal.read_journal or Journal. After a torn-tail repair, util.read_jsonl raises StateError on the terminated torn line. Today manifest._journal turns that into '_unreadable', which blocks search and evaluate --test, and state._rows falls back to a lenient parse.
- (review) Journal entries can include op 'journal' torn_tail markers and status 'handle' rows (from add_handle). entries() returns both, so filter by op and status. last() and attempts() skip handle rows. open_handles() reads both started and handle rows.
- (review) recover() interrupted entries have detail {started_seq, outcome:'interrupted', infra:true, failure_class:null}, and recover() leaves alone attempts that the same Journal object started.
- (review) workspace.snapshot raises WorkspaceError on any symlink in WS outside logs/.venv/__pycache__/.empty_plugins (util.tree_hashes rule), including agents/x.py as a symlink. A proposer runner must catch it or quarantine first.
- (review) load_upstream trusts pristine.json's tree_sha256 and has no manifest of its own. Run ensure_workspace (which compares the pin with the manifest) before it at every start.

### L1-telemetry-pure

- call_records fixtures are named <kind>.json: completed, replayed, refused, provider_rejected, failed, truncated_empty
- Fixture join: 2 learn records (rejected, failed), then val examples 0-3 -> completed, truncated_empty, replayed, refused (via batch)
- Problem strings: unclaimed_call:<id>, miss_without_record:<seq>, no_done_row, admission_only:<id>, sidecar_*:<seq>
- parse_sidecar adds no_done_row when it has rows but no done row; it raises StateError on a sidecar schema major other than 1
- Example call entries have input/output_tokens None when usage is null (row sums count it as 0); latency_s is ended_utc minus received_utc
- A cache hit refers to the record first taken for its hash, so it can add that call to another example's calls
- ToolUse.result_text is None only when no result was found; bash_results uses that
- request_efforts leaves out messages with no effort (no request record, or the stream)
- Stream: agent_id falls back to parent_tool_use_id; skipped_types counts system events other than init as 'system'
- The limit stream gives one message with model '<synthetic>'; price_usage then returns None (no price for it)
- price_usage returns None when a nonzero counter has no price field; it rounds up once over the total
- read_session_dir raises FileNotFoundError without the main file and TranscriptError(shape) over max_bytes or on a symlinked main file
- policy.portable labels each roots key as <key>, longest root first, and turns any other /Users/<name> into /Users/<user>
- policy.check_attributes also rejects str values that scan.scan_bytes flags as secrets
- policy.parse_rule and MAX_ATTRIBUTES are also exported

Spec rulings:

- 3.21 join: Step-5 join problems have no return path: join_examples returns rows and call_roles returns a dict -> Added join.join_problems(sidecar, records, split): the sidecar's own problems plus the join problems
- 3.21 transcript: Subagents attach to their Agent tool use, but NormalizedSession has no field to say which one -> Added NormalizedSession.agent_links (agent id -> Agent tool_use id), defaulting to {}, as the last field
- 3.21 join step 2: No rule for call rows with an error, or with cache_hit null, against refused/rejected/failed records -> An error row claims the next non-success record with its hash; leftovers go to the last error row with that hash
- 3.21 join step 3: Contents of an ambiguous example's row are unstated -> matched false, calls [] and zero tokens; its records get the role of the split with example_id None
- 3.24 apply_text truncate: Unclear whether <chars> and the sha256 in the cut marker cover the cut part or the whole text -> <chars> is the number of characters cut; the sha256 is the first 12 hex of the whole original text
- 3.24 policy (P) header: Spec cites H2 report_v4.py L505-L511 for portable, but at 31b0f82 the function starts at L494 -> The header cites report_v4.py (portable) and gives no line numbers
- 3.21 join step 4: Comparing host received_utc with the worker's eval t is unreliable across clocks -> A claimed record is learn if its row's seq comes before the first wanted eval; only unclaimed records compare times
- (review) price_usage returns 0 (not None) for empty or all-zero usage, and cost_state.total_cost_micro can be 0. L2-proposer's fallback chain must treat 0 as missing to keep 'never zero'.
- (review) price_usage returns None for the whole session if any message's model has no price entry, even at zero tokens. The CLI's '<synthetic>' error messages (see the limit stream) will push the basis to the estimate.
- (review) attribute_dataset follows the contract (dataset name or dir anywhere in the text), not the path-only prose. A tool call like 'cat data/*/val.jsonl; echo USPTO' gets USPTO's rules. L2-spans should run policy.guard_text on attributed tool results too, or protected datasets' text can leak under a looser rule.
- (review) join attaches error rows (refused, rejected, failed) to examples, so such an example is matched:true with calls carrying input_tokens None. A cache hit on a prompt shared by two examples puts the same record in both rows; sum tokens per call_id, not per row.
- (review) NormalizedSession.agent_links (extra last field, default {}) maps subagent id to the Agent tool_use id; join.join_problems is the extra problems accessor.
- (review) A sidecar whose rows are all invalid is treated as no sidecar: join_problems returns ['no_sidecar'] and drops the sidecar_bad_* entries.

### L1-provider

- provider extras: settle_basis(result, disposition) gives usage/rejected/bounded_unknown; circuit_outcome(d) gives success/neutral/failure
- provider extras: usage_well_formed, StreamAccumulator, error_from_body, filter_headers, mask_key, DISPOSITIONS, ERROR_KINDS, FORGE_BASE_URL
- key_facts: stub kind with no key gets len and sha256 None; CLI must skip append_key_digest when sha256 is None
- scrub(name) is called whenever given, even if the variable is absent; name is popped from environ before any config error is raised
- complete() strips usage, response_format, stream and stream_options from the body, then adds the stream fields; Host and Content-Length are also sent
- ProviderError.code is always str or None (an int code 400 becomes '400'); body_excerpt is the masked raw body or error JSON
- first_byte_s: first SSE data line on a 200, status line otherwise; the sink stops after the [DONE] line (no trailing blank line)
- Fault.arg: http accepts int, (status, error) or {status, error, headers}; hang accepts seconds or (seconds, data_lines_first); error_stream accepts the error object
- Fault.when 'always' ignores times; StubProvider latency_s and hang use clock.sleep (real sleep when clock is None)
- render_sse reply keys: content, reasoning, reasoning_field, finish_reason, usage, error, done (False drops [DONE]); model None omits model
- serve_stub returns StubServer: .requests (Authorization redacted, authorization_sha256 kept), .calls, .peak_in_flight, .close() releases hangs
- test_provider child script uses 'from os import environ' inside a string; an env-access source check over tests/ may need to allow it

Spec rulings:

- 3.9 complete headers: OpenAI-Project comes from manifest.openai_project, but that module belongs to another layer-1 owner and I may not import it -> Computed it locally: provider.openai_project, else entity/project; forge with neither raises ProviderConfigFault; stub uses mhforge-offline/stub
- 3.9 classify table: Statuses the table does not name (405, 410, 201 and so on) have no row -> They classify as retry_transport (booked rejected, circuit failure)
- 3.9 classify table: Row 1 'no error' versus row 4 'timeout after status line' is ambiguous when the stream already carried a finish_reason and usage -> A stream cut after a choice, finish_reason and well-formed usage gets error None and classifies ok (the complete-without-DONE rule)
- 3.9 ProviderError kinds: Failures after send with no status line (reset, bad status line) fit no row; only connect and timeout have rows -> Reported as kind timeout with http_status None, so they book bounded_unknown, never rejected
- 3.9 classify row 2: The table says the error-chunk code must be an integer; providers may send it as the string '400' -> Digit strings are accepted as integer codes too
- (review) ForgeProviderClient.complete takes deadline_s as relative seconds (the gateway passes provider.attempt_deadline_s). Each attempt starts one daemon Timer thread that enforces the deadline in real seconds, derived from the injected clock at start.
- (review) Statuses the table does not name (405, 410, 201 and so on) classify retry_transport with ledger 'rejected' and circuit failure. Reset or bad status line after send gives kind timeout with http_status None, booked bounded_unknown.
- (review) A stream with choice, finish_reason and well-formed usage but no [DONE] gets error None and classifies ok, with done False kept. The no_usage stub fault classifies ok with settle bounded_unknown, so the gateway must charge the reservation.
- (review) provider.py helpers used by other layers: settle_basis(result, disposition) returns usage|rejected|bounded_unknown; circuit_outcome(disposition) returns success|neutral|failure. A well-formed usage block on a 200 always wins the settle.
- (review) OpenAI-Project comes from provider.openai_project, else entity/project. With kind stub and neither set, mhforge-offline/stub is sent. With kind forge and neither set, ProviderConfigFault is raised.

### L1-ledger

- ledger.connect(path), ledger.transaction(path) and ledger.create_file(path, ddl) are the shared 2.10 SQLite helpers; replay.py uses them
- ledger.FULLFSYNC is a module flag (sys.platform == 'darwin'); every connection sets synchronous=FULL
- ledger.prices_from_manifest(manifest) returns {'input','cached','output'} Decimals per million (micro-dollars per token)
- ledger.judge_usage(usage, prices, in_lim, out_lim, reserved) -> (status, cost, reason) is the pure settle table
- Ledger.__init__ raises StateError for a different campaign or schema_version; meta.manifest_sha256 = sha256(canonical_json(manifest))
- Ledger does not validate the manifest; it reads prices, gateway limits and budgets at init
- register_job validates the JobIdentity (ValueError); token_sha256 must be 64 lower-case hex
- settle with an unknown ledger_id raises StateError; a second settle raises BudgetError(kind='settled')
- rejected rows store reason '<rejection>: <reason>'; limit_exceeded rows store reason 'limit_exceeded[; reason]'
- set_halt validates the reason against errors.HALT_REASONS (ValueError otherwise)
- Each meter has a fixed unit (ms, bytes, spans, micro_usd, bytes, tables); a different unit raises ValueError
- lens_bytes and lens_spans need a bucket in wire.LENS_BUCKETS and are one-shot only; meter_open refuses them
- For proposer_list_price, micro_usd defaults to quantity in meter, meter_open and meter_close
- check_meter_cap('proposer_list_price') refuses when budgets.proposer_list_usd is null; lens_spans never refuses
- MeterCapExceeded for non-lens meters has bucket=None
- snapshot()['halt'] is a dict or None; snapshot()['meters'] has every wire.METERS key
- replay.Lease.leader_done is an extra property; a None from wait() means call begin() again (safe in every case)
- replay: commit() with a non-200 raises ValueError; a 200 with no choices is not stored but still wakes followers
- replay: bypass leaders are not entered in the in-flight table, so nothing coalesces onto them
- replay.request_sha256 hashes only the four keys that are present (an absent key is omitted, not null)
- circuit.json is {'schema':'mhforge.circuit/1','failures','until_epoch'}; another schema raises StateError
- Circuit.enter polls every 50 ms of real time and reads time from the injected clock; FakeClock tests advance it from another thread
- Circuit.finish raises ValueError for anything other than 'success', 'failure' or 'neutral' (including bools)

Spec rulings:

- Rules for every file: target size: ledger.py holds the full 3.6 contract and the 2.10 DDL and comes to 648 lines, over the 500-line target -> Compacted the small methods; left it over the target rather than drop contract surface
- 3.6 check_meter_cap lens_bytes: It is unclear what bucket the exception names when the total cap, not the bucket cap, refuses -> MeterCapExceeded.bucket is the requested bucket in both cases; the message says 'total' when the total refused
- 3.6 reserve and register_job: The spec does not say what happens when reserve is called for a job that was never registered, or for an older attempt -> reserve registers counters implicitly for a new or newer attempt; an older attempt is checked but does not add to requests
- 3.6 settle table, partial usage: 'nothing contradictory' is not defined for partial usage -> Partial usage is unknown when a present counter is malformed or its cost floor exceeds the reservation; otherwise bounded_unknown
- 3.6 Settlement.basis: No basis value is given for each status -> complete=usage, bounded_unknown and unknown=reservation, rejected=none
- 3.6 reconcile and clear_halt errors: No exception types are given -> Bad note or spend raises ValueError; a row that is absent or not 'unknown' raises StateError; clear_halt with unknown rows raises BudgetError(kind='unreconciled')
- 5.2 test_totals_equal_sums_after_random_operations: With fullfsync on darwin a commit takes about 13 ms, so 2,000 operations take about 50 s -> That one test patches ledger.FULLFSYNC to False; test_connections_set_synchronous_full checks the real pragmas
- (review) sandbox_seconds: meter(), meter_open() and meter_close() now need micro_usd / worst_micro, or they raise ValueError.
- (review) check_meter_cap and meter() run in separate transactions and are not atomic together. The design assumes one writer per meter: the exporter writes the lens, wandb and table meters, and the runner writes the sandbox and proposer meters.
- (review) Circuit.finish from any caller clears the probe flag, as the contract says. So a request admitted earlier that finishes while a probe is in flight lets a second probe in.
- (review) After a wall-clock step back, the circuit's cool-down is re-anchored to at most cooldown_s from now. Threads waiting in enter() still time out on the injected monotonic clock.
- (review) reserve for an older job attempt, after a newer one was registered, does not add to job_counters.requests. Only the attempts and job_usd hard bounds limit it.
- (review) reconcile with a spend above SQLite's int64 range raises OverflowError, not ValueError.

### L1-manifest

- manifest.path_allowed(path, entries) is public: the AMENDABLE/IMPORT_DIFF_ALLOWED matching rule, for qualification.import_problems
- live_blockers raises ValueError for an unknown command, for evaluate without a mode in EVALUATE_MODES, or for a bad key_source
- key_source accepts env, prompt, fd and fd:N; prompt and fd skip every key row
- Blocker fields: dotted manifest paths, or --live, manifest, frozen, halt, offline_gate, qualification/passed.json, journal, finalized.json, protocol.md, amendments
- More blocker fields: environ.<NAME> and results.<dataset>.<system>
- gate_problems become ('offline_gate', name) blockers for every command, account-check included
- live_blockers validates first; if the manifest is invalid it returns the --live and ('manifest', problem) rows only
- The search calibration row reads its own calibration.json first, then sibling(import_from) only if no passed.json diff path starts with calibration. (LD3)
- The search row reads protocol.md at paths.root.parent.parent; a missing file counts as unsigned in a measured campaign
- freeze validates, writes manifest.json with sorted keys (mode 0600), manifest.sha256 as hex plus newline, and engine.json only when absent
- read_manifest raises ManifestError (not StateError) for a missing file or bad JSON
- append_amendment needs paths.manifest to equal the given manifest; it validates the effective manifest with SC = paths.root.parent.parent
- StateLock(lock_file, command, clock=None, *, host=None) creates the parent directory; StateLockError.holder is the body that was read
- status() always returns the full key set; the halt dict has reason, detail, call_id, set_utc; lens MB means bytes / 1048576
- procenv.PLACEHOLDERS is public; proposer_env raises ValueError for an unknown placeholder and drops entries whose {uv_dir} cannot be resolved
- exporter_env raises ValueError if any of the three key variable names would appear; minimal_env ignores base
- IMPORT_DIFF_ALLOWED is written out from the 3.22 list (entries ending in '.' match everything under them)

Spec rulings:

- 1 Rules (500 lines per file): manifest.py's closed schema, 21 rules, live gates and amendments come to 1038 lines -> Kept it in one file because my row lists exactly these files; nothing split out
- 2.4 runner.lock body vs 3.27: The lock body has a host field, but only cli.py may read the host name -> Added keyword-only StateLock(..., host=None); the body records whatever the CLI passes, or null
- 3.4 status ledger_view: Only current and possibly_stale are defined; a missing or unreadable ledger is not covered -> Added absent (no ledger file) and unreadable (open or query failed)
- 3.4 status export.pending: pending is not defined -> pending = receipts in planned/scanned/emitting/emitted plus units ready in the journal that have no receipt at all
- 3.4 status jobs.running: Not defined -> Counts journal job entries with status started and no later terminal entry, whether or not the lock is held
- 3.3 live_blockers engine_sha256: Nothing says what happens when engine_sha256 is None -> None skips only the engine comparison with passed.json; existence and manifest_sha256 are still checked
- 2.3 live gates, must-be-unset variables: 3.3 says presence and non-empty are tested; for 'absent' variables an empty value is ambiguous -> Key rows use bool(environ.get); must-be-unset rows (CLAUDECODE, WANDB_*, OTEL_*) refuse whenever the name is in environ, even if empty
- 2.3 rule 1: No key-set rules for dataset_dirs, sizes and forecast maps; placeholders and qualification.system/dataset are not checked -> Added checks: keys must equal datasets, placeholders must be one of the three, qualification.system/dataset must be in baselines/datasets, decimal fields non-negative
- 5 manifest_valid.json fixture: Checked against the draft: it differs in exactly the fields 5 lists -> No change to the fixture
- (review) cli: live_blockers/require_live now refuse search and evaluate when engine_sha256 is None, so always pass qualification.engine_snapshot's hash
- (review) L3-export: read_amendments and effective do not validate the result; call manifest.validate(effective, ...) yourself, or rely only on the live_blockers export gate, before you use content rules
- (review) cli amend: append_amendment takes no lock, so hold export/export.lock around it (2.7). freeze takes no lock either, so call it under the runner lock
- (review) StateLock raises OSError (ELOOP), not StateLockError, when the lock path is a symlink
- (review) status takes a brief shared flock on runner.lock (as the spec says), so a runner starting at that instant can get StateLockError with the previous holder's stale body. Retry once in the CLI
- (review) procenv.worker_env and proposer_env now raise ValueError on any credential-shaped name; worker_env allows only OPENROUTER_API_KEY
- (review) util.append_jsonl does not repair a torn tail: a row appended after a crash is glued to the fragment. The journal owner must handle this (manifest.py repairs amendments.jsonl itself)

### L1-stub

- Refusal headers are wire.refusal_reply's: content-type, x-should-retry false, x-mhforge-source gateway
- stub.txt is JSON lines; the last {event: summary} row has unauthorized, local_refusals, frames_in/out, attaches
- Stub writes run/stub.ready {pid,port} in every unix-uplink run, with or without --daemonize
- serve exits 2 before forking on a bad job file, bad --uplink, or a socket path over 90 bytes
- After the worker leader exits the stub SIGKILLs the worker's whole process group (it reaps stragglers)
- On SIGTERM the stub SIGKILLs the worker group and exits 143 without writing exit.json
- unanswered_at_exit counts only pending requests already sent on some uplink
- Daemon HELLO.pending and the resent REQs are ordered by rid number; a resent REQ is byte-identical
- Stub forwards only the RES headers content-type, x-should-retry, x-mhforge-source, plus its own Content-Length
- load_job requires a non-empty argv, so the stub refuses import-check files (exit 2)
- Selfcheck --import-check execs sys.executable -c IMPORT_CHECK_CODE; FOREIGN_CLASS prints module.qualname
- Selfcheck exits 0 even when the job file is unreadable, adding 'job_file: unreadable' to expect_problems
- freeze_sha256 de-duplicates identical name==version lines; distributions counts the lines after that
- expect_problems entries start with 'machine:', 'python:' or 'freeze_sha256:'
- macOS adds __CF_USER_TEXT_ENCODING to every child env; tests filter it out of env-equality checks

Spec rulings:

- 3.13 serve step 6/7: No rule for BYE in daemon mode while the worker is still running -> Ignored it and logged it to stub.txt; BYE only ends the linger once the worker has ended
- 2.6 exit file / 3.13 step 6: returncode for reasons killed and uplink_lost is not given (only timeout=124) -> Wrote the worker's real return code (a negative signal number); 127 if argv cannot start
- 3.13 step 5: Spec does not say whether a reply timeout sends CAN -> A reply timeout also drops the request and sends CAN, so the host's live count shrinks
- 3.13 selfcheck report: Shape of egress.probes without --probe-egress and source of cache_writable are not given -> Without the flag every probe is listed as skipped; cache_writable tests job.env TEXT_CLASSIFICATION_LLM_CACHE_DIR (False when absent)
- 5.2 test_import_check_no_class_exits_4: fake_pkg has no agent without a MemorySystem class -> The test copies fake_pkg to a temp dir and adds agents/no_class.py there; no fixture edited
- (review) Stub sends CAN on reply timeout as well as on client-gone. relay.py must treat CAN for any rid as 'dropped', including rids it is still serving.
- (review) A CAN is lost when no bridge is attached (daemon mode). The host must reconcile against HELLO.pending on each attach: a rid it holds that is not in pending has been dropped.
- (review) BYE in daemon mode is ignored while the worker runs; only a BYE after the worker ended ends the linger. Send KILL to stop a running worker.
- (review) exit.json returncode for reasons killed/uplink_lost is the worker's real code (negative signal number); 127 when the worker could not start (reason stays 'exit').
- (review) Stdio: EOF on fd 0 after KILL gives exit 75 with reason 'killed'. The process exit code alone does not tell KILL from link loss: read exit.json reason.
- (review) Selfcheck cache_writable tests job.env TEXT_CLASSIFICATION_LLM_CACHE_DIR (creates it); False when absent. Without --probe-egress every probe, gating or not, is 'skipped'.
- (review) Stub RES header values must be latin-1 encodable: a non-latin-1 value from the host closes the worker's connection with no response (not handled; host is trusted).

## Layer 2

### L2-tables

- plan_tables wrapper also has backend, export_attempt and dropped; backend 'off' gives tables [] and no dropped rows
- span_urls must be dataset -> verified-receipt URL; only the exact conversation URL of the job's ok attempt (any .e<k>) is accepted, else ''
- plan_tables.table_backend(manifest) maps decisions.evaltables_backend to ces|wandb_table|off|unset (null and 'unset' are unset)
- Only status 'succeeded' jobs get tables; skipped_usable, failed and halted get none and are not listed in dropped
- Artifact dicts: log has files[] and withheld_files[] (name, sha256, bytes); use has alias=campaign; base use in propose has best_effort true
- expect.artifacts leaves out best_effort uses; expect.summary_markers = table_keys as passed
- Exporter must supply file bytes via proposal_entry['bytes'] (baselines may pass a minimal entry too) or fill files[].bytes itself
- withheld is looked up by '<name>.py' (also the source path, candidates/<name>.py, agents/<name>.py)
- Iteration-record dataset accuracy is read as a fraction and turned into percent (x100) for avg/best/improvement
- search_metrics dicts carry a 'cumulative' sub-dict of micro sums; improvement is 0.0 when there is no earlier best
- Search history row leaves out keys whose value is None; config is {} for iterations > 0
- manifest_sha256 in run config is manifest.manifest_sha256 of the manifest passed in (the effective one when the exporter passes that)
- Proposer metadata strings go through policy.guard_text and then policy.portable({}) so no /Users/<name> survives

Spec rulings:

- 2.14 / 3.23 matches_result_file: Text compares only the true-row count with correct; fraction-true equal to accuracy also needs the row count to equal total -> The check requires true rows == result.correct AND row_count == result.total (same as join.check_examples)
- 2.14 dropped table: Says the table is dropped with the reason recorded, but the tableplanset wrapper has no field for it -> Added wrapper key dropped: [{key, job_id, reason, failed_checks}]; a table failing any check goes there, not in tables
- 2.15 run config vs 3.23: 3.23 says config is only the allow-listed manifest subset; the 2.15 example config holds candidate, split, iteration, manifest_sha256 etc. -> Config = portable(run_config(manifest, ALLOW)) plus top-level identity keys (candidate, split, iteration, base_system, candidate_sha256, upstream_commit, manifest_sha256, session_key)
- 2.15 artifact files[].bytes: No input to plan_evaluate_run or plan_propose_run carries the candidate file size -> bytes is taken from proposal_entry/candidate_entry 'bytes' when an int, else null
- 2.15 search history row / export attempt: Retried row must carry search/export_attempt=k yet be equal to the first row -> search/export_attempt is added only for k >= 2; all other keys are identical
- 2.14 long label hash: Sidecar text is cut at max_text_chars, so a hash of the held text may differ from the whole-text hash -> Hash form uses the row's whole-text *_sha256 when it is 64 hex, else the hash of the held text; chars = held length
- 3.23 search/upstream_frontier_val: The iteration record has no frontier-after value -> Running max of frontier_before.val_accuracy and upstream_avg_val of valid, non-quarantined candidates up to this iteration
- (review) tables.json adds wrapper keys backend, export_attempt and dropped: [{key, job_id, reason, failed_checks}]. Treat each dropped entry as a skipped_policy table.
- (review) plan_tables accepts a trace_url only for conversation <campaign>.<job_id> or its .e2 to .e9 forms, in the manifest's project. Export attempt 10 and later get an empty string.
- (review) Example rows with duplicate example_id are not rejected. Only the true count and the row count are compared with the result file.
- (review) Under the 2.14 contract, label datasets (Symptom2Disease, LawBench) keep predictions of 200 characters or less in clear text, and the exporter's scan does not apply the verbatim check to table rows.

### L2-spans

- verify_spans reads rows shaped like the H2 read-back (operation_name chat/execute_tool/invoke_agent); module docstring lists fields
- Row attributes are read from row['attributes'] (flat or nested) and/or raw_span_dump JSON 'attributes'
- Row times: naive or Z/+00:00 ISO text, or epoch s/ms/us/ns numbers; tolerance 1 ms
- Matching keys: llm by <prefix>.call_id (proposer llm call_id = message id), tool by tool_call_id
- Matching keys: subagent by <prefix>.agent_id attr, turn by <prefix>.turn_key attr (else agent_name)
- Mismatch strings: '<kind>:<id>:<field>', 'missing:<kind>:<id>', 'unexpected:<kind>:<id>'
- verify_spans: plan error must come back with status_code ERROR, and no error must not
- verify_spans: tool results may also match by JSON equivalence (ported from H2)
- conversation_ids_body is {project_id, include_details false, include_costs false, limit, offset} with no query
- Outer-loop tool spans have tool_call_id/key 'iterNNN.<name>'; conversation id is '<campaign>.search.p<n>'
- Solver turns carry attrs turn_key, examples, correct, replayed_calls, refused_calls; part/parts only when split
- Solver plans skip call records whose job.job_id differs from the attempt's id and list them as foreign_call problems
- Proposer llm usage is null for a stream-source session because its output counts are placeholders
- plan_proposer takes no verbatim index, so attributed tool results are not guard_text'ed (L1 review asked for it)
- validate_plan checks each string inside outer-loop JSON summaries, not the whole JSON text
- tests/test_readback.py imports builders from tests/test_plan_spans.py

Spec rulings:

- 3.23 solver job, replayed/coalesced spans: 2.5 gives these records response null, so there is no reply text to plan -> Input text hashed, output_messages []
- 3.23 outer loop frontier_update span: No journal op exists to time frontier_update -> Timed from the end of the previous span to the journal 'iteration settled' entry
- 3.23 proposer killed session: Turn error given as {type: killed} but 2.13 errors need a message -> Error is {type: killed, message: 'session ended without a result event'} on every part's turn
- 3.23 refused span text: request_excerpt has sha256 and bytes, not chars, so the hash form does not fit -> Content is '[sha256 <hex> bytes <n>]'
- 3.23 proposer reasoning part: NormalizedSession keeps only thinking_chars, no thinking text -> Proposer llm spans have no reasoning part; thinking_chars is an attribute
- 3.23 outer loop strings, 'each string' vs 'text the proposer wrote': Hashing every string would also hide our own statuses and names -> hypothesis/axis/components (and non-identifier strings) are guarded and ruled; our enums and ids stay readable
- 2.13 system_instructions under hash rules: The example shows 'Reasoning: medium' unhashed while the rules say hash -> Kept verbatim only when equal to upstream.expect.system_prompt; any other system text gets span_prompt
- Rules for every file: 500-line target: plan_spans.py is 749 lines (spec estimate 560) -> Kept one file; the row allows no other module
- (review) L3-export: validate the effective manifest before planning; validate_plan now also refuses any plan when LawBench rules are not all hash
- (review) Proposer tool results take the rule of the one dataset named in the arguments (spec 3.23): 'echo Symptom2Disease; cat data/*/val.jsonl' gets Symptom2Disease's rule for all datasets' text. Run the exporter's verbatim scan over proposer tool results
- (review) Outer-loop numeric-looking strings that are not ours (e.g. propose result proposer_list_usd from util.usd) are hashed under the default rule. They are unreadable but nothing leaks
- (review) verify_spans does not check conversation membership or cache_creation_input_tokens; rows are matched on call_id, tool_call_id or turn_key only

### L2-relay

- PipeChannel sets proc.stdin non-blocking; start the process with stdin=PIPE, stdout=PIPE, bufsize=0
- attach() calls channel.set_write_timeout(pong_timeout_s) when the channel has that method (an explicit value wins)
- The session closes a bridge's channel itself when it marks it dead; Bridge.dead_reason is eof|write|frame_errors|silent|detached|closed
- Extras: session.bridges, session.current, session.log_lines (first 50 LOGs), relay.FRAME_ERROR_LIMIT, LOG_KEEP
- counters() also has pongs, gateway_errors, in_flight, live, done_cached
- wait_exit returns type-checked EXIT meta plus 'valid'; a bad field reads worst (returncode None, reason 'invalid', unanswered 1)
- A gateway exception answers gateway_internal; a BaseException (CampaignStop) answers halted; the pool thread survives
- Mode (cache or not) comes from the first valid HELLO; before any HELLO nothing is cached
- close(send_bye=True) waits at most min(5, pong_timeout_s) for BYE to be written, then detaches every bridge
- FaultChannel(proc, seed, min/max_chunk, drop_after_frames, cut_fraction, stall_after_frames, stall_writes, noise_rate, write_delay_s, write_timeout_s); stall(), drop(), block_writes
- tests/fake_channel.ScriptedChannel: a process-less FrameChannel (feed_frame, end, hold_writes, expect) for L3 fakes

Spec rulings:

- 1 Rules: 500 lines per file: relay.py holds the full 3.12 host contract and comes to 593 lines -> Kept one file as the row allows; reported
- 3.12 flood rule: 'more than 4*max_workers live requests' does not say whether 4N live is still allowed -> At most 4*max_workers live; the REQ that would make 4N+1 gets relay_overloaded
- 3.12 PipeChannel signature: write must time out at relay.pong_timeout_s but the constructor has no timeout parameter -> Added keyword write_timeout_s=None and a set_write_timeout(s) hook that attach() calls with pong_timeout_s
- 3.12 HELLO handling: A late HELLO from an older bridge would prune the done cache and cancel newer rids -> Only the current (newest) bridge's HELLO prunes the cache and cancels; RES is written even for cancelled rids
- 3.12 REQ handling: No rule for a REQ whose method/path make_envelope rejects -> Answered at once with the invalid_request refusal; the gateway is not called
- (review) attach() calls channel.set_write_timeout(pong_timeout_s) when the channel has that method. A sandbox FrameChannel without it must make write() time out at relay.pong_timeout_s itself.
- (review) When the current bridge dies, only RES frames stay queued (daemon mode only). A queued KILL, PING or BYE is dropped, so after a re-attach the executor must call send_kill again if the kill had not been confirmed.
- (review) Writes go only to the newest attached bridge. If a newly attached bridge dies while an older one is still alive, nothing is written until the next attach, so the executor must re-attach whenever session.current is not alive.
- (review) Flood rule as built: at most 4*max_workers live requests. Refusals (relay_overloaded, invalid_request) are not put in the done cache, so a REQ the stub re-sends is judged again.
- (review) When a bridge dies, its abandoned partial frame is counted in that bridge's decoder noise_chars and therefore in counters()['noise_chars'].
- (review) wait_exit meta has an extra 'valid' bool. Invalid fields are replaced by their worst reading: returncode None, reason 'invalid', unanswered_at_exit 1.

### L2-bundle

- build_job_file has an extra kwarg capture: bool|None (None = manifest capture.enabled); JobSpec.capture should be passed
- build_job_file/build_check_file raise ValueError on any credential-shaped env name, OPENROUTER_API_KEY included, or any scan finding
- LocalExecutor must add procenv.route_env at Popen time, not in the job file env
- Job file env is written with sorted keys; expect_outputs entries become <box>/out/<basename>
- build_check_file: route from routing.route, timeout_s from executor.import_check_timeout_s
- build_bundle reads pristine.json at ws.parent/pristine.json; it returns an extra 'skipped' list [{name, reason}]
- extract_bundle raises ValueError on any refusal and validates every member before writing anything
- PACK_SNIPPET argv: [python, -c, PACK_SNIPPET, box] plus 'cache' to include cache/; it prints a JSON {packed, skipped}
- WHEELHOUSE_SNIPPET also requires SHA256SUMS to list exactly the wheels in the tar; it prints only the hex on success
- collect_outputs writes attempt_dir/collect.json; tar mode skips duplicates, unsafe names, links and devices
- executors exports merge_refusal_codes(gateway_codes, exit_file, flood_refusals) and positive_codes; executors should use it
- check_selfcheck: vocab_file_present None or True is ok; a missing credential_names/egress is an isolation problem
- SandboxHandle.read_file raises FileNotFoundError for an absent file (reason CWSANDBOX_FILE_NOT_FOUND)
- SandboxHandle.exec returns ExecResult(timed_out=True) on an SDK timeout instead of raising
- CwSandboxClient raises ExecutorError('auth') when decisions.entity/project are null; missing SDK gives ExecutorError('create', retryable=False)
- map_sdk_error(exc, during=) is public; stop retries once after clock.sleep(20) on UNAVAILABLE
- kill_group treats EPERM as gone (macOS zombie group) and never reaps; callers still wait() their Popen

Spec rulings:

- 3.16 classify_attempt row 1: Unclear whether kill_reason=='halt' needs halted=True too -> Read it as (halted and taint has halted/budget_exhausted) or kill_reason=='halt'
- 2.6 exit file / 3.16 refusal_codes: local_refusals carries zero counts, so a naive merge makes set(codes) tainted -> classify_attempt and merge_refusal_codes count only positive integer counts
- 3.17 error mapping, PERMISSION_DENIED: SDK 41b7a8a maps gRPC PERMISSION_DENIED to CWSandboxAuthenticationError with no status attribute -> permission only when the error carries code/status PERMISSION_DENIED or a PermissionDenied class; else it surfaces as auth (also terminal)
- 3.17 fallback image: No documented reason or class for an image pull failure -> IMAGE_PULL_REASONS guessed [inf] plus an ImagePull class-name test; settle at PF6
- 3.16 collect_outputs: Oversize files must be 'reported' but the return type is only out-hashes -> Writes attempt_dir/collect.json {schema mhforge.collect/1, copied, skipped}
- 3.16 build_bundle limits: Not stated whether breaking the agent size/count limits is an error or a skip -> Oversize or non-module agent files skipped and reported; more than 400 agent files raises WorkspaceError; an oversize override raises
- 1 Rules, 500-line target: bundle.py with the three snippets is 599 lines -> Kept one file as the row lists; reported
- 3.16 result_is_usable: Upstream raises UnicodeDecodeError on undecodable bytes -> Returns False; every other case matches upstream's real function (compiled from EX in the test)
- (review) CwSandboxClient.create raises ValueError, before any SDK call, unless deny_egress is True, dns_egress is 'deny', and entity/project equal decisions.entity/project. SandboxExecutor and the fake must fill SandboxSpec from the manifest.
- (review) Run PACK_SNIPPET (after the worker) with python -I, or with a cwd outside <box>. 'python -c' puts the cwd on sys.path, so a worker-planted json.py or tarfile.py would load. The host's collect_outputs re-checks everything regardless.
- (review) build_bundle trusts pristine.json's own tree digest and does not compare it with upstream.tree_sha256, so run ensure_workspace first. Its return value has an extra 'skipped' list; reasons include pycache, symlink, not_regular, not_a_module_file, name_too_long, too_large.
- (review) collect.json skipped entries can name a whole subdirectory ('out', 'run' or 'cache') with reason link or not_regular.
- (review) positive_codes and merge_refusal_codes count a malformed (non-int) value as 1.
- (review) build_job_file has an extra optional kwarg capture: None means capture.enabled from the manifest. An executor should pass JobSpec.capture.

### L2-gateway

- Executors must call gateway.register_job before ingress.open; LoopbackIngress never registers a job. It also has close_all().
- register_job raises ValueError when a job_id is re-registered after close or with another identity; re-registering an open job does nothing.
- Gateway.poll_s (default 1.0) is the follower, queue and drain re-check interval; tests set it to 0.05.
- in_flight() and drain() count provider attempts between reserve and settle; close_job waits on handle calls in steps 8 to 11.
- close_job on an unknown job_id returns zeroed counters; after abort_queued, new requests also get halted at step 4.
- outcome 'refused' is used only when the request made no provider attempt; a halt, ceiling or close after one gives 'failed'.
- Only 'refused' records swap request for request_excerpt (sha256 of the raw body); past the cap the admission record is deleted.
- Record cost_micro is the spend of complete attempts; cost_basis is 'reservation' if any attempt is bounded_unknown, pending or unknown.
- Refusals made by the ingress itself (oversize body left unread, no Content-Length, non-POST) never reach the gateway and get no call record.
- The unknown_job refusals in calls/_unknown share one gateway-wide record cap.
- test_gateway and test_ingress_http set ledger.FULLFSYNC=False while they run (speed only); test_ledger checks the real pragma.

Spec rulings:

- tests/fixtures/call_records/failed.json vs 3.10 step 9.7: The fixture's refusal code is provider_unavailable with detail repeatable_provider_failure; the spec says a repeatable failure replies provider_rejected -> Followed the spec: code provider_rejected, detail repeatable_provider_failure, outcome failed. Left the fixture alone.
- Rules for every file: target of 500 lines: gateway.py comes to 801 lines and test_gateway.py to 1261, because the 3.10 contract and its 55 named cases are all in my row -> Kept one file each, since the row allows no extra file. Reporting it here.
- 2.6 gateway counters refused / refused_unrecorded: The spec does not say whether 'refused' counts refusals beyond the record cap -> refused counts recorded refusals only; refused_unrecorded counts those past the cap; refusal_codes counts every non-200 code
- 3.10 step 5 admission: The spec does not say what code a missing model, max_tokens or temperature gets -> Missing model gives model_not_allowed; missing max_tokens gives invalid_request; temperature is optional
- 3.10 close_job / drain timeouts: The waits are real-time, but the clock is injected; a FakeClock never advances -> The waits use time.monotonic and Condition.wait; record times and backoff use the injected clock
- (review) Fixture tests/fixtures/call_records/failed.json (L1-telemetry-pure) has code provider_unavailable with detail repeatable_provider_failure. The gateway follows 3.10 step 9.7 and emits provider_rejected with that detail. Readers must key on the detail, not the fixture's code.
- (review) Gateway.in_flight() and drain() count an attempt from just before reserve() until it settles. A leader still blocked in circuit.enter is not counted; after an abort it replies halted without reserving.
- (review) close_job waits only for handle calls between steps 8 and 11. Followers (step 7) that leave with unknown_job after the close are not in the returned counters.
- (review) Tests that open the circuit with a FakeClock must advance the clock themselves. Circuit.enter polls in real time and reads the fake monotonic clock, so a scenario where every thread waits in enter hangs.

### L2-proposer

- take_settings_inputs has an extra keyword claude_tmp_root (default /private/tmp/claude-<uid>); SettingsInputs gains report_files last
- check_model returns extra key resolved_model; check_sandboxed returns extra passed and bash_tool_uses
- copy_transcript returns extra key skipped; transcript copy lives at <session dir>/transcript/<sid>.jsonl
- command_fingerprint/command.json: {argv_sha256, argv_flags (flag names only), redacted: {-p|--append-system-prompt: {sha256, chars}}}
- session.json extra keys: reaped, evidence_exposed; integrity.reports_added; drift.reports_modified
- Journal proposer complete detail.candidates lists stored candidates (ok outcome only); exceptions journal 'interrupted' and close the meter at W
- A CampaignStop before 'started' uses no ordinal: the next session reuses s<k>
- frontier_before is {system, val_accuracy} from FRONTIER_VAL _pareto[0], or None when absent
- Shim deadline uses the injected clock's monotonic; grace and reap waits are real seconds; stream.jsonl and stderr.txt always created
- parse_upstream_argv raises UpstreamContractError on --disallowedTools or any unknown token
- ProposerRunner needs meta_harness.LOGS_DIR, PENDING_EVAL, FRONTIER_VAL, EVOLVE_DIR, PROPOSER_ALLOWED_TOOLS; EVOLUTION_SUMMARY optional
- In restricted/contained the runner creates LOGS/reports before taking settings inputs (LD1)
- fake_claude_cli: write_fake_cli(dir, *sessions, **opts); one plan per call, last reused; session id 00000000-0000-4000-8000-<call no.>
- fake_claude_wrapper.new_module(plugin_dir) returns a fresh claude_wrapper-like module per test

Spec rulings:

- 3.19 session_argv: Model/effort said to come from upstream's command, yet --model must be proposer.model -> Used ArgvSpec.model and ArgvSpec.effort (manifest values); runner passes the same to wrapper.run
- 3.19 settings path syntax: '//<absolute path>' is ambiguous (would give ///Users/...) -> Rules are '/' + abspath, i.e. Edit(//Users/...), the CLI's documented form
- 3.19 check_model vs limit fixture: Limit stream's main-thread message has model '<synthetic>'; literal rules give model_mismatch, not limit_window -> check_model skips '<synthetic>' events; with no real reply init.model passes the alias/pinned rule
- 3.19 sandbox_check: Transcript present but fewer Bash results than stream Bash calls is not covered -> Missing results count as unknown, so the proof fails (fail closed)
- 5.2 test_state_access_signals_set_evidence_exposed: evidence_exposed lives in the iteration record, owned by L4-driver -> Added evidence_exposed (bool) to session.json and tested that; driver can copy it
- 3.19 cli version mismatch: Both TranscriptError(version)->proposer_cli and an integrity violation apply -> Stop order: environment, then proposer_cli (transcript), then proposer_integrity; init-only mismatch gives proposer_integrity
- Rules: 500 lines per file: proposer.py holds the full 3.19 contract and is 1241 lines -> Kept one file: my row lists no extra file to split into
- 3.19 step 13 / test_exit_zero_without_result_event: A cut-off session with transcript usage prices at usage_sum, never estimate -> Test asserts usage_sum there and estimate only when no transcript was written
- LD1 vs 3.19 table: Reports rules changed after the table -> Applied LD1: 4 allow rules, per-report Edit deny and denyWrite, reports kept out of LOGS denyWrite, new addendum bullet
- (review) check_sandboxed counts stream Bash calls that have no transcript result as unknown. In restricted or contained mode, a session cut off by the timeout during a Bash call becomes settings_ignored and raises CampaignStop(proposer_settings), not a settled timeout.
- (review) session.json carries evidence_exposed (bool). L4-driver copies it into the iteration record.
- (review) Session stops are raised in this order: proposer_environment (venv fingerprint), then proposer_cli (transcript version), then proposer_integrity. A stop raised before journal 'started' reuses the session ordinal and its directory.
- (review) take_settings_inputs has an extra optional claude_tmp_root kwarg (tests only). The generated denyRead paths always use /private/tmp/claude-<uid>.

### Cross-owner fixes (Six cross-owner repairs (journal reader, stale halt, key names, scan gaps, two rule pins))

- item 1 [done] mhforge/util.py, mhforge/journal.py, mhforge/manifest.py, mhforge/state.py, tests/test_util.py, tests/test_journal.py, tests/test_manifest.py, tests/test_state.py: Added util.parse_marked_jsonl(data, check=None, *, name), util.read_marked_jsonl(path) and util.JOURNAL_TORN_MARKER. journal._parse now delegates to it and keeps its own entry checks through the check callback. manifest._journal and state.status read the journal through read_marked_jsonl. An unmarked bad middle line is still a StateError.
- item 2 [done] mhforge/manifest.py, tests/test_manifest.py: When possibly_stale is true, _halt_row re-reads the halt row through sqlite3.connect(ledger, timeout=30). If that fails it returns a 'ledger could not be read' blocker. state.py is unchanged and still creates no file.
- item 3 [done] mhforge/manifest.py, tests/test_manifest.py: Rule 8 now requires each of the three key variable names to match procenv.is_credential_name (re.search of wire.CREDENTIAL_NAME_PATTERN on the upper-cased name) and not be in wire.CREDENTIAL_NAME_EXEMPT. The fixture and manifest.draft.json still validate.
- item 4 [done] mhforge/scan.py, tests/test_scan.py: keyed_hex40 now accepts escaped quotes and a comma separator, and scan_obj checks adjacent list items as argv pairs. authorization_header gained a JSON-form alternative, also checked across dict key and value. known_digest also scans wider runs that include . + / =, with duplicates removed. Still 11 patterns and 16 rules, and all existing negative cases still pass.
- item 5 [done] tests/test_provider.py: Added OpenAIProjectRule, which pins ForgeProviderClient._project to manifest.openai_project over the cases both accept. They agreed already, so provider.py is unchanged and this test does not fail on the old code.
- item 6 [done] tests/test_workspace.py: Added test_venv_root_is_the_procenv_rule, which pins workspace._venv_root to procenv.venv_root, including the case where both raise WorkspaceError. They agreed already, so workspace.py is unchanged and this test does not fail on the old code.
- Read the journal with util.read_marked_jsonl(path), or journal.read_journal if you may import journal.py. Do not use util.read_jsonl, which refuses a journal after a repaired torn tail. Marker rows (op 'journal', key 'torn_tail', status 'terminated') are included in the rows returned.
- util.parse_marked_jsonl(data, check=None, *, name='journal') returns (rows, torn_bytes_or_None). check(row, line_number) can validate each good row and returns the row to keep. util.JOURNAL_TORN_MARKER is the (op, key, status) triple, and journal._MARKER now aliases it.
- state._rows gained an optional reader parameter (default util.read_jsonl). It still falls back to reading every parseable line on a bad middle line.
- manifest._halt_row now imports sqlite3. On a possibly-stale view it opens a normal connection with timeout=30, which can create -shm or checkpoint the -wal. Only the live gate does this; state.status() does not.
- Rule 8 now refuses non-credential key variable names (PATH, HOME_DIR and similar) and the exempt names OPENROUTER_API_KEY and TIKTOKEN_RS_CACHE_DIR. A manifest that names its key variables differently must use a name that matches wire.CREDENTIAL_NAME_PATTERN.
- scan: a JSON list such as ["token", "<40 hex>"] and adjacent string items in scan_obj lists are now keyed_hex40 findings. {"Authorization": "<value of 8 or more characters>"} is an authorization_header finding, with offset 0 and the value's length when found by scan_obj.
- Items 5 and 6 found no difference between the two copies, so their tests are pins only and pass on the old code as well.

## Layer 2, contract repairs LD4 to LD6

### Cross-owner fixes (LD4-LD6 contract repairs (layer 2))

- item LD4 [done] showcase/mhforge/telemetry/plan_spans.py, showcase/tests/test_plan_spans.py: plan_proposer gains keyword verbatim=None. New public constant VERBATIM_INDEX_MISSING. With an index, any reply or tool text under a non-hash rule goes through policy.guard_text when some dataset has a stricter rule for that field. The check runs on tool text after portable and hex masking, before truncation, and a finding gives the hash form. With no index and any proposer rule open (strictest span_reply or tool_result, or any dataset's tool_result), every text is hashed and the plan records the problem verbatim_index_missing. 4 tests added, including the reviewer's grep of a Symptom2Disease path that holds USPTO text. The existing ProposerTest and SharedRulesTest helpers now pass an empty index.
- item LD5 [done] showcase/mhforge/telemetry/plan_tables.py, showcase/tests/test_plan_tables.py: For a dataset shown in clear, a prediction appears in clear only when it exactly equals a target label shown in the same table. Any other prediction becomes the gated cell form: the row's 64-hex prediction_sha256, or the sha256 of the text held when the row has no valid hash. Every table plan now has an int predictions_withheld, which is 0 for gated tables. 3 tests added, including a 200-char LawBench case-text echo. test_long_label_is_hashed now expects a bare hex prediction.
- item LD6 [done] showcase/mhforge/proposer.py, showcase/tests/test_proposer.py: check_settings(settings, mode, *, base=None). With base it also reports: (a) any generated list or object whose merged value has another type, checked recursively; (b) generated entries missing from the KEPT_LISTS paths (new public constant), which are permissions.deny, sandbox.filesystem.denyRead, sandbox.filesystem.denyWrite and sandbox.credentials.files; (c) a non-empty sandbox.excludedCommands; (d) a changed permissions.additionalDirectories. The runner passes base=generate_settings(inputs) whenever an override is truthy and the mode is not upstream, and any problem raises CampaignStop("proposer_environment") before the CLI runs. 3 tests added, including the reviewer's null override.
- Owner of tests/test_readback.py: pass verbatim=scan.build_verbatim_index([]) to the three plan_proposer calls (lines 105, 220, 225). Without it, LD4 fails closed and test_verify_tolerates_known_normalisations fails. With it, the whole module passes; checked by wrapping the call.
- Exporter (L3): build Exporter.verbatim_index() once per export and pass it as verbatim= to plan_proposer, plan_outer_iteration, plan_propose_run and plan_search_step. If plan_proposer gets verbatim=None while any proposer rule is open, it hashes every text and adds 'verbatim_index_missing' to plan['problems']. That plan is safe but has lost its text, so a live export should treat the problem as a planning error (index not built), not emit it silently.
- Exporter (L3): the index does not say which dataset a finding came from. A proposer tool text under a loosened rule (for example Symptom2Disease full) is therefore withheld if it holds any indexed text, including that dataset's own question text, unless that dataset is fully open and so left out of the index. Text under the strictest rule is never guarded, because no dataset has a stricter rule.
- Exporter/publisher (L3/L4): in a non-gated table, the 'prediction' column (type str) can now hold a bare 64-hex sha256. Handle it as you already handle a gated prediction_sha256 cell. Each table plan has an int 'predictions_withheld' (0 for gated tables); put it in receipts or summaries if wanted. check_table is unchanged.
- Preflight (L4): to check settings outside the runner, call check_settings(merged, mode, base=generate_settings(inputs)) whenever an override was merged in mode restricted or contained. Problem strings look like 'permissions.deny must stay a list' and 'sandbox.filesystem.denyRead must keep every generated entry (N missing)'.

## Forge SDKs: installed, and what they really do (read before writing or reviewing any Forge-facing code)

The pinned SDKs are now installed in `.venv` (wandb 0.31.0, coreweave 0.1.0b0,
cwevals 0.0.1b1, cwsandbox 1.27.0, wandb-workspaces 0.4.13, opentelemetry 1.45.2). The exact reference, with
file and line for every statement, is `<local-workspace>`; the
Sandboxes client was checked call by call in `<local-workspace>`.
Where the spec and the reference disagree about an SDK call, the reference wins. To introspect, run the
environment's interpreter isolated, from an empty directory, with no credentials reachable:
`env -i HOME=<an empty dir> PATH=/usr/bin:/bin WANDB_MODE=disabled .venv/bin/python -I <script>`.
Import and inspect only; never log in, start a run, open a session or send a request.

What a careful reader of the public documentation would get wrong:

- wandb 0.31.0 defaults `base_url` to `https://api.forge.coreweave.com`, not `api.wandb.ai`; the default app URL is `https://forge.coreweave.com/wandb`.
- Agent Lens picks its trace host differently: `https://trace.wandb.ai` when `WANDB_BASE_URL` is unset, otherwise `WANDB_BASE_URL + "/traces"`. The `init` argument `base_url` is the trace server, and `/agents/otel/v1/traces` is appended to it.
- Agent Lens ignores `NETRC` and reads `$HOME/.netrc`, looked up by the `WANDB_BASE_URL` host. A malformed `~/.netrc` makes `init` raise.
- `tracing.force_flush()` returns True even when every export failed. Failed exports (401, 403, 413, 500) are logged and the spans are dropped; only 429, 502, 503, 504 and connection errors are retried, within 10 s.
- A serialized batch over 64 MiB is dropped whole. More than 128 attributes per span are dropped silently.
- `OTEL_EXPORTER_OTLP_HEADERS` is merged into the trace request. `OTEL_SDK_DISABLED=true` or `OTEL_TRACES_SAMPLER=always_off` turns tracing off silently although `init` succeeds.
- There is no `end_tool()` or `end_subagent()`: a Tool or SubAgent ends only through `.end()` or a `with` block.
- Trace and span ids cannot be set; only `conversation_id` can. A Conversation emits no span.
- Batch `log_turn` and `log_conversation` make every child a direct child of the turn; a nested sub-agent tree needs the streaming path with `started_at` and `ended_at`.
- `LLM` and `Turn` set `started_at` to now when constructed, and the `start_*` factories take no timestamps. Naive datetimes are read as local time.
- `autopatch_integrations` defaults to True and installs a global import hook that traces anthropic, openai and google.genai calls.
- `wandb.EvalTable` defaults to the `weave` backend, which imports `weave` in the constructor; `weave` is not installed, so only `backend="ces"` works.
- CES EvalTable writes happen synchronously inside `run.log`. Limits in the code: at most 10,000 rows, primitive score columns, no all-null column, no offline runs, `allow_mixed_types=False`, `log_mode` IMMUTABLE only.
- The `cwevals` distribution installs as the module `coreweave_evaluations`. `CES_CUSTOM_HEADERS` overrides the Authorization header on CES requests.
- Reports (`wandb_workspaces`) take no `api_key` or `base_url`: every call makes a fresh `wandb.Api()` with credentials from the session, the environment, netrc or settings files. `Report.save()` creates the project if it is missing. `Report.url` drops the `/wandb` path of the Forge app URL.
- `wandb.Api(api_key=...)` makes a verification request in its constructor.
- `run.use_artifact(<draft Artifact>)` logs it as an output of the same run and blocks.
- For Forge, wandb's netrc lookup falls back to the `api.wandb.ai` entry, so an old W&B key in `~/.netrc` would be used for Forge.
- `wandb.login(key=...)` writes the key to netrc. `wandb.init(settings=Settings(api_key=...))` writes nothing but makes the key the process-wide session credential.
- Any Settings field can be set from a `WANDB_<FIELD>` or `WANDB__<X>` environment variable, including `x_extra_http_headers` and `insecure_disable_ssl`.
- `<cwd>/wandb/settings` is read as a settings file before environment variables, whatever `WANDB_DIR` says.
- `wandb-core` is started with `env=os.environ`.
- cwsandbox reads `CWSANDBOX_BASE_URL` on every entry point and would send the key headers to that host; the client now refuses when it is set to another host. `Sandbox.run` returns on acceptance: an image pull failure surfaces later, from `wait()`, as `SandboxFailedError.status_reason` starting `ErrImagePull`. In `wandb_auth` mode the SDK uses wandb's session credential and host, not the key we pass.

## Layer 3

### L3-qualification

- Pass offline_gate results to live_blockers through qualification.gate_problems(gates) (strings '<name>: <detail>')
- Gate names: manifest_frozen, upstream_source, workspace_pinned, worker_lock, smoke_receipt, proposer_cli, ledger_unknown, protocol, wheelhouse_tar, requirements_lock
- A check that does not apply to the command or executor is listed ok, with a 'not applicable' detail
- offline_gate raises ValueError for a command that is not in manifest.COMMANDS
- smoke receipt engine_sha256 must equal engine_snapshot(Path(mhforge.__file__).resolve().parent)['engine_sha256']
- engine_snapshot file keys are 'mhforge/<rel>'; engine.json from freeze should use the same snapshot
- observations(records, job_record=job.json) gives job_seconds from attempts[-1].executor.seconds
- Extras: require_fits(fc) raises CampaignStop(forecast_exceeds_budget); quantile(); calibration_verdict(manifest, paths)
- record() takes clock=; it writes attempt-<n>/qualification.json and forecast.json; passed.json needs all checks ok and fc.fits
- An imported record() with fc={} counts as fitting; pass imported_from={campaign, manifest_sha256, diff_paths}
- import_problems reads reconciliation.json in the source's highest qualification/attempt-<n> dir; the verdict must be pass or waived
- noise_stats results arg: system -> dataset -> repeat -> result dict; a result total is the denominator when present
- restart_allowed reads Ledger.snapshot() keys in_flight_micro, unknown_requests, committed_by_phase, caps_micro
- audit returns an extra 'listed' dict: reconciled, limit_exceeded, repeatable_provider_failure, admission_only, open_meters
- audit 'calls' is the number of call records read (calls/*/*.json, _unknown included)

Spec rulings:

- 3.22 observations signature: job_seconds needs the job record but the signature takes only call records -> Added keyword job_record=None; without it job_seconds is the span of the call records
- 3.22 observations limit_exceeded: Asks for limit_exceeded ledger rows, but the function gets only call records -> Counted calls whose usage is over input_limit/output_limit, the same rule as ledger.judge_usage
- 3.22 forecast quantile: No rule for the quantile when no completion tokens were observed -> Nearest-rank quantile; with nothing observed q is gateway.max_output_tokens (worst case)
- 3.22 offline_gate vs 3.3 gate_problems: offline_gate returns dicts, but live_blockers takes gate_problems as strings -> Added qualification.gate_problems(gates) -> ['<name>: <detail>'] for the failed checks
- 2.4 noise.json / 3.22 noise_stats: noise_stats is pure but the body of noise.json has a utc -> Returns the body without utc plus extra keys problems and totals; the caller adds utc
- 3.22 audit scan scope: 'records under calls/ or jobs/' would also scan the extracted work tree and the worker cache -> The scan skips the box/ and cache/ directories (audit.SCAN_SKIP_DIRS); run/ and out/ are scanned
- Rules: 500-line target: qualification.py is 661 lines, and my row allows no other file -> Kept it as one file and am reporting the size here
- (review) L4-preflight: record() writes passed.json with environment=None if you pass none on a self-qualification. Pass the qualification job's attempts[-1].executor.environment, or the freeze-hash proof is skipped for later jobs.
- (review) Call qualification.calibration_verdict for LD3; it mirrors manifest._calibration_verdict.
- (review) offline_gate fails proposer_cli, wheelhouse_tar and the lock checks when the pinned path is a symlink. Pin the real file, not a symlink.
- (review) import_problems reads reconciliation.json from the source's highest attempt-<n>. A later preflight attempt without PF-R blocks the import.
- (review) The audit's secret scan skips jobs/*/a<N>/box/ and cache/ (audit.SCAN_SKIP_DIRS).
- (review) test_observations now expects limit_exceeded == 2: its rec 20 has reasoning_tokens above completion_tokens, which the ledger settles as limit_exceeded.

### L3-exec-local

- on_handle dicts: pgid, pid, t_start, proc_start (ps lstart epoch), executor, purpose (worker|stub|import_check)
- daemon stub handle also has sock_dir; cleanup_orphans removes /tmp/mhf-* sock_dir and needs proc_start or t_start
- JobResult.facts: executor, isolated False, pgid, seconds, setup_seconds 0.0, cost_micro 0, environment, selfcheck
- LocalRelay facts.relay: mode plus attaches, rotations, frames_in/out, chars_in, noise_chars, flood_refusals and the rest
- environment.env_mode is 'local'; image_digest, requirements_sha256, sha256sums_sha256 are null
- ExecutorError kinds raised: image_mismatch (no tokenizer dir or env proof), selfcheck, upload, exec
- Tunable attributes: poll_s (1.0), term_wait_s (10.0); LocalRelayExecutor.channel_factory and .rng
- import_check builds work_root/box and writes work_root/checkfile.json (isolated false)
- Daemon stub pgid is refused if it shares the runner's session or group (ExecutorError exec)

Spec rulings:

- 3.17 LocalExecutor/LocalRelayExecutor job file expect: sandbox.expect.machine x86_64 and python 3.13 would make the host selfcheck refuse every arm64 Mac -> Local job files set expect.machine and expect.python to null; the freeze hash expectation is kept
- 3.17 worker PATH '<venv root>/bin': procenv.venv_root raises for a non-venv interpreter such as sys.executable, which the tests use -> PATH uses dirname(abspath(worker_python)), the same as venv_root/bin inside a venv
- 3.17 LocalExecutor.run order: The spec registers the job and opens the ingress before the selfcheck, so an image problem would leave a registered job behind -> The selfcheck runs first, then register_job, then ingress.open inside the try/finally
- 3.17 daemon reattach 'in a row': The spec does not say what resets the count of failed reattaches -> Any bridge that delivered a HELLO resets the count to zero
- 3.16 wait loop, KILL under daemon relay: relay.py drops a queued KILL when its bridge dies; the spec does not say the executor must resend it -> While waiting on a KILL in daemon mode the loop keeps reattaching and resends the KILL on each new bridge; tested
- (review) The local job file sets expect.machine and expect.python to null and keeps the freeze hash. Only the sandbox enforces machine and Python version.
- (review) LocalExecutor runs the selfcheck before register_job and ingress.open (a reorder of 3.17). The selfcheck process gets no on_handle.
- (review) Daemon mode blocks in clock.sleep for each reattach backoff (up to 30 s with the draft values), and sandbox.py does the same. A kill request or halt seen during that wait is acted on only after the next bridge attaches. While no bridge is up nothing is spent.
- (review) If a kill is due while no bridge is alive, the stub group is killed and relay_end becomes 'error'. classify_attempt then gives relay_lost (infra), even when kill_reason is refusal_flood.

### L3-jobs

- jobs.make_passthrough(python, ws, logs_dir, *, home, timeout_s=600, run=subprocess.run) builds the 3.18 passthrough; driver passes abspath worker python
- jobs.import_rejection(ImportResult) -> None|'import'|'loaded_class' for the iteration record's rejected field
- import_check returns {candidate: ImportResult} without the probe; CampaignStop(infrastructure) when probe/executor fails max_infra times
- run_job(publish=False) never publishes; repeat/qualification keys get .r<k>/qual. via JobIdentity
- Test calls (--test forms) always use phase test; validation calls use tracker.phase, iteration only in phase search
- Executor name/isolated read with getattr; job.json executor field may be None for executors without them
- Job record published has extra key attempts {rel: n}; launcher_log set only when something was published
- Quarantine stamp uses journal.clock (inject FakeClock into Journal for deterministic paths)
- Names failing CANDIDATE_RE return upstream's not-found message without any journal entry
- _clean drops dict keys matching token|bearer|api_key|secret|password|address|url|port|listener from gateway counters, facts and handles

Spec rulings:

- 1 Rules, 500-line target: jobs.py holds the full 3.18 contract and comes to 1042 lines (estimate 710) -> Kept one file; the row lists no other source file
- 3.18 verify_published_results vs Test inputs: Verify lists only val.json/test.json, yet Test inputs says a changed LOGS memory.json was already quarantined and republished by it -> Also quarantine+republish an owned memory.json that changed or went missing; unowned memory.json left alone
- 2.6 published: No field says which attempt a published file came from, which resolve_test_input and republish need -> Added published.attempts {rel path: attempt number}; readers ignore unknown keys
- 3.18 step 3 skipped_usable: Status for a skipped job with no record of ours is not given -> Existing records keep their status (only name_collision updated); a missing record is written with status skipped_usable
- 2.4 attempt directory: Bundle tar location not listed; keeping it costs disk per attempt -> Tar built at a<N>/bundle.tar and deleted after the run; bundle.json keeps member hashes
- 3.18 import_check: Silent on non-retryable ExecutorError (billing, auth...) and on check.json shape -> Stops at once with CampaignStop(infrastructure, kind); check.json schema mhforge.importcheck/1 with probe, results (+rejected), infrastructure_failure
- 3.18 run_job candidate bytes: No rule if the workspace file changes between the hash check and the bundle -> Rebuild once from the stored copy with the expected hash; test job without one raises WorkspaceError; val job records bundled hash
- (review) L4-driver must journal ('run', <cmd>, 'started') at each runner invocation. Without it, Journal.session_start_seq() is 0, so infrastructure attempts are counted for the whole campaign and a job that used them up never runs again.
- (review) Every --memory job bundle now calls candidate_reference(name, None) for every non-pinned agents/*.py in WS, not only for the job's own candidate. The driver's bound proposer.candidate_reference must be cheap and must return None for names it never stored.
- (review) A run directory under LOGS that the proposer replaced with a symlink is now skipped by verify_published. A later publish to it still raises WorkspaceError (pre-existing refusal), so the driver should quarantine such entries or turn that error into a stop.
- (review) job.json published.attempts {rel: attempt} is an addition (implementer's choice). resolve_test_input and republish depend on it, so L3-export and others must keep it when they rewrite records.

### L3-exec-sandbox

- Sandbox tags (5, each <=63 chars): mhf; mhf-c-<campaign>; mhf-j-<first 16 hex of sha256(job_key)>; mhf-a-<attempt>; mhf-p-<phase>
- Import-check sandbox tags: mhf; mhf-c-<campaign>; mhf-j-<16 hex of sha256('import_check')>; mhf-a-<n-th check of this executor>; mhf-p-import-check
- cleanup_orphans lists by ['mhf','mhf-c-<campaign>'], so it finds both job and import-check sandboxes
- Metered: meter sandbox_seconds, key = sandbox id, unit ms, opened at create with worst L*1000 ms and its price
- Metered: meter_open detail {purpose: job, job_id} or {purpose: import_check}
- Metered: meter_close uses billable ms from wait_running to stop on the runner clock, priced with sandbox_cost_micro
- Metered: a re-created sandbox gets its own row; a failed stop leaves the row open at worst case for cleanup_orphans
- Metered: cleanup_orphans closes open rows (except this process's live ones) with min(worst, now - opened)
- Budget: check_meter_cap(sandbox_seconds, extra_micro=price(L)) before every create; refusal sets halt budget:sandbox and raises CampaignStop
- L = timeout_s + lifetime_margin_s (+ setup_allowance_s in wheelhouse); import check uses 300 s as timeout_s
- JobResult.facts keys: executor, isolated, sandbox_id, sandbox_ids, lifetime_s, tags, seconds, cost_micro, setup_seconds
- JobResult.facts keys (cont.): environment, selfcheck, probe_egress, relay (counters+mode+bridge_ends+attach_errors), exit_file, unary_kill, stop_error
- facts.environment keys: env_mode, image_digest, requirements_sha256, sha256sums_sha256 (None in image mode), freeze_sha256, machine, python
- jobfile.json and checkfile.json carry executor='sandbox' and isolated=true, like the local executors' copies
- Errors raised: ExecutorError env_setup (wheelhouse or proof in wheelhouse mode), image_mismatch (proof in image mode), quota, create, billing/permission/auth as given
- CampaignStop('isolation') after ledger.set_halt('isolation') on open egress or a visible credential; no worker exec happens
- fake_sandbox: exec labels are extract, wheelhouse_size, wheelhouse_check, pip_install, selfcheck, import_check:<name>, serve, bridge, pack, kill
- fake_sandbox: client._record(op, sid, label=...) lets tests put ledger/gateway events into the same call log

Spec rulings:

- 3.17 step 7 / insandbox/mhf_relay_stub.py daemonize: The daemonized stub's pid in stub.ready does not lead its own process group (the double fork leaves it in the intermediate child's group), so killpg(pid) finds no group -> KILL_SNIPPET looks up the group with os.getpgid(pid) and falls back to kill(pid); test_reattach_exhausted checks the stub is dead before exit.json is read
- Rules for every file (500-line target): sandbox.py is 759 lines because the whole 3.17 sequence, the wait loop, import check and cleanup live in it; the test file is 1067 lines -> Kept each in one file, since my row lists no other file
- 3.17 step 3 'On failure or a stuck create': It is not clear whether a create() call that raises a retryable error should also be retried once -> Only a failed or stuck wait_running is stopped and re-created once; a create() error other than quota is raised as it is (an infrastructure attempt)
- 2.10 meter detail for import_check: The detail is meant to carry iteration, but import_check gets no iteration -> detail is {purpose: import_check} as 3.17 says
- (review) The SandboxExecutor is shared by parallel dataset jobs. The cap check now also counts sandboxes still being created (ex._pending_micro), so any code that calls check_meter_cap('sandbox_seconds') outside the executor does not see those reservations.
- (review) The selfcheck (L1-stub) proves the vocab file only in the tokenizer dir it finds itself. With sandbox.tiktoken_cache_dir set, the override dir that goes into the job file is never proven.
- (review) import_check takes no freeze hash, so the import-check sandbox proves machine and Python but not the package set (freeze_sha256) before it runs candidate imports.
- (review) With the draft defaults, a wheelhouse tar on the host that does not match sandbox.wheelhouse.* is found only after a sandbox was created and set up. Each env_setup attempt then pays for a short sandbox.
- (review) tests/test_sandbox_executor.py holds os.environ inside the WORKER child-script string. A later env-access source check over tests/ must allow it, as for test_provider.

### L3-export

- SDK coreweave.forge.agentlens.tracing.init(project, api_key=, base_url='https://trace.wandb.ai', service_name=, autopatch_integrations=False)
- SDK client.force_flush() (result ignored) and client.shutdown() in finally
- SDK tracing.Conversation(conversation_id=, conversation_name=, attributes=) used as a context manager
- SDK tracing.Turn(agent_name=, agent_version=, messages=[], started_at=datetime, ended_at=datetime) used as a context manager
- SDK Turn.set_attributes(dict) and Turn.record_error(exc) are not used in H2 (UNVERIFIED); the read-back matches turns by turn_key
- SDK tracing.LLM(model=, provider_name=, response_model=, response_id=, input_messages=, system_instructions=, output_messages=, request_max_tokens=, finish_reasons=, started_at=, ended_at=)
- SDK llm.usage = tracing.Usage(input_tokens=, output_tokens=, total_tokens=, cache_read_input_tokens=, reasoning_tokens=), None values left out
- SDK tracing.Tool(name=, tool_call_id=, arguments=, result=, started_at=, ended_at=)
- SDK tracing.SubAgent(name=, model=, started_at=, ended_at=) is not used in H2 (UNVERIFIED)
- SDK tracing.Message(role=, parts=[...]), tracing.TextPart(content=), tracing.ReasoningPart(content=)
- SDK span.set_attributes(dict) and span.record_error(exc), where the exception class name is the plan's error type
- SDK opentelemetry.trace.get_current_span().get_span_context().trace_id, formatted as 032x inside each Turn
- HTTP read-back: urllib POST readback.READBACK_URL, Basic api:<key>, explicit User-Agent, no proxy handler, paged by offset
- SDK wandb.Settings(api_key=, disable_git=True, save_code=False, console='off', x_disable_meta=True, x_disable_stats=True, x_disable_machine_info=True, x_save_requirements=False)
- SDK wandb.init(entity=, project=, id=, group=, job_type=, name=, tags=, config=, dir=STATE/export/wandb-local, resume='allow'|None, settings=)
- SDK run.define_metric(name) and run.define_metric(name, step_metric=)
- SDK run.log(dict) with no step; also used as run.log({table_key: table}) once per table
- SDK wandb.Artifact(name, type=, metadata=); artifact.add_file(local_path, name=); run.log_artifact(artifact, aliases=[...]).wait()
- SDK run.use_artifact('<name>:<alias>'); a failure is a receipt problem only when best_effort
- SDK wandb.EvalTable(columns=, data=, input_columns=, output_columns=, score_columns=, backend='ces', unsupported_media_mode='raise')
- SDK wandb.Table(columns=, data=) for backend wandb_table
- SDK run.summary.update(dict); run.url; run.finish() in finally
- SDK wandb.Api(api_key=); api.client.execute(wandb_gql.gql(query), variable_values=) for the project and server-features queries
- SDK api.run(run_path).summary and api.artifact('<entity>/<project>/<name>:<campaign>') inside confirm
- wandb.login is never called. Both adapters read environ[telemetry.api_key_env] once and expose key_facts {name, len, sha256}
- Adapters change os.environ: OTEL vars (Agent Lens) and NETRC, WANDB_DIR, WANDB_CACHE_DIR, WANDB_DATA_DIR, WANDB_CONSOLE, WANDB_DISABLE_GIT, WANDB_DISABLE_WEAVE
- WandbSdkSink refuses (ExportError) when WANDB_BASE_URL, CES_BASE_URL, WANDB_APP_URL or WANDB_MODE is present in environ or os.environ
- With weave importable, every WandbSdkSink method raises AdapterUnavailable; from _check_project this propagates out of export_unit
- Exporter._check_project runs once before any meter, on every live surface, and needs a WandbSink; ExportError propagates
- export_all returns {receipts, statuses, deferred, not_ready}; receipts carry extra keys traces, skipped_tables and problems
- The CLI must pass example_dir; without it every candidate file is withheld and open proposer rules fail closed
- tests/test_exporter.Campaign is a reusable scaffold of finished campaign records; test_adapters imports it

Spec rulings:

- 1 Rules, 500-line target: exporter.py is 840 lines (the previous implementer's file); the row allows no extra module -> Kept one file and am reporting it; the two adapters are under 250 lines as 3.25 requires
- 3.25 feature_ces: The spec gives no SDK call for the CES probe -> GraphQL serverInfo.features query, feature name EVAL_TABLES_CES [inf], tried 3 times; must be settled at PF3
- 3.25 WandbSink.project_access: No SDK call is named; only the PF4 fields access, readOnly, weavePermissions.canWrite -> Read-only GraphQL project(name, entityName) query through wandb.Api(api_key).client.execute
- 3.25 tracing.LLM / tracing.Usage kwargs: The plan has request_temperature and cache_creation_input_tokens, but H2 never passed them to the SDK -> Kept exactly H2's kwargs; temperature and cache_creation are not sent (the read-back does not check them)
- 3.25 confirm marker: Only the eval-table-ces marker is named; after the wandb_table fallback the summary marker differs -> confirm accepts table-file for wandb_table tables, and either marker after a restart
- 3.25 step 2 candidate files: Nothing says what happens without a verbatim index (no example_dir) -> Kept the earlier choice: while any dataset is protected, every candidate file is withheld (fail closed)
- LD4 exporter: The interface notes ask that verbatim_index_missing count as a planning error live -> A live proposer plan with that problem raises PlanError (receipt failed); a dry run keeps the hashed plan
- 5.2 test_spawn: python -m mhforge export (cli.py, __main__.py) is built by a later layer -> Spawn tests run a stand-in mhforge package in a temp package root; it checks argv, cwd, env, key pipe and receipts
- (review) L5-integration (cli): always pass example_dir to the live Exporter. Without it, live proposer and propose units now fail with a PlanError, and baseline candidate files are withheld.
- (review) L5-integration (cli): wandb 0.31.0 reads ~/.config/wandb/settings (it can set base_url) under the real HOME. Manual export runs with the real HOME, and the spawner passes base_env HOME to the child. Consider a HOME or WANDB_CONFIG_DIR under STATE; the spec names neither.
- (review) L5-integration (cli): with --key-source env the export key stays in os.environ, and wandb-core is started with env=os.environ. Scrub it after the adapters read it.
- (review) L4/L5: plan_runs gives loop:iter000 resume=False, so a retried iteration 0 calls wandb.init on the existing <campaign>-search id with resume=None. Check this at PF8.

## SDK conformance of the adapters and sandboxes (after layer 3)

Reports: `<local-workspace>` and `sdk-conformance-cwsandbox.md`. Lead decisions LD7 to LD9 are applied.

- sandbox.py imports the private sandbox_client._is_pull_failure (as _client_pull_failure) and reads ExecutorError.__context__. It relies on _CwHandle.wait_running raising map_sdk_error(...) from None inside its except block. If that raise moves outside the handler, the fallback silently stops working; test_pull_failure_classified_through_the_real_adapter_raise_path guards this.
- The client's create-time fallback in CwSandboxClient.create cannot be reached under 1.27.0. If it ever ran, facts.image would name the primary image even though the fallback ran.
- The LD7 blocker applies to all live commands (including export, publish and account-check), so the WANDB_AUTH_MUST_BE_UNSET rows can now only appear alongside it. L2-bundle and L4-preflight should drop any wandb_auth live path.
- fake_sandbox.py: pull_fail_first now fails the next sandbox's wait_running (once). New knobs: pull_fail_images, failing_stops, and a SandboxFailedError with the real keyword-only signature.
- Before any SDK bump, rerun test_sdk_signatures_sandbox.py under the SDK venv. It pins __version__ == 1.27.0 and asserts that the class names the client matches but 1.27.0 lacks are still absent.
- applied: LD7: every live command gets the blocker ('sandbox.auth_mode', WANDB_AUTH_REFUSED) when auth_mode is not headers, whatever the executor kind. The message says why. Validation is unchanged, so manifest_valid.json and manifest.draft.json validate as before.
- applied: LD8: a failed wait is classified with the client's own _is_pull_failure, applied to the mapped error's __context__. On a pull failure with a fallback image, the failed sandbox is stopped and its meter row closed, then one re-create runs with image=fallback and fallback_image=None. A second failure is a retryable create error, as before. facts gain image and image_fallback.
- applied: LD8 safety: if stopping a failed sandbox fails, no second sandbox is created. This applies to every re-create, not only the fallback one. The row stays open at the worst case for cleanup_orphans, so the meter can only over-count.
- applied: LD9: nothing in these files (not mine to change).
- only a live call settles: L3: a bad image can stay in CREATING with ImagePullBackOff until create_wait_s. The wait then raises SandboxTimeoutError with no status_reason, so this is not classed as a pull failure and the re-create uses the same image.
- only a live call settles: The exact status_reason text and kubelet prefix the live backend sends for a pull failure (PF6).
- only a live call settles: Whether stop() on a sandbox that failed to start releases billing without a delete (L8).
- CLI owner: `python -m mhforge export` now starts with cwd = its per-child home, not the package root; use absolute paths only (PYTHONPATH carries the package)
- Publisher owner (LD9 Reports): WandbSdkSink refuses WANDB_API_KEY in os.environ; set it for Reports only after the adapter's wandb calls, or in a separate process
- adapter_wandb now relies on the private Api._service_api (no public GraphQL in wandb 0.31.0); test_sdk_signatures fails if a wandb upgrade moves it
- Publisher/live-check owners: reuse adapter_wandb.config_dir(paths) or a mkdtemp home under STATE for WANDB_CONFIG_DIR/HOME
- _sdk_graphql signature changed to (api, query, variables) and _import_gql removed; both module-private, no other caller
- Homes that SDKs write into are kept under STATE/export/homes for inspection; nothing deletes them
- applied: LD9: each export child gets a new mkdtemp 0700 home under STATE/export/homes as HOME, WANDB_CONFIG_DIR and cwd; removed after when empty
- applied: LD9: exporter_env sets WANDB_CONFIG_DIR=HOME, refuses home == caller HOME, carries no OTEL_/CES_/CWSANDBOX_/other WANDB_ names
- applied: LD9: key reaches wandb only via Settings(api_key) / Api(api_key), Agent Lens via init(api_key); base_url pinned in both
- applied: LD9: W&B adapter refuses foreign WANDB_*/CES_*, WANDB_CONFIG_DIR outside STATE or with settings, and cwd wandb/settings
- applied: LD7/LD8: not applicable to these files (sandbox owners)
- only a live call settles: trace.wandb.ai /agents/otel/v1/traces accepting a Forge key and entity/project; read-back endpoint shape
- only a live call settles: PROJECT_QUERY fields (access, readOnly, weavePermissions) via execute_graphql on Forge
- only a live call settles: EVAL_TABLES_CES enabled for the account; CES write inside run.log; eval-table-ces in summary_metrics
- only a live call settles: wandb.Api(api_key) verification; wandb.init fixed id with resume=None on rerun; artifact upload and wait()
- only a live call settles: whether wandb-core reads env/netrc itself
- only a live call settles: what the SDKs write into the child home during a live run
- only a live call settles: server-side span/request size limits, billing
