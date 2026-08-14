from __future__ import annotations

from math import exp

import pytest

from crypto_options.instruments import OptionType
from crypto_options.pricing.black76 import (
    ImpliedVolError,
    black76_greeks,
    black76_price,
    implied_volatility,
    no_arbitrage_bounds,
)


@pytest.mark.parametrize("option_type", [OptionType.CALL, OptionType.PUT])
@pytest.mark.parametrize(
    "forward,strike,volatility", [(100, 70, 0.2), (100, 100, 0.7), (100, 150, 1.4)]
)
def test_implied_volatility_round_trip(
    option_type: OptionType,
    forward: float,
    strike: float,
    volatility: float,
) -> None:
    maturity = 0.37
    discount = exp(-0.04 * maturity)
    premium = black76_price(forward, strike, maturity, volatility, discount, option_type)
    recovered = implied_volatility(premium, forward, strike, maturity, discount, option_type)
    assert recovered == pytest.approx(volatility, rel=1e-9, abs=1e-10)


def test_put_call_parity() -> None:
    forward, strike, maturity, volatility = 62_500.0, 60_000.0, 0.25, 0.63
    discount = exp(-0.03 * maturity)
    call = black76_price(forward, strike, maturity, volatility, discount, OptionType.CALL)
    put = black76_price(forward, strike, maturity, volatility, discount, OptionType.PUT)
    assert call - put == pytest.approx(discount * (forward - strike), abs=1e-10)


def test_analytic_greeks_match_central_differences() -> None:
    forward, strike, maturity, volatility, discount = 60_000.0, 57_500.0, 0.41, 0.72, 0.99
    greeks = black76_greeks(forward, strike, maturity, volatility, discount, OptionType.CALL)
    forward_step = 0.5
    up = black76_price(
        forward + forward_step,
        strike,
        maturity,
        volatility,
        discount,
        OptionType.CALL,
    )
    base = greeks.price
    down = black76_price(
        forward - forward_step,
        strike,
        maturity,
        volatility,
        discount,
        OptionType.CALL,
    )
    numerical_delta = (up - down) / (2 * forward_step)
    numerical_gamma = (up - 2 * base + down) / forward_step**2
    vol_step = 1e-4
    vol_up = black76_price(
        forward,
        strike,
        maturity,
        volatility + vol_step,
        discount,
        OptionType.CALL,
    )
    vol_down = black76_price(
        forward,
        strike,
        maturity,
        volatility - vol_step,
        discount,
        OptionType.CALL,
    )
    numerical_vega = (vol_up - vol_down) / (2 * vol_step)
    numerical_volga = (vol_up - 2 * base + vol_down) / vol_step**2

    assert greeks.forward_delta == pytest.approx(numerical_delta, rel=1e-8)
    assert greeks.gamma_forward == pytest.approx(numerical_gamma, rel=2e-5)
    assert greeks.vega == pytest.approx(numerical_vega, rel=1e-8)
    assert greeks.volga == pytest.approx(numerical_volga, rel=2e-4)


def test_price_bounds_are_enforced() -> None:
    lower, upper = no_arbitrage_bounds(100, 90, 0.98, OptionType.CALL)
    with pytest.raises(ImpliedVolError, match="violates"):
        implied_volatility(lower - 1.0, 100, 90, 0.5, 0.98, OptionType.CALL)
    with pytest.raises(ImpliedVolError, match="infinite-volatility"):
        implied_volatility(upper, 100, 90, 0.5, 0.98, OptionType.CALL)


def test_zero_volatility_uses_discounted_intrinsic() -> None:
    price = black76_price(105, 100, 1.0, 0.0, 0.95, OptionType.CALL)
    assert price == pytest.approx(4.75)
