"""Grafana JSON datasource endpoints.

Implements the contract the SimpleJSON-style datasources speak: `/`, `/search`,
`/query`, `/annotations`, `/tag-keys` and `/tag-values`. Grafana is a pure
rendering layer -- every aggregation, window and uncertainty estimate is
computed here, which is what keeps improved statistics applying retroactively
to all history.

Error bands: Grafana's JSON datasource has no notion of a point with a width,
so a band is drawn as two extra series. Requesting `seeding/runtime` yields
that target plus `seeding/runtime (upper)` and `seeding/runtime (lower)`, which
a panel joins with a "fill below to" series override.
"""

from __future__ import annotations

import datetime as dt
import statistics
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .deps import config, session
from .models import Measurement, Role
from .queries import machine_scatter, run_points
from .stats import estimate

router = APIRouter(prefix="/grafana", tags=["grafana"])

UPPER = " (upper)"
LOWER = " (lower)"


class TimeRange(BaseModel):
    from_: dt.datetime | None = Field(default=None, alias="from")
    to: dt.datetime | None = None

    model_config = {"populate_by_name": True}


class QueryTarget(BaseModel):
    target: str = ""
    refId: str = ""
    type: str = "timeseries"


class QueryRequest(BaseModel):
    targets: list[QueryTarget] = Field(default_factory=list)
    range: TimeRange | None = None
    maxDataPoints: int = 1000


class SearchRequest(BaseModel):
    target: str = ""


class AnnotationQuery(BaseModel):
    name: str = ""
    query: str = ""


class AnnotationRequest(BaseModel):
    range: TimeRange | None = None
    annotation: AnnotationQuery = Field(default_factory=AnnotationQuery)


def series_catalogue(db: Session, repo: str | None = None) -> dict[str, dict]:
    """Human-readable target name -> the series it identifies.

    Grafana users pick from a list of strings, so each series needs a stable,
    legible name. Labels are folded into it because they are what distinguishes
    one series from another.
    """
    stmt = select(
        Measurement.series,
        Measurement.repo,
        Measurement.metric,
        Measurement.unit,
        Measurement.labels,
    ).where(Measurement.role == Role.PAYLOAD.value)
    if repo:
        stmt = stmt.where(Measurement.repo == repo)

    out: dict[str, dict] = {}
    for key, series_repo, metric, unit, labels in db.execute(stmt.distinct()):
        parts = [f"{k}={v}" for k, v in sorted((labels or {}).items())]
        name = f"{series_repo}/{metric}"
        if parts:
            name += " {" + ", ".join(parts) + "}"
        out[name] = {
            "series": key,
            "repo": series_repo,
            "metric": metric,
            "unit": unit,
            "labels": labels or {},
        }
    return out


def _epoch_ms(when: dt.datetime) -> int:
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return int(when.timestamp() * 1000)


def _points_with_bars(
    db: Session, repo: str, series: str, window: int
) -> list[tuple[int, float, float]]:
    """(timestamp, value, sigma) for a series, oldest first."""
    points = run_points(db, repo, series, window)
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
        out.append((_epoch_ms(point.created_at), unc.value, unc.absolute))
    return out


def _within(when_ms: int, rng: TimeRange | None) -> bool:
    if rng is None:
        return True
    if rng.from_ and when_ms < _epoch_ms(rng.from_):
        return False
    if rng.to and when_ms > _epoch_ms(rng.to):
        return False
    return True


@router.get("/")
def health() -> dict[str, str]:
    """Grafana tests a datasource by GETting the root."""
    return {"status": "ok"}


@router.post("/search")
def search(
    body: SearchRequest,
    db: Annotated[Session, Depends(session)],
) -> list[str]:
    """Metric picker contents.

    Band series are offered explicitly so a panel can select them, but the
    plain target already returns them alongside its own data.
    """
    names = sorted(series_catalogue(db))
    if body.target:
        needle = body.target.lower()
        names = [n for n in names if needle in n.lower()]
    return names


@router.post("/query")
def query(
    body: QueryRequest,
    db: Annotated[Session, Depends(session)],
    cfg: Annotated[Settings, Depends(config)],
) -> list[dict[str, Any]]:
    catalogue = series_catalogue(db)
    response: list[dict[str, Any]] = []

    for target in body.targets:
        name = target.target
        # A panel may ask for a band series directly; serve it from the
        # same computation rather than recomputing per suffix.
        base = name
        want: str | None = None
        for suffix in (UPPER, LOWER):
            if name.endswith(suffix):
                base, want = name[: -len(suffix)], suffix
                break

        entry = catalogue.get(base)
        if entry is None:
            continue

        points = _points_with_bars(
            db, entry["repo"], entry["series"], cfg.baseline_window
        )
        points = [p for p in points if _within(p[0], body.range)]

        if target.type == "table":
            response.append(_as_table(base, entry, points))
            continue

        if want == UPPER:
            response.append(_as_series(name, [(t, v + s) for t, v, s in points]))
        elif want == LOWER:
            response.append(_as_series(name, [(t, v - s) for t, v, s in points]))
        else:
            response.append(_as_series(base, [(t, v) for t, v, _ in points]))
            # Only emit a band when something actually measured one.
            if any(s > 0 for _, _, s in points):
                response.append(
                    _as_series(base + UPPER, [(t, v + s) for t, v, s in points])
                )
                response.append(
                    _as_series(base + LOWER, [(t, v - s) for t, v, s in points])
                )

    return response


@router.post("/annotations")
def annotations(
    body: AnnotationRequest,
    db: Annotated[Session, Depends(session)],
    cfg: Annotated[Settings, Depends(config)],
) -> list[dict[str, Any]]:
    """Mark runs where the machine misbehaved.

    Far more useful on a dashboard than another line: it explains a step in
    the data that has nothing to do with the code.
    """
    from .reporting import UNSTABLE_PCT

    catalogue = series_catalogue(db)
    entry = catalogue.get(body.annotation.query)
    if entry is None:
        return []

    out = []
    for point in run_points(db, entry["repo"], entry["series"], cfg.baseline_window):
        if point.bracket and point.bracket.instability >= UNSTABLE_PCT:
            when = _epoch_ms(point.created_at)
            if not _within(when, body.range):
                continue
            out.append(
                {
                    "annotation": body.annotation.model_dump(),
                    "time": when,
                    "title": "Machine unstable",
                    "tags": ["machine"],
                    "text": (
                        f"Reference moved ±{point.bracket.instability:.1f}% "
                        f"during this run ({point.head_sha[:12]})"
                    ),
                }
            )
    return out


@router.post("/tag-keys")
def tag_keys(db: Annotated[Session, Depends(session)]) -> list[dict[str, str]]:
    keys: set[str] = set()
    for entry in series_catalogue(db).values():
        keys.update(entry["labels"])
    return [{"type": "string", "text": k} for k in sorted(keys)]


@router.post("/tag-values")
def tag_values(
    body: dict, db: Annotated[Session, Depends(session)]
) -> list[dict[str, str]]:
    key = body.get("key", "")
    values = {
        entry["labels"][key]
        for entry in series_catalogue(db).values()
        if key in entry["labels"]
    }
    return [{"text": v} for v in sorted(values)]


def _as_series(name: str, points: list[tuple[int, float]]) -> dict[str, Any]:
    # Grafana wants [value, timestamp], in that order.
    return {"target": name, "datapoints": [[v, t] for t, v in points]}


def _as_table(
    name: str, entry: dict, points: list[tuple[int, float, float]]
) -> dict[str, Any]:
    return {
        "type": "table",
        "columns": [
            {"text": "Time", "type": "time"},
            {"text": "Value", "type": "number"},
            {"text": "Sigma", "type": "number"},
            {"text": "Unit", "type": "string"},
        ],
        "rows": [[t, v, s, entry["unit"]] for t, v, s in points],
    }
