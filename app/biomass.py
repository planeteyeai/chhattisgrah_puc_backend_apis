"""Field biomass from stored village Sentinel-2 scenes.

Rice and cotton only. The season runs from 1 June through the latest
clear scene in the identification window, not the 15th. That window is
60 days either side of 15 September. Each scene covers the days until
the next scene, capped at 16. Villages with no usable scenes take the
average tonnes per hectare of the villages that had scenes.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import bindparam, text

from app.database import engine

CROPS = ("Rice", "Cotton")
YEARS = (2024, 2025)
PAR_MJ = 9.0
STRESS = 1.0
EFFICIENCY = {"rice": 2.2, "cotton": 1.8}
MAX_SCENE_DAYS = 16
FAPAR_LOW = 0.0
FAPAR_HIGH = 0.95
COTTON_NDVI_SWITCH = 0.40
SEASON_START_MONTH = 6
SEASON_START_DAY = 1
IDENTIFICATION_MONTH = 9
WINDOW_REACH_DAYS = 60
BIOMASS_MIN_FACTOR = 0.85
BIOMASS_MAX_FACTOR = 1.15

router = APIRouter(prefix="/biomass", tags=["biomass"])
_BIOMASS_SCHEMA_READY = False
PAGE_DEFAULT = 100
PAGE_MAX = 1000
_PLACES_CACHE: dict[str, Any] | None = None
_COMPARE_CACHE: dict[tuple[str, int, int], dict[str, Any]] = {}


def _clear_biomass_cache() -> None:
    global _PLACES_CACHE
    _PLACES_CACHE = None
    _COMPARE_CACHE.clear()


def season_start(year: int) -> date:
    return date(year, SEASON_START_MONTH, SEASON_START_DAY)


def identification_window(year: int, month: int = IDENTIFICATION_MONTH) -> tuple[date, date]:
    """Start is included. End is excluded. The 15th is only the middle of the window."""
    center = date(year, month, 15)
    reach = timedelta(days=WINDOW_REACH_DAYS)
    return center - reach, center + reach


def latest_scene_date(
    scenes: list[tuple[date, float, float | None]],
    start: date,
    end: date,
) -> date | None:
    found = [obs for obs, ndvi, _savi in scenes if ndvi is not None and start <= obs < end]
    return max(found) if found else None


def biomass_bounds(total_tonnes: float) -> tuple[float, float]:
    """15% below and 15% above the stored field biomass, in tonnes."""
    total = float(total_tonnes or 0.0)
    return round(total * BIOMASS_MIN_FACTOR, 6), round(total * BIOMASS_MAX_FACTOR, 6)


def fapar(index: float) -> float:
    value = 1.24 * index - 0.16
    return min(FAPAR_HIGH, max(FAPAR_LOW, value))


def absorption_index(crop: str, ndvi: float, savi: float | None) -> float | None:
    """Rice uses NDVI. Cotton uses SAVI while NDVI is below 0.40, then NDVI."""
    if crop == "rice":
        return ndvi
    if ndvi < COTTON_NDVI_SWITCH:
        return savi
    return ndvi


def daily_growth(fapar_value: float, crop: str) -> float:
    """Grams of dry matter per square metre per day."""
    return PAR_MJ * fapar_value * EFFICIENCY[crop] * STRESS


def scene_days(scene_date: date, next_date: date | None, season_end: date) -> int:
    """Days this scene represents, capped at 16.

    The last scene runs through the observation date. A scene on that date
    still covers that one day.
    """
    boundary = next_date if next_date is not None else season_end
    span = (boundary - scene_date).days
    if next_date is None and span == 0:
        span = 1
    if span < 0:
        return 0
    return min(MAX_SCENE_DAYS, span)


def biomass_t_ha(
    scenes: list[tuple[date, float, float | None]],
    season_end: date,
    crop: str,
) -> tuple[float, int] | None:
    """Tonnes per hectare and the number of scenes that contributed.

    `scenes` are (obs_date, ndvi, savi) on or before `season_end`.
    """
    usable: list[tuple[date, float]] = []
    for obs_date, ndvi, savi in scenes:
        if obs_date > season_end:
            continue
        index = absorption_index(crop, ndvi, savi)
        if index is None:
            continue
        usable.append((obs_date, index))
    if not usable:
        return None
    usable.sort(key=lambda item: item[0])
    dry_matter = 0.0
    for index, (obs_date, absorption) in enumerate(usable):
        next_date = usable[index + 1][0] if index + 1 < len(usable) else None
        days = scene_days(obs_date, next_date, season_end)
        dry_matter += daily_growth(fapar(absorption), crop) * days
    return 0.01 * dry_matter, len(usable)


def village_rates(
    scenes_by_village: dict[str, list[tuple[date, float, float | None]]],
    season_end: date,
    crop: str,
) -> tuple[dict[str, tuple[float, int]], float | None]:
    """Per-village tonnes per hectare, plus the mean of villages that had scenes."""
    measured: dict[str, tuple[float, int]] = {}
    for village, scenes in scenes_by_village.items():
        result = biomass_t_ha(scenes, season_end, crop)
        if result is None:
            continue
        measured[village] = result
    if not measured:
        return {}, None
    average = sum(rate for rate, _count in measured.values()) / len(measured)
    return measured, average


def ensure_biomass_schema() -> None:
    global _BIOMASS_SCHEMA_READY
    if _BIOMASS_SCHEMA_READY:
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS biomass (
                  id BIGINT NOT NULL AUTO_INCREMENT,
                  crop_id BIGINT NOT NULL,
                  field_boundary_id BIGINT NULL,
                  field_id INT NULL,
                  district_name VARCHAR(191) NULL,
                  subdistrict_name VARCHAR(191) NULL,
                  village_name VARCHAR(191) NULL,
                  crop VARCHAR(32) NOT NULL,
                  `year` SMALLINT NOT NULL,
                  observation_date DATE NOT NULL,
                  season_start DATE NOT NULL,
                  season_end DATE NOT NULL,
                  area_ha DECIMAL(14,6) NULL,
                  biomass_t_ha DOUBLE NOT NULL,
                  total_tonnes DOUBLE NOT NULL,
                  min_biomass DOUBLE NULL,
                  max_biomass DOUBLE NULL,
                  scene_count INT NOT NULL,
                  rate_source VARCHAR(32) NOT NULL,
                  created_at DATETIME NULL,
                  PRIMARY KEY (id),
                  UNIQUE KEY biomass_uq_crop (crop_id),
                  KEY biomass_idx_year_crop (`year`, crop),
                  KEY biomass_idx_village (village_name)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
        )
        columns = {
            str(name).lower()
            for name in conn.execute(
                text(
                    """
                    SELECT COLUMN_NAME
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'biomass'
                    """
                )
            ).scalars()
        }
        added = False
        if "min_biomass" not in columns:
            conn.execute(text("ALTER TABLE biomass ADD COLUMN min_biomass DOUBLE NULL AFTER total_tonnes"))
            added = True
        if "max_biomass" not in columns:
            conn.execute(text("ALTER TABLE biomass ADD COLUMN max_biomass DOUBLE NULL AFTER min_biomass"))
            added = True
        if added:
            conn.execute(
                text(
                    """
                    UPDATE biomass
                    SET min_biomass = ROUND(total_tonnes * :low, 6),
                        max_biomass = ROUND(total_tonnes * :high, 6)
                    """
                ),
                {"low": BIOMASS_MIN_FACTOR, "high": BIOMASS_MAX_FACTOR},
            )
        indexes = {
            str(name).lower()
            for name in conn.execute(
                text(
                    """
                    SELECT INDEX_NAME
                    FROM information_schema.STATISTICS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'biomass'
                    """
                )
            ).scalars()
        }
        if "biomass_idx_place" not in indexes:
            conn.execute(
                text(
                    """
                    CREATE INDEX biomass_idx_place
                    ON biomass (village_name, `year`, crop, field_id)
                    """
                )
            )
    _BIOMASS_SCHEMA_READY = True


def _load_scenes(start: date, end: date) -> dict[str, list[tuple[date, float, float | None]]]:
    sql = """
        SELECT LOWER(TRIM(village)) AS village, obs_date,
               AVG(ndvi) AS ndvi, AVG(savi) AS savi
        FROM indices_village
        WHERE obs_date >= :start AND obs_date <= :end
          AND village IS NOT NULL AND TRIM(village) <> ''
          AND ndvi IS NOT NULL
        GROUP BY LOWER(TRIM(village)), obs_date
        ORDER BY village, obs_date
    """
    grouped: dict[str, list[tuple[date, float, float | None]]] = {}
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {"start": start, "end": end}).mappings()
        for row in rows:
            savi = None if row["savi"] is None else float(row["savi"])
            grouped.setdefault(row["village"], []).append((row["obs_date"], float(row["ndvi"]), savi))
    return grouped


def _load_fields(years: tuple[int, ...]) -> list[dict[str, Any]]:
    sql = """
        SELECT id, field_boundary_id, field_id, district_name, subdistrict_name, village_name,
               final_label, observation_date, area_ha
        FROM crop_identification
        WHERE final_label IN ('Rice', 'Cotton')
          AND YEAR(observation_date) IN :years
        ORDER BY id
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql).bindparams(bindparam("years", expanding=True)), {"years": list(years)}).mappings().all()
    return [dict(row) for row in rows]


def store_biomass(years: tuple[int, ...] = YEARS) -> dict[str, Any]:
    """Replace stored rice and cotton biomass for the requested years."""
    ensure_biomass_schema()
    earliest = min(season_start(year) for year in years)
    latest = date(max(years), 12, 31)
    scenes = _load_scenes(earliest, latest)
    fields = _load_fields(years)
    created_at = datetime.now(timezone.utc).replace(tzinfo=None)
    # (year, village, crop) -> tonnes per hectare, scene count, latest image date
    measured: dict[tuple[int, str, str], tuple[float, int, date]] = {}
    averages: dict[tuple[int, str], float] = {}
    fallback_end: dict[int, date] = {}
    village_counts: dict[tuple[int, str], int] = {}
    for year in years:
        window_start, window_end = identification_window(year)
        begin = season_start(year)
        totals: dict[str, list[float]] = {"rice": [], "cotton": []}
        ends: list[date] = []
        for village, items in scenes.items():
            season_end = latest_scene_date(items, window_start, window_end)
            if season_end is None:
                continue
            series = [(obs, ndvi, savi) for obs, ndvi, savi in items if begin <= obs <= season_end]
            ends.append(season_end)
            for crop in ("rice", "cotton"):
                result = biomass_t_ha(series, season_end, crop)
                if result is None:
                    continue
                tonnes_per_ha, scene_count = result
                measured[(year, village, crop)] = (tonnes_per_ha, scene_count, season_end)
                totals[crop].append(tonnes_per_ha)
        for crop, rates in totals.items():
            if rates:
                averages[(year, crop)] = sum(rates) / len(rates)
                village_counts[(year, crop)] = len(rates)
        if ends:
            fallback_end[year] = max(ends)

    payload = []
    summary: dict[tuple[int, str], dict[str, Any]] = {}
    for row in fields:
        observed = row["observation_date"]
        if observed is None or observed.year not in years:
            continue
        crop_name = row["final_label"]
        crop_key = crop_name.lower()
        village_key = (row["village_name"] or "").strip().lower()
        village_rate = measured.get((observed.year, village_key, crop_key))
        average = averages.get((observed.year, crop_key))
        if village_rate is not None:
            tonnes_per_ha, scene_count, season_end = village_rate
            source = "scenes"
        elif average is not None and observed.year in fallback_end:
            tonnes_per_ha, scene_count = average, 0
            season_end = fallback_end[observed.year]
            source = "village_average"
        else:
            continue
        area = float(row["area_ha"] or 0)
        total = round(tonnes_per_ha * area, 6)
        min_biomass, max_biomass = biomass_bounds(total)
        payload.append(
            {
                "crop_id": int(row["id"]),
                "field_boundary_id": row["field_boundary_id"],
                "field_id": row["field_id"],
                "district_name": row["district_name"],
                "subdistrict_name": row["subdistrict_name"],
                "village_name": row["village_name"],
                "crop": crop_name,
                "year": observed.year,
                "observation_date": season_end,
                "season_start": season_start(observed.year),
                "season_end": season_end,
                "area_ha": area,
                "biomass_t_ha": round(tonnes_per_ha, 6),
                "total_tonnes": total,
                "min_biomass": min_biomass,
                "max_biomass": max_biomass,
                "scene_count": scene_count,
                "rate_source": source,
                "created_at": created_at,
            }
        )
        bucket = summary.setdefault(
            (observed.year, crop_name),
            {
                "year": observed.year,
                "crop": crop_name,
                "fields": 0,
                "area_ha": 0.0,
                "total_tonnes": 0.0,
                "from_scenes": 0,
                "from_average": 0,
                "villages_with_scenes": village_counts.get((observed.year, crop_key), 0),
                "average_t_ha": None if average is None else round(average, 4),
            },
        )
        bucket["fields"] += 1
        bucket["area_ha"] += area
        bucket["total_tonnes"] += total
        bucket["from_scenes" if source == "scenes" else "from_average"] += 1

    insert_sql = text(
        """
        INSERT INTO biomass (
          crop_id, field_boundary_id, field_id, district_name, subdistrict_name, village_name,
          crop, `year`, observation_date, season_start, season_end, area_ha,
          biomass_t_ha, total_tonnes, min_biomass, max_biomass, scene_count, rate_source, created_at
        ) VALUES (
          :crop_id, :field_boundary_id, :field_id, :district_name, :subdistrict_name, :village_name,
          :crop, :year, :observation_date, :season_start, :season_end, :area_ha,
          :biomass_t_ha, :total_tonnes, :min_biomass, :max_biomass, :scene_count, :rate_source, :created_at
        )
        """
    )
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM biomass WHERE `year` IN :years AND crop IN ('Rice', 'Cotton')").bindparams(
                bindparam("years", expanding=True)
            ),
            {"years": list(years)},
        )
        for start in range(0, len(payload), 500):
            conn.execute(insert_sql, payload[start : start + 500])
    rows = []
    for bucket in summary.values():
        bucket["area_ha"] = round(bucket["area_ha"], 2)
        bucket["total_tonnes"] = round(bucket["total_tonnes"], 2)
        rows.append(bucket)
    rows.sort(key=lambda item: (item["year"], item["crop"]))
    _clear_biomass_cache()
    return {"stored": len(payload), "results": rows}


def _filters(
    year: int | None,
    crop: str | None,
    village: str | None,
    subdistrict: str | None = None,
) -> tuple[str, dict[str, Any]]:
    clauses = ["1=1"]
    params: dict[str, Any] = {}
    if year is not None:
        clauses.append("`year` = :year")
        params["year"] = year
    if crop:
        clauses.append("LOWER(crop) = LOWER(:crop)")
        params["crop"] = crop.strip()
    if subdistrict:
        clauses.append("subdistrict_name = :subdistrict")
        params["subdistrict"] = subdistrict.strip()
    if village:
        clauses.append("village_name = :village")
        params["village"] = village
    return " AND ".join(clauses), params


def _page_meta(total: int, skip: int, limit: int, count: int) -> dict[str, Any]:
    has_more = skip + count < total
    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "count": count,
        "has_more": has_more,
        "next_skip": skip + count if has_more else None,
    }


@router.get("/summary")
def biomass_summary(
    year: int | None = Query(default=None, ge=1900, le=2100),
    crop: str | None = None,
) -> dict[str, Any]:
    """Stored rice and cotton biomass totals."""
    ensure_biomass_schema()
    where_sql, params = _filters(year, crop, None)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"""
                SELECT `year` AS year, crop,
                       COUNT(*) AS fields,
                       COUNT(DISTINCT village_name) AS villages,
                       ROUND(SUM(area_ha), 2) AS area_ha,
                       ROUND(SUM(total_tonnes), 2) AS total_tonnes,
                       ROUND(SUM(min_biomass), 2) AS min_biomass,
                       ROUND(SUM(max_biomass), 2) AS max_biomass,
                       ROUND(SUM(total_tonnes) / NULLIF(SUM(area_ha), 0), 4) AS biomass_t_ha,
                       SUM(rate_source = 'scenes') AS fields_from_scenes,
                       SUM(rate_source = 'village_average') AS fields_from_average
                FROM biomass
                WHERE {where_sql}
                GROUP BY `year`, crop
                ORDER BY `year`, crop
                """
            ),
            params,
        ).mappings().all()
    results = []
    for row in rows:
        item = dict(row)
        for key in ("fields", "villages", "fields_from_scenes", "fields_from_average"):
            item[key] = int(item[key] or 0)
        for key in ("area_ha", "total_tonnes", "min_biomass", "max_biomass", "biomass_t_ha"):
            item[key] = None if item[key] is None else float(item[key])
        results.append(item)
    return {"count": len(results), "results": results}


@router.get("/village")
def biomass_village(
    village: str = Query(min_length=1),
    year: int = Query(ge=1900, le=2100),
    crop: str | None = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=PAGE_DEFAULT, ge=1, le=PAGE_MAX),
) -> dict[str, Any]:
    """Village totals, plus one page of field rows."""
    ensure_biomass_schema()
    if crop and crop.strip().lower() not in {"rice", "cotton"}:
        raise HTTPException(status_code=400, detail="crop must be Rice or Cotton")
    where_sql, params = _filters(year, crop, village)
    with engine.connect() as conn:
        totals = conn.execute(
            text(
                f"""
                SELECT COUNT(*) AS fields,
                       COALESCE(SUM(area_ha), 0) AS area_ha,
                       COALESCE(SUM(total_tonnes), 0) AS total_tonnes,
                       COALESCE(SUM(min_biomass), 0) AS min_biomass,
                       COALESCE(SUM(max_biomass), 0) AS max_biomass
                FROM biomass
                WHERE {where_sql}
                """
            ),
            params,
        ).mappings().one()
        rows = conn.execute(
            text(
                f"""
                SELECT crop_id, field_id, village_name, crop, `year` AS year, area_ha,
                       biomass_t_ha, total_tonnes, min_biomass, max_biomass, rate_source
                FROM biomass
                WHERE {where_sql}
                ORDER BY crop, crop_id
                LIMIT :limit OFFSET :skip
                """
            ),
            {**params, "limit": limit, "skip": skip},
        ).mappings().all()
    fields = []
    for row in rows:
        item = dict(row)
        item["area_ha"] = None if item["area_ha"] is None else float(item["area_ha"])
        item["biomass_t_ha"] = None if item["biomass_t_ha"] is None else float(item["biomass_t_ha"])
        item["total_tonnes"] = None if item["total_tonnes"] is None else float(item["total_tonnes"])
        item["min_biomass"] = None if item["min_biomass"] is None else float(item["min_biomass"])
        item["max_biomass"] = None if item["max_biomass"] is None else float(item["max_biomass"])
        fields.append(item)
    total = int(totals["fields"] or 0)
    return {
        "village": village,
        "year": year,
        "crop": crop,
        "total": total,
        "fields": total,
        "skip": skip,
        "limit": limit,
        "count": len(fields),
        "area_ha": round(float(totals["area_ha"] or 0), 4),
        "total_tonnes": round(float(totals["total_tonnes"] or 0), 4),
        "min_biomass": round(float(totals["min_biomass"] or 0), 4),
        "max_biomass": round(float(totals["max_biomass"] or 0), 4),
        "records": fields,
    }


@router.get("/places")
def biomass_places() -> dict[str, Any]:
    """Biomass for every village in 2024 and 2025."""
    global _PLACES_CACHE
    if _PLACES_CACHE is not None:
        return _PLACES_CACHE
    ensure_biomass_schema()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT village_name, `year` AS year, crop,
                       COUNT(*) AS fields,
                       COALESCE(SUM(area_ha), 0) AS area_ha,
                       COALESCE(SUM(total_tonnes), 0) AS total_tonnes,
                       COALESCE(SUM(min_biomass), 0) AS min_biomass,
                       COALESCE(SUM(max_biomass), 0) AS max_biomass
                FROM biomass
                GROUP BY village_name, `year`, crop
                ORDER BY village_name, `year`, crop
                """
            )
        ).mappings().all()
    places: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = row["village_name"] or ""
        place = places.setdefault(
            name,
            {"village": name, "2024": _empty_year(), "2025": _empty_year()},
        )
        year_key = str(int(row["year"]))
        if year_key not in place:
            continue
        bucket = place[year_key]
        fields = int(row["fields"] or 0)
        tonnes = float(row["total_tonnes"] or 0)
        area = float(row["area_ha"] or 0)
        low = float(row["min_biomass"] or 0)
        high = float(row["max_biomass"] or 0)
        bucket["fields"] += fields
        bucket["tonnes"] += tonnes
        bucket["area_ha"] += area
        bucket["min_biomass"] += low
        bucket["max_biomass"] += high
        crop_key = "rice" if str(row["crop"]).lower() == "rice" else "cotton"
        bucket[crop_key]["fields"] += fields
        bucket[crop_key]["tonnes"] += tonnes
        bucket[crop_key]["area_ha"] += area
        bucket[crop_key]["min_biomass"] += low
        bucket[crop_key]["max_biomass"] += high
    ordered = sorted(places.values(), key=lambda item: item["village"].lower())
    for place in ordered:
        for year_key in ("2024", "2025"):
            _round_year(place[year_key])
    _PLACES_CACHE = {"count": len(ordered), "villages": ordered}
    return _PLACES_CACHE


def _empty_year() -> dict[str, Any]:
    crop = {"fields": 0, "tonnes": 0.0, "area_ha": 0.0, "min_biomass": 0.0, "max_biomass": 0.0}
    return {
        "fields": 0,
        "tonnes": 0.0,
        "area_ha": 0.0,
        "min_biomass": 0.0,
        "max_biomass": 0.0,
        "rice": dict(crop),
        "cotton": dict(crop),
    }


def _round_year(bucket: dict[str, Any]) -> None:
    for key in ("tonnes", "area_ha", "min_biomass", "max_biomass"):
        bucket[key] = round(bucket[key], 2)
    for crop_key in ("rice", "cotton"):
        for key in ("tonnes", "area_ha", "min_biomass", "max_biomass"):
            bucket[crop_key][key] = round(bucket[crop_key][key], 2)


@router.get("/compare")
def biomass_compare(
    village: str = Query(min_length=1),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=PAGE_DEFAULT, ge=1, le=PAGE_MAX),
) -> dict[str, Any]:
    """One page of fields that exist in both 2024 and 2025. Totals cover every match."""
    cached = _COMPARE_CACHE.get((village, skip, limit))
    if cached is not None:
        return cached
    ensure_biomass_schema()
    join_sql = """
        FROM biomass y24
        JOIN biomass y25
          ON y25.village_name = y24.village_name
         AND y25.field_id = y24.field_id
         AND y25.crop = y24.crop
         AND y25.`year` = 2025
        WHERE y24.`year` = 2024
          AND y24.field_id IS NOT NULL
          AND y24.village_name = :village
    """
    with engine.connect() as conn:
        crop_rows = conn.execute(
            text(
                f"""
                SELECT y24.crop AS crop,
                       COUNT(*) AS total,
                       SUM(y25.total_tonnes > y24.total_tonnes) AS higher,
                       SUM(y25.total_tonnes < y24.total_tonnes) AS lower,
                       COALESCE(SUM(y24.min_biomass), 0) AS min_biomass_2024,
                       COALESCE(SUM(y24.max_biomass), 0) AS max_biomass_2024,
                       COALESCE(SUM(y25.min_biomass), 0) AS min_biomass_2025,
                       COALESCE(SUM(y25.max_biomass), 0) AS max_biomass_2025
                {join_sql}
                GROUP BY y24.crop
                ORDER BY y24.crop
                """
            ),
            {"village": village},
        ).mappings().all()
        rows = conn.execute(
            text(
                f"""
                SELECT y24.field_id, y24.crop,
                       y24.total_tonnes AS tonnes_2024,
                       y25.total_tonnes AS tonnes_2025,
                       y24.min_biomass AS min_biomass_2024,
                       y24.max_biomass AS max_biomass_2024,
                       y25.min_biomass AS min_biomass_2025,
                       y25.max_biomass AS max_biomass_2025,
                       y24.area_ha AS area_2024,
                       y25.area_ha AS area_2025
                {join_sql}
                ORDER BY y25.total_tonnes DESC, y24.field_id
                LIMIT :limit OFFSET :skip
                """
            ),
            {"village": village, "limit": limit, "skip": skip},
        ).mappings().all()
    fields = []
    for row in rows:
        fields.append(
            {
                "field_id": int(row["field_id"]) if row["field_id"] is not None else None,
                "crop": row["crop"],
                "tonnes_2024": float(row["tonnes_2024"] or 0),
                "tonnes_2025": float(row["tonnes_2025"] or 0),
                "min_biomass_2024": float(row["min_biomass_2024"] or 0),
                "max_biomass_2024": float(row["max_biomass_2024"] or 0),
                "min_biomass_2025": float(row["min_biomass_2025"] or 0),
                "max_biomass_2025": float(row["max_biomass_2025"] or 0),
                "area_2024": float(row["area_2024"] or 0),
                "area_2025": float(row["area_2025"] or 0),
            }
        )
    crops = []
    total = 0
    higher = 0
    lower = 0
    for row in crop_rows:
        total += int(row["total"] or 0)
        higher += int(row["higher"] or 0)
        lower += int(row["lower"] or 0)
        crops.append(
            {
                "crop": row["crop"],
                "min_biomass_2024": round(float(row["min_biomass_2024"] or 0), 4),
                "max_biomass_2024": round(float(row["max_biomass_2024"] or 0), 4),
                "min_biomass_2025": round(float(row["min_biomass_2025"] or 0), 4),
                "max_biomass_2025": round(float(row["max_biomass_2025"] or 0), 4),
            }
        )
    payload = {
        "village": village,
        "total": total,
        "count": total,
        "skip": skip,
        "limit": limit,
        "page_count": len(fields),
        "higher": higher,
        "lower": lower,
        "crops": crops,
        "fields": fields,
    }
    _COMPARE_CACHE[(village, skip, limit)] = payload
    return payload


def _biomass_item(row: Any) -> dict[str, Any]:
    item = dict(row)
    for key in ("observation_date", "season_start", "season_end"):
        value = item[key]
        item[key] = value.isoformat() if hasattr(value, "isoformat") else value
    item["area_ha"] = None if item["area_ha"] is None else float(item["area_ha"])
    item["biomass_t_ha"] = None if item["biomass_t_ha"] is None else float(item["biomass_t_ha"])
    item["total_tonnes"] = None if item["total_tonnes"] is None else float(item["total_tonnes"])
    item["min_biomass"] = None if item["min_biomass"] is None else float(item["min_biomass"])
    item["max_biomass"] = None if item["max_biomass"] is None else float(item["max_biomass"])
    return item


@router.get("/field")
def biomass_field(
    field_id: int | None = Query(default=None),
    village: str | None = None,
    year: int | None = Query(default=None, ge=1900, le=2100),
    crop: str | None = None,
    crop_id: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    """Biomass for one plot. Pass field_id. Add village and year when that number exists in more than one place."""
    if field_id is None and crop_id is None:
        raise HTTPException(status_code=400, detail="field_id is required")
    ensure_biomass_schema()
    clauses = []
    params: dict[str, Any] = {}
    if field_id is not None:
        clauses.append("field_id = :field_id")
        params["field_id"] = field_id
    if crop_id is not None and field_id is None:
        clauses.append("crop_id = :crop_id")
        params["crop_id"] = crop_id
    if village:
        clauses.append("village_name = :village")
        params["village"] = village.strip()
    if year is not None:
        clauses.append("`year` = :year")
        params["year"] = year
    if crop:
        clauses.append("LOWER(crop) = LOWER(:crop)")
        params["crop"] = crop.strip()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"""
                SELECT crop_id, field_boundary_id, field_id, district_name, subdistrict_name, village_name,
                       crop, `year` AS year, observation_date, season_start, season_end, area_ha,
                       biomass_t_ha, total_tonnes, min_biomass, max_biomass, scene_count, rate_source
                FROM biomass
                WHERE {" AND ".join(clauses)}
                ORDER BY village_name, `year`, crop
                """
            ),
            params,
        ).mappings().all()
    if not rows:
        raise HTTPException(status_code=404, detail="No biomass stored for this field. Rice and cotton in 2024 and 2025 are stored.")
    fields = [_biomass_item(row) for row in rows]
    if len(fields) == 1:
        return fields[0]
    return {"field_id": field_id, "count": len(fields), "fields": fields}


@router.get("/fields")
def biomass_fields(
    year: int = Query(ge=1900, le=2100),
    crop: str | None = None,
    subdistrict: str | None = None,
    village: str | None = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=PAGE_DEFAULT, ge=1, le=PAGE_MAX),
) -> dict[str, Any]:
    """One page of stored biomass rows.

    Pass only subdistrict to walk every field in that subdistrict.
    Keep requesting skip=next_skip until has_more is false.
    """
    ensure_biomass_schema()
    if crop and crop.strip().lower() not in {"rice", "cotton"}:
        raise HTTPException(status_code=400, detail="crop must be Rice or Cotton")
    where_sql, params = _filters(year, crop, village, subdistrict)
    with engine.connect() as conn:
        total = int(conn.execute(text(f"SELECT COUNT(*) FROM biomass WHERE {where_sql}"), params).scalar_one())
        rows = conn.execute(
            text(
                f"""
                SELECT crop_id, field_boundary_id, field_id, district_name, subdistrict_name, village_name,
                       crop, `year` AS year, observation_date, season_start, season_end, area_ha,
                       biomass_t_ha, total_tonnes, min_biomass, max_biomass, scene_count, rate_source
                FROM biomass
                WHERE {where_sql}
                ORDER BY village_name, crop_id
                LIMIT :limit OFFSET :skip
                """
            ),
            {**params, "limit": limit, "skip": skip},
        ).mappings().all()
    fields = []
    for row in rows:
        item = dict(row)
        for key in ("observation_date", "season_start", "season_end"):
            value = item[key]
            item[key] = value.isoformat() if hasattr(value, "isoformat") else value
        item["area_ha"] = None if item["area_ha"] is None else float(item["area_ha"])
        item["min_biomass"] = None if item["min_biomass"] is None else float(item["min_biomass"])
        item["max_biomass"] = None if item["max_biomass"] is None else float(item["max_biomass"])
        fields.append(item)
    return {**_page_meta(total, skip, limit, len(fields)), "fields": fields}


if __name__ == "__main__":
    import json

    print(json.dumps(store_biomass(), default=str), flush=True)
