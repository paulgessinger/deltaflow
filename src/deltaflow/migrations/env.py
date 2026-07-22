"""Alembic environment.

The database URL comes from deltaflow's own settings rather than alembic.ini,
so there is exactly one place that decides which database is in use and no way
for a migration to run against a different one than the application.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import engine_from_config, pool

from deltaflow.config import settings
from deltaflow.models import Base

config = context.config
target_metadata = Base.metadata

config.set_main_option("sqlalchemy.url", settings().database_url)


def run_migrations_offline() -> None:
    context.configure(
        url=settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite cannot ALTER most things in place; batch mode rebuilds the
        # table instead, which is what makes schema changes viable here at all.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
