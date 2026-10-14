"""Sentinel-2, Sentinel-1, and weather index formulas.

Optical scenes are COPERNICUS/S2_SR_HARMONIZED, the same Sentinel-2 L2A
products published by Earth Search:
https://earth-search.aws.element84.com/v1/collections/sentinel-2-l2a

msavi and gci are computed and then dropped. They are not stored.
ndwi_gao and ndmi share one formula and both columns are kept.
osavi is not multiplied by 1.16. rvi_radar is VV / VH, not 4*VH/(VV+VH).
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.config import settings
from app.indices_data import DROPPED_INDEXES, INDEX_COLUMNS

logger = logging.getLogger(__name__)

S2_COLLECTION = "COPERNICUS/S2_SR_HARMONIZED"
EARTH_SEARCH_STAC = "https://earth-search.aws.element84.com/v1"
OPTICAL_SCALE_M = 20
RADAR_SCALE_M = 10
PRECIP_SCALE_M = 5500
TEMPERATURE_SCALE_M = 11000
LST_SCALE_M = 1000
SMAP_SCALE_M = 9000
SAVI_L = 0.5

_READY = False
_LOCK = threading.Lock()


class EarthEngineUnavailable(RuntimeError):
    """Raised when Google Earth Engine cannot be initialized."""


def clean_stat(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


def _divide(numerator: float, denominator: float) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def optical_from_reflectance(
    blue: float,
    green: float,
    red: float,
    red_edge: float,
    nir: float,
    swir1: float,
) -> dict[str, float | None]:
    """13 stored optical indices from surface reflectance. msavi and gci are dropped."""
    ndvi = _divide(nir - red, nir + red)
    evi = _divide(2.5 * (nir - red), nir + 6 * red - 7.5 * blue + 1)
    evi2 = _divide(nir - red, nir + 2 * red + 1)
    gndvi = _divide(nir - green, nir + green)
    ndre = _divide(nir - red_edge, nir + red_edge)
    ndwi_mcfeeters = _divide(green - nir, green + nir)
    ndwi_gao = _divide(nir - swir1, nir + swir1)
    ndmi = _divide(nir - swir1, nir + swir1)
    rvi_optical = nir / (red + 1e-6)
    ndbi = _divide(swir1 - nir, swir1 + nir)
    bsi = _divide((swir1 + red) - (nir + blue), (swir1 + red) + (nir + blue))
    savi = _divide(nir - red, nir + red + SAVI_L)
    if savi is not None:
        savi *= 1 + SAVI_L
    osavi = _divide(nir - red, nir + red + 0.16)
    # msavi and gci match the Earth Engine image, then are discarded.
    _msavi_inner = 2 * nir + 1
    _msavi_disc = (_msavi_inner ** 2) - 8 * (nir - red)
    if _msavi_disc >= 0:
        _msavi = (_msavi_inner - math.sqrt(_msavi_disc)) / 2
    else:
        _msavi = None
    _gci = _divide(nir, green)
    if _gci is not None:
        _gci -= 1
    del _msavi, _gci
    values = {
        "ndvi": ndvi,
        "evi": evi,
        "evi2": evi2,
        "gndvi": gndvi,
        "ndre": ndre,
        "ndwi_mcfeeters": ndwi_mcfeeters,
        "ndwi_gao": ndwi_gao,
        "ndmi": ndmi,
        "rvi_optical": rvi_optical,
        "ndbi": ndbi,
        "bsi": bsi,
        "savi": savi,
        "osavi": osavi,
    }
    for name in DROPPED_INDEXES:
        values.pop(name, None)
    return values


def radar_from_backscatter(vv: float, vh: float) -> dict[str, float]:
    return {
        "vh_vv_ratio": vh / (vv + 1e-6),
        "rvi_radar": vv / (vh + 1e-6),
    }


def kelvin_to_celsius(value: float | None) -> float | None:
    if value is None:
        return None
    return value - 273.15


def modis_lst_to_celsius(value: float | None) -> float | None:
    """Spatial mean of LST_Day_1km, then mean * 0.02 - 273.15."""
    if value is None:
        return None
    return value * 0.02 - 273.15


def precipitation_fields(total: float | None, n_days: int = 1) -> dict[str, float | None]:
    if total is None or n_days <= 0:
        return {"precipitation_sum": None, "precipitation_daily": None}
    return {"precipitation_sum": total, "precipitation_daily": total / n_days}


def blank_indices() -> dict[str, None]:
    return {column: None for column in INDEX_COLUMNS}


def _earth_engine():
    import ee

    return ee


def initialize_earth_engine() -> None:
    global _READY
    with _LOCK:
        if _READY:
            return
        try:
            ee = _earth_engine()
        except Exception as exc:
            raise EarthEngineUnavailable(str(exc)) from exc
        project = (settings.ee_project or os.environ.get("EE_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT") or "").strip() or None
        raw_json = (os.environ.get("EE_SERVICE_ACCOUNT_JSON") or "").strip()
        key_path = (
            settings.google_application_credentials
            or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
            or ""
        ).strip()
        credentials = None
        try:
            if raw_json.startswith("{"):
                info = json.loads(raw_json)
                credentials = ee.ServiceAccountCredentials(info["client_email"], key_data=raw_json)
            elif key_path and os.path.isfile(key_path):
                with open(key_path, encoding="utf-8") as handle:
                    info = json.load(handle)
                credentials = ee.ServiceAccountCredentials(info["client_email"], key_path)
            if credentials is not None:
                ee.Initialize(credentials, project=project)
            elif project:
                ee.Initialize(project=project)
            else:
                ee.Initialize()
            ee.Number(1).getInfo()
        except EarthEngineUnavailable:
            raise
        except Exception as exc:
            message = str(exc)
            if "already been initialized" in message.lower() or "already initialized" in message.lower():
                _READY = True
                return
            raise EarthEngineUnavailable(message) from exc
        _READY = True


def require_earth_engine() -> None:
    initialize_earth_engine()


def _reduce_mean(image: Any, geometry: dict[str, Any], scale: float) -> dict[str, float | None]:
    ee = _earth_engine()
    raw = image.reduceRegion(
        reducer=ee.Reducer.mean(),
        geometry=ee.Geometry(geometry),
        scale=scale,
        maxPixels=1e13,
        bestEffort=True,
        tileScale=4,
    ).getInfo()
    if not isinstance(raw, dict):
        return {}
    return {str(key): clean_stat(value) for key, value in raw.items()}


def _optical_image(image: Any) -> Any:
    """Mask SCL and QA60, scale reflectance, then build indices. B12 is selected and unused."""
    ee = _earth_engine()
    scl = image.select("SCL")
    qa = image.select("QA60")
    mask = (
        scl.eq(4).Or(scl.eq(5)).Or(scl.eq(6)).Or(scl.eq(7))
        .And(qa.bitwiseAnd(1 << 10).eq(0))
        .And(qa.bitwiseAnd(1 << 11).eq(0))
    )
    scaled = image.updateMask(mask).select(["B2", "B3", "B4", "B5", "B8", "B11", "B12"]).divide(10000)
    blue = scaled.select("B2")
    green = scaled.select("B3")
    red = scaled.select("B4")
    red_edge = scaled.select("B5")
    nir = scaled.select("B8")
    swir1 = scaled.select("B11")
    ndvi = nir.subtract(red).divide(nir.add(red)).rename("ndvi")
    evi = (
        nir.subtract(red)
        .multiply(2.5)
        .divide(nir.add(red.multiply(6)).subtract(blue.multiply(7.5)).add(1))
        .rename("evi")
    )
    evi2 = nir.subtract(red).divide(nir.add(red.multiply(2)).add(1)).rename("evi2")
    gndvi = nir.subtract(green).divide(nir.add(green)).rename("gndvi")
    ndre = nir.subtract(red_edge).divide(nir.add(red_edge)).rename("ndre")
    ndwi_mcfeeters = green.subtract(nir).divide(green.add(nir)).rename("ndwi_mcfeeters")
    ndwi_gao = nir.subtract(swir1).divide(nir.add(swir1)).rename("ndwi_gao")
    ndmi = nir.subtract(swir1).divide(nir.add(swir1)).rename("ndmi")
    rvi_optical = nir.divide(red.add(1e-6)).rename("rvi_optical")
    ndbi = swir1.subtract(nir).divide(swir1.add(nir)).rename("ndbi")
    bsi = (
        swir1.add(red)
        .subtract(nir.add(blue))
        .divide(swir1.add(red).add(nir).add(blue))
        .rename("bsi")
    )
    savi = nir.subtract(red).divide(nir.add(red).add(SAVI_L)).multiply(1 + SAVI_L).rename("savi")
    osavi = nir.subtract(red).divide(nir.add(red).add(0.16)).rename("osavi")
    inner = nir.multiply(2).add(1)
    msavi = inner.subtract(inner.pow(2).subtract(nir.subtract(red).multiply(8)).sqrt()).divide(2).rename("msavi")
    gci = nir.divide(green).subtract(1).rename("gci")
    return ee.Image.cat(
        [
            ndvi,
            evi,
            evi2,
            gndvi,
            ndre,
            ndwi_mcfeeters,
            ndwi_gao,
            ndmi,
            rvi_optical,
            ndbi,
            bsi,
            savi,
            osavi,
            msavi,
            gci,
        ]
    )


def _image_day(obs_date: date) -> tuple[str, str]:
    start = obs_date.isoformat()
    end = (obs_date + timedelta(days=1)).isoformat()
    return start, end


def _radar(geometry: dict[str, Any], obs_date: date) -> dict[str, float | None]:
    ee = _earth_engine()
    start, end = _image_day(obs_date)
    geom = ee.Geometry(geometry)
    collection = (
        ee.ImageCollection("COPERNICUS/S1_GRD")
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(ee.Filter.eq("resolution_meters", 10))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
        .filterBounds(geom)
        .filterDate(start, end)
        .select(["VV", "VH"])
    )
    if not collection.size().getInfo():
        return {"vh_vv_ratio": None, "rvi_radar": None}
    composite = collection.median()
    vh = composite.select("VH")
    vv = composite.select("VV")
    ratio = vh.divide(vv.add(1e-6)).rename("vh_vv_ratio")
    rvi = vv.divide(vh.add(1e-6)).rename("rvi_radar")
    stats = _reduce_mean(ee.Image.cat([rvi, ratio]), geometry, RADAR_SCALE_M)
    return {
        "vh_vv_ratio": stats.get("vh_vv_ratio"),
        "rvi_radar": stats.get("rvi_radar"),
    }


def _collection_mean(collection_id: str, band: str, start: str, end: str, geometry: dict[str, Any]) -> Any:
    ee = _earth_engine()
    return (
        ee.ImageCollection(collection_id)
        .filterDate(start, end)
        .filterBounds(ee.Geometry(geometry))
        .select(band)
        .mean()
    )


def _weather(geometry: dict[str, Any], obs_date: date) -> dict[str, float | None]:
    ee = _earth_engine()
    start, end = _image_day(obs_date)
    values: dict[str, float | None] = {
        "precipitation_sum": None,
        "precipitation_daily": None,
        "temperature": None,
        "lst": None,
        "soil_water_fraction_surface": None,
        "soil_water_fraction_subsurface": None,
    }
    n_days = 1
    try:
        precip = (
            ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
            .filterDate(start, end)
            .filterBounds(ee.Geometry(geometry))
            .select("precipitation")
            .sum()
        )
        total = _reduce_mean(precip, geometry, PRECIP_SCALE_M).get("precipitation")
        values.update(precipitation_fields(total, n_days))
    except Exception:
        logger.exception("precipitation reduce failed")
    try:
        temperature = _reduce_mean(
            _collection_mean("ECMWF/ERA5_LAND/DAILY_AGGR", "temperature_2m", start, end, geometry),
            geometry,
            TEMPERATURE_SCALE_M,
        ).get("temperature_2m")
        values["temperature"] = kelvin_to_celsius(temperature)
    except Exception:
        logger.exception("temperature reduce failed")
    try:
        lst = _reduce_mean(
            _collection_mean("MODIS/061/MOD11A1", "LST_Day_1km", start, end, geometry),
            geometry,
            LST_SCALE_M,
        ).get("LST_Day_1km")
        values["lst"] = modis_lst_to_celsius(lst)
    except Exception:
        logger.exception("lst reduce failed")
    surface = None
    subsurface = None
    try:
        smap = (
            ee.ImageCollection("NASA/SMAP/SPL4SMGP/007_L4_D")
            .filterDate(start, end)
            .filterBounds(ee.Geometry(geometry))
            .select(["sm_surface", "sm_rootzone"])
            .mean()
        )
        stats = _reduce_mean(smap, geometry, SMAP_SCALE_M)
        surface = stats.get("sm_surface")
        subsurface = stats.get("sm_rootzone")
    except Exception:
        logger.exception("smap reduce failed")
    if surface is None:
        try:
            surface, subsurface = _era5_soil(geometry, start, end)
        except Exception:
            logger.exception("era5 soil fallback failed")
    values["soil_water_fraction_surface"] = surface
    values["soil_water_fraction_subsurface"] = subsurface
    return values


def _era5_soil(geometry: dict[str, Any], start: str, end: str) -> tuple[float | None, float | None]:
    ee = _earth_engine()
    image = (
        ee.ImageCollection("ECMWF/ERA5_LAND/DAILY_AGGR")
        .filterDate(start, end)
        .filterBounds(ee.Geometry(geometry))
        .mean()
    )
    try:
        bands = set(image.bandNames().getInfo() or [])
    except Exception:
        logger.exception("era5 soil bands unavailable")
        return None, None
    surface_name = next((name for name in ("volumetric_soil_water_layer1", "volumetric_soil_water_layer_1") if name in bands), None)
    subsurface_name = next((name for name in ("volumetric_soil_water_layer2", "volumetric_soil_water_layer_2") if name in bands), None)
    selected = [name for name in (surface_name, subsurface_name) if name]
    if not selected:
        return None, None
    stats = _reduce_mean(image.select(selected), geometry, TEMPERATURE_SCALE_M)
    surface = stats.get(surface_name) if surface_name else None
    subsurface = stats.get(subsurface_name) if subsurface_name else None
    return surface, subsurface


def list_sentinel2(geometry: dict[str, Any], start: date, end_exclusive: date) -> list[str]:
    """Image ids intersecting the AOI. filterDate end is exclusive."""
    ee = _earth_engine()
    collection = (
        ee.ImageCollection(S2_COLLECTION)
        .filterBounds(ee.Geometry(geometry))
        .filterDate(start.isoformat(), end_exclusive.isoformat())
    )
    indexes = collection.aggregate_array("system:index").getInfo() or []
    image_ids = []
    for index in indexes:
        if not index:
            continue
        text = str(index)
        if text.startswith(S2_COLLECTION):
            image_ids.append(text)
        else:
            image_ids.append(f"{S2_COLLECTION}/{text}")
    return image_ids


def _observation_date(millis: Any) -> date | None:
    number = clean_stat(millis)
    if number is None:
        return None
    return datetime.fromtimestamp(number / 1000, timezone.utc).date()


def compute_scene(image_id: str, geometry: dict[str, Any], include_aux: bool) -> dict[str, Any] | None:
    """One Sentinel-2 scene. A failed source leaves its columns null."""
    ee = _earth_engine()
    image = ee.Image(image_id)
    try:
        meta = ee.Dictionary(
            {
                "product_id": image.get("PRODUCT_ID"),
                "tile_id": image.get("MGRS_TILE"),
                "cloud_coverage": image.get("CLOUDY_PIXEL_PERCENTAGE"),
                "time_start": image.get("system:time_start"),
                "system_index": image.get("system:index"),
            }
        ).getInfo()
    except Exception:
        logger.exception("sentinel-2 metadata failed for %s", image_id)
        return None
    if not isinstance(meta, dict):
        return None
    obs_date = _observation_date(meta.get("time_start"))
    product_id = meta.get("product_id") or meta.get("system_index")
    if obs_date is None or not product_id:
        return None
    row: dict[str, Any] = {
        "obs_date": obs_date,
        "product_id": str(product_id),
        "tile_id": None if meta.get("tile_id") is None else str(meta.get("tile_id")),
        "cloud_coverage": clean_stat(meta.get("cloud_coverage")),
        **blank_indices(),
    }
    try:
        means = _reduce_mean(_optical_image(image), geometry, OPTICAL_SCALE_M)
        for name in DROPPED_INDEXES:
            means.pop(name, None)
        for column in INDEX_COLUMNS:
            if column in means:
                row[column] = means[column]
    except Exception:
        logger.exception("optical indices failed for %s", image_id)
    if not include_aux:
        return row
    try:
        row.update(_radar(geometry, obs_date))
    except Exception:
        logger.exception("radar indices failed for %s", image_id)
    try:
        row.update(_weather(geometry, obs_date))
    except Exception:
        logger.exception("weather indices failed for %s", image_id)
    return row
