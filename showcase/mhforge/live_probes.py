# SPDX-License-Identifier: MIT
"""Billable infrastructure checks with durable handles and explicit evidence.

Relay soak traffic is synthetic, never inference. It exercises the real sandbox
transport and echoes exact UTF-8 bytes through the production relay protocol.
"""
from __future__ import annotations

import copy
import io
import json
import tarfile
import threading
import time
from dataclasses import asdict
from pathlib import Path

from . import bundle, util, wire, workspace
from .executors import JobSpec, classify_attempt
from .executors.sandbox import SandboxExecutor
from .journal import Journal

SOAK_SECONDS = 600
SOAK_WORKER = r'''
import concurrent.futures, hashlib, json, os, pathlib, sys, time, urllib.request
out = pathlib.Path(sys.argv[sys.argv.index('--out') + 1])
seconds = float(sys.argv[sys.argv.index('--seconds') + 1])
base = os.environ['OPENROUTER_API_BASE'].rstrip('/')
payload = ('汉字é🙂' * 10000)  # 120,000 UTF-8 bytes, deliberately crosses stream chunks
rows = []
def one(i):
    data = json.dumps({'i': i, 'payload': payload}, ensure_ascii=False).encode()
    request = urllib.request.Request(base + '/chat/completions', data=data,
                                     headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=180) as r:
        reply = r.read()
        if r.status != 200 or reply != data:
            raise RuntimeError('echo mismatch')
    return {'i': i, 'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
started = time.monotonic()
with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
    batch = 0
    while time.monotonic() - started < seconds:
        rows.extend(pool.map(one, range(batch * 32, (batch + 1) * 32)))
        batch += 1
        (out / 'memory.json').write_text(json.dumps({'rows': rows}))
        time.sleep(min(20, max(0, seconds - (time.monotonic() - started))))
(out / 'val.json').write_text(json.dumps({'accuracy': 1.0, 'correct': len(rows), 'total': len(rows),
                                         'duration_s': time.monotonic() - started, 'synthetic': True}))
'''


class EchoGateway:
    def __init__(self, ledger):
        self.ledger, self.calls, self.lock = ledger, {}, threading.Lock()

    def register_job(self, job, *, token=None):
        self.job_id = job.job_id

    def close_job(self, job_id, **kwargs):
        return {'calls': len(self.calls), 'in_flight_at_close': 0}

    def halted(self):
        return self.ledger.halted()

    def kill_requested(self, job_id):
        return None

    def handle(self, job, request, **kwargs):
        method, path, body = wire.parse_envelope(request)
        if job.job_id != self.job_id or method != 'POST' or path != '/v1/chat/completions':
            return wire.refusal_reply('not_allowed')
        doc = json.loads(body)
        with self.lock:
            self.calls.setdefault(doc['i'], []).append(util.sha256_bytes(body))
        return wire.GatewayReply(200, (('content-type', 'application/json'),), body)


class DisconnectExecutor(SandboxExecutor):
    """Drop one real exec stream; the ordinary executor must reattach itself."""
    def _attach_bridge(self, sbx, run, lifetime):
        super()._attach_bridge(sbx, run, lifetime)
        if len(run.session.bridges) == 1:
            timer = threading.Timer(1, run.session.bridges[0].detach)
            timer.daemon = True
            timer.start()


def _soak_bundle(dest):
    members = {'pkg/text_classification/__init__.py': b'',
               'pkg/text_classification/inner_loop.py': SOAK_WORKER.encode()}
    for name in bundle.INSANDBOX_FILES:
        members['bin/' + name] = (Path(__file__).parent / 'insandbox' / name).read_bytes()
    with tarfile.open(dest, 'w', format=tarfile.USTAR_FORMAT) as tar:
        for name, data in sorted(members.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o644
            tar.addfile(info, io.BytesIO(data))


def sandbox(ctx):
    m, paths = ctx.manifest, ctx.paths
    ex = ctx.executor
    if not isinstance(ex, SandboxExecutor):
        return {'passed': False, 'reason': 'requires the live sandbox executor'}
    root = paths.qualification_dir(ctx.attempt) / 'sandbox-probe'
    root.mkdir(parents=True, exist_ok=True)
    journal = Journal(paths.journal)
    handles = []
    key = 'sandbox-probe-' + str(ctx.attempt)
    journal.record('import_check', key, 'started', attempt=ctx.attempt)
    def on_handle(handle):
        handles.append(handle)
        journal.record('import_check', key, 'handle', attempt=ctx.attempt, detail=handle)
    ex.cleanup_orphans(journal.open_handles())
    example = (ctx.sc_dir / m['upstream']['example_dir']).absolute()
    workspace.ensure_workspace(m, paths, example, ctx.sc_dir / 'worker-image/uv.lock')
    payload = root / 'imports.tar'
    bundle.build_bundle(example, paths.ws, m['qualification']['dataset'], m, payload)
    try:
        imports = ex.import_check(m['upstream']['expect']['baselines'], payload, root / 'imports', on_handle,
                                  timeouts_s=[m['executor']['import_check_timeout_s']])
        if not all(r.ok for r in imports.values()):
            return {'passed': False, 'imports': {k: asdict(v) for k, v in imports.items()}}
        probe_manifest = copy.deepcopy(m)
        probe_manifest['relay']['rotate_after_mb'] = 8
        echo = EchoGateway(ctx.ledger)
        soak_ex = DisconnectExecutor(probe_manifest, paths, client=ctx.sandbox_client, gateway=echo, ledger=ctx.ledger)
        payload = root / 'soak.tar'
        _soak_bundle(payload)
        job = wire.JobIdentity(m['campaign'], 'val', 'relay_probe', m['qualification']['dataset'], ctx.attempt,
                               'qualification', None)
        spec = JobSpec(job, ('--out', '{out}', '--seconds', str(SOAK_SECONDS)), 2, SOAK_SECONDS + 180,
                       (), ('val.json',), False, payload, util.sha256_file(payload))
        try:
            result = soak_ex.run(spec, root / 'soak', on_handle)
        finally:
            soak_ex.close()
        score = util.read_json(root / 'soak/out/val.json') if (root / 'soak/out/val.json').exists() else {}
        rows = util.read_json(root / 'soak/out/memory.json').get('rows', []) if (root / 'soak/out/memory.json').exists() else []
        classification = classify_attempt(result, result_usable=bool(score), halted=bool(ctx.ledger.halted()))
        relay = result.facts.get('relay', {})
        exact = bool(rows) and len(rows) == len(echo.calls) and all(echo.calls.get(row['i']) == [row['sha256']] for row in rows)
        soak_ok = (classification[0] == 'ok' and exact and score.get('duration_s', 0) >= SOAK_SECONDS
                   and relay.get('attaches', 0) >= 3 and relay.get('rotations', 0) > 0
                   and 'detached' in relay.get('bridge_ends', []))
        evidence = {'imports': {k: asdict(v) for k, v in imports.items()}, 'soak': {'passed': soak_ok,
                    'synthetic_requests': len(rows), 'exactly_once_echo': exact, 'duration_s': score.get('duration_s'),
                    'classification': classification, 'executor': result.facts}, 'inference_calls': 0}
        util.write_json(root / 'evidence.json', evidence, fsync=True)
        if not soak_ok:
            return {'passed': False, **evidence}
        # A separate idle sandbox must expire without a stop request from this process.
        tags = ('mhf', 'mhf-c-' + m['campaign'], 'mhf-p-lifetime')
        sbx = ex._create_running(60, tags, on_handle, {'purpose': 'lifetime_probe'}, [])
        started = time.monotonic()
        expired = False
        try:
            while time.monotonic() - started < 150:
                if sbx.id not in ctx.sandbox_client.list_ids(list(tags)):
                    expired = True
                    break
                time.sleep(5)
        finally:
            ex._dispose(sbx)
        evidence['lifetime'] = {'passed': expired, 'max_lifetime_s': 60, 'observed_wait_s': time.monotonic() - started}
        return {'passed': expired and soak_ok, **evidence}
    finally:
        ex.cleanup_orphans(handles)
        journal.record('import_check', key, 'complete', attempt=ctx.attempt, detail={'cleanup_verified': True})


def rehearsal_review(ctx, export_check):
    """A live repeat of the completed baseline must not create another provider call."""
    before = ctx.ledger.snapshot()
    result = ctx.run_baselines()
    after = ctx.ledger.snapshot()
    stable = all(before.get(k) == after.get(k) for k in
                 ('requests', 'observed_micro', 'bounded_unknown_micro', 'in_flight_micro', 'meters'))
    remote = export_check(ctx)
    return {'passed': result.get('status') == 'complete' and stable and remote.get('passed') is True,
            'resume_spends_nothing': stable, 'remote_readback': remote,
            'scope': 'live baseline resume and cleanup; process-kill recovery covered by offline fault tests'}
