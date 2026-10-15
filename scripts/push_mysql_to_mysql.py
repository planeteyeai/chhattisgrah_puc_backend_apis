"""Copy every table from the MySQL in DATABASE_URL into another MySQL.

Re-runnable: missing tables are created from SHOW CREATE TABLE, rows are copied in primary-key
order with INSERT IGNORE, and each table resumes after the highest primary key already present.
Usage (from repository root):
    python scripts/push_mysql_to_mysql.py "mysql://user:pass@host.proxy.rlwy.net:PORT/railway" [--batch 500] [--tables a,b]
"""

import argparse
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import pymysql
import pymysql.cursors

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import mysql_connect_args, settings


def connect(url: str, streaming: bool = False) -> pymysql.connections.Connection:
    u = urlparse(url.replace("mysql+pymysql://", "mysql://"))
    args = mysql_connect_args(url, settings.mysql_ssl)
    return pymysql.connect(
        host=u.hostname,
        port=u.port or 3306,
        user=unquote(u.username or ""),
        password=unquote(u.password or ""),
        database=u.path.lstrip("/"),
        charset="utf8mb4",
        autocommit=False,
        read_timeout=600,
        write_timeout=600,
        cursorclass=pymysql.cursors.SSCursor if streaming else pymysql.cursors.Cursor,
        **args,
    )


def primary_key(conn: pymysql.connections.Connection, table: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.key_column_usage "
            "WHERE table_schema = DATABASE() AND table_name = %s AND constraint_name = 'PRIMARY' "
            "ORDER BY ordinal_position",
            (table,),
        )
        return [r[0] for r in cur.fetchall()]


def count(conn: pymysql.connections.Connection, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM `{table}`")
        return int(cur.fetchone()[0])


def copy_table(src_url: str, dst: pymysql.connections.Connection, table: str, batch: int) -> None:
    started = time.time()
    meta = connect(src_url)
    try:
        with meta.cursor() as cur:
            cur.execute(f"SHOW CREATE TABLE `{table}`")
            ddl = cur.fetchone()[1]
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = %s ORDER BY ordinal_position",
                (table,),
            )
            columns = [r[0] for r in cur.fetchall()]
        pk = primary_key(meta, table)
        total = count(meta, table)
    finally:
        meta.close()

    with dst.cursor() as cur:
        cur.execute(ddl.replace("CREATE TABLE", "CREATE TABLE IF NOT EXISTS", 1))
    dst.commit()

    present = count(dst, table)
    if present >= total:
        print(f"{table}: already complete ({present}/{total})")
        return

    where, params = "", ()
    if len(pk) == 1 and present:
        with dst.cursor() as cur:
            cur.execute(f"SELECT MAX(`{pk[0]}`) FROM `{table}`")
            last = cur.fetchone()[0]
        if last is not None:
            where, params = f"WHERE `{pk[0]}` > %s", (last,)
    order = f"ORDER BY {', '.join(f'`{c}`' for c in pk)}" if pk else ""

    col_sql = ", ".join(f"`{c}`" for c in columns)
    insert = f"INSERT IGNORE INTO `{table}` ({col_sql}) VALUES ({', '.join(['%s'] * len(columns))})"
    src = connect(src_url, streaming=True)
    done = present
    try:
        with src.cursor() as cur:
            cur.execute(f"SELECT {col_sql} FROM `{table}` {where} {order}", params)
            while True:
                rows = cur.fetchmany(batch)
                if not rows:
                    break
                with dst.cursor() as out:
                    out.executemany(insert, rows)
                dst.commit()
                done += len(rows)
                rate = (done - present) / max(time.time() - started, 0.001)
                print(f"\r{table}: {done}/{total} ({rate:.0f} rows/s)", end="", flush=True)
    finally:
        src.close()
    final = count(dst, table)
    status = "OK" if final >= total else "MISMATCH"
    print(f"\r{table}: source={total} target={final} {status} ({time.time() - started:.0f}s)          ")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("target")
    parser.add_argument("--batch", type=int, default=500)
    parser.add_argument("--tables", default="")
    args = parser.parse_args()

    src_url = settings.sqlalchemy_url
    if (urlparse(args.target).hostname or "").endswith(".railway.internal"):
        raise SystemExit(
            "*.railway.internal only resolves inside Railway. Use the MySQL service's "
            "MYSQL_PUBLIC_URL (host like xxx.proxy.rlwy.net) instead."
        )
    if urlparse(args.target).hostname == urlparse(src_url.replace("+pymysql", "")).hostname and \
            urlparse(args.target).port == urlparse(src_url.replace("+pymysql", "")).port:
        raise SystemExit("target is the same server as DATABASE_URL")

    src = connect(src_url)
    with src.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE' "
            "ORDER BY data_length + index_length"
        )
        tables = [r[0] for r in cur.fetchall()]
    src.close()
    if args.tables:
        wanted = {t.strip() for t in args.tables.split(",") if t.strip()}
        tables = [t for t in tables if t in wanted]

    dst = connect(args.target)
    with dst.cursor() as cur:
        cur.execute("SET SESSION foreign_key_checks = 0")
    try:
        for table in tables:
            copy_table(src_url, dst, table, args.batch)
    finally:
        dst.close()


if __name__ == "__main__":
    main()
