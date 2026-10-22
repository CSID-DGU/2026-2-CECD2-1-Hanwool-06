# NCP 서버 담당자 인수인계: Object Storage 연동

작성 기준: 2026-10-18. 이 문서는 **Object Storage 보관 기능만** 다룹니다. 같은 저장소에서 작업 중인 관리자 설정 저장 기능(`settings_store`, `APP_SETTINGS_KEY`, `/api/settings/agent`)은 **이번 커밋·배포에 포함되지 않으며** 별도로 안내합니다. 실제 비밀값은 이 문서나 저장소에 넣지 않고 변수 이름만 적습니다.

## 0. 현재 상태 요약

| 항목 | 상태 |
|---|---|
| 코드 | `back/storage/` 모듈과 수집·모델 코드의 훅 호출. 로컬 PC에서 실제 NCP 버킷에 대해 연결·업로드·다운로드·sha256 검증·격리 셀프테스트 통과 |
| 서버 | 현재 배포 커밋 `ea6f27f`에는 이 기능이 없음. 스토리지 커밋이 `main`에 올라간 뒤 반영 필요 |
| 버킷 | 생성 완료, 비공개. `mulsaemi/collection/` 아래에 수집 상태 JSON 81개(13.8KB)가 로컬 PC에서 올린 상태로 있음. DB 백업은 아직 없음 |
| 기존 03:30 DB 백업 | 서버에서 별도로 동작 중. **이번 배포에서 바꾸지 않음** (6절) |
| 08:00 자동 수집·재시도·실패 메일 | 저장소 코드 그대로. 스토리지 변경은 스케줄 코드를 건드리지 않음 |

## 1. 버킷 요구사항 (완료 여부 확인용)

| 항목 | 요청 사항 |
|---|---|
| 버킷 | 1개, 비공개(퍼블릭 액세스 없음). 저장 시 암호화 옵션 권장 |
| 리전·엔드포인트 | `kr-standard`, `https://kr.object.ncloudstorage.com` (기본값) |
| 접근 권한 | 서버용 API 인증키 한 쌍. 이 버킷에 읽기·쓰기·목록·삭제(오래된 백업 정리용) 필요 |
| 폴더 구조 | 코드가 `mulsaemi/` 접두어 아래에 만듭니다. 구조는 `back/storage/README.md` 표 참고 |
| 네트워크 | 서버 → 엔드포인트 **아웃바운드 HTTPS(443)** 허용. 인바운드 변경 없음 |

보관 자료에 고객번호·주소·청구 내역이 들어가므로 버킷과 콘솔 접근 계정을 제한해 주세요.

## 2. 환경변수 (이번 배포에서 새로 추가되는 것)

운영(`APP_ENV=production`)에서는 코드가 `.env`를 읽지 않습니다. systemd `EnvironmentFile` 등 **프로세스 환경변수**로 넣어 주세요. 비밀값은 환경변수로만 전달하고 로그·문서에 남기지 않습니다.

| 변수 | 값 | 비고 |
|---|---|---|
| `OBJECT_STORAGE_ENABLED` | **처음에는 `false`** | `true`가 되면 수집·모델 갱신 뒤 복사와 하루 1회 DB 백업이 시작됩니다. 5절의 검토가 끝난 뒤 켭니다 |
| `OBJECT_STORAGE_BUCKET` | 버킷 이름 | |
| `OBJECT_STORAGE_ACCESS_KEY` | NCP API Access Key | `NCP_ACCESS_KEY` 이름도 인식 |
| `OBJECT_STORAGE_SECRET_KEY` | NCP API Secret Key | `NCP_SECRET_KEY` 이름도 인식 |
| `OBJECT_STORAGE_ENDPOINT` | (생략 가능) | 기본 `https://kr.object.ncloudstorage.com` |
| `OBJECT_STORAGE_REGION` | (생략 가능) | 기본 `kr-standard` |
| `OBJECT_STORAGE_PREFIX` | (생략 가능) | 기본 `mulsaemi`. 로컬 PC가 올린 81개 객체와 같은 접두어 |
| `OBJECT_STORAGE_ADDRESSING` | (생략 가능) | `path` 기본. `virtual`도 동작 확인됨 |
| `OBJECT_STORAGE_DB_BACKUP_KEEP` | (생략 가능) | 버킷에 남길 DB 백업 개수. 기본 30, 0이면 정리하지 않음 |

기존 변수(`APP_*`, `COLLECTION_WORKER_ENABLED`, `ARISU_*`, `OPENAI_*`, `SMTP_*`, `SEOUL_PSGR_KEY`)는 변경이 없습니다. `APP_SETTINGS_KEY`는 이번 배포 대상이 아니므로 **설정하지 않습니다.**

API 시작 시 검증: `OBJECT_STORAGE_ENABLED=true`인데 버킷·키가 비어 있거나, 엔드포인트가 https가 아니거나, `OBJECT_STORAGE_ADDRESSING`이 `path`/`virtual`이 아니면 API가 시작 단계에서 멈춥니다. `false`이면 스토리지 관련 검증은 건너뜁니다.

## 3. 의존성

`back/api/requirements.txt`에 `boto3>=1.34,<2`가 추가됩니다. `pip install -r requirements-dev.txt`(또는 `back/api/requirements.txt`)를 다시 실행해 주세요. 그 밖의 요구사항(LightGBM용 OpenMP 등)은 그대로입니다.

알아둘 점: boto3 1.36 이상은 업로드마다 CRC32 체크섬 트레일러를 붙이는데 NCP는 이를 `AccessDenied`로 거절합니다(권한 문제처럼 보임). `back/storage/client.py`가 이 기능을 꺼 두므로 모듈을 통해 올리면 문제가 없고, 서버에서 boto3나 AWS CLI를 직접 쓸 때만 `request_checksum_calculation=when_required` 설정이 필요합니다.

## 4. 이번 커밋에 들어가는 변경

| 위치 | 내용 |
|---|---|
| `back/storage/` | 클라이언트(`client.py`), 키 배치(`layout.py`), 동기화·백업·복원·마이그레이션(`sync.py`), 운영 훅(`hooks.py`), 격리 셀프테스트(`selftest.py`), CLI(`python -m back.storage`) |
| `back/locking.py` | 모듈이 쓰는 파일 잠금 도우미(Linux `fcntl`, Windows `msvcrt`). 기존 수집 코드는 바꾸지 않음 |
| `back/api/collection.py` | 수집 작업 종료 후 `hooks.after_collection_job`, 큐가 비었을 때 `hooks.daily_maintenance` 호출 **7줄 추가**. 스케줄(`DAILY_HOUR = 8`), 재시도, 메일 로직은 그대로 |
| `back/pipelines/refresh.py`, `daily_ridership/monthly.py` | 실행 끝에 `hooks.after_refresh` 호출 |
| `back/api/config.py` | `OBJECT_STORAGE_*` 환경변수 읽기와 시작 시 검증 |
| `back/api/manage.py` | 기존 `backup` 명령에 `--upload` 옵션(버킷에도 올림) |
| `back/api/requirements.txt`, `.env.example`, README 들 | 의존성·양식·설명 |
| `tests/test_storage.py` | 가짜 S3 클라이언트 단위 테스트 |

DB 스키마 변경은 없습니다. 꺼져 있을 때(`OBJECT_STORAGE_ENABLED=false`) 훅은 호출만 되고 아무 일도 하지 않으며, 켜져 있어도 복사 실패는 로그에만 남고 수집 결과에는 영향을 주지 않습니다.

## 5. 서버 배포와 Test B 절차

### 5-1. 배포 (서비스 영향: API 재시작 1회)

1. 스토리지 커밋이 `main`에 올라간 뒤 서버에서 `git pull`. `git log -1`로 커밋을 확인하고 `python -c "import back.storage, back.api.main"`이 되는지 확인합니다.
2. `pip install -r requirements-dev.txt`.
3. API 서비스 환경에 2절의 변수를 넣되 **`OBJECT_STORAGE_ENABLED=false`**로 둡니다(버킷·키는 넣어 두어도 되고 비워도 됩니다).
4. API 재시작(시점 협의). 이 상태에서는 기존과 완전히 같이 동작하며 버킷에 아무것도 올리지 않습니다.

### 5-2. 연결·범위 확인 (운영 서비스는 그대로 `false`)

`status`와 `plan`은 꺼진 상태에서도 실행되지만 `status`는 연결을 시도하지 않고 설정만 보여줍니다. 실제 연결 확인(`status`의 `reachable`, `selftest`)에는 `OBJECT_STORAGE_ENABLED=true`가 필요하므로 **CLI 프로세스에만** 변수를 넘겨 실행합니다. API 서비스의 환경은 바꾸지 않습니다.

```sh
# 스토리지 변수만 담은 파일(예: /etc/mulsaemi/storage.env, 권한 600)을 이 셸에만 적용
set -a; . /etc/mulsaemi/storage.env; set +a      # OBJECT_STORAGE_ENABLED=true 포함, 비밀값은 화면에 출력하지 않음
export APP_ENV=production APP_DATA_DIR=<운영 자료 경로> APP_DB_PATH=<운영 DB 경로>   # API 와 같은 값

python -m back.storage status      # "reachable": true 확인. 비밀값은 출력되지 않음
python -m back.storage plan        # 올릴 파일(upload)과 제외 파일(excluded) 목록, 유형별 파일 수·용량. 네트워크 없음
python -m back.storage selftest    # 임시 자료·임시 DB·접두어 mulsaemi/_auto-sync-test/<ID>/ 에서 수집 종료 훅 → 업로드 → 검증 → 정리. 마지막 줄 SELFTEST PASS
```

`selftest`는 운영 `APP_DATA_DIR`·DB를 읽지도 쓰지도 않고, 아리수나 외부 API를 호출하지 않으며, 끝나면 자기 접두어의 객체만 지웁니다. 로컬 PC에서 실제 버킷으로 2026-10-18 통과했습니다.

### 5-3. 활성화 전 반드시 검토할 것

- `plan` 결과의 `upload` 목록을 봅니다. 훅은 `APP_DATA_DIR` 전체에서 바뀐 파일을 올리므로, 켠 뒤 **첫 수집 작업이 끝나면 기존 파일 전체**(`daily/`, `snapshot/`, `raw/`, `model/`, `dataset/`, `processed/`, `collection/`)가 한 번에 올라갑니다. 파일 수와 용량을 확인하고 승인받습니다.
- `raw/billing_i121/`에는 주소·고지서상 성명이 포함된 청구서 원본이 들어갑니다. 버킷 보관이 승인된 범위인지 확인합니다.
- 6절의 03:30 백업 파일이 `plan`에서 `upload`로 나오면 켜기 전에 백엔드 담당자에게 알려 제외 규칙을 추가합니다.
- 필요하면 전체를 한 번에 올리는 대신 `python -m back.storage migrate --dry-run` → `migrate --verify-download`로 먼저 올려 보고서(`APP_DATA_DIR/.object-storage-migration-<시각>.json`)로 검증한 뒤 켭니다. 어느 쪽이든 **로컬 파일은 지우지 않습니다.**

### 5-4. 활성화와 Test B

1. API 서비스 환경에서 `OBJECT_STORAGE_ENABLED=true`로 바꾸고 API 재시작(시점 협의).
2. 관리 화면에서 **계량기 1개**만 "정보 업데이트"를 눌러 수동 수집 작업을 실행합니다(작업 단위가 계량기별이라 1개로 제한 가능).
3. 작업이 끝나면 서버에서 `python -m back.storage verify`로 로컬 파일과 버킷 객체의 크기·sha256을 대조합니다(`mismatch: 0`). NCP 콘솔에서 `mulsaemi/collection/`, `mulsaemi/daily/water/`, `mulsaemi/snapshot/` 아래 객체의 갱신 시각을 확인합니다. 첫 활성화라면 이때 기존 파일 전체가 함께 올라갑니다(5-3).
4. 다음 날 08:00 자동 수집 뒤 `mulsaemi/backups/sqlite/app-<UTC시각>.sqlite3`가 생기는지, `audit_log`에 `storage_db_backup`이 남는지, 로그에 `Object storage sync ... failed`가 없는지 확인합니다.
5. (선택) 임시 디렉터리에 `python -m back.storage restore /tmp/restore-test --db`로 복원이 되는지 확인 후 삭제.

## 6. 기존 03:30 DB 백업 (변경 없음)

서버에서 매일 03:30에 실행되는 기존 DB 백업은 저장소 코드가 아니라 서버 쪽 설정(cron 또는 systemd timer로 추정)입니다. **이번 배포에서 수정·비활성화하지 않습니다.** 다만 Object Storage 동기화와 겹치는 지점이 있어 활성화 전에 다음을 확인해 주세요.

1. 실제 백업 실행 명령과 시각: `crontab -l`, `sudo ls /etc/cron.d`, `systemctl list-timers --all | grep -iE "backup|mulsaemi|water"`.
2. 백업 파일의 저장 경로와 확장자: `ls -la <백업 디렉터리>`.
3. 그 경로가 `APP_DATA_DIR` **안**인지 **밖**인지.
   - 밖이면 동기화와 무관합니다.
   - 안이면 파일명이 `.sqlite3` 또는 `.db`로 끝날 때만 DB로 분류돼 자동 제외됩니다. **`.bak`, `.gz`, `.tar`, 날짜만 붙은 이름 등은 "기타" 파일로 분류돼 켜는 순간 버킷에 올라갑니다.** DB 사본에는 개인정보가 들어가므로 `plan` 결과에서 해당 파일이 `excluded`에 있는지 확인하고, `upload`에 있으면 켜기 전에 제외 규칙을 추가해야 합니다.
4. 중복 여부: 패치의 `daily_maintenance`는 08:00 자동 수집이 마무리된 뒤 하루 한 번 DB 백업을 버킷 `backups/sqlite/`에 올립니다(기본 최근 30개 유지). 03:30 로컬 백업과는 독립적이라 둘이 공존해도 충돌은 없습니다. 선택지는 (a) 둘 다 유지, (b) 03:30 명령에 `--upload`를 붙여 그 백업도 버킷에 올리기(`python -m back.api.manage backup <경로> --upload`, CLI 환경에 스토리지 변수 필요), (c) 하나로 통합. **통합·삭제 여부는 운영 검증이 끝난 뒤 별도로 결정**하고, 그때까지는 기존 백업을 그대로 둡니다.

## 7. 서버 교체·재배포 시

새 서버에서 코드 체크아웃 → 의존성 설치 → 환경변수 설정 → `python -m back.storage restore $APP_DATA_DIR --db` → API 시작. 이전 서버의 수집 자료·스냅샷·모델 결과와 가장 최근 DB 백업이 같은 경로로 복원됩니다. 기존 파일이 있으면 덮어쓰지 않습니다.

## 8. 주의 사항

- **디스크 사용량**: 로컬 `APP_DATA_DIR`는 그대로 쓰므로 서버 디스크 요구량은 줄지 않습니다. 버킷은 추가 사본입니다.
- **아웃바운드 방화벽**: 막히면 수집은 정상 동작하고 복사만 실패하며 로그에 `Object storage sync ... failed (...)`가 남습니다. 다음 작업 때 다시 시도합니다.
- **동일 DB에 여러 서버**: 한 설치에 하나의 `APP_DATA_DIR`·DB만 쓰세요. 서버를 나누면 `OBJECT_STORAGE_PREFIX`를 다르게 둡니다.
- **GitHub Actions self-hosted 워크플로(`.github/workflows/daily.yml`)**: 수동 실행 전용이며 수정하지 않았습니다. 그 러너에 스토리지 변수가 없으면(기본 꺼짐) 복사만 건너뜁니다.
- **키 교체**: Object Storage 인증키를 바꾸면 환경변수만 교체하고 API를 재시작합니다.
- **삭제**: 버킷에서 코드가 지우는 것은 `backups/sqlite/`의 오래된 백업(기본 30개 초과분), 지난 스냅샷 버전(`snapshot/versions/` 중 화면이 지금 읽는 버전이 아닌 것. 지금 버전이 버킷에 그대로 있는지 크기·sha256으로 확인한 뒤에만 지움), `selftest` 자신의 임시 접두어뿐입니다. 그 밖의 객체와 로컬 파일 삭제는 사람이 승인 후 수행합니다.
- **운영 중 `.env`**: 운영 모드는 `.env`를 읽지 않습니다. 개발 PC에서만 사용합니다.

## 9. 별도 배포 예정 (이번 커밋 미포함)

총괄 관리자가 화면에서 OpenAI·메일·아리수·승하차 API 설정을 바꾸는 기능(`back/api/settings_store.py`, `/api/settings/agent`, `APP_SETTINGS_KEY`, `cryptography` 의존성)은 백엔드 로컬에 구현되어 있지만 이번 커밋에 넣지 않았습니다. 배포 시점이 되면 환경변수와 절차를 따로 전달합니다. 현재 서버는 기존처럼 환경변수만 사용합니다.

## 10. 백엔드가 하지 않은 것

NCP Server 생성·네트워크·방화벽·프로세스 관리(systemd 등)·배포 스크립트·03:30 백업 설정·운영 서버 환경변수 변경. 위 환경변수와 명령만 반영해 주시면 됩니다.
