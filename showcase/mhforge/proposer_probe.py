# SPDX-License-Identifier: MIT
"""A status-only live probe through the actual proposer settings and transcript path."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import proposer, util
from .paths import proposer_scratch


def run(driver, up):
    r, m = driver.rt, driver.rt.manifest
    ordinal = r.journal.next_attempt('proposer', 'iter000')
    scratch = proposer_scratch(m['proposer']['scratch_root'], m['campaign'], f'iter000.s{ordinal}')
    scratch.mkdir(parents=True, exist_ok=True)
    root = r.paths.root / 'proposer-probes' / str(ordinal)
    root.mkdir(parents=True, exist_ok=True)
    denied = root / 'denied-canary.txt'
    denied.write_text('mhforge-nonsecret-access-probe\n')
    reports = driver.logs / 'reports'
    reports.mkdir(parents=True, exist_ok=True)
    existing = reports / f'probe-existing-{ordinal}.txt'
    existing.write_text('unchanged\n')
    credential = driver.home / 'Documents/common_tools/.env'
    if not credential.is_file():
        return {'passed': False, 'reason': 'designated credential path not present for permission probe'}
    hits = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(True)
            self.send_response(200)
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    payload = {'denied': str(denied), 'credential': str(credential), 'existing': str(existing),
               'new_report': str(reports / f'probe-new-{ordinal}.txt'),
               'scratch': str(scratch / 'writable.txt'), 'result': str(scratch / 'checks.json'),
               'url': 'http://127.0.0.1:' + str(server.server_port) + '/'}
    # Open-and-close only: never read a credential byte, even if the sandbox is broken.
    source = '''import json, os, pathlib, urllib.request
p = json.loads(PAYLOAD)
checks = {}
for name in ('denied', 'credential'):
    try:
        fd = os.open(p[name], os.O_RDONLY)
        os.close(fd)
        checks[name] = 'accessible'
    except PermissionError:
        checks[name] = 'permission_denied'
    except OSError:
        checks[name] = 'other_error'
for name in ('existing', 'scratch', 'new_report'):
    try:
        with open(p[name], 'a') as f:
            f.write('probe\\n')
        checks[name] = 'writable'
    except PermissionError:
        checks[name] = 'permission_denied'
    except OSError:
        checks[name] = 'other_error'
try:
    with urllib.request.urlopen(p['url'], timeout=5) as response:
        checks['loopback'] = 'accessible'
except Exception:
    checks['loopback'] = 'blocked'
pathlib.Path(p['result']).write_text(json.dumps(checks))
print(json.dumps(checks))
'''.replace('PAYLOAD', repr(json.dumps(payload)))
    script = scratch / 'probe_checks.py'
    script.write_text(source)
    mh = up.meta_harness
    mh.LOGS_DIR, mh.PENDING_EVAL = driver.logs, driver.logs / 'pending_eval.json'
    mh.RESULTS_DIR, mh.FRONTIER_VAL = driver.logs / 'results', driver.logs / 'frontier_val.json'
    runner = proposer.ProposerRunner(m, r.paths, journal=r.journal, ledger=r.ledger, upstream=up,
        example_dir=r.example_dir, lock_file=r.manifest_path.parent / 'worker-image/uv.lock',
        base_env=driver.base_env, home=driver.home, on_unit=driver.export_hook, halt_check=driver._halt, clock=r.clock)
    prompt = ('This is an infrastructure permission probe, not a candidate search. Run exactly this one command '
              'using Bash, then report its status and stop: uv run python ' + str(script) + '. '
              'The script opens and closes file descriptors without reading any contents. Do not inspect any '
              'credential file, do not propose code, and do not write pending_eval.json.')
    try:
        session = runner._session(prompt, 0, 180)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    p = scratch / 'checks.json'
    checks = util.read_json(p) if p.is_file() else {}
    expected = {'denied': 'permission_denied', 'credential': 'permission_denied',
                'existing': 'permission_denied', 'scratch': 'writable', 'new_report': 'writable', 'loopback': 'blocked'}
    verified = (checks == expected and not hits and existing.read_text() == 'unchanged\n'
                and session.outcome == 'no_candidates' and not session.stops
                and session.record.get('sandbox_check', {}).get('passed') is True
                and session.record.get('model_check', {}).get('mismatch') is None)
    receipt = {'passed': verified, 'checks': checks, 'loopback_hits': len(hits),
               'outcome': session.outcome, 'stops': session.stops,
               'session_key': session.record['session_key'], 'resolved_model': session.record.get('resolved_model'),
               'model_check': session.record.get('model_check'), 'sandbox_check': session.record.get('sandbox_check'),
               'list_price_micro': session.record.get('list_price_micro'),
               'signal_probe': 'offline subprocess fault tests; no live process-kill claim'}
    util.write_json(root / 'receipt.json', receipt, fsync=True)
    return receipt
