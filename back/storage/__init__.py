"""NCP Object Storage(S3 호환) 보관 모듈.

로컬 `APP_DATA_DIR`는 그대로 작업 사본으로 쓰고, 장기 보관이 필요한 파일만 Object Storage에
같은 상대 경로로 복사한다. 운영 DB(SQLite)는 직접 올리지 않고 백업 파일만 올린다.
"""
from .client import ObjectStorage, StorageDisabled, StorageError, storage_from_config  # noqa: F401
from .layout import classify, object_key  # noqa: F401
