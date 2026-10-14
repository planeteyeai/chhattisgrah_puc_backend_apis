"""Parallel backfill of stored classwise responses.

Calls the live ASGI app in-process. Raster work still runs on worker threads
inside those endpoints.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Any

import httpx

from app.api_response_storage import has_end_date
from app.config import settings
from app.indices_data import list_village_locations

logger = logging.getLogger(__name__)

CLASSWISE_PATHS = (
    "/pest-detectionclasswise",
    "/analyze_Growthclasswise",
    "/wateruptakeclasswise",
    "/SoilMoistureclasswise",
)
PRODUCTS = ("pest_detection", "growth", "water_uptake", "soil_moisture")

_JOBS: dict[str, dict[str, Any]] = {}
_STOP = threading.Event()
_FULL_THREAD: threading.Thread | None = None
_LOCK = threading.Lock()


def month_windows(months: int, anchor: date | None = None) -> list[tuple[date, date]]:
    """Newest month first. Current month ends today. Older months end on the last day."""
    anchor = anchor or date.today()
    windows: list[tuple[date, date]] = []
    year, month = anchor.year, anchor.month
    count = max(1, int(months))
    for offset in range(count):
        cursor = month - offset
        year_cursor = year
        while cursor <= 0:
            cursor += 12
            year_cursor -= 1
        last_day = calendar.monthrange(year_cursor, cursor)[1]
        month_end = date(year_cursor, cursor, last_day)
        end = min(anchor, month_end) if offset == 0 else month_end
        start = end - timedelta(days=29)
        windows.append((start, end))
    return windows


def _parallelism() -> int:
    return max(1, min(8, int(settings.backfill_parallelism)))


async def _request(method: str, path: str, params: dict) -> httpx.Response:
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    timeout = float(settings.backfill_request_timeout)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver", timeout=timeout) as client:
        return await client.request(method, path, params=params)


def _memory_limited(response: httpx.Response) -> bool:
    if response.status_code != 500:
        return False
    body = response.text.lower()
    return "memory limit" in body or "user memory" in body


def _call(method: str, path: str, params: dict) -> None:
    retries = max(0, int(settings.backfill_memory_retries))
    for attempt in range(retries + 1):
        response = asyncio.run(_request(method, path, params))
        if _memory_limited(response) and attempt < retries:
            time.sleep(12 * (attempt + 1))
            continue
        response.raise_for_status()
        return


def _location_params(district: str, subdistrict: str, village: str, start: date, end: date, include_tiles: bool) -> dict:
    params = {
        "district": district,
        "end_date": end.isoformat(),
        "start_date": start.isoformat(),
        "include_tiles": str(include_tiles).lower(),
    }
    if subdistrict:
        params["subdistrict"] = subdistrict
    if village:
        params["village"] = village
    return params


def _blank(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def location_complete(district: str, subdistrict: str | None, village: str | None, windows: list[tuple[date, date]]) -> bool:
    for _start, end in windows:
        end_text = end.isoformat()
        for product in PRODUCTS:
            if not has_end_date(product, district, subdistrict, village, end_text):
                return False
    return True


def _call_optional(method: str, path: str, params: dict) -> None:
    try:
        _call(method, path, params)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            logger.info("optional backfill endpoint is not implemented: %s", path)
            return
        raise


def run_location(
    _client: object,
    district: str,
    subdistrict: str,
    village: str,
    windows: list[tuple[date, date]],
    include_tiles: bool,
    extra: bool,
    stop: threading.Event | None = None,
) -> None:
    for start, end in windows:
        if stop is not None and stop.is_set():
            return
        params = _location_params(district, subdistrict, village, start, end, include_tiles)
        for path in CLASSWISE_PATHS:
            _call("POST", path, params)
    if extra and windows:
        newest_start, newest_end = windows[0]
        _call_optional(
            "POST",
            "/CanopyHeightAgeStructureclasswise",
            _location_params(district, subdistrict, village, newest_start, newest_end, include_tiles),
        )
        weather = {"district": district}
        if subdistrict:
            weather["subdistrict"] = subdistrict
        if village:
            weather["village"] = village
        _call_optional("GET", "/weather/daily", weather)
        _call_optional("GET", "/weather/wind", weather)


def _full_main() -> None:
    windows = month_windows(14)
    locations = list_village_locations()
    workers = _parallelism()
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [
                pool.submit(run_location, None, district, subdistrict, village, windows, True, True, _STOP)
                for district, subdistrict, village in locations
            ]
            for future in as_completed(futures):
                if _STOP.is_set():
                    break
                try:
                    future.result()
                except Exception:
                    logger.exception("classwise backfill location failed")
    finally:
        global _FULL_THREAD
        with _LOCK:
            _FULL_THREAD = None


def start_full_backfill() -> bool:
    global _FULL_THREAD
    with _LOCK:
        if _FULL_THREAD is not None and _FULL_THREAD.is_alive():
            return False
        _STOP.clear()
        _FULL_THREAD = threading.Thread(target=_full_main, name="classwise-backfill", daemon=True)
        _FULL_THREAD.start()
        return True


def full_backfill_running() -> bool:
    with _LOCK:
        return _FULL_THREAD is not None and _FULL_THREAD.is_alive()


def _daily_job(job_id: str, district: str | None, subdistrict: str | None, village: str | None, months: int) -> None:
    job = _JOBS[job_id]
    windows = month_windows(months)
    if district:
        locations = [(district, subdistrict or "", village or "")]
    else:
        locations = list_village_locations()
    job["total"] = len(locations)
    job["status"] = "running"
    stop = job["stop"]
    try:
        for loc_district, loc_sub, loc_village in locations:
            if stop.is_set():
                job["status"] = "stopped"
                return
            job["current"] = [loc_district, loc_sub, loc_village]
            if location_complete(loc_district, _blank(loc_sub), _blank(loc_village), windows):
                job["skipped"] += 1
                job["completed"] += 1
                continue
            try:
                run_location(None, loc_district, loc_sub, loc_village, windows, True, False, stop)
            except Exception as exc:
                logger.exception("daily backfill failed")
                job["errors"].append(str(exc))
            job["completed"] += 1
        if job["status"] == "running":
            job["status"] = "stopped" if stop.is_set() else "done"
    except Exception as exc:
        job["status"] = "error"
        job["errors"].append(str(exc))
    finally:
        job["finished_at"] = datetime.utcnow().isoformat()


def start_daily_job(district: str | None, subdistrict: str | None, village: str | None, months: int) -> str:
    months = max(1, min(24, int(months or 15)))
    job_id = uuid.uuid4().hex
    _JOBS[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "district": district,
        "subdistrict": subdistrict,
        "village": village,
        "months": months,
        "total": 0,
        "completed": 0,
        "skipped": 0,
        "errors": [],
        "current": None,
        "started_at": datetime.utcnow().isoformat(),
        "finished_at": None,
        "stop": threading.Event(),
    }
    thread = threading.Thread(
        target=_daily_job,
        args=(job_id, district, subdistrict, village, months),
        name=f"classwise-daily-{job_id[:8]}",
        daemon=True,
    )
    _JOBS[job_id]["thread"] = thread
    thread.start()
    return job_id


def job_status(job_id: str) -> dict[str, Any] | None:
    job = _JOBS.get(job_id)
    if job is None:
        return None
    return {key: value for key, value in job.items() if key not in {"stop", "thread"}}


def stop_all_jobs() -> int:
    _STOP.set()
    stopped = 0
    for job in _JOBS.values():
        if job["status"] in {"queued", "running"}:
            job["stop"].set()
            stopped += 1
    return stopped
