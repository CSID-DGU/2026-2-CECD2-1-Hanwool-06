`build_clean_dataset.py`에는 운영에 쓰는 청구 집계 함수 `build_bills_bimonthly`만 남겼습니다. 이 함수는 현재 `back.pipelines.refresh`에서 청구 데이터를 모델 입력으로 집계할 때 사용하므로 운영 코드로 유지합니다.

운영 갱신은 `python -m back.pipelines.refresh`를 사용합니다. 갱신한 정제 자료·학습 자료는 `APP_DATA_DIR/processed/`와 `APP_DATA_DIR/dataset/`에 저장하며, 기존 `data/processed/` 자료를 재사용합니다.

과거 일일 CSV 병합기 `build_daily_usage_long.py`와 고정 범위 달력 생성기 `build_date_index.py`는 [`archive/back/scripts/dataset_etl/`](../../../archive/back/scripts/dataset_etl/)에 보관했습니다. 현재 날짜 범위의 달력은 `refresh.build_calendar`가 생성합니다.

이전 일괄 변환 CLI와 일일·승하차 변환 함수는 [`archive/back/scripts/dataset_etl/build_clean_dataset.py`](../../../archive/back/scripts/dataset_etl/build_clean_dataset.py)에 보관했습니다. 기본 입력이 없는 과거 CLI로 현재 정제 자료를 덮어쓰지 않습니다.
