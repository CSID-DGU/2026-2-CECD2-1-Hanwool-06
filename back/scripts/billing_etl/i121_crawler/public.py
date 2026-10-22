"""Official customer-number/name billing lookup; no account registration required."""
from __future__ import annotations

from datetime import date
import hashlib
import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

from back.pipelines.common import atomic_text, customer_number, today
from .fetch import shift_month
from .auth import BASE, update_csrf
from .parser import _clean, _extract_station

LIST_URL = BASE + '/cyber/front/cgcalc/NR_cgTotalInfo.do'


class PublicCustomerNameError(ValueError):
    pass


def _amount(value):
    text = _clean(value).replace(',', '').replace('원', '')
    if text in ('', '-', '—'):
        return None
    if not re.fullmatch(r'-?\d+(?:\.\d+)?', text):
        raise ValueError('Invalid amount in Arisu bill detail')
    return float(text) if '.' in text else int(text)


def _date(value):
    digits = re.sub(r'\D', '', value)
    if len(digits) != 8:
        raise ValueError('Missing Arisu bill due date')
    return date(int(digits[:4]), int(digits[4:6]), int(digits[6:])).isoformat()


def parse_public_list(html, customer, name, start, end):
    soup = BeautifulSoup(html, 'html.parser')
    form = soup.select_one('form#searchForm')
    fields = {n.get('name'): n.get('value', '').strip() for n in form.select('input[name]')} if form else {}
    if fields.get('searchMkey') != customer or fields.get('searchCsNm') != name:
        raise PublicCustomerNameError('Arisu did not confirm the requested customer and name')
    if fields.get('searchStartYm') != start or fields.get('searchEndYm') != end:
        raise ValueError('Arisu returned a different billing period')
    message = re.search(r'\bvar\s+noResultMessage\s*=\s*("(?:[^"\\]|\\.)*")', html)
    if message and any(word in json.loads(message[1]) for word in ('고객번호', '사용자명')):
        raise PublicCustomerNameError('Arisu customer number and registered name do not match')
    caption = next((c for c in soup.find_all('caption') if '조회결과표' in c.get_text()), None)
    table = caption.find_parent('table') if caption else None
    if table is None:
        raise ValueError('Arisu public bill result table is missing')
    rows = []
    for tr in table.select('tbody > tr'):
        cells = tr.find_all('td', recursive=False)
        button = tr.select_one('.btn-detail-view')
        if not button:
            if len(cells) > 1:
                raise ValueError('Arisu bill row has no detail identity')
            continue
        kind = button.get('data-goji-gubun')
        row_customer, row_name = button.get('data-search-mkey'), button.get('data-search-cs-nm')
        if (row_customer and row_customer != customer or row_name and row_name != name
                or kind == '1' and (row_customer != customer or row_name != name)):
            raise PublicCustomerNameError('Arisu bill belongs to another customer')
        if kind == '3':
            continue  # Arrears are a payment summary of existing bills, not a new bill.
        if kind not in ('1', '2') or len(cells) != 6:
            raise ValueError('Unsupported Arisu public bill row')
        identifier = button.get('data-cstmidno', '')
        if not re.fullmatch(r'\d{6,12}', identifier) or kind == '1' and customer_number(identifier) != customer:
            raise ValueError('Arisu bill detail identity does not match')
        due = _date(button.get('data-napgi', ''))
        if _clean(cells[2].get_text()) != due[:7]:
            raise ValueError('Arisu bill due dates do not agree')
        if not start <= due[:7] <= end:
            continue  # The public page also includes current unpaid bills outside the search range.
        rows.append({'mkey': customer, 'notice_number': identifier if kind == '2' else '',
                     'sunbeon': int(_clean(cells[0].get_text())), 'gubun': '정기분' if kind == '1' else '수시분',
                     'napgi': due, 'napgi_compact': due.replace('-', ''),
                     'sunap_status': _clean(cells[3].get_text()), 'bugwa_amount_won': _amount(cells[4].get_text()),
                     'total_usage_ton': None, 'address': '', 'station_from_address': None})
    return rows


def parse_public_detail(html, bill):
    soup = BeautifulSoup(html, 'html.parser')
    if soup.select_one('#serverBlockMessage'):
        raise ValueError('Arisu rejected bill detail lookup')
    expected = bill['notice_number'] or bill['mkey']
    identified = any((n.get('data-cstmidno') or n.get('data-mkey')) == expected
                     and n.get('data-napgi') == bill['napgi_compact'] for n in soup.select('[data-napgi]'))
    labels = {}
    for li in soup.find_all('li'):
        cells = li.find_all(['p', 'span', 'strong'], recursive=False)
        if len(cells) == 2:
            labels[_clean(cells[0].get_text())] = _clean(cells[1].get_text())
    if bill['notice_number']:
        due_parts = re.findall(r'\d+', labels.get('납부기한', ''))
        identified = (labels.get('고지번호') == expected and len(due_parts) == 3
                      and str(date(*map(int, due_parts))) == bill['napgi'])
    if not identified:
        raise ValueError('Arisu bill detail identity or due date does not match')
    details = {'납기일': bill['napgi']}
    if bill['notice_number'] and labels.get('납부금액'):
        details['납부금액'] = _amount(labels['납부금액'])
    numeric = {'공동사용량', '총사용량', '전납기사용량', '전년동기사용량', '가구수'}
    text_fields = {'전자수용가번호', '전자납부번호', '업종', '계량기번호', '구경', '납부방법'}
    for li in soup.find_all('li'):
        cells = li.find_all(['p', 'span'], recursive=False)
        if len(cells) != 2:
            continue
        label, value = (_clean(c.get_text(' ', strip=True)) for c in cells)
        if label.startswith('고객번호') and bill['gubun'] == '정기분' and customer_number(value) != bill['mkey']:
            raise ValueError('Arisu detail shows a different customer')
        if label in numeric:
            details[label] = _amount(value)
        elif label in text_fields:
            details[label] = None if value == '-' else value
        elif label in ('성명 :', '주소 :'):
            details['고지서성명' if label.startswith('성명') else '주소'] = value
    for table in soup.find_all('table'):
        caption = table.find('caption')
        caption = caption.get_text() if caption else ''
        rows = [[_clean(c.get_text(' ', strip=True)) for c in tr.find_all('td', recursive=False)]
                for tr in table.find_all('tr')]
        rows = [r for r in rows if r]
        if '총 사용금액' in caption and rows:
            if len(rows[0]) != 3:
                raise ValueError('Incomplete Arisu bill payment totals')
            details.update(zip(('총사용금액', '차감금액', '납부금액'), map(_amount, rows[0])))
        elif '상수도요금' in caption:
            for row in rows:
                if len(row) != 4:
                    continue
                if row[0] == '기본요금':
                    details['상수도_기본료'] = _amount(row[1])
                elif row[0] == '사용요금':
                    details.update(zip(('상수도_사용료', '하수도_사용료', '물이용부담금'), map(_amount, row[1:])))
                elif row[0] == '계':
                    details['상수도_합계'] = _amount(row[1])
        elif '정기검침일' in caption:
            for th in table.find_all('th'):
                day = re.search(r'정기검침일\s*(\d{1,2})', th.get_text(' ', strip=True))
                if day:
                    details['정기검침일'] = day[1]
            for row in rows:
                if row[0] == '계량기 검침':
                    keys = ('당월지침', '지하수당월지침')
                elif re.fullmatch(r'\d+월지침', row[0]):
                    keys = ('전월지침', '지하수전월지침')
                elif row[0] == '사용량':
                    keys = ('사용량', '지하수사용량')
                else:
                    continue
                details.update(zip(keys, map(_amount, row[-2:])))
        elif '납부 상세 내역' in caption:
            for row in rows:
                if len(row) == 3:
                    if row[0] in ('계량기대금', '설치비'):
                        details[row[0]] = _amount(row[1])
                    elif row[0] == '합계':
                        details['총사용금액'] = _amount(row[1])
                        details['연체금'] = _amount(row[2])
        elif '미납(체납)' in caption:
            for row in rows:
                if len(row) == 2 and row[0] in ('체납금액', '미납금액'):
                    details['체납액' if row[0] == '체납금액' else '미납액'] = _amount(row[1])
    for p in soup.select('p.textInfo'):
        period = re.search(r'사용기간\s+(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일\s*~\s*(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일', p.get_text())
        if period:
            values = list(map(int, period.groups()))
            details['periodStart'], details['periodEnd'] = str(date(*values[:3])), str(date(*values[3:]))
    if details.get('납부금액') is None:
        raise ValueError('Arisu detail is missing payment amount')
    return details


def fetch_public_window(session, customer, name, start, end, cache_dir, *, force=False, timeout=30, known_details=None):
    customer, name = customer_number(customer), str(name).strip()
    if not name:
        raise PublicCustomerNameError('Registered customer name is required')
    cache = Path(cache_dir) / f'public_{customer}_{hashlib.sha256(name.encode()).hexdigest()[:16]}_{start}_{end}.json'
    identity = {'customer': customer, 'name': name, 'start': start, 'end': end}
    known_details = set(known_details or ())
    recent = shift_month(today().strftime('%Y-%m'), -2)
    def known(row):
        return row['napgi'][:7] < recent and (customer, row['napgi'][:7], row['gubun'], row.get('notice_number', '')) in known_details
    if cache.exists() and not force:
        try:
            stored = json.loads(cache.read_text())
            if all(stored.get(k) == v for k, v in identity.items()):
                rows = stored['rows']
                if isinstance(rows, list) and all(r['mkey'] == customer and start <= date.fromisoformat(r['napgi']).strftime('%Y-%m') <= end and (isinstance(r.get('details'), dict) or r.get('_detail_skipped') and known(r)) for r in rows):
                    return rows, True, []
        except (OSError, ValueError, KeyError, TypeError):
            pass
    response = session.get(LIST_URL, timeout=timeout)
    response.raise_for_status()
    csrf = update_csrf(session, response.text)
    response = session.post(LIST_URL, data={'searchMkey': customer, 'searchCsNm': name,
        'searchStartYm': start, 'searchEndYm': end, **csrf}, timeout=timeout)
    response.raise_for_status()
    rows = parse_public_list(response.text, customer, name, start, end)
    update_csrf(session, response.text)
    errors = []
    for bill in rows:
        if known(bill):
            bill['_detail_skipped'] = True
            continue
        route = 'PR_cgSusiInfoPrint.do' if bill['notice_number'] else 'PR_cgJungInfoPrint.do'
        params = {'searchCstmidno' if bill['notice_number'] else 'searchMkey': bill['notice_number'] or customer,
                  'searchNapgi': bill['napgi_compact']}
        try:
            detail = session.post(BASE + '/cyber/front/cgcalc/' + route, data=params,
                headers={'X-Requested-With': 'XMLHttpRequest'}, timeout=timeout)
            detail.raise_for_status()
            bill['details'] = parse_public_detail(detail.text, bill)
            bill['detail_source'] = 'i121_public_detail'
            bill['total_usage_ton'] = bill['details'].get('총사용량')
            bill['address'] = bill['details'].get('주소', '')
            bill['station_from_address'] = _extract_station(bill['address'])
        except Exception as exc:
            errors.append({'customer_number': customer, 'start': start, 'end': end,
                           'napgi': bill['napgi'], 'error': type(exc).__name__})
    if not errors:
        atomic_text(cache, json.dumps(identity | {'rows': rows}, ensure_ascii=False, allow_nan=False))
    return rows, False, errors
