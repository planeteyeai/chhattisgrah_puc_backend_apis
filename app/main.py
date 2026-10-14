import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.api_response_storage import ensure_response_schema
from app.biomass import router as biomass_router
from app.boundaries import router as boundaries_router
from app.classwise_api import router as classwise_router
from app.config import settings
from app.crops import router as crops_router
from app.database import engine, kawardha_engine
from app.indices import cron_router, ops_router
from app.indices import router as indices_router
from app.indices_data import ensure_indices_schema
from app.indices_jobs import begin_indices_cron, stop_server_compute
from app.kawardha import router as kawardha_router
from app.kawardha_data import router as kawardha_data_router
from app.satellite_analysis import prometheus_middleware
from app.satellite_analysis import router as satellite_router

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indices_schema()
    except Exception:
        logger.exception("indices schema setup failed")
    try:
        ensure_response_schema()
    except Exception:
        logger.exception("classwise response schema setup failed")
    if settings.indices_cron_autostart:
        try:
            begin_indices_cron()
        except Exception:
            logger.exception("indices cron autostart failed")
    yield
    stop_server_compute()
    try:
        from app.workers.backfill_cron import stop_backfill_cron
        from app.workers.backfill_parallel import stop_all_jobs

        stop_backfill_cron()
        stop_all_jobs()
    except Exception:
        logger.exception("classwise backfill shutdown failed")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

app = FastAPI(
    title="Kabirdham Cadastral Data API",
    description=(
        "Serves stored Kabirdham cadastral plots, administrative boundaries, "
        "crop identification results, and Sentinel-2 indices."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(kawardha_router)
app.include_router(kawardha_data_router)
app.include_router(crops_router)
app.include_router(boundaries_router)
app.include_router(indices_router)
app.include_router(cron_router)
app.include_router(ops_router)
app.include_router(satellite_router)
app.include_router(classwise_router)
app.include_router(biomass_router)
app.middleware("http")(prometheus_middleware)
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


@app.exception_handler(OperationalError)
async def database_unavailable(request: Request, exc: OperationalError) -> JSONResponse:
    logger.error("database unavailable on %s: %s", request.url.path, exc.orig)
    return JSONResponse(
        status_code=503,
        content={"detail": "Database unavailable", "error": str(exc.orig).strip()},
    )


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@app.get("/api")
def api_index() -> dict[str, object]:
    return {
        "name": "Kabirdham Cadastral Data API",
        "docs": "/docs",
        "dashboard": "/",
        "endpoints": {
            "health": "/health",
            "kawardha_health": "/kawardha/health",
            "kawardha_hierarchy": "/kawardha/hierarchy",
            "kawardha_summary": "/kawardha/summary",
            "kawardha_villages": "/kawardha/villages",
            "kawardha_plots": "/kawardha/plots",
            "kawardha_plots_map": "/kawardha/plots/map",
            "kawardha_plot": "/kawardha/plots/{plot_id}",
            "boundaries": "/kawardha-data?district=Kabirdham&tehsil=Kawardha&village=Hedaspur",
            "names": "/kawardha-data/names",
            "crop_summary": "/crops/summary",
            "crop_places": "/crops/places",
            "crop_fields": "/crops/fields",
            "crop_points": "/crops/points?subdistrict=r0c5",
            "place_boundaries": "/boundaries?district_name=Chhattisgarh&subdistrict_name=r5c1&village_name=Chhattisgarh",
            "place_names": "/boundaries/names?district_name=Chhattisgarh",
            "indices_aggregated": "/indices/retrieve-aggregated?district=Ahmadnagar&frequency=monthly",
            "indices_retrieve": "/indices/retrieve?district=Ahmadnagar&start_date=2024-01-01&end_date=2024-12-31",
            "indices_locations": "/indices/stored-locations?district=Ahmadnagar",
            "indices_store": "/indices/store?district=Ahmadnagar&years=10&step_days=16&sync=false",
            "indices_places": "/indices/places",
            "indices_store_status": "/indices/store-status",
            "indices_store_all": "/indices/store-all?years=10&step_days=16&sync=false",
            "indices_cron_start": "/indices-cron/start",
            "indices_cron_status": "/indices-cron/status",
            "indices_cron_stop": "/indices-cron/stop",
            "analyze_growth": "/analyze_Growth?plot_name={field_boundaries.id}&end_date=YYYY-MM-DD",
            "water_uptake": "/wateruptake?plot_name={field_boundaries.id}&end_date=YYYY-MM-DD",
            "soil_moisture": "/SoilMoisture?plot_name={field_boundaries.id}&end_date=YYYY-MM-DD",
            "pest_detection": "/pest-detection?plot_name={field_boundaries.id}&end_date=YYYY-MM-DD",
            "daily_report": "/daily-report?plot_name={field_boundaries.id}&date=YYYY-MM-DD",
            "analysis_tiles": "/analysis-tiles/{key}/{z}/{x}/{y}.png",
            "satellite_health": "/satellite/health",
            "analyze_growth_classwise": "/analyze_Growthclasswise?district=Kabirdham",
            "water_uptake_classwise": "/wateruptakeclasswise?district=Kabirdham",
            "soil_moisture_classwise": "/SoilMoistureclasswise?district=Kabirdham",
            "pest_detection_classwise": "/pest-detectionclasswise?district=Kabirdham",
            "ndwi_detection": "/NDWIDetection?district=Kabirdham",
            "land_surface_temperature": "/land-surface-temperature?district=Kabirdham&start_date=YYYY-MM-DD&end_date=YYYY-MM-DD",
            "land_statistics": "/api/land-statistics?district=Kabirdham",
            "stored_growth": "/api-stored/growth?district=Kabirdham",
            "backfill_stored": "/backfill-stored-data",
            "backfill_cron": "/backfill-cron/status",
            "biomass_summary": "/biomass/summary?year=2024&crop=Rice",
            "biomass_places": "/biomass/places",
            "biomass_compare": "/biomass/compare?village=Agarikalan",
            "biomass_fields": "/biomass/fields?year=2025&subdistrict=Kawardha&skip=0&limit=100",
        },
    }


@app.get("/health")
def health() -> JSONResponse:
    railway = "error"
    kawardha = "not configured"
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        railway = "connected"
    except Exception as exc:
        railway = str(exc)
    if kawardha_engine is not None:
        try:
            with kawardha_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            kawardha = "connected"
        except Exception as exc:
            kawardha = str(exc)
    if railway != "connected":
        status = "error"
        code = 503
    elif kawardha not in {"connected", "not configured"}:
        status = "degraded"
        code = 200
    else:
        status = "ok"
        code = 200
    return JSONResponse(
        status_code=code,
        content={
            "status": status,
            "mysql": railway,
            "kawardha_postgres": kawardha,
        },
    )
