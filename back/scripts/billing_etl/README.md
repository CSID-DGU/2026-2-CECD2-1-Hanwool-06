아리수 수도요금 청구서 수집과 계정의 고객번호 연결 진단 도구입니다.

- `crawl_bills.py`: 등록부의 활성 고객번호와 고지서상 성명으로 공개 요금 조회·상세를 수집합니다. 이름이 없는 호출에는 회원 조회 경로를 사용합니다.
- `i121_crawler/`: 현재 아리수 인증, 고객번호 확인, 목록·상세 파싱, 캐시 검증. 일일 검침 수집기에서도 인증 모듈을 사용합니다.
- `discover_mkeys.py`: 계정에 등록된 고객번호를 확인하는 운영 진단 CLI입니다.

수집 결과·캐시는 `APP_DATA_DIR/raw/billing_i121/`에 저장합니다. 기본 경로는 `data/runtime/raw/billing_i121/`이며, 웹 수집은 상세를 운영 DB에도 반영합니다. 관리 명령은 `python -m back.pipelines.refresh --bills`를 사용하세요.

예전 일회성 후처리 도구 `postprocess_bills.py`는 [`archive/back/scripts/billing_etl/`](../../../archive/back/scripts/billing_etl/)에 보관했습니다. `data/billing/`은 과거 정제 청구 자료와 현재 재사용하는 계량기 매핑을 유지합니다.
