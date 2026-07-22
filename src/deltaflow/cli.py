"""Command line entry points: serve the API, administer tokens, submit from CI."""

from __future__ import annotations

import json
import os
import pathlib
import sys
from typing import Annotated

import httpx
import typer

app = typer.Typer(help="Continuous benchmark tracking.", no_args_is_help=True)


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Bind port.")] = 8000,
    reload: Annotated[bool, typer.Option(help="Reload on source changes.")] = False,
) -> None:
    """Run the API server."""
    import uvicorn

    uvicorn.run("deltaflow.api:app", host=host, port=port, reload=reload)


@app.command()
def initdb() -> None:
    """Create tables in the configured database."""
    from .db import init_db

    init_db()
    typer.echo("schema created")


@app.command("mint-token")
def mint_token(
    name: Annotated[str, typer.Argument(help="Human label, e.g. 'bare-metal-01'.")],
    repo: Annotated[str, typer.Option(help="Repository this token may write to.")],
) -> None:
    """Issue a scoped API token for a submitter outside GitHub Actions.

    Printed once and never recoverable -- only the hash is stored.
    """
    from .auth import new_secret
    from .db import init_db, session
    from .models import ApiToken

    init_db()
    secret, digest = new_secret()
    with next(session()) as db:  # type: ignore[arg-type]
        db.add(ApiToken(name=name, repo=repo, secret_hash=digest))
        db.commit()
    typer.echo(secret)


def _oidc_token(audience: str) -> str | None:
    """Mint a GitHub Actions OIDC token, or None if this job cannot.

    Requires `permissions: id-token: write`, which pull requests from forks on
    public repositories cannot be granted. Absence is the normal fork case, not
    an error -- the caller falls back to the lease path.
    """
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not token:
        return None
    # The URL already carries an api-version query string, and httpx replaces
    # the query rather than merging into it, so append by hand.
    sep = "&" if "?" in url else "?"
    resp = httpx.get(
        f"{url}{sep}audience={audience}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["value"]


def _claim_lease(server: str, repo: str, job: str, pr: int, head_sha: str) -> str:
    """Take the fork path: ask the server to verify and reserve this slot."""
    resp = httpx.post(
        f"{server}/v1/claim",
        params={"repo": repo},
        json={
            "run_id": os.environ.get("GITHUB_RUN_ID", ""),
            "run_attempt": int(os.environ.get("GITHUB_RUN_ATTEMPT", "1")),
            "job": job,
            "pr": pr,
            "head_sha": head_sha,
        },
        timeout=30,
    )
    if resp.status_code == 409:
        raise typer.BadParameter(
            "this benchmark slot was already claimed by something else. "
            "If this job did not claim it, someone is submitting under your "
            "identity -- please report it."
        )
    resp.raise_for_status()
    return resp.json()["secret"]


@app.command()
def claim(
    server: Annotated[str, typer.Option(envvar="DELTAFLOW_SERVER")],
    head_sha: Annotated[str, typer.Option(envvar="DELTAFLOW_HEAD_SHA")],
    pr: Annotated[int, typer.Option(help="Pull request number.")],
    repo: Annotated[str, typer.Option(envvar="GITHUB_REPOSITORY")] = "",
    job: Annotated[str, typer.Option(envvar="GITHUB_JOB")] = "",
) -> None:
    """Reserve this job's submission slot. Run it as the job's first step.

    Claiming early is the whole point: it shrinks the window in which anything
    else could claim this slot down to the moment between the job starting and
    this call. Print the secret into GITHUB_ENV as DELTAFLOW_LEASE and the
    later submit step will use it.
    """
    typer.echo(_claim_lease(server.rstrip("/"), repo, job, pr, head_sha))


@app.command()
def submit(
    results: Annotated[
        pathlib.Path,
        typer.Argument(help="JSON file of measurements, or '-' for stdin."),
    ],
    server: Annotated[
        str, typer.Option(envvar="DELTAFLOW_SERVER", help="Base URL of the API.")
    ],
    head_sha: Annotated[
        str,
        typer.Option(
            envvar="DELTAFLOW_HEAD_SHA",
            help="Commit the numbers describe. Must be the real head commit, "
            "not the pull request merge commit.",
        ),
    ],
    audience: Annotated[
        str, typer.Option(envvar="DELTAFLOW_AUDIENCE", help="OIDC audience to request.")
    ] = "",
    token: Annotated[
        str,
        typer.Option(
            envvar="DELTAFLOW_TOKEN",
            help="Scoped API token, for submitters outside GitHub Actions.",
        ),
    ] = "",
    lease: Annotated[
        str,
        typer.Option(
            envvar="DELTAFLOW_LEASE",
            help="Secret from an earlier `deltaflow claim`, on the fork path.",
        ),
    ] = "",
    repo: Annotated[str, typer.Option(envvar="GITHUB_REPOSITORY")] = "",
    job: Annotated[str, typer.Option(envvar="GITHUB_JOB")] = "",
    runner: Annotated[str, typer.Option(envvar="RUNNER_NAME")] = "",
    base_sha: Annotated[str, typer.Option(envvar="DELTAFLOW_BASE_SHA")] = "",
    pr: Annotated[
        int, typer.Option(help="Pull request number, required on the fork path.")
    ] = 0,
    fail_on_error: Annotated[
        bool,
        typer.Option(
            help="Exit nonzero if submission fails. Off by default: benchmark "
            "reporting is informational and must not break CI."
        ),
    ] = False,
) -> None:
    """Send benchmark results to the server.

    Picks a credential automatically: an explicit token if given, otherwise a
    GitHub OIDC token if this job can mint one, otherwise a lease -- which is
    the fork pull request case.
    """
    server = server.rstrip("/")
    raw = sys.stdin.read() if str(results) == "-" else results.read_text()
    measurements = json.loads(raw)
    if isinstance(measurements, dict):
        measurements = measurements.get("measurements", measurements)

    def fail(message: str) -> None:
        typer.echo(f"deltaflow: {message}", err=True)
        raise typer.Exit(1 if fail_on_error else 0)

    try:
        credential = token or lease or _oidc_token(audience)
        if credential is None:
            # No claim was made at job start, so fall back to claiming now.
            # This works, but the slot sat unclaimed for the whole benchmark
            # run rather than for a moment -- prefer a `claim` step.
            typer.echo(
                "deltaflow: no lease held; claiming late. Run `deltaflow claim` "
                "as the job's first step to narrow the window.",
                err=True,
            )
            if not (repo and pr):
                fail("fork submission needs --repo and --pr")
            credential = _claim_lease(server, repo, job, pr, head_sha)

        resp = httpx.post(
            f"{server}/v1/submit",
            json={
                "run": {
                    "head_sha": head_sha,
                    "base_sha": base_sha or None,
                    "job": job,
                    "runner": runner,
                },
                "measurements": measurements,
            },
            headers={"Authorization": f"Bearer {credential}"},
            timeout=30,
        )
    except (httpx.HTTPError, typer.BadParameter) as exc:
        fail(str(exc))
        return

    if resp.status_code >= 400:
        fail(f"submission rejected [{resp.status_code}]: {resp.text}")
        return
    typer.echo(json.dumps(resp.json()))


if __name__ == "__main__":
    app()
