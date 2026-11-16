"""Current Arisu login and levy contracts, with synthetic responses only."""
import json
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

    def registration(self, *, flag='Y', used=0, reply=auth.REGISTERED, before=('000000001',), listed=('000000001', '000000009'),
                     lookup=None, excluded=None, release_reply=None):
        """A session answering the 고객번호등록 form's requests in the order the page sends them.
        before/listed: the account's customers before and after. release_reply: the answer to removing another member's link."""
        def answer(payload):
            response = page(json.dumps(payload), auth.MKEY_URL)
            response.json.return_value = payload
            return response

        def account(customers):
            options = ''.join(f'<option value="{customer}">역</option>' for customer in customers)
            return page(token('after') + f'<select name="searchMkey">{options}</select>')
        session = Mock(headers={}, arisu_customer_numbers=frozenset({'000000001'}))
        session.get.side_effect = [account(before), page(token('form'), auth.MKEY_URL + '/NR_mkeyList.do'), answer({'mkeyUseFlag': used}), account(listed)]
        session.post.side_effect = [answer(excluded or {'success': True, 'blocked': False}), answer({'csNmFlag': flag, 'area': '1'}),
                                    answer(lookup or {'success': True, 'dataMap': {'mkey': '000000009', 'csNm': '나역', 'gcMthCd': auth.REMOTE_READ}}),
                                    *([page(release_reply, auth.MKEY_URL)] if release_reply is not None else []), page(reply, auth.MKEY_URL)]
        return session

    @staticmethod
    def posted(session):
        return [(call.args[0].rsplit('/', 1)[1], call.kwargs['data']) for call in session.post.call_args_list]

    def test_registration_runs_the_forms_checks_then_inserts_once(self):
        session = self.registration()
        self.assertEqual(auth.register_customer(session, '000000009', '나역', '9'), 'registered')
        posts = self.posted(session)
        self.assertEqual([endpoint for endpoint, _ in posts],
                         ['JR_mkeySearchExceptCheck.do', 'JR_getCsNmFlag.do', 'JR_mkeyInsertLookup.do', 'TR_mkeyInsertAction.do'])
        self.assertEqual(posts[1][1], {'searchMkey': '000000009', 'searchCsNm': '나역'})
        self.assertEqual(posts[-1][1], {'_csrf': 'form', 'cusNo': '000000009', 'userName': '나역', 'mkey': '000000009',
                                        'searchMkey': '000000009', 'searchCsNm': '나역', 'csRelationCd': '9'})
        self.assertEqual(session.get.call_args_list[2].kwargs['params'], {'searchMkey': '000000009'})
        self.assertEqual(session.arisu_customer_numbers, frozenset({'000000001', '000000009'}))
        self.assertEqual(session.headers['X-CSRF-TOKEN'], 'after')

    def test_registration_starts_from_a_fresh_account_list(self):
        # The list read at login can be partial. A fresh one that has the customer leaves nothing to register or remove.
        session = self.registration(before=('000000001', '000000009'), used=1, release_reply=auth.RELEASED)
        self.assertEqual(auth.register_customer(session, '000000009', '나역', '9', release=True), 'listed')
        session.post.assert_not_called()
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(session.arisu_customer_numbers, frozenset({'000000001', '000000009'}))
        # An empty list is a failed lookup, never an empty account: nothing is asked or removed on its strength.
        empty = self.registration(before=(), used=1, release_reply=auth.RELEASED)
        with self.assertRaises(ValueError):
            auth.register_customer(empty, '000000009', '나역', '9', release=True)
        empty.post.assert_not_called()

    def test_another_members_link_is_removed_only_when_asked_and_before_the_insert(self):
        session = self.registration(used=1, release_reply=auth.RELEASED)
        self.assertEqual(auth.register_customer(session, '000000009', '나역', '9', release=True), 'released')
        posts = self.posted(session)
        self.assertEqual([endpoint for endpoint, _ in posts[3:]], ['NR_mkeyDeleteUpdateAction.do', 'TR_mkeyInsertAction.do'])
        self.assertEqual(posts[3][1], {'searchMkey': '000000009', 'searchCsNm': '나역'})
        self.assertEqual(session.arisu_customer_numbers, frozenset({'000000001', '000000009'}))
        # The site did not confirm the removal: its notice is the refusal, and nothing is inserted.
        unconfirmed = self.registration(used=1, release_reply='처리할 수 없습니다.')
        with self.assertRaises(auth.RegistrationRefused) as refused:
            auth.register_customer(unconfirmed, '000000009', '나역', '9', release=True)
        self.assertEqual(str(refused.exception), '처리할 수 없습니다.')
        self.assertEqual([endpoint for endpoint, _ in self.posted(unconfirmed)][3:], ['NR_mkeyDeleteUpdateAction.do'])
        # A free number needs no removal, with or without leave to remove.
        free = self.registration()
        self.assertEqual(auth.register_customer(free, '000000009', '나역', '9', release=True), 'registered')
        self.assertFalse(any('Delete' in endpoint for endpoint, _ in self.posted(free)))

    def test_a_meter_not_read_remotely_is_never_registered_or_taken_from_another_member(self):
        # No remote meter, no daily readings: registering would gain nothing, so the other member keeps the number.
        manual = self.registration(lookup={'success': True, 'dataMap': {'mkey': '000000009', 'gcMthCd': '01'}}, used=1, release_reply=auth.RELEASED)
        with self.assertRaises(auth.NotRemoteMeter):
            auth.register_customer(manual, '000000009', '나역', '9', release=True)
        self.assertEqual([endpoint for endpoint, _ in self.posted(manual)], ['JR_mkeySearchExceptCheck.do', 'JR_getCsNmFlag.do', 'JR_mkeyInsertLookup.do'])
        self.assertEqual(manual.get.call_count, 2)   # the account list and the form; who holds the number is never asked
        # Arisu not saying how the meter is read is no answer either way: nothing is registered, and it may be asked again.
        silent = self.registration(lookup={'success': True, 'dataMap': {'mkey': '000000009'}}, used=1, release_reply=auth.RELEASED)
        with self.assertRaises(ValueError):
            auth.register_customer(silent, '000000009', '나역', '9', release=True)
        self.assertEqual(silent.post.call_count, 3)

    def test_remote_read_asks_only_the_lookup(self):
        for method, expected in (('03', True), ('01', False), ('', None), (None, None)):
            with self.subTest(method=method):
                response = page('{}', auth.MKEY_URL)
                response.json.return_value = {'success': True, 'dataMap': {'mkey': '000000009', 'gcMthCd': method}}
                session = Mock(headers={})
                session.post.return_value = response
                self.assertIs(auth.remote_read(session, '000000009', '나역'), expected)
                self.assertEqual(self.posted(session), [('JR_mkeyInsertLookup.do', {'mkey': '000000009', 'searchMkey': '000000009', 'searchCsNm': '나역'})])
                session.get.assert_not_called()

    def test_registration_refusals_stop_before_the_insert_and_never_unlink_another_member(self):
        cases = [('name', {'flag': 'N'}, '불일치', 2), ('other member', {'used': 1}, '다른 회원', 3), ('unreadable use flag', {'used': 'x'}, '다른 회원', 3),
                 ('excluded', {'excluded': {'success': True, 'blocked': True}}, '검색 제외', 1),
                 ('policy', {'lookup': {'success': False, 'message': '50세대 이상은 등록할 수 없습니다.'}}, '50세대', 3)]
        for label, answers, text, posts in cases:
            with self.subTest(label):
                session = self.registration(**answers)
                with self.assertRaises(auth.RegistrationRefused) as refused:
                    auth.register_customer(session, '000000009', '나역', '9')   # without leave to remove another member's link
                self.assertIn(text, str(refused.exception))
                self.assertEqual(session.post.call_count, posts)
                self.assertFalse(any('TR_mkeyInsertAction' in endpoint or 'Delete' in endpoint for endpoint, _ in self.posted(session)))
                self.assertEqual(session.arisu_customer_numbers, frozenset({'000000001'}))

    def test_registration_reply_decides_and_the_account_list_confirms(self):
        session = self.registration(reply='이미 등록된 고객번호입니다.')
        with self.assertRaises(auth.RegistrationRefused) as refused:
            auth.register_customer(session, '000000009', '나역', '9')
        self.assertEqual(str(refused.exception), '이미 등록된 고객번호입니다.')
        # An answer without the site's own notice is not a refusal: the caller may ask again.
        for label, unclear in (('html', self.registration(reply='<html><body>오류</body></html>')),
                               ('not listed', self.registration(listed=('000000001',))),
                               ('no notice', self.registration(lookup={'success': False})),
                               ('other customer', self.registration(lookup={'success': True, 'dataMap': {'mkey': '000000008'}}))):
            with self.subTest(label), self.assertRaises(ValueError):
                auth.register_customer(unclear, '000000009', '나역', '9')
            self.assertEqual(unclear.arisu_customer_numbers, frozenset({'000000001'}))
        with self.assertRaises(auth.LoginError):
            auth.register_customer(self.registration(reply='<input name="mbrId"><input name="pwd">'), '000000009', '나역', '9')
        untouched = self.registration()
        for name, relation in (('나역', ''), ('나역', '12'), ('', '9')):
            with self.assertRaises(ValueError):
                auth.register_customer(untouched, '000000009', name, relation)
        untouched.get.assert_not_called()
        untouched.post.assert_not_called()

if __name__ == '__main__':
    unittest.main()
