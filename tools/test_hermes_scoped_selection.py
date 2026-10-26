"""Native target selection plus disposable children, NOT native termination proof."""
import ast
import hashlib
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
from typing import Optional
import unittest

FIXTURE = Path(__file__).parent / 'fixtures/hermes-kill-all-818c13be.py'


class NativeSelectionTests(unittest.TestCase):
    def test_raw_owner_filter_preserves_concurrent_and_reviewer_children(self):
        # Execute only this fingerprinted native method, with an explicit callback
        # limited to our Popen handles. No checkpoint/recovery/module imports.
        source = FIXTURE.read_bytes()
        self.assertEqual(hashlib.sha256(source).hexdigest(),
                         'c75110348d5155d4a7fa69ba78b487f66195604bdc5233299cbcb3847c6b0134')
        method = ast.parse(source).body[0]
        self.assertEqual(method.name, 'kill_all')
        namespace = {'Optional': Optional}
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(FIXTURE), 'exec'), namespace)
        children = {}
        rows = {}
        calls = []
        try:
            for name, owner, persistent in [('a', 'raw-owner-a', False), ('persistent-a', 'raw-owner-a', True),
                                             ('b', 'raw-owner-b', False), ('reviewer', 'review-owner', False)]:
                children[name] = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(30)'],
                                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                rows[name] = SimpleNamespace(id=name, owner_task_id=owner, task_id='shared-container',
                                             persist_on_release=persistent, exited=False, _scope_stop_pending=False)
            def stop(name, **kwargs):
                calls.append(name)
                children[name].terminate()
                children[name].wait(timeout=5)
                rows[name].exited = True
                return {'status': 'killed'}
            registry = SimpleNamespace(_running=rows, _finished={}, _lock=threading.Lock(), kill_process=stop,
                                       _LIFECYCLE_KILL_SOURCES=frozenset({'kill_all', 'gateway_turn_timeout', 'agent_close'}))
            kill_all = namespace['kill_all']
            self.assertEqual(kill_all(registry, 'shared-container', source='process.kill'), 0)
            self.assertEqual(kill_all(registry, 'raw-owner-a'), 1)
            self.assertIsNone(children['persistent-a'].poll())
            self.assertEqual(kill_all(registry, 'raw-owner-a', source='process.kill'), 1)
            self.assertEqual(calls, ['a', 'persistent-a'])
            self.assertIsNone(children['b'].poll())
            self.assertIsNone(children['reviewer'].poll())
            self.assertEqual(kill_all(registry, 'raw-owner-a', source='process.kill'), 0)
        finally:
            for process in children.values():
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=5)


if __name__ == '__main__':
    unittest.main()
