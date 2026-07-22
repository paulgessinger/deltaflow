"""Database schema.

One row per *repetition*, never per aggregate. Aggregation happens at read time
so that improved statistical methods apply retroactively to all history.
"""

from __future__ import annotations

import datetime as dt
import enum
import hashlib
import json

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Context(str, enum.Enum):
    """Which series a measurement belongs to.

    Measurements from pull requests must never enter the mainline baseline, or a
    fork PR could poison every future comparison.
    """

    MAINLINE = "mainline"
    PR = "pr"


class Trust(str, enum.Enum):
    """How the submitter's identity was established.

    OIDC claims are cryptographically attested by GitHub. TOKEN is a scoped
    pre-shared secret, for submitters that are not GitHub Actions jobs at all.
    LEASE is the uncredentialed fork path: identity is inferred by confirming
    with GitHub that the claimed job is genuinely running right now, which is
    weaker and is therefore barred from mainline.
    """

    OIDC = "oidc"
    TOKEN = "token"
    LEASE = "lease"

    @property
    def mainline_eligible(self) -> bool:
        return self is not Trust.LEASE


class Direction(str, enum.Enum):
    LOWER_BETTER = "lower_better"
    HIGHER_BETTER = "higher_better"


class Role(str, enum.Enum):
    """What a measurement is for.

    A REFERENCE is a short, fixed workload run either side of the payload to
    quantify how much the machine moved while the real benchmark was running.
    It is never used to normalise the payload -- only to say how much the
    measurement should be trusted.
    """

    PAYLOAD = "payload"
    REFERENCE = "reference"


class Position(str, enum.Enum):
    """Which half of a reference bracket a sample came from."""

    BEFORE = "before"
    AFTER = "after"


def series_key(
    repo: str, metric: str, labels: dict[str, str], role: Role = Role.PAYLOAD
) -> str:
    """Stable identity for "the same measurement over time".

    Grouping by a JSON column does not index; this hash does. Label order is
    normalised so that submitters cannot accidentally fork a series.

    Role participates in identity so a reference can never collide with a
    payload. Position deliberately does not: the two halves of a bracket are
    two samples of one reference series, not two series.
    """
    canonical = json.dumps(
        {"repo": repo, "metric": metric, "labels": labels, "role": role.value},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


class Measurement(Base):
    __tablename__ = "measurement"

    id: Mapped[int] = mapped_column(primary_key=True)

    # --- series identity -------------------------------------------------
    repo: Mapped[str] = mapped_column(String(255), index=True)
    series: Mapped[str] = mapped_column(String(32), index=True)
    metric: Mapped[str] = mapped_column(String(255))
    unit: Mapped[str] = mapped_column(String(32))
    direction: Mapped[Direction] = mapped_column(String(16))
    labels: Mapped[dict] = mapped_column(JSON, default=dict)

    # --- the number ------------------------------------------------------
    value: Mapped[float] = mapped_column(Float)
    rep: Mapped[int] = mapped_column(Integer, default=0)

    # --- reference bracketing --------------------------------------------
    role: Mapped[Role] = mapped_column(String(16), default=Role.PAYLOAD, index=True)
    # Machine variation is excluded from this metric's uncertainty.
    deterministic: Mapped[bool] = mapped_column(default=False)
    # Empty string, never NULL: this column is part of the dedup constraint,
    # and SQL treats NULLs as distinct from one another, so a nullable column
    # here would silently stop every payload row from deduplicating.
    position: Mapped[str] = mapped_column(String(8), default="")
    # Ties a bracket to the payload it surrounds. Defaults to the job, which is
    # right when a job runs one benchmark; set it explicitly when a job runs
    # several and each gets its own bracket.
    group: Mapped[str] = mapped_column(String(255), default="")

    # --- provenance ------------------------------------------------------
    context: Mapped[Context] = mapped_column(String(16), index=True)
    trust: Mapped[Trust] = mapped_column(String(16))

    # head_sha is client-supplied: the OIDC `sha` claim on a pull_request event
    # is the ephemeral merge commit, which disappears when the base moves.
    head_sha: Mapped[str] = mapped_column(String(40), index=True)
    base_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    merge_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pr: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    workflow: Mapped[str | None] = mapped_column(String(255), nullable=True)
    job: Mapped[str | None] = mapped_column(String(255), nullable=True)
    runner: Mapped[str | None] = mapped_column(String(255), nullable=True)
    run_id: Mapped[str] = mapped_column(String(32))
    run_attempt: Mapped[int] = mapped_column(Integer, default=1)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC), index=True
    )

    __table_args__ = (
        # CI reruns resubmit identical payloads; the second write must be a
        # no-op. head_sha belongs in the key: one run legitimately measures more
        # than one commit (a bisect, a matrix over refs), and without it those
        # measurements silently annihilate each other as false duplicates.
        UniqueConstraint(
            "run_id",
            "run_attempt",
            "job",
            "series",
            "rep",
            "head_sha",
            "position",
            "group",
            name="uq_measurement_dedup",
        ),
        # The query every read path makes: one series' history, newest first.
        Index("ix_series_history", "series", "context", "created_at"),
    )


class Lease(Base):
    """A benchmark job's claim on a submission slot.

    Held for the lifetime of one job in one run attempt. The unique constraint
    is the whole point: while a lease is held, nothing else can submit under
    that identity, and a would-be impostor's claim fails loudly rather than
    silently succeeding.
    """

    __tablename__ = "lease"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo: Mapped[str] = mapped_column(String(255), index=True)
    run_id: Mapped[str] = mapped_column(String(32))
    run_attempt: Mapped[int] = mapped_column(Integer, default=1)
    job: Mapped[str] = mapped_column(String(255))

    # Verified against GitHub at claim time, then frozen. Submissions under this
    # lease cannot address any other pull request or commit.
    pr: Mapped[int] = mapped_column(Integer, index=True)
    head_sha: Mapped[str] = mapped_column(String(40))

    secret_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    submissions: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC)
    )

    __table_args__ = (
        UniqueConstraint("repo", "run_id", "run_attempt", "job", name="uq_lease_slot"),
    )


class ApiToken(Base):
    """Scoped credential for submitters outside GitHub Actions.

    A bare-metal reference machine on a cron has no OIDC to offer, and is
    likely the source of the most trustworthy runtime numbers, so this path is
    mainline-eligible. Only the hash is stored.
    """

    __tablename__ = "api_token"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    repo: Mapped[str] = mapped_column(String(255), index=True)
    secret_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    revoked: Mapped[bool] = mapped_column(default=False)
    last_used_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC)
    )


class Report(Base):
    """A verdict as it was posted.

    Uncertainty is computed at read time, which means a comparison rerun months
    later will not reproduce today's answer. Snapshotting the verdict plus the
    method version keeps past decisions auditable without freezing the method.
    """

    __tablename__ = "report"

    id: Mapped[int] = mapped_column(primary_key=True)
    repo: Mapped[str] = mapped_column(String(255), index=True)
    pr: Mapped[int] = mapped_column(Integer, index=True)
    head_sha: Mapped[str] = mapped_column(String(40))
    method: Mapped[str] = mapped_column(String(64))
    body: Mapped[dict] = mapped_column(JSON)
    comment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.UTC)
    )

    __table_args__ = (UniqueConstraint("repo", "pr", name="uq_report_pr"),)


class Comparison(Base):
    """Per-series detail backing a Report, kept for auditing false positives."""

    __tablename__ = "comparison"

    id: Mapped[int] = mapped_column(primary_key=True)
    report_id: Mapped[int] = mapped_column(ForeignKey("report.id"), index=True)
    series: Mapped[str] = mapped_column(String(32))
    metric: Mapped[str] = mapped_column(String(255))
    labels: Mapped[dict] = mapped_column(JSON)
    head_value: Mapped[float] = mapped_column(Float)
    baseline_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    delta_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    threshold_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    verdict: Mapped[str] = mapped_column(String(16))
    n_baseline: Mapped[int] = mapped_column(Integer, default=0)
