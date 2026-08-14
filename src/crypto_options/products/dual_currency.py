from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from math import exp, isfinite

from crypto_options.instruments import OptionType
from crypto_options.pricing.black76 import black_scholes_spot_greeks


@dataclass(frozen=True, slots=True)
class DualCurrencySettlement:
    currency: str
    amount: float
    quote_value: float


@dataclass(frozen=True, slots=True)
class DualCurrencyGreeks:
    price: float
    spot_delta: float
    spot_gamma: float
    vega: float
    theta: float


@dataclass(frozen=True, slots=True)
class DualCurrencyInvestment:
    """Stablecoin-deposit DCI replicated by a zero-coupon cash leg and short put.

    `term_coupon_rate` is the contractual return over the complete product term,
    not an annualized rate. If terminal spot is below strike, the redemption amount
    is paid in base currency at the strike conversion rate.
    """

    principal_quote: float
    term_coupon_rate: float
    strike: float
    expiry: datetime
    quote_currency: str = "USD"
    base_currency: str = "BTC"

    def __post_init__(self) -> None:
        values = (self.principal_quote, self.term_coupon_rate, self.strike)
        if not all(isfinite(value) for value in values):
            raise ValueError("product terms must be finite")
        if self.principal_quote <= 0 or self.strike <= 0:
            raise ValueError("principal_quote and strike must be positive")
        if self.term_coupon_rate <= -1:
            raise ValueError("term_coupon_rate must exceed -100%")
        if self.expiry.tzinfo is None or self.expiry.utcoffset() is None:
            raise ValueError("expiry must be timezone-aware")
        if not self.quote_currency or not self.base_currency:
            raise ValueError("currency labels must be non-empty")

    @property
    def promised_quote_redemption(self) -> float:
        return self.principal_quote * (1.0 + self.term_coupon_rate)

    def time_to_expiry(self, valuation_time: datetime) -> float:
        if valuation_time.tzinfo is None or valuation_time.utcoffset() is None:
            raise ValueError("valuation_time must be timezone-aware")
        seconds = (self.expiry.astimezone(UTC) - valuation_time.astimezone(UTC)).total_seconds()
        return max(seconds, 0.0) / (365.0 * 24.0 * 60.0 * 60.0)

    def settle(self, terminal_spot: float) -> DualCurrencySettlement:
        if terminal_spot <= 0:
            raise ValueError("terminal_spot must be positive")
        redemption = self.promised_quote_redemption
        if terminal_spot >= self.strike:
            return DualCurrencySettlement(self.quote_currency, redemption, redemption)
        base_amount = redemption / self.strike
        return DualCurrencySettlement(
            self.base_currency,
            base_amount,
            base_amount * terminal_spot,
        )

    def payoff_quote_value(self, terminal_spot: float) -> float:
        return self.settle(terminal_spot).quote_value

    def price_and_greeks(
        self,
        valuation_time: datetime,
        spot: float,
        volatility: float,
        rate: float,
        carry_rate: float = 0.0,
    ) -> DualCurrencyGreeks:
        maturity = self.time_to_expiry(valuation_time)
        redemption = self.promised_quote_redemption
        discount = exp(-rate * maturity)
        put_price, put_delta, put_gamma, put_vega, put_theta = black_scholes_spot_greeks(
            spot,
            self.strike,
            maturity,
            volatility,
            rate,
            carry_rate,
            OptionType.PUT,
        )
        put_units = redemption / self.strike
        return DualCurrencyGreeks(
            price=discount * redemption - put_units * put_price,
            spot_delta=-put_units * put_delta,
            spot_gamma=-put_units * put_gamma,
            vega=-put_units * put_vega,
            theta=rate * discount * redemption - put_units * put_theta,
        )

    def fair_term_coupon_rate(
        self,
        valuation_time: datetime,
        spot: float,
        volatility: float,
        rate: float,
        carry_rate: float = 0.0,
    ) -> float:
        """Return the whole-term coupon that makes model value equal principal."""

        maturity = self.time_to_expiry(valuation_time)
        discount = exp(-rate * maturity)
        put_price = black_scholes_spot_greeks(
            spot,
            self.strike,
            maturity,
            volatility,
            rate,
            carry_rate,
            OptionType.PUT,
        )[0]
        redemption_value_per_quote = discount - put_price / self.strike
        if redemption_value_per_quote <= 0:
            raise ValueError("market inputs imply a non-positive redemption-value factor")
        return 1.0 / redemption_value_per_quote - 1.0
