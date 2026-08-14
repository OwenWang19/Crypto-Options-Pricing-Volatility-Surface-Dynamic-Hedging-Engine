from __future__ import annotations

from dataclasses import dataclass
from math import erfc, exp, isfinite, log, pi, sqrt

from scipy.optimize import brentq

from crypto_options.instruments import OptionType

_SQRT_2 = sqrt(2.0)
_INV_SQRT_2PI = 1.0 / sqrt(2.0 * pi)


class ImpliedVolError(ValueError):
    """Raised when a premium cannot be mapped to a finite Black-76 volatility."""


@dataclass(frozen=True, slots=True)
class Black76Greeks:
    price: float
    forward_delta: float
    gamma_forward: float
    vega: float
    vanna_forward: float
    volga: float


def _normal_cdf(x: float) -> float:
    return 0.5 * erfc(-x / _SQRT_2)


def _normal_pdf(x: float) -> float:
    return _INV_SQRT_2PI * exp(-0.5 * x * x)


def _validate_inputs(
    forward: float,
    strike: float,
    time_to_expiry: float,
    volatility: float,
    discount_factor: float,
) -> None:
    values = (forward, strike, time_to_expiry, volatility, discount_factor)
    if not all(isfinite(value) for value in values):
        raise ValueError("pricing inputs must be finite")
    if forward <= 0 or strike <= 0:
        raise ValueError("forward and strike must be positive")
    if time_to_expiry < 0 or volatility < 0:
        raise ValueError("time_to_expiry and volatility must be non-negative")
    if not 0 < discount_factor <= 1.5:
        raise ValueError("discount_factor must be positive and economically plausible")


def no_arbitrage_bounds(
    forward: float,
    strike: float,
    discount_factor: float,
    option_type: OptionType,
) -> tuple[float, float]:
    """Return model-independent Black-76 lower and upper premium bounds."""

    _validate_inputs(forward, strike, 0.0, 0.0, discount_factor)
    phi = option_type.sign
    lower = discount_factor * max(phi * (forward - strike), 0.0)
    upper = discount_factor * (forward if option_type is OptionType.CALL else strike)
    return lower, upper


def black76_price(
    forward: float,
    strike: float,
    time_to_expiry: float,
    volatility: float,
    discount_factor: float,
    option_type: OptionType,
) -> float:
    """Price a European option on a forward under the Black-76 model."""

    _validate_inputs(forward, strike, time_to_expiry, volatility, discount_factor)
    phi = option_type.sign
    if time_to_expiry == 0.0 or volatility == 0.0:
        return discount_factor * max(phi * (forward - strike), 0.0)

    vol_sqrt_t = volatility * sqrt(time_to_expiry)
    d1 = log(forward / strike) / vol_sqrt_t + 0.5 * vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    return (
        discount_factor * phi * (forward * _normal_cdf(phi * d1) - strike * _normal_cdf(phi * d2))
    )


def black76_greeks(
    forward: float,
    strike: float,
    time_to_expiry: float,
    volatility: float,
    discount_factor: float,
    option_type: OptionType,
) -> Black76Greeks:
    """Return forward Greeks; vega is per 1.00 absolute volatility."""

    _validate_inputs(forward, strike, time_to_expiry, volatility, discount_factor)
    price = black76_price(forward, strike, time_to_expiry, volatility, discount_factor, option_type)
    phi = option_type.sign
    if time_to_expiry == 0.0 or volatility == 0.0:
        if phi * (forward - strike) > 0:
            delta = discount_factor * phi
        elif forward == strike:
            delta = discount_factor * 0.5 * phi
        else:
            delta = 0.0
        return Black76Greeks(price, delta, 0.0, 0.0, 0.0, 0.0)

    sqrt_t = sqrt(time_to_expiry)
    vol_sqrt_t = volatility * sqrt_t
    d1 = log(forward / strike) / vol_sqrt_t + 0.5 * vol_sqrt_t
    d2 = d1 - vol_sqrt_t
    density = _normal_pdf(d1)
    vega = discount_factor * forward * density * sqrt_t
    gamma = discount_factor * density / (forward * vol_sqrt_t)
    vanna = -discount_factor * density * d2 / volatility
    volga = vega * d1 * d2 / volatility
    delta = discount_factor * phi * _normal_cdf(phi * d1)
    return Black76Greeks(price, delta, gamma, vega, vanna, volga)


def implied_volatility(
    premium: float,
    forward: float,
    strike: float,
    time_to_expiry: float,
    discount_factor: float,
    option_type: OptionType,
    *,
    price_tolerance: float = 1e-10,
    max_volatility: float = 20.0,
) -> float:
    """Invert Black-76 with explicit arbitrage-bound and boundary handling."""

    if not isfinite(premium):
        raise ImpliedVolError("premium must be finite")
    if time_to_expiry <= 0:
        raise ImpliedVolError("positive time_to_expiry is required for implied volatility")
    lower, upper = no_arbitrage_bounds(forward, strike, discount_factor, option_type)
    scaled_tolerance = max(price_tolerance, 1e-12 * max(forward, strike))
    if premium < lower - scaled_tolerance or premium > upper + scaled_tolerance:
        raise ImpliedVolError(
            f"premium {premium:.12g} violates [{lower:.12g}, {upper:.12g}] bounds"
        )
    if premium <= lower + scaled_tolerance:
        return 0.0
    if premium >= upper - scaled_tolerance:
        raise ImpliedVolError("premium is at the infinite-volatility upper bound")

    def objective(sigma: float) -> float:
        return (
            black76_price(
                forward,
                strike,
                time_to_expiry,
                sigma,
                discount_factor,
                option_type,
            )
            - premium
        )

    high = 0.5
    while high < max_volatility and objective(high) < 0.0:
        high *= 2.0
    high = min(high, max_volatility)
    if objective(high) < 0.0:
        raise ImpliedVolError(f"no finite root below max_volatility={max_volatility:g}")
    return float(brentq(objective, 1e-12, high, xtol=1e-12, rtol=1e-12, maxiter=200))


def black_scholes_spot_greeks(
    spot: float,
    strike: float,
    time_to_expiry: float,
    volatility: float,
    rate: float,
    carry_rate: float,
    option_type: OptionType,
) -> tuple[float, float, float, float, float]:
    """Return price, spot delta, spot gamma, vega, and calendar theta."""

    if spot <= 0:
        raise ValueError("spot must be positive")
    discount = exp(-rate * time_to_expiry)
    forward = spot * exp((rate - carry_rate) * time_to_expiry)
    greeks = black76_greeks(forward, strike, time_to_expiry, volatility, discount, option_type)
    if time_to_expiry == 0.0 or volatility == 0.0:
        spot_delta = greeks.forward_delta * forward / spot
        return greeks.price, spot_delta, 0.0, 0.0, 0.0

    sqrt_t = sqrt(time_to_expiry)
    d1 = (log(spot / strike) + (rate - carry_rate + 0.5 * volatility**2) * time_to_expiry) / (
        volatility * sqrt_t
    )
    d2 = d1 - volatility * sqrt_t
    phi = option_type.sign
    discounted_spot = spot * exp(-carry_rate * time_to_expiry)
    discounted_strike = strike * exp(-rate * time_to_expiry)
    delta = phi * exp(-carry_rate * time_to_expiry) * _normal_cdf(phi * d1)
    gamma = exp(-carry_rate * time_to_expiry) * _normal_pdf(d1) / (spot * volatility * sqrt_t)
    vega = discounted_spot * _normal_pdf(d1) * sqrt_t
    theta = -discounted_spot * _normal_pdf(d1) * volatility / (2.0 * sqrt_t)
    theta += phi * (
        carry_rate * discounted_spot * _normal_cdf(phi * d1)
        - rate * discounted_strike * _normal_cdf(phi * d2)
    )
    return greeks.price, delta, gamma, vega, theta
