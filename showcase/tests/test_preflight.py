# SPDX-License-Identifier: MIT
import dataclasses
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mhforge import preflight, util
from tests import helpers


class PreflightTests(unittest.TestCase):
    def test_describe_preserves_order_without_calls(self):
        rows = preflight.describe()
        self.assertEqual(rows[0]['step'], 'PF1')
        self.assertEqual(rows[-1]['step'], 'PF-R')
        self.assertTrue(all('run' not in r for r in rows))
        self.assertEqual(len({r['name'] for r in rows}), len(rows))

    def test_reconciliation_is_not_zero_when_missing(self):
        m = helpers.make_manifest()
        self.assertFalse(preflight.reconciliation({}, {}, m)['passed'])
        out = preflight.reconciliation({}, {'platform_inference_usd': '0', 'platform_sandbox_usd': '0'}, m)
        self.assertTrue(out['passed'])
        self.assertEqual(out['verdict'], 'pass')

    def test_reconciliation_unknown_spend_blocks(self):
        out = preflight.reconciliation({'bounded_unknown_requests': 1},
            {'platform_inference_usd': '0', 'platform_sandbox_usd': '0'}, helpers.make_manifest())
        self.assertFalse(out['passed'])

    def test_reconciliation_tolerance_and_bad_numbers(self):
        m = helpers.make_manifest()
        for amount in ('NaN', '-1', 'Infinity', 'n/a', '5'):
            out = preflight.reconciliation({'observed_micro': 1000000},
                {'platform_inference_usd': amount, 'platform_sandbox_usd': '0'}, m)
            self.assertFalse(out['passed'], amount)
        self.assertTrue(preflight.reconciliation({'observed_micro': 1000000},
            {'platform_inference_usd': '1.01', 'platform_sandbox_usd': '0'}, m)['passed'])

    def test_selected_check_cannot_qualify_campaign(self):
        with helpers.tmp_state(manifest=True) as st:
            ctx = preflight.Context(st.manifest, st.paths, {}, st.sc,
                probes={'telemetry': lambda _: {'passed': True}})
            out = preflight.run_checks(ctx, ['telemetry'])
            self.assertTrue(out['passed'])
            self.assertFalse(out['qualified'])
            self.assertFalse(st.paths.qualification_passed.exists())

    def test_unavailable_probe_is_failed_and_receipted(self):
        with helpers.tmp_state(manifest=True) as st:
            out = preflight.run_checks(preflight.Context(st.manifest, st.paths, {}, st.sc), ['sandbox'])
            self.assertFalse(out['passed'])
            self.assertEqual(out['checks'][0]['status'], 'unverified')
            self.assertTrue((st.paths.qualification_dir(1) / 'sandbox.json').exists())

    def test_full_qualification_preserves_environment_identity(self):
        with helpers.tmp_state(manifest=True) as st:
            environment = {'freeze_sha256': 'f' * 64}
            check = preflight.Check('PF7', 'qualification', True, True, False, '', '',
                lambda _: {'passed': True, 'observations': {}, 'forecast': {'fits': True}, 'environment': environment})
            with patch.object(preflight, 'CHECKS', (check,)):
                out = preflight.run_checks(preflight.Context(st.manifest, st.paths, {}, st.sc))
            self.assertTrue(out['qualified'])
            self.assertEqual(util.read_json(st.paths.qualification_passed)['environment'], environment)

    def test_failed_required_check_stops_later_spend(self):
        with helpers.tmp_state(manifest=True) as st:
            called = []
            ctx = preflight.Context(st.manifest, st.paths, {}, st.sc,
                probes={'sandbox': lambda _: {'passed': False}, 'telemetry': lambda _: called.append(1)})
            out = preflight.run_checks(ctx, ['sandbox', 'telemetry'])
            self.assertFalse(out['passed'])
            self.assertEqual(called, [])

    def test_exception_receipt_omits_exception_text(self):
        with helpers.tmp_state(manifest=True) as st:
            def fail(_):
                raise ValueError('private-credential')
            ctx = preflight.Context(st.manifest, st.paths, {}, st.sc, probes={'telemetry': fail})
            out = preflight.run_checks(ctx, ['telemetry'])
            self.assertFalse(out['passed'])
            self.assertNotIn('private-credential', str(out))

    def test_create_project_reads_back_private_access(self):
        with helpers.tmp_state(manifest=True) as st:
            calls, project = [], [None]
            def create(entity, name):
                calls.append((entity, name))
                project[0] = {'access': 'PRIVATE', 'read_only': False, 'can_write': True}
            account = SimpleNamespace(server_features=lambda: {}, entity_org=lambda _: {'org': None},
                project=lambda *args: project[0], create_project_private=create)
            st.manifest['executor']['kind'] = 'local'
            ctx = preflight.Context(st.manifest, st.paths, {}, st.sc, account=account)
            out = preflight.account_check(ctx, create_project=True)
            self.assertTrue(out['passed'], out)
            self.assertEqual(len(calls), 1)
            preflight.account_check(ctx, create_project=True)
            self.assertEqual(len(calls), 1)

    def test_probe_script_quotes_paths_and_is_status_only(self):
        from pathlib import Path
        script = preflight.build_probe_script(canary_denied=Path('/tmp/denied'), credential_paths=["/tmp/a'; echo unsafe"],
            scratch=Path('/tmp/scratch dir'), loopback_port=1234)
        self.assertIn('>/dev/null 2>&1', script)
        self.assertNotIn('cat ', script)
        self.assertIn('curl --max-time 5', script)
        self.assertEqual(preflight.probe_expectations('restricted')['existing_report'], 'nonzero')

    def test_inference_probe_real_gateway_meters_both_calls(self):
        from mhforge.driver import build_runtime
        from mhforge.stubprovider import StubProvider
        from tests.fake_facade import FakeExecutor
        with helpers.tmp_state(manifest=True) as st:
            provider = StubProvider()
            rt = build_runtime(st.manifest, st.manifest_path, provider=provider, environ={}, executor=FakeExecutor())
            ctx = preflight.Context(st.manifest, st.paths, {}, st.sc, gateway=rt.gateway, ledger=rt.ledger)
            result = preflight._inference(ctx)
            self.assertTrue(result['passed'], result)
            self.assertEqual(len(provider.calls), 2)
            self.assertEqual(rt.ledger.snapshot()['requests'], 2)
            self.assertFalse(rt.ledger.verify_totals())
