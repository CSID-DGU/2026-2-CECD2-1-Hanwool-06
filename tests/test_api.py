"""End-to-end authorization, registry and download checks without external credentials."""
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import unquote
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from pypdf import PdfReader
from back.api import collection, config, data_access, db, main, mailer


class ApplicationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.admin_password = 'LocalTestPass!234'
        root = Path(self.temp.name)
        self.patches = [patch.object(config, 'APP_DB_PATH', root/'test.sqlite3'), patch.object(config, 'SEED_DIR', root), patch.object(config, 'HISTORY_DIR', root / 'history'),
                        patch.object(config, 'ADMIN_EMAIL', 'chief@example.com'), patch.object(config, 'ADMIN_PASSWORD', 'LocalTestPass!234'),
                        patch.object(config, 'COOKIE_SECURE', False), patch.object(config, 'OPENAI_API_KEY', ''), patch.object(config, 'TODAY_OVERRIDE', ''),
                        patch.object(config, 'COLLECTION_WORKER_ENABLED', False)]
        for p in self.patches:
            p.start()
        self.client = TestClient(main.app, base_url='http://localhost')
        self.client.__enter__()
        with db.connect() as c:
            for office, cid in [('동부', '000000001'), ('서부', '000000002')]:
                c.execute('INSERT INTO offices VALUES(?,?)', (office, office))
                c.execute('INSERT INTO stations VALUES(?,?,?,?)', (office+'역', office+'역', 10, 20))
                c.execute('INSERT INTO meters(id,provider,customer_number,station_id,office_id,display_name,line,daily_enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)', (cid, 'arisu', cid, office+'역', office, office+'역', '2', 1, db.now(), db.now()))
                bill = {'ym': '2026-08', '사용량': 32, '납부금액': 123000}
                c.execute('INSERT INTO bills(id,meter_id,period,gubun,payload,source) VALUES(?,?,?,?,?,?)', (cid+':2026-08:정기분', cid, '2026-08', '정기분', json.dumps(bill), 'test'))
        risk = {cid: [{'date': '2026-08-31', 'actual': 20, 'pred': 10, 'residual': 10, 'z': 4, 'severity': '경고', 'dir': '증가', 'err': False}] for cid in ('000000001','000000002')}
        daily = {cid: {'usage': [{'date': '2026-08-31', 'value': 20}], 'ridership': []} for cid in risk}
        self.source_patch = patch('back.api.catalog.data_sources', return_value=(daily, risk))
        self.source_patch.start()

    def tearDown(self):
        self.source_patch.stop()
        self.client.__exit__(None, None, None)
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def login(self, client=None, email='chief@example.com', password=None):
        client = client or self.client
        password = password or self.admin_password
        response = client.post('/api/auth/login', json={'email': email, 'password': password})
        self.assertEqual(response.status_code, 200, response.text)
        client.headers['X-CSRF-Token'] = response.json()['csrf_token']
        if response.json()['user']['must_change_password']:
            self.assertEqual(client.get('/api/data').status_code, 403)
            changed = client.post('/api/auth/password', json={'current_password': password, 'new_password': password + 'New'})
            self.assertEqual(changed.status_code, 200, changed.text)
            if email == 'chief@example.com':
                self.admin_password = password + 'New'
        return response.json()['user']

    def create_manager(self, email='staff@example.com', office='동부'):
        response = self.client.post('/api/users', json={'email': email, 'password': 'ManagerPass!234', 'name': '담당자', 'role': 'office_admin', 'office_ids': [office], 'active': True})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_scoped_session_registry_documents_and_alert(self):
        for path in ('/api/data','/api/meters','/api/users','/api/summary','/api/export.xlsx','/api/bills/000000001:2026-08:정기분/pdf'):
            self.assertEqual(self.client.get(path).status_code, 401, path)
        root = self.login()
        self.assertIn('HttpOnly', self.client.cookies.jar._cookies['localhost.local']['/']['water_session']._rest)
        self.create_manager()
        self.create_manager('staff2@example.com')
        blocked = self.client.post('/api/users', json={'email': 'third@example.com', 'password': 'ManagerPass!234','name': '세번째','role':'office_admin','office_ids':['동부']})
        self.assertEqual(blocked.status_code, 409)
        self.assertEqual(self.client.patch(f"/api/users/{root['id']}", json={'active': False}).status_code, 409)
        manager = TestClient(main.app, base_url='http://localhost')
        staff = self.login(manager, 'staff@example.com', 'ManagerPass!234')
        self.assertEqual(manager.get('/api/users').status_code, 403)
        payload = manager.get('/api/data').json()
        self.assertEqual(set(payload['stations']), {'000000001'})
        self.assertEqual(set(payload['risk']), {'000000001'})
        # Unchanged data is not sent again; another scope never matches this fingerprint.
        self.assertEqual(manager.get('/api/data', params={'version': payload['version']}).status_code, 204)
        self.assertEqual(self.client.get('/api/data', params={'version': payload['version']}).status_code, 200)
        missing, foreign = (manager.get(f'/api/bills/{bill}/pdf') for bill in ('unknown', '000000002:2026-08:정기분'))
        self.assertEqual((missing.status_code, missing.json()), (404, foreign.json()))
        self.assertEqual(len(manager.get('/api/summary').json()['items']), 1)
        for path in ('/api/bills/000000002:2026-08:정기분/pdf', '/api/export.xlsx?meter_id=000000002'):
            self.assertEqual(manager.get(path).status_code, 404)
        self.assertEqual(manager.get('/api/export.xlsx?office_id=서부').status_code, 403)
        self.assertEqual(manager.post('/api/analyze',json={'meter_id':'000000002'}).status_code, 404)
        self.assertEqual(manager.post('/api/analyze',json={'meter_id':'000000001','date':'2025-01-01'}).status_code, 404)
        self.assertEqual(manager.get('/api/summary?date=bad').status_code, 422)
        self.assertEqual(manager.patch('/api/meters/000000001',json={'office_id':'서부'}).status_code, 403)
        nine = manager.post('/api/meters',json={'provider':'arisu','customer_number':'000000004','station_name':'신규역','office_id':'동부','display_name':'9호선','line':'9'})
        self.assertEqual(nine.status_code, 422, nine.text)   # 서울교통공사는 1~8호선만 관리한다
        saved = manager.post('/api/meters',json={'provider':'arisu','customer_number':'000000003','station_name':'신규역','office_id':'동부','display_name':'신규역 직원용','line':'2'})
        self.assertEqual(saved.status_code, 201, saved.text)
        changed = manager.get('/api/data', params={'version': payload['version']})
        self.assertEqual(changed.status_code, 200)
        self.assertEqual(changed.json()['bills']['000000003']['bills'], [])
        self.assertEqual(manager.post('/api/meters',json={'customer_number':'000000003','station_name':'신규역','office_id':'동부','display_name':'중복'}).status_code, 409)
        self.assertEqual(manager.patch('/api/meters/000000003',json={'customer_number':'000000004'}).status_code, 422)
        pdf = manager.get('/api/bills/000000001:2026-08:정기분/pdf?download=true')
        self.assertEqual(pdf.status_code, 200)
        self.assertIn('attachment',pdf.headers['content-disposition'])
        self.assertIn('123,000', PdfReader(io.BytesIO(pdf.content)).pages[0].extract_text())
        xlsx = manager.get('/api/export.xlsx?kind=bills&dl=abc123')
        rows = list(load_workbook(io.BytesIO(xlsx.content)).active.values)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][1], '000000001')
        self.assertEqual([name for name in rows[0] if name.startswith('_') or name == '집계사용량_톤'], [])   # 내부 집계값은 파일에 없다
        # the download marker comes back as a readable cookie so the screen can close its "preparing" notice
        self.assertEqual(xlsx.cookies.get('dl_abc123'), '1')
        self.assertNotIn('HttpOnly', xlsx.headers['set-cookie'])
        self.assertNotIn('set-cookie', manager.get('/api/export.xlsx?kind=bills&dl=bad%20token').headers)
        # a file that could not be made is marked too, so the screen stops waiting and says so
        failed = manager.get('/api/export.xlsx?kind=bills&meter_id=missing&dl=fail1')
        self.assertEqual((failed.status_code, failed.cookies.get('dl_fail1')), (404, '0'))
        self.assertNotIn('HttpOnly', failed.headers['set-cookie'])
        self.assertNotIn('set-cookie', manager.get('/api/export.xlsx?kind=bills&meter_id=missing').headers)
        # 내려받는 파일 이름: 계량기 하나면 역이름_호선_고객번호_종류, 여럿이면 종류(_기간)
        saved_as = lambda query: unquote(manager.get('/api/export.xlsx?' + query).headers['content-disposition'].split("filename*=UTF-8''")[1])
        self.assertEqual(saved_as('kind=bills&meter_id=000000001'), '동부역_2호선_000000001_청구내역.xlsx')
        self.assertEqual(saved_as('kind=usage&meter_id=000000001'), '동부역_2호선_000000001_사용량.xlsx')
        self.assertEqual(saved_as('kind=bills'), '청구내역.xlsx')
        self.assertEqual(saved_as('kind=usage&start=2026-08-01&end=2026-08-31'), '사용량_2026-08-01_2026-08-31.xlsx')
        self.assertEqual(main.export_filename('bills', {'station_name': '가/나:역', 'line': None, 'customer_number': '1'}), '가_나_역_1_청구내역.xlsx')
        stats = manager.get('/api/stats').json()['rows']
        self.assertEqual(stats[0]['billed_won'],123000)
        self.assertEqual(stats[0]['billed_usage_ton'], 32)   # 청구 사용량 합계. Excel에서 뺀 집계 열이 통계에서도 빠지면 안 된다
        self.assertEqual(stats[0]['usage_ton'],20)
        with patch.object(mailer,'send_mail',return_value={'sent':True,'to':['staff@example.com']}) as send:
            response = manager.post('/api/alert',json={'meter_id':'000000001','date':'2026-08-31'})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(set(send.call_args.args[2].split(',')),{'staff@example.com','staff2@example.com'})
            self.assertEqual(manager.post('/api/alert',json={'meter_id':'000000001','date':'2026-08-31'}).status_code,429)
            self.assertEqual(manager.post('/api/alert',json={'meter_id':'000000001','to':'outside@example.com'}).status_code,422)
            with db.connect() as c:
                for index in range(30):
                    db.audit(c, root['id'], 'alert_attempt', f'earlier:{index}')
            capped = self.client.post('/api/alert', json={'meter_id': '000000002', 'date': '2026-08-31'})
            self.assertEqual(capped.status_code, 429)
            self.assertIn('한 시간', capped.json()['detail'])
            self.assertEqual(send.call_count, 1)
        self.client.patch(f"/api/users/{staff['id']}",json={'active':False})
        self.assertEqual(manager.get('/api/data').status_code,401)
        manager.close()

    def test_delete_user_keeps_history_and_frees_email(self):
        root = self.login()
        staff = self.create_manager()
        manager = TestClient(main.app, base_url='http://localhost')
        self.login(manager, 'staff@example.com', 'ManagerPass!234')
        self.assertEqual(manager.delete(f"/api/users/{staff['id']}").status_code, 403)
        self.assertEqual(self.client.delete(f"/api/users/{root['id']}").status_code, 409)
        with db.connect() as c:
            db.audit(c, staff['id'], 'meter_update', '000000001')
            c.execute("INSERT INTO chat_conversations(id,user_id,scope_fingerprint,updated_at) VALUES('chat1',?,'x',0)", (staff['id'],))
        self.assertEqual(self.client.delete(f"/api/users/{staff['id']}").status_code, 200)
        self.assertEqual(manager.get('/api/data').status_code, 401)
        self.assertNotIn(staff['id'], [u['id'] for u in self.client.get('/api/users').json()])
        self.assertEqual(self.client.post('/api/auth/login', json={'email': 'staff@example.com', 'password': 'ManagerPass!234New'}).status_code, 401)
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM audit_log WHERE actor_id=? AND action='meter_update'", (staff['id'],)).fetchone()[0], 1)
            self.assertIsNone(c.execute('SELECT 1 FROM chat_conversations WHERE user_id=?', (staff['id'],)).fetchone())
            deleted = json.loads(c.execute("SELECT details FROM audit_log WHERE action='user_delete'").fetchone()[0])
        self.assertEqual((deleted['email'], deleted['office_ids']), ('staff@example.com', ['동부']))
        self.assertEqual(self.client.delete(f"/api/users/{staff['id']}").status_code, 404)
        self.assertEqual(self.client.patch(f"/api/users/{staff['id']}", json={'active': True}).status_code, 404)
        self.create_manager()   # 같은 이메일로 같은 사업소에 다시 등록할 수 있다
        manager.close()

    def test_user_list_shows_latest_successful_login(self):
        root = self.login()
        staff = self.create_manager()
        last_login = lambda: {u['id']: u['last_login_at'] for u in self.client.get('/api/users').json()}
        self.assertIsNone(last_login()[staff['id']])
        self.assertIsNotNone(last_login()[root['id']])
        self.assertEqual(self.client.post('/api/auth/login', json={'email': 'staff@example.com', 'password': 'wrong-password'}).status_code, 401)
        self.assertIsNone(last_login()[staff['id']])
        with db.connect() as c:
            for action, created in [('login', '2026-10-05T01:00:00+00:00'), ('login', '2026-10-01T01:00:00+00:00'), ('meter_update', '2026-10-09T01:00:00+00:00')]:
                c.execute('INSERT INTO audit_log(actor_id,action,target,created_at) VALUES(?,?,?,?)', (staff['id'], action, '', created))
        self.assertEqual(last_login()[staff['id']], '2026-10-05T01:00:00+00:00')
        manager = TestClient(main.app, base_url='http://localhost')
        self.login(manager, 'staff@example.com', 'ManagerPass!234')
        self.assertGreater(last_login()[staff['id']], '2026-10-05T01:00:00+00:00')
        self.assertNotIn('last_login_at', manager.get('/api/auth/me').json()['user'])
        manager.close()

    def test_csrf_password_session_and_login_limit(self):
        self.login()
        csrf = self.client.headers.pop('X-CSRF-Token')
        self.assertEqual(self.client.post('/api/auth/logout').status_code,403)
        self.assertEqual(self.client.post('/api/auth/logout', headers={'X-CSRF-Token': b'\xe9'}).status_code, 403)
        self.client.headers['X-CSRF-Token'] = csrf
        self.assertEqual(self.client.post('/api/auth/logout',headers={'Origin':'https://outsider.invalid'}).status_code,403)
        old_session = TestClient(main.app, base_url='http://localhost')
        self.login(old_session)
        changed = self.client.post('/api/auth/password',json={'current_password':self.admin_password,'new_password':'ChangedPass!234'})
        self.assertEqual(changed.status_code,200)
        self.assertEqual(old_session.get('/api/data').status_code,401)
        self.assertEqual(self.client.post('/api/auth/logout').status_code,200)
        self.assertEqual(self.client.get('/api/data').status_code,401)
        for _ in range(5):
            self.assertEqual(self.client.post('/api/auth/login',json={'email':'chief@example.com','password':'incorrectpass'}).status_code,401)
        self.assertEqual(self.client.post('/api/auth/login',json={'email':'chief@example.com','password':'ChangedPass!234'}).status_code,429)
        other_ip = TestClient(main.app, base_url='http://localhost', client=('192.0.2.1', 50000))
        self.assertEqual(other_ip.post('/api/auth/login', json={'email': 'chief@example.com', 'password': 'ChangedPass!234'}).status_code, 200)
        other_ip.close()
        old_session.close()

    def test_password_change_cannot_undo_a_reset_made_meanwhile(self):
        """현재 비밀번호를 확인한 뒤 저장하기 전에 관리자가 재설정하면, 먼저 시작한 변경 요청은 버려야 한다."""
        self.login()
        verify = main.auth.verify_password

        def verified_then_reset(password, hashed):
            ok = verify(password, hashed)
            with db.connect() as c:   # 그 사이 관리자가 비밀번호를 바꾸고 세션을 끊는다
                c.execute('UPDATE users SET password_hash=?,must_change_password=1', (main.auth.hash_password('AdminResetPass!234'),))
                c.execute('DELETE FROM sessions')
            return ok

        with patch.object(main.auth, 'verify_password', side_effect=verified_then_reset):
            late = self.client.post('/api/auth/password', json={'current_password': self.admin_password, 'new_password': 'TakenOverPass!234'})
        self.assertEqual(late.status_code, 401, late.text)
        fresh = TestClient(main.app, base_url='http://localhost', client=('192.0.2.3', 50000))
        self.assertEqual(fresh.post('/api/auth/login', json={'email': 'chief@example.com', 'password': 'TakenOverPass!234'}).status_code, 401)
        self.assertEqual(fresh.post('/api/auth/login', json={'email': 'chief@example.com', 'password': 'AdminResetPass!234'}).status_code, 200)
        fresh.close()

    def test_password_change_attempts_are_limited_like_login(self):
        user = self.login()
        wrong = {'current_password': 'NotTheCurrentPass!1', 'new_password': 'AnotherNewPass!234'}
        for _ in range(5):
            self.assertEqual(self.client.post('/api/auth/password', json=wrong).status_code, 400)
        with patch.object(main.auth, 'verify_password') as verify:   # 막힌 뒤에는 비싼 검증을 하지 않는다
            blocked = self.client.post('/api/auth/password', json={'current_password': self.admin_password, 'new_password': 'AnotherNewPass!234'})
        self.assertEqual((blocked.status_code, verify.call_count), (429, 0))
        with db.connect() as c:
            c.execute('DELETE FROM login_attempts')
        for _ in range(4):
            self.assertTrue(main.LOGIN_HASH_SLOTS.acquire(blocking=False))
        try:   # 로그인과 같은 해시 연산 한도를 쓴다
            self.assertEqual(self.client.post('/api/auth/password', json=wrong).status_code, 429)
        finally:
            for _ in range(4):
                main.LOGIN_HASH_SLOTS.release()
        # Requests that were already verifying when the limit filled up are refused too, even a correct one.
        identity = main.auth.token_hash(f"password:{user['id']}")
        verify = main.auth.verify_password

        def limit_fills_meanwhile(password, hashed):
            with db.connect() as c:
                c.executemany('INSERT INTO login_attempts(attempted_at,identity) VALUES(?,?)', [(main.time.time(), identity)] * 5)
            return verify(password, hashed)

        with patch.object(main.auth, 'verify_password', side_effect=limit_fills_meanwhile):
            late = self.client.post('/api/auth/password', json={'current_password': self.admin_password, 'new_password': 'AnotherNewPass!234'})
        self.assertEqual(late.status_code, 429, late.text)
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM login_attempts WHERE identity=?', (identity,)).fetchone()[0], 5)
            c.execute('DELETE FROM login_attempts')
        changed = self.client.post('/api/auth/password', json={'current_password': self.admin_password, 'new_password': 'AnotherNewPass!234'})
        self.assertEqual(changed.status_code, 200, changed.text)

    def test_file_request_that_crashes_is_marked_as_failed(self):
        quiet = TestClient(main.app, base_url='http://localhost', raise_server_exceptions=False)
        self.login(quiet)
        with patch.object(main.documents, 'export_xlsx', side_effect=RuntimeError('disk full')):
            crashed = quiet.get('/api/export.xlsx?kind=bills&dl=boom1')
        self.assertEqual((crashed.status_code, crashed.cookies.get('dl_boom1')), (500, '0'))
        self.assertNotIn('disk full', crashed.text)
        quiet.close()

    def test_page_policy_and_request_body_limit(self):
        self.assertIn("default-src 'self'", self.client.get('/').headers['content-security-policy'])
        health = self.client.get('/api/health')
        self.assertNotIn('content-security-policy', health.headers)
        self.assertEqual(health.headers['cache-control'], 'no-store')
        oversized = b'{"email":"' + b'a' * main.MAX_BODY_BYTES + b'","password":"x"}'
        for body in (oversized, iter([b'{"email":"a@example.com","password":"x"}'])):
            response = self.client.post('/api/auth/login', content=body, headers={'Content-Type': 'application/json'})
            self.assertEqual(response.status_code, 413)
        self.assertEqual(self.client.post('/api/auth/login', json={'email': 'a@example.com', 'password': 'x'}).status_code, 401)

    def test_login_hash_does_not_hold_writer_lock_and_ip_limit_applies(self):
        verify = main.auth.verify_password

        def unlocked_verify(*args):
            with sqlite3.connect(config.APP_DB_PATH, timeout=0) as c:
                c.execute('BEGIN IMMEDIATE')
            return verify(*args)

        with patch.object(main.auth, 'verify_password', side_effect=unlocked_verify):
            response = self.client.post('/api/auth/login', json={'email': 'chief@example.com', 'password': self.admin_password})
        self.assertEqual(response.status_code, 200, response.text)
        with patch.object(main.auth, 'verify_password', return_value=False):
            for index in range(30):
                response = self.client.post('/api/auth/login', json={'email': f'unknown{index}@example.com', 'password': 'wrong'})
                self.assertEqual(response.status_code, 401)
        self.assertEqual(self.client.post('/api/auth/login', json={'email': 'chief@example.com', 'password': self.admin_password}).status_code, 429)
        other_ip = TestClient(main.app, base_url='http://localhost', client=('192.0.2.2', 50000))
        self.assertEqual(other_ip.post('/api/auth/login', json={'email': 'chief@example.com', 'password': self.admin_password}).status_code, 200)
        other_ip.close()
        for _ in range(4):
            self.assertTrue(main.LOGIN_HASH_SLOTS.acquire(blocking=False))
        try:
            self.assertEqual(self.client.post('/api/auth/login', json={'email': 'chief@example.com', 'password': self.admin_password}).status_code, 429)
        finally:
            for _ in range(4):
                main.LOGIN_HASH_SLOTS.release()

    def test_export_can_be_limited_to_the_meters_shown_on_screen(self):
        def exported(client, query):
            response = client.get('/api/export.xlsx?' + query)
            self.assertEqual(response.status_code, 200, response.text)
            rows = list(load_workbook(io.BytesIO(response.content)).active.values)
            return {row[rows[0].index('고객번호')] for row in rows[1:]}
        self.login()
        # meter_ids 가 없으면 이전과 같다
        self.assertEqual(exported(self.client, 'kind=usage'), {'000000001', '000000002'})
        self.assertEqual(exported(self.client, 'kind=usage&meter_ids=000000002'), {'000000002'})
        self.assertEqual(exported(self.client, 'kind=bills&meter_ids=000000001,%20000000002,000000001'), {'000000001', '000000002'})
        # 다른 조건과 함께 쓰면 둘 다 만족하는 계량기만
        self.assertEqual(exported(self.client, 'kind=usage&office_id=동부&meter_ids=000000001,000000002'), {'000000001'})
        self.assertEqual(exported(self.client, 'kind=usage&start=2026-09-01&meter_ids=000000001'), set())
        self.assertEqual(self.client.get('/api/export.xlsx?kind=usage&meter_ids=unknown').status_code, 404)
        for invalid in ('', ',,', ','.join(str(n) for n in range(1001)), 'a' * 101):
            self.assertEqual(self.client.get('/api/export.xlsx', params={'kind': 'usage', 'meter_ids': invalid}).status_code, 422, invalid[:20])
        # 담당 사업소 밖 계량기는 없는 계량기와 똑같이 거절한다
        self.create_manager()
        manager = TestClient(main.app, base_url='http://localhost')
        self.login(manager, 'staff@example.com', 'ManagerPass!234')
        self.assertEqual(exported(manager, 'kind=usage&meter_ids=000000001'), {'000000001'})
        self.assertEqual(manager.get('/api/export.xlsx?kind=usage&meter_ids=000000001,000000002').status_code, 404)
        manager.close()

    def test_ai_analysis_limits_paid_calls_per_account(self):
        self.login()
        with patch.object(config, 'OPENAI_API_KEY', 'test-key'), patch.object(main.agent, 'analyze_cause', return_value={'ok': True}) as analyze:
            for _ in range(20):
                response = self.client.post('/api/analyze', json={'meter_id': '000000001', 'date': '2026-08-31'})
                self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.client.post('/api/analyze', json={'meter_id': '000000001', 'date': '2026-08-31'}).status_code, 429)
            self.assertEqual(analyze.call_count, 20)

    def test_production_config_requires_https_and_secure_cookie(self):
        with patch.object(config, 'APP_ENV', 'production'), patch.object(config, 'COOKIE_SECURE', False):
            with self.assertRaisesRegex(RuntimeError, 'APP_COOKIE_SECURE'):
                config.validate_deployment()
        with patch.object(config, 'APP_ENV', 'production'), patch.object(config, 'COOKIE_SECURE', True), \
             patch.object(config, 'FRONT_ORIGINS', ['https://water.example.com']), \
             patch.object(config, 'ALLOWED_HOSTS', ['water.example.com']), \
             patch.object(config, 'DATA_DIR', Path(self.temp.name)):
            config.validate_deployment()
            with patch.object(config, 'DATA_DIR', config.ROOT / 'data/runtime'):
                with self.assertRaisesRegex(RuntimeError, '저장소 밖'):
                    config.validate_deployment()
            with patch.object(config, 'APP_DB_PATH', config.ROOT / 'data/runtime/app.sqlite3'):
                with self.assertRaisesRegex(RuntimeError, '저장소 밖'):
                    config.validate_deployment()

    def test_collection_worker_recovers_after_status_write_failure(self):
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            job_id = collection.enqueue(c, ['000000001'], 'manual')
        original_connect = db.connect
        calls = 0

        def flaky_connect():
            nonlocal calls
            calls += 1
            if calls == 2:
                raise sqlite3.OperationalError('temporary database failure')
            return original_connect()

        stop = Mock()
        stop.is_set.side_effect = [False, False, False, False, True]
        with patch.object(config, 'DATA_DIR', Path(self.temp.name) / 'runtime'), \
             patch.object(collection, 'schedule_due', return_value=None), \
             patch.object(collection, 'run_job', side_effect=RuntimeError('worker error')) as run_job, \
             patch.object(collection.db, 'connect', side_effect=flaky_connect):
            collection.worker(stop)
        run_job.assert_called_once()
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT status FROM collection_jobs WHERE id=?', (job_id,)).fetchone()[0], 'failed')

    def test_other_provider_cannot_alias_foreign_arisu_customer(self):
        self.login()
        self.create_manager()
        manager = TestClient(main.app, base_url='http://localhost')
        self.login(manager, 'staff@example.com', 'ManagerPass!234')
        created = manager.post('/api/meters', json={
            'provider': 'other', 'customer_number': '000000002', 'station_name': '기타공급역',
            'office_id': '동부', 'display_name': '기타 공급 계약', 'daily_enabled': False,
        })
        self.assertEqual(created.status_code, 201, created.text)
        alias_id = created.json()['id']
        _, risk = main.catalog.data_sources()
        risk['000000002'].append(risk['000000002'][0] | {'date': '2026-09-01'})
        scoped = manager.get('/api/data').json()
        self.assertNotIn(alias_id, scoped['risk'])
        self.assertEqual(manager.get('/api/summary').json()['기준일'], '2026-08-31')
        for route in ('summary', 'anomalies'):
            response = manager.get(f'/api/{route}?date=2026-08-31').json()
            self.assertEqual([item['meter_id'] for item in response['items']], ['000000001'])
        for route in ('analyze', 'alert'):
            response = manager.post(f'/api/{route}', json={'meter_id': alias_id, 'date': '2026-09-01'})
            self.assertEqual(response.status_code, 404, response.text)
        self.assertIsNone(data_access.analysis_context(alias_id, '2026-09-01', ['동부']))
        manager.close()

    def test_collection_permissions_queue_and_recoverable_deletion(self):
        self.assertEqual(self.client.get('/api/collection').status_code, 401)
        self.login()
        self.create_manager()
        manager = TestClient(main.app, base_url='http://localhost')
        self.login(manager, 'staff@example.com', 'ManagerPass!234')
        self.assertEqual(manager.post('/api/collection', json={'meter_id': '000000002'}).status_code, 404)
        self.assertEqual(manager.delete('/api/meters/000000002').status_code, 404)
        self.assertEqual(manager.post('/api/meters/000000002/restore').status_code, 404)
        # The schedule is fixed; no role can change it through the API.
        self.assertEqual(self.client.patch('/api/collection/settings', json={'enabled': False, 'hour': 9}).status_code, 405)
        queued = self.client.post('/api/collection', json={})
        self.assertEqual(queued.status_code, 202, queued.text)
        self.assertEqual(queued.json()['total'], 2)
        self.assertEqual(manager.post('/api/collection', json={}).status_code, 409)
        state = manager.get('/api/collection').json()
        self.assertEqual(state['running']['total'], 1)
        self.assertEqual([m['meter_id'] for m in state['meters']], ['000000001'])
        self.assertNotIn('000000002', json.dumps(state))
        # Deletion withdraws queued work and hides all history until restored.
        self.assertEqual(manager.delete('/api/meters/000000001').status_code, 200)
        self.assertEqual(manager.get('/api/meters').json(), [])
        archived = manager.get('/api/meters?include_deleted=true').json()
        self.assertEqual(len(archived), 1)
        self.assertTrue(archived[0]['deleted_at'])
        self.assertEqual(manager.get('/api/bills/000000001:2026-08:정기분/pdf').status_code, 404)
        self.assertEqual(manager.post('/api/collection', json={'meter_id': '000000001'}).status_code, 404)
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT status FROM collection_items WHERE meter_id=?', ('000000001',)).fetchone()[0], 'skipped')
        restored = manager.post('/api/meters/000000001/restore')
        self.assertEqual(restored.status_code, 200, restored.text)
        self.assertIsNone(restored.json()['deleted_at'])
        self.assertEqual(manager.get('/api/bills/000000001:2026-08:정기분/pdf').status_code, 200)
        self.assertEqual(manager.get('/api/collection').json()['running']['trigger'], 'restoration')
        # A running request holds a stable meter registration until it completes.
        with db.connect() as c:
            c.execute("UPDATE collection_items SET status='running' WHERE meter_id=? AND status='queued'", ('000000001',))
        self.assertEqual(manager.delete('/api/meters/000000001').status_code, 409)
        self.assertEqual(manager.patch('/api/meters/000000001', json={'active': False}).status_code, 409)
        manager.close()

    def test_daily_schedule_is_once_per_kst_day_and_skips_inactive(self):
        with patch.object(collection, 'configured', return_value={'water': True, 'bills': True}):
            with db.connect() as c:
                c.execute('UPDATE meters SET active=0 WHERE id=?', ('000000002',))
                c.execute('BEGIN IMMEDIATE') if not c.in_transaction else None
                current = datetime(2026, 9, 23, 8, 5, tzinfo=collection.KST)
                collection.schedule_due(c, current)
                self.assertEqual(c.execute('SELECT count(*) FROM collection_jobs').fetchone()[0], 1)
                self.assertEqual(c.execute('SELECT count(*) FROM collection_items').fetchone()[0], 1)
                c.execute("UPDATE collection_jobs SET status='success'")
                c.execute("UPDATE collection_items SET status='success'")
                self.assertIsNone(collection.schedule_due(c, current))   # 모두 성공: 재시도·메일 없이 마무리
                self.assertEqual(c.execute('SELECT count(*) FROM collection_jobs').fetchone()[0], 1)
                self.assertEqual(c.execute("SELECT value FROM settings WHERE key='collection_auto_settled'").fetchone()[0], '2026-09-23')
                collection.schedule_due(c, datetime(2026, 9, 24, 7, tzinfo=collection.KST))
                self.assertEqual(c.execute('SELECT count(*) FROM collection_jobs').fetchone()[0], 1)
                collection.schedule_due(c, datetime(2026, 9, 24, 8, tzinfo=collection.KST))
                self.assertEqual(c.execute('SELECT count(*) FROM collection_jobs').fetchone()[0], 2)

    def test_daily_schedule_is_fixed_at_8_kst_and_ignores_stored_setting(self):
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            # An older database may still hold the removed superadmin setting.
            c.execute("INSERT INTO settings VALUES('collection_schedule',?)", (json.dumps({'enabled': False, 'hour': 9}),))
            before = datetime(2026, 9, 23, 7, 59, tzinfo=collection.KST)
            with patch.object(config, 'COLLECTION_WORKER_ENABLED', True):
                self.assertEqual(collection.settings(c, before), {'enabled': True, 'hour': 8, 'timezone': 'Asia/Seoul',
                    'next_run_at': '2026-09-23T08:00:00+09:00', 'smart_hours': [0, 8, 12, 18],
                    'smart_next_run_at': '2026-09-23T07:59:00+09:00', 'smart_paused_reason': None})
            self.assertIsNone(collection.settings(c, before)['next_run_at'])
            self.assertFalse(collection.settings(c, before)['enabled'])   # 서버에서 끄면 화면에 '꺼짐'
            collection.schedule_due(c, before)
            self.assertEqual(c.execute('SELECT count(*) FROM collection_jobs').fetchone()[0], 0)
            collection.schedule_due(c, datetime(2026, 9, 23, 8, tzinfo=collection.KST))
            self.assertEqual(c.execute("SELECT count(*) FROM collection_jobs WHERE trigger='automatic'").fetchone()[0], 1)
            with patch.object(config, 'COLLECTION_WORKER_ENABLED', True):   # 오늘 자동 수집을 넣은 뒤에는 다음 날 08:00
                self.assertEqual(collection.settings(c, datetime(2026, 9, 23, 10, tzinfo=collection.KST))['next_run_at'], '2026-09-24T08:00:00+09:00')

    def schedule_at(self, hour, minute=0, day=23):
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            return collection.schedule_due(c, datetime(2026, 9, day, hour, minute, tzinfo=collection.KST))

    def finish_queued_job(self, collect, hour, minute, stop=None, session=None, day=23):
        """Runs the queued job through run_job with a stand-in collector and Arisu session, finishing at hour:minute KST."""
        with db.connect() as c:
            job_id = c.execute("SELECT id FROM collection_jobs WHERE status='queued'").fetchone()[0]
        with patch('back.scripts.billing_etl.i121_crawler.auth.collection_session', return_value=session or Mock(arisu_login_error=None)), \
             patch('back.pipelines.incremental.collect_meter', side_effect=collect):
            collection.run_job(job_id, stop or threading.Event())
        finished = datetime(2026, 9, day, hour, minute, tzinfo=collection.KST).astimezone(timezone.utc).isoformat(timespec='seconds')
        with db.connect() as c:
            c.execute('UPDATE collection_jobs SET finished_at=? WHERE id=?', (finished, job_id))
            return [r[0] for r in c.execute('SELECT meter_id FROM collection_items WHERE job_id=? ORDER BY meter_id', (job_id,))]

    def test_daily_network_failures_retry_twice_30_minutes_apart_then_mail_once(self):
        timeout = {'status': 'failed', 'message': '아리수 서버의 응답 시간이 초과되었습니다.', 'retryable': True}
        results = {'000000001': timeout, '000000002': {'status': 'partial', 'message': '일일 사용량: 아리수 계정 설정 또는 로그인을 확인해 주세요', 'bill_rows': 1}}
        collect = lambda meter, **kwargs: results[meter['id']]
        self.assertIsNone(self.schedule_at(8))
        self.assertEqual(self.finish_queued_job(collect, 8, 20), ['000000001', '000000002'])
        self.assertIsNone(self.schedule_at(8, 49))   # 끝나고 30분이 지나야 다시 시도한다
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM collection_jobs').fetchone()[0], 1)
        self.assertIsNone(self.schedule_at(8, 50))
        self.assertEqual(self.finish_queued_job(collect, 8, 55), ['000000001'])   # 로그인 문제는 다시 시도하지 않는다
        self.assertIsNone(self.schedule_at(9, 24))   # 두 번째도 앞 재시도가 끝나고 30분 뒤
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM collection_jobs').fetchone()[0], 2)
        self.assertIsNone(self.schedule_at(9, 25))
        self.assertEqual(self.finish_queued_job(collect, 9, 30), ['000000001'])
        alert = self.schedule_at(10)
        self.assertEqual((alert['day'], alert['retries'], alert['recipients']), ('2026-09-23', 2, ['chief@example.com']))
        self.assertEqual([(i['meter_id'], i['status'], i['display_name']) for i in alert['items']],
                         [('000000001', 'failed', '동부역'), ('000000002', 'partial', '서부역')])
        self.assertIsNone(self.schedule_at(11))   # 메일은 하루 한 번
        with db.connect() as c:
            self.assertEqual([r[0] for r in c.execute('SELECT trigger FROM collection_jobs ORDER BY id')], ['automatic', 'automatic_retry', 'automatic_retry'])
            self.assertEqual(c.execute("SELECT value FROM settings WHERE key='collection_auto_settled'").fetchone()[0], '2026-09-23')

    def test_interrupted_daily_run_is_retried_and_unrecovered_failure_is_mailed(self):
        stop = threading.Event()

        def interrupted(meter, **kwargs):
            stop.set()   # 서버가 첫 계량기 뒤에 멈춘다
            return {'status': 'success', 'message': '자료 확인'}

        self.assertIsNone(self.schedule_at(8))
        self.finish_queued_job(interrupted, 8, 10, stop)
        with db.connect() as c:
            self.assertEqual(c.execute("SELECT status FROM collection_items WHERE meter_id='000000002'").fetchone()[0], 'interrupted')
        self.assertIsNone(self.schedule_at(8, 40))
        name_error = {'status': 'failed', 'message': '청구서: 고객번호와 고지서상 성명을 확인해 주세요'}
        self.assertEqual(self.finish_queued_job(lambda meter, **kwargs: name_error, 8, 45), ['000000002'])
        alert = self.schedule_at(9, 30)   # 자료 오류는 재시도 한도 전이라도 다시 시도하지 않고 바로 알린다
        self.assertEqual((alert['retries'], [i['meter_id'] for i in alert['items']]), (1, ['000000002']))
        body = mailer.build_collection_alert_html(alert)
        self.assertIn('재시도 1번', body)
        self.assertIn('서부역(000000002)', body)

    def test_rejected_login_stops_retries_for_the_rest_of_the_day(self):
        stop = threading.Event()
        rejected = Mock(arisu_customer_numbers=frozenset(), arisu_login_error='LoginError')
        login = {'status': 'partial', 'message': '일일 사용량: 아리수 계정 설정 또는 로그인을 확인해 주세요', 'bill_rows': 1}

        def interrupted(meter, **kwargs):
            stop.set()   # 비밀번호가 거부된 날, 서버가 첫 계량기 뒤에 멈춘다
            return login

        self.assertIsNone(self.schedule_at(8))
        self.finish_queued_job(interrupted, 8, 10, stop, rejected)
        alert = self.schedule_at(9)   # 중단된 계량기도 다시 로그인하지 않고 바로 알린다
        self.assertEqual((alert['retries'], [(i['meter_id'], i['status']) for i in alert['items']]),
                         (0, [('000000001', 'partial'), ('000000002', 'interrupted')]))
        # 다음 날은 평소처럼 한 번 로그인하고, 일시 오류는 다시 시도한다.
        timeout = {'status': 'failed', 'message': '아리수 서버의 응답 시간이 초과되었습니다.', 'retryable': True}
        self.assertIsNone(self.schedule_at(8, day=24))
        self.finish_queued_job(lambda meter, **kwargs: timeout, 8, 20, day=24)
        self.assertIsNone(self.schedule_at(8, 50, day=24))
        with db.connect() as c:
            self.assertEqual([r[0] for r in c.execute('SELECT trigger FROM collection_jobs ORDER BY id')], ['automatic', 'automatic', 'automatic_retry'])

    def test_login_rejected_in_manual_update_cancels_pending_retry(self):
        timeout = {'status': 'failed', 'message': '아리수 서버의 응답 시간이 초과되었습니다.', 'retryable': True}
        self.assertIsNone(self.schedule_at(8))
        self.finish_queued_job(lambda meter, **kwargs: timeout, 8, 5)
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            collection.enqueue(c, ['000000001'], 'manual')
        login = {'status': 'partial', 'message': '일일 사용량: 아리수 계정 설정 또는 로그인을 확인해 주세요', 'bill_rows': 1}
        self.finish_queued_job(lambda meter, **kwargs: login, 8, 10, session=Mock(arisu_customer_numbers=frozenset(), arisu_login_error='LoginError'))
        alert = self.schedule_at(8, 40)   # 08:05 시간 초과분도 다시 로그인하지 않는다
        self.assertEqual([(i['meter_id'], i['status']) for i in alert['items']], [('000000001', 'partial'), ('000000002', 'failed')])
        with db.connect() as c:
            self.assertEqual([r[0] for r in c.execute('SELECT trigger FROM collection_jobs ORDER BY id')], ['automatic', 'manual'])

    def test_day_with_nothing_to_collect_settles_at_once(self):
        with db.connect() as c:
            c.execute('UPDATE meters SET active=0')
        self.assertIsNone(self.schedule_at(8, day=22))   # 켜진 아리수 계량기가 하나도 없는 날
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM collection_jobs').fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT value FROM settings WHERE key='collection_auto_settled'").fetchone()[0], '2026-09-22')

    def test_worker_mails_settled_failures_after_commit_and_survives_mail_errors(self):
        alert = {'day': '2026-09-23', 'retries': 2, 'recipients': ['chief@example.com'],
                 'items': [{'meter_id': '000000001', 'status': 'failed', 'message': '응답 시간 초과 <b>',
                            'display_name': '동부역', 'customer_number': '000000001'}]}
        for outcome in ({'sent': True, 'to': ['chief@example.com']}, RuntimeError('smtp down')):
            with self.subTest(outcome=type(outcome).__name__):
                def send(subject, body, to):
                    with db.connect() as c:
                        c.execute('BEGIN IMMEDIATE')   # 큐 트랜잭션을 끝낸 뒤 보낸다
                    if isinstance(outcome, Exception):
                        raise outcome
                    return outcome

                stop = Mock()
                stop.is_set.side_effect = [False, False, True]
                with patch.object(config, 'DATA_DIR', Path(self.temp.name) / 'runtime'), \
                     patch.object(collection, 'schedule_due', return_value=alert), \
                     patch.object(mailer, 'send_mail', side_effect=send) as mail:
                    collection.worker(stop)
                subject, body, to = mail.call_args.args
                self.assertIn('1곳', subject)
                self.assertEqual(to, 'chief@example.com')
                self.assertIn('재시도 2번', body)
                self.assertNotIn('<b>', body)
        with db.connect() as c:
            self.assertEqual([(r[0], json.loads(r[1]).get('reason')) for r in c.execute("SELECT action,details FROM audit_log WHERE action LIKE 'collection_alert%' ORDER BY id")],
                             [('collection_alert_sent', None), ('collection_alert_failed', 'RuntimeError')])

    def test_existing_collection_history_gains_retry_flag(self):
        with db.connect() as c:
            c.executescript('''DROP TABLE collection_items;
                CREATE TABLE collection_items(
                  job_id INTEGER NOT NULL REFERENCES collection_jobs(id),meter_id TEXT NOT NULL REFERENCES meters(id),
                  status TEXT NOT NULL DEFAULT 'queued',message TEXT NOT NULL DEFAULT '',started_at TEXT,finished_at TEXT,
                  daily_rows INTEGER NOT NULL DEFAULT 0,bill_rows INTEGER NOT NULL DEFAULT 0,
                  connection_verified INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(job_id,meter_id));
                INSERT INTO collection_jobs(id,status,trigger,created_at) VALUES(1,'failed','automatic','2026-09-22T23:00:00+00:00');
                INSERT INTO collection_items(job_id,meter_id,status) VALUES(1,'000000001','failed');''')
        db.init_db()
        db.init_db()
        with db.connect() as c:
            self.assertEqual(tuple(c.execute('SELECT status,retryable FROM collection_items').fetchone()), ('failed', 0))

    def test_collection_worker_keeps_partial_results_and_no_false_connection(self):
        self.login()
        job = self.client.post('/api/collection', json={}).json()
        with db.connect() as c:
            c.execute("UPDATE collection_jobs SET status='running' WHERE id=?", (job['id'],))
        results = [dict(status='success', message='새 청구서 1건', daily_rows=0, bill_rows=1, connection_verified=True),
                   dict(status='empty', message='조회 기간에 자료 없음', daily_rows=0, bill_rows=0, connection_verified=False)]
        session = Mock()
        with patch('back.pipelines.incremental.collect_meter', side_effect=results) as collect, patch('back.scripts.billing_etl.i121_crawler.auth.session_from_env', return_value=session) as login:
            collection.run_job(job['id'], threading.Event())
            self.assertEqual(collect.call_count, 2)
            login.assert_called_once()
            session.close.assert_called_once()
        state = self.client.get('/api/collection').json()
        self.assertIsNone(state['running'])
        self.assertEqual(state['latest']['completed'], 2)
        self.assertEqual([m['connection_verified'] for m in state['meters']], [True, False])
        self.assertIsNone(state['meters'][1]['last_success_at'])
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM bills').fetchone()[0], 2)

    def test_collection_session_failure_is_reported_once_and_preserves_last_success(self):
        from requests import Response
        from requests.exceptions import HTTPError
        self.login()
        previous = self.client.post('/api/collection', json={}).json()
        collection._run_job(previous['id'], threading.Event(), lambda *args, **kwargs:
                            {'status': 'success', 'message': '자료 확인', 'connection_verified': True})
        before = self.client.get('/api/collection').json()['meters']
        job = self.client.post('/api/collection', json={}).json()
        response = Response()
        response.status_code = 404
        error = HTTPError('secret must never reach clients', response=response)
        with patch('back.scripts.billing_etl.i121_crawler.auth.collection_session', side_effect=error) as login, \
             patch('back.pipelines.incremental.collect_meter') as collect, \
             patch.object(collection.logger, 'error') as log:
            collection.run_job(job['id'], threading.Event())
            login.assert_called_once()
            collect.assert_not_called()
            log.assert_called_once()
        state = self.client.get('/api/collection').json()
        self.assertIsNone(state['running'])
        self.assertEqual(state['latest']['status'], 'failed')
        self.assertEqual(state['latest']['completed'], 2)
        self.assertIn('실패 2개', state['latest']['message'])
        self.assertIn('HTTP 404', state['latest']['message'])
        self.assertNotIn('확인 완료', state['latest']['message'])
        self.assertNotIn('secret', json.dumps(state))
        self.assertTrue(all('연결 단계에서 중단' in item['message'] for item in state['latest']['items']))
        self.assertEqual([m['last_success_at'] for m in state['meters']], [m['last_success_at'] for m in before])
        with db.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM bills').fetchone()[0], 2)

    def test_collection_failure_messages_distinguish_causes(self):
        from requests import Response
        from requests.exceptions import ConnectionError, HTTPError, Timeout
        from back.scripts.billing_etl.i121_crawler.auth import ConfigurationError, LoginError
        # Only network trouble is retried automatically; a rejected login could lock the account.
        cases = [(ConfigurationError('private'), '설정', False), (LoginError('private'), '로그인', False),
                 (Timeout('private'), '응답 시간', True), (ConnectionError('private'), '네트워크', True)]
        for code in (401, 403, 404, 429, 503):
            response = Response()
            response.status_code = code
            cases.append((HTTPError('private', response=response), f'HTTP {code}', code in (429, 503)))
        for error, expected, retryable in cases:
            with self.subTest(error=type(error).__name__, expected=expected):
                result = collection.failure_result(error)
                self.assertIn(expected, result['message'])
                self.assertEqual(result['retryable'], retryable)
                self.assertNotIn('private', result['message'])
                self.assertIn('기존 자료는 보존', result['message'])

    def test_session_failure_leaves_restored_meter_in_its_new_queue(self):
        from back.scripts.billing_etl.i121_crawler.auth import LoginError
        self.login()
        job = self.client.post('/api/collection', json={}).json()

        def failed_login(*args):
            self.assertEqual(self.client.delete('/api/meters/000000002').status_code, 200)
            self.assertEqual(self.client.post('/api/meters/000000002/restore').status_code, 200)
            raise LoginError('session setup failed')

        with patch('back.scripts.billing_etl.i121_crawler.auth.collection_session', side_effect=failed_login):
            collection.run_job(job['id'], threading.Event())
        with db.connect() as c:
            states = [r[0] for r in c.execute('SELECT status FROM collection_items WHERE meter_id=? ORDER BY job_id', ('000000002',))]
            self.assertEqual(states, ['skipped', 'queued'])

    def test_deleting_and_restoring_queued_meter_cannot_run_it_twice(self):
        self.login()
        job = self.client.post('/api/collection', json={}).json()
        called = []

        def collect(meter, **kwargs):
            called.append(meter['id'])
            self.assertEqual(self.client.delete('/api/meters/000000002').status_code, 200)
            self.assertEqual(self.client.post('/api/meters/000000002/restore').status_code, 200)
            return {'status': 'empty', 'message': '조회 자료 없음'}

        collection._run_job(job['id'], threading.Event(), collect)
        self.assertEqual(called, ['000000001'])
        with db.connect() as c:
            states = [r[0] for r in c.execute('SELECT status FROM collection_items WHERE meter_id=? ORDER BY job_id', ('000000002',))]
            self.assertEqual(states, ['skipped', 'queued'])

    def test_risk_requires_observed_daily_water(self):
        self.login()
        daily, risk = main.catalog.data_sources()
        daily['000000001']['usage'][0]['value'] = 0
        daily['000000002'] = {'usage': [], 'ridership': [{'date': '2026-09-01', 'value': 100}]}
        risk['000000002'].append(risk['000000002'][0] | {'date': '2026-09-01'})
        for enabled, expected in [(0, 'billing_only'), (1, 'pending')]:
            with db.connect() as c:
                c.execute("UPDATE meters SET daily_enabled=? WHERE id='000000002'", (enabled,))
            payload = self.client.get('/api/data').json()
            modes = {m['id']: m['data_mode'] for m in payload['meters']}
            self.assertEqual(modes, {'000000001': 'daily', '000000002': expected})
            self.assertEqual(payload['stations']['000000002']['data_mode'], expected)
            self.assertEqual(payload['status']['counts'], {'daily': 1, 'billing_only': 1-enabled, 'pending': enabled})
            self.assertEqual(set(payload['risk']), {'000000001'})
            self.assertEqual(payload['status']['latest_risk'], '2026-08-31')
            self.assertEqual(len(payload['bills']['000000002']['bills']), 1)
            summary = self.client.get('/api/summary').json()
            self.assertEqual(summary['기준일'], '2026-08-31')
            self.assertEqual(summary['counts']['경고'], 1)
            self.assertEqual([item['meter_id'] for item in summary['items']], ['000000001'])
            self.assertEqual(self.client.get('/api/anomalies?date=2026-09-01').json()['items'], [])
            for route in ('analyze', 'alert'):
                self.assertEqual(self.client.post(f'/api/{route}', json={'meter_id': '000000002', 'date': '2026-09-01'}).status_code, 404)
            self.assertIsNone(data_access.analysis_context('000000002', '2026-09-01'))
            # A collected row without an actual value still is not daily water data.
            daily['000000002']['usage'] = [{'date': '2026-09-01', 'value': None}]

    def test_invalid_observation_is_separate_from_alerts_and_cannot_trigger_external_actions(self):
        self.login()
        _, risk = main.catalog.data_sources()
        risk['000000001'][0].update(actual=-1, residual=-11, z=-5, err=True)
        risk['000000002'][0].update(actual=3186, residual=3176, z=8, err=False)
        summary = self.client.get('/api/summary').json()
        self.assertEqual(summary['counts'], {'경고': 1, '주의': 0, '자료 확인': 1, '총': 2, '분석': 1})
        invalid = next(item for item in summary['items'] if item['meter_id'] == '000000001')
        self.assertEqual(invalid['심각도'], '자료 확인')
        self.assertTrue(invalid['likely_data_error'])
        anomaly = next(item for item in self.client.get('/api/anomalies').json()['items'] if item['meter_id'] == '000000001')
        self.assertEqual(anomaly['심각도'], '자료 확인')
        with patch.object(main.agent, 'analyze_cause') as analyze, patch.object(mailer, 'send_mail') as send:
            for route in ('analyze', 'alert'):
                result = self.client.post('/api/' + route, json={'meter_id': '000000001', 'date': '2026-08-31'})
                self.assertEqual(result.status_code, 422, result.text)
                self.assertIn('원자료', result.json()['detail'])
            analyze.assert_not_called()
            send.assert_not_called()
        risk['000000002'][0]['err'] = True
        only_errors = self.client.get('/api/summary').json()
        self.assertEqual(only_errors['counts']['분석'], 0)
        self.assertEqual(only_errors['counts']['자료 확인'], 2)

    def test_unchanged_remote_reading_is_a_data_check_with_its_reason(self):
        self.login()
        _, risk = main.catalog.data_sources()
        risk['000000001'][0].update(actual=0, pred=None, residual=None, z=None, severity='자료 확인', dir='', err=True,
                                    reason='값 그대로', since='2026-08-20', days=12, usual=10.5)
        summary = self.client.get('/api/summary').json()
        self.assertEqual(summary['counts']['자료 확인'], 1)
        item = next(item for item in summary['items'] if item['meter_id'] == '000000001')
        self.assertEqual((item['심각도'], item['reason'], item['since'], item['days'], item['usual'], item['pct']),
                         ('자료 확인', '값 그대로', '2026-08-20', 12, 10.5, None))
        self.assertEqual(next(item for item in summary['items'] if item['meter_id'] == '000000002')['reason'], '')
        self.assertEqual(self.client.get('/api/data').json()['risk']['000000001'][0]['reason'], '값 그대로')
        with patch.object(main.agent, 'analyze_cause') as analyze:
            result = self.client.post('/api/analyze', json={'meter_id': '000000001', 'date': '2026-08-31'})
            self.assertEqual(result.status_code, 422, result.text)
            analyze.assert_not_called()

    def test_inactive_meter_history_does_not_move_dashboard_reference_dates(self):
        self.login()
        daily, risk = main.catalog.data_sources()
        daily['000000001']['ridership'] = [{'date': '2026-08-30', 'value': 10}]
        daily['000000002']['usage'][0]['date'] = '2026-09-01'
        daily['000000002']['ridership'] = [{'date': '2026-09-01', 'value': 20}]
        risk['000000002'][0]['date'] = '2026-09-01'
        with db.connect() as c:
            c.execute('UPDATE meters SET active=0 WHERE id=?', ('000000002',))
        payload = self.client.get('/api/data').json()
        self.assertEqual(payload['status']['reference_date'], '2026-08-31')
        self.assertEqual(payload['status']['latest_usage'], '2026-08-31')
        self.assertEqual(payload['status']['latest_ridership'], '2026-08-30')
        self.assertEqual(payload['status']['latest_risk'], '2026-08-31')
        self.assertIn('000000002', payload['daily'])
        self.assertIn('000000002', payload['risk'])
        summary = self.client.get('/api/summary', params={'date': payload['status']['reference_date']}).json()
        self.assertEqual(summary['counts']['경고'], 1)

    def test_detailed_groundwater_bill_does_not_become_zero_water_usage(self):
        self.login()
        with db.connect() as c:
            c.execute('UPDATE bills SET payload=? WHERE meter_id=?', (json.dumps({
                '사용량': None, '총사용량': 0, '지하수사용량': 4384,
                '납부금액': 2104320, 'detail_available': True,
            }), '000000001'))
        row = self.client.get('/api/stats?meter_id=000000001').json()['rows'][0]
        self.assertIsNone(row['billed_usage_ton'])
        self.assertEqual(row['billed_won'], 2104320)

    def test_imported_missing_usage_matches_statistics_export_and_pdf(self):
        from back.api.import_data import import_details
        self.login()
        source = Path(self.temp.name) / 'missing-usage.csv'
        source.write_text('mkey,napgi,gubun,pay_amount_won,total_usage\n000000001,2026-09,정기분,,0\n', encoding='utf-8')
        with db.connect() as c:
            self.assertEqual(import_details(c, source)['imported'], 1)
        query = {'meter_id': '000000001', 'start': '2026-09-01', 'end': '2026-09-30'}
        row = self.client.get('/api/stats', params=query).json()['rows'][0]
        self.assertIsNone(row['billed_usage_ton'])
        response = self.client.get('/api/export.xlsx', params=query | {'kind': 'bills'})
        workbook = load_workbook(io.BytesIO(response.content), read_only=True)
        values = list(workbook.active.values)
        workbook.close()
        exported = dict(zip(values[0], values[1]))
        self.assertIsNone(exported['사용량'])
        self.assertEqual(exported['총사용량'], 0)
        self.assertFalse({'periodStart', 'periodEnd', 'summary_only', 'detail_available', '집계사용량_톤', '출처'} & set(exported))
        pdf = self.client.get('/api/bills/000000001:2026-09:정기분/pdf')
        text = ''.join(PdfReader(io.BytesIO(pdf.content)).pages[0].extract_text().split())
        self.assertIn('상·하수도사용량(톤)-', text)

    def test_invalid_model_response_uses_safe_failure_instead_of_react_objects(self):
        self.login()
        valid = {'primary_cause': '원자료와 운영 현황 확인이 필요합니다.', 'confidence': '낮음',
                 'recommendation': ['사용량 원자료를 확인하세요.', '밸브 개폐 기록을 확인하세요.'], 'reasons': ['관측값의 변동'],
                 'events': [{'title': '관련 정보', 'date': '2026-08-31', 'source': 'https://example.com/info'}]}
        service = Mock()
        with patch.object(config, 'OPENAI_API_KEY', 'mock'), patch.object(main.agent, 'OpenAI', return_value=service):
            for invalid in ({'confidence': {'level': 'low'}}, {'recommendation': [{'step': '원자료 확인'}]},
                            {'reasons': [{'text': '자료 확인'}]}, {'is_calendar_effect': 'false'},
                            {'events': [{'title': {'name': '행사'}, 'source': 'https://example.com'}]}):
                service.responses.create.return_value.output_text = json.dumps(valid | invalid, ensure_ascii=False)
                response = self.client.post('/api/analyze', json={'meter_id': '000000001'}).json()
                self.assertEqual(response['generated_by'], 'none')
                self.assertIn('error', response['analysis'])
            service.responses.create.return_value.output_text = json.dumps(valid, ensure_ascii=False)
            response = self.client.post('/api/analyze', json={'meter_id': '000000001'}).json()
            self.assertEqual(response['analysis'], valid)
            # A single sentence still shows, as a one-step list.
            service.responses.create.return_value.output_text = json.dumps(valid | {'recommendation': '원자료를 확인하세요.'}, ensure_ascii=False)
            response = self.client.post('/api/analyze', json={'meter_id': '000000001'}).json()
            self.assertEqual(response['analysis']['recommendation'], ['원자료를 확인하세요.'])
        sent = service.responses.create.call_args.kwargs['input']
        self.assertNotIn('000000001', sent)   # customer numbers never leave the application
        sent = json.loads(sent)
        self.assertEqual((len(sent['history']), sent['역'], sent['호선']), (28, '동부역', '2'))
        self.assertEqual(sent['history'][-1] | {'요일': None}, {'날짜': '2026-08-31', '요일': None, '사용량_톤': 20, '예측_톤': 10.0, '심각도': '경고', '승하차_명': None})

    def test_analysis_context_gives_usual_levels_for_the_same_weekday(self):
        from datetime import date as Date, timedelta
        days = [Date(2026, 7, 1) + timedelta(days=n) for n in range(62)]   # 2026-07-01 ~ 08-31
        usage = [{'date': d.isoformat(), 'value': 30 if d.weekday() == 5 else 10} for d in days if d != Date(2026, 8, 20)]
        riders = [{'date': d.isoformat(), 'value': 900 if d.weekday() == 5 else 300} for d in days]
        daily = {'000000001': {'usage': usage, 'ridership': riders}}
        risk = {'000000001': [{'date': '2026-08-29', 'actual': 30, 'pred': 12.34, 'residual': 17.66, 'z': 3, 'severity': '주의'},
                              {'date': '2026-08-27', 'actual': -5, 'pred': 10, 'residual': -15, 'z': 0, 'severity': '경고', 'err': True}]}
        with patch('back.api.catalog.data_sources', return_value=(daily, risk)):
            context = data_access.analysis_context('000000001', '2026-08-29')   # 토요일
        self.assertEqual(context['baseline'], {'같은_요일_지난_8주_사용량_중앙값': 30, '지난_28일_사용량_중앙값': 10,
                                               '같은_요일_지난_8주_승하차_중앙값': 900, '당일_승하차_명': 900})
        last = context['history'][-1]
        self.assertEqual((last['요일'], last['예측_톤'], last['심각도']), ('토', 12.3, '주의'))
        self.assertIsNone(next(r for r in context['history'] if r['날짜'] == '2026-08-20')['사용량_톤'])   # 빠진 날은 자료 없음
        invalid = next(r for r in context['history'] if r['날짜'] == '2026-08-27')   # 음수 검침은 사용량으로 넘기지 않는다
        self.assertEqual((invalid['사용량_톤'], invalid['심각도']), (None, '자료 확인'))

    def test_collection_fields_are_validated_and_existing_metadata_is_preserved(self):
        self.login()
        with db.connect() as c:
            c.execute('UPDATE meters SET metadata=? WHERE id=?', (json.dumps({'review_notes': {'note': 'keep'}}), '000000001'))
        result = self.client.patch('/api/meters/000000001', json={'daily_enabled': False,
                'metadata': {'arisu_customer_name': '  지하철 가역  ', '계량기번호': '123'}})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['metadata']['arisu_customer_name'], '지하철 가역')
        self.assertEqual(result.json()['metadata']['review_notes'], {'note': 'keep'})
        payload = self.client.get('/api/data').json()
        self.assertEqual(payload['stations']['000000001']['data_mode'], 'billing_only')
        self.assertNotIn('000000001', payload['daily'])
        self.assertNotIn('000000001', payload['risk'])
        self.assertEqual(len(payload['bills']['000000001']['bills']), 1)
        workbook = load_workbook(io.BytesIO(self.client.get('/api/export.xlsx?kind=usage').content), read_only=True)
        exported = list(workbook.active.values)
        workbook.close()
        self.assertEqual(len(exported), 2)
        self.assertEqual(exported[1][exported[0].index('고객번호')], '000000002')
        for invalid in ('가*역', '가＊역', 'bad\nname', 123):
            response = self.client.patch('/api/meters/000000001', json={'metadata': {'arisu_customer_name': invalid}})
            self.assertEqual(response.status_code, 422)


if __name__ == '__main__':
    unittest.main()
