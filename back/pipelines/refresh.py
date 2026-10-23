"""Refresh private observations and publish a validated, versioned snapshot.

Run from the repository root: python -m back.pipelines.refresh --start YYYY-MM-DD
--end YYYY-MM-DD --water --ridership --bills --model. No flags means no network work.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from back.pipelines.common import HISTORY, RUNTIME, atomic_text, collection_lock, collector_meters, customer_number, today, write_json


def _read_csv(path):
    import pandas as pd
    return pd.read_csv(path, encoding="utf-8-sig", dtype={"고객번호": str})


# 원격검침 값이 그대로인 날(0톤이 이어짐)은 원인(단말·계량기·실제 미사용)을 가릴 수 없어 '자료 확인'으로 둔다.
# 0톤만 본다: 수집 원자료에서 지침이 그대로인 날은 모두 0톤이었고, 그 반대는 없었다.
STALL_MIN_ZERO_DAYS = 3     # 0톤 관측이 이만큼 이어지면 그 구간 전체. 모델은 4~5일째부터 0톤을 '정상'으로 본다
STALL_MIN_USUAL_TON = 3.0   # 멈추기 전 평소(직전 28관측 중앙값)가 이 이상인 계량기만. 지침 단위가 1톤이라 적게 쓰면 0톤이 정상일 수 있다

# 직전 실행의 분할이 없을 때 쓰는 학습·검증 경계(2026-10-21 정함). 검증에 여름이 들어가고, 학습 이력이 모자라
# 판정하지 못하던 계량기도 판정된다. 경계를 옮길 때는 --train-end/--valid-end를 한 번 넘긴다(분기마다 권장).
DEFAULT_TRAIN_END, DEFAULT_VALID_END = "2026-04-30", "2026-08-31"


def stalled_days(master):
    """{(고객번호, 날짜): {reason, since, days, usual}}: 원격검침 값이 그대로(0톤)인 날과, 그 뒤 다시 움직인 첫날.

    빠진 날 바로 다음 0톤은 아리수 장애 직후 값일 수 있어 구간의 시작으로 치지 않지만, 시작한 구간은 빠진 날이 있어도
    끊지 않는다(긴 멈춤 중에 수집이 며칠 끊겼다고 모델 판정으로 돌아가지 않게). 날수는 빠진 날을 포함한 달력 날수다.
    평소량은 구간이 시작될 때 직전 28관측(앞서 표시한 날은 뺀다)으로 정해 끝날 때까지 바꾸지 않는다. 앞선 멈춤의 0톤이
    평소량에 들어가면 규칙이 스스로 꺼진다. 다시 움직인 첫날은 그동안의 양이 몰렸을 수 있어 함께 표시한다.
    매번 전체 자료로 다시 계산하므로 값이 움직이면 저절로 풀린다.
    """
    from statistics import median
    found = {}
    if master.empty:
        return found
    for cid, group in master.sort_values(["고객번호", "날짜"]).groupby("고객번호", sort=False):
        days = [date.fromisoformat(str(d)) for d in group["날짜"]]
        values = [float(v) for v in group["일사용량_톤"]]
        run, marked = [], set()

        def close(resume):
            if len(run) < STALL_MIN_ZERO_DAYS:
                return
            before = [values[j] for j in range(run[0]) if j not in marked][-28:]
            if len(before) < 7 or median(before) < STALL_MIN_USUAL_TON:
                return
            since, usual = days[run[0]], round(median(before), 1)
            for i in run:
                found[(cid, days[i].isoformat())] = {"reason": "값 그대로", "since": since.isoformat(), "days": (days[i] - since).days + 1, "usual": usual}
            marked.update(run)
            if resume is not None:
                marked.add(resume)
                found[(cid, days[resume].isoformat())] = {"reason": "다시 움직인 첫날", "since": since.isoformat(),
                                                          "days": (days[run[-1]] - since).days + 1, "usual": usual}

        for i, value in enumerate(values):
            if value == 0 and (run or (i and (days[i] - days[i - 1]).days == 1)):
                run.append(i)
                continue
            close(i if value > 0 else None)
            run = []
        close(None)
    return found


def build_dataset(output_dir, *, meters, data_root=None, daily_root=None, train_end=None, valid_end=None, test_end=None):
    """Build by customer number, keeping missing ridership distinct from zero."""
    import numpy as np
    import pandas as pd
    data_root = Path(data_root or HISTORY)
    if not (data_root / "processed" / "daily_water_usage.csv").is_file():
        raise FileNotFoundError(f"이력 자료가 없습니다: {data_root}. Object Storage의 history/를 받으세요(python -m back.storage restore <APP_DATA_DIR>)")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    mapping = {customer_number(m["customer_number"]): m for m in meters}
    daily = _read_csv(data_root / "processed" / "daily_water_usage.csv")
    # 날짜는 물을 쓴 날(사용일)이다. 과거 자료의 검침일 D 값은 D-1 하루 사용분(검침일자 = 사용일 + 1)이라 하루 당긴다.
    daily["날짜"] = (pd.to_datetime(daily["검침일"], errors="raise") - pd.Timedelta(days=1)).dt.strftime("%Y-%m-%d")
    rides = _read_csv(data_root / "processed" / "ridership.csv").rename(columns={"사용일": "날짜"})
    rides["총승객수"] = rides["승차총승객수"] + rides["하차총승객수"]
    # Legacy rows identify observations by a reviewed CSV alias, never by a fuzzy station match.
    alias = _read_csv(data_root / "billing" / "meter_match.csv")
    name_to_customer = dict(zip(alias["일일CSV파일명"], alias["고객번호"].str.zfill(9)))
    water_parts = [daily[["고객번호", "날짜", "일사용량_톤"]]]
    ride_parts = [rides[["고객번호", "날짜", "승차총승객수", "하차총승객수", "총승객수"]]]
    sources = [data_root / "daily", Path(daily_root or RUNTIME / "daily")]
    for source in dict.fromkeys(sources):
        for path in sorted((source / "water").glob("*.csv")):
            frame = _read_csv(path)
            if frame.empty:
                continue
            if "고객번호" not in frame:
                frame["고객번호"] = frame["역명"].map(name_to_customer)
            if frame["고객번호"].isna().any():
                raise ValueError(f"Unmapped historical water rows in {path.name}")
            frame = frame.rename(columns={"사용일": "날짜", "일사용량(톤)": "일사용량_톤"})
            water_parts.append(frame[["고객번호", "날짜", "일사용량_톤"]])
        for path in sorted((source / "ridership").glob("*.csv")):
            frame = _read_csv(path)
            if not frame.empty:
                ride_parts.append(frame[["고객번호", "날짜", "총승객수"]])
    water = pd.concat(water_parts, ignore_index=True)
    riders = pd.concat(ride_parts, ignore_index=True)
    for frame in (water, riders):
        frame["고객번호"] = frame["고객번호"].map(customer_number)
        frame["날짜"] = pd.to_datetime(frame["날짜"], errors="raise").dt.strftime("%Y-%m-%d")
    water["일사용량_톤"] = pd.to_numeric(water["일사용량_톤"].astype(str).str.replace(",", ""), errors="coerce")
    water = water[water["고객번호"].isin(mapping)].drop_duplicates(["고객번호", "날짜"], keep="last")
    water = water[np.isfinite(water["일사용량_톤"]) & (water["일사용량_톤"] >= 0)]
    riders = riders[riders["고객번호"].isin(mapping)].drop_duplicates(["고객번호", "날짜"], keep="last")
    master = water.merge(riders, on=["고객번호", "날짜"], how="left", validate="one_to_one")
    for target, field in [("역명", "station_name"), ("사업소명", "office_name"), ("고지서_성명", "station_name")]:
        master[target] = master["고객번호"].map(lambda key: mapping[key].get(field) or "")
    dt = pd.to_datetime(master["날짜"])
    master["요일"] = dt.dt.weekday.map(dict(enumerate("월화수목금토일")))
    master["일유형"] = pd.NA
    master = master.sort_values(["고객번호", "날짜"]).reset_index(drop=True)
    def split_end(folder, name):   # a split written empty has no boundary to offer
        path = folder / f"{name}.csv"
        dates = _read_csv(path)["날짜"].dropna() if path.exists() else []
        return str(max(dates)) if len(dates) else None

    # Preserve established evaluation cutoffs unless explicitly retraining with new boundaries:
    # the previous runtime split if it holds rows, else the default cutoffs above.
    previous = output_dir if split_end(output_dir, "train") and split_end(output_dir, "valid") else None
    train_end = train_end or (split_end(previous, "train") if previous else DEFAULT_TRAIN_END)
    valid_end = valid_end or (split_end(previous, "valid") if previous else DEFAULT_VALID_END)
    for boundary in filter(None, (train_end, valid_end, test_end)):
        date.fromisoformat(str(boundary))   # never compare against "nan" or another non-date
    if train_end >= valid_end or test_end and valid_end >= test_end:
        raise ValueError("train_end must precede valid_end, and valid_end must precede test_end")
    # test_end only bounds what the model is evaluated on; master keeps every observation for the snapshot.
    splits = {"train": master[master["날짜"] <= train_end],
              "valid": master[(master["날짜"] > train_end) & (master["날짜"] <= valid_end)],
              "test": master[(master["날짜"] > valid_end) & (master["날짜"] <= (test_end or master["날짜"].max()))]}
    # 원격검침 값이 그대로인 날은 사용량이 아니므로 학습·검증·채점 모두에서 뺀다. 화면에는 publish_snapshot이
    # '자료 확인'으로 올린다. 이력 특성이 행 기준이라 다시 움직인 뒤에는 멈추기 전 수준과 비교한다.
    stalled = stalled_days(master)
    if stalled:
        for name, frame in splits.items():
            keep = [(c, d) not in stalled for c, d in zip(frame["고객번호"], frame["날짜"])]
            splits[name] = frame.loc[pd.Series(keep, index=frame.index, dtype=bool)]
    train_counts = splits["train"].groupby("고객번호").size()
    valid_counts = splits["valid"].groupby("고객번호").size()
    eligible = {key for key in mapping if train_counts.get(key, 0) >= 90 and valid_counts.get(key, 0) >= 14}
    withheld = [{"customer_number": key, "reason": "학습 90일 및 검증 14일의 이력이 필요합니다",
                 "train_days": int(train_counts.get(key, 0)), "valid_days": int(valid_counts.get(key, 0))}
                for key in mapping if key not in eligible]
    atomic_text(output_dir / "master.csv", master.to_csv(index=False), encoding="utf-8-sig")
    for name, frame in splits.items():
        frame = frame[frame["고객번호"].isin(eligible)]
        atomic_text(output_dir / f"{name}.csv", frame.to_csv(index=False), encoding="utf-8-sig")
    return master, riders, withheld, eligible


def build_calendar(master, output):
    import holidays
    import pandas as pd
    dates = pd.date_range(master["날짜"].min(), master["날짜"].max())
    ko = holidays.KR(years=range(dates[0].year, dates[-1].year + 1), language="ko")
    frame = pd.DataFrame({"날짜": dates.strftime("%Y-%m-%d"), "월": dates.month,
        "요일_숫자": dates.weekday, "주말": (dates.weekday >= 5).astype(int),
        "공휴일": [int(d.date() in ko) for d in dates]})
    frame["휴무일"] = ((frame["주말"] == 1) | (frame["공휴일"] == 1)).astype(int)
    frame["징검다리"] = ((frame["휴무일"] == 0) & (frame["휴무일"].shift(1) == 1) & (frame["휴무일"].shift(-1) == 1)).astype(int)
    atomic_text(output, frame.to_csv(index=False), encoding="utf-8-sig")


def publish_snapshot(master, riders, *, output_dir, anomalies=None, withheld=(), previous_risk=None):
    import math
    daily = {}
    for source, field, value in [(master, "usage", "일사용량_톤"), (riders, "ridership", "총승객수")]:
        for key, group in source.groupby("고객번호", observed=True):
            entry = daily.setdefault(customer_number(key), {"usage": [], "ridership": []})
            entry[field] = [{"date": str(row["날짜"]), "value": float(row[value])}
                            for _, row in group.sort_values("날짜").iterrows() if math.isfinite(float(row[value]))]
    risk = previous_risk or {}
    if anomalies is not None:
        risk = {}
        for _, row in anomalies.iterrows():
            if row["심각도"] == "자료부족" or not math.isfinite(float(row["deviation_score"])):
                continue
            key = customer_number(row["고객번호"])
            risk.setdefault(key, []).append({"date": str(row["날짜"])[:10],
                "actual": float(row["일사용량_톤"]), "pred": float(row["predicted_ton"]),
                "residual": float(row["error_ton"]), "z": float(row["deviation_score"]),
                "severity": row["심각도"], "dir": row["방향"] if isinstance(row["방향"], str) else "",
                "err": bool(row["likely_data_error"])})
    return publish_data(daily, mark_stalled(risk, master, withheld), output_dir=output_dir, withheld=withheld)


def mark_stalled(risk, master, withheld=()):
    """원격검침 값이 그대로인 날을 '자료 확인' 행(err, reason·since·days·usual)으로 올린 새 위험도.

    위험도가 이미 덮는 날짜 범위(모든 계량기의 첫날~마지막 날) 안에만 넣어, 이 행이 위험도 기준일을 앞당기지 않게 한다
    (08:05 승하차 단계는 전날 위험도에 오늘 사용량을 얹어 다시 게시한다). 지난 게시의 reason 행은 버리고 새로 계산하며,
    같은 날의 모델 판정은 이 행으로 바꾼다. 학습 이력이 모자라 판정하지 않는 계량기에는 넣지 않는다.
    멈춘 날이 하나도 없으면 위험도를 그대로 돌려준다.
    """
    stalled = stalled_days(master)
    if not stalled or not any(risk.values()):
        return risk
    dates = [r["date"] for rows in risk.values() for r in rows]
    first, last = min(dates), max(dates)
    skip = {w["customer_number"] for w in withheld}
    actual = {(c, d): float(v) for c, d, v in zip(master["고객번호"], master["날짜"], master["일사용량_톤"])}
    marked = {}
    for (cid, day), info in stalled.items():
        if cid not in skip and first <= day <= last:
            marked.setdefault(cid, {})[day] = {"date": day, "actual": actual[(cid, day)], "pred": None, "residual": None,
                                               "z": None, "severity": "자료 확인", "dir": "", "err": True, **info}
    result = {}
    for cid in [*risk, *(c for c in marked if c not in risk)]:
        rows = {r["date"]: r for r in risk.get(cid, []) if not r.get("reason")} | marked.get(cid, {})
        result[cid] = [rows[day] for day in sorted(rows)]
    return result


def publish_data(daily, risk, *, output_dir, withheld=()):
    output_dir = Path(output_dir)
    version = f"versions/{uuid.uuid4().hex}"
    destination = output_dir / version
    write_json(destination / "daily.json", daily)
    write_json(destination / "risk.json", risk)
    latest = lambda field: max((r["date"] for d in daily.values() for r in d[field]), default=None)
    manifest = {"published": True, "directory": version, "generated_at": datetime.now(timezone.utc).isoformat(),
                "water_as_of": latest("usage"), "ridership_as_of": latest("ridership"),
                "risk_as_of": max((r["date"] for values in risk.values() for r in values), default=None),
                "withheld": list(withheld), "counts": {"daily_customers": len(daily), "risk_customers": len(risk)}}
    # A single atomic pointer publishes both files; failed generation retains the old version.
    write_json(output_dir / "manifest.json", manifest)
    # Keep every version from the last day plus at least the latest three.
    # Readers have a full day to finish using an older manifest pointer.
    try:
        versions = [p for p in (output_dir / "versions").iterdir()
                    if not p.is_symlink() and p.is_dir() and re.fullmatch(r"[0-9a-f]{32}", p.name)]
        versions.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        for old in versions[3:]:
            if old.name != destination.name and old.stat().st_mtime < time.time() - 86400:
                shutil.rmtree(old)
    except OSError:
        pass  # Cleanup failure must not invalidate a published snapshot.
    return manifest


def refresh_bills(start, end, meters, runtime):
    import pandas as pd
    from back.scripts.billing_etl.crawl_bills import collect_bills
    raw_dir = runtime / "raw" / "billing_i121"
    report = collect_bills(start_ym=str(start)[:7], end_ym=str(end)[:7],
        mkeys=[m["customer_number"] for m in collector_meters(meters, daily=False)],
        customer_names={m['customer_number']: m.get('metadata', {}).get('arisu_customer_name', '') for m in meters},
        cache_dir=raw_dir / "i121_cache", out_dir=raw_dir / "i121_bills")
    if report["errors"]:
        raise RuntimeError("Some bill requests failed; raw successes preserved, derived bills unchanged")
    path = raw_dir / "i121_bills" / "bills_long.csv"
    if not path.exists():
        return report
    # Notice numbers are identifiers; blank regular notices and leading zeroes must survive CSV.
    raw = pd.read_csv(path, encoding="utf-8-sig", dtype={"mkey": str, "notice_number": str}, keep_default_na=False)
    from back.api import db
    from back.api.import_data import upsert_bill_summaries
    with db.connect() as conn:
        upsert_bill_summaries(conn, raw.where(pd.notna(raw), None).to_dict("records"))
    build_model_bills(meters, runtime)
    return report


def build_model_bills(meters, runtime):
    """Model bill baseline from the historical export plus every bill collected since; sends no requests."""
    import pandas as pd
    from back.scripts.dataset_etl.build_clean_dataset import build_bills_bimonthly
    path = runtime / "raw" / "billing_i121" / "i121_bills" / "bills_long.csv"
    if not path.exists():
        return
    raw = pd.read_csv(path, encoding="utf-8-sig", dtype={"mkey": str, "notice_number": str}, keep_default_na=False)
    raw = raw[raw["gubun"] == "정기분"].rename(columns={"mkey": "고객번호", "napgi": "납기일", "total_usage_ton": "총사용량_톤", "bugwa_amount_won": "부과금액_원"})
    raw = raw[pd.to_numeric(raw["총사용량_톤"], errors="coerce").notna()]   # an incomplete row never replaces a stored bill
    # Only Arisu contracts have Arisu bills; other providers' contract numbers are free text.
    metadata = {customer_number(m["customer_number"]): m for m in meters if m.get("provider", "arisu") == "arisu"}
    raw["고객번호"] = raw["고객번호"].map(customer_number)
    raw = raw[raw["고객번호"].isin(metadata)]
    for target, field in [("역명", "station_name"), ("영업사업소", "office_name"), ("용도", "purpose")]:
        raw[target] = raw["고객번호"].map(lambda key: metadata[key].get(field))
    old = _read_csv(HISTORY / "billing" / "bills_clean.csv")
    old["고객번호"] = old["고객번호"].map(customer_number)
    merged = pd.concat([old, raw], ignore_index=True).drop_duplicates(["고객번호", "납기일"], keep="last")
    for target, field in [("역명", "station_name"), ("영업사업소", "office_name"), ("용도", "purpose")]:
        merged[target] = merged["고객번호"].map(lambda key: metadata.get(key, {}).get(field)).fillna(merged[target])
    clean_path = runtime / "processed" / "bills_clean.csv"
    atomic_text(clean_path, merged.to_csv(index=False), encoding="utf-8-sig")
    labels = pd.DataFrame([{"고객번호": k, "역명": m["station_name"]} for k, m in metadata.items()])
    result = build_bills_bimonthly(clean_path, meter_labels=labels)
    atomic_text(runtime / "processed" / "water_bills.csv", result.to_csv(index=False), encoding="utf-8-sig")


def run(args):
    with collection_lock(args.runtime_dir):
        return _run(args)


def _run(args):
    import yaml
    from back.api.catalog import collector_meters as registered
    meters = registered()
    runtime = Path(args.runtime_dir)
    runtime.mkdir(parents=True, exist_ok=True)
    report = {"start": str(args.start), "end": str(args.end), "steps": {}, "errors": []}
    if args.start > args.end or args.end >= today():
        raise ValueError("Collect dates through yesterday; start must not exceed end")
    if args.water:
        from back.pipelines.daily_water.scraper import scrape_range
        try:
            result = scrape_range(args.start, args.end, meters=meters, out_dir=runtime / "daily" / "water", runtime_dir=runtime)
            report["steps"]["water"] = result
            if any(r["errors"] for r in result):
                report["errors"].append("water collection failed")
        except Exception as exc:
            report["errors"].append(f"water: {type(exc).__name__}")
    if args.ridership:
        from back.pipelines.daily_ridership.scraper import scrape
        report["steps"]["ridership"] = []
        day = args.start
        while day <= args.end:
            try:
                path = scrape(day, meters=meters, out_dir=runtime / "daily" / "ridership")
                status_path = runtime / "daily" / "ridership" / f"{day}.status.json"
                status = json.loads(status_path.read_text()) if status_path.exists() else {"date": str(day), "status": "collected" if path else "pending"}
                report["steps"]["ridership"].append(status)
            except Exception as exc:
                report["errors"].append(f"ridership {day}: {type(exc).__name__}")
            day += timedelta(days=1)
    if args.bills:
        try:
            report["steps"]["bills"] = refresh_bills(args.start, args.end, meters, runtime)
        except Exception as exc:
            report["errors"].append(f"bills: {type(exc).__name__}")
    if report["errors"]:
        write_json(runtime / "refresh_report.json", report)
        return 1
    if args.model or args.water or args.ridership:
        try:
            dataset_dir = runtime / "dataset"
            active = collector_meters(meters)
            master, riders, withheld, eligible = build_dataset(dataset_dir, meters=active,
                daily_root=runtime / "daily", train_end=args.train_end, valid_end=args.valid_end, test_end=args.test_end)
            if master.empty:
                raise ValueError("No valid water observations available")
            build_calendar(master, dataset_dir / "calendar.csv")
            cfg = yaml.safe_load((ROOT / "back" / "ml" / "lightgbm" / "config.yaml").read_text())
            cfg["data"] = {split: str((dataset_dir / f"{split}.csv").resolve()) for split in ["train", "valid", "test"]}
            cfg["data"]["calendar"] = str((dataset_dir / "calendar.csv").resolve())
            if args.model:
                build_model_bills(meters, runtime)   # bills collected after the historical export count too
            bills_path = runtime / "processed" / "water_bills.csv"
            cfg["data"]["bills"] = str((bills_path if bills_path.exists() else HISTORY / "processed" / "water_bills.csv").resolve())
            cfg["model"]["device"] = "cpu"
            cfg["model"]["n_jobs"] = args.jobs
            cfg["save_plot"] = False
            config_path = dataset_dir / "config.yaml"
            atomic_text(config_path, yaml.safe_dump(cfg, allow_unicode=True))
            previous_risk = None
            anomalies = None
            if args.model and eligible and not _read_csv(dataset_dir / "test.csv").empty:
                from back.ml.lightgbm.main import run as train
                metrics = train(config_path, runtime / "model")
                anomalies = _read_csv(runtime / "model" / "test_anomalies.csv")
                if len(anomalies) != metrics["test"]["n_samples"]:
                    raise ValueError("Saved prediction count differs from model evaluation")
            else:
                if args.model:
                    # The requested model run is impossible: say so and keep the last good risk
                    # instead of publishing an empty one as a success.
                    report["errors"].append("model: 학습·평가할 자료가 부족해 위험도를 새로 계산하지 못했습니다. 이전 위험도를 유지합니다")
                manifest_path = runtime / "snapshot" / "manifest.json"
                if manifest_path.exists():
                    old_manifest = json.loads(manifest_path.read_text())
                    snapshot_root = (runtime / "snapshot").resolve()
                    old_dir = (snapshot_root / old_manifest.get("directory", ".")).resolve()
                    if old_dir != snapshot_root and snapshot_root not in old_dir.parents:
                        raise ValueError("Invalid previous snapshot directory")
                    risk_path = old_dir / "risk.json"
                    if risk_path.exists():
                        previous_risk = json.loads(risk_path.read_text())
                if previous_risk is None:
                    from back.api.catalog import data_sources
                    _, previous_risk = data_sources()
            report["steps"]["snapshot"] = publish_snapshot(master, riders, output_dir=runtime / "snapshot",
                anomalies=anomalies, withheld=withheld, previous_risk=previous_risk)
        except Exception as exc:
            report["errors"].append(f"model: {type(exc).__name__}: {exc}")
    if any(step.get("smart_status_errors") for step in report["steps"].get("water", [])):
        report["errors"].append("아리수 스마트검침 상태를 일부 갱신하지 못했습니다")
    write_json(runtime / "refresh_report.json", report)
    # Everything this run produced is on disk; mirror the changed files to Object Storage (no-op when disabled).
    from back.storage.hooks import after_refresh
    after_refresh(runtime)
    return 1 if report["errors"] else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, default=today() - timedelta(days=5))
    parser.add_argument("--end", type=date.fromisoformat, default=today() - timedelta(days=1))
    for flag in ["water", "ridership", "bills", "model", "all"]:
        parser.add_argument(f"--{flag}", action="store_true")
    parser.add_argument("--runtime-dir", type=Path, default=RUNTIME)
    parser.add_argument("--train-end", type=str)
    parser.add_argument("--valid-end", type=str)
    parser.add_argument("--test-end", type=str, help="last date the model is evaluated on (default: the latest observation)")
    parser.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args()
    if args.all:
        args.water = args.ridership = args.bills = args.model = True
    if not any([args.water, args.ridership, args.bills, args.model]):
        parser.error("Choose at least one of --water --ridership --bills --model (or --all)")
    raise SystemExit(run(args))


if __name__ == "__main__":
    main()
