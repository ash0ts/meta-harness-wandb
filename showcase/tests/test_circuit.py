# SPDX-License-Identifier: Apache-2.0
# Adapted from ash0ts/harness2-wandb@31b0f82 tests/showcase/test_transport_v4.py L287-L296
# (test_circuit_persists_failure_threshold_and_closes_after_probe). Changes: rewritten for unittest against the
# three-outcome finish, the required max_wait_s and the injected clock of spec 3.8; new cases for the probe release,
# the time-out, neutral outcomes and restarts.
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mhforge.circuit import Circuit
from mhforge.errors import StateError
from tests.helpers import FakeClock


class CircuitCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="mhf-circuit-")
        self.path = Path(self._tmp.name) / "circuit.json"
        self.clock = FakeClock()

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, cooldown_s=60.0, threshold=3):
        return Circuit(self.path, threshold=threshold, cooldown_s=cooldown_s, clock=self.clock)

    def open_circuit(self, circuit):
        for _ in range(circuit.threshold):
            circuit.finish("failure")

    def start_enter(self, circuit, max_wait_s=30.0):
        """enter() in a thread; returns (thread, results list)."""
        results = []

        def run():
            try:
                results.append(circuit.enter(max_wait_s=max_wait_s))
            except TimeoutError as exc:
                results.append(exc)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t, results

    def wait_until(self, predicate, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()


class TestCircuit(CircuitCase):
    def test_opens_probes_and_resets(self):
        circuit = self.make(cooldown_s=0)
        self.open_circuit(circuit)
        restarted = self.make(cooldown_s=0)
        self.assertEqual(restarted.state()["failures"], 3)
        restarted.enter(max_wait_s=1)
        self.assertTrue(restarted.state()["probing"])
        restarted.finish("success")
        self.assertEqual(restarted.state(), {"failures": 0, "until_epoch": 0.0, "probing": False})
        # with a cool-down: blocked until it passes, then exactly one probe; a success lets everyone through
        circuit = self.make(cooldown_s=60)
        self.open_circuit(circuit)
        t1, r1 = self.start_enter(circuit)
        time.sleep(0.1)
        self.assertEqual(r1, [])                   # open: blocked
        self.clock.advance(61)
        self.assertTrue(self.wait_until(lambda: r1))
        self.assertGreaterEqual(r1[0], 61)         # seconds waited, on the injected clock
        self.assertTrue(circuit.state()["probing"])
        t2, r2 = self.start_enter(circuit)
        time.sleep(0.1)
        self.assertEqual(r2, [])                   # a second caller waits for the probe
        circuit.finish("success")
        self.assertTrue(self.wait_until(lambda: r2))
        self.assertFalse(isinstance(r2[0], Exception))
        self.assertEqual(circuit.state()["failures"], 0)
        self.assertEqual(circuit.enter(max_wait_s=0), 0)   # closed: no wait at all

    def test_state_survives_restart(self):
        circuit = self.make()
        circuit.finish("failure")
        circuit.finish("failure")
        self.assertEqual(self.make().state(), {"failures": 2, "until_epoch": 0.0, "probing": False})
        circuit.finish("failure")
        until = circuit.state()["until_epoch"]
        self.assertEqual(until, self.clock.now() + 60)
        self.clock.advance(61)
        circuit.enter(max_wait_s=1)                # the probe flag is in memory only
        restarted = self.make()
        self.assertEqual(restarted.state(), {"failures": 3, "until_epoch": until, "probing": False})
        doc = json.loads(self.path.read_text())
        self.assertEqual(doc, {"schema": "mhforge.circuit/1", "failures": 3, "until_epoch": until})
        restarted.enter(max_wait_s=1)              # the restarted process may probe
        for bad in ('{"schema": "mhforge.circuit/2", "failures": 0, "until_epoch": 0}', '{"failures": -1}', "[]",
                    "not json", '{"failures": 3, "until_epoch": Infinity}', '{"failures": 3, "until_epoch": NaN}'):
            self.path.write_text(bad)
            with self.subTest(bad=bad), self.assertRaises(StateError):
                self.make()

    def test_enter_times_out(self):
        circuit = self.make(cooldown_s=60)
        self.open_circuit(circuit)
        with self.assertRaises(TimeoutError):
            circuit.enter(max_wait_s=0)
        t, results = self.start_enter(circuit, max_wait_s=5)
        time.sleep(0.1)
        self.assertEqual(results, [])
        self.clock.advance(6)                      # past max_wait_s, still inside the cool-down
        self.assertTrue(self.wait_until(lambda: results))
        self.assertIsInstance(results[0], TimeoutError)
        real = Circuit(Path(self._tmp.name) / "real.json", threshold=1, cooldown_s=3600)
        real.finish("failure")
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            real.enter(max_wait_s=0.2)
        self.assertLess(time.monotonic() - started, 5)

    def test_probe_refused_at_reserve_releases_probe(self):
        circuit = self.make(cooldown_s=60)
        self.open_circuit(circuit)
        self.clock.advance(61)
        circuit.enter(max_wait_s=1)                # the probe; its reservation is then refused (BudgetError)
        t, results = self.start_enter(circuit, max_wait_s=30)
        time.sleep(0.1)
        self.assertEqual(results, [])
        circuit.finish("neutral")                  # the caller's finally: no change, but the probe is released
        self.assertTrue(self.wait_until(lambda: results))
        self.assertNotIsInstance(results[0], Exception)
        self.assertTrue(circuit.state()["probing"])        # the waiter became the next probe
        self.assertEqual(circuit.state()["failures"], 3)
        circuit.finish("neutral")
        circuit.enter(max_wait_s=0)                # nothing blocks: the next caller probes at once
        circuit.finish("failure")                  # a failed probe re-arms the cool-down
        self.assertEqual(circuit.state()["until_epoch"], self.clock.now() + 60)
        with self.assertRaises(TimeoutError):
            circuit.enter(max_wait_s=0)

    def test_wall_clock_step_back_waits_one_cooldown_at_most(self):
        circuit = self.make(cooldown_s=60, threshold=1)
        circuit.finish("failure")
        self.clock.advance(61)
        self.clock._now -= 3600                    # the wall clock is stepped back an hour
        with self.assertRaises(TimeoutError):      # re-anchored: one more cool-down from now, not an hour
            circuit.enter(max_wait_s=0)
        self.assertEqual(circuit.state()["until_epoch"], self.clock.now() + 60)
        self.clock.advance(60)
        circuit.enter(max_wait_s=0)                # the probe is admitted
        self.assertTrue(circuit.state()["probing"])

    def test_neutral_keeps_count_and_cooldown(self):
        circuit = self.make()
        circuit.finish("failure")
        circuit.finish("failure")
        circuit.finish("neutral")
        self.assertEqual(circuit.state(), {"failures": 2, "until_epoch": 0.0, "probing": False})
        circuit.finish("failure")
        until = circuit.state()["until_epoch"]
        self.clock.advance(10)
        circuit.finish("neutral")
        self.assertEqual(circuit.state(), {"failures": 3, "until_epoch": until, "probing": False})
        self.assertEqual(self.make().state()["until_epoch"], until)
        with self.assertRaises(ValueError):
            circuit.finish(True)

    def test_failure_then_success_resets(self):
        circuit = self.make()
        circuit.finish("failure")
        circuit.finish("failure")
        circuit.finish("success")
        self.assertEqual(circuit.state(), {"failures": 0, "until_epoch": 0.0, "probing": False})
        self.assertEqual(json.loads(self.path.read_text())["failures"], 0)
        circuit.finish("failure")
        circuit.finish("failure")
        self.assertEqual(circuit.state()["failures"], 2)   # below the threshold: still closed
        self.assertEqual(circuit.enter(max_wait_s=0), 0)
        circuit.finish("success")
        self.assertEqual(self.make().state()["failures"], 0)


if __name__ == "__main__":
    unittest.main()
