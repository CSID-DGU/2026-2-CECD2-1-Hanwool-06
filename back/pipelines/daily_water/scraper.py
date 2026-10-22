"""Arisu monthly queries, keyed by registered customer number; resumable date ranges."""
from __future__ import annotations

import math
import re
import time
from datetime import date, timedelta
from pathlib import Path

from back.pipelines.common import RUNTIME, collector_meters, customer_number, merge_csv, today, write_json
from back.scripts.billing_etl.i121_crawler.auth import (BASE, session_from_env, _looks_like_login_page,
    LoginError, CustomerAccessError, ensure_customer_access)
from back.scripts.billing_etl.i121_crawler.fetch import shift_month

DAILY_DIR = RUNTIME / "daily" / "water"
FIELDS = ["고객번호", "역명", "납기", "사용일", "검침일자", "지침값", "일사용량(톤)", "납기별누적사용량(톤)"]


def _reading_date(value):
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 8:
        raise ValueError("Missing Arisu observation date")
    return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))


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


def _reading_rows(payload, meter, ym, *, issues=None):
    expected = customer_number(meter["customer_number"])
    # Check every caller, including unattended daily jobs, against fallback customers.
    if customer_number(payload.get("searchMkey", "")) != expected:
        raise ValueError("Arisu returned a different selected customer")
    groups = payload.get("groups")
    if not isinstance(groups, list):
        raise ValueError("Arisu reading groups are missing")
    rows, seen = [], {}
    for group in groups:
        if not isinstance(group, dict) or customer_number(group.get("mkey", "")) != expected:
            raise ValueError("Arisu returned another customer's observations")
        due = _reading_date(group.get("napgi"))
        details = group.get("details")
        if not isinstance(details, list):
            raise ValueError("Arisu reading details are missing")
        for detail in details:
            if not isinstance(detail, dict):
                raise ValueError("Invalid Arisu reading row")
            observed = _reading_date(detail.get("measDt"))
            if observed.strftime("%Y-%m") != ym:
                continue
            measured = _reading_date(detail.get("realGcDay"))
            # Today's amount can still change; only completed, normal readings are usable.
            if observed >= today() or measured > today() or detail.get("status") != "정상":
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
                str(observed), str(measured), pointer, usage, cumulative])))
    return rows


def fetch_month(session, meter, ym, *, issues=None):
    expected = customer_number(meter["customer_number"])
    ensure_customer_access(session, expected)
    date.fromisoformat(ym + "-01")
    # Settled bi-monthly readings can belong to a due month three months later.
    history = _request_readings(session, "JR_remoteMeterHistory.do", {
        "searchMkey": expected, "searchStartYm": ym, "searchEndYm": shift_month(ym, 3),
    })
    reading_issues = [] if issues is not None else None
    rows = {row["사용일"]: row for row in _reading_rows(history, meter, ym, issues=reading_issues)}
    if ym >= shift_month(today().strftime("%Y-%m"), -2):
        # History omits unbilled readings. Reuse one latest query per customer/job session.
        cache = getattr(session, "_arisu_day_readings", None)
        if not isinstance(cache, dict):
            cache = session._arisu_day_readings = {}
        key = (expected, str(today()))
        latest = cache.get(key)
        if latest is None:
            latest = _request_readings(session, "JR_remoteMeterDayList.do", {"searchMkey": expected})
        rows.update({row["사용일"]: row for row in _reading_rows(latest, meter, ym, issues=reading_issues)})
        cache[key] = latest
    if issues is not None:
        issues[:] = sorted((set(issues) | set(reading_issues)) - rows.keys())
    return [rows[day] for day in sorted(rows)]


def scrape_range(start: date, end: date, *, meters=None, session=None, out_dir=None, sleep=0.4):
    if start > end:
        raise ValueError("start must be on or before end")
    meters = collector_meters(meters)
    if not meters:
        raise ValueError("No active Arisu customers enabled for daily collection")
    out_dir = Path(out_dir or DAILY_DIR)
    session = session or session_from_env()
    days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    rows_by_day = {str(day): [] for day in days}
    failures = []
    for ym in sorted({day.strftime("%Y-%m") for day in days}):
        for meter in meters:
            try:
                issues = []
                for row in fetch_month(session, meter, ym, issues=issues):
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
                  "status": "complete" if found == expected else "failed" if errors else "pending"}
        write_json(out_dir / f"{day}.status.json", report)
        reports.append(report)
    return reports
