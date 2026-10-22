"""모델 메타데이터 연결 확인과 프로세스 캐시를 외부 호출 없이 검증한다."""
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx
from openai import APIConnectionError, APITimeoutError, AuthenticationError

from back.api import chat_agent, config


class ChatStatusTest(unittest.TestCase):
    def setUp(self):
        self.patches = [patch.object(config, 'OPENAI_API_KEY', 'test-key'),
                        patch.object(config, 'OPENAI_MODEL', 'test-model'),
                        patch.object(chat_agent, '_status_cache', None), patch.object(chat_agent, 'OpenAI')]
        for item in self.patches:
            item.start()
        self.client = chat_agent.OpenAI.return_value.__enter__.return_value

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()

    def test_active_checks_configured_model_and_closes_without_generating(self):
        self.assertTrue(chat_agent.connection_active())
        chat_agent.OpenAI.assert_called_once_with(api_key='test-key', timeout=4, max_retries=0)
        self.client.models.retrieve.assert_called_once_with('test-model')
        self.client.responses.create.assert_not_called()
        chat_agent.OpenAI.return_value.__exit__.assert_called_once()

    def test_missing_key_is_immediately_inactive_even_during_another_probe(self):
        self.assertTrue(chat_agent.connection_active())
        chat_agent.OpenAI.reset_mock()
        with patch.object(config, 'OPENAI_API_KEY', ''), ThreadPoolExecutor(max_workers=1) as pool:
            with chat_agent._status_lock:
                self.assertFalse(pool.submit(chat_agent.connection_active).result(timeout=1))
        chat_agent.OpenAI.assert_not_called()
        self.assertTrue(chat_agent.connection_active())
        chat_agent.OpenAI.assert_called_once()

    def test_api_authentication_connection_and_timeout_errors_are_private_and_cached(self):
        request = httpx.Request('GET', 'https://api.openai.com/v1/models/test-model')
        errors = [AuthenticationError('private-key', response=httpx.Response(401, request=request), body=None),
                  APIConnectionError(request=request), APITimeoutError(request=request)]
        for error in errors:
            with self.subTest(error=type(error).__name__), patch.object(chat_agent, '_status_cache', None):
                self.client.models.retrieve.reset_mock()
                self.client.models.retrieve.side_effect = error
                with self.assertNoLogs(chat_agent.logger):
                    self.assertFalse(chat_agent.connection_active())
                    self.assertFalse(chat_agent.connection_active())
                self.client.models.retrieve.assert_called_once()
                self.client.responses.create.assert_not_called()

    def test_success_and_failure_expire_after_sixty_seconds(self):
        for active in (True, False):
            with self.subTest(active=active), patch.object(chat_agent, '_status_cache', None), \
                 patch.object(chat_agent.time, 'monotonic', return_value=10) as monotonic:
                self.client.models.retrieve.reset_mock()
                self.client.models.retrieve.side_effect = None if active else RuntimeError('private error')
                self.assertEqual(chat_agent.connection_active(), active)
                monotonic.return_value = 69
                self.assertEqual(chat_agent.connection_active(), active)
                self.client.models.retrieve.assert_called_once()
                monotonic.return_value = 70
                self.assertEqual(chat_agent.connection_active(), active)
                self.assertEqual(self.client.models.retrieve.call_count, 2)

    def test_key_and_model_changes_recheck_before_cache_expiry(self):
        self.assertTrue(chat_agent.connection_active())
        with patch.object(config, 'OPENAI_API_KEY', 'new-key'):
            self.assertTrue(chat_agent.connection_active())
            chat_agent.OpenAI.assert_called_with(api_key='new-key', timeout=4, max_retries=0)
            with patch.object(config, 'OPENAI_MODEL', 'new-model'):
                self.assertTrue(chat_agent.connection_active())
                self.client.models.retrieve.assert_called_with('new-model')
        self.assertEqual(self.client.models.retrieve.call_count, 3)

    def test_configuration_changed_during_probe_cannot_publish_old_result(self):
        self.client.models.retrieve.side_effect = lambda model: setattr(config, 'OPENAI_MODEL', 'new-model')
        self.assertFalse(chat_agent.connection_active())
        self.assertIsNone(chat_agent._status_cache)
        self.client.models.retrieve.side_effect = None
        self.assertTrue(chat_agent.connection_active())
        self.client.models.retrieve.assert_called_with('new-model')

    def test_concurrent_requests_share_one_probe(self):
        ready = threading.Barrier(4)
        probing, release = threading.Event(), threading.Event()

        def retrieve(model):
            probing.set()
            if not release.wait(timeout=2):
                raise TimeoutError('test probe was not released')

        def status():
            ready.wait(timeout=2)
            return chat_agent.connection_active()

        self.client.models.retrieve.side_effect = retrieve
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = [pool.submit(status) for _ in range(4)]
            try:
                self.assertTrue(probing.wait(timeout=2))
            finally:
                release.set()
            self.assertEqual([item.result(timeout=2) for item in pending], [True] * 4)
        self.client.models.retrieve.assert_called_once()


if __name__ == '__main__':
    unittest.main()
