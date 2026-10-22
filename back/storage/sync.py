"""로컬 `APP_DATA_DIR` 와 Object Storage 사이의 복사·백업·복원·마이그레이션.

- `sync_data_dir`: 바뀐 파일만 올린다. 로컬 색인(.object-storage-index.json)과 객체 메타데이터의 sha256 을
  비교하므로 다시 실행해도 같은 파일을 두 번 올리거나 덮어써서 깨뜨리지 않는다.
- `backup_database`: SQLite 백업 API로 일관된 복사본을 만들어 `backups/sqlite/` 에 올린다.
- `restore_data_dir`: 새 서버에서 같은 상대 경로로 내려받는다(기존 파일은 덮어쓰지 않음).
- `migrate`: 최초 이전. 목록 → 분류 → 업로드 → 무결성 검증 → 보고서. 로컬 파일은 지우지 않는다.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from back.locking import lock_file, unlock_file
from .client import ObjectStorage, StorageError, sha256_file
from .layout import (BACKUP_PREFIX, INDEX_FILE, LOCK_FILE, MIGRATION_REPORT_PREFIX, backup_key, backup_moment,
                     current_snapshot, iter_files, object_key, relative_from_key)

logger = logging.getLogger(__name__)
MANIFEST = 'snapshot/manifest.json'


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _atomic_write(path: Path, text: str):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_index(data_dir: Path) -> dict:
    path = Path(data_dir) / INDEX_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_index(data_dir: Path, index: dict):
    _atomic_write(Path(data_dir) / INDEX_FILE, json.dumps(index, ensure_ascii=False, indent=1, sort_keys=True))


class _SyncLock:
    """같은 자료 폴더에 대한 동시 동기화를 막는다. 비차단 모드에서는 다른 동기화가 진행 중이면 None 을 돌려준다."""

    def __init__(self, data_dir: Path, blocking: bool = True):
        self.path = Path(data_dir) / LOCK_FILE
        self.blocking = blocking
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open('a')
        try:
            lock_file(self.handle, blocking=self.blocking)
        except BlockingIOError:
            self.handle.close()
            self.handle = None
            return None
        return self

    def __exit__(self, *exc):
        if self.handle is not None:
            try:
                unlock_file(self.handle)
            finally:
                self.handle.close()


def plan(data_dir: Path, storage: ObjectStorage, index: dict | None = None, current: str | None = None) -> list[dict]:
    """파일마다 할 일을 정한다: upload / unchanged / excluded. 네트워크는 쓰지 않는다."""
    index = load_index(data_dir) if index is None else index
    items = []
    for path, relative, category in iter_files(data_dir, current):
        try:
            info = path.stat()
        except OSError:
            continue
        item = {'relative': relative.as_posix(), 'path': str(path), 'category': category.name, 'label': category.label,
                'size': info.st_size, 'mtime_ns': info.st_mtime_ns}
        if not category.archive:
            item.update(action='excluded', reason=category.reason)
        else:
            item['key'] = object_key(storage.prefix, relative)
            known = index.get(relative.as_posix())
            if known and known.get('mtime_ns') == info.st_mtime_ns and known.get('size') == info.st_size and known.get('sha256'):
                item.update(action='unchanged', sha256=known['sha256'])
            else:
                item.update(action='upload')
        items.append(item)
    return items


def sync_data_dir(data_dir: Path, storage: ObjectStorage, *, dry_run=False, verify_download=False, blocking=True,
                  progress=None) -> dict:
    """바뀐 파일을 올리고 색인을 갱신한다. 실패한 파일은 보고서에 남기고 나머지는 계속한다."""
    data_dir = Path(data_dir)
    report = {'started_at': _now(), 'data_dir': str(data_dir), 'bucket': storage.bucket, 'prefix': storage.prefix,
              'dry_run': dry_run, 'uploaded': [], 'unchanged': [], 'verified_remote': [], 'excluded': [], 'failed': []}
    notify = progress or (lambda message: None)
    with _SyncLock(data_dir, blocking=blocking) as held:
        if held is None:
            report.update(skipped='another sync is running', finished_at=_now())
            return report
        index = load_index(data_dir)
        current = current_snapshot(data_dir)   # 올리기와 지우기가 같은 버전을 기준으로 삼는다
        # manifest 는 맨 끝에, 그 버전을 다 올린 뒤에만 올린다. 버킷의 manifest 가 없는 버전을 가리키지 않게 한다.
        for item in sorted(plan(data_dir, storage, index, current), key=lambda item: item['relative'] == MANIFEST):
            if item['relative'] == MANIFEST and item['action'] == 'upload' and (
                    current_snapshot(data_dir) != current or any(f['relative'].startswith('snapshot/') for f in report['failed'])):
                report['failed'].append({'relative': MANIFEST, 'error': 'SnapshotNotReady'})   # 다음 동기화에서 다시 맞춘다
                continue
            if item['action'] == 'excluded':
                report['excluded'].append({k: item[k] for k in ('relative', 'category', 'reason')})
                continue
            if item['action'] == 'unchanged':
                report['unchanged'].append(item['relative'])
                continue
            path = Path(item['path'])
            try:
                digest = sha256_file(path)
                after = path.stat()
                if (after.st_mtime_ns, after.st_size) != (item['mtime_ns'], item['size']):
                    # 읽는 사이 바뀐 파일은 다음 동기화에 맡긴다. 반쯤 쓰인 내용을 올리지 않는다.
                    report['failed'].append({'relative': item['relative'], 'error': 'ChangedDuringRead'})
                    continue
                if index.get(item['relative'], {}).get('sha256') == digest:
                    remote = {'sha256': digest, 'size': item['size']}   # 내용은 같고 mtime 만 바뀐 파일
                else:
                    remote = storage.head(item['key']) if not dry_run else None
                if remote and remote.get('sha256') == digest and remote.get('size') == item['size']:
                    report['verified_remote'].append(item['relative'])
                elif dry_run:
                    report['uploaded'].append({'relative': item['relative'], 'key': item['key'], 'size': item['size'], 'planned': True})
                    continue
                else:
                    notify(f"올리는 중: {item['relative']}")
                    storage.put_file(path, item['key'], sha256=digest)
                    if verify_download:
                        if storage.get_bytes(item['key']) != path.read_bytes():
                            raise StorageError('Object Storage 내려받기 검증 실패 (내용 불일치)')
                    report['uploaded'].append({'relative': item['relative'], 'key': item['key'], 'size': item['size'], 'sha256': digest})
                index[item['relative']] = {'key': item['key'], 'sha256': digest, 'size': item['size'],
                                           'mtime_ns': item['mtime_ns'], 'category': item['category'], 'synced_at': _now()}
            except (OSError, StorageError) as exc:
                logger.warning('Object storage sync of %s failed (%s)', item['relative'], type(exc).__name__)
                report['failed'].append({'relative': item['relative'], 'error': type(exc).__name__})
        if not dry_run:
            # 스냅샷이 바뀐 동기화에서만 정리한다(바뀐 것이 없으면 요청을 보내지 않는다).
            changed = any(u['relative'].startswith('snapshot/') for u in report['uploaded']) \
                or any(r.startswith('snapshot/') for r in report['verified_remote'])
            report['pruned'] = _prune_old_snapshots(storage, data_dir, current, report['failed']) if changed else []
            save_index(data_dir, index)
    report['finished_at'] = _now()
    report['summary'] = {'uploaded': len(report['uploaded']), 'unchanged': len(report['unchanged']),
                         'verified_remote': len(report['verified_remote']), 'excluded': len(report['excluded']),
                         'failed': len(report['failed'])}
    if not dry_run:
        report['summary']['pruned'] = len(report['pruned'])
    return report


def _prune_old_snapshots(storage: ObjectStorage, data_dir: Path, current: str | None, failed: list[dict]) -> list[str]:
    """버킷의 지난 스냅샷 버전을 지운다.

    지금 버전이 이 서버에 있고, 그 파일마다 버킷 객체의 크기·sha256 이 같다고 확인한 뒤에만 지운다.
    지금 버전을 모르거나 이번에 스냅샷을 다 올리지 못했으면 아무것도 지우지 않는다.
    """
    if not current or any(f['relative'].startswith('snapshot/') for f in failed):
        return []
    version_dir = Path(data_dir) / 'snapshot' / current
    files = sorted(p for p in version_dir.rglob('*') if p.is_file()) if version_dir.is_dir() else []
    if not files:
        return []
    keep = object_key(storage.prefix, f'snapshot/{current}') + '/'
    pruned = []
    try:
        for path in files:
            remote = storage.head(object_key(storage.prefix, path.relative_to(data_dir).as_posix()))
            if not remote or remote.get('size') != path.stat().st_size or remote.get('sha256') != sha256_file(path):
                return []
        for item in storage.list(object_key(storage.prefix, 'snapshot/versions') + '/'):
            if not item['key'].startswith(keep):
                storage.delete(item['key'])
                pruned.append(item['key'])
    except (OSError, StorageError) as exc:
        logger.warning('Old snapshot versions were not pruned (%s)', type(exc).__name__)
    return pruned


def backup_database(db_path: Path, storage: ObjectStorage, *, keep: int = 30, moment: datetime | None = None) -> dict:
    """실행 중에도 안전한 SQLite 백업을 만들어 올린다. keep 개를 넘는 오래된 백업만 정리한다(0이면 정리하지 않음)."""
    db_path = Path(db_path)
    if not db_path.is_file():
        raise StorageError('백업할 운영 DB 파일이 없습니다.')
    key = backup_key(storage.prefix, moment)
    fd, name = tempfile.mkstemp(prefix='.app-backup.', suffix='.sqlite3', dir=db_path.parent)
    os.close(fd)
    try:
        source = sqlite3.connect(db_path, timeout=30)
        try:
            destination = sqlite3.connect(name)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
        try:
            os.chmod(name, 0o600)
        except OSError:
            pass
        result = storage.put_file(Path(name), key, content_type='application/vnd.sqlite3')
    finally:
        if os.path.exists(name):
            os.unlink(name)
    pruned = []
    if keep and keep > 0:
        backups = sorted((b for b in storage.list(storage.prefix + BACKUP_PREFIX) if backup_moment(b['key'])), key=lambda b: b['key'])
        for old in backups[:-keep] if len(backups) > keep else []:
            try:
                storage.delete(old['key'])
                pruned.append(old['key'])
            except StorageError as exc:
                logger.warning('Old database backup was not pruned (%s)', type(exc).__name__)
    return result | {'pruned': pruned, 'backed_up_at': _now()}


def list_backups(storage: ObjectStorage) -> list[dict]:
    return sorted((b for b in storage.list(storage.prefix + BACKUP_PREFIX) if backup_moment(b['key'])), key=lambda b: b['key'])


def restore_data_dir(storage: ObjectStorage, destination: Path, *, overwrite=False, include_db=False, progress=None) -> dict:
    """버킷의 접두어 아래 자료를 같은 상대 경로로 내려받는다. 기존 파일은 overwrite 를 켜지 않으면 건너뛴다."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    notify = progress or (lambda message: None)
    report = {'started_at': _now(), 'destination': str(destination), 'restored': [], 'skipped_existing': [], 'failed': [], 'database': None}
    for item in storage.list(storage.prefix):
        if item['key'].startswith(storage.prefix + BACKUP_PREFIX):
            continue
        try:
            relative = relative_from_key(storage.prefix, item['key'])
        except ValueError:
            report['failed'].append({'key': item['key'], 'error': 'UnsafeKey'})
            continue
        target = destination.joinpath(*relative.parts)
        if target.exists() and not overwrite:
            report['skipped_existing'].append(relative.as_posix())
            continue
        try:
            notify(f'내려받는 중: {relative.as_posix()}')
            head = storage.head(item['key']) or {}
            storage.get_file(item['key'], target, expected_sha256=head.get('sha256'))
            report['restored'].append(relative.as_posix())
        except (OSError, StorageError) as exc:
            report['failed'].append({'key': item['key'], 'error': type(exc).__name__})
    if include_db:
        backups = list_backups(storage)
        target = destination / 'app.sqlite3'
        if not backups:
            report['database'] = {'error': 'NoBackup'}
        elif target.exists() and not overwrite:
            report['database'] = {'skipped_existing': str(target)}
        else:
            latest = backups[-1]
            head = storage.head(latest['key']) or {}
            storage.get_file(latest['key'], target, expected_sha256=head.get('sha256'))
            try:
                target.chmod(0o600)
            except OSError:
                pass
            report['database'] = {'key': latest['key'], 'path': str(target)}
    report['finished_at'] = _now()
    return report


def verify_data_dir(data_dir: Path, storage: ObjectStorage, *, download=False) -> dict:
    """로컬 보관 대상 파일마다 원격 객체의 크기·sha256 을 맞춰 본다. 삭제 후보 판단의 근거다."""
    data_dir = Path(data_dir)
    report = {'verified': [], 'mismatch': [], 'missing_remote': [], 'excluded': []}
    for path, relative, category in iter_files(data_dir):
        if not category.archive:
            report['excluded'].append(relative.as_posix())
            continue
        key = object_key(storage.prefix, relative)
        digest = sha256_file(path)
        remote = storage.head(key)
        if remote is None:
            report['missing_remote'].append(relative.as_posix())
        elif remote.get('sha256') != digest or remote.get('size') != path.stat().st_size:
            report['mismatch'].append({'relative': relative.as_posix(), 'local_sha256': digest, 'remote_sha256': remote.get('sha256')})
        elif download and storage.get_bytes(key) != path.read_bytes():
            report['mismatch'].append({'relative': relative.as_posix(), 'error': 'DownloadMismatch'})
        else:
            report['verified'].append({'relative': relative.as_posix(), 'key': key, 'size': path.stat().st_size, 'sha256': digest, 'category': category.name})
    report['summary'] = {k: len(v) for k, v in report.items()}
    return report


def migrate(data_dir: Path, storage: ObjectStorage, db_path: Path | None = None, *, dry_run=False, verify_download=False,
            backup_db=True, progress=None) -> dict:
    """최초 이전. 재실행해도 이미 올라간 파일은 건너뛴다. 로컬 파일은 절대 지우지 않고 삭제 후보만 적는다."""
    data_dir = Path(data_dir)
    notify = progress or (lambda message: None)
    report = {'started_at': _now(), 'dry_run': dry_run, 'data_dir': str(data_dir), 'bucket': storage.bucket, 'prefix': storage.prefix,
              'inventory': {}, 'database_backup': None}
    for _, relative, category in iter_files(data_dir):
        entry = report['inventory'].setdefault(category.name, {'label': category.label, 'archive': category.archive,
                                                                'reason': category.reason, 'files': 0, 'bytes': 0})
        entry['files'] += 1
        entry['bytes'] += (data_dir / Path(*relative.parts)).stat().st_size
    if not dry_run:
        storage.ping()
    if backup_db and db_path and Path(db_path).is_file():
        if dry_run:
            report['database_backup'] = {'planned': True, 'key_prefix': storage.prefix + BACKUP_PREFIX}
        else:
            notify('운영 DB 백업을 올리는 중')
            report['database_backup'] = backup_database(db_path, storage, keep=0)
    notify('파일을 올리는 중')
    report['sync'] = sync_data_dir(data_dir, storage, dry_run=dry_run, verify_download=verify_download, progress=progress)
    if not dry_run:
        notify('무결성을 검증하는 중')
        verification = verify_data_dir(data_dir, storage, download=verify_download)
        report['verification'] = verification
        # 원격과 내용이 일치하는 파일만 삭제 후보다. 실제 삭제는 별도 승인 뒤 사람이 한다.
        report['delete_candidates'] = [v['relative'] for v in verification['verified']]
        report['keep_local'] = {'excluded': verification['excluded'], 'not_verified': verification['missing_remote']
                                + [m['relative'] for m in verification['mismatch']]}
    report['finished_at'] = _now()
    report_path = data_dir / f"{MIGRATION_REPORT_PREFIX}{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    _atomic_write(report_path, json.dumps(report, ensure_ascii=False, indent=1, default=str))
    report['report_path'] = str(report_path)
    return report
