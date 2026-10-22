"""기존 DB와 분리한 로컬 채팅 검증 서버. python -m back.api.dev_chat --remote-snapshot"""
import argparse
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import tempfile


def copy_remote_snapshot(data_dir):
    from back.rag.storage import RemoteStore

    snapshot = RemoteStore().snapshot()
    target = data_dir / 'snapshot' / snapshot['version']
    target.mkdir(parents=True)
    for name in ('daily', 'risk'):
        (target / f'{name}.json').write_text(json.dumps(snapshot[name], ensure_ascii=False, allow_nan=False), encoding='utf-8')
    # 두 자료를 모두 쓴 뒤 manifest를 두어 기존 상세 화면이 같은 게시 버전을 읽게 한다.
    (data_dir / 'snapshot/manifest.json').write_text(json.dumps(snapshot['manifest'], ensure_ascii=False, allow_nan=False), encoding='utf-8')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8011, help='127.0.0.1 포트 (기본 8011, 0이면 빈 포트)')
    parser.add_argument('--remote-snapshot', action='store_true', help='NCP 게시 자료를 임시 폴더에 읽어 상세 화면에도 사용 (업로드 없음)')
    parser.add_argument('--no-build', action='store_true', help='이미 빌드한 front/dist 사용')
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error('포트는 0~65535 사이여야 합니다.')
    root = Path(__file__).resolve().parents[2]
    if not args.no_build:
        subprocess.run(['npm', 'run', 'build', '--workspace', 'front'], cwd=root, check=True)
    if not (root / 'front/dist/index.html').is_file():
        parser.error('front/dist가 없습니다. --no-build 없이 실행하세요.')

    # .env를 읽기 전에 두 경로를 모두 고정한다. 기존 APP_DB_PATH가 남으면 운영 DB를 열 수 있다.
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='water-chat-dev-') as temporary, socket.socket() as listener:
        data_dir = Path(temporary).resolve()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', args.port))
        port = listener.getsockname()[1]
        origin = f'http://127.0.0.1:{port}'
        os.environ.update({
            'APP_ENV': 'development', 'APP_DATA_DIR': str(data_dir), 'APP_DB_PATH': str(data_dir / 'app.sqlite3'),
            'APP_COOKIE_SECURE': 'false', 'APP_ALLOWED_HOSTS': '127.0.0.1,localhost,testserver',
            'APP_ORIGINS': origin, 'COLLECTION_WORKER_ENABLED': 'false', 'OBJECT_STORAGE_ENABLED': 'false',
            'ADMIN_EMAIL': '', 'ADMIN_PASSWORD': '', 'SMTP_USER': '', 'SMTP_PASS': '', 'ALERT_FROM': '',
            'TODAY_OVERRIDE': '',
        })
        from . import auth, config, db
        import uvicorn

        if config.DATA_DIR != data_dir or config.APP_DB_PATH != data_dir / 'app.sqlite3':
            parser.error('설정이 이미 로드되어 격리할 수 없습니다. 새 터미널 프로세스에서 실행하세요.')
        db.init_db()
        credentials = {'email': 'local-chat@example.invalid', 'password': secrets.token_urlsafe(24)}
        with db.connect() as connection:
            connection.execute('INSERT INTO users(email,name,password_hash,role,must_change_password,created_at) VALUES(?,?,?,?,?,?)',
                               (credentials['email'], '로컬 검증 관리자', auth.hash_password(credentials['password']), 'superadmin', 0, db.now()))
        login_file = data_dir / 'login.json'
        login_file.write_text(json.dumps(credentials), encoding='utf-8')
        login_file.chmod(0o600)
        if args.remote_snapshot:
            print('NCP 게시 스냅샷을 임시 폴더로 읽는 중…', flush=True)
            try:
                copy_remote_snapshot(data_dir)
            except Exception:
                parser.exit(1, 'NCP 스냅샷을 읽지 못했습니다. CHAT_STORAGE_* 설정과 읽기 권한을 확인하세요.\n')
        from .main import app

        print(f'로컬 검증 URL: {origin}\n임시 데이터: {data_dir}\n로그인 정보 파일: {login_file}', flush=True)
        print('로그인 정보는 화면에 출력하지 않습니다. 종료(Ctrl+C)하면 임시 DB·로그인 파일·스냅샷이 삭제됩니다.', flush=True)
        if not args.remote_snapshot:
            print('상세 화면은 저장소의 기존 자료를 사용합니다. NCP 자료와 비교하려면 --remote-snapshot으로 실행하세요.', flush=True)
        uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port)).run(sockets=[listener])


if __name__ == '__main__':
    # 종료 신호에도 TemporaryDirectory 정리가 실행되도록 한다.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        main()
    except KeyboardInterrupt:
        pass
