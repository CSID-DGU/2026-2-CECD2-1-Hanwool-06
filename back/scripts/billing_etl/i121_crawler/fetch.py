from __future__ import annotations

from datetime import date
from pathlib import Path
import re

import requests
from bs4 import BeautifulSoup

from .auth import BASE, LoginError, _looks_like_login_page, update_csrf, ensure_customer_access
from .parser import parse_bill_list
from back.pipelines.common import customer_number


def shift_month(ym: str, delta: int) -> str:
    """Shift a YYYY-MM string by an integer number of months."""
    y, m = map(int, ym.split("-"))
    idx = y * 12 + (m - 1) + delta
    return f"{idx // 12:04d}-{(idx % 12) + 1:02d}"


def _bill_page(html, mkey):
    """Only cache pages whose records belong to the requested contract."""
    if _looks_like_login_page(html):
        raise LoginError('Arisu session expired during bill collection')
    soup = BeautifulSoup(html, 'html.parser')
    caption = soup.find('caption', string=lambda value: value and '부과내역이며' in value)
    table = caption.find_parent('table') if caption else None
    if table is None or table.find('tbody') is None:
        raise ValueError('Arisu bill response is missing its expected table')
    for bill in parse_bill_list(html, customer=mkey):
        if customer_number(bill['mkey']) != customer_number(mkey):
            raise ValueError('Bill customer does not match requested contract')
        due = date.fromisoformat(bill['napgi'])
        if bill['napgi_compact'] and bill['napgi_compact'] != due.strftime('%Y%m%d'):
            raise ValueError('Arisu bill dates do not agree')
    return soup, table


def fetch_bill_window(
    session: requests.Session,
    mkey: str,
    start_ym: str,
    end_ym: str,
    cache_dir: Path,
    *,
    timeout: float = 30.0,
    force: bool = False,
) -> tuple[str, bool]:
    """Fetch every page of the current levy history for one customer/window.

    Returns (html_text, was_cached). Cached pages are read from disk and
    no request is made.
    """
    ensure_customer_access(session, mkey)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{mkey}_{start_ym}_{end_ym}.html"
    if cache_path.exists() and not force:
        try:
            cached = cache_path.read_text(encoding="utf-8")
            _bill_page(cached, mkey)
            return cached, True
        except (OSError, UnicodeError, ValueError, LoginError):
            pass  # Re-fetch invalid historical caches instead of retrying them forever.

    params = {
        "searchMkey": mkey,
        "searchStartYm": start_ym,
        "searchEndYm": end_ym,
        "historyType": "LEVY",
        "_m": "m6_1",
    }
    headers = {
        "Referer": f"{BASE}/cyber/front/mypage/NR_myArisu.do",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    combined = None
    page, last_page = 1, 1
    while page <= last_page:
        response = session.post(f'{BASE}/cyber/front/mypage/NR_levySearch.do',
            data=params | {'pageIndex': page}, headers=headers, timeout=timeout)
        response.raise_for_status()
        if 'NR_loginForm.do' in response.url:
            raise LoginError('Arisu session expired during bill collection')
        soup, table = _bill_page(response.text, mkey)
        update_csrf(session, response.text)
        pages = [int(v) for v in re.findall(r'jsMovePage\((\d+)\)', response.text)]
        last_page = max([last_page, *pages])
        if last_page > 100:
            raise ValueError('Unexpected Arisu bill pagination')
        if combined is None:
            combined = soup
            destination = table.find('tbody')
        else:
            for row in table.select('tbody > tr'):
                destination.append(row)
        page += 1
    html = str(combined)
    from back.pipelines.common import atomic_text
    atomic_text(cache_path, html)
    return html, False
