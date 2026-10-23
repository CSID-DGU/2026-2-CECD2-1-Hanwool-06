"""Smart-meter source parsing never turns missing data into a safe/zero reading."""
import unittest
from datetime import datetime
from unittest.mock import Mock

from back.pipelines.daily_water.smart_meter import fetch_smart_status, parse_smart_status
from back.scripts.billing_etl.i121_crawler.auth import CustomerAccessError, LoginError


CUSTOMER = '000000001'


def page(body, *, selected=CUSTOMER):
    return f'''<select name="searchMkey"><option value="{selected}" selected>선택 고객</option></select>
    <div class="box smartRemoteMeter"><div class="title"><p>스마트검침내역(원격검침내역)</p></div>
    <div class="remoteMeter">{body}</div></div>'''


def card(leak='안전', usage='4톤', updated='2026-10-24 00:00 갱신'):
    return f'''<div class="remoteMeterBox">
        <div class="risk"><div class="inner"><span>누수위험</span><strong>{leak}</strong></div></div>
        <div class="usage"><div class="inner"><span>실시간 사용량</span><strong>{usage}</strong></div></div>
        </div><div class="updateTimeBox"><p class="updateTime">{updated}</p>
        <button>원격검침 과거내역 조회</button><button>원격검침 일별 내역 조회</button></div>'''


class SmartMeterTest(unittest.TestCase):
    def test_safe_caution_and_zero_usage_are_source_values(self):
        for risk, usage, expected in [('안전', '4톤', 4.0), ('주의', '0톤', 0.0), ('주의', '1,234.5톤', 1234.5)]:
            with self.subTest(risk=risk, usage=usage):
                result = parse_smart_status(page(card(risk, usage)), CUSTOMER)
                self.assertEqual(result['leak_status'], risk)
                self.assertEqual(result['realtime_usage'], expected)
                self.assertEqual(result['updated_at'], '2026-10-24 00:00')
                self.assertEqual(result['customer_number'], CUSTOMER)
                self.assertIsNone(result['meter_status'])
                self.assertIsNone(result['notice'])
                self.assertIsNotNone(datetime.fromisoformat(result['collected_at']).tzinfo)

    def test_fault_notice_keeps_exact_source_wording(self):
        for notice in ('계량기 장애로 인해 원격검침 정보를 제공할 수 없습니다.', '계량기장애', '단말기장애', '통신 오류'):
            with self.subTest(notice=notice):
                result = parse_smart_status(page(f'<p class="notice">{notice}</p>'), CUSTOMER)
                self.assertEqual(result['meter_status'], notice)
                self.assertEqual(result['notice'], notice)
                self.assertIsNone(result['leak_status'])
                self.assertIsNone(result['realtime_usage'])
                self.assertIsNone(result['updated_at'])

    def test_fault_in_risk_usage_or_label_is_not_lost(self):
        for body in (card(usage='계량기 장애'), card(leak='계량기장애', usage='-'),
                     card().replace('<span>실시간 사용량</span>', '<span>단말기장애</span>'),
                     card().replace('<span>누수위험</span>', '<span>계량기장애</span>')):
            with self.subTest(body=body):
                result = parse_smart_status(page(body), CUSTOMER)
                self.assertIn('장애', result['meter_status'])
        self.assertIsNone(parse_smart_status(page(card(leak='계량기장애')), CUSTOMER)['leak_status'])

    def test_missing_data_does_not_imply_safe_normal_or_zero(self):
        result = parse_smart_status(page('<p>원격검침 데이터가 없습니다.</p>'), CUSTOMER)
        for field in ('leak_status', 'meter_status', 'realtime_usage', 'updated_at'):
            self.assertIsNone(result[field])
        self.assertEqual(result['notice'], '원격검침 데이터가 없습니다.')
        for usage in ('-', '', '-1톤', 'NaN톤'):
            self.assertIsNone(parse_smart_status(page(card(usage=usage)), CUSTOMER)['realtime_usage'])
        result = parse_smart_status(page(card(leak='-') + '<p>원격검침 데이터 수집중</p>'), CUSTOMER)
        self.assertIsNone(result['leak_status'])
        self.assertEqual(result['notice'], '원격검침 데이터 수집중')

    def test_wrong_or_missing_selected_customer_and_missing_card_are_rejected(self):
        for html in (page(card(), selected='000000002'), page(card()).replace(' selected', ''),
                     '<select name="searchMkey"><option value="000000001" selected>고객</option></select>'):
            with self.subTest(html=html), self.assertRaises(ValueError):
                parse_smart_status(html, CUSTOMER)

    def test_fetch_checks_session_and_account_access(self):
        session = Mock()
        session.arisu_customer_numbers = frozenset({CUSTOMER})
        session.get.return_value = Mock(text=page(card()), status_code=200, url='https://i121.seoul.go.kr/mypage')
        self.assertEqual(fetch_smart_status(session, {'customer_number': CUSTOMER})['realtime_usage'], 4)
        self.assertEqual(session.get.call_args.kwargs['params']['searchMkey'], CUSTOMER)
        with self.assertRaises(CustomerAccessError):
            fetch_smart_status(session, {'customer_number': '000000002'})
        for response in (
            Mock(text=page(card()), status_code=403, url='https://i121.seoul.go.kr/mypage'),
            Mock(text=page(card()), status_code=200, url='https://i121.seoul.go.kr/NR_loginForm.do'),
            Mock(text='<input name="mbrId"><input name="pwd">', status_code=200, url='https://i121.seoul.go.kr/mypage'),
        ):
            session.get.return_value = response
            with self.assertRaises(LoginError):
                fetch_smart_status(session, {'customer_number': CUSTOMER})


if __name__ == '__main__':
    unittest.main()
