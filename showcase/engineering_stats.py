# SPDX-License-Identifier: MIT
"""Portable score-only analysis. No network, private data, or third-party packages."""
import collections
import random
import statistics


def choose(labels, k):
    counts = collections.Counter(labels[:k])
    top = max(counts.values())
    return next(i for i,x in enumerate(labels[:k]) if counts[x] == top)


def paired(left, right, repeats=10000, seed=20261111):
    strata = []
    for ds in sorted(left):
        if set(left[ds]) != set(right[ds]): raise ValueError('unpaired task sets')
        strata.append([right[ds][i]-left[ds][i] for i in sorted(left[ds])])
    delta = statistics.mean(statistics.mean(x) for x in strata)
    rng = random.Random(seed)
    draws = sorted(statistics.mean(sum(rng.choices(x,k=len(x)))/len(x) for x in strata) for _ in range(repeats))
    return {'delta_pp':100*delta, 'ci95_pp':[100*draws[int(repeats*.025)],100*draws[int(repeats*.975)]],
            'method':'paired task bootstrap within datasets; all draws of a task remain together',
            'repeats':repeats,'seed':seed}


def summarize(rows, *, exclude_overlap=False, bootstrap=True):
    grouped = collections.defaultdict(list)
    for r in rows:
        if exclude_overlap and r['dataset']=='Symptom2Disease' and int(r['index'])==11: continue
        grouped[(r['system'],r['dataset'],int(r['index']))].append(r)
    means, policies, systems = {}, {}, {}
    for (system,ds,i), group in sorted(grouped.items()):
        group.sort(key=lambda x:int(x['sample']))
        if [int(x['sample']) for x in group] != [1,2,3,4,5]: raise ValueError('sample membership differs')
        correct = [int(x['correct']) for x in group]
        labels = [int(x['label_class']) for x in group]
        means.setdefault(system,{}).setdefault(ds,{})[i] = sum(correct)/5
        for k in (1,3,5):
            policies.setdefault(system,{}).setdefault(k,{}).setdefault(ds,{})[i] = correct[choose(labels,k)]
    macro = lambda arm: 100*statistics.mean(statistics.mean(v.values()) for v in arm.values())
    for system, arm in means.items():
        selected = [r for r in rows if r['system']==system and not (exclude_overlap and r['dataset']=='Symptom2Disease' and int(r['index'])==11)]
        systems[system] = {'mean_accuracy_percent':macro(arm),
            'per_dataset_mean_accuracy_percent':{ds:100*statistics.mean(v.values()) for ds,v in arm.items()},
            'vote_accuracy_percent':{str(k):macro(policies[system][k]) for k in (1,3,5)},
            'single_draw_macro_percent':[100*statistics.mean(statistics.mean(int(r['correct']) for r in selected if r['dataset']==ds and int(r['sample'])==s) for ds in arm) for s in range(1,6)],
            'empty_answers':sum(int(r['empty']) for r in selected),
            'strict_json_valid_fraction':statistics.mean(int(r['strict_json_valid']) for r in selected),
            'examples_mean':statistics.mean(int(r['examples']) for r in selected),
            'examples_range':[min(int(r['examples']) for r in selected),max(int(r['examples']) for r in selected)]}
    comparisons = {}
    if bootstrap:
        for a,b in [('context_0','context_5'),('context_5','context_all'),('context_0','context_all')]:
            comparisons[b+'_minus_'+a] = paired(means[a],means[b])
        for arm in ('context_all','context_all_t1'):
            for a,b in [(1,3),(1,5),(3,5)]:
                comparisons[f'{arm}_vote{b}_minus_vote{a}'] = paired(policies[arm][a],policies[arm][b])
    return {'systems':systems,'paired':comparisons,'unique_tasks':sum(len(x) for x in means['context_all'].values()),
            'scored_outputs':sum(len(x) for x in grouped.values()),'overlap_excluded':exclude_overlap}
