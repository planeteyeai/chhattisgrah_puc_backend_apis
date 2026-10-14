from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.config import mysql_connect_args, settings

engine = create_engine(
    settings.sqlalchemy_url,
    pool_pre_ping=True,
    pool_recycle=280,
    pool_size=5,
    max_overflow=5,
    pool_timeout=30,
    connect_args=mysql_connect_args(settings.sqlalchemy_url, settings.mysql_ssl),
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

kawardha_engine: Engine | None = None
if settings.sqlalchemy_kawardha_url:
    kawardha_engine = create_engine(
        settings.sqlalchemy_kawardha_url,
        pool_pre_ping=True,
        pool_recycle=280,
        pool_size=5,
        max_overflow=5,
        pool_timeout=30,
        connect_args={"connect_timeout": 15},
    )

KawardhaSessionLocal = (
    sessionmaker(bind=kawardha_engine, autoflush=False, autocommit=False)
    if kawardha_engine is not None
    else None
)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

