"""Invite-only accounts, hashed sessions and CSRF checks without external auth services."""
import hashlib
import hmac
import secrets
import time
from fastapi import Depends, HTTPException, Request
from . import config, db

COOKIE = 'water_session'


def hash_password(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise ValueError('비밀번호는 12~128자로 입력하세요.')
    salt = secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=salt.encode(), n=32768, r=8, p=3, maxmem=64*1024*1024)
    return f'scrypt${salt}${value.hex()}'


def verify_password(password, stored):
    try:
        scheme, salt, expected = stored.split('$')
        if scheme != 'scrypt' or not 12 <= len(password) <= 128:
            return False
        value = hashlib.scrypt(password.encode(), salt=salt.encode(), n=32768, r=8, p=3, maxmem=64*1024*1024)
        return hmac.compare_digest(value.hex(), expected)
    except (ValueError, TypeError):
        return False


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def public_user(c, row):
    return {**{k: row[k] for k in ('id', 'email', 'name', 'role')},
            'active': bool(row['active']), 'must_change_password': bool(row['must_change_password']),
            'office_ids': [r[0] for r in c.execute('SELECT office_id FROM office_members WHERE user_id=? ORDER BY office_id', (row['id'],))]}


def bootstrap_admin():
    if not config.ADMIN_EMAIL or not config.ADMIN_PASSWORD:
        return
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if not c.execute('SELECT 1 FROM users LIMIT 1').fetchone():
            c.execute('INSERT INTO users(email,name,password_hash,role,must_change_password,created_at) VALUES(?,?,?,?,?,?)',
                      (config.ADMIN_EMAIL, config.ADMIN_NAME, hash_password(config.ADMIN_PASSWORD), 'superadmin', 1, db.now()))


def check_origin(request):
    origin = request.headers.get('origin')
    if origin and origin not in config.FRONT_ORIGINS and origin != str(request.base_url).rstrip('/'):
        raise HTTPException(403, '허용되지 않은 요청 출처입니다.')


def current_user(request: Request):
    token = request.cookies.get(COOKIE, '')
    with db.connect() as c:
        row = c.execute('SELECT u.*,s.csrf_token FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>? AND u.active=1',
                        (token_hash(token), time.time())).fetchone()
        if row is None:
            raise HTTPException(401, '로그인이 필요합니다.')
        user = public_user(c, row)
        user['csrf_token'] = row['csrf_token']
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        check_origin(request)
        if not hmac.compare_digest(request.headers.get('x-csrf-token', '').encode(), user['csrf_token'].encode()):
            raise HTTPException(403, '세션 확인에 실패했습니다. 다시 로그인하세요.')
    if user['must_change_password'] and request.url.path not in ('/api/auth/me', '/api/auth/password', '/api/auth/logout'):
        raise HTTPException(403, '임시 비밀번호를 먼저 변경하세요.')
    return user


def superadmin(user=Depends(current_user)):
    if user['role'] != 'superadmin':
        raise HTTPException(403, '총괄 관리자 권한이 필요합니다.')
    return user


def office_scope(user):
    return None if user['role'] == 'superadmin' else user['office_ids']


def require_office(user, office_id):
    if user['role'] != 'superadmin' and office_id not in user['office_ids']:
        raise HTTPException(403, '담당 사업소의 데이터만 관리할 수 있습니다.')
