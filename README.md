# Kabirdham cadastral and crop dashboard

FastAPI service and dashboard for Kabirdham (Kawardha) cadastral records and crop identification results.

Open the dashboard at `http://127.0.0.1:8000/` after the server starts. API docs stay at `http://127.0.0.1:8000/docs`.

## Data sources

| Store | Environment variable | Tables | Used by |
| --- | --- | --- | --- |
| MySQL | `DATABASE_URL` | `kawardha_data` | `/kawardha-data` |
| MySQL | `DATABASE_URL` | `crop_identification` | `/crops` and the crop view |
| PostgreSQL `kawardha_db` | `KAWARDHA_DATABASE_URL` | `states`, `districts`, `talukas`, `ris`, `villages`, `plots`, `plot_extents` | `/kawardha` |

`kawardha_data` holds one GeoJSON geometry per administrative unit. `level` is `district` or `village`. A tehsil boundary is built by merging the village polygons in that tehsil. A district boundary uses a stored `level=district` row when one exists, and otherwise merges every village polygon in the district.

Plot rows carry khasra, area, and owner text parsed from `plots.info` (Hindi) and `plots.info_en` (English). Geometry comes from `plot_extents`.

## Setup

Python 3.11 or newer.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env` before starting the server.

```
DATABASE_URL=mysql+pymysql://USER:PASSWORD@HOST:PORT/DATABASE
KAWARDHA_DATABASE_URL=postgresql+psycopg2://postgres:PASSWORD@127.0.0.1:5432/kawardha_db
HOST=0.0.0.0
PORT=8000
```

`DATABASE_URL` may use the `mysql://` scheme. The app rewrites it to `mysql+pymysql://` and adds `charset=utf8mb4` when that parameter is missing. A `postgres://` or `postgresql://` value in `KAWARDHA_DATABASE_URL` is rewritten to `postgresql+psycopg2://`. Public PostgreSQL URLs get `sslmode=require`. Hosts on `rlwy.net` use TLS for MySQL. Set `MYSQL_SSL=0` to turn that off, or `MYSQL_SSL=1` to force it. Addresses on `*.railway.internal` stay without TLS, which is what Railway private networking expects.

## Run

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## GitHub

This folder is the repository root. `.env` is ignored. Copy values into GitHub only as Railway variables, never as committed files.

```bash
git init
git add .
git status
git commit -m "Prepare Kabirdham dashboard for Railway"
git branch -M main
git remote add origin https://github.com/planeteyeai/chhattisgrah_puc_backend_apis.git
git push -u origin main
```

Confirm `git status` does not list `.env` before the commit. The check workflow imports `app.main` on Python 3.12.

## Railway

Railway builds `Dockerfile` from `railway.toml`. The container listens on `PORT`. Do not commit `.env`.

1. Create a Railway project and deploy this GitHub repository. The service root is the repository root.
2. Add a MySQL plugin, or point at the existing MySQL instance.
3. Set the variables below on the web service. If the app runs in the same Railway project as MySQL, use the private URL (`*.railway.internal`). Use the public `*.proxy.rlwy.net` URL only when the app is outside that private network. Reference `${{MySQL.MYSQL_URL}}` for `DATABASE_URL` so a Postgres plugin cannot replace it.
4. Deploy. `GET /health` must return HTTP 200 before the release is marked healthy. The dashboard is `GET /`.

| Variable | Required | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | Yes | MySQL URL. `MYSQL_URL` is used when `DATABASE_URL` is empty. Reference the MySQL plugin as `${{MySQL.MYSQL_URL}}` so a Postgres plugin cannot overwrite it. |
| `KAWARDHA_DATABASE_URL` | No | PostgreSQL cadastral store for the Records tab. The crop map and biomass work without it. |
| `PUBLIC_BASE_URL` | No | Public `https://` origin of this service, with no trailing slash. |
| `EE_PROJECT` | No | Google Earth Engine project id for live index jobs. |
| `EE_SERVICE_ACCOUNT_JSON` | No | Full Earth Engine service-account JSON in one line. Prefer this on Railway. `GOOGLE_APPLICATION_CREDENTIALS` is a file path and is empty inside the container unless you mount a file. |
| `MYSQL_SSL` | No | `1` forces MySQL TLS. `0` disables it. Public `rlwy.net` hosts enable TLS unless this is `0`. |

`GET /health` returns `"status": "ok"` when MySQL accepts a connection. PostgreSQL may be `"not configured"`. `"degraded"` means MySQL is up and PostgreSQL was configured but did not connect. HTTP 503 means MySQL is down, and Railway will not keep that deploy.

The page at `/` is served from the `frontend/` folder. It has two views:

- **Cadastral hierarchy** walks state, district, tehsil, revenue inspector circle, and village, then draws that village’s parcels and owner details.
- **Crop identification** summarises every row in `crop_identification` and pages through the full record set. District, subdistrict, and village dropdowns call `/boundaries/names`, then `/boundaries` draws the district outline, the subdistrict and village outlines, or that village’s field polygons.

## Name matching

District, tehsil, and village filters are trimmed, case-insensitive, and must match the stored name exactly after that normalization.

These district strings all resolve to Kabirdham:

- `Kabirdham`
- `Kabeerdham`
- `Kawardha`
- `Kawardha District`

A village filter requires a tehsil. `GET /kawardha-data?district=Kabirdham&village=Hedaspur` returns `400`.

## Plot geometry

Two kinds of plot coordinates are stored.

- **Georeferenced.** Extent falls in latitude 17–25 and longitude 78–84. The response geometry is the stored GeoJSON, returned as a `MultiPolygon` when the source is a polygon.
- **Local CAD.** Coordinates are sheet metres (`x_min` at or below 100000). `GET /kawardha/plots` with `valid_geo=true` (the default) omits these plots. Pass `village_id` for a local-CAD village and the API places that village on a schematic origin near Kabirdham so the parcels can be drawn together. `properties.geo_status` is `local_cad`. That placement is a browsing layout, not a surveyed position.

`GET /kawardha/plots/map` follows the same rule and streams the FeatureCollection.

## Endpoints

### Service

`GET /api`

Lists the route templates.

`GET /health`

```json
{
  "status": "ok",
  "mysql": "connected",
  "kawardha_postgres": "connected"
}
```

`status` is `ok` when MySQL is connected and PostgreSQL is either connected or not configured. `status` is `degraded` when PostgreSQL is configured but down. `status` is `error` and the HTTP status is 503 when MySQL is down. `mysql` and `kawardha_postgres` contain `connected`, `not configured`, or the driver error text.

`GET /kawardha/health`

Checks PostgreSQL only. Returns `503` from the plot routes when `KAWARDHA_DATABASE_URL` is unset. This health route returns `"database": "not configured"` in that case.

### Plots

`GET /kawardha/summary`

Counts for the PostgreSQL cadastral store:

| Field | Meaning |
| --- | --- |
| `plots` | Rows in `plots` |
| `villages` | Rows in `villages` |
| `villages_done` | Villages with `scrape_status = done` |
| `plots_valid_geo` | Plots whose extent is inside latitude 17–25 and longitude 78–84 |
| `plots_local_cad` | Plots whose `x_min` is at or below 100000 |
| `villages_with_plots` | Distinct villages that have at least one plot |
| `area_ha` | Sum of `area_ha` for georeferenced plots |

`GET /kawardha/villages?valid_geo=false`

Villages that have plots, ordered by plot count descending.

| Query | Default | Effect |
| --- | --- | --- |
| `valid_geo` | `false` | When `true`, only georeferenced plot extents are included |

Each village includes `id`, `name`, Hindi and English names, `status`, `plot_count`, `segment_count`, `area_ha`, `georeferenced_plots`, `utm_plots`, the local sheet extent (`x_min`, `y_min`, `x_max`, `y_max`), `geo_status`, and `min_lon`, `min_lat`, `max_lon`, `max_lat`.

- `geo_status=georeferenced` when any plot has `x_min` above 100000. The lon/lat box is the real extent.
- `geo_status=local_cad` otherwise. The lon/lat box is the schematic layout for that village id.

`GET /kawardha/plots`

GeoJSON FeatureCollection, one feature per plot.

| Query | Default | Limits |
| --- | --- | --- |
| `village_id` | omitted | Restricts to one village. Local-CAD villages are laid out schematically and `valid_geo` is ignored |
| `valid_geo` | `true` | Keeps only georeferenced extents when no local layout applies |
| `skip` | `0` | Offset, minimum 0 |
| `limit` | `100` | Page size, 1–5000 |

Response fields: `type`, `total`, `skip`, `limit`, `count`, `features`.

Feature `properties`:

| Field | Source |
| --- | --- |
| `village_id`, `plot_no`, `plot_id`, `survey_no`, `ulpin`, `area_ha` | `plots` |
| `village_name`, `village_name_hi` | `villages` |
| `tenure`, `khasra`, `land_use`, `included_khasra` | Parsed owner text |
| `owners` | List of `{name, name_en, relation, relation_name, address}` |
| `owner_count`, `owner_name`, `owner_name_en`, `owner_relation`, `owner_relation_name`, `owner_address`, `owners_text`, `owner_color` | First owner, plus a stable color for the primary name |
| `gisinfo`, `owner_plots` | JSON inside `plots.info` |
| `geometry_type` | GeoJSON type, usually `MultiPolygon` |
| `geo_status` | `georeferenced` or `local_cad` |

`GET /kawardha/plots/map?village_id=1&valid_geo=true`

Same features as `/kawardha/plots`, streamed as `application/geo+json` with no page limit.

`GET /kawardha/plots/{plot_id}`

One GeoJSON Feature. `404` when the plot id is missing. Local-CAD plots for that village use the schematic layout.

### Boundaries

`GET /kawardha-data`

`POST /kawardha-data`

Both return a GeoJSON FeatureCollection for one administrative level. POST body:

```json
{
  "district": "Kabirdham",
  "tehsil": "Kawardha",
  "village": "Hedaspur"
}
```

`tehsil` and `village` are optional. `village` requires `tehsil`.

| Filters | `level` | Geometry |
| --- | --- | --- |
| district | `district` | Stored district polygon, or every village polygon merged |
| district + tehsil | `tehsil` | Village polygons in that tehsil, merged. `village_count` is set |
| district + tehsil + village | `village` | Stored village polygon or polygons |

Collection fields: `type`, `count`, `level`, `filters`, `features`.

Feature `properties`: `district_name`, `tehsil_name`, `village_name`, `village_code`, `state_name`, `level`, `source`, `bbox` as `[min_lon, min_lat, max_lon, max_lat]`.

| Status | When |
| --- | --- |
| `400` | District is blank, or village is set without tehsil |
| `404` | No matching boundary |

Examples:

```text
GET /kawardha-data?district=Kabirdham
GET /kawardha-data?district=Kabirdham&tehsil=Kawardha
GET /kawardha-data?district=Kabirdham&tehsil=Kawardha&village=Hedaspur
```

`GET /kawardha-data/names`

Distinct names for dropdowns.

| Query | Effect |
| --- | --- |
| none | Every district, tehsil, and village |
| `district` | Tehsils and villages in that district |
| `district` and `tehsil` | Villages in that tehsil. The tehsil list stays scoped to the district |

```json
{
  "districts": ["Kabirdham"],
  "tehsils": ["Kawardha"],
  "villages": ["Hedaspur"]
}
```

### Hierarchy

`GET /kawardha/hierarchy`

Nested cadastral tree: state, district, tehsils, revenue inspector circles, and villages. Each village includes `stored_plots` (rows in `plots`) and `plot_count` from the portal. `totals` counts tehsils, circles, villages, villages that have plots, and plots.

### Crop identification

`GET /crops/summary`

Totals for the whole `crop_identification` table: field count, area, rice count, label breakdown, observation dates, source models, and seasons.

`GET /crops/places`

One entry per district, subdistrict, and village, with field count, area, and a `labels` object.

`GET /crops/fields`

Paged records. Every stored column is returned.

| Query | Default | Effect |
| --- | --- | --- |
| `district`, `subdistrict`, `village`, `label`, `season` | omitted | Exact match |
| `rice_only` | `false` | Keeps `is_rice = 1` |
| `skip` | `0` | Offset |
| `limit` | `40` | Page size, 1–200 |

`GET /crops/points?subdistrict=r0c5`

Centroid points for one subdistrict. Optional `label` filter. `404` when that subdistrict has no centroids. The dashboard map draws field polygons from `/boundaries` instead.

## Field boundaries by place

Stored field polygons live in MySQL `field_boundaries`. Fields that fall inside a Kawardha village polygon are stored as district `Kabirdham`, subdistrict `Kawardha`, and that village’s name from the Kawardha village KML. Other fields keep their block code in `sub_dist`.

`GET /boundaries` and `POST /boundaries`

| Input | Result |
| --- | --- |
| `district_name` | One district outline |
| `district_name` and `subdistrict_name` | That subdistrict outline, plus one outline for every village in it |
| `district_name`, `subdistrict_name`, and `village_name` | Every field polygon in that village |

`subdistrict_name` is required when `village_name` is set. Names match without case sensitivity. A village request is GeoJSON streamed as `application/geo+json`. Each field feature includes `final_label`, `predicted_crop`, `area_ha`, and `is_rice` so the map can color polygons by crop.

```text
GET /boundaries?district_name=Chhattisgarh
GET /boundaries?district_name=Chhattisgarh&subdistrict_name=r5c1
GET /boundaries?district_name=Chhattisgarh&subdistrict_name=r5c1&village_name=Chhattisgarh
```

`GET /boundaries/names` and `POST /boundaries/names`

| Input | Result |
| --- | --- |
| `district_name` | Every `subdistrict_name` in that district |
| `district_name` and `subdistrict_name` | Every `village_name` in that subdistrict |

```text
GET /boundaries/names?district_name=Chhattisgarh
GET /boundaries/names?district_name=Chhattisgarh&subdistrict_name=r5c1
```

## Project layout

```text
app/
  main.py            application, dashboard route, /api, /health
  crops.py           crop identification routes
  boundaries.py      district, subdistrict, village, and field boundaries
  config.py          environment settings
  database.py        MySQL and PostgreSQL engines
  kawardha.py        hierarchy, plot, and village routes
  kawardha_data.py   boundary and name routes
  models.py          kawardha_data table
  owners.py          owner-text parser and plot GeoJSON
  geojson.py         JSON-safe numbers and timestamps
frontend/
  index.html         dashboard page
  styles.css
  app.js
requirements.txt
Dockerfile
railway.toml
.env.example
```
