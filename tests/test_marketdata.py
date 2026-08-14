from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from crypto_options.instruments import OptionType
from crypto_options.marketdata.chain import (
    OptionQuote,
    clean_option_chain,
    estimate_forward_from_put_call_parity,
)
from crypto_options.marketdata.deribit import DeribitClient
from crypto_options.research.synthetic import generate_synthetic_option_chain


def test_synthetic_chain_cleans_and_parity_recovers_forwards() -> None:
    quotes = generate_synthetic_option_chain(seed=3)
    result = clean_option_chain(quotes)
    assert result.total_quotes == 104
    assert result.accepted_quotes >= 100
    assert result.rejection_counts.get("wide_relative_spread", 0) <= 4

    estimates = estimate_forward_from_put_call_parity(quotes)
    assert len(estimates) == 4
    for estimate in estimates:
        matching = next(
            quote for quote in quotes if quote.time_to_expiry == estimate.time_to_expiry
        )
        assert estimate.forward == pytest.approx(matching.forward, rel=1e-12)
        assert estimate.dispersion < 1e-8


def test_cleaner_reports_rejections_without_substitution() -> None:
    base = dict(
        time_to_expiry=0.25,
        strike=100.0,
        option_type=OptionType.CALL,
        forward=100.0,
    )
    quotes = [
        OptionQuote("crossed", bid=10.0, ask=9.0, **base),
        OptionQuote("negative", bid=-1.0, ask=2.0, **base),
        OptionQuote("wide", bid=0.1, ask=10.0, **base),
    ]
    result = clean_option_chain(quotes)
    assert result.accepted_quotes == 0
    assert result.rejection_counts == {
        "crossed_or_locked": 1,
        "non_positive_quote": 1,
        "wide_relative_spread": 1,
    }


def test_deribit_adapter_joins_metadata_and_converts_base_premium(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    expiry = now + timedelta(days=30)
    instruments = [
        {
            "instrument_name": "BTC-TEST-60000-C",
            "expiration_timestamp": int(expiry.timestamp() * 1000),
            "strike": 60_000,
            "option_type": "call",
        }
    ]
    summaries = [
        {
            "instrument_name": "BTC-TEST-60000-C",
            "bid_price": 0.05,
            "ask_price": 0.055,
            "underlying_price": 61_000,
            "interest_rate": 0.03,
            "open_interest": 125,
        }
    ]
    index_result = {"index_price": 59_000}
    client = DeribitClient(max_attempts=1)

    def fake_get(method: str, params: dict[str, str]) -> object:
        if method == "get_index_price":
            assert params["index_name"] == "btc_usd"
            return index_result
        assert params["currency"] == "BTC"
        if method == "get_instruments":
            return instruments
        assert method == "get_book_summary_by_currency"
        return summaries

    monkeypatch.setattr(client, "_get", fake_get)
    snapshot = client.option_snapshot("BTC", now=now)
    assert snapshot.skipped_instruments == 0
    assert len(snapshot.quotes) == 1
    quote = snapshot.quotes[0]
    assert snapshot.index_price == 59_000
    assert quote.bid == pytest.approx(0.05 * 59_000)
    assert quote.ask == pytest.approx(0.055 * 59_000)
    assert quote.option_type is OptionType.CALL
