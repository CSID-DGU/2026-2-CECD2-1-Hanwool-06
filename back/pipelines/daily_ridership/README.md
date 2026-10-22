서울교통공사 일별 승하차 인원 수집 파이프라인입니다.

등록부의 일일 수집 대상 역사에 대한 API 자료를 `APP_DATA_DIR/daily/ridership/<date>.csv`와 날짜별 상태 파일에 저장합니다. 기본 경로는 `data/runtime/daily/ridership/`입니다.

`SEOUL_PSGR_KEY`를 설정하고 `python -m back.pipelines.refresh --ridership`으로 갱신합니다. 날짜 범위와 모델 갱신은 [상위 문서](../README.md)를 참고하세요. 과거 월별 CSV 다운로드 도구는 [`archive/back/scripts/ridership_etl/`](../../../archive/back/scripts/ridership_etl/)에 보관했습니다.

일별 API는 최근 1주일치만 제공합니다. 그보다 오래된 날짜는 `python -m back.pipelines.daily_ridership.monthly 2026-05 2026-08`처럼 달을 지정해 서울 열린데이터광장의 월별 파일(OA-12914, 인증키 불필요)로 채웁니다. 결과는 같은 폴더에 같은 형식으로 저장하며, 기존 `data/processed/ridership.csv`와 같은 기준으로 집계합니다. 검토 매핑(`data/billing/meter_match.csv`)에 호선이 비어 있는 계량기는 같은 이름의 역 전체 합계를, 호선이 있는 계량기는 그 호선만 씁니다.
