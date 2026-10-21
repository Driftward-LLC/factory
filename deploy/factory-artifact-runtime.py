#!/usr/bin/env python3
"""Inspect Factory CI storage and explicitly prune narrowly approved artifacts."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
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
MAX_PAGES = 10
MAX_ROUTINE_DELETES = 100
ROUTINE_NAMES = ('driftward-ui-storybook', 'factory-console-fixtures')


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


def artifact_listing():
    """Require a complete bounded listing before reporting totals or choosing deletions."""
    rows, total = [], None
    for page in range(1, MAX_PAGES + 1):
        value = gh(f'actions/artifacts?per_page=100&page={page}')
        require(isinstance(value, dict) and type(value.get('total_count')) is int
                and 0 <= value['total_count'] <= MAX_PAGES * 100,
                'artifact inventory exceeds limit or lacks total count')
        require(total is None or total == value['total_count'], 'artifact inventory changed during pagination')
        total = value['total_count']
        items = value.get('artifacts')
        require(isinstance(items, list) and len(items) <= 100, 'invalid artifact listing')
        for item in items:
            require(isinstance(item, dict) and type(item.get('id')) is int and item['id'] > 0
                    and isinstance(item.get('name'), str) and 0 < len(item['name']) <= 1024
                    and type(item.get('size_in_bytes')) is int and 0 <= item['size_in_bytes'] < 2 ** 63
                    and type(item.get('expired')) is bool, 'invalid artifact metadata')
            stamp(item.get('created_at'))
            stamp(item.get('expires_at'))
            run = item.get('workflow_run')
            require(isinstance(run, dict) and type(run.get('id')) is int and run['id'] > 0,
                    'invalid artifact run metadata')
        rows.extend(items)
        require(len(rows) <= total and len({item['id'] for item in rows}) == len(rows),
                'artifact inventory count or IDs differ')
        if len(items) < 100 or len(rows) == total:
            require(len(rows) == total, 'incomplete artifact inventory')
            return sorted(rows, key=lambda item: item['id'])
    raise Refusal('artifact inventory exceeds bounded pagination')


def label(value):
    # Names and branch labels are untrusted; expose no controls, URLs or terminal escapes.
    return re.sub(r'[^a-zA-Z0-9_. -]', '_', value)[:160] if isinstance(value, str) else None


def inventory():
    rows, groups = [], {}
    for item in artifact_listing():
        name = label(item['name'])
        run = item['workflow_run']
        rows.append({key: item[key] for key in ('id', 'size_in_bytes', 'created_at', 'expires_at', 'expired')}
                    | {'name': name, 'run_id': run['id'], 'head_branch': label(run.get('head_branch')),
                       'head_sha': run.get('head_sha') if re.fullmatch(r'[0-9a-f]{40}', str(run.get('head_sha'))) else None})
        group = groups.setdefault(item['name'], {'name': name, 'count': 0, 'total_bytes': 0})
        group['count'] += 1
        group['total_bytes'] += item['size_in_bytes']
    return {'repository': REPO, 'count': len(rows), 'total_bytes': sum(row['size_in_bytes'] for row in rows),
            'groups': sorted(groups.values(), key=lambda group: (-group['total_bytes'], group['name'])), 'artifacts': rows}


def routine_snapshot(before, runs=None):
    cutoff = stamp(before)
    require(cutoff <= datetime.now(timezone.utc) - timedelta(days=7), 'routine cutoff must be at least seven days old')
    items = [item for item in artifact_listing() if item['name'] in ROUTINE_NAMES]
    if runs is None:
        runs = {}
    trusted, protected_rows = [], []
    for item in items:
        run_id = item['workflow_run']['id']
        if run_id not in runs:
            runs[run_id] = gh(f'actions/runs/{run_id}')
        run = runs[run_id]
        require(isinstance(run, dict), 'invalid workflow run response')
        row = {key: item[key] for key in ('id', 'name', 'size_in_bytes', 'created_at', 'expires_at')}
        row['run_id'] = run_id
        artifact_run = item['workflow_run']
        source_ok = (type(run.get('id')) is int and run['id'] == run_id
                     and run.get('name') == 'Hatchet Quality'
                     and run.get('path') == '.github/workflows/hatchet-quality.yml'
                     and run.get('event') in ('push', 'pull_request')
                     and (run.get('event') != 'push' or run.get('head_branch') == 'main')
                     and run.get('repository', {}).get('full_name') == REPO
                     and run.get('head_repository', {}).get('full_name') == REPO
                     and isinstance(run.get('head_branch'), str) and 0 < len(run['head_branch']) <= 1024
                     and re.fullmatch(r'[0-9a-f]{40}', str(run.get('head_sha')))
                     and artifact_run.get('head_sha') == run['head_sha']
                     and artifact_run.get('head_branch') == run['head_branch']
                     and type(run.get('repository', {}).get('id')) is int
                     and artifact_run.get('repository_id') == run['repository']['id']
                     and type(run.get('head_repository', {}).get('id')) is int
                     and artifact_run.get('head_repository_id') == run['head_repository']['id'])
        if not source_ok:
            protected_rows.append(row | {'reason': 'untrusted_run'})
            continue
        row.update(head_branch=label(run['head_branch']), branch_digest=digest(run['head_branch']),
                   head_sha=run['head_sha'], event=run['event'], is_main=run['head_branch'] == 'main',
                   repository_id=run['repository']['id'], head_repository_id=run['head_repository']['id'])
        if run.get('status') != 'completed':
            protected_rows.append(row | {'reason': 'active_run'})
            continue
        # Bind reruns/completion changes and GitHub's immutable content digest to approval.
        require(type(run.get('run_attempt')) is int and run['run_attempt'] > 0
                and type(run.get('workflow_id')) is int and run['workflow_id'] > 0
                and run.get('conclusion') in ('success', 'failure', 'cancelled', 'skipped', 'timed_out',
                                               'action_required', 'neutral', 'stale', 'startup_failure'),
                'invalid completed run metadata')
        stamp(run.get('updated_at'))
        row.update(run_attempt=run['run_attempt'], workflow_id=run['workflow_id'],
                   run_updated_at=run['updated_at'], conclusion=run['conclusion'])
        if item['expired'] or stamp(item['expires_at']) <= datetime.now(timezone.utc):
            protected_rows.append(row | {'reason': 'expired'})
            continue
        if not re.fullmatch('sha256:' + HEX, str(item.get('digest', ''))):
            protected_rows.append(row | {'reason': 'missing_content_digest'})
            continue
        stamp(item.get('updated_at'))
        row.update(digest=item['digest'], updated_at=item['updated_at'])
        trusted.append(row)
    latest = {}
    for row in trusted:
        for key in ((row['name'], 'global'), (row['name'], 'main')) if row['is_main'] else ((row['name'], 'global'),):
            latest[key] = max(latest.get(key, ('', 0)), (row['created_at'], row['run_id']))
    selected = []
    for row in trusted:
        if (row['created_at'], row['run_id']) in (latest[(row['name'], 'global')], latest.get((row['name'], 'main'))):
            reason = 'latest_global_or_main_evidence'
        elif stamp(row['created_at']) >= cutoff or stamp(row['updated_at']) >= cutoff or stamp(row['run_updated_at']) >= cutoff:
            reason = 'younger_than_cutoff'
        else:
            selected.append(row)
            continue
        protected_rows.append(row | {'reason': reason})
    result = {'repository': REPO, 'policy': 'routine-ui-evidence-seven-days-v1', 'before': before,
              'delete_limit': MAX_ROUTINE_DELETES,
              'artifacts': sorted(selected, key=lambda row: row['id']),
              'protected_artifacts': sorted(protected_rows, key=lambda row: row['id'])}
    return result | {'snapshot_digest': digest(result), 'total_bytes': sum(row['size_in_bytes'] for row in selected)}


def routine_prune(view, runs):
    receipt = ROOT / f"routine-{view['snapshot_digest']}.json"
    require(not receipt.exists() and not receipt.is_symlink(), 'routine cleanup audit already exists; inspect it and preview again')
    audit = {'snapshot': view, 'events': []}
    completed = []
    for row in view['artifacts'][:MAX_ROUTINE_DELETES]:
        # Reuse only this operation's source checks; the current run is always fresh.
        runs.pop(row['run_id'], None)
        fresh = routine_snapshot(view['before'], runs)
        require(fresh['artifacts'] == [item for item in view['artifacts'] if item['id'] not in completed]
                and fresh['protected_artifacts'] == view['protected_artifacts'],
                'routine artifact snapshot changed during pruning')
        event = {'artifact_id': row['id'], 'time': datetime.now(timezone.utc).isoformat(), 'status': 'delete_intent'}
        audit['events'].append(event)
        atomic(receipt, audit)
        try:
            gh(f"actions/artifacts/{row['id']}", method='DELETE')
        except Exception:
            audit['events'].append(event | {'time': datetime.now(timezone.utc).isoformat(), 'status': 'delete_failed_or_unknown'})
            atomic(receipt, audit)
            raise
        audit['events'].append(event | {'time': datetime.now(timezone.utc).isoformat(), 'status': 'deleted'})
        atomic(receipt, audit)
        completed.append(row['id'])
    return {'action': 'routine_prune', 'snapshot_digest': view['snapshot_digest'], 'artifact_ids': completed,
            'total_bytes': sum(row['size_in_bytes'] for row in view['artifacts'] if row['id'] in completed),
            'remaining_count': len(view['artifacts']) - len(completed), 'archive_root': str(ROOT)}


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


@contextmanager
def archive_lock(create=False):
    root_checked(create=create, durable=True)
    lock = ROOT / '.lock'
    if not lock.exists():
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    protected(lock)
    with lock.open('rb') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def dispatch(args):
    if args == ['status']:
        return status()
    if args == ['inventory']:
        return inventory()
    routine = args[:1] == ['routine']
    if routine:
        args = args[1:]
    parser = argparse.ArgumentParser(prog='factory artifacts')
    parser.add_argument('action', choices=('preview', 'prune') if routine else ('preview', 'preserve', 'prune'))
    parser.add_argument('--before', required=True)
    parser.add_argument('--snapshot-digest')
    parser.add_argument('--yes', action='store_true')
    options = parser.parse_args(args)
    if options.action == 'preview':
        require(not options.yes and options.snapshot_digest is None, 'preview takes no approval arguments')
        return routine_snapshot(options.before) if routine else snapshot(options.before)
    require(options.yes and re.fullmatch(HEX, options.snapshot_digest or ''), 'snapshot digest and --yes required')
    with archive_lock(create=routine or options.action == 'preserve'):
        runs = {}
        view = routine_snapshot(options.before, runs) if routine else snapshot(options.before)
        require(view['snapshot_digest'] == options.snapshot_digest, 'artifact snapshot changed; preview again')
        return routine_prune(view, runs) if routine else operate(options.action, view)
