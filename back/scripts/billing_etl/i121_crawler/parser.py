from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag


_MKEY_RE = re.compile(r"^\d{6,12}$")
_STATION_RE = re.compile(r"([가-힣A-Za-z0-9]+역)")


def discover_mkeys(html: str) -> list[str]:
    """Extract customer numbers from the <select name="mkey"> dropdown."""
    soup = BeautifulSoup(html, "html.parser")
    select = soup.select_one('select[name="searchMkey"], select#selfTestcusNum, select[name="mkey"], select#mkey')
    mkeys: list[str] = []
    if isinstance(select, Tag):
        for opt in select.find_all("option"):
            value = (opt.get("value") or "").strip()
            if _MKEY_RE.match(value):
                mkeys.append(value)
    seen: set[str] = set()
    out: list[str] = []
    for v in mkeys:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


def parse_bill_list(html: str, *, customer: str | None = None) -> list[dict[str, Any]]:
    """Parse 부과내역 table from NR_myArisu.do response.

    Returns one dict per bill row. Empty list when no bills (the page
    shows '조회된 결과가 없습니다' inside a colspan row).
    """
    soup = BeautifulSoup(html, "html.parser")
    selected = {field.get('value', '').strip() for form in soup.find_all('form')
                if urlsplit(form.get('action') or '').path == '/cyber/front/mypage/NR_levySearch.do'
                for field in form.select('input[name="searchMkey"]') if field.get('value', '').strip()}
    if len(selected) > 1:
        raise ValueError('Ambiguous bill customer selection')
    selected_customer = next(iter(selected), '')
    if selected_customer and (not _MKEY_RE.fullmatch(selected_customer)
                              or customer and selected_customer.zfill(9) != str(customer).zfill(9)):
        raise ValueError('Bill customer selection does not match requested contract')
    table = _find_table_by_caption(soup, "부과내역이며")
    if table is None:
        return []
    tbody = table.find("tbody")
    if not isinstance(tbody, Tag):
        return []

    rows: list[dict[str, Any]] = []
    for tr in tbody.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 8:
            # 'no result' row uses a single colspan'd td
            continue
        sunbeon = _clean(cells[0].get_text())
        if not sunbeon.isdigit():
            continue

        mkey = _clean(cells[1].get_text())
        gubun = _clean(cells[2].get_text())
        notice_number = ''
        if gubun == '수시분' and selected_customer:
            if not _MKEY_RE.fullmatch(mkey):
                raise ValueError('Invalid supplementary bill notice number')
            notice_number, mkey = mkey, selected_customer
        napgi = _clean(cells[3].get_text())
        sunap_status = _clean(cells[4].get_text())
        bugwa_amount = _to_int(cells[5].get_text())
        address = _clean(cells[6].get_text())
        total_usage = _to_int(cells[7].get_text())
        napgi_compact = _extract_napgi_param(cells[-1])

        rows.append(
            {
                "sunbeon": int(sunbeon),
                "mkey": mkey,
                "notice_number": notice_number,
                "gubun": gubun,
                "napgi": napgi,
                "napgi_compact": napgi_compact,
                "sunap_status": sunap_status,
                "bugwa_amount_won": bugwa_amount,
                "address": address,
                "station_from_address": _extract_station(address),
                "total_usage_ton": total_usage,
            }
        )
    return rows


def _find_table_by_caption(soup: BeautifulSoup, caption_substring: str) -> Tag | None:
    for cap in soup.find_all("caption"):
        if caption_substring in cap.get_text():
            parent = cap.find_parent("table")
            if isinstance(parent, Tag):
                return parent
    return None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _to_int(text: str) -> int | None:
    s = _clean(text).replace(",", "")
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _extract_station(address: str) -> str | None:
    if not address:
        return None
    matches = _STATION_RE.findall(address)
    if not matches:
        return None
    return matches[-1]


def _extract_napgi_param(cell: Tag) -> str | None:
    button = cell.select_one('[data-napgi]')
    if button and re.fullmatch(r'\d{8}', button.get('data-napgi', '')):
        return button['data-napgi']
    anchor = cell.find("a")
    if not isinstance(anchor, Tag):
        return None
    onclick = anchor.get("onclick") or ""
    m = re.search(r"jungInfoDetail\('(\d{8})'\)", onclick)
    return m.group(1) if m else None
