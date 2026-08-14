from __future__ import annotations

import numpy as np
import pytest

from crypto_options.marketdata.chain import clean_option_chain
from crypto_options.research.synthetic import generate_synthetic_option_chain
from crypto_options.surface.svi import SVIParameters, calibrate_svi_surface


def test_svi_analytic_derivatives_match_finite_differences() -> None:
    parameters = SVIParameters(0.04, 0.12, -0.35, 0.03, 0.28)
    first_step = 1e-5
    second_step = 1e-4
    for k in np.linspace(-0.8, 0.8, 9):
        w = float(parameters.total_variance(k))
        first = (
            float(parameters.total_variance(k + first_step))
            - float(parameters.total_variance(k - first_step))
        ) / (2 * first_step)
        second = (
            float(parameters.total_variance(k + second_step))
            - 2 * w
            + float(parameters.total_variance(k - second_step))
        ) / second_step**2
        assert parameters.first_derivative(k) == pytest.approx(first, rel=1e-8, abs=1e-10)
        assert parameters.second_derivative(k) == pytest.approx(second, rel=2e-5, abs=1e-7)


@pytest.fixture(scope="module")
def calibrated_surface():
    points = clean_option_chain(generate_synthetic_option_chain(seed=17)).points
    return calibrate_svi_surface(points, strict=True)


def test_surface_is_static_arbitrage_safe(calibrated_surface) -> None:
    diagnostics = calibrated_surface.diagnostics()
    assert diagnostics.is_safe()
    assert diagnostics.butterfly_violations == 0
    assert diagnostics.calendar_violations == 0
    assert diagnostics.minimum_total_variance > 0


def test_surface_fits_observed_implied_vols(calibrated_surface) -> None:
    points = clean_option_chain(generate_synthetic_option_chain(seed=17)).points
    errors = [
        calibrated_surface.implied_volatility(point.time_to_expiry, point.strike, point.forward)
        - point.implied_volatility
        for point in points
    ]
    assert float(np.sqrt(np.mean(np.square(errors)))) < 5e-4


def test_total_variance_interpolation_is_linear_and_positive(calibrated_surface) -> None:
    lower, upper = calibrated_surface.slices[0:2]
    midpoint = 0.5 * (lower.time_to_expiry + upper.time_to_expiry)
    k = -0.2
    expected = 0.5 * (float(lower.total_variance(k)) + float(upper.total_variance(k)))
    assert calibrated_surface.total_variance(midpoint, k) == pytest.approx(expected)
    assert calibrated_surface.total_variance(midpoint, k) > 0
