"""Use the same private snapshot and registry as the dashboard, scoped before analysis."""
from datetime import date as Date, timedelta
from functools import lru_cache
from statistics import median
from back.pipelines.common import today
from . import catalog, config, db

SEV_RANK = {'정상': 0, '주의': 1, '경고': 2}


def reference_date(office_ids=None):
    if config.TODAY_OVERRIDE:
        return config.TODAY_OVERRIDE
    daily, risk = catalog.data_sources()
    with db.connect() as c:
        ids = {m['customer_number'] for m in catalog.list_meters(c, office_ids)
               if m['active'] and catalog.meter_data_mode(m, daily) == 'daily'}
    dates = [r['date'] for cid in ids for r in risk.get(cid, [])]
    if not dates:
        dates = [r['date'] for cid in ids for r in daily.get(cid, {}).get('usage', [])]
    return max(dates, default=today().isoformat())


@lru_cache(maxsize=None)
def _holidays(year):
    import holidays   # 모델 달력(refresh.build_calendar)과 같은 출처. 저장소에 달력 파일을 두지 않는다
    return holidays.KR(years=[year], language='ko')


def calendar_info(date):
    day = Date.fromisoformat(date)
    name = _holidays(day.year).get(day)
    return {'weekend': day.weekday() >= 5, 'holiday': name is not None, 'holiday_name': name or '',
            '요일': '월화수목금토일'[day.weekday()]}


def item_from_row(meter, row):
    pred, actual = catalog.number(row.get('pred')), catalog.number(row.get('actual'))
    error, z = catalog.number(row.get('residual')), catalog.number(row.get('z'))
    data_error = row.get('err') in (True, 'true', 'True', 1)
    return {'meter_id': meter['id'], '고객번호': meter['customer_number'], '역명': meter['display_name'],
            '날짜': row['date'], '심각도': '자료 확인' if data_error else row.get('severity') or '미분석', '방향': row.get('dir') or '',
            'predicted_ton': pred, 'actual_ton': actual, 'error_ton': error, 'deviation_score': z,
            'pct': round(error / pred * 100) if pred and error is not None else None,
            'likely_data_error': data_error,
            # 자료 확인의 이유(예: 원격검침 값이 그대로인 날). 모델 판정 행에는 없다.
            'reason': row.get('reason') or '', 'since': row.get('since'), 'days': row.get('days'), 'usual': row.get('usual'),
            '영업사업소': meter['office_name'], '용도': meter['purpose']}


def anomalies_on(date=None, include_normal=False, office_ids=None):
    date = date or reference_date(office_ids)
    with db.connect() as c:
        meters = catalog.list_meters(c, office_ids)
    daily, risk = catalog.data_sources()
    items = [item_from_row(m, r) for m in meters if m['active'] and catalog.meter_data_mode(m, daily) == 'daily'
             for r in risk.get(m['customer_number'], []) if r['date'] == date]
    items = [item for item in items if include_normal or item['심각도'] in ('주의', '경고', '자료 확인')]
    return sorted(items, key=lambda x: (x['likely_data_error'], -SEV_RANK.get(x['심각도'], 0), -abs(x['deviation_score'] or 0)))


def find_one(meter_id, date=None, office_ids=None):
    return next((i for i in anomalies_on(date, True, office_ids) if i['meter_id'] == meter_id), None)


def _arisu(cid):
    """Arisu card and per-day meter statuses for cause analysis. Missing or unreadable files give nothing."""
    try:
        card = catalog._json(config.DATA_DIR / 'arisu' / f'{cid}.json')
        index = catalog._json(config.DATA_DIR / 'raw' / 'remote_meter' / cid / 'statuses.json')
    except (OSError, ValueError):
        return {}, {}
    card = card if isinstance(card, dict) and card.get('customer_number') == cid else {}
    readings = index.get('readings') if isinstance(index, dict) and index.get('customer_number') == cid else None
    statuses = {r['date']: r.get('status') for r in readings if isinstance(r, dict) and isinstance(r.get('date'), str)} \
        if isinstance(readings, list) else {}
    return card, statuses


def analysis_context(meter_id, date, office_ids=None, days=28):
    """Recent days and usual levels for cause analysis; the levels are computed here, not by the model."""
    with db.connect() as c:
        meter = next((m for m in catalog.list_meters(c, office_ids) if m['id'] == meter_id), None)
    if not meter or not meter['active']:
        return None
    daily, risk = catalog.data_sources()
    if catalog.meter_data_mode(meter, daily) != 'daily':
        return None
    entry = daily.get(meter['customer_number'], {})
    scored = {r['date']: r for r in risk.get(meter['customer_number'], [])}
    errors = {day for day, r in scored.items() if r.get('err') in (True, 'true', 'True', 1)}
    # A scored day shows the value the screen and facts use; an invalid reading is no usage level at all.
    usage = {r['date']: r['value'] for r in entry.get('usage', [])} | {
        day: catalog.number(r.get('actual')) for day, r in scored.items() if catalog.number(r.get('actual')) is not None}
    usage = {day: value for day, value in usage.items() if day not in errors}
    riders = {r['date']: round(r['value']) for r in entry.get('ridership', []) if r['value'] is not None}   # people, not 72326.0
    card, statuses = _arisu(meter['customer_number'])
    end = Date.fromisoformat(date)
    history = []
    for day in (end - timedelta(days=n) for n in range(days - 1, -1, -1)):
        row, pred = scored.get(day.isoformat(), {}), catalog.number(scored.get(day.isoformat(), {}).get('pred'))
        history.append({'날짜': day.isoformat(), '요일': '월화수목금토일'[day.weekday()], '사용량_톤': usage.get(day.isoformat()),
                        '예측_톤': None if pred is None else round(pred, 1),
                        '심각도': '자료 확인' if day.isoformat() in errors else row.get('severity'),
                        '승하차_명': riders.get(day.isoformat()),
                        # 아리수 원문 검침 상태(정상·통신 장애·계량기 장애 등). None은 그날 검침 기록이 없다는 뜻이다.
                        '아리수_검침상태': statuses.get(day.isoformat())})

    def usual(values, digits=1):
        values = [v for v in values if v is not None]
        return round(median(values), digits) if values else None
    weeks = [(end - timedelta(weeks=n)).isoformat() for n in range(1, 9)]
    arisu = {'누수위험': card.get('leak_status'), '계량기_상태': card.get('meter_status'),
             '갱신_시각': card.get('updated_at')} if card else None
    return {'역': meter['station_name'], '호선': meter['line'], 'history': history, 'arisu': arisu, 'baseline': {
        '같은_요일_지난_8주_사용량_중앙값': usual(usage.get(d) for d in weeks),
        '지난_28일_사용량_중앙값': usual(r['사용량_톤'] for r in history[:-1]),
        '같은_요일_지난_8주_승하차_중앙값': usual((riders.get(d) for d in weeks), None),
        '당일_승하차_명': riders.get(date)}}
