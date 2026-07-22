"""Read paths.

Repetitions are collapsed into per-run points here rather than in SQL, because
the aggregation rule is part of the statistical method and is expected to change
(median today, possibly minimum for runtime tomorrow). Keeping it in Python
means changing it does not mean rewriting queries.
"""

from __future__ import annotations

import dataclasses
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Context, Direction, Measurement
from .stats import aggregate


@dataclasses.dataclass
class SeriesReps:
    series: str
    metric: str
    unit: str
    direction: Direction
    labels: dict[str, str]
    reps: list[float]


def head_series(session: Session, repo: str, head_sha: str) -> list[SeriesReps]:
    """Every series measured at a given commit, with all repetitions."""
    rows = session.scalars(
        select(Measurement)
        .where(Measurement.repo == repo, Measurement.head_sha == head_sha)
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
            )
        entry.reps.append(row.value)
    return list(grouped.values())


def baseline_points(
    session: Session, series: str, window: int, before_sha: str | None = None
) -> list[float]:
    """Recent mainline history for a series, one point per run, oldest first.

    Only MAINLINE rows are eligible. Pull request measurements -- including
    anything forwarded from a fork -- can never influence a baseline.
    """
    stmt = (
        select(Measurement)
        .where(
            Measurement.series == series,
            Measurement.context == Context.MAINLINE.value,
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
