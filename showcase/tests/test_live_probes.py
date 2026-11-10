# SPDX-License-Identifier: MIT
import unittest
from types import SimpleNamespace

from mhforge import live_probes, wire
from tests import helpers


class LiveProbeTests(unittest.TestCase):
    def test_echo_checks_job_and_roundtrips_multibyte_body(self):
        gateway = live_probes.EchoGateway(SimpleNamespace(halted=lambda: None))
        job = wire.JobIdentity('mh-test', 'val', 'relay_probe', 'USPTO', 1, 'qualification', None)
        gateway.register_job(job)
        raw = '{"i":1,"payload":"汉字é🙂"}'.encode()
        reply = gateway.handle(job, wire.make_envelope('POST', '/v1/chat/completions', raw))
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body, raw)
        self.assertEqual(len(gateway.calls), 1)
        reply = gateway.handle(job, wire.make_envelope('POST', '/v1/wrong', raw))
        self.assertEqual(reply.status, 403)
        self.assertEqual(len(gateway.calls), 1)

    def test_local_executor_cannot_pass_live_sandbox_probe(self):
        ctx = SimpleNamespace(manifest={}, paths=None, executor=object())
        self.assertFalse(live_probes.sandbox(ctx)['passed'])
