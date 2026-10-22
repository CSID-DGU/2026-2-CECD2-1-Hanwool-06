"""Object Storage 보관 모듈: 가짜 S3 클라이언트로 키 배치·동기화·백업·복원·마이그레이션을 검증한다. 네트워크는 쓰지 않는다."""
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from unittest.mock import patch

from back.api import config
from back.storage import client as storage_client, hooks, layout
from back.storage.client import ObjectStorage, StorageError, StorageSettings
from back.storage.sync import (backup_database, list_backups, load_index, migrate, plan, restore_data_dir,
                               sync_data_dir, verify_data_dir)


class ClientError(Exception):
    """botocore.exceptions.ClientError 와 같은 모양(response 속성)."""

    def __init__(self, code, status):
        super().__init__(code)
        self.response = {'Error': {'Code': code}, 'ResponseMetadata': {'HTTPStatusCode': status}}


class EndpointConnectionError(Exception):
    pass


class FakeS3:
    """boto3 S3 클라이언트에서 쓰는 호출만 흉내 낸다. 객체는 메모리에 둔다."""

    def __init__(self):
        self.objects = {}
        self.calls = []
        self.fail_next = []   # 다음 호출에서 던질 예외 목록

    def _maybe_fail(self):
        if self.fail_next:
            raise self.fail_next.pop(0)

    def head_bucket(self, Bucket):
        self.calls.append(('head_bucket', Bucket))
        self._maybe_fail()
        return {}

    def put_object(self, Bucket, Key, Body, Metadata=None, ContentType=None):
        self.calls.append(('put_object', Key))
        self._maybe_fail()
        self.objects[Key] = {'Body': bytes(Body), 'Metadata': dict(Metadata or {}), 'LastModified': datetime.now(timezone.utc)}
        return {}

    def upload_fileobj(self, Fileobj, Bucket, Key, ExtraArgs=None):
        self.calls.append(('upload_fileobj', Key))
        self._maybe_fail()
        self.objects[Key] = {'Body': Fileobj.read(), 'Metadata': dict((ExtraArgs or {}).get('Metadata', {})),
                             'LastModified': datetime.now(timezone.utc)}

    def head_object(self, Bucket, Key):
        self.calls.append(('head_object', Key))
        self._maybe_fail()
        if Key not in self.objects:
            raise ClientError('404', 404)
        item = self.objects[Key]
        return {'ContentLength': len(item['Body']), 'Metadata': item['Metadata'], 'ETag': '"x"', 'LastModified': item['LastModified']}

    def get_object(self, Bucket, Key):
        self.calls.append(('get_object', Key))
        self._maybe_fail()
        if Key not in self.objects:
            raise ClientError('NoSuchKey', 404)
        return {'Body': io.BytesIO(self.objects[Key]['Body'])}

    def download_fileobj(self, Bucket, Key, Fileobj):
        self.calls.append(('download_fileobj', Key))
        self._maybe_fail()
        if Key not in self.objects:
            raise ClientError('NoSuchKey', 404)
        Fileobj.write(self.objects[Key]['Body'])

    def list_objects_v2(self, Bucket, Prefix='', MaxKeys=1000, ContinuationToken=None):
        self.calls.append(('list_objects_v2', Prefix))
        self._maybe_fail()
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page = keys[start:start + MaxKeys]
        response = {'Contents': [{'Key': k, 'Size': len(self.objects[k]['Body']), 'LastModified': self.objects[k]['LastModified']} for k in page],
                    'IsTruncated': start + MaxKeys < len(keys)}
        if response['IsTruncated']:
            response['NextContinuationToken'] = str(start + MaxKeys)
        return response

    def delete_object(self, Bucket, Key):
        self.calls.append(('delete_object', Key))
        self._maybe_fail()
        self.objects.pop(Key, None)
        return {}


SETTINGS = StorageSettings(enabled=True, endpoint='https://kr.object.ncloudstorage.com', region='kr-standard', bucket='bucket',
                           access_key='access', secret_key='secret', prefix='mulsaemi/')


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class LayoutTest(unittest.TestCase):
    def test_every_runtime_file_type_has_a_category_and_mirrored_key(self):
        cases = {'app.sqlite3': ('database', False), 'app.sqlite3-wal': ('database', False), 'app.sqlite3-shm': ('database', False),
                 'app.sqlite3-journal': ('database', False), '.app-backup.x1y2z3.sqlite3': ('database', False), '.collection.lock': ('lock', False),
                 '.object-storage-index.json': ('storage_index', False), 'daily/water/.2026-06-01.csv.ab12cd': ('temp', False),
                 'collection/000000001.json': ('collection_state', True), 'daily/water/2026-06-01.csv': ('daily_water', True),
                 'daily/water/2026-06-01.status.json': ('daily_water', True), 'daily/ridership/2026-06-01.csv': ('daily_ridership', True),
                 'raw/billing_i121/i121_bills/bills_long.csv': ('raw_bills', True), 'raw/billing_i121/i121_cache/x.html': ('raw_bill_cache', True),
                 'raw/ridership_monthly/CARD_SUBWAY_MONTH_202605.csv': ('raw_ridership_monthly', True), 'dataset/train.csv': ('dataset', True),
                 'processed/water_bills.csv': ('processed', True), 'model/model.txt': ('model', True),
                 'snapshot/manifest.json': ('snapshot', True), 'snapshot/versions/abc/risk.json': ('snapshot', True),
                 'refresh_report.json': ('report', True), 'notes/anything.txt': ('other', True),
                 'history/processed/daily_water_usage.csv': ('history', True), 'history/app_seed/station_positions.json': ('history', True),
                 'history/daily/water/강동역.csv': ('history', True)}
        for relative, (name, archive) in cases.items():
            category = layout.classify(relative)
            self.assertEqual((category.name, category.archive), (name, archive), relative)
        self.assertEqual(layout.object_key('mulsaemi', 'daily/water/2026-06-01.csv'), 'mulsaemi/daily/water/2026-06-01.csv')
        self.assertEqual(layout.object_key('', 'snapshot/manifest.json'), 'snapshot/manifest.json')
        self.assertEqual(layout.relative_from_key('mulsaemi/', 'mulsaemi/model/model.txt'), PurePosixPath('model/model.txt'))
        for bad in ('../x', '/abs', ''):
            with self.assertRaises(ValueError):
                layout.object_key('p', bad)
        with self.assertRaises(ValueError):
            layout.relative_from_key('mulsaemi/', 'other/model.txt')
        key = layout.backup_key('mulsaemi', datetime(2026, 10, 17, 1, 2, 3, tzinfo=timezone.utc))
        self.assertEqual(key, 'mulsaemi/backups/sqlite/app-20261017T010203Z.sqlite3')
        self.assertEqual(layout.backup_moment(key), '20261017T010203Z')
        self.assertIsNone(layout.backup_moment('mulsaemi/backups/sqlite/other.sqlite3'))


class ClientTest(unittest.TestCase):
    def test_disabled_storage_refuses_connections_and_exposes_no_secrets(self):
        storage = ObjectStorage(StorageSettings(enabled=False, endpoint='', region='', bucket='', access_key='', secret_key='', prefix=''))
        with self.assertRaises(storage_client.StorageDisabled):
            storage.client()
        enabled = ObjectStorage(SETTINGS)
        status = json.dumps(enabled.status())
        self.assertNotIn('secret', status.replace('"credentials_configured"', ''))
        self.assertNotIn('access', status.replace('"credentials_configured"', ''))
        self.assertTrue(enabled.status()['credentials_configured'])

    def test_transient_errors_are_retried_and_others_become_storage_errors_with_code_only(self):
        fake = FakeS3()
        storage = ObjectStorage(SETTINGS, client=fake)
        fake.fail_next = [EndpointConnectionError('endpoint https://x?AWSAccessKeyId=access')]
        with patch.object(storage_client.time, 'sleep') as sleep:
            storage.put_bytes('mulsaemi/a.txt', b'hello')
        sleep.assert_called_once()
        self.assertEqual(storage.get_bytes('mulsaemi/a.txt'), b'hello')
        fake.fail_next = [ClientError('AccessDenied', 403)]
        with self.assertRaises(StorageError) as error:
            storage.put_bytes('mulsaemi/b.txt', b'x')
        self.assertIn('AccessDenied', str(error.exception))
        self.assertNotIn('access', str(error.exception).replace('AccessDenied', ''))
        self.assertIsNone(storage.head('mulsaemi/missing'))
        self.assertFalse(storage.exists('mulsaemi/missing'))

    def test_file_upload_and_download_verify_sha256(self):
        fake = FakeS3()
        storage = ObjectStorage(SETTINGS, client=fake)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'file.bin'
            source.write_bytes(b'content' * 1000)
            result = storage.put_file(source, 'mulsaemi/file.bin')
            self.assertEqual(result['sha256'], sha(source.read_bytes()))
            self.assertEqual(storage.head('mulsaemi/file.bin')['sha256'], result['sha256'])
            target = Path(directory) / 'restored' / 'file.bin'
            storage.get_file('mulsaemi/file.bin', target, expected_sha256=result['sha256'])
            self.assertEqual(target.read_bytes(), source.read_bytes())
            fake.objects['mulsaemi/file.bin']['Body'] = b'corrupted'
            with self.assertRaises(StorageError):
                storage.get_file('mulsaemi/file.bin', target, expected_sha256=result['sha256'])
            self.assertEqual(target.read_bytes(), source.read_bytes())   # 깨진 내려받기는 기존 파일을 건드리지 않는다
            self.assertEqual([p.name for p in target.parent.iterdir()], ['file.bin'])   # 임시 파일은 남지 않는다

    def test_list_follows_continuation_tokens(self):
        fake = FakeS3()
        storage = ObjectStorage(SETTINGS, client=fake)
        for n in range(2500):
            fake.objects[f'mulsaemi/x/{n:05d}'] = {'Body': b'1', 'Metadata': {}, 'LastModified': None}
        self.assertEqual(len(storage.list('mulsaemi/x/')), 2500)


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / 'runtime'
        files = {'app.sqlite3': b'sqlite', 'app.sqlite3-wal': b'wal', '.collection.lock': b'', '.collection-worker.lock': b'',
                 'collection/000000001.json': b'{"water_checked": {}}', 'daily/water/2026-06-01.csv': b'a,b\n1,2\n',
                 'daily/water/2026-06-01.status.json': b'{}', 'snapshot/manifest.json': b'{"directory": "versions/abc"}',
                 'snapshot/versions/abc/daily.json': b'{}', 'snapshot/versions/abc/risk.json': b'{}', 'model/model.txt': b'tree',
                 'refresh_report.json': b'{}', 'daily/water/.2026-06-02.csv.tmp123': b'half'}
        for relative, content in files.items():
            path = self.data / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.db_path = self.data / 'app.sqlite3'
        self.db_path.unlink()
        conn = sqlite3.connect(self.db_path)
        with conn:
            conn.execute('CREATE TABLE t(x)')
            conn.executemany('INSERT INTO t VALUES(?)', [(n,) for n in range(100)])
        conn.close()
        (self.data / 'app.sqlite3-wal').write_bytes(b'wal')   # SQLite 가 열 때 지우는 낡은 WAL 흉내: DB 를 만든 뒤 둔다
        self.fake = FakeS3()
        self.storage = ObjectStorage(SETTINGS, client=self.fake)

    def tearDown(self):
        self.tmp.cleanup()

    def test_plan_separates_archive_and_excluded_without_network(self):
        items = plan(self.data, self.storage)
        actions = {i['relative']: i['action'] for i in items}
        self.assertEqual(actions['app.sqlite3'], 'excluded')
        self.assertEqual(actions['app.sqlite3-wal'], 'excluded')
        self.assertEqual(actions['.collection.lock'], 'excluded')
        self.assertEqual(actions['daily/water/.2026-06-02.csv.tmp123'], 'excluded')
        self.assertEqual(actions['daily/water/2026-06-01.csv'], 'upload')
        self.assertEqual(actions['snapshot/versions/abc/risk.json'], 'upload')
        self.assertEqual(self.fake.calls, [])

    def test_sync_is_idempotent_and_uploads_only_changes(self):
        first = sync_data_dir(self.data, self.storage)
        uploaded = {u['relative'] for u in first['uploaded']}
        self.assertEqual(uploaded, {'collection/000000001.json', 'daily/water/2026-06-01.csv', 'daily/water/2026-06-01.status.json',
                                    'snapshot/manifest.json', 'snapshot/versions/abc/daily.json', 'snapshot/versions/abc/risk.json',
                                    'model/model.txt', 'refresh_report.json'})
        self.assertNotIn('mulsaemi/app.sqlite3', self.fake.objects)
        self.assertEqual(self.fake.objects['mulsaemi/daily/water/2026-06-01.csv']['Metadata']['sha256'], sha(b'a,b\n1,2\n'))
        self.assertTrue((self.data / layout.INDEX_FILE).is_file())
        before = len(self.fake.calls)
        second = sync_data_dir(self.data, self.storage)
        self.assertEqual(second['summary']['uploaded'], 0)
        self.assertEqual(second['summary']['unchanged'], 8)
        self.assertEqual(len(self.fake.calls), before)   # 바뀐 것이 없으면 요청도 없다
        (self.data / 'daily/water/2026-06-01.csv').write_bytes(b'a,b\n1,3\n')
        (self.data / 'daily/water/2026-06-02.csv').write_bytes(b'new')
        third = sync_data_dir(self.data, self.storage)
        self.assertEqual({u['relative'] for u in third['uploaded']}, {'daily/water/2026-06-01.csv', 'daily/water/2026-06-02.csv'})
        self.assertEqual(self.fake.objects['mulsaemi/daily/water/2026-06-01.csv']['Body'], b'a,b\n1,3\n')
        # 색인이 없어져도(새 서버) 원격 해시가 같으면 다시 올리지 않는다
        (self.data / layout.INDEX_FILE).unlink()
        fourth = sync_data_dir(self.data, self.storage)
        self.assertEqual(fourth['summary']['uploaded'], 0)
        self.assertEqual(fourth['summary']['verified_remote'], 9)

    def test_sync_reports_failures_and_continues_with_other_files(self):
        self.fake.fail_next = [ClientError('AccessDenied', 403)]
        with patch.object(storage_client.time, 'sleep'):
            report = sync_data_dir(self.data, self.storage)
        self.assertEqual(len(report['failed']), 1)
        self.assertEqual(report['failed'][0]['error'], 'StorageError')
        self.assertEqual(report['summary']['uploaded'], 7)
        retry = sync_data_dir(self.data, self.storage)
        self.assertEqual(retry['summary']['uploaded'], 1)   # 실패한 파일만 다음에 올라간다
        self.assertEqual(retry['failed'], [])

    def test_only_the_snapshot_version_the_screen_reads_is_kept_in_the_bucket(self):
        old = self.data / 'snapshot/versions/old'
        old.mkdir()
        (old / 'daily.json').write_bytes(b'{"old": 1}')
        self.fake.objects['mulsaemi/snapshot/versions/old/daily.json'] = {   # 고치기 전 동기화가 올려 둔 지난 버전
            'Body': b'{"old": 1}', 'Metadata': {}, 'LastModified': datetime.now(timezone.utc)}
        report = sync_data_dir(self.data, self.storage)
        self.assertIn('snapshot/versions/abc/daily.json', {u['relative'] for u in report['uploaded']})
        self.assertIn(('snapshot/versions/old/daily.json', 'old_snapshot'), {(e['relative'], e['category']) for e in report['excluded']})
        self.assertEqual(report['pruned'], ['mulsaemi/snapshot/versions/old/daily.json'])
        self.assertNotIn('snapshot/versions/old/daily.json', verify_data_dir(self.data, self.storage)['missing_remote'])

        def versions():
            return sorted(k for k in self.fake.objects if '/snapshot/versions/' in k)
        # 화면이 새 버전으로 바뀌면 새 버전을 올리고 버킷의 이전 버전은 지운다
        new = self.data / 'snapshot/versions/new'
        new.mkdir()
        (new / 'daily.json').write_bytes(b'{"new": 1}')
        (self.data / 'snapshot/manifest.json').write_bytes(b'{"directory": "versions/new"}')
        start = len(self.fake.calls)
        sync_data_dir(self.data, self.storage)
        self.assertEqual(versions(), ['mulsaemi/snapshot/versions/new/daily.json'])
        uploads = [key for call, key in self.fake.calls[start:] if call == 'upload_fileobj' and '/snapshot/' in key]
        self.assertEqual(uploads[-1], 'mulsaemi/snapshot/manifest.json')   # manifest 는 그 버전을 다 올린 뒤에
        # 새 버전을 올리다 실패하면 버킷의 지금 버전은 지우지 않는다
        newer = self.data / 'snapshot/versions/newer'
        newer.mkdir()
        (newer / 'daily.json').write_bytes(b'{"newer": 1}')
        (self.data / 'snapshot/manifest.json').write_bytes(b'{"directory": "versions/newer"}')
        put_file = self.storage.put_file

        def failing(path, key, **kwargs):
            if '/versions/newer/' in key:
                raise StorageError('AccessDenied')
            return put_file(path, key, **kwargs)
        with patch.object(self.storage, 'put_file', side_effect=failing):
            failed = sync_data_dir(self.data, self.storage)
        self.assertEqual(failed['pruned'], [])
        self.assertEqual(versions(), ['mulsaemi/snapshot/versions/new/daily.json'])
        self.assertIn(b'versions/new', self.fake.objects['mulsaemi/snapshot/manifest.json']['Body'])   # 버킷은 아직 올라간 버전을 가리킨다
        # 지금 버전을 알 수 없으면(manifest 없음) 버킷에서 아무것도 지우지 않는다
        (self.data / 'snapshot/manifest.json').unlink()
        self.assertEqual(sync_data_dir(self.data, self.storage)['pruned'], [])
        self.assertEqual(versions(), ['mulsaemi/snapshot/versions/new/daily.json'])

    def seed(self, relative, body):
        self.fake.objects['mulsaemi/' + relative] = {'Body': body, 'Metadata': {'sha256': sha(body)}, 'LastModified': datetime.now(timezone.utc)}

    def test_restored_server_without_the_current_version_keeps_the_last_complete_snapshot(self):
        # 새 버전(v2)을 올리다 옛 서버가 멈춘 버킷: manifest 는 v2, 버전은 v1 만 있다. 그 버킷에서 복원한 새 서버의 첫 동기화.
        for relative in ('snapshot/versions/abc/daily.json', 'snapshot/versions/abc/risk.json'):
            (self.data / relative).unlink()
        (self.data / 'snapshot/versions/abc').rmdir()
        v1 = self.data / 'snapshot/versions/v1'
        v1.mkdir()
        (v1 / 'daily.json').write_bytes(b'{"v1": 1}')
        (self.data / 'snapshot/manifest.json').write_bytes(b'{"directory": "versions/v2"}')
        self.seed('snapshot/manifest.json', b'{"directory": "versions/v2"}')
        self.seed('snapshot/versions/v1/daily.json', b'{"v1": 1}')
        report = sync_data_dir(self.data, self.storage)
        self.assertEqual(report['pruned'], [])
        self.assertIn('mulsaemi/snapshot/versions/v1/daily.json', self.fake.objects)

    def test_snapshot_switched_during_a_sync_is_left_for_the_next_one(self):
        old = self.data / 'snapshot/versions/old'
        old.mkdir()
        (old / 'daily.json').write_bytes(b'{"old": 1}')
        self.seed('snapshot/versions/old/daily.json', b'{"old": 1}')
        from back.storage import sync as sync_module
        with patch.object(sync_module, 'current_snapshot', side_effect=['versions/abc', 'versions/later']):
            report = sync_data_dir(self.data, self.storage)
        self.assertIn({'relative': 'snapshot/manifest.json', 'error': 'SnapshotNotReady'}, report['failed'])
        self.assertNotIn('mulsaemi/snapshot/manifest.json', self.fake.objects)
        self.assertEqual(report['pruned'], [])
        self.assertIn('mulsaemi/snapshot/versions/old/daily.json', self.fake.objects)

    def test_nothing_is_pruned_unless_the_current_version_is_really_in_the_bucket(self):
        sync_data_dir(self.data, self.storage)
        self.seed('snapshot/versions/old/daily.json', b'{"old": 1}')
        del self.fake.objects['mulsaemi/snapshot/versions/abc/risk.json']   # 버킷에서 사라진 지금 버전 파일(색인은 올렸다고 기억)
        (self.data / 'snapshot/manifest.json').write_bytes(b'{"directory": "versions/abc", "refreshed": 1}')
        report = sync_data_dir(self.data, self.storage)
        self.assertEqual(report['pruned'], [])
        self.assertIn('mulsaemi/snapshot/versions/old/daily.json', self.fake.objects)

    def test_dry_run_changes_nothing(self):
        report = sync_data_dir(self.data, self.storage, dry_run=True)
        self.assertEqual(len(report['uploaded']), 8)
        self.assertNotIn('pruned', report['summary'])   # 시험 실행은 무엇을 지울지 계산하지 않는다
        self.assertTrue(all(u.get('planned') for u in report['uploaded']))
        self.assertEqual(self.fake.objects, {})
        self.assertFalse((self.data / layout.INDEX_FILE).exists())

    def test_database_backup_is_consistent_uploaded_and_pruned(self):
        db_path = self.db_path
        moments = [datetime(2026, 10, d, 9, tzinfo=timezone.utc) for d in range(1, 5)]
        results = [backup_database(db_path, self.storage, keep=3, moment=m) for m in moments]
        self.assertEqual(results[-1]['pruned'], ['mulsaemi/backups/sqlite/app-20261001T090000Z.sqlite3'])
        backups = list_backups(self.storage)
        self.assertEqual([b['key'][-24:-8] for b in backups], ['20261002T090000Z', '20261003T090000Z', '20261004T090000Z'])
        restored = self.root / 'restored.sqlite3'
        self.storage.get_file(backups[-1]['key'], restored, expected_sha256=self.storage.head(backups[-1]['key'])['sha256'])
        conn = sqlite3.connect(restored)
        self.assertEqual(conn.execute('SELECT count(*) FROM t').fetchone()[0], 100)
        conn.close()
        self.assertEqual([p.name for p in self.data.iterdir() if p.name.startswith('.app-backup.')], [])
        with self.assertRaises(StorageError):
            backup_database(self.root / 'missing.sqlite3', self.storage)
        # keep=0 이면 정리하지 않는다
        backup_database(db_path, self.storage, keep=0, moment=datetime(2026, 10, 5, tzinfo=timezone.utc))
        self.assertEqual(len(list_backups(self.storage)), 4)

    def test_restore_recreates_the_data_dir_without_overwriting_existing_files(self):
        sync_data_dir(self.data, self.storage)
        db_path = self.db_path
        backup_database(db_path, self.storage, keep=0)
        destination = self.root / 'new-server'
        destination.mkdir()
        (destination / 'model').mkdir()
        (destination / 'model' / 'model.txt').write_bytes(b'local edit')
        report = restore_data_dir(self.storage, destination, include_db=True)
        self.assertEqual(sorted(report['restored']), sorted({'collection/000000001.json', 'daily/water/2026-06-01.csv',
                         'daily/water/2026-06-01.status.json', 'snapshot/manifest.json', 'snapshot/versions/abc/daily.json',
                         'snapshot/versions/abc/risk.json', 'refresh_report.json'}))
        self.assertEqual(report['skipped_existing'], ['model/model.txt'])
        self.assertEqual((destination / 'model' / 'model.txt').read_bytes(), b'local edit')
        self.assertEqual((destination / 'daily/water/2026-06-01.csv').read_bytes(), b'a,b\n1,2\n')
        self.assertTrue((destination / 'app.sqlite3').is_file())
        self.assertEqual(report['database']['key'], list_backups(self.storage)[-1]['key'])
        self.assertFalse((destination / layout.INDEX_FILE).exists())
        again = restore_data_dir(self.storage, destination, include_db=True)
        self.assertEqual(again['restored'], [])
        self.assertIn('skipped_existing', again['database'])

    def test_migration_verifies_integrity_reports_delete_candidates_and_deletes_nothing(self):
        db_path = self.db_path
        def listing():   # 잠금 파일과 SQLite 가 열 때 스스로 정리하는 가짜 WAL 은 비교에서 뺀다
            return sorted(str(p.relative_to(self.data)) for p in self.data.rglob('*') if p.is_file()
                          and not p.name.endswith(('.lock', '-wal')) and p.name != layout.INDEX_FILE
                          and not p.name.startswith(layout.MIGRATION_REPORT_PREFIX))
        before = listing()
        dry = migrate(self.data, self.storage, db_path, dry_run=True)
        self.assertEqual(self.fake.objects, {})
        self.assertEqual(dry['inventory']['database']['files'], 2)
        self.assertFalse(dry['inventory']['database']['archive'])
        self.assertEqual(dry['database_backup'], {'planned': True, 'key_prefix': 'mulsaemi/backups/sqlite/'})
        report = migrate(self.data, self.storage, db_path, verify_download=True)
        self.assertEqual(report['sync']['summary']['failed'], 0)
        self.assertEqual(report['verification']['summary']['mismatch'], 0)
        self.assertEqual(report['verification']['summary']['missing_remote'], 0)
        self.assertEqual(sorted(report['delete_candidates']), sorted({'collection/000000001.json', 'daily/water/2026-06-01.csv',
                         'daily/water/2026-06-01.status.json', 'snapshot/manifest.json', 'snapshot/versions/abc/daily.json',
                         'snapshot/versions/abc/risk.json', 'model/model.txt', 'refresh_report.json'}))
        self.assertIn('app.sqlite3', report['keep_local']['excluded'])
        self.assertTrue(report['database_backup']['key'].startswith('mulsaemi/backups/sqlite/app-'))
        after = listing()
        self.assertEqual(before, after)   # 로컬 파일은 하나도 지우거나 바꾸지 않았다
        saved = json.loads(Path(report['report_path']).read_text(encoding='utf-8'))
        self.assertEqual(saved['delete_candidates'], report['delete_candidates'])
        self.assertNotIn('secret', json.dumps(saved))
        # 재실행: 이미 올라간 파일은 건너뛰고 결과는 같다
        second = migrate(self.data, self.storage, db_path, backup_db=False)
        self.assertEqual(second['sync']['summary']['uploaded'], 0)
        self.assertEqual(sorted(second['delete_candidates']), sorted(report['delete_candidates']))
        # 원격이 바뀌었으면 삭제 후보에서 빠진다
        self.fake.objects['mulsaemi/model/model.txt']['Body'] = b'tampered'
        verification = verify_data_dir(self.data, self.storage, download=True)
        self.assertEqual([m['relative'] for m in verification['mismatch']], ['model/model.txt'])

    def test_index_tolerates_corruption(self):
        (self.data / layout.INDEX_FILE).write_text('not json', encoding='utf-8')
        self.assertEqual(load_index(self.data), {})
        self.assertEqual(sync_data_dir(self.data, self.storage)['summary']['failed'], 0)


class HooksTest(unittest.TestCase):
    def test_hooks_are_no_ops_when_disabled_and_never_raise_when_enabled(self):
        with patch.object(config, 'OBJECT_STORAGE_ENABLED', False):
            self.assertIsNone(hooks.sync_now(reason='test'))
            self.assertIsNone(hooks.daily_maintenance(force=True))
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            (data / 'collection').mkdir()
            (data / 'collection' / '1.json').write_text('{}')
            fake = FakeS3()
            with patch.object(config, 'OBJECT_STORAGE_ENABLED', True), patch.object(config, 'OBJECT_STORAGE_BUCKET', 'b'), \
                 patch.object(config, 'OBJECT_STORAGE_ACCESS_KEY', 'a'), patch.object(config, 'OBJECT_STORAGE_SECRET_KEY', 's'), \
                 patch.object(config, 'DATA_DIR', data), patch.object(ObjectStorage, 'client', lambda self: fake):
                report = hooks.sync_now(reason='test')
                self.assertEqual(report['summary']['uploaded'], 1)
                self.assertIn('mulsaemi/collection/1.json', fake.objects)
                fake.fail_next = [ClientError('AccessDenied', 403)] * 10
                with patch.object(storage_client.time, 'sleep'):
                    report = hooks.sync_now(reason='test')
                self.assertEqual(report['summary']['failed'], 0)   # 변경 없음: 요청 전에 색인에서 걸러진다
                (data / 'collection' / '2.json').write_text('{}')
                with patch.object(storage_client.time, 'sleep'):
                    report = hooks.sync_now(reason='test')
                self.assertEqual(report['summary']['failed'], 1)   # 실패는 보고서에만 남고 예외는 나오지 않는다


if __name__ == '__main__':
    unittest.main()
