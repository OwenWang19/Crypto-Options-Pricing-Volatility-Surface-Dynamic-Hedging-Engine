"""Historical quote replay; USD-equivalent accounting for BTC-settled options.

This is an indicative historical backtest, not an executable L2/margin simulation.
Option marks are observed data, not synthetic paths or theoretical entry premiums.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import erfc, exp, isfinite, log, sqrt

import pandas as pd

from crypto_options.instruments import OptionType
from crypto_options.marketdata.historical import HistoricalDataError, spot_exact
from crypto_options.pricing.black76 import ImpliedVolError, implied_volatility

YEAR_SECONDS = 31536000.0


@dataclass(frozen=True)
class HistoricalCosts:
    name: str = "baseline"
    spot_fee_bps: float = 5.0
    spot_slippage_bps: float = 1.0
    option_slippage_fraction: float = 0.01
    option_fee_btc: float = 0.0003
    delivery_fee_btc: float = 0.00015
    fee_premium_cap: float = 0.125
    execution_lag_minutes: int = 1
    annual_borrow_rate: float = 0.0
    max_delta_age_hours: float = 2.0

    def __post_init__(self) -> None:
        numbers = (
            self.spot_fee_bps,
            self.spot_slippage_bps,
            self.option_slippage_fraction,
            self.option_fee_btc,
            self.delivery_fee_btc,
            self.fee_premium_cap,
            self.annual_borrow_rate,
            self.max_delta_age_hours,
        )
        if not all(isfinite(x) for x in numbers) or min(numbers) < 0:
            raise ValueError("cost assumptions must be finite and non-negative")
        if self.option_slippage_fraction >= 1 or not 0 < self.fee_premium_cap <= 1:
            raise ValueError("invalid premium fraction")
        if self.execution_lag_minutes < 1 or self.max_delta_age_hours <= 0:
            raise ValueError("execution must lag a known signal; delta age must be positive")


@dataclass(frozen=True)
class HistoricalPolicy:
    name: str
    interval_hours: float | None = None
    delta_band: float | None = None
    active: bool = True

    def __post_init__(self) -> None:
        if self.active and self.interval_hours is None and self.delta_band is None:
            raise ValueError("an active policy needs a trigger")
        if self.interval_hours is not None and (
            not isfinite(self.interval_hours) or self.interval_hours <= 0
        ):
            raise ValueError("interval must be finite and positive")
        if self.delta_band is not None and (not isfinite(self.delta_band) or self.delta_band <= 0):
            raise ValueError("band must be finite and positive")


POLICIES = (
    HistoricalPolicy("unhedged", active=False),
    HistoricalPolicy("hourly", interval_hours=1),
    HistoricalPolicy("four_hour", interval_hours=4),
    HistoricalPolicy("band_002", delta_band=0.02),
    HistoricalPolicy("band_005", delta_band=0.05),
    HistoricalPolicy("band_010", delta_band=0.10),
)


def execution_time(timestamp: pd.Timestamp, lag_minutes: int) -> pd.Timestamp:
    if lag_minutes < 1:
        raise ValueError("look-ahead protection requires a positive execution lag")
    return timestamp.floor("min") + pd.Timedelta(minutes=lag_minutes)


def inverse_option_iv(
    premium_btc: float, forward: float, strike: float, tau: float, option_type: OptionType
) -> float:
    """BTC inverse premium = undiscounted Black price / forward.

    Equivalently USD value = spot * BTC premium and Black discount = spot/forward.
    Do not accidentally interpret a BTC premium as an exchange-native USD premium.
    """
    return implied_volatility(premium_btc * forward, forward, strike, tau, 1.0, option_type)


def inverse_usd_spot_delta(
    forward: float, strike: float, tau: float, volatility: float, option_type: OptionType
) -> float:
    """Frozen-IV delta of USD-equivalent value, holding F/S fixed (not BTC-price delta)."""
    if min(forward, strike, tau, volatility) <= 0:
        raise ValueError("positive pricing inputs required")
    d1 = (log(forward / strike) + 0.5 * volatility**2 * tau) / (volatility * sqrt(tau))
    cdf = 0.5 * erfc(-d1 / sqrt(2))
    return cdf if option_type is OptionType.CALL else cdf - 1.0


def historical_opportunities(panel: pd.DataFrame) -> tuple[list[dict], list[dict]]:
    """Ex-ante weekly ATM selections, then the NEXT quote; no future-liquidity selection."""
    cases: list[dict] = []
    audit: list[dict] = []
    for day in pd.date_range("2024-01-19", "2024-07-19", freq="W-FRI", tz="UTC"):
        signal_time = day + pd.Timedelta(hours=9)
        expiry = day + pd.Timedelta(days=7, hours=8)
        snapshot = (
            panel[
                (panel.snapshot_time <= signal_time)
                & (panel.snapshot_time > signal_time - pd.Timedelta(minutes=70))
                & (panel.expiry_date == expiry)
            ]
            .sort_values("snapshot_time")
            .drop_duplicates("instrument_name", keep="last")
        )
        for side in ("C", "P"):
            record = {
                "week": day.date().isoformat(),
                "side": side,
                "selection_time": signal_time.isoformat(),
                "expiry": expiry.isoformat(),
            }
            candidates = snapshot[
                snapshot.two_sided
                & (snapshot.relative_spread <= 0.25)
                & (snapshot.open_interest > 0)
                & (snapshot.option_type == side)
            ].copy()
            if candidates.empty:
                audit.append({**record, "status": "skipped", "reason": "no_eligible_signal"})
                continue
            candidates["distance"] = abs(candidates.strike / candidates.underlying_price - 1)
            selected = candidates.sort_values(["distance", "instrument_name"]).iloc[0]
            instrument = str(selected.instrument_name)
            history = panel[panel.instrument_name == instrument].sort_values("snapshot_time")
            next_rows = history[history.snapshot_time > signal_time]
            if next_rows.empty:
                audit.append(
                    {
                        **record,
                        "instrument": instrument,
                        "status": "skipped",
                        "reason": "missing_next_snapshot",
                    }
                )
                continue
            entry = next_rows.iloc[0]
            if (
                entry.snapshot_time > signal_time + pd.Timedelta(hours=2)
                or not entry.two_sided
                or entry.relative_spread > 0.25
            ):
                audit.append(
                    {
                        **record,
                        "instrument": instrument,
                        "status": "skipped",
                        "reason": "next_snapshot_not_executable",
                    }
                )
                continue
            try:
                inverse_option_iv(
                    float(entry.mid_btc),
                    float(entry.underlying_price),
                    float(entry.strike),
                    float(entry.tau),
                    OptionType.parse(side),
                )
            except ImpliedVolError:
                audit.append(
                    {
                        **record,
                        "instrument": instrument,
                        "status": "skipped",
                        "reason": "entry_iv_outside_bounds",
                    }
                )
                continue
            case_id = f"{day.date().isoformat()}_{side}"
            cases.append(
                {
                    **record,
                    "case_id": case_id,
                    "instrument": instrument,
                    "signal_snapshot": selected.snapshot_time,
                    "entry": entry.to_dict(),
                    "expiry_time": expiry,
                    "quotes": history[
                        (history.snapshot_time >= entry.snapshot_time)
                        & (history.snapshot_time < expiry)
                    ].copy(),
                }
            )
            audit.append(
                {
                    **record,
                    "case_id": case_id,
                    "instrument": instrument,
                    "signal_snapshot": selected.snapshot_time.isoformat(),
                    "entry_quote_time": entry.snapshot_time.isoformat(),
                    "status": "entered",
                    "reason": "fixed_next_snapshot_entry",
                }
            )
    return cases, audit


def replay_case(
    case: dict,
    spot: pd.Series,
    delivery_price: float,
    policy: HistoricalPolicy,
    costs: HistoricalCosts,
) -> tuple[dict, list[dict]]:
    """Short ONE BTC contract, convert premium, spot hedge, settle official BTC payoff.

    USD/USDT parity, unconstrained financing/borrow availability and size-independent
    execution are assumptions. There is no synthetic market-path generation here.
    """
    if not isfinite(delivery_price) or delivery_price <= 0:
        raise HistoricalDataError("missing or invalid official delivery price")
    entry = case["entry"]
    expiry = pd.Timestamp(case["expiry_time"])
    option_type = OptionType.parse(str(entry["option_type"]))
    strike = float(entry["strike"])
    entry_quote_time = pd.Timestamp(entry["snapshot_time"])
    first_time = execution_time(entry_quote_time, costs.execution_lag_minutes)
    settlement_time = expiry + pd.Timedelta(minutes=costs.execution_lag_minutes)
    first_spot = spot_exact(spot, first_time)
    fill_btc = float(entry["bid_price"]) * (1 - costs.option_slippage_fraction)
    premium_usd = fill_btc * first_spot
    option_fee = min(costs.option_fee_btc, costs.fee_premium_cap * fill_btc) * first_spot
    conversion_cost = premium_usd * (costs.spot_fee_bps + costs.spot_slippage_bps) * 1e-4
    cash = premium_usd - option_fee - conversion_cost
    hedge = 0.0
    hedge_pnl = financing = hedge_cost = 0.0
    turnover = 0.0
    trade_count = stale_count = fallback_marks = delta_freezes = 0
    max_signal_age = 0.0
    last_time, last_spot, last_hedge = first_time, first_spot, first_time
    last_delta = 0.0
    last_delta_time = entry_quote_time
    last_mark_btc = float(entry["mid_btc"])
    ledger = []
    total_mark_count = 0

    for raw in case["quotes"].to_dict("records"):
        source_time = pd.Timestamp(raw["snapshot_time"])
        now = execution_time(source_time, costs.execution_lag_minutes)
        if now >= expiry - pd.Timedelta(minutes=30):
            continue  # Do not pretend an ordinary delta hedge models the settlement TWAP.
        if now < first_time or (now <= last_time and ledger):
            raise HistoricalDataError("non-chronological execution events")
        current_spot = spot_exact(spot, now)
        dt = (now - last_time).total_seconds() / YEAR_SECONDS
        funding = min(cash, 0.0) * (exp(costs.annual_borrow_rate * dt) - 1)
        funding -= max(-hedge, 0.0) * last_spot * (exp(costs.annual_borrow_rate * dt) - 1)
        cash += funding
        financing += funding
        hedge_pnl += hedge * (current_spot - last_spot)
        valid = bool(raw["two_sided"]) and float(raw["relative_spread"]) <= 0.5
        if valid:
            try:
                vol = inverse_option_iv(
                    float(raw["mid_btc"]),
                    float(raw["underlying_price"]),
                    strike,
                    float(raw["tau"]),
                    option_type,
                )
                # Delta known at the snapshot; execution uses the next minute, not its close.
                last_delta = inverse_usd_spot_delta(
                    float(raw["underlying_price"]), strike, float(raw["tau"]), vol, option_type
                )
                last_delta_time = source_time
            except (ImpliedVolError, ValueError):
                valid = False
        if not valid:
            delta_freezes += 1
        if bool(raw["two_sided"]):
            last_mark_btc = float(raw["mid_btc"])
            mark_source = "observed_mid"
        elif isfinite(float(raw["mark_price"])) and float(raw["mark_price"]) >= 0:
            last_mark_btc = float(raw["mark_price"])
            mark_source = "exchange_mark_not_fill"
            fallback_marks += 1
        else:
            mark_source = "stale_mark_not_fill"
            fallback_marks += 1
        total_mark_count += 1
        age = (now - last_delta_time).total_seconds() / 3600
        max_signal_age = max(max_signal_age, age)
        stale = age > costs.max_delta_age_hours
        stale_count += int(stale)
        target = round(last_delta / 0.001) * 0.001  # Short option quantity is -1.
        interval_due = policy.interval_hours is not None and (
            (now - last_hedge).total_seconds() >= policy.interval_hours * 3600
        )
        band_due = policy.delta_band is not None and abs(target - hedge) >= policy.delta_band
        rebalance = policy.active and not stale and (not ledger or interval_due or band_due)
        quantity = target - hedge if rebalance else 0.0
        transaction_cost = (
            abs(quantity) * current_spot * (costs.spot_fee_bps + costs.spot_slippage_bps) * 1e-4
        )
        if quantity:
            cash -= quantity * current_spot + transaction_cost
            hedge = target
            trade_count += 1
            hedge_cost += transaction_cost
            turnover += abs(quantity) * current_spot
        if rebalance:
            last_hedge = now
        equity = cash + hedge * current_spot - last_mark_btc * current_spot
        ledger.append(
            {
                "case_id": case["case_id"],
                "instrument": case["instrument"],
                "scenario": costs.name,
                "policy": policy.name,
                "source_time": source_time.isoformat(),
                "execution_time": now.isoformat(),
                "spot_open": current_spot,
                "observed_mark_btc": last_mark_btc,
                "mark_source": mark_source,
                "long_option_delta": last_delta,
                "delta_source_time": last_delta_time.isoformat(),
                "delta_age_hours": age,
                "stale_delta": stale,
                "trade_btc": quantity,
                "hedge_btc": hedge,
                "net_delta": hedge - last_delta,
                "cash_usd": cash,
                "equity_usd": equity,
                "hedge_transaction_cost": transaction_cost,
                "financing_cashflow": funding,
                "option_premium_cashflow": premium_usd if not ledger else 0.0,
                "option_fee_cashflow": -option_fee if not ledger else 0.0,
                "conversion_cashflow": -conversion_cost if not ledger else 0.0,
                "spot_trade_cashflow": -quantity * current_spot - transaction_cost,
                "settlement_cashflow": 0.0,
                "delivery_fee_cashflow": 0.0,
                "event": "entry" if not ledger else "mark_and_hedge",
            }
        )
        last_time, last_spot = now, current_spot

    if not ledger:
        raise HistoricalDataError("no pre-expiry observations for entered contract")
    terminal_spot = spot_exact(spot, settlement_time)
    dt = (settlement_time - last_time).total_seconds() / YEAR_SECONDS
    funding = min(cash, 0.0) * (exp(costs.annual_borrow_rate * dt) - 1)
    funding -= max(-hedge, 0.0) * last_spot * (exp(costs.annual_borrow_rate * dt) - 1)
    cash += funding
    financing += funding
    hedge_pnl += hedge * (terminal_spot - last_spot)
    payoff_btc = max(option_type.sign * (delivery_price - strike), 0.0) / delivery_price
    settlement_usd = payoff_btc * terminal_spot
    delivery_fee = min(costs.delivery_fee_btc, costs.fee_premium_cap * payoff_btc) * terminal_spot
    # Settlement payment and hedge close share a conservative BTC/USD conversion cost.
    settlement_conversion = settlement_usd * (costs.spot_fee_bps + costs.spot_slippage_bps) * 1e-4
    close_cost = abs(hedge) * terminal_spot * (costs.spot_fee_bps + costs.spot_slippage_bps) * 1e-4
    cash += hedge * terminal_spot - close_cost - settlement_usd - delivery_fee
    cash -= settlement_conversion
    trade_count += int(hedge != 0)
    hedge_cost += close_cost
    turnover += abs(hedge) * terminal_spot
    identity = (
        premium_usd
        - settlement_usd
        - option_fee
        - delivery_fee
        - conversion_cost
        - settlement_conversion
        + hedge_pnl
        + financing
        - hedge_cost
    )
    error = cash - identity
    if abs(error) > 1e-7:
        raise ArithmeticError(f"historical PnL accounting failed: {error}")
    ledger.append(
        {
            **ledger[-1],
            "source_time": expiry.isoformat(),
            "execution_time": settlement_time.isoformat(),
            "spot_open": terminal_spot,
            "observed_mark_btc": 0.0,
            "mark_source": "official_delivery",
            "trade_btc": -hedge,
            "hedge_btc": 0.0,
            "net_delta": 0.0,
            "long_option_delta": 0.0,
            "delta_age_hours": 0.0,
            "delta_source_time": expiry.isoformat(),
            "stale_delta": False,
            "cash_usd": cash,
            "equity_usd": cash,
            "hedge_transaction_cost": close_cost,
            "financing_cashflow": funding,
            "option_premium_cashflow": 0.0,
            "option_fee_cashflow": 0.0,
            "conversion_cashflow": -settlement_conversion,
            "spot_trade_cashflow": hedge * terminal_spot - close_cost,
            "settlement_cashflow": -settlement_usd,
            "delivery_fee_cashflow": -delivery_fee,
            "event": "official_settlement_and_close",
        }
    )
    # A second identity reconstructs cash independently from the serialized event cash flows.
    reconstructed_cash = 0.0
    max_cash_error = 0.0
    cash_fields = (
        "option_premium_cashflow",
        "option_fee_cashflow",
        "conversion_cashflow",
        "spot_trade_cashflow",
        "settlement_cashflow",
        "delivery_fee_cashflow",
        "financing_cashflow",
    )
    for row in ledger:
        reconstructed_cash += sum(float(row[key]) for key in cash_fields)
        max_cash_error = max(max_cash_error, abs(reconstructed_cash - float(row["cash_usd"])))
    if max_cash_error > 1e-7:
        raise ArithmeticError("serialized event cash flows do not reconstruct the cash account")
    equities = [0.0, *(float(row["equity_usd"]) for row in ledger)]
    peak, drawdown = 0.0, 0.0
    for equity in equities:
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    summary = {
        "case_id": case["case_id"],
        "week": case["week"],
        "instrument": case["instrument"],
        "side": case["side"],
        "scenario": costs.name,
        "policy": policy.name,
        "entry_time": first_time.isoformat(),
        "expiry": expiry.isoformat(),
        "split": "calibration"
        if expiry <= pd.Timestamp("2024-04-19 08:00", tz="UTC")
        else "holdout",
        "entry_spot": first_spot,
        "strike": strike,
        "entry_bid_btc": float(entry["bid_price"]),
        "fill_btc": fill_btc,
        "premium_usd": premium_usd,
        "official_delivery_price": delivery_price,
        "expiry_spot": terminal_spot,
        "payoff_btc": payoff_btc,
        "settlement_usd": settlement_usd,
        "option_fee_usd": option_fee,
        "delivery_fee_usd": delivery_fee,
        "premium_conversion_cost": conversion_cost,
        "settlement_conversion_cost": settlement_conversion,
        "hedge_cost_usd": hedge_cost,
        "hedge_pnl_usd": hedge_pnl,
        "financing_usd": financing,
        "terminal_pnl_usd": cash,
        "pnl_per_entry_notional_bps": cash / first_spot * 10000,
        "hedge_trade_count": trade_count,
        "hedge_turnover_usd": turnover,
        "max_drawdown_usd": drawdown,
        "max_abs_reconciliation_error": abs(error),
        "max_cashflow_reconstruction_error": max_cash_error,
        "stale_delta_events": stale_count,
        "frozen_delta_events": delta_freezes,
        "fallback_mark_events": fallback_marks,
        "mark_events": total_mark_count,
        "max_delta_age_hours": max_signal_age,
        "max_option_observation_gap_hours": float(
            case["quotes"].snapshot_time.diff().dt.total_seconds().max() / 3600
        ),
    }
    return summary, ledger
