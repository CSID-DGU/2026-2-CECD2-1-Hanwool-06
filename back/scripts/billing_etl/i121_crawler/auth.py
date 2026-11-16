from __future__ import annotations

import os

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from back.api import config  # noqa: F401  # the one place a development .env is read; never in production
from .parser import discover_mkeys


BASE = "https://i121.seoul.go.kr"
LOGIN_FORM_URL = f"{BASE}/cyber/front/login/NR_loginForm.do"
LOGIN_ACTION_URL = f"{BASE}/cyber/front/login/AR_loginAction.do"
MYARISU_URL = f"{BASE}/cyber/front/mypage/NR_myArisu.do"
MKEY_URL = f"{BASE}/cyber/front/mkey"
# 고객번호등록의 '사용자와의 관계' 선택지: 1 본인, 2 배우자, 3 부모, 4 자녀, 5 친인척, 6 사위, 7 며느리, 8 세입자, 9 관리인, 10 기타, 11 부동산
RELATIONS = frozenset(map(str, range(1, 12)))
REGISTERED, RELEASED = '정상 처리되었습니다.', '정상 삭제되었습니다.'
# 등록 조회가 알려 주는 지금의 검침 방법(gcMthCd). 03이 원격검침이고, 일일 검침값은 원격검침 계량기에만 있다.
# 2026-11 계정의 79개로 확인: 일일 자료가 나오는 76개는 모두 03, 01인 3개는 모두 일일 자료가 없었다.
REMOTE_READ = '03'


class LoginError(RuntimeError):
    pass


class ConfigurationError(LoginError):
    pass


class CustomerAccessError(RuntimeError):
    pass


class RegistrationRefused(RuntimeError):
    """Arisu's own reason for not adding a customer to the account. The text is the site's notice, safe to show."""


class NotRemoteMeter(RegistrationRefused):
    """The customer's meter is not read remotely, so it has no daily readings, whoever holds the number."""


def ensure_customer_access(session, customer):
    allowed = getattr(session, 'arisu_customer_numbers', None)
    if isinstance(allowed, frozenset) and customer not in allowed:
        raise CustomerAccessError('Customer is not registered in this Arisu account')


# Network trouble that can clear by itself. Unlike a rejected login, retrying it cannot lock the account.
TRANSIENT_ERRORS = (requests.Timeout, requests.ConnectionError, requests.exceptions.RetryError,
                    requests.exceptions.ChunkedEncodingError, TimeoutError, ConnectionError)
TRANSIENT_NAMES = frozenset({'Timeout', 'ConnectTimeout', 'ReadTimeout', 'ConnectionError', 'SSLError', 'ProxyError',
                             'RetryError', 'ChunkedEncodingError', 'TimeoutError'})


def transient(error):
    """An exception, or the class name a collection report recorded for one."""
    if isinstance(error, str):
        return error in TRANSIENT_NAMES
    if isinstance(error, requests.HTTPError):
        code = error.response.status_code if error.response is not None else 0
        return code == 429 or code >= 500
    return isinstance(error, TRANSIENT_ERRORS)


def _new_session() -> requests.Session:
    s = requests.Session()
    # Every 5xx ends as RetryError, which collection reports record and retry later the same morning.
    retries = Retry(total=3, backoff_factor=1, status_forcelist=(429, *range(500, 600)),
                    allowed_methods={"GET", "POST"})
    s.mount("https://", HTTPAdapter(max_retries=retries))
    s.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
        }
    )
    return s


def update_csrf(session, html):
    """Use the site's issued CSRF token; refresh after login rotates the session."""
    soup = BeautifulSoup(html, 'html.parser')
    fields = {name: soup.find('meta', attrs={'name': name}) for name in ('_csrf', '_csrf_header', '_csrf_parameter')}
    values = {name: element.get('content', '') if element else '' for name, element in fields.items()}
    if values['_csrf'] and values['_csrf_header']:
        session.headers[values['_csrf_header']] = values['_csrf']
    return {values['_csrf_parameter']: values['_csrf']} if values['_csrf_parameter'] and values['_csrf'] else {}


def login(user_id: str, user_pwd: str, *, timeout: float = 30.0) -> requests.Session:
    """Perform form login and return an authenticated session.

    Raises LoginError if the session cannot reach the protected mypage.
    """
    if not user_id or not user_pwd:
        raise LoginError("user_id and user_pwd must be non-empty")

    session = _new_session()

    try:
        r = session.get(LOGIN_FORM_URL, params={"_m": "m7"}, timeout=timeout)
        r.raise_for_status()
        csrf = update_csrf(session, r.text)
        if not csrf:
            raise LoginError('Arisu login form does not contain its required CSRF token')
        r = session.post(
            LOGIN_ACTION_URL,
            data={"mbrId": user_id, "pwd": user_pwd, "mbrTypeCd": "01", **csrf},
            headers={"X-Requested-With": "XMLHttpRequest", "Referer": LOGIN_FORM_URL,
                     "Origin": BASE, "Accept": "*/*"},
            timeout=timeout,
        )
        r.raise_for_status()
        if r.text.strip().split('|')[0] != '200':
            raise LoginError('Arisu requires account verification or rejected the supplied login')
        verify = session.get(MYARISU_URL, params={"_m": "m6"}, timeout=timeout)
        verify.raise_for_status()
        if _looks_like_login_page(verify.text) or "NR_loginForm.do" in verify.url:
            raise LoginError('Arisu session verification failed')
        update_csrf(session, verify.text)
        if BeautifulSoup(verify.text, 'html.parser').select_one('select[name="searchMkey"], select#selfTestcusNum'):
            session.arisu_customer_numbers = frozenset(discover_mkeys(verify.text))
        return session
    except Exception:
        session.close()
        raise


def _looks_like_login_page(html: str) -> bool:
    soup = BeautifulSoup(html, 'html.parser')
    return bool(soup.select_one('input[name="userId"], input[name="userPwd"], input[name="mbrId"], input[name="pwd"]'))


def session_from_env() -> requests.Session:
    """ARISU credentials from the process environment; I121 names remain accepted for older installations."""
    user_id = (os.environ.get("ARISU_USER_ID") or os.environ.get("I121_USER_ID", "")).strip()
    user_pwd = os.environ.get("ARISU_USER_PWD") or os.environ.get("I121_USER_PWD", "")
    if not user_id or not user_pwd:
        raise ConfigurationError(
            "missing credentials; copy .env.example to .env and set "
            "ARISU_USER_ID / ARISU_USER_PWD"
        )
    return login(user_id, user_pwd)


def collection_session():
    """A member-login failure must not prevent public customer/name bill lookup."""
    try:
        return session_from_env()
    except (LoginError, requests.RequestException) as exc:
        session = _new_session()
        session.arisu_customer_numbers = frozenset()
        session.arisu_login_error = type(exc).__name__
        return session


def _expired(response):
    return (response.status_code in (401, 403) or 'NR_loginForm.do' in response.url
            or _looks_like_login_page(response.text))


def _mkey(session, method, endpoint, ajax, timeout, **fields):
    """One request of the 고객번호관리 page. ajax: whether the page's own script marks the request as one."""
    headers = {'Referer': f'{MKEY_URL}/NR_mkeyList.do?_m=m6_2'} | ({'X-Requested-With': 'XMLHttpRequest'} if ajax else {})
    if method == 'GET':
        response = session.get(f'{MKEY_URL}/{endpoint}', params=fields, headers=headers, timeout=timeout)
    else:
        response = session.post(f'{MKEY_URL}/{endpoint}', data=fields, headers=headers | {'Origin': BASE}, timeout=timeout)
    if _expired(response):
        raise LoginError('Arisu session expired during customer registration')
    response.raise_for_status()
    return response


def _answer(response):
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError('Unexpected Arisu registration response')
    return payload


def _refuse(payload, fallback=''):
    """With the site's notice this is a refusal. Without one the answer is not understood, and may be asked again."""
    message = ' '.join(str(payload.get('message') or fallback).split())[:120]
    if not message:
        raise ValueError('Unexpected Arisu registration response')
    raise RegistrationRefused(message)


def _lookup(session, customer, name, timeout):
    """Arisu's own record of one customer, as the registration form looks it up. Read-only, for any customer."""
    found = _answer(_mkey(session, 'POST', 'JR_mkeyInsertLookup.do', False, timeout, mkey=customer, searchMkey=customer, searchCsNm=name))
    if found.get('success') is not True or not isinstance(found.get('dataMap'), dict):
        _refuse(found)
    if str(found['dataMap'].get('mkey') or customer).strip().zfill(9) != customer:
        raise ValueError('Arisu looked up a different customer')
    return found['dataMap']


def remote_read(session, customer, name, *, timeout=30.0):
    """Whether Arisu reads this customer's meter remotely, the only kind with daily readings. None when it does not say.
    Read-only, and answers for customers inside or outside the account."""
    method = _lookup(session, customer, name, timeout).get('gcMthCd')
    return method == REMOTE_READ if method else None


def register_customer(session, customer, name, relation, *, release=False, timeout=30.0):
    """Make sure the logged-in account holds a customer number, as 마이페이지 > 고객번호관리 > 고객번호등록 does: the
    form's four checks, then its one insert. Returns 'listed' when the account already had it, 'registered', or
    'released' when another member's link was removed first. The site removes that link after its
    '연결정보를 해제하시겠습니까?' question; here it happens only with release=True, and is refused otherwise.
    A meter that is not read remotely is refused before any of that: there would be no daily readings to gain."""
    if relation not in RELATIONS or not name:
        raise ValueError('Arisu registration needs a bill name and a relation code')

    def done(response, expected):
        """The action pages answer with one line of text. Anything but the expected line is the site's refusal."""
        reply = response.text.strip()
        if reply != expected:
            _refuse({'message': '' if '<html' in reply.lower() or reply.lower().startswith('<!doctype') else reply})

    def account():
        """The list every later query is checked against. An empty one is a failed lookup, never an empty account."""
        listing = session.get(MYARISU_URL, params={'_m': 'm6'}, timeout=timeout)
        if _expired(listing):
            raise LoginError('Arisu session expired during customer registration')
        listing.raise_for_status()
        update_csrf(session, listing.text)
        customers = frozenset(discover_mkeys(listing.text))
        if not customers:
            raise ValueError('Arisu did not return the account customer list')
        return customers

    # The list read at login can be partial, and the site reports this account's own customers as held too.
    # So nothing is asked, and no link removed, until a fresh list agrees that the customer is missing.
    customers = account()
    if customer in customers:
        session.arisu_customer_numbers = customers
        return 'listed'
    csrf = update_csrf(session, _mkey(session, 'GET', 'NR_mkeyList.do', False, timeout, _m='m6_2').text)
    if not csrf:
        raise ValueError('Arisu registration form does not contain its required CSRF token')
    excluded = _answer(_mkey(session, 'POST', 'JR_mkeySearchExceptCheck.do', False, timeout, mkey=customer))
    if excluded.get('success') is not True:
        _refuse(excluded)
    if excluded.get('blocked') is True:
        _refuse(excluded, '검색 제외로 설정된 고객번호입니다')
    named = _answer(_mkey(session, 'POST', 'JR_getCsNmFlag.do', True, timeout, searchMkey=customer, searchCsNm=name))
    if named.get('csNmFlag') != 'Y':
        raise RegistrationRefused('고객번호와 사용자명이 불일치합니다')
    if named.get('area') == '7' and named.get('apthouseCd') != 'M':
        raise RegistrationRefused('위탁관리 대상자입니다. 관리사무소에 연락하세요')
    method = _lookup(session, customer, name, timeout).get('gcMthCd')
    if not method:
        raise ValueError('Arisu did not say how the meter is read')
    if method != REMOTE_READ:
        raise NotRemoteMeter('원격검침 계량기가 아닙니다')
    used = _answer(_mkey(session, 'GET', 'JR_getMkeyChk.do', True, timeout, searchMkey=customer)).get('mkeyUseFlag') or 0
    try:
        held = float(used) > 0
    except (TypeError, ValueError):
        held = True
    if held and not release:
        raise RegistrationRefused('다른 회원에게 등록된 고객번호입니다. 아리수 고객번호관리에서 직접 등록해 주세요')
    if held:
        done(_mkey(session, 'POST', 'NR_mkeyDeleteUpdateAction.do', True, timeout, searchMkey=customer, searchCsNm=name), RELEASED)
    done(_mkey(session, 'POST', 'TR_mkeyInsertAction.do', False, timeout, **csrf, cusNo=customer, userName=name, mkey=customer,
               searchMkey=customer, searchCsNm=name, csRelationCd=relation), REGISTERED)
    customers = account()
    if customer not in customers:
        raise ValueError('Arisu accepted the registration but does not list the customer')
    session.arisu_customer_numbers = customers
    return 'released' if held else 'registered'
