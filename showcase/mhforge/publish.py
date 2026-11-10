# SPDX-License-Identifier: MIT
"""Allowlisted, scanned publication bundles and separately receipted outward steps.

Visibility/report/view APIs remain explicitly unverified until their live rehearsal.
A failed or interrupted outward write is never automatically retried.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from . import scan, util
from .errors import ExportError, SecretFound
from .telemetry import policy
from .telemetry.exporter import Exporter


def allowed_files(paths, manifest):
    fixed = [paths.manifest, paths.amendments, paths.journal, paths.stopped,
             paths.root.parent.parent / 'protocol.md', paths.root / 'audit.json']
    patterns = ('jobs/*/job.json', 'iterations/iter-*.json', 'proposer/iter-*/s*/session.json',
                'proposer/iter-*/s*/candidates/*.py', 'export/receipts/*.json', 'export/plans/**/*.json',
                'export/readback/*.json', 'qualification/*.json', 'qualification/attempt-*/*.json')
    found = fixed + [p for pattern in patterns for p in paths.root.glob(pattern)]
    logs = paths.logs_dir(manifest['campaign'])
    found += [logs / 'evolution_summary.jsonl', *logs.glob('frontier*.json')]
    found += list((paths.ws / 'agents').glob('*.py'))
    # Reject links in every path component, including a symlinked state root.
    return sorted({p for p in found if p.is_file() and not p.is_symlink()
                   and str(p.absolute()) == os.path.realpath(p)}, key=str)


def _portable_bytes(path, roots):
    raw = path.read_bytes()
    if path.suffix == '.json':
        return util.canonical_json(policy.portable(json.loads(raw), roots)) + b'\n'
    if path.suffix == '.jsonl':
        return b''.join(util.canonical_json(policy.portable(json.loads(line), roots)) + b'\n'
                        for line in raw.splitlines() if line.strip())
    return policy.portable(raw.decode('utf-8'), roots).encode('utf-8')


def bundle(manifest, paths, dest, *, known_secrets, hostname=''):
    """Stage privately, scan the final bytes, then atomically expose a new bundle."""
    dest = Path(dest).absolute()
    if dest.exists() or dest.is_symlink():
        raise ExportError('bundle destination already exists; preserve it and choose a new directory')
    roots = {'state': paths.root, 'workspace': paths.ws,
             'example': (paths.root.parent.parent / manifest['upstream']['example_dir']).absolute()}
    exporter = Exporter(manifest, paths, ledger=None, lens=None, wandb=None,
                        example_dir=roots['example'] if paths.pristine.exists() else None, dry_run=True)
    index = exporter.verbatim_index()
    digests = scan.load_digests(paths.keydigests)
    dest.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.bundle-', dir=dest.parent))
    os.chmod(staging, 0o700)
    hashes, withheld = {}, []
    try:
        payloads = {}
        for p in allowed_files(paths, manifest):
            rel = p.relative_to(paths.root).as_posix() if p.is_relative_to(paths.root) else 'protocol.md'
            data = _portable_bytes(p, roots)
            # Secret scan happens BEFORE redaction/withholding, so partial secrets cannot escape.
            findings = scan.scan_bytes(p.read_bytes(), known=known_secrets, digests=digests, path=rel)
            if findings:
                raise SecretFound(findings=findings)
            is_plan = rel.startswith('export/plans/')
            # Plans already applied content rules; metadata and candidate code may contain dataset quotations.
            if not is_plan and (index is None or scan.scan_verbatim(data.decode('utf-8'), index, path=rel)):
                if p.suffix == '.py' or 'session.json' in rel or 'iterations/' in rel or 'evolution_summary' in rel:
                    withheld.append(rel)
                    data = util.canonical_json({'withheld': True, 'sha256': util.sha256_bytes(data), 'bytes': len(data)})
                elif index is None and rel.startswith(('jobs/', 'qualification/')):
                    withheld.append(rel)
                    data = util.canonical_json({'withheld': True, 'sha256': util.sha256_bytes(data), 'bytes': len(data)})
                elif index is not None and scan.scan_verbatim(data.decode('utf-8'), index, path=rel):
                    raise ExportError('dataset text in publication metadata: ' + rel)
            payloads[rel] = data
        if paths.ledger.exists():
            db, stale = util.open_sqlite_ro(paths.ledger)
            try:
                if stale:
                    raise ExportError('ledger history may be stale; retry after the writer closes')
                db.row_factory = sqlite3.Row
                history = {t: [dict(row) for row in db.execute('SELECT * FROM ' + t)]
                           for t in ('halt_history', 'reconcile_history')}
            finally:
                db.close()
            payloads['halt_history.json'] = util.canonical_json(policy.portable(history, roots))
        for rel, data in payloads.items():
            findings = scan.scan_bytes(data, known=known_secrets, digests=digests, path=rel,
                                       classes=('secret', 'privacy'), hostnames=(hostname,))
            if findings:
                raise SecretFound(findings=findings)
            p = staging / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
            hashes[rel] = util.sha256_bytes(data)
        receipt = {'schema': 'mhforge.publication-bundle/1', 'files': hashes,
                   'withheld': withheld, 'bundle_sha256': util.tree_digest(hashes)}
        util.write_json(staging / 'bundle.json', receipt, fsync=True)
        os.rename(staging, dest)
        return receipt
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def foreign_conversations(recorded, remote):
    return sorted(set(remote) - set(recorded))


def publish(manifest, paths, *, wandb, remote_conversation_ids):
    if manifest['campaign_kind'] != 'measured' or manifest['decisions']['publish_approved'] is not True:
        raise ExportError('publication requires a measured campaign and publish_approved')
    recorded = set()
    for path in (paths.export_root / 'receipts').glob('*.json'):
        rec = util.read_json(path)
        if rec.get('status') != 'verified':
            raise ExportError('all publication receipts must be verified')
        recorded.update(rec.get('conversation_ids') or [])
        for trace in rec.get('traces') or []:
            if isinstance(trace, dict) and trace.get('conversation_id'):
                recorded.add(trace['conversation_id'])
    if foreign_conversations(recorded, remote_conversation_ids()):
        raise ExportError('project contains conversations not owned by this campaign')
    # Optional surface adapter: unknown write outcomes are durable and require independent readback.
    receipts = []
    for name in ('visibility', 'report', 'saved_views'):
        path = paths.publish_root / (name + '.json')
        if path.exists():
            previous = util.read_json(path)
            if previous.get('status') == 'verified':
                receipts.append(previous)
                continue
            raise ExportError(name + ': prior outcome requires independent readback before retry')
        operation = getattr(wandb, 'publish_' + name, None)
        if operation is None:
            receipt = {'step': name, 'status': 'unverified', 'reason': 'live surface adapter unavailable'}
            util.write_json(path, receipt, fsync=True)
            return {'status': 'unverified', 'receipts': receipts + [receipt]}
        receipt = {'step': name, 'status': 'emitting'}
        util.write_json(path, receipt, fsync=True)
        result = operation(manifest, paths)
        receipt.update(status='verified' if result.get('verified') is True else 'unverified', result=result)
        util.write_json(path, receipt, fsync=True)
        receipts.append(receipt)
        if receipt['status'] != 'verified':
            break
    return {'status': 'verified' if len(receipts) == 3 and all(x['status'] == 'verified' for x in receipts)
            else 'unverified', 'receipts': receipts}
