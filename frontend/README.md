# Kabirdham Crop Monitoring — frontend guide

Rebuild the same dashboard against the existing API. The reference UI is the three files in this folder: `index.html`, `app.js`, and `styles.css`. Match that layout and behavior.

Base URL: `http://192.168.42.142:8000`

The page and the API are the same server. Do not hard-code a host in the app. Call paths such as `/biomass/places`. The browser then uses whatever address opened the page (`http://192.168.42.142:8000` on the LAN, `http://localhost:8000` on the machine running the server).

## Run

From the project root:

```powershell
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open `http://192.168.42.142:8000`. There is no separate frontend command and no Node build. Leaflet 1.9.4 and Chart.js 4.4.6 are loaded from CDN in `index.html`.

## Connection

Two helpers in `app.js` talk to the API:

- `readJson(url)` — cached GET. Used for crops, boundaries, biomass, and the cadastral map.
- `readFresh(url, options)` — uncached GET or POST. Used for indices, live analysis layers, and stored backfill.

A failed response throws `detail` from the JSON body.

## Screens

The header has three tabs.

| Tab | View id | What it shows |
|---|---|---|
| Map explore | `#view-crops` | Default screen. District, subdistrict, village, year, and month. Field map. Crop legend. Growth, water, soil, pest, and biomass. |
| Records | `#view-cadastral` | Tehsil / RI / village tree, cadastral plot map, plot table. |
| Dashboard | `#view-indices` | Sentinel-2 index charts for a district, subdistrict, or village. |

Map explore is a three-column shell: filters on the left, Leaflet map in the center, a scrolling card on the right. The right card must scroll. Village lists and long biomass charts must scroll inside their own boxes.

Body text is large (`font-size: 32px` on `body`, so `rem` units are large). Keep cards readable. Do not shrink numbers until they wrap.

## Map explore

Filters:

- District, subdistrict, village. The village list includes **All villages**.
- Year and month come from `GET /crops/periods`.
- Village boundaries checkbox.
- Analysis buttons: Growth, Water uptake, Soil moisture, Pest, Biomass.
- Crop chips: All crops, Rice, Sugarcane, Cotton. Hide labels `subpixel`, `tree`, and `uncertain`.

Colors:

| Crop | Color |
|---|---|
| Rice | `#facc15` |
| Sugarcane | `#22c55e` |
| Cotton | `#ef4444` |

Map rules:

- Subdistrict only (All villages): draw the subdistrict outline and village outlines. Do not request every field polygon. Kawardha has 270,348 crop fields for 2025.
- One village plus a crop: `GET /boundaries?district_name=Kabirdham&subdistrict_name=Kawardha&village_name=Agarikalan&year=2025&month=9` returns a GeoJSON `FeatureCollection` of that village’s fields. Draw rice, sugarcane, and cotton. A selected crop filters the polygons.
- Feature `id` and `properties.crop_id` equal `crop_identification.id`. That id is `biomass.crop_id` when a year is selected. Use it when opening biomass for a clicked field.
- Field polygons sit above village outlines and must receive clicks. Village outlines are not clickable while fields are shown.

### Crop totals

`GET /crops/summary?year=2025&month=9` and `GET /crops/places?year=2025&month=9`.

The right-hand crop bars (for example “Rice 376 fields / 59.8 ha”) are field counts and hectares from crop identification. They are not biomass. Bar width follows the field count.

### Growth, water, soil, pest

These four layers are not biomass. Do not look up a biomass click in this table.

Stored months (fast, already computed):

| Layer | Stored |
|---|---|
| Growth | `GET /api-stored/growth?district=Kabirdham&subdistrict=Kawardha&village=Agarikalan` |
| Water uptake | `GET /api-stored/water-uptake?district=Kabirdham&subdistrict=Kawardha&village=Agarikalan` |
| Soil moisture | `GET /api-stored/soil-moisture?district=Kabirdham&subdistrict=Kawardha&village=Agarikalan` |
| Pest | `GET /api-stored/pest-detection?district=Kabirdham&subdistrict=Kawardha&village=Agarikalan` |

Live layer for the selected month. `POST` with `include_tiles=true`. The JSON includes a tile URL template. Draw it on Leaflet with `GET /classwise-tiles/{key}/{z}/{x}/{y}.png`.

| Layer | Live |
|---|---|
| Growth | `POST /analyze_Growthclasswise?district=Kabirdham&subdistrict=Kawardha&village=Agarikalan&end_date=2025-09-15&include_tiles=true` |
| Water | `POST /wateruptakeclasswise?district=Kabirdham&subdistrict=Kawardha&village=Agarikalan&end_date=2025-09-15&include_tiles=true` |
| Soil | `POST /SoilMoistureclasswise?district=Kabirdham&subdistrict=Kawardha&village=Agarikalan&end_date=2025-09-15&include_tiles=true` |
| Pest | `POST /pest-detectionclasswise?district=Kabirdham&subdistrict=Kawardha&village=Agarikalan&end_date=2025-09-15&include_tiles=true` |

Class colors:

- Growth: Weak `#bc1e29`, Stress `#58cf54`, Moderate `#28ae31`, Healthy `#056c3e`
- Water: Deficient `#EBFF34`, Less `#CC8213`, Adequate `#1348E8`, Excellent `#2E199A`, Excess `#060217`
- Soil: Less `#2FC0D3`, Adequate `#4365D4`, Excellent `#473CDF`, Excess `#2116BF`, Shallow water `#000475`
- Pest: Healthy `#22c55e`, Chewing `#FF0000`, Fungi `#00DD88`, Sucking `#2600FF`, Wilt `#D60BFF`, Soil borne `#FFE608`

Show a pie of class share and a legend. Clicking a class switches the map tile to that class.

One plot, if needed. `plot_name` is `field_boundaries.id`:

- `POST /analyze_Growth?plot_name={id}&end_date=2025-09-15`
- `GET /wateruptake?plot_name={id}&end_date=2025-09-15`
- `POST /SoilMoisture?plot_name={id}&end_date=2025-09-15`
- `POST /pest-detection?plot_name={id}&end_date=2025-09-15`

Tiles for a plot: `GET /analysis-tiles/{key}/{z}/{x}/{y}.png`.

## Biomass

Rice and cotton only, years 2024 and 2025. Biomass is its own panel. It must not go through the growth/water/soil/pest title lookup.

### What to show

Show a **min–max band**, never the exact stored tonnes and never `biomass_t_ha` (t/ha). Area in hectares and field counts stay exact. Year-to-year change is a percent only, not a tonne delta.

The API already stores the band:

- `min_biomass` = 85% of the estimate
- `max_biomass` = 115% of the estimate

Prefer those fields. If a payload only has a single number `n`, display `n * 0.85` to `n * 1.15`.

Digits: 0 when the value is at least 100, 1 when it is at least 10, otherwise 2. Zero is `0 t`. Format with Indian grouping (`en-IN`).

2025 higher than 2024 is `#047857`. 2025 lower is `#b91c1c`. Comparison bars: 2024 `#475569`, 2025 `#059669`.

`rate_source` text, when a field is open:

- `scenes` — “Calculated from this village's Sentinel-2 scenes.”
- `village_average` — “This village had no scenes, so the rate is the average of villages that did.”
- `mixed` — “One season used this village's scenes. The other used the average of villages with scenes.”

Every identified crop has biomass, each with its own formula (`GET /biomass/crops`). A double crop such as `Rice + Wheat` stores the Kharif part (`kharif_crop`, `kharif_tonnes`) and the Rabi part (`rabi_crop`, `rabi_tonnes`). `total_tonnes` is both. `year` is the Kharif year, so Rabi 2023-24 is 2023. `Fallow/Uncertain` has no biomass.

### Biomass panel

1. Short note: min is 15% below the estimate, max is 15% above it.
2. Selected village card: load `GET /biomass/compare?village=…`. One card per year with Min/Max, t/ha, field count, top crops, and % change versus the previous year. Compare villages year by year — never match the same field_id across years.
3. Clicked field: detail only (crop, year, hectares, Min/Max). Not used for year-to-year compare.
4. Village chart: whole-village biomass per year, Total plus the six biggest crops.
5. All-villages card: sum of every village per year, same layout. Load `GET /biomass/places` before drawing this card. An empty list is not zero biomass.
6. Scrollable village list. Each row is the village name plus one range per year. Clicking a row selects that village.

### Biomass endpoints

| Call | Use |
|---|---|
| `GET /biomass/places` | Every village, every year. Each year has totals plus `crops` (one entry per single crop; a `Rice + Wheat` field adds to Rice and to Wheat). Not paged. |
| `GET /biomass/summary?year=2025` | District totals per identified label in `results`, per single crop in `by_crop`. Optional `crop=Wheat`. |
| `GET /biomass/compare?village=Agarikalan` | Whole-village biomass year by year: `totals` and `crops`, each year with `tonnes`, `biomass_t_ha`, Min / Max, and change versus the previous year. Optional `crop=Rice`. |
| `GET /biomass/crops` | Formula, RUE, season window, and index for every crop. |
| `GET /biomass/village?village=Agarikalan&year=2025&skip=0&limit=100` | Village totals plus one page of `records`. `fields` is the full count. |
| `GET /biomass/field?field_id=17&village=Agarikalan&year=2025` | One plot. `field_id` is the plot number in that village. Add `crop=Rice` when the plot has more than one crop. |
| `GET /biomass/fields?year=2025&subdistrict=Kawardha&skip=0&limit=100` | Every biomass row in the subdistrict, one page. Add `village=Agarikalan` for one village. Optional `crop=Wheat` also matches `Rice + Wheat`. |

Village and field lists default to 100 rows and accept up to 1,000. A page does not drop the rest of the data: `total` is the full count.

Walk pages until `has_more` is false:

```
skip = 0
repeat
  GET ...&skip={skip}&limit=100
  append the page
  stop when has_more is false
  skip = next_skip
```

## All fields and one village

Attribute lists are paged. Polygons for a whole subdistrict are not drawn.

| Scope | Endpoint |
|---|---|
| Every crop field in Kawardha, 2025 (270,348 rows) | `GET /crops/fields?subdistrict=Kawardha&year=2025&skip=0&limit=1000` |
| One village | `GET /crops/fields?subdistrict=Kawardha&village=Agarikalan&year=2025&skip=0&limit=1000` |
| Field points for the subdistrict | `GET /crops/points?subdistrict=Kawardha&year=2025&skip=0&limit=1000` |
| Polygons for one village | `GET /boundaries?district_name=Kabirdham&subdistrict_name=Kawardha&village_name=Agarikalan&year=2025` |
| Village outlines | `GET /boundaries?district_name=Kabirdham&subdistrict_name=Kawardha` |

`/crops/fields` and `/crops/points` return `total`, `count`, `has_more`, and `next_skip`. Keep requesting until `has_more` is false.

## Records

`GET /kawardha/hierarchy` builds the tehsil, RI, and village tree.

`GET /kawardha/plots?village_id={id}&skip=0&limit=100` fills the plot table (khasra, area, land use, owner).

`GET /kawardha/plots/map?village_id={id}&valid_geo=false` draws that village’s parcels.

`GET /kawardha-data?district=Kabirdham&tehsil=Kawardha&village=Hedaspur` is the administrative outline, not the crop-field layer.

## Dashboard (indices)

Index district names are lowercase (`kabirdham`, `kawardha`).

| Call | Use |
|---|---|
| `GET /indices/places?district=kabirdham` | Dropdown keys. Empty subdistrict and village means the district. Empty village with a subdistrict means the subdistrict. |
| `GET /indices/retrieve-aggregated?district=kabirdham&subdistrict=kawardha&village=agarikalan&frequency=monthly` | Chart series. `frequency` is `weekly`, `monthly`, or `yearly`. |
| `GET /indices/retrieve?district=kabirdham&subdistrict=kawardha&village=agarikalan&start_date=2024-01-01&end_date=2024-12-31` | One row per Sentinel-2 scene. |
| `GET /indices/stored-locations?district=kabirdham` | Places that already have scenes. |

Default charts: NDVI, EVI, GNDVI, NDMI, SAVI, NDWI McFeeters. The full series keys are in `INDEX_SERIES` at the top of `app.js`.

## Numbers already checked

- 2025 crop fields in Kawardha: 270,348 rows. Page them. Do not draw all of them on the map.
