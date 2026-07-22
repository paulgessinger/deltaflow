"""Engine and session management.

SQLite is the v1 store. Everything goes through SQLAlchemy Core/ORM so the
PostgreSQL escape hatch stays real rather than aspirational -- no raw SQL, no
SQLite-only functions.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator
from typing import TYPE_CHECKING

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

if TYPE_CHECKING:
    from alembic.config import Config
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


def alembic_config() -> "Config":
    """Alembic configuration built in code rather than read from alembic.ini.

    The migrations ship inside the package, so the script location is resolved
    relative to this module. That keeps `deltaflow migrate` working from an
    installed wheel, where there is no repository checkout and no ini file.
    """
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option(
        "script_location", str(pathlib.Path(__file__).parent / "migrations")
    )
    cfg.set_main_option("sqlalchemy.url", settings().database_url)
    return cfg


def init_db() -> None:
    """Bring the database up to the current schema.

    Migrations, not `create_all`: once there is history worth keeping, the
    schema has to evolve without dropping it. `create_all` remains the right
    tool in tests and the simulator, which build a database from nothing and
    throw it away.
    """
    from alembic import command

    command.upgrade(alembic_config(), "head")


def pending_migrations() -> int:
    """How many revisions the database is behind the code."""
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(alembic_config())
    with engine().connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
    if current == script.get_current_head():
        return 0
    return len(list(script.walk_revisions(base=current or "base"))) - (
        0 if current is None else 1
    )


def create_all() -> None:
    """Build the schema directly, bypassing migrations. Tests and tools only."""
    Base.metadata.create_all(engine())


def session() -> Iterator[Session]:
    engine()
    assert _Session is not None
    with _Session() as s:
        yield s
