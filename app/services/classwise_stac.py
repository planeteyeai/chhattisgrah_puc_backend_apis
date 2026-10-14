"""Classwise growth, water uptake, soil moisture, and pest classification.

Rasters come from Earth Search STAC. Reads are unsigned. Responses use the
class names, colors, and JSON keys the agricultural UI expects.
"""

from __future__ import annotations

import hashlib
import io
import logging
import time
from collections import OrderedDict
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np
import requests
from PIL import Image
from pyproj import Geod
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.transform import from_bounds
from rasterio.warp import reproject, transform_bounds, transform_geom
from shapely.geometry import mapping

from app.api_response_storage import load_response, save_response
from app.config import settings
from app.geometry_helpers import lookup_aoi
from app.satellite_analysis import (
    _apply_s2_scale,
    _force_unsigned,
    _has_asset,
    _item_date,
    _item_dt,
    _read_s1_band,
    _read_s2_band,
    _s1_iw_vv_vh,
    _xyz_bounds,
)

logger = logging.getLogger(__name__)

GROWTH_JSON = {1: "#bc1e29", 2: "#58cf54", 3: "#28ae31", 4: "#00351d"}
GROWTH_TILE = {1: "#bc1e29", 2: "#58cf54", 3: "#28ae31", 4: "#056c3e"}
GROWTH_NAMES = {1: "Weak", 2: "Stress", 3: "Moderate", 4: "Healthy"}
WATER_COLORS = {1: "#EBFF34", 2: "#CC8213AF", 3: "#1348E88E", 4: "#2E199ABD", 5: "#0602178F"}
WATER_NAMES = {1: "Deficient", 2: "Less", 3: "Adequat", 4: "Excellent", 5: "Excess"}
SOIL_COLORS = {1: "#2FC0D3", 2: "#4365D4", 3: "#473CDF", 4: "#2116BF", 5: "#000475"}
SOIL_NAMES = {1: "less", 2: "adequate", 3: "excellent", 4: "excess", 5: "shallow_water"}
PEST_COLORS = {1: "#FF0000", 2: "#00DD88", 3: "#2600FF", 4: "#D60BFF", 5: "#FFE608"}

_tiles: OrderedDict[str, dict[str, Any]] = OrderedDict()
_GEOD = Geod(ellps="WGS84")


@contextmanager
def _unsigned():
    token = _force_unsigned.set(True)
    try:
        yield
    finally:
        _force_unsigned.reset(token)


def resolve_classwise_dates(start_date: str | None, end_date: str | None) -> tuple[str, str]:
    if end_date:
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    else:
        end = date.today() + timedelta(days=1)
    if start_date:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
    else:
        start = end - timedelta(days=14)
    if start > end:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="start_date must be on or before end_date")
    return start.isoformat(), end.isoformat()


def aoi_hectares(geom) -> float:
    area, _ = _GEOD.geometry_area_perimeter(geom)
    return abs(float(area)) / 10000.0


def raster_resolution(geom) -> float:
    minx, miny, maxx, maxy = geom.bounds
    side = max(maxx - minx, maxy - miny, 1e-12)
    return max(float(settings.classwise_raster_res), side / float(settings.classwise_max_raster_side))


def _stac_search(collection: str, geom, start_date: str, end_date: str, limit: int = 50) -> list[dict]:
    payload = {
        "collections": [collection],
        "bbox": [float(v) for v in geom.bounds],
        "datetime": f"{start_date}T00:00:00Z/{end_date}T23:59:59Z",
        "limit": limit,
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
    }
    response = requests.post(
        f"{settings.stac_api_url.rstrip('/')}/search",
        json=payload,
        timeout=int(settings.aws_stac_timeout),
    )
    response.raise_for_status()
    return list(response.json().get("features") or [])


def _latest(items: list[dict]) -> dict | None:
    if not items:
        return None
    floor = datetime.min.replace(tzinfo=timezone.utc)
    return max(items, key=lambda item: _item_dt(item) or floor)


def _s2_ready(item: dict, assets: tuple[str, ...]) -> bool:
    return all(_has_asset(item, name) for name in assets)


def pick_sensor(
    geom,
    start_date: str,
    end_date: str,
    s2_assets: tuple[str, ...],
    s2_collection: str | None = None,
    s1_collection: str | None = None,
) -> dict[str, Any]:
    from fastapi import HTTPException

    s2_name = s2_collection or settings.stac_s2_collection
    s1_name = s1_collection or settings.stac_s1_collection
    s2 = [item for item in _stac_search(s2_name, geom, start_date, end_date, 30) if _s2_ready(item, s2_assets)]
    s1 = [item for item in _stac_search(s1_name, geom, start_date, end_date, 40) if _s1_iw_vv_vh(item)]
    latest_s2 = _latest(s2)
    latest_s1 = _latest(s1)
    floor = datetime.min.replace(tzinfo=timezone.utc)
    if latest_s2 and (latest_s1 is None or (_item_dt(latest_s2) or floor) >= (_item_dt(latest_s1) or floor)):
        return {"sensor": "s2", "date": _item_date(latest_s2), "item": latest_s2}
    if latest_s1:
        return {"sensor": "s1", "date": _item_date(latest_s1), "item": latest_s1}
    raise HTTPException(status_code=404, detail="No Sentinel-1 or Sentinel-2 images found for the given date range.")


def _read_pair(item: dict, first: str, second: str, geom, resolution: float):
    left, transform, crs = _read_s2_band(item, first, geom, resolution=resolution)
    right, _, _ = _read_s2_band(item, second, geom, resolution=resolution)
    left = _apply_s2_scale(left, item, first)
    right = _apply_s2_scale(right, item, second)
    left, right = _match_pair(left, right)
    return left, right, transform, crs


def _match_pair(left: np.ndarray, right: np.ndarray):
    height = min(left.shape[0], right.shape[0])
    width = min(left.shape[1], right.shape[1])
    return left[:height, :width], right[:height, :width]


def _inside(array: np.ndarray, transform, geom) -> np.ndarray:
    return geometry_mask([mapping(geom)], transform=transform, invert=True, out_shape=array.shape)


def _hex_rgba(value: str) -> tuple[int, int, int, int]:
    raw = value.lstrip("#")
    if len(raw) >= 8:
        return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16), int(raw[6:8], 16)
    return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16), 255


def _tile_url(key: str | None, class_id: int | None = None) -> str | None:
    if not key:
        return None
    base = (settings.public_base_url or "").rstrip("/")
    if class_id is None:
        path = f"/classwise-tiles/{key}/{{z}}/{{x}}/{{y}}.png"
    else:
        path = f"/classwise-tiles/{key}/class/{class_id}/{{z}}/{{x}}/{{y}}.png"
    return f"{base}{path}" if base else path


def register_tile(array: np.ndarray, transform, geom, palette: dict[int, str]) -> str:
    key = hashlib.sha256(f"{time.time_ns()}-{id(array)}".encode()).hexdigest()[:24]
    _tiles[key] = {
        "array": np.asarray(array, dtype=np.float32).copy(),
        "transform": transform,
        "geometry": mapping(geom),
        "palette": {cid: _hex_rgba(color) for cid, color in palette.items()},
    }
    _tiles.move_to_end(key)
    while len(_tiles) > int(settings.tile_cache_max):
        _tiles.popitem(last=False)
    return key


def tile_cached(key: str) -> bool:
    return key in _tiles


def extract_tile_keys(payload: Any) -> list[str]:
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        elif isinstance(node, str) and "/classwise-tiles/" in node:
            part = node.split("/classwise-tiles/", 1)[1]
            token = part.split("/", 1)[0]
            if len(token) == 24:
                found.append(token)

    walk(payload)
    return found


def _cache_hit(product: str, aoi: dict, end_date: str, include_tiles: bool) -> dict | None:
    stored = load_response(product, aoi["district"], aoi["subdistrict"], aoi["village"], end_date, aoi["field_id"])
    if not stored or "current" not in stored:
        return None
    if include_tiles:
        keys = extract_tile_keys(stored)
        if not keys or not all(tile_cached(key) for key in keys):
            return None
    current = dict(stored["current"])
    current["from_cache"] = True
    return {"current": current, "stored": []}


def _class_rows(masks: dict[int, np.ndarray], names: dict[int, str], colors: dict[int, str], inside_count: int, hectares: float, key: str | None) -> tuple[list[dict], dict[str, Any]]:
    rows = []
    summary: dict[str, Any] = {}
    for class_id in sorted(names):
        count = int(np.count_nonzero(masks[class_id]))
        percentage = (count / inside_count * 100.0) if inside_count else 0.0
        area = round((count / inside_count * hectares) if inside_count else 0.0, 2)
        rows.append(
            {
                "class_id": class_id,
                "class_name": names[class_id],
                "color": colors[class_id],
                "percentage": percentage,
                "area_hectares": area,
                "tile_url": _tile_url(key, class_id),
            }
        )
        label = names[class_id].lower()
        summary[f"{label}_pixel_count"] = count
        summary[f"{label}_pixel_percentage"] = percentage
    return rows, summary


def _feature(plot_id: str, geom, properties: dict) -> dict:
    return {
        "type": "Feature",
        "geometry": {"type": geom.geom_type, "coordinates_stripped": True},
        "properties": properties,
    }


def _finish(product: str, aoi: dict, start_date: str, end_date: str, current: dict) -> dict:
    for extra in ("villages", "subdistricts"):
        if extra in aoi:
            current[extra] = aoi[extra]
    wrapper = {"current": current, "stored": []}
    save_response(
        product,
        aoi["district"],
        aoi["subdistrict"],
        aoi["village"],
        start_date,
        end_date,
        aoi["field_id"],
        wrapper,
    )
    current.pop("villages", None)
    current.pop("subdistricts", None)
    return {"current": current, "stored": []}


def classify_growth_ndvi(ndvi: np.ndarray) -> dict[int, np.ndarray]:
    finite = np.isfinite(ndvi)
    return {
        1: finite & (ndvi < 0.10),
        2: finite & (ndvi >= 0.10) & (ndvi < 0.20),
        3: finite & (ndvi >= 0.20) & (ndvi < 0.40),
        4: finite & (ndvi >= 0.40),
    }


def classify_growth_vh(vh: np.ndarray) -> dict[int, np.ndarray]:
    finite = np.isfinite(vh)
    return {
        1: finite & (vh >= -11),
        2: finite & (vh < -11) & (vh > -13),
        3: finite & (vh <= -13) & (vh > -15),
        4: finite & (vh <= -15),
    }


def classify_water_ndwi(ndwi: np.ndarray) -> dict[int, np.ndarray]:
    finite = np.isfinite(ndwi)
    return {
        1: finite & (ndwi < -0.50),
        2: finite & (ndwi >= -0.50) & (ndwi < -0.42),
        3: finite & (ndwi >= -0.42) & (ndwi < -0.375),
        4: finite & (ndwi >= -0.375) & (ndwi < -0.365),
        5: finite & (ndwi >= -0.365),
    }


def classify_water_delta(delta: np.ndarray) -> dict[int, np.ndarray]:
    finite = np.isfinite(delta)
    return {
        1: finite & (delta < -3.13),
        2: finite & (delta >= -3.13) & (delta <= -0.1),
        3: finite & (delta > -0.1) & (delta < 4),
        4: finite & (delta >= 4) & (delta < 6),
        5: finite & (delta >= 6),
    }


def classify_soil_ndmi(ndmi: np.ndarray) -> dict[int, np.ndarray]:
    finite = np.isfinite(ndmi)
    return {
        1: finite & (ndmi <= -0.30),
        2: finite & (ndmi > -0.30) & (ndmi <= 0),
        3: finite & (ndmi > 0) & (ndmi <= 0.10),
        4: finite & (ndmi > 0.10) & (ndmi <= 0.24),
        5: finite & (ndmi > 0.24),
    }


def classify_soil_vv(vv: np.ndarray) -> dict[int, np.ndarray]:
    finite = np.isfinite(vv)
    return {
        1: finite & (vv <= -12),
        2: finite & (vv > -12) & (vv <= -10),
        3: finite & (vv > -10) & (vv <= -8),
        4: finite & (vv > -8) & (vv <= -6),
        5: finite & (vv > -6),
    }


def _apply_inside(masks: dict[int, np.ndarray], valid: np.ndarray) -> dict[int, np.ndarray]:
    return {class_id: mask & valid for class_id, mask in masks.items()}


def _raster_from_masks(shape, masks: dict[int, np.ndarray], order: list[int]) -> np.ndarray:
    classes = np.zeros(shape, dtype=np.float32)
    for class_id in order:
        classes[masks[class_id]] = class_id
    return classes


def compute_growth(district, subdistrict, village, field_id, end_date, start_date, include_tiles: bool) -> dict:
    start_date, end_date = resolve_classwise_dates(start_date, end_date)
    aoi = lookup_aoi(district, subdistrict, village, field_id)
    cached = _cache_hit("growth", aoi, end_date, include_tiles)
    if cached:
        return cached
    geom = aoi["geometry"]
    resolution = raster_resolution(geom)
    with _unsigned():
        scene = pick_sensor(geom, start_date, end_date, ("red", "nir"))
        if scene["sensor"] == "s2":
            red, nir, transform, _crs = _read_pair(scene["item"], "red", "nir", geom, resolution)
            index = (nir - red) / (nir + red + 1e-12)
            masks = classify_growth_ndvi(index)
            data_source = "Sentinel-2 NDVI"
        else:
            vh, transform, _crs = _read_s1_band(scene["item"], "vh", geom, resolution=resolution)
            index = vh
            masks = classify_growth_vh(index)
            data_source = "Sentinel-1 VH"
    valid = _inside(index, transform, geom) & np.isfinite(index)
    masks = _apply_inside(masks, valid)
    classes = _raster_from_masks(index.shape, masks, [1, 2, 3, 4])
    key = register_tile(classes, transform, geom, GROWTH_TILE) if include_tiles else None
    hectares = aoi_hectares(geom)
    inside_count = int(np.count_nonzero(valid))
    rows, _summary = _class_rows(masks, GROWTH_NAMES, GROWTH_JSON, inside_count, hectares, key)
    counts = {row["class_id"]: int(np.count_nonzero(masks[row["class_id"]])) for row in rows}
    percentages = {row["class_id"]: row["percentage"] for row in rows}
    properties = {
        "plot_id": aoi["plot_id"],
        "area_acres": round(hectares * 2.471, 2),
        "start_date": start_date,
        "end_date": end_date,
        "tile_url": _tile_url(key),
        "data_source": data_source,
        "last_updated": datetime.now().isoformat(),
    }
    if aoi["field_id"]:
        properties["field_id"] = aoi["field_id"]
    current = {
        "feature": _feature(aoi["plot_id"], geom, properties),
        "pixel_summary": {
            "total_pixel_count": inside_count,
            "healthy_pixel_count": counts[4],
            "healthy_pixel_percentage": percentages[4],
            "moderate_pixel_count": counts[3],
            "moderate_pixel_percentage": percentages[3],
            "weak_pixel_count": counts[1],
            "weak_pixel_percentage": percentages[1],
            "stress_pixel_count": counts[2],
            "stress_pixel_percentage": percentages[2],
            "analysis_start_date": start_date,
            "analysis_end_date": end_date,
            "latest_image_date": scene["date"],
            "area_hectares": round(hectares, 2),
        },
        "classwise": rows,
    }
    return _finish("growth", aoi, start_date, end_date, current)


def _previous_s1(geom, latest_item: dict, latest_date: str) -> dict:
    from fastapi import HTTPException

    latest_dt = _item_dt(latest_item) or datetime.min.replace(tzinfo=timezone.utc)
    start = (datetime.strptime(latest_date, "%Y-%m-%d").date() - timedelta(days=120)).isoformat()
    items = [
        item
        for item in _stac_search(settings.stac_s1_collection, geom, start, latest_date, 100)
        if _s1_iw_vv_vh(item) and (_item_dt(item) or datetime.min.replace(tzinfo=timezone.utc)) < latest_dt
    ]
    previous = _latest(items)
    if previous is None:
        raise HTTPException(status_code=404, detail="Need 2 Sentinel-1 VH scenes for water uptake")
    return previous


def compute_water(district, subdistrict, village, field_id, end_date, start_date, include_tiles: bool) -> dict:
    start_date, end_date = resolve_classwise_dates(start_date, end_date)
    aoi = lookup_aoi(district, subdistrict, village, field_id)
    cached = _cache_hit("water_uptake", aoi, end_date, include_tiles)
    if cached:
        return cached
    geom = aoi["geometry"]
    resolution = raster_resolution(geom)
    with _unsigned():
        scene = pick_sensor(geom, start_date, end_date, ("green", "nir"))
        if scene["sensor"] == "s2":
            green, nir, transform, _crs = _read_pair(scene["item"], "green", "nir", geom, resolution)
            index = (green - nir) / (green + nir + 1e-12)
            masks = classify_water_ndwi(index)
        else:
            previous = _previous_s1(geom, scene["item"], scene["date"])
            latest_vh, transform, _crs = _read_s1_band(scene["item"], "vh", geom, resolution=resolution)
            previous_vh, _, _ = _read_s1_band(previous, "vh", geom, resolution=resolution)
            latest_vh, previous_vh = _match_pair(latest_vh, previous_vh)
            index = latest_vh - previous_vh
            masks = classify_water_delta(index)
    valid = _inside(index, transform, geom) & np.isfinite(index)
    masks = _apply_inside(masks, valid)
    classes = _raster_from_masks(index.shape, masks, [1, 2, 3, 4, 5])
    key = register_tile(classes, transform, geom, WATER_COLORS) if include_tiles else None
    hectares = aoi_hectares(geom)
    inside_count = int(np.count_nonzero(valid))
    rows, summary = _class_rows(masks, WATER_NAMES, WATER_COLORS, inside_count, hectares, key)
    properties = {
        "plot_id": aoi["plot_id"],
        "start_date": start_date,
        "end_date": end_date,
        "tile_url": _tile_url(key),
        "last_updated": datetime.now().isoformat(),
    }
    current = {
        "feature": _feature(aoi["plot_id"], geom, properties),
        "pixel_summary": {
            "total_pixel_count": inside_count,
            "analysis_start_date": start_date,
            "analysis_end_date": end_date,
            "area_hectares": round(hectares, 2),
            **summary,
        },
        "classwise": rows,
    }
    return _finish("water_uptake", aoi, start_date, end_date, current)


def compute_soil(district, subdistrict, village, field_id, end_date, start_date, include_tiles: bool) -> dict:
    start_date, end_date = resolve_classwise_dates(start_date, end_date)
    aoi = lookup_aoi(district, subdistrict, village, field_id)
    cached = _cache_hit("soil_moisture", aoi, end_date, include_tiles)
    if cached:
        return cached
    geom = aoi["geometry"]
    resolution = raster_resolution(geom)
    with _unsigned():
        scene = pick_sensor(geom, start_date, end_date, ("nir08", "swir16"))
        if scene["sensor"] == "s2":
            nir08, swir16, transform, _crs = _read_pair(scene["item"], "nir08", "swir16", geom, resolution)
            index = (nir08 - swir16) / (nir08 + swir16 + 1e-12)
            masks = classify_soil_ndmi(index)
            sensor_used = "Sentinel-2"
            data_source = "Sentinel-2 Soil Moisture"
        else:
            vv, transform, _crs = _read_s1_band(scene["item"], "vv", geom, resolution=resolution)
            index = vv
            masks = classify_soil_vv(index)
            sensor_used = "Sentinel-1"
            data_source = "Sentinel-1 Soil Moisture"
    valid = _inside(index, transform, geom) & np.isfinite(index)
    masks = _apply_inside(masks, valid)
    classes = _raster_from_masks(index.shape, masks, [1, 2, 3, 4, 5])
    key = register_tile(classes, transform, geom, SOIL_COLORS) if include_tiles else None
    hectares = aoi_hectares(geom)
    inside_count = int(np.count_nonzero(valid))
    rows, summary = _class_rows(masks, SOIL_NAMES, SOIL_COLORS, inside_count, hectares, key)
    for class_id, name in SOIL_NAMES.items():
        label = name.lower()
        summary[f"{label}_pixel_percentage"] = round(summary[f"{label}_pixel_percentage"], 2)
    properties = {
        "plot_id": aoi["plot_id"],
        "start_date": start_date,
        "end_date": end_date,
        "tile_url": _tile_url(key),
        "sensor_used": sensor_used,
        "data_source": data_source,
        "last_updated": datetime.now().isoformat(),
    }
    current = {
        "feature": _feature(aoi["plot_id"], geom, properties),
        "pixel_summary": {
            "total_pixel_count": inside_count,
            "analysis_start_date": start_date,
            "analysis_end_date": end_date,
            "latest_image_date": scene["date"],
            "sensor_used": sensor_used,
            **summary,
        },
        "classwise": rows,
    }
    return _finish("soil_moisture", aoi, start_date, end_date, current)


def _norm01(arr: np.ndarray) -> np.ndarray:
    lo = np.nanmin(arr)
    hi = np.nanmax(arr)
    if not np.isfinite(lo) or not np.isfinite(hi) or hi == lo:
        return np.zeros_like(arr, dtype=np.float32)
    return np.clip((arr - lo) / (hi - lo), 0, 1).astype(np.float32)


def _db_to_linear(img: np.ndarray) -> np.ndarray:
    return np.power(10.0, img / 10.0) + 1e-6


def _s1_items(geom, start_date: str, end_date: str, descending_only: bool = False) -> list[dict]:
    items = []
    for item in _stac_search(settings.stac_s1_collection, geom, start_date, end_date, 80):
        if not _s1_iw_vv_vh(item):
            continue
        if descending_only:
            orbit = str((item.get("properties") or {}).get("sat:orbit_state") or "").upper()
            if orbit not in {"DESCENDING", "DESC"}:
                continue
        items.append(item)
    floor = datetime.min.replace(tzinfo=timezone.utc)
    items.sort(key=lambda item: _item_dt(item) or floor, reverse=True)
    return items[:6]


def _median_vv_vh(items: list[dict], geom, resolution: float):
    vv_stack = []
    vh_stack = []
    transform = crs = None
    shape = None
    for item in items:
        try:
            vv, transform, crs = _read_s1_band(item, "vv", geom, resolution=resolution)
            vh, _, _ = _read_s1_band(item, "vh", geom, resolution=resolution)
        except Exception as exc:
            logger.warning("Skipping Sentinel-1 item %s: %s", item.get("id"), exc)
            continue
        vv, vh = _match_pair(vv, vh)
        if shape is None:
            shape = vv.shape
        else:
            vv = vv[: shape[0], : shape[1]]
            vh = vh[: shape[0], : shape[1]]
        vv_stack.append(vv)
        vh_stack.append(vh)
    if not vv_stack:
        return None, None, transform, crs
    return np.nanmedian(np.stack(vv_stack), axis=0), np.nanmedian(np.stack(vh_stack), axis=0), transform, crs


def _pest_masks(vv, vh, bvv, bvh, gvv, gvh, gbvv, gbvh):
    ratio = vv / (vh + 1e-6)
    sar_mean = np.nanmean(np.stack([vv, vh, ratio]), axis=0)
    avg = (_norm01(sar_mean) + _norm01(vh * -1)) * 0.5
    low = float(np.nanpercentile(avg, 2))
    high = float(np.nanpercentile(avg, 98))
    span = high - low if high != low else 1.0
    stretched = np.clip((avg - low) / span, 0, 1)
    chewing = stretched <= 0.01
    delta_ratio = vv / (vh + 1e-6) - bvv / (bvh + 1e-6)
    stress = delta_ratio >= 0.45
    max_delta = float(np.nanmax(delta_ratio)) if np.isfinite(delta_ratio).any() else 1.0
    if not np.isfinite(max_delta) or max_delta == 0:
        max_delta = 1.0
    severity = np.where(stress, delta_ratio / max_delta, np.nan)
    fungi = severity >= 0.75
    sucking = avg >= 1
    wilt = severity >= 1
    vv_lin = _db_to_linear(gvv)
    vh_lin = _db_to_linear(gvh)
    base_vv = _db_to_linear(gbvv)
    base_vh = _db_to_linear(gbvh)
    vhvv = vh_lin / vv_lin
    rvi = vh_lin * 4 / (vv_lin + vh_lin)
    vv_norm = np.clip((vv_lin - 0.001) / (0.3 - 0.001), 0, 1)
    base_rvi = base_vh * 4 / (base_vv + base_vh)
    base_vhvv = base_vh / base_vv
    soilborne = (base_rvi - rvi > 0.30) & (base_vhvv - vhvv > 0.2) & (vh_lin - base_vh < -0.01) & (vv_norm < 0.10)
    return {
        "chewing": chewing,
        "fungi": fungi,
        "sucking": sucking,
        "wilt": wilt,
        "soilborne": soilborne,
    }


def _child(area: float, parent: float, tile: str | None, include_tile: bool = True) -> dict:
    payload = {"area_ha": round(area, 2), "pct_of_parent": 100 if parent > 0 and area > 0 else 0}
    if include_tile:
        payload = {"tile_url": tile, **payload}
    return payload


def compute_pest(district, subdistrict, village, field_id, end_date, start_date, include_tiles: bool) -> dict:
    from fastapi import HTTPException

    _start, end_date = resolve_classwise_dates(start_date, end_date)
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    analysis_start = (end - timedelta(days=15)).isoformat()
    analysis_end = end.isoformat()
    baseline_start = date(end.year - 1, 6, 1).isoformat()
    baseline_end = date(end.year - 1, 10, 30).isoformat()
    aoi = lookup_aoi(district, subdistrict, village, field_id)
    cached = _cache_hit("pest_detection", aoi, analysis_end, include_tiles)
    if cached:
        return cached
    geom = aoi["geometry"]
    resolution = raster_resolution(geom)
    with _unsigned():
        analysis_items = _s1_items(geom, analysis_start, analysis_end)
        if not analysis_items:
            raise HTTPException(status_code=404, detail="No Sentinel-1 IW images found")
        baseline_items = _s1_items(geom, baseline_start, baseline_end)
        if not baseline_items:
            raise HTTPException(status_code=404, detail="No Sentinel-1 baseline images found")
        vv, vh, transform, _crs = _median_vv_vh(analysis_items, geom, resolution)
        bvv, bvh, _, _ = _median_vv_vh(baseline_items, geom, resolution)
        if vv is None or vh is None or bvv is None or bvh is None:
            raise HTTPException(status_code=404, detail="No usable Sentinel-1 VV/VH assets found")
        vv, vh = _match_pair(vv, vh)
        bvv, bvh = _match_pair(bvv, bvh)
        vv, bvv = _match_pair(vv, bvv)
        vh, bvh = _match_pair(vh, bvh)
        grub_analysis = _s1_items(geom, analysis_start, analysis_end, descending_only=True) or analysis_items
        grub_baseline = _s1_items(geom, baseline_start, baseline_end, descending_only=True) or baseline_items
        gvv, gvh, _, _ = _median_vv_vh(grub_analysis, geom, resolution)
        gbvv, gbvh, _, _ = _median_vv_vh(grub_baseline, geom, resolution)
        if gvv is None or gvh is None:
            gvv, gvh = vv, vh
        if gbvv is None or gbvh is None:
            gbvv, gbvh = bvv, bvh
        gvv, gvh = _match_pair(gvv, gvh)
        gbvv, gbvh = _match_pair(gbvv, gbvh)
        vv, gvv = _match_pair(vv, gvv)
        vh, gvh = _match_pair(vh, gvh)
        bvv, gbvv = _match_pair(bvv, gbvv)
        bvh, gbvh = _match_pair(bvh, gbvh)
        masks = _pest_masks(vv, vh, bvv, bvh, gvv, gvh, gbvv, gbvh)
    valid = _inside(vv, transform, geom) & np.isfinite(vv) & np.isfinite(vh)
    masks = {name: mask & valid for name, mask in masks.items()}
    classes = np.zeros(vv.shape, dtype=np.float32)
    classes[masks["chewing"]] = 1
    classes[masks["fungi"]] = 2
    classes[masks["sucking"]] = 3
    classes[masks["wilt"]] = 4
    classes[masks["soilborne"]] = 5
    key = register_tile(classes, transform, geom, PEST_COLORS) if include_tiles else None
    hectares = aoi_hectares(geom)
    inside_count = int(np.count_nonzero(valid))

    def hectares_for(mask: np.ndarray) -> float:
        if inside_count <= 0:
            return 0.0
        return float(np.count_nonzero(mask)) / inside_count * hectares

    areas = {name: hectares_for(mask) for name, mask in masks.items()}
    healthy = max(hectares - sum(areas.values()), 0.0)

    def pct(area: float) -> float:
        return round(area / hectares * 100.0, 2) if hectares else 0.0

    def group(name: str, class_id: int, children: dict) -> dict:
        return {
            "tile_url": _tile_url(key, class_id),
            "total_area_ha": round(areas[name], 2),
            "percentage": pct(areas[name]),
            "children": children,
        }

    empty = _tile_url(key, 0)
    hierarchy = {
        "healthy": {"total_area_ha": round(healthy, 2), "percentage": pct(healthy), "children": {}},
        "chewing": group(
            "chewing",
            1,
            {"leaf_eating_caterpillar": _child(areas["chewing"], areas["chewing"], _tile_url(key, 1))},
        ),
        "fungi": group(
            "fungi",
            2,
            {
                "red_rot": _child(0, areas["fungi"], empty),
                "rust": {"area_ha": 0, "pct_of_parent": 0},
                "downy_mildew": {"area_ha": 0, "pct_of_parent": 0},
                "aflatoxin": {"area_ha": 0, "pct_of_parent": 0},
            },
        ),
        "sucking": group(
            "sucking",
            3,
            {
                "thrips": {"area_ha": 0, "pct_of_parent": 0},
                "mealybug": {"area_ha": 0, "pct_of_parent": 0},
            },
        ),
        "wilt": group(
            "wilt",
            4,
            {"unknown_wilt": _child(areas["wilt"], areas["wilt"], _tile_url(key, 4))},
        ),
        "soilborne": group(
            "soilborne",
            5,
            {"grub": _child(areas["soilborne"], areas["soilborne"], None, include_tile=False)},
        ),
    }
    properties = {
        "plot_id": aoi["plot_id"],
        "total_area_ha": round(hectares, 2),
        "area_acres": round(hectares * 2.471, 2),
        "tile_url": _tile_url(key),
        "start_date": analysis_start,
        "end_date": analysis_end,
        "last_updated": datetime.now().isoformat(),
    }
    current = {
        "type": "FeatureCollection",
        "features": [_feature(aoi["plot_id"], geom, properties)],
        "hierarchy": hierarchy,
    }
    return _finish("pest_detection", aoi, analysis_start, analysis_end, current)


def render_tile(key: str, z: int, x: int, y: int, class_id: int | None = None) -> bytes:
    from fastapi import HTTPException

    entry = _tiles.get(key)
    if entry is None:
        raise HTTPException(status_code=404, detail="Tile cache expired or not found")
    _tiles.move_to_end(key)
    size = 256
    bounds = _xyz_bounds(x, y, z)
    minx, miny, maxx, maxy = transform_bounds("EPSG:4326", "EPSG:3857", *bounds)
    dst_transform = from_bounds(minx, miny, maxx, maxy, size, size)
    rendered = np.full((size, size), np.nan, dtype=np.float32)
    reproject(
        source=entry["array"],
        destination=rendered,
        src_transform=entry["transform"],
        src_crs="EPSG:4326",
        dst_transform=dst_transform,
        dst_crs="EPSG:3857",
        src_nodata=np.nan,
        dst_nodata=np.nan,
        resampling=Resampling.nearest,
    )
    tile_geometry = transform_geom("EPSG:4326", "EPSG:3857", entry["geometry"])
    inside = geometry_mask([tile_geometry], transform=dst_transform, invert=True, out_shape=(size, size))
    rgba = np.zeros((size, size, 4), dtype=np.uint8)
    finite = inside & np.isfinite(rendered)
    classes = np.rint(rendered)
    for cid, color in entry["palette"].items():
        if class_id is not None and int(cid) != int(class_id):
            continue
        selected = finite & (classes == cid)
        rgba[selected] = color
    rgba[~inside, 3] = 0
    if class_id is not None:
        rgba[classes != int(class_id), 3] = 0
    buffer = io.BytesIO()
    Image.fromarray(rgba, mode="RGBA").save(buffer, format="PNG")
    return buffer.getvalue()
