"""Small persistent registry. Observations remain in the existing data pipeline."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from . import config


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


@contextmanager
def connect():
    conn = sqlite3.connect(config.APP_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db():
    config.APP_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript('''
        CREATE TABLE IF NOT EXISTS offices(id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE);
        CREATE TABLE IF NOT EXISTS stations(id TEXT PRIMARY KEY,name TEXT NOT NULL,map_x REAL,map_y REAL);
        CREATE TABLE IF NOT EXISTS meters(
          id TEXT PRIMARY KEY,provider TEXT NOT NULL,customer_number TEXT NOT NULL,
          station_id TEXT NOT NULL REFERENCES stations(id),office_id TEXT NOT NULL REFERENCES offices(id),
          line TEXT,display_name TEXT NOT NULL,purpose TEXT NOT NULL DEFAULT '',tariff TEXT NOT NULL DEFAULT '',
          address TEXT NOT NULL DEFAULT '',active INTEGER NOT NULL DEFAULT 1,daily_enabled INTEGER NOT NULL DEFAULT 0,
          metadata TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
          UNIQUE(provider,customer_number));
        CREATE TABLE IF NOT EXISTS bills(
          id TEXT PRIMARY KEY,meter_id TEXT NOT NULL REFERENCES meters(id),period TEXT NOT NULL,
          gubun TEXT NOT NULL DEFAULT '정기분',payload TEXT NOT NULL,source TEXT NOT NULL,
          notice_number TEXT NOT NULL DEFAULT '',UNIQUE(meter_id,period,gubun,notice_number));
        CREATE INDEX IF NOT EXISTS bills_meter ON bills(meter_id,period);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY,email TEXT NOT NULL UNIQUE,name TEXT NOT NULL,password_hash TEXT NOT NULL,
          role TEXT NOT NULL CHECK(role IN ('superadmin','office_admin')),active INTEGER NOT NULL DEFAULT 1,
          must_change_password INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS office_members(user_id INTEGER NOT NULL REFERENCES users(id),
          office_id TEXT NOT NULL REFERENCES offices(id),PRIMARY KEY(user_id,office_id));
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),
          csrf_token TEXT NOT NULL,expires_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS login_attempts(id INTEGER PRIMARY KEY,attempted_at REAL NOT NULL,identity TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS login_attempt_identity ON login_attempts(identity,attempted_at);
        CREATE TABLE IF NOT EXISTS audit_log(id INTEGER PRIMARY KEY,actor_id INTEGER REFERENCES users(id),
          action TEXT NOT NULL,target TEXT NOT NULL,details TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS audit_actor_action_time ON audit_log(actor_id,action,created_at);
        CREATE TABLE IF NOT EXISTS chat_conversations(
          id TEXT PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id),
          scope_fingerprint TEXT NOT NULL,context TEXT NOT NULL DEFAULT '{}',messages TEXT NOT NULL DEFAULT '[]',
          lease_token TEXT,lease_until REAL NOT NULL DEFAULT 0,updated_at REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS chat_owner_updated ON chat_conversations(user_id,updated_at);
        CREATE TABLE IF NOT EXISTS collection_jobs(
          id INTEGER PRIMARY KEY,status TEXT NOT NULL,trigger TEXT NOT NULL,actor_id INTEGER REFERENCES users(id),
          created_at TEXT NOT NULL,started_at TEXT,finished_at TEXT);
        CREATE UNIQUE INDEX IF NOT EXISTS one_running_collection ON collection_jobs(status) WHERE status='running';
        CREATE TABLE IF NOT EXISTS collection_items(
          job_id INTEGER NOT NULL REFERENCES collection_jobs(id),meter_id TEXT NOT NULL REFERENCES meters(id),
          status TEXT NOT NULL DEFAULT 'queued',message TEXT NOT NULL DEFAULT '',started_at TEXT,finished_at TEXT,
          daily_rows INTEGER NOT NULL DEFAULT 0,bill_rows INTEGER NOT NULL DEFAULT 0,
          connection_verified INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(job_id,meter_id));
        CREATE INDEX IF NOT EXISTS collection_meter ON collection_items(meter_id,job_id);
        ''')
        if 'deleted_at' not in {r['name'] for r in c.execute('PRAGMA table_info(meters)')}:
            c.execute('ALTER TABLE meters ADD COLUMN deleted_at TEXT')
        if 'deleted_at' not in {r['name'] for r in c.execute('PRAGMA table_info(users)')}:
            c.execute('ALTER TABLE users ADD COLUMN deleted_at TEXT')
        if 'retryable' not in {r['name'] for r in c.execute('PRAGMA table_info(collection_items)')}:
            c.execute('ALTER TABLE collection_items ADD COLUMN retryable INTEGER NOT NULL DEFAULT 0')
        if 'notice_number' not in {r['name'] for r in c.execute('PRAGMA table_info(bills)')}:
            c.execute('BEGIN IMMEDIATE')
            c.execute('''CREATE TABLE bills_with_notices(
                id TEXT PRIMARY KEY,meter_id TEXT NOT NULL REFERENCES meters(id),period TEXT NOT NULL,
                gubun TEXT NOT NULL DEFAULT '정기분',payload TEXT NOT NULL,source TEXT NOT NULL,
                notice_number TEXT NOT NULL DEFAULT '',UNIQUE(meter_id,period,gubun,notice_number))''')
            c.execute('INSERT INTO bills_with_notices(id,meter_id,period,gubun,payload,source) SELECT id,meter_id,period,gubun,payload,source FROM bills')
            c.execute('DROP TABLE bills')
            c.execute('ALTER TABLE bills_with_notices RENAME TO bills')
            c.execute('CREATE INDEX bills_meter ON bills(meter_id,period)')
    config.APP_DB_PATH.chmod(0o600)


def audit(c, actor_id, action, target='', details=None):
    c.execute('INSERT INTO audit_log(actor_id,action,target,details,created_at) VALUES(?,?,?,?,?)',
              (actor_id, action, str(target), json.dumps(details or {}, ensure_ascii=False), now()))
