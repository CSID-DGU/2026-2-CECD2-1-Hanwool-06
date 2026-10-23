"""Read the source site's smart-meter card without inferring missing statuses."""
from __future__ import annotations

import math
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from back.pipelines.common import customer_number
from back.scripts.billing_etl.i121_crawler.auth import (
    MYARISU_URL, LoginError, _looks_like_login_page, ensure_customer_access, update_csrf,
)


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
