from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from crypto_options.instruments import OptionType
from crypto_options.marketdata.chain import OptionQuote


class DeribitAPIError(RuntimeError):
    """Raised for transport errors, API errors, or inconsistent payloads."""


@dataclass(frozen=True, slots=True)
class DeribitSnapshot:
    currency: str
    captured_at: datetime
    index_price: float
    quotes: tuple[OptionQuote, ...]
    skipped_instruments: int


class DeribitClient:
    """Small unauthenticated REST client for point-in-time option snapshots."""

    def __init__(
        self,
        base_url: str = "https://www.deribit.com/api/v2",
        *,
        timeout_seconds: float = 10.0,
        max_attempts: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        if timeout_seconds <= 0 or max_attempts < 1:
            raise ValueError("timeout_seconds and max_attempts must be positive")

    def _get(self, method: str, params: dict[str, str]) -> Any:
        url = f"{self.base_url}/public/{method}?{urlencode(params)}"
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "crypto-options-engine/0.1",
            },
        )
        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if "error" in payload:
                    raise DeribitAPIError(f"Deribit error: {payload['error']}")
                if "result" not in payload:
                    raise DeribitAPIError("Deribit response omitted result")
                return payload["result"]
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt + 1 < self.max_attempts:
                    time.sleep(0.25 * 2**attempt)
        raise DeribitAPIError(f"request failed after {self.max_attempts} attempts: {last_error}")

    def option_snapshot(
        self,
        currency: str = "BTC",
        *,
        now: datetime | None = None,
    ) -> DeribitSnapshot:
        currency = currency.upper()
        if currency not in {"BTC", "ETH"}:
            raise ValueError("this adapter currently supports BTC and ETH inverse options")
        valuation_time = now or datetime.now(UTC)
        if valuation_time.tzinfo is None or valuation_time.utcoffset() is None:
            raise ValueError("now must be timezone-aware")

        instruments = self._get(
            "get_instruments", {"currency": currency, "kind": "option", "expired": "false"}
        )
        summaries = self._get(
            "get_book_summary_by_currency", {"currency": currency, "kind": "option"}
        )
        index_result = self._get("get_index_price", {"index_name": f"{currency.lower()}_usd"})
        try:
            index_price = float(index_result["index_price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise DeribitAPIError("get_index_price returned an invalid index_price") from exc
        if index_price <= 0:
            raise DeribitAPIError("get_index_price returned a non-positive index_price")
        metadata = {row["instrument_name"]: row for row in instruments}
        quotes: list[OptionQuote] = []
        skipped = 0

        for summary in summaries:
            try:
                instrument = metadata[summary["instrument_name"]]
                bid_base = summary["bid_price"]
                ask_base = summary["ask_price"]
                underlying = float(summary["underlying_price"])
                if bid_base is None or ask_base is None or underlying <= 0:
                    raise ValueError("incomplete two-sided quote")
                expiry = datetime.fromtimestamp(
                    float(instrument["expiration_timestamp"]) / 1000.0, tz=UTC
                )
                tau = (expiry - valuation_time.astimezone(UTC)).total_seconds() / (
                    365.0 * 24.0 * 60.0 * 60.0
                )
                if tau <= 0:
                    raise ValueError("expired")
                discount_factor = index_price / underlying
                # Inverse premiums are quoted in base currency. Deribit defines the USD
                # equivalent using the spot index, while implied volatility uses the
                # expiry forward (`underlying_price`) and D = exp(-rT) = index/forward.
                quotes.append(
                    OptionQuote(
                        instrument_name=summary["instrument_name"],
                        time_to_expiry=tau,
                        strike=float(instrument["strike"]),
                        option_type=OptionType.parse(instrument["option_type"]),
                        bid=float(bid_base) * index_price,
                        ask=float(ask_base) * index_price,
                        forward=underlying,
                        discount_factor=discount_factor,
                        open_interest=float(summary.get("open_interest") or 0.0),
                    )
                )
            except (KeyError, TypeError, ValueError):
                skipped += 1

        return DeribitSnapshot(currency, valuation_time, index_price, tuple(quotes), skipped)
