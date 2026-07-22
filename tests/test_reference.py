"""Reference bracketing: the two halves, and the two signals derived from them."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from deltaflow.config import Settings
from deltaflow.models import (
    Base,
    Context,
    Direction,
    Measurement,
    Position,
    Role,
    Trust,
    series_key,
)
from deltaflow.queries import Bracket, baseline_points, brackets, head_series
from deltaflow.reporting import build

REPO = "acts-project/acts"
PAYLOAD_LABELS = {"benchmark": "seeding"}
REF_LABELS = {"benchmark": "reference-fixed"}


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'ref.db'}", future=True)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(
        "deltaflow.reporting.settings",
        lambda: Settings(
            database_url="sqlite://", allowed_repos=[REPO], baseline_window=50
        ),
    )
    with sessionmaker(engine, expire_on_commit=False)() as s:
        yield s


def add(
    db,
    head_sha,
    values,
    role=Role.PAYLOAD,
    position="",
    labels=None,
    context=Context.MAINLINE,
    job="bench",
    run=None,
    group="",
):
    labels = (
        labels
        if labels is not None
        else (REF_LABELS if role is Role.REFERENCE else PAYLOAD_LABELS)
    )
    key = series_key(REPO, "runtime", labels, role)
    for i, v in enumerate(values):
        db.add(
            Measurement(
                repo=REPO,
                series=key,
                metric="runtime",
                unit="s",
                direction=Direction.LOWER_BETTER.value,
                labels=labels,
                value=v,
                rep=i,
                role=role.value,
                position=position,
                group=group or job,
                context=context.value,
                trust=Trust.TOKEN.value,
                head_sha=head_sha,
                run_id=run or head_sha,
                run_attempt=1,
                job=job,
                created_at=dt.datetime.now(dt.UTC),
            )
        )
    db.commit()
    return key


def bracketed(db, sha, before, after, payload, job="bench", **kw):
    add(
        db,
        sha,
        [before],
        role=Role.REFERENCE,
        position=Position.BEFORE.value,
        job=job,
        **kw,
    )
    add(db, sha, payload, job=job, **kw)
    add(
        db,
        sha,
        [after],
        role=Role.REFERENCE,
        position=Position.AFTER.value,
        job=job,
        **kw,
    )


# --- the bracket itself ------------------------------------------------------


def test_instability_measures_movement_across_the_payload():
    assert Bracket(before=10.0, after=11.0).instability == pytest.approx(9.52, abs=0.1)


def test_stable_machine_reports_no_instability():
    assert Bracket(before=10.0, after=10.0).instability == 0.0


def test_instability_is_available_without_any_history(db):
    """The property that makes bracketing useful from the very first run."""
    bracketed(db, "a" * 40, before=10.0, after=11.0, payload=[20.0])
    (line,) = build(db, REPO, "a" * 40)
    assert line.instability_pct == pytest.approx(9.52, abs=0.1)
    assert line.n_baseline == 0  # no history at all, yet a variation estimate


def test_half_a_bracket_yields_nothing_rather_than_a_guess(db):
    add(db, "a" * 40, [10.0], role=Role.REFERENCE, position=Position.BEFORE.value)
    add(db, "a" * 40, [20.0])
    (line,) = build(db, REPO, "a" * 40)
    assert line.instability_pct is None


def test_brackets_are_keyed_per_job(db):
    bracketed(db, "a" * 40, before=10.0, after=10.0, payload=[20.0], job="quiet")
    bracketed(db, "a" * 40, before=10.0, after=13.0, payload=[20.0], job="noisy")
    found = brackets(db, REPO, "a" * 40)
    assert found[("quiet", "quiet")].instability == pytest.approx(0.0)
    assert found[("noisy", "noisy")].instability > 20


# --- series identity ---------------------------------------------------------


def test_both_halves_belong_to_one_series():
    """Position is metadata, not identity: a bracket samples one workload twice."""
    before = series_key(REPO, "runtime", REF_LABELS, Role.REFERENCE)
    after = series_key(REPO, "runtime", REF_LABELS, Role.REFERENCE)
    assert before == after


def test_reference_and_payload_never_share_a_series():
    same_labels = {"benchmark": "x"}
    assert series_key(REPO, "runtime", same_labels, Role.PAYLOAD) != series_key(
        REPO, "runtime", same_labels, Role.REFERENCE
    )


def test_reference_is_not_reported_as_a_measurement(db):
    bracketed(db, "a" * 40, before=10.0, after=10.0, payload=[20.0])
    assert [s.metric for s in head_series(db, REPO, "a" * 40)] == ["runtime"]
    assert len(build(db, REPO, "a" * 40)) == 1


def test_reference_never_enters_a_payload_baseline(db):
    key = add(db, "a" * 40, [20.0])
    add(db, "a" * 40, [10.0], role=Role.REFERENCE, position=Position.BEFORE.value)
    assert baseline_points(db, key, 50) == [20.0]


# --- drift -------------------------------------------------------------------


def test_drift_reports_the_machine_moving_against_its_own_history(db):
    for i in range(10):
        bracketed(db, f"{i:040d}", before=10.0, after=10.0, payload=[20.0])
    # Same software, machine now 25% slower.
    bracketed(db, "f" * 40, before=12.5, after=12.5, payload=[25.0])

    (line,) = build(db, REPO, "f" * 40)
    assert line.drift_pct == pytest.approx(25.0, abs=0.5)
    assert line.n_drift == 10


def test_no_drift_reported_on_a_steady_machine(db):
    for i in range(10):
        bracketed(db, f"{i:040d}", before=10.0, after=10.0, payload=[20.0])
    bracketed(db, "f" * 40, before=10.0, after=10.0, payload=[20.0])

    (line,) = build(db, REPO, "f" * 40)
    assert line.drift_pct == pytest.approx(0.0, abs=0.5)


def test_drift_needs_history_and_says_so_when_absent(db):
    bracketed(db, "a" * 40, before=10.0, after=10.0, payload=[20.0])
    (line,) = build(db, REPO, "a" * 40)
    assert line.drift_pct is None
    assert line.n_drift == 0


def test_reference_history_excludes_the_commit_being_reported(db):
    for i in range(10):
        bracketed(db, f"{i:040d}", before=10.0, after=10.0, payload=[20.0])
    bracketed(db, "f" * 40, before=50.0, after=50.0, payload=[20.0])

    (line,) = build(db, REPO, "f" * 40)
    # Were the commit included in its own reference norm, the drift would be
    # pulled toward zero and a machine change would hide itself.
    assert line.drift_pct == pytest.approx(400.0, abs=1.0)
