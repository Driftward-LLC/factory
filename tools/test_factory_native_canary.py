import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid

ROOT = Path(__file__).resolve().parents[1]


def module(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


canary = module('native_canary', 'deploy/factory-native-canary.py')
driver = module('native_canary_driver', 'deploy/native-canary/driver.py')
runtime = module('native_canary_runtime', 'deploy/factory-profile-runtime.py')
cli = module('native_canary_cli', 'deploy/factory-cli.py')


def binding():
    job, attempt = str(uuid.uuid4()), str(uuid.uuid4())
    return {'invocation_id': job + ':' + attempt + ':1', 'epoch': 1, 'fingerprint': 'a' * 64}


def observations(bound):
    properties = {'factory.invocation_id': bound['invocation_id'], 'factory.lease_epoch': 1,
                  'factory.experimental': True, 'hermes.session_id': 'factory-' + 'a' * 32,
                  'hermes.task_id': 'factory-' + 'b' * 32, 'factory.runtime_incarnation': str(uuid.uuid4()),
                  '$process_person_profile': False}
    return [{'event': 'factory.experimental_owner', 'distinct_id': 'driftward-hermes',
             'properties': {**properties, 'factory.stop.stage': stage}} for stage in ('registered', 'stopping', 'unknown')]


class NativeCanaryTests(unittest.TestCase):
    def test_exact_canonical_command_only_and_bound_backend(self):
        backend = SimpleNamespace(dispatch=Mock(return_value={}))
        install = SimpleNamespace()
        config = {'hermes_wrapper': '/reviewed/wrapper', 'profile_runtime': '/reviewed/helper'}
        with patch.object(cli, 'installation', return_value=install), patch.object(cli, 'checked_hermes_wrapper', side_effect=Path), \
             patch.object(cli.importlib.util, 'module_from_spec', return_value=backend), \
             patch.object(cli.importlib.util, 'spec_from_file_location'), patch('builtins.print'):
            self.assertEqual(cli.dispatch_bound(config, ['profiles', 'canary', 'native-owner']), 0)
            for tail in (['--command', 'anything'], ['--image', 'anything'], ['--source', '/tmp/source']):
                with self.assertRaises(ValueError):
                    cli.dispatch_bound(config, ['profiles', 'canary', 'native-owner', *tail])
        backend.dispatch.assert_called_once_with(install, config, Path('/reviewed/wrapper'), ['canary', 'native-owner'])

    def test_disposable_arguments_never_inherit_mounts_network_credentials_or_entrypoint(self):
        args = canary.create_arguments('factory-native-canary-' + 'a' * 32, 'sha256:' + 'b' * 64, 'fixed public code')
        for required in ('--network=none', '--read-only', '--user=10000:10000', '--cap-drop=ALL', '--security-opt=no-new-privileges:true', '--pids-limit=64'):
            self.assertIn(required, args)
        self.assertFalse(any(value in args for value in ('--volume', '-v', '--mount', '--volumes-from', '--env-file', '--privileged')))
        self.assertEqual(args[args.index('--entrypoint=/usr/bin/env') + 2], '-i')
        self.assertNotIn('POSTHOG_API_KEY', ' '.join(args))
        with self.assertRaises(ValueError):
            canary.create_arguments('real-runtime', 'image:latest', 'anything')

    def test_image_validation_refuses_volumes_credentials_or_wrong_identity(self):
        ident = 'a' * 12
        image = 'sha256:' + 'b' * 64
        container = [{'Id': 'a' * 64, 'Image': image, 'Config': {'Hostname': ident}, 'State': {'Running': True}}]
        safe = [{'Id': image, 'Config': {'Env': ['PATH=/usr/bin'], 'Entrypoint': ['/fixed-entrypoint']}}]
        with patch.object(canary, 'docker', side_effect=[json.dumps(container), json.dumps(safe)]):
            self.assertEqual(canary.image_identity(ident), image)
        for changes in ({'Volumes': {'/opt/data': {}}}, {'Env': ['OPENAI_API_KEY=must-not-print']}, {'OnBuild': ['anything']}):
            unsafe = copy.deepcopy(safe)
            unsafe[0]['Config'].update(changes)
            with patch.object(canary, 'docker', side_effect=[json.dumps(container), json.dumps(unsafe)]):
                with self.assertRaises(ValueError) as error:
                    canary.image_identity(ident)
                self.assertNotIn('must-not-print', str(error.exception))
        with patch.object(canary, 'docker') as call, self.assertRaises(ValueError):
            canary.image_identity('shell; arbitrary')
        call.assert_not_called()

    def test_timeout_removes_only_created_id_retains_sanitized_evidence(self):
        receipts = []
        fake = SimpleNamespace(__file__=str(ROOT / 'deploy/factory-profile-runtime.py'), os=SimpleNamespace(geteuid=lambda: 0),
                               checked=lambda p: p, run_native=Mock(return_value={'hostname': 'a' * 12}),
                               write_observer_receipt=lambda path, value: receipts.append(copy.deepcopy(value)))
        calls = []
        def docker(args, **kwargs):
            calls.append(args)
            if args[0] == 'create':
                return 'c' * 64
            if args[0] == 'start':
                raise subprocess.TimeoutExpired(['docker'], 85, output='SENSITIVE_NATIVE_OUTPUT')
            return ''
        with patch.object(canary, 'public_asset', side_effect=lambda runtime, path, base: path.read_text()), \
             patch.object(canary, 'image_identity', return_value='sha256:' + 'b' * 64), patch.object(canary, 'docker', side_effect=docker):
            result = canary.run(fake, SimpleNamespace(directory=Path('/unused')), '/reviewed/wrapper')
        self.assertEqual(calls[-1], ['rm', '--force', 'c' * 64])
        self.assertEqual(result['status'], 'unknown')
        self.assertTrue(result['container_removed'])
        self.assertNotIn('SENSITIVE', json.dumps(receipts))
        fake.run_native.assert_called_once()
        # Ambiguous creation must not delete a colliding name or shared process.
        calls.clear()
        with patch.object(canary, 'public_asset', side_effect=lambda runtime, path, base: path.read_text()), \
             patch.object(canary, 'image_identity', return_value='sha256:' + 'b' * 64), \
             patch.object(canary, 'docker', side_effect=ValueError('private error')):
            result = canary.run(fake, SimpleNamespace(directory=Path('/unused')), '/reviewed/wrapper')
        self.assertFalse(result['container_removed'])
        self.assertEqual(result['cleanup'], 'creation_or_cleanup_unknown')

    def test_nonroot_refused_before_runtime_operations(self):
        fake = SimpleNamespace(os=SimpleNamespace(geteuid=lambda: 1000))
        with self.assertRaises(ValueError):
            canary.run(fake, None, None)

    def test_changed_public_asset_refused_before_runtime(self):
        fake = SimpleNamespace(__file__=str(ROOT / 'deploy/factory-profile-runtime.py'), os=SimpleNamespace(geteuid=lambda: 0), run_native=Mock())
        with patch.object(canary, 'public_asset', return_value='unreviewed replacement'), self.assertRaises(ValueError):
            canary.run(fake, None, None)
        fake.run_native.assert_not_called()

    def test_wrong_native_source_refused_before_child_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'run_agent.py').write_text('wrong source')
            with patch.object(driver.subprocess, 'Popen') as spawn, self.assertRaises(ValueError):
                driver.execute({}, root, root / 'canary')
            spawn.assert_not_called()

    def test_actual_observer_relay_bounds_correlation_and_no_provider_credential(self):
        bound = binding()
        job, attempt, _ = bound['invocation_id'].split(':')
        payload = {'binding': bound, 'job_id': job, 'attempt_id': attempt,
                   'observer': (ROOT / 'deploy/factory-observers/posthog/__init__.py').read_text(), 'observations': observations(bound)}
        delivered = []
        def post(_url, **kwargs):
            self.assertNotIn('OPENAI_API_KEY', os.environ)
            delivered.extend(kwargs['json']['batch'])
            return SimpleNamespace(raise_for_status=lambda: None)
        with patch.dict(os.environ, {'POSTHOG_API_KEY': 'fake-ingest-only', 'POSTHOG_HOST': 'https://us.i.posthog.com', 'OPENAI_API_KEY': 'must-not-leak'}), \
             patch.dict(sys.modules, {'hermes_constants': SimpleNamespace(get_hermes_home=lambda: '/tmp/isolated-exporter')}), \
             patch('requests.post', side_effect=post):
            result = runtime.native_canary_export(payload)
        self.assertEqual(result['events_queued'], 3)
        self.assertEqual(len(delivered), 3)
        self.assertEqual(result['trace_id'], str(uuid.uuid5(uuid.NAMESPACE_URL, 'driftward-hermes:trace:attempt:' + attempt)))
        self.assertTrue(all(event['properties']['factory.validation_kind'] == 'isolated_native_owner_relay' for event in delivered))
        self.assertNotIn('must-not-leak', json.dumps(delivered))
        for key, value in (('prompt', 'must-not-leak'), ('factory.stop.stage', 'arbitrary')):
            bad = copy.deepcopy(payload)
            bad['observations'][1]['properties'][key] = value
            with patch.dict(os.environ, {'POSTHOG_API_KEY': 'fake-only'}), \
                 patch.dict(sys.modules, {'hermes_constants': SimpleNamespace(get_hermes_home=lambda: '/tmp/isolated-exporter')}), \
                 patch('requests.post') as send, self.assertRaises(ValueError):
                runtime.native_canary_export(bad)
            send.assert_not_called()

    @unittest.skipUnless(os.environ.get('FACTORY_TEST_HERMES_SOURCE') and os.environ.get('FACTORY_TEST_HERMES_PYTHON'), 'explicit isolated native fixture required')
    def test_real_controlled_canary_driver(self):
        assets = {name: (ROOT / 'deploy/experimental-native-executor' / name).read_text() for name in ('main.py', 'owner.py')}
        assets['transport.py'] = (ROOT / 'deploy/native-canary/transport.py').read_text()
        # Run with the explicit isolated interpreter, never the host's native SDK.
        source = "import importlib.util,json,sys; from pathlib import Path; s=importlib.util.spec_from_file_location('driver',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);print(json.dumps(m.execute(json.load(sys.stdin),Path(sys.argv[2]),Path(sys.argv[3]))))"
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run([os.environ['FACTORY_TEST_HERMES_PYTHON'], '-c', source, str(ROOT / 'deploy/native-canary/driver.py'),
                                     os.environ['FACTORY_TEST_HERMES_SOURCE'], str(Path(temporary) / 'canary')],
                                    input=json.dumps({'assets': assets, 'binding': binding()}), capture_output=True, text=True, timeout=80,
                                    env={'PATH': '/usr/bin:/bin'})
        self.assertEqual(result.returncode, 0, 'isolated native driver failed')
        proof = json.loads(result.stdout)
        self.assertTrue(proof['marker_and_exit_zero'])
        self.assertEqual([event['properties']['factory.stop.stage'] for event in proof['observations']], ['registered', 'stopping', 'unknown'])
        self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
