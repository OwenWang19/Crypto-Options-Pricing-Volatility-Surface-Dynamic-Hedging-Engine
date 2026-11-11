from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from crypto_options.instruments import OptionType
from crypto_options.marketdata.chain import clean_option_chain
from crypto_options.marketdata.deribit import DeribitClient
from crypto_options.pricing.black76 import black76_greeks, implied_volatility
from crypto_options.research.experiments import run_reference_research


def _price_command(args: argparse.Namespace) -> int:
    option_type = OptionType.parse(args.option_type)
    discount = pow(2.718281828459045, -args.rate * args.maturity)
    greeks = black76_greeks(
        args.forward,
        args.strike,
        args.maturity,
        args.volatility,
        discount,
        option_type,
    )
    print(json.dumps(asdict(greeks), indent=2, sort_keys=True))
    return 0


def _implied_vol_command(args: argparse.Namespace) -> int:
    option_type = OptionType.parse(args.option_type)
    discount = pow(2.718281828459045, -args.rate * args.maturity)
    volatility = implied_volatility(
        args.premium,
        args.forward,
        args.strike,
        args.maturity,
        discount,
        option_type,
    )
    print(f"{volatility:.12g}")
    return 0


def _demo_command(args: argparse.Namespace) -> int:
    manifest = run_reference_research(
        Path(args.out_dir), seed=args.seed, paths=args.paths, steps=args.steps
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


def _fetch_command(args: argparse.Namespace) -> int:
    client = DeribitClient(timeout_seconds=args.timeout)
    snapshot = client.option_snapshot(args.currency, now=datetime.now(UTC))
    cleaning = clean_option_chain(snapshot.quotes)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "instrument_name",
        "time_to_expiry",
        "strike",
        "option_type",
        "bid_usd_equivalent",
        "ask_usd_equivalent",
        "forward",
        "discount_factor",
        "open_interest",
    ]
    with output.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for quote in snapshot.quotes:
            writer.writerow(
                {
                    "instrument_name": quote.instrument_name,
                    "time_to_expiry": quote.time_to_expiry,
                    "strike": quote.strike,
                    "option_type": quote.option_type.value,
                    "bid_usd_equivalent": quote.bid,
                    "ask_usd_equivalent": quote.ask,
                    "forward": quote.forward,
                    "discount_factor": quote.discount_factor,
                    "open_interest": quote.open_interest,
                }
            )
    summary = {
        "captured_at": snapshot.captured_at.isoformat(),
        "currency": snapshot.currency,
        "index_price": snapshot.index_price,
        "raw_two_sided_quotes": len(snapshot.quotes),
        "skipped_instruments": snapshot.skipped_instruments,
        "clean_calibration_points": cleaning.accepted_quotes,
        "rejection_counts": cleaning.rejection_counts,
        "output": str(output),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _download_historical_command(args: argparse.Namespace) -> int:
    from crypto_options.marketdata.historical import download_historical_data

    manifest = download_historical_data(Path(args.raw_dir))
    print(json.dumps(manifest, indent=2))
    return 0


def _historical_command(args: argparse.Namespace) -> int:
    from crypto_options.research.historical import run_historical_research

    manifest = run_historical_research(
        Path(args.raw_dir), Path(args.out_dir), with_surface=not args.skip_surface
    )
    print(
        json.dumps(
            {
                key: manifest[key]
                for key in ("scope", "cases", "selected_band", "surface_validation")
            },
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crypto-options",
        description="Crypto option pricing, SVI calibration, and dynamic-hedging research engine",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    price = subparsers.add_parser("price", help="price a European option with Black-76")
    price.add_argument("--forward", type=float, required=True)
    price.add_argument("--strike", type=float, required=True)
    price.add_argument("--maturity", type=float, required=True, help="ACT/365 years")
    price.add_argument("--volatility", type=float, required=True)
    price.add_argument("--rate", type=float, default=0.0)
    price.add_argument("--option-type", choices=["call", "put", "c", "p"], required=True)
    price.set_defaults(handler=_price_command)

    iv = subparsers.add_parser("implied-vol", help="invert a Black-76 premium")
    iv.add_argument("--premium", type=float, required=True)
    iv.add_argument("--forward", type=float, required=True)
    iv.add_argument("--strike", type=float, required=True)
    iv.add_argument("--maturity", type=float, required=True, help="ACT/365 years")
    iv.add_argument("--rate", type=float, default=0.0)
    iv.add_argument("--option-type", choices=["call", "put", "c", "p"], required=True)
    iv.set_defaults(handler=_implied_vol_command)

    demo = subparsers.add_parser("demo", help="run the deterministic reference research")
    demo.add_argument("--out-dir", default="artifacts/reference")
    demo.add_argument("--seed", type=int, default=17)
    demo.add_argument("--paths", type=int, default=250)
    demo.add_argument("--steps", type=int, default=180)
    demo.set_defaults(handler=_demo_command)

    fetch = subparsers.add_parser(
        "fetch-deribit", help="download a public two-sided option snapshot"
    )
    fetch.add_argument("--currency", choices=["BTC", "ETH"], default="BTC")
    fetch.add_argument("--timeout", type=float, default=10.0)
    fetch.add_argument("--output", default="artifacts/deribit_snapshot.csv")
    fetch.set_defaults(handler=_fetch_command)

    download = subparsers.add_parser("download-historical", help="download pinned real datasets")
    download.add_argument("--raw-dir", default="data/raw")
    download.set_defaults(handler=_download_historical_command)

    historical = subparsers.add_parser("backtest-historical", help="replay real historical quotes")
    historical.add_argument("--raw-dir", default="data/raw")
    historical.add_argument("--out-dir", default="artifacts/historical")
    historical.add_argument(
        "--skip-surface",
        action="store_true",
        help="run only historical hedging; record SVI as not run",
    )
    historical.set_defaults(handler=_historical_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
