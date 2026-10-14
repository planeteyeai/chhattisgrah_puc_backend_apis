import json
import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.sql import ColumnElement

from app.database import get_db
from app.geojson import json_safe
from app.models import KawardhaData

router = APIRouter(prefix="/kawardha-data", tags=["kawardha-data"])

DISTRICT_ALIASES = {
    "kabirdham": "kabirdham",
    "kabeerdham": "kabirdham",
    "kawardha": "kabirdham",
    "kawardha district": "kabirdham",
}

StrQuery = Annotated[str | None, Query()]


class BoundaryLookup(BaseModel):
    district: str = Field(..., examples=["Kabirdham"])
    tehsil: str | None = Field(None, examples=["Kawardha"])
    village: str | None = Field(None, examples=["Hedaspur"])


def present(value: str | None) -> str | None:
    text = re.sub(r"\s+", " ", (value or "").strip())
    return text or None


def normalize(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def district_key(value: str | None) -> str:
    key = normalize(value)
    return DISTRICT_ALIASES.get(key, key)


def name_match(column: Any, value: str, *, district: bool = False) -> ColumnElement[bool]:
    needle = district_key(value) if district else normalize(value)
    cleaned = func.lower(func.trim(column))
    if district:
        return func.replace(cleaned, "kabeerdham", "kabirdham") == needle
    return cleaned == needle


def geometry_bbox(geom: dict | None) -> list[float | None]:
    if not geom:
        return [None, None, None, None]
    lons: list[float] = []
    lats: list[float] = []

    def walk(node: object) -> None:
        if isinstance(node, list) and node and isinstance(node[0], (int, float)):
            lons.append(float(node[0]))
            lats.append(float(node[1]))
            return
        if isinstance(node, list):
            for item in node:
                walk(item)

    walk(geom.get("coordinates"))
    if not lons:
        return [None, None, None, None]
    return [min(lons), min(lats), max(lons), max(lats)]


def parse_geometry(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def merge_polygons(geoms: list[dict | None]) -> dict | None:
    polygons: list[Any] = []
    for geom in geoms:
        if not geom:
            continue
        kind = geom.get("type")
        if kind == "Polygon":
            polygons.append(geom["coordinates"])
        elif kind == "MultiPolygon":
            polygons.extend(geom["coordinates"])
    if not polygons:
        return None
    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": polygons[0]}
    return {"type": "MultiPolygon", "coordinates": polygons}


def row_to_feature(row: KawardhaData) -> dict[str, Any]:
    geometry = parse_geometry(row.geom_geojson)
    return {
        "type": "Feature",
        "id": row.id,
        "geometry": geometry,
        "properties": {
            "district_name": row.district_name,
            "tehsil_name": row.tehsil_name,
            "village_name": row.village_name,
            "village_code": row.village_code,
            "state_name": row.state_name,
            "level": row.level,
            "source": row.source,
            "bbox": [
                json_safe(row.min_lon),
                json_safe(row.min_lat),
                json_safe(row.max_lon),
                json_safe(row.max_lat),
            ],
        },
    }


def assembled_feature(
    *,
    feature_id: str,
    geometry: dict | None,
    district_name: str | None,
    tehsil_name: str | None,
    village_name: str | None,
    level: str,
    state_name: str | None,
    source: str | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    properties = {
        "district_name": district_name,
        "tehsil_name": tehsil_name,
        "village_name": village_name,
        "village_code": None,
        "state_name": state_name,
        "level": level,
        "source": source,
        "bbox": geometry_bbox(geometry),
    }
    if extra:
        properties.update(extra)
    return {
        "type": "Feature",
        "id": feature_id,
        "geometry": geometry,
        "properties": properties,
    }


def feature_collection(
    features: list[dict[str, Any]],
    district: str | None,
    tehsil: str | None,
    village: str | None,
    level: str,
) -> dict[str, Any]:
    return {
        "type": "FeatureCollection",
        "count": len(features),
        "level": level,
        "filters": {
            "district": district,
            "tehsil": tehsil,
            "village": village,
        },
        "features": features,
    }


def query_rows(
    db: Session,
    district: str,
    tehsil: str | None = None,
    village: str | None = None,
    level: str | None = None,
) -> list[KawardhaData]:
    stmt = select(KawardhaData).where(name_match(KawardhaData.district_name, district, district=True))
    if tehsil:
        stmt = stmt.where(name_match(KawardhaData.tehsil_name, tehsil))
    if village:
        stmt = stmt.where(name_match(KawardhaData.village_name, village))
    if level:
        stmt = stmt.where(KawardhaData.level == level)
    return list(db.execute(stmt.order_by(KawardhaData.id)).scalars().all())


def lookup_boundaries(
    db: Session,
    district: str | None,
    tehsil: str | None,
    village: str | None,
) -> dict[str, Any]:
    district = present(district)
    tehsil = present(tehsil)
    village = present(village)
    if not district:
        raise HTTPException(status_code=400, detail="district is required")
    if village and not tehsil:
        raise HTTPException(status_code=400, detail="tehsil is required when village is provided")

    if village:
        rows = query_rows(db, district, tehsil=tehsil, village=village, level="village")
        if not rows:
            raise HTTPException(
                status_code=404,
                detail=f"No village boundary found for district={district!r}, tehsil={tehsil!r}, village={village!r}",
            )
        return feature_collection([row_to_feature(row) for row in rows], district, tehsil, village, "village")

    if tehsil:
        stored = query_rows(db, district, tehsil=tehsil, level="tehsil")
        if stored:
            return feature_collection([row_to_feature(row) for row in stored], district, tehsil, None, "tehsil")
        rows = query_rows(db, district, tehsil=tehsil, level="village")
        if not rows:
            raise HTTPException(
                status_code=404,
                detail=f"No tehsil boundary found for district={district!r}, tehsil={tehsil!r}",
            )
        geometry = merge_polygons([parse_geometry(row.geom_geojson) for row in rows])
        feature = assembled_feature(
            feature_id=f"tehsil-{rows[0].id}",
            geometry=geometry,
            district_name=rows[0].district_name,
            tehsil_name=rows[0].tehsil_name,
            village_name=None,
            level="tehsil",
            state_name=rows[0].state_name,
            source=rows[0].source,
            extra={"village_count": len(rows)},
        )
        return feature_collection([feature], district, tehsil, None, "tehsil")

    rows = query_rows(db, district, level="district")
    if rows:
        return feature_collection([row_to_feature(row) for row in rows], district, None, None, "district")

    villages = query_rows(db, district, level="village")
    if not villages:
        raise HTTPException(status_code=404, detail=f"No district boundary found for district={district!r}")
    geometry = merge_polygons([parse_geometry(row.geom_geojson) for row in villages])
    feature = assembled_feature(
        feature_id=f"district-{villages[0].id}",
        geometry=geometry,
        district_name=villages[0].district_name,
        tehsil_name=None,
        village_name=None,
        level="district",
        state_name=villages[0].state_name,
        source=villages[0].source,
        extra={"village_count": len(villages)},
    )
    return feature_collection([feature], district, None, None, "district")


@router.get("")
@router.get("/")
def get_boundaries(
    district: Annotated[str, Query(description="District name, e.g. Kabirdham")],
    tehsil: Annotated[str | None, Query(description="Tehsil name, e.g. Kawardha")] = None,
    village: Annotated[str | None, Query(description="Village name, e.g. Hedaspur")] = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return lookup_boundaries(db, district, tehsil, village)


@router.post("")
@router.post("/")
def post_boundaries(body: BoundaryLookup, db: Session = Depends(get_db)) -> dict[str, Any]:
    return lookup_boundaries(db, body.district, body.tehsil, body.village)


@router.get("/names")
def list_names(
    district: StrQuery = None,
    tehsil: StrQuery = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    districts = [
        value
        for (value,) in db.execute(
            select(KawardhaData.district_name)
            .where(KawardhaData.district_name.is_not(None))
            .distinct()
            .order_by(KawardhaData.district_name)
        ).all()
    ]
    tehsil_stmt = (
        select(KawardhaData.tehsil_name)
        .where(KawardhaData.tehsil_name.is_not(None), KawardhaData.level == "village")
        .distinct()
        .order_by(KawardhaData.tehsil_name)
    )
    village_stmt = (
        select(KawardhaData.village_name)
        .where(KawardhaData.village_name.is_not(None), KawardhaData.level == "village")
        .distinct()
        .order_by(KawardhaData.village_name)
    )
    if district:
        tehsil_stmt = tehsil_stmt.where(name_match(KawardhaData.district_name, district, district=True))
        village_stmt = village_stmt.where(name_match(KawardhaData.district_name, district, district=True))
    if tehsil:
        village_stmt = village_stmt.where(name_match(KawardhaData.tehsil_name, tehsil))
    return {
        "districts": districts,
        "tehsils": [value for (value,) in db.execute(tehsil_stmt).all()],
        "villages": [value for (value,) in db.execute(village_stmt).all()],
    }
