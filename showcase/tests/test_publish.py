# SPDX-License-Identifier: MIT
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from mhforge import publish, util
from mhforge.errors import ExportError, SecretFound
from tests import helpers


class PublishTests(unittest.TestCase):
    def setup_state(self, st):
        util.write_json(st.paths.manifest, st.manifest)
        util.write_json(st.paths.stopped, {'reason': 'operator', 'local_path': str(st.paths.root / 'x')})
        for rel in ('account/account.json', 'runtime.json', 'keydigests.json', 'calls/a/record.json',
                    'jobs/a/a1/out/memory.json', 'proposer/iter-001/s1/transcript.jsonl'):
            p = st.paths.root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('{}')
        # keydigests has a closed schema and is consulted, but never copied.
        util.write_json(st.paths.keydigests, {'schema': 'mhforge.keydigests/1', 'keys': []})

    def test_allowlist_excludes_raw_and_account_data(self):
        with helpers.tmp_state(manifest=True) as st:
            self.setup_state(st)
            files = {p.relative_to(st.paths.root).as_posix() for p in publish.allowed_files(st.paths, st.manifest)}
            self.assertEqual(files, {'manifest.json', 'stopped.json'})

    def test_bundle_scrubs_paths_and_is_immutable(self):
        with helpers.tmp_state(manifest=True) as st:
            self.setup_state(st)
            dest = st.paths.publish_root / 'bundle'
            result = publish.bundle(st.manifest, st.paths, dest, known_secrets=[])
            self.assertIn('stopped.json', result['files'])
            self.assertNotIn(str(st.paths.root), (dest / 'stopped.json').read_text())
            with self.assertRaises(ExportError):
                publish.bundle(st.manifest, st.paths, dest, known_secrets=[])

    def test_secrets_abort_before_final_bundle_exists(self):
        with helpers.tmp_state(manifest=True) as st:
            self.setup_state(st)
            util.write_json(st.paths.stopped, {'detail': 'canary-secret-123456789'})
            dest = st.paths.publish_root / 'bundle'
            with self.assertRaises(SecretFound):
                publish.bundle(st.manifest, st.paths, dest, known_secrets=[b'canary-secret-123456789'])
            self.assertFalse(dest.exists())
            self.assertFalse(list(dest.parent.glob('.bundle-*')))

    def test_symlinked_parent_not_followed(self):
        with helpers.tmp_state(manifest=True) as st:
            foreign = st.sc / 'foreign'
            foreign.mkdir()
            util.write_json(foreign / 'job.json', {'private': True})
            st.paths.jobs_root.mkdir()
            (st.paths.jobs_root / 'a').symlink_to(foreign, target_is_directory=True)
            self.assertNotIn(st.paths.jobs_root / 'a/job.json', publish.allowed_files(st.paths, st.manifest))

    def test_candidate_code_with_no_dataset_index_is_withheld(self):
        with helpers.tmp_state(manifest=True) as st:
            self.setup_state(st)
            p = st.paths.ws / 'agents' / 'candidate.py'
            p.parent.mkdir(parents=True)
            p.write_text('PRIVATE_DATASET_TEXT = "question content"\n')
            dest = st.paths.publish_root / 'bundle'
            out = publish.bundle(st.manifest, st.paths, dest, known_secrets=[])
            rel = p.relative_to(st.paths.root).as_posix()
            self.assertIn(rel, out['withheld'])
            self.assertNotIn('question content', (dest / rel).read_text())

    def test_preflight_campaign_never_publishes(self):
        with helpers.tmp_state(manifest=True) as st:
            st.manifest['campaign_kind'] = 'preflight'
            with self.assertRaises(ExportError):
                publish.publish(st.manifest, st.paths, wandb=None, remote_conversation_ids=lambda: [])

    def test_foreign_conversation_blocks_outward_calls(self):
        with helpers.tmp_state(manifest=True) as st:
            st.manifest['campaign_kind'] = 'measured'
            st.manifest['decisions']['publish_approved'] = True
            with self.assertRaises(ExportError):
                publish.publish(st.manifest, st.paths, wandb=None, remote_conversation_ids=lambda: ['foreign'])

    def test_unknown_write_outcome_is_not_retried(self):
        with helpers.tmp_state(manifest=True) as st:
            st.manifest['campaign_kind'] = 'measured'
            st.manifest['decisions']['publish_approved'] = True
            calls = []
            def fail(*args):
                calls.append('write')
                raise RuntimeError('uncertain')
            sink = SimpleNamespace(publish_visibility=fail)
            with self.assertRaises(RuntimeError):
                publish.publish(st.manifest, st.paths, wandb=sink, remote_conversation_ids=lambda: [])
            with self.assertRaises(ExportError):
                publish.publish(st.manifest, st.paths, wandb=sink, remote_conversation_ids=lambda: [])
            self.assertEqual(calls, ['write'])

    def test_ledger_halt_and_reconciliation_history_included(self):
        from mhforge.ledger import Ledger
        with helpers.tmp_state(manifest=True) as st:
            self.setup_state(st)
            Ledger(st.paths.ledger, st.manifest).set_halt('operator', 'stopped for review')
            dest = st.paths.publish_root / 'bundle'
            publish.bundle(st.manifest, st.paths, dest, known_secrets=[])
            self.assertEqual(util.read_json(dest / 'halt_history.json')['halt_history'][0]['reason'], 'operator')
