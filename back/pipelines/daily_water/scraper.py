"""Arisu monthly queries, keyed by registered customer number; resumable date ranges."""
from __future__ import annotations

import json
import math
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from back.pipelines.common import RUNTIME, collector_meters, customer_number, merge_csv, today, write_json
from back.scripts.billing_etl.i121_crawler.auth import (BASE, session_from_env, _looks_like_login_page,
    LoginError, CustomerAccessError, ensure_customer_access)
from back.scripts.billing_etl.i121_crawler.fetch import shift_month
from .smart_meter import fetch_smart_status, write_arisu_index

DAILY_DIR = RUNTIME / "daily" / "water"
FIELDS = ["고객번호", "역명", "납기", "사용일", "검침일자", "지침값", "일사용량(톤)", "납기별누적사용량(톤)", "상태"]


def _reading_date(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 8:
        raise ValueError("Missing Arisu observation date")
    return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))


def _reading_timestamp(value):
    digits = re.sub(r"\D", "", str(value or ""))
    formats = {8: "%Y%m%d", 10: "%Y%m%d%H", 12: "%Y%m%d%H%M", 14: "%Y%m%d%H%M%S"}
    if len(digits) not in formats:
        raise ValueError("Invalid Arisu measurement timestamp")
    measured = datetime.strptime(digits, formats[len(digits)])
    stamp = (str(measured.date()) if len(digits) == 8 else
             measured.isoformat(sep=" ", timespec="seconds" if len(digits) == 14 else "minutes"))
    return measured, stamp


def _reading_number(value, *, required=False):
    text = "" if value is None else str(value).replace(",", "").strip()
    if not text:
        if required:
            raise ValueError("Missing daily usage")
        return ""
    number = float(text)
    if not math.isfinite(number) or number < 0:
        raise ValueError("Invalid Arisu reading value")
    return text


def _request_readings(session, endpoint, params):
    response = session.get(
        f"{BASE}/cyber/front/mypage/{endpoint}", params=params,
        headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}, timeout=30,
    )
    if response.status_code in (401, 403) or "NR_loginForm.do" in response.url or _looks_like_login_page(response.text):
        raise LoginError("Arisu session expired during reading collection")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Unexpected Arisu reading response")
    if payload.get("result") is not True:
        message = str(payload.get("message", ""))
        if "조회 가능한 고객번호가 아닙니다" in message:
            raise CustomerAccessError("Arisu account cannot access this customer")
        if payload.get("loginUrl") or payload.get("code") in ("LOGIN_EXPIRED", "UNAUTHORIZED", 401, 403) or "로그인" in message:
            raise LoginError("Arisu session expired during reading collection")
        raise ValueError("Arisu reading query was rejected")
    return payload


def _reading_groups(payload, expected):
    # Check every caller, including unattended daily jobs, against fallback customers.
    if customer_number(payload.get("searchMkey", "")) != expected:
        raise ValueError("Arisu returned a different selected customer")
    groups = payload.get("groups")
    if not isinstance(groups, list):
        raise ValueError("Arisu reading groups are missing")
    for group in groups:
        if not isinstance(group, dict) or customer_number(group.get("mkey", "")) != expected:
            raise ValueError("Arisu returned another customer's observations")
    return groups


def _archive_readings(archive_dir, payload, expected, endpoint, params, name):
    _reading_groups(payload, expected)
    if archive_dir is not None:
        write_json(Path(archive_dir) / expected / name, {
            "customer_number": expected, "endpoint": endpoint, "query": params,
            "collected_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(), "payload": payload,
        })
        _save_reading_statuses(archive_dir, payload, expected)


def _save_reading_statuses(archive_dir, payload, expected):
    """Merge source statuses for display; raw responses retain unindexable rows."""
    expected = customer_number(expected)
    groups = _reading_groups(payload, expected)
    path = Path(archive_dir) / expected / "statuses.json"
    by_day = {}
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(previous, dict) or previous.get("customer_number") != expected
                or not isinstance(previous.get("readings"), list)):
            raise ValueError("Invalid Arisu reading status index")
        by_day = {reading["date"]: reading for reading in previous["readings"]}
    for group in groups:
        details = group.get("details")
        if not isinstance(details, list):
            continue
        for detail in details:
            if not isinstance(detail, dict):
                continue
            try:
                observed = str(_reading_date(detail.get("measDt")))
            except (TypeError, ValueError):
                continue
            try:
                measured, stamp = _reading_timestamp(detail.get("realGcDay"))
            except (TypeError, ValueError):
                measured, stamp = datetime.min, None
            previous = by_day.get(observed)
            previous_time = datetime.fromisoformat(previous["measured_at"]) if previous and previous.get("measured_at") else datetime.min
            # A fault can remove the timestamp; the new source status must still replace the old normal row.
            if stamp and previous and previous.get("measured_at") and measured < previous_time:
                continue
            status = detail.get("status")
            by_day[observed] = {"date": observed, "measured_at": stamp,
                                "status": status if isinstance(status, str) else None}
    result = {"customer_number": expected, "readings": [by_day[day] for day in sorted(by_day)]}
    write_json(path, result)
    return result


def _reading_rows(payload, meter, ym, *, issues=None):
    expected = customer_number(meter["customer_number"])
    rows, seen = [], {}
    for group in _reading_groups(payload, expected):
        due = _reading_date(group.get("napgi"))
        details = group.get("details")
        if not isinstance(details, list):
            raise ValueError("Arisu reading details are missing")
        for detail in details:
            if not isinstance(detail, dict):
                raise ValueError("Invalid Arisu reading row")
            if detail.get("status") != "정상":
                continue  # Fault rows may lack dates/times; their full contents are already archived.
            observed = _reading_date(detail.get("measDt"))
            if ym is not None and observed.strftime("%Y-%m") != ym:
                continue
            measured, measured_stamp = _reading_timestamp(detail.get("realGcDay"))
            # Today's amount can still change; only completed, normal readings are usable.
            if observed >= today() or measured.date() > today():
                continue
            try:
                pointer = _reading_number(detail.get("thsmmPointer"))
                usage = _reading_number(detail.get("dayUseQty"), required=True)
                cumulative = detail.get("thsmmUseqty")
                if cumulative is None or cumulative == "":
                    cumulative = group.get("thsmmUseqty")
                cumulative = _reading_number(cumulative)
            except ValueError:
                if issues is None:
                    raise
                issues.append(str(observed))
                continue
            core = (measured, float(pointer) if pointer else None, float(usage))
            if observed in seen:
                if seen[observed] != core:
                    raise ValueError("Conflicting Arisu daily observations")
                continue  # The same reading can appear under more than one due-month group.
            seen[observed] = core
            rows.append(dict(zip(FIELDS, [expected, meter["station_name"], str(due),
                str(observed), measured_stamp, pointer, usage, cumulative, detail["status"]])))
    return rows


def fetch_day_statuses(session, meter, *, archive_dir):
    """One DayList query refreshes the per-day meter statuses (statuses.json); usage files stay with the daily run."""
    expected = customer_number(meter["customer_number"])
    ensure_customer_access(session, expected)
    params = {"searchMkey": expected}
    latest = _request_readings(session, "JR_remoteMeterDayList.do", params)
    _archive_readings(archive_dir, latest, expected, "JR_remoteMeterDayList.do", params, f"day/{today()}.json")


def fetch_month(session, meter, ym, *, issues=None, archive_dir=None):
    expected = customer_number(meter["customer_number"])
    ensure_customer_access(session, expected)
    date.fromisoformat(ym + "-01")
    # Settled bi-monthly readings can belong to a due month three months later.
    history_params = {
        "searchMkey": expected, "searchStartYm": ym, "searchEndYm": shift_month(ym, 3),
    }
    history = _request_readings(session, "JR_remoteMeterHistory.do", history_params)
    _archive_readings(archive_dir, history, expected, "JR_remoteMeterHistory.do", history_params,
                      f"history/{ym}_{history_params['searchEndYm']}.json")
    latest = None
    if ym >= shift_month(today().strftime("%Y-%m"), -2):
        # History omits unbilled readings. Reuse one latest query per customer/job session.
        cache = getattr(session, "_arisu_day_readings", None)
        if not isinstance(cache, dict):
            cache = session._arisu_day_readings = {}
        key = (expected, str(today()))
        latest = cache.get(key)
        if latest is None:
            latest = _request_readings(session, "JR_remoteMeterDayList.do", {"searchMkey": expected})
        _archive_readings(archive_dir, latest, expected, "JR_remoteMeterDayList.do", {"searchMkey": expected},
                          f"day/{today()}.json")
    reading_issues = [] if issues is not None else None
    rows = {row["사용일"]: row for row in _reading_rows(history, meter, ym, issues=reading_issues)}
    if latest is not None:
        rows.update({row["사용일"]: row for row in _reading_rows(latest, meter, ym, issues=reading_issues)})
        cache[key] = latest
    if issues is not None:
        issues[:] = sorted((set(issues) | set(reading_issues)) - rows.keys())
    return [rows[day] for day in sorted(rows)]


def scrape_range(start: date, end: date, *, meters=None, session=None, out_dir=None, runtime_dir=None, sleep=0.4):
    if start > end:
        raise ValueError("start must be on or before end")
    meters = collector_meters(meters)
    if not meters:
        raise ValueError("No active Arisu customers enabled for daily collection")
    runtime_dir = Path(runtime_dir or (RUNTIME if out_dir is None else out_dir))
    out_dir = Path(out_dir or DAILY_DIR)
    session = session or session_from_env()
    days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    rows_by_day = {str(day): [] for day in days}
    failures = []
    smart_failures = []
    for meter in meters:
        try:
            smart = fetch_smart_status(session, meter)
            write_json(runtime_dir / "arisu" / f"{customer_number(meter['customer_number'])}.json", smart)
        except Exception as exc:
            smart_failures.append({"customer_number": meter["customer_number"], "error": type(exc).__name__})
        if sleep:
            time.sleep(sleep)
    for ym in sorted({day.strftime("%Y-%m") for day in days}):
        for meter in meters:
            try:
                issues = []
                for row in fetch_month(session, meter, ym, issues=issues,
                                       archive_dir=runtime_dir / "raw" / "remote_meter"):
                    if row["사용일"] in rows_by_day:
                        rows_by_day[row["사용일"]].append(row)
                failures.extend({"customer_number": meter["customer_number"], "month": ym,
                                 "date": day, "error": "InvalidReading"} for day in issues)
            except Exception as exc:
                failures.append({"customer_number": meter["customer_number"], "month": ym,
                                 "error": type(exc).__name__})
            if sleep:
                time.sleep(sleep)
    reports = []
    expected = {customer_number(m["customer_number"]) for m in meters}
    for day, rows in rows_by_day.items():
        path = out_dir / f"{day}.csv"
        if rows:
            merge_csv(path, rows, FIELDS, ["고객번호", "사용일"])
        found = {r["고객번호"] for r in rows}
        errors = [f for f in failures if f["month"] == day[:7] and f.get("date", day) == day]
        report = {"date": day, "expected": len(expected), "fetched": len(found),
                  "missing": sorted(expected - found), "errors": errors,
                  "smart_status_errors": smart_failures,
                  "status": "complete" if found == expected else "failed" if errors else "pending"}
        write_json(out_dir / f"{day}.status.json", report)
        reports.append(report)
    write_arisu_index(runtime_dir)
    return reports
