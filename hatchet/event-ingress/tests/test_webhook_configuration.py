"""An absent webhook credential can never authorize an enabled endpoint."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from hatchet_event_ingress.server import IngressHandler


class WebhookConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'organization.json'
        self.config = json.loads((Path(__file__).resolve().parents[2] / 'workers/test-fixtures/organization.json').read_text())
        self.path.write_text(json.dumps(self.config))
        self.env = patch.dict(os.environ, {'FACTORY_ORGANIZATION_CONFIG': str(self.path), 'HATCHET_EVENT_INGRESS_GITHUB_WEBHOOK_SECRET': '', 'HATCHET_EVENT_INGRESS_LINEAR_WEBHOOK_SECRET': ''})
        self.env.start(); self.addCleanup(self.env.stop)
        self.handler = IngressHandler.__new__(IngressHandler)
        self.handler.headers = {}
        self.responses = []
        self.handler._write_json = lambda status, body: self.responses.append((status, body))

    def test_enabled_endpoints_reject_missing_secrets(self):
        self.assertFalse(self.handler._require_github_signature(b'{}'))
        self.assertFalse(self.handler._require_linear_signature(b'{}', {}))
        self.assertEqual([status for status, _ in self.responses], [503, 503])

    def test_disabled_integrations_reject_even_valid_signature(self):
        self.config['github']['enabled'] = False; self.config['linear'] = None
        self.path.write_text(json.dumps(self.config))
        with patch.dict(os.environ, {'HATCHET_EVENT_INGRESS_GITHUB_WEBHOOK_SECRET': 'test', 'HATCHET_EVENT_INGRESS_LINEAR_WEBHOOK_SECRET': 'test'}):
            self.handler.headers = {'X-Hub-Signature-256': 'sha256=' + hmac.new(b'test', b'{}', hashlib.sha256).hexdigest()}
            self.assertFalse(self.handler._require_github_signature(b'{}'))
            self.assertFalse(self.handler._require_linear_signature(b'{}', {}))

    def test_signed_configured_linear_request_and_timestamp(self):
        body = {'webhookTimestamp': int(time.time() * 1000)}
        raw = json.dumps(body).encode()
        self.handler.headers = {'Linear-Signature': hmac.new(b'test', raw, hashlib.sha256).hexdigest()}
        with patch.dict(os.environ, {'HATCHET_EVENT_INGRESS_LINEAR_WEBHOOK_SECRET': 'test'}):
            self.assertTrue(self.handler._require_linear_signature(raw, body))
            self.assertFalse(self.handler._require_linear_signature(raw, {'webhookTimestamp': 1}))
