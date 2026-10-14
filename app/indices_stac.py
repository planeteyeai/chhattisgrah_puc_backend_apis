"""Scene indices from the Earth Search STAC API on AWS.

Catalog: https://earth-search.aws.element84.com/v1
Optical scenes: collection sentinel-2-l2a. Bands are surface reflectance
scaled by 0.0001. The cloud mask is SCL in {4, 5, 6, 7}. This catalog does
not publish QA60, so those bits are not applied. Spatial means are taken at
20 m inside the location geometry.

Radar scenes are sentinel-1-grd. Those GeoTIFFs are requester-pays, so the
radar columns stay null unless AWS credentials are configured. Weather and
soil collections are not in this catalog, so those columns stay null.
"""

from __future__ import annotations

import json
import logging
import math
import os
import urllib.request
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.features import rasterize
from rasterio.transform import from_origin
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_geom

from app.indices_compute import clean_stat

logger = logging.getLogger(__name__)

SEARCH_URL = "https://earth-search.aws.element84.com/v1/search"
S2_COLLECTION = "sentinel-2-l2a"
S1_COLLECTION = "sentinel-1-grd"
OPTICAL_SCALE_M = 20.0
MAX_SIDE = 4000
SCL_CLEAR = {4, 5, 6, 7}
OPTICAL_ASSETS = ("scl", "blue", "green", "red", "rededge1", "nir", "swir16")
OPTICAL_COLUMNS = (
    "ndvi",
    "evi",
    "evi2",
    "gndvi",
    "ndre",
    "ndwi_mcfeeters",
    "ndwi_gao",
    "ndmi",
    "rvi_optical",
    "ndbi",
    "bsi",
    "savi",
    "osavi",
)


def _aws_credentials_present() -> bool:
    return bool(
        os.environ.get("AWS_ACCESS_KEY_ID")
        or os.environ.get("AWS_PROFILE")
        or os.environ.get("AWS_SHARED_CREDENTIALS_FILE")
    )


if not _aws_credentials_present():
    os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")
os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
os.environ.setdefault("GDAL_HTTP_TIMEOUT", "60")
os.environ.setdefault("GDAL_HTTP_MAX_RETRY", "2")
os.environ.setdefault("VSI_CACHE", "TRUE")


def _request_json(url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/geo+json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = json.loads(response.read().decode())
    if not isinstance(payload, dict):
        raise RuntimeError("Earth Search returned a non-object response")
    return payload


def _search(body: dict[str, Any]) -> list[dict[str, Any]]:
    payload = _request_json(SEARCH_URL, body)
    features = list(payload.get("features") or [])
    pages = 1
    while pages < 20:
        link = next((item for item in payload.get("links") or [] if item.get("rel") == "next"), None)
        if not link or not link.get("href"):
            break
        merged = dict(body)
        if isinstance(link.get("body"), dict):
            merged.update(link["body"])
        if str(link.get("method") or "GET").upper() == "POST":
            payload = _request_json(link["href"], merged)
        else:
            payload = _request_json(link["href"])
        features.extend(payload.get("features") or [])
        pages += 1
    return features


def _interval(start: date, end_exclusive: date) -> str:
    return f"{start.isoformat()}T00:00:00Z/{end_exclusive.isoformat()}T00:00:00Z"


def _product_id(properties: dict[str, Any], item_id: str) -> str:
    uri = str(properties.get("s2:product_uri") or item_id)
    if uri.endswith(".SAFE"):
        return uri[: -len(".SAFE")]
    return uri


def _tile_id(properties: dict[str, Any]) -> str | None:
    code = str(properties.get("grid:code") or "")
    tile = code.split("-")[-1] if code else ""
    return tile or None


def _obs_date(properties: dict[str, Any]) -> date | None:
    raw = properties.get("datetime")
    if not raw:
        return None
    parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).date()


def search_sentinel2(geometry: dict[str, Any], start: date, end_exclusive: date) -> list[dict[str, Any]]:
    """Sentinel-2 L2A items intersecting the AOI. The end date is exclusive."""
    features = _search(
        {
            "collections": [S2_COLLECTION],
            "intersects": geometry,
            "datetime": _interval(start, end_exclusive),
            "limit": 100,
        }
    )
    scenes: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in features:
        properties = item.get("properties") or {}
        assets = item.get("assets") or {}
        hrefs = {
            name: assets[name].get("href")
            for name in OPTICAL_ASSETS
            if isinstance(assets.get(name), dict) and assets[name].get("href")
        }
        if "scl" not in hrefs:
            continue
        obs_date = _obs_date(properties)
        product_id = _product_id(properties, str(item.get("id") or ""))
        if obs_date is None or not product_id or product_id in seen:
            continue
        seen.add(product_id)
        scenes.append(
            {
                "product_id": product_id,
                "obs_date": obs_date.isoformat(),
                "tile_id": _tile_id(properties),
                "cloud_coverage": clean_stat(properties.get("eo:cloud_cover")),
                "assets": hrefs,
            }
        )
    return scenes


def _walk_bounds(geometry: dict[str, Any]) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []

    def walk(node: object) -> None:
        if isinstance(node, (list, tuple)) and node and isinstance(node[0], (int, float)):
            xs.append(float(node[0]))
            ys.append(float(node[1]))
            return
        if isinstance(node, (list, tuple)):
            for item in node:
                walk(item)

    walk(geometry.get("coordinates"))
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _grid(scl_href: str, geometry: dict[str, Any]) -> dict[str, Any] | None:
    with rasterio.open(scl_href) as source:
        projected = transform_geom("EPSG:4326", source.crs, geometry)
        bounds = _walk_bounds(projected)
        if bounds is None:
            return None
        minx, miny, maxx, maxy = bounds
        resolution = OPTICAL_SCALE_M
        width = max(1, math.ceil((maxx - minx) / resolution))
        height = max(1, math.ceil((maxy - miny) / resolution))
        if width > MAX_SIDE or height > MAX_SIDE:
            resolution = max((maxx - minx) / MAX_SIDE, (maxy - miny) / MAX_SIDE, OPTICAL_SCALE_M)
            width = max(1, math.ceil((maxx - minx) / resolution))
            height = max(1, math.ceil((maxy - miny) / resolution))
        minx = math.floor(minx / resolution) * resolution
        maxy = math.ceil(maxy / resolution) * resolution
        width = max(1, int(round((math.ceil(maxx / resolution) * resolution - minx) / resolution)))
        height = max(1, int(round((maxy - math.floor(miny / resolution) * resolution) / resolution)))
        transform = from_origin(minx, maxy, resolution, resolution)
        covered = rasterize(
            [(projected, 1)],
            out_shape=(height, width),
            transform=transform,
            fill=0,
            dtype="uint8",
        )
        if int(covered.sum()) == 0:
            return None
        return {"crs": source.crs, "transform": transform, "width": width, "height": height, "aoi": covered > 0}


def _read_grid(href: str, grid: dict[str, Any], resampling: Resampling) -> np.ndarray:
    with rasterio.open(href) as source:
        with WarpedVRT(
            source,
            crs=grid["crs"],
            transform=grid["transform"],
            width=grid["width"],
            height=grid["height"],
            resampling=resampling,
        ) as warped:
            return warped.read(1)


def _mean(values: np.ndarray) -> float | None:
    finite = np.isfinite(values)
    if not finite.any():
        return None
    return float(values[finite].mean())


def _divide(numerator: np.ndarray, denominator: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        result = numerator / denominator
    result[~np.isfinite(result)] = np.nan
    return result


def _optical_means(assets: dict[str, str], geometry: dict[str, Any]) -> dict[str, float | None]:
    empty = {column: None for column in OPTICAL_COLUMNS}
    try:
        grid = _grid(assets["scl"], geometry)
    except Exception:
        logger.exception("sentinel-2 grid failed")
        return empty
    if grid is None:
        return empty
    try:
        scl = _read_grid(assets["scl"], grid, Resampling.nearest)
    except Exception:
        logger.exception("sentinel-2 scl read failed")
        return empty
    clear = grid["aoi"] & np.isin(scl, list(SCL_CLEAR))
    if not clear.any():
        return empty
    reflectance: dict[str, np.ndarray | None] = {}
    for name in ("blue", "green", "red", "rededge1", "nir", "swir16"):
        href = assets.get(name)
        if not href:
            reflectance[name] = None
            continue
        try:
            raw = _read_grid(href, grid, Resampling.average).astype("float32")
        except Exception:
            logger.exception("sentinel-2 %s read failed", name)
            reflectance[name] = None
            continue
        scaled = raw * 0.0001
        scaled[(raw <= 0) | ~clear] = np.nan
        reflectance[name] = scaled
    blue = reflectance["blue"]
    green = reflectance["green"]
    red = reflectance["red"]
    red_edge = reflectance["rededge1"]
    nir = reflectance["nir"]
    swir1 = reflectance["swir16"]
    values = dict(empty)
    if nir is not None and red is not None:
        values["ndvi"] = _mean(_divide(nir - red, nir + red))
        values["evi"] = _mean(_divide(2.5 * (nir - red), nir + 6 * red - 7.5 * (blue if blue is not None else np.nan) + 1)) if blue is not None else None
        values["evi2"] = _mean(_divide(nir - red, nir + 2 * red + 1))
        values["rvi_optical"] = _mean(_divide(nir, red + 1e-6))
        values["savi"] = _mean(_divide(nir - red, nir + red + 0.5) * 1.5)
        values["osavi"] = _mean(_divide(nir - red, nir + red + 0.16))
    if nir is not None and green is not None:
        values["gndvi"] = _mean(_divide(nir - green, nir + green))
    if nir is not None and red_edge is not None:
        values["ndre"] = _mean(_divide(nir - red_edge, nir + red_edge))
    if green is not None and nir is not None:
        values["ndwi_mcfeeters"] = _mean(_divide(green - nir, green + nir))
    if nir is not None and swir1 is not None:
        ndwi_gao = _mean(_divide(nir - swir1, nir + swir1))
        values["ndwi_gao"] = ndwi_gao
        values["ndmi"] = ndwi_gao
        values["ndbi"] = _mean(_divide(swir1 - nir, swir1 + nir))
    if swir1 is not None and red is not None and nir is not None and blue is not None:
        values["bsi"] = _mean(_divide((swir1 + red) - (nir + blue), (swir1 + red) + (nir + blue)))
    return values


def _radar_readable() -> bool:
    if os.environ.get("AWS_NO_SIGN_REQUEST", "").strip().upper() in {"YES", "TRUE", "1"}:
        return False
    return _aws_credentials_present()


def _radar_means(geometry: dict[str, Any], obs_date: date) -> dict[str, float | None]:
    empty = {"vh_vv_ratio": None, "rvi_radar": None}
    if not _radar_readable():
        return empty
    try:
        features = _search(
            {
                "collections": [S1_COLLECTION],
                "intersects": geometry,
                "datetime": _interval(obs_date, obs_date + timedelta(days=1)),
                "query": {
                    "sar:instrument_mode": {"eq": "IW"},
                    "s1:resolution": {"eq": "high"},
                },
                "limit": 20,
            }
        )
    except Exception:
        logger.exception("sentinel-1 search failed")
        return empty
    vv_stack: list[np.ndarray] = []
    vh_stack: list[np.ndarray] = []
    grid = None
    for item in features:
        properties = item.get("properties") or {}
        polarizations = {str(value).upper() for value in properties.get("sar:polarizations") or []}
        if not {"VV", "VH"} <= polarizations:
            continue
        assets = item.get("assets") or {}
        vv_href = (assets.get("vv") or {}).get("href")
        vh_href = (assets.get("vh") or {}).get("href")
        if not vv_href or not vh_href:
            continue
        try:
            if grid is None:
                grid = _grid(vv_href, geometry)
            if grid is None:
                return empty
            vv = _read_grid(vv_href, grid, Resampling.average).astype("float32")
            vh = _read_grid(vh_href, grid, Resampling.average).astype("float32")
        except Exception:
            logger.exception("sentinel-1 read failed for %s", item.get("id"))
            continue
        vv[~grid["aoi"] | ~np.isfinite(vv)] = np.nan
        vh[~grid["aoi"] | ~np.isfinite(vh)] = np.nan
        vv_stack.append(vv)
        vh_stack.append(vh)
    if grid is None or not vv_stack:
        return empty
    vv_median = np.nanmedian(np.stack(vv_stack), axis=0)
    vh_median = np.nanmedian(np.stack(vh_stack), axis=0)
    return {
        "vh_vv_ratio": _mean(_divide(vh_median, vv_median + 1e-6)),
        "rvi_radar": _mean(_divide(vv_median, vh_median + 1e-6)),
    }


def list_sentinel2(geometry: dict[str, Any], start: date, end_exclusive: date) -> list[dict[str, Any]]:
    return search_sentinel2(geometry, start, end_exclusive)


def compute_scene(scene: dict[str, Any], geometry: dict[str, Any], include_aux: bool) -> dict[str, Any] | None:
    """One Sentinel-2 STAC item. A failed source leaves its columns null."""
    obs_date = date.fromisoformat(scene["obs_date"]) if isinstance(scene.get("obs_date"), str) else scene.get("obs_date")
    product_id = scene.get("product_id")
    if obs_date is None or not product_id:
        return None
    row: dict[str, Any] = {
        "obs_date": obs_date,
        "product_id": str(product_id),
        "tile_id": scene.get("tile_id"),
        "cloud_coverage": clean_stat(scene.get("cloud_coverage")),
        **{column: None for column in OPTICAL_COLUMNS},
        "rvi_radar": None,
        "vh_vv_ratio": None,
        "lst": None,
        "temperature": None,
        "precipitation_daily": None,
        "precipitation_sum": None,
        "soil_water_fraction_surface": None,
        "soil_water_fraction_subsurface": None,
    }
    assets = scene.get("assets") or {}
    if assets.get("scl"):
        try:
            row.update(_optical_means(assets, geometry))
        except Exception:
            logger.exception("optical indices failed for %s", product_id)
    if include_aux:
        try:
            row.update(_radar_means(geometry, obs_date))
        except Exception:
            logger.exception("radar indices failed for %s", product_id)
    return row
