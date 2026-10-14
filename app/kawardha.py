import json
import math
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database import KawardhaSessionLocal, kawardha_engine
from app.geojson import json_safe
from app.owners import plot_to_feature

router = APIRouter(prefix="/kawardha", tags=["kawardha"])

VALID_GEO_SQL = "e.min_lat BETWEEN 17 AND 25 AND e.min_lon BETWEEN 78 AND 84"
UTM_SQL = "e.x_min > 100000"


def schematic_origin(village_id: int) -> tuple[float, float]:
    col = int(village_id) % 10
    row = (int(village_id) // 10) % 8
    return 81.62 + col * 0.055, 21.48 + row * 0.055


def _local_bbox(village_id: int, x_min: float, y_min: float, x_max: float, y_max: float) -> dict[str, float]:
    lon0, lat0 = schematic_origin(village_id)
    width = max(float(x_max) - float(x_min), 80.0)
    height = max(float(y_max) - float(y_min), 80.0)
    lon_scale = 111320.0 * math.cos(math.radians(lat0))
    return {
        "min_lon": lon0,
        "min_lat": lat0,
        "max_lon": lon0 + width / lon_scale,
        "max_lat": lat0 + height / 110540.0,
    }


def require_kawardha() -> Iterator[Session]:
    if KawardhaSessionLocal is None:
        raise HTTPException(status_code=503, detail="Kawardha PostgreSQL is not configured")
    db = KawardhaSessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/health")
def kawardha_health() -> dict[str, str]:
    if kawardha_engine is None:
        return {"status": "error", "database": "not configured"}
    try:
        with kawardha_engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected", "engine": "postgresql", "name": "kawardha_db"}
    except Exception as exc:
        return {"status": "error", "database": str(exc)}


@router.get("/summary")
def kawardha_summary(db: Session = Depends(require_kawardha)) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            SELECT
              (SELECT COUNT(*) FROM plots) AS plots,
              (SELECT COUNT(*) FROM villages) AS villages,
              (SELECT COUNT(*) FROM villages WHERE scrape_status = 'done') AS villages_done,
              (SELECT COUNT(*) FROM plot_extents e WHERE {valid}) AS plots_valid_geo,
              (SELECT COUNT(*) FROM plot_extents e WHERE e.x_min <= 100000) AS plots_local_cad,
              (SELECT COUNT(DISTINCT p.village_id) FROM plots p) AS villages_with_plots,
              (SELECT COALESCE(SUM(area_ha), 0) FROM plots p
                 JOIN plot_extents e ON e.plot_id_fk = p.id
                 WHERE {valid}) AS area_ha
            """.format(valid=VALID_GEO_SQL)
        )
    ).mappings().one()
    return {key: json_safe(value) for key, value in dict(row).items()}


@router.get("/villages")
def list_villages(
    db: Session = Depends(require_kawardha),
    valid_geo: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    geo_filter = f"WHERE {VALID_GEO_SQL}" if valid_geo else ""
    rows = db.execute(
        text(
            f"""
            SELECT
              v.id,
              COALESCE(v.village_name_eng, v.village_name_en, v.village_name) AS name,
              v.village_name,
              v.village_name_en,
              v.village_name_eng,
              v.scrape_status AS status,
              v.plot_count,
              COUNT(p.id) AS segment_count,
              COALESCE(SUM(p.area_ha), 0) AS area_ha,
              COUNT(p.id) FILTER (WHERE {VALID_GEO_SQL}) AS georeferenced_plots,
              COUNT(p.id) FILTER (WHERE {UTM_SQL}) AS utm_plots,
              MIN(e.x_min) AS x_min,
              MIN(e.y_min) AS y_min,
              MAX(e.x_max) AS x_max,
              MAX(e.y_max) AS y_max,
              MIN(e.min_lon) FILTER (WHERE {VALID_GEO_SQL}) AS real_min_lon,
              MIN(e.min_lat) FILTER (WHERE {VALID_GEO_SQL}) AS real_min_lat,
              MAX(e.max_lon) FILTER (WHERE {VALID_GEO_SQL}) AS real_max_lon,
              MAX(e.max_lat) FILTER (WHERE {VALID_GEO_SQL}) AS real_max_lat
            FROM villages v
            JOIN plots p ON p.village_id = v.id
            JOIN plot_extents e ON e.plot_id_fk = p.id
            {geo_filter}
            GROUP BY v.id
            ORDER BY COUNT(p.id) DESC
            """
        )
    ).mappings().all()
    villages = []
    for row in rows:
        item = {key: json_safe(value) for key, value in dict(row).items()}
        if (item.get("utm_plots") or 0) > 0:
            item["geo_status"] = "georeferenced"
            item["min_lon"] = item.pop("real_min_lon", None)
            item["min_lat"] = item.pop("real_min_lat", None)
            item["max_lon"] = item.pop("real_max_lon", None)
            item["max_lat"] = item.pop("real_max_lat", None)
        else:
            item["geo_status"] = "local_cad"
            item.update(
                _local_bbox(
                    item["id"],
                    item["x_min"],
                    item["y_min"],
                    item["x_max"],
                    item["y_max"],
                )
            )
            item.pop("real_min_lon", None)
            item.pop("real_min_lat", None)
            item.pop("real_max_lon", None)
            item.pop("real_max_lat", None)
        villages.append(item)
    return {"total": len(villages), "villages": villages}


PLOT_COLUMNS = """
              p.id, p.village_id, p.plot_no, p.plot_id, p.survey_no, p.ulpin,
              p.area_ha, p.info, p.info_en,
              COALESCE(v.village_name_eng, v.village_name_en, v.village_name) AS village_name,
              v.village_name AS village_name_hi,
              e.the_geom_geojson AS geom_geojson,
              e.the_geom AS geom_wkt
"""


def _plot_filters(village_id: int | None, valid_geo: bool) -> tuple[str, dict[str, Any]]:
    clauses = ["1=1"]
    params: dict[str, Any] = {}
    if village_id is not None:
        clauses.append("p.village_id = :village_id")
        params["village_id"] = village_id
    if valid_geo:
        clauses.append(VALID_GEO_SQL)
    return " AND ".join(clauses), params


def _village_layout(db: Session, village_id: int) -> dict[str, float] | None:
    row = db.execute(
        text(
            """
            SELECT MIN(e.x_min) AS x_min, MIN(e.y_min) AS y_min,
                   COUNT(*) FILTER (WHERE e.x_min > 100000) AS utm_n
            FROM plots p
            JOIN plot_extents e ON e.plot_id_fk = p.id
            WHERE p.village_id = :village_id
            """
        ),
        {"village_id": village_id},
    ).mappings().one()
    if row["x_min"] is None or (row["utm_n"] or 0) > 0:
        return None
    lon0, lat0 = schematic_origin(village_id)
    return {
        "x0": float(row["x_min"]),
        "y0": float(row["y_min"]),
        "lon0": lon0,
        "lat0": lat0,
    }


@router.get("/plots")
def list_plots(
    db: Session = Depends(require_kawardha),
    village_id: Annotated[int | None, Query()] = None,
    valid_geo: Annotated[bool, Query()] = True,
    skip: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=5000)] = 100,
) -> dict[str, Any]:
    local = _village_layout(db, village_id) if village_id is not None else None
    use_valid_geo = False if local else valid_geo
    where_sql, params = _plot_filters(village_id, use_valid_geo)
    total = db.execute(
        text(
            f"""
            SELECT COUNT(*) FROM plots p
            JOIN plot_extents e ON e.plot_id_fk = p.id
            WHERE {where_sql}
            """
        ),
        params,
    ).scalar_one()
    rows = db.execute(
        text(
            f"""
            SELECT {PLOT_COLUMNS}
            FROM plots p
            JOIN plot_extents e ON e.plot_id_fk = p.id
            LEFT JOIN villages v ON v.id = p.village_id
            WHERE {where_sql}
            ORDER BY p.id
            OFFSET :skip LIMIT :limit
            """
        ),
        {**params, "skip": skip, "limit": limit},
    ).all()
    features = [plot_to_feature(row, local) for row in rows]
    return {
        "type": "FeatureCollection",
        "total": int(total),
        "skip": skip,
        "limit": limit,
        "count": len(features),
        "features": features,
    }


@router.get("/plots/map")
def map_plots(
    db: Session = Depends(require_kawardha),
    village_id: Annotated[int | None, Query()] = None,
    valid_geo: Annotated[bool, Query()] = True,
) -> StreamingResponse:
    local = _village_layout(db, village_id) if village_id is not None else None
    use_valid_geo = False if local else valid_geo
    where_sql, params = _plot_filters(village_id, use_valid_geo)
    stmt = text(
        f"""
        SELECT {PLOT_COLUMNS}
        FROM plots p
        JOIN plot_extents e ON e.plot_id_fk = p.id
        LEFT JOIN villages v ON v.id = p.village_id
        WHERE {where_sql}
        ORDER BY p.id
        """
    )

    def generate() -> Iterator[str]:
        yield '{"type":"FeatureCollection","features":['
        first = True
        with kawardha_engine.connect() as conn:
            result = conn.execution_options(stream_results=True).execute(stmt, params)
            for row in result:
                if not first:
                    yield ","
                first = False
                yield json.dumps(plot_to_feature(row, local), separators=(",", ":"), ensure_ascii=False)
        yield "]}"

    return StreamingResponse(generate(), media_type="application/geo+json")


@router.get("/plots/{plot_id}")
def get_plot(plot_id: int, db: Session = Depends(require_kawardha)) -> dict[str, Any]:
    row = db.execute(
        text(
            f"""
            SELECT {PLOT_COLUMNS}
            FROM plots p
            JOIN plot_extents e ON e.plot_id_fk = p.id
            LEFT JOIN villages v ON v.id = p.village_id
            WHERE p.id = :plot_id
            """
        ),
        {"plot_id": plot_id},
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Plot not found")
    local = _village_layout(db, int(row._mapping["village_id"]))
    return plot_to_feature(row, local)


@router.get("/hierarchy")
def cadastral_hierarchy(db: Session = Depends(require_kawardha)) -> dict[str, Any]:
    rows = db.execute(
        text(
            """
            SELECT
              s.state_name,
              s.state_code,
              d.district_name,
              d.district_name_en,
              d.district_code,
              t.id AS taluka_id,
              t.taluka_code,
              t.taluka_name,
              t.taluka_name_en,
              r.id AS ri_id,
              r.ri_code,
              r.ri_name,
              r.ri_name_en,
              v.id AS village_id,
              v.village_code,
              v.giscode,
              v.village_name,
              v.village_name_en,
              v.village_name_eng,
              v.plot_count,
              v.scrape_status,
              COALESCE(pc.stored_plots, 0) AS stored_plots
            FROM villages v
            JOIN ris r ON r.id = v.ri_id
            JOIN talukas t ON t.id = r.taluka_id
            JOIN districts d ON d.id = t.district_id
            JOIN states s ON s.id = d.state_id
            LEFT JOIN (
              SELECT village_id, COUNT(*) AS stored_plots
              FROM plots
              GROUP BY village_id
            ) pc ON pc.village_id = v.id
            ORDER BY t.taluka_name_en, r.ri_name, v.village_name
            """
        )
    ).mappings().all()
    if not rows:
        raise HTTPException(status_code=404, detail="No cadastral hierarchy found")

    talukas: dict[int, dict[str, Any]] = {}
    ri_nodes: dict[int, dict[str, Any]] = {}
    for row in rows:
        taluka_id = int(row["taluka_id"])
        if taluka_id not in talukas:
            talukas[taluka_id] = {
                "id": taluka_id,
                "code": row["taluka_code"],
                "name": row["taluka_name"],
                "name_en": row["taluka_name_en"],
                "ris": [],
            }
        ri_id = int(row["ri_id"])
        if ri_id not in ri_nodes:
            ri_node = {
                "id": ri_id,
                "code": row["ri_code"],
                "name": row["ri_name"],
                "name_en": row["ri_name_en"],
                "villages": [],
            }
            ri_nodes[ri_id] = ri_node
            talukas[taluka_id]["ris"].append(ri_node)
        stored = int(row["stored_plots"] or 0)
        ri_nodes[ri_id]["villages"].append(
            {
                "id": int(row["village_id"]),
                "code": row["village_code"],
                "giscode": row["giscode"],
                "name": row["village_name"],
                "name_en": row["village_name_en"],
                "name_eng": row["village_name_eng"],
                "plot_count": int(row["plot_count"] or 0),
                "stored_plots": stored,
                "status": row["scrape_status"],
            }
        )

    def sort_name(node: dict[str, Any]) -> str:
        return (node.get("name_en") or node.get("name_eng") or node.get("name") or "").lower()

    taluka_list = sorted(talukas.values(), key=sort_name)
    village_total = 0
    villages_with_plots = 0
    plot_total = 0
    ri_total = 0
    for taluka in taluka_list:
        taluka["ris"].sort(key=sort_name)
        taluka_villages = 0
        taluka_plots = 0
        for ri_node in taluka["ris"]:
            ri_node["villages"].sort(key=sort_name)
            ri_node["village_count"] = len(ri_node["villages"])
            ri_node["plot_count"] = sum(village["stored_plots"] for village in ri_node["villages"])
            taluka_villages += ri_node["village_count"]
            taluka_plots += ri_node["plot_count"]
            ri_total += 1
        taluka["village_count"] = taluka_villages
        taluka["plot_count"] = taluka_plots
        village_total += taluka_villages
        plot_total += taluka_plots
        villages_with_plots += sum(
            1
            for ri_node in taluka["ris"]
            for village in ri_node["villages"]
            if village["stored_plots"] > 0
        )

    first = rows[0]
    return {
        "state": {"name": first["state_name"], "code": first["state_code"]},
        "district": {
            "name": first["district_name"],
            "name_en": first["district_name_en"],
            "code": first["district_code"],
        },
        "talukas": taluka_list,
        "totals": {
            "talukas": len(taluka_list),
            "ris": ri_total,
            "villages": village_total,
            "villages_with_plots": villages_with_plots,
            "plots": plot_total,
        },
    }
