# 프로젝트 자료

| 경로 | 용도 |
|---|---|
| `processed/date_index.csv` | 달력(요일·주말·공휴일·휴무일·징검다리). 화면과 모델이 읽는다 |
| `raw/` | 옛 원자료 입력·검토 자료가 있던 위치 설명 |
| `runtime/` | 운영 DB·수집 원본·갱신한 자료·모델 결과·스냅샷의 기본 저장소(`APP_DATA_DIR`, git 제외) |

고객번호·역명·사용량·승하차·청구 이력이 든 자료는 2026-10-22부터 저장소에 두지 않습니다. 운영 자료 폴더의 `history/`(`APP_DATA_DIR/history`, `APP_HISTORY_DIR`로 바꿀 수 있음)에 두고, 14:00 보관(`back/storage sync`)이 Object Storage `<prefix>/history/`에 올리며, 새 서버는 `python -m back.storage restore <APP_DATA_DIR>`로 되살립니다. 등록부와 청구서는 운영 DB와 03:30 백업 버킷에만 있습니다.

## `history/` 구조 (옛 `data/`와 같음)

| 경로 | 내용 | 읽는 곳 |
|---|---|---|
| `history/processed/daily_water_usage.csv` | 고객번호, 역명, 검침일, 일사용량_톤. 검침일 D의 값은 D-1에 쓴 물이라 읽을 때 하루 당긴다 | 모델 자료 구성(`refresh.build_dataset`), 화면 예비 자료 |
| `history/processed/ridership.csv` | 고객번호, 역명, 사용일, 승차·하차 총승객수 | 같음 |
| `history/processed/water_bills.csv` | 격월 청구 집계(고객번호, 역명, 시작·종료연월, 사용량, 부과금액). 수집된 청구서가 없을 때의 모델 기준값 | `refresh --model` |
| `history/ml_dataset/master.csv` | 옛 통합 자료(고객번호, 역명, 날짜, 사용량, 승하차). 운영 스냅샷이 없을 때 화면이 읽는 예비 자료 | 화면 예비 자료 |
| `history/billing/bills_clean.csv` | 정기분 청구서 통합 이력(고객번호, 역명, 영업사업소, 용도, 납기일, 총사용량_톤, 부과금액_원). 성명 열 없음 | `refresh.build_model_bills` |
| `history/billing/meter_match.csv` | 고객번호 ↔ 일일CSV파일명·역명·호선·용도 매핑(80행). 호선이 빈 계량기는 같은 이름 역 전체 합계를 쓴다 | 승하차 수집, 모델 자료 구성, 등록부 초기 적재 |
| `history/app_seed/stations.json`, `locations.json`, `station_positions.json` | 고객번호별 역명·사업소·호선, 계량기 좌표, 역 노선도 위치 | 등록부 초기 적재, 매 시작 때 비어 있는 역 위치 채움 |
| `history/app_seed/risk/test_anomalies.csv` | 초기 위험도 자료. 운영 스냅샷이 게시되면 쓰지 않는다 | 화면 예비 자료 |
| `history/daily/` | 서버의 옛 수집 폴더(수집기가 쓰기 전 자료) | 모델 자료 구성 |

로컬에서 모델을 돌리거나 등록부를 채우려면 이 폴더가 필요합니다. 버킷에서 받으려면 읽기 키로 `OBJECT_STORAGE_ENABLED=true OBJECT_STORAGE_BUCKET=hanwool-data OBJECT_STORAGE_PREFIX=prod OBJECT_STORAGE_ACCESS_KEY=… OBJECT_STORAGE_SECRET_KEY=… python -m back.storage restore data/runtime`을 실행합니다(이미 있는 파일은 덮어쓰지 않음). 없으면 화면은 스냅샷만 보여 주고, `refresh --model`은 이력이 없다는 오류로 멈춥니다.
