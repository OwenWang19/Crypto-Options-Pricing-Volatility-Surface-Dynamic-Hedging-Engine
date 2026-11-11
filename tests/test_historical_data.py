from __future__ import annotations

import hashlib
import io
import json
import zipfile
from contextlib import contextmanager
from urllib.error import URLError

import pandas as pd
import pytest

from crypto_options.marketdata import historical as data


def raw_frame():
    return pd.DataFrame(
        [
            {
                "snapshot_time": "2024-01-19 08:00:10",
                "instrument_name": "BTC-26JAN24-60000-C",
                "mark_price": 0.03,
                "bid_price": 0.029,
                "ask_price": 0.031,
                "underlying_index": "BTC-26JAN24",
                "underlying_price": 60000,
                "open_interest": 10,
                "volume": 1,
                "estimated_delivery_price": 59900,
                "expiry_date": "2024-01-26 08:00:00",
            }
        ]
    )


def test_normalization_utc_and_quality():
    frame = data.normalize_option_panel(raw_frame())
    assert str(frame.snapshot_time.dt.tz) == "UTC"
    assert frame.iloc[0].two_sided
    assert frame.iloc[0].strike == 60000
    frame = raw_frame()
    frame.loc[0, "bid_price"] = float("nan")
    assert not data.normalize_option_panel(frame).iloc[0].two_sided


@pytest.mark.parametrize("change", ["missing", "duplicate", "symbol", "expiry", "null"])
def test_panel_corruption_fails(change):
    frame = raw_frame()
    if change == "missing":
        frame = frame.drop(columns="bid_price")
    elif change == "duplicate":
        frame = pd.concat([frame, frame])
    elif change == "symbol":
        frame.loc[0, "instrument_name"] = "ETH-PERPETUAL"
    elif change == "expiry":
        frame.loc[0, "expiry_date"] = "2024-01-27 08:00"
    else:
        frame.loc[0, "snapshot_time"] = None
    with pytest.raises(data.HistoricalDataError):
        data.normalize_option_panel(frame)


def test_pinned_panel_and_audit(tmp_path, monkeypatch):
    path = tmp_path / data.OPTION_FILE
    raw_frame().to_parquet(path, index=False)
    with pytest.raises(data.HistoricalDataError, match="SHA-256"):
        data.load_option_panel(tmp_path)
    monkeypatch.setattr(data, "OPTION_SHA256", data.sha256_file(path))
    frame, audit = data.load_option_panel(tmp_path)
    assert len(frame) == audit["raw_rows"] == 1
    assert not audit["missing_panel_hours"]


def test_asof_prices_never_look_forward():
    prices = pd.Series(
        [100.0, 200.0], index=pd.date_range("2024-01-01", periods=2, freq="min", tz="UTC")
    )
    at = prices.index[0] + pd.Timedelta(seconds=59)
    assert data.spot_at_or_before(prices, at) == 100
    with pytest.raises(data.HistoricalDataError):
        data.spot_at_or_before(prices, prices.index[0] - pd.Timedelta(seconds=1))
    with pytest.raises(data.HistoricalDataError):
        data.spot_at_or_before(prices, prices.index[-1] + pd.Timedelta(minutes=2))
    with pytest.raises(data.HistoricalDataError):
        data.spot_exact(prices, at)
    assert data.spot_exact(prices, prices.index[0]) == 100


def make_spot_zip(path, *, micros=False, duplicate=False, invalid=False):
    start = 1705622400000 * (1000 if micros else 1)
    interval = 60000 * (1000 if micros else 1)
    price = -1 if invalid else 60000
    content = f"{start},{price},0,0\n{start if duplicate else start + interval},60001,0,0\n"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("test.csv", content)
    path.with_suffix(".zip.CHECKSUM").write_text(data.sha256_file(path) + "  test.zip")


@pytest.mark.parametrize("micros", [False, True])
def test_spot_archive_units_and_checksum(tmp_path, micros):
    path = tmp_path / "BTCUSDT-1m-2024-01.zip"
    make_spot_zip(path, micros=micros)
    prices = data.load_spot_opens(tmp_path)
    assert prices.iloc[0] == 60000
    assert prices.index[1] - prices.index[0] == pd.Timedelta(minutes=1)
    path.with_suffix(".zip.CHECKSUM").write_text("bad")
    with pytest.raises(data.HistoricalDataError, match="checksum"):
        data.load_spot_opens(tmp_path)


@pytest.mark.parametrize("bad", ["missing", "duplicate", "invalid", "structure"])
def test_spot_archive_rejection(tmp_path, bad):
    path = tmp_path / "BTCUSDT-1m-2024-01.zip"
    if bad != "missing":
        make_spot_zip(path, duplicate=bad == "duplicate", invalid=bad == "invalid")
    if bad == "structure":
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("second.csv", "1,2")
        path.with_suffix(".zip.CHECKSUM").write_text(data.sha256_file(path))
    with pytest.raises(data.HistoricalDataError):
        data.load_spot_opens(tmp_path)


def test_download_caching_integrity_and_paginated_delivery(tmp_path, monkeypatch):
    payload = b"pinned-option-file"
    option_hash = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(data, "OPTION_SHA256", option_hash)
    calls = []

    def request(url):
        calls.append(url)
        if "CHECKSUM" in url:
            return (option_hash + "  archive.zip").encode()
        if "get_delivery_prices" in url:
            rows = [{"date": "2024-01-01", "delivery_price": 40000}]
            if "offset=0" not in url:
                rows = [{"date": "2024-01-02", "delivery_price": 41000}]
            return json.dumps({"result": {"data": rows, "records_total": 2}}).encode()
        return payload

    monkeypatch.setattr(data, "_request_bytes", request)
    manifest = data.download_historical_data(tmp_path)
    assert len(manifest["files"]) == 9
    assert len(json.loads((tmp_path / "delivery_prices.json").read_text())) == 2
    assert sum("get_delivery_prices" in x for x in calls) == 2
    calls.clear()
    data.download_historical_data(tmp_path)
    assert not calls
    (tmp_path / data.OPTION_FILE).write_bytes(b"corrupted")
    with pytest.raises(data.HistoricalDataError, match="mismatch"):
        data.download_historical_data(tmp_path)


def test_failed_download_not_promoted(tmp_path, monkeypatch):
    monkeypatch.setattr(data, "_request_bytes", lambda _: b"wrong")
    with pytest.raises(data.HistoricalDataError, match="mismatch"):
        data._download("https://example.test", tmp_path / "file", "0" * 64)
    assert not (tmp_path / "file").exists()
    assert (tmp_path / "file.part").exists()


@pytest.mark.parametrize("mode", ["error", "empty", "checksum"])
def test_source_failures_are_not_synthetic_fallbacks(tmp_path, monkeypatch, mode):
    payload = b"pinned"
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(data, "OPTION_SHA256", digest)

    def request(url):
        if "CHECKSUM" in url:
            return b"invalid" if mode == "checksum" else digest.encode()
        if "get_delivery_prices" in url:
            response = (
                {"error": "rejected"}
                if mode == "error"
                else {"result": {"data": [], "records_total": 20}}
            )
            return json.dumps(response).encode()
        return payload

    monkeypatch.setattr(data, "_request_bytes", request)
    with pytest.raises(data.HistoricalDataError):
        data.download_historical_data(tmp_path)


def test_network_retry_and_failure(monkeypatch):
    attempts = []

    @contextmanager
    def open_request(*args, **kwargs):
        attempts.append(1)
        if len(attempts) < 3:
            raise URLError("temporary")
        yield io.BytesIO(b"ok")

    monkeypatch.setattr(data, "urlopen", open_request)
    monkeypatch.setattr(data.time, "sleep", lambda _: None)
    assert data._request_bytes("https://example.test") == b"ok"

    def fail(*args, **kwargs):
        raise URLError("always")

    monkeypatch.setattr(data, "urlopen", fail)
    with pytest.raises(data.HistoricalDataError, match="failed"):
        data._request_bytes("https://example.test")
