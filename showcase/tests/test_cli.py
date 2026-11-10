# SPDX-License-Identifier: MIT
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mhforge import cli, manifest, util
from mhforge.errors import LiveGateError
from mhforge.paths import resolve_state
from tests import helpers


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sc = Path(self.tmp.name)
        self.m = helpers.make_manifest()
        self.source = self.sc / 'manifest.json'
        util.write_json(self.source, self.m)
        self.paths = resolve_state(self.m, self.source)

    def call(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli.main([*args, '--manifest', str(self.source)])
        return code, out.getvalue()

    def test_status_does_not_create_state(self):
        self.assertEqual(self.call('status')[0], 0)
        self.assertFalse(self.paths.root.exists())

    def test_default_is_never_live(self):
        for command in ('account-check', 'preflight', 'search', 'export', 'publish'):
            code, output = self.call(command)
            self.assertEqual(code, 3, (command, output))
        self.assertFalse(self.paths.root.exists())

    def test_empty_halt_refused(self):
        self.assertEqual(self.call('halt', '--note', 'operator')[0], 3)

    def test_roles(self):
        self.m['executor']['kind'] = 'sandbox'
        self.assertEqual(cli.roles_needed('preflight', self.m), ('provider', 'sandbox', 'export'))
        self.assertEqual(cli.roles_needed('export', self.m), ('export',))
        self.m['executor']['kind'] = 'local'
        self.assertEqual(cli.roles_needed('evaluate', self.m), ('provider',))

    def test_prompt_reuses_previous_without_environment_write(self):
        replies = iter(['private-canary', '', ''])
        env = {}
        result = cli.read_keys(('prompt', None), cli.KEY_ROLES, self.m, env, getpass_fn=lambda _: next(replies))
        self.assertEqual(set(result.values()), {'private-canary'})
        self.assertEqual(env, {})

    def test_fd_all_and_role_override(self):
        result = cli.read_keys(('fd', 7), cli.KEY_ROLES, self.m, {},
                              read_fd=lambda _: b'all=first\nexport=second\n')
        self.assertEqual(result[self.m['provider']['api_key_env']], 'first')
        self.assertEqual(result[self.m['telemetry']['api_key_env']], 'second')

    def test_missing_or_duplicate_fd_roles_refused_without_values(self):
        for raw in (b'provider=canary\n', b'all=canary\nall=secret\n', b'unknown=canary\n'):
            with self.assertRaises(LiveGateError) as cm:
                cli.read_keys(('fd', 7), cli.KEY_ROLES, self.m, {}, read_fd=lambda _: raw)
            self.assertNotIn('canary', str(cm.exception))

    def test_fd_reader_closes_descriptor(self):
        read, write = os.pipe()
        os.write(write, b'all=canary\n')
        os.close(write)
        self.assertEqual(cli._fd_bytes(read), b'all=canary\n')
        with self.assertRaises(OSError):
            os.fstat(read)

    def test_invalid_key_sources(self):
        for text in ('fd:-1', 'fd:abc', 'password', 'fd:', 'prompt=abc'):
            with self.assertRaises(Exception):
                cli.parse_key_source(text)

    def test_sdk_home_is_empty_private_and_environment_restored(self):
        prior = Path.cwd()
        with patch.dict(os.environ, {'HOME': str(self.sc), 'WANDB_API_KEY': 'unrelated', 'CES_CUSTOM_HEADERS': 'unrelated'}):
            with cli.sdk_process_environment(self.paths):
                child = Path.cwd()
                self.assertEqual(child, Path(os.environ['HOME']))
                self.assertEqual(child.stat().st_mode & 0o777, 0o700)
                self.assertNotIn('WANDB_API_KEY', os.environ)
                self.assertNotIn('CES_CUSTOM_HEADERS', os.environ)
                self.assertFalse(list(child.iterdir()))
                self.assertEqual(self.paths.export_netrc.read_bytes(), b'')
            self.assertEqual(os.environ['WANDB_API_KEY'], 'unrelated')
        self.assertEqual(Path.cwd(), prior)

    def test_halt_does_not_take_runner_lock(self):
        from mhforge.ledger import Ledger
        from mhforge.state import StateLock
        ledger = Ledger(self.paths.ledger, self.m)
        with StateLock(self.paths.runner_lock, 'search'):
            self.assertEqual(self.call('halt', '--note', 'operator stop')[0], 0)
        self.assertEqual(ledger.halted().reason, 'operator')

    def test_reset_takes_runner_lock(self):
        from mhforge.ledger import Ledger
        from mhforge.state import StateLock
        Ledger(self.paths.ledger, self.m)
        with StateLock(self.paths.runner_lock, 'search'):
            self.assertEqual(self.call('status', '--reset-job', 'val.no_memory.USPTO', '--note', 'reviewed')[0], 4)

    def test_no_key_value_printed_on_refusal(self):
        with patch.dict(os.environ, {self.m['provider']['api_key_env']: 'PRIVATE-CANARY-DO-NOT-PRINT'}):
            code, output = self.call('search', '--live')
        self.assertEqual(code, 3)
        self.assertNotIn('PRIVATE-CANARY-DO-NOT-PRINT', output)

    def test_dry_export_needs_no_keys(self):
        self.assertEqual(self.call('export', '--dry-run')[0], 0)

    def test_arbitrary_sdk_exception_never_prints_sensitive_text(self):
        with patch.object(cli, '_main', side_effect=RuntimeError('PRIVATE-CANARY')):
            code, output = self.call('account-check', '--live')
        self.assertEqual(code, 1)
        self.assertIn('RuntimeError', output)
        self.assertNotIn('PRIVATE-CANARY', output)

    def test_runner_initialization_failure_is_durable(self):
        args = cli.parser().parse_args(['search', '--manifest', str(self.source)])
        with patch('mhforge.driver.build_runtime', side_effect=RuntimeError('PRIVATE-CANARY')):
            with self.assertRaises(RuntimeError):
                cli._runner(args, self.m, self.paths, {self.m['provider']['api_key_env']: 'canary'}, {}, self.sc, 'test-host')
        self.assertEqual(util.read_json(self.paths.stopped)['exception_type'], 'RuntimeError')
        self.assertNotIn('PRIVATE-CANARY', self.paths.stopped.read_text())

    def test_keyboard_interrupt_exits_cleanly(self):
        with patch.object(cli, '_main', side_effect=KeyboardInterrupt):
            code, output = self.call('search')
        self.assertEqual(code, 2)
        self.assertIn('interrupted', output)
