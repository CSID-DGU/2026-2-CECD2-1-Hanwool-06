"""LightGBM 전용 학습/평가 진입점 (오케스트레이션만 담당).

실행:
  conda run -n hanul python back/ml/lightgbm/main.py

흐름:
  [1/2] 검증모델  train -> valid : valid 잔차로 역별 척도와 이상탐지 임계값(q95/q99) 확정
  [2/2] 최종모델  train+valid -> test : 재학습 후 후처리 → 이상탐지 → 결과/그림 저장
  두 단계 모두 승하차를 쓰는 본 모델과 승하차 없이 배우는 보조 모델을 함께 학습하고,
  당일·전날 승하차가 아직 없는 날(월별 파일이 나오기 전의 최근 날짜)은 보조 모델로 판정한다.

실제 로직은 dataset.py(피처) · model.py(모델) · metric.py(지표) · utils.py(설정/후처리/그림)에 있다.
"""

from __future__ import annotations

import sys
import argparse
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(HERE.parents[2]) not in sys.path:
    sys.path.insert(0, str(HERE.parents[2]))

if __package__:
    from . import dataset, metric, utils
    from .model import LightGBMForecaster
else:
    import dataset
    import metric
    import utils
    from model import LightGBMForecaster


def _without_ridership(X: pd.DataFrame) -> pd.DataFrame:
    """승하차 열을 뺀 피처. 최근 승하차가 아직 수집되지 않은 날을 판정하는 보조 모델이 쓴다."""
    return X.drop(columns=[c for c in X.columns if c == "총승객수" or c.startswith("riders_")])


def _ridership_missing(X: pd.DataFrame) -> np.ndarray:
    """당일이나 전날 승하차가 없는 행. 승하차 열이 비면 본 모델의 오차가 크게 늘어 보조 모델로 예측한다."""
    return X[["총승객수", "riders_1days_ago"]].isna().any(axis=1).to_numpy()


def _fit_models(cfg: dict, X: pd.DataFrame, y, categorical_cols: list[str]) -> tuple:
    """승하차를 쓰는 본 모델과 승하차 없이 배우는 보조 모델을 같은 행으로 학습한다."""
    level = dataset.recent_level(X)
    main_model = LightGBMForecaster(cfg["model"]).fit(X, y, categorical_cols, level)
    spare_model = LightGBMForecaster(cfg["model"]).fit(_without_ridership(X), y, categorical_cols, level,
                                                        progress_desc="LightGBM 학습(승하차 없음)")
    return main_model, spare_model


def _predict(models: tuple, X: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """행마다 알맞은 모델로 예측한 톤과, 보조 모델을 쓴 행 표시를 돌려준다."""
    main_model, spare_model = models
    level = dataset.recent_level(X)
    missing = _ridership_missing(X)
    pred = main_model.predict(X, level)
    if missing.any():
        pred = np.where(missing, spare_model.predict(_without_ridership(X), level), pred)
    return pred, missing


def run(config_path: Path | None = None, out_dir: Path | None = None) -> dict:
    """전체 파이프라인 실행: 검증모델→임계값 확정, 최종모델→test 예측·이상탐지·결과저장. 지표 dict 반환."""
    cfg = utils.load_config(config_path or HERE / "config.yaml")
    utils.set_seed(int(cfg["seed"]))
    results_dir = utils.ensure_dir(out_dir or cfg.get("output_dir") or HERE.parents[2] / "data" / "runtime" / "model")
    warn_q = float(cfg["anomaly"]["warn_quantile"])    # 주의 분위수 (상위 5%)
    alert_q = float(cfg["anomaly"]["alert_quantile"])  # 경고 분위수 (상위 1%)

    # [1/2] 검증모델: train 으로 학습, valid 로 역별 척도와 임계값 산출
    print("[1/2] validation model: train -> valid")
    valid_data = dataset.prepare_data(cfg, fit_splits=("train",))
    train_mask = dataset.training_mask(valid_data.frame, ("train",), cfg)
    X_train, y_train = dataset.split_xy(valid_data, train_mask)
    X_valid = dataset.split_features(valid_data, "valid")
    y_valid = valid_data.frame.loc[valid_data.frame["split"] == "valid", dataset.TARGET].reset_index(drop=True)
    if X_train.empty or X_valid.empty or not np.isfinite(y_valid.to_numpy(dtype=float)).all():
        raise ValueError("Model requires non-empty train and finite validation observations")

    valid_models = _fit_models(cfg, X_train, y_train, valid_data.categorical_cols)

    valid_meta = dataset.split_meta(valid_data, "valid")
    valid_level = dataset.recent_level(X_valid, 28)   # 역끼리 오차를 견주는 기준: 최근 28일 중앙 사용량
    # test 예측과 같은 기준으로 잔차를 재도록 valid 예측에도 같은 날 공통 보정을 적용한다
    valid_pred = utils.apply_common_shift(valid_meta, _predict(valid_models, X_valid)[0], valid_level, cfg)
    # valid 의 |deviation_score| 분포 q95/q99 를 임계값으로 확정 → test 에도 동일 적용
    calibration = metric.fit_calibration(valid_meta, valid_pred, valid_level)
    valid_scored = metric.score_predictions(valid_meta, valid_pred, calibration, valid_level)
    warn_t, alert_t = metric.score_quantiles(valid_scored["deviation_score"], warn_q, alert_q)
    valid_rows = (valid_data.frame["split"] == "valid").to_numpy()
    valid_summary = metric.summarize(metric.classify(valid_scored, warn_t, alert_t),
                                     metric.ordinary_days(valid_data.frame).to_numpy()[valid_rows])
    print(f"  임계값(|deviation_score|): 주의>={warn_t:.3f} (q{warn_q:g}), 경고>={alert_t:.3f} (q{alert_q:g})")
    print(f"  valid {utils.format_summary(valid_summary)}")

    # [2/2] 최종모델: train+valid 로 재학습, test 예측 → 후처리 → 이상탐지
    print("[2/2] final model: train+valid -> test")
    final_data = dataset.prepare_data(cfg, fit_splits=("train", "valid"))
    final_mask = dataset.training_mask(final_data.frame, ("train", "valid"), cfg)
    X_final, y_final = dataset.split_xy(final_data, final_mask)
    X_test = dataset.split_features(final_data, "test")

    if X_test.empty:
        raise ValueError("No eligible test observations; snapshot must be withheld")
    final_models = _fit_models(cfg, X_final, y_final, final_data.categorical_cols)

    test_meta = dataset.split_meta(final_data, "test")
    test_level = dataset.recent_level(X_test, 28)
    test_pred_raw, without_ridership = _predict(final_models, X_test)
    test_pred = utils.postprocess_predictions(test_meta, test_pred_raw, test_level, cfg)
    if not np.isfinite(test_pred).all() or len(test_pred) != len(test_meta):
        raise ValueError("Model returned incomplete or non-finite predictions")
    test_anomalies = metric.classify(metric.score_predictions(test_meta, test_pred, calibration, test_level),
                                     warn_t, alert_t)
    test_anomalies.insert(5, "predicted_ton_before_adjust", np.asarray(test_pred_raw, dtype=float).round(3))
    test_anomalies["without_ridership"] = without_ridership   # 승하차 없이 판정한 날(아직 수집 전인 최근 날짜)
    test_rows = (final_data.frame["split"] == "test").to_numpy()
    test_summary = metric.summarize(test_anomalies, metric.ordinary_days(final_data.frame).to_numpy()[test_rows])
    print(f"  test {utils.format_summary(test_summary)}")

    # 결과 저장
    metrics = {
        "model": "LightGBM",
        "mode": "validation=train->valid, final=train+valid->test",
        "postprocess": cfg.get("postprocess", {}),
        "feature_count": len(final_data.feature_cols),
        "n_estimators": int(cfg["model"]["n_estimators"]),
        "warn_threshold": warn_t,
        "alert_threshold": alert_t,
        "train_rows_for_valid_model": int(train_mask.sum()),
        "train_rows_for_final_model": int(final_mask.sum()),
        "test_rows_without_ridership": int(without_ridership.sum()),
        "valid": valid_summary,
        "test": test_summary,
    }
    utils.save_json(metrics, results_dir / "metrics.json")
    utils.save_csv(test_anomalies, results_dir / "test_anomalies.csv")
    utils.save_csv(test_anomalies[test_anomalies["심각도"] != "정상"].sort_values("deviation_score"),
                   results_dir / "test_anomalies_flagged.csv")
    utils.save_csv(final_models[0].feature_importance(final_data.feature_cols),
                   results_dir / "feature_importance.csv")
    utils.save_model(final_models[0], results_dir / "model.txt")
    utils.save_model(final_models[1], results_dir / "model_without_ridership.txt")
    if cfg.get("save_plot", True):
        utils.save_scatter_plot(test_anomalies, warn_q, alert_q, results_dir / "test_pred_vs_actual.png")
    print(f"  saved: {results_dir}")
    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=HERE / "config.yaml")
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args()
    run(args.config, args.out_dir)
