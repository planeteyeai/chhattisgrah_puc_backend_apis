import re
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import get_db
from app.geojson import json_safe

router = APIRouter(prefix="/crops", tags=["crops"])

CHUNK_KEY = re.compile(r"r(\d+)c(\d+)", re.I)


def api_value(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    return json_safe(value)


def chunk_sort(name: str | None) -> tuple[int, int, str]:
    match = CHUNK_KEY.fullmatch((name or "").strip())
    if match:
        return int(match.group(1)), int(match.group(2)), ""
    return 999, 999, name or ""


def period_filter(year: int | None, month: int | None) -> tuple[str, dict[str, Any]]:
    if month is not None and year is None:
        raise HTTPException(status_code=400, detail="year is required when month is provided")
    if month is not None and not 1 <= month <= 12:
        raise HTTPException(status_code=400, detail="month must be from 1 to 12")
    if year is not None and not 1900 <= year <= 2100:
        raise HTTPException(status_code=400, detail="year is out of range")
    if year is None:
        return "1=1", {}
    start = date(year, month or 1, 1)
    if month is None or month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)
    return (
        "observation_date >= :period_from AND observation_date < :period_to",
        {"period_from": start, "period_to": end},
    )


def _filters(
    district: str | None,
    subdistrict: str | None,
    village: str | None,
    label: str | None,
    season: str | None,
    rice_only: bool,
    year: int | None = None,
    month: int | None = None,
) -> tuple[str, dict[str, Any]]:
    period_sql, params = period_filter(year, month)
    clauses = [period_sql]
    if district:
        clauses.append("district_name = :district")
        params["district"] = district
    if subdistrict:
        clauses.append("subdistrict_name = :subdistrict")
        params["subdistrict"] = subdistrict
    if village:
        clauses.append("village_name = :village")
        params["village"] = village
    if label:
        clauses.append("final_label = :label")
        params["label"] = label
    if season:
        clauses.append("season = :season")
        params["season"] = season
    if rice_only:
        clauses.append("is_rice = 1")
    return " AND ".join(clauses), params


@router.get("/periods")
def crop_periods(db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.execute(
        text(
            """
            SELECT YEAR(observation_date) AS year, MONTH(observation_date) AS month,
                   COUNT(*) AS fields, COALESCE(SUM(area_ha), 0) AS area_ha
            FROM crop_identification
            GROUP BY YEAR(observation_date), MONTH(observation_date)
            ORDER BY year, month
            """
        )
    ).mappings().all()
    periods = [{key: api_value(value) for key, value in dict(row).items()} for row in rows]
    return {"count": len(periods), "periods": periods}


@router.get("/summary")
def crop_summary(
    db: Session = Depends(get_db),
    year: Annotated[int | None, Query(ge=1900, le=2100)] = None,
    month: Annotated[int | None, Query(ge=1, le=12)] = None,
) -> dict[str, Any]:
    where_sql, params = period_filter(year, month)
    totals = db.execute(
        text(
            f"""
            SELECT
              COUNT(*) AS fields,
              COALESCE(SUM(area_ha), 0) AS area_ha,
              COALESCE(SUM(is_rice), 0) AS rice_fields,
              COUNT(DISTINCT district_name) AS districts,
              COUNT(DISTINCT subdistrict_name) AS subdistricts,
              COUNT(DISTINCT village_name) AS villages,
              MIN(observation_date) AS observation_from,
              MAX(observation_date) AS observation_to,
              MIN(identified_on) AS identified_from,
              MAX(identified_on) AS identified_to
            FROM crop_identification
            WHERE {where_sql}
            """
        ),
        params,
    ).mappings().one()
    labels = db.execute(
        text(
            f"""
            SELECT final_label AS label, COUNT(*) AS fields, COALESCE(SUM(area_ha), 0) AS area_ha
            FROM crop_identification
            WHERE {where_sql}
            GROUP BY final_label
            ORDER BY fields DESC
            """
        ),
        params,
    ).mappings().all()
    observations = db.execute(
        text(
            f"""
            SELECT observation_date, COUNT(*) AS fields, COALESCE(SUM(area_ha), 0) AS area_ha
            FROM crop_identification
            WHERE {where_sql}
            GROUP BY observation_date
            ORDER BY observation_date
            """
        ),
        params,
    ).mappings().all()
    models = db.execute(
        text(
            f"""
            SELECT source_model, COUNT(*) AS fields, COALESCE(SUM(area_ha), 0) AS area_ha
            FROM crop_identification
            WHERE {where_sql}
            GROUP BY source_model
            ORDER BY fields DESC
            """
        ),
        params,
    ).mappings().all()
    seasons = db.execute(
        text(
            f"""
            SELECT season, COUNT(*) AS fields, COALESCE(SUM(area_ha), 0) AS area_ha
            FROM crop_identification
            WHERE {where_sql}
            GROUP BY season
            ORDER BY season
            """
        ),
        params,
    ).mappings().all()
    return {
        "year": year,
        "month": month,
        "fields": api_value(totals["fields"]),
        "area_ha": api_value(totals["area_ha"]),
        "rice_fields": api_value(totals["rice_fields"]),
        "districts": api_value(totals["districts"]),
        "subdistricts": api_value(totals["subdistricts"]),
        "villages": api_value(totals["villages"]),
        "observation_from": api_value(totals["observation_from"]),
        "observation_to": api_value(totals["observation_to"]),
        "identified_from": api_value(totals["identified_from"]),
        "identified_to": api_value(totals["identified_to"]),
        "labels": [{key: api_value(value) for key, value in dict(row).items()} for row in labels],
        "observations": [{key: api_value(value) for key, value in dict(row).items()} for row in observations],
        "models": [{key: api_value(value) for key, value in dict(row).items()} for row in models],
        "seasons": [{key: api_value(value) for key, value in dict(row).items()} for row in seasons],
    }


@router.get("/places")
def crop_places(
    db: Session = Depends(get_db),
    year: Annotated[int | None, Query(ge=1900, le=2100)] = None,
    month: Annotated[int | None, Query(ge=1, le=12)] = None,
) -> dict[str, Any]:
    where_sql, params = period_filter(year, month)
    rows = db.execute(
        text(
            f"""
            SELECT
              district_name,
              subdistrict_name,
              village_name,
              final_label,
              COUNT(*) AS fields,
              COALESCE(SUM(area_ha), 0) AS area_ha
            FROM crop_identification
            WHERE {where_sql}
            GROUP BY district_name, subdistrict_name, village_name, final_label
            """
        ),
        params,
    ).mappings().all()
    places: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["district_name"] or "", row["subdistrict_name"] or "", row["village_name"] or "")
        place = places.get(key)
        if place is None:
            place = {
                "district_name": row["district_name"],
                "subdistrict_name": row["subdistrict_name"],
                "village_name": row["village_name"],
                "fields": 0,
                "area_ha": 0.0,
                "labels": {},
            }
            places[key] = place
        fields = int(row["fields"] or 0)
        area = float(api_value(row["area_ha"]) or 0)
        place["fields"] += fields
        place["area_ha"] += area
        place["labels"][row["final_label"]] = {"fields": fields, "area_ha": round(area, 4)}
    ordered = sorted(
        places.values(),
        key=lambda item: (
            item["district_name"] or "",
            item["village_name"] or "",
            chunk_sort(item["subdistrict_name"]),
        ),
    )
    for place in ordered:
        place["area_ha"] = round(place["area_ha"], 4)
    return {"count": len(ordered), "places": ordered}


@router.get("/fields")
def crop_fields(
    db: Session = Depends(get_db),
    district: Annotated[str | None, Query()] = None,
    subdistrict: Annotated[str | None, Query()] = None,
    village: Annotated[str | None, Query()] = None,
    label: Annotated[str | None, Query()] = None,
    season: Annotated[str | None, Query()] = None,
    rice_only: Annotated[bool, Query()] = False,
    year: Annotated[int | None, Query(ge=1900, le=2100)] = None,
    month: Annotated[int | None, Query(ge=1, le=12)] = None,
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> dict[str, Any]:
    """One page of crop fields. Subdistrict alone returns that whole subdistrict.

    Keep requesting skip=next_skip until has_more is false.
    """
    where_sql, params = _filters(district, subdistrict, village, label, season, rice_only, year, month)
    total = int(
        db.execute(text(f"SELECT COUNT(*) FROM crop_identification WHERE {where_sql}"), params).scalar_one()
    )
    rows = db.execute(
        text(
            f"""
            SELECT
              id, field_boundary_id, field_id, district_name, subdistrict_name, village_name,
              season, observation_date, identified_on, predicted_crop, final_label,
              is_rice, confidence, margin, area_ha, source_model, centroid_lon, centroid_lat
            FROM crop_identification
            WHERE {where_sql}
            ORDER BY id
            LIMIT :limit OFFSET :skip
            """
        ),
        {**params, "limit": limit, "skip": skip},
    ).mappings().all()
    fields = []
    for row in rows:
        item = {key: api_value(value) for key, value in dict(row).items()}
        item["is_rice"] = bool(row["is_rice"])
        fields.append(item)
    count = len(fields)
    has_more = skip + count < total
    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "count": count,
        "has_more": has_more,
        "next_skip": skip + count if has_more else None,
        "fields": fields,
    }


@router.get("/points")
def crop_points(
    subdistrict: Annotated[str, Query(min_length=1)],
    db: Session = Depends(get_db),
    label: Annotated[str | None, Query()] = None,
    year: Annotated[int | None, Query(ge=1900, le=2100)] = None,
    month: Annotated[int | None, Query(ge=1, le=12)] = None,
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> dict[str, Any]:
    """One page of field points for a subdistrict. Repeat skip=next_skip until has_more is false."""
    period_sql, params = period_filter(year, month)
    clauses = [period_sql, "subdistrict_name = :subdistrict", "centroid_lon IS NOT NULL", "centroid_lat IS NOT NULL"]
    params["subdistrict"] = subdistrict
    if label:
        clauses.append("final_label = :label")
        params["label"] = label
    where_sql = " AND ".join(clauses)
    total = int(db.execute(text(f"SELECT COUNT(*) FROM crop_identification WHERE {where_sql}"), params).scalar_one())
    if total == 0:
        raise HTTPException(status_code=404, detail=f"No crop points for subdistrict={subdistrict!r}")
    rows = db.execute(
        text(
            f"""
            SELECT id, final_label, area_ha, is_rice, centroid_lon, centroid_lat, field_boundary_id
            FROM crop_identification
            WHERE {where_sql}
            ORDER BY id
            LIMIT :limit OFFSET :skip
            """
        ),
        {**params, "limit": limit, "skip": skip},
    ).mappings().all()
    points = []
    for row in rows:
        points.append(
            {
                "id": int(row["id"]),
                "field_boundary_id": int(row["field_boundary_id"]) if row["field_boundary_id"] is not None else None,
                "label": row["final_label"],
                "area_ha": api_value(row["area_ha"]),
                "is_rice": bool(row["is_rice"]),
                "lon": api_value(row["centroid_lon"]),
                "lat": api_value(row["centroid_lat"]),
            }
        )
    count = len(points)
    has_more = skip + count < total
    return {
        "subdistrict": subdistrict,
        "total": total,
        "skip": skip,
        "limit": limit,
        "count": count,
        "has_more": has_more,
        "next_skip": skip + count if has_more else None,
        "points": points,
    }
