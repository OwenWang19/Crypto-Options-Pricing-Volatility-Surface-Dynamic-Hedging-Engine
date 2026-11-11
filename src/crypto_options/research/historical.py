"""Reproducible real-data experiments with chronological selection and honest uncertainty."""

from __future__ import annotations

import json
import platform
import subprocess
from dataclasses import asdict, replace
from importlib.metadata import version
from pathlib import Path

import numpy as np
import pandas as pd

from crypto_options.hedging.historical import (
    POLICIES,
    HistoricalCosts,
    historical_opportunities,
    replay_case,
)
from crypto_options.instruments import OptionType
from crypto_options.marketdata.chain import OptionQuote, clean_option_chain
from crypto_options.marketdata.historical import (
    HistoricalDataError,
    load_option_panel,
    load_spot_opens,
    sha256_file,
)
from crypto_options.surface.svi import SurfaceCalibrationError, calibrate_svi_surface


def summarize_cases(results: pd.DataFrame) -> pd.DataFrame:
    """No annualized Sharpe or capital return is invented for an uncollateralized experiment."""
    records = []
    for (scenario, split, policy), group in results.groupby(["scenario", "split", "policy"]):
        values = group.terminal_pnl_usd.to_numpy()
        normalized = group.pnl_per_entry_notional_bps.to_numpy()
        threshold = np.quantile(values, 0.05)
        records.append(
            {
                "scenario": scenario,
                "split": split,
                "policy": policy,
                "cases": len(group),
                "independent_week_blocks": group.week.nunique(),
                "mean_pnl_usd": float(values.mean()),
                "pnl_std_usd": float(values.std(ddof=1)) if len(values) > 1 else float("nan"),
                "normalized_pnl_rmse_bps": float(np.sqrt(np.mean(normalized**2))),
                "pnl_std_bps": float(normalized.std(ddof=1)) if len(values) > 1 else float("nan"),
                "worst_case_pnl_usd": float(values.min()),
                "empirical_es05_usd": float(values[values <= threshold].mean()),
                "mean_hedge_cost_usd": float(group.hedge_cost_usd.mean()),
                "mean_hedge_trades": float(group.hedge_trade_count.mean()),
                "max_observed_case_drawdown_usd": float(group.max_drawdown_usd.max()),
                "stale_delta_events": int(group.stale_delta_events.sum()),
                "fallback_mark_events": int(group.fallback_mark_events.sum()),
                "max_reconciliation_error": float(group.max_abs_reconciliation_error.max()),
            }
        )
    return pd.DataFrame(records)


def choose_band(results: pd.DataFrame) -> str:
    """Minimize normalized terminal hedging RMSE on CALIBRATION observations only."""
    calibration = results[
        (results.scenario == "baseline")
        & (results.split == "calibration")
        & results.policy.str.startswith("band_")
    ]
    if calibration.empty or calibration.week.nunique() < 8:
        raise HistoricalDataError("at least eight calibration weeks are required")
    scores = calibration.groupby("policy").pnl_per_entry_notional_bps.agg(
        lambda x: float(np.sqrt(np.mean(x.to_numpy() ** 2)))
    )
    return str(scores.sort_index().idxmin())


def paired_week_bootstrap(
    results: pd.DataFrame, policy: str, draws: int = 5000, seed: int = 20261111
) -> dict:
    """Pair all policies and both option sides inside each resampled entry-week block."""
    holdout = results[(results.scenario == "baseline") & (results.split == "holdout")]
    table = holdout.pivot(
        index=["week", "case_id"], columns="policy", values="pnl_per_entry_notional_bps"
    )
    table = table[["unhedged", "hourly", policy]].dropna()
    weeks = sorted(table.index.get_level_values("week").unique())
    if len(weeks) < 5:
        raise HistoricalDataError("fewer than five paired holdout weeks")
    blocks = [table.xs(week, level="week").to_numpy() for week in weeks]
    rng = np.random.default_rng(seed)
    samples = []
    for _ in range(draws):
        sample = np.concatenate([blocks[i] for i in rng.integers(0, len(blocks), len(blocks))])
        base, hourly, candidate = sample.T
        samples.append(
            (
                float(candidate.std(ddof=1) - hourly.std(ddof=1)),
                float(1 - candidate.std(ddof=1) / base.std(ddof=1))
                if base.std(ddof=1) > 0
                else 0.0,
            )
        )
    intervals = np.quantile(np.asarray(samples), [0.025, 0.975], axis=0)
    return {
        "policy": policy,
        "week_blocks": len(weeks),
        "paired_cases": len(table),
        "draws": draws,
        "seed": seed,
        "std_difference_vs_hourly_bps_95pct": intervals[:, 0].tolist(),
        "std_reduction_vs_unhedged_95pct": intervals[:, 1].tolist(),
        "caution": "Small sample; weekly blocks do not remove all serial dependence.",
    }


def split_surface_strikes(points: tuple) -> tuple[list, list]:
    """Hold out internal strikes, keeping call/put duplicates together; no wing extrapolation."""
    train, held_out = [], []
    maturities = sorted({p.time_to_expiry for p in points})
    for maturity in maturities:
        slice_points = [p for p in points if p.time_to_expiry == maturity]
        strikes = sorted({p.strike for p in slice_points})
        held = {
            strike for i, strike in enumerate(strikes) if i % 3 == 1 and 0 < i < len(strikes) - 1
        }
        if len(strikes) - len(held) < 5:
            continue
        train.extend(p for p in slice_points if p.strike not in held)
        held_out.extend(p for p in slice_points if p.strike in held)
    return train, held_out


def real_surface_validation(
    panel: pd.DataFrame, output_dir: Path, *, max_snapshots: int | None = None
) -> dict:
    """Weekly contemporaneous multi-expiry SVI fits and strike-held-out validation.

    No failed fit is turned into a successful point. Real-data failures are output.
    The primary hedge does NOT pretend to use a calibrated SVI surface.
    """
    records, residuals, rejection_rows = [], [], []
    days = pd.date_range("2024-01-19", "2024-07-19", freq="W-FRI", tz="UTC")
    if max_snapshots is not None:
        days = days[:max_snapshots]
    for day in days:
        decision = day + pd.Timedelta(hours=9)
        snapshot = panel[
            (panel.snapshot_time <= decision)
            & (panel.snapshot_time > decision - pd.Timedelta(minutes=70))
        ]
        snapshot = snapshot.sort_values("snapshot_time").drop_duplicates(
            "instrument_name", keep="last"
        )
        if snapshot.empty:
            records.append({"decision": decision.isoformat(), "status": "no_snapshot"})
            continue
        valuation_time = snapshot.snapshot_time.max()
        maturities = sorted(
            snapshot.loc[
                snapshot.two_sided
                & (snapshot.open_interest > 0)
                & ((snapshot.expiry_date - valuation_time).dt.total_seconds() >= 86400),
                "expiry_date",
            ].unique()
        )
        selected = []
        for target in (7, 30, 90, 180):
            if maturities:
                expiry = min(
                    maturities,
                    key=lambda x: abs((x - valuation_time).total_seconds() / 86400 - target),
                )
                if expiry not in selected:
                    selected.append(expiry)
        quotes = []
        for expiry in sorted(selected):
            rows = snapshot[(snapshot.expiry_date == expiry) & (snapshot.open_interest > 0)]
            forward = float(rows.underlying_price.median())
            # ITM inverse quotes amplify tick noise when solving for small time value.
            # This side rule is fixed by moneyness, never chosen by fit error or PnL.
            rows = rows[
                ((rows.option_type.str.upper().isin(["C", "CALL"])) & (rows.strike >= forward))
                | ((rows.option_type.str.upper().isin(["P", "PUT"])) & (rows.strike <= forward))
            ]
            tau = (expiry - valuation_time).total_seconds() / 31536000
            for row in rows.to_dict("records"):
                # Normalized inverse-option premium; no invented spot or zero forward basis.
                quotes.append(
                    OptionQuote(
                        row["instrument_name"],
                        tau,
                        row["strike"],
                        OptionType.parse(row["option_type"]),
                        row["bid_price"] * forward,
                        row["ask_price"] * forward,
                        forward,
                        1.0,
                        row["open_interest"],
                    )
                )
        cleaning = clean_option_chain(
            quotes, max_relative_spread=0.35, min_vega_ratio=1e-3, max_abs_log_moneyness=0.5
        )
        rejection_rows.extend(
            {"decision": decision.isoformat(), "reason": k, "count": v}
            for k, v in cleaning.rejection_counts.items()
        )
        # Drop sparse expiries based on quote availability before fitting, not on fit quality.
        eligible = {
            p.time_to_expiry
            for p in cleaning.points
            if len({x.strike for x in cleaning.points if x.time_to_expiry == p.time_to_expiry}) >= 8
        }
        points = tuple(p for p in cleaning.points if p.time_to_expiry in eligible)
        train, held_out = split_surface_strikes(points)
        record = {
            "decision": decision.isoformat(),
            "valuation_time": valuation_time.isoformat(),
            "input_quotes": len(quotes),
            "clean_quotes": cleaning.accepted_quotes,
            "fit_points": len(points),
            "train_points": len(train),
            "held_out_points": len(held_out),
            "maturities": len(eligible),
        }
        try:
            if not train or not held_out:
                raise SurfaceCalibrationError("insufficient paired-strike validation points")
            surface = calibrate_svi_surface(train, strict=False)
            diagnostics = surface.diagnostics()
            record.update(
                {
                    "status": "safe_on_grid" if diagnostics.is_safe(1e-7) else "unsafe_on_grid",
                    **asdict(diagnostics),
                }
            )
            errors = {"train": [], "held_out": []}
            for split, group in (("train", train), ("held_out", held_out)):
                for point in group:
                    fitted = surface.implied_volatility(
                        point.time_to_expiry, point.strike, point.forward
                    )
                    error = (fitted - point.implied_volatility) * 10000
                    errors[split].append(error)
                    residuals.append(
                        {
                            "decision": decision.isoformat(),
                            "split": split,
                            "instrument": point.instrument_name,
                            "maturity_years": point.time_to_expiry,
                            "market_iv": point.implied_volatility,
                            "fitted_iv": fitted,
                            "error_vol_bps": error,
                            "grid_safe": diagnostics.is_safe(1e-7),
                        }
                    )
            for split, values in errors.items():
                record[f"{split}_iv_rmse_vol_bps"] = float(np.sqrt(np.mean(np.square(values))))
        except (SurfaceCalibrationError, ValueError, FloatingPointError) as exc:
            record.update({"status": "fit_failed", "error": str(exc)})
        records.append(record)
        print(f"SVI {decision.date()}: {record['status']}", flush=True)
    frame = pd.DataFrame(records)
    frame.to_csv(output_dir / "real_svi_validation.csv", index=False)
    pd.DataFrame(residuals).to_csv(output_dir / "real_svi_residuals.csv", index=False)
    pd.DataFrame(rejection_rows).to_csv(output_dir / "real_svi_rejections.csv", index=False)
    return {
        "snapshots_attempted": len(frame),
        "status_counts": frame.status.value_counts().to_dict(),
        "held_out_design": "internal strikes, calls and puts grouped; not a temporal forecast",
        "primary_hedger_uses_svi": False,
        "side_selection": "OTM calls and puts by forward moneyness; min normalized vega 0.001",
    }


def _code_hashes() -> dict:
    source = Path(__file__).resolve().parents[1]
    return {
        str(path.relative_to(source.parent)): sha256_file(path)
        for path in sorted(source.rglob("*.py"))
    }


def _plots(results: pd.DataFrame, output_dir: Path, selected: str) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    policies = ["unhedged", "hourly", "four_hour", selected]
    holdout = results[(results.scenario == "baseline") & (results.split == "holdout")]
    figure, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    axes[0].boxplot(
        [holdout[holdout.policy == p].pnl_per_entry_notional_bps for p in policies],
        tick_labels=policies,
        showfliers=True,
    )
    axes[0].set(
        ylabel="P&L / entry BTC notional (bp; NOT capital return)",
        title="Historical holdout: individual option cases",
    )
    for policy in policies:
        group = holdout[holdout.policy == policy]
        axes[1].scatter(group.hedge_cost_usd.mean(), group.terminal_pnl_usd.std(), label=policy)
    axes[1].set(
        xlabel="Average hedge cost (USD)",
        ylabel="Terminal P&L standard deviation (USD)",
        title="Real data: cost versus dispersion",
    )
    axes[1].legend()
    for axis in axes:
        axis.grid(alpha=0.2)
    figure.tight_layout()
    figure.savefig(output_dir / "historical_holdout.png", dpi=160)
    plt.close(figure)


def run_historical_research(raw_dir: Path, output_dir: Path, *, with_surface: bool = True) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    panel, audit = load_option_panel(raw_dir)
    spot = load_spot_opens(raw_dir)
    provenance = json.loads((raw_dir / "provenance.json").read_text())
    for source in provenance["files"]:
        if sha256_file(raw_dir / source["file"]) != source["sha256"]:
            raise HistoricalDataError(f"provenance mismatch: {source['file']}")
    deliveries = json.loads((raw_dir / "delivery_prices.json").read_text())
    cases, opportunities = historical_opportunities(panel)
    pd.DataFrame(opportunities).to_csv(output_dir / "opportunities.csv", index=False)
    (output_dir / "data_audit.json").write_text(json.dumps(audit, indent=2) + "\n")
    costs = HistoricalCosts()
    scenarios = [
        costs,
        replace(costs, name="higher_spot_cost", spot_fee_bps=10, spot_slippage_bps=2),
        replace(
            costs,
            name="stress_cost",
            spot_fee_bps=20,
            spot_slippage_bps=5,
            option_slippage_fraction=0.05,
        ),
        replace(costs, name="lag_2m", execution_lag_minutes=2),
        replace(costs, name="lag_5m", execution_lag_minutes=5),
        replace(costs, name="borrow_5pct", annual_borrow_rate=0.05),
        replace(costs, name="borrow_10pct", annual_borrow_rate=0.10),
    ]
    summaries, ledger_rows = [], []
    for scenario in scenarios:
        for case in cases:
            date = case["expiry_time"].date().isoformat()
            if date not in deliveries:
                raise HistoricalDataError(f"official delivery missing: {date}")
            for policy in POLICIES:
                result, ledger = replay_case(case, spot, float(deliveries[date]), policy, scenario)
                summaries.append(result)
                if scenario.name == "baseline":
                    ledger_rows.extend(ledger)
        print(f"Historical replay: {scenario.name}, {len(cases)} cases", flush=True)
    results = pd.DataFrame(summaries)
    selected = choose_band(results)
    results.to_csv(output_dir / "case_results.csv", index=False)
    pd.DataFrame(ledger_rows).to_csv(
        output_dir / "event_ledger.csv.gz", index=False, compression={"method": "gzip", "mtime": 0}
    )
    summary = summarize_cases(results)
    summary.to_csv(output_dir / "policy_summary.csv", index=False)
    bootstrap = paired_week_bootstrap(results, selected)
    (output_dir / "bootstrap.json").write_text(json.dumps(bootstrap, indent=2) + "\n")
    surface = real_surface_validation(panel, output_dir) if with_surface else {"status": "not_run"}
    _plots(results, output_dir, selected)
    # Data and source hashes, not machine-specific absolute paths, establish reproducibility.
    git = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    manifest = {
        "scope": "Real historical L1 option quotes and real spot prices; simulated executions",
        "protocol": "docs/historical_protocol.md",
        "data": provenance,
        "data_audit": audit,
        "cases": len(cases),
        "opportunities": len(opportunities),
        "skipped_opportunities": len(opportunities) - len(cases),
        "distinct_entry_weeks": len({c["week"] for c in cases}),
        "selected_band": selected,
        "band_selection": "calibration-only normalized terminal-PnL RMSE; frozen for holdout",
        "scenarios": [asdict(s) for s in scenarios],
        "surface_validation": surface,
        "bootstrap": bootstrap,
        "source_sha256": _code_hashes(),
        "git_head": git.stdout.strip(),
        "git_worktree_expected_dirty": True,
        "python": platform.python_version(),
        "packages": {
            name: version(name) for name in ("numpy", "scipy", "pandas", "pyarrow", "matplotlib")
        },
        "claims_not_made": [
            "guaranteed fills",
            "historical L2 impact",
            "audited PnL",
            "capital returns",
            "margin feasibility",
            "live performance",
            "independently preregistered out-of-sample alpha",
        ],
    }
    (output_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest
