"""The Grafana JSON datasource contract."""

from __future__ import annotations

import datetime as dt

import pytest

from deltaflow.models import (
    Context,
    Direction,
    Measurement,
    Position,
    Role,
    Trust,
    series_key,
)

REPO = "acts-project/acts"
LABELS = {"benchmark": "seeding", "runner": "ubuntu-latest"}
TARGET = f"{REPO}/runtime {{benchmark=seeding, runner=ubuntu-latest}}"


def add(db, sha, values, role=Role.PAYLOAD, position="", labels=None, run=None):
    labels = LABELS if labels is None else labels
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
                group="bench",
                context=Context.MAINLINE.value,
                trust=Trust.TOKEN.value,
                head_sha=sha,
                run_id=run or sha,
                run_attempt=1,
                job="bench",
                created_at=dt.datetime.now(dt.UTC),
            )
        )
    db.commit()


@pytest.fixture
def populated(client):
    with client.sessionmaker() as db:
        for i in range(10):
            sha = f"{i:040d}"
            add(db, sha, [10.0 + i * 0.01, 10.02 + i * 0.01])
    return client


@pytest.fixture
def bracketed(client):
    with client.sessionmaker() as db:
        for i in range(10):
            sha = f"{i:040d}"
            add(db, sha, [5.0], role=Role.REFERENCE, position=Position.BEFORE.value)
            add(db, sha, [10.0, 10.1])
            add(db, sha, [5.2], role=Role.REFERENCE, position=Position.AFTER.value)
    return client


def test_root_answers_the_datasource_health_check(client):
    assert client.get("/grafana/").status_code == 200


def test_search_lists_series_with_their_labels(populated):
    names = populated.post("/grafana/search", json={"target": ""}).json()
    assert TARGET in names


def test_search_filters_by_substring(populated):
    assert populated.post("/grafana/search", json={"target": "seeding"}).json()
    assert populated.post("/grafana/search", json={"target": "nonsense"}).json() == []


def test_references_are_not_offered_as_series(bracketed):
    names = bracketed.post("/grafana/search", json={"target": ""}).json()
    assert all("reference" not in n for n in names)


def test_query_returns_datapoints_newest_last(populated):
    body = populated.post(
        "/grafana/query", json={"targets": [{"target": TARGET}]}
    ).json()
    main = next(s for s in body if s["target"] == TARGET)
    assert len(main["datapoints"]) == 10
    # Grafana wants [value, timestamp].
    values = [v for v, _ in main["datapoints"]]
    timestamps = [t for _, t in main["datapoints"]]
    assert timestamps == sorted(timestamps)
    assert values[0] < values[-1]


def test_unknown_target_is_ignored_rather_than_erroring(populated):
    body = populated.post(
        "/grafana/query", json={"targets": [{"target": "no/such"}]}
    ).json()
    assert body == []


def test_a_bracketed_series_gains_a_band(bracketed):
    body = bracketed.post(
        "/grafana/query", json={"targets": [{"target": TARGET}]}
    ).json()
    names = {s["target"] for s in body}
    assert names == {TARGET, TARGET + " (upper)", TARGET + " (lower)"}


def test_the_band_brackets_the_value(bracketed):
    body = bracketed.post(
        "/grafana/query", json={"targets": [{"target": TARGET}]}
    ).json()
    by_name = {s["target"]: s["datapoints"] for s in body}
    for (v, _), (up, _), (lo, _) in zip(
        by_name[TARGET],
        by_name[TARGET + " (upper)"],
        by_name[TARGET + " (lower)"],
        strict=True,
    ):
        assert lo < v < up


def test_repetition_spread_alone_still_draws_a_band(populated):
    """A bracket is not required: spread across repetitions is uncertainty too."""
    body = populated.post(
        "/grafana/query", json={"targets": [{"target": TARGET}]}
    ).json()
    assert TARGET + " (upper)" in {s["target"] for s in body}


def test_no_band_when_there_is_nothing_to_measure(client):
    """One repetition, no bracket: a bare line is the honest rendering."""
    with client.sessionmaker() as db:
        for i in range(5):
            add(db, f"{i:040d}", [10.0])
    body = client.post("/grafana/query", json={"targets": [{"target": TARGET}]}).json()
    assert {s["target"] for s in body} == {TARGET}


def test_a_band_series_can_be_requested_directly(bracketed):
    body = bracketed.post(
        "/grafana/query", json={"targets": [{"target": TARGET + " (upper)"}]}
    ).json()
    assert [s["target"] for s in body] == [TARGET + " (upper)"]


def test_time_range_filters_points(populated):
    future = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).isoformat()
    later = (dt.datetime.now(dt.UTC) + dt.timedelta(days=2)).isoformat()
    body = populated.post(
        "/grafana/query",
        json={"targets": [{"target": TARGET}], "range": {"from": future, "to": later}},
    ).json()
    assert body[0]["datapoints"] == []


def test_table_format_carries_the_sigma_column(bracketed):
    body = bracketed.post(
        "/grafana/query", json={"targets": [{"target": TARGET, "type": "table"}]}
    ).json()
    assert body[0]["type"] == "table"
    assert [c["text"] for c in body[0]["columns"]] == [
        "Time",
        "Value",
        "Sigma",
        "Unit",
    ]
    assert all(row[3] == "s" for row in body[0]["rows"])


def test_unstable_runs_are_annotated(client):
    """A step caused by the machine should be explained on the dashboard."""
    with client.sessionmaker() as db:
        for i in range(5):
            sha = f"{i:040d}"
            add(db, sha, [5.0], role=Role.REFERENCE, position=Position.BEFORE.value)
            add(db, sha, [10.0])
            # A large swing across the payload on the last run only.
            after = 9.0 if i == 4 else 5.0
            add(db, sha, [after], role=Role.REFERENCE, position=Position.AFTER.value)

    body = client.post(
        "/grafana/annotations",
        json={"annotation": {"name": "machine", "query": TARGET}},
    ).json()
    assert len(body) == 1
    assert "Machine unstable" in body[0]["title"]


def test_tag_keys_expose_label_names(populated):
    keys = {k["text"] for k in populated.post("/grafana/tag-keys", json={}).json()}
    assert {"benchmark", "runner"} <= keys


def test_tag_values_expose_label_values(populated):
    values = {
        v["text"]
        for v in populated.post("/grafana/tag-values", json={"key": "runner"}).json()
    }
    assert values == {"ubuntu-latest"}
