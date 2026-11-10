# SPDX-License-Identifier: MIT
"""ExportSpawner (spec 3.25, export_mode "after_unit"): one bounded exporter child at a time.

The child is `<python> -m mhforge export --manifest <abs> --live --key-source fd:<n> --unit <unit>`, started with the
environment of procenv.exporter_env (PYTHONPATH = the directory that holds the package, NETRC = the empty state file, no
key). LD9: each child gets its own new empty home `STATE/export/homes/<unit>.<random>` (mode 0700, from mkdtemp), which
is its HOME, its WANDB_CONFIG_DIR and its working directory, so no ~/.netrc, global or workspace wandb settings file is
found; the home is removed after the child when it is still empty. The export key reaches the child only over an
inherited pipe: the spawner writes the one line `export=<key>` and closes its end. An export failure never fails the
search; it is a journal entry and a receipt. A halt does not stop the child.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Callable, Mapping

from .. import procenv, util
from ..journal import Journal
from ..paths import StatePaths, unit_slug

__all__ = ["ExportSpawner"]

_UNIT_OPS = ("job:", "proposer:", "propose:", "eval:", "loop:")


class ExportSpawner:
    def __init__(self, manifest: dict, manifest_path: Path, paths: StatePaths, *, journal: Journal,
                 base_env: Mapping[str, str], package_root: Path, export_key: Callable[[], str],
                 python: str = sys.executable, clock: util.Clock | None = None) -> None:
        self.manifest = manifest
        self.manifest_path = Path(os.path.abspath(manifest_path))
        self.paths = paths
        self.journal = journal
        self.package_root = Path(os.path.abspath(package_root))
        self.export_key = export_key
        self.python = str(python)
        self.clock = clock or util.SystemClock()
        self.timeout_s = float(util.lookup(manifest, "telemetry.export_timeout_s"))
        self.base_env = dict(base_env)
        self.homes_root = paths.export_root / "homes"            # one new empty home per child under it (LD9)
        self.env = self._env(self.homes_root)                    # checked once here; each child gets its own HOME
        self.results: list[dict] = []                            # {"unit", "exit_code", "timed_out", "statuses"}
        self._queue: "queue.Queue[str | None]" = queue.Queue()
        self._cond = threading.Condition()
        self._pending = 0
        self._closed = False
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ queue

    def submit(self, unit: str) -> None:
        """Queues one unit for export; returns at once."""
        if not isinstance(unit, str) or not unit.startswith(_UNIT_OPS) or "\n" in unit or len(unit) > 256:
            raise ValueError(f"not an export unit: {str(unit)[:80]!r}")
        unit_slug(unit)                                          # ValueError for an unsafe unit
        with self._cond:
            if self._closed:
                raise RuntimeError("the export spawner is closed")
            self._pending += 1
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="mhforge-export-spawner", daemon=True)
                self._thread.start()
        self._queue.put(unit)

    def backlog(self) -> int:
        """Units queued or running."""
        with self._cond:
            return self._pending

    def drain(self, timeout_s: float | None = None) -> None:
        """Waits until every queued unit has been exported (or timeout_s real seconds have passed)."""
        with self._cond:
            self._cond.wait_for(lambda: self._pending == 0, timeout=timeout_s)

    def close(self) -> None:
        """Drains the queue, then stops the worker thread."""
        self.drain()
        with self._cond:
            self._closed = True
            thread = self._thread
        if thread is not None:
            self._queue.put(None)
            thread.join()

    # ------------------------------------------------------------ worker

    def _run(self) -> None:
        while True:
            unit = self._queue.get()
            if unit is None:
                return
            try:
                self._one(unit)
            except Exception as exc:                             # noqa: BLE001 - an export failure never fails the search
                self._journal(unit, "failed", {"exit_code": None, "statuses": {}, "error": type(exc).__name__})
            finally:
                with self._cond:
                    self._pending -= 1
                    self._cond.notify_all()

    def _journal(self, unit: str, status: str, detail: dict) -> None:
        try:
            self.journal.record("export", unit, status, detail=detail)
        except Exception:                                        # noqa: BLE001 - the receipt is the record of truth
            pass

    def _statuses(self, unit: str) -> dict:
        out = {}
        for surface in ("agent_lens", "wandb"):
            try:
                doc = util.read_json(self.paths.receipt_file(unit, surface))
            except Exception:                                    # noqa: BLE001 - absent or unreadable: not listed
                continue
            if isinstance(doc, dict):
                out[surface] = doc.get("status")
        return out

    def _env(self, home: Path) -> dict:
        return procenv.exporter_env(self.manifest, self.base_env, home=str(home), package_root=self.package_root,
                                    netrc_file=self.paths.export_netrc)

    def _ensure_netrc(self) -> None:
        path = self.paths.export_netrc
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        os.chmod(path, 0o600)

    @staticmethod
    def _remove_if_empty(home: Path) -> None:
        try:
            os.rmdir(home)                                       # only an empty directory is removed
        except OSError:
            pass

    def _new_home(self, unit: str) -> Path:
        """A new empty directory, mode 0700 (mkdtemp), under STATE/export/homes; never a symlink."""
        root = self.homes_root
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError("the export homes directory is not a plain directory")
        os.chmod(root, 0o700)
        return Path(tempfile.mkdtemp(prefix=f"{unit_slug(unit)}.", dir=str(root)))

    def _one(self, unit: str) -> None:
        self._journal(unit, "requested", {})
        self._ensure_netrc()
        key = self.export_key()
        if not isinstance(key, str) or not key or "\n" in key or "\r" in key:
            self._journal(unit, "failed", {"exit_code": None, "statuses": {}, "error": "export key missing or malformed"})
            return
        home = self._new_home(unit)
        env = self._env(home)
        read_fd, write_fd = os.pipe()
        argv = [self.python, "-m", "mhforge", "export", "--manifest", str(self.manifest_path), "--live",
                "--key-source", f"fd:{read_fd}", "--unit", unit]
        log_dir = self.paths.export_root / "children"
        log_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(log_dir / f"{unit_slug(unit)}.stderr.txt", "ab") as err, \
                    open(log_dir / f"{unit_slug(unit)}.stdout.txt", "ab") as out:
                proc = subprocess.Popen(argv, cwd=str(home), env=env, pass_fds=(read_fd,),
                                        stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                        start_new_session=True)
        except BaseException:
            os.close(write_fd)
            self._remove_if_empty(home)
            raise
        finally:
            os.close(read_fd)
        try:
            os.write(write_fd, f"export={key}\n".encode("utf-8"))
        except BrokenPipeError:
            pass                                                 # the child is already gone; its exit code says why
        finally:
            os.close(write_fd)
            key = ""
        timed_out = False
        try:
            rc = proc.wait(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                proc.kill()
            rc = proc.wait()
        self._remove_if_empty(home)                              # a home the SDK wrote into stays for inspection
        statuses = self._statuses(unit)
        detail = {"exit_code": rc, "statuses": statuses}
        if timed_out:
            detail["timed_out"] = True
        self.results.append({"unit": unit, "exit_code": rc, "timed_out": timed_out, "statuses": statuses})
        self._journal(unit, "complete" if rc == 0 and not timed_out else "failed", detail)
