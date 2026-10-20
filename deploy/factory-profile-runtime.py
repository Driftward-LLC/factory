#!/usr/bin/env python3
"""Fixed Factory role configuration, invoked only by the bound canonical CLI."""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json
import os
import re
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tarfile
import tempfile
import uuid
import sqlite3

ROOT = Path('/opt/data')
COMMIT = 'db14524356cfebd132833ed06a7c8a097ef5c11c'
ROLES = {'executor': 'factory-executor', 'reviewer': 'factory-independent-reviewer'}
LIMIT = 8 * 1024 * 1024
CAPABILITIES = ('mcp_servers', 'browser', 'terminal', 'tools', 'toolsets', 'enabled_toolsets', 'disabled_toolsets')
PROVISION = Path('/etc/driftward/factory-reviewer.json')
DROPIN = Path('/etc/systemd/system/driftward-hermes-factory-gateway.service.d/factory-profile.conf')
SERVICE = 'driftward-hermes-factory-gateway.service'
PROFILE_DROPIN = (b'[Service]\nEnvironment=DRIFTWARD_HERMES_PROFILE=factory-executor\n'
                  b'RuntimeDirectory=driftward-hermes-factory\nRuntimeDirectoryPreserve=yes\n')
CUTOVER_STAGES = {'not_started', 'unknown', 'quiescence', 'workers_stop', 'gateway_stop', 'native_idle',
                  'public_backup', 'public_publish', 'auth_move', 'routing_publish', 'daemon_reload',
                  'gateway_restart', 'gateway_health', 'workers_restore', 'complete', 'workers_restop',
                  'routing_restore', 'auth_restore', 'public_restore'}
TERMINAL = ('completed', 'failed', 'verified_ready', 'canceled', 'canceled_after_effect', 'superseded', 'legacy_completed')
REVIEW_SKILL_DIGESTS = {
    'engineering/review-code.md': ('review_skill_digest', '913ab9cd76bfa6403357dd5f8f423a663986eb1b6a9663f425a7ac1a873c660b'),
    'engineering/verify-app.md': ('verify_skill_digest', '43455f2cb5b5d810c58569e3a02be8e140291a44b3198f4bd1291693ac2dfd84'),
}
PUBLIC_ASSETS = {
    'gateway/hermes-gateway-server.js': Path('/usr/local/share/driftward-factory/hermes-gateway/hermes-gateway-server.js'),
    **{'independent-reviewer/' + name: Path('/usr/local/share/driftward-factory/independent-reviewer/deploy/independent-reviewer') / name
       for name in ('native_agent.py', 'runner.py', 'review_observer.py')},
    **{name: Path('/etc/driftward/factory-reviewer') / Path(name).name for name in REVIEW_SKILL_DIGESTS},
}


def no_symlinks(path):
    for part in (*reversed(path.parents), path):
        if part.is_symlink():
            raise ValueError('managed profile paths cannot contain symlinks')


def checked(path: Path, *, directory=False, owner=None):
    no_symlinks(path)
    info = path.stat()
    if info.st_uid != (os.geteuid() if owner is None else owner) or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError('managed profile ownership or file type differs')
    return path


def runtime_identity():
    no_symlinks(ROOT)
    info = ROOT.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o022:
        raise ValueError('unsafe runtime identity directory')
    return info.st_uid, info.st_gid


def normalize_factory_configs():
    """Make only the Factory reviewer auth parent private; never change runtime secrets."""
    uid, gid = runtime_identity()
    for role, name in ROLES.items():
        profile = ROOT / 'profiles' / name
        no_symlinks(profile)
        info = profile.stat()
        target_uid, target_gid = (os.geteuid(), os.getegid()) if role == 'reviewer' else (uid, gid)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {uid, target_uid} or info.st_mode & 0o022:
            raise ValueError('unmanaged Factory profile directory')
        path = profile / 'config.yaml'
        no_symlinks(path)
        config = path.stat()
        if not stat.S_ISREG(config.st_mode) or config.st_uid != os.geteuid() or config.st_mode & 0o022:
            raise ValueError('unmanaged Factory profile config')
        # Authentication is never copied, chmodded or chowned by configuration.
        if info.st_uid != target_uid:
            if role != 'reviewer' or (profile / 'auth.json').exists() or (profile / 'auth.json').is_symlink():
                raise ValueError('profile identity migration is not safe')
            os.chown(profile, target_uid, target_gid)
        if role == 'reviewer' and stat.S_IMODE(info.st_mode) != 0o700:
            profile.chmod(0o700)


def archive_files(archive: bytes):
    if len(archive) > LIMIT:
        raise ValueError('plugin archive exceeds limit')
    files = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as stream:
        for member in stream:
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or not path.parts or path.as_posix() != member.name.rstrip('/'):
                raise ValueError('unsafe plugin archive')
            if member.isdir():
                continue
            if not member.isfile() or member.name in files:
                raise ValueError('plugin archive must contain unique regular files')
            files[member.name] = stream.extractfile(member).read()
    for name in ('plugin.json', 'plugin.yaml', '.codex-plugin/plugin.json'):
        manifest = json.loads(files[name])
        if manifest.get('name') != 'driftward-engineering' or manifest.get('version') != '0.1.0-rc.6':
            raise ValueError('plugin release identity differs')
    return files


def tree_digest(files):
    return hashlib.sha256(json.dumps({name: hashlib.sha256(data).hexdigest()
                                     for name, data in sorted(files.items())},
                                    sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def immutable_release(parent, name, files):
    no_symlinks(parent)
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    checked(parent, directory=True)
    destination = parent / name
    if destination.exists():
        checked(destination, directory=True)
        existing = {}
        for path in destination.rglob('*'):
            checked(path, directory=path.is_dir())
            if path.is_file():
                existing[path.relative_to(destination).as_posix()] = path.read_bytes()
        if existing != files:
            raise ValueError('immutable plugin release differs')
        return destination
    with tempfile.TemporaryDirectory(prefix='.factory-stage-', dir=parent) as scratch:
        candidate = Path(scratch) / name
        candidate.mkdir(mode=0o700)
        for relative, content in files.items():
            path = candidate / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(0o444)
        candidate.rename(destination)
    return destination


def link_release(path, release):
    checked(path.parent, directory=True)
    if path.is_symlink():
        if path.resolve() != release:
            raise ValueError('profile plugin already selects another release')
    elif path.exists():
        raise ValueError('unmanaged profile plugin already exists')
    else:
        path.symlink_to(release, target_is_directory=True)


def default_capabilities(validate=True):
    from hermes_cli.config import read_user_config_raw
    config = read_user_config_raw(checked(ROOT / 'config.yaml', owner=runtime_identity()[0]))
    selected = {name: copy.deepcopy(config[name]) for name in CAPABILITIES if name in config}
    harmless_literals = {
        'nodeenv': {'production', 'development', 'test'},
        'loglevel': {'trace', 'debug', 'info', 'warn', 'warning', 'error', 'critical'},
    }
    mime_types = {'application/json', 'text/event-stream', 'text/plain', 'application/octet-stream', '*/*'}
    def harmless(name, item, context):
        if not isinstance(item, str):
            return False
        if context in {'env', 'environment'}:
            return item in harmless_literals.get(name, set())
        return name in {'accept', 'contenttype'} and all(part.strip() in mime_types for part in item.split(','))
    def reject_inline(value, context=''):
        if isinstance(value, dict):
            for name, item in value.items():
                normalized = re.sub(r'[^a-z0-9]', '', str(name).lower())
                sensitive = normalized.endswith(('apikey', 'token', 'password', 'secret', 'authorization',
                                                  'privatekey', 'accesskey', 'accesskeyid', 'credential', 'credentials'))
                transport = context in {'env', 'environment', 'headers', 'httpheaders'}
                if item and (sensitive or (transport and not harmless(normalized, item, context))):
                    if not isinstance(item, str) or not re.fullmatch(r'(?:Bearer )?\$\{[A-Za-z_][A-Za-z0-9_]*\}', item):
                        raise ValueError('executor capability settings contain inline credentials')
                    variable = item[item.index('${') + 2:-1]
                    if not os.environ.get(variable):
                        raise ValueError('executor capability credential environment reference is unavailable')
                reject_inline(item, normalized)
        elif isinstance(value, list):
            for item in value:
                reject_inline(item, context)
    if validate:
        reject_inline(selected)
    return selected


def configure(payload):
    from hermes_cli.config import read_user_config_raw, atomic_config_write
    capabilities = default_capabilities()
    files = archive_files(base64.b64decode(payload['engineering'], validate=True))
    observer = {name: base64.b64decode(data, validate=True) for name, data in payload['observer'].items()}
    if set(observer) != {'__init__.py', 'plugin.yaml'}:
        raise ValueError('observer package differs')
    normalize_factory_configs()
    profiles = {role: checked(ROOT / 'profiles' / name, directory=True,
                              owner=runtime_identity()[0] if role == 'executor' else os.geteuid())
                for role, name in ROLES.items()}
    executor = profiles['executor']
    releases = executor / 'factory-managed' / 'releases'
    engineering = immutable_release(releases / 'driftward-engineering', COMMIT, files)
    observability = immutable_release(releases / 'factory-posthog', tree_digest(observer), observer)
    no_symlinks(executor / 'plugins' / 'observability')
    (executor / 'plugins' / 'observability').mkdir(parents=True, exist_ok=True)
    link_release(executor / 'plugins' / 'driftward-engineering', engineering)
    link_release(executor / 'plugins' / 'observability' / 'posthog', observability)
    for role, profile in profiles.items():
        path = checked(profile / 'config.yaml')
        config = read_user_config_raw(path)
        if role == 'executor':
            config.update(copy.deepcopy(capabilities))
        if not isinstance(config.get('model'), dict):
            config['model'] = {}
        config['model'].update({'default': 'gpt-6.1-sol' if role == 'executor' else 'gpt-6-astra', 'provider': 'openai-codex'})
        plugins = config.setdefault('plugins', {})
        desired = ['driftward-engineering', 'observability/posthog'] if role == 'executor' else []
        if set(desired).intersection(plugins.get('disabled', [])):
            raise ValueError('required executor plugin is explicitly disabled')
        plugins['enabled'] = desired
        if role == 'reviewer':
            config.setdefault('auth', {})['adopt_external_logins'] = False
            for name in ('tool_search', 'connectors'):
                config.setdefault('tools', {}).setdefault(name, {})['enabled'] = 'off' if name == 'tool_search' else False
        atomic_config_write(path, config)
    return {'configured': True, 'plugin_commit': COMMIT, 'observer_digest': tree_digest(observer),
            'credentials_copied': False, 'routing_changed': False,
            'executor_capability_keys': sorted(capabilities)}


def inventory():
    from hermes_cli.config import read_user_config_raw
    result = {}
    capabilities = default_capabilities(False)
    try:
        default_capabilities()
        ready = True
    except ValueError:
        ready = False
    for role, name in ROLES.items():
        profile = ROOT / 'profiles' / name
        owner = runtime_identity()[0] if role == 'executor' else os.geteuid()
        # Before configure, the native-created reviewer directory has runtime ownership.
        if role == 'reviewer' and profile.stat().st_uid == runtime_identity()[0] and not (profile / 'auth.json').exists():
            owner = runtime_identity()[0]
        checked(profile, directory=True, owner=owner)
        config = read_user_config_raw(checked(profile / 'config.yaml'))
        enabled = config.get('plugins', {}).get('enabled', [])
        result[role] = {'profile': name, 'engineering_enabled': 'driftward-engineering' in enabled,
                        'posthog_plugin_enabled': 'observability/posthog' in enabled,
                        'auth_present': (profile / 'auth.json').is_file()}
        if role == 'executor':
            result[role]['capabilities_match_default'] = all(config.get(key) == value for key, value in capabilities.items())
            result[role]['capabilities_ready'] = ready
            names = list(capabilities.get('mcp_servers', {}))
            if not all(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', name) for name in names):
                raise ValueError('invalid capability server name')
            result[role]['mcp_servers'] = names
            result[role]['capability_keys'] = sorted(capabilities)
    return {'profiles': result, 'posthog_runtime_configured': bool(os.environ.get('POSTHOG_API_KEY'))}


def idle_runtime():
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            arguments = (entry / 'cmdline').read_bytes().split(b'\0')
            environment = (entry / 'environ').read_bytes().split(b'\0')
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            # Other runtime identities are outside this profile's execution role.
            continue
        if (any(arg.startswith(b'factory-reviewer:') for arg in arguments)
                or any(item in {b'HERMES_HOME=/opt/data/factory-reviewer-auth',
                                b'HERMES_HOME=/opt/data/profiles/factory-independent-reviewer'} for item in environment)
                or any(item.startswith((b'FACTORY_OBSERVABILITY_JOB_ID=', b'FACTORY_OBSERVABILITY_ATTEMPT_ID='))
                       and item.split(b'=', 1)[1] for item in environment)):
            raise ValueError('Factory runtime invocation is active')


def move_auth(reverse=False):
    idle_runtime()
    legacy = ROOT / 'factory-reviewer-auth' / 'auth.json'
    named = ROOT / 'profiles' / ROLES['reviewer'] / 'auth.json'
    source, destination = (named, legacy) if reverse else (legacy, named)
    checked(source)
    checked(destination.parent, directory=True)
    if source.stat().st_mode & 0o077 or destination.exists() or destination.is_symlink():
        raise ValueError('reviewer sign-in move requires private source and absent destination')
    os.rename(source, destination)
    return {'reviewer_auth_moved': not reverse}


def canary(payload):
    role = payload['role']
    if role not in ROLES:
        raise ValueError('unsupported canary role')
    profile = checked(ROOT / 'profiles' / ROLES[role], directory=True,
                      owner=runtime_identity()[0] if role == 'executor' else os.geteuid())
    telemetry = {key: os.environ[key] for key in ('POSTHOG_API_KEY', 'POSTHOG_HOST') if key in os.environ}
    if not telemetry.get('POSTHOG_API_KEY'):
        raise ValueError('PostHog runtime configuration unavailable')
    identifier = str(uuid.UUID(payload['id']))
    session = 'factory-profile-canary:' + identifier
    observer = None
    scratch = None
    if role == 'reviewer':
        no_symlinks(profile / 'profiles')
        auth = checked(profile / 'auth.json')
        if profile.stat().st_mode & 0o077 or auth.stat().st_mode & 0o077:
            raise ValueError('reviewer authentication state must be private')
        os.environ['HERMES_HOME'] = str(profile)
    from hermes_constants import set_hermes_home_override
    if role == 'reviewer':
        (profile / 'profiles').mkdir(mode=0o700, exist_ok=True)
        scratch = tempfile.TemporaryDirectory(prefix='factory-canary-', dir=profile / 'profiles')
        isolated = Path(scratch.name)
        os.environ['HERMES_HOME'] = str(isolated)
        (isolated / 'config.yaml').write_text(json.dumps({'model': {'default': 'gpt-6-astra', 'provider': 'openai-codex'},
            'auth': {'adopt_external_logins': False}, 'plugins': {'enabled': []}, 'fallback_providers': [],
            'tools': {'tool_search': {'enabled': 'off'}, 'connectors': {'enabled': False}}, 'mcp_servers': {}}))
        set_hermes_home_override(isolated)
        from agent.secret_scope import set_multiplex_context, set_secret_scope
        set_multiplex_context(True)
        set_secret_scope({}, profile_home=str(isolated))
    else:
        set_hermes_home_override(profile)
    from hermes_cli.plugins_discovery import suppress_plugin_discovery
    from hermes_cli.plugins import get_plugin_manager
    if role == 'executor':
        manager = get_plugin_manager()
        manager.discover_and_load()
        loaded = manager._plugins.get('observability/posthog')
        if not loaded or not loaded.enabled or loaded.error:
            raise ValueError('executor PostHog observer unavailable')
    else:
        from types import ModuleType
        module = ModuleType('factory_canary_review_observer')
        exec(base64.b64decode(payload['review_observer'], validate=True), module.__dict__)
        metadata = {'role': 'reviewer', 'review_id': 'profile-canary-' + identifier,
                    'head_sha': COMMIT, 'pass_number': 1, 'repository': 'Driftward-LLC/driftward-engineering'}
        observer = module.attach_observer(metadata, session, set(), telemetry)
        if not observer or not observer.enabled:
            raise ValueError('reviewer PostHog observer unavailable')
    with suppress_plugin_discovery() if role == 'reviewer' else __import__('contextlib').nullcontext():
        from run_agent import AIAgent
        agent = AIAgent(model='gpt-6.1-sol' if role == 'executor' else 'gpt-6-astra', provider='openai-codex',
                        api_mode='codex_responses', enabled_toolsets=[], max_iterations=1, run_budget_seconds=60,
                        fallback_model=[], session_id=session, session_db=None, quiet_mode=True,
                        save_trajectories=False, verbose_logging=False, skip_context_files=True,
                        load_soul_identity=False, skip_memory=True, skip_background_review=True,
                        checkpoints_enabled=False, cwd=str(profile))
        if agent.tools or agent.valid_tool_names or agent._fallback_chain:
            raise ValueError('canary capability drift')
        try:
            result = agent.run_conversation('Reply with exactly FACTORY_PROFILE_CANARY_OK. Do not call tools or take external actions.')
            if result.get('error') or result.get('interrupted'):
                raise ValueError('native model canary failed')
        finally:
            if observer:
                observer.close()
            if scratch:
                scratch.cleanup()
    trace = (str(uuid.uuid5(uuid.NAMESPACE_URL, 'driftward-hermes:trace:session:' + session))
             if role == 'executor' else module.trace_id(metadata))
    return {'role': role, 'profile': ROLES[role], 'session_id': session, 'trace_id': trace,
            'model_run_completed': True, 'posthog_delivery': 'requires_server_verification'}


def container_main(command, payload):
    import contextlib
    import sys
    sys.path.insert(0, '/opt/hermes')
    # Native/plugin imports may print configuration diagnostics. Only our bounded
    # result reaches the canonical CLI; exceptions are sanitized by the launcher.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        if command == 'configure':
            result = configure(payload)
        elif command == 'canary':
            result = canary(payload)
        elif command in {'move-auth', 'rollback-auth'}:
            result = move_auth(command == 'rollback-auth')
        elif command == 'idle':
            idle_runtime()
            result = {'idle': True}
        elif command == 'status':
            result = inventory()
        else:
            raise ValueError('unsupported native profile operation')
    print(json.dumps(result, sort_keys=True))


def run_native(wrapper, command, payload):
    source = Path(__file__).read_text()
    script = source + '\ncontainer_main(' + repr(command) + ', json.loads(' + repr(json.dumps(payload)) + '))\n'
    result = subprocess.run([str(wrapper), 'shell', '-c', 'exec python3 -'], input=script,
                            capture_output=True, text=True, check=True, timeout=180,
                            env={'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin',
                                 'HOME': '/root', 'LANG': 'C.UTF-8', 'NO_COLOR': '1', 'TERM': 'dumb'})
    return json.loads(result.stdout)


def write_private(path, content, mode=0o600):
    no_symlinks(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if path.exists():
        checked(path)
    checked(path.parent, directory=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.factory-profile-', delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(mode)
    temporary.replace(path)


def assert_quiescent(connection):
    marks = ','.join('?' for _ in TERMINAL)
    counts = [connection.execute(f'SELECT count(*) FROM factory_attempts WHERE status NOT IN ({marks})', TERMINAL).fetchone()[0],
              connection.execute('SELECT count(*) FROM factory_execution_leases WHERE released_at IS NULL').fetchone()[0],
              connection.execute("SELECT count(*) FROM bridge_jobs WHERE state NOT IN ('completed','ready_for_human_review','failed')").fetchone()[0]]
    if any(counts):
        raise ValueError('active or unresolved Factory execution')


def systemctl(*args):
    subprocess.run(['systemctl', *args], capture_output=True, check=True, timeout=90)


def gateway_status():
    result = subprocess.run(['systemctl', 'show', '--property=ActiveState,SubState,Result,ExecMainStatus', SERVICE],
                            capture_output=True, text=True, check=True, timeout=15)
    values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    bounded = {}
    for key in ('ActiveState', 'SubState', 'Result'):
        value = values.get(key, '')
        bounded[key] = value if re.fullmatch(r'[a-z][a-z-]{0,39}', value) else 'unknown'
    status = values.get('ExecMainStatus', '')
    bounded['ExecMainStatus'] = int(status) if re.fullmatch(r'[0-9]{1,3}', status) else None
    return bounded


def cutover_status(install):
    receipt = install.directory / 'profile-cutover.json'
    if not receipt.exists() and not receipt.is_symlink():
        return {'phase': 'not_started', 'stage': 'not_started'}
    checked(receipt)
    if receipt.stat().st_mode & 0o077 or receipt.stat().st_size > 65536:
        raise ValueError('unsafe profile cutover receipt')
    value = json.loads(receipt.read_text())
    phases = {'preparing', 'complete', 'rolled_back', 'requires_operator'}
    return {'phase': value.get('phase') if value.get('phase') in phases else 'unknown',
            **{key: value.get(key) if value.get(key) in CUTOVER_STAGES else 'unknown'
               for key in ('stage', 'rollback_stage')}}


def activate(install, wrapper):
    # The canonical launcher holds the installation lock across this operation.
    checked(PROVISION)
    if PROVISION.stat().st_mode & 0o077:
        raise ValueError('reviewer provisioning must be private')
    original = PROVISION.read_bytes()
    provisioning = json.loads(original)
    if not isinstance(provisioning, dict) or provisioning.get('schema') != 'factory-hermes-reviewer-provisioning-v2':
        raise ValueError('unsupported reviewer provisioning')
    target = str(ROOT / 'profiles' / ROLES['reviewer'])
    expected_dropin = PROFILE_DROPIN
    if provisioning.get('auth_root') == target:
        if DROPIN.is_symlink() or not DROPIN.is_file() or DROPIN.read_bytes() != expected_dropin:
            raise ValueError('profile activation state differs')
        return {'activated': True, 'changed': False}
    if provisioning.get('auth_root', str(ROOT / 'factory-reviewer-auth')) != str(ROOT / 'factory-reviewer-auth') or DROPIN.exists() or DROPIN.is_symlink():
        raise ValueError('unmanaged profile routing already exists')
    status = run_native(wrapper, 'status', {})
    if (not status['profiles']['executor']['engineering_enabled'] or not status['profiles']['executor']['posthog_plugin_enabled']
            or not status['profiles']['executor']['capabilities_match_default'] or not status['profiles']['executor']['capabilities_ready']):
        raise ValueError('configure executor plugins before activation')
    if status['profiles']['reviewer']['posthog_plugin_enabled'] or status['profiles']['reviewer']['auth_present']:
        raise ValueError('reviewer profile is not ready for sign-in move')
    assets = []
    for relative, destination in PUBLIC_ASSETS.items():
        source = checked(Path(__file__).parent / relative)
        checked(destination)
        if source.stat().st_size > LIMIT or source.stat().st_mode & 0o022 or destination.stat().st_mode & 0o022:
            raise ValueError('public role artifact is unsafe')
        content = source.read_bytes()
        if not content:
            raise ValueError('public role artifact is empty')
        if relative in REVIEW_SKILL_DIGESTS:
            if hashlib.sha256(content).hexdigest() != REVIEW_SKILL_DIGESTS[relative][1]:
                raise ValueError('reviewer skill snapshot differs from pinned engineering release')
        else:
            marker = (b'DRIFTWARD_HERMES_PROFILE' if relative.startswith('gateway/')
                      else b'auth_root' if relative.endswith('runner.py') else b'factory-independent-reviewer')
            if marker not in content:
                raise ValueError('public role artifact lacks named-profile support')
        assets.append((relative, destination, content, destination.read_bytes(), stat.S_IMODE(destination.stat().st_mode)))
    volume = install.project + '-bridge-data'
    rows = json.loads(subprocess.run(['docker', 'volume', 'inspect', volume], capture_output=True,
                                     text=True, check=True, timeout=30).stdout)
    if len(rows) != 1 or rows[0]['Name'] != volume:
        raise ValueError('bridge volume identity differs')
    database = checked(Path(rows[0]['Mountpoint']) / 'bridge.db')
    services = [row['Service'] for row in install.service_rows()
                if row.get('State') == 'running' and row['Service'] in {'execution-dispatch-worker', 'bridge-executor-worker'}]
    moved = changed = gateway_stopped = workers_touched = False
    published = []
    receipt = install.directory / 'profile-cutover.json'
    stage = 'quiescence'
    def record(next_stage):
        nonlocal stage
        stage = next_stage
        write_private(receipt, json.dumps({'phase': 'preparing', 'stage': stage, 'services': services}).encode())
    record(stage)
    try:
        with sqlite3.connect(database.as_uri() + '?mode=rw', uri=True, timeout=5) as connection:
            connection.execute('BEGIN IMMEDIATE')
            assert_quiescent(connection)
            if services:
                record('workers_stop')
                workers_touched = True
                install.compose('stop', '--timeout', '60', *services, private=True)
            connection.rollback()
        record('gateway_stop')
        gateway_stopped = True
        systemctl('stop', SERVICE)
        record('native_idle')
        run_native(wrapper, 'idle', {})
        record('public_backup')
        backup = install.directory / 'profile-cutover-public-backup'
        if backup.exists():
            raise ValueError('prior public cutover backup requires operator inspection')
        backup.mkdir(mode=0o700)
        record('public_publish')
        for index, (relative, destination, content, previous, mode) in enumerate(assets):
            write_private(backup / str(index), previous)
            published.append((destination, previous, mode))
            write_private(destination, content, mode)
        record('auth_move')
        run_native(wrapper, 'move-auth', {})
        moved = True
        provisioning['auth_root'] = target
        for relative, (field, digest) in REVIEW_SKILL_DIGESTS.items():
            if relative in PUBLIC_ASSETS:
                provisioning[field] = digest
        record('routing_publish')
        changed = True
        write_private(PROVISION, (json.dumps(provisioning, sort_keys=True, indent=2) + '\n').encode())
        write_private(DROPIN, expected_dropin)
        record('daemon_reload')
        systemctl('daemon-reload')
        record('gateway_restart')
        systemctl('restart', SERVICE)
        record('gateway_health')
        systemctl('is-active', '--quiet', SERVICE)
        if services:
            record('workers_restore')
            install.compose('up', '-d', '--no-build', '--pull', 'never', *services, private=True)
        write_private(receipt, json.dumps({'phase': 'complete', 'stage': 'complete', 'services': services, 'auth_root': target,
                      'public_assets': {relative: hashlib.sha256(content).hexdigest() for relative, _, content, _, _ in assets},
                      'public_backup': str(backup)}).encode())
        return {'activated': True, 'changed': True, 'reviewer_auth': 'moved', 'receipt': str(receipt)}
    except BaseException:
        # Roll back only these exact artifacts; never restore broad runtime data.
        failed_stage = stage
        rollback_stage = 'workers_restop'
        try:
            if services and workers_touched:
                install.compose('stop', '--timeout', '60', *services, private=True)
            if changed:
                rollback_stage = 'routing_restore'
                systemctl('stop', SERVICE)
                write_private(PROVISION, original)
                if DROPIN.exists():
                    checked(DROPIN)
                    if DROPIN.read_bytes() != expected_dropin:
                        raise ValueError('gateway profile drop-in changed during cutover')
                    DROPIN.unlink()
            if moved:
                rollback_stage = 'auth_restore'
                run_native(wrapper, 'rollback-auth', {})
            rollback_stage = 'public_restore'
            for destination, previous, mode in reversed(published):
                write_private(destination, previous, mode)
            if gateway_stopped:
                rollback_stage = 'gateway_restart'
                systemctl('daemon-reload')
                systemctl('restart', SERVICE)
            if services and workers_touched:
                rollback_stage = 'workers_restore'
                install.compose('up', '-d', '--no-build', '--pull', 'never', *services, private=True)
            rollback_stage = 'gateway_health'
            systemctl('is-active', '--quiet', SERVICE)
            rollback_stage = 'complete'
            phase = 'rolled_back'
        except BaseException:
            phase = 'requires_operator'
        write_private(receipt, json.dumps({'phase': phase, 'stage': failed_stage,
                                          'rollback_stage': rollback_stage, 'services': services}).encode())
        raise ValueError('profile cutover failed; inspect private cutover receipt') from None


def dispatch(install, config, wrapper, args):
    if args == ['status']:
        result = run_native(wrapper, 'status', {})
        provisioning = json.loads(checked(PROVISION).read_text())
        result['reviewer_routed'] = provisioning.get('auth_root') == str(ROOT / 'profiles' / ROLES['reviewer'])
        environment = subprocess.run(['systemctl', 'show', '--property=Environment', '--value', SERVICE],
                                     capture_output=True, text=True, check=True, timeout=15).stdout
        result['gateway_executor_profile_selected'] = 'DRIFTWARD_HERMES_PROFILE=factory-executor' in environment.split()
        result['gateway_service'] = gateway_status()
        result['cutover'] = cutover_status(install)
        return result
    if len(args) == 3 and args[:2] == ['configure', '--plugin-source']:
        if DROPIN.exists() or DROPIN.is_symlink():
            raise ValueError('active role configuration requires a separately reviewed upgrade')
        archive = subprocess.run(['git', '-C', str(Path(args[2]).resolve()), 'archive', '--format=tar', COMMIT],
                                 capture_output=True, check=True, timeout=30).stdout
        archive_files(archive)
        observer_root = Path(__file__).parent / 'factory-observers' / 'posthog'
        observer = {name: base64.b64encode(checked(observer_root / name).read_bytes()).decode()
                    for name in ('__init__.py', 'plugin.yaml')}
        return run_native(wrapper, 'configure', {'engineering': base64.b64encode(archive).decode(), 'observer': observer})
    if args == ['activate']:
        return activate(install, wrapper)
    if len(args) == 2 and args[0] == 'canary' and args[1] in ROLES:
        payload = {'role': args[1], 'id': str(uuid.uuid4())}
        if args[1] == 'reviewer':
            path = Path(__file__).parent / 'independent-reviewer' / 'review_observer.py'
            payload['review_observer'] = base64.b64encode(checked(path).read_bytes()).decode()
        return run_native(wrapper, 'canary', payload)
    raise ValueError('usage: factory profiles configure --plugin-source REVIEWED_GIT_REPO | status')
