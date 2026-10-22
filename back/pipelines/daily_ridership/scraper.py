"""서울교통공사 역별승하차 API → 일별 역 총승객수(모델 입력 형식) 수집.

소스 : 공공데이터포털 B553766  getStnPsgr
키   : .env 의 SEOUL_PSGR_KEY (포털 Encoding 키 — URL 에 그대로 붙임, 재인코딩 금지)
출력 : APP_DATA_DIR/daily/ridership/<YYYY-MM-DD>.csv  (고객번호, 역명, 날짜, 총승객수)

집계: 응답은 일자×시간×권종×사용자구분별 승차(rideNope)/하차(gffNope) → 역·호선별 전부 합산.
매칭: API stnNm 은 부역명 '(...)' 을 달고 '역' 이 없으므로 등록된 계약의 역명과 정규화해 맞춘다.
      환승역은 호선별(모델 역명이 base+호선숫자). 검토 매핑(meter_match.csv)에 호선이 비어 있는 계량기는
      기존 정제 자료(data/processed/ridership.csv)와 같이 같은 이름의 역 전체 합계를 쓴다.
      월별 파일 수집(monthly.py)도 이 기준을 그대로 쓴다: 같은 고객·날짜에 쓰므로 기준이 하나여야 한다.
"""
from __future__ import annotations

import csv
import json
import os
import re
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date
from pathlib import Path

from back.pipelines.common import HISTORY, RUNTIME, collector_meters, customer_number, merge_csv, write_json
MATCH = HISTORY / "billing" / "meter_match.csv"
ENDPOINT = "https://apis.data.go.kr/B553766/psgr/getStnPsgr"
OUT_DIR = RUNTIME / "daily" / "ridership"
NUM_OF_ROWS = 1000   # 하루 ≈ 66k행 → ~66콜/일 (일일한도 10,000)


def _load_key() -> str:
    key = os.getenv("SEOUL_PSGR_KEY", "").strip()
    if not key:
        raise RuntimeError("SEOUL_PSGR_KEY is not configured")
    return key


def _normalize_station(name: str) -> str:
    """API/우리 역명을 공통 키로: 부역명 '(...)' · 끝 숫자 · 끝 '역' 제거."""
    s = re.sub(r"\(.*?\)", "", str(name).strip())
    s = re.sub(r"\d+$", "", s)
    return re.sub(r"역$", "", s)


def _parse_line(line_name: str) -> int | None:
    m = re.match(r"(\d+)호선", str(line_name))
    return int(m.group(1)) if m else None


def _load_meters(registered=None) -> list[dict]:
    """등록 계약 → 고객번호와 승하차 API 매칭용 역·호선."""
    with MATCH.open(encoding="utf-8-sig", newline="") as fp:
        whole_station = {customer_number(row["고객번호"]) for row in csv.DictReader(fp) if not row["호선"]}
    meters = []
    for row in collector_meters(registered):
        base = row["station_name"].strip()
        cid = customer_number(row["customer_number"])
        ln = None if cid in whole_station else row.get("line")
        line = int(float(ln)) if ln else None
        meters.append({
            "고객번호": cid,
            "역명": base,
            "key역": _normalize_station(base),
            "호선": line,
        })
    return meters


def _fetch_day(key: str, ymd: str) -> tuple[dict, dict]:
    """하루치를 받아 (역·호선)별·(역)별 총승객수를 만든다. 미적재면 ({}, {})."""
    def call(page: int, retries: int = 3) -> dict:
        url = (f"{ENDPOINT}?serviceKey={key}&pageNo={page}"
               f"&numOfRows={NUM_OF_ROWS}&dataType=JSON&pasngYmd={ymd}")
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        for attempt in range(1, retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, TimeoutError):
                if attempt == retries:
                    raise
                time.sleep(3)  # 일시적 SSL/네트워크 오류 재시도 (67콜 중 1건 타임아웃에 하루 전체가 죽는 것 방지)

    result = call(1)["response"]
    header = result.get("header", {})
    if str(header.get("resultCode", "00")) not in {"00", "000", "0"}:
        raise RuntimeError("Ridership API rejected request; check credentials and quota")
    body = result["body"]
    total = int(body["totalCount"])
    if total == 0 or body["items"] in ("", None):
        return {}, {}
    items = body["items"]["item"]
    if isinstance(items, dict):
        items = [items]
    pages = -(-total // NUM_OF_ROWS)
    received = 0

    byline: dict = defaultdict(int)
    bystn: dict = defaultdict(int)
    for page in range(1, pages + 1):
        rows = items if page == 1 else call(page)["response"]["body"]["items"]["item"]
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            raise ValueError("Incomplete ridership API page")
        received += len(rows)
        for it in rows:
            if "pasngYmd" in it and str(it["pasngYmd"]).replace("-", "") != ymd:
                raise ValueError("Ridership API returned a different date")
            n = _normalize_station(it["stnNm"])
            ride, alight = int(it["rideNope"]), int(it["gffNope"])
            if ride < 0 or alight < 0:
                raise ValueError("Negative ridership count")
            v = ride + alight
            byline[(n, _parse_line(it["lineNm"]))] += v
            bystn[n] += v
    if received != total:
        raise ValueError("Ridership API page count does not match totalCount")
    return byline, bystn


def scrape(target_date: date, *, meters=None, out_dir=None, fetcher=None) -> Path | None:
    """하루치 수집 → data/daily/ridership/<date>.csv 저장 후 경로 반환. 미적재면 None."""
    meters = _load_meters(meters)
    if not meters:
        raise ValueError("No registered daily collection targets")
    out_dir = Path(out_dir or OUT_DIR)
    byline, bystn = (fetcher or _fetch_day)(_load_key() if fetcher is None else "", target_date.strftime("%Y%m%d"))
    if not bystn:
        write_json(out_dir / f"{target_date}.status.json", {"date": str(target_date), "status": "pending", "fetched": 0})
        return None   # 아직 미적재(범위 밖/미게시)

    iso = target_date.strftime("%Y-%m-%d")
    rows = []
    for m in meters:
        total = byline.get((m["key역"], m["호선"])) if m["호선"] is not None else bystn.get(m["key역"])
        if total is None:
            print(f"  ⚠ {m['역명']}: 승하차 자료에서 찾지 못함")
            continue
        rows.append({"고객번호": m["고객번호"], "역명": m["역명"], "날짜": iso, "총승객수": int(total)})

    path = out_dir / f"{iso}.csv"
    merge_csv(path, rows, ["고객번호", "역명", "날짜", "총승객수"], ["고객번호", "날짜"])
    found = {r["고객번호"] for r in rows}
    write_json(out_dir / f"{iso}.status.json", {"date": iso,
        "status": "complete" if len(rows) == len(meters) else "partial",
        "expected": len(meters), "fetched": len(rows),
        "missing": [m["고객번호"] for m in meters if m["고객번호"] not in found]})
    return path if path.exists() else None
