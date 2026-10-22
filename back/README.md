# 백엔드

설치와 실행은 루트 `README.md`에, 운영 서버 설정은 [`../deploy/README.md`](../deploy/README.md)에 있습니다.

- `api/`: FastAPI, SQLite 등록부·계정·세션, 보호된 자료 조회, PDF/Excel, 분석·메일.
- `pipelines/`: 아리수·승하차 수집과 모델 실행. `python -m back.pipelines.refresh --help`.
- `storage/`: NCP Object Storage(S3 호환) 보관·백업·복원·마이그레이션. `python -m back.storage --help`. 설명은 [`storage/README.md`](storage/README.md).
- `scripts/`: 현재 청구서 수집·연결 진단 도구와 운영 파이프라인에서 공유하는 ETL 함수.
- `ml/lightgbm/`: 현재 사용하는 CPU 학습·검증·이상 탐지 코드.

운영 API와 수집기는 같은 비공개 `APP_DATA_DIR`와 SQLite DB를 사용합니다.
수집 원본·완성 스냅샷은 웹 정적 폴더나 데이터 Git 브랜치에 게시하지 않습니다.

이전 분석·크롤러·ETL 및 저장된 실험 결과는 [`archive/`](../archive/README.md)에 있습니다.
현재 `ml/lightgbm/` 모델 코드와 `scripts/dataset_etl/build_clean_dataset.py`의 청구 집계 함수는 계속 사용합니다.

## 운영 설정과 관리 명령

운영 배포에서는 `APP_ENV=production`, `APP_COOKIE_SECURE=true`, `APP_ORIGINS`, `APP_ALLOWED_HOSTS`, `APP_DATA_DIR`를 실제 값으로 두고 HTTPS 프록시 뒤에서 8000번은 로컬에만 엽니다. DB는 SQLite 전용이라 RDS(PostgreSQL)로 바로 옮길 수 없습니다. 공개 회원가입은 없고 담당자 계정은 관리자가 관리 화면에서 만듭니다.

```sh
python -m back.api.manage backup /private/backup/water.sqlite3    # 실행 중에도 안전한 SQLite 백업 (--upload: Object Storage에도 올림)
python -m back.api.manage reset-password admin@example.com        # 비밀번호 재설정(터미널에서 입력, 다음 로그인 때 변경 요구)
```

`APP_DATA_DIR`(기본 `data/runtime/`)에는 계정·세션·수집 자료가 있으니 외부에 노출하지 않습니다.
