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
import math
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


# A machine sweeping from `before` to `after` during the payload is modelled as
# uniform over that interval; a uniform distribution of width w has sigma
# w/sqrt(12). Cruder alternatives (half-range as 1 sigma) overstate it by ~1.7x.
_UNIFORM_TO_SIGMA = 1.0 / math.sqrt(12.0)


@dataclasses.dataclass(frozen=True)
class Uncertainty:
    """A one-sigma bar on a single measured point.

    Three independent contributions, added in quadrature:

    * `repetition` -- spread across repetitions within the job. What the
      benchmark itself could not pin down.
    * `instability` -- the machine moved *during* the measurement, so the
      payload could have landed anywhere across that sweep.
    * `machine` -- run-to-run machine variability, from the sliding window of
      reference levels, widened when today's level sits unusually far from it.

    On the third: a machine offset is strictly a bias, not a variance, and
    folding it into a symmetric bar is a simplification. It is the right one
    here because the quantity of interest is software cost, and normalising the
    payload by the reference is explicitly refused -- so a machine running 25%
    slow makes the point a 25%-worse estimate of the thing we actually want,
    which is exactly what a wider bar should say.
    """

    value: float
    repetition: float = 0.0
    instability: float = 0.0
    machine: float = 0.0

    @property
    def relative(self) -> float:
        """One sigma, as a fraction of the value."""
        return math.sqrt(
            self.repetition**2 + self.instability**2 + self.machine**2
        )

    @property
    def absolute(self) -> float:
        return abs(self.value) * self.relative

    @property
    def known(self) -> bool:
        return self.relative > 0.0


def repetition_sigma(reps: Sequence[float]) -> float:
    """Relative spread across repetitions.

    Deliberately *not* divided by sqrt(n). Repetitions inside one job share a
    machine state, so treating them as independent samples would understate the
    uncertainty -- often badly, since that shared state is the dominant term.
    """
    if len(reps) < 2:
        return 0.0
    centre = statistics.median(reps)
    if centre == 0:
        return 0.0
    spread = robust_sigma(reps) if len(reps) >= 4 else statistics.stdev(reps)
    return spread / abs(centre)


def estimate(
    reps: Sequence[float],
    instability_pct: float | None = None,
    machine_scatter: float = 0.0,
    drift_pct: float | None = None,
) -> Uncertainty:
    """Combine the measurement and both machine signals into one bar.

    `machine_scatter` is how much this machine normally varies run to run, as a
    fraction, taken from the sliding window of reference levels. `drift_pct` is
    how far it sits from that norm today; the larger of the two wins rather
    than both being counted, since they describe the same phenomenon.
    """
    value = aggregate(reps)
    instability = (
        (instability_pct / 100.0) * _UNIFORM_TO_SIGMA
        if instability_pct is not None
        else 0.0
    )
    machine = machine_scatter
    if drift_pct is not None:
        machine = max(machine_scatter, abs(drift_pct) / 100.0)

    return Uncertainty(
        value=value,
        repetition=repetition_sigma(reps),
        instability=instability,
        machine=machine,
    )


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
