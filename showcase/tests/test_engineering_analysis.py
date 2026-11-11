# SPDX-License-Identifier: MIT
"""Recovery provenance checks; synthetic records do not represent model outcomes."""
import copy
import tempfile
import unittest
from pathlib import Path

from analyze_engineering import response_source
from mhforge import util
from mhforge.paths import StatePaths


class ReplayAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.paths=StatePaths(Path(self.temp.name))
        request={'messages':[{'role':'user','content':'synthetic fixture'}],'temperature':0}
        job={'job_key':'test.context_5.USPTO.s3','job_id':'test.context_5.USPTO.s3.a1',
             'sample':3,'candidate':'context_5','dataset':'USPTO','split':'test','campaign':'fixture'}
        self.source={'call_id':'original','outcome':'completed','job':job,'request':request,
                     'request_sha256':util.sha256_bytes(util.canonical_json(request)),
                     'reuse_salt':'s3','response_id':'response','response_model':'model','finish_reason':'stop',
                     'usage':{'prompt_tokens':2,'completion_tokens':1},
                     'response':{'choices':[{'message':{'content':'{"final_answer":"fixture"}'}}]}}
        self.call=copy.deepcopy(self.source)
        self.call.update(call_id='replayed',outcome='replayed',response=None,
                         replay={'scope':'job','source_call_id':'original'})
        self.call['job']['job_id']='test.context_5.USPTO.s3.a2'
        self.path=self.paths.calls_dir(job['job_id'])/'original.json'
        util.write_json(self.path,self.source)

    def tearDown(self):self.temp.cleanup()

    def test_recovery_resolves_original_without_changing_current_record(self):
        self.assertEqual(response_source(self.paths,self.call),self.source)
        self.assertIsNone(self.call['response'])
        self.assertIs(response_source(self.paths,self.source),self.source)

    def test_recovery_refuses_mixed_samples_requests_or_response_facts(self):
        variants=[]
        source=copy.deepcopy(self.source);source['job']['sample']=4;variants.append(source)
        source=copy.deepcopy(self.source);source['request']['temperature']=1;variants.append(source)
        source=copy.deepcopy(self.source);source['response_id']='different';variants.append(source)
        source=copy.deepcopy(self.source);source['outcome']='failed';variants.append(source)
        for source in variants:
            util.write_json(self.path,source)
            with self.assertRaises(ValueError):response_source(self.paths,self.call)
        self.path.unlink()
        with self.assertRaises(ValueError):response_source(self.paths,self.call)

    def test_recovery_refuses_cycles(self):
        source=copy.deepcopy(self.source)
        source.update(response=None,outcome='replayed',replay={'scope':'job','source_call_id':'original'})
        util.write_json(self.path,source)
        with self.assertRaises(ValueError):response_source(self.paths,self.call)
