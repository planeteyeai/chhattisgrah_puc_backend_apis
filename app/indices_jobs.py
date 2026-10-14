"""Scene storage, background workers, and the indices crons."""

from __future__ import annotations

import logging
import multiprocessing
import threading
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.database import engine
from app.indices_stac import compute_scene, list_sentinel2
from app.indices_data import (
    INDEX_COLUMNS,
    ensure_indices_schema,
    list_village_locations,
    location_has_obs_between,
    normalize_location,
    product_stored,
    read_checkpoint,
    resolve_geometry,
    upsert_indices,
    utc_now,
    write_checkpoint,
)

logger = logging.getLogger(__name__)


def _place_label(district: str, subdistrict: str, village: str) -> str:
    return " / ".join(part for part in (district, subdistrict, village) if part)


def _log_progress(district: str, subdistrict: str, village: str, when: date | str, action: str) -> None:
    """Print the place and date to the server terminal. Worker processes do not inherit uvicorn's logger."""
    text = when.isoformat() if isinstance(when, date) else str(when)
    print(f"indices  {_place_label(district, subdistrict, village)}  date {text}  {action}", flush=True)

_LOCK = threading.Lock()
_CRON_STOP = threading.Event()
_CRON_THREAD: threading.Thread | None = None
_CRON_EXECUTOR: ProcessPoolExecutor | None = None
_DAILY_STOP = threading.Event()
_DAILY_THREAD: threading.Thread | None = None
_STORE_JOBS: list[dict[str, Any]] = []

CRON_STATUS: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "status": "idle",
    "current_location": None,
    "current_obs_date": None,
    "locations_done_this_cycle": 0,
    "locations_total_this_cycle": 0,
    "last_error": None,
    "stop_requested": False,
    "next_run_at": None,
}

STORE_STATUS: dict[str, Any] = {
    "running": False,
    "status": "idle",
    "locations_total": 0,
    "locations_done": 0,
    "rows_written": 0,
    "last_error": None,
    "started_at": None,
    "finished_at": None,
}

DAILY_STATUS: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "status": "idle",
    "current_location": None,
    "current_obs_date": None,
    "locations_done_this_cycle": 0,
    "locations_total_this_cycle": 0,
    "last_error": None,
    "stop_requested": False,
    "next_run_at": None,
}


def default_window_half_days(step_days: int) -> int:
    return max(step_days // 2, 1)


def observation_dates(end_date: date, years: int, step_days: int) -> list[date]:
    start = end_date - timedelta(days=int(365.25 * years))
    current = start
    dates: list[date] = []
    while current <= end_date:
        dates.append(current)
        current += timedelta(days=step_days)
    return dates


def clamp_workers(value: int, upper: int) -> int:
    return min(upper, max(1, int(value)))


def _set(holder: dict[str, Any], **values: Any) -> None:
    with _LOCK:
        holder.update(values)


def cron_status() -> dict[str, Any]:
    with _LOCK:
        return dict(CRON_STATUS)


def daily_status() -> dict[str, Any]:
    with _LOCK:
        return dict(DAILY_STATUS)


def store_status() -> dict[str, Any]:
    with _LOCK:
        return dict(STORE_STATUS)


def store_is_running() -> bool:
    with _LOCK:
        return bool(STORE_STATUS["running"])


def _begin_store_progress(total: int) -> None:
    with _LOCK:
        STORE_STATUS.update(
            {
                "running": True,
                "status": "running",
                "locations_total": total,
                "locations_done": 0,
                "rows_written": 0,
                "last_error": None,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None,
            }
        )


def _note_store_result(rows: int, error: str | None) -> None:
    with _LOCK:
        STORE_STATUS["locations_done"] = int(STORE_STATUS["locations_done"]) + 1
        STORE_STATUS["rows_written"] = int(STORE_STATUS["rows_written"]) + max(0, rows)
        if error:
            STORE_STATUS["last_error"] = error


def _finish_store_progress(stopped: bool) -> None:
    with _LOCK:
        STORE_STATUS["running"] = False
        STORE_STATUS["status"] = "stopped" if stopped else "done"
        STORE_STATUS["finished_at"] = datetime.now(timezone.utc).isoformat()


def _location_payload(district: str, subdistrict: str, village: str) -> dict[str, str]:
    return {"district": district, "subdistrict": subdistrict, "village": village}


def _row_from_scene(district: str, subdistrict: str, village: str, scene: dict[str, Any]) -> dict[str, Any]:
    now = utc_now()
    row: dict[str, Any] = {
        "obs_date": scene["obs_date"],
        "district": district,
        "subdistrict": subdistrict,
        "village": village,
        "product_id": scene["product_id"],
        "tile_id": scene.get("tile_id"),
        "cloud_coverage": scene.get("cloud_coverage"),
        "created_at": now,
        "updated_at": now,
    }
    for column in INDEX_COLUMNS:
        row[column] = scene.get(column)
    return row


def store_windows(
    district: str,
    subdistrict: str,
    village: str,
    geometry: dict[str, Any],
    *,
    years: int,
    step_days: int,
    end_date: date,
    window_half_days: int | None,
    include_aux: bool,
    stop: threading.Event | None = None,
) -> dict[str, Any]:
    """Walk observation dates, upsert every Sentinel-2 scene, commit per window."""
    ensure_indices_schema()
    half = default_window_half_days(step_days) if window_half_days is None else window_half_days
    dates = observation_dates(end_date, years, step_days)
    written = 0
    if dates:
        _log_progress(district, subdistrict, village, dates[0], f"start {len(dates)} dates")
    for obs in dates:
        if stop is not None and stop.is_set():
            break
        window_start = obs - timedelta(days=half)
        window_end_exclusive = obs + timedelta(days=half) + timedelta(days=1)
        _log_progress(district, subdistrict, village, obs, "reading")
        try:
            image_ids = list_sentinel2(geometry, window_start, window_end_exclusive)
        except Exception:
            logger.exception("scene list failed for %s %s", district, obs.isoformat())
            _log_progress(district, subdistrict, village, obs, "search failed")
            continue
        scenes: list[dict[str, Any]] = []
        for image_id in image_ids:
            if stop is not None and stop.is_set():
                break
            scene = compute_scene(image_id, geometry, include_aux)
            if scene is not None:
                scenes.append(scene)
        if not scenes:
            _log_progress(district, subdistrict, village, obs, "no scene")
            continue
        with engine.begin() as conn:
            for scene in scenes:
                upsert_indices(conn, _row_from_scene(district, subdistrict, village, scene))
        written += len(scenes)
        for scene in scenes:
            scene_date = scene.get("obs_date") or obs
            _log_progress(district, subdistrict, village, scene_date, "stored")
    return {
        "location": _location_payload(district, subdistrict, village),
        "observation_dates": [item.isoformat() for item in dates],
        "rows_written": written,
    }


def store_location(
    district: str,
    subdistrict: str | None,
    village: str | None,
    *,
    years: int,
    step_days: int,
    end_date: date,
    window_half_days: int | None,
    include_aux: bool = True,
    geometry: dict[str, Any] | None = None,
    stop: threading.Event | None = None,
) -> dict[str, Any]:
    district_name, subdistrict_name, village_name = normalize_location(district, subdistrict, village)
    area = geometry or resolve_geometry(district_name, subdistrict_name, village_name)
    return store_windows(
        district_name,
        subdistrict_name,
        village_name,
        area,
        years=years,
        step_days=step_days,
        end_date=end_date,
        window_half_days=window_half_days,
        include_aux=include_aux,
        stop=stop,
    )


def store_location_task(task: dict[str, Any]) -> dict[str, Any]:
    """Process-pool entry point. Reads Sentinel-2 from Earth Search STAC."""
    end = date.fromisoformat(task["end_date"])
    return store_location(
        task["district"],
        task["subdistrict"],
        task["village"],
        years=int(task["years"]),
        step_days=int(task["step_days"]),
        end_date=end,
        window_half_days=task.get("window_half_days"),
        include_aux=bool(task.get("include_aux", True)),
    )


def _kill_executor(executor: ProcessPoolExecutor | None) -> None:
    if executor is None:
        return
    processes = getattr(executor, "_processes", None) or {}
    for process in list(processes.values()):
        try:
            process.terminate()
        except Exception:
            logger.exception("failed to terminate indices worker")
    try:
        executor.shutdown(wait=False, cancel_futures=True)
    except Exception:
        logger.exception("failed to shut down indices pool")


def _run_pool(tasks: list[dict[str, Any]], workers: int, stop: threading.Event) -> None:
    if not tasks or stop.is_set():
        return
    executor = ProcessPoolExecutor(
        max_workers=workers,
        mp_context=multiprocessing.get_context("spawn"),
    )
    job = {"executor": executor, "stop": stop}
    with _LOCK:
        _STORE_JOBS.append(job)
    futures = []
    try:
        for task in tasks:
            if stop.is_set():
                break
            futures.append(executor.submit(store_location_task, task))
        for future in as_completed(futures):
            if stop.is_set():
                break
            try:
                result = future.result()
                rows = int(result.get("rows_written") or 0) if isinstance(result, dict) else 0
                _note_store_result(rows, None)
            except Exception as exc:
                logger.exception("indices location worker failed")
                _note_store_result(0, str(exc))
    finally:
        if stop.is_set():
            _kill_executor(executor)
        else:
            executor.shutdown(wait=True, cancel_futures=False)
        with _LOCK:
            if job in _STORE_JOBS:
                _STORE_JOBS.remove(job)


def run_store_all(
    *,
    years: int,
    step_days: int,
    end_date: date,
    window_half_days: int | None,
    district: str | None,
    max_locations: int,
    locations: list[tuple[str, str, str]] | None = None,
    stop: threading.Event | None = None,
) -> int:
    if locations is None:
        locations = list_village_locations(district, max_locations)
    halt = stop or threading.Event()
    ordered = sorted(locations, key=lambda item: (0 if item[2] else 1 if item[1] else 2, item[0], item[1], item[2]))
    tasks = [
        {
            "district": item[0],
            "subdistrict": item[1],
            "village": item[2],
            "years": years,
            "step_days": step_days,
            "end_date": end_date.isoformat(),
            "window_half_days": window_half_days,
            "include_aux": True,
        }
        for item in ordered
    ]
    _begin_store_progress(len(locations))
    try:
        _run_pool(tasks, clamp_workers(settings.worker_parallelism, 32), halt)
    finally:
        _finish_store_progress(halt.is_set())
    return len(locations)


def start_store_all_background(**kwargs: Any) -> None:
    stop = threading.Event()
    job: dict[str, Any] = {"executor": None, "stop": stop}

    def _target() -> None:
        try:
            run_store_all(stop=stop, **kwargs)
        except Exception:
            logger.exception("background store-all failed")
        finally:
            with _LOCK:
                if job in _STORE_JOBS:
                    _STORE_JOBS.remove(job)

    thread = threading.Thread(target=_target, name="indices-store-all", daemon=False)
    job["thread"] = thread
    with _LOCK:
        _STORE_JOBS.append(job)
    thread.start()


def abort_store_all() -> bool:
    with _LOCK:
        jobs = list(_STORE_JOBS)
    if not jobs:
        return False
    for job in jobs:
        job["stop"].set()
        _kill_executor(job.get("executor"))
    return True


def _recent_window(step_days: int) -> tuple[date, date]:
    today = datetime.now(timezone.utc).date()
    half = timedelta(days=step_days / 2)
    midnight = datetime(today.year, today.month, today.day)
    return (midnight - half).date(), (midnight + half).date()


def _cron_locations(step_days: int) -> tuple[list[tuple[str, str, str]], int]:
    locations = sorted(
        list_village_locations(),
        key=lambda item: (0 if item[2] else 1 if item[1] else 2, item[0], item[1], item[2]),
    )
    checkpoint = read_checkpoint()
    start_index = 0
    if checkpoint is not None:
        key = (checkpoint["district"], checkpoint["subdistrict"], checkpoint["village"])
        if key in locations:
            start_index = locations.index(key) + 1
    return locations, start_index


def _cron_cycle(years: int, step_days: int) -> None:
    locations, start_index = _cron_locations(step_days)
    today = datetime.now(timezone.utc).date()
    _set(
        CRON_STATUS,
        status="running",
        current_obs_date=today.isoformat(),
        locations_total_this_cycle=len(locations),
        locations_done_this_cycle=start_index,
        next_run_at=None,
        current_location=None,
    )
    window_start, window_end = _recent_window(step_days)
    pending: list[tuple[int, tuple[str, str, str]]] = []
    for index, location in enumerate(locations):
        if _CRON_STOP.is_set():
            return
        if index < start_index:
            continue
        district, subdistrict, village = location
        _set(CRON_STATUS, current_location=[district, subdistrict, village])
        try:
            if location_has_obs_between(district, subdistrict, village, window_start, window_end):
                write_checkpoint(district, subdistrict, village, today)
                done = CRON_STATUS["locations_done_this_cycle"] + 1
                _set(CRON_STATUS, locations_done_this_cycle=done)
                continue
        except Exception as exc:
            logger.exception("indices cron skip check failed")
            _set(CRON_STATUS, last_error=str(exc))
        pending.append((index, location))
    if pending and not _CRON_STOP.is_set():
        tasks = [
            {
                "district": location[0],
                "subdistrict": location[1],
                "village": location[2],
                "years": years,
                "step_days": step_days,
                "end_date": today.isoformat(),
                "window_half_days": None,
                "include_aux": True,
                "index": index,
            }
            for index, location in pending
        ]
        workers = clamp_workers(settings.indices_cron_workers, 8)
        executor = ProcessPoolExecutor(
            max_workers=workers,
            mp_context=multiprocessing.get_context("spawn"),
        )
        with _LOCK:
            global _CRON_EXECUTOR
            _CRON_EXECUTOR = executor
        try:
            future_map = {
                executor.submit(store_location_task, {key: value for key, value in task.items() if key != "index"}): task
                for task in tasks
            }
            finished: set[int] = set()
            for future in as_completed(future_map):
                task = future_map[future]
                location = (task["district"], task["subdistrict"], task["village"])
                try:
                    future.result()
                except Exception as exc:
                    logger.exception("indices cron worker failed for %s", location)
                    _set(CRON_STATUS, last_error=str(exc))
                finished.add(int(task["index"]))
                write_checkpoint(location[0], location[1], location[2], today)
                _set(
                    CRON_STATUS,
                    current_location=[location[0], location[1], location[2]],
                    locations_done_this_cycle=start_index + len(finished) + (len(locations) - start_index - len(pending)),
                )
                if _CRON_STOP.is_set():
                    break
        finally:
            if _CRON_STOP.is_set():
                _kill_executor(executor)
            else:
                executor.shutdown(wait=True, cancel_futures=False)
            with _LOCK:
                if _CRON_EXECUTOR is executor:
                    _CRON_EXECUTOR = None
    if not _CRON_STOP.is_set():
        write_checkpoint(None, None, None, today)


def _sleep_until(when: datetime) -> None:
    _set(CRON_STATUS, status="sleeping", next_run_at=when.isoformat(), current_location=None)
    while datetime.now(timezone.utc) < when:
        if _CRON_STOP.wait(5):
            return


def _cron_main() -> None:
    years = min(30, max(1, int(settings.indices_cron_years)))
    step_days = min(90, max(1, int(settings.indices_cron_step_days)))
    try:
        while not _CRON_STOP.is_set():
            try:
                _cron_cycle(years, step_days)
            except Exception as exc:
                logger.exception("indices cron cycle failed")
                _set(CRON_STATUS, last_error=str(exc))
            if _CRON_STOP.is_set():
                break
            _sleep_until(datetime.now(timezone.utc) + timedelta(days=step_days))
    finally:
        _set(
            CRON_STATUS,
            running=False,
            status="stopped",
            current_location=None,
            next_run_at=None,
        )


def begin_indices_cron() -> bool:
    global _CRON_THREAD
    with _LOCK:
        if _CRON_THREAD is not None and _CRON_THREAD.is_alive():
            return False
        _CRON_STOP.clear()
        CRON_STATUS.update(
            {
                "running": True,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "status": "starting",
                "current_location": None,
                "current_obs_date": None,
                "locations_done_this_cycle": 0,
                "locations_total_this_cycle": 0,
                "last_error": None,
                "stop_requested": False,
                "next_run_at": None,
            }
        )
        _CRON_THREAD = threading.Thread(target=_cron_main, name="indices-cron", daemon=False)
        _CRON_THREAD.start()
        return True


def indices_cron_running() -> bool:
    with _LOCK:
        return _CRON_THREAD is not None and _CRON_THREAD.is_alive()


def stop_indices_cron() -> bool:
    global _CRON_EXECUTOR
    with _LOCK:
        running = _CRON_THREAD is not None and _CRON_THREAD.is_alive()
        if not running:
            return False
        CRON_STATUS["stop_requested"] = True
        executor = _CRON_EXECUTOR
    _CRON_STOP.set()
    _kill_executor(executor)
    return True


def _store_district_scenes(district: str, lookback_days: int) -> None:
    geometry = resolve_geometry(district, "", "")
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=lookback_days)
    end_exclusive = today + timedelta(days=1)
    image_ids = list_sentinel2(geometry, start, end_exclusive)
    for image_id in image_ids:
        if _DAILY_STOP.is_set():
            return
        scene = compute_scene(image_id, geometry, include_aux=False)
        if scene is None:
            continue
        if product_stored(district, "", "", scene["product_id"]):
            continue
        with engine.begin() as conn:
            upsert_indices(conn, _row_from_scene(district, "", "", scene))


def _daily_main() -> None:
    lookback = max(1, int(settings.district_daily_lookback_days))
    try:
        while not _DAILY_STOP.is_set():
            districts = sorted({item[0] for item in list_village_locations()})
            today = datetime.now(timezone.utc).date().isoformat()
            _set(
                DAILY_STATUS,
                status="running",
                current_obs_date=today,
                locations_total_this_cycle=len(districts),
                locations_done_this_cycle=0,
                next_run_at=None,
            )
            for index, district in enumerate(districts, start=1):
                if _DAILY_STOP.is_set():
                    return
                _set(DAILY_STATUS, current_location=[district, "", ""])
                try:
                    _store_district_scenes(district, lookback)
                    from app.workers.district_daily_cron import refresh_district_classwise

                    refresh_district_classwise(district, datetime.now(timezone.utc).date())
                except Exception as exc:
                    logger.exception("district daily cron failed for %s", district)
                    _set(DAILY_STATUS, last_error=str(exc))
                _set(DAILY_STATUS, locations_done_this_cycle=index)
            if _DAILY_STOP.is_set():
                break
            nxt = datetime.now(timezone.utc) + timedelta(days=1)
            _set(DAILY_STATUS, status="sleeping", next_run_at=nxt.isoformat(), current_location=None)
            while datetime.now(timezone.utc) < nxt:
                if _DAILY_STOP.wait(5):
                    return
    finally:
        _set(DAILY_STATUS, running=False, status="stopped", current_location=None, next_run_at=None)


def begin_district_daily_cron() -> bool:
    global _DAILY_THREAD
    with _LOCK:
        if _DAILY_THREAD is not None and _DAILY_THREAD.is_alive():
            return False
        _DAILY_STOP.clear()
        DAILY_STATUS.update(
            {
                "running": True,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "status": "starting",
                "current_location": None,
                "current_obs_date": None,
                "locations_done_this_cycle": 0,
                "locations_total_this_cycle": 0,
                "last_error": None,
                "stop_requested": False,
                "next_run_at": None,
            }
        )
        _DAILY_THREAD = threading.Thread(target=_daily_main, name="district-daily-cron", daemon=False)
        _DAILY_THREAD.start()
        return True


def stop_district_daily_cron() -> bool:
    with _LOCK:
        running = _DAILY_THREAD is not None and _DAILY_THREAD.is_alive()
        if not running:
            return False
        DAILY_STATUS["stop_requested"] = True
    _DAILY_STOP.set()
    return True


def stop_server_compute() -> dict[str, bool]:
    return {
        "indices_cron_stopped": stop_indices_cron(),
        "store_all_aborted": abort_store_all(),
        "district_daily_cron_stopped": stop_district_daily_cron(),
    }
