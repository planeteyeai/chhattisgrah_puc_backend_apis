"""Load all_crops_villages KMLs into crop_identification.

Each placemark is matched to the field_boundaries polygon that contains its
centroid. The stored village, district, and field id come from that polygon.
Rows are upserted on (field_boundary_id, season).

Kharif 2024, 2025, and 2026 rows are not updated or deleted. The table is not
altered.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

from shapely.geometry import Point, shape
from shapely.strtree import STRtree
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import engine  # noqa: E402

KML_DIR = ROOT / "all_crops_villages"
DATA_RE = re.compile(r'<Data name="([^"]+)"><value>([^<]*)</value>')
YEAR_RE = re.compile(r"(20\d{2})")

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


def parse_confidence(raw: str) -> float | None:
    text_value = raw.strip()
    if not text_value:
        return None
    if text_value.endswith("%"):
        return round(float(text_value[:-1]) / 100.0, 4)
    value = float(text_value)
    if value > 1:
        return round(value / 100.0, 4)
    return round(value, 4)


def observation_date_for(season: str) -> date:
    match = YEAR_RE.search(season)
    year = int(match.group(1)) if match else date.today().year
    return date(year, 9, 15)


def iter_placemarks(path: Path):
    data = path.read_text(encoding="utf-8")
    for chunk in data.split("<Placemark>"):
        end = chunk.find("</ExtendedData>")
        if end < 0:
            continue
        fields = dict(DATA_RE.findall(chunk[:end]))
        if fields.get("field_id"):
            yield fields


def load_admin_boxes() -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT village_name, min_lon, min_lat, max_lon, max_lat
                FROM kawardha_data
                WHERE level = 'village'
                  AND min_lon IS NOT NULL AND max_lon IS NOT NULL
                  AND min_lat IS NOT NULL AND max_lat IS NOT NULL
                """
            )
        ).mappings().all()
    merged: dict[str, dict] = {}
    for row in rows:
        name = row["village_name"]
        if not name:
            continue
        box = merged.get(name)
        if box is None:
            merged[name] = {
                "village_name": name,
                "min_lon": float(row["min_lon"]),
                "min_lat": float(row["min_lat"]),
                "max_lon": float(row["max_lon"]),
                "max_lat": float(row["max_lat"]),
            }
            continue
        box["min_lon"] = min(box["min_lon"], float(row["min_lon"]))
        box["min_lat"] = min(box["min_lat"], float(row["min_lat"]))
        box["max_lon"] = max(box["max_lon"], float(row["max_lon"]))
        box["max_lat"] = max(box["max_lat"], float(row["max_lat"]))
    return list(merged.values())


def overlapping_villages(boxes: list[dict], min_lon: float, min_lat: float, max_lon: float, max_lat: float) -> list[str]:
    names = []
    for box in boxes:
        if box["max_lon"] < min_lon or box["min_lon"] > max_lon:
            continue
        if box["max_lat"] < min_lat or box["min_lat"] > max_lat:
            continue
        names.append(box["village_name"])
    return names


def parse_field(row) -> dict | None:
    try:
        geom = shape(json.loads(row["geom_geojson"]))
        if not geom.is_valid:
            geom = geom.buffer(0)
        if geom.is_empty:
            return None
    except Exception:
        return None
    return {
        "geom": geom,
        "id": int(row["id"]),
        "field_id": None if row["field_id"] is None else int(row["field_id"]),
        "district": row["district"],
        "sub_dist": row["sub_dist"],
        "village_name": row["village_name"],
        "area_ha": float(row["area_ha"]) if row["area_ha"] is not None else None,
    }


def load_fields_for_villages(names: set[str]) -> dict[str, list[dict]]:
    """Read field polygons by primary-key ranges so each row is scanned once."""
    grouped = {name: [] for name in names}
    if not names:
        return grouped
    with engine.connect() as conn:
        min_id, max_id = conn.execute(text("SELECT MIN(id), MAX(id) FROM field_boundaries")).one()
    if min_id is None:
        return grouped
    step = 4000
    cursor = int(min_id)
    last = int(max_id)
    loaded = 0
    while cursor <= last:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT id, field_id, district, sub_dist, village_name, area_ha, geom_geojson
                    FROM field_boundaries
                    WHERE id >= :start AND id < :stop AND geom_geojson IS NOT NULL
                    """
                ),
                {"start": cursor, "stop": cursor + step},
            ).mappings().all()
        kept = 0
        for row in rows:
            if row["village_name"] not in names:
                continue
            field = parse_field(row)
            if field is None:
                continue
            grouped[row["village_name"]].append(field)
            kept += 1
        loaded += kept
        print(f"  fields id {cursor}-{cursor + step - 1}: kept {kept}, total {loaded}", flush=True)
        cursor += step
    return grouped


def choose_hit(hits: list[dict], field_id: int, area_ha: float | None) -> dict | None:
    if not hits:
        return None
    if len(hits) == 1:
        return hits[0]
    same_id = [hit for hit in hits if hit["field_id"] == field_id]
    if len(same_id) == 1:
        return same_id[0]
    pool = same_id or hits
    if area_ha is None:
        return None

    def distance(hit: dict) -> float:
        other = hit["area_ha"] or 0.0
        if other <= 0:
            return 999.0
        return abs(other - area_ha) / max(other, area_ha)

    ranked = sorted(pool, key=distance)
    if distance(ranked[0]) > 0.5:
        return None
    return ranked[0]


def collect(boxes: list[dict]) -> tuple[list[dict], dict]:
    stats = {
        "files": 0,
        "placemarks": 0,
        "ready": 0,
        "unmatched": 0,
        "ambiguous": 0,
        "duplicate_field": 0,
        "rejected": 0,
        "villages_loaded": 0,
    }
    rows: list[dict] = []
    seen: dict[tuple[int, str], dict] = {}
    identified_on = date.today()
    files: list[tuple[Path, list, list[str]]] = []
    needed: set[str] = set()

    for path in sorted(KML_DIR.glob("*.kml")):
        stats["files"] += 1
        placemarks = []
        min_lon = min_lat = 999.0
        max_lon = max_lat = -999.0
        for fields in iter_placemarks(path):
            stats["placemarks"] += 1
            try:
                lon = float(fields["centroid_lon"])
                lat = float(fields["centroid_lat"])
                field_id = int(fields["field_id"])
                area = float(fields["area_ha"]) if fields.get("area_ha") else None
            except (KeyError, ValueError):
                stats["rejected"] += 1
                continue
            min_lon = min(min_lon, lon)
            min_lat = min(min_lat, lat)
            max_lon = max(max_lon, lon)
            max_lat = max(max_lat, lat)
            placemarks.append((fields, field_id, lon, lat, area))
        names = overlapping_villages(boxes, min_lon, min_lat, max_lon, max_lat) if placemarks else []
        needed.update(names)
        files.append((path, placemarks, names))
        print(f"read {path.name}: {len(placemarks)} placemarks, {len(names)} candidate villages", flush=True)

    print(f"loading polygons for {len(needed)} villages", flush=True)
    cache = load_fields_for_villages(needed)
    stats["villages_loaded"] = len(needed)

    for path, placemarks, names in files:
        if not placemarks:
            print(f"{path.name}: no placemarks", flush=True)
            continue
        candidates = [field for name in names for field in cache.get(name, [])]
        if not candidates:
            stats["unmatched"] += len(placemarks)
            print(f"{path.name}: no candidate fields", flush=True)
            continue
        tree = STRtree([field["geom"] for field in candidates])
        matched = 0
        for fields, field_id, lon, lat, area in placemarks:
            indexes = tree.query(Point(lon, lat), predicate="intersects")
            hits = [candidates[int(index)] for index in indexes]
            boundary = choose_hit(hits, field_id, area)
            if boundary is None:
                if len(hits) > 1:
                    stats["ambiguous"] += 1
                else:
                    stats["unmatched"] += 1
                continue
            built = build_row(fields, boundary, area, identified_on)
            if built is None:
                stats["rejected"] += 1
                continue
            key = (built["field_boundary_id"], built["season"])
            previous = seen.get(key)
            if previous is not None:
                stats["duplicate_field"] += 1
                if (built["confidence"] or 0) > (previous["confidence"] or 0):
                    seen[key] = built
                continue
            seen[key] = built
            matched += 1
        print(f"{path.name}: matched {matched} of {len(placemarks)}", flush=True)
    rows = list(seen.values())
    stats["ready"] = len(rows)
    return rows, stats


def build_row(fields: dict[str, str], boundary: dict, area: float | None, identified_on: date) -> dict | None:
    crop = fields.get("crop", "").strip()
    season = fields.get("season", "").strip()
    source = fields.get("source", "").strip() or "kml"
    if not crop or not season or not boundary.get("district"):
        return None
    if len(crop) > 64 or len(season) > 32 or len(source) > 64:
        return None
    try:
        confidence = parse_confidence(fields.get("confidence", ""))
        lon = round(float(fields["centroid_lon"]), 6)
        lat = round(float(fields["centroid_lat"]), 6)
    except ValueError:
        return None
    return {
        "field_boundary_id": boundary["id"],
        "field_id": boundary["field_id"],
        "district_name": boundary["district"],
        "subdistrict_name": boundary["sub_dist"],
        "village_name": boundary["village_name"],
        "season": season,
        "observation_date": observation_date_for(season),
        "identified_on": identified_on,
        "predicted_crop": crop,
        "final_label": crop,
        "is_rice": 1 if crop == "Rice" else 0,
        "confidence": confidence,
        "margin": None,
        "area_ha": None if area is None else round(area, 4),
        "source_model": source,
        "centroid_lon": lon,
        "centroid_lat": lat,
    }


def existing_season_counts() -> list[tuple]:
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    """
                    SELECT season, source_model, COUNT(*) n
                    FROM crop_identification
                    GROUP BY season, source_model
                    ORDER BY season, source_model
                    """
                )
            ).all()
        )


def write_rows(rows: list[dict]) -> None:
    protected = {"Kharif 2024", "Kharif 2025", "Kharif 2026"}
    seasons = {row["season"] for row in rows}
    blocked = sorted(seasons & protected)
    if blocked:
        raise SystemExit(f"Refusing to write seasons that already belong to the model run: {blocked}")
    by_village: dict[str, list[dict]] = {}
    for row in rows:
        by_village.setdefault(row["village_name"], []).append(row)
    written = 0
    for village, village_rows in sorted(by_village.items()):
        ordered = sorted(village_rows, key=lambda item: (item["season"], item["field_boundary_id"]))
        for attempt in range(1, 6):
            try:
                with engine.begin() as conn:
                    for start in range(0, len(ordered), 200):
                        conn.execute(UPSERT, ordered[start : start + 200])
                written += len(ordered)
                print(f"  stored {village}: {len(ordered)}", flush=True)
                break
            except OperationalError as exc:
                if attempt == 5:
                    raise
                print(f"  {village} database retry {attempt}: {exc}", flush=True)
    print(f"stored {written} rows", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Write rows. Without this, only report.")
    args = parser.parse_args()
    if not KML_DIR.is_dir():
        raise SystemExit(f"Missing folder: {KML_DIR}")
    print("seasons before:", existing_season_counts(), flush=True)
    boxes = load_admin_boxes()
    print(f"admin villages {len(boxes)}", flush=True)
    rows, stats = collect(boxes)
    seasons: dict[str, int] = {}
    for row in rows:
        seasons[row["season"]] = seasons.get(row["season"], 0) + 1
    print("ready seasons:", seasons, flush=True)
    print("stats:", stats, flush=True)
    matched = stats["ready"] + stats["duplicate_field"]
    if stats["placemarks"] and matched / stats["placemarks"] < 0.8:
        raise SystemExit("Match rate is below 80 percent, so nothing was written.")
    if not args.apply:
        print("dry run only", flush=True)
        return
    write_rows(rows)
    print("seasons after:", existing_season_counts(), flush=True)


if __name__ == "__main__":
    main()
