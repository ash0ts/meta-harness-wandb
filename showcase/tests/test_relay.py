# SPDX-License-Identifier: MIT
"""Tests for ``mhforge/relay.py`` (spec 3.12, frames 2.9, named cases 5.2).

Two kinds of case. Scripted cases drive a RelaySession through ``fake_channel.ScriptedChannel`` (the test plays the
stub). Stub cases run the real ``mhf_relay_stub.py`` as a subprocess of ``sys.executable`` and talk to it through
``relay.PipeChannel`` or ``fake_channel.FaultChannel``: in stdio mode over the stub's own pipes, in daemon mode over
``bridge`` subprocesses attached to the stub's Unix socket. The worker is a small stdlib script driven by a JSON plan;
it runs on Route 1 (the stub puts its own base URL into the worker's argv), so the script reads no environment.
The gateway is ``helpers.FakeGateway``. The test process runs under ``util.loopback_guard()``.
"""

import base64
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mhforge import relay, util, wire
from mhforge.errors import ChannelClosed, RelayProtocolError
from mhforge.insandbox import mhf_frames
from mhforge.relay import PipeChannel, RelaySession
from tests import helpers
from tests.fake_channel import FaultChannel, ScriptedChannel

STUB = helpers.SC / "mhforge" / "insandbox" / "mhf_relay_stub.py"
CHAT = "/v1/chat/completions"
JOB = wire.JobIdentity(campaign="mh-test", split="val", candidate="no_memory", dataset="USPTO", attempt=1,
                       phase="baseline", iteration=None)

WORKER_SRC = r'''
import base64, hashlib, http.client, json, os, sys, threading, time, urllib.parse
plan, out, base = json.loads(sys.argv[1]), sys.argv[2], sys.argv[3]
key = "local"
state = {"results": []}
lock = threading.Lock()

def save():
    with lock:
        with open(out + ".tmp", "w") as fh:
            json.dump(state, fh)
        os.replace(out + ".tmp", out)

def post(step, i):
    u = urllib.parse.urlsplit(base)
    size = step.get("size", 10)
    pad = base64.b64encode(os.urandom(size * 3 // 4 + 1)).decode()[:size] if step.get("random") else "x" * size
    body = json.dumps({"i": i, "pad": pad}).encode()
    res = {"i": i, "sent_sha": hashlib.sha256(body).hexdigest()[:12]}
    try:
        conn = http.client.HTTPConnection(u.hostname, u.port, timeout=step.get("timeout", 60))
        conn.request("POST", u.path + "/chat/completions", body=body,
                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + key})
        r = conn.getresponse()
        data = r.read()
        conn.close()
        res.update(status=r.status, reply_sha=hashlib.sha256(data).hexdigest(), t=time.time())
        doc = json.loads(data)
        if r.status == 200:
            res["content"] = doc["choices"][0]["message"]["content"][:200]
        else:
            res["code"] = doc["error"].get("code")
    except Exception as exc:
        res["error"] = type(exc).__name__
    with lock:
        state["results"].append(res)

save()
for step in plan:
    if step["op"] == "sleep":
        time.sleep(step["s"])
    elif step["op"] == "post":
        threads = []
        for k in range(step.get("n", 1)):
            t = threading.Thread(target=post, args=(step, step.get("start", 0) + k))
            t.start()
            threads.append(t)
            if step.get("stagger"):
                time.sleep(step["stagger"])
        for t in threads:
            t.join()
    save()
save()
'''


def manifest(**relay_overrides):
    base = {"relay.ping_interval_s": 5, "relay.pong_timeout_s": 30}
    base.update({"relay." + k: v for k, v in relay_overrides.items()})
    return helpers.make_manifest(base)


def body_of(request: bytes) -> bytes:
    return wire.parse_envelope(request)[2]


def echo_content(body: bytes) -> str:
    doc = json.loads(body)
    return "echo-%s-%s" % (doc["i"], hashlib.sha256(body).hexdigest()[:12])


def echo(job, request):
    return helpers.completion_reply(echo_content(body_of(request)))


def wait_until(cond, timeout=10.0, what="condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for " + what)


def req_meta():
    return {"method": "POST", "path": CHAT}


def req_body(i):
    return json.dumps({"i": i, "pad": "x"}).encode()


class Blocking:
    """A gateway reply callable that waits for release(); records the order replies finished."""

    def __init__(self):
        self.gate = threading.Event()
        self.started = 0
        self.lock = threading.Lock()

    def __call__(self, job, request):
        with self.lock:
            self.started += 1
        self.gate.wait(30)
        return echo(job, request)


# ======================================================================================== scripted cases

class ScriptedBase(unittest.TestCase):
    def gateway(self, **kw):
        kw.setdefault("default", echo)
        gw = helpers.FakeGateway(**kw)
        gw.register_job(JOB)
        return gw

    def session(self, gw=None, clock=None, **relay_overrides):
        gw = gw if gw is not None else self.gateway()
        s = RelaySession(gw, JOB, manifest(**relay_overrides), clock=clock)
        self.addCleanup(s.close, send_bye=False)
        return s

    def channel(self, session, mode="daemon", pending=(), hello=True, **kw):
        ch = ScriptedChannel(**kw)
        bridge = session.attach(ch)
        if hello:
            ch.feed_frame("HELLO", "0", {"proto": 1, "mode": mode, "pid": 4242, "port": 5555, "pending": list(pending),
                                         "worker": {"state": "running", "returncode": None}})
            wait_until(lambda: bridge.hello is not None, what="HELLO")
        return ch, bridge


class ScriptedTests(ScriptedBase):
    def test_identity_comes_from_constructor(self):
        gw = self.gateway()
        s = self.session(gw)
        ch, b = self.channel(s, hello=False)
        ch.feed_frame("HELLO", "0", {"proto": 1, "mode": "daemon", "pid": 1, "port": 2, "pending": [],
                                     "worker": {"state": "running", "returncode": None},
                                     "job_id": "test.evil.LawBench.a9", "campaign": "other-campaign"})
        meta = dict(req_meta(), job_id="test.evil.LawBench.a9", campaign="other-campaign",
                    job={"campaign": "other-campaign", "split": "test", "candidate": "evil", "dataset": "LawBench",
                         "attempt": 9, "phase": "test", "iteration": None})
        ch.feed_frame("REQ", "r1", meta, json.dumps({"i": 1, "job_id": "test.evil.LawBench.a9"}).encode())
        res = ch.expect("RES", "r1")
        self.assertEqual(res[2]["status"], 200)
        self.assertEqual(len(gw.calls), 1)
        self.assertIs(gw.calls[0]["job"], JOB)
        self.assertEqual(gw.calls[0]["ingress"], "relay")
        self.assertIs(s.job, JOB)
        self.assertNotIn("job_id", b.hello)

    def test_pool_threads_are_daemon(self):
        seen = []

        def slow(job, request):
            seen.append(threading.current_thread())
            time.sleep(0.1)
            return echo(job, request)

        gw = self.gateway(default=slow)
        s = self.session(gw, max_workers=3)
        ch, _ = self.channel(s)
        for i in range(12):
            ch.feed_frame("REQ", "r%d" % i, req_meta(), req_body(i))
        for i in range(12):
            ch.expect("RES", "r%d" % i)
        self.assertEqual(len(seen), 12)
        self.assertTrue(all(t.daemon for t in seen))
        self.assertLessEqual(len(set(seen)), 3)
        self.assertLessEqual(gw.peak_in_flight, 3)

    def test_duplicate_in_flight_request_ignored(self):
        blk = Blocking()
        gw = self.gateway(default=blk)
        s = self.session(gw)
        ch, _ = self.channel(s)
        ch.feed_frame("REQ", "r1", req_meta(), req_body(1))
        wait_until(lambda: blk.started == 1)
        ch.feed_frame("REQ", "r1", req_meta(), req_body(1))
        wait_until(lambda: s.counters()["duplicate_requests"] == 1)
        blk.gate.set()
        ch.expect("RES", "r1")
        time.sleep(0.2)
        self.assertEqual(len(ch.of_type("RES", "r1")), 1)
        self.assertEqual(len(gw.calls), 1)

    def test_done_cache_resends_and_is_pruned_by_hello(self):
        gw = self.gateway()
        s = self.session(gw)
        ch1, _ = self.channel(s)
        ch1.feed_frame("REQ", "r1", req_meta(), req_body(1))
        ch1.feed_frame("REQ", "r2", req_meta(), req_body(2))
        first = ch1.expect("RES", "r1")
        ch1.expect("RES", "r2")
        ch1.feed_frame("REQ", "r1", req_meta(), req_body(1))         # answered already: re-answered from the cache
        again = ch1.expect("RES", "r1", n=2)
        self.assertEqual(again, first)
        self.assertEqual(len(gw.calls), 2)
        self.assertEqual(s.counters()["resent_replies"], 1)
        ch2, _ = self.channel(s, pending=["r2"])                       # r1 was delivered; r2 is still pending
        self.assertEqual(ch2.expect("RES", "r2")[3], ch1.of_type("RES", "r2")[0][3])
        self.assertEqual(s.counters()["resent_replies"], 2)
        self.assertEqual(s.counters()["done_cached"], 1)
        ch2.feed_frame("REQ", "r2", req_meta(), req_body(2))         # the stub's re-sent REQ: already re-answered
        ch2.feed_frame("REQ", "r1", req_meta(), req_body(1))         # pruned from the cache: back to the gateway
        ch2.expect("RES", "r1")
        self.assertEqual(len(gw.calls), 3)
        self.assertEqual(len(ch2.of_type("RES", "r2")), 1)
        self.assertEqual(s.counters()["duplicate_requests"], 1)

    def test_done_cache_evicts_oldest(self):
        gw = self.gateway()
        s = self.session(gw, done_cache_max=2)
        ch, _ = self.channel(s)
        for i in (1, 2, 3):
            ch.feed_frame("REQ", "r%d" % i, req_meta(), req_body(i))
            ch.expect("RES", "r%d" % i)
        self.assertEqual(s.counters()["done_cached"], 2)
        ch.feed_frame("REQ", "r3", req_meta(), req_body(3))
        ch.expect("RES", "r3", n=2)
        self.assertEqual(len(gw.calls), 3)
        ch.feed_frame("REQ", "r1", req_meta(), req_body(1))
        ch.expect("RES", "r1", n=2)
        self.assertEqual(len(gw.calls), 4)

    def test_stdio_mode_caches_nothing(self):
        gw = self.gateway()
        s = self.session(gw)
        ch, _ = self.channel(s, mode="stdio")
        ch.feed_frame("REQ", "r1", req_meta(), req_body(1))
        ch.expect("RES", "r1")
        ch.feed_frame("REQ", "r1", req_meta(), req_body(1))
        ch.expect("RES", "r1", n=2)
        c = s.counters()
        self.assertEqual(c["mode"], "stdio")
        self.assertEqual(c["done_cached"], 0)
        self.assertEqual(c["resent_replies"], 0)
        self.assertEqual(len(gw.calls), 2)

    def test_can_removes_live_request_and_flood_counts_live_only(self):
        blk = Blocking()
        gw = self.gateway(default=blk)
        s = self.session(gw, max_workers=1)                          # 4 live requests at most
        ch, _ = self.channel(s)
        for i in range(1, 5):
            ch.feed_frame("REQ", "r%d" % i, req_meta(), req_body(i))
        wait_until(lambda: s.counters()["live"] == 4)
        ch.feed_frame("CAN", "r1")
        ch.feed_frame("CAN", "r2")
        wait_until(lambda: s.counters()["cancelled"] == 2)
        c = s.counters()
        self.assertEqual((c["live"], c["in_flight"]), (2, 4))
        ch.feed_frame("REQ", "r5", req_meta(), req_body(5))
        ch.feed_frame("REQ", "r6", req_meta(), req_body(6))
        wait_until(lambda: s.counters()["live"] == 4)
        self.assertEqual(s.counters()["flood_refusals"], 0)
        ch.feed_frame("REQ", "r7", req_meta(), req_body(7))
        self.assertEqual(ch.expect("RES", "r7")[2]["status"], 403)
        self.assertEqual(s.counters()["flood_refusals"], 1)
        blk.gate.set()
        for i in range(3, 7):
            self.assertEqual(ch.expect("RES", "r%d" % i)[2]["status"], 200)
        wait_until(lambda: s.counters()["in_flight"] == 0)
        self.assertEqual(len(gw.calls), 6)                            # cancelled calls are left to finish
        self.assertNotIn(7, [json.loads(c["body"])["i"] for c in gw.calls])

    def test_hello_cancels_requests_missing_from_pending(self):
        blk = Blocking()
        gw = self.gateway(default=blk)
        s = self.session(gw, max_workers=1)
        ch1, _ = self.channel(s)
        for i in (1, 2, 3):
            ch1.feed_frame("REQ", "r%d" % i, req_meta(), req_body(i))
        wait_until(lambda: s.counters()["live"] == 3)
        ch1.end()
        self.channel(s, pending=["r2"])                               # r1 and r3 were dropped while no uplink existed
        wait_until(lambda: s.counters()["live"] == 1)
        self.assertEqual(s.counters()["cancelled"], 2)
        blk.gate.set()

    def test_stale_hello_from_an_older_bridge_is_ignored(self):
        blk = Blocking()
        gw = self.gateway(default=blk)
        s = self.session(gw)
        ch1, b1 = self.channel(s, hello=False)
        ch2, _ = self.channel(s)
        ch2.feed_frame("REQ", "r1", req_meta(), req_body(1))
        wait_until(lambda: s.counters()["live"] == 1)
        ch1.feed_frame("HELLO", "0", {"proto": 1, "mode": "daemon", "pid": 1, "port": 2, "pending": [],
                                      "worker": {"state": "running", "returncode": None}})
        wait_until(lambda: b1.hello is not None)
        self.assertEqual(s.counters()["live"], 1)
        blk.gate.set()
        ch2.expect("RES", "r1")

    def test_flood_refusal_is_relay_overloaded(self):
        blk = Blocking()
        gw = self.gateway(default=blk)
        s = self.session(gw, max_workers=1)
        ch, _ = self.channel(s)
        for i in range(1, 6):
            ch.feed_frame("REQ", "r%d" % i, req_meta(), req_body(i))
        res = ch.expect("RES", "r5")
        expected = wire.refusal_reply("relay_overloaded")
        self.assertEqual(res[2], {"status": expected.status, "headers": [list(h) for h in expected.headers]})
        self.assertEqual(res[3], expected.body)
        self.assertEqual(json.loads(res[3])["error"]["code"], "relay_overloaded")
        helpers.assert_neutral(wire.GatewayReply(res[2]["status"], tuple(tuple(h) for h in res[2]["headers"]), res[3]))
        self.assertEqual(s.counters()["flood_refusals"], 1)
        blk.gate.set()
        wait_until(lambda: s.counters()["in_flight"] == 0)
        self.assertEqual(sorted(json.loads(c["body"])["i"] for c in gw.calls), [1, 2, 3, 4])

    def test_send_kill_without_a_live_bridge_is_false(self):
        s = self.session()
        self.assertFalse(s.send_kill("refusal_flood"))
        ch, b = self.channel(s)
        self.assertTrue(s.send_kill("refusal_flood"))
        self.assertEqual(ch.expect("KILL")[2], {"reason": "refusal_flood"})
        ch.end()
        wait_until(lambda: not b.alive)
        self.assertFalse(s.send_kill("refusal_flood"))

    def test_total_chars_in_across_bridges(self):
        s = self.session()
        ch1, b1 = self.channel(s)
        ch1.feed("noise-1 ")
        ch2, b2 = self.channel(s)
        ch2.feed("more noise �")
        hello_len = len(mhf_frames.encode_frame("HELLO", "0", {"proto": 1, "mode": "daemon", "pid": 4242, "port": 5555,
                                                               "pending": [], "worker": {"state": "running",
                                                                                         "returncode": None}}))
        want1, want2 = hello_len + len("noise-1 "), hello_len + len("more noise �")
        wait_until(lambda: (b1.chars_in, b2.chars_in) == (want1, want2))
        ch1.end()
        wait_until(lambda: not b1.alive)
        self.assertEqual(s.total_chars_in(), want1 + want2)
        c = s.counters()
        self.assertEqual(c["chars_in"], want1 + want2)
        self.assertEqual(c["noise_chars"], len("noise-1 ") + len("more noise �"))

    def test_rotation_counted(self):
        s = self.session()
        ch1, b1 = self.channel(s)
        ch2, b2 = self.channel(s)
        s.note_rotation()
        b1.detach()
        c = s.counters()
        self.assertEqual((c["attaches"], c["rotations"]), (2, 1))
        self.assertFalse(b1.alive)
        self.assertEqual(b1.dead_reason, "detached")
        self.assertTrue(ch1.closed.is_set())
        self.assertIs(s.current, b2)

    def test_eight_frame_errors_kill_the_bridge(self):
        s = self.session()
        ch, b = self.channel(s)
        good = mhf_frames.encode_frame("LOG", "0", {"level": "info", "msg": "hello"}).decode("ascii")
        head, payload = good.split("\n", 1)
        bad = head[:-8] + ("0" * 8 if not head.endswith("0" * 8) else "1" * 8) + "\n" + payload
        for k in range(7):
            ch.feed(bad)
        wait_until(lambda: s.counters()["frame_errors"] == 7)
        time.sleep(0.1)
        self.assertTrue(b.alive)
        ch.feed(bad)
        wait_until(lambda: not b.alive)
        self.assertEqual(b.dead_reason, "frame_errors")
        self.assertEqual(s.counters()["logs"], 0)

    def test_hostile_frames_do_not_crash_the_session(self):
        gw = self.gateway()
        s = self.session(gw)
        ch, b = self.channel(s)
        hostile_meta = [
            ("HELLO", "0", {"proto": 2, "mode": "daemon", "pending": []}),
            ("HELLO", "0", {"proto": 1, "mode": "daemon", "pid": "x", "port": 1, "pending": [1, 2], "worker": {}}),
            ("HELLO", "0", {"proto": 1, "mode": "both", "pid": 1, "port": 1, "pending": "r1", "worker": None}),
            ("RES", "r1", {"status": 200, "headers": []}), ("PING", "0", {"t": "x"}), ("KILL", "0", {}),
            ("BYE", "0", {}), ("CAN", "r999", {}), ("PONG", "0", {"t": [1]}),
            ("REQ", "r10", {"method": 5, "path": CHAT}), ("REQ", "r11", {"path": "relative"}),
            ("REQ", "r12", {"method": "POST", "path": "/x\ny"}), ("REQ", "r13", {}),
            ("LOG", "0", {"level": 7, "msg": ["x"]}), ("LOG", "0", {"level": "warn", "msg": "y" * 100000}),
            ("EXIT", "0", {"returncode": "zero", "reason": None, "timed_out": "no", "local_refusals": ["a"],
                           "unanswered_at_exit": -3}),
            ("EXIT", "0", {"returncode": 0, "reason": "exit", "timed_out": False, "local_refusals": {},
                           "unanswered_at_exit": 0}),
        ]
        for ftype, rid, meta in hostile_meta:
            ch.feed_frame(ftype, rid, meta, b"\x00\xff" * 3)
        for _ in range(60):
            ch.feed_frame("LOG", "0", {"level": "info", "msg": "m"})
        ch.feed("garbage \x00\x7f� #MHF #MHF1 #MHF1 REQ \n" + "é中" * 50 + "#MHF1 REQ r1 - 9 8 zzzzzzzz\n")
        ch.feed("#MHF1 REQ r1 z 10 8 0000abcd\nAAAA$AAA\n")
        ch.feed_frame("REQ", "r20", req_meta(), req_body(20))
        self.assertEqual(ch.expect("RES", "r20")[2]["status"], 200)
        self.assertTrue(b.alive)
        for rid in ("r10", "r11", "r12", "r13"):
            res = ch.expect("RES", rid)
            self.assertEqual(json.loads(res[3])["error"]["code"], "invalid_request")
        self.assertEqual(b.hello["proto"], 1)                          # the valid HELLO of the attach stands
        c = s.counters()
        self.assertEqual(c["logs"], 62)
        self.assertEqual(len(s.log_lines), relay.LOG_KEEP)
        self.assertTrue(all(len(x["msg"]) <= relay.LOG_MSG_CHARS for x in s.log_lines))
        self.assertGreater(c["noise_chars"], 0)
        self.assertLess(c["frame_errors"], relay.FRAME_ERROR_LIMIT)
        ex = s.wait_exit(1.0)                                         # the first EXIT, type-checked into its worst
        self.assertEqual((ex["returncode"], ex["reason"], ex["timed_out"], ex["local_refusals"]),
                         (None, "invalid", False, {}))
        self.assertGreaterEqual(ex["unanswered_at_exit"], 1)
        self.assertFalse(ex["valid"])
        self.assertEqual(len(gw.calls), 1)

    def test_end_of_stream_marks_bridge_dead(self):
        blk = Blocking()
        gw = self.gateway(default=blk)
        s = self.session(gw)
        ch1, b1 = self.channel(s)
        ch1.feed_frame("REQ", "r1", req_meta(), req_body(1))
        wait_until(lambda: blk.started == 1)
        ch1.end()
        wait_until(lambda: not b1.alive)
        self.assertEqual(b1.dead_reason, "eof")
        self.assertFalse(s.send_kill("x"))
        blk.gate.set()                                                # the gateway call keeps running
        wait_until(lambda: s.counters()["done_cached"] == 1)
        ch2, _ = self.channel(s, pending=["r1"])
        self.assertEqual(ch2.expect("RES", "r1")[2]["status"], 200)
        self.assertEqual(len(gw.calls), 1)

    def test_reader_threads_never_write(self):
        gw = self.gateway()
        s = self.session(gw, ping_interval_s=0.05, pong_timeout_s=30)
        ch, b = self.channel(s, write_timeout_s=20)
        ch.hold_writes()                                              # the far side stops reading
        ch.feed_frame("REQ", "r1", req_meta(), req_body(1))
        wait_until(lambda: ch.blocked_writes >= 1, what="a blocked write")
        for k in range(20):
            ch.feed_frame("LOG", "0", {"level": "info", "msg": "line %d" % k})
        for i in (2, 3):
            ch.feed_frame("REQ", "r%d" % i, req_meta(), req_body(i))
        wait_until(lambda: s.counters()["logs"] == 20 and len(gw.calls) == 3, what="reader progress")
        wait_until(lambda: s.counters()["pings"] >= 3, what="PINGs queued behind the blocked write")
        self.assertTrue(b.alive)
        ch.release_writes()
        for i in (1, 2, 3):
            ch.expect("RES", "r%d" % i)
        ch.expect("PING")
        self.assertTrue(ch.read_threads)
        self.assertFalse(ch.read_threads & ch.write_threads)
        self.assertTrue(all(t.name == "mhf-relay-writer" for t in ch.write_threads))

    def test_each_bridge_has_its_own_decoder_scripted(self):
        s = self.session()
        ch1, b1 = self.channel(s)
        big = mhf_frames.encode_frame("REQ", "r1", req_meta(), os.urandom(60000), compress=False).decode("ascii")
        ch1.feed(big[: len(big) // 3])
        ch1.end()
        wait_until(lambda: not b1.alive)
        ch2, b2 = self.channel(s)                                     # waits for b2.hello
        self.assertIsNot(b1.decoder, b2.decoder)
        b1.reader_thread.join(5)
        self.assertGreater(b1.decoder.noise_chars, 1000)              # the cut frame was discarded by bridge 1's decoder
        self.assertEqual(b2.decoder.noise_chars, 0)
        self.assertEqual(b2.hello["mode"], "daemon")

    def test_dead_bridge_releases_its_partial_frame(self):
        # 3.12 host memory: one decoder with at most one partial frame per LIVE bridge; a dead one keeps only counts.
        s = self.session()
        for k in range(3):
            ch, b = self.channel(s)
            big = mhf_frames.encode_frame("REQ", "r%d" % (k + 1), req_meta(), os.urandom(300000),
                                          compress=False).decode("ascii")
            ch.feed(big[: len(big) // 2])
            wait_until(lambda: b.chars_in >= len(big) // 2)
            ch.end()
            wait_until(lambda: not b.alive)
            b.reader_thread.join(5)
            self.assertEqual(b.decoder.pending_chars, 0)
            self.assertGreaterEqual(b.decoder.noise_chars, len(big) // 2 - 64)
        self.assertGreaterEqual(s.counters()["noise_chars"], 3 * (len(big) // 2 - 64))

    def test_ping_not_starved_by_reply_backlog(self):
        # The host's own write backlog must not hold back the PING, or the far side cannot PONG and the bridge is
        # declared silent while the host is still writing (2.9: a PING every interval whatever other traffic there is).
        class SlowChannel(ScriptedChannel):
            def write(self, data):
                time.sleep(0.01)
                before = len(self.of_type("PING"))
                super().write(data)
                for _ in range(len(self.of_type("PING")) - before):
                    self.feed_frame("PONG", "0", {"t": 1.0})

        body = os.urandom(200000)
        gw = self.gateway(default=lambda job, req: wire.GatewayReply(200, (("content-type", "application/json"),),
                                                                     body))
        s = self.session(gw, write_chunk_bytes=10000, ping_interval_s=0.1, pong_timeout_s=0.6)
        ch = SlowChannel()
        b = s.attach(ch)
        ch.feed_frame("HELLO", "0", {"proto": 1, "mode": "daemon", "pid": 1, "port": 2, "pending": [],
                                     "worker": {"state": "running", "returncode": None}})
        wait_until(lambda: b.hello is not None)
        for i in range(1, 11):                                        # about 2 s of writes, over three pong timeouts
            ch.feed_frame("REQ", "r%d" % i, req_meta(), req_body(i))
        for i in range(1, 11):
            ch.expect("RES", "r%d" % i, timeout=20)
        self.assertTrue(b.alive, b.dead_reason)
        self.assertEqual(len(gw.calls), 10)

    def test_write_failure_keeps_res_for_next_bridge_in_daemon_mode(self):
        gw = self.gateway()
        s = self.session(gw)
        ch1, b1 = self.channel(s, write_timeout_s=0.3)
        ch1.hold_writes()
        ch1.feed_frame("REQ", "r1", req_meta(), req_body(1))
        wait_until(lambda: not b1.alive, what="write timeout")
        self.assertEqual(b1.dead_reason, "write")
        ch2, _ = self.channel(s, pending=["r1"])
        self.assertEqual(ch2.expect("RES", "r1")[2]["status"], 200)
        self.assertEqual(len(gw.calls), 1)

    def test_write_slices_and_set_write_timeout(self):
        big = helpers.completion_reply("z" * 200000)
        gw = self.gateway(default=lambda job, req: wire.GatewayReply(200, big.headers, os.urandom(200000)))
        s = self.session(gw, write_chunk_bytes=1000, pong_timeout_s=7)
        ch, _ = self.channel(s)
        self.assertEqual(ch.write_timeout_s, 7.0)
        ch.feed_frame("REQ", "r1", req_meta(), req_body(1))
        ch.expect("RES", "r1")
        self.assertLessEqual(max(ch.write_sizes), 1000)
        self.assertGreater(len(ch.write_sizes), 200)

    def test_gateway_exception_answers_gateway_internal(self):
        gw = self.gateway(replies=[RuntimeError("boom")])
        s = self.session(gw)
        ch, _ = self.channel(s)
        ch.feed_frame("REQ", "r1", req_meta(), req_body(1))
        res = ch.expect("RES", "r1")
        self.assertEqual(json.loads(res[3])["error"]["code"], "gateway_internal")
        self.assertEqual(s.counters()["gateway_errors"], 1)

    def test_close_sends_bye_and_refuses_attach(self):
        s = self.session()
        ch, b = self.channel(s)
        s.close()
        ch.expect("BYE")
        self.assertFalse(b.alive)
        self.assertTrue(ch.closed.is_set())
        with self.assertRaises(RelayProtocolError):
            s.attach(ScriptedChannel())

    def test_silent_bridge_marked_dead_with_fake_clock(self):
        clock = helpers.FakeClock()
        s = self.session(clock=clock, ping_interval_s=1, pong_timeout_s=3)
        ch, b = self.channel(s)
        clock.advance(2.5)
        time.sleep(0.3)
        self.assertTrue(b.alive)
        ch.feed_frame("PONG", "0", {"t": 1.0})
        wait_until(lambda: s.counters()["pongs"] == 1)
        clock.advance(2.5)
        time.sleep(0.3)
        self.assertTrue(b.alive)                                      # the PONG refreshed it
        clock.advance(1.0)
        wait_until(lambda: not b.alive)
        self.assertEqual(b.dead_reason, "silent")
        self.assertGreater(s.counters()["pings"], 0)


# ======================================================================================== pipe channel

class PipeChannelTests(unittest.TestCase):
    def child(self, code):
        proc = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, bufsize=0, env=helpers.child_env())
        self.addCleanup(proc.wait, 10)
        self.addCleanup(proc.kill)
        return proc

    def drain(self, ch):
        parts = []
        while True:
            t = ch.read_text()
            if t is None:
                return "".join(parts)
            parts.append(t)

    def test_pipe_channel_mimics_sdk_decoding_and_counts_retained_bytes(self):
        frame = mhf_frames.encode_frame("LOG", "0", {"level": "info", "msg": "café"}, b"\xc3\xa9")
        payload = b"ab\xc3\xa9cd" + frame
        code = "import os,sys; os.write(1, bytes.fromhex(sys.stdin.readline().strip()))"
        proc = self.child(code)
        ch = PipeChannel(proc, chunk=1)
        ch.write(payload.hex().encode() + b"\n")
        text = self.drain(ch)
        self.assertEqual(text[:6], "ab��cd")                # each byte decoded alone, with replacement
        self.assertEqual(ch.retained_bytes, len(payload))
        frames = mhf_frames.FrameDecoder().feed(text)
        self.assertEqual(frames, [("LOG", "0", {"level": "info", "msg": "café"}, b"\xc3\xa9")])
        ch.close()
        proc2 = self.child(code)
        ch2 = PipeChannel(proc2, mimic_sdk=False, chunk=1)
        ch2.write(payload.hex().encode() + b"\n")
        text2 = self.drain(ch2)
        self.assertEqual(text2[:5], "abécd")
        self.assertEqual(ch2.retained_bytes, 0)
        ch2.close()

    def test_pipe_channel_large_write_and_closed_far_end(self):
        proc = self.child("import os\nwhile True:\n d=os.read(0,65536)\n if not d: break\n os.write(1,d)\n")
        ch = PipeChannel(proc)
        data = os.urandom(3 * 1024 * 1024)
        got = bytearray()

        def reader():
            while True:
                t = ch.read_text()
                if t is None:
                    return
                got.extend(t.encode("utf-8", "surrogatepass"))

        base = base64.b64encode(data)
        th = threading.Thread(target=reader, daemon=True)
        th.start()
        ch.write(base)
        proc.stdin.close()
        th.join(20)
        self.assertEqual(bytes(got), base)
        proc.wait(10)
        with self.assertRaises(ChannelClosed):
            ch.write(b"after the far end is gone")
        ch.close()
        ch.close()


# ======================================================================================== real stub

class StubBase(unittest.TestCase):
    def setUp(self):
        guard = util.loopback_guard()
        guard.__enter__()
        self.addCleanup(guard.__exit__, None, None, None)
        self.tmp = Path(tempfile.mkdtemp(prefix="mhf-", dir="/tmp"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.procs = []
        self.addCleanup(self._kill_all)
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()
        self.out = self.tmp / "out.json"
        self.worker = self.tmp / "worker.py"
        self.worker.write_text(WORKER_SRC, encoding="utf-8")

    def _kill_all(self):
        for proc in self.procs:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait(5)
            for stream in (proc.stdin, proc.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass

    def gateway(self, default=echo):
        gw = helpers.FakeGateway(default=default)
        gw.register_job(JOB)
        return gw

    def session(self, gw, **relay_overrides):
        s = RelaySession(gw, JOB, manifest(**relay_overrides))
        self.addCleanup(s.close, send_bye=False)
        return s

    def job(self, plan, *, ping=60, pong=120, timeout_s=60):
        job = {"schema": "mhforge.jobfile/1", "job_id": JOB.job_id,
               "argv": [sys.executable, str(self.worker), json.dumps(plan), str(self.out), "{api_base}"],
               "env": {"PATH": "/usr/bin:/bin", "HOME": str(self.tmp), "TMPDIR": str(self.tmp),
                       "PYTHONDONTWRITEBYTECODE": "1", "LANG": "C.UTF-8", "NO_PROXY": "127.0.0.1,localhost"},
               "cwd": str(self.tmp), "route": 1, "port": 0, "timeout_s": timeout_s, "reply_timeout_s": 60,
               "ping_interval_s": ping, "pong_timeout_s": pong, "max_request_bytes": 4000000,
               "run_dir": str(self.run_dir), "expect_outputs": [], "vocab_file": None, "repeat": None, "sample": None,
               "probe_endpoints": [], "expect": {"machine": None, "python": None, "freeze_sha256": None}}
        path = self.tmp / "job.json"
        path.write_text(json.dumps(job), encoding="utf-8")
        return path

    def popen(self, args, stdin=subprocess.PIPE, stdout=subprocess.PIPE):
        proc = subprocess.Popen([sys.executable, str(STUB), *args], stdin=stdin, stdout=stdout,
                                stderr=subprocess.DEVNULL, bufsize=0, env=helpers.child_env(),
                                start_new_session=True)
        self.procs.append(proc)
        return proc

    def start_stdio(self, plan, **job_kw):
        return self.popen(["serve", "--job", str(self.job(plan, **job_kw)), "--uplink", "stdio"])

    def sock(self):
        return "unix:%s" % (self.tmp / "u.sock")

    def start_daemon(self, plan, **job_kw):
        proc = self.popen(["serve", "--job", str(self.job(plan, **job_kw)), "--uplink", self.sock(), "--linger", "5"],
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
        wait_until(lambda: (self.run_dir / "stub.ready").exists(), 20, "stub.ready")
        return proc

    def start_bridge(self):
        return self.popen(["bridge", "--uplink", self.sock()])

    def results(self):
        return json.loads(self.out.read_text())["results"]

    def assert_echoed(self, results, n):
        self.assertEqual(sorted(r["i"] for r in results), list(range(n)))
        for r in results:
            self.assertEqual(r.get("status"), 200, r)
            self.assertTrue(r["content"].startswith("echo-%d-%s" % (r["i"], r["sent_sha"])), r)

    def run_daemon(self, session, first_channel, done, *, rotate_every=None, max_bridges=50, deadline_s=60):
        """Play the executor in daemon mode: reattach a dead bridge, rotate on a timer, until done() or the deadline."""
        bridge = session.attach(first_channel)
        last = time.monotonic()
        end = time.monotonic() + deadline_s
        while not done():
            if time.monotonic() > end:
                self.fail("daemon run did not finish; counters %s" % session.counters())
            rotate = rotate_every is not None and time.monotonic() - last >= rotate_every
            if not bridge.alive or rotate:
                self.assertLess(session.counters()["attaches"], max_bridges)
                old = bridge
                bridge = session.attach(PipeChannel(self.start_bridge()))
                if rotate and old.alive:
                    session.note_rotation()
                    old.detach()
                last = time.monotonic()
            time.sleep(0.02)
        return bridge


class StdioStubTests(StubBase):
    def test_one_request_through_stub_and_session(self):
        gw = self.gateway()
        s = self.session(gw)
        b = s.attach(PipeChannel(self.start_stdio([{"op": "post", "n": 1, "size": 50}])))
        ex = s.wait_exit(30)
        self.assertEqual((ex["returncode"], ex["reason"], ex["unanswered_at_exit"]), (0, "exit", 0))
        self.assert_echoed(self.results(), 1)
        self.assertEqual(len(gw.calls), 1)
        call = gw.calls[0]
        self.assertIs(call["job"], JOB)
        self.assertEqual((call["ingress"], call["method"], call["path"]), ("relay", "POST", CHAT))
        self.assertEqual(json.loads(call["body"])["pad"], "x" * 50)
        self.assertEqual(b.hello["mode"], "stdio")
        self.assertEqual(s.counters()["mode"], "stdio")
        wait_until(lambda: not b.alive)                               # the stub exits after EXIT: end of stream
        self.assertIn(b.dead_reason, ("eof", "write"))

    def test_32_interleaved_100kb_requests_over_one_pipe(self):
        gw = self.gateway()
        s = self.session(gw)
        b = s.attach(PipeChannel(self.start_stdio([{"op": "post", "n": 32, "size": 100000, "random": True}])))
        ex = s.wait_exit(60)
        self.assertEqual(ex["returncode"], 0)
        self.assert_echoed(self.results(), 32)
        self.assertEqual(len(gw.calls), 32)
        c = s.counters()
        self.assertGreaterEqual(c["frames_in"], 34)
        self.assertEqual(c["frame_errors"], 0)
        self.assertGreater(b.chars_in, 32 * 100000)

    def test_out_of_order_replies(self):
        finished = []

        def delayed(job, request):
            i = json.loads(body_of(request))["i"]
            time.sleep(0.15 * (7 - i))
            finished.append(i)
            return echo(job, request)

        gw = self.gateway(delayed)
        s = self.session(gw)
        s.attach(PipeChannel(self.start_stdio([{"op": "post", "n": 8, "stagger": 0.02}])))
        self.assertEqual(s.wait_exit(30)["returncode"], 0)
        self.assert_echoed(self.results(), 8)
        self.assertEqual(finished[:3], [7, 6, 5])
        arrival = [r["i"] for r in sorted(self.results(), key=lambda r: r["t"])]
        self.assertNotEqual(arrival, sorted(arrival))

    def test_flooding_job_gets_refusals_without_forwarding(self):
        blk = Blocking()
        gw = self.gateway(blk)
        s = self.session(gw, max_workers=1)
        s.attach(PipeChannel(self.start_stdio([{"op": "post", "n": 20}])))
        wait_until(lambda: s.counters()["flood_refusals"] == 16, 20, "16 refusals")
        blk.gate.set()
        self.assertEqual(s.wait_exit(30)["returncode"], 0)
        res = self.results()
        ok = [r for r in res if r.get("status") == 200]
        refused = [r for r in res if r.get("status") == 403]
        self.assertEqual((len(ok), len(refused)), (4, 16))
        self.assertTrue(all(r["code"] == "relay_overloaded" for r in refused))
        self.assertEqual(len(gw.calls), 4)
        self.assertEqual(sorted(json.loads(c["body"])["i"] for c in gw.calls), sorted(r["i"] for r in ok))

    def test_send_kill(self):
        s = self.session(self.gateway())
        b = s.attach(PipeChannel(self.start_stdio([{"op": "sleep", "s": 30}])))
        wait_until(lambda: b.hello is not None)
        self.assertTrue(s.send_kill("refusal_flood"))
        ex = s.wait_exit(20)
        self.assertEqual(ex["reason"], "killed")
        self.assertEqual(json.loads((self.run_dir / "exit.json").read_text())["reason"], "killed")
        wait_until(lambda: not b.alive)
        self.assertFalse(s.send_kill("again"))

    def test_ping_pong(self):
        # The stub's own stdio watchdog (3 * 0.2 + 0.8 = 1.4 s) would kill a worker sleeping 2.5 s if no PING arrived,
        # and the host would mark the bridge silent after 0.8 s if no PONG came back.
        s = self.session(self.gateway(), ping_interval_s=0.2, pong_timeout_s=0.8)
        b = s.attach(PipeChannel(self.start_stdio([{"op": "sleep", "s": 2.5}], ping=0.2, pong=0.8)))
        ex = s.wait_exit(20)
        self.assertEqual((ex["returncode"], ex["reason"]), (0, "exit"))
        c = s.counters()
        self.assertGreaterEqual(c["pings"], 8)
        self.assertGreaterEqual(c["pongs"], 6)
        self.assertNotEqual(b.dead_reason, "silent")

    def test_backpressure_slow_reader(self):
        bodies = {}

        def big(job, request):
            i = json.loads(body_of(request))["i"]
            body = helpers.completion_body(base64.b64encode(os.urandom(150000)).decode())
            bodies[i] = hashlib.sha256(body).hexdigest()
            return wire.GatewayReply(200, (("content-type", "application/json"),), body)

        s = self.session(self.gateway(big), write_chunk_bytes=4096)
        ch = FaultChannel(self.start_stdio([{"op": "post", "n": 6}]), seed=3, max_chunk=1500, write_delay_s=0.002)
        s.attach(ch)
        self.assertEqual(s.wait_exit(60)["returncode"], 0)
        res = self.results()
        self.assertEqual(len(res), 6)
        for r in res:
            self.assertEqual((r["status"], r["reply_sha"]), (200, bodies[r["i"]]))
        self.assertLessEqual(max(ch.write_sizes), 4096)
        self.assertGreater(len(ch.write_sizes), 6 * 30)
        self.assertEqual(ch.read_threads & ch.write_threads, set())

    def test_fault_channel_rechunking_and_noise(self):
        gw = self.gateway()
        s = self.session(gw)
        ch = FaultChannel(self.start_stdio([{"op": "post", "n": 5, "size": 5000, "random": True}]), seed=11,
                          max_chunk=7, noise_rate=1.0)
        b = s.attach(ch)
        self.assertEqual(s.wait_exit(30)["returncode"], 0)
        self.assert_echoed(self.results(), 5)
        self.assertGreater(ch.noise_injected, 5)
        wait_until(lambda: not b.alive)
        c = s.counters()
        self.assertGreater(c["noise_chars"], 0)
        self.assertEqual(c["frame_errors"], 0)
        self.assertGreaterEqual(ch.retained_bytes, b.chars_in)       # one byte decodes to at most one character
        self.assertEqual(s.total_chars_in(), b.chars_in)


class DaemonStubTests(StubBase):
    def test_bridge_killed_mid_request_completes_with_one_gateway_call(self):
        self.start_daemon([{"op": "sleep", "s": 0.3}, {"op": "post", "n": 1}])
        first_proc = self.start_bridge()

        def kill_bridge(job, request):
            os.killpg(first_proc.pid, signal.SIGKILL)                 # the exec stream dies with the call in flight
            time.sleep(0.5)
            return echo(job, request)

        gw = self.gateway(kill_bridge)
        s = self.session(gw)
        self.run_daemon(s, PipeChannel(first_proc), lambda: self.out.exists() and len(self.results()) == 1)
        self.assert_echoed(self.results(), 1)
        self.assertEqual(len(gw.calls), 1)
        self.assertGreaterEqual(s.counters()["attaches"], 2)
        self.assertIn(s.bridges[0].dead_reason, ("eof", "write"))

    def test_rotation_loses_nothing(self):
        def slow(job, request):
            time.sleep(0.05)
            return echo(job, request)

        self.start_daemon([{"op": "post", "n": 30, "stagger": 0.04, "size": 3000, "random": True}])
        gw = self.gateway(slow)
        s = self.session(gw)
        self.run_daemon(s, PipeChannel(self.start_bridge()),
                        lambda: (self.run_dir / "exit.json").exists() and s.wait_exit(0) is not None,
                        rotate_every=0.25)
        self.assert_echoed(self.results(), 30)
        self.assertEqual(len(gw.calls), 30)
        c = s.counters()
        self.assertGreaterEqual(c["rotations"], 3)
        self.assertEqual(c["attaches"], c["rotations"] + 1)

    def test_silent_stall_detected_and_reattached(self):
        self.start_daemon([{"op": "post", "n": 1}])
        fc = FaultChannel(self.start_bridge())

        def stall_then_answer(job, request):
            fc.stall()                                                # the stream goes quiet; the RES is swallowed
            time.sleep(0.2)
            return echo(job, request)

        gw = self.gateway(stall_then_answer)
        s = self.session(gw, ping_interval_s=0.2, pong_timeout_s=1.0)
        t0 = time.monotonic()
        self.run_daemon(s, fc, lambda: self.out.exists() and len(self.results()) == 1)
        first = s.bridges[0]
        self.assertEqual(first.dead_reason, "silent")
        self.assertGreaterEqual(time.monotonic() - t0, 1.0)
        self.assertGreater(fc.swallowed, 0)
        self.assert_echoed(self.results(), 1)
        self.assertEqual(len(gw.calls), 1)                            # the second bridge got it from the done cache
        self.assertGreaterEqual(s.counters()["resent_replies"], 1)

    def test_write_timeout_marks_bridge_dead(self):
        self.start_daemon([{"op": "post", "n": 1}])
        fc = FaultChannel(self.start_bridge(), write_timeout_s=0.5)

        def block_then_answer(job, request):
            fc.block_writes = True
            return echo(job, request)

        gw = self.gateway(block_then_answer)
        s = self.session(gw, ping_interval_s=5, pong_timeout_s=30)
        self.run_daemon(s, fc, lambda: self.out.exists() and len(self.results()) == 1, deadline_s=20)
        self.assertEqual(s.bridges[0].dead_reason, "write")
        self.assert_echoed(self.results(), 1)
        self.assertEqual(len(gw.calls), 1)

    def test_each_bridge_has_its_own_decoder(self):
        # Bridge 1 delivers the HELLO, then half of a large REQ frame, then drops. With one decoder per session the
        # cut frame would swallow bridge 2's HELLO; with one per bridge the request completes on bridge 2.
        self.start_daemon([{"op": "sleep", "s": 0.3}, {"op": "post", "n": 1, "size": 400000, "random": True}])
        fc = FaultChannel(self.start_bridge(), drop_after_frames=1, cut_fraction=0.5, seed=5, max_chunk=50000)
        gw = self.gateway()
        s = self.session(gw)
        self.run_daemon(s, fc, lambda: self.out.exists() and len(self.results()) == 1)
        b1, b2 = s.bridges[0], s.bridges[1]
        self.assertTrue(fc.dropped)
        self.assertEqual(b1.dead_reason, "eof")
        b1.reader_thread.join(5)
        self.assertGreater(b1.decoder.noise_chars, 100000)            # the cut frame, discarded by bridge 1's decoder
        self.assertEqual(b1.decoder.pending_chars, 0)
        self.assertIsNotNone(b2.hello)
        self.assertEqual(b2.hello["pending"], ["r1"])
        self.assert_echoed(self.results(), 1)
        self.assertEqual(len(gw.calls), 1)


if __name__ == "__main__":
    unittest.main()
