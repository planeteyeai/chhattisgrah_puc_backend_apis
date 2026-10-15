"""Field biomass from stored village Sentinel-2 scenes, for every identified crop.

Biomass is Monteith light-use efficiency, summed scene by scene:

    dry matter (g/m2) = sum over days of PAR(month) x fAPAR(index) x RUE(crop) x stress
    t/ha = 0.01 x dry matter

Each crop has its own RUE, its own season window, and its own index. Rice,
maize, wheat, mustard, linseed and sugarcane use NDVI. Cotton, soybean and
the pulses use SAVI while NDVI is below 0.40 (sparse canopy), then NDVI.

A double crop such as "Rice + Wheat" is the Kharif crop over the Kharif window
plus the Rabi crop over the Rabi window. Both parts are stored on the row.

Windows, by Kharif year Y. The season ends on the latest clear scene inside
the end range:

    kharif     1 Jun Y   -> end 17 Jul Y .. 14 Nov Y   (60 days either side of 15 Sep)
    arhar      1 Jun Y   -> end 1 Dec Y .. 1 Mar Y+1   (Arhar grown alone, long duration)
    rabi       14 Nov Y  -> end 1 Feb Y+1 .. 16 Apr Y+1
    annual     1 Jun Y   -> end 1 Mar Y+1 .. 1 Jun Y+1 (Sugarcane)

Each scene covers the days until the next scene, capped at 16. Villages with
no usable scenes take the average t/ha of the villages that had scenes.
A field identified in both the rice-only and the all-crops season of the same
year is stored once, from the all-crops season.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, NamedTuple

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import text

from app.crop_calendar import CALENDAR_SEASON, parse_calendar_season
from app.database import engine


class CropProfile(NamedTuple):
    window: str
    rue: float
    sparse: bool


CROP_PROFILES: dict[str, CropProfile] = {
    "Rice": CropProfile("kharif", 2.2, False),
    "Maize": CropProfile("kharif", 3.3, False),
    "Soybean": CropProfile("kharif", 1.6, True),
    "Cotton": CropProfile("kharif", 1.8, True),
    "Arhar (Tur)": CropProfile("arhar", 1.4, True),
    "Sugarcane": CropProfile("annual", 3.2, False),
    "Wheat": CropProfile("rabi", 2.8, False),
    "Mustard": CropProfile("rabi", 2.0, False),
    "Linseed (Tisi)": CropProfile("rabi", 1.7, False),
    "Gram (Chana)": CropProfile("rabi", 1.5, True),
    "Lentil (Masoor)": CropProfile("rabi", 1.3, True),
}
# Daily PAR (MJ/m2/day) for Kabirdham, about 48% of global radiation at 22 N.
PAR_MJ = {1: 8.2, 2: 9.4, 3: 10.6, 4: 11.5, 5: 12.0, 6: 10.1, 7: 7.9, 8: 7.7, 9: 8.4, 10: 8.9, 11: 8.4, 12: 7.9}
STRESS = 1.0
MAX_SCENE_DAYS = 16
FAPAR_LOW = 0.0
FAPAR_HIGH = 0.95
SPARSE_NDVI_SWITCH = 0.40
SEASON_START_MONTH = 6
SEASON_START_DAY = 1
IDENTIFICATION_MONTH = 9
WINDOW_REACH_DAYS = 60
# Scale model output before storage; ±15% band is applied on the scaled exact value.
BIOMASS_SCALE = 0.64
BIOMASS_MIN_FACTOR = 0.85
BIOMASS_MAX_FACTOR = 1.15
SEASON_YEAR = re.compile(r"(\d{4})")

router = APIRouter(prefix="/biomass", tags=["biomass"])
_BIOMASS_SCHEMA_READY = False
PAGE_DEFAULT = 100
PAGE_MAX = 1000
_PLACES_CACHE: dict[str, dict[str, Any]] = {}
_COMPARE_CACHE: dict[tuple[str, ...], dict[str, Any]] = {}

Scenes = list[tuple[date, float, float | None]]


def _clear_biomass_cache() -> None:
    _PLACES_CACHE.clear()
    _COMPARE_CACHE.clear()


def parse_season(value: str | None) -> str | None:
    """Normalize to `kharif` / `rabi`, or None when both seasons are wanted."""
    return parse_calendar_season(value)


def parse_seasons(values: list[str] | tuple[str, ...] | None) -> tuple[str, ...] | None:
    """CLI / store helper. None means both kharif and rabi."""
    if not values:
        return None
    seasons = []
    for value in values:
        for part in str(value).split(","):
            season = parse_season(part)
            if season and season not in seasons:
                seasons.append(season)
    return tuple(seasons) or None


def crops_for_season(season: str | None) -> frozenset[str] | None:
    if season is None:
        return None
    return frozenset(crop for crop, bucket in CALENDAR_SEASON.items() if bucket == season)


def season_start(year: int) -> date:
    return date(year, SEASON_START_MONTH, SEASON_START_DAY)


def identification_window(year: int, month: int = IDENTIFICATION_MONTH) -> tuple[date, date]:
    """Start is included. End is excluded. The 15th is only the middle of the window."""
    center = date(year, month, 15)
    reach = timedelta(days=WINDOW_REACH_DAYS)
    return center - reach, center + reach


def season_window(year: int, window: str) -> tuple[date, date, date]:
    """Season start, then the range [from, to) that must hold the last scene."""
    kharif_from, kharif_to = identification_window(year)
    if window == "kharif":
        return season_start(year), kharif_from, kharif_to
    if window == "arhar":
        return season_start(year), date(year, 12, 1), date(year + 1, 3, 1)
    if window == "rabi":
        return kharif_to, date(year + 1, 2, 1), date(year + 1, 4, 16)
    if window == "annual":
        return season_start(year), date(year + 1, 3, 1), date(year + 1, 6, 1)
    raise ValueError(f"unknown window {window}")


_EXCLUDED_BIOMASS_LABELS = frozenset(
    {"fallow/uncertain", "tree", "subpixel", "uncertain", "unclassified"}
)


def crop_components(
    label: str | None,
    seasons: tuple[str, ...] | None = None,
) -> list[tuple[str, str]]:
    """(crop, window) parts of an identified label. Empty when the label has no biomass.

    When `seasons` is set, only Chhattisgarh calendar crops for those seasons are kept.
    """
    text = (label or "").strip()
    if not text or text.casefold() in _EXCLUDED_BIOMASS_LABELS:
        return []
    parts = [part.strip() for part in text.split(" + ") if part.strip()]
    if not parts or len(parts) > 2 or any(part not in CROP_PROFILES for part in parts):
        return []
    if len(parts) == 1:
        components = [(parts[0], CROP_PROFILES[parts[0]].window)]
    else:
        components = [(parts[0], "kharif"), (parts[1], "rabi")]
    if seasons:
        allowed = set(seasons)
        components = [
            (crop, window)
            for crop, window in components
            if CALENDAR_SEASON.get(crop) in allowed
        ]
    return components


def season_year(season: str | None, observed: date | None) -> int | None:
    match = SEASON_YEAR.search(season or "")
    if match:
        return int(match.group(1))
    return observed.year if observed else None


def latest_scene_date(scenes: Scenes, start: date, end: date) -> date | None:
    found = [obs for obs, ndvi, _savi in scenes if ndvi is not None and start <= obs < end]
    return max(found) if found else None


def scale_biomass(value: float) -> float:
    """Apply the storage scale factor to an exact biomass value (t/ha or tonnes)."""
    return float(value or 0.0) * BIOMASS_SCALE


def biomass_bounds(total_tonnes: float) -> tuple[float, float]:
    """15% below and 15% above the stored (already scaled) biomass, in tonnes."""
    total = float(total_tonnes or 0.0)
    return round(total * BIOMASS_MIN_FACTOR, 6), round(total * BIOMASS_MAX_FACTOR, 6)


def fapar(index: float) -> float:
    value = 1.24 * index - 0.16
    return min(FAPAR_HIGH, max(FAPAR_LOW, value))


def absorption_index(profile: CropProfile, ndvi: float, savi: float | None) -> float | None:
    """NDVI, except sparse crops use SAVI while NDVI is below 0.40."""
    if profile.sparse and ndvi < SPARSE_NDVI_SWITCH:
        return savi
    return ndvi


def daily_growth(fapar_value: float, profile: CropProfile, day: date) -> float:
    """Grams of dry matter per square metre on one day."""
    return PAR_MJ[day.month] * fapar_value * profile.rue * STRESS


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


def biomass_t_ha(scenes: Scenes, start: date, season_end: date, crop: str) -> tuple[float, int] | None:
    """Tonnes per hectare of `crop` from `start` through `season_end`, and the scene count."""
    profile = CROP_PROFILES[crop]
    usable: list[tuple[date, float]] = []
    for obs_date, ndvi, savi in scenes:
        if obs_date < start or obs_date > season_end:
            continue
        index = absorption_index(profile, ndvi, savi)
        if index is None:
            continue
        usable.append((obs_date, index))
    if not usable:
        return None
    usable.sort(key=lambda item: item[0])
    dry_matter = 0.0
    for position, (obs_date, absorption) in enumerate(usable):
        next_date = usable[position + 1][0] if position + 1 < len(usable) else None
        fraction = fapar(absorption)
        for offset in range(scene_days(obs_date, next_date, season_end)):
            dry_matter += daily_growth(fraction, profile, obs_date + timedelta(days=offset))
    return 0.01 * dry_matter, len(usable)


def crop_rates(
    scenes_by_village: dict[str, Scenes],
    year: int,
    crop: str,
    window: str,
) -> tuple[dict[str, tuple[float, int, date, date]], tuple[float, date, date] | None]:
    """Per-village (t/ha, scenes, start, end), plus the (average t/ha, start, end) fallback."""
    start, end_from, end_to = season_window(year, window)
    measured: dict[str, tuple[float, int, date, date]] = {}
    for village, scenes in scenes_by_village.items():
        season_end = latest_scene_date(scenes, end_from, end_to)
        if season_end is None:
            continue
        result = biomass_t_ha(scenes, start, season_end, crop)
        if result is None:
            continue
        measured[village] = (result[0], result[1], start, season_end)
    if not measured:
        return measured, None
    average = sum(item[0] for item in measured.values()) / len(measured)
    return measured, (average, start, max(item[3] for item in measured.values()))


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
                  season VARCHAR(64) NULL,
                  crop VARCHAR(64) NOT NULL,
                  kharif_crop VARCHAR(32) NULL,
                  kharif_t_ha DOUBLE NULL,
                  kharif_tonnes DOUBLE NULL,
                  rabi_crop VARCHAR(32) NULL,
                  rabi_t_ha DOUBLE NULL,
                  rabi_tonnes DOUBLE NULL,
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
            str(name).lower(): str(kind).lower()
            for name, kind in conn.execute(
                text(
                    """
                    SELECT COLUMN_NAME, COLUMN_TYPE
                    FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'biomass'
                    """
                )
            )
        }
        added_bounds = False
        if "min_biomass" not in columns:
            conn.execute(text("ALTER TABLE biomass ADD COLUMN min_biomass DOUBLE NULL AFTER total_tonnes"))
            added_bounds = True
        if "max_biomass" not in columns:
            conn.execute(text("ALTER TABLE biomass ADD COLUMN max_biomass DOUBLE NULL AFTER min_biomass"))
            added_bounds = True
        if added_bounds:
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
        if columns.get("crop") != "varchar(64)":
            conn.execute(text("ALTER TABLE biomass MODIFY COLUMN crop VARCHAR(64) NOT NULL"))
        for name, ddl, after in (
            ("season", "VARCHAR(64) NULL", "village_name"),
            ("kharif_crop", "VARCHAR(32) NULL", "crop"),
            ("kharif_t_ha", "DOUBLE NULL", "kharif_crop"),
            ("kharif_tonnes", "DOUBLE NULL", "kharif_t_ha"),
            ("rabi_crop", "VARCHAR(32) NULL", "kharif_tonnes"),
            ("rabi_t_ha", "DOUBLE NULL", "rabi_crop"),
            ("rabi_tonnes", "DOUBLE NULL", "rabi_t_ha"),
        ):
            if name not in columns:
                conn.execute(text(f"ALTER TABLE biomass ADD COLUMN {name} {ddl} AFTER {after}"))
        conn.execute(
            text(
                """
                UPDATE biomass
                SET kharif_crop = crop, kharif_t_ha = biomass_t_ha, kharif_tonnes = total_tonnes
                WHERE kharif_crop IS NULL AND rabi_crop IS NULL AND crop IN ('Rice', 'Cotton')
                """
            )
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


def _load_scenes(start: date, end: date) -> dict[str, Scenes]:
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
    grouped: dict[str, Scenes] = {}
    with engine.connect() as conn:
        rows = conn.execute(text(sql), {"start": start, "end": end}).mappings()
        for row in rows:
            savi = None if row["savi"] is None else float(row["savi"])
            grouped.setdefault(row["village"], []).append((row["obs_date"], float(row["ndvi"]), savi))
    return grouped


def _load_fields() -> list[dict[str, Any]]:
    """One identification per field and year. The all-crops season wins over rice-only."""
    sql = """
        SELECT id, field_boundary_id, field_id, district_name, subdistrict_name, village_name,
               final_label, season, observation_date, area_ha
        FROM crop_identification
        WHERE final_label IS NOT NULL
        ORDER BY id
    """
    with engine.connect() as conn:
        rows = [dict(row) for row in conn.execute(text(sql)).mappings()]
    chosen: dict[tuple[int, Any], dict[str, Any]] = {}
    for row in rows:
        year = season_year(row["season"], row["observation_date"])
        if year is None:
            continue
        row["year"] = year
        key = (year, row["field_boundary_id"] if row["field_boundary_id"] is not None else f"id:{row['id']}")
        current = chosen.get(key)
        if current is None or ("Rabi" in (row["season"] or "") and "Rabi" not in (current["season"] or "")):
            chosen[key] = row
    return list(chosen.values())


def store_biomass(
    years: tuple[int, ...] | None = None,
    seasons: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Replace stored biomass for the requested Kharif years and calendar seasons.

    `years` are Kharif years (e.g. 2023 means Kharif 2023 and Rabi 2023-24).
    `seasons` is `kharif`, `rabi`, or both (default). Only matching calendar crops are stored.
    """
    ensure_biomass_schema()
    seasons = parse_seasons(list(seasons) if seasons else None)
    fields = _load_fields()
    if years is None:
        years = tuple(sorted({row["year"] for row in fields}))
    if not years:
        return {"years": [], "seasons": list(seasons or ("kharif", "rabi")), "stored": 0, "skipped": {}, "results": []}
    fields = [row for row in fields if row["year"] in years]
    scenes = _load_scenes(season_start(min(years)), date(max(years) + 1, 6, 1))
    created_at = datetime.now(timezone.utc).replace(tzinfo=None)
    rate_cache: dict[tuple[int, str, str], tuple[dict[str, tuple[float, int, date, date]], tuple[float, date, date] | None]] = {}

    def rate(year: int, village: str, crop: str, window: str) -> tuple[float, int, date, date, str] | None:
        key = (year, crop, window)
        if key not in rate_cache:
            rate_cache[key] = crop_rates(scenes, year, crop, window)
        measured, fallback = rate_cache[key]
        hit = measured.get(village)
        if hit is not None:
            return (*hit, "scenes")
        if fallback is not None:
            return fallback[0], 0, fallback[1], fallback[2], "village_average"
        return None

    payload = []
    skipped: dict[str, int] = {}
    summary: dict[tuple[int, str], dict[str, Any]] = {}
    for row in fields:
        label = row["final_label"]
        components = crop_components(label, seasons)
        year = row["year"]
        village_key = (row["village_name"] or "").strip().lower()
        parts = []
        for crop, window in components:
            found = rate(year, village_key, crop, window)
            if found is not None:
                parts.append((crop, window, *found))
        if not parts:
            reason = label if not components else f"{label} (no scenes yet)"
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        area = float(row["area_ha"] or 0)
        kharif = next((part for part in parts if CALENDAR_SEASON.get(part[0]) == "kharif"), None)
        rabi = next((part for part in parts if CALENDAR_SEASON.get(part[0]) == "rabi"), None)
        # Exact stored values are model output × BIOMASS_SCALE; ±15% uses those scaled figures.
        tonnes_per_ha = scale_biomass(sum(part[2] for part in parts))
        total = round(tonnes_per_ha * area, 6)
        min_biomass, max_biomass = biomass_bounds(total)
        kharif_t_ha = scale_biomass(kharif[2]) if kharif else None
        rabi_t_ha = scale_biomass(rabi[2]) if rabi else None
        sources = {part[6] for part in parts}
        source = sources.pop() if len(sources) == 1 else "mixed"
        season_end = max(part[5] for part in parts)
        payload.append(
            {
                "crop_id": int(row["id"]),
                "field_boundary_id": row["field_boundary_id"],
                "field_id": row["field_id"],
                "district_name": row["district_name"],
                "subdistrict_name": row["subdistrict_name"],
                "village_name": row["village_name"],
                "season": row["season"],
                "crop": label,
                "kharif_crop": kharif[0] if kharif else None,
                "kharif_t_ha": round(kharif_t_ha, 6) if kharif_t_ha is not None else None,
                "kharif_tonnes": round(kharif_t_ha * area, 6) if kharif_t_ha is not None else None,
                "rabi_crop": rabi[0] if rabi else None,
                "rabi_t_ha": round(rabi_t_ha, 6) if rabi_t_ha is not None else None,
                "rabi_tonnes": round(rabi_t_ha * area, 6) if rabi_t_ha is not None else None,
                "year": year,
                "observation_date": season_end,
                "season_start": min(part[4] for part in parts),
                "season_end": season_end,
                "area_ha": area,
                "biomass_t_ha": round(tonnes_per_ha, 6),
                "total_tonnes": total,
                "min_biomass": min_biomass,
                "max_biomass": max_biomass,
                "scene_count": sum(part[3] for part in parts),
                "rate_source": source,
                "created_at": created_at,
            }
        )
        bucket = summary.setdefault(
            (year, label),
            {"year": year, "crop": label, "fields": 0, "area_ha": 0.0, "total_tonnes": 0.0, "from_scenes": 0, "from_average": 0},
        )
        bucket["fields"] += 1
        bucket["area_ha"] += area
        bucket["total_tonnes"] += total
        if source == "scenes":
            bucket["from_scenes"] += 1
        else:
            bucket["from_average"] += 1

    insert_sql = text(
        """
        INSERT INTO biomass (
          crop_id, field_boundary_id, field_id, district_name, subdistrict_name, village_name, season,
          crop, kharif_crop, kharif_t_ha, kharif_tonnes, rabi_crop, rabi_t_ha, rabi_tonnes,
          `year`, observation_date, season_start, season_end, area_ha,
          biomass_t_ha, total_tonnes, min_biomass, max_biomass, scene_count, rate_source, created_at
        ) VALUES (
          :crop_id, :field_boundary_id, :field_id, :district_name, :subdistrict_name, :village_name, :season,
          :crop, :kharif_crop, :kharif_t_ha, :kharif_tonnes, :rabi_crop, :rabi_t_ha, :rabi_tonnes,
          :year, :observation_date, :season_start, :season_end, :area_ha,
          :biomass_t_ha, :total_tonnes, :min_biomass, :max_biomass, :scene_count, :rate_source, :created_at
        )
        """
    )
    with engine.begin() as conn:
        for year in years:
            conn.execute(text("DELETE FROM biomass WHERE `year` = :year"), {"year": year})
        for start in range(0, len(payload), 1000):
            conn.execute(insert_sql, payload[start : start + 1000])
    rows = []
    for bucket in summary.values():
        bucket["area_ha"] = round(bucket["area_ha"], 2)
        bucket["total_tonnes"] = round(bucket["total_tonnes"], 2)
        rows.append(bucket)
    rows.sort(key=lambda item: (item["year"], -item["total_tonnes"]))
    _clear_biomass_cache()
    return {
        "years": list(years),
        "seasons": list(seasons or ("kharif", "rabi")),
        "stored": len(payload),
        "skipped": skipped,
        "results": rows,
    }


def _components_sql(where_sql: str, season: str | None = None) -> str:
    """Season crop parts as separate rows, so each crop can be summed on its own."""
    parts = []
    if season in (None, "kharif"):
        parts.append(
            f"""
            SELECT `year`, village_name, kharif_crop AS crop, area_ha, kharif_tonnes AS tonnes
            FROM biomass WHERE kharif_crop IS NOT NULL AND {where_sql}
            """
        )
    if season in (None, "rabi"):
        parts.append(
            f"""
            SELECT `year`, village_name, rabi_crop AS crop, area_ha, rabi_tonnes AS tonnes
            FROM biomass WHERE rabi_crop IS NOT NULL AND {where_sql}
            """
        )
    return " UNION ALL ".join(parts)


def _season_totals_sql(where_sql: str, season: str | None) -> str:
    """Village/year totals for one calendar season, or both when season is None."""
    if season == "kharif":
        return f"""
            SELECT village_name, `year` AS year,
                   SUM(CASE WHEN kharif_crop IS NOT NULL THEN 1 ELSE 0 END) AS fields,
                   COALESCE(SUM(CASE WHEN kharif_crop IS NOT NULL THEN area_ha ELSE 0 END), 0) AS area_ha,
                   COALESCE(SUM(COALESCE(kharif_tonnes, 0)), 0) AS total_tonnes
            FROM biomass
            WHERE {where_sql} AND kharif_crop IS NOT NULL
            GROUP BY village_name, `year`
        """
    if season == "rabi":
        return f"""
            SELECT village_name, `year` AS year,
                   SUM(CASE WHEN rabi_crop IS NOT NULL THEN 1 ELSE 0 END) AS fields,
                   COALESCE(SUM(CASE WHEN rabi_crop IS NOT NULL THEN area_ha ELSE 0 END), 0) AS area_ha,
                   COALESCE(SUM(COALESCE(rabi_tonnes, 0)), 0) AS total_tonnes
            FROM biomass
            WHERE {where_sql} AND rabi_crop IS NOT NULL
            GROUP BY village_name, `year`
        """
    return f"""
        SELECT village_name, `year` AS year,
               COUNT(*) AS fields,
               COALESCE(SUM(area_ha), 0) AS area_ha,
               COALESCE(SUM(total_tonnes), 0) AS total_tonnes
        FROM biomass
        WHERE {where_sql}
        GROUP BY village_name, `year`
    """


def _filters(
    year: int | None,
    crop: str | None,
    village: str | None,
    subdistrict: str | None = None,
    season: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """`crop` matches the identified label or either part, so Wheat includes Rice + Wheat."""
    clauses = ["1=1"]
    params: dict[str, Any] = {}
    if year is not None:
        clauses.append("`year` = :year")
        params["year"] = year
    if crop:
        clauses.append("(LOWER(crop) = LOWER(:crop) OR LOWER(kharif_crop) = LOWER(:crop) OR LOWER(rabi_crop) = LOWER(:crop))")
        params["crop"] = crop.strip()
    if season == "kharif":
        clauses.append("kharif_crop IS NOT NULL")
    elif season == "rabi":
        clauses.append("rabi_crop IS NOT NULL")
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


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


@router.get("/crops")
def biomass_crops(season: str | None = Query(default=None)) -> dict[str, Any]:
    """The biomass formula settings for every crop (Chhattisgarh crop calendar)."""
    season = parse_season(season)
    allowed = crops_for_season(season)
    crops = []
    for crop, profile in CROP_PROFILES.items():
        calendar = CALENDAR_SEASON[crop]
        if allowed is not None and crop not in allowed:
            continue
        crops.append(
            {
                "crop": crop,
                "season": calendar,
                "rue_g_per_mj": profile.rue,
                "window": profile.window,
                "index": f"SAVI while NDVI < {SPARSE_NDVI_SWITCH}, then NDVI" if profile.sparse else "NDVI",
            }
        )
    return {
        "formula": "t/ha = 0.01 x sum(PAR(month) x fAPAR x RUE x stress) over the crop's season",
        "fapar": "1.24 x index - 0.16, kept between 0 and 0.95",
        "par_mj_per_day": PAR_MJ,
        "stress": STRESS,
        "season": season,
        "calendar": {
            "kharif": sorted(crop for crop, bucket in CALENDAR_SEASON.items() if bucket == "kharif"),
            "rabi": sorted(crop for crop, bucket in CALENDAR_SEASON.items() if bucket == "rabi"),
        },
        "crops": crops,
        "double_crops": "Kharif crop over the Kharif window plus Rabi crop over the Rabi window",
    }


@router.get("/summary")
def biomass_summary(
    year: int | None = Query(default=None, ge=1900, le=2100),
    season: str | None = Query(default=None),
    crop: str | None = None,
) -> dict[str, Any]:
    """Stored biomass totals per identified label, plus totals per single crop."""
    ensure_biomass_schema()
    season = parse_season(season)
    where_sql, params = _filters(year, crop, None, season=season)
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
                       SUM(rate_source <> 'scenes') AS fields_from_average
                FROM biomass
                WHERE {where_sql}
                GROUP BY `year`, crop
                ORDER BY `year`, total_tonnes DESC
                """
            ),
            params,
        ).mappings().all()
        part_where, part_params = _filters(year, None, None, season=season)
        part_sql = f"SELECT * FROM ({_components_sql(part_where, season)}) parts"
        if crop:
            part_sql += " WHERE LOWER(crop) = LOWER(:crop)"
            part_params["crop"] = crop.strip()
        parts = conn.execute(
            text(
                f"""
                SELECT `year` AS year, crop,
                       COUNT(*) AS fields,
                       COUNT(DISTINCT village_name) AS villages,
                       ROUND(SUM(area_ha), 2) AS area_ha,
                       ROUND(SUM(tonnes), 2) AS total_tonnes,
                       ROUND(SUM(tonnes) / NULLIF(SUM(area_ha), 0), 4) AS biomass_t_ha
                FROM ({part_sql}) picked
                GROUP BY `year`, crop
                ORDER BY `year`, total_tonnes DESC
                """
            ),
            part_params,
        ).mappings().all()
    results = []
    for row in rows:
        item = dict(row)
        for key in ("fields", "villages", "fields_from_scenes", "fields_from_average"):
            item[key] = int(item[key] or 0)
        for key in ("area_ha", "total_tonnes", "min_biomass", "max_biomass", "biomass_t_ha"):
            item[key] = _float(item[key])
        results.append(item)
    by_crop = []
    allowed = crops_for_season(season)
    for row in parts:
        if allowed is not None and row["crop"] not in allowed:
            continue
        item = dict(row)
        item["fields"] = int(item["fields"] or 0)
        item["villages"] = int(item["villages"] or 0)
        item["season"] = CALENDAR_SEASON.get(row["crop"])
        for key in ("area_ha", "total_tonnes", "biomass_t_ha"):
            item[key] = _float(item[key])
        item["min_biomass"], item["max_biomass"] = biomass_bounds(item["total_tonnes"] or 0)
        by_crop.append(item)
    return {"count": len(results), "season": season, "results": results, "by_crop": by_crop}


@router.get("/village")
def biomass_village(
    village: str = Query(min_length=1),
    year: int = Query(ge=1900, le=2100),
    season: str | None = Query(default=None),
    crop: str | None = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=PAGE_DEFAULT, ge=1, le=PAGE_MAX),
) -> dict[str, Any]:
    """Village totals, plus one page of field rows."""
    ensure_biomass_schema()
    season = parse_season(season)
    where_sql, params = _filters(year, crop, village, season=season)
    if season == "kharif":
        tonnes_expr = "COALESCE(SUM(COALESCE(kharif_tonnes, 0)), 0)"
        area_expr = "COALESCE(SUM(CASE WHEN kharif_crop IS NOT NULL THEN area_ha ELSE 0 END), 0)"
    elif season == "rabi":
        tonnes_expr = "COALESCE(SUM(COALESCE(rabi_tonnes, 0)), 0)"
        area_expr = "COALESCE(SUM(CASE WHEN rabi_crop IS NOT NULL THEN area_ha ELSE 0 END), 0)"
    else:
        tonnes_expr = "COALESCE(SUM(total_tonnes), 0)"
        area_expr = "COALESCE(SUM(area_ha), 0)"
    with engine.connect() as conn:
        totals = conn.execute(
            text(
                f"""
                SELECT COUNT(*) AS fields,
                       {area_expr} AS area_ha,
                       {tonnes_expr} AS total_tonnes
                FROM biomass
                WHERE {where_sql}
                """
            ),
            params,
        ).mappings().one()
        rows = conn.execute(
            text(
                f"""
                SELECT crop_id, field_id, village_name, season, crop, kharif_crop, kharif_tonnes,
                       rabi_crop, rabi_tonnes, `year` AS year, area_ha,
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
        for key in ("area_ha", "biomass_t_ha", "total_tonnes", "min_biomass", "max_biomass", "kharif_tonnes", "rabi_tonnes"):
            item[key] = _float(item[key])
        viewed = _season_field_view(item, season)
        if season == "kharif" and not item.get("kharif_crop"):
            continue
        if season == "rabi" and not item.get("rabi_crop"):
            continue
        fields.append(viewed)
    total = int(totals["fields"] or 0)
    tonnes = round(float(totals["total_tonnes"] or 0), 4)
    low, high = biomass_bounds(tonnes)
    return {
        "village": village,
        "year": year,
        "season": season,
        "crop": crop,
        "total": total,
        "fields": total,
        "skip": skip,
        "limit": limit,
        "count": len(fields),
        "area_ha": round(float(totals["area_ha"] or 0), 4),
        "total_tonnes": tonnes,
        "min_biomass": round(low, 4),
        "max_biomass": round(high, 4),
        "records": fields,
    }


def _empty_amount() -> dict[str, Any]:
    return {"fields": 0, "tonnes": 0.0, "area_ha": 0.0, "min_biomass": 0.0, "max_biomass": 0.0}


def _empty_year() -> dict[str, Any]:
    return {**_empty_amount(), "crops": {}, "rice": _empty_amount(), "cotton": _empty_amount()}


def _round_amount(bucket: dict[str, Any]) -> None:
    for key in ("tonnes", "area_ha", "min_biomass", "max_biomass"):
        bucket[key] = round(bucket[key], 2)


def _stored_years(conn: Any) -> list[str]:
    return [str(int(year)) for year in conn.execute(text("SELECT DISTINCT `year` FROM biomass ORDER BY `year`")).scalars()]


@router.get("/places")
def biomass_places(season: str | None = Query(default=None)) -> dict[str, Any]:
    """Biomass totals for every village and every stored year, split by crop.

    Pass `season=kharif` or `season=rabi` to return only that Chhattisgarh calendar
    season's crops. `crops` sums each crop's own part, so a Rice + Wheat field adds
    to Rice under kharif and to Wheat under rabi.
    """
    season = parse_season(season)
    cache_key = season or ""
    cached = _PLACES_CACHE.get(cache_key)
    if cached is not None:
        return cached
    ensure_biomass_schema()
    allowed = crops_for_season(season)
    with engine.connect() as conn:
        years = _stored_years(conn)
        totals = conn.execute(text(_season_totals_sql("1=1", season))).mappings().all()
        parts = conn.execute(
            text(
                f"""
                SELECT village_name, `year` AS year, crop,
                       COUNT(*) AS fields,
                       COALESCE(SUM(area_ha), 0) AS area_ha,
                       COALESCE(SUM(tonnes), 0) AS tonnes
                FROM ({_components_sql("1=1", season)}) parts
                GROUP BY village_name, `year`, crop
                """
            )
        ).mappings().all()
    places: dict[str, dict[str, Any]] = {}

    def year_bucket(name: str, year: Any) -> dict[str, Any]:
        place = places.setdefault(name, {"village": name, **{key: _empty_year() for key in years}})
        return place[str(int(year))]

    for row in totals:
        bucket = year_bucket(row["village_name"] or "", row["year"])
        tonnes = float(row["total_tonnes"] or 0)
        low, high = biomass_bounds(tonnes)
        bucket["fields"] += int(row["fields"] or 0)
        bucket["tonnes"] += tonnes
        bucket["area_ha"] += float(row["area_ha"] or 0)
        bucket["min_biomass"] += low
        bucket["max_biomass"] += high
    for row in parts:
        crop_name = row["crop"]
        if allowed is not None and crop_name not in allowed:
            continue
        bucket = year_bucket(row["village_name"] or "", row["year"])
        tonnes = float(row["tonnes"] or 0)
        low, high = biomass_bounds(tonnes)
        amount = bucket["crops"].setdefault(crop_name, _empty_amount())
        amount["fields"] += int(row["fields"] or 0)
        amount["tonnes"] += tonnes
        amount["area_ha"] += float(row["area_ha"] or 0)
        amount["min_biomass"] += low
        amount["max_biomass"] += high
    ordered = sorted(places.values(), key=lambda item: item["village"].lower())
    for place in ordered:
        for year_key in years:
            bucket = place[year_key]
            _round_amount(bucket)
            for amount in bucket["crops"].values():
                _round_amount(amount)
            bucket["crops"] = dict(sorted(bucket["crops"].items(), key=lambda item: -item[1]["tonnes"]))
            bucket["rice"] = bucket["crops"].get("Rice", _empty_amount())
            bucket["cotton"] = bucket["crops"].get("Cotton", _empty_amount())
    payload = {
        "count": len(ordered),
        "season": season,
        "years": years,
        "crops": sorted(allowed) if allowed is not None else sorted(CALENDAR_SEASON),
        "villages": ordered,
    }
    _PLACES_CACHE[cache_key] = payload
    return payload


def _year_change(before: dict[str, Any] | None, after: dict[str, Any]) -> dict[str, Any]:
    if before is None:
        return {"change_tonnes": None, "change_pct": None, "change_t_ha_pct": None}
    diff = after["tonnes"] - before["tonnes"]
    rate_before, rate_after = before["biomass_t_ha"], after["biomass_t_ha"]
    return {
        "change_tonnes": round(diff, 4),
        "change_pct": round(diff / before["tonnes"] * 100, 2) if before["tonnes"] else None,
        "change_t_ha_pct": round((rate_after - rate_before) / rate_before * 100, 2) if rate_before and rate_after is not None else None,
    }


def _year_amount(year: str, fields: int, area: float, tonnes: float, seasons: str | None = None) -> dict[str, Any]:
    low, high = biomass_bounds(tonnes)
    amount = {
        "year": int(year),
        "fields": fields,
        "area_ha": round(area, 4),
        "tonnes": round(tonnes, 4),
        "min_biomass": round(low, 4),
        "max_biomass": round(high, 4),
        "biomass_t_ha": round(tonnes / area, 4) if area else None,
    }
    if seasons is not None:
        amount["seasons"] = [season for season in seasons.split(",") if season]
    return amount


def _with_changes(amounts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Each year against the previous year that had biomass. Years with no fields have no change."""
    previous = None
    for amount in amounts:
        amount.update(_year_change(previous if amount["fields"] else None, amount))
        if amount["fields"]:
            previous = amount
    return amounts


@router.get("/compare")
def biomass_compare(
    village: str = Query(min_length=1),
    season: str | None = Query(default=None),
    crop: str | None = None,
) -> dict[str, Any]:
    """Whole-village biomass year by year, in total and per crop.

    Pass `season=kharif` or `season=rabi` for that calendar season only.
    Fields are not matched across years. Each year is the sum of every field stored
    for the village that year. `biomass_t_ha` is tonnes over area, which stays
    comparable when a year has more or fewer identified fields.
    """
    season = parse_season(season)
    key = (village, season or "", (crop or "").strip().lower())
    cached = _COMPARE_CACHE.get(key)
    if cached is not None:
        return cached
    ensure_biomass_schema()
    where_sql, params = _filters(None, None, village, season=season)
    allowed = crops_for_season(season)
    with engine.connect() as conn:
        years = _stored_years(conn)
        total_rows = conn.execute(
            text(
                f"""
                SELECT `year` AS year, fields, area_ha, total_tonnes AS tonnes
                FROM ({_season_totals_sql(where_sql, season)}) season_totals
                """
            ),
            params,
        ).mappings().all()
        part_rows = conn.execute(
            text(
                f"""
                SELECT `year` AS year, crop, COUNT(*) AS fields,
                       COALESCE(SUM(area_ha), 0) AS area_ha,
                       COALESCE(SUM(tonnes), 0) AS tonnes
                FROM ({_components_sql(where_sql, season)}) parts
                GROUP BY `year`, crop
                """
            ),
            params,
        ).mappings().all()
    if not total_rows:
        raise HTTPException(status_code=404, detail=f"No biomass stored for village {village}")
    by_year = {str(int(row["year"])): row for row in total_rows}
    totals = []
    for year in years:
        row = by_year.get(year)
        if row is None:
            totals.append(_year_amount(year, 0, 0.0, 0.0, season or ""))
        else:
            totals.append(
                _year_amount(
                    year,
                    int(row["fields"]),
                    float(row["area_ha"]),
                    float(row["tonnes"]),
                    season or "",
                )
            )
    per_crop: dict[str, dict[str, Any]] = {}
    for row in part_rows:
        name = row["crop"]
        if allowed is not None and name not in allowed:
            continue
        per_crop.setdefault(name, {})[str(int(row["year"]))] = row
    crops = []
    for name, rows in per_crop.items():
        if crop and name.lower() != crop.strip().lower():
            continue
        amounts = []
        for year in years:
            row = rows.get(year)
            if row is None:
                amounts.append(_year_amount(year, 0, 0.0, 0.0))
            else:
                amounts.append(_year_amount(year, int(row["fields"]), float(row["area_ha"]), float(row["tonnes"])))
        crops.append({"crop": name, "season": CALENDAR_SEASON.get(name), "years": _with_changes(amounts)})
    crops.sort(key=lambda item: -sum(amount["tonnes"] for amount in item["years"]))
    payload = {
        "village": village,
        "season": season,
        "years": [int(year) for year in years],
        "totals": _with_changes(totals),
        "crops": crops,
    }
    _COMPARE_CACHE[key] = payload
    return payload


def _biomass_item(row: Any) -> dict[str, Any]:
    item = dict(row)
    for key in ("observation_date", "season_start", "season_end"):
        value = item[key]
        item[key] = value.isoformat() if hasattr(value, "isoformat") else value
    for key in ("area_ha", "biomass_t_ha", "total_tonnes", "min_biomass", "max_biomass", "kharif_t_ha", "kharif_tonnes", "rabi_t_ha", "rabi_tonnes"):
        if key in item:
            item[key] = _float(item[key])
    return item


FIELD_COLUMNS = """
    crop_id, field_boundary_id, field_id, district_name, subdistrict_name, village_name, season,
    crop, kharif_crop, kharif_t_ha, kharif_tonnes, rabi_crop, rabi_t_ha, rabi_tonnes,
    `year` AS year, observation_date, season_start, season_end, area_ha,
    biomass_t_ha, total_tonnes, min_biomass, max_biomass, scene_count, rate_source
"""


def _season_field_view(item: dict[str, Any], season: str | None) -> dict[str, Any]:
    """Expose one calendar season's crop name and tonnes on a biomass row (no double labels)."""
    if season == "kharif":
        crop = item.get("kharif_crop")
        tonnes = float(item.get("kharif_tonnes") or 0)
        if not crop:
            return item
        low, high = biomass_bounds(tonnes)
        area = float(item.get("area_ha") or 0)
        return {
            **item,
            "season": "kharif",
            "crop": crop,
            "total_tonnes": tonnes,
            "biomass_t_ha": round(tonnes / area, 6) if area else item.get("biomass_t_ha"),
            "min_biomass": low,
            "max_biomass": high,
        }
    if season == "rabi":
        crop = item.get("rabi_crop")
        tonnes = float(item.get("rabi_tonnes") or 0)
        if not crop:
            return item
        low, high = biomass_bounds(tonnes)
        area = float(item.get("area_ha") or 0)
        return {
            **item,
            "season": "rabi",
            "crop": crop,
            "total_tonnes": tonnes,
            "biomass_t_ha": round(tonnes / area, 6) if area else item.get("biomass_t_ha"),
            "min_biomass": low,
            "max_biomass": high,
        }
    return item


@router.get("/field")
def biomass_field(
    field_id: int | None = Query(default=None),
    village: str | None = None,
    year: int | None = Query(default=None, ge=1900, le=2100),
    season: str | None = Query(default=None),
    crop: str | None = None,
    crop_id: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    """Biomass for one plot. Pass field_id. Add village, year, and season when needed.

    With `season=kharif` or `season=rabi`, `crop` is the season crop only (never `Rice + Wheat`)
    and tonnes / min / max are that season's part.
    """
    if field_id is None and crop_id is None:
        raise HTTPException(status_code=400, detail="field_id is required")
    ensure_biomass_schema()
    season = parse_season(season)
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
    if season == "kharif":
        clauses.append("kharif_crop IS NOT NULL")
    elif season == "rabi":
        clauses.append("rabi_crop IS NOT NULL")
    if crop:
        clauses.append("(LOWER(crop) = LOWER(:crop) OR LOWER(kharif_crop) = LOWER(:crop) OR LOWER(rabi_crop) = LOWER(:crop))")
        params["crop"] = crop.strip()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"""
                SELECT {FIELD_COLUMNS}
                FROM biomass
                WHERE {" AND ".join(clauses)}
                ORDER BY village_name, `year`, crop
                """
            ),
            params,
        ).mappings().all()
    if not rows:
        raise HTTPException(status_code=404, detail="No biomass stored for this field.")
    fields = [_season_field_view(_biomass_item(row), season) for row in rows]
    if len(fields) == 1:
        return fields[0]
    return {"field_id": field_id, "season": season, "count": len(fields), "fields": fields}


@router.get("/fields")
def biomass_fields(
    year: int = Query(ge=1900, le=2100),
    season: str | None = Query(default=None),
    crop: str | None = None,
    subdistrict: str | None = None,
    village: str | None = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=PAGE_DEFAULT, ge=1, le=PAGE_MAX),
) -> dict[str, Any]:
    """One page of stored biomass rows.

    Pass `season=kharif` or `season=rabi` for that calendar season only.
    Pass only subdistrict to walk every field in that subdistrict.
    Keep requesting skip=next_skip until has_more is false.
    """
    ensure_biomass_schema()
    season = parse_season(season)
    where_sql, params = _filters(year, crop, village, subdistrict, season=season)
    with engine.connect() as conn:
        total = int(conn.execute(text(f"SELECT COUNT(*) FROM biomass WHERE {where_sql}"), params).scalar_one())
        rows = conn.execute(
            text(
                f"""
                SELECT {FIELD_COLUMNS}
                FROM biomass
                WHERE {where_sql}
                ORDER BY village_name, crop_id
                LIMIT :limit OFFSET :skip
                """
            ),
            {**params, "limit": limit, "skip": skip},
        ).mappings().all()
    fields = [_season_field_view(_biomass_item(row), season) for row in rows]
    return {**_page_meta(total, skip, limit, len(fields)), "season": season, "fields": fields}


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(
        description="Recalculate and store biomass by Kharif year and Chhattisgarh season (kharif/rabi)."
    )
    parser.add_argument(
        "--years",
        default="",
        help="Comma-separated Kharif years, e.g. 2023,2024,2025,2026. Default: all.",
    )
    parser.add_argument(
        "--seasons",
        default="kharif,rabi",
        help="Comma-separated seasons: kharif,rabi. Default: both.",
    )
    args = parser.parse_args()
    chosen_years = tuple(int(year) for year in args.years.split(",") if year.strip()) or None
    chosen_seasons = parse_seasons([args.seasons])
    print(json.dumps(store_biomass(chosen_years, chosen_seasons), default=str, indent=2), flush=True)
