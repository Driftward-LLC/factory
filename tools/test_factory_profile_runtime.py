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
    def test_observer_upgrade_status_is_bounded_read_only_and_accepts_stopped_gateway(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install, database, snapshot, after, args = self.upgrade_fixture(root)
            request_id = args[-2]
            receipt = root / ('observer-upgrade-' + request_id + '.json')
            value = {'phase': 'requires_operator', 'stage': 'observer_upgrade', 'request_id': request_id,
                     'actor': 'root', 'snapshot_digest': '1' * 64, 'source_digest': '2' * 64, 'target_digest': '3' * 64,
                     'reason': 'PRIVATE_REASON', 'stop_errors': ['gateway_stop'], 'private_path': '/SECRET_PATH'}
            runtime.write_observer_receipt(receipt, value)
            before = receipt.read_bytes(), receipt.stat().st_mtime_ns, set(root.iterdir())
            native = {'links': {'posthog': {'digest': '2' * 64}}}
            with patch.object(runtime, 'run_native', return_value=native) as run, \
                 patch.object(runtime, 'systemctl') as ctl, patch.object(runtime, 'bridge_database') as db:
                result = runtime.observer_upgrade(install, '/wrapper', ['status', '--request-id', request_id])
            self.assertEqual(result['stage'], 'observer_upgrade')
            self.assertEqual(result['current_selected_observer_digest'], '2' * 64)
            self.assertTrue(result['read_only'])
            self.assertNotIn('PRIVATE_', json.dumps(result))
            self.assertNotIn('SECRET_PATH', json.dumps(result))
            self.assertEqual(before, (receipt.read_bytes(), receipt.stat().st_mtime_ns, set(root.iterdir())))
            run.assert_called_once_with('/wrapper', 'observer-snapshot', {})
            self.assertFalse(install.compose.called or ctl.called or db.called)
            with patch.object(runtime, 'run_native', side_effect=RuntimeError('SECRET_NATIVE_STDERR')):
                result = runtime.observer_upgrade(install, '/wrapper', ['status', '--request-id', request_id])
            self.assertEqual(result['native_snapshot_status'], 'unavailable')
            self.assertIsNone(result['current_selected_observer_digest'])
            self.assertNotIn('SECRET_', json.dumps(result))

    def test_observer_upgrade_status_refuses_unsafe_receipt_and_approval_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install, database, snapshot, after, args = self.upgrade_fixture(root)
            request_id = args[-2]
            status_args = ['status', '--request-id', request_id]
            with patch.object(runtime, 'run_native') as native:
                result = runtime.observer_upgrade(install, '/wrapper', status_args)
                self.assertEqual(result['phase'], 'not_started')
                native.assert_not_called()
                for suffix in (['--yes'], ['--snapshot-digest', 'a' * 64], ['--reason', 'PRIVATE_REASON']):
                    with self.assertRaises(ValueError):
                        runtime.observer_upgrade(install, '/wrapper', status_args + suffix)
                receipt = root / ('observer-upgrade-' + request_id + '.json')
                receipt.write_text('{}')
                for mode in (0o644, 0o600):
                    receipt.chmod(mode)
                    with self.assertRaises(ValueError):
                        runtime.observer_upgrade(install, '/wrapper', status_args)
                receipt.unlink()
                receipt.symlink_to(database)
                with self.assertRaises(ValueError):
                    runtime.observer_upgrade(install, '/wrapper', status_args)

    def upgrade_fixture(self, root):
        database = root / 'bridge.db'
        with sqlite3.connect(database) as connection:
            connection.executescript('CREATE TABLE factory_attempts(status TEXT); CREATE TABLE factory_execution_leases(released_at TEXT); CREATE TABLE bridge_jobs(state TEXT);')
        rows = [{'Service': 'bridge-executor-worker', 'State': 'exited'},
                {'Service': 'execution-dispatch-worker', 'State': 'running'}]
        def compose(*args, **kwargs):
            if args[0] == 'stop':
                rows[1]['State'] = 'exited'
            if args[0] == 'up':
                rows[1]['State'] = 'running'
        install = SimpleNamespace(directory=root, project='fixture', service_rows=lambda: rows, compose=Mock(side_effect=compose))
        old = 'a' * 64
        native = {'protected': 'protected-state', 'links': {
            'posthog': {'target': '/releases/' + old, 'digest': old},
            'driftward-engineering': {'target': '/engineering/' + runtime.COMMIT, 'digest': 'engineering'}}}
        target = runtime.tree_digest(runtime.observer_files(runtime.observer_payload()))
        snapshot = {'native': native, 'target_digest': target, 'host_inputs': 'host',
                    'services': [('bridge-executor-worker', 'exited'), ('execution-dispatch-worker', 'running')]}
        after = json.loads(json.dumps(native))
        after['links']['posthog'] = {'target': '/releases/' + target, 'digest': target}
        args = ['apply', '--snapshot-digest', runtime.recovery_digest(snapshot), '--reason', 'Reviewed observer upgrade',
                '--request-id', '12345678-1234-4234-8234-123456789abc', '--yes']
        return install, database, snapshot, after, args

    def test_observer_upgrade_preview_is_read_only_and_apply_preserves_pause_and_consumes_request(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install, database, snapshot, after, args = self.upgrade_fixture(root)
            before = set(root.iterdir())
            with patch.object(runtime, 'observer_upgrade_snapshot', return_value=snapshot), \
                 patch.object(runtime, 'observer_host_digest', return_value='host'), \
                 patch.object(runtime, 'bridge_database', return_value=database), \
                 patch.object(runtime, 'gateway_status', side_effect=[{'ActiveState': 'inactive'}, {'ActiveState': 'active'}]), \
                 patch.object(runtime, 'gateway_socket_listening', return_value=True), \
                 patch.object(runtime, 'run_native', return_value=after) as native, patch.object(runtime, 'systemctl'):
                preview = runtime.observer_upgrade(install, '/wrapper', ['preview'])
                self.assertTrue(preview['read_only'])
                self.assertEqual(before, set(root.iterdir()))
                self.assertFalse(install.compose.called)
                result = runtime.observer_upgrade(install, '/wrapper', args)
                self.assertEqual(result['paused_execution'], ['bridge-executor-worker'])
                with self.assertRaisesRegex(ValueError, 'already consumed'):
                    runtime.observer_upgrade(install, '/wrapper', args)
                self.assertEqual(native.call_count, 1)
            self.assertEqual(install.service_rows()[0]['State'], 'exited')
            self.assertTrue(all('bridge-executor-worker' not in call.args for call in install.compose.call_args_list))
            receipt = json.loads((root / 'observer-upgrade-12345678-1234-4234-8234-123456789abc.json').read_text())
            self.assertEqual(receipt['phase'], 'complete')
            self.assertEqual(receipt['actor'], 'root')

    def test_observer_upgrade_stale_snapshot_rejected_before_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install, database, snapshot, after, args = self.upgrade_fixture(root)
            args[2] = '0' * 64
            with patch.object(runtime, 'observer_upgrade_snapshot', return_value=snapshot), \
                 patch.object(runtime, 'run_native') as native, self.assertRaisesRegex(ValueError, 'snapshot changed'):
                runtime.observer_upgrade(install, '/wrapper', args)
            self.assertFalse(native.called)
            self.assertFalse(install.compose.called)
            self.assertFalse(list(root.glob('observer-upgrade-*')))

    def test_observer_upgrade_failure_remains_paused_and_request_cannot_be_retried(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install, database, snapshot, after, args = self.upgrade_fixture(root)
            with patch.object(runtime, 'observer_upgrade_snapshot', return_value=snapshot), \
                 patch.object(runtime, 'observer_host_digest', return_value='host'), \
                 patch.object(runtime, 'bridge_database', return_value=database), \
                 patch.object(runtime, 'gateway_status', return_value={'ActiveState': 'inactive'}), \
                 patch.object(runtime, 'run_native', side_effect=RuntimeError('PRIVATE_DIAGNOSTIC')), \
                 patch.object(runtime, 'systemctl'):
                with self.assertRaisesRegex(ValueError, 'inspect private receipt'):
                    runtime.observer_upgrade(install, '/wrapper', args)
                with self.assertRaisesRegex(ValueError, 'already consumed'):
                    runtime.observer_upgrade(install, '/wrapper', args)
            receipt = (root / 'observer-upgrade-12345678-1234-4234-8234-123456789abc.json').read_text()
            self.assertNotIn('PRIVATE_DIAGNOSTIC', receipt)
            self.assertEqual(json.loads(receipt)['phase'], 'requires_operator')
            self.assertTrue(all(row['State'] == 'exited' for row in install.service_rows()))
            self.assertFalse(any(call.args[0] == 'up' for call in install.compose.call_args_list))

    def test_observer_upgrade_rechecks_host_state_after_stopping_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            install, database, snapshot, after, args = self.upgrade_fixture(Path(temporary))
            with patch.object(runtime, 'observer_upgrade_snapshot', return_value=snapshot), \
                 patch.object(runtime, 'observer_host_digest', return_value='changed'), \
                 patch.object(runtime, 'bridge_database', return_value=database), \
                 patch.object(runtime, 'gateway_status', return_value={'ActiveState': 'inactive'}), \
                 patch.object(runtime, 'run_native') as native, patch.object(runtime, 'systemctl'), \
                 self.assertRaisesRegex(ValueError, 'inspect private receipt'):
                runtime.observer_upgrade(install, '/wrapper', args)
            self.assertFalse(native.called)

    def test_observer_upgrade_fsync_failure_prevents_any_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            install, database, snapshot, after, args = self.upgrade_fixture(Path(temporary))
            with patch.object(runtime, 'observer_upgrade_snapshot', return_value=snapshot), \
                 patch.object(runtime, 'sync_directory', side_effect=OSError('fixture fsync failure')), \
                 patch.object(runtime, 'run_native') as native, patch.object(runtime, 'systemctl') as ctl, \
                 self.assertRaises(OSError):
                runtime.observer_upgrade(install, '/wrapper', args)
            self.assertFalse(native.called)
            self.assertFalse(install.compose.called)
            self.assertFalse(ctl.called)

    def test_observer_upgrade_unhealthy_restore_restops_and_retains_consumed_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install, database, snapshot, after, args = self.upgrade_fixture(root)
            with patch.object(runtime, 'observer_upgrade_snapshot', return_value=snapshot), \
                 patch.object(runtime, 'observer_host_digest', return_value='host'), \
                 patch.object(runtime, 'bridge_database', return_value=database), \
                 patch.object(runtime, 'gateway_status', side_effect=[{'ActiveState': 'inactive'}, {'ActiveState': 'active'}]), \
                 patch.object(runtime, 'gateway_socket_listening', return_value=False), \
                 patch.object(runtime, 'run_native', return_value=after), patch.object(runtime, 'systemctl'), \
                 self.assertRaisesRegex(ValueError, 'inspect private receipt'):
                runtime.observer_upgrade(install, '/wrapper', args)
            self.assertTrue(all(row['State'] == 'exited' for row in install.service_rows()))
            receipt = json.loads((root / 'observer-upgrade-12345678-1234-4234-8234-123456789abc.json').read_text())
            self.assertEqual(receipt['phase'], 'requires_operator')
            self.assertEqual(receipt['stage'], 'restored_health')

    def test_explicit_runtime_owner_does_not_relax_managed_artifact_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'config.yaml'
            path.write_text('synthetic')
            actual = path.stat().st_uid
            with patch.object(runtime.os, 'geteuid', return_value=actual + 1):
                with self.assertRaises(ValueError):
                    runtime.checked(path)
                self.assertEqual(runtime.checked(path, owner=actual), path)
            link = Path(temporary) / 'link'
            link.symlink_to(path)
            with self.assertRaises(ValueError):
                runtime.checked(link, owner=actual)

    def test_runtime_identity_rejects_writable_directory_and_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'data'
            root.mkdir(mode=0o777)
            root.chmod(0o777)
            with patch.object(runtime, 'ROOT', root), self.assertRaises(ValueError):
                runtime.runtime_identity()
            root.chmod(0o755)
            link = Path(temporary) / 'alias'
            link.symlink_to(root)
            with patch.object(runtime, 'ROOT', link), self.assertRaises(ValueError):
                runtime.runtime_identity()

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
            health_checks = []
            def native(wrapper, command, payload):
                calls.append(command)
                return status if command == 'status' else {}
            def ctl(*args):
                if args[0] == 'is-active':
                    health_checks.append(args)
                    if len(health_checks) == 1:
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

    def test_failure_stage_is_sanitized_and_failed_gateway_is_not_healthy_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provision, database, install, status = self.activation_fixture(root)
            install.compose.side_effect = [RuntimeError('SECRET private diagnostic'), None, None]
            def ctl(*args):
                if args[0] == 'is-active':
                    raise RuntimeError('SECRET gateway diagnostic')
            with patch.object(runtime, 'PROVISION', provision), patch.object(runtime, 'DROPIN', root/'dropin'), \
                 patch.object(runtime, 'PUBLIC_ASSETS', {}), patch.object(runtime, 'systemctl', side_effect=ctl), \
                 patch.object(runtime, 'run_native', return_value=status), \
                 patch.object(runtime.subprocess, 'run', return_value=SimpleNamespace(stdout=json.dumps([{'Name': 'factory-fixture-bridge-data', 'Mountpoint': str(root)}]))):
                with self.assertRaises(ValueError):
                    runtime.activate(install, '/wrapper')
            receipt = (root/'profile-cutover.json').read_text()
            self.assertNotIn('SECRET', receipt)
            self.assertEqual(json.loads(receipt), {'phase':'requires_operator','stage':'workers_stop',
                                                  'rollback_stage':'gateway_health','services':['execution-dispatch-worker']})

    def test_status_returns_bounded_cutover_and_gateway_health_without_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            provision=root/'reviewer.json'
            provision.write_text('{}')
            receipt=root/'profile-cutover.json'
            receipt.write_text(json.dumps({'phase':'rolled_back','stage':'native_idle','secret':'never expose'}))
            receipt.chmod(0o600)
            before={p:p.read_bytes() for p in (provision,receipt)}
            outputs=[SimpleNamespace(stdout='DRIFTWARD_HERMES_PROFILE=factory-executor'),
                     SimpleNamespace(stdout='ActiveState=activating\nSubState=auto-restart\nResult=exit-code\nExecMainStatus=226\nSecret=never expose')]
            with patch.object(runtime, 'PROVISION', provision), patch.object(runtime, 'run_native', return_value={}), \
                 patch.object(runtime.subprocess, 'run', side_effect=outputs), patch.object(runtime, 'write_private') as write:
                result=runtime.dispatch(SimpleNamespace(directory=root),{},'/wrapper',['status'])
            self.assertEqual(result['cutover'],{'phase':'rolled_back','stage':'native_idle','rollback_stage':'unknown'})
            self.assertEqual(result['gateway_service'],{'ActiveState':'activating','SubState':'auto-restart','Result':'exit-code','ExecMainStatus':226})
            self.assertNotIn('never expose',json.dumps(result))
            self.assertEqual(before,{p:p.read_bytes() for p in before})
            write.assert_not_called()

    def test_gateway_runtime_directory_is_created_before_service_namespace(self):
        source=(ROOT/'deploy/systemd/driftward-hermes-factory-gateway.service').read_text()
        for directive in ('RuntimeDirectory=driftward-hermes-factory','RuntimeDirectoryPreserve=yes'):
            self.assertIn(directive,source)
            self.assertIn(directive.encode(),runtime.PROFILE_DROPIN)

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
            ctl.assert_called_once_with('is-active', '--quiet', runtime.SERVICE)

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
        auth_module = ModuleType('hermes_cli.auth')
        auth_module.get_codex_auth_status = lambda: {'logged_in': True, 'auth_mode': 'chatgpt',
                                                   'base_url': 'https://chatgpt.com/backend-api/codex'}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            default = root / 'config.yaml'
            (root / 'factory-reviewer-auth').mkdir(mode=0o700)
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
            with patch.object(runtime, 'ROOT', root), patch.dict(sys.modules, {'hermes_cli.config': config_module, 'hermes_cli.auth': auth_module}):
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

    def test_mcp_transport_requires_runtime_refs_except_harmless_standard_literals(self):
        module = ModuleType('hermes_cli.config')
        module.read_user_config_raw = lambda path: json.loads(path.read_text())
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'config.yaml'
            with patch.object(runtime, 'ROOT', root), patch.dict(sys.modules, {'hermes_cli.config': module}), \
                 patch.dict(os.environ, {'RUNTIME_CREDENTIAL': 'private-runtime-value'}):
                for context, key in (('env', 'AWS_SECRET_ACCESS_KEY'), ('env', 'GITHUB_APP_PRIVATE_KEY'),
                                     ('env', 'AWS_ACCESS_KEY_ID'), ('env', 'CUSTOM_AUTH'),
                                     ('headers', 'X-Custom-Auth'), ('options', 'apiKey'),
                                     ('options', 'private_key'), ('options', 'access-key')):
                    with self.subTest(context=context, key=key):
                        source.write_text(json.dumps({'mcp_servers': {'example': {context: {key: 'private-inline'}}}}))
                        with self.assertRaisesRegex(ValueError, 'inline credentials'):
                            runtime.default_capabilities()
                        source.write_text(json.dumps({'mcp_servers': {'example': {context: {key: '${RUNTIME_CREDENTIAL}'}}}}))
                        selected = runtime.default_capabilities()
                        self.assertEqual(selected['mcp_servers']['example'][context][key], '${RUNTIME_CREDENTIAL}')
                        self.assertNotIn('private-runtime-value', json.dumps(selected))
                safe = {'mcp_servers': {'example': {'env': {'NODE_ENV': 'production', 'LOG_LEVEL': 'info'},
                         'headers': {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream'}}}}
                source.write_text(json.dumps(safe))
                self.assertEqual(runtime.default_capabilities(), safe)
                source.write_text(json.dumps({'mcp_servers': {'example': {'env': {'NODE_ENV': 'private-inline'}}}}))
                with self.assertRaisesRegex(ValueError, 'inline credentials'):
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
