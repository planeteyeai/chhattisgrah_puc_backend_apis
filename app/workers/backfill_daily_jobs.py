"""Read stored classwise rows for the daily backfill view."""

from __future__ import annotations

from typing import Any

from app.api_response_storage import list_series
from app.workers.backfill_parallel import month_windows


def daily_snapshot(district: str, subdistrict: str | None, village: str | None, months: int = 15) -> dict[str, Any]:
    windows = month_windows(months)
    series = {
        "pest_detection": list_series("pest_detection", district, subdistrict, village, limit=500),
        "growth": list_series("growth", district, subdistrict, village, limit=500),
        "water_uptake": list_series("water_uptake", district, subdistrict, village, limit=500),
        "soil_moisture": list_series("soil_moisture", district, subdistrict, village, limit=500),
    }
    villages = sorted({row["village"] for rows in series.values() for row in rows if row.get("village")})
    return {
        "district": district,
        "subdistrict": subdistrict,
        "village": village,
        "months": [
            {"start_date": start.isoformat(), "end_date": end.isoformat(), "year_month": end.isoformat()[:7]}
            for start, end in windows
        ],
        "villages": villages,
        "counts": {name: len(rows) for name, rows in series.items()},
        **series,
    }
