#!/usr/bin/env python3
"""Preview/apply native PostHog setup to existing, idle Factory role profiles.

Run from a source checkout with PyYAML available and Hermes stopped.
This is not a portable-release helper. Does not start execution.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

import yaml

import bootstrap
import telemetry

ROLES = ('factory-executor', 'factory-independent-reviewer')
PLUGIN = 'observability/posthog'
HERE = Path(__file__).resolve().parent


def digest(data):
    return hashlib.sha256(data).hexdigest()


def checked(path, *, directory=False):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('symlinked observer provisioning path')
    info = path.stat()
    if (not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or info.st_mode & 0o022):
        raise ValueError('unsafe observer provisioning path')
    return info


def contents(path):
    if not path.exists():
        if path.is_symlink():
            raise ValueError('symlinked observer provisioning path')
        return None
    info = checked(path)
    if info.st_size > 8 * 1024 * 1024:
        raise ValueError('observer provisioning file too large')
    return path.read_bytes()


def environment(previous, values):
    """Retain unrelated dotenv bytes; refuse ambiguous/conflicting telemetry entries."""
    import re
    text = (previous or b'').decode('utf-8')
    for line in text.splitlines():
        if re.match(r'\s*(?:export\s+)?POSTHOG_(?:API_KEY|HOST)\b', line):
            raise ValueError('profile telemetry already configured; rotation requires separate review')
    return (text + ('\n' if text and not text.endswith('\n') else '')
            + bootstrap.env_text(values)).encode()


def plan(root, credential_file):
    root = root.absolute()
    owner = checked(root, directory=True)
    source = telemetry.credentials(credential_file)
    values = {'POSTHOG_API_KEY': source['POSTHOG_PROJECT_TOKEN'],
              'POSTHOG_HOST': source['POSTHOG_TRACES_ENDPOINT'].removesuffix('/i/v1/traces')}
    protected, writes = {}, {}
    for home in (root, *(root / 'profiles' / name for name in ROLES)):
        checked(home, directory=True)
        for name in ('config.yaml', 'auth.json', '.env'):
            path = home / name
            data = contents(path)
            info = path.stat() if data is not None else None
            protected[str(path.relative_to(root))] = None if info is None else {
                'sha256': digest(data), 'uid': info.st_uid, 'gid': info.st_gid,
                'mode': stat.S_IMODE(info.st_mode)}
    for name in ROLES:
        profile = root / 'profiles' / name
        info = checked(profile, directory=True)
        if info.st_uid != owner.st_uid or info.st_mode & 0o077:
            raise ValueError('named role profile must be private and owned by Hermes')
        path = profile / 'config.yaml'
        config = yaml.safe_load(contents(path))
        model = 'gpt-6.1-sol' if name == ROLES[0] else 'gpt-6-astra'
        if not isinstance(config, dict) or config.get('model', {}).get('default') != model or config['model'].get('provider') != 'openai-codex':
            raise ValueError('named role model differs from approved native roles')
        plugins = config.get('plugins', {})
        if (not isinstance(plugins, dict) or not isinstance(plugins.get('enabled', []), list)
                or not isinstance(plugins.get('disabled', []), list)):
            raise ValueError('invalid role plugin settings')
        if name == ROLES[0]:
            if PLUGIN in plugins.get('disabled', []):
                raise ValueError('executor observer explicitly disabled')
            updated = copy.deepcopy(config)
            enabled = updated.setdefault('plugins', {}).setdefault('enabled', [])
            if PLUGIN not in enabled:
                enabled.append(PLUGIN)
                writes[path] = yaml.safe_dump(updated, sort_keys=False).encode()
        elif plugins.get('enabled') or config.get('mcp_servers'):
            raise ValueError('reviewer generic plugins and MCP must remain disabled')
        env = profile / '.env'
        writes[env] = environment(contents(env), values)
    plugin = root / 'profiles' / ROLES[0] / 'plugins' / 'observability' / 'posthog'
    for parent in (plugin.parent.parent, plugin.parent, plugin):
        if parent.exists() or parent.is_symlink():
            checked(parent, directory=True)
    for name in ('__init__.py', 'plugin.yaml'):
        path = plugin / name
        current, desired = contents(path), (HERE / 'factory-observers' / 'posthog' / name).read_bytes()
        protected[str(path.relative_to(root))] = None if current is None else {'sha256': digest(current)}
        if current is not None and current != desired:
            raise ValueError('existing observer differs; upgrade requires separate review')
        if current is None:
            writes[path] = desired
    target = {str(p.relative_to(root)): digest(data) for p, data in writes.items()}
    state = {'protected': protected, 'writes': target}
    return state, writes, owner


def publish(path, data, uid, gid):
    descriptor, temporary = tempfile.mkstemp(prefix='.observer-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            os.fchmod(stream.fileno(), 0o600)
            os.fchown(stream.fileno(), uid, gid)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def provision(root, credential_file, *, expected_digest=None):
    root = root.absolute()
    state, writes, owner = plan(root, credential_file)
    snapshot = digest(json.dumps(state, sort_keys=True).encode())
    report = {'snapshot_digest': snapshot, **state, 'applied': False}
    if expected_digest is None:
        return report
    if snapshot != expected_digest:
        raise ValueError('observer provisioning preview changed')
    # The operator stops Hermes before apply; no profile/auth/capability migration.
    for path, data in writes.items():
        missing = []
        parent = path.parent
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for parent in reversed(missing):
            parent.mkdir(mode=0o700)
            os.chown(parent, owner.st_uid, owner.st_gid)
        publish(path, data, owner.st_uid, owner.st_gid)
    for relative, before in state['protected'].items():
        path = root / relative
        if path not in writes:
            data = contents(path)
            if (None if data is None else digest(data)) != (None if before is None else before['sha256']):
                raise ValueError('protected role state changed during provisioning')
    report['applied'] = True
    report['preserved'] = {name: value for name, value in state['protected'].items() if root / name not in writes}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-dir', required=True, type=Path)
    parser.add_argument('--credentials', required=True, type=Path)
    parser.add_argument('--apply-preview', metavar='SNAPSHOT_SHA256', help='apply the exact preview with Hermes stopped')
    args = parser.parse_args()
    try:
        print(json.dumps(provision(args.runtime_dir, args.credentials, expected_digest=args.apply_preview), sort_keys=True))
    except Exception:
        parser.exit(1, 'Native observer provisioning failed; private values withheld.\n')


if __name__ == '__main__':
    main()
