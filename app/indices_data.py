"""MySQL storage for Sentinel-2 scene indices.

`indices` is the table GET /indices/retrieve-aggregated reads.
District, subdistrict, and village rows are also written to their own tables.
One row is one Sentinel-2 scene for one location, not one row per month.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Connection

from app.database import engine
from app.kawardha_data import district_key, merge_polygons, parse_geometry

INDEX_COLUMNS: tuple[str, ...] = (
    "ndvi",
    "evi",
    "evi2",
    "gndvi",
    "ndre",
    "ndwi_mcfeeters",
    "ndwi_gao",
    "ndmi",
    "rvi_optical",
    "ndbi",
    "bsi",
    "savi",
    "osavi",
    "rvi_radar",
    "vh_vv_ratio",
    "lst",
    "temperature",
    "precipitation_daily",
    "precipitation_sum",
    "soil_water_fraction_surface",
    "soil_water_fraction_subsurface",
)

DROPPED_INDEXES = ("msavi", "gci")

CROPTYPE_NOTE = (
    "croptype is accepted as input but not applied because indices table has no crop_type column."
)

ROW_COLUMNS: tuple[str, ...] = (
    "id",
    "obs_date",
    "district",
    "subdistrict",
    "village",
    "product_id",
    "tile_id",
    "cloud_coverage",
    *INDEX_COLUMNS,
    "created_at",
    "updated_at",
)

INSERT_COLUMNS: tuple[str, ...] = (
    "obs_date",
    "district",
    "subdistrict",
    "village",
    "product_id",
    "tile_id",
    "cloud_coverage",
    *INDEX_COLUMNS,
    "created_at",
    "updated_at",
)

UPDATE_COLUMNS: tuple[str, ...] = (
    "tile_id",
    "cloud_coverage",
    *INDEX_COLUMNS,
    "updated_at",
)

TABLES = ("indices", "indices_district", "indices_subdistrict", "indices_village")

PERIOD_SQL = {
    "weekly": "CAST(DATE_SUB(obs_date, INTERVAL WEEKDAY(obs_date) DAY) AS DATE)",
    "monthly": "CAST(DATE_SUB(obs_date, INTERVAL (DAY(obs_date) - 1) DAY) AS DATE)",
    "yearly": "CAST(DATE_SUB(obs_date, INTERVAL (DAYOFYEAR(obs_date) - 1) DAY) AS DATE)",
}

# PostgreSQL date_trunc drops the current week and year. The current month stays.
PERIOD_CUTOFF_SQL = {
    "weekly": "CAST(DATE_SUB(CURDATE(), INTERVAL WEEKDAY(CURDATE()) DAY) AS DATE)",
    "yearly": "CAST(DATE_SUB(CURDATE(), INTERVAL (DAYOFYEAR(CURDATE()) - 1) DAY) AS DATE)",
}

_SCHEMA_READY = False


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def clean_text(value: str | None) -> str:
    return (value or "").strip().lower()


def normalize_location(
    district: str | None,
    subdistrict: str | None,
    village: str | None,
) -> tuple[str, str, str]:
    district_name = clean_text(district)
    if not district_name:
        raise ValueError("district is required")
    return district_name, clean_text(subdistrict), clean_text(village)


def level_table(subdistrict: str, village: str) -> str:
    if village:
        return "indices_village"
    if subdistrict:
        return "indices_subdistrict"
    return "indices_district"


def _column_sql() -> str:
    index_sql = ",\n  ".join(f"`{name}` DOUBLE NULL" for name in INDEX_COLUMNS)
    return f"""
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `obs_date` DATE NOT NULL,
  `district` VARCHAR(191) NOT NULL,
  `subdistrict` VARCHAR(191) NOT NULL DEFAULT '',
  `village` VARCHAR(191) NOT NULL DEFAULT '',
  `product_id` VARCHAR(128) NOT NULL,
  `tile_id` VARCHAR(32) NULL,
  `cloud_coverage` DOUBLE NULL,
  {index_sql},
  `created_at` DATETIME NULL,
  `updated_at` DATETIME NULL
"""


def ensure_indices_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    statements = [
        f"""
        CREATE TABLE IF NOT EXISTS `indices` (
          {_column_sql()},
          PRIMARY KEY (`id`),
          UNIQUE KEY `indices_uq_loc_obs_product` (`district`, `subdistrict`, `village`, `obs_date`, `product_id`),
          KEY `idx_indices_district_obs` (`district`, `obs_date`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        f"""
        CREATE TABLE IF NOT EXISTS `indices_district` (
          {_column_sql()},
          PRIMARY KEY (`id`),
          UNIQUE KEY `indices_district_uq_loc_obs_product` (`district`, `obs_date`, `product_id`),
          KEY `idx_indices_district_obs` (`district`, `obs_date`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        f"""
        CREATE TABLE IF NOT EXISTS `indices_subdistrict` (
          {_column_sql()},
          PRIMARY KEY (`id`),
          UNIQUE KEY `indices_subdistrict_uq_loc_obs_product` (`district`, `subdistrict`, `obs_date`, `product_id`),
          KEY `idx_indices_subdistrict_obs` (`district`, `subdistrict`, `obs_date`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        f"""
        CREATE TABLE IF NOT EXISTS `indices_village` (
          {_column_sql()},
          PRIMARY KEY (`id`),
          UNIQUE KEY `indices_village_uq_loc_obs_product`
            (`district`, `subdistrict`, `village`, `obs_date`, `product_id`),
          KEY `idx_indices_village_obs` (`district`, `subdistrict`, `village`, `obs_date`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS `indices_fallback` (
          `district` VARCHAR(191) NOT NULL,
          `subdistrict` VARCHAR(191) NOT NULL DEFAULT '',
          `village` VARCHAR(191) NOT NULL DEFAULT '',
          `source_district` VARCHAR(191) NOT NULL,
          `source_subdistrict` VARCHAR(191) NOT NULL DEFAULT '',
          `source_village` VARCHAR(191) NOT NULL DEFAULT '',
          `created_at` DATETIME NULL,
          PRIMARY KEY (`district`, `subdistrict`, `village`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        """
        CREATE TABLE IF NOT EXISTS `indices_cron_checkpoint` (
          `id` TINYINT NOT NULL,
          `district` VARCHAR(191) NULL,
          `subdistrict` VARCHAR(191) NULL,
          `village` VARCHAR(191) NULL,
          `obs_date` DATE NULL,
          `updated_at` DATETIME NULL,
          PRIMARY KEY (`id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """,
        "INSERT IGNORE INTO `indices_cron_checkpoint` (`id`) VALUES (1)",
    ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
    _SCHEMA_READY = True


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _date_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)[:10]


def _time_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def upsert_indices(conn: Connection, row: dict[str, Any]) -> None:
    """Insert or update `indices` and the matching level table. Location keys stay put."""
    payload = {column: row.get(column) for column in INSERT_COLUMNS}
    for column in INDEX_COLUMNS:
        payload[column] = _number(payload[column])
    payload["cloud_coverage"] = _number(payload["cloud_coverage"])
    payload["subdistrict"] = payload["subdistrict"] or ""
    payload["village"] = payload["village"] or ""
    columns = ", ".join(f"`{column}`" for column in INSERT_COLUMNS)
    values = ", ".join(f":{column}" for column in INSERT_COLUMNS)
    assignments = ", ".join(f"`{column}` = incoming.`{column}`" for column in UPDATE_COLUMNS)
    sql = (
        f"INSERT INTO `{{table}}` ({columns}) VALUES ({values}) AS incoming "
        f"ON DUPLICATE KEY UPDATE {assignments}"
    )
    tables = ("indices", level_table(payload["subdistrict"], payload["village"]))
    for table in tables:
        conn.execute(text(sql.format(table=table)), payload)


def _location_clause(
    district: str,
    subdistrict: str | None,
    village: str | None,
) -> tuple[str, dict[str, Any]]:
    clauses = ["LOWER(TRIM(district)) = LOWER(TRIM(:district))"]
    params: dict[str, Any] = {"district": district}
    if subdistrict is not None:
        clauses.append("LOWER(TRIM(COALESCE(subdistrict, ''))) = LOWER(TRIM(:subdistrict))")
        params["subdistrict"] = subdistrict
    if village is not None:
        clauses.append("LOWER(TRIM(COALESCE(village, ''))) = LOWER(TRIM(:village))")
        params["village"] = village
    return " AND ".join(clauses), params


def location_has_rows(district: str, subdistrict: str | None, village: str | None) -> bool:
    where, params = _location_clause(district, subdistrict, village)
    sql = f"SELECT 1 FROM `indices` WHERE {where} LIMIT 1"
    with engine.connect() as conn:
        return conn.execute(text(sql), params).first() is not None


def pending_locations(
    district: str | None = None,
    max_locations: int = 0,
    *,
    villages_only: bool = True,
) -> list[tuple[str, str, str]]:
    """Places from list_village_locations that do not yet have indices rows."""
    locations = list_village_locations(district, max_locations=0)
    if villages_only:
        locations = [item for item in locations if item[2]]
    stored = {
        (item["district"], item["subdistrict"] or "", item["village"] or "")
        for item in stored_locations(district, None, None, 200000)
    }
    pending = [item for item in locations if item not in stored]
    if max_locations > 0:
        pending = pending[:max_locations]
    return pending


_CENTROID_CACHE: dict[tuple[str, str, str], tuple[float, float] | None] = {}


def _centroid_xy(district: str, subdistrict: str, village: str) -> tuple[float, float] | None:
    key = (clean_text(district), clean_text(subdistrict), clean_text(village))
    if key in _CENTROID_CACHE:
        return _CENTROID_CACHE[key]
    try:
        geometry = resolve_geometry(district, subdistrict or None, village or None)
    except LookupError:
        _CENTROID_CACHE[key] = None
        return None
    try:
        from shapely.geometry import shape

        point = shape(geometry).centroid
        if point.is_empty:
            _CENTROID_CACHE[key] = None
            return None
        value = (float(point.x), float(point.y))
    except Exception:
        value = None
    _CENTROID_CACHE[key] = value
    return value


def _read_sticky_fallback(district: str, subdistrict: str, village: str) -> tuple[str, str, str] | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT source_district, source_subdistrict, source_village
                FROM `indices_fallback`
                WHERE LOWER(TRIM(district)) = LOWER(TRIM(:district))
                  AND LOWER(TRIM(subdistrict)) = LOWER(TRIM(:subdistrict))
                  AND LOWER(TRIM(village)) = LOWER(TRIM(:village))
                LIMIT 1
                """
            ),
            {"district": district, "subdistrict": subdistrict, "village": village},
        ).mappings().first()
    if not row:
        return None
    return (
        str(row["source_district"] or ""),
        str(row["source_subdistrict"] or ""),
        str(row["source_village"] or ""),
    )


def _write_sticky_fallback(
    district: str,
    subdistrict: str,
    village: str,
    source: tuple[str, str, str],
) -> None:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO `indices_fallback` (
                  district, subdistrict, village,
                  source_district, source_subdistrict, source_village, created_at
                ) VALUES (
                  :district, :subdistrict, :village,
                  :source_district, :source_subdistrict, :source_village, :created_at
                )
                ON DUPLICATE KEY UPDATE
                  source_district = VALUES(source_district),
                  source_subdistrict = VALUES(source_subdistrict),
                  source_village = VALUES(source_village)
                """
            ),
            {
                "district": district,
                "subdistrict": subdistrict,
                "village": village,
                "source_district": source[0],
                "source_subdistrict": source[1],
                "source_village": source[2],
                "created_at": now,
            },
        )


def clear_sticky_fallback(district: str, subdistrict: str, village: str) -> None:
    """Drop proxy mapping so the next read uses this village's exact stored series."""
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                DELETE FROM `indices_fallback`
                WHERE LOWER(TRIM(district)) = LOWER(TRIM(:district))
                  AND LOWER(TRIM(subdistrict)) = LOWER(TRIM(:subdistrict))
                  AND LOWER(TRIM(village)) = LOWER(TRIM(:village))
                """
            ),
            {"district": district, "subdistrict": subdistrict, "village": village},
        )


def _village_centroids(district: str) -> dict[tuple[str, str], tuple[float, float]]:
    """Batch-load village centroids for one district from admin polygons."""
    names = _district_names(district)
    if not names:
        return {}
    out: dict[tuple[str, str], tuple[float, float]] = {}
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT tehsil_name, village_name, geom_geojson
                FROM kawardha_data
                WHERE level = 'village'
                  AND LOWER(TRIM(district_name)) IN :names
                  AND geom_geojson IS NOT NULL
                  AND village_name IS NOT NULL
                  AND TRIM(village_name) <> ''
                """
            ).bindparams(bindparam("names", expanding=True)),
            {"names": list(names)},
        ).mappings()
        from shapely.geometry import shape

        for row in rows:
            try:
                sub = clean_text(row["tehsil_name"])
                vil = clean_text(row["village_name"])
                geometry = _geometry_from_json(row["geom_geojson"])
                if not geometry:
                    continue
                point = shape(geometry).centroid
                if point.is_empty:
                    continue
                out[(sub, vil)] = (float(point.x), float(point.y))
            except Exception:
                continue
    return out


def _nearest_stored_village(district: str, subdistrict: str, village: str) -> tuple[str, str, str] | None:
    """Pick the nearest already-stored village. Tie-break is alphabetical (stable)."""
    candidates = [
        (item["district"], item["subdistrict"] or "", item["village"] or "")
        for item in stored_locations(district, None, None, 200000)
        if (item.get("village") or "").strip()
        and clean_text(item.get("village")) != clean_text(village)
    ]
    if not candidates:
        return None
    same_sub = [
        item for item in candidates if clean_text(item[1]) == clean_text(subdistrict)
    ]
    pool = same_sub or candidates
    centroids = _village_centroids(district)
    origin = centroids.get((clean_text(subdistrict), clean_text(village)))
    if origin is None:
        origin = _centroid_xy(district, subdistrict, village)
    scored: list[tuple[float, str, str, str]] = []
    for cand_district, cand_sub, cand_village in pool:
        point = centroids.get((clean_text(cand_sub), clean_text(cand_village)))
        if origin is not None and point is not None:
            dx = origin[0] - point[0]
            dy = origin[1] - point[1]
            distance = dx * dx + dy * dy
        else:
            # Stable name distance when geometry is missing.
            distance = abs(len(cand_village) - len(village)) + (
                0 if cand_village[:1].lower() == village[:1].lower() else 10
            )
        scored.append((distance, cand_district, cand_sub, cand_village))
    scored.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    best = scored[0]
    return best[1], best[2], best[3]


def resolve_series_location(
    district: str,
    subdistrict: str | None,
    village: str | None,
) -> tuple[str, str, str, dict[str, Any] | None]:
    """Return the location to read, with a sticky nearest-village fallback when empty.

    A sticky proxy stays in place across refreshes (and while that village is still
    being stored). Store completion clears the sticky row, and the next read returns
    that village's exact series.
    """
    district_name, subdistrict_name, village_name = normalize_location(district, subdistrict, village)
    if not village_name:
        return district_name, subdistrict_name, village_name, None

    sticky = _read_sticky_fallback(district_name, subdistrict_name, village_name)
    if sticky and location_has_rows(sticky[0], sticky[1], sticky[2]):
        source = sticky
        meta = {
            "requested": {
                "district": district_name,
                "subdistrict": subdistrict_name,
                "village": village_name,
            },
            "source": {
                "district": source[0],
                "subdistrict": source[1],
                "village": source[2],
            },
            "sticky": True,
            "note": (
                f"No indices stored yet for {village_name}; "
                f"showing nearest stored village {source[2]} until this village is stored."
            ),
        }
        return source[0], source[1], source[2], meta

    if location_has_rows(district_name, subdistrict_name, village_name):
        return district_name, subdistrict_name, village_name, None

    source = _nearest_stored_village(district_name, subdistrict_name, village_name)
    if source is None:
        return district_name, subdistrict_name, village_name, None
    _write_sticky_fallback(district_name, subdistrict_name, village_name, source)
    meta = {
        "requested": {
            "district": district_name,
            "subdistrict": subdistrict_name,
            "village": village_name,
        },
        "source": {
            "district": source[0],
            "subdistrict": source[1],
            "village": source[2],
        },
        "sticky": True,
        "note": (
            f"No indices stored yet for {village_name}; "
            f"showing nearest stored village {source[2]} until this village is stored."
        ),
    }
    return source[0], source[1], source[2], meta


def aggregated_series(
    district: str,
    subdistrict: str | None,
    village: str | None,
    frequency: str,
) -> list[dict[str, Any]]:
    period = PERIOD_SQL[frequency]
    where, params = _location_clause(district, subdistrict, village)
    cutoff = PERIOD_CUTOFF_SQL.get(frequency)
    if cutoff:
        where += f" AND obs_date < {cutoff}"
    averages = ", ".join(f"AVG(`{column}`) AS `{column}`" for column in INDEX_COLUMNS)
    sql = f"""
        SELECT {period} AS period, {averages}
        FROM `indices`
        WHERE {where}
        GROUP BY {period}
        ORDER BY period ASC
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    series = []
    for row in rows:
        item: dict[str, Any] = {"period": _date_text(row["period"])}
        for column in INDEX_COLUMNS:
            item[column] = _number(row[column])
        series.append(item)
    return series


def raw_series(
    district: str,
    subdistrict: str | None,
    village: str | None,
    start_date: date | None,
    end_date: date | None,
    limit: int,
) -> list[dict[str, Any]]:
    where, params = _location_clause(district, subdistrict, village)
    if start_date is not None:
        where += " AND obs_date >= :start_date"
        params["start_date"] = start_date
    if end_date is not None:
        where += " AND obs_date <= :end_date"
        params["end_date"] = end_date
    columns = ", ".join(f"`{column}`" for column in ROW_COLUMNS)
    sql = f"""
        SELECT {columns}
        FROM `indices`
        WHERE {where}
        ORDER BY obs_date ASC, product_id ASC
        LIMIT {int(limit)}
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    series = []
    for row in rows:
        item: dict[str, Any] = {}
        for column in ROW_COLUMNS:
            value = row[column]
            if column == "id":
                item[column] = int(value) if value is not None else None
            elif column == "obs_date":
                item[column] = _date_text(value)
            elif column in ("created_at", "updated_at"):
                item[column] = _time_text(value)
            elif column in INDEX_COLUMNS or column == "cloud_coverage":
                item[column] = _number(value)
            else:
                item[column] = value
        series.append(item)
    return series


def stored_locations(
    district: str | None,
    subdistrict: str | None,
    village: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if district is not None:
        clauses.append("LOWER(TRIM(district)) = LOWER(TRIM(:district))")
        params["district"] = district
    if subdistrict is not None:
        clauses.append("LOWER(TRIM(COALESCE(subdistrict, ''))) = LOWER(TRIM(:subdistrict))")
        params["subdistrict"] = subdistrict
    if village is not None:
        clauses.append("LOWER(TRIM(COALESCE(village, ''))) = LOWER(TRIM(:village))")
        params["village"] = village
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT
          district,
          subdistrict,
          village,
          COUNT(*) AS rows_count,
          MIN(obs_date) AS min_obs_date,
          MAX(obs_date) AS max_obs_date,
          MAX(updated_at) AS last_updated_at
        FROM `indices`
        {where}
        GROUP BY district, subdistrict, village
        ORDER BY district, subdistrict, village
        LIMIT {int(limit)}
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    return [
        {
            "district": row["district"],
            "subdistrict": row["subdistrict"] or "",
            "village": row["village"] or "",
            "rows_count": int(row["rows_count"]),
            "min_obs_date": _date_text(row["min_obs_date"]),
            "max_obs_date": _date_text(row["max_obs_date"]),
            "last_updated_at": _time_text(row["last_updated_at"]),
        }
        for row in rows
    ]


def product_stored(district: str, subdistrict: str, village: str, product_id: str) -> bool:
    sql = """
        SELECT 1
        FROM `indices`
        WHERE LOWER(TRIM(district)) = LOWER(TRIM(:district))
          AND LOWER(TRIM(COALESCE(subdistrict, ''))) = LOWER(TRIM(:subdistrict))
          AND LOWER(TRIM(COALESCE(village, ''))) = LOWER(TRIM(:village))
          AND product_id = :product_id
        LIMIT 1
    """
    with engine.connect() as conn:
        found = conn.execute(
            text(sql),
            {
                "district": district,
                "subdistrict": subdistrict,
                "village": village,
                "product_id": product_id,
            },
        ).scalar()
    return found is not None


def location_has_obs_between(
    district: str,
    subdistrict: str,
    village: str,
    start_date: date,
    end_date: date,
) -> bool:
    sql = """
        SELECT 1
        FROM `indices`
        WHERE LOWER(TRIM(district)) = LOWER(TRIM(:district))
          AND LOWER(TRIM(COALESCE(subdistrict, ''))) = LOWER(TRIM(:subdistrict))
          AND LOWER(TRIM(COALESCE(village, ''))) = LOWER(TRIM(:village))
          AND obs_date >= :start_date
          AND obs_date <= :end_date
        LIMIT 1
    """
    with engine.connect() as conn:
        found = conn.execute(
            text(sql),
            {
                "district": district,
                "subdistrict": subdistrict,
                "village": village,
                "start_date": start_date,
                "end_date": end_date,
            },
        ).scalar()
    return found is not None


def read_checkpoint() -> dict[str, Any] | None:
    sql = """
        SELECT district, subdistrict, village, obs_date, updated_at
        FROM `indices_cron_checkpoint`
        WHERE id = 1
    """
    with engine.connect() as conn:
        row = conn.execute(text(sql)).mappings().first()
    if row is None or not row["district"]:
        return None
    return {
        "district": row["district"] or "",
        "subdistrict": row["subdistrict"] or "",
        "village": row["village"] or "",
        "obs_date": row["obs_date"],
        "updated_at": row["updated_at"],
    }


def write_checkpoint(
    district: str | None,
    subdistrict: str | None,
    village: str | None,
    obs_date: date | None,
) -> None:
    sql = """
        UPDATE `indices_cron_checkpoint`
        SET district = :district,
            subdistrict = :subdistrict,
            village = :village,
            obs_date = :obs_date,
            updated_at = :updated_at
        WHERE id = 1
    """
    with engine.begin() as conn:
        conn.execute(
            text(sql),
            {
                "district": district,
                "subdistrict": subdistrict,
                "village": village,
                "obs_date": obs_date,
                "updated_at": utc_now(),
            },
        )


def _district_names(district: str) -> list[str]:
    key = district_key(district)
    if key == "kabirdham":
        return ["kabirdham", "kawardha", "kabeerdham", "kawardha district"]
    cleaned = clean_text(district)
    return [cleaned] if cleaned else []


_GRID_BLOCK = re.compile(r"^r\d+c\d+$", re.IGNORECASE)


def _is_grid_block(name: str | None) -> bool:
    """Field-boundary tiles such as r0c5 are map cells, not tehsils."""
    return bool(name and _GRID_BLOCK.fullmatch(name.strip()))


def _same_district(left: str, right: str) -> bool:
    return district_key(left) == district_key(right) or clean_text(left) == clean_text(right)


def list_village_locations(district: str | None = None, max_locations: int = 0) -> list[tuple[str, str, str]]:
    """Village rows plus one subdistrict row and one district row for each parent."""
    found: dict[tuple[str, str, str], tuple[str, str, str]] = {}
    with engine.connect() as conn:
        admin_rows = conn.execute(
            text(
                """
                SELECT district_name, tehsil_name, village_name
                FROM kawardha_data
                WHERE level = 'village'
                  AND village_name IS NOT NULL
                  AND TRIM(village_name) <> ''
                """
            )
        ).mappings()
        for row in admin_rows:
            try:
                key = normalize_location(row["district_name"], row["tehsil_name"], row["village_name"])
            except ValueError:
                continue
            found[key] = key
        field_rows = conn.execute(
            text(
                """
                SELECT district, sub_dist, village_name
                FROM field_boundaries
                WHERE village_name IS NOT NULL
                  AND TRIM(village_name) <> ''
                GROUP BY district, sub_dist, village_name
                """
            )
        ).mappings()
        for row in field_rows:
            try:
                key = normalize_location(row["district"], row["sub_dist"], row["village_name"])
            except ValueError:
                continue
            if _is_grid_block(key[1]) or _is_grid_block(key[2]):
                continue
            found.setdefault(key, key)
    for district_name, subdistrict_name, _village_name in list(found):
        found.setdefault((district_name, "", ""), (district_name, "", ""))
        if subdistrict_name:
            found.setdefault((district_name, subdistrict_name, ""), (district_name, subdistrict_name, ""))
    locations = sorted(found.values())
    if district is not None and clean_text(district):
        locations = [item for item in locations if _same_district(item[0], district)]
    if max_locations > 0:
        locations = locations[:max_locations]
    return locations


def _geometry_from_json(raw: str | None) -> dict[str, Any] | None:
    geometry = parse_geometry(raw)
    if not geometry:
        return None
    if geometry.get("type") == "Feature":
        geometry = geometry.get("geometry")
    if not isinstance(geometry, dict):
        return None
    if geometry.get("type") == "GeometryCollection":
        return merge_polygons(geometry.get("geometries") or [])
    if geometry.get("type") in {"Polygon", "MultiPolygon"} and geometry.get("coordinates"):
        return geometry
    return None


def _admin_geometry(level: str, district: str, subdistrict: str, village: str) -> dict[str, Any] | None:
    names = _district_names(district)
    if not names:
        return None
    clauses = ["level = :level", "LOWER(TRIM(district_name)) IN :names"]
    params: dict[str, Any] = {"level": level, "names": list(names)}
    if level in {"village", "tehsil"}:
        clauses.append("LOWER(TRIM(COALESCE(tehsil_name, ''))) = :subdistrict")
        params["subdistrict"] = clean_text(subdistrict)
    if level == "village":
        clauses.append("LOWER(TRIM(COALESCE(village_name, ''))) = :village")
        params["village"] = clean_text(village)
    sql = f"""
        SELECT geom_geojson
        FROM kawardha_data
        WHERE {' AND '.join(clauses)}
          AND geom_geojson IS NOT NULL
        ORDER BY id DESC
        LIMIT 1
    """
    statement = text(sql).bindparams(bindparam("names", expanding=True))
    with engine.connect() as conn:
        raw = conn.execute(statement, params).scalar()
    return _geometry_from_json(raw)


def _merged_admin_geometry(district: str, subdistrict: str | None) -> dict[str, Any] | None:
    names = _district_names(district)
    if not names:
        return None
    clauses = ["level = 'village'", "LOWER(TRIM(district_name)) IN :names"]
    params: dict[str, Any] = {"names": list(names)}
    if subdistrict is not None:
        clauses.append("LOWER(TRIM(COALESCE(tehsil_name, ''))) = :subdistrict")
        params["subdistrict"] = clean_text(subdistrict)
    sql = f"""
        SELECT geom_geojson
        FROM kawardha_data
        WHERE {' AND '.join(clauses)}
          AND geom_geojson IS NOT NULL
    """
    statement = text(sql).bindparams(bindparam("names", expanding=True))
    with engine.connect() as conn:
        rows = conn.execute(statement, params).scalars().all()
    return merge_polygons([_geometry_from_json(raw) for raw in rows])


def _field_hull(district: str, subdistrict: str | None, village: str | None) -> dict[str, Any] | None:
    names = _district_names(district)
    if not names:
        return None
    clauses = ["LOWER(TRIM(district)) IN :names", "geom IS NOT NULL"]
    params: dict[str, Any] = {"names": list(names)}
    if subdistrict is not None:
        clauses.append("LOWER(TRIM(COALESCE(sub_dist, ''))) = :subdistrict")
        params["subdistrict"] = clean_text(subdistrict)
    if village is not None:
        clauses.append("LOWER(TRIM(COALESCE(village_name, ''))) = :village")
        params["village"] = clean_text(village)
    sql = f"""
        SELECT ST_AsGeoJSON(ST_ConvexHull(ST_Collect(geom)))
        FROM field_boundaries
        WHERE {' AND '.join(clauses)}
    """
    statement = text(sql).bindparams(bindparam("names", expanding=True))
    with engine.connect() as conn:
        raw = conn.execute(statement, params).scalar()
    if not raw:
        return None
    try:
        return _geometry_from_json(raw if isinstance(raw, str) else json.dumps(raw))
    except (TypeError, json.JSONDecodeError):
        return None


def resolve_geometry(district: str, subdistrict: str | None, village: str | None) -> dict[str, Any]:
    """AOI for a district, district+subdistrict, or district+subdistrict+village."""
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
    if geometry is None:
        place = ", ".join(part for part in (district_name, subdistrict_name, village_name) if part)
        raise LookupError(f"No stored geometry for {place}")
    return geometry
