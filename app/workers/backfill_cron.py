"""Current-month classwise backfill that resumes from a checkpoint."""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Any

from app.api_response_storage import read_checkpoint, write_checkpoint
from app.indices_data import list_village_locations
from app.workers.backfill_parallel import location_complete, month_windows, run_location

logger = logging.getLogger(__name__)

_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_LOCK = threading.Lock()
STATUS: dict[str, Any] = {
    "running": False,
    "status": "stopped",
    "current_location": None,
    "year_month": None,
    "locations_done": 0,
    "locations_total": 0,
    "last_error": None,
    "next_run_at": None,
}


def _set(**values: Any) -> None:
    with _LOCK:
        STATUS.update(values)


def _next_first(now: datetime) -> datetime:
    year = now.year + (1 if now.month == 12 else 0)
    month = 1 if now.month == 12 else now.month + 1
    return datetime(year, month, 1, 0, 5, tzinfo=timezone.utc)


def _sleep_until(moment: datetime) -> bool:
    while datetime.now(timezone.utc) < moment:
        if _STOP.wait(5):
            return True
    return False


def _resume_index(locations: list[tuple[str, str, str]], year_month: str) -> int:
    checkpoint = read_checkpoint() or {}
    if checkpoint.get("year_month") != year_month or not checkpoint.get("district"):
        return 0
    target = (
        checkpoint.get("district") or "",
        checkpoint.get("subdistrict") or "",
        checkpoint.get("village") or "",
    )
    for index, location in enumerate(locations):
        if location == target:
            return index
    return 0


def _run_month() -> None:
    windows = month_windows(1)
    _start, end = windows[0]
    year_month = end.isoformat()[:7]
    locations = list_village_locations()
    begin = _resume_index(locations, year_month)
    _set(year_month=year_month, locations_total=len(locations), locations_done=begin, status="running")
    for index, (district, subdistrict, village) in enumerate(locations[begin:], start=begin + 1):
        if _STOP.is_set():
            return
        _set(current_location=[district, subdistrict, village])
        write_checkpoint(district, subdistrict or None, village or None, year_month)
        if location_complete(district, subdistrict or None, village or None, windows):
            _set(locations_done=index)
            continue
        try:
            run_location(None, district, subdistrict, village, windows, True, False, _STOP)
        except Exception as exc:
            logger.exception("backfill cron failed for %s", district)
            _set(last_error=str(exc))
        _set(locations_done=index)


def _main() -> None:
    try:
        while not _STOP.is_set():
            _run_month()
            if _STOP.is_set():
                break
            nxt = _next_first(datetime.now(timezone.utc))
            _set(status="sleeping", next_run_at=nxt.isoformat(), current_location=None)
            if _sleep_until(nxt):
                break
    finally:
        _set(running=False, status="stopped", current_location=None, next_run_at=None)


def start_backfill_cron() -> bool:
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return False
        _STOP.clear()
        STATUS.update(
            {
                "running": True,
                "status": "starting",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "last_error": None,
                "next_run_at": None,
            }
        )
        _THREAD = threading.Thread(target=_main, name="classwise-backfill-cron", daemon=False)
        _THREAD.start()
        return True


def stop_backfill_cron() -> bool:
    with _LOCK:
        running = _THREAD is not None and _THREAD.is_alive()
    if not running:
        return False
    _STOP.set()
    return True


def backfill_cron_status() -> dict[str, Any]:
    with _LOCK:
        payload = dict(STATUS)
    payload["running"] = _THREAD is not None and _THREAD.is_alive()
    return payload


def seconds_until_next_month() -> float:
    """Kept for tests. Unused by the sleep loop."""
    nxt = _next_first(datetime.now(timezone.utc))
    return max(0.0, (nxt - datetime.now(timezone.utc)).total_seconds())
