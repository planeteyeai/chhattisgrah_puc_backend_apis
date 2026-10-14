"""Hectare land-use statistics from Earth Search.

This is not a classwise tile product and it is not stored or backfilled.
Sentinel-2 uses collection sentinel-2-l2a. Water is classified before vegetation.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np

from app.geometry_helpers import lookup_aoi
from app.services.classwise_stac import (
    _inside,
    _read_pair,
    _stac_search,
    _unsigned,
    aoi_hectares,
    pick_sensor,
    raster_resolution,
)
from app.satellite_analysis import _apply_s2_scale, _has_asset, _item_date, _read_s1_band, _read_s2_band, _s1_iw_vv_vh

S2_COLLECTION = "sentinel-2-l2a"
S1_COLLECTION = "sentinel-1-grd"
SCL_NODATA = {0, 1, 3, 8, 9, 10}


def resolve_land_dates(end_date: str | None) -> tuple[str, str]:
    from fastapi import HTTPException

    if end_date:
        end = datetime.strptime(end_date, "%Y-%m-%d").date()
    else:
        end = date.today()
    if end > date.today():
        raise HTTPException(status_code=400, detail="endDate cannot be in the future")
    start = end - timedelta(days=19)
    return start.isoformat(), end.isoformat()


def _scope(subdistrict: str | None, village: str | None) -> str:
    if village:
        return "village"
    if subdistrict:
        return "subdistrict"
    return "district"


def _hectares(count: int, inside: int, total_ha: float) -> float:
    if inside <= 0:
        return 0.0
    return count / inside * total_ha


def _s2_classes(item: dict, geom, resolution: float):
    red, nir, transform, _crs = _read_pair(item, "red", "nir", geom, resolution)
    green, _, _ = _read_s2_band(item, "green", geom, resolution=resolution)
    swir, _, _ = _read_s2_band(item, "swir16", geom, resolution=resolution)
    scl, _, _ = _read_s2_band(item, "scl", geom, resolution=resolution)
    green = _apply_s2_scale(green, item, "green")
    swir = _apply_s2_scale(swir, item, "swir16")
    height = min(red.shape[0], nir.shape[0], green.shape[0], swir.shape[0], scl.shape[0])
    width = min(red.shape[1], nir.shape[1], green.shape[1], swir.shape[1], scl.shape[1])
    red, nir, green, swir, scl = (band[:height, :width] for band in (red, nir, green, swir, scl))
    ndvi = (nir - red) / (nir + red + 1e-12)
    mndwi = (green - swir) / (green + swir + 1e-12)
    clear = np.isfinite(ndvi) & np.isfinite(mndwi) & ~np.isin(np.rint(scl), list(SCL_NODATA))
    water = clear & ((mndwi > 0.20) | ((mndwi > 0.12) & (ndvi < 0.25)))
    barren = clear & ~water & (ndvi < 0.20)
    stress = clear & ~water & (ndvi >= 0.20) & (ndvi < 0.35)
    green_mask = clear & ~water & (ndvi >= 0.35) & (ndvi < 0.50)
    excellent = clear & ~water & (ndvi >= 0.50)
    return {
        "water": water,
        "barren": barren,
        "stress": stress,
        "green": green_mask,
        "excellent": excellent,
        "clear": clear,
        "transform": transform,
        "shape": red.shape,
    }


def _s1_classes(item: dict, geom, resolution: float):
    vv, transform, _crs = _read_s1_band(item, "vv", geom, resolution=resolution)
    vh, _, _ = _read_s1_band(item, "vh", geom, resolution=resolution)
    height = min(vv.shape[0], vh.shape[0])
    width = min(vv.shape[1], vh.shape[1])
    vv, vh = vv[:height, :width], vh[:height, :width]
    clear = np.isfinite(vv) & np.isfinite(vh)
    water = clear & (vv < -18) & (vh < -22)
    barren = clear & ~water & (vh >= -11)
    stress = clear & ~water & (vh < -11) & (vh > -13)
    green_mask = clear & ~water & (vh <= -13) & (vh > -15)
    excellent = clear & ~water & (vh <= -15)
    return {
        "water": water,
        "barren": barren,
        "stress": stress,
        "green": green_mask,
        "excellent": excellent,
        "clear": clear,
        "transform": transform,
        "shape": vv.shape,
    }


def _mosaic(scenes: list[dict], inside: np.ndarray) -> dict[str, np.ndarray]:
    names = ("water", "barren", "stress", "green", "excellent")
    filled = np.zeros(inside.shape, dtype=bool)
    masks = {name: np.zeros(inside.shape, dtype=bool) for name in names}
    for scene in scenes:
        clear = scene["clear"]
        if clear.shape != inside.shape:
            clear = clear[: inside.shape[0], : inside.shape[1]]
        take = inside & clear & ~filled
        for name in names:
            scene_mask = scene[name]
            if scene_mask.shape != inside.shape:
                scene_mask = scene_mask[: inside.shape[0], : inside.shape[1]]
            masks[name] |= take & scene_mask
        filled |= take
    return masks


def compute_land_statistics(district: str, subdistrict: str | None, village: str | None, end_date: str | None) -> dict:
    from fastapi import HTTPException

    start_date, end_value = resolve_land_dates(end_date)
    aoi = lookup_aoi(district, subdistrict, village, None)
    geom = aoi["geometry"]
    resolution = raster_resolution(geom)
    with _unsigned():
        scene = pick_sensor(
            geom,
            start_date,
            end_value,
            ("red", "nir", "green", "swir16", "scl"),
            s2_collection=S2_COLLECTION,
            s1_collection=S1_COLLECTION,
        )
        if scene["sensor"] == "s2":
            items = [
                item
                for item in _stac_search(S2_COLLECTION, geom, start_date, end_value, 12)
                if all(_has_asset(item, name) for name in ("red", "nir", "green", "swir16", "scl"))
            ]
            classified = []
            transform = None
            for item in items:
                try:
                    parsed = _s2_classes(item, geom, resolution)
                except Exception:
                    continue
                transform = parsed["transform"]
                classified.append(parsed)
            satellite = "Sentinel-2"
            collection = S2_COLLECTION
            data_source = "Sentinel-2"
        else:
            items = [
                item
                for item in _stac_search(S1_COLLECTION, geom, start_date, end_value, 12)
                if _s1_iw_vv_vh(item)
            ]
            classified = []
            transform = None
            for item in items:
                try:
                    parsed = _s1_classes(item, geom, resolution)
                except Exception:
                    continue
                transform = parsed["transform"]
                classified.append(parsed)
            satellite = "Sentinel-1"
            collection = S1_COLLECTION
            data_source = "Sentinel-1"
    if not classified or transform is None:
        raise HTTPException(status_code=404, detail="No classified pixels inside the AOI")
    inside = _inside(np.zeros(classified[0]["shape"], dtype=np.float32), transform, geom)
    masks = _mosaic(classified, inside)
    total = aoi_hectares(geom)
    inside_count = int(np.count_nonzero(inside))
    water = _hectares(int(np.count_nonzero(masks["water"])), inside_count, total)
    barren = _hectares(int(np.count_nonzero(masks["barren"])), inside_count, total)
    stress = _hectares(int(np.count_nonzero(masks["stress"])), inside_count, total)
    green = _hectares(int(np.count_nonzero(masks["green"])), inside_count, total)
    excellent = _hectares(int(np.count_nonzero(masks["excellent"])), inside_count, total)
    vegetative = stress + green + excellent
    classified_ha = water + barren + vegetative
    return {
        "vegetativeLandHectare": round(vegetative, 2),
        "barrenLandHectare": round(barren, 2),
        "greenLandHectare": round(green, 2),
        "landUnderStressHectare": round(stress, 2),
        "excellentHealthLandHectare": round(excellent, 2),
        "waterAreaHectare": round(water, 2),
        "totalAreaHectare": round(total, 2),
        "classifiedAreaHectare": round(classified_ha, 2),
        "unclassifiedAreaHectare": round(max(total - classified_ha, 0.0), 2),
        "imageDate": _item_date(items[0]) if items else scene["date"],
        "satellite": satellite,
        "collection": collection,
        "dataSource": data_source,
        "scope": _scope(aoi["subdistrict"], aoi["village"]),
        "district": aoi["district"],
        "subdistrict": aoi["subdistrict"],
        "village": aoi["village"],
        "startDate": start_date,
        "endDate": end_value,
    }
