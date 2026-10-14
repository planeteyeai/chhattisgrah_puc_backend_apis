"""AOI lookup for classwise analysis.

District geometry comes from stored admin polygons. A subdistrict is the
union of its village polygons. A village is one village polygon. field_id
selects one field_boundaries polygon and requires both subdistrict and village.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from shapely.geometry import shape
from sqlalchemy import bindparam, text

from app.database import engine
from app.indices_data import (
    _admin_geometry,
    _district_names,
    _field_hull,
    _geometry_from_json,
    _merged_admin_geometry,
    clean_text,
    normalize_location,
    resolve_geometry,
)


def _blank(value: str | None) -> str | None:
    text_value = (value or "").strip()
    return text_value or None


def validate_location(
    district: str | None,
    subdistrict: str | None,
    village: str | None,
    field_id: str | None = None,
) -> tuple[str, str | None, str | None, str | None]:
    district_name = _blank(district)
    subdistrict_name = _blank(subdistrict)
    village_name = _blank(village)
    field = _blank(None if field_id is None else str(field_id))
    if not district_name:
        raise HTTPException(status_code=400, detail="district is required")
    if village_name and not subdistrict_name:
        raise HTTPException(status_code=400, detail="subdistrict is required when village is provided")
    if field and not (subdistrict_name and village_name):
        raise HTTPException(status_code=400, detail="field_id requires subdistrict and village")
    return district_name, subdistrict_name, village_name, field


def _location_lists(district: str, subdistrict: str | None) -> dict[str, list[str]]:
    names = _district_names(district)
    if not names:
        return {}
    params: dict[str, Any] = {"names": list(names)}
    sub_sql = ""
    if subdistrict:
        sub_sql = " AND LOWER(TRIM(COALESCE(tehsil_name, ''))) = :subdistrict"
        params["subdistrict"] = clean_text(subdistrict)
    statement = text(
        f"""
        SELECT tehsil_name, village_name
        FROM kawardha_data
        WHERE level = 'village'
          AND LOWER(TRIM(district_name)) IN :names
          {sub_sql}
        """
    ).bindparams(bindparam("names", expanding=True))
    subdistricts: set[str] = set()
    villages: set[str] = set()
    try:
        with engine.connect() as conn:
            rows = conn.execute(statement, params).mappings()
            for row in rows:
                if row["tehsil_name"]:
                    subdistricts.add(str(row["tehsil_name"]).strip())
                if row["village_name"]:
                    villages.add(str(row["village_name"]).strip())
    except Exception:
        return {}
    payload: dict[str, list[str]] = {}
    if subdistricts and not subdistrict:
        payload["subdistricts"] = sorted(subdistricts)
    if villages and not (subdistrict and False):
        if subdistrict or not village_only(subdistrict):
            payload["villages"] = sorted(villages)
    return payload


def village_only(subdistrict: str | None) -> bool:
    return bool(subdistrict)


def _field_geometry(field_id: str, district: str, subdistrict: str, village: str):
    names = _district_names(district)
    if not names:
        return None
    fid: int | str = int(field_id) if str(field_id).isdigit() else field_id
    statement = text(
        """
        SELECT geom_geojson
        FROM field_boundaries
        WHERE (id = :fid OR field_id = :fid)
          AND LOWER(TRIM(district)) IN :names
          AND LOWER(TRIM(COALESCE(sub_dist, ''))) = :subdistrict
          AND LOWER(TRIM(COALESCE(village_name, ''))) = :village
          AND geom_geojson IS NOT NULL
        LIMIT 1
        """
    ).bindparams(bindparam("names", expanding=True))
    with engine.connect() as conn:
        raw = conn.execute(
            statement,
            {
                "fid": fid,
                "names": list(names),
                "subdistrict": clean_text(subdistrict),
                "village": clean_text(village),
            },
        ).scalar()
    return _geometry_from_json(raw)


def lookup_aoi(
    district: str | None,
    subdistrict: str | None = None,
    village: str | None = None,
    field_id: str | None = None,
) -> dict[str, Any]:
    """Return a shapely AOI plus the names used in responses and storage."""
    district_name, subdistrict_name, village_name, field = validate_location(
        district, subdistrict, village, field_id
    )
    try:
        normalize_location(district_name, subdistrict_name or "", village_name or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if field:
        geometry = _field_geometry(field, district_name, subdistrict_name or "", village_name or "")
        if geometry is None:
            raise HTTPException(status_code=404, detail=f"Field not found: {field}")
    else:
        try:
            geometry = resolve_geometry(district_name, subdistrict_name or "", village_name or "")
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    geom = shape(geometry)
    if geom.is_empty:
        raise HTTPException(status_code=404, detail="Stored geometry is empty")
    plot_id = village_name or subdistrict_name or district_name
    extra = _location_lists(district_name, subdistrict_name)
    if village_name:
        extra.pop("villages", None)
    return {
        "geometry": geom,
        "geojson": geometry,
        "plot_id": plot_id,
        "district": district_name,
        "subdistrict": subdistrict_name,
        "village": village_name,
        "field_id": field,
        **extra,
    }


def admin_geometry_or_none(district: str, subdistrict: str | None, village: str | None):
    """Same cascade as resolve_geometry, without raising."""
    district_name, subdistrict_name, village_name = normalize_location(district, subdistrict, village)
    geometry = None
    if village_name:
        geometry = _admin_geometry("village", district_name, subdistrict_name, village_name)
        if geometry is None:
            geometry = _field_hull(district_name, subdistrict_name, village_name)
    elif subdistrict_name:
        geometry = _admin_geometry("tehsil", district_name, subdistrict_name, "")
        if geometry is None:
            geometry = _merged_admin_geometry(district_name, subdistrict_name)
        if geometry is None:
            geometry = _field_hull(district_name, subdistrict_name, None)
    else:
        geometry = _admin_geometry("district", district_name, "", "")
        if geometry is None:
            geometry = _merged_admin_geometry(district_name, None)
        if geometry is None:
            geometry = _field_hull(district_name, None, None)
    return geometry
