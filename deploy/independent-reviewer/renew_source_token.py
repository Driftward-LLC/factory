"""Root-only atomic source token renewal; no provider/platform key leaves native Hermes."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time

from source_token_broker import validate_response

ROOT = Path('/etc/driftward/factory-reviewer')


def private(location: Path, directory: bool = False) -> None:
    info = location.lstat()
    if (stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
            or (not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode))):
        raise ValueError('reviewer source credential paths must be private and root-owned')


def replace(location: Path, content: bytes) -> None:
    fd, name = tempfile.mkstemp(prefix=location.name + '.', dir=location.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, location)
        directory = os.open(location.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def broker() -> dict:
    code = Path(__file__).with_name('source_token_broker.py').read_text()
    result = subprocess.run(['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock', 'exec',
        '--workdir', '/opt/hermes', 'hermes-driftward-hermes-1', '/opt/hermes/.venv/bin/python', '-c', code],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=40, check=True,
        env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent'})
    if len(result.stdout) > 128 * 1024:
        raise ValueError('source credential response exceeds budget')
    return json.loads(result.stdout)


def renew(directory: Path = ROOT, issue=broker) -> dict:
    private(directory, True)
    token, metadata, lock = directory / 'source-token', directory / 'source-token-metadata.json', directory / 'source-token.lock'
    for location in [token, metadata, lock]:
        if location.exists() or location.is_symlink():
            private(location)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if token.exists() and metadata.exists():
            current = json.loads(metadata.read_text())
            if (time.time() + 1800 < current.get('expires_at_epoch', 0) <= time.time() + 3900
                    and current.get('token_sha256') == hashlib.sha256(token.read_bytes()).hexdigest()):
                return {'status': 'current', 'expires_at': current['expires_at']}
        value = validate_response(issue())
        content = value.pop('token').encode()
        value['token_sha256'] = hashlib.sha256(content).hexdigest()
        # A failed issuance/validation cannot alter the previous credential. The token rename is atomic.
        replace(metadata, (json.dumps(value, sort_keys=True) + '\n').encode())
        replace(token, content)
        return {'status': 'renewed', 'expires_at': value['expires_at']}
    finally:
        os.close(fd)


if __name__ == '__main__':
    if os.geteuid() != 0:
        sys.exit('Reviewer source renewal requires root')
    try:
        print(json.dumps(renew()))
    except Exception:
        sys.exit('Reviewer source renewal failed; inspect native App/configuration; no auth fallback used')
