from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'deploy/independent-reviewer'))
import source_token_broker as broker
import renew_source_token as renewal


def issued():
    return {'token': 'synthetic-read-credential-0123456789', 'expires_at':
        (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        'permissions': {'contents': 'read', 'metadata': 'read'}, 'repository_selection': 'selected',
        'repositories': [{'full_name': name} for name in ('ExampleOrg/widget', 'ExampleOrg/api')]}


class ReviewerSourceTokenTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.policy_path = Path(self.temporary.name) / 'review-policy.json'
        self.policy = {'schema': 'factory-independent-review-policy-v1', 'enabled': True,
                       'automatic_repairs_allowed': False, 'max_passes': 2,
                       'repositories': ['ExampleOrg/widget', 'ExampleOrg/api']}
        self.policy_path.write_text(json.dumps(self.policy))
        self.organization_path = Path(self.temporary.name) / 'organization.json'
        self.organization = {'schema': 'factory-organization-v1', 'linear': None, 'github': {'enabled': True}}
        self.organization_path.write_text(json.dumps(self.organization))
        environment = patch.dict(os.environ, HATCHET_INDEPENDENT_REVIEW_POLICY_PATH=str(self.policy_path),
                                 FACTORY_ORGANIZATION_CONFIG=str(self.organization_path))
        environment.start()
        self.addCleanup(environment.stop)

    def test_disabled_github_refuses_mint_validation_and_cached_reuse(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            renewal.renew(directory, issued)
            self.organization['github']['enabled'] = False
            self.organization_path.write_text(json.dumps(self.organization))
            with patch.object(broker, 'mint_jwt') as mint_jwt:
                with self.assertRaises(ValueError):
                    broker.mint()
                mint_jwt.assert_not_called()
            with self.assertRaises(ValueError):
                broker.validate_response(issued())
            with self.assertRaises(ValueError):
                renewal.renew(directory, issued)
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            broker.load_organization()

    def test_repository_policy_must_be_explicit_enabled_nonempty_and_valid(self):
        self.assertEqual(broker.load_repositories(), ('ExampleOrg/widget', 'ExampleOrg/api'))
        for changes in [{'enabled': False}, {'repositories': []}, {'repositories': ['bad']},
                        {'repositories': ['ExampleOrg/widget', 'exampleorg/WIDGET']},
                        {'repositories': ['ExampleOrg/widget', 'OtherOrg/api']}, {'schema': 'old'}]:
            self.policy_path.write_text(json.dumps({**self.policy, **changes}))
            with patch.object(broker, 'mint_jwt') as mint_jwt:
                with self.assertRaises(ValueError):
                    broker.mint()
                mint_jwt.assert_not_called()
        self.policy_path.unlink()
        with self.assertRaises(FileNotFoundError):
            broker.load_repositories()
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(ValueError):
            broker.load_repositories()

    def test_exact_downscope_and_private_ipc_response_validation(self):
        captured = []
        value = issued()
        class Response:
            status = 201
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def read(self, limit): return json.dumps(value).encode()[:limit]
        class Opener:
            def open(self, request, timeout):
                captured.append((request, timeout))
                return Response()
        with patch.object(broker, 'mint_jwt', return_value=('synthetic-jwt', '123')), \
             patch.object(broker, 'build_opener', return_value=Opener()):
            self.assertEqual(broker.mint(), value)
        request, timeout = captured[0]
        self.assertEqual(request.full_url, 'https://api.github.com/app/installations/123/access_tokens')
        self.assertEqual(json.loads(request.data), {'repositories': ['widget', 'api'], 'permissions': {'contents': 'read'}})
        self.assertEqual(timeout, 20)
        self.assertIsNone(broker.NoRedirect().redirect_request(None, None))
        for changes in [{'permissions': {'contents': 'write'}}, {'permissions': {'contents': 'read', 'issues': 'read'}},
                        {'permissions': {'contents': 'read', 'metadata': 'write'}}, {'repositories': []},
                        {'repositories': [{'full_name': 'ExampleOrg/widget'}] * 2},
                        {'repository_selection': 'all'}, {'token': 'synthetic\ninvalid'},
                        {'expires_at': (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()},
                        {'expires_at': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}]:
            with self.assertRaises(ValueError):
                broker.validate_response({**value, **changes})

    def test_jwt_uses_fixed_native_key_in_place_without_generator_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary)
            (app / 'app-id').write_text('123')
            (app / 'installation-id').write_text('456')
            with patch.object(broker, 'APP', app), patch.object(broker.subprocess, 'run',
                return_value=subprocess.CompletedProcess([], 0, stdout=b'synthetic-signature')) as sign:
                jwt, installation = broker.mint_jwt()
            self.assertEqual(installation, '456')
            payload = json.loads(base64.urlsafe_b64decode(jwt.split('.')[1] + '=='))
            self.assertEqual(payload['iss'], '123')
            self.assertEqual(payload['exp'] - payload['iat'], 540)
            self.assertEqual(sign.call_args.args[0], ['/usr/bin/openssl', 'dgst', '-sha256', '-sign', str(app / 'private-key.pem'), '-binary'])
            self.assertEqual(sign.call_args.kwargs['input'].decode(), '.'.join(jwt.split('.')[:2]))
            self.assertFalse((app / 'private-key.pem').exists())

    def test_rotation_is_private_atomic_reuses_valid_state_and_preserves_on_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            value = issued()
            calls = []
            def issue():
                calls.append(True)
                return value
            output = renewal.renew(directory, issue)
            self.assertEqual(output['status'], 'renewed')
            self.assertNotIn(value['token'], json.dumps(output))
            token = directory / 'source-token'
            self.assertEqual(token.read_text(), value['token'])
            self.assertEqual(token.stat().st_mode & 0o777, 0o600)
            self.assertEqual((directory / 'source-token-metadata.json').stat().st_mode & 0o777, 0o600)
            self.assertEqual(renewal.renew(directory, issue)['status'], 'current')
            self.assertEqual(len(calls), 1)
            metadata = json.loads((directory / 'source-token-metadata.json').read_text())
            metadata['expires_at_epoch'] = time.time() + 900
            (directory / 'source-token-metadata.json').write_text(json.dumps(metadata))
            before = token.read_bytes()
            with self.assertRaises(ValueError):
                renewal.renew(directory, lambda: {**issued(), 'permissions': {'contents': 'write'}})
            self.assertEqual(token.read_bytes(), before)
            with self.assertRaises(OSError):
                renewal.renew(directory, lambda: (_ for _ in ()).throw(OSError('synthetic unavailable')))
            self.assertEqual(token.read_bytes(), before)
            self.assertEqual(sorted(p.name for p in directory.iterdir()),
                             ['source-token', 'source-token-metadata.json', 'source-token.lock'])

    def test_cached_source_token_is_not_reused_after_policy_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            self.assertEqual(renewal.renew(directory, issued)['status'], 'renewed')
            before = (directory / 'source-token').read_bytes()
            self.policy['repositories'] = ['ExampleOrg/widget']
            self.policy_path.write_text(json.dumps(self.policy))
            with self.assertRaises(ValueError):
                renewal.renew(directory, issued)
            self.assertEqual((directory / 'source-token').read_bytes(), before)
            reduced = {**issued(), 'repositories': [{'full_name': 'ExampleOrg/widget'}]}
            self.assertEqual(renewal.renew(directory, lambda: reduced)['status'], 'renewed')
            metadata = json.loads((directory / 'source-token-metadata.json').read_text())
            self.assertEqual(metadata['repositories'], ['ExampleOrg/widget'])
            self.policy['enabled'] = False
            self.policy_path.write_text(json.dumps(self.policy))
            with patch.object(renewal, 'broker') as issue, self.assertRaises(ValueError):
                renewal.renew(directory, issue)
            issue.assert_not_called()
            self.assertEqual((directory / 'source-token').read_bytes(), before)

    def test_permissions_and_links_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            outside = directory / 'outside'
            outside.write_text('synthetic')
            (directory / 'source-token').symlink_to(outside)
            with self.assertRaises(ValueError):
                renewal.renew(directory, lambda: issued())
            (directory / 'source-token').unlink()
            (directory / 'source-token').write_text('synthetic')
            (directory / 'source-token').chmod(0o644)
            with self.assertRaises(ValueError):
                renewal.renew(directory, lambda: issued())

    def test_concurrent_renewal_has_one_issuer_and_no_inner_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            calls = []
            def issue():
                calls.append(True)
                with self.assertRaises(BlockingIOError):
                    renewal.renew(directory, lambda: self.fail('Concurrent mint was attempted'))
                return issued()
            self.assertEqual(renewal.renew(directory, issue)['status'], 'renewed')
            self.assertEqual(len(calls), 1)

    def test_host_transport_captures_token_without_environment_or_stderr_leak(self):
        response = issued()
        with patch.object(renewal.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0,
            stdout=json.dumps(response).encode())) as run:
            self.assertEqual(renewal.broker(), response)
        args = run.call_args.args[0]
        self.assertEqual(args[:6], ['/usr/bin/docker', '--host', 'unix:///var/run/docker.sock', 'exec', '--workdir', '/opt/hermes'])
        self.assertEqual(args[6], 'hermes-driftward-hermes-1')
        self.assertNotIn(response['token'], ' '.join(args))
        self.assertNotIn('token-cache', args[-1])
        self.assertEqual(run.call_args.kwargs['stdout'], subprocess.PIPE)
        self.assertEqual(run.call_args.kwargs['stderr'], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs['env'], {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent'})

    def test_activation_policy_and_service_boundaries(self):
        policy = json.loads((ROOT / 'hatchet/workers/independent-review-policy.json').read_text())
        self.assertFalse(policy['enabled'])
        self.assertFalse(policy['automatic_repairs_allowed'])
        self.assertEqual(policy['max_passes'], 2)
        self.assertEqual(policy['repositories'], [])
        gateway = (ROOT / 'deploy/systemd/driftward-hermes-factory-gateway.service').read_text()
        self.assertIn('/var/lib/driftward-factory-reviewer', gateway.split('ReadWritePaths=')[1].splitlines()[0])
        service = (ROOT / 'deploy/systemd/driftward-factory-reviewer-source-token.service').read_text()
        self.assertIn('Type=oneshot', service)
        self.assertIn('RestrictAddressFamilies=AF_UNIX', service)
        self.assertNotIn('Restart=', service)
        self.assertNotIn('private-key.pem', service)
        self.assertIn('OnUnitActiveSec=20min', (ROOT / 'deploy/systemd/driftward-factory-reviewer-source-token.timer').read_text())


if __name__ == '__main__':
    unittest.main()
