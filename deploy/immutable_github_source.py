"""Bounded immutable Git objects via the executor's existing native Composio session.

Objects stay in a temporary bare repository. No checkout, credentials in Git,
repository configuration, or repository programs are involved.
"""
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import resource
import time
import subprocess
import zlib

SPEC = importlib.util.spec_from_file_location('factory_composio_read', Path(__file__).with_name('composio-github-read.py'))
READ = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(READ)
SHA = re.compile(r'[a-f0-9]{40}')
ENV = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'GIT_CONFIG_NOSYSTEM': '1',
       'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_COUNT': '0', 'GIT_TERMINAL_PROMPT': '0', 'LC_ALL': 'C'}
MAX_BYTES = 256 * 1024 * 1024
MAX_FILES = 20000


def sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValueError('Invalid immutable Git identity')
    return value


def path(value):
    if (not isinstance(value, str) or len(value.encode()) > 1024 or '\\' in value or '\x00' in value
            or any(part in {'', '.', '..', '.git'} for part in value.split('/'))):
        raise ValueError('Invalid immutable source path')
    return value


class Source:
    def __init__(self, repository, base, head, directory, client=None, output_limit=16*1024*1024):
        if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)
                or any(part in {'.', '..'} for part in repository.split('/'))):
            raise ValueError('Invalid repository')
        self.base, self.head = sha(base), sha(head)
        owner, repo = repository.split('/')
        self.args = {'owner': owner, 'repo': repo}
        self.client = client or READ.GithubRead()
        self.directory = Path(directory)
        if type(output_limit) is not int or not 0 < output_limit <= 16*1024*1024:
            raise ValueError('Invalid source output budget')
        self.output_limit = output_limit
        self.bytes = 0
        self.objects = set()
        self.git = ['/usr/bin/git', '--no-replace-objects', '-c', 'core.hooksPath=/dev/null',
                    '-c', 'core.fsmonitor=false', '--git-dir', str(self.directory)]
        self.command(['init', '--bare', '--quiet', '--template='])
        compared = self.client.one('GITHUB_COMPARE_TWO_COMMITS', {**self.args,
            'basehead': self.base + '...' + self.head, 'per_page': 1, 'page': 1})
        if compared['base_commit']['sha'] != self.base:
            raise ValueError('Comparison base mismatch')
        self.merge_base = sha(compared['merge_base_commit']['sha'])
        self.base_tree, self.base_entries = self.tree(self.merge_base)
        self.head_tree, self.head_entries = self.tree(self.head)

    def command(self, args):
        remaining = self.client.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('Source budget exhausted')
        # Git's output is bounded on disk before reading it into memory.
        import tempfile
        with tempfile.TemporaryFile() as output:
            def bound_output():
                resource.setrlimit(resource.RLIMIT_FSIZE, (self.output_limit + 1, self.output_limit + 1))
            subprocess.run([*self.git, *args], env=ENV, stdin=subprocess.DEVNULL,
                           stdout=output, stderr=subprocess.DEVNULL, check=True, timeout=remaining, preexec_fn=bound_output)
            if output.tell() > self.output_limit:
                raise OverflowError('Source output exceeds budget')
            output.seek(0)
            return output.read().decode('utf-8', errors='strict')

    def object(self, kind, raw, expected):
        expected = sha(expected)
        framed = kind.encode() + b' ' + str(len(raw)).encode() + b'\0' + raw
        if hashlib.sha1(framed).hexdigest() != expected:
            raise ValueError('Git object integrity mismatch')
        if expected in self.objects:
            return
        self.bytes += len(framed)
        if self.bytes > MAX_BYTES:
            raise ValueError('Source snapshot exceeds budget')
        target = self.directory / 'objects' / expected[:2] / expected[2:]
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(zlib.compress(framed))
        self.objects.add(expected)

    def tree(self, commit):
        metadata = self.client.one('GITHUB_GET_A_COMMIT', {**self.args, 'ref': sha(commit), 'per_page': 1, 'page': 1})
        if metadata['sha'] != commit:
            raise ValueError('Commit identity mismatch')
        root = sha(metadata['commit']['tree']['sha'])
        value = self.client.one('GITHUB_GET_A_TREE', {**self.args, 'tree_sha': root, 'recursive': True})
        if value.get('sha') != root or value.get('truncated') is not False:
            raise ValueError('Incomplete source tree')
        rows = value['tree']
        if not isinstance(rows, list) or len(rows) > MAX_FILES:
            raise ValueError('Source tree exceeds budget')
        entries, directories = {}, {'': root}
        children = {'': []}
        for row in rows:
            name = path(row['path'])
            if name in entries or name in directories:
                raise ValueError('Duplicate source path')
            mode, kind, identity = row['mode'], row['type'], sha(row['sha'])
            if (mode, kind) not in {('040000', 'tree'), ('100644', 'blob'), ('100755', 'blob'),
                                    ('120000', 'blob'), ('160000', 'commit')}:
                raise ValueError('Unsupported Git tree entry')
            parent, _, leaf = name.rpartition('/')
            children.setdefault(parent, []).append((leaf, mode, identity))
            if kind == 'tree':
                directories[name] = identity
                children.setdefault(name, [])
            else:
                size = row.get('size', 0) if kind == 'blob' else 0
                if type(size) is not int or size < 0 or size > MAX_BYTES:
                    raise ValueError('Invalid blob size')
                entries[name] = (mode, identity, size)
        if set(children) != set(directories):
            raise ValueError('Incomplete parent tree')
        for name, identity in directories.items():
            # Git sorts tree names as if a directory has a trailing slash.
            rows = sorted(children[name], key=lambda item: (item[0] + ('/' if item[1] == '040000' else '')).encode())
            raw = b''.join(mode.lstrip('0').encode() + b' ' + leaf.encode() + b'\0' + bytes.fromhex(child)
                           for leaf, mode, child in rows)
            self.object('tree', raw, identity)
        return root, entries

    def blobs(self, entries):
        wanted = {}
        for mode, identity, size in entries:
            if mode != '160000' and identity not in self.objects:
                if identity in wanted and wanted[identity] != size:
                    raise ValueError('Conflicting Git blob size')
                wanted[identity] = size
        if self.bytes + sum(wanted.values()) > MAX_BYTES:
            raise ValueError('Source snapshot exceeds budget')
        identities = list(wanted)
        for offset in range(0, len(identities), 16):
            batch = identities[offset:offset+16]
            values = self.client.call([('GITHUB_GET_A_BLOB', {**self.args, 'file_sha': identity}) for identity in batch])
            for identity, value in zip(batch, values):
                if value.get('sha') != identity or value.get('encoding') != 'base64' or value.get('size') != wanted[identity]:
                    raise ValueError('Blob identity mismatch')
                content = value['content']
                if not isinstance(content, str) or len(content) > ((wanted[identity] + 2)//3)*4*2 + 100:
                    raise ValueError('Blob response exceeds budget')
                raw = base64.b64decode(''.join(content.split()), validate=True)
                if len(raw) != wanted[identity]:
                    raise ValueError('Incomplete Git blob')
                self.object('blob', raw, identity)

    def read_file(self, location):
        entry = self.head_entries.get(path(location))
        if entry is None or entry[0] not in {'100644', '100755'} or entry[2] > 64000:
            raise ValueError('Source file unavailable or exceeds budget')
        self.blobs([entry])
        return self.command(['cat-file', 'blob', entry[1]])

    def diff(self):
        names = sorted(name for name in self.base_entries.keys() | self.head_entries.keys()
                       if self.base_entries.get(name) != self.head_entries.get(name))
        self.blobs([entry for name in names for entry in (self.base_entries.get(name), self.head_entries.get(name)) if entry])
        text = self.command(['diff', '--no-ext-diff', '--no-textconv', '--no-color', '--find-renames',
            '--ignore-submodules=none', '--full-index', '--src-prefix=a/', '--dst-prefix=b/', self.base_tree, self.head_tree, '--'])
        # Ask Git for the names too: rename presentation must match the diff.
        raw = self.command(['diff', '--no-ext-diff', '--no-textconv', '--find-renames', '--name-only', '-z', self.base_tree, self.head_tree, '--'])
        return {'files': [{'path': name} for name in raw.rstrip('\0').split('\0') if name], 'diffText': text}
