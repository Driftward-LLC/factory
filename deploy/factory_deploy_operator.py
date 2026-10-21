"""Audited, single-release approvals; imported only by the root canonical CLI."""
from contextlib import closing, contextmanager, redirect_stdout
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import tempfile

API_VERSION = 1
GUARDED_SOURCE = 'hatchet/workers/bridge-executor-worker/src/workflows/create-draft-pr-executor.ts'
GUARDED_BLOB = '5bc72abddaaa24343bcbe0d3fc67e2d161010c04'
REVIEWABLE = {'factory/factory': '100755', 'deploy/CLI.md': '100644',
              '.github/workflows/hatchet-quality.yml': '100644'}
INSTALLED_MODES = {'deploy/factory-auto-deploy.py': '100644', 'deploy/auto_deploy_artifact.py': '100644',
                   'deploy/factory-profile-runtime.py': '100644', 'deploy/factory-artifact-runtime.py': '100644',
                   'deploy/factory_deploy_operator.py': '100644', 'deploy/factory-cli.py': '100644'}
PHASES = {'preview', 'approved', 'consumed', 'complete', 'failed'}
ACTIVATION_BLOBS = {
    ".github/workflows/factory-deploy.yml": "b346138c8d4c0839549c46b486e9bae53b0fe886",
    "AGENTS.md": "44a02dbeb554197edef4004766b4700cbde5f36d",
    "deploy/INDEPENDENT-REVIEW.md": "4d299f3344645b56e733bcf3d3ab848892719bf1",
    "deploy/factory-observers/posthog/__init__.py": "a794872a1fab8ad918a56c26fc9a0c0d70664cc6",
    "deploy/factory-observers/posthog/plugin.yaml": "76429d07396067a1651e8db6aa938c331d038796",
    "deploy/independent-reviewer/native_agent.py": "82adedaecf357c0a8806e2317998490f5a315a4d",
    "deploy/independent-reviewer/review_observer.py": "e0257d7c70450642221987e952812273d3f9d8db",
    "deploy/independent-reviewer/runner.py": "5b50c94e6422e9936ebf5e418d7b467f56d1517d",
    "deploy/systemd/driftward-hermes-factory-gateway.service": "51e4eabdefac240cf34f798fd2a209366866d665",
    "hatchet/workers/bridge-executor-worker/src/hermes/create-draft-pr.test.ts": "51f67b925c7e4e08225ee86d6b9bd9419e764dac",
    "hatchet/workers/bridge-executor-worker/src/hermes/create-draft-pr.ts": "8c8138d0fe3348d83a35f8e4c4601c73095417e6",
    "hatchet/workers/bridge-executor-worker/src/hermes/independent-review.ts": "e74eb196d831f18994fef22e62a071aa6ee7243a",
    "tools/check_factory_profile_runtime_native.py": "e9d25c9ad4ebd01b5d930da4ecfab850f415b8be"
}


class Refusal(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise Refusal(reason)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def strict_json(source):
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, 'invalid_deployment_json')
            value[key] = item
        return value
    return json.loads(source, object_pairs_hook=pairs,
                      parse_constant=lambda _: require(False, 'invalid_deployment_json'))


def private_json(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and info.st_nlink == 1
            and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size <= 1024 * 1024,
            'unsafe_deployment_record')
    return strict_json(path.read_text())


def private_directory(path):
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0
            and stat.S_IMODE(info.st_mode) == 0o700, 'unsafe_deployment_directory')


def exists(path):
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False


def write(auto, path, value):
    if exists(path):
        private_json(path)
    auto.record(path, value)


def tree(auto, commit):
    entries = auto.source_entries(commit)
    result = {}
    for entry in entries:
        description, name = entry.decode().split('\t', 1)
        mode, kind, sha = description.split(' ')
        require(re.fullmatch(r'[A-Za-z0-9_./@+-]+', name) and '..' not in name.split('/')
                and not name.startswith('/') and name not in result
                and re.fullmatch(auto.SHA, sha), 'unsafe_source_tree')
        result[name] = {'mode': mode, 'type': kind, 'sha': sha}
    return result, auto.compatibility_entries(entries)


def blob(path):
    content = path.read_bytes()
    return hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()


def protected_changes(auto, baseline, target, installed):
    from auto_deploy_artifact import routine_path
    changes, blockers = [], []
    for name in sorted(set(baseline) | set(target)):
        before, after = baseline.get(name), target.get(name)
        if before == after or routine_path(name):
            continue
        changes.append({'path': name, 'before': before, 'after': after})
        expected_mode = REVIEWABLE.get(name, INSTALLED_MODES.get(name) if name in installed else None)
        allowed = (expected_mode and after and after['type'] == 'blob' and after['mode'] == expected_mode
                   and (before is None or before['type'] == 'blob' and before['mode'] == expected_mode))
        if name in installed:
            allowed = allowed and after['sha'] == blob(installed[name])
        elif name in ACTIVATION_BLOBS:
            allowed = after == {'mode': '100644', 'type': 'blob', 'sha': ACTIVATION_BLOBS[name]}
        if not allowed:
            blockers.append(name)
    require(len(changes) <= 256, 'too_many_protected_changes')
    return changes, blockers


def production(config):
    bundle = Path(config['production_bundle'])
    sys.path.insert(0, str(bundle))
    spec = importlib.util.spec_from_file_location('factory_operator_production', bundle / 'factory-production.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def config_for(auto, binding):
    config = private_json(auto.CONFIG)
    require(isinstance(config, dict) and set(config) == {
        'enabled', 'installation', 'production_bundle', 'work_directory', 'compatibility'}, 'invalid_deployment_configuration')
    require(type(config['enabled']) is bool, 'invalid_deployment_configuration')
    require(all(isinstance(config[name], str) and Path(config[name]).is_absolute()
                and len(config[name]) <= 4096 and all(char.isprintable() for char in config[name])
                for name in ('installation', 'production_bundle', 'work_directory'))
            and isinstance(config['compatibility'], str) and re.fullmatch(auto.DIGEST, config['compatibility']),
            'invalid_deployment_configuration')
    require(all(config[name] == binding[name] for name in ('installation', 'production_bundle')),
            'deployment_binding_differs')
    private_directory(Path(config['work_directory']))
    return config


@contextmanager
def locked(auto, binding):
    config = config_for(auto, binding)
    prod = production(config)
    with prod.portable.installation_lock(Path(config['installation'])):
        install = prod.Production(Path(config['installation']))
        require(install.metadata['installation_id'] == binding['installation_id'], 'deployment_installation_changed')
        with prod.portable.installation_lock(Path('/tmp') / ('factory-tenant-' + install.tenant)):
            # Re-read after both locks; another operator cannot change the target unnoticed.
            require(config_for(auto, binding) == config, 'deployment_configuration_changed')
            yield config, prod, install


def records_directory(config, *, create=True):
    directory = Path(config['work_directory']) / 'operator-deploy'
    if not exists(directory):
        if not create:
            return directory
        directory.mkdir(mode=0o700)
        descriptor = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    private_directory(directory)
    require(not list(directory.glob('*.pending')), 'interrupted_deployment_record')
    return directory


def rollout_status(auto, work):
    require(not exists(work / 'status.pending'), 'interrupted_deployment_record')
    path = work / 'status.json'
    if not exists(path):
        return None
    value = private_json(path)
    require(isinstance(value, dict) and value.get('phase') in {
        'complete', 'blocked', 'quiescing', 'backing_up', 'upgrading', 'starting'}, 'invalid_deployment_status')
    return value['phase']


def check_idle(auto, install):
    auto.healthy(install, auto.paused_execution(install))
    mount = Path(json.loads(auto.command('docker', 'volume', 'inspect', install.project + '-bridge-data'))[0]['Mountpoint'])
    database = mount / 'bridge.db'
    require(database.is_file() and not database.is_symlink(), 'bridge_database_unavailable')
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5)) as conn:
        auto.assert_quiescent(conn)


def anchor(auto, directory, config, current_commit):
    path = directory / 'baseline-anchor.json'
    if exists(path):
        value = private_json(path)
        require(isinstance(value, dict) and value.get('format') == 1
                and re.fullmatch(auto.SHA, str(value.get('commit')))
                and value.get('compatibility') == config['compatibility'], 'deployment_baseline_changed')
        commit = value['commit']
    else:
        commit = current_commit
    baseline, fingerprint = tree(auto, commit)
    require(fingerprint == config['compatibility'], 'deployment_baseline_anchor_required')
    return commit, baseline


def current_identity(auto, install):
    info = json.loads(auto.command('docker', 'image', 'inspect', install.images['factory']))[0]
    labels = info['Config'].get('Labels') or {}
    commit = labels.get('org.opencontainers.image.revision')
    require(info['Id'] == install.images['factory'] and isinstance(commit, str)
            and re.fullmatch(auto.SHA, commit)
            and labels.get('org.opencontainers.image.source') == 'https://github.com/' + auto.REPO,
            'current_image_provenance_unavailable')
    return {'current_image': install.images['factory'], 'current_commit': commit}


def snapshot(auto, binding, config, prod, install, directory, run_id, installed):
    work = Path(config['work_directory'])
    require(rollout_status(auto, work) in {None, 'complete', 'blocked'}, 'prior_deployment_requires_operator')
    check_idle(auto, install)
    run = auto.api(f'actions/runs/{run_id}')
    commit = run.get('head_sha')
    require(isinstance(commit, str) and re.fullmatch(auto.SHA, commit), 'untrusted_or_unsuccessful_ci_run')
    auto.validate_run(run, auto.api('actions/workflows/hatchet-quality.yml'), run_id, commit)
    auto.current_main(commit)
    artifact = auto.deployment_artifact(run_id, commit)
    current_commit = current_identity(auto, install)['current_commit']
    require(current_commit != commit, 'release_already_deployed')
    baseline_commit, baseline = anchor(auto, directory, config, current_commit)
    target, fingerprint = tree(auto, commit)
    changes, blockers = protected_changes(auto, baseline, target, installed)
    if current_commit != baseline_commit:
        current, _ = tree(auto, current_commit)
        transition, transition_blockers = protected_changes(auto, current, target, installed)
        changes = [{**change, 'comparison': 'baseline_to_target'} for change in changes]
        changes.extend({**change, 'comparison': 'current_to_target'} for change in transition)
        blockers = sorted(set(blockers) | set(transition_blockers))
    bundle = Path(config['production_bundle'])
    host_files = {str(path): auto.sha256(path) for path in installed.values()}
    bundle_files = {path.name: auto.sha256(path) for path in bundle.iterdir()
                    if path.suffix in {'.py', '.yml'}}
    require(type(run.get('run_attempt')) is int and run['run_attempt'] >= 1, 'invalid_ci_run_attempt')
    require(auto.shutil.disk_usage(work).free >= 12 * 1024**3, 'insufficient_backup_space')
    value = {'format': 1, 'installation': config['installation'], 'installation_id': binding['installation_id'],
             'project': install.project, 'tenant': install.tenant, 'current_image': install.images['factory'],
             'current_commit': current_commit, 'commit': commit, 'run_id': run_id,
             'run_attempt': run['run_attempt'], 'run_updated_at': run.get('updated_at'),
             'artifact': artifact, 'configuration': digest(config), 'baseline_commit': baseline_commit,
             'baseline_compatibility': config['compatibility'], 'target_compatibility': fingerprint,
             'protected_changes': changes, 'blocked_paths': blockers,
             'host_files': host_files, 'bundle_files': bundle_files,
             'installation_files': {name: auto.sha256(install.directory / name)
                                    for name in ('.env', '.factory-install.json', '.factory-cutover.json')},
             'baseline_adoption': False}
    value['paused_execution'] = list(auto.paused_execution(install))
    return value


def read_preview(auto, directory, identifier):
    require(re.fullmatch(auto.DIGEST, identifier), 'invalid_preview_digest')
    value = private_json(directory / (identifier + '.json'))
    require(isinstance(value, dict) and value.get('format') == 1 and value.get('digest') == identifier
            and value.get('phase') in PHASES and isinstance(value.get('snapshot'), dict)
            and digest(value['snapshot']) == identifier, 'invalid_deployment_preview')
    return value


def report(value):
    snap = value['snapshot']
    phase = value['phase']
    blocked = bool(snap['blocked_paths'])
    command = ('Review protected changes; this CLI cannot approve them.' if blocked else
               'factory deploy approve --preview ' + value['digest'] + ' --reason "Reviewed release" --yes' if phase == 'preview' else
               'factory deploy apply --preview ' + value['digest'] + ' --yes' if phase == 'approved' else
               'factory deploy status')
    result = {'phase': phase, 'preview': value['digest'], 'installation': snap['installation'],
            'current_image': snap['current_image'], 'current_commit': snap['current_commit'],
            'target_commit': snap['commit'], 'run_id': snap['run_id'], 'run_attempt': snap['run_attempt'],
            'artifact_id': snap['artifact']['id'], 'artifact_digest': snap['artifact']['digest'],
            'protected_changes': snap['protected_changes'], 'blocked_paths': snap['blocked_paths'],
            'eligible': not blocked and phase in {'preview', 'approved'}, 'restart_impact': 'Factory services stop for a cold backup; Hermes stays running.',
            'baseline_adoption': snap['baseline_adoption'], 'next_command': command}
    if phase == 'complete':
        require(isinstance(value.get('result'), dict) and set(value['result']) == {'image', 'commit'}
                and isinstance(value['result']['image'], str)
                and re.fullmatch(r'sha256:[0-9a-f]{64}', value['result']['image'])
                and value['result']['commit'] == snap['commit'],
                'invalid_deployment_result')
        result.update(previous_image=snap['current_image'], previous_commit=snap['current_commit'],
                      current_image=value['result']['image'], current_commit=value['result']['commit'])
    if value.get('dispatch'):
        result.update(dispatch_phase=value['dispatch']['phase'], deploy_run_id=value['dispatch']['deploy_run_id'],
                      eligible=False, next_command='factory deploy status --json')
    return result


def execution_snapshot(auto, binding, config, install, installed, readiness):
    identity = current_identity(auto, install)
    check_idle(auto, install)
    status = private_json(Path(config['work_directory']) / 'status.json')
    require(status.get('phase') == 'complete' and status.get('image') == identity['current_image']
            and status.get('commit') == identity['current_commit'], 'audited_image_receipt_required')
    run = auto.api(f'actions/runs/{status["run_id"]}')
    auto.validate_run(run, auto.api('actions/workflows/hatchet-quality.yml'), status['run_id'], identity['current_commit'])
    source, _ = tree(auto, identity['current_commit'])
    require(source.get(GUARDED_SOURCE) == {'mode': '100644', 'type': 'blob', 'sha': GUARDED_BLOB},
            'terminal_inspection_guard_unverified')
    # Read only a public source file from the pinned image, with no credentials or mounts.
    content = auto.command('docker', 'run', '--rm', '--network', 'none', '--read-only',
                           '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                           '--entrypoint', 'cat', identity['current_image'], '/app/' + GUARDED_SOURCE)
    raw = (content + '\n').encode()
    require(hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest() == GUARDED_BLOB,
            'terminal_inspection_guard_unverified')
    profile = readiness(binding, install)
    roles = profile.get('profiles', {})
    require(profile.get('reviewer_routed') is True and profile.get('gateway_executor_profile_selected') is True
            and profile.get('executor_auth_ready') is True
            and profile.get('gateway_socket_listening') is True
            and profile.get('gateway_service', {}).get('ActiveState') == 'active'
            and roles.get('executor', {}).get('engineering_enabled') is True
            and roles.get('executor', {}).get('capabilities_ready') is True
            and roles.get('executor', {}).get('capabilities_match_default') is True
            and roles.get('reviewer', {}).get('auth_present') is True,
            'execution_roles_not_ready')
    return {'installation': config['installation'], 'installation_id': binding['installation_id'],
            **identity, 'guarded_blob': GUARDED_BLOB, 'configuration': digest(config),
            'paused_execution': list(auto.paused_execution(install)), 'roles': profile,
            'host_files': {str(path): auto.sha256(path) for path in installed.values()},
            'installation_files': {name: auto.sha256(install.directory / name)
                                   for name in ('.env', '.factory-install.json', '.factory-cutover.json')}}


def execution_run(auto, binding, args, installed, readiness):
    os.umask(0o077)
    require(os.geteuid() == 0, 'root_required')
    with locked(auto, binding) as (config, prod, install):
        directory = Path(config['work_directory']) / 'operator-execution'
        if args.action == 'status':
            result = {'installation': config['installation'], **current_identity(auto, install),
                      'paused_execution': list(auto.paused_execution(install)), 'phase': 'unknown',
                      'next_command': 'factory execution preview --json'}
            if exists(directory / 'receipt.json'):
                private_directory(directory)
                receipt = private_json(directory / 'receipt.json')
                result['phase'] = receipt['phase']
            return result
        require(rollout_status(auto, Path(config['work_directory'])) == 'complete', 'deployment_not_complete')
        auto.assert_manual_idle(Path(config['work_directory']))
        snap = execution_snapshot(auto, binding, config, install, installed, readiness)
        identifier = digest(snap)
        if args.action == 'preview':
            require(not args.yes and args.reason is None and args.snapshot_digest is None, 'unexpected_execution_approval')
            return {'snapshot_digest': identifier, 'installation': config['installation'], **current_identity(auto, install),
                    'paused_execution': snap['paused_execution'], 'guarded_blob': GUARDED_BLOB,
                    'next_command': 'factory execution resume --snapshot-digest ' + identifier + ' --reason "Reviewed execution activation" --yes --json'}
        require(args.yes and args.snapshot_digest == identifier and isinstance(args.reason, str)
                and 1 <= len(args.reason.strip()) <= 512 and all(c.isprintable() for c in args.reason),
                'execution_preview_stale_or_consent_missing')
        require(snap['paused_execution'] == ['bridge-executor-worker'], 'executor_not_paused')
        if not exists(directory):
            directory.mkdir(mode=0o700)
        private_directory(directory)
        path = directory / 'receipt.json'
        if exists(path):
            require(private_json(path).get('phase') == 'complete', 'execution_requires_operator')
        receipt = {'phase': 'consumed', 'snapshot_digest': identifier, 'snapshot': snap,
                   'reason': args.reason, 'uid': os.geteuid()}
        write(auto, path, receipt)
        try:
            mount = Path(json.loads(auto.command('docker', 'volume', 'inspect', install.project + '-bridge-data'))[0]['Mountpoint'])
            database = mount / 'bridge.db'
            require(database.is_file() and not database.is_symlink(), 'bridge_database_unavailable')
            with closing(sqlite3.connect(database.as_uri() + '?mode=rw', uri=True, timeout=5)) as conn:
                conn.execute('BEGIN IMMEDIATE')
                auto.assert_quiescent(conn)
                # The worker cannot read/claim durable work until this barrier is released.
                install.compose('up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'bridge-executor-worker', private=True)
                conn.rollback()
            auto.healthy(install)
            receipt['phase'] = 'complete'
        except Exception:
            receipt['phase'] = 'failed'
            install.compose('stop', '--timeout', '60', 'bridge-executor-worker', private=True)
            raise
        finally:
            write(auto, path, receipt)
        return {'phase': 'complete', 'snapshot_digest': identifier, 'installation': config['installation'],
                'next_command': 'factory execution status --json'}




def queue_deployment(auto, value, path):
    snap = value['snapshot']
    require('dispatch' not in value, 'deployment_dispatch_already_requested')
    workflow = auto.api('actions/workflows/factory-deploy.yml')
    require(workflow.get('path') == '.github/workflows/factory-deploy.yml', 'untrusted_deployment_workflow')
    runs = auto.api('actions/workflows/factory-deploy.yml/runs?event=workflow_run&head_sha=' + snap['commit'] + '&per_page=100')
    require(type(runs.get('total_count')) is int and runs['total_count'] <= 100, 'deployment_run_ambiguous')
    matches = [row for row in runs['workflow_runs'] if row.get('workflow_id') == workflow['id']
               and row.get('path') == '.github/workflows/factory-deploy.yml'
               and row.get('event') == 'workflow_run' and row.get('head_branch') == 'main'
               and row.get('head_sha') == snap['commit'] and row.get('status') == 'completed'
               and row.get('repository', {}).get('full_name') == auto.REPO
               and row.get('head_repository', {}).get('full_name') == auto.REPO]
    require(len(matches) == 1 and type(matches[0].get('id')) is int, 'deployment_run_ambiguous')
    deploy_run = matches[0]['id']
    value['dispatch'] = {'phase': 'requested', 'deploy_run_id': deploy_run}
    write(auto, path, value)
    try:
        auto.command('gh', 'api', '--method', 'POST', f'repos/{auto.REPO}/actions/runs/{deploy_run}/rerun')
    except Exception:
        value['dispatch']['phase'] = 'unknown'
        write(auto, path, value)
        raise Refusal('deployment_dispatch_outcome_unknown') from None
    value['dispatch']['phase'] = 'queued'
    write(auto, path, value)
    return {**report(value), 'dispatch_phase': 'queued', 'deploy_run_id': deploy_run,
            'next_command': 'factory deploy status --json'}


def forced_deployment(auto, run_id, commit, credentials):
    """Existing forced-SSH grammar supplies credentials only for the exact approved release."""
    if not exists(Path('/etc/driftward/factory-cli.json')):
        return False
    binding = private_json(Path('/etc/driftward/factory-cli.json'))
    config = config_for(auto, binding)
    directory = records_directory(config, create=False)
    if not exists(directory / 'latest.json'):
        return False
    latest = private_json(directory / 'latest.json')
    value = read_preview(auto, directory, latest['digest'])
    if value['phase'] != 'approved':
        return False
    snap = value['snapshot']
    require(snap['run_id'] == run_id and snap['commit'] == commit, 'approved_deployment_callback_differs')
    require(value.get('dispatch', {}).get('phase') in {'requested', 'queued', 'unknown'}, 'deployment_dispatch_required')
    from types import SimpleNamespace
    installed = {'deploy/factory-auto-deploy.py': Path(auto.__file__),
                 'deploy/auto_deploy_artifact.py': Path(auto.__file__).parent / 'auto_deploy_artifact.py',
                 'deploy/factory_deploy_operator.py': Path(__file__),
                 'deploy/factory-cli.py': Path('/usr/local/bin/factory')}
    for key, name in (('profile_runtime', 'deploy/factory-profile-runtime.py'),
                      ('artifact_runtime', 'deploy/factory-artifact-runtime.py')):
        if key in binding:
            installed[name] = Path(binding[key])
    args = SimpleNamespace(action='apply', preview=value['digest'], yes=True,
                           callback_run=run_id, callback_commit=commit)
    run(auto, binding, args, installed, credentials=credentials)
    return True


def run(auto, binding, args, installed, *, credentials=None):
    os.umask(0o077)
    require(os.geteuid() == 0, 'root_required')
    with locked(auto, binding) as (config, prod, install):
        directory = records_directory(config, create=args.action != 'status')
        latest_value = None
        if exists(directory / 'latest.json'):
            latest = private_json(directory / 'latest.json')
            require(isinstance(latest, dict) and set(latest) == {'digest', 'updated_at'}, 'invalid_deployment_status')
            latest_value = read_preview(auto, directory, latest['digest'])
        if args.action == 'status':
            phase = rollout_status(auto, Path(config['work_directory']))
            if latest_value:
                result = report(latest_value)
                result.update(current_identity(auto, install))
                result['enabled'] = config['enabled']
            else:
                result = {'phase': phase or 'idle', 'installation': config['installation'], 'enabled': config['enabled'],
                          **current_identity(auto, install), 'next_command': 'factory deploy preview --run RUN_ID'}
            result['rollout_phase'] = phase
            automatic_blocked = phase not in {None, 'complete', 'blocked'}
            requires_recovery = automatic_blocked
            try:
                auto.assert_manual_idle(Path(config['work_directory']),
                                        latest_value['digest'] if latest_value and latest_value['phase'] == 'approved' else None)
            except auto.Refusal as error:
                if str(error) != 'prior_deployment_requires_operator':
                    raise
                requires_recovery = True
            if requires_recovery:
                result.update(phase=result['phase'] if not automatic_blocked and result['phase'] in {'failed', 'consumed'} else 'blocked',
                              blocked_reason='prior_deployment_requires_operator', eligible=False,
                              next_command='factory deploy status',
                              next_step='Preserve the cold backup and audit records for an explicit recovery review.')
            return result
        require(config['enabled'], 'automatic_deployment_disabled')
        require(not latest_value or latest_value['phase'] not in {'consumed', 'failed'},
                'prior_deployment_requires_operator')
        auto.assert_manual_idle(Path(config['work_directory']), args.preview if credentials is not None else None)
        if args.action == 'preview':
            snap = snapshot(auto, binding, config, prod, install, directory, args.run, installed)
            identifier = digest(snap)
            path = directory / (identifier + '.json')
            if exists(path):
                value = read_preview(auto, directory, identifier)
            else:
                value = {'format': 1, 'digest': identifier, 'phase': 'preview', 'snapshot': snap}
                write(auto, path, value)
            anchor_path = directory / 'baseline-anchor.json'
            if not exists(anchor_path):
                write(auto, anchor_path, {'format': 1, 'commit': snap['baseline_commit'],
                                          'compatibility': snap['baseline_compatibility']})
            write(auto, directory / 'latest.json', {'digest': identifier})
            return report(value)
        value = read_preview(auto, directory, args.preview)
        require(value['phase'] == ('preview' if args.action == 'approve' else 'approved'), 'approval_already_used_or_unavailable')
        previous = value['snapshot']
        snap = snapshot(auto, binding, config, prod, install, directory, previous['run_id'], installed)
        require(digest(snap) == value['digest'], 'deployment_preview_stale')
        require(not snap['blocked_paths'], 'protected_changes_not_approvable')
        path = directory / (args.preview + '.json')
        if args.action == 'approve':
            require(args.yes and 1 <= len(args.reason.strip()) <= 512
                    and all(char.isprintable() for char in args.reason), 'invalid_approval_reason')
            value.update(phase='approved', approval={'reason': args.reason, 'uid': os.geteuid(),
                                                    'approved_at': datetime.now(timezone.utc).isoformat()})
            write(auto, path, value)
            return report(value)
        require(args.yes and isinstance(value.get('approval'), dict)
                and value['approval'].get('uid') == 0, 'approval_required')
        if credentials is None:
            return queue_deployment(auto, value, path)
        require(getattr(args, 'callback_run', None) == snap['run_id']
                and getattr(args, 'callback_commit', None) == snap['commit']
                and value.get('dispatch', {}).get('phase') in {'requested', 'queued', 'unknown'},
                'approved_deployment_callback_differs')
        # Consume durably before image import, service stops, backup, or pin changes.
        value['phase'] = 'consumed'
        value['consumed_at'] = datetime.now(timezone.utc).isoformat()
        write(auto, path, value)
        try:
            with tempfile.TemporaryDirectory(prefix='operator-artifact-', dir=config['work_directory']) as temporary:
                image = auto.get_image(snap['run_id'], snap['commit'],
                    {**config, 'compatibility': snap['target_compatibility']}, Path(temporary),
                    snap['run_attempt'], credentials,
                    expected_artifact=snap['artifact'])
                run = auto.api(f'actions/runs/{snap["run_id"]}')
                auto.validate_run(run, auto.api('actions/workflows/hatchet-quality.yml'), snap['run_id'], snap['commit'])
                require(run.get('run_attempt') == snap['run_attempt'] and run.get('updated_at') == snap['run_updated_at'],
                        'deployment_ci_run_changed')
                auto.current_main(snap['commit'])
                with redirect_stdout(io.StringIO()):
                    auto.rollout(prod, install, image, snap['commit'], snap['run_id'], Path(config['work_directory']))
            value['phase'] = 'complete'
            value['result'] = {'image': image, 'commit': snap['commit']}
            value['completed_at'] = datetime.now(timezone.utc).isoformat()
            write(auto, path, value)
        except Exception:
            value['phase'] = 'failed'
            value['failed_at'] = datetime.now(timezone.utc).isoformat()
            write(auto, path, value)
            raise
        return report(value)
