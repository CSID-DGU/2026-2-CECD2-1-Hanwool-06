"""로컬 실행기가 기존 DB 대신 임시 계정과 스냅샷을 사용하는지 확인한다."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class DevChatTest(unittest.TestCase):
    def test_launcher_isolates_db_credentials_workers_and_snapshot(self):
        root = Path(__file__).resolve().parents[1]
        if not (root / 'front/dist/index.html').is_file():
            self.skipTest('로컬 실행 확인에는 npm run build 결과가 필요합니다.')
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / 'existing.sqlite3'
            existing.write_bytes(b'existing database must remain untouched')
            result = subprocess.run([sys.executable, '-c', '''
import contextlib, io, json, os
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from back.api import dev_chat

paths = []
original_copy = dev_chat.copy_remote_snapshot
fixture = {'version': 'versions/local-test', 'manifest': {'published': True, 'directory': 'versions/local-test'},
           'daily': {'test': {'usage': [{'date': '2026-10-20', 'value': 7}]}}, 'risk': {}}
def copy_fixture(data_dir):
    from back.rag.storage import RemoteStore
    with patch.object(RemoteStore, 'snapshot', return_value=fixture):
        original_copy(data_dir)

output = io.StringIO()
def check_server(sockets):
    from back.api import config, catalog
    from back.api.main import app
    paths.append(config.DATA_DIR)
    assert config.DATA_DIR != Path(os.environ['EXPECTED_EXISTING']).parent
    assert config.APP_DB_PATH == config.DATA_DIR / 'app.sqlite3'
    assert not config.COLLECTION_WORKER_ENABLED and not config.OBJECT_STORAGE_ENABLED
    assert not config.smtp_ready() and config.ADMIN_EMAIL == '' and config.ADMIN_PASSWORD == ''
    login_file = config.DATA_DIR / 'login.json'
    assert login_file.stat().st_mode & 0o777 == 0o600
    credentials = json.loads(login_file.read_text())
    assert credentials['password'] not in output.getvalue()
    with TestClient(app) as client:
        assert client.get('/api/health').json() == {'ok': True, 'setup_required': False}
        response = client.post('/api/auth/login', json=credentials)
        assert response.status_code == 200, response.status_code
        user = response.json()['user']
        assert user['role'] == 'superadmin' and not user['must_change_password']
        assert catalog.data_sources() == (fixture['daily'], fixture['risk'])
        assert client.get('/api/data').status_code == 200

with patch.object(dev_chat, 'socket') as sockets, patch('uvicorn.Server.run', side_effect=check_server), \
     patch.object(dev_chat, 'copy_remote_snapshot', side_effect=copy_fixture), contextlib.redirect_stdout(output):
    listener = sockets.socket.return_value.__enter__.return_value
    listener.getsockname.return_value = ('127.0.0.1', 18111)
    dev_chat.main(['--no-build', '--port', '0', '--remote-snapshot'])
    listener.setsockopt.assert_called_once_with(sockets.SOL_SOCKET, sockets.SO_REUSEADDR, 1)
    listener.bind.assert_called_once_with(('127.0.0.1', 0))
assert paths and not paths[0].exists()
assert Path(os.environ['EXPECTED_EXISTING']).read_bytes() == b'existing database must remain untouched'
print('isolated launcher verified')
'''], cwd=root, env={**os.environ, 'APP_DB_PATH': str(existing), 'APP_DATA_DIR': directory,
                    'APP_ENV': 'production', 'COLLECTION_WORKER_ENABLED': 'true', 'OBJECT_STORAGE_ENABLED': 'true',
                    'EXPECTED_EXISTING': str(existing)}, text=True, capture_output=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('isolated launcher verified', result.stdout)
            self.assertEqual(existing.read_bytes(), b'existing database must remain untouched')


if __name__ == '__main__':
    unittest.main()
