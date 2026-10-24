"""아리수 원격검침·주변 사건 검색: 권한과 출처는 서버가 정하고, 모델 문장은 근거와 맞을 때만 통과한다."""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from back.api import chat_agent, collection, config, data_access, db
from back.pipelines.common import write_json
from back.pipelines.daily_water.smart_meter import write_arisu_index
from back.rag import storage as storage_module
from back.rag.tools import TOOL_SCHEMAS, ChatTools
from test_chat_agent import answer, response, route, tool
from test_chat_storage import encoded, make_store

CARD = {'leak_status': '주의', 'meter_status': None, 'realtime_usage': 6.0, 'updated_at': '2026-10-24 00:00',
        'collected_at': '2026-10-24T12:00:05.1+09:00', 'notice': None,
        'readings': [{'date': '2026-10-21', 'measured_at': '2026-10-22 00:00', 'status': '정상'},
                     {'date': '2026-10-22', 'measured_at': None, 'status': '통신 장애'},
                     {'date': '2026-10-23', 'measured_at': '2026-10-24 00:00', 'status': '정상'}]}


def index(**meters):
    return {'generated_at': '2026-10-24T12:00:05+09:00', 'meters': meters}


class ArisuToolTest(unittest.TestCase):
    def setUp(self):
        storage_module._CACHE.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(config, 'APP_DB_PATH', Path(self.temp.name) / 'app.db')
        self.patch.start()
        db.init_db()
        with db.connect() as c:
            for office in ('동부', '서부'):
                c.execute('INSERT INTO offices VALUES(?,?)', (office, office))
            for station in ('동부역', '서부역', '청구역'):
                c.execute('INSERT INTO stations VALUES(?,?,NULL,NULL)', (station, station))
            for mid, station, office, daily in [('000000001', '동부역', '동부', 1), ('000000002', '서부역', '서부', 1),
                                                ('000000004', '동부역', '동부', 0), ('000000005', '청구역', '동부', 0)]:
                c.execute('''INSERT INTO meters(id,provider,customer_number,station_id,office_id,line,display_name,
                           active,daily_enabled,deleted_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                          (mid, 'arisu', mid, station, office, '2', station + ' ' + mid[-1], 1, daily, None, db.now(), db.now()))
        self.store, self.fake = make_store({}, {})
        self.tools = ChatTools(['동부'], store=self.store)

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def arisu(self, tools=None, **args):
        return (tools or self.tools).execute('get_arisu_status', {'meter_ref': None, 'station': None, 'line': None,
                                                                  'start_date': None, 'end_date': None} | args)

    def test_missing_index_is_reported_as_missing_arisu_data(self):
        self.assertEqual(self.arisu(station='동부역')['code'], 'arisu_unavailable')

    def test_scoped_card_with_its_times_and_only_unusual_days(self):
        self.fake.objects['prod/arisu/index.json'] = encoded(index(**{'000000001': CARD, '000000002': CARD | {'leak_status': '경고'}}))
        out = self.arisu(station='동부역')
        self.assertEqual(len(out['meters']), 1)   # 청구 전용(4)은 원격검침 대상이 아니고 서부는 권한 밖
        row = out['meters'][0]
        self.assertEqual((row['arisu_leak_status'], row['arisu_realtime_usage_ton'], row['arisu_meter_status']), ('주의', 6.0, '정상'))
        self.assertEqual((row['arisu_card_updated_at'], row['arisu_checked_at']), ('2026-10-24 00:00', '2026-10-24 12:00'))
        self.assertIsNone(row['arisu_in_progress_day'])   # 지난 자정 검침은 그대로 기준 시각이다
        self.assertEqual(row['fault_readings'], [{'date': '2026-10-22', 'status': '통신 장애'}])
        summary = out['summary']
        # 기본 기간은 마지막 검침 사용일(10-23)까지 7일이다. 카드 갱신일(10-24)로 하루 밀리지 않는다.
        self.assertEqual((summary['start_date'], summary['end_date'], summary['match_count']), ('2026-10-17', '2026-10-23', 1))
        self.assertEqual(summary['leak_status_counts'], {'안전': 0, '주의': 1, '경고': 0})
        self.assertEqual((summary['realtime_usage_total_ton'], summary['realtime_usage_meter_count']), (6.0, 1))
        self.assertEqual(summary['top_realtime_usage'][0]['arisu_realtime_usage_ton'], 6.0)
        self.assertIn('LightGBM', out['note'])
        serialized = json.dumps(out, ensure_ascii=False)
        for private in ('000000001', '000000002', '서부'):
            self.assertNotIn(private, serialized)
        source = next(s for s in self.tools.sources if s['id'] == row['source_id'])
        self.assertEqual((source['kind'], source['meter_id']), ('arisu', '000000001'))
        self.assertIn('date=2026-10-23', source['href'])
        self.assertEqual(self.arisu(station='서부역')['code'], 'not_found')
        quiet = self.arisu(station='동부역', start_date='2026-10-23', end_date='2026-10-23')
        self.assertEqual((quiet['summary']['match_count'], quiet['meters'][0]['fault_readings']), (0, []))

    def test_a_time_after_the_check_is_the_day_in_progress_not_a_basis_time(self):
        # 운영 사례: 10-24 12:00 확인에 아리수가 그날 사용분(하루치가 아님)을 '10-25 00:00'으로 붙여 보냈다
        today = {'date': '2026-10-24', 'measured_at': '2026-10-25 00:00', 'status': '정상'}
        card = CARD | {'updated_at': '2026-10-25 00:00', 'realtime_usage': 9.0, 'readings': CARD['readings'] + [today]}
        self.fake.objects['prod/arisu/index.json'] = encoded(index(**{'000000001': card}))
        out = self.arisu(station='동부역')
        row = out['meters'][0]
        self.assertEqual((row['arisu_card_updated_at'], row['arisu_last_measured_at'], row['arisu_in_progress_day']), (None, None, '2026-10-24'))
        self.assertEqual((row['arisu_checked_at'], row['arisu_last_reading_date'], row['arisu_realtime_usage_ton']), ('2026-10-24 12:00', '2026-10-24', 9.0))
        self.assertNotIn('2026-10-25', json.dumps(out, ensure_ascii=False))   # 아직 오지 않은 시각은 모델에 가지 않는다
        summary = out['summary']
        self.assertEqual((summary['in_progress_day'], summary['in_progress_meter_count'], summary['card_updated_at_last']), ('2026-10-24', 1, None))
        self.assertEqual(summary['top_realtime_usage'][0]['arisu_in_progress_day'], '2026-10-24')
        source = next(s for s in self.tools.sources if s['id'] == row['source_id'])
        self.assertTrue(source['summary'].endswith('2026-10-24 진행 중(2026-10-24 12:00 확인)'))

    def test_billing_only_station_says_so_instead_of_a_missing_date(self):
        self.fake.objects['prod/arisu/index.json'] = encoded(index(**{'000000001': CARD}))
        self.assertEqual(self.arisu(station='청구역')['code'], 'billing_only')
        status = self.tools.execute('get_station_status', {'meter_ref': None, 'station': '청구역', 'line': None, 'date': None})
        self.assertEqual(status['code'], 'billing_only')

    def test_card_fault_is_the_meter_status_and_invalid_values_are_left_out(self):
        notice = '계량기 장애로 인해 원격검침 정보를 제공할 수 없습니다.'
        card = CARD | {'leak_status': None, 'meter_status': notice, 'notice': notice, 'realtime_usage': -1,
                       'updated_at': 'yesterday', 'readings': CARD['readings'] + [{'date': '2026-13-01', 'status': '정상'}]}
        self.fake.objects['prod/arisu/index.json'] = encoded(index(**{'000000001': card}))
        row = self.arisu(station='동부역')['meters'][0]
        self.assertEqual((row['arisu_leak_status'], row['arisu_meter_status'], row['arisu_notice']), ('표시 없음', notice, None))
        self.assertEqual((row['arisu_realtime_usage_ton'], row['arisu_card_updated_at']), (None, None))
        self.assertEqual(row['arisu_last_reading_date'], '2026-10-23')
        # 손상된 색인(아주 큰 수, 원문에 섞인 고객번호)도 조회를 깨뜨리거나 고객번호를 모델에 보내지 않는다.
        card = CARD | {'realtime_usage': 10 ** 400, 'leak_status': '주의 123456789'}
        self.fake.objects['prod/arisu/index.json'] = encoded(index(**{'000000001': card}))
        storage_module._CACHE.clear()
        out = self.arisu(tools=ChatTools(['동부'], store=self.store), station='동부역')
        self.assertIsNone(out['meters'][0]['arisu_realtime_usage_ton'])
        self.assertNotIn('123456789', json.dumps(out, ensure_ascii=False))

    def test_stuck_meter_rows_carry_since_and_days_from_the_risk_file(self):
        risk = {'000000001': [{'date': '2026-10-23', 'actual': 0.0, 'severity': '자료 확인', 'err': True, 'reason': '값 그대로',
                               'since': '2026-07-27', 'days': 89}]}
        store, _ = make_store({'000000001': {'usage': [{'date': '2026-10-23', 'value': 0.0}], 'ridership': []}}, risk)
        tools = ChatTools(['동부'], store=store)
        status = tools.execute('get_station_status', {'meter_ref': None, 'station': '동부역', 'line': None, 'date': None})
        row = next(row for row in status['meters'] if row['severity'] == '자료 확인')
        self.assertEqual((row['reason'], row['data_check_since'], row['data_check_days']), ('값 그대로', '2026-07-27', 89))
        history = tools.execute('get_station_history', {'meter_ref': None, 'station': '동부역', 'line': None, 'start_date': None, 'end_date': None})
        day = next(meter for meter in history['meters'] if meter['rows'])['rows'][-1]
        self.assertEqual((day['reason'], day['data_check_since'], day['data_check_days']), ('값 그대로', '2026-07-27', 89))

    def test_local_events_once_for_a_permitted_station_with_checked_sources_only(self):
        calls = []

        def web(station, day):
            calls.append((station, day))
            return [{'title': '단수 안내', 'date': '2026-10-23', 'place': '동부동', 'url': 'https://example.go.kr/notice/1'},
                    {'title': '나쁜 주소', 'date': '2026-10-23', 'place': '동부동', 'url': 'javascript:alert(1)'},
                    {'title': '날짜 오류', 'date': '23일', 'place': '동부동', 'url': 'https://example.go.kr/2'}]
        tools = ChatTools(['동부'], store=self.store, web=web)
        out = tools.execute('search_local_events', {'station': '동부역', 'date': '2026-10-23'})
        self.assertEqual(calls, [('동부역', '2026-10-23')])
        self.assertEqual([event['title'] for event in out['events']], ['단수 안내'])
        source = next(s for s in tools.sources if s['id'] == out['events'][0]['source_id'])
        self.assertEqual((source['kind'], source['url'], source['href']), ('web', 'https://example.go.kr/notice/1', None))
        self.assertTrue(tools.evidence[source['id']]['untrusted_document'])
        self.assertEqual(tools.execute('search_local_events', {'station': '동부역', 'date': None})['code'], 'web_search_limit')
        self.assertEqual(ChatTools(['동부'], store=self.store, web=web).execute(
            'search_local_events', {'station': '서부역', 'date': None})['code'], 'not_found')
        self.assertEqual(len(calls), 1)
        self.assertEqual(ChatTools(['동부'], store=self.store).execute(
            'search_local_events', {'station': '동부역', 'date': None})['code'], 'web_unavailable')
        failed = ChatTools(['동부'], store=self.store, web=lambda station, day: None).execute(
            'search_local_events', {'station': '동부역', 'date': '2026-10-23'})
        self.assertEqual((failed['events'], failed['searched']), ([], False))

    def test_cause_analysis_input_has_daily_arisu_statuses_without_customer_numbers(self):
        root = Path(self.temp.name)
        write_json(root / 'arisu/000000001.json', {'customer_number': '000000001', 'leak_status': '주의', 'meter_status': None,
                                                   'updated_at': '2026-10-24 00:00'})
        write_json(root / 'raw/remote_meter/000000001/statuses.json', {'customer_number': '000000001', 'readings': CARD['readings']})
        daily = {'000000001': {'usage': [{'date': '2026-10-23', 'value': 6}], 'ridership': []}}
        risk = {'000000001': [{'date': '2026-10-23', 'actual': 6, 'pred': 18, 'residual': -12, 'severity': '주의'}]}
        with patch.object(config, 'DATA_DIR', root), patch('back.api.catalog.data_sources', return_value=(daily, risk)):
            context = data_access.analysis_context('000000001', '2026-10-23')
        by_day = {row['날짜']: row['아리수_검침상태'] for row in context['history']}
        self.assertEqual((by_day['2026-10-22'], by_day['2026-10-23'], by_day['2026-10-20']), ('통신 장애', '정상', None))
        self.assertEqual(context['arisu'], {'누수위험': '주의', '계량기_상태': None, '갱신_시각': '2026-10-24 00:00'})
        self.assertNotIn('000000001', json.dumps(context, ensure_ascii=False))


class ArisuIndexTest(unittest.TestCase):
    def test_missing_chat_index_is_built_and_uploaded_when_the_worker_starts(self):
        # 배포 직후처럼 색인이 없으면 다음 상태 확인(최대 6시간 뒤)을 기다리지 않고 받아 둔 카드로 바로 만든다
        with tempfile.TemporaryDirectory() as folder, patch.object(config, 'DATA_DIR', Path(folder)), \
                patch('back.storage.hooks.sync_now') as sync:
            root = Path(folder)
            collection.ensure_arisu_index()   # 카드가 하나도 없으면 빈 색인을 만들지 않는다
            self.assertFalse((root / 'arisu/index.json').exists())
            write_json(root / 'arisu/000000001.json', {'customer_number': '000000001', 'leak_status': '주의'})
            collection.ensure_arisu_index()
            self.assertEqual(json.loads((root / 'arisu/index.json').read_text(encoding='utf-8'))['meters']['000000001']['leak_status'], '주의')
            sync.assert_called_once()
            collection.ensure_arisu_index()   # 이미 있으면 다시 만들지 않는다(수집 작업이 끝날 때마다 새로 쓴다)
            sync.assert_called_once()

    def test_index_keeps_matching_cards_and_about_three_months_of_readings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            write_json(root / 'arisu/000000001.json', {'customer_number': '000000001', 'leak_status': '주의', 'realtime_usage': 6})
            (root / 'arisu/000000002.json').write_text('{broken', encoding='utf-8')
            write_json(root / 'arisu/000000003.json', {'customer_number': '999999999', 'leak_status': '경고'})
            readings = [{'date': day, 'measured_at': None, 'status': '정상'} for day in ('2026-06-01', '2026-07-24', '2026-10-23')]
            write_json(root / 'raw/remote_meter/000000001/statuses.json', {'customer_number': '000000001', 'readings': readings})
            write_arisu_index(root)
            write_arisu_index(root)   # 다시 만들어도 자기 자신(index.json)은 계량기로 읽지 않는다
            data = json.loads((root / 'arisu/index.json').read_text(encoding='utf-8'))
        self.assertEqual(set(data['meters']), {'000000001'})
        meter = data['meters']['000000001']
        self.assertEqual((meter['leak_status'], meter['realtime_usage']), ('주의', 6))
        self.assertEqual([r['date'] for r in meter['readings']], ['2026-07-24', '2026-10-23'])


class SearchEventsTest(unittest.TestCase):
    def test_only_addresses_the_search_returned_and_dates_within_a_day(self):
        sources = [{'type': 'url', 'url': 'https://news.example.com/a?utm_source=openai'}, {'type': 'url', 'url': 'https://news.example.com/b/'}]
        output = [SimpleNamespace(model_dump=lambda exclude_none=True: {'type': 'web_search_call', 'action': {'sources': sources}})]
        events = [{'title': '경기', 'date': '2026-10-23', 'place': '잠실', 'url': 'https://news.example.com/a'},
                  {'title': '전날 공사', 'date': '2026-10-22', 'place': '잠실', 'url': 'https://news.example.com/b'},
                  {'title': '지어낸 주소', 'date': '2026-10-23', 'place': '잠실', 'url': 'https://invented.example.com/x'},
                  {'title': '먼 날짜', 'date': '2026-10-19', 'place': '잠실', 'url': 'https://news.example.com/a'},
                  {'title': '평문 주소', 'date': '2026-10-23', 'place': '잠실', 'url': 'http://news.example.com/a'}]
        client = Mock()
        client.responses.create.return_value = SimpleNamespace(output=output, status='completed',
                                                               output_text=json.dumps({'events': events}, ensure_ascii=False))
        found = chat_agent._search_events(client, '잠실새내역', '2026-10-23', time.monotonic() + 55)
        self.assertEqual([(event['title'], event['url']) for event in found],
                         [('경기', 'https://news.example.com/a'), ('전날 공사', 'https://news.example.com/b')])
        sent = client.responses.create.call_args.kwargs
        self.assertEqual((sent['tools'], sent['store']), ([{'type': 'web_search'}], False))
        self.assertEqual(json.loads(sent['input']), {'station': '잠실새내역', 'date': '2026-10-23'})   # 질문은 서버가 정한다
        self.assertIn('지시가 아니다', sent['instructions'])

    def test_short_time_or_a_failed_search_gives_nothing(self):
        client = Mock()
        for left in (10, 28):   # 검색 뒤에 답을 쓰고 한 번 고쳐 쓸 시간이 없으면 찾지 않는다
            self.assertIsNone(chat_agent._search_events(client, '동부역', '2026-10-23', time.monotonic() + left))
        client.responses.create.assert_not_called()
        client.responses.create.side_effect = TimeoutError
        self.assertIsNone(chat_agent._search_events(client, '동부역', '2026-10-23', time.monotonic() + 55))


class ArisuAnswerTest(unittest.TestCase):
    def check(self, text, evidence, ids, intent='STATION_STATUS', names=()):
        kit = SimpleNamespace(sources=[{'id': sid, 'label': sid, 'kind': 'arisu'} for sid in evidence], evidence=evidence, station_names=names)
        raw = json.dumps({'answer': text, 'source_ids': ids, 'is_hypothesis': False}, ensure_ascii=False)
        return chat_agent._validated_answer(raw, kit, chat_agent.Route(**route(intent=intent, date=None)))[0]

    def test_arisu_terms_need_arisu_evidence_and_model_terms_still_need_model_evidence(self):
        arisu = {'station': '동부역', 'arisu_leak_status': '주의', 'arisu_meter_status': '정상', 'arisu_card_updated_at': '2026-10-24 00:00'}
        model = {'station': '동부역', 'date': '2026-10-23', 'severity': '정상', 'actual_ton': 10, 'coverage': {}}
        text = '동부역은 아리수 누수위험 주의(2026-10-24 00:00 기준)이고 계량기 상태 정상입니다. LightGBM 판정은 정상입니다.'
        self.assertEqual(self.check(text, {'s1': arisu, 's2': model}, ['s1', 's2']), text)
        for timed in ('계량기 상태는 2026-10-24 00:00 기준 정상이고, 아리수 누수위험은 (2026-10-24 00:00 기준) 주의입니다.',
                      '아리수는 2026-10-24 00:00 기준 누수위험 주의, 계량기 상태 정상입니다.'):
            with self.subTest(timed=timed):
                self.assertEqual(self.check(timed, {'s1': arisu}, ['s1']), timed)
        for wrong in ('아리수 누수위험 경고입니다.', '아리수 누수위험 주의이고 LightGBM 판정은 경고입니다.'):
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                self.check(wrong, {'s1': arisu, 's2': model}, ['s1', 's2'])
        with self.assertRaises(ValueError):   # 아리수 근거 없이 '주의'는 LightGBM 판정으로 검사된다
            self.check('동부역 판정은 주의입니다.', {'s2': model}, ['s2'])

    def test_a_day_in_progress_is_written_with_its_day_and_check_time(self):
        arisu = {'station': '동부역', 'arisu_leak_status': '주의', 'arisu_meter_status': '정상', 'arisu_card_updated_at': None,
                 'arisu_in_progress_day': '2026-10-24', 'arisu_checked_at': '2026-10-24 12:01', 'arisu_realtime_usage_ton': 9.0}
        text = ('2026-10-24 진행 중(2026-10-24 12:01 확인), 동부역은 아리수 누수위험 주의이고 계량기 상태 정상입니다. '
                '실시간 사용량 9톤은 하루치가 아니라 그날 진행 중인 값입니다.')
        self.assertEqual(self.check(text, {'s1': arisu}, ['s1']), text)
        with self.assertRaises(ValueError):   # 아직 오지 않은 시각은 근거에 없어 쓸 수 없다
            self.check('2026-10-25 00:00 기준, 동부역은 아리수 누수위험 주의입니다.', {'s1': arisu}, ['s1'])

    def test_arisu_wording_cannot_hide_model_labels_or_counts(self):
        # 보안 검토에서 찾은 우회: 아리수 표현 뒤에 LightGBM 판정·건수를 붙여 검사를 피하던 답
        arisu = {'station': '동부역', 'arisu_leak_status': '안전', 'arisu_meter_status': '정상'}
        model = {'station': '동부역', 'counts': {'주의': 0, '경고': 5, '자료 확인': 0},
                 'coverage': {'expected_meter_days': 7, 'analyzed_meter_days': 7}}
        summary = {'leak_status_counts': {'안전': 5, '주의': 2, '경고': 0}, 'match_count': 0}
        evidence, ids = {'s1': arisu, 's2': model, 's3': summary}, ['s1', 's2', 's3']
        for text in ('동부역 아리수 누수위험 안전이며 경고 0건입니다.', '아리수 누수위험 주의 및 경고 7건입니다.',
                     '아리수 누수위험 주의 24곳입니다.', '누수 위험도 주의입니다.', '아리수 누수위험 주의·경고입니다.',
                     '아리수 누수위험 주의 2곳, 경고 3곳입니다.'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.check(text, evidence, ids, 'ANOMALY_SEARCH')
        for text in ('아리수 누수위험 주의·경고 2곳, LightGBM 경고 5건입니다.', '아리수 누수위험 경고 0곳입니다.',
                     '아리수 누수위험 주의 2곳, 경고 0곳입니다.'):
            with self.subTest(text=text):
                self.assertEqual(self.check(text, evidence, ids, 'ANOMALY_SEARCH'), text)
        with self.assertRaisesRegex(ValueError, 'Arisu count'):   # 계량기 행만으로는 아리수 건수를 확인할 수 없다
            self.check('아리수 누수위험 안전 1곳입니다.', {'s1': arisu}, ['s1'])

    def test_arisu_claims_must_match_the_cited_arisu_values(self):
        # 보안 재검토 N1: 아리수 근거와 다른 아리수 주장은 LightGBM 근거로 넘기지 않고 거부한다. 문장에 역 이름이 있으면 그 역 값과 맞춘다.
        warn = {'station': '동부역', 'arisu_leak_status': '경고', 'arisu_meter_status': '통신 장애'}
        safe = {'station': '서부역', 'arisu_leak_status': '안전', 'arisu_meter_status': '정상'}
        model = {'station': '동부역', 'date': '2026-10-23', 'severity': '정상', 'coverage': {'expected_meter_days': 1, 'analyzed_meter_days': 1}}
        summary = {'leak_status_counts': {'안전': 38, '주의': 0, '경고': 1}, 'match_count': 0}
        for text, evidence in (('동부역 아리수 누수위험 안전입니다.', {'s1': warn}),
                               ('동부역 계량기 상태 정상, LightGBM 판정 정상입니다.', {'s1': warn, 's2': model}),
                               ('동부역 아리수 누수위험 안전입니다.', {'s1': warn, 's3': summary}),
                               ('동부역 아리수 누수위험 경고는 없습니다.', {'s1': warn})):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'Arisu status'):
                self.check(text, evidence, list(evidence))
        # 이름이 겹치는 역('신용산'과 '용산', '강남구청'과 '강남')은 더 긴 이름으로 본다
        self.assertEqual([name for _, _, name in chat_agent._station_mentions('신용산역과 강남구청역, 강남역', ['용산역', '신용산역', '강남역', '강남구청역'])],
                         ['신용산역', '강남구청역', '강남역'])
        both = {'s1': warn, 's4': safe}
        for text, evidence in (('동부역은 아리수 누수위험 경고, 서부역은 아리수 누수위험 안전입니다. 서부역 계량기 상태는 정상입니다.', both),
                               ('아리수 누수위험 주의인 곳은 없습니다.', {'s3': summary}),
                               ('동부역 계량기 상태가 정상이 아닙니다.', {'s1': warn})):
            with self.subTest(text=text):
                self.assertEqual(self.check(text, evidence, list(evidence)), text)

    def test_fault_claims_are_checked_against_the_cited_fault_records(self):
        # 보안 재검토 N1: '장애가 없다'는 주장도 그 역·날짜·종류의 검침 장애 기록과 맞아야 한다
        row = {'station': '동부역', 'arisu_leak_status': '안전', 'arisu_meter_status': '정상',
               'fault_readings': [{'date': '2026-10-22', 'status': '통신 장애'}], 'fault_reading_count': 1, 'fault_status_counts': {'통신 장애': 1}}
        week = {'station': '동부역', 'start_date': '2026-10-17', 'end_date': '2026-10-23', 'match_count': 1,
                'leak_status_counts': {'안전': 1, '주의': 0, '경고': 0}, 'fault_status_counts': {'통신 장애': 1}}
        evidence, ids = {'s1': row, 's2': week}, ['s1', 's2']
        for text in ('통신 장애·계량기 장애 기록은 조회되지 않았습니다.', '2026-10-22 통신 장애 기록은 없습니다.', '검침 장애 0건입니다.',
                     '계량기 장애나 통신 장애는 없었습니다.', '통신 오류는 없었습니다.', '검침 이상 없음.'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'Arisu status'):
                self.check(text, evidence, ids)
        for text in ('계량기 장애 기록은 조회되지 않았습니다.', '2026-10-17 통신 장애·계량기 장애 기록은 조회되지 않았습니다.',
                     '2026-10-22 통신 장애가 1건 있었고, 계량기 장애는 없었습니다.', '통신 장애 여부는 확인할 수 없습니다.',
                     '다만 통신 장애가 이번 감소에 영향을 줬는지는 확인되지 않았습니다.',
                     '동부역에 통신 장애가 없었다고 답할 수는 없습니다. 2026-10-22 통신 장애 1건이 있습니다.'):
            with self.subTest(text=text):
                self.assertEqual(self.check(text, evidence, ids), text)
        # 두 역 비교: 문장에 역이 없으면 같은 줄 앞의 역으로 본다('6.0톤'의 소수점은 문장 끝이 아니다)
        quiet = {'station': '서부역', 'arisu_leak_status': '주의', 'arisu_meter_status': '정상', 'arisu_realtime_usage_ton': 6.0,
                 'fault_readings': [], 'fault_reading_count': 0, 'fault_status_counts': {}}
        pair = {'s1': row, 's4': quiet}
        text = '- 동부역: 2026-10-22 통신 장애 1건.\n- 서부역: 실시간 사용량 6.0톤, 계량기 상태 정상. 같은 기간 통신 장애·계량기 장애 기록은 조회되지 않았습니다.'
        self.assertEqual(self.check(text, pair, list(pair)), text)
        with self.assertRaisesRegex(ValueError, 'Arisu status'):
            self.check('- 서부역: 계량기 상태 정상.\n- 동부역: 실시간 사용량 6.0톤. 같은 기간 통신 장애 기록은 없습니다.', pair, list(pair))
        model = {'station': '동부역', 'date': '2026-10-19', 'severity': '경고', 'coverage': {'expected_meter_days': 1, 'analyzed_meter_days': 1}}
        with self.assertRaisesRegex(ValueError, 'Arisu status'):   # 아리수 근거 없이 장애가 없다고 할 수 없다
            self.check('통신 장애 기록은 없습니다.', {'s3': model}, ['s3'])
        with self.assertRaises(ValueError):   # '비정상'은 아리수 표현으로 빼지 않는다(재검토 N2). 지시로 쓰지 않게 한다.
            self.check('2026-10-19 판정은 경고이며 비정상 검침 기록은 조회되지 않았습니다.', {'s1': row, 's3': model}, ['s1', 's3'])
        with self.assertRaises(ValueError):   # 부정문이어도 판정처럼 읽히는 '정상'은 그대로 막는다
            self.check('그날 검침이 모두 정상이었다고 판단할 수는 없습니다.', {'s1': row, 's3': model}, ['s1', 's3'])

    def test_claims_about_a_station_use_that_station_or_are_rejected(self):
        # 보안 재확인(3차) 1~5: 역을 잘못 고르거나 못 골라 다른 계량기 값으로 통과하던 답
        network = {'leak_status_counts': {'안전': 38, '주의': 1, '경고': 1}, 'match_count': 0}
        east = {'station': '동부역', 'arisu_leak_status': '경고', 'arisu_meter_status': '정상', 'arisu_realtime_usage_ton': 6.5}
        gangnam = {'station': '강남역', 'arisu_leak_status': '안전', 'arisu_meter_status': '정상'}
        city = {'station': '강남구청역', 'arisu_leak_status': '경고', 'arisu_meter_status': '정상'}
        west = {'station': '서부역', 'leak_status_counts': {'안전': 1, '주의': 0, '경고': 0}}
        names = ['동부역', '서부역', '강남역', '강남구청역']
        for text, evidence in (('서부역은 아리수 누수위험 안전입니다.', {'s1': network, 's2': east}),   # 말한 역의 아리수 행이 없다
                               ('아리수 누수위험 안전인 역은 동부역입니다.', {'s1': network, 's2': east}),   # 역이 주장 뒤에 나온다
                               ('동부역은 실시간 사용량 6.5톤이고 아리수 누수위험 안전입니다.', {'s1': network, 's2': east}),   # 소수점
                               ('강남구청역은 아리수 누수위험 안전입니다.', {'s3': gangnam, 's4': city}),   # 겹치는 이름
                               ('동부역 아리수 누수위험 경고 0곳입니다.', {'s2': east, 's5': west})):   # 다른 역의 집계로 건수 확인
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.check(text, evidence, list(evidence), names=names)
        text = '아리수 누수위험 주의: 동부역. 강남역은 아리수 누수위험 안전입니다.'
        with self.assertRaises(ValueError):   # 목록의 역(동부역)도 그 역 값과 맞아야 한다
            self.check(text, {'s2': east, 's3': gangnam}, ['s2', 's3'], names=names)

    def test_station_lists_all_cited_values_and_plain_wordings(self):
        # 보안 재확인(4차) P1~P8: 거짓 '이상 없음'이나 근거와 반대인 상태는 거부한다
        east_warn = {'station': '동부역', 'arisu_leak_status': '경고', 'arisu_meter_status': '정상'}
        west_safe = {'station': '서부역', 'arisu_leak_status': '안전', 'arisu_meter_status': '정상'}
        east_safe = {'station': '동부역', 'arisu_leak_status': '안전', 'arisu_meter_status': '정상'}
        west_warn = {'station': '서부역', 'arisu_leak_status': '경고', 'arisu_meter_status': '정상'}
        network = {'leak_status_counts': {'안전': 38, '주의': 0, '경고': 1}, 'match_count': 0}
        model_ok = {'station': '동부역', 'date': '2026-10-23', 'severity': '정상', 'coverage': {'expected_meter_days': 1, 'analyzed_meter_days': 1}}
        faulty = {'station': '동부역', 'arisu_leak_status': '안전', 'arisu_meter_status': '통신 장애',
                  'fault_readings': [{'date': '2026-10-22', 'status': '통신 장애'}], 'fault_reading_count': 1, 'fault_status_counts': {'통신 장애': 1}}
        names = ['동부역', '서부역', '상수역', '청구역']
        for text, evidence in (('동부역과 서부역은 아리수 누수위험 안전입니다.', {'s1': east_warn, 's2': west_safe}),   # 앞쪽 목록
                               ('아리수 누수위험 안전인 역은 동부역, 서부역입니다.', {'s1': east_safe, 's2': west_warn}),   # 뒤쪽 목록
                               ('동부역은 아리수 누수위험 경고 상태가 아닙니다.', {'s1': east_warn}),   # '상태' 뒤의 부정
                               ('아리수 누수위험은 모두 안전입니다.', {'s1': east_warn, 's2': west_safe}),   # '모두'
                               ('동부역 계량기 상태는 모두 정상입니다.', {'s1': faulty, 's3': model_ok}),   # LightGBM '정상'으로 덮지 못한다
                               ('아리수 누수위험은 안전입니다.', {'s4': network}),   # 역 없는 주장은 인용한 모든 값에 맞아야 한다
                               ('동부역은 같은 기간 장애 기록은 없습니다.', {'s1': faulty}),   # 종류를 말하지 않은 '장애'
                               ('동부역은 모든 계량기가 정상적으로 검침되었습니다.', {'s1': faulty, 's3': model_ok})):   # 규칙 밖의 검침 '정상'
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.check(text, evidence, list(evidence), names=names)
        # 건수 뒤의 역 목록은 그 값에 속한 역이다: 건수는 전체 집계로, 역은 하나씩 확인한다
        listed = {'leak_status_counts': {'안전': 1, '주의': 1, '경고': 0}, 'station_names': ['동부역', '서부역'],
                  'leak_status_stations': {'주의': ['동부역'], '안전': ['서부역']}, 'match_count': 0}
        self.assertTrue(self.check('아리수 누수위험 주의 1곳: 동부역입니다.', {'s6': listed}, ['s6'], names=names))
        with self.assertRaisesRegex(ValueError, 'Arisu status'):
            self.check('아리수 누수위험 주의 1곳: 서부역입니다.', {'s6': listed}, ['s6'], names=names)
        # 계량기가 둘인 역(1호선 주의, 2호선 안전)은 '주의 목록'에 들 수 있지만, 그 역이 '안전'이라고 하지는 못한다
        mixed = {'leak_status_counts': {'안전': 1, '주의': 1, '경고': 0}, 'station_names': ['동부역'],
                 'leak_status_stations': {'주의': ['동부역'], '안전': ['동부역']}, 'match_count': 0}
        self.assertTrue(self.check('아리수 누수위험 주의 1곳: 동부역입니다.', {'s7': mixed}, ['s7'], names=names))
        with self.assertRaisesRegex(ValueError, 'Arisu status'):
            self.check('동부역은 아리수 누수위험 안전입니다.', {'s7': mixed}, ['s7'], names=names)
        # F1~F5: 맞는 문장은 통과한다
        east_summary = {'station': '동부역', 'leak_status_counts': {'안전': 1, '주의': 0, '경고': 0}, 'match_count': 0}
        for text, evidence in (('동부역 아리수 누수위험 안전입니다.', {'s5': east_summary}),   # 그 역 집계만 인용
                               ('동부역 상수도 사용량은 줄었고 아리수 누수위험은 안전입니다.', {'s1': east_safe}),   # '상수도'는 상수역이 아니다
                               ('동부역 결과입니다. 아리수 누수위험 경고 표시는 없습니다.', {'s1': east_safe}),   # 행만 인용한 부정
                               # '문제가 없다'는 아리수 값의 부정이 아니다(LightGBM 판정 없이 '문제 없음'은 원래 검사가 막는다)
                               ('동부역 아리수 누수위험은 안전으로 문제가 없습니다.', {'s1': east_safe, 's3': model_ok}),
                               ('동부역 계량기 상태 정상으로 이상 없습니다.', {'s1': east_safe, 's3': model_ok})):
            with self.subTest(text=text):
                self.assertEqual(self.check(text, evidence, list(evidence), names=names), text)

    def test_negations_after_the_claim_and_double_negations(self):
        # 보안 재확인(3차) 6~7: 몇 단어 뒤의 부정과 이중 부정
        east = {'station': '동부역', 'arisu_leak_status': '경고', 'arisu_meter_status': '통신 장애'}
        for text in ('동부역 아리수 누수위험 경고인 계량기는 한 곳도 없습니다.', '동부역 아리수 누수위험 경고는 확인되지 않았습니다.',
                     '계량기 상태가 정상이 아닌 곳은 없습니다.', '동부역은 아리수 누수위험 안전이 아닌 계량기가 없습니다.'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'Arisu status'):
                self.check(text, {'s1': east}, ['s1'])
        for text in ('동부역 아리수 누수위험 경고이며, 통신 장애 기록은 확인이 필요합니다.', '아리수 누수위험 경고로 표시되지만 실제 누수는 아닙니다.'):
            with self.subTest(text=text):
                self.assertEqual(self.check(text, {'s1': east}, ['s1']), text)

    def test_no_fault_claims_cover_more_wordings_the_card_and_cut_records(self):
        # 보안 재확인(3차) 8~9: '있었던 날이 없다', '0회', 띄어쓰기 없는 상태, 카드의 장애 표시, 잘린 장애 기록, 같은 주장 수백 개
        row = {'station': '동부역', 'arisu_leak_status': '안전', 'arisu_meter_status': '정상', 'start_date': '2026-07-01', 'end_date': '2026-10-23',
               'fault_readings': [{'date': '2026-10-22', 'status': '통신 장애'}], 'fault_reading_count': 1, 'fault_status_counts': {'통신 장애': 1}}
        squashed = row | {'fault_readings': [{'date': '2026-10-22', 'status': '계량기장애'}], 'fault_status_counts': {'계량기장애': 1}}
        card = row | {'arisu_meter_status': '계량기 장애로 인해 원격검침 정보를 제공할 수 없습니다.', 'fault_readings': [],
                      'fault_reading_count': 0, 'fault_status_counts': {}}
        cut = row | {'fault_reading_count': 40, 'fault_status_counts': {'통신 장애': 40}}
        for text, evidence in (('동부역은 통신 장애가 있었던 날이 없습니다.', row), ('동부역 통신 장애 0회입니다.', row),
                               ('동부역 계량기 장애 기록은 없습니다.', squashed), ('동부역은 계량기 장애가 없습니다.', card),
                               ('동부역 2026-07-24 통신 장애 기록은 없습니다.', cut)):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'Arisu status'):
                self.check(text, {'s1': evidence}, ['s1'])
        started = time.monotonic()
        with self.assertRaisesRegex(ValueError, 'Arisu status'):
            chat_agent._without_arisu_claims('2026-01-01~12-31 ' * 88 + '통신장애없. ' * 300, [row])
        self.assertLess(time.monotonic() - started, 1)

    def test_each_requested_station_needs_its_own_model_evidence_for_an_all_clear(self):
        # 보안 재검토 N4: 동부역의 LightGBM 판정이 서부역의 '이상 없음'을 뒷받침하지 못한다
        model = {'station': '동부역', 'date': '2026-10-23', 'severity': '경고', 'coverage': {'expected_meter_days': 1, 'analyzed_meter_days': 1}}
        arisu = {'station': '서부역', 'arisu_leak_status': '안전', 'arisu_meter_status': '정상'}
        kit = SimpleNamespace(sources=[{'id': 's1', 'label': 's1', 'kind': 'snapshot'}, {'id': 's3', 'label': 's3', 'kind': 'arisu'}],
                              evidence={'s1': model, 's3': arisu})
        classified = chat_agent.Route(**route(station=None, stations=['동부역', '서부역'], date=None))
        raw = json.dumps({'answer': '동부역은 경고입니다. 서부역은 이상 징후가 없습니다.', 'source_ids': ['s1', 's3'], 'is_hypothesis': False},
                         ensure_ascii=False)
        with self.assertRaisesRegex(ValueError, 'without analysis'):
            chat_agent._validated_answer(raw, kit, classified, primary_groups={'동부역': {'s1'}, '서부역': {'s3'}})
        # 보안 재확인(3차) 10: 서부역 자료가 있어도 분석한 날이 없으면 서부역의 '이상 없음'은 근거가 없다
        kit.evidence['s3'] = {'station': '서부역', 'coverage': {'expected_meter_days': 7, 'analyzed_meter_days': 0}}
        raw = json.dumps({'answer': '서부역은 최근 7일 이상 징후가 없습니다.', 'source_ids': ['s1', 's3'], 'is_hypothesis': False}, ensure_ascii=False)
        with self.assertRaisesRegex(ValueError, 'without analysis'):
            chat_agent._validated_answer(raw, kit, classified, primary_groups={'동부역': {'s1'}, '서부역': {'s3'}})
        # 보안 재검토 N5: 아리수 건수 앞에 아무 말이나 끼워 판정 없는 답의 '이상 없음'을 숨기지 못한다
        unjudged = {'coverage': {'expected_meter_days': 7, 'analyzed_meter_days': 0}}
        summary = {'leak_status_counts': {'안전': 38, '주의': 0, '경고': 0}, 'match_count': 0}
        with self.assertRaisesRegex(ValueError, 'without analysis'):
            self.check('아리수 누수위험 안전이며 이상없는 38곳입니다.', {'s1': unjudged, 's2': summary}, ['s1', 's2'], 'ANOMALY_SEARCH')

    def test_only_explicit_arisu_wording_lets_arisu_answer_alone(self):
        # 보안 재검토 N3: LightGBM 판정으로도 읽히는 질문은 아리수 근거만으로 답하지 못한다
        for text, expected in (('동부역 누수 위험 있어?', False), ('서부역 검침 기록 확인해줘', False), ('동부역 계량기 상태 어때?', False),
                               ('동부역 아리수 상태 알려줘', True), ('최근 통신 장애 난 역은?', True), ('실시간 사용량 얼마야?', True),
                               ('원격검침 값 보여줘', True)):
            with self.subTest(text=text):
                self.assertEqual(bool(chat_agent.ARISU_TOPIC.search(text)), expected)

    def test_no_analysis_still_allows_checked_arisu_status_but_not_an_all_clear(self):
        arisu = {'station': '동부역', 'arisu_leak_status': '안전', 'arisu_meter_status': '정상'}
        unjudged = {'station': '동부역', 'severity': '자료 확인', 'coverage': {'expected_meter_days': 7, 'analyzed_meter_days': 0}}
        text = '최근 7일은 모두 자료 확인입니다. 계량기 상태는 정상입니다.'
        self.assertEqual(self.check(text, {'s1': arisu, 's2': unjudged}, ['s1', 's2']), text)
        for wrong in ('최근 7일 이상 없습니다.', '최근 7일 판정은 정상입니다.'):
            with self.subTest(wrong=wrong), self.assertRaises(ValueError):
                self.check(wrong, {'s1': arisu, 's2': unjudged}, ['s1', 's2'])
        # LightGBM 자료를 하나도 인용하지 않은 답(아리수만 인용)도 판정이 없는 답으로 본다
        with self.assertRaisesRegex(ValueError, 'without analysis'):
            self.check('특이 사항이 없습니다.', {'s1': arisu}, ['s1'], 'ANOMALY_SEARCH')
        self.assertEqual(self.check('계량기 상태 정상입니다.', {'s1': arisu}, ['s1']), '계량기 상태 정상입니다.')

    def test_long_whitespace_or_repeated_labels_do_not_stall_the_validator(self):
        # 답은 3000자까지다. 예전 식은 공백 800자에 1초 넘게, '주의 '를 40번 넘게 되풀이하면 몇 시간씩 되추적했다.
        facts = [{'arisu_leak_status': '주의', 'arisu_meter_status': '정상', 'leak_status_counts': {'주의': 1}}]
        started = time.monotonic()
        for text in ('아리수 누수위험' + ' ' * 2900 + '끝', '아리수 누수위험' + '\n' * 2900 + '주의', '계량기 상태' + ' ' * 2900):
            chat_agent._without_arisu_claims(text, facts)
        for text in ('주의 ' * 900 + 'X', '주의·' * 900 + '1건'):
            list(chat_agent._count_claims(text))
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(list(chat_agent._count_claims('주의·경고는 총 3건, 경고 2건, 총 4건, 5회')),
                         [(('주의', '경고'), 3), (('경고',), 2), ((), 4)])

    def test_arisu_counts_must_match_the_aggregate(self):
        summary = {'leak_status_counts': {'주의': 2, '안전': 5}, 'match_count': 1}
        for text in ('아리수 누수위험 주의인 계량기는 2곳입니다.', '아리수 누수위험 주의는 계량기 기준 2곳입니다.', '아리수 누수위험 주의 표시 계량기는 2곳입니다.'):
            with self.subTest(text=text):
                self.assertEqual(self.check(text, {'s1': summary}, ['s1'], 'ANOMALY_SEARCH'), text)
        with self.assertRaisesRegex(ValueError, 'Arisu count'):
            self.check('아리수 누수위험 주의인 계량기는 5곳입니다.', {'s1': summary}, ['s1'], 'ANOMALY_SEARCH')

    def test_arisu_lookup_must_cover_the_requested_day_and_may_reach_a_week_around_it(self):
        day = chat_agent.Route(**route(intent='CAUSE_ANALYSIS', date='2026-10-23'))
        for first, last in (('2026-10-22', '2026-10-23'), ('2026-10-23', '2026-10-23'), ('2026-10-16', '2026-10-30')):
            chat_agent._check_arisu_range(day, {'start_date': first, 'end_date': last})
        for first, last in (('2026-10-15', '2026-10-23'), ('2026-10-22', '2026-10-22'), ('2026-10-24', '2026-10-25'), (None, '2026-10-23')):
            with self.subTest(first=first, last=last), self.assertRaises(ValueError):
                chat_agent._check_arisu_range(day, {'start_date': first, 'end_date': last})
        chat_agent._check_arisu_range(chat_agent.Route(**route(date=None)), {'start_date': None, 'end_date': None})

    def test_complete_period_with_billing_only_meters_is_not_called_partial(self):
        coverage = {'requested_start_date': '2026-10-17', 'requested_end_date': '2026-10-23', 'missing_usage_meter_days': 0,
                    'billing_only_meter_count': 4, 'usage_first_date': '2026-10-17', 'usage_last_date': '2026-10-23',
                    'usage_day_count': 7, 'stored_last_date': '2026-10-23'}
        kit = SimpleNamespace(evidence={'s1': {'coverage': coverage}})
        notice = chat_agent._coverage_notice(kit, [{'id': 's1', 'label': '집계'}], chat_agent.Route(**route(intent='TREND_ANALYSIS')))
        self.assertNotIn('전체 요청 기간의 집계가 아니며', notice)
        self.assertIn('일별 자료 미대상 계량기 4개', notice)
        # 청구서 계량기만 있어 자료가 아예 없으면 '확인할 수 없다'는 안내는 남긴다
        coverage.update(billing_only_meter_count=2, usage_first_date=None, usage_last_date=None, usage_day_count=0)
        notice = chat_agent._coverage_notice(kit, [{'id': 's1', 'label': '집계'}], chat_agent.Route(**route(intent='TREND_ANALYSIS')))
        self.assertIn('확인할 수 없습니다', notice)

    def test_overlapping_range_notices_are_given_once(self):
        def coverage(start, end, days, meters):
            return {'requested_start_date': start, 'requested_end_date': end, 'requested_days': days, 'selected_meter_count': meters,
                    'missing_analysis_meter_days': 2, 'billing_only_meter_count': 0, 'analysis_first_date': start,
                    'analysis_last_date': end, 'analysis_day_count': days}
        kit = SimpleNamespace(evidence={'s1': {'station': None, 'coverage': coverage('2026-10-17', '2026-10-17', 1, 76)},
                                        's2': {'station': None, 'coverage': coverage('2026-10-17', '2026-10-23', 7, 76)},
                                        's3': {'station': '동부역', 'coverage': coverage('2026-10-17', '2026-10-23', 7, 1)}})
        sources = [{'id': sid, 'label': sid} for sid in ('s1', 's2', 's3')]
        notice = chat_agent._coverage_notice(kit, sources, chat_agent.Route(**route(intent='ANOMALY_SEARCH', date=None)))
        self.assertEqual(notice.count('자료 범위 안내'), 2)   # 전체 7일(1일 조회는 그 안에 들어감)과 동부역
        self.assertIn('요청 기간 7일 모두 분석 자료가 있으나, 일부 계량기의 빠진 2건', notice)
        self.assertNotIn('전체 요청 기간의 집계가 아니며', notice)
        # 보안 재검토 N6: 전체 집계와 2호선 집계는 범위가 달라 둘 다 안내한다(2호선의 '자료 없음'을 지우지 않는다)
        line = coverage('2026-10-17', '2026-10-23', 7, 9) | {'analysis_first_date': None, 'analysis_last_date': None, 'analysis_day_count': 0}
        kit.evidence['s4'] = {'station': None, 'line': '2', 'coverage': line}
        notice = chat_agent._coverage_notice(kit, sources + [{'id': 's4', 'label': 's4'}], chat_agent.Route(**route(intent='ANOMALY_SEARCH', date=None)))
        self.assertEqual(notice.count('자료 범위 안내'), 3)
        self.assertIn('유효한 분석 자료가 없어', notice)
        # 보안 재확인(3차) 11: 같은 역의 다른 계량기 안내는 따로 쓴다(집계 안내가 없을 때)
        empty = coverage('2026-10-17', '2026-10-23', 7, 1) | {'analysis_first_date': None, 'analysis_last_date': None, 'analysis_day_count': 0}
        kit = SimpleNamespace(evidence={'s7': {'station': '서부역', 'meter_ref': 'm1', 'coverage': coverage('2026-10-17', '2026-10-23', 7, 1)},
                                        's8': {'station': '서부역', 'meter_ref': 'm2', 'coverage': empty}})
        notice = chat_agent._coverage_notice(kit, [{'id': 's7', 'label': 's7'}, {'id': 's8', 'label': 's8'}],
                                             chat_agent.Route(**route(intent='ANOMALY_SEARCH', date=None)))
        self.assertEqual(notice.count('자료 범위 안내'), 2)
        self.assertIn('유효한 분석 자료가 없어', notice)


class AlertListTest(unittest.TestCase):
    """주의·경고 역을 나열한 답은 그날의 주의·경고 역을 증가·감소와 상관없이 모두 써야 한다.
    운영 사례: '정상이 아닌 곳' 질문에 10-23 경고(안국역)만 쓰고 사용량이 줄어든 주의 두 곳을 뺐다."""
    ROWS = [{'station': '안국역', 'date': '2026-10-23', 'severity': '경고', 'deviation_direction': '증가', 'actual_ton': 48.0},
            {'station': '석촌역', 'date': '2026-10-23', 'severity': '주의', 'deviation_direction': '감소', 'actual_ton': 3.0},
            {'station': '잠실새내역', 'date': '2026-10-23', 'severity': '주의', 'deviation_direction': '감소', 'actual_ton': 22.0},
            {'station': '삼각지역', 'date': '2026-10-23', 'severity': '자료 확인', 'deviation_direction': None, 'actual_ton': 0.0},
            {'station': '온수역', 'date': '2026-10-22', 'severity': '경고', 'deviation_direction': '증가', 'actual_ton': 30.0}]

    def check(self, text, message='지금 아리수 실시간 누수 위험이 정상이 아닌곳', **changes):
        aggregate = {'start_date': '2026-10-17', 'end_date': '2026-10-23', 'period_days': 7,
                     'severity_counts': {'정상': 60, '주의': 2, '경고': 2, '자료 확인': 1},
                     'counts': {'주의': 2, '경고': 2, '자료 확인': 1}, 'match_count': 5, 'truncated': False, 'rows': self.ROWS} | changes
        kit = SimpleNamespace(sources=[{'id': 's1', 'label': 's1', 'kind': 'query'}], evidence={'s1': aggregate},
                              station_names=('안국역', '석촌역', '잠실새내역', '삼각지역', '온수역'))
        raw = json.dumps({'answer': text, 'source_ids': ['s1'], 'is_hypothesis': False}, ensure_ascii=False)
        return chat_agent._validated_answer(raw, kit, chat_agent.Route(**route(intent='ANOMALY_SEARCH', station=None, date=None)),
                                            message=message)[0]

    def test_the_day_named_needs_every_alert_station_up_or_down(self):
        for text in ('2026-10-23에는 안국역이 LightGBM 경고로 조회됐습니다. 사용량은 48톤입니다.',
                     '최신 자료일인 2026-10-23의 LightGBM 판정에서 사용량 증가 경고는 안국역입니다.',
                     '2026-10-23 경고는 안국역, 주의는 석촌역입니다.'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'Missing alert stations'):
                self.check(text)
        full = '2026-10-23 LightGBM 판정은 안국역 경고(사용량 증가), 석촌역·잠실새내역 주의(사용량 감소)입니다.'
        self.assertEqual(self.check(full), full)   # 그 전날(온수역)은 답에 쓴 날이 아니라 보지 않는다
        self.assertIn('증가·감소', chat_agent._repair_note(ValueError('Missing alert stations')))

    def test_a_question_about_one_label_or_one_direction_needs_only_those(self):
        self.check('2026-10-23 경고는 안국역입니다.', message='경고가 나온 역 알려줘')
        self.check('2026-10-23 사용량 증가 경고는 안국역입니다.', message='사용량이 늘어난 역 알려줘')
        with self.assertRaisesRegex(ValueError, 'Missing alert stations'):
            self.check('2026-10-23 사용량 감소 주의는 석촌역입니다.', message='사용량이 줄어든 역은?')
        self.check('2026-10-23에 LightGBM 주의·경고 판정이 있었습니다.')   # 역을 나열하지 않은 답은 건수 검사가 맡는다

    def test_a_period_or_no_date_needs_the_whole_lookup_except_a_cut_oldest_day(self):
        for text in ('2026-10-17~2026-10-23 동안 경고는 안국역입니다.', '최근 7일 동안 경고는 안국역입니다.'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'Missing alert stations'):
                self.check(text, message='최근 경고 나온 역')
        self.check('최근 7일 동안 경고는 안국역과 온수역입니다.', message='최근 경고 나온 역')
        # 실제 모델 답: 기간은 건수로 말하고, 따로 쓴 날짜(10-23)의 역만 나열했다. 그날 역을 다 썼으니 통과한다.
        self.check('최근 7일(2026-10-17~2026-10-23) 판정은 주의 2건, 경고 2건입니다. '
                   '최신 분석일인 2026-10-23에는 안국역 경고, 석촌역·잠실새내역 주의입니다.', message='최근 누수 의심되는 역 있어?')
        # 상세가 잘린 조회는 가장 오래된 날(10-22)이 일부만 왔을 수 있어 그날은 보지 않는다
        self.check('2026-10-17~2026-10-23 동안 경고는 안국역입니다.', message='최근 경고 나온 역', truncated=True)


class ArisuLoopTest(unittest.TestCase):
    """도구 선택은 서버 규칙을 따른다: 웹 검색은 원인 질문의 요청한 역에만, 고쳐 쓰기는 한 번만."""

    def setUp(self):
        self.conversation = {'id': 'a' * 32, 'user_id': 1, 'offices': ['east'], 'fingerprint': 'f', 'context': {}, 'messages': []}
        self.client, self.sent, self.responses = Mock(), [], []

        def create(**kwargs):
            self.sent.append(copy.deepcopy(kwargs))
            return self.responses.pop(0)
        self.client.responses.create.side_effect = create
        self.kit = SimpleNamespace(context={}, sources=[{'id': 's1', 'label': '동부역 운영 자료', 'kind': 'snapshot',
            'meter_id': 'secret-meter', 'date': '2026-08-31', 'href': '#/detail?meter=secret-meter', 'summary': '경고'}],
            evidence={'s1': {'station': '동부역', 'date': '2026-08-31', 'actual_ton': 20.0, 'predicted_ton': 10.0,
                             'severity': '경고', 'coverage': {}}})
        self.kit.execute = Mock(return_value={'meters': [self.kit.evidence['s1'] | {'source_id': 's1'}]})
        for target in (patch.object(chat_agent, '_check_scope'), patch('back.rag.tools.ChatTools', return_value=self.kit)):
            target.start()
            self.addCleanup(target.stop)

    def run_agent(self, text):
        return chat_agent._run(self.client, text, self.conversation, time.monotonic() + 55)[0]

    def test_web_search_only_for_the_requested_station_in_cause_questions(self):
        cause = route(intent='CAUSE_ANALYSIS', date=None)
        for classified, station in ((cause, '서부역'), (route(), '동부역')):
            with self.subTest(intent=classified['intent'], station=station):
                self.kit.execute.reset_mock()
                self.responses = [response(classified), response(output=[tool('search_local_events', {'station': station, 'date': None})]),
                                  response(answer())]
                self.run_agent('동부역 사용량이 왜 늘었어?')
                self.kit.execute.assert_not_called()
                self.assertIn('invalid_tool_call', self.sent[-1]['input'][-1]['output'])
        self.kit.execute.reset_mock()
        self.responses = [response(cause), response(output=[tool('search_local_events', {'station': '동부역', 'date': None})]),
                          response(output=[tool()]), response(answer())]
        self.run_agent('동부역 사용량이 왜 늘었어?')
        self.assertEqual(self.kit.execute.call_args_list[0].args, ('search_local_events', {'station': '동부역', 'date': None}))

    def test_arisu_only_evidence_answers_only_arisu_questions(self):
        self.kit.sources.append({'id': 's2', 'label': '아리수 집계', 'kind': 'query', 'meter_id': None, 'date': '2026-08-31',
                                 'href': None, 'summary': '아리수 검침 장애 0건'})
        self.kit.evidence['s2'] = {'leak_status_counts': {'안전': 1, '주의': 0, '경고': 0}, 'match_count': 0}
        self.kit.execute = Mock(return_value={'summary': self.kit.evidence['s2'] | {'source_id': 's2'}})
        call = tool('get_arisu_status', {'meter_ref': None, 'station': None, 'line': None, 'start_date': None, 'end_date': None})
        reply = answer(answer='아리수 누수위험 경고 0곳입니다.', source_ids=['s2'])
        for text, accepted in (('최근 이상 있는 역 알려줘', False), ('아리수 누수위험 경고인 역 알려줘', True)):
            with self.subTest(text=text):
                self.responses = [response(route(intent='ANOMALY_SEARCH', station=None, date=None)), response(output=[call]), response(reply)]
                result = self.run_agent(text)
                self.assertEqual(result['generated_by'] != 'none', accepted)

    def test_missing_arisu_data_still_lets_the_lightgbm_lookup_answer(self):
        missing = {'error': 'NCP에 아리수 원격검침 자료가 아직 없습니다.', 'code': 'arisu_unavailable'}
        self.kit.execute = Mock(side_effect=[missing, {'meters': [self.kit.evidence['s1'] | {'source_id': 's1'}]}])
        arisu = tool('get_arisu_status', {'meter_ref': None, 'station': '동부역', 'line': None, 'start_date': None, 'end_date': None})
        self.responses = [response(route(date=None)), response(output=[arisu]), response(output=[tool(call_id='call_2')]), response(answer())]
        result = self.run_agent('동부역 최근 통신 장애 있었어?')
        self.assertEqual(result['generated_by'], chat_agent.config.OPENAI_MODEL)   # 정해진 '아리수 자료 없음' 안내로 끝나지 않는다
        self.assertIn('arisu_unavailable', self.sent[1]['instructions'])

    def test_web_search_date_without_a_requested_day_stays_in_the_recent_week_of_the_lookup(self):
        self.kit.context = {'date': '2026-08-31'}
        for day, allowed in (('2026-08-24', False), ('2026-08-25', True), ('2026-09-01', False), ('2026-08-31', True)):
            with self.subTest(day=day):
                self.kit.execute.reset_mock()
                self.responses = [response(route(intent='CAUSE_ANALYSIS', date=None)),
                                  response(output=[tool('search_local_events', {'station': '동부역', 'date': day})]),
                                  response(output=[tool()]), response(answer())]
                self.run_agent('동부역 사용량이 왜 늘었어?')
                names = [call.args[0] for call in self.kit.execute.call_args_list]
                self.assertEqual('search_local_events' in names, allowed)
        # 보안 재검토: 끝이 열린 기간('10월 1일부터')은 이번 조회의 기준일까지만 검색한다
        start_only = chat_agent.Route(**route(intent='CAUSE_ANALYSIS', date=None, start_date='2026-10-01'))
        self.assertFalse(chat_agent._web_day_allowed(start_only, {}, '2099-12-31'))
        self.assertTrue(chat_agent._web_day_allowed(start_only, {'date': '2026-10-23'}, '2026-10-19'))
        self.assertFalse(chat_agent._web_day_allowed(start_only, {'date': '2026-10-23'}, '2026-10-24'))

    def test_no_repair_on_the_last_round_so_the_evidence_is_still_shown(self):
        self.responses = [response(route()), response(output=[tool()]), response(answer(source_ids=['s1配']))]
        with patch.object(chat_agent, 'MAX_ROUNDS', 2):
            result = self.run_agent('동부역 현황은?')
        self.assertTrue(result['answer'].startswith('답변 문장을 확인하지 못해'))
        self.assertEqual(len(self.sent), 3)

    def test_progress_reports_fixed_stage_codes_in_order(self):
        stages = []
        self.responses = [response(route(intent='CAUSE_ANALYSIS', date=None)),
                          response(output=[tool('search_local_events', {'station': '동부역', 'date': None})]),
                          response(output=[tool()]), response(answer(source_ids=['s1配'])), response(answer())]
        chat_agent._run(self.client, '동부역 사용량이 왜 늘었어?', self.conversation, time.monotonic() + 55, stages.append)
        self.assertEqual(stages, ['classify', 'plan', 'web', 'compare', 'data', 'compare', 'repair', 'compare'])
        self.assertEqual(set(chat_agent.TOOL_STAGES), {schema['name'] for schema in TOOL_SCHEMAS})   # 새 도구는 단계도 정한다

    def test_one_corrected_answer_is_accepted_after_a_rejected_one(self):
        self.responses = [response(route()), response(output=[tool()]), response(answer(source_ids=['s1配'])), response(answer())]
        result = self.run_agent('동부역 현황은?')
        self.assertEqual(result['generated_by'], chat_agent.config.OPENAI_MODEL)
        self.assertIn('source_id', self.sent[-1]['input'][-1]['content'])
        self.assertEqual(len(self.sent), 4)


if __name__ == '__main__':
    unittest.main()
