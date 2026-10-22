아리수 일별 상수도 사용량 수집 파이프라인입니다. 등록부의 일일 수집 대상에 대해 과거 확정 검침과 최근 미결산 검침을 합쳐 수집합니다.

결과는 `APP_DATA_DIR/daily/water/<date>.csv`와 날짜별 상태 파일에 저장합니다. 기본 경로는 `data/runtime/daily/water/`입니다. 전체 갱신에는 `python -m back.pipelines.refresh --water`를 사용하며 날짜 범위 등은 [상위 문서](../README.md)를 참고하세요.

이전 `detector.py`는 [`archive/back/pipelines/daily_water/`](../../../archive/back/pipelines/daily_water/)에 보관했습니다. 현재 이상 탐지는 `back/ml/lightgbm/` 모델을 사용합니다.
