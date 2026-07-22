"""Read paths.

Repetitions are collapsed into per-run points here rather than in SQL, because
the aggregation rule is part of the statistical method and is expected to change
(median today, possibly minimum for runtime tomorrow). Keeping it in Python
means changing it does not mean rewriting queries.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import statistics
from collections import defaultdict
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Context, Direction, Measurement, Position, Role
from .stats import aggregate, robust_sigma


@dataclasses.dataclass
class Bracket:
    """What a pair of reference runs says about the machine.

    The useful property is that `instability` needs no history at all: it is
    the machine disagreeing with itself minutes apart, measured within a single
    run. A variation estimate is therefore available from the very first
    submission, before any baseline exists -- and on a quiet machine it simply
    collapses toward zero without any code needing to change.
    """

    before: float
    after: float
    series: str = ""

    @property
    def level(self) -> float:
        """Machine speed during the payload, in reference units."""
        return (self.before + self.after) / 2.0

    @property
    def instability(self) -> float:
        """Relative movement across the payload, as a percentage."""
        level = self.level
        if level == 0:
            return 0.0
        return abs(self.after - self.before) / abs(level) * 100.0


def brackets(
    session: Session, repo: str, head_sha: str
) -> dict[tuple[str, str], Bracket]:
    """Reference brackets for one commit, keyed by (job, group).

    A bracket needs both halves. A job whose reference only ran once -- because
    the payload crashed, or the after-run was skipped -- yields nothing rather
    than a fabricated estimate.
    """
    rows = session.scalars(
        select(Measurement).where(
            Measurement.repo == repo,
            Measurement.head_sha == head_sha,
            Measurement.role == Role.REFERENCE.value,
        )
    ).all()

    halves: dict[tuple[str, str], dict[str, list[float]]] = defaultdict(
        lambda: {"before": [], "after": []}
    )
    series_of: dict[tuple[str, str], str] = {}
    for row in rows:
        if not row.position:
            continue
        key = (row.job or "", row.group or row.job or "")
        halves[key][row.position].append(row.value)
        series_of[key] = row.series

    out: dict[tuple[str, str], Bracket] = {}
    for key, sides in halves.items():
        if sides[Position.BEFORE.value] and sides[Position.AFTER.value]:
            out[key] = Bracket(
                before=aggregate(sides[Position.BEFORE.value]),
                after=aggregate(sides[Position.AFTER.value]),
                series=series_of[key],
            )
    return out


def machine_scatter(levels: Sequence[float]) -> float:
    """How much this machine normally varies run to run, as a fraction.

    Derived from the sliding window of reference levels. This is the floor on
    any point's uncertainty: even a perfectly stable measurement on a machine
    that wanders 4% between runs is only known to 4% as an estimate of software
    cost.
    """
    if len(levels) < 4:
        return 0.0
    centre = statistics.median(levels)
    if centre == 0:
        return 0.0
    return robust_sigma(levels) / abs(centre)


@dataclasses.dataclass
class RunPoint:
    """One run's contribution to a series, with the machine state around it."""

    head_sha: str
    created_at: dt.datetime
    reps: list[float]
    job: str
    group: str
    bracket: Bracket | None = None


def run_points(
    session: Session,
    repo: str,
    series: str,
    window: int,
    context: Context = Context.MAINLINE,
    before_sha: str | None = None,
) -> list[RunPoint]:
    """A series' recent history as individual runs, oldest first.

    Unlike `baseline_points` this keeps each run's repetitions and attaches its
    reference bracket, so every historical point can carry its own error bar --
    which is what a dashboard needs to draw a band rather than a bare line.
    """
    rows = session.scalars(
        select(Measurement)
        .where(
            Measurement.series == series,
            Measurement.context == context.value,
            Measurement.role == Role.PAYLOAD.value,
        )
        .order_by(Measurement.created_at.desc())
        .limit(window * 200)
    ).all()

    points: dict[tuple, RunPoint] = {}
    order: list[tuple] = []
    for row in rows:
        if before_sha and row.head_sha == before_sha:
            continue
        key = (row.run_id, row.run_attempt, row.job)
        if key not in points:
            if len(order) >= window:
                break
            order.append(key)
            points[key] = RunPoint(
                head_sha=row.head_sha,
                created_at=row.created_at,
                reps=[],
                job=row.job or "",
                group=row.group or row.job or "",
            )
        points[key].reps.append(row.value)

    _attach_brackets(session, repo, order, points)

    ordered = [points[k] for k in order]
    ordered.reverse()  # oldest first
    return ordered


def _attach_brackets(
    session: Session, repo: str, order: list[tuple], points: dict[tuple, RunPoint]
) -> None:
    """Pair each run with the reference bracket recorded around it."""
    if not order:
        return
    run_ids = {key[0] for key in order}
    refs = session.scalars(
        select(Measurement).where(
            Measurement.repo == repo,
            Measurement.role == Role.REFERENCE.value,
            Measurement.run_id.in_(run_ids),
        )
    ).all()

    halves: dict[tuple, dict[str, list[float]]] = defaultdict(
        lambda: {"before": [], "after": []}
    )
    series_of: dict[tuple, str] = {}
    for row in refs:
        if not row.position:
            continue
        key = (row.run_id, row.run_attempt, row.job or "")
        halves[key][row.position].append(row.value)
        series_of[key] = row.series

    for key in order:
        sides = halves.get(key)
        if not sides:
            continue
        if sides[Position.BEFORE.value] and sides[Position.AFTER.value]:
            points[key].bracket = Bracket(
                before=aggregate(sides[Position.BEFORE.value]),
                after=aggregate(sides[Position.AFTER.value]),
                series=series_of[key],
            )


@dataclasses.dataclass
class SeriesReps:
    series: str
    metric: str
    unit: str
    direction: Direction
    labels: dict[str, str]
    reps: list[float]
    job: str = ""
    group: str = ""
    deterministic: bool = False


def head_series(session: Session, repo: str, head_sha: str) -> list[SeriesReps]:
    """Every payload series measured at a given commit, with all repetitions.

    References are excluded: they describe the machine, not the software, and
    reporting them alongside the payload would invite exactly the comparison
    the design refuses to make.
    """
    rows = session.scalars(
        select(Measurement)
        .where(
            Measurement.repo == repo,
            Measurement.head_sha == head_sha,
            Measurement.role == Role.PAYLOAD.value,
        )
        .order_by(Measurement.series, Measurement.rep)
    ).all()

    grouped: dict[str, SeriesReps] = {}
    for row in rows:
        entry = grouped.get(row.series)
        if entry is None:
            entry = grouped[row.series] = SeriesReps(
                series=row.series,
                metric=row.metric,
                unit=row.unit,
                direction=Direction(row.direction),
                labels=row.labels or {},
                reps=[],
                job=row.job or "",
                group=row.group or row.job or "",
                deterministic=bool(row.deterministic),
            )
        entry.reps.append(row.value)
    return list(grouped.values())


def baseline_points(
    session: Session,
    series: str,
    window: int,
    before_sha: str | None = None,
    role: Role = Role.PAYLOAD,
) -> list[float]:
    """Recent mainline history for a series, one point per run, oldest first.

    Only MAINLINE rows are eligible. Pull request measurements -- including
    anything forwarded from a fork -- can never influence a baseline.

    Passing `role=REFERENCE` yields the machine's own history. The per-run
    aggregate of a reference series is exactly its bracket level, so this is
    the drift signal for free: a step in it means the hardware changed
    underneath, not that anything in the repository did.
    """
    stmt = (
        select(Measurement)
        .where(
            Measurement.series == series,
            Measurement.context == Context.MAINLINE.value,
            Measurement.role == role.value,
        )
        .order_by(Measurement.created_at.desc())
        # Generous row cap: `window` runs' worth of repetitions, not of rows.
        .limit(window * 200)
    )
    rows = session.scalars(stmt).all()

    per_run: dict[tuple, list[float]] = defaultdict(list)
    order: list[tuple] = []
    for row in rows:
        if before_sha and row.head_sha == before_sha:
            continue
        key = (row.run_id, row.run_attempt, row.job)
        if key not in per_run:
            order.append(key)
        per_run[key].append(row.value)
        if len(order) > window:
            break

    points = [aggregate(per_run[k]) for k in order[:window]]
    points.reverse()  # oldest first, so callers can reason about drift
    return points
