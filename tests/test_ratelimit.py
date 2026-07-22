"""Limits on the one endpoint the open internet can reach without a credential."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import select

from deltaflow import api
from deltaflow.models import Lease, RateBucket
from deltaflow.ratelimit import Limit, RateLimited, consume, purge

REPO = "acts-project/acts"
HEAD = "c" * 40
MINUTE = Limit(3, dt.timedelta(minutes=1))


def claim_body(**kw):
    body = {"run_id": "99", "run_attempt": 1, "job": "bench", "pr": 7, "head_sha": HEAD}
    body.update(kw)
    return body


class StubAttestor:
    def attest(self, **_kw):
        pass


@pytest.fixture
def claiming(client):
    api.app.dependency_overrides[api.attestor] = lambda: StubAttestor()
    return client


# --- the counter -------------------------------------------------------------


def test_budget_allows_exactly_its_count(db_sessionmaker):
    with db_sessionmaker() as db:
        for _ in range(MINUTE.count):
            consume(db, "k", MINUTE)
        with pytest.raises(RateLimited):
            consume(db, "k", MINUTE)


def test_keys_have_independent_budgets(db_sessionmaker):
    with db_sessionmaker() as db:
        for _ in range(MINUTE.count):
            consume(db, "a", MINUTE)
        consume(db, "b", MINUTE)  # must not raise


def test_budget_refreshes_in_the_next_window(db_sessionmaker):
    with db_sessionmaker() as db:
        now = dt.datetime.now(dt.UTC)
        for _ in range(MINUTE.count):
            consume(db, "k", MINUTE, now=now)
        with pytest.raises(RateLimited):
            consume(db, "k", MINUTE, now=now)
        consume(db, "k", MINUTE, now=now + dt.timedelta(minutes=1))


def test_rejection_says_when_to_come_back(db_sessionmaker):
    with db_sessionmaker() as db:
        for _ in range(MINUTE.count):
            consume(db, "k", MINUTE)
        with pytest.raises(RateLimited) as exc:
            consume(db, "k", MINUTE)
    assert 0 < exc.value.retry_after <= 60


def test_counters_survive_a_restart(db_sessionmaker):
    """In the database, not in memory: a deploy must not reset an attacker."""
    with db_sessionmaker() as db:
        for _ in range(MINUTE.count):
            consume(db, "k", MINUTE)
        db.commit()
    with db_sessionmaker() as fresh:
        with pytest.raises(RateLimited):
            consume(fresh, "k", MINUTE)


def test_purge_drops_only_stale_buckets(db_sessionmaker):
    with db_sessionmaker() as db:
        consume(db, "recent", MINUTE)
        db.add(
            RateBucket(
                key="ancient",
                window_start=dt.datetime.now(dt.UTC) - dt.timedelta(days=7),
                count=5,
            )
        )
        db.commit()
        assert purge(db) == 1
        db.commit()
        assert {b.key for b in db.scalars(select(RateBucket)).all()} == {"recent"}


# --- the endpoint ------------------------------------------------------------


def test_claims_are_limited_per_address(claiming, settings):
    settings.claim_per_ip_per_minute = 2
    for i in range(2):
        resp = claiming.post(
            "/v1/claim", params={"repo": REPO}, json=claim_body(job=f"bench-{i}")
        )
        assert resp.status_code == 200
    resp = claiming.post(
        "/v1/claim", params={"repo": REPO}, json=claim_body(job="bench-x")
    )
    assert resp.status_code == 429
    assert resp.headers["Retry-After"]


def test_limit_is_charged_before_github_is_consulted(claiming, settings):
    """A blocked caller must not be able to burn the app's API budget."""
    settings.claim_per_ip_per_minute = 1

    class Exploding:
        def attest(self, **_kw):
            raise AssertionError("GitHub must not be called when rate limited")

    claiming.post("/v1/claim", params={"repo": REPO}, json=claim_body())
    api.app.dependency_overrides[api.attestor] = lambda: Exploding()
    resp = claiming.post(
        "/v1/claim", params={"repo": REPO}, json=claim_body(job="other")
    )
    assert resp.status_code == 429


def test_a_run_cannot_hold_unbounded_lease_slots(claiming, settings):
    settings.max_leases_per_run = 3
    for i in range(3):
        assert (
            claiming.post(
                "/v1/claim", params={"repo": REPO}, json=claim_body(job=f"j{i}")
            ).status_code
            == 200
        )
    resp = claiming.post("/v1/claim", params={"repo": REPO}, json=claim_body(job="j4"))
    assert resp.status_code == 429


def test_expired_leases_are_cleaned_up(claiming, settings):
    with claiming.sessionmaker() as db:
        db.add(
            Lease(
                repo=REPO,
                run_id="old",
                run_attempt=1,
                job="stale",
                pr=1,
                head_sha=HEAD,
                secret_hash="dead",
                expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=2),
            )
        )
        db.commit()

    claiming.post("/v1/claim", params={"repo": REPO}, json=claim_body())

    with claiming.sessionmaker() as db:
        assert db.scalars(select(Lease).where(Lease.job == "stale")).all() == []


# --- what a granted lease may spend ------------------------------------------


def _submit(client, secret, series_suffix, pr_head=HEAD):
    return client.post(
        "/v1/submit",
        json={
            "run": {"head_sha": pr_head, "job": "bench"},
            "measurements": [
                {
                    "metric": "runtime",
                    "values": [1.0],
                    "labels": {"benchmark": f"b{series_suffix}"},
                }
            ],
        },
        headers={"Authorization": f"Bearer {secret}"},
    )


@pytest.fixture
def leased(client):
    from deltaflow.auth import new_secret

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


def test_a_pull_request_cannot_invent_unbounded_series(client, leased, settings):
    """Untrusted label values are how a metrics store fills up."""
    settings.max_series_per_pr = 3
    for i in range(3):
        assert _submit(client, leased, i).status_code == 200
    assert _submit(client, leased, 99).status_code == 429


def test_resubmitting_known_series_is_not_blocked_by_the_cap(client, leased, settings):
    settings.max_series_per_pr = 2
    assert _submit(client, leased, 0).status_code == 200
    assert _submit(client, leased, 1).status_code == 200
    # At the cap, but introducing nothing new.
    assert _submit(client, leased, 0).status_code == 200


def test_one_lease_cannot_stream_forever(client, leased, settings):
    settings.max_submissions_per_lease = 2
    for i in range(2):
        assert _submit(client, leased, i).status_code == 200
    assert _submit(client, leased, 3).status_code == 429
