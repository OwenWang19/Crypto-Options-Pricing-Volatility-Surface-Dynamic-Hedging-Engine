from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import exp, log, sqrt

import numpy as np

from crypto_options.hedging.simulator import MarketEvent


@dataclass(frozen=True, slots=True)
class StochasticVolatilityPathConfig:
    initial_spot: float
    initial_volatility: float
    long_run_volatility: float = 0.65
    mean_reversion: float = 3.0
    volatility_of_log_volatility: float = 0.70
    spot_volatility_correlation: float = -0.45
    rate: float = 0.03
    carry_rate: float = 0.0
    physical_drift: float | None = None
    minimum_volatility: float = 0.05
    maximum_volatility: float = 3.0

    def __post_init__(self) -> None:
        if self.initial_spot <= 0:
            raise ValueError("initial_spot must be positive")
        positive = (
            self.initial_volatility,
            self.long_run_volatility,
            self.mean_reversion,
            self.volatility_of_log_volatility,
            self.minimum_volatility,
            self.maximum_volatility,
        )
        if any(value <= 0 for value in positive):
            raise ValueError("volatility and mean-reversion parameters must be positive")
        if not -1 < self.spot_volatility_correlation < 1:
            raise ValueError("spot_volatility_correlation must be inside (-1, 1)")
        if self.minimum_volatility >= self.maximum_volatility:
            raise ValueError("minimum_volatility must be below maximum_volatility")


def generate_market_path(
    start: datetime,
    expiry: datetime,
    steps: int,
    config: StochasticVolatilityPathConfig,
    *,
    seed: int,
) -> tuple[MarketEvent, ...]:
    """Generate a correlated lognormal-spot, mean-reverting log-vol path."""

    if start.tzinfo is None or start.utcoffset() is None:
        raise ValueError("start must be timezone-aware")
    if expiry.tzinfo is None or expiry.utcoffset() is None:
        raise ValueError("expiry must be timezone-aware")
    if expiry <= start or steps < 1:
        raise ValueError("expiry must follow start and steps must be positive")

    total_seconds = (expiry - start).total_seconds()
    dt_years = total_seconds / steps / (365.0 * 24.0 * 60.0 * 60.0)
    sqrt_dt = sqrt(dt_years)
    drift = (
        config.rate - config.carry_rate if config.physical_drift is None else config.physical_drift
    )
    rng = np.random.default_rng(seed)
    independent = rng.standard_normal((steps, 2))
    correlated_vol_shock = (
        config.spot_volatility_correlation * independent[:, 0]
        + sqrt(1.0 - config.spot_volatility_correlation**2) * independent[:, 1]
    )

    log_spot = log(config.initial_spot)
    log_vol = log(config.initial_volatility)
    long_run_log_vol = log(config.long_run_volatility)
    events = [
        MarketEvent(
            start,
            config.initial_spot,
            config.initial_volatility,
            config.rate,
            config.carry_rate,
        )
    ]

    for step in range(1, steps + 1):
        volatility = exp(log_vol)
        log_spot += (drift - 0.5 * volatility * volatility) * dt_years
        log_spot += volatility * sqrt_dt * independent[step - 1, 0]
        log_vol += config.mean_reversion * (long_run_log_vol - log_vol) * dt_years
        log_vol += config.volatility_of_log_volatility * sqrt_dt * correlated_vol_shock[step - 1]
        log_vol = float(
            np.clip(log_vol, log(config.minimum_volatility), log(config.maximum_volatility))
        )
        timestamp = start + (expiry - start) * (step / steps)
        events.append(
            MarketEvent(
                timestamp,
                exp(log_spot),
                exp(log_vol),
                config.rate,
                config.carry_rate,
            )
        )
    return tuple(events)
