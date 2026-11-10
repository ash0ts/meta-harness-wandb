# Lead decisions after verification

These were decided by the lead after the independent check of the amended spec. Where one of them
contradicts the spec text in this slice, the decision wins. Each names the owners it binds.

## LD1 Reports directory in modes `restricted` and `contained` [owners: L2-proposer, L4-preflight, L0-docs]

This replaces the third construction rule of 3.19 ("Reports are read-only in mode `restricted`"), the
`reports` part of the `denyWrite` row of the settings table, and the last bullet pair of the addendum.
Reason: the skill's Step 0 (`SKILL.md` L46-L48) has the proposer write missing post-eval reports under
`LOGS/reports/`, and later sessions read them. Denying that write changes the method being replicated.

The reports directory now follows the same pattern as `agents/`: new files can be created, existing
files cannot be changed.

- `SettingsInputs` gains `report_files: tuple[str, ...]`: the names of the entries of `<LOGS>/reports`
  at session start (empty when the directory does not exist). `take_settings_inputs` lists it.
- In modes `restricted` and `contained` the runner creates `<LOGS>/reports/` when it is missing
  (`mkdir(parents=True, exist_ok=True)`) before it takes the settings inputs. Mode `upstream` is untouched.
- `permissions.allow` (mode `restricted` only) has four rules: the three of the table plus
  `Edit(//<LOGS>/reports/**)`.
- `permissions.deny`, edit rules (modes `restricted` and `contained`): also `Edit(//<LOGS>/reports/<name>)`
  for every entry of `report_files`.
- `sandbox.filesystem.denyWrite` (modes `restricted` and `contained`): `<LOGS>/<name>` for every entry of
  `logs_entries` other than `pending_eval.json` and `reports`; plus `<LOGS>/reports/<name>` for every
  entry of `report_files`.
- Addendum: the bullet that starts "You can create new files under agents/" becomes exactly

  ```
  - You can create new files under agents/ and in the reports directory, and write pending_eval.json at the
    path the task prompt gives. Existing files under agents/ and in the reports directory, and every other
    project file, are read-only in this session.
  ```

  The sentence "If a step of the skill would write a report, skip that step." is removed.
- Post-session integrity (P6): a new entry directly under `<LOGS>/reports/` is allowed. Its name goes
  into the session record's integrity data under the key `reports_added` (a sorted list of names). A
  changed or deleted report that existed at session start is a tree violation, handled like a modified
  agent file in the same mode.
- Report text is never exported or published. It is treated like `memory.json` and `log.jsonl` (C5):
  it may quote dataset text.
- Tests (`test_proposer.py`): add `test_reports_follow_the_agents_pattern` (a new report name has no
  deny entry; an existing report has an `Edit` deny and a `denyWrite` entry; `reports` is not in the
  `<LOGS>` `denyWrite` list; the allow list has four rules in mode `restricted`) and
  `test_new_report_is_recorded_changed_report_is_a_violation`. Adjust `test_generated_settings_restricted`
  to the four allow rules.
- `preflight.py` (PF9): add one probe pair to the proposer probes: creating a new file under the reports
  directory succeeds; changing an existing report is denied.
- Section 7, V32: restricted mode no longer skips Step 0; the remaining deviation is the addendum itself.

## LD2 Round-two constant lists live in `wire.py` [owners: L0-core, L1-manifest, L1-ledger, L1-stub, L2-proposer, L3-jobs, L3-exec-sandbox, L3-export, L3-qualification, L4-driver, L4-preflight, L5-integration]

`wire.py` (owner `L0-core`) also defines these four tuples, next to `JOB_PHASES`:

```python
CAMPAIGN_KINDS = ("preflight", "measured")                       # 2.3 campaign_kind
METERS = ("sandbox_seconds", "lens_bytes", "lens_spans", "proposer_list_price",
          "wandb_upload_bytes", "eval_tables")                    # 2.10 meter_events.meter
LENS_BUCKETS = ("main", "preflight", "recovery")                 # 2.10 meter_events.bucket for lens_bytes and lens_spans
FAILURE_CLASSES = ("import_error", "dependency_missing", "context_limit", "request_ceiling", "stream_budget",
                   "relay_overloaded", "refusal_flood", "harmony_vocab", "worker_exception", "host_sleep",
                   "unknown")                                     # 2.6 failure_class (or None)
```

`test_wire.py` gains `test_round_two_constant_lists` (the four tuples equal the lists above).

Every later owner uses these names instead of repeating the literals. `ledger.py` builds the `CHECK`
lists of its DDL from `wire.LEDGER_PHASES` and `wire.METERS`, or asserts in a test that the DDL text
and the tuples agree. Files under `mhforge/insandbox/` cannot import `mhforge`; where one needs a
list it carries a copy and a host-side test pins the copy to `wire`.

## LD3 An imported calibration verdict needs identical calibration fields [owners: L1-manifest, L3-qualification]

`IMPORT_DIFF_ALLOWED` still lets fields under `calibration.` differ between the preflight manifest and
the measured manifest. But the source campaign's calibration verdict stands in for this campaign's only
when `passed.json["imported_from"]["diff_paths"]` holds no path under `calibration.`. When one does, the
`search` gate requires this campaign's own calibration verdict (`evaluate --calibration` run in this
campaign), exactly as if nothing had been imported.

- `manifest.py`: the `search` gate applies the rule above when it decides whether a calibration verdict exists.
- `qualification.py`: `import_problems` adds no message for a `calibration.` difference (it is allowed);
  the function that reads the calibration verdict for a campaign follows the rule above.
- Tests: `test_manifest.py` gains `test_imported_calibration_verdict_refused_when_calibration_fields_differ`;
  `test_qualification.py` gains `test_calibration_verdict_not_borrowed_across_different_gates`.

## LD4 Proposer span text passes the verbatim guard [owners: L2-spans, L3-export, L4-publish]

Found by the layer-2 review: `plan_spans.plan_proposer` takes no verbatim index, so a tool result that is
attributed to one dataset with a loosened content rule (for example the output of a shell glob) can carry
another dataset's text out under the looser rule.

- `plan_proposer` gains the keyword `verbatim=None`: the same verbatim index the other span planners take.
- When it is given, every text a proposer span would carry under a rule other than `hash` goes through
  `policy.guard_text` with that index, as solver-span text does. A text that holds a verbatim run of a
  dataset whose rule for that field is stricter than the rule being applied is withheld (hash form).
- When it is `None` and any content rule that applies to proposer spans is not `hash`, `plan_proposer`
  applies `hash` to every text and records the problem `verbatim_index_missing` (it fails closed).
- The exporter builds the index once per export and passes it to every planner that accepts it.

## LD5 A prediction is shown only when it is a known label [owners: L2-tables, L3-export, L4-publish]

Found by the layer-2 review: 2.14 publishes a prediction as a label of up to 200 characters. A model that
fails to answer with a label often echoes part of the question, and LawBench case text must never be
published.

- In `plan_tables`, for a dataset whose prediction and target are shown in clear: a prediction is shown
  in clear only when it equals a target string that occurs in the rows of the same table (exact match on
  the strings as the table shows them). Any other prediction is written in the same hash form that a
  gated dataset's cell uses, and the table plan counts those rows under `predictions_withheld`.
- Correct predictions are unaffected (a correct prediction equals its target).
- The exporter and the publisher treat the hash form as they already do for a gated dataset.

## LD6 A settings override can only add [owners: L2-proposer, L4-preflight]

Found by the layer-2 review: `merge_settings` lets an override replace a generated list or object with
null, which removes every deny rule and still passes `check_settings`.

- `check_settings(settings, mode, *, base=None)`: `base` is the generated settings before the merge.
  With `base` it also returns a problem for (a) any key whose generated value is a list or an object and
  whose merged value has another type; (b) any element of a generated list under `permissions.deny`,
  `sandbox.filesystem.denyRead`, `sandbox.filesystem.denyWrite` or `sandbox.credentials.files` that is
  missing from the merged settings; (c) a non-empty `sandbox.excludedCommands`; (d) a
  `permissions.additionalDirectories` that differs from the generated value.
- The runner passes `base` whenever an override was merged. A problem stops the session before it starts
  (`CampaignStop("proposer_environment")`), like the existing invariants.

## LD7 Live sandboxes authenticate with explicit headers only [owners: L1-manifest, L2-bundle, L4-preflight]

Found by the cwsandbox 1.27.0 check: in auth mode `wandb_auth` the SDK takes W&B's session credential and
host from wandb's own settings, not the key we pass. A live command therefore requires the manifest's
sandbox auth mode to be the explicit-headers mode; `wandb_auth` is refused by the live gate with a message
that says why. Offline tests and fakes may still use either mode.

## LD8 The fallback image is tried when the pull fails after create [owners: L3-exec-sandbox]

Found by the same check: `Sandbox.run` returns on acceptance and an image pull failure surfaces later, from
waiting for the sandbox to run. When that wait fails with the client's pull-failure classification and the
manifest has a fallback image, the executor stops the failed sandbox, closes its meter row, and creates
once more with the fallback image. The attempt's record says which image ran. A second pull failure is an
infrastructure failure as before.

## LD9 The export child has its own empty home [owners: L1-manifest, L3-export, L4-publish, L4-preflight]

Found by the SDK reference: Agent Lens ignores `NETRC` and reads `$HOME/.netrc`; wandb reads a global
settings file under the real home and `<cwd>/wandb/settings`; Reports build a fresh `wandb.Api()` from
whatever credential wandb finds. Every process that imports a Forge SDK (export child, publisher, live
checks) therefore runs with `HOME` and `WANDB_CONFIG_DIR` pointing at an empty directory under `STATE`
that the run creates (mode 0700), with a working directory that holds no `wandb/settings`, and with none of
`OTEL_*`, `CES_*`, `CWSANDBOX_*` or `WANDB_*` in its environment except the names the adapter sets itself.
The key reaches wandb only through `Settings(api_key=...)` or `Api(api_key=...)`, or, where an SDK offers
no parameter (Reports), through `WANDB_API_KEY` set inside that child process after it has read the key
from the inherited pipe.
