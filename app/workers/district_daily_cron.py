"""District-only classwise refresh for one observation date.

Tiles are omitted. The window is 30 inclusive days ending on the observation date.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.workers.backfill_parallel import run_location


def refresh_district_classwise(district: str, obs_date: date) -> None:
    start = obs_date - timedelta(days=29)
    run_location(None, district, "", "", [(start, obs_date)], include_tiles=False, extra=False)
