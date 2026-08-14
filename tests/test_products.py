from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import exp

import pytest

from crypto_options.instruments import OptionType
from crypto_options.pricing.black76 import black_scholes_spot_greeks
from crypto_options.products.dual_currency import DualCurrencyInvestment


def _product() -> tuple[DualCurrencyInvestment, datetime]:
    valuation_time = datetime(2026, 1, 1, tzinfo=UTC)
    product = DualCurrencyInvestment(
        principal_quote=100_000.0,
        term_coupon_rate=0.02,
        strike=60_000.0,
        expiry=valuation_time + timedelta(days=30),
    )
    return product, valuation_time


def test_dual_currency_settlement_switches_currency_at_strike() -> None:
    product, _ = _product()
    above = product.settle(65_000.0)
    below = product.settle(50_000.0)
    assert above.currency == "USD"
    assert above.amount == pytest.approx(102_000.0)
    assert above.quote_value == pytest.approx(102_000.0)
    assert below.currency == "BTC"
    assert below.amount == pytest.approx(1.7)
    assert below.quote_value == pytest.approx(85_000.0)


@pytest.mark.parametrize("terminal_spot", [30_000.0, 59_999.0, 60_000.0, 90_000.0])
def test_payoff_equals_cash_minus_short_put_replication(terminal_spot: float) -> None:
    product, _ = _product()
    redemption = product.promised_quote_redemption
    replicated = redemption - redemption / product.strike * max(product.strike - terminal_spot, 0.0)
    assert product.payoff_quote_value(terminal_spot) == pytest.approx(replicated)


def test_price_and_greeks_equal_cash_plus_short_put_replication() -> None:
    product, valuation_time = _product()
    spot, volatility, rate = 58_000.0, 0.65, 0.03
    maturity = product.time_to_expiry(valuation_time)
    put = black_scholes_spot_greeks(
        spot,
        product.strike,
        maturity,
        volatility,
        rate,
        0.0,
        OptionType.PUT,
    )
    put_units = product.promised_quote_redemption / product.strike
    result = product.price_and_greeks(valuation_time, spot, volatility, rate)
    assert result.price == pytest.approx(
        exp(-rate * maturity) * product.promised_quote_redemption - put_units * put[0]
    )
    assert result.spot_delta == pytest.approx(-put_units * put[1])
    assert result.spot_gamma == pytest.approx(-put_units * put[2])
    assert result.vega == pytest.approx(-put_units * put[3])
    assert result.spot_gamma < 0
    assert result.vega < 0


def test_fair_coupon_prices_product_at_principal() -> None:
    product, valuation_time = _product()
    fair_coupon = product.fair_term_coupon_rate(
        valuation_time, spot=58_000.0, volatility=0.65, rate=0.03
    )
    fair_product = DualCurrencyInvestment(
        principal_quote=product.principal_quote,
        term_coupon_rate=fair_coupon,
        strike=product.strike,
        expiry=product.expiry,
    )
    model_value = fair_product.price_and_greeks(
        valuation_time, spot=58_000.0, volatility=0.65, rate=0.03
    ).price
    assert model_value == pytest.approx(product.principal_quote, abs=1e-10)
