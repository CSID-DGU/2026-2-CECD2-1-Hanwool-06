"""수집 종료 훅 → Object Storage 자동 업로드를 실제 버킷에 대해 격리 검증한다 (`python -m back.storage selftest`).

운영 자료를 전혀 건드리지 않는다.
- 임시 APP_DATA_DIR·임시 SQLite DB 를 쓰고 끝나면 지운다 (운영 data/runtime, app.sqlite3, 색인 파일은 읽지도 않는다)
- 접두어 `<OBJECT_STORAGE_PREFIX>/_auto-sync-test/<실행ID>/` 아래에만 올리고 끝나면 그 아래 객체만 지운다
- 아리수·외부 API 를 부르지 않는다: 수집기를 가짜로 바꿔 민감정보 없는 테스트 파일만 만든다
- 업로드 함수를 직접 부르지 않는다: 실제 `collection.run_job` → `_run_job` → `hooks.after_collection_job` 경로로만 올라간다
"""
from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from .client import sha256_file, storage_from_config


def run(progress=None) -> dict:
    from back.api import collection, config, db
    from back.storage import hooks
    from back.storage.sync import list_backups

    notify = progress or (lambda message: None)
    run_id = uuid.uuid4().hex[:12]
    base_prefix = config.OBJECT_STORAGE_PREFIX.strip().strip('/')
    test_prefix = f"{base_prefix}/_auto-sync-test/{run_id}" if base_prefix else f"_auto-sync-test/{run_id}"
    report = {'run_id': run_id, 'test_prefix': test_prefix + '/', 'steps': {}}

    live = storage_from_config()
    live.ping()
    marker = live.prefix + '_auto-sync-test/'
    before_service = {i['key'] for i in live.list(live.prefix) if not i['key'].startswith(marker)}
    production_index = config.DATA_DIR / '.object-storage-index.json'
    index_stat = production_index.stat().st_mtime_ns if production_index.exists() else None
    db_stat = config.APP_DB_PATH.stat().st_mtime_ns if config.APP_DB_PATH.exists() else None

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        data_dir = root / 'runtime'
        data_dir.mkdir()
        with patch.object(config, 'DATA_DIR', data_dir), patch.object(config, 'APP_DB_PATH', root / 'test.sqlite3'), \
             patch.object(config, 'SEED_DIR', root), patch.object(config, 'OBJECT_STORAGE_PREFIX', test_prefix), \
             patch.object(config, 'OBJECT_STORAGE_DB_BACKUP_KEEP', 5):
            db.init_db()
            with db.connect() as c:
                c.execute("INSERT INTO offices VALUES('테스트사업소','테스트사업소')")
                c.execute("INSERT INTO stations VALUES('테스트역','테스트역',NULL,NULL)")
                c.execute("INSERT INTO meters(id,provider,customer_number,station_id,office_id,display_name,line,daily_enabled,created_at,updated_at) "
                          "VALUES('999999001','arisu','999999001','테스트역','테스트사업소','테스트 계량기','1',1,?,?)", (db.now(), db.now()))
                if not c.in_transaction:   # the inserts above already opened the implicit write transaction
                    c.execute('BEGIN IMMEDIATE')
                job_id = collection.enqueue(c, ['999999001'], 'manual')

            def fake_collect(meter, *, progress=None, session=None):
                runtime = config.DATA_DIR
                (runtime / 'collection').mkdir(parents=True, exist_ok=True)
                (runtime / 'daily' / 'water').mkdir(parents=True, exist_ok=True)
                (runtime / 'collection' / '999999001.json').write_text(json.dumps(
                    {'water_checked': {'2026-06': '2026-10-18'}, 'water_excluded': [], 'unlisted_runs': 0, 'bill_retry_from': None,
                     'bill_retry_windows': [], 'updated_at': db.now(), 'selftest': run_id}, ensure_ascii=False, indent=2), encoding='utf-8')
                (runtime / 'daily' / 'water' / '2026-06-01.csv').write_text(
                    '고객번호,역명,납기,사용일,검침일자,지침값,일사용량(톤),납기별누적사용량(톤)\n'
                    '999999001,테스트역,2026-06-30,2026-06-01,2026-06-02,1000,12.5,100\n', encoding='utf-8-sig')
                (runtime / 'daily' / 'water' / '2026-06-01.status.json').write_text(json.dumps(
                    {'date': '2026-06-01', 'expected': 1, 'fetched': 1, 'missing': [], 'errors': [], 'status': 'complete', 'selftest': run_id}), encoding='utf-8')
                (runtime / 'app.sqlite3-journal').write_bytes(b'')   # 제외되어야 하는 DB 부속 파일
                return {'status': 'success', 'message': '셀프테스트 수집 완료', 'daily_rows': 1, 'bill_rows': 0,
                        'daily_available': True, 'connection_verified': True, 'retryable': False}

            calls = {'hook': 0}
            real_hook = hooks.after_collection_job

            def spy_hook(job_id=None):
                calls['hook'] += 1
                return real_hook(job_id)

            notify('1/5 수집 작업을 실행하고 종료 훅으로 올립니다')
            with patch('back.storage.hooks.after_collection_job', side_effect=spy_hook), \
                 patch('back.scripts.billing_etl.i121_crawler.auth.collection_session', return_value=Mock(arisu_login_error=None)), \
                 patch('back.pipelines.incremental.collect_meter', side_effect=fake_collect):
                collection.run_job(job_id, threading.Event())
            with db.connect() as c:
                job_status = c.execute('SELECT status FROM collection_jobs WHERE id=?', (job_id,)).fetchone()[0]
            report['steps']['collection_job'] = {'job_status': job_status, 'hook_called': calls['hook']}

            notify('2/5 버킷의 객체와 무결성을 확인합니다')
            test_storage = storage_from_config()
            remote = {i['key'] for i in test_storage.list(test_storage.prefix)}
            expected = {f"{test_prefix}/collection/999999001.json", f"{test_prefix}/daily/water/2026-06-01.csv",
                        f"{test_prefix}/daily/water/2026-06-01.status.json"}
            objects = {}
            for key in sorted(expected):
                relative = key[len(test_prefix) + 1:]
                local = data_dir.joinpath(*relative.split('/'))
                head = test_storage.head(key)
                down = root / 'download' / local.name
                if head:
                    test_storage.get_file(key, down, expected_sha256=head['sha256'])
                objects[relative] = {'exists': key in remote, 'size': local.stat().st_size,
                                     'size_match': bool(head) and head['size'] == local.stat().st_size,
                                     'metadata_sha256_match': bool(head) and head['sha256'] == sha256_file(local),
                                     'downloaded_sha256_match': bool(head) and down.exists() and sha256_file(down) == sha256_file(local)}
            report['steps']['objects'] = objects
            report['steps']['only_expected_keys'] = remote == expected
            report['steps']['unexpected_keys'] = sorted(remote - expected)

            notify('3/5 같은 작업을 다시 돌려 중복 업로드가 없는지 확인합니다')
            with db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                job2 = collection.enqueue(c, ['999999001'], 'manual')
            with patch('back.scripts.billing_etl.i121_crawler.auth.collection_session', return_value=Mock(arisu_login_error=None)), \
                 patch('back.pipelines.incremental.collect_meter', side_effect=fake_collect):
                collection.run_job(job2, threading.Event())
            report['steps']['rerun_unchanged'] = len(test_storage.list(test_storage.prefix)) == len(expected)

            notify('4/5 하루 한 번 DB 백업 훅을 확인합니다 (임시 DB)')
            with db.connect() as c:
                c.execute("INSERT INTO settings VALUES('collection_auto_settled','selftest') ON CONFLICT(key) DO UPDATE SET value=excluded.value")
            hooks.daily_maintenance(force=True)
            backups = list_backups(test_storage)
            backup = {'backups': len(backups)}
            if backups:
                head = test_storage.head(backups[-1]['key'])
                down_db = root / 'download' / 'backup.sqlite3'
                test_storage.get_file(backups[-1]['key'], down_db, expected_sha256=head['sha256'])
                conn = sqlite3.connect(down_db)
                backup['restored_meters'] = conn.execute('SELECT count(*) FROM meters').fetchone()[0]
                conn.close()
                with db.connect() as c:
                    backup['audit'] = [r[0] for r in c.execute("SELECT action FROM audit_log WHERE action LIKE 'storage_%'")]
            report['steps']['daily_maintenance'] = backup

            (data_dir / 'refresh_report.json').write_text(json.dumps({'selftest': run_id, 'errors': []}), encoding='utf-8')
            hooks.after_refresh(data_dir)
            report['steps']['after_refresh_uploaded'] = test_storage.exists(f"{test_prefix}/refresh_report.json")

            notify('5/5 테스트 객체를 정리합니다')
            created = [i['key'] for i in test_storage.list(test_storage.prefix)]
            for key in created:
                test_storage.delete(key)
            report['steps']['cleanup'] = {'deleted': len(created), 'remaining': len(test_storage.list(test_storage.prefix))}

    after_service = {i['key'] for i in live.list(live.prefix) if not i['key'].startswith(marker)}
    report['production_untouched'] = {
        'service_objects_before': len(before_service), 'service_objects_after': len(after_service), 'same_set': before_service == after_service,
        'production_index_unchanged': (production_index.stat().st_mtime_ns if production_index.exists() else None) == index_stat,
        'production_db_unchanged': (config.APP_DB_PATH.stat().st_mtime_ns if config.APP_DB_PATH.exists() else None) == db_stat}
    steps = report['steps']
    report['passed'] = bool(
        steps['collection_job']['job_status'] == 'success' and steps['collection_job']['hook_called'] == 1
        and steps['only_expected_keys'] and all(all(v.values()) for v in steps['objects'].values())
        and steps['rerun_unchanged'] and steps['daily_maintenance'].get('backups') == 1 and steps['after_refresh_uploaded']
        and steps['cleanup']['remaining'] == 0 and report['production_untouched']['same_set'])
    return report
