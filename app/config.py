import os
import ssl

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _skip_tls(url: str) -> bool:
    """Local hosts and Railway private networking do not use TLS."""
    lowered = url.lower()
    return any(
        token in lowered
        for token in ("@127.0.0.1", "@localhost", "@[::1]", ".railway.internal")
    )


def normalize_mysql_url(url: str) -> str:
    if url.startswith("mysql://"):
        url = "mysql+pymysql://" + url[len("mysql://") :]
    if "charset=" not in url:
        url += ("&" if "?" in url else "?") + "charset=utf8mb4"
    return url


def normalize_postgres_url(url: str) -> str:
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://") and not url.startswith("postgresql+"):
        url = "postgresql+psycopg2://" + url[len("postgresql://") :]
    if not _skip_tls(url) and "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


def mysql_connect_args(url: str, ssl_flag: str = "") -> dict[str, object]:
    args: dict[str, object] = {"connect_timeout": 15}
    flag = (ssl_flag or os.environ.get("MYSQL_SSL", "")).strip().lower()
    if flag in {"0", "false", "no", "off"}:
        return args
    use_ssl = flag in {"1", "true", "yes", "on"} or "rlwy.net" in url.lower()
    if not use_ssl:
        return args
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    args["ssl"] = context
    return args


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = ""
    kawardha_database_url: str | None = None
    host: str = "0.0.0.0"
    port: int = 8000
    worker_parallelism: int = 2
    indices_cron_years: int = 10
    indices_cron_step_days: int = 16
    indices_cron_workers: int = 2
    indices_cron_autostart: bool = False
    district_daily_lookback_days: int = 16
    ee_project: str | None = None
    google_application_credentials: str | None = None
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_session_token: str | None = None
    aws_default_region: str = "us-west-2"
    aws_stac_api_key: str | None = None
    stac_api_url: str = "https://earth-search.aws.element84.com/v1"
    stac_s2_collection: str = "sentinel-2-c1-l2a"
    stac_s1_collection: str = "sentinel-1-grd"
    aws_stac_timeout: int = 45
    classwise_raster_res: float = 0.00009
    classwise_max_raster_side: int = 2048
    tile_cache_max: int = 128
    backfill_parallelism: int = 2
    backfill_request_timeout: int = 300
    backfill_memory_retries: int = 3
    public_base_url: str = ""
    mysql_ssl: str = ""

    @model_validator(mode="after")
    def require_database(self) -> "Settings":
        if not self.database_url:
            self.database_url = os.environ.get("MYSQL_URL", "")
        if not self.database_url:
            raise ValueError("Set DATABASE_URL to the MySQL connection string")
        return self

    @property
    def sqlalchemy_url(self) -> str:
        return normalize_mysql_url(self.database_url)

    @property
    def sqlalchemy_kawardha_url(self) -> str | None:
        if not self.kawardha_database_url:
            return None
        return normalize_postgres_url(self.kawardha_database_url)


settings = Settings()
