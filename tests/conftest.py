from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from deltaflow import api, deps
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


@pytest.fixture(autouse=True)
def no_ambient_database(monkeypatch):
    """Keep the app's startup hook away from the configured database.

    The lifespan applies migrations, and under TestClient that would run
    against whatever DELTAFLOW_DATABASE_URL happens to be set to -- creating a
    stray file in the repository and coupling the suite to ambient config.
    Fixtures build their own schema explicitly.
    """
    monkeypatch.setattr("deltaflow.api.init_db", lambda: None)


@pytest.fixture
def client(db_sessionmaker, settings):
    def _session():
        with db_sessionmaker() as s:
            yield s

    api.app.dependency_overrides[deps.session] = _session
    api.app.dependency_overrides[deps.config] = lambda: settings
    api.app.dependency_overrides[deps.github] = lambda: None
    deps.reset()
    with TestClient(api.app) as c:
        c.sessionmaker = db_sessionmaker
        yield c
    api.app.dependency_overrides.clear()
