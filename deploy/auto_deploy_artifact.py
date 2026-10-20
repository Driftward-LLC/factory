#!/usr/bin/env python3
"""Package the already-tested image; keep compatibility policy conservative."""
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REGISTRY_IMAGE = 'ghcr.io/driftward-llc/factory-production'


def private_package(*, allow_missing=False):
    request = Request('https://api.github.com/orgs/Driftward-LLC/packages/container/factory-production',
                      headers={'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
                               'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
    try:
        with urlopen(request, timeout=30) as response:
            package = json.load(response)
    except HTTPError as error:
        if allow_missing and error.code == 404:
            return
        raise RuntimeError('Cannot verify private production package') from None
    if (package.get('visibility') != 'private'
            or package.get('repository', {}).get('full_name') != 'Driftward-LLC/factory'):
        raise RuntimeError('Production package must be private and linked to this repository')


def routine_path(name):
    # ponytail: allow known image-only surfaces; expand after reviewing a new surface.
    return (name.startswith(('factory/console/', 'docs/', 'tools/test_'))
            or name in {'README.md', 'DESIGN.md'}
            or name in {'hatchet/bridge/hatchet_hermes_bridge/console.py',
                        'hatchet/bridge/hatchet_hermes_bridge/analytics.py'}
            or any(name.startswith('hatchet/workers/' + worker + '/src/') for worker in
                   ('intake-worker', 'execution-dispatch-worker', 'pr-event-worker', 'reconciliation-worker'))
            or name.startswith('hatchet/workers/bridge-executor-worker/src/workflows/'))


def compatibility_entries(entries):
    protected = sorted((entry for entry in entries if entry and not routine_path(entry.split(b'\t', 1)[1].decode())),
                       key=lambda entry: entry.split(b'\t', 1)[1])
    return hashlib.sha256(b'\0'.join(protected)).hexdigest()


def compatibility(root):
    entries = subprocess.check_output(['git', '-C', str(root), 'ls-tree', '-rz', 'HEAD'])
    return compatibility_entries(entries.split(b'\0'))


def package(root, output, image):
    commit = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    info = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image], text=True))[0]
    assert info['Config']['Labels']['org.opencontainers.image.revision'] == commit
    assert info['Config']['Labels']['org.opencontainers.image.source'] == 'https://github.com/Driftward-LLC/factory'
    assert info['Architecture'] == 'amd64' and info['Os'] == 'linux'
    output.mkdir(parents=True, exist_ok=False)
    run_id, attempt = int(os.environ['GITHUB_RUN_ID']), int(os.environ['GITHUB_RUN_ATTEMPT'])
    assert run_id > 0 and attempt > 0
    tag = f'{REGISTRY_IMAGE}:run-{run_id}-{attempt}'
    private_package(allow_missing=True)
    subprocess.run(['docker', 'image', 'tag', info['Id'], tag], check=True)
    # Publish only this platform, never an index containing untested platforms.
    pushed = subprocess.check_output(['docker', 'image', 'push', '--platform', 'linux/amd64', tag], text=True)
    digests = re.findall(r'digest: (sha256:[0-9a-f]{64})\b', pushed)
    assert len(digests) == 1
    reference = REGISTRY_IMAGE + '@' + digests[0]
    manifest = json.loads(subprocess.check_output(['docker', 'manifest', 'inspect', reference], text=True))
    assert manifest['schemaVersion'] == 2 and 'manifests' not in manifest
    config = manifest['config']['digest']
    assert re.fullmatch(r'sha256:[0-9a-f]{64}', config)
    assert info['Id'] in {config, digests[0]}
    private_package()
    (output / 'deployment.json').write_text(json.dumps({
        'format': 2, 'commit': commit, 'image': reference, 'image_config': config,
        'compatibility': compatibility(root), 'run_id': run_id, 'run_attempt': attempt,
    }, sort_keys=True) + '\n')


if __name__ == '__main__':
    package(Path(__file__).resolve().parents[1], Path(sys.argv[1]), sys.argv[2])
