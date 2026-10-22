"""데이터 로딩 + 피처 생성 파이프라인.

'한 역의 하루'(역, 날짜)가 한 행인 시계열 예측이다.
모든 피처는 해당 날짜 '이전'에 알 수 있는 값만 쓴다(미래 정보 누수 방지).
최종 test 모델은 train+valid 를 과거 학습 구간으로 쓴다.

피처 이름은 전문용어 대신 자기설명적으로 지었다. 예)
  usage_1days_ago        = 1일 전 사용량
  usage_avg_last7days    = 최근 7일 사용량 평균
  riders_std_last2days   = 최근 2일 승객수 표준편차
  bill_daily_avg         = 청구서 기반 그 달 일평균 사용량
  station_month_typical  = 그 역이 그 달에 평소 쓰는 양(중앙값)
  rel_usage_1days_ago    = 1일 전 사용량 ÷ 그 역의 최근 수준(직전 7일 중앙값)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd


TARGET = "일사용량_톤"   # 예측 대상 컬럼


@dataclass
class PreparedData:
    """피처 생성이 끝난 데이터 묶음.

    frame            : 전체 행(train/valid/test) + 모든 피처가 든 DataFrame
    feature_cols     : 모델 입력으로 쓸 피처 컬럼 이름들
    categorical_cols : 그중 범주형으로 다룰 컬럼들 (LightGBM 이 직접 처리)
    """
    frame: pd.DataFrame
    feature_cols: list[str]
    categorical_cols: list[str]


def prepare_data(cfg: dict, fit_splits: tuple[str, ...]) -> PreparedData:
    """CSV 로딩부터 피처 생성까지 한 번에 수행한다.

    fit_splits : 학습에 허용된 과거 구간. 검증모델은 ("train",), 최종모델은 ("train","valid").
    """
    df = _load_splits(cfg)
    df = _merge_calendar(df, cfg)
    df = _merge_bill_baseline(df, cfg)
    df = _add_calendar_cycle_features(df)
    df = _add_history_features(df, cfg)
    df = _add_typical_level_features(df, fit_splits)
    df = _fill_missing(df)
    df = _add_level_relative_features(df)

    categorical_cols = ["고객번호", "사업소명", "역명", "고지서_성명"]
    for col in categorical_cols:
        df[col] = df[col].astype("category")

    # 요일·일유형은 값이 없는 열이고, 승차·하차 구분은 일별 수집 자료에 없어 판정할 때는 항상 비어 있다.
    exclude = {"split", "날짜", TARGET, "요일", "일유형", "승차총승객수", "하차총승객수"}
    feature_cols = [c for c in df.columns if c not in exclude]
    for col in feature_cols:
        if col not in categorical_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce").replace([np.inf, -np.inf], np.nan)

    return PreparedData(df.copy(), feature_cols, categorical_cols)


def recent_level(features: pd.DataFrame, days: int = 7) -> np.ndarray:
    """그 역의 최근 수준(톤, 하한 1): 직전 days 일(관측 기준) 사용량의 중앙값.

    7일은 예측 목표를 나누는 기준으로, 28일은 역끼리 오차를 견줄 때(같은 날 공통 보정·이상 점수) 쓴다.
    """
    return np.fmax(features[f"usage_median_last{days}days"].to_numpy(dtype=float), 1.0)


def training_mask(df: pd.DataFrame, splits: tuple[str, ...], cfg: dict) -> pd.Series:
    """학습에 쓸 행을 True/False 로 표시한다. 과거 구간 중 명백한 오류·극단값은 제외.

    역별로 [Q1 - k*IQR, Q3 + k*IQR] 범위를 벗어나면 빼고, 음수 사용량도 뺀다.
    """
    in_splits = df["split"].isin(splits)
    keep = pd.Series(False, index=df.index)
    k = float(cfg["features"]["outlier_iqr_k"])
    drop_negative = bool(cfg["features"]["drop_negative"])
    for _, idx in df[in_splits].groupby("고객번호").groups.items():
        values = df.loc[idx, TARGET]
        q1, q3 = values.quantile(0.25), values.quantile(0.75)
        iqr = q3 - q1
        lo, hi = q1 - k * iqr, q3 + k * iqr
        ok = (values >= lo) & (values <= hi)
        if drop_negative:
            ok = ok & (values >= 0)
        keep.loc[idx] = ok
    return keep


def split_xy(prepared: PreparedData, mask: pd.Series):
    """표시된 행만 골라 (X 피처, y 정답) 으로 나눈다."""
    df = prepared.frame.loc[mask]
    X = df[prepared.feature_cols]
    y = df[TARGET].astype(float)
    return X, y


def split_meta(prepared: PreparedData, split: str) -> pd.DataFrame:
    """특정 구간(valid/test)의 식별·정답 정보(고객번호·역명·날짜·실제사용량)만 뽑는다."""
    cols = ["고객번호", "역명", "날짜", TARGET]
    return prepared.frame.loc[prepared.frame["split"] == split, cols].reset_index(drop=True)


def split_features(prepared: PreparedData, split: str) -> pd.DataFrame:
    """특정 구간(valid/test)의 피처 행렬 X 만 뽑는다(예측 입력용)."""
    return prepared.frame.loc[prepared.frame["split"] == split, prepared.feature_cols].reset_index(drop=True)


def _load_splits(cfg: dict) -> pd.DataFrame:
    """train/valid/test CSV 를 구간 표시(split)와 함께 합친다(중복 제거 + 역·날짜순 정렬).

    과거값 참조 피처가 구간 경계를 넘나들 수 있도록 따로 계산하지 않고 합쳐서 만든다.
    """
    frames = []
    for split in ("train", "valid", "test"):
        path = Path(cfg["data"][split])
        df = pd.read_csv(path, encoding="utf-8-sig", parse_dates=["날짜"], dtype={"고객번호": str})
        df["고객번호"] = df["고객번호"].str.zfill(9)
        df["split"] = split
        frames.append(df)
    return (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates(subset=["고객번호", "날짜"], keep="last")
        .sort_values(["고객번호", "날짜"])
        .reset_index(drop=True)
    )


def _merge_calendar(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """date_index.csv 를 붙여 달력 정보(월/요일/주말/공휴일/휴무일/징검다리)를 추가한다."""
    cal = pd.read_csv(cfg["data"]["calendar"], encoding="utf-8-sig", parse_dates=["날짜"])
    cal = cal[["날짜", "월", "요일_숫자", "주말", "공휴일", "휴무일", "징검다리"]]
    out = df.merge(cal, on="날짜", how="left")
    for col in ["주말", "공휴일", "휴무일", "징검다리"]:
        out[col] = out[col].fillna(0).astype(int)
    return out


def _merge_bill_baseline(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """Only bills available before an observation may inform its seasonal baseline.

    Exact issue dates are absent in the historical export. Use the first day after
    the final billing month as a conservative availability boundary. Incomplete
    one-month buckets cannot identify which month was billed, so exclude them.
    """
    bills = pd.read_csv(cfg["data"]["bills"], encoding="utf-8-sig", dtype={"고객번호": str})
    records = []
    for row in bills.itertuples(index=False):
        if row.포함월수 != 2 or not np.isfinite(row.격월사용량_톤) or row.격월사용량_톤 < 0:
            continue
        start = pd.Timestamp(str(row.시작연월))
        end = pd.Timestamp(str(row.종료연월))
        available = end + pd.offsets.MonthBegin(1)
        for month in {start.month, end.month}:
            records.append((str(row.고객번호).zfill(9), month, available,
                            float(row.격월사용량_톤) / 2 / 30.4))
    if not records:
        return df.assign(bill_daily_avg=np.nan)
    baseline = pd.DataFrame(records, columns=["고객번호", "月", "available", "value"])
    baseline = baseline.sort_values("available").drop_duplicates(["고객번호", "月", "available"], keep="last")
    baseline["bill_daily_avg"] = baseline.groupby(["고객번호", "月"])["value"].transform(lambda s: s.expanding().median())
    left = df.copy()
    left["_order"] = np.arange(len(left))
    left["月"] = left["날짜"].dt.month.astype("int64")
    left["날짜"] = left["날짜"].astype("datetime64[ns]")
    baseline["available"] = baseline["available"].astype("datetime64[ns]")
    joined = pd.merge_asof(left.sort_values("날짜"),
        baseline[["고객번호", "月", "available", "bill_daily_avg"]].sort_values("available"),
        left_on="날짜", right_on="available", by=["고객번호", "月"], allow_exact_matches=False)
    return joined.sort_values("_order").drop(columns=["_order", "月", "available"]).reset_index(drop=True)


def _add_calendar_cycle_features(df: pd.DataFrame) -> pd.DataFrame:
    """월/요일/일/연중일을 sin·cos 로 바꾼다(12월↔1월, 일↔월요일을 가깝게 연결)."""
    out = df.copy()
    out["dayofmonth"] = out["날짜"].dt.day
    out["dayofyear"] = out["날짜"].dt.dayofyear
    # (출력이름, 원본컬럼, 주기길이)
    cycles = [("month", "월", 12), ("weekday", "요일_숫자", 7),
              ("dayofmonth", "dayofmonth", 31), ("dayofyear", "dayofyear", 366)]
    for name, src, period in cycles:
        out[f"{name}_sin"] = np.sin(2 * np.pi * out[src] / period)
        out[f"{name}_cos"] = np.cos(2 * np.pi * out[src] / period)
    return out


def _add_history_features(df: pd.DataFrame, cfg: dict) -> pd.DataFrame:
    """역별 과거 사용량·승객수의 '며칠 전 값 / 최근 통계 / 비율·차이' 피처를 만든다.

    모두 과거로 밀어(shift) 계산하므로 '해당 날짜 이전' 값만 쓴다(누수 없음).
    config 의 usage_lags / passenger_lags / rolling_windows 가 어떤 피처를 만들지 정한다.
    """
    out = df.copy()
    by_station = out.groupby("고객번호", sort=False)
    usage = by_station[TARGET]
    riders = by_station["총승객수"]

    features: dict[str, pd.Series] = {}
    for n in cfg["features"]["usage_lags"]:          # n일 전 사용량
        n = int(n)
        features[f"usage_{n}days_ago"] = usage.shift(n)
    for n in cfg["features"]["passenger_lags"]:      # n일 전 승객수
        n = int(n)
        features[f"riders_{n}days_ago"] = riders.shift(n)

    usage_until_yesterday = usage.shift(1)           # 오늘 값을 빼려고 하루 민다
    riders_until_yesterday = riders.shift(1)
    for w in cfg["features"]["rolling_windows"]:     # 최근 w일 통계
        w = int(w)
        ru = usage_until_yesterday.groupby(out["고객번호"]).rolling(w, min_periods=1)
        features[f"usage_avg_last{w}days"]    = ru.mean().reset_index(level=0, drop=True)
        features[f"usage_median_last{w}days"] = ru.median().reset_index(level=0, drop=True)
        features[f"usage_std_last{w}days"]    = ru.std().reset_index(level=0, drop=True)
        features[f"usage_min_last{w}days"]    = ru.min().reset_index(level=0, drop=True)
        features[f"usage_max_last{w}days"]    = ru.max().reset_index(level=0, drop=True)
        rp = riders_until_yesterday.groupby(out["고객번호"]).rolling(w, min_periods=1)
        features[f"riders_avg_last{w}days"]   = rp.mean().reset_index(level=0, drop=True)
        features[f"riders_std_last{w}days"]   = rp.std().reset_index(level=0, drop=True)

    out = pd.concat([out, pd.DataFrame(features, index=out.index)], axis=1)
    out["days_since_last_record"] = by_station["날짜"].diff().dt.days.fillna(30).clip(1, 30)
    out["riders_today_vs_7days_ago_ratio"] = out["총승객수"] / out["riders_7days_ago"].replace(0, np.nan)
    out["usage_1day_vs_7days_ago_ratio"] = out["usage_1days_ago"] / out["usage_7days_ago"].replace(0, np.nan)
    out["usage_1day_minus_7days_ago"] = out["usage_1days_ago"] - out["usage_7days_ago"]
    out["riders_today_minus_7days_ago"] = out["총승객수"] - out["riders_7days_ago"]
    return out


def _add_typical_level_features(df: pd.DataFrame, fit_splits: tuple[str, ...]) -> pd.DataFrame:
    """'이 역이 이 달/이 요일에 평소 얼마 쓰나'(중앙값)를 만들어 모든 행에 붙인다.

    학습 구간에서 극단치를 뺀 값으로 계산하므로 test 에 미래정보가 새지 않는다.
    역별 값이 없을 때 쓸 전체(월/요일) 평소값도 함께 만든다.
    """
    fit = df[df["split"].isin(fit_splits)].copy()
    clean = fit[_normal_rows(fit)]

    station_month = (clean.groupby(["고객번호", "월"])[TARGET].median()
                     .rename("station_month_typical").reset_index())
    station_weekday = (clean.groupby(["고객번호", "요일_숫자"])[TARGET].median()
                       .rename("station_weekday_typical").reset_index())
    all_month = (clean.groupby("월")[TARGET].median()
                 .rename("all_month_typical").reset_index())
    all_weekday = (clean.groupby("요일_숫자")[TARGET].median()
                   .rename("all_weekday_typical").reset_index())

    out = df.merge(station_month, on=["고객번호", "월"], how="left")
    out = out.merge(station_weekday, on=["고객번호", "요일_숫자"], how="left")
    out = out.merge(all_month, on="월", how="left")
    out = out.merge(all_weekday, on="요일_숫자", how="left")
    return out


def _add_level_relative_features(df: pd.DataFrame) -> pd.DataFrame:
    """톤 단위 피처를 그 역의 최근 수준으로 나눈 값(rel_*)과 수준끼리의 비율을 함께 준다.

    역마다 사용량 규모가 열 배씩 달라도 '평소의 몇 배'라는 같은 모양으로 배우게 한다.
    """
    level = recent_level(df)
    ton_cols = [c for c in df.columns if (c.startswith("usage_") and not c.endswith("_ratio"))
                or c in ("bill_daily_avg", "station_month_typical", "station_weekday_typical")]
    new = {f"rel_{c}": df[c].to_numpy(dtype=float) / level for c in ton_cols}
    week = df["usage_median_last7days"]
    for days in (28, 56):   # 최근 1주 수준이 지난 4주·8주 수준의 몇 배인지
        longer = df[f"usage_median_last{days}days"].fillna(week).to_numpy(dtype=float)
        new[f"usage_last7_vs_last{days}days_ratio"] = week.to_numpy(dtype=float) / np.maximum(longer, 1.0)
    last_year = df[["usage_364days_ago", "usage_365days_ago", "usage_366days_ago", "usage_371days_ago"]].median(axis=1)
    new["usage_last_year_vs_now_ratio"] = last_year.to_numpy(dtype=float) / level
    return pd.concat([df, pd.DataFrame(new, index=df.index)], axis=1)


def _normal_rows(df: pd.DataFrame) -> pd.Series:
    """평소수준 통계 계산에 쓸 정상 행 표시(역별 IQR×3 범위 안 + 음수 제외) — 극단치가 중앙값을 흔들지 않게."""
    keep = pd.Series(True, index=df.index)
    for _, idx in df.groupby("고객번호").groups.items():
        values = df.loc[idx, TARGET]
        q1, q3 = values.quantile(0.25), values.quantile(0.75)
        iqr = q3 - q1
        keep.loc[idx] = (values >= q1 - 3.0 * iqr) & (values <= q3 + 3.0 * iqr) & (values >= 0)
    return keep


def _fill_missing(df: pd.DataFrame) -> pd.DataFrame:
    """결측 피처를 단계적으로 메운다.

    bill_daily_avg : 역×월 평소값 → 전체×월 평소값 → train 중앙값 순으로 보충.
    핵심 과거값 피처 : bill_daily_avg(그 역의 평소 수준)로 보충.
    """
    out = df.copy()
    out["bill_daily_avg"] = (
        out["bill_daily_avg"]
        .fillna(out["station_month_typical"])
        .fillna(out["all_month_typical"])
        .fillna(out[TARGET].where(out["split"] == "train").median())
    )
    for col in ["usage_1days_ago", "usage_2days_ago", "usage_3days_ago", "usage_7days_ago",
                "usage_avg_last7days", "usage_median_last7days"]:
        if col in out:
            out[col] = out[col].fillna(out["bill_daily_avg"])
    return out
