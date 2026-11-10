# SPDX-License-Identifier: MIT
import json
import os
import random
import re
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock

from mhforge import util
from mhforge.errors import StateError, WorkspaceError
from tests import helpers

_SCRUB_CHILD = r"""
import json, sys
from mhforge import util
sys.stdout.write("before\n"); sys.stdout.flush()
sys.stdin.readline()
results = {"scrubbed": util.scrub_process_env("MHF_TEST_CANARY"), "absent": util.scrub_process_env("MHF_TEST_NOT_SET")}
import os
results["still_in_environ"] = "MHF_TEST_CANARY" in os.environ
sys.stdout.write(json.dumps(results) + "\n"); sys.stdout.flush()
sys.stdin.readline()
"""


class UtilTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="mhf-util-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    # ------------------------------------------------------------ SQLite read-only open
    def test_open_sqlite_ro_creates_nothing_and_reads_wal(self):
        db = self.tmp / "ledger.sqlite"
        w = sqlite3.connect(db, isolation_level=None)
        w.execute("PRAGMA journal_mode=WAL")
        w.execute("CREATE TABLE t (x INTEGER)")
        w.execute("INSERT INTO t VALUES (1)")
        w.close()
        self.assertFalse(Path(str(db) + "-wal").exists())   # an idle WAL database: the log was checkpointed away
        before = sorted(p.name for p in self.tmp.iterdir())
        conn, stale = util.open_sqlite_ro(db)
        self.assertEqual(conn.execute("SELECT x FROM t").fetchall(), [(1,)])
        self.assertFalse(stale)
        conn.close()
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), before, "the read-only open created a file")

        # an active writer: the log exists and holds rows the main file does not
        w = sqlite3.connect(db, isolation_level=None)
        w.execute("PRAGMA wal_autocheckpoint=0")
        w.execute("INSERT INTO t VALUES (2)")
        self.assertTrue(Path(str(db) + "-wal").exists())
        conn, stale = util.open_sqlite_ro(db)
        self.assertEqual(sorted(r[0] for r in conn.execute("SELECT x FROM t")), [1, 2])
        self.assertFalse(stale)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("INSERT INTO t VALUES (3)")
        conn.close()
        w.close()

        # a read-only copy of an idle database
        copy_dir = self.tmp / "ro"
        copy_dir.mkdir()
        shutil.copy2(db, copy_dir / "ledger.sqlite")
        os.chmod(copy_dir / "ledger.sqlite", 0o444)
        os.chmod(copy_dir, 0o555)
        self.addCleanup(os.chmod, copy_dir, 0o755)
        conn, stale = util.open_sqlite_ro(copy_dir / "ledger.sqlite")
        self.assertEqual(sorted(r[0] for r in conn.execute("SELECT x FROM t")), [1, 2])
        conn.close()
        self.assertEqual(sorted(p.name for p in copy_dir.iterdir()), ["ledger.sqlite"])

        # a stray -wal next to a read-only copy: mode=ro fails, the retry is immutable and flagged possibly stale
        ro2 = self.tmp / "ro2"
        ro2.mkdir()
        shutil.copy2(db, ro2 / "ledger.sqlite")
        (ro2 / "ledger.sqlite-wal").write_bytes(b"")
        os.chmod(ro2 / "ledger.sqlite", 0o444)
        os.chmod(ro2 / "ledger.sqlite-wal", 0o444)
        os.chmod(ro2, 0o555)
        self.addCleanup(os.chmod, ro2, 0o755)
        conn, stale = util.open_sqlite_ro(ro2 / "ledger.sqlite")
        self.assertEqual(sorted(r[0] for r in conn.execute("SELECT x FROM t")), [1, 2])
        self.assertTrue(stale)
        conn.close()
        self.assertEqual(sorted(p.name for p in ro2.iterdir()), ["ledger.sqlite", "ledger.sqlite-wal"])

        with self.assertRaises(FileNotFoundError):
            util.open_sqlite_ro(self.tmp / "absent.sqlite")
        self.assertFalse((self.tmp / "absent.sqlite").exists())

    # ------------------------------------------------------------ environment scrub
    def test_scrub_process_env_hides_value_from_ps(self):
        ps = shutil.which("ps", path="/bin:/usr/bin")
        if ps is None:
            self.skipTest("ps is not available")
        canary = "mhfcanary" + "%032x" % random.getrandbits(128)
        env = helpers.child_env(MHF_TEST_CANARY=canary)
        child = subprocess.Popen([sys.executable, "-c", _SCRUB_CHILD], env=env, cwd=str(helpers.SC),
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.kill)

        def visible() -> bool:              # the ps output is searched, never printed
            out = subprocess.run([ps, "eww", "-p", str(child.pid)], capture_output=True, text=True,
                                 env=helpers.child_env()).stdout
            return canary in out

        try:
            self.assertEqual(child.stdout.readline().strip(), "before")
            if not visible():
                self.skipTest("ps does not show process environments here")
            child.stdin.write("go\n")
            child.stdin.flush()
            result = json.loads(child.stdout.readline())
            self.assertTrue(result["scrubbed"])
            self.assertFalse(result["absent"])
            self.assertFalse(result["still_in_environ"])
            self.assertFalse(visible(), "the canary is still visible to ps after the scrub")
            child.stdin.write("done\n")
            child.stdin.flush()
            child.wait(timeout=10)
        finally:
            child.stdin.close()
            child.stdout.close()

    # ------------------------------------------------------------ key digests
    def test_append_key_digest_mode_0600_no_value(self):
        clock = helpers.FakeClock()
        path = self.tmp / "state" / "keydigests.json"
        value = "f" * 8 + "0123456789abcdef" * 2
        entry = util.append_key_digest(path, "MHFORGE_FORGE_API_KEY", value, clock=clock)
        self.assertEqual(entry, {"name": "MHFORGE_FORGE_API_KEY", "len": len(value),
                                 "sha256": util.sha256_bytes(value.encode()), "utc": util.utc_iso(clock.now())})
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        raw = path.read_bytes()
        self.assertNotIn(value.encode(), raw)
        doc = json.loads(raw)
        self.assertEqual(doc["schema"], "mhforge.keydigests/1")
        self.assertEqual(doc["keys"], [entry])
        again = util.append_key_digest(path, "MHFORGE_FORGE_API_KEY", value, clock=clock)
        self.assertEqual(again, entry)
        self.assertEqual(len(json.loads(path.read_bytes())["keys"]), 1)   # one entry per digest
        other = util.append_key_digest(path, "MHFORGE_EXPORT_API_KEY", "another-key-value-123456", clock=clock)
        self.assertEqual([e["name"] for e in json.loads(path.read_bytes())["keys"]],
                         ["MHFORGE_FORGE_API_KEY", "MHFORGE_EXPORT_API_KEY"])
        self.assertEqual(other["len"], 24)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["keydigests.json"])
        with self.assertRaises(ValueError):
            util.append_key_digest(path, "X_KEY", "")

    # ------------------------------------------------------------ loopback guard
    def test_loopback_guard_blocks_non_loopback_connect(self):
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)
        port = server.getsockname()[1]
        with util.loopback_guard():
            with util.loopback_guard():                 # re-entrant
                pass
            c = socket.create_connection(("127.0.0.1", port), timeout=5)
            c.close()
            for target in (("192.0.2.1", 9), ("198.51.100.1", 80), ("203.0.113.1", 53), ("::ffff:192.0.2.1", 9)):
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                try:
                    with self.assertRaises(util.NonLoopbackConnect):
                        s.connect(target)
                    with self.assertRaises(util.NonLoopbackConnect):
                        s.connect_ex(target)
                finally:
                    s.close()
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                with self.assertRaises(util.NonLoopbackConnect):
                    u.sendto(b"x", ("192.0.2.1", 9))
                u.sendto(b"x", ("127.0.0.1", port))
            finally:
                u.close()
            with self.assertRaises(OSError):            # a subclass of PermissionError / OSError
                socket.create_connection(("192.0.2.1", 9), timeout=1)
        self.assertTrue(issubclass(util.NonLoopbackConnect, PermissionError))
        # after the guard: the socket class is restored (no guarded wrapper left)
        self.assertNotIn("connect", socket.socket.__dict__)
        self.assertNotIn("sendto", socket.socket.__dict__)

    # ------------------------------------------------------------ JSON and JSONL
    def test_write_json_is_atomic_on_failure(self):
        path = self.tmp / "rec" / "job.json"
        util.write_json(path, {"v": 1, "text": "é"})
        self.assertEqual(json.loads(path.read_text("utf-8")), {"v": 1, "text": "é"})
        self.assertIn("é", path.read_text("utf-8"))              # UTF-8, not escaped
        with self.assertRaises(TypeError):
            util.write_json(path, {"v": object()})                # serialisation fails
        self.assertEqual(json.loads(path.read_text("utf-8"))["v"], 1)
        with mock.patch("mhforge.util.os.replace", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                util.write_json(path, {"v": 2})
        self.assertEqual(json.loads(path.read_text("utf-8"))["v"], 1)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["job.json"], "a temp file was left behind")
        util.write_json(path, {"v": 3}, fsync=True)
        self.assertEqual(util.read_json(path), {"v": 3})
        big = self.tmp / "big.json"
        util.write_json(big, {"x": "a" * 200})
        with self.assertRaises(StateError):
            util.read_json(big, max_bytes=50)
        bad = self.tmp / "bad.json"
        bad.write_text("{not json")
        with self.assertRaises(StateError):
            util.read_json(bad)

    def test_append_jsonl_concurrent_rows_stay_whole(self):
        path = self.tmp / "journal.jsonl"
        pad = "x" * 300

        def work(t):
            for i in range(200):
                util.append_jsonl(path, {"t": t, "i": i, "pad": pad, "u": "\u2028é"})

        threads = [threading.Thread(target=work, args=(t,)) for t in range(16)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        rows = util.read_jsonl(path)
        self.assertEqual(len(rows), 16 * 200)
        for t in range(16):
            self.assertEqual([r["i"] for r in rows if r["t"] == t], list(range(200)))
        self.assertTrue(all(r["pad"] == pad and r["u"] == "\u2028é" for r in rows))
        self.assertEqual(path.read_bytes().count(b"\n"), 16 * 200)

    def test_read_jsonl_ignores_torn_tail_only(self):
        path = self.tmp / "rows.jsonl"
        util.append_jsonl(path, {"a": 1})
        util.append_jsonl(path, {"a": 2})
        with open(path, "ab") as fh:
            fh.write(b'{"a": 3, "tor')
        self.assertEqual(util.read_jsonl(path), [{"a": 1}, {"a": 2}])
        bad = self.tmp / "bad.jsonl"
        bad.write_bytes(b'{"a": 1}\n{"a": \n{"a": 3}\n')
        with self.assertRaises(StateError):
            util.read_jsonl(bad)
        notobj = self.tmp / "list.jsonl"
        notobj.write_bytes(b'{"a": 1}\n[1, 2]\n')
        with self.assertRaises(StateError):
            util.read_jsonl(notobj)
        self.assertEqual(util.read_jsonl(self.tmp / "absent.jsonl"), [])
        with self.assertRaises(TypeError):
            util.append_jsonl(path, ["not", "a", "dict"])

    def test_read_marked_jsonl_accepts_a_terminated_torn_tail_only(self):
        import hashlib
        torn = b'{"a": 3, "tor'
        marker = {"op": "journal", "key": "torn_tail", "status": "terminated",
                  "detail": {"bytes": len(torn), "sha256": hashlib.sha256(torn).hexdigest()}}
        path = self.tmp / "journal.jsonl"
        path.write_bytes(b'{"a": 1}\n' + torn + b"\n" + json.dumps(marker).encode() + b'\n{"a": 4}\n' + torn)
        self.assertEqual(util.read_marked_jsonl(path), [{"a": 1}, marker, {"a": 4}])   # last torn tail ignored
        with self.assertRaises(StateError):
            util.read_jsonl(path)                          # the plain reader refuses the repaired file
        rows, tail = util.parse_marked_jsonl(path.read_bytes())
        self.assertEqual((len(rows), tail), (3, torn))
        self.assertEqual(util.JOURNAL_TORN_MARKER, ("journal", "torn_tail", "terminated"))
        # an unmarked bad middle line, a marker naming other bytes, and a non-object line stay errors
        wrong = dict(marker, detail={"bytes": len(torn), "sha256": "0" * 64})
        for data in (b'{"a": 1}\n' + torn + b'\n{"a": 4}\n',
                     b'{"a": 1}\n' + torn + b"\n" + json.dumps(wrong).encode() + b"\n",
                     b'{"a": 1}\n[1, 2]\n{"a": 4}\n'):
            with self.subTest(data=data[:30]):
                path.write_bytes(data)
                with self.assertRaises(StateError):
                    util.read_marked_jsonl(path)
        self.assertEqual(util.read_marked_jsonl(self.tmp / "absent.jsonl"), [])

    def test_canonical_json_is_stable_keeps_unicode_rejects_nan(self):
        a = {"b": [1, {"z": "中文", "a": None}], "a": 1.5}
        b = {"a": 1.5, "b": [1, {"a": None, "z": "中文"}]}
        self.assertEqual(util.canonical_json(a), util.canonical_json(b))
        self.assertEqual(util.canonical_json(a), '{"a":1.5,"b":[1,{"a":null,"z":"中文"}]}'.encode("utf-8"))
        for bad in (float("nan"), float("inf"), {"x": float("-inf")}):
            with self.assertRaises(ValueError):
                util.canonical_json(bad)
        self.assertEqual(util.sha256_bytes(b""), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
        f = self.tmp / "f.bin"
        f.write_bytes(b"abc")
        self.assertEqual(util.sha256_file(f), util.sha256_bytes(b"abc"))

    # ------------------------------------------------------------ trees
    def test_tree_hashes_refuse_symlink_and_digest_changes(self):
        root = self.tmp / "ws"
        (root / "agents").mkdir(parents=True)
        (root / "agents" / "x.py").write_text("x = 1\n")
        (root / "config.yaml").write_text("a: 1\n")
        for skipped in (".venv", "__pycache__", "logs", ".empty_plugins"):
            (root / skipped).mkdir()
            (root / skipped / "f.txt").write_text("skip")
        os.symlink("/usr/bin/env", root / ".venv" / "python")    # links inside excluded directories are not looked at
        (root / "agents" / "__pycache__").mkdir()
        (root / "agents" / "__pycache__" / "x.pyc").write_bytes(b"\0")
        hashes = util.tree_hashes(root)
        self.assertEqual(sorted(hashes), ["agents/x.py", "config.yaml"])
        self.assertEqual(hashes["config.yaml"], util.sha256_bytes(b"a: 1\n"))
        self.assertEqual(util.tree_hashes(root, include=["agents/*.py"]), {"agents/x.py": hashes["agents/x.py"]})
        d1 = util.tree_digest(hashes)
        self.assertEqual(d1, util.tree_digest(dict(reversed(list(hashes.items())))))
        (root / "agents" / "x.py").write_text("x = 2\n")
        self.assertNotEqual(util.tree_digest(util.tree_hashes(root)), d1)
        os.symlink(root / "config.yaml", root / "agents" / "link.py")
        with self.assertRaises(WorkspaceError):
            util.tree_hashes(root)
        os.unlink(root / "agents" / "link.py")
        os.symlink(root / "logs", root / "agents" / "dirlink")
        with self.assertRaises(WorkspaceError):
            util.tree_hashes(root)

    def test_changed_files(self):
        before = {"a": "1", "b": "2", "c": "3"}
        after = {"b": "2", "c": "4", "d": "5"}
        self.assertEqual(util.changed_files(before, after), {"added": ["d"], "removed": ["a"], "modified": ["c"]})
        self.assertEqual(util.changed_files(before, dict(before)), {"added": [], "removed": [], "modified": []})

    # ------------------------------------------------------------ money
    def test_micros_rounds_up(self):
        self.assertEqual(util.micros("0.006718"), 6718)
        self.assertEqual(util.micros("0.0000001"), 1)                 # any fraction of a micro-dollar rounds up
        self.assertEqual(util.micros("1.0000001"), 1000001)
        self.assertEqual(util.micros("60.00"), 60_000_000)
        self.assertEqual(util.micros(2), 2_000_000)
        self.assertEqual(util.micros(Decimal("0.03") * 131072 / 1_000_000 + Decimal("0.17") * 16384 / 1_000_000), 6718)
        self.assertEqual(util.micros("-0.0000009"), 0)                # ceiling, not away from zero
        for bad in (0.1, True):
            with self.assertRaises(TypeError):
                util.micros(bad)
        for bad in ("abc", "NaN", "Infinity"):
            with self.assertRaises(ValueError):
                util.micros(bad)
        self.assertEqual(util.usd(6480), "0.006480")
        self.assertEqual(util.usd(60_000_000), "60.000000")
        self.assertEqual(util.usd(-1), "-0.000001")
        self.assertEqual(util.micros(util.usd(123456789)), 123456789)

    # ------------------------------------------------------------ time and ids
    def test_utc_iso_round_trip_microseconds(self):
        self.assertEqual(util.utc_iso(1794268800.0), "2026-11-10T00:00:00.000000Z")
        self.assertEqual(util.utc_iso(1794268800 + 3723.000001), "2026-11-10T01:02:03.000001Z")
        self.assertEqual(util.parse_utc("2026-11-10T01:02:03.000001Z"), 1794268800 + 3723.000001)
        rng = random.Random(7)
        for _ in range(500):
            us = rng.randrange(0, 4_000_000_000_000_000)
            text = util.utc_iso(us / 1_000_000)
            self.assertTrue(re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", text), text)
            self.assertEqual(util.utc_iso(util.parse_utc(text)), text)
        for bad in ("2026-11-10T01:02:03.000001", "2026-11-10T01:02:03+00:00", "2026-11-10 01:02:03Z", "", "Z"):
            with self.assertRaises(ValueError):
                util.parse_utc(bad)
        self.assertEqual(util.parse_utc("2026-11-10T00:00:00Z"), 1794268800.0)
        self.assertEqual(util.utc_now(helpers.FakeClock(start=1794268800.5)), "2026-11-10T00:00:00.500000Z")
        clock = util.SystemClock()
        self.assertGreater(clock.now(), 1.7e9)
        m = clock.monotonic()
        clock.sleep(0)
        self.assertGreaterEqual(clock.monotonic(), m)

    def test_ids_shape(self):
        clock = helpers.FakeClock(start=1794268800.0 + 3723)
        ids = util.IdSource(clock=clock)
        self.assertRegex(ids.call_id(), r"\Ac-20261110T010203-[0-9a-f]{12}\Z")
        self.assertRegex(ids.ledger_id(), r"\Al-20261110T010203-[0-9a-f]{12}\Z")
        tok = ids.token()
        self.assertRegex(tok, r"\Amhfjob_[A-Za-z0-9_-]{43}\Z")
        self.assertNotEqual(tok, ids.token())
        a = util.IdSource(clock=clock, rng=random.Random(5))
        b = util.IdSource(clock=clock, rng=random.Random(5))
        self.assertEqual([a.call_id(), a.ledger_id(), a.token()], [b.call_id(), b.ledger_id(), b.token()])
        self.assertRegex(a.token(), r"\Amhfjob_[A-Za-z0-9_-]{43}\Z")
        seq = helpers.SeqIds(clock=clock)
        self.assertEqual(seq.call_id(), "c-20261110T010203-000000000001")
        self.assertEqual(seq.ledger_id(), "l-20261110T010203-000000000002")
        self.assertRegex(seq.token(), r"\Amhfjob_[A-Za-z0-9_-]{43}\Z")

    # ------------------------------------------------------------ lookups and components
    def test_lookup_and_safe_component(self):
        m = {"a": {"b": {"c": 3}, "n": None}, "x": 1}
        self.assertEqual(util.lookup(m, "a.b.c"), 3)
        self.assertIsNone(util.lookup(m, "a.n"))
        self.assertEqual(util.lookup(m, "a.b"), {"c": 3})
        with self.assertRaises(KeyError):
            util.lookup(m, "a.b.d")
        with self.assertRaises(KeyError):
            util.lookup(m, "x.y")
        self.assertEqual(util.lookup(m, "a.b.d", None), None)
        self.assertEqual(util.lookup(m, "q", default=7), 7)
        for ok in ("val.no_memory.USPTO.a1", "job__x", "A", "a" * 128, "c-20261110T010203-0c11aa90be21"):
            self.assertEqual(util.safe_component(ok), ok)
        for bad in ("", ".hidden", "..", "a/b", "a\\b", "a b", "a" * 129, "x\n", "-x", "é", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                util.safe_component(bad)

    def test_sleep_gap(self):
        self.assertEqual(util.sleep_gap(10.0, 10.0), 0.0)
        self.assertEqual(util.sleep_gap(70.0, 5.0), 65.0)
        self.assertEqual(util.sleep_gap(5.0, 7.0), 0.0)               # the wall clock stepped back: never negative
        clock = helpers.FakeClock()
        w0, m0 = clock.now(), clock.monotonic()
        clock.advance(5)
        clock.advance_wall(3600)                                       # the host slept for an hour
        self.assertEqual(util.sleep_gap(clock.now() - w0, clock.monotonic() - m0), 3600.0)

    # ------------------------------------------------------------ review regressions
    def test_micros_rounds_up_beyond_default_decimal_precision(self):
        # 29+ significant digits: the default 28-digit context rounded the product half-even before the ceiling
        self.assertEqual(util.micros("0.00000100000000000000000000000000001"), 2)
        self.assertEqual(util.micros(Decimal("1.0000000000000000000000000000001")), 1000001)
        self.assertEqual(util.micros("0.000001000000000000000000000000000000"), 1)

    def test_tree_hashes_refuse_symlink_with_excluded_name_and_unreadable_dir(self):
        root = self.tmp / "ws"
        (root / "agents").mkdir(parents=True)
        (root / "agents" / "x.py").write_text("x\n")
        for link in (root / "logs", root / "agents" / "__pycache__", root / ".venv"):
            os.symlink(self.tmp, link)
            with self.assertRaises(WorkspaceError, msg=str(link.name)):
                util.tree_hashes(root)
            os.unlink(link)
        hidden = root / "agents" / "hidden"
        hidden.mkdir()
        (hidden / "y.py").write_text("y\n")
        os.chmod(hidden, 0)
        self.addCleanup(os.chmod, hidden, 0o755)
        if os.access(hidden, os.R_OK):
            self.skipTest("running with permissions that ignore mode 000")
        with self.assertRaises(WorkspaceError):
            util.tree_hashes(root)
        os.chmod(hidden, 0o755)
        self.assertIn("agents/hidden/y.py", util.tree_hashes(root))
        self.assertEqual(util.tree_hashes(self.tmp / "absent"), {})

    def test_append_key_digest_concurrent_writers_keep_every_entry(self):
        path = self.tmp / "state" / "keydigests.json"
        values = ["key-value-%04d-abcdefghijklmnop" % i for i in range(32)]
        threads = [threading.Thread(target=util.append_key_digest, args=(path, f"K_{i}", v)) for i, v in enumerate(values)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        keys = json.loads(path.read_bytes())["keys"]
        self.assertEqual(sorted(e["sha256"] for e in keys), sorted(util.sha256_bytes(v.encode()) for v in values))
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["keydigests.json"])

    def test_open_sqlite_ro_wal_without_shm_creates_nothing(self):
        db = self.tmp / "ledger.sqlite"
        w = sqlite3.connect(db, isolation_level=None)
        w.execute("PRAGMA journal_mode=WAL")
        w.execute("PRAGMA wal_autocheckpoint=0")
        w.execute("CREATE TABLE t (x INTEGER)")
        w.execute("INSERT INTO t VALUES (1)")
        copy = self.tmp / "copy"
        copy.mkdir()
        shutil.copy(db, copy / "ledger.sqlite")
        shutil.copy(str(db) + "-wal", copy / "ledger.sqlite-wal")         # a writable copy with a log and no -shm
        w.close()
        conn, stale = util.open_sqlite_ro(copy / "ledger.sqlite")
        conn.close()
        self.assertTrue(stale)
        self.assertEqual(sorted(p.name for p in copy.iterdir()), ["ledger.sqlite", "ledger.sqlite-wal"])

    def test_helpers_fake_clock_and_stamp_rows(self):
        clock = helpers.FakeClock(start=1794268800.0, monotonic_start=50.0)
        clock.sleep(2.5)
        clock.sleep(0)
        self.assertEqual(clock.sleeps, [2.5, 0.0])
        self.assertEqual((clock.now(), clock.monotonic()), (1794268802.5, 52.5))
        self.assertEqual(clock.utc(), "2026-11-10T00:00:02.500000Z")
        rows = helpers.stamp_rows(['{"type": "system"}\n', b'{"type": "result"}', "", "\n"],
                                  "2026-11-10T00:00:00.000000Z", 0.5)
        self.assertEqual(rows, [{"t_utc": "2026-11-10T00:00:00.000000Z", "stream": "stdout", "line": '{"type": "system"}'},
                                {"t_utc": "2026-11-10T00:00:00.500000Z", "stream": "stdout", "line": '{"type": "result"}'}])
        env = helpers.child_env(EXTRA="1")
        self.assertEqual(env["EXTRA"], "1")
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(set(env), {"PATH", "PYTHONDONTWRITEBYTECODE", "LANG", "PYTHONPATH", "EXTRA"})


if __name__ == "__main__":
    unittest.main()
