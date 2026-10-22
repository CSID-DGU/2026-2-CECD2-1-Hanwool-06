#!/usr/bin/env python3
"""Log into i121.seoul.go.kr and extract the full mkey dropdown.

Outputs:
    data/raw/billing_i121/i121_mkeys.json       — list of mkey strings
    data/raw/billing_i121/i121_cache/myarisu_initial.html  — raw landing HTML (debug)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT))

from i121_crawler.auth import MYARISU_URL, session_from_env, _looks_like_login_page, LoginError  # noqa: E402
from i121_crawler.parser import discover_mkeys  # noqa: E402
from back.pipelines.common import RUNTIME, atomic_text, write_json
RAW_DIR = RUNTIME / "raw" / "billing_i121"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out-path",
        type=Path,
        default=RAW_DIR / "i121_mkeys.json",
        help="where to save the extracted mkey list",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=RAW_DIR / "i121_cache",
        help="where to save the raw landing HTML for debugging",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    args.out_path.parent.mkdir(parents=True, exist_ok=True)

    session = session_from_env()
    print("login ok", flush=True)

    response = session.get(MYARISU_URL, params={"_m": "m6"}, timeout=30)
    response.raise_for_status()
    if _looks_like_login_page(response.text):
        raise LoginError("Arisu session expired")
    html_path = args.cache_dir / "myarisu_initial.html"
    atomic_text(html_path, response.text)
    print(f"saved landing html → {html_path}", flush=True)

    mkeys = discover_mkeys(response.text)
    if not mkeys:
        print(
            "ERROR: no mkeys found in the dropdown. "
            "Inspect the saved HTML to see what the page actually returned.",
            file=sys.stderr,
        )
        return 1

    write_json(args.out_path, mkeys)
    print(f"discovered {len(mkeys)} mkeys → {args.out_path}")
    for k in mkeys[:5]:
        print(f"  {k}")
    if len(mkeys) > 5:
        print(f"  ...({len(mkeys) - 5} more)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
