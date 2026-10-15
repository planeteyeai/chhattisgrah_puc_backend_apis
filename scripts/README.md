# Database helper scripts

Run these from the repository root so `.env` and `app.config` resolve.

| Script | Purpose |
| --- | --- |
| `migrate_mysql.py` | Drop/recreate every table from `DATABASE_URL` into another MySQL |
| `push_mysql_to_mysql.py` | Resume-friendly copy (INSERT IGNORE by primary key) |
| `push_kawardha_mysql.py` | Copy local Kawardha PostgreSQL cadastral tables into MySQL |

Examples:

```bash
python scripts/migrate_mysql.py --inspect
python scripts/push_mysql_to_mysql.py "mysql://user:pass@host.proxy.rlwy.net:PORT/railway"
python scripts/push_kawardha_mysql.py --batch 200
```
