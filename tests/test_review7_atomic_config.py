"""Atomic configuration rollback regression (temporary database only)."""
import tempfile
import unittest
from pathlib import Path
from app.db import DB

class TestAtomicConfig(unittest.TestCase):
    def test_serialization_failure_rolls_back_entire_pair(self):
        with tempfile.TemporaryDirectory() as td:
            db = DB(str(Path(td) / 'test.db'))
            try:
                db.set_config_many([('kr_api_key', 'old-key'), ('kr_api_secret', 'old-secret')])
                with self.assertRaises(TypeError):
                    db.set_config_many([('kr_api_key', 'new-key'), ('kr_api_secret', object())])
                self.assertEqual(db.get_config_str('kr_api_key'), 'old-key')
                self.assertEqual(db.get_config_str('kr_api_secret'), 'old-secret')
                db.set_config('unrelated', 1)
                self.assertEqual(db.get_config_str('kr_api_key'), 'old-key')
            finally:
                db.conn.close()
