import unittest
from io import BytesIO

from pypdf import PdfReader

from back.api.documents import bill_pdf


class DocumentCheck(unittest.TestCase):
    def test_public_detail_preserves_groundwater_and_source(self):
        meter = {"display_name": "시험역 지하수 계량기", "customer_number": "000000001", "office_name": "시험사업소", "tariff": "일반용",
                 "address": "서울특별시 성동구 지하 연결통로 시설관리실 " * 3}
        bill = {"ym": "2026-05", "source": "i121_public_detail", "고지서성명": "시험공사***", "사용량": None,
                "총사용량": 0, "지하수사용량": 4384, "지하수전월지침": 9616, "지하수당월지침": 14000,
                "상수도_기본료": 0, "상수도_사용료": 0, "하수도_사용료": 57000, "납부금액": 57000,
                "납기일": "2026-05-31", "periodStart": "2026-03-21", "periodEnd": "2026-05-20"}
        reader = PdfReader(BytesIO(bill_pdf(meter, bill)))
        self.assertEqual(len(reader.pages), 1)
        text = "".join(reader.pages[0].extract_text().split())
        for expected in ("지하수사용량(톤)", "4,384", "9,616", "14,000", "57,000", "시험공사***",
                         "2026-05-31", "2026-03-21", "2026-05-20", "아리수요금전체조회상세자료"):
            self.assertIn(expected, text)
        self.assertIn("상·하수도사용량(톤)-", text)

    def test_legacy_groundwater_alias_and_zero_are_preserved(self):
        for bill, shows_4384 in (({"지하수_사용량": 4384}, True), ({"지하수사용량": 0, "지하수_사용량": 4384}, False)):
            text = "".join(PdfReader(BytesIO(bill_pdf({}, bill))).pages[0].extract_text().split())
            self.assertIn("지하수사용량(톤)", text)
            self.assertEqual("4,384" in text, shows_4384)

    def test_missing_csv_usage_is_not_replaced_with_total_zero(self):
        bill = {"source": "details_csv", "사용량": None, "총사용량": 0}
        text = "".join(PdfReader(BytesIO(bill_pdf({}, bill))).pages[0].extract_text().split())
        self.assertIn("상·하수도사용량(톤)-", text)

    def test_additional_fees_show_only_recorded_values_in_one_page(self):
        meter = {"display_name": "시험역 지하수 계량기", "customer_number": "000000001", "address": "서울특별시 성동구 지하 연결통로 시설관리실"}
        bill = {"ym": "2026-09", "gubun": "수시분", "source": "i121_public_detail", "고지서성명": "시험공사***",
                "계량기대금": 12000, "설치비": 25000, "연체금": 0, "납부금액": 94000, "사용량": None,
                "지하수사용량": 4384, "지하수전월지침": 9616, "지하수당월지침": 14000, "하수도_사용료": 57000,
                "notice_number": "000000090", "납기일": "2026-09-30", "periodStart": "2026-07-21", "periodEnd": "2026-09-20",
                "전자수용가번호": "000000000000001", "전자납부번호": "000000000000002"}
        reader = PdfReader(BytesIO(bill_pdf(meter, bill)))
        self.assertEqual(len(reader.pages), 1)
        text = "".join(reader.pages[0].extract_text().split())
        for expected in ("계량기대금(원)", "설치비(원)", "연체금(원)", "12,000", "25,000", "4,384"):
            self.assertIn(expected, text)
        absent = PdfReader(BytesIO(bill_pdf({}, {"계량기대금": None}))).pages[0].extract_text()
        for key in ("계량기대금", "설치비", "연체금"):
            self.assertNotIn(key, absent)


if __name__ == "__main__":
    unittest.main()
