import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('composio_read', Path(__file__).resolve().parents[1] / 'deploy/composio-github-read.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
HEAD, BASE = 'a'*40, 'b'*40
PR = {'number': 7, 'html_url': 'https://github.com/example/demo/pull/7', 'state': 'open',
      'draft': True, 'head': {'ref': 'codex/demo', 'sha': HEAD}, 'base': {'ref': 'main', 'sha': BASE},
      'user': {'login': 'dedicated-user'}, 'body': 'Refs Linear ENG-1', 'changed_files': 1}


class Client:
    def __init__(self, mutate=None):
        self.values = {'GITHUB_GET_A_PULL_REQUEST': PR,
          'GITHUB_LIST_PULL_REQUESTS_FILES': {'details': [{'filename': 'app.py'}]},
          'GITHUB_LIST_CHECK_RUNS_FOR_A_REF': {'total_count': 1,
            'check_runs': [{'id': 1, 'name': 'checks', 'head_sha': HEAD, 'status': 'completed', 'conclusion': 'success'}]},
          'GITHUB_GET_THE_COMBINED_STATUS_FOR_A_SPECIFIC_REFERENCE': {'total_count': 0, 'statuses': [], 'sha': HEAD}}
        if mutate:
            mutate(self.values)
    def one(self, slug, args):
        return self.values[slug]
    def call(self, tools):
        return [self.one(slug, args) for slug, args in tools]
    pages = MODULE.GithubRead.pages


class ComposioReadTests(unittest.TestCase):
    def test_complete_pr_evidence_binds_checks_and_statuses_to_exact_head(self):
        value = MODULE.read(Client(), 'pr-view', 'example/demo', '7')
        self.assertEqual(value['headRefOid'], HEAD)
        self.assertEqual(value['files'], [{'path': 'app.py'}])
        self.assertEqual(value['statusCheckRollup'][0]['name'], 'checks')

    def test_incomplete_files_and_wrong_check_revision_fail_closed(self):
        mutations = [lambda v: v.update(GITHUB_LIST_PULL_REQUESTS_FILES={'details': []}),
          lambda v: v.update(GITHUB_LIST_CHECK_RUNS_FOR_A_REF={'total_count': 0, 'check_runs': v['GITHUB_LIST_CHECK_RUNS_FOR_A_REF']['check_runs']}),
          lambda v: v.update(GITHUB_LIST_CHECK_RUNS_FOR_A_REF={'total_count': 1, 'check_runs': [{'id': 1, 'name': 'checks', 'head_sha': BASE}]}),
          lambda v: v.update(GITHUB_GET_THE_COMBINED_STATUS_FOR_A_SPECIFIC_REFERENCE={'total_count': 0, 'statuses': [], 'sha': BASE})]
        for mutation in mutations:
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                MODULE.read(Client(mutation), 'pr-view', 'example/demo', '7')

    def test_pagination_requires_complete_count_and_stops_at_budget(self):
        client = Client()
        pages = [{ 'check_runs': list(range(100)), 'total_count': 101 }, {'check_runs': [100], 'total_count': 101}]
        with patch.object(client, 'one', side_effect=pages):
            self.assertEqual(len(client.pages('checks', {}, 'check_runs', 101)), 101)
        with patch.object(client, 'one', return_value={'check_runs': [], 'total_count': 101}):
            with self.assertRaisesRegex(ValueError, 'Incomplete'):
                client.pages('checks', {}, 'check_runs', 101)

    def test_native_session_credentials_are_resolved_privately_and_never_in_request_body(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory)
            profile.joinpath('config.yaml').write_text(json.dumps({'mcp_servers': {'factory-composio': {
              'url': 'https://backend.composio.dev/tool_router/trs_synthetic/mcp',
              'headers': {'x-api-key': '${FACTORY_COMPOSIO_API_KEY}'}}}}))
            result = {'result_type': 'completed', 'error': None, 'data': {'results': [{'tool_slug': 'GITHUB_GET_A_REPOSITORY',
                      'response': {'successful': True, 'data': {'full_name': 'example/demo'}}}]}}
            class Response(io.BytesIO):
                def __enter__(self): return self
                def __exit__(self, *_): self.close()
            requests = []
            def open_request(request, **kwargs):
                requests.append(request)
                return Response(json.dumps(result).encode())
            opener = types.SimpleNamespace(open=open_request)
            dotenv = types.SimpleNamespace(dotenv_values=lambda *args, **kwargs: {'FACTORY_COMPOSIO_API_KEY': 'synthetic-key'})
            with patch.dict(sys.modules, {'dotenv': dotenv}):
                client = MODULE.GithubRead(profile, opener)
            self.assertEqual(client.one('GITHUB_GET_A_REPOSITORY', {}), {'full_name': 'example/demo'})
            self.assertEqual(requests[-1].get_header('X-api-key'), 'synthetic-key')
            self.assertNotIn(b'synthetic-key', requests[-1].data)
            result.update(result_type='failed', error='1 tool failed', data={'results': [{'tool_slug': 'GITHUB_GET_A_REFERENCE',
                'response': {'successful': False, 'data': {'status_code': 404}}}]})
            self.assertEqual(client.call([('GITHUB_GET_A_REFERENCE', {})], allow_missing_ref=True), [None])
            with self.assertRaises(ValueError):
                client.call([('GITHUB_GET_A_REFERENCE', {})])
            result['data']['results'][0]['response']['data']['status_code'] = '404'
            with self.assertRaises(ValueError):
                client.call([('GITHUB_GET_A_REFERENCE', {})], allow_missing_ref=True)
            profile.joinpath('config.yaml').write_text(json.dumps({'mcp_servers': {'factory-composio': {
              'url': 'https://other.invalid/tool_router/trs_synthetic/mcp', 'headers': {'x-api-key': '${FACTORY_COMPOSIO_API_KEY}'}}}}))
            with patch.dict(sys.modules, {'dotenv': dotenv}), self.assertRaises(ValueError):
                MODULE.GithubRead(profile, opener)

    def test_unsupported_operations_and_reference_injection_are_rejected(self):
        for operation, repo, number in [('write', 'example/demo', None), ('pr-view', '../demo', '7'),
                                         ('pr-view', 'example/demo', '-1')]:
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                MODULE.read(Client(), operation, repo, number)

    def test_absence_requires_readable_repository_and_exact_branch_response(self):
        class HeadClient(Client):
            def __init__(self):
                super().__init__()
                self.values.update(GITHUB_GET_A_REPOSITORY={'full_name': 'example/demo'},
                  GITHUB_LIST_PULL_REQUESTS={'pull_requests': []})
            def call(self, tools, allow_missing_ref=False):
                self.allowed = allow_missing_ref
                return [None]
        client = HeadClient()
        value = MODULE.read(client, 'head', 'example/demo', 'codex/new')
        self.assertTrue(client.allowed)
        self.assertFalse(value['branch_exists'])
        self.assertEqual(value['pull_requests'], [])
        client.values['GITHUB_GET_A_REPOSITORY'] = {'full_name': 'other/repo'}
        with self.assertRaises(ValueError):
            MODULE.read(client, 'head', 'example/demo', 'codex/new')


    def test_paginated_checks_reject_duplicate_stable_ids(self):
        class DuplicateChecks(Client):
            def one(self, slug, args):
                if slug == 'GITHUB_LIST_CHECK_RUNS_FOR_A_REF':
                    items = [{'id': index, 'name': 'check-'+str(index), 'head_sha': HEAD,
                              'status': 'completed', 'conclusion': 'success'} for index in range(1, 101)]
                    return {'total_count': 101, 'check_runs': items if args.get('page') == 1 else [items[-1]]}
                return super().one(slug, args)
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            MODULE.read(DuplicateChecks(), 'pr-view', 'example/demo', '7')

    def test_status_pages_bind_every_sha_and_reject_duplicate_contexts(self):
        client = Client()
        with patch.object(client, 'one', return_value={'statuses': [], 'total_count': 0, 'sha': BASE}):
            with self.assertRaisesRegex(ValueError, 'page head'):
                client.pages('statuses', {}, 'statuses', 0, HEAD)
        client.values['GITHUB_GET_THE_COMBINED_STATUS_FOR_A_SPECIFIC_REFERENCE'] = {
            'total_count': 2, 'sha': HEAD, 'statuses': [{'context': 'build', 'state': 'success'}]*2}
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            MODULE.read(client, 'pr-view', 'example/demo', '7')
