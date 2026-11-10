# SPDX-License-Identifier: MIT
import copy
import shutil
import unittest

from mhforge import qualification, util
from mhforge.telemetry import plan_study
from tests import helpers
from tests.test_exporter import Campaign


class StudyExport(unittest.TestCase):
    def setUp(self):
        self.ctx = helpers.tmp_state(manifest=True)
        self.st = self.ctx.__enter__()
        self.addCleanup(lambda: self.ctx.__exit__(None, None, None))
        self.m = copy.deepcopy(self.st.manifest)
        self.m['upstream']['expect']['baselines'] = ['no_memory']
        self.m['upstream']['expect']['datasets'] = ['USPTO']
        self.m['upstream']['expect']['sizes']['USPTO'][1] = 4
        self.m['calibration']['noise_repeats'] = 2
        c = Campaign(self.st)
        c.job()
        key = 'val.no_memory.USPTO'
        source = self.st.paths.job_dir(key)
        target = self.st.paths.job_dir(key + '.r2')
        shutil.copytree(source, target)
        sidecars, results = {'no_memory': {'USPTO': {}}}, {'no_memory': {'USPTO': {}}}
        for repeat, folder in ((1, source), (2, target)):
            job = util.read_json(folder / 'job.json')
            job.update(job_key=key + ('.r2' if repeat == 2 else ''), repeat=repeat if repeat == 2 else None,
                       sample=None)
            out = folder / 'a1/out'
            rows = util.read_jsonl(out / 'sidecar.jsonl')
            util.write_json(out / 'val.json', job['result'])
            job['attempts'][0]['outputs'] = {name: util.sha256_file(out / name)
                                             for name in ('sidecar.jsonl', 'val.json')}
            util.write_json(folder / 'job.json', job)
            sidecars['no_memory']['USPTO'][repeat] = rows
            results['no_memory']['USPTO'][repeat] = job['result']
        util.write_json(self.st.paths.noise, qualification.noise_stats(sidecars, results))

    def plan(self, url='https://forge.coreweave.com/agent-lens/fixture'):
        return plan_study.plan('noise', self.st.paths, self.m, lambda job: url, export_attempt=1)

    def test_complete_evidence_has_one_row_per_repeat_and_no_raw_predictions(self):
        run, tables = self.plan()
        self.assertEqual(len(tables['tables'][0]['rows']), 2)
        self.assertNotIn('prediction', tables['tables'][0]['columns'])
        self.assertEqual(len(run['run']['artifacts'][0]['metadata']['sources']), 2)

    def test_unverified_trace_and_changed_aggregate_refuse_export(self):
        with self.assertRaisesRegex(ValueError, 'trace readback'):
            self.plan(None)
        doc = util.read_json(self.st.paths.noise)
        doc['macro_std']['no_memory'] += 1
        util.write_json(self.st.paths.noise, doc)
        with self.assertRaisesRegex(ValueError, 'does not reproduce'):
            self.plan()

    def test_mutated_sidecar_and_missing_repeat_refuse_export(self):
        path = self.st.paths.job_dir('val.no_memory.USPTO.r2') / 'a1/out/sidecar.jsonl'
        path.write_bytes(path.read_bytes() + b'\n')
        with self.assertRaisesRegex(ValueError, 'digest changed'):
            self.plan()
        path.unlink()
        with self.assertRaises(OSError):
            self.plan()


if __name__ == '__main__':
    unittest.main()
