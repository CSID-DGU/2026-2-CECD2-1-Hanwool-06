"""서울 열린데이터광장 월별 승하차 파일(OA-12914)로 지난 날짜의 승하차를 채운다.

일별 API(scraper.py)는 최근 1주일치만 주므로, 그보다 오래된 날짜는 이 월별 파일이 유일한 원천이다.
인증키는 필요 없다. 출력은 scraper.py 와 같다: APP_DATA_DIR/daily/ridership/<YYYY-MM-DD>.csv

    python -m back.pipelines.daily_ridership.monthly 2026-05 2026-08

집계 기준은 scraper.py 와 같다(같은 원천으로 만든 data/processed/ridership.csv 의 기준): 검토 매핑에
호선이 비어 있는 계량기는 같은 이름의 역 전체 합계를, 호선이 있는 계량기와 새 계량기는 그 호선만 쓴다.
"""
from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

import requests

from back.pipelines.common import RUNTIME, atomic_bytes, collection_lock
from back.pipelines.daily_ridership.scraper import _normalize_station, _parse_line, scrape
from back.scripts.billing_etl.i121_crawler.fetch import shift_month

LIST_URL = "https://data.seoul.go.kr/dataList/OA-12914/F/1/datasetView.do"
DOWNLOAD_URL = "https://datafile.seoul.go.kr/bigfile/iot/inf/nio_download.do?useCache=false"
RAW_DIR = RUNTIME / "raw" / "ridership_monthly"


def published_months(session) -> dict[str, str]:
    """{'2026-05': 내려받기 번호}. 아직 올라오지 않은 달은 들어 있지 않다."""
    page = session.get(LIST_URL, timeout=60)
    page.raise_for_status()
    found = re.findall(r"downloadFile\('(\d+)'\)[^>]*>CARD_SUBWAY_MONTH_(\d{4})(\d{2})\.csv", page.text)
    return {f"{year}-{month}": seq for seq, year, month in found}


def _rows(content: bytes):
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("cp949")  # 2022년 이전 파일
    return csv.DictReader(text.splitlines(), strict=True)   # 따옴표가 닫히지 않은 채 끊긴 행을 값으로 읽지 않는다


def _parse(content: bytes) -> dict:
    """{YYYYMMDD: ((역, 호선)별 합계, 역별 합계)}. 승차와 하차를 더한다. 읽을 수 없는 행이 하나라도 있으면 ValueError."""
    days = defaultdict(lambda: (defaultdict(int), defaultdict(int)))
    try:
        for row in _rows(content):
            ymd = str(row["사용일자"])
            if not re.fullmatch(r"\d{8}", ymd):
                raise ValueError("Unexpected date in monthly ridership file")
            date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:]))   # 달력에 없는 날짜(20260532)는 여기서 걸린다
            ride, alight = int(row["승차총승객수"]), int(row["하차총승객수"])   # 잘려서 칸이 모자란 행은 여기서 걸린다
            if ride < 0 or alight < 0:
                raise ValueError("Negative ridership count")
            station = _normalize_station(row["역명"])
            byline, bystn = days[ymd]
            byline[(station, _parse_line(row["노선명"]))] += ride + alight
            bystn[station] += ride + alight
    except (csv.Error, KeyError, TypeError) as error:
        raise ValueError("Damaged monthly ridership file") from error
    return days


def _is_month(content: bytes, month: str) -> bool:
    """그 달의 온전한 승하차 파일인가: 모든 행이 읽히고 모든 날짜가 그 달이다. 빈 응답·잘린 파일·다른 달이 섞인 파일은 아니다."""
    try:
        days = _parse(content)
    except ValueError:
        return False
    return bool(days) and all(day.startswith(month.replace("-", "")) for day in days)


def download_month(month: str, seq: str, session, raw_dir=None) -> Path:
    """한 달 파일을 받아 둔다. 제대로 받아 둔 달은 다시 받지 않는다."""
    path = Path(raw_dir or RAW_DIR) / f"CARD_SUBWAY_MONTH_{month.replace('-', '')}.csv"
    if path.exists() and _is_month(path.read_bytes(), month):
        return path   # 빈 파일이나 잘못 받은 파일이 남아 있으면 믿지 않고 다시 받는다
    response = session.post(DOWNLOAD_URL, timeout=120, headers={"Referer": LIST_URL, "Origin": "https://data.seoul.go.kr"},
                            data={"infId": "OA-12914", "infSeq": "3", "seqNo": seq, "seq": seq})
    response.raise_for_status()
    if response.content.lstrip()[:1] == b"<":
        raise ValueError("Monthly ridership download returned a page instead of a CSV file")
    if not _is_month(response.content, month):
        raise ValueError("Monthly ridership download is empty, damaged, or not the requested month")
    atomic_bytes(path, response.content)
    return path


def read_month(path) -> dict:
    return _parse(Path(path).read_bytes())


def import_month(path, *, meters=None, out_dir=None) -> list[str]:
    """한 달 파일을 날짜별 파일로 풀어 쓴다. 같은 고객·날짜의 기존 행은 새 값으로 바뀐다."""
    written = []
    for ymd, totals in sorted(read_month(path).items()):
        day = date(int(ymd[:4]), int(ymd[4:6]), int(ymd[6:]))
        if scrape(day, meters=meters, out_dir=out_dir, fetcher=lambda _key, _ymd, totals=totals: totals):
            written.append(str(day))
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("start", help="첫 달 YYYY-MM")
    parser.add_argument("end", nargs="?", help="마지막 달 YYYY-MM (생략하면 첫 달만)")
    args = parser.parse_args()
    end = args.end or args.start
    for month in (args.start, end):
        date.fromisoformat(month + "-01")
    if args.start > end:
        parser.error("첫 달이 마지막 달보다 늦습니다.")
    session = requests.Session()
    session.headers["User-Agent"] = "Mozilla/5.0"
    published = published_months(session)
    missing = False
    month = args.start
    with collection_lock(RUNTIME):
        while month <= end:
            if month in published:
                days = import_month(download_month(month, published[month], session))
                print(f"{month}: {len(days)}일치 저장 ({days[0]} ~ {days[-1]})" if days else f"{month}: 저장한 날짜 없음")
            else:
                print(f"{month}: 아직 올라오지 않은 달입니다.")
                missing = True
            month = shift_month(month, 1)
    from back.storage.hooks import after_refresh
    after_refresh(RUNTIME)   # 받은 월별 원본과 날짜별 파일을 Object Storage에도 둔다(꺼져 있으면 아무 일도 하지 않음)
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
