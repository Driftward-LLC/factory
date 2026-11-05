"""Reviewed, installation-local execution using the existing native profiles.

Cancellation remains conservative: unknown outcomes and durable admission holds
require operator recovery, never automatic reuse or claims of settled effects.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import subprocess

import bootstrap

HERE = Path(__file__).resolve().parent
FILES = ('compose.local-executor.yml', 'compose.local-execution.yml', 'local-runtime-binding.py', 'local_execution.py')
CONFIG = ('executor-plugin.json', 'factory-hermes-execution.env', 'reviewer/provisioning.json', 'reviewer/review-code.md', 'reviewer/verify-app.md')
IDENTITY = {'FACTORY_HERMES_ALLOWED_REPOS', 'FACTORY_HERMES_EXPECTED_GITHUB_LOGIN',
            'FACTORY_HERMES_EXPECTED_GIT_AUTHOR_NAME', 'FACTORY_HERMES_EXPECTED_GIT_AUTHOR_EMAIL'}
# This runs within the installation's dedicated volume, never in shared Hermes.
IDLE = '''import json,pathlib,sqlite3
from hatchet_hermes_bridge.store import connect_readonly,list_active_factory_attempts
with connect_readonly() as db:
 if list_active_factory_attempts(db): raise SystemExit(73)
print('idle')
'''
NATIVE_IDLE = '''import pathlib,json,sys,hashlib,stat
raw=sys.stdin.read(65537)
if len(raw)>65536: raise SystemExit(73)
retired=json.loads(raw)
if not isinstance(retired,list) or len(retired)>64: raise SystemExit(73)
by_job={item['job_id']:item for item in retired}
if len(by_job)!=len(retired): raise SystemExit(73)
def document(path):
 info=path.lstat()
 if not stat.S_ISREG(info.st_mode) or info.st_size>16384: raise SystemExit(73)
 return json.loads(path.read_text())
def resolved(item):
 evidence=by_job.get(item.name)
 if not evidence: return False
 intent=document(item/'intent.json'); runtime=document(item/'runtime.json')
 lease=intent['lease']
 return (intent.get('fingerprint_version')==2 and evidence.get('fingerprint_version')==2 and intent['fingerprint']==evidence['fingerprint']
  and runtime==evidence['runtime'] and lease['lease_id']==evidence['lease_id']
  and lease['owner']==evidence['owner'] and lease['epoch']==evidence['epoch']
  and hashlib.sha256(lease['fence_token'].encode()).hexdigest()==evidence['fence_sha256'])
root=pathlib.Path('/opt/data/factory-invocations')
if root.is_symlink(): raise SystemExit(73)
if root.exists():
 for item in root.iterdir():
  if item.is_symlink(): raise SystemExit(73)
  if item.name=='local-admission':
   if any(item.iterdir()): raise SystemExit(73)
  elif item.is_dir():
   result=item/'result.json'
   if result.is_symlink(): raise SystemExit(73)
   if not result.is_file() and not resolved(item): raise SystemExit(73)
print('idle')
'''


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def hashes(installation):
    names = (*CONFIG, '.env', *('config/' + name for name in bootstrap.ORGANIZATION_DEFAULTS),
             *('env/' + name for name in bootstrap.ENV_FILES), 'env/hatchet-client.env')
    return {name: hashlib.sha256(bootstrap._read_private(installation.directory / name).encode()).hexdigest() for name in names}


def policies(installation):
    root = installation.directory
    config = bootstrap.configuration(root / 'config')
    organization = json.loads(config['organization.json'])
    if not organization.get('linear') or organization['github'].get('enabled') is not True:
        raise ValueError('local execution requires configured Linear and GitHub integration')
    routes = [route for route in json.loads(config['routing-registry.json'])['routes'] if route['execution_enabled']]
    if len(routes) != 1:
        raise ValueError('first local activation requires exactly one enabled project route')
    route = routes[0]
    repository = route['repo']
    identity = bootstrap.read_env(root / 'factory-hermes-execution.env')
    if set(identity) != IDENTITY or identity['FACTORY_HERMES_ALLOWED_REPOS'] != repository or any(not value for value in identity.values()):
        raise ValueError('local execution identity must contain only explicit public identity for the routed repository')
    verifier = json.loads(config['verifier-policy.json'])
    matches = [policy for policy in verifier['policies'] if policy['repo'] == repository]
    review = json.loads(config['independent-review-policy.json'])
    if len(matches) != 1 or review['enabled'] is not True or review['repositories'] != [repository]:
        raise ValueError('routed repository requires verifier and independent reviewer enrollment')
    reviewer = bootstrap.checked_path(root / 'reviewer')
    if not reviewer.is_dir() or reviewer.stat().st_mode & 0o077 or {path.name for path in reviewer.iterdir()} != {'provisioning.json', 'review-code.md', 'verify-app.md'}:
        raise ValueError('reviewer directory requires exactly the three private public assets')
    provisioning = json.loads(bootstrap._read_private(root / 'reviewer/provisioning.json'))
    if (provisioning.get('schema') != 'factory-hermes-source-reviewer-v1'
            or provisioning.get('auth_root') != '/opt/data/profiles/factory-independent-reviewer'
            or provisioning.get('repositories') != {repository: {'base_ref': matches[0]['expected_base']}}
            or provisioning.get('policy_digest') != digest(review)):
        raise ValueError('source reviewer provisioning differs from the enrolled policy')
    for name, key in [('review-code', 'review_skill_digest'), ('verify-app', 'verify_skill_digest')]:
        if provisioning.get(key) != hashlib.sha256(bootstrap._read_private(root / f'reviewer/{name}.md').encode()).hexdigest():
            raise ValueError('reviewer skill digest mismatch')
    plugin_manifest(installation)
    return route


def plugin_manifest(installation):
    # Same public metadata schema as the native runtime probe. No plugin code or
    # archive is included in the release or passed to credential-minimal workers.
    path = installation.directory/'executor-plugin.json'
    value = json.loads(bootstrap._read_private(path))
    if (not isinstance(value,dict) or set(value)!={'schema','namespace','version','source_commit','archive_sha256','tree_sha256','repository'}
            or value.get('schema')!='factory-executor-plugin-v1' or not re.fullmatch('[a-z][a-z0-9-]{0,99}',str(value.get('namespace','')))
            or not re.fullmatch('[a-f0-9]{40}',str(value.get('source_commit','')))
            or any(not re.fullmatch('[a-f0-9]{64}',str(value.get(key,''))) for key in ('archive_sha256','tree_sha256'))):
        raise ValueError('invalid reviewed native plugin manifest')
    return value


def validate(installation):
    receipt = installation.metadata.get('local_execution')
    if receipt is None:
        return None
    if (not isinstance(receipt, dict) or set(receipt) != {'schema', 'approved_sha256', 'configuration', 'deployment', 'route', 'optional_services'}
            or receipt['schema'] != 1 or not re.fullmatch('[a-f0-9]{64}', str(receipt['approved_sha256']))
            or not isinstance(receipt['optional_services'], list)
            or len(set(receipt['optional_services'])) != len(receipt['optional_services'])
            or any(name not in ('linear-webhook-proxy', 'factory-linear-token-refresh') for name in receipt['optional_services'])
            or receipt['configuration'] != hashes(installation)
            or receipt['deployment'] != {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest() for name in FILES}
            or receipt['route'] != policies(installation)):
        raise ValueError('local execution configuration changed; stop and obtain a new reviewed preview')
    return receipt


def attach(installation):
    receipt = installation.metadata.get('local_execution')
    if receipt:
        if (not isinstance(receipt, dict) or not isinstance(receipt.get('route'), dict)
                or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', str(receipt['route'].get('repo', '')))):
            raise ValueError('invalid local execution receipt')
        optional = receipt.get('optional_services', [])
        if not isinstance(optional, list) or any(name not in ('linear-webhook-proxy', 'factory-linear-token-refresh') for name in optional):
            raise ValueError('unsupported local optional service')
        if optional:
            installation.args.extend(('--profile', 'linear'))
            installation.services = (*installation.services, *optional)
        installation.env['FACTORY_EXECUTOR_SKILL_NAMESPACE'] = plugin_manifest(installation)['namespace']
        installation.env['FACTORY_LOCAL_EXECUTION_REPOSITORY'] = receipt['route']['repo']
        installation.args.extend(arg for name in FILES[:2] for arg in ('-f', str(HERE / name)))


def runtime(installation):
    container = installation.compose('ps', '-q', 'factory-hermes', private=True).strip()
    if not re.fullmatch('[a-f0-9]{12,64}', container):
        raise ValueError('exactly one running installation-local Hermes container is required')
    spec = importlib.util.spec_from_file_location('factory_local_runtime_binding', HERE / 'local-runtime-binding.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    observed = module.observe(container, installation.metadata['installation_id'])
    import portable_state
    expected = portable_state._docker(['image', 'inspect', '--format', '{{.Id}}', installation.values['FACTORY_IMAGE']], installation.env)
    if observed['runtime']['image_digest'] != expected:
        raise ValueError('running Hermes image differs from the selected image')
    return observed


def retirement_descriptors(installation, *, stopped=False):
    command = (['run', '--rm', '--no-deps', '--entrypoint', '/opt/factory/bin/python', 'bridge'] if stopped
               else ['exec', '-T', 'bridge', '/opt/factory/bin/python'])
    raw = installation.compose(*command, '-m', 'hatchet_hermes_bridge.retired_execution', private=True)
    if len(raw)>65536: raise ValueError('retirement evidence exceeds budget')
    value = json.loads(raw)
    if not isinstance(value,list) or len(value)>64: raise ValueError('invalid retirement evidence')
    import portable_state
    # A successful full inventory distinguishes removal from inspect failure.
    existing = portable_state._docker(['ps', '-aq', '--no-trunc'], installation.env).splitlines()
    if any(not re.fullmatch('[a-f0-9]{64}', identity) for identity in existing):
        raise ValueError('invalid Docker identity inventory')
    for item in value:
        identity = item.get('runtime',{}).get('instance_id','')
        if not re.fullmatch('[a-f0-9]{64}',identity) or identity in existing:
            raise ValueError('resolved original runtime is not permanently removed')
    return value


def assert_native_idle(installation, *, stopped=False):
    evidence = retirement_descriptors(installation, stopped=stopped)
    command = (['run', '--rm', '--no-deps', '--entrypoint', 'python3', 'factory-hermes'] if stopped
               else ['exec', '-T', '--user', '10000', 'factory-hermes', 'python3'])
    result = subprocess.run([*installation.args,*command,'-c',NATIVE_IDLE], input=json.dumps(evidence),
                            text=True, capture_output=True, env=installation.env, timeout=30)
    if result.returncode or result.stdout.strip()!='idle':
        raise ValueError('native execution requires operator recovery; private output withheld')


def assert_idle(installation):
    if installation.compose('exec', '-T', 'bridge', 'python3', '-c', IDLE, private=True).strip() != 'idle':
        raise ValueError('bridge has unresolved execution')
    assert_native_idle(installation)


def assert_executor_ready(installation, *, candidate=False):
    pin=plugin_manifest(installation)
    arguments=['exec','-T','--user','10000','factory-hermes','env','HERMES_HOME=/opt/data/profiles/factory-executor']
    if candidate:
        arguments += ['FACTORY_HERMES_PROFILE=factory-executor','FACTORY_HERMES_SKILL_NAMESPACE='+pin['namespace']]
    arguments += ['/opt/hermes/.venv/bin/python','/app/deploy/executor-readiness.py']
    if candidate: arguments += ['--manifest-json',json.dumps(pin,separators=(',',':'))]
    value=json.loads(installation.compose(*arguments,private=True))
    if not isinstance(value,dict) or value.get('profile')!='factory-executor' or value.get('namespace')!=pin['namespace'] or value.get('model_called') is not False or value.get('cli_arguments_checked') is not True or value.get('source_commit')!=pin['source_commit']:
        raise ValueError('native executor plugin readiness failed')
    return value


def preview(installation):
    installation.assert_owned_resources()
    installation.assert_config()
    restored = installation.metadata.get('local_execution_restored')
    if restored:
        import portable_state
        if (not isinstance(restored, dict) or set(restored) != {'source_installation', 'optional_services'}
                or not re.fullmatch('[a-f0-9-]{36}', str(restored['source_installation']))):
            raise ValueError('invalid restored execution provenance')
        if portable_state._docker(['ps', '--filter', 'label=io.factory.installation=' + restored['source_installation'], '--format', '{{.ID}}'], installation.env):
            raise ValueError('original installation must be stopped before restored execution activation')
    route = policies(installation)
    skill_readiness = assert_executor_ready(installation, candidate=not bool(installation.metadata.get("local_execution")))
    if not re.fullmatch(r'ghcr\.io/driftward-llc/factory@sha256:[a-f0-9]{64}', installation.values['FACTORY_IMAGE']):
        raise ValueError('local activation requires a digest-pinned Factory release')
    assert_idle(installation)
    rows = installation.compose('ps', '--all', '--format', 'json', private=True)
    rows = json.loads(rows) if rows.strip().startswith('[') else [json.loads(line) for line in rows.splitlines() if line.strip()]
    if any(row.get('Service') == 'factory-lifecycle-writer' and row.get('State') == 'running' for row in rows):
        raise ValueError('existing Linear lifecycle writer requires separate reviewed enrollment')
    optional = sorted(set(row['Service'] for row in rows if row.get('State') == 'running' and row.get('Service') in ('linear-webhook-proxy', 'factory-linear-token-refresh')) | set(restored['optional_services'] if restored else []))
    if any(name not in ('linear-webhook-proxy', 'factory-linear-token-refresh') for name in optional):
        raise ValueError('unsupported restored optional service')
    value = {'schema': 1, 'skill_readiness': skill_readiness, 'installation_id': installation.metadata['installation_id'], 'project': installation.project,
             'image': installation.values['FACTORY_IMAGE'], 'runtime': runtime(installation), 'route': route,
             'configuration': hashes(installation),
             'deployment': {name: hashlib.sha256((HERE / name).read_bytes()).hexdigest() for name in FILES},
             'cancellation': 'unknown outcomes retain admission hold; operator recovery required',
             'services': sorted(set(installation.services) | set(optional)), 'optional_services': optional}
    return value


def atomic(path, value):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.execution-')
    try:
        with os.fdopen(fd, 'w') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(value, stream, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def activate(installation, approved=None):
    value = preview(installation)
    fingerprint = digest(value)
    if approved is None:
        print(json.dumps({**value, 'preview_sha256': fingerprint}, sort_keys=True, indent=2))
        return
    if approved != fingerprint:
        raise ValueError('activation preview changed; review the current preview before applying')
    # Stop writers before rechecking the native/bridge boundary. A failed apply
    # leaves the installation stopped or admission closed, never silently dry-run.
    installation.compose('stop', *(service for service in value['services'] if service not in ('postgres', 'hatchet-lite', 'bridge', 'factory-hermes')))
    assert_idle(installation)
    for relative, owner, mode in [('gateway', 10000, 0o700), ('runtime', 0, 0o755)]:
        path = bootstrap.checked_path(installation.directory / relative)
        path.mkdir(mode=mode, exist_ok=True)
        info = path.stat()
        if not path.is_dir() or info.st_mode & 0o022:
            raise ValueError('unsafe local gateway runtime directory')
        os.chown(path, owner, owner)
        path.chmod(mode)
    os.chown(installation.directory/'executor-plugin.json',10000,10000)
    for relative in ('config', 'reviewer'):
        path = bootstrap.checked_path(installation.directory / relative)
        if not path.is_dir() or path.stat().st_mode & 0o077:
            raise ValueError('public execution assets must remain private')
        for asset in path.iterdir():
            bootstrap._read_private(asset)
            os.chown(asset, 10000, 10000)
        os.chown(path, 10000, 10000)
    receipt = {key: value[key] for key in ('schema', 'configuration', 'deployment', 'route', 'optional_services')}
    receipt['approved_sha256'] = fingerprint
    metadata = {**installation.metadata, 'local_execution': receipt}
    atomic(installation.directory / bootstrap.MARKER, metadata)
    # Reconstruct through the wrapper so every subsequent lifecycle operation
    # follows exactly the same reviewed Compose path.
    resumed = type(installation)(installation.directory)
    start(resumed)


def start(installation):
    if validate(installation) is None:
        raise ValueError('local startup requires reviewed activation')
    installation.assert_owned_resources()
    installation.assert_config()
    # Read-only one-shot mounts support readiness even while containers stopped.
    # No executor tools or provider request is run by this check.
    if installation.compose('run', '--rm', '--no-deps', '--entrypoint', 'python3', 'bridge', '-c', IDLE, private=True).strip() != 'idle':
        raise ValueError('local execution requires recovery before startup')
    assert_native_idle(installation, stopped=True)
    installation.compose('up', '-d', '--no-build', '--wait', '--wait-timeout', '180', 'postgres', 'hatchet-lite', 'bridge', 'factory-hermes')
    assert_executor_ready(installation)
    binding = runtime(installation)
    # Public identity must be readable by uid 10000, credentials never enter it.
    path = installation.directory / 'runtime/runtime-binding.json'
    atomic(path, binding)
    path.chmod(0o644)
    installation.compose('exec', '-T', '--user', '10000', 'factory-hermes', 'python3', '-c',
        "import socket,time; deadline=time.monotonic()+30\nwhile True:\n try:\n  s=socket.socket(socket.AF_UNIX); s.connect('/run/factory-gateway/hermes.sock'); s.close(); break\n except OSError:\n  s.close()\n  if time.monotonic()>deadline: raise SystemExit(73)\n  time.sleep(.1)", private=True)
    installation.compose('up', '-d', '--no-build', '--wait', '--wait-timeout', '180', *installation.services)
    installation.status()
