# SPDX-License-Identifier: MIT
"""Tests for mhforge/proposer_shim.py (spec 3.19, 5.2) with the fake CLI and the fake wrapper; the real claude CLI is
never run."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

from mhforge import util
from mhforge.errors import UpstreamContractError
from mhforge.proposer_shim import (CREDENTIAL_READ_DENIES, ArgvSpec, SubprocessShim, Tee, command_fingerprint,
                                   installed, parse_upstream_argv, retry_pause_s, session_argv)
from tests import fake_claude_wrapper
from tests.fake_claude_cli import write_fake_cli
from tests.helpers import FakeClock, example_dir

TOOLS = ["Read", "Glob", "Grep", "Agent", "Write", "Edit", "Bash"]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_gone(pid: int, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


class ShimCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mhf-shim-")
        self.root = Path(self._tmp.name).resolve()
        self.work = self.root / "ws"
        self.work.mkdir()
        self.sdir = self.root / "session"
        self.sdir.mkdir()
        self.cli = write_fake_cli(self.root / "bin")
        self.env = {"PATH": "/usr/bin:/bin", "HOME": str(self.root / "home"), "LANG": "C.UTF-8", "MHF_MARK": "1"}
        self.wrapper = fake_claude_wrapper.new_module(self.work / ".empty_plugins")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def spec(self, mode: str = "upstream") -> ArgvSpec:
        scratch = self.root / "scratch"
        return ArgvSpec(mode=mode, cli_path=str(self.cli.path), model="opus", effort="max", max_budget_usd="25.00",
                        settings_file=self.sdir / "settings.json", scratch_dir=None if mode == "upstream" else scratch,
                        addendum="ADDENDUM" if mode == "restricted" else None)

    def shim(self, *, timeout_s: float = 60.0, grace_s=(0.3, 0.3), clock=None, cli_path=None) -> SubprocessShim:
        return SubprocessShim(env=self.env, cli_path=cli_path or str(self.cli.path),
                              stream_path=self.sdir / "stream.jsonl", stderr_path=self.sdir / "stderr.txt",
                              timeout_s=timeout_s, grace_s=grace_s, clock=clock)

    def run_wrapper(self, shim: SubprocessShim, *, wrapper_timeout=None, mode="upstream", prompt="PROMPT-TEXT"):
        original = self.wrapper.build_command
        spec = self.spec(mode)

        def build(*a, **kw):
            return session_argv(original(*a, **kw), spec)

        with contextlib.redirect_stdout(io.StringIO()), installed(self.wrapper, shim, build_command=build):
            return self.wrapper.run(prompt=prompt, model="opus", allowed_tools=TOOLS, cwd=str(self.work),
                                    log_dir=str(self.root / "logs"), name="iter1", timeout_seconds=wrapper_timeout,
                                    effort="max", system_prompt="SKILL-TEXT")

    def stream_rows(self) -> list[dict]:
        return util.read_jsonl(self.sdir / "stream.jsonl")


class TestShimProcess(ShimCase):
    def test_cmd0_replaced_with_pinned_path(self) -> None:
        shim = self.shim()
        result = self.run_wrapper(shim)
        self.assertEqual(result.exit_code, 0)
        call = self.cli.calls()[0]
        self.assertEqual(call["argv"][0], str(self.cli.path))
        self.assertEqual(self.wrapper.LAST_COMMAND[0], str(self.cli.path))
        meta = json.loads(next((self.root / "logs").glob("*/meta.json")).read_text())
        self.assertEqual(meta["command"][0], str(self.cli.path))          # the wrapper's own log has the mode's argv
        self.assertIn("--settings", meta["command"])
        self.assertEqual(self.wrapper.subprocess.__name__, "subprocess")      # restored after the call

    def test_env_is_exactly_the_given_mapping(self) -> None:
        self.run_wrapper(self.shim())
        names = set(self.cli.calls()[0]["env_names"]) - {"__CF_USER_TEXT_ENCODING"}
        self.assertEqual(names, set(self.env))
        self.assertNotIn("FAKE_WRAPPER_PARENT_ENV", names)                 # the wrapper's env= is ignored

    def test_tee_stamps_every_line_utc(self) -> None:
        clock = FakeClock()
        self.run_wrapper(self.shim(clock=clock))
        rows = self.stream_rows()
        self.assertGreaterEqual(len(rows), 4)
        for row in rows:
            self.assertEqual(set(row), {"t_utc", "stream", "line"})
            self.assertEqual(row["stream"], "stdout")
            self.assertEqual(row["t_utc"], clock.utc())
            self.assertFalse(row["line"].endswith("\n"))
            json.loads(row["line"])
        self.assertEqual(json.loads(rows[0]["line"])["subtype"], "init")
        self.assertEqual(json.loads(rows[-1]["line"])["type"], "result")

    def test_tee_unit_stdout_rows_and_stderr_raw(self) -> None:
        clock = FakeClock()
        seen = []
        tee = Tee(io.StringIO("a\nb\n"), self.sdir / "s.jsonl", "stdout", clock, on_line=seen.append)
        self.assertEqual([tee.readline(), tee.readline(), tee.readline()], ["a\n", "b\n", ""])
        self.assertTrue(tee.eof.is_set())
        self.assertEqual([r["line"] for r in util.read_jsonl(self.sdir / "s.jsonl")], ["a", "b"])
        self.assertEqual(seen, ["a\n", "b\n"])
        err = Tee(io.StringIO("x y\n"), self.sdir / "e.txt", "stderr", clock)
        err.readline()
        err.readline()
        self.assertEqual((self.sdir / "e.txt").read_text(), "x y\n")

    def test_sigint_then_term_then_kill(self) -> None:
        self.cli.configure("ignore_all", sleep_s=30)
        shim = self.shim(timeout_s=0.3, grace_s=(0.3, 0.3))
        result = self.run_wrapper(shim)
        proc = shim.last
        self.assertTrue(proc.timed_out)
        self.assertEqual([s[0] for s in proc.signals], ["SIGINT", "SIGTERM", "SIGKILL"])
        self.assertEqual(result.exit_code, -9)
        for _, utc in proc.signals:
            util.parse_utc(utc)

    def test_graceful_timeout_keeps_result_event(self) -> None:
        self.cli.configure("graceful", sleep_s=30)
        shim = self.shim(timeout_s=0.5, grace_s=(5.0, 5.0))
        result = self.run_wrapper(shim)
        shim.last.stdout.eof.wait(5)
        self.assertEqual([s[0] for s in shim.last.signals], ["SIGINT"])
        self.assertEqual(result.exit_code, 0)
        events = [json.loads(r["line"]) for r in self.stream_rows()]
        self.assertEqual(events[-1]["type"], "result")
        self.assertEqual(events[-1]["result"], "SYNTHETIC: interrupted.")

    def test_process_group_is_gone_after_timeout(self) -> None:
        pid_file = self.root / "grandchild.pid"
        self.cli.configure(("grandchild", "ignore_all"), child_pid_file=str(pid_file), sleep_s=30)
        shim = self.shim(timeout_s=0.3, grace_s=(0.2, 0.2))
        self.run_wrapper(shim)
        pid = int(pid_file.read_text())
        self.assertTrue(_wait_gone(pid), "the grandchild survived the deadline escalation")
        self.assertIn("SIGKILL", [s[0] for s in shim.last.signals])

    def test_wrapper_kill_is_group_sigkill(self) -> None:
        pid_file = self.root / "grandchild.pid"
        self.cli.configure("grandchild", child_pid_file=str(pid_file), sleep_s=30)
        shim = self.shim(timeout_s=600.0)
        result = self.run_wrapper(shim, wrapper_timeout=1.0)
        self.assertEqual(result.exit_code, 124)
        self.assertFalse(shim.last.timed_out)
        self.assertEqual([s[0] for s in shim.last.signals], ["SIGKILL"])
        self.assertTrue(_wait_gone(int(pid_file.read_text())), "proc.kill() left the grandchild behind")

    def test_group_reaped_after_normal_exit(self) -> None:
        pid_file = self.root / "child.pid"
        self.cli.configure("spawns_child", child_pid_file=str(pid_file))
        shim = self.shim(grace_s=(0.5, 0.5))
        result = self.run_wrapper(shim)
        self.assertEqual(result.exit_code, 0)
        pid = int(pid_file.read_text())
        self.assertTrue(_alive(pid))
        sent = shim.last.reap_group()
        self.assertEqual(sent, ["reap_term"])
        self.assertTrue(_wait_gone(pid))
        self.assertIn("reap_term", [s[0] for s in shim.last.signals])
        self.assertTrue(shim.last.stdout.eof.wait(5))
        self.assertEqual(shim.last.reap_group(), [])                        # nothing left the second time

    def test_popen_refuses_unpinned_argv0(self) -> None:
        shim = self.shim()
        with self.assertRaises(UpstreamContractError):
            shim.Popen(["claude", "-p", "x"], stdout=shim.PIPE, stderr=shim.PIPE)
        with self.assertRaises(UpstreamContractError):
            shim.Popen([], stdout=shim.PIPE)
        self.assertEqual(self.cli.calls(), [])
        with contextlib.redirect_stdout(io.StringIO()), installed(self.wrapper, shim,
                                                                  build_command=self.wrapper.build_command):
            with self.assertRaises(UpstreamContractError):            # bypassing the build_command wrapper is refused
                self.wrapper.run(prompt="p", model="opus", allowed_tools=TOOLS, cwd=str(self.work), effort="max")

    def test_deadline_is_monotonic_and_pauses_for_rate_limit_waits(self) -> None:
        clock = FakeClock()
        self.cli.configure("api_retry_wait", retries=3, retry_delay_ms=600000, sleep_s=30)
        shim = self.shim(timeout_s=100.0, grace_s=(0.2, 0.2), clock=clock)
        box: dict = {}
        runner = threading.Thread(target=lambda: box.setdefault("r", self.run_wrapper(shim)))
        runner.start()
        end = time.monotonic() + 10
        while time.monotonic() < end and (shim.last is None or shim.last.paused_s < 100.0):
            time.sleep(0.02)
        proc = shim.last
        self.assertEqual(proc.paused_s, 100.0)                  # 3 x 600 s reported, capped at one session timeout
        clock.advance(150.0)                                    # past the plain deadline, inside the paused one
        time.sleep(0.4)
        self.assertFalse(proc.timed_out)
        self.assertIsNone(proc.poll())
        clock.advance(60.0)                                     # past start + 100 + 100
        runner.join(15)
        self.assertFalse(runner.is_alive())
        self.assertTrue(proc.timed_out)
        self.assertEqual(proc.signals[0][0], "SIGINT")

    def test_wall_clock_jump_does_not_time_out(self) -> None:
        clock = FakeClock()
        self.cli.configure("api_retry_wait", retries=0, sleep_s=1.0)
        shim = self.shim(timeout_s=5.0, clock=clock)
        box: dict = {}
        runner = threading.Thread(target=lambda: box.setdefault("r", self.run_wrapper(shim)))
        runner.start()
        time.sleep(0.3)
        clock.advance_wall(100000.0)                            # the host slept: wall clock jumped, monotonic did not
        runner.join(15)
        self.assertFalse(shim.last.timed_out)
        self.assertEqual(box["r"].exit_code, 0)
        self.assertEqual(shim.last.signals, [])

    def test_file_not_found_propagates(self) -> None:
        missing = str(self.root / "no-such-claude")
        shim = self.shim(cli_path=missing)
        with self.assertRaises(FileNotFoundError):
            shim.Popen([missing, "-p", "x"])
        self.assertIsNone(shim.last)
        spec = ArgvSpec(mode="upstream", cli_path=missing, model="opus", effort="max", max_budget_usd="1",
                        settings_file=self.sdir / "settings.json")
        original = self.wrapper.build_command
        with contextlib.redirect_stdout(io.StringIO()), installed(
                self.wrapper, shim, build_command=lambda *a, **kw: session_argv(original(*a, **kw), spec)):
            result = self.wrapper.run(prompt="p", model="opus", allowed_tools=TOOLS, cwd=str(self.work), effort="max",
                                      system_prompt="S")
        self.assertEqual(result.exit_code, 127)                 # the wrapper maps it, as upstream does


class TestArgv(ShimCase):
    def upstream_cmd(self, prompt: str = "PROMPT-TEXT", system: str = "SKILL-TEXT") -> list[str]:
        return self.wrapper.build_command(prompt, "opus", TOOLS, system, effort="max")

    def test_session_argv_exact_per_mode(self) -> None:
        cmd = self.upstream_cmd()
        cli, plug, settings = str(self.cli.path), str(self.work / ".empty_plugins"), str(self.sdir / "settings.json")
        scratch = str(self.root / "scratch")
        tools = ",".join(TOOLS)
        upstream = [cli, "--dangerously-skip-permissions", "-p", "PROMPT-TEXT", "--output-format", "stream-json",
                    "--verbose", "--model", "opus", "--setting-sources", "", "--tools", tools, "--allowedTools", *TOOLS,
                    "--disable-slash-commands", "--strict-mcp-config", "--plugin-dir", plug,
                    "--append-system-prompt", "SKILL-TEXT", "--effort", "max",
                    "--settings", settings, "--max-budget-usd", "25.00"]
        self.assertEqual(session_argv(cmd, self.spec("upstream")), upstream)
        restricted = [cli, "-p", "PROMPT-TEXT", "--output-format", "stream-json", "--verbose",
                      "--model", "opus", "--effort", "max", "--setting-sources", "", "--tools", tools,
                      "--disable-slash-commands", "--strict-mcp-config", "--plugin-dir", plug,
                      "--permission-mode", "dontAsk", "--permission-prompts", "none", "--settings", settings,
                      "--add-dir", scratch, "--disallowedTools", "Read(~/.netrc)", "Read(~/.ssh/**)",
                      "Read(~/.config/gh/**)", "Read(~/.config/wandb/**)", "Read(~/Library/Keychains/**)",
                      "--append-system-prompt", "SKILL-TEXT\n\nADDENDUM", "--max-budget-usd", "25.00"]
        self.assertEqual(session_argv(cmd, self.spec("restricted")), restricted)
        contained = upstream[:-2] + ["--add-dir", scratch, "--max-budget-usd", "25.00"]
        self.assertEqual(session_argv(cmd, self.spec("contained")), contained)
        self.assertEqual(contained[contained.index("--settings") + 2], "--add-dir")

    def test_restricted_argv_has_no_allowed_tools_and_repeats_credential_denies(self) -> None:
        argv = session_argv(self.upstream_cmd(), self.spec("restricted"))
        self.assertNotIn("--allowedTools", argv)
        self.assertNotIn("--dangerously-skip-permissions", argv)
        i = argv.index("--disallowedTools")
        self.assertEqual(tuple(argv[i + 1:i + 6]), CREDENTIAL_READ_DENIES)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        with self.assertRaises(ValueError):
            session_argv(self.upstream_cmd(), ArgvSpec(mode="restricted", cli_path="c", model="opus", effort="max",
                                                       max_budget_usd="1", settings_file=Path("s")))

    def test_settings_and_budget_flags_in_every_mode(self) -> None:
        for mode in ("upstream", "restricted", "contained"):
            argv = session_argv(self.upstream_cmd(), self.spec(mode))
            self.assertEqual(argv[0], str(self.cli.path))
            self.assertEqual(argv.count("--settings"), 1, mode)
            self.assertEqual(argv[argv.index("--settings") + 1], str(self.sdir / "settings.json"), mode)
            self.assertEqual(argv[-2:], ["--max-budget-usd", "25.00"], mode)
            self.assertEqual(argv[argv.index("--model") + 1], "opus")
            self.assertEqual(argv[argv.index("--effort") + 1], "max")
        with self.assertRaises(ValueError):
            session_argv(self.upstream_cmd(), ArgvSpec(mode="bogus", cli_path="c", model="m", effort="e",
                                                       max_budget_usd="1", settings_file=Path("s")))

    def test_parse_upstream_argv_contract(self) -> None:
        cmd = self.upstream_cmd("P -p tricky", "S")
        got = parse_upstream_argv(cmd)
        self.assertEqual(got, {"prompt": "P -p tricky", "model": "opus", "tools": ",".join(TOOLS),
                               "allowed_tools": TOOLS, "plugin_dir": str(self.work / ".empty_plugins"),
                               "system_prompt": "S", "effort": "max"})
        for flag in ("--effort", "--plugin-dir", "--append-system-prompt", "--allowedTools", "--tools",
                     "--dangerously-skip-permissions", "--strict-mcp-config"):
            broken = list(cmd)
            i = broken.index(flag)
            del broken[i:i + (1 if flag in ("--dangerously-skip-permissions", "--strict-mcp-config") else 2)]
            if flag == "--allowedTools":
                broken = [t for t in broken if t not in TOOLS[1:]]
            with self.assertRaises(UpstreamContractError, msg=flag):
                parse_upstream_argv(broken)
        with self.assertRaises(UpstreamContractError):
            parse_upstream_argv(cmd + ["--surprise"])
        with self.assertRaises(UpstreamContractError):
            parse_upstream_argv(cmd + ["--disallowedTools", "Read"])

    @unittest.skipUnless(example_dir().is_dir(), "example directory not present")
    def test_parse_upstream_argv_on_the_real_build_command(self) -> None:
        source = (example_dir() / "claude_wrapper.py").read_bytes()
        module = types.ModuleType("claude_wrapper_copy")
        module.__file__ = str(self.root / "copy" / "claude_wrapper.py")   # its plugin dir lands in the temp tree
        (self.root / "copy").mkdir()
        exec(compile(source, module.__file__, "exec", dont_inherit=True), module.__dict__)
        cmd = module.build_command("PROMPT", "opus", TOOLS, "SKILL", effort="max")
        got = parse_upstream_argv(cmd)
        self.assertEqual(got["plugin_dir"], str(self.root / "copy" / ".empty_plugins"))
        self.assertEqual(got["allowed_tools"], TOOLS)

    def test_command_fingerprint_has_no_prompt_text(self) -> None:
        argv = session_argv(self.upstream_cmd("SECRET-PROMPT-WORDS", "SECRET-SKILL-WORDS"), self.spec("restricted"))
        fp = command_fingerprint(argv)
        text = json.dumps(fp)
        self.assertNotIn("SECRET-PROMPT-WORDS", text)
        self.assertNotIn("SECRET-SKILL-WORDS", text)
        self.assertEqual(fp["argv_sha256"], util.sha256_bytes(util.canonical_json(argv)))
        self.assertEqual(fp["redacted"]["-p"], {"sha256": util.sha256_bytes(b"SECRET-PROMPT-WORDS"), "chars": 19})
        self.assertIn("--append-system-prompt", fp["redacted"])
        self.assertEqual(fp["argv_flags"][:3], ["-p", "--output-format", "--verbose"])
        self.assertTrue(all(f.startswith("-") for f in fp["argv_flags"]))

    def test_installed_restores_attributes_after_exception(self) -> None:
        shim = self.shim()
        old_sub, old_build = self.wrapper.subprocess, self.wrapper.build_command

        def other(*a, **kw):
            return []

        with self.assertRaises(RuntimeError):
            with installed(self.wrapper, shim, build_command=other):
                self.assertIs(self.wrapper.subprocess, shim)
                self.assertIs(self.wrapper.build_command, other)
                raise RuntimeError("boom")
        self.assertIs(self.wrapper.subprocess, old_sub)
        self.assertIs(self.wrapper.build_command, old_build)

    def test_retry_pause_counts_only_rate_limit_retries(self) -> None:
        line = json.dumps({"type": "system", "subtype": "api_retry", "retry_delay_ms": 1500, "error": "rate_limit"})
        self.assertEqual(retry_pause_s(line), 1.5)
        self.assertEqual(retry_pause_s(json.dumps({"type": "system", "subtype": "api_retry", "retry_delay_ms": 2000,
                                                   "error_status": 429, "error": "other"})), 2.0)
        self.assertEqual(retry_pause_s(json.dumps({"type": "system", "subtype": "api_retry", "retry_delay_ms": 2000,
                                                   "error": "overloaded", "error_status": 529})), 0.0)
        self.assertEqual(retry_pause_s("not json"), 0.0)
        self.assertEqual(retry_pause_s(json.dumps({"type": "assistant"})), 0.0)


if __name__ == "__main__":
    unittest.main()
