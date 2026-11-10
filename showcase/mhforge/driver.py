# SPDX-License-Identifier: MIT
"""Resumable upstream orchestration. No SDK imports or ambient credential reads."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from . import bundle, jobs, proposer, qualification, util, workspace
from .circuit import Circuit
from .errors import CampaignStop, UpstreamContractError
from .gateway import Gateway
from .ingress_http import LoopbackIngress
from .journal import Journal
from .ledger import Ledger
from .paths import StatePaths, resolve_state
from .replay import ReplayStore


@dataclass
class Runtime:
    manifest: dict
    paths: StatePaths
    manifest_path: Path
    example_dir: Path
    clock: object
    ids: object
    ledger: Ledger
    replay: ReplayStore
    circuit: Circuit
    gateway: Gateway
    ingress: LoopbackIngress
    executor: object
    journal: Journal
    tracker: jobs.PhaseTracker


def make_executor(manifest, paths, *, gateway, ingress, ledger, clock, ids, environ,
                  scrub=None, sandbox_client=None, command_prefix=()):
    kind = manifest['executor']['kind']
    common = dict(gateway=gateway, clock=clock)
    if kind == 'local':
        from .executors.local import LocalExecutor
        return LocalExecutor(manifest, paths, ingress=ingress, ids=ids, command_prefix=command_prefix, **common)
    if kind == 'local_relay':
        from .executors.local_relay import LocalRelayExecutor
        return LocalRelayExecutor(manifest, paths, mode='stdio', ids=ids, command_prefix=command_prefix, **common)
    if kind == 'sandbox':
        from .executors.sandbox import SandboxExecutor
        from .executors.sandbox_client import CwSandboxClient
        client = sandbox_client or CwSandboxClient(manifest, environ=environ, scrub=scrub)
        return SandboxExecutor(manifest, paths, client=client, ledger=ledger, **common)
    raise UpstreamContractError('unknown executor kind')


def build_runtime(manifest, manifest_path, *, provider, environ, scrub=None, executor=None,
                  sandbox_client=None, command_prefix=(), clock=None, ids=None):
    manifest_path = Path(manifest_path).absolute()
    paths = resolve_state(manifest, manifest_path)
    clock = clock or util.SystemClock()
    ids = ids or util.IdSource(clock)
    ledger = Ledger(paths.ledger, manifest, clock=clock)
    replay = ReplayStore(paths.replay, manifest['routing']['reply_reuse_scope'], clock=clock)
    circuit = Circuit(paths.circuit, threshold=manifest['provider']['circuit']['failures'],
                      cooldown_s=manifest['provider']['circuit']['cooldown_s'], clock=clock)
    gateway = Gateway(manifest, paths, provider=provider, ledger=ledger, replay=replay,
                      circuit=circuit, clock=clock, ids=ids)
    ingress = LoopbackIngress(gateway, manifest)
    executor = executor or make_executor(manifest, paths, gateway=gateway, ingress=ingress,
        ledger=ledger, clock=clock, ids=ids, environ=environ, scrub=scrub,
        sandbox_client=sandbox_client, command_prefix=command_prefix)
    example = (manifest_path.parent / manifest['upstream']['example_dir']).absolute()
    return Runtime(manifest, paths, manifest_path, example, clock, ids, ledger, replay,
                   circuit, gateway, ingress, executor, Journal(paths.journal, clock=clock), jobs.PhaseTracker())


def summary_rows(summary_path):
    """Keep upstream's tolerance for interrupted JSONL writes during recovery."""
    if not Path(summary_path).exists():
        return
    for line in Path(summary_path).read_text().strip().split('\n'):
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def count_iterations(summary_path):
    """Exactly upstream's summary arithmetic (including malformed JSON lines)."""
    return max([0] + [row.get('iteration', 0) for row in summary_rows(summary_path)])


def remaining_iterations(total, start_iteration):
    return max(0, total - start_iteration + 1)


def settled_iterations(paths, summary_path):
    """Failed proposals consume an iteration even when upstream writes no summary row."""
    highest = count_iterations(summary_path)
    for path in paths.iterations_root.glob('iter-*.json'):
        row = util.read_json(path)
        highest = max(highest, row['iteration'])
    return highest


def install(up, *, run_benchmark, propose, validate, summary):
    replacements = dict(run_benchmark=run_benchmark, validate_candidates=validate,
                        update_evolution_summary=summary)
    if propose is not None:
        replacements['propose_claude'] = propose
    originals = {k: getattr(up.meta_harness, k) for k in replacements}
    for k, v in replacements.items():
        setattr(up.meta_harness, k, v)
    def uninstall():
        for k, v in originals.items():
            setattr(up.meta_harness, k, v)
    return uninstall


def startup_recovery(rt):
    out = {'interrupted_calls': rt.ledger.interrupt_pending(), 'closed_jobs': rt.ledger.close_open_jobs()}
    # Capture handles before recover marks their operations interrupted.
    handles = rt.journal.open_handles()
    out['operations'] = len(rt.journal.recover())
    out['orphans'] = rt.executor.cleanup_orphans(handles)
    for row in rt.ledger.open_meter_rows('proposer_list_price'):
        rt.ledger.meter_close('proposer_list_price', row['key'], row['quantity'], micro_usd=row['micro_usd'])
    workspace.ensure_workspace(rt.manifest, rt.paths, rt.example_dir,
                               rt.manifest_path.parent / 'worker-image' / 'uv.lock')
    logs = rt.paths.logs_dir(rt.manifest['campaign'])
    out['publication'] = jobs.verify_published_results(rt.paths, rt.journal, logs, logs / 'results')
    halt = rt.ledger.halted()
    if halt and not rt.journal.last('halt', 'set'):
        rt.journal.record('halt', 'set', 'complete', detail={'reason': halt.reason})
    for row in summary_rows(logs / 'evolution_summary.jsonl'):
        iteration = row.get('iteration', 0)
        key = f'iter{iteration:03d}'
        if not rt.paths.iteration_file(iteration).exists():
            util.write_json(rt.paths.iteration_file(iteration), {'schema': 'mhforge.iteration/1',
                'iteration': iteration, 'kind': 'recovered', 'candidates': []})
        if not rt.journal.last('iteration', key):
            rt.journal.record('iteration', key, 'settled', detail={'kind': 'recovered'})
    return out


def check_config(config, expect):
    """Refuse a changed experimental protocol before an executor is called."""
    got = {'datasets': config['datasets'], 'baselines': config['memory_systems']['baselines'],
           'model': config['models'][0]['model']}
    got.update({k: config['inner_loop'].get(k) for k in ('seed', 'mode', 'num_epochs', 'temperature')})
    d = config['dataset']
    got['sizes'] = {name: [d.get('overrides', {}).get(name, {}).get(k, d[k])
                          for k in ('num_train', 'num_val', 'num_test')] for name in got['datasets']}
    different = [k for k, v in got.items() if v != expect[k]]
    if different:
        raise UpstreamContractError('upstream config differs: ' + ', '.join(different))


class Driver:
    def __init__(self, rt, *, base_env: Mapping[str, str], home: Path, export_hook=None):
        self.rt, self.base_env, self.home = rt, base_env, Path(home)
        self.export_hook = export_hook or (lambda unit: None)
        self.stop_requested = False
        self.force_stop = False
        self._closed = False
        self.rejected = {}

    @property
    def logs(self):
        return self.rt.paths.logs_dir(self.rt.manifest['campaign'])

    def _halt(self):
        halt = self.rt.ledger.halted()
        if halt:
            raise CampaignStop(reason=halt.reason, detail=halt.detail)

    @contextlib.contextmanager
    def _signals(self):
        def handler(signum, frame):
            if self.stop_requested:
                self.force_stop = True
                raise KeyboardInterrupt
            self.stop_requested = True
        old = {}
        try:
            for sig in (signal.SIGINT, signal.SIGTERM):
                old[sig] = signal.signal(sig, handler)
            yield
        finally:
            for sig, previous in old.items():
                signal.signal(sig, previous)

    def _runner(self, up):
        r = self.rt
        python = os.path.abspath(r.manifest_path.parent / r.manifest['upstream']['worker_python'])
        return jobs.JobRunner(r.manifest, r.paths, journal=r.journal, ledger=r.ledger,
            executor=r.executor, facade=jobs.real_facade(up.benchmark), tracker=r.tracker,
            example_dir=r.example_dir, logs_dir=self.logs, results_dir=self.logs / 'results',
            passthrough=jobs.make_passthrough(python, r.paths.ws, self.logs, home=self.home),
            candidate_reference=lambda name, sha: proposer.candidate_reference(r.paths, name, sha),
            expect_freeze_sha256=lambda: (qualification.qualified_environment(r.paths) or {}).get('freeze_sha256'),
            on_unit=self.export_hook, clock=r.clock)

    def _record_iteration(self, iteration, candidates, scores, prop=None, kind='scored', timing=None):
        r, m = self.rt, self.rt.manifest
        datasets = m['upstream']['expect']['datasets']
        before = prop.frontier_before if prop else None
        best = (before or {}).get('val_accuracy')
        records = []
        solver_cost = sandbox_cost = 0
        for c in candidates:
            name = c['name']
            rows = {}
            context_chars, prompt_sum, usage_calls, prompt_max = {}, 0, 0, None
            collision = False
            for ds in datasets:
                p = r.paths.job_dir(f'val.{name}.{ds}') / 'job.json'
                record = util.read_json(p) if p.exists() else {}
                rows[ds] = {'status': record.get('status', 'missing'),
                            'accuracy': (record.get('result') or {}).get('accuracy')}
                collision = collision or record.get('name_collision', False)
                chars = (record.get('result') or {}).get('memory_context_chars')
                if chars is not None:
                    context_chars[ds] = chars
                for attempt in record.get('attempts', []):
                    gateway = attempt.get('gateway') or {}
                    solver_cost += gateway.get('cost_micro', 0) or 0
                    sandbox_cost += (attempt.get('executor') or {}).get('cost_micro', 0) or 0
                    prompt_sum += gateway.get('prompt_tokens_sum', 0) or 0
                    usage_calls += gateway.get('usage_calls', 0) or 0
                    peak = gateway.get('prompt_tokens_max')
                    if peak is not None:
                        prompt_max = max(prompt_max or 0, peak)
            complete = all(x['status'] == 'succeeded' and x['accuracy'] is not None for x in rows.values())
            avg = sum((x['accuracy'] or 0) * 100 for x in rows.values()) / len(datasets)
            records.append({**c, 'valid': name not in self.rejected, 'rejected': self.rejected.get(name),
                'candidate_sha256': workspace.candidate_sha256(r.paths, name), 'datasets': rows,
                'upstream_avg_val': scores.get(name, 0), 'avg_val_all_datasets': avg, 'complete': complete,
                'delta_vs_previous_best': None if best is None else avg - best, 'name_collision': collision,
                'context': {'memory_context_chars': context_chars,
                    'memory_context_chars_avg': sum(context_chars.values()) / len(context_chars) if context_chars else None,
                    'chars_div_4': sum(context_chars.values()) / (4 * len(context_chars)) if context_chars else None,
                    'prompt_tokens_mean': prompt_sum / usage_calls if usage_calls else None, 'prompt_tokens_max': prompt_max}})
        after = max([best or 0] + [c['avg_val_all_datasets'] for c in records if c['complete']])
        session = {}
        if prop and prop.last_session_key:
            for path in r.paths.proposer_root.glob(f'iter-{iteration:03d}/s*/session.json'):
                item = util.read_json(path)
                if item.get('session_key') == prop.last_session_key:
                    session = item
        doc = {'schema': 'mhforge.iteration/1', 'iteration': iteration, 'kind': kind,
            'session_key': prop.last_session_key if prop else None, 'frontier_before': before,
            'candidates': records, 'best_complete_before': best, 'best_complete_after': after,
            'improvement': None if best is None else after - best, 'timing_s': timing or {},
            'solver_cost_micro': solver_cost, 'sandbox_cost_micro': sandbox_cost,
            'proposer_list_price_micro': session.get('list_price_micro'),
            'evidence_exposed': bool(session.get('state_access_signals')),
            'started_utc': session.get('started_utc'),
            'ended_utc': util.utc_now(r.clock)}
        util.write_json(r.paths.iteration_file(iteration), doc, fsync=True)
        r.journal.record('iteration', f'iter{iteration:03d}', 'settled', detail={'kind': kind})
        self.export_hook(f'loop:iter{iteration:03d}')

    def _baseline_complete(self, runner):
        m = self.rt.manifest
        for system in m['upstream']['expect']['baselines']:
            for ds in runner.facade.datasets:
                p = runner.facade.run_dir(self.logs, ds, system, m['upstream']['expect']['model_short']) / 'val.json'
                if not bundle.result_is_usable(p):
                    raise CampaignStop(reason='baseline_incomplete')

    def _validate(self, candidates, runner):
        eligible, self.rejected = [], {}
        limit = self.rt.manifest['search']['max_candidates_per_iteration']
        for i, c in enumerate(candidates):
            name = c.get('name')
            rejection = 'over_limit' if i >= limit else ('name' if workspace.validate_candidate_file(self.rt.paths, name) else None)
            if rejection:
                self.rejected[name] = rejection
            else:
                eligible.append(c)
        checked = runner.import_check([c['name'] for c in eligible], iteration=self.rt.tracker.iteration or 0) if eligible else {}
        valid = []
        for c in eligible:
            rejection = jobs.import_rejection(checked[c['name']])
            if rejection:
                self.rejected[c['name']] = rejection
            else:
                valid.append(c)
        return valid

    def _execute(self, command, action):
        r = self.rt
        result = {'status': 'error', 'reason': 'internal_error'}
        try:
            startup_recovery(r)
            protocol = r.manifest_path.parent / 'protocol.md'
            r.journal.record('run', command, 'started', detail={
                'protocol_sha256': util.sha256_bytes(protocol.read_bytes()) if protocol.exists() else None})
            self._halt()
            with self._signals(), workspace.load_upstream(r.paths) as up:
                check_config(up.benchmark._CONFIG, r.manifest['upstream']['expect'])
                result = action(up, self._runner(up))
        except CampaignStop as exc:
            result = {'status': 'stopped', 'reason': exc.reason, 'detail': exc.detail, 'phase': exc.phase}
        except KeyboardInterrupt:
            result = {'status': 'stopped', 'reason': 'interrupted'}
            raise
        except SystemExit:
            result = {'status': 'incomplete' if command == 'test' else 'error', 'reason': 'upstream_contract'}
        except Exception as exc:
            # Exception strings can contain a provider credential or raw dataset text. Record the type only.
            result = {'status': 'error', 'reason': 'upstream_contract' if isinstance(exc, UpstreamContractError)
                      else 'internal_error', 'exception_type': type(exc).__name__}
        finally:
            try:
                cleanup = self.shutdown()
            except Exception as exc:
                cleanup = {'exception_type': type(exc).__name__, 'undrained': None}
                result = {'status': 'error', 'reason': 'internal_error', 'operation': 'shutdown'}
            result['cleanup'] = cleanup
            if command == 'qualification':
                result['phase'] = 'qualification'
            if result['status'] in ('error', 'stopped', 'incomplete'):
                util.write_json(r.paths.stopped, {**result, 'utc': util.utc_now(r.clock)}, fsync=True)
            elif r.paths.stopped.exists():
                r.paths.stopped.unlink()
            status = result['status'] if result['status'] in ('paused', 'complete') else 'stopped'
            r.journal.record('run', command, status, detail=result)
            if command == 'test':
                r.journal.record('test', r.manifest['campaign'],
                                 'complete' if result['status'] == 'complete' else 'incomplete')
        return result

    def _evolve(self, command, iterations=None, max_iterations_this_run=None, end_search=False):
        r, m = self.rt, self.rt.manifest
        def action(up, runner):
            r.tracker.phase, r.tracker.iteration = 'baseline', None
            prop = proposer.ProposerRunner(m, r.paths, journal=r.journal, ledger=r.ledger, upstream=up,
                example_dir=r.example_dir, lock_file=r.manifest_path.parent / 'worker-image' / 'uv.lock',
                base_env=self.base_env, home=self.home, on_unit=self.export_hook, halt_check=self._halt, clock=r.clock)
            original_summary = up.meta_harness.update_evolution_summary
            total = m['search']['iterations'] if iterations is None else iterations
            start = settled_iterations(r.paths, self.logs / 'evolution_summary.jsonl') + 1
            n = remaining_iterations(total, start)
            if max_iterations_this_run is not None:
                if max_iterations_this_run < 1:
                    raise ValueError('max_iterations_this_run must be positive')
                n = min(n, max_iterations_this_run)
            def benchmark(args):
                self._halt()
                result = runner.run_benchmark(args)
                if args[0] == '--frontier' and '--test' not in args and r.tracker.phase == 'baseline':
                    self._baseline_complete(runner)
                    if not r.paths.iteration_file(0).exists():
                        self._record_iteration(0, [{'name': b} for b in m['upstream']['expect']['baselines']], {}, kind='baselines')
                if command != 'search' and self.stop_requested:
                    raise CampaignStop(reason='interrupted')
                return result
            def propose(prompt, iteration, timeout=2400):
                if self.stop_requested:
                    raise CampaignStop(reason='interrupted')
                self._halt()
                if r.ingress.open_ports():
                    raise UpstreamContractError('gateway listener open during proposer session')
                r.tracker.phase, r.tracker.iteration = 'search', iteration
                ok = prop.propose(prompt, iteration, timeout)
                if not ok:
                    self._record_iteration(iteration, [], {}, prop, 'proposer_failed')
                return ok
            def summary(iteration, candidates, val_scores, **timing):
                original_summary(iteration, candidates, val_scores, **timing)
                self._record_iteration(iteration, candidates, val_scores, prop,
                    'scored' if any(c['name'] not in self.rejected for c in candidates) else 'zero_valid', timing)
            uninstall = install(up, run_benchmark=benchmark, propose=propose,
                                validate=lambda c: self._validate(c, runner), summary=summary)
            original_count = up.meta_harness.count_iterations_from_summary
            up.meta_harness.count_iterations_from_summary = lambda: start - 1
            try:
                if command == 'test':
                    r.journal.record('test', m['campaign'], 'started', detail={'end_search': end_search})
                up.meta_harness.run_evolve(argparse.Namespace(model=m['upstream']['expect']['model'],
                    run_name=m['campaign'], fresh=False, skip_baseline=False, iterations=n,
                    propose_timeout=m['search']['propose_timeout_s'], test=command == 'test'))
            finally:
                uninstall()
                up.meta_harness.count_iterations_from_summary = original_count
            last = max([count_iterations(self.logs / 'evolution_summary.jsonl')] +
                       [int(p.stem.split('-')[1]) for p in r.paths.iterations_root.glob('iter-*.json')])
            if self.stop_requested:
                raise CampaignStop(reason='interrupted')
            return {'status': 'paused' if command == 'search' and last < total else 'complete', 'last_iteration': last}
        return self._execute(command, action)

    def run_search(self, iterations=None, *, max_iterations_this_run=None):
        return self._evolve('search', iterations, max_iterations_this_run)

    def run_baselines(self):
        return self._evolve('baselines', 0)

    def run_test(self, *, end_search=False):
        return self._evolve('test', 0, end_search=end_search)

    def _unpublished(self, runner, system, *, test=False, phase='qualification', repeat=None):
        r = self.rt
        planned, error, skipped = jobs.plan_jobs(runner.facade, system, test=test,
            logs_dir=self.logs if test else r.paths.scratch_root / 'logs',
            results_dir=r.paths.scratch_root / 'results', manifest=r.manifest)
        if error or skipped:
            raise CampaignStop(reason='qualification_failed', detail='missing calibration inputs')
        if phase == 'qualification':
            planned = [p for p in planned if p.dataset == r.manifest['qualification']['dataset']]
        if not planned:
            raise CampaignStop(reason='qualification_failed', detail='no jobs planned')
        return {p.dataset: runner.run_job(p, phase=phase, iteration=None, publish=False, repeat=repeat) for p in planned}

    def run_qualification_job(self):
        def action(up, runner):
            rows = self._unpublished(runner, self.rt.manifest['qualification']['system'])
            record = next(iter(rows.values()))
            return {'status': 'complete' if record['status'] == 'succeeded' else 'incomplete', 'job': record}
        return self._execute('qualification', action)

    def run_proposer_probe(self):
        from . import proposer_probe
        def action(up, runner):
            result = proposer_probe.run(self, up)
            return {'status': 'complete' if result['passed'] else 'incomplete', **result}
        return self._execute('proposer_probe', action)

    def run_calibration(self):
        def action(up, runner):
            m, r = self.rt.manifest, self.rt
            r.journal.record('calibration', m['campaign'], 'started')
            systems = m['calibration']['systems'] or m['upstream']['expect']['baselines']
            records = {s: self._unpublished(runner, s, test=True, phase='calibration') for s in systems}
            verdict = calibration_verdict(m, records, r.paths)
            util.write_json(r.paths.calibration, verdict, fsync=True)
            r.journal.record('calibration', m['campaign'], 'complete', detail={'verdict': verdict['verdict']})
            return {'status': 'complete', 'calibration': verdict}
        return self._execute('calibration', action)

    def run_noise(self):
        def action(up, runner):
            r, m = self.rt, self.rt.manifest
            r.journal.record('noise', m['campaign'], 'started')
            sidecars, results = {}, {}
            for s in m['upstream']['expect']['baselines']:
                for ds in m['upstream']['expect']['datasets']:
                    record = util.read_json(r.paths.job_dir(f'val.{s}.{ds}') / 'job.json')
                    add_noise_record(sidecars, results, s, ds, 1, record, r.paths)
                for k in range(2, m['calibration']['noise_repeats'] + 1):
                    for ds, record in self._unpublished(runner, s, phase='calibration', repeat=k).items():
                        add_noise_record(sidecars, results, s, ds, k, record, r.paths)
            doc = qualification.noise_stats(sidecars, results)
            util.write_json(r.paths.noise, doc, fsync=True)
            r.journal.record('noise', m['campaign'], 'complete')
            return {'status': 'complete', 'noise': doc}
        return self._execute('noise', action)

    def run_engineering(self):
        from . import engineering
        return engineering.run(self)

    def run_controls(self):
        from . import controls
        def action(up, runner):
            r, m = self.rt, self.rt.manifest
            root = r.paths.root / 'controls'
            root.mkdir(parents=True, exist_ok=True)
            plan = dict(controls.PLAN, engine_sha256=qualification.engine_snapshot(Path(__file__).parent)['engine_sha256'])
            if (root / 'plan.json').exists() and util.read_json(root / 'plan.json') != plan:
                raise CampaignStop(reason='upstream_contract', detail='control protocol changed; use a new campaign')
            util.write_json(root / 'plan.json', plan, fsync=True)
            r.journal.record('controls', m['campaign'], 'started', detail={'plan_sha256': util.sha256_file(root / 'plan.json')})
            planned, error, skipped = jobs.plan_jobs(runner.facade, controls.PLAN['system'], test=True,
                logs_dir=self.logs, results_dir=r.paths.scratch_root / 'control-results', manifest=m)
            if error or skipped or len(planned) != len(m['upstream']['expect']['datasets']):
                raise CampaignStop(reason='qualification_failed', detail='control baseline inputs missing')
            grader = controls.load_grader(r.paths)
            results = {}
            for p in planned:
                argv = list(p.inner_argv)
                argv[argv.index('--temperature') + 1] = str(controls.PLAN['temperature'])
                p = replace(p, inner_argv=tuple(argv))
                samples, records = [], []
                for sample in range(1, controls.PLAN['k'] + 1):
                    record = runner.run_job(p, phase='controls', iteration=None, publish=False, sample=sample)
                    a = _successful_attempt(record)
                    if record['status'] != 'succeeded' or a is None:
                        raise CampaignStop(reason='qualification_failed', detail='control sample incomplete')
                    sidecar = r.paths.attempt_dir(record['job_key'], a['attempt']) / 'out/sidecar.jsonl'
                    samples.append(controls.final_evals(util.read_jsonl(sidecar)))
                    records.append({'job_key': record['job_key'], 'attempt': a['attempt'],
                                    'sidecar_sha256': util.sha256_file(sidecar)})
                result = controls.aggregate(p.dataset, samples, grader,
                                            expected_count=m['upstream']['expect']['sizes'][p.dataset][2])
                result['evidence'] = records
                results[p.dataset] = result
                util.write_json(root / (p.dataset + '.json'), result, fsync=True)
            summary = {'schema': 'mhforge.controls-result/1', 'plan': plan, 'datasets': results,
                       'macro_accuracy': sum(v['accuracy'] for v in results.values()) / len(results)}
            util.write_json(root / 'result.json', summary, fsync=True)
            r.journal.record('controls', m['campaign'], 'complete')
            compact = {ds: {k: result[k] for k in ('correct', 'total', 'accuracy')}
                       for ds, result in results.items()}
            return {'status': 'complete', 'controls': {'plan': plan, 'datasets': compact,
                    'macro_accuracy': summary['macro_accuracy'], 'result_file': 'controls/result.json',
                    'result_sha256': util.sha256_file(root / 'result.json')}}
        return self._execute('controls', action)

    def shutdown(self):
        if self._closed:
            return getattr(self, '_cleanup', {'undrained': 0})
        self._closed = True
        self.rt.gateway.abort_queued()
        try:
            undrained = self.rt.gateway.in_flight() if self.force_stop else self.rt.gateway.drain(
                self.rt.manifest['provider']['attempt_deadline_s'] + 60)
        finally:
            try:
                self.rt.executor.close()
            finally:
                self.rt.ingress.close_all()
        self._cleanup = {'undrained': undrained}
        return self._cleanup


def _successful_attempt(record):
    return next((a for a in reversed(record.get('attempts', [])) if a.get('outcome') == 'ok'), None)


def add_noise_record(sidecars, results, system, dataset, repeat, record, paths):
    attempt = _successful_attempt(record)
    if attempt is None or record.get('status') != 'succeeded':
        raise CampaignStop(reason='qualification_failed', detail='noise repeat has no successful attempt')
    out = paths.attempt_dir(record['job_key'], attempt['attempt']) / 'out'
    sidecars.setdefault(system, {}).setdefault(dataset, {})[repeat] = util.read_jsonl(out / 'sidecar.jsonl')
    results.setdefault(system, {}).setdefault(dataset, {})[repeat] = util.read_json(out / 'val.json')


def calibration_verdict(manifest, records, paths):
    from .telemetry.join import parse_sidecar
    datasets = manifest['upstream']['expect']['datasets']
    systems, gates = {}, {}
    for name, rows in records.items():
        complete = all(rows.get(ds, {}).get('status') == 'succeeded' for ds in datasets)
        per = {ds: rows.get(ds, {}).get('result') or {} for ds in datasets}
        systems[name] = {'datasets': per, 'complete': complete,
                         'macro_test_accuracy': sum((v.get('accuracy') or 0) * 100 for v in per.values()) / len(datasets)}
    for name, gate in manifest['calibration']['gates'].items():
        s = systems.get(gate['system'], {})
        passed = s.get('complete', False) and gate['macro_test_accuracy'][0] <= s['macro_test_accuracy'] <= gate['macro_test_accuracy'][1]
        for ds, bounds in gate['correct'].items():
            row = records.get(gate['system'], {}).get(ds, {})
            attempt = _successful_attempt(row)
            events = util.read_jsonl(paths.attempt_dir(row['job_key'], attempt['attempt']) / 'out' / 'sidecar.jsonl') if attempt else []
            sidecar = parse_sidecar(events)
            by_index = {}
            for ev in sidecar['evals']:
                if ev.get('split') != 'test' or ev.get('index') is None:
                    continue
                prev = by_index.get(ev['index'])
                if prev is None or (ev['pass'], ev['seq']) > (prev['pass'], prev['seq']):
                    by_index[ev['index']] = ev
            correct = sum(e.get('was_correct') is True for e in by_index.values())
            expected = manifest['upstream']['expect']['sizes'][ds][2]
            reported = row.get('result') or {}
            consistent = (not sidecar['problems'] and set(by_index) == set(range(expected))
                          and reported.get('correct') == correct and reported.get('total') == expected
                          and abs((reported.get('accuracy') or 0) - correct / expected) < 1e-9)
            passed = bool(passed and consistent and bounds[0] <= correct <= bounds[1])
            if gate['content_on_every_call']:
                calls = [util.read_json(p) for p in paths.calls_dir(attempt['job_id']).glob('*.json')] if attempt else []
                passed = bool(passed and calls and all(c.get('outcome') in ('completed', 'replayed', 'coalesced')
                    and not c.get('flags', {}).get('empty_content') and c.get('finish_reason') != 'length' for c in calls)
                    and all(e.get('prediction_chars', 0) > 0 for e in by_index.values()))
        gates[name] = {'passed': passed, 'hard': gate['hard']}
    hard = [g['passed'] for g in gates.values() if g['hard']]
    return {'schema': 'mhforge.calibration/1', 'systems': systems, 'gates': gates,
            'verdict': ('pass' if all(hard) else 'fail') if hard else 'recorded',
            'flags': [name for name, g in gates.items() if not g['passed'] and not g['hard']]}
