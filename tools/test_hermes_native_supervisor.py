"""Disposable Linux process trees only; no Hermes state or persisted PID signaling."""
import importlib.util
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ADAPTER = Path(__file__).resolve().parents[1] / 'deploy/experimental-native-executor'
spec = importlib.util.spec_from_file_location('native_supervisor', ADAPTER / 'owner.py')
owner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(owner)
BOUND = {'invocation_id': '00000000-0000-4000-8000-000000000001:00000000-0000-4000-8000-000000000002:1',
         'epoch': 1, 'fingerprint': 'a' * 64}


class SupervisorTests(unittest.TestCase):
    def test_signal_requires_current_child_authority_and_errors_remain_unknown(self):
        for failure in ('identity', 'signal', 'wait', 'deadline'):
            with self.subTest(failure=failure), patch.object(owner.os, 'waitpid', return_value=(0, 0)) as wait, \
                 patch.object(owner, 'child_ids', return_value=[123]), patch.object(owner.os, 'pidfd_open', return_value=99), \
                 patch.object(owner.os, 'waitid', return_value=None) as identity, \
                 patch.object(owner.signal, 'pidfd_send_signal') as send, patch.object(owner.os, 'close'):
                if failure == 'identity':
                    identity.side_effect = ChildProcessError()
                if failure == 'signal':
                    send.side_effect = PermissionError('SENSITIVE')
                if failure == 'wait':
                    wait.side_effect = PermissionError('SENSITIVE')
                result = owner.drain_children(timeout=0 if failure == 'deadline' else 1)
                self.assertFalse(result['descendants_drained'])
                self.assertTrue(result['errors'])
                self.assertNotIn('SENSITIVE', json.dumps(result))
                if failure != 'signal':
                    send.assert_not_called()

    def test_empty_snapshot_is_not_kernel_proof_and_duplicate_drain_is_stable(self):
        with patch.object(owner.os, 'waitpid', side_effect=[(0, 0), ChildProcessError()]), \
             patch.object(owner, 'child_ids', return_value=[]):
            self.assertEqual(owner.drain_children(), {'descendants_drained': True, 'reaped': 0, 'errors': []})
        with patch.object(owner.os, 'waitpid', side_effect=ChildProcessError()):
            self.assertEqual(owner.drain_children(), owner.drain_children())

    def test_supervisor_refuses_existing_children_or_threads_before_subreaping(self):
        for children, threads in (([123], 1), ([], 2)):
            with patch.object(owner, 'child_ids', return_value=children), \
                 patch.object(owner.threading, 'active_count', return_value=threads), \
                 patch.object(owner.ctypes, 'CDLL') as libc, self.assertRaises(owner.Refused):
                owner.prepare_supervisor()
            libc.assert_not_called()

    def test_missing_kernel_capability_refuses_before_fork(self):
        with patch.object(owner, 'child_ids', return_value=[]), patch.object(owner.signal, 'signal'), \
             patch.object(owner.ctypes, 'CDLL') as libc, patch.object(owner.os, 'fork') as fork:
            libc.return_value.prctl.return_value = -1
            with self.assertRaises(owner.Refused):
                owner.supervise(lambda: None, Path('/unused'), BOUND, lambda value: None)
            fork.assert_not_called()

    def test_initial_journal_failure_never_authorizes_child_effect(self):
        script = """
import json,pathlib,sys
sys.path.insert(0,sys.argv[1]); import owner
root=pathlib.Path(sys.argv[2])
def fail(path,value): raise OSError('SENSITIVE')
owner.atomic=fail
sys.exit(owner.supervise(lambda:(root/'effect').touch(),root,json.loads(sys.argv[3]),lambda v:print(json.dumps(v),flush=True)))
"""
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run([sys.executable, '-I', '-c', script, str(ADAPTER), temporary, json.dumps(BOUND)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertFalse((Path(temporary) / 'effect').exists())
            receipt = json.loads(result.stdout)['receipt']
            self.assertTrue(receipt['descendants_drained'])
            self.assertIn('supervisor_journal_write_failed', receipt['errors'])
            self.assertIn('recovery_journal_write_failed', receipt['errors'])
            self.assertNotIn('SENSITIVE', result.stdout + result.stderr)

    def test_sigkill_drains_nested_and_double_fork_children_without_touching_sibling(self):
        self.run_tree()

    def test_recovery_journal_failure_keeps_cleanup_but_refuses_success(self):
        self.run_tree(journal_failure=True)

    def run_tree(self, journal_failure=False):
        script = r'''
import json, os, pathlib, signal, sys, time
sys.path.insert(0, sys.argv[1])
import owner
root = pathlib.Path(sys.argv[2]); bound = json.loads(sys.argv[3])
def emit(value): print(json.dumps(value), flush=True)
def record(name): (root / name).write_text(str(os.getpid()))
def run():
    branch = os.fork()
    if branch == 0:
        if os.fork() == 0:
            os.setsid(); record('nested.pid'); signal.pause(); os._exit(0)
        record('branch.pid'); signal.pause(); os._exit(0)
    if os.fork() == 0:
        if os.fork() != 0: os._exit(0)
        os.setsid(); record('detached.pid'); signal.pause(); os._exit(0)
    files = ['nested.pid', 'branch.pid', 'detached.pid']
    deadline = time.monotonic() + 5
    while not all((root / p).exists() and (root / p).read_text() for p in files):
        if time.monotonic() > deadline: raise RuntimeError('fixture timeout')
        time.sleep(.01)
    emit({'ready': [os.getpid(), *[int((root / p).read_text()) for p in files]]})
    sys.stdin.readline()
    os.kill(os.getpid(), signal.SIGKILL)
if sys.argv[4] == 'fail':
    original = owner.atomic
    def atomic(path, value):
        if path.name == 'recovery.json': raise OSError('SENSITIVE')
        original(path, value)
    owner.atomic = atomic
sys.exit(owner.supervise(run, root, bound, emit))
'''
        with tempfile.TemporaryDirectory(prefix='factory-crash-supervisor-') as temporary:
            root = Path(temporary)
            control = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(30)'])
            process = subprocess.Popen([sys.executable, '-I', '-u', '-c', script, str(ADAPTER), str(root),
                                        json.dumps(BOUND), 'fail' if journal_failure else 'ok'],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            handles = []
            try:
                self.assertTrue(select.select([process.stdout], [], [], 8)[0])
                ready = json.loads(process.stdout.readline())
                self.assertEqual(len(ready['ready']), 4)
                handles = [os.pidfd_open(pid) for pid in ready['ready']]
                process.stdin.write('crash\n'); process.stdin.flush()
                output, errors = process.communicate(timeout=12)
                self.assertEqual(process.returncode, 1)
                self.assertEqual(errors, '')
                frame = json.loads(output)
                self.assertEqual(frame['event'], 'owner_reaped')
                receipt = frame['receipt']
                self.assertEqual(receipt['owner_exit_code'], -9)
                self.assertTrue(receipt['descendants_drained'])
                self.assertGreaterEqual(receipt['reaped'], 4)
                self.assertEqual(receipt['errors'], ['recovery_journal_write_failed'] if journal_failure else [])
                self.assertEqual(receipt['completion'], 'not_established')
                self.assertFalse(receipt['profile_reusable'])
                self.assertEqual(receipt['invocation_id'], BOUND['invocation_id'])
                self.assertEqual(set(select.select(handles, [], [], 0)[0]), set(handles))
                self.assertIsNone(control.poll())
                if not journal_failure:
                    self.assertEqual(json.loads((root / 'recovery.json').read_text()), receipt)
                self.assertTrue((root / 'supervisor.json').is_file())
                self.assertNotIn('SENSITIVE', output)
            finally:
                for fd in handles: os.close(fd)
                if process.stdin and not process.stdin.closed:
                    process.stdin.close()
                if process.poll() is None:
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill(); process.wait(timeout=5)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream and not stream.closed: stream.close()
                control.terminate(); control.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
