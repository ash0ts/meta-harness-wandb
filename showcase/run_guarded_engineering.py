#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Local execution of fixed engineering controls with a macOS worker network guard.

This is not cloud sandbox isolation. Only the reviewed, frozen control candidates
run locally; proposer search is refused. Inference and telemetry remain in Forge.
The manifest binds this launcher by hash, alongside the normal engine freeze.
"""
import hashlib
from pathlib import Path
import sys

from mhforge import cli, driver, manifest, util
from mhforge.errors import MhforgeError
from mhforge.smoke import network_guard_prefix


def main(argv=None):
    argv=list(sys.argv[1:] if argv is None else argv)
    args=cli.parser().parse_args(argv)
    if args.command not in ('validate-manifest','account-check','preflight','status','halt','export','audit'):
        raise MhforgeError('This launcher cannot execute proposer search or arbitrary evaluations')
    m=manifest.read_manifest(args.manifest)
    digest=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if 'guarded_launcher_sha256='+digest not in m['notes']:
        raise MhforgeError('Manifest does not bind this exact guarded launcher')
    if m['executor']['kind']!='local_relay' or not m['executor']['allow_unisolated_live']:
        raise MhforgeError('Manifest must explicitly select local relay execution')
    if args.command=='preflight' and args.only!=['engineering_controls']:
        raise MhforgeError('Only the fixed engineering controls may execute through this launcher')
    original=driver.build_runtime
    def guarded(*pos,**kw):
        prefix,kind=network_guard_prefix()
        if kind!='sandbox-exec':
            raise MhforgeError('macOS worker network guard is unavailable')
        kw['command_prefix']=prefix
        rt=original(*pos,**kw)
        util.write_json(rt.paths.root/'local-network-guard.json',{
            'launcher_sha256':digest,'guard':kind,'profile_sha256':hashlib.sha256(prefix[-1].encode()).hexdigest(),
            'boundary':'Worker network access restricted to loopback; reviewed code runs on the host, not in a cloud sandbox.',
            'proposer_allowed':False},fsync=True)
        return rt
    driver.build_runtime=guarded
    try:
        return cli.main(argv)
    finally:
        driver.build_runtime=original


if __name__=='__main__':
    raise SystemExit(main())
