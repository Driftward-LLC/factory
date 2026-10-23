import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from hatchet_hermes_bridge.console_intake import observed_intake


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'ingress.db'
        self.ingress = sqlite3.connect(self.path)
        self.addCleanup(self.ingress.close)
        self.ingress.execute('CREATE TABLE ingress_events (event_id INTEGER PRIMARY KEY, source TEXT, subject TEXT, normalized_json TEXT, created_at TEXT, provider_updated_at_us INTEGER)')
        self.bridge = sqlite3.connect(':memory:')
        self.addCleanup(self.bridge.close)
        self.bridge.execute('CREATE TABLE factory_attempts (work_item_key TEXT)')

    def event(self, issue='ENG-154', status='Blocked', clock=1, **fields):
        value = {'status': status, 'action': 'update', 'updatedAt': '2026-10-06T18:00:00Z', 'description': 'PRIVATE-SENTINEL', 'title': 'PRIVATE-SENTINEL', **fields}
        self.ingress.execute('INSERT INTO ingress_events(source,subject,normalized_json,created_at,provider_updated_at_us) VALUES (?,?,?,?,?)', ('linear', issue, json.dumps(value), '2026-10-06T18:01:00Z', clock))
        self.ingress.commit()

    def test_blocked_and_ready_are_visible_without_creating_attempts(self):
        self.event(blockedBy=[{'id': 'ENG-151', 'title': 'PRIVATE-SENTINEL'}, {'id': 'private-uuid'}])
        self.event('ENG-155', 'Ready', dispatchEligible=True)
        before = self.path.read_bytes()
        result = observed_intake(self.bridge, self.path)
        self.assertTrue(result['available'])
        self.assertEqual(len(result['items']), 2)
        blocked = next(x for x in result['items'] if x['issue'] == 'ENG-154')
        self.assertEqual(blocked['blocked_by'], ['ENG-151'])
        self.assertEqual(blocked['blocked_by_count'], 2)
        self.assertNotIn('PRIVATE-SENTINEL', json.dumps(result))
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(self.bridge.execute('SELECT COUNT(*) FROM factory_attempts').fetchone()[0], 0)

    def test_provider_order_ties_removal_and_all_historical_attempts(self):
        self.event('ENG-154', 'Done', clock=5)
        self.event('ENG-154', 'Ready', clock=2)  # Arrived later, older provider state.
        self.event('ENG-155', 'Ready', clock=5)
        self.event('ENG-155', 'Blocked', clock=5)  # Deterministic event-id tie break.
        self.event('ENG-156', 'Ready', action='remove')
        self.event('ENG-157', 'Blocked')
        self.bridge.execute("INSERT INTO factory_attempts VALUES ('ENG-157')")
        result = observed_intake(self.bridge, self.path)
        self.assertEqual([(x['issue'], x['linear_status']) for x in result['items']], [('ENG-155', 'Blocked')])
        self.assertEqual(observed_intake(self.bridge, self.path, 'ENG-154')['items'][0]['linear_status'], 'Done')

    def test_unavailable_empty_malformed_and_sanitized_unknown(self):
        missing = self.path.parent / 'missing.db'
        self.assertFalse(observed_intake(self.bridge, missing)['available'])
        self.assertFalse(missing.exists())
        self.assertFalse(observed_intake(self.bridge, None)['available'])
        self.assertEqual(observed_intake(self.bridge, self.path)['items'], [])
        self.event(status='PRIVATE-SENTINEL', updatedAt='bad', blockedBy='PRIVATE-SENTINEL')
        row = observed_intake(self.bridge, self.path)['items'][0]
        self.assertEqual(row['linear_status'], 'Unknown')
        self.assertIsNone(row['provider_updated_at'])
        self.assertIsNone(row['blocked_by_count'])
        self.ingress.execute("UPDATE ingress_events SET normalized_json = 'invalid'")
        self.ingress.commit()
        result = observed_intake(self.bridge, self.path)
        self.assertFalse(result['available'])
        self.assertEqual(result['items'], [])

    def test_limits_and_exact_lookup_outside_scan(self):
        for i in range(1, 503):
            self.event('ENG-' + str(i), clock=i)
        result = observed_intake(self.bridge, self.path)
        self.assertTrue(result['truncated'])
        self.assertEqual(len(result['items']), 100)
        self.assertEqual(result['items'][0]['issue'], 'ENG-502')
        self.assertEqual(observed_intake(self.bridge, self.path, 'ENG-1')['items'][0]['issue'], 'ENG-1')
        self.assertEqual(observed_intake(self.bridge, self.path, 'ENG-999')['items'], [])
