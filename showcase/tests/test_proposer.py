# SPDX-License-Identifier: MIT
"""Tests for mhforge/proposer.py (spec 3.19, 2.11, 5.2, LD1). Sessions run the fake CLI through the fake wrapper (or the
real claude_wrapper from a workspace copy); the real claude CLI is never run."""

from __future__ import annotations

import contextlib
import io
import json
import os
import random
import tempfile
import types
import unittest
from pathlib import Path

from mhforge import proposer as P
from mhforge import util, workspace
from mhforge.errors import CampaignStop
from mhforge.journal import Journal, read_journal
from mhforge.ledger import Ledger
from mhforge.paths import StatePaths
from mhforge.proposer_shim import CREDENTIAL_READ_DENIES
from mhforge.telemetry import transcript as tx
from mhforge.workspace import PINNED_FILES, Upstream
from tests import fake_claude_wrapper
from tests.fake_claude_cli import write_fake_cli
from tests.helpers import FIXTURES, FakeClock, example_dir, make_manifest, needs_example_dir, stamp_rows, tmp_state

SID = "5e55104e-0000-4000-8000-00000000abcd"
PROMPT = "UNIQUE-PROMPT-TEXT run iteration 1"
SKILL = "UNIQUE-SKILL-TEXT of the meta-harness skill"
TOOLS = ["Read", "Glob", "Grep", "Agent", "Write", "Edit", "Bash"]


def _fixture_rows(name: str) -> list[dict]:
    return stamp_rows((FIXTURES / name).read_text().splitlines(), "2026-11-10T01:00:00.000000Z", 0.5)


def _fixture_session(drop_cost_state: bool = False) -> tx.NormalizedSession:
    recs, subs = tx.read_session_dir(FIXTURES / "transcript_synthetic", SID, max_bytes=1 << 24)
    if drop_cost_state:
        recs = [r for r in recs if r.get("type") != "cost-state"]
    return tx.normalize_transcript(recs, expect_version="2.1.317", subagents=subs)


def _write_example(root: Path) -> Path:
    for rel in PINNED_FILES:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SKILL + "\n" if rel.endswith("SKILL.md") else f"# synthetic pinned file {rel}\n")
    return root


def _write_venv(root: Path) -> Path:
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "python").write_text("")
    (root / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (root / "lib" / "python3.13" / "site-packages" / "pkg-1.0.dist-info").mkdir(parents=True)
    return root


class Env:
    """One temp campaign: a synthetic example tree, a fake venv, a fake home, the fake CLI, ledger, journal, runner."""

    def __init__(self, case: unittest.TestCase, mode: str = "upstream", sessions=("ok",), clock=None,
                 wrapper: types.ModuleType | None = None, example: Path | None = None, **overrides) -> None:
        self.case = case
        self.stack = contextlib.ExitStack()
        case.addCleanup(self.stack.close)
        self.top = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="mhf-prop-"))).resolve()
        self.example = example or _write_example(self.top / "example")
        self.venv = _write_venv(self.top / "venv")
        self.home = self.top / "home"
        self.home.mkdir()
        self.scratch_root = self.top / "scratch"
        self.cli = write_fake_cli(self.top / "bin", "ok")
        doc = make_manifest({"upstream.tree_sha256": workspace.source_digest(self.example)[0],
                             "upstream.worker_python": str(self.venv / "bin" / "python"),
                             "proposer.cli_path": str(self.cli.path), "proposer.cli_sha256": self.cli.sha256,
                             "proposer.scratch_root": str(self.scratch_root), "proposer.grace_s": [1, 1],
                             "proposer.transient_backoff_s": [60, 300],
                             "decisions.proposer_permission_mode": mode, **overrides})
        st = self.stack.enter_context(tmp_state(manifest=doc))
        self.manifest, self.paths, self.sc = st.manifest, st.paths, st.sc
        self.ws = self.paths.ws
        self.logs = self.paths.logs_dir(self.manifest["campaign"])
        self.pending = self.logs / "pending_eval.json"
        self.summary = self.logs / "evolution_summary.jsonl"
        self.frontier = self.logs / "frontier_val.json"
        self.clock = clock if clock is not None else FakeClock()
        self.mh = types.SimpleNamespace(LOGS_DIR=self.logs, PENDING_EVAL=self.pending, FRONTIER_VAL=self.frontier,
                                        EVOLUTION_SUMMARY=self.summary, EVOLVE_DIR=self.ws,
                                        PROPOSER_ALLOWED_TOOLS=list(TOOLS))
        self.wrapper = wrapper or fake_claude_wrapper.new_module(self.ws / ".empty_plugins")
        self.upstream = Upstream(meta_harness=self.mh, benchmark=types.SimpleNamespace(), claude_wrapper=self.wrapper)
        self.units: list[str] = []
        self.halt_checks = 0
        self.configure(*sessions)

    def configure(self, *sessions, **defaults) -> None:
        opts = {"pending_path": str(self.pending), "summary_path": str(self.summary)}
        opts.update(defaults)
        self.cli.configure(*sessions, **opts)

    def _halt(self) -> None:
        self.halt_checks += 1

    def runner(self) -> P.ProposerRunner:
        return P.ProposerRunner(self.manifest, self.paths, journal=Journal(self.paths.journal, clock=self.clock),
                                ledger=Ledger(self.paths.ledger, self.manifest, clock=self.clock), upstream=self.upstream,
                                example_dir=self.example, lock_file=None,
                                base_env={"HOME": str(self.home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                                home=self.home, on_unit=self.units.append, halt_check=self._halt, clock=self.clock,
                                rng=random.Random(7))

    def propose(self, iteration: int = 1, runner: P.ProposerRunner | None = None, timeout: int = 60):
        with contextlib.redirect_stdout(io.StringIO()):
            return (runner or self.runner()).propose(PROMPT, iteration, timeout=timeout)

    def stop(self, iteration: int = 1) -> CampaignStop:
        with self.case.assertRaises(CampaignStop) as ctx:
            self.propose(iteration)
        return ctx.exception

    def sdir(self, iteration: int = 1, k: int = 1) -> Path:
        return self.paths.session_dir(iteration, k)

    def session(self, iteration: int = 1, k: int = 1) -> dict:
        return json.loads((self.sdir(iteration, k) / "session.json").read_text())

    def ledger(self) -> Ledger:
        return Ledger(self.paths.ledger, self.manifest, clock=self.clock)


def _facts(**kw) -> P.SessionFacts:
    base = dict(mode="upstream", exit_code=0, timed_out=False, result={"is_error": False, "subtype": "success"},
                pending_ok=True, stderr_text="", limit=P.limit_signal({}), model_check={"mismatch": None},
                sandbox_check={"passed": True, "transcript_checked": True}, api_retry_errors=())
    base.update(kw)
    return P.SessionFacts(**base)


def _err(**kw) -> dict:
    out = {"type": "result", "subtype": "success", "is_error": True, "result": "", "api_error_status": None}
    out.update(kw)
    return out


# ================================================================ classifier

class TestClassifier(unittest.TestCase):
    def setUp(self) -> None:
        self.m = make_manifest()

    def c(self, **kw):
        return P.classify_session(_facts(**kw), self.m)

    def test_classification_table(self) -> None:
        window = {"signal": "rate_limit_event", "status": "rejected", "rate_limit_type": "five_hour",
                  "resets_at_utc": "2026-11-10T05:00:00.000000Z", "error_code": None, "overage_status": None,
                  "overage_disabled_reason": None}
        rows = [
            (dict(exit_code=127, result=None), ("cli_missing", None)),
            (dict(result=_err(startup_failure_reason="sandbox_required_unavailable"), exit_code=1),
             ("sandbox_unavailable", None)),
            (dict(model_check={"mismatch": "fallback_event"}), ("model_mismatch", "fallback_event")),
            (dict(mode="restricted", sandbox_check={"passed": False, "transcript_checked": False}),
             ("settings_ignored", "transcript_missing")),
            (dict(mode="contained", sandbox_check={"passed": False, "transcript_checked": True}),
             ("settings_ignored", "unsandboxed")),
            (dict(mode="upstream", sandbox_check={"passed": False, "transcript_checked": True}), ("ok", None)),
            (dict(), ("ok", None)),
            (dict(pending_ok=False), ("no_candidates", None)),
            (dict(timed_out=True), ("timeout", None)),
            (dict(timed_out=True, result=None, exit_code=0), ("timeout", None)),
            (dict(result=_err(subtype="error_max_budget_usd")), ("failed", "max_budget_usd")),
            (dict(result=_err(subtype="error_max_turns")), ("failed", "max_turns")),
            (dict(result=_err(api_error_status=429), limit=dict(window, rate_limit_type="overage",
                                                                 overage_disabled_reason="out_of_credits",
                                                                 resets_at_utc=None)),
             ("spend_limit", "rate_limit_event")),
            (dict(result=_err(), limit=dict(window, error_code="credits_required")), ("spend_limit", "rate_limit_event")),
            (dict(result=_err(api_error_status=429), limit=window), ("limit_window", "five_hour")),
            (dict(result=_err(), limit=dict(window, resets_at_utc=None)), ("spend_limit", "unrecognised_limit")),
            (dict(result=_err(), limit=dict(window, rate_limit_type="mystery")), ("spend_limit", "unrecognised_limit")),
            (dict(result=_err(api_error_status=401)), ("auth", "status")),
            (dict(result=_err(result="Not logged in"), stderr_text=""), ("auth", "text")),
            (dict(result=_err(api_error_status=402)), ("spend_limit", "status")),
            (dict(result=_err(api_error_status=429)), ("spend_limit", "status")),
            (dict(result=_err(result="Usage limit reached")), ("spend_limit", "text")),
            (dict(result=_err(api_error_status=529)), ("transient", "status")),
            (dict(result=_err(api_error_status=408)), ("transient", "status")),
            (dict(result=_err(), api_retry_errors=("rate_limit", "overloaded")), ("transient", "api_retry")),
            (dict(result=None, exit_code=0), ("transient", "no_result_event")),
            (dict(result=_err(result="something else")), ("failed", None)),
            (dict(result=None, exit_code=0, timed_out=False, pending_ok=True), ("transient", "no_result_event")),
        ]
        for kw, expected in rows:
            self.assertEqual(self.c(**kw), expected, kw)

    def test_ok_session_mentioning_limit_reached_stays_ok(self) -> None:
        ok = {"is_error": False, "subtype": "success", "result": "falls back when the char limit reached; usage limit"}
        self.assertEqual(self.c(result=ok), ("ok", None))
        self.assertEqual(self.c(result=ok, pending_ok=False), ("no_candidates", None))
        env = Env(self, sessions=("mentions_limit",))
        self.assertTrue(env.propose())
        self.assertEqual(env.session()["outcome"], "ok")

    def test_text_patterns_are_fallback_only(self) -> None:
        text = _err(result="usage limit reached, resets at 5pm")
        self.assertEqual(self.c(result=text), ("spend_limit", "text"))
        window = P.limit_signal(P.parse_stream([{"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "resetsAt": 1794286800, "rateLimitType": "seven_day"}}]))
        self.assertEqual(self.c(result=text, limit=window), ("limit_window", "seven_day"))   # structured first
        self.assertEqual(self.c(result=_err(result="no pattern here"), stderr_text="please run /login"),
                         ("auth", "text"))
        self.assertEqual(self.c(result={"is_error": False, "result": "please run /login"}), ("ok", None))
        m = make_manifest({"proposer.usage_limit_patterns": [], "proposer.auth_patterns": []})
        self.assertEqual(P.classify_session(_facts(result=text), m), ("failed", None))

    def test_structured_limit_fields_before_text(self) -> None:
        # stream_limit_synthetic.jsonl is INFERRED, never observed (spec 5.1): its shape is a guess.
        expected = json.loads((FIXTURES / "transcript_synthetic" / "EXPECTED.json").read_text())
        parsed = P.parse_stream(_fixture_rows("stream_limit_synthetic.jsonl"))
        limit = P.limit_signal(parsed)
        self.assertEqual(limit["signal"], "rate_limit_event")
        self.assertEqual(limit["rate_limit_type"], "five_hour")
        self.assertEqual(util.parse_utc(limit["resets_at_utc"]),
                         float(expected["usage_limit_stream"]["wait_until_epoch_s"]))
        mc = P.check_model(parsed, requested_model="opus", expected_resolved=None, requested_effort="max",
                           request_efforts=None)
        self.assertIsNone(mc["mismatch"])                       # the "<synthetic>" error message is not a model reply
        facts = _facts(result=parsed["result"], exit_code=1, limit=limit, model_check=mc, pending_ok=False,
                       api_retry_errors=tuple(e["error"] for e in parsed["api_retries"]))
        self.assertEqual(P.classify_session(facts, self.m), ("limit_window", "five_hour"))

    def test_unrecognised_limit_halts(self) -> None:
        lim = P.limit_signal({"rate_limits": [{"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}}]})
        self.assertEqual(self.c(result=_err(), limit=lim), ("spend_limit", "unrecognised_limit"))
        env = Env(self, sessions=({"modes": ["limit_window"], "rate_limit_type": "mystery_window",
                                   "resets_at": 1794286800},))
        stop = env.stop()
        self.assertEqual(stop.reason, "proposer_spend_limit")
        self.assertEqual(env.session()["outcome_detail"], "unrecognised_limit")
        self.assertEqual(env.clock.sleeps, [])                  # no wait without a recognised plan window


# ================================================================ model, sandbox, integrity checks

def _asst(model: str, parent: str | None = None) -> dict:
    return {"type": "assistant", "parent_tool_use_id": parent, "message": {"model": model, "content": []}}


class TestChecks(unittest.TestCase):
    def mc(self, events, **kw):
        args = dict(requested_model="opus", expected_resolved=None, requested_effort="max", request_efforts=None)
        args.update(kw)
        return P.check_model(P.parse_stream(events), **args)

    def init(self, **kw):
        ev = {"type": "system", "subtype": "init", "model": "claude-opus-5-5"}
        ev.update(kw)
        return ev

    def test_model_checked_on_every_main_thread_event_and_fallback_events(self) -> None:
        ok = self.mc([self.init(), _asst("claude-opus-5-5"), _asst("claude-opus-5-5")])
        self.assertIsNone(ok["mismatch"])
        self.assertEqual(ok["resolved_model"], "claude-opus-5-5")
        later = self.mc([self.init(), _asst("claude-opus-5-5"), _asst("claude-sonnet-5-5")])
        self.assertEqual(later["mismatch"], "main_thread_model_changed")
        fb = self.mc([self.init(), _asst("claude-opus-5-5"),
                      {"type": "system", "subtype": "model_refusal_fallback", "from": "a", "to": "b"}])
        self.assertEqual(fb["mismatch"], "fallback_event")
        self.assertEqual(self.mc([self.init(), _asst("claude-sonnet-5-5")])["mismatch"],
                         "resolved_model_differs_from_request")
        self.assertEqual(self.mc([self.init(model="claude-sonnet-5-5"), _asst("claude-opus-5-5")])["mismatch"],
                         "init_model_differs")
        pinned = dict(requested_model="claude-opus-4-6")
        self.assertIsNone(self.mc([self.init(model="claude-opus-4-6"), _asst("claude-opus-4-6-20260101")],
                                  **pinned)["mismatch"])
        self.assertEqual(self.mc([self.init(model="claude-opus-4-6"), _asst("claude-opus-4-7")], **pinned)["mismatch"],
                         "resolved_model_differs_from_request")
        self.assertEqual(self.mc([self.init(), _asst("claude-opus-5-5")],
                                 expected_resolved="claude-opus-5-6")["mismatch"], "resolved_model_differs_from_expected")

    def test_subagent_models_recorded_not_asserted(self) -> None:
        mc = self.mc(_fixture_rows("stream_synthetic.jsonl"), request_efforts=tx.request_efforts(_fixture_session()))
        self.assertIsNone(mc["mismatch"])
        self.assertEqual(mc["subagent_models"], ["claude-haiku-5-5"])
        self.assertEqual(mc["main_thread_models"], ["claude-opus-5-5"])
        self.assertEqual(mc["request_efforts"], ["max", "max", "max"])   # the subagent's "high" is not main thread

    def test_effort_mismatch_from_init_or_transcript(self) -> None:
        self.assertEqual(self.mc([self.init(effort="high"), _asst("claude-opus-5-5")])["mismatch"],
                         "init_effort_differs")
        self.assertEqual(self.mc([self.init(), _asst("claude-opus-5-5")], request_efforts=["max", "high"])["mismatch"],
                         "request_effort_differs")
        self.assertIsNone(self.mc([self.init(effort="max"), _asst("claude-opus-5-5")],
                                  request_efforts=["max"])["mismatch"])

    def test_unsandboxed_bash_result_is_settings_ignored(self) -> None:
        session = _fixture_session()
        parsed = P.parse_stream(_fixture_rows("stream_synthetic.jsonl"))
        good = P.check_sandboxed(tx.bash_results(session), parsed["bash_tool_uses"])
        self.assertTrue(good["passed"])
        self.assertEqual((good["bash_results"], good["unsandboxed"], good["unknown"]), (1, 0, 0))
        bad = P.check_sandboxed([{"sandboxed": False}], 1)
        self.assertFalse(bad["passed"])
        self.assertFalse(P.check_sandboxed([{"sandboxed": None}], 1)["passed"])       # key absent
        self.assertTrue(P.check_sandboxed(None, 0)["passed"])                         # no Bash call at all
        env = Env(self, mode="contained", sessions=(("writes_pending", "unsandboxed_bash"),))
        self.assertEqual(env.stop().reason, "proposer_settings")
        rec = env.session()
        self.assertEqual((rec["outcome"], rec["outcome_detail"]), ("settings_ignored", "unsandboxed"))
        self.assertEqual(rec["sandbox_check"]["unsandboxed"], 1)
        self.assertFalse((env.sdir() / "proposal.json").exists())

    def test_missing_transcript_in_restricted_mode_is_settings_ignored(self) -> None:
        env = Env(self, mode="restricted", sessions=({"modes": ["writes_pending"], "write_transcript": False},))
        self.assertEqual(env.stop().reason, "proposer_settings")
        rec = env.session()
        self.assertEqual((rec["outcome"], rec["outcome_detail"]), ("settings_ignored", "transcript_missing"))
        self.assertFalse(rec["transcript"]["found"])

    def test_integrity_violations_by_mode(self) -> None:
        ws = Path("/w/ws")
        init = {"plugins": [], "skills": [], "mcp_servers": [], "memory_paths": [], "claude_code_version": "2.1.317"}
        drift = {"agents_modified": ["agents/old.py"], "quarantined": ["stray.md"], "upstream_modified": [],
                 "upstream_removed": [], "logs_modified": [], "reports_modified": []}
        kw = dict(ws=ws, cli_version="2.1.317", memory_dir_changed=False, memory_saved_events=0)
        self.assertEqual(P.integrity_violations(init, drift, mode="upstream", **kw), [])
        for mode in ("restricted", "contained"):
            got = P.integrity_violations(init, drift, mode=mode, **kw)
            self.assertEqual(len(got), 2, mode)
        pinned = dict(drift, upstream_modified=["benchmark.py"], logs_modified=["logs/c/evolution_summary.jsonl"])
        for mode in ("upstream", "restricted", "contained"):
            got = P.integrity_violations(init, pinned, mode=mode, **kw)
            self.assertTrue(any("benchmark.py" in v for v in got), mode)
            self.assertTrue(any("evolution_summary" in v for v in got), mode)
        self.assertEqual(len(P.integrity_violations(init, {}, mode="upstream", ws=ws, cli_version="2.1.317",
                                                    memory_dir_changed=True, memory_saved_events=1)), 2)
        ok_plugin = dict(init, plugins=[{"name": "e", "path": str(ws / ".empty_plugins")}])
        self.assertEqual(P.integrity_violations(ok_plugin, {}, mode="upstream", **kw), [])
        self.assertEqual(len(P.integrity_violations(dict(init, claude_code_version="2.1.318"), {}, mode="upstream",
                                                    **kw)), 1)

    def test_only_exact_hash_pinned_builtin_privacy_plugin_is_accepted(self) -> None:
        ws = Path('/tmp/ws')
        plugin = {'name': 'cc-plugin-privacy', 'path': 'builtin', 'source': 'cc-plugin-privacy@builtin'}
        init = {'plugins': [plugin], 'claude_code_version': '2.1.317'}
        kw = dict(mode='restricted', ws=ws, cli_version='2.1.317', memory_dir_changed=False,
                  memory_saved_events=0, cli_sha256=P.PINNED_PRIVACY_PLUGIN[1])
        self.assertEqual(P.integrity_violations(init, {}, **kw), [])
        for changed in (dict(plugin, path='/tmp/builtin'), dict(plugin, source='privacy@other'),
                        dict(plugin, name='cc-plugin-telemetry')):
            self.assertTrue(P.integrity_violations(dict(init, plugins=[changed]), {}, **kw))
        self.assertTrue(P.integrity_violations(init, {}, **dict(kw, cli_sha256='0'*64)))
        self.assertTrue(P.integrity_violations(init, {}, **dict(kw, cli_version='different')))

    def test_init_with_plugins_skills_mcp_or_memory_paths_halts(self) -> None:
        kw = dict(drift={}, mode="upstream", ws=Path("/w"), cli_version="2.1.317", memory_dir_changed=False,
                  memory_saved_events=0)
        base = {"plugins": [], "skills": [], "mcp_servers": [], "memory_paths": [], "claude_code_version": "2.1.317"}
        for key, value in (("plugins", [{"name": "p", "path": "/x"}]), ("skills", ["s"]),
                           ("mcp_servers", [{"name": "m"}]), ("memory_paths", ["/m"])):
            self.assertEqual(len(P.integrity_violations(dict(base, **{key: value}), **kw)), 1, key)
        env = Env(self, sessions=({"modes": ["writes_pending", "plugins_loaded"], "loaded": ["memory_paths"]},))
        stop = env.stop()
        self.assertEqual(stop.reason, "proposer_integrity")
        rec = env.session()
        self.assertEqual(rec["outcome"], "ok")                  # the proposal stays stored and replays after a look
        self.assertTrue(rec["integrity"]["violations"])
        self.assertTrue((env.sdir() / "proposal.json").exists())


# ================================================================ generated settings

def _inputs(mode: str = "restricted", **kw) -> P.SettingsInputs:
    state = Path("/S/state/camp")
    ws = state / "workspace" / "text_classification"
    logs = ws / "logs" / "camp"
    base = dict(mode=mode, ws=ws, logs_dir=logs, state_root=state, package_dir=Path("/P/mhforge"),
                scratch_dir=Path("/T/mhf/camp/iter001.s1"), scratch_root=Path("/T/mhf"), example_venv=Path("/E/.venv"),
                uv_dir=Path("/H/.local/bin"), home=Path("/H"), uid=502,
                state_children=("calls", "jobs", "journal.jsonl", "ledger.sqlite", "proposer", "workspace"),
                state_siblings=("_smoke", "other-camp"),
                ws_top=(".claude", ".empty_plugins", "agents", "benchmark.py", "data", "logs", "uv.lock"),
                agent_files=("__init__.py", "fewshot_all.py", "no_memory.py"),
                logs_entries=("claude_sessions", "frontier_val.json", "pending_eval.json", "reports"),
                claude_tmp_projects=(P.transcript_slug(ws), "-Users-someone-else"),
                scratch_others=("camp/iter000.s1", "other/iter001.s1"), report_files=("iter001.md",))
    base.update(kw)
    return P.SettingsInputs(**base)


class TestSettings(unittest.TestCase):
    def test_generated_settings_restricted(self) -> None:
        s = P.generate_settings(_inputs())
        perms = s["permissions"]
        ws = "//S/state/camp/workspace/text_classification"
        self.assertEqual(perms["defaultMode"], "dontAsk")
        self.assertEqual(perms["allow"], [f"Edit({ws}/agents/**)", f"Edit({ws}/logs/camp/pending_eval.json)",
                                          "Edit(//T/mhf/camp/iter001.s1/**)", f"Edit({ws}/logs/camp/reports/**)"])
        for name in ("__init__.py", "fewshot_all.py", "no_memory.py"):
            self.assertIn(f"Edit({ws}/agents/{name})", perms["deny"])
        for name in (".claude", ".empty_plugins", "benchmark.py", "data", "uv.lock"):
            self.assertIn(f"Edit({ws}/{name})", perms["deny"])
            self.assertIn(f"Edit({ws}/{name}/**)", perms["deny"])
        self.assertFalse(any(r.startswith(f"Edit({ws}/agents)") or r == f"Edit({ws}/logs)" for r in perms["deny"]))
        for p in P.CREDENTIAL_PATHS:
            self.assertIn(f"Read({p})", perms["deny"])
            self.assertIn(f"Read({p}/**)", perms["deny"])
        self.assertTrue(perms["blockReadsOutsideWorkingDirectories"])
        self.assertTrue(perms["blockWritesOutsideWorkingDirectories"])
        sb = s["sandbox"]
        self.assertEqual({k: sb[k] for k in ("enabled", "failIfUnavailable", "allowUnsandboxedCommands",
                                             "autoAllowBashIfSandboxed")},
                         {"enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
                          "autoAllowBashIfSandboxed": True})
        self.assertEqual(sb["network"], {"allowedDomains": [], "strictAllowlist": True})
        self.assertEqual(sb["filesystem"]["allowRead"], ["/H/.local/share/uv/python", "/S/state/camp/workspace",
                                                         "/E/.venv", "/H/.local/bin"])
        self.assertEqual(sb["credentials"]["files"], [{"path": p, "mode": "deny"} for p in P.CREDENTIAL_PATHS])
        self.assertIn(f"{ws[1:]}/benchmark.py", sb["filesystem"]["denyWrite"])
        self.assertIn(f"{ws[1:]}/agents/no_memory.py", sb["filesystem"]["denyWrite"])
        self.assertIn(f"{ws[1:]}/logs/camp/frontier_val.json", sb["filesystem"]["denyWrite"])
        self.assertNotIn(f"{ws[1:]}/logs/camp/pending_eval.json", sb["filesystem"]["denyWrite"])
        self.assertEqual(P.check_settings(s, "restricted"), [])
        self.assertEqual(json.loads(util.canonical_json(s)), s)

    def test_reports_follow_the_agents_pattern(self) -> None:
        s = P.generate_settings(_inputs(report_files=("old.md",)))
        reports = "//S/state/camp/workspace/text_classification/logs/camp/reports"
        deny, deny_write = s["permissions"]["deny"], s["sandbox"]["filesystem"]["denyWrite"]
        self.assertIn(f"Edit({reports}/old.md)", deny)
        self.assertIn(f"{reports[1:]}/old.md", deny_write)
        self.assertFalse(any("new.md" in r for r in deny + deny_write))
        self.assertNotIn(reports[1:], deny_write)               # the reports directory itself stays writable
        self.assertEqual(len(s["permissions"]["allow"]), 4)
        contained = P.generate_settings(_inputs(mode="contained", report_files=("old.md",)))
        self.assertIn(f"Edit({reports}/old.md)", contained["permissions"]["deny"])
        self.assertNotIn(reports[1:], contained["sandbox"]["filesystem"]["denyWrite"])
        self.assertIn("in the reports directory", P.RESTRICTED_ADDENDUM)
        self.assertNotIn("skip that step", P.RESTRICTED_ADDENDUM)

    def test_no_blanket_state_deny_one_per_child_except_workspace(self) -> None:
        deny = P.generate_settings(_inputs())["permissions"]["deny"]
        self.assertNotIn("Read(//S/state/camp/**)", deny)
        self.assertNotIn("Read(//S/state/camp)", deny)
        for child in ("calls", "jobs", "journal.jsonl", "ledger.sqlite", "proposer"):
            self.assertIn(f"Read(//S/state/camp/{child})", deny)
            self.assertIn(f"Read(//S/state/camp/{child}/**)", deny)
        self.assertFalse(any("camp/workspace" in r and r.startswith("Read(") for r in deny))
        for sib in ("_smoke", "other-camp"):
            self.assertIn(f"Read(//S/state/{sib}/**)", deny)
        self.assertIn("Read(//P/mhforge/**)", deny)

    def test_other_scratch_folders_denied_by_name(self) -> None:
        inputs = _inputs()
        deny_read = P.generate_settings(inputs)["sandbox"]["filesystem"]["denyRead"]
        self.assertEqual(deny_read, ["/private/tmp/claude-502/-Users-someone-else", "/T/mhf/camp/iter000.s1",
                                     "/T/mhf/other/iter001.s1"])
        with tempfile.TemporaryDirectory(prefix="mhf-scr-") as tmp:
            root = Path(tmp).resolve()
            for rel in ("scratch/camp/iter001.s1", "scratch/camp/iter000.s1", "scratch/zz/iter001.s1",
                        "claude/-a", "claude/-b"):
                (root / rel).mkdir(parents=True)
            m = make_manifest({"proposer.scratch_root": str(root / "scratch")})
            paths = StatePaths(root / "sc" / "state" / "camp")
            got = P.take_settings_inputs(m, paths, mode="restricted", logs_dir=root / "logs",
                                         scratch_dir=root / "scratch" / "camp" / "iter001.s1", home=root,
                                         example_venv=root, uv_dir=None, uid=502, claude_tmp_root=root / "claude")
            self.assertEqual(got.scratch_others, ("camp/iter000.s1", "zz/iter001.s1"))
            self.assertEqual(got.claude_tmp_projects, ("-a", "-b"))
            self.assertEqual(got.report_files, ())

    def test_contained_and_upstream_settings(self) -> None:
        up = P.generate_settings(_inputs(mode="upstream"), override={"sandbox": {"enabled": False}})
        self.assertEqual(up, P.COMMON_SETTINGS)                 # the override is ignored in mode upstream
        c = P.generate_settings(_inputs(mode="contained"))
        self.assertNotIn("defaultMode", c["permissions"])
        self.assertNotIn("allow", c["permissions"])
        r = P.generate_settings(_inputs(mode="restricted"))
        self.assertEqual(c["permissions"]["deny"], r["permissions"]["deny"])
        self.assertEqual(c["sandbox"], r["sandbox"])
        self.assertEqual(P.check_settings(c, "contained"), [])
        self.assertEqual(P.check_settings(up, "upstream"), [])
        self.assertTrue(P.check_settings(c, "restricted"))      # no dontAsk

    def test_generated_settings_parse_as_json_with_common_keys_in_every_mode(self) -> None:
        for mode in ("upstream", "restricted", "contained"):
            s = json.loads(util.canonical_json(P.generate_settings(_inputs(mode=mode))).decode("utf-8"))
            for key, value in P.COMMON_SETTINGS.items():
                self.assertEqual(s[key], value, mode) if isinstance(value, dict) else self.assertIs(s[key], value, mode)
        with self.assertRaises(ValueError):
            P.generate_settings(_inputs(mode="bogus"))

    def test_override_merges_and_cannot_switch_off_the_sandbox(self) -> None:
        override = {"sandbox": {"filesystem": {"allowRead": ["/extra", "/E/.venv"]}},
                    "permissions": {"allow": ["Edit(//x/**)"]}}
        s = P.generate_settings(_inputs(), override=override)
        self.assertEqual(s["sandbox"]["filesystem"]["allowRead"][-1], "/extra")
        self.assertEqual(s["sandbox"]["filesystem"]["allowRead"].count("/E/.venv"), 1)
        self.assertEqual(s["permissions"]["allow"][-1], "Edit(//x/**)")
        self.assertEqual(P.check_settings(s, "restricted"), [])
        self.assertEqual(P.merge_settings({"a": {"b": 1, "c": [1]}, "d": 1}, {"a": {"c": [1, 2]}, "d": 2}),
                         {"a": {"b": 1, "c": [1, 2]}, "d": 2})
        for bad in ({"sandbox": {"enabled": False}}, {"sandbox": {"failIfUnavailable": False}},
                    {"sandbox": {"allowUnsandboxedCommands": True}}, {"sandbox": {"network": {"allowedDomains": ["x"]}}},
                    {"sandbox": {"network": {"strictAllowlist": False}}}, {"permissions": {"allow": ["Bash(ls:*)"]}},
                    {"permissions": {"allow": ["Bash"]}}, {"permissions": {"defaultMode": "bypassPermissions"}},
                    {"permissions": {"blockReadsOutsideWorkingDirectories": False}}, {"autoMemoryEnabled": True},
                    {"disableAllHooks": False}):
            self.assertTrue(P.check_settings(P.generate_settings(_inputs(), override=bad), "restricted"), bad)
        env = Env(self, mode="restricted", **{"proposer.restricted.settings": {"sandbox": {"enabled": False}}})
        self.assertEqual(env.stop().reason, "proposer_environment")
        self.assertEqual(env.cli.calls(), [])
        env2 = Env(self, mode="restricted", sessions=("writes_pending",),
                   **{"proposer.restricted.settings": {"sandbox": {"filesystem": {"allowRead": ["/extra"]}}}})
        self.assertTrue(env2.propose())
        rec = env2.session()
        self.assertTrue(rec["settings_override"])
        written = (env2.sdir() / "settings.json").read_bytes()
        self.assertEqual(rec["settings_sha256"], util.sha256_bytes(written))
        self.assertIn("/extra", json.loads(written)["sandbox"]["filesystem"]["allowRead"])
        self.assertEqual(os.stat(env2.sdir() / "settings.json").st_mode & 0o777, 0o600)

    # ---- LD6: a settings override can only add

    REVIEW_OVERRIDE = {"permissions": {"deny": None}, "sandbox": {"filesystem": None, "credentials": None}}

    def test_override_nulling_deny_lists_is_refused(self) -> None:
        # reviewer case: nulls replace every deny rule and still passed check_settings without the base
        base = P.generate_settings(_inputs())
        s = P.generate_settings(_inputs(), override=self.REVIEW_OVERRIDE)
        self.assertIsNone(s["permissions"]["deny"])
        self.assertEqual(P.check_settings(s, "restricted"), [])         # what the mode rests on alone does not see it
        problems = " | ".join(P.check_settings(s, "restricted", base=base))
        for needle in ("permissions.deny must stay a list", "sandbox.filesystem must stay an object",
                       "sandbox.credentials must stay an object", "permissions.deny must keep every generated entry",
                       "sandbox.filesystem.denyRead must keep", "sandbox.filesystem.denyWrite must keep",
                       "sandbox.credentials.files must keep"):
            self.assertIn(needle, problems)
        self.assertEqual(P.check_settings(base, "restricted", base=base), [])
        c_base = P.generate_settings(_inputs(mode="contained"))
        c = P.generate_settings(_inputs(mode="contained"), override=self.REVIEW_OVERRIDE)
        self.assertTrue(P.check_settings(c, "contained", base=c_base))

    def test_override_type_change_and_lost_entries_refused(self) -> None:
        base = P.generate_settings(_inputs())
        deny0 = base["permissions"]["deny"][0]
        cases = {
            "permissions.deny must stay a list": {"permissions": {"deny": {"x": 1}}},
            "sandbox.network must stay an object": {"sandbox": {"network": "off"}},
            "sandbox.filesystem.allowRead must stay a list": {"sandbox": {"filesystem": {"allowRead": "/"}}},
            "permissions must stay an object": {"permissions": []},
            "sandbox.excludedCommands must be empty": {"sandbox": {"excludedCommands": ["git"]}},
            "permissions.additionalDirectories must not change": {"permissions": {"additionalDirectories": ["/"]}},
        }
        for needle, override in cases.items():
            merged = P.generate_settings(_inputs(), override=override)
            self.assertIn(needle, " | ".join(P.check_settings(merged, "restricted", base=base)), override)
        removed = json.loads(json.dumps(base))                          # settings edited after the merge
        removed["permissions"]["deny"].remove(deny0)
        removed["sandbox"]["credentials"]["files"] = removed["sandbox"]["credentials"]["files"][1:]
        got = " | ".join(P.check_settings(removed, "restricted", base=base))
        self.assertIn("permissions.deny must keep every generated entry (1 missing)", got)
        self.assertIn("sandbox.credentials.files must keep every generated entry (1 missing)", got)
        adding = P.generate_settings(_inputs(), override={
            "permissions": {"deny": ["Read(//x/**)"]}, "sandbox": {"excludedCommands": [],
                                                                  "filesystem": {"denyRead": ["/x"]}}})
        self.assertEqual(P.check_settings(adding, "restricted", base=base), [])   # adding is allowed

    def test_runner_checks_override_against_generated_settings(self) -> None:
        env = Env(self, mode="restricted", **{"proposer.restricted.settings": self.REVIEW_OVERRIDE})
        stop = env.stop()
        self.assertEqual(stop.reason, "proposer_environment")
        self.assertIn("permissions.deny must stay a list", stop.detail)
        self.assertEqual(env.cli.calls(), [])
        env2 = Env(self, mode="contained", **{"proposer.restricted.settings": {"sandbox": {"filesystem": None}}})
        self.assertEqual(env2.stop().reason, "proposer_environment")
        self.assertEqual(env2.cli.calls(), [])

    def test_addendum_text_and_hash_recorded(self) -> None:
        env = Env(self, mode="restricted", sessions=("writes_pending",))
        self.assertTrue(env.propose())
        rec = env.session()
        scratch = Path(rec["scratch_dir"])
        text = P.render_addendum(scratch)
        self.assertIn(f"- Scratch directory: {scratch}\n", text)
        self.assertNotIn("{scratch_dir}", text)
        self.assertTrue(text.startswith("## Run environment (added by the harness; not part of the skill)\n"))
        self.assertEqual(rec["addendum_sha256"], util.sha256_bytes(text.encode("utf-8")))
        argv = env.cli.calls()[0]["argv"]
        self.assertTrue(argv[argv.index("--append-system-prompt") + 1].endswith("\n\n" + text))
        self.assertNotIn("--allowedTools", argv)
        self.assertEqual(argv[argv.index("--add-dir") + 1], str(scratch))
        self.assertEqual(tuple(argv[argv.index("--disallowedTools") + 1:][:5]), CREDENTIAL_READ_DENIES)
        self.assertEqual(os.stat(scratch).st_mode & 0o777, 0o700)
        up = Env(self, sessions=("writes_pending",))
        self.assertTrue(up.propose())
        self.assertIsNone(up.session()["addendum_sha256"])


# ================================================================ cost, transcripts, signals

class TestCostAndTranscripts(unittest.TestCase):
    def test_proven_pre_api_auth_failure_is_observed_zero(self) -> None:
        result = {'is_error': True, 'terminal_reason': 'api_error', 'duration_api_ms': 0,
            'total_cost_usd': 0, 'result': 'Failed to authenticate: OAuth session expired and could not be refreshed',
            'modelUsage': {}, 'usage': dict.fromkeys(('input_tokens', 'output_tokens',
                'cache_creation_input_tokens', 'cache_read_input_tokens'), 0)}
        self.assertEqual(P.session_cost(result, None, make_manifest(), 10_000_000), (0, 'no_api_auth_failure'))
        for key in ('duration_api_ms', 'usage', 'modelUsage', 'terminal_reason'):
            missing = dict(result); missing.pop(key)
            self.assertEqual(P.session_cost(missing, None, make_manifest(), 10), (10, 'estimate'))
        self.assertEqual(P.session_cost(result, _fixture_session(), make_manifest(), 10), (197859, 'cost_state'))

    def test_cost_fallback_chain_never_zero(self) -> None:
        m = make_manifest()
        self.assertEqual(P.session_cost({"total_cost_usd": 12.31}, None, m, 5), (12310000, "result_event"))
        full = _fixture_session()
        self.assertEqual(P.session_cost({"total_cost_usd": 0}, full, m, 5), (197859, "cost_state"))
        no_state = _fixture_session(drop_cost_state=True)
        self.assertEqual(P.session_cost(None, no_state, m, 5), (197859, "usage_sum"))
        unpriced = make_manifest({"proposer.list_prices": {"claude-opus-5-5": m["proposer"]["list_prices"]
                                                           ["claude-opus-5-5"]}})
        self.assertEqual(P.session_cost(None, no_state, unpriced, 10_000_000), (10_000_000, "estimate"))
        self.assertEqual(P.session_cost(None, None, m, 0), (1, "estimate"))     # never zero
        zero_state = tx.NormalizedSession(session_id=None, source="transcript", cli_version=None, prompt_text=None,
                                          messages=(), started_utc=None, ended_utc=None,
                                          cost_state={"total_cost_micro": 0, "model_usage": {}}, skipped_types={},
                                          warnings=())
        self.assertEqual(P.session_cost({"total_cost_usd": 0.0}, zero_state, m, 7), (7, "estimate"))

    def test_transcript_located_by_glob_and_set_aside_copies_copied(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mhf-tr-") as tmp:
            home = Path(tmp).resolve() / "home"
            folder = home / ".claude" / "projects" / ("-x" * 120)[:230]       # a truncated, hashed folder name
            (folder / SID / "subagents").mkdir(parents=True)
            (folder / f"{SID}.jsonl").write_text("{}\n")
            (folder / f"{SID}.orphaned-1").write_text("o\n")
            (folder / f"{SID}.jsonl.superseded-2").write_text("s\n")
            (folder / f"{SID}" / "subagents" / "agent-a.jsonl").write_text("{}\n")
            (folder / "other.jsonl").write_text("not ours\n")
            self.assertEqual(P.locate_transcript(home, SID), folder / f"{SID}.jsonl")
            self.assertIsNone(P.locate_transcript(home, "missing-session"))
            self.assertIsNone(P.locate_transcript(home, "../etc"))
            dest = Path(tmp).resolve() / "dest"
            got = P.copy_transcript(folder / f"{SID}.jsonl", dest, max_bytes=1 << 20)
            self.assertEqual((got["found"], got["files"]), (True, 4))
            for rel in (f"{SID}.jsonl", f"{SID}.orphaned-1", f"{SID}.jsonl.superseded-2",
                        f"{SID}/subagents/agent-a.jsonl"):
                self.assertTrue((dest / rel).is_file(), rel)
            self.assertFalse((dest / "other.jsonl").exists())
            small = P.copy_transcript(folder / f"{SID}.jsonl", Path(tmp) / "small", max_bytes=5)
            self.assertLessEqual(small["bytes"], 5)

    def test_transcript_and_session_directory_copied_no_symlinks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mhf-tr-") as tmp:
            root = Path(tmp).resolve()
            src_dir = root / "proj"
            (src_dir / SID / "tool-results").mkdir(parents=True)
            (src_dir / SID / "subagents").mkdir(parents=True)
            (src_dir / f"{SID}.jsonl").write_text("{}\n")
            (src_dir / SID / "tool-results" / "r1.txt").write_text("spilled\n")
            (src_dir / SID / "subagents" / "agent-b.meta.json").write_text("{}")
            secret = root / "secret.txt"
            secret.write_text("SECRET\n")
            os.symlink(secret, src_dir / SID / "tool-results" / "link.txt")
            os.symlink(root, src_dir / SID / "loop")
            got = P.copy_transcript(src_dir / f"{SID}.jsonl", root / "dest", max_bytes=1 << 20)
            self.assertEqual(got["files"], 3)
            self.assertTrue((root / "dest" / SID / "tool-results" / "r1.txt").is_file())
            self.assertFalse(os.path.lexists(root / "dest" / SID / "tool-results" / "link.txt"))
            self.assertFalse(os.path.lexists(root / "dest" / SID / "loop"))
            os.symlink(src_dir / f"{SID}.jsonl", root / "main-link.jsonl")
            self.assertFalse(P.copy_transcript(root / "main-link.jsonl", root / "d2", max_bytes=99)["found"])
        env = Env(self, sessions=("writes_pending",))
        self.assertTrue(env.propose())
        rec = env.session()
        sid = rec["session_id"]
        self.assertTrue(rec["transcript"]["found"])
        self.assertTrue((env.sdir() / "transcript" / f"{sid}.jsonl").is_file())
        for name in ("session.json", "stream.jsonl", "stderr.txt", "settings.json", "env.json", "command.json",
                     "pending_eval.json", "proposal.json", "candidates/x.py"):
            self.assertTrue((env.sdir() / name).is_file(), name)

    def test_solver_attempt_signals(self) -> None:
        def bash(tid, command):
            return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": "Bash",
                                                                   "input": {"command": command}}]}}
        rows = [bash("t1", "curl https://api.inference.wandb.ai/v1/models"), bash("t2", "ls agents"),
                bash("t3", "OPENROUTER_API_BASE=http://127.0.0.1:9 python x.py"),
                {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t4", "name": "Read",
                                                               "input": {"file_path": "litellm.py"}}]}}]
        got = P.solver_attempt_signals(rows)
        self.assertEqual(len(got), 2)
        self.assertTrue(got[0].startswith("Bash t1"))
        self.assertIn("openrouter", got[1])
        self.assertNotIn("curl", " ".join(got))                 # no command text
        self.assertEqual(P.solver_attempt_signals(_fixture_rows("stream_synthetic.jsonl")), [])

    def test_state_access_signals_set_evidence_exposed(self) -> None:
        state, ws, pkg = Path("/S/state/c"), Path("/S/state/c/workspace/text_classification"), Path("/P/mhforge")

        def tool(tid, name, inp):
            return {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": tid, "name": name,
                                                                   "input": inp}]}}
        rows = [tool("a", "Read", {"file_path": "../../jobs/val.x.USPTO/a1/out/sidecar.jsonl"}),
                tool("b", "Read", {"file_path": "agents/x.py"}),
                tool("c", "Bash", {"command": "cat /S/state/c/ledger.sqlite | head"}),
                tool("d", "Grep", {"pattern": "x", "path": "/P/mhforge/gateway.py"}),
                tool("e", "Glob", {"pattern": "/S/state/c/workspace/text_classification/agents/*.py"}),
                tool("f", "Bash", {"command": "ls ../../calls/"})]
        got = P.state_access_signals(rows, state_root=state, ws=ws, package_dir=pkg)
        self.assertEqual([g.split(" ")[1].rstrip(":") for g in got], ["a", "c", "d", "f"])
        self.assertIn("package", got[2])
        self.assertNotIn("sidecar", " ".join(got))
        env = Env(self, sessions=(("writes_pending", "reads_state"),))
        self.assertTrue(env.propose())
        rec = env.session()
        self.assertEqual(len(rec["state_access_signals"]), 2)
        self.assertTrue(rec["evidence_exposed"])
        clean = Env(self, sessions=("writes_pending",))
        clean.propose()
        self.assertFalse(clean.session()["evidence_exposed"])

    def test_session_id_from_init_event_without_result(self) -> None:
        env = Env(self, sessions=("no_result_exit0",), **{"proposer.transient_retries": 0})
        self.assertEqual(env.stop().reason, "proposer_transient")
        rec = env.session()
        self.assertEqual(rec["session_id"], "00000000-0000-4000-8000-000000000001")
        self.assertFalse(rec["result_event"]["present"])
        self.assertTrue(rec["transcript"]["found"])


# ================================================================ the proposal store

class TestStore(unittest.TestCase):
    def test_candidate_reference_latest_and_by_hash(self) -> None:
        with tmp_state() as st:
            paths = st.paths

            def store(iteration, k, body):
                sdir = paths.session_dir(iteration, k)
                (sdir / "candidates").mkdir(parents=True)
                (sdir / "candidates" / "x.py").write_text(body)
                digest = util.sha256_bytes(body.encode())
                util.write_json(sdir / "proposal.json", {"schema": "mhforge.proposal/1", "candidates": [
                    {"name": "x", "file": "candidates/x.py", "sha256": digest}]})
                return digest
            h1 = store(1, 1, "one")
            h2 = store(2, 1, "two")
            h3 = store(2, 3, "three")
            self.assertEqual(P.candidate_reference(paths, "x"), (paths.session_dir(2, 3) / "candidates" / "x.py", h3))
            self.assertEqual(P.candidate_reference(paths, "x", h1)[1], h1)
            self.assertEqual(P.candidate_reference(paths, "x", h2)[0], paths.session_dir(2, 1) / "candidates" / "x.py")
            self.assertIsNone(P.candidate_reference(paths, "x", "0" * 64))
            self.assertIsNone(P.candidate_reference(paths, "y"))
            self.assertIsNone(P.candidate_reference(paths, "../x"))
            (paths.session_dir(2, 3) / "candidates" / "x.py").write_text("tampered")
            self.assertEqual(P.candidate_reference(paths, "x")[1], h2)        # a damaged copy is never a reference


# ================================================================ the runner

class TestRunner(unittest.TestCase):
    def test_bad_hypothesis_is_no_candidates_not_a_stored_proposal(self) -> None:
        env = Env(self, sessions=("bad_hypothesis",))
        self.assertFalse(env.propose())
        rec = env.session()
        self.assertEqual(rec["outcome"], "no_candidates")
        self.assertTrue(rec["pending_eval"]["present"])
        self.assertFalse((env.sdir() / "proposal.json").exists())
        self.assertIsNone(P.find_usable_proposal(env.paths, 1))
        self.assertEqual(P.settled_failure(env.paths, 1)["outcome"], "no_candidates")

    def test_stray_workspace_file_quarantined(self) -> None:
        env = Env(self, sessions=(("writes_pending", "writes_stray_file"),))
        self.assertTrue(env.propose())
        rec = env.session()
        self.assertEqual(rec["drift"]["quarantined"], ["yaml.py"])
        self.assertFalse((env.ws / "yaml.py").exists())
        moved = list((env.paths.quarantine_root).glob("*/workspace/yaml.py"))
        self.assertEqual(len(moved), 1)
        self.assertEqual(rec["integrity"]["violations"], [])    # mode upstream: recorded, not a violation
        entries = [e for e in read_journal(env.paths.journal) if e["op"] == "workspace"]
        self.assertEqual(entries[-1]["detail"]["paths"], ["yaml.py"])
        restricted = Env(self, mode="contained", sessions=(("writes_pending", "writes_stray_file"),))
        self.assertEqual(restricted.stop().reason, "proposer_integrity")

    def test_evolution_summary_change_restored(self) -> None:
        env = Env(self, sessions=(("writes_pending", "edits_summary"),))
        env.summary.parent.mkdir(parents=True, exist_ok=True)
        env.summary.write_text('{"iteration": 0, "system": "no_memory"}\n')
        before = env.summary.read_bytes()
        self.assertEqual(env.stop().reason, "proposer_integrity")
        self.assertEqual(env.summary.read_bytes(), before)
        rec = env.session()
        self.assertEqual(rec["drift"]["logs_modified"], [f"logs/{env.manifest['campaign']}/evolution_summary.jsonl"])
        self.assertEqual((env.sdir() / "evolution_summary.before.jsonl").read_bytes(), before)
        created = Env(self, sessions=(("writes_pending", "edits_summary"),))
        created.stop()
        self.assertFalse(created.summary.exists())              # a summary the session created is removed

    def test_frontier_before_stored_and_reused_on_replay(self) -> None:
        env = Env(self, sessions=("writes_pending",))
        env.frontier.parent.mkdir(parents=True, exist_ok=True)
        env.frontier.write_text(json.dumps({"_pareto": [{"system": "fewshot_all", "val_accuracy": 41.3}]}))
        r1 = env.runner()
        self.assertTrue(env.propose(runner=r1))
        self.assertEqual(r1.frontier_before, {"system": "fewshot_all", "val_accuracy": 41.3})
        proposal = json.loads((env.sdir() / "proposal.json").read_text())
        self.assertEqual(proposal["frontier_before"], {"system": "fewshot_all", "val_accuracy": 41.3})
        env.frontier.write_text(json.dumps({"_pareto": [{"system": "x", "val_accuracy": 60.0}]}))
        r2 = env.runner()
        self.assertTrue(env.propose(runner=r2))
        self.assertEqual(r2.frontier_before, {"system": "fewshot_all", "val_accuracy": 41.3})
        self.assertEqual(len(env.cli.calls()), 1)

    def test_stored_proposal_replayed_without_a_session(self) -> None:
        env = Env(self, sessions=("writes_pending",))
        self.assertTrue(env.propose())
        stored = json.loads((env.sdir() / "proposal.json").read_text())
        good = (env.ws / "agents" / "x.py").read_bytes()
        env.pending.unlink()                                    # upstream unlinks it at L467-L468
        (env.ws / "agents" / "x.py").write_text("# changed after the session\n")
        r2 = env.runner()
        self.assertTrue(env.propose(runner=r2))
        self.assertEqual(len(env.cli.calls()), 1)               # no second paid session
        self.assertEqual(env.pending.read_bytes(), (env.sdir() / "pending_eval.json").read_bytes())
        self.assertEqual((env.ws / "agents" / "x.py").read_bytes(), good)
        self.assertEqual(util.sha256_bytes(good), stored["candidates"][0]["sha256"])
        last = [e for e in read_journal(env.paths.journal) if e["op"] == "proposer"][-1]
        self.assertEqual((last["status"], last["detail"]["from"]), ("replayed", "s1"))
        self.assertIn("agents/x.py", last["detail"]["restored"])
        self.assertEqual(r2.last_session_key, "iter001.s1")

    def test_settled_failure_returns_false_without_a_session(self) -> None:
        env = Env(self, sessions=("max_budget", "writes_pending"))
        self.assertFalse(env.propose())
        self.assertFalse(env.propose())
        self.assertEqual(len(env.cli.calls()), 1)
        self.assertFalse(env.sdir(1, 2).exists())

    def test_max_budget_result_is_a_settled_failure(self) -> None:
        env = Env(self, sessions=("max_budget",))
        self.assertFalse(env.propose())
        rec = env.session()
        self.assertEqual((rec["outcome"], rec["outcome_detail"]), ("failed", "max_budget_usd"))
        self.assertEqual(P.settled_failure(env.paths, 1)["session_key"], "iter001.s1")

    def test_timed_out_session_returns_false_even_with_pending_eval(self) -> None:
        env = Env(self, sessions=({"modes": ["writes_pending", "graceful"], "sleep_s": 30},),
                  clock=util.SystemClock())
        self.assertFalse(env.propose(timeout=1))
        rec = env.session()
        self.assertEqual(rec["outcome"], "timeout")
        self.assertTrue(rec["timed_out"])
        self.assertEqual(rec["exit_code"], 0)                   # the CLI exited 0 after SIGINT: still not success
        self.assertTrue(rec["result_event"]["present"])
        self.assertTrue(rec["pending_eval"]["present"])
        self.assertFalse((env.sdir() / "proposal.json").exists())
        self.assertEqual(rec["signals"][0][0], "SIGINT")

    def test_pending_eval_copied_with_base_system(self) -> None:
        env = Env(self, sessions=("writes_pending",))
        env.configure("writes_pending", base_system="fewshot_memory", candidates=["x", "y"])
        self.assertTrue(env.propose())
        copy_ = json.loads((env.sdir() / "pending_eval.json").read_text())
        self.assertEqual([c["base_system"] for c in copy_["candidates"]], ["fewshot_memory", "fewshot_memory"])
        rec = env.session()
        self.assertEqual(rec["pending_eval"]["sha256"], util.sha256_file(env.sdir() / "pending_eval.json"))
        self.assertEqual([c["base_system"] for c in rec["pending_eval"]["candidates"]], ["fewshot_memory"] * 2)
        self.assertEqual(rec["pending_eval"]["candidates"][0]["file_sha256"],
                         util.sha256_file(env.ws / "agents" / "x.py"))
        proposal = json.loads((env.sdir() / "proposal.json").read_text())
        self.assertEqual([c["base_system"] for c in proposal["candidates"]], ["fewshot_memory"] * 2)
        self.assertEqual(env.units, ["proposer:iter001.s1", "propose:x", "propose:y"])
        done = [e for e in read_journal(env.paths.journal) if e["op"] == "proposer" and e["status"] == "complete"]
        self.assertEqual([c["name"] for c in done[0]["detail"]["candidates"]], ["x", "y"])

    def test_list_price_metered_once(self) -> None:
        env = Env(self, sessions=("writes_pending",))
        self.assertTrue(env.propose())
        self.assertTrue(env.propose())                          # replayed: no new row
        led = env.ledger()
        self.assertEqual(led.open_meter_rows("proposer_list_price"), [])
        self.assertEqual(led.meter_total("proposer_list_price"), (1_250_000, 1_250_000))
        self.assertEqual(env.session()["list_price_micro"], 1_250_000)
        self.assertEqual(env.session()["list_price_basis"], "result_event")

    def test_budget_check_uses_session_cap_before_the_session(self) -> None:
        env = Env(self, sessions=("writes_pending",), **{"budgets.proposer_list_usd": "30.00"})
        env.ledger().meter("proposer_list_price", "iter000.s1", 5_000_000, "micro_usd")
        self.assertTrue(env.propose())                          # 5 + 25 (the session cap) <= 30
        other = Env(self, sessions=("writes_pending",), **{"budgets.proposer_list_usd": "30.00"})
        other.ledger().meter("proposer_list_price", "iter000.s1", 5_000_001, "micro_usd")
        stop = other.stop()
        self.assertEqual(stop.reason, "budget:proposer")
        self.assertEqual(other.cli.calls(), [])

    def test_proposer_budget_cap_halts(self) -> None:
        env = Env(self, sessions=("writes_pending",), **{"budgets.proposer_list_usd": "26.00"})
        self.assertTrue(env.propose(1))                         # 0 + 25 <= 26; the session costs 1.25
        self.assertEqual(env.stop(2).reason, "budget:proposer")  # 1.25 + 25 > 26
        halt = env.ledger().halted()
        self.assertEqual(halt.reason, "budget:proposer")
        self.assertEqual(len(env.cli.calls()), 1)

    def test_binary_hash_mismatch_stops_campaign(self) -> None:
        env = Env(self, **{"proposer.cli_sha256": "0" * 64})
        self.assertEqual(env.stop().reason, "proposer_cli")
        self.assertEqual(env.cli.calls(), [])
        gone = Env(self, **{"proposer.cli_path": "/nonexistent/claude"})
        self.assertEqual(gone.stop().reason, "proposer_cli")

    def test_spend_limit_and_auth_stop_campaign_and_are_not_settled(self) -> None:
        env = Env(self, sessions=("spend_limit", "not_logged_in", "writes_pending"))
        self.assertEqual(env.stop().reason, "proposer_spend_limit")
        self.assertIsNone(P.settled_failure(env.paths, 1))
        self.assertEqual(env.stop().reason, "proposer_auth")
        self.assertEqual(env.session(1, 2)["outcome"], "auth")
        self.assertIsNone(P.settled_failure(env.paths, 1))
        self.assertTrue(env.propose())                          # after the stops, the same iteration runs again
        self.assertEqual(len(env.cli.calls()), 3)
        self.assertEqual(env.session(1, 3)["outcome"], "ok")

    def test_cli_version_mismatch_stops(self) -> None:
        env = Env(self, sessions=({"modes": ["writes_pending"], "version": "2.1.999"},))
        self.assertEqual(env.stop().reason, "proposer_cli")
        rec = env.session()
        self.assertTrue(any("claude_code_version" in v for v in rec["integrity"]["violations"]))
        init_only = Env(self, sessions=({"modes": ["writes_pending"], "init": {"claude_code_version": "2.1.999"}},))
        self.assertEqual(init_only.stop().reason, "proposer_integrity")

    def test_pinned_file_edit_restored_and_recorded(self) -> None:
        env = Env(self, sessions=(("writes_pending", "edits_upstream_file"),))
        self.assertEqual(env.stop().reason, "proposer_integrity")
        rec = env.session()
        self.assertEqual(rec["drift"]["upstream_modified"], ["benchmark.py"])
        self.assertEqual(rec["drift"]["restored"], ["benchmark.py"])
        self.assertEqual((env.ws / "benchmark.py").read_bytes(), (env.example / "benchmark.py").read_bytes())
        self.assertEqual(workspace.verify_pinned(env.paths), {"modified": [], "removed": []})

    def test_env_fingerprint_change_stops(self) -> None:
        env = Env(self)
        new = env.venv / "lib" / "python3.13" / "site-packages" / "intruder-9.9.dist-info" / "METADATA"
        env.configure({"modes": ["writes_pending"], "writes": [{"path": str(new), "text": "x"}]})
        self.assertEqual(env.stop().reason, "proposer_environment")
        rec = env.session()
        self.assertNotEqual(rec["env_fingerprint_before"], rec["env_fingerprint_after"])

    def test_session_record_has_no_prompt_or_skill_text(self) -> None:
        env = Env(self, sessions=("writes_pending",))
        self.assertTrue(env.propose())
        self.assertIn(PROMPT, env.cli.calls()[0]["argv"])        # the CLI did get them
        self.assertTrue(any(SKILL in a for a in env.cli.calls()[0]["argv"]))
        for name in ("session.json", "command.json", "proposal.json", "env.json", "settings.json", "stream.jsonl"):
            text = (env.sdir() / name).read_text()
            self.assertNotIn("UNIQUE-PROMPT-TEXT", text, name)
            self.assertNotIn("UNIQUE-SKILL-TEXT", text, name)
        for entry in read_journal(env.paths.journal):
            self.assertNotIn("UNIQUE-", json.dumps(entry))
        cmd = json.loads((env.sdir() / "command.json").read_text())
        self.assertEqual(cmd["redacted"]["-p"]["chars"], len(PROMPT))

    def test_exit_zero_without_result_event_is_not_success(self) -> None:
        env = Env(self, sessions=(("writes_pending", "no_result_exit0"),), **{"proposer.transient_retries": 0})
        self.assertEqual(env.stop().reason, "proposer_transient")
        rec = env.session()
        self.assertEqual(rec["exit_code"], 0)
        self.assertEqual((rec["outcome"], rec["outcome_detail"]), ("transient", "no_result_event"))
        self.assertFalse((env.sdir() / "proposal.json").exists())
        self.assertEqual(rec["list_price_basis"], "usage_sum")     # no result event, no cost-state record
        self.assertGreater(rec["list_price_micro"], 0)
        bare = Env(self, sessions=({"modes": ["no_result_exit0"], "write_transcript": False},),
                   **{"proposer.transient_retries": 0})
        bare.stop()
        self.assertEqual(bare.session()["list_price_basis"], "estimate")
        self.assertEqual(bare.session()["list_price_micro"], 10_000_000)   # max(meter max, session_estimate_usd)

    def test_limit_window_waits_then_same_iteration(self) -> None:
        clock = FakeClock()
        env = Env(self, clock=clock)
        reset = clock.now() + 600
        env.configure({"modes": ["limit_window"], "resets_at": reset}, "writes_pending")
        self.assertTrue(env.propose())
        self.assertEqual(env.session(1, 1)["outcome"], "limit_window")
        self.assertEqual(env.session(1, 2)["outcome"], "ok")
        self.assertGreaterEqual(clock.now(), reset + 60)
        self.assertTrue(all(s <= 30.0 for s in clock.sleeps))
        self.assertGreaterEqual(env.halt_checks, 21)
        done = [e for e in read_journal(env.paths.journal) if e["op"] == "proposer" and e["status"] == "complete"]
        self.assertEqual(done[0]["detail"]["wait_until_utc"], util.utc_iso(reset))
        late = Env(self, clock=FakeClock(), **{"proposer.max_limit_wait_s": 300})
        late.configure({"modes": ["limit_window"], "resets_at": late.clock.now() + 600})
        self.assertEqual(late.stop().reason, "proposer_limit_wait")
        third = Env(self, clock=FakeClock())
        third.configure({"modes": ["limit_window"], "resets_at": third.clock.now() + 10})
        self.assertEqual(third.stop().reason, "proposer_limit_wait")
        self.assertEqual(len(third.cli.calls()), 3)             # two waits, the third limit stops

    def test_transient_reruns_bounded_then_stops(self) -> None:
        env = Env(self, sessions=("transient",), **{"proposer.transient_retries": 2})
        self.assertEqual(env.stop().reason, "proposer_transient")
        self.assertEqual(len(env.cli.calls()), 3)
        self.assertEqual([env.session(1, k)["outcome"] for k in (1, 2, 3)], ["transient"] * 3)
        self.assertEqual(len(env.clock.sleeps) >= 2, True)
        waited = env.clock.now() - FakeClock().now()
        self.assertTrue(120 <= waited <= 600, waited)            # two waits in [60, 300]
        ok = Env(self, sessions=("transient", "writes_pending"))
        self.assertTrue(ok.propose())
        self.assertEqual(ok.session(1, 2)["outcome"], "ok")

    def test_raw_events_of_error_results_are_kept(self) -> None:
        env = Env(self, sessions=("limit_text",))
        self.assertEqual(env.stop().reason, "proposer_spend_limit")
        rows = util.read_jsonl(env.sdir() / "stream.jsonl")
        results = [json.loads(r["line"]) for r in rows if json.loads(r["line"]).get("type") == "result"]
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["is_error"])
        self.assertIn("usage limit", results[0]["result"])
        self.assertEqual(env.session()["outcome_detail"], "text")

    def test_scratch_dir_must_be_resolved_and_outside_state(self) -> None:
        env = Env(self)
        real = env.top / "real-scratch"
        real.mkdir()
        os.symlink(real, env.top / "link-scratch")
        env.manifest["proposer"]["scratch_root"] = str(env.top / "link-scratch")
        self.assertEqual(env.stop().reason, "proposer_environment")
        env.manifest["proposer"]["scratch_root"] = str(env.paths.root / "scratch-inside")
        self.assertEqual(env.stop().reason, "proposer_environment")
        self.assertEqual(env.cli.calls(), [])
        env.manifest["proposer"]["scratch_root"] = str(real)
        env.configure("writes_pending")
        self.assertTrue(env.propose())
        # a stop before "started" uses up no session ordinal
        self.assertEqual(env.session(1, 1)["scratch_dir"], str(real / env.manifest["campaign"] / "iter001.s1"))

    def test_new_report_is_recorded_changed_report_is_a_violation(self) -> None:
        env = Env(self, mode="restricted")
        reports = env.logs / "reports"
        env.configure({"modes": ["writes_pending"], "writes": [{"path": str(reports / "iter001.md"), "text": "r"}]})
        self.assertTrue(env.propose(1))
        rec = env.session(1, 1)
        self.assertEqual(rec["integrity"]["reports_added"], ["iter001.md"])
        self.assertEqual(rec["integrity"]["violations"], [])
        settings = json.loads((env.sdir(1, 1) / "settings.json").read_text())
        self.assertEqual(len(settings["permissions"]["allow"]), 4)
        env.configure({"modes": ["writes_pending"], "writes": [{"path": str(reports / "iter001.md"), "text": "x"}]})
        self.assertEqual(env.stop(2).reason, "proposer_integrity")
        rec2 = env.session(2, 1)
        self.assertTrue(any("reports_modified" in v for v in rec2["integrity"]["violations"]))
        settings2 = json.loads((env.sdir(2, 1) / "settings.json").read_text())
        self.assertTrue(any(r.endswith("/reports/iter001.md)") for r in settings2["permissions"]["deny"]))
        up = Env(self, mode="upstream")
        up.configure({"modes": ["writes_pending"], "writes": [{"path": str(up.logs / "reports" / "a.md"), "text": "1"}]})
        self.assertTrue(up.propose())
        self.assertEqual(up.session()["integrity"]["reports_added"], ["a.md"])

    def test_session_record_shape(self) -> None:
        env = Env(self, sessions=("writes_pending",))
        self.assertTrue(env.propose())
        rec = env.session()
        for key in ("schema", "campaign", "iteration", "ordinal", "session_key", "permission_mode", "cli", "argv_sha256",
                    "argv_flags", "settings_sha256", "settings_override", "addendum_sha256", "scratch_dir", "env_names",
                    "started_utc", "ended_utc", "duration_s", "timeout_s", "paused_s", "exit_code", "timed_out",
                    "signals", "outcome", "outcome_detail", "session_id", "requested_model", "resolved_model", "effort",
                    "model_check", "init", "result_event", "limit", "sandbox_check", "integrity", "list_price_micro",
                    "list_price_basis", "pending_eval", "transcript", "drift", "env_fingerprint_before",
                    "env_fingerprint_after", "auto_memory_dir_changed", "solver_attempt_signals",
                    "state_access_signals", "wrapper_log_dir"):
            self.assertIn(key, rec)
        self.assertEqual(rec["schema"], "mhforge.session/1")
        self.assertEqual(rec["resolved_model"], "claude-opus-5-5")
        self.assertEqual(rec["model_check"]["request_efforts"], ["max", "max"])
        self.assertEqual(rec["result_event"]["total_cost_usd"], "1.25")
        self.assertEqual(rec["drift"]["agents_added"], ["agents/x.py"])
        self.assertTrue(rec["wrapper_log_dir"].startswith(f"logs/{env.manifest['campaign']}/claude_sessions/"))
        self.assertIn("HOME", rec["env_names"])
        self.assertIn("CLAUDE_CODE_DISABLE_AUTO_MEMORY", rec["env_names"])
        names = set(env.cli.calls()[0]["env_names"]) - {"__CF_USER_TEXT_ENCODING"}
        self.assertEqual(names, set(rec["env_names"]))


class TestReviewRegressions(unittest.TestCase):
    def test_interrupted_session_kills_the_group_after_cli_exit(self) -> None:
        import time
        env = Env(self)
        pid_file = env.top / "child.pid"
        env.configure(("writes_pending", "spawns_child"), child_pid_file=str(pid_file))
        original = env.wrapper.run

        def run(*a, **kw):                                  # the CLI has ended; the runner is interrupted before reaping
            original(*a, **kw)
            raise KeyboardInterrupt
        env.wrapper.run = run
        with self.assertRaises(KeyboardInterrupt):
            env.propose()
        pid = int(pid_file.read_text())
        end = time.monotonic() + 5
        while time.monotonic() < end:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            os.kill(pid, 9)
            self.fail("a background child of the interrupted session survived")
        self.assertEqual(env.ledger().open_meter_rows("proposer_list_price"), [])

    def test_limit_signal_out_of_range_reset_is_no_reset(self) -> None:
        lim = P.limit_signal({"rate_limits": [{"type": "rate_limit_event", "rate_limit_info": {
            "status": "rejected", "resetsAt": 1e20, "rateLimitType": "five_hour"}}]})
        self.assertEqual((lim["signal"], lim["resets_at_utc"]), ("rate_limit_event", None))
        self.assertEqual(P.classify_session(_facts(result=_err(), limit=lim), make_manifest()),
                         ("spend_limit", "unrecognised_limit"))    # no wait without a usable reset time

    def test_signals_tolerate_non_mapping_tool_input(self) -> None:
        rows = [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t", "name": "Bash",
                                                               "input": "openrouter"}]}}]
        self.assertEqual(P.solver_attempt_signals(rows), [])
        self.assertEqual(P.state_access_signals(rows, state_root=Path("/S"), ws=Path("/S/w"), package_dir=Path("/P")),
                         [])

    def test_no_candidates_shows_the_successful_result(self) -> None:
        env = Env(self, sessions=("bad_hypothesis",))
        results = []
        original = env.wrapper.run

        def run(*a, **kw):
            results.append(original(*a, **kw))
            return results[-1]
        env.wrapper.run = run
        self.assertFalse(env.propose())
        self.assertEqual(env.session()["outcome"], "no_candidates")
        self.assertEqual(results[0].shown, 1)


@needs_example_dir
class TestRealWrapper(unittest.TestCase):
    def test_real_wrapper_pins_path_and_env(self) -> None:
        ex = example_dir()
        env = Env(self, example=ex, sessions=("writes_pending",), wrapper=types.ModuleType("placeholder"))
        workspace.ensure_workspace(env.manifest, env.paths, ex, None)
        source = (env.ws / "claude_wrapper.py").read_bytes()
        real = types.ModuleType("claude_wrapper")
        real.__file__ = str(env.ws / "claude_wrapper.py")
        exec(compile(source, real.__file__, "exec", dont_inherit=True), real.__dict__)
        env.upstream.claude_wrapper = real
        real_subprocess = real.subprocess
        self.assertTrue(env.propose())
        self.assertIs(real.subprocess, real_subprocess)          # the shim was removed again
        call = env.cli.calls()[0]
        self.assertEqual(call["argv"][0], str(env.cli.path))
        names = set(call["env_names"]) - {"__CF_USER_TEXT_ENCODING"}
        self.assertEqual(names, set(env.session()["env_names"]))
        self.assertNotIn("MHFORGE_EXAMPLE_DIR", names)
        meta = json.loads(next((env.logs / "claude_sessions").glob("*_iter1/meta.json")).read_text())
        self.assertEqual(meta["command"][0], str(env.cli.path))
        self.assertIn("--settings", meta["command"])
        self.assertEqual(meta["command"][meta["command"].index("--plugin-dir") + 1], str(env.ws / ".empty_plugins"))
        self.assertEqual(env.session()["outcome"], "ok")


if __name__ == "__main__":
    unittest.main()
