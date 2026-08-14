from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class OptionType(StrEnum):
    CALL = "call"
    PUT = "put"

    @property
    def sign(self) -> int:
        return 1 if self is OptionType.CALL else -1

    @classmethod
    def parse(cls, value: str) -> OptionType:
        normalized = value.strip().lower()
        aliases = {"c": cls.CALL, "call": cls.CALL, "p": cls.PUT, "put": cls.PUT}
        try:
            return aliases[normalized]
        except KeyError as exc:
            raise ValueError(f"Unsupported option type: {value!r}") from exc


@dataclass(frozen=True, slots=True)
class EuropeanOption:
    strike: float
    expiry: datetime
    option_type: OptionType
    contract_size: float = 1.0

    def __post_init__(self) -> None:
        if self.strike <= 0:
            raise ValueError("strike must be positive")
        if self.contract_size <= 0:
            raise ValueError("contract_size must be positive")
        if self.expiry.tzinfo is None or self.expiry.utcoffset() is None:
            raise ValueError("expiry must be timezone-aware")

    def time_to_expiry(self, valuation_time: datetime) -> float:
        if valuation_time.tzinfo is None or valuation_time.utcoffset() is None:
            raise ValueError("valuation_time must be timezone-aware")
        seconds = (self.expiry.astimezone(UTC) - valuation_time.astimezone(UTC)).total_seconds()
        return max(seconds, 0.0) / (365.0 * 24.0 * 60.0 * 60.0)

    def payoff(self, spot: float) -> float:
        if spot <= 0:
            raise ValueError("spot must be positive")
        return self.contract_size * max(self.option_type.sign * (spot - self.strike), 0.0)
