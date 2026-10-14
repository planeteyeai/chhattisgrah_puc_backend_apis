╔══════════════════════════════════════════════════════════════════╗
║   Kabirdham Village Crop Detection — VS Code 2025 / 2026        ║
╚══════════════════════════════════════════════════════════════════╝

Extracted directly from the tested and working Colab notebook.
No Colab, no notebook, no disconnections. Runs to completion locally.

WHAT YOU NEED IN THIS FOLDER:
───────────────────────────────
  ✓ kabirdham_run.py                              (already here)
  ✓ requirements.txt                              (already here)
  ✗ crop_classifier_model_full.joblib             ← copy here
  ✗ crop_classifier_model_sugarcane_xgb.joblib    ← copy here
  ✗ Kabirdham_field_boundaries_by_village.zip     ← copy here

STEP 1 — Install (once only)
──────────────────────────────
Open VS Code Terminal (Ctrl + `) and run:

    pip install -r requirements.txt
    earthengine authenticate

STEP 2 — Run for 2025
───────────────────────
    python kabirdham_run.py --year 2025

Or press F5 and select "Kabirdham — Kharif 2025"

STEP 3 — Run for 2026
───────────────────────
    python kabirdham_run.py --year 2026

Or press F5 and select "Kabirdham — Kharif 2026"

WHAT EACH YEAR DOES:
──────────────────────
  --year 2025  →  Rice/Wheat/Cotton: Sep 2025 imagery
                  Sugarcane:         Nov 2025 imagery

  --year 2026  →  Rice/Wheat/Cotton: Sep 2026 imagery
                  Sugarcane:         Nov 2026 imagery

CUSTOM DATES (if needed):
──────────────────────────
    python kabirdham_run.py --year 2025 --rice_month 10 --sc_month 12

RESUME AFTER CRASH:
────────────────────
Just run the same command again. Progress is saved after every village
in kabirdham_progress_2025.json — already-done villages are skipped.
Each year has its own progress file so 2025 and 2026 never interfere.

OUTPUTS (appear in same folder after completion):
──────────────────────────────────────────────────
  kabirdham_dashboard_2025.html         ← open in Chrome/Edge
  kabirdham_prediction_kmls_2025.zip    ← open in Google Earth
      by_village/Agarikalan.kml         (one per village)
      by_crop/Rice.kml                  (all villages combined)
      by_crop/Sugarcane.kml
      by_crop/Wheat.kml
      by_crop/Cotton.kml
  kabirdham_summary_2025.csv            ← Excel, one row per village
  kabirdham_fields_2025.csv             ← Excel, one row per field
  kabirdham_results_2025.json           ← full raw data

ESTIMATED TIME:
────────────────
  ~8-12 hours for all 88 villages (GEE quota dependent)
  Each village takes 2-5 minutes
  Leave VS Code open — Windows won't sleep while script runs

GEE QUOTA ERRORS:
──────────────────
The script retries automatically (5 attempts, exponential backoff).
If quota errors persist, open kabirdham_run.py and change line:
    CHUNK_SIZE = 50  →  CHUNK_SIZE = 30
    CHUNK_DELAY = 6  →  CHUNK_DELAY = 10
Then run again — it resumes from where it stopped.
