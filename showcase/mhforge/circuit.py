# SPDX-License-Identifier: Apache-2.0
# Adapted from ash0ts/harness2-wandb@31b0f82 harness2/showcase/transport_v4.py L91-L124 (Circuit). Changes: finish takes
# one of three outcomes ("success", "failure", "neutral") and every one clears the probe flag and wakes the waiters;
# enter takes a required max_wait_s and raises TimeoutError past it; injected clock; state() accessor; the record carries
# a schema tag and is written atomically with util.write_json.
"""Persisted circuit breaker for the provider path (spec 3.8).

Blocks while failures >= threshold, admits exactly one probe after the cool-down, a success resets, and the state
survives a restart (circuit.json; the probe flag is not persisted and starts false).
"""

from __future__ import annotations

import math
import threading
from pathlib import Path

from . import util
from .errors import StateError

SCHEMA = "mhforge.circuit/1"
OUTCOMES = ("success", "failure", "neutral")
_POLL_S = 0.05                                     # real-time wait step; time itself is read from the injected clock


class Circuit:
    def __init__(self, path: Path, *, threshold: int, cooldown_s: float, clock: util.Clock | None = None) -> None:
        if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold < 1:
            raise ValueError("threshold must be a positive integer")
        if cooldown_s < 0:
            raise ValueError("cooldown_s must not be negative")
        self.path = Path(path)
        self.threshold = threshold
        self.cooldown_s = float(cooldown_s)
        self.clock = clock or util.SystemClock()
        self._cond = threading.Condition()
        self._probing = False
        self._failures = 0
        self._until = 0.0
        if self.path.exists():
            doc = util.read_json(self.path)
            if not isinstance(doc, dict):
                raise StateError(f"{self.path.name}: not an object")
            if doc.get("schema", SCHEMA) != SCHEMA:
                raise StateError(f"{self.path.name}: unsupported schema {doc.get('schema')!r}")
            failures, until = doc.get("failures", 0), doc.get("until_epoch", 0.0)
            if not isinstance(failures, int) or isinstance(failures, bool) or failures < 0 \
                    or not isinstance(until, (int, float)) or isinstance(until, bool) or not math.isfinite(until):
                raise StateError(f"{self.path.name}: malformed circuit state")
            self._failures, self._until = failures, float(until)

    def _save(self) -> None:
        util.write_json(self.path, {"schema": SCHEMA, "failures": self._failures, "until_epoch": self._until})

    def enter(self, *, max_wait_s: float) -> float:
        """Seconds waited. Blocks while the circuit is open; after the cool-down exactly one caller probes.
        TimeoutError when still blocked after max_wait_s."""
        started = self.clock.monotonic()
        with self._cond:
            while self._failures >= self.threshold:
                now = self.clock.now()
                if self._until - now > self.cooldown_s:     # the wall clock stepped back: wait one cool-down at most
                    self._until = now + self.cooldown_s
                if self._until - now <= 0 and not self._probing:
                    self._probing = True
                    break
                if self.clock.monotonic() - started >= max_wait_s:
                    raise TimeoutError("circuit stayed open past max_wait_s")
                self._cond.wait(timeout=_POLL_S)
        return self.clock.monotonic() - started

    def finish(self, outcome: str) -> None:
        if outcome not in OUTCOMES:
            raise ValueError(f"circuit outcome must be one of {OUTCOMES}")
        with self._cond:
            try:
                if outcome == "success":
                    if self._failures or self._until:
                        self._failures, self._until = 0, 0.0
                        self._save()
                elif outcome == "failure":
                    self._failures += 1
                    if self._failures >= self.threshold:
                        self._until = self.clock.now() + self.cooldown_s
                    self._save()
            finally:
                self._probing = False
                self._cond.notify_all()

    def state(self) -> dict:
        with self._cond:
            return {"failures": self._failures, "until_epoch": self._until, "probing": self._probing}
