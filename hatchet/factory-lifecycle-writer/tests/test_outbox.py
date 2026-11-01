import unittest
import tempfile
from pathlib import Path
from factory_lifecycle_writer.outbox import Outbox

class NamingGenerationTests(unittest.TestCase):
    def test_prior_database_is_refused_without_mutation(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'outbox.db'
            with sqlite3.connect(path) as conn:
                conn.execute('CREATE TABLE historical_evidence (value TEXT)')
            before = path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, 'naming generation'):
                Outbox(str(path))
            self.assertEqual(path.read_bytes(), before)
