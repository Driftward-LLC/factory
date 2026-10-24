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
import socket
import time
from datetime import datetime, timezone

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
    # Native status is read-only; shared runtime auth need not be copied into the named profile.
    from hermes_cli.auth import get_codex_auth_status
    auth = get_codex_auth_status()
    return {'profiles': result, 'posthog_runtime_configured': bool(os.environ.get('POSTHOG_API_KEY')),
            'executor_auth_ready': auth.get('logged_in') is True and auth.get('auth_mode') == 'chatgpt'
            and auth.get('base_url') == 'https://chatgpt.com/backend-api/codex'}


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


def observer_files(payload):
    files = {name: base64.b64decode(data, validate=True) for name, data in payload['observer'].items()}
    if set(files) != {'__init__.py', 'plugin.yaml'} or sum(map(len, files.values())) > LIMIT:
        raise ValueError('observer package differs')
    return files


def observer_snapshot():
    """Hash protected role state without returning credentials or configuration."""
    protected = {}
    for home in (ROOT, *(ROOT / 'profiles' / name for name in ROLES.values())):
        checked(home, directory=True, owner=runtime_identity()[0] if home in (ROOT, ROOT / 'profiles' / ROLES['executor']) else os.geteuid())
        for name in ('config.yaml', 'auth.json', '.env'):
            path = home / name
            no_symlinks(path)
            if path.exists():
                info = path.stat()
                checked(path, owner=info.st_uid)
                if info.st_mode & 0o022 or info.st_size > LIMIT:
                    raise ValueError('unsafe protected role file')
                protected[str(path)] = [hashlib.sha256(path.read_bytes()).hexdigest(), info.st_uid,
                                        info.st_gid, stat.S_IMODE(info.st_mode), info.st_ino]
            else:
                protected[str(path)] = None
    executor = ROOT / 'profiles' / ROLES['executor']
    links = {}
    for name, parent in (('driftward-engineering', executor / 'plugins'),
                         ('posthog', executor / 'plugins' / 'observability')):
        checked(parent, directory=True)
        path = parent / name
        if not path.is_symlink():
            raise ValueError('managed plugin link required')
        release = path.resolve(strict=True)
        expected = executor / 'factory-managed' / 'releases' / ('factory-posthog' if name == 'posthog' else name)
        if release.parent != expected or (name == 'driftward-engineering' and release.name != COMMIT):
            raise ValueError('managed plugin release identity differs')
        checked(release, directory=True)
        files = {}
        for item in release.rglob('*'):
            checked(item, directory=item.is_dir())
            if item.is_file():
                if item.stat().st_size > LIMIT:
                    raise ValueError('managed plugin file too large')
                files[item.relative_to(release).as_posix()] = item.read_bytes()
        digest = tree_digest(files)
        if name == 'posthog' and (set(files) != {'__init__.py', 'plugin.yaml'} or release.name != digest):
            raise ValueError('observer release digest differs')
        links[name] = {'target': str(release), 'digest': digest}
    return {'protected': recovery_digest(protected), 'links': links}


def upgrade_observer(payload):
    idle_runtime()
    before = observer_snapshot()
    if recovery_digest(before) != payload['snapshot_digest']:
        raise ValueError('native observer snapshot changed')
    files = observer_files(payload)
    digest = tree_digest(files)
    if digest == before['links']['posthog']['digest']:
        raise ValueError('observer release is already selected')
    executor = ROOT / 'profiles' / ROLES['executor']
    release = immutable_release(executor / 'factory-managed' / 'releases' / 'factory-posthog', digest, files)
    # Persist release bytes before the selected symlink can survive a restart.
    for path in (*sorted(release.rglob('*'), key=lambda item: len(item.parts), reverse=True), release, release.parent):
        if path.is_file():
            with path.open('rb') as handle:
                os.fsync(handle.fileno())
        else:
            sync_directory(path)
    link = executor / 'plugins' / 'observability' / 'posthog'
    temporary = link.parent / ('.factory-observer-' + str(uuid.uuid4()))
    temporary.symlink_to(release, target_is_directory=True)
    try:
        if observer_snapshot() != before:
            raise ValueError('native observer snapshot changed')
        temporary.replace(link)
        sync_directory(link.parent)
    finally:
        if temporary.is_symlink():
            temporary.unlink()
    after = observer_snapshot()
    if after['protected'] != before['protected'] or after['links']['driftward-engineering'] != before['links']['driftward-engineering']:
        raise ValueError('protected role state changed')
    return after


def github_canary(payload):
    """One fixed local version invocation through the real native dispatcher."""
    selected = observer_snapshot()['links']['posthog']['digest']
    if selected != payload['observer_digest']:
        raise ValueError('GitHub canary observer release differs')
    from hermes_constants import set_hermes_home_override
    set_hermes_home_override(ROOT / 'profiles' / ROLES['executor'])
    from hermes_cli.plugins import get_plugin_manager
    manager = get_plugin_manager()
    manager.discover_and_load()
    plugin = manager._plugins.get('observability/posthog')
    if not plugin or not plugin.enabled or plugin.error or 'factory_github' not in plugin.tools_registered:
        raise ValueError('native GitHub observer unavailable')
    observer = plugin.module
    if not observer._enabled():
        raise ValueError('GitHub canary telemetry unavailable')
    identifier = str(uuid.UUID(payload['id']))
    ids = {'session_id': 'factory-github-canary:' + identifier, 'task_id': identifier,
           'turn_id': identifier, 'tool_call_id': identifier}
    observed = []
    original = observer._enqueue
    def capture(event):
        properties = event.get('properties', {})
        if properties.get('hermes.tool_call_id') == identifier:
            properties['factory.validation_only'] = True
            properties['factory.validation_kind'] = 'github_version_canary'
            observed.append(event)
        original(event)
    observer._enqueue = capture
    try:
        from model_tools import handle_function_call
        response = json.loads(handle_function_call('tool_call', {'calls': [{'name': 'factory_github',
            'arguments': {'args': ['--version'], 'workdir': '/tmp', 'timeout': 15}}]},
            enabled_toolsets=['terminal'], **ids))
    finally:
        observer._enqueue = original
        observer._flush_at_exit()
    parents = [event['properties'] for event in observed if event['properties'].get('$ai_span_name') == 'factory_github']
    children = [event['properties'] for event in observed if event['properties'].get('github.observation') == 'cli_invocation']
    if len(parents) != 1 or len(children) != 1 or response.get('exit_code') != 0:
        raise ValueError('native GitHub canary failed')
    parent, child = parents[0], children[0]
    if (child['$ai_parent_id'] != parent['$ai_span_id'] or child['$ai_trace_id'] != parent['$ai_trace_id']
            or parent['hermes.tool_status'] != 'ok' or child['github.cli_operation'] != 'help_or_version'
            or child['github.cli_status'] != 'success'):
        raise ValueError('native GitHub canary correlation failed')
    return {'session_id': ids['session_id'], 'trace_id': parent['$ai_trace_id'],
            'tool_call_id': identifier, 'parent_span_id': parent['$ai_span_id'], 'child_span_id': child['$ai_span_id'],
            'operation': 'help_or_version', 'exit_code': 0, 'observer_digest': selected,
            'validation_only': True, 'posthog_delivery': 'server_verification_pending'}


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
        elif command == 'observer-snapshot':
            idle_runtime()
            result = observer_snapshot()
        elif command == 'observer-upgrade':
            result = upgrade_observer(payload)
        elif command == 'github-canary':
            idle_runtime()
            result = github_canary(payload)
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


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_observer_receipt(path, value):
    write_private(path, json.dumps(value).encode())
    sync_directory(path.parent)


def assert_quiescent(connection):
    marks = ','.join('?' for _ in TERMINAL)
    counts = [connection.execute(f'SELECT count(*) FROM factory_attempts WHERE status NOT IN ({marks})', TERMINAL).fetchone()[0],
              connection.execute('SELECT count(*) FROM factory_execution_leases WHERE released_at IS NULL').fetchone()[0],
              connection.execute("SELECT count(*) FROM bridge_jobs WHERE state NOT IN ('completed','ready_for_human_review','failed')").fetchone()[0]]
    if any(counts):
        raise ValueError('active or unresolved Factory execution')


def bridge_database(install):
    volume = install.project + '-bridge-data'
    rows = json.loads(subprocess.run(['docker', 'volume', 'inspect', volume], capture_output=True,
                                     text=True, check=True, timeout=30).stdout)
    if len(rows) != 1 or rows[0]['Name'] != volume:
        raise ValueError('bridge volume identity differs')
    return checked(Path(rows[0]['Mountpoint']) / 'bridge.db')


def recovery_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def inspection_snapshot(connection, job_id):
    """Private snapshot: only its digest and bounded facts may leave this helper."""
    connection.row_factory = sqlite3.Row
    job = connection.execute('SELECT * FROM bridge_jobs WHERE job_id=?', (job_id,)).fetchone()
    if not job:
        raise ValueError('inspection job not found')
    result = {'job': dict(job)}
    for name in ('factory_inspection_requests', 'factory_inspection_outbox'):
        result[name] = [dict(row) for row in connection.execute(f'SELECT * FROM {name} WHERE job_id=? LIMIT 3', (job_id,))]
    result['prior_attempts'] = [dict(row) for row in connection.execute(
        'SELECT * FROM factory_attempts WHERE attempt_id IN (SELECT prior_attempt_id FROM factory_inspection_requests WHERE job_id=?)', (job_id,))]
    result['events'] = [dict(row) for row in connection.execute('SELECT * FROM bridge_events WHERE job_id=? ORDER BY event_id LIMIT 3', (job_id,))]
    # Check all schema-declared job references, including future evidence tables.
    links = {}
    tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    # Stale-job warnings are observations, not execution authority. Their
    # counters/timestamps may refresh while an operator reviews the preview.
    result['diagnostic_findings'] = [dict(row) for row in connection.execute(
        'SELECT finding_key,job_id,kind FROM reconciliation_findings WHERE job_id=? ORDER BY finding_key LIMIT 11', (job_id,))] if 'reconciliation_findings' in tables else []
    for table in tables:
        if not re.fullmatch(r'[a-z_]+', table) or table in {'bridge_jobs', 'bridge_events', 'factory_inspection_requests', 'factory_inspection_outbox', 'reconciliation_findings'}:
            continue
        columns = [row[1] for row in connection.execute(f'PRAGMA table_info({table})')
                   if row[1] in {'job_id', 'source_job_id', 'prior_job_id'}]
        if columns:
            links[table] = connection.execute(f'SELECT count(*) FROM {table} WHERE ' + ' OR '.join(f'{column}=?' for column in columns),
                                              [job_id] * len(columns)).fetchone()[0]
    result['links'] = links
    marks = ','.join('?' for _ in TERMINAL)
    result['guards'] = [connection.execute(f'SELECT count(*) FROM factory_attempts WHERE status NOT IN ({marks})', TERMINAL).fetchone()[0],
                        connection.execute('SELECT count(*) FROM factory_execution_leases WHERE released_at IS NULL').fetchone()[0],
                        connection.execute("SELECT count(*) FROM bridge_jobs WHERE job_id!=? AND state NOT IN ('completed','ready_for_human_review','failed')", (job_id,)).fetchone()[0]]
    return result


def eligible_inspection(snapshot):
    job = snapshot['job']
    requests = snapshot['factory_inspection_requests']
    outboxes = snapshot['factory_inspection_outbox']
    if (job['state'] != 'accepted' or job['requested_action'] != 'inspect_draft_pr_state'
            or any(snapshot['links'].values()) or any(snapshot['guards'])
            or len(snapshot['diagnostic_findings']) > 10
            or any(row['kind'] != 'stale_active_bridge_job' for row in snapshot['diagnostic_findings'])
            or len(requests) != 1 or len(outboxes) != 1
            or len(snapshot['events']) != 1 or snapshot['events'][0]['event_type'] != 'accepted'
            or snapshot['events'][0]['state'] != 'accepted'):
        raise ValueError('only an untouched, linked expired inspection without execution evidence can be recovered')
    if any(value for key, value in job.items() if key not in {
            'job_id', 'idempotency_key', 'state', 'requested_action', 'payload_json', 'payload_hash', 'branch', 'created_at', 'updated_at'}):
        raise ValueError('inspection has workflow, execution, artifact or result evidence')
    payload = json.loads(job['payload_json'])
    if not isinstance(payload, dict):
        raise ValueError('inspection payload is unsupported')
    request, outbox = requests[0], outboxes[0]
    inspection = payload.get('inspection', {})
    if (set(payload) != {'repo', 'inspection', 'inspection_request_key', 'inspection_capability', 'dry_run'}
            or payload['dry_run'] is not False or request['state'] != 'pending'
            or any(request[key] for key in ('result_json', 'result_digest', 'observed_at', 'reason'))
            or request['request_key'] != job['idempotency_key'] or payload['inspection_request_key'] != request['request_key']
            or payload['inspection_capability'] != request['capability']
            or not isinstance(payload['inspection_capability'], str) or len(payload['inspection_capability']) < 20
            or payload['repo'] != request['repository']
            or inspection != {key: request[key] for key in ('repository', 'number', 'url')}
            or outbox['request_key'] != request['request_key']
            or outbox['delivery_lease_id'] or outbox['delivery_lease_expires_at']):
        raise ValueError('inspection linkage, result or delivery evidence is unsupported')
    prior = snapshot['prior_attempts']
    if request['prior_attempt_id'] and (len(prior) != 1 or prior[0]['status'] not in TERMINAL
            or prior[0]['work_item_key'] != request['work_item_key'] or prior[0]['job_id'] == job['job_id']):
        raise ValueError('inspection prior attempt is unresolved or mismatched')
    for value in (job['created_at'], request['created_at']):
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(value.replace('Z', '+00:00'))).total_seconds()
        if age <= 120:
            raise ValueError('inspection capability has not expired')
    return request


def recover_inspection(install, wrapper, args):
    import argparse
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise ValueError('invalid inspection recovery arguments')
    parser = Parser(prog='factory profiles recover-inspection', add_help=False)
    parser.add_argument('job_id')
    parser.add_argument('operation', choices=('preview', 'apply', 'status'))
    parser.add_argument('--snapshot-digest')
    parser.add_argument('--reason')
    parser.add_argument('--request-id')
    parser.add_argument('--yes', action='store_true')
    options = parser.parse_args(args)
    if str(uuid.UUID(options.job_id)) != options.job_id:
        raise ValueError('canonical job UUID required')
    database = bridge_database(install)
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5) as connection:
        snapshot = inspection_snapshot(connection, options.job_id)
    if options.operation == 'status':
        if any((options.snapshot_digest, options.reason, options.request_id, options.yes)):
            raise ValueError('status does not accept approval arguments')
        receipt = install.directory / 'inspection-recovery.json'
        summary = {'phase': 'not_started', 'committed': False, 'errors': []}
        if receipt.exists() or receipt.is_symlink():
            checked(receipt)
            if receipt.stat().st_mode & 0o077 or receipt.stat().st_size > 65536:
                raise ValueError('unsafe inspection recovery receipt')
            value = json.loads(receipt.read_text())
            if value.get('job_id') == options.job_id:
                summary = {'phase': value.get('phase') if value.get('phase') in {'preparing', 'complete', 'requires_operator'} else 'unknown',
                           'committed': value.get('committed') is True,
                           'errors': [token for token in ('writers_stop', 'gateway_stop', 'native_idle', 'transaction', 'gateway_restore', 'writers_restore')
                                      if token in value.get('errors', [])]}
        bounded_states = {'accepted', 'running', 'failed', 'completed', 'pending', 'expired', 'acknowledged', 'delivering', 'retrying'}
        def state(row):
            return row['state'] if row['state'] in bounded_states else 'unknown'
        audited = any(event['event_type'] == 'inspection_expired' and event['state'] == 'failed'
                      and json.loads(event['details_json']).get('actor') == 'root'
                      and 'intent_digest' in json.loads(event['details_json']) for event in snapshot['events'])
        return {'job_id': options.job_id, 'state': state(snapshot['job']),
                'inspection_states': [state(row) for row in snapshot['factory_inspection_requests']],
                'outbox_states': [state(row) for row in snapshot['factory_inspection_outbox']],
                'recovery_audit_present': audited, 'recovery': summary, 'read_only': True}
    if options.operation == 'preview':
        if any((options.snapshot_digest, options.reason, options.request_id, options.yes)):
            raise ValueError('preview does not accept approval arguments')
        eligible_inspection(snapshot)
        return {'job_id': options.job_id, 'action': 'expire_incomplete_inspection', 'state': 'accepted',
                'snapshot_digest': recovery_digest(snapshot), 'inspection_expired': True,
                'execution_evidence': False, 'read_only': True}
    if (not options.yes or not re.fullmatch(r'[0-9a-f]{64}', options.snapshot_digest or '')
            or not options.reason or not 8 <= len(options.reason) <= 300
            or not all(character.isprintable() for character in options.reason)
            or not options.request_id or str(uuid.UUID(options.request_id)) != options.request_id):
        raise ValueError('apply requires exact digest, reason, request UUID and --yes')
    intent = recovery_digest({'job_id': options.job_id, 'snapshot_digest': options.snapshot_digest,
                              'reason': options.reason, 'request_id': options.request_id})
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'factory:inspection-recovery:' + options.request_id))
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=5) as connection:
        previous = connection.execute('SELECT job_id,details_json FROM bridge_events WHERE event_id=?', (event_id,)).fetchone()
    if previous:
        details = json.loads(previous[1])
        persisted = {key: value for key, value in snapshot.items() if key != 'events'}
        if previous[0] != options.job_id or details.get('intent_digest') != intent or details.get('result_digest') != recovery_digest(persisted):
            raise ValueError('recovery request reuse or recovered state drift')
        return {'job_id': options.job_id, 'recovered': True, 'changed': False, 'state': 'failed'}
    eligible_inspection(snapshot)
    if recovery_digest(snapshot) != options.snapshot_digest:
        raise ValueError('inspection recovery snapshot changed')
    # The old executor image has no terminal inspection guard. Keep an already
    # stopped executor stopped until that separately reviewed image is installed.
    rows = install.service_rows()
    if [row.get('State') for row in rows if row['Service'] == 'bridge-executor-worker'] != ['exited']:
        raise ValueError('inspection recovery requires the executor already exited')
    writers = {'bridge', 'factory-operator', 'outbox-dispatcher', 'intake-worker', 'execution-dispatch-worker',
               'pr-event-worker', 'reconciliation-worker'}
    services = [row['Service'] for row in rows if row['Service'] in writers and row.get('State') == 'running']
    if any(row['Service'] in writers and row.get('State') not in {'running', 'exited'} for row in rows):
        raise ValueError('inspection writer service state is unresolved')
    receipt = install.directory / 'inspection-recovery.json'
    gateway_before = gateway_status()
    stopped = gateway_stopped = False
    errors = []
    committed = False
    stage = 'writers_stop'
    write_private(receipt, json.dumps({'phase': 'preparing', 'stage': stage, 'job_id': options.job_id,
                                     'request_id': options.request_id, 'services': services, 'gateway_before': gateway_before}).encode())
    try:
        if services:
            stopped = True
            install.compose('stop', '--timeout', '60', *services, private=True)
        stage = 'gateway_stop'
        gateway_stopped = True
        systemctl('stop', SERVICE)
        if gateway_status()['ActiveState'] not in {'inactive', 'failed'}:
            raise ValueError('gateway is not stopped')
        current = install.service_rows()
        if any(row['Service'] in writers | {'bridge-executor-worker'} and row.get('State') != 'exited' for row in current):
            raise ValueError('inspection writer did not stop')
        stage = 'native_idle'
        run_native(wrapper, 'idle', {})
        stage = 'transaction'
        with sqlite3.connect(database.as_uri() + '?mode=rw', uri=True, timeout=5) as connection:
            connection.execute('BEGIN IMMEDIATE')
            snapshot = inspection_snapshot(connection, options.job_id)
            eligible_inspection(snapshot)
            if recovery_digest(snapshot) != options.snapshot_digest:
                raise ValueError('inspection recovery snapshot changed')
            now = datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
            # Match the existing inspection expiry domain: retain every row and
            # close delivery authority; never invent an inspection result.
            connection.execute("UPDATE factory_inspection_requests SET state='expired',reason='inspection_capability_expired',updated_at=? WHERE job_id=?", (now, options.job_id))
            connection.execute("UPDATE factory_inspection_outbox SET state='expired',acknowledged_at=COALESCE(acknowledged_at,?),delivery_lease_id=NULL,delivery_lease_expires_at=NULL,updated_at=? WHERE job_id=? AND acknowledged_at IS NULL", (now, now, options.job_id))
            connection.execute("UPDATE bridge_jobs SET state='failed',reason_code='inspection_capability_expired',error='inspection capability expired before completion',updated_at=? WHERE job_id=?", (now, options.job_id))
            persisted = inspection_snapshot(connection, options.job_id)
            details = {'reason_code': 'inspection_capability_expired', 'actor': 'root', 'reason': options.reason,
                       'request_id': options.request_id, 'snapshot_digest': options.snapshot_digest,
                       'intent_digest': intent, 'result_digest': recovery_digest({key: value for key, value in persisted.items() if key != 'events'})}
            connection.execute('INSERT INTO bridge_events VALUES (?,?,?,?,?,?)',
                               (event_id, options.job_id, 'inspection_expired', 'failed', json.dumps(details, sort_keys=True), now))
        committed = True
    except BaseException:
        errors.append(stage)
    finally:
        # Restore each owned service independently, including when a broken
        # baseline gateway cannot restart. Never call it a healthy rollback.
        if gateway_stopped and gateway_before['ActiveState'] in {'active', 'activating', 'reloading'}:
            try:
                systemctl('start', SERVICE)
            except BaseException:
                errors.append('gateway_restore')
        if stopped:
            try:
                install.compose('up', '-d', '--no-deps', '--no-build', '--pull', 'never', *services, private=True)
                restored = {row['Service']: row.get('State') for row in install.service_rows()}
                if any(restored.get(service) != 'running' for service in services) or restored.get('bridge-executor-worker') != 'exited':
                    raise ValueError('writer restoration differs')
            except BaseException:
                errors.append('writers_restore')
        write_private(receipt, json.dumps({'phase': 'requires_operator' if errors else 'complete', 'errors': errors,
                      'committed': committed, 'job_id': options.job_id, 'request_id': options.request_id,
                      'services': services, 'gateway_before': gateway_before}).encode())
    if errors:
        raise ValueError('inspection recovery requires operator inspection of private receipt')
    return {'job_id': options.job_id, 'recovered': True, 'changed': True, 'state': 'failed',
            'receipt': str(receipt), 'executor': 'remains_exited'}


def systemctl(*args):
    subprocess.run(['systemctl', *args], capture_output=True, check=True, timeout=90)


def gateway_status(*, timeout=15):
    result = subprocess.run(['systemctl', 'show', '--property=ActiveState,SubState,Result,ExecMainStatus', SERVICE],
                            capture_output=True, text=True, check=True, timeout=timeout)
    values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    bounded = {}
    for key in ('ActiveState', 'SubState', 'Result'):
        value = values.get(key, '')
        bounded[key] = value if re.fullmatch(r'[a-z][a-z-]{0,39}', value) else 'unknown'
    status = values.get('ExecMainStatus', '')
    bounded['ExecMainStatus'] = int(status) if re.fullmatch(r'[0-9]{1,3}', status) else None
    return bounded


def gateway_socket_listening(*, timeout=0.5):
    path = Path('/run/driftward-hermes-factory/hermes.sock')
    try:
        no_symlinks(path)
        if not stat.S_ISSOCK(path.stat().st_mode):
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(path))
        return True
    except (OSError, ValueError):
        return False


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
    database = bridge_database(install)
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


def observer_payload():
    root = Path(__file__).parent / 'factory-observers' / 'posthog'
    return {'observer': {name: base64.b64encode(checked(root / name).read_bytes()).decode()
                         for name in ('__init__.py', 'plugin.yaml')}}


def observer_host_digest(install):
    files = [DROPIN, PROVISION, Path(__file__), install.directory / '.env']
    env = install.directory / 'env'
    checked(env, directory=True)
    files.extend(sorted(env.iterdir()))
    hashes = {}
    for path in files:
        checked(path)
        if path.stat().st_size > LIMIT or path.stat().st_mode & 0o022:
            raise ValueError('unsafe observer upgrade input')
        hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return recovery_digest(hashes)


def observer_upgrade_snapshot(install, wrapper):
    checked(DROPIN)
    if DROPIN.read_bytes() != PROFILE_DROPIN:
        raise ValueError('active executor routing required')
    provision = checked(PROVISION)
    if json.loads(provision.read_text()).get('auth_root') != str(ROOT / 'profiles' / ROLES['reviewer']):
        raise ValueError('active reviewer routing required')
    rows = sorted((row['Service'], row.get('State')) for row in observer_service_rows(install))
    if dict(rows).get('bridge-executor-worker') != 'exited' or dict(rows).get('execution-dispatch-worker') not in {'running', 'exited'}:
        raise ValueError('observer upgrade requires paused executor and resolved dispatcher')
    if gateway_status()['ActiveState'] != 'active':
        raise ValueError('active gateway required')
    with sqlite3.connect(bridge_database(install).as_uri() + '?mode=ro', uri=True, timeout=5) as connection:
        assert_quiescent(connection)
    native = run_native(wrapper, 'observer-snapshot', {})
    target = tree_digest(observer_files(observer_payload()))
    if native['links']['posthog']['digest'] == target:
        raise ValueError('observer release is already selected')
    return {'native': native, 'target_digest': target, 'host_inputs': observer_host_digest(install), 'services': rows}


OBSERVER_HEALTH_PREDICATES = {'host_inputs_changed', 'service_states', 'gateway_active', 'gateway_listener'}


def observer_compose(install, *args, timeout=90):
    # Installed helpers may use an older Production bundle without timeout keywords.
    result = subprocess.run([*install.args, *args], env=install.env, capture_output=True,
                            text=True, check=True, timeout=timeout)
    return result.stdout


def observer_service_rows(install, *, timeout=15):
    raw = observer_compose(install, 'ps', '--all', '--format', 'json', timeout=timeout)
    return json.loads(raw) if raw.strip().startswith('[') else [json.loads(line) for line in raw.splitlines() if line.strip()]


class ObserverHealthFailure(ValueError):
    def __init__(self, predicate):
        super().__init__('observer service readiness failed')
        self.predicate = predicate


def wait_observer_health(install, services, host_inputs, timeout=30):
    deadline = time.monotonic() + timeout
    predicate = 'service_states'
    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise ObserverHealthFailure(predicate)
        return seconds
    while True:
        remaining()
        if observer_host_digest(install) != host_inputs:
            raise ObserverHealthFailure('host_inputs_changed')
        try:
            predicate = 'service_states'
            current = sorted((row['Service'], row.get('State')) for row in observer_service_rows(install, timeout=remaining()))
            remaining()
            if current == services:
                predicate = 'gateway_active'
                active = gateway_status(timeout=min(15, remaining()))['ActiveState']
                remaining()
                if active == 'active':
                    predicate = 'gateway_listener'
                    listening = gateway_socket_listening(timeout=min(0.5, remaining()))
                    remaining()
                    if listening:
                        return
        except subprocess.TimeoutExpired:
            raise ObserverHealthFailure(predicate) from None
        if time.monotonic() >= deadline:
            raise ObserverHealthFailure(predicate)
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))


def read_observer_receipt(install, request_id, prefix='observer-upgrade-'):
    receipt = install.directory / (prefix + request_id + '.json')
    if not receipt.exists() and not receipt.is_symlink():
        return None
    checked(receipt)
    info = receipt.stat()
    if info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > 65536:
        raise ValueError('unsafe observer upgrade receipt')
    value = json.loads(receipt.read_text())
    phases = {'consumed', 'complete', 'requires_operator'}
    stages = {'dispatcher_stop', 'gateway_stop', 'observer_upgrade', 'gateway_restore',
              'dispatcher_restore', 'restored_health'}
    if (not isinstance(value, dict) or value.get('request_id') != request_id
            or value.get('actor') != 'root' or not isinstance(value.get('phase'), str) or value['phase'] not in phases
            or any(not isinstance(value.get(key), str) or not re.fullmatch(r'[0-9a-f]{64}', value[key])
                   for key in ('snapshot_digest', 'source_digest', 'target_digest'))
            or ('stage' in value and (not isinstance(value['stage'], str) or value['stage'] not in stages))
            or not isinstance(value.get('stop_errors', []), list)
            or len(value.get('stop_errors', [])) > 2
            or any(not isinstance(error, str) or error not in {'dispatcher_stop', 'gateway_stop'} for error in value.get('stop_errors', []))):
        raise ValueError('invalid observer upgrade receipt')
    if 'failed_predicate' in value and (not isinstance(value['failed_predicate'], str) or value['failed_predicate'] not in OBSERVER_HEALTH_PREDICATES):
        raise ValueError('invalid observer readiness predicate')
    return value


def observer_upgrade_status(install, wrapper, request_id, prefix='observer-upgrade-'):
    value = read_observer_receipt(install, request_id, prefix)
    if value is None:
        return {'request_id': request_id, 'phase': 'not_started', 'stage': 'not_started',
                'stop_errors': [], 'read_only': True}
    selected = None
    try:
        selected = run_native(wrapper, 'observer-snapshot', {})['links']['posthog']['digest']
        if not isinstance(selected, str) or not re.fullmatch(r'[0-9a-f]{64}', selected):
            selected = None
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
        pass
    gateway = 'unavailable'
    states = {name: 'unavailable' for name in ('bridge-executor-worker', 'execution-dispatch-worker')}
    try:
        active = gateway_status()['ActiveState']
        gateway = active if active in {'active', 'inactive', 'failed', 'activating', 'deactivating', 'reloading'} else 'unknown'
        for row in observer_service_rows(install):
            if row['Service'] in states:
                states[row['Service']] = row.get('State') if row.get('State') in {'running', 'exited'} else 'unknown'
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError):
        pass
    return {'request_id': request_id, 'phase': value['phase'],
            'stage': value.get('stage', 'complete' if value['phase'] == 'complete' else 'unknown'),
            'stop_errors': value.get('stop_errors', []),
            'failed_predicate': value.get('failed_predicate', 'unknown'),
            **{key: value[key] for key in ('snapshot_digest', 'source_digest', 'target_digest')},
            'current_selected_observer_digest': selected,
            'native_snapshot_status': 'available' if selected else 'unavailable',
            'gateway_state': gateway, 'execution_service_states': states,
            'read_only': True}


def observer_recovery_snapshot(install, wrapper, failed_request_id, restore_dispatcher):
    failed = read_observer_receipt(install, failed_request_id)
    if (failed is None or failed['phase'] != 'requires_operator' or failed.get('stage') != 'restored_health'
            or failed.get('stop_errors', []) or failed['source_digest'] == failed['target_digest']):
        raise ValueError('only a cleanly stopped post-upgrade readiness failure can be recovered')
    prior = failed.get('prior_services')
    if prior is not None:
        if (not isinstance(prior, list) or any(not isinstance(row, list) or len(row) != 2
                                             or not all(isinstance(item, str) for item in row) for row in prior)
                or dict(prior).get('bridge-executor-worker') != 'exited'
                or dict(prior).get('execution-dispatch-worker') not in {'running', 'exited'}
                or (restore_dispatcher and dict(prior)['execution-dispatch-worker'] != 'running')):
            raise ValueError('recorded prior services do not authorize dispatcher restoration')
    checked(DROPIN)
    if (DROPIN.read_bytes() != PROFILE_DROPIN
            or json.loads(checked(PROVISION).read_text()).get('auth_root') != str(ROOT / 'profiles' / ROLES['reviewer'])):
        raise ValueError('active role routing differs')
    rows = sorted((row['Service'], row.get('State')) for row in observer_service_rows(install))
    if (any(dict(rows).get(name) != 'exited' for name in ('bridge-executor-worker', 'execution-dispatch-worker'))
            or gateway_status()['ActiveState'] not in {'inactive', 'failed'}):
        raise ValueError('observer recovery requires stopped gateway and execution services')
    with sqlite3.connect(bridge_database(install).as_uri() + '?mode=ro', uri=True, timeout=5) as connection:
        assert_quiescent(connection)
    native = run_native(wrapper, 'observer-snapshot', {})
    target = tree_digest(observer_files(observer_payload()))
    if native['links']['posthog']['digest'] != failed['target_digest'] or target != failed['target_digest']:
        raise ValueError('observer recovery requires the exact already-selected target')
    return {'failed_request_id': failed_request_id, 'failed_receipt_digest': recovery_digest(failed),
            'source_digest': failed['source_digest'], 'target_digest': target, 'native': native,
            'host_inputs': observer_host_digest(install), 'services': rows,
            'restore_dispatcher': restore_dispatcher,
            'dispatcher_evidence': 'recorded_prior_services' if prior is not None else 'operator_selection'}


def recover_observer(install, wrapper, args):
    import argparse
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise ValueError('invalid observer recovery arguments')
    parser = Parser(prog='factory profiles recover-observer', add_help=False)
    parser.add_argument('failed_request_id')
    parser.add_argument('operation', choices=('preview', 'apply', 'status'))
    parser.add_argument('--restore-dispatcher', action='store_true')
    parser.add_argument('--snapshot-digest')
    parser.add_argument('--request-id')
    parser.add_argument('--reason')
    parser.add_argument('--yes', action='store_true')
    options = parser.parse_args(args)
    if str(uuid.UUID(options.failed_request_id)) != options.failed_request_id:
        raise ValueError('canonical failed request UUID required')
    if options.operation == 'status':
        if (any((options.restore_dispatcher, options.snapshot_digest, options.reason, options.yes))
                or not options.request_id or str(uuid.UUID(options.request_id)) != options.request_id):
            raise ValueError('recovery status requires only failed request and recovery request UUIDs')
        value = read_observer_receipt(install, options.request_id, 'observer-recovery-')
        if value is not None and value.get('failed_request_id') != options.failed_request_id:
            raise ValueError('observer recovery request mismatch')
        result = observer_upgrade_status(install, wrapper, options.request_id, 'observer-recovery-')
        result['failed_request_id'] = options.failed_request_id
        return result
    if options.operation == 'preview':
        if any((options.snapshot_digest, options.reason, options.request_id, options.yes)):
            raise ValueError('recovery preview does not accept approval arguments')
        snapshot = observer_recovery_snapshot(install, wrapper, options.failed_request_id, options.restore_dispatcher)
        return {'failed_request_id': options.failed_request_id, 'snapshot_digest': recovery_digest(snapshot),
                'source_digest': snapshot['source_digest'], 'target_digest': snapshot['target_digest'],
                'restore_dispatcher': options.restore_dispatcher, 'dispatcher_evidence': snapshot['dispatcher_evidence'],
                'paused_execution': ['bridge-executor-worker'], 'observer_mutation': False, 'read_only': True}
    if (not options.yes or not re.fullmatch(r'[0-9a-f]{64}', options.snapshot_digest or '')
            or not options.reason or not 8 <= len(options.reason) <= 300
            or not all(character.isprintable() for character in options.reason)
            or not options.request_id or str(uuid.UUID(options.request_id)) != options.request_id):
        raise ValueError('recovery apply requires exact digest, reason, new request UUID and --yes')
    receipt = install.directory / ('observer-recovery-' + options.request_id + '.json')
    if receipt.exists() or receipt.is_symlink():
        raise ValueError('observer recovery request already consumed')
    snapshot = observer_recovery_snapshot(install, wrapper, options.failed_request_id, options.restore_dispatcher)
    if recovery_digest(snapshot) != options.snapshot_digest:
        raise ValueError('observer recovery snapshot changed')
    audit = {'phase': 'consumed', 'request_id': options.request_id, 'actor': 'root', 'reason': options.reason,
             'failed_request_id': options.failed_request_id, 'failed_receipt_digest': snapshot['failed_receipt_digest'],
             'snapshot_digest': options.snapshot_digest, 'source_digest': snapshot['source_digest'],
             'target_digest': snapshot['target_digest'], 'restore_dispatcher': options.restore_dispatcher,
             'dispatcher_evidence': snapshot['dispatcher_evidence'], 'prior_services': snapshot['services'],
             'paused_execution': ['bridge-executor-worker'], 'observer_mutation': False,
             'timestamp': datetime.now(timezone.utc).isoformat()}
    write_observer_receipt(receipt, audit)
    stage = 'gateway_restore'
    try:
        with sqlite3.connect(bridge_database(install).as_uri() + '?mode=rw', uri=True, timeout=5) as connection:
            connection.execute('BEGIN IMMEDIATE')
            assert_quiescent(connection)
            if observer_recovery_snapshot(install, wrapper, options.failed_request_id, options.restore_dispatcher) != snapshot:
                raise ValueError('observer recovery inputs changed')
            systemctl('start', SERVICE)
            wait_observer_health(install, snapshot['services'], snapshot['host_inputs'])
            stage = 'dispatcher_restore'
            expected = dict(snapshot['services'])
            if options.restore_dispatcher:
                observer_compose(install, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', 'execution-dispatch-worker')
                expected['execution-dispatch-worker'] = 'running'
            stage = 'restored_health'
            wait_observer_health(install, sorted(expected.items()), snapshot['host_inputs'])
            if run_native(wrapper, 'observer-snapshot', {}) != snapshot['native']:
                raise ValueError('observer recovery protected state changed')
            connection.rollback()
        audit['phase'] = 'complete'
        write_observer_receipt(receipt, audit)
        return {'recovered': True, 'request_id': options.request_id, 'failed_request_id': options.failed_request_id,
                'restore_dispatcher': options.restore_dispatcher, 'paused_execution': ['bridge-executor-worker'],
                'observer_mutation': False, 'target_digest': snapshot['target_digest']}
    except BaseException as error:
        audit.update(phase='requires_operator', stage=stage, stop_errors=[])
        if isinstance(error, ObserverHealthFailure):
            audit['failed_predicate'] = error.predicate
        for name, action in (('dispatcher_stop', lambda: observer_compose(install, 'stop', '--timeout', '60', 'execution-dispatch-worker') if options.restore_dispatcher else None),
                             ('gateway_stop', lambda: systemctl('stop', SERVICE))):
            try:
                action()
            except BaseException:
                audit['stop_errors'].append(name)
        write_observer_receipt(receipt, audit)
        raise ValueError('observer recovery failed; inspect canonical recovery status') from None


def observer_upgrade(install, wrapper, args):
    import argparse
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise ValueError('invalid observer upgrade arguments')
    parser = Parser(prog='factory profiles upgrade-observer', add_help=False)
    parser.add_argument('operation', choices=('preview', 'apply', 'status'))
    parser.add_argument('--snapshot-digest')
    parser.add_argument('--reason')
    parser.add_argument('--request-id')
    parser.add_argument('--yes', action='store_true')
    options = parser.parse_args(args)
    if options.operation == 'status':
        if (any((options.snapshot_digest, options.reason, options.yes)) or not options.request_id
                or str(uuid.UUID(options.request_id)) != options.request_id):
            raise ValueError('status requires only a canonical request UUID')
        return observer_upgrade_status(install, wrapper, options.request_id)
    if options.operation == 'preview':
        if any((options.snapshot_digest, options.reason, options.request_id, options.yes)):
            raise ValueError('preview does not accept approval arguments')
        snapshot = observer_upgrade_snapshot(install, wrapper)
        return {'action': 'upgrade_executor_observer', 'snapshot_digest': recovery_digest(snapshot),
                'source_digest': snapshot['native']['links']['posthog']['digest'],
                'target_digest': snapshot['target_digest'], 'paused_execution': ['bridge-executor-worker'], 'read_only': True}
    if (not options.yes or not re.fullmatch(r'[0-9a-f]{64}', options.snapshot_digest or '')
            or not options.reason or not 8 <= len(options.reason) <= 300
            or not all(character.isprintable() for character in options.reason)
            or not options.request_id or str(uuid.UUID(options.request_id)) != options.request_id):
        raise ValueError('apply requires exact digest, reason, request UUID and --yes')
    # The canonical CLI installation lock covers preview/apply/canary dispatch.
    receipt = install.directory / ('observer-upgrade-' + options.request_id + '.json')
    if receipt.exists() or receipt.is_symlink():
        raise ValueError('observer upgrade request already consumed; inspect private receipt')
    snapshot = observer_upgrade_snapshot(install, wrapper)
    if recovery_digest(snapshot) != options.snapshot_digest:
        raise ValueError('observer upgrade snapshot changed')
    payload = observer_payload()
    if tree_digest(observer_files(payload)) != snapshot['target_digest']:
        raise ValueError('observer target changed')
    services = ['execution-dispatch-worker'] if dict(snapshot['services'])['execution-dispatch-worker'] == 'running' else []
    audit = {'phase': 'consumed', 'request_id': options.request_id, 'reason': options.reason,
             'actor': 'root', 'snapshot_digest': options.snapshot_digest, 'target_digest': snapshot['target_digest'],
             'source_digest': snapshot['native']['links']['posthog']['digest'],
             'prior_services': snapshot['services'],
             'paused_execution': ['bridge-executor-worker'], 'timestamp': datetime.now(timezone.utc).isoformat()}
    write_observer_receipt(receipt, audit)
    stage = 'dispatcher_stop'
    try:
        if services:
            observer_compose(install, 'stop', '--timeout', '60', *services)
        stage = 'gateway_stop'
        systemctl('stop', SERVICE)
        if gateway_status()['ActiveState'] not in {'inactive', 'failed'}:
            raise ValueError('gateway did not stop')
        current = dict((row['Service'], row.get('State')) for row in observer_service_rows(install))
        if any(current.get(name) != 'exited' for name in ('execution-dispatch-worker', 'bridge-executor-worker')):
            raise ValueError('execution services did not stop')
        stage = 'observer_upgrade'
        with sqlite3.connect(bridge_database(install).as_uri() + '?mode=rw', uri=True, timeout=5) as connection:
            connection.execute('BEGIN IMMEDIATE')
            assert_quiescent(connection)
            if observer_host_digest(install) != snapshot['host_inputs'] or tree_digest(observer_files(observer_payload())) != snapshot['target_digest']:
                raise ValueError('observer upgrade inputs changed')
            result = run_native(wrapper, 'observer-upgrade', {**payload, 'snapshot_digest': recovery_digest(snapshot['native'])})
            expected = copy.deepcopy(snapshot['native'])
            expected['links']['posthog'] = {'target': str(Path(expected['links']['posthog']['target']).parent / snapshot['target_digest']),
                                            'digest': snapshot['target_digest']}
            if result != expected:
                raise ValueError('observer upgrade result differs')
            connection.rollback()
        stage = 'gateway_restore'
        systemctl('restart', SERVICE)
        wait_observer_health(install, sorted(current.items()), snapshot['host_inputs'])
        stage = 'dispatcher_restore'
        if services:
            observer_compose(install, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', *services)
        stage = 'restored_health'
        wait_observer_health(install, snapshot['services'], snapshot['host_inputs'])
        audit['phase'] = 'complete'
        write_observer_receipt(receipt, audit)
        return {'upgraded': True, 'target_digest': snapshot['target_digest'], 'receipt': str(receipt),
                'paused_execution': ['bridge-executor-worker']}
    except BaseException as error:
        # Never retry a consumed mutation or restart a previously paused executor.
        audit.update(phase='requires_operator', stage=stage)
        if isinstance(error, ObserverHealthFailure):
            audit['failed_predicate'] = error.predicate
        errors = []
        for name, action in (('dispatcher_stop', lambda: observer_compose(install, 'stop', '--timeout', '60', *services) if services else None),
                             ('gateway_stop', lambda: systemctl('stop', SERVICE))):
            try:
                action()
            except BaseException:
                errors.append(name)
        audit['stop_errors'] = errors
        write_observer_receipt(receipt, audit)
        raise ValueError('observer upgrade failed; inspect private receipt; execution remains paused') from None


def dispatch(install, config, wrapper, args):
    if args and args[0] == 'recover-observer':
        return recover_observer(install, wrapper, args[1:])
    if args and args[0] == 'upgrade-observer':
        return observer_upgrade(install, wrapper, args[1:])
    if args == ['canary', 'github']:
        # Require the same pause, idle and routing checks as an observer upgrade.
        checked(DROPIN)
        if DROPIN.read_bytes() != PROFILE_DROPIN or [row.get('State') for row in install.service_rows() if row['Service'] == 'bridge-executor-worker'] != ['exited']:
            raise ValueError('GitHub canary requires active profile and paused execution')
        with sqlite3.connect(bridge_database(install).as_uri() + '?mode=ro', uri=True, timeout=5) as connection:
            assert_quiescent(connection)
        identifier = str(uuid.uuid4())
        digest = tree_digest(observer_files(observer_payload()))
        result = run_native(wrapper, 'github-canary', {'id': identifier, 'observer_digest': digest})
        write_observer_receipt(install.directory / ('github-canary-' + identifier + '.json'), result)
        return result
    if args and args[0] == 'recover-inspection':
        return recover_inspection(install, wrapper, args[1:])
    if args == ['status']:
        result = run_native(wrapper, 'status', {})
        provisioning = json.loads(checked(PROVISION).read_text())
        result['reviewer_routed'] = provisioning.get('auth_root') == str(ROOT / 'profiles' / ROLES['reviewer'])
        environment = subprocess.run(['systemctl', 'show', '--property=Environment', '--value', SERVICE],
                                     capture_output=True, text=True, check=True, timeout=15).stdout
        result['gateway_executor_profile_selected'] = 'DRIFTWARD_HERMES_PROFILE=factory-executor' in environment.split()
        result['gateway_service'] = gateway_status()
        result['gateway_socket_listening'] = gateway_socket_listening()
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
