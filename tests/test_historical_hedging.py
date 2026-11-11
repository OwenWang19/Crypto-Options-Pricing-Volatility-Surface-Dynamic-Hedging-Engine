from __future__ import annotations

from dataclasses import replace
from math import exp

import numpy as np
import pandas as pd
import pytest

from crypto_options.hedging.historical import (
    POLICIES,
    HistoricalCosts,
    HistoricalPolicy,
    execution_time,
    historical_opportunities,
    inverse_option_iv,
    inverse_usd_spot_delta,
    replay_case,
)
from crypto_options.instruments import OptionType
from crypto_options.marketdata.historical import HistoricalDataError, normalize_option_panel
from crypto_options.pricing.black76 import black76_price


def make_case(side="C"):
    start = pd.Timestamp("2024-01-19 08:00:10", tz="UTC")
    expiry = pd.Timestamp("2024-01-19 14:00", tz="UTC")
    times = pd.date_range(start.floor("min"), expiry + pd.Timedelta(minutes=5), freq="min")
    spot = pd.Series(60000 + 300 * np.sin(np.arange(len(times)) / 100), index=times)
    rows = []
    for hour in range(6):
        timestamp = start + pd.Timedelta(hours=hour)
        tau = (expiry - timestamp).total_seconds() / 31536000
        forward = float(spot.loc[timestamp.floor("min")]) * exp(0.05 * tau)
        premium = black76_price(forward, 60000, tau, 0.6, 1, OptionType.parse(side)) / forward
        rows.append(
            {
                "snapshot_time": timestamp,
                "expiry_date": expiry,
                "instrument_name": f"BTC-19JAN24-60000-{side}",
                "option_type": side,
                "strike": 60000.0,
                "tau": tau,
                "underlying_price": forward,
                "bid_price": premium * 0.99,
                "ask_price": premium * 1.01,
                "mid_btc": premium,
                "relative_spread": 0.02,
                "two_sided": True,
                "mark_price": premium,
            }
        )
    frame = pd.DataFrame(rows)
    case = {
        "case_id": "2024-01-19_" + side,
        "week": "2024-01-19",
        "side": side,
        "instrument": rows[0]["instrument_name"],
        "entry": rows[0],
        "expiry_time": expiry,
        "quotes": frame,
    }
    return case, spot


@pytest.mark.parametrize("side", [OptionType.CALL, OptionType.PUT])
def test_inverse_units_iv_and_delta_finite_difference(side):
    forward, spot, strike, tau, vol = 61000.0, 60000.0, 62000.0, 0.1, 0.65
    premium_btc = black76_price(forward, strike, tau, vol, 1, side) / forward
    assert inverse_option_iv(premium_btc, forward, strike, tau, side) == pytest.approx(vol)

    def usd_value(s):
        f = forward * s / spot
        return s / f * black76_price(f, strike, tau, vol, 1, side)

    numeric = (usd_value(spot + 0.1) - usd_value(spot - 0.1)) / 0.2
    assert inverse_usd_spot_delta(forward, strike, tau, vol, side) == pytest.approx(
        numeric, rel=1e-8
    )
    with pytest.raises(ValueError):
        inverse_usd_spot_delta(forward, strike, 0, vol, side)


def test_execution_is_strictly_after_signal():
    timestamp = pd.Timestamp("2024-01-19 09:00:10", tz="UTC")
    assert execution_time(timestamp, 1) == pd.Timestamp("2024-01-19 09:01", tz="UTC")
    assert execution_time(timestamp.floor("min"), 1) > timestamp.floor("min")
    with pytest.raises(ValueError):
        execution_time(timestamp, 0)


@pytest.mark.parametrize("side", ["C", "P"])
@pytest.mark.parametrize("policy", POLICIES)
def test_all_policies_reconcile_and_use_observed_entry(side, policy):
    case, spot = make_case(side)
    result, ledger = replay_case(case, spot, 62000.0, policy, HistoricalCosts())
    assert result["max_abs_reconciliation_error"] < 1e-8
    assert result["fill_btc"] == pytest.approx(case["entry"]["bid_price"] * 0.99)
    assert all(
        pd.Timestamp(row["source_time"]) < pd.Timestamp(row["execution_time"]) for row in ledger
    )
    assert ledger[-1]["hedge_btc"] == ledger[-1]["observed_mark_btc"] == 0
    expected = (
        result["premium_usd"]
        - result["settlement_usd"]
        - result["option_fee_usd"]
        - result["delivery_fee_usd"]
        - result["premium_conversion_cost"]
        - result["settlement_conversion_cost"]
        + result["hedge_pnl_usd"]
        - result["hedge_cost_usd"]
        + result["financing_usd"]
    )
    assert result["terminal_pnl_usd"] == pytest.approx(expected, abs=1e-8)


def test_official_delivery_is_not_spot_expiry_and_btc_units_matter():
    case, spot = make_case("C")
    result, _ = replay_case(case, spot, 62000.0, POLICIES[0], HistoricalCosts())
    assert result["payoff_btc"] == pytest.approx(2000 / 62000)
    assert result["settlement_usd"] == pytest.approx(2000 / 62000 * result["expiry_spot"])
    assert result["settlement_usd"] != pytest.approx(2000)
    with pytest.raises(HistoricalDataError):
        replay_case(case, spot, float("nan"), POLICIES[0], HistoricalCosts())


def test_future_quotes_cannot_change_earlier_decisions():
    case, spot = make_case()
    _, original = replay_case(case, spot, 62000.0, POLICIES[1], HistoricalCosts())
    changed = {**case, "quotes": case["quotes"].copy()}
    changed["quotes"].loc[4:, ["bid_price", "ask_price", "mid_btc"]] *= 1.5
    _, modified = replay_case(changed, spot, 62000.0, POLICIES[1], HistoricalCosts())
    assert original[:4] == modified[:4]


def test_missing_two_sided_quotes_freeze_delta_without_dropping_the_case():
    case, spot = make_case()
    case["quotes"].loc[1:, "two_sided"] = False
    case["quotes"].loc[3:, "mark_price"] = float("nan")
    result, ledger = replay_case(case, spot, 62000.0, POLICIES[1], HistoricalCosts())
    assert result["frozen_delta_events"] == 5
    assert result["stale_delta_events"] > 0
    assert result["fallback_mark_events"] == 5
    assert {x["mark_source"] for x in ledger} >= {"stale_mark_not_fill", "exchange_mark_not_fill"}
    assert result["max_abs_reconciliation_error"] < 1e-8


def test_cost_stress_does_not_improve_unhedged_pnl_and_financing_is_charged():
    case, spot = make_case()
    costs = HistoricalCosts()
    base, _ = replay_case(case, spot, 62000.0, POLICIES[0], costs)
    stress, _ = replay_case(
        case,
        spot,
        62000.0,
        POLICIES[0],
        replace(costs, spot_fee_bps=20, option_slippage_fraction=0.05),
    )
    assert stress["terminal_pnl_usd"] < base["terminal_pnl_usd"]
    financed, _ = replay_case(
        case, spot, 62000.0, POLICIES[1], replace(costs, annual_borrow_rate=0.1)
    )
    assert financed["financing_usd"] < 0
    put, prices = make_case("P")
    short_spot, _ = replay_case(
        put, prices, 58000.0, POLICIES[1], replace(costs, annual_borrow_rate=0.1)
    )
    assert short_spot["financing_usd"] < 0


def test_missing_spot_does_not_get_interpolated():
    case, spot = make_case()
    with pytest.raises(HistoricalDataError, match="missing execution minute"):
        replay_case(
            case,
            spot.drop(execution_time(case["entry"]["snapshot_time"], 1)),
            62000.0,
            POLICIES[1],
            HistoricalCosts(),
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"spot_fee_bps": -1},
        {"spot_fee_bps": float("nan")},
        {"option_slippage_fraction": 1},
        {"fee_premium_cap": 0},
        {"execution_lag_minutes": 0},
        {"max_delta_age_hours": 0},
    ],
)
def test_bad_costs_fail(kwargs):
    with pytest.raises(ValueError):
        HistoricalCosts(**kwargs)


@pytest.mark.parametrize("kwargs", [{}, {"interval_hours": -1}, {"delta_band": float("nan")}])
def test_bad_policy_fails(kwargs):
    with pytest.raises(ValueError):
        HistoricalPolicy("bad", **kwargs)


def test_selection_never_changes_instrument_using_next_hour():
    rows = []
    for strike in (60000, 61000):
        for hour in (8, 9):
            rows.append(
                {
                    "snapshot_time": f"2024-01-19 {hour:02d}:00:10",
                    "instrument_name": f"BTC-26JAN24-{strike}-C",
                    "mark_price": 0.03,
                    "bid_price": 0.029,
                    "ask_price": 0.031,
                    "underlying_index": "BTC-26JAN24",
                    "underlying_price": 60001 if hour == 8 else 61000,
                    "open_interest": 20,
                    "volume": 1,
                    "estimated_delivery_price": 59900,
                    "expiry_date": "2024-01-26 08:00",
                }
            )
    cases, audit = historical_opportunities(normalize_option_panel(pd.DataFrame(rows)))
    assert len(cases) == 1
    assert cases[0]["instrument"] == "BTC-26JAN24-60000-C"
    assert len(audit) == 54
    assert sum(row["status"] == "entered" for row in audit) == 1
    # Bad NEXT quote cannot trigger substitution into a more convenient contract.
    rows[1]["bid_price"] = None
    cases, audit = historical_opportunities(normalize_option_panel(pd.DataFrame(rows)))
    assert not cases
    assert any(x["reason"] == "next_snapshot_not_executable" for x in audit)


def test_missing_or_impossible_next_option_quote_is_audited():
    from test_historical_data import raw_frame

    signal = raw_frame()
    cases, audit = historical_opportunities(normalize_option_panel(signal))
    assert not cases
    assert any(x["reason"] == "missing_next_snapshot" for x in audit)
    next_quote = signal.copy()
    next_quote["snapshot_time"] = "2024-01-19 09:00:10"
    next_quote["bid_price"], next_quote["ask_price"] = 1.1, 1.2
    cases, audit = historical_opportunities(normalize_option_panel(pd.concat([signal, next_quote])))
    assert not cases
    assert any(x["reason"] == "entry_iv_outside_bounds" for x in audit)
