# Object Storage 보관 모듈 (`back/storage`)

NCP Object Storage(S3 호환 API)에 `APP_DATA_DIR`의 장기 보관 파일을 복사하고, 운영 DB 백업을 올리며, 새 서버에서 같은 구조로 되돌리는 모듈입니다. 서버 구축과 독립적으로 동작하며, 환경변수만 넣으면 연동됩니다.

## 설계 원칙

- **로컬 파일이 작업 사본, Object Storage는 보관소**입니다. 화면·모델·수집기는 지금처럼 로컬 파일(mtime 기반 캐시 포함)을 읽습니다. 수집이나 모델 갱신이 끝난 뒤 바뀐 파일만 복사합니다.
- **운영 DB(SQLite)는 올리지 않습니다.** `backup-db`가 SQLite 백업 API로 만든 일관된 복사본만 `backups/sqlite/`에 둘 수 있고, 훅이 켜져 있으면 자동 수집이 마무리된 날 하루 한 번 자동으로도 올립니다. 운영 서버는 이 기능을 쓰지 않습니다(`deploy/README.md`). 채팅의 읽기 키가 같은 접두어를 읽어서 DB 사본이 그 키로 내려받히기 때문이며, DB는 03:30 백업이 별도 버킷에 둡니다.
- **어떤 명령도 로컬 파일을 지우지 않습니다.** `migrate`는 원격과 sha256이 일치한 파일만 "삭제 후보"로 보고서에 적습니다.
- **인증정보는 환경변수에서만 읽고** 로그·보고서·상태 응답에는 설정 여부만 남깁니다. 오류 메시지에는 오류 코드만 들어갑니다.
- **꺼져 있으면(기본값) 아무 일도 하지 않고**, 켜져 있어도 보관 훅은 예외를 밖으로 내지 않습니다. 복사 실패는 수집 결과에 영향을 주지 않고 다음 훅에서 다시 시도합니다.

## 환경변수

| 변수 | 필수 | 설명 |
|---|---|---|
| `OBJECT_STORAGE_ENABLED` | | `true`면 연동. 기본 `false` |
| `OBJECT_STORAGE_ENDPOINT` | | 기본 `https://kr.object.ncloudstorage.com` |
| `OBJECT_STORAGE_REGION` | | 기본 `kr-standard` |
| `OBJECT_STORAGE_BUCKET` | 켤 때 필수 | 버킷 이름 |
| `OBJECT_STORAGE_ACCESS_KEY` | 켤 때 필수 | NCP API 인증키 Access Key (`NCP_ACCESS_KEY`도 인식) |
| `OBJECT_STORAGE_SECRET_KEY` | 켤 때 필수 | NCP API 인증키 Secret Key (`NCP_SECRET_KEY`도 인식) |
| `OBJECT_STORAGE_PREFIX` | | 버킷 안 접두어(폴더). 기본 `mulsaemi` |
| `OBJECT_STORAGE_ADDRESSING` | | `path`(기본) 또는 `virtual` |
| `OBJECT_STORAGE_DB_BACKUP_KEEP` | | 남길 DB 백업 개수. 기본 30, 0이면 정리하지 않음 |

켜 두고 버킷·키가 비어 있으면 API가 시작 단계(`validate_deployment`)에서 멈춥니다.

## 객체 키 구조

키는 `<prefix>/<APP_DATA_DIR 기준 상대 경로>`입니다. 예: `mulsaemi/daily/water/2026-06-01.csv`. 객체 메타데이터 `sha256`에 내용 해시를 함께 저장해 다시 내려받지 않고도 무결성을 확인합니다.

| 자료 유형 | 로컬 위치 (`APP_DATA_DIR/`) | 복사 | 비고 |
|---|---|---|---|
| 운영 DB | `app.sqlite3`, `-wal`, `-shm`, `-journal`, `.app-backup.*` | ✗ | 백업만 `backups/sqlite/app-<UTC시각>.sqlite3` |
| 잠금·임시 파일 | `*.lock`, `.<name>.XXXXXX` | ✗ | |
| 수집 상태 | `collection/<고객번호>.json` | ✓ | 확인한 달·제외일 기록 |
| 일일 사용량 | `daily/water/<날짜>.csv`, `.status.json` | ✓ | 아리수 원본 관측 |
| 일별 승하차 | `daily/ridership/<날짜>.csv`, `.status.json` | ✓ | API는 최근 1주만 제공 |
| 청구서 원본 | `raw/billing_i121/i121_bills/` | ✓ | `bills_long.csv` 는 모델 청구 기준값 입력 |
| 아리수 페이지 캐시 | `raw/billing_i121/i121_cache/` | ✓ | 재조회 요청을 줄임 |
| 승하차 월별 원본 | `raw/ridership_monthly/` | ✓ | |
| 모델 입력 | `dataset/` | ✓ | train/valid 분할 경계가 여기 남음 |
| 가공 자료 | `processed/` | ✓ | |
| 이력 자료 | `history/` | ✓ | 모델 학습 이력(`processed/`), 계량기 매핑(`billing/`), 초기 자료(`app_seed/`), 옛 수집 폴더(`daily/`). 저장소에 두지 않아 버킷이 원본 |
| 모델 결과 | `model/` | ✓ | `model.txt`, `test_anomalies.csv`, `metrics.json` 등 |
| 스냅샷 | `snapshot/manifest.json`, manifest가 가리키는 `snapshot/versions/<id>/` | ✓ | 화면이 읽는 daily.json·risk.json |
| 지난 스냅샷 버전 | 그 밖의 `snapshot/versions/<id>/` | ✗ | 자료가 바뀔 때마다 전체를 다시 묶은 사본(버전마다 약 31MB, 서버에서는 하루 뒤 지워짐). 스냅샷이 바뀐 동기화에서, 지금 버전이 버킷에 그대로 있는지(크기·sha256) 확인한 뒤 버킷의 지난 버전도 지운다. `manifest.json`은 그 버전을 다 올린 뒤 마지막에 올린다 |
| 실행 보고서 | `refresh_report.json` | ✓ | |

로컬 색인 `.object-storage-index.json`(상대 경로 → sha256·크기·mtime)으로 바뀐 파일만 골라 올립니다. 색인이 없어도(새 서버) 원격 해시가 같으면 다시 올리지 않습니다.

주의: 분류되지 않은 파일은 "그 밖의 파일"로 **복사 대상**입니다. 서버의 별도 백업 작업이 `APP_DATA_DIR` 안에 `.bak`·`.gz` 같은 이름으로 DB 사본을 두면 그대로 올라가므로, 켜기 전에 `python -m back.storage plan`의 `upload` 목록을 확인하고 필요하면 `layout.py`에 제외 규칙을 추가합니다. `.sqlite3`·`.db`(및 `-wal`, `-shm`, `-journal`)로 끝나는 파일만 자동 제외됩니다.

## 자동으로 실행되는 시점

| 시점 | 호출 | 하는 일 |
|---|---|---|
| 웹 수집 작업 하나가 끝났을 때 | `collection._run_job` → `hooks.after_collection_job` | 바뀐 파일 복사(다른 동기화가 진행 중이면 건너뜀) |
| 자동 수집이 그날 마무리된 뒤 | `collection.worker` → `hooks.daily_maintenance` | 하루 한 번 DB 백업 업로드 + 복사. 감사 기록 `storage_db_backup` |
| `python -m back.pipelines.refresh …` 끝 | `refresh._run` → `hooks.after_refresh` | 모델 결과·스냅샷 복사 |
| `python -m back.pipelines.daily_ridership.monthly …` 끝 | `hooks.after_refresh` | 월별 원본·날짜별 파일 복사 |

## 명령

```sh
python -m back.storage status                 # 설정·연결 확인(비밀값은 설정 여부만)
python -m back.storage plan                   # 올릴/제외할 파일 목록(네트워크 없음)
python -m back.storage migrate --dry-run      # 최초 이전 계획
python -m back.storage migrate --verify-download   # 최초 이전: DB 백업 + 업로드 + 무결성 검증 + 보고서
python -m back.storage sync                   # 바뀐 파일만 올리기
python -m back.storage verify [--download]    # 로컬 ↔ 원격 대조
python -m back.storage backup-db [--keep N]   # 운영 DB 백업 올리기
python -m back.storage list-backups
python -m back.storage restore /private/data [--db]   # 새 서버로 내려받기(기존 파일은 덮어쓰지 않음)
python -m back.storage selftest               # 수집 종료 훅·DB 백업 훅이 실제 버킷에 올리는지 격리 검증(아래 설명)
python -m back.api.manage backup /private/backup/water.sqlite3 --upload   # 기존 백업 명령 + 업로드
```

`migrate` 보고서는 `APP_DATA_DIR/.object-storage-migration-<UTC시각>.json`에 남습니다. `delete_candidates`가 삭제 후보, `keep_local`이 남겨야 하는 파일입니다. 실제 삭제는 별도 승인 후 사람이 수행합니다.

## 새 서버로 옮길 때

1. 코드 체크아웃, 의존성 설치, 환경변수 설정(`OBJECT_STORAGE_*`, 기존 `APP_*`).
2. `python -m back.storage restore $APP_DATA_DIR --db` 로 자료와 최신 DB 백업을 내려받습니다.
3. API를 시작합니다. 화면·모델·수집이 이전 서버와 같은 파일을 봅니다.

## `selftest`: 자동 업로드 End-to-End 격리 검증

운영 자료를 건드리지 않고 "수집 작업 종료 → `hooks.after_collection_job` → 실제 버킷 업로드"가 동작하는지 확인합니다. 임시 `APP_DATA_DIR`와 임시 DB를 만들고, 수집기를 가짜로 바꿔 민감정보 없는 파일 3개(수집 상태 JSON, 일일 사용량 CSV, 상태 JSON)를 쓴 뒤 실제 `collection.run_job`을 돌립니다. 업로드는 `<prefix>/_auto-sync-test/<실행ID>/` 아래에만 일어나고, 크기·sha256·다운로드 검증, 재실행 시 중복 업로드 없음, `daily_maintenance`의 임시 DB 백업, `after_refresh`까지 확인한 뒤 그 접두어의 객체만 지웁니다. 아리수나 외부 API는 호출하지 않습니다. 서버에 배포한 직후 환경변수와 네트워크가 맞는지 확인하는 용도입니다. 2026-10-18 로컬 PC에서 실제 NCP 버킷을 대상으로 통과했습니다.

## 실환경에서 확인된 사항 (2026-10-17, 로컬 PC → NCP)

- `head_bucket`·목록·`get_bucket_acl`·멀티파트 시작은 되는데 `put_object`만 `AccessDenied`가 나면 권한 문제가 아니라 **boto3 1.36 이상의 기본 체크섬 트레일러(`aws-chunked` + CRC32)** 때문입니다. NCP가 이 요청을 거절합니다. `client.py`가 `request_checksum_calculation='when_required'`로 꺼 두므로 이 모듈을 쓰면 발생하지 않습니다. 다른 스크립트에서 boto3를 직접 쓸 때는 같은 옵션이 필요합니다.
- `path`·`virtual` 주소 방식 모두 동작했습니다. 기본값 `path`를 유지합니다.
- 12MB 파일(멀티파트 임계 8MB 초과)의 업로드·다운로드·sha256 일치를 확인했습니다.

## 테스트

`python -m unittest tests.test_storage` — 가짜 S3 클라이언트로 키 배치, 동기화 멱등성, 실패 격리, DB 백업·정리, 복원, 마이그레이션 검증을 확인합니다. 실제 버킷 연결은 서버 구축 뒤 `status`·`migrate --dry-run`으로 확인합니다.
