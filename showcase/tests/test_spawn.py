# SPDX-License-Identifier: MIT
"""ExportSpawner (spec 3.25, export_mode "after_unit"): one bounded exporter child at a time.

The child is a stand-in `mhforge` package written into a temp package root (the real `python -m mhforge export` is the
CLI of a later layer). It records what it was started with, reads the key from the inherited descriptor its argv names,
and writes a receipt, so every case checks the spawner's real subprocess handling.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from mhforge import journal as journal_mod
from mhforge import procenv, util
from mhforge.telemetry.spawn import ExportSpawner
from tests import helpers

KEY = "mhf-export-test-key-0123456789abcdef"

CHILD_MAIN = textwrap.dedent('''
    import hashlib, json, os, sys, time, subprocess
    argv = sys.argv[1:]
    manifest_path = argv[argv.index("--manifest") + 1]
    unit = argv[argv.index("--unit") + 1]
    source = argv[argv.index("--key-source") + 1]
    fd = int(source.split(":", 1)[1])
    with os.fdopen(fd, "r") as fh:
        line = fh.read()
    key = line.strip().split("=", 1)[1] if line.startswith("export=") else ""
    manifest = json.load(open(manifest_path))
    root = os.path.join(os.path.dirname(manifest_path), "state", manifest["campaign"])
    behaviour = open(os.path.join(os.path.dirname(manifest_path), "behaviour.txt")).read().strip()
    log = os.path.join(os.path.dirname(manifest_path), "children.jsonl")
    started = time.time()
    report = {"unit": unit, "argv": sys.argv, "cwd": os.getcwd(), "sys_path": sys.path,
              "package_file": os.path.abspath(__file__), "env": dict(os.environ), "key_sha256":
              hashlib.sha256(key.encode()).hexdigest(), "line_ends_newline": line.endswith("\\n"),
              "start": started, "pid": os.getpid(), "pgid": os.getpgid(0),
              "home_mode": os.stat(os.environ["HOME"]).st_mode & 0o777, "home_list": sorted(os.listdir(os.environ["HOME"]))}
    if behaviour == "write_home":
        open(os.path.join(os.environ["HOME"], ".netrc"), "w").close()
    if behaviour == "hang":
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        report["grandchild"] = child.pid
    fdw = os.open(log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.write(fdw, (json.dumps({**report, "event": "start"}) + "\\n").encode())
    if behaviour == "hang":
        time.sleep(60)
    time.sleep(0.3)
    slug = unit.replace(":", "__")
    receipts = os.path.join(root, "export", "receipts")
    os.makedirs(receipts, exist_ok=True)
    with open(os.path.join(receipts, slug + ".agent_lens.json"), "w") as fh:
        json.dump({"status": "verified" if behaviour != "fail" else "failed"}, fh)
    os.write(fdw, (json.dumps({"unit": unit, "event": "end", "end": time.time()}) + "\\n").encode())
    sys.stderr.write("child finished " + unit + "\\n")
    sys.exit(3 if behaviour == "fail" else 0)
''')


class SpawnCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="mhf-spawn-")
        tmp = Path(self._tmp.name).resolve()
        self.package_root = tmp / "pkgroot"
        (self.package_root / "mhforge").mkdir(parents=True)
        (self.package_root / "mhforge" / "__init__.py").write_text("")
        (self.package_root / "mhforge" / "__main__.py").write_text(CHILD_MAIN)
        self._ctx = helpers.tmp_state(manifest=helpers.make_manifest(**{"telemetry.export_mode": "after_unit",
                                                                         "telemetry.export_timeout_s": 20}))
        self.st = self._ctx.__enter__()
        self.behaviour("ok")
        self.clock = helpers.FakeClock()
        self.journal = journal_mod.Journal(self.st.paths.journal, clock=self.clock)
        self.base_env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp), "LANG": "C.UTF-8",
                         "MHFORGE_EXPORT_API_KEY": KEY, "SOME_OTHER": "x"}

    def tearDown(self) -> None:
        self._ctx.__exit__(None, None, None)
        self._tmp.cleanup()

    def behaviour(self, text: str) -> None:
        (self.st.sc / "behaviour.txt").write_text(text)

    def spawner(self, *, manifest=None, key=KEY) -> ExportSpawner:
        return ExportSpawner(manifest or self.st.manifest, self.st.manifest_path, self.st.paths, journal=self.journal,
                             base_env=self.base_env, package_root=self.package_root, export_key=lambda: key,
                             python=sys.executable, clock=self.clock)

    def children(self) -> list[dict]:
        p = self.st.sc / "children.jsonl"
        return [json.loads(line) for line in p.read_text().splitlines()] if p.exists() else []

    def exports(self) -> list[tuple]:
        return [(e["key"], e["status"], e.get("detail")) for e in journal_mod.read_journal(self.st.paths.journal)
                if e["op"] == "export"]


class Spawner(SpawnCase):
    def test_one_child_at_a_time(self):
        sp = self.spawner()
        units = ["job:val.no_memory.USPTO.a1", "eval:val.no_memory", "loop:iter000"]
        for u in units:
            sp.submit(u)
        self.assertGreaterEqual(sp.backlog(), 1)
        sp.drain(timeout_s=60)
        self.assertEqual(sp.backlog(), 0)
        events = self.children()
        starts = {e["unit"]: e["start"] for e in events if e["event"] == "start"}
        ends = {e["unit"]: e["end"] for e in events if e["event"] == "end"}
        self.assertEqual([e["unit"] for e in events if e["event"] == "start"], units)     # in submission order
        for a, b in zip(units, units[1:]):
            self.assertLessEqual(ends[a], starts[b])                  # the next child starts after the last ended
        self.assertEqual([(k, s) for k, s, _d in self.exports()],
                         [(u, s) for u in units for s in ("requested", "complete")])
        self.assertEqual(self.exports()[1][2], {"exit_code": 0, "statuses": {"agent_lens": "verified"}})
        sp.close()

    def test_child_env_is_exporter_env(self):
        sp = self.spawner()
        sp.submit("job:val.no_memory.USPTO.a1")
        sp.close()
        env = self.children()[0]["env"]
        env.pop("__CF_USER_TEXT_ENCODING", None)                      # macOS adds it to every child
        want = procenv.exporter_env(self.st.manifest, self.base_env, home=env["HOME"],
                                    package_root=self.package_root, netrc_file=self.st.paths.export_netrc)
        self.assertEqual(env, want)
        self.assertNotEqual(env["HOME"], self.base_env["HOME"])
        self.assertEqual(env["NETRC"], str(self.st.paths.export_netrc))
        self.assertEqual(env["PYTHONPATH"], str(self.package_root))
        self.assertNotIn("SOME_OTHER", env)
        self.assertEqual(self.st.paths.export_netrc.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.st.paths.export_netrc.read_bytes(), b"")

    def test_child_gets_key_over_inherited_pipe_not_env_or_argv(self):
        sp = self.spawner()
        sp.submit("job:val.no_memory.USPTO.a1")
        sp.close()
        child = self.children()[0]
        self.assertEqual(child["key_sha256"], hashlib.sha256(KEY.encode()).hexdigest())
        self.assertTrue(child["line_ends_newline"])
        argv = child["argv"][1:]
        self.assertEqual(argv[:6], ["export", "--manifest", str(self.st.manifest_path), "--live", "--key-source",
                                    argv[5]])
        self.assertRegex(argv[5], r"fd:[0-9]+")
        self.assertEqual(argv[6:], ["--unit", "job:val.no_memory.USPTO.a1"])
        self.assertNotIn(KEY, json.dumps(child["argv"]))
        self.assertNotIn(KEY, json.dumps(child["env"]))
        self.assertNotIn("MHFORGE_EXPORT_API_KEY", child["env"])
        for p in self.st.paths.root.rglob("*"):                       # nothing the spawner wrote holds the key
            if p.is_file():
                self.assertNotIn(KEY.encode(), p.read_bytes(), p.name)
        self.assertNotIn(KEY, json.dumps(sp.results))
        bad = self.spawner(key="two\nlines")                          # a malformed key is never written to a child
        bad.submit("job:val.no_memory.USPTO.a1")
        bad.close()
        self.assertEqual(len([e for e in self.children() if e["event"] == "start"]), 1)     # no second child
        self.assertEqual(self.exports()[-1][1:], ("failed", {"exit_code": None, "statuses": {},
                                                             "error": "export key missing or malformed"}))

    def test_child_can_import_package_from_foreign_cwd(self):
        here = os.getcwd()
        with tempfile.TemporaryDirectory(prefix="mhf-cwd-") as foreign:
            os.chdir(foreign)
            try:
                sp = self.spawner()
                sp.submit("job:val.no_memory.USPTO.a1")
                sp.close()
            finally:
                os.chdir(here)
        child = self.children()[0]
        self.assertEqual(child["argv"][0], str(self.package_root / "mhforge" / "__main__.py"))
        self.assertEqual(child["package_file"], str(self.package_root / "mhforge" / "__main__.py"))
        self.assertEqual(os.path.realpath(child["cwd"]), os.path.realpath(child["env"]["HOME"]))   # LD9: not the package
        self.assertIn(str(self.package_root), child["sys_path"])
        self.assertEqual(self.exports()[-1][1], "complete")

    def test_each_child_has_its_own_empty_home_under_state(self):
        # LD9: Agent Lens reads $HOME/.netrc whatever NETRC says, wandb reads ~/.config/wandb/settings or
        # $WANDB_CONFIG_DIR/settings and <cwd>/wandb/settings; the child's HOME, WANDB_CONFIG_DIR and cwd are one new
        # empty 0700 directory under STATE, never the caller's HOME
        real_home = Path(self.base_env["HOME"])
        (real_home / ".netrc").write_text("machine api.wandb.ai login user password users-own-key\n")
        sp = self.spawner()
        sp.submit("job:val.no_memory.USPTO.a1")
        sp.submit("eval:val.no_memory")
        sp.close()
        starts = [c for c in self.children() if c["event"] == "start"]
        homes = [c["env"]["HOME"] for c in starts]
        self.assertEqual(len(set(homes)), 2)                          # a new one per child
        homes_root = os.path.realpath(self.st.paths.export_root / "homes")
        for c in starts:
            home = c["env"]["HOME"]
            self.assertEqual(os.path.dirname(os.path.realpath(home)), homes_root)
            self.assertEqual(c["env"]["WANDB_CONFIG_DIR"], home)
            self.assertEqual(os.path.realpath(c["cwd"]), os.path.realpath(home))
            self.assertEqual((c["home_mode"], c["home_list"]), (0o700, []))
            self.assertFalse(os.path.exists(home))                    # empty after the child: removed
            self.assertFalse([n for n in c["env"] if n.startswith(("OTEL_", "CES_", "CWSANDBOX_"))
                              or (n.startswith("WANDB_") and n not in ("WANDB_CONFIG_DIR", "WANDB_DISABLE_WEAVE"))])
        self.behaviour("write_home")
        sp = self.spawner()
        sp.submit("loop:iter000")
        sp.close()
        kept = [c for c in self.children() if c["event"] == "start"][-1]["env"]["HOME"]
        self.assertTrue(os.path.isfile(os.path.join(kept, ".netrc")))  # a written home is kept, never deleted
        self.assertEqual((real_home / ".netrc").read_text(), "machine api.wandb.ai login user password users-own-key\n")

    def test_timeout_kills_child_and_journals_failed(self):
        self.behaviour("hang")
        m = helpers.make_manifest(**{"telemetry.export_mode": "after_unit", "telemetry.export_timeout_s": 1})
        sp = self.spawner(manifest=m)
        t0 = time.monotonic()
        sp.submit("job:val.no_memory.USPTO.a1")
        sp.drain(timeout_s=30)
        self.assertLess(time.monotonic() - t0, 20)
        key, status, detail = self.exports()[-1]
        self.assertEqual((key, status), ("job:val.no_memory.USPTO.a1", "failed"))
        self.assertTrue(detail["timed_out"])
        self.assertEqual(detail["exit_code"], -signal.SIGKILL)
        child = self.children()[0]
        self.assertEqual(child["pgid"], child["pid"])                 # its own process group
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:                            # the whole group was killed
            try:
                os.kill(child["grandchild"], 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("the child's own child survived the timeout")
        sp.close()

    def test_drain(self):
        sp = self.spawner()
        sp.drain(timeout_s=1)                                         # nothing queued: returns at once
        sp.submit("job:val.no_memory.USPTO.a1")
        sp.submit("eval:val.no_memory")
        t0 = time.monotonic()
        sp.drain(timeout_s=0.05)                                      # a timeout returns with work left
        self.assertLess(time.monotonic() - t0, 2)
        self.assertGreater(sp.backlog(), 0)
        sp.drain()
        self.assertEqual(sp.backlog(), 0)
        self.assertEqual(len([e for e in self.children() if e["event"] == "end"]), 2)
        sp.close()
        with self.assertRaises(RuntimeError):
            sp.submit("loop:iter001")

    def test_failed_export_is_journaled_not_raised(self):
        self.behaviour("fail")
        sp = self.spawner()
        sp.submit("job:val.no_memory.USPTO.a1")
        sp.close()
        self.assertEqual(self.exports()[-1][1:], ("failed", {"exit_code": 3, "statuses": {"agent_lens": "failed"}}))
        log = self.st.paths.export_root / "children" / "job__val.no_memory.USPTO.a1.stderr.txt"
        self.assertIn("child finished", log.read_text())
        for bad in ("not-a-unit", "job:x\ny", "job:../../etc"):
            with self.assertRaises(ValueError):
                sp.submit(bad)


if __name__ == "__main__":
    unittest.main()
