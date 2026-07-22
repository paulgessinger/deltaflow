"""Identity: establishing who submitted a measurement.

The security property worth stating plainly: none of this attests that a number
is *true*. Any workflow running contributor code can emit whatever values it
likes. The job here is to confine a dishonest measurement to the pull request it
came from, and above all to keep it out of the mainline baseline -- the only
place where a bad write corrupts data going forward rather than making a single
comment wrong.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import fnmatch
import hashlib
import re
import secrets
import time

import httpx
import jwt
from jwt import PyJWKClient

from .models import Context, Trust

ISSUER = "https://token.actions.githubusercontent.com"
JWKS_URL = f"{ISSUER}/.well-known/jwks"
_PR_REF = re.compile(r"^refs/pull/(\d+)/(merge|head)$")

LEASE_TTL = dt.timedelta(hours=3)


class AuthError(Exception):
    """Rejected. Messages are safe to return; they never echo claim contents."""


def new_secret() -> tuple[str, str]:
    """Return (secret, hash). Only the hash is ever stored."""
    secret = secrets.token_urlsafe(32)
    return secret, hash_secret(secret)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


@dataclasses.dataclass(frozen=True)
class Identity:
    """Trusted submission context. Derived from claims, never from the body."""

    repo: str
    context: Context
    trust: Trust
    run_id: str
    run_attempt: int
    workflow: str = ""
    job: str = ""
    ref: str | None = None
    merge_sha: str | None = None
    pr: int | None = None
    # Set only on the lease path, where the commit was pinned at claim time and
    # the submission is not permitted to address any other.
    pinned_head_sha: str | None = None


class Verifier:
    """Validates GitHub Actions OIDC tokens."""

    def __init__(
        self,
        audience: str,
        allowed_repos: list[str],
        default_branch: str = "main",
        jwks_url: str = JWKS_URL,
    ) -> None:
        self.audience = audience
        self.allowed_repos = allowed_repos
        self.default_branch = default_branch
        self._jwks = PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)

    def repo_allowed(self, repo: str) -> bool:
        return any(fnmatch.fnmatch(repo, pat) for pat in self.allowed_repos)

    def verify(self, token: str) -> Identity:
        try:
            key = self._jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                # The audience is deployment-specific: it is what stops a token
                # minted for another service being replayed against this one.
                audience=self.audience,
                issuer=ISSUER,
                options={"require": ["exp", "iat", "aud", "iss", "sub"]},
                leeway=60,
            )
        except (jwt.PyJWTError, httpx.HTTPError) as exc:
            raise AuthError("token validation failed") from exc

        repo = claims.get("repository", "")
        if not self.repo_allowed(repo):
            raise AuthError("repository not permitted")

        if claims.get("iat", 0) < time.time() - 3600:
            raise AuthError("token too old")

        ref = claims.get("ref")
        event = claims.get("event_name", "")

        pr = None
        if m := _PR_REF.match(ref or ""):
            pr = int(m.group(1))

        # The single combination that may write a baseline. A pull request
        # cannot forge `ref`, so it cannot reach this branch.
        is_mainline = event == "push" and ref == f"refs/heads/{self.default_branch}"

        return Identity(
            repo=repo,
            context=Context.MAINLINE if is_mainline else Context.PR,
            trust=Trust.OIDC,
            run_id=str(claims.get("run_id", "")),
            run_attempt=int(claims.get("run_attempt", 1) or 1),
            workflow=claims.get("workflow", ""),
            ref=ref,
            merge_sha=claims.get("sha"),
            pr=pr,
        )


class RunAttestor:
    """Confirms with GitHub that a claimed benchmark job is genuinely running.

    This is the substitute for a credential on the fork path. A fork pull
    request cannot mint an OIDC token, but it knows something an outsider does
    not: which run and job it is. GitHub will confirm that for free.
    """

    def __init__(self, api_token_provider, api_base: str = "https://api.github.com"):
        self._token = api_token_provider
        self.api = api_base

    def _get(self, path: str, **params) -> dict:
        resp = httpx.get(
            f"{self.api}{path}",
            headers={
                "Authorization": f"Bearer {self._token()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            params=params or None,
            timeout=15,
        )
        if resp.status_code == 404:
            raise AuthError("no such run, job, or pull request")
        resp.raise_for_status()
        return resp.json()

    def attest(
        self, repo: str, run_id: str, run_attempt: int, job: str, pr: int, head_sha: str
    ) -> None:
        """Raise AuthError unless every asserted fact checks out."""
        run = self._get(f"/repos/{repo}/actions/runs/{run_id}")

        if run.get("status") not in ("queued", "in_progress"):
            # A finished run cannot still be producing benchmark results; this
            # is what stops replay against old runs.
            raise AuthError("run is not currently executing")

        if str(run.get("run_attempt", 1)) != str(run_attempt):
            raise AuthError("run attempt mismatch")

        if run.get("head_sha") != head_sha:
            raise AuthError("commit does not match the run under way")

        # Verifying the *job* -- not merely the run -- is what collapses the
        # race window from the benchmark's whole duration to about a second.
        jobs = self._get(
            f"/repos/{repo}/actions/runs/{run_id}/attempts/{run_attempt}/jobs",
            per_page=100,
        )
        match = next((j for j in jobs.get("jobs", []) if j.get("name") == job), None)
        if match is None:
            raise AuthError("no such job in this run")
        if match.get("status") != "in_progress":
            raise AuthError("job is not currently executing")

        pull = self._get(f"/repos/{repo}/pulls/{pr}")
        if pull.get("state") != "open":
            raise AuthError("pull request is not open")
        if (pull.get("head") or {}).get("sha") != head_sha:
            raise AuthError("commit is not the head of that pull request")

        # Ties the run to the pull request's source repository, so a run on one
        # fork cannot be used to submit against another's pull request.
        run_head_repo = (run.get("head_repository") or {}).get("full_name")
        pr_head_repo = ((pull.get("head") or {}).get("repo") or {}).get("full_name")
        if run_head_repo and pr_head_repo and run_head_repo != pr_head_repo:
            raise AuthError("run does not originate from the pull request's head repo")
