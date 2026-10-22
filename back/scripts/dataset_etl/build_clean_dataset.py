"""현재 모델 갱신에서 사용하는 청구 집계. 과거 일괄 변환 CLI는 archive에 보관한다."""
from pathlib import Path

import numpy as np
import pandas as pd


def build_bills_bimonthly(bills_path: Path, meter_labels: pd.DataFrame) -> pd.DataFrame:
    """모든 청구를 **2개월 단위**로 통일.

    - 원래 격월(2개월) 청구: 그대로 유지. period = (납기월-1, 납기월)
    - 원래 월별(1개월) 청구: 인접한 2개의 월별 청구를 합쳐 1개 격월 행으로 집계
                            (홀수월 청구는 다음달과, 짝수월 청구는 이전달과 짝지음)
    """
    columns = ["고객번호", "역명", "시작연월", "종료연월", "포함월수", "격월사용량_톤", "격월부과금액_원"]
    b = pd.read_csv(bills_path, encoding="utf-8-sig", dtype={"고객번호": str})
    b["납기일"] = pd.to_datetime(b["납기일"], errors="coerce")
    b = b.dropna(subset=["납기일"]).sort_values(["고객번호", "납기일"]).reset_index(drop=True)
    if b.empty:
        return pd.DataFrame(columns=columns)
    for field in ("총사용량_톤", "부과금액_원"):
        b[field] = pd.to_numeric(b[field], errors="coerce").replace([np.inf, -np.inf], np.nan)

    # 역명을 다른 CSV들과 통일된 정규화 형태로 교체
    meter_norm = meter_labels.copy()
    meter_norm = meter_norm.rename(columns={"역명": "역명_정규화"})
    b = b.merge(meter_norm, on="고객번호", how="left")
    b["역명"] = b["역명_정규화"].fillna(b["역명"])
    b = b.drop(columns=["역명_정규화"])

    median_gaps = b.groupby("고객번호")["납기일"].apply(lambda s: s.diff().dt.days.median())
    mkey_cycle: dict[str, int] = {
        mkey: (1 if (gap is not None and not pd.isna(gap) and gap < 40) else 2)
        for mkey, gap in median_gaps.items()
    }

    def assign_bucket(napgi: pd.Timestamp, cycle: int) -> tuple[str, str]:
        if cycle == 2:
            start = napgi - pd.DateOffset(months=1)
            end = napgi
        else:
            if napgi.month % 2 == 0:
                start = napgi - pd.DateOffset(months=1)
                end = napgi
            else:
                start = napgi
                end = napgi + pd.DateOffset(months=1)
        return start.strftime("%Y-%m"), end.strftime("%Y-%m")

    bucket_rows: list[dict] = []
    for r in b.itertuples(index=False):
        cycle = mkey_cycle.get(r.고객번호, 2)
        start_ym, end_ym = assign_bucket(r.납기일, cycle)
        bucket_rows.append(
            {
                "고객번호": r.고객번호,
                "역명": r.역명,
                "영업사업소": r.영업사업소,
                "용도": r.용도,
                "시작연월": start_ym,
                "종료연월": end_ym,
                "원본_청구주기_개월": cycle,
                "납기월": r.납기일.strftime("%Y-%m"),
                "사용량_톤": float(r.총사용량_톤),
                "부과금액_원": float(r.부과금액_원),
            }
        )
    df = pd.DataFrame(bucket_rows)

    agg = (
        df.groupby(
            ["고객번호", "역명", "영업사업소", "용도",
             "시작연월", "종료연월", "원본_청구주기_개월"],
            as_index=False,
            dropna=False,
        )
        .agg(
            청구월수=("납기월", "nunique"),
            격월사용량_톤=("사용량_톤", lambda values: values.sum(min_count=len(values))),
            격월부과금액_원=("부과금액_원", lambda values: values.sum(min_count=len(values))),
        )
    )
    agg["포함월수"] = agg.apply(
        lambda r: 2 if (r["원본_청구주기_개월"] == 2 or r["청구월수"] == 2) else 1,
        axis=1,
    ).astype(int)

    # 역명 join (사람이 보기 쉽게)
    labels = meter_labels
    agg = agg.merge(labels, on="고객번호", how="left", suffixes=("", "_정답"))
    if "역명_정답" in agg.columns:
        agg["역명"] = agg["역명_정답"].fillna(agg["역명"])
        agg = agg.drop(columns=["역명_정답"])
    out = agg[columns].sort_values(["역명", "고객번호", "시작연월"]).reset_index(drop=True)
    return out
