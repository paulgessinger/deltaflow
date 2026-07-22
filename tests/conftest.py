from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from deltaflow import api
from deltaflow.config import Settings
from deltaflow.models import Base

REPO = "acts-project/acts"


@pytest.fixture
def db_sessionmaker(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", future=True)

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _record):
        cur = conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
def settings():
    return Settings(
        database_url="sqlite://",
        audience="https://deltaflow.test",
        allowed_repos=[REPO],
        default_branch="main",
        github_app_id="",
        github_private_key="",
    )


@pytest.fixture
def client(db_sessionmaker, settings):
    def _session():
        with db_sessionmaker() as s:
            yield s

    api.app.dependency_overrides[api.session] = _session
    api.app.dependency_overrides[api.config] = lambda: settings
    api.app.dependency_overrides[api.github] = lambda: None
    api._verifier = None
    with TestClient(api.app) as c:
        c.sessionmaker = db_sessionmaker
        yield c
    api.app.dependency_overrides.clear()
