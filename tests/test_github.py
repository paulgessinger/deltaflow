"""The GitHub App client, exercised over a fake API rather than method stubs."""

from __future__ import annotations

import pytest

from deltaflow.auth import AuthError, RunAttestor
from deltaflow.github import MARKER, GitHubApp, GitHubError, normalise_private_key

from fake_github import FakeGitHub, generate_private_key

REPO = "acts-project/acts"


@pytest.fixture(scope="module")
def private_key() -> str:
    return generate_private_key()


@pytest.fixture
def fake() -> FakeGitHub:
    return FakeGitHub()


@pytest.fixture
def app(fake, private_key) -> GitHubApp:
    return GitHubApp(
        app_id="12345",
        private_key=private_key,
        installation_id="678",
        transport=fake.transport(),
    )


# --- authentication ----------------------------------------------------------


def test_installation_token_is_cached(app, fake):
    app.installation_token()
    app.installation_token()
    assert fake.token_calls == 1


def test_check_reports_the_app_identity(app):
    assert app.check()["app"] == "deltaflow-test"


def test_escaped_newlines_in_a_private_key_are_repaired():
    """Keys routinely arrive from env vars with literal backslash-n."""
    pem = "-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----"
    assert "\\n" not in normalise_private_key(pem)
    assert normalise_private_key(pem).count("\n") == 2


def test_a_real_key_is_left_alone(private_key):
    assert normalise_private_key(private_key) == private_key.strip()


def test_transient_failure_minting_a_token_is_retried(fake, private_key, monkeypatch):
    """A 5xx here would otherwise fail a report that was about to succeed."""
    monkeypatch.setattr("deltaflow.github.time.sleep", lambda _s: None)
    fake.fail_token_next = [(503, {})]
    app = GitHubApp("1", private_key, "2", transport=fake.transport())
    assert app.installation_token()


def test_a_credential_rejection_is_never_retried(fake, private_key, monkeypatch):
    monkeypatch.setattr("deltaflow.github.time.sleep", lambda _s: None)
    fake.fail_token_next = [(401, {})] * 5
    app = GitHubApp("1", private_key, "2", transport=fake.transport())
    with pytest.raises(GitHubError):
        app.installation_token()
    assert len(fake.fail_token_next) == 4  # one attempt consumed, not four


def test_bad_credentials_fail_with_an_actionable_message(fake, private_key):
    fake.fail_token_next = [(401, {})]
    app = GitHubApp("1", private_key, "2", transport=fake.transport())
    with pytest.raises(GitHubError, match="app id, installation id, and private key"):
        app.installation_token()


# --- comment upsert ----------------------------------------------------------


def test_first_report_creates_a_comment(app, fake):
    app.upsert_comment(REPO, 7, "hello")
    assert fake.comment_bodies(7) == [f"{MARKER}\nhello"]


def test_second_report_edits_in_place(app, fake):
    first = app.upsert_comment(REPO, 7, "one")
    second = app.upsert_comment(REPO, 7, "two", comment_id=first)
    assert first == second
    assert fake.comment_bodies(7) == [f"{MARKER}\ntwo"]


def test_a_known_id_avoids_scanning_entirely(app, fake):
    cid = app.upsert_comment(REPO, 7, "one")
    fake.requests.clear()
    app.upsert_comment(REPO, 7, "two", comment_id=cid)
    assert not any(m == "GET" for m, _ in fake.requests)


def test_a_deleted_comment_is_recreated(app, fake):
    cid = app.upsert_comment(REPO, 7, "one")
    del fake.comments[cid]
    new = app.upsert_comment(REPO, 7, "two", comment_id=cid)
    assert new != cid
    assert fake.comment_bodies(7) == [f"{MARKER}\ntwo"]


def test_the_comment_is_found_beyond_the_first_page(app, fake):
    """The bug this replaced: on a busy pull request the marker fell off page
    one, was never found, and a duplicate was posted on every submission."""
    ours = app.upsert_comment(REPO, 7, "ours")
    for i in range(250):
        fake.add_comment(7, f"unrelated discussion {i}")

    again = app.upsert_comment(REPO, 7, "updated")

    assert again == ours
    assert len([b for b in fake.comment_bodies(7) if MARKER in b]) == 1


def test_other_comments_are_never_touched(app, fake):
    theirs = fake.add_comment(7, "please rebase")
    app.upsert_comment(REPO, 7, "report")
    assert fake.comments[theirs]["body"] == "please rebase"


# --- resilience --------------------------------------------------------------


def test_a_transient_failure_is_retried(app, fake, monkeypatch):
    monkeypatch.setattr("deltaflow.github.time.sleep", lambda _s: None)
    fake.fail_next = [(502, {})]
    app.upsert_comment(REPO, 7, "hello")
    assert fake.comment_bodies(7) == [f"{MARKER}\nhello"]


def test_secondary_rate_limits_are_honoured(app, fake, monkeypatch):
    """GitHub signals these with 403 plus Retry-After, not 429."""
    slept: list[float] = []
    monkeypatch.setattr("deltaflow.github.time.sleep", slept.append)
    fake.fail_next = [(403, {"Retry-After": "7"})]

    app.upsert_comment(REPO, 7, "hello")
    assert slept == [7.0]


def test_retry_delay_is_capped(app, fake, monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr("deltaflow.github.time.sleep", slept.append)
    fake.fail_next = [(429, {"Retry-After": "99999"})]

    app.upsert_comment(REPO, 7, "hello")
    assert slept == [60.0]


def test_persistent_failure_eventually_gives_up(app, fake, monkeypatch):
    monkeypatch.setattr("deltaflow.github.time.sleep", lambda _s: None)
    fake.fail_next = [(500, {})] * 10
    with pytest.raises(GitHubError, match="after 4 attempts"):
        app.upsert_comment(REPO, 7, "hello")


def test_a_client_error_is_not_retried(app, fake, monkeypatch):
    """A malformed request stays malformed; repeating it only wastes budget."""
    monkeypatch.setattr("deltaflow.github.time.sleep", lambda _s: None)
    app.installation_token()  # take token minting out of the count
    fake.requests.clear()
    fake.fail_next = [(422, {})]

    with pytest.raises(Exception):
        app.upsert_comment(REPO, 7, "hello")

    assert len(fake.requests) == 1


# --- attestation over the same client ----------------------------------------


def test_attestation_runs_against_the_real_http_path(app, fake):
    RunAttestor(app).attest(
        repo=REPO, run_id="99", run_attempt=1, job="bench", pr=7, head_sha="c" * 40
    )


def test_a_missing_run_is_a_rejected_claim_not_an_outage(app, fake):
    fake.missing = True
    with pytest.raises(AuthError):
        RunAttestor(app).attest(
            repo=REPO, run_id="1", run_attempt=1, job="b", pr=7, head_sha="c" * 40
        )


def test_attestor_and_poster_share_one_installation_token(app, fake):
    app.upsert_comment(REPO, 7, "hello")
    RunAttestor(app).attest(
        repo=REPO, run_id="99", run_attempt=1, job="bench", pr=7, head_sha="c" * 40
    )
    assert fake.token_calls == 1
