"""Copy every table (schema + data) and view from one MySQL database to another.

Usage (from repository root):
    python scripts/migrate_mysql.py --inspect
    python scripts/migrate_mysql.py --target "mysql://root:PASS@HOST.proxy.rlwy.net:PORT/railway"

The source defaults to DATABASE_URL from .env. Existing tables on the target
with the same name are dropped and recreated.
"""

import argparse
import sys
import time
from pathlib import Path

import pymysql
import pymysql.cursors
from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import mysql_connect_args, normalize_mysql_url, settings

BATCH = 2000


def connect(url: str, streaming: bool = False) -> pymysql.connections.Connection:
    url = normalize_mysql_url(url)
    u = make_url(url)
    args = mysql_connect_args(url)
    return pymysql.connect(
        host=u.host,
        port=u.port or 3306,
        user=u.username,
        password=u.password or "",
        database=u.database,
        charset="utf8mb4",
        autocommit=False,
        read_timeout=600,
        write_timeout=600,
        cursorclass=pymysql.cursors.SSCursor if streaming else pymysql.cursors.Cursor,
        **args,
    )


def list_objects(conn) -> tuple[list[str], list[str]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, table_type FROM information_schema.tables "
            "WHERE table_schema = DATABASE() ORDER BY table_name"
        )
        rows = cur.fetchall()
    tables = [r[0] for r in rows if r[1] == "BASE TABLE"]
    views = [r[0] for r in rows if r[1] == "VIEW"]
    return tables, views


def count(conn, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM `{table}`")
        return int(cur.fetchone()[0])


def inspect(src) -> None:
    with src.cursor() as cur:
        cur.execute(
            "SELECT table_name, table_type, table_rows, "
            "ROUND((data_length + index_length) / 1024 / 1024, 1) "
            "FROM information_schema.tables WHERE table_schema = DATABASE() "
            "ORDER BY 4 DESC"
        )
        rows = cur.fetchall()
    total = 0.0
    for name, kind, est_rows, mb in rows:
        total += float(mb or 0)
        print(f"{name:50s} {kind:10s} ~{est_rows or 0:>12} rows {mb or 0:>10} MB")
    print(f"Total: {total:.1f} MB in {len(rows)} objects")


def copy_table(src_url: str, src, dst, table: str) -> None:
    with src.cursor() as cur:
        cur.execute(f"SHOW CREATE TABLE `{table}`")
        ddl = cur.fetchone()[1]
    with dst.cursor() as cur:
        cur.execute(f"DROP TABLE IF EXISTS `{table}`")
        cur.execute(ddl)
    dst.commit()

    stream = connect(src_url, streaming=True)
    copied = 0
    started = time.time()
    try:
        with stream.cursor() as rcur:
            rcur.execute(f"SELECT * FROM `{table}`")
            cols = ", ".join(f"`{d[0]}`" for d in rcur.description)
            placeholders = ", ".join(["%s"] * len(rcur.description))
            insert = f"INSERT INTO `{table}` ({cols}) VALUES ({placeholders})"
            while True:
                batch = rcur.fetchmany(BATCH)
                if not batch:
                    break
                with dst.cursor() as wcur:
                    wcur.executemany(insert, batch)
                dst.commit()
                copied += len(batch)
                print(f"  {table}: {copied} rows ({time.time() - started:.0f}s)", end="\r")
    finally:
        stream.close()
    print(f"  {table}: {copied} rows copied in {time.time() - started:.0f}s      ")


def copy_view(src, dst, view: str) -> None:
    with src.cursor() as cur:
        cur.execute(f"SHOW CREATE VIEW `{view}`")
        ddl = cur.fetchone()[1]
    # Drop DEFINER so the view is valid for the target user.
    start = ddl.find("DEFINER=")
    if start != -1:
        end = ddl.find(" SQL SECURITY", start)
        ddl = ddl[:start] + ddl[end + 1 :]
    with dst.cursor() as cur:
        cur.execute(f"DROP VIEW IF EXISTS `{view}`")
        cur.execute(ddl)
    dst.commit()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=settings.database_url)
    parser.add_argument("--target")
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--tables", nargs="*", help="Only copy these tables")
    args = parser.parse_args()

    src = connect(args.source)
    if args.inspect:
        inspect(src)
        return 0
    if not args.target:
        parser.error("--target is required")
    if ".railway.internal" in args.target:
        parser.error(
            "railway.internal hosts are only reachable inside Railway; "
            "use the MySQL service's public URL (MYSQL_PUBLIC_URL)"
        )

    dst = connect(args.target)
    with dst.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        cur.execute("SET UNIQUE_CHECKS = 0")

    tables, views = list_objects(src)
    if args.tables:
        tables = [t for t in tables if t in set(args.tables)]
        views = []
    print(f"Copying {len(tables)} tables and {len(views)} views")

    for table in tables:
        copy_table(args.source, src, dst, table)
    for view in views:
        copy_view(src, dst, view)
        print(f"  view {view} created")

    with dst.cursor() as cur:
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")
        cur.execute("SET UNIQUE_CHECKS = 1")

    print("\nVerifying row counts")
    mismatches = 0
    for table in tables:
        a, b = count(src, table), count(dst, table)
        flag = "OK" if a == b else "MISMATCH"
        mismatches += a != b
        print(f"  {table:50s} source={a:<10} target={b:<10} {flag}")
    print("Done" if not mismatches else f"{mismatches} tables differ")
    return 1 if mismatches else 0


if __name__ == "__main__":
    sys.exit(main())
