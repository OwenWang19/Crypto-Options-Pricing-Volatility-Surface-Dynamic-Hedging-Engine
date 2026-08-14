from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from math import isfinite, log, sqrt

import numpy as np
from numpy.typing import NDArray
from scipy.optimize import least_squares

from crypto_options.marketdata.chain import CalibrationPoint

FloatArray = NDArray[np.float64]


class SurfaceCalibrationError(RuntimeError):
    """Raised when a usable, diagnostically valid surface cannot be calibrated."""


@dataclass(frozen=True, slots=True)
class SVIParameters:
    """Raw-SVI parameters for total variance w(k)."""

    a: float
    b: float
    rho: float
    m: float
    sigma: float

    def __post_init__(self) -> None:
        if not all(isfinite(value) for value in (self.a, self.b, self.rho, self.m, self.sigma)):
            raise ValueError("SVI parameters must be finite")
        if self.b < 0 or self.sigma <= 0 or abs(self.rho) >= 1:
            raise ValueError("SVI requires b >= 0, sigma > 0, and |rho| < 1")

    @property
    def minimum_total_variance(self) -> float:
        return self.a + self.b * self.sigma * sqrt(1.0 - self.rho * self.rho)

    def total_variance(self, log_moneyness: float | FloatArray) -> float | FloatArray:
        k = np.asarray(log_moneyness, dtype=float)
        x = k - self.m
        result = self.a + self.b * (self.rho * x + np.sqrt(x * x + self.sigma**2))
        return float(result) if result.ndim == 0 else result

    def first_derivative(self, log_moneyness: float | FloatArray) -> float | FloatArray:
        k = np.asarray(log_moneyness, dtype=float)
        x = k - self.m
        root = np.sqrt(x * x + self.sigma**2)
        result = self.b * (self.rho + x / root)
        return float(result) if result.ndim == 0 else result

    def second_derivative(self, log_moneyness: float | FloatArray) -> float | FloatArray:
        k = np.asarray(log_moneyness, dtype=float)
        x = k - self.m
        result = self.b * self.sigma**2 / (x * x + self.sigma**2) ** 1.5
        return float(result) if result.ndim == 0 else result

    def butterfly_density_factor(self, log_moneyness: float | FloatArray) -> float | FloatArray:
        """Gatheral-Jacquier g(k); negative values imply butterfly arbitrage."""

        k = np.asarray(log_moneyness, dtype=float)
        w = np.maximum(np.asarray(self.total_variance(k)), 1e-14)
        wp = np.asarray(self.first_derivative(k))
        wpp = np.asarray(self.second_derivative(k))
        result = (1.0 - k * wp / (2.0 * w)) ** 2
        result -= (wp * wp / 4.0) * (1.0 / w + 0.25)
        result += 0.5 * wpp
        return float(result) if result.ndim == 0 else result


@dataclass(frozen=True, slots=True)
class SVISlice:
    time_to_expiry: float
    forward: float
    parameters: SVIParameters
    weighted_rmse: float
    max_abs_total_variance_error: float
    observations: int

    def total_variance(self, log_moneyness: float | FloatArray) -> float | FloatArray:
        return self.parameters.total_variance(log_moneyness)

    def implied_volatility(self, strike: float) -> float:
        if strike <= 0:
            raise ValueError("strike must be positive")
        variance = float(self.total_variance(log(strike / self.forward)))
        if variance <= 0:
            raise SurfaceCalibrationError("surface returned non-positive total variance")
        return sqrt(variance / self.time_to_expiry)


@dataclass(frozen=True, slots=True)
class ArbitrageDiagnostics:
    minimum_total_variance: float
    minimum_butterfly_density: float
    minimum_calendar_spread: float
    butterfly_violations: int
    calendar_violations: int
    grid_points: int

    def is_safe(self, tolerance: float = 1e-8) -> bool:
        return (
            self.minimum_total_variance >= -tolerance
            and self.minimum_butterfly_density >= -tolerance
            and self.minimum_calendar_spread >= -tolerance
        )


@dataclass(frozen=True, slots=True)
class SVISurface:
    slices: tuple[SVISlice, ...]

    def __post_init__(self) -> None:
        if not self.slices:
            raise ValueError("surface requires at least one slice")
        maturities = [item.time_to_expiry for item in self.slices]
        if any(t <= 0 for t in maturities) or maturities != sorted(set(maturities)):
            raise ValueError("surface maturities must be positive, unique, and sorted")

    def total_variance(self, time_to_expiry: float, log_moneyness: float) -> float:
        if time_to_expiry <= 0:
            raise ValueError("time_to_expiry must be positive")
        maturities = np.array([item.time_to_expiry for item in self.slices])
        if time_to_expiry <= maturities[0]:
            first = float(self.slices[0].total_variance(log_moneyness))
            return first * time_to_expiry / maturities[0]
        if time_to_expiry >= maturities[-1]:
            last = float(self.slices[-1].total_variance(log_moneyness))
            return last * time_to_expiry / maturities[-1]
        upper_index = int(np.searchsorted(maturities, time_to_expiry))
        lower_slice = self.slices[upper_index - 1]
        upper_slice = self.slices[upper_index]
        alpha = (time_to_expiry - lower_slice.time_to_expiry) / (
            upper_slice.time_to_expiry - lower_slice.time_to_expiry
        )
        lower_w = float(lower_slice.total_variance(log_moneyness))
        upper_w = float(upper_slice.total_variance(log_moneyness))
        return (1.0 - alpha) * lower_w + alpha * upper_w

    def implied_volatility(self, time_to_expiry: float, strike: float, forward: float) -> float:
        if strike <= 0 or forward <= 0:
            raise ValueError("strike and forward must be positive")
        total_variance = self.total_variance(time_to_expiry, log(strike / forward))
        if total_variance <= 0:
            raise SurfaceCalibrationError("surface returned non-positive total variance")
        return sqrt(total_variance / time_to_expiry)

    def diagnostics(
        self,
        *,
        minimum_log_moneyness: float = -1.5,
        maximum_log_moneyness: float = 1.5,
        grid_points: int = 401,
        tolerance: float = 1e-10,
    ) -> ArbitrageDiagnostics:
        if grid_points < 11 or minimum_log_moneyness >= maximum_log_moneyness:
            raise ValueError("invalid diagnostic grid")
        grid = np.linspace(minimum_log_moneyness, maximum_log_moneyness, grid_points)
        minimum_w = float("inf")
        minimum_g = float("inf")
        butterfly_violations = 0
        calendar_violations = 0
        minimum_calendar = float("inf")
        previous_w: FloatArray | None = None

        for svi_slice in self.slices:
            w = np.asarray(svi_slice.total_variance(grid))
            g = np.asarray(svi_slice.parameters.butterfly_density_factor(grid))
            minimum_w = min(
                minimum_w,
                float(np.min(w)),
                svi_slice.parameters.minimum_total_variance,
            )
            minimum_g = min(minimum_g, float(np.min(g)))
            butterfly_violations += int(np.count_nonzero(g < -tolerance))
            if previous_w is not None:
                calendar_spread = w - previous_w
                minimum_calendar = min(minimum_calendar, float(np.min(calendar_spread)))
                calendar_violations += int(np.count_nonzero(calendar_spread < -tolerance))
            previous_w = w
        if len(self.slices) == 1:
            minimum_calendar = 0.0
        return ArbitrageDiagnostics(
            minimum_total_variance=minimum_w,
            minimum_butterfly_density=minimum_g,
            minimum_calendar_spread=minimum_calendar,
            butterfly_violations=butterfly_violations,
            calendar_violations=calendar_violations,
            grid_points=grid_points,
        )

    def assert_arbitrage_safe(self, tolerance: float = 1e-8) -> None:
        diagnostics = self.diagnostics(tolerance=tolerance)
        if not diagnostics.is_safe(tolerance):
            raise SurfaceCalibrationError(f"static-arbitrage diagnostics failed: {diagnostics}")


def _aggregate_points(
    points: list[CalibrationPoint],
) -> tuple[FloatArray, FloatArray, FloatArray]:
    grouped: dict[float, list[CalibrationPoint]] = {}
    for point in points:
        grouped.setdefault(round(point.log_moneyness, 12), []).append(point)
    k_values, w_values, weights = [], [], []
    for k, rows in sorted(grouped.items()):
        row_weights = np.array([row.weight for row in rows])
        row_variances = np.array([row.total_variance for row in rows])
        k_values.append(k)
        w_values.append(float(np.average(row_variances, weights=row_weights)))
        weights.append(float(np.sum(row_weights)))
    return (
        np.asarray(k_values, dtype=float),
        np.asarray(w_values, dtype=float),
        np.asarray(weights, dtype=float),
    )


def _parameters_from_vector(vector: FloatArray) -> SVIParameters:
    return SVIParameters(*(float(value) for value in vector))


def _calibrate_slice(
    points: list[CalibrationPoint],
    *,
    lower_slice: SVISlice | None,
    penalty_strength: float,
) -> SVISlice:
    k, observed_w, weights = _aggregate_points(points)
    if len(k) < 5:
        raise SurfaceCalibrationError("each maturity needs at least five distinct strikes")
    maturity = points[0].time_to_expiry
    forward = float(np.median([point.forward for point in points]))
    if max(abs(point.time_to_expiry - maturity) for point in points) > 1e-10:
        raise SurfaceCalibrationError("slice contains mixed maturities")
    if max(abs(point.forward / forward - 1.0) for point in points) > 0.01:
        raise SurfaceCalibrationError("slice forward inputs differ by more than 1%")

    scale = max(float(np.median(observed_w)), 1e-4)
    normalized_weights = weights / max(float(np.median(weights)), 1e-12)
    diagnostic_grid = np.linspace(
        min(-1.5, float(k.min()) - 0.25),
        max(1.5, float(k.max()) + 0.25),
        301,
    )
    lower_curve = (
        np.asarray(lower_slice.total_variance(diagnostic_grid)) if lower_slice is not None else None
    )
    sqrt_penalty = sqrt(penalty_strength)

    def residuals(vector: FloatArray) -> FloatArray:
        parameters = _parameters_from_vector(vector)
        model = np.asarray(parameters.total_variance(k))
        data_residual = np.sqrt(normalized_weights) * (model - observed_w) / scale
        grid_w = np.asarray(parameters.total_variance(diagnostic_grid))
        grid_g = np.asarray(parameters.butterfly_density_factor(diagnostic_grid))
        penalties = [
            np.minimum(grid_w, 0.0) / scale,
            np.minimum(grid_g, 0.0),
            np.array([min(parameters.minimum_total_variance, 0.0) / scale]),
            np.array(
                [
                    max(parameters.b * (1.0 + parameters.rho) - 2.0, 0.0),
                    max(parameters.b * (1.0 - parameters.rho) - 2.0, 0.0),
                ]
            ),
        ]
        if lower_curve is not None:
            penalties.append(np.minimum(grid_w - lower_curve, 0.0) / scale)
        return np.concatenate([data_residual, *(sqrt_penalty * row for row in penalties)])

    max_w = max(float(observed_w.max()), 1e-3)
    k_range = max(float(np.ptp(k)), 0.1)
    base_b = max(min((float(observed_w.max()) - float(observed_w.min())) / k_range, 1.0), 0.02)
    minimum_k = float(k[int(np.argmin(observed_w))])
    bounds = (
        np.array([-2.0 * max_w, 1e-8, -0.999, float(k.min()) - 2.0, 1e-4]),
        np.array([2.0 * max_w, 2.5, 0.999, float(k.max()) + 2.0, 5.0]),
    )
    starts = []
    for rho in (-0.6, -0.2, 0.2):
        for sigma in (0.1, 0.35, 0.8):
            b = base_b
            a = max(float(observed_w.min()) - b * sigma * sqrt(1.0 - rho * rho), 1e-6)
            starts.append(np.array([a, b, rho, minimum_k, sigma], dtype=float))

    best = None
    last_optimizer_message = "no optimizer candidate was evaluated"
    for start in starts:
        candidate = least_squares(
            residuals,
            np.clip(start, bounds[0] + 1e-10, bounds[1] - 1e-10),
            bounds=bounds,
            loss="soft_l1",
            f_scale=0.1,
            x_scale="jac",
            max_nfev=5000,
            ftol=1e-12,
            xtol=1e-12,
            gtol=1e-12,
        )
        last_optimizer_message = str(candidate.message)
        if not candidate.success:
            continue
        objective = float(np.dot(residuals(candidate.x), residuals(candidate.x)))
        if best is None or objective < best[0]:
            best = (objective, candidate)
    if best is None:
        raise SurfaceCalibrationError(
            f"optimizer did not return a successful candidate: {last_optimizer_message}"
        )

    parameters = _parameters_from_vector(best[1].x)
    fitted_w = np.asarray(parameters.total_variance(k))
    errors = fitted_w - observed_w
    weighted_rmse = sqrt(float(np.average(errors * errors, weights=normalized_weights)))
    return SVISlice(
        time_to_expiry=maturity,
        forward=forward,
        parameters=parameters,
        weighted_rmse=weighted_rmse,
        max_abs_total_variance_error=float(np.max(np.abs(errors))),
        observations=len(points),
    )


def calibrate_svi_surface(
    points: Iterable[CalibrationPoint],
    *,
    calendar_penalty: bool = True,
    penalty_strength: float = 1_000.0,
    strict: bool = True,
    arbitrage_tolerance: float = 1e-7,
) -> SVISurface:
    """Calibrate maturity slices sequentially with soft static-arbitrage penalties."""

    point_list = list(points)
    if not point_list:
        raise SurfaceCalibrationError("no calibration points supplied")
    grouped: dict[float, list[CalibrationPoint]] = {}
    for point in point_list:
        grouped.setdefault(round(point.time_to_expiry, 12), []).append(point)

    slices: list[SVISlice] = []
    for _, maturity_points in sorted(grouped.items()):
        lower_slice = slices[-1] if calendar_penalty and slices else None
        slices.append(
            _calibrate_slice(
                maturity_points,
                lower_slice=lower_slice,
                penalty_strength=penalty_strength,
            )
        )
    surface = SVISurface(tuple(slices))
    if strict:
        surface.assert_arbitrage_safe(arbitrage_tolerance)
    return surface
