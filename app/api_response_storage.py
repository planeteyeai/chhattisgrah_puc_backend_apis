"""Save and load classwise JSON responses.

The application database is MySQL, so response documents are JSON columns
with the same keys the specification stores in JSONB.
"""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from app.database import engine

PRODUCT_TABLES = {
    "growth": "api_response_growth",
    "water_uptake": "api_response_water_uptake",
    "soil_moisture": "api_response_soil_moisture",
    "pest_detection": "api_response_pest_detection",
    "canopy_height": "api_response_canopy_height",
    "weather_daily": "api_response_weather_daily",
    "weather_wind": "api_response_weather_wind",
}

_READY = False
_DROP_KEYS = {
    "villages",
    "subdistricts",
    "combined_tile",
    "lst_tile_url",
    "lst_class_combined_tile",
    "lst_class_tiles",
}
_GEOM_TYPES = {
    "Point",
    "MultiPoint",
    "LineString",
    "MultiLineString",
    "Polygon",
    "MultiPolygon",
    "GeometryCollection",
}


def ensure_response_schema() -> None:
    global _READY
    if _READY:
        return
    statements = []
    for table in PRODUCT_TABLES.values():
        statements.append(
            f"""
            CREATE TABLE IF NOT EXISTS `{table}` (
              `id` BIGINT NOT NULL AUTO_INCREMENT,
              `district` VARCHAR(191) NULL,
              `subdistrict` VARCHAR(191) NULL,
              `village` VARCHAR(191) NULL,
              `year_month` VARCHAR(7) NULL,
              `request_params` JSON NULL,
              `response_data` JSON NULL,
              `created_at` DATETIME NULL,
              PRIMARY KEY (`id`),
              KEY `idx_{table}_district` (`district`),
              KEY `idx_{table}_subdistrict` (`subdistrict`),
              KEY `idx_{table}_village` (`village`),
              KEY `idx_{table}_year_month` (`year_month`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
    statements.append(
        """
        CREATE TABLE IF NOT EXISTS `backfill_cron_checkpoint` (
          `id` TINYINT NOT NULL,
          `district` VARCHAR(191) NULL,
          `subdistrict` VARCHAR(191) NULL,
          `village` VARCHAR(191) NULL,
          `year_month` VARCHAR(7) NULL,
          `updated_at` DATETIME NULL,
          PRIMARY KEY (`id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    statements.append("INSERT IGNORE INTO `backfill_cron_checkpoint` (`id`) VALUES (1)")
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
    _READY = True


def _json_load(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode()
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def strip_for_storage(payload: Any) -> Any:
    """Deep-copy, drop list keys, and replace coordinates before insert."""
    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(item) for item in node]
        if not isinstance(node, dict):
            return node
        if node.get("type") in _GEOM_TYPES and "coordinates" in node:
            return {"type": node.get("type"), "coordinates_stripped": True}
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in _DROP_KEYS:
                continue
            if key == "geometry" and isinstance(value, dict):
                out[key] = {"type": value.get("type"), "coordinates_stripped": True}
                continue
            out[key] = walk(value)
        if isinstance(out.get("features"), list):
            out["features_count"] = len(out["features"])
        return out

    return walk(deepcopy(payload))


def request_params(
    district: str,
    subdistrict: str | None,
    village: str | None,
    start_date: str,
    end_date: str,
    field_id: str | None,
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "district": district,
        "subdistrict": subdistrict,
        "village": village,
        "start_date": start_date,
        "end_date": end_date,
    }
    if field_id:
        params["field_id"] = str(field_id)
    return params


def _location_sql(alias: str = "") -> str:
    prefix = f"{alias}." if alias else ""
    return f"""
      LOWER(TRIM(COALESCE({prefix}district, ''))) = LOWER(TRIM(:district))
      AND LOWER(TRIM(COALESCE({prefix}subdistrict, ''))) = LOWER(TRIM(:subdistrict))
      AND LOWER(TRIM(COALESCE({prefix}village, ''))) = LOWER(TRIM(:village))
    """


def _field_sql() -> str:
    return """
      (
        (:field_id IS NULL AND (
          JSON_EXTRACT(request_params, '$.field_id') IS NULL
          OR JSON_TYPE(JSON_EXTRACT(request_params, '$.field_id')) = 'NULL'
        ))
        OR JSON_UNQUOTE(JSON_EXTRACT(request_params, '$.field_id')) = :field_id
      )
      AND JSON_UNQUOTE(JSON_EXTRACT(request_params, '$.end_date')) = :end_date
    """


def save_response(
    product: str,
    district: str,
    subdistrict: str | None,
    village: str | None,
    start_date: str,
    end_date: str,
    field_id: str | None,
    payload: dict[str, Any],
) -> None:
    ensure_response_schema()
    table = PRODUCT_TABLES[product]
    params = request_params(district, subdistrict, village, start_date, end_date, field_id)
    stored = strip_for_storage(payload)
    year_month = end_date[:7]
    bind = {
        "district": district,
        "subdistrict": subdistrict or "",
        "village": village or "",
        "year_month": year_month,
        "request_params": json.dumps(params),
        "response_data": json.dumps(stored),
        "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "end_date": end_date,
        "field_id": str(field_id) if field_id else None,
    }
    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                DELETE FROM `{table}`
                WHERE {_location_sql()}
                  AND `year_month` = :year_month
                  AND {_field_sql()}
                """
            ),
            bind,
        )
        conn.execute(
            text(
                f"""
                INSERT INTO `{table}`
                  (district, subdistrict, village, `year_month`, request_params, response_data, created_at)
                VALUES
                  (:district, :subdistrict, :village, :year_month, CAST(:request_params AS JSON), CAST(:response_data AS JSON), :created_at)
                """
            ),
            bind,
        )


def load_response(
    product: str,
    district: str,
    subdistrict: str | None,
    village: str | None,
    end_date: str,
    field_id: str | None,
) -> dict[str, Any] | None:
    ensure_response_schema()
    table = PRODUCT_TABLES[product]
    with engine.connect() as conn:
        raw = conn.execute(
            text(
                f"""
                SELECT response_data
                FROM `{table}`
                WHERE {_location_sql()}
                  AND `year_month` = :year_month
                  AND {_field_sql()}
                ORDER BY id DESC
                LIMIT 1
                """
            ),
            {
                "district": district,
                "subdistrict": subdistrict or "",
                "village": village or "",
                "year_month": end_date[:7],
                "end_date": end_date,
                "field_id": str(field_id) if field_id else None,
            },
        ).scalar()
    loaded = _json_load(raw)
    return loaded if isinstance(loaded, dict) else None


def has_end_date(
    product: str,
    district: str,
    subdistrict: str | None,
    village: str | None,
    end_date: str,
) -> bool:
    return load_response(product, district, subdistrict, village, end_date, None) is not None


def list_series(
    product: str,
    district: str,
    subdistrict: str | None = None,
    village: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    ensure_response_schema()
    table = PRODUCT_TABLES[product]
    clauses = ["LOWER(TRIM(COALESCE(district, ''))) = LOWER(TRIM(:district))"]
    bind: dict[str, Any] = {"district": district, "limit": max(1, min(int(limit), 5000))}
    if subdistrict is not None and str(subdistrict).strip() != "":
        clauses.append("LOWER(TRIM(COALESCE(subdistrict, ''))) = LOWER(TRIM(:subdistrict))")
        bind["subdistrict"] = subdistrict
    if village is not None and str(village).strip() != "":
        clauses.append("LOWER(TRIM(COALESCE(village, ''))) = LOWER(TRIM(:village))")
        bind["village"] = village
    sql = f"""
        SELECT id, district, subdistrict, village, `year_month`, created_at, request_params, response_data
        FROM `{table}`
        WHERE {' AND '.join(clauses)}
        ORDER BY `year_month`, created_at, id
        LIMIT :limit
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), bind).mappings().all()
    series = []
    for row in rows:
        created = row["created_at"]
        series.append(
            {
                "id": int(row["id"]),
                "district": row["district"],
                "subdistrict": row["subdistrict"],
                "village": row["village"],
                "year_month": row["year_month"],
                "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
                "request_params": _json_load(row["request_params"]),
                "response_data": _json_load(row["response_data"]),
            }
        )
    return series


def delete_stored(district: str | None = None) -> dict[str, int]:
    ensure_response_schema()
    deleted: dict[str, int] = {}
    with engine.begin() as conn:
        for product, table in PRODUCT_TABLES.items():
            if district and str(district).strip():
                result = conn.execute(
                    text(
                        f"DELETE FROM `{table}` WHERE LOWER(TRIM(COALESCE(district, ''))) = LOWER(TRIM(:district))"
                    ),
                    {"district": district},
                )
            else:
                result = conn.execute(text(f"DELETE FROM `{table}`"))
            deleted[table] = int(result.rowcount or 0)
    return deleted


def read_checkpoint() -> dict[str, Any] | None:
    ensure_response_schema()
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT district, subdistrict, village, `year_month`, updated_at
                FROM backfill_cron_checkpoint
                WHERE id = 1
                """
            )
        ).mappings().first()
    if row is None:
        return None
    updated = row["updated_at"]
    return {
        "district": row["district"],
        "subdistrict": row["subdistrict"],
        "village": row["village"],
        "year_month": row["year_month"],
        "updated_at": updated.isoformat() if hasattr(updated, "isoformat") else updated,
    }


def write_checkpoint(district: str | None, subdistrict: str | None, village: str | None, year_month: str) -> None:
    ensure_response_schema()
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE backfill_cron_checkpoint
                SET district = :district,
                    subdistrict = :subdistrict,
                    village = :village,
                    `year_month` = :year_month,
                    updated_at = :updated_at
                WHERE id = 1
                """
            ),
            {
                "district": district,
                "subdistrict": subdistrict,
                "village": village,
                "year_month": year_month,
                "updated_at": datetime.now(timezone.utc).replace(tzinfo=None),
            },
        )
