"""Synthetic proc metadata only; never inspects an installed runtime."""
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('lookup_runtime', Path(__file__).resolve().parents[1] / 'deploy/factory-profile-runtime.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
ATTEMPT = '11111111-1111-4111-8111-111111111111'
JOB = '22222222-2222-4222-8222-222222222222'
SECRET = 'PRIVATE_PROMPT_COOKIE_TOKEN'
ENV = (f'FACTORY_OBSERVABILITY_ATTEMPT_ID={ATTEMPT}\0'
       f'FACTORY_OBSERVABILITY_JOB_ID={JOB}\0TOKEN={SECRET}\0').encode()


class ProcessLookupTests(unittest.TestCase):
    def lookup(self, environments, *, fault=None, changing=False, limit=False, race=None, deadline=False):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for pid, environment in enumerate(environments, 100):
                directory = root / str(pid)
                directory.mkdir()
                (directory / 'environ').write_bytes(environment)
                (directory / 'stat').write_bytes(b'100 (private ) command) ' + b' '.join([b'S'] + [b'0'] * 18 + [b'12345']))
            opened = os.open
            stat_reads = 0
            env_reads = 0
            def fake_open(path, flags, **kwargs):
                nonlocal stat_reads, env_reads
                if isinstance(path, str) and path.startswith('/proc/'):
                    path = root / path.removeprefix('/proc/')
                if path == 'stat':
                    stat_reads += 1
                    if race == 'start' and stat_reads == 2:
                        (root / '100/stat').write_bytes(b'100 (name) ' + b' '.join([b'S'] + [b'0'] * 18 + [b'54321']))
                if path == 'environ':
                    env_reads += 1
                    if race == 'environment' and env_reads == 2:
                        (root / '100/environ').write_bytes(ENV.replace(ATTEMPT.encode(), JOB.encode()))
                if path == 'environ' and fault:
                    raise fault(SECRET)
                return opened(path, flags, **kwargs)
            readlink_count = 0
            def namespace(*args, **kwargs):
                nonlocal readlink_count
                readlink_count += 1
                return 'pid:[2]' if changing and readlink_count > 2 else 'pid:[1]'
            class Entries:
                def __enter__(self):
                    return iter(SimpleNamespace(name=str(pid)) for pid in
                                (range(100, 5000) if limit else range(100, 100 + len(environments))))
                def __exit__(self, *args):
                    pass
            with patch.object(runtime.os, 'open', side_effect=fake_open), \
                 patch.object(runtime.os, 'scandir', return_value=Entries()), \
                 patch.object(runtime.os, 'readlink', side_effect=namespace), \
                 patch.object(runtime.Path, 'read_text', return_value=ATTEMPT), \
                 patch.object(runtime.os, 'geteuid', return_value=0), \
                 patch.object(runtime.time, 'monotonic', side_effect=[0, 4] if deadline else None,
                              return_value=0):
                return runtime.execution_process_lookup({'attempt_id': ATTEMPT, 'job_id': JOB})

    def test_exact_both_ids_privacy_and_initial_profile_evidence(self):
        result = self.lookup([ENV, ENV + b'HERMES_HOME=/opt/data/profiles/factory-executor\0',
                              ENV + b'HERMES_HOME=/opt/data\0'])
        self.assertEqual(len(result['matches']), 3)
        self.assertEqual([row['profile_evidence'] for row in result['matches']],
                         ['initial_home_differs_or_missing', 'initial_home_matches', 'initial_home_differs_or_missing'])
        self.assertEqual(result['matches'][0]['start_ticks'], '12345')
        self.assertEqual(result['ownership'], 'not_established')
        self.assertFalse(result['stop_authorized'])
        self.assertNotIn(SECRET, json.dumps(result))
        self.assertNotIn('command', json.dumps(result))

    def test_job_only_attempt_only_wrong_ids_and_empty_environment_do_not_match(self):
        result = self.lookup([f'FACTORY_OBSERVABILITY_JOB_ID={JOB}\0'.encode(),
                              f'FACTORY_OBSERVABILITY_ATTEMPT_ID={ATTEMPT}\0'.encode(),
                              ENV.replace(ATTEMPT.encode(), JOB.encode()), b''])
        self.assertEqual(result['matches'], [])
        self.assertEqual(result['state'], 'no_correlated_process_observed')

    def test_duplicate_malformed_partial_and_oversize_are_unresolved(self):
        result = self.lookup([ENV + f'FACTORY_OBSERVABILITY_JOB_ID={JOB}\0'.encode(),
                              ENV + b'broken\0', ENV[:-1], ENV + b'x' * 131073])
        self.assertEqual(result['matches'], [])
        self.assertEqual(result['unresolved'], 4)
        self.assertNotIn(SECRET, json.dumps(result))

    def test_missing_permission_and_namespace_race_never_accept_candidate(self):
        for fault in (FileNotFoundError, PermissionError):
            result = self.lookup([ENV], fault=fault)
            self.assertEqual(result['unresolved'], 1)
            self.assertEqual(result['matches'], [])
        self.assertEqual(self.lookup([ENV], changing=True)['matches'], [])
        for race in ('start', 'environment'):
            result = self.lookup([ENV], race=race)
            self.assertEqual(result['matches'], [])
            self.assertEqual(result['unresolved'], 1)

    def test_scan_and_output_bounds_are_explicit(self):
        result = self.lookup([ENV] * 65)
        self.assertEqual(len(result['matches']), 64)
        self.assertTrue(result['limit_reached'])
        result = self.lookup([ENV], deadline=True)
        self.assertEqual(result['scanned'], 0)
        self.assertTrue(result['limit_reached'])
        result = self.lookup([], limit=True)
        self.assertEqual(result['scanned'], 4096)
        self.assertTrue(result['limit_reached'])

    def test_arbitrary_paths_ids_or_extra_payload_refused(self):
        for payload in ({'attempt_id': '../secret', 'job_id': JOB},
                        {'attempt_id': ATTEMPT, 'job_id': JOB, 'path': '/private'}):
            with self.assertRaises(ValueError):
                runtime.execution_process_lookup(payload)
        with patch.object(runtime.os, 'geteuid', return_value=1000), self.assertRaisesRegex(ValueError, 'requires root'):
            runtime.execution_process_lookup({'attempt_id': ATTEMPT, 'job_id': JOB})


if __name__ == '__main__':
    unittest.main()
