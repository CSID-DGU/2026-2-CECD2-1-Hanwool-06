"""운영 코드가 부르는 보관 훅. Object Storage가 꺼져 있으면 아무 일도 하지 않고, 켜져 있어도 절대 예외를 내지 않는다.

수집·모델 갱신은 로컬 파일을 먼저 완성하고, 그 뒤에 바뀐 파일만 Object Storage로 복사한다.
복사가 실패해도 수집 결과와 화면은 영향을 받지 않으며 다음 훅에서 다시 시도한다.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from .client import ObjectStorage, StorageError, storage_from_config

logger = logging.getLogger(__name__)
_last_maintenance_check = 0.0
MAINTENANCE_INTERVAL = 60


def _storage() -> ObjectStorage | None:
    try:
        storage = storage_from_config()
    except Exception as exc:   # 잘못된 접두어 등 설정 오류
        logger.warning('Object storage settings are invalid (%s)', type(exc).__name__)
        return None
    return storage if storage.enabled else None


def sync_now(data_dir: Path | None = None, *, reason: str = '', blocking: bool = False) -> dict | None:
    """바뀐 파일을 올린다. 다른 동기화가 진행 중이면(비차단) 건너뛴다."""
    storage = _storage()
    if storage is None:
        return None
    from back.api import config
    from .sync import sync_data_dir
    try:
        report = sync_data_dir(Path(data_dir or config.DATA_DIR), storage, blocking=blocking)
        summary = report.get('summary')
        if summary and (summary['uploaded'] or summary['failed']):
            logger.info('Object storage sync (%s): uploaded %s, failed %s', reason or 'manual', summary['uploaded'], summary['failed'])
        return report
    except Exception as exc:
        logger.warning('Object storage sync (%s) failed (%s)', reason or 'manual', type(exc).__name__)
        return None


def after_collection_job(job_id=None) -> None:
    sync_now(reason=f'collection job {job_id}')


def after_refresh(data_dir: Path) -> None:
    sync_now(data_dir, reason='refresh', blocking=True)


def daily_maintenance(force: bool = False) -> dict | None:
    """자동 수집이 그날 마무리된 뒤 하루 한 번 운영 DB 백업을 올리고 자료를 동기화한다."""
    global _last_maintenance_check
    now = time.monotonic()
    if not force and now - _last_maintenance_check < MAINTENANCE_INTERVAL:
        return None
    _last_maintenance_check = now
    storage = _storage()
    if storage is None:
        return None
    from back.api import config, db
    from .sync import backup_database
    try:
        with db.connect() as c:
            settled = c.execute("SELECT value FROM settings WHERE key='collection_auto_settled'").fetchone()
            done = c.execute("SELECT value FROM settings WHERE key='storage_db_backup_day'").fetchone()
        if not settled or (done and done[0] == settled[0] and not force):
            return None
        day = settled[0]
        result = backup_database(config.APP_DB_PATH, storage, keep=config.OBJECT_STORAGE_DB_BACKUP_KEEP)
        with db.connect() as c:
            c.execute("INSERT INTO settings VALUES('storage_db_backup_day',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (day,))
            db.audit(c, None, 'storage_db_backup', day, {'key': result['key'], 'size': result['size'], 'pruned': len(result['pruned'])})
        logger.info('Database backup uploaded to object storage (%s bytes)', result['size'])
        sync_now(reason='daily maintenance')
        return result
    except (StorageError, Exception) as exc:
        logger.warning('Daily object storage maintenance failed (%s)', type(exc).__name__)
        try:
            with db.connect() as c:
                db.audit(c, None, 'storage_db_backup_failed', '', {'error': type(exc).__name__})
        except Exception:
            pass
        return None
