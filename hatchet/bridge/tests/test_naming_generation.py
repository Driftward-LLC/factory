import sqlite3
import tempfile
import unittest
from pathlib import Path
from hatchet_hermes_bridge.store import BridgeValidationError, connect_runtime
from test_support import connect_migrate

class NamingGenerationTests(unittest.TestCase):
    def test_prior_database_is_refused_without_changing_bytes(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'bridge.db'
            with sqlite3.connect(path) as conn:
                conn.execute('CREATE TABLE historical_evidence (value TEXT)')
                conn.execute("INSERT INTO historical_evidence VALUES ('retained')")
            before = path.read_bytes()
            for open_database in (connect_migrate, connect_runtime):
                with self.assertRaisesRegex((RuntimeError, BridgeValidationError), 'naming generation'):
                    open_database(str(path))
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse(path.with_name('bridge.db-wal').exists())
