# SPDX-License-Identifier: MIT
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from mhforge import wire
from mhforge.errors import StateError
from mhforge.replay import ReplayStore, request_sha256, reuse_key, zero_usage
from mhforge.wire import GatewayReply, JobIdentity
from tests.helpers import FakeClock, completion_body, completion_reply


def J(attempt=1, phase="search", candidate="cand_a", dataset="USPTO", repeat=None, sample=None):
    return JobIdentity("mh-test", "val", candidate, dataset, attempt, phase, 1 if phase == "search" else None,
                       repeat, sample)


ADMITTED = {"model": "openai/gpt-oss-120b", "temperature": 0.0, "max_tokens": 16384,
            "messages": [{"role": "system", "content": "Reasoning: medium"}, {"role": "user", "content": "Q?"}]}
DIGEST = request_sha256(ADMITTED)


class ReplayCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="mhf-replay-")
        self.path = Path(self._tmp.name) / "replay.sqlite"
        self.clock = FakeClock()

    def tearDown(self):
        self._tmp.cleanup()

    def store(self, scope="search"):
        return ReplayStore(self.path, scope, clock=self.clock)

    def answer(self, store, job, digest=DIGEST, reply=None, call_id="c-1"):
        lease = store.begin(job, digest, call_id=call_id)
        self.assertEqual(lease.role, "leader")
        reply = reply or completion_reply("answer")
        lease.commit(reply, job_id=job.job_id, finish_reason="stop", prompt_tokens=10, completion_tokens=2)
        return reply


class TestScopes(ReplayCase):
    def test_scope_search_shares_across_jobs(self):
        store = self.store("search")
        self.answer(store, J(candidate="cand_a"))
        hit = store.begin(J(candidate="cand_b", dataset="LawBench"), DIGEST, call_id="c-2")
        self.assertEqual(hit.role, "hit")
        self.assertEqual(hit.stored.source_job_id, J(candidate="cand_a").job_id)
        self.assertEqual(hit.stored.source_call_id, "c-1")
        self.assertEqual((hit.stored.finish_reason, hit.stored.prompt_tokens, hit.stored.completion_tokens),
                         ("stop", 10, 2))
        self.assertEqual(store.scope_key(J()), "search")

    def test_scope_job_isolates_jobs_but_not_attempts(self):
        store = self.store("job")
        self.answer(store, J(attempt=1))
        self.assertEqual(store.begin(J(candidate="cand_b"), DIGEST, call_id="c-2").role, "leader")
        self.assertEqual(store.begin(J(attempt=2), DIGEST, call_id="c-3").role, "hit")
        self.assertEqual(store.scope_key(J()), "job:val.cand_a.USPTO")

    def test_qualification_scope_is_always_private(self):
        for scope in ("search", "job"):
            with self.subTest(scope=scope):
                self.path = Path(self._tmp.name) / f"{scope}.sqlite"
                store = self.store(scope)
                qual = J(phase="qualification", candidate="no_memory")
                self.assertEqual(store.scope_key(qual), "q:qual.val.no_memory.USPTO")
                self.answer(store, qual)
                base = J(phase="baseline", candidate="no_memory")
                self.assertEqual(store.begin(base, DIGEST, call_id="c-2").role, "leader")
                self.assertEqual(store.begin(J(phase="qualification", candidate="no_memory", attempt=2), DIGEST,
                                             call_id="c-3").role, "hit")

    def test_repeat_scope_isolates_repeats_but_not_attempts(self):
        store = self.store("search")
        r2 = J(phase="calibration", candidate="no_memory", repeat=2)
        self.assertEqual(store.scope_key(r2), "repeat:val.no_memory.USPTO.r2")
        self.assertEqual(wire.REUSE_SCOPES, ("search", "job", "qualification", "repeat"))
        self.answer(store, r2)
        self.assertEqual(store.begin(J(phase="calibration", candidate="no_memory", repeat=3), DIGEST,
                                     call_id="c-2").role, "leader")
        self.assertEqual(store.begin(J(phase="baseline", candidate="no_memory"), DIGEST, call_id="c-3").role, "leader")
        self.assertEqual(store.begin(J(phase="calibration", candidate="no_memory", repeat=2, attempt=2), DIGEST,
                                     call_id="c-4").role, "hit")

    def test_reuse_salt_separates_samples(self):
        self.assertEqual(reuse_key(DIGEST, None), DIGEST)
        k1, k2 = reuse_key(DIGEST, "s1"), reuse_key(DIGEST, "s2")
        self.assertNotEqual(k1, k2)
        self.assertNotEqual(k1, DIGEST)
        self.assertRegex(k1, r"[0-9a-f]{64}")
        store = self.store("search")
        s1, s2 = J(phase="controls", sample=1), J(phase="controls", sample=2)
        lead1 = store.begin(s1, k1, call_id="c-1")
        lead2 = store.begin(s2, k2, call_id="c-2")       # in flight at once: no coalescing across samples
        self.assertEqual((lead1.role, lead2.role), ("leader", "leader"))
        self.assertEqual(store.begin(J(phase="controls", sample=1, dataset="LawBench"), k1, call_id="c-3").role,
                         "follower")                     # the same sample still coalesces
        lead1.commit(completion_reply("one"), job_id=s1.job_id, finish_reason="stop", prompt_tokens=1,
                     completion_tokens=1)
        lead2.abandon()
        self.assertEqual(store.begin(s2, k2, call_id="c-4").role, "leader")    # no replay across samples
        self.assertEqual(store.begin(s1, k1, call_id="c-5").role, "hit")


class TestStore(ReplayCase):
    def test_only_200_is_stored_and_empty_length_reply_is_stored(self):
        store = self.store()
        lease = store.begin(J(), DIGEST, call_id="c-1")
        with self.assertRaises(ValueError):
            lease.commit(wire.refusal_reply("provider_rejected"), job_id="x", finish_reason=None, prompt_tokens=None,
                         completion_tokens=None)
        lease.fail(wire.refusal_reply("provider_rejected"), share=True)
        self.assertEqual(store.stats()["stored"], 0)
        no_choice = GatewayReply(200, (("content-type", "application/json"),),
                                 json.dumps({"id": "x", "choices": []}).encode())
        lease = store.begin(J(), DIGEST, call_id="c-2")
        lease.commit(no_choice, job_id=J().job_id, finish_reason=None, prompt_tokens=None, completion_tokens=None)
        self.assertEqual(store.stats()["stored"], 0)
        empty = GatewayReply(200, (("content-type", "application/json"),),
                             completion_body("", finish_reason="length", completion_tokens=16384))
        self.answer(store, J(), reply=empty, call_id="c-3")
        self.assertEqual(store.stats()["stored"], 1)
        hit = store.begin(J(candidate="other"), DIGEST, call_id="c-4")
        self.assertEqual(hit.role, "hit")
        self.assertEqual(hit.stored.body, empty.body)

    def test_hit_has_zero_usage_and_nothing_else_changes(self):
        store = self.store()
        body = completion_body("réponse 答案", reasoning="think", prompt_tokens=120, completion_tokens=16)
        self.answer(store, J(), reply=GatewayReply(200, (), body))
        hit = store.begin(J(candidate="cand_b"), DIGEST, call_id="c-2")
        self.assertEqual(hit.stored.body, body)    # the stored body is exactly the first reply
        zeroed = zero_usage(hit.stored.body)
        before, after = json.loads(body), json.loads(zeroed)
        self.assertEqual(after["usage"], {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        before.pop("usage")
        after.pop("usage")
        self.assertEqual(before, after)
        self.assertEqual(list(json.loads(zeroed)), list(json.loads(body)))      # key order kept
        self.assertIn("答案".encode("utf-8"), zeroed)
        self.assertEqual(json.loads(zero_usage(b'{"choices": []}'))["usage"]["total_tokens"], 0)
        with self.assertRaises(ValueError):
            zero_usage(b"[]")
        cut = b'{"choices":[{"index":0,"message":{"role":"assistant","content":"cut \\ud83d"}}],"usage":{"prompt_tokens":1}}'
        zeroed = zero_usage(cut)                   # a lone surrogate escape (a cut emoji) must not raise on a hit
        self.assertEqual(json.loads(zeroed)["choices"], json.loads(cut)["choices"])
        self.assertEqual(json.loads(zeroed)["usage"]["prompt_tokens"], 0)

    def test_body_hash_mismatch_is_a_miss(self):
        store = self.store()
        self.answer(store, J())
        conn = sqlite3.connect(self.path)
        conn.execute("UPDATE replies SET body_sha256 = ?", ("0" * 64,))
        conn.commit()
        self.assertEqual(store.begin(J(), DIGEST, call_id="c-2").role, "leader")
        store2 = ReplayStore(Path(self._tmp.name) / "b.sqlite", "search")
        self.path = store2.path
        self.answer(store2, J())
        conn2 = sqlite3.connect(store2.path)
        conn2.execute("UPDATE replies SET body_zlib = ?", (b"not zlib",))
        conn2.commit()
        conn2.close()
        conn.close()
        self.assertEqual(store2.begin(J(), DIGEST, call_id="c-3").role, "leader")

    def test_scope_change_on_existing_file_refused(self):
        self.store("search")
        with self.assertRaises(StateError):
            self.store("job")
        self.store("search")                       # the same scope reopens
        with self.assertRaises(ValueError):
            ReplayStore(Path(self._tmp.name) / "x.sqlite", "qualification")

    def test_request_hash_ignores_key_order_and_dropped_fields(self):
        reordered = {"messages": [{"content": "Reasoning: medium", "role": "system"}, {"content": "Q?", "role": "user"}],
                     "max_tokens": 16384, "temperature": 0.0, "model": "openai/gpt-oss-120b"}
        self.assertEqual(request_sha256(reordered), DIGEST)
        self.assertEqual(request_sha256(dict(ADMITTED, usage={"include": True}, stream=False)), DIGEST)
        self.assertNotEqual(request_sha256(dict(ADMITTED, temperature=0.7)), DIGEST)
        self.assertNotEqual(request_sha256(dict(ADMITTED, max_tokens=100)), DIGEST)
        self.assertNotEqual(request_sha256(dict(ADMITTED, messages=ADMITTED["messages"][1:])), DIGEST)
        self.assertRegex(DIGEST, r"[0-9a-f]{64}")

    def test_store_survives_restart(self):
        store = self.store()
        reply = self.answer(store, J())
        restarted = ReplayStore(self.path, "search", clock=self.clock)
        hit = restarted.begin(J(candidate="cand_b"), DIGEST, call_id="c-2")
        self.assertEqual(hit.role, "hit")
        self.assertEqual(hit.stored.body, reply.body)
        self.assertEqual(restarted.stats(), {"stored": 1, "hits": 1, "in_flight": 0})


class TestCoalescing(ReplayCase):
    def test_followers_get_leaders_reply_once(self):
        store = self.store()
        barrier = threading.Barrier(30)
        leases, got = [], []
        lock = threading.Lock()
        ready = threading.Event()

        def worker(i):
            barrier.wait()
            lease = store.begin(J(candidate=f"cand_{i}"), DIGEST, call_id=f"c-{i}")
            with lock:
                leases.append(lease)
                if len(leases) == 30:
                    ready.set()
            if lease.role == "follower":
                got.append(lease.wait(10))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(30)]
        for t in threads:
            t.start()
        self.assertTrue(ready.wait(10))
        leaders = [lease for lease in leases if lease.role == "leader"]
        self.assertEqual(len(leaders), 1)          # one provider call for 30 identical requests
        self.assertEqual(sum(1 for lease in leases if lease.role == "follower"), 29)
        self.assertEqual(store.stats()["in_flight"], 1)
        reply = completion_reply("shared")
        leaders[0].commit(reply, job_id="j", finish_reason="stop", prompt_tokens=1, completion_tokens=1)
        for t in threads:
            t.join()
        self.assertEqual(len(got), 29)
        self.assertTrue(all(r is reply for r in got))
        self.assertEqual(store.stats(), {"stored": 1, "hits": 0, "in_flight": 0})
        followers = [lease for lease in leases if lease.role == "follower"]
        self.assertTrue(all(f.leader_call_id == leaders[0]._call_id for f in followers))
        with self.assertRaises(RuntimeError):      # a leader finishes exactly once
            leaders[0].commit(reply, job_id="j", finish_reason="stop", prompt_tokens=1, completion_tokens=1)

    def test_shared_failure_reaches_followers_and_is_not_stored(self):
        store = self.store()
        leader = store.begin(J(), DIGEST, call_id="c-1")
        follower = store.begin(J(candidate="cand_b"), DIGEST, call_id="c-2")
        self.assertEqual(follower.role, "follower")
        self.assertIsNone(follower.wait(0.01))     # the leader is still running
        self.assertFalse(follower.leader_done)
        refusal = wire.refusal_reply("provider_unavailable")
        leader.fail(refusal, share=True)
        self.assertIs(follower.wait(1), refusal)
        self.assertEqual(store.stats()["stored"], 0)
        self.assertEqual(store.begin(J(), DIGEST, call_id="c-3").role, "leader")

    def test_unshared_failure_promotes_a_follower(self):
        store = self.store()
        leader = store.begin(J(), DIGEST, call_id="c-1")
        follower = store.begin(J(candidate="cand_b"), DIGEST, call_id="c-2")
        result = []
        t = threading.Thread(target=lambda: result.append(follower.wait(5)))
        t.start()
        time.sleep(0.05)
        leader.fail(wire.refusal_reply("job_request_ceiling"), share=False)
        t.join(5)
        self.assertEqual(result, [None])
        self.assertTrue(follower.leader_done)
        again = store.begin(J(candidate="cand_b"), DIGEST, call_id="c-2")
        self.assertEqual(again.role, "leader")

    def test_abandoned_leader_promotes_a_follower(self):
        store = self.store()
        leader = store.begin(J(), DIGEST, call_id="c-1")
        follower = store.begin(J(candidate="cand_b"), DIGEST, call_id="c-2")
        leader.abandon()
        self.assertIsNone(follower.wait(1))
        self.assertTrue(follower.leader_done)
        promoted = store.begin(J(candidate="cand_b"), DIGEST, call_id="c-2")
        self.assertEqual(promoted.role, "leader")
        promoted.commit(completion_reply(), job_id="j", finish_reason="stop", prompt_tokens=1, completion_tokens=1)
        promoted.abandon()                         # a no-op after commit (the gateway's finally)
        self.assertEqual(store.stats(), {"stored": 1, "hits": 0, "in_flight": 0})
        hit = store.begin(J(), DIGEST, call_id="c-3")
        hit.abandon()                              # a no-op for a hit
        self.assertIsNone(hit.wait(0))

    def test_bypass_does_not_store(self):
        store = self.store()
        qual = J(phase="qualification", candidate="no_memory")
        self.answer(store, qual)
        first = store.begin(qual, DIGEST, call_id="c-2", bypass=True)
        self.assertEqual(first.role, "leader")     # always a leader, even with a stored reply
        second = store.begin(qual, DIGEST, call_id="c-3", bypass=True)
        self.assertEqual(second.role, "leader")    # nothing coalesces onto a bypass leader
        first.commit(completion_reply("different"), job_id=qual.job_id, finish_reason="stop", prompt_tokens=1,
                     completion_tokens=1)
        second.fail(wire.refusal_reply("halted"), share=True)
        hit = store.begin(qual, DIGEST, call_id="c-4")
        self.assertEqual(json.loads(hit.stored.body)["choices"][0]["message"]["content"], "answer")
        self.assertEqual(store.stats()["stored"], 1)


if __name__ == "__main__":
    unittest.main()
