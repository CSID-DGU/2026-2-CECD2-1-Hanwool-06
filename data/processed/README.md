`date_index.csv`만 둡니다. 날짜별 요일·주말·공휴일·휴무일·징검다리를 적은 달력이며, 화면의 날짜 정보와 모델의 달력 피처가 읽습니다.

과거 사용량·승하차·청구 집계 CSV는 2026-10-22부터 저장소가 아니라 운영 자료 폴더의 `history/processed/`에 있습니다([`data/README.md`](../README.md)). `daily_water_usage.csv`의 `검침일`은 아리수 원격검침을 읽은 날이라 검침일 D의 `일사용량_톤`은 D-1에 쓴 물이고, 앱과 모델은 읽을 때 하루를 뺍니다. 운영 갱신(`python -m back.pipelines.refresh`)은 그 이력과 새 관측을 합쳐 `APP_DATA_DIR/processed/`와 `APP_DATA_DIR/dataset/`에 결과를 저장합니다.
