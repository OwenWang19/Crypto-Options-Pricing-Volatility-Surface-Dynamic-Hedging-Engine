from __future__ import annotations

import json
from dataclasses import replace

import pandas as pd
import pytest

from crypto_options import cli
from crypto_options.hedging.historical import HistoricalCosts, replay_case
from crypto_options.marketdata.historical import HistoricalDataError, sha256_file
from crypto_options.research import historical as research
from crypto_options.research.synthetic import generate_synthetic_option_chain
from crypto_options.surface.svi import ArbitrageDiagnostics, SurfaceCalibrationError


def score_frame():
    rows = []
    for split in ("calibration", "holdout"):
        for week in range(10):
            for side in ("C", "P"):
                for policy, scale in (
                    ("unhedged", 4),
                    ("hourly", 1),
                    ("four_hour", 1.2),
                    ("band_002", 1.1),
                    ("band_005", 0.8),
                    ("band_010", 2),
                ):
                    value = (week - 4.5 + (0.2 if side == "P" else 0)) * scale
                    rows.append(
                        {
                            "scenario": "baseline",
                            "split": split,
                            "week": str(week),
                            "case_id": f"{week}_{side}",
                            "policy": policy,
                            "terminal_pnl_usd": value,
                            "pnl_per_entry_notional_bps": value,
                            "hedge_cost_usd": scale,
                            "hedge_trade_count": 10,
                            "max_drawdown_usd": abs(value),
                            "stale_delta_events": 1,
                            "fallback_mark_events": 0,
                            "max_abs_reconciliation_error": 1e-12,
                        }
                    )
    return pd.DataFrame(rows)


def test_chronological_selection_does_not_consult_holdout():
    frame = score_frame()
    assert research.choose_band(frame) == "band_005"
    frame.loc[frame.split == "holdout", "pnl_per_entry_notional_bps"] *= 1e6
    assert research.choose_band(frame) == "band_005"
    with pytest.raises(HistoricalDataError):
        research.choose_band(frame[frame.week == "0"])


def test_summary_and_paired_week_bootstrap():
    frame = score_frame()
    summary = research.summarize_cases(frame)
    assert len(summary) == 12
    assert (summary.cases == 20).all()
    assert (summary.independent_week_blocks == 10).all()
    first = research.paired_week_bootstrap(frame, "band_005", draws=100, seed=19)
    assert first == research.paired_week_bootstrap(frame, "band_005", draws=100, seed=19)
    assert first["week_blocks"] == 10
    assert first["paired_cases"] == 20
    assert first["std_difference_vs_hourly_bps_95pct"][1] < 0
    with pytest.raises(HistoricalDataError):
        research.paired_week_bootstrap(frame[frame.week == "0"], "band_005", draws=100)


def surface_panel():
    anchor = pd.Timestamp("2024-01-19 08:00:10", tz="UTC")
    rows = []
    for quote in generate_synthetic_option_chain(seed=17):
        rows.append(
            {
                "snapshot_time": anchor,
                "instrument_name": quote.instrument_name,
                "expiry_date": anchor + pd.Timedelta(days=round(quote.time_to_expiry * 365)),
                "two_sided": True,
                "open_interest": max(quote.open_interest, 1),
                "underlying_price": quote.forward,
                "strike": quote.strike,
                "option_type": quote.option_type.value,
                "bid_price": quote.bid / quote.forward / quote.discount_factor,
                "ask_price": quote.ask / quote.forward / quote.discount_factor,
            }
        )
    return pd.DataFrame(rows)


def test_strike_split_never_leaks_call_put_pairs():
    from crypto_options.marketdata.chain import clean_option_chain

    points = clean_option_chain(generate_synthetic_option_chain(seed=17)).points
    train, held = research.split_surface_strikes(points)

    def key(p):
        return p.time_to_expiry, p.strike

    assert {key(p) for p in train}.isdisjoint({key(p) for p in held})
    assert len(train) + len(held) == len(points)
    for maturity in {p.time_to_expiry for p in train}:
        train_strikes = [p.strike for p in train if p.time_to_expiry == maturity]
        held_strikes = [p.strike for p in held if p.time_to_expiry == maturity]
        assert min(train_strikes) < min(held_strikes) < max(held_strikes) < max(train_strikes)
    tiny = points[:4]
    assert research.split_surface_strikes(tiny) == ([], [])


def test_real_surface_path_works_on_an_explicit_synthetic_unit_fixture(tmp_path):
    # This is a UNIT TEST fixture; it is never included in historical research artifacts.
    result = research.real_surface_validation(surface_panel(), tmp_path, max_snapshots=2)
    assert result["snapshots_attempted"] == 2
    assert result["status_counts"]["safe_on_grid"] == 1
    assert result["status_counts"]["no_snapshot"] == 1
    errors = pd.read_csv(tmp_path / "real_svi_residuals.csv")
    assert {"train", "held_out"} == set(errors.split)
    assert (tmp_path / "real_svi_rejections.csv").exists()


@pytest.mark.parametrize("mode", ["failure", "unsafe", "sparse"])
def test_bad_real_surfaces_are_counted_not_hidden(tmp_path, monkeypatch, mode):
    def failed(*args, **kwargs):
        raise SurfaceCalibrationError("deliberate test failure")

    if mode == "failure":
        monkeypatch.setattr(research, "calibrate_svi_surface", failed)
    elif mode == "unsafe":

        class Unsafe:
            def diagnostics(self):
                return ArbitrageDiagnostics(0.01, -0.1, -0.1, 2, 3, 401)

            def implied_volatility(self, *args):
                return 0.6

        monkeypatch.setattr(research, "calibrate_svi_surface", lambda *a, **k: Unsafe())
    panel = surface_panel()
    if mode == "sparse":
        panel = panel.iloc[:2]
    result = research.real_surface_validation(panel, tmp_path, max_snapshots=1)
    assert result["status_counts"] == {"unsafe_on_grid" if mode == "unsafe" else "fit_failed": 1}


def test_plots_and_source_fingerprints(tmp_path):
    research._plots(score_frame(), tmp_path, "band_005")
    assert (tmp_path / "historical_holdout.png").stat().st_size > 1000
    hashes = research._code_hashes()
    assert "crypto_options/hedging/historical.py" in hashes
    assert all(len(x) == 64 for x in hashes.values())


def test_end_to_end_historical_artifacts_without_network(tmp_path, monkeypatch):
    from test_historical_hedging import make_case

    case, spot = make_case()
    raw = tmp_path / "raw"
    raw.mkdir()
    source = raw / "test_source"
    source.write_bytes(b"offline fixture, not real market data")
    provenance = {"files": [{"file": source.name, "sha256": sha256_file(source)}]}
    (raw / "provenance.json").write_text(json.dumps(provenance))
    date = case["expiry_time"].date().isoformat()
    (raw / "delivery_prices.json").write_text(json.dumps({date: 62000}))
    monkeypatch.setattr(research, "load_option_panel", lambda _: (pd.DataFrame(), {"raw_rows": 1}))
    monkeypatch.setattr(research, "load_spot_opens", lambda _: spot)
    monkeypatch.setattr(
        research, "historical_opportunities", lambda _: ([case], [{"status": "entered"}])
    )
    monkeypatch.setattr(research, "choose_band", lambda _: "band_005")
    monkeypatch.setattr(research, "paired_week_bootstrap", lambda *a, **k: {"test_fixture": True})
    monkeypatch.setattr(research, "_plots", lambda *a: None)
    monkeypatch.setattr(research, "real_surface_validation", lambda *a: {"test_fixture": True})
    out = tmp_path / "output"
    manifest = research.run_historical_research(raw, out)
    assert manifest["cases"] == 1
    assert manifest["surface_validation"]["test_fixture"]
    results = pd.read_csv(out / "case_results.csv")
    assert len(results) == 7 * 6
    assert results.max_abs_reconciliation_error.max() < 1e-8
    assert not pd.read_csv(out / "event_ledger.csv.gz").empty
    skipped = research.run_historical_research(raw, out, with_surface=False)
    assert skipped["surface_validation"] == {"status": "not_run"}
    source.write_bytes(b"changed")
    with pytest.raises(HistoricalDataError, match="provenance"):
        research.run_historical_research(raw, out)
    source.write_bytes(b"offline fixture, not real market data")
    (raw / "delivery_prices.json").write_text("{}")
    with pytest.raises(HistoricalDataError, match="delivery missing"):
        research.run_historical_research(raw, out)


def test_historical_cli_commands(tmp_path, monkeypatch, capsys):
    from crypto_options.marketdata import historical as data

    monkeypatch.setattr(data, "download_historical_data", lambda _: {"fixture": True})
    assert cli.main(["download-historical", "--raw-dir", str(tmp_path)]) == 0
    fake = {
        "scope": "fixture",
        "cases": 1,
        "selected_band": "band_005",
        "surface_validation": {"status": "not_run"},
    }
    monkeypatch.setattr(research, "run_historical_research", lambda *a, **k: fake)
    assert cli.main(["backtest-historical", "--skip-surface"]) == 0
    assert "fixture" in capsys.readouterr().out


def test_cash_only_zero_friction_identity():
    from test_historical_hedging import make_case

    from crypto_options.hedging.historical import POLICIES

    case, spot = make_case()
    costs = replace(
        HistoricalCosts(),
        spot_fee_bps=0,
        spot_slippage_bps=0,
        option_slippage_fraction=0,
        option_fee_btc=0,
        delivery_fee_btc=0,
    )
    result, _ = replay_case(case, spot, 62000.0, POLICIES[0], costs)
    assert result["terminal_pnl_usd"] == pytest.approx(
        result["premium_usd"] - result["settlement_usd"]
    )
