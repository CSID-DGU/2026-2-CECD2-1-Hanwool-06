"""채팅은 로컬 대체 없이 원격 게시 버전만 읽는다."""
import hashlib
import http.client
import io
import json
import time
import unittest
from dataclasses import replace
from unittest.mock import patch

from back.rag import storage as module
from back.rag.storage import ChatDeadlineError, ChatStorageError, RemoteStore
from back.storage.client import ObjectStorage, StorageSettings


class Missing(Exception):
    response = {'Error': {'Code': 'NoSuchKey'}, 'ResponseMetadata': {'HTTPStatusCode': 404}}


class FakeS3:
    def __init__(self, objects):
        self.objects, self.calls = objects, []
        self.metadata = {}

    def get_object(self, Bucket, Key):
        self.calls.append(Key)
        if Key not in self.objects:
            raise Missing()
        value = self.objects[Key]
        if callable(value):
            value = value()
        if isinstance(value, Exception):
            raise value
        return {'Body': io.BytesIO(value), 'ContentLength': len(value), 'Metadata': self.metadata.get(Key, {})}


class MemorySocket:
    """실제 HTTPResponse의 EOF 소켓 종료를 네트워크 없이 재현한다."""

    def __init__(self, payload):
        self.stream = io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: ' + str(len(payload)).encode() + b'\r\n\r\n' + payload)
        self.stream.raw = self.stream
        self.stream._sock = self

    def makefile(self, *args):
        return self.stream

    def settimeout(self, value):
        pass


class HttpBody:
    def __init__(self, payload):
        self.response = http.client.HTTPResponse(MemorySocket(payload))
        self.response.begin()

    def set_socket_timeout(self, value):
        # botocore StreamingBody와 같은 접근: EOF 뒤 fp가 None이면 실패해야 한다.
        self.response.fp.raw._sock.settimeout(value)

    def read(self, amount):
        return self.response.read(amount)

    def close(self):
        self.response.close()


def encoded(value):
    return json.dumps(value, ensure_ascii=False).encode()


def make_store(daily=None, risk=None, **kwargs):
    manifest = {'published': True, 'directory': 'versions/test', 'generated_at': '2026-10-20T23:29:55+00:00'}
    fake = FakeS3({'prod/snapshot/manifest.json': encoded(manifest),
                   'prod/snapshot/versions/test/daily.json': encoded(daily or {}),
                   'prod/snapshot/versions/test/risk.json': encoded(risk or {})})
    settings = StorageSettings(True, 'https://kr.object.ncloudstorage.com', 'kr-standard', 'fixture-bucket',
                               'fixture-access', 'fixture-secret', 'prod/')
    return RemoteStore(ObjectStorage(settings, client=fake), **kwargs), fake


class ChatStorageTest(unittest.TestCase):
    def setUp(self):
        module._CACHE.clear()

    def test_lazy_remote_reads_consistent_version_and_cache(self):
        store, fake = make_store({'remote': {'usage': [{'date': '2026-10-20', 'value': 71}], 'ridership': []}}, {})
        self.assertEqual(fake.calls, [])
        self.assertEqual(store.snapshot()['daily']['remote']['usage'][0]['value'], 71)
        self.assertEqual(len(fake.calls), 3)
        same = RemoteStore(store.storage)
        self.assertEqual(same.snapshot(), store.snapshot())
        self.assertEqual(fake.calls[-1], 'prod/snapshot/manifest.json')
        self.assertEqual(len(fake.calls), 4)
        fake.objects['prod/snapshot/manifest.json'] = Missing()
        with self.assertRaises(ChatStorageError):
            RemoteStore(store.storage).snapshot()  # 캐시가 있어도 현재 게시 상태 확인 실패를 숨기지 않는다.

    def test_safe_manifest_published_flag_and_path(self):
        for directory in ('../private', 'versions/../x', 'versions/a/b', '/versions/a', 'versions/a\\b'):
            store, fake = make_store()
            fake.objects['prod/snapshot/manifest.json'] = encoded({'published': True, 'directory': directory})
            with self.assertRaises(ChatStorageError):
                store.snapshot()
            self.assertEqual(len(fake.calls), 1)
        store, fake = make_store()
        fake.objects['prod/snapshot/manifest.json'] = encoded({'published': False, 'directory': 'versions/test'})
        with self.assertRaises(ChatStorageError):
            store.snapshot()
        for invalid in ([], None, 'versions/test'):
            store, fake = make_store()
            fake.objects['prod/snapshot/manifest.json'] = encoded(invalid)
            with self.assertRaises(ChatStorageError):
                store.snapshot()

    def test_retry_only_after_changed_manifest_for_pruned_version(self):
        store, fake = make_store()
        v2 = encoded({'published': True, 'directory': 'versions/new'})
        def prune():
            fake.objects['prod/snapshot/manifest.json'] = v2
            raise Missing()
        fake.objects['prod/snapshot/versions/test/risk.json'] = prune
        fake.objects['prod/snapshot/versions/new/daily.json'] = encoded({'new': {}})
        fake.objects['prod/snapshot/versions/new/risk.json'] = encoded({'new': []})
        self.assertEqual(store.snapshot()['version'], 'versions/new')
        self.assertEqual(set(store.snapshot()['daily']), {'new'})

    def test_checksums_limits_deadline_and_optional_missing(self):
        store, fake = make_store()
        fake.objects['prod/rag/test.txt'] = b'12345'
        with self.assertRaises(ChatStorageError):
            store.read('rag/test.txt', 4)
        with self.assertRaises(ChatStorageError):
            store.read('rag/test.txt', 8, sha256='bad')
        self.assertEqual(store.read('rag/test.txt', 8, sha256=hashlib.sha256(b'12345').hexdigest()), b'12345')
        self.assertIsNone(store.read('rag/absent.txt', 8, optional=True))
        store.deadline = time.monotonic() - 1
        with self.assertRaises(ChatDeadlineError):
            store.snapshot()

    def test_cache_is_separated_by_credentials(self):
        first, _ = make_store({'first': {}})
        first.snapshot()
        second, fake = make_store({'second': {}})
        second.storage.settings = replace(second.storage.settings, access_key='other-access')
        self.assertEqual(set(second.snapshot()['daily']), {'second'})
        self.assertEqual(len(fake.calls), 3)

    def test_dedicated_configuration_does_not_enable_uploads(self):
        with patch.dict('os.environ', {'CHAT_STORAGE_BUCKET': 'chat-bucket', 'CHAT_STORAGE_PREFIX': 'prod'}, clear=True), \
                patch.object(module.config, 'OBJECT_STORAGE_ACCESS_KEY', 'ncp-access'), \
                patch.object(module.config, 'OBJECT_STORAGE_SECRET_KEY', 'ncp-secret'), \
                patch.object(module.config, 'OBJECT_STORAGE_ENABLED', False):
            settings = module.settings_from_env()
            self.assertEqual((settings.bucket, settings.prefix, settings.access_key), ('chat-bucket', 'prod/', 'ncp-access'))
            self.assertFalse(module.config.OBJECT_STORAGE_ENABLED)

    def test_http_body_closed_at_eof_is_success_with_deadline(self):
        for size in (0, 47, 1024 * 1024, 1024 * 1024 + 7):
            for declared in (True, False):
                with self.subTest(size=size, declared=declared):
                    store, fake = make_store(deadline=time.monotonic() + 30)
                    payload = b'x' * size
                    body = HttpBody(payload)
                    response = {'Body': body}
                    if declared:
                        response['ContentLength'] = size
                    with patch.object(fake, 'get_object', return_value=response):
                        self.assertEqual(store.read('snapshot/manifest.json', size + 1), payload)
                    self.assertIsNone(body.response.fp)

    def test_incomplete_body_is_not_accepted_as_success(self):
        store, fake = make_store(deadline=time.monotonic() + 30)
        with patch.object(fake, 'get_object', return_value={'Body': io.BytesIO(b'{}'), 'ContentLength': 3}):
            with self.assertRaisesRegex(ChatStorageError, 'incomplete_document'):
                store.read('snapshot/manifest.json', 100)
