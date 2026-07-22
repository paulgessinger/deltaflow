from __future__ import annotations

import datetime as dt

import pytest

from deltaflow.config import Settings
from deltaflow.models import Base, Context, Direction, Measurement, Trust, series_key
from deltaflow.queries import baseline_points
from deltaflow.reporting import Progress, build, render

REPO = "acts-project/acts"
LABELS = {"benchmark": "seeding"}


@pytest.fixture
def db(db_sessionmaker, settings, monkeypatch):
    monkeypatch.setattr("deltaflow.reporting.settings", lambda: settings)
    with db_sessionmaker() as s:
        yield s


def add(db, head_sha, values, context=Context.MAINLINE, metric="runtime", run=None):
    key = series_key(REPO, metric, LABELS)
    for i, v in enumerate(values):
        db.add(
            Measurement(
                repo=REPO,
                series=key,
                metric=metric,
                unit="s",
                direction=Direction.LOWER_BETTER.value,
                labels=LABELS,
                value=v,
                rep=i,
                context=context.value,
                trust=Trust.TOKEN.value,
                head_sha=head_sha,
                run_id=run or head_sha,
                run_attempt=1,
                job="bench",
                created_at=dt.datetime.now(dt.UTC),
            )
        )
    db.commit()
    return key


def test_commit_is_excluded_from_its_own_baseline(db):
    """Otherwise a landed regression drags the centre toward itself."""
    for i in range(10):
        add(db, f"{i:040d}", [10.0])
    key = add(db, "f" * 40, [20.0])

    assert 20.0 not in baseline_points(db, key, 50, before_sha="f" * 40)
    assert 20.0 in baseline_points(db, key, 50)


def test_change_is_described_against_clean_history(db):
    for i in range(10):
        add(db, f"{i:040d}", [10.0, 10.1])
    add(db, "f" * 40, [20.0, 20.1])

    (line,) = build(db, REPO, "f" * 40)
    assert line.n_baseline == 10
    assert line.delta_pct == pytest.approx(99.0, abs=2.0)
    assert line.baseline == pytest.approx(10.05, abs=0.1)


def test_report_classifies_nothing(db):
    """No verdicts by design: the reader judges, the tool does not."""
    for i in range(10):
        add(db, f"{i:040d}", [10.0])
    add(db, "f" * 40, [50.0])

    body = render(build(db, REPO, "f" * 40), "f" * 40)
    for word in ("regress", "improve", "🔴", "🟢"):
        assert word not in body.lower().replace("regression.", "")


def test_pull_request_points_are_absent_from_the_baseline(db):
    for i in range(10):
        add(db, f"{i:040d}", [10.0])
    key = add(db, "e" * 40, [99.0], context=Context.PR)
    assert 99.0 not in baseline_points(db, key, 50)


def test_baseline_window_is_respected(db):
    for i in range(30):
        add(db, f"{i:040d}", [10.0])
    key = series_key(REPO, "runtime", LABELS)
    assert len(baseline_points(db, key, 5)) == 5


def test_render_marks_incomplete_reporting(db):
    for i in range(10):
        add(db, f"{i:040d}", [10.0])
    comparisons = build(db, REPO, f"{0:040d}")
    body = render(comparisons, f"{0:040d}", Progress(claimed=6, reported=4))
    assert "4 of 6" in body


def test_render_omits_progress_when_all_jobs_reported(db):
    comparisons = build(db, REPO, "f" * 40)
    body = render(comparisons, "f" * 40, Progress(claimed=3, reported=3))
    assert "benchmark jobs have reported" not in body


def test_render_states_that_it_does_not_block_merging(db):
    add(db, "f" * 40, [10.0])
    body = render(build(db, REPO, "f" * 40), "f" * 40)
    assert "nothing here blocks merging" in body.lower()


@pytest.fixture
def settings():
    return Settings(
        database_url="sqlite://",
        allowed_repos=[REPO],
        baseline_window=50,
        default_branch="main",
    )


@pytest.fixture
def db_sessionmaker(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine(f"sqlite:///{tmp_path / 'r.db'}", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
