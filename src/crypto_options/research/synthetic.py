from __future__ import annotations

from math import exp, sqrt

import numpy as np

from crypto_options.instruments import OptionType
from crypto_options.marketdata.chain import OptionQuote
from crypto_options.pricing.black76 import black76_greeks, black76_price, no_arbitrage_bounds
from crypto_options.surface.svi import SVIParameters


def ground_truth_parameters(time_to_expiry: float) -> SVIParameters:
    """Calendar-consistent raw-SVI family used by the offline benchmark."""

    if time_to_expiry <= 0:
        raise ValueError("time_to_expiry must be positive")
    return SVIParameters(
        a=0.25 * time_to_expiry,
        b=0.12 * time_to_expiry,
        rho=-0.35,
        m=0.02,
        sigma=0.35,
    )


def generate_synthetic_option_chain(
    *,
    spot: float = 60_000.0,
    rate: float = 0.03,
    carry_rate: float = 0.0,
    seed: int = 17,
) -> tuple[OptionQuote, ...]:
    """Create a deterministic, two-sided option chain with known SVI truth.

    Noise is applied in implied-volatility space and shared by calls and puts at a
    strike, preserving put-call parity at the mid while still making calibration
    non-trivial.
    """

    if spot <= 0:
        raise ValueError("spot must be positive")
    rng = np.random.default_rng(seed)
    maturities = np.array([30.0, 90.0, 180.0, 365.0]) / 365.0
    log_moneyness_grid = np.linspace(-0.60, 0.60, 13)
    quotes: list[OptionQuote] = []

    for maturity_index, maturity in enumerate(maturities):
        forward = spot * exp((rate - carry_rate) * maturity)
        discount = exp(-rate * maturity)
        parameters = ground_truth_parameters(float(maturity))
        for strike_index, log_moneyness in enumerate(log_moneyness_grid):
            strike = forward * exp(float(log_moneyness))
            true_variance = float(parameters.total_variance(float(log_moneyness)))
            smooth_noise = 0.0015 * np.sin(0.7 * strike_index + 0.3 * maturity_index)
            random_noise = rng.normal(0.0, 0.00035)
            volatility = sqrt(true_variance / maturity) + smooth_noise + random_noise
            for option_type in (OptionType.CALL, OptionType.PUT):
                mid = black76_price(
                    forward, strike, float(maturity), volatility, discount, option_type
                )
                vega = black76_greeks(
                    forward, strike, float(maturity), volatility, discount, option_type
                ).vega
                lower, upper = no_arbitrage_bounds(forward, strike, discount, option_type)
                half_spread = max(0.0075 * vega, 0.00001 * forward)
                half_spread = min(half_spread, 0.45 * (mid - lower), 0.45 * (upper - mid))
                bid = mid - half_spread
                ask = mid + half_spread
                open_interest = 25.0 + 400.0 * exp(-2.5 * log_moneyness**2)
                quotes.append(
                    OptionQuote(
                        instrument_name=(
                            f"SYN-{round(maturity * 365):03d}D-{strike:.2f}-"
                            f"{option_type.value[0].upper()}"
                        ),
                        time_to_expiry=float(maturity),
                        strike=strike,
                        option_type=option_type,
                        bid=bid,
                        ask=ask,
                        forward=forward,
                        discount_factor=discount,
                        open_interest=open_interest,
                    )
                )
    return tuple(quotes)
