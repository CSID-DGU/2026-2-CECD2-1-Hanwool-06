import unittest

from back.api import data_access


class CalendarCheck(unittest.TestCase):
    def test_holidays_come_from_the_package_so_no_calendar_file_is_needed(self):
        self.assertEqual(data_access.calendar_info('2026-10-03'),
                         {'weekend': True, 'holiday': True, 'holiday_name': '개천절', '요일': '토'})
        self.assertEqual(data_access.calendar_info('2026-10-22'),
                         {'weekend': False, 'holiday': False, 'holiday_name': '', '요일': '목'})
        self.assertTrue(data_access.calendar_info('2027-03-01')['holiday'])   # 옛 달력 파일(2026년까지) 밖의 해


if __name__ == '__main__':
    unittest.main()
