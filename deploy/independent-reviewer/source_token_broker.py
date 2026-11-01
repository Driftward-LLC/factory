"""Mint only reviewer source-read authority using the existing native GitHub App."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

APP = Path('/opt/data/github-apps/factory-hermes')
def load_organization(organization_path: str | Path | None = None) -> dict:
    location = organization_path or os.environ.get('FACTORY_ORGANIZATION_CONFIG')
    if not location:
        raise ValueError('GitHub organization configuration is required')
    return validate_organization(json.loads(Path(location).read_text()))


def validate_organization(value: object) -> dict:
    if (not isinstance(value, dict) or set(value) != {'schema', 'linear', 'github'}
            or value.get('schema') != 'factory-organization-v1' or not isinstance(value.get('github'), dict)
            or set(value['github']) != {'enabled'} or value['github'].get('enabled') is not True):
        raise ValueError('GitHub integration is disabled or malformed')
    linear = value['linear']
    if linear is not None:
        if (not isinstance(linear, dict)
                or set(linear) != {'workspace_id', 'team_id', 'team_key', 'workspace_slug', 'actor_id', 'delegate_name'}
                or any(not isinstance(linear[key], str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', linear[key]) for key in ('workspace_id', 'team_id', 'actor_id'))
                or not isinstance(linear['team_key'], str) or not re.fullmatch(r'[A-Z][A-Z0-9]{0,15}', linear['team_key'])
                or not isinstance(linear['workspace_slug'], str) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,79}', linear['workspace_slug'])
                or not isinstance(linear['delegate_name'], str) or not 1 <= len(linear['delegate_name']) <= 100
                or linear['delegate_name'].strip() != linear['delegate_name'] or any(ord(c) < 32 for c in linear['delegate_name'])):
            raise ValueError('Linear organization configuration is malformed')
    return value


def load_repositories(policy_path: str | Path | None = None) -> tuple[str, ...]:
    location = policy_path or os.environ.get('HATCHET_INDEPENDENT_REVIEW_POLICY_PATH')
    if not location:
        raise ValueError('independent review policy path is required')
    policy = json.loads(Path(location).read_text())
    repositories = policy.get('repositories') if isinstance(policy, dict) else None
    if (not isinstance(policy, dict) or policy.get('schema') != 'factory-independent-review-policy-v1'
            or policy.get('enabled') is not True or not isinstance(repositories, list)
            or not 1 <= len(repositories) <= 100
            or any(not isinstance(repo, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo) for repo in repositories)
            or len({repo.lower() for repo in repositories}) != len(repositories)
            or len({repo.split('/')[0].lower() for repo in repositories}) != 1):
        raise ValueError('enabled independent review repository policy is required')
    return tuple(repositories)



def validate_response(value: object, now: float | None = None, repositories: tuple[str, ...] | None = None, organization: dict | None = None) -> dict:
    validate_organization(load_organization() if organization is None else organization)
    expected_repositories = load_repositories() if repositories is None else repositories
    now = time.time() if now is None else now
    if not isinstance(value, dict):
        raise ValueError('invalid source credential response')
    token, permissions, repositories = value.get('token'), value.get('permissions'), value.get('repositories')
    if (not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{20,8192}', token)
            or not isinstance(permissions, dict) or permissions.get('contents') != 'read'
            or set(permissions) - {'contents', 'metadata'} or permissions.get('metadata', 'read') != 'read'
            or value.get('repository_selection') != 'selected' or not isinstance(repositories, list)
            or len(repositories) != len(expected_repositories)
            or any(not isinstance(repo, dict) for repo in repositories)
            or sorted(repo.get('full_name', '') for repo in repositories) != sorted(expected_repositories)):
        raise ValueError('source credential scope mismatch')
    expiry = value.get('expires_at')
    if not isinstance(expiry, str) or len(expiry) > 64:
        raise ValueError('invalid source credential expiry')
    expires = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
    if expires.tzinfo is None or not now + 1200 < expires.timestamp() <= now + 3900:
        raise ValueError('source credential expiry outside renewal window')
    return {'token': token, 'expires_at': expires.astimezone(timezone.utc).isoformat(),
            'expires_at_epoch': int(expires.timestamp()), 'permissions': permissions,
            'repositories': list(expected_repositories)}


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode('ascii')


def mint_jwt() -> tuple[str, str]:
    app_id = (APP / 'app-id').read_text().strip()
    installation_id = (APP / 'installation-id').read_text().strip()
    if not re.fullmatch(r'[1-9][0-9]{0,19}', app_id) or not re.fullmatch(r'[1-9][0-9]{0,19}', installation_id):
        raise ValueError('native GitHub App identity unavailable')
    now = int(time.time())
    unsigned = encode(b'{"alg":"RS256","typ":"JWT"}') + '.' + encode(json.dumps(
        {'iat': now - 60, 'exp': now + 480, 'iss': app_id}, separators=(',', ':')).encode())
    signature = subprocess.run(['/usr/bin/openssl', 'dgst', '-sha256', '-sign', str(APP / 'private-key.pem'), '-binary'],
        input=unsigned.encode(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10, check=True).stdout
    return unsigned + '.' + encode(signature), installation_id


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def mint(repositories: tuple[str, ...] | None = None, organization: dict | None = None) -> dict:
    organization = load_organization() if organization is None else validate_organization(organization)
    repositories = load_repositories() if repositories is None else repositories
    if not repositories:
        raise ValueError('enabled independent review repository policy is required')
    jwt, installation = mint_jwt()
    request = Request(f'https://api.github.com/app/installations/{installation}/access_tokens',
        data=json.dumps({'repositories': [repo.split('/')[1] for repo in repositories],
                         'permissions': {'contents': 'read'}}).encode(),
        headers={'Authorization': 'Bearer ' + jwt, 'Accept': 'application/vnd.github+json',
                 'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28'}, method='POST')
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=20) as response:
        raw = response.read(128 * 1024 + 1)
        if response.status != 201 or len(raw) > 128 * 1024:
            raise ValueError('source credential issuance unavailable')
    value = json.loads(raw)
    validate_response(value, repositories=repositories, organization=organization)
    # Only the host helper's private captured pipe receives this bounded token response.
    return value


if __name__ == '__main__':
    policy_path = os.environ.get('HATCHET_INDEPENDENT_REVIEW_POLICY_PATH')
    organization_path = os.environ.get('FACTORY_ORGANIZATION_CONFIG')
    os.environ.clear()
    if organization_path:
        os.environ['FACTORY_ORGANIZATION_CONFIG'] = organization_path
    if policy_path:
        os.environ['HATCHET_INDEPENDENT_REVIEW_POLICY_PATH'] = policy_path
    os.environ.update(PATH='/usr/bin:/bin', LANG='C.UTF-8')
    try:
        print(json.dumps(mint(), separators=(',', ':')))
    except Exception:
        sys.exit(1)
