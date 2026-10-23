#!/usr/bin/env python3
"""Fixed-host, image-only deployment. Install separately; never self-update."""
from contextlib import closing
import base64
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import zipfile
from auto_deploy_artifact import compatibility_entries, REGISTRY_IMAGE

REPO = 'Driftward-LLC/factory'
CONFIG = Path('/etc/driftward/factory-auto-deploy.json')
SHA = r'[0-9a-f]{40}'
DIGEST = r'[0-9a-f]{64}'
MAX_ARCHIVE = 64 * 1024
TERMINAL = ('completed', 'failed', 'verified_ready', 'canceled', 'canceled_after_effect', 'superseded', 'legacy_completed')


class Refusal(RuntimeError):
    pass


def require(condition, reason):
    if not condition:
        raise Refusal(reason)


def command(*args, output=None, env=None):
    result = subprocess.run(args, stdout=output or subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=1200, env={**os.environ, 'GH_PROMPT_DISABLED': '1', **(env or {})})
    require(result.returncode == 0, Path(args[0]).name + '_failed')
    return result.stdout.decode().strip() if output is None else ''


def api(path):
    return json.loads(command('gh', 'api', 'repos/' + REPO + '/' + path))


def private_json(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1 and info.st_size <= 1024 * 1024, 'unsafe_private_configuration')
    def unique(items):
        value = {}
        for key, item in items:
            require(key not in value, 'invalid_private_json')
            value[key] = item
        return value
    return json.loads(path.read_text(), object_pairs_hook=unique,
                      parse_constant=lambda _: require(False, 'invalid_private_json'))


def assert_manual_idle(work, approved_digest=None):
    """An interrupted operator rollout also fences the automatic deployment path."""
    configuration = work / 'operator-execution-configure'
    if configuration.exists() or configuration.is_symlink():
        info = configuration.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o700,
                'unsafe_manual_deployment_journal')
        require(sorted(path.name for path in configuration.iterdir()) == ['receipt.json'],
                'execution_configuration_requires_operator')
        receipt = private_json(configuration / 'receipt.json')
        snap = receipt.get('snapshot')
        require(receipt.get('phase') == 'complete' and isinstance(snap, dict)
                and re.fullmatch(DIGEST, str(snap.get('reviewer_configuration', {}).get('after')))
                and hashlib.sha256(json.dumps(snap, sort_keys=True, separators=(',', ':'),
                                             allow_nan=False).encode()).hexdigest() == receipt.get('snapshot_digest')
                and receipt.get('result') == {
                    'environment_sha256': snap.get('reviewer_configuration', {}).get('after'),
                    'independent_reviewer_enabled': True}, 'execution_configuration_requires_operator')
    directory = work / 'operator-deploy'
    if not directory.exists() and not directory.is_symlink():
        return
    info = directory.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o700,
            'unsafe_manual_deployment_journal')
    require(not list(directory.glob('*.pending')), 'prior_deployment_requires_operator')
    for path in directory.iterdir():
        require(path.suffix == '.json', 'unsafe_manual_deployment_journal')
        value = private_json(path)
        require(isinstance(value, dict), 'unsafe_manual_deployment_journal')
        if path.name == 'latest.json':
            require(set(value) == {'digest', 'updated_at'} and re.fullmatch(DIGEST, str(value.get('digest')))
                    and (directory / (value['digest'] + '.json')).is_file(), 'unsafe_manual_deployment_journal')
        elif path.name == 'baseline-anchor.json':
            require(value.get('format') == 1 and re.fullmatch(SHA, str(value.get('commit')))
                    and re.fullmatch(DIGEST, str(value.get('compatibility'))), 'unsafe_manual_deployment_journal')
        else:
            require(re.fullmatch(DIGEST + r'\.json', path.name) and value.get('format') == 1
                    and value.get('digest') == path.stem and isinstance(value.get('snapshot'), dict)
                    and hashlib.sha256(json.dumps(value['snapshot'], sort_keys=True, separators=(',', ':'),
                                                 allow_nan=False).encode()).hexdigest() == path.stem,
                    'unsafe_manual_deployment_journal')
            require(value.get('phase') in {'preview', 'approved', 'complete'}, 'prior_deployment_requires_operator')
            require(not value.get('dispatch') or value.get('phase') == 'complete'
                    or value.get('phase') == 'approved' and value.get('digest') == approved_digest,
                    'prior_deployment_requires_operator')


def record(path, value, *, timestamp=True):
    temporary = path.with_suffix('.pending')
    with temporary.open('x', encoding='utf8') as handle:
        json.dump({**value, **({'updated_at': datetime.now(timezone.utc).isoformat()} if timestamp else {})}, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sha256(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def validate_run(run, workflow, run_id, commit):
    require(run.get('id') == run_id and run.get('workflow_id') == workflow['id']
            and workflow.get('path') == '.github/workflows/hatchet-quality.yml'
            and run.get('path') == '.github/workflows/hatchet-quality.yml'
            and run.get('event') == 'push' and run.get('head_branch') == 'main'
            and run.get('head_sha') == commit and run.get('status') == 'completed'
            and run.get('conclusion') == 'success'
            and run.get('repository', {}).get('full_name') == REPO
            and run.get('head_repository', {}).get('full_name') == REPO, 'untrusted_or_unsuccessful_ci_run')


def current_main(commit):
    require(api('git/ref/heads/main')['object']['sha'] == commit, 'superseded_commit')


def checked_manifest(value, commit, compatibility, run_id, run_attempt):
    require(isinstance(value, dict)
            and set(value) == {'format', 'commit', 'image', 'image_config', 'compatibility', 'run_id', 'run_attempt'}
            and value['format'] == 2 and value['commit'] == commit
            and re.fullmatch(re.escape(REGISTRY_IMAGE) + '@sha256:' + DIGEST, str(value['image']))
            and re.fullmatch('sha256:' + DIGEST, str(value['image_config']))
            and type(value['run_id']) is int and value['run_id'] == run_id
            and type(value['run_attempt']) is int and value['run_attempt'] == run_attempt
            and value['compatibility'] == compatibility, 'compatibility_review_required')
    return value


def unpack(archive, directory):
    # Never extract a supplied path, symlink, script or secret into the host.
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        require(len(entries) == 1 and entries[0].filename == 'deployment.json',
                'unexpected_artifact_members')
        for item in entries:
            require(item.file_size <= 4096
                    and not stat.S_ISLNK(item.external_attr >> 16), 'unsafe_artifact_member')
            with bundle.open(item) as source, (directory / item.filename).open('xb') as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
    return json.loads((directory / 'deployment.json').read_text())


def source_entries(commit):
    # Use the root-installed policy and GitHub's source tree, not the build's
    # self-reported fingerprint: a changed CI script cannot approve itself.
    tree = api(f'git/trees/{commit}?recursive=1')
    require(tree.get('truncated') is False, 'incomplete_source_tree')
    return [f"{item['mode']} {item['type']} {item['sha']}\t{item['path']}".encode()
            for item in tree['tree'] if item['type'] != 'tree']


def deployment_artifact(run_id, commit):
    artifacts = api(f'actions/runs/{run_id}/artifacts?per_page=100')
    matches = [item for item in artifacts['artifacts'] if item['name'] == 'factory-production-' + commit]
    require(len(matches) == 1 and artifacts['total_count'] <= 100, 'deployment_artifact_missing_or_ambiguous')
    artifact = matches[0]
    require(not artifact['expired'] and 0 < artifact['size_in_bytes'] <= MAX_ARCHIVE
            and re.fullmatch('sha256:' + DIGEST, str(artifact.get('digest')))
            and artifact['workflow_run']['id'] == run_id
            and artifact['workflow_run']['head_sha'] == commit, 'invalid_artifact_provenance')
    return {**{key: artifact[key] for key in ('id', 'name', 'expired', 'size_in_bytes', 'digest')},
            'workflow_run': {key: artifact['workflow_run'][key] for key in ('id', 'head_sha')}}

def get_image(run_id, commit, config, directory, run_attempt, credentials, *, expected_artifact=None):
    # Use the root-installed policy and GitHub's source tree, not the build's
    # self-reported fingerprint: a changed CI script cannot approve itself.
    tree = api(f'git/trees/{commit}?recursive=1')
    require(tree.get('truncated') is False, 'incomplete_source_tree')
    entries = [f"{item['mode']} {item['type']} {item['sha']}\t{item['path']}".encode()
               for item in tree['tree'] if item['type'] != 'tree']
    require(compatibility_entries(entries) == config['compatibility'], 'compatibility_review_required')
    artifact = deployment_artifact(run_id, commit)
    require(expected_artifact is None or artifact == expected_artifact, 'deployment_artifact_changed')
    archive = directory / 'artifact.zip'
    with archive.open('xb') as handle:
        command('gh', 'api', f'repos/{REPO}/actions/artifacts/{artifact["id"]}/zip', output=handle)
    require(archive.stat().st_size <= MAX_ARCHIVE and 'sha256:' + sha256(archive) == artifact['digest'], 'artifact_digest_mismatch')
    manifest = checked_manifest(unpack(archive, directory), commit, config['compatibility'], run_id, run_attempt)
    docker_config = directory / 'docker-config'
    docker_config.mkdir(mode=0o700)
    env = {'DOCKER_CONFIG': str(docker_config)}
    try:
        # A nonempty auths map disables Docker's native credential-helper
        # autodetection; never let a temporary token reach the host keychain.
        auth = base64.b64encode((credentials['username'] + ':' + credentials['token']).encode()).decode()
        descriptor = os.open(docker_config / 'config.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w') as handle:
            json.dump({'auths': {'ghcr.io': {'auth': auth}}}, handle)
        reference = manifest['image']
        registry_manifest = json.loads(command('docker', 'manifest', 'inspect', reference, env=env))
        require(registry_manifest.get('schemaVersion') == 2 and 'manifests' not in registry_manifest
                and registry_manifest.get('config', {}).get('digest') == manifest['image_config'], 'registry_config_mismatch')
        command('docker', 'image', 'pull', '--platform', 'linux/amd64', reference, env=env)
        info = json.loads(command('docker', 'image', 'inspect', reference, env=env))[0]
        # Classic Docker IDs are config digests; containerd IDs can be manifest
        # digests. Pin the local ID only after verifying the exact repository digest.
        image = info['Id']
        labels = info['Config'].get('Labels') or {}
        require(re.fullmatch('sha256:' + DIGEST, image) and reference in info.get('RepoDigests', [])
                and image in {manifest['image_config'], reference.split('@')[1]}
                and info['Architecture'] == 'amd64' and info['Os'] == 'linux'
                and labels.get('org.opencontainers.image.revision') == commit
                and labels.get('org.opencontainers.image.source') == 'https://github.com/' + REPO, 'image_provenance_mismatch')
        return image
    finally:
        shutil.rmtree(docker_config)


def assert_quiescent(conn):
    marks = ','.join('?' for _ in TERMINAL)
    counts = [conn.execute(f'SELECT count(*) FROM factory_attempts WHERE status NOT IN ({marks})', TERMINAL).fetchone()[0],
              conn.execute('SELECT count(*) FROM factory_execution_leases WHERE released_at IS NULL').fetchone()[0],
              conn.execute("SELECT count(*) FROM bridge_jobs WHERE state NOT IN ('completed','ready_for_human_review','failed')").fetchone()[0]]
    require(not any(counts), 'active_or_unresolved_execution')


def paused_execution(install):
    return tuple(row['Service'] for row in install.service_rows()
                 if row['Service'] == 'bridge-executor-worker' and row.get('State') == 'exited')


def healthy(install, paused=()):
    rows = {row['Service']: row for row in install.service_rows()}
    require(set(rows) == set(install.services) and all(
        (row.get('State') == 'exited' if name in paused else
         row.get('State') == 'running' and row.get('Health', '') in ('', 'healthy'))
        for name, row in rows.items()), 'service_health_failed')
    install.preflight()


def resume(install, paused=()):
    install.preflight()
    selected = [name for name in install.services if name not in paused]
    install.compose('up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '180', *selected, private=True)
    healthy(install, paused)


def rollout(production, install, image, commit, run_id, work):
    status = work / 'status.json'
    if status.exists():
        require(private_json(status).get('phase') in {'complete', 'blocked'}, 'prior_deployment_requires_operator')
    paused = paused_execution(install)
    healthy(install, paused)
    info = json.loads(command('docker', 'image', 'inspect', install.images['factory']))[0]
    if info['Config']['Labels'].get('org.opencontainers.image.revision') == commit:
        print('Already deployed; no restart.')
        return
    volume = install.project + '-bridge-data'
    mount = Path(json.loads(command('docker', 'volume', 'inspect', volume))[0]['Mountpoint'])
    db = mount / 'bridge.db'
    require(db.is_file() and not db.is_symlink(), 'bridge_database_unavailable')
    # This is a write barrier, not a state edit. Holding SQLite's writer lock
    # closes the race between the idle check and stopping execution workers.
    with closing(sqlite3.connect(db.as_uri() + '?mode=rw', uri=True, timeout=5)) as conn:
        conn.execute('BEGIN IMMEDIATE')
        assert_quiescent(conn)
        current_main(commit)
        record(status, {'phase': 'quiescing', 'commit': commit, 'run_id': run_id})
        install.compose('stop', '--timeout', '60', 'execution-dispatch-worker', 'bridge-executor-worker', private=True)
        conn.rollback()
    snapshot = work / f'backup-{commit}-{run_id}'
    record(status, {'phase': 'backing_up', 'commit': commit, 'run_id': run_id})
    production.state.backup(install, snapshot, apply=True)
    record(status, {'phase': 'upgrading', 'commit': commit, 'run_id': run_id})
    install.record_upgrade(image, snapshot, apply=True)
    updated = production.Production(install.directory)
    record(status, {'phase': 'starting', 'commit': commit, 'run_id': run_id})
    # No migrations, gateway restart, journal restore or automatic state rollback.
    # A partial failure stays latched for an operator, rather than guessing about effects.
    resume(updated, paused)
    record(status, {'phase': 'complete', 'commit': commit, 'run_id': run_id, 'backup': str(snapshot), 'image': image})
    print('Deployment healthy; exact image active and cold backup retained.')


def deploy(config, run_id, commit, credentials):
    require(config.get('enabled') is True, 'automatic_deployment_disabled')
    bundle = Path(config['production_bundle'])
    sys.path.insert(0, str(bundle))
    spec = importlib.util.spec_from_file_location('factory_production', bundle / 'factory-production.py')
    production = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(production)
    work = Path(config['work_directory'])
    assert_manual_idle(work)
    require(work.is_dir() and work.stat().st_uid == 0 and stat.S_IMODE(work.stat().st_mode) == 0o700, 'unsafe_work_directory')
    with production.portable.installation_lock(Path(config['installation'])):
        install = production.Production(Path(config['installation']))
        with production.portable.installation_lock(Path('/tmp') / ('factory-tenant-' + install.tenant)):
            assert_manual_idle(work)
            install.preflight()
            run = api(f'actions/runs/{run_id}')
            validate_run(run, api('actions/workflows/hatchet-quality.yml'), run_id, commit)
            attempt = run.get('run_attempt')
            require(type(attempt) is int and attempt > 0, 'invalid_run_attempt')
            current_main(commit)
            require(shutil.disk_usage(work).free >= 12 * 1024**3, 'insufficient_backup_space')
            with tempfile.TemporaryDirectory(prefix='artifact-', dir=work) as temporary:
                image = get_image(run_id, commit, config, Path(temporary), attempt, credentials)
                # A rerun must not replace the successful check while downloading.
                after = api(f'actions/runs/{run_id}')
                validate_run(after, api('actions/workflows/hatchet-quality.yml'), run_id, commit)
                require(after.get('run_attempt') == attempt, 'ci_run_attempt_changed')
                rollout(production, install, image, commit, run_id, work)


def main():
    os.umask(0o077)
    require(os.geteuid() == 0, 'root_required')
    require(len(sys.argv) == 2, 'invalid_deployment_command')
    config = private_json(CONFIG)
    if sys.argv[1] == 'status':
        print(json.dumps({'configured': True, 'enabled': config.get('enabled') is True}))
        return
    match = re.fullmatch(r'deploy ([1-9][0-9]{0,19}) (' + SHA + ')', sys.argv[1])
    require(match is not None, 'invalid_deployment_command')
    envelope = sys.stdin.buffer.read(8193)
    require(len(envelope) <= 8192, 'invalid_registry_credentials')
    credentials = json.loads(envelope)
    require(isinstance(credentials, dict) and set(credentials) == {'username', 'token'}
            and isinstance(credentials['username'], str)
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?', credentials['username'])
            and isinstance(credentials['token'], str) and 1 <= len(credentials['token']) <= 4096
            # GitHub tokens are opaque; enforce transport safety, not a token format.
            and re.fullmatch(r'[!-~]+', credentials['token']), 'invalid_registry_credentials')
    from factory_deploy_operator import forced_deployment
    from types import SimpleNamespace
    if not forced_deployment(SimpleNamespace(**globals()), int(match[1]), match[2], credentials):
        deploy(config, int(match[1]), match[2], credentials)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Never emit captured process output, configuration, journals or tracebacks.
        print('Deployment stopped: ' + (str(error) if isinstance(error, Refusal) else type(error).__name__)
              + '. No automatic retry or state restore.', file=sys.stderr)
        sys.exit(1)
