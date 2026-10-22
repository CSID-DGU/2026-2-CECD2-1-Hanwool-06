LightGBM 기반 사용량 예측 및 이상탐지 모델 코드입니다.

운영 자료로 재학습하고 API 스냅샷까지 갱신하려면 저장소 루트에서 `python -m back.pipelines.refresh --model`을 실행합니다. 결과는 `APP_DATA_DIR/model/`에 저장되며 기본 경로는 `data/runtime/model/`입니다.

예측 목표는 그 역의 최근 7일 중앙 사용량 대비 비율이고, 예측한 뒤 같은 날 다른 역들이 함께 벗어난 만큼 옮깁니다(자기 검침값 제외). 이상 점수는 오차를 최근 28일 중앙 사용량으로 나눈 비율로 계산합니다. 결과 폴더에는 `metrics.json`, `test_anomalies.csv`, `feature_importance.csv`와 학습한 모델 `model.txt`가 저장됩니다. `metrics.json`의 `rmse_ordinary_days`·`mae_ordinary_days`는 모델과 무관하게 고른 평소 같은 날의 오차로, 설정을 바꾼 모델끼리 비교할 때 씁니다.

모델만 직접 시험하려면 `python -m back.ml.lightgbm.main`을 사용합니다. 기본 설정은 `data/ml_dataset/`과 `data/processed/`를 읽고 `data/runtime/model/`에 결과를 저장합니다. `--config`와 `--out-dir`로 별도 입력 설정과 출력 경로를 지정할 수 있습니다.

`data/ml_dataset/`은 이전 기준을 그대로 둔 보존 자료라 사용량은 검침일(`날짜` 전날 쓴 물), 승하차는 사용일 기준으로 하루 어긋나 있어, 이 자료로 돌린 결과는 운영 모델과 비교할 수 없습니다. 운영 모델은 갱신이 `APP_DATA_DIR/dataset/`에 만든 자료를 쓰므로, 같은 자료로 시험하려면 그 폴더의 `config.yaml`을 `--config`로 넘깁니다.

이전 `results/`는 [`archive/back/ml/lightgbm/results/`](../../../archive/back/ml/lightgbm/results/)에 보관했습니다. 초기 앱의 위험도 자료는 `data/app_seed/risk/test_anomalies.csv`에 별도로 유지합니다.
