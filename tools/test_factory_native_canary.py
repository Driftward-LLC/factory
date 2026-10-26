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
        backend = SimpleNamespace(dispatch=Mock(return_value={'status': 'passed'}))
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

    def test_canonical_refused_and_unknown_exit_nonzero(self):
        backend = SimpleNamespace(dispatch=Mock())
        with patch.object(cli, 'installation'), patch.object(cli, 'checked_hermes_wrapper', side_effect=Path), \
             patch.object(cli.importlib.util, 'module_from_spec', return_value=backend), \
             patch.object(cli.importlib.util, 'spec_from_file_location'), patch('builtins.print'):
            for status in ('refused', 'unknown'):
                backend.dispatch.return_value = {'status': status, 'phase': 'public_assets', 'reason': 'operation_unavailable'}
                self.assertEqual(cli.dispatch_bound({'hermes_wrapper': '/safe', 'profile_runtime': '/safe'}, ['profiles', 'canary', 'native-owner']), 1)

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
        for changes in ({'Volumes': {'/other/data': {}}}, {'Env': ['OPENAI_API_KEY=must-not-print']}, {'OnBuild': 'anything'}):
            unsafe = copy.deepcopy(safe)
            unsafe[0]['Config'].update(changes)
            with patch.object(canary, 'docker', side_effect=[json.dumps(container), json.dumps(unsafe)]):
                with self.assertRaises(ValueError) as error:
                    canary.image_identity(ident)
                self.assertNotIn('must-not-print', str(error.exception))
        with patch.object(canary, 'docker') as call, self.assertRaises(ValueError):
            canary.image_identity('shell; arbitrary')
        call.assert_not_called()

    def test_measured_volume_and_build_hooks_allowed_only_with_fixed_tmpfs(self):
        image = 'sha256:' + 'b' * 64
        container = [{'Id': 'a' * 64, 'Image': image, 'Config': {'Hostname': 'a' * 12}, 'State': {'Running': True}}]
        config = {'Volumes': {'/opt/data': {}}, 'OnBuild': ['RUN downstream-only']}
        with patch.object(canary, 'docker', side_effect=[json.dumps(container), json.dumps([{'Id': image, 'Config': config}])]):
            self.assertEqual(canary.image_identity('a' * 12), image)
        args = canary.create_arguments('factory-native-canary-' + 'a' * 32, image, 'fixed')
        self.assertIn('/opt/data:' + canary.TMPFS['/opt/data'], args)
        self.assertIn('--no-healthcheck', args)

    def test_created_container_storage_and_safety_fail_closed(self):
        container, name, image = 'c' * 64, 'factory-native-canary-' + 'a' * 32, 'sha256:' + 'b' * 64
        row = {'Id': container, 'Name': '/' + name, 'Image': image, 'State': {'Status': 'created', 'Running': False},
               'Config': {'User': '10000:10000', 'Healthcheck': {'Test': ['NONE']}}, 'Mounts': [],
               'HostConfig': {'NetworkMode': 'none', 'ReadonlyRootfs': True, 'Privileged': False,
                              'CapDrop': ['ALL'], 'SecurityOpt': ['no-new-privileges:true'], 'Tmpfs': canary.TMPFS}}
        with patch.object(canary, 'docker', return_value=json.dumps([row])):
            canary.verify_created(container, name, image)
        for field, value in (('Mounts', [{'Type': 'volume', 'Destination': '/opt/data', 'Name': 'unexpected'}]),
                             ('Mounts', [{'Type': 'bind', 'Destination': '/tmp', 'Source': '/private'}]),
                             ('Name', '/wrong'), ('Image', 'wrong')):
            changed = copy.deepcopy(row)
            changed[field] = value
            with patch.object(canary, 'docker', return_value=json.dumps([changed])), self.assertRaises(ValueError):
                canary.verify_created(container, name, image)
        for field, value in (('Tmpfs', {'/tmp': canary.TMPFS['/tmp']}), ('NetworkMode', 'host'),
                             ('ReadonlyRootfs', False), ('Privileged', True), ('Binds', ['/private:/tmp']),
                             ('VolumesFrom', ['shared']), ('CapAdd', ['SYS_ADMIN']), ('SecurityOpt', [])):
            changed = copy.deepcopy(row)
            changed['HostConfig'][field] = value
            with patch.object(canary, 'docker', return_value=json.dumps([changed])), self.assertRaises(ValueError):
                canary.verify_created(container, name, image)
        fake = SimpleNamespace(__file__=str(ROOT / 'deploy/factory-profile-runtime.py'), os=SimpleNamespace(geteuid=lambda: 0),
                               checked=lambda p: p, run_native=Mock(return_value={'hostname': 'a' * 12}), write_observer_receipt=Mock())
        with patch.object(canary, 'public_asset', side_effect=lambda runtime, path, base: path.read_text()), \
             patch.object(canary, 'image_identity', return_value=image), \
             patch.object(canary, 'verify_created', side_effect=ValueError('private')), \
             patch.object(canary, 'docker', side_effect=[container, '']) as docker:
            result = canary.run(fake, SimpleNamespace(directory=Path('/unused')), None)
        self.assertEqual(result['phase'], 'container_safety')
        self.assertTrue(result['container_removed'])
        self.assertEqual(docker.call_args_list[-1].args[0], ['rm', '--force', '--volumes', container])
        self.assertFalse(any(call.args[0][0] == 'start' for call in docker.call_args_list))

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
             patch.object(canary, 'image_identity', return_value='sha256:' + 'b' * 64), \
             patch.object(canary, 'verify_created'), patch.object(canary, 'docker', side_effect=docker):
            result = canary.run(fake, SimpleNamespace(directory=Path('/unused')), '/reviewed/wrapper')
        self.assertEqual(calls[-1], ['rm', '--force', '--volumes', 'c' * 64])
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

    def test_success_checks_created_container_before_start_and_relay(self):
        calls = []
        fake = SimpleNamespace(__file__=str(ROOT / 'deploy/factory-profile-runtime.py'), os=SimpleNamespace(geteuid=lambda: 0),
                               run_native=Mock(side_effect=[{'hostname': 'a' * 12}, {'events_queued': 3}]),
                               write_observer_receipt=Mock())
        def docker(args, **kwargs):
            calls.append(args[0])
            if args[0] == 'create':
                return 'c' * 64
            if args[0] == 'start':
                return json.dumps({'marker_and_exit_zero': True, 'owner_exit_observed': True, 'observations': [{}, {}, {}],
                                   'recovery_checks': {name: True for name in ('legacy_checkpoint_refused',
                                       'completed_profile_reuse_refused', 'crash_restart_refused', 'inflight_cancel_quiesced',
                                       'escaped_fixture_exited', 'duplicate_cancel_stable')}})
            return ''
        with patch.object(canary, 'public_asset', side_effect=lambda runtime, path, base: path.read_text()), \
             patch.object(canary, 'image_identity', return_value='sha256:' + 'b' * 64), \
             patch.object(canary, 'verify_created', side_effect=lambda *args: calls.append('verify')), \
             patch.object(canary, 'docker', side_effect=docker):
            result = canary.run(fake, SimpleNamespace(directory=Path('/unused')), None)
        self.assertEqual(calls, ['create', 'verify', 'start', 'rm'])
        self.assertEqual(result['status'], 'passed')
        self.assertTrue(result['container_safety_verified'])
        self.assertTrue(result['container_removed'])

    def test_nonroot_refused_before_runtime_operations(self):
        fake = SimpleNamespace(os=SimpleNamespace(geteuid=lambda: 1000))
        with self.assertRaises(ValueError):
            canary.run(fake, None, None)

    def test_missing_recovery_proof_cleans_up_without_relaying_success(self):
        fake = SimpleNamespace(__file__=str(ROOT / 'deploy/factory-profile-runtime.py'), os=SimpleNamespace(geteuid=lambda: 0),
                               run_native=Mock(return_value={'hostname': 'a' * 12}), write_observer_receipt=Mock())
        result = {'marker_and_exit_zero': True, 'owner_exit_observed': True, 'observations': [{}, {}, {}]}
        with patch.object(canary, 'public_asset', side_effect=lambda runtime, path, base: path.read_text()), \
             patch.object(canary, 'image_identity', return_value='sha256:' + 'b' * 64), patch.object(canary, 'verify_created'), \
             patch.object(canary, 'docker', side_effect=['c' * 64, json.dumps(result), '']):
            receipt = canary.run(fake, SimpleNamespace(directory=Path('/unused')), None)
        self.assertEqual(receipt['status'], 'unknown')
        self.assertEqual(receipt['phase'], 'native_execution')
        self.assertTrue(receipt['container_removed'])
        fake.run_native.assert_called_once()

    def test_changed_public_asset_refused_before_runtime(self):
        fake = SimpleNamespace(__file__=str(ROOT / 'deploy/factory-profile-runtime.py'), os=SimpleNamespace(geteuid=lambda: 0), run_native=Mock(), write_observer_receipt=Mock())
        with patch.object(canary, 'public_asset', return_value='unreviewed replacement'), patch.object(canary, 'docker') as docker:
            result = canary.run(fake, SimpleNamespace(directory=Path('/unused')), None)
        self.assertEqual((result['status'], result['phase'], result['reason']), ('refused', 'public_assets', 'asset_digest_mismatch'))
        self.assertTrue(result['receipt_persisted'])
        self.assertEqual(result['cleanup'], 'not_needed_no_create')
        fake.run_native.assert_not_called()
        fake.write_observer_receipt.assert_called_once()
        docker.assert_not_called()

    def test_preflight_phase_reason_differentiation_and_secret_error_suppression(self):
        fake = SimpleNamespace(__file__=str(ROOT / 'deploy/factory-profile-runtime.py'), os=SimpleNamespace(geteuid=lambda: 0),
                               run_native=Mock(), write_observer_receipt=Mock())
        install = SimpleNamespace(directory=Path('/unused'))
        with patch.object(canary, 'public_asset', side_effect=RuntimeError('SENSITIVE_ASSET_ERROR')), patch.object(canary, 'docker') as docker:
            result = canary.run(fake, install, None)
        self.assertEqual((result['phase'], result['reason']), ('public_assets', 'operation_unavailable'))
        docker.assert_not_called()
        with patch.object(canary, 'public_asset', side_effect=lambda runtime, path, base: path.read_text()):
            fake.run_native.side_effect = RuntimeError('SENSITIVE_ENVIRONMENT_ERROR')
            result = canary.run(fake, install, None)
            self.assertEqual((result['status'], result['phase'], result['reason']), ('refused', 'runtime_identity', 'operation_unavailable'))
            fake.run_native.side_effect = None
            fake.run_native.return_value = {'hostname': 'a' * 12}
            with patch.object(canary, 'docker', side_effect=RuntimeError('SENSITIVE_DOCKER_STDERR')) as docker:
                result = canary.run(fake, install, None)
                self.assertEqual((result['phase'], result['reason']), ('container_inspection', 'inspection_unavailable'))
                self.assertEqual(docker.call_count, 1)
            container = [{'Id': 'a' * 64, 'Image': 'sha256:' + 'b' * 64, 'Config': {'Hostname': 'a' * 12}, 'State': {'Running': True}}]
            for image, reason in (({'Volumes': {'/data': {}}}, 'implicit_volumes_or_build_hooks'),
                                  ({'Env': ['OPENAI_API_KEY=SENSITIVE_KEY_VALUE']}, 'credential_environment_refused')):
                with patch.object(canary, 'docker', side_effect=[json.dumps(container), json.dumps([{'Id': 'sha256:' + 'b' * 64, 'Config': image}])]) as docker:
                    result = canary.run(fake, install, None)
                self.assertEqual((result['status'], result['phase'], result['reason']), ('refused', 'image_inspection', reason))
                self.assertEqual(docker.call_count, 2)
                self.assertNotIn('SENSITIVE', json.dumps(result))
        self.assertNotIn('SENSITIVE', str(fake.write_observer_receipt.call_args_list))

    def test_image_feature_receipt_is_bounded_and_refuses_before_create(self):
        fake = SimpleNamespace(__file__=str(ROOT / 'deploy/factory-profile-runtime.py'), os=SimpleNamespace(geteuid=lambda: 0),
                               run_native=Mock(return_value={'hostname': 'a' * 12}), write_observer_receipt=Mock())
        image = 'sha256:' + 'b' * 64
        container = [{'Id': 'a' * 64, 'Image': image, 'Config': {'Hostname': 'a' * 12}, 'State': {'Running': True}}]
        volumes = {'/opt/data': {}, '/private\nSENSITIVE_PATH': {}, '/' + 'x' * 256: {}}
        volumes.update({f'/data/{index}': {} for index in range(20)})
        config = {'Volumes': volumes, 'OnBuild': ['RUN SENSITIVE_BUILD_COMMAND'],
                  'Healthcheck': {'Test': ['CMD', 'SENSITIVE_HEALTHCHECK']}, 'Env': ['TOKEN=SENSITIVE_ENV']}
        with patch.object(canary, 'public_asset', side_effect=lambda runtime, path, base: path.read_text()), \
             patch.object(canary, 'docker', side_effect=[json.dumps(container), json.dumps([{'Id': image, 'Config': config}])]) as docker:
            result = canary.run(fake, SimpleNamespace(directory=Path('/unused')), None)
        self.assertEqual((result['status'], result['phase'], result['reason']),
                         ('refused', 'image_inspection', 'implicit_volumes_or_build_hooks'))
        self.assertEqual(result['image_features'], {
            'volume_destinations': ['/opt/data', '[redacted]', '[redacted]'] + [f'/data/{index}' for index in range(13)],
            'volume_destination_count': 23, 'volume_destinations_truncated': True,
            'onbuild_count': 1, 'healthcheck_present': True})
        self.assertEqual(result['cleanup'], 'not_needed_no_create')
        self.assertEqual(docker.call_count, 2)
        self.assertNotIn('SENSITIVE', json.dumps(result))
        self.assertNotIn('SENSITIVE', str(fake.write_observer_receipt.call_args_list))

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
             patch.dict(sys.modules, {'hermes_constants': SimpleNamespace(get_hermes_home=lambda: '/tmp/isolated-exporter'),
                                      'requests': SimpleNamespace(post=post)}):
            result = runtime.native_canary_export(payload)
        self.assertEqual(result['events_queued'], 3)
        self.assertEqual(len(delivered), 3)
        self.assertEqual(result['trace_id'], str(uuid.uuid5(uuid.NAMESPACE_URL, 'driftward-hermes:trace:attempt:' + attempt)))
        self.assertTrue(all(event['properties']['factory.validation_kind'] == 'isolated_native_owner_relay' for event in delivered))
        self.assertNotIn('must-not-leak', json.dumps(delivered))
        for key, value in (('prompt', 'must-not-leak'), ('factory.stop.stage', 'arbitrary')):
            bad = copy.deepcopy(payload)
            bad['observations'][1]['properties'][key] = value
            send = Mock()
            with patch.dict(os.environ, {'POSTHOG_API_KEY': 'fake-only'}), \
                 patch.dict(sys.modules, {'hermes_constants': SimpleNamespace(get_hermes_home=lambda: '/tmp/isolated-exporter'),
                                          'requests': SimpleNamespace(post=send)}), self.assertRaises(ValueError):
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
        self.assertEqual(proof['recovery_checks'], {name: True for name in ('legacy_checkpoint_refused',
                         'completed_profile_reuse_refused', 'crash_restart_refused', 'inflight_cancel_quiesced',
                         'escaped_fixture_exited', 'duplicate_cancel_stable')})
        self.assertEqual([event['properties']['factory.stop.stage'] for event in proof['observations']], ['registered', 'stopping', 'unknown'])
        self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
