"""Earth-observation index routes.

Reads and writes the MySQL `indices` table. District, subdistrict, and village
rows are mirrored into `indices_district`, `indices_subdistrict`, and
`indices_village`. Cron routes stay on the application root.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from app.config import settings
from app.indices_data import (
    CROPTYPE_NOTE,
    aggregated_series,
    ensure_indices_schema,
    list_village_locations,
    normalize_location,
    raw_series,
    resolve_geometry,
    stored_locations,
)
from app.indices_jobs import (
    begin_district_daily_cron,
    begin_indices_cron,
    clamp_workers,
    cron_status,
    daily_status,
    run_store_all,
    start_store_all_background,
    stop_district_daily_cron,
    stop_indices_cron,
    stop_server_compute,
    store_is_running,
    store_location,
    store_status,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/indices", tags=["indices"])
cron_router = APIRouter(tags=["indices"])
ops_router = APIRouter(tags=["ops"])

StrQuery = Annotated[str | None, Query()]
DateQuery = Annotated[date | None, Query()]


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _stored_location(district: str, subdistrict: str | None, village: str | None) -> dict[str, str]:
    try:
        district_name, subdistrict_name, village_name = normalize_location(district, subdistrict, village)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "district": district_name,
        "subdistrict": subdistrict_name,
        "village": village_name,
    }


@router.get("/retrieve-aggregated")
def retrieve_indices_aggregated(
    district: Annotated[str, Query()],
    frequency: Annotated[str, Query()],
    subdistrict: StrQuery = None,
    village: StrQuery = None,
    croptype: Annotated[
        str | None,
        Query(description="Reserved input; not applied on indices table"),
    ] = None,
) -> dict[str, Any]:
    """Average the 21 stored indices by week, month, or year."""
    ensure_indices_schema()
    if frequency not in {"weekly", "monthly", "yearly"}:
        raise HTTPException(
            status_code=400,
            detail="Unsupported frequency. Use weekly, monthly, or yearly.",
        )
    series = aggregated_series(district, subdistrict, village, frequency)
    return {
        "count": len(series),
        "filters": {
            "district": district,
            "subdistrict": subdistrict,
            "village": village,
            "frequency": frequency,
            "croptype": croptype,
        },
        "croptype_applied": False,
        "note": CROPTYPE_NOTE,
        "series": series,
    }


@router.get("/retrieve")
def retrieve_indices(
    district: Annotated[str, Query()],
    subdistrict: StrQuery = None,
    village: StrQuery = None,
    start_date: DateQuery = None,
    end_date: DateQuery = None,
    limit: Annotated[int, Query(ge=1, le=200000)] = 50000,
) -> dict[str, Any]:
    """Raw Sentinel-2 scene rows for one location filter."""
    ensure_indices_schema()
    series = raw_series(district, subdistrict, village, start_date, end_date, limit)
    return {"count": len(series), "series": series}


@router.get("/stored-locations")
def retrieve_stored_locations(
    district: StrQuery = None,
    subdistrict: StrQuery = None,
    village: StrQuery = None,
    limit: Annotated[int, Query(ge=1, le=200000)] = 5000,
) -> dict[str, Any]:
    """Locations that already have rows in `indices`."""
    ensure_indices_schema()
    locations = stored_locations(district, subdistrict, village, limit)
    return {"count": len(locations), "locations": locations}


@router.get("/places")
def index_places(
    district: Annotated[str | None, Query()] = None,
) -> dict[str, Any]:
    """District, subdistrict, and village keys that storage and the trend view use."""
    ensure_indices_schema()
    locations = list_village_locations(district)
    return {
        "count": len(locations),
        "locations": [
            {"district": item[0], "subdistrict": item[1], "village": item[2]}
            for item in locations
        ],
    }


@router.get("/store-status")
def index_store_status() -> dict[str, Any]:
    """Progress for the background store of every village, subdistrict, and district."""
    return store_status()


@router.post("/store")
def store_specific_location(
    district: Annotated[str, Query()],
    subdistrict: StrQuery = None,
    village: StrQuery = None,
    years: Annotated[int, Query(ge=1, le=30)] = 10,
    step_days: Annotated[int, Query(ge=1, le=90)] = 16,
    end_date: DateQuery = None,
    window_half_days: Annotated[int | None, Query()] = None,
    sync: Annotated[bool, Query(description="Run in request if true; else async background")] = False,
) -> dict[str, Any]:
    """Store Sentinel-2 scene indices for one district, subdistrict, or village."""
    ensure_indices_schema()
    if window_half_days is not None and window_half_days < 0:
        raise HTTPException(status_code=400, detail="window_half_days must be >= 0")
    location = _stored_location(district, subdistrict, village)
    try:
        geometry = resolve_geometry(location["district"], location["subdistrict"], location["village"])
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    resolved_end = end_date or _today()
    kwargs = {
        "district": location["district"],
        "subdistrict": location["subdistrict"],
        "village": location["village"],
        "years": years,
        "step_days": step_days,
        "end_date": resolved_end,
        "window_half_days": window_half_days,
        "include_aux": True,
        "geometry": geometry,
    }
    if sync:
        result = store_location(**kwargs)
        return {
            "status": "done",
            "location": result["location"],
            "observation_dates": result["observation_dates"],
            "rows_written": result["rows_written"],
        }

    def _background() -> None:
        try:
            store_location(**kwargs)
        except Exception:
            logger.exception("background indices store failed for %s", location)

    threading.Thread(target=_background, name="indices-store", daemon=False).start()
    return {"status": "accepted", "mode": "background", "location": location}


@router.post("/store-all")
def store_all_locations(
    years: Annotated[int, Query(ge=1, le=30)] = 10,
    step_days: Annotated[int, Query(ge=1, le=90)] = 16,
    end_date: DateQuery = None,
    window_half_days: Annotated[int | None, Query()] = None,
    district: Annotated[str | None, Query(description="Optional district filter; omit for all districts")] = None,
    max_locations: Annotated[int, Query(ge=0, description="0 = all")] = 0,
    sync: Annotated[bool, Query(description="Run in request if true; else async background")] = False,
) -> dict[str, Any]:
    """Store scene indices for every village, subdistrict, and district."""
    ensure_indices_schema()
    if window_half_days is not None and window_half_days < 0:
        raise HTTPException(status_code=400, detail="window_half_days must be >= 0")
    if store_is_running():
        raise HTTPException(status_code=409, detail="Index storage is already running")
    resolved_end = end_date or _today()
    locations = list_village_locations(district, max_locations)
    payload = {
        "years": years,
        "step_days": step_days,
        "end_date": resolved_end,
        "window_half_days": window_half_days,
        "district": district,
        "max_locations": max_locations,
        "locations": locations,
    }
    if sync:
        run_store_all(**payload)
        return {"status": "done", "locations": len(locations), "mode": "sync"}
    start_store_all_background(**payload)
    return {
        "status": "accepted",
        "mode": "background",
        "locations": len(locations),
        "years": years,
        "step_days": step_days,
        "end_date": resolved_end,
    }


@cron_router.post("/indices-cron/start")
def indices_cron_start() -> dict[str, Any]:
    """Start the continuous indices cron. 409 when it is already running."""
    ensure_indices_schema()
    if not begin_indices_cron():
        raise HTTPException(status_code=409, detail="Indices cron is already running")
    years = min(30, max(1, int(settings.indices_cron_years)))
    step_days = min(90, max(1, int(settings.indices_cron_step_days)))
    workers = clamp_workers(settings.indices_cron_workers, 8)
    return {
        "status": "started",
        "poll": "/indices-cron/status",
        "config": {"years": years, "step_days": step_days, "workers": workers},
    }


@cron_router.post("/indices-cron/stop")
def indices_cron_stop() -> dict[str, Any]:
    """Force-stop the indices cron and its in-flight location workers."""
    if not stop_indices_cron():
        raise HTTPException(status_code=404, detail="Indices cron is not running")
    return {"status": "stopped", "stop_requested": True}


@cron_router.get("/indices-cron/status")
def indices_cron_status() -> dict[str, Any]:
    """Live indices cron status."""
    return cron_status()


@cron_router.post("/district-daily-cron/start")
def district_daily_cron_start() -> dict[str, Any]:
    """Store new district-only Sentinel-2 scenes. Auxiliary indices stay null."""
    ensure_indices_schema()
    if not begin_district_daily_cron():
        raise HTTPException(status_code=409, detail="District daily cron is already running")
    return {
        "status": "started",
        "poll": "/district-daily-cron/status",
        "config": {
            "lookback_days": max(1, int(settings.district_daily_lookback_days)),
            "include_aux": False,
        },
    }


@cron_router.post("/district-daily-cron/stop")
def district_daily_cron_stop() -> dict[str, Any]:
    if not stop_district_daily_cron():
        raise HTTPException(status_code=404, detail="District daily cron is not running")
    return {"status": "stopped", "stop_requested": True}


@cron_router.get("/district-daily-cron/status")
def district_daily_cron_status() -> dict[str, Any]:
    return daily_status()


@ops_router.post("/ops/stop-server-compute")
def stop_server_compute_route() -> dict[str, Any]:
    """Force-stop the indices cron, the district daily cron, and /indices/store-all."""
    stopped = stop_server_compute()
    return {"status": "stopped", **stopped}
