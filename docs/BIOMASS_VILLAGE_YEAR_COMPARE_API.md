# Biomass year-wise village comparison — API reference

Endpoints used by the frontend (`frontend/app.js`) for **village-to-village, year-by-year biomass visualization**. Fields are **not** matched across years.

Base URL (local):

```
http://127.0.0.1:8000
```

Interactive docs: `http://127.0.0.1:8000/docs`

---

## Inputs: year + season (Chhattisgarh crop calendar)

Biomass is keyed by **Kharif year** and optional **season**:

| Query | Values | Meaning |
|---|---|---|
| `year` | `2023` … `2026` | Kharif year `Y` (covers Kharif Y and Rabi Y–Y+1) |
| `season` | `kharif` / `rabi` / omit | Return only that calendar season’s crops |

| Season | Crops returned |
|---|---|
| `kharif` | Rice, Maize, Soybean, Cotton, Arhar (Tur), Sugarcane |
| `rabi` | Wheat, Mustard, Linseed (Tisi), Gram (Chana), Lentil (Masoor) |

Store / recompute (CLI):

```bash
python -u -m app.biomass --years 2023,2024,2025,2026 --seasons kharif,rabi
```

## How the frontend uses these calls

| UI piece | Endpoint | Purpose |
|---|---|---|
| All-villages list + district year cards + chart data | `GET /biomass/places?season=…` | Every village, every year, season crop split |
| Selected village year cards (tonnes, t/ha, % change) | `GET /biomass/compare?village=…&season=…` | Whole-village year compare |
| Optional field popup (detail only, not year compare) | `GET /biomass/field?…` | One plot’s stored biomass |

Related (not required for the compare panel, useful for docs / tools):

| Endpoint | Purpose |
|---|---|
| `GET /biomass/crops` | Formula, RUE, season window per crop |
| `GET /biomass/summary` | District totals by label / by single crop |
| `GET /biomass/village` | One village, one year, paged field rows |
| `GET /biomass/fields` | Paged field rows by year / place |

**Rule:** year compare always sums the whole village for that year. Never join on `field_id` across years.

---

## 1. `GET /biomass/places`

All villages, all stored years. Not paged. Used to draw the village list, “All villages” year cards, and the bar chart series.

### Request

```
GET /biomass/places
GET /biomass/places?season=kharif
GET /biomass/places?season=rabi
```

| Query | Required | Description |
|---|---|---|
| `season` | No | `kharif` or `rabi`. Omit for both seasons |

### Response

```json
{
  "count": 176,
  "years": ["2023", "2024", "2025", "2026"],
  "villages": [
    {
      "village": "Agarikalan",
      "2023": {
        "fields": 513,
        "tonnes": 2222.23,
        "area_ha": 90.53,
        "min_biomass": 1888.9,
        "max_biomass": 2555.56,
        "crops": {
          "Wheat": {
            "fields": 200,
            "tonnes": 721.19,
            "area_ha": 56.4,
            "min_biomass": 613.01,
            "max_biomass": 829.37
          },
          "Rice": {
            "fields": 180,
            "tonnes": 688.08,
            "area_ha": 64.6,
            "min_biomass": 584.87,
            "max_biomass": 791.29
          }
        },
        "rice": { "fields": 180, "tonnes": 688.08, "area_ha": 64.6, "min_biomass": 584.87, "max_biomass": 791.29 },
        "cotton": { "fields": 0, "tonnes": 0, "area_ha": 0, "min_biomass": 0, "max_biomass": 0 }
      },
      "2024": { "fields": 448, "tonnes": 774.49, "area_ha": 75.91, "min_biomass": 658.32, "max_biomass": 890.66, "crops": {}, "rice": {}, "cotton": {} },
      "2025": { "fields": 479, "tonnes": 1913.53, "area_ha": 83.78, "min_biomass": 1626.5, "max_biomass": 2200.56, "crops": {}, "rice": {}, "cotton": {} },
      "2026": { "fields": 156, "tonnes": 69.07, "area_ha": 23.58, "min_biomass": 58.71, "max_biomass": 79.43, "crops": {}, "rice": {}, "cotton": {} }
    }
  ]
}
```

### Field notes

| Field | Meaning |
|---|---|
| `years` | Stored Kharif years as strings, sorted |
| `villages[].village` | Village name |
| `villages[]["2025"]` | Year bucket keyed by year string |
| `fields` | Number of biomass rows (identified fields) that year |
| `tonnes` | Sum of `total_tonnes` for the village that year |
| `area_ha` | Sum of field area (ha) |
| `min_biomass` / `max_biomass` | ±15% band on tonnes |
| `crops` | Single-crop parts. A `Rice + Wheat` field adds to **Rice** and to **Wheat** |
| `rice` / `cotton` | Convenience copies of `crops.Rice` / `crops.Cotton` (may be empty) |

### Frontend use (`frontend/app.js`)

- `loadVillageBiomass()` → `GET /biomass/places`
- Village list ranges: `biomassVillageList`
- District rollup: `biomassTotals`
- Chart: `drawVillageYearly` / `biomassSeries` (Total + top crops)

---

## 2. `GET /biomass/compare`  ← main year-wise village compare

Whole-village biomass for one village, every year. Includes % change versus the previous year that had data. **No field-to-field matching.**

### Request

```
GET /biomass/compare?village=Agarikalan
GET /biomass/compare?village=Agarikalan&season=kharif
GET /biomass/compare?village=Agarikalan&season=rabi&crop=Wheat
```

| Query | Required | Description |
|---|---|---|
| `village` | Yes | Exact village name (e.g. `Agarikalan`) |
| `season` | No | `kharif` or `rabi`. Omit for both; response crops are calendar-filtered |
| `crop` | No | Filter `crops[]` to one single crop (e.g. `Rice`, `Wheat`). Totals stay season/village |

### Success response

```json
{
  "village": "Agarikalan",
  "years": [2023, 2024, 2025, 2026],
  "totals": [
    {
      "year": 2023,
      "fields": 513,
      "area_ha": 90.533,
      "tonnes": 2222.2329,
      "min_biomass": 1888.898,
      "max_biomass": 2555.5678,
      "biomass_t_ha": 24.5461,
      "seasons": ["Kharif 2023 - Rabi 2023-24"],
      "change_tonnes": null,
      "change_pct": null,
      "change_t_ha_pct": null
    },
    {
      "year": 2024,
      "fields": 448,
      "area_ha": 75.9088,
      "tonnes": 774.49,
      "min_biomass": 658.3165,
      "max_biomass": 890.6635,
      "biomass_t_ha": 10.2029,
      "seasons": ["Kharif 2024"],
      "change_tonnes": -1447.7429,
      "change_pct": -65.15,
      "change_t_ha_pct": -58.43
    },
    {
      "year": 2025,
      "fields": 479,
      "area_ha": 83.7773,
      "tonnes": 1913.5326,
      "min_biomass": 1626.5027,
      "max_biomass": 2200.5625,
      "biomass_t_ha": 22.8407,
      "seasons": ["Kharif 2025", "Kharif 2025 - Rabi 2025-26"],
      "change_tonnes": 1139.0426,
      "change_pct": 147.07,
      "change_t_ha_pct": 123.86
    }
  ],
  "crops": [
    {
      "crop": "Rice",
      "years": [
        {
          "year": 2023,
          "fields": 180,
          "area_ha": 64.6,
          "tonnes": 688.0833,
          "min_biomass": 584.8708,
          "max_biomass": 791.2958,
          "biomass_t_ha": 10.6486,
          "change_tonnes": null,
          "change_pct": null,
          "change_t_ha_pct": null
        },
        {
          "year": 2024,
          "fields": 448,
          "area_ha": 73.65,
          "tonnes": 710.101,
          "min_biomass": 603.5859,
          "max_biomass": 816.6161,
          "biomass_t_ha": 9.6416,
          "change_tonnes": 22.0177,
          "change_pct": 3.2,
          "change_t_ha_pct": -9.46
        }
      ]
    }
  ]
}
```

### Field notes

| Field | Meaning |
|---|---|
| `years` | All stored years (ints) |
| `totals[]` | Whole-village sum per year (all crops) |
| `totals[].biomass_t_ha` | `tonnes / area_ha` — fairer when field counts differ between years |
| `totals[].seasons` | Season labels that contributed rows that year |
| `totals[].change_pct` | % change in tonnes vs previous year that had fields |
| `totals[].change_t_ha_pct` | % change in t/ha vs that previous year |
| `crops[]` | Same shape, one entry per single crop (Rice part of Rice + Wheat counts as Rice) |
| Empty year | `fields: 0`, `tonnes: 0`, change fields `null` (not −100%) |

### Errors

| Status | When |
|---|---|
| `400` | Missing `village` |
| `404` | No biomass rows for that village |

### Frontend use

- `loadVillageCompare(village)` → `GET /biomass/compare?village=…`
- Year cards: `biomassYearCardsFromCompare` (tonnes band, t/ha, field count, % vs previous year, top crops)

---

## 3. `GET /biomass/field`  (detail only)

One plot. Used for map popup / “selected field” strip. **Not** used for year-to-year compare.

### Request

```
GET /biomass/field?field_id=17&village=Agarikalan&year=2025
GET /biomass/field?field_id=17&village=Agarikalan&year=2025&crop=Rice
GET /biomass/field?crop_id=606402
```

| Query | Required | Description |
|---|---|---|
| `field_id` | Yes* | Plot number in the village |
| `crop_id` | Yes* | Internal biomass / crop_identification id (alternative to `field_id`) |
| `village` | Recommended | Disambiguates the same `field_id` in different villages |
| `year` | Optional | Kharif year |
| `crop` | Optional | Label or component (e.g. `Wheat` matches `Rice + Wheat`) |

\* One of `field_id` or `crop_id` is required.

### Response (single match)

```json
{
  "crop_id": 606402,
  "field_boundary_id": 300821,
  "field_id": 4,
  "district_name": "Kabirdham",
  "subdistrict_name": "Kawardha",
  "village_name": "Agarikalan",
  "season": "Kharif 2025 - Rabi 2025-26",
  "crop": "Arhar (Tur) + Wheat",
  "kharif_crop": "Arhar (Tur)",
  "kharif_t_ha": 6.811912,
  "kharif_tonnes": 4.012897,
  "rabi_crop": "Wheat",
  "rabi_t_ha": 12.526178,
  "rabi_tonnes": 7.379171,
  "year": 2025,
  "observation_date": "2026-04-14",
  "season_start": "2025-06-01",
  "season_end": "2026-04-14",
  "area_ha": 0.5891,
  "biomass_t_ha": 19.33809,
  "total_tonnes": 11.392069,
  "min_biomass": 9.683259,
  "max_biomass": 13.100879,
  "scene_count": 95,
  "rate_source": "scenes"
}
```

If several rows match:

```json
{
  "field_id": 17,
  "count": 2,
  "fields": [ { "...": "..." }, { "...": "..." } ]
}
```

### `rate_source` values

| Value | Meaning |
|---|---|
| `scenes` | Rate from this village’s Sentinel-2 scenes |
| `village_average` | No scenes; used average of villages that had scenes |
| `mixed` | One season from scenes, the other from average |

---

## 4. Supporting endpoints

### `GET /biomass/crops`

Formula settings for every crop (RUE, window, index).

```
GET /biomass/crops
```

```json
{
  "formula": "t/ha = 0.01 x sum(PAR(month) x fAPAR x RUE x stress) over the crop's season",
  "fapar": "1.24 x index - 0.16, kept between 0 and 0.95",
  "par_mj_per_day": { "1": 8.2, "6": 10.1, "9": 8.4 },
  "stress": 1.0,
  "crops": [
    { "crop": "Rice", "rue_g_per_mj": 2.2, "window": "kharif", "index": "NDVI" },
    { "crop": "Wheat", "rue_g_per_mj": 2.8, "window": "rabi", "index": "NDVI" }
  ],
  "double_crops": "Kharif crop over the Kharif window plus Rabi crop over the Rabi window"
}
```

### `GET /biomass/summary`

District-level totals.

```
GET /biomass/summary
GET /biomass/summary?year=2025
GET /biomass/summary?year=2025&crop=Wheat
```

```json
{
  "count": 12,
  "results": [
    {
      "year": 2025,
      "crop": "Rice + Wheat",
      "fields": 4248,
      "villages": 63,
      "area_ha": 820.1,
      "total_tonnes": 18500.2,
      "min_biomass": 15725.17,
      "max_biomass": 21275.23,
      "biomass_t_ha": 22.56,
      "fields_from_scenes": 3000,
      "fields_from_average": 1248
    }
  ],
  "by_crop": [
    {
      "year": 2025,
      "crop": "Wheat",
      "fields": 24705,
      "villages": 151,
      "area_ha": 5074.5,
      "total_tonnes": 56799.08,
      "biomass_t_ha": 11.193,
      "min_biomass": 48279.22,
      "max_biomass": 65318.94
    }
  ]
}
```

- `results` = identified labels (`Rice + Wheat`, `Sugarcane`, …)
- `by_crop` = single-crop parts (`Wheat` includes the Wheat half of double crops)

### `GET /biomass/village`

One village, one year, plus a page of field rows.

```
GET /biomass/village?village=Agarikalan&year=2025&skip=0&limit=100
GET /biomass/village?village=Agarikalan&year=2025&crop=Rice&skip=0&limit=25
```

| Query | Required | Default | Description |
|---|---|---|---|
| `village` | Yes | — | Village name |
| `year` | Yes | — | Kharif year |
| `crop` | No | — | Label or component |
| `skip` | No | `0` | Offset |
| `limit` | No | `100` | Page size (max `1000`) |

```json
{
  "village": "Agarikalan",
  "year": 2025,
  "crop": null,
  "total": 479,
  "fields": 479,
  "skip": 0,
  "limit": 100,
  "count": 100,
  "area_ha": 83.7773,
  "total_tonnes": 1913.5326,
  "min_biomass": 1626.5027,
  "max_biomass": 2200.5625,
  "records": [
    {
      "crop_id": 606402,
      "field_id": 4,
      "village_name": "Agarikalan",
      "season": "Kharif 2025 - Rabi 2025-26",
      "crop": "Arhar (Tur) + Wheat",
      "kharif_crop": "Arhar (Tur)",
      "kharif_tonnes": 4.012897,
      "rabi_crop": "Wheat",
      "rabi_tonnes": 7.379171,
      "year": 2025,
      "area_ha": 0.5891,
      "biomass_t_ha": 19.33809,
      "total_tonnes": 11.392069,
      "min_biomass": 9.683259,
      "max_biomass": 13.100879,
      "rate_source": "scenes"
    }
  ]
}
```

### `GET /biomass/fields`

Paged field rows for a year (optional place filters).

```
GET /biomass/fields?year=2025&subdistrict=Kawardha&skip=0&limit=100
GET /biomass/fields?year=2025&village=Agarikalan&crop=Wheat&skip=0&limit=100
```

| Query | Required | Default | Description |
|---|---|---|---|
| `year` | Yes | — | Kharif year |
| `crop` | No | — | Label or component |
| `subdistrict` | No | — | e.g. `Kawardha` |
| `village` | No | — | Village name |
| `skip` | No | `0` | Offset |
| `limit` | No | `100` | Page size (max `1000`) |

```json
{
  "total": 56358,
  "skip": 0,
  "limit": 100,
  "count": 100,
  "has_more": true,
  "next_skip": 100,
  "fields": [ { "...same columns as /biomass/field..." } ]
}
```

Walk pages until `has_more` is false:

```
skip = 0
repeat
  GET /biomass/fields?year=2025&subdistrict=Kawardha&skip={skip}&limit=100
  append fields
  stop when has_more is false
  skip = next_skip
```

---

## Display rules used by the frontend

| Rule | Value |
|---|---|
| Min / Max band | `min_biomass = 0.85 × tonnes`, `max_biomass = 1.15 × tonnes` (also stored in API) |
| Year key | Kharif year (`2025` covers Rabi 2025–26 parts) |
| Compare unit | Whole village per year (and optionally per single crop) |
| Fair rate when coverage differs | Prefer `biomass_t_ha` over raw `tonnes` |
| Chart colors | Total `#176b66`, Arhar `#fad036`, Sugarcane `#f04c46`, Mustard `#4f78e9`, Maize `#a849f3`, Wheat `#f78229`, Rice `#23bd67` |
| Up / down | Higher than previous year `#047857`, lower `#b91c1c` |

---

## Minimal client flow (year-wise village compare)

```javascript
// 1) Load every village once
const places = await fetch("/biomass/places").then((r) => r.json());
// places.years -> ["2023","2024","2025","2026"]
// places.villages[i]["2025"].tonnes -> village total

// 2) When user selects a village
const compare = await fetch(
  "/biomass/compare?village=" + encodeURIComponent("Agarikalan")
).then((r) => r.json());

// Year cards
compare.totals.forEach((y) => {
  console.log(y.year, y.tonnes, y.biomass_t_ha, y.change_pct);
});

// Crop series (village totals per crop per year — not field matching)
compare.crops.forEach((c) => {
  console.log(c.crop, c.years.map((y) => [y.year, y.tonnes]));
});
```

---

## Refresh stored biomass (server / ops)

Not a frontend call. From the repository root:

```bash
python -m app.biomass
python -m app.biomass --years 2023,2025
```

This recomputes and replaces rows in the `biomass` MySQL table for the requested years.
