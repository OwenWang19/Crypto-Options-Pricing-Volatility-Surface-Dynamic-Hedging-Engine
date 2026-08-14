from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from math import isfinite, log, sqrt

import numpy as np

from crypto_options.instruments import OptionType
from crypto_options.pricing.black76 import (
    ImpliedVolError,
    black76_greeks,
    implied_volatility,
    no_arbitrage_bounds,
)


@dataclass(frozen=True, slots=True)
class OptionQuote:
    instrument_name: str
    time_to_expiry: float
    strike: float
    option_type: OptionType
    bid: float
    ask: float
    forward: float
    discount_factor: float = 1.0
    open_interest: float = 0.0

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True, slots=True)
class CalibrationPoint:
    instrument_name: str
    time_to_expiry: float
    strike: float
    option_type: OptionType
    forward: float
    log_moneyness: float
    implied_volatility: float
    total_variance: float
    bid_implied_volatility: float | None
    ask_implied_volatility: float | None
    price_spread: float
    vega: float
    weight: float


@dataclass(frozen=True, slots=True)
class ChainCleaningResult:
    points: tuple[CalibrationPoint, ...]
    rejection_counts: dict[str, int]
    total_quotes: int

    @property
    def accepted_quotes(self) -> int:
        return len(self.points)


def _structurally_valid(quote: OptionQuote) -> str | None:
    numeric = (
        quote.time_to_expiry,
        quote.strike,
        quote.bid,
        quote.ask,
        quote.forward,
        quote.discount_factor,
        quote.open_interest,
    )
    if not all(isfinite(value) for value in numeric):
        return "non_finite"
    if quote.time_to_expiry <= 0:
        return "expired"
    if quote.strike <= 0 or quote.forward <= 0:
        return "non_positive_market_input"
    if quote.bid < 0 or quote.ask <= 0:
        return "non_positive_quote"
    if quote.ask <= quote.bid:
        return "crossed_or_locked"
    if quote.discount_factor <= 0:
        return "invalid_discount_factor"
    if quote.open_interest < 0:
        return "negative_open_interest"
    return None


def _safe_iv(premium: float, quote: OptionQuote) -> float | None:
    try:
        iv = implied_volatility(
            premium,
            quote.forward,
            quote.strike,
            quote.time_to_expiry,
            quote.discount_factor,
            quote.option_type,
        )
    except ImpliedVolError:
        return None
    return iv if iv > 0 else None


def clean_option_chain(
    quotes: Iterable[OptionQuote],
    *,
    max_relative_spread: float = 0.5,
    min_vega_ratio: float = 1e-6,
    max_abs_log_moneyness: float = 2.0,
) -> ChainCleaningResult:
    """Validate quotes, invert IV, and create liquidity-aware SVI calibration weights.

    The function never replaces an invalid market price with a mark price. Each rejected
    observation receives an auditable reason instead.
    """

    quote_list = list(quotes)
    rejected: Counter[str] = Counter()
    provisional: list[tuple[OptionQuote, float, float | None, float | None, float]] = []

    for quote in quote_list:
        reason = _structurally_valid(quote)
        if reason:
            rejected[reason] += 1
            continue
        lower, upper = no_arbitrage_bounds(
            quote.forward, quote.strike, quote.discount_factor, quote.option_type
        )
        tolerance = 1e-10 * max(quote.forward, quote.strike)
        if quote.mid <= lower + tolerance or quote.mid >= upper - tolerance:
            rejected["mid_outside_strict_bounds"] += 1
            continue
        if quote.spread / max(quote.mid, tolerance) > max_relative_spread:
            rejected["wide_relative_spread"] += 1
            continue
        log_moneyness = log(quote.strike / quote.forward)
        if abs(log_moneyness) > max_abs_log_moneyness:
            rejected["extreme_log_moneyness"] += 1
            continue
        try:
            mid_iv = implied_volatility(
                quote.mid,
                quote.forward,
                quote.strike,
                quote.time_to_expiry,
                quote.discount_factor,
                quote.option_type,
            )
        except ImpliedVolError:
            rejected["mid_iv_failure"] += 1
            continue
        bid_iv = _safe_iv(quote.bid, quote)
        ask_iv = _safe_iv(quote.ask, quote)
        vega = black76_greeks(
            quote.forward,
            quote.strike,
            quote.time_to_expiry,
            mid_iv,
            quote.discount_factor,
            quote.option_type,
        ).vega
        if vega / quote.forward < min_vega_ratio:
            rejected["negligible_vega"] += 1
            continue
        provisional.append((quote, mid_iv, bid_iv, ask_iv, vega))

    points: list[CalibrationPoint] = []
    if provisional:
        raw_weights = []
        for quote, _, bid_iv, ask_iv, vega in provisional:
            if bid_iv is not None and ask_iv is not None and ask_iv > bid_iv:
                iv_spread = ask_iv - bid_iv
            else:
                iv_spread = quote.spread / max(vega, 1e-12)
            liquidity = 1.0 + sqrt(quote.open_interest)
            raw_weights.append(liquidity / max(iv_spread, 0.01) ** 2)
        weight_scale = float(np.median(raw_weights))

        for row, raw_weight in zip(provisional, raw_weights, strict=True):
            quote, mid_iv, bid_iv, ask_iv, vega = row
            points.append(
                CalibrationPoint(
                    instrument_name=quote.instrument_name,
                    time_to_expiry=quote.time_to_expiry,
                    strike=quote.strike,
                    option_type=quote.option_type,
                    forward=quote.forward,
                    log_moneyness=log(quote.strike / quote.forward),
                    implied_volatility=mid_iv,
                    total_variance=mid_iv * mid_iv * quote.time_to_expiry,
                    bid_implied_volatility=bid_iv,
                    ask_implied_volatility=ask_iv,
                    price_spread=quote.spread,
                    vega=vega,
                    weight=min(raw_weight / max(weight_scale, 1e-12), 100.0),
                )
            )

    points.sort(key=lambda point: (point.time_to_expiry, point.strike, point.option_type.value))
    return ChainCleaningResult(tuple(points), dict(sorted(rejected.items())), len(quote_list))


@dataclass(frozen=True, slots=True)
class ForwardEstimate:
    time_to_expiry: float
    forward: float
    dispersion: float
    matched_strikes: int


def estimate_forward_from_put_call_parity(
    quotes: Iterable[OptionQuote],
    *,
    minimum_pairs: int = 3,
) -> tuple[ForwardEstimate, ...]:
    """Estimate forwards via a spread-weighted robust median of call-put parity."""

    grouped: dict[tuple[float, float], dict[OptionType, OptionQuote]] = {}
    for quote in quotes:
        if _structurally_valid(quote) is None:
            grouped.setdefault((quote.time_to_expiry, quote.strike), {})[quote.option_type] = quote

    by_expiry: dict[float, list[tuple[float, float]]] = {}
    for (expiry, strike), pair in grouped.items():
        if OptionType.CALL not in pair or OptionType.PUT not in pair:
            continue
        call, put = pair[OptionType.CALL], pair[OptionType.PUT]
        if abs(call.discount_factor - put.discount_factor) > 1e-12:
            continue
        estimate = strike + (call.mid - put.mid) / call.discount_factor
        if estimate <= 0:
            continue
        combined_spread = call.spread + put.spread
        weight = 1.0 / max(combined_spread, 1e-8)
        by_expiry.setdefault(expiry, []).append((estimate, weight))

    results: list[ForwardEstimate] = []
    for expiry, estimates_and_weights in sorted(by_expiry.items()):
        if len(estimates_and_weights) < minimum_pairs:
            continue
        estimates = np.array([row[0] for row in estimates_and_weights])
        weights = np.array([row[1] for row in estimates_and_weights])
        order = np.argsort(estimates)
        sorted_estimates = estimates[order]
        cumulative = np.cumsum(weights[order])
        median_index = int(np.searchsorted(cumulative, 0.5 * cumulative[-1]))
        forward = float(sorted_estimates[median_index])
        absolute_deviation = np.abs(estimates - forward)
        dispersion = float(np.median(absolute_deviation))
        results.append(ForwardEstimate(expiry, forward, dispersion, len(estimates)))
    return tuple(results)
