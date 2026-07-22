"""Regression detection.

This is v1 and it is deliberately crude: a robust location estimate plus an IQR
spread, with a relative floor so that quiet metrics do not fire on rounding. It
is *not* the final model, and the design assumes that -- raw repetitions are
stored, so a better method applies retroactively to all history.

Two properties are non-negotiable even in v1:

* Robust to outliers. A single CI hiccup in the baseline window must not widen
  the band enough to hide a real regression, nor narrow it into false alarms.
* Never assume normality for runtime. Timing noise is right-skewed and one-sided
  -- a slow run is always possible, a negatively-slow one is not.
"""

from __future__ import annotations

import dataclasses
import statistics
from collections.abc import Sequence

from .models import Direction

METHOD = "iqr-v1"

# Multiples of a robust sigma before a change is called. 3.0 is a starting
# guess; it wants tuning against real history before anyone trusts it.
DEFAULT_K = 3.0

# Nothing below this relative change is ever reported, however tight the band.
# Deterministic metrics have zero spread, so without a floor every 1-byte
# allocation difference becomes a regression.
DEFAULT_FLOOR_PCT = 1.0

# Below this many baseline points the spread estimate is meaningless.
MIN_BASELINE = 5

_IQR_TO_SIGMA = 0.7413  # 1 / (2 * Phi^-1(0.75)), the normal-consistent scaling


@dataclasses.dataclass(frozen=True)
class Comparison:
    metric: str
    labels: dict[str, str]
    head: float
    baseline: float | None
    delta_pct: float | None
    threshold_pct: float | None
    n_baseline: int
    verdict: str  # regressed | improved | unchanged | insufficient-data

    @property
    def notable(self) -> bool:
        return self.verdict in ("regressed", "improved")


def aggregate(reps: Sequence[float]) -> float:
    """Collapse one job's repetitions into a single point.

    Median, not mean: a job that got descheduled once should not drag the whole
    point. Minimum is arguably better for pure runtime -- noise only ever adds
    time -- but it is fragile when repetition counts vary between runs, so the
    median is the safer default until the data says otherwise.
    """
    return statistics.median(reps)


def robust_sigma(values: Sequence[float]) -> float:
    """Spread estimate that survives outliers, unlike the standard deviation."""
    if len(values) < 4:
        return 0.0
    quantiles = statistics.quantiles(values, n=4, method="inclusive")
    return (quantiles[2] - quantiles[0]) * _IQR_TO_SIGMA


def compare(
    metric: str,
    labels: dict[str, str],
    head_reps: Sequence[float],
    baseline_points: Sequence[float],
    direction: Direction = Direction.LOWER_BETTER,
    k: float = DEFAULT_K,
    floor_pct: float = DEFAULT_FLOOR_PCT,
) -> Comparison:
    """Compare one series' head measurement against its recent baseline.

    `baseline_points` are already-aggregated per-run values, newest last.
    """
    head = aggregate(head_reps)

    if len(baseline_points) < MIN_BASELINE:
        return Comparison(
            metric, labels, head, None, None, None, len(baseline_points),
            "insufficient-data",
        )

    centre = statistics.median(baseline_points)
    if centre == 0:
        return Comparison(
            metric, labels, head, centre, None, None, len(baseline_points),
            "insufficient-data",
        )

    sigma = robust_sigma(baseline_points)
    delta_pct = (head - centre) / abs(centre) * 100.0

    # The band is whichever is wider: the observed noise, or the floor.
    band_pct = max(k * sigma / abs(centre) * 100.0, floor_pct)

    if abs(delta_pct) <= band_pct:
        verdict = "unchanged"
    elif (delta_pct > 0) == (direction is Direction.LOWER_BETTER):
        verdict = "regressed"
    else:
        verdict = "improved"

    return Comparison(
        metric, labels, head, centre, delta_pct, band_pct,
        len(baseline_points), verdict,
    )
