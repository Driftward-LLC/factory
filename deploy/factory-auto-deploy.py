#!/usr/bin/env python3
"""Fixed-host, image-only deployment. Install separately; never self-update."""
from contextlib import closing
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
from auto_deploy_artifact import compatibility_entries

REPO = 'Driftward-LLC/factory'
CONFIG = Path('/etc/driftward/factory-auto-deploy.json')
SHA = r'[0-9a-f]{40}'
DIGEST = r'[0-9a-f]{64}'
MAX_ARCHIVE = 4 * 1024**3
TERMINAL = ('completed', 'failed', 'verified_ready', 'canceled', 'canceled_after_effect', 'superseded', 'legacy_completed')


class Refusal(RuntimeError):
    pass


def require(condition, reason):
    if not condition:
        raise Refusal(reason)


def command(*args, output=None):
    result = subprocess.run(args, stdout=output or subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=1200, env={**os.environ, 'GH_PROMPT_DISABLED': '1'})
    require(result.returncode == 0, Path(args[0]).name + '_failed')
    return result.stdout.decode().strip() if output is None else ''


def api(path):
    return json.loads(command('gh', 'api', 'repos/' + REPO + '/' + path))


def private_json(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1, 'unsafe_private_configuration')
    return json.loads(path.read_text())


def record(path, value):
    temporary = path.with_suffix('.pending')
    with temporary.open('x', encoding='utf8') as handle:
        json.dump({**value, 'updated_at': datetime.now(timezone.utc).isoformat()}, handle)
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


def checked_manifest(value, commit, compatibility):
    require(set(value) == {'format', 'commit', 'image', 'compatibility', 'archive_sha256'}
            and value['format'] == 1 and value['commit'] == commit
            and re.fullmatch('sha256:' + DIGEST, str(value['image']))
            and re.fullmatch(DIGEST, str(value['archive_sha256']))
            and value['compatibility'] == compatibility, 'compatibility_review_required')
    return value


def unpack(archive, directory):
    # Never extract a supplied path, symlink, script or secret into the host.
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        require(len(entries) == 2 and {item.filename for item in entries} == {'factory-image.tar', 'deployment.json'},
                'unexpected_artifact_members')
        for item in entries:
            require(item.file_size <= (MAX_ARCHIVE if item.filename == 'factory-image.tar' else 4096)
                    and not stat.S_ISLNK(item.external_attr >> 16), 'unsafe_artifact_member')
            with bundle.open(item) as source, (directory / item.filename).open('xb') as target:
                shutil.copyfileobj(source, target, 1024 * 1024)
    return json.loads((directory / 'deployment.json').read_text())


def get_image(run_id, commit, config, directory):
    # Use the root-installed policy and GitHub's source tree, not the build's
    # self-reported fingerprint: a changed CI script cannot approve itself.
    tree = api(f'git/trees/{commit}?recursive=1')
    require(tree.get('truncated') is False, 'incomplete_source_tree')
    entries = [f"{item['mode']} {item['type']} {item['sha']}\t{item['path']}".encode()
               for item in tree['tree'] if item['type'] != 'tree']
    require(compatibility_entries(entries) == config['compatibility'], 'compatibility_review_required')
    artifacts = api(f'actions/runs/{run_id}/artifacts?per_page=100')
    matches = [item for item in artifacts['artifacts'] if item['name'] == 'factory-production-' + commit]
    require(len(matches) == 1 and artifacts['total_count'] <= 100, 'deployment_artifact_missing_or_ambiguous')
    artifact = matches[0]
    require(not artifact['expired'] and 0 < artifact['size_in_bytes'] <= MAX_ARCHIVE
            and re.fullmatch('sha256:' + DIGEST, str(artifact.get('digest')))
            and artifact['workflow_run']['id'] == run_id
            and artifact['workflow_run']['head_sha'] == commit, 'invalid_artifact_provenance')
    archive = directory / 'artifact.zip'
    with archive.open('xb') as handle:
        command('gh', 'api', f'repos/{REPO}/actions/artifacts/{artifact["id"]}/zip', output=handle)
    require(archive.stat().st_size <= MAX_ARCHIVE and 'sha256:' + sha256(archive) == artifact['digest'], 'artifact_digest_mismatch')
    manifest = checked_manifest(unpack(archive, directory), commit, config['compatibility'])
    require(sha256(directory / 'factory-image.tar') == manifest['archive_sha256'], 'image_archive_digest_mismatch')
    command('docker', 'image', 'load', '--input', str(directory / 'factory-image.tar'))
    info = json.loads(command('docker', 'image', 'inspect', manifest['image']))[0]
    labels = info['Config'].get('Labels') or {}
    require(info['Id'] == manifest['image'] and info['Architecture'] == 'amd64' and info['Os'] == 'linux'
            and labels.get('org.opencontainers.image.revision') == commit
            and labels.get('org.opencontainers.image.source') == 'https://github.com/' + REPO, 'image_provenance_mismatch')
    return manifest['image']


def assert_quiescent(conn):
    marks = ','.join('?' for _ in TERMINAL)
    counts = [conn.execute(f'SELECT count(*) FROM factory_attempts WHERE status NOT IN ({marks})', TERMINAL).fetchone()[0],
              conn.execute('SELECT count(*) FROM factory_execution_leases WHERE released_at IS NULL').fetchone()[0],
              conn.execute("SELECT count(*) FROM bridge_jobs WHERE state NOT IN ('completed','ready_for_human_review','failed')").fetchone()[0]]
    require(not any(counts), 'active_or_unresolved_execution')


def healthy(install):
    rows = {row['Service']: row for row in install.service_rows()}
    require(set(rows) == set(install.services) and all(
        row.get('State') == 'running' and row.get('Health', '') in ('', 'healthy') for row in rows.values()), 'service_health_failed')
    install.preflight()


def resume(install):
    install.preflight()
    install.compose('up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '180', *install.services, private=True)
    healthy(install)


def rollout(production, install, image, commit, run_id, work):
    status = work / 'status.json'
    if status.exists():
        require(private_json(status).get('phase') in {'complete', 'blocked'}, 'prior_deployment_requires_operator')
    healthy(install)
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
    resume(updated)
    record(status, {'phase': 'complete', 'commit': commit, 'run_id': run_id, 'backup': str(snapshot), 'image': image})
    print('Deployment healthy; exact image active and cold backup retained.')


def deploy(config, run_id, commit):
    require(config.get('enabled') is True, 'automatic_deployment_disabled')
    bundle = Path(config['production_bundle'])
    sys.path.insert(0, str(bundle))
    spec = importlib.util.spec_from_file_location('factory_production', bundle / 'factory-production.py')
    production = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(production)
    work = Path(config['work_directory'])
    require(work.is_dir() and work.stat().st_uid == 0 and stat.S_IMODE(work.stat().st_mode) == 0o700, 'unsafe_work_directory')
    with production.portable.installation_lock(Path(config['installation'])):
        install = production.Production(Path(config['installation']))
        with production.portable.installation_lock(Path('/tmp') / ('factory-tenant-' + install.tenant)):
            install.preflight()
            validate_run(api(f'actions/runs/{run_id}'), api('actions/workflows/hatchet-quality.yml'), run_id, commit)
            current_main(commit)
            require(shutil.disk_usage(work).free >= 12 * 1024**3, 'insufficient_backup_space')
            with tempfile.TemporaryDirectory(prefix='artifact-', dir=work) as temporary:
                image = get_image(run_id, commit, config, Path(temporary))
                # A rerun must not replace the successful check while downloading.
                validate_run(api(f'actions/runs/{run_id}'), api('actions/workflows/hatchet-quality.yml'), run_id, commit)
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
    deploy(config, int(match[1]), match[2])


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Never emit captured process output, configuration, journals or tracebacks.
        print('Deployment stopped: ' + (str(error) if isinstance(error, Refusal) else type(error).__name__)
              + '. No automatic retry or state restore.', file=sys.stderr)
        sys.exit(1)
