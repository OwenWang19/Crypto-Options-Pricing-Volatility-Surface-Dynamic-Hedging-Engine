from __future__ import annotations

import csv
import json

from crypto_options.cli import main
from crypto_options.research.experiments import run_reference_research


def test_reference_research_writes_reproducible_artifacts(tmp_path) -> None:
    manifest = run_reference_research(tmp_path, seed=5, paths=20, steps=12)
    assert manifest["seed"] == 5
    assert manifest["paths"] == 20
    expected_files = {
        "hedge_cost_risk.png",
        "hedging_pnl_distribution.png",
        "hedging_summary.csv",
        "run_manifest.json",
        "sample_hedge_ledger.csv",
        "surface_metrics.json",
        "surface_points.csv",
        "svi_parameters.csv",
        "svi_smiles.png",
    }
    assert expected_files == {path.name for path in tmp_path.iterdir()}

    with (tmp_path / "run_manifest.json").open(encoding="utf-8") as stream:
        written_manifest = json.load(stream)
    assert written_manifest == manifest
    with (tmp_path / "hedging_summary.csv").open(encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    assert {row["policy"] for row in rows} == {
        "unhedged",
        "four_hour",
        "five_delta_band",
    }
    assert max(float(row["max_abs_reconciliation_error"]) for row in rows) < 1e-8


def test_price_and_implied_vol_cli_round_trip(capsys) -> None:
    assert (
        main(
            [
                "price",
                "--forward",
                "100",
                "--strike",
                "100",
                "--maturity",
                "0.5",
                "--volatility",
                "0.6",
                "--option-type",
                "call",
            ]
        )
        == 0
    )
    premium = json.loads(capsys.readouterr().out)["price"]
    assert (
        main(
            [
                "implied-vol",
                "--premium",
                str(premium),
                "--forward",
                "100",
                "--strike",
                "100",
                "--maturity",
                "0.5",
                "--option-type",
                "call",
            ]
        )
        == 0
    )
    recovered = float(capsys.readouterr().out)
    assert abs(recovered - 0.6) < 1e-10
