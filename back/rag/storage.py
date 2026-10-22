"""채팅 전용 읽기 경로. 로컬 스냅샷이나 업로드 훅으로 우회하지 않는다."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import OrderedDict

from back.api import config
from back.storage.client import DEFAULT_ENDPOINT, DEFAULT_REGION, ObjectStorage, StorageSettings, _is_missing
from back.storage.layout import normalize_prefix

_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024


class ChatStorageError(RuntimeError):
    pass


class ChatDeadlineError(ChatStorageError):
    pass


def settings_from_env():
    return StorageSettings(
        enabled=True,
        endpoint=os.getenv('CHAT_STORAGE_ENDPOINT', DEFAULT_ENDPOINT).strip(),
        region=os.getenv('CHAT_STORAGE_REGION', DEFAULT_REGION).strip(),
        bucket=os.getenv('CHAT_STORAGE_BUCKET', '').strip(),
        access_key=os.getenv('CHAT_STORAGE_ACCESS_KEY', '') or config.OBJECT_STORAGE_ACCESS_KEY,
        secret_key=os.getenv('CHAT_STORAGE_SECRET_KEY', '') or config.OBJECT_STORAGE_SECRET_KEY,
        prefix=normalize_prefix(os.getenv('CHAT_STORAGE_PREFIX', 'prod')),
        connect_timeout=3, read_timeout=10,
    )


def decode_json(data):
    try:
        return json.loads(data.decode('utf-8-sig'), parse_constant=lambda value: None)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ChatStorageError('invalid_document') from exc


class RemoteStore:
    """주입 가능한 S3 클라이언트, 읽기 API만 사용한다. 생성 시에는 접속하지 않는다."""

    def __init__(self, storage=None, deadline=None):
        self.storage = storage
        self.deadline = deadline
        self._snapshot = None
        self._owns_client = storage is None

    def check_deadline(self):
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise ChatDeadlineError('deadline_exceeded')

    def _storage(self):
        self.check_deadline()
        if self._owns_client:
            settings = settings_from_env()
            if not settings.credentials_ready or not settings.endpoint.startswith('https://'):
                raise ChatStorageError('not_configured')
            # 채팅 요청의 시간 예산을 넘는 SDK 자동 재시도는 사용하지 않는다.
            try:
                import boto3
                from botocore.config import Config
                remaining = (self.deadline or time.monotonic() + 15) - time.monotonic()
                if remaining < .5:
                    raise ChatDeadlineError('deadline_exceeded')
                timeout = min(10, remaining / 2)
                options = dict(signature_version='s3v4', retries={'total_max_attempts': 1},
                               connect_timeout=min(3, timeout), read_timeout=timeout, s3={'addressing_style': 'path'})
                try:
                    sdk_config = Config(**options, request_checksum_calculation='when_required', response_checksum_validation='when_required')
                except TypeError:
                    sdk_config = Config(**options)
                client = boto3.client('s3', endpoint_url=settings.endpoint, region_name=settings.region,
                    aws_access_key_id=settings.access_key, aws_secret_access_key=settings.secret_key,
                    config=sdk_config)
                self.storage = ObjectStorage(settings, client=client)
            except ChatDeadlineError:
                raise
            except Exception as exc:
                raise ChatStorageError('connection_unavailable') from exc
        return self.storage

    def read(self, relative, limit, *, optional=False, sha256=None):
        self.check_deadline()
        if not isinstance(relative, str) or relative.startswith('/') or '\\' in relative or any(
                part in ('', '.', '..') for part in relative.split('/')):
            raise ChatStorageError('invalid_path')
        storage = self._storage()
        try:
            response = storage.client().get_object(Bucket=storage.bucket, Key=storage.prefix + relative)
            body = response['Body']
            try:
                length = response.get('ContentLength')
                length = int(length) if length is not None else None
                if length is not None and length < 0:
                    raise ChatStorageError('invalid_content_length')
                if length is not None and length > limit:
                    raise ChatStorageError('document_too_large')
                chunks, size = [], 0
                # SDK는 본문을 다 읽으면 소켓을 닫으므로 EOF 뒤에는 timeout을 다시 설정하지 않는다.
                while length is None or size < length:
                    self.check_deadline()
                    if self.deadline is not None and hasattr(body, 'set_socket_timeout') and (length is not None or not size):
                        body.set_socket_timeout(max(.01, min(10, self.deadline - time.monotonic())))
                    chunk = body.read(min(1024 * 1024, limit + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > limit:
                        raise ChatStorageError('document_too_large')
                if length is not None and size != length:
                    raise ChatStorageError('incomplete_document')
                data = b''.join(chunks)
            finally:
                body.close()
            self.check_deadline()
            if len(data) > limit:
                raise ChatStorageError('document_too_large')
            expected = sha256 or (response.get('Metadata') or {}).get('sha256')
            if expected and hashlib.sha256(data).hexdigest() != expected:
                raise ChatStorageError('checksum_mismatch')
            return data
        except ChatStorageError:
            raise
        except Exception as exc:
            if optional and _is_missing(exc):
                return None
            raise ChatStorageError('remote_read_failed') from exc
        finally:
            if self._owns_client:
                storage.client().close()

    def snapshot(self):
        if self._snapshot is not None:
            self.check_deadline()
            return self._snapshot
        previous = None
        for attempt in range(2):
            raw = self.read('snapshot/manifest.json', 1024 * 1024)
            manifest = decode_json(raw)
            if not isinstance(manifest, dict):
                raise ChatStorageError('invalid_manifest')
            directory = manifest.get('directory')
            if (manifest.get('published') is not True or not isinstance(directory, str)
                    or not re.fullmatch(r'versions/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', directory)):
                raise ChatStorageError('invalid_manifest')
            if previous == raw:
                raise ChatStorageError('snapshot_unavailable')
            storage = self.storage
            identity = hashlib.sha256((storage.settings.access_key + '\0' + storage.settings.secret_key).encode()).hexdigest()
            key = (storage.settings.endpoint, storage.bucket, storage.prefix, identity, directory, hashlib.sha256(raw).hexdigest())
            with _CACHE_LOCK:
                cached = _CACHE.get(key)
                if cached and time.monotonic() - cached[0] < 60:
                    _CACHE.move_to_end(key)
                    self._snapshot = cached[1]
                    return self._snapshot
            try:
                daily = decode_json(self.read(f'snapshot/{directory}/daily.json', MAX_SNAPSHOT_BYTES))
                risk = decode_json(self.read(f'snapshot/{directory}/risk.json', MAX_SNAPSHOT_BYTES))
                if not isinstance(daily, dict) or not isinstance(risk, dict):
                    raise ChatStorageError('invalid_snapshot')
                self._snapshot = {'daily': daily, 'risk': risk, 'manifest': manifest, 'version': directory}
                with _CACHE_LOCK:
                    _CACHE[key] = (time.monotonic(), self._snapshot)
                    while len(_CACHE) > 2:
                        _CACHE.popitem(last=False)
                return self._snapshot
            except ChatDeadlineError:
                raise
            except ChatStorageError:
                if attempt:
                    raise
                # 새 manifest 게시 직후 이전 버전이 정리된 경우에만 새 버전으로 한 번 재시도한다.
                previous = raw
        raise ChatStorageError('snapshot_unavailable')
