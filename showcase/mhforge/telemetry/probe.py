# SPDX-License-Identifier: MIT
"""Export real qualification evidence and require every remote readback receipt."""
from .adapter_agentlens import CoreweaveSink
from .adapter_wandb import WandbSdkSink
from .exporter import Exporter
from .. import audit


def run(ctx, keys, *, drain=None):
    if drain:
        drain()
    m = ctx.manifest
    sink = CoreweaveSink(m, environ=dict(keys)) if m['telemetry']['agent_lens']['enabled'] else None
    exporter = Exporter(m, ctx.paths, ledger=ctx.ledger, lens=sink,
        wandb=WandbSdkSink(m, environ=dict(keys), paths=ctx.paths),
        known_secrets=[v.encode() for v in keys.values()], hostname=ctx.hostname,
        example_dir=(ctx.sc_dir / m['upstream']['example_dir']).absolute())
    result = exporter.export_all()
    statuses = result['statuses']
    verified = bool(statuses) and not result['deferred'] and not result['not_ready'] and all(
        v in ('verified', 'skipped_policy') for unit in statuses.values() for v in unit.values())
    check = audit.audit(m, ctx.paths)
    return {'passed': verified and check['ok'], 'units': len(statuses), 'statuses': statuses,
            'deferred': result['deferred'], 'not_ready': result['not_ready'], 'audit': check}
