"""Server-only settings; environment variables take precedence over .env."""
import os
from pathlib import Path
from urllib.parse import urlsplit
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
os.umask(0o077)  # Runtime database, collected observations and snapshots are private.
if os.getenv('APP_ENV') != 'production':
    load_dotenv(ROOT / '.env')
DATA_DIR = Path(os.getenv('APP_DATA_DIR') or ROOT / 'data/runtime').resolve()
APP_DB_PATH = Path(os.getenv('APP_DB_PATH') or DATA_DIR / 'app.sqlite3').resolve()
# 고객번호·역명·사용량이 든 과거 이력(모델 학습 이력, 계량기 매핑, 초기 자료, 옛 수집 폴더). 저장소에는 두지 않고 운영 자료 폴더 아래에 둔다.
# 폴더 구조는 옛 data/와 같다(history/processed, history/billing, history/app_seed, history/daily). 원본은 Object Storage의 history/.
HISTORY_DIR = Path(os.getenv('APP_HISTORY_DIR') or DATA_DIR / 'history').resolve()
SEED_DIR = HISTORY_DIR / 'app_seed'
APP_ENV = os.getenv('APP_ENV', 'development')
COOKIE_SECURE = os.getenv('APP_COOKIE_SECURE', str(APP_ENV == 'production')).lower() == 'true'
SESSION_HOURS = int(os.getenv('SESSION_HOURS', '8'))
ADMIN_EMAIL = os.getenv('ADMIN_EMAIL', '').strip().lower()
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', '')
ADMIN_NAME = os.getenv('ADMIN_NAME', '총괄 관리자').strip()
FRONT_ORIGINS = [v.strip() for v in os.getenv('APP_ORIGINS', 'http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:8000,http://localhost:8000').split(',') if v.strip()]
ALLOWED_HOSTS = [v.strip() for v in os.getenv('APP_ALLOWED_HOSTS', '127.0.0.1,localhost,testserver').split(',') if v.strip()]
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
OPENAI_MODEL = os.getenv('OPENAI_MODEL', 'gpt-5.5')
SMTP_HOST = os.getenv('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT = int(os.getenv('SMTP_PORT', '587'))
SMTP_USER = os.getenv('SMTP_USER', '')
SMTP_PASS = os.getenv('SMTP_PASS', '')
ALERT_FROM = os.getenv('ALERT_FROM', '') or SMTP_USER
TODAY_OVERRIDE = os.getenv('TODAY_OVERRIDE', '').strip()
COLLECTION_WORKER_ENABLED = os.getenv('COLLECTION_WORKER_ENABLED', 'true').lower() == 'true'
# 일일 수집 계량기의 고객번호가 아리수 계정에 없을 때 수집이 대신 등록하며 보내는 '사용자와의 관계' 코드(1 본인 … 9 관리인, 10 기타).
# 비우면(기본) 자동으로 등록하지 않는다. 아리수 고객번호등록 화면에서 사람이 고르던 값이라 코드가 정하지 않는다.
# 켜면 다른 아리수 회원에게 등록된 번호는 그 연결을 해제하고 등록한다(한 계량기에 한 번, 감사 기록에 남음).
ARISU_REGISTER_RELATION = os.getenv('ARISU_REGISTER_RELATION', '').strip()
# NCP Object Storage(S3 호환). 끄면(기본) 모든 보관 훅은 아무 일도 하지 않는다. 값은 환경변수로만 주고 코드에 적지 않는다.
OBJECT_STORAGE_ENABLED = os.getenv('OBJECT_STORAGE_ENABLED', 'false').lower() == 'true'
OBJECT_STORAGE_ENDPOINT = os.getenv('OBJECT_STORAGE_ENDPOINT', 'https://kr.object.ncloudstorage.com').strip()
OBJECT_STORAGE_REGION = os.getenv('OBJECT_STORAGE_REGION', 'kr-standard').strip()
OBJECT_STORAGE_BUCKET = os.getenv('OBJECT_STORAGE_BUCKET', '').strip()
OBJECT_STORAGE_ACCESS_KEY = os.getenv('OBJECT_STORAGE_ACCESS_KEY', '') or os.getenv('NCP_ACCESS_KEY', '')
OBJECT_STORAGE_SECRET_KEY = os.getenv('OBJECT_STORAGE_SECRET_KEY', '') or os.getenv('NCP_SECRET_KEY', '')
OBJECT_STORAGE_PREFIX = os.getenv('OBJECT_STORAGE_PREFIX', 'mulsaemi').strip()
OBJECT_STORAGE_ADDRESSING = os.getenv('OBJECT_STORAGE_ADDRESSING', 'path').strip().lower()
OBJECT_STORAGE_DB_BACKUP_KEEP = int(os.getenv('OBJECT_STORAGE_DB_BACKUP_KEEP', '30'))

def configured(value):
    return bool(value and not value.lower().startswith(('your_', 'sk-...')))

def openai_ready():
    return configured(OPENAI_API_KEY)

def smtp_ready():
    return configured(SMTP_USER) and configured(SMTP_PASS)


def validate_deployment():
    if APP_ENV not in ('development', 'production'):
        raise RuntimeError('APP_ENV는 development 또는 production이어야 합니다.')
    if not 1 <= SESSION_HOURS <= 24:
        raise RuntimeError('SESSION_HOURS는 1~24 사이여야 합니다.')
    if ARISU_REGISTER_RELATION:
        from back.scripts.billing_etl.i121_crawler.auth import RELATIONS   # the crawler reads this module, so not at the top
        if ARISU_REGISTER_RELATION not in RELATIONS:
            raise RuntimeError('ARISU_REGISTER_RELATION은 아리수 고객번호등록의 관계 코드 1~11 중 하나여야 합니다.')
    if OBJECT_STORAGE_ENABLED:
        if not (OBJECT_STORAGE_BUCKET and OBJECT_STORAGE_ACCESS_KEY and OBJECT_STORAGE_SECRET_KEY):
            raise RuntimeError('OBJECT_STORAGE_ENABLED=true 이면 OBJECT_STORAGE_BUCKET, OBJECT_STORAGE_ACCESS_KEY, OBJECT_STORAGE_SECRET_KEY 가 필요합니다.')
        if not OBJECT_STORAGE_ENDPOINT.startswith('https://'):
            raise RuntimeError('OBJECT_STORAGE_ENDPOINT 는 https 주소여야 합니다.')
        if OBJECT_STORAGE_ADDRESSING not in ('path', 'virtual'):
            raise RuntimeError('OBJECT_STORAGE_ADDRESSING 은 path 또는 virtual 이어야 합니다.')
        if OBJECT_STORAGE_DB_BACKUP_KEEP < 0:
            raise RuntimeError('OBJECT_STORAGE_DB_BACKUP_KEEP 은 0 이상이어야 합니다.')
    if APP_ENV != 'production':
        return
    if not COOKIE_SECURE:
        raise RuntimeError('운영 환경에서는 APP_COOKIE_SECURE=true가 필요합니다.')
    if TODAY_OVERRIDE:
        raise RuntimeError('운영 환경에서는 TODAY_OVERRIDE를 비워야 합니다.')
    if not FRONT_ORIGINS:
        raise RuntimeError('운영 APP_ORIGINS에는 공개 HTTPS 출처만 지정하세요.')
    for origin in FRONT_ORIGINS:
        url = urlsplit(origin)
        if (url.scheme != 'https' or not url.hostname or url.username or url.password
                or url.path or url.query or url.fragment):
            raise RuntimeError('운영 APP_ORIGINS에는 공개 HTTPS 출처만 지정하세요.')
    if not ALLOWED_HOSTS or any('*' in host or host == 'testserver' for host in ALLOWED_HOSTS):
        raise RuntimeError('운영 APP_ALLOWED_HOSTS에는 실제 호스트를 명시하세요.')
    if DATA_DIR.is_relative_to(ROOT) or APP_DB_PATH.is_relative_to(ROOT):
        raise RuntimeError('운영 데이터와 DB는 저장소 밖의 비공개 경로에 두세요.')
