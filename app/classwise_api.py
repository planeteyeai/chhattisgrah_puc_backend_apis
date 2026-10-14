"""Classwise satellite endpoints, stored reads, and backfill controls."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app.api_response_storage import delete_stored, ensure_response_schema, list_series
from app.services.classwise_stac import (
    compute_growth,
    compute_pest,
    compute_soil,
    compute_water,
    render_tile,
)
from app.services.gee_classwise import compute_lst, compute_ndwi
from app.services.land_statistics import compute_land_statistics
from app.workers.backfill_cron import backfill_cron_status, start_backfill_cron, stop_backfill_cron
from app.workers.backfill_daily_jobs import daily_snapshot
from app.workers.backfill_parallel import (
    full_backfill_running,
    job_status,
    start_daily_job,
    start_full_backfill,
    stop_all_jobs,
)

router = APIRouter(tags=["classwise"])


def _tiles(include_tiles: bool) -> bool:
    return bool(include_tiles)


@router.post("/analyze_Growthclasswise")
async def analyze_growth_classwise(
    district: str = Query(...),
    subdistrict: str | None = Query(None),
    village: str | None = Query(None),
    field_id: str | None = Query(None),
    end_date: str | None = Query(None),
    start_date: str | None = Query(None),
    include_tiles: bool = Query(True),
) -> dict[str, Any]:
    return await asyncio.to_thread(
        compute_growth, district, subdistrict, village, field_id, end_date, start_date, _tiles(include_tiles)
    )


@router.post("/wateruptakeclasswise")
async def water_uptake_classwise(
    district: str = Query(...),
    subdistrict: str | None = Query(None),
    village: str | None = Query(None),
    field_id: str | None = Query(None),
    end_date: str | None = Query(None),
    start_date: str | None = Query(None),
    include_tiles: bool = Query(True),
) -> dict[str, Any]:
    return await asyncio.to_thread(
        compute_water, district, subdistrict, village, field_id, end_date, start_date, _tiles(include_tiles)
    )


@router.post("/SoilMoistureclasswise")
async def soil_moisture_classwise(
    district: str = Query(...),
    subdistrict: str | None = Query(None),
    village: str | None = Query(None),
    field_id: str | None = Query(None),
    end_date: str | None = Query(None),
    start_date: str | None = Query(None),
    include_tiles: bool = Query(True),
) -> dict[str, Any]:
    return await asyncio.to_thread(
        compute_soil, district, subdistrict, village, field_id, end_date, start_date, _tiles(include_tiles)
    )


@router.post("/pest-detectionclasswise")
async def pest_detection_classwise(
    district: str = Query(...),
    subdistrict: str | None = Query(None),
    village: str | None = Query(None),
    field_id: str | None = Query(None),
    end_date: str | None = Query(None),
    start_date: str | None = Query(None),
    include_tiles: bool = Query(True),
) -> dict[str, Any]:
    return await asyncio.to_thread(
        compute_pest, district, subdistrict, village, field_id, end_date, start_date, _tiles(include_tiles)
    )


@router.post("/NDWIDetection")
async def ndwi_detection(
    district: str = Query(...),
    subdistrict: str | None = Query(None),
    village: str | None = Query(None),
    end_date: str | None = Query(None),
    start_date: str | None = Query(None),
    max_cloud_pct: float = Query(60),
) -> dict[str, Any]:
    return await asyncio.to_thread(compute_ndwi, district, subdistrict, village, end_date, start_date, max_cloud_pct)


@router.post("/land-surface-temperature")
@router.post("/land-surface-temperatureclasswisearea")
async def land_surface_temperature(
    district: str = Query(...),
    subdistrict: str | None = Query(None),
    village: str | None = Query(None),
    start_date: str = Query(...),
    end_date: str = Query(...),
) -> dict[str, Any]:
    return await asyncio.to_thread(compute_lst, district, subdistrict, village, start_date, end_date)


@router.get("/api/land-statistics")
async def land_statistics(
    district: str = Query(...),
    subdistrict: str | None = Query(None),
    village: str | None = Query(None),
    endDate: str | None = Query(None),
) -> dict[str, Any]:
    return await asyncio.to_thread(compute_land_statistics, district, subdistrict, village, endDate)


@router.get("/classwise-tiles/{key}/{z}/{x}/{y}.png")
async def classwise_tile(key: str, z: int, x: int, y: int):
    if z < 0 or z > 24:
        raise HTTPException(status_code=400, detail="Invalid zoom level")
    data = await asyncio.to_thread(render_tile, key, z, x, y, None)
    return Response(content=data, media_type="image/png", headers={"Cache-Control": "public, max-age=300"})


@router.get("/classwise-tiles/{key}/class/{class_id}/{z}/{x}/{y}.png")
async def classwise_class_tile(key: str, class_id: int, z: int, x: int, y: int):
    if z < 0 or z > 24:
        raise HTTPException(status_code=400, detail="Invalid zoom level")
    data = await asyncio.to_thread(render_tile, key, z, x, y, class_id)
    return Response(content=data, media_type="image/png", headers={"Cache-Control": "public, max-age=300"})


@router.get("/api-stored/growth")
def stored_growth(
    district: str = Query(...),
    subdistrict: str | None = Query(None),
    village: str | None = Query(None),
    limit: int = Query(500),
) -> dict[str, Any]:
    rows = list_series("growth", district, subdistrict, village, limit)
    return {"district": district, "count": len(rows), "series": rows}


@router.get("/api-stored/water-uptake")
def stored_water(
    district: str = Query(...),
    subdistrict: str | None = Query(None),
    village: str | None = Query(None),
    limit: int = Query(500),
) -> dict[str, Any]:
    rows = list_series("water_uptake", district, subdistrict, village, limit)
    return {"district": district, "count": len(rows), "series": rows}


@router.get("/api-stored/soil-moisture")
def stored_soil(
    district: str = Query(...),
    subdistrict: str | None = Query(None),
    village: str | None = Query(None),
    limit: int = Query(500),
) -> dict[str, Any]:
    rows = list_series("soil_moisture", district, subdistrict, village, limit)
    return {"district": district, "count": len(rows), "series": rows}


@router.get("/api-stored/pest-detection")
def stored_pest(
    district: str = Query(...),
    subdistrict: str | None = Query(None),
    village: str | None = Query(None),
    limit: int = Query(500),
) -> dict[str, Any]:
    rows = list_series("pest_detection", district, subdistrict, village, limit)
    return {"district": district, "count": len(rows), "series": rows}


@router.post("/backfill-stored-data", status_code=202)
def backfill_stored_data() -> dict[str, Any]:
    if not start_full_backfill():
        raise HTTPException(status_code=409, detail="Backfill is already running")
    return {"status": "started", "months": 14, "running": full_backfill_running()}


@router.post("/backfill-stored-data/daily", status_code=202)
def backfill_daily(
    district: str | None = Query(None),
    subdistrict: str | None = Query(None),
    village: str | None = Query(None),
    months: int = Query(15),
) -> dict[str, str]:
    if months < 1 or months > 24:
        raise HTTPException(status_code=400, detail="months must be between 1 and 24")
    job_id = start_daily_job(district, subdistrict, village, months)
    return {"job_id": job_id, "status": "started"}


@router.get("/backfill-stored-data/daily/jobs/{job_id}")
def backfill_daily_job(job_id: str) -> dict[str, Any]:
    status = job_status(job_id)
    if status is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return status


@router.post("/backfill-stored-data/daily/jobs/stop-all")
def backfill_stop_all() -> dict[str, int]:
    return {"stopped": stop_all_jobs()}


@router.get("/backfill-stored-data/daily")
def backfill_daily_read(
    district: str = Query(...),
    subdistrict: str | None = Query(None),
    village: str | None = Query(None),
) -> dict[str, Any]:
    return daily_snapshot(district, subdistrict, village)


@router.post("/backfill-cron/start")
def backfill_cron_start() -> dict[str, Any]:
    started = start_backfill_cron()
    return {"started": started, **backfill_cron_status()}


@router.get("/backfill-cron/status")
def backfill_cron_state() -> dict[str, Any]:
    return backfill_cron_status()


@router.post("/backfill-cron/stop")
def backfill_cron_stop() -> dict[str, bool]:
    return {"stopped": stop_backfill_cron()}


@router.delete("/backfill-stored-data")
def backfill_delete(confirm: bool = Query(False), district: str | None = Query(None)) -> dict[str, Any]:
    if confirm is not True:
        raise HTTPException(status_code=400, detail="confirm=true is required")
    ensure_response_schema()
    return {"deleted": delete_stored(district)}
