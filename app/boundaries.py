import json
import re
from collections.abc import Iterator
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.database import engine
from app.geojson import json_safe

router = APIRouter(tags=["boundaries"])

BLOCK = re.compile(r"^r(\d+)c(\d+)$", re.I)


class BoundaryRequest(BaseModel):
    district_name: str = Field(..., examples=["Chhattisgarh"])
    subdistrict_name: str | None = Field(None, examples=["r5c1"])
    village_name: str | None = Field(None, examples=["Chhattisgarh"])
    year: int | None = Field(None, ge=1900, le=2100)
    month: int | None = Field(None, ge=1, le=12)


class NameRequest(BaseModel):
    district_name: str = Field(..., examples=["Chhattisgarh"])
    subdistrict_name: str | None = Field(None, examples=["r5c1"])


def clean(value: str | None) -> str | None:
    text_value = re.sub(r"\s+", " ", (value or "").strip())
    return text_value or None


def same_name(left: str | None, right: str | None) -> bool:
    return clean(left).casefold() == clean(right).casefold() if left and right else False


def block_sort(name: str) -> tuple[int, int, str]:
    match = BLOCK.fullmatch(name or "")
    if not match:
        return 999, 999, name or ""
    return int(match.group(1)), int(match.group(2)), ""


def is_block(name: str | None) -> bool:
    return bool(name and BLOCK.fullmatch(name.strip()))


def parse_block(name: str) -> tuple[int, int]:
    match = BLOCK.fullmatch(name.strip())
    if not match:
        raise HTTPException(
            status_code=400,
            detail="subdistrict_name must look like r5c1",
        )
    return int(match.group(1)), int(match.group(2))


def require_places(district_name: str | None, subdistrict_name: str | None, village_name: str | None) -> tuple[str, str | None, str | None]:
    district_name = clean(district_name)
    subdistrict_name = clean(subdistrict_name)
    village_name = clean(village_name)
    if not district_name:
        raise HTTPException(status_code=400, detail="district_name is required")
    if village_name and not subdistrict_name:
        raise HTTPException(status_code=400, detail="subdistrict_name is required when village_name is provided")
    return district_name, subdistrict_name, village_name


def convex_hull(points: list[tuple[float, float]]) -> dict[str, Any] | None:
    unique = sorted(set(points))
    if len(unique) < 3:
        return None

    def cross(origin: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        return (a[0] - origin[0]) * (b[1] - origin[1]) - (a[1] - origin[1]) * (b[0] - origin[0])

    lower: list[tuple[float, float]] = []
    for point in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    upper: list[tuple[float, float]] = []
    for point in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    ring = lower[:-1] + upper[:-1]
    ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [[[lon, lat] for lon, lat in ring]]}


def feature(feature_id: str, geometry: dict[str, Any] | None, properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "Feature", "id": feature_id, "geometry": geometry, "properties": properties}


def load_blocks(district_name: str) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT
                  c.id,
                  c.chunk_row,
                  c.chunk_col,
                  c.segment_count,
                  c.min_lon,
                  c.min_lat,
                  c.max_lon,
                  c.max_lat,
                  (
                    SELECT f.sub_dist FROM field_boundaries f
                    WHERE f.chunk_id = c.id LIMIT 1
                  ) AS sub_dist,
                  (
                    SELECT f.district FROM field_boundaries f
                    WHERE f.chunk_id = c.id LIMIT 1
                  ) AS district
                FROM region_chunks c
                WHERE c.segment_count > 0
                """
            )
        ).mappings().all()
    blocks = []
    for row in rows:
        if not row["sub_dist"] or not row["district"]:
            continue
        if not same_name(row["district"], district_name):
            continue
        blocks.append(
            {
                "chunk_id": int(row["id"]),
                "chunk_row": int(row["chunk_row"]),
                "chunk_col": int(row["chunk_col"]),
                "field_count": int(row["segment_count"] or 0),
                "subdistrict_name": row["sub_dist"],
                "district_name": row["district"],
                "min_lon": float(row["min_lon"]),
                "min_lat": float(row["min_lat"]),
                "max_lon": float(row["max_lon"]),
                "max_lat": float(row["max_lat"]),
            }
        )
    if not blocks:
        raise HTTPException(status_code=404, detail=f"No stored fields found for district={district_name!r}")
    blocks.sort(key=lambda item: block_sort(item["subdistrict_name"]))
    return blocks


def find_block(district_name: str, subdistrict_name: str) -> dict[str, Any]:
    row_index, col_index = parse_block(subdistrict_name)
    for block in load_blocks(district_name):
        if block["chunk_row"] == row_index and block["chunk_col"] == col_index:
            return block
    raise HTTPException(
        status_code=404,
        detail=f"No subdistrict {subdistrict_name!r} under district={district_name!r}",
    )


def village_stats(chunk_id: int) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT village_name, MIN(village_id) AS village_id, COUNT(*) AS fields,
                       COALESCE(SUM(area_ha), 0) AS area_ha
                FROM field_boundaries
                WHERE chunk_id = :chunk_id
                GROUP BY village_name
                ORDER BY village_name
                """
            ),
            {"chunk_id": chunk_id},
        ).mappings().all()
    return [
        {
            "village_name": row["village_name"],
            "village_id": int(row["village_id"]) if row["village_id"] is not None else None,
            "fields": int(row["fields"]),
            "area_ha": json_safe(row["area_ha"]),
        }
        for row in rows
    ]


def field_hull(chunk_id: int, village_name: str | None = None) -> dict[str, Any] | None:
    sql = """
        SELECT ST_AsGeoJSON(ST_ConvexHull(ST_Collect(ST_SRID(geom, 0))))
        FROM field_boundaries
        WHERE chunk_id = :chunk_id
    """
    params: dict[str, Any] = {"chunk_id": chunk_id}
    if village_name:
        sql += " AND village_name = :village_name"
        params["village_name"] = village_name
    with engine.connect() as conn:
        raw = conn.execute(text(sql), params).scalar()
    if not raw:
        return None
    return json.loads(raw)


def district_payload(district_name: str) -> dict[str, Any]:
    blocks = load_blocks(district_name)
    stored_name = blocks[0]["district_name"]
    corners: list[tuple[float, float]] = []
    for block in blocks:
        corners.extend(
            (
                (block["min_lon"], block["min_lat"]),
                (block["max_lon"], block["min_lat"]),
                (block["max_lon"], block["max_lat"]),
                (block["min_lon"], block["max_lat"]),
            )
        )
    with engine.connect() as conn:
        field_count = int(
            conn.execute(
                text("SELECT COUNT(id) FROM field_boundaries WHERE district = :district"),
                {"district": stored_name},
            ).scalar_one()
        )
    body = feature(
        f"district-{stored_name}",
        convex_hull(corners),
        {
            "level": "district",
            "district_name": stored_name,
            "subdistrict_count": len(blocks),
            "field_count": field_count,
        },
    )
    return {
        "level": "district",
        "district_name": stored_name,
        "type": "FeatureCollection",
        "count": 1,
        "features": [body],
    }


def subdistrict_payload(district_name: str, subdistrict_name: str) -> dict[str, Any]:
    block = find_block(district_name, subdistrict_name)
    villages = village_stats(block["chunk_id"])
    if not villages:
        raise HTTPException(status_code=404, detail=f"No villages found in subdistrict={block['subdistrict_name']!r}")
    outline = field_hull(block["chunk_id"])
    sub_feature = feature(
        f"subdistrict-{block['subdistrict_name']}",
        outline,
        {
            "level": "subdistrict",
            "district_name": block["district_name"],
            "subdistrict_name": block["subdistrict_name"],
            "village_count": len(villages),
            "field_count": block["field_count"],
        },
    )
    village_features = []
    for village in villages:
        geometry = outline if len(villages) == 1 else field_hull(block["chunk_id"], village["village_name"])
        village_features.append(
            feature(
                f"village-{block['subdistrict_name']}-{village['village_id']}",
                geometry,
                {
                    "level": "village",
                    "district_name": block["district_name"],
                    "subdistrict_name": block["subdistrict_name"],
                    "village_name": village["village_name"],
                    "village_id": village["village_id"],
                    "field_count": village["fields"],
                    "area_ha": village["area_ha"],
                },
            )
        )
    return {
        "level": "subdistrict",
        "district_name": block["district_name"],
        "subdistrict_name": block["subdistrict_name"],
        "subdistrict": sub_feature,
        "villages": {
            "type": "FeatureCollection",
            "count": len(village_features),
            "features": village_features,
        },
    }


def stream_village_fields(district_name: str, subdistrict_name: str, village_name: str) -> StreamingResponse:
    block = find_block(district_name, subdistrict_name)
    villages = village_stats(block["chunk_id"])
    match = next((village for village in villages if same_name(village["village_name"], village_name)), None)
    if match is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No village {village_name!r} in district={block['district_name']!r}, "
                f"subdistrict={block['subdistrict_name']!r}"
            ),
        )
    stored_village = match["village_name"]
    statement = text(
        """
        SELECT f.id, f.field_id, f.village_id, f.village_name, f.sub_dist, f.district,
               f.area_ha, f.`class`, f.geom_geojson, c.final_label, c.predicted_crop, c.is_rice
        FROM field_boundaries f
        LEFT JOIN crop_identification c ON c.field_boundary_id = f.id
        WHERE f.chunk_id = :chunk_id AND f.village_name = :village_name
        ORDER BY f.field_id, f.id
        """
    )
    params = {"chunk_id": block["chunk_id"], "village_name": stored_village}

    def generate() -> Iterator[str]:
        header = {
            "type": "FeatureCollection",
            "level": "fields",
            "district_name": block["district_name"],
            "subdistrict_name": block["subdistrict_name"],
            "village_name": stored_village,
            "count": match["fields"],
        }
        prefix = json.dumps(header, ensure_ascii=False)
        yield prefix[:-1] + ',"features":['
        first = True
        with engine.connect() as conn:
            result = conn.execution_options(stream_results=True).execute(statement, params)
            for row in result:
                geometry = row.geom_geojson or "null"
                properties = {
                    "level": "field",
                    "field_boundary_id": int(row.id),
                    "field_id": int(row.field_id) if row.field_id is not None else None,
                    "village_id": int(row.village_id) if row.village_id is not None else None,
                    "district_name": row.district,
                    "subdistrict_name": row.sub_dist,
                    "village_name": row.village_name,
                    "area_ha": json_safe(row.area_ha),
                    "class": row._mapping["class"],
                    "final_label": row.final_label,
                    "predicted_crop": row.predicted_crop,
                    "is_rice": bool(row.is_rice) if row.is_rice is not None else False,
                }
                piece = '{"type":"Feature","id":' + str(int(row.id)) + ',"geometry":' + geometry
                piece += ',"properties":' + json.dumps(properties, ensure_ascii=False) + "}"
                if not first:
                    yield ","
                first = False
                yield piece
        yield "]}"

    return StreamingResponse(generate(), media_type="application/geo+json")


def stored_subdistricts(district_name: str) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT MIN(district) AS district_name, sub_dist AS subdistrict_name, COUNT(*) AS fields
                FROM field_boundaries
                WHERE district = :district
                GROUP BY sub_dist
                """
            ),
            {"district": district_name},
        ).mappings().all()
    if not rows:
        raise HTTPException(status_code=404, detail=f"No stored fields found for district={district_name!r}")
    return sorted((dict(row) for row in rows), key=lambda row: block_sort(row["subdistrict_name"]))


def stored_villages(district_name: str, subdistrict_name: str) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT village_name, MIN(village_id) AS village_id, COUNT(*) AS fields,
                       COALESCE(SUM(area_ha), 0) AS area_ha
                FROM field_boundaries
                WHERE district = :district AND sub_dist = :subdistrict
                GROUP BY village_name
                ORDER BY village_name
                """
            ),
            {"district": district_name, "subdistrict": subdistrict_name},
        ).mappings().all()
    return [
        {
            "village_name": row["village_name"],
            "village_id": int(row["village_id"]) if row["village_id"] is not None else None,
            "fields": int(row["fields"]),
            "area_ha": json_safe(row["area_ha"]),
        }
        for row in rows
    ]


def admin_villages(district_name: str, subdistrict_name: str) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT village_name, village_code, geom_geojson, min_lon, min_lat, max_lon, max_lat
                FROM kawardha_data
                WHERE level = 'village'
                  AND district_name = :district
                  AND tehsil_name = :subdistrict
                ORDER BY village_name
                """
            ),
            {"district": district_name, "subdistrict": subdistrict_name},
        ).mappings().all()
    unique = []
    seen = set()
    for row in rows:
        key = (row["village_name"] or "").casefold()
        if key in seen:
            continue
        seen.add(key)
        unique.append(dict(row))
    return unique


def named_subdistrict_payload(district_name: str, subdistrict_name: str) -> dict[str, Any]:
    places = stored_subdistricts(district_name)
    block = next((row for row in places if same_name(row["subdistrict_name"], subdistrict_name)), None)
    if block is None:
        raise HTTPException(
            status_code=404,
            detail=f"No subdistrict {subdistrict_name!r} under district={district_name!r}",
        )
    stored_district = block["district_name"]
    stored_subdistrict = block["subdistrict_name"]
    field_rows = {row["village_name"].casefold(): row for row in stored_villages(stored_district, stored_subdistrict)}
    boundaries = admin_villages(stored_district, stored_subdistrict)
    corners: list[tuple[float, float]] = []
    village_features = []
    for boundary in boundaries:
        stats = field_rows.get((boundary["village_name"] or "").casefold(), {})
        corners.extend(
            (
                (float(boundary["min_lon"]), float(boundary["min_lat"])),
                (float(boundary["max_lon"]), float(boundary["min_lat"])),
                (float(boundary["max_lon"]), float(boundary["max_lat"])),
                (float(boundary["min_lon"]), float(boundary["max_lat"])),
            )
        )
        village_features.append(
            feature(
                f"village-{stored_subdistrict}-{boundary['village_code'] or boundary['village_name']}",
                json.loads(boundary["geom_geojson"]),
                {
                    "level": "village",
                    "district_name": stored_district,
                    "subdistrict_name": stored_subdistrict,
                    "village_name": boundary["village_name"],
                    "village_id": int(boundary["village_code"]) if str(boundary["village_code"] or "").isdigit() else stats.get("village_id"),
                    "field_count": stats.get("fields", 0),
                    "area_ha": stats.get("area_ha", 0),
                },
            )
        )
    if not village_features:
        for row in field_rows.values():
            village_features.append(
                feature(
                    f"village-{stored_subdistrict}-{row['village_id']}",
                    field_hull_by_place(stored_district, stored_subdistrict, row["village_name"]),
                    {
                        "level": "village",
                        "district_name": stored_district,
                        "subdistrict_name": stored_subdistrict,
                        "village_name": row["village_name"],
                        "village_id": row["village_id"],
                        "field_count": row["fields"],
                        "area_ha": row["area_ha"],
                    },
                )
            )
    sub_geometry = stored_tehsil_geometry(stored_district, stored_subdistrict) or (convex_hull(corners) if corners else None)
    sub_feature = feature(
        f"subdistrict-{stored_subdistrict}",
        sub_geometry,
        {
            "level": "subdistrict",
            "district_name": stored_district,
            "subdistrict_name": stored_subdistrict,
            "village_count": len(village_features),
            "field_count": int(block["fields"]),
        },
    )
    return {
        "level": "subdistrict",
        "district_name": stored_district,
        "subdistrict_name": stored_subdistrict,
        "subdistrict": sub_feature,
        "villages": {
            "type": "FeatureCollection",
            "count": len(village_features),
            "features": village_features,
        },
    }


def stored_tehsil_geometry(district_name: str, subdistrict_name: str) -> dict[str, Any] | None:
    with engine.connect() as conn:
        raw = conn.execute(
            text(
                """
                SELECT geom_geojson
                FROM kawardha_data
                WHERE level = 'tehsil'
                  AND district_name = :district
                  AND tehsil_name = :subdistrict
                ORDER BY id DESC
                LIMIT 1
                """
            ),
            {"district": district_name, "subdistrict": subdistrict_name},
        ).scalar()
    return json.loads(raw) if raw else None


def field_hull_by_place(district_name: str, subdistrict_name: str, village_name: str) -> dict[str, Any] | None:
    with engine.connect() as conn:
        raw = conn.execute(
            text(
                """
                SELECT ST_AsGeoJSON(ST_ConvexHull(ST_Collect(ST_SRID(geom, 0))))
                FROM field_boundaries
                WHERE district = :district
                  AND sub_dist = :subdistrict
                  AND village_name = :village
                """
            ),
            {"district": district_name, "subdistrict": subdistrict_name, "village": village_name},
        ).scalar()
    return json.loads(raw) if raw else None


def observation_window(year: int | None, month: int | None) -> tuple[date, date] | None:
    if month is not None and year is None:
        raise HTTPException(status_code=400, detail="year is required when month is provided")
    if year is None:
        return None
    start = date(year, month or 1, 1)
    if month is None or month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)
    return start, end


def stream_crop_period_fields(
    district_name: str,
    subdistrict_name: str,
    village_name: str,
    year: int,
    month: int | None,
) -> StreamingResponse:
    window = observation_window(year, month)
    assert window is not None
    start, end = window
    params: dict[str, Any] = {
        "district": district_name,
        "subdistrict": subdistrict_name,
        "village_name": village_name,
        "period_from": start,
        "period_to": end,
    }
    statement = text(
        """
        SELECT c.id, c.field_boundary_id, c.field_id, c.village_name, c.subdistrict_name, c.district_name,
               c.area_ha, c.final_label, c.predicted_crop, c.is_rice,
               COALESCE(f.geom_geojson, c.geom_geojson) AS geom_geojson
        FROM crop_identification c
        LEFT JOIN field_boundaries f ON f.id = c.field_boundary_id AND c.field_boundary_id > 0
        WHERE c.district_name = :district
          AND c.subdistrict_name = :subdistrict
          AND c.village_name = :village_name
          AND c.observation_date >= :period_from
          AND c.observation_date < :period_to
        ORDER BY c.field_id, c.id
        """
    )
    header = {
        "type": "FeatureCollection",
        "level": "fields",
        "district_name": district_name,
        "subdistrict_name": subdistrict_name,
        "village_name": village_name,
        "year": year,
        "month": month,
    }
    return StreamingResponse(iter_crop_features(statement, params, header), media_type="application/geo+json")


def iter_crop_features(statement, params: dict[str, Any], header: dict[str, Any]) -> Iterator[str]:
    prefix = json.dumps(header, ensure_ascii=False)
    yield prefix[:-1] + ',"features":['
    first = True
    with engine.connect() as conn:
        result = conn.execution_options(stream_results=True).execute(statement, params)
        for row in result:
            geometry = row.geom_geojson or "null"
            properties = {
                "level": "field",
                "crop_id": int(row.id),
                "field_boundary_id": int(row.field_boundary_id) if row.field_boundary_id is not None else None,
                "field_id": int(row.field_id) if row.field_id is not None else None,
                "district_name": row.district_name,
                "subdistrict_name": row.subdistrict_name,
                "village_name": row.village_name,
                "area_ha": json_safe(row.area_ha),
                "final_label": row.final_label,
                "predicted_crop": row.predicted_crop,
                "is_rice": bool(row.is_rice) if row.is_rice is not None else False,
                "year": header.get("year"),
                "month": header.get("month"),
            }
            piece = '{"type":"Feature","id":' + str(int(row.id)) + ',"geometry":' + geometry
            piece += ',"properties":' + json.dumps(properties, ensure_ascii=False) + "}"
            if not first:
                yield ","
            first = False
            yield piece
    yield "]}"


def stream_named_village_fields(
    district_name: str,
    subdistrict_name: str,
    village_name: str,
    year: int | None = None,
    month: int | None = None,
) -> StreamingResponse:
    places = stored_subdistricts(district_name)
    block = next((row for row in places if same_name(row["subdistrict_name"], subdistrict_name)), None)
    if block is None:
        raise HTTPException(
            status_code=404,
            detail=f"No subdistrict {subdistrict_name!r} under district={district_name!r}",
        )
    villages = stored_villages(block["district_name"], block["subdistrict_name"])
    admin = admin_villages(block["district_name"], block["subdistrict_name"])
    match = next((village for village in villages if same_name(village["village_name"], village_name)), None)
    admin_match = next((village for village in admin if same_name(village["village_name"], village_name)), None)
    if year is not None:
        stored_village = match["village_name"] if match else admin_match["village_name"] if admin_match else village_name
        return stream_crop_period_fields(
            block["district_name"],
            block["subdistrict_name"],
            stored_village,
            year,
            month,
        )
    if match is None and admin_match is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No village {village_name!r} in district={block['district_name']!r}, "
                f"subdistrict={block['subdistrict_name']!r}"
            ),
        )
    stored_village = match["village_name"] if match else admin_match["village_name"]
    statement = text(
        """
        SELECT f.id, f.field_id, f.village_id, f.village_name, f.sub_dist, f.district,
               f.area_ha, f.`class`, f.geom_geojson, c.final_label, c.predicted_crop, c.is_rice
        FROM field_boundaries f
        LEFT JOIN crop_identification c ON c.field_boundary_id = f.id
        WHERE f.district = :district
          AND f.sub_dist = :subdistrict
          AND f.village_name = :village_name
        ORDER BY f.field_id, f.id
        """
    )
    params = {
        "district": block["district_name"],
        "subdistrict": block["subdistrict_name"],
        "village_name": stored_village,
    }
    header = {
        "type": "FeatureCollection",
        "level": "fields",
        "district_name": block["district_name"],
        "subdistrict_name": block["subdistrict_name"],
        "village_name": stored_village,
        "count": match["fields"] if match else 0,
    }
    return StreamingResponse(iter_features(statement, params, header), media_type="application/geo+json")


def iter_features(statement, params: dict[str, Any], header: dict[str, Any]) -> Iterator[str]:
    prefix = json.dumps(header, ensure_ascii=False)
    yield prefix[:-1] + ',"features":['
    first = True
    with engine.connect() as conn:
        result = conn.execution_options(stream_results=True).execute(statement, params)
        for row in result:
            geometry = row.geom_geojson or "null"
            properties = {
                "level": "field",
                "field_boundary_id": int(row.id),
                "field_id": int(row.field_id) if row.field_id is not None else None,
                "village_id": int(row.village_id) if row.village_id is not None else None,
                "district_name": row.district,
                "subdistrict_name": row.sub_dist,
                "village_name": row.village_name,
                "area_ha": json_safe(row.area_ha),
                "class": row._mapping["class"],
                "final_label": row.final_label,
                "predicted_crop": row.predicted_crop,
                "is_rice": bool(row.is_rice) if row.is_rice is not None else False,
            }
            piece = '{"type":"Feature","id":' + str(int(row.id)) + ',"geometry":' + geometry
            piece += ',"properties":' + json.dumps(properties, ensure_ascii=False) + "}"
            if not first:
                yield ","
            first = False
            yield piece
    yield "]}"


def boundary_response(
    district_name: str | None,
    subdistrict_name: str | None,
    village_name: str | None,
    year: int | None = None,
    month: int | None = None,
):
    district_name, subdistrict_name, village_name = require_places(district_name, subdistrict_name, village_name)
    named = bool(subdistrict_name) and not is_block(subdistrict_name)
    if named and village_name:
        return stream_named_village_fields(district_name, subdistrict_name, village_name, year, month)
    if named:
        return named_subdistrict_payload(district_name, subdistrict_name)
    if subdistrict_name and village_name:
        return stream_village_fields(district_name, subdistrict_name, village_name)
    if subdistrict_name:
        return subdistrict_payload(district_name, subdistrict_name)
    return district_payload(district_name)


def name_response(district_name: str | None, subdistrict_name: str | None) -> dict[str, Any]:
    district_name, subdistrict_name, _village = require_places(district_name, subdistrict_name, None)
    places = stored_subdistricts(district_name)
    if not subdistrict_name:
        return {
            "district_name": places[0]["district_name"],
            "subdistricts": [row["subdistrict_name"] for row in places],
        }
    if is_block(subdistrict_name):
        block = find_block(district_name, subdistrict_name)
        villages = village_stats(block["chunk_id"])
        return {
            "district_name": block["district_name"],
            "subdistrict_name": block["subdistrict_name"],
            "villages": [village["village_name"] for village in villages],
        }
    match = next((row for row in places if same_name(row["subdistrict_name"], subdistrict_name)), None)
    if match is None:
        raise HTTPException(
            status_code=404,
            detail=f"No subdistrict {subdistrict_name!r} under district={district_name!r}",
        )
    boundaries = admin_villages(match["district_name"], match["subdistrict_name"])
    if boundaries:
        villages = [row["village_name"] for row in boundaries]
    else:
        villages = [row["village_name"] for row in stored_villages(match["district_name"], match["subdistrict_name"])]
    return {
        "district_name": match["district_name"],
        "subdistrict_name": match["subdistrict_name"],
        "villages": villages,
    }


@router.get("/boundaries", response_model=None)
def get_boundaries(
    district_name: Annotated[str, Query(description="District name, for example Chhattisgarh")],
    subdistrict_name: Annotated[str | None, Query(description="Subdistrict block, for example r5c1")] = None,
    village_name: Annotated[str | None, Query(description="Village name, for example Chhattisgarh")] = None,
    year: Annotated[int | None, Query(ge=1900, le=2100, description="Observation year")] = None,
    month: Annotated[int | None, Query(ge=1, le=12, description="Observation month")] = None,
):
    """District outline, subdistrict plus village outlines, or field polygons for the selected period."""
    return boundary_response(district_name, subdistrict_name, village_name, year, month)


@router.post("/boundaries", response_model=None)
def post_boundaries(body: BoundaryRequest):
    return boundary_response(body.district_name, body.subdistrict_name, body.village_name, body.year, body.month)


@router.get("/boundaries/names")
def get_boundary_names(
    district_name: Annotated[str, Query(description="District name, for example Chhattisgarh")],
    subdistrict_name: Annotated[str | None, Query(description="Subdistrict block, for example r5c1")] = None,
) -> dict[str, Any]:
    """Subdistrict names in a district, or village names in a subdistrict."""
    return name_response(district_name, subdistrict_name)


@router.post("/boundaries/names")
def post_boundary_names(body: NameRequest) -> dict[str, Any]:
    return name_response(body.district_name, body.subdistrict_name)
