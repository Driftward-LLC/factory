"""Synthetic-only checks for the root CLI's private executor evidence boundary."""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('diagnostics_runtime', SOURCE / 'deploy/factory-profile-runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
ATTEMPT = 'b0ac9017-1f27-49f9-9250-274d6b8b8b23'
JOB = 'a007ff3e-8062-4b8b-a8eb-ced9511c25a6'
LEASE = '34684470-7c66-4043-8987-011866e6f27d'
INVOCATION = JOB + ':' + LEASE + ':1'
SECRET = 'PRIVATE_PROMPT_TOKEN_COOKIE_SESSION'


class ExecutionDiagnosticsTests(unittest.TestCase):
    def fixture(self, root):
        database = root / 'bridge.db'
        lease = {'lease_id': LEASE, 'owner': SECRET, 'epoch': 1, 'fence_token': SECRET}
        payload = {'repo': 'Driftward-LLC/factory', 'factory_attempt_id': ATTEMPT,
                   'instruction': SECRET, 'source_delivery_id': 'delivery-after-invocation',
                   'source_event_keys': ['later-correlation']}
        job = {'job_id': JOB, 'idempotency_key': SECRET, 'requested_action': 'create_draft_pr',
               'branch': 'factory/eng-162', 'payload': payload}
        # Golden hashes produced by the journal writer's Node canonical()/SHA256.
        intent = {'fingerprint_version': 2,
                  'fingerprint': '369e3e26b731ac8f4a8fe3b471066923d822614cd33f60bbb6ea541e16b63c35',
                  'lease': lease}
        directory = root / 'invocations' / JOB
        directory.mkdir(parents=True, mode=0o700)
        directory.parent.chmod(0o700)
        self.write(directory / 'intent.json', intent)
        with sqlite3.connect(database) as connection:
            connection.executescript('''
                CREATE TABLE factory_attempts(attempt_id TEXT,job_id TEXT);
                CREATE TABLE bridge_jobs(job_id TEXT,idempotency_key TEXT,requested_action TEXT,branch TEXT,payload_json TEXT);
                CREATE TABLE factory_execution_leases(attempt_id TEXT,lease_id TEXT,owner TEXT,epoch INTEGER,fence_token TEXT);
                CREATE TABLE factory_attempt_events(event_id TEXT,attempt_id TEXT,event_type TEXT,details_json TEXT,created_at TEXT);
            ''')
            connection.execute('INSERT INTO factory_attempts VALUES (?,?)', (ATTEMPT, JOB))
            connection.execute('INSERT INTO bridge_jobs VALUES (?,?,?,?,?)',
                               (JOB, SECRET, job['requested_action'], job['branch'], json.dumps(payload)))
            connection.execute('INSERT INTO factory_execution_leases VALUES (?,?,?,?,?)', (ATTEMPT, *lease.values()))
            for index, kind in enumerate(('execution_backend_invoked', 'execution_backend_failed')):
                details = {'invocation_id': INVOCATION, 'reason_code': 'hermes_process_exit_nonzero',
                           'process_exit_code': 1, 'transport_status': 'grpc_failed_precondition',
                           'stdout': SECRET, 'stderr': SECRET, 'prompt': SECRET}
                connection.execute('INSERT INTO factory_attempt_events VALUES (?,?,?,?,?)',
                                   (str(index), ATTEMPT, kind, json.dumps(details), '2026-10-24T16:35:02Z'))
        return database, directory, intent

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def diagnose(self, database, directory, invocation=INVOCATION):
        original = os.fstat
        temporary_info = Path('/tmp').stat()
        def fixture_stat(descriptor):
            info = original(descriptor)
            values = list(info)
            values[4] = 0  # Synthetic fixture owner, independent of the CI user.
            if info.st_ino == temporary_info.st_ino and info.st_dev == temporary_info.st_dev:
                values[0] = stat.S_IFDIR | 0o755  # /tmp is only a fixture ancestor.
            return os.stat_result(values)
        with patch.object(runtime, 'INVOCATION_DIRECTORY', directory.parent), \
             patch.object(runtime, 'bridge_database', return_value=database), \
             patch.object(runtime.os, 'geteuid', return_value=0), \
             patch.object(runtime.os, 'fstat', side_effect=fixture_stat):
            return runtime.dispatch(SimpleNamespace(), {}, None,
                                    ['diagnose-execution', ATTEMPT, '--invocation-id', invocation])

    def test_exact_v2_binding_and_private_fields_never_leave_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            database, directory, _ = self.fixture(Path(temporary))
            result = self.diagnose(database, directory)
            self.assertNotIn(SECRET, json.dumps(result))
            self.assertEqual(result['journal']['state'], 'exact_binding_verified')
            self.assertFalse(result['journal']['result_present'])
            self.assertEqual(result['events'][1]['process_exit_code'], 1)
            self.assertEqual(result['events'][1]['transport_status'], 'grpc_failed_precondition')
            self.assertEqual(result['underlying_error'], 'unavailable_not_retained')
            self.assertFalse(result['recovery_authorized'])
            self.assertEqual(result['containment'], 'not_established')
            self.write(directory / 'result.json', {'stdout': SECRET})
            result = self.diagnose(database, directory)
            self.assertTrue(result['journal']['result_present'])
            self.assertNotIn(SECRET, json.dumps(result))

    def test_mismatched_attempt_job_lease_epoch_or_fingerprint_refused(self):
        for mismatch in ('lease', 'epoch', 'fingerprint', 'payload'):
            with self.subTest(mismatch=mismatch), tempfile.TemporaryDirectory() as temporary:
                database, directory, intent = self.fixture(Path(temporary))
                if mismatch == 'lease':
                    intent['lease']['fence_token'] = 'different'
                elif mismatch == 'epoch':
                    intent['lease']['epoch'] = 2
                elif mismatch == 'fingerprint':
                    intent['fingerprint'] = '0' * 64
                else:
                    with sqlite3.connect(database) as connection:
                        connection.execute("UPDATE bridge_jobs SET payload_json='{}'")
                self.write(directory / 'intent.json', intent)
                with self.assertRaises(ValueError):
                    self.diagnose(database, directory)
                with self.assertRaises(ValueError):
                    self.diagnose(database, directory, INVOCATION[:-1] + '2')

    def test_legacy_v1_binding(self):
        with tempfile.TemporaryDirectory() as temporary:
            database, directory, intent = self.fixture(Path(temporary))
            with sqlite3.connect(database) as connection:
                payload = json.loads(connection.execute('SELECT payload_json FROM bridge_jobs').fetchone()[0])
                payload = {key: value for key, value in payload.items() if not key.startswith('source_')}
                connection.execute('UPDATE bridge_jobs SET payload_json=?', (json.dumps(payload),))
            intent.pop('fingerprint_version')
            intent['fingerprint'] = 'f8f5f28ddee3fb8c9342679ce2cdd4ba7996a2191159d5380dbad6cf0c28e395'
            self.write(directory / 'intent.json', intent)
            self.assertEqual(self.diagnose(database, directory)['journal']['state'], 'exact_binding_verified')

    def test_unsafe_missing_or_oversize_artifacts_fail_closed(self):
        for kind in ('symlink', 'directory_symlink', 'oversize', 'public', 'hardlink', 'missing_intent'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as temporary:
                database, directory, _ = self.fixture(Path(temporary))
                intent = directory / 'intent.json'
                if kind == 'symlink':
                    intent.rename(directory / 'secret')
                    intent.symlink_to(directory / 'secret')
                elif kind == 'directory_symlink':
                    directory.rename(directory.parent / 'elsewhere')
                    directory.symlink_to(directory.parent / 'elsewhere', target_is_directory=True)
                elif kind == 'oversize':
                    intent.write_bytes(b'x' * 65537)
                elif kind == 'public':
                    intent.chmod(0o644)
                elif kind == 'hardlink':
                    os.link(intent, directory / 'other')
                else:
                    intent.unlink()
                with self.assertRaises((OSError, ValueError)):
                    self.diagnose(database, directory)

    def test_missing_journal_is_evidence_gap_and_arbitrary_reason_is_not_echoed(self):
        with tempfile.TemporaryDirectory() as temporary:
            database, directory, _ = self.fixture(Path(temporary))
            (directory / 'intent.json').unlink()
            directory.rmdir()
            with sqlite3.connect(database) as connection:
                connection.execute('UPDATE factory_attempt_events SET details_json=? WHERE event_type=?',
                                   (json.dumps({'invocation_id': INVOCATION, 'reason_code': SECRET,
                                                'process_exit_code': SECRET}), 'execution_backend_failed'))
            result = self.diagnose(database, directory)
            self.assertEqual(result['journal']['state'], 'not_found')
            self.assertEqual(result['events'][1]['reason_code'], 'unclassified')
            self.assertNotIn(SECRET, json.dumps(result))

    def test_root_and_exact_invocation_event_required(self):
        with patch.object(runtime.os, 'geteuid', return_value=1000), self.assertRaises(ValueError):
            runtime.diagnose_execution(None, [ATTEMPT, '--invocation-id', INVOCATION])
        with tempfile.TemporaryDirectory() as temporary:
            database, directory, _ = self.fixture(Path(temporary))
            with sqlite3.connect(database) as connection:
                connection.execute('DELETE FROM factory_attempt_events WHERE event_type=?', ('execution_backend_invoked',))
            with self.assertRaises(ValueError):
                self.diagnose(database, directory)


if __name__ == '__main__':
    unittest.main()
