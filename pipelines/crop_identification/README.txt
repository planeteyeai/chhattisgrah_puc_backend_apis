Kabirdham crop identification (offline pipeline)
================================================

This folder is NOT part of the Railway / FastAPI deploy. The API reads
results from MySQL table crop_identification.

Files
-----
  kabirdham_run.py              Classify fields with Earth Engine + joblib models
  store_identification.py       Classify from field_boundaries and upsert MySQL
  store_kml_identification.py   Load KML placemarks into MySQL
  crop_classifier_model_*.joblib
                                Trained models (gitignored; keep a local copy)

Setup
-----
  From the repository root:

    pip install -r requirements.txt
    pip install -r pipelines/crop_identification/requirements.txt
    copy .env.example .env

  Put village KMLs in:

    data/all_crops_villages/*.kml

Rice / cotton / sugarcane (from field_boundaries)
-------------------------------------------------
    python pipelines/crop_identification/store_identification.py --year 2025

Load all-crops KMLs into MySQL
------------------------------
    python pipelines/crop_identification/store_kml_identification.py

Legacy Earth Engine batch (writes local KML / CSV / HTML)
--------------------------------------------------------
    python pipelines/crop_identification/kabirdham_run.py --year 2025
