import base64
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tarfile
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('profile_runtime', ROOT / 'deploy/factory-profile-runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


def plugin_archive(extra=None):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as archive:
        for name in ('plugin.json', 'plugin.yaml', '.codex-plugin/plugin.json'):
            value = json.dumps({'name': 'driftward-engineering', 'version': '0.1.0-rc.6'}).encode()
            member = tarfile.TarInfo(name)
            member.size = len(value)
            archive.addfile(member, io.BytesIO(value))
        if extra:
            archive.addfile(extra)
    return stream.getvalue()


class ProfileRuntimeTests(unittest.TestCase):
    def activation_fixture(self, root):
        provision = root / 'reviewer.json'
        provision.write_text(json.dumps({'schema': 'factory-hermes-reviewer-provisioning-v2', 'unrelated': 'preserve'}))
        provision.chmod(0o600)
        database = root / 'bridge.db'
        with sqlite3.connect(database) as connection:
            connection.executescript('CREATE TABLE factory_attempts(status TEXT); CREATE TABLE factory_execution_leases(released_at TEXT); CREATE TABLE bridge_jobs(state TEXT);')
        install = SimpleNamespace(directory=root, project='factory-fixture', compose=Mock(),
                                  service_rows=lambda: [{'Service': 'execution-dispatch-worker', 'State': 'running'},
                                                        {'Service': 'bridge-executor-worker', 'State': 'exited'}])
        status = {'profiles': {'executor': {'engineering_enabled': True, 'posthog_plugin_enabled': True,
                                           'capabilities_match_default': True, 'capabilities_ready': True},
                               'reviewer': {'posthog_plugin_enabled': False, 'auth_present': False}}}
        return provision, database, install, status

    def test_activation_cuts_over_exact_files_and_preserves_exited_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provision, database, install, status = self.activation_fixture(root)
            dropin = root / 'dropin/factory-profile.conf'
            calls = []
            def native(wrapper, command, payload):
                calls.append(command)
                return status if command == 'status' else {}
            with patch.object(runtime, 'PROVISION', provision), patch.object(runtime, 'DROPIN', dropin), \
                 patch.object(runtime, 'PUBLIC_ASSETS', {}), \
                 patch.object(runtime, 'run_native', side_effect=native), patch.object(runtime, 'systemctl') as ctl, \
                 patch.object(runtime.subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps([{'Name': 'factory-fixture-bridge-data', 'Mountpoint': str(root)}]))):
                result = runtime.activate(install, '/wrapper')
            self.assertTrue(result['activated'])
            self.assertEqual(json.loads(provision.read_text())['unrelated'], 'preserve')
            self.assertEqual(json.loads(provision.read_text())['auth_root'], '/opt/data/profiles/factory-independent-reviewer')
            self.assertEqual(calls, ['status', 'idle', 'move-auth'])
            self.assertIn('DRIFTWARD_HERMES_PROFILE=factory-executor', dropin.read_text())
            for call in install.compose.call_args_list:
                self.assertNotIn('bridge-executor-worker', call.args)
            self.assertEqual(json.loads((root / 'profile-cutover.json').read_text())['phase'], 'complete')

    def test_activation_failure_restores_exact_state_and_auth_move(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provision, database, install, status = self.activation_fixture(root)
            original = provision.read_bytes()
            dropin = root / 'dropin/factory-profile.conf'
            calls = []
            def native(wrapper, command, payload):
                calls.append(command)
                return status if command == 'status' else {}
            def ctl(*args):
                if args[0] == 'is-active':
                    raise RuntimeError('health failure')
            with patch.object(runtime, 'PROVISION', provision), patch.object(runtime, 'DROPIN', dropin), \
                 patch.object(runtime, 'PUBLIC_ASSETS', {}), \
                 patch.object(runtime, 'run_native', side_effect=native), patch.object(runtime, 'systemctl', side_effect=ctl), \
                 patch.object(runtime.subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps([{'Name': 'factory-fixture-bridge-data', 'Mountpoint': str(root)}]))):
                with self.assertRaisesRegex(ValueError, 'cutover failed'):
                    runtime.activate(install, '/wrapper')
            self.assertEqual(provision.read_bytes(), original)
            self.assertFalse(dropin.exists())
            self.assertIn('rollback-auth', calls)
            self.assertEqual(json.loads((root / 'profile-cutover.json').read_text())['phase'], 'rolled_back')

    def test_public_artifacts_publish_only_after_idle_and_restore_byte_exact(self):
        for fail in (False, True):
            with self.subTest(failure=fail), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                provision, database, install, status = self.activation_fixture(root)
                helper = root / 'bundle/factory-profile-runtime.py'
                source = helper.parent / 'gateway/hermes-gateway-server.js'
                source.parent.mkdir(parents=True)
                source.write_bytes(b'new public DRIFTWARD_HERMES_PROFILE support')
                skill_source = helper.parent / 'engineering/review-code.md'
                skill_source.parent.mkdir()
                skill_source.write_bytes(b'pinned reviewer skill bytes\n')
                skill_destination = root / 'review-code.md'
                skill_destination.write_bytes(b'previous skill bytes\n')
                skill_destination.chmod(0o600)
                digest = hashlib.sha256(skill_source.read_bytes()).hexdigest()
                destination = root / 'installed.js'
                destination.write_bytes(b'previous public bytes\n')
                destination.chmod(0o644)
                def native(wrapper, command, payload):
                    if command == 'idle':
                        self.assertEqual(destination.read_bytes(), b'previous public bytes\n')
                    return status if command == 'status' else {}
                def ctl(*args):
                    if fail and args[0] == 'is-active':
                        raise RuntimeError('health failure')
                with patch.object(runtime, '__file__', str(helper)), \
                     patch.object(runtime, 'PUBLIC_ASSETS', {'gateway/hermes-gateway-server.js': destination,
                                                            'engineering/review-code.md': skill_destination}), \
                     patch.object(runtime, 'REVIEW_SKILL_DIGESTS', {'engineering/review-code.md': ('review_skill_digest', digest)}), \
                     patch.object(runtime, 'PROVISION', provision), patch.object(runtime, 'DROPIN', root / 'dropin/profile.conf'), \
                     patch.object(runtime, 'run_native', side_effect=native), patch.object(runtime, 'systemctl', side_effect=ctl), \
                     patch.object(runtime.subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps([{'Name': 'factory-fixture-bridge-data', 'Mountpoint': str(root)}]))):
                    if fail:
                        with self.assertRaises(ValueError):
                            runtime.activate(install, '/wrapper')
                    else:
                        runtime.activate(install, '/wrapper')
                self.assertEqual(destination.read_bytes(), b'previous public bytes\n' if fail else source.read_bytes())
                self.assertEqual(destination.stat().st_mode & 0o777, 0o644)
                self.assertEqual(skill_destination.read_bytes(), b'previous skill bytes\n' if fail else skill_source.read_bytes())
                self.assertEqual(skill_destination.stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads(provision.read_text()).get('review_skill_digest'), None if fail else digest)
                self.assertEqual((root / 'profile-cutover-public-backup/0').read_bytes(), b'previous public bytes\n')

    def test_unpinned_reviewer_snapshot_refused_before_cutover(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provision, database, install, status = self.activation_fixture(root)
            helper = root / 'bundle/helper.py'
            source = helper.parent / 'engineering/review-code.md'
            source.parent.mkdir(parents=True)
            source.write_bytes(b'not the pinned release')
            destination = root / 'review-code.md'
            destination.write_bytes(b'previous')
            with patch.object(runtime, '__file__', str(helper)), \
                 patch.object(runtime, 'PUBLIC_ASSETS', {'engineering/review-code.md': destination}), \
                 patch.object(runtime, 'PROVISION', provision), patch.object(runtime, 'DROPIN', root / 'missing'), \
                 patch.object(runtime, 'run_native', return_value=status), patch.object(runtime, 'systemctl') as ctl:
                with self.assertRaisesRegex(ValueError, 'pinned engineering release'):
                    runtime.activate(install, '/wrapper')
            install.compose.assert_not_called()
            ctl.assert_not_called()

    def test_active_guard_does_not_stop_workers_or_gateway(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provision, database, install, status = self.activation_fixture(root)
            with sqlite3.connect(database) as connection:
                connection.execute("INSERT INTO factory_attempts VALUES ('running')")
            with patch.object(runtime, 'PROVISION', provision), patch.object(runtime, 'DROPIN', root / 'missing'), \
                 patch.object(runtime, 'PUBLIC_ASSETS', {}), \
                 patch.object(runtime, 'run_native', return_value=status), patch.object(runtime, 'systemctl') as ctl, \
                 patch.object(runtime.subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps([{'Name': 'factory-fixture-bridge-data', 'Mountpoint': str(root)}]))):
                with self.assertRaises(ValueError):
                    runtime.activate(install, '/wrapper')
            install.compose.assert_not_called()
            ctl.assert_not_called()

    def test_archive_rejects_links_devices_and_escape(self):
        for name, kind in (('../escape', tarfile.REGTYPE), ('/escape', tarfile.REGTYPE),
                           ('link', tarfile.SYMTYPE), ('hardlink', tarfile.LNKTYPE), ('device', tarfile.CHRTYPE)):
            member = tarfile.TarInfo(name)
            member.type = kind
            with self.subTest(name=name), self.assertRaises(ValueError):
                runtime.archive_files(plugin_archive(member))

    def test_configure_updates_only_named_roles_and_stages_idempotently(self):
        config_module = ModuleType('hermes_cli.config')
        config_module.read_user_config_raw = lambda path: json.loads(path.read_text())
        config_module.atomic_config_write = lambda path, value: path.write_text(json.dumps(value))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            default = root / 'config.yaml'
            default_config = json.dumps({'mcp_servers': {'example': {'command': 'example'}},
                                         'browser': {'provider': 'remote'}, 'identity': 'default-sentinel'})
            default.write_text(default_config)
            for name in runtime.ROLES.values():
                profile = root / 'profiles' / name
                profile.mkdir(parents=True)
                (profile / 'config.yaml').write_text(json.dumps({'unrelated': 'preserved',
                    'model': {'base_url': 'https://example.invalid', 'default': 'before'},
                    'plugins': {'unrelated': 'preserve'}, 'auth': {'unrelated': 'preserve'},
                    'tools': {'tool_search': {'unrelated': 'preserve'}}}))
            payload = {'engineering': base64.b64encode(plugin_archive()).decode(),
                       'observer': {name: base64.b64encode(content).decode() for name, content in
                                    {'__init__.py': b'public observer', 'plugin.yaml': b'name: posthog'}.items()}}
            with patch.object(runtime, 'ROOT', root), patch.dict(sys.modules, {'hermes_cli.config': config_module}):
                first = runtime.configure(payload)
                self.assertEqual(runtime.configure(payload), first)
                inventory = runtime.inventory()
                self.assertTrue(inventory['profiles']['executor']['engineering_enabled'])
                self.assertFalse(inventory['profiles']['reviewer']['posthog_plugin_enabled'])
            self.assertEqual(default.read_text(), default_config)
            executor = root / 'profiles' / 'factory-executor'
            config = json.loads((executor / 'config.yaml').read_text())
            self.assertEqual(config['unrelated'], 'preserved')
            self.assertEqual(config['model']['base_url'], 'https://example.invalid')
            self.assertEqual(config['plugins']['unrelated'], 'preserve')
            self.assertEqual(config['mcp_servers'], {'example': {'command': 'example'}})
            self.assertNotIn('identity', config)
            release = (executor / 'plugins/driftward-engineering').resolve()
            (release / 'plugin.json').chmod(0o600)
            (release / 'plugin.json').write_text('tampered')
            with patch.object(runtime, 'ROOT', root), patch.dict(sys.modules, {'hermes_cli.config': config_module}), self.assertRaises(ValueError):
                runtime.configure(payload)

    def test_auth_move_is_atomic_refuses_overwrite_and_keeps_unrelated_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / 'factory-reviewer-auth'
            named = root / 'profiles/factory-independent-reviewer'
            legacy.mkdir()
            named.mkdir(parents=True)
            (legacy / 'auth.json').write_text('private sentinel')
            (legacy / 'auth.json').chmod(0o600)
            (legacy / 'unrelated').write_text('preserve')
            inode = (legacy / 'auth.json').stat().st_ino
            with patch.object(runtime, 'ROOT', root), patch.object(runtime, 'idle_runtime'):
                runtime.move_auth()
                self.assertFalse((legacy / 'auth.json').exists())
                self.assertEqual((named / 'auth.json').stat().st_ino, inode)
                self.assertEqual((legacy / 'unrelated').read_text(), 'preserve')
                runtime.move_auth(reverse=True)
                (named / 'auth.json').write_text('existing')
                with self.assertRaises(ValueError):
                    runtime.move_auth()

    def test_capabilities_reject_inline_credentials_and_preserve_runtime_references(self):
        module = ModuleType('hermes_cli.config')
        module.read_user_config_raw = lambda path: json.loads(path.read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'config.yaml'
            source.write_text(json.dumps({'mcp_servers': {'example': {'headers': {'Authorization': 'private-inline'}}}}))
            with patch.object(runtime, 'ROOT', root), patch.dict(sys.modules, {'hermes_cli.config': module}):
                with self.assertRaisesRegex(ValueError, 'inline credentials') as error:
                    runtime.default_capabilities()
                self.assertNotIn('private-inline', str(error.exception))
                source.write_text(json.dumps({'mcp_servers': {'example': {'env': {'API_KEY': '${EXAMPLE_RUNTIME_KEY}'}}},
                                             'auth': {'secret': 'never selected'}}))
                with patch.dict(os.environ, {'EXAMPLE_RUNTIME_KEY': 'private-runtime-value'}):
                    selected = runtime.default_capabilities()
                self.assertNotIn('auth', selected)
                self.assertEqual(selected['mcp_servers']['example']['env']['API_KEY'], '${EXAMPLE_RUNTIME_KEY}')
                self.assertNotIn('private-runtime-value', json.dumps(selected))
                with patch.dict(os.environ, {'EXAMPLE_RUNTIME_KEY': ''}), self.assertRaisesRegex(ValueError, 'unavailable'):
                    runtime.default_capabilities()

    def test_active_attempts_prevent_activation(self):
        connection = sqlite3.connect(':memory:')
        connection.executescript('CREATE TABLE factory_attempts(status TEXT); CREATE TABLE factory_execution_leases(released_at TEXT); CREATE TABLE bridge_jobs(state TEXT);')
        runtime.assert_quiescent(connection)
        for sql in ("INSERT INTO factory_attempts VALUES ('running')", 'INSERT INTO factory_execution_leases VALUES (NULL)',
                    "INSERT INTO bridge_jobs VALUES ('accepted')"):
            with connection:
                connection.execute(sql)
                with self.assertRaises(ValueError):
                    runtime.assert_quiescent(connection)
            connection.execute('DELETE FROM factory_attempts')
            connection.execute('DELETE FROM factory_execution_leases')
            connection.execute('DELETE FROM bridge_jobs')

    def test_native_transport_fixed_and_captures_output(self):
        with patch.object(runtime.subprocess, 'run', return_value=SimpleNamespace(stdout='{"idle":true}')) as run:
            self.assertEqual(runtime.run_native('/trusted/wrapper', 'idle', {}), {'idle': True})
        self.assertEqual(run.call_args.args[0], ['/trusted/wrapper', 'shell', '-c', 'exec python3 -'])
        self.assertTrue(run.call_args.kwargs['capture_output'])
        self.assertNotIn('DRIFTWARD_HERMES_ROOT', run.call_args.kwargs['env'])

    def test_unsupported_role_cannot_trigger_canary(self):
        with self.assertRaises(ValueError):
            runtime.dispatch(SimpleNamespace(), {}, '/wrapper', ['canary', 'default'])
