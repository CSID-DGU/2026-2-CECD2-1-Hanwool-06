"""Local operator tools. Passwords are read from a terminal, never command arguments."""
import argparse
import getpass
import sqlite3
from pathlib import Path
from . import auth, config, db, static_page


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    backup = commands.add_parser('backup')
    backup.add_argument('destination', type=Path)
    backup.add_argument('--upload', action='store_true', help='Object Storage(backups/sqlite/)에도 올린다. OBJECT_STORAGE_* 설정 필요')
    commands.add_parser('reset-password').add_argument('email')
    commands.add_parser('export-static', help='로그인 없이 열어 보는 한 파일짜리 화면 저장본').add_argument('destination', type=Path)
    args = parser.parse_args()
    if not config.APP_DB_PATH.is_file():
        parser.error('운영 DB가 없습니다. API를 먼저 실행하세요.')
    if args.command == 'backup':
        target = args.destination.expanduser().resolve()
        if target.exists():
            parser.error('기존 파일을 덮어쓸 수 없습니다. 다른 백업 파일명을 선택하세요.')
        target.parent.mkdir(parents=True, exist_ok=True)
        with db.connect() as source, sqlite3.connect(target) as destination:
            source.backup(destination)
        target.chmod(0o600)
        print(f'백업 완료: {target}')
        if args.upload:
            from back.storage import StorageError, storage_from_config
            from back.storage.sync import backup_database
            try:
                result = backup_database(config.APP_DB_PATH, storage_from_config(), keep=config.OBJECT_STORAGE_DB_BACKUP_KEEP)
            except StorageError as error:
                parser.error(str(error))
            print(f"Object Storage 업로드 완료: {result['key']} ({result['size']} bytes)")
        return
    if args.command == 'export-static':
        try:
            target = static_page.build(args.destination.expanduser().resolve())
        except (FileNotFoundError, ValueError) as error:
            parser.error(str(error))
        print(f'저장했습니다: {target} ({target.stat().st_size / 1e6:.1f}MB). 자료가 통째로 들어 있으니 공개된 곳에 두지 마세요.')
        return
    with db.connect() as c:
        row = c.execute('SELECT id FROM users WHERE email=?', (args.email.strip().lower(),)).fetchone()
        if not row:
            parser.error('등록된 사용자가 없습니다.')
        password = getpass.getpass('새 비밀번호 (12~128자): ')
        if password != getpass.getpass('새 비밀번호 확인: '):
            parser.error('입력한 비밀번호가 다릅니다.')
        try:
            hashed = auth.hash_password(password)
        except ValueError as error:
            parser.error(str(error))
        c.execute('UPDATE users SET password_hash=?,must_change_password=1 WHERE id=?', (hashed, row['id']))
        c.execute('DELETE FROM sessions WHERE user_id=?', (row['id'],))
        db.audit(c, None, 'operator_password_reset', row['id'])
    print('비밀번호를 변경하고 기존 세션을 만료했습니다.')


if __name__ == '__main__':
    main()
