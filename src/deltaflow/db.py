"""Engine and session management.

SQLite is the v1 store. Everything goes through SQLAlchemy Core/ORM so the
PostgreSQL escape hatch stays real rather than aspirational -- no raw SQL, no
SQLite-only functions.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base

_engine: Engine | None = None
_Session: sessionmaker[Session] | None = None


def _configure_sqlite(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    # WAL is what makes concurrent readers viable alongside the single writer.
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA foreign_keys=ON")
    # Ingest bursts when several CI jobs finish together; wait rather than fail.
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()


def engine() -> Engine:
    global _engine, _Session
    if _engine is None:
        url = settings().database_url
        _engine = create_engine(url, future=True)
        if url.startswith("sqlite"):
            event.listen(_engine, "connect", _configure_sqlite)
        _Session = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def init_db() -> None:
    Base.metadata.create_all(engine())


def session() -> Iterator[Session]:
    engine()
    assert _Session is not None
    with _Session() as s:
        yield s
