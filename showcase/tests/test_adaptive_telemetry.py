import copy,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from adaptive_export import enrich_decisions
from mhforge import util
from mhforge.telemetry.adapter_agentlens import _import_sdk,_sdk_conversation,_sdk_turn

class AdaptiveTelemetry(unittest.TestCase):
    def test_sdk_conversation_and_turn_identity_and_numeric_votes(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths=SimpleNamespace(root=Path(tmp));p=paths.root/'adaptive/decisions/block/0000.json'
            util.write_json(p,{'policy':'stop3_else5','chosen_position':1,'samples':[{'call_id':'c1','position':1,'label':'PRIVATE_LABEL'},{'call_id':'c2','position':2,'label':'PRIVATE_LABEL'},{'call_id':'c3','position':3,'label':'PRIVATE_LABEL'}]})
            turn={'turn_key':'task-0','agent_name':'fixed-adaptive-vote','agent_version':'1','started_utc':'2026-11-11T00:00:00.000000Z','ended_utc':'2026-11-11T00:00:01.000000Z','attributes':{'metaharness.task_index':0},'spans':[{'key':'c'+str(i),'attributes':{}} for i in range(1,4)]}
            plan={'unit':'adaptive:block','conversations':[{'conversation_id':'valid','name':'block','attributes':{},'turns':[turn]}]}
            got=enrich_decisions(plan,paths,{'telemetry':{'attribute_prefix':'metaharness'}});conv=got['conversations'][0]
            self.assertEqual(conv['conversation_name'],'block');self.assertNotIn('name',conv)
            self.assertEqual(turn['attributes']['metaharness.turn_key'],'task-0')
            self.assertEqual(turn['attributes']['metaharness.chosen_position'],1)
            self.assertFalse(turn['attributes']['metaharness.second_batch_requested'])
            self.assertEqual([s['attributes']['metaharness.normalized_label_class'] for s in turn['spans']],[0,0,0])
            self.assertEqual([s['attributes']['metaharness.selected'] for s in turn['spans']],[True,False,False])
            self.assertNotIn('PRIVATE_LABEL',json.dumps(got))
            try:tracing,_=_import_sdk()
            except ImportError:self.skipTest('Optional installed CoreWeave SDK unavailable')
            _sdk_conversation(tracing,conv);_sdk_turn(tracing,turn)
if __name__=='__main__':unittest.main()
