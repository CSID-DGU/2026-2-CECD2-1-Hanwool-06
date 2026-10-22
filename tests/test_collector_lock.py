"""Standalone collectors share the web worker's real cross-process file lock."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from back.pipelines.common import collection_lock


class CollectorLockTest(unittest.TestCase):
    def test_bill_cli_waits_for_existing_collection_before_writing(self):
        script = '''
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sys
from back.scripts.billing_etl import crawl_bills
root = Path(sys.argv[1])
args = SimpleNamespace(mkeys_path=None, max_mkeys=None, start_ym='2026-06', end_ym='2026-06',
                       cache_dir=root, out_dir=root, sleep=0, env_path=None)
def collect(**kwargs):
    (root / 'collector-entered').write_text('collected')
    return {'errors': []}
with patch.object(crawl_bills, 'RUNTIME', root), \\
     patch.object(crawl_bills, 'parse_args', return_value=args), \\
     patch.object(crawl_bills, 'collector_meters', return_value=[{'customer_number': '000000001'}]), \\
     patch.object(crawl_bills, 'collection_session', return_value=Mock()), \\
     patch.object(crawl_bills, 'collect_bills', side_effect=collect):
    print('ready', flush=True)
    crawl_bills.main()
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = os.environ | {'APP_DATA_DIR': directory, 'APP_DB_PATH': str(root / 'unused.sqlite3')}
            process = None
            try:
                with collection_lock(root):
                    process = subprocess.Popen([sys.executable, '-u', '-c', script, directory],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
                    self.assertEqual(process.stdout.readline().strip(), 'ready')
                    with self.assertRaises(subprocess.TimeoutExpired):
                        process.communicate(timeout=0.2)
                    self.assertFalse((root / 'collector-entered').exists())
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertEqual((root / 'collector-entered').read_text(), 'collected')
                self.assertFalse((root / 'unused.sqlite3').exists())
            finally:
                if process and process.poll() is None:
                    process.kill()
                    process.communicate()


if __name__ == '__main__':
    unittest.main()
