"""Collect one registered Arisu contract without replacing existing observations."""
from __future__ import annotations

import json
import logging
import math
from datetime import date, timedelta

from back.api import catalog, config, db
from back.api.import_data import upsert_bill_summaries
from back.pipelines.common import collection_lock, customer_number, merge_csv, today, write_json
from back.pipelines.daily_water.scraper import FIELDS, fetch_month
from back.pipelines.refresh import publish_data
from back.scripts.billing_etl.crawl_bills import collect_bills
from back.scripts.billing_etl.i121_crawler.auth import ConfigurationError, CustomerAccessError, LoginError, collection_session, transient
from back.scripts.billing_etl.i121_crawler.fetch import shift_month

logger = logging.getLogger(__name__)


def _months(start, end):
    month = start.strftime("%Y-%m")
    while month <= end.strftime("%Y-%m"):
        yield month
        month = shift_month(month, 1)


def water_months(known_dates, end, checked):
    """Fill history gaps, retry recent days, and revisit older empty months weekly."""
    floor = date.fromisoformat(shift_month(end.strftime("%Y-%m"), -11) + "-01")
    known = {date.fromisoformat(d) for d in known_dates if date.fromisoformat(d) <= end}
    start = min(known) if known else floor
    missing_months = set()
    day = start
    while day <= end:
        if day not in known:
            missing_months.add(day.strftime("%Y-%m"))
        day += timedelta(days=1)
    # Recent unbilled readings can be published/corrected across a bi-monthly period.
    recent = set(_months(date.fromisoformat(shift_month(end.strftime("%Y-%m"), -2) + "-01"), end))
    retry_before = str(today() - timedelta(days=7))
    return sorted(recent | {month for month in missing_months if checked.get(month, "") <= retry_before})


def _bill_windows(meter_id, end, retries=()):
    with db.connect() as conn:
        periods = sorted({r[0] for r in conn.execute(
            "SELECT period FROM bills WHERE meter_id=? AND gubun='정기분'", (meter_id,))})
    # Query only gaps, not every already stored bill between an old gap and today.
    start = min(shift_month(end, -2), shift_month(periods[-1], 1)) if periods else shift_month(end, -11)
    windows = [(start, end), *(tuple(window) for window in retries)]
    # Consecutive regular due months show monthly billing. Supplementary notices cannot
    # establish that cadence or stand in for a missing regular bill.
    cycle = 1 if any(shift_month(a, 1) == b for a, b in zip(periods, periods[1:])) else 2
    windows += [(shift_month(older, 1), shift_month(newer, -1)) for older, newer in zip(periods, periods[1:])
                if shift_month(older, cycle) < newer]
    merged = []
    for start, finish in sorted(windows):
        if merged and start <= shift_month(merged[-1][1], 1):
            merged[-1] = (merged[-1][0], max(finish, merged[-1][1]))
        else:
            merged.append((start, finish))
    return list(reversed(merged))


def _result(status, message, daily_rows=0, bill_rows=0, daily_available=False, verified=False, retryable=False):
    return {"status": status, "message": message, "daily_rows": daily_rows, "bill_rows": bill_rows,
            "daily_available": daily_available, "connection_verified": verified, "retryable": retryable}


def collect_meter(meter, *, progress=None, session=None):
    """A failure affects this contract only; successful source rows remain usable."""
    if meter.get("provider") != "arisu":
        return _result("failed", "현재 아리수 고객번호의 자동 조회만 지원합니다.")
    if not meter.get("active", True) or meter.get("deleted_at"):
        return _result("failed", "비활성 또는 삭제된 계약은 조회하지 않습니다.")
    try:
        cid = customer_number(meter["customer_number"])
    except (KeyError, ValueError):
        return _result("failed", "고객번호 형식을 확인해 주세요.")
    notify = progress or (lambda message: None)
    notify("빠진 기간을 확인하는 중입니다.")
    owns_session = session is None
    with collection_lock(config.DATA_DIR):
        try:
            if owns_session:
                session = collection_session()
        except ConfigurationError:
            return _result("configuration_required", "서버에 아리수 아이디와 비밀번호를 설정해야 합니다.")
        except LoginError:
            return _result("failed", "아리수 로그인에 실패했습니다. 서버 계정과 접근 권한을 확인해 주세요.")
        except Exception as exc:
            logger.warning("Arisu session setup failed (%s)", type(exc).__name__)
            return _result("failed", "아리수에 연결하지 못했습니다. 잠시 후 다시 시도해 주세요.", retryable=transient(exc))
        try:
            return _collect(meter | {"customer_number": cid}, session, notify)
        except Exception as exc:
            # Exception text can contain a remote URL or credentials. Log types only.
            logger.error("Unexpected incremental collection failure (%s)", type(exc).__name__)
            return _result("failed", "수집한 자료를 저장하지 못했습니다. 기존 자료는 그대로입니다.")
        finally:
            if owns_session:
                session.close()


def _collect(meter, session, notify):
    cid, runtime = meter["customer_number"], config.DATA_DIR
    daily_enabled = bool(meter.get('daily_enabled'))
    state_path = runtime / "collection" / f"{cid}.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    checked = state.get("water_checked", {})
    # Days the source itself reports as missing or invalid; reported once, then rechecked quietly with the month.
    known_issues = set(state.get("water_excluded", []))
    old_daily, risk = catalog.data_sources()
    old_entry = old_daily.get(cid, {"usage": [], "ridership": []})
    observations = {r["date"]: r["value"] for r in old_entry.get("usage", [])} if daily_enabled else {}
    # Recover successful raw rows even if a previous snapshot write failed.
    for path in sorted((runtime / "daily" / "water").glob("*.csv")) if daily_enabled else ():
        for row in catalog.read_csv(path):
            if customer_number(row["고객번호"]) == cid:
                value = catalog.number(row.get("일사용량(톤)"))
                if value is not None and value >= 0:
                    observations[row["사용일"]] = value
    end = today() - timedelta(days=1)
    fresh, errors = {}, []
    allowed = getattr(session, 'arisu_customer_numbers', None)
    login_error = getattr(session, 'arisu_login_error', None)
    # Network errors are retried the same morning; a login lost to one leaves the whole session without account
    # pages, so every failure on it is. A rejected login never is: each retry logs in again and could lock the account.
    login_rejected = isinstance(login_error, str) and not transient(login_error)
    retry = isinstance(login_error, str) and not login_rejected
    notes = []
    # Consecutive usable customer lists that did not contain this customer.
    unlisted = 0 if isinstance(allowed, frozenset) and cid in allowed else state.get("unlisted_runs", 0)
    if daily_enabled and isinstance(allowed, frozenset) and cid not in allowed:
        if isinstance(login_error, str):
            errors.append('일일 사용량: 아리수 계정 설정 또는 로그인을 확인해 주세요' if login_rejected else '일일 사용량: 아리수 서버에 연결하지 못했습니다')
        elif not allowed:
            # An empty list is a failed lookup, not proof that every customer left the account.
            errors.append('일일 사용량: 아리수에서 고객 목록을 받지 못해 이번에는 조회하지 않았습니다')
        elif unlisted < 1:
            # A single list without this customer may be a partial response; switch off only when it repeats.
            unlisted += 1
            errors.append('일일 사용량: 아리수 계정의 고객 목록에 이 고객번호가 없습니다. 다음 수집에서도 없으면 일일 관제에서 제외합니다')
        else:
            unlisted += 1
            # The account cannot read this customer's daily meter: keep bills only instead of failing every day.
            with db.connect() as conn:
                conn.execute("UPDATE meters SET daily_enabled=0,updated_at=? WHERE id=?", (db.now(), meter["id"]))
                db.audit(conn, None, "meter_daily_disabled", meter["id"], {"reason": "아리수 계정에 등록되지 않은 고객번호"})
            notes.append('일일 사용량: 아리수 계정에 등록되지 않은 고객번호라 일일 관제에서 제외하고 청구서만 수집합니다. '
                         '계정에 등록한 뒤 계량기 관리에서 다시 켜세요')
        months = []
    else:
        months = water_months(observations, end, checked) if daily_enabled else []
    gaps = []   # days the source itself left without a usable value
    for month in months:
        notify(f"{month} 일일 사용량을 조회하는 중입니다.")
        try:
            issues = []
            rows = fetch_month(session, meter, month, issues=issues)
            for row in rows:
                observed = date.fromisoformat(row["사용일"])
                value = float(str(row["일사용량(톤)"]).replace(",", ""))
                if observed > end:
                    continue
                if customer_number(row["고객번호"]) != cid or not math.isfinite(value) or value < 0:
                    raise ValueError("Invalid customer or usage")
                merge_csv(runtime / "daily" / "water" / f"{observed}.csv", [row], FIELDS, ["고객번호", "사용일"])
                fresh[str(observed)] = value
            gaps += [day for day in issues if day not in known_issues]
            known_issues.update(issues)
            checked[month] = str(today())
        except CustomerAccessError:
            errors.append("일일 사용량: 현재 아리수 계정으로 조회할 수 없습니다. 청구서는 별도로 조회합니다")
            break
        except LoginError:
            errors.append("아리수 인증이 만료되었습니다")
            break
        except Exception as exc:
            logger.warning("Water query %s failed (%s)", month, type(exc).__name__)
            errors.append(f"{month} 일일 사용량 조회에 실패했습니다")
            retry = retry or transient(exc)
    if gaps:   # a notice beside usable readings; with no usable reading at all, the source itself may be broken
        (notes if fresh else errors).append(f"{', '.join(gaps)} 아리수 검침값 누락·오류로 해당 관측을 제외했습니다")
    notify("청구서를 조회하는 중입니다.")
    bills = []
    bill_end = today().strftime('%Y-%m')
    retries = state.get('bill_retry_windows', [])
    if 'bill_retry_windows' not in state and state.get('bill_retry_from'):
        retries = [(state['bill_retry_from'], bill_end)]
    windows = _bill_windows(meter['id'], bill_end, retries)
    failed_windows = list(windows)
    try:
        with db.connect() as conn:
            known_details = {(cid, r['period'], r['gubun'], r['notice_number']) for r in conn.execute(
                'SELECT period,gubun,notice_number,payload FROM bills WHERE meter_id=?', (meter['id'],))
                if json.loads(r['payload']).get('detail_available')}
        failures = []
        for bill_start, window_end in windows:
            notify(f"{bill_start}~{window_end} 청구서를 조회하는 중입니다.")
            report = collect_bills(start_ym=bill_start, end_ym=window_end, mkeys=[cid], session=session,
                customer_names={cid: meter.get('metadata', {}).get('arisu_customer_name', '')},
                known_details=known_details, cache_dir=runtime / "raw/billing_i121/i121_cache",
                out_dir=runtime / "raw/billing_i121/i121_bills", include_rows=True, sleep=0)
            for row in report['rows']:
                date.fromisoformat(row['napgi'])
                if customer_number(row['mkey']) != cid:
                    raise ValueError('Bill customer mismatch')
            bills.extend(report['rows'])
            failures.extend(report['errors'])
            if not report['errors']:
                failed_windows.remove((bill_start, window_end))
            if any(e.get('error') == 'PublicCustomerNameError' for e in report['errors']):
                break
        if failures:
            retry = retry or any(transient(str(e.get('error'))) for e in failures)
            error_types = {e.get('error') for e in failures}
            if error_types & {'CustomerAccessError', 'PublicCustomerNameError'}:
                errors.append('청구서: 고객번호와 고지서상 성명을 확인해 주세요')
            elif 'LoginError' in error_types:
                errors.append('청구서: 아리수 인증이 만료되었습니다')
            else:
                errors.append('일부 청구 내역 조회에 실패했습니다')
    except LoginError:
        errors.append("아리수 인증이 만료되었습니다")
    except Exception as exc:
        logger.warning("Bill collection failed (%s)", type(exc).__name__)
        errors.append("청구 내역 조회에 실패했습니다")
        retry = retry or transient(exc)
    notify("수집한 자료를 저장하는 중입니다.")
    if bills:
        try:
            with db.connect() as conn:
                upsert_bill_summaries(conn, bills)
                address = next((r["address"] for r in reversed(bills) if r.get("address")), "")
                conn.execute("UPDATE meters SET address=CASE WHEN address='' THEN ? ELSE address END WHERE id=?", (address, meter["id"]))
        except Exception as exc:
            logger.error("Collected data database write failed (%s)", type(exc).__name__)
            failed_windows = list(windows)
            errors.append("수집 결과의 데이터베이스 반영에 실패했습니다")
    observations.update(fresh)
    previous_observations = {r["date"]: r["value"] for r in old_entry.get("usage", [])}
    if observations and observations != previous_observations:
        try:
            daily = dict(old_daily)
            daily[cid] = old_entry | {"usage": [{"date": d, "value": v} for d, v in sorted(observations.items())]}
            manifest_path = runtime / "snapshot/manifest.json"
            manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
            publish_data(daily, risk, output_dir=runtime / "snapshot", withheld=manifest.get("withheld", []))
        except Exception as exc:
            logger.error("Incremental snapshot publication failed (%s)", type(exc).__name__)
            errors.append("일일 자료의 화면 반영에 실패했습니다")
    known_issues -= set(fresh)   # a day the source has since corrected is an ordinary observation again
    try:
        write_json(state_path, {"water_checked": checked, "water_excluded": sorted(known_issues), "unlisted_runs": unlisted,
                                "bill_retry_from": None, "bill_retry_windows": failed_windows, "updated_at": db.now()})
    except OSError as exc:
        logger.error("Collection checkpoint write failed (%s)", type(exc).__name__)
        errors.append("수집 상태 저장에 실패했습니다")
    valid = bool(fresh or bills)
    status = "partial" if errors and valid else "failed" if errors else "success" if valid else "empty"
    if errors:
        message = "; ".join(dict.fromkeys(errors)) + ". 수집된 자료는 저장했고, 나머지는 다음 수집 때 다시 시도합니다."
    elif valid:
        message = (f"일일 사용량 {len(fresh)}건, 청구 내역 {len(bills)}건을 확인했습니다." if daily_enabled
                   else f"청구 전용 계량기: 청구 내역 {len(bills)}건을 확인했습니다.")
    else:
        message = "조회된 자료가 없습니다. 이 기간에 자료가 없거나, 이 계정으로 볼 수 없는 고객번호일 수 있습니다."
    if notes:
        message = "; ".join(notes) + ". " + message
    return _result(status, message, len(fresh), len(bills), bool(observations), valid,
                   retryable=bool(errors) and retry and not login_rejected)
