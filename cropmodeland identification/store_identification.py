"""Classify Kabirdham fields and store only Rice, Cotton, and Sugarcane.

Reads polygons from field_boundaries. Uses the joblib models in this folder.
An existing plot for the same field and season is updated. A plot that this
run does not label Rice, Cotton, or Sugarcane is deleted. A plot whose
satellite read failed is left unchanged.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import ee
import joblib
import pandas as pd
from shapely.geometry import Polygon
from sqlalchemy import bindparam, text
from sqlalchemy.exc import OperationalError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import engine  # noqa: E402

SCRIPT_DIR = Path(__file__).parent
KEEP = ("Rice", "Cotton", "Sugarcane")
GEE_PROJECT = os.environ.get("EE_PROJECT", "inlaid-way-490106-u1")
CONFIDENCE_THRESHOLD = 0.75
MARGIN_THRESHOLD = 0.20
MIN_AREA_M2 = 900.0
TREE_CIRC_THRESH = 0.78
TREE_ASPECT_THRESH = 0.72
TREE_MAX_AREA_M2 = 20000
TREE_MIN_VERTS = 16
CHUNK_SIZE = 50
CHUNK_DELAY = 6
WINDOW_DAYS = 15
CLOUD_MAX = 60
SCALE = 10
S2_BANDS = ["B2", "B3", "B4", "B5", "B6", "B7", "B8", "B8A", "B11", "B12"]
S2_INDICES = [
    "NDVI", "GNDVI", "NDRE", "NDWI", "NDMI", "SAVI", "EVI", "BSI",
    "RGI", "GCI", "MCARI", "NDBI", "ARVI", "MSAVI2",
]
S1_BANDS = ["VV", "VH", "VH_VV_ratio", "RVI"]
ALL_BANDS = S2_BANDS + S2_INDICES + S1_BANDS
TIME_OFFSETS = {"tm45": -45, "t0": 0, "tp45": 45}

UPSERT = text(
    """
    INSERT INTO crop_identification (
      field_boundary_id, field_id, district_name, subdistrict_name, village_name,
      season, observation_date, identified_on, predicted_crop, final_label,
      is_rice, confidence, margin, area_ha, source_model, centroid_lon, centroid_lat
    ) VALUES (
      :field_boundary_id, :field_id, :district_name, :subdistrict_name, :village_name,
      :season, :observation_date, :identified_on, :predicted_crop, :final_label,
      :is_rice, :confidence, :margin, :area_ha, :source_model, :centroid_lon, :centroid_lat
    )
    ON DUPLICATE KEY UPDATE
      field_id = VALUES(field_id),
      district_name = VALUES(district_name),
      subdistrict_name = VALUES(subdistrict_name),
      village_name = VALUES(village_name),
      observation_date = VALUES(observation_date),
      identified_on = VALUES(identified_on),
      predicted_crop = VALUES(predicted_crop),
      final_label = VALUES(final_label),
      is_rice = VALUES(is_rice),
      confidence = VALUES(confidence),
      margin = VALUES(margin),
      area_ha = VALUES(area_ha),
      source_model = VALUES(source_model),
      centroid_lon = VALUES(centroid_lon),
      centroid_lat = VALUES(centroid_lat)
    """
)


def poly_area_m2(pts: list[tuple[float, float]]) -> float:
    try:
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = poly.buffer(0)
        lat0 = sum(point[1] for point in pts) / len(pts)
        kx = 111320 * math.cos(math.radians(lat0))
        return float(poly.area * kx * 111320)
    except Exception:
        return 0.0


def is_tree(pts: list[tuple[float, float]]) -> bool:
    try:
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty or poly.area <= 0:
            return False
        perim = poly.length
        if perim <= 0:
            return False
        circ = 4 * math.pi * poly.area / perim ** 2
        minx, miny, maxx, maxy = poly.bounds
        width, height = maxx - minx, maxy - miny
        aspect = min(width, height) / max(width, height) if max(width, height) > 0 else 0.0
        return (
            poly_area_m2(pts) < TREE_MAX_AREA_M2
            and circ > TREE_CIRC_THRESH
            and aspect > TREE_ASPECT_THRESH
            and len(pts) >= TREE_MIN_VERTS
        )
    except Exception:
        return False


def ring_points(ring: list) -> list[tuple[float, float]]:
    points = []
    for coord in ring:
        if len(coord) >= 2:
            points.append((float(coord[0]), float(coord[1])))
    if len(points) >= 2 and points[0] == points[-1]:
        points = points[:-1]
    return points


def largest_ring(raw: str) -> list[tuple[float, float]]:
    geometry = json.loads(raw)
    rings = []
    if geometry.get("type") == "Polygon":
        rings = geometry.get("coordinates") or []
    elif geometry.get("type") == "MultiPolygon":
        rings = [polygon[0] for polygon in geometry.get("coordinates") or [] if polygon]
    best: list[tuple[float, float]] = []
    best_area = -1.0
    for ring in rings:
        points = ring_points(ring)
        if len(points) < 3:
            continue
        area = poly_area_m2(points)
        if area > best_area:
            best = points
            best_area = area
    return best


def mask_clouds(img):
    qa = img.select("QA60")
    return img.updateMask(qa.bitwiseAnd(1 << 10).eq(0).And(qa.bitwiseAnd(1 << 11).eq(0)))


def add_indices(img):
    bands = {name: img.select(name) for name in S2_BANDS}
    return img.addBands([
        img.normalizedDifference(["B8", "B4"]).rename("NDVI"),
        img.normalizedDifference(["B8", "B3"]).rename("GNDVI"),
        img.normalizedDifference(["B8A", "B5"]).rename("NDRE"),
        img.normalizedDifference(["B3", "B8"]).rename("NDWI"),
        img.normalizedDifference(["B8", "B11"]).rename("NDMI"),
        bands["B8"].subtract(bands["B4"]).multiply(1.5).divide(bands["B8"].add(bands["B4"]).add(0.5)).rename("SAVI"),
        bands["B8"].subtract(bands["B4"]).multiply(2.5).divide(
            bands["B8"].add(bands["B4"].multiply(6)).subtract(bands["B2"].multiply(7.5)).add(1)
        ).rename("EVI"),
        (bands["B11"].add(bands["B4"])).subtract(bands["B8"].add(bands["B2"])).divide(
            (bands["B11"].add(bands["B4"])).add(bands["B8"].add(bands["B2"]))
        ).rename("BSI"),
        bands["B4"].divide(bands["B3"]).rename("RGI"),
        bands["B8"].divide(bands["B3"]).subtract(1).rename("GCI"),
        ((bands["B5"].subtract(bands["B4"])).subtract((bands["B5"].subtract(bands["B3"])).multiply(0.2))).multiply(
            bands["B5"].divide(bands["B4"])
        ).rename("MCARI"),
        img.normalizedDifference(["B11", "B8"]).rename("NDBI"),
        (bands["B8"].subtract(bands["B4"].multiply(2)).subtract(bands["B2"])).divide(
            bands["B8"].add(bands["B4"].multiply(2)).subtract(bands["B2"])
        ).rename("ARVI"),
        bands["B8"].multiply(2).add(1).subtract(
            ((bands["B8"].multiply(2).add(1)).pow(2).subtract(bands["B8"].subtract(bands["B4"]).multiply(8))).sqrt()
        ).multiply(0.5).rename("MSAVI2"),
    ])


def _keep_time(img):
    return img.set("system:time_start", img.get("system:time_start"))


def _latest_image(collection, dummy):
    """Newest scene in the window. An empty window stays masked."""
    newest = collection.map(_keep_time)
    underneath = dummy.set("system:time_start", 0)
    return ee.Image(ee.ImageCollection([underneath]).merge(newest).sort("system:time_start").mosaic())


def _s2_collection(start, end, region):
    return (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterDate(start, end)
        .filterBounds(region)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", CLOUD_MAX))
        .map(mask_clouds)
        .map(lambda img: _keep_time(img.select(S2_BANDS).toFloat()))
    )


def _s2_dummy(region):
    return (
        ee.Image.constant([0.0] * len(S2_BANDS))
        .rename(S2_BANDS)
        .toFloat()
        .updateMask(ee.Image.constant(0))
        .clip(region)
    )


def s2_composite(ref_date, region):
    """Latest clear Sentinel-2 scene in the 15 days before and after ref_date."""
    start = ref_date.advance(-WINDOW_DAYS, "day")
    end = ref_date.advance(WINDOW_DAYS, "day")
    latest = _latest_image(_s2_collection(start, end, region), _s2_dummy(region))
    return add_indices(latest.select(S2_BANDS))


def s1_composite(ref_date, region):
    """Latest Sentinel-1 scene in the same window."""
    start = ref_date.advance(-WINDOW_DAYS, "day")
    end = ref_date.advance(WINDOW_DAYS, "day")
    collection = (
        ee.ImageCollection("COPERNICUS/S1_GRD")
        .filterDate(start, end)
        .filterBounds(region)
        .filter(ee.Filter.eq("instrumentMode", "IW"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
        .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
        .select(["VV", "VH"])
        .map(lambda img: _keep_time(img.toFloat()))
    )
    dummy = (
        ee.Image.constant([0.0, 0.0])
        .rename(["VV", "VH"])
        .toFloat()
        .updateMask(ee.Image.constant(0))
        .clip(region)
    )
    composite = _latest_image(collection, dummy).select(["VV", "VH"])
    vv, vh = composite.select("VV"), composite.select("VH")
    return composite.addBands([
        vh.subtract(vv).rename("VH_VV_ratio"),
        vh.multiply(4).divide(vv.add(vh)).rename("RVI"),
    ])


def _scene_millis_band(year: int, month: int, region):
    """Acquisition time of the latest clear scene in the full identification window.

    The window runs from 60 days before the 15th to 60 days after it: the 45-day
    offsets plus the 15-day slice around each offset. The 15th itself is not used.
    """
    center = ee.Date.fromYMD(year, month, 15)
    reach = max(abs(offset) for offset in TIME_OFFSETS.values()) + WINDOW_DAYS
    collection = _s2_collection(center.advance(-reach, "day"), center.advance(reach, "day"), region)
    newest = collection.sort("system:time_start", False)
    peak = newest.aggregate_max("system:time_start")
    millis = ee.Number(ee.List([peak, 0]).reduce(ee.Reducer.firstNonNull()))
    latest = _latest_image(collection, _s2_dummy(region))
    return ee.Image.constant(millis).toDouble().rename("scene_millis").updateMask(latest.select("B8").mask())


def stacked_image(year: int, month: int, region):
    ref = ee.Date.fromYMD(year, month, 15)
    stacked = None
    for suffix, offset in TIME_OFFSETS.items():
        day = ref.advance(offset, "day")
        combo = s2_composite(day, region).addBands(s1_composite(day, region)).select(ALL_BANDS)
        renamed = combo.rename([f"{band}_{suffix}" for band in ALL_BANDS])
        stacked = renamed if stacked is None else stacked.addBands(renamed)
    return stacked.addBands(_scene_millis_band(year, month, region))


def scene_date_from_millis(value) -> date | None:
    if value is None:
        return None
    millis = float(value)
    if not math.isfinite(millis) or millis <= 0:
        return None
    return datetime.fromtimestamp(millis / 1000.0, timezone.utc).date()


def gee_extract(usable: list[dict], year: int, month: int, feature_cols: list[str]):
    reducer = ee.Reducer.mean().combine(ee.Reducer.stdDev(), sharedInputs=True)
    chunks = math.ceil(len(usable) / CHUNK_SIZE)
    rows = [None] * len(usable)
    errors = 0
    for chunk_index in range(chunks):
        chunk = usable[chunk_index * CHUNK_SIZE : (chunk_index + 1) * CHUNK_SIZE]
        features = [
            ee.Feature(ee.Geometry.Polygon([item["pts"]]), {"idx": index})
            for index, item in enumerate(chunk)
        ]
        collection = ee.FeatureCollection(features)
        region = collection.geometry().bounds()
        image = stacked_image(year, month, region)
        stats_fc = image.reduceRegions(collection=collection, reducer=reducer, scale=SCALE, tileScale=4)
        stats = []
        for attempt in range(5):
            try:
                stats = stats_fc.getInfo()["features"]
                break
            except Exception as exc:
                message = str(exc).lower()
                if any(token in message for token in ("too many requests", "concurrency", "quota", "rate", "429")):
                    wait = 10 * (2 ** attempt)
                    print(f"    GEE quota, retry in {wait}s ({attempt + 1}/5)", flush=True)
                    time.sleep(wait)
                else:
                    print(f"    GEE error chunk {chunk_index}: {exc}", flush=True)
                    errors += 1
                    break
        for feature in stats:
            props = dict(feature["properties"])
            index = int(float(props.pop("idx", 0)))
            global_index = chunk_index * CHUNK_SIZE + index
            if 0 <= global_index < len(usable):
                row = {column: props.get(column) for column in feature_cols}
                row["area_m2"] = usable[global_index]["computed_area_m2"]
                row["month_sin"] = math.sin(2 * math.pi * month / 12)
                row["month_cos"] = math.cos(2 * math.pi * month / 12)
                row["scene_date"] = scene_date_from_millis(props.get("scene_millis_mean"))
                rows[global_index] = row
        if chunk_index < chunks - 1:
            time.sleep(CHUNK_DELAY)
    return rows, errors


def load_models():
    full = joblib.load(SCRIPT_DIR / "crop_classifier_model_full.joblib")
    sugarcane = joblib.load(SCRIPT_DIR / "crop_classifier_model_sugarcane_xgb.joblib")
    if set(full["feature_cols"]) != set(sugarcane["feature_cols"]):
        raise RuntimeError("Feature mismatch between the two models")
    return full, sugarcane


def load_village(name: str) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, field_id, district, sub_dist, village_name, area_ha, ST_AsGeoJSON(geom) AS geojson
                FROM field_boundaries
                WHERE village_name = :village
                ORDER BY id
                """
            ),
            {"village": name},
        ).mappings().all()
    plots = []
    for row in rows:
        points = largest_ring(row["geojson"]) if row["geojson"] else []
        plots.append({
            "field_boundary_id": int(row["id"]),
            "field_id": None if row["field_id"] is None else int(row["field_id"]),
            "district": row["district"],
            "subdistrict": row["sub_dist"],
            "village": row["village_name"],
            "area_ha": float(row["area_ha"]) if row["area_ha"] is not None else None,
            "pts": points,
        })
    return plots


def classify_village(plots, year, full, sugarcane):
    usable = []
    rejected = []
    broken = []
    for plot in plots:
        points = plot["pts"]
        if len(points) < 3:
            broken.append(plot["field_boundary_id"])
            continue
        area = poly_area_m2(points)
        if area < MIN_AREA_M2 or is_tree(points):
            rejected.append(plot["field_boundary_id"])
            continue
        plot["computed_area_m2"] = area
        usable.append(plot)
    if not usable:
        return [], set(), set(rejected), set(broken)

    feature_cols = full["feature_cols"]
    september, _sep_errors = gee_extract(usable, year, 9, feature_cols)
    november, _nov_errors = gee_extract(usable, year, 11, feature_cols)
    valid = [
        index for index in range(len(usable))
        if september[index] is not None or november[index] is not None
    ]
    failed = {
        usable[index]["field_boundary_id"]
        for index in range(len(usable))
        if index not in valid
    }
    if not valid:
        return [], failed, set(rejected), set(broken)

    labels_full = full["label_names"]
    labels_sc = sugarcane["label_names"]
    sugarcane_index = labels_sc.index("Sugarcane")
    empty = {column: None for column in feature_cols}
    september_rows = [september[index] if september[index] is not None else empty for index in valid]
    november_rows = [november[index] if november[index] is not None else empty for index in valid]
    median_full = pd.Series(full["imputer"].statistics_, index=full["feature_cols"])
    median_sc = pd.Series(sugarcane["imputer"].statistics_, index=sugarcane["feature_cols"])
    full_frame = pd.DataFrame(september_rows)[full["feature_cols"]].fillna(median_full).infer_objects(copy=False).astype(float)
    sc_frame = pd.DataFrame(november_rows)[sugarcane["feature_cols"]].fillna(median_sc).infer_objects(copy=False).astype(float)
    sc_codes = sugarcane["model"].predict(sc_frame)
    sc_proba = sugarcane["model"].predict_proba(sc_frame)
    full_codes = full["model"].predict(full_frame)
    full_proba = full["model"].predict_proba(full_frame)

    stored = []
    for position, index in enumerate(valid):
        plot = usable[index]
        area_m2 = float(plot["computed_area_m2"])
        sc_prob = float(sc_proba[position][sugarcane_index])
        sc_sorted = sorted(sc_proba[position], reverse=True)
        sc_margin = float(sc_sorted[0] - sc_sorted[1])
        if (
            int(sc_codes[position]) == sugarcane_index
            and sc_prob >= CONFIDENCE_THRESHOLD
            and sc_margin >= MARGIN_THRESHOLD
            and november[index] is not None
        ):
            final_label = "Sugarcane"
            predicted = "Sugarcane"
            confidence = round(sc_prob, 4)
            margin = round(sc_margin, 4)
            source = "sugarcane_xgb"
            observed = (november[index] or {}).get("scene_date") or date(year, 11, 15)
        else:
            code = int(full_codes[position])
            top = float(full_proba[position][code])
            ordered = sorted(full_proba[position], reverse=True)
            margin_full = float(ordered[0] - ordered[1])
            predicted = labels_full[code]
            if (
                predicted in {"Rice", "Cotton"}
                and top >= CONFIDENCE_THRESHOLD
                and margin_full >= MARGIN_THRESHOLD
                and september[index] is not None
            ):
                final_label = predicted
            else:
                final_label = "Uncertain"
            confidence = round(top, 4)
            margin = round(margin_full, 4)
            source = "full_7class"
            observed = (september[index] or {}).get("scene_date") or date(year, 9, 15)
        if final_label not in KEEP:
            continue
        points = plot["pts"]
        stored.append({
            "field_boundary_id": plot["field_boundary_id"],
            "field_id": plot["field_id"],
            "district_name": plot["district"],
            "subdistrict_name": plot["subdistrict"],
            "village_name": plot["village"],
            "season": f"Kharif {year}",
            "observation_date": observed,
            "identified_on": date.today(),
            "predicted_crop": predicted,
            "final_label": final_label,
            "is_rice": 1 if final_label == "Rice" else 0,
            "confidence": confidence,
            "margin": margin,
            "area_ha": round(area_m2 / 10000, 4),
            "source_model": source,
            "centroid_lon": round(sum(point[0] for point in points) / len(points), 6),
            "centroid_lat": round(sum(point[1] for point in points) / len(points), 6),
        })
    return stored, failed, set(rejected), set(broken)


def mysql_errno(exc: BaseException) -> int | None:
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    return None


def acquire_lock(lock_path: Path, stale_seconds: float = 180) -> int:
    while True:
        try:
            return os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age > stale_seconds:
                lock_path.unlink(missing_ok=True)
                continue
            time.sleep(0.2)


def write_village(year: int, village: str, stored: list[dict], failed: set[int], rejected: set[int], broken: set[int]) -> None:
    season = f"Kharif {year}"
    kept = {row["field_boundary_id"] for row in stored}
    protect = failed | broken
    ordered = sorted(stored, key=lambda row: row["field_boundary_id"])
    with engine.begin() as conn:
        existing = {
            int(value)
            for value in conn.execute(
                text(
                    """
                    SELECT field_boundary_id
                    FROM crop_identification
                    WHERE season = :season AND village_name = :village AND field_boundary_id IS NOT NULL
                    """
                ),
                {"season": season, "village": village},
            ).scalars()
        }
        for start in range(0, len(ordered), 200):
            conn.execute(UPSERT, ordered[start : start + 200])
        remove = sorted(value for value in existing if value not in kept and value not in protect)
        for start in range(0, len(remove), 400):
            chunk = remove[start : start + 400]
            conn.execute(
                text(
                    """
                    DELETE FROM crop_identification
                    WHERE season = :season AND field_boundary_id IN :ids
                    """
                ).bindparams(bindparam("ids", expanding=True)),
                {"season": season, "ids": chunk},
            )
        print(
            f"  stored {len(stored)}  deleted {len(remove)}  kept-on-failure {len(protect & existing)}  rejected {len(rejected)}",
            flush=True,
        )


def apply_village(year: int, village: str, stored: list[dict], failed: set[int], rejected: set[int], broken: set[int]) -> None:
    lock_path = SCRIPT_DIR / "identification_db.lock"
    fd = acquire_lock(lock_path)
    try:
        for attempt in range(1, 6):
            try:
                write_village(year, village, stored, failed, rejected, broken)
                return
            except OperationalError as exc:
                if mysql_errno(exc) not in (1205, 1213) or attempt == 5:
                    raise
                print(f"  {village} database lock, retry {attempt}", flush=True)
                time.sleep(0.5 * attempt)
    finally:
        os.close(fd)
        lock_path.unlink(missing_ok=True)


def with_progress_lock(progress_path: Path, update):
    lock_path = progress_path.with_suffix(".lock")
    while True:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            break
        except FileExistsError:
            time.sleep(0.1)
    try:
        progress = json.loads(progress_path.read_text(encoding="utf-8")) if progress_path.exists() else {}
        result = update(progress)
        progress_path.write_text(json.dumps(progress), encoding="utf-8")
        return result
    finally:
        os.close(fd)
        lock_path.unlink(missing_ok=True)


def claim_village(progress_path: Path, year: int, names: list[str]) -> str | None:
    def update(progress: dict) -> str | None:
        done = set(progress.get(str(year), []))
        busy = set(progress.get(f"busy-{year}", []))
        for name in names:
            if name not in done and name not in busy:
                busy.add(name)
                progress[f"busy-{year}"] = sorted(busy)
                return name
        return None

    return with_progress_lock(progress_path, update)


def finish_village(progress_path: Path, year: int, name: str) -> None:
    def update(progress: dict) -> None:
        done = set(progress.get(str(year), []))
        done.add(name)
        progress[str(year)] = sorted(done)
        busy = set(progress.get(f"busy-{year}", []))
        busy.discard(name)
        progress[f"busy-{year}"] = sorted(busy)

    with_progress_lock(progress_path, update)


def release_village(progress_path: Path, year: int, name: str) -> None:
    def update(progress: dict) -> None:
        busy = set(progress.get(f"busy-{year}", []))
        busy.discard(name)
        progress[f"busy-{year}"] = sorted(busy)

    with_progress_lock(progress_path, update)


def release_busy(progress_path: Path) -> None:
    if not progress_path.exists():
        return

    def update(progress: dict) -> None:
        for key in list(progress):
            if str(key).startswith("busy-"):
                progress[key] = []

    with_progress_lock(progress_path, update)


def village_names(only: str | None) -> list[str]:
    if only:
        return [only]
    with engine.connect() as conn:
        return list(conn.execute(text(
            "SELECT village_name FROM field_boundaries GROUP BY village_name ORDER BY village_name"
        )).scalars())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, action="append", dest="years")
    parser.add_argument("--village", default=None)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    years = args.years or [2025, 2024]
    if args.workers > 1 and not args.village:
        progress_path = SCRIPT_DIR / "identification_progress.json"
        release_busy(progress_path)
        (SCRIPT_DIR / "identification_db.lock").unlink(missing_ok=True)
        processes = []
        for _slot in range(args.workers):
            command = [sys.executable, "-u", str(Path(__file__).resolve())]
            for year in years:
                command.extend(["--year", str(year)])
            processes.append(subprocess.Popen(command))
        raise SystemExit(max(process.wait() for process in processes))
    print(f"Earth Engine project: {GEE_PROJECT}", flush=True)
    ee.Initialize(project=GEE_PROJECT)
    full, sugarcane = load_models()
    print(f"Full model: {full['label_names']}", flush=True)
    print(f"Sugarcane model: {sugarcane['label_names']}", flush=True)
    names = village_names(args.village)
    progress_path = SCRIPT_DIR / "identification_progress.json"
    pd.set_option("future.no_silent_downcasting", True)
    for year in years:
        total = len(names)
        print(f"Kharif {year}: worker waiting for the next village of {total}", flush=True)
        while True:
            name = claim_village(progress_path, year, names)
            if name is None:
                break
            started = time.time()
            print(f"[{year}] {name}", flush=True)
            plots = load_village(name)
            stored, failed, rejected, broken = classify_village(plots, year, full, sugarcane)
            try:
                apply_village(year, name, stored, failed, rejected, broken)
            except OperationalError as exc:
                if mysql_errno(exc) not in (1205, 1213):
                    raise
                print(f"  {name} database lock persisted; this village will be retried", flush=True)
                release_village(progress_path, year, name)
                continue
            finish_village(progress_path, year, name)
            print(f"  {name} finished in {round(time.time() - started)}s", flush=True)
    print("Identification update finished", flush=True)


if __name__ == "__main__":
    main()
