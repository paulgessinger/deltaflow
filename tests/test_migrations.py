"""Migrations: that they run, and that they match the models."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from deltaflow.db import alembic_config
from deltaflow.models import Base


@pytest.fixture
def migrated(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    monkeypatch.setenv("DELTAFLOW_DATABASE_URL", url)
    from deltaflow.config import settings

    settings.cache_clear()
    cfg = alembic_config()
    command.upgrade(cfg, "head")
    return create_engine(url), cfg


def test_upgrade_creates_every_table(migrated):
    engine, _ = migrated
    tables = set(inspect(engine).get_table_names())
    assert set(Base.metadata.tables) <= tables


def test_schema_matches_the_models(migrated):
    """A drifted migration is worse than none: it fails at query time."""
    engine, cfg = migrated
    with engine.connect() as conn:
        context = MigrationContext.configure(
            conn, opts={"compare_type": True, "target_metadata": Base.metadata}
        )
        from alembic.autogenerate import compare_metadata

        diff = compare_metadata(context, Base.metadata)
    assert diff == [], f"models and migrations disagree: {diff}"


def test_downgrade_to_base_is_possible(migrated):
    engine, cfg = migrated
    command.downgrade(cfg, "base")
    remaining = set(inspect(engine).get_table_names()) - {"alembic_version"}
    assert remaining == set()


def test_there_is_exactly_one_head(migrated):
    """Two heads mean a merge is needed and upgrades will fail confusingly."""
    _, cfg = migrated
    assert len(ScriptDirectory.from_config(cfg).get_heads()) == 1


def test_dedup_constraint_survives_migration(migrated):
    """The constraint three bugs have already hidden behind."""
    engine, _ = migrated
    constraints = inspect(engine).get_unique_constraints("measurement")
    dedup = next(c for c in constraints if c["name"] == "uq_measurement_dedup")
    assert set(dedup["column_names"]) == {
        "run_id",
        "run_attempt",
        "job",
        "series",
        "rep",
        "head_sha",
        "position",
        "group",
    }
