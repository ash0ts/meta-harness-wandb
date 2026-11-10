# SPDX-License-Identifier: MIT
"""Exercise orchestration against pinned upstream code, with a fake executor."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from mhforge import driver, util, workspace
from mhforge.errors import CampaignStop
from mhforge.stubprovider import StubProvider
from tests import helpers
from tests.fake_facade import FakeExecutor


class DriverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sc = Path(self.temp.name)
        self.example = helpers.example_dir()
        self.m = helpers.make_manifest({'executor.kind': 'local_relay',
            'upstream.example_dir': str(self.example), 'upstream.worker_python': str(helpers.example_python()),
            'upstream.tree_sha256': workspace.source_digest(self.example)[0], 'search.iterations': 2})
        self.source = self.sc / 'manifest.json'
        self.ex = FakeExecutor()
        self.provider = StubProvider()
        util.write_json(self.source, self.m)
        self.new_driver()

    def new_driver(self):
        self.rt = driver.build_runtime(self.m, self.source, provider=self.provider, environ={}, executor=self.ex)
        self.d = driver.Driver(self.rt, base_env={}, home=self.sc / 'home')
        return self.d

    def run_quiet(self, method):
        with contextlib.redirect_stdout(io.StringIO()):
            return method()

    def test_real_upstream_baselines_and_resume(self):
        result = self.run_quiet(self.d.run_baselines)
        self.assertEqual(result['status'], 'complete', result)
        self.assertEqual(len(self.ex.runs), 6)
        self.assertTrue(self.rt.paths.iteration_file(0).exists())
        self.assertEqual(self.run_quiet(self.new_driver().run_baselines)['status'], 'complete')
        self.assertEqual(len(self.ex.runs), 6)
        self.assertEqual(self.rt.ledger.snapshot()['in_flight_micro'], 0)

    def test_campaign_stop_escapes_upstream_and_records_reason(self):
        with patch.object(driver.jobs.JobRunner, 'run_benchmark', side_effect=CampaignStop(reason='budget:baseline')):
            out = self.run_quiet(self.d.run_baselines)
        self.assertEqual(out['status'], 'stopped')
        self.assertEqual(util.read_json(self.rt.paths.stopped)['reason'], 'budget:baseline')
        self.assertFalse((self.d.logs / 'evolution_summary.jsonl').exists())

    def test_exception_record_never_contains_exception_text(self):
        with patch.object(driver.jobs.JobRunner, 'run_benchmark', side_effect=ValueError('sensitive-canary-string')):
            out = self.run_quiet(self.d.run_baselines)
        self.assertEqual(out['status'], 'error')
        self.assertNotIn('sensitive-canary-string', self.rt.paths.stopped.read_text())
        self.assertEqual(out['exception_type'], 'ValueError')

    def test_config_drift_refuses_before_executor(self):
        self.m['upstream']['expect']['temperature'] = .5
        out = self.run_quiet(self.d.run_baselines)
        self.assertEqual(out['reason'], 'upstream_contract')
        self.assertEqual(self.ex.runs, [])

    def test_baseline_incomplete_stops_proposals(self):
        with (patch.object(driver.jobs.JobRunner, 'run_benchmark', return_value=SimpleNamespace(returncode=0, stdout='', stderr='')),
              patch.object(driver.proposer.ProposerRunner, 'propose') as propose):
            out = self.run_quiet(self.d.run_search)
        self.assertEqual(out['reason'], 'baseline_incomplete')
        propose.assert_not_called()

    def test_calibration_does_not_finalize_or_publish_test_results(self):
        self.assertEqual(self.run_quiet(self.d.run_baselines)['status'], 'complete')
        out = self.run_quiet(self.new_driver().run_calibration)
        self.assertEqual(out['status'], 'complete', out)
        self.assertTrue(self.rt.paths.calibration.exists())
        self.assertFalse((self.d.logs / 'finalized.json').exists())
        self.assertFalse(list((self.d.logs / 'results').glob('**/test.json')))
        self.assertEqual(len(self.ex.runs), 12)
        self.assertTrue(all(r['spec'].identity.phase == 'calibration' for r in self.ex.runs[6:]))

    def test_finalization_reuses_calibration_jobs(self):
        self.run_quiet(self.d.run_baselines)
        self.run_quiet(self.new_driver().run_calibration)
        out = self.run_quiet(lambda: self.new_driver().run_test(end_search=True))
        self.assertEqual(out['status'], 'complete', out)
        self.assertEqual(len(self.ex.runs), 12)
        self.assertEqual(util.read_json(self.d.logs / 'finalized.json')['status'], 'complete')
        self.assertEqual([r['status'] for r in self.rt.journal.entries('test')], ['started', 'complete'])

    def test_summary_arithmetic(self):
        p = self.sc / 'summary.jsonl'
        p.write_text('{"iteration": 3}\nnot json\n\n{}\n{"iteration": 1}\n')
        self.assertEqual(driver.count_iterations(p), 3)
        self.assertEqual(driver.remaining_iterations(20, 4), 17)
        self.assertEqual(driver.remaining_iterations(0, 1), 0)

    def test_install_restores_hooks(self):
        attrs = {k: object() for k in ('run_benchmark', 'propose_claude', 'validate_candidates', 'update_evolution_summary')}
        up = SimpleNamespace(meta_harness=SimpleNamespace(**attrs))
        undo = driver.install(up, run_benchmark=lambda a: None, propose=lambda *a: None,
                              validate=lambda a: [], summary=lambda *a: None)
        undo()
        self.assertEqual(vars(up.meta_harness), attrs)

    def test_stop_flag_honoured_before_proposal(self):
        with patch.object(driver.proposer.ProposerRunner, 'propose') as propose:
            self.d.stop_requested = True
            out = self.run_quiet(self.d.run_search)
        self.assertEqual(out['reason'], 'interrupted')
        self.assertEqual(len(self.ex.runs), 6)
        propose.assert_not_called()

    def test_validation_rejects_names_before_import(self):
        driver.startup_recovery(self.rt)
        runner = SimpleNamespace(import_check=lambda *a, **kw: self.fail('invalid names reached import'))
        with patch.object(workspace, 'validate_candidate_file', return_value='invalid'):
            self.assertEqual(self.d._validate([{'name': 'bad-name'}], runner), [])
        self.assertEqual(self.d.rejected, {'bad-name': 'name'})

    def test_missing_dataset_counts_as_zero(self):
        driver.startup_recovery(self.rt)
        util.write_json(self.rt.paths.job_dir('val.no_memory.USPTO') / 'job.json',
                        {'status': 'succeeded', 'result': {'accuracy': .9}})
        self.d._record_iteration(1, [{'name': 'no_memory'}], {'no_memory': 90})
        c = util.read_json(self.rt.paths.iteration_file(1))['candidates'][0]
        self.assertAlmostEqual(c['avg_val_all_datasets'], 30)
        self.assertFalse(c['complete'])

    def fake_proposal(self, obj, prompt, iteration, timeout):
        name = f'candidate_{iteration}'
        (obj.paths.ws / 'agents' / (name + '.py')).write_text('class SyntheticMemory: pass\n')
        util.write_json(obj.upstream.meta_harness.PENDING_EVAL, {'candidates': [{'name': name}]})
        obj.frontier_before = {'system': 'no_memory', 'val_accuracy': 50.0}
        obj.last_session_key = f'iter{iteration:03d}.s1'
        return True

    def test_two_blocks_pause_and_resume(self):
        with patch.object(driver.proposer.ProposerRunner, 'propose', autospec=True, side_effect=self.fake_proposal) as propose:
            first = self.run_quiet(lambda: self.d.run_search(max_iterations_this_run=1))
            self.assertEqual(first['status'], 'paused', first)
            self.assertEqual(first['last_iteration'], 1)
            second = self.run_quiet(lambda: self.new_driver().run_search(max_iterations_this_run=1))
            self.assertEqual(second['status'], 'complete', second)
            self.assertEqual(second['last_iteration'], 2)
            self.assertEqual(propose.call_count, 2)
        self.assertEqual(len(self.ex.runs), 12)
        self.assertEqual([x['spec'].identity.phase for x in self.ex.runs], ['baseline'] * 6 + ['search'] * 6)

    def test_failed_trailing_proposal_does_not_stall_resume(self):
        with patch.object(driver.proposer.ProposerRunner, 'propose', autospec=True, return_value=False) as propose:
            first = self.run_quiet(lambda: self.d.run_search(max_iterations_this_run=1))
            second = self.run_quiet(lambda: self.new_driver().run_search(max_iterations_this_run=1))
        self.assertEqual(first['last_iteration'], 1)
        self.assertEqual(second['last_iteration'], 2)
        self.assertEqual(second['status'], 'complete')
        self.assertEqual([call.args[2] for call in propose.call_args_list], [1, 2])

    def test_recovery_tolerates_interrupted_summary_line(self):
        driver.startup_recovery(self.rt)
        summary = self.d.logs / 'evolution_summary.jsonl'
        summary.parent.mkdir(parents=True, exist_ok=True)
        summary.write_text('{"iteration": 3}\n{"iteration":')
        driver.startup_recovery(self.rt)
        self.assertEqual(driver.settled_iterations(self.rt.paths, summary), 3)
        self.assertEqual(util.read_json(self.rt.paths.iteration_file(3))['kind'], 'recovered')

    def test_iteration_uses_percentage_frontier_and_executor_costs(self):
        driver.startup_recovery(self.rt)
        for ds in self.m['upstream']['expect']['datasets']:
            util.write_json(self.rt.paths.job_dir('val.no_memory.' + ds) / 'job.json',
                {'status': 'succeeded', 'result': {'accuracy': .01},
                 'attempts': [{'gateway': {'cost_micro': 100}, 'executor': {'cost_micro': 200}}]})
        prop = SimpleNamespace(frontier_before={'val_accuracy': .5}, last_session_key=None)
        self.d._record_iteration(1, [{'name': 'no_memory'}], {'no_memory': 1}, prop)
        row = util.read_json(self.rt.paths.iteration_file(1))
        self.assertEqual(row['candidates'][0]['delta_vs_previous_best'], .5)
        self.assertEqual(row['solver_cost_micro'], 300)
        self.assertEqual(row['sandbox_cost_micro'], 600)

    def test_calibration_uses_final_pass_and_reconciles_correct_count(self):
        self.m['upstream']['expect'].update(datasets=['USPTO'], sizes={'USPTO': [2, 2, 2]})
        self.m['calibration']['gates'] = {'G1': {'system': 'no_memory', 'hard': True,
            'macro_test_accuracy': [49, 51], 'correct': {'USPTO': [1, 1]}, 'content_on_every_call': False}}
        events = [{'kind': 'meta', 'seq': 0, 'schema': 'mhforge.sidecar/1'},
                  {'kind': 'splits', 'seq': 1, 'wanted': ['test']}]
        for seq, index, pass_, correct in ((2, 0, 1, True), (3, 1, 1, False), (4, 0, 0, False)):
            events.append({'kind': 'eval', 'seq': seq, 'index': index, 'pass': pass_, 'split': 'test',
                           'was_correct': correct, 'prediction': 'x', 'target': 'x', 'prediction_chars': 1})
        events.append({'kind': 'done', 'seq': 5})
        row = {'status': 'succeeded', 'job_key': 'test.no_memory.USPTO',
               'result': {'accuracy': .5, 'correct': 1, 'total': 2},
               'attempts': [{'outcome': 'ok', 'attempt': 1}]}
        path = self.rt.paths.attempt_dir(row['job_key'], 1) / 'out' / 'sidecar.jsonl'
        path.parent.mkdir(parents=True)
        path.write_bytes(b''.join(util.canonical_json(e) + b'\n' for e in events))
        records = {'no_memory': {'USPTO': row}}
        self.assertEqual(driver.calibration_verdict(self.m, records, self.rt.paths)['verdict'], 'pass')
        row['result']['correct'] = 2
        self.assertEqual(driver.calibration_verdict(self.m, records, self.rt.paths)['verdict'], 'fail')
