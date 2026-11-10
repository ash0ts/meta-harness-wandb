#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Reconcile marginal inference costs across every attempt of selected jobs.

These are frozen-price usage estimates, never platform invoice totals. Replays
carry no new charge; their earlier source calls remain outside this job scope.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3

from mhforge import ledger, util
from mhforge.paths import StatePaths


def settle(rows, prices):
    counts = Counter()
    spent = uncertain = prompt = completion = cached = 0
    for row in rows:
        status = row['status']; counts[status] += 1
        if status == 'complete':
            usage = json.loads(row['usage_json'])
            computed = ledger.cost_micro(prices, usage)
            if computed != row['spent_micro']:
                raise ValueError('usage-priced cost differs from the ledger')
            spent += computed
            prompt += usage['prompt_tokens']; completion += usage['completion_tokens']
            cached += (usage.get('prompt_tokens_details') or {}).get('cached_tokens') or 0
        elif status == 'bounded_unknown':
            if row['spent_micro'] is not None:
                raise ValueError('unknown usage has a settled cost')
            uncertain += row['reserved_micro']
        elif status == 'rejected':
            if row['spent_micro'] != 0:
                raise ValueError('rejected request has a nonzero charge')
        else:
            raise ValueError('cost study needs settled or explicitly bounded attempts: '+status)
    return {'provider_attempts':dict(sorted(counts.items())), 'usage_priced_micro_usd':spent,
            'unknown_cost_upper_micro_usd':uncertain, 'cost_range_micro_usd':[spent, spent+uncertain],
            'known_usage':{'prompt_tokens':prompt,'completion_tokens':completion,'cached_tokens':cached}}


def inspect(root, system, phases):
    root = root.resolve(); paths = StatePaths(root); manifest = util.read_json(paths.manifest)
    prices = ledger.prices_from_manifest(manifest)
    db = sqlite3.connect('file:'+str(root/'ledger.sqlite')+'?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    evidence, jobs, all_rows, seen_ledger = [], [], [], set()
    def source(path):
        evidence.append({'path':str(path.relative_to(root)), 'sha256':util.sha256_file(path)})
    try:
        db.execute('BEGIN')
        for path in sorted(paths.jobs_root.glob('*/job.json')):
            job = util.read_json(path)
            if job['candidate'] != system or job['phase'] not in phases:
                continue
            if job['status'] != 'succeeded':
                raise ValueError('selected job is not complete')
            source(path); rows = []; call_cost = 0; outcomes = Counter(); seen_attempts = set()
            for attempt in job['attempts']:
                jid = attempt['job_id']
                part = [dict(r) for r in db.execute('SELECT * FROM calls WHERE job_id=? ORDER BY id', (jid,))]
                by_id = {r['id']:r for r in part}; records_seen = set()
                for call_path in sorted(paths.calls_dir(jid).glob('*.json')):
                    call = util.read_json(call_path); source(call_path)
                    outcomes[call['outcome']] += 1; call_cost += call['cost_micro']
                    for item in call['attempts']:
                        lid = item['ledger_id']
                        if lid in records_seen or lid in seen_ledger or lid not in by_id:
                            raise ValueError('provider attempt is missing or counted twice')
                        records_seen.add(lid); row = by_id[lid]
                        expected = {'call_id':call['call_id'], 'n':item['n'], 'status':item['ledger_status'],
                                    'spent_micro':item['spent_micro'], 'reserved_micro':item['reserved_micro']}
                        if any(row[k] != value for k,value in expected.items()):
                            raise ValueError('gateway record and ledger differ')
                if records_seen != set(by_id):
                    raise ValueError('ledger attempt has no gateway evidence')
                seen_ledger.update(records_seen); rows.extend(part); seen_attempts.add(jid)
            expected_ids = {r[0] for r in db.execute('SELECT DISTINCT job_id FROM calls WHERE job_key=?', (job['job_key'],))}
            if not expected_ids <= seen_attempts:
                raise ValueError('job history omits a charged attempt')
            result = settle(rows, prices)
            if call_cost != result['usage_priced_micro_usd']:
                raise ValueError('gateway cost and ledger sum differ')
            jobs.append({'job_key':job['job_key'], 'dataset':job['dataset'], 'phase':job['phase'],
                         'sample':job['sample'],'repeat':job['repeat'], 'attempt_count':len(job['attempts']),
                         'logical_call_outcomes':dict(sorted(outcomes.items())), **result})
            all_rows.extend(rows)
    finally:
        db.close()
    if not jobs:
        raise ValueError('no selected jobs')
    totals = settle(all_rows, prices)
    by_dataset = {}
    for ds in sorted({j['dataset'] for j in jobs}):
        selected = [j for j in jobs if j['dataset']==ds]
        by_dataset[ds] = {key:sum(j[key] for j in selected) for key in
                          ('usage_priced_micro_usd','unknown_cost_upper_micro_usd')}
    return {'schema':'mhforge.inference-cost-analysis/1','campaign':manifest['campaign'],
            'system':system,'phases':sorted(phases),'frozen_manifest_sha256':util.sha256_bytes(util.canonical_json(manifest)),
            'prices':manifest['prices'], 'jobs':jobs,'datasets':by_dataset,'totals':totals,
            'source_evidence':evidence,'source_evidence_sha256':util.sha256_bytes(util.canonical_json(evidence)),
            'script_sha256':util.sha256_file(Path(__file__)),
            'scope':'Marginal inference for every recorded attempt of these jobs, including failures and retries.',
            'excluded':'Earlier sources of replays, sandbox compute, proposer, telemetry and development costs.',
            'invoice_reconciliation':'Unavailable. Costs are computed from observed usage and frozen prices; unknown usage remains bounded, never zero.',
            'latency_comparison':'Not estimated: campaigns overlapped and experienced provider throttling and transport interruptions.'}


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--state', type=Path, required=True); p.add_argument('--system', required=True)
    p.add_argument('--phase', nargs='+', required=True); p.add_argument('--output', type=Path, required=True)
    a=p.parse_args(); result=inspect(a.state,a.system,set(a.phase)); util.write_json(a.output,result,fsync=True)
    print(json.dumps({k:result[k] for k in ('campaign','system','phases','datasets','totals')},indent=2))
