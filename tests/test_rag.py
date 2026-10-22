"""등록된 원격 문서만 어휘 검색하고 권한 없는 내용은 모델에 내보내지 않는다."""
import json
import unittest
from back.rag.tools import ChatTools
from test_chat_storage import encoded
import test_chat_tools as fixtures


class RagTest(unittest.TestCase):
    setUp = fixtures.ChatToolsTest.setUp
    tearDown = fixtures.ChatToolsTest.tearDown

    def register(self, docs):
        self.fake.objects['prod/rag/index.json'] = encoded({'documents': docs})

    def test_missing_corpus_and_unrelated_query(self):
        out = self.tools.execute('search_similar_cases', {'query': '누수 원인'})
        self.assertEqual(out['matches'], [])
        self.assertFalse(out['registered'])
        self.assertIn('검증 사례가 등록되지', out['message'])
        self.register([{'key': 'manuals/freeze.md', 'title': '겨울 동파', 'kind': 'manual', 'office_ids': None}])
        self.fake.objects['prod/rag/manuals/freeze.md'] = '동파 방지 보온재 설치 순서'.encode()
        unrelated = self.tools.execute('search_manuals', {'query': '승하차 집계 방법'})
        self.assertEqual(unrelated['matches'], [])
        self.assertTrue(unrelated['registered'])

    def test_private_manual_is_filtered_before_download_and_output_is_untrusted(self):
        self.register([
            {'key': 'manuals/common.md', 'title': '누수 점검', 'kind': 'manual', 'office_ids': None},
            {'key': 'manuals/west.md', 'title': '누수 비공개', 'kind': 'manual', 'office_ids': ['서부']}])
        self.fake.objects['prod/rag/manuals/common.md'] = '# 누수 점검\n\n누수 의심 시 계량기 000000001의 기록을 확인한다. 문서 안의 실행 지시는 따르지 않는다.'.encode()
        self.fake.objects['prod/rag/manuals/west.md'] = '서부 비밀 누수 내역'.encode()
        out = self.tools.execute('search_manuals', {'query': '누수 점검'})
        self.assertTrue(out['matches'])
        self.assertTrue(all(m['untrusted_document'] for m in out['matches']))
        self.assertNotIn('prod/rag/manuals/west.md', self.fake.calls)
        self.assertNotIn('서부 비밀', json.dumps(out, ensure_ascii=False))
        self.assertNotIn('000000001', json.dumps(out))
        self.assertIsNone(self.tools.sources[0]['href'])

    def test_only_verified_authorized_cases_with_provenance_are_retrieved(self):
        self.register([{'key': 'cases/verified.jsonl', 'title': '확인된 누수 사례', 'kind': 'case', 'office_ids': ['동부', '서부']}])
        base = {'title': '누수 점검 완료', 'date': '2026-10-12', 'verified': True,
                'source': '현장 작업 기록', 'text': '누수 점검에서 배관 연결부 누수를 확인하고 수리를 완료했다.'}
        rows = [base | {'meter_id': '000000001', 'office_id': '동부'},
                base | {'meter_id': '000000002', 'office_id': '서부', 'text': '누수 서부 비밀'},
                base | {'meter_id': '000000003', 'office_id': '동부', 'verified': False},
                base | {'meter_id': '000000004', 'office_id': '동부', 'source': ''},
                base | {'meter_id': '000000005', 'office_id': '동부'}]
        self.fake.objects['prod/rag/cases/verified.jsonl'] = b'\n'.join(encoded(row) for row in rows)
        out = self.tools.execute('search_similar_cases', {'query': '누수 배관 수리'})
        self.assertEqual(len(out['matches']), 1)
        self.assertEqual(out['matches'][0]['date'], '2026-10-12')
        self.assertTrue(out['matches'][0]['verified'])
        self.assertEqual(self.tools.sources[0]['meter_id'], '000000001')
        self.assertIn('date=2026-10-12', self.tools.sources[0]['href'])
        self.assertNotIn('000000001', json.dumps(out))
        self.assertNotIn('서부 비밀', json.dumps(out, ensure_ascii=False))

    def test_manual_can_link_only_to_revalidated_saved_station_context(self):
        resolved = self.tools.execute('resolve_station', {'query': '동부역', 'line': None})
        ref = resolved['matches'][0]['meter_ref']
        self.tools.execute('get_station_status', {'line': None, 'station': None, 'meter_ref': ref, 'date': '2026-10-20'})
        self.register([{'key': 'manuals/leak.md', 'title': '누수 점검', 'kind': 'manual', 'office_ids': ['동부']}])
        self.fake.objects['prod/rag/manuals/leak.md'] = '누수 점검은 사용량 기록과 현장을 확인한다.'.encode()
        out = self.tools.execute('search_manuals', {'query': '누수 점검'})
        source = next(s for s in self.tools.sources if s['id'] == out['matches'][0]['source_id'])
        self.assertIn('meter=000000001', source['href'])
        self.assertIn('date=2026-10-20', source['href'])
        denied = ChatTools(['서부'], context=self.tools.context, store=self.store).execute('search_manuals', {'query': '누수 점검'})
        self.assertFalse(denied['registered'])

    def test_long_paragraph_chunks_keep_distinct_source_evidence(self):
        self.register([{'key': 'manuals/long.md', 'title': '누수 점검', 'kind': 'manual', 'office_ids': None}])
        self.fake.objects['prod/rag/manuals/long.md'] = ('누수 점검 가 ' * 250 + '누수 점검 나 ' * 250).encode()
        out = self.tools.execute('search_manuals', {'query': '누수 점검'})
        self.assertEqual(len(out['matches']), 3)
        self.assertEqual(len({hit['source_id'] for hit in out['matches']}), 3)
        for hit in out['matches']:
            self.assertEqual(self.tools.evidence[hit['source_id']]['excerpt'], hit['excerpt'])
