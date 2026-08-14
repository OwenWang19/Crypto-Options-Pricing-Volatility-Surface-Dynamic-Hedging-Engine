from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from crypto_options.hedging.paths import StochasticVolatilityPathConfig, generate_market_path
from crypto_options.hedging.simulator import DeltaHedgingSimulator, HedgePolicy, MarketEvent
from crypto_options.instruments import EuropeanOption, OptionType


def _events() -> tuple[MarketEvent, ...]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    expiry = start + timedelta(days=2)
    return (
        MarketEvent(start, 100.0, 0.60, 0.0, 0.0),
        MarketEvent(start + timedelta(days=1), 108.0, 0.64, 0.0, 0.0),
        MarketEvent(expiry, 103.0, 0.58, 0.0, 0.0),
    )


def test_self_financing_ledger_reconciles_to_terminal_equity() -> None:
    events = _events()
    option = EuropeanOption(100.0, events[-1].timestamp, OptionType.CALL)
    policy = HedgePolicy(
        rebalance_interval=timedelta(hours=12),
        half_spread_bps=2.0,
        fee_bps=3.0,
        hedge_lot_size=0.0,
    )
    result = DeltaHedgingSimulator(policy).run(option, -2.0, events)
    assert result.ledger[-1].option_mark == 0.0
    assert result.ledger[-1].hedge_units == 0.0
    assert abs(result.summary.reconciliation_error) < 1e-10
    assert result.summary.transaction_costs > 0


def test_execution_costs_reduce_terminal_pnl_by_accounted_cost() -> None:
    events = _events()
    option = EuropeanOption(100.0, events[-1].timestamp, OptionType.CALL)
    free = HedgePolicy(
        rebalance_interval=timedelta(hours=12),
        half_spread_bps=0.0,
        fee_bps=0.0,
        hedge_lot_size=0.0,
    )
    costly = HedgePolicy(
        rebalance_interval=timedelta(hours=12),
        half_spread_bps=2.0,
        fee_bps=3.0,
        hedge_lot_size=0.0,
    )
    free_result = DeltaHedgingSimulator(free).run(option, -1.0, events)
    costly_result = DeltaHedgingSimulator(costly).run(option, -1.0, events)
    difference = free_result.summary.terminal_pnl - costly_result.summary.terminal_pnl
    assert difference == pytest.approx(costly_result.summary.transaction_costs, abs=1e-12)


def test_unhedged_constant_spot_short_call_keeps_initial_premium_when_otm() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    expiry = start + timedelta(days=30)
    events = (
        MarketEvent(start, 100.0, 0.5),
        MarketEvent(expiry, 100.0, 0.5),
    )
    option = EuropeanOption(110.0, expiry, OptionType.CALL)
    policy = HedgePolicy(rebalance_interval=None, delta_band=None, initial_hedge=False)
    result = DeltaHedgingSimulator(policy).run(option, -1.0, events)
    assert result.summary.terminal_pnl > 0
    assert result.summary.trade_count == 0
    assert abs(result.summary.reconciliation_error) < 1e-12


def test_market_path_is_seed_reproducible_and_hits_expiry_exactly() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    expiry = start + timedelta(days=30)
    config = StochasticVolatilityPathConfig(60_000.0, 0.6)
    first = generate_market_path(start, expiry, 24, config, seed=9)
    second = generate_market_path(start, expiry, 24, config, seed=9)
    assert first == second
    assert first[-1].timestamp == expiry


def test_simulator_rejects_missing_expiry_observation() -> None:
    events = _events()[:-1]
    option = EuropeanOption(100.0, _events()[-1].timestamp, OptionType.CALL)
    with pytest.raises(ValueError, match="exactly at expiry"):
        DeltaHedgingSimulator(HedgePolicy()).run(option, -1.0, events)
