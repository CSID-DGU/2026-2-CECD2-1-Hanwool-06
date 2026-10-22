"""LightGBM 전용 유틸리티."""

from __future__ import annotations

import random
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from scipy.stats import trim_mean
from tqdm.auto import tqdm
from back.pipelines.common import atomic_text, write_json

# 예측 타겟 컬럼명 (후처리에서 실측값 참조용)
TARGET = "일사용량_톤"


def load_config(path: str | Path) -> dict:
    """config.yaml 을 읽어 dict 로 반환한다."""
    with open(path, "r", encoding="utf-8") as fp:
        config = yaml.safe_load(fp)
    root = Path(__file__).resolve().parents[3]
    for name, value in config["data"].items():
        data_path = Path(value)
        config["data"][name] = str(data_path if data_path.is_absolute() else root / data_path)
    return config


def set_seed(seed: int) -> None:
    """재현성을 위해 파이썬/넘파이 난수 시드를 고정한다."""
    random.seed(seed)
    np.random.seed(seed)


def ensure_dir(path: str | Path) -> Path:
    """폴더가 없으면 만들고 Path 로 반환한다."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_json(obj: dict, path: str | Path) -> None:
    """dict 를 보기 좋은 JSON(한글 그대로)으로 저장한다."""
    write_json(Path(path), obj)


def save_csv(df: pd.DataFrame, path: str | Path) -> None:
    """DataFrame 을 UTF-8-BOM CSV(엑셀 호환)로 저장한다."""
    atomic_text(Path(path), df.to_csv(index=False), encoding="utf-8-sig")


def save_model(forecaster, path: str | Path) -> None:
    """학습한 최종 모델을 LightGBM 텍스트 형식으로 저장한다(lightgbm.Booster(model_file=...) 로 다시 읽는다)."""
    atomic_text(Path(path), forecaster.model.booster_.model_to_string())


def format_summary(summary: dict) -> str:
    """metric.summarize 결과를 한 줄 로그 문자열로 만든다."""
    values = [f"{key}={summary[key]:.4f}" if summary[key] is not None else f"{key}=N/A"
              for key in ['rmse', 'rmse_without_data_errors', 'rmse_normal_only', 'rmse_excluding_alerts']]
    return ' '.join(values) + f" 경고={summary['경고']} 주의={summary['주의']}"


# ── 예측 후처리 ────────────────────────────────────────────────────
# config 의 postprocess 섹션으로 켜고 끈다. 온라인 잔차보정은 그 역의 과거 잔차만, 같은 날 공통 보정은
# 다른 역의 당일 실측만 쓴다. 어느 것도 그 역 자신의 당일 실측은 쓰지 않는다.

def apply_common_shift(meta: pd.DataFrame, pred: np.ndarray, level: np.ndarray, cfg: dict) -> np.ndarray:
    """같은 날 다른 역들이 예측에서 함께 벗어난 만큼 예측을 옮긴다(자기 자신은 빼고 본다).

    검침 시각이 당겨진 날이나 연휴 다음 날처럼 모든 역이 같이 움직인 날을 개별 역의 이상으로 보지 않게 한다.
    벗어난 정도는 각 역의 최근 수준(level, 톤) 대비 비율로 재고 위아래 20%를 잘라낸 평균을 쓰므로,
    한 역의 급증이나 누수가 다른 역의 예측을 끌고 가지 못한다.
    """
    pp = cfg.get("postprocess", {})
    weight = float(pp.get("common_shift_weight", 0.0))
    min_peers = int(pp.get("common_shift_min_peers", 6))
    pred = np.asarray(pred, dtype=float)
    if weight <= 0:
        return pred.copy()
    level = np.fmax(np.asarray(level, dtype=float), 1.0)
    actual = meta[TARGET].to_numpy(dtype=float)
    relative = np.where(np.isfinite(actual) & (actual >= 0), (actual - pred) / level, np.nan)
    shift = np.zeros(len(pred))
    for rows in pd.Series(np.arange(len(pred))).groupby(pd.to_datetime(meta["날짜"]).to_numpy()).groups.values():
        rows = np.asarray(rows)
        for position, row in enumerate(rows):
            others = np.delete(relative[rows], position)
            others = others[np.isfinite(others)]
            if len(others) < min_peers:
                continue   # 견줄 역이 너무 적은 날은 옮기지 않는다
            common = trim_mean(others, 0.2)
            noise = 1.2533 * 1.4826 * np.median(np.abs(others - common)) / np.sqrt(len(others))   # 그 평균의 표준오차
            shift[row] = common ** 3 / (common ** 2 + noise ** 2 + 1e-12)   # 오차 범위 안의 작은 움직임은 줄여서 반영
    return np.clip(pred + weight * shift * level, 0.0, None)


def postprocess_predictions(meta: pd.DataFrame, pred: np.ndarray, level: np.ndarray, cfg: dict) -> np.ndarray:
    """config postprocess 설정대로 (온라인 잔차 + 같은 날 공통) 보정을 순서대로 적용한다."""
    pp = cfg.get("postprocess", {})
    out = _apply_online_residual_correction(meta, pred, float(pp.get("online_residual_alpha", 0.0)),
                                            float(pp.get("online_residual_clip", 0.0)))
    return apply_common_shift(meta, out, level, cfg)


def _apply_online_residual_correction(meta, pred, alpha: float, clip_value: float) -> np.ndarray:
    """역별로 날짜순 진행하며 과거 잔차의 지수이동평균(EWMA)을 다음 예측에 더한다 (인과적 온라인 보정)."""
    pred = np.asarray(pred, dtype=float)
    if alpha <= 0 or clip_value <= 0:
        return pred.copy()
    corrected = np.zeros_like(pred, dtype=float)
    state: dict[int, float] = {}
    work = meta[["고객번호", "날짜", TARGET]].copy()
    work["_pred"] = pred
    work["_row"] = np.arange(len(work))
    ordered = work.sort_values(["고객번호", "날짜"])
    for cid, _, actual, base_pred, row_idx in tqdm(
        ordered.itertuples(index=False, name=None), total=len(ordered), desc="온라인 잔차보정", leave=False,
    ):
        correction = state.get(cid, 0.0)
        corrected[row_idx] = max(float(base_pred) + correction, 0.0)
        if 0 <= actual <= 500:   # 급증 관측에 온라인 보정이 빠르게 적응하지 않도록 제한
            residual = float(np.clip(actual - base_pred, -clip_value, clip_value))
            state[cid] = (1.0 - alpha) * correction + alpha * residual
    return corrected


def save_scatter_plot(anomalies: pd.DataFrame, warn_q: float, alert_q: float, path: str | Path) -> None:
    """예측(x) vs 실측(y) 산점도를 저장한다.

    - y=x 완벽예측선: 검은 실선
    - 잔차 |actual-pred| 의 q95/q99 밴드: 빨간 점선 (y=x ± 해당 잔차)
    - 점 색: 심각도(정상/주의/경고). 음수·비유한 관측은 제외.
    (한글 폰트 깨짐 방지로 라벨은 영문)
    """
    import matplotlib
    matplotlib.use("Agg")   # 화면 없이 파일로만 저장
    import matplotlib.pyplot as plt

    df = anomalies[~anomalies["likely_data_error"]]
    if df.empty:
        return
    actual = df["일사용량_톤"].to_numpy(dtype=float)
    pred = df["predicted_ton"].to_numpy(dtype=float)
    abs_resid = np.abs(actual - pred)
    d_warn = float(np.quantile(abs_resid, warn_q))    # 잔차 q95
    d_alert = float(np.quantile(abs_resid, alert_q))  # 잔차 q99
    hi = float(max(actual.max(), pred.max())) * 1.02
    lim = (0.0, hi)

    fig, ax = plt.subplots(figsize=(7, 7))
    palette = [("정상", "Normal", "#7f9bb3"),
               ("주의", "Warning", "#f5a142"),
               ("경고", "Alert", "#d62728")]
    for sev_kr, sev_en, color in palette:
        m = (df["심각도"] == sev_kr).to_numpy()
        ax.scatter(pred[m], actual[m], s=12, alpha=0.85, c=color,
                   edgecolors="none", label=f"{sev_en} ({int(m.sum())})")

    line = np.array(lim)
    ax.plot(line, line, color="black", lw=1.8, label="y = x (perfect)")
    for d in (d_warn, d_alert):   # 양방향(과다/과소) 밴드
        ax.plot(line, line + d, "r--", lw=1.1, alpha=0.85)
        ax.plot(line, line - d, "r--", lw=1.1, alpha=0.85)
    ax.plot([], [], "r--", lw=1.1, label=f"residual q{warn_q:g}=±{d_warn:.1f}, q{alert_q:g}=±{d_alert:.1f}")

    ax.set_xlim(lim)
    ax.set_ylim(lim)
    ax.set_aspect("equal")
    ax.set_xlabel("Prediction (ton)")
    ax.set_ylabel("Actual (ton)")
    ax.set_title("LightGBM — Prediction vs Actual (test)")
    ax.legend(loc="upper left", fontsize=9, framealpha=0.9)
    ax.grid(True, ls=":", alpha=0.4)
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
