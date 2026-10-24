"""Read the source site's smart-meter card without inferring missing statuses."""
from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from back.pipelines.common import atomic_text, customer_number
from back.scripts.billing_etl.i121_crawler.auth import (
    MYARISU_URL, LoginError, _looks_like_login_page, ensure_customer_access, update_csrf,
)

CARD_FIELDS = ('leak_status', 'meter_status', 'realtime_usage', 'updated_at', 'collected_at', 'notice')
INDEX_READING_DAYS = 92   # the chat index keeps about three months of per-day meter statuses


def write_arisu_index(runtime):
    """One file for the chat (arisu/index.json): every saved card plus recent per-day meter statuses.

    The per-meter files stay the source; a file that cannot be read is left out rather than guessed.
    """
    runtime = Path(runtime)
    meters = {}
    for path in sorted((runtime / 'arisu').glob('*.json')):
        if not re.fullmatch(r'\d{9}', path.stem):
            continue
        try:
            card = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(card, dict) and card.get('customer_number') == path.stem:
            meters[path.stem] = {key: card.get(key) for key in CARD_FIELDS}
    for path in sorted((runtime / 'raw' / 'remote_meter').glob('*/statuses.json')):
        cid = path.parent.name
        try:
            index = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not re.fullmatch(r'\d{9}', cid) or not isinstance(index, dict) or index.get('customer_number') != cid:
            continue
        readings = [r for r in index.get('readings') or [] if isinstance(r, dict) and isinstance(r.get('date'), str)
                    and re.fullmatch(r'\d{4}-\d{2}-\d{2}', r['date'])]
        if readings:
            floor = str(date.fromisoformat(max(r['date'] for r in readings)) - timedelta(days=INDEX_READING_DAYS - 1))
            meters.setdefault(cid, {})['readings'] = [{key: r.get(key) for key in ('date', 'measured_at', 'status')}
                                                      for r in sorted(readings, key=lambda r: r['date']) if r['date'] >= floor]
    # 채팅은 이 파일을 4MB까지만 읽으므로 들여쓰기 없이 쓴다(들여쓰면 계량기 하나에 약 11KB).
    atomic_text(runtime / 'arisu' / 'index.json', json.dumps({
        'generated_at': datetime.now(ZoneInfo('Asia/Seoul')).isoformat(timespec='seconds'), 'meters': meters},
        ensure_ascii=False, separators=(',', ':'), allow_nan=False))


def _text(element):
    return element.get_text(" ", strip=True) if element else ""


def parse_smart_status(html, customer):
    expected = customer_number(customer)
    if _looks_like_login_page(html):
        raise LoginError("Arisu session expired during smart-meter collection")
    soup = BeautifulSoup(html, "html.parser")
    selected = soup.select_one('select[name="searchMkey"] option[selected]')
    if selected is None or customer_number(selected.get("value", "")) != expected:
        raise ValueError("Arisu returned a different selected customer")
    card = soup.select_one(".box.smartRemoteMeter")
    if card is None:
        raise ValueError("Arisu smart-meter card is missing")

    leak = _text(card.select_one(".remoteMeterBox .risk strong")) or None
    if leak in {"-", "—", "–"}:
        leak = None
    risk_box_text = _text(card.select_one(".remoteMeterBox .risk"))
    usage_text = _text(card.select_one(".remoteMeterBox .usage strong"))
    usage_box_text = _text(card.select_one(".remoteMeterBox .usage"))
    usage_match = re.fullmatch(r"([\d,]+(?:\.\d+)?)\s*톤", usage_text)
    usage = float(usage_match[1].replace(",", "")) if usage_match else None
    if usage is not None and not math.isfinite(usage):
        raise ValueError("Invalid Arisu realtime usage")
    timestamp = re.search(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?", _text(card.select_one(".updateTime")))
    updated_at = None
    if timestamp:
        updated = datetime.fromisoformat(timestamp[0])
        updated_at = updated.isoformat(sep=" ", timespec="seconds" if updated.second else "minutes")

    # Site fault/no-data notices can replace the entire risk/usage box.
    for element in card.select(".title, .risk, .usage, .updateTime, button, script, style"):
        element.decompose()
    notice = _text(card) or None
    fault = re.compile(r"장애|고장|(?:통신|수신)\s*오류")
    fault_messages = [notice] if notice and fault.search(notice) else []
    if fault.search(usage_box_text):
        fault_messages.append(usage_text if fault.search(usage_text) else usage_box_text)
    if fault.search(risk_box_text):
        fault_messages.append(leak if leak and fault.search(leak) else risk_box_text)
        if leak and fault.search(leak):
            leak = None
    meter_status = " / ".join(dict.fromkeys(fault_messages)) or None
    return {
        "customer_number": expected, "leak_status": leak,
        "meter_status": meter_status, "realtime_usage": usage, "updated_at": updated_at,
        "collected_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(), "notice": notice,
    }


def fetch_smart_status(session, meter):
    expected = customer_number(meter["customer_number"])
    ensure_customer_access(session, expected)
    response = session.get(MYARISU_URL, params={"searchMkey": expected, "_m": "m6"}, timeout=30)
    if response.status_code in (401, 403) or "NR_loginForm.do" in response.url:
        raise LoginError("Arisu session expired during smart-meter collection")
    response.raise_for_status()
    result = parse_smart_status(response.text, expected)
    update_csrf(session, response.text)
    return result
