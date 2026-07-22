"""Runtime configuration, all via DELTAFLOW_* environment variables."""

from __future__ import annotations

import functools

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DELTAFLOW_", env_file=".env")

    database_url: str = "sqlite:///deltaflow.db"

    # The `aud` claim submitters must request. Set it to your deployment's URL:
    # a token minted for someone else's audience must not be replayable here.
    audience: str = "https://deltaflow.invalid"

    # fnmatch patterns, e.g. ["acts-project/*"]. Empty means nothing is accepted.
    allowed_repos: list[str] = []

    default_branch: str = "main"

    # --- limits on the uncredentialed claim path -------------------------
    # Per source address. The tightest limit, and the one that matters when
    # someone is simply hammering the endpoint.
    claim_per_ip_per_minute: int = 30
    # Per repository, across all callers. A ceiling on how much GitHub API
    # budget any one repository can burn through.
    claim_per_repo_per_minute: int = 300
    # Per pull request per hour. A pull request with a large job matrix claims
    # once per job per push; this allows for busy ones without being unbounded.
    claim_per_pr_per_hour: int = 120
    # Hard cap on live lease slots for a single run. A run with more benchmark
    # jobs than this is misconfigured.
    max_leases_per_run: int = 100
    # Distinct series a single pull request may create. Unbounded label
    # cardinality from an untrusted submitter is how a metrics store dies.
    max_series_per_pr: int = 2000
    # Submissions permitted under one lease, so a job cannot stream forever.
    max_submissions_per_lease: int = 500

    # Set only when running behind a proxy that overwrites X-Forwarded-For.
    # Trusting the header without one lets any caller forge their address and
    # bypass the per-address limit entirely.
    trust_forwarded_for: bool = False

    # Baseline window: how many recent mainline points feed the comparison.
    baseline_window: int = 50

    # GitHub App credentials for posting the pull request comment. Ingest needs
    # no secret; reporting unavoidably does.
    github_app_id: str = ""
    github_private_key: str = ""
    github_installation_id: str = ""

    # Run every path for real -- OIDC, attestation, ingest -- but record what
    # would have been posted instead of writing to anyone's pull request.
    github_dry_run: bool = False

    grafana_url: str = ""


@functools.lru_cache
def settings() -> Settings:
    return Settings()
