"""Google Earth Engine products that are not stored and not backfilled.

Water sources are open-water detection. Land-surface temperature is Landsat.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.geometry_helpers import admin_geometry_or_none, validate_location
from app.indices_compute import EarthEngineUnavailable, require_earth_engine
from app.services.classwise_stac import resolve_classwise_dates
from shapely.geometry import mapping, shape

LST_LABELS = {
    1: "< 25 °C",
    2: "25–30 °C",
    3: "30–35 °C",
    4: "35–40 °C",
    5: "> 40 °C",
}
LST_COLORS = {1: "0000FF", 2: "00FF00", 3: "FFFF00", 4: "FFA500", 5: "FF0000"}


def _ee():
    require_earth_engine()
    import ee

    return ee


def _geometry(district, subdistrict, village):
    district_name, subdistrict_name, village_name, _field = validate_location(district, subdistrict, village, None)
    raw = admin_geometry_or_none(district_name, subdistrict_name, village_name)
    if raw is None:
        return None, district_name, subdistrict_name, village_name
    geom = shape(raw)
    return geom, district_name, subdistrict_name, village_name


def _plot_id(district: str, subdistrict: str | None, village: str | None) -> str:
    return village or subdistrict or district


def _empty_water(plot_name: str, start_date: str, end_date: str, requested_start: str, cloud: float, message: str) -> dict:
    return {
        "type": "FeatureCollection",
        "features": [],
        "area_summary": {
            "total_area_hectare": 0,
            "water_area_hectare": 0,
            "water_area_percentage": 0,
        },
        "message": message,
        "plot_name": plot_name,
        "start_date": start_date,
        "end_date": end_date,
        "requested_start_date": requested_start,
        "max_cloud_pct": cloud,
    }


def _map_url(image) -> str:
    info = image.getMapId()
    fetcher = info.get("tile_fetcher")
    if fetcher is not None:
        return fetcher.url_format
    return str(info.get("tileurl") or info.get("urlFormat") or "")


def _mask_s2(image):
    ee = _ee()
    scl = image.select("SCL")
    qa = image.select("QA60")
    scl_ok = (
        scl.neq(0).And(scl.neq(1)).And(scl.neq(3)).And(scl.neq(8)).And(scl.neq(9)).And(scl.neq(10))
    )
    clear_bits = qa.bitwiseAnd(1 << 10).eq(0).And(qa.bitwiseAnd(1 << 11).eq(0))
    return image.updateMask(scl_ok.And(clear_bits))


def _collection(ee, geom, start: str, end: str, cloud: float):
    return (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(geom)
        .filterDate(start, end)
        .filter(ee.Filter.lte("CLOUDY_PIXEL_PERCENTAGE", cloud))
    )


def compute_ndwi(district, subdistrict, village, end_date, start_date, max_cloud_pct: float) -> dict:
    requested_start, end_value = resolve_classwise_dates(start_date, end_date)
    try:
        geom, district_name, subdistrict_name, village_name = _geometry(district, subdistrict, village)
    except Exception as exc:
        from fastapi import HTTPException

        if isinstance(exc, HTTPException) and exc.status_code == 400:
            raise
        geom = None
        district_name, subdistrict_name, village_name = district, subdistrict, village
    plot_name = _plot_id(str(district_name or ""), subdistrict_name, village_name)
    if geom is None or geom.is_empty:
        return _empty_water(plot_name, requested_start, end_value, requested_start, max_cloud_pct, "Boundary not found")
    try:
        ee = _ee()
    except EarthEngineUnavailable as exc:
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail=f"Earth Engine is unavailable: {exc}") from exc

    ee_geom = ee.Geometry(mapping(geom))
    cloud = float(max_cloud_pct)
    start_value = requested_start
    collection = _collection(ee, ee_geom, start_value, end_value, cloud)
    count = int(collection.size().getInfo() or 0)
    if count == 0:
        start_value = (datetime.strptime(end_value, "%Y-%m-%d").date() - timedelta(days=60)).isoformat()
        collection = _collection(ee, ee_geom, start_value, end_value, cloud)
        count = int(collection.size().getInfo() or 0)
    if count == 0 and cloud < 80:
        cloud = 80
        collection = _collection(ee, ee_geom, start_value, end_value, cloud)
        count = int(collection.size().getInfo() or 0)
    if count == 0:
        return _empty_water(plot_name, start_value, end_value, requested_start, cloud, "No Sentinel-2 images in the selected period")

    composite = collection.map(_mask_s2).median().clip(ee_geom)
    ndwi = composite.normalizedDifference(["B3", "B8"])
    mndwi = composite.normalizedDifference(["B3", "B11"])
    water = ndwi.gt(0.15).Or(mndwi.gt(0.05))
    tile = _map_url(water.selfMask().visualize(min=0, max=1, palette=["0000ff"]))
    pixel_area = ee.Image.pixelArea().divide(10000)
    reducer = ee.Reducer.sum()
    total = pixel_area.reduceRegion(reducer, ee_geom, 10, bestEffort=True, maxPixels=1e13).getInfo() or {}
    water_sum = pixel_area.updateMask(water).reduceRegion(reducer, ee_geom, 10, bestEffort=True, maxPixels=1e13).getInfo() or {}
    total_ha = round(float(total.get("area") or 0), 2)
    water_ha = round(float(water_sum.get("area") or 0), 2)
    percentage = round((water_ha / total_ha * 100.0) if total_ha else 0.0, 2)
    latest = ee.Date(collection.aggregate_max("system:time_start")).format("YYYY-MM-dd").getInfo()
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "id": f"water-source-{plot_name}",
                "geometry": mapping(geom),
                "properties": {
                    "plot_name": plot_name,
                    "start_date": start_value,
                    "end_date": end_value,
                    "requested_start_date": requested_start,
                    "max_cloud_pct": cloud,
                    "latest_image_date": latest,
                    "image_count": count,
                    "tile_url": tile,
                    "last_updated": datetime.now().isoformat(),
                },
            }
        ],
        "area_summary": {
            "total_area_hectare": total_ha,
            "water_area_hectare": water_ha,
            "water_area_percentage": percentage,
        },
    }


def _lst_class_image(ee, lst):
    return (
        lst.lt(25).multiply(1)
        .add(lst.gte(25).And(lst.lt(30)).multiply(2))
        .add(lst.gte(30).And(lst.lt(35)).multiply(3))
        .add(lst.gte(35).And(lst.lt(40)).multiply(4))
        .add(lst.gte(40).multiply(5))
        .rename("lst_class")
        .updateMask(lst.mask())
    )


def _outline(ee, geom, image):
    painted = ee.Image().byte().paint(ee.FeatureCollection([ee.Feature(geom)]), 1, 2)
    white = painted.selfMask().visualize(min=0, max=1, palette=["ffffff"])
    return image.blend(white)


def compute_lst(district, subdistrict, village, start_date: str, end_date: str) -> dict:
    from fastapi import HTTPException

    geom, district_name, subdistrict_name, village_name = _geometry(district, subdistrict, village)
    if geom is None or geom.is_empty:
        raise HTTPException(status_code=404, detail="Boundary not found")
    try:
        ee = _ee()
    except EarthEngineUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"Earth Engine is unavailable: {exc}") from exc
    ee_geom = ee.Geometry(mapping(geom))
    landsat = (
        ee.ImageCollection("LANDSAT/LC08/C02/T1_L2")
        .merge(ee.ImageCollection("LANDSAT/LC09/C02/T1_L2"))
        .filterDate(start_date, end_date)
        .filterBounds(ee_geom)
        .filter(ee.Filter.lt("CLOUD_COVER", 10))
        .select("ST_B10")
    )
    if int(landsat.size().getInfo() or 0) == 0:
        raise HTTPException(status_code=404, detail="No Landsat scenes found for the given date range.")
    lst = landsat.map(lambda image: image.multiply(0.00341802).add(149.0).subtract(273.15)).mean().clip(ee_geom)
    classes = _lst_class_image(ee, lst)
    continuous = _outline(
        ee,
        ee_geom,
        lst.visualize(min=20, max=45, palette=["0000FF", "00FFFF", "00FF00", "FFFF00", "FFA500", "FF0000"]),
    )
    combined = _outline(ee, ee_geom, classes.visualize(min=1, max=5, palette=[LST_COLORS[i] for i in range(1, 6)]))
    class_tiles = {}
    for class_id, label in LST_LABELS.items():
        solid = classes.eq(class_id).selfMask().visualize(min=1, max=1, palette=[LST_COLORS[class_id]])
        class_tiles[label] = _map_url(_outline(ee, ee_geom, solid))
    stacked = ee.Image.pixelArea().addBands(classes)
    grouped = stacked.reduceRegion(
        reducer=ee.Reducer.sum().group(groupField=1, groupName="lst_class"),
        geometry=ee_geom,
        scale=30,
        maxPixels=1e13,
        bestEffort=True,
    ).getInfo() or {}
    features = []
    for group in grouped.get("groups") or []:
        class_id = int(group.get("lst_class") or 0)
        if class_id not in LST_LABELS:
            continue
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "class_id": class_id,
                    "area_hectare": float(group.get("sum") or 0) / 10000.0,
                    "class_label": LST_LABELS[class_id],
                },
            }
        )
    return {
        "plot_id": _plot_id(district_name, subdistrict_name, village_name),
        "geometry": mapping(geom),
        "lst_tile_url": _map_url(continuous),
        "lst_class_combined_tile": _map_url(combined),
        "lst_class_tiles": class_tiles,
        "lst_classwise_area_hectare": {"features": features},
        "start_date": start_date,
        "end_date": end_date,
        "last_updated": datetime.now().isoformat(),
    }
