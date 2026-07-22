"""Shared FastAPI dependencies.

Separate from `api` so that route modules (`grafana`) can depend on them
without importing the app that mounts them. It also keeps annotations
resolvable at module scope: with postponed evaluation, a dependency captured in
a closure cannot be looked up when FastAPI inspects the signature.

Tests override these by identity -- `app.dependency_overrides[deps.session]` --
so they must stay module-level singletons rather than being rebuilt per import.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException

from .auth import RunAttestor, Verifier
from .config import Settings, settings
from .db import session
from .github import GitHubApp, NullGitHubApp

__all__ = ["session", "config", "verifier", "github", "attestor"]

_verifier: Verifier | None = None
_github: GitHubApp | NullGitHubApp | None = None


def config() -> Settings:
    return settings()


def verifier(cfg: Annotated[Settings, Depends(config)]) -> Verifier:
    global _verifier
    if _verifier is None:
        _verifier = Verifier(
            audience=cfg.audience,
            allowed_repos=cfg.allowed_repos,
            default_branch=cfg.default_branch,
        )
    return _verifier


def github(
    cfg: Annotated[Settings, Depends(config)],
) -> GitHubApp | NullGitHubApp | None:
    global _github
    if _github is not None:
        return _github
    if cfg.github_dry_run:
        _github = NullGitHubApp()
    elif cfg.github_app_id and cfg.github_private_key:
        _github = GitHubApp(
            cfg.github_app_id, cfg.github_private_key, cfg.github_installation_id
        )
    return _github


def attestor(
    gh: Annotated[GitHubApp | NullGitHubApp | None, Depends(github)],
) -> RunAttestor:
    if isinstance(gh, NullGitHubApp):
        raise HTTPException(503, "lease path unavailable: GitHub is in dry-run mode")
    if gh is None:
        # Without GitHub credentials the fork path cannot verify anything, and
        # accepting unverified claims would be strictly worse than refusing.
        raise HTTPException(503, "lease path unavailable: no GitHub app configured")
    return RunAttestor(gh)


def reset() -> None:
    """Drop cached clients. For tests and for configuration reloads."""
    global _verifier, _github
    _verifier = None
    _github = None
