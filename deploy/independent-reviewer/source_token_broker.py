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
REPOSITORIES = ('Driftward-LLC/factory', 'Driftward-LLC/capykit')


def validate_response(value: object, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    if not isinstance(value, dict):
        raise ValueError('invalid source credential response')
    token, permissions, repositories = value.get('token'), value.get('permissions'), value.get('repositories')
    if (not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{20,8192}', token)
            or not isinstance(permissions, dict) or permissions.get('contents') != 'read'
            or set(permissions) - {'contents', 'metadata'} or permissions.get('metadata', 'read') != 'read'
            or value.get('repository_selection') != 'selected' or not isinstance(repositories, list)
            or len(repositories) != len(REPOSITORIES)
            or any(not isinstance(repo, dict) for repo in repositories)
            or sorted(repo.get('full_name', '') for repo in repositories) != sorted(REPOSITORIES)):
        raise ValueError('source credential scope mismatch')
    expiry = value.get('expires_at')
    if not isinstance(expiry, str) or len(expiry) > 64:
        raise ValueError('invalid source credential expiry')
    expires = datetime.fromisoformat(expiry.replace('Z', '+00:00'))
    if expires.tzinfo is None or not now + 1200 < expires.timestamp() <= now + 3900:
        raise ValueError('source credential expiry outside renewal window')
    return {'token': token, 'expires_at': expires.astimezone(timezone.utc).isoformat(),
            'expires_at_epoch': int(expires.timestamp()), 'permissions': permissions,
            'repositories': list(REPOSITORIES)}


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


def mint() -> dict:
    jwt, installation = mint_jwt()
    request = Request(f'https://api.github.com/app/installations/{installation}/access_tokens',
        data=json.dumps({'repositories': [repo.split('/')[1] for repo in REPOSITORIES],
                         'permissions': {'contents': 'read'}}).encode(),
        headers={'Authorization': 'Bearer ' + jwt, 'Accept': 'application/vnd.github+json',
                 'Content-Type': 'application/json', 'X-GitHub-Api-Version': '2022-11-28'}, method='POST')
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=20) as response:
        raw = response.read(128 * 1024 + 1)
        if response.status != 201 or len(raw) > 128 * 1024:
            raise ValueError('source credential issuance unavailable')
    value = json.loads(raw)
    validate_response(value)
    # Only the host helper's private captured pipe receives this bounded token response.
    return value


if __name__ == '__main__':
    os.environ.clear()
    os.environ.update(PATH='/usr/bin:/bin', LANG='C.UTF-8')
    try:
        print(json.dumps(mint(), separators=(',', ':')))
    except Exception:
        sys.exit(1)
