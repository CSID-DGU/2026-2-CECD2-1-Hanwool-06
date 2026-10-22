현재 수집과 자료 갱신에서 사용하는 도구를 모아둔 폴더입니다.

- `billing_etl/`: 아리수 청구서 수집과 계정의 고객번호 연결 진단.
- `dataset_etl/build_clean_dataset.py`: 청구 집계 등 공유 ETL 함수. `back.pipelines.refresh`가 `build_bills_bimonthly`를 사용합니다.

운영 자료 갱신은 웹 정보 업데이트 또는 `python -m back.pipelines.refresh`로 실행합니다. 이전 이력 수집기·승하차 ETL·일회성 전처리 도구는 [`archive/`](../../archive/README.md)에 보관했습니다.
