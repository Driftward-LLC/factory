import copy
import os
import sqlite3
import unittest
from unittest.mock import patch

from hatchet_hermes_bridge.console import _with_agent_trace


class AgentTraceTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.addCleanup(self.conn.close)
        self.conn.execute('CREATE TABLE factory_attempt_events (attempt_id TEXT,event_type TEXT,created_at TEXT,event_id TEXT)')
        self.conn.execute('PRAGMA query_only=ON')
        self.record = {
            'attempt': {'attempt_id': 'bdb1edb7-459e-4c1c-a30f-b657c1c4833d', 'mode': 'real', 'created_at': '2026-09-30T05:53:34.202223Z'},
            'job': {'payload': {'factory_retry': {'attempt_id': 'bdb1edb7-459e-4c1c-a30f-b657c1c4833d'}}},
        }

    def test_matches_installed_observer_canary_and_does_not_mutate(self):
        before = copy.deepcopy(self.record)
        for host in ('us', 'eu', 'app'):
            with patch.dict(os.environ, {'FACTORY_CONSOLE_POSTHOG_PROJECT_URL': f'https://{host}.posthog.com/project/12345/'}):
                trace = _with_agent_trace(self.record, self.conn)['agent_trace']
            self.assertEqual(trace['url'], f'https://{host}.posthog.com/project/12345/ai-observability/traces/fb9fa7e1-4065-5818-af72-3e41a35fe39a?timestamp=2026-09-30T05%3A53%3A34.202Z')
            self.assertEqual(trace['status'], 'unverified')
        self.assertEqual(self.record, before)

    def test_invalid_config_is_not_exposed(self):
        for url in ('', 'javascript:alert(1)', 'https://us.i.posthog.com/project/12',
                    'https://us.posthog.com.evil.test/project/12', 'https://user:SECRET@us.posthog.com/project/12',
                    'https://us.posthog.com/project/12?token=SECRET', 'https://us.posthog.com/project/12#SECRET',
                    'https://us.posthog.com/project/12\n', 'https://us.posthog.com:443/project/12',
                    'http://us.posthog.com/project/12', 'https://us.posthog.com/project/0'):
            with self.subTest(url=url), patch.dict(os.environ, {'FACTORY_CONSOLE_POSTHOG_PROJECT_URL': url}):
                trace = _with_agent_trace(self.record, self.conn)['agent_trace']
                self.assertEqual(trace['status'], 'not_configured')
                self.assertIsNone(trace['url'])

    def test_retries_have_distinct_trace_destinations(self):
        with patch.dict(os.environ, {'FACTORY_CONSOLE_POSTHOG_PROJECT_URL': 'https://us.posthog.com/project/12345'}):
            first = _with_agent_trace(self.record, self.conn)['agent_trace']['url']
            second_id = 'abcdef01-2345-6789-abcd-ef0123456789'
            self.record['attempt']['attempt_id'] = second_id
            self.record['job']['payload']['factory_retry']['attempt_id'] = second_id
            second = _with_agent_trace(self.record, self.conn)['agent_trace']['url']
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)

    def test_dry_run_missing_or_mismatched_lineage_never_links(self):
        with patch.dict(os.environ, {'FACTORY_CONSOLE_POSTHOG_PROJECT_URL': 'https://us.posthog.com/project/12345'}):
            self.record['attempt']['mode'] = 'dry-run'
            self.assertEqual(_with_agent_trace(self.record, self.conn)['agent_trace']['status'], 'not_applicable')
            self.record['attempt']['mode'] = 'real'
            for lineage in (None, {}, 'invalid', {'attempt_id': 'another-attempt'}):
                self.record['job']['payload']['factory_retry'] = lineage
                self.assertIsNone(_with_agent_trace(self.record, self.conn)['agent_trace']['url'])
