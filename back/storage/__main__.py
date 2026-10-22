"""Object Storage 운영 도구. 저장소 루트에서 실행한다.

  python -m back.storage status                       # 설정·연결 확인(비밀값은 설정 여부만)
  python -m back.storage plan                         # 올릴 파일·제외 파일 목록(네트워크 없음)
  python -m back.storage migrate [--dry-run] [--verify-download]   # 최초 이전 + 무결성 검증 + 보고서
  python -m back.storage sync [--verify-download]     # 바뀐 파일만 올리기
  python -m back.storage verify [--download]          # 로컬 파일과 원격 객체 대조
  python -m back.storage backup-db [--keep N]         # 운영 DB 백업 올리기
  python -m back.storage list-backups
  python -m back.storage restore DEST [--db] [--overwrite]   # 새 서버로 내려받기
  python -m back.storage selftest                     # 수집 종료 훅 → 실제 버킷 업로드를 격리 접두어에서 검증(운영 자료 무영향)

로컬 파일은 어떤 명령도 지우지 않는다. 삭제 후보는 migrate 보고서에만 적힌다.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from back.api import config
from .client import StorageError, storage_from_config
from .sync import backup_database, list_backups, migrate, plan, restore_data_dir, sync_data_dir, verify_data_dir


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=1, default=str))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--data-dir', type=Path, default=config.DATA_DIR, help='기본값 APP_DATA_DIR')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    commands.add_parser('plan')
    migrate_cmd = commands.add_parser('migrate')
    migrate_cmd.add_argument('--dry-run', action='store_true')
    migrate_cmd.add_argument('--verify-download', action='store_true', help='올린 뒤 다시 내려받아 내용까지 비교')
    migrate_cmd.add_argument('--no-db-backup', action='store_true')
    sync_cmd = commands.add_parser('sync')
    sync_cmd.add_argument('--dry-run', action='store_true')
    sync_cmd.add_argument('--verify-download', action='store_true')
    verify_cmd = commands.add_parser('verify')
    verify_cmd.add_argument('--download', action='store_true')
    backup_cmd = commands.add_parser('backup-db')
    backup_cmd.add_argument('--keep', type=int, default=config.OBJECT_STORAGE_DB_BACKUP_KEEP)
    commands.add_parser('list-backups')
    restore_cmd = commands.add_parser('restore')
    restore_cmd.add_argument('destination', type=Path)
    restore_cmd.add_argument('--db', action='store_true', help='가장 최근 DB 백업도 destination/app.sqlite3 로 내려받기')
    restore_cmd.add_argument('--overwrite', action='store_true')
    commands.add_parser('selftest', help='격리된 임시 자료·접두어로 수집 종료 훅과 DB 백업 훅의 실제 업로드를 검증한다')
    args = parser.parse_args(argv)

    storage = storage_from_config()
    progress = lambda message: print(message, file=sys.stderr, flush=True)
    try:
        if args.command == 'status':
            status = storage.status()
            if storage.enabled and storage.settings.credentials_ready:
                try:
                    storage.ping()
                    status['reachable'] = True
                except StorageError as error:
                    status['reachable'] = False
                    status['error'] = str(error)
            _print(status)
            return 0 if not storage.enabled or status.get('reachable') else 1
        if args.command == 'plan':
            items = plan(args.data_dir, storage)
            summary = {}
            for item in items:
                entry = summary.setdefault(item['category'], {'label': item['label'], 'action': item['action'], 'files': 0, 'bytes': 0})
                entry['files'] += 1
                entry['bytes'] += item['size']
            _print({'data_dir': str(args.data_dir), 'summary': summary,
                    'upload': [i['relative'] for i in items if i['action'] == 'upload'],
                    'excluded': [i['relative'] for i in items if i['action'] == 'excluded']})
            return 0
        if not storage.enabled:
            parser.error('OBJECT_STORAGE_ENABLED=true 와 접속 정보를 환경변수에 설정하세요.')
        if args.command == 'migrate':
            report = migrate(args.data_dir, storage, config.APP_DB_PATH, dry_run=args.dry_run,
                             verify_download=args.verify_download, backup_db=not args.no_db_backup, progress=progress)
            _print({k: report[k] for k in ('report_path', 'inventory', 'database_backup') if k in report}
                   | {'sync': report['sync'].get('summary', report['sync']),
                      'verification': report.get('verification', {}).get('summary'),
                      'delete_candidates': len(report.get('delete_candidates', []))})
            return 1 if report['sync'].get('failed') or report.get('verification', {}).get('mismatch') else 0
        if args.command == 'sync':
            report = sync_data_dir(args.data_dir, storage, dry_run=args.dry_run, verify_download=args.verify_download, progress=progress)
            _print(report.get('summary', report) | {'failed_files': report.get('failed', [])})
            return 1 if report.get('failed') else 0
        if args.command == 'verify':
            report = verify_data_dir(args.data_dir, storage, download=args.download)
            _print({'summary': report['summary'], 'mismatch': report['mismatch'], 'missing_remote': report['missing_remote']})
            return 1 if report['mismatch'] or report['missing_remote'] else 0
        if args.command == 'backup-db':
            _print(backup_database(config.APP_DB_PATH, storage, keep=args.keep))
            return 0
        if args.command == 'list-backups':
            _print(list_backups(storage))
            return 0
        if args.command == 'selftest':
            from .selftest import run as selftest
            report = selftest(progress=progress)
            _print(report)
            print('SELFTEST', 'PASS' if report['passed'] else 'FAIL', file=sys.stderr)
            return 0 if report['passed'] else 1
        if args.command == 'restore':
            report = restore_data_dir(storage, args.destination, overwrite=args.overwrite, include_db=args.db, progress=progress)
            _print({'restored': len(report['restored']), 'skipped_existing': len(report['skipped_existing']),
                    'failed': report['failed'], 'database': report['database']})
            return 1 if report['failed'] else 0
    except StorageError as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
