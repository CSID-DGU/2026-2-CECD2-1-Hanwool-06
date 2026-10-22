"""`APP_DATA_DIR` 아래 파일을 자료 유형으로 나누고 Object Storage 키를 정한다.

키는 `<prefix><APP_DATA_DIR 기준 상대 경로>`다. 서버를 바꿔도 같은 상대 경로로 내려받으면
기존 코드가 그대로 읽을 수 있고, 유형별 접두어(collection/, daily/, snapshot/ …)로 버킷 안에서 구분된다.
운영 DB 백업만 `<prefix>backups/sqlite/` 아래에 시각을 붙여 둔다.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator


@dataclass(frozen=True)
class Category:
    name: str
    label: str
    archive: bool      # Object Storage에 복사하는가
    reason: str        # 복사하거나 제외하는 이유(마이그레이션 보고서용)


DATABASE = Category('database', '운영 DB(SQLite)', False, 'SQLite는 로컬에서 직접 읽고 쓴다. backup-db 로 만든 백업 파일만 올린다')
LOCK = Category('lock', '프로세스 잠금 파일', False, '실행 중인 프로세스 사이의 잠금이라 보관 대상이 아니다')
TEMP = Category('temp', '임시 파일', False, '쓰기 도중 생기는 임시 파일')
INDEX = Category('storage_index', 'Object Storage 색인·보고서', False, '이 모듈이 로컬에 남기는 기록')
COLLECTION_STATE = Category('collection_state', '계량기별 수집 상태(collection/*.json)', True, '어느 달을 확인했는지 기록. 잃으면 지난 12개월을 다시 조회한다')
DAILY_WATER = Category('daily_water', '수집한 일일 사용량(daily/water)', True, '아리수에서 받은 원본 관측. 재수집에 시간이 들고 과거 값은 다시 못 받을 수 있다')
DAILY_RIDERSHIP = Category('daily_ridership', '수집한 일별 승하차(daily/ridership)', True, '일별 API는 최근 1주일만 제공한다')
RAW_BILLS = Category('raw_bills', '청구서 수집 원본(raw/billing_i121/i121_bills)', True, '모델 청구 기준값의 입력(bills_long.csv)')
RAW_BILL_CACHE = Category('raw_bill_cache', '아리수 청구 페이지 캐시(raw/billing_i121/i121_cache)', True, '재수집 요청을 줄이는 캐시. 없어도 동작하지만 복원하면 조회가 줄어든다')
RAW_RIDERSHIP_MONTHLY = Category('raw_ridership_monthly', '승하차 월별 원본 파일(raw/ridership_monthly)', True, '열린데이터광장 월별 파일 원본')
DATASET = Category('dataset', '모델 입력 자료(dataset)', True, 'train/valid 경계가 여기에 남아 다음 모델 실행이 같은 분할을 쓴다')
PROCESSED = Category('processed', '가공 자료(processed)', True, '청구 기준값 등 모델 갱신 결과')
MODEL = Category('model', '모델 결과(model)', True, '위험도 판정 결과와 학습한 모델 파일')
SNAPSHOT = Category('snapshot', '공개 스냅샷(snapshot)', True, '화면이 읽는 daily.json·risk.json 과 manifest')
OLD_SNAPSHOT = Category('old_snapshot', '지난 스냅샷 버전(snapshot/versions)', False,
                        '자료가 바뀔 때마다 전체를 다시 묶은 사본이라, 화면이 지금 읽는 버전 하나만 보관한다')
REPORT = Category('report', '실행 보고서', True, 'refresh_report.json 등 실행 기록')
HISTORY = Category('history', '과거 이력 자료(history)', True, '모델 학습 이력·계량기 매핑·초기 자료·옛 수집 폴더. 저장소에 두지 않으므로 버킷이 원본이다')
OTHER = Category('other', '그 밖의 파일', True, '분류되지 않은 파일도 누락 없이 복사한다')

CATEGORIES = [DATABASE, LOCK, TEMP, INDEX, COLLECTION_STATE, DAILY_WATER, DAILY_RIDERSHIP, RAW_BILLS, RAW_BILL_CACHE,
              RAW_RIDERSHIP_MONTHLY, DATASET, PROCESSED, MODEL, SNAPSHOT, OLD_SNAPSHOT, REPORT, HISTORY, OTHER]

INDEX_FILE = '.object-storage-index.json'
LOCK_FILE = '.object-storage.lock'
MIGRATION_REPORT_PREFIX = '.object-storage-migration-'
BACKUP_PREFIX = 'backups/sqlite/'
_TEMP_NAME = re.compile(r'^\.[^/]+\.[A-Za-z0-9_]{6,}$')   # tempfile.mkstemp(prefix=f".{name}.")
_BACKUP_NAME = re.compile(r'^app-(\d{8}T\d{6}Z)\.sqlite3$')


def classify(relative: PurePosixPath | str) -> Category:
    relative = PurePosixPath(relative)
    parts = relative.parts
    name = relative.name
    if not parts:
        return OTHER
    # SQLite 본체와 부속 파일(WAL, 공유 메모리, 롤백 저널, 백업 임시 파일)은 모두 로컬 전용이다.
    if name.endswith(('.sqlite3', '.sqlite3-wal', '.sqlite3-shm', '.sqlite3-journal', '.db', '.db-wal', '.db-shm', '.db-journal')) \
            or name.startswith('.app-backup.'):
        return DATABASE
    if name.endswith('.lock'):
        return LOCK
    if name == INDEX_FILE or name.startswith(MIGRATION_REPORT_PREFIX):
        return INDEX
    if _TEMP_NAME.match(name) or name.endswith(('.tmp', '.temp')):
        return TEMP
    if parts[0] == 'collection':
        return COLLECTION_STATE
    if parts[0] == 'daily' and len(parts) > 1:
        if parts[1] == 'water':
            return DAILY_WATER
        if parts[1] == 'ridership':
            return DAILY_RIDERSHIP
    if parts[0] == 'raw' and len(parts) > 1:
        if parts[1] == 'billing_i121' and len(parts) > 2:
            if parts[2] == 'i121_cache':
                return RAW_BILL_CACHE
            return RAW_BILLS
        if parts[1] == 'ridership_monthly':
            return RAW_RIDERSHIP_MONTHLY
    if parts[0] == 'dataset':
        return DATASET
    if parts[0] == 'processed':
        return PROCESSED
    if parts[0] == 'model':
        return MODEL
    if parts[0] == 'snapshot':
        return SNAPSHOT
    if parts[0] == 'history':
        return HISTORY
    if name == 'refresh_report.json':
        return REPORT
    return OTHER


def normalize_prefix(prefix: str | None) -> str:
    prefix = (prefix or '').strip().strip('/')
    if not prefix:
        return ''
    if '..' in prefix.split('/') or any(ord(ch) < 32 for ch in prefix):
        raise ValueError('Object Storage 접두어가 올바르지 않습니다.')
    return prefix + '/'


def object_key(prefix: str, relative: PurePosixPath | str) -> str:
    relative = PurePosixPath(relative)
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise ValueError('Object key must be a relative path inside the data directory')
    return normalize_prefix(prefix) + relative.as_posix()


def relative_from_key(prefix: str, key: str) -> PurePosixPath:
    prefix = normalize_prefix(prefix)
    if not key.startswith(prefix):
        raise ValueError('Object key is outside the configured prefix')
    relative = PurePosixPath(key[len(prefix):])
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise ValueError('Object key does not map to a safe relative path')
    return relative


def backup_key(prefix: str, moment: datetime | None = None) -> str:
    moment = (moment or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return normalize_prefix(prefix) + BACKUP_PREFIX + f"app-{moment.strftime('%Y%m%dT%H%M%SZ')}.sqlite3"


def backup_moment(key: str) -> str | None:
    match = _BACKUP_NAME.match(PurePosixPath(key).name)
    return match.group(1) if match else None


def current_snapshot(data_dir: Path) -> str | None:
    """화면이 지금 읽는 스냅샷 버전 폴더(`snapshot/manifest.json`의 directory, 예: `versions/<id>`). 모르면 None."""
    try:
        directory = json.loads((Path(data_dir) / 'snapshot' / 'manifest.json').read_text(encoding='utf-8'))['directory']
    except (OSError, ValueError, KeyError, TypeError):
        return None
    directory = directory.strip('/') if isinstance(directory, str) else ''
    parts = PurePosixPath(directory).parts
    return directory if len(parts) == 2 and parts[0] == 'versions' and parts[1] not in ('.', '..') else None


def iter_files(data_dir: Path, current: str | None = None) -> Iterator[tuple[Path, PurePosixPath, Category]]:
    """`data_dir` 아래 모든 일반 파일을 (절대 경로, 상대 경로, 분류)로 돌려준다. 심볼릭 링크는 따라가지 않는다.

    스냅샷 버전 폴더는 `current`(기본: manifest 가 가리키는 버전)만 보관 대상이고 나머지는 지난 버전이다.
    """
    data_dir = Path(data_dir)
    if not data_dir.is_dir():
        return
    current = current or current_snapshot(data_dir)
    for path in sorted(p for p in data_dir.rglob('*') if p.is_file() and not p.is_symlink()):
        if any(part == '__pycache__' for part in path.relative_to(data_dir).parts):
            continue
        relative = PurePosixPath(path.relative_to(data_dir).as_posix())
        category = classify(relative)
        if category is SNAPSHOT and relative.parts[1:2] == ('versions',) and '/'.join(relative.parts[1:3]) != current:
            category = OLD_SNAPSHOT
        yield path, relative, category
