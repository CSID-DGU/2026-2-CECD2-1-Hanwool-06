"""Private meter catalogue and permission-scoped application data."""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from datetime import date as Date, datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

from . import config, db
from back.pipelines.common import today


def customer_id(value) -> str:
    value = re.sub(r"\.0$", "", str(value or "").strip())
    return value.zfill(9) if value.isdigit() else value


def number(value):
    if value in (None, "", "-", "—"):
        return None
    try:
        value = float(str(value).replace(",", ""))
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def bill_usage_field(bill):
    """Detailed water usage may be unknown; only summary rows fall back to totals."""
    detailed = not bill.get('summary_only') and (
        '사용량' in bill or bill.get('detail_available') or bill.get('source') == 'i121_public_detail')
    return '사용량' if bill.get('사용량') is not None or detailed else '총사용량'


def bill_usage(bill):
    return number(bill.get(bill_usage_field(bill)))


def read_csv(path):
    if Path(path).exists():
        with Path(path).open(encoding="utf-8-sig", newline="") as stream:
            yield from csv.DictReader(stream)


def _json(path, default=None):
    return json.loads(Path(path).read_text(encoding="utf-8-sig")) if Path(path).exists() else (default or {})


def bill_id(meter_id, period, gubun="정기분", notice_number=""):
    return f"{meter_id}:{period}:{gubun}" + (f":{notice_number}" if notice_number else "")


def _bootstrap_legacy(conn):
    """Import repository seeds once; never overwrite subsequent administrative edits."""
    if conn.execute("SELECT 1 FROM settings WHERE key='catalog_seed_v1'").fetchone():
        return
    if not (config.SEED_DIR / "bills.json").is_file():
        return   # 이력 자료가 아직 없다(복원 전). 완료 표시를 남기지 않아, 자료가 생기면 다음 시작 때 적재한다
    bills = _json(config.SEED_DIR / "bills.json")
    stations = _json(config.SEED_DIR / "stations.json")
    positions = _json(config.SEED_DIR / "locations.json")
    matches = {customer_id(r["고객번호"]): r for r in read_csv(config.HISTORY_DIR / "billing/meter_match.csv")}
    reviews = {customer_id(r["고객번호"]): r for r in read_csv(config.HISTORY_DIR / "billing/mkey_station_map.csv")}
    now = datetime.now(timezone.utc).isoformat()
    for cid, entry in bills.items():
        cid = customer_id(cid)
        meta = stations.get(cid, {})
        match = matches.get(cid, {})
        name = re.sub(r"\d+$", "", meta.get("역명") or entry.get("역명") or cid)
        office = meta.get("영업사업소") or entry.get("사업소명") or "미지정"
        pos = positions.get(cid, {})
        conn.execute("INSERT OR IGNORE INTO offices(id,name) VALUES(?,?)", (office, office))
        conn.execute("INSERT OR IGNORE INTO stations(id,name,map_x,map_y) VALUES(?,?,?,?)",
                     (name, name, pos.get("x"), pos.get("y")))
        if pos:
            conn.execute("UPDATE stations SET map_x=COALESCE(map_x,?),map_y=COALESCE(map_y,?) WHERE id=?",
                         (pos["x"], pos["y"], name))
        metadata = {k: v for k, v in entry.items() if k != "bills"}
        metadata["daily_csv_name"] = match.get("일일CSV파일명")
        metadata["review_notes"] = {k: v for k, v in reviews.get(cid, {}).items() if "검토" in k and v}
        conn.execute("""INSERT OR IGNORE INTO meters
            (id,provider,customer_number,station_id,office_id,line,display_name,purpose,tariff,address,
             active,daily_enabled,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)""",
            (cid, "arisu", cid, name, office, str(meta.get("호선") or ""),
             meta.get("역명") or entry.get("역명") or name, match.get("용도") or "",
             entry.get("용도") or "", entry.get("주소") or "", int(cid in matches),
             json.dumps(metadata, ensure_ascii=False), now, now))
        for bill in entry.get("bills", []):
            period = bill["ym"]
            gubun = bill.get("gubun") or "정기분"
            conn.execute("INSERT OR IGNORE INTO bills(id,meter_id,period,gubun,payload,source) VALUES(?,?,?,?,?,?)",
                         (bill_id(cid, period, gubun), cid, period, gubun,
                          json.dumps(bill, ensure_ascii=False), "legacy_seed"))
    conn.execute("INSERT INTO settings(key,value) VALUES('catalog_seed_v1',?)", (now,))


# (역, 처음 위치 자료에서 다른 역과 함께 쓰던 점, 노선도 그림에서 다시 읽은 자리) — 2026-10-20 확인. 환승역은 계량기 호선의 동그라미.
POSITION_FIXES = [
    ("홍대입구역", (32.7, 40.2), (31.4, 42.8)), ("망원역", (26.3, 42.8), (27.9, 44.7)),
    ("가산디지털단지역", (32.6, 70.7), (30.4, 70.7)), ("마천역", (82.5, 71.6), (84.0, 71.6)),
    ("성수역", (68.1, 41.8), (69.2, 44.5)), ("청구역", (60.3, 36.0), (58.3, 36.0)), ("창동역", (66.9, 9.8), (69.0, 9.9)),
]


def bootstrap(conn):
    """Run each seed migration once, including databases bootstrapped before details existed."""
    _bootstrap_legacy(conn)
    details = config.SEED_DIR / "details_long.csv"
    if details.is_file() and not conn.execute("SELECT 1 FROM settings WHERE key='catalog_details_v1'").fetchone():
        from .import_data import import_details
        counts = import_details(conn, details)
        conn.execute("INSERT INTO settings(key,value) VALUES('catalog_details_v1',?)",
                     (json.dumps(counts, ensure_ascii=False),))
    collection = config.SEED_DIR / 'meter_collection.csv'
    if collection.is_file() and not conn.execute("SELECT 1 FROM settings WHERE key='catalog_collection_v1'").fetchone():
        for row in read_csv(collection):
            cid = customer_id(row['customer_number'])
            meter = conn.execute("SELECT id,metadata FROM meters WHERE provider='arisu' AND customer_number=?", (cid,)).fetchone()
            if meter is None:
                continue
            if row['daily_enabled'] not in ('0', '1'):
                raise ValueError('Invalid daily collection flag in meter seed')
            metadata = json.loads(meter['metadata'] or '{}')
            if not metadata.get('arisu_customer_name'):
                metadata['arisu_customer_name'] = re.sub(r'\s+', ' ', row['arisu_customer_name']).strip()
            conn.execute('UPDATE meters SET metadata=?,daily_enabled=? WHERE id=?',
                         (json.dumps(metadata, ensure_ascii=False), int(row['daily_enabled']), meter['id']))
        conn.execute("INSERT INTO settings(key,value) VALUES('catalog_collection_v1',?)", (db.now(),))
    # Map positions read off the official map image fill stations that still have none; admin edits are kept.
    for name, pos in _json(config.SEED_DIR / "station_positions.json", {}).items():
        conn.execute("UPDATE stations SET map_x=COALESCE(map_x,?),map_y=COALESCE(map_y,?) WHERE id=? AND map_x IS NULL",
                     (pos["x"], pos["y"], name))
    # The first position seed gave seven pairs of stations one shared point, so one hid the other on the map.
    # Move a station only while it still holds that shared point; a position an administrator set is kept.
    for name, old, new in POSITION_FIXES:
        conn.execute("UPDATE stations SET map_x=?,map_y=? WHERE id=? AND map_x=? AND map_y=?", (*new, name, *old))


def list_meters(conn, office_ids=None, include_deleted=False):
    """Archived contracts are only visible through an explicit, still-scoped lookup."""
    sql = """SELECT m.*,s.name AS station_name,s.map_x,s.map_y,o.name AS office_name
             FROM meters m JOIN stations s ON s.id=m.station_id JOIN offices o ON o.id=m.office_id"""
    params = []
    conditions = [] if include_deleted else ["m.deleted_at IS NULL"]
    if office_ids is not None:
        if not office_ids:
            return []
        conditions.append("m.office_id IN (" + ",".join("?" for _ in office_ids) + ")")
        params = list(office_ids)
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    rows = []
    for row in conn.execute(sql + " ORDER BY s.name,m.line,m.customer_number", params):
        item = dict(row)
        item["metadata"] = json.loads(item.get("metadata") or "{}")
        rows.append(item)
    return rows


def collector_meters():
    db.init_db()
    with db.connect() as conn:
        bootstrap(conn)
        return list_meters(conn)


def _stat(path):
    """(mtime, size), or None when absent, including a file removed between two checks."""
    try:
        info = Path(path).stat()
        return info.st_mtime_ns, info.st_size
    except OSError:
        return None


def _signature(paths):
    return tuple((str(p), *state) for p in paths if (state := _stat(p)))


@lru_cache(maxsize=2)
def _fallback_data(signature):
    daily, risk = {}, {}
    names = {}
    paths = [Path(p) for p, *_ in signature]

    def add(cid, date, field, value, reading=False):
        cid, value = customer_id(cid), number(value)
        if not cid or value is None or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date or "")):
            return
        if reading:   # 과거 자료의 검침일 D 값은 D-1에 쓴 물이다. 수집 자료·승하차와 같은 사용일로 둔다.
            try:
                date = (Date.fromisoformat(date) - timedelta(days=1)).isoformat()
            except ValueError:   # 형식은 맞지만 달력에 없는 날(2026-02-30 등)은 건너뛴다
                return
        daily.setdefault(cid, {"usage": {}, "ridership": {}})[field][date] = value

    master = next((p for p in paths if p.name == "master.csv"), None)
    master_customers = set()
    if master:
        for row in read_csv(master):
            cid = customer_id(row["고객번호"])
            master_customers.add(cid)
            names[row["역명"]] = cid
    # Processed sources cover water dates and meters missing from the ML join.
    for p in paths:
        if p.name == "daily_water_usage.csv":
            for r in read_csv(p):
                cid = customer_id(r["고객번호"])
                names[r["역명"]] = cid
                add(cid, r.get("검침일"), "usage", r.get("일사용량_톤"), reading=True)
        elif p.name == "ridership.csv" and p.parent.name == "processed":
            for r in read_csv(p):
                cid = customer_id(r["고객번호"])
                if cid not in master_customers:
                    boarding, leaving = number(r.get("승차총승객수")), number(r.get("하차총승객수"))
                    if boarding is not None and leaving is not None:
                        add(cid, r.get("사용일"), "ridership", boarding + leaving)
    if master:
        for r in read_csv(master):
            add(r["고객번호"], r["날짜"], "usage", r.get("일사용량_톤"), reading=True)   # 이 파일의 사용량만 검침일 기준
            add(r["고객번호"], r["날짜"], "ridership", r.get("총승객수"))
    for p in paths:
        if p.parent.name in ("water", "ridership") and "daily" in p.parts:
            for r in read_csv(p):
                cid = r.get("고객번호") or names.get(r.get("역명"))
                date = r.get("사용일") or r.get("날짜")
                if p.parent.name == "water":
                    add(cid, date, "usage", r.get("일사용량(톤)", r.get("일사용량_톤")))
                else:
                    add(cid, date, "ridership", r.get("총승객수"))
    # Latest file wins overlapping predictions. Prefer current ML output on ties.
    predictions = []
    for p in paths:
        if p.name == "test_anomalies.csv":
            rows = list(read_csv(p))
            predictions.append((max((r.get("날짜", "") for r in rows), default=""), "back" in p.parts, rows))
    for _, _, rows in sorted(predictions, key=lambda v: v[:2]):
        for r in rows:
            values = [number(r.get(k, r.get(old))) for k, old in
                      [("일사용량_톤", "일사용량_톤"), ("predicted_ton", "pred_ton"),
                       ("error_ton", "residual"), ("deviation_score", "z_score")]]
            if any(v is None for v in values):
                continue
            cid = customer_id(r["고객번호"])
            risk.setdefault(cid, {})[r["날짜"]] = dict(zip(("actual", "pred", "residual", "z"), values)) | {
                "date": r["날짜"], "severity": r.get("심각도", "정상"), "dir": r.get("방향") or "",
                "err": str(r.get("likely_data_error", r.get("data_error_candidate", ""))).lower() in ("true", "1")}
    return ({cid: {field: [{"date": d, "value": v} for d, v in sorted(values.items())]
                   for field, values in series.items()} for cid, series in daily.items()},
            {cid: [r for _, r in sorted(values.items())] for cid, values in risk.items()})


@lru_cache(maxsize=2)
def _snapshot_data(signature):
    return tuple(_json(path) for path, *_ in signature)


def _sources():
    """Loader and files of the published snapshot, else of the repository data."""
    snapshot = config.DATA_DIR / "snapshot"
    manifest = _json(snapshot / "manifest.json")
    if manifest.get("published"):
        directory = (snapshot / manifest.get("directory", ".")).resolve()
        if directory.is_relative_to(snapshot.resolve()):
            paths = [directory / "daily.json", directory / "risk.json"]
            if all(p.is_file() for p in paths):
                return _snapshot_data, paths
    # 이력 자료는 저장소 밖(APP_DATA_DIR/history)에 있다. 없으면 화면은 수집·게시된 스냅샷만 보여 준다.
    paths = [config.HISTORY_DIR / p for p in ("processed/daily_water_usage.csv", "processed/ridership.csv", "ml_dataset/master.csv")]
    paths.append(config.SEED_DIR / 'risk/test_anomalies.csv')
    paths += sorted((config.HISTORY_DIR / "daily").glob("*/*.csv"))
    return _fallback_data, paths


def data_sources():
    loader, paths = _sources()
    return loader(_signature(paths))


def _collected_paths():
    return sorted(path for kind in ('water', 'ridership')
                  for path in (config.DATA_DIR / 'daily' / kind).glob('*.csv'))


@lru_cache(maxsize=2)
def _collected_daily(signature, before):
    """Completed source readings remain visible while snapshot/model publication catches up."""
    daily = {}
    for path, *_ in signature:
        water = Path(path).parent.name == 'water'
        field = 'usage' if water else 'ridership'
        for row in read_csv(path):
            cid = customer_id(row.get('고객번호'))
            day = row.get('사용일') or row.get('날짜')
            value = number(row.get('일사용량(톤)', row.get('일사용량_톤')) if water else row.get('총승객수'))
            if not re.fullmatch(r'\d{9}', cid) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(day or '')):
                continue
            try:
                Date.fromisoformat(day)
            except ValueError:
                continue
            if day >= before or value is None or value < 0 or water and row.get('상태') not in (None, '', '정상'):
                continue
            daily.setdefault(cid, {}).setdefault(field, {})[day] = value
    return daily


def _with_collected_daily(daily):
    collected = _collected_daily(_signature(_collected_paths()), str(today()))
    combined = dict(daily)
    for cid, fields in collected.items():
        entry = dict(daily.get(cid, {'usage': [], 'ridership': []}))
        for field, updates in fields.items():
            values = {row['date']: row['value'] for row in entry.get(field, [])} | updates
            entry[field] = [{'date': day, 'value': value} for day, value in sorted(values.items())]
        combined[cid] = entry
    return combined


def data_version(office_ids=None):
    """Fingerprint of everything payload() reads for this scope, without building it.

    A commit rewrites the database file or leaves frames in its WAL, and publication points
    the manifest at new snapshot files. Bookkeeping writes change it too, which only costs
    one more full response. An empty WAL comes and goes with every connection, so it is ignored.
    """
    # ponytail: file stats rely on the filesystem's mtime resolution (ns on ext4/xfs/APFS).
    # Keep a version counter in the database if this ever runs on coarser storage.
    wal = _stat(f"{config.APP_DB_PATH}-wal")
    arisu_paths = sorted((config.DATA_DIR / 'arisu').glob('*.json'))
    arisu_paths += sorted((config.DATA_DIR / 'raw/remote_meter').glob('*/statuses.json'))
    state = (office_ids, str(today()), _stat(config.APP_DB_PATH), wal if wal and wal[1] else None,
             _signature(_sources()[1]), _signature(arisu_paths), _signature(_collected_paths()))
    return hashlib.sha256(repr(state).encode()).hexdigest()[:32]


def meter_data_mode(meter, daily):
    """Only dated, numeric water observations qualify; zero is a valid observation."""
    if not meter['daily_enabled']:
        return 'billing_only'
    usage = daily.get(meter["customer_number"], {}).get("usage", [])
    if meter["provider"] == "arisu" and any(row.get("date") and number(row.get("value")) is not None for row in usage):
        return "daily"
    return "pending" if meter["active"] and meter["daily_enabled"] else "billing_only"


def payload(allowed_office_ids=None):
    with db.connect() as conn:
        meters = list_meters(conn, allowed_office_ids)
        ids = {m["id"] for m in meters}
        entries = {mid: [] for mid in ids}
        # SQL scope keeps forbidden bill content outside the response builder.
        if ids:
            query = "SELECT * FROM bills WHERE meter_id IN (" + ",".join("?" for _ in ids) + ") ORDER BY period,gubun"
            for row in conn.execute(query, list(ids)):
                bill = json.loads(row["payload"])
                bill.update(id=row["id"], gubun=row["gubun"], source=row["source"], notice_number=row['notice_number'])
                entries[row["meter_id"]].append(bill)
        offices = [{"id": r["id"], "name": r["name"]} for r in conn.execute("SELECT * FROM offices ORDER BY name")
                   if allowed_office_ids is None or r["id"] in allowed_office_ids]
    all_daily, all_risk = data_sources()
    all_daily = _with_collected_daily(all_daily)
    # Meters the last model run left out because their training/validation history was too short.
    history_short = {w.get("customer_number") for w in _json(config.DATA_DIR / "snapshot" / "manifest.json").get("withheld", [])}
    bills, stations, daily, risk, locations, arisu = {}, {}, {}, {}, {}, {}
    counts = dict.fromkeys(("daily", "billing_only", "pending"), 0)
    for m in meters:
        mid, cid = m["id"], m["customer_number"]
        m["data_mode"] = meter_data_mode(m, all_daily)
        if m["active"]:
            counts[m["data_mode"]] += 1
        bills[mid] = m["metadata"] | {"역명": m["display_name"], "주소": m["address"], "사업소명": m["office_name"],
                     "용도": m["tariff"], "purpose": m["purpose"], "bills": entries[mid]}
        stations[mid] = {"역명": m["display_name"], "호선": int(m["line"]) if str(m["line"]).isdigit() else None,
                         "영업사업소": m["office_name"], "office_id": m["office_id"], "station_id": m["station_id"],
                         "provider": m["provider"], "customer_number": cid, "active": bool(m["active"]),
                         "daily_enabled": bool(m["daily_enabled"]), "purpose": m["purpose"], "data_mode": m["data_mode"],
                         "history_short": cid in history_short}
        if m["provider"] == "arisu" and m['daily_enabled']:
            if re.fullmatch(r'\d{9}', cid):
                smart = _json(config.DATA_DIR / "arisu" / f"{cid}.json")
                if isinstance(smart, dict) and smart.get('customer_number') == cid:
                    arisu[mid] = smart
                readings = _json(config.DATA_DIR / "raw/remote_meter" / cid / "statuses.json")
                if isinstance(readings, dict) and readings.get('customer_number') == cid and isinstance(readings.get('readings'), list):
                    arisu.setdefault(mid, {'customer_number': cid})['readings'] = readings['readings']
            if cid in all_daily:
                daily[mid] = all_daily[cid]
            if m["data_mode"] == "daily" and cid in all_risk:
                risk[mid] = all_risk[cid]
        if m["map_x"] is not None and m["map_y"] is not None:
            locations[m["station_id"]] = {"x": m["map_x"], "y": m["map_y"], "name": m["station_name"]}
    active_ids = {m['id'] for m in meters if m['active']}
    latest_usage = max((r["date"] for mid, v in daily.items() if mid in active_ids for r in v.get("usage", [])), default=None)
    latest_ridership = max((r["date"] for mid, v in daily.items() if mid in active_ids for r in v.get("ridership", [])), default=None)
    latest_risk = max((r["date"] for mid, rs in risk.items() if mid in active_ids for r in rs), default=None)
    return {"bills": bills, "stations": stations, "daily": daily, "risk": risk, "arisu": arisu, "meters": meters,
            "offices": offices, "locations": locations,
            "status": {"today": str(today()), "reference_date": latest_risk or latest_usage or latest_ridership,
                       "latest_usage": latest_usage, "latest_ridership": latest_ridership, "latest_risk": latest_risk,
                       "counts": counts}}
