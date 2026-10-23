#!/usr/bin/env python3
"""Canonical CLI for an explicitly bound, source-free Factory installation."""
import importlib.util
from importlib.machinery import SourceFileLoader
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sqlite3
import subprocess
import sys
import tempfile

CONFIG = Path('/etc/driftward/factory-cli.json')
OPERATOR_COMMANDS = {'analytics', 'retry', 'lifecycle', 'attempts', 'review-recovery', 'review-repair'}
PROFILES = {
    'factory-executor': 'Factory executor; provisioned separately from execution routing.',
    'factory-independent-reviewer': 'Factory independent reviewer; provisioned separately from review routing.',
}


DEPLOY_HELPER = Path('/usr/local/libexec/factory-auto-deploy')
DEPLOY_POLICY = Path('/usr/local/libexec/auto_deploy_artifact.py')
DEPLOY_OPERATOR = Path('/usr/local/libexec/factory_deploy_operator.py')


class DeployRefusal(ValueError):
    pass


class DeployArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, 'factory deploy: invalid arguments; use the command --help for required flags.\n')


def deploy_parser(args):
    parser = DeployArgumentParser(prog='factory deploy', description='Review and apply one exact verified release. No migrations or automatic recovery.')
    commands = parser.add_subparsers(dest='action', required=True)
    baseline = commands.add_parser('baseline', help='Review the exact deployed protected-source baseline.')
    actions = baseline.add_subparsers(dest='baseline_action', required=True)
    preview = actions.add_parser('preview')
    preview.add_argument('--run', required=True, type=int)
    preview.add_argument('--json', action='store_true')
    apply = actions.add_parser('apply')
    apply.add_argument('--preview', required=True)
    apply.add_argument('--reason', required=True)
    apply.add_argument('--yes', required=True, action='store_true')
    apply.add_argument('--json', action='store_true')
    for name, description in (
            ('status', 'Show the bound installation and audited deployment phase.'),
            ('preview', 'Save a release snapshot without changing runtime configuration or services.'),
            ('approve', 'Record root approval for one unchanged, eligible preview.'),
            ('apply', 'Consume approval once, cold-back up, deploy, and verify health.')):
        command = commands.add_parser(name, help=description, description=description)
        command.add_argument('--json', action='store_true', help='Emit a machine-readable result.')
        if name == 'preview':
            command.add_argument('--run', required=True, type=int, help='Successful current-main Hatchet Quality run ID.')
            command.epilog = 'Example: factory deploy preview --run 123456 --json'
        elif name in {'approve', 'apply'}:
            command.add_argument('--preview', required=True, help='Exact SHA-256 printed by preview.')
            command.add_argument('--yes', required=True, action='store_true', help='Explicitly confirm this operation.')
            if name == 'approve':
                command.add_argument('--reason', required=True, help='Why the exact listed changes were reviewed (1–512 printable characters).')
            command.epilog = 'Example: factory deploy ' + name + ' --preview DIGEST ' + ('--reason "Reviewed CLI release" ' if name == 'approve' else '') + '--yes'
    parsed = parser.parse_args(args)
    if (parsed.action == 'preview' or parsed.action == 'baseline' and parsed.baseline_action == 'preview') and not 0 < parsed.run < 10**20:
        parser.error('--run must be a positive run ID')
    return parsed


def trusted_file(path):
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise DeployRefusal('unsafe_installed_deployment_helper')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_nlink != 1 or info.st_mode & 0o022:
        raise DeployRefusal('unsafe_installed_deployment_helper')


def installed_module(name, path):
    trusted_file(path)
    spec = importlib.util.spec_from_loader(name, SourceFileLoader(name, str(path)))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def execution_parser(args):
    parser = DeployArgumentParser(prog='factory execution')
    parser.add_argument('action', choices=('status', 'preview', 'resume', 'configure'))
    parser.add_argument('configure_action', nargs='?', choices=('preview', 'apply'))
    parser.add_argument('--snapshot-digest')
    parser.add_argument('--reason')
    parser.add_argument('--yes', action='store_true')
    parser.add_argument('--json', action='store_true')
    parsed = parser.parse_args(args)
    if (parsed.action == 'configure') != (parsed.configure_action is not None):
        parser.error('configure requires preview or apply')
    return parsed


def profile_readiness(binding, install):
    helper = checked_hermes_wrapper(binding.get('profile_runtime'))
    wrapper = checked_hermes_wrapper(binding.get('hermes_wrapper'))
    backend = installed_module('factory_execution_profiles', helper)
    result = backend.dispatch(install, binding, wrapper, ['status'])
    result['native_runtime_idle'] = backend.run_native(wrapper, 'idle', {}).get('idle') is True
    return result


def deploy_dispatch(binding, args, *, execution=False):
    parsed = execution_parser(args) if execution else deploy_parser(args)
    safe_refusals = (DeployRefusal,)
    try:
        installed = {'deploy/factory-auto-deploy.py': DEPLOY_HELPER,
                     'deploy/auto_deploy_artifact.py': DEPLOY_POLICY,
                     'deploy/factory_deploy_operator.py': DEPLOY_OPERATOR,
                     'deploy/factory-cli.py': Path(__file__).absolute()}
        for key, name in (('profile_runtime', 'deploy/factory-profile-runtime.py'),
                          ('artifact_runtime', 'deploy/factory-artifact-runtime.py')):
            if key in binding:
                installed[name] = checked_hermes_wrapper(binding[key])
        for path in installed.values():
            try:
                trusted_file(path)
            except FileNotFoundError:
                raise DeployRefusal('installed_deployment_helper_update_required') from None
        bundle = Path(binding['production_bundle'])
        for path in bundle.iterdir():
            if path.suffix in {'.py', '.yml'}:
                trusted_file(path)
        installed_module('auto_deploy_artifact', DEPLOY_POLICY)
        auto = installed_module('factory_cli_auto_deploy', DEPLOY_HELPER)
        manual = installed_module('factory_cli_deploy_operator', DEPLOY_OPERATOR)
        if getattr(manual, 'API_VERSION', None) != 1 or not all(hasattr(auto, name) for name in ('source_entries', 'deployment_artifact')):
            raise DeployRefusal('installed_deployment_helper_update_required')
        safe_refusals = (DeployRefusal, auto.Refusal, manual.Refusal)
        result = (manual.execution_run(auto, binding, parsed, installed, profile_readiness)
                  if execution else manual.baseline_run(auto, binding, parsed, installed)
                  if parsed.action == 'baseline' else manual.run(auto, binding, parsed, installed))
    except Exception as error:
        reason = str(error)
        if isinstance(error, safe_refusals) and re.fullmatch(r'[a-z][a-z0-9_]{0,95}', reason):
            raise DeployRefusal(reason) from None
        if isinstance(error, ValueError) and reason == 'another operation is using this installation':
            raise DeployRefusal('deployment_operation_in_progress') from None
        raise DeployRefusal('deployment_evidence_unavailable') from None
    if parsed.json:
        print(json.dumps(result, sort_keys=True, allow_nan=False))
    else:
        for label, key in (('Installation', 'installation'), ('Phase', 'phase'), ('Deployment enabled', 'enabled'),
                           ('Rollout phase', 'rollout_phase'),
                           ('Configured compatibility baseline', 'configured_compatibility'),
                           ('Blocked reason', 'blocked_reason'), ('Current image', 'current_image'),
                           ('Target commit', 'target_commit'), ('CI run', 'run_id'), ('Preview', 'preview'),
                           ('Artifact digest', 'artifact_digest'),
                           ('Restart impact', 'restart_impact')):
            if key in result:
                print(f'{label}: {result[key]}')
        for change in result.get('protected_changes', []):
            print('Protected change: ' + change['path'])
            if result.get('baseline_adoption'):
                print('  Before: ' + json.dumps(change['before'], sort_keys=True))
                print('  After: ' + json.dumps(change['after'], sort_keys=True))
        if result.get('blocked_paths'):
            print('Blocked: protected changes require a separate operation review.')
        if 'baseline_adoption' in result:
            if result['baseline_adoption']:
                print('Compatibility baseline: ' + ('adopted' if result['phase'] == 'complete' else 'preview only'))
                print('Protected fingerprint: ' + result.get('target_compatibility', result.get('compatibility', '')))
                print('Deployed commit: ' + result.get('current_commit', result.get('commit', '')))
            else:
                print('Compatibility baseline: unchanged; this approves one release only.')
        if result.get('next_step'):
            print(result['next_step'])
        print('Next: ' + result['next_command'])
    return 2 if result.get('blocked_paths') else 0

def checked_config(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError('CLI binding must be a root-owned regular file with mode 0600')
    value = json.loads(path.read_text())
    if set(value) - {'installation', 'installation_id', 'production_bundle', 'console_url', 'hermes_wrapper', 'profile_runtime', 'artifact_runtime'} or not all(
            isinstance(value.get(key), str) and Path(value[key]).is_absolute()
            for key in ('installation', 'production_bundle')):
        raise ValueError('invalid CLI binding')
    if 'hermes_wrapper' in value:
        checked_hermes_wrapper(value['hermes_wrapper'])
    if 'profile_runtime' in value:
        checked_hermes_wrapper(value['profile_runtime'])
    if 'artifact_runtime' in value:
        checked_hermes_wrapper(value['artifact_runtime'])
    return value


def checked_hermes_wrapper(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError('Hermes wrapper requires an absolute path')
    path = Path(value)
    # Validate parents too: a protected executable in a writable directory can
    # be replaced without writing the executable itself.
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if (info.st_uid != 0 or info.st_mode & 0o022
                or not (stat.S_ISDIR(info.st_mode) if part != path else stat.S_ISREG(info.st_mode))):
            raise ValueError('Hermes wrapper path must be root-owned and protected')
    if not os.access(path, os.X_OK):
        raise ValueError('Hermes wrapper must be executable')
    return path


def hermes_profiles(wrapper, create):
    # The wrapper owns the runtime location. No ambient root/profile, Python,
    # Docker, or Compose settings may retarget this operation.
    environment = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                   'HOME': '/root', 'LANG': 'C.UTF-8', 'NO_COLOR': '1', 'TERM': 'dumb'}
    # Fixed native API calls return inventory only. Config is inspected privately
    # because native fresh-profile creation can seed inline provider credentials.
    script = '''import contextlib, io, json, os, re, sys
sys.path.insert(0, '/opt/hermes')
from hermes_cli.profiles import list_profile_names, get_active_profile_name, create_profile, _get_profiles_root
profiles = json.loads(PROFILE_DEFINITIONS)
try:
    with os.scandir(_get_profiles_root()) as entries:
        list(entries)
except FileNotFoundError:
    pass
before = set(list_profile_names())
active = get_active_profile_name()
if not isinstance(active, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", active):
    raise RuntimeError("invalid active profile")
if CREATE_PROFILES and set(profiles) - before:
    from hermes_cli.profiles import launch_model_seed
    from hermes_cli.config import read_user_config_raw
    from hermes_constants import get_hermes_home
    def reject_credentials(value):
        if isinstance(value, dict):
            for key, item in value.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if normalized.endswith(("apikey", "token", "password", "secret", "authorization")) and item:
                    raise RuntimeError("native profile model seed contains inline credentials")
                reject_credentials(item)
        elif isinstance(value, list):
            for item in value:
                reject_credentials(item)
    source = get_hermes_home() / 'config.yaml'
    reject_credentials(launch_model_seed(read_user_config_raw(source)) if source.is_file() else {})
results = []
for name, description in profiles.items():
    existed = name in before
    if CREATE_PROFILES and not existed:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            create_profile(name, no_alias=True, no_skills=True, description=description)
    results.append({"name": name, "existed": existed, "created": CREATE_PROFILES and not existed})
after = set(list_profile_names())
if get_active_profile_name() != active or (CREATE_PROFILES and not set(profiles).issubset(after)):
    raise RuntimeError("profile creation verification failed")
for result in results:
    result["present"] = result["name"] in after
print(json.dumps({"profiles": results, "active_profile": active,
                  "routing": "unchanged", "credentials": "not_provisioned_by_factory"}))
'''.replace('PROFILE_DEFINITIONS', repr(json.dumps(PROFILES))).replace('CREATE_PROFILES', repr(create))
    result = subprocess.run([str(wrapper), 'shell', '-c', 'exec python3 -'], input=script, env=environment,
                            capture_output=True, text=True, timeout=60, check=True)
    value = json.loads(result.stdout)
    if (not isinstance(value, dict) or set(value) != {'profiles', 'active_profile', 'routing', 'credentials'}
            or value['routing'] != 'unchanged' or value['credentials'] != 'not_provisioned_by_factory'
            or not isinstance(value['active_profile'], str)
            or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value['active_profile'])
            or not isinstance(value['profiles'], list) or len(value['profiles']) != len(PROFILES)):
        raise ValueError('invalid native profile inventory')
    for row, name in zip(value['profiles'], PROFILES):
        if (not isinstance(row, dict) or set(row) != {'name', 'existed', 'created', 'present'}
                or row['name'] != name or any(type(row[key]) is not bool for key in ('existed', 'created', 'present'))
                or row['created'] != (create and not row['existed'])
                or row['present'] != (create or row['existed'])):
            raise ValueError('invalid native profile inventory')
    return value


def production_module(config):
    bundle = Path(config['production_bundle'])
    sys.path.insert(0, str(bundle))
    spec = importlib.util.spec_from_file_location('factory_cli_production', bundle / 'factory-production.py')
    production = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(production)
    return production


def installation(config):
    production = production_module(config)
    result = production.Production(Path(config['installation']))
    if result.metadata['installation_id'] != config.get('installation_id'):
        raise ValueError('bound installation identity changed')
    result.assert_owned_resources()
    result.assert_candidate_writers()
    return result


def operator(install):
    ids = install.compose('ps', '-q', 'factory-operator', private=True).splitlines()
    if len(ids) != 1:
        raise ValueError('active operator container is unavailable')
    result = subprocess.run(['docker', 'inspect', ids[0]], capture_output=True, text=True, check=True)
    row, = json.loads(result.stdout)
    labels = row['Config']['Labels']
    if (labels.get('com.docker.compose.project') != install.project
            or labels.get('com.docker.compose.service') != 'factory-operator'
            or labels.get('io.driftward.factory.installation') != install.metadata['installation_id']
            or row['Image'] != install.values['FACTORY_IMAGE'] or not row['State']['Running']):
        raise ValueError('operator identity differs from the bound installation')
    mounts = [item for item in row['Mounts'] if item['Destination'] == '/run/driftward-factory-operator'
              and item.get('Name') == install.project + '-operator-socket' and item['Type'] == 'volume']
    if len(mounts) != 1:
        raise ValueError('operator socket volume differs')
    return ids[0], Path(mounts[0]['Source']) / 'operator.sock', row


def cli_image(install):
    image = install.images['factory']
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        raise ValueError('CLI image must be immutable')
    result = subprocess.run(['docker', 'image', 'inspect', image], capture_output=True, text=True, check=True)
    row, = json.loads(result.stdout)
    labels = row.get('Config', {}).get('Labels') or {}
    if (row['Id'] != image or labels.get('org.opencontainers.image.source') != 'https://github.com/Driftward-LLC/factory'
            or not re.fullmatch(r'[0-9a-f]{40}', labels.get('org.opencontainers.image.revision', ''))):
        raise ValueError('CLI image release identity is unavailable')
    return image


def dispatch(config, args):
    if args and args[0] == 'execution':
        return deploy_dispatch(config, args[1:], execution=True)
    if args and args[0] == 'deploy':
        return deploy_dispatch(config, args[1:])
    if args and args[0] in OPERATOR_COMMANDS | {'profiles', 'artifacts'}:
        production = production_module(config)
        with production.portable.installation_lock(Path(config['installation'])):
            return dispatch_bound(config, args)
    return dispatch_bound(config, args)


def dispatch_bound(config, args):
    install = installation(config)
    command = args[0] if args else 'help'
    if command == 'artifacts':
        helper = checked_hermes_wrapper(config.get('artifact_runtime'))
        spec = importlib.util.spec_from_file_location('factory_artifact_runtime', helper)
        backend = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(backend)
        try:
            result = backend.dispatch(args[1:])
        except backend.Refusal as error:
            print('factory artifacts: ' + str(error), file=sys.stderr)
            return 1
        print(json.dumps(result, sort_keys=True))
    elif command == 'profiles':
        if not (args[1:] in (['status'], ['list'], ['create'], ['activate'], ['canary', 'executor'], ['canary', 'reviewer'], ['canary', 'github'])
                or (len(args) >= 3 and args[1] == 'upgrade-observer')
                or (len(args) == 4 and args[1:3] == ['configure', '--plugin-source'])
                or (len(args) >= 4 and args[1] == 'recover-inspection')):
            raise ValueError('usage: factory profiles status | list | create | configure --plugin-source REPO | activate | canary executor|reviewer|github | upgrade-observer preview|apply [approval arguments] | recover-inspection JOB preview|status|apply [approval arguments]')
        wrapper = checked_hermes_wrapper(config.get('hermes_wrapper'))
        if args in (['profiles', 'create'], ['profiles', 'list']) or (args == ['profiles', 'status'] and 'profile_runtime' not in config):
            result = hermes_profiles(wrapper, args[1] == 'create')
        else:
            helper = checked_hermes_wrapper(config.get('profile_runtime'))
            spec = importlib.util.spec_from_file_location('factory_profile_runtime', helper)
            backend = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(backend)
            result = backend.dispatch(install, config, wrapper, args[1:])
        print(json.dumps(result, sort_keys=True))
    elif command == 'status' and len(args) == 1:
        install.status()
    elif command == 'logs':
        # Compose validates service names; options remain a deliberately small CLI surface.
        services = {'ingress': 'event-ingress', 'intake': 'intake-worker', 'executor': 'bridge-executor-worker',
                    'bridge': 'bridge', 'pr': 'pr-event-worker', 'reconciliation': 'reconciliation-worker',
                    'console': 'factory-operator'}
        services = {name: [service] for name, service in services.items()}
        services['lifecycle'] = ['factory-lifecycle-writer', 'factory-linear-token-refresh']
        services['linear'] = ['event-ingress', 'intake-worker', 'bridge-executor-worker', 'factory-lifecycle-writer']
        target = args[1] if len(args) > 1 and not args[1].startswith('-') else 'all'
        options = args[2:] if target != 'all' or (len(args) > 1 and args[1] == 'all') else args[1:]
        forwarded = []
        while options:
            flag, *options = options
            if flag in ('-f', '--follow'):
                forwarded.append('--follow')
            elif flag in ('-n', '--lines') and options and options[0].isdigit():
                forwarded.extend(('--tail', options.pop(0)))
            else:
                raise ValueError('usage: factory logs [target] [-f] [-n N]')
        if target != 'all' and target not in services:
            raise ValueError('unknown log target')
        install.compose('logs', *forwarded, *(services[target] if target != 'all' else []))
    elif command == 'mode' and args in (['mode'], ['mode', 'status']):
        # The installation owns mode; never read legacy /etc worker configuration.
        import bootstrap
        value = bootstrap.read_env(install.directory / 'env/hatchet-workers.env').get('HATCHET_INTAKE_DRY_RUN')
        if value not in ('true', 'false'):
            raise ValueError('installation intake mode is invalid')
        print('dry-run' if value == 'true' else 'real')
    elif command == 'console' and len(args) == 1:
        url = config.get('console_url', '')
        if not isinstance(url, str) or not url.startswith(('https://', 'http://')) or any(c.isspace() for c in url):
            raise ValueError('set console_url in the reviewed CLI binding')
        print(url)
    elif command == 'version' and len(args) == 1:
        _, _, row = operator(install)
        print(json.dumps({'installation': str(install.directory), 'project': install.project,
                          'image': row['Image'], 'revision': row['Config']['Labels'].get('org.opencontainers.image.revision')}))
    elif command in OPERATOR_COMMANDS:
        for dependency in ('bash', 'curl', 'jq'):
            if not shutil.which(dependency):
                raise ValueError('host dependency unavailable: ' + dependency)
        _, socket, _ = operator(install)
        image = cli_image(install)
        if not socket.is_socket():
            raise ValueError('bound operator socket is unavailable')
        import bootstrap
        token = bootstrap.read_env(install.directory / 'env/factory-operator.env')['HATCHET_FACTORY_OPERATOR_TOKEN']
        if not token or any(c in token for c in '=\r\n\x00'):
            raise ValueError('invalid operator credential')
        # Read the CLI from the immutable active image on every call, so image-only
        # rollouts cannot leave the canonical command behind.
        with tempfile.TemporaryDirectory(prefix='factory-cli-') as temporary:
            cli = Path(temporary) / 'factory'
            with cli.open('wb') as output:
                subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--read-only',
                                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                                '--entrypoint', 'cat', image, '/app/factory/factory'], stdout=output,
                               stderr=subprocess.DEVNULL, check=True)
            credentials = Path(temporary) / 'operator.env'
            credentials.write_text('HATCHET_FACTORY_OPERATOR_TOKEN=' + token + '\n')
            credentials.chmod(0o600)
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(('FACTORY_', 'DRIFTWARD_', 'HATCHET_', 'COMPOSE_'))}
            env.update(FACTORY_OPERATOR_ENV=str(credentials), FACTORY_OPERATOR_SOCKET=str(socket))
            return subprocess.run(['bash', str(cli), *args], env=env).returncode
    elif command in ('help', '-h', '--help'):
        print('factory status | console | version | logs [target] [-f] [-n N] | mode\n'
              'factory artifacts status | preview|preserve|prune --before ISO [--snapshot-digest DIGEST --yes]\n'
              'factory profiles status | list | create\n'
              'factory profiles configure --plugin-source REVIEWED_GIT_REPO | activate | canary executor|reviewer\n'
              'factory profiles upgrade-observer preview|apply|status [--snapshot-digest DIGEST --reason REASON --request-id UUID --yes]\n'
              'factory profiles canary github\n'
              'factory profiles recover-inspection JOB preview|status|apply [--snapshot-digest DIGEST --reason REASON --request-id UUID --yes]\n'
              'factory analytics | retry | lifecycle | attempts | review-recovery | review-repair [arguments]\n'
              'Operator command arguments and approval gates are provided by the active image.\n'
              'Mode changes and doctor are unavailable for managed installations.')
    else:
        raise ValueError('unsupported managed-installation command; run factory help')
    return 0


def main():
    try:
        if len(sys.argv) > 1 and sys.argv[1] == 'deploy':
            deploy_parser(sys.argv[2:])
        if len(sys.argv) > 1 and sys.argv[1] == 'execution':
            execution_parser(sys.argv[2:])
        if sys.argv[1:] in ([], ['help'], ['-h'], ['--help']):
            print('factory status | profiles | artifacts | deploy status|preview|approve|apply (factory deploy --help)\n'
                  'factory execution status|preview|resume (factory execution --help)')
            return 0
        if os.geteuid() != 0:
            raise ValueError('canonical Factory CLI requires root')
        return dispatch(checked_config(CONFIG), sys.argv[1:])
    except DeployRefusal as error:
        print('factory deploy: ' + str(error) + '. Next: factory deploy status; use factory deploy --help.', file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, RuntimeError, sqlite3.Error, subprocess.SubprocessError):
        # Validation and private subprocess exceptions can contain credentials.
        print('factory: command refused; check the root-owned CLI binding, installation, and active deployment. '
              'Managed mode changes and doctor are unsupported; run factory help.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
