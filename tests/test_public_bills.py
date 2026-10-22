"""Public bill lookup regressions using synthetic, non-account fixtures."""
import csv
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from back.scripts.billing_etl.i121_crawler import public
from back.scripts.billing_etl import crawl_bills


def page(text):
    return Mock(text=text, status_code=200)


def listing(start='2026-08', end='2026-09', due='20260930'):
    return f'''<form id="searchForm"><input name="searchMkey" value="000000001">
      <input name="searchCsNm" value="가역"><input name="searchStartYm" value="{start}">
      <input name="searchEndYm" value="{end}"></form>
      <table><caption>조회결과표로 순번, 구분을 제공합니다.</caption><tbody><tr>
      <td>1</td><td>정기분</td><td>{due[:4]}-{due[4:6]}</td><td>미납</td><td>2,000</td>
      <td><button class="btn-detail-view" data-goji-gubun="1" data-napgi="{due}"
      data-cstmidno="000000001" data-search-mkey="000000001" data-search-cs-nm="가역"></button></td>
      </tr></tbody></table>'''


def detail(due='20260930'):
    return f'''<button data-mkey="000000001" data-napgi="{due}"></button>
      <ul><li><p>고객번호 (자동납부신청번호)</p><span>000000001</span></li>
      <li><p>총사용량</p><p>0</p></li><li><p>주소 :</p><p>서울 가역</p></li></ul>
      <table><caption>총 사용금액, 차감금액, 납부금액</caption>
      <tr><td>2,000</td><td>0</td><td>2,000</td></tr></table>
      <table><caption>정기검침일, 상・하수도, 지하수 정보를 제공합니다.</caption>
      <tr><td>사용량</td><td>-</td><td>4,384</td></tr></table>
      <p class="textInfo">사용기간 2026년 06월 28일 ~ 2026년 08월 27일</p>'''


class PublicBillsTest(unittest.TestCase):
    def test_public_customer_lookup_and_detail_preserve_groundwater_meaning(self):
        session = Mock(headers={}, arisu_customer_numbers=frozenset())
        session.get.return_value = page('')
        session.post.side_effect = [page(listing()), page(detail())]
        with tempfile.TemporaryDirectory() as directory:
            rows, cached, errors = public.fetch_public_window(session, '000000001', '가역', '2026-08', '2026-09', Path(directory))
            self.assertFalse(cached)
            self.assertEqual(errors, [])
            values = rows[0]['details']
            self.assertEqual((values['사용량'], values['지하수사용량'], values['총사용량']), (None, 4384, 0))
            self.assertEqual(values['periodStart'], '2026-06-28')
            self.assertEqual(rows[0]['detail_source'], 'i121_public_detail')
            _, cached, _ = public.fetch_public_window(session, '000000001', '가역', '2026-08', '2026-09', Path(directory))
            self.assertTrue(cached)
            self.assertEqual(session.post.call_count, 2)

    def test_wrong_names_and_wrong_detail_customer_are_rejected(self):
        for html in (listing().replace('value="가역"', 'value="다른역"'),
                     listing() + '<script>var noResultMessage = "고객번호와 사용자명을 확인해 주세요.";</script>',
                     listing().replace('data-search-mkey="000000001"', 'data-search-mkey="000000002"')):
            with self.assertRaises(public.PublicCustomerNameError):
                public.parse_public_list(html, '000000001', '가역', '2026-08', '2026-09')
        bill = public.parse_public_list(listing(), '000000001', '가역', '2026-08', '2026-09')[0]
        with self.assertRaises(ValueError):
            public.parse_public_detail(detail().replace('data-mkey="000000001"', 'data-mkey="000000002"'), bill)

    def test_supplementary_notice_uses_verified_form_and_its_own_detail_identity(self):
        html = listing().replace('data-goji-gubun="1"', 'data-goji-gubun="2"')
        html = html.replace('data-cstmidno="000000001"', 'data-cstmidno="900000001"')
        html = html.replace('data-search-mkey="000000001"', 'data-search-mkey=""').replace('data-search-cs-nm="가역"', 'data-search-cs-nm=""')
        bill = public.parse_public_list(html, '000000001', '가역', '2026-08', '2026-09')[0]
        self.assertEqual((bill['mkey'], bill['notice_number']), ('000000001', '900000001'))
        detail_html = '<ul><li><p>고지번호</p><p>900000001</p></li><li><p>납부기한</p><strong>2026년09월30일</strong></li><li><p>납부금액</p><strong>99,350원</strong></li></ul>'
        self.assertEqual(public.parse_public_detail(detail_html, bill)['납부금액'], 99350)
        with self.assertRaises(ValueError):
            public.parse_public_detail(detail_html.replace('900000001', '900000002'), bill)
        with self.assertRaises(public.PublicCustomerNameError):
            public.parse_public_list(html.replace('data-search-mkey=""', 'data-search-mkey="000000002"'), '000000001', '가역', '2026-08', '2026-09')

    def test_cli_uses_current_registry_names_and_applies_limit(self):
        args = Mock(mkeys_path=None, max_mkeys=1, start_ym='2026-01', end_ym='2026-09',
                    cache_dir=None, out_dir=None, sleep=0, env_path=None)
        meters = [{'customer_number': cid, 'metadata': {'arisu_customer_name': name}}
                  for cid, name in [('000000001', '가역'), ('000000002', '나역')]]
        with tempfile.TemporaryDirectory() as directory, patch.object(crawl_bills, 'RUNTIME', Path(directory)), \
             patch.object(crawl_bills, 'parse_args', return_value=args), patch.object(crawl_bills, 'collector_meters', return_value=meters), \
             patch.object(crawl_bills, 'collection_session', return_value=Mock()), patch.object(crawl_bills, 'collect_bills', return_value={'errors': []}) as collect:
            self.assertEqual(crawl_bills.main(), 0)
        self.assertEqual(collect.call_args.kwargs['mkeys'], ['000000001'])
        self.assertEqual(collect.call_args.kwargs['customer_names'], {'000000001': '가역', '000000002': '나역'})

    def test_current_unpaid_bill_outside_historical_window_is_not_duplicated(self):
        self.assertEqual(public.parse_public_list(listing('2024-01', '2024-12'), '000000001', '가역', '2024-01', '2024-12'), [])

    def test_known_historical_details_skip_remote_detail_without_skipping_summary(self):
        session = Mock(headers={})
        session.get.return_value = page('')
        session.post.return_value = page(listing('2024-01', '2024-12', '20240930'))
        with tempfile.TemporaryDirectory() as directory, patch.object(public, 'today', return_value=date(2026, 9, 23)):
            rows, _, errors = public.fetch_public_window(session, '000000001', '가역', '2024-01', '2024-12', Path(directory),
                known_details={('000000001', '2024-09', '정기분', '')})
            self.assertEqual(session.post.call_count, 1)
            self.assertEqual(rows[0]['bugwa_amount_won'], 2000)
            self.assertNotIn('details', rows[0])
            self.assertEqual(errors, [])

    def test_detail_failure_keeps_summary_but_does_not_cache_incomplete_success(self):
        session = Mock(headers={})
        session.get.return_value = page('')
        session.post.side_effect = [page(listing()), page('<div id="serverBlockMessage"></div>')]
        with tempfile.TemporaryDirectory() as directory:
            rows, _, errors = public.fetch_public_window(session, '000000001', '가역', '2026-08', '2026-09', Path(directory))
            self.assertEqual(len(rows), 1)
            self.assertEqual(errors[0]['error'], 'ValueError')
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_bad_name_stops_old_windows_and_summary_refresh_preserves_raw_details(self):
        with tempfile.TemporaryDirectory() as directory:
            args = dict(start_ym='2020-01', end_ym='2026-09', mkeys=['1'], customer_names={'1': '가역'},
                        session=Mock(), cache_dir=Path(directory), out_dir=Path(directory), sleep=0)
            with patch.object(crawl_bills, 'fetch_public_window', side_effect=public.PublicCustomerNameError()) as fetch:
                result = crawl_bills.collect_bills(**args)
                self.assertEqual(fetch.call_count, 1)
                self.assertEqual(result['errors'][0]['error'], 'PublicCustomerNameError')
            row = public.parse_public_list(listing(), '000000001', '가역', '2026-08', '2026-09')[0]
            full = row | {'details': {'납부금액': 2000, '총사용량': 0}, 'detail_source': 'i121_public_detail'}
            args['start_ym'] = '2026-08'
            with patch.object(crawl_bills, 'fetch_public_window', side_effect=[([full], False, []), ([row], False, [])]):
                crawl_bills.collect_bills(**args)
                crawl_bills.collect_bills(**args)
            with (Path(directory) / 'bills_long.csv').open(encoding='utf-8-sig') as stream:
                stored = list(csv.DictReader(stream))
            self.assertEqual(json.loads(stored[0]['details']), full['details'])
            self.assertEqual(stored[0]['detail_source'], 'i121_public_detail')


if __name__ == '__main__':
    unittest.main()
