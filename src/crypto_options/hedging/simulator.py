from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import exp, isfinite

from crypto_options.instruments import EuropeanOption
from crypto_options.pricing.black76 import black_scholes_spot_greeks

_SECONDS_PER_YEAR = 365.0 * 24.0 * 60.0 * 60.0


@dataclass(frozen=True, slots=True)
class MarketEvent:
    timestamp: datetime
    spot: float
    volatility: float
    rate: float = 0.0
    carry_rate: float = 0.0

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("market-event timestamps must be timezone-aware")
        if not all(
            isfinite(value) for value in (self.spot, self.volatility, self.rate, self.carry_rate)
        ):
            raise ValueError("market-event values must be finite")
        if self.spot <= 0 or self.volatility <= 0:
            raise ValueError("spot and volatility must be positive")


@dataclass(frozen=True, slots=True)
class HedgePolicy:
    rebalance_interval: timedelta | None = timedelta(hours=4)
    delta_band: float | None = None
    half_spread_bps: float = 1.0
    fee_bps: float = 2.0
    hedge_lot_size: float = 0.001
    initial_hedge: bool = True

    def __post_init__(self) -> None:
        if self.rebalance_interval is not None and self.rebalance_interval.total_seconds() <= 0:
            raise ValueError("rebalance_interval must be positive")
        if self.delta_band is not None and self.delta_band <= 0:
            raise ValueError("delta_band must be positive")
        if self.rebalance_interval is None and self.delta_band is None and self.initial_hedge:
            raise ValueError("an active policy needs an interval or a delta band")
        if min(self.half_spread_bps, self.fee_bps) < 0 or self.hedge_lot_size < 0:
            raise ValueError("costs and hedge_lot_size must be non-negative")


@dataclass(frozen=True, slots=True)
class LedgerRow:
    timestamp: datetime
    spot: float
    volatility: float
    time_to_expiry: float
    option_mark: float
    option_delta: float
    option_gamma: float
    option_vega: float
    option_theta: float
    hedge_units: float
    net_delta: float
    cash: float
    equity: float
    trade_units: float
    execution_price: float | None
    transaction_cost: float
    option_delta_pnl: float
    option_gamma_pnl: float
    option_vega_pnl: float
    option_theta_pnl: float
    option_residual_pnl: float
    hedge_pnl: float
    financing_pnl: float
    reason: str


@dataclass(frozen=True, slots=True)
class HedgeSummary:
    terminal_pnl: float
    trade_count: int
    turnover_notional: float
    transaction_costs: float
    maximum_absolute_net_delta: float
    option_delta_pnl: float
    option_gamma_pnl: float
    option_vega_pnl: float
    option_theta_pnl: float
    option_residual_pnl: float
    hedge_pnl: float
    financing_pnl: float
    reconciliation_error: float


@dataclass(frozen=True, slots=True)
class HedgeResult:
    ledger: tuple[LedgerRow, ...]
    summary: HedgeSummary


def _round_to_lot(target: float, lot_size: float) -> float:
    if lot_size == 0:
        return target
    return round(target / lot_size) * lot_size


class DeltaHedgingSimulator:
    """Event-driven, self-financing spot-delta hedging with an audit ledger."""

    def __init__(self, policy: HedgePolicy) -> None:
        self.policy = policy

    def _should_rebalance(
        self,
        timestamp: datetime,
        last_hedge_time: datetime,
        hedge_units: float,
        target_units: float,
    ) -> tuple[bool, str]:
        interval_due = (
            self.policy.rebalance_interval is not None
            and timestamp - last_hedge_time >= self.policy.rebalance_interval
        )
        band_breached = (
            self.policy.delta_band is not None
            and abs(target_units - hedge_units) >= self.policy.delta_band
        )
        if interval_due and band_breached:
            return True, "interval_and_delta_band"
        if interval_due:
            return True, "interval"
        if band_breached:
            return True, "delta_band"
        return False, "mark"

    def _execute(
        self,
        cash: float,
        trade_units: float,
        spot: float,
    ) -> tuple[float, float, float]:
        if trade_units == 0:
            return cash, spot, 0.0
        direction = 1.0 if trade_units > 0 else -1.0
        half_spread = self.policy.half_spread_bps * 1e-4
        execution_price = spot * (1.0 + direction * half_spread)
        explicit_fee = abs(trade_units) * spot * self.policy.fee_bps * 1e-4
        spread_cost = abs(trade_units) * abs(execution_price - spot)
        cash -= trade_units * execution_price + explicit_fee
        return cash, execution_price, explicit_fee + spread_cost

    def run(
        self,
        option: EuropeanOption,
        option_quantity: float,
        events: Iterable[MarketEvent],
    ) -> HedgeResult:
        if option_quantity == 0 or not isfinite(option_quantity):
            raise ValueError("option_quantity must be finite and non-zero")
        event_list = sorted(events, key=lambda event: event.timestamp)
        if len(event_list) < 2:
            raise ValueError("at least two market events are required")
        timestamps = [event.timestamp.astimezone(UTC) for event in event_list]
        if len(set(timestamps)) != len(timestamps):
            raise ValueError("market-event timestamps must be unique")
        expiry_utc = option.expiry.astimezone(UTC)
        if timestamps[0] >= expiry_utc:
            raise ValueError("the first market event must precede expiry")
        if expiry_utc not in timestamps:
            raise ValueError("market events must contain an observation exactly at expiry")
        event_list = [
            event for event in event_list if event.timestamp.astimezone(UTC) <= expiry_utc
        ]

        effective_quantity = option_quantity * option.contract_size
        initial = event_list[0]
        initial_tau = option.time_to_expiry(initial.timestamp)
        price, delta, gamma, vega, theta = black_scholes_spot_greeks(
            initial.spot,
            option.strike,
            initial_tau,
            initial.volatility,
            initial.rate,
            initial.carry_rate,
            option.option_type,
        )
        option_mark = effective_quantity * price
        option_delta = effective_quantity * delta
        option_gamma = effective_quantity * gamma
        option_vega = effective_quantity * vega
        option_theta = effective_quantity * theta
        cash = -option_mark
        hedge_units = 0.0
        initial_trade = 0.0
        initial_execution: float | None = None
        initial_cost = 0.0
        if self.policy.initial_hedge:
            target = _round_to_lot(-option_delta, self.policy.hedge_lot_size)
            initial_trade = target
            cash, initial_execution, initial_cost = self._execute(cash, initial_trade, initial.spot)
            hedge_units = target
        equity = option_mark + hedge_units * initial.spot + cash
        ledger = [
            LedgerRow(
                timestamp=initial.timestamp,
                spot=initial.spot,
                volatility=initial.volatility,
                time_to_expiry=initial_tau,
                option_mark=option_mark,
                option_delta=option_delta,
                option_gamma=option_gamma,
                option_vega=option_vega,
                option_theta=option_theta,
                hedge_units=hedge_units,
                net_delta=option_delta + hedge_units,
                cash=cash,
                equity=equity,
                trade_units=initial_trade,
                execution_price=initial_execution,
                transaction_cost=initial_cost,
                option_delta_pnl=0.0,
                option_gamma_pnl=0.0,
                option_vega_pnl=0.0,
                option_theta_pnl=0.0,
                option_residual_pnl=0.0,
                hedge_pnl=0.0,
                financing_pnl=0.0,
                reason="initial_hedge" if self.policy.initial_hedge else "initial_mark",
            )
        ]
        last_hedge_time = initial.timestamp

        previous = initial
        previous_option_mark = option_mark
        previous_delta = option_delta
        previous_gamma = option_gamma
        previous_vega = option_vega
        previous_theta = option_theta

        for event in event_list[1:]:
            dt_years = (event.timestamp - previous.timestamp).total_seconds() / _SECONDS_PER_YEAR
            if dt_years <= 0:
                raise ValueError("market events must be strictly increasing")
            cash_before_accrual = cash
            cash *= exp(previous.rate * dt_years)
            cash += hedge_units * previous.spot * (exp(previous.carry_rate * dt_years) - 1.0)
            financing_pnl = cash - cash_before_accrual
            hedge_pnl = hedge_units * (event.spot - previous.spot)
            delta_pnl = previous_delta * (event.spot - previous.spot)
            gamma_pnl = 0.5 * previous_gamma * (event.spot - previous.spot) ** 2
            vega_pnl = previous_vega * (event.volatility - previous.volatility)
            theta_pnl = previous_theta * dt_years
            is_expiry = event.timestamp.astimezone(UTC) == expiry_utc

            if is_expiry:
                settlement = option_quantity * option.payoff(event.spot)
                actual_option_change = settlement - previous_option_mark
                residual_pnl = actual_option_change - (delta_pnl + gamma_pnl + vega_pnl + theta_pnl)
                trade_units = -hedge_units
                cash, execution_price, transaction_cost = self._execute(
                    cash, trade_units, event.spot
                )
                cash += settlement
                hedge_units = 0.0
                option_mark = 0.0
                option_delta = option_gamma = option_vega = option_theta = 0.0
                equity = cash
                reason = "expiry_settlement"
            else:
                tau = option.time_to_expiry(event.timestamp)
                price, delta, gamma, vega, theta = black_scholes_spot_greeks(
                    event.spot,
                    option.strike,
                    tau,
                    event.volatility,
                    event.rate,
                    event.carry_rate,
                    option.option_type,
                )
                option_mark = effective_quantity * price
                option_delta = effective_quantity * delta
                option_gamma = effective_quantity * gamma
                option_vega = effective_quantity * vega
                option_theta = effective_quantity * theta
                actual_option_change = option_mark - previous_option_mark
                residual_pnl = actual_option_change - (delta_pnl + gamma_pnl + vega_pnl + theta_pnl)
                target = _round_to_lot(-option_delta, self.policy.hedge_lot_size)
                rebalance, reason = self._should_rebalance(
                    event.timestamp, last_hedge_time, hedge_units, target
                )
                trade_units = target - hedge_units if rebalance else 0.0
                execution_price = None
                transaction_cost = 0.0
                if trade_units != 0.0:
                    cash, execution_price, transaction_cost = self._execute(
                        cash, trade_units, event.spot
                    )
                    hedge_units = target
                if rebalance:
                    last_hedge_time = event.timestamp
                equity = option_mark + hedge_units * event.spot + cash

            ledger.append(
                LedgerRow(
                    timestamp=event.timestamp,
                    spot=event.spot,
                    volatility=event.volatility,
                    time_to_expiry=option.time_to_expiry(event.timestamp),
                    option_mark=option_mark,
                    option_delta=option_delta,
                    option_gamma=option_gamma,
                    option_vega=option_vega,
                    option_theta=option_theta,
                    hedge_units=hedge_units,
                    net_delta=option_delta + hedge_units,
                    cash=cash,
                    equity=equity,
                    trade_units=trade_units,
                    execution_price=execution_price,
                    transaction_cost=transaction_cost,
                    option_delta_pnl=delta_pnl,
                    option_gamma_pnl=gamma_pnl,
                    option_vega_pnl=vega_pnl,
                    option_theta_pnl=theta_pnl,
                    option_residual_pnl=residual_pnl,
                    hedge_pnl=hedge_pnl,
                    financing_pnl=financing_pnl,
                    reason=reason,
                )
            )
            previous = event
            previous_option_mark = option_mark
            previous_delta = option_delta
            previous_gamma = option_gamma
            previous_vega = option_vega
            previous_theta = option_theta

        return self._summarize(tuple(ledger))

    @staticmethod
    def _summarize(ledger: tuple[LedgerRow, ...]) -> HedgeResult:
        transaction_costs = sum(row.transaction_cost for row in ledger)
        delta_pnl = sum(row.option_delta_pnl for row in ledger)
        gamma_pnl = sum(row.option_gamma_pnl for row in ledger)
        vega_pnl = sum(row.option_vega_pnl for row in ledger)
        theta_pnl = sum(row.option_theta_pnl for row in ledger)
        residual_pnl = sum(row.option_residual_pnl for row in ledger)
        hedge_pnl = sum(row.hedge_pnl for row in ledger)
        financing_pnl = sum(row.financing_pnl for row in ledger)
        attributed = (
            delta_pnl
            + gamma_pnl
            + vega_pnl
            + theta_pnl
            + residual_pnl
            + hedge_pnl
            + financing_pnl
            - transaction_costs
        )
        terminal_pnl = ledger[-1].equity
        summary = HedgeSummary(
            terminal_pnl=terminal_pnl,
            trade_count=sum(row.trade_units != 0 for row in ledger),
            turnover_notional=sum(abs(row.trade_units) * row.spot for row in ledger),
            transaction_costs=transaction_costs,
            maximum_absolute_net_delta=max(abs(row.net_delta) for row in ledger),
            option_delta_pnl=delta_pnl,
            option_gamma_pnl=gamma_pnl,
            option_vega_pnl=vega_pnl,
            option_theta_pnl=theta_pnl,
            option_residual_pnl=residual_pnl,
            hedge_pnl=hedge_pnl,
            financing_pnl=financing_pnl,
            reconciliation_error=terminal_pnl - attributed,
        )
        return HedgeResult(ledger, summary)
