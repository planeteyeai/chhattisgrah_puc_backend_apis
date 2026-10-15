# Kabirdham API for the frontend

Base URL on the office network:

```
http://192.168.42.142:8000
```

Same machine only:

```
http://127.0.0.1:8000
```

Interactive docs (try every call in the browser):

```
http://192.168.42.142:8000/docs
```

CORS allows every origin. Send query parameters on the URL. JSON bodies are used only where noted. Dates are `YYYY-MM-DD`.

There are two plot identifiers. Do not mix them.

| Name | Where it comes from | Used by |
|---|---|---|
| `field_boundaries.id` | MySQL field polygon | `plot_name` on `/analyze_Growth`, `/wateruptake`, `/SoilMoisture`, `/pest-detection`, `/daily-report` |
| Place name | District, subdistrict, village | Classwise analysis, indices, stored charts |
| `plots.id` | PostgreSQL cadastral parcel | `/kawardha/plots/{plot_id}` |

A village filter always needs a subdistrict. `field_id` on classwise routes also needs both subdistrict and village. Those invalid combinations return HTTP 400.

---

## 1. App

### `GET /`

Dashboard HTML.

### `GET /health`

```json
{ "status": "ok", "mysql": "connected", "kawardha_postgres": "connected" }
```

`status` is `error` when either database is down.

### `GET /api`

JSON index of route templates.

### `GET /static/...`

Dashboard CSS and JavaScript.

---

## 2. Cadastral plots (PostgreSQL)

Prefix: `/kawardha`

### `GET /kawardha/health`

`{ "status": "ok", "database": "connected", "engine": "postgresql", "name": "kawardha_db" }`

### `GET /kawardha/summary`

Counts: `plots`, `villages`, `villages_done`, `plots_valid_geo`, `plots_local_cad`, `villages_with_plots`, `area_ha`.

### `GET /kawardha/hierarchy`

State, district, tehsil, revenue-inspector circle, and village tree. Each village has `id` and `stored_plots`.

### `GET /kawardha/villages?valid_geo=false`

Villages that have plots, ordered by plot count. `valid_geo=true` keeps only parcels inside latitude 17–25 and longitude 78–84.

### `GET /kawardha/plots`

| Query | Default | Meaning |
|---|---|---|
| `village_id` | none | Limit to one village |
| `valid_geo` | `true` | Drop local CAD parcels |
| `skip` | `0` | Offset |
| `limit` | `100` | Max 5000 |

Response is a GeoJSON `FeatureCollection` plus `total`, `skip`, `limit`, `count`.

Local CAD villages (`x_min` at or below 100000) are placed on a schematic origin when `village_id` is sent. `properties.geo_status` is `local_cad`. That placement is for browsing, not a surveyed position.

### `GET /kawardha/plots/map?village_id=&valid_geo=true`

Same filters as `/kawardha/plots`, streamed as GeoJSON. No paging.

### `GET /kawardha/plots/{plot_id}`

One GeoJSON Feature. 404 when the parcel does not exist.

---

## 3. Admin boundaries (MySQL `kawardha_data`)

### `GET /kawardha-data?district=Kabirdham&tehsil=Kawardha&village=Hedaspur`

### `POST /kawardha-data`

JSON body: `{ "district": "Kabirdham", "tehsil": "Kawardha", "village": "Hedaspur" }`

`district` is required. A village requires a tehsil. Names are trimmed and case-insensitive. `Kabirdham`, `Kabeerdham`, and `Kawardha` all resolve to Kabirdham.

| Filters | Geometry returned |
|---|---|
| district | District polygon |
| district + tehsil | Tehsil polygon (stored row, or the union of its villages) |
| district + tehsil + village | That village polygon |

### `GET /kawardha-data/names?district=Kabirdham&tehsil=Kawardha`

District names, tehsil names, and village names for the dropdowns.

---

## 4. Crop identification

### `GET /crops/summary`

Totals and breakdowns: `fields`, `area_ha`, `rice_fields`, `districts`, `subdistricts`, `villages`, date ranges, `labels`, `observations`, `models`, `seasons`.

### `GET /crops/places`

```json
{
  "count": 1,
  "places": [
    {
      "district_name": "Kabirdham",
      "subdistrict_name": "Kawardha",
      "village_name": "Hedaspur",
      "fields": 10,
      "area_ha": 4.2,
      "labels": { "Rice": { "fields": 8, "area_ha": 3.1 } }
    }
  ]
}
```

### `GET /crops/fields`

| Query | Default | Meaning |
|---|---|---|
| `district` | | |
| `subdistrict` | | |
| `village` | | |
| `label` | | `final_label`, for example Rice |
| `season` | | |
| `rice_only` | `false` | |
| `skip` | `0` | |
| `limit` | `40` | Max 200 |

Returns `total`, `skip`, `limit`, `count`, `fields`. Each field includes `field_boundary_id`, `final_label`, `area_ha`, `centroid_lon`, `centroid_lat`.

### `GET /crops/points?subdistrict=Kawardha&label=Rice`

Map points. `subdistrict` is required. Each point has `id`, `final_label`, `area_ha`, `is_rice`, `centroid_lon`, `centroid_lat`, `field_boundary_id`.

---

## 5. Field outlines

### `GET /boundaries?district_name=Kabirdham&subdistrict_name=Kawardha&village_name=Hedaspur`

### `POST /boundaries`

JSON body:

```json
{ "district_name": "Kabirdham", "subdistrict_name": "Kawardha", "village_name": "Hedaspur" }
```

| Filters | What is drawn |
|---|---|
| district | District outline |
| district + subdistrict | Subdistrict outline and village outlines |
| district + subdistrict + village | Every field polygon in that village |

Field features include `field_boundary_id`, `field_id`, `area_ha`, `final_label`, `predicted_crop`, `is_rice`. Use `field_boundary_id` as `plot_name` for the plot-level satellite calls below.

### `GET /boundaries/names?district_name=Kabirdham&subdistrict_name=Kawardha`

### `POST /boundaries/names`

JSON body: `{ "district_name": "Kabirdham", "subdistrict_name": "Kawardha" }`

Subdistrict names, or village names when a subdistrict is sent.

---

## 6. Sentinel index charts

Stored scene values, not a live satellite call. One row is one Sentinel-2 scene. District names in this table are lowercase (`kabirdham`).

### `GET /indices/places?district=kabirdham`

Dropdown keys: `{ "district", "subdistrict", "village" }`. Empty subdistrict and village mean the district rollup. Empty village with a subdistrict means the subdistrict rollup.

### `GET /indices/retrieve-aggregated?district=kabirdham&frequency=monthly&subdistrict=kawardha&village=anchhi`

`frequency` is `weekly`, `monthly`, or `yearly`.

| Scope | Query |
|---|---|
| District trend | `subdistrict=` and `village=` (empty strings) |
| Subdistrict trend | `subdistrict=kawardha&village=` |
| Village trend | `subdistrict=kawardha&village=anchhi` |

Response: `{ "count", "filters", "series" }`. Each series item has `period` plus averaged columns:

`ndvi`, `evi`, `evi2`, `gndvi`, `ndre`, `ndwi_mcfeeters`, `ndwi_gao`, `ndmi`, `rvi_optical`, `ndbi`, `bsi`, `savi`, `osavi`, `rvi_radar`, `vh_vv_ratio`, `lst`, `temperature`, `precipitation_daily`, `precipitation_sum`, `soil_water_fraction_surface`, `soil_water_fraction_subsurface`.

Null means that column was not computed for those scenes. `croptype` is accepted and ignored.

### `GET /indices/retrieve?district=kabirdham&subdistrict=kawardha&village=anchhi&start_date=2024-01-01&end_date=2024-12-31&limit=50000`

Raw scene rows. `limit` max is 200000.

### `GET /indices/stored-locations?district=kabirdham&limit=5000`

Places that already have rows: `rows_count`, `min_obs_date`, `max_obs_date`, `last_updated_at`.

### `POST /indices/store?district=kabirdham&subdistrict=kawardha&village=anchhi&years=10&step_days=16&sync=false`

Starts storage for one place. `sync=false` returns immediately with `{ "status": "accepted" }`. `sync=true` waits and returns `rows_written`.

### `POST /indices/store-all?years=10&step_days=16&sync=false`

Stores every village, subdistrict, and district. Optional `district` and `max_locations` (`0` means all). HTTP 409 if a store is already running.

### `GET /indices/store-status`

`running`, `status`, `locations_done`, `locations_total`, `rows_written`, `last_error`.

### `POST /indices-cron/start`

### `GET /indices-cron/status`

### `POST /indices-cron/stop`

Continuous background store. HTTP 409 if already running. HTTP 404 on stop when it is not running.

### `POST /district-daily-cron/start`

### `GET /district-daily-cron/status`

### `POST /district-daily-cron/stop`

Daily district-only scene refresh.

### `POST /ops/stop-server-compute`

Stops the indices cron, the district daily cron, and `/indices/store-all`.

---

## 7. Plot-level satellite analysis

`plot_name` is `field_boundaries.id` (the same value as `field_boundary_id` on `/boundaries` field features). Example: `129606`.

Imagery is Earth Search (`https://earth-search.aws.element84.com/v1`). If `end_date` is omitted it is tomorrow. If `start_date` is omitted it is 15 days before `end_date`.

These responses are GeoJSON `FeatureCollection` objects. `features[0].properties.tile_url` is an XYZ template. Replace `{z}`, `{x}`, `{y}` and load it as a map overlay. `thumb_url` is one PNG of the plot.

Tile and thumb URLs:

```
GET /analysis-tiles/{key}/{z}/{x}/{y}.png
GET /analysis-thumbs/{key}.png
```

Cache header: `public, max-age=300`.

### `POST /analyze_Growth?plot_name=129606&end_date=2026-09-30`

Optional: `start_date`, `skip_cache` (default false), `django_id` (alternate numeric id).

`pixel_summary` keys: `total_pixel_count`, `healthy_pixel_count`, `healthy_pixel_percentage`, `moderate_pixel_*`, `weak_pixel_*`, `stress_pixel_*`, `analysis_start_date`, `analysis_end_date`, `latest_image_date`. Each class also has `*_pixel_coordinates` (lon, lat), capped at 2000 points.

Classes: 1 Weak, 2 Stress, 3 Moderate, 4 Healthy.

### `GET /wateruptake` and `POST /wateruptake`

Query: `plot_name` (required), `end_date`, `start_date`, `days_back`, `skip_cache`.

`pixel_summary` classes: `deficient`, `less`, `adequat`, `excellent`, `very_healthy`. The spelling `adequat` is intentional.

### `POST /SoilMoisture?plot_name=129606&end_date=2026-09-30`

Optional: `start_date`, `skip_cache`.

Classes: `less`, `adequate`, `excellent`, `excess`, `shallow_water`. Percentages in this summary are rounded to 2 decimals.

### `POST /pest-detection?plot_name=129606&end_date=2026-09-30`

The analysis window is `end_date` minus 15 days. The baseline is 1 June through 30 October of the previous year. `start_date` is kept for compatibility and does not change that window.

`pixel_summary` keys: `healthy_pixel_count`, `chewing_affected_*`, `fungi_affected_*`, `sucking_affected_*`, `wilt_affected_*`, `SoilBorn_pixel_count`, `SoilBorn_affected_pixel_percentage`, `SoilBorn_affected_pixel_coordinates`.

### `GET /plots`

List of every `field_boundaries.id` as strings. This is a large list.

### `GET /plots/{plot_name}/info`

`{ "name", "geometry_type", "area_acres" }`

### `GET /daily-report?plot_name=129606&date=2026-09-30&lat=22.0&lon=81.2`

Combines growth, soil, water, and pest for that plot, plus weather, events, and NPK when those outside services respond. `lat` and `lon` are optional and only used for weather. Pixel coordinates are removed from the report summaries.

### `GET /satellite/health`

### `GET /metrics`

Prometheus text.

### `GET /refresh-from-django` and `POST /refresh-from-django`

Clears the in-memory plot cache and recounts `field_boundaries`.

### `GET /debug/plot/{plot_name}`

Whether that id exists.

---

## 8. Classwise satellite analysis (place, not a single field)

These are the chart and class-map endpoints. Query parameters are the same on all four:

| Query | Required | Meaning |
|---|---|---|
| `district` | yes | Example `Kabirdham` or `kabirdham` |
| `subdistrict` | no | Example `Kawardha` |
| `village` | no | Requires `subdistrict` |
| `field_id` | no | One field. Requires subdistrict and village |
| `end_date` | no | Default is tomorrow |
| `start_date` | no | Default is `end_date` minus 14 days (15 days inclusive) |
| `include_tiles` | no | Default `true`. `false` returns stats only |

Success body:

```json
{ "current": { }, "stored": [] }
```

`current.from_cache` is `true` when the saved row is reused. Geometry coordinates are replaced with `{ "type": "Polygon", "coordinates_stripped": true }`. Draw the boundary from `/boundaries` or `/kawardha-data`, and draw the classes from `tile_url`.

Tile templates inside `tile_url`:

```
GET /classwise-tiles/{key}/{z}/{x}/{y}.png
GET /classwise-tiles/{key}/class/{class_id}/{z}/{x}/{y}.png
```

256 px PNG, transparent outside the area of interest and outside the requested class. `Cache-Control: public, max-age=300`.

HTTP 404 when no Sentinel-1 or Sentinel-2 scene exists in the window.

### `POST /analyze_Growthclasswise?district=Kabirdham&subdistrict=Kawardha&village=Baharmuda&end_date=2026-09-30`

`current.feature.properties`: `plot_id`, `area_acres`, `start_date`, `end_date`, `tile_url`, `data_source` (`Sentinel-2 NDVI` or `Sentinel-1 VH`), `last_updated`, and `field_id` only when requested.

`current.pixel_summary`:

`total_pixel_count`, `healthy_pixel_count`, `healthy_pixel_percentage`, `moderate_pixel_count`, `moderate_pixel_percentage`, `weak_pixel_count`, `weak_pixel_percentage`, `stress_pixel_count`, `stress_pixel_percentage`, `analysis_start_date`, `analysis_end_date`, `latest_image_date`, `area_hectares`.

`current.classwise` items, in this order:

| class_id | class_name | color in JSON | color on the map tile |
|---|---|---|---|
| 1 | Weak | `#bc1e29` | `#bc1e29` |
| 2 | Stress | `#58cf54` | `#58cf54` |
| 3 | Moderate | `#28ae31` | `#28ae31` |
| 4 | Healthy | `#00351d` | `#056c3e` |

Each item: `percentage`, `area_hectares` (2 decimals), `tile_url`.

### `POST /wateruptakeclasswise`

Same query parameters. `feature.properties` has `plot_id`, `start_date`, `end_date`, `tile_url`, `last_updated`. There is no `data_source`.

`pixel_summary`: `total_pixel_count`, `analysis_start_date`, `analysis_end_date`, `area_hectares`, then count and percentage for `deficient`, `less`, `adequat`, `excellent`, `excess`.

| class_id | class_name | color |
|---|---|---|
| 1 | Deficient | `#EBFF34` |
| 2 | Less | `#CC8213AF` |
| 3 | Adequat | `#1348E88E` |
| 4 | Excellent | `#2E199ABD` |
| 5 | Excess | `#0602178F` |

Keep the spelling **Adequat**.

### `POST /SoilMoistureclasswise`

`pixel_summary` percentages are rounded to 2 decimals. `classwise[].percentage` is not rounded. `sensor_used` is `Sentinel-2` or `Sentinel-1`. `data_source` is `Sentinel-2 Soil Moisture` or `Sentinel-1 Soil Moisture`.

| class_id | class_name | color |
|---|---|---|
| 1 | less | `#2FC0D3` |
| 2 | adequate | `#4365D4` |
| 3 | excellent | `#473CDF` |
| 4 | excess | `#2116BF` |
| 5 | shallow_water | `#000475` |

Summary keys are `{class_name}_pixel_count` and `{class_name}_pixel_percentage`, plus `latest_image_date` and `sensor_used`.

### `POST /pest-detectionclasswise`

Uses `end_date` only. Analysis is the 15 days ending on `end_date`. Baseline is 1 June–30 October of the previous year. Class masks can overlap, so hierarchy percentages can add up to more than 100.

`current` is a FeatureCollection, not the growth shape:

```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "geometry": { "type": "Polygon", "coordinates_stripped": true },
      "properties": {
        "plot_id": "Baharmuda",
        "total_area_ha": 12.4,
        "area_acres": 30.64,
        "tile_url": "/classwise-tiles/{key}/{z}/{x}/{y}.png",
        "start_date": "2026-09-15",
        "end_date": "2026-09-30",
        "last_updated": "2026-09-30T12:00:00"
      }
    }
  ],
  "hierarchy": {
    "healthy": { "total_area_ha": 0, "percentage": 0, "children": {} },
    "chewing": {
      "tile_url": "...",
      "total_area_ha": 0,
      "percentage": 0,
      "children": {
        "leaf_eating_caterpillar": { "tile_url": "...", "area_ha": 0, "pct_of_parent": 0 }
      }
    },
    "fungi": {
      "tile_url": "...",
      "total_area_ha": 0,
      "percentage": 0,
      "children": {
        "red_rot": { "tile_url": "...", "area_ha": 0, "pct_of_parent": 0 },
        "rust": { "area_ha": 0, "pct_of_parent": 0 },
        "downy_mildew": { "area_ha": 0, "pct_of_parent": 0 },
        "aflatoxin": { "area_ha": 0, "pct_of_parent": 0 }
      }
    },
    "sucking": {
      "tile_url": "...",
      "total_area_ha": 0,
      "percentage": 0,
      "children": {
        "thrips": { "area_ha": 0, "pct_of_parent": 0 },
        "mealybug": { "area_ha": 0, "pct_of_parent": 0 }
      }
    },
    "wilt": {
      "tile_url": "...",
      "total_area_ha": 0,
      "percentage": 0,
      "children": {
        "unknown_wilt": { "tile_url": "...", "area_ha": 0, "pct_of_parent": 0 }
      }
    },
    "soilborne": {
      "tile_url": "...",
      "total_area_ha": 0,
      "percentage": 0,
      "children": {
        "grub": { "area_ha": 0, "pct_of_parent": 0 }
      }
    }
  }
}
```

Map colors: chewing `#FF0000`, fungi `#00DD88`, sucking `#2600FF`, wilt `#D60BFF`, soilborne `#FFE608`.

`leaf_eating_caterpillar` copies chewing. `unknown_wilt` copies wilt. `grub` copies soilborne. The other child diseases stay at area 0.

---

## 9. Stored classwise charts

Read what the backfill already saved. `district` is required. `subdistrict` and `village` are optional filters. `limit` defaults to 500.

```
GET /api-stored/growth?district=kabirdham&subdistrict=kawardha&village=baharmuda
GET /api-stored/water-uptake?district=kabirdham
GET /api-stored/soil-moisture?district=kabirdham
GET /api-stored/pest-detection?district=kabirdham
```

```json
{
  "district": "kabirdham",
  "count": 2,
  "series": [
    {
      "id": 1,
      "district": "kabirdham",
      "subdistrict": "kawardha",
      "village": "baharmuda",
      "year_month": "2026-09",
      "created_at": "2026-09-30T12:00:00",
      "request_params": { "district": "kabirdham", "end_date": "2026-09-30" },
      "response_data": { "current": {}, "stored": [] }
    }
  ]
}
```

Render `response_data` the same way as a live `current` payload. Match a village row when `village` is set. Match a subdistrict row when `village` is empty. Match a district row when both are empty.

For the index chart, village scenes are stored. Subdistrict-only index rows are not filled yet, so a subdistrict trend can be empty while the village trend has data.

---

## 10. Open water and land-surface temperature

These are live Google Earth Engine calls. They are not stored and not backfilled. They return HTTP 503 when Earth Engine credentials are not configured.

### `POST /NDWIDetection?district=Kabirdham&subdistrict=Kawardha&village=Baharmuda&end_date=2026-09-30`

Optional: `start_date` (default end minus 14 days), `max_cloud_pct` (default 60).

If no scene is found, the start date widens to 60 days and then cloud cover rises to 80. Still empty returns HTTP 200 with `features: []`, zero `area_summary`, and a `message`. A missing boundary is the same 200 shape, not a 404.

```json
{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "id": "water-source-Baharmuda",
      "geometry": { "type": "Polygon", "coordinates": [] },
      "properties": {
        "plot_name": "Baharmuda",
        "start_date": "2026-09-16",
        "end_date": "2026-09-30",
        "requested_start_date": "2026-09-16",
        "max_cloud_pct": 60,
        "latest_image_date": "2026-09-14",
        "image_count": 4,
        "tile_url": "https://earthengine.googleapis.com/...",
        "last_updated": "2026-09-30T12:00:00"
      }
    }
  ],
  "area_summary": {
    "total_area_hectare": 10.5,
    "water_area_hectare": 0.4,
    "water_area_percentage": 3.81
  }
}
```

Read `area_summary.water_area_percentage`. One legend item, `#3b82f6`, label `Water %`. Feature ids use the prefix `water-source-`.

### `POST /land-surface-temperature?district=Kabirdham&start_date=2026-09-01&end_date=2026-09-30`

### `POST /land-surface-temperatureclasswisearea`

Same handler. `start_date` and `end_date` are required.

```json
{
  "plot_id": "Kabirdham",
  "geometry": {},
  "lst_tile_url": "...",
  "lst_class_combined_tile": "...",
  "lst_class_tiles": {
    "< 25 °C": "...",
    "25–30 °C": "...",
    "30–35 °C": "...",
    "35–40 °C": "...",
    "> 40 °C": "..."
  },
  "lst_classwise_area_hectare": {
    "features": [
      { "properties": { "class_id": 1, "area_hectare": 1.2, "class_label": "< 25 °C" } }
    ]
  },
  "start_date": "2026-09-01",
  "end_date": "2026-09-30",
  "last_updated": "2026-09-30T12:00:00"
}
```

Class colors: `#0000FF`, `#00FF00`, `#FFFF00`, `#FFA500`, `#FF0000`. The degree labels use an en dash (`25–30 °C`).

---

## 11. Land-use hectares

Not classwise and not stored.

### `GET /api/land-statistics?district=Kabirdham&subdistrict=Kawardha&village=Baharmuda&endDate=2026-09-30`

`endDate` defaults to today and cannot be in the future (HTTP 400). The window is always 20 days ending on that date.

```json
{
  "vegetativeLandHectare": 1.2,
  "barrenLandHectare": 0.4,
  "greenLandHectare": 0.5,
  "landUnderStressHectare": 0.3,
  "excellentHealthLandHectare": 0.4,
  "waterAreaHectare": 0.1,
  "totalAreaHectare": 2.1,
  "classifiedAreaHectare": 2.0,
  "unclassifiedAreaHectare": 0.1,
  "imageDate": "2026-09-14",
  "satellite": "Sentinel-2",
  "collection": "sentinel-2-l2a",
  "dataSource": "Sentinel-2",
  "scope": "village",
  "district": "Kabirdham",
  "subdistrict": "Kawardha",
  "village": "Baharmuda",
  "startDate": "2026-09-11",
  "endDate": "2026-09-30"
}
```

`scope` is `village`, `subdistrict`, or `district`. `totalAreaHectare` is the polygon area.

---

## 12. Backfill jobs

These fill the stored classwise tables. They are long-running. Do not call them from a page load.

### `POST /backfill-stored-data`

No body. HTTP 202. All locations, 14 months, four classwise analyses. HTTP 409 if already running.

### `POST /backfill-stored-data/daily?district=kabirdham&subdistrict=kawardha&village=baharmuda&months=15`

HTTP 202. `{ "job_id", "status": "started" }`. `months` is 1–24, default 15. Omit `district` to run every location.

### `GET /backfill-stored-data/daily/jobs/{job_id}`

`status` is `queued`, `running`, `done`, `stopped`, or `error`. Also `total`, `completed`, `skipped`, `errors`, `current`.

### `POST /backfill-stored-data/daily/jobs/stop-all`

Stops the full backfill and daily jobs. `{ "stopped": 1 }`

### `GET /backfill-stored-data/daily?district=kabirdham&subdistrict=kawardha&village=baharmuda`

Read-only. Returns `months`, `villages`, `counts`, and arrays `pest_detection`, `growth`, `water_uptake`, `soil_moisture`.

### `POST /backfill-cron/start`

### `GET /backfill-cron/status`

### `POST /backfill-cron/stop`

Current month only. Resumes from the last checkpoint after a restart. Status includes `running`, `current_location`, `year_month`, `locations_done`, `locations_total`, `last_error`.

### `DELETE /backfill-stored-data?confirm=true&district=kabirdham`

`confirm=true` is required or the call returns HTTP 400. Deletes classwise, canopy, and weather response rows. It does not delete `indices`.

---

## Errors

| Code | When |
|---|---|
| 400 | Missing district, village without subdistrict, `field_id` without both, future `endDate` on land statistics, `confirm` not true |
| 404 | Unknown plot, no geometry, no satellite scene |
| 409 | A store or backfill job is already running |
| 503 | Earth Engine is not configured (`/NDWIDetection`, land-surface temperature) |
| 500 | Unexpected server error. `detail` is a string |

---

## Calls to try first

```
GET  http://192.168.42.142:8000/health
GET  http://192.168.42.142:8000/kawardha/hierarchy
GET  http://192.168.42.142:8000/crops/places
GET  http://192.168.42.142:8000/indices/places?district=kabirdham
GET  http://192.168.42.142:8000/indices/retrieve-aggregated?district=kabirdham&subdistrict=kawardha&village=anchhi&frequency=monthly
GET  http://192.168.42.142:8000/api-stored/growth?district=kabirdham&subdistrict=kawardha&village=baharmuda
POST http://192.168.42.142:8000/analyze_Growthclasswise?district=Kabirdham&subdistrict=Kawardha&village=Baharmuda
```

Classwise and plot analysis calls download satellite imagery and can take one to several minutes. Stored chart calls return immediately.
