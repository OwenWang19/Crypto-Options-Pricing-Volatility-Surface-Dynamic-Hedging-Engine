"""Independent artifact checks: no market-data requests or trading actions.

Run from the repository root AFTER backtest-historical has finished. Checks include
source hashes, row-level cash-flow reconstruction, inverse units, pairing and time ordering.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from crypto_options.marketdata.historical import sha256_file


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def verify(root: Path, output: Path, raw: Path) -> dict:
    manifest = json.loads((output / "run_manifest.json").read_text())
    for name, expected in manifest["source_sha256"].items():
        require(sha256_file(root / "src" / name) == expected, f"changed source: {name}")
    for entry in manifest["data"]["files"]:
        require(
            sha256_file(raw / entry["file"]) == entry["sha256"], f"changed data: {entry['file']}"
        )
    cases = pd.read_csv(output / "case_results.csv")
    opportunities = pd.read_csv(output / "opportunities.csv")
    entered = opportunities[opportunities.status == "entered"]
    require(len(entered) == manifest["cases"], "opportunity count mismatch")
    require(
        (
            pd.to_datetime(entered.signal_snapshot, utc=True)
            <= pd.to_datetime(entered.selection_time, utc=True)
        ).all(),
        "selection look-ahead",
    )
    require(
        (
            pd.to_datetime(entered.entry_quote_time, utc=True)
            > pd.to_datetime(entered.selection_time, utc=True)
        ).all(),
        "entry look-ahead",
    )
    require(not cases.duplicated(["case_id", "policy", "scenario"]).any(), "duplicate case keys")
    expected_ids = set(entered.case_id)
    for _, group in cases.groupby(["scenario", "policy"]):
        require(set(group.case_id) == expected_ids, "a policy dropped an entered case")
    require(len(cases) == len(expected_ids) * 6 * len(manifest["scenarios"]), "incomplete grid")
    require((cases.fill_btc <= cases.entry_bid_btc).all(), "entry better than the recorded bid")
    intrinsic = np.maximum(
        np.where(cases.side == "C", 1, -1) * (cases.official_delivery_price - cases.strike), 0
    )
    expected_payoff_btc = intrinsic / cases.official_delivery_price
    require(np.allclose(cases.payoff_btc, expected_payoff_btc, atol=1e-12), "BTC payoff mismatch")
    require(
        np.allclose(cases.settlement_usd, expected_payoff_btc * cases.expiry_spot, atol=1e-8),
        "USD settlement conversion mismatch",
    )
    independent_pnl = (
        cases.premium_usd
        - cases.settlement_usd
        - cases.option_fee_usd
        - cases.delivery_fee_usd
        - cases.premium_conversion_cost
        - cases.settlement_conversion_cost
        + cases.hedge_pnl_usd
        + cases.financing_usd
        - cases.hedge_cost_usd
    )
    max_pnl_error = float(np.max(np.abs(independent_pnl - cases.terminal_pnl_usd)))
    require(max_pnl_error < 1e-7, "economic terminal PnL identity failed")
    ledger = pd.read_csv(output / "event_ledger.csv.gz")
    source_time = pd.to_datetime(ledger.source_time, utc=True)
    execution_time = pd.to_datetime(ledger.execution_time, utc=True)
    delta_time = pd.to_datetime(ledger.delta_source_time, utc=True)
    require((source_time < execution_time).all(), "execution did not lag signal")
    require((delta_time <= source_time).all(), "future delta signal")
    require((ledger.loc[ledger.stale_delta, "trade_btc"] == 0).all(), "traded a stale delta")
    cash_columns = [
        "option_premium_cashflow",
        "option_fee_cashflow",
        "conversion_cashflow",
        "spot_trade_cashflow",
        "settlement_cashflow",
        "delivery_fee_cashflow",
        "financing_cashflow",
    ]
    ledger["net_cashflow"] = ledger[cash_columns].sum(axis=1)
    reconstructed = ledger.groupby(["case_id", "policy"], sort=False).net_cashflow.cumsum()
    max_cash_error = float(np.max(np.abs(reconstructed - ledger.cash_usd)))
    require(max_cash_error < 1e-7, "event cash-flow reconstruction failed")
    baseline = cases[cases.scenario == "baseline"].set_index(["case_id", "policy"])
    for key, group in ledger.groupby(["case_id", "policy"], sort=False):
        times = pd.to_datetime(group.execution_time, utc=True)
        require(times.is_monotonic_increasing and not times.duplicated().any(), "bad event order")
        require(
            abs(group.iloc[-1].cash_usd - baseline.loc[key].terminal_pnl_usd) < 1e-7,
            "ledger terminal differs from case summary",
        )
        require(group.iloc[-1].hedge_btc == 0, "unclosed hedge")
    surface = pd.read_csv(output / "real_svi_validation.csv")
    counts = surface.status.value_counts().to_dict()
    require(counts == manifest["surface_validation"]["status_counts"], "surface counts differ")
    safe = surface[surface.status == "safe_on_grid"]
    require(
        (
            (safe.minimum_butterfly_density >= -1e-7)
            & (safe.minimum_calendar_spread >= -1e-7)
            & (safe.minimum_total_variance >= -1e-7)
        ).all(),
        "unsafe surface labelled safe",
    )
    result = {
        "status": "passed",
        "case_rows": len(cases),
        "entered_opportunities": len(entered),
        "ledger_rows": len(ledger),
        "ledger_groups": len(baseline),
        "max_independent_pnl_error_usd": max_pnl_error,
        "max_cash_reconstruction_error_usd": max_cash_error,
        "source_hashes_verified": len(manifest["source_sha256"]),
        "raw_files_verified": len(manifest["data"]["files"]),
        "surface_status_counts_verified": counts,
        "artifact_sha256": {
            p.name: sha256_file(p)
            for p in sorted(output.iterdir())
            if p.is_file()
            and p.suffix in {".csv", ".gz", ".json", ".png"}
            and p.name != "verification.json"
        },
        "interpretation": "Accounting and provenance passed; this does NOT prove feasible fills.",
    }
    (output / "verification.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path("artifacts/historical"))
    parser.add_argument("--raw", type=Path, default=Path("data/raw"))
    args = parser.parse_args()
    print(json.dumps(verify(args.root, args.output, args.raw), indent=2))


if __name__ == "__main__":
    main()
