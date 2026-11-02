"""구조화 의도 분류, 제한된 도구 루프, 출처 검증의 모델 모의 응답 테스트."""
import copy
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException
from pydantic import ValidationError
from back.api import chat_agent
from back.rag.tools import TOOL_SCHEMAS


def response(text='', output=None, status='completed'):
    return SimpleNamespace(output_text=json.dumps(text, ensure_ascii=False) if not isinstance(text, str) else text,
                           output=output or [], status=status)


def route(**changes):
    station = changes.pop('station', '동부역')
    return {'domain': 'IN_SCOPE', 'intent': 'STATION_STATUS', 'stations': [station] if station else [], 'line': None, 'date': '2026-08-31',
            'start_date': None, 'end_date': None, 'needs_clarification': False, **changes}


def tool(name='get_station_status', arguments=None, call_id='call_1'):
    if arguments is not None and name in chat_agent.DATA_TOOLS:
        arguments = {'meter_ref': None, 'station': None, 'line': None, **arguments}
    return {'type': 'function_call', 'name': name, 'call_id': call_id,
            'arguments': json.dumps(arguments or {'meter_ref': None, 'line': None, 'station': '동부역', 'date': '2026-08-31'})}


def answer(**changes):
    return {'answer': '동부역의 사용량은 20톤, 예측은 10톤이며 판정은 경고입니다.',
            'source_ids': ['s1'], 'is_hypothesis': False, **changes}


class ChatAgentTest(unittest.TestCase):
    def setUp(self):
        self.conversation = {'id': 'a' * 32, 'user_id': 1, 'offices': ['east'], 'fingerprint': 'f',
                             'context': {'station': '동부역', 'date': '2026-08-31', 'meters': {'m1': 'secret-meter'}},
                             'messages': [{'role': 'user', 'content': '동부역 현황'}]}
        self.client = Mock()
        self.sent = []
        self.responses = []
        def create(**kwargs):
            self.sent.append(copy.deepcopy(kwargs))
            return self.responses.pop(0)
        self.client.responses.create.side_effect = create
        self.kit = SimpleNamespace(context=self.conversation['context'], sources=[
            {'id': 's1', 'label': '동부역 운영 자료', 'kind': 'snapshot', 'meter_id': 'secret-meter',
             'date': '2026-08-31', 'href': '#/detail?meter=secret-meter', 'summary': '경고'}],
            evidence={'s1': {'station': '동부역', 'date': '2026-08-31', 'actual_ton': 20.0,
                             'predicted_ton': 10.0, 'severity': '경고', 'coverage': {}}})
        self.kit.execute = Mock(return_value={'meters': [self.kit.evidence['s1'] | {'source_id': 's1'}]})
        self.scope_patch = patch.object(chat_agent, '_check_scope')
        self.scope_check = self.scope_patch.start()
        self.tool_patch = patch('back.rag.tools.ChatTools', return_value=self.kit)
        self.tool_class = self.tool_patch.start()

    def tearDown(self):
        self.tool_patch.stop()
        self.scope_patch.stop()

    def run_agent(self, text='동부역 현황은?'):
        return chat_agent._run(self.client, text, self.conversation, time.monotonic() + 55)[0]

    def test_outside_and_unclear_have_no_data_load_or_model_answer_call(self):
        for domain, text in [('OUT_OF_SCOPE', '맛집 추천'), ('UNCLEAR', '이것 좀')]:
            with self.subTest(domain=domain):
                self.responses = [response(route(domain=domain, intent='UNKNOWN'))]
                result = self.run_agent(text)
                self.assertEqual(result['domain'], domain)
                self.assertEqual(result['sources'], [])
                self.tool_class.assert_not_called()
                self.kit.execute.assert_not_called()
        self.assertEqual(len(self.sent), 2)
        self.assertNotIn('secret-meter', self.sent[0]['input'])
        self.assertTrue(self.sent[0]['text']['format']['strict'])

    def test_conflicting_conditions_ask_for_clarification_without_loading_data(self):
        self.conversation['context'] = {}
        self.responses = [response(route(station=None, needs_clarification=True))]
        result = self.run_agent('어제 사용량')
        self.assertIn('역 이름', result['answer'])
        self.tool_class.assert_not_called()

    def test_actual_loop_replays_reasoning_and_exact_call_id_then_cites_trusted_source(self):
        reasoning = {'type': 'reasoning', 'id': 'reasoning_1', 'summary': [], 'encrypted_content': 'opaque'}
        call = tool()
        self.responses = [response(route()), response(output=[reasoning, call]), response(answer())]
        result = self.run_agent()
        self.assertEqual(result['sources'], self.kit.sources)
        self.assertNotIn('근거:', result['answer'])  # 화면이 서버의 sources로 한 번만 근거를 표시한다.
        self.assertEqual(self.kit.execute.call_args.args, ('get_station_status',
                         {'meter_ref': None, 'line': None, 'station': '동부역', 'date': '2026-08-31'}))
        sent = self.sent[2]['input']
        self.assertIn(reasoning, sent)
        self.assertIn(call, sent)
        self.assertEqual(sent[-1]['type'], 'function_call_output')
        self.assertEqual(sent[-1]['call_id'], 'call_1')
        for call in self.sent:
            self.assertFalse(call['store'])
            self.assertEqual(call['include'], ['reasoning.encrypted_content'])
            self.assertLessEqual(call['timeout'], 55)
        self.assertNotIn('secret-meter', json.dumps(self.sent, ensure_ascii=False))
        names = {schema['name'] for schema in self.sent[1]['tools']}
        self.assertEqual(names, {'resolve_station'} | chat_agent.DATA_TOOLS | chat_agent.ARISU_TOOLS)
        self.assertGreaterEqual(self.scope_check.call_count, 4)

    def test_invalid_or_cross_intent_tools_never_execute(self):
        for call in (tool('send_mail'), tool('search_manuals', {'query': '밸브'}),
                     tool(arguments={'meter_ref': None, 'line': None, 'station': '동부역', 'date': 123}),
                     tool(arguments={'meter_ref': None, 'line': None, 'station': '동부역', 'date': None, 'sql': 'DROP TABLE users'})):
            with self.subTest(call=call):
                self.responses = [response(route()), response(output=[call]), response(answer())]
                result = self.run_agent()
                self.assertEqual(result['sources'], [])
                self.assertEqual(result['generated_by'], 'none')
                self.kit.execute.assert_not_called()
                self.assertIn('invalid_tool_call', self.sent[-1]['input'][-1]['output'])

    def test_no_tool_evidence_or_forged_citation_never_becomes_factual_answer(self):
        self.responses = [response(route()), response(answer())]
        result = self.run_agent()
        self.assertEqual(result['answer'], chat_agent.NO_EVIDENCE)
        for invalid in (answer(source_ids=['made-up']), answer(answer='실측 사용량은 999톤입니다.'),
                        answer(answer='정상입니다.'), answer(answer='https://invented.example.com 자료입니다.'),
                        answer(answer='[자료](javascript:alert) 링크입니다.'), answer(answer='2026-08-08 자료입니다.')):
            with self.subTest(invalid=invalid):
                # 한 번 고쳐 쓸 기회를 줘도 같은 거짓 답은 끝까지 통과하지 못한다.
                self.responses = [response(route()), response(output=[tool()]), response(invalid), response(invalid)]
                result = self.run_agent()
                self.assertIn('서버 검증을 통과하지 못했다', self.sent[-1]['input'][-1]['content'])
                self.assertEqual(result['sources'], self.kit.sources)
                self.assertEqual(result['generated_by'], 'none')
                self.assertIn('조회된 근거를 직접 표시', result['answer'])
                self.assertNotIn(invalid['answer'], result['answer'])

    def test_malformed_final_keeps_actual_evidence_but_undelivered_output_does_not(self):
        self.responses = [response(route()), response(output=[tool()]), response('not JSON'), response('not JSON')]
        result = self.run_agent()
        self.assertEqual(result['sources'], self.kit.sources)
        self.assertIn('경고', result['answer'])
        self.kit.execute.return_value = {'source_id': 's1', 'oversized': 'x' * 40_000}
        self.responses = [response(route()), response(output=[tool()]), response('not JSON')]
        result = self.run_agent()
        self.assertEqual(result['sources'], [])
        self.assertIn('조회 범위가 너무 넓어', result['answer'])

    def test_decimal_display_variants_are_supported_but_new_calculations_are_not(self):
        valid = answer(answer='사용량은 20.0톤이고 예측은 10톤입니다.')
        parsed, _ = chat_agent._validated_answer(json.dumps(valid), self.kit, chat_agent.Route(**route()))
        self.assertIn('20.0톤', parsed)
        self.kit.evidence['s1']['change_percent'] = 100.0
        valid = answer(answer='사용량은 100% 증가했습니다.')
        self.assertIn('100%', chat_agent._validated_answer(json.dumps(valid), self.kit, chat_agent.Route(**route()))[0])

    def test_abbreviated_date_ranges_keep_real_negative_numbers_and_require_exact_dates(self):
        self.kit.evidence['s1'] = {'current': ['2026-10-14', '2026-10-20'],
                                  'previous': ['2026-10-07', '2026-10-13'], 'change_ton': -26, 'days': 19}
        classified = chat_agent.Route(**route(intent='TREND_ANALYSIS'))
        text = '2026-10-14~10-20과 2026-10-07~10-13의 사용량 차이는 -26톤입니다.'
        self.assertEqual(chat_agent._validated_answer(json.dumps(answer(answer=text)), self.kit, classified)[0], text)
        self.assertIn(chat_agent.Decimal('-26'), chat_agent._numbers(text))
        self.assertNotIn(chat_agent.Decimal('-20'), chat_agent._numbers(text))
        with self.assertRaisesRegex(ValueError, 'Unsupported dates'):
            chat_agent._validated_answer(json.dumps(answer(answer='2026-10-14~10-19 사용량입니다.')), self.kit, classified)
        with self.assertRaisesRegex(ValueError, 'Unsupported numbers'):
            chat_agent._validated_answer(json.dumps(answer(answer='사용량 차이는 -27톤입니다.')), self.kit, classified)

    def test_anomaly_list_requires_sources_for_its_detail_dates_and_lines(self):
        self.kit.sources[0]['kind'] = 'query'
        self.kit.sources.append(dict(self.kit.sources[0], id='s2', kind='snapshot'))
        self.kit.evidence['s1'] = {'start_date': '2026-10-14', 'end_date': '2026-10-20', 'counts': {'경고': 1}}
        self.kit.evidence['s2'] = {'station': '석촌역', 'line': '8', 'date': '2026-10-19', 'severity': '경고'}
        classified = chat_agent.Route(**route(intent='ANOMALY_SEARCH', station=None))
        text = '2026-10-14~10-20에 석촌역 8호선에서 10월 19일 경고가 1건 발생했습니다.'
        with self.assertRaisesRegex(ValueError, 'Unsupported (numbers|dates)'):
            chat_agent._validated_answer(json.dumps(answer(answer=text)), self.kit, classified)
        valid = answer(answer=text, source_ids=['s1', 's2'])
        self.assertEqual(chat_agent._validated_answer(json.dumps(valid), self.kit, classified)[0], text)

    def test_unsigned_decrease_needs_explicit_server_magnitude_evidence(self):
        self.kit.evidence['s1'] = {'actual_ton': 7.0, 'predicted_ton': 24.504, 'severity': '경고',
                                  'deviation_percent': -71.4, 'deviation_direction': '감소'}
        text = '사용량은 7.0톤으로 예측 사용량 24.504톤보다 71.4% 낮습니다.'
        classified = chat_agent.Route(**route())
        with self.assertRaisesRegex(ValueError, 'Unsupported numbers'):
            chat_agent._validated_answer(json.dumps(answer(answer=text)), self.kit, classified)
        self.kit.evidence['s1']['deviation_percent_magnitude'] = 71.4
        self.assertEqual(chat_agent._validated_answer(json.dumps(answer(answer=text)), self.kit, classified)[0], text)
        for invented in ('71.5% 낮습니다.', '사용량은 -7.0톤입니다.'):
            with self.subTest(invented=invented), self.assertRaisesRegex(ValueError, 'Unsupported numbers'):
                chat_agent._validated_answer(json.dumps(answer(answer=invented)), self.kit, classified)

    def test_followup_classifier_gets_last_station_date_but_no_private_alias_map(self):
        self.responses = [response(route(date='2026-08-30')), response(output=[tool(arguments={
            'meter_ref': None, 'station': '동부역', 'date': '2026-08-30'})]), response(answer())]
        self.run_agent('전날은?')
        classifier_input = json.loads(self.sent[0]['input'])
        self.assertEqual(classifier_input['context'], {'station': '동부역', 'date': '2026-08-31'})
        self.assertEqual(self.tool_class.call_args.args[1]['date'], '2026-08-30')

    def test_status_tool_cannot_replace_explicit_date_with_latest_or_another_day(self):
        for requested in (None, '2026-08-30'):
            with self.subTest(requested=requested):
                call = tool(arguments={'meter_ref': None, 'line': None, 'station': '동부역', 'date': requested})
                self.responses = [response(route(date='2026-08-31')), response(output=[call]), response(answer())]
                result = self.run_agent()
                self.kit.execute.assert_not_called()
                self.assertEqual(result['sources'], [])
                self.assertIn('invalid_tool_call', self.sent[-1]['input'][-1]['output'])
        self.responses = [response(route(date=None)), response(output=[tool(arguments={
            'meter_ref': None, 'station': '동부역', 'date': None})]), response(answer())]
        result = self.run_agent('동부역 최신 현황')
        self.assertIsNone(self.kit.execute.call_args.args[1]['date'])
        self.assertEqual(result['sources'], self.kit.sources)

    def test_history_tool_must_preserve_each_explicit_range_boundary(self):
        classified = route(intent='TREND_ANALYSIS', start_date='2026-08-18', end_date='2026-08-31')
        correct = {'meter_ref': None, 'line': None, 'station': '동부역', 'start_date': '2026-08-18', 'end_date': '2026-08-31'}
        for args in (correct | {'start_date': '2026-08-19'}, correct | {'end_date': '2026-08-30'}):
            with self.subTest(args=args):
                self.responses = [response(classified), response(output=[tool('get_station_history', args)]), response(answer())]
                result = self.run_agent('동부역 기간별 사용량 비교')
                self.kit.execute.assert_not_called()
                self.assertEqual(result['sources'], [])
                self.assertIn('invalid_tool_call', self.sent[-1]['input'][-1]['output'])
        self.responses = [response(classified), response(output=[tool('get_station_history', correct)]), response(answer())]
        result = self.run_agent('동부역 기간별 사용량 비교')
        self.assertEqual(self.kit.execute.call_args.args[1], correct)
        self.assertEqual(result['sources'], self.kit.sources)

    def test_anomaly_search_without_station_or_date_requests_remote_recent_range(self):
        self.conversation['context'] = {}
        self.kit.sources[0]['kind'] = 'query'
        args = {'meter_ref': None, 'station': None, 'line': None, 'start_date': None, 'end_date': None, 'severity': '경고'}
        self.responses = [response(route(intent='ANOMALY_SEARCH', station=None, date=None)),
                          response(output=[tool('find_anomalies', args)]), response(answer())]
        result = self.run_agent('최근 경고가 나온 역을 알려 주세요')
        self.assertEqual(self.kit.execute.call_args.args, ('find_anomalies', args))
        self.assertEqual(self.sent[1]['tool_choice'], 'required')
        self.assertEqual(result['sources'], self.kit.sources)

    def test_anomaly_search_preserves_explicit_day_or_period(self):
        cases = [
            (route(intent='ANOMALY_SEARCH', station=None, date='2026-08-31'),
             {'start_date': None, 'end_date': None, 'severity': None}),
            (route(intent='ANOMALY_SEARCH', station=None, date='2026-08-31'),
             {'start_date': '2026-08-30', 'end_date': '2026-08-30', 'severity': None}),
            (route(intent='ANOMALY_SEARCH', station=None, date=None, start_date='2026-08-18', end_date='2026-08-31'),
             {'start_date': '2026-08-25', 'end_date': '2026-08-31', 'severity': None}),
        ]
        for classified, args in cases:
            with self.subTest(args=args):
                self.responses = [response(classified), response(output=[tool('find_anomalies', args)]), response(answer())]
                result = self.run_agent('지정 날짜의 이상 역')
                self.kit.execute.assert_not_called()
                self.assertEqual(result['sources'], [])
                self.assertIn('invalid_tool_call', self.sent[-1]['input'][-1]['output'])

    def test_whole_scope_does_not_reuse_the_previous_station(self):
        self.kit.sources[0]['kind'] = 'query'
        self.responses = [response(route(station=None, date=None)),
                          response(output=[tool(arguments={'station': None, 'date': None})]), response(answer())]
        result = self.run_agent('전체 역 최신 현황')
        self.assertIsNone(self.kit.execute.call_args.args[1]['station'])
        context = self.tool_class.call_args.args[1]
        self.assertEqual(context['stations'], [])
        self.assertNotIn('station', context)
        self.assertEqual(result['generated_by'], chat_agent.config.OPENAI_MODEL)

    def test_station_anomaly_range_is_not_broadened_and_accepts_a_long_period(self):
        classified = route(intent='ANOMALY_SEARCH', station='경복궁역', date=None,
                           start_date='2025-01-01', end_date='2026-10-21', line='3')
        args = {'station': None, 'start_date': '2025-01-01', 'end_date': '2026-10-21', 'severity': '주의'}
        self.responses = [response(classified), response(output=[tool('find_anomalies', args)]), response(answer())]
        self.run_agent('경복궁역 2025년부터 오늘까지 주의 횟수')
        self.assertEqual(self.kit.execute.call_args.args[1]['station'], '경복궁역')
        self.assertEqual(self.kit.execute.call_args.args[1]['line'], '3')
        self.assertEqual(self.kit.execute.call_args.args[1]['start_date'], '2025-01-01')
        self.kit.execute.reset_mock()
        for invalid in (args | {'station': '다른역'}, args | {'line': '4'}):
            with self.subTest(args=invalid):
                self.responses = [response(classified), response(output=[tool('find_anomalies', invalid)]), response(answer())]
                self.run_agent()
                self.kit.execute.assert_not_called()

    def test_recent_nondefault_range_can_follow_a_reference_date_lookup(self):
        self.responses = [response(route(intent='ANOMALY_SEARCH', date=None)),
                          response(output=[tool(arguments={'station': '동부역', 'date': None})]),
                          response(output=[tool('find_anomalies', {'station': '동부역', 'start_date': '2026-08-18',
                                                                'end_date': '2026-08-31', 'severity': '경고'})]), response(answer())]
        self.run_agent('동부역 최근 14일 경고 횟수')
        self.assertEqual(self.kit.execute.call_count, 2)
        self.assertEqual(self.kit.execute.call_args.args[1]['start_date'], '2026-08-18')

    def test_multiple_stations_require_each_station_evidence(self):
        self.kit.sources.append(dict(self.kit.sources[0], id='s2', label='서부역 운영 자료'))
        self.kit.evidence['s2'] = dict(self.kit.evidence['s1'], station='서부역')
        calls = [tool(arguments={'station': '동부역', 'date': '2026-08-31'}),
                 tool(arguments={'station': '서부역', 'date': '2026-08-31'}, call_id='call_2')]
        for ids, generated in [(['s1'], 'none'), (['s1', 's2'], chat_agent.config.OPENAI_MODEL)]:
            with self.subTest(ids=ids):
                self.kit.execute.side_effect = [{'source_id': 's1'}, {'source_id': 's2'}]
                self.responses = [response(route(stations=['동부역', '서부역'])),
                                  response(output=[calls[0]]), response(output=[calls[1]]), response(answer(source_ids=ids)),
                                  response(answer(source_ids=ids))]
                result = self.run_agent('동부역과 서부역 사용량 비교')
                self.assertEqual(result['generated_by'], generated)

    def test_whole_scope_cannot_be_answered_with_a_single_station(self):
        self.kit.sources[0]['kind'] = 'query'
        self.responses = [response(route(station=None)), response(output=[tool()]), response(answer()), response(answer())]
        result = self.run_agent('전체 역 현황')
        self.assertEqual(result['generated_by'], 'none')
        self.assertIn('전체 범위의 자료를 확인하지 못해', result['answer'])

    def test_whole_scope_requires_the_aggregate_source_even_if_details_have_coverage(self):
        self.responses = [response(route(station=None)),
                          response(output=[tool(arguments={'station': None, 'date': '2026-08-31'})]), response(answer())]
        result = self.run_agent('전체 역 현황')
        self.assertEqual(result['sources'], [])
        self.assertEqual(result['generated_by'], 'none')

    def test_resolved_canonical_station_is_accepted_for_the_requested_station(self):
        self.kit.execute.side_effect = [
            {'matches': [{'station': '동부(중앙)', 'line': '3', 'meter_ref': 'm123'}], 'ambiguous': False},
            {'source_id': 's1'}]
        self.responses = [response(route()), response(output=[tool('resolve_station', {'query': '동부역', 'line': None})]),
                          response(output=[tool(arguments={'station': '동부(중앙)', 'date': '2026-08-31'})]), response(answer())]
        result = self.run_agent()
        self.assertEqual(self.kit.execute.call_count, 2)
        self.assertEqual(result['generated_by'], chat_agent.config.OPENAI_MODEL)

    def test_range_answer_requires_aggregate_coverage_not_just_a_detail_row(self):
        self.kit.evidence['s1'].pop('coverage')
        self.responses = [response(route(intent='ANOMALY_SEARCH', date=None)),
                          response(output=[tool('find_anomalies', {'start_date': None, 'end_date': None, 'severity': '경고'})]),
                          response(answer())]
        result = self.run_agent('동부역 최근 경고 횟수')
        self.assertEqual(result['sources'], [])

    def test_partial_analysis_and_no_data_have_explicit_server_notices(self):
        coverage = {'requested_start_date': '2025-01-01', 'requested_end_date': '2026-10-21',
                    'missing_analysis_meter_days': 499, 'analysis_first_date': '2026-05-14',
                    'analysis_last_date': '2026-10-20', 'analysis_day_count': 160}
        classified = chat_agent.Route(**route(intent='ANOMALY_SEARCH'))
        self.kit.evidence['s1']['coverage'] = coverage
        notice = chat_agent._coverage_notice(self.kit, self.kit.sources, classified)
        self.assertIn('2025-01-01~2026-10-21', notice)
        self.assertIn('2026-05-14~2026-10-20 중 160일', notice)
        self.assertIn('전체 요청 기간의 집계가 아니며', notice)
        coverage.update(analysis_first_date=None, analysis_last_date=None, analysis_day_count=0)
        notice = chat_agent._coverage_notice(self.kit, self.kit.sources, classified)
        self.assertIn('분석 자료가 없어', notice)
        self.assertIn('확인할 수 없습니다', notice)

    def test_no_analysis_is_not_reported_as_zero_anomalies(self):
        classified = chat_agent.Route(**route(intent='ANOMALY_SEARCH'))
        self.kit.evidence['s1']['coverage'] = {'expected_meter_days': 365, 'analyzed_meter_days': 0}
        self.kit.evidence['s1']['counts'] = {'주의': 0, '경고': 0}
        for text in ('주의는 0건입니다.', '경고가 0회 발생했습니다.'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'without analysis'):
                chat_agent._validated_answer(json.dumps(answer(answer=text)), self.kit, classified)
        text = '분석 자료가 없어 주의 횟수를 확인할 수 없습니다.'
        self.assertEqual(chat_agent._validated_answer(json.dumps(answer(answer=text)), self.kit, classified)[0], text)

    def aggregate(self, **counts):
        # find_anomalies 집계 출처의 모양: 라벨은 건수 표의 키로 항상 들어 있고, 작은 수(기간 7일)도 늘 있다.
        levels = {'정상': 7 - sum(counts.values()), '주의': 0, '경고': 0, '자료 확인': 0} | counts
        self.kit.sources[0]['kind'] = 'query'
        self.kit.evidence['s1'] = {'start_date': '2026-10-14', 'end_date': '2026-10-20', 'period_days': 7, 'station': '동부역',
                                  'severity_counts': levels, 'severity_day_counts': levels,
                                  'counts': {label: levels[label] for label in ('주의', '경고', '자료 확인')},
                                  'match_count': sum(counts.values()), 'matched_day_count': sum(counts.values()),
                                  'station_count': int(bool(counts)), 'selected_station_count': 1, 'severity_filter': None,
                                  'coverage': {'requested_start_date': '2026-10-14', 'requested_end_date': '2026-10-20',
                                               'expected_meter_days': 7, 'analyzed_meter_days': 7, 'stored_last_date': '2026-10-20'},
                                  'rows': [{'date': '2026-10-19', 'severity': label} for label, n in counts.items() for _ in range(n)]}
        return chat_agent.Route(**route(intent='ANOMALY_SEARCH', date=None, start_date='2026-10-14', end_date='2026-10-20'))

    def check(self, text, classified, ids=('s1',)):
        return chat_agent._validated_answer(json.dumps(answer(answer=text, source_ids=list(ids))), self.kit, classified)[0]

    def test_alert_counts_and_labels_must_come_from_the_cited_counts(self):
        classified = self.aggregate()
        for text in ('동부역은 2026-10-14~2026-10-20 동안 경고 7건, 주의 1건입니다.', '동부역에서 경고가 발생했습니다.', '경고는 총 1건입니다.'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'Unsupported (count|severity)'):
                self.check(text, classified)
        for text in ('2026-10-14~2026-10-20 동안 주의·경고는 0건입니다.', '경고 0건, 주의 0건입니다.', '7일 동안 경고는 없었습니다.',
                     '경고 판정은 확인되지 않았습니다.'):
            with self.subTest(text=text):
                self.assertEqual(self.check(text, classified), text)
        classified = self.aggregate(경고=2, 주의=1)
        for text in ('경고 2건, 주의 1건입니다.', '주의·경고는 총 3건입니다.', '경고는 2회 있었습니다.', '총 3건(계량기·사용일 기준)입니다.'):
            with self.subTest(text=text):
                self.assertEqual(self.check(text, classified), text)
        for text in ('경고 1건, 주의 2건입니다.', '주의·경고는 총 2건입니다.', '총 7건입니다.'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'Unsupported count'):
                self.check(text, classified)

    def test_untrusted_document_text_cannot_supply_labels_or_counts(self):
        classified = self.aggregate()
        self.kit.sources.append(dict(self.kit.sources[0], id='s2', kind='manual'))
        self.kit.evidence['s2'] = {'title': '점검 매뉴얼', 'kind': 'manual', 'untrusted_document': True,
                                  'excerpt': '점검 결과 경고 3건이 있었고 나머지는 정상이다. 2026-10-21 기준.'}
        for text in ('경고 3건이 있었습니다.', '2026-10-21 기준 모든 역은 정상입니다.'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.check(text, classified, ('s1', 's2'))
        # 문서만 인용하는 점검 안내는 문서의 표현을 그대로 옮길 수 있다.
        guide = chat_agent.Route(**route(intent='MAINTENANCE_GUIDE', station=None, date=None))
        text = '경고가 뜨면 점검 결과를 기록합니다.'
        self.assertEqual(self.check(text, guide, ('s2',)), text)

    def test_korean_and_dotted_date_phrases_are_checked_as_dates(self):
        classified = self.aggregate(경고=1)
        for text in ('10월 19일 경고 1건입니다.', '2026년 10월 19일 경고 1건입니다.', '2026.10.19 경고 1건입니다.'):
            with self.subTest(text=text):
                self.assertEqual(self.check(text, classified), text)
        for text in ('10월 21일 경고 1건입니다.', '2026년 10월 21일 경고 1건입니다.', '2026.10.21 경고 1건입니다.', '2026/10/21 경고 1건입니다.'):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'Unsupported dates'):
                self.check(text, classified)

    def test_missing_remote_storage_and_unregistered_corpus_are_distinct(self):
        self.kit.execute.return_value = {'error': '연결 실패', 'code': 'storage_unavailable'}
        self.responses = [response(route()), response(output=[tool()]), response(answer(source_ids=[]))]
        result = self.run_agent()
        self.assertIn('자료를 불러오지 못해', result['answer'])
        self.assertIn('서비스 운영 담당자에게 문의', result['answer'])
        self.assertNotIn('NCP', result['answer'])
        self.kit.execute.return_value = {'error': '자료 없음', 'code': 'arisu_unavailable'}
        self.responses = [response(route()), response(output=[tool('get_arisu_status',
                          {'meter_ref': None, 'station': '동부역', 'line': None, 'start_date': '2026-08-31', 'end_date': '2026-08-31'})]),
                          response(answer(source_ids=[]))]
        result = self.run_agent('동부역 아리수 원격검침 상태는?')
        self.assertIn('아리수 원격검침 자료가 아직 준비되지 않아', result['answer'])
        self.assertIn('서비스 운영 담당자에게 문의', result['answer'])
        self.assertNotIn('NCP', result['answer'])
        self.kit.execute.return_value = {'registered': False, 'matches': []}
        self.responses = [response(route(intent='MAINTENANCE_GUIDE', station=None)),
                          response(output=[tool('search_manuals', {'query': '밸브 점검'})]), response(answer(source_ids=[]))]
        result = self.run_agent('밸브 점검 방법')
        self.assertIn('점검 안내문이나 참고할 사례가 아직 준비되지 않아', result['answer'])
        self.assertIn('서비스 운영 담당자에게 자료 준비를 요청', result['answer'])
        self.assertEqual(result['sources'], [])

    def test_cause_is_hypothesis_and_definitive_cause_is_rejected(self):
        cause_route = chat_agent.Route(**route(intent='CAUSE_ANALYSIS'))
        result, _ = chat_agent._validated_answer(json.dumps(answer(answer='누수 가능성은 현장 확인이 필요합니다.')), self.kit, cause_route)
        self.assertIn('현장 확인 전에는 원인을 확정할 수 없습니다.', result)
        with self.assertRaises(ValueError):
            chat_agent._validated_answer(json.dumps(answer(answer='원인은 누수입니다.')), self.kit, cause_route)

    def test_loop_deadline_round_limit_and_tool_call_limit_are_enforced(self):
        self.responses = [response(route())] + [response(output=[tool(call_id=f'call_{n}')]) for n in range(chat_agent.MAX_ROUNDS)]
        result = self.run_agent()
        self.assertEqual(self.kit.execute.call_count, chat_agent.MAX_ROUNDS)
        self.assertEqual(result['generated_by'], 'none')
        self.kit.execute.reset_mock()
        self.responses = [response(route()), response(output=[tool(call_id=f'call_{n}') for n in range(chat_agent.MAX_TOOL_CALLS + 1)])]
        self.run_agent()
        self.kit.execute.assert_not_called()
        with self.assertRaises(HTTPException) as error:
            chat_agent._run(self.client, '동부역 현황', self.conversation, time.monotonic() - 1)
        self.assertEqual(error.exception.status_code, 504)

    def test_incomplete_or_malformed_classifier_never_loads_data(self):
        for fake in (response(route(), status='incomplete'), response({'domain': 'IN_SCOPE'}), response(route(date='2026-02-30'))):
            with self.subTest(fake=fake):
                self.responses = [fake]
                with self.assertRaises((ValueError, ValidationError)):
                    self.run_agent()
                self.tool_class.assert_not_called()

    def test_tool_arguments_reject_missing_extra_wrong_type_and_enum(self):
        schema = next(item for item in TOOL_SCHEMAS if item['name'] == 'find_anomalies')
        for args in ({'start_date': '2026-08-01', 'end_date': '2026-08-31'},
                     {'start_date': 1, 'end_date': '2026-08-31', 'severity': None},
                     {'start_date': '2026-08-01', 'end_date': '2026-08-31', 'severity': 'critical'}):
            with self.assertRaises(ValueError):
                chat_agent._arguments(schema, json.dumps(args))


if __name__ == '__main__':
    unittest.main()
