"""Canonical backend only: disposable image-pinned native-owner validation."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

ENV = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8'}
ASSET_SHA256 = {
    'main.py': '3ed4685bb587eac0a33bae2af806244095419638ccb450191c4decfefa89c3fe',
    'owner.py': 'cce7c551a2e45b63836e2e764a5cab072ac3f3dc4520b4ca80a660442343c4b0',
    'transport.py': 'aaf61cbd45be7ebc7d9c4977a70838ae04f3fbd5c3ca631c213f67acef4df7c5',
    'driver.py': '3c99d2711994f7ff8405a696f40f91d3c3aad2fb14c98c816200034ebf754792',
    'observer.py': '89c0654d39bdc8fe3bd1c7583908e33dc1faf884260722601469c0866f523788',
}


def public_asset(runtime, path, base):
    path = runtime.checked(path)
    for part in (path, *path.relative_to(base).parents):
        selected = part if part.is_absolute() else base / part
        info = selected.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('unsafe reviewed public asset')
    if path.stat().st_size > 131072:
        raise ValueError('oversize public asset')
    return path.read_text()


def docker(args, *, data=None, timeout=15):
    # Files avoid unbounded captured output. Docker/client diagnostics are never
    # returned, including inherited image environment values and native errors.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        result = subprocess.run(['docker', *args], input=data, stdout=output, stderr=errors, env=ENV, timeout=timeout)
        output.seek(0)
        content = output.read(65537)
        if result.returncode or len(content) > 65536:
            raise ValueError('canary Docker operation failed')
        return content.decode('utf-8')


def image_identity(identity):
    if not isinstance(identity, str) or not re.fullmatch(r'[0-9a-f]{12,64}', identity):
        raise ValueError('runtime identity unavailable')
    rows = json.loads(docker(['inspect', '--type=container', identity]))
    if len(rows) != 1:
        raise ValueError('runtime identity ambiguous')
    row = rows[0]
    image = row.get('Image', '')
    if (not row.get('Id', '').startswith(identity) or row.get('Config', {}).get('Hostname') != identity
            or row.get('State', {}).get('Running') is not True or not re.fullmatch(r'sha256:[0-9a-f]{64}', image)):
        raise ValueError('runtime identity mismatch')
    images = json.loads(docker(['image', 'inspect', image]))
    if len(images) != 1 or images[0].get('Id') != image:
        raise ValueError('image identity mismatch')
    config = images[0].get('Config', {})
    if config.get('Volumes') or config.get('OnBuild'):
        raise ValueError('image has implicit volumes or build hooks')
    for item in config.get('Env') or []:
        if not isinstance(item, str) or '=' not in item or len(item) > 4096:
            raise ValueError('unsafe image environment')
        key = item.split('=', 1)[0]
        if re.search(r'KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|COOKIE', key, re.I):
            raise ValueError('image contains credential environment')
    for field in ('Entrypoint', 'Cmd'):
        if any(not isinstance(item, str) or len(item) > 4096 for item in config.get(field) or []):
            raise ValueError('unsafe image startup configuration')
    return image


def create_arguments(name, image, driver):
    if not re.fullmatch(r'factory-native-canary-[0-9a-f]{32}', name) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        raise ValueError('invalid disposable identity')
    return ['create', '--name', name, '--label', 'com.driftward.factory.validation=native-owner', '-i',
            '--network=none', '--read-only', '--user=10000:10000', '--cap-drop=ALL',
            '--security-opt=no-new-privileges:true', '--pids-limit=64', '--memory=768m', '--cpus=1',
            '--ulimit', 'fsize=1048576:1048576', '--ulimit', 'nofile=256:256',
            '--tmpfs', '/tmp:rw,nosuid,nodev,size=128m,mode=1777', '--workdir=/tmp',
            '--entrypoint=/usr/bin/env', image, '-i', 'PATH=/opt/hermes/.venv/bin:/usr/bin:/bin',
            'HOME=/tmp', 'LANG=C.UTF-8', '/opt/hermes/.venv/bin/python', '-u', '-c', driver]


def run(runtime, install, wrapper):
    if runtime.os.geteuid() != 0:
        raise ValueError('native canary requires root canonical CLI')
    identifier = uuid.uuid4()
    job = str(uuid.uuid4())
    attempt = str(identifier)
    binding = {'invocation_id': job + ':' + attempt + ':1', 'epoch': 1,
               'fingerprint': hashlib.sha256(('native-canary:' + attempt).encode()).hexdigest()}
    base = Path(runtime.__file__).parent
    paths = {'main.py': base / 'experimental-native-executor/main.py',
             'owner.py': base / 'experimental-native-executor/owner.py',
             'transport.py': base / 'native-canary/transport.py', 'driver.py': base / 'native-canary/driver.py',
             'observer.py': base / 'factory-observers/posthog/__init__.py'}
    assets = {}
    for name, path in paths.items():
        assets[name] = public_asset(runtime, path, base)
    digests = {name: hashlib.sha256(value.encode()).hexdigest() for name, value in assets.items()}
    if digests != ASSET_SHA256:
        raise ValueError('reviewed public asset digest mismatch')
    identity = runtime.run_native(wrapper, 'native-canary-identity', {})
    image = image_identity(identity.get('hostname'))
    name = 'factory-native-canary-' + identifier.hex
    receipt = {'canary_id': attempt, 'job_id': job, 'attempt_id': attempt, 'image_id': image,
               'asset_sha256': digests, 'validation_only': True, 'gateway_auth_tested': False,
               'production_route_activated': False, 'posthog_delivery': 'not_attempted',
               'status': 'unknown', 'container_removed': False, 'disposable_name': name}
    created = False
    try:
        container = docker(create_arguments(name, image, assets['driver.py'])).strip()
        if not re.fullmatch(r'[0-9a-f]{64}', container):
            raise ValueError('invalid created container identity')
        created = True
        payload = {'assets': {key: assets[key] for key in ('main.py', 'owner.py', 'transport.py')}, 'binding': binding}
        result = json.loads(docker(['start', '--attach', '--interactive', container],
                                  data=(json.dumps(payload) + '\n').encode(), timeout=85))
        observations = result.pop('observations', None)
        if (result.get('marker_and_exit_zero') is not True or result.get('owner_exit_observed') is not True
                or not isinstance(observations, list) or len(observations) != 3):
            raise ValueError('native canary proof incomplete')
        # The container is credential-free and cannot ingest. Only this fixed
        # exporter in the existing runtime sees its configured ingestion key.
        delivery = runtime.run_native(wrapper, 'native-canary-export', {
            'observer': assets['observer.py'], 'observations': observations, 'binding': binding,
            'job_id': job, 'attempt_id': attempt})
        receipt.update(result)
        receipt.update({'status': 'passed', 'telemetry_relay': delivery,
                        'posthog_delivery': 'server_verification_pending'})
    except Exception:
        receipt['reason'] = 'isolated_canary_failed_or_unknown'
    finally:
        # Never remove a name on an ambiguous create failure/collision. Only the
        # exact returned container ID is ours; retain the name for reconciliation.
        if created:
            try:
                docker(['rm', '--force', container])
                receipt['container_removed'] = True
            except Exception:
                receipt['cleanup'] = 'unknown'
        else:
            receipt['cleanup'] = 'creation_or_cleanup_unknown'
        runtime.write_observer_receipt(install.directory / ('native-canary-' + attempt + '.json'), receipt)
    return receipt
