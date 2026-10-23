import importlib.util
import json
import os
from pathlib import Path
import tempfile
import socketserver
import subprocess
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('canonical_cli', Path(__file__).resolve().parents[1] / 'deploy/factory-cli.py')
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


class CanonicalCliTests(unittest.TestCase):
    def test_hermes_wrapper_rejects_unprotected_paths(self):
        for value in (None, 42, 'relative/path'):
            with self.assertRaises(ValueError):
                cli.checked_hermes_wrapper(value)
        directory = 0o040755
        regular = 0o100755
        for bad in (SimpleNamespace(st_mode=0o120777, st_uid=0),
                    SimpleNamespace(st_mode=0o100775, st_uid=0),
                    SimpleNamespace(st_mode=regular, st_uid=1000)):
            def metadata(path):
                return bad if path == Path('/usr/local/bin/hermes-fixture') else SimpleNamespace(st_mode=directory, st_uid=0)
            with patch.object(Path, 'lstat', autospec=True, side_effect=metadata), \
                 patch.object(cli.os, 'access', return_value=True):
                with self.assertRaises(ValueError):
                    cli.checked_hermes_wrapper('/usr/local/bin/hermes-fixture')
        def protected(path):
            return SimpleNamespace(st_mode=regular if path.name == 'hermes-fixture' else directory, st_uid=0)
        with patch.object(Path, 'lstat', autospec=True, side_effect=protected), \
             patch.object(cli.os, 'access', return_value=True):
            self.assertEqual(cli.checked_hermes_wrapper('/usr/local/bin/hermes-fixture'), Path('/usr/local/bin/hermes-fixture'))
        with patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=0o040777, st_uid=0)):
            with self.assertRaises(ValueError):
                cli.checked_hermes_wrapper('/usr/local/bin/hermes-fixture')

    def test_profile_subprocess_has_fixed_environment_timeout_and_private_output(self):
        with patch.dict(cli.os.environ, {'DRIFTWARD_HERMES_ROOT': '/wrong', 'HERMES_HOME': '/wrong', 'COMPOSE_FILE': '/wrong'}), \
             patch.object(cli.subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps(self.profile_result(False)))) as run:
            cli.hermes_profiles(Path('/usr/local/bin/hermes-fixture'), False)
        self.assertEqual(run.call_args.args[0], ['/usr/local/bin/hermes-fixture', 'shell', '-c', 'exec python3 -'])
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs['timeout'], 60)
        self.assertTrue(kwargs['capture_output'])
        self.assertTrue(kwargs['check'])
        for name in ('DRIFTWARD_HERMES_ROOT', 'HERMES_HOME', 'COMPOSE_FILE'):
            self.assertNotIn(name, kwargs['env'])

    @staticmethod
    def profile_result(create, existing=()):
        return {'profiles': [{'name': name, 'existed': name in existing,
                             'created': create and name not in existing, 'present': create or name in existing}
                            for name in cli.PROFILES], 'active_profile': 'default',
                'routing': 'unchanged', 'credentials': 'not_provisioned_by_factory'}

    def test_native_profile_creation_and_idempotence(self):
        # Execute the exact stdin script with a fake native module, covering
        # creation and repeated calls without a Docker runtime or credentials.
        import types
        names = {'default'}
        creations = []
        module = types.ModuleType('hermes_cli.profiles')
        module._get_profiles_root = lambda: Path(tempfile.gettempdir())
        module.list_profile_names = lambda: sorted(names)
        module.get_active_profile_name = lambda: 'default'
        def create_profile(name, **kwargs):
            self.assertEqual(kwargs, {'no_alias': True, 'no_skills': True, 'description': cli.PROFILES[name]})
            creations.append(name)
            names.add(name)
            print('native output must be suppressed')
        module.create_profile = create_profile
        module.launch_model_seed = lambda value: value
        config = types.ModuleType('hermes_cli.config')
        config.read_user_config_raw = lambda source: {'model': {'default': 'configured-model'}}
        constants = types.ModuleType('hermes_constants')
        constants.get_hermes_home = lambda: Path(tempfile.gettempdir()) / 'absent-fixture-profile'
        def run(args, **kwargs):
            import contextlib
            import io
            output = io.StringIO()
            with patch.dict(sys.modules, {'hermes_cli.profiles': module, 'hermes_cli.config': config,
                                         'hermes_constants': constants}), contextlib.redirect_stdout(output):
                exec(kwargs['input'], {})
            return SimpleNamespace(stdout=output.getvalue())
        with patch.object(cli.subprocess, 'run', side_effect=run):
            status = cli.hermes_profiles(Path('/fixture'), False)
            self.assertTrue(all(not row['present'] for row in status['profiles']))
            first = cli.hermes_profiles(Path('/fixture'), True)
            self.assertTrue(all(row['created'] for row in first['profiles']))
            again = cli.hermes_profiles(Path('/fixture'), True)
            self.assertTrue(all(row['existed'] and not row['created'] for row in again['profiles']))
        self.assertEqual(creations, list(cli.PROFILES))

    def test_native_creation_failure_is_not_reported_as_success(self):
        import contextlib
        import io
        import types
        module = types.ModuleType('hermes_cli.profiles')
        module._get_profiles_root = lambda: Path(tempfile.gettempdir())
        module.list_profile_names = lambda: ['default']
        module.get_active_profile_name = lambda: 'default'
        def fail(*args, **kwargs):
            raise RuntimeError('native creation failed')
        module.create_profile = fail
        module.launch_model_seed = lambda value: value
        config = types.ModuleType('hermes_cli.config')
        config.read_user_config_raw = lambda source: {}
        constants = types.ModuleType('hermes_constants')
        constants.get_hermes_home = lambda: Path(tempfile.gettempdir()) / 'absent-fixture-profile'
        def run(args, **kwargs):
            with patch.dict(sys.modules, {'hermes_cli.profiles': module, 'hermes_cli.config': config,
                                         'hermes_constants': constants}), contextlib.redirect_stdout(io.StringIO()):
                exec(kwargs['input'], {})
        with patch.object(cli.subprocess, 'run', side_effect=run), self.assertRaises(RuntimeError):
            cli.hermes_profiles(Path('/fixture'), True)

    def test_native_seed_inline_credentials_refuse_before_any_creation(self):
        import contextlib
        import io
        import types
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'config.yaml').write_text('fixture')
            names = {'default'}
            module = types.ModuleType('hermes_cli.profiles')
            module._get_profiles_root = lambda: root
            module.list_profile_names = lambda: sorted(names)
            module.get_active_profile_name = lambda: 'default'
            module.create_profile = unittest.mock.Mock()
            module.launch_model_seed = lambda value: value
            config = types.ModuleType('hermes_cli.config')
            constants = types.ModuleType('hermes_constants')
            constants.get_hermes_home = lambda: root
            def run(args, **kwargs):
                output = io.StringIO()
                with patch.dict(sys.modules, {'hermes_cli.profiles': module, 'hermes_cli.config': config,
                                             'hermes_constants': constants}), contextlib.redirect_stdout(output):
                    exec(kwargs['input'], {})
                return SimpleNamespace(stdout=output.getvalue())
            with patch.object(cli.subprocess, 'run', side_effect=run):
                for key in ('api_key', 'API-Key', 'apiKey', 'Token', 'access_token', 'refresh-token',
                            'password', 'CLIENT_SECRET', 'Authorization'):
                    with self.subTest(key=key):
                        config.read_user_config_raw = lambda source: {'providers': {'selected': {'headers': {key: 'private-fixture'}}}}
                        with self.assertRaisesRegex(RuntimeError, 'inline credentials') as error:
                            cli.hermes_profiles(Path('/fixture'), True)
                        self.assertNotIn('private-fixture', str(error.exception))
                        module.create_profile.assert_not_called()
                config.read_user_config_raw = lambda source: {'model': {'default': 'configured-model'},
                                                              'providers': {'selected': {'api_key': ''}}}
                module.create_profile.side_effect = lambda name, **kwargs: names.add(name)
                created = cli.hermes_profiles(Path('/fixture'), True)
                self.assertTrue(all(row['created'] for row in created['profiles']))
                self.assertEqual(module.create_profile.call_count, 2)
                config.read_user_config_raw = unittest.mock.Mock(side_effect=AssertionError('must not inspect config'))
                cli.hermes_profiles(Path('/fixture'), False)
                names.update(cli.PROFILES)
                cli.hermes_profiles(Path('/fixture'), True)
                config.read_user_config_raw.assert_not_called()

    def test_profile_errors_never_relay_native_output(self):
        for error in (subprocess.TimeoutExpired(['fixture'], 60, output='secret', stderr='secret'),
                      subprocess.CalledProcessError(1, ['fixture'], output='secret', stderr='secret')):
            import io
            output = io.StringIO()
            with patch.object(cli.os, 'geteuid', return_value=0), \
                 patch.object(cli, 'checked_config', return_value={}), \
                 patch.object(cli, 'dispatch', side_effect=error), patch.object(cli.sys, 'stderr', output):
                self.assertEqual(cli.main(), 1)
            self.assertNotIn('secret', output.getvalue())

    def test_profile_dispatch_keeps_fixed_scope(self):
        with patch.object(cli, 'installation', return_value=SimpleNamespace()), \
             patch.object(cli, 'checked_hermes_wrapper', return_value=Path('/fixture')), \
             patch.object(cli, 'hermes_profiles', return_value=self.profile_result(True)) as profiles, \
             patch('builtins.print'):
            self.assertEqual(cli.dispatch_bound({}, ['profiles', 'create']), 0)
            profiles.assert_called_once_with(Path('/fixture'), True)
            for args in (['profiles'], ['profiles', 'create', 'other'], ['profiles', 'delete']):
                with self.assertRaises(ValueError):
                    cli.dispatch_bound({}, args)

    @unittest.skipUnless(os.geteuid() == 0, 'real Bash operator transport requires root')
    def test_operator_round_trip_uses_private_parsed_credentials_and_cleans_up(self):
        root = Path(__file__).resolve().parents[1]
        sys.path.insert(0, str(root / 'deploy'))
        received = []
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                request = []
                while True:
                    line = self.rfile.readline()
                    if line == b'\r\n' or not line:
                        break
                    request.append(line.decode().strip())
                received.extend(request)
                body = b'{"attempts":[]}'
                self.wfile.write(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(body)).encode() + b'\r\n\r\n' + body)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'env').mkdir()
            credentials = path / 'env/factory-operator.env'
            token = 'literal-$(must-not-execute)-token'
            credentials.write_text("HATCHET_FACTORY_OPERATOR_TOKEN='" + token + "'\n")
            credentials.chmod(0o600)
            install = SimpleNamespace(directory=path)
            config = {'installation': str(path / 'installation'), 'production_bundle': str(root / 'deploy')}
            production = cli.production_module(config)
            socket = path / 'operator.sock'
            with socketserver.UnixStreamServer(str(socket), Handler) as server:
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                original_run = subprocess.run
                calls = []
                def run(args, **kwargs):
                    calls.append((args, kwargs))
                    if args[0] == 'docker':
                        kwargs['stdout'].write((root / 'factory/factory').read_bytes())
                        return SimpleNamespace(returncode=0)
                    with self.assertRaisesRegex(ValueError, 'another operation'):
                        with production.portable.installation_lock(Path(config['installation'])):
                            pass
                    result = original_run(args, **kwargs, capture_output=True, text=True, timeout=10)
                    self.assertNotIn(token, result.stdout + result.stderr)
                    self.assertEqual(result.stdout.strip(), '{"attempts":[]}')
                    return result
                try:
                    with patch.object(cli, 'installation', return_value=install), \
                         patch.object(cli, 'operator', return_value=('cid', socket, {})), \
                         patch.object(cli, 'cli_image', return_value='sha256:exact'), \
                         patch.object(cli.subprocess, 'run', side_effect=run):
                        self.assertEqual(cli.dispatch(config, ['attempts', 'list', 'ENG-162', '--json']), 0)
                finally:
                    server.shutdown()
                    thread.join()
            self.assertIn('GET /v1/attempts/work-item/ENG-162 HTTP/1.1', received)
            self.assertIn('Authorization: Bearer ' + token, received)
            self.assertFalse(Path(calls[-1][1]['env']['FACTORY_OPERATOR_ENV']).exists())
            self.assertIn('--network', calls[0][0])
            self.assertIn('--read-only', calls[0][0])

    def test_binding_requires_private_explicit_target(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'binding.json'
            value = dict(installation='/srv/factory', installation_id='identity', production_bundle='/srv/bundle')
            path.write_text(json.dumps(value))
            path.chmod(0o600)
            real_lstat = Path.lstat
            # Model root ownership only; retain actual file type/mode and content checks.
            def root_owned(target):
                return SimpleNamespace(st_mode=real_lstat(target).st_mode, st_uid=0)
            with patch.object(Path, 'lstat', autospec=True, side_effect=root_owned):
                self.assertEqual(cli.checked_config(path), value)
                path.chmod(0o644)
                with self.assertRaises(ValueError):
                    cli.checked_config(path)
                path.chmod(0o600)
                value['installation'] = 'relative'
                path.write_text(json.dumps(value))
                with self.assertRaises(ValueError):
                    cli.checked_config(path)
            with patch.object(Path, 'lstat', return_value=SimpleNamespace(st_mode=real_lstat(path).st_mode, st_uid=1000)):
                with self.assertRaisesRegex(ValueError, 'root-owned'):
                    cli.checked_config(path)

    def test_nonroot_entrypoint_refuses_before_loading_binding(self):
        with patch.object(cli.os, 'geteuid', return_value=1000), \
             patch.object(cli, 'checked_config') as binding, \
             patch.object(cli, 'dispatch') as dispatch, \
             patch.object(cli.sys, 'stderr'):
            self.assertEqual(cli.main(), 1)
            binding.assert_not_called()
            dispatch.assert_not_called()

    def test_logs_are_scoped_and_preserve_arguments(self):
        install = SimpleNamespace(compose=unittest.mock.Mock())
        with patch.object(cli, 'installation', return_value=install):
            cli.dispatch({}, ['logs', 'intake', '-n', '12', '-f'])
            install.compose.assert_called_once_with('logs', '--tail', '12', '--follow', 'intake-worker')
            with self.assertRaises(ValueError):
                cli.dispatch({}, ['logs', '--project-name', 'legacy'])

    def test_mutating_mode_and_legacy_doctor_fail_closed(self):
        with patch.object(cli, 'installation', return_value=SimpleNamespace()):
            for args in (['mode', 'real', '--yes'], ['doctor', 'hatchet']):
                with self.assertRaises(ValueError):
                    cli.dispatch({}, args)

    def test_operator_refuses_wrong_identity_image_or_socket(self):
        install = SimpleNamespace(project='factory-current', metadata={'installation_id': 'identity'},
                                  values={'FACTORY_IMAGE': 'sha256:active'},
                                  compose=lambda *args, **kwargs: 'container\n')
        row = {'Config': {'Labels': {'com.docker.compose.project': install.project,
               'com.docker.compose.service': 'factory-operator', 'io.driftward.factory.installation': 'identity'}},
               'Image': 'sha256:active', 'State': {'Running': True}, 'Mounts': [
                   {'Destination': '/run/driftward-factory-operator', 'Name': 'factory-current-operator-socket',
                    'Type': 'volume', 'Source': '/private/socket'}]}
        with patch.object(cli.subprocess, 'run', side_effect=lambda *a, **k: SimpleNamespace(stdout=json.dumps([row]))):
            self.assertEqual(cli.operator(install)[1], Path('/private/socket/operator.sock'))
            row['Image'] = 'sha256:stale'
            with self.assertRaises(ValueError):
                cli.operator(install)

    def test_cli_source_is_exact_release_image(self):
        image = 'sha256:' + 'a' * 64
        install = SimpleNamespace(images={'factory': image})
        row = {'Id': image, 'Config': {'Labels': {
            'org.opencontainers.image.source': 'https://github.com/Driftward-LLC/factory',
            'org.opencontainers.image.revision': 'b' * 40}}}
        with patch.object(cli.subprocess, 'run', side_effect=lambda *a, **k: SimpleNamespace(stdout=json.dumps([row]))):
            self.assertEqual(cli.cli_image(install), image)
            row['Config']['Labels']['org.opencontainers.image.source'] = 'other'
            with self.assertRaises(ValueError):
                cli.cli_image(install)

    def test_operator_lock_precedes_loading_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {'installation': str(Path(directory) / 'installation'),
                      'production_bundle': str(Path(__file__).resolve().parents[1] / 'deploy')}
            production = cli.production_module(config)
            with production.portable.installation_lock(Path(config['installation'])):
                with patch.object(cli, 'installation') as construction:
                    with self.assertRaisesRegex(ValueError, 'another operation'):
                        cli.dispatch(config, ['attempts', 'active', '--json'])
                    construction.assert_not_called()


if __name__ == '__main__':
    unittest.main()
