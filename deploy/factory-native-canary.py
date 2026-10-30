"""Canonical backend only: disposable image-pinned native-owner validation."""
import hashlib
import json
import os
from pathlib import Path
import re
import select
import subprocess
import tempfile
import time
import uuid

ENV = {'PATH': '/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8'}
TMPFS = {'/tmp': 'rw,nosuid,nodev,size=128m,mode=1777',
         '/opt/data': 'rw,nosuid,nodev,noexec,size=16m,mode=0700,uid=10000,gid=10000'}
ASSET_SHA256 = {
    'main.py': '3cebd326655eeedf6d8649e6b9af228cdc56c64ffbf6cfe114f5ffccc0a418be',
    'owner.py': 'b5220ca78d658c0ca5c0ede84535cc87184149e5f6541dd5958ab99216ee84ab',
    'transport.py': 'b1c4879bd9da207067e77d19e41b403c92b31cb661230ccd9d3c14f2764f10e9',
    'driver.py': 'f6be1307e725ee64dd80c6c87045432a5caf04f3db249fa23c2b8955539ed526',
    'observer.py': '89c0654d39bdc8fe3bd1c7583908e33dc1faf884260722601469c0866f523788',
}
REFUSAL_CODES = {
    'public_assets': {'unsafe_asset_permissions', 'asset_oversize', 'asset_digest_mismatch'},
    'runtime_identity': {'identity_invalid'},
    'container_inspection': {'inspection_schema_invalid', 'inspection_unavailable', 'identity_mismatch'},
    'image_inspection': {'inspection_schema_invalid', 'inspection_unavailable', 'identity_mismatch',
                         'implicit_volumes_or_build_hooks', 'environment_schema_invalid',
                         'credential_environment_refused', 'startup_schema_invalid'},
}


class PreflightRefusal(ValueError):
    """Only fixed codes, never exception text, cross the canonical boundary."""
    def __init__(self, phase, reason, *, image_features=None):
        self.phase, self.reason = phase, reason
        self.image_features = image_features
        super().__init__('native canary preflight refused')


def inspect_json(args, phase):
    try:
        value = json.loads(docker(args))
        if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
            raise PreflightRefusal(phase, 'inspection_schema_invalid')
        return value
    except PreflightRefusal:
        raise
    except Exception:
        raise PreflightRefusal(phase, 'inspection_unavailable') from None


def public_asset(runtime, path, base):
    path = runtime.checked(path)
    for part in (path, *path.relative_to(base).parents):
        selected = part if part.is_absolute() else base / part
        info = selected.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise PreflightRefusal('public_assets', 'unsafe_asset_permissions')
    if path.stat().st_size > 131072:
        raise PreflightRefusal('public_assets', 'asset_oversize')
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
        raise PreflightRefusal('runtime_identity', 'identity_invalid')
    rows = inspect_json(['inspect', '--type=container', identity], 'container_inspection')
    row = rows[0]
    image = row.get('Image', '')
    if (not row.get('Id', '').startswith(identity) or row.get('Config', {}).get('Hostname') != identity
            or row.get('State', {}).get('Running') is not True or not re.fullmatch(r'sha256:[0-9a-f]{64}', image)):
        raise PreflightRefusal('container_inspection', 'identity_mismatch')
    images = inspect_json(['image', 'inspect', image], 'image_inspection')
    if images[0].get('Id') != image:
        raise PreflightRefusal('image_inspection', 'identity_mismatch')
    config = images[0].get('Config', {})
    volumes = config.get('Volumes')
    hooks = config.get('OnBuild')
    if (volumes is not None and (not isinstance(volumes, dict) or set(volumes) - {'/opt/data'})) or (
            hooks is not None and (not isinstance(hooks, list) or any(not isinstance(item, str) for item in hooks))):
        volumes = config.get('Volumes')
        hooks = config.get('OnBuild')
        # Only bounded destination names cross the receipt boundary; image
        # environment, build commands and healthcheck commands stay private.
        destinations = list(volumes) if isinstance(volumes, dict) else []
        features = {
            'volume_destinations': [path if isinstance(path, str) and
                                    re.fullmatch(r'/[A-Za-z0-9_./-]{0,255}', path)
                                    else '[redacted]' for path in destinations[:16]],
            'volume_destination_count': len(destinations),
            'volume_destinations_truncated': len(destinations) > 16,
            'onbuild_count': len(hooks) if isinstance(hooks, list) else None,
            'healthcheck_present': bool(config.get('Healthcheck')),
        }
        raise PreflightRefusal('image_inspection', 'implicit_volumes_or_build_hooks', image_features=features)
    for item in config.get('Env') or []:
        if not isinstance(item, str) or '=' not in item or len(item) > 4096:
            raise PreflightRefusal('image_inspection', 'environment_schema_invalid')
        key = item.split('=', 1)[0]
        if re.search(r'KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|COOKIE', key, re.I):
            raise PreflightRefusal('image_inspection', 'credential_environment_refused')
    for field in ('Entrypoint', 'Cmd'):
        if any(not isinstance(item, str) or len(item) > 4096 for item in config.get(field) or []):
            raise PreflightRefusal('image_inspection', 'startup_schema_invalid')
    return image


def create_arguments(name, image, driver):
    if not re.fullmatch(r'factory-native-canary-[0-9a-f]{32}', name) or not re.fullmatch(r'sha256:[0-9a-f]{64}', image):
        raise ValueError('invalid disposable identity')
    return ['create', '--name', name, '--label', 'com.driftward.factory.validation=native-owner', '-i', '--restart=no',
            '--network=none', '--read-only', '--no-healthcheck', '--user=10000:10000', '--cap-drop=ALL',
            '--security-opt=no-new-privileges:true', '--pids-limit=64', '--memory=768m', '--cpus=1',
            '--ulimit', 'fsize=1048576:1048576', '--ulimit', 'nofile=256:256',
            '--tmpfs', '/tmp:' + TMPFS['/tmp'], '--tmpfs', '/opt/data:' + TMPFS['/opt/data'], '--workdir=/tmp',
            '--entrypoint=/usr/bin/env', image, '-i', 'PATH=/opt/hermes/.venv/bin:/usr/bin:/bin',
            'HOME=/tmp', 'LANG=C.UTF-8', '/opt/hermes/.venv/bin/python', '-u', '-c', driver]


def verify_created(container, name, image):
    row = inspect_json(['inspect', '--type=container', container], 'container_inspection')[0]
    host, config = row.get('HostConfig', {}), row.get('Config', {})
    mounts = row.get('Mounts')
    if (row.get('Id') != container or row.get('Name') != '/' + name or row.get('Image') != image
            or row.get('State', {}).get('Status') != 'created' or row.get('State', {}).get('Running') is not False
            or config.get('User') != '10000:10000' or config.get('Healthcheck', {}).get('Test') != ['NONE']
            or host.get('NetworkMode') != 'none' or host.get('ReadonlyRootfs') is not True
            or host.get('Privileged') is not False or host.get('CapAdd')
            or host.get('PidMode') != '' or host.get('Init') not in (None, False)
            or host.get('RestartPolicy', {}).get('Name') != 'no'
            or host.get('CapDrop') != ['ALL'] or host.get('SecurityOpt') != ['no-new-privileges:true']
            or host.get('Binds') or host.get('VolumesFrom') or host.get('Devices') or host.get('Mounts')
            or host.get('Tmpfs') != TMPFS or not isinstance(mounts, list)
            or any(not isinstance(mount, dict) or mount.get('Type') != 'tmpfs'
                   or mount.get('Destination') not in TMPFS or mount.get('Source') for mount in mounts)
            or len({mount.get('Destination') for mount in mounts}) != len(mounts)):
        raise ValueError('disposable container isolation mismatch')


def send_frame(process, value, deadline):
    data = memoryview((json.dumps(value) + '\n').encode())
    if len(data) > 524288:
        raise ValueError('fixture_payload_oversize')
    os.set_blocking(process.stdin.fileno(), False)
    while data:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([], [process.stdin], [], remaining)[1]:
            raise ValueError('fixture_write_timeout')
        try:
            data = data[os.write(process.stdin.fileno(), data):]
        except BlockingIOError:
            continue


def read_frame(process, deadline):
    data = bytearray()
    while len(data) <= 65536:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
            raise ValueError('fixture_read_timeout')
        byte = os.read(process.stdout.fileno(), 1)
        if not byte:
            raise ValueError('fixture_protocol_eof')
        data.extend(byte)
        if byte == b'\n':
            value = json.loads(data)
            if not isinstance(value, dict):
                raise ValueError('fixture_frame_invalid')
            return value
    raise ValueError('fixture_frame_oversize')


def running_container(container, image):
    row = inspect_json(['inspect', '--type=container', container], 'container_inspection')[0]
    if (row.get('Id') != container or row.get('Image') != image
            or row.get('State', {}).get('Running') is not True
            or row.get('HostConfig', {}).get('PidMode') != ''):
        raise ValueError('fixture_runtime_identity_changed')
    return row


def fixture_handles(container, image, expected):
    """Polling handles only. Docker's exact created ID is the kill authority."""
    row = running_container(container, image)
    init = row['State']['Pid']
    if type(init) is not int or init <= 1:
        raise ValueError('fixture_init_identity_invalid')
    namespace = os.stat(f'/proc/{init}/ns/pid')
    host_namespace = os.stat('/proc/self/ns/pid')
    if (namespace.st_dev, namespace.st_ino) == (host_namespace.st_dev, host_namespace.st_ino):
        raise ValueError('private_namespace_missing')
    rows = docker(['top', container, '-eo', 'pid']).splitlines()
    if not rows or rows[0].strip() != 'PID' or not 1 <= len(rows) - 1 <= 64:
        raise ValueError('fixture_process_inventory_invalid')
    handles, members = [], set()
    try:
        for value in rows[1:]:
            if not re.fullmatch(r'\s*[1-9][0-9]*\s*', value):
                raise ValueError('fixture_process_identity_invalid')
            pid = int(value)
            handle = os.pidfd_open(pid)
            handles.append(handle)
            selected = os.stat(f'/proc/{pid}/ns/pid')
            if (selected.st_dev, selected.st_ino) != (namespace.st_dev, namespace.st_ino):
                raise ValueError('fixture_namespace_mismatch')
            status = Path(f'/proc/{pid}/status').read_text()
            line = next(line for line in status.splitlines() if line.startswith('NSpid:'))
            native_pid = int(line.split()[-1])
            if pid == init and native_pid != 1:
                raise ValueError('supervisor_not_namespace_init')
            if native_pid in members:
                raise ValueError('fixture_process_inventory_duplicate')
            members.add(native_pid)
        if not ({1, *expected} <= members) or select.select(handles, [], [], 0)[0]:
            raise ValueError('fixture_processes_not_live')
        if running_container(container, image)['State']['Pid'] != init:
            raise ValueError('fixture_init_changed')
        return handles, (namespace.st_dev, namespace.st_ino)
    except BaseException:
        for handle in handles:
            os.close(handle)
        raise


def supervisor_loss_checks(image, assets, bound, receipt, persist):
    """Two disposable namespaces; never stop the shared Hermes runtime."""
    runs = []
    proof = {'status': 'unknown', 'completion': 'not_established', 'profile_reusable': False,
             'boundary': 'dedicated_container_pid_namespace', 'containers': []}
    receipt['supervisor_loss'] = proof
    try:
        for role in ('target', 'control'):
            name = 'factory-native-canary-' + uuid.uuid4().hex
            record = {'role': role, 'name': name, 'created': False, 'removed': False}
            proof['containers'].append(record)
            persist()  # Record creation intent before an ambiguous Docker call.
            container = docker(create_arguments(name, image, assets['driver.py'])).strip()
            if not re.fullmatch(r'[0-9a-f]{64}', container):
                raise ValueError('fixture_container_identity_invalid')
            record.update(id=container, created=True)
            run = {'record': record, 'handles': []}
            runs.append(run)
            persist()
            verify_created(container, name, image)
            process = subprocess.Popen(['docker', 'start', '--attach', '--interactive', container], env=ENV,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                       bufsize=0)
            run['process'] = process
            deadline = time.monotonic() + 25
            send_frame(process, {'assets': {key: assets[key] for key in ('main.py', 'owner.py', 'transport.py')},
                                 'binding': bound, 'supervisor_loss_fixture': True}, deadline)
            send_frame(process, {'binding': bound, 'backend': 'local',
                                 'enabled_tools': ['terminal', 'process_manage']}, deadline)
            registration = read_frame(process, deadline)
            ownership = registration.get('ownership', {})
            if registration.get('event') != 'registered' or any(ownership.get(k) != v for k, v in bound.items()):
                raise ValueError('fixture_registration_invalid')
            send_frame(process, {'start': bound}, deadline)
            if read_frame(process, deadline).get('event') != 'fixture_spawn_admitted':
                raise ValueError('fixture_admission_missing')
            ready = read_frame(process, deadline)
            background = ready.get('background', [])
            if (ready.get('event') != 'fixture_ready' or len(background) != 2
                    or [row.get('persistent') for row in background] != [False, True]):
                raise ValueError('fixture_readiness_missing')
            expected = [ownership.get('pid'), ready.get('foreground_pid'), ready.get('escaped_pid'),
                        *[row.get('pid') for row in background]]
            if any(type(pid) is not int or pid <= 1 for pid in expected) or len(set(expected)) != 5:
                raise ValueError('fixture_process_ids_invalid')
            run['handles'], run['namespace'] = fixture_handles(container, image, expected)
            record.update(ready=True, measured_processes=len(run['handles']), supervisor_is_pid1=True)
            persist()
        target, control = runs
        if target['namespace'] == control['namespace']:
            raise ValueError('fixture_namespaces_shared')
        # All handles must still be live immediately before the exact-ID kill.
        if any(select.select(run['handles'], [], [], 0)[0] for run in runs):
            raise ValueError('fixture_ended_before_injection')
        proof['kill_requested'] = True
        persist()
        docker(['kill', '--signal=KILL', target['record']['id']])
        deadline = time.monotonic() + 10
        pending = set(target['handles'])
        while pending and time.monotonic() < deadline:
            pending.difference_update(select.select(list(pending), [], [], max(0, deadline - time.monotonic()))[0])
        stopped = inspect_json(['inspect', '--type=container', target['record']['id']], 'container_inspection')[0]
        if (pending or stopped.get('Id') != target['record']['id'] or stopped.get('Image') != image
                or stopped.get('State', {}).get('Running') is not False
                or stopped.get('State', {}).get('Status') != 'exited'
                or stopped.get('State', {}).get('ExitCode') != 137):
            raise ValueError('fixture_namespace_exit_unproven')
        running_container(control['record']['id'], image)
        if select.select(control['handles'], [], [], 0)[0]:
            raise ValueError('fixture_control_exited')
        proof.update(status='passed', target_processes_exited=True, unrelated_control_survived=True,
                     supervisor_exit_code=137, native_recovery_receipt='not_expected_after_sigkill')
        persist()
    except BaseException:
        proof['status'] = 'unknown'
        raise
    finally:
        cleanup_failed = False
        for run in runs:
            try:
                docker(['rm', '--force', '--volumes', run['record']['id']])
                run['record']['removed'] = True
            except Exception:
                cleanup_failed = True
            process = run.get('process')
            if process:
                try:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=5)  # Client only; never counted as remote cleanup.
                except Exception:
                    cleanup_failed = True
                finally:
                    for stream in (process.stdin, process.stdout):
                        try:
                            stream.close()
                        except OSError:
                            cleanup_failed = True
            for handle in run['handles']:
                os.close(handle)
        if cleanup_failed:
            proof['status'] = 'unknown'
        persist()
        if cleanup_failed:
            raise ValueError('fixture_container_cleanup_unknown')


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
    name = 'factory-native-canary-' + identifier.hex
    receipt = {'canary_id': attempt, 'job_id': job, 'attempt_id': attempt,
               'validation_only': True, 'gateway_auth_tested': False,
               'production_route_activated': False, 'posthog_delivery': 'not_attempted',
               'status': 'unknown', 'container_removed': False, 'disposable_name': name}
    created = False
    create_attempted = False
    phase = 'public_assets'
    try:
        assets = {key: public_asset(runtime, path, base) for key, path in paths.items()}
        digests = {key: hashlib.sha256(value.encode()).hexdigest() for key, value in assets.items()}
        if digests != ASSET_SHA256:
            raise PreflightRefusal('public_assets', 'asset_digest_mismatch')
        receipt['asset_sha256'] = digests
        phase = 'runtime_identity'
        identity = runtime.run_native(wrapper, 'native-canary-identity', {})
        if not isinstance(identity, dict):
            raise PreflightRefusal(phase, 'identity_invalid')
        phase = 'container_inspection'
        image = image_identity(identity.get('hostname'))
        receipt['image_id'] = image
        phase = 'container_create'
        create_attempted = True
        container = docker(create_arguments(name, image, assets['driver.py'])).strip()
        if not re.fullmatch(r'[0-9a-f]{64}', container):
            raise ValueError('invalid created container identity')
        created = True
        phase = 'container_safety'
        verify_created(container, name, image)
        receipt['container_safety_verified'] = True
        phase = 'native_execution'
        payload = {'assets': {key: assets[key] for key in ('main.py', 'owner.py', 'transport.py')}, 'binding': binding}
        result = json.loads(docker(['start', '--attach', '--interactive', container],
                                  data=(json.dumps(payload) + '\n').encode(), timeout=85))
        observations = result.pop('observations', None)
        if (result.get('marker_and_exit_zero') is not True or result.get('owner_exit_observed') is not True
                or not isinstance(observations, list) or len(observations) != 3
                or result.get('recovery_checks') != {name: True for name in (
                    'legacy_checkpoint_refused', 'completed_profile_reuse_refused', 'crash_restart_refused',
                    'inflight_cancel_quiesced', 'escaped_fixture_exited', 'duplicate_cancel_stable',
                    'active_crash_descendants_drained', 'crash_control_survived', 'active_crash_reuse_refused')}):
            raise ValueError('native canary proof incomplete')
        phase = 'supervisor_loss'
        def persist():
            runtime.write_observer_receipt(install.directory / ('native-canary-' + attempt + '.json'), receipt)
        supervisor_loss_checks(image, assets, binding, receipt, persist)
        # The container is credential-free and cannot ingest. Only this fixed
        # exporter in the existing runtime sees its configured ingestion key.
        phase = 'telemetry_relay'
        delivery = runtime.run_native(wrapper, 'native-canary-export', {
            'observer': assets['observer.py'], 'observations': observations, 'binding': binding,
            'job_id': job, 'attempt_id': attempt})
        receipt.update(result)
        receipt.update({'status': 'passed', 'phase': 'complete', 'telemetry_relay': delivery,
                        'posthog_delivery': 'server_verification_pending'})
    except Exception as error:
        receipt.update({'status': 'unknown' if create_attempted else 'refused', 'phase': phase,
                        'reason': 'operation_unavailable' if not create_attempted else 'isolated_canary_failed_or_unknown'})
        if isinstance(error, PreflightRefusal) and error.reason in REFUSAL_CODES.get(error.phase, set()):
            receipt.update(phase=error.phase, reason=error.reason)
            if error.phase == 'image_inspection' and error.reason == 'implicit_volumes_or_build_hooks':
                receipt['image_features'] = error.image_features
    finally:
        # Never remove a name on an ambiguous create failure/collision. Only the
        # exact returned container ID is ours; retain the name for reconciliation.
        if created:
            try:
                # Only our exact newly-created ID: remove any unexpected
                # anonymous volume too if the pre-start storage check refused.
                docker(['rm', '--force', '--volumes', container])
                receipt['container_removed'] = True
            except Exception:
                receipt['cleanup'] = 'unknown'
                receipt['status'] = 'unknown'
        elif create_attempted:
            receipt['cleanup'] = 'creation_or_cleanup_unknown'
        else:
            receipt['cleanup'] = 'not_needed_no_create'
        try:
            runtime.write_observer_receipt(install.directory / ('native-canary-' + attempt + '.json'), receipt)
            receipt['receipt_persisted'] = True
        except Exception:
            receipt.update(status='unknown', receipt_persisted=False)
    return receipt
