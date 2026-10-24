"""Private water-monitoring API and production SPA entry point."""
import json
import queue
import re
import secrets
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, timezone
from urllib.parse import quote

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from back.pipelines.common import today
from . import agent, auth, catalog, chat_agent, collection, config, data_access, db, documents, mailer


@asynccontextmanager
async def lifespan(app):
    config.validate_deployment()
    db.init_db()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        catalog.bootstrap(c)
    auth.bootstrap_admin()
    stop, thread = collection.start_worker()
    try:
        yield
    finally:
        stop.set()
        if thread.is_alive():
            thread.join(timeout=2)


app = FastAPI(title='서울교통공사 상수도 관리', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(CORSMiddleware, allow_origins=config.FRONT_ORIGINS, allow_credentials=True,
                   allow_methods=['GET', 'POST', 'PATCH', 'DELETE'], allow_headers=['Content-Type', 'X-CSRF-Token'])
app.add_middleware(TrustedHostMiddleware, allowed_hosts=config.ALLOWED_HOSTS)
app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=5)


MAX_BODY_BYTES = 1_000_000
# The built SPA loads only its own scripts, styles and images; React sets styles through the DOM.
CONTENT_SECURITY_POLICY = ("default-src 'self'; img-src 'self' data:; object-src 'none'; "
                           "base-uri 'self'; form-action 'self'; frame-ancestors 'self'")


@app.middleware('http')
async def private_headers(request, call_next):
    # Every request body is a small JSON form; refuse larger or unframed ones before reading them.
    length = request.headers.get('content-length', '0')
    if 'transfer-encoding' in request.headers or not length.isdigit() or int(length) > MAX_BODY_BYTES:
        response = JSONResponse({'detail': '요청 본문이 너무 큽니다.'}, status_code=413)
    else:
        response = await call_next(request)
    if response.status_code >= 400:
        mark_download(response, request.query_params.get('dl'), '0')   # the screen waiting for this file can stop
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    else:
        response.headers['Content-Security-Policy'] = CONTENT_SECURITY_POLICY
    if config.COOKIE_SECURE:
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    return response


@app.get('/api/health')
def health():
    with db.connect() as c:
        setup_required = not bool(c.execute('SELECT 1 FROM users LIMIT 1').fetchone())
    return {'ok': True, 'setup_required': setup_required}


class StrictModel(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Login(StrictModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)


DUMMY_HASH = auth.hash_password(secrets.token_urlsafe(32))
# ponytail: Four concurrent scrypt checks bound memory on one EC2 worker; use proxy rate limits for heavier traffic.
LOGIN_HASH_SLOTS = threading.BoundedSemaphore(4)


@app.post('/api/auth/login')
def login(body: Login, request: Request, response: Response):
    auth.check_origin(request)
    email = body.email.strip().lower()
    host = request.client.host if request.client else 'unknown'
    identity = auth.token_hash('email-ip:' + email + ':' + host)
    ip_identity = auth.token_hash('ip:' + host)
    now = time.time()
    # Limit the source, not a whole account; the trusted proxy must pass the real client IP.
    with db.connect() as c:
        counts = {r['identity']: r['n'] for r in c.execute(
            'SELECT identity,count(*) AS n FROM login_attempts WHERE identity IN (?,?) AND attempted_at>=? GROUP BY identity',
            (identity, ip_identity, now-900))}
        blocked = counts.get(identity, 0) >= 5 or counts.get(ip_identity, 0) >= 30
        row = c.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
    if blocked:
        raise HTTPException(429, '로그인 시도가 너무 많습니다. 15분 후 다시 시도하세요.')
    if not LOGIN_HASH_SLOTS.acquire(blocking=False):
        raise HTTPException(429, '로그인 요청이 많습니다. 잠시 후 다시 시도하세요.')
    try:
        valid = auth.verify_password(body.password, row['password_hash'] if row else DUMMY_HASH)
    finally:
        LOGIN_HASH_SLOTS.release()
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('DELETE FROM login_attempts WHERE attempted_at<?', (now-900,))
        counts = {r['identity']: r['n'] for r in c.execute(
            'SELECT identity,count(*) AS n FROM login_attempts WHERE identity IN (?,?) GROUP BY identity',
            (identity, ip_identity))}
        if counts.get(identity, 0) >= 5 or counts.get(ip_identity, 0) >= 30:
            raise HTTPException(429, '로그인 시도가 너무 많습니다. 15분 후 다시 시도하세요.')
        current = c.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
        if not valid or not current or not current['active'] or not row or current['password_hash'] != row['password_hash']:
            c.executemany('INSERT INTO login_attempts(attempted_at,identity) VALUES(?,?)',
                          [(now, identity), (now, ip_identity)])
            user = None
        else:
            c.execute('DELETE FROM login_attempts WHERE identity=?', (identity,))
            c.execute('DELETE FROM sessions WHERE expires_at<?', (now,))
            token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            c.execute('INSERT INTO sessions VALUES(?,?,?,?)', (auth.token_hash(token), current['id'], csrf, now + config.SESSION_HOURS*3600))
            user = auth.public_user(c, current)
            db.audit(c, user['id'], 'login')
    if user is None:
        raise HTTPException(401, '이메일 또는 비밀번호를 확인하세요.')
    response.set_cookie(auth.COOKIE, token, max_age=config.SESSION_HOURS*3600, httponly=True, secure=config.COOKIE_SECURE, samesite='lax', path='/')
    return {'user': user, 'csrf_token': csrf}


@app.get('/api/auth/me')
def me(user=Depends(auth.current_user)):
    return {'user': {k: v for k, v in user.items() if k != 'csrf_token'}, 'csrf_token': user['csrf_token']}


@app.post('/api/auth/logout')
def logout(request: Request, response: Response, user=Depends(auth.current_user)):
    with db.connect() as c:
        c.execute('DELETE FROM sessions WHERE token_hash=?', (auth.token_hash(request.cookies.get(auth.COOKIE, '')),))
    response.delete_cookie(auth.COOKIE, path='/', secure=config.COOKIE_SECURE, httponly=True, samesite='lax')
    return {'ok': True}


class Password(StrictModel):
    current_password: str = Field(max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


@app.post('/api/auth/password')
def password(body: Password, request: Request, user=Depends(auth.current_user)):
    token = auth.token_hash(request.cookies.get(auth.COOKIE, ''))
    identity = auth.token_hash(f"password:{user['id']}")
    now = time.time()
    with db.connect() as c:
        tries = c.execute('SELECT count(*) FROM login_attempts WHERE identity=? AND attempted_at>=?', (identity, now-900)).fetchone()[0]
        row = c.execute('SELECT password_hash FROM users WHERE id=?', (user['id'],)).fetchone()
    too_many = HTTPException(429, '현재 비밀번호를 여러 번 틀렸습니다. 15분 후 다시 시도하세요.')
    if tries >= 5:
        raise too_many
    # Hashing shares the login limit and never runs while holding the database writer lock.
    if not LOGIN_HASH_SLOTS.acquire(blocking=False):
        raise HTTPException(429, '요청이 많습니다. 잠시 후 다시 시도하세요.')
    try:
        valid = auth.verify_password(body.current_password, row['password_hash'])
        same = body.current_password == body.new_password
        new_hash = auth.hash_password(body.new_password) if valid and not same else None
    finally:
        LOGIN_HASH_SLOTS.release()
    saved = False
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        # Requests that were already hashing when the limit filled up get no extra try, not even a correct one.
        limited = c.execute('SELECT count(*) FROM login_attempts WHERE identity=? AND attempted_at>=?', (identity, now-900)).fetchone()[0] >= 5
        if limited:
            pass
        elif not valid:
            c.execute('INSERT INTO login_attempts(attempted_at,identity) VALUES(?,?)', (now, identity))
        elif not same:
            # Save only if the verified password and this session are still the current ones: a reset
            # made while this request was hashing must win over it.
            alive = c.execute('SELECT 1 FROM sessions WHERE token_hash=? AND user_id=? AND expires_at>?', (token, user['id'], now)).fetchone()
            saved = bool(alive) and c.execute('UPDATE users SET password_hash=?,must_change_password=0 WHERE id=? AND active=1 AND password_hash=?',
                                              (new_hash, user['id'], row['password_hash'])).rowcount == 1
            if saved:
                c.execute('DELETE FROM login_attempts WHERE identity=?', (identity,))
                c.execute('DELETE FROM sessions WHERE user_id=? AND token_hash<>?', (user['id'], token))
                db.audit(c, user['id'], 'password_change', user['id'])
    if limited:
        raise too_many
    if not valid:
        raise HTTPException(400, '현재 비밀번호를 확인하세요.')
    if same:
        raise HTTPException(400, '기존 비밀번호와 다른 값을 입력하세요.')
    if not saved:
        raise HTTPException(401, '로그인이 만료됐거나 비밀번호가 다른 곳에서 바뀌었습니다. 다시 로그인하세요.')
    return {'ok': True}


@app.get('/api/data')
def data(version: str | None = None, user=Depends(auth.current_user)):
    scope = auth.office_scope(user)
    # Fingerprint first: a change during the build then costs one more reload, never stale data.
    current = catalog.data_version(scope)
    if version == current:
        return Response(status_code=204)
    # The payload is plain JSON data already; skip FastAPI's per-item encoder for this large response.
    return JSONResponse({'version': current} | catalog.payload(scope))


@app.get('/api/offices')
def offices(user=Depends(auth.current_user)):
    with db.connect() as c:
        return [dict(r) for r in c.execute('SELECT * FROM offices ORDER BY name') if user['role'] == 'superadmin' or r['id'] in user['office_ids']]


@app.get('/api/meters')
def meters(include_deleted: bool = False, user=Depends(auth.current_user)):
    with db.connect() as c:
        return catalog.list_meters(c, auth.office_scope(user), include_deleted=include_deleted)


def get_meter(c, meter_id, user, include_deleted=False):
    meter = next((m for m in catalog.list_meters(c, auth.office_scope(user), include_deleted=include_deleted) if m['id'] == meter_id), None)
    if meter is None:
        raise HTTPException(404, '계량기를 찾을 수 없습니다.')
    return meter


METER_FIELDS = {'provider', 'customer_number', 'station_id', 'station_name', 'office_id', 'line', 'display_name', 'purpose', 'tariff', 'address', 'active', 'daily_enabled', 'metadata', 'map_x', 'map_y'}


def meter_values(c, body, user, existing=None):
    if set(body) - METER_FIELDS:
        raise HTTPException(422, '허용되지 않은 계량기 필드입니다.')
    value = (existing or {}) | body
    for key in ('provider', 'customer_number', 'station_id', 'station_name', 'office_id', 'line', 'display_name', 'purpose', 'tariff', 'address'):
        raw = value.get(key) or ''
        if not isinstance(raw, (str, int)) or len(str(raw)) > (500 if key == 'address' else 100) or any(ord(ch) < 32 for ch in str(raw)):
            raise HTTPException(422, f'{key} 값이 올바르지 않습니다.')
        value[key] = str(raw).strip()
    value['provider'] = value['provider'] or 'arisu'
    if value['provider'] not in ('arisu', 'other'):
        raise HTTPException(422, 'provider는 arisu 또는 other여야 합니다.')
    cid = value['customer_number']
    if not cid or (value['provider'] == 'arisu' and not re.fullmatch(r'\d{9}', cid)):
        raise HTTPException(422, '아리수 고객번호는 앞자리 0을 포함한 9자리입니다.')
    if existing and (cid != existing['customer_number'] or value['provider'] != existing['provider']):
        raise HTTPException(422, '고객번호나 공급기관이 바뀌었다면 새 계량기로 등록하세요.')
    if not value['display_name']:
        raise HTTPException(422, '계량기 표시명을 입력하세요.')
    if value['line'] and not re.fullmatch(r'[1-8]', value['line']):   # 서울교통공사는 1~8호선만 관리한다
        raise HTTPException(422, '호선은 1~8 중 선택하세요.')
    for key in ('active', 'daily_enabled'):
        raw = value.get(key, key == 'active')
        if raw not in (True, False, 0, 1):
            raise HTTPException(422, f'{key}는 참/거짓이어야 합니다.')
        value[key] = int(raw)
    if value['provider'] != 'arisu' and value['daily_enabled']:
        raise HTTPException(422, '일일 사용량은 아리수 계량기만 수집할 수 있습니다.')
    auth.require_office(user, value['office_id'])
    if not c.execute('SELECT 1 FROM offices WHERE id=?', (value['office_id'],)).fetchone():
        raise HTTPException(422, '등록된 사업소를 선택하세요.')
    station_id = value['station_id']
    if station_id and not c.execute('SELECT 1 FROM stations WHERE id=?', (station_id,)).fetchone():
        raise HTTPException(422, '등록된 역을 선택하거나 새 역명을 입력하세요.')
    if not station_id:
        station_id = value['station_name']
        if not station_id:
            raise HTTPException(422, '역명을 입력하세요.')
        c.execute('INSERT OR IGNORE INTO stations(id,name) VALUES(?,?)', (station_id, station_id))
    value['station_id'] = station_id
    # Coordinates describe the physical station shared by several meters.
    if 'map_x' in body or 'map_y' in body:
        if user['role'] != 'superadmin':
            raise HTTPException(403, '노선도 위치는 총괄 관리자만 바꿀 수 있습니다.')
        x, y = value.get('map_x'), value.get('map_y')
        if not all(isinstance(v, (int, float)) and 0 <= v <= 100 for v in (x, y)):
            raise HTTPException(422, '지도 위치는 0~100 사이의 x,y 값을 함께 입력하세요.')
        c.execute('UPDATE stations SET map_x=?,map_y=? WHERE id=?', (x, y, station_id))
    metadata = value.get('metadata') or {}
    if not isinstance(metadata, dict):
        raise HTTPException(422, '계량기 메타데이터가 올바르지 않습니다.')
    if 'metadata' in body:
        if not isinstance(body['metadata'], dict):
            raise HTTPException(422, '계량기 메타데이터가 올바르지 않습니다.')
        if set(body['metadata']) - {'계량기번호', 'arisu_customer_name'} or any(not isinstance(v, str) or len(v) > 100 or any(ord(ch) < 32 for ch in v) for v in body['metadata'].values()):
            raise HTTPException(422, '계량기번호와 고지서상 성명은 100자 이하로 입력하세요.')
        metadata = (existing or {}).get('metadata', {}) | body['metadata']
    name = metadata.get('arisu_customer_name', '').strip()
    if '*' in name or '＊' in name:
        raise HTTPException(422, '별표로 가린 성명 대신 아리수에 등록된 고지서상 성명 전체를 입력하세요.')
    if 'arisu_customer_name' in metadata:
        metadata['arisu_customer_name'] = name
    value['metadata'] = json.dumps(metadata, ensure_ascii=False)
    return value


def save_meter(body, user, meter_id=None):
    try:
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            existing = get_meter(c, meter_id, user) if meter_id else None
            if existing and c.execute("SELECT 1 FROM collection_items WHERE meter_id=? AND status='running'", (meter_id,)).fetchone():
                raise HTTPException(409, '현재 수집 중인 계량기입니다. 수집 완료 후 수정하세요.')
            v = meter_values(c, body, user, existing)
            columns = ('provider', 'customer_number', 'station_id', 'office_id', 'line', 'display_name', 'purpose', 'tariff', 'address', 'active', 'daily_enabled', 'metadata')
            if existing:
                c.execute('UPDATE meters SET ' + ','.join(f'{k}=?' for k in columns) + ',updated_at=? WHERE id=?', [v[k] for k in columns] + [db.now(), meter_id])
            else:
                meter_id = v['customer_number'] if v['provider'] == 'arisu' else 'other-' + secrets.token_hex(8)
                c.execute('INSERT INTO meters(id,' + ','.join(columns) + ',created_at,updated_at) VALUES(' + ','.join('?' for _ in range(len(columns)+3)) + ')', [meter_id] + [v[k] for k in columns] + [db.now(), db.now()])
                if v['provider'] == 'arisu' and v['active']:
                    collection.enqueue(c, [meter_id], 'registration', user['id'])
            db.audit(c, user['id'], 'meter_update' if existing else 'meter_create', meter_id, {k: body[k] for k in body if k != 'metadata'})
            return get_meter(c, meter_id, user)
    except sqlite3.IntegrityError:
        raise HTTPException(409, '이미 등록된 고객번호입니다.') from None


@app.post('/api/meters', status_code=201)
def create_meter(body: dict = Body(...), user=Depends(auth.current_user)):
    return save_meter(body, user)


@app.patch('/api/meters/{meter_id}')
def patch_meter(meter_id: str, body: dict = Body(...), user=Depends(auth.current_user)):
    return save_meter(body, user, meter_id)


@app.delete('/api/meters/{meter_id}')
def delete_meter(meter_id: str, user=Depends(auth.current_user)):
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        get_meter(c, meter_id, user)
        if c.execute("SELECT 1 FROM collection_items WHERE meter_id=? AND status='running'", (meter_id,)).fetchone():
            raise HTTPException(409, '수집 중인 계량기는 수집 완료 후 삭제할 수 있습니다.')
        c.execute('UPDATE meters SET deleted_at=?,active=0,updated_at=? WHERE id=?', (db.now(), db.now(), meter_id))
        c.execute("UPDATE collection_items SET status='skipped',message='삭제되어 수집에서 제외했습니다.',finished_at=? WHERE meter_id=? AND status='queued'", (db.now(), meter_id))
        db.audit(c, user['id'], 'meter_delete', meter_id)
    return {'ok': True}


@app.post('/api/meters/{meter_id}/restore')
def restore_meter(meter_id: str, user=Depends(auth.current_user)):
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        meter = get_meter(c, meter_id, user, include_deleted=True)
        if not meter['deleted_at']:
            raise HTTPException(409, '삭제되지 않은 계량기입니다.')
        c.execute('UPDATE meters SET deleted_at=NULL,active=1,updated_at=? WHERE id=?', (db.now(), meter_id))
        if meter['provider'] == 'arisu':
            collection.enqueue(c, [meter_id], 'restoration', user['id'])
        db.audit(c, user['id'], 'meter_restore', meter_id)
        return get_meter(c, meter_id, user)


@app.get('/api/collection')
def collection_status(user=Depends(auth.current_user)):
    return collection.status(user)


class CollectionRequest(StrictModel):
    meter_id: str | None = Field(default=None, min_length=1, max_length=100)


@app.post('/api/collection', status_code=202)
def collect(body: CollectionRequest, user=Depends(auth.current_user)):
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        selected = [get_meter(c, body.meter_id, user)] if body.meter_id else catalog.list_meters(c, auth.office_scope(user))
        selected = [m for m in selected if m['active'] and m['provider'] == 'arisu']
        job_id = collection.enqueue(c, [m['id'] for m in selected], 'manual', user['id'])
        db.audit(c, user['id'], 'collection_start', job_id, {'meter_count': len(selected)})
        return collection.job_view(c, c.execute('SELECT * FROM collection_jobs WHERE id=?', (job_id,)).fetchone(), {m['id']: m for m in selected})


@app.get('/api/users')
def users(user=Depends(auth.superadmin)):
    with db.connect() as c:
        # 최근 로그인: 로그인에 성공할 때마다 남기는 감사 기록(login) 중 가장 늦은 시각. 한 번도 없으면 None.
        # 사용자마다 (actor_id, action) 인덱스로 바로 찾는다. 감사 기록 전체를 훑지 않는다.
        return [auth.public_user(c, row) | {'last_login_at': row['last_login_at']} for row in c.execute(
            "SELECT u.*,(SELECT max(created_at) FROM audit_log a WHERE a.actor_id=u.id AND a.action='login') AS last_login_at "
            'FROM users u WHERE u.deleted_at IS NULL ORDER BY u.name,u.id')]


def save_user(body, actor, user_id=None):
    if set(body) - {'email', 'name', 'password', 'role', 'office_ids', 'active'}:
        raise HTTPException(422, '허용되지 않은 사용자 필드입니다.')
    try:
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM users WHERE id=? AND deleted_at IS NULL', (user_id,)).fetchone() if user_id else None
            if user_id and row is None:
                raise HTTPException(404, '사용자를 찾을 수 없습니다.')
            if row:
                auth.require_manageable(actor, row)
            old = auth.public_user(c, row) if row else {}
            v = old | body
            email, name = v.get('email', ''), v.get('name', '')
            if not isinstance(email, str) or len(email) > 254 or not re.fullmatch(r'[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+', email):
                raise HTTPException(422, '유효한 업무 이메일을 입력하세요.')
            email = email.strip().lower()
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
                raise HTTPException(422, '담당자 이름을 입력하세요.')
            if v.get('role') not in ('superadmin', 'office_admin') or v.get('active', True) not in (True, False, 0, 1):
                raise HTTPException(422, '역할과 활성 상태를 확인하세요.')
            active, role = bool(v.get('active', True)), v['role']
            offices = v.get('office_ids', [])
            if not isinstance(offices, list) or any(not isinstance(o, str) for o in offices):
                raise HTTPException(422, '담당 사업소 목록이 올바르지 않습니다.')
            offices = sorted(set(offices)) if role == 'office_admin' else []
            if role == 'office_admin' and active and not offices:
                raise HTTPException(422, '사업소 담당자는 담당 사업소를 지정해야 합니다.')
            for office in offices:
                if not c.execute('SELECT 1 FROM offices WHERE id=?', (office,)).fetchone():
                    raise HTTPException(422, '등록된 사업소를 선택하세요.')
                count = c.execute('SELECT count(*) FROM office_members m JOIN users u ON u.id=m.user_id WHERE m.office_id=? AND u.active=1 AND u.id<>?', (office, user_id or -1)).fetchone()[0]
                if active and count >= 2:
                    raise HTTPException(409, '사업소 담당자는 최대 2명까지 지정할 수 있습니다.')
            if row and row['role'] == 'superadmin' and row['active'] and (role != 'superadmin' or not active):
                if not c.execute("SELECT 1 FROM users WHERE role='superadmin' AND active=1 AND id<>?", (user_id,)).fetchone():
                    raise HTTPException(409, '활성 총괄 관리자를 최소 1명 유지해야 합니다.')
            password_hash = row['password_hash'] if row else None
            if body.get('password'):
                try:
                    password_hash = auth.hash_password(body['password'])
                except ValueError as error:
                    raise HTTPException(422, str(error)) from None
            if password_hash is None:
                raise HTTPException(422, '최초 비밀번호를 입력하세요.')
            if row:
                c.execute('UPDATE users SET email=?,name=?,role=?,active=?,password_hash=?,must_change_password=? WHERE id=?',
                          (email, name.strip(), role, int(active), password_hash, int(bool(body.get('password')) or row['must_change_password']), user_id))
                c.execute('DELETE FROM office_members WHERE user_id=?', (user_id,))
                c.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))
            else:
                cursor = c.execute('INSERT INTO users(email,name,role,active,password_hash,must_change_password,created_at) VALUES(?,?,?,?,?,1,?)', (email, name.strip(), role, int(active), password_hash, db.now()))
                user_id = cursor.lastrowid
            c.executemany('INSERT INTO office_members(user_id,office_id) VALUES(?,?)', [(user_id, office) for office in offices])
            db.audit(c, actor['id'], 'user_update' if row else 'user_create', user_id, {k: body[k] for k in body if k != 'password'})
            return auth.public_user(c, c.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone())
    except sqlite3.IntegrityError:
        raise HTTPException(409, '이미 등록된 이메일입니다.') from None


@app.post('/api/users', status_code=201)
def create_user(body: dict = Body(...), user=Depends(auth.superadmin)):
    return save_user(body, user)


@app.patch('/api/users/{user_id}')
def patch_user(user_id: int, body: dict = Body(...), user=Depends(auth.superadmin)):
    return save_user(body, user, user_id)


@app.delete('/api/users/{user_id}')
def delete_user(user_id: int, user=Depends(auth.superadmin)):
    if user_id == user['id']:
        raise HTTPException(409, '본인 계정은 삭제할 수 없습니다.')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        row = c.execute('SELECT * FROM users WHERE id=? AND deleted_at IS NULL', (user_id,)).fetchone()
        if row is None:
            raise HTTPException(404, '사용자를 찾을 수 없습니다.')
        auth.require_manageable(user, row)
        old = auth.public_user(c, row)
        # 활동 기록(audit_log·수집 작업)이 이 사용자를 가리키므로 행은 남긴다. 같은 이메일로 다시 등록할 수 있게 이메일은 비운다.
        c.execute('UPDATE users SET email=?,active=0,deleted_at=? WHERE id=?', (f'deleted-{user_id}@deleted.invalid', db.now(), user_id))
        for table in ('sessions', 'office_members', 'chat_conversations'):
            c.execute(f'DELETE FROM {table} WHERE user_id=?', (user_id,))
        db.audit(c, user['id'], 'user_delete', user_id, {k: old[k] for k in ('email', 'name', 'role', 'office_ids')})
    return {'ok': True}


def checked_date(value):
    try:
        return Date.fromisoformat(value).isoformat()
    except (ValueError, TypeError):
        raise HTTPException(422, '날짜는 YYYY-MM-DD 형식이어야 합니다.') from None


@app.get('/api/anomalies')
def anomalies(date: str | None = None, include_normal: bool = False, user=Depends(auth.current_user)):
    scope = auth.office_scope(user)
    date = checked_date(date or data_access.reference_date(scope))
    return {'기준일': date, 'calendar': data_access.calendar_info(date), 'items': data_access.anomalies_on(date, include_normal, scope)}


@app.get('/api/summary')
def summary(date: str | None = None, user=Depends(auth.current_user)):
    scope = auth.office_scope(user)
    return agent.daily_summary(checked_date(date or data_access.reference_date(scope)), scope)


class Analyze(StrictModel):
    meter_id: str = Field(min_length=1, max_length=100)
    date: str | None = None


def analysis_input(body, user):
    with db.connect() as c:
        meter = get_meter(c, body.meter_id, user)
    date = checked_date(body.date or data_access.reference_date(auth.office_scope(user)))
    item = data_access.find_one(body.meter_id, date, auth.office_scope(user))
    if not item:
        raise HTTPException(404, '이 계량기는 해당 날짜의 분석 자료가 없습니다.')
    if item['likely_data_error']:
        raise HTTPException(422, '자료 확인 항목이라 분석과 알림을 쓸 수 없습니다. 원자료를 먼저 확인하세요.')
    return meter, date, item


def attempts_last_hour(c, actor_id, action):
    cutoff = datetime.fromtimestamp(time.time() - 3600, timezone.utc).isoformat(timespec='seconds')
    return c.execute('SELECT count(*) FROM audit_log WHERE actor_id=? AND action=? AND created_at>=?', (actor_id, action, cutoff)).fetchone()[0]


@app.post('/api/analyze')
def analyze(body: Analyze, user=Depends(auth.current_user)):
    _, date, _ = analysis_input(body, user)
    if config.openai_ready():
        with db.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if attempts_last_hour(c, user['id'], 'analyze_attempt') >= 20:
                raise HTTPException(429, '원인 분석은 계정당 한 시간에 20회까지 요청할 수 있습니다.')
            db.audit(c, user['id'], 'analyze_attempt', body.meter_id + ':' + date)
    return agent.analyze_cause(body.meter_id, date, auth.office_scope(user))


class Chat(StrictModel):
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')


@app.get('/api/chat/status')
def chat_status(user=Depends(auth.current_user)):
    return {'active': chat_agent.connection_active()}


def chat_message(body: Chat):
    if not body.message.strip() or any(ord(char) < 32 and char not in '\n\r\t' for char in body.message):
        raise HTTPException(422, '질문은 공백을 제외한 1~2000자로 입력하세요.')
    return body.message.strip()


@app.post('/api/chat')
def chat(body: Chat, user=Depends(auth.current_user)):
    return chat_agent.chat(chat_message(body), body.conversation_id, user['id'])


@app.post('/api/chat/stream')
def chat_stream(body: Chat, user=Depends(auth.current_user)):
    """진행 단계(서버가 정한 코드)를 먼저 흘려보내고, 마지막에 검증을 마친 답이나 오류를 보낸다(text/event-stream).

    모델이 쓰는 문장은 검증 전에는 보내지 않는다. 인증·입력 오류는 스트림을 열기 전에 평소처럼 응답한다.
    gzip은 이 형식을 압축하지 않고, X-Accel-Buffering으로 nginx가 모아 보내지 않게 한다.
    """
    message = chat_message(body)
    events = queue.Queue()

    def run():
        try:
            result = chat_agent.chat(message, body.conversation_id, user['id'],
                                     progress=lambda stage: events.put({'type': 'progress', 'stage': stage}))
            events.put({**result, 'type': 'result'})
        except HTTPException as exc:
            events.put({'type': 'error', 'status': exc.status_code, 'detail': exc.detail})
        except Exception:   # chat()이 이미 502로 바꾸지만, 스레드 밖으로 새지 않게 한 번 더 막는다
            events.put({'type': 'error', 'status': 502, 'detail': '대화 서비스에서 확인된 답을 받지 못했습니다. 잠시 후 다시 시도하세요.'})

    threading.Thread(target=run, daemon=True).start()

    def stream():
        while True:
            try:
                event = events.get(timeout=chat_agent.MAX_SECONDS + 30)
            except queue.Empty:
                event = {'type': 'error', 'status': 504, 'detail': '답변 작성 시간이 초과되었습니다. 범위를 좁혀 다시 질문하세요.'}
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            if event['type'] != 'progress':
                return

    return StreamingResponse(stream(), media_type='text/event-stream', headers={'X-Accel-Buffering': 'no'})


class RiskLabel(StrictModel):
    meter_id: str = Field(min_length=1, max_length=100)
    date: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')
    label: str = Field(pattern=r'^(anomaly|normal|unknown)$')   # 이상 발생, 이상 없음, 알 수 없음


@app.get('/api/risk-labels')
def risk_labels(meter_id: str, user=Depends(auth.current_user)):
    with db.connect() as c:
        get_meter(c, meter_id, user)
        return {r['date']: dict(r) for r in c.execute(
            'SELECT l.date,l.label,l.severity,l.updated_at,u.name AS user_name FROM risk_labels l JOIN users u ON u.id=l.user_id WHERE l.meter_id=? ORDER BY l.date', (meter_id,))}


@app.post('/api/risk-labels')
def save_risk_label(body: RiskLabel, user=Depends(auth.current_user)):
    """주의·경고 날이 실제로 이상이었는지 사람이 확인해 남긴다. 모델을 고칠 때 쓸 정답 자료라 그때의 판정도 함께 둔다."""
    _, date, item = analysis_input(body, user)
    if item['심각도'] not in ('주의', '경고'):
        raise HTTPException(422, '주의·경고로 판정한 날에만 확인 결과를 남길 수 있습니다.')
    now = db.now()
    with db.connect() as c:
        c.execute('INSERT INTO risk_labels(meter_id,date,label,severity,user_id,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(meter_id,date) DO UPDATE '
                  'SET label=excluded.label,severity=excluded.severity,user_id=excluded.user_id,updated_at=excluded.updated_at',
                  (body.meter_id, date, body.label, item['심각도'], user['id'], now))
        db.audit(c, user['id'], 'risk_label', f'{body.meter_id}:{date}', {'label': body.label})
    return {'date': date, 'label': body.label, 'severity': item['심각도'], 'updated_at': now, 'user_name': user['name']}


def report_text(value, label):
    value = value.strip()
    if not value or any(ord(ch) < 32 and ch not in '\n\r\t' for ch in value):
        raise HTTPException(422, f'{label}을 입력하세요. 줄바꿈 말고는 특수 문자를 쓸 수 없습니다.')
    return value


class Report(StrictModel):
    title: str = Field(min_length=1, max_length=100)
    body: str = Field(min_length=1, max_length=2000)
    page: str = Field(default='', max_length=200, pattern=r'^[^\x00-\x1f]*$')   # 보고할 때 보던 화면(#/meters 등)


class ReportAnswer(StrictModel):
    answer: str = Field(min_length=1, max_length=2000)


@app.get('/api/reports')
def reports(user=Depends(auth.current_user)):
    """오류 보고(문의) 목록, 최근 순. 서버 관리자는 모든 보고를, 다른 사용자는 자기 문의 내역만 본다."""
    # ponytail: 보고가 적어 한 번에 전부 보낸다. 많아지면 쪽 단위로 나눠 받는다.
    query = '''SELECT r.id,r.user_id,u.name AS user_name,u.email AS user_email,r.title,r.body,r.page,r.created_at,
                      r.answer,a.name AS answered_by_name,r.answered_at,r.answer_read
               FROM reports r JOIN users u ON u.id=r.user_id LEFT JOIN users a ON a.id=r.answered_by'''
    with db.connect() as c:
        rows = c.execute(query + ' ORDER BY r.id DESC') if user['server_admin'] else c.execute(query + ' WHERE r.user_id=? ORDER BY r.id DESC', (user['id'],))
        return [dict(r) | {'answer_read': bool(r['answer_read'])} for r in rows]


@app.post('/api/reports', status_code=201)
def create_report(body: Report, user=Depends(auth.current_user)):
    title, text = report_text(body.title, '제목'), report_text(body.body, '내용')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        if attempts_last_hour(c, user['id'], 'report_create') >= 20:
            raise HTTPException(429, '오류 보고는 계정당 한 시간에 20건까지 보낼 수 있습니다.')
        report_id = c.execute('INSERT INTO reports(user_id,title,body,page,created_at) VALUES(?,?,?,?,?)',
                              (user['id'], title, text, body.page.strip(), db.now())).lastrowid
        db.audit(c, user['id'], 'report_create', report_id)
    return {'id': report_id}


@app.post('/api/reports/{report_id}/answer')
def answer_report(report_id: int, body: ReportAnswer, user=Depends(auth.current_user)):
    """답변은 서버 관리자만 단다. 고쳐 저장하면 보낸 사람에게 다시 새 답변으로 보인다."""
    if not user['server_admin']:
        raise HTTPException(403, '오류 보고에는 서버 관리자만 답변할 수 있습니다.')
    answer = report_text(body.answer, '답변')
    with db.connect() as c:
        if not c.execute('UPDATE reports SET answer=?,answered_by=?,answered_at=?,answer_read=0 WHERE id=?', (answer, user['id'], db.now(), report_id)).rowcount:
            raise HTTPException(404, '오류 보고를 찾을 수 없습니다.')
        db.audit(c, user['id'], 'report_answer', report_id)
    return {'ok': True}


@app.post('/api/reports/{report_id}/read')
def read_report(report_id: int, user=Depends(auth.current_user)):
    """보낸 사람이 답변을 열어 보면 확인한 것으로 남긴다(머리글의 새 답변 숫자, 서버 관리자의 확인 여부)."""
    with db.connect() as c:
        if not c.execute('SELECT 1 FROM reports WHERE id=? AND user_id=?', (report_id, user['id'])).fetchone():
            raise HTTPException(404, '오류 보고를 찾을 수 없습니다.')
        c.execute('UPDATE reports SET answer_read=1 WHERE id=? AND answer IS NOT NULL', (report_id,))
    return {'ok': True}


@app.post('/api/alert')
def alert(body: Analyze, user=Depends(auth.current_user)):
    meter, _, item = analysis_input(body, user)
    if item['심각도'] not in ('주의', '경고'):
        raise HTTPException(422, '주의·경고 항목에만 이상 알림을 발송할 수 있습니다.')
    with db.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        recipients = [r[0] for r in c.execute('SELECT u.email FROM users u JOIN office_members m ON m.user_id=u.id WHERE u.active=1 AND m.office_id=?', (meter['office_id'],))]
        recent = c.execute("SELECT created_at FROM audit_log WHERE action IN ('alert_attempt','alert_sent') AND target=? ORDER BY id DESC LIMIT 1", (body.meter_id + ':' + item['날짜'],)).fetchone()
        if recent and recent[0] > datetime.fromtimestamp(time.time()-60, timezone.utc).isoformat(timespec='seconds'):
            raise HTTPException(429, '같은 항목은 1분 후 다시 발송할 수 있습니다.')
        if attempts_last_hour(c, user['id'], 'alert_attempt') >= 30:
            raise HTTPException(429, '이상 알림은 계정당 한 시간에 30회까지 발송할 수 있습니다.')
        db.audit(c, user['id'], 'alert_attempt', body.meter_id + ':' + item['날짜'])
    result = mailer.send_mail(f"[{item['심각도']}] {item['역명']} 수도 사용량 확인 ({item['날짜']})", mailer.build_alert_html(item), ','.join(recipients))
    with db.connect() as c:
        db.audit(c, user['id'], 'alert_sent' if result['sent'] else 'alert_failed', body.meter_id + ':' + item['날짜'])
    return {**result, 'item': item}


def mark_download(response: Response, token: str | None, state: str = '1') -> Response:
    """화면이 '파일 준비 중' 안내를 내릴 수 있도록, 요청에 딸려 온 표식을 응답 쿠키로 돌려준다(1 준비됨, 0 만들지 못함)."""
    if token and re.fullmatch(r'[A-Za-z0-9_-]{1,32}', token):
        response.set_cookie(f'dl_{token}', state, max_age=60, path='/', samesite='lax', secure=config.COOKIE_SECURE, httponly=False)
    return response


@app.exception_handler(Exception)
async def unhandled_error(request: Request, exc: Exception):
    # The server still logs the error after this. A screen waiting for a file learns of the failure
    # at once instead of at its three-minute timeout; the cause is not sent to the browser.
    return mark_download(JSONResponse({'detail': '서버에서 요청을 처리하지 못했습니다.'}, status_code=500), request.query_params.get('dl'), '0')


@app.get('/api/bills/{bill_id}/pdf')
def bill_pdf(bill_id: str, download: bool = False, dl: str | None = None, user=Depends(auth.current_user)):
    with db.connect() as c:
        bill = c.execute('SELECT * FROM bills WHERE id=?', (bill_id,)).fetchone()
        # A bill outside the caller's scope is indistinguishable from a missing one.
        meter = bill and next((m for m in catalog.list_meters(c, auth.office_scope(user)) if m['id'] == bill['meter_id']), None)
        if not meter:
            raise HTTPException(404, '청구내역을 찾을 수 없습니다.')
    payload = json.loads(bill['payload']) | {'id': bill['id'], 'source': bill['source'], 'gubun': bill['gubun'], 'notice_number': bill['notice_number']}
    suffix = '_' + bill['notice_number'] if bill['notice_number'] else ''
    name = f"{meter['display_name']}_{bill['period']}_{bill['gubun']}{suffix}.pdf"
    return mark_download(Response(documents.bill_pdf(meter, payload), media_type='application/pdf',
                                  headers={'Content-Disposition': f"{'attachment' if download else 'inline'}; filename=water-bill.pdf; filename*=UTF-8''{quote(name, safe='')}"}), dl)


MAX_EXPORT_METERS = 1000


def parse_meter_ids(value):
    """Comma-separated meter IDs chosen on screen (e.g. a search result). None when the parameter is absent."""
    if value is None:
        return None
    ids = list(dict.fromkeys(part.strip() for part in value.split(',') if part.strip()))
    if not ids:
        raise HTTPException(422, '내려받을 계량기 목록이 비어 있습니다.')
    if len(ids) > MAX_EXPORT_METERS or any(len(i) > 100 or any(ord(ch) < 32 for ch in i) for i in ids):
        raise HTTPException(422, '계량기 목록이 올바르지 않습니다.')
    return set(ids)


def export_rows(user, kind, start=None, end=None, office_id=None, line=None, meter_id=None, meter_ids=None):
    if kind not in ('usage', 'bills'):
        raise HTTPException(422, 'kind는 usage 또는 bills여야 합니다.')
    start, end = checked_date(start or '2000-01-01'), checked_date(end or today().isoformat())
    if start > end:
        raise HTTPException(422, '시작일은 종료일보다 늦을 수 없습니다.')
    if office_id:
        auth.require_office(user, office_id)
    with db.connect() as c:
        available = catalog.list_meters(c, auth.office_scope(user))
        if meter_id:
            get_meter(c, meter_id, user)
        wanted = parse_meter_ids(meter_ids)
        # A meter outside the caller's scope is indistinguishable from a missing one.
        if wanted is not None and not wanted <= {m['id'] for m in available}:
            raise HTTPException(404, '계량기를 찾을 수 없습니다.')
        selected = [m for m in available if (not office_id or m['office_id'] == office_id) and (not line or m['line'] == line) and (not meter_id or m['id'] == meter_id)
                    and (wanted is None or m['id'] in wanted)]
        daily, _ = catalog.data_sources() if kind == 'usage' else ({}, {})
        rows = []
        for m in selected:
            meta = {'계량기ID': m['id'], '고객번호': m['customer_number'], '역명': m['display_name'], '사업소ID': m['office_id'], '영업사업소': m['office_name'], '호선': m['line'], '사용목적': m['purpose']}
            if kind == 'usage':
                if m['provider'] != 'arisu' or not m['daily_enabled']:
                    continue
                for r in daily.get(m['customer_number'], {}).get('usage', []):
                    if start <= r['date'] <= end:
                        rows.append(meta | {'날짜': r['date'], '사용량_톤': r['value']})
            else:
                for b in c.execute('SELECT * FROM bills WHERE meter_id=? AND period>=? AND period<=? ORDER BY period,gubun', (m['id'], start[:7], end[:7])):
                    payload = json.loads(b['payload']) | {'source': b['source']}
                    # 실무자가 보는 열만 둔다: 수집 출처·내부 표식(summary_only 등)은 빼고 원문 항목만 쓴다.
                    # 밑줄로 시작하는 값은 통계가 쓰는 내부 집계라 Excel에는 나가지 않는다.
                    rows.append(meta | {'청구월': b['period'], '청구구분': b['gubun'], '고지번호': b['notice_number'], **{k: payload.get(k) for k in ('사용량', '지하수사용량', '총사용량', '납부금액', '부과금액', '총사용금액', '차감금액', '상수도_기본료', '상수도_사용료', '하수도_사용료', '물이용부담금', '계량기대금', '설치비', '연체금', '수납상태', '납부방법', '납기일')},
                                        '_집계사용량_톤': catalog.bill_usage(payload)})
    return rows


def export_filename(kind, meter=None, start=None, end=None):
    """계량기 하나면 역이름_호선_고객번호_종류, 여럿이면 종류(_기간). 같은 역·호선에 계량기가 여럿 있어 고객번호로 구분한다."""
    label = '사용량' if kind == 'usage' else '청구내역'
    parts = [meter['station_name'], meter['line'] and f"{meter['line']}호선", meter['customer_number'], label] if meter else [label, start, end]
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', '_'.join(p for p in parts if p)) + '.xlsx'


@app.get('/api/export.xlsx')
def export(kind: str = 'usage', start: str | None = None, end: str | None = None, office_id: str | None = None, line: str | None = None, meter_id: str | None = None,
           meter_ids: str | None = None, dl: str | None = None, user=Depends(auth.current_user)):
    rows = [{k: v for k, v in row.items() if not k.startswith('_')} for row in export_rows(user, kind, start, end, office_id, line, meter_id, meter_ids)]
    with db.connect() as c:
        meter = get_meter(c, meter_id, user) if meter_id else None
    name = quote(export_filename(kind, meter, start, end), safe='')
    return mark_download(Response(documents.export_xlsx(rows), media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', headers={'Content-Disposition': f"attachment; filename=water-{kind}.xlsx; filename*=UTF-8''{name}"}), dl)


@app.get('/api/stats')
def statistics(start: str | None = None, end: str | None = None, office_id: str | None = None, line: str | None = None, meter_id: str | None = None, user=Depends(auth.current_user)):
    groups = {}
    for kind in ('usage', 'bills'):
        for row in export_rows(user, kind, start, end, office_id, line, meter_id):
            month = row.get('날짜', row.get('청구월'))[:7]
            key = (month, row['사업소ID'], row['호선'])
            group = groups.setdefault(key, {'month': month, 'office_id': row['사업소ID'], 'office_name': row['영업사업소'], 'line': row['호선'], 'usage_ton': None, 'billed_usage_ton': None, 'billed_won': None, 'observation_count': 0, 'bill_count': 0, 'summary_bill_count': 0})
            if kind == 'bills':
                if row.get('납부금액') is None:
                    row['납부금액'] = row.get('부과금액')
                    group['summary_bill_count'] += 1
            fields = [('usage_ton', '사용량_톤')] if kind == 'usage' else [('billed_usage_ton', '_집계사용량_톤'), ('billed_won', '납부금액')]
            for target, source in fields:
                number = catalog.number(row.get(source))
                if number is not None:
                    group[target] = round((group[target] or 0) + number, 3)
            group['observation_count' if kind == 'usage' else 'bill_count'] += 1
    return {'rows': [groups[key] for key in sorted(groups)], 'note': '일일 사용량은 사용일(물을 쓴 날), 요금은 청구월을 기준으로 합산했습니다. 납부금액이 없는 청구서는 부과금액으로 계산했고, 값이 없는 항목은 합계에서 뺐습니다.'}


# Only explicitly built frontend assets are public; raw data directories are never mounted.
DIST = config.ROOT / 'front/dist'
if (DIST / 'assets').is_dir():
    app.mount('/assets', StaticFiles(directory=DIST / 'assets'), name='assets')


@app.get('/{path:path}')
def spa(path: str):
    if path.startswith('api/') or path.endswith('.json'):
        raise HTTPException(404, '경로를 찾을 수 없습니다.')
    resolved = (DIST / path).resolve()
    if path and resolved.is_relative_to(DIST.resolve()) and resolved.is_file() and resolved.suffix in ('.png', '.svg', '.ico', '.woff2'):
        return FileResponse(resolved)
    if (DIST / 'index.html').is_file():
        return FileResponse(DIST / 'index.html', headers={'Cache-Control': 'no-cache'})
    raise HTTPException(404, '프론트엔드를 빌드하거나 개발 서버(5173)를 실행하세요.')
