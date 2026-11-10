# SPDX-License-Identifier: MIT
"""Ordered live-check receipts. Unavailable evidence is a failed check, never a pass.

Remote schema and service behavior remain UNVERIFIED until these checks run live.
"""
from __future__ import annotations

import json
import shlex
import traceback
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Mapping

from . import audit, manifest as manifests, qualification, util
from .errors import CampaignStop, ExecutorError, LiveGateError
from .paths import StatePaths
from .wire import JobIdentity, make_envelope


@dataclass(frozen=True)
class Check:
    step: str
    name: str
    live: bool
    required: bool
    per_project: bool
    needs: str
    describe: str
    run: Callable


@dataclass
class Context:
    manifest: dict
    paths: StatePaths
    environ: Mapping[str, str]
    sc_dir: Path
    attempt: int = 1
    gateway: object = None
    ledger: object = None
    executor: object = None
    sandbox_client: object = None
    lens: object = None
    wandb: object = None
    account: object = None
    run_qualification_job: Callable | None = None
    run_baselines: Callable | None = None
    run_calibration: Callable | None = None
    proposer_session: Callable | None = None
    user_figures: Mapping[str, str] = field(default_factory=dict)
    hostname: str = ''
    # Runtime-specific probes are injected; absence cannot qualify a campaign.
    probes: Mapping[str, Callable] = field(default_factory=dict)


class WandbAccountReader:
    def __init__(self, *, api_key, user_agent):
        if not api_key:
            raise LiveGateError(blockers=[('provider', 'an explicit key is required')])
        import wandb
        from .telemetry.adapter_wandb import DEFAULT_BASE_URL
        self.api = wandb.Api(api_key=api_key, overrides={'base_url': DEFAULT_BASE_URL})

    def _query(self, query, variables):
        return self.api._service_api.execute_graphql(query, variables=dict(variables))

    def server_features(self):
        return {name: self.api._service_api.feature_enabled(name) is True for name in ('EVAL_TABLES_CES',)}

    def default_entity(self):
        return self.api.default_entity

    def entity_org(self, entity):
        result = self._query('query($name: String!) { entity(name: $name) { name isTeam } }', {'name': entity})
        value = result.get('entity')
        return None if value is None else {'org': None, 'is_team': value.get('isTeam'),
                                          'members': None, 'default_access': None}

    def org_flags(self, org, names):
        return {name: None for name in names}  # role/API dependent; unknown, never guessed

    def subscription(self, org):
        return None

    def project(self, entity, name):
        from .telemetry.adapter_wandb import PROJECT_QUERY
        result = self._query(PROJECT_QUERY, {'entity': entity, 'name': name}).get('project')
        return None if result is None else {'access': result.get('access'), 'read_only': result.get('readOnly'),
                                           'can_write': (result.get('weavePermissions') or {}).get('canWrite')}

    def create_project_private(self, entity, name):
        self._query('mutation($entity: String!, $name: String!) { upsertModel(input: '
                    '{entityName: $entity, name: $name, access: "PRIVATE"}) { project { name access } } }',
                    {'entity': entity, 'name': name})


def _account_features(ctx):
    return {'passed': True, 'features': ctx.account.server_features()}


def _account_identity(ctx):
    entity = ctx.manifest['decisions']['entity']
    info = ctx.account.entity_org(entity)
    return {'passed': info is not None, 'entity': entity, 'organisation': info,
            'subscription': ctx.account.subscription(info['org']) if info and info.get('org') else None,
            'subscription_readable': False}


def _account_services(ctx):
    if ctx.manifest['executor']['kind'] != 'sandbox':
        return {'passed': True, 'sandbox': 'not_requested'}
    if ctx.sandbox_client is None:
        return {'passed': False, 'reason': 'sandbox client unavailable'}
    found = ctx.sandbox_client.list_ids(tags=['mhf', 'mhf-c-' + ctx.manifest['campaign']])
    return {'passed': True, 'sandbox_list_count': len(found)}


def _account_project(ctx):
    d = ctx.manifest['decisions']
    info = ctx.account.project(d['entity'], d['project'])
    return {'passed': bool(info and info.get('access', '').upper() == 'PRIVATE'
                           and info.get('read_only') is False and info.get('can_write') is True), 'project': info}


ACCOUNT_STEPS = (
    Check('PF1', 'account_features', True, True, True, 'read only', 'Read account feature availability.', _account_features),
    Check('PF2', 'account_identity', True, True, True, 'read only', 'Resolve team and organisation; keep facts local.', _account_identity),
    Check('PF3', 'account_services', True, True, True, 'read only', 'Check sandbox list permission.', _account_services),
    Check('PF4', 'account_project', True, True, True, 'read only; optional private project creation',
          'Read back private project visibility and write access.', _account_project),
)


def _offline(ctx):
    checks = qualification.offline_gate(ctx.manifest, ctx.paths, ctx.sc_dir, command='preflight')
    return {'passed': not qualification.gate_problems(checks), 'checks': checks}


def _probe(name):
    def run(ctx):
        callback = ctx.probes.get(name)
        if callback is None:
            return {'passed': False, 'status': 'unverified', 'reason': name + ' needs its live probe runner'}
        result = callback(ctx)
        return {**result, 'passed': result.get('passed') is True}
    return run


def _inference(ctx):
    if ctx.gateway is None:
        return {'passed': False, 'reason': 'gateway unavailable'}
    # Repeat pair has no replay: it tests live behavior twice and meters both attempts.
    job = JobIdentity(campaign=ctx.manifest['campaign'], split='val', candidate='inference_probe',
                      dataset=ctx.manifest['qualification']['dataset'], attempt=ctx.attempt,
                      phase='qualification', iteration=None)
    request = util.canonical_json({'model': ctx.manifest['provider']['wire_model'],
        'messages': [{'role': 'system', 'content': ctx.manifest['upstream']['expect']['system_prompt']},
                     {'role': 'user', 'content': 'Reply with exactly OK.'}], 'temperature': 0.0,
        'max_tokens': min(2048, ctx.manifest['gateway']['max_output_tokens'])})
    replies = []
    ctx.gateway.register_job(job)
    try:
        for _ in range(2):
            reply = ctx.gateway.handle(job, make_envelope('POST', '/v1/chat/completions', request),
                                       ingress='loopback', no_replay=True)
            body = json.loads(reply.body)
            replies.append({'status': reply.status, 'has_content': bool((body.get('choices') or [{}])[0].get('message', {}).get('content'))})
    finally:
        ctx.gateway.close_job(job.job_id)
    return {'passed': all(r['status'] == 200 and r['has_content'] for r in replies), 'replies': replies}


def _qualification(ctx):
    if ctx.run_qualification_job is None:
        return {'passed': False, 'reason': 'driver unavailable'}
    result = ctx.run_qualification_job()
    job = result.get('job') or {}
    attempts = [a for a in job.get('attempts', []) if a.get('outcome') == 'ok']
    selected = attempts[-1]['job_id'] if attempts else None
    calls = [util.read_json(p) for p in ctx.paths.calls_root.glob('**/*.json') if p.name.endswith('.json')]
    calls = [c for c in calls if selected and (c.get('job') or {}).get('job_id') == selected]
    obs = qualification.observations(calls, job_record=job)
    fc = qualification.forecast(ctx.manifest, obs)
    # Full qualification is recorded by run_checks only after all required probes pass.
    check = audit.audit(ctx.manifest, ctx.paths)
    from .telemetry.join import parse_sidecar, join_examples
    sidecar_rows = util.read_jsonl(ctx.paths.attempt_dir(job['job_key'], attempts[-1]['attempt']) /
                                  'out' / 'sidecar.jsonl') if attempts else []
    examples = join_examples(parse_sidecar(sidecar_rows), calls,
                             dataset=ctx.manifest['qualification']['dataset'], split='val')
    checks = qualification.checks(ctx.manifest, job, obs, check['problems'], examples)
    environment = ((attempts[-1].get('executor') or {}).get('environment') or {}) if attempts else {}
    if ctx.manifest['executor']['kind'] == 'sandbox':
        checks.append({'name': 'environment_pinned', 'ok': bool(environment.get('freeze_sha256'))})
    return {'passed': result.get('status') == 'complete' and all(c['ok'] for c in checks) and fc.get('fits') is True,
            'checks': checks, 'environment': environment,
            'job_key': job.get('job_key'), 'observations': obs, 'forecast': fc, 'audit': check}


def _baselines(ctx):
    result = ctx.run_baselines() if ctx.run_baselines else {}
    return {'passed': result.get('status') == 'complete', 'result': result}


def _calibration(ctx):
    result = ctx.run_calibration() if ctx.run_calibration else {}
    verdict = (result.get('calibration') or {}).get('verdict')
    return {'passed': result.get('status') == 'complete' and verdict in ('pass', 'recorded'), 'result': result}


def reconciliation(ledger_snapshot, figures, manifest):
    waiver = figures.get('waive_reconciliation', '').strip()
    if waiver:
        return {'passed': True, 'verdict': 'waived', 'note': waiver}
    pairs = {}
    for name, micro in (('inference', ledger_snapshot.get('observed_micro', 0)),
                        ('sandbox', ledger_snapshot.get('meters', {}).get('sandbox_seconds', {}).get('micro_usd', 0))):
        raw = figures.get('platform_' + name + '_usd')
        try:
            platform = Decimal(raw) if raw is not None else None
        except InvalidOperation:
            platform = None
        observed = Decimal(micro) / 1000000
        valid = platform is not None and platform.is_finite() and platform >= 0
        tolerance = max(Decimal('.05'), max(observed, platform if valid else 0)
                        * Decimal(manifest['accounting']['reconcile_tolerance']))
        pairs[name] = {'ledger_usd': str(observed), 'platform_usd': str(platform) if valid else None,
                       'passed': bool(valid and abs(platform - observed) <= tolerance)}
    pending = ledger_snapshot.get('unknown_requests', 0) + ledger_snapshot.get('bounded_unknown_requests', 0)
    passed = not pending and all(p['passed'] for p in pairs.values())
    return {'passed': passed, 'verdict': 'pass' if passed else 'fail', 'pairs': pairs}


CHECKS = (
    Check('PF0', 'offline', False, True, True, 'none', 'Verify source, lock, smoke, protocol and environment pins.', _offline),
    Check('PF5', 'inference', True, True, True, 'two capped inference calls', 'Check streaming output and independently metered repeats.', _inference),
    Check('PF6', 'sandbox', True, True, True, 'bounded sandboxes', 'Prove egress denial, image, relay soak, lifetime and cleanup.', _probe('sandbox')),
    Check('PF7', 'qualification', True, True, False, 'one validation job', 'Measure usage, audit the job and forecast the campaign.', _qualification),
    Check('PF7', 'baselines', True, True, False, 'six validation jobs', 'Run every baseline on every dataset.', _baselines),
    Check('PF7', 'calibration_gate', True, True, False, 'baseline test jobs', 'Judge G1 and G2 without exposing test scores to the proposer.', _calibration),
    Check('PF8', 'telemetry', True, True, True, 'trace, table and artifact writes', 'Read back spans, table, artifact and metadata hygiene.', _probe('telemetry')),
    Check('PF9', 'proposer_probes', True, True, False, 'bounded proposer sessions', 'Status-only isolation, settings, signals, model and budget probes.', _probe('proposer_probes')),
    Check('PF11', 'rehearsal_review', True, True, False, 'readback and cleanup', 'Verify crash/resume, finalization and all export receipts.', _probe('rehearsal_review')),
    Check('PF-R', 'reconciliation', False, True, False, 'operator billing figures', 'Reconcile platform usage with the local ledger.',
          lambda ctx: reconciliation(ctx.ledger.snapshot(), ctx.user_figures, ctx.manifest)),
)
# Opt-in experiments may run while proposer access is unavailable. They never
# mint qualification/passed.json and are excluded from full qualification.
DIAGNOSTICS = (
    Check('EXP-E', 'engineering_controls', True, True, False, 'fixed context and sampling arms',
          'Measure 0/5/all examples and nested 1/3/5 samples without adaptive selection.', _probe('engineering_controls')),
    Check('EXP-N', 'baseline_noise', True, True, False, 'fresh baseline repeats',
          'Measure repeated-run variation with distinct provider call identities.', _probe('baseline_noise')),
    Check('EXP-K', 'sampling_controls', True, True, False, 'five fresh test samples per dataset',
          'Measure fixed five-sample voting with released graders.', _probe('sampling_controls')),
)


TEARDOWN = (
    {'item': 'sandboxes', 'how': 'Stop campaign-tagged sandboxes; require two empty lists.', 'automated': True},
    {'item': 'credential', 'how': 'Revoke the dedicated key when no longer needed.', 'automated': False},
    {'item': 'project', 'how': 'Delete the private scratch project only when requested.', 'automated': False},
    {'item': 'local assets', 'how': 'Remove unneeded images, wheelhouse and proposer scratch files.', 'automated': False},
)


def describe():
    return [{k: v for k, v in asdict(c).items() if k != 'run'} for c in ACCOUNT_STEPS + CHECKS + DIAGNOSTICS]


def _invoke(check, ctx):
    try:
        result = check.run(ctx)
    except CampaignStop:
        raise
    except Exception as exc:
        result = {'passed': False, 'exception_type': type(exc).__name__,
                  'failure_frames': [{'file': Path(f.filename).name, 'function': f.name, 'line': f.lineno}
                                     for f in traceback.extract_tb(exc.__traceback__)[-6:]]}
        if isinstance(exc, ExecutorError):
            result.update(error_kind=exc.kind, retryable=exc.retryable)
    return {'schema': 'mhforge.check/1', 'name': check.name, 'step': check.step,
            'required': check.required, 'utc': util.utc_now(), **result}


def account_check(ctx, *, create_project=False):
    receipts = []
    for check in ACCOUNT_STEPS:
        if check.name == 'account_project' and create_project:
            d = ctx.manifest['decisions']
            if ctx.account.project(d['entity'], d['project']) is None:
                # Durable intent prevents blind re-creation after an uncertain remote write.
                intent = ctx.paths.account_root / 'project-create.json'
                if intent.exists():
                    return {'passed': False, 'reason': 'previous project creation needs independent readback'}
                util.write_json(intent, {'status': 'emitting'}, fsync=True)
                ctx.account.create_project_private(d['entity'], d['project'])
        receipts.append(_invoke(check, ctx))
        doc = {'schema': 'mhforge.account/1', 'checks': receipts,
               'passed': all(r.get('passed') is True for r in receipts)}
        util.write_json(ctx.paths.account_file, doc, fsync=True)
        if not receipts[-1]['passed']:
            break
    return doc


def run_checks(ctx, only=None):
    available = CHECKS + DIAGNOSTICS if only else CHECKS
    names = {c.name for c in available}
    if only and set(only) - names:
        raise ValueError('unknown preflight check')
    engine = qualification.engine_snapshot(Path(__file__).parent)
    util.write_json(ctx.paths.qualification_dir(ctx.attempt) / 'engine.json', engine, fsync=True)
    imported = ctx.manifest['qualification']['import_from']
    if imported:
        problems = qualification.import_problems(ctx.manifest, ctx.paths, engine)
        if problems:
            return {'passed': False, 'problems': problems}
    chosen = [c for c in available if (not only or c.name in only) and (not imported or c.per_project)]
    receipts = []
    for check in chosen:
        result = _invoke(check, ctx)
        result.update(engine_sha256=engine['engine_sha256'], manifest_sha256=manifests.manifest_sha256(ctx.manifest))
        util.write_json(ctx.paths.qualification_dir(ctx.attempt) / (check.name + '.json'), result, fsync=True)
        receipts.append(result)
        if check.required and result.get('passed') is not True:
            break
    # A selected subset must never issue passed.json. Required evidence is not inferred from its absence.
    complete = not only and len(receipts) == len(chosen) and bool(receipts) and all(r.get('passed') is True for r in receipts)
    if complete:
        q = next((r for r in receipts if r['name'] == 'qualification'), {})
        record = qualification.record(ctx.paths, ctx.attempt, engine=engine,
            manifest_sha256=manifests.manifest_sha256(ctx.manifest),
            check_list=[{'name': r['name'], 'ok': r['passed']} for r in receipts],
            obs=q.get('observations', {}), fc=q.get('forecast', {}),
            environment=q.get('environment') or None,
            imported_from={'campaign': imported, 'diff_paths': manifests.diff_paths(
                util.read_json(ctx.paths.sibling(imported).manifest), ctx.manifest)} if imported else None)
        complete = record['passed']
    return {'passed': all(r.get('passed') is True for r in receipts) and bool(receipts),
            'qualified': complete, 'checks': receipts}


def build_probe_script(*, canary_denied, credential_paths, scratch, loopback_port):
    lines = ['#!/bin/sh']
    for i, path in enumerate(credential_paths):
        quoted = shlex.quote(str(path))
        lines.append(f'(test -r {quoted}) >/dev/null 2>&1; echo "credential_{i} $?"')
    for name, command in (('canary', 'test -r ' + shlex.quote(str(canary_denied))),
                          ('scratch', 'touch ' + shlex.quote(str(Path(scratch) / 'probe'))),
                          ('loopback', f'curl --max-time 5 -s -o /dev/null http://127.0.0.1:{int(loopback_port)}/')):
        lines.append(f'({command}) >/dev/null 2>&1; echo "{name} $?"')
    return '\n'.join(lines) + '\n'


def probe_expectations(mode):
    return {'canary': 'record' if mode == 'upstream' else 'nonzero', 'scratch': 'zero',
            'loopback': 'record' if mode == 'upstream' else 'nonzero',
            'new_report': 'zero', 'existing_report': 'record' if mode == 'upstream' else 'nonzero'}
