"""One-contract refresh checks using temporary storage and mocked remote responses."""
import json
import os
import sqlite3
import tempfile
import time
import unittest
import csv
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from back.api import catalog, config, db
from back.pipelines import incremental as inc
from back.pipelines.daily_water import scraper as water
from back.pipelines.refresh import publish_data
from back.pipelines import refresh
from back.scripts.billing_etl.i121_crawler.auth import ConfigurationError, LoginError, NotRemoteMeter, RegistrationRefused


class IncrementalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = patch.multiple(config, ROOT=self.root, DATA_DIR=self.root / 'runtime', HISTORY_DIR=self.root / 'history',
                                     APP_DB_PATH=self.root / 'app.sqlite3')
        self.config.start()
        self.clock = patch.object(inc, 'today', return_value=date(2026, 6, 3))
        self.clock.start()
        db.init_db()
        with db.connect() as conn:
            conn.execute("INSERT INTO offices(id,name) VALUES('office','사업소')")
            conn.execute("INSERT INTO stations(id,name) VALUES('station','가역')")
            conn.execute("""INSERT INTO meters(id,provider,customer_number,station_id,office_id,display_name,created_at,updated_at)
                            VALUES('meter','arisu','000000001','station','office','가역','now','now')""")
            conn.execute('UPDATE meters SET daily_enabled=1')
            self.meter = catalog.list_meters(conn)[0]
        self.session = patch.object(inc, 'collection_session', return_value=Mock())
        self.session.start()
        self.smart = patch.object(inc, 'fetch_smart_status', return_value=None)
        self.smart.start()

    def tearDown(self):
        self.smart.stop()
        self.session.stop()
        self.clock.stop()
        self.config.stop()
        self.tmp.cleanup()

    def row(self, day='2026-06-01', value=10):
        return {'고객번호': '000000001', '역명': '가역', '사용일': day, '일사용량(톤)': value}

    def bill(self):
        return {'mkey': '000000001', 'napgi': '2026-06-30', 'napgi_compact': '20260630',
                'gubun': '정기분', 'total_usage_ton': 12, 'bugwa_amount_won': 500, 'address': '서울 가역'}

    def test_smart_status_updates_without_changing_model_observations(self):
        smart = {'customer_number': '000000001', 'leak_status': '주의', 'meter_status': '계량기 장애',
                 'realtime_usage': 0, 'updated_at': '2026-06-03 00:00', 'collected_at': '2026-06-03T08:00:00+09:00'}
        with patch.object(inc, 'fetch_smart_status', return_value=smart), \
                patch.object(inc, 'fetch_month', return_value=[]) as fetch, \
                patch.object(inc, 'collect_bills', return_value={'rows': [], 'errors': []}):
            result = inc.collect_meter(self.meter)
        self.assertEqual(result['status'], 'empty')   # the card alone is not a usage or bill result
        self.assertEqual(catalog.payload()['arisu']['meter'], smart)
        self.assertEqual(catalog.data_sources()[0], {})
        self.assertFalse((config.DATA_DIR / 'snapshot/manifest.json').exists())
        self.assertEqual(fetch.call_args.kwargs['archive_dir'], config.DATA_DIR / 'raw/remote_meter')
        before = (config.DATA_DIR / 'arisu/000000001.json').read_bytes()
        with patch.object(inc, 'fetch_smart_status', side_effect=TimeoutError), \
                patch.object(inc, 'fetch_month', return_value=[self.row()]), \
                patch.object(inc, 'collect_bills', return_value={'rows': [], 'errors': []}):
            result = inc.collect_meter(self.meter)
        # A failed card is a note: it never fails, retries or mails the daily usage and bill run.
        self.assertEqual(result['status'], 'success')
        self.assertFalse(result['retryable'])
        self.assertIn('아리수 누수위험을 확인하지 못했습니다', result['message'])
        self.assertEqual((config.DATA_DIR / 'arisu/000000001.json').read_bytes(), before)
        self.assertEqual(catalog.data_sources()[0]['000000001']['usage'], [{'date': '2026-06-01', 'value': 10.0}])

    def test_unlisted_customer_does_not_fetch_smart_status(self):
        with patch.object(inc, 'fetch_smart_status') as smart, \
                patch.object(inc, 'collect_bills', return_value={'rows': [], 'errors': []}):
            inc.collect_meter(self.meter, session=Mock(arisu_customer_numbers=frozenset({'000000002'})))
        smart.assert_not_called()

    def test_new_daily_enabled_meter_is_probed_published_and_repeated_safely(self):
        messages = []
        with patch.object(inc, 'fetch_month', side_effect=lambda _s, _m, month, **_: [self.row()] if month == '2026-06' else []) as fetch:
            with patch.object(inc, 'collect_bills', return_value={'rows': [self.bill()], 'errors': []}):
                first = inc.collect_meter(self.meter, progress=messages.append)
                self.assertEqual(fetch.call_count, 12)
                second = inc.collect_meter(self.meter)
        self.assertEqual(first['status'], 'success')
        self.assertTrue(first['connection_verified'])
        self.assertTrue(second['daily_available'])
        self.assertTrue(messages)
        daily, _ = catalog.data_sources()
        self.assertEqual(daily['000000001']['usage'], [{'date': '2026-06-01', 'value': 10.0}])
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM bills').fetchone()[0], 1)
            row = conn.execute('SELECT daily_enabled,address FROM meters').fetchone()
            self.assertEqual(tuple(row), (1, '서울 가역'))

    def test_empty_refresh_preserves_existing_data_and_does_not_claim_invalid_customer(self):
        with patch.object(inc, 'fetch_month', return_value=[self.row()]), patch.object(inc, 'collect_bills', return_value={'rows': [self.bill()], 'errors': []}):
            inc.collect_meter(self.meter)
        manifest = config.DATA_DIR / 'snapshot/manifest.json'
        before = manifest.read_bytes()
        with patch.object(inc, 'fetch_month', return_value=[]), patch.object(inc, 'collect_bills', return_value={'rows': [], 'errors': []}):
            result = inc.collect_meter(self.meter)
        self.assertEqual(result['status'], 'empty')
        self.assertFalse(result['connection_verified'])
        self.assertTrue(result['daily_available'])
        self.assertIn('볼 수 없는 고객번호일 수 있습니다', result['message'])
        self.assertEqual(manifest.read_bytes(), before)
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM bills').fetchone()[0], 1)
            self.assertEqual(conn.execute('SELECT daily_enabled FROM meters').fetchone()[0], 1)

    def test_partial_success_survives_other_month_and_bill_failure(self):
        def fetch(_session, _meter, month, **kwargs):
            if month == '2026-06':
                raise TimeoutError()
            return [self.row('2026-05-31')] if month == '2026-05' else []
        with patch.object(inc, 'fetch_month', side_effect=fetch), patch.object(inc, 'collect_bills', side_effect=TimeoutError()):
            result = inc.collect_meter(self.meter)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['daily_rows'], 1)
        self.assertTrue(result['connection_verified'])
        self.assertTrue(result['retryable'])
        self.assertEqual(catalog.data_sources()[0]['000000001']['usage'][0]['date'], '2026-05-31')

    def test_configuration_and_login_failures_are_distinct(self):
        for error, status, message in [(ConfigurationError(), 'configuration_required', '설정'),
                                       (LoginError(), 'failed', '로그인')]:
            with patch.object(inc, 'collection_session', side_effect=error):
                result = inc.collect_meter(self.meter)
            self.assertEqual(result['status'], status)
            self.assertIn(message, result['message'])
            self.assertFalse(result['connection_verified'])
            self.assertFalse(result['retryable'])

    def test_only_network_failures_are_retryable_and_rejected_login_blocks_retry(self):
        timeout = {'rows': [], 'errors': [{'start': '2026-05', 'error': 'ReadTimeout'}]}
        name = {'rows': [], 'errors': [{'start': '2026-05', 'error': 'PublicCustomerNameError'}]}
        listed = frozenset({'000000001'})
        cases = [('login connection', Mock(arisu_customer_numbers=frozenset(), arisu_login_error='ConnectionError'),
                  {'rows': [self.bill()], 'errors': []}, True, '아리수 서버에 연결하지 못했습니다'),
                 # Retrying would log in again with rejected credentials, even though the bills timed out.
                 ('login rejected', Mock(arisu_customer_numbers=frozenset(), arisu_login_error='LoginError'), timeout, False, '로그인을 확인'),
                 ('bill timeout', Mock(arisu_customer_numbers=listed, arisu_login_error=None), timeout, True, '청구 내역 조회에 실패'),
                 ('customer name', Mock(arisu_customer_numbers=listed, arisu_login_error=None), name, False, '성명을 확인')]
        for label, session, bills, retryable, message in cases:
            with self.subTest(label):
                with patch.object(inc, 'fetch_month', return_value=[]), patch.object(inc, 'collect_bills', return_value=bills):
                    result = inc.collect_meter(self.meter, session=session)
                self.assertIn(result['status'], ('failed', 'partial'))
                self.assertEqual(result['retryable'], retryable)
                self.assertIn(message, result['message'])
        # A billing-only meter without a stored bill name needs the login for its bills as well.
        with db.connect() as conn:
            conn.execute('UPDATE meters SET daily_enabled=0')
            meter = catalog.list_meters(conn)[0]
        with patch.object(inc, 'fetch_month') as water, patch.object(inc, 'collect_bills',
                return_value={'rows': [], 'errors': [{'start': '2026-05', 'error': 'CustomerAccessError'}]}):
            result = inc.collect_meter(meter, session=Mock(arisu_customer_numbers=frozenset(), arisu_login_error='ConnectTimeout'))
        water.assert_not_called()
        self.assertEqual((result['status'], result['retryable']), ('failed', True))

    def test_failed_bill_window_is_retried_even_when_existing_months_are_present(self):
        with patch.object(inc, 'fetch_month', return_value=[]), patch.object(inc, 'collect_bills',
                return_value={'rows': [self.bill()], 'errors': [{'start': '2025-07', 'error': 'ValueError'}]}):
            result = inc.collect_meter(self.meter)
        self.assertEqual(result['status'], 'partial')
        with patch.object(inc, 'fetch_month', return_value=[]), patch.object(inc, 'collect_bills',
                return_value={'rows': [self.bill()], 'errors': []}) as collect:
            inc.collect_meter(self.meter)
        self.assertEqual(collect.call_args.kwargs['start_ym'], '2025-07')
        state = json.loads((config.DATA_DIR / 'collection/000000001.json').read_text())
        self.assertIsNone(state['bill_retry_from'])

    def test_batch_session_is_reused_and_owned_by_caller(self):
        session = Mock()
        with patch.object(inc, 'collection_session', side_effect=AssertionError('must reuse session')):
            with patch.object(inc, 'fetch_month', return_value=[]), patch.object(inc, 'collect_bills', return_value={'rows': [], 'errors': []}):
                result = inc.collect_meter(self.meter, session=session)
        self.assertEqual(result['status'], 'empty')
        session.close.assert_not_called()

    def test_unregistered_billing_only_customer_skips_daily_and_collects_bills(self):
        session = Mock(arisu_customer_numbers=frozenset({'000000002'}))
        meter = self.meter | {'daily_enabled': False, 'metadata': {'arisu_customer_name': '가역'}}
        with patch.object(inc, 'fetch_month') as water, patch.object(inc, 'collect_bills', return_value={'rows': [self.bill()], 'errors': []}) as bills:
            result = inc.collect_meter(meter, session=session)
        self.assertEqual(result['status'], 'success')
        self.assertIn('청구 전용', result['message'])
        self.assertTrue(result['connection_verified'])
        water.assert_not_called()
        self.assertEqual(bills.call_args.kwargs['customer_names'], {'000000001': '가역'})
        self.assertFalse((config.DATA_DIR / 'snapshot/manifest.json').exists())

    def test_unregistered_daily_customer_still_saves_public_bills(self):
        session = Mock(arisu_customer_numbers=frozenset({'000000002'}))
        daily_enabled = lambda: sqlite_value("SELECT daily_enabled FROM meters WHERE id='meter'")

        def sqlite_value(sql):
            with db.connect() as conn:
                return conn.execute(sql).fetchone()[0]

        def collect(with_session):
            with patch.object(inc, 'fetch_month', return_value=[]) as water, patch.object(inc, 'collect_bills', return_value={'rows': [self.bill()], 'errors': []}):
                return inc.collect_meter(self.meter, session=with_session), water

        # A customer list that arrives empty is a failed lookup, never proof that every meter left the account.
        for _ in range(3):
            result, water = collect(Mock(arisu_customer_numbers=frozenset()))
            self.assertEqual((result['status'], daily_enabled()), ('partial', 1))
            self.assertIn('고객 목록을 받지 못', result['message'])
            water.assert_not_called()
        # One list without this customer could be a partial response: warn once, keep the setting.
        result, water = collect(session)
        self.assertEqual((result['status'], daily_enabled()), ('partial', 1))
        self.assertIn('다음 수집에서도 없으면', result['message'])
        water.assert_not_called()
        # Seeing the customer again clears that warning, so the next miss counts as the first one.
        result, water = collect(Mock(arisu_customer_numbers=frozenset({'000000001'})))
        self.assertEqual((result['status'], daily_enabled()), ('success', 1))
        water.assert_called()
        self.assertEqual((collect(session)[0]['status'], daily_enabled()), ('partial', 1))
        with patch.object(inc, 'fetch_month') as water, patch.object(inc, 'collect_bills', return_value={'rows': [self.bill()], 'errors': []}):
            result = inc.collect_meter(self.meter, session=session)
        # missing from two lists in a row: the customer leaves daily monitoring instead of failing on every run; bills keep coming
        self.assertEqual(result['status'], 'success')
        self.assertIn('일일 관제에서 제외', result['message'])
        self.assertEqual(result['bill_rows'], 1)
        water.assert_not_called()
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM bills').fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT daily_enabled FROM meters WHERE id='meter'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT action FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()[0], 'meter_daily_disabled')
        login_failed = Mock(arisu_customer_numbers=frozenset(), arisu_login_error='login')
        with db.connect() as conn:
            conn.execute('UPDATE meters SET daily_enabled=1')
        with patch.object(inc, 'fetch_month'), patch.object(inc, 'collect_bills', return_value={'rows': [self.bill()], 'errors': []}):
            result = inc.collect_meter(self.meter, session=login_failed)
        self.assertEqual(result['status'], 'partial')   # a login problem is not a reason to drop the meter
        with db.connect() as conn:
            self.assertEqual(conn.execute("SELECT daily_enabled FROM meters WHERE id='meter'").fetchone()[0], 1)

    def registering(self):
        """A named daily meter the account does not list yet, and one collection of it."""
        meter = self.meter | {'metadata': {'arisu_customer_name': '가역'}}
        session = Mock(arisu_customer_numbers=frozenset({'000000002'}), arisu_login_error=None)

        def collect(with_meter=meter, remote=True):
            with patch.object(inc, 'fetch_month', side_effect=lambda _s, _m, month, **_: [self.row()] if month == '2026-06' else []) as water, \
                    patch.object(inc, 'collect_bills', return_value={'rows': [self.bill()], 'errors': []}), \
                    patch.object(inc, 'remote_read', return_value=remote):   # what Arisu's record says about how the meter is read
                return inc.collect_meter(with_meter, session=session), water
        return meter, session, collect

    def checkpoint(self):
        return json.loads((config.DATA_DIR / 'collection/000000001.json').read_text())

    def daily_enabled(self):
        with db.connect() as conn:
            return conn.execute("SELECT daily_enabled FROM meters WHERE id='meter'").fetchone()[0]

    def test_meter_not_read_remotely_keeps_bills_only_without_registering_or_waiting(self):
        meter, session, collect = self.registering()

        def reasons():
            with db.connect() as conn:
                return [json.loads(r[0])['reason'] for r in conn.execute("SELECT details FROM audit_log WHERE action='meter_daily_disabled'")]

        # Not in the account: Arisu's record says there is no remote meter, so one run settles it and nothing is registered.
        # That holds with registration switched off or on, and when only the registration's own check finds it out.
        for relation, remote, refusal in (('', False, None), ('9', False, None), ('9', None, NotRemoteMeter('원격검침 계량기가 아닙니다'))):
            with self.subTest(relation=relation, remote=remote):
                with db.connect() as conn:
                    conn.execute('UPDATE meters SET daily_enabled=1')
                    conn.execute('DELETE FROM audit_log')
                with patch.object(config, 'ARISU_REGISTER_RELATION', relation), patch.object(inc, 'register_customer', side_effect=refusal) as asked:
                    result, water = collect(remote=remote)
                self.assertEqual(asked.call_count, 1 if refusal else 0)
                self.assertEqual((result['status'], self.daily_enabled(), reasons()), ('success', 0, ['원격검침 계량기가 아님']))
                self.assertIn('원격검침 계량기가 아니라 일일 사용량이 없습니다. 청구서만 수집하도록 바꿨습니다', result['message'])
                self.assertIn('청구 전용 계량기', result['message'])
                self.assertNotIn('다음 수집에서도', result['message'])
                water.assert_not_called()
                self.assertEqual((self.checkpoint()['unlisted_runs'], self.checkpoint()['register_refused']), (0, None))
        # In the account but never read: the same record is asked once the month queries come back empty.
        with db.connect() as conn:
            conn.execute('UPDATE meters SET daily_enabled=1')
            conn.execute('DELETE FROM audit_log')
        listed = Mock(arisu_customer_numbers=frozenset({'000000001'}), arisu_login_error=None)

        def empty(**answer):
            with patch.object(inc, 'fetch_month', return_value=[]), patch.object(inc, 'collect_bills', return_value={'rows': [self.bill()], 'errors': []}), \
                    patch.object(inc, 'remote_read', **answer) as asked:
                return inc.collect_meter(meter, session=listed), asked

        for answer in ({'return_value': True}, {'return_value': None}, {'side_effect': ConnectionError('private')}):
            result, asked = empty(**answer)   # a remote meter with no reading yet, or no answer: it keeps waiting
            self.assertEqual((asked.call_count, self.daily_enabled(), reasons()), (1, 1, []))
            self.assertNotIn('private', result['message'])
        result, asked = empty(return_value=False)
        self.assertEqual((asked.call_args.args[1:], self.daily_enabled(), reasons()), (('000000001', '가역'), 0, ['원격검침 계량기가 아님']))
        self.assertIn('청구서만 수집하도록 바꿨습니다', result['message'])
        # A meter that has given readings is never second-guessed, even in a month without any.
        with db.connect() as conn:
            conn.execute('UPDATE meters SET daily_enabled=1')
        with patch.object(inc, 'remote_read') as asked, patch.object(inc, 'collect_bills', return_value={'rows': [], 'errors': []}):
            with patch.object(inc, 'fetch_month', side_effect=lambda _s, _m, month, **_: [self.row()] if month == '2026-06' else []):
                inc.collect_meter(meter, session=listed)
            with patch.object(inc, 'fetch_month', return_value=[]):
                inc.collect_meter(meter, session=listed)
        asked.assert_not_called()
        self.assertEqual(self.daily_enabled(), 1)

    def test_another_members_link_is_released_once_and_a_partial_list_registers_nothing(self):
        meter, session, collect = self.registering()
        leave = []   # whether each request came with leave to remove another member's link

        def register(outcome):
            def run(with_session, customer, name, relation, release):
                leave.append(release)
                with_session.arisu_customer_numbers = frozenset({'000000001', '000000002'})
                return outcome
            return run

        def registrations():
            with db.connect() as conn:
                return [json.loads(r[0]) for r in conn.execute("SELECT details FROM audit_log WHERE action='meter_arisu_registered'")]

        with patch.object(config, 'ARISU_REGISTER_RELATION', '9'):
            # A fresh list already had the customer: the list from login was partial, and nothing was registered.
            with patch.object(inc, 'register_customer', side_effect=register('listed')):
                result, water = collect()
            self.assertEqual(result['status'], 'success')
            self.assertNotIn('등록했습니다', result['message'])
            water.assert_called()
            self.assertEqual((registrations(), self.checkpoint()['released']), ([], False))
            # Held by another member: their link is removed for the registration, and that is on record.
            session.arisu_customer_numbers = frozenset({'000000002'})
            with patch.object(inc, 'register_customer', side_effect=register('released')):
                result, water = collect()
            self.assertIn('아리수 계정에 고객번호를 등록했습니다(다른 회원의 기존 연결은 해제했습니다)', result['message'])
            water.assert_called()
            self.assertEqual((registrations(), self.checkpoint()['released']), ([{'released_other_member': True}], True))
            # If that member registers the number again, it is not taken from them a second time: a person decides.
            session.arisu_customer_numbers = frozenset({'000000002'})
            with patch.object(inc, 'register_customer', side_effect=RegistrationRefused('다른 회원에게 등록된 고객번호입니다.')) as asked:
                result, water = collect()
            self.assertEqual(asked.call_args.kwargs, {'release': False})
            self.assertIn('등록하지 못했습니다(다른 회원에게 등록된 고객번호입니다)', result['message'])
            water.assert_not_called()
            self.assertEqual((len(registrations()), self.checkpoint()['released'], self.daily_enabled()), (1, True, 1))
        self.assertEqual(leave, [True, True])

    def test_missing_daily_customer_is_registered_once_then_collected(self):
        meter, session, collect = self.registering()

        def register(with_session, customer, name, relation, release):
            self.assertEqual((customer, name, relation, release), ('000000001', '가역', '9', True))
            with_session.arisu_customer_numbers = frozenset({'000000001', '000000002'})
            return 'registered'

        # Off unless the server names the relation to declare: the earlier warning stays as it was.
        for relation in ('', '12'):
            with patch.object(config, 'ARISU_REGISTER_RELATION', relation), patch.object(inc, 'register_customer') as asked:
                result, _ = collect(meter | {'customer_number': '000000001'})
            asked.assert_not_called()
            self.assertIn('고객 목록에 이 고객번호가 없습니다', result['message'])
            (config.DATA_DIR / 'collection/000000001.json').unlink()
        with patch.object(config, 'ARISU_REGISTER_RELATION', '9'), patch.object(inc, 'register_customer', side_effect=register) as asked:
            # no bill name, nothing to verify the customer with
            self.assertIn('고객 목록에 이 고객번호가 없습니다', collect(self.meter)[0]['message'])
            asked.assert_not_called()
            (config.DATA_DIR / 'collection/000000001.json').unlink()
            result, water = collect()
            self.assertEqual((result['status'], asked.call_count), ('success', 1))
            self.assertIn('아리수 계정에 고객번호를 등록했습니다', result['message'])
            water.assert_called()
            collect()   # listed now: nothing more to ask
            self.assertEqual(asked.call_count, 1)
        state = self.checkpoint()
        self.assertEqual((self.daily_enabled(), state['unlisted_runs'], state['register_refused'], state['released']), (1, 0, None, False))
        with db.connect() as conn:
            self.assertEqual([(r[0], json.loads(r[1])) for r in conn.execute("SELECT action,details FROM audit_log WHERE action LIKE 'meter_%'")],
                             [('meter_arisu_registered', {'released_other_member': False})])

    def test_refused_registration_is_reported_and_asked_again_only_after_a_change(self):
        meter, session, collect = self.registering()
        refusal = RegistrationRefused('고객번호와 사용자명이 불일치합니다.')
        with patch.object(config, 'ARISU_REGISTER_RELATION', '9'), patch.object(inc, 'register_customer', side_effect=refusal) as asked:
            result, water = collect()
            self.assertEqual((result['status'], asked.call_count, self.daily_enabled()), ('partial', 1, 1))
            self.assertIn('등록하지 못했습니다(고객번호와 사용자명이 불일치합니다). 다음 수집에서도 계정에 없으면', result['message'])
            self.assertFalse(result['retryable'])
            water.assert_not_called()
            self.assertEqual(self.checkpoint()['register_refused'], '가역')
            # The same name is not asked twice; the second miss ends daily monitoring as before.
            result, _ = collect()
            self.assertEqual((asked.call_count, self.daily_enabled()), (1, 0))
            self.assertIn('등록되지 않은 고객번호라 일일 관제에서 제외', result['message'])
            # Switching daily collection back on is a new request, and so is a corrected name.
            result, _ = collect()
            self.assertEqual(asked.call_count, 2)
            self.assertIn('등록하지 못해(고객번호와 사용자명이 불일치합니다) 일일 관제에서 제외', result['message'])
            self.assertIsNone(self.checkpoint()['register_refused'])
        (config.DATA_DIR / 'collection/000000001.json').unlink()
        with patch.object(config, 'ARISU_REGISTER_RELATION', '9'), patch.object(inc, 'register_customer', side_effect=refusal) as asked:
            collect()
            collect(meter | {'metadata': {'arisu_customer_name': '가역장'}})
            self.assertEqual([call.args[2] for call in asked.call_args_list], ['가역', '가역장'])

    def test_registration_that_could_not_be_asked_is_retried_without_counting_a_miss(self):
        _, session, collect = self.registering()
        for error, retryable, message in ((ConnectionError('private'), True, '다음 수집 때 다시 시도'), (ValueError('private'), False, '다음 수집 때 다시 시도'),
                                          (LoginError('private'), False, '인증이 만료')):
            with self.subTest(type(error).__name__), patch.object(config, 'ARISU_REGISTER_RELATION', '9'), \
                    patch.object(inc, 'register_customer', side_effect=error) as asked:
                for attempt in (1, 2, 3):
                    result, water = collect()
                    self.assertEqual((asked.call_count, result['status'], result['retryable']), (attempt, 'partial', retryable))
                    self.assertIn(message, result['message'])
                    self.assertNotIn('private', result['message'])
                    self.assertNotIn('고객 목록에 이 고객번호가 없습니다', result['message'])
                    water.assert_not_called()
                self.assertEqual((self.daily_enabled(), self.checkpoint()['unlisted_runs'], self.checkpoint()['register_refused']), (1, 0, None))
        # An empty list or a failed login is no ground for registering anything.
        for unusable in (Mock(arisu_customer_numbers=frozenset(), arisu_login_error=None),
                         Mock(arisu_customer_numbers=frozenset(), arisu_login_error='LoginError')):
            with patch.object(config, 'ARISU_REGISTER_RELATION', '9'), patch.object(inc, 'register_customer') as asked, \
                    patch.object(inc, 'fetch_month', return_value=[]), patch.object(inc, 'collect_bills', return_value={'rows': [self.bill()], 'errors': []}):
                inc.collect_meter(self.meter | {'metadata': {'arisu_customer_name': '가역'}}, session=unusable)
            asked.assert_not_called()

    def test_billing_gap_windows_do_not_recrawl_fifteen_stored_years(self):
        with db.connect() as conn:
            for ym in ['2010-11', '2012-01', *[f'{year}-{month:02d}' for year in range(2012, 2027) for month in range(1, 13) if f'{year}-{month:02d}' <= '2026-05']]:
                conn.execute("INSERT OR IGNORE INTO bills(id,meter_id,period,gubun,payload,source) VALUES(?, 'meter', ?, '정기분', '{}', 'test')", (ym, ym))
        self.assertEqual(inc._bill_windows('meter', '2026-09'), [('2026-06', '2026-09'), ('2010-12', '2011-12')])

    def test_monthly_bill_gap_is_found_even_with_supplementary_notice(self):
        with db.connect() as conn:
            for ym in ['2025-01', '2025-02', '2025-04', '2025-05', '2025-06']:
                conn.execute("INSERT INTO bills(id,meter_id,period,gubun,payload,source) VALUES(?, 'meter', ?, '정기분', '{}', 'test')", (ym, ym))
            conn.execute("INSERT INTO bills(id,meter_id,period,gubun,payload,source) VALUES('extra', 'meter', '2025-03', '수시분', '{}', 'test')")
        self.assertIn(('2025-03', '2025-03'), inc._bill_windows('meter', '2026-09'))
        with db.connect() as conn:
            conn.execute("DELETE FROM bills WHERE period IN ('2025-02', '2025-04', '2025-06')")
            conn.execute("INSERT INTO bills(id,meter_id,period,gubun,payload,source) VALUES('regular', 'meter', '2025-03', '정기분', '{}', 'test')")
        windows = inc._bill_windows('meter', '2026-09')
        self.assertFalse(any(start <= '2025-02' <= end or start <= '2025-04' <= end for start, end in windows))

    def test_refresh_cli_preserves_blank_and_zero_prefixed_notice_ids(self):
        path = config.DATA_DIR / 'raw/billing_i121/i121_bills/bills_long.csv'
        path.parent.mkdir(parents=True)
        rows = [self.bill() | {'notice_number': ''},
                self.bill() | {'gubun': '수시분', 'notice_number': '090000001'}]
        with path.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        # The collector's raw CSV is the boundary under test; no request or model work.
        import pandas as pd
        old = pd.DataFrame(columns=['고객번호', '납기일', '역명', '영업사업소', '용도'])
        with patch('back.scripts.billing_etl.crawl_bills.collect_bills', return_value={'errors': []}), \
             patch.object(refresh, '_read_csv', return_value=old), \
             patch('back.scripts.dataset_etl.build_clean_dataset.build_bills_bimonthly', return_value=pd.DataFrame()):
            for _ in range(2):
                refresh.refresh_bills(date(2026, 6, 1), date(2026, 6, 30), [self.meter], config.DATA_DIR)
        with db.connect() as conn:
            stored = [tuple(row) for row in conn.execute('SELECT gubun,notice_number FROM bills ORDER BY gubun')]
        self.assertEqual(stored, [('수시분', '090000001'), ('정기분', '')])

    def test_database_failure_retains_successfully_fetched_bill_window_for_retry(self):
        with patch.object(inc, 'fetch_month', return_value=[]), patch.object(inc, 'collect_bills', return_value={'rows': [self.bill()], 'errors': []}), patch.object(inc, 'upsert_bill_summaries', side_effect=sqlite3.OperationalError('locked')):
            result = inc.collect_meter(self.meter)
        self.assertEqual(result['status'], 'partial')
        state = json.loads((config.DATA_DIR / 'collection/000000001.json').read_text())
        self.assertEqual(state['bill_retry_windows'], [['2025-07', '2026-06']])

    def test_history_gaps_retry_weekly_and_recent_dates_always_retry(self):
        months = inc.water_months(['2026-01-01', '2026-06-01'], date(2026, 6, 2),
                                  {'2026-01': '2026-06-01', '2026-02': '2026-05-01'})
        self.assertNotIn('2026-01', months)
        self.assertIn('2026-02', months)
        self.assertIn('2026-06', months)
        months = inc.water_months(['2026-08-01'], date(2026, 9, 22), {'2026-08': '2026-09-22'})
        self.assertIn('2026-08', months)

    def test_nan_observation_never_publishes_or_verifies_connection(self):
        with patch.object(inc, 'fetch_month', return_value=[self.row(value=float('nan'))]), patch.object(inc, 'collect_bills', return_value={'rows': [], 'errors': []}):
            result = inc.collect_meter(self.meter)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['connection_verified'])
        self.assertFalse((config.DATA_DIR / 'snapshot/manifest.json').exists())

    def test_invalid_source_day_does_not_discard_valid_large_reading(self):
        def fetch(_session, _meter, month, *, issues, **kwargs):
            if month != '2026-06':
                return []
            issues.append('2026-06-02')
            return [self.row(value=3186)]
        with patch.object(inc, 'fetch_month', side_effect=fetch), patch.object(inc, 'collect_bills', return_value={'rows': [], 'errors': []}):
            result = inc.collect_meter(self.meter)
        self.assertEqual(result['status'], 'success')   # a gap in Arisu's own readings is a notice, not a failure
        self.assertEqual(result['daily_rows'], 1)
        self.assertIn('2026-06-02 아리수 검침값 누락', result['message'])
        self.assertEqual(catalog.data_sources()[0]['000000001']['usage'], [{'date': '2026-06-01', 'value': 3186.0}])
        state = json.loads((config.DATA_DIR / 'collection/000000001.json').read_text())
        self.assertEqual(state['water_excluded'], ['2026-06-02'])
        # the same source gap is remembered and not reported again
        with patch.object(inc, 'fetch_month', side_effect=fetch), patch.object(inc, 'collect_bills', return_value={'rows': [], 'errors': []}):
            again = inc.collect_meter(self.meter)
        self.assertNotIn('누락', again['message'])
        self.assertNotEqual(again['status'], 'partial')
        # once the source supplies the day, it is an ordinary observation and the memory is cleared
        def fixed(_session, _meter, month, *, issues, **kwargs):
            return [self.row(value=3186), self.row(day='2026-06-02', value=12)] if month == '2026-06' else []
        with patch.object(inc, 'fetch_month', side_effect=fixed), patch.object(inc, 'collect_bills', return_value={'rows': [], 'errors': []}):
            inc.collect_meter(self.meter)
        self.assertEqual(json.loads((config.DATA_DIR / 'collection/000000001.json').read_text())['water_excluded'], [])

    def test_source_gaps_without_any_usable_reading_are_a_failure(self):
        def broken(_session, _meter, month, *, issues, **kwargs):
            if month == '2026-06':
                issues.append('2026-06-04')
            return []
        with patch.object(inc, 'fetch_month', side_effect=broken), patch.object(inc, 'collect_bills', return_value={'rows': [], 'errors': []}):
            result = inc.collect_meter(self.meter)
        # with no usable reading at all the source itself may be broken: a failure that is mailed, not a notice
        self.assertEqual((result['status'], result['retryable']), ('failed', False))
        self.assertIn('2026-06-04 아리수 검침값 누락', result['message'])

    def test_model_bill_baseline_adds_collected_bills_to_the_historical_export(self):
        path = config.DATA_DIR / 'raw/billing_i121/i121_bills/bills_long.csv'
        path.parent.mkdir(parents=True)
        rows = [{'mkey': '000000001', 'napgi': '2026-08-31', 'gubun': '정기분', 'notice_number': '', 'total_usage_ton': 60, 'bugwa_amount_won': 900},
                {'mkey': '000000001', 'napgi': '2026-09-15', 'gubun': '수시분', 'notice_number': '090000001', 'total_usage_ton': 5, 'bugwa_amount_won': 50},
                {'mkey': '000000002', 'napgi': '2026-08-31', 'gubun': '정기분', 'notice_number': '', 'total_usage_ton': 70, 'bugwa_amount_won': 990},
                # a collected row whose detail failed carries no usage and must not replace the stored bill
                {'mkey': '000000001', 'napgi': '2026-06-30', 'gubun': '정기분', 'notice_number': '', 'total_usage_ton': '', 'bugwa_amount_won': ''}]
        with path.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        import pandas as pd
        old = pd.DataFrame([{'고객번호': '000000001', '역명': '가역', '영업사업소': '사업소', '용도': '', '납기일': '2026-06-30',
                             '총사용량_톤': 40, '부과금액_원': 600}])
        other = {'id': 'other', 'customer_number': 'GM-2026-0001', 'provider': 'other', 'station_name': '기타역'}
        with patch.object(refresh, '_read_csv', return_value=old):
            refresh.build_model_bills([self.meter, other], config.DATA_DIR)   # another provider's free-text number is skipped
        built = pd.read_csv(config.DATA_DIR / 'processed/water_bills.csv', encoding='utf-8-sig', dtype={'고객번호': str})
        # the stored bill and the newly collected one; supplementary notices and unregistered customers stay out
        self.assertEqual(built[['고객번호', '종료연월', '격월사용량_톤']].values.tolist(),
                         [['000000001', '2026-06', 40.0], ['000000001', '2026-08', 60.0]])

    def test_remote_default_customer_is_not_accepted_for_another_customer(self):
        session = Mock()
        payload = {"result": True, "searchMkey": "000000002", "groups": []}
        session.get.return_value = Mock(url='https://i121.seoul.go.kr/cyber/front/mypage/JR_remoteMeterHistory.do',
                                       status_code=200, text=json.dumps(payload), json=Mock(return_value=payload))
        with self.assertRaises(ValueError):
            water.fetch_month(session, self.meter, '2026-06')

    def test_readding_same_customer_cannot_create_second_contract(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with db.connect() as conn:
                conn.execute("""INSERT INTO meters(id,provider,customer_number,station_id,office_id,display_name,created_at,updated_at)
                            VALUES('another','arisu','000000001','station','office','가역','now','now')""")
        with db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM meters').fetchone()[0], 1)

    def test_snapshot_retention_keeps_recent_versions_and_unmanaged_files(self):
        root = config.DATA_DIR / 'snapshot'
        for index in range(6):
            directory = root / 'versions' / f'{index:032x}'
            directory.mkdir(parents=True)
            (directory / 'daily.json').write_text('{}')
            timestamp = time.time() - (90000 - index)
            os.utime(directory, (timestamp, timestamp))
        recent = root / 'versions' / ('f' * 32)
        recent.mkdir()
        unmanaged = root / 'versions/operator-backup'
        unmanaged.mkdir()
        manifest = publish_data({}, {}, output_dir=root)
        self.assertTrue((root / manifest['directory']).is_dir())
        self.assertTrue(recent.is_dir())
        self.assertTrue(unmanaged.is_dir())
        self.assertFalse((root / 'versions' / ('0' * 32)).exists())
        self.assertEqual(len(list((root / 'versions').iterdir())), 4)

    def test_unexpected_failure_logs_type_without_sensitive_exception_text(self):
        with patch.object(catalog, 'data_sources', side_effect=ValueError('do-not-log-secret')):
            with self.assertLogs(inc.logger, level='ERROR') as captured:
                result = inc.collect_meter(self.meter)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('ValueError', captured.output[0])
        self.assertNotIn('do-not-log-secret', captured.output[0])


if __name__ == '__main__':
    unittest.main()
