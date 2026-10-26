"""Fixed public recorded-transport canary, only in an unmounted disposable container."""
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time

SOURCES = {
    'run_agent.py': '73efa221f42be96a41a6b8a1ee144caf47041e06f5c7d32b3bbd898d8dc250b2',
    'agent/process_bootstrap.py': '32c754f552ad6e043267c6284e91677e2fa69d71e404e9c620c09ca51bb2da40',
    'tools/process_registry.py': '4dacd3f6753d7b81e4ab338f7612624ec1f11c074cc969db49024a967a3b625e',
    'tools/environments/local.py': 'd96c53a2755e0afc6566e790ae9489a825c4d790c51bfcc258c78aafc2a47a26',
    'tools/environments/base.py': '2ac965c3f0baa3beb5ca591bb3fc2523037991e811c7abcc5bd941de63d19d4a',
    'tools/terminal_tool.py': 'a3c62398a79e062697d9cd2b2f766b55cfa36c277bc06281e95144f8760a5181',
}


def read_frame(process, deadline):
    data = bytearray()
    while len(data) <= 65536:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
            raise ValueError('protocol_timeout')
        byte = os.read(process.stdout.fileno(), 1)
        if not byte:
            raise ValueError('protocol_eof')
        data.extend(byte)
        if byte == b'\n':
            return json.loads(data)
    raise ValueError('protocol_oversize')


def recovery_checks(root, env, bound):
    """Recorded child handles only; crashes occur before tool authorization."""
    launch = {'binding': bound, 'backend': 'local', 'enabled_tools': ['terminal', 'process_manage']}
    def start(home, journal):
        return subprocess.Popen([sys.executable, '-u', str(root / 'main.py'),
                                 '--experimental-local-terminal', '--fixture', '--controlled', '--journal', str(journal)],
                                cwd=root, env={**env, 'HERMES_HOME': str(home), 'HOME': str(home),
                                               'XDG_CONFIG_HOME': str(home), 'XDG_CACHE_HOME': str(home)},
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    def send(process, value):
        process.stdin.write((json.dumps(value) + '\n').encode()); process.stdin.flush()
    def finish(process):
        if not process.stdin.closed:
            process.stdin.close()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill(); process.wait(timeout=5)
        process.stdout.close()
    def refuse(home, journal):
        # A new job and journal must not bypass the retained profile claim.
        other = {**bound, 'invocation_id': '00000000-0000-4000-8000-000000000099:' + bound['invocation_id'].split(':', 1)[1]}
        process = start(home, journal)
        try:
            send(process, {**launch, 'binding': other})
            frame = read_frame(process, time.monotonic() + 8)
            process.wait(timeout=5)
            if frame != {'event': 'refused', 'reason': 'invalid_launch_or_runtime'} or process.returncode != 1 or journal.exists():
                raise ValueError('profile_reuse_not_refused')
        finally:
            finish(process)
    # Successful owner exit still leaves unknown containment; reuse is blocked.
    refuse(root / 'home', root / 'completed-replay')
    legacy = root / 'legacy-home'
    legacy.mkdir(mode=0o700)
    (legacy / 'processes.json').write_text('[]')
    refuse(legacy, root / 'legacy-replay')
    if (legacy / 'processes.json').read_text() != '[]' or (legacy / 'factory-native-owner.claim').exists():
        raise ValueError('legacy_checkpoint_changed')
    crashed = root / 'crashed-home'
    crashed.mkdir(mode=0o700)
    process = start(crashed, root / 'crashed-journal')
    try:
        send(process, launch)
        if read_frame(process, time.monotonic() + 12).get('event') != 'registered':
            raise ValueError('crash_registration_missing')
        claim = (crashed / 'factory-native-owner.claim/binding.json').read_bytes()
        process.kill()  # Exact new child handle; start was never authorized.
        process.wait(timeout=5)
    finally:
        finish(process)
    refuse(crashed, root / 'crash-replay')
    if (crashed / 'factory-native-owner.claim/binding.json').read_bytes() != claim:
        raise ValueError('crash_claim_changed')
    home = root / 'inflight-home'
    home.mkdir(mode=0o700)
    process = start(home, root / 'inflight-journal')
    try:
        send(process, launch)
        deadline = time.monotonic() + 25
        if read_frame(process, deadline).get('event') != 'registered':
            raise ValueError('inflight_registration_missing')
        send(process, {'start': bound})
        while True:
            frame = read_frame(process, deadline)
            if frame.get('event') == 'fixture_ready':
                break
            if frame.get('event') != 'fixture_spawn_admitted':
                raise ValueError('inflight_fixture_failed')
        pids = [frame['foreground_pid'], frame['escaped_pid'], *[p['pid'] for p in frame['background']]]
        send(process, {'cancel': bound})
        stop = read_frame(process, deadline)
        # Fenced foreground work may return through the fixture's failure frame.
        if stop.get('event') == 'execution_failed':
            stop = read_frame(process, deadline)
        receipt = stop.get('receipt', {})
        if (stop.get('event') != 'stop' or receipt.get('dispatch_quiesced') is not True or receipt.get('errors')
                or receipt.get('state') != 'unknown' or len(receipt.get('background', [])) != 2):
            raise ValueError('inflight_stop_unknown')
        def alive(pid):
            try:
                return Path('/proc', str(pid), 'stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z'
            except FileNotFoundError:
                return False
        until = time.monotonic() + 5
        while any(alive(pid) for pid in pids) and time.monotonic() < until:
            time.sleep(0.02)
        if any(alive(pid) for pid in pids):
            raise ValueError('fixture_survivor_observed')
        send(process, {'cancel': bound})
        if read_frame(process, deadline) != stop:
            raise ValueError('duplicate_stop_changed')
        process.stdin.close()
        exited = read_frame(process, deadline)
        process.wait(timeout=5)
        if (exited.get('event') != 'owner_exiting' or exited.get('binding') != bound
                or exited.get('worker_exited') is not True or exited.get('dispatch_quiesced') is not True
                or exited.get('errors') != [] or process.returncode != 0):
            raise ValueError('inflight_owner_exit_unknown')
    finally:
        finish(process)
    refuse(home, root / 'inflight-replay')
    return {'legacy_checkpoint_refused': True, 'completed_profile_reuse_refused': True,
            'crash_restart_refused': True, 'inflight_cancel_quiesced': True,
            'escaped_fixture_exited': True, 'duplicate_cancel_stable': True}


def execute(payload, native_root=Path('/opt/hermes'), root=Path('/tmp/canary')):
    for name, digest in SOURCES.items():
        path = native_root / name
        if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024 or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError('native_source_mismatch')
    if set(payload) != {'assets', 'binding'} or set(payload['assets']) != {'main.py', 'owner.py', 'transport.py'}:
        raise ValueError('invalid_fixed_assets')
    root.mkdir(mode=0o700)
    home = root / 'home'
    home.mkdir(mode=0o700)
    for name, data in payload['assets'].items():
        if not isinstance(data, str) or len(data) > 131072:
            raise ValueError('invalid_fixed_asset')
        (root / name).write_text(data)
    (home / 'config.yaml').write_text("model:\n  default: local-test\n  provider: custom\n  base_url: http://127.0.0.1:1/v1\nplatform_toolsets:\n  cli: [terminal]\nterminal:\n  backend: local\ntools:\n  tool_search:\n    enabled: 'off'\n")
    for name in ('implement-change', 'verify-app', 'prepare-pr'):
        directory = home / 'skills' / ('driftward-engineering:' + name)
        directory.mkdir(parents=True)
        (directory / 'SKILL.md').write_text('---\nname: "driftward-engineering:' + name + '"\ndescription: Disposable native preload proof\n---\nFIXED_FACTORY_SKILL_' + name + '\n')
    env = {'PATH': '/opt/hermes/.venv/bin:/usr/bin:/bin', 'HOME': str(home), 'HERMES_HOME': str(home),
           'XDG_CONFIG_HOME': str(home), 'XDG_CACHE_HOME': str(home), 'PYTHONPATH': str(native_root),
           'FACTORY_NATIVE_CANARY_RELAY': '1', 'LANG': 'C.UTF-8'}
    process = subprocess.Popen([sys.executable, '-u', str(root / 'transport.py'), str(root / 'main.py'),
                                '--experimental-local-terminal', '--controlled', '--journal', str(root / 'journal')],
                               cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    def send(value):
        process.stdin.write((json.dumps(value) + '\n').encode()); process.stdin.flush()
    bound = payload['binding']
    observations = []
    registered = result = exited = stopped = False
    deadline = time.monotonic() + 65
    try:
        send({'binding': bound, 'backend': 'local', 'enabled_tools': ['terminal', 'process_manage'],
              'model': 'local-test', 'provider': 'custom', 'prompt': 'Run the disposable terminal check and return its receipt.',
              'receipt_policy': {'repo': 'example/project', 'branch': 'codex/disposable'}})
        while not exited:
            frame = read_frame(process, deadline)
            event = frame.get('event')
            if event == 'owner_observation':
                if len(observations) >= 3:
                    raise ValueError('excess_owner_observations')
                observations.append(frame['observation'])
            elif event == 'registered' and not registered:
                if any(frame['ownership'].get(key) != value for key, value in bound.items()) or (home / 'native-effect-marker').exists():
                    raise ValueError('registration_barrier_failed')
                registered = True
                send({'start': bound})
            elif event == 'result' and registered and not result:
                proof = json.loads((home / 'transport-proof.json').read_text())
                if (not all(proof.get(key) is True for key in ('native_tool_result_observed', 'filesystem_effect_observed', 'mandatory_skills_observed'))
                        or proof.get('provider_requests') != 2 or (home / 'native-effect-marker').read_text() != 'native-real-dispatch-proof'):
                    raise ValueError('native_effect_missing')
                result = True
                send({'cancel': bound})
            elif event == 'stop' and result and not stopped:
                receipt = frame['receipt']
                if (any(receipt.get(key) != value for key, value in bound.items()) or receipt.get('state') != 'unknown'
                        or receipt.get('dispatch_quiesced') is not True or receipt.get('completion') != 'not_established'):
                    raise ValueError('invalid_stop_receipt')
                stopped = True
                process.stdin.close()
            elif event == 'owner_exiting' and stopped:
                if frame['binding'] != bound or frame['worker_exited'] is not True or frame['errors']:
                    raise ValueError('owner_exit_unknown')
                exited = True
            else:
                raise ValueError('unexpected_owner_protocol')
        process.wait(timeout=5)
        if process.returncode != 0:
            raise ValueError('native_command_failed')
        if [event.get('properties', {}).get('factory.stop.stage') for event in observations] != ['registered', 'stopping', 'unknown']:
            raise ValueError('missing_owner_observations')
        return {'native_agent': 'run_agent.AIAgent', 'provider': 'recorded_no_network', 'controlled_start': True,
                'marker_and_exit_zero': True, 'mandatory_dummy_skills': True, 'owner_exit_observed': True,
                'stop_state': 'unknown', 'containment': 'not_established', 'source_hashes': SOURCES,
                'observations': observations, 'recovery_checks': recovery_checks(root, env, bound)}
    finally:
        if not process.stdin.closed:
            process.stdin.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


if __name__ == '__main__':
    try:
        line = sys.stdin.buffer.readline(524289)
        if len(line) > 524288 or not line.endswith(b'\n'):
            raise ValueError('invalid_payload')
        value = execute(json.loads(line))
        print(json.dumps(value), flush=True)
    except BaseException:
        print(json.dumps({'status': 'failed', 'reason': 'isolated_native_canary_failed'}), flush=True)
        sys.exit(1)
