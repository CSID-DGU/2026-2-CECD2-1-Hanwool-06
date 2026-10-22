#!/usr/bin/env python3
"""Collect registered contracts' bills for an explicit month range into private storage.

Recent cache windows are refreshed; empty windows never stop historical backfill.
Existing observations survive partial failures and regular/supplementary bills remain distinct.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from back.scripts.billing_etl.i121_crawler.auth import collection_session  # noqa: E402
from back.pipelines.common import RUNTIME, collection_lock, collector_meters, customer_number, merge_csv, write_json, today
from back.scripts.billing_etl.i121_crawler.fetch import fetch_bill_window, shift_month  # noqa: E402
from back.scripts.billing_etl.i121_crawler.parser import parse_bill_list
from back.scripts.billing_etl.i121_crawler.public import fetch_public_window, PublicCustomerNameError  # noqa: E402
RAW_DIR = RUNTIME / "raw" / "billing_i121"


FIELDNAMES = [
    "mkey",
    "notice_number",
    "sunbeon",
    "gubun",
    "napgi",
    "napgi_compact",
    "sunap_status",
    "bugwa_amount_won",
    "total_usage_ton",
    "station_from_address",
    "address",
    "details",
    "detail_source",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mkeys-path", type=Path, help="optional explicit customer-number JSON; defaults to the current meter registry")
    parser.add_argument("--cache-dir", type=Path, default=RAW_DIR / "i121_cache")
    parser.add_argument("--out-dir", type=Path, default=RAW_DIR / "i121_bills")
    parser.add_argument("--sleep", type=float, default=1.5, help="seconds to sleep between live fetches")
    parser.add_argument("--floor-year", type=int, default=2008, help="stop iterating below this year")
    parser.add_argument("--max-mkeys", type=int, default=None, help="limit to first N mkeys (debug)")
    parser.add_argument("--end-ym", type=str, default=None, help="latest YYYY-MM to start from (default: today)")
    parser.add_argument("--start-ym", type=str, default=None)
    return parser.parse_args()


def collect_bills(*, start_ym, end_ym, mkeys=None, session=None, cache_dir=None, out_dir=None, sleep=1.5, include_rows=False, customer_names=None, known_details=None):
    if start_ym > end_ym:
        raise ValueError("start month must be before end month")
    customer_names = dict(customer_names or {})
    if mkeys is None:
        meters = collector_meters(daily=False)
        mkeys = [m["customer_number"] for m in meters]
        for meter in meters:
            customer_names.setdefault(meter["customer_number"], meter.get("metadata", {}).get("arisu_customer_name", ""))
    customer_names = {customer_number(k): str(v).strip() for k, v in customer_names.items() if v}
    mkeys = sorted({customer_number(k) for k in mkeys})
    if not mkeys:
        raise ValueError("No registered Arisu contracts")
    cache_dir = Path(cache_dir or RAW_DIR / "i121_cache")
    out_dir = Path(out_dir or RAW_DIR / "i121_bills")
    session = session or collection_session()
    bills, errors = [], []
    for mkey in mkeys:
        window_end = end_ym
        while window_end >= start_ym:
            window_start = max(start_ym, shift_month(window_end, -11))
            try:
                force = window_end >= shift_month(today().strftime("%Y-%m"), -2)
                if customer_names.get(mkey):
                    rows, cached, detail_errors = fetch_public_window(session, mkey, customer_names[mkey],
                        window_start, window_end, cache_dir, force=force, known_details=known_details)
                    errors.extend(detail_errors)
                else:
                    html, cached = fetch_bill_window(session, mkey, window_start, window_end, cache_dir, force=force)
                    rows = parse_bill_list(html, customer=mkey)
                for bill in rows:
                    bill["mkey"] = customer_number(bill["mkey"])
                    if bill["mkey"] != mkey:
                        raise ValueError("Bill customer does not match requested contract")
                    date.fromisoformat(bill["napgi"])
                    bills.append(bill)
                if not cached and sleep:
                    time.sleep(sleep)
            except Exception as exc:
                errors.append({"customer_number": mkey, "start": window_start,
                               "end": window_end, "error": type(exc).__name__})
                if isinstance(exc, PublicCustomerNameError):
                    break
            window_end = shift_month(window_start, -1)
    # Regular and supplementary bills can share a due date; retain both.
    path = out_dir / "bills_long.csv"
    keys = ["mkey", "napgi", "gubun", "notice_number"]
    previous = {}
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as stream:
            previous = {tuple(row.get(k, "") for k in keys): row for row in csv.DictReader(stream)}
    stored_rows = []
    for bill in bills:
        stored = dict(bill)
        old = previous.get(tuple(str(bill.get(k, "")) for k in keys), {})
        for field in ("details", "detail_source", "address", "station_from_address", "total_usage_ton"):
            if stored.get(field) is None or stored.get(field) == "":
                if old.get(field):
                    stored[field] = old[field]
        if isinstance(stored.get("details"), dict):
            stored["details"] = json.dumps(stored["details"], ensure_ascii=False, allow_nan=False)
        stored_rows.append(stored)
    count = merge_csv(path, stored_rows, FIELDNAMES, keys)
    report = {"start": start_ym, "end": end_ym, "customers": len(mkeys),
              "fetched": len(bills), "total": count, "errors": errors,
              "status": "failed" if errors else "complete"}
    write_json(out_dir / "crawl_summary.json", report)
    return report | {"rows": bills} if include_rows else report


def main() -> int:
    args = parse_args()
    meters = collector_meters(daily=False)
    names = {m["customer_number"]: m.get("metadata", {}).get("arisu_customer_name", "") for m in meters}
    mkeys = json.loads(args.mkeys_path.read_text(encoding="utf-8")) if args.mkeys_path else list(names)
    if args.max_mkeys is not None:
        if args.max_mkeys < 1:
            raise ValueError("max-mkeys must be positive")
        mkeys = mkeys[:args.max_mkeys]
    with collection_lock(RUNTIME):
        report = collect_bills(start_ym=args.start_ym or f"{args.floor_year}-01",
            end_ym=args.end_ym or today().strftime("%Y-%m"), mkeys=mkeys,
            cache_dir=args.cache_dir, out_dir=args.out_dir, sleep=args.sleep,
            session=collection_session(), customer_names=names)
    print(report)
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
