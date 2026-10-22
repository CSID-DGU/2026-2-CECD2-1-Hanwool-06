"""Offline collector regressions. No credentials, network requests, or production writes."""
import csv
import json
import tempfile
import unittest
from argparse import Namespace
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import numpy as np
import pandas as pd

from back.pipelines.common import merge_csv
from back.pipelines.daily_water import scraper as water
from back.pipelines.daily_ridership import monthly, scraper as riders
from back.scripts.billing_etl import crawl_bills as bills
from back.scripts.billing_etl.i121_crawler import auth
from back.ml.lightgbm import metric, dataset
from back.ml.lightgbm.utils import save_csv, save_json
from back.pipelines.refresh import build_dataset, publish_snapshot
from back.pipelines import refresh

ROOT = Path(__file__).resolve().parents[1]

METERS = [{"id": str(n), "customer_number": str(n).zfill(9), "provider": "arisu",
           "station_name": {1: "강동역", 2: "길동역"}[n], "line": n, "active": True, "daily_enabled": True}
          for n in [1, 2]]


def reading_payload(value=12):
    return {"result": True, "searchMkey": "000000001", "groups": [
        {"mkey": "000000001", "napgi": "20260930", "thsmmUseqty": "100", "details": [
            {"measDt": "2026-06-01 00:00:00", "realGcDay": "2026060200",
             "thsmmPointer": "1,000", "dayUseQty": value, "thsmmUseqty": "0", "status": "정상"}]}]}


def reading_response(payload=None):
    payload = reading_payload() if payload is None else payload
    return Mock(text=json.dumps(payload), url='https://i121.seoul.go.kr/cyber/front/mypage/JR_remoteMeterHistory.do',
                status_code=200, json=Mock(return_value=payload))


def bill_html():
    return '<table><caption>부과내역이며</caption><tbody>' + ''.join(
        f'<tr><td>{i}</td><td>000000001</td><td>{kind}</td><td>2026-06-30</td><td>수납 완료</td><td>100</td><td>역1</td><td>20</td><td><a onclick="jungInfoDetail(\'20260630\')"></a></td></tr>'
        for i, kind in enumerate(['정기분', '수시분'], 1)) + '</tbody></table>'


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def rows(self, path):
        with path.open(encoding='utf-8-sig', newline='') as fp:
            return list(csv.DictReader(fp))

    def test_model_outputs_replace_existing_files_privately(self):
        csv_path, json_path = self.path / 'model.csv', self.path / 'metrics.json'
        for path in (csv_path, json_path):
            path.write_text('old')
            path.chmod(0o644)
        save_csv(pd.DataFrame([{'고객번호': '000000001'}]), csv_path)
        save_json({'결과': '정상'}, json_path)
        self.assertEqual(self.rows(csv_path), [{'고객번호': '000000001'}])
        self.assertEqual(json.loads(json_path.read_text()), {'결과': '정상'})
        self.assertTrue(all(path.stat().st_mode & 0o077 == 0 for path in (csv_path, json_path)))

    def test_water_month_reuse_upsert_partial_and_login_error(self):
        session = Mock()
        response = reading_response()
        session.get.return_value = response
        reports = water.scrape_range(date(2026, 6, 1), date(2026, 6, 2), meters=METERS[:1],
                                     session=session, out_dir=self.path, sleep=0)
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(reports[1]['status'], 'pending')
        self.assertFalse((self.path / '2026-06-02.csv').exists())
        water.scrape_range(date(2026, 6, 1), date(2026, 6, 1), meters=METERS[:1],
                          session=session, out_dir=self.path, sleep=0)
        path = self.path / '2026-06-01.csv'
        self.assertEqual(len(self.rows(path)), 1)
        before = path.read_bytes()
        response.text = '<input name="userId"><input name="userPwd">'
        reports = water.scrape_range(date(2026, 6, 1), date(2026, 6, 1), meters=METERS,
                                     session=session, out_dir=self.path, sleep=0)
        self.assertEqual(reports[0]['status'], 'failed')
        self.assertEqual(path.read_bytes(), before)

    def test_water_json_identity_empty_error_and_numeric_validation(self):
        session = Mock()
        for identity in ('top', 'group'):
            payload = reading_payload()
            if identity == 'top':
                payload['searchMkey'] = '000000002'
            else:
                payload['groups'][0]['mkey'] = '000000002'
            session.get.return_value = reading_response(payload)
            with self.assertRaises(ValueError):
                water.fetch_month(session, METERS[0], '2026-06')
        for value in ('', None, 'NaN', '-1', 'Infinity'):
            session.get.return_value = reading_response(reading_payload(value))
            with self.subTest(value=value), self.assertRaises(ValueError):
                water.fetch_month(session, METERS[0], '2026-06')
        session.get.return_value = reading_response(reading_payload(0))
        row = water.fetch_month(session, METERS[0], '2026-06')[0]
        self.assertEqual((row['일사용량(톤)'], row['납기별누적사용량(톤)']), ('0', '0'))
        self.assertEqual((row['사용일'], row['검침일자'], row['지침값']), ('2026-06-01', '2026-06-02', '1000'))
        self.assertEqual(session.get.call_args.kwargs['params']['searchEndYm'], '2026-09')
        for payload, error in [({'result': False, 'message': '로그인 시간이 만료되었습니다.'}, auth.LoginError),
                               ({'result': False, 'message': '조회 가능한 고객번호가 아닙니다.'}, auth.CustomerAccessError),
                               ({'result': False, 'message': '조회 오류'}, ValueError),
                               ({'result': True, 'searchMkey': '000000001'}, ValueError)]:
            session.get.return_value = reading_response(payload)
            with self.assertRaises(error):
                water.fetch_month(session, METERS[0], '2026-06')
        session.get.return_value = reading_response({'result': True, 'searchMkey': '000000001', 'groups': []})
        self.assertEqual(water.fetch_month(session, METERS[0], '2026-06'), [])

    def test_water_json_skips_incomplete_or_abnormal_observations(self):
        session = Mock()
        payload = reading_payload()
        detail = payload['groups'][0]['details'][0]
        for change in ({'status': '수집중'}, {'measDt': '2026-06-02 00:00:00', 'realGcDay': '2026060300'},
                       {'realGcDay': '2026060300'}):
            payload['groups'][0]['details'] = [detail | change]
            session.get.return_value = reading_response(payload)
            with patch.object(water, 'today', return_value=date(2026, 6, 2)):
                self.assertEqual(water.fetch_month(session, METERS[0], '2026-06'), [])
        payload['groups'][0]['details'] = [detail | {'measDt': '2026-06-31 00:00:00'}]
        session.get.return_value = reading_response(payload)
        with self.assertRaises(ValueError):
            water.fetch_month(session, METERS[0], '2026-06')

    def test_invalid_reading_is_reported_by_day_without_losing_good_days(self):
        payload = reading_payload()
        detail = payload['groups'][0]['details'][0]
        payload['groups'][0]['details'].extend([
            detail | {'measDt': '2026-06-02', 'realGcDay': '2026060300', 'dayUseQty': '-6'},
            detail | {'measDt': '2026-06-03', 'realGcDay': '2026060400', 'dayUseQty': ''},
            detail | {'measDt': '2026-06-04', 'realGcDay': '2026060500', 'dayUseQty': '3186'}])
        session = Mock()
        session.get.return_value = reading_response(payload)
        with self.assertRaises(ValueError):
            water.fetch_month(session, METERS[0], '2026-06')
        issues = []
        rows = water.fetch_month(session, METERS[0], '2026-06', issues=issues)
        self.assertEqual(issues, ['2026-06-02', '2026-06-03'])
        self.assertEqual([r['사용일'] for r in rows], ['2026-06-01', '2026-06-04'])
        self.assertEqual(rows[-1]['일사용량(톤)'], '3186')
        reports = water.scrape_range(date(2026, 6, 1), date(2026, 6, 4), meters=METERS[:1],
                                     session=session, out_dir=self.path, sleep=0)
        self.assertEqual([r['status'] for r in reports], ['complete', 'failed', 'failed', 'complete'])
        self.assertEqual([r['errors'][0]['date'] for r in reports if r['errors']], issues)
        self.assertTrue((self.path / '2026-06-01.csv').exists())
        self.assertTrue((self.path / '2026-06-04.csv').exists())

    def test_latest_normal_reading_clears_duplicate_source_issue(self):
        history = reading_payload('')
        history['groups'][0]['details'] *= 2
        session = Mock()
        session.get.side_effect = [reading_response(history), reading_response(reading_payload(0))]
        issues = []
        with patch.object(water, 'today', return_value=date(2026, 6, 23)):
            rows = water.fetch_month(session, METERS[0], '2026-06', issues=issues)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['일사용량(톤)'], '0')
        self.assertEqual(issues, [])

    def test_matching_daily_reading_in_two_due_periods_is_only_counted_once(self):
        payload = reading_payload()
        original = payload['groups'][0]
        detail = original['details'][0]
        duplicate = original | {'napgi': '20261031', 'details': [detail | {
            'thsmmPointer': '1000.0', 'dayUseQty': '12.0', 'thsmmUseqty': '999'}]}
        payload['groups'].append(duplicate)
        session = Mock()
        session.get.return_value = reading_response(payload)
        rows = water.fetch_month(session, METERS[0], '2026-06')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['일사용량(톤)'], '12')
        self.assertEqual(rows[0]['납기별누적사용량(톤)'], '0')
        for change in ({'realGcDay': '2026060300'}, {'thsmmPointer': '1001'}, {'dayUseQty': '13'}):
            duplicate['details'] = [detail | change]
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'Conflicting'):
                water.fetch_month(session, METERS[0], '2026-06')

    def test_latest_daily_readings_fill_unbilled_days_and_reuse_one_query(self):
        def payload(days):
            result = reading_payload()
            detail = result['groups'][0]['details'][0]
            result['groups'][0]['details'] = [detail | {'measDt': day, 'realGcDay': measured}
                for day, measured in days]
            return result
        history = payload([('2026-08-22', '2026082300')])
        latest = payload([('2026-08-22', '2026082300'), ('2026-08-23', '2026082400'),
                          ('2026-09-22', '2026092300'), ('2026-09-23', '2026092400')])
        session = Mock()
        session.get.side_effect = lambda url, **_: reading_response(latest if 'DayList' in url else history)
        with patch.object(water, 'today', return_value=date(2026, 9, 23)):
            august = water.fetch_month(session, METERS[0], '2026-08')
            september = water.fetch_month(session, METERS[0], '2026-09')
        self.assertEqual([r['사용일'] for r in august], ['2026-08-22', '2026-08-23'])
        self.assertEqual([r['사용일'] for r in september], ['2026-09-22'])
        urls = [call.args[0] for call in session.get.call_args_list]
        self.assertEqual(sum('DayList' in url for url in urls), 1)
        self.assertEqual(len(urls), 3)
        with patch.object(water, 'today', return_value=date(2026, 9, 24)):
            next_day = water.fetch_month(session, METERS[0], '2026-09')
        self.assertEqual([r['사용일'] for r in next_day], ['2026-09-22', '2026-09-23'])
        self.assertEqual(sum('DayList' in call.args[0] for call in session.get.call_args_list), 2)

    def test_latest_daily_response_also_rejects_another_customer(self):
        history = {'result': True, 'searchMkey': '000000001', 'groups': []}
        latest = {'result': True, 'searchMkey': '000000002', 'groups': []}
        session = Mock()
        session.get.side_effect = [reading_response(history), reading_response(latest)]
        with patch.object(water, 'today', return_value=date(2026, 9, 23)), self.assertRaises(ValueError):
            water.fetch_month(session, METERS[0], '2026-09')
        self.assertEqual(session._arisu_day_readings, {})

    def test_ridership_partial_retains_previous_and_pending_never_zeros(self):
        path = self.path / '2026-06-01.csv'
        fields = ['고객번호', '역명', '날짜', '총승객수']
        merge_csv(path, [{'고객번호': '000000002', '역명': '역2', '날짜': '2026-06-01', '총승객수': 40}], fields, ['고객번호', '날짜'])
        # 검토 매핑은 저장소가 아니라 이력 폴더에 있으므로 테스트용 파일을 준다.
        match = self.path / 'meter_match.csv'
        match.write_text('고객번호,호선\n000000001,\n000000002,2.0\n', encoding='utf-8-sig')
        with patch.object(riders, 'MATCH', match):
            riders.scrape(date(2026, 6, 1), meters=METERS, out_dir=self.path,
                          fetcher=lambda *_: ({('강동', 1): 30}, {'강동': 30}))
            self.assertEqual(len(self.rows(path)), 2)
            previous = path.read_bytes()
            self.assertIsNone(riders.scrape(date(2026, 6, 1), meters=METERS, out_dir=self.path,
                                           fetcher=lambda *_: ({}, {})))
        self.assertEqual(path.read_bytes(), previous)

    def test_monthly_ridership_file_fills_days_with_the_reviewed_station_rule(self):
        source = self.path / 'CARD_SUBWAY_MONTH_202605.csv'
        source.write_text('\n'.join(['"사용일자","노선명","역명","승차총승객수","하차총승객수","등록일자"',
            '"20260501","1호선","강동","10","20","20260504",""', '"20260501","중앙선","강동(구청)","100","200","20260504",""',
            '"20260501","2호선","길동","1","2","20260504",""', '"20260501","5호선","길동","50","60","20260504",""',
            '"20260502","2호선","길동","3","4","20260505",""']), encoding='utf-8-sig')
        match = self.path / 'meter_match.csv'
        match.write_text('고객번호,호선\n000000001,\n000000002,2.0\n', encoding='utf-8-sig')
        out = self.path / 'ridership'
        with patch.object(riders, 'MATCH', match):
            for _ in range(2):  # importing the same month again changes nothing
                self.assertEqual(monthly.import_month(source, meters=METERS, out_dir=out), ['2026-05-01', '2026-05-02'])
        first = {r['고객번호']: r['총승객수'] for r in self.rows(out / '2026-05-01.csv')}
        self.assertEqual(first, {'000000001': '330', '000000002': '3'})  # whole station vs. its own line
        # The daily collector writes the same customer and date, so it must count by the same rule.
        byline, bystn = monthly.read_month(source)['20260501']
        with patch.object(riders, 'MATCH', match):
            riders.scrape(date(2026, 5, 1), meters=METERS, out_dir=self.path / 'daily-api', fetcher=lambda *_: (byline, bystn))
        self.assertEqual({r['고객번호']: r['총승객수'] for r in self.rows(self.path / 'daily-api/2026-05-01.csv')}, first)
        self.assertEqual([(r['고객번호'], r['총승객수']) for r in self.rows(out / '2026-05-02.csv')], [('000000002', '7')])
        self.assertEqual(json.loads((out / '2026-05-02.status.json').read_text())['missing'], ['000000001'])
        source.write_text(source.read_text(encoding='utf-8-sig').replace('"3","4"', '"-3","4"'), encoding='utf-8-sig')
        with patch.object(riders, 'MATCH', match), self.assertRaises(ValueError):
            monthly.import_month(source, meters=METERS, out_dir=out)

    def test_monthly_download_never_keeps_an_empty_or_wrong_file(self):
        good = ('"사용일자","노선명","역명","승차총승객수","하차총승객수","등록일자"\n"20260501","1호선","강동","10","20","20260504"\n').encode('utf-8-sig')
        session = Mock()
        fetch = lambda: monthly.download_month('2026-05', '7', session, raw_dir=self.path)
        cut_short = good + '"20260502","1호선","강'.encode()                                    # the transfer stopped mid-row
        next_month = good + '"20260601","1호선","강동","1","2","20260604"\n'.encode()          # rows of another month mixed in
        not_a_count = good + '"20260502","1호선","강동","x","2","20260504"\n'.encode()
        cut_in_number = good + '"20260502","1호선","강동","10","2'.encode()                     # stopped inside a quoted count
        no_such_day = good + '"20260532","1호선","강동","1","2","20260604"\n'.encode()
        for body in (b'', b'  \n', good.replace(b'20260501', b'20260401'), b'<html>login</html>', cut_short, next_month, not_a_count,
                     cut_in_number, no_such_day):
            session.post.return_value = Mock(content=body, raise_for_status=Mock())
            with self.assertRaises(ValueError):
                fetch()
            self.assertEqual(list(self.path.glob('CARD_SUBWAY_MONTH_*')), [])
        # an empty or damaged file left by an earlier run is fetched again instead of being trusted
        session.post.return_value = Mock(content=good, raise_for_status=Mock())
        for left_behind in (b'', cut_short, next_month, cut_in_number, no_such_day):
            (self.path / 'CARD_SUBWAY_MONTH_202605.csv').write_bytes(left_behind)
            self.assertEqual(fetch().read_bytes(), good)
        session.post.reset_mock()
        self.assertEqual(fetch().read_bytes(), good)   # a good file is reused without another request
        session.post.assert_not_called()

    def test_bills_preserve_partial_success_and_supplementary_rows(self):
        def fetch(_session, customer, *_args, **_kwargs):
            if customer.endswith('2'):
                raise TimeoutError()
            return bill_html(), True
        with patch.object(bills, 'fetch_bill_window', side_effect=fetch):
            for _ in range(2):
                report = bills.collect_bills(start_ym='2026-06', end_ym='2026-06', mkeys=['1', '2'],
                    session=Mock(), cache_dir=self.path / 'cache', out_dir=self.path, sleep=0)
        self.assertEqual(report['status'], 'failed')
        rows = self.rows(self.path / 'bills_long.csv')
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['gubun'] for r in rows}, {'정기분', '수시분'})

    def test_supplementary_notice_numbers_survive_csv_refresh(self):
        html = '<form action="/cyber/front/mypage/NR_levySearch.do"><input type="hidden" name="searchMkey" value="000000001"></form>'
        html += bill_html().replace('<td>000000001</td><td>수시분</td>', '<td>900000001</td><td>수시분</td>')
        second = '<tr><td>3</td><td>900000002</td><td>수시분</td><td>2026-06-30</td><td>수납 완료</td><td>0</td><td>역1</td><td>0</td><td></td></tr>'
        html = html.replace('</tbody>', second + '</tbody>')
        with patch.object(bills, 'fetch_bill_window', return_value=(html, True)):
            for _ in range(2):
                report = bills.collect_bills(start_ym='2026-06', end_ym='2026-06', mkeys=['1'],
                    session=Mock(), cache_dir=self.path / 'cache', out_dir=self.path, sleep=0)
                self.assertEqual(report['errors'], [])
        rows = self.rows(self.path / 'bills_long.csv')
        self.assertEqual(len(rows), 3)
        self.assertEqual({r['mkey'] for r in rows}, {'000000001'})
        self.assertEqual({r['notice_number'] for r in rows}, {'', '900000001', '900000002'})

    def test_env_credentials_prefer_arisu_and_login_is_verified(self):
        with patch.dict('os.environ', {'ARISU_USER_ID': 'new', 'ARISU_USER_PWD': 'pwd',
                                      'I121_USER_ID': 'old', 'I121_USER_PWD': 'oldpwd'}, clear=True):
            with patch.object(auth, 'login', return_value='session') as login:
                self.assertEqual(auth.session_from_env(), 'session')
                login.assert_called_once_with('new', 'pwd')
        # Collectors use the process environment only; reading a .env is left to back.api.config.
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(auth.ConfigurationError):
                auth.session_from_env()
            with self.assertRaises(RuntimeError):
                riders._load_key()
        session = Mock()
        session.get.return_value = Mock(text='<input name="userId">', url='https://x/NR_loginForm.do')
        with patch.object(auth, '_new_session', return_value=session):
            with self.assertRaises(auth.LoginError):
                auth.login('user', 'password')

    def test_validation_calibration_is_unchanged_by_future_outlier(self):
        valid = pd.DataFrame({'고객번호': ['1'] * 3, '일사용량_톤': [9., 10., 11.]})
        calibration = metric.fit_calibration(valid, np.array([10., 10., 10.]))
        earlier = valid.iloc[:1].copy()
        score = metric.score_predictions(earlier, np.array([10.]), calibration)['deviation_score'].iloc[0]
        later = pd.concat([earlier, pd.DataFrame({'고객번호': ['1'], '일사용량_톤': [999.]})], ignore_index=True)
        scored = metric.score_predictions(later, np.array([10., 10.]), calibration)
        self.assertEqual(score, scored['deviation_score'].iloc[0])
        unknown = pd.DataFrame({'고객번호': ['new'], '일사용량_톤': [10.]})
        self.assertEqual(metric.classify(metric.score_predictions(unknown, np.array([10.]), calibration), 2, 3)['심각도'].iloc[0], '자료부족')

    def test_bill_baseline_never_reads_future_bills(self):
        path = self.path / 'bills.csv'
        pd.DataFrame([{'고객번호': '000000001', '포함월수': 2, '격월사용량_톤': 608,
                       '시작연월': '2024-01', '종료연월': '2024-02'}]).to_csv(path, index=False)
        frame = pd.DataFrame({'고객번호': ['000000001'] * 2, '날짜': pd.to_datetime(['2024-02-01', '2025-02-01'])})
        result = dataset._merge_bill_baseline(frame, {'data': {'bills': str(path)}})
        self.assertTrue(pd.isna(result['bill_daily_avg'].iloc[0]))
        self.assertAlmostEqual(result['bill_daily_avg'].iloc[1], 10.)

    def test_perfect_validation_does_not_mark_zero_error_as_alert(self):
        frame = pd.DataFrame({'고객번호': ['1'] * 3, '일사용량_톤': [10.] * 3})
        scored = metric.score_predictions(frame, np.array([10.] * 3))
        thresholds = metric.score_quantiles(scored['deviation_score'], .95, .99)
        result = metric.classify(scored, *thresholds)
        self.assertEqual(set(result['심각도']), {'정상'})

    def test_snapshot_pointer_only_changes_after_both_files_validate(self):
        master = pd.DataFrame({'고객번호': ['000000001'], '날짜': ['2026-06-01'], '일사용량_톤': [12.]})
        ride = pd.DataFrame({'고객번호': ['000000001'], '날짜': ['2026-06-01'], '총승객수': [30.]})
        manifest = publish_snapshot(master, ride, output_dir=self.path)
        before = (self.path / 'manifest.json').read_bytes()
        self.assertTrue((self.path / manifest['directory'] / 'daily.json').exists())
        with self.assertRaises(ValueError):
            publish_snapshot(master, ride, output_dir=self.path, previous_risk={'bad': [float('nan')]})
        self.assertEqual((self.path / 'manifest.json').read_bytes(), before)

    def test_first_observation_only_refresh_preserves_initial_risk(self):
        master = pd.DataFrame({'고객번호': ['000000001'], '날짜': ['2026-06-01'], '일사용량_톤': [12.]})
        riders = pd.DataFrame({'고객번호': ['000000001'], '날짜': ['2026-06-01'], '총승객수': [30.]})
        risk = {'000000001': [{'date': '2026-05-31', 'severity': '주의'}]}
        args = Namespace(runtime_dir=self.path, start=date(2026, 6, 1), end=date(2026, 6, 1),
                         water=True, ridership=False, bills=False, model=False, train_end=None, valid_end=None, test_end=None, jobs=1)
        with patch('back.api.catalog.collector_meters', return_value=METERS), \
             patch('back.api.catalog.data_sources', return_value=({}, risk)), \
             patch.object(water, 'scrape_range', return_value=[]), \
             patch.object(refresh, 'build_dataset', return_value=(master, riders, [], set())), \
             patch.object(refresh, 'build_calendar'):
            self.assertEqual(refresh.run(args), 0)
        manifest = json.loads((self.path / 'snapshot/manifest.json').read_text())
        self.assertEqual(manifest['risk_as_of'], '2026-05-31')
        self.assertEqual(json.loads((self.path / 'snapshot' / manifest['directory'] / 'risk.json').read_text()), risk)

    def test_snapshot_shows_unchanged_remote_readings_as_data_checks_only_on_scored_days(self):
        days = pd.date_range('2026-06-01', periods=36).strftime('%Y-%m-%d').tolist()
        usage = [10.] * 28 + [0.] * 8   # 평소 하루 10톤이던 계량기가 days[28]부터 0톤
        early = [10.] * 8 + [0.] * 4 + [10.] * 24   # days[8]~days[11]의 멈춤은 게시된 위험도 범위(days[20]~) 밖
        master = pd.concat([pd.DataFrame({'고객번호': cid, '날짜': days, '일사용량_톤': values})
                            for cid, values in (('000000001', usage), ('000000002', usage), ('000000003', early))], ignore_index=True)
        riders = pd.DataFrame({'고객번호': ['000000001'], '날짜': [days[0]], '총승객수': [30.]})
        verdict = lambda day: {'date': day, 'actual': 0., 'pred': 9., 'residual': -9., 'z': -1., 'severity': '정상', 'dir': '과소', 'err': False}
        # The model judged through days[34]; days[35] is usage the 08:05 ridership step publishes before the model runs.
        previous = {'000000001': [verdict(day) for day in days[20:35]]}
        withheld = [{'customer_number': '000000002'}]   # 학습 이력이 모자라 판정하지 않는 계량기
        manifest = publish_snapshot(master, riders, output_dir=self.path, previous_risk=previous, withheld=withheld)
        risk = json.loads((self.path / manifest['directory'] / 'risk.json').read_text())
        rows = {r['date']: r for r in risk['000000001']}
        self.assertEqual(manifest['risk_as_of'], days[34])   # 자료 확인 행이 기준일을 앞당기지 않는다
        self.assertNotIn(days[35], rows)
        self.assertEqual(rows[days[27]], verdict(days[27]))
        self.assertEqual({k: rows[days[34]][k] for k in ('severity', 'err', 'reason', 'since', 'days', 'usual', 'actual', 'pred')},
                         {'severity': '자료 확인', 'err': True, 'reason': '값 그대로', 'since': days[28], 'days': 7,
                          'usual': 10.0, 'actual': 0.0, 'pred': None})
        self.assertEqual([r['date'] for r in risk['000000001']], days[20:35])
        self.assertNotIn('000000002', risk)
        self.assertNotIn('000000003', risk)
        # The next republish starts from this snapshot and gives the same rows.
        again = publish_snapshot(master, riders, output_dir=self.path, previous_risk=risk, withheld=withheld)
        self.assertEqual(json.loads((self.path / again['directory'] / 'risk.json').read_text()), risk)

    def test_model_run_publishes_the_frozen_days_it_did_not_score_as_data_checks(self):
        days = pd.date_range('2026-06-01', periods=36).strftime('%Y-%m-%d').tolist()
        frozen = [10.] * 28 + [0.] * 8
        master = pd.concat([pd.DataFrame({'고객번호': cid, '날짜': days, '일사용량_톤': values}) for cid, values in
                            (('000000001', frozen), ('000000002', [10.] * 36), ('000000003', frozen))], ignore_index=True)
        riders = pd.DataFrame({'고객번호': ['000000001'], '날짜': [days[0]], '총승객수': [30.]})
        # build_dataset left the frozen days out of test, so the model scored 000000001 only before them
        # and 000000003 (frozen through its whole scored period) not at all.
        scored = [('000000001', day) for day in days[20:28]] + [('000000002', day) for day in days[20:36]]
        anomalies = pd.DataFrame([{'고객번호': cid, '날짜': day, '일사용량_톤': 10., 'predicted_ton': 10., 'error_ton': 0.,
                                   'deviation_score': 0., '심각도': '정상', '방향': '과다', 'likely_data_error': False} for cid, day in scored])
        manifest = publish_snapshot(master, riders, output_dir=self.path, anomalies=anomalies)
        risk = json.loads((self.path / manifest['directory'] / 'risk.json').read_text())
        self.assertEqual(manifest['risk_as_of'], days[35])
        self.assertEqual([(r['date'], r['severity'], r.get('reason')) for r in risk['000000001'][7:9]],
                         [(days[27], '정상', None), (days[28], '자료 확인', '값 그대로')])
        self.assertEqual((len(risk['000000001']), risk['000000001'][-1]['days']), (16, 8))
        self.assertEqual([r['date'] for r in risk['000000003']], days[28:36])
        self.assertTrue(all(r['err'] and r['reason'] == '값 그대로' for r in risk['000000003']))
        self.assertTrue(all(not r['err'] and 'reason' not in r for r in risk['000000002']))
        self.assertEqual(len(risk['000000002']), 16)
        self.assertEqual(refresh.mark_stalled({}, master), {})

    def test_first_moving_day_is_published_with_its_value_and_clears_when_the_data_is_corrected(self):
        days = pd.date_range('2026-06-01', periods=36).strftime('%Y-%m-%d').tolist()
        frame = lambda cid, values: pd.DataFrame({'고객번호': cid, '날짜': days, '일사용량_톤': values})
        master = pd.concat([frame('000000001', [10.] * 28 + [0.] * 5 + [42.] + [10.] * 2), frame('000000002', [10.] * 36)], ignore_index=True)
        riders = pd.DataFrame({'고객번호': ['000000001'], '날짜': [days[0]], '총승객수': [30.]})
        scored = [('000000001', day) for day in days[20:28] + days[34:36]] + [('000000002', day) for day in days[20:36]]
        anomalies = pd.DataFrame([{'고객번호': cid, '날짜': day, '일사용량_톤': 10., 'predicted_ton': 10., 'error_ton': 0.,
                                   'deviation_score': 0., '심각도': '정상', '방향': '과다', 'likely_data_error': False} for cid, day in scored])
        manifest = publish_snapshot(master, riders, output_dir=self.path, anomalies=anomalies)
        risk = json.loads((self.path / manifest['directory'] / 'risk.json').read_text())
        row = {r['date']: r for r in risk['000000001']}[days[33]]
        self.assertEqual({k: row[k] for k in ('severity', 'reason', 'actual', 'days', 'usual')},
                         {'severity': '자료 확인', 'reason': '다시 움직인 첫날', 'actual': 42.0, 'days': 5, 'usual': 10.0})
        # The source later fills 000000001 with real values while 000000002 freezes: the old rows go, the new ones come.
        fixed = pd.concat([frame('000000001', [10.] * 36), frame('000000002', [10.] * 28 + [0.] * 8)], ignore_index=True)
        again = publish_snapshot(fixed, riders, output_dir=self.path, previous_risk=risk)
        risk = json.loads((self.path / again['directory'] / 'risk.json').read_text())
        self.assertFalse(any(r.get('reason') for r in risk['000000001']))
        self.assertEqual([r['date'] for r in risk['000000002'] if r.get('reason')], days[28:36])

    def test_model_refresh_reads_bills_collected_after_the_historical_export(self):
        master = pd.DataFrame({'고객번호': ['000000001'], '날짜': ['2026-06-01'], '일사용량_톤': [12.]})
        riders = pd.DataFrame({'고객번호': ['000000001'], '날짜': ['2026-06-01'], '총승객수': [30.]})
        merge_csv(self.path / 'raw/billing_i121/i121_bills/bills_long.csv',
                  [{'mkey': '000000001', 'napgi': '2026-08-31', 'gubun': '정기분', 'notice_number': '', 'total_usage_ton': 60, 'bugwa_amount_won': 900}],
                  ['mkey', 'napgi', 'gubun', 'notice_number', 'total_usage_ton', 'bugwa_amount_won'], ['mkey', 'napgi', 'gubun', 'notice_number'])
        # 과거 청구 이력은 저장소가 아니라 이력 폴더(APP_DATA_DIR/history)에서 읽는다.
        pd.DataFrame([{'고객번호': '000000002', '역명': '길동역', '영업사업소': '사업소', '용도': '시민용', '납기일': '2026-06-30',
                       '구분': '정기분', '총사용량_톤': 40, '부과금액_원': 500}]).to_csv(
            self.path / 'history/billing/bills_clean.csv', index=False, encoding='utf-8-sig') if (self.path / 'history/billing').mkdir(parents=True) is None else None
        args = Namespace(runtime_dir=self.path, start=date(2026, 6, 1), end=date(2026, 6, 1),
                         water=False, ridership=False, bills=False, model=True, train_end=None, valid_end=None, test_end=None, jobs=1)
        with patch('back.api.catalog.collector_meters', return_value=METERS), \
             patch('back.api.catalog.data_sources', return_value=({}, {})), \
             patch.object(refresh, 'build_dataset', return_value=(master, riders, [], set())), \
             patch.object(refresh, 'build_calendar'), patch.object(refresh, 'HISTORY', self.path / 'history'):
            refresh.run(args)
        config = (self.path / 'dataset/config.yaml').read_text()
        self.assertIn(str((self.path / 'processed/water_bills.csv').resolve()), config)
        built = pd.read_csv(self.path / 'processed/water_bills.csv', encoding='utf-8-sig', dtype={'고객번호': str})
        self.assertIn('2026-08', set(built[built['고객번호'] == '000000001']['종료연월']))
        self.assertGreater(built['고객번호'].nunique(), 1)   # the historical export stays in the baseline

    def test_model_refresh_that_cannot_run_keeps_the_previous_risk_and_fails(self):
        master = pd.DataFrame({'고객번호': ['000000001'], '날짜': ['2026-06-01'], '일사용량_톤': [12.]})
        riders = pd.DataFrame({'고객번호': ['000000001'], '날짜': ['2026-06-01'], '총승객수': [30.]})
        risk = {'000000001': [{'date': '2026-05-31', 'severity': '주의'}]}
        args = Namespace(runtime_dir=self.path, start=date(2026, 6, 1), end=date(2026, 6, 1),
                         water=False, ridership=False, bills=False, model=True, train_end=None, valid_end=None, test_end=None, jobs=1)
        # No meter has enough history, so the requested model run is impossible.
        with patch('back.api.catalog.collector_meters', return_value=METERS), \
             patch('back.api.catalog.data_sources', return_value=({}, risk)), \
             patch.object(refresh, 'build_dataset', return_value=(master, riders, [], set())), \
             patch.object(refresh, 'build_calendar'):
            self.assertEqual(refresh.run(args), 1)
        manifest = json.loads((self.path / 'snapshot/manifest.json').read_text())
        self.assertEqual(json.loads((self.path / 'snapshot' / manifest['directory'] / 'risk.json').read_text()), risk)
        self.assertIn('이전 위험도', json.loads((self.path / 'refresh_report.json').read_text())['errors'][0])

    def test_empty_previous_split_does_not_poison_the_default_boundaries(self):
        processed = self.path / 'processed'
        processed.mkdir()
        (self.path / 'billing').mkdir()
        dates = pd.date_range('2025-01-01', periods=110).strftime('%Y-%m-%d')
        pd.DataFrame([{'고객번호': '1', '검침일': day, '일사용량_톤': 10} for day in dates]).to_csv(processed / 'daily_water_usage.csv', index=False)
        pd.DataFrame([{'고객번호': '1', '사용일': dates[-1], '승차총승객수': 30, '하차총승객수': 40}]).to_csv(processed / 'ridership.csv', index=False)
        pd.DataFrame([{'고객번호': '1', '일일CSV파일명': '옛이름'}]).to_csv(self.path / 'billing/meter_match.csv', index=False)
        build = lambda **kw: build_dataset(self.path / 'output', meters=METERS, data_root=self.path, daily_root=self.path / 'daily', **kw)
        # Boundaries this early leave no meter eligible, so the split files are written empty.
        self.assertEqual(build(train_end=dates[10], valid_end=dates[20])[3], set())
        self.assertEqual(self.rows(self.path / 'output/train.csv'), [])
        # The next default run must not read "nan" from those empty files; it falls back to the default cutoffs.
        with patch.multiple(refresh, DEFAULT_TRAIN_END=dates[89], DEFAULT_VALID_END=dates[103]):
            self.assertEqual(build()[3], {'000000001'})
        self.assertEqual(self.rows(self.path / 'output/train.csv')[-1]['날짜'], dates[89])
        # Not the old example split in the repository: the cutoffs agreed on 2026-10-21.
        self.assertEqual((refresh.DEFAULT_TRAIN_END, refresh.DEFAULT_VALID_END), ('2026-04-30', '2026-08-31'))
        with self.assertRaises(ValueError):
            build(train_end='nan', valid_end=dates[103])

    def test_dataset_joins_customer_numbers_and_withholds_new_contract(self):
        processed = self.path / 'processed'
        processed.mkdir()
        (self.path / 'billing').mkdir()
        dates = pd.date_range('2025-01-01', periods=110).strftime('%Y-%m-%d')
        readings = pd.date_range('2025-01-02', periods=110).strftime('%Y-%m-%d')   # 검침일 = 사용일 + 1
        water = [{'고객번호': '1', '검침일': day, '일사용량_톤': 10} for day in readings]
        water[-1]['일사용량_톤'] = 3186
        water.append({'고객번호': '2', '검침일': readings[-1], '일사용량_톤': 20})
        pd.DataFrame(water).to_csv(processed / 'daily_water_usage.csv', index=False)
        pd.DataFrame([{'고객번호': '1', '사용일': dates[-1], '승차총승객수': 30, '하차총승객수': 40}]).to_csv(processed / 'ridership.csv', index=False)
        pd.DataFrame([{'고객번호': '1', '일일CSV파일명': '옛이름'}]).to_csv(self.path / 'billing/meter_match.csv', index=False)
        master, _, withheld, eligible = build_dataset(self.path / 'output', meters=METERS,
            data_root=self.path, daily_root=self.path / 'daily', train_end=dates[89], valid_end=dates[103])
        self.assertEqual(eligible, {'000000001'})
        last = master[master['고객번호'] == '000000001'].iloc[-1]
        self.assertEqual((last['날짜'], last['일사용량_톤'], last['총승객수']), (dates[-1], 3186, 70))
        self.assertEqual(withheld[0]['customer_number'], '000000002')
        new = master[master['고객번호'] == '000000002'].iloc[0]
        self.assertEqual(new['역명'], '길동역')
        self.assertTrue(pd.isna(new['총승객수']))
        # Without explicit boundaries the previous runtime split is kept, so daily reruns use the same model split.
        build_dataset(self.path / 'output', meters=METERS, data_root=self.path, daily_root=self.path / 'daily')
        self.assertEqual(self.rows(self.path / 'output/train.csv')[-1]['날짜'], dates[89])
        self.assertEqual(self.rows(self.path / 'output/valid.csv')[-1]['날짜'], dates[103])
        # An evaluation cut-off shortens only the test split; later observations stay in master.
        capped, *_ = build_dataset(self.path / 'output', meters=METERS, data_root=self.path,
            daily_root=self.path / 'daily', train_end=dates[89], valid_end=dates[103], test_end=dates[106])
        self.assertEqual(capped['날짜'].max(), dates[-1])
        self.assertEqual(self.rows(self.path / 'output/test.csv')[-1]['날짜'], dates[106])
        with self.assertRaises(ValueError):
            build_dataset(self.path / 'output', meters=METERS, data_root=self.path, daily_root=self.path / 'daily',
                          train_end=dates[89], valid_end=dates[103], test_end=dates[103])

    def test_unchanged_remote_reading_marks_zero_runs_and_the_first_moving_day(self):
        days = pd.date_range('2026-06-01', periods=80).strftime('%Y-%m-%d').tolist()
        frame = lambda cid, values, dates=days: pd.DataFrame({'고객번호': cid, '날짜': dates[:len(values)], '일사용량_톤': values})
        gap = days[:30] + days[32:]   # days[30], days[31] 수집 안 됨
        outage = days[:48] + days[53:]   # days[48]~days[52] 수집 안 됨
        master = pd.concat([
            frame('000000001', [10.] * 28 + [0.] * 5 + [42.] + [10.] * 16),   # 0톤 5일, 그다음 날 다시 움직임
            frame('000000002', [10.] * 28 + [0.] * 2 + [10.] * 20),           # 이틀 0톤은 그대로 둔다
            frame('000000003', [1.] * 28 + [0.] * 5 + [1.] * 17),             # 평소 3톤 미만이면 0톤이 정상일 수 있다
            frame('000000004', [10.] * 28 + [0.] * 4 + [10.] * 16, gap),      # 빠진 날이 있어도 0톤이 이어지면 같은 구간
            frame('000000005', [10.] * 30 + [0.] * 3 + [10.] * 15, gap),      # 빠진 날 바로 다음 0톤은 구간의 시작이 아니다
            frame('000000006', [10.] * 28 + [0.] * 25 + [10.] * 22, outage),  # 20일 멈춘 뒤 5일 수집이 끊겨도 같은 구간
            frame('000000007', [10.] * 28 + [0.] * 3 + [10.] * 19),           # 정확히 사흘
            frame('000000008', [3.] * 28 + [0.] * 3 + [3.] * 19),             # 평소 정확히 3톤
            frame('000000009', [2., 3.] * 14 + [0.] * 3 + [3.] * 19),         # 평소 2.5톤
            # 20일 멈춤 → 사흘 움직임 → 다시 멈춤: 앞선 멈춤의 0톤은 평소량에 넣지 않는다
            frame('000000010', [10.] * 28 + [0.] * 20 + [10.] * 3 + [0.] * 5 + [10.] * 24),
        ], ignore_index=True)
        found = refresh.stalled_days(master)
        self.assertEqual(sorted({cid for cid, _ in found}), ['000000001', '000000004', '000000006', '000000007', '000000008', '000000010'])
        self.assertEqual(found[('000000001', days[28])], {'reason': '값 그대로', 'since': days[28], 'days': 1, 'usual': 10.0})
        self.assertEqual(found[('000000001', days[32])]['days'], 5)
        self.assertEqual(found[('000000001', days[33])], {'reason': '다시 움직인 첫날', 'since': days[28], 'days': 5, 'usual': 10.0})
        self.assertEqual(len([key for key in found if key[0] == '000000001']), 6)
        self.assertEqual([(day, found[('000000004', day)]['days']) for day in (days[28], days[29], days[32], days[33])],
                         [(days[28], 1), (days[29], 2), (days[32], 5), (days[33], 6)])
        self.assertEqual(found[('000000004', days[34])]['reason'], '다시 움직인 첫날')
        self.assertEqual([found[('000000006', day)]['days'] for day in (days[28], days[47], days[53], days[57])], [1, 20, 26, 30])
        self.assertEqual(found[('000000006', days[58])], {'reason': '다시 움직인 첫날', 'since': days[28], 'days': 30, 'usual': 10.0})
        self.assertEqual(len([key for key in found if key[0] == '000000006']), 26)
        self.assertEqual([found[('000000007', day)]['days'] for day in days[28:31]], [1, 2, 3])
        self.assertEqual(found[('000000007', days[31])]['reason'], '다시 움직인 첫날')
        self.assertEqual(len([key for key in found if key[0] == '000000007']), 4)
        self.assertEqual(found[('000000008', days[30])], {'reason': '값 그대로', 'since': days[28], 'days': 3, 'usual': 3.0})
        self.assertEqual(found[('000000010', days[48])]['reason'], '다시 움직인 첫날')
        self.assertNotIn(('000000010', days[49]), found)
        self.assertEqual(found[('000000010', days[51])], {'reason': '값 그대로', 'since': days[51], 'days': 1, 'usual': 10.0})
        self.assertEqual(found[('000000010', days[56])], {'reason': '다시 움직인 첫날', 'since': days[51], 'days': 5, 'usual': 10.0})
        self.assertEqual(refresh.stalled_days(master.iloc[0:0]), {})

    def test_usual_is_the_median_of_the_28_unmarked_observations_before(self):
        days = pd.date_range('2026-06-01', periods=120).strftime('%Y-%m-%d').tolist()
        frame = lambda cid, values, dates=days: pd.DataFrame({'고객번호': cid, '날짜': dates[:len(values)], '일사용량_톤': [float(v) for v in values]})
        master = pd.concat([
            frame('000000001', [1] * 40 + [10] * 15 + [1] * 13 + [0] * 3 + [10]),          # 직전 28관측: 10톤 15번, 1톤 13번 → 10
            frame('000000002', [10] * 33 + [0] * 3 + [10], days[:30] + days[55:]),           # 달력 28일이 아니라 28관측
            frame('000000003', [0, 5] * 14 + [0] * 4 + [5]),                                 # 표시하지 않은 0톤은 평소량에 든다 → 2.5
            frame('000000004', [2] * 14 + [4] * 14 + [0] * 3 + [100] + [0] * 3 + [10]),     # 다시 움직인 첫날은 평소량에서 뺀다 → 3
            frame('000000005', [10] * 5 + [0] * 5 + [10]),                                   # 앞선 관측이 7개 미만
            frame('000000006', [10] * 10 + [0] * 5 + [10]),
            frame('000000007', [10] * 28 + [0] * 30 + [10]),
            frame('000000008', [1] * 32 + [10] * 28 + [0] * 3 + [10]),                       # 표시한 날은 계량기마다 따로
        ], ignore_index=True)
        found = refresh.stalled_days(master)
        self.assertEqual(found[('000000001', days[68])]['usual'], 10.0)
        self.assertIn(('000000002', days[58]), found)
        self.assertNotIn(('000000003', days[28]), found)
        self.assertEqual(found[('000000004', days[32])]['usual'], 3.0)
        self.assertNotIn(('000000005', days[5]), found)
        self.assertIn(('000000006', days[10]), found)
        self.assertEqual(found[('000000008', days[60])]['usual'], 10.0)

    def test_missing_days_around_a_run(self):
        days = pd.date_range('2026-06-01', periods=70).strftime('%Y-%m-%d').tolist()
        frame = lambda cid, values, dates: pd.DataFrame({'고객번호': cid, '날짜': dates[:len(values)], '일사용량_톤': [float(v) for v in values]})
        master = pd.concat([
            frame('000000001', [10] * 28 + [0] * 25 + [10] * 5, days[:48] + days[58:]),   # 멈춘 중 열흘 수집 안 됨
            frame('000000002', [10] * 28 + [0] * 4 + [10] * 5, days[:28] + days[30:]),   # 빠진 날 바로 다음 0톤부터
            frame('000000003', [10] * 28 + [0] * 5 + [40] + [10] * 5, days[:33] + days[36:]),   # 마지막 0톤과 움직인 날 사이가 빔
            frame('000000004', [10] * 28 + [0, 0] + [10] * 3, days[:29] + days[30:]),     # 0톤 관측 둘(달력 사흘)
        ], ignore_index=True)
        found = refresh.stalled_days(master)
        self.assertEqual(found[('000000001', days[63])], {'reason': '다시 움직인 첫날', 'since': days[28], 'days': 35, 'usual': 10.0})
        self.assertEqual(sorted(d for c, d in found if c == '000000002'), days[31:35])   # 둘째 0톤에서 시작
        self.assertEqual(found[('000000003', days[36])], {'reason': '다시 움직인 첫날', 'since': days[28], 'days': 5, 'usual': 10.0})
        self.assertFalse(any(c == '000000004' for c, _ in found))

    def test_unchanged_remote_readings_are_left_out_of_every_split(self):
        processed = self.path / 'processed'
        processed.mkdir()
        (self.path / 'billing').mkdir()
        dates = pd.date_range('2025-01-01', periods=140).strftime('%Y-%m-%d')
        readings = pd.date_range('2025-01-02', periods=140).strftime('%Y-%m-%d')   # 검침일 = 사용일 + 1
        usage = [10] * 140
        usage[40:44] = [0] * 4                  # 학습 기간의 멈춤
        usage[115:119] = [0] * 4                # 검증 기간의 멈춤
        usage[130:135] = [0, 0, 0, 0, 30]       # 채점 기간의 멈춤과 다시 움직인 첫날
        rows = [{'고객번호': '1', '검침일': day, '일사용량_톤': v} for day, v in zip(readings, usage)]
        rows += [{'고객번호': '2', '검침일': day, '일사용량_톤': 10} for day in readings]   # 멈추지 않은 계량기는 그대로
        pd.DataFrame(rows).to_csv(processed / 'daily_water_usage.csv', index=False)
        pd.DataFrame([{'고객번호': '1', '사용일': dates[-1], '승차총승객수': 30, '하차총승객수': 40}]).to_csv(processed / 'ridership.csv', index=False)
        pd.DataFrame([{'고객번호': '1', '일일CSV파일명': '옛이름'}]).to_csv(self.path / 'billing/meter_match.csv', index=False)
        master, _, _, eligible = build_dataset(self.path / 'output', meters=METERS, data_root=self.path,
                                               daily_root=self.path / 'daily', train_end=dates[109], valid_end=dates[129])
        self.assertEqual((len(master), eligible), (280, {'000000001', '000000002'}))
        mine = lambda name: [r['날짜'] for r in self.rows(self.path / 'output' / f'{name}.csv') if r['고객번호'] == '000000001']
        dropped = set(dates[40:45]) | set(dates[115:120]) | set(dates[130:135])   # 멈춘 날과 다시 움직인 첫날
        self.assertEqual(mine('train'), [day for day in dates[:110] if day not in dropped])
        self.assertEqual(mine('valid'), [day for day in dates[110:130] if day not in dropped])
        self.assertEqual(mine('test'), list(dates[135:]))
        self.assertEqual(len([r for r in self.rows(self.path / 'output/test.csv') if r['고객번호'] == '000000002']), 10)

    def test_history_reading_dates_become_the_usage_days_of_collected_files(self):
        processed = self.path / 'processed'
        processed.mkdir()
        (self.path / 'billing').mkdir()
        # The readings of 03-01..03-05 measured the water used on 02-28..03-04.
        pd.DataFrame([{'고객번호': '1', '검침일': f'2026-03-0{d}', '일사용량_톤': 9 + d} for d in range(1, 6)]).to_csv(
            processed / 'daily_water_usage.csv', index=False)
        pd.DataFrame([{'고객번호': '1', '사용일': '2026-03-02', '승차총승객수': 30, '하차총승객수': 40}]).to_csv(
            processed / 'ridership.csv', index=False)
        pd.DataFrame([{'고객번호': '1', '일일CSV파일명': '옛이름'}]).to_csv(self.path / 'billing/meter_match.csv', index=False)
        water = self.path / 'daily' / 'water'
        water.mkdir(parents=True)
        # The collector keys the same readings by 사용일 (검침일자 = 사용일 + 1) and continues after the history.
        for day, value in [('2026-03-03', 13), ('2026-03-04', 14), ('2026-03-05', 15), ('2026-03-06', 16)]:
            pd.DataFrame([{'고객번호': '000000001', '역명': '강동역', '사용일': day, '일사용량(톤)': value}]).to_csv(
                water / f'{day}.csv', index=False)
        master, *_ = build_dataset(self.path / 'output', meters=METERS[:1], data_root=self.path,
                                   daily_root=self.path / 'daily', train_end='2026-03-01', valid_end='2026-03-03')
        self.assertEqual(master['날짜'].tolist(), ['2026-02-28', '2026-03-01', '2026-03-02', '2026-03-03',
                                                 '2026-03-04', '2026-03-05', '2026-03-06'])
        self.assertEqual(master['일사용량_톤'].tolist(), [10, 11, 12, 13, 14, 15, 16])   # no day lost or counted twice
        day = master[master['날짜'] == '2026-03-02'].iloc[0]
        self.assertEqual((day['총승객수'], day['요일']), (70, '월'))   # water and riders of the same day

    @unittest.skipUnless((ROOT / 'archive/data/arisu_station_history').is_dir(), 'archive/ was removed from the repository')
    def test_history_dates_are_arisu_reading_dates_one_day_after_use(self):
        # Arisu's history export lists each billing period's usage days beside its readings, which run
        # from start+1 to end+1. data/processed keeps exactly those reading dates as 검침일.
        names = pd.read_csv(ROOT / 'data/billing/meter_match.csv', encoding='utf-8-sig', dtype=str)
        alias = dict(zip(names['일일CSV파일명'], names['고객번호'].str.zfill(9)))
        export = pd.concat([pd.read_csv(p, encoding='utf-8-sig').assign(고객번호=alias[p.stem]) for p in
                            sorted((ROOT / 'archive/data/arisu_station_history').glob('*.csv'))], ignore_index=True)
        period = export['사용일'].str.extract(r'(\d{4}-\d{2}-\d{2}) ~ (\d{4}-\d{2}-\d{2})')
        used = pd.to_datetime(export['검침일자']) - pd.Timedelta(days=1)
        self.assertTrue(used.between(pd.to_datetime(period[0]), pd.to_datetime(period[1])).all())
        history = pd.read_csv(ROOT / 'data/processed/daily_water_usage.csv', encoding='utf-8-sig', dtype={'고객번호': str})
        found = history.assign(고객번호=history['고객번호'].str.zfill(9)).merge(
            export.drop_duplicates(['고객번호', '검침일자']), left_on=['고객번호', '검침일'], right_on=['고객번호', '검침일자'])
        self.assertEqual(len(found), len(history))
        self.assertTrue((found['일사용량_톤'] == found['일사용량(톤)']).all())


if __name__ == '__main__':
    unittest.main()
