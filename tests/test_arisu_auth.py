"""Current Arisu login and levy contracts, with synthetic responses only."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from back.scripts.billing_etl.i121_crawler import auth, fetch, parser


def page(body, url='https://i121.seoul.go.kr/cyber/front/mypage/NR_myArisu.do'):
    return Mock(text=body, url=url)


def token(value):
    return f'<meta name="_csrf" content="{value}"><meta name="_csrf_header" content="X-CSRF-TOKEN"><meta name="_csrf_parameter" content="_csrf">'


def bills(day, number=1):
    return f'''<table><caption>부과내역이며, 고객번호를 제공합니다.</caption><tbody>
      <tr><td>{number}</td><td>000000001</td><td>정기분</td><td>2026-{day[:2]}-{day[2:]}</td>
      <td>미납</td><td>1,000</td><td>서울 가역</td><td>20</td>
      <td><button data-napgi="2026{day}"></button></td></tr></tbody></table>
      <a onclick="jsMovePage(2); return false;">2</a>'''


class CurrentArisuTest(unittest.TestCase):
    def test_login_failure_keeps_public_billing_available_without_secret_text(self):
        for error in (auth.ConfigurationError('private'), auth.LoginError('private')):
            session = Mock()
            with patch.object(auth, 'session_from_env', side_effect=error), patch.object(auth, '_new_session', return_value=session):
                self.assertIs(auth.collection_session(), session)
            self.assertEqual(session.arisu_login_error, type(error).__name__)
            with self.assertRaises(auth.CustomerAccessError):
                auth.ensure_customer_access(session, '000000001')

    def test_every_server_error_is_retried_then_reported_as_transient(self):
        retries = auth._new_session().get_adapter('https://i121.seoul.go.kr').max_retries
        self.assertTrue({429, 500, 501, 503, 507, 599} <= set(retries.status_forcelist))
        self.assertTrue(all(auth.transient(name) for name in ('RetryError', 'ReadTimeout', 'ConnectTimeout', 'ConnectionError')))
        self.assertFalse(any(auth.transient(name) for name in ('LoginError', 'ConfigurationError', 'CustomerAccessError', 'HTTPError', 'ValueError')))

    def test_login_sends_current_fields_csrf_and_preserves_password(self):
        session = Mock(headers={})
        session.get.side_effect = [page(token('before') + '<input name="mbrId"><input name="pwd">'),
                                   page(token('after') + '<title>마이아리수</title><script>const message="로그인이 필요";</script><select name="searchMkey"><option value="000000001">가역</option></select>')]
        session.post.return_value = page('200')
        with patch.object(auth, '_new_session', return_value=session):
            self.assertIs(auth.login('sample', ' pass with spaces '), session)
        post = session.post.call_args
        self.assertEqual(post.args[0], auth.BASE + '/cyber/front/login/AR_loginAction.do')
        self.assertEqual(post.kwargs['data'], {'mbrId': 'sample', 'pwd': ' pass with spaces ', 'mbrTypeCd': '01', '_csrf': 'before'})
        self.assertEqual(session.headers['X-CSRF-TOKEN'], 'after')
        self.assertEqual(session.arisu_customer_numbers, frozenset({'000000001'}))
        auth.ensure_customer_access(session, '000000001')
        with self.assertRaises(auth.CustomerAccessError):
            auth.ensure_customer_access(session, '000000002')
        session.close.assert_not_called()

    def test_login_rejection_never_reads_or_exposes_member_data(self):
        session = Mock(headers={})
        session.get.return_value = page(token('before'))
        session.post.return_value = page('loginLimit|private account response')
        with patch.object(auth, '_new_session', return_value=session):
            with self.assertRaises(auth.LoginError) as error:
                auth.login('sample', 'password')
        self.assertNotIn('private account response', str(error.exception))
        session.get.assert_called_once()
        session.close.assert_called_once()

    def test_levy_posts_customer_range_and_collects_all_pages(self):
        session = Mock(headers={})
        session.post.side_effect = [page(bills('0930')), page(bills('0731', 2))]
        with tempfile.TemporaryDirectory() as directory:
            html, cached = fetch.fetch_bill_window(session, '000000001', '2026-01', '2026-09', Path(directory))
            self.assertFalse(cached)
            self.assertEqual(session.post.call_count, 2)
            self.assertEqual(session.post.call_args.kwargs['data']['pageIndex'], 2)
            self.assertEqual(session.post.call_args.kwargs['data']['searchMkey'], '000000001')
            parsed = parser.parse_bill_list(html)
            self.assertEqual([r['napgi_compact'] for r in parsed], ['20260930', '20260731'])
            self.assertEqual([r['total_usage_ton'] for r in parsed], [20, 20])
            _, cached = fetch.fetch_bill_window(session, '000000001', '2026-01', '2026-09', Path(directory))
            self.assertTrue(cached)
            self.assertEqual(session.post.call_count, 2)

    def test_invalid_historical_bill_cache_is_refetched(self):
        for invalid in (bills('0930').replace('000000001', '000000002'), bills('0931'),
                        bills('0930').replace('data-napgi="20260930"', 'data-napgi="20260731"')):
            with self.subTest(invalid=invalid[:20]), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                cache = root / '000000001_2026-01_2026-09.html'
                cache.write_text(invalid)
                session = Mock(headers={})
                session.post.side_effect = [page(bills('0930')), page(bills('0731', 2))]
                html, cached = fetch.fetch_bill_window(session, '000000001', '2026-01', '2026-09', root)
                self.assertFalse(cached)
                self.assertEqual(session.post.call_count, 2)
                self.assertEqual(len(parser.parse_bill_list(html)), 2)
                self.assertEqual(cache.read_text(), html)

    def test_wrong_customer_page_never_replaces_a_valid_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / '000000001_2026-01_2026-09.html'
            original = bills('0930')
            cache.write_text(original)
            session = Mock(headers={})
            session.post.side_effect = [page(bills('0930')), page(bills('0731').replace('000000001', '000000002'))]
            with self.assertRaises(ValueError):
                fetch.fetch_bill_window(session, '000000001', '2026-01', '2026-09', root, force=True)
            self.assertEqual(cache.read_text(), original)

    def test_supplementary_notice_numbers_require_matching_selected_customer(self):
        form = '<form action="/cyber/front/mypage/NR_levySearch.do"><input type="hidden" name="searchMkey" value="000000001"></form>'
        first = bills('0408').replace('000000001', '900000001').replace('정기분', '수시분')
        second = bills('0408', 2).replace('000000001', '900000002').replace('정기분', '수시분')
        session = Mock(headers={})
        session.post.side_effect = [page(form + first), page(form + second)]
        with tempfile.TemporaryDirectory() as directory:
            html, _ = fetch.fetch_bill_window(session, '000000001', '2026-01', '2026-09', Path(directory))
            rows = parser.parse_bill_list(html, customer='000000001')
            self.assertEqual([r['mkey'] for r in rows], ['000000001', '000000001'])
            self.assertEqual([r['notice_number'] for r in rows], ['900000001', '900000002'])
        for invalid in (first, form.replace('000000001', '000000002') + first,
                        form + bills('0408').replace('000000001', '000000002')):
            with self.subTest(invalid=invalid[:30]), self.assertRaises(ValueError):
                fetch._bill_page(invalid, '000000001')

    def test_current_customer_selector_and_expired_bill_session(self):
        self.assertEqual(parser.discover_mkeys('<select id="selfTestcusNum" name="searchMkey"><option value="000000001">가역</option></select>'), ['000000001'])
        session = Mock(headers={})
        session.post.return_value = page('<input name="mbrId"><input name="pwd">')
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(auth.LoginError):
                fetch.fetch_bill_window(session, '000000001', '2026-01', '2026-09', Path(directory))
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == '__main__':
    unittest.main()
