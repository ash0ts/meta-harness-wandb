# SPDX-License-Identifier: MIT
from decimal import Decimal
import json
import unittest

from analyze_inference_cost import settle


class CostAnalysisTests(unittest.TestCase):
    prices = {'input':Decimal('1'),'cached':Decimal('.1'),'output':Decimal('2')}

    def test_cached_usage_and_unknown_retry_stay_separate(self):
        rows=[{'status':'complete','spent_micro':120,'reserved_micro':1000,
               'usage_json':json.dumps({'prompt_tokens':100,'completion_tokens':10,
                                       'prompt_tokens_details':{'cached_tokens':0}})},
              {'status':'bounded_unknown','spent_micro':None,'reserved_micro':1000},
              {'status':'rejected','spent_micro':0,'reserved_micro':1000}]
        result=settle(rows,self.prices)
        self.assertEqual(result['cost_range_micro_usd'],[120,1120])
        self.assertEqual(result['known_usage']['prompt_tokens'],100)
        rows[0]['usage_json']=json.dumps({'prompt_tokens':100,'completion_tokens':10,
                                         'prompt_tokens_details':{'cached_tokens':50}})
        rows[0]['spent_micro']=75
        self.assertEqual(settle(rows,self.prices)['cost_range_micro_usd'],[75,1075])

    def test_missing_usage_or_mismatched_cost_cannot_become_zero(self):
        for row in ({'status':'pending'}, {'status':'unknown'},
                    {'status':'bounded_unknown','spent_micro':0},
                    {'status':'rejected','spent_micro':1},
                    {'status':'complete','spent_micro':0,'usage_json':json.dumps({'prompt_tokens':1,'completion_tokens':1})}):
            with self.assertRaises(ValueError): settle([row],self.prices)


if __name__ == '__main__': unittest.main()
