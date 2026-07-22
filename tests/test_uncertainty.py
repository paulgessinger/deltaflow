"""The one-sigma bar: what goes into it, and what must not."""

from __future__ import annotations

import math

import pytest

from deltaflow.queries import machine_scatter
from deltaflow.stats import Uncertainty, estimate, repetition_sigma

STEADY = [10.0, 10.0, 10.0, 10.0]


def test_a_perfect_measurement_on_a_perfect_machine_has_no_bar():
    assert not estimate(STEADY, instability_pct=0.0).known


def test_components_add_in_quadrature():
    u = Uncertainty(value=10.0, repetition=0.03, instability=0.04, machine=0.12)
    assert u.relative == pytest.approx(math.sqrt(0.03**2 + 0.04**2 + 0.12**2))
    assert u.absolute == pytest.approx(10.0 * u.relative)


def test_absolute_bar_is_in_the_metric_s_own_units():
    u = estimate([100.0, 100.0, 100.0, 100.0], instability_pct=10.0)
    assert u.value == 100.0
    assert u.absolute == pytest.approx(100.0 * u.relative)


def test_instability_widens_the_bar():
    calm = estimate(STEADY, instability_pct=0.0)
    rough = estimate(STEADY, instability_pct=20.0)
    assert rough.relative > calm.relative


def test_instability_is_treated_as_a_uniform_sweep_not_a_full_sigma():
    """A machine sweeping across a range is not one sigma wide at its edges."""
    u = estimate(STEADY, instability_pct=10.0)
    assert u.instability == pytest.approx(0.10 / math.sqrt(12), abs=1e-6)


def test_machine_scatter_sets_a_floor_even_on_a_flawless_measurement():
    """A steady reading on a wandering machine is still a poor estimate of cost."""
    u = estimate(STEADY, instability_pct=0.0, machine_scatter=0.04)
    assert u.repetition == 0.0
    assert u.relative == pytest.approx(0.04)


def test_drift_and_scatter_do_not_double_count():
    """Both describe machine variability; the larger wins."""
    u = estimate(STEADY, machine_scatter=0.04, drift_pct=25.0)
    assert u.machine == pytest.approx(0.25)

    u = estimate(STEADY, machine_scatter=0.30, drift_pct=2.0)
    assert u.machine == pytest.approx(0.30)


def test_repetition_spread_is_not_divided_by_root_n():
    """Repetitions share a machine state, so they are not independent samples."""
    reps = [10.0, 10.5, 9.5, 10.2, 9.8]
    sigma = repetition_sigma(reps)
    naive_sem = sigma / math.sqrt(len(reps))
    assert sigma > naive_sem


def test_single_repetition_contributes_no_spread():
    assert repetition_sigma([10.0]) == 0.0


def test_repetition_spread_survives_one_bad_repetition():
    clean = repetition_sigma([10.0, 10.1, 9.9, 10.05, 10.02])
    with_outlier = repetition_sigma([10.0, 10.1, 9.9, 10.05, 10.02, 90.0])
    assert with_outlier < clean * 4


# --- machine scatter over the sliding window --------------------------------


def test_scatter_of_a_steady_machine_is_zero():
    assert machine_scatter([10.0] * 20) == 0.0


def test_scatter_grows_with_a_wandering_machine():
    steady = machine_scatter([10.0, 10.1, 9.9, 10.05, 9.95, 10.02])
    wandering = machine_scatter([10.0, 12.0, 8.0, 11.0, 9.0, 13.0])
    assert wandering > steady


def test_scatter_needs_a_few_points_before_it_means_anything():
    assert machine_scatter([10.0, 20.0]) == 0.0


def test_scatter_is_relative_so_units_do_not_matter():
    seconds = machine_scatter([10.0, 11.0, 9.0, 10.5, 9.5, 10.2])
    millis = machine_scatter([v * 1000 for v in [10.0, 11.0, 9.0, 10.5, 9.5, 10.2]])
    assert seconds == pytest.approx(millis)
