"""Small shared helpers for collectors; credentials and observations stay private."""
from __future__ import annotations

import csv
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
from back.api.config import DATA_DIR, HISTORY_DIR
RUNTIME = DATA_DIR
HISTORY = HISTORY_DIR   # 과거 이력 자료(저장소 밖, APP_DATA_DIR/history)


def today():
    return datetime.now(ZoneInfo("Asia/Seoul")).date()


def customer_number(value) -> str:
    value = str(value).strip()
    if value.endswith(".0"):
        value = value[:-2]
    if not value.isdigit() or len(value) > 9:
        raise ValueError("Invalid Arisu customer number")
    return value.zfill(9)


def collector_meters(meters=None, *, daily=True):
    if meters is None:
        from back.api.catalog import collector_meters as registered
        meters = registered()
    return [m for m in meters if m.get("active", True) and not m.get("deleted_at")
            and m.get("provider", "arisu") == "arisu"
            and (not daily or m.get("daily_enabled", False))]


@contextmanager
def collection_lock(runtime):
    """Serialize web workers and CLI writes across processes on the server."""
    import fcntl
    runtime = Path(runtime)
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime / ".collection.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def atomic_text(path: Path, text: str, *, encoding="utf-8"):
    atomic_bytes(path, text.encode(encoding))


def atomic_bytes(path: Path, content: bytes):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fp:
            fp.write(content)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def write_json(path: Path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


def merge_csv(path: Path, rows, fields, keys):
    """Upsert successful observations without discarding rows from failed requests."""
    import io
    previous = []
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as fp:
            previous = list(csv.DictReader(fp))
    merged = {tuple(str(r.get(k, "")) for k in keys): r for r in previous}
    for row in rows:
        merged[tuple(str(row.get(k, "")) for k in keys)] = row
    if not merged:
        return 0
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(merged[k] for k in sorted(merged))
    atomic_text(path, buffer.getvalue(), encoding="utf-8-sig")
    return len(merged)
