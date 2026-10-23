"""Arisu status checks at 0/12/18 o'clock reuse the persisted queue and save only Arisu status."""
import json
import tempfile
import threading
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import Mock, patch

from back.api import catalog, collection, config, db
from back.pipelines import incremental as inc
from back.pipelines.common import write_json
from back.pipelines.daily_water import scraper
from tests.test_smart_meter import card, page


CID = '000000001'
SUCCESS = {'status': 'success', 'message': '완료', 'connection_verified': True}
SUPERADMIN = {'role': 'superadmin', 'office_ids': []}


def at(hour, minute=0, day=24):
    return datetime(2026, 10, day, hour, minute, tzinfo=collection.KST)


class StatusCollectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        patches = [patch.multiple(config, DATA_DIR=self.root, APP_DB_PATH=self.root / 'app.sqlite3',
                                  HISTORY_DIR=self.root / 'history', COLLECTION_WORKER_ENABLED=True),
                   patch.object(inc, 'today', return_value=date(2026, 10, 24)),
                   patch.object(scraper, 'today', return_value=date(2026, 10, 24)),
                   patch.object(catalog, 'today', return_value=date(2026, 10, 24)),
                   patch('back.storage.hooks.after_collection_job')]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self.tmp.cleanup)
        db.init_db()
        with db.connect() as c:
            c.execute("INSERT INTO offices(id,name) VALUES('office','사업소')")
            c.execute("INSERT INTO stations(id,name) VALUES('station','가역')")
            for n, provider, active, daily, deleted in [
                    (1, 'arisu', 1, 1, None), (2, 'arisu', 1, 0, None),
                    (3, 'arisu', 0, 1, None), (4, 'other', 1, 1, None), (5, 'arisu', 1, 1, 'deleted')]:
                c.execute('''INSERT INTO meters(id,provider,customer_number,station_id,office_id,display_name,
                             active,daily_enabled,deleted_at,created_at,updated_at) VALUES(?,?,?,'station','office','가역',?,?,?,?,?)''',
                          (str(n), provider, f'{n:09d}', active, daily, deleted, db.now(), db.now()))
            self.meter = catalog.list_meters(c)[0]

    def schedule(self, when):
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            return collection.schedule_smart_due(c, when)

    def finish(self, job_id):
        with db.connect() as c:
            c.execute("UPDATE collection_jobs SET status='success',finished_at=? WHERE id=?", (at(6).isoformat(), job_id))
            c.execute("UPDATE collection_items SET status='success' WHERE job_id=?", (job_id,))

    def session(self):
        payload = {'result': True, 'searchMkey': CID, 'groups': [{'mkey': CID, 'napgi': '20261231', 'details': [
            {'measDt': '2026-10-23', 'realGcDay': '2026102400', 'status': '정상',
             'thsmmPointer': '1234', 'dayUseQty': '4', 'thsmmUseqty': '99'},
            {'measDt': '2026-10-22', 'realGcDay': None, 'status': '계량기 장애', 'dayUseQty': None}]}]}
        session = Mock(arisu_customer_numbers=frozenset({CID}), arisu_login_error=None)
        def get(url, **kwargs):
            if 'NR_myArisu.do' in url:
                return Mock(text=page(card()), status_code=200, url=url)
            self.assertIn('JR_remoteMeterDayList.do', url)
            return Mock(text=json.dumps(payload), status_code=200, url=url, json=Mock(return_value=payload))
        session.get.side_effect = get
        return session

    def test_checks_run_at_0_12_18_and_missed_checks_coalesce(self):
        first = self.schedule(at(6, 15))   # never checked: check now
        with db.connect() as c:
            self.assertEqual([r[0] for r in c.execute('SELECT meter_id FROM collection_items WHERE job_id=?', (first,))], ['1'])
            settings = collection.settings(c, at(6, 15))
            self.assertEqual(settings['smart_hours'], [0, 8, 12, 18])
            self.assertEqual(settings['smart_next_run_at'], at(8).isoformat())   # the daily run is the 08:00 check
        self.assertIsNone(self.schedule(at(13)))   # the first job is still queued
        self.finish(first)
        self.assertIsNone(self.schedule(at(11, 59)))
        second = self.schedule(at(12))   # a new database connection sees the saved check time
        self.finish(second)
        third = self.schedule(at(23))   # 18:00 was missed: one check, not one per missed slot
        self.assertIsNone(self.schedule(at(23, 30)))
        self.finish(third)
        with db.connect() as c:
            self.assertEqual(collection.settings(c, at(23, 30))['smart_next_run_at'], at(0, day=25).isoformat())
            self.assertEqual([r[0] for r in c.execute('SELECT trigger FROM collection_jobs ORDER BY id')], ['smart_status'] * 3)
        self.assertIsNotNone(self.schedule(at(0, 1, day=25)))

    def test_other_check_hours_work_before_their_first_slot(self):
        with patch.object(collection, 'SMART_HOURS', (1, 12, 18)):
            first = self.schedule(at(0, 30))   # before 01:00 the latest slot is yesterday's 18:00
            self.finish(first)
            self.assertIsNone(self.schedule(at(0, 45)))
            with db.connect() as c:
                self.assertEqual(collection.settings(c, at(0, 45))['smart_next_run_at'], at(1).isoformat())
            self.assertIsNotNone(self.schedule(at(1)))

    def test_daily_run_is_the_8_oclock_check(self):
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            collection.schedule_due(c, at(8))
            daily = c.execute("SELECT id FROM collection_jobs WHERE trigger='automatic'").fetchone()[0]
        self.finish(daily)
        self.assertIsNone(self.schedule(at(9)))
        with db.connect() as c:
            self.assertEqual(collection.settings(c, at(9))['smart_next_run_at'], at(12).isoformat())
        self.assertIsNotNone(self.schedule(at(12)))

    def test_disabled_worker_and_existing_full_job_prevent_status_queue(self):
        with patch.object(config, 'COLLECTION_WORKER_ENABLED', False):
            self.assertIsNone(self.schedule(at(12)))
            with db.connect() as c:
                self.assertIsNone(collection.settings(c, at(12))['smart_next_run_at'])
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            collection.enqueue(c, ['1'], 'manual')
        self.assertIsNone(self.schedule(at(12)))

    def test_status_check_saves_only_arisu_status(self):
        checkpoint = self.root / 'collection' / f'{CID}.json'
        manifest = self.root / 'snapshot/manifest.json'
        write_json(checkpoint, {'water_checked': {'2026-09': '2026-10-23'}})
        write_json(manifest, {'published': False})
        before = checkpoint.read_bytes(), manifest.read_bytes()
        with patch.object(inc, 'fetch_month') as history, patch.object(inc, 'collect_bills') as bills, \
                patch.object(inc, 'publish_data') as publish:
            session = self.session()
            result = inc.collect_meter(self.meter, session=session, status_only=True)
        self.assertEqual(result['status'], 'success')
        self.assertEqual((result['daily_rows'], result['bill_rows']), (0, 0))
        self.assertTrue(result['connection_verified'])
        self.assertEqual(session.get.call_count, 2)
        history.assert_not_called()
        bills.assert_not_called()
        publish.assert_not_called()
        self.assertEqual(before, (checkpoint.read_bytes(), manifest.read_bytes()))
        self.assertEqual(json.loads((self.root / f'arisu/{CID}.json').read_text())['realtime_usage'], 4)
        index = json.loads((self.root / f'raw/remote_meter/{CID}/statuses.json').read_text())
        self.assertEqual(index['readings'][0]['status'], '계량기 장애')
        self.assertTrue((self.root / f'raw/remote_meter/{CID}/day/2026-10-24.json').exists())
        # Usage stays with the 08:00 run, so the screen never shows a day's usage before its verdict is due.
        self.assertFalse((self.root / 'daily').exists())
        self.assertEqual(catalog.payload()['daily'], {})

    def test_failed_card_keeps_previous_value_and_both_failures_keep_everything(self):
        smart_path = self.root / f'arisu/{CID}.json'
        write_json(smart_path, {'customer_number': CID, 'leak_status': '주의', 'realtime_usage': 10})
        before = smart_path.read_bytes()
        with patch.object(inc, 'fetch_smart_status', side_effect=TimeoutError):
            result = inc.collect_meter(self.meter, session=self.session(), status_only=True)
        self.assertEqual(result['status'], 'partial')
        self.assertTrue(result['retryable'])
        self.assertEqual(smart_path.read_bytes(), before)
        statuses = self.root / f'raw/remote_meter/{CID}/statuses.json'
        statuses_before = statuses.read_bytes()
        with patch.object(inc, 'fetch_smart_status', side_effect=TimeoutError), \
                patch.object(inc, 'fetch_day_statuses', side_effect=TimeoutError):
            result = inc.collect_meter(self.meter, session=self.session(), status_only=True)
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(result['connection_verified'])
        self.assertEqual((smart_path.read_bytes(), statuses.read_bytes()), (before, statuses_before))

    def test_job_dispatch_keeps_manual_full_collection_and_skips_disabled_meter(self):
        session = self.session()
        check = self.schedule(at(12))
        with patch('back.scripts.billing_etl.i121_crawler.auth.collection_session', return_value=session) as login, \
                patch.object(inc, 'collect_meter', return_value=SUCCESS) as collect:
            collection.run_job(check, threading.Event())
            self.assertTrue(collect.call_args.kwargs['status_only'])
            login.assert_called_once()
            with db.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                manual = collection.enqueue(c, ['1'], 'manual')
            collection.run_job(manual, threading.Event())
            self.assertNotIn('status_only', collect.call_args.kwargs)
            check = self.schedule(at(18))
            with db.connect() as c:
                c.execute("UPDATE meters SET daily_enabled=0 WHERE id='1'")
            collect.reset_mock()
            collection.run_job(check, threading.Event())
            collect.assert_not_called()

    def test_status_check_cannot_hide_daily_failures_or_cancel_daily_retry(self):
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            daily = collection.enqueue(c, ['1'], 'automatic')
            c.execute("UPDATE collection_jobs SET status='failed',finished_at='2026-10-24T08:05:00+09:00' WHERE id=?", (daily,))
            c.execute("UPDATE collection_items SET status='failed',message='청구서 조회 실패',retryable=1 WHERE job_id=?", (daily,))
            c.execute("INSERT INTO settings VALUES('collection_auto_day','2026-10-24')")
            check = collection.enqueue(c, ['1'], 'smart_status')
        meter_state = lambda: next(m for m in collection.status(SUPERADMIN)['meters'] if m['meter_id'] == '1')
        self.assertEqual(meter_state()['status'], 'queued')   # a check still shows while it waits or runs
        self.finish(check)
        view = collection.status(SUPERADMIN)
        self.assertEqual(view['latest']['id'], daily)
        self.assertEqual((meter_state()['status'], meter_state()['message']), ('failed', '청구서 조회 실패'))
        self.assertEqual([job['trigger'] for job in view['history']], ['smart_status', 'automatic'])
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            self.assertIsNone(collection.schedule_due(c, at(8, 35)))
            self.assertEqual(c.execute('SELECT trigger FROM collection_jobs ORDER BY id DESC LIMIT 1').fetchone()[0], 'automatic_retry')

    def test_login_rejection_pauses_checks_until_a_later_login_succeeds(self):
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            rejected_job = collection.enqueue(c, ['1'], 'manual')
        rejected = Mock(arisu_customer_numbers=frozenset(), arisu_login_error='LoginError')
        with patch('back.scripts.billing_etl.i121_crawler.auth.collection_session', return_value=rejected), \
                patch.object(inc, 'collect_meter', return_value={'status': 'failed', 'message': '로그인 거부'}):
            collection.run_job(rejected_job, threading.Event())
        self.assertIsNone(self.schedule(at(12)))
        self.assertIsNone(self.schedule(at(0, 1, day=25)))   # no new login at midnight: a wrong password costs one try a day
        with db.connect() as c:
            settings = collection.settings(c, at(12))
            self.assertEqual(settings['smart_paused_reason'], 'login_rejected')
            self.assertIsNone(settings['smart_next_run_at'])
            c.execute('BEGIN IMMEDIATE')
            recovered_job = collection.enqueue(c, ['1'], 'manual')
        with patch('back.scripts.billing_etl.i121_crawler.auth.collection_session', return_value=self.session()), \
                patch.object(inc, 'collect_meter', return_value=SUCCESS):
            collection.run_job(recovered_job, threading.Event())
        with db.connect() as c:
            self.assertIsNone(c.execute("SELECT 1 FROM settings WHERE key='collection_login_rejected'").fetchone())
        self.assertIsNotNone(self.schedule(at(0, 1, day=25)))


if __name__ == '__main__':
    unittest.main()
