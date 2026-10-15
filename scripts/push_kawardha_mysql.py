"""Copy the local Kawardha cadastral PostgreSQL tables into the hosted MySQL (DATABASE_URL).

Re-runnable: tables are created if missing and rows are upserted by primary key.
Usage (from repository root):
    python scripts/push_kawardha_mysql.py [--batch 200]
"""

import argparse
import sys
import time
from pathlib import Path

from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import mysql_connect_args, settings

TABLES: dict[str, str] = {
    "states": """
        id INT NOT NULL PRIMARY KEY,
        state_code VARCHAR(32),
        state_name VARCHAR(255)
    """,
    "districts": """
        id INT NOT NULL PRIMARY KEY,
        state_id INT,
        district_code VARCHAR(32),
        district_name VARCHAR(255),
        district_name_en VARCHAR(255),
        KEY idx_districts_state (state_id)
    """,
    "talukas": """
        id INT NOT NULL PRIMARY KEY,
        district_id INT,
        taluka_code VARCHAR(32),
        taluka_name VARCHAR(255),
        taluka_name_en VARCHAR(255),
        KEY idx_talukas_district (district_id)
    """,
    "ris": """
        id INT NOT NULL PRIMARY KEY,
        taluka_id INT,
        ri_code VARCHAR(32),
        ri_name VARCHAR(255),
        ri_name_en VARCHAR(255),
        KEY idx_ris_taluka (taluka_id)
    """,
    "villages": """
        id BIGINT NOT NULL PRIMARY KEY,
        ri_id INT,
        village_code VARCHAR(64),
        bhunaksha_village_code VARCHAR(64),
        lgd_code VARCHAR(64),
        village_name VARCHAR(255),
        village_name_en VARCHAR(255),
        giscode VARCHAR(64),
        plot_count INT,
        scrape_status VARCHAR(32),
        scraped_at DATETIME(6),
        village_name_eng VARCHAR(255),
        KEY idx_kawardha_villages_ri (ri_id),
        KEY idx_kawardha_villages_status (scrape_status)
    """,
    "village_extents": """
        village_id BIGINT NOT NULL PRIMARY KEY,
        x_min DOUBLE, y_min DOUBLE, x_max DOUBLE, y_max DOUBLE,
        min_lon DOUBLE, min_lat DOUBLE, max_lon DOUBLE, max_lat DOUBLE,
        the_geom_lonlat MEDIUMTEXT,
        the_geom_geojson MEDIUMTEXT
    """,
    "plots": """
        id BIGINT NOT NULL PRIMARY KEY,
        village_id BIGINT,
        plot_no VARCHAR(64),
        plot_id VARCHAR(128),
        survey_no VARCHAR(64),
        surnoc VARCHAR(64),
        hissa_no VARCHAR(64),
        land_code VARCHAR(64),
        ulpin VARCHAR(64),
        area_ha DECIMAL(20,6),
        ext_acre DECIMAL(20,4),
        ext_gunta DECIMAL(20,4),
        ext_fgunta DECIMAL(20,4),
        info MEDIUMTEXT,
        stored_at DATETIME(6),
        info_en MEDIUMTEXT,
        KEY idx_kawardha_plots_village (village_id)
    """,
    "plot_extents": """
        plot_id_fk BIGINT NOT NULL PRIMARY KEY,
        x_min DOUBLE, y_min DOUBLE, x_max DOUBLE, y_max DOUBLE,
        min_lon DOUBLE, min_lat DOUBLE, max_lon DOUBLE, max_lat DOUBLE,
        the_geom MEDIUMTEXT,
        the_geom_lonlat MEDIUMTEXT,
        the_geom_geojson MEDIUMTEXT,
        KEY idx_kawardha_plot_extents_bbox (min_lon, min_lat, max_lon, max_lat)
    """,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=int, default=200)
    args = parser.parse_args()

    if not settings.sqlalchemy_kawardha_url:
        raise SystemExit("KAWARDHA_DATABASE_URL is not set")
    pg = create_engine(settings.sqlalchemy_kawardha_url)
    my = create_engine(
        settings.sqlalchemy_url,
        pool_pre_ping=True,
        connect_args=mysql_connect_args(settings.sqlalchemy_url, settings.mysql_ssl),
    )

    with my.begin() as conn:
        for table, ddl in TABLES.items():
            conn.execute(text(
                f"CREATE TABLE IF NOT EXISTS `{table}` ({ddl}) "
                "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"
            ))

    for table in TABLES:
        started = time.time()
        with pg.connect() as src:
            columns = src.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = :t ORDER BY ordinal_position"
            ), {"t": table}).scalars().all()
            total = src.execute(text(f'SELECT COUNT(*) FROM "{table}"')).scalar_one()
            col_sql = ", ".join(f"`{c}`" for c in columns)
            val_sql = ", ".join(f":{c}" for c in columns)
            upd_sql = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in columns)
            insert = text(
                f"INSERT INTO `{table}` ({col_sql}) VALUES ({val_sql}) "
                f"ON DUPLICATE KEY UPDATE {upd_sql}"
            )
            select = text(f'SELECT {", ".join(chr(34) + c + chr(34) for c in columns)} FROM "{table}" ORDER BY 1')
            result = src.execution_options(stream_results=True, max_row_buffer=args.batch).execute(select)
            done = 0
            while True:
                rows = result.mappings().fetchmany(args.batch)
                if not rows:
                    break
                with my.begin() as dst:
                    dst.execute(insert, [dict(r) for r in rows])
                done += len(rows)
                print(f"\r{table}: {done}/{total}", end="", flush=True)
        with my.connect() as dst:
            remote = dst.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar_one()
        status = "OK" if remote >= total else "MISMATCH"
        print(f"\r{table}: local={total} hosted={remote} {status} ({time.time() - started:.1f}s)")


if __name__ == "__main__":
    main()
