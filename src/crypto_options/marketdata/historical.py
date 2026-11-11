"""Public historical data, immutable provenance, and strict point-in-time normalization."""

from __future__ import annotations

import hashlib
import json
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

OPTION_FILE = "btc_option_data_toshare.parquet"
OPTION_SHA256 = "6ed2162288d177d3c3a5443456c3fb78ed2c175249f0a0ff1665a5dd9c89800b"
OPTION_URL = (
    "https://drive.usercontent.google.com/download"
    "?id=1g9p2Kq8op40y4ZFQQ9AJw8a9CaDThOY8&export=download"
)
ARTICLE_URL = "https://insights.deribit.com/education/option-backtest-selling-weekend-vol/"
AUTHOR_URL = "https://x.com/cryptarbitrage/status/1817888742650085616"
BINANCE_ROOT = "https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1m/"
DELIVERY_ROOT = "https://www.deribit.com/api/v2/public/get_delivery_prices"
OPTION_COLUMNS = [
    "snapshot_time",
    "instrument_name",
    "mark_price",
    "bid_price",
    "ask_price",
    "underlying_index",
    "underlying_price",
    "open_interest",
    "volume",
    "estimated_delivery_price",
    "expiry_date",
]


class HistoricalDataError(ValueError):
    """A data-integrity failure: do not silently manufacture missing observations."""


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _request_bytes(url: str) -> bytes:
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            request = Request(url, headers={"User-Agent": "crypto-options-research/0.2"})
            with urlopen(request, timeout=60) as response:
                return response.read()
        except (OSError, URLError) as exc:
            last_error = exc
            time.sleep(0.5 * 2**attempt)
    raise HistoricalDataError(f"public download failed: {url}") from last_error


def _download(url: str, path: Path, expected_hash: str | None = None) -> dict[str, object]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        temporary = path.with_suffix(path.suffix + ".part")
        temporary.write_bytes(_request_bytes(url))
        if expected_hash and sha256_file(temporary) != expected_hash:
            raise HistoricalDataError(f"SHA-256 mismatch for {path.name}; partial file retained")
        temporary.replace(path)
    actual_hash = sha256_file(path)
    if expected_hash and actual_hash != expected_hash:
        raise HistoricalDataError(f"SHA-256 mismatch for cached {path.name}")
    return {"file": path.name, "url": url, "bytes": path.stat().st_size, "sha256": actual_hash}


def download_historical_data(raw_dir: Path) -> dict[str, object]:
    """Download the fixed Jan-Jul 2024 panel, spot archives and delivery prices.

    No credentials, authenticated trading, paid subscriptions or synthetic fallbacks.
    Raw data are deliberately gitignored; public availability is not a redistribution license.
    """
    records = [_download(OPTION_URL, raw_dir / OPTION_FILE, OPTION_SHA256)]
    for month in range(1, 8):
        filename = f"BTCUSDT-1m-2024-{month:02d}.zip"
        checksum_path = raw_dir / (filename + ".CHECKSUM")
        _download(BINANCE_ROOT + filename + ".CHECKSUM", checksum_path)
        expected = checksum_path.read_text().split()[0]
        if len(expected) != 64:
            raise HistoricalDataError("invalid Binance checksum")
        records.append(_download(BINANCE_ROOT + filename, raw_dir / filename, expected))
    delivery_path = raw_dir / "delivery_prices.json"
    if not delivery_path.exists():
        deliveries: dict[str, float] = {}
        offset = 0
        while True:
            params = {
                "index_name": "btc_usd",
                "start_timestamp": 1704067200000,
                "end_timestamp": 1722470400000,
                "offset": offset,
                "count": 100,
            }
            result = json.loads(_request_bytes(DELIVERY_ROOT + "?" + urlencode(params)))
            if "error" in result:
                raise HistoricalDataError(f"delivery API error: {result['error']}")
            page = result["result"]
            rows = page["data"]
            if not rows:
                raise HistoricalDataError("delivery API ended before reported record count")
            for row in rows:
                deliveries[row["date"]] = row["delivery_price"]
            offset += len(rows)
            if offset >= page["records_total"]:
                break
        delivery_path.write_text(json.dumps(deliveries, indent=2, sort_keys=True) + "\n")
    records.append(
        {
            "file": delivery_path.name,
            "url": DELIVERY_ROOT,
            "params": {
                "index_name": "btc_usd",
                "start_timestamp": 1704067200000,
                "end_timestamp": 1722470400000,
                "count": 100,
                "pagination": "offset",
            },
            "bytes": delivery_path.stat().st_size,
            "sha256": sha256_file(delivery_path),
        }
    )
    manifest: dict[str, object] = {
        "downloaded_at_utc": datetime.now(UTC).isoformat(),
        "option_source_article": ARTICLE_URL,
        "option_source_author": AUTHOR_URL,
        "source_type": "third-party recorded Deribit hourly snapshots; not exchange-audited",
        "files": records,
        "redistribution": "Raw files are local only. No redistribution license assumed.",
    }
    (raw_dir / "provenance.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def normalize_option_panel(frame: pd.DataFrame) -> pd.DataFrame:
    """Source naive timestamps are explicitly interpreted as UTC, never local time."""
    missing = set(OPTION_COLUMNS) - set(frame.columns)
    if missing:
        raise HistoricalDataError(f"missing option columns: {sorted(missing)}")
    frame = frame[OPTION_COLUMNS].copy()
    for column in ("snapshot_time", "expiry_date"):
        frame[column] = pd.to_datetime(frame[column], utc=True, errors="raise")
    if frame[["snapshot_time", "expiry_date", "instrument_name"]].isna().any().any():
        raise HistoricalDataError("null option identity or timestamp")
    if frame.duplicated(["snapshot_time", "instrument_name"]).any():
        raise HistoricalDataError("duplicate option snapshot keys")
    parsed = frame.instrument_name.str.extract(r"^BTC-(\d{1,2}[A-Z]{3}\d{2})-(\d+)-(C|P)$")
    if parsed.isna().any().any():
        raise HistoricalDataError("unrecognized BTC inverse-option instrument")
    frame["strike"] = parsed[1].astype(float)
    frame["option_type"] = parsed[2]
    parsed_expiry = pd.to_datetime(parsed[0], format="%d%b%y", utc=True) + pd.Timedelta(hours=8)
    if not (parsed_expiry == frame.expiry_date).all():
        raise HistoricalDataError("instrument name and expiry_date disagree")
    frame["hour"] = frame.snapshot_time.dt.floor("h")
    frame["tau"] = (frame.expiry_date - frame.snapshot_time).dt.total_seconds() / 31536000
    frame["mid_btc"] = (frame.bid_price + frame.ask_price) / 2
    frame["relative_spread"] = (frame.ask_price - frame.bid_price) / frame.mid_btc
    frame["two_sided"] = (
        np.isfinite(frame.bid_price)
        & np.isfinite(frame.ask_price)
        & (frame.bid_price > 0)
        & (frame.ask_price > frame.bid_price)
        & np.isfinite(frame.underlying_price)
        & (frame.underlying_price > 0)
        & np.isfinite(frame.open_interest)
        & (frame.open_interest >= 0)
        & (frame.tau > 0)
    )
    return frame.sort_values(["snapshot_time", "instrument_name"]).reset_index(drop=True)


def load_option_panel(raw_dir: Path) -> tuple[pd.DataFrame, dict[str, object]]:
    path = raw_dir / OPTION_FILE
    if sha256_file(path) != OPTION_SHA256:
        raise HistoricalDataError("option dataset differs from the pinned source SHA-256")
    raw = pd.read_parquet(path, columns=OPTION_COLUMNS)
    frame = normalize_option_panel(raw)
    hours = pd.date_range(frame.hour.min(), frame.hour.max(), freq="h")
    audit: dict[str, object] = {
        "raw_rows": len(raw),
        "normalized_rows": len(frame),
        "start_utc": frame.snapshot_time.min().isoformat(),
        "end_utc": frame.snapshot_time.max().isoformat(),
        "instruments": int(frame.instrument_name.nunique()),
        "expiries": int(frame.expiry_date.nunique()),
        "null_counts": {k: int(v) for k, v in raw.isna().sum().items()},
        "two_sided_rows": int(frame.two_sided.sum()),
        "missing_panel_hours": [x.isoformat() for x in hours.difference(frame.hour.unique())],
        "timestamp_assumption": "source naive snapshot_time and expiry_date are UTC",
        "source_timestamp_is_not": "exchange quote update time; quote-age/depth unavailable",
        "sha256": OPTION_SHA256,
    }
    return frame, audit


def load_spot_opens(raw_dir: Path) -> pd.Series:
    """Only minute OPEN trades are used. Never use that minute's future close/high/low."""
    chunks = []
    for path in sorted(raw_dir.glob("BTCUSDT-1m-2024-*.zip")):
        checksum = path.with_suffix(".zip.CHECKSUM").read_text().split()[0]
        if sha256_file(path) != checksum:
            raise HistoricalDataError(f"spot archive checksum mismatch: {path.name}")
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if len(names) != 1 or not names[0].endswith(".csv"):
                raise HistoricalDataError("unexpected Binance archive structure")
            with archive.open(names[0]) as stream:
                chunk = pd.read_csv(stream, header=None, usecols=[0, 1], names=["time", "open"])
        timestamps = chunk.time.to_numpy(dtype=np.int64)
        # Binance switched spot archive timestamps from milliseconds to microseconds in 2025.
        unit = "us" if timestamps.min() > 10**14 else "ms"
        chunks.append(
            pd.Series(
                chunk.open.to_numpy(float),
                index=pd.to_datetime(timestamps, unit=unit, utc=True),
                name="spot_open",
            )
        )
    if not chunks:
        raise HistoricalDataError("no spot archives found; run download-historical first")
    prices = pd.concat(chunks).sort_index()
    if prices.index.has_duplicates or not np.isfinite(prices).all() or (prices <= 0).any():
        raise HistoricalDataError("duplicate or invalid spot observations")
    return prices


def spot_at_or_before(
    prices: pd.Series, timestamp: pd.Timestamp, max_age_seconds: int = 60
) -> float:
    index = prices.index.searchsorted(timestamp, side="right") - 1
    if index < 0 or (timestamp - prices.index[index]).total_seconds() > max_age_seconds:
        raise HistoricalDataError(f"missing as-of spot price at {timestamp}")
    return float(prices.iloc[index])


def spot_exact(prices: pd.Series, timestamp: pd.Timestamp) -> float:
    if timestamp not in prices.index:
        raise HistoricalDataError(f"missing execution minute: {timestamp}; no interpolation")
    return float(prices.loc[timestamp])
