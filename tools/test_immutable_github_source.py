import base64
import copy
import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy'))
from immutable_github_source import Source


def identity(kind, content):
    return hashlib.sha1(kind.encode() + b' ' + str(len(content)).encode() + b'\0' + content).hexdigest()


class Client:
    def __init__(self):
        self.deadline = time.monotonic() + 30
        self.base, self.head = 'a'*40, 'b'*40
        self.blob_data = {}
        self.trees = {}
        self.calls = []
        for commit, text in [(self.base, b'old\n'), (self.head, b'\nnew\n')]:
            blob = identity('blob', text)
            link = b'../../private-credentials'
            link_sha = identity('blob', link)
            tree = identity('tree', b'100644 app.py\0' + bytes.fromhex(blob) + b'120000 link\0' + bytes.fromhex(link_sha))
            self.blob_data[blob] = text
            self.blob_data[link_sha] = link
            self.trees[commit] = {'sha': tree, 'truncated': False, 'tree': [
                {'path': 'app.py', 'mode': '100644', 'type': 'blob', 'sha': blob, 'size': len(text)},
                {'path': 'link', 'mode': '120000', 'type': 'blob', 'sha': link_sha, 'size': len(link)}]}

    def one(self, slug, args):
        self.calls.append((slug, args))
        if slug == 'GITHUB_COMPARE_TWO_COMMITS':
            return {'base_commit': {'sha': self.base}, 'merge_base_commit': {'sha': self.base}}
        if slug == 'GITHUB_GET_A_COMMIT':
            return {'sha': args['ref'], 'commit': {'tree': {'sha': self.trees[args['ref']]['sha']}}}
        if slug == 'GITHUB_GET_A_TREE':
            return copy.deepcopy(next(v for v in self.trees.values() if v['sha'] == args['tree_sha']))
        if slug == 'GITHUB_GET_A_BLOB':
            raw = self.blob_data[args['file_sha']]
            return {'sha': args['file_sha'], 'size': len(raw), 'encoding': 'base64', 'content': base64.b64encode(raw).decode()}
        raise AssertionError(slug)

    def call(self, items):
        return [self.one(slug, args) for slug, args in items]


class PrivateSourceTests(unittest.TestCase):
    def source(self, directory, client=None):
        client = client or Client()
        return Source('example/private', client.base, client.head, directory, client)

    def test_real_git_diff_and_lazy_verified_file_read(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.source(directory)
            self.assertEqual(source.read_file('app.py'), '\nnew\n')
            self.assertEqual(source.diff()['files'], [{'path': 'app.py'}])
            self.assertIn('-old\n+\n+new\n', source.diff()['diffText'])
            self.assertFalse((Path(directory) / 'app.py').exists())
            self.assertFalse(any(slug == 'GITHUB_GET_A_BLOB' and args['file_sha'] == identity('blob', b'../../private-credentials')
                                 for slug, args in source.client.calls))
            for location in ['link', '../auth', '/opt/data/auth', '.git/config', 'a//b', 'a\\b']:
                with self.subTest(location=location), self.assertRaises(ValueError):
                    source.read_file(location)

    def test_truncated_malformed_duplicate_and_corrupt_trees_fail_closed(self):
        for mutation in ['truncated', 'duplicate', 'path', 'mode', 'hash', 'parent']:
            client = Client()
            tree = client.trees[client.head]
            if mutation == 'truncated': tree['truncated'] = True
            if mutation == 'duplicate': tree['tree'].append(tree['tree'][0])
            if mutation == 'path': tree['tree'][0]['path'] = '../app.py'
            if mutation == 'mode': tree['tree'][0]['mode'] = '100664'
            if mutation == 'hash': tree['tree'][0]['sha'] = 'c'*40
            if mutation == 'parent': tree['tree'][0]['path'] = 'missing/app.py'
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
                self.source(directory, client)

    def test_corrupt_blob_and_missing_bytes_are_rejected(self):
        for raw in [b'evil\n', b'new\n']:
            with tempfile.TemporaryDirectory() as directory:
                client = Client()
                source = self.source(directory, client)
                client.blob_data[source.head_entries['app.py'][1]] = raw
                with self.assertRaises(ValueError): source.read_file('app.py')

    def test_commit_identity_and_comparison_binding(self):
        for slug in ['GITHUB_GET_A_COMMIT', 'GITHUB_COMPARE_TWO_COMMITS']:
            client = Client()
            original = client.one
            def one(name, args):
                result = original(name, args)
                if name == slug:
                    if name == 'GITHUB_GET_A_COMMIT': result['sha'] = 'c'*40
                    else: result['base_commit']['sha'] = 'c'*40
                return result
            client.one = one
            with self.subTest(slug=slug), tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
                self.source(directory, client)

    def test_file_size_and_expired_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.source(directory)
            source.head_entries['app.py'] = ('100644', 'c'*40, 64001)
            with self.assertRaises(ValueError): source.read_file('app.py')
            source.client.deadline = time.monotonic() - 1
            with self.assertRaises(TimeoutError): source.command(['status'])

    def test_diff_uses_actual_merge_base_instead_of_advanced_base_tip(self):
        client = Client()
        merge_base = 'c'*40
        client.trees[merge_base] = copy.deepcopy(client.trees[client.base])
        original = client.one
        def one(slug, args):
            value = original(slug, args)
            if slug == 'GITHUB_COMPARE_TWO_COMMITS': value['merge_base_commit']['sha'] = merge_base
            return value
        client.one = one
        with tempfile.TemporaryDirectory() as directory:
            source = self.source(directory, client)
            self.assertEqual(source.merge_base, merge_base)
            self.assertEqual(source.diff()['files'], [{'path': 'app.py'}])
            commits = [args['ref'] for slug, args in client.calls if slug == 'GITHUB_GET_A_COMMIT']
            self.assertEqual(commits, [merge_base, client.head])
