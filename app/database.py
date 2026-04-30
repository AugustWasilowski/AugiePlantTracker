"""SQLAlchemy engine + session setup."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


engine = create_engine(
    f"sqlite:///{settings.db_path}",
    echo=False,
    future=True,
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def _migrate_sqlite() -> None:
    """Tiny in-place migrations for SQLite. Idempotent — safe to run on every boot.

    SQLAlchemy's ``create_all`` creates new *tables* but never alters existing
    ones, so columns added in later versions need a hand. We don't bring in
    Alembic for a single-file pet project; an inspect + ``ALTER TABLE`` does
    the job.
    """
    insp = inspect(engine)
    if "photos" not in insp.get_table_names():
        return  # fresh DB; create_all will build it correctly
    photo_cols = {c["name"] for c in insp.get_columns("photos")}
    with engine.begin() as conn:
        if "is_milestone" not in photo_cols:
            log.info("Adding photos.is_milestone column")
            conn.exec_driver_sql(
                "ALTER TABLE photos ADD COLUMN is_milestone BOOLEAN NOT NULL DEFAULT 0"
            )
        if "immich_asset_id" not in photo_cols:
            log.info("Adding photos.immich_asset_id column + index")
            conn.exec_driver_sql(
                "ALTER TABLE photos ADD COLUMN immich_asset_id VARCHAR(64)"
            )
            conn.exec_driver_sql(
                "CREATE INDEX IF NOT EXISTS ix_photos_immich_asset_id "
                "ON photos (immich_asset_id)"
            )


def init_db() -> None:
    """Create tables. Safe to call repeatedly."""
    # Import models so they register with Base.metadata.
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _migrate_sqlite()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
