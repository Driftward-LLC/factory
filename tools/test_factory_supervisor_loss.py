"""Failure injection for the canonical disposable namespace proof; no live Docker."""
from contextlib import ExitStack
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from test_factory_native_canary import canary, binding


class SupervisorLossTests(unittest.TestCase):
    def exercise(self, failure=None):
        bound = binding()
        receipt, snapshots, calls = {}, [], []
        containers = ['a' * 64, 'b' * 64]
        processes = [Mock(), Mock()]
        killed = False
        created = 0
        def docker(args, **kwargs):
            nonlocal killed, created
            calls.append(args)
            if args[0] == 'create':
                created += 1
                if failure == 'ambiguous_create' and created == 2:
                    raise ValueError('SENSITIVE')
                return containers[created - 1]
            if args[0] == 'kill':
                if failure == 'kill':
                    raise ValueError('SENSITIVE')
                killed = True
            if args[0] == 'rm' and args[-1] == containers[0] and failure == 'cleanup':
                raise ValueError('SENSITIVE')
            return ''
        def ready_frames():
            for _ in containers:
                yield {'event': 'registered', 'ownership': {**bound, 'pid': 2}}
                yield {'event': 'fixture_spawn_admitted'}
                yield {'event': 'fixture_ready', 'foreground_pid': 3, 'escaped_pid': 4,
                       'background': [{'pid': 5, 'persistent': False}, {'pid': 6, 'persistent': True}]}
        frames = list(ready_frames())
        if failure == 'readiness':
            frames[2]['event'] = 'not_ready'
        if failure == 'binding':
            frames[0]['ownership']['fingerprint'] = 'b' * 64
        def selected(read, write, errors, timeout):
            if killed and (set(read) == {101, 102} or failure == 'control_exit'):
                return list(read), [], []
            return [], [], []
        def persist():
            snapshots.append(copy.deepcopy(receipt))
            proof = receipt['supervisor_loss']
            if failure == 'intent_write' and proof.get('kill_requested'):
                raise OSError('SENSITIVE')
        stopped = {'Id': containers[0], 'Image': 'sha256:' + 'c' * 64,
                   'State': {'Running': False, 'Status': 'exited', 'ExitCode': 137}}
        if failure == 'wrong_exit':
            stopped['State']['ExitCode'] = 0
        with ExitStack() as stack:
            stack.enter_context(patch.object(canary, 'docker', side_effect=docker))
            stack.enter_context(patch.object(canary, 'verify_created'))
            stack.enter_context(patch.object(canary.subprocess, 'Popen', side_effect=processes))
            stack.enter_context(patch.object(canary, 'send_frame'))
            stack.enter_context(patch.object(canary, 'read_frame', side_effect=frames))
            stack.enter_context(patch.object(canary, 'fixture_handles', side_effect=[([101, 102], (0, 1)),
                                                                                  ([201, 202], (0, 2))]))
            stack.enter_context(patch.object(canary.select, 'select', side_effect=selected))
            stack.enter_context(patch.object(canary, 'running_container'))
            stack.enter_context(patch.object(canary, 'inspect_json', return_value=[stopped]))
            stack.enter_context(patch.object(canary.os, 'close'))
            assets = dict.fromkeys(('main.py', 'owner.py', 'transport.py', 'driver.py'), 'fixed public asset')
            if failure:
                with self.assertRaises((ValueError, OSError)):
                    canary.supervisor_loss_checks(stopped['Image'], assets, bound, receipt, persist)
                self.assertEqual(receipt['supervisor_loss']['status'], 'unknown')
            else:
                canary.supervisor_loss_checks(stopped['Image'], assets, bound, receipt, persist)
        self.assertNotIn('SENSITIVE', json.dumps(receipt))
        return receipt['supervisor_loss'], snapshots, calls

    def test_target_exit_control_survival_and_cleanup_have_durable_intent(self):
        proof, snapshots, calls = self.exercise()
        self.assertEqual(proof['status'], 'passed')
        self.assertEqual(proof['completion'], 'not_established')
        self.assertFalse(proof['profile_reusable'])
        self.assertTrue(proof['target_processes_exited'])
        self.assertTrue(proof['unrelated_control_survived'])
        self.assertTrue(all(row['removed'] for row in proof['containers']))
        self.assertEqual([c for c in calls if c[0] == 'kill'], [['kill', '--signal=KILL', 'a' * 64]])
        before_kill = [s['supervisor_loss'] for s in snapshots if s['supervisor_loss'].get('kill_requested')
                       and s['supervisor_loss']['status'] == 'unknown']
        self.assertTrue(before_kill)
        self.assertTrue(all(c['ready'] for c in before_kill[0]['containers']))

    def test_failures_remain_unknown_and_cleanup_continues_for_each_created_id(self):
        for failure in ('ambiguous_create', 'binding', 'readiness', 'intent_write', 'kill',
                        'wrong_exit', 'control_exit', 'cleanup'):
            with self.subTest(failure=failure):
                proof, _, calls = self.exercise(failure)
                removed = [c[-1] for c in calls if c[0] == 'rm']
                self.assertEqual(removed, [c['id'] for c in proof['containers'] if c['created']])
                if failure in ('ambiguous_create', 'binding', 'readiness', 'intent_write'):
                    self.assertFalse(any(c[0] == 'kill' for c in calls))

    def test_polling_inventory_refuses_other_namespaces_missing_members_and_exited_tasks(self):
        row = {'State': {'Pid': 100}}
        for failure in ('namespace', 'missing', 'exited', 'same_as_host', 'oversize'):
            with self.subTest(failure=failure), ExitStack() as stack:
                stack.enter_context(patch.object(canary, 'running_container', return_value=row))
                inventory = 'PID\n100\n101\n' if failure != 'oversize' else 'PID\n' + '100\n' * 65
                stack.enter_context(patch.object(canary, 'docker', return_value=inventory))
                def metadata(path):
                    inode = 10 if str(path) == '/proc/self/ns/pid' else 20
                    if failure == 'namespace' and str(path) == '/proc/101/ns/pid': inode = 30
                    if failure == 'same_as_host': inode = 10
                    return SimpleNamespace(st_dev=1, st_ino=inode)
                stack.enter_context(patch.object(canary.os, 'stat', side_effect=metadata))
                opened = stack.enter_context(patch.object(canary.os, 'pidfd_open', side_effect=[50, 51]))
                closed = stack.enter_context(patch.object(canary.os, 'close'))
                stack.enter_context(patch.object(Path, 'read_text', side_effect=['NSpid:\t100\t1', 'NSpid:\t101\t2']))
                stack.enter_context(patch.object(canary.select, 'select', return_value=([50] if failure == 'exited' else [], [], [])))
                with self.assertRaises(ValueError):
                    canary.fixture_handles('a' * 64, 'sha256:' + 'c' * 64, [3] if failure == 'missing' else [2])
                self.assertEqual(closed.call_count, opened.call_count)

    def test_bounded_control_frames_refuse_eof_invalid_shape_and_timeout(self):
        process = Mock()
        for data in (b'', b'[]\n', b'x' * 65537):
            with self.subTest(data=data[:8]), patch.object(canary.select, 'select', return_value=([process.stdout], [], [])), \
                 patch.object(canary.os, 'read', side_effect=[bytes([b]) for b in data] + [b'']):
                with self.assertRaises(ValueError):
                    canary.read_frame(process, canary.time.monotonic() + 5)
        with self.assertRaises(ValueError):
            canary.read_frame(process, 0)


if __name__ == '__main__':
    unittest.main()
