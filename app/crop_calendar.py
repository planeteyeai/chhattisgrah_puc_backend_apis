"""Chhattisgarh crop calendar helpers shared by biomass, boundaries, and crops APIs."""

from __future__ import annotations

from fastapi import HTTPException

CALENDAR_SEASON: dict[str, str] = {
    "Rice": "kharif",
    "Maize": "kharif",
    "Soybean": "kharif",
    "Cotton": "kharif",
    "Arhar (Tur)": "kharif",
    "Sugarcane": "kharif",
    "Wheat": "rabi",
    "Mustard": "rabi",
    "Linseed (Tisi)": "rabi",
    "Gram (Chana)": "rabi",
    "Lentil (Masoor)": "rabi",
}
VALID_SEASONS = frozenset({"kharif", "rabi"})
KHARIF_CROPS = tuple(crop for crop, bucket in CALENDAR_SEASON.items() if bucket == "kharif")
RABI_CROPS = tuple(crop for crop, bucket in CALENDAR_SEASON.items() if bucket == "rabi")


def parse_calendar_season(value: str | None) -> str | None:
    """Normalize to `kharif` / `rabi`, or None when both seasons are wanted."""
    if value is None or not str(value).strip():
        return None
    season = str(value).strip().lower()
    if season in ("all", "both", "*"):
        return None
    if season not in VALID_SEASONS:
        raise HTTPException(status_code=400, detail="season must be kharif or rabi")
    return season


def crops_for_season(season: str | None) -> tuple[str, ...] | None:
    if season == "kharif":
        return KHARIF_CROPS
    if season == "rabi":
        return RABI_CROPS
    return None


def _sql_in_list(crops: tuple[str, ...]) -> str:
    return ", ".join("'" + crop.replace("'", "''") + "'" for crop in crops)


def calendar_label_sql(season: str | None, column: str = "final_label") -> str:
    """SQL predicate: label belongs to the calendar season (including double-crop parts).

    Double crops are stored as `KharifCrop + RabiCrop`. Kharif matches the left
    part; rabi matches the right part (or a single rabi crop).
    """
    if season is None:
        return "1=1"
    crops = crops_for_season(season)
    assert crops is not None
    listed = _sql_in_list(crops)
    if season == "kharif":
        return (
            f"("
            f"{column} IN ({listed}) "
            f"OR ("
            f"LOCATE(' + ', {column}) > 0 "
            f"AND SUBSTRING_INDEX({column}, ' + ', 1) IN ({listed})"
            f")"
            f")"
        )
    return (
        f"("
        f"{column} IN ({listed}) "
        f"OR ("
        f"LOCATE(' + ', {column}) > 0 "
        f"AND SUBSTRING_INDEX({column}, ' + ', -1) IN ({listed})"
        f")"
        f")"
    )


def season_display_label_sql(season: str | None, column: str = "final_label") -> str:
    """Return only the season's crop name from a label (for response properties)."""
    if season == "kharif":
        return f"SUBSTRING_INDEX({column}, ' + ', 1)"
    if season == "rabi":
        return (
            f"CASE WHEN LOCATE(' + ', {column}) > 0 "
            f"THEN SUBSTRING_INDEX({column}, ' + ', -1) "
            f"ELSE {column} END"
        )
    return column
