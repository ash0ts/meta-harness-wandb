# SPDX-License-Identifier: MIT
"""CLI gates, credential entry and operator controls for the Forge showcase."""
from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import socket
import sys
import tempfile
from pathlib import Path

from . import audit, manifest as manifests, preflight, procenv, qualification, util, workspace
from .errors import CampaignStop, LiveGateError, ManifestError, MhforgeError, exit_code_for
from .journal import Journal
from .ledger import Ledger
from .paths import resolve_state
from .state import StateLock, status

KEY_ROLES = ('provider', 'sandbox', 'export')
KEY_PATHS = {'provider': 'provider.api_key_env', 'sandbox': 'sandbox.auth_env', 'export': 'telemetry.api_key_env'}


def parse_key_source(text):
    if text in ('prompt', 'env'):
        return text, None
    if text.startswith('fd:') and text[3:].isdigit():
        return 'fd', int(text[3:])
    raise argparse.ArgumentTypeError('key source must be prompt, env or fd:N')


def roles_needed(command, manifest, *, mode=None):
    if command in ('export', 'publish'):
        return ('export',)
    roles = ['provider']
    if manifest['executor']['kind'] == 'sandbox':
        roles.append('sandbox')
    if command == 'preflight' or (command != 'account-check' and manifest['telemetry']['export_mode'] == 'after_unit'):
        roles.append('export')
    return tuple(roles)


def _fd_bytes(fd):
    try:
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, 4096)
            if not chunk:
                return b''.join(chunks)
            size += len(chunk)
            if size > 65536:
                raise LiveGateError(blockers=[('key-source', 'credential input exceeds size limit')])
            chunks.append(chunk)
    finally:
        os.close(fd)


def read_keys(source, roles, manifest, environ, *, getpass_fn=getpass.getpass, read_fd=None):
    kind, fd = source
    values, previous = {}, ''
    if kind == 'fd':
        try:
            for line in (read_fd or _fd_bytes)(fd).decode('utf-8').splitlines():
                if not line:
                    continue
                role, sep, value = line.partition('=')
                if not sep or role not in KEY_ROLES + ('all',) or role in values:
                    raise ValueError
                values[role] = value
        except (ValueError, UnicodeError):
            raise LiveGateError(blockers=[('key-source', 'expected unique role=key lines')]) from None
    result = environ if kind == 'env' else {}
    for role in roles:
        name = util.lookup(manifest, KEY_PATHS[role])
        if kind == 'prompt':
            value = getpass_fn(f'{role} ({name}; Enter reuses previous): ') or previous
            previous = value
        elif kind == 'fd':
            value = values.get(role, values.get('all', ''))
        else:
            value = environ.get(name, '')
        if not value or any(c in value for c in '\r\n\x00'):
            raise LiveGateError(blockers=[(role, 'credential missing or invalid')])
        result[name] = value
    return result


@contextlib.contextmanager
def sdk_process_environment(paths):
    """LD9: every live SDK gets an empty home and a working directory without settings."""
    homes = paths.export_root / 'homes'
    homes.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix='cli-', dir=homes)).absolute()
    os.chmod(home, 0o700)
    old_env, old_cwd = dict(os.environ), Path.cwd()
    clean = procenv.minimal_env(old_env, home=str(home))
    paths.export_netrc.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(paths.export_netrc, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    clean.update(HOME=str(home), WANDB_CONFIG_DIR=str(home), NETRC=str(paths.export_netrc))
    os.environ.clear()
    os.environ.update(clean)
    os.chdir(home)
    try:
        yield
    finally:
        os.chdir(old_cwd)
        os.environ.clear()
        os.environ.update(old_env)


def parser():
    p = argparse.ArgumentParser(prog='mhforge')
    sub = p.add_subparsers(dest='command', required=True)
    commands = {}
    for name in ('status', 'halt', 'validate-manifest', 'account-check', 'preflight', 'search', 'evaluate', 'export', 'amend', 'publish', 'audit'):
        c = sub.add_parser(name)
        c.add_argument('--manifest', type=Path, required=True)
        commands[name] = c
    for name in manifests.COMMANDS:
        commands[name].add_argument('--live', action='store_true')
        commands[name].add_argument('--key-source', type=parse_key_source)
    c = commands['status']
    group = c.add_mutually_exclusive_group()
    group.add_argument('--clear-halt', metavar='NOTE')
    group.add_argument('--reconcile', metavar='LEDGER_ID')
    group.add_argument('--reset-job', metavar='JOB_KEY')
    c.add_argument('--spent-usd')
    c.add_argument('--note')
    c.add_argument('--from-billing-export', action='store_true')
    commands['halt'].add_argument('--note', required=True)
    c = commands['validate-manifest']
    c.add_argument('--pin-upstream', action='store_true')
    c.add_argument('--freeze', action='store_true')
    commands['account-check'].add_argument('--create-project', action='store_true')
    for name in ('claude-spent-usd', 'claude-limit-usd', 'claude-reserve-usd'):
        commands['account-check'].add_argument('--' + name)
    c = commands['preflight']
    c.add_argument('--only', nargs='+')
    c.add_argument('--teardown', action='store_true')
    for name in ('platform-inference-usd', 'platform-sandbox-usd', 'waive-reconciliation'):
        c.add_argument('--' + name)
    commands['search'].add_argument('--max-iterations-this-run', type=int)
    c = commands['evaluate']
    group = c.add_mutually_exclusive_group(required=True)
    for mode in manifests.EVALUATE_MODES:
        group.add_argument('--' + mode, dest='mode', action='store_const', const=mode)
    c.add_argument('--end-search', action='store_true')
    c = commands['export']
    c.add_argument('--dry-run', action='store_true')
    c.add_argument('--unit', nargs='+')
    c.add_argument('--retry-partial', action='store_true')
    c.add_argument('--retry-budget', action='store_true',
                   help='Retry budget-deferred exports against existing recovery and total caps')
    c.add_argument('--supersede', nargs='+', default=[])
    commands['amend'].add_argument('--set', nargs='+', required=True)
    commands['amend'].add_argument('--note', required=True)
    c = sub.add_parser('offline-smoke')
    c.add_argument('--executor', choices=('local', 'local_relay'), default='local_relay')
    c.add_argument('--example-dir', type=Path)
    c.add_argument('--keep', action='store_true')
    return p


def _emit(obj):
    print(json.dumps(obj, indent=2, sort_keys=True))


def _result_code(result):
    if result.get('status') == 'stopped':
        return 2
    if result.get('status') in ('error', 'incomplete', 'unverified') or result.get('passed') is False:
        return 1
    return 0


@contextlib.contextmanager
def runner_failure_record(paths, command):
    """Initialization and teardown can fail outside Driver._execute too."""
    try:
        yield
    except (Exception, CampaignStop, KeyboardInterrupt) as exc:
        stopped = isinstance(exc, (CampaignStop, KeyboardInterrupt))
        reason = exc.reason if isinstance(exc, CampaignStop) else ('interrupted' if stopped else 'internal_error')
        row = {'status': 'stopped' if stopped else 'error', 'reason': reason,
               'exception_type': type(exc).__name__, 'command': command, 'utc': util.utc_now()}
        if command == 'preflight':
            row['phase'] = 'qualification'
        util.write_json(paths.stopped, row, fsync=True)
        Journal(paths.journal).record('run', command, 'stopped', detail=row)
        raise


def _export(args, m, paths, keys, hostname):
    from .telemetry.exporter import Exporter
    lens = wandb = None
    dry = args.dry_run
    if not dry:
        from .telemetry.adapter_agentlens import CoreweaveSink
        from .telemetry.adapter_wandb import WandbSdkSink
        if m['telemetry']['agent_lens']['enabled']:
            lens = CoreweaveSink(m, environ=dict(keys))
        wandb = WandbSdkSink(m, environ=dict(keys), paths=paths)
    amendments = manifests.read_amendments(paths, manifests.manifest_sha256(m))
    effective = manifests.effective(m, amendments)
    exporter = Exporter(effective, paths, ledger=None if dry else Ledger(paths.ledger, m), lens=lens,
        wandb=wandb, known_secrets=[v.encode() for v in keys.values()], hostname=hostname,
        example_dir=(args.manifest.parent / m['upstream']['example_dir']).absolute() if paths.pristine.exists() else None,
        amendment=amendments[-1] if amendments else None, dry_run=dry)
    out = exporter.export_all(only=args.unit, retry_partial=args.retry_partial, retry_budget=args.retry_budget,
                              supersede=args.supersede)
    _emit(out)
    failed = out['not_ready'] or out['deferred'] or any(
        value not in ('dry_run', 'verified', 'skipped_policy') for surfaces in out['statuses'].values() for value in surfaces.values())
    return 1 if failed else 0


def _runner(args, m, paths, keys, base_env, home, hostname):
    from .driver import Driver, build_runtime
    from .provider import ForgeProviderClient
    from .telemetry.spawn import ExportSpawner
    spawner = None
    if m['telemetry']['export_mode'] == 'after_unit':
        export_name = m['telemetry']['api_key_env']
        export_value = keys[export_name]
        spawner = ExportSpawner(m, args.manifest, paths, journal=Journal(paths.journal), base_env=base_env,
            package_root=Path(__file__).parent.parent, export_key=lambda: export_value)
    def make_driver():
        rt = build_runtime(m, args.manifest, provider=ForgeProviderClient(m, environ=dict(keys)), environ=dict(keys))
        return Driver(rt, base_env=base_env, home=home, export_hook=spawner.submit if spawner else None)
    with StateLock(paths.runner_lock, args.command, host=hostname), runner_failure_record(paths, args.command):
        current = None
        try:
            if args.command == 'preflight':
                from . import live_probes
                # Each phase gets a fresh gateway; a completed Driver has aborted its old gateway.
                def run(method):
                    nonlocal current
                    if current is not None:
                        current.shutdown()
                    current = make_driver()
                    return getattr(current, method)()
                def diagnostic(method):
                    result = run(method)
                    return {'passed': result.get('status') == 'complete',
                            'research_status': 'diagnostic; does not qualify search', **result}
                current = make_driver()
                from .telemetry import probe as telemetry_probe
                export_check = lambda ctx: telemetry_probe.run(ctx, keys, drain=spawner.drain if spawner else None)
                ctx = preflight.Context(m, paths, {}, args.manifest.parent,
                    attempt=qualification.next_attempt(paths), gateway=current.rt.gateway, ledger=current.rt.ledger,
                    executor=current.rt.executor, sandbox_client=getattr(current.rt.executor, 'client', None),
                    run_qualification_job=lambda: run('run_qualification_job'), run_baselines=lambda: run('run_baselines'),
                    run_calibration=lambda: run('run_calibration'), hostname=hostname,
                    user_figures={k: v for k, v in vars(args).items() if k.startswith(('platform_', 'waive_')) and v},
                    probes={'sandbox': live_probes.sandbox, 'telemetry': export_check,
                            'proposer_probes': lambda ctx: run('run_proposer_probe'),
                            'baseline_noise': lambda ctx: diagnostic('run_noise'),
                            'sampling_controls': lambda ctx: diagnostic('run_controls'),
                            'engineering_controls': lambda ctx: diagnostic('run_engineering'),
                            'rehearsal_review': lambda ctx: live_probes.rehearsal_review(ctx, export_check)})
                if args.teardown:
                    current.rt.executor.cleanup_orphans(current.rt.journal.open_handles())
                    out = {'teardown': preflight.TEARDOWN, 'status': 'complete'}
                else:
                    out = preflight.run_checks(ctx, args.only)
            else:
                current = make_driver()
                out = current.run_search(max_iterations_this_run=args.max_iterations_this_run) if args.command == 'search' else (
                    current.run_test(end_search=args.end_search) if args.mode == 'test' else getattr(current, 'run_' + args.mode)())
        finally:
            if current:
                current.shutdown()
            if spawner:
                spawner.close()
    _emit(out)
    return _result_code(out)


def _main(args):
    if args.command == 'offline-smoke':
        from .smoke import run_offline_smoke
        _emit(run_offline_smoke(Path(__file__).parent.parent, executor=args.executor,
                                example_dir=args.example_dir, keep=args.keep))
        return 0
    args.manifest = args.manifest.absolute()
    m = manifests.read_manifest(args.manifest)
    manifests.require_valid(m, args.manifest)
    paths = resolve_state(m, args.manifest)
    if args.command == 'status':
        if args.clear_halt or args.reconcile or args.reset_job:
            note = args.clear_halt or args.note
            if not note or not note.strip():
                raise ManifestError(problems=['operator changes need a nonempty note'])
            if not paths.ledger.exists():
                raise ManifestError(problems=['campaign ledger does not exist'])
            with StateLock(paths.runner_lock, 'status'):
                ledger, journal = Ledger(paths.ledger, m), Journal(paths.journal)
                if args.clear_halt:
                    ledger.clear_halt(note)
                    journal.record('halt', 'cleared', 'complete', detail={'note': note})
                elif args.reconcile:
                    if args.spent_usd is None:
                        raise ManifestError(problems=['--reconcile requires --spent-usd'])
                    ledger.reconcile(args.reconcile, util.micros(args.spent_usd), note, below_reservation=args.from_billing_export)
                    journal.record('halt', 'reconciled', 'complete', detail={'ledger_id': args.reconcile, 'note': note})
                else:
                    util.safe_component(args.reset_job)
                    journal.record('job', args.reset_job, 'reset', detail={'note': note})
        out = status(m, paths)
        if out['stall']['alarm']:
            print('Warning: campaign journal has stalled.', file=sys.stderr)
        _emit(out)
        return 0
    if args.command == 'halt':
        if not paths.ledger.exists():
            raise ManifestError(problems=['campaign ledger does not exist'])
        Ledger(paths.ledger, m).set_halt('operator', args.note)
        _emit({'halt': 'operator'})
        return 0
    if args.command == 'audit':
        out = audit.audit(m, paths)
        _emit(out)
        return 0 if out['ok'] else 1
    if args.command == 'validate-manifest':
        out = {'valid': True}
        if args.pin_upstream:
            example = (args.manifest.parent / m['upstream']['example_dir']).absolute()
            out['upstream.tree_sha256'] = workspace.source_digest(example)[0]
            lock = args.manifest.parent / 'worker-image' / 'uv.lock'
            out['upstream.worker_lock_sha256'] = util.sha256_bytes(lock.read_bytes()) if lock.exists() else None
        if args.freeze:
            with StateLock(paths.runner_lock, 'freeze'):
                out['manifest_sha256'] = manifests.freeze(m, paths, qualification.engine_snapshot(Path(__file__).parent))
        _emit(out)
        return 0
    if args.command == 'amend':
        m, paths = manifests.load_frozen(args.manifest)
        changes = {}
        for item in args.set:
            key, sep, value = item.partition('=')
            if not sep or key in changes:
                raise ManifestError(problems=['--set needs unique PATH=JSON entries'])
            changes[key] = json.loads(value)
        with StateLock(paths.export_lock, 'amend'):
            row = manifests.append_amendment(m, paths, changes, args.note)
            Journal(paths.journal).record('amendment', str(row['seq']), 'appended', detail={'row_sha256': row['row_sha256']})
        _emit({'seq': row['seq'], 'row_sha256': row['row_sha256']})
        return 0
    if args.command == 'export' and args.dry_run:
        if args.live:
            raise ManifestError(problems=['choose --live or --dry-run'])
        return _export(args, m, paths, {}, '')
    source = args.key_source or ('prompt' if sys.stdin.isatty() else 'env', None)
    if not args.live:
        out = {'blockers': manifests.live_blockers(m, args.command, os.environ, paths, live_flag=False,
                key_source=source[0], mode=getattr(args, 'mode', None))}
        if args.command in ('preflight', 'account-check'):
            out['checks'] = preflight.TEARDOWN if getattr(args, 'teardown', False) else preflight.describe()
        _emit(out)
        return 3
    if args.command != 'account-check':
        m, paths = manifests.load_frozen(args.manifest)
    engine = qualification.engine_snapshot(Path(__file__).parent)
    gates = qualification.gate_problems(qualification.offline_gate(m, paths, args.manifest.parent, command=args.command)) if args.command in manifests.RUNNER_COMMANDS else []
    manifests.require_live(m, args.command, os.environ, paths, live_flag=True, engine_sha256=engine['engine_sha256'],
        gate_problems=gates, key_source=source[0], mode=getattr(args, 'mode', None),
        end_search=getattr(args, 'end_search', False), create_project=getattr(args, 'create_project', False))
    roles = roles_needed(args.command, m, mode=getattr(args, 'mode', None))
    keys = read_keys(source, roles, m, os.environ)
    # Keep only required role values, never a copy of the entire credential environment.
    private = {util.lookup(m, KEY_PATHS[r]): keys[util.lookup(m, KEY_PATHS[r])] for r in roles}
    scrubbed = {}
    for name, value in private.items():
        util.append_key_digest(paths.keydigests, name, value)
        scrubbed[name] = util.scrub_process_env(name) if source[0] == 'env' else None
    keys = private
    home = Path(os.environ.get('HOME', str(paths.scratch_root))).absolute()
    base_env = {k: v for k, v in os.environ.items() if k in set(m['proposer']['env_allow']) | {'PATH'}}
    hostname = socket.gethostname()
    util.write_json(paths.runtime, {'pid': os.getpid(), 'command': args.command,
        'key_source': source[0], 'key_scrubbed': scrubbed}, fsync=True)
    with sdk_process_environment(paths):
        if args.command == 'export':
            return _export(args, m, paths, keys, hostname)
        if args.command == 'account-check':
            account = preflight.WandbAccountReader(api_key=keys[m['provider']['api_key_env']], user_agent=m['provider']['user_agent'])
            client = None
            if m['executor']['kind'] == 'sandbox':
                from .executors.sandbox_client import CwSandboxClient
                client = CwSandboxClient(m, environ=dict(keys))
            out = preflight.account_check(preflight.Context(m, paths, {}, args.manifest.parent,
                account=account, sandbox_client=client), create_project=args.create_project)
            _emit(out)
            return _result_code(out)
        if args.command == 'publish':
            from .publish import bundle
            amendments = manifests.read_amendments(paths, manifests.manifest_sha256(m))
            effective = manifests.effective(m, amendments)
            with StateLock(paths.export_lock, 'publish'):
                out = bundle(effective, paths, paths.publish_root / ('bundle-' + util.utc_now().replace(':', '-')),
                             known_secrets=[v.encode() for v in keys.values()], hostname=hostname)
            _emit({'status': 'unverified', 'bundle': out,
                   'reason': 'Public visibility, Report and saved views require the unverified surface adapters; no remote publication occurred.'})
            return 3
        return _runner(args, m, paths, keys, base_env, home, hostname)


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return _main(args)
    except CampaignStop as exc:
        _emit({'status': 'stopped', 'reason': exc.reason})
        return 2
    except KeyboardInterrupt:
        _emit({'status': 'stopped', 'reason': 'interrupted'})
        return 2
    except Exception as exc:
        # SDK and filesystem exceptions may include credentials; only structured safe fields are printed.
        if isinstance(exc, LiveGateError):
            _emit({'blockers': exc.blockers})
        elif isinstance(exc, ManifestError):
            _emit({'problems': exc.problems})
        else:
            _emit({'error': type(exc).__name__})
        return exit_code_for(exc)
