from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from deltaflow import api
from deltaflow.auth import AuthError, new_secret
from deltaflow.models import ApiToken, Context, Lease, Measurement, Trust

REPO = "acts-project/acts"

HEAD = "c" * 40
OTHER = "d" * 40


def payload(head_sha=HEAD, metric="runtime", values=(1.0, 1.1, 1.05), job="bench"):
    return {
        "run": {"head_sha": head_sha, "job": job, "runner": "ubuntu-latest"},
        "measurements": [
            {
                "metric": metric,
                "values": list(values),
                "unit": "s",
                "labels": {"benchmark": "seeding"},
            }
        ],
    }


@pytest.fixture
def api_token(client):
    secret, digest = new_secret()
    with client.sessionmaker() as db:
        db.add(ApiToken(name="bare-metal-01", repo=REPO, secret_hash=digest))
        db.commit()
    return secret


@pytest.fixture
def lease_secret(client):
    secret, digest = new_secret()
    with client.sessionmaker() as db:
        db.add(
            Lease(
                repo=REPO,
                run_id="99",
                run_attempt=1,
                job="bench",
                pr=7,
                head_sha=HEAD,
                secret_hash=digest,
                expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(hours=1),
            )
        )
        db.commit()
    return secret


def post(client, secret, body):
    return client.post(
        "/v1/submit", json=body, headers={"Authorization": f"Bearer {secret}"}
    )


# --- credentials -------------------------------------------------------------


def test_missing_credential_is_rejected(client):
    assert client.post("/v1/submit", json=payload()).status_code == 422


def test_garbage_credential_is_rejected(client):
    assert post(client, "not-a-real-secret", payload()).status_code == 401


def test_expired_lease_is_rejected(client):
    secret, digest = new_secret()
    with client.sessionmaker() as db:
        db.add(
            Lease(
                repo=REPO,
                run_id="1",
                run_attempt=1,
                job="bench",
                pr=7,
                head_sha=HEAD,
                secret_hash=digest,
                expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1),
            )
        )
        db.commit()
    assert post(client, secret, payload()).status_code == 401


def test_revoked_token_is_rejected(client, api_token):
    with client.sessionmaker() as db:
        token = db.scalars(select(ApiToken)).one()
        token.revoked = True
        db.commit()
    assert post(client, api_token, payload()).status_code == 401


# --- the mainline boundary ---------------------------------------------------


def test_scoped_token_writes_mainline(client, api_token):
    resp = post(client, api_token, payload())
    assert resp.status_code == 200
    assert resp.json()["context"] == Context.MAINLINE.value
    assert resp.json()["trust"] == Trust.TOKEN.value


def test_lease_submission_is_pull_request_context(client, lease_secret):
    resp = post(client, lease_secret, payload())
    assert resp.status_code == 200
    assert resp.json()["context"] == Context.PR.value

    with client.sessionmaker() as db:
        rows = db.scalars(select(Measurement)).all()
    assert rows
    assert all(r.context == Context.PR.value for r in rows)
    assert all(r.pr == 7 for r in rows)


def test_lease_measurements_never_enter_a_baseline(client, lease_secret, api_token):
    """The one corruption that outlives a single comment."""
    for _ in range(10):
        post(client, lease_secret, payload())

    from deltaflow.queries import baseline_points
    from deltaflow.models import series_key

    key = series_key(REPO, "runtime", {"benchmark": "seeding"})
    with client.sessionmaker() as db:
        assert baseline_points(db, key, window=50) == []


def test_lease_cannot_submit_for_another_commit(client, lease_secret):
    """A claim is pinned to the commit it was granted for."""
    resp = post(client, lease_secret, payload(head_sha=OTHER))
    assert resp.status_code == 403
    assert "lease" in resp.json()["detail"]


def test_lease_pull_request_cannot_be_overridden_by_the_body(client, lease_secret):
    body = payload()
    body["run"]["pr"] = 9999  # ignored: not part of the schema
    post(client, lease_secret, body)
    with client.sessionmaker() as db:
        assert {r.pr for r in db.scalars(select(Measurement)).all()} == {7}


# --- storage semantics -------------------------------------------------------


def test_every_repetition_is_stored_separately(client, api_token):
    post(client, api_token, payload(values=(1.0, 1.1, 1.05, 0.98)))
    with client.sessionmaker() as db:
        rows = db.scalars(select(Measurement)).all()
    assert len(rows) == 4
    assert sorted(r.rep for r in rows) == [0, 1, 2, 3]


def test_rerun_of_the_same_job_is_deduplicated(client, lease_secret):
    first = post(client, lease_secret, payload())
    second = post(client, lease_secret, payload())
    assert first.json()["accepted"] == 3
    assert second.json()["accepted"] == 0
    assert second.json()["duplicates"] == 3


def test_non_finite_values_are_refused(client, api_token):
    """An overflowing literal parses to infinity and would poison every
    aggregate computed over the series."""
    raw = (
        '{"run": {"head_sha": "' + HEAD + '", "job": "bench"}, '
        '"measurements": [{"metric": "runtime", "values": [1e999]}]}'
    )
    resp = client.post(
        "/v1/submit",
        content=raw,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 422


def test_label_cardinality_is_bounded(client, api_token):
    body = payload()
    body["measurements"][0]["labels"] = {f"k{i}": "v" for i in range(20)}
    assert post(client, api_token, body).status_code == 422


# --- claim -------------------------------------------------------------------


class StubAttestor:
    def __init__(self, error: str | None = None):
        self.error = error

    def attest(self, **_kw):
        if self.error:
            raise AuthError(self.error)


def use_attestor(stub):
    api.app.dependency_overrides[api.attestor] = lambda: stub


def test_claim_grants_a_secret(client):
    use_attestor(StubAttestor())
    resp = client.post(
        "/v1/claim",
        params={"repo": REPO},
        json={"run_id": "99", "run_attempt": 1, "job": "bench", "pr": 7,
              "head_sha": HEAD},
    )
    assert resp.status_code == 200
    assert resp.json()["secret"]


def test_second_claim_on_the_same_slot_conflicts(client):
    """Losing this race must be loud, not silent."""
    use_attestor(StubAttestor())
    body = {"run_id": "99", "run_attempt": 1, "job": "bench", "pr": 7,
            "head_sha": HEAD}
    assert client.post("/v1/claim", params={"repo": REPO}, json=body).status_code == 200
    second = client.post("/v1/claim", params={"repo": REPO}, json=body)
    assert second.status_code == 409


def test_rerun_gets_a_fresh_slot(client):
    use_attestor(StubAttestor())
    base = {"run_id": "99", "job": "bench", "pr": 7, "head_sha": HEAD}
    a = client.post("/v1/claim", params={"repo": REPO}, json={**base, "run_attempt": 1})
    b = client.post("/v1/claim", params={"repo": REPO}, json={**base, "run_attempt": 2})
    assert a.status_code == 200 and b.status_code == 200


def test_claim_is_refused_when_github_disagrees(client):
    use_attestor(StubAttestor("job is not currently executing"))
    resp = client.post(
        "/v1/claim",
        params={"repo": REPO},
        json={"run_id": "99", "run_attempt": 1, "job": "bench", "pr": 7,
              "head_sha": HEAD},
    )
    assert resp.status_code == 403


def test_claim_is_refused_for_a_foreign_repository(client):
    use_attestor(StubAttestor())
    resp = client.post(
        "/v1/claim",
        params={"repo": "attacker/evil"},
        json={"run_id": "99", "run_attempt": 1, "job": "bench", "pr": 7,
              "head_sha": HEAD},
    )
    assert resp.status_code == 403
