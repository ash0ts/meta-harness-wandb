# SPDX-License-Identifier: MIT
"""Real upstream workers through loopback HTTP, with no external inference or SDKs."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import audit, manifest, procenv, qualification, scan, util, workspace
from .driver import Driver, build_runtime
from .errors import MhforgeError, SmokeFailed
from .paths import resolve_state
from .provider import ForgeProviderClient
from .stubprovider import serve_stub
from .telemetry.exporter import Exporter


PROFILE = '(version 1)(allow default)(deny network*)(allow network-inbound (local ip "localhost:*"))(allow network-outbound (remote ip "localhost:*"))'


def network_guard_prefix():
    if sys.platform != 'darwin' or not Path('/usr/bin/sandbox-exec').exists():
        return (), 'none'
    prefix = ('/usr/bin/sandbox-exec', '-p', PROFILE)
    result = subprocess.run([*prefix, '/usr/bin/true'], env=procenv.minimal_env({}, home='/private/tmp'),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return (prefix, 'sandbox-exec') if result.returncode == 0 else ((), 'none')


def run_offline_smoke(sc_dir, *, executor='local_relay', example_dir=None, keep=False):
    sc_dir = Path(sc_dir).absolute()
    example = Path(example_dir).absolute() if example_dir else (sc_dir.parent / 'reference_examples/text_classification').absolute()
    python = example / '.venv/bin/python'
    if not python.is_file():
        raise MhforgeError('example Python environment missing; install the example dependencies first')
    before = workspace.source_digest(example)[0]
    scratch = sc_dir / 'state/_smoke/sc'
    scratch.mkdir(parents=True, exist_ok=True)
    engine = qualification.engine_snapshot(Path(__file__).parent)
    campaign = 'smoke-' + util.utc_now().replace('-', '').replace(':', '').replace('.', '').lower()
    m = manifest.read_manifest(sc_dir / 'manifest.draft.json')
    m.update(campaign=campaign, state_dir='state/' + campaign)
    m['upstream'].update(example_dir=str(example), worker_python=str(python), tree_sha256=before)
    m['search']['iterations'] = 0
    m['executor']['kind'] = executor
    m['telemetry']['export_mode'] = 'manual'
    key = 'mhforge-offline-canary-not-a-live-credential-101'
    prefix, guard = network_guard_prefix()
    failures = []
    runtime = None
    server = None
    try:
        with util.loopback_guard():
            server, url = serve_stub()
            m['provider'].update(kind='stub', base_url=url)
            source = scratch / 'manifest.json'
            util.write_json(source, m)
            manifest.require_valid(m, source)
            paths = resolve_state(m, source)
            manifest.freeze(m, paths, engine)
            key_name = m['provider']['api_key_env']
            util.append_key_digest(paths.keydigests, key_name, key)
            def make_driver():
                nonlocal runtime
                runtime = build_runtime(m, source, provider=ForgeProviderClient(m, environ={key_name: key}),
                                        environ={}, command_prefix=prefix)
                return Driver(runtime, base_env={}, home=paths.scratch_root / 'home')
            first = make_driver().run_baselines()
            if first['status'] != 'complete':
                raise SmokeFailed(invariants=['baseline_run:' + str(first)])
            count = len(server.calls)
            export = Exporter(m, paths, ledger=None, lens=None, wandb=None, dry_run=True,
                              example_dir=example, known_secrets=[key.encode()]).export_all()
            snap = runtime.ledger.snapshot()
            second = make_driver().run_baselines()
            records = [util.read_json(p) for p in paths.jobs_root.glob('*/job.json')]
            expected = len(m['upstream']['expect']['baselines']) * sum(v[1] for v in m['upstream']['expect']['sizes'].values())
            call_records = [util.read_json(p) for p in paths.calls_root.glob('**/*.json')]
            if len(records) != 6 or any(r.get('status') != 'succeeded' for r in records):
                failures.append('six_successful_jobs')
            if count != expected or len(call_records) != expected or any(c.get('outcome') != 'completed' for c in call_records):
                failures.append('exact_call_count')
            if second['status'] != 'complete' or len(server.calls) != count or runtime.ledger.snapshot() != snap:
                failures.append('resume_spends_nothing')
            if runtime.ledger.verify_totals() or runtime.ledger.halted():
                failures.append('ledger')
            if export['deferred'] or any(s != 'dry_run' for unit in export['statuses'].values() for s in unit.values()):
                failures.append('dry_export')
            audit_result = audit.audit(m, paths)
            if not audit_result['ok']:
                failures.append('audit:' + ','.join(audit_result['problems']))
            if scan.scan_paths([paths.root], known=[key.encode()], digests=scan.load_digests(paths.keydigests)):
                failures.append('secret_scan')
            if workspace.source_digest(example)[0] != before:
                failures.append('upstream_changed')
            receipt = {'schema': 'mhforge.smoke/1', 'passed': not failures, 'engine_sha256': engine['engine_sha256'],
                'executor': executor, 'worker_network_guard': guard, 'provider_calls': count,
                'resume_provider_calls': len(server.calls) - count, 'jobs': len(records),
                'dry_run_units': len(export['statuses']), 'audit': audit_result, 'invariants_failed': failures,
                'utc': util.utc_now(), 'state': str(paths.root) if keep else None}
            if failures:
                raise SmokeFailed(invariants=failures)
            util.write_json(sc_dir / 'state/_smoke/receipt.json', receipt, fsync=True)
            if not keep:
                shutil.rmtree(paths.root)
            return receipt
    finally:
        if runtime:
            runtime.executor.close()
            runtime.ingress.close_all()
        if server:
            server.close()
