"""Satellite analysis endpoints backed by field boundaries in MySQL.

Same request parameters and response contracts as the AWS STAC admintest
service (growth, water uptake, soil moisture, pest detection, tiles, and
daily report). Plot geometry and identity come from `field_boundaries`,
not from Django. `plot_name` is the `field_boundaries.id` value.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import io
import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from collections import OrderedDict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import requests
import rasterio
from cachetools import TTLCache
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response
from PIL import Image
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from rasterio.enums import Resampling
from rasterio.features import geometry_mask
from rasterio.transform import from_bounds
from affine import Affine
from rasterio.crs import CRS
from rasterio.warp import reproject, transform_bounds, transform_geom
from shapely.geometry import shape, mapping
from sqlalchemy import text

from app.config import settings
from app.database import engine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("admintest_aws")

_S3_VIRTUAL_HOST = re.compile(
    r"^https://(?P<bucket>[a-z0-9.-]+)\.s3(?:[.-](?P<region>[a-z0-9-]+))?\.amazonaws\.com/(?P<key>.+)$",
    re.IGNORECASE,
)
_S3_PATH_HOST = re.compile(
    r"^https://s3(?:[.-](?P<region>[a-z0-9-]+))?\.amazonaws\.com/(?P<bucket>[^/]+)/(?P<key>.+)$",
    re.IGNORECASE,
)


def _aws_credentials() -> dict[str, str]:
    """AWS keys used to sign Earth Search COG reads. STAC search stays public."""
    key = (settings.aws_access_key_id or os.getenv("AWS_ACCESS_KEY_ID") or "").strip()
    secret = (settings.aws_secret_access_key or os.getenv("AWS_SECRET_ACCESS_KEY") or "").strip()
    if not key or not secret:
        return {}
    region = (settings.aws_default_region or os.getenv("AWS_DEFAULT_REGION") or "us-west-2").strip()
    creds = {
        "AWS_ACCESS_KEY_ID": key,
        "AWS_SECRET_ACCESS_KEY": secret,
        "AWS_DEFAULT_REGION": region,
        "AWS_REGION": region,
        "AWS_REQUEST_PAYER": "requester",
    }
    token = (settings.aws_session_token or os.getenv("AWS_SESSION_TOKEN") or "").strip()
    if token:
        creds["AWS_SESSION_TOKEN"] = token
    return creds


def _configure_aws() -> None:
    """Prefer signed S3 access when an AWS key is configured."""
    os.environ.setdefault("AWS_S3_USE_HTTPS", "YES")
    os.environ.setdefault("GDAL_HTTP_MULTIPLEX", "YES")
    os.environ.setdefault("GDAL_HTTP_VERSION", "2")
    os.environ.setdefault("GDAL_DISABLE_READDIR_ON_OPEN", "EMPTY_DIR")
    creds = _aws_credentials()
    if creds:
        os.environ.pop("AWS_NO_SIGN_REQUEST", None)
        os.environ.update(creds)
        logger.info("Earth Search COGs will use signed AWS requests")
        return
    os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")


# Classwise analysis forces unsigned public COG reads even when AWS keys exist.
_force_unsigned: contextvars.ContextVar[bool] = contextvars.ContextVar("stac_force_unsigned", default=False)


def _gdal_env_options() -> dict[str, str]:
    options = {
        "AWS_S3_USE_HTTPS": "YES",
        "GDAL_HTTP_MULTIPLEX": "YES",
        "GDAL_HTTP_VERSION": "2",
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
    }
    if _force_unsigned.get():
        options["AWS_NO_SIGN_REQUEST"] = "YES"
        return options
    creds = _aws_credentials()
    if creds:
        options.update(creds)
    else:
        options["AWS_NO_SIGN_REQUEST"] = "YES"
    return options


def _signed_cog_href(href: str) -> str:
    """Turn an S3 HTTPS COG URL into s3:// so GDAL can sign requester-pays reads."""
    if not _aws_credentials() or href.lower().startswith("s3://"):
        return href
    bare = href.split("?", 1)[0]
    match = _S3_VIRTUAL_HOST.match(bare) or _S3_PATH_HOST.match(bare)
    if not match:
        return href
    return f"s3://{match.group('bucket')}/{match.group('key')}"


def _stac_headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/geo+json"}
    api_key = (settings.aws_stac_api_key or os.getenv("AWS_STAC_API_KEY") or "").strip()
    if api_key:
        headers["x-api-key"] = api_key
    return headers


_configure_aws()

# Configuration
# ---------------------------------------------------------------------------
STAC_API_URL = (settings.stac_api_url or os.getenv("STAC_API_URL") or "https://earth-search.aws.element84.com/v1").rstrip("/")
S2_COLLECTION = os.getenv("STAC_S2_COLLECTION", "sentinel-2-c1-l2a")
S1_COLLECTION = os.getenv("STAC_S1_COLLECTION", "sentinel-1-grd")
STAC_TIMEOUT = int(os.getenv("AWS_STAC_TIMEOUT", "45"))
ANALYSIS_CACHE_TTL = int(os.getenv("ANALYSIS_CACHE_TTL", "1800"))
TILE_CACHE_TTL = int(os.getenv("TILE_CACHE_TTL", "1800"))
TILE_CACHE_MAX = int(os.getenv("TILE_CACHE_MAX", "128"))
MAX_SAMPLE_COORDS = int(os.getenv("MAX_SAMPLE_COORDS", "2000"))

# Original visualization palettes.
GROWTH_PALETTE = ["#9E9E9E", "#bc1e29", "#58cf54", "#28ae31", "#056c3e"]
WATER_PALETTE = ["#9E9E9E", "#EBFF34", "#CC8213", "#00F5D4", "#00B4D8", "#1348E8"]
SOIL_PALETTE = ["#9E9E9E", "#2FC0D3", "#4365D4", "#473CDF", "#2116BF", "#000475"]
PEST_PALETTE = ["#FF0000", "#000000"]

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
router = APIRouter(tags=["satellite-analysis"])

REQUEST_COUNT = Counter(
    "http_requests_total_admin_aws",
    "Total HTTP Requests for AWS STAC Admin API",
    ["method", "endpoint", "status"],
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds_admin_aws",
    "Request latency for AWS STAC Admin API",
    ["endpoint"],
)

_plot_cache: TTLCache = TTLCache(maxsize=512, ttl=300)
_response_cache: TTLCache = TTLCache(maxsize=1000, ttl=ANALYSIS_CACHE_TTL)
_stac_item_cache: TTLCache = TTLCache(maxsize=2048, ttl=900)
_plot_lock = threading.RLock()

# tile key -> (created_at, temp GeoTIFF path, metadata)
_tile_cache: OrderedDict[str, Tuple[float, str, Dict[str, Any]]] = OrderedDict()
_tile_lock = threading.RLock()

# Rendered PNG cache: avoids reprojecting the same XYZ tile on every request.
_png_tile_cache: OrderedDict[str, Tuple[float, bytes]] = OrderedDict()
_png_tile_lock = threading.RLock()
PNG_TILE_CACHE_MAX = int(os.getenv("PNG_TILE_CACHE_MAX", "2048"))

# S1 band cache: the Pest workflow can reuse the same VV/VH scene several times
# (analysis, baseline and descending-orbit branches). Cache only the already-read
# AOI arrays; no classification or threshold logic is changed.
_s1_band_cache: TTLCache = TTLCache(maxsize=256, ttl=900)
_s1_band_lock = threading.RLock()

# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
async def prometheus_middleware(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration = time.time() - start
    REQUEST_COUNT.labels(request.method, request.url.path, str(response.status_code)).inc()
    REQUEST_LATENCY.labels(request.url.path).observe(duration)
    return response

# ---------------------------------------------------------------------------
# Generic utilities
# ---------------------------------------------------------------------------
def default_start_date(end_date: Optional[str] = None) -> str:
    if end_date is None:
        d = date.today()
    else:
        try:
            d = datetime.strptime(end_date, "%Y-%m-%d").date()
        except Exception:
            d = date.today()
    return (d - timedelta(days=15)).strftime("%Y-%m-%d")


def _date_only(value: Any) -> Optional[str]:
    if value is None:
        return None
    s = str(value)
    return s[:10] if len(s) >= 10 else s


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    s = str(value).replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _as_geojson_geometry(value: Any) -> Optional[dict]:
    if not value:
        return None
    if isinstance(value, dict) and value.get("type") and value.get("coordinates"):
        return value
    return None


def _plot_geometry(plot: dict):
    geom = plot.get("geometry")
    if geom is not None:
        return geom
    coords = plot.get("original_coords")
    geom_type = plot.get("geom_type", "Polygon")
    if coords:
        return shape({"type": geom_type, "coordinates": coords})
    return None


def _strip_z_coords(coords: Any):
    if isinstance(coords, (list, tuple)):
        if coords and isinstance(coords[0], (int, float)):
            return list(coords[:2])
        return [_strip_z_coords(x) for x in coords]
    return coords


def _make_geometry(boundary: dict):
    if not boundary:
        return None
    g = dict(boundary)
    if "coordinates" in g:
        g["coordinates"] = _strip_z_coords(g["coordinates"])
    try:
        return shape(g)
    except Exception:
        return None


def _geometry_geojson(geom) -> dict:
    return mapping(geom)


def _geometry_fingerprint(geom) -> str:
    try:
        return hashlib.sha256(json.dumps(mapping(geom), sort_keys=True).encode()).hexdigest()
    except Exception:
        return ""


def _hex_rgb(h: str) -> Tuple[int, int, int]:
    h = h.lstrip("#")[:6]
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _interpolate_palette(values: np.ndarray, palette: List[str], vmin: float, vmax: float) -> np.ndarray:
    colors = np.array([_hex_rgb(x) for x in palette], dtype=np.float32)
    if vmax <= vmin:
        idx = np.zeros(values.shape, dtype=np.int32)
        idx[...] = min(len(colors) - 1, 0)
        return colors[idx].astype(np.uint8)
    t = np.clip((values.astype(np.float32) - vmin) / (vmax - vmin), 0, 1)
    p = t * (len(colors) - 1)
    lo = np.floor(p).astype(np.int32)
    hi = np.minimum(lo + 1, len(colors) - 1)
    f = (p - lo)[..., None]
    rgb = colors[lo] * (1 - f) + colors[hi] * f
    return np.clip(rgb, 0, 255).astype(np.uint8)

# ---------------------------------------------------------------------------
# Plot loading from field_boundaries
# ---------------------------------------------------------------------------
_PLOT_SQL = """
SELECT id, field_id, village_id, village_name, sub_dist, district, state,
       area_ha, geom_geojson, created_at
FROM field_boundaries
WHERE id = :plot_id
"""


def _iso(value):
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _plot_from_row(row) -> Optional[dict]:
    raw = row["geom_geojson"]
    if not raw:
        return None
    geom_json = json.loads(raw) if isinstance(raw, str) else raw
    geom = _make_geometry(geom_json)
    if geom is None or geom.is_empty:
        return None
    plot_id = int(row["id"])
    field_id = int(row["field_id"]) if row["field_id"] is not None else None
    created = _iso(row["created_at"])
    area_ha = float(row["area_ha"]) if row["area_ha"] is not None else None
    address = {
        "village": row["village_name"],
        "taluka": row["sub_dist"],
        "district": row["district"],
        "state": row["state"],
        "country": "India",
        "pin_code": None,
    }
    props = {
        "django_id": plot_id,
        "fastapi_plot_id": str(plot_id),
        "field_id": field_id,
        "village_id": int(row["village_id"]) if row["village_id"] is not None else None,
        "gat_number": None,
        "plot_number": str(field_id) if field_id is not None else None,
        "updated_at": created,
        "django_updated_at": created,
        "area_ha": area_ha,
        "village": address["village"],
        "taluka": address["taluka"],
        "district": address["district"],
        "state": address["state"],
        "country": address["country"],
        "pin_code": address["pin_code"],
    }
    record = {
        "id": plot_id,
        "fastapi_plot_id": str(plot_id),
        "gat_number": None,
        "plot_number": props["plot_number"],
        "address": address,
        "location": {},
        "boundary": mapping(geom),
        "farmer": None,
        "farms": [],
        "farms_count": 0,
        "created_at": created,
        "updated_at": created,
    }
    return {
        "geometry": geom,
        "geom_type": geom.geom_type,
        "original_coords": mapping(geom)["coordinates"],
        "properties": props,
        "django_record": record,
    }


def _fetch_plot_row(plot_id: int):
    with engine.connect() as conn:
        return conn.execute(text(_PLOT_SQL), {"plot_id": plot_id}).mappings().first()


def _load_plot(plot_name: str) -> Optional[dict]:
    normalized = _normalize_plot_name(plot_name)
    if not normalized.isdigit():
        return None
    with _plot_lock:
        cached = _plot_cache.get(normalized)
    if cached is not None:
        return cached
    row = _fetch_plot_row(int(normalized))
    if row is None:
        return None
    plot = _plot_from_row(row)
    if plot is None:
        return None
    with _plot_lock:
        _plot_cache[normalized] = plot
    return plot


def _normalize_plot_name(plot_name: str) -> str:
    value = str(plot_name).strip()
    return value.replace("/", "_")


def _get_plot(plot_name: str):
    plot = _load_plot(plot_name)
    if plot is None:
        raise HTTPException(status_code=404, detail=f"Plot not found: {plot_name}")
    return plot


def _count_plots() -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("SELECT COUNT(*) FROM field_boundaries")).scalar() or 0)


def _list_plot_ids() -> List[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT id FROM field_boundaries ORDER BY id")).scalars().all()
    return [str(row) for row in rows]


# ---------------------------------------------------------------------------
# STAC access
# ---------------------------------------------------------------------------
def _stac_search(collection: str, geometry, start_date: str, end_date: str, limit: int = 50) -> List[dict]:
    cache_key = (collection, _geometry_fingerprint(geometry), start_date, end_date, limit)
    cached = _stac_item_cache.get(cache_key)
    if cached is not None:
        return cached
    payload = {
        "collections": [collection],
        "intersects": _geometry_geojson(geometry),
        "datetime": f"{start_date}T00:00:00Z/{end_date}T23:59:59Z",
        "limit": limit,
        "sortby": [{"field": "properties.datetime", "direction": "desc"}],
    }
    r = requests.post(
        f"{STAC_API_URL}/search",
        json=payload,
        headers=_stac_headers(),
        timeout=STAC_TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    items = data.get("features") or []
    _stac_item_cache[cache_key] = items
    return items


def _has_asset(item: dict, name: str) -> bool:
    return bool((item.get("assets") or {}).get(name, {}).get("href"))


def _item_date(item: dict) -> Optional[str]:
    return _date_only((item.get("properties") or {}).get("datetime") or item.get("properties", {}).get("start_datetime"))


def _item_dt(item: dict) -> Optional[datetime]:
    p = item.get("properties") or {}
    return _parse_dt(p.get("datetime") or p.get("start_datetime"))


def _s1_asset_name(item: dict, polarization: str) -> Optional[str]:
    """Resolve the existing Sentinel-1 VV/VH asset without changing analysis logic.

    Only the asset-name compatibility is handled here. The original VV/VH
    analysis is left untouched. Earth Search/STAC payloads can expose the
    logical asset as ``vv``/``vh`` while the href can end in names such as
    ``iw-vv.tiff``/``iw-vh.tiff``.
    """
    assets = item.get("assets") or {}
    pol = polarization.lower().strip()

    # Exact/common STAC keys first.
    preferred = (
        pol, f"iw-{pol}", f"iw_{pol}",
        f"measurement/{pol}", f"measurement/iw-{pol}",
        f"measurement/iw_{pol}",
    )
    for key in preferred:
        asset = assets.get(key)
        if isinstance(asset, dict) and asset.get("href"):
            return key

    # Then inspect both the STAC asset key and the actual S3 filename.
    # Do not require one exact naming convention.
    for key, asset in assets.items():
        if not isinstance(asset, dict) or not asset.get("href"):
            continue

        key_norm = str(key).strip().lower().replace("\\", "/").replace("_", "-")
        href_name = str(asset.get("href", "")).strip().lower().rsplit("/", 1)[-1]
        href_norm = href_name.replace("_", "-")

        # Accept only an actual VV/VH token, avoiding false matches such as
        # metadata/thumbnail assets.
        candidates = (key_norm, href_norm)
        for candidate in candidates:
            stem = candidate.rsplit("/", 1)[-1]
            stem_no_ext = stem.rsplit(".", 1)[0]
            if (
                stem_no_ext == pol
                or stem_no_ext.endswith(f"-{pol}")
                or stem_no_ext.endswith(f"_{pol}")
                or stem_no_ext.startswith(f"iw-{pol}")
                or stem_no_ext.startswith(f"iw_{pol}")
            ):
                return key

    return None


def _s1_iw_vv_vh(item: dict) -> bool:
    """Return True when the item has usable VV and VH measurement assets.

    The collection itself guarantees Sentinel-1 GRD. For this application the
    presence of the IW measurement hrefs is a more reliable operational check
    than requiring a particular SAR metadata shape. If an explicit mode is
    present and is not IW, reject it; otherwise accept the VV/VH item.
    """
    p = item.get("properties") or {}
    mode = str(p.get("sar:instrument_mode") or p.get("s1:instrument_mode") or "").upper().strip()
    vv_key = _s1_asset_name(item, "vv")
    vh_key = _s1_asset_name(item, "vh")
    if not vv_key or not vh_key:
        return False
    # Some STAC payloads spell/format the mode slightly differently. Only
    # reject an explicitly known non-IW mode; do not reject a valid VV/VH
    # item because of metadata formatting.
    if mode and "IW" not in mode:
        return False
    return True


def pick_shared_scene(geometry, start_date: str, end_date: str) -> Optional[dict]:
    """Replicate the shared S2-first scene selection used by growth/water/soil."""
    s2 = _stac_search(S2_COLLECTION, geometry, start_date, end_date, 50)
    s1 = _stac_search(S1_COLLECTION, geometry, start_date, end_date, 100)
    s2 = [x for x in s2 if _has_asset(x, "red") and _has_asset(x, "nir")]
    s1 = [x for x in s1 if _s1_iw_vv_vh(x)]
    latest_s2 = max(s2, key=lambda x: _item_dt(x) or datetime.min.replace(tzinfo=timezone.utc), default=None)
    latest_s1 = max(s1, key=lambda x: _item_dt(x) or datetime.min.replace(tzinfo=timezone.utc), default=None)
    if latest_s2 and (not latest_s1 or (_item_dt(latest_s2) or datetime.min.replace(tzinfo=timezone.utc)) >= (_item_dt(latest_s1) or datetime.min.replace(tzinfo=timezone.utc))):
        return {"sensor": "s2", "date": _item_date(latest_s2), "item": latest_s2}
    if latest_s1:
        return {"sensor": "s1", "date": _item_date(latest_s1), "item": latest_s1}
    return None


def _find_s2_item_on_date(geometry, target_date: str):
    items = _stac_search(S2_COLLECTION, geometry, target_date, target_date, 50)
    items = [x for x in items if _item_date(x) == target_date and _has_asset(x, "red") and _has_asset(x, "nir")]
    return min(items, key=lambda x: float((x.get("properties") or {}).get("eo:cloud_cover", 100))) if items else None


def _find_s1_items_on_date(geometry, target_date: str, require_iw: bool = True):
    items = _stac_search(S1_COLLECTION, geometry, target_date, target_date, 100)
    if require_iw:
        items = [x for x in items if _s1_iw_vv_vh(x)]
    else:
        items = [x for x in items if _has_asset(x, "vv") or _has_asset(x, "vh")]
    items = [x for x in items if _item_date(x) == target_date]
    items.sort(key=lambda x: _item_dt(x) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return items

# ---------------------------------------------------------------------------
# Raster reading helpers
# ---------------------------------------------------------------------------
def _asset_href(item: dict, name: str) -> str:
    assets = item.get("assets") or {}
    asset = assets.get(name)
    if not asset and name in ("vv", "vh"):
        key = _s1_asset_name(item, name)
        asset = assets.get(key) if key else None
    if not asset or not asset.get("href"):
        raise HTTPException(status_code=404, detail=f"STAC asset '{name}' unavailable")
    return asset["href"]


@contextmanager
def _open_remote_dataset(href: str):
    """Open an Earth Search COG. Classwise reads stay unsigned."""
    if not _force_unsigned.get():
        href = _signed_cog_href(href)
    env = rasterio.Env(**_gdal_env_options())
    try:
        env.__enter__()
        with rasterio.open(href) as src:
            yield src
    finally:
        env.__exit__(None, None, None)


def _stac_projection(item: Optional[dict]):
    """Return CRS and affine transform from STAC projection metadata."""
    props = (item or {}).get("properties") or {}
    epsg = props.get("proj:epsg")
    wkt = props.get("proj:wkt2") or props.get("proj:wkt")
    crs = None
    if epsg:
        try:
            crs = CRS.from_epsg(int(epsg))
        except Exception:
            pass
    if crs is None and wkt:
        try:
            crs = CRS.from_wkt(str(wkt))
        except Exception:
            pass

    transform = props.get("proj:transform")
    affine = None
    if isinstance(transform, (list, tuple)) and len(transform) == 6:
        try:
            affine = Affine(*[float(x) for x in transform])
        except Exception:
            pass

    # Earth Search Sentinel-1 items normally publish proj:epsg/proj:transform.
    # If transform is absent, derive it from proj:bbox + proj:shape.
    if affine is None:
        bbox = props.get("proj:bbox") or (item or {}).get("bbox")
        shp = props.get("proj:shape")
        if bbox and shp and len(bbox) >= 4 and len(shp) >= 2:
            try:
                left, bottom, right, top = map(float, bbox[:4])
                height, width = int(shp[0]), int(shp[1])
                if width > 0 and height > 0:
                    affine = from_bounds(left, bottom, right, top, width, height)
            except Exception:
                pass
    return crs, affine


def _read_asset_on_grid(
    href: str,
    geometry,
    target_crs="EPSG:4326",
    resolution: float = 0.00009,
    stac_item: Optional[dict] = None,
):
    """Read an AOI from an Earth Search COG onto a stable WGS84 grid.

    Sentinel-1 Earth Search COGs can have no CRS embedded in the TIFF even
    though the STAC Item contains the authoritative projection metadata.
    In that case we use proj:epsg + proj:transform (or proj:bbox + proj:shape)
    from the STAC Item for both CRS and georeferencing.
    """
    stac_crs, stac_transform = _stac_projection(stac_item)

    # If STAC supplies a projected transform but its CRS field is missing or
    # incorrectly reported as geographic, infer the local UTM CRS from the plot
    # centroid. This is only georeferencing repair; analysis thresholds/formulas
    # are untouched.
    if stac_crs is None or (stac_crs and stac_crs.to_epsg() == 4326):
        if stac_transform is not None and stac_transform != Affine.identity():
            try:
                stac_pixel_size = max(abs(float(stac_transform.a)), abs(float(stac_transform.e)))
                if stac_pixel_size > 1.0:
                    lon0, lat0 = geometry.centroid.x, geometry.centroid.y
                    zone = int(math.floor((lon0 + 180.0) / 6.0) + 1)
                    zone = max(1, min(60, zone))
                    epsg = 32600 + zone if lat0 >= 0 else 32700 + zone
                    stac_crs = CRS.from_epsg(epsg)
                    logger.info("Inferred S1 STAC CRS EPSG:%s from projected transform for %s", epsg, href)
            except Exception as exc:
                logger.debug("Could not infer S1 STAC CRS from transform: %s", exc)

    with _open_remote_dataset(href) as src:
        embedded_crs = src.crs
        src_crs = embedded_crs
        src_transform = src.transform

        # Some Sentinel-1 COGs omit the CRS but retain a valid projected
        # affine transform.  Do not pair a metre-based transform with EPSG:4326
        # (that causes rasterio WindowError).  Infer the local UTM CRS only for
        # this missing-CRS case; all existing analysis formulas remain unchanged.
        if src_crs is None and src_transform is not None and src_transform != Affine.identity():
            try:
                pixel_size = max(abs(float(src_transform.a)), abs(float(src_transform.e)))
                if pixel_size > 1.0:
                    lon0, lat0 = geometry.centroid.x, geometry.centroid.y
                    zone = int(math.floor((lon0 + 180.0) / 6.0) + 1)
                    zone = max(1, min(60, zone))
                    epsg = 32600 + zone if lat0 >= 0 else 32700 + zone
                    src_crs = CRS.from_epsg(epsg)
                    logger.info("Inferred S1 CRS EPSG:%s from projected COG transform for %s", epsg, href)
            except Exception as exc:
                logger.debug("Could not infer S1 CRS from transform: %s", exc)

        # IMPORTANT: for Sentinel-1, the TIFF may omit CRS/geotransform while
        # STAC still provides the correct projection metadata. Prefer STAC
        # metadata whenever the embedded metadata is incomplete.
        if src_crs is None and stac_crs is not None:
            src_crs = stac_crs
            logger.info("Using STAC CRS %s for %s", src_crs, href)

        if stac_transform is not None and (
                src_transform is None or src_transform == Affine.identity()):
            src_transform = stac_transform
            logger.info("Using STAC transform for %s", href)

        # Some Earth Search S1 assets have no embedded CRS. The STAC item is
        # authoritative; if projection fields are absent, the Sentinel-1 GRD
        # archive is geographic WGS84 for these public COG measurement assets.
        if src_crs is None:
            src_crs = stac_crs or CRS.from_epsg(4326)
            logger.warning("Using WGS84 fallback CRS for S1 asset: %s", href)

        if src_transform is None or src_transform == Affine.identity():
            if stac_transform is not None:
                src_transform = stac_transform
            else:
                # Last-resort georeferencing from STAC bbox + raster dimensions.
                bbox = ((stac_item or {}).get("bbox") or [])
                if len(bbox) >= 4:
                    src_transform = from_bounds(
                        float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]),
                        src.width, src.height
                    )
                else:
                    raise RuntimeError(f"Asset has no usable geotransform: {href}")

        bounds = geometry.bounds
        left, bottom, right, top = map(float, bounds)
        pad = resolution * 2
        left -= pad
        right += pad
        bottom -= pad
        top += pad

        # Transform the AOI bounds into the actual raster CRS before creating
        # a raster window. This is essential for projected rasters and avoids
        # WindowError caused by mixing WGS84 bounds with raster coordinates.
        sb = transform_bounds(
            target_crs,
            src_crs,
            left, bottom, right, top,
            densify_pts=21,
        )
        sleft, sbottom, sright, stop = sb

        # Intersect the requested window with the actual raster extent.
        raster_left, raster_bottom, raster_right, raster_top = src.bounds
        if stac_transform is not None and (embedded_crs is None or src_transform != src.transform):
            # Recompute bounds from the authoritative STAC transform.
            raster_left, raster_bottom, raster_right, raster_top = rasterio.transform.array_bounds(
                src.height, src.width, src_transform
            )

        sleft = max(sleft, min(raster_left, raster_right))
        sright = min(sright, max(raster_left, raster_right))
        sbottom = max(sbottom, min(raster_bottom, raster_top))
        stop = min(stop, max(raster_bottom, raster_top))

        if not (sleft < sright and sbottom < stop):
            raise RuntimeError(
                f"AOI does not overlap raster: aoi={sb}, raster={(raster_left, raster_bottom, raster_right, raster_top)}"
            )

        try:
            win = rasterio.windows.from_bounds(
                sleft, sbottom, sright, stop, transform=src_transform
            ).round_offsets().round_lengths()
        except rasterio.errors.WindowError:
            # Fallback: calculate the pixel window directly from the inverse
            # affine. This handles valid STAC transforms that are not identical
            # to the incomplete transform reported by the TIFF driver.
            inv = ~src_transform
            cols = [inv * (sleft, sbottom), inv * (sleft, stop),
                    inv * (sright, sbottom), inv * (sright, stop)]
            xs = [p[0] for p in cols]
            ys = [p[1] for p in cols]
            col0, col1 = math.floor(min(xs)), math.ceil(max(xs))
            row0, row1 = math.floor(min(ys)), math.ceil(max(ys))
            win = rasterio.windows.Window(
                col0, row0, max(1, col1 - col0), max(1, row1 - row0)
            )

        # Clamp window to dataset dimensions.
        win = win.intersection(rasterio.windows.Window(0, 0, src.width, src.height))
        if win.width <= 0 or win.height <= 0:
            raise RuntimeError(f"Computed raster window is empty: {href}")

        arr = src.read(1, window=win, masked=True)
        transform = rasterio.windows.transform(win, src_transform)
        arr = np.ma.asarray(arr, dtype=np.float32)
        source = arr.filled(np.nan)

        width = max(1, int(math.ceil((right - left) / resolution)))
        height = max(1, int(math.ceil((top - bottom) / resolution)))
        dst_transform = from_bounds(left, bottom, right, top, width, height)
        dst = np.full((height, width), np.nan, dtype=np.float32)

        reproject(
            source=source,
            destination=dst,
            src_transform=transform,
            src_crs=src_crs,
            dst_transform=dst_transform,
            dst_crs=target_crs,
            src_nodata=np.nan,
            dst_nodata=np.nan,
            resampling=Resampling.bilinear,
        )
        return dst, dst_transform, target_crs


def _read_s2_band(item: dict, asset_name: str, geometry, resolution: float = 0.00009):
    return _read_asset_on_grid(_asset_href(item, asset_name), geometry, resolution=resolution, stac_item=item)


def _read_s1_band(item: dict, asset_name: str, geometry, resolution: float = 0.00009):
    """Read Earth Search Sentinel-1 GRD amplitude COG.

    Earth Search documents these measurement assets as uint16 amplitude data,
    not GEE-ready dB sigma0. We therefore convert amplitude to a provisional
    dB intensity representation (20*log10(amplitude)). This keeps the endpoint
    operational, but exact GEE-equivalence for Sentinel-1 requires radiometric
    calibration using the per-scene calibration/noise annotation assets.
    """
    item_id = str(item.get("id") or _asset_href(item, asset_name))
    cache_key = (item_id, asset_name.lower(), _geometry_fingerprint(geometry), float(resolution))
    with _s1_band_lock:
        cached = _s1_band_cache.get(cache_key)
    if cached is not None:
        arr, transform, crs = cached
        return arr.copy(), transform, crs

    arr, transform, crs = _read_asset_on_grid(
        _asset_href(item, asset_name),
        geometry,
        resolution=resolution,
        stac_item=item,
    )
    arr = np.where(np.isfinite(arr) & (arr > 0), 20.0 * np.log10(np.maximum(arr, 1e-6)), np.nan)
    with _s1_band_lock:
        _s1_band_cache[cache_key] = (arr.copy(), transform, crs)
    return arr, transform, crs


def _apply_s2_scale(arr: np.ndarray, item: dict, asset_name: str) -> np.ndarray:
    """Honor Earth Search raster scale/offset metadata when supplied."""
    asset = (item.get("assets") or {}).get(asset_name) or {}
    rb = (asset.get("raster:bands") or [{}])[0]
    scale = float(rb.get("scale", 1.0))
    offset = float(rb.get("offset", 0.0))
    if scale != 1.0 or offset != 0.0:
        return arr * scale + offset
    return arr


def _clip_invalid(arrays: List[np.ndarray]) -> np.ndarray:
    valid = np.ones(arrays[0].shape, dtype=bool)
    for a in arrays:
        valid &= np.isfinite(a)
    return valid


def _smooth_classified(classes: np.ndarray, radius_pixels: int) -> np.ndarray:
    """Approximate EE focal_mean with a square local mean kernel."""
    if radius_pixels <= 0:
        return classes.astype(np.float32)
    from numpy.lib.stride_tricks import sliding_window_view
    pad = radius_pixels
    padded = np.pad(classes.astype(np.float32), pad, mode="edge")
    window = sliding_window_view(padded, (2 * pad + 1, 2 * pad + 1))
    return np.nanmean(window, axis=(-2, -1)).astype(np.float32)


def _sample_coordinates(mask: np.ndarray, transform, max_points: int = MAX_SAMPLE_COORDS) -> List[List[float]]:
    ys, xs = np.where(mask)
    if len(xs) == 0:
        return []
    if len(xs) > max_points:
        # Deterministic evenly spaced sampling, matching the original purpose of
        # numPixels=2000 without generating a huge response.
        idx = np.linspace(0, len(xs) - 1, max_points).astype(int)
        xs, ys = xs[idx], ys[idx]
    coords = []
    for x, y in zip(xs, ys):
        lon, lat = rasterio.transform.xy(transform, int(y), int(x), offset="center")
        coords.append([float(lon), float(lat)])
    # Preserve the original de-duplication behavior.
    seen = set()
    unique = []
    for c in coords:
        t = (round(c[0], 8), round(c[1], 8))
        if t not in seen:
            seen.add(t)
            unique.append(c)
    return unique


def _mask_to_plot(mask: np.ndarray, transform, geometry) -> np.ndarray:
    return mask & geometry_mask([mapping(geometry)], transform=transform, invert=True, out_shape=mask.shape)

# ---------------------------------------------------------------------------
# Local tile store - replaces EE getMapId()
# ---------------------------------------------------------------------------
def _register_tile_raster(
    array: np.ndarray,
    transform,
    crs: str,
    palette: List[str],
    vmin: float,
    vmax: float,
    geometry
) -> str:

    key = hashlib.sha256(
        f"{time.time_ns()}-{np.random.random()}".encode()
    ).hexdigest()[:24]

    path = os.path.join(
        tempfile.gettempdir(),
        f"cropeye_aws_{key}.tif"
    )

    # Copy the array so the original analysis array is never modified.
    data = np.asarray(
        array,
        dtype=np.float32
    ).copy()

    # ---------------------------------------------------------------
    # IMPORTANT:
    # Everything outside the actual plot must be NaN.
    # Otherwise class 0 can be rendered as a visible color.
    # ---------------------------------------------------------------

    try:
        inside = geometry_mask(
            [mapping(geometry)],
            transform=transform,
            invert=True,
            out_shape=data.shape
        )

        data[~inside] = np.nan

    except Exception as exc:
        logger.warning(
            "Could not apply geometry mask to tile raster: %s",
            exc
        )

    profile = {
        "driver": "GTiff",
        "height": data.shape[0],
        "width": data.shape[1],
        "count": 1,
        "dtype": "float32",
        "crs": crs,
        "transform": transform,
        "nodata": np.nan,

        # Internal tiling for faster reads.
        "tiled": True,

        # Compression without changing values.
        "compress": "deflate",

        "BIGTIFF": "IF_SAFER",
    }

    with rasterio.open(
        path,
        "w",
        **profile
    ) as dst:

        dst.write(
            data,
            1
        )

    meta = {
        "palette": palette,
        "vmin": float(vmin),
        "vmax": float(vmax),
        "geometry": mapping(geometry),
    }

    with _tile_lock:

        _tile_cache[key] = (
            time.time(),
            path,
            meta
        )

        _tile_cache.move_to_end(key)

        while len(_tile_cache) > TILE_CACHE_MAX:

            _, (
                _,
                old_path,
                _
            ) = _tile_cache.popitem(
                last=False
            )

            try:
                os.remove(old_path)
            except OSError:
                pass

    return key


def _get_tile_entry(key: str):
    with _tile_lock:
        item = _tile_cache.get(key)
        if item is None:
            return None
        if time.time() - item[0] > TILE_CACHE_TTL:
            _tile_cache.pop(key, None)
            try:
                os.remove(item[1])
            except OSError:
                pass
            prefix = f"{key}:"
            with _png_tile_lock:
                for cache_key in list(_png_tile_cache.keys()):
                    if cache_key.startswith(prefix):
                        _png_tile_cache.pop(cache_key, None)
            return None
        _tile_cache.move_to_end(key)
        return item


def _xyz_bounds(x: int, y: int, z: int):
    n = 2 ** z
    lon1 = x / n * 360.0 - 180.0
    lon2 = (x + 1) / n * 360.0 - 180.0
    def lat(yy):
        v = math.pi * (1 - 2 * yy / n)
        return math.degrees(math.atan(math.sinh(v)))
    return lon1, lat(y + 1), lon2, lat(y)


def _smooth_display_raster(array: np.ndarray, radius_pixels: int = 2) -> np.ndarray:
    """
    Smooth visualization raster without creating dark pixel/cell borders.

    Important:
    - Only visualization is changed.
    - Original analysis/classification values remain unchanged.
    - NaN/no-data pixels are excluded from the smoothing calculation.
    - Uses weighted Gaussian-like smoothing.
    """
    arr = np.asarray(array, dtype=np.float32)

    if arr.ndim != 2:
        raise ValueError("Expected a 2D raster")

    if radius_pixels <= 0:
        return arr.copy()

    valid = np.isfinite(arr)

    if not np.any(valid):
        return np.full_like(arr, np.nan, dtype=np.float32)

    r = int(radius_pixels)

    # Gaussian-like kernel
    coords = np.arange(-r, r + 1, dtype=np.float32)

    sigma = max(float(r) / 1.5, 0.75)

    weights = np.exp(
        -(coords ** 2) / (2.0 * sigma ** 2)
    )

    weights /= np.sum(weights)

    kernel = np.outer(weights, weights).astype(np.float32)

    # Never allow NaN to participate in the smoothing.
    values = np.where(valid, arr, 0.0).astype(np.float32)
    validity = valid.astype(np.float32)

    # Reflect padding prevents dark/artificial borders at raster edges.
    padded_values = np.pad(
        values,
        r,
        mode="reflect"
    )

    padded_valid = np.pad(
        validity,
        r,
        mode="reflect"
    )

    from numpy.lib.stride_tricks import sliding_window_view

    value_windows = sliding_window_view(
        padded_values,
        (2 * r + 1, 2 * r + 1)
    )

    valid_windows = sliding_window_view(
        padded_valid,
        (2 * r + 1, 2 * r + 1)
    )

    weighted_values = value_windows * kernel
    weighted_valid = valid_windows * kernel

    value_sum = np.sum(
        weighted_values,
        axis=(-2, -1)
    )

    weight_sum = np.sum(
        weighted_valid,
        axis=(-2, -1)
    )

    result = np.full(
        arr.shape,
        np.nan,
        dtype=np.float32
    )

    np.divide(
        value_sum,
        weight_sum,
        out=result,
        where=weight_sum > 1e-8
    )

    # Original no-data remains no-data.
    result[~valid] = np.nan

    return result


def _fill_visual_nodata(array: np.ndarray, valid_mask: np.ndarray, max_search_distance: int = 64) -> np.ndarray:
    """
    Fill visualization-only gaps that occur inside the plot polygon.

    This does NOT modify the analysis raster stored in the tile cache.
    It only prevents small/large no-data gaps inside the requested plot
    from becoming transparent holes in the rendered smooth tile.

    Existing valid satellite values are preserved. Missing pixels are
    filled from the nearest valid visualization pixel, after which the
    normal smoothing is applied.
    """
    arr = np.asarray(array, dtype=np.float32).copy()
    inside = np.asarray(valid_mask, dtype=bool)

    if arr.ndim != 2 or inside.shape != arr.shape:
        return arr

    missing_inside = inside & ~np.isfinite(arr)
    if not np.any(missing_inside):
        return arr

    source_valid = np.isfinite(arr) & inside
    if not np.any(source_valid):
        return arr

    try:
        # rasterio's fillnodata performs nearest-valid-value filling and
        # avoids introducing an artificial dark/black value into gaps.
        from rasterio.fill import fillnodata

        filled = fillnodata(
            arr,
            mask=source_valid.astype(np.uint8),
            max_search_distance=max_search_distance,
            smoothing_iterations=0,
        ).astype(np.float32)

        # Only fill pixels that are inside the plot. Outside remains NaN.
        arr[missing_inside] = filled[missing_inside]
    except Exception as exc:
        logger.warning(
            "Could not fill visualization nodata gaps: %s",
            exc
        )

    # fillnodata can leave large holes when a COG footprint is sparse.
    # For visualization only, use an exact nearest-valid-pixel fallback so the
    # plot is fully covered. This never modifies the analysis arrays/counts.
    missing_inside = inside & ~np.isfinite(arr)
    if np.any(missing_inside):
        try:
            from scipy.ndimage import distance_transform_edt
            source_valid = np.isfinite(arr) & inside
            if np.any(source_valid):
                _, indices = distance_transform_edt(~source_valid, return_distances=True, return_indices=True)
                nearest = arr[tuple(indices)]
                arr[missing_inside] = nearest[missing_inside]
        except Exception as exc:
            logger.warning("Nearest visualization fill unavailable: %s", exc)

    return arr


def _render_tile(
    path: str,
    x: int,
    y: int,
    z: int,
    palette: List[str],
    vmin: float,
    vmax: float,
    geometry_geojson: dict
) -> bytes:
    """Render a smooth visualization tile without changing analysis data.

    The stored raster remains unchanged. Smoothing/interpolation is applied
    only while creating the PNG so neighboring colors blend instead of showing
    hard square cells.
    """
    TILE_SIZE = 256
    SCALE = 1
    OUT_SIZE = TILE_SIZE * SCALE
    bounds = _xyz_bounds(x, y, z)
    dst_crs = "EPSG:3857"
    minx, miny, maxx, maxy = transform_bounds("EPSG:4326", dst_crs, *bounds)
    dst_transform = from_bounds(minx, miny, maxx, maxy, OUT_SIZE, OUT_SIZE)

    tile_geometry = transform_geom("EPSG:4326", dst_crs, geometry_geojson)
    inside = geometry_mask([tile_geometry], transform=dst_transform, invert=True, out_shape=(OUT_SIZE, OUT_SIZE))
    rendered = np.full((OUT_SIZE, OUT_SIZE), np.nan, dtype=np.float32)

    with rasterio.open(path) as src:
        reproject(
            source=rasterio.band(src, 1),
            destination=rendered,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
            src_nodata=np.nan,
            dst_nodata=np.nan,
            # Visualization only: bilinear blending removes hard 10 m
            # square edges in the displayed tile. Stored analysis values are
            # never modified.
            resampling=Resampling.bilinear,
            init_dest_nodata=np.nan,
        )

    # Never let outside-polygon values participate in the fill.
    rendered[~inside] = np.nan

    # Fill all visualization gaps inside the plot from the nearest valid class.
    # This changes only the PNG, never the stored analysis/counts.
    missing = inside & ~np.isfinite(rendered)
    if np.any(missing):
        try:
            from rasterio.fill import fillnodata
            source_valid = (np.isfinite(rendered) & inside).astype(np.uint8)
            if np.any(source_valid):
                filled = fillnodata(rendered, mask=source_valid, max_search_distance=min(128, OUT_SIZE), smoothing_iterations=0)
                rendered[missing] = filled[missing]
        except Exception as exc:
            logger.warning("Tile nodata fill failed: %s", exc)

    # Final nearest-valid fallback for visualization only. Use a distance
    # transform when SciPy is available; this avoids the very slow O(N^2)
    # Python/chunked search that can make deployed tile requests stall.
    missing = inside & ~np.isfinite(rendered)
    if np.any(missing):
        source_valid = np.isfinite(rendered) & inside
        if np.any(source_valid):
            try:
                from scipy.ndimage import distance_transform_edt
                _, indices = distance_transform_edt(
                    ~source_valid,
                    return_distances=True,
                    return_indices=True,
                )
                nearest = rendered[tuple(indices)]
                rendered[missing] = nearest[missing]
            except Exception as exc:
                logger.warning("Fast nearest visualization fill unavailable: %s", exc)
                # Keep a small, bounded fallback rather than performing a
                # quadratic search over the whole tile.
                try:
                    from rasterio.fill import fillnodata
                    filled = fillnodata(
                        rendered,
                        mask=source_valid.astype(np.uint8),
                        max_search_distance=OUT_SIZE * 8,
                        smoothing_iterations=0,
                    )
                    rendered[missing] = filled[missing]
                except Exception as fill_exc:
                    logger.warning("Final tile nodata fill failed: %s", fill_exc)

    # Convert class IDs directly to palette colors. No smoothing/interpolation.
    rgba = np.zeros((OUT_SIZE, OUT_SIZE, 4), dtype=np.uint8)
    valid = inside & np.isfinite(rendered)
    if np.any(valid):
        rgb = _interpolate_palette(rendered[valid], palette, vmin, vmax)
        rgba[valid, :3] = rgb
        rgba[valid, 3] = 255

    # Visualization only: smooth downsampling for blended color transitions.
    image = Image.fromarray(rgba, mode="RGBA").resize(
        (TILE_SIZE, TILE_SIZE),
        Image.Resampling.LANCZOS
    )
    final = np.asarray(image, dtype=np.uint8).copy()

    # Reapply exact polygon mask at final resolution.
    final_transform = from_bounds(minx, miny, maxx, maxy, TILE_SIZE, TILE_SIZE)
    final_inside = geometry_mask([tile_geometry], transform=final_transform, invert=True, out_shape=(TILE_SIZE, TILE_SIZE))
    final[~final_inside, 3] = 0
    transparent = final[:, :, 3] == 0
    final[transparent, :3] = 0

    buf = io.BytesIO()
    Image.fromarray(final, mode="RGBA").save(buf, format="PNG", optimize=False, compress_level=2)
    return buf.getvalue()

def _tile_url(key: str) -> str:
    # Relative URL is intentionally avoided so frontend code can use it directly.
    # Set PUBLIC_BASE_URL in Railway/Render for a stable absolute URL.
    base = (settings.public_base_url or os.getenv("PUBLIC_BASE_URL") or "").rstrip("/")
    if not base:
        # Relative XYZ URL is valid when frontend and API share an origin.
        return f"/analysis-tiles/{key}/{{z}}/{{x}}/{{y}}.png"
    return f"{base}/analysis-tiles/{key}/{{z}}/{{x}}/{{y}}.png"


def _get_png_tile(key: str, x: int, y: int, z: int) -> Optional[bytes]:
    cache_key = f"{key}:{z}:{x}:{y}"
    with _png_tile_lock:
        item = _png_tile_cache.get(cache_key)
        if item is None:
            return None
        created, data = item
        if time.time() - created > TILE_CACHE_TTL:
            _png_tile_cache.pop(cache_key, None)
            return None
        _png_tile_cache.move_to_end(cache_key)
        return data


def _put_png_tile(key: str, x: int, y: int, z: int, data: bytes) -> None:
    cache_key = f"{key}:{z}:{x}:{y}"
    with _png_tile_lock:
        _png_tile_cache[cache_key] = (time.time(), data)
        _png_tile_cache.move_to_end(cache_key)
        while len(_png_tile_cache) > PNG_TILE_CACHE_MAX:
            _png_tile_cache.popitem(last=False)


@router.get("/analysis-tiles/{key}/{z}/{x}/{y}.png")
async def analysis_tile(key: str, z: int, x: int, y: int):
    entry = _get_tile_entry(key)
    if entry is None:
        raise HTTPException(status_code=404, detail="Tile cache expired or not found")

    if z < 0 or z > 24:
        raise HTTPException(status_code=400, detail="Invalid zoom level")
    n = 2 ** z
    if x < 0 or x >= n or y < 0 or y >= n:
        raise HTTPException(status_code=400, detail="Invalid tile coordinates")

    cached = _get_png_tile(key, x, y, z)
    if cached is not None:
        return Response(
            content=cached,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=300, immutable"},
        )

    _, path, meta = entry
    geometry_geojson = meta["geometry"]
    try:
        data = await asyncio.to_thread(
            _render_tile,
            path, x, y, z,
            meta["palette"],
            meta["vmin"],
            meta["vmax"],
            geometry_geojson,
        )
    except Exception as exc:
        logger.exception("Tile rendering failed for %s/%s/%s/%s", key, z, x, y)
        raise HTTPException(status_code=500, detail=f"Tile rendering failed: {exc}") from exc

    _put_png_tile(key, x, y, z, data)
    return Response(
        content=data,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=300, immutable"},
    )


def _make_thumb(
    key: str,
    geometry,
    max_dim: int = 768
) -> bytes:

    entry = _get_tile_entry(key)

    if entry is None:
        raise RuntimeError("Tile cache expired")

    _, path, meta = entry

    minx, miny, maxx, maxy = geometry.bounds

    aspect = max(
        (maxx - minx) / max(maxy - miny, 1e-12),
        1e-6
    )

    if aspect >= 1:
        width = max_dim
        height = max(
            1,
            round(max_dim / aspect)
        )
    else:
        width = max(
            1,
            round(max_dim * aspect)
        )
        height = max_dim

    # Render larger first.
    scale = 2

    high_width = width * scale
    high_height = height * scale

    dst_transform = from_bounds(
        minx,
        miny,
        maxx,
        maxy,
        high_width,
        high_height
    )

    dst = np.full(
        (high_height, high_width),
        np.nan,
        dtype=np.float32
    )

    with rasterio.open(path) as src:

        reproject(
            source=rasterio.band(src, 1),
            destination=dst,

            src_transform=src.transform,
            src_crs=src.crs,

            dst_transform=dst_transform,
            dst_crs="EPSG:4326",

            src_nodata=np.nan,
            dst_nodata=np.nan,

            resampling=Resampling.bilinear,

            init_dest_nodata=np.nan,
        )

    # Build exact thumbnail plot mask first.
    try:
        thumb_inside = geometry_mask(
            [mapping(geometry)],
            transform=dst_transform,
            invert=True,
            out_shape=dst.shape
        )
    except Exception:
        thumb_inside = np.ones(dst.shape, dtype=bool)

    # Fill visualization-only no-data gaps inside the plot before
    # smoothing, so the thumbnail is also fully covered.
    dst = _fill_visual_nodata(
        dst,
        thumb_inside,
        max_search_distance=max(high_width, high_height, 64)
    )
    dst[~thumb_inside] = np.nan

    # Smooth visualization only.
    dst = _smooth_display_raster(
        dst,
        radius_pixels=3
    )
    dst[~thumb_inside] = np.nan

    valid = np.isfinite(dst)

    rgba = np.zeros(
        (high_height, high_width, 4),
        dtype=np.uint8
    )

    if np.any(valid):

        rgba[valid, :3] = _interpolate_palette(
            dst[valid],
            meta["palette"],
            meta["vmin"],
            meta["vmax"]
        )

        rgba[valid, 3] = 255

    image_high = Image.fromarray(
        rgba,
        mode="RGBA"
    )

    image_final = image_high.resize(
        (width, height),
        resample=Image.Resampling.LANCZOS
    )

    final_array = np.asarray(
        image_final,
        dtype=np.uint8
    ).copy()

    alpha = final_array[:, :, 3]

    alpha[alpha < 20] = 0

    # Final exact geometry mask after LANCZOS downsampling.
    try:
        thumb_inside_final = geometry_mask(
            [mapping(geometry)],
            transform=from_bounds(
                minx,
                miny,
                maxx,
                maxy,
                width,
                height
            ),
            invert=True,
            out_shape=(height, width)
        )
        alpha[~thumb_inside_final] = 0
    except Exception as exc:
        logger.warning(
            "Could not apply final plot mask to thumbnail: %s",
            exc
        )

    transparent = alpha == 0

    final_array[transparent, :3] = 0
    final_array[:, :, 3] = alpha

    buffer = io.BytesIO()

    Image.fromarray(
        final_array,
        mode="RGBA"
    ).save(
        buffer,
        format="PNG",
        optimize=True
    )

    return buffer.getvalue()

@router.get("/analysis-thumbs/{key}.png")
async def analysis_thumb(key: str):
    entry = _get_tile_entry(key)
    if entry is None:
        raise HTTPException(status_code=404, detail="Thumbnail cache expired or not found")
    geometry = shape(entry[2]["geometry"])
    try:
        data = await asyncio.to_thread(_make_thumb, key, geometry)
        return Response(content=data, media_type="image/png", headers={"Cache-Control": "public, max-age=300"})
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Thumbnail rendering failed: {exc}") from exc

# ---------------------------------------------------------------------------
# Pixel counting / common output
# ---------------------------------------------------------------------------
def _counts(classes: np.ndarray, ids: List[int], plot_mask: np.ndarray) -> Dict[int, int]:
    out = {}
    valid = plot_mask & np.isfinite(classes)
    for i in ids:
        out[i] = int(np.count_nonzero(valid & (classes == i)))
    return out


def _common_feature(plot_name: str, plot: dict, properties: dict) -> dict:
    return {
        "type": "Feature",
        "geometry": mapping(plot["geometry"]),
        "properties": properties,
    }

# ---------------------------------------------------------------------------
# Growth - thresholds exactly copied from admintest.py
# ---------------------------------------------------------------------------
def _compute_analyze_growth(plot_name: str, start_date: str, end_date: str) -> Dict[str, Any]:
    plot = _get_plot(plot_name)
    geometry = plot["geometry"]
    scene = pick_shared_scene(geometry, start_date, end_date)
    if not scene:
        raise HTTPException(status_code=404, detail="No Sentinel-1 or Sentinel-2 images found for the given date range.")
    latest_image_date = scene["date"]
    use_s2 = scene["sensor"] == "s2"

    if use_s2:
        item = scene["item"]
        red, transform, crs = _read_s2_band(item, "red", geometry)
        nir, _, _ = _read_s2_band(item, "nir", geometry)
        red = _apply_s2_scale(red, item, "red")
        nir = _apply_s2_scale(nir, item, "nir")
        ndvi = (nir - red) / (nir + red + 1e-12)
        data_source = "Sentinel-2 NDVI"
        weak_mask = ndvi < 0.1
        stress_mask = (ndvi >= 0.1) & (ndvi < 0.2)
        moderate_mask = (ndvi >= 0.2) & (ndvi < 0.4)
        healthy_mask = ndvi >= 0.4
    else:
        items = _find_s1_items_on_date(geometry, latest_image_date)
        if not items:
            raise HTTPException(status_code=404, detail=f"S1 image missing on {latest_image_date}")
        item = items[0]
        vh, transform, crs = _read_s1_band(item, "vh", geometry)
        weak_mask = vh >= -11
        stress_mask = (vh < -11) & (vh > -13)
        moderate_mask = (vh <= -13) & (vh > -15)
        healthy_mask = vh <= -15
        data_source = "Sentinel-1 VH"

    plot_mask = _mask_to_plot(np.ones(ndvi.shape if use_s2 else vh.shape, dtype=bool), transform, geometry)
    classes = np.zeros(plot_mask.shape, dtype=np.float32)
    classes[weak_mask & plot_mask] = 1
    classes[stress_mask & plot_mask] = 2
    classes[moderate_mask & plot_mask] = 3
    classes[healthy_mask & plot_mask] = 4
       # Keep ORIGINAL classes for analysis/counting.
    # Do NOT use the smoothed class raster as the analysis raster.
    tile_key = _register_tile_raster(
        classes,
        transform,
        crs,
        GROWTH_PALETTE,
        0,
        4,
        geometry,
    )

    counts = _counts(classes, [1, 2, 3, 4], plot_mask)
    total = sum(counts.values())
    props = {
        "plot_name": plot_name,
        "django_id": (plot["properties"] or {}).get("django_id"),
        "fastapi_plot_id": (plot["properties"] or {}).get("fastapi_plot_id") or plot_name,
        "area_acres": round(float(geometry.area) * 0.0 + _area_acres(geometry), 2),
        "start_date": start_date,
        "end_date": end_date,
        "image_count": 1,
        "tile_url": _tile_url(tile_key),
        "thumb_url": f"{(settings.public_base_url or os.getenv('PUBLIC_BASE_URL') or '').rstrip('/')}/analysis-thumbs/{tile_key}.png",
        "data_source": data_source,
        "latest_image_date": latest_image_date,
        "last_updated": datetime.now().isoformat(),
    }
    
    return {
        "type": "FeatureCollection",
        "features": [_common_feature(plot_name, plot, props)],
        "pixel_summary": {
            "total_pixel_count": total,
            "healthy_pixel_count": counts[4],
            "healthy_pixel_percentage": (counts[4] / total) * 100 if total else 0,
            "healthy_pixel_coordinates": _sample_coordinates(healthy_mask & plot_mask, transform),
            "moderate_pixel_count": counts[3],
            "moderate_pixel_percentage": (counts[3] / total) * 100 if total else 0,
            "moderate_pixel_coordinates": _sample_coordinates(moderate_mask & plot_mask, transform),
            "weak_pixel_count": counts[1],
            "weak_pixel_percentage": (counts[1] / total) * 100 if total else 0,
            "weak_pixel_coordinates": _sample_coordinates(weak_mask & plot_mask, transform),
            "stress_pixel_count": counts[2],
            "stress_pixel_percentage": (counts[2] / total) * 100 if total else 0,
            "stress_pixel_coordinates": _sample_coordinates(stress_mask & plot_mask, transform),
            "analysis_start_date": start_date,
            "analysis_end_date": end_date,
            "latest_image_date": latest_image_date,
        },
    }

# ---------------------------------------------------------------------------
# Water uptake - thresholds exactly copied from admintest.py
# ---------------------------------------------------------------------------
def _compute_water_uptake(plot_name: str, start_date: str, end_date: str) -> Dict[str, Any]:
    plot = _get_plot(plot_name)
    geometry = plot["geometry"]
    scene = pick_shared_scene(geometry, start_date, end_date)
    if not scene:
        raise HTTPException(status_code=404, detail="No Sentinel-1 or Sentinel-2 images in the selected period")
    latest_date = scene["date"]
    use_s2 = scene["sensor"] == "s2"
    previous_date = None

    if use_s2:
        item = scene["item"]
        green, transform, crs = _read_s2_band(item, "green", geometry)
        nir, _, _ = _read_s2_band(item, "nir", geometry)
        green = _apply_s2_scale(green, item, "green")
        nir = _apply_s2_scale(nir, item, "nir")
        ndwi = (green - nir) / (green + nir + 1e-12)
        deficient = ndwi < -0.5
        less = (ndwi >= -0.5) & (ndwi < -0.42)
        adequat = (ndwi >= -0.42) & (ndwi < -0.375)
        excellent = (ndwi >= -0.375) & (ndwi < -0.365)
        very_healthy = ndwi >= -0.365
        sensor = "s2"
        image_count = 1
    else:
        items = _find_s1_items_on_date(geometry, latest_date)
        if not items:
            raise HTTPException(status_code=404, detail=f"Need S1 VH scenes for water delta around {latest_date}")
        latest_item = items[0]
        latest_vh, transform, crs = _read_s1_band(latest_item, "vh", geometry)
        # Find the latest earlier acquisition intersecting the same AOI.
        all_s1 = _stac_search(S1_COLLECTION, geometry, start_date, latest_date, 100)
        earlier = [x for x in all_s1 if _s1_iw_vv_vh(x) and (_item_dt(x) or datetime.min.replace(tzinfo=timezone.utc)) < (_item_dt(latest_item) or datetime.min.replace(tzinfo=timezone.utc))]
        if not earlier:
            raise HTTPException(status_code=404, detail=f"Need 2 S1 VH scenes for water delta around {latest_date}")
        previous_item = max(earlier, key=lambda x: _item_dt(x) or datetime.min.replace(tzinfo=timezone.utc))
        previous_date = _item_date(previous_item)
        previous_vh, _, _ = _read_s1_band(previous_item, "vh", geometry)
        delta_vh = latest_vh - previous_vh
        very_healthy = delta_vh >= 6
        excellent = (delta_vh >= 4.0) & (delta_vh < 6)
        adequat = (delta_vh > -0.1) & (delta_vh < 4)
        less = (delta_vh >= -3.13) & (delta_vh <= -0.1)
        deficient = delta_vh < -3.13
        sensor = "s1"
        image_count = 2

    plot_mask = _mask_to_plot(np.ones(deficient.shape, dtype=bool), transform, geometry)
    classes = np.zeros(deficient.shape, dtype=np.float32)
    classes[deficient & plot_mask] = 1
    classes[less & plot_mask] = 2
    classes[adequat & plot_mask] = 3
    classes[excellent & plot_mask] = 4
    classes[very_healthy & plot_mask] = 5
    tile_key = _register_tile_raster(
        classes,
        transform,
        crs,
        WATER_PALETTE,
        0,
        5,
        geometry
    )
    counts = _counts(classes, [1, 2, 3, 4, 5], plot_mask)
    total = sum(counts.values())

    coords = {
        1: _sample_coordinates((classes == 1) & plot_mask, transform),
        2: _sample_coordinates((classes == 2) & plot_mask, transform),
        3: _sample_coordinates((classes == 3) & plot_mask, transform),
        4: _sample_coordinates((classes == 4) & plot_mask, transform),
        5: _sample_coordinates((classes == 5) & plot_mask, transform),
    }

    props = {
        "plot_name": plot_name,
        "tile_url": _tile_url(tile_key),
        "thumb_url": f"{(settings.public_base_url or os.getenv('PUBLIC_BASE_URL') or '').rstrip('/')}/analysis-thumbs/{tile_key}.png",
        "sensor": sensor,
        "image_count_in_range": image_count,
        "analysis_dates": {
            "start_date": start_date,
            "end_date": end_date,
            "latest_image_date": latest_date,
            "previous_image_date": previous_date,
        },
        "last_updated": datetime.now().isoformat(),
    }
    
    names = [(1, "deficient"), (2, "less"), (3, "adequat"), (4, "excellent"), (5, "very_healthy")]
    summary = {"total_pixel_count": total, "analysis_start_date": start_date, "analysis_end_date": end_date}
        
    for cid, name in names:
            summary[f"{name}_pixel_count"] = counts[cid]
            summary[f"{name}_pixel_percentage"] = (counts[cid] / total) * 100 if total else 0
            summary[f"{name}_pixel_coordinates"] = coords[cid]
    return {"type": "FeatureCollection", "features": [_common_feature(plot_name, plot, props)], "pixel_summary": summary}

# ---------------------------------------------------------------------------
# Soil moisture - thresholds exactly copied from admintest.py
# ---------------------------------------------------------------------------
def _compute_soil_moisture(plot_name: str, start_date: str, end_date: str) -> Dict[str, Any]:
    plot = _get_plot(plot_name)
    geometry = plot["geometry"]
    scene = pick_shared_scene(geometry, start_date, end_date)
    if not scene:
        raise HTTPException(status_code=404, detail="No satellite images available in given date range")
    latest_date = scene["date"]
    use_s2 = scene["sensor"] == "s2"
    sensor_used = "Sentinel-2" if use_s2 else "Sentinel-1"

    if use_s2:
        item = scene["item"]
        # Exact original formula: normalizedDifference([B8A, B11]).
        nir08, transform, crs = _read_s2_band(item, "nir08", geometry)
        swir16, _, _ = _read_s2_band(item, "swir16", geometry)
        nir08 = _apply_s2_scale(nir08, item, "nir08")
        swir16 = _apply_s2_scale(swir16, item, "swir16")
        ndmi = (nir08 - swir16) / (nir08 + swir16 + 1e-12)
        classes = np.zeros(ndmi.shape, dtype=np.float32)
        classes[ndmi <= -0.3] = 1
        classes[(ndmi > -0.3) & (ndmi <= 0)] = 2
        classes[(ndmi > 0) & (ndmi <= 0.1)] = 3
        classes[(ndmi > 0.1) & (ndmi <= 0.24)] = 4
        classes[ndmi > 0.24] = 5
    else:
        items = _find_s1_items_on_date(geometry, latest_date)
        if not items:
            raise HTTPException(status_code=404, detail=f"S1 VV image missing on {latest_date}")
        item = items[0]
        vv, transform, crs = _read_s1_band(item, "vv", geometry)
        classes = np.zeros(vv.shape, dtype=np.float32)
        classes[vv <= -12] = 1
        classes[(vv > -12) & (vv <= -10)] = 2
        classes[(vv > -10) & (vv <= -8)] = 3
        classes[(vv > -8) & (vv <= -6)] = 4
        classes[vv > -6] = 5

    plot_mask = _mask_to_plot(np.ones(classes.shape, dtype=bool), transform, geometry)
    classes[~plot_mask] = 0
    tile_key = _register_tile_raster(
        classes,
        transform,
        crs,
        SOIL_PALETTE,
        0,
        5,
        geometry
    )
    counts = _counts(classes, [1, 2, 3, 4, 5], plot_mask)
    total = sum(counts.values())
    names = [(1, "less"), (2, "adequate"), (3, "excellent"), (4, "excess"), (5, "shallow_water")]
    summary = {
        "total_pixel_count": total,
        "start_date": start_date,
        "end_date": end_date,
        "latest_image_date": latest_date,
        "sensor_used": sensor_used,
        "s1_available": not use_s2,
        "s2_available": use_s2,
        "s1_latest_date": None if use_s2 else latest_date,
        "s2_latest_date": latest_date if use_s2 else None,
    }
    for cid, name in names:
        summary[f"{name}_pixel_count"] = counts[cid]
        summary[f"{name}_pixel_percentage"] = round((counts[cid] / total) * 100, 2) if total else 0
        summary[f"{name}_pixel_coordinates"] = _sample_coordinates((classes == cid) & plot_mask, transform)
    props = {
        "plot_name": plot_name,
        "start_date": start_date,
        "end_date": end_date,
        "sensor_used": sensor_used,
        "latest_image_date": latest_date,
        "image_count": 1,
        "tile_url": _tile_url(tile_key),
        "thumb_url": f"{(settings.public_base_url or os.getenv('PUBLIC_BASE_URL') or '').rstrip('/')}/analysis-thumbs/{tile_key}.png",
        "last_updated": datetime.now().isoformat(),
    }
    return {"type": "FeatureCollection", "features": [_common_feature(plot_name, plot, props)], "pixel_summary": summary}

# ---------------------------------------------------------------------------
# Pest detection - formulas/thresholds exactly copied from admintest.py
# ---------------------------------------------------------------------------
def _db_to_linear(img):
    return np.power(10.0, img / 10.0) + 1e-6


def _s1_median_arrays(items: List[dict], geometry, need_vv=True, need_vh=True):
    arrays_vv, arrays_vh = [], []
    transform = crs = None
    for item in items:
        try:
            if need_vv:
                a, transform, crs = _read_s1_band(item, "vv", geometry)
                arrays_vv.append(a)
            if need_vh:
                a, transform, crs = _read_s1_band(item, "vh", geometry)
                arrays_vh.append(a)
        except Exception as exc:
            logger.warning("Skipping S1 item %s: %s", item.get("id"), exc)
    if not arrays_vv or not arrays_vh:
        return None, None, transform, crs
    return np.nanmedian(np.stack(arrays_vv), axis=0), np.nanmedian(np.stack(arrays_vh), axis=0), transform, crs


def _s1_items(geometry, start_date, end_date, descending_only=False, fallback_days: int = 45):
    """Return usable S1 VV/VH scenes, with a bounded acquisition fallback.

    Sentinel-1 revisit is not guaranteed for every AOI in every 15-day window.
    If the requested window contains no scene, look backwards up to
    ``fallback_days`` from the requested start date. This prevents a valid
    plot from becoming a 404 merely because the latest acquisition was, for
    example, 20-30 days earlier. The response still reports the real image
    date used.
    """
    items = _stac_search(S1_COLLECTION, geometry, start_date, end_date, 200)

    def usable(rows):
        out = []
        for item in rows:
            if not _s1_iw_vv_vh(item):
                continue
            if descending_only:
                p = item.get("properties") or {}
                orbit = str(p.get("sat:orbit_state") or p.get("orbitProperties_pass") or "").upper()
                if orbit not in ("DESCENDING", "DESC"):
                    continue
            out.append(item)
        out.sort(key=lambda x: _item_dt(x) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return out

    out = usable(items)
    if out:
        return out

    logger.warning(
        "No usable S1 VV/VH items in requested window %s..%s (STAC returned %d items); trying bounded historical fallback",
        start_date, end_date, len(items),
    )

    # Bounded fallback: search earlier acquisitions only.
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
        fallback_start = start - timedelta(days=int(fallback_days))
        earlier = _stac_search(
            S1_COLLECTION, geometry,
            fallback_start.strftime("%Y-%m-%d"),
            start_date,
            200,
        )
        out = usable(earlier)
        if out:
            logger.warning(
                "No S1 VV/VH scene in requested window %s..%s; using latest available acquisition %s",
                start_date, end_date, _item_date(out[0])
            )
    except Exception as exc:
        logger.warning("S1 fallback search failed: %s", exc)
    return out


def _compute_pest_detection(plot_name: str, start_date: str, end_date: str) -> Dict[str, Any]:
    # The supplied implementation intentionally derives its actual analysis
    # period from end_date (-15 days) and its baseline from previous year
    # June 1 -> October 30. Preserve that behavior exactly.
    today = datetime.strptime(end_date, "%Y-%m-%d").date()
    analysis_end = today
    analysis_start = today - timedelta(days=15)
    baseline_start = date(analysis_end.year - 1, 6, 1)
    baseline_end = date(analysis_end.year - 1, 10, 30)

    plot = _get_plot(plot_name)
    geometry = plot["geometry"]
    analysis_items = _s1_items(geometry, analysis_start.strftime("%Y-%m-%d"), analysis_end.strftime("%Y-%m-%d"))
    if not analysis_items:
        raise HTTPException(status_code=404, detail="No Sentinel-1 IW images found")

    # First SAR composite: VV, VH and VV/VH, then min-max normalize the mean
    # of those 3 bands and water_index = -VH, exactly as in the original.
    vv, vh, transform, crs = _s1_median_arrays(analysis_items, geometry)
    if vv is None or vh is None:
        raise HTTPException(status_code=404, detail="No usable Sentinel-1 VV/VH assets found")
    ratio = vv / (vh + 1e-6)
    sar_mean = np.nanmean(np.stack([vv, vh, ratio]), axis=0)
    water_index = vh * -1

    def normalize01(arr):
        lo = np.nanmin(arr)
        hi = np.nanmax(arr)
        if not np.isfinite(lo) or not np.isfinite(hi) or hi == lo:
            return np.zeros_like(arr, dtype=np.float32)
        return np.clip((arr - lo) / (hi - lo), 0, 1)

    sar_norm = normalize01(sar_mean)
    water_norm = normalize01(water_index)
    avg = (sar_norm + water_norm) * 0.5
    low = float(np.nanpercentile(avg, 2))
    high = float(np.nanpercentile(avg, 98))
    stretched = np.clip((avg - low) / (high - low if high != low else 1), 0, 1)
    chewing_mask = stretched <= 0.01

    # Baseline and analysis VV/VH ratio.
    baseline_items = _s1_items(geometry, baseline_start.strftime("%Y-%m-%d"), baseline_end.strftime("%Y-%m-%d"))
    if not baseline_items:
        raise HTTPException(status_code=404, detail="No Sentinel-1 baseline images found")
    bvv, bvh, _, _ = _s1_median_arrays(baseline_items, geometry)
    if bvv is None or bvh is None:
        raise HTTPException(status_code=404, detail="No usable Sentinel-1 baseline VV/VH assets found")
    baseline_ratio = bvv / (bvh + 1e-6)
    analysis_ratio = vv / (vh + 1e-6)
    delta_ratio = analysis_ratio - baseline_ratio
    stress_mask = delta_ratio >= 0.45
    max_delta = float(np.nanmax(delta_ratio)) if np.isfinite(delta_ratio).any() else 1.0
    if not np.isfinite(max_delta) or max_delta == 0:
        max_delta = 1.0
    severity = np.where(stress_mask, delta_ratio / max_delta, np.nan)
    fungi_mask = severity >= 0.75
    sucking_mask = avg >= 1
    wilt_mask = severity >= 1

    # Grub/soilborne branch: descending orbit only.
    grub_analysis_items = _s1_items(geometry, analysis_start.strftime("%Y-%m-%d"), analysis_end.strftime("%Y-%m-%d"), descending_only=True)
    grub_baseline_items = _s1_items(geometry, baseline_start.strftime("%Y-%m-%d"), baseline_end.strftime("%Y-%m-%d"), descending_only=True)
    if grub_analysis_items:
        gvv, gvh, _, _ = _s1_median_arrays(grub_analysis_items, geometry)
    else:
        gvv, gvh = vv.copy(), vh.copy()
    if grub_baseline_items:
        gbvv, gbvh, _, _ = _s1_median_arrays(grub_baseline_items, geometry)
    else:
        gbvv, gbvh = bvv.copy(), bvh.copy()
    vv_lin = _db_to_linear(gvv)
    vh_lin = _db_to_linear(gvh)
    baseline_vv = _db_to_linear(gbvv)
    baseline_vh = _db_to_linear(gbvh)
    vhvv_ratio = vh_lin / vv_lin
    rvi = vh_lin * 4 / (vv_lin + vh_lin)
    vv_norm = np.clip((vv_lin - 0.001) / (0.3 - 0.001), 0, 1)
    baseline_rvi = baseline_vh * 4 / (baseline_vv + baseline_vh)
    baseline_vhvv = baseline_vh / baseline_vv
    rvi_drop = baseline_rvi - rvi
    vhvv_drop = baseline_vhvv - vhvv_ratio
    vh_drop = vh_lin - baseline_vh
    veg_stress = (rvi_drop > 0.30) & (vhvv_drop > 0.2) & (vh_drop < -0.01)
    water_stress = vv_norm < 0.10
    soilborne_mask = veg_stress & water_stress

    plot_mask = _mask_to_plot(np.ones(vv.shape, dtype=bool), transform, geometry)
    classes = np.zeros(vv.shape, dtype=np.float32)
    classes[chewing_mask & plot_mask] = 1
    classes[fungi_mask & plot_mask] = 2
    classes[sucking_mask & plot_mask] = 3
    classes[wilt_mask & plot_mask] = 4
    classes[soilborne_mask & plot_mask] = 5
    # Visualization only: 0 = no pest (RED), 1 = pest present (BLACK).
    # Existing `classes`, masks, counts and analysis logic remain unchanged.
    pest_presence = (classes > 0).astype(np.float32)

    tile_key = _register_tile_raster(
        pest_presence,
        transform,
        crs,
        PEST_PALETTE,
        0,
        1,
        geometry
    )

    masks = {
        "chewing_affected": chewing_mask & plot_mask,
        "fungi_affected": fungi_mask & plot_mask,
        "sucking_affected": sucking_mask & plot_mask,
        "wilt_affected": wilt_mask & plot_mask,
        "SoilBorn": soilborne_mask & plot_mask,
    }
    counts = {k: int(np.count_nonzero(v)) for k, v in masks.items()}
    total = int(np.count_nonzero(plot_mask & np.isfinite(classes)))
    healthy = total - sum(counts.values())
    image_dates = [_item_date(x) for x in analysis_items]

    summary = {
        "total_pixel_count": total,
        "healthy_pixel_count": healthy,
    }
    for k, mask in masks.items():
        summary[f"{k}_pixel_count"] = counts[k]
        pct_key = f"{k}_pixel_percentage"
        if k == "SoilBorn":
            pct_key = "SoilBorn_affected_pixel_percentage"
        summary[pct_key] = (counts[k] / total) * 100 if total else 0
        coord_key = f"{k}_pixel_coordinates"
        if k == "SoilBorn":
            coord_key = "SoilBorn_affected_pixel_coordinates"
        summary[coord_key] = _sample_coordinates(mask, transform)
        summary.update({
        "baseline_start_date": baseline_start.strftime("%Y-%m-%d"),
        "baseline_end_date": baseline_end.strftime("%Y-%m-%d"),
        "analysis_start_date": analysis_start.strftime("%Y-%m-%d"),
        "analysis_end_date": analysis_end.strftime("%Y-%m-%d"),
    })
    props = {
        "plot_name": plot_name,
        "start_date": start_date,
        "end_date": end_date,
        "image_count": len(analysis_items),
        "image_dates": image_dates,
        "tile_url": _tile_url(tile_key),
        "thumb_url": f"{(settings.public_base_url or os.getenv('PUBLIC_BASE_URL') or '').rstrip('/')}/analysis-thumbs/{tile_key}.png",
        "last_updated": datetime.now().isoformat(),
    }
    return {"type": "FeatureCollection", "features": [_common_feature(plot_name, plot, props)], "pixel_summary": summary}

# ---------------------------------------------------------------------------
# Area / cache helpers
# ---------------------------------------------------------------------------
def _area_acres(geometry) -> float:
    """Approximate geodesic area in acres using a local equal-area projection."""
    try:
        from rasterio.crs import CRS
        from pyproj import Transformer, Geod
        geod = Geod(ellps="WGS84")
        area, _ = geod.geometry_area_perimeter(geometry)
        return abs(area) / 4046.8564224
    except Exception:
        return 0.0


def _cache_key(endpoint: str, plot_name: str, start_date: str, end_date: str):
    return endpoint, _normalize_plot_name(plot_name), start_date, end_date


def _cached_or_compute(key, fn):
    cached = _response_cache.get(key)
    if cached is not None:
        return cached
    result = fn()
    _response_cache[key] = result
    return result

# ---------------------------------------------------------------------------
# Basic endpoints
# ---------------------------------------------------------------------------
@router.get("/plots", response_model=List[str])
async def get_plots():
    return await asyncio.to_thread(_list_plot_ids)


@router.get("/debug/plot/{plot_name}")
async def debug_plot(plot_name: str):
    normalized = _normalize_plot_name(plot_name)
    plot = await asyncio.to_thread(_load_plot, plot_name)
    return {
        "requested": plot_name,
        "normalized": normalized,
        "exact_exists": plot is not None and str(plot_name).strip() == normalized,
        "normalized_exists": plot is not None,
        "plot_count": await asyncio.to_thread(_count_plots),
        "source": "field_boundaries",
    }


@router.get("/plots/{plot_name}/info")
async def get_plot_info(plot_name: str):
    plot = _get_plot(plot_name)
    return {
        "name": plot_name,
        "geometry_type": plot["geom_type"],
        "area_acres": round(_area_acres(plot["geometry"]), 2),
    }


@router.get("/satellite/health")
async def health_check():
    return {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "metrics_endpoint": "/metrics",
        "prometheus_enabled": True,
        "satellite_backend": STAC_API_URL,
        "aws_signed": bool(_aws_credentials()),
        "plot_source": "field_boundaries",
    }


@router.get("/metrics")
async def metrics():
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"},
    )


@router.get("/refresh-from-django", operation_id="admin_refresh_from_django_get")
@router.post("/refresh-from-django", operation_id="admin_refresh_from_django_post")
async def refresh_from_django():
    try:
        def _refresh():
            with _plot_lock:
                _plot_cache.clear()
            count = _count_plots()
            return count
        count = await asyncio.to_thread(_refresh)
        return {
            "status": "success",
            "message": f"Successfully refreshed {count} plots from field_boundaries",
            "plot_count": count,
            "plots_with_django_ids": count,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to refresh plots: {exc}") from exc

# ---------------------------------------------------------------------------
# Endpoint wrappers - same parameters and response contracts
# ---------------------------------------------------------------------------
@router.post("/analyze_Growth", response_model=Dict[str, Any])
async def analyze_growth(
    plot_name: str = Query(..., description="Name of the plot to analyze"),
    end_date: str = Query(default_factory=lambda: (date.today() + timedelta(days=1)).strftime("%Y-%m-%d"), description="End date for analysis"),
    start_date: Optional[str] = Query(None, description="Optional start date (default: end_date - 15 days)"),
    skip_cache: bool = Query(False, description="Skip DB/memory cache and recompute"),
    django_id: Optional[int] = Query(None, description="Django plot id when fastapi_plot_id is shared"),
):
    if django_id is not None:
        plot_name = str(django_id)
    _get_plot(plot_name)
    explicit_start = start_date is not None
    start_date = start_date or default_start_date(end_date)
    key = _cache_key("analyze_Growth", plot_name, start_date, end_date)
    if not skip_cache and not explicit_start:
        cached = _response_cache.get(key)
        if cached is not None:
            return cached
    result = await asyncio.to_thread(_compute_analyze_growth, plot_name, start_date, end_date)
    _response_cache[key] = result
    return result


@router.get("/wateruptake", response_model=Dict[str, Any])
@router.post("/wateruptake", response_model=Dict[str, Any])
async def analyze_water_uptake(
    plot_name: str,
    end_date: str = Query(default_factory=lambda: (date.today() + timedelta(days=1)).strftime("%Y-%m-%d"), description="End date for analysis"),
    days_back: Optional[int] = Query(None, description="Days before end_date for start (e.g. 7); if omitted, start defaults to end_date - 15 days"),
    start_date: Optional[str] = Query(None, description="Start date (optional); if omitted, computed from end_date"),
    skip_cache: bool = Query(False, description="Skip DB/memory cache and recompute"),
):
    explicit_start = start_date is not None
    if start_date is None:
        try:
            d = datetime.strptime(end_date, "%Y-%m-%d").date()
            start_date = (d - timedelta(days=days_back if days_back is not None else 15)).strftime("%Y-%m-%d")
        except Exception:
            start_date = default_start_date(end_date)
    _get_plot(plot_name)
    key = _cache_key("wateruptake", plot_name, start_date, end_date)
    if not skip_cache and not explicit_start:
        cached = _response_cache.get(key)
        if cached is not None:
            return cached
    result = await asyncio.to_thread(_compute_water_uptake, plot_name, start_date, end_date)
    _response_cache[key] = result
    return result


@router.post("/SoilMoisture", response_model=Dict[str, Any])
async def analyze_soil_moisture(
    plot_name: str,
    end_date: str = Query(default_factory=lambda: (date.today() + timedelta(days=1)).strftime("%Y-%m-%d"), description="End date for analysis"),
    start_date: Optional[str] = Query(None, description="Optional start date (default: end_date - 15 days)"),
    skip_cache: bool = Query(False, description="Skip DB/memory cache and recompute"),
):
    explicit_start = start_date is not None
    start_date = start_date or default_start_date(end_date)
    _get_plot(plot_name)
    key = _cache_key("SoilMoisture", plot_name, start_date, end_date)
    if not skip_cache and not explicit_start:
        cached = _response_cache.get(key)
        if cached is not None:
            return cached
    result = await asyncio.to_thread(_compute_soil_moisture, plot_name, start_date, end_date)
    _response_cache[key] = result
    return result


@router.post("/pest-detection", response_model=Dict[str, Any])
async def pest_detection_combined(
    plot_name: str,
    end_date: str = Query(default_factory=lambda: (date.today() + timedelta(days=1)).strftime("%Y-%m-%d"), description="End date for analysis"),
    start_date: Optional[str] = Query(None, description="Optional start date (kept for response compatibility)"),
    skip_cache: bool = Query(False, description="Skip DB/memory cache and recompute"),
):
    # Original implementation derives analysis_start from end_date, so preserve it.
    effective_start = start_date or default_start_date(end_date)
    _get_plot(plot_name)
    key = _cache_key("pest-detection", plot_name, effective_start, end_date)
    if not skip_cache:
        cached = _response_cache.get(key)
        if cached is not None:
            return cached
    result = await asyncio.to_thread(_compute_pest_detection, plot_name, effective_start, end_date)
    _response_cache[key] = result
    return result

# ---------------------------------------------------------------------------
# Daily report - compatible aggregation using the AWS-backed four analyses.
# External soil/weather/event services remain configurable, as in the original.
# ---------------------------------------------------------------------------
_REPORT_CACHE: TTLCache = TTLCache(maxsize=500, ttl=1800)
_SOIL_BASE = os.getenv("SOIL_BASE_URL", "https://main-cropeye.up.railway.app").rstrip("/")
_WEATHER_BASE = os.getenv("WEATHER_BASE_URL", "https://weather-cropeye.up.railway.app").rstrip("/")
_EVENTS_BASE = os.getenv("EVENTS_BASE_URL", "https://events-cropeye.up.railway.app").rstrip("/")


def _safe_call(fn):
    try:
        return fn(), None
    except Exception as exc:
        return None, str(exc)


def _strip_coords(pixel_summary: dict) -> dict:
    return {k: v for k, v in pixel_summary.items() if not k.endswith("_coordinates")}


def _fetch_django_plot_detail_simple(plot_name: str) -> dict:
    plot = _load_plot(plot_name) or {}
    return plot.get("django_record") or {}


def _build_plot_info(plot_name: str, django_detail: dict) -> dict:
    plot = _load_plot(plot_name) or {}
    props = plot.get("properties") or {}
    address = django_detail.get("address") or {}
    location = django_detail.get("location") or {}
    boundary = django_detail.get("boundary") or {}
    farmer = django_detail.get("farmer") or {}
    farms = django_detail.get("farms") or []
    farms_clean = []
    for f in farms:
        irrigations = f.get("irrigations") or []
        owner = f.get("farm_owner") or {}
        farms_clean.append({
            "farm_id": f.get("id"), "plantation_date": f.get("plantation_date"),
            "plantation_type": f.get("plantation_type"), "plantation_type_code": f.get("plantation_type_code"),
            "planting_method": f.get("planting_method"), "planting_method_code": f.get("planting_method_code"),
            "crop_variety": f.get("crop_variety"), "crop_type_name": f.get("crop_type_name"),
            "soil_type_id": f.get("soil_type_id"), "spacing_a": f.get("spacing_a"), "spacing_b": f.get("spacing_b"),
            "variety_type": f.get("variety_type"), "variety_subtype": f.get("variety_subtype"), "variety_timing": f.get("variety_timing"),
            "plant_age": f.get("plant_age"), "sugarcane_type": f.get("sugarcane_type"), "sugarcane_yield": f.get("sugarcane_yield"),
            "foundation_pruning_date": f.get("foundation_pruning_date"), "fruit_pruning_date": f.get("fruit_pruning_date"),
            "last_harvesting_date": f.get("last_harvesting_date"), "irrigations": irrigations,
            "irrigation_types": [i.get("type") for i in irrigations if i.get("type")],
            "farm_owner": {"id": owner.get("id"), "username": owner.get("username"), "full_name": owner.get("full_name")} if owner else None,
        })
    coords = location.get("coordinates") or []
    return {
        "plot_id": django_detail.get("id") or props.get("django_id"),
        "fastapi_plot_id": django_detail.get("fastapi_plot_id") or props.get("fastapi_plot_id") or plot_name,
        "gat_number": django_detail.get("gat_number") or props.get("gat_number"),
        "plot_number": django_detail.get("plot_number") or props.get("plot_number"),
        "address": {
            "village": address.get("village") or props.get("village"), "taluka": address.get("taluka") or props.get("taluka"),
            "district": address.get("district") or props.get("district"), "state": address.get("state") or props.get("state"),
            "country": address.get("country") or props.get("country"), "pin_code": address.get("pin_code") or props.get("pin_code"),
        },
        "location": {"type": location.get("type"), "latitude": location.get("latitude") or (coords[1] if len(coords) >= 2 else None), "longitude": location.get("longitude") or (coords[0] if len(coords) >= 2 else None), "coordinates": location.get("coordinates")},
        "boundary": {"type": boundary.get("type"), "coordinates": boundary.get("coordinates"), "has_boundary": boundary.get("has_boundary")} if boundary else None,
        "farmer": {"id": farmer.get("id"), "username": farmer.get("username"), "full_name": farmer.get("full_name")} if farmer else None,
        "farms": farms_clean, "farms_count": django_detail.get("farms_count") or len(farms_clean),
        "created_at": django_detail.get("created_at"), "updated_at": django_detail.get("updated_at"),
    }


@router.get("/daily-report", response_model=Dict[str, Any])
async def get_daily_report(
    plot_name: str = Query(..., description="Plot identifier, e.g. '123_4'"),
    date: str = Query(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"), description="Report date YYYY-MM-DD (defaults to today)"),
    lat: Optional[float] = Query(None, description="Latitude for weather data"),
    lon: Optional[float] = Query(None, description="Longitude for weather data"),
):
    _get_plot(plot_name)
    cache_key = ("daily-report", plot_name, date, lat, lon)
    if cache_key in _REPORT_CACHE:
        return _REPORT_CACHE[cache_key]
    end_date = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    start_date = default_start_date(end_date)

    def _growth(): return _compute_analyze_growth(plot_name, start_date, end_date)
    def _soil(): return _compute_soil_moisture(plot_name, start_date, end_date)
    def _water(): return _compute_water_uptake(plot_name, start_date, end_date)
    def _pest(): return _compute_pest_detection(plot_name, start_date, end_date)

    # Keep non-satellite integrations parallel, matching the original report design.
    def _current_weather():
        if lat is None or lon is None: return None
        r = requests.get(f"{_WEATHER_BASE}/current-weather", params={"lat": lat, "lon": lon}, timeout=30)
        r.raise_for_status(); return r.json()
    def _forecast():
        if lat is None or lon is None: return None
        r = requests.get(f"{_WEATHER_BASE}/forecast", params={"lat": lat, "lon": lon}, timeout=30)
        r.raise_for_status(); return r.json().get("data")
    def _agro_stats():
        r = requests.get(f"{_EVENTS_BASE}/plots/{plot_name}/agroStats", params={"end_date": date}, timeout=120)
        r.raise_for_status(); return r.json()
    def _npk():
        r = requests.post(f"{_SOIL_BASE}/required-n/{plot_name}", params={"end_date": date}, timeout=60)
        r.raise_for_status(); return r.json()
    def _npk_analysis():
        r = requests.post(f"{_SOIL_BASE}/analyze-npk/{plot_name}", params={"date": date, "fe_days_back": 30}, timeout=120)
        r.raise_for_status(); return r.json()

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {
            "django_detail": ex.submit(_safe_call, lambda: _fetch_django_plot_detail_simple(plot_name)),
            "agro_stats": ex.submit(_safe_call, _agro_stats), "npk": ex.submit(_safe_call, _npk),
            "npk_analysis": ex.submit(_safe_call, _npk_analysis), "current_weather": ex.submit(_safe_call, _current_weather),
            "forecast": ex.submit(_safe_call, _forecast), "growth": ex.submit(_safe_call, _growth),
            "soil_moisture": ex.submit(_safe_call, _soil), "water_uptake": ex.submit(_safe_call, _water),
            "pest_detection": ex.submit(_safe_call, _pest),
        }
        results = {k: f.result() for k, f in futures.items()}

    def val(k): return results[k][0]
    errors = {k: results[k][1] for k in results if results[k][1] is not None}

    def satellite_section(raw, property_keys):
        if not raw: return None
        feat = (raw.get("features") or [{}])[0]
        props = feat.get("properties", {})
        out = {k: props.get(v) for k, v in property_keys.items()}
        out["pixel_summary"] = _strip_coords(raw.get("pixel_summary") or {})
        return out

    growth_raw, sm_raw, wu_raw, pd_raw = val("growth"), val("soil_moisture"), val("water_uptake"), val("pest_detection")
    report = {
        "report_date": date, "plot_name": plot_name, "generated_at": datetime.now().isoformat(timespec="seconds"),
        "plot_info": _build_plot_info(plot_name, val("django_detail") or {}),
        "agro_stats": {k: v for k, v in (val("agro_stats") or {}).items() if k != "geometry"} if val("agro_stats") else None,
        "growth": satellite_section(growth_raw, {"data_source": "data_source", "latest_image_date": "latest_image_date", "image_count": "image_count", "tile_url": "tile_url"}),
        "soil_moisture": satellite_section(sm_raw, {"sensor_used": "sensor_used", "latest_image_date": "latest_image_date", "image_count": "image_count", "tile_url": "tile_url"}),
        "water_uptake": satellite_section(wu_raw, {"sensor": "sensor", "latest_image_date": "latest_image_date", "image_count": "image_count_in_range", "tile_url": "tile_url"}),
        "pest_detection": satellite_section(pd_raw, {"image_count": "image_count", "image_dates": "image_dates", "tile_url": "tile_url"}),
        "npk": val("npk"), "npk_analysis": val("npk_analysis"), "current_weather": val("current_weather"), "forecast": val("forecast"),
        "errors": errors or None,
    }
    _REPORT_CACHE[cache_key] = report
    return report