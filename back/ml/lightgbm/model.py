"""LightGBM 회귀 모델 래퍼."""

from __future__ import annotations

import lightgbm as lgb
import numpy as np
import pandas as pd
from tqdm.auto import tqdm


def _tqdm_callback(total: int, desc: str):
    """부스팅 라운드 진행률을 tqdm 막대에 보여주는 콜백."""
    bar = tqdm(total=total, desc=desc, leave=False)

    def _callback(env: "lgb.callback.CallbackEnv") -> None:
        """매 부스팅 라운드마다 LightGBM 이 호출 → 막대 1칸 전진."""
        bar.update(1)
        if env.iteration + 1 >= env.end_iteration:   # 마지막 라운드면 닫기
            bar.close()

    return _callback


class LightGBMForecaster:
    """LightGBM 회귀기를 감싼 예측기 (config 의 model 파라미터로 생성)."""

    name = "LightGBM"

    def __init__(self, params: dict):
        """config 의 model 섹션(dict)을 받아 LGBMRegressor 를 만든다."""
        self.params = dict(params)
        self.model = lgb.LGBMRegressor(**self.params)

    def fit(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        categorical_cols: list[str],
        level: np.ndarray,
        progress_desc: str = "LightGBM 학습",
    ) -> "LightGBMForecaster":
        """모델을 학습한다. 목표는 톤이 아니라 '그 역의 최근 수준 대비 비율'(y / level)이다.

        categorical_cols : 범주형으로 처리할 컬럼(고객번호 등) — LightGBM 이 native 처리
        level            : 행마다 그 역의 최근 수준(톤). 가중치로도 쓰므로 비율의 절대오차 합이
                           톤 단위 절대오차 합과 같아진다(사용량이 큰 역이 묻히지 않는다)
        """
        # 부스팅 라운드 수만큼 진행률 막대 표시 (device=cuda 이면 GPU 에서 학습됨)
        callbacks = [_tqdm_callback(int(self.params.get("n_estimators", 100)), progress_desc)]
        self.model.fit(X, y / level, sample_weight=level, categorical_feature=categorical_cols, callbacks=callbacks)
        return self

    def predict(self, X: pd.DataFrame, level: np.ndarray) -> np.ndarray:
        """예측값(톤)을 반환. 음수 사용량은 불가능하므로 비율을 0 이상으로 자른 뒤 수준을 곱한다."""
        ratio = np.asarray(self.model.predict(X), dtype=float)
        return np.clip(ratio, 0.0, None) * level

    def feature_importance(self, feature_names: list[str]) -> pd.DataFrame:
        """피처별 중요도를 내림차순 표로 반환한다(어떤 피처가 예측에 많이 쓰였는지)."""
        return (
            pd.DataFrame({
                "feature": feature_names,
                "importance": self.model.feature_importances_,
            })
            .sort_values("importance", ascending=False)
            .reset_index(drop=True)
        )
