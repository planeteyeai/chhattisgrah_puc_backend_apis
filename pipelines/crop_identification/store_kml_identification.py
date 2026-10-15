"""Load village KMLs from data/all_crops_villages into crop_identification.

The KML village name is not the cadastral village. Each placemark is matched
to a field_boundaries row with the same field_id in a village whose extent
covers the placemark centroid. When several villages share that field_id, the
field whose first vertex lies nearest the centroid is used. The stored
village, district, and field id come from that row. Rows are upserted on
(field_boundary_id, season).

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

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app.database import engine  # noqa: E402

KML_DIR = ROOT / "data" / "all_crops_villages"
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


BOX_PAD = 0.01


def villages_at(boxes: list[dict], lon: float, lat: float) -> list[str]:
    names = []
    for box in boxes:
        if box["max_lon"] + BOX_PAD < lon or box["min_lon"] - BOX_PAD > lon:
            continue
        if box["max_lat"] + BOX_PAD < lat or box["min_lat"] - BOX_PAD > lat:
            continue
        names.append(box["village_name"])
    return names


def load_field_index() -> dict[tuple[str, int], list[dict]]:
    """Field identity only. Polygons are loaded later for the few ambiguous plots."""
    with engine.connect() as conn:
        min_id, max_id = conn.execute(text("SELECT MIN(id), MAX(id) FROM field_boundaries")).one()
    index: dict[tuple[str, int], list[dict]] = {}
    if min_id is None:
        return index
    step = 8000
    cursor = int(min_id)
    last = int(max_id)
    loaded = 0
    while cursor <= last:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT id, field_id, district, sub_dist, village_name, area_ha,
                           SUBSTRING(geom_geojson, 1, 160) AS head
                    FROM field_boundaries
                    WHERE id >= :start AND id < :stop
                    """
                ),
                {"start": cursor, "stop": cursor + step},
            ).mappings().all()
        for row in rows:
            if row["field_id"] is None or not row["village_name"] or not row["district"]:
                continue
            field = {
                "id": int(row["id"]),
                "field_id": int(row["field_id"]),
                "district": row["district"],
                "sub_dist": row["sub_dist"],
                "village_name": row["village_name"],
                "area_ha": float(row["area_ha"]) if row["area_ha"] is not None else None,
                "vertex": first_vertex(row["head"]),
            }
            index.setdefault((field["village_name"], field["field_id"]), []).append(field)
            loaded += 1
        print(f"  identity id {cursor}-{cursor + step - 1}: total {loaded}", flush=True)
        cursor += step
    return index


VERTEX_RE = re.compile(r"\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)")
MAX_VERTEX_DEG = 0.005


def first_vertex(head: str | None) -> tuple[float, float] | None:
    if not head:
        return None
    match = VERTEX_RE.search(head)
    if not match:
        return None
    return float(match.group(1)), float(match.group(2))


def nearest_hit(hits: list[dict], lon: float, lat: float) -> dict | None:
    """A field's first vertex sits within a few hundred metres of its centroid."""
    ranked = []
    for hit in hits:
        vertex = hit.get("vertex")
        if vertex is None:
            continue
        ranked.append(((vertex[0] - lon) ** 2 + (vertex[1] - lat) ** 2, hit))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0])
    best_d, best = ranked[0]
    if best_d ** 0.5 > MAX_VERTEX_DEG:
        return None
    if len(ranked) > 1 and ranked[1][0] ** 0.5 <= MAX_VERTEX_DEG:
        return None
    return best


def remember(seen: dict[tuple[int, str], dict], stats: dict, built: dict) -> None:
    key = (built["field_boundary_id"], built["season"])
    previous = seen.get(key)
    if previous is not None:
        stats["duplicate_field"] += 1
        if (built["confidence"] or 0) > (previous["confidence"] or 0):
            seen[key] = built
        return
    seen[key] = built


def kml_sources():
    for path in sorted(KML_DIR.glob("*.kml")):
        yield path.name, iter_placemarks(path)


def progress_sources(path: Path):
    """Results saved by kabirdham_all_crops_run.py, in the same shape as KML placemarks."""
    data = json.loads(path.read_text(encoding="utf-8"))
    for village, result in sorted((data.get("results") or {}).items()):
        placemarks = []
        for item in result.get("predictions") or []:
            placemarks.append({
                "field_id": str(item.get("field_id") or ""),
                "village": item.get("village") or village,
                "crop": item.get("final_label") or "",
                "confidence": "" if item.get("confidence") is None else str(item["confidence"]),
                "source": item.get("source_model") or "",
                "area_ha": "" if item.get("area_ha") is None else str(item["area_ha"]),
                "centroid_lon": str(item.get("centroid_lon", "")),
                "centroid_lat": str(item.get("centroid_lat", "")),
                "season": item.get("season") or "",
            })
        yield village, placemarks


def collect(boxes: list[dict], sources) -> tuple[list[dict], dict]:
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
    seen: dict[tuple[int, str], dict] = {}
    identified_on = date.today()
    files: list[tuple[str, list]] = []

    for name, items in sources:
        stats["files"] += 1
        placemarks = []
        for fields in items:
            if not fields.get("field_id"):
                continue
            stats["placemarks"] += 1
            try:
                lon = float(fields["centroid_lon"])
                lat = float(fields["centroid_lat"])
                field_id = int(fields["field_id"])
                area = float(fields["area_ha"]) if fields.get("area_ha") else None
            except (KeyError, ValueError):
                stats["rejected"] += 1
                continue
            placemarks.append((fields, field_id, lon, lat, area))
        files.append((name, placemarks))
        print(f"read {name}: {len(placemarks)} placemarks", flush=True)

    print("loading field identity", flush=True)
    index = load_field_index()
    print(f"identity keys {len(index)}", flush=True)

    for name, placemarks in files:
        matched = 0
        for fields, field_id, lon, lat, area in placemarks:
            hits = []
            for village in villages_at(boxes, lon, lat):
                hits.extend(index.get((village, field_id), []))
            unique = {hit["id"]: hit for hit in hits}
            hits = list(unique.values())
            if not hits:
                stats["unmatched"] += 1
                continue
            boundary = nearest_hit(hits, lon, lat)
            if boundary is None:
                stats["ambiguous"] += 1
                continue
            built = build_row(fields, boundary, area, identified_on)
            if built is None:
                stats["rejected"] += 1
                continue
            remember(seen, stats, built)
            matched += 1
        print(f"{name}: matched {matched} of {len(placemarks)}", flush=True)

    stats["ready"] = len(seen)
    return list(seen.values()), stats


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
    parser.add_argument("--progress", type=Path, default=None, help="Read kabirdham_allcrops_progress.json instead of KML files.")
    args = parser.parse_args()
    if args.progress is not None:
        if not args.progress.is_file():
            raise SystemExit(f"Missing file: {args.progress}")
        sources = progress_sources(args.progress)
    else:
        if not KML_DIR.is_dir():
            raise SystemExit(f"Missing folder: {KML_DIR}")
        sources = kml_sources()
    print("seasons before:", existing_season_counts(), flush=True)
    boxes = load_admin_boxes()
    print(f"admin villages {len(boxes)}", flush=True)
    rows, stats = collect(boxes, sources)
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
