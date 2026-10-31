#!/usr/bin/env python3
"""Package the already-tested image; keep compatibility policy conservative."""
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REGISTRY_IMAGE = 'ghcr.io/driftward-llc/factory-production'
RELEASE_IMAGE = 'ghcr.io/driftward-llc/factory'
RELEASE_TAG = r'v[0-9]{4}\.[0-9]{1,2}\.[0-9]{1,2}(\.[0-9]+)?'


def release_context(root):
    if (os.environ.get('GITHUB_REPOSITORY') != 'Driftward-LLC/factory'
            or os.environ.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
            or os.environ.get('GITHUB_REF') != 'refs/heads/main'):
        raise RuntimeError('Release publication requires the canonical main-branch release workflow')
    commit = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    if not re.fullmatch(r'[0-9a-f]{40}', commit) or commit != os.environ.get('GITHUB_SHA'):
        raise RuntimeError('Release checkout must match the triggering commit')
    return commit


def release_api(path):
    request = Request('https://api.github.com/' + path, headers={
        'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
        'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except (HTTPError, URLError, TimeoutError, ValueError):
        raise RuntimeError('Release verification failed: GitHub evidence unavailable') from None


def release_quality(root):
    commit = release_context(root)
    runs = release_api('repos/Driftward-LLC/factory/actions/workflows/hatchet-quality.yml/runs'
                       '?event=push&head_sha=' + commit + '&per_page=100')
    if not isinstance(runs, dict) or not isinstance(runs.get('workflow_runs'), list) or not any(
            isinstance(run, dict) and run.get('path') == '.github/workflows/hatchet-quality.yml'
            and run.get('head_sha') == commit and run.get('head_branch') == 'main'
            and run.get('event') == 'push' and run.get('status') == 'completed'
            and run.get('conclusion') == 'success'
            and run.get('repository', {}).get('full_name') == 'Driftward-LLC/factory'
            and run.get('head_repository', {}).get('full_name') == 'Driftward-LLC/factory'
            for run in runs['workflow_runs']):
        raise RuntimeError('Release requires successful quality checks for this exact commit')


def public_release_package():
    package = release_api('orgs/Driftward-LLC/packages/container/factory')
    if (not isinstance(package, dict) or package.get('name') != 'factory'
            or package.get('package_type') != 'container' or package.get('visibility') != 'public'):
        raise RuntimeError('Set the Factory GHCR package visibility to public before publishing a release')
    for field, key, expected in [('owner', 'login', 'Driftward-LLC'),
                                 ('repository', 'full_name', 'Driftward-LLC/factory')]:
        identity = package.get(field)
        if identity is not None and (not isinstance(identity, dict) or identity.get(key) != expected):
            raise RuntimeError('Unexpected release package ownership')


def release_metadata(root, output, image, version, reference):
    """Bind the public, already-tested release image to durable release evidence."""
    commit = release_context(root)
    if not re.fullmatch(RELEASE_TAG, version) or not re.fullmatch(
            re.escape(RELEASE_IMAGE) + r'@sha256:[0-9a-f]{64}', reference):
        raise RuntimeError('Invalid release version or image digest')
    run_id, attempt = os.environ.get('GITHUB_RUN_ID', ''), os.environ.get('GITHUB_RUN_ATTEMPT', '')
    if not all(re.fullmatch(r'[1-9][0-9]*', value) for value in (run_id, attempt)):
        raise RuntimeError('Invalid release producer identity')
    public_release_package()
    info = json.loads(subprocess.check_output(
        ['docker', 'image', 'inspect', '--platform', 'linux/amd64', image], text=True))[0]
    manifest = json.loads(subprocess.check_output(['docker', 'manifest', 'inspect', reference], text=True))
    config = manifest.get('config', {}).get('digest', '')
    labels = info.get('Config', {}).get('Labels') or {}
    if (manifest.get('schemaVersion') != 2 or 'manifests' in manifest
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', config)
            or info.get('Id') not in {config, reference.split('@')[1]}
            or info.get('Architecture') != 'amd64' or info.get('Os') != 'linux'
            or labels.get('org.opencontainers.image.source') != 'https://github.com/Driftward-LLC/factory'
            or labels.get('org.opencontainers.image.revision') != commit
            or labels.get('org.opencontainers.image.version') != version):
        raise RuntimeError('Release image differs from the tested source, version, or platform')
    output.mkdir(parents=True, exist_ok=True)
    with (output / 'deployment.json').open('x', encoding='utf8') as handle:
        json.dump({'format': 3, 'release_tag': version, 'commit': commit, 'image': reference,
                   'image_config': config, 'compatibility': compatibility(root),
                   'run_id': int(run_id), 'run_attempt': int(attempt)}, handle, sort_keys=True)
        handle.write('\n')


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
        raise RuntimeError(f'Production package API verification failed: HTTP {error.code}') from None
    except (ValueError, URLError, TimeoutError):
        raise RuntimeError('Production package API verification failed: invalid response or transport') from None
    if not isinstance(package, dict):
        raise RuntimeError('Production package API verification failed: expected object')
    if package.get('visibility') != 'private':
        raise RuntimeError('Production package privacy verification failed: expected private')
    if package.get('name') != 'factory-production' or package.get('package_type') != 'container':
        raise RuntimeError('Production package identity verification failed: expected name/type')
    owner = package.get('owner')
    if owner is not None and (not isinstance(owner, dict) or owner.get('login') != 'Driftward-LLC'):
        raise RuntimeError('Production package owner verification failed: unexpected owner')
    # GitHub's package schema permits absent/null repository; provenance also binds
    # the trusted publisher context and tested OCI source/revision/config digest.
    repository = package.get('repository')
    if repository is not None and (not isinstance(repository, dict)
                                  or repository.get('full_name') != 'Driftward-LLC/factory'):
        raise RuntimeError('Production package repository verification failed: unexpected repository')
    print('Production package verified: private container; owner=' +
          ('matching' if owner is not None else 'null' if 'owner' in package else 'omitted') +
          '; repository=' + ('matching' if repository is not None else
                            'null' if 'repository' in package else 'omitted'))


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
    if (os.environ.get('GITHUB_REPOSITORY') != 'Driftward-LLC/factory'
            or os.environ.get('GITHUB_EVENT_NAME') != 'push'
            or os.environ.get('GITHUB_REF') != 'refs/heads/main'):
        raise RuntimeError('Production publication requires this repository main-push workflow context')
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
    if sys.argv[1:] == ['--verify-package']:
        private_package()
    elif sys.argv[1:] == ['--check-release-quality']:
        release_quality(Path(__file__).resolve().parents[1])
    elif sys.argv[1:] == ['--verify-public-package']:
        public_release_package()
    elif len(sys.argv) == 6 and sys.argv[1] == '--release':
        release_metadata(Path(__file__).resolve().parents[1], Path(sys.argv[2]), *sys.argv[3:])
    else:
        package(Path(__file__).resolve().parents[1], Path(sys.argv[1]), sys.argv[2])
