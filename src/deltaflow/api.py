"""HTTP surface.

Four responsibilities: grant leases, accept measurements, answer queries for the
dashboard, and rewrite the pull request comment. Notably absent: any webhook
receiver. Because the comment is upserted on every submission, there is nothing
to wait for and no completion to detect.
"""

from __future__ import annotations

import datetime as dt
import logging
import statistics
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import reporting
from .auth import (
    LEASE_TTL,
    AuthError,
    Identity,
    RunAttestor,
    Verifier,
    hash_secret,
    new_secret,
)
from .config import Settings, settings
from .db import init_db, session
from .github import GitHubApp, GitHubError, NullGitHubApp
from .models import ApiToken, Context, Lease, Measurement, Report, Trust, series_key
from .queries import baseline_points, head_series, machine_scatter, run_points
from .ratelimit import Limit, RateLimited, consume, purge
from .schemas import ClaimIn, ClaimOut, SubmissionIn, SubmissionOut
from .stats import estimate

log = logging.getLogger("deltaflow")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    init_db()
    yield


app = FastAPI(title="deltaflow", version="0.1.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def _validation_error(_request: Request, exc: RequestValidationError):
    """Report validation failures without echoing the offending input.

    Beyond not reflecting submitter-controlled data back, this is load-bearing:
    the default handler embeds the raw input in the response, and a value such
    as `1e999` parses to infinity, which cannot be serialised as JSON -- turning
    a well-formed rejection into a 500.
    """
    return JSONResponse(
        status_code=422,
        content={
            "detail": [
                {"loc": [str(p) for p in err["loc"]], "msg": err["msg"]}
                for err in exc.errors()
            ]
        },
    )


_verifier: Verifier | None = None
_github: GitHubApp | None = None


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


def github(cfg: Annotated[Settings, Depends(config)]) -> GitHubApp | None:
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


def attestor(gh: Annotated[GitHubApp | None, Depends(github)]) -> RunAttestor:
    if isinstance(gh, NullGitHubApp):
        raise HTTPException(
            503, "lease path unavailable: GitHub is in dry-run mode"
        )
    if gh is None:
        # Without GitHub credentials the fork path cannot verify anything, and
        # accepting unverified claims would be strictly worse than refusing.
        raise HTTPException(503, "lease path unavailable: no GitHub app configured")
    return RunAttestor(gh)


def client_address(request: Request, cfg: Settings) -> str:
    """Best available identifier for the caller.

    X-Forwarded-For is only consulted when a trusted proxy is declared to be in
    front. Without one the header is attacker-controlled, and honouring it
    would make the per-address limit trivially bypassable.
    """
    if cfg.trust_forwarded_for:
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _enforce_claim_limits(
    db: Session, cfg: Settings, address: str, repo: str, pr: int
) -> None:
    """Charge a claim attempt against every axis it consumes.

    Charged *before* the GitHub calls, so a rejected caller cannot use this
    endpoint to burn the installation's API budget.
    """
    minute = dt.timedelta(minutes=1)
    hour = dt.timedelta(hours=1)
    for key, limit in (
        (f"claim-ip:{address}", Limit(cfg.claim_per_ip_per_minute, minute)),
        (f"claim-repo:{repo}", Limit(cfg.claim_per_repo_per_minute, minute)),
        (f"claim-pr:{repo}#{pr}", Limit(cfg.claim_per_pr_per_hour, hour)),
    ):
        consume(db, key, limit)


def _bearer(authorization: str) -> str:
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(401, "expected: Authorization: Bearer <credential>")
    return token


def identity(
    authorization: Annotated[str, Header()],
    v: Annotated[Verifier, Depends(verifier)],
    db: Annotated[Session, Depends(session)],
) -> Identity:
    """Resolve a credential to an identity, trying each ingest path in turn.

    Order matters only for efficiency: OIDC tokens are JWTs and are recognisable
    by shape, so opaque secrets fall through to the database lookups.
    """
    credential = _bearer(authorization)

    if credential.count(".") == 2:  # JWT
        try:
            return v.verify(credential)
        except AuthError as exc:
            log.warning("rejected oidc submission: %s", exc)
            raise HTTPException(401, str(exc)) from exc

    digest = hash_secret(credential)

    lease = db.scalars(select(Lease).where(Lease.secret_hash == digest)).one_or_none()
    if lease is not None:
        if lease.expires_at.replace(tzinfo=dt.UTC) < dt.datetime.now(dt.UTC):
            raise HTTPException(401, "lease expired")
        return Identity(
            repo=lease.repo,
            context=Context.PR,  # structurally barred from mainline
            trust=Trust.LEASE,
            run_id=lease.run_id,
            run_attempt=lease.run_attempt,
            job=lease.job,
            pr=lease.pr,
            pinned_head_sha=lease.head_sha,
        )

    token = db.scalars(
        select(ApiToken).where(ApiToken.secret_hash == digest)
    ).one_or_none()
    if token is not None and not token.revoked:
        token.last_used_at = dt.datetime.now(dt.UTC)
        db.commit()
        return Identity(
            repo=token.repo,
            context=Context.MAINLINE,
            trust=Trust.TOKEN,
            # Distinct per submission. A timestamp is not enough: a cron that
            # posts several results in the same second would have them
            # collapse into one another as false duplicates.
            run_id=f"token-{token.id}-{uuid.uuid4().hex[:16]}",
            run_attempt=1,
            workflow=token.name,
        )

    raise HTTPException(401, "unrecognised credential")


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/claim", response_model=ClaimOut)
def claim(
    payload: ClaimIn,
    repo: str,
    request: Request,
    db: Annotated[Session, Depends(session)],
    cfg: Annotated[Settings, Depends(config)],
    v: Annotated[Verifier, Depends(verifier)],
    att: Annotated[RunAttestor, Depends(attestor)],
) -> ClaimOut:
    """Grant a fork job exclusive right to submit under its own identity.

    Uncredentialed by necessity -- fork pull requests have no credential to
    offer. Everything asserted is verified against GitHub before the slot is
    granted, and the slot is single-occupancy.
    """
    if not v.repo_allowed(repo):
        raise HTTPException(403, "repository not permitted")

    try:
        _enforce_claim_limits(db, cfg, client_address(request, cfg), repo, payload.pr)
        db.commit()
    except RateLimited as exc:
        db.commit()  # keep the charge; the caller still consumed an attempt
        log.warning("rate limited claim for %s from %s", repo, exc.scope)
        raise HTTPException(
            429, str(exc), headers={"Retry-After": str(exc.retry_after)}
        ) from exc

    live = db.scalar(
        select(func.count())
        .select_from(Lease)
        .where(
            Lease.repo == repo,
            Lease.run_id == payload.run_id,
            Lease.expires_at > dt.datetime.now(dt.UTC),
        )
    )
    if live and live >= cfg.max_leases_per_run:
        raise HTTPException(429, "too many benchmark jobs claimed for this run")

    try:
        att.attest(
            repo=repo,
            run_id=payload.run_id,
            run_attempt=payload.run_attempt,
            job=payload.job,
            pr=payload.pr,
            head_sha=payload.head_sha,
        )
    except AuthError as exc:
        log.warning("rejected claim for %s run %s: %s", repo, payload.run_id, exc)
        raise HTTPException(403, str(exc)) from exc

    secret, digest = new_secret()
    expires = dt.datetime.now(dt.UTC) + LEASE_TTL
    lease = Lease(
        repo=repo,
        run_id=payload.run_id,
        run_attempt=payload.run_attempt,
        job=payload.job,
        pr=payload.pr,
        head_sha=payload.head_sha,
        secret_hash=digest,
        expires_at=expires,
    )
    db.add(lease)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        # Someone already holds this slot. The legitimate job surfaces this as a
        # failed step, which is the point: silent spoofing becomes visible.
        raise HTTPException(
            409,
            "slot already claimed for this run, attempt and job — "
            "if this job did not claim it, the claim is not yours",
        ) from exc

    _housekeeping(db)

    return ClaimOut(
        secret=secret,
        expires_at=expires.isoformat(),
        pr=payload.pr,
        head_sha=payload.head_sha,
    )


def _housekeeping(db: Session) -> None:
    """Opportunistic cleanup, cheap enough to run on the claim path.

    Expired leases and stale counters would otherwise accumulate forever in a
    deployment with no scheduled jobs -- which is the deployment this is
    designed for.
    """
    now = dt.datetime.now(dt.UTC)
    expired = db.scalars(
        select(Lease).where(Lease.expires_at < now - LEASE_TTL)
    ).all()
    for lease in expired:
        db.delete(lease)
    purge(db)
    db.commit()


@app.post("/v1/submit", response_model=SubmissionOut)
def submit(
    payload: SubmissionIn,
    who: Annotated[Identity, Depends(identity)],
    db: Annotated[Session, Depends(session)],
    cfg: Annotated[Settings, Depends(config)],
    gh: Annotated[GitHubApp | None, Depends(github)],
) -> SubmissionOut:
    head_sha = payload.run.head_sha
    job = who.job or payload.run.job

    # A lease pins its commit. Anything else would let a claim for one commit be
    # spent on another.
    if who.pinned_head_sha is not None and head_sha != who.pinned_head_sha:
        raise HTTPException(403, "commit does not match the claimed lease")

    if who.context is Context.MAINLINE and not who.trust.mainline_eligible:
        raise HTTPException(403, "this credential may not write mainline history")

    lease = _lease_for(db, who)
    if lease is not None:
        if lease.submissions >= cfg.max_submissions_per_lease:
            raise HTTPException(429, "too many submissions under this lease")
        # Series cardinality is capped per pull request, not globally: a fork
        # can invent label values freely, and each new combination is a new
        # series that never merges back.
        if _would_exceed_series_cap(db, cfg, who, payload):
            raise HTTPException(
                429,
                f"pull request would exceed {cfg.max_series_per_pr} distinct "
                "series; check for unbounded label values",
            )

    accepted = duplicates = 0
    seen: set[str] = set()

    for m in payload.measurements:
        key = series_key(who.repo, m.metric, m.labels, m.role)
        seen.add(key)
        for i, value in enumerate(m.values):
            row = Measurement(
                repo=who.repo,
                series=key,
                metric=m.metric,
                unit=m.unit,
                direction=m.direction.value,
                labels=m.labels,
                value=value,
                rep=i,
                role=m.role.value,
                deterministic=m.deterministic,
                position=m.position.value if m.position else "",
                group=m.group or job,
                context=who.context.value,
                trust=who.trust.value,
                head_sha=head_sha,
                base_sha=payload.run.base_sha,
                merge_sha=who.merge_sha,
                ref=who.ref,
                pr=who.pr,
                workflow=who.workflow,
                job=job,
                runner=payload.run.runner,
                run_id=who.run_id,
                run_attempt=who.run_attempt,
            )
            # Reruns resubmit identical payloads. Absorb the collision per row
            # rather than failing the whole request.
            try:
                with db.begin_nested():
                    db.add(row)
                accepted += 1
            except IntegrityError:
                duplicates += 1

    if lease is not None:
        lease.submissions += 1

    db.commit()

    if who.context is Context.PR and who.pr is not None:
        _refresh_comment(db, cfg, gh, who.repo, who.pr, head_sha)

    return SubmissionOut(
        accepted=accepted,
        duplicates=duplicates,
        series=len(seen),
        context=who.context.value,
        trust=who.trust.value,
    )


def _lease_for(db: Session, who: Identity) -> Lease | None:
    if who.trust is not Trust.LEASE:
        return None
    return db.scalars(
        select(Lease).where(
            Lease.repo == who.repo,
            Lease.run_id == who.run_id,
            Lease.run_attempt == who.run_attempt,
            Lease.job == who.job,
        )
    ).one_or_none()


def _would_exceed_series_cap(
    db: Session, cfg: Settings, who: Identity, payload: SubmissionIn
) -> bool:
    existing = set(
        db.scalars(
            select(Measurement.series)
            .where(Measurement.repo == who.repo, Measurement.pr == who.pr)
            .distinct()
        ).all()
    )
    incoming = {
        series_key(who.repo, m.metric, m.labels, m.role) for m in payload.measurements
    }
    return len(existing | incoming) > cfg.max_series_per_pr


def _refresh_comment(
    db: Session,
    cfg: Settings,
    gh: GitHubApp | None,
    repo: str,
    pr: int,
    head_sha: str,
) -> None:
    """Rebuild and repost the report. Never fails the submission."""
    comparisons = reporting.build(db, repo, head_sha)
    progress = reporting.job_progress(db, repo, pr, head_sha)
    body = reporting.render(comparisons, head_sha, progress)

    record = db.scalars(
        select(Report).where(Report.repo == repo, Report.pr == pr)
    ).one_or_none()
    if record is None:
        record = Report(repo=repo, pr=pr)
        db.add(record)
    record.head_sha = head_sha
    record.method = reporting.METHOD
    record.body = {"markdown": body}

    if gh is not None:
        try:
            # Reuse the known id: far cheaper than scanning, and immune to the
            # comment falling off the first page of a long discussion.
            record.comment_id = gh.upsert_comment(
                repo, pr, body, comment_id=record.comment_id
            )
        except Exception:
            # Measurements are the durable artefact; the comment is a view of
            # them and can be regenerated. Losing it must not lose data.
            log.exception("failed to post report for %s#%s", repo, pr)

    db.commit()


@app.get("/v1/series")
def series(
    repo: str,
    head_sha: str,
    db: Annotated[Session, Depends(session)],
    cfg: Annotated[Settings, Depends(config)],
) -> dict:
    """Point-in-time view of one commit, with each series' baseline."""
    return {
        "repo": repo,
        "head_sha": head_sha,
        "series": [
            {
                "series": s.series,
                "metric": s.metric,
                "unit": s.unit,
                "labels": s.labels,
                "reps": s.reps,
                "baseline": baseline_points(db, s.series, cfg.baseline_window),
            }
            for s in head_series(db, repo, head_sha)
        ],
    }


@app.get("/v1/timeseries")
def timeseries(
    repo: str,
    series: str,
    db: Annotated[Session, Depends(session)],
    cfg: Annotated[Settings, Depends(config)],
    window: int | None = None,
) -> dict:
    """A series' history as points with one-sigma bars, oldest first.

    Shaped for a dashboard to draw `value` as a line and `value ± sigma` as a
    band. The bar is computed per point from that run's own repetitions and
    reference bracket, so a run measured on a misbehaving machine widens where
    it actually happened rather than smearing the whole series.
    """
    size = window or cfg.baseline_window
    points = run_points(db, repo, series, size)

    # Machine scatter is a property of the machine over the window, not of any
    # one run, so it is computed once and applied to every point.
    levels = [p.bracket.level for p in points if p.bracket]
    scatter = machine_scatter(levels)
    norm = statistics.median(levels) if levels else None

    out = []
    for point in points:
        drift = None
        if point.bracket and norm:
            drift = (point.bracket.level - norm) / abs(norm) * 100.0
        unc = estimate(
            point.reps,
            instability_pct=point.bracket.instability if point.bracket else None,
            machine_scatter=scatter,
            drift_pct=drift,
        )
        out.append(
            {
                "head_sha": point.head_sha,
                "timestamp": point.created_at.isoformat(),
                "value": unc.value,
                "sigma": unc.absolute,
                "lower": unc.value - unc.absolute,
                "upper": unc.value + unc.absolute,
                "reps": len(point.reps),
                "components": {
                    "repetition": unc.repetition,
                    "instability": unc.instability,
                    "machine": unc.machine,
                },
            }
        )

    return {
        "repo": repo,
        "series": series,
        "machine_scatter": scatter,
        "points": out,
    }


@app.get("/v1/report")
def report(
    repo: str,
    head_sha: str,
    db: Annotated[Session, Depends(session)],
    pr: int | None = None,
) -> dict:
    """Recompute a report without posting it -- useful for tuning the model."""
    comparisons = reporting.build(db, repo, head_sha)
    progress = (
        reporting.job_progress(db, repo, pr, head_sha) if pr is not None else None
    )
    return {
        "markdown": reporting.render(comparisons, head_sha, progress),
        "comparisons": [c.__dict__ for c in comparisons],
    }
