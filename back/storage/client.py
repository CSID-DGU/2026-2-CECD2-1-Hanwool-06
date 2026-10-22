"""S3 호환 API로 NCP Object Storage에 올리고 내려받는 얇은 클라이언트.

접속 정보는 환경변수(`back.api.config`)에서만 읽고 어디에도 기록하지 않는다. boto3 는 실제로
연결할 때만 불러오므로 Object Storage를 끈 서버에는 설치되어 있지 않아도 된다.
"""
from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .layout import normalize_prefix

logger = logging.getLogger(__name__)

DEFAULT_ENDPOINT = 'https://kr.object.ncloudstorage.com'
DEFAULT_REGION = 'kr-standard'
SHA256_METADATA = 'sha256'
_RETRY_DELAYS = (1, 2, 4)


class StorageError(RuntimeError):
    """Object Storage 요청 실패. 메시지에는 오류 종류만 담고 접속 정보나 응답 본문은 넣지 않는다."""


class StorageDisabled(StorageError):
    """OBJECT_STORAGE_ENABLED 가 꺼져 있다."""


@dataclass(frozen=True)
class StorageSettings:
    enabled: bool
    endpoint: str
    region: str
    bucket: str
    access_key: str
    secret_key: str
    prefix: str
    addressing: str = 'path'
    connect_timeout: int = 5
    read_timeout: int = 60

    @property
    def credentials_ready(self) -> bool:
        return bool(self.bucket and self.access_key and self.secret_key)

    def public(self) -> dict:
        """화면·로그에 보여도 되는 값만. 키는 설정 여부만 알린다."""
        return {'enabled': self.enabled, 'endpoint': self.endpoint, 'region': self.region, 'bucket': self.bucket,
                'prefix': self.prefix, 'credentials_configured': bool(self.access_key and self.secret_key)}


def settings_from_config(config=None) -> StorageSettings:
    if config is None:
        from back.api import config as app_config
        config = app_config
    return StorageSettings(
        enabled=bool(getattr(config, 'OBJECT_STORAGE_ENABLED', False)),
        endpoint=(getattr(config, 'OBJECT_STORAGE_ENDPOINT', '') or DEFAULT_ENDPOINT).strip(),
        region=(getattr(config, 'OBJECT_STORAGE_REGION', '') or DEFAULT_REGION).strip(),
        bucket=(getattr(config, 'OBJECT_STORAGE_BUCKET', '') or '').strip(),
        access_key=getattr(config, 'OBJECT_STORAGE_ACCESS_KEY', '') or '',
        secret_key=getattr(config, 'OBJECT_STORAGE_SECRET_KEY', '') or '',
        prefix=normalize_prefix(getattr(config, 'OBJECT_STORAGE_PREFIX', '')),
        addressing=(getattr(config, 'OBJECT_STORAGE_ADDRESSING', '') or 'path').strip() or 'path',
    )


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(chunk), b''):
            digest.update(block)
    return digest.hexdigest()


def _error_code(exc) -> str:
    response = getattr(exc, 'response', None) or {}
    error = response.get('Error', {}) if isinstance(response, dict) else {}
    return str(error.get('Code') or type(exc).__name__)


def _is_missing(exc) -> bool:
    code = _error_code(exc)
    status = ((getattr(exc, 'response', None) or {}).get('ResponseMetadata') or {}).get('HTTPStatusCode')
    return code in ('404', 'NoSuchKey', 'NotFound') or status == 404


def _is_transient(exc) -> bool:
    name = type(exc).__name__
    if name in ('EndpointConnectionError', 'ConnectTimeoutError', 'ReadTimeoutError', 'ConnectionClosedError',
                'ResponseStreamingError', 'IncompleteReadError'):
        return True
    code = _error_code(exc)
    status = ((getattr(exc, 'response', None) or {}).get('ResponseMetadata') or {}).get('HTTPStatusCode')
    return code in ('SlowDown', 'RequestTimeout', 'InternalError', 'ServiceUnavailable', '503', '500') or (status or 0) >= 500


class ObjectStorage:
    """버킷 하나와 접두어 하나를 다루는 클라이언트. `client` 를 넘기면 테스트용 가짜 클라이언트를 쓸 수 있다."""

    def __init__(self, settings: StorageSettings | None = None, client=None):
        self.settings = settings or settings_from_config()
        self._client = client

    @property
    def enabled(self) -> bool:
        return self.settings.enabled

    @property
    def prefix(self) -> str:
        return self.settings.prefix

    @property
    def bucket(self) -> str:
        return self.settings.bucket

    def status(self) -> dict:
        return self.settings.public()

    # ── 연결 ────────────────────────────────────────────────────────
    def client(self):
        if self._client is None:
            if not self.settings.enabled:
                raise StorageDisabled('Object Storage 연동이 꺼져 있습니다(OBJECT_STORAGE_ENABLED).')
            if not self.settings.credentials_ready:
                raise StorageError('Object Storage 버킷·Access Key·Secret Key 환경변수가 필요합니다.')
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:   # pragma: no cover - 설치 환경에 따라 다름
                raise StorageError('boto3 가 설치되어 있지 않습니다. requirements 를 다시 설치하세요.') from exc
            options = dict(signature_version='s3v4', retries={'max_attempts': 4, 'mode': 'standard'},
                           connect_timeout=self.settings.connect_timeout, read_timeout=self.settings.read_timeout,
                           s3={'addressing_style': self.settings.addressing})
            try:
                # boto3 1.36+ sends CRC32 checksum trailers (aws-chunked) on every upload by default; NCP Object Storage
                # rejects those requests with AccessDenied. Only compute checksums when the operation requires one.
                config = Config(**options, request_checksum_calculation='when_required', response_checksum_validation='when_required')
            except TypeError:   # older botocore without these options never sent the trailers
                config = Config(**options)
            self._client = boto3.client(
                's3', endpoint_url=self.settings.endpoint, region_name=self.settings.region,
                aws_access_key_id=self.settings.access_key, aws_secret_access_key=self.settings.secret_key, config=config)
        return self._client

    def _call(self, description, operation, *args, **kwargs):
        """일시 오류는 몇 번 더 시도하고, 그 밖의 오류는 종류만 남긴 StorageError 로 바꾼다."""
        last = None
        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            try:
                return operation(*args, **kwargs)
            except StorageError:
                raise
            except Exception as exc:   # botocore 예외 계층은 설치 여부에 따라 달라 이름으로 가른다
                last = exc
                if delay is None or not _is_transient(exc):
                    break
                logger.warning('Object storage %s failed (%s); retrying in %ss', description, _error_code(exc), delay)
                time.sleep(delay)
        raise StorageError(f'Object Storage {description} 실패 ({_error_code(last)})') from None

    def ping(self) -> bool:
        self._call('bucket check', self.client().head_bucket, Bucket=self.bucket)
        return True

    # ── 객체 ────────────────────────────────────────────────────────
    def head(self, key: str) -> dict | None:
        """{'size','sha256','etag','last_modified'} 또는 없으면 None."""
        try:
            response = self.client().head_object(Bucket=self.bucket, Key=key)
        except StorageError:
            raise
        except Exception as exc:
            if _is_missing(exc):
                return None
            if _is_transient(exc):
                response = self._call('head', self.client().head_object, Bucket=self.bucket, Key=key)
            else:
                raise StorageError(f'Object Storage head 실패 ({_error_code(exc)})') from None
        metadata = {k.lower(): v for k, v in (response.get('Metadata') or {}).items()}
        return {'key': key, 'size': int(response.get('ContentLength', 0)), 'sha256': metadata.get(SHA256_METADATA),
                'etag': str(response.get('ETag', '')).strip('"'), 'last_modified': response.get('LastModified')}

    def exists(self, key: str) -> bool:
        return self.head(key) is not None

    def put_bytes(self, key: str, data: bytes, *, content_type: str | None = None, sha256: str | None = None) -> dict:
        sha256 = sha256 or hashlib.sha256(data).hexdigest()
        extra = {'Metadata': {SHA256_METADATA: sha256}}
        if content_type:
            extra['ContentType'] = content_type
        self._call('upload', self.client().put_object, Bucket=self.bucket, Key=key, Body=data, **extra)
        return {'key': key, 'size': len(data), 'sha256': sha256}

    def put_file(self, path: Path, key: str, *, sha256: str | None = None, content_type: str | None = None) -> dict:
        """파일을 올리고 올라간 크기·해시를 확인한다. 큰 파일은 boto3 가 멀티파트로 나눠 올린다."""
        path = Path(path)
        sha256 = sha256 or sha256_file(path)
        size = path.stat().st_size
        extra = {'Metadata': {SHA256_METADATA: sha256}}
        if content_type:
            extra['ContentType'] = content_type

        def upload():
            with path.open('rb') as stream:
                self.client().upload_fileobj(stream, self.bucket, key, ExtraArgs=extra)
        self._call('upload', upload)
        stored = self.head(key)
        if not stored or stored['size'] != size or stored['sha256'] != sha256:
            raise StorageError('Object Storage 업로드 확인 실패 (크기 또는 해시 불일치)')
        return {'key': key, 'size': size, 'sha256': sha256}

    def get_bytes(self, key: str) -> bytes:
        def fetch():
            response = self.client().get_object(Bucket=self.bucket, Key=key)
            return response['Body'].read()
        return self._call('download', fetch)

    def get_file(self, key: str, path: Path, *, expected_sha256: str | None = None) -> dict:
        """임시 파일에 받아 해시를 확인한 뒤 제자리에 놓는다. 깨진 파일이 기존 파일을 덮어쓰지 않는다."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                def download():
                    stream.seek(0)
                    stream.truncate()
                    self.client().download_fileobj(self.bucket, key, stream)
                self._call('download', download)
                stream.flush()
                os.fsync(stream.fileno())
            digest = sha256_file(Path(name))
            if expected_sha256 and digest != expected_sha256:
                raise StorageError('Object Storage 내려받기 확인 실패 (해시 불일치)')
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return {'key': key, 'path': str(path), 'sha256': digest, 'size': path.stat().st_size}

    def list(self, prefix: str = '') -> list[dict]:
        """접두어 아래 모든 객체 {'key','size','last_modified'} (페이지 처리 포함)."""
        items = []
        token = None
        while True:
            kwargs = {'Bucket': self.bucket, 'Prefix': prefix, 'MaxKeys': 1000}
            if token:
                kwargs['ContinuationToken'] = token
            response = self._call('list', self.client().list_objects_v2, **kwargs)
            for item in response.get('Contents', []) or []:
                items.append({'key': item['Key'], 'size': int(item.get('Size', 0)), 'last_modified': item.get('LastModified')})
            if not response.get('IsTruncated'):
                return items
            token = response.get('NextContinuationToken')
            if not token:
                return items

    def delete(self, key: str) -> None:
        self._call('delete', self.client().delete_object, Bucket=self.bucket, Key=key)


def storage_from_config(config=None, client=None) -> ObjectStorage:
    return ObjectStorage(settings_from_config(config), client=client)
