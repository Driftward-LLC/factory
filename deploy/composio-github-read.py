#!/usr/bin/env python3
"""Fixed read-only GitHub receipts through the executor's existing native Composio session."""
import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

PROFILE = Path('/opt/data/profiles/factory-executor')
LIMIT = 16 * 1024 * 1024


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class GithubRead:
    def __init__(self, profile=PROFILE, opener=None, budget=120):
        import yaml
        from dotenv import dotenv_values
        config = yaml.safe_load((profile / 'config.yaml').read_text())
        server = config['mcp_servers']['factory-composio']
        url = urlsplit(server['url'])
        if (url.scheme != 'https' or url.hostname != 'backend.composio.dev' or url.port not in (None, 443)
                or url.username or url.password or url.query or url.fragment
                or server.get('headers') != {'x-api-key': '${FACTORY_COMPOSIO_API_KEY}'}):
            raise ValueError('Unsupported native Composio session configuration')
        session = re.search(r'(?:^|/)(trs_[A-Za-z0-9]+)(?:/|$)', url.path)
        if not session:
            raise ValueError('Native Composio session identity unavailable')
        self.endpoint = 'https://backend.composio.dev/api/v3.1/tool_router/session/' + session[1] + '/execute_meta'
        values = dotenv_values(profile / '.env', interpolate=False)
        self.key = values.get('FACTORY_COMPOSIO_API_KEY')
        if not isinstance(self.key, str) or not self.key or '\n' in self.key or '\r' in self.key:
            raise ValueError('Executor Composio authentication unavailable')
        self.opener = opener or urllib.request.build_opener(NoRedirect)
        self.deadline = time.monotonic() + min(budget, 120)

    def call(self, tools, allow_missing_ref=False):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('GitHub read budget exhausted')
        body = {'slug': 'COMPOSIO_MULTI_EXECUTE_TOOL', 'arguments': {'tools': [
            {'tool_slug': slug, 'arguments': args} for slug, args in tools]}}
        request = urllib.request.Request(self.endpoint, data=json.dumps(body).encode(), method='POST',
            headers={'x-api-key': self.key, 'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=remaining) as response:
            raw = response.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError('GitHub response exceeds budget')
        value = json.loads(raw)
        if value.get('result_type') != 'completed' or value.get('error') is not None:
            results = value.get('data', {}).get('results', [])
            if (allow_missing_ref and len(tools) == 1 and tools[0][0] == 'GITHUB_GET_A_REFERENCE'
                    and len(results) == 1 and results[0].get('tool_slug') == 'GITHUB_GET_A_REFERENCE'
                    and results[0].get('response', {}).get('successful') is False
                    and type(results[0]['response'].get('data', {}).get('status_code')) is int
                    and results[0]['response']['data']['status_code'] == 404):
                return [None]
            raise ValueError('Composio read did not complete')
        results = value['data']['results']
        if len(results) != len(tools):
            raise ValueError('Incomplete Composio results')
        output = []
        for item, (slug, _) in zip(results, tools):
            if item.get('tool_slug') != slug or item['response'].get('successful') is not True:
                raise ValueError('GitHub read unavailable')
            output.append(item['response']['data'])
        return output

    def one(self, slug, args):
        return self.call([(slug, args)])[0]

    def pages(self, slug, args, key, total=None, expected_sha=None):
        output = []
        for page in range(1, 33):
            value = self.one(slug, {**args, 'per_page': 100, 'page': page})
            if expected_sha is not None and value.get('sha') != expected_sha:
                raise ValueError('GitHub status page head mismatch')
            items = value[key]
            if not isinstance(items, list) or len(items) > 100:
                raise ValueError('Malformed GitHub page')
            output.extend(items)
            if total is not None and value.get('total_count') != total:
                raise ValueError('GitHub total changed during pagination')
            if len(items) < 100:
                if total is not None and len(output) != total:
                    raise ValueError('Incomplete GitHub list')
                return output
        raise ValueError('GitHub pagination exceeds budget')


def pr_metadata(value):
    return {'number': value['number'], 'url': value['html_url'], 'state': value['state'].upper(),
            'mergedAt': value.get('merged_at'), 'isDraft': value['draft'],
            'headRefName': value['head']['ref'], 'headRefOid': value['head']['sha'],
            'baseRefName': value['base']['ref'], 'baseRefOid': value['base']['sha'],
            'author': {'login': value['user']['login']}, 'body': value.get('body') or '',
            'changedFiles': value['changed_files']}


def read(client, operation, repository, argument=None):
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repository)
            or any(part in {'.', '..'} for part in repository.split('/'))):
        raise ValueError('Invalid GitHub repository')
    owner, repo = repository.split('/')
    args = {'owner': owner, 'repo': repo}
    if operation == 'identity':
        user, target = client.call([('GITHUB_GET_THE_AUTHENTICATED_USER', {}), ('GITHUB_GET_A_REPOSITORY', args)])
        if target['full_name'] != repository:
            raise ValueError('GitHub repository identity mismatch')
        def author(key):
            return subprocess.check_output(['git', 'config', '--get', key], timeout=5, text=True).strip()
        return {'githubLogin': user['login'], 'accessibleRepo': target['full_name'],
                'gitAuthorName': author('user.name'), 'gitAuthorEmail': author('user.email')}
    if operation == 'head':
        if (not argument or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}', argument)
                or '..' in argument or argument.endswith('/') or '//' in argument):
            raise ValueError('Invalid branch reference')
        # A GitHub 404 hides inaccessible private resources; prove repository access first.
        if client.one('GITHUB_GET_A_REPOSITORY', args)['full_name'] != repository:
            raise ValueError('Repository access unavailable')
        reference = client.call([('GITHUB_GET_A_REFERENCE', {**args, 'ref': 'heads/' + argument})], allow_missing_ref=True)[0]
        if reference is not None and reference.get('ref') != 'refs/heads/' + argument:
            raise ValueError('GitHub branch identity mismatch')
        pulls = client.pages('GITHUB_LIST_PULL_REQUESTS', {**args, 'head': owner + ':' + argument, 'state': 'all'}, 'pull_requests')
        mapped = []
        for item in pulls:
            if item['head']['ref'] != argument:
                raise ValueError('GitHub pull request branch mismatch')
            mapped.append({'number': item['number'], 'url': item['html_url'], 'state': item['state'].upper(),
                'isDraft': item['draft'], 'headRefName': item['head']['ref'], 'headRefOid': item['head']['sha'],
                'baseRefName': item['base']['ref'], 'mergedAt': item.get('merged_at'), 'closedAt': item.get('closed_at'),
                'title': item['title']})
        return {'kind': 'github_branch_pr_absence_v1', 'repository': repository, 'head_branch': argument,
                'branch_exists': reference is not None, 'pull_requests': mapped,
                'observed_at': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'reason': 'ok'}
    if operation in {'pr-metadata', 'pr-view'}:
        if not argument or not re.fullmatch('[1-9][0-9]*', argument):
            raise ValueError('Invalid pull request number')
        pull_args = {**args, 'pull_number': int(argument)}
        view = pr_metadata(client.one('GITHUB_GET_A_PULL_REQUEST', pull_args))
        if view['number'] != int(argument) or view['url'] != f'https://github.com/{repository}/pull/{argument}':
            raise ValueError('Pull request identity mismatch')
        if operation == 'pr-metadata':
            return view
        files = client.pages('GITHUB_LIST_PULL_REQUESTS_FILES', pull_args, 'details')
        if len(files) != view['changedFiles'] or len({item['filename'] for item in files}) != len(files):
            raise ValueError('Incomplete changed-file evidence')
        view['files'] = [{'path': item['filename']} for item in files]
        commit_args = {**args, 'ref': view['headRefOid'], 'per_page': 100, 'page': 1}
        checks, statuses = client.call([('GITHUB_LIST_CHECK_RUNS_FOR_A_REF', {**commit_args, 'filter': 'latest'}),
            ('GITHUB_GET_THE_COMBINED_STATUS_FOR_A_SPECIFIC_REFERENCE', commit_args)])
        check_runs = checks['check_runs']
        contexts = statuses['statuses']
        if any(type(value.get('total_count')) is not int or value['total_count'] < 0 for value in (checks, statuses)):
            raise ValueError('Malformed GitHub check totals')
        if not isinstance(check_runs, list) or not isinstance(contexts, list):
            raise ValueError('Malformed GitHub check lists')
        if checks['total_count'] > 100:
            check_runs = client.pages('GITHUB_LIST_CHECK_RUNS_FOR_A_REF', {**args, 'ref': view['headRefOid'], 'filter': 'latest'},
                                      'check_runs', checks['total_count'])
        if statuses['sha'] != view['headRefOid']:
            raise ValueError('GitHub status head mismatch')
        if statuses['total_count'] > 100:
            contexts = client.pages('GITHUB_GET_THE_COMBINED_STATUS_FOR_A_SPECIFIC_REFERENCE', {**args, 'ref': view['headRefOid']},
                                   'statuses', statuses['total_count'], view['headRefOid'])
        if len(check_runs) != checks['total_count'] or len(contexts) != statuses['total_count']:
            raise ValueError('Incomplete GitHub checks')
        identities = [item.get('id') for item in check_runs]
        names = [item.get('context') for item in contexts]
        if (any(type(identity) is not int or identity < 1 for identity in identities) or len(set(identities)) != len(identities)
                or any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names)):
            raise ValueError('Duplicate or malformed GitHub check identity')
        if any(item['head_sha'] != view['headRefOid'] for item in check_runs):
            raise ValueError('GitHub check head mismatch')
        view['statusCheckRollup'] = [{'name': item['name'], 'status': item['status'], 'conclusion': item.get('conclusion')}
            for item in check_runs] + [{'name': item['context'], 'state': item['state']} for item in contexts]
        return view
    raise ValueError('Unsupported fixed GitHub read')


if __name__ == '__main__':
    try:
        operation, repository, *arguments = sys.argv[1:]
        result = read(GithubRead(), operation, repository, arguments[0] if arguments else None)
        output = json.dumps(result, separators=(',', ':'))
        if len(output.encode()) > LIMIT:
            raise ValueError('GitHub receipt exceeds budget')
        print(output)
    except Exception:
        sys.exit(75)  # Never expose Composio diagnostics, source credentials or private config.
