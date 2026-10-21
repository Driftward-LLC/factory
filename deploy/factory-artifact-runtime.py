#!/usr/bin/env python3
"""Preserve verified legacy CI image ZIPs before explicit GitHub artifact pruning."""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
from itertools import islice
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile
import zlib

REPO = 'Driftward-LLC/factory'
ROOT = Path('/srv/factory-artifact-archive')
LAST_LEGACY = '2026-10-20T22:35:06Z'
HEX = r'[0-9a-f]{64}'
MAX_ZIP = 1024 ** 3
MAX_IMAGE = 4 * 1024 ** 3
HEADROOM = 12 * 1024 ** 3


class Refusal(ValueError):
    """Only fixed, operator-safe explanations may cross the CLI boundary."""


def require(ok, reason):
    if not ok:
        raise Refusal(reason)


def stamp(value):
    require(isinstance(value, str) and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ', value), 'invalid UTC timestamp')
    return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def sha(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def gh(endpoint, target=None, method='GET'):
    # Use the host's existing authenticated gh identity, never caller token/host overrides.
    env = {k: v for k, v in os.environ.items() if not k.startswith('GH_') and k != 'GITHUB_TOKEN'}
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(['/usr/bin/gh', 'api', '--hostname', 'github.com', '--method', method,
                                    f'repos/{REPO}/{endpoint}'], stdout=subprocess.PIPE, stderr=errors, env=env)
        try:
            size, chunks = 0, []
            deadline = time.monotonic() + 900
            selector = selectors.DefaultSelector()
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                require(time.monotonic() < deadline and selector.select(timeout=60), 'GitHub response timed out')
                block = os.read(process.stdout.fileno(), 1024 * 1024)
                if not block:
                    break
                size += len(block)
                require(size <= (MAX_ZIP if target else 2 * 1024 * 1024), 'GitHub response exceeds limit')
                if target:
                    target.write(block)
                else:
                    chunks.append(block)
            require(process.wait(timeout=30) == 0, 'GitHub API failed')
        finally:
            selector.close()
            process.stdout.close()
            if process.poll() is None:
                process.kill()
            process.wait()
        return json.loads(b''.join(chunks)) if not target and method == 'GET' else None


def snapshot(before):
    require(stamp(before) <= stamp(LAST_LEGACY), 'cutoff exceeds legacy boundary')
    rows = []
    for page in range(1, 11):
        value = gh(f'actions/artifacts?per_page=100&page={page}')
        artifacts = value['artifacts']
        require(isinstance(artifacts, list) and len(artifacts) <= 100, 'invalid artifact listing')
        for item in artifacts:
            match = re.fullmatch(r'factory-production-([0-9a-f]{40})', str(item.get('name', '')))
            if not match or item.get('expired') is True or stamp(item['created_at']) >= stamp(before):
                continue
            require(item.get('expired') is False and type(item.get('id')) is int and item['id'] > 0
                    and type(item.get('size_in_bytes')) is int and 0 < item['size_in_bytes'] <= MAX_ZIP
                    and re.fullmatch('sha256:' + HEX, str(item.get('digest', ''))), 'invalid artifact metadata')
            run_id = item['workflow_run']['id']
            require(type(run_id) is int and run_id > 0, 'invalid run ID')
            run = gh(f'actions/runs/{run_id}')
            require(run.get('id') == run_id and run.get('path') == '.github/workflows/hatchet-quality.yml'
                    and run.get('event') == 'push' and run.get('head_branch') == 'main'
                    and run.get('head_sha') == match[1] and run.get('status') == 'completed'
                    and run.get('conclusion') == 'success'
                    and run.get('repository', {}).get('full_name') == REPO
                    and run.get('head_repository', {}).get('full_name') == REPO, 'untrusted artifact source run')
            require(stamp(item['expires_at']) > datetime.now(timezone.utc), 'artifact already expired')
            rows.append({key: item[key] for key in ('id', 'name', 'size_in_bytes', 'digest', 'created_at', 'expires_at')}
                        | {'run_id': run_id, 'commit': match[1]})
        if len(artifacts) < 100:
            break
    else:
        raise Refusal('artifact inventory exceeds bounded pagination')
    require(len({row['id'] for row in rows}) == len(rows), 'duplicate artifact IDs')
    rows.sort(key=lambda row: (row['expires_at'], row['id']))
    result = {'repository': REPO, 'before': before, 'artifacts': rows}
    return result | {'snapshot_digest': digest(result), 'total_bytes': sum(row['size_in_bytes'] for row in rows)}


def protected(path, directory=False):
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise Refusal('archive copy, receipt or root missing; preserve before pruning') from error
    require(info.st_uid == 0 and stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600)
            and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode) and info.st_nlink == 1),
            'archive path must be root owned, private and regular')


def root_checked(create=False, durable=False):
    for parent in reversed(ROOT.parents):
        info = parent.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                'unprotected archive parent')
    if create:
        ROOT.mkdir(mode=0o700, exist_ok=True)
    protected(ROOT, True)
    if durable:
        # Recover durability even if an earlier creation stopped before its parent fsync.
        sync_directory(ROOT.parent)


def validate(path, row):
    try:
        return validate_zip(path, row)
    except (zipfile.BadZipFile, EOFError, zlib.error, RuntimeError) as error:
        raise Refusal('invalid or corrupt image ZIP') from error


def validate_zip(path, row):
    protected(path)
    require(path.stat().st_size == row['size_in_bytes'] and 'sha256:' + sha(path) == row['digest'], 'ZIP digest differs')
    with zipfile.ZipFile(path) as bundle:
        entries = bundle.infolist()
        require(len(entries) == 2 and {x.filename for x in entries} == {'factory-image.tar', 'deployment.json'},
                'unexpected ZIP members')
        for entry in entries:
            mode = entry.external_attr >> 16
            require(not entry.flag_bits & 1 and stat.S_IFMT(mode) in (0, stat.S_IFREG)
                    and 0 < entry.file_size <= (MAX_IMAGE if entry.filename == 'factory-image.tar' else 4096),
                    'unsafe ZIP member')
        metadata = json.loads(bundle.read('deployment.json'))
        require(isinstance(metadata, dict) and set(metadata) == {'format', 'commit', 'image', 'compatibility', 'archive_sha256'}
                and type(metadata['format']) is int and metadata['format'] == 1 and metadata['commit'] == row['commit']
                and re.fullmatch('sha256:' + HEX, str(metadata['image']))
                and re.fullmatch(HEX, str(metadata['compatibility']))
                and re.fullmatch(HEX, str(metadata['archive_sha256'])), 'invalid legacy deployment metadata')
        with bundle.open('factory-image.tar') as image:
            require(hashlib.file_digest(image, 'sha256').hexdigest() == metadata['archive_sha256'], 'image content digest differs')
    return metadata


def atomic(path, value):
    # All receipts persist before a destructive API call, including partial failures.
    if path.exists() or path.is_symlink():
        protected(path)
    fd, temporary = tempfile.mkstemp(prefix='.receipt-', dir=ROOT)
    try:
        with os.fdopen(fd, 'w') as output:
            json.dump(value, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        sync_directory(ROOT)
    finally:
        Path(temporary).unlink(missing_ok=True)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_receipt(path, row, metadata):
    protected(path)
    require(path.stat().st_size <= 64 * 1024, 'archive receipt exceeds limit')
    audit = json.loads(path.read_text())
    require(isinstance(audit, dict) and audit.get('artifact') == row and audit.get('metadata') == metadata
            and isinstance(audit.get('events'), list), 'archive receipt differs')
    return audit


def operate(action, view):
    rows = view['artifacts']
    if action == 'preserve':
        require(shutil.disk_usage(ROOT).free >= HEADROOM + sum(row['size_in_bytes'] for row in rows), 'insufficient archive disk headroom')
    # Validate the whole rollback set before the first deletion.
    if action == 'prune':
        for row in rows:
            metadata = validate(ROOT / f"{row['id']}.zip", row)
            read_receipt(ROOT / f"{row['id']}.json", row, metadata)
    completed = []
    for row in rows:
        path = ROOT / f"{row['id']}.zip"
        receipt = ROOT / f"{row['id']}.json"
        if action == 'preserve' and not (path.exists() or path.is_symlink()):
            fd, temporary = tempfile.mkstemp(prefix='.download-', dir=ROOT)
            try:
                with os.fdopen(fd, 'wb') as target:
                    gh(f"actions/artifacts/{row['id']}/zip", target)
                    target.flush()
                    os.fsync(target.fileno())
                validate(Path(temporary), row)
                os.replace(temporary, path)
                sync_directory(ROOT)
            finally:
                Path(temporary).unlink(missing_ok=True)
        metadata = validate(path, row)
        if action == 'preserve':
            if receipt.exists() or receipt.is_symlink():
                read_receipt(receipt, row, metadata)
            else:
                atomic(receipt, {'artifact': row, 'metadata': metadata, 'events': []})
        else:
            fresh = snapshot(view['before'])
            require(fresh['artifacts'] == [item for item in rows if item['id'] not in completed],
                    'artifact snapshot changed during pruning')
            audit = read_receipt(receipt, row, metadata)
            event = {'snapshot_digest': view['snapshot_digest'], 'time': datetime.now(timezone.utc).isoformat(), 'status': 'delete_intent'}
            audit['events'].append(event)
            atomic(receipt, audit)
            try:
                gh(f"actions/artifacts/{row['id']}", method='DELETE')
            except Exception:
                event['status'] = 'delete_failed_or_unknown'
                atomic(receipt, audit)
                raise
            event['status'] = 'deleted'
            atomic(receipt, audit)
        completed.append(row['id'])
    return {'action': action, 'snapshot_digest': view['snapshot_digest'], 'artifact_ids': completed, 'archive_root': str(ROOT)}


def status():
    if not ROOT.exists() and not ROOT.is_symlink():
        return {'archive_root': str(ROOT), 'count': 0, 'total_bytes': 0, 'artifacts': []}
    root_checked()
    entries = list(islice(ROOT.iterdir(), 2002))
    require(len(entries) <= 2001, 'archive inventory exceeds limit')
    receipts = {path.stem: path for path in entries if re.fullmatch(r'[1-9][0-9]*\.json', path.name)}
    images = {path.stem for path in entries if re.fullmatch(r'[1-9][0-9]*\.zip', path.name)}
    require(len(receipts) <= 1000 and set(receipts) == images, 'archive copy or receipt missing')
    rows = []
    for artifact_id, path in sorted(receipts.items(), key=lambda item: int(item[0])):
        protected(path)
        require(path.stat().st_size <= 64 * 1024, 'archive receipt exceeds limit')
        audit = json.loads(path.read_text())
        row = audit['artifact']
        require(row['id'] == int(artifact_id) and row['name'] == 'factory-production-' + row['commit']
                and re.fullmatch(r'[0-9a-f]{40}', row['commit']), 'archive receipt identity differs')
        metadata = validate(ROOT / f'{artifact_id}.zip', row)
        audit = read_receipt(path, row, metadata)
        state = audit['events'][-1]['status'] if audit['events'] else 'preserved'
        require(state in ('preserved', 'delete_intent', 'delete_failed_or_unknown', 'deleted'), 'invalid audit status')
        rows.append({'id': row['id'], 'size_in_bytes': row['size_in_bytes'], 'delete_status': state})
    return {'archive_root': str(ROOT), 'count': len(rows),
            'total_bytes': sum(row['size_in_bytes'] for row in rows), 'artifacts': rows}


def dispatch(args):
    if args == ['status']:
        return status()
    parser = argparse.ArgumentParser(prog='factory artifacts')
    parser.add_argument('action', choices=('preview', 'preserve', 'prune'))
    parser.add_argument('--before', required=True)
    parser.add_argument('--snapshot-digest')
    parser.add_argument('--yes', action='store_true')
    options = parser.parse_args(args)
    if options.action == 'preview':
        require(not options.yes and options.snapshot_digest is None, 'preview takes no approval arguments')
        return snapshot(options.before)
    require(options.yes and re.fullmatch(HEX, options.snapshot_digest or ''), 'snapshot digest and --yes required')
    root_checked(create=options.action == 'preserve', durable=True)
    lock = ROOT / '.lock'
    if not lock.exists():
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    protected(lock)
    with lock.open('rb') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        view = snapshot(options.before)
        require(view['snapshot_digest'] == options.snapshot_digest, 'artifact snapshot changed; preview again')
        return operate(options.action, view)
