#!/usr/bin/env python3
"""Canonical CLI for an explicitly bound, source-free Factory installation."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile

CONFIG = Path('/etc/driftward/factory-cli.json')
OPERATOR_COMMANDS = {'analytics', 'retry', 'lifecycle', 'attempts', 'review-recovery', 'review-repair'}
PROFILES = {
    'factory-executor': 'Factory executor; provisioned separately from execution routing.',
    'factory-independent-reviewer': 'Factory independent reviewer; provisioned separately from review routing.',
}


def checked_config(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError('CLI binding must be a root-owned regular file with mode 0600')
    value = json.loads(path.read_text())
    if set(value) - {'installation', 'installation_id', 'production_bundle', 'console_url', 'hermes_wrapper'} or not all(
            isinstance(value.get(key), str) and Path(value[key]).is_absolute()
            for key in ('installation', 'production_bundle')):
        raise ValueError('invalid CLI binding')
    if 'hermes_wrapper' in value:
        checked_hermes_wrapper(value['hermes_wrapper'])
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
    # Fixed native API calls give structured inventory without reading/printing
    # profile configs. Native create owns staging and does not change defaults.
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
    if args and args[0] in OPERATOR_COMMANDS | {'profiles'}:
        production = production_module(config)
        with production.portable.installation_lock(Path(config['installation'])):
            return dispatch_bound(config, args)
    return dispatch_bound(config, args)


def dispatch_bound(config, args):
    install = installation(config)
    command = args[0] if args else 'help'
    if command == 'profiles':
        if args not in (['profiles', 'status'], ['profiles', 'list'], ['profiles', 'create']):
            raise ValueError('usage: factory profiles status | list | create')
        wrapper = checked_hermes_wrapper(config.get('hermes_wrapper'))
        print(json.dumps(hermes_profiles(wrapper, args[1] == 'create'), sort_keys=True))
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
              'factory profiles status | list | create\n'
              'factory analytics | retry | lifecycle | attempts | review-recovery | review-repair [arguments]\n'
              'Operator command arguments and approval gates are provided by the active image.\n'
              'Mode changes and doctor are unavailable for managed installations.')
    else:
        raise ValueError('unsupported managed-installation command; run factory help')
    return 0


def main():
    try:
        if os.geteuid() != 0:
            raise ValueError('canonical Factory CLI requires root')
        return dispatch(checked_config(CONFIG), sys.argv[1:])
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        # Validation and private subprocess exceptions can contain credentials.
        print('factory: command refused; check the root-owned CLI binding, installation, and active deployment. '
              'Managed mode changes and doctor are unsupported; run factory help.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
