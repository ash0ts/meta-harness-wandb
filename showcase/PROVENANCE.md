# Upstream pin and read-through notes

## Pin

| | |
|---|---|
| Upstream | https://github.com/stanford-iris-lab/meta-harness |
| Commit | `f805ad3aa4ada290458c1e0deadbe132fbbdea4e` (2026-11-05) |
| Licence | MIT, © 2026 Yoonho Lee |
| Branch | `showcase/forge`, created from the commit above |
| Example used | `reference_examples/text_classification` |

No upstream file has been modified. Upstream ships no `uv.lock` and pins only
`litellm==1.84.10`, so a lock file still has to be generated and committed here
before any measured run.

## What the pinned code does

Read from the source at the pinned commit. Nothing below has been run yet.

**Outer loop** (`meta_harness.py`)

- Phase 0 evaluates the baselines `no_memory` and `fewshot_all`.
- Each iteration: propose, import-check each candidate, benchmark each candidate
  on every dataset, update the frontier, append to `evolution_summary.jsonl`.
- Default is 20 iterations. `--iterations 0` runs the baselines only.
- `--test` freezes a named run and scores the frontier on the held-out split once.

**Datasets** (`config.yaml`, train / val / test)

- USPTO 50 / 30 / 100
- Symptom2Disease 200 / 50 / 212
- LawBench 200 / 50 / 100

**Proposer** (`claude_wrapper.py`, `.claude/skills/meta-harness/SKILL.md`)

- Runs `claude --dangerously-skip-permissions -p` with model `opus`, effort `max`
  and a 2400 second timeout, in the example directory.
- Allowed tools: Read, Glob, Grep, Agent, Write, Edit, Bash.
- `ANTHROPIC_API_KEY` is removed from the environment so the CLI uses
  subscription auth.
- The skill requires exactly three candidates per iteration, written to `agents/`
  and listed in `pending_eval.json`.
- Evidence the proposer is told to read: `evolution_summary.jsonl`,
  `frontier_val.json`, `config.yaml`, and the per-run traces at
  `logs/<run>/<dataset>/<memory>/<model>/log.jsonl`.

**Solver** (`benchmark.py`, `inner_loop.py`, `llm.py`)

- Each candidate and dataset is one subprocess of
  `python -m text_classification.inner_loop`, 16 at a time.
- Each writes `val.json` (accuracy and token counts), `memory.json` and `log.jsonl`.
- Calls go through LiteLLM with temperature 0.0, the system prompt
  `Reasoning: medium` for gpt-oss models, and harmony parsing of the reply.

## Consequences for running on CoreWeave Forge

- **Model string.** With `api_base` set, a model starting with `openai/` is passed
  to LiteLLM as is, and LiteLLM strips that prefix. Serverless Inference's id is
  `openai/gpt-oss-120b`, so the config entry needs to be
  `openai/openai/gpt-oss-120b`, or the gateway has to restore the prefix.
  Inferred from `_normalized_model`; not yet confirmed by a call.
- **API key.** With `api_base` set, `llm.py` sends the key `local` unless one is
  passed, and `inner_loop.py` never passes one. A direct call to Serverless
  Inference would be unauthenticated. Pointing `api_base` at the gateway, which
  holds the Forge key, needs no upstream change.
- **Cost.** `llm.py` records a cost of 0.0 whenever `api_base` is set. Token counts
  are still written, so the ledger has to price the calls.
- **Cache.** Replies are cached under `~/.cache/text-classification/litellm`, and
  cached calls skip the usage counters. Set `TEXT_CLASSIFICATION_LLM_CACHE_DIR`
  per run to keep accounting clean.
- **Sandboxes.** The candidate's `predict()` makes the LLM calls, so once
  evaluation moves into a sandbox those calls start inside it. The gateway then
  has to be reachable from the sandbox, or the calls have to be relayed back to
  the host. This is undecided.
