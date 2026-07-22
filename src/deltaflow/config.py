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

    # Baseline window: how many recent mainline points feed the comparison.
    baseline_window: int = 50

    # GitHub App credentials for posting the pull request comment. Ingest needs
    # no secret; reporting unavoidably does.
    github_app_id: str = ""
    github_private_key: str = ""
    github_installation_id: str = ""

    grafana_url: str = ""


@functools.lru_cache
def settings() -> Settings:
    return Settings()
