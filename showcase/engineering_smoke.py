#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Run every engineering arm with a stub, then verify zero-call resume and analysis."""
import argparse
import json
from pathlib import Path

from analyze_engineering import inspect
from mhforge import audit, manifest, qualification, util, workspace
from mhforge.driver import Driver, build_runtime
from mhforge.paths import resolve_state
from mhforge.smoke import network_guard_prefix
from mhforge.stubprovider import StubProvider
from mhforge.telemetry.exporter import Exporter


def run(example, destination):
    sc = Path(__file__).resolve().parent
    m = manifest.read_manifest(sc/'manifest.draft.json')
    name = 'engineering-smoke-'+util.utc_now().replace('-','').replace(':','').replace('.','').lower()
    m.update(campaign=name,state_dir='state/'+name)
    m['upstream'].update(example_dir=str(example),worker_python=str(example/'.venv/bin/python'),
                         tree_sha256=workspace.source_digest(example)[0])
    m['executor']['kind']='local_relay'
    m['provider'].update(kind='stub',base_url='http://127.0.0.1:1/v1')
    m['routing']['reply_reuse_scope']='job'
    m['telemetry']['export_mode']='manual'
    source=destination/name/'manifest.json';util.write_json(source,m)
    paths=resolve_state(m,source)
    engine=qualification.engine_snapshot(sc/'mhforge')
    manifest.freeze(m,paths,engine)
    provider=StubProvider();prefix,guard=network_guard_prefix()
    with util.loopback_guard():
        def driver():
            rt=build_runtime(m,source,provider=provider,environ={},command_prefix=prefix)
            return Driver(rt,base_env={},home=paths.scratch_root/'home')
        first=driver().run_engineering()
        if first['status']!='complete' or len(provider.calls)!=8760:
            raise AssertionError({'first':first,'calls':len(provider.calls)})
        before=len(provider.calls)
        second=driver().run_engineering()
        if second['status']!='complete' or len(provider.calls)!=before:
            raise AssertionError({'resume':second,'new_calls':len(provider.calls)-before})
        exported=Exporter(m,paths,ledger=None,lens=None,wandb=None,dry_run=True,example_dir=example).export_all()
        if exported['deferred'] or exported['not_ready'] or any(v!='dry_run' for surfaces in exported['statuses'].values() for v in surfaces.values()):
            raise AssertionError('offline export plan failed')
        output=destination/name/'analysis';output.mkdir(parents=True,exist_ok=True)
        summary=inspect(paths.root,output,offline=True)
        checked=audit.audit(m,paths)
        if not checked['ok'] or summary['all_outputs_regraded']!=8240:
            raise AssertionError({'audit':checked,'outputs':summary['all_outputs_regraded']})
    result={'schema':'mhforge.engineering-smoke/1','passed':True,'provider':'stub',
            'remote_readback_verified':False,'jobs':72,'scored_outcomes':8760,
            'test_outcomes_regraded':8240,'identical_request_groups':summary['identical_request_groups'],
            'resume_new_calls':len(provider.calls)-before,'dry_export_units':len(exported['statuses']),
            'engine_sha256':engine['engine_sha256'],'worker_network_guard':guard,'utc':util.utc_now()}
    util.write_json(destination/'receipt.json',result,fsync=True)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--example-dir',type=Path,default=Path(__file__).resolve().parents[1]/'reference_examples/text_classification')
    p.add_argument('--output',type=Path,default=Path(__file__).resolve().parent/'state/_engineering_smoke')
    a=p.parse_args();print(json.dumps(run(a.example_dir.resolve(),a.output.resolve()),indent=2))
