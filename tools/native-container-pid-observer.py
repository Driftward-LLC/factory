"""Disposable acceptance observer; pidfds are polled, never used to signal."""
import json
import os
import select
import subprocess
import sys
import time


def capture(container):
    state = json.loads(subprocess.check_output(['docker', 'inspect', '--format', '{{json .State}}', container]))
    assert state['Running'] is True and state['Pid'] > 1
    namespace = os.stat(f"/proc/{state['Pid']}/ns/pid")
    assert namespace.st_ino != os.stat('/proc/self/ns/pid').st_ino
    rows = subprocess.check_output(['docker', 'top', container, '-eo', 'pid'], text=True).splitlines()
    assert rows[0].strip() == 'PID' and 3 <= len(rows) <= 129
    handles = []
    try:
        for row in rows[1:]:
            pid = int(row.strip())
            fd = os.pidfd_open(pid)
            handles.append(fd)
            assert os.stat(f'/proc/{pid}/ns/pid').st_ino == namespace.st_ino
        assert not select.select(handles, [], [], 0)[0]
        return handles
    except BaseException:
        for fd in handles:
            os.close(fd)
        raise


handles = []
try:
    target = capture(sys.argv[1]); handles.extend(target)
    control = capture(sys.argv[2]); handles.extend(control)
    print('ready', flush=True)
    deadline = time.monotonic() + 10
    while len(select.select(target, [], [], 0)[0]) != len(target) and time.monotonic() < deadline:
        time.sleep(.02)
    assert len(select.select(target, [], [], 0)[0]) == len(target)
    assert not select.select(control, [], [], 0)[0]
    print(json.dumps({'target_exited': len(target), 'control_alive': len(control)}), flush=True)
finally:
    for fd in handles:
        os.close(fd)
