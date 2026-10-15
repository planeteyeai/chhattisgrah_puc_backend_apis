"""
Kabirdham Village Crop Detection — VS Code Standalone Script
=============================================================
Extracted from the tested and working Colab notebook.
Updated for 2025 and 2026 seasons.

Put these files in the same folder:
  - pipelines/crop_identification/kabirdham_run.py
  - crop_classifier_model_full.joblib
  - crop_classifier_model_sugarcane_xgb.joblib
  - Kabirdham_field_boundaries_by_village.zip

Run from repository root:
  python pipelines/crop_identification/kabirdham_run.py
  python pipelines/crop_identification/kabirdham_run.py --year 2026

If interrupted, just run again — progress is saved after every village.
"""

import joblib, math, time, json, os, re, sys, zipfile, warnings, argparse
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
import pandas as pd
import numpy as np
from shapely.geometry import Polygon
from pathlib import Path
from collections import defaultdict

warnings.filterwarnings('ignore', category=FutureWarning)

SCRIPT_DIR = Path(__file__).parent

# ── CLI ───────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description='Kabirdham Village Crop Detection')
parser.add_argument('--year',        type=int, default=2025,
                    help='Kharif year (sets both rice and sugarcane year). Default: 2025')
parser.add_argument('--rice_year',   type=int, help='Override: year for Rice/Wheat/Cotton imagery')
parser.add_argument('--rice_month',  type=int, help='Override: month for Rice/Wheat/Cotton (default: 9=Sep)')
parser.add_argument('--sc_year',     type=int, help='Override: year for Sugarcane imagery')
parser.add_argument('--sc_month',    type=int, help='Override: month for Sugarcane (default: 11=Nov)')
args = parser.parse_args()

RICE_YEAR  = args.rice_year  or args.year
RICE_MONTH = args.rice_month or 9
SC_YEAR    = args.sc_year    or args.year
SC_MONTH   = args.sc_month   or 11
SEASON     = f'Kharif {args.year}'

PROGRESS_FILE = SCRIPT_DIR / f'kabirdham_progress_{args.year}.json'
RESULTS_FILE  = SCRIPT_DIR / f'kabirdham_results_{args.year}.json'

print(f"{'='*60}")
print(f"  Kabirdham Village Crop Detection — {SEASON}")
print(f"  Rice/Wheat/Cotton: latest scene in the window around {RICE_YEAR}-{RICE_MONTH:02d}-15")
print(f"  Sugarcane:         latest scene in the window around {SC_YEAR}-{SC_MONTH:02d}-15")
print(f"  Progress file:     {PROGRESS_FILE.name}")
print(f"{'='*60}")

# ── Constants ─────────────────────────────────────────────────────────────
GEE_PROJECT          = 'inlaid-way-490106-u1'
TARGET_CROPS         = {'Rice', 'Sugarcane', 'Wheat', 'Cotton'}
CONFIDENCE_THRESHOLD = 0.75
MARGIN_THRESHOLD     = 0.20
MIN_AREA_M2          = 900.0
TREE_CIRC_THRESH     = 0.78
TREE_ASPECT_THRESH   = 0.72
TREE_MAX_AREA_M2     = 20000
TREE_MIN_VERTS       = 16
CHUNK_SIZE           = 50
CHUNK_DELAY          = 6
WINDOW_DAYS          = 15
CLOUD_MAX            = 60
SCALE                = 10
NS                   = 'http://www.opengis.net/kml/2.2'
S2_BANDS   = ['B2','B3','B4','B5','B6','B7','B8','B8A','B11','B12']
S2_INDICES = ['NDVI','GNDVI','NDRE','NDWI','NDMI','SAVI','EVI','BSI',
              'RGI','GCI','MCARI','NDBI','ARVI','MSAVI2']
S1_BANDS   = ['VV','VH','VH_VV_ratio','RVI']
ALL_BANDS  = S2_BANDS + S2_INDICES + S1_BANDS
TIME_OFFSETS = {'tm45': -45, 't0': 0, 'tp45': 45}

# ── Verify required files ─────────────────────────────────────────────────
FULL_MODEL_PATH = SCRIPT_DIR / 'crop_classifier_model_full.joblib'
SC_MODEL_PATH   = SCRIPT_DIR / 'crop_classifier_model_sugarcane_xgb.joblib'
KML_ZIP_PATH    = SCRIPT_DIR / 'Kabirdham_field_boundaries_by_village.zip'

for f in [FULL_MODEL_PATH, SC_MODEL_PATH, KML_ZIP_PATH]:
    if not f.exists():
        print(f"ERROR: Missing file: {f}")
        sys.exit(1)
print("All input files found.")

# ── Load models ───────────────────────────────────────────────────────────
print("Loading models...")
bundle_full  = joblib.load(FULL_MODEL_PATH)
model_full   = bundle_full['model']
imputer_full = bundle_full['imputer']
feat_full    = bundle_full['feature_cols']
labels_full  = bundle_full['label_names']
print(f"  Full model: {labels_full}")

bundle_sc  = joblib.load(SC_MODEL_PATH)
model_sc   = bundle_sc['model']
imputer_sc = bundle_sc['imputer']
feat_sc    = bundle_sc['feature_cols']
labels_sc  = bundle_sc['label_names']
print(f"  Sugarcane model: {labels_sc}")

assert set(feat_full) == set(feat_sc), 'Feature mismatch between models!'
feature_cols = feat_full
print(f"  Features: {len(feature_cols)}")

# ── GEE init ─────────────────────────────────────────────────────────────
import ee

sa_email = os.environ.get('EE_SERVICE_ACCOUNT_EMAIL')
sa_key   = os.environ.get('EE_SERVICE_ACCOUNT_KEY')
if sa_email and sa_key and os.path.exists(sa_key):
    creds = ee.ServiceAccountCredentials(sa_email, sa_key)
    ee.Initialize(creds, project=GEE_PROJECT)
    print(f"GEE: service account")
else:
    ee.Initialize(project=GEE_PROJECT)
    print("GEE: stored credentials")

# ── GEE image functions (exact same as working notebook) ──────────────────
def mask_clouds(img):
    qa = img.select('QA60')
    return img.updateMask(qa.bitwiseAnd(1<<10).eq(0).And(qa.bitwiseAnd(1<<11).eq(0)))

def add_indices(img):
    b = {n: img.select(n) for n in S2_BANDS}
    return img.addBands([
        img.normalizedDifference(['B8','B4']).rename('NDVI'),
        img.normalizedDifference(['B8','B3']).rename('GNDVI'),
        img.normalizedDifference(['B8A','B5']).rename('NDRE'),
        img.normalizedDifference(['B3','B8']).rename('NDWI'),
        img.normalizedDifference(['B8','B11']).rename('NDMI'),
        b['B8'].subtract(b['B4']).multiply(1.5).divide(b['B8'].add(b['B4']).add(0.5)).rename('SAVI'),
        b['B8'].subtract(b['B4']).multiply(2.5).divide(
            b['B8'].add(b['B4'].multiply(6)).subtract(b['B2'].multiply(7.5)).add(1)).rename('EVI'),
        (b['B11'].add(b['B4'])).subtract(b['B8'].add(b['B2'])).divide(
            (b['B11'].add(b['B4'])).add(b['B8'].add(b['B2']))).rename('BSI'),
        b['B4'].divide(b['B3']).rename('RGI'),
        b['B8'].divide(b['B3']).subtract(1).rename('GCI'),
        ((b['B5'].subtract(b['B4'])).subtract(
            (b['B5'].subtract(b['B3'])).multiply(0.2))).multiply(
            b['B5'].divide(b['B4'])).rename('MCARI'),
        img.normalizedDifference(['B11','B8']).rename('NDBI'),
        (b['B8'].subtract(b['B4'].multiply(2)).subtract(b['B2'])).divide(
            b['B8'].add(b['B4'].multiply(2)).subtract(b['B2'])).rename('ARVI'),
        b['B8'].multiply(2).add(1).subtract(
            ((b['B8'].multiply(2).add(1)).pow(2).subtract(
                b['B8'].subtract(b['B4']).multiply(8))).sqrt()).multiply(0.5).rename('MSAVI2'),
    ])

def _keep_time(img):
    return img.set('system:time_start', img.get('system:time_start'))

def _latest_image(collection, dummy):
    """Newest scene in the window. An empty window stays masked."""
    newest = collection.map(_keep_time)
    underneath = dummy.set('system:time_start', 0)
    return ee.Image(ee.ImageCollection([underneath]).merge(newest).sort('system:time_start').mosaic())

def _s2_collection(start, end, region):
    return (ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED')
            .filterDate(start, end)
            .filterBounds(region)
            .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', CLOUD_MAX))
            .map(mask_clouds)
            .map(lambda img: _keep_time(img.select(S2_BANDS).toFloat())))

def _s2_dummy(region):
    return (ee.Image.constant([0.0]*len(S2_BANDS)).rename(S2_BANDS)
            .toFloat().updateMask(ee.Image.constant(0)).clip(region))

def s2_composite(ref_date, region):
    """Latest clear Sentinel-2 scene in the 15 days before and after ref_date."""
    start = ref_date.advance(-WINDOW_DAYS, 'day')
    end = ref_date.advance(WINDOW_DAYS, 'day')
    latest = _latest_image(_s2_collection(start, end, region), _s2_dummy(region))
    return add_indices(latest.select(S2_BANDS))

def s1_composite(ref_date, region):
    """Latest Sentinel-1 scene in the same window."""
    start = ref_date.advance(-WINDOW_DAYS, 'day')
    end = ref_date.advance(WINDOW_DAYS, 'day')
    col = (ee.ImageCollection('COPERNICUS/S1_GRD')
           .filterDate(start, end)
           .filterBounds(region)
           .filter(ee.Filter.eq('instrumentMode','IW'))
           .filter(ee.Filter.listContains('transmitterReceiverPolarisation','VV'))
           .filter(ee.Filter.listContains('transmitterReceiverPolarisation','VH'))
           .select(['VV','VH']).map(lambda img: _keep_time(img.toFloat())))
    dummy = (ee.Image.constant([0.0,0.0]).rename(['VV','VH'])
             .toFloat().updateMask(ee.Image.constant(0)).clip(region))
    comp = _latest_image(col, dummy).select(['VV','VH'])
    vv, vh = comp.select('VV'), comp.select('VH')
    return comp.addBands([vh.subtract(vv).rename('VH_VV_ratio'),
                          vh.multiply(4).divide(vv.add(vh)).rename('RVI')])

def _scene_millis_band(year, month, region):
    """Latest clear scene in the full window: 60 days either side of the 15th."""
    center = ee.Date.fromYMD(year, month, 15)
    reach = max(abs(offset) for offset in TIME_OFFSETS.values()) + WINDOW_DAYS
    collection = _s2_collection(center.advance(-reach, 'day'), center.advance(reach, 'day'), region)
    newest = collection.sort('system:time_start', False)
    peak = newest.aggregate_max('system:time_start')
    millis = ee.Number(ee.List([peak, 0]).reduce(ee.Reducer.firstNonNull()))
    latest = _latest_image(collection, _s2_dummy(region))
    return ee.Image.constant(millis).toDouble().rename('scene_millis').updateMask(latest.select('B8').mask())

def stacked_image(year, month, region):
    ref = ee.Date.fromYMD(year, month, 15)
    stacked = None
    for suffix, offset in TIME_OFFSETS.items():
        d = ref.advance(offset, 'day')
        combo = s2_composite(d, region).addBands(s1_composite(d, region)).select(ALL_BANDS)
        renamed = combo.rename([f'{b}_{suffix}' for b in ALL_BANDS])
        stacked = renamed if stacked is None else stacked.addBands(renamed)
    return stacked.addBands(_scene_millis_band(year, month, region))

def scene_date_text(millis):
    if millis is None:
        return None
    value = float(millis)
    if not math.isfinite(value) or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000.0, timezone.utc).date().isoformat()

# ── Geometry helpers (exact same as working notebook) ─────────────────────
def poly_area_m2(pts):
    try:
        poly = Polygon(pts)
        if not poly.is_valid: poly = poly.buffer(0)
        lat0 = sum(p[1] for p in pts) / len(pts)
        kx = 111320 * math.cos(math.radians(lat0))
        return float(poly.area * kx * 111320)
    except: return 0.0

def is_tree(pts):
    try:
        poly = Polygon(pts)
        if not poly.is_valid: poly = poly.buffer(0)
        if poly.is_empty or poly.area <= 0: return False
        perim = poly.length
        if perim <= 0: return False
        circ = 4 * math.pi * poly.area / perim ** 2
        minx, miny, maxx, maxy = poly.bounds
        w, h = maxx - minx, maxy - miny
        asp = min(w, h) / max(w, h) if max(w, h) > 0 else 0.0
        area_m2 = poly_area_m2(pts)
        return (area_m2 < TREE_MAX_AREA_M2
                and circ > TREE_CIRC_THRESH
                and asp  > TREE_ASPECT_THRESH
                and len(pts) >= TREE_MIN_VERTS)
    except: return False

# ── Parse KML ─────────────────────────────────────────────────────────────
print(f"\nExtracting {KML_ZIP_PATH.name}...")
kml_extract_dir = SCRIPT_DIR / 'kml_extracted'
kml_extract_dir.mkdir(exist_ok=True)
with zipfile.ZipFile(KML_ZIP_PATH) as z:
    z.extractall(kml_extract_dir)

TARGET_KML = 'Kabirdham_field_boundaries_by_village.kml'
KML_PATH   = None
for p in kml_extract_dir.rglob('*.kml'):
    if p.name == TARGET_KML:
        KML_PATH = p
        break
if KML_PATH is None:
    all_kmls = list(kml_extract_dir.rglob('*.kml'))
    KML_PATH = max(all_kmls, key=lambda p: p.stat().st_size)

print(f"Parsing {KML_PATH.name} ({KML_PATH.stat().st_size/1024/1024:.0f} MB)...")
root_kml = ET.parse(KML_PATH).getroot()
folders  = list(root_kml.iter(f'{{{NS}}}Folder'))
print(f"Found {len(folders)} folders")

villages = {}
for folder in folders:
    name_el = folder.find(f'{{{NS}}}name')
    if name_el is None: continue
    nm = name_el.text.strip()
    if not nm.startswith('Village'): continue
    if '\u2014' in nm:
        raw = nm.split('\u2014')[1].strip()
    elif ' - ' in nm:
        raw = nm.split(' - ', 1)[1].strip()
    else:
        raw = nm.replace('Village','').strip()
    village_name = re.sub(r'\s*\(\d+.*?\)\s*$', '', raw).strip()

    polygons = []
    for pm in folder.iter(f'{{{NS}}}Placemark'):
        meta = {}
        for d in pm.iter(f'{{{NS}}}Data'):
            val_el = d.find(f'{{{NS}}}value')
            if val_el is not None:
                meta[d.get('name')] = val_el.text
        coords_el = pm.find(f'.//{{{NS}}}coordinates')
        if coords_el is None or not coords_el.text: continue
        pts = []
        for tok in coords_el.text.strip().split():
            p = tok.split(',')
            if len(p) >= 2:
                try: pts.append((float(p[0]), float(p[1])))
                except ValueError: pass
        if len(pts) < 3: continue
        polygons.append({
            'pts'     : pts,
            'field_id': str(meta.get('field_id', '')),
            'area_ha' : float(meta.get('area_ha', 0) or 0),
            'village' : village_name,
            'tehsil'  : meta.get('tehsil', 'KAWARDHA'),
        })
    if polygons:
        villages[village_name] = polygons

total_polygons = sum(len(v) for v in villages.values())
print(f"Parsed {len(villages)} villages, {total_polygons:,} polygons")
if not villages:
    print("ERROR: 0 villages parsed. Check KML file.")
    sys.exit(1)

# ── GEE extraction helper (exact same as working notebook) ────────────────
reducer = ee.Reducer.mean().combine(ee.Reducer.stdDev(), sharedInputs=True)

def gee_extract(usable_list, year, month):
    n_ch      = math.ceil(len(usable_list) / CHUNK_SIZE)
    feat_rows = [None] * len(usable_list)
    n_errors  = 0
    for ch_num in range(n_ch):
        chunk    = usable_list[ch_num * CHUNK_SIZE:(ch_num + 1) * CHUNK_SIZE]
        feats_ee = [ee.Feature(ee.Geometry.Polygon([c['pts']]), {'idx': i})
                    for i, c in enumerate(chunk)]
        fc       = ee.FeatureCollection(feats_ee)
        region   = fc.geometry().bounds()
        image    = stacked_image(year, month, region)
        stats_fc = image.reduceRegions(collection=fc, reducer=reducer,
                                        scale=SCALE, tileScale=4)
        stats = []
        for attempt in range(5):
            try:
                stats = stats_fc.getInfo()['features']
                break
            except Exception as e:
                msg = str(e).lower()
                if any(x in msg for x in ['too many requests','concurrency','quota','rate','429']):
                    wait = 10 * (2 ** attempt)
                    print(f'    GEE quota ({month:02d}/{year}), retry in {wait}s '
                          f'(attempt {attempt+1}/5)...')
                    time.sleep(wait)
                else:
                    print(f'    GEE error chunk {ch_num}: {e}')
                    n_errors += 1
                    break
        for feat_r in stats:
            props = dict(feat_r['properties'])
            idx   = int(float(props.pop('idx', 0)))
            g_idx = ch_num * CHUNK_SIZE + idx
            if 0 <= g_idx < len(usable_list):
                row = {c: props.get(c) for c in feature_cols}
                row['area_m2']   = usable_list[g_idx]['computed_area_m2']
                row['month_sin'] = math.sin(2 * math.pi * month / 12)
                row['month_cos'] = math.cos(2 * math.pi * month / 12)
                row['scene_date'] = scene_date_text(props.get('scene_millis_mean'))
                feat_rows[g_idx] = row
        if ch_num < n_ch - 1:
            time.sleep(CHUNK_DELAY)
    return feat_rows, n_errors

# ── Load progress ─────────────────────────────────────────────────────────
if PROGRESS_FILE.exists():
    with open(PROGRESS_FILE) as f:
        progress = json.load(f)
    done_villages = set(progress.get('done', []))
    all_results   = progress.get('results', {})
    print(f"\nResuming: {len(done_villages)} villages already done")
else:
    done_villages = set()
    all_results   = {}
    print("\nStarting fresh")

village_list = sorted(villages.keys())
remaining    = [v for v in village_list if v not in done_villages]
print(f"Villages remaining: {len(remaining)} / {len(village_list)}")
print()

# ── Main village loop (exact same logic as working notebook) ──────────────
sc_idx_lbl = labels_sc.index('Sugarcane')

for village_idx, village_name in enumerate(remaining):
    polygons = villages[village_name]
    t_start  = time.time()
    filtered = {'subpixel': 0, 'tree': 0}
    usable   = []

    for poly in polygons:
        area = poly_area_m2(poly['pts'])
        if area < MIN_AREA_M2:
            filtered['subpixel'] += 1
            continue
        if is_tree(poly['pts']):
            filtered['tree'] += 1
            continue
        poly['computed_area_m2'] = area
        usable.append(poly)

    if not usable:
        all_results[village_name] = {
            'village': village_name,
            'tehsil': polygons[0]['tehsil'] if polygons else 'KAWARDHA',
            'total_polygons': len(polygons),
            'filtered_subpixel': filtered['subpixel'],
            'filtered_tree': filtered['tree'],
            'processed': 0, 'extraction_errors': 0,
            'crop_counts': {}, 'total_area_ha': 0.0,
            'predictions': [],
            'elapsed_seconds': round(time.time() - t_start, 1),
        }
        done_villages.add(village_name)
        with open(PROGRESS_FILE, 'w') as f:
            json.dump({'done': list(done_villages), 'results': all_results}, f)
        continue

    # Two-pass GEE extraction
    features_sep, errs_sep = gee_extract(usable, RICE_YEAR, RICE_MONTH)
    features_nov, errs_nov = gee_extract(usable, SC_YEAR,   SC_MONTH)

    valid_indices = [i for i in range(len(usable))
                     if features_sep[i] is not None or features_nov[i] is not None]

    predictions = []
    if valid_indices:
        empty = {c: None for c in feature_cols}
        sep_rows = [features_sep[i] if features_sep[i] is not None else empty
                    for i in valid_indices]
        nov_rows = [features_nov[i] if features_nov[i] is not None else empty
                    for i in valid_indices]

        med_full = pd.Series(imputer_full.statistics_, index=feat_full)
        med_sc   = pd.Series(imputer_sc.statistics_,   index=feat_sc)

        X_full = pd.DataFrame(sep_rows)[feat_full].fillna(med_full)
        X_full = X_full.infer_objects(copy=False).astype(float)
        X_sc   = pd.DataFrame(nov_rows)[feat_sc].fillna(med_sc)
        X_sc   = X_sc.infer_objects(copy=False).astype(float)

        sc_codes   = model_sc.predict(X_sc)
        sc_proba   = model_sc.predict_proba(X_sc)
        full_codes = model_full.predict(X_full)
        full_proba = model_full.predict_proba(X_full)

        for j, i in enumerate(valid_indices):
            poly    = usable[i]
            area_m2 = float(poly['computed_area_m2'])
            area_ha = area_m2 / 10000

            sc_prob   = float(sc_proba[j][sc_idx_lbl])
            sc_sorted = sorted(sc_proba[j], reverse=True)
            sc_margin = float(sc_sorted[0] - sc_sorted[1])

            if (int(sc_codes[j]) == sc_idx_lbl
                    and sc_prob   >= CONFIDENCE_THRESHOLD
                    and sc_margin >= MARGIN_THRESHOLD
                    and features_nov[i] is not None):
                final_label    = 'Sugarcane'
                predicted_crop = 'Sugarcane'
                confidence     = round(sc_prob, 4)
                margin_out     = round(sc_margin, 4)
                source_model   = 'sugarcane_xgb'
                obs_date_used  = (features_nov[i] or {}).get('scene_date') or f'{SC_YEAR}-{SC_MONTH:02d}-15'
            else:
                full_code      = int(full_codes[j])
                top_prob       = float(full_proba[j][full_code])
                f_sorted       = sorted(full_proba[j], reverse=True)
                margin_f       = float(f_sorted[0] - f_sorted[1])
                predicted_crop = labels_full[full_code]
                if (predicted_crop in TARGET_CROPS
                        and top_prob  >= CONFIDENCE_THRESHOLD
                        and margin_f  >= MARGIN_THRESHOLD
                        and features_sep[i] is not None):
                    final_label = predicted_crop
                else:
                    final_label = 'Uncertain'
                confidence    = round(top_prob, 4)
                margin_out    = round(margin_f, 4)
                source_model  = 'full_7class'
                obs_date_used = (features_sep[i] or {}).get('scene_date') or f'{RICE_YEAR}-{RICE_MONTH:02d}-15'

            predictions.append({
                'field_id'      : poly['field_id'],
                'village'       : village_name,
                'source_model'  : source_model,
                'tehsil'        : poly['tehsil'],
                'area_ha'       : round(area_ha, 4),
                'area_m2'       : round(area_m2, 1),
                'predicted_crop': predicted_crop,
                'final_label'   : final_label,
                'confidence'    : confidence,
                'margin'        : margin_out,
                'centroid_lon'  : round(float(sum(p[0] for p in poly['pts'])/len(poly['pts'])), 6),
                'centroid_lat'  : round(float(sum(p[1] for p in poly['pts'])/len(poly['pts'])), 6),
                'season'        : SEASON,
                'obs_date'      : obs_date_used,
            })

    elapsed     = round(time.time() - t_start, 1)
    crop_counts = {}
    for p in predictions:
        lbl = p['final_label']
        crop_counts[lbl] = crop_counts.get(lbl, 0) + 1

    all_results[village_name] = {
        'village'          : village_name,
        'tehsil'           : polygons[0]['tehsil'] if polygons else 'KAWARDHA',
        'total_polygons'   : len(polygons),
        'filtered_subpixel': filtered['subpixel'],
        'filtered_tree'    : filtered['tree'],
        'processed'        : len(valid_indices),
        'extraction_errors': errs_sep + errs_nov,
        'crop_counts'      : crop_counts,
        'total_area_ha'    : round(float(sum(p['area_ha'] for p in predictions)), 2),
        'predictions'      : predictions,
        'elapsed_seconds'  : elapsed,
    }
    done_villages.add(village_name)

    with open(PROGRESS_FILE, 'w') as f:
        json.dump({'done': list(done_villages), 'results': all_results}, f)

    done_so_far = village_idx + 1
    top = ', '.join(f'{k}:{v}' for k,v in
                    sorted(crop_counts.items(), key=lambda x: -x[1])[:3])
    print(f"[{done_so_far:3d}/{len(remaining)}] {village_name:<25s} "
          f"{len(polygons):5d} polygons → {len(valid_indices):4d} processed "
          f"({elapsed:.0f}s) | {top}")

print(f"\n✓ All {len(village_list)} villages processed")
with open(RESULTS_FILE, 'w') as f:
    json.dump(all_results, f)
print(f"Results → {RESULTS_FILE}")

# ── Dashboard (exact same as working notebook) ────────────────────────────
print("\nGenerating dashboard...")
with open(RESULTS_FILE) as f:
    all_results = json.load(f)

rows = []
all_fields = []
for vname, vdata in all_results.items():
    cc    = vdata.get('crop_counts', {})
    preds = vdata.get('predictions', [])
    total_area = sum(p['area_ha'] for p in preds)
    row = {
        'village'        : vname,
        'tehsil'         : vdata.get('tehsil',''),
        'season'         : SEASON,
        'obs_date'       : max((p.get('obs_date') for p in preds if p.get('obs_date')), default=f'{RICE_YEAR}-{RICE_MONTH:02d}-15'),
        'total_plots'    : vdata.get('total_polygons', 0),
        'processed_plots': vdata.get('processed', 0),
        'filtered_tree'  : vdata.get('filtered_tree', 0),
        'filtered_small' : vdata.get('filtered_subpixel', 0),
        'total_area_ha'  : round(total_area, 2),
    }
    for crop in ['Rice','Sugarcane','Wheat','Cotton','Uncertain']:
        row[f'{crop}_count'] = cc.get(crop, 0)
        row[f'{crop}_ha']    = round(sum(p['area_ha'] for p in preds
                                         if p['final_label']==crop), 2)
    rows.append(row)
    for p in preds:
        if p['final_label'] in TARGET_CROPS:
            all_fields.append(p)

rows_json   = json.dumps(rows)
fields_json = json.dumps(all_fields[:50000])

total_rice       = sum(r.get('Rice_count',0) for r in rows)
total_sugarcane  = sum(r.get('Sugarcane_count',0) for r in rows)
total_wheat      = sum(r.get('Wheat_count',0) for r in rows)
total_cotton     = sum(r.get('Cotton_count',0) for r in rows)
total_rice_ha    = sum(r.get('Rice_ha',0) for r in rows)
total_sc_ha      = sum(r.get('Sugarcane_ha',0) for r in rows)
total_wheat_ha   = sum(r.get('Wheat_ha',0) for r in rows)
total_cotton_ha  = sum(r.get('Cotton_ha',0) for r in rows)
total_processed  = sum(r.get('processed_plots',0) for r in rows)
OBS_YEAR, OBS_MONTH = RICE_YEAR, RICE_MONTH

html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Kabirdham Crop Dashboard — {SEASON}</title>
<style>
:root{{
  --rice:#2e7d32;--sugarcane:#f57f17;--wheat:#ffd54f;--cotton:#e3f2fd;
  --cotton-txt:#1565c0;--uncertain:#9e9e9e;
  --bg:#f5f5f5;--card:#fff;--border:#e0e0e0;
}}
*{{box-sizing:border-box;margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}}
body{{background:var(--bg);color:#212121}}
.header{{background:#1a237e;color:#fff;padding:20px 32px}}
.header h1{{font-size:22px;font-weight:700}}
.header p{{font-size:13px;opacity:.8;margin-top:4px}}
.kpis{{display:flex;gap:16px;padding:20px 32px;flex-wrap:wrap}}
.kpi{{background:var(--card);border-radius:12px;padding:16px 20px;
       min-width:160px;flex:1;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
.kpi .val{{font-size:28px;font-weight:700;line-height:1}}
.kpi .lbl{{font-size:12px;color:#757575;margin-top:4px}}
.kpi.rice .val{{color:var(--rice)}}
.kpi.sugarcane .val{{color:var(--sugarcane)}}
.kpi.wheat .val{{color:#f9a825}}
.kpi.cotton .val{{color:var(--cotton-txt)}}
.controls{{padding:0 32px 12px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}}
.controls input{{border:1px solid var(--border);border-radius:8px;padding:8px 14px;font-size:14px;width:260px;outline:none}}
.controls select{{border:1px solid var(--border);border-radius:8px;padding:8px 14px;font-size:14px;outline:none}}
.tab-bar{{padding:0 32px;display:flex;gap:0;border-bottom:2px solid var(--border);margin-bottom:16px}}
.tab{{padding:10px 20px;cursor:pointer;font-size:14px;font-weight:600;color:#757575;border-bottom:3px solid transparent;margin-bottom:-2px}}
.tab.active{{color:#1a237e;border-bottom-color:#1a237e}}
.table-wrap{{padding:0 32px 32px;overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:13px;background:var(--card);border-radius:12px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
th{{background:#1a237e;color:#fff;padding:10px 12px;text-align:left;white-space:nowrap;font-weight:600;font-size:12px}}
td{{padding:9px 12px;border-bottom:1px solid var(--border);white-space:nowrap}}
tr:last-child td{{border-bottom:none}}
tr:hover td{{background:#f5f5f5}}
.badge{{display:inline-block;padding:2px 8px;border-radius:20px;font-size:11px;font-weight:700}}
.badge.Rice{{background:#e8f5e9;color:var(--rice)}}
.badge.Sugarcane{{background:#fff8e1;color:var(--sugarcane)}}
.badge.Wheat{{background:#fffde7;color:#f57f17}}
.badge.Cotton{{background:#e3f2fd;color:var(--cotton-txt)}}
.badge.Uncertain{{background:#f5f5f5;color:var(--uncertain)}}
#detail-section{{display:none}}
.export-btn{{margin-left:auto;background:#1a237e;color:#fff;border:none;padding:8px 18px;border-radius:8px;font-size:13px;font-weight:600;cursor:pointer}}
</style>
</head>
<body>
<div class="header">
  <h1>🌾 Kabirdham District — Crop Classification Dashboard</h1>
  <p>Season: {SEASON} &nbsp;|&nbsp; Rice/Wheat/Cotton: latest scene around {RICE_YEAR}-{RICE_MONTH:02d}-15
     &nbsp;|&nbsp; Sugarcane: latest scene around {SC_YEAR}-{SC_MONTH:02d}-15
     &nbsp;|&nbsp; Confidence: 75% &nbsp;|&nbsp; Villages: {len(rows)}</p>
</div>
<div class="kpis">
  <div class="kpi"><div class="val">{len(rows)}</div><div class="lbl">Villages Analyzed</div></div>
  <div class="kpi"><div class="val">{total_processed:,}</div><div class="lbl">Fields Processed</div></div>
  <div class="kpi rice"><div class="val">{total_rice:,}</div><div class="lbl">Rice Fields ({total_rice_ha:,.1f} ha)</div></div>
  <div class="kpi sugarcane"><div class="val">{total_sugarcane:,}</div><div class="lbl">Sugarcane Fields ({total_sc_ha:,.1f} ha)</div></div>
  <div class="kpi wheat"><div class="val">{total_wheat:,}</div><div class="lbl">Wheat Fields ({total_wheat_ha:,.1f} ha)</div></div>
  <div class="kpi cotton"><div class="val">{total_cotton:,}</div><div class="lbl">Cotton Fields ({total_cotton_ha:,.1f} ha)</div></div>
</div>
<div class="tab-bar">
  <div class="tab active" onclick="showTab('village')">Village Summary</div>
  <div class="tab" onclick="showTab('field')">Field-level Detail</div>
</div>
<div class="controls">
  <input type="text" id="search" placeholder="Search village..." oninput="filterTable()">
  <select id="cropFilter" onchange="filterTable()">
    <option value="">All Crops</option>
    <option value="Rice">Rice</option>
    <option value="Sugarcane">Sugarcane</option>
    <option value="Wheat">Wheat</option>
    <option value="Cotton">Cotton</option>
  </select>
  <button class="export-btn" onclick="exportCSV()">⬇ Export CSV</button>
</div>
<div id="village-section" class="table-wrap">
<table id="village-table">
<thead><tr>
  <th>#</th><th>Village</th><th>Tehsil</th><th>Season</th><th>Obs. Date</th>
  <th>Total Plots</th><th>Processed</th><th>Trees Removed</th><th>Small Removed</th>
  <th>Total Area (ha)</th>
  <th>Rice Plots</th><th>Rice (ha)</th>
  <th>Sugarcane Plots</th><th>Sugarcane (ha)</th>
  <th>Wheat Plots</th><th>Wheat (ha)</th>
  <th>Cotton Plots</th><th>Cotton (ha)</th>
  <th>Uncertain</th>
</tr></thead>
<tbody id="village-body"></tbody>
</table>
</div>
<div id="detail-section" class="table-wrap">
<table id="field-table">
<thead><tr>
  <th>#</th><th>Village</th><th>Field ID</th><th>Predicted Crop</th>
  <th>Confidence</th><th>Margin</th><th>Area (ha)</th>
  <th>Centroid Lon</th><th>Centroid Lat</th><th>Season</th><th>Obs Date</th>
</tr></thead>
<tbody id="field-body"></tbody>
</table>
</div>
<script>
const ROWS   = {rows_json};
const FIELDS = {fields_json};
let currentTab = 'village';
function showTab(tab) {{
  currentTab = tab;
  document.querySelectorAll('.tab').forEach((t,i) => {{
    t.classList.toggle('active', (tab==='village'&&i===0)||(tab==='field'&&i===1));
  }});
  document.getElementById('village-section').style.display = tab==='village'?'block':'none';
  document.getElementById('detail-section').style.display  = tab==='field' ?'block':'none';
  if(tab==='field') renderFields();
}}
function filterTable() {{
  const q = document.getElementById('search').value.toLowerCase();
  const crop = document.getElementById('cropFilter').value;
  if(currentTab==='village') renderVillages(q, crop);
  else renderFields(q, crop);
}}
function renderVillages(q='', crop='') {{
  const tbody = document.getElementById('village-body');
  let filtered = ROWS.filter(r =>
    (!q || r.village.toLowerCase().includes(q)) &&
    (!crop || r[crop+'_count'] > 0)
  );
  tbody.innerHTML = filtered.map((r,i) => `
    <tr>
      <td>${{i+1}}</td><td><b>${{r.village}}</b></td><td>${{r.tehsil}}</td>
      <td>${{r.season}}</td><td>${{r.obs_date}}</td>
      <td>${{r.total_plots.toLocaleString()}}</td>
      <td>${{r.processed_plots.toLocaleString()}}</td>
      <td>${{r.filtered_tree.toLocaleString()}}</td>
      <td>${{r.filtered_small.toLocaleString()}}</td>
      <td>${{r.total_area_ha.toLocaleString()}}</td>
      <td style="color:#2e7d32;font-weight:700">${{r.Rice_count||0}}</td>
      <td style="color:#2e7d32">${{(r.Rice_ha||0).toFixed(1)}}</td>
      <td style="color:#f57f17;font-weight:700">${{r.Sugarcane_count||0}}</td>
      <td style="color:#f57f17">${{(r.Sugarcane_ha||0).toFixed(1)}}</td>
      <td style="color:#f9a825;font-weight:700">${{r.Wheat_count||0}}</td>
      <td style="color:#f9a825">${{(r.Wheat_ha||0).toFixed(1)}}</td>
      <td style="color:#1565c0;font-weight:700">${{r.Cotton_count||0}}</td>
      <td style="color:#1565c0">${{(r.Cotton_ha||0).toFixed(1)}}</td>
      <td style="color:#9e9e9e">${{r.Uncertain_count||0}}</td>
    </tr>`).join('');
}}
function renderFields(q='', crop='') {{
  const tbody = document.getElementById('field-body');
  let filtered = FIELDS.filter(f =>
    (!q || f.village.toLowerCase().includes(q)) &&
    (!crop || f.final_label===crop)
  ).slice(0,5000);
  tbody.innerHTML = filtered.map((f,i) => `
    <tr>
      <td>${{i+1}}</td><td>${{f.village}}</td><td>${{f.field_id}}</td>
      <td><span class="badge ${{f.final_label}}">${{f.final_label}}</span></td>
      <td>${{(f.confidence*100).toFixed(1)}}%</td>
      <td>${{(f.margin*100).toFixed(1)}}%</td>
      <td>${{f.area_ha.toFixed(3)}}</td>
      <td>${{f.centroid_lon}}</td><td>${{f.centroid_lat}}</td>
      <td>${{f.season}}</td><td>${{f.obs_date}}</td>
    </tr>`).join('');
}}
function exportCSV() {{
  const data = currentTab==='village' ? ROWS : FIELDS;
  const keys = Object.keys(data[0]);
  const csv  = [keys.join(','),...data.map(r => keys.map(k => JSON.stringify(r[k]??'')).join(','))].join('\\n');
  const a = document.createElement('a');
  a.href = 'data:text/csv;charset=utf-8,' + encodeURIComponent(csv);
  a.download = 'kabirdham_' + currentTab + '.csv';
  a.click();
}}
renderVillages();
</script>
</body></html>"""

dashboard_path = SCRIPT_DIR / f'kabirdham_dashboard_{args.year}.html'
with open(dashboard_path, 'w', encoding='utf-8') as f:
    f.write(html)
print(f"Dashboard → {dashboard_path}")

# ── CSVs ──────────────────────────────────────────────────────────────────
pd.DataFrame(rows).to_csv(SCRIPT_DIR / f'kabirdham_summary_{args.year}.csv', index=False)
pd.DataFrame(all_fields).to_csv(SCRIPT_DIR / f'kabirdham_fields_{args.year}.csv', index=False)

# ── KMLs (exact same as working notebook) ─────────────────────────────────
print("\nGenerating KML files...")
CROP_COLORS = {
    'Rice'     : {'line': 'ff2e7d32', 'fill': 'a02e7d32'},
    'Sugarcane': {'line': 'fff57f17', 'fill': 'a0f57f17'},
    'Wheat'    : {'line': 'ffffd54f', 'fill': 'a0ffd54f'},
    'Cotton'   : {'line': 'ff1565c0', 'fill': 'a01565c0'},
    'Uncertain': {'line': 'ff9e9e9e', 'fill': 'a09e9e9e'},
}

def kml_styles():
    out = []
    for crop, colors in CROP_COLORS.items():
        slug = crop.replace(' ','_').replace('/','_')
        out.append(f'  <Style id="style_{slug}"><LineStyle><color>{colors["line"]}</color>'
                   f'<width>2</width></LineStyle><PolyStyle><color>{colors["fill"]}</color>'
                   f'</PolyStyle></Style>')
    return '\n'.join(out)

def make_placemark(field, pts):
    crop     = field['final_label']
    slug     = crop.replace(' ','_').replace('/','_')
    coords   = ' '.join(f"{p[0]},{p[1]},0" for p in pts)
    conf_pct = round(field['confidence'] * 100, 1)
    return (f'  <Placemark><name>{crop} — {field["village"]} #{field["field_id"]}</name>'
            f'<styleUrl>#style_{slug}</styleUrl><ExtendedData>'
            f'<Data name="village"><value>{field["village"]}</value></Data>'
            f'<Data name="predicted_crop"><value>{crop}</value></Data>'
            f'<Data name="confidence"><value>{conf_pct}%</value></Data>'
            f'<Data name="margin"><value>{round(field["margin"]*100,1)}%</value></Data>'
            f'<Data name="area_ha"><value>{field["area_ha"]}</value></Data>'
            f'<Data name="season"><value>{field["season"]}</value></Data>'
            f'<Data name="obs_date"><value>{field["obs_date"]}</value></Data>'
            f'</ExtendedData><Polygon><outerBoundaryIs><LinearRing>'
            f'<coordinates>{coords}</coordinates>'
            f'</LinearRing></outerBoundaryIs></Polygon></Placemark>')

def wrap_kml(name, body):
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
            f'<name>{name}</name>{kml_styles()}{body}</Document></kml>')

# Build polygon lookup
pts_lookup = {}
for vname, polys in villages.items():
    for poly in polys:
        pts_lookup[(vname, str(poly['field_id']))] = poly['pts']

by_village_dir = SCRIPT_DIR / 'kmls_by_village'
by_crop_dir    = SCRIPT_DIR / 'kmls_by_crop'
by_village_dir.mkdir(exist_ok=True)
by_crop_dir.mkdir(exist_ok=True)

village_kml_paths = []
crop_all = {'Rice':[], 'Sugarcane':[], 'Wheat':[], 'Cotton':[]}

for vname, vdata in all_results.items():
    preds = vdata.get('predictions', [])
    if not preds: continue
    groups = defaultdict(list)
    for field in preds:
        pts = pts_lookup.get((vname, str(field['field_id'])), [])
        if pts:
            groups[field['final_label']].append(make_placemark(field, pts))
            if field['final_label'] in crop_all:
                crop_all[field['final_label']].append((vname, field, pts))

    folders = []
    for crop in ['Rice','Sugarcane','Wheat','Cotton','Uncertain']:
        if groups[crop]:
            folders.append(f'<Folder><name>{crop} ({len(groups[crop])} fields)</name>'
                           + ''.join(groups[crop]) + '</Folder>')
    safe  = vname.replace(' ','_').replace('/','_')
    kpath = by_village_dir / f'{safe}.kml'
    with open(kpath, 'w', encoding='utf-8') as f:
        f.write(wrap_kml(f'{vname} — Crop Predictions ({SEASON})', ''.join(folders)))
    village_kml_paths.append(kpath)

crop_kml_paths = []
for crop, entries in crop_all.items():
    if not entries: continue
    by_v = defaultdict(list)
    for vname, field, pts in entries:
        by_v[vname].append(make_placemark(field, pts))
    folders = [f'<Folder><name>{vn} ({len(pms)} fields)</name>{"".join(pms)}</Folder>'
               for vn, pms in sorted(by_v.items())]
    kpath = by_crop_dir / f'{crop}.kml'
    with open(kpath, 'w', encoding='utf-8') as f:
        f.write(wrap_kml(f'{crop} — All Villages Kabirdham ({SEASON})', ''.join(folders)))
    crop_kml_paths.append(kpath)
    print(f"  {crop}.kml — {len(entries):,} fields")

zip_path = SCRIPT_DIR / f'kabirdham_prediction_kmls_{args.year}.zip'
with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
    for p in village_kml_paths:
        zf.write(p, f'by_village/{p.name}')
    for p in crop_kml_paths:
        zf.write(p, f'by_crop/{p.name}')

print(f"KMLs zipped → {zip_path} ({zip_path.stat().st_size/1024/1024:.1f} MB)")

print(f"\n{'='*60}")
print(f"  DONE — {SEASON}")
print(f"  Dashboard : kabirdham_dashboard_{args.year}.html")
print(f"  KMLs      : kabirdham_prediction_kmls_{args.year}.zip")
print(f"  Summary   : kabirdham_summary_{args.year}.csv")
print(f"  Fields    : kabirdham_fields_{args.year}.csv")
print(f"{'='*60}")
