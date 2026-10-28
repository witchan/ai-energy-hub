import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import server


class SecurityConfigTest(unittest.TestCase):
    def test_missing_credentials_are_rejected(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "UPSTREAM_USER"):
                server.validate_config()

    def test_new_database_has_no_credential_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            old_file = server.DB_FILE
            server.DB_FILE = os.path.join(directory, "shop.db")
            try:
                server.init_db()
                with sqlite3.connect(server.DB_FILE) as connection:
                    columns = {row[1] for row in connection.execute("PRAGMA table_info(orders)")}
                self.assertTrue({"account", "password", "ai_token"}.isdisjoint(columns))
            finally:
                server.DB_FILE = old_file


if __name__ == "__main__":
    unittest.main()
