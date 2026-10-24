"""대화의 인증·소유권·권한 변경·동시 요청을 외부 호출 없이 검증한다."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from back.api import auth, chat_agent, config, db, main


class ChatApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patches = [patch.object(config, 'APP_DB_PATH', Path(self.temp.name) / 'chat.sqlite3'),
                        patch.object(config, 'OPENAI_API_KEY', 'mock'), patch.object(config, 'COOKIE_SECURE', False),
                        patch.object(chat_agent, 'OpenAI')]
        for item in self.patches:
            item.start()
        db.init_db()
        with db.connect() as c:
            for office in ('east', 'west'):
                c.execute('INSERT INTO offices VALUES(?,?)', (office, office))
            c.execute("INSERT INTO stations VALUES('station','동부역',NULL,NULL)")
            c.execute('''INSERT INTO meters(id,provider,customer_number,station_id,office_id,display_name,created_at,updated_at)
                         VALUES('private-meter','arisu','private-customer','station','east','동부역',?,?)''', (db.now(), db.now()))
            for user_id in (1, 2):
                c.execute('''INSERT INTO users(id,email,name,password_hash,role,created_at)
                             VALUES(?,?,?,'unused','office_admin',?)''', (user_id, f'{user_id}@example.com', '담당자', db.now()))
                c.execute('INSERT INTO office_members VALUES(?,?)', (user_id, 'east'))
                c.execute('INSERT INTO sessions VALUES(?,?,?,?)', (auth.token_hash(f'session-{user_id}'), user_id, f'csrf-{user_id}', time.time() + 3600))
        self.client = TestClient(main.app, base_url='http://localhost')
        self.client.cookies.set(auth.COOKIE, 'session-1')
        self.client.headers['X-CSRF-Token'] = 'csrf-1'
        self.run_patch = patch.object(chat_agent, '_run', side_effect=self.answer)
        self.runner = self.run_patch.start()

    def tearDown(self):
        self.run_patch.stop()
        self.client.close()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    @staticmethod
    def answer(client, message, conversation, deadline, progress=None):
        if progress:
            progress('classify')
            progress('data')
        return ({'answer': '동부역 기준 자료입니다.' + ' 자세한 내용은 출처를 확인하세요.' * 60, 'sources': [], 'domain': 'IN_SCOPE',
                 'intent': 'STATION_STATUS', 'generated_by': 'none'},
                {'station': '동부역', 'date': '2026-08-31', 'meters': {'meter_a': 'private-meter'}})

    def post(self, message='동부역 현황', conversation_id=None):
        return self.client.post('/api/chat', json={'message': message, 'conversation_id': conversation_id})

    @staticmethod
    def events(response):
        return [json.loads(block[len('data: '):]) for block in response.text.split('\n\n') if block.startswith('data: ')]

    def test_stream_sends_fixed_stage_codes_then_the_checked_answer_unbuffered(self):
        response = self.client.post('/api/chat/stream', json={'message': '동부역 현황'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers['content-type'].startswith('text/event-stream'))
        self.assertEqual(response.headers['x-accel-buffering'], 'no')   # nginx가 모았다 보내지 않는다
        self.assertNotIn('content-encoding', response.headers)          # gzip도 모았다 보내지 않는다(답이 1KB를 넘어도)
        events = self.events(response)
        self.assertEqual([event['stage'] for event in events[:-1]], ['classify', 'data'])
        self.assertEqual((events[-1]['type'], len(events[-1]['conversation_id'])), ('result', 32))
        self.assertTrue(events[-1]['answer'].startswith('동부역 기준 자료입니다.'))
        self.assertNotIn('private-meter', response.text)
        # 대화 소유권·한도 같은 오류는 스트림 안에서 상태와 함께 보낸다
        foreign = self.events(self.client.post('/api/chat/stream', json={'message': '동부역 현황', 'conversation_id': 'f' * 32}))
        self.assertEqual([(event['type'], event['status']) for event in foreign], [('error', 404)])
        # 입력·인증 오류는 스트림을 열기 전에 평소처럼 답한다
        self.assertEqual(self.client.post('/api/chat/stream', json={'message': '   '}).status_code, 422)
        self.client.headers.pop('X-CSRF-Token')
        self.assertEqual(self.client.post('/api/chat/stream', json={'message': '동부역 현황'}).status_code, 403)

    def test_status_requires_login_and_a_usable_account_before_openai(self):
        self.client.cookies.clear()
        self.assertEqual(self.client.get('/api/chat/status').status_code, 401)
        self.client.cookies.set(auth.COOKIE, 'session-1')
        with db.connect() as c:
            c.execute('UPDATE users SET must_change_password=1 WHERE id=1')
        self.assertEqual(self.client.get('/api/chat/status').status_code, 403)
        chat_agent.OpenAI.assert_not_called()

    def test_status_returns_only_active_shares_cache_and_preserves_public_health(self):
        with patch.object(chat_agent, '_status_cache', None):
            self.client.headers.pop('X-CSRF-Token')
            active = self.client.get('/api/chat/status')
            self.assertEqual(active.status_code, 200)
            self.assertEqual(active.json(), {'active': True})
            self.assertEqual(active.headers['cache-control'], 'no-store')
            self.client.cookies.set(auth.COOKIE, 'session-2')
            self.assertEqual(self.client.get('/api/chat/status').json(), {'active': True})
            chat_agent.OpenAI.assert_called_once()
            with patch.object(config, 'OPENAI_API_KEY', ''):
                self.assertEqual(self.client.get('/api/chat/status').json(), {'active': False})
            chat_agent.OpenAI.return_value.__enter__.return_value.models.retrieve.side_effect = RuntimeError('private-key')
            inactive = self.client.get('/api/chat/status')
            self.assertEqual(inactive.status_code, 200)
            self.assertEqual(inactive.json(), {'active': False})
            self.assertNotIn('private-key', inactive.text)
            self.client.cookies.clear()
            self.assertEqual(self.client.get('/api/health').json(), {'ok': True, 'setup_required': False})
            self.runner.assert_not_called()

    def test_input_authentication_csrf_and_foreign_conversation_fail_before_openai(self):
        for body in ({'message': ''}, {'message': '   '}, {'message': 'a' * 2001},
                     {'message': 'x\x00'}, {'message': 'x', 'history': []},
                     {'message': 'x', 'conversation_id': 'not-an-id'}):
            self.assertEqual(self.client.post('/api/chat', json=body).status_code, 422)
        self.assertEqual(self.post(conversation_id='f' * 32).status_code, 404)
        chat_agent.OpenAI.assert_not_called()
        self.client.headers.pop('X-CSRF-Token')
        self.assertEqual(self.post().status_code, 403)
        self.client.cookies.clear()
        self.assertEqual(self.post().status_code, 401)

    def test_owner_context_persists_and_foreign_id_is_hidden(self):
        first = self.post().json()
        self.assertEqual(len(first['conversation_id']), 32)
        second = self.post('전날은?', first['conversation_id'])
        self.assertEqual(second.status_code, 200, second.text)
        previous = self.runner.call_args.args[2]
        self.assertEqual(previous['context']['date'], '2026-08-31')
        self.assertEqual(previous['messages'][0]['content'], '동부역 현황')
        with db.connect() as c:
            audit = json.loads(c.execute("SELECT details FROM audit_log WHERE action='chat_complete' LIMIT 1").fetchone()[0])
            self.assertNotIn('answer', audit)
            self.assertNotIn('동부역', json.dumps(audit, ensure_ascii=False))
        self.client.cookies.set(auth.COOKIE, 'session-2')
        self.client.headers['X-CSRF-Token'] = 'csrf-2'
        count = chat_agent.OpenAI.call_count
        missing, foreign = self.post(conversation_id='a' * 32), self.post(conversation_id=first['conversation_id'])
        self.assertEqual((missing.status_code, missing.json()), (foreign.status_code, foreign.json()))
        self.assertEqual(count, chat_agent.OpenAI.call_count)

    def test_reassigned_deactivated_deleted_meter_or_scope_clears_history(self):
        for change in ("UPDATE meters SET office_id='west'", 'UPDATE meters SET active=0',
                       "UPDATE meters SET deleted_at='2026-09-01'", "UPDATE office_members SET office_id='west' WHERE user_id=1",
                       "UPDATE meters SET provider='other'", "UPDATE meters SET customer_number='replacement-customer'"):
            with self.subTest(change=change):
                with db.connect() as c:
                    c.execute("UPDATE meters SET office_id='east',active=1,deleted_at=NULL,provider='arisu',customer_number='private-customer'")
                    c.execute("UPDATE office_members SET office_id='east' WHERE user_id=1")
                first = self.post().json()
                with db.connect() as c:
                    c.execute(change)
                second = self.post('왜 경고야?', first['conversation_id'])
                self.assertEqual(second.status_code, 200, second.text)
                previous = self.runner.call_args.args[2]
                self.assertEqual(previous['context'], {})
                self.assertEqual(previous['messages'], [])

    def test_changed_scope_during_generation_cannot_commit_answer(self):
        def changed(*args):
            result = self.answer(*args)
            with db.connect() as c:
                c.execute("UPDATE meters SET office_id='west'")
            return result
        self.runner.side_effect = changed
        result = self.post()
        self.assertEqual(result.status_code, 409, result.text)
        with db.connect() as c:
            row = c.execute('SELECT * FROM chat_conversations').fetchone()
            self.assertEqual(json.loads(row['messages']), [])
            self.assertEqual(row['lease_until'], 0)

    def test_concurrent_turn_is_rejected_and_expired_lease_recovers(self):
        first = self.post().json()
        conversation = chat_agent._acquire(1, first['conversation_id'])
        self.assertEqual(self.post(conversation_id=first['conversation_id']).status_code, 409)
        with db.connect() as c:
            c.execute('UPDATE chat_conversations SET lease_until=?', (time.time() - 1,))
        self.assertEqual(self.post('전날은?', first['conversation_id']).status_code, 200)
        chat_agent._release(conversation)
        with db.connect() as c:
            self.assertEqual(len(json.loads(c.execute('SELECT messages FROM chat_conversations').fetchone()[0])), 4)

    def test_rate_limit_and_bounded_history(self):
        conversation_id = None
        for _ in range(20):
            result = self.post('현황 ' * 500, conversation_id)
            self.assertEqual(result.status_code, 200, result.text)
            conversation_id = result.json()['conversation_id']
        self.assertEqual(self.post(conversation_id=conversation_id).status_code, 429)
        with db.connect() as c:
            messages = json.loads(c.execute('SELECT messages FROM chat_conversations').fetchone()[0])
            self.assertLessEqual(len(messages), 8)
            self.assertLessEqual(sum(len(item['content']) for item in messages), chat_agent.MAX_HISTORY_CHARS)

    def test_api_failure_releases_lease_without_secret_details(self):
        self.runner.side_effect = RuntimeError('private-api-key')
        result = self.post()
        self.assertEqual(result.status_code, 502)
        self.assertNotIn('private-api-key', result.text)
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT lease_until FROM chat_conversations').fetchone()[0], 0)

    def test_api_real_tool_loop_uses_only_remote_facts_and_persists_followup_date(self):
        self.run_patch.stop()
        with db.connect() as c:
            c.execute('UPDATE meters SET daily_enabled=1')
        snapshot = {'version': 'remote-version', 'manifest': {'generated_at': '2026-09-01T00:00:00Z'},
                    'daily': {'private-customer': {'usage': [{'date': '2026-08-31', 'value': 7}], 'ridership': []}},
                    'risk': {'private-customer': [{'date': '2026-08-31', 'actual': 7, 'pred': 24.504, 'residual': -17.504, 'severity': '경고'},
                                                  {'date': '2026-08-30', 'actual': 15, 'pred': 10, 'severity': '주의'}]}}
        store = Mock()
        store.snapshot.return_value = snapshot
        sent = []
        def model_response(day, text):
            classified = {'domain': 'IN_SCOPE', 'intent': 'STATION_STATUS', 'stations': ['동부역'], 'line': None, 'date': day,
                          'start_date': None, 'end_date': None, 'needs_clarification': False}
            call = {'type': 'function_call', 'name': 'get_station_status', 'call_id': 'call_status',
                    'arguments': json.dumps({'station': '동부역', 'meter_ref': None, 'line': None, 'date': day})}
            final = {'answer': text, 'source_ids': ['s1'], 'is_hypothesis': False}
            return [SimpleNamespace(output_text=json.dumps(classified), output=[], status='completed'),
                    SimpleNamespace(output_text='', output=[call], status='completed'),
                    SimpleNamespace(output_text=json.dumps(final), output=[], status='completed')]
        replies = model_response('2026-08-31', '동부역 사용량은 7톤으로 예측 24.504톤보다 71.4% 낮으며 경고입니다.') + \
                  model_response('2026-08-30', '동부역 사용량은 15톤이며 주의입니다.')
        def create(**kwargs):
            sent.append(json.loads(json.dumps(kwargs)))
            return replies.pop(0)
        chat_agent.OpenAI.return_value.responses.create.side_effect = create
        with patch('back.rag.tools.RemoteStore', return_value=store), \
             patch('back.api.catalog.data_sources', side_effect=AssertionError('local snapshots are forbidden')) as local:
            first = self.post()
            self.assertEqual(first.status_code, 200, first.text)
            self.assertIn('7톤', first.json()['answer'])
            self.assertIn('71.4% 낮으며', first.json()['answer'])
            self.assertEqual(first.json()['generated_by'], config.OPENAI_MODEL)
            second = self.post('전날은?', first.json()['conversation_id'])
            self.assertEqual(second.status_code, 200, second.text)
            self.assertIn('15톤', second.json()['answer'])
            self.assertEqual(second.json()['sources'][0]['date'], '2026-08-30')
            self.assertEqual(second.json()['sources'][0]['meter_id'], 'private-meter')
            local.assert_not_called()
        self.assertNotIn('private-customer', json.dumps(sent))
        self.assertNotIn('private-meter', json.dumps(sent))
        self.assertEqual(json.loads(sent[3]['input'])['context']['date'], '2026-08-31')
        with db.connect() as c:
            context = json.loads(c.execute('SELECT context FROM chat_conversations').fetchone()[0])
            self.assertEqual(context['date'], '2026-08-30')

    def test_recent_anomalies_need_no_station_or_date_and_use_remote_scoped_latest_day(self):
        self.run_patch.stop()
        with db.connect() as c:
            c.execute('UPDATE meters SET daily_enabled=1')
            c.execute('''INSERT INTO meters(id,provider,customer_number,station_id,office_id,display_name,daily_enabled,created_at,updated_at)
                         VALUES('foreign-meter','arisu','foreign-customer','station','west','서부역',1,?,?)''', (db.now(), db.now()))
        snapshot = {'version': 'remote-version', 'manifest': {'generated_at': '2026-09-01T00:00:00Z'},
                    'daily': {'private-customer': {'usage': [{'date': '2026-08-31', 'value': 20}], 'ridership': []}},
                    'risk': {'private-customer': [{'date': '2026-08-31', 'actual': 20, 'pred': 10, 'severity': '경고'},
                                                  {'date': '2026-08-20', 'actual': 15, 'pred': 10, 'severity': '주의'}],
                             'foreign-customer': [{'date': '2026-09-20', 'actual': 999, 'pred': 10, 'severity': '경고'}]}}
        store = Mock()
        store.snapshot.return_value = snapshot
        replies, sent = [], []
        for day, start, end, severity in [(None, None, None, '경고'),
                                          ('2026-08-20', '2026-08-20', '2026-08-20', None)]:
            classified = {'domain': 'IN_SCOPE', 'intent': 'ANOMALY_SEARCH', 'stations': [], 'line': None, 'date': day,
                          'start_date': None, 'end_date': None, 'needs_clarification': False}
            call = {'type': 'function_call', 'name': 'find_anomalies', 'call_id': 'call_anomaly',
                    'arguments': json.dumps({'station': None, 'meter_ref': None, 'line': None, 'start_date': start, 'end_date': end, 'severity': severity})}
            final = {'answer': ('최근 7일인 2026-08-25~2026-08-31에 경고는 1건이며 동부역입니다.' if day is None
                                else '2026-08-20에 주의는 1건이며 동부역입니다.'),
                     'source_ids': ['s1', 's2'], 'is_hypothesis': False}
            replies.extend([SimpleNamespace(output_text=json.dumps(classified), output=[], status='completed'),
                            SimpleNamespace(output_text='', output=[call], status='completed'),
                            SimpleNamespace(output_text=json.dumps(final), output=[], status='completed')])
        def create(**kwargs):
            sent.append(json.loads(json.dumps(kwargs)))
            return replies.pop(0)
        chat_agent.OpenAI.return_value.responses.create.side_effect = create
        with patch('back.rag.tools.RemoteStore', return_value=store), \
             patch('back.api.catalog.data_sources', side_effect=AssertionError('local snapshots are forbidden')) as local:
            recent = self.post('최근 경고가 나온 역을 알려 주세요')
            self.assertEqual(recent.status_code, 200, recent.text)
            self.assertIn('2026-08-25~2026-08-31', recent.json()['answer'])
            self.assertEqual([source['date'] for source in recent.json()['sources']], ['2026-08-31', '2026-08-31'])
            self.assertEqual(recent.json()['generated_by'], config.OPENAI_MODEL)
            explicit = self.post('2026년 8월 20일에 경고나 주의가 나온 역 있어?')
            self.assertEqual(explicit.status_code, 200, explicit.text)
            self.assertIn('2026-08-20에 주의는 1건', explicit.json()['answer'])
            self.assertEqual(explicit.json()['generated_by'], config.OPENAI_MODEL)
            local.assert_not_called()
        self.assertNotIn('foreign-customer', json.dumps(sent))
        self.assertNotIn('2026-09-20', json.dumps(sent))
        self.assertEqual(sent[1]['tool_choice'], 'required')
        with db.connect() as c:
            saved = json.loads(c.execute('SELECT context FROM chat_conversations WHERE id=?', (recent.json()['conversation_id'],)).fetchone()[0])
            self.assertEqual(saved['date'], '2026-08-31')


if __name__ == '__main__':
    unittest.main()
