from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from statistics import fmean

import numpy as np

from crypto_options.hedging.paths import StochasticVolatilityPathConfig, generate_market_path
from crypto_options.hedging.simulator import DeltaHedgingSimulator, HedgePolicy
from crypto_options.instruments import EuropeanOption, OptionType
from crypto_options.marketdata.chain import clean_option_chain
from crypto_options.research.synthetic import generate_synthetic_option_chain
from crypto_options.surface.svi import SVISurface, calibrate_svi_surface


def _write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def _surface_experiment(output_dir: Path, seed: int) -> tuple[SVISurface, dict[str, object]]:
    quotes = generate_synthetic_option_chain(seed=seed)
    cleaning = clean_option_chain(quotes)
    surface = calibrate_svi_surface(cleaning.points, strict=True)
    diagnostics = surface.diagnostics()

    point_rows: list[dict[str, object]] = []
    squared_iv_errors: list[float] = []
    for point in cleaning.points:
        fitted_iv = surface.implied_volatility(point.time_to_expiry, point.strike, point.forward)
        squared_iv_errors.append((fitted_iv - point.implied_volatility) ** 2)
        point_rows.append(
            {
                "instrument_name": point.instrument_name,
                "maturity_years": point.time_to_expiry,
                "strike": point.strike,
                "forward": point.forward,
                "option_type": point.option_type.value,
                "log_moneyness": point.log_moneyness,
                "market_iv": point.implied_volatility,
                "fitted_iv": fitted_iv,
                "iv_residual": fitted_iv - point.implied_volatility,
                "weight": point.weight,
            }
        )
    _write_csv(
        output_dir / "surface_points.csv",
        point_rows,
        list(point_rows[0]),
    )

    parameter_rows = []
    for item in surface.slices:
        parameter_rows.append(
            {
                "maturity_years": item.time_to_expiry,
                "forward": item.forward,
                **asdict(item.parameters),
                "weighted_total_variance_rmse": item.weighted_rmse,
                "max_abs_total_variance_error": item.max_abs_total_variance_error,
                "observations": item.observations,
            }
        )
    _write_csv(output_dir / "svi_parameters.csv", parameter_rows, list(parameter_rows[0]))

    metrics: dict[str, object] = {
        "input_quotes": cleaning.total_quotes,
        "accepted_quotes": cleaning.accepted_quotes,
        "rejection_counts": cleaning.rejection_counts,
        "iv_rmse_vol_points": float(np.sqrt(np.mean(squared_iv_errors))),
        "iv_rmse_basis_points": float(np.sqrt(np.mean(squared_iv_errors)) * 10_000.0),
        "diagnostics": asdict(diagnostics),
    }
    with (output_dir / "surface_metrics.json").open("w", encoding="utf-8") as stream:
        json.dump(metrics, stream, indent=2, sort_keys=True)
    return surface, metrics


def _expected_shortfall(values: np.ndarray, probability: float = 0.05) -> float:
    threshold = float(np.quantile(values, probability))
    tail = values[values <= threshold]
    return float(np.mean(tail))


def _hedging_experiment(
    output_dir: Path,
    *,
    seed: int,
    paths: int,
    steps: int,
) -> tuple[list[dict[str, object]], dict[str, list[float]]]:
    if paths < 20:
        raise ValueError("paths must be at least 20 for a meaningful reference distribution")
    start = datetime(2026, 1, 2, 8, tzinfo=UTC)
    expiry = start + timedelta(days=30)
    option = EuropeanOption(60_000.0, expiry, OptionType.CALL)
    path_config = StochasticVolatilityPathConfig(
        initial_spot=60_000.0,
        initial_volatility=0.60,
        long_run_volatility=0.65,
        mean_reversion=4.0,
        volatility_of_log_volatility=0.75,
        spot_volatility_correlation=-0.50,
        rate=0.03,
    )
    policies = {
        "unhedged": HedgePolicy(
            rebalance_interval=None,
            delta_band=None,
            initial_hedge=False,
            half_spread_bps=1.0,
            fee_bps=2.0,
        ),
        "four_hour": HedgePolicy(
            rebalance_interval=timedelta(hours=4),
            delta_band=None,
            half_spread_bps=1.0,
            fee_bps=2.0,
            hedge_lot_size=0.001,
        ),
        "five_delta_band": HedgePolicy(
            rebalance_interval=None,
            delta_band=0.05,
            half_spread_bps=1.0,
            fee_bps=2.0,
            hedge_lot_size=0.001,
        ),
    }
    samples: dict[str, dict[str, list[float]]] = {
        name: {"pnl": [], "cost": [], "trades": [], "turnover": [], "recon": []}
        for name in policies
    }
    sample_ledger: list[dict[str, object]] = []

    for path_index in range(paths):
        events = generate_market_path(
            start,
            expiry,
            steps,
            path_config,
            seed=seed * 100_000 + path_index,
        )
        for policy_name, policy in policies.items():
            result = DeltaHedgingSimulator(policy).run(option, -1.0, events)
            samples[policy_name]["pnl"].append(result.summary.terminal_pnl)
            samples[policy_name]["cost"].append(result.summary.transaction_costs)
            samples[policy_name]["trades"].append(float(result.summary.trade_count))
            samples[policy_name]["turnover"].append(result.summary.turnover_notional)
            samples[policy_name]["recon"].append(result.summary.reconciliation_error)
            if path_index == 0 and policy_name == "five_delta_band":
                for row in result.ledger:
                    sample_ledger.append(
                        {
                            "timestamp": row.timestamp.isoformat(),
                            "spot": row.spot,
                            "volatility": row.volatility,
                            "option_mark": row.option_mark,
                            "hedge_units": row.hedge_units,
                            "net_delta": row.net_delta,
                            "cash": row.cash,
                            "equity": row.equity,
                            "trade_units": row.trade_units,
                            "transaction_cost": row.transaction_cost,
                            "reason": row.reason,
                        }
                    )

    _write_csv(output_dir / "sample_hedge_ledger.csv", sample_ledger, list(sample_ledger[0]))
    summary_rows: list[dict[str, object]] = []
    pnl_by_policy: dict[str, list[float]] = {}
    unhedged_std = float(np.std(samples["unhedged"]["pnl"], ddof=1))
    for policy_name, policy_samples in samples.items():
        pnl = np.asarray(policy_samples["pnl"])
        pnl_by_policy[policy_name] = list(pnl)
        pnl_std = float(np.std(pnl, ddof=1))
        summary_rows.append(
            {
                "policy": policy_name,
                "paths": paths,
                "mean_pnl": float(np.mean(pnl)),
                "pnl_std": pnl_std,
                "pnl_p05": float(np.quantile(pnl, 0.05)),
                "expected_shortfall_05": _expected_shortfall(pnl),
                "average_transaction_cost": fmean(policy_samples["cost"]),
                "average_trade_count": fmean(policy_samples["trades"]),
                "average_turnover_notional": fmean(policy_samples["turnover"]),
                "std_reduction_vs_unhedged": 1.0 - pnl_std / unhedged_std,
                "max_abs_reconciliation_error": max(abs(v) for v in policy_samples["recon"]),
            }
        )
    _write_csv(output_dir / "hedging_summary.csv", summary_rows, list(summary_rows[0]))
    return summary_rows, pnl_by_policy


def _make_figures(
    output_dir: Path,
    surface: SVISurface,
    hedging_summary: list[dict[str, object]],
    pnl_by_policy: dict[str, list[float]],
) -> None:
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError("install the research extra to generate figures") from exc

    figure, axis = plt.subplots(figsize=(8.4, 5.0))
    k_grid = np.linspace(-0.7, 0.7, 281)
    colors: dict[float, str] = {}
    for item in surface.slices:
        fitted_iv = np.sqrt(np.asarray(item.total_variance(k_grid)) / item.time_to_expiry)
        (line,) = axis.plot(k_grid, fitted_iv, label=f"{item.time_to_expiry * 365:.0f} days")
        colors[round(item.time_to_expiry, 12)] = line.get_color()
    with (output_dir / "surface_points.csv").open(encoding="utf-8") as stream:
        plotted: set[tuple[float, float]] = set()
        for row in csv.DictReader(stream):
            maturity = round(float(row["maturity_years"]), 12)
            log_moneyness = float(row["log_moneyness"])
            key = (maturity, round(log_moneyness, 12))
            if key in plotted:
                continue
            plotted.add(key)
            axis.scatter(
                log_moneyness,
                float(row["market_iv"]),
                s=13,
                facecolors="none",
                edgecolors=colors[maturity],
                linewidths=0.8,
                alpha=0.8,
            )
    axis.set(
        title="Arbitrage-diagnosed raw-SVI smiles",
        xlabel="log(K / F)",
        ylabel="implied volatility",
    )
    axis.grid(alpha=0.25)
    axis.legend(frameon=False, ncol=2)
    figure.tight_layout()
    figure.savefig(output_dir / "svi_smiles.png", dpi=180)
    plt.close(figure)

    labels = [str(row["policy"]) for row in hedging_summary]
    costs = [float(row["average_transaction_cost"]) for row in hedging_summary]
    stds = [float(row["pnl_std"]) for row in hedging_summary]
    figure, axis = plt.subplots(figsize=(7.2, 5.0))
    axis.scatter(costs, stds, s=75)
    for label, x_value, y_value in zip(labels, costs, stds, strict=True):
        axis.annotate(
            label.replace("_", " "),
            (x_value, y_value),
            xytext=(6, 5),
            textcoords="offset points",
        )
    axis.set(
        title="Hedge-risk versus transaction-cost frontier",
        xlabel="average transaction cost (USD)",
        ylabel="terminal PnL standard deviation (USD)",
    )
    axis.set_yscale("log")
    axis.grid(alpha=0.25)
    figure.tight_layout()
    figure.savefig(output_dir / "hedge_cost_risk.png", dpi=180)
    plt.close(figure)

    figure, axes = plt.subplots(1, 2, figsize=(10.5, 4.8))
    axes[0].hist(
        pnl_by_policy["unhedged"],
        bins=30,
        alpha=0.62,
        density=True,
        color="tab:blue",
    )
    axes[0].set(
        title="Unhedged short call",
        xlabel="terminal PnL (USD)",
        ylabel="density",
    )
    for label in ("four_hour", "five_delta_band"):
        axes[1].hist(
            pnl_by_policy[label],
            bins=30,
            alpha=0.45,
            density=True,
            label=label.replace("_", " "),
        )
    axes[1].set(title="Delta-hedged policies", xlabel="terminal PnL (USD)")
    axes[1].legend(frameon=False)
    for axis in axes:
        axis.grid(alpha=0.25)
    figure.suptitle("Short-call terminal hedging PnL")
    figure.tight_layout()
    figure.savefig(output_dir / "hedging_pnl_distribution.png", dpi=180)
    plt.close(figure)


def run_reference_research(
    output_dir: Path,
    *,
    seed: int = 17,
    paths: int = 250,
    steps: int = 180,
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    surface, surface_metrics = _surface_experiment(output_dir, seed)
    hedging_summary, pnl_by_policy = _hedging_experiment(
        output_dir, seed=seed, paths=paths, steps=steps
    )
    _make_figures(output_dir, surface, hedging_summary, pnl_by_policy)
    manifest: dict[str, object] = {
        "seed": seed,
        "paths": paths,
        "steps_per_path": steps,
        "surface": surface_metrics,
        "hedging": hedging_summary,
        "scope": "synthetic research benchmark; no claim of live tradability",
    }
    with (output_dir / "run_manifest.json").open("w", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2, sort_keys=True)
    return manifest
