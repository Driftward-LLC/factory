#!/usr/bin/env python3
"""Package the already-tested image; keep compatibility policy conservative."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys


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


def sha256(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def package(root, output, image):
    commit = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    info = json.loads(subprocess.check_output(['docker', 'image', 'inspect', image], text=True))[0]
    assert info['Config']['Labels']['org.opencontainers.image.revision'] == commit
    assert info['Config']['Labels']['org.opencontainers.image.source'] == 'https://github.com/Driftward-LLC/factory'
    assert info['Architecture'] == 'amd64' and info['Os'] == 'linux'
    output.mkdir(parents=True, exist_ok=False)
    archive = output / 'factory-image.tar'
    # Save by immutable ID: loading cannot replace a production tag.
    subprocess.run(['docker', 'image', 'save', '--output', str(archive), info['Id']], check=True)
    (output / 'deployment.json').write_text(json.dumps({
        'format': 1, 'commit': commit, 'image': info['Id'],
        'compatibility': compatibility(root), 'archive_sha256': sha256(archive),
    }, sort_keys=True) + '\n')


if __name__ == '__main__':
    package(Path(__file__).resolve().parents[1], Path(sys.argv[1]), sys.argv[2])
