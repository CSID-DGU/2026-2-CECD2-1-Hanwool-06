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


class LoginError(RuntimeError):
    pass


class ConfigurationError(LoginError):
    pass


class CustomerAccessError(RuntimeError):
    pass


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
