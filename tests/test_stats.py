from __future__ import annotations

import pytest

from deltaflow.models import Direction
from deltaflow.stats import MIN_BASELINE, compare, robust_sigma


def _compare(head, baseline, **kw):
    return compare("runtime", {}, head, baseline, **kw)


def test_insufficient_baseline_is_not_a_verdict():
    c = _compare([1.0], [1.0] * (MIN_BASELINE - 1))
    assert c.verdict == "insufficient-data"
    assert c.delta_pct is None


def test_stable_series_reports_unchanged():
    c = _compare([10.0], [10.0, 10.1, 9.9, 10.05, 9.95, 10.02])
    assert c.verdict == "unchanged"


def test_clear_slowdown_is_a_regression():
    c = _compare([20.0], [10.0, 10.1, 9.9, 10.05, 9.95, 10.02])
    assert c.verdict == "regressed"
    assert c.delta_pct == pytest.approx(99.0, abs=2.0)


def test_speedup_is_an_improvement_when_lower_is_better():
    c = _compare([5.0], [10.0, 10.1, 9.9, 10.05, 9.95, 10.02])
    assert c.verdict == "improved"


def test_direction_inverts_the_verdict():
    baseline = [10.0, 10.1, 9.9, 10.05, 9.95, 10.02]
    c = _compare([20.0], baseline, direction=Direction.HIGHER_BETTER)
    assert c.verdict == "improved"


def test_floor_suppresses_noise_on_deterministic_metrics():
    """Zero-variance series would otherwise flag every last-byte difference."""
    baseline = [1000.0] * 10
    assert _compare([1000.5], baseline).verdict == "unchanged"
    assert _compare([1100.0], baseline).verdict == "regressed"


def test_single_outlier_does_not_widen_the_band_enough_to_hide_a_regression():
    """The property that rules out a plain standard deviation."""
    baseline = [10.0, 10.1, 9.9, 10.05, 9.95, 10.02, 40.0]
    assert _compare([13.0], baseline).verdict == "regressed"


def test_robust_sigma_ignores_extremes():
    clean = [10.0, 10.1, 9.9, 10.05, 9.95, 10.02]
    assert robust_sigma(clean + [500.0]) < 1.0


def test_repetitions_are_reduced_by_median_not_mean():
    """One descheduled repetition must not drag the whole point."""
    baseline = [10.0, 10.1, 9.9, 10.05, 9.95, 10.02]
    c = _compare([10.0, 10.1, 60.0], baseline)
    assert c.verdict == "unchanged"


def test_zero_centred_baseline_is_refused_rather_than_dividing_by_zero():
    assert _compare([1.0], [0.0] * 10).verdict == "insufficient-data"
