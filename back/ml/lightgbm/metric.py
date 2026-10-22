"""평가 지표 + 이상탐지.

이상탐지 흐름:
  1) score_predictions : 예측오차(error_ton)를 역별 'deviation_score' 로 바꾼다.
       deviation_score = (오차 ÷ 그 역의 최근 수준) / 역별 척도(1.4826 × 중앙값절대편차)
       → 톤이 아니라 최근 수준 대비 비율로 재므로 사용량이 많은 여름에 주의·경고가 덩달아 늘지 않는다.
       → 평균/표준편차 대신 '중앙값' 기준이라 극단치 몇 개에 흔들리지 않는다.
  2) score_quantiles : 기준 분포(보통 valid)의 |deviation_score| 에서 q95/q99 임계값을 구한다.
  3) classify : 그 임계값으로 정상/주의/경고 + 방향(과다/과소)을 매긴다.
  → valid 의 q95/q99 를 test 에도 그대로 적용하면, test 가 실제로 더 이상할 때
    경고가 1% 보다 많이 잡혀 '이상 정도'를 반영한다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def rmse(actual: np.ndarray, pred: np.ndarray) -> float:
    """평균제곱근오차(톤). 큰 오차에 더 민감 → 극단치 1건에도 크게 튄다."""
    actual = np.asarray(actual, dtype=float)
    pred = np.asarray(pred, dtype=float)
    return float(np.sqrt(np.mean((actual - pred) ** 2)))


def mae(actual: np.ndarray, pred: np.ndarray) -> float:
    """평균절대오차(톤). 극단치에 덜 민감."""
    actual = np.asarray(actual, dtype=float)
    pred = np.asarray(pred, dtype=float)
    return float(np.mean(np.abs(actual - pred)))


SHRINK_ROWS = 30   # 역별 척도를 전체 척도 쪽으로 당기는 세기(검증 행 수로 환산)


def _per_level(error_ton, level) -> np.ndarray:
    """오차를 그 역의 최근 수준(톤, 하한 1)으로 나눈다. level 이 없으면 톤 단위 그대로 둔다."""
    error_ton = np.asarray(error_ton, dtype=float)
    return error_ton if level is None else error_ton / np.fmax(np.asarray(level, dtype=float), 1.0)


def _spread(values) -> float:
    """1.4826 × 중앙값절대편차."""
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    return float(1.4826 * np.median(np.abs(values - np.median(values)))) if len(values) else float("nan")


def fit_calibration(meta: pd.DataFrame, predicted_ton: np.ndarray, level=None) -> pd.DataFrame:
    """Freeze each meter's residual scale on validation, independently of future test rows.

    level 은 행마다 그 역의 최근 수준(최근 28일 중앙 사용량)이다. 검증 행이 적은 역의 척도는 전체 척도 쪽으로 당긴다.
    """
    relative = pd.Series(_per_level(meta["일사용량_톤"].to_numpy(dtype=float) - np.asarray(predicted_ton, dtype=float), level))
    pooled = _spread(relative)
    if not np.isfinite(pooled) or pooled < 1e-6:
        pooled = 1.0
    rows = []
    for customer, values in relative.groupby(meta["고객번호"].to_numpy()):
        own = _spread(values)
        if not np.isfinite(own) or own < 1e-6:
            own = pooled
        count = int(np.isfinite(values).sum())
        scale = np.exp((count * np.log(own) + SHRINK_ROWS * np.log(pooled)) / (count + SHRINK_ROWS))
        rows.append({"고객번호": customer, "scale": float(scale)})
    return pd.DataFrame(rows).set_index("고객번호")


def score_predictions(meta: pd.DataFrame, predicted_ton: np.ndarray, calibration=None, level=None) -> pd.DataFrame:
    """예측오차와 역별 deviation_score 까지만 계산(심각도 분류 전 단계)."""
    df = meta.copy()
    df["predicted_ton"] = np.asarray(predicted_ton, dtype=float).round(3)
    df["error_ton"] = (df["일사용량_톤"] - df["predicted_ton"]).round(3)   # 실제 − 예측
    calibration = fit_calibration(meta, predicted_ton, level) if calibration is None else calibration
    scale = df["고객번호"].map(calibration["scale"]).astype(float)   # 검증 이력이 없는 역은 NaN → 자료부족
    df["deviation_score"] = (_per_level(df["error_ton"], level) / scale).round(3)
    # 큰 사용량만으로 자료 오류로 판정하면 실제 급증 경고가 화면에서 숨겨진다.
    df["likely_data_error"] = ~np.isfinite(df["일사용량_톤"]) | (df["일사용량_톤"] < 0)
    return df


def score_quantiles(deviation_score: pd.Series, warn_q: float, alert_q: float) -> tuple[float, float]:
    """|deviation_score| 분포에서 주의/경고 임계값을 분위수로 구한다 (예: warn_q=0.95, alert_q=0.99)."""
    abs_score = deviation_score.abs()
    abs_score = abs_score[np.isfinite(abs_score)]
    if abs_score.empty:
        raise ValueError("Validation has no finite anomaly calibration scores")
    return max(1e-6, float(abs_score.quantile(warn_q))), max(1e-6, float(abs_score.quantile(alert_q)))


def classify(df: pd.DataFrame, warn_threshold: float, alert_threshold: float) -> pd.DataFrame:
    """|deviation_score| 가 임계값 이상이면 주의/경고로 분류하고 방향(과다/과소)을 매긴다."""
    out = df.copy()
    abs_score = out["deviation_score"].abs()
    out["심각도"] = np.where(abs_score >= alert_threshold, "경고",
                           np.where(abs_score >= warn_threshold, "주의", "정상"))
    out.loc[abs_score.isna(), "심각도"] = "자료부족"
    out["방향"] = np.where(out["심각도"].isin(["정상", "자료부족"]), "",
                         np.where(out["deviation_score"] > 0, "과다", "과소"))
    return out


def ordinary_days(frame: pd.DataFrame) -> pd.Series:
    """모델과 무관하게 '평소와 같은 날'을 표시한다. 앞뒤 날짜를 함께 보므로 평가에만 쓴다.

    계량기별로 앞뒤 15일 중앙값에서 벗어난 정도가 그 무렵 로버스트 표준편차의 4배 이내인 날이다.
    (폭의 하한: 중앙값의 5% 와 0.5톤 중 큰 값)
    """
    ordered = frame.sort_values(["고객번호", "날짜"])
    by_meter = ordered["고객번호"].astype(str)
    usage = ordered["일사용량_톤"].astype(float)
    median = usage.groupby(by_meter).transform(lambda s: s.rolling(15, center=True, min_periods=5).median())
    gap = (usage - median).abs()
    spread = gap.groupby(by_meter).transform(lambda s: s.rolling(61, center=True, min_periods=15).median())
    limit = 4 * np.fmax(1.4826 * spread, np.fmax(0.05 * median, 0.5))
    return ((gap <= limit) | median.isna()).reindex(frame.index)


def summarize(anomalies: pd.DataFrame, ordinary=None) -> dict:
    """RMSE 변형들과 이상탐지 건수를 요약한다.

    rmse_without_data_errors : 음수/비유한 관측만 제외한 예측오차.
    rmse_excluding_alerts    : 경고로 탐지된 날까지 뺀 값 — 참고용(낙관적).
    rmse_normal_only         : 주의·경고 모두 뺀 '정상'만 — 모델이 고른 행이라 임계값이 바뀌면 함께 바뀐다.
    rmse_ordinary_days       : ordinary_days 가 표시한 평소 같은 날만 — 모델·임계값이 달라도 같은 날로 비교한다.
    """
    actual = anomalies["일사용량_톤"].to_numpy(dtype=float)
    pred = anomalies["predicted_ton"].to_numpy(dtype=float)
    not_alert = (anomalies["심각도"] != "경고").to_numpy()
    no_error = (~anomalies["likely_data_error"]).to_numpy()
    normal_only = (anomalies["심각도"] == "정상").to_numpy()   # 주의·경고 모두 제외
    ordinary = np.zeros(len(anomalies), dtype=bool) if ordinary is None else np.asarray(ordinary, dtype=bool)
    def measured(fn, mask):
        return round(fn(actual[mask], pred[mask]), 4) if mask.any() else None

    return {
        "rmse": round(rmse(actual, pred), 4),
        "rmse_without_data_errors": measured(rmse, no_error),
        "rmse_excluding_alerts": measured(rmse, not_alert),
        "rmse_normal_only": measured(rmse, normal_only),
        "mae": round(mae(actual, pred), 4),
        "mae_normal_only": measured(mae, normal_only),
        "rmse_ordinary_days": measured(rmse, ordinary),
        "mae_ordinary_days": measured(mae, ordinary),
        "n_ordinary_days": int(ordinary.sum()),
        "n_samples": int(len(anomalies)),
        "n_not_alert": int(not_alert.sum()),
        "n_normal_only": int(normal_only.sum()),
        "n_without_errors": int(no_error.sum()),
        "경고": int((anomalies["심각도"] == "경고").sum()),
        "주의": int((anomalies["심각도"] == "주의").sum()),
        "과다": int((anomalies["방향"] == "과다").sum()),
        "과소": int((anomalies["방향"] == "과소").sum()),
        "n_likely_data_error": int(anomalies["likely_data_error"].sum()),
    }
