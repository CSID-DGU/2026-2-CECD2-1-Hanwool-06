"""권한·결측·출처·이력 계산은 모델의 주장과 무관하게 서버에서 결정한다."""
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from back.api import catalog, config, db
from back.rag import storage as storage_module
from back.rag.tools import ChatTools, TOOL_SCHEMAS
from test_chat_storage import make_store


class ChatToolsTest(unittest.TestCase):
    def setUp(self):
        storage_module._CACHE.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(config, 'APP_DB_PATH', Path(self.temp.name) / 'app.db')
        self.patch.start()
        db.init_db()
        with db.connect() as c:
            for office in ('동부', '서부'):
                c.execute('INSERT INTO offices VALUES(?,?)', (office, office))
            for station in ('동부역', '동부신역', '서부역'):
                c.execute('INSERT INTO stations VALUES(?,?,NULL,NULL)', (station, station))
            for mid, station, office, active, daily, deleted in [
                ('000000001', '동부역', '동부', 1, 1, None), ('000000002', '서부역', '서부', 1, 1, None),
                ('000000003', '동부역', '동부', 1, 1, None), ('000000004', '동부역', '동부', 1, 0, None),
                ('000000005', '동부역', '동부', 0, 1, None), ('000000006', '동부역', '동부', 1, 1, '2026-01-01'),
                ('000000007', '동부신역', '동부', 1, 1, None)]:
                c.execute('''INSERT INTO meters(id,provider,customer_number,station_id,office_id,line,display_name,
                           active,daily_enabled,deleted_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                          (mid, 'arisu', mid, station, office, '2', station + ' ' + mid[-1], active, daily, deleted, db.now(), db.now()))
        daily = {'000000001': {'usage': [{'date': '2026-10-18', 'value': 5}, {'date': '2026-10-19', 'value': 5},
                                         {'date': '2026-10-20', 'value': 20}], 'ridership': [{'date': '2026-10-20', 'value': 2000}]},
                 '000000002': {'usage': [{'date': '2026-10-25', 'value': 999}], 'ridership': []},
                 '000000003': {'usage': [{'date': '2026-10-20', 'value': 0}], 'ridership': []},
                 '000000004': {'usage': [{'date': '2026-10-20', 'value': 700}], 'ridership': []}}
        risk = {'000000001': [{'date': '2026-10-19', 'actual': 5, 'pred': 10, 'severity': '경고', 'err': True, 'reason': '검침값 고정'},
                              {'date': '2026-10-20', 'actual': 20, 'pred': 10, 'residual': 10, 'z': 4, 'severity': '경고', 'dir': '증가', 'err': False}],
                '000000002': [{'date': '2026-10-25', 'actual': 999, 'pred': 20, 'severity': '경고', 'err': False}]}
        self.store, self.fake = make_store(daily, risk)
        self.tools = ChatTools(['동부'], store=self.store)

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_remote_facts_differ_from_local_and_are_scoped(self):
        with patch.object(catalog, 'data_sources', return_value=({'000000001': {'usage': [{'date': '2026-10-20', 'value': 9999}]}}, {})) as local:
            out = self.tools.execute('get_station_status', {'line': None, 'station': '동부역', 'meter_ref': None, 'date': None})
            local.assert_not_called()
        self.assertEqual(out['date'], '2026-10-20')
        self.assertEqual(len(out['meters']), 3)
        self.assertEqual([m['actual_ton'] for m in out['meters']], [20, 0, None])
        self.assertEqual([m['severity'] for m in out['meters']], ['경고', '미분석', '일일 수집 대상 아님'])
        serialized = json.dumps(out, ensure_ascii=False)
        self.assertNotIn('000000001', serialized)
        self.assertNotIn('000000002', serialized)
        self.assertNotIn('서부', serialized)
        self.assertTrue(all('/detail?meter=' in s['href'] and 'date=2026-10-20' in s['href']
                            for s in self.tools.sources if s['kind'] == 'snapshot'))
        self.assertEqual(next(s for s in self.tools.sources if s['id'] == out['meters'][0]['source_id'])['meter_id'], '000000001')
        self.assertEqual(self.tools.evidence[out['meters'][0]['source_id']]['actual_ton'], 20)
        denied = self.tools.execute('get_station_status', {'line': None, 'station': '서부역', 'meter_ref': None, 'date': '2026-10-20'})
        self.assertEqual(denied['code'], 'not_found')

    def test_frozen_missing_and_history_statistics_never_zero_fill(self):
        out = self.tools.execute('get_station_history', {'line': None, 'station': '동부역', 'meter_ref': None, 'start_date': '2026-10-17', 'end_date': '2026-10-20'})
        first = out['meters'][0]
        self.assertEqual([r['severity'] for r in first['rows']], ['미분석', '자료 확인', '경고'])
        self.assertEqual([r['actual_ton'] for r in first['rows']], [5, None, 20])
        self.assertEqual(first['statistics']['total_ton'], 25)
        self.assertEqual(first['statistics']['mean_ton'], 12.5)
        self.assertEqual(first['statistics']['missing_or_invalid_days'], 2)
        self.assertIsNone(first['statistics']['comparisons']['previous_day']['change_ton'])
        self.assertEqual(out['meters'][1]['statistics']['total_ton'], 0)
        self.assertIsNone(out['meters'][2]['statistics']['total_ton'])
        self.assertEqual(self.tools.evidence[first['source_id']]['statistics'], first['statistics'])

    def test_validation_and_unknown_tool_do_not_touch_storage(self):
        invalid = [('_data', {}), ('get_station_status', {'line': None, 'station': '동부역', 'meter_ref': None, 'date': '2026-02-30'}),
                   ('get_station_status', {'line': None, 'station': '동부역', 'meter_ref': None, 'date': '2026-10-20', 'office_ids': ['서부']}),
                   ('get_station_history', {'line': None, 'station': '동부역', 'meter_ref': None, 'start_date': '2026-10-21', 'end_date': '2026-10-20'}),
                   ('find_anomalies', {'station': None, 'line': None, 'meter_ref': None, 'start_date': '2026-10-20', 'end_date': '2026-10-01', 'severity': None})]
        for name, args in invalid:
            self.assertEqual(self.tools.execute(name, args)['code'], 'validation_error')
        self.assertEqual(self.fake.calls, [])
        self.assertTrue(all(s['strict'] and s['parameters']['additionalProperties'] is False for s in TOOL_SCHEMAS))

    def test_resolve_ambiguity_aliases_context_and_authorization_change(self):
        ambiguous = self.tools.execute('resolve_station', {'query': '동', 'line': '2'})
        self.assertTrue(ambiguous['ambiguous'])
        result = self.tools.execute('resolve_station', {'query': '동부', 'line': None})
        self.assertFalse(result['ambiguous'])
        alias = result['matches'][0]['meter_ref']
        followup = ChatTools(['동부'], context=self.tools.context, store=self.store)
        self.assertNotIn('000000001', json.dumps(followup.model_context))
        selected = followup.execute('get_station_status', {'line': None, 'station': None, 'meter_ref': alias, 'date': '2026-10-20'})
        self.assertEqual(len(selected['meters']), 1)
        denied = ChatTools(['서부'], context=self.tools.context, store=self.store).execute(
            'get_station_status', {'line': None, 'station': None, 'meter_ref': alias, 'date': '2026-10-20'})
        self.assertEqual(denied['code'], 'not_found')

    def test_anomalies_use_remote_error_status_and_scope(self):
        out = self.tools.execute('find_anomalies', {'station': None, 'line': None, 'meter_ref': None, 'start_date': '2026-10-18', 'end_date': '2026-10-25', 'severity': None})
        self.assertEqual([r['severity'] for r in out['rows']], ['경고', '자료 확인'])
        self.assertFalse(out['truncated'])
        self.assertNotIn('000000002', json.dumps(out))

    def test_zero_anomalies_and_large_search_have_grounded_bounded_summary(self):
        none = self.tools.execute('find_anomalies', {'station': None, 'line': None, 'meter_ref': None, 'start_date': '2026-10-01', 'end_date': '2026-10-02', 'severity': None})
        self.assertEqual(none['match_count'], 0)
        self.assertEqual(self.tools.evidence[none['source_id']]['match_count'], 0)
        data = self.store.snapshot()
        data['risk']['000000001'] = [
            {'date': f'2026-10-{n:02}', 'actual': n, 'pred': 1, 'severity': '경고', 'err': False} for n in range(1, 21)]
        large = self.tools.execute('find_anomalies', {'station': None, 'line': None, 'meter_ref': None, 'start_date': '2026-10-01', 'end_date': '2026-10-20', 'severity': None})
        self.assertEqual((large['match_count'], large['returned_count']), (20, 11))
        self.assertTrue(large['truncated'])
        self.assertEqual(large['rows'][0]['date'], '2026-10-20')
        query_evidence = self.tools.evidence[large['source_id']]
        self.assertEqual(query_evidence['rows'], large['rows'])
        self.assertEqual(len(query_evidence['rows']), 11)
        self.assertNotIn('2026-10-09', json.dumps(query_evidence))
        self.assertLess(len(json.dumps(large, ensure_ascii=False).encode()), 32000)

    def test_anomaly_query_citation_supports_only_returned_detail_dates(self):
        from back.api import chat_agent
        out = self.tools.execute('find_anomalies', {'station': None, 'line': None, 'meter_ref': None,
            'start_date': '2026-10-01', 'end_date': '2026-10-25', 'severity': '경고'})
        route = chat_agent.Route(domain='IN_SCOPE', intent='ANOMALY_SEARCH', stations=[], line=None,
            date=None, start_date='2026-10-01', end_date='2026-10-25', needs_clarification=False)
        answer = {'answer': '동부역 2호선에서 2026-10-20 경고가 확인됐습니다.',
            'source_ids': [out['source_id']], 'is_hypothesis': False}
        chat_agent._validated_answer(json.dumps(answer, ensure_ascii=False), self.tools, route)
        answer['answer'] = '동부역 2호선에서 2026-10-21 경고가 확인됐습니다.'
        with self.assertRaises(ValueError):
            chat_agent._validated_answer(json.dumps(answer, ensure_ascii=False), self.tools, route)

    def test_day_after_the_stored_data_is_not_called_normal_and_names_the_last_day(self):
        from back.api import chat_agent
        out = self.tools.execute('get_station_status', {'line': None, 'station': '동부역', 'meter_ref': None, 'date': '2026-10-21'})
        ids = [out['meters'][0]['source_id'], out['summary']['source_id']]
        route = chat_agent.Route(domain='IN_SCOPE', intent='STATION_STATUS', stations=['동부역'], line=None,
            date='2026-10-21', start_date=None, end_date=None, needs_clarification=False)

        def check(text):
            answer = {'answer': text, 'source_ids': ids, 'is_hypothesis': False}
            return chat_agent._validated_answer(json.dumps(answer, ensure_ascii=False), self.tools, route)[0]

        for claim in ('2026-10-21 동부역은 이상이 없습니다.', '동부역은 정상입니다.', '특이사항 없이 문제 없습니다.',
                      '이상 징후는 보이지 않습니다.', '경고는 0건입니다.', '10월 21일에는 이상이 확인되지 않았습니다.',
                      '이상이 없고 원인은 판단할 수 없습니다.'):
            with self.subTest(claim=claim), self.assertRaisesRegex(ValueError, 'without analysis'):
                check(claim)
        for honest in ('2026-10-21 자료는 아직 없습니다. 자료는 2026-10-20까지 있습니다.', '자료가 없어 정상 여부를 확인할 수 없습니다.',
                       # 실제 모델이 쓴 문장: 판정을 부정하는 표현은 주장이 아니다.
                       '2026-10-21 자료는 아직 들어오지 않아 이상 여부를 확인할 수 없습니다. 2026-10-21을 정상이나 경고 0건으로 판단할 수는 없습니다.',
                       '10월 21일을 정상이나 경고 없음으로 판단할 수는 없습니다.'):
            with self.subTest(honest=honest):
                self.assertEqual(check(honest), honest)
        sources = [source for source in self.tools.sources if source['id'] in ids]
        self.assertIn('2026-10-20', chat_agent._coverage_notice(self.tools, sources, route))

    def test_weekly_comparison_is_computed_and_incomplete_weeks_are_not_compared(self):
        data = self.store.snapshot()
        data['risk']['000000001'] = []
        data['daily']['000000001']['usage'] = [{'date': f'2026-10-{n:02}', 'value': 10 if n <= 13 else 20} for n in range(7, 21)]
        ref = self.tools.execute('resolve_station', {'query': '동부역', 'line': None})['matches'][0]['meter_ref']
        result = self.tools.execute('get_station_history', {'line': None, 'station': None, 'meter_ref': ref,
                   'start_date': '2026-10-14', 'end_date': '2026-10-20'})
        weeks = result['meters'][0]['statistics']['weeks']
        self.assertEqual(weeks['recent_week']['total_ton'], 140)
        self.assertEqual(weeks['previous_week']['total_ton'], 70)
        self.assertEqual(weeks['comparison'], {'complete': True, 'change_ton': 70, 'change_percent': 100,
                                             'change_ton_magnitude': 70, 'change_percent_magnitude': 100, 'change_direction': '증가'})
        data['daily']['000000001']['usage'].pop(0)
        incomplete = self.tools.execute('get_station_history', {'line': None, 'station': None, 'meter_ref': ref,
                   'start_date': '2026-10-14', 'end_date': '2026-10-20'})['meters'][0]['statistics']['weeks']
        self.assertFalse(incomplete['comparison']['complete'])
        self.assertIsNone(incomplete['comparison']['change_ton'])
        self.assertIsNone(incomplete['comparison']['change_ton_magnitude'])
        self.assertIsNone(incomplete['comparison']['change_percent_magnitude'])
        self.assertIsNone(incomplete['comparison']['change_direction'])

    def test_each_call_rechecks_active_catalog(self):
        self.tools.execute('resolve_station', {'query': '동부역', 'line': None})
        with db.connect() as c:
            c.execute("UPDATE meters SET active=0 WHERE station_id='동부역'")
        output = self.tools.execute('get_station_status', {'line': None, 'station': '동부역', 'meter_ref': None, 'date': '2026-10-20'})
        self.assertEqual(output['code'], 'not_found')
        self.assertEqual(self.fake.calls, [])

    def test_anomaly_null_dates_use_scoped_remote_latest_week(self):
        with patch.object(catalog, 'data_sources', side_effect=AssertionError('local fallback')):
            recent = self.tools.execute('find_anomalies', {'station': None, 'line': None, 'meter_ref': None, 'start_date': None, 'end_date': None, 'severity': None})
        self.assertEqual((recent['start_date'], recent['end_date']), ('2026-10-14', '2026-10-20'))
        self.assertEqual(recent['match_count'], 2)
        self.assertEqual(self.tools.evidence[recent['source_id']]['end_date'], '2026-10-20')
        self.assertEqual(self.tools.evidence[recent['source_id']]['period_days'], 7)
        explicit_end = self.tools.execute('find_anomalies', {'station': None, 'line': None, 'meter_ref': None, 'start_date': None, 'end_date': '2026-10-19', 'severity': None})
        self.assertEqual((explicit_end['start_date'], explicit_end['end_date']), ('2026-10-13', '2026-10-19'))
        explicit_start = self.tools.execute('find_anomalies', {'station': None, 'line': None, 'meter_ref': None, 'start_date': '2026-10-20', 'end_date': None, 'severity': None})
        self.assertEqual((explicit_start['start_date'], explicit_start['end_date']), ('2026-10-20', '2026-10-20'))

    def test_anomaly_missing_remote_reference_does_not_use_wall_clock(self):
        self.store.snapshot()['daily'].clear()
        self.store.snapshot()['risk'].clear()
        args = {'station': None, 'line': None, 'meter_ref': None, 'start_date': None, 'end_date': None}
        for name in ('find_anomalies', 'get_station_history'):
            with self.subTest(tool=name):
                result = self.tools.execute(name, args | ({'severity': None} if name == 'find_anomalies' else {}))
                self.assertEqual(result['code'], 'no_reference_date')

    def test_history_null_dates_use_latest_day_in_the_selected_scope(self):
        args = {'station': '동부역', 'line': None, 'meter_ref': None}
        for start, end, expected in [
            (None, None, ('2026-10-14', '2026-10-20')),
            (None, '2026-10-19', ('2026-10-13', '2026-10-19')),
            ('2026-10-18', None, ('2026-10-18', '2026-10-20')),
        ]:
            with self.subTest(start=start, end=end):
                out = self.tools.execute('get_station_history', args | {'start_date': start, 'end_date': end})
                self.assertEqual((out['start_date'], out['end_date']), expected)
                self.assertEqual(out['summary']['coverage']['requested_end_date'], expected[1])

    def test_long_ranges_keep_exact_totals_without_expanding_missing_calendar_days(self):
        args = {'station': '동부역', 'line': None, 'meter_ref': None}
        for start, end in [('2025-01-01', '2026-10-21'), ('0001-01-01', '9999-12-31')]:
            with self.subTest(start=start, end=end):
                out = self.tools.execute('get_station_history', args | {'start_date': start, 'end_date': end})
                days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
                summary = out['summary']
                self.assertEqual((summary['period_days'], summary['total_ton']), (days, 25))
                self.assertEqual(summary['total_row_count'], 4)
                self.assertEqual(sum(len(m['rows']) for m in out['meters']), 4)
                self.assertEqual(summary['coverage']['expected_meter_days'], days * 2)
                self.assertEqual(summary['coverage']['missing_usage_meter_days'], days * 2 - 3)
                self.assertEqual(self.tools.evidence[summary['source_id']]['coverage'], summary['coverage'])
                self.assertLess(len(json.dumps(out, ensure_ascii=False).encode()), 32000)
                anomalies = self.tools.execute('find_anomalies', args | {
                    'start_date': start, 'end_date': end, 'severity': '경고'})
                self.assertEqual((anomalies['match_count'], anomalies['matched_day_count']), (1, 1))

    def test_coverage_separates_unanalyzed_data_from_empty_periods(self):
        args = {'station': '동부역', 'line': None, 'meter_ref': None,
                'start_date': '2026-10-17', 'end_date': '2026-10-21'}
        out = self.tools.execute('find_anomalies', args | {'severity': '주의'})
        self.assertEqual(out['match_count'], 0)
        coverage = out['coverage']
        self.assertEqual((coverage['selected_meter_count'], coverage['eligible_meter_count'],
                          coverage['billing_only_meter_count']), (3, 2, 1))
        self.assertEqual((coverage['expected_meter_days'], coverage['usage_meter_days'],
                          coverage['analyzed_meter_days'], coverage['data_error_meter_days']), (10, 3, 1, 1))
        self.assertEqual((coverage['missing_usage_meter_days'], coverage['missing_analysis_meter_days']), (7, 9))
        self.assertEqual((coverage['usage_first_date'], coverage['usage_last_date']), ('2026-10-18', '2026-10-20'))
        self.assertEqual((coverage['analysis_first_date'], coverage['analysis_last_date']), ('2026-10-20', '2026-10-20'))
        self.assertEqual((coverage['usage_day_count'], coverage['analysis_day_count'],
                          coverage['complete_usage_day_count'], coverage['complete_analysis_day_count']), (2, 1, 1, 0))
        self.assertEqual(self.tools.evidence[out['source_id']]['coverage'], coverage)
        self.store.snapshot()['risk'].clear()
        unanalyzed = self.tools.execute('get_station_history', args)['summary']['coverage']
        self.assertEqual((unanalyzed['usage_meter_days'], unanalyzed['analyzed_meter_days']), (4, 0))
        self.assertIsNone(unanalyzed['analysis_first_date'])
        self.assertIsNone(unanalyzed['analysis_last_date'])
        no_analysis = ChatTools(['동부'], store=self.store)
        unknown_count = no_analysis.execute('find_anomalies', args | {'severity': '주의'})
        source = next(s for s in no_analysis.sources if s['id'] == unknown_count['source_id'])
        self.assertIn('확인할 수 없음', source['summary'])
        self.assertNotIn('0건', source['summary'])
        for start, end in [('1900-01-01', '2000-12-31'), ('0001-01-01', '0001-01-01')]:
            with self.subTest(empty_start=start):
                empty = self.tools.execute('get_station_history', args | {'start_date': start, 'end_date': end})
                summary = empty['summary']
                self.assertIsNone(summary['total_ton'])
                self.assertEqual(summary['total_row_count'], 0)
                self.assertTrue(all(not m['rows'] and m['statistics']['total_ton'] is None for m in empty['meters']))
                self.assertEqual(summary['coverage']['usage_meter_days'], 0)
                self.assertEqual(summary['coverage']['missing_analysis_meter_days'], summary['coverage']['expected_meter_days'])
                self.assertIsNone(summary['coverage']['usage_first_date'])
                self.assertIsNone(summary['coverage']['usage_last_date'])
                self.assertLess(len(json.dumps(empty, ensure_ascii=False).encode()), 32000)

    def test_anomaly_target_filters_and_meter_day_counts_do_not_inherit_a_narrow_scope(self):
        with db.connect() as c:
            c.execute("UPDATE meters SET line='3' WHERE id='000000003'")
        data = self.store.snapshot()
        data['risk']['000000001'][-1]['severity'] = '주의'
        data['risk']['000000003'] = [{'date': '2026-10-20', 'actual': 0, 'pred': 10, 'severity': '주의'}]
        data['risk']['000000007'] = [{'date': '2026-10-20', 'actual': 8, 'pred': 1, 'severity': '경고'}]
        args = {'station': '동부역', 'line': None, 'meter_ref': None,
                'start_date': '2026-10-20', 'end_date': '2026-10-20', 'severity': '주의'}
        station = self.tools.execute('find_anomalies', args)
        self.assertEqual((station['match_count'], station['matched_day_count'], station['station_count']), (2, 1, 1))
        self.assertEqual((station['severity_counts']['주의'], station['severity_day_counts']['주의']), (2, 1))
        line = self.tools.execute('find_anomalies', args | {'station': None, 'line': '3'})
        self.assertEqual(line['match_count'], 1)
        ref = line['rows'][0]['meter_ref']
        meter = self.tools.execute('find_anomalies', args | {'meter_ref': ref, 'line': '3'})
        self.assertEqual((meter['match_count'], meter['coverage']['eligible_meter_count']), (1, 1))
        source = next(s for s in self.tools.sources if s['id'] == meter['rows'][0]['source_id'])
        self.assertEqual((source['meter_id'], source['date']), ('000000003', '2026-10-20'))
        denied = self.tools.execute('find_anomalies', args | {'meter_ref': ref, 'line': '2'})
        self.assertEqual(denied['code'], 'not_found')
        all_meters = self.tools.execute('find_anomalies', args | {'station': None, 'severity': None})
        self.assertEqual((all_meters['match_count'], all_meters['station_count']), (3, 2))
        self.assertEqual(all_meters['coverage']['selected_meter_count'], 4)
        self.assertNotIn('서부', json.dumps(all_meters, ensure_ascii=False))
        for tool, dates in [('get_station_status', {'date': '2026-10-20'}),
                            ('get_station_history', {'start_date': '2026-10-20', 'end_date': '2026-10-20'})]:
            with self.subTest(tool=tool):
                filtered = self.tools.execute(tool, {'station': '동부역', 'line': '3', 'meter_ref': ref} | dates)
                self.assertEqual((filtered['summary']['meter_count'], filtered['summary']['total_ton']), (1, 0))
                broad = self.tools.execute(tool, {'station': None, 'line': None, 'meter_ref': None} | dates)
                self.assertEqual((broad['summary']['meter_count'], broad['summary']['total_ton']), (4, 28))

    def test_duplicate_dates_use_the_last_published_fact_once_for_counts_totals_and_sources(self):
        data = self.store.snapshot()
        data['daily']['000000001']['usage'].append({'date': '2026-10-18', 'value': 6})
        data['risk']['000000001'].append({'date': '2026-10-20', 'actual': 7, 'pred': 10, 'severity': '주의'})
        args = {'station': '동부역', 'line': None, 'meter_ref': None}
        history = self.tools.execute('get_station_history', args | {'start_date': '2026-10-18', 'end_date': '2026-10-20'})
        self.assertEqual(history['summary']['total_ton'], 13)
        self.assertEqual(history['summary']['total_row_count'], 4)
        self.assertEqual([row['actual_ton'] for row in history['meters'][0]['rows']], [6, None, 7])
        anomalies = self.tools.execute('find_anomalies', args | {
            'start_date': '2026-10-20', 'end_date': '2026-10-20', 'severity': None})
        self.assertEqual(anomalies['match_count'], 1)
        self.assertEqual(anomalies['counts'], {'주의': 1, '경고': 0, '자료 확인': 0})
        row = anomalies['rows'][0]
        self.assertEqual((row['actual_ton'], row['severity']), (7, '주의'))
        self.assertEqual(self.tools.evidence[row['source_id']]['actual_ton'], 7)

    def test_all_meter_aggregates_and_rankings_survive_detail_and_payload_limits(self):
        data = self.store.snapshot()
        days = [(date(2026, 10, 20) - timedelta(days=n)).isoformat() for n in range(64, -1, -1)]
        with db.connect() as c:
            for index in range(13):
                mid, station = f'{100 + index:09}', f'추가{index:02}역'
                c.execute('INSERT INTO stations VALUES(?,?,NULL,NULL)', (station, station))
                c.execute('''INSERT INTO meters(id,provider,customer_number,station_id,office_id,line,display_name,
                           active,daily_enabled,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,1,?,?)''',
                          (mid, 'arisu', mid, station, '동부', '2', station, db.now(), db.now()))
                amount = 1000 if index == 12 else index + 1
                data['daily'][mid] = {'usage': [{'date': day, 'value': amount} for day in days]}
                data['risk'][mid] = [{'date': day, 'actual': amount, 'pred': amount, 'severity': '정상'} for day in days]
        target = {'station': None, 'line': None, 'meter_ref': None}
        history = self.tools.execute('get_station_history', target | {'start_date': '2025-01-01', 'end_date': '2026-10-20'})
        summary = history['summary']
        self.assertEqual((summary['meter_count'], summary['total_ton']), (17, 70095))
        top = summary['rankings']['top_stations_by_total_ton'][0]
        self.assertEqual((top['station'], top['total_ton']), ('추가12역', 65000))
        self.assertEqual(summary['rankings']['ranked_station_count'], 14)
        self.assertEqual(len(history['meters']), 4)
        self.assertLessEqual(sum(len(m['rows']) for m in history['meters']), 120)
        self.assertTrue(summary['meters_truncated'] and summary['rows_truncated'])
        self.assertTrue(all(m['truncated'] and m['rows'][-1]['date'] == '2026-10-20' for m in history['meters']))
        self.assertLess(len(json.dumps(history, ensure_ascii=False).encode()), 32000)
        self.assertEqual(self.tools.evidence[summary['source_id']]['total_ton'], 70095)
        for meter in history['meters']:
            source = next(s for s in self.tools.sources if s['id'] == meter['source_id'])
            self.assertEqual(source['meter_id'], self.tools.context['meters'][meter['meter_ref']])
            self.assertIn('date=2026-10-20', source['href'])
            self.assertEqual(self.tools.evidence[meter['source_id']]['statistics'], meter['statistics'])
        status = self.tools.execute('get_station_status', target | {'date': '2026-10-20'})
        self.assertEqual((status['summary']['meter_count'], status['summary']['total_ton']), (17, 1098))
        self.assertEqual(status['summary']['severity_counts']['정상'], 13)
        self.assertEqual(len(status['meters']), 11)
        self.assertTrue(status['summary']['truncated'])
        self.assertLess(len(json.dumps(status, ensure_ascii=False).encode()), 32000)
        self.assertEqual(self.tools.evidence[status['summary']['source_id']]['total_ton'], 1098)

    def test_decrease_facts_include_signed_values_magnitudes_and_direction(self):
        data = self.store.snapshot()
        data['risk']['000000001'] = [{'date': '2026-10-20', 'actual': 7, 'pred': 24.504,
            'residual': -17.504, 'severity': '주의', 'err': False}]
        status = self.tools.execute('get_station_status', {'line': None, 'station': '동부역', 'meter_ref': None, 'date': '2026-10-20'})
        row = status['meters'][0]
        self.assertEqual((row['deviation_percent'], row['deviation_percent_magnitude'], row['deviation_direction']),
                         (-71.4, 71.4, '감소'))
        self.assertEqual(row['residual_ton_magnitude'], 17.504)
        evidence = self.tools.evidence[row['source_id']]
        self.assertEqual(evidence['deviation_percent_magnitude'], 71.4)
        # 분석되지 않은 관측과 청구 전용에는 변화량을 만들어 넣지 않는다.
        for unknown in status['meters'][1:]:
            self.assertIsNone(unknown['deviation_percent_magnitude'])
            self.assertIsNone(unknown['residual_ton_magnitude'])
            self.assertIsNone(unknown['deviation_direction'])
        data['risk']['000000001'] = []
        data['daily']['000000001']['usage'] = [{'date': f'2026-10-{n:02}', 'value': 20 if n <= 13 else 7} for n in range(7, 21)]
        history = self.tools.execute('get_station_history', {'line': None, 'station': '동부역', 'meter_ref': row['meter_ref'],
            'start_date': '2026-10-14', 'end_date': '2026-10-20'})
        stats = history['meters'][0]['statistics']
        weekly = stats['weeks']['comparison']
        self.assertEqual((weekly['change_ton'], weekly['change_ton_magnitude'], weekly['change_percent_magnitude'], weekly['change_direction']),
                         (-91, 91, 65, '감소'))
        same_day = stats['comparisons']['previous_day']
        self.assertEqual((same_day['change_ton_magnitude'], same_day['change_percent_magnitude'], same_day['change_direction']),
                         (0, 0, '동일'))
        self.assertEqual(stats['period_days'], 7)
