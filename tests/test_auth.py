"""OIDC claim handling and run attestation.

The tests that matter here are the negative ones: what must *not* be accepted.
"""

from __future__ import annotations

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from deltaflow.auth import ISSUER, AuthError, RunAttestor, Verifier
from deltaflow.models import Context, Trust

REPO = "acts-project/acts"
AUD = "https://deltaflow.test"


@pytest.fixture(scope="module")
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key, key.public_key()


@pytest.fixture
def verifier(keypair):
    _, public = keypair
    v = Verifier(audience=AUD, allowed_repos=[REPO], default_branch="main")

    class _Key:
        key = public

    v._jwks.get_signing_key_from_jwt = lambda _token: _Key()  # type: ignore[method-assign]
    return v


def make_token(keypair, **overrides) -> str:
    private, _ = keypair
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "aud": AUD,
        "sub": f"repo:{REPO}:ref:refs/heads/main",
        "iat": now,
        "exp": now + 300,
        "repository": REPO,
        "event_name": "push",
        "ref": "refs/heads/main",
        "sha": "a" * 40,
        "run_id": "12345",
        "run_attempt": "1",
        "workflow": "Benchmarks",
    }
    claims.update(overrides)
    return jwt.encode(claims, private, algorithm="RS256")


def test_push_to_default_branch_is_mainline(verifier, keypair):
    who = verifier.verify(make_token(keypair))
    assert who.context is Context.MAINLINE
    assert who.trust is Trust.OIDC
    assert who.repo == REPO


def test_push_to_other_branch_is_not_mainline(verifier, keypair):
    who = verifier.verify(make_token(keypair, ref="refs/heads/topic"))
    assert who.context is Context.PR


def test_pull_request_is_never_mainline(verifier, keypair):
    who = verifier.verify(
        make_token(keypair, event_name="pull_request", ref="refs/pull/4021/merge")
    )
    assert who.context is Context.PR
    assert who.pr == 4021


def test_pull_request_event_on_main_ref_is_still_not_mainline(verifier, keypair):
    """Only `push` may write a baseline, whatever the ref says."""
    who = verifier.verify(make_token(keypair, event_name="pull_request"))
    assert who.context is Context.PR


def test_merge_sha_is_kept_separate_from_history_anchor(verifier, keypair):
    """The `sha` claim on a PR is the throwaway merge commit."""
    who = verifier.verify(
        make_token(
            keypair,
            event_name="pull_request",
            ref="refs/pull/7/merge",
            sha="b" * 40,
        )
    )
    assert who.merge_sha == "b" * 40


def test_foreign_repository_is_refused(verifier, keypair):
    with pytest.raises(AuthError, match="not permitted"):
        verifier.verify(make_token(keypair, repository="attacker/evil"))


def test_wrong_audience_is_refused(verifier, keypair):
    with pytest.raises(AuthError):
        verifier.verify(make_token(keypair, aud="https://someone-else.test"))


def test_expired_token_is_refused(verifier, keypair):
    now = int(time.time())
    with pytest.raises(AuthError):
        verifier.verify(make_token(keypair, iat=now - 7200, exp=now - 3600))


def test_token_signed_by_another_key_is_refused(verifier):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    forged = jwt.encode(
        {
            "iss": ISSUER,
            "aud": AUD,
            "sub": "x",
            "iat": now,
            "exp": now + 300,
            "repository": REPO,
            "event_name": "push",
            "ref": "refs/heads/main",
        },
        other,
        algorithm="RS256",
    )
    with pytest.raises(AuthError):
        verifier.verify(forged)


# --- run attestation ---------------------------------------------------------


class FakeGitHub:
    """Minimal stand-in for the endpoints RunAttestor consults."""

    def __init__(self, run=None, jobs=None, pull=None):
        self.run = run if run is not None else {
            "status": "in_progress",
            "run_attempt": 1,
            "head_sha": "c" * 40,
            "head_repository": {"full_name": "contributor/acts"},
        }
        self.jobs = jobs if jobs is not None else {
            "jobs": [{"name": "bench", "status": "in_progress"}]
        }
        self.pull = pull if pull is not None else {
            "state": "open",
            "head": {"sha": "c" * 40, "repo": {"full_name": "contributor/acts"}},
        }

    def get(self, path, **_params):
        if "/jobs" in path:
            return self.jobs
        if "/pulls/" in path:
            return self.pull
        return self.run


def attestor_with(fake: FakeGitHub) -> RunAttestor:
    att = RunAttestor(lambda: "token")
    att._get = fake.get  # type: ignore[method-assign]
    return att


def attest(att: RunAttestor) -> None:
    att.attest(
        repo=REPO, run_id="99", run_attempt=1, job="bench", pr=7, head_sha="c" * 40
    )


def test_live_job_on_matching_pull_request_is_attested():
    attest(attestor_with(FakeGitHub()))


def test_completed_run_cannot_submit():
    fake = FakeGitHub(run={"status": "completed", "run_attempt": 1, "head_sha": "c" * 40})
    with pytest.raises(AuthError, match="not currently executing"):
        attest(attestor_with(fake))


def test_job_that_is_not_running_cannot_submit():
    fake = FakeGitHub(jobs={"jobs": [{"name": "bench", "status": "completed"}]})
    with pytest.raises(AuthError, match="job is not currently executing"):
        attest(attestor_with(fake))


def test_unknown_job_name_cannot_submit():
    fake = FakeGitHub(jobs={"jobs": [{"name": "something-else", "status": "in_progress"}]})
    with pytest.raises(AuthError, match="no such job"):
        attest(attestor_with(fake))


def test_stale_commit_cannot_submit():
    fake = FakeGitHub(
        pull={"state": "open", "head": {"sha": "d" * 40, "repo": {"full_name": "x/y"}}}
    )
    with pytest.raises(AuthError, match="not the head of that pull request"):
        attest(attestor_with(fake))


def test_closed_pull_request_cannot_submit():
    fake = FakeGitHub(
        pull={"state": "closed", "head": {"sha": "c" * 40, "repo": {"full_name": "x/y"}}}
    )
    with pytest.raises(AuthError, match="not open"):
        attest(attestor_with(fake))


def test_run_from_a_different_fork_cannot_submit():
    """A run on one fork must not be usable against another's pull request."""
    fake = FakeGitHub(
        pull={
            "state": "open",
            "head": {"sha": "c" * 40, "repo": {"full_name": "someone-else/acts"}},
        }
    )
    with pytest.raises(AuthError, match="head repo"):
        attest(attestor_with(fake))
