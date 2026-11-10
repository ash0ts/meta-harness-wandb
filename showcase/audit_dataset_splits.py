#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Audit exact released question overlap without exporting any dataset text."""
import argparse
from collections import defaultdict
from itertools import combinations
from pathlib import Path

from mhforge import util


def inspect(manifest, example):
    result = {'schema':'mhforge.split-audit/1','match':'exact question and optional instruction; target checked separately',
              'datasets':{},'sensitivity_exclude_test_indices':{}}
    for ds, directory in manifest['upstream']['expect']['dataset_dirs'].items():
        splits, files = {}, {}
        for split, n in zip(('train','val','test'),manifest['upstream']['expect']['sizes'][ds]):
            p = example/'data'/directory/(split+'.jsonl')
            rows = util.read_jsonl(p)
            if len(rows) != n:
                raise ValueError('declared split membership differs from released file')
            files[split] = {'sha256':util.sha256_file(p),'count':n}
            items = defaultdict(list)
            for i,row in enumerate(rows):
                digest = util.sha256_bytes(util.canonical_json({'question':row['question'],'instruction':row.get('instruction')}))
                target = util.sha256_bytes(util.canonical_json(row.get('target',row.get('answer'))))
                items[digest].append({'index':i,'target_sha256':target})
            splits[split] = items
        overlap = {}
        excluded = set()
        for a,b in combinations(splits,2):
            pairs = []
            for digest in sorted(splits[a].keys() & splits[b].keys()):
                for x in splits[a][digest]:
                    for y in splits[b][digest]:
                        pairs.append({'question_sha256':digest,'left_index':x['index'],'right_index':y['index'],
                                      'same_target':x['target_sha256']==y['target_sha256']})
                        if b == 'test':excluded.add(y['index'])
            overlap[a+'__'+b] = pairs
        result['datasets'][ds] = {'files':files,'cross_split':overlap,
            'within_split_duplicate_groups':{s:[{'question_sha256':h,'indices':[x['index'] for x in group]}
                for h,group in sorted(items.items()) if len(group)>1] for s,items in splits.items()}}
        result['sensitivity_exclude_test_indices'][ds] = sorted(excluded)
    return result


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    m=util.read_json(args.manifest)
    doc=inspect(m,(args.manifest.parent/m['upstream']['example_dir']).absolute())
    util.write_json(args.output,doc,fsync=True)
    print({ds:{pair:len(rows) for pair,rows in d['cross_split'].items()} for ds,d in doc['datasets'].items()})
