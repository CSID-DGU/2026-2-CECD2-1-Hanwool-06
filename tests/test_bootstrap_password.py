"""The deployment bootstrap password must be replaced after first login."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from back.api import auth, config, db


class BootstrapPasswordTest(unittest.TestCase):
    def test_bootstrap_admin_requires_password_change(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(config, 'APP_DB_PATH', Path(directory) / 'app.sqlite3'), \
             patch.object(config, 'ADMIN_EMAIL', 'admin@example.com'), \
             patch.object(config, 'ADMIN_PASSWORD', 'TemporaryPass!234'):
            db.init_db()
            auth.bootstrap_admin()
            with db.connect() as connection:
                row = connection.execute('SELECT role,must_change_password FROM users').fetchone()
            self.assertEqual((row['role'], row['must_change_password']), ('superadmin', 1))


if __name__ == '__main__':
    unittest.main()
