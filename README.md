# Crypto Options Pricing, Volatility Surface & Dynamic Hedging Engine

An auditable research engine for crypto European options. It cleans two-sided option chains,
inverts Black-76 implied volatility with explicit price bounds, calibrates raw-SVI in total-
variance space, diagnoses static arbitrage, and simulates self-financing delta hedging with a
fully reconcilable event ledger.

The implementation separates market observations from execution assumptions and retains quote
rejections, model diagnostics, transaction costs and reconciled cash flows for review.

> **Scope:** research and model-validation software, not trading advice or a production execution
> system. There are now **two separate experiments**: a synthetic numerical benchmark in
> `artifacts/reference/`, and an actual historical quote/spot replay in `artifacts/historical/`.
> Historical market observations are real; fills, borrowing and market impact remain assumptions.

## Real historical backtest

The historical experiment uses **4,498,832 Deribit BTC option snapshots**, recorded by
Cryptarbitrage and [linked by Deribit Insights](https://insights.deribit.com/education/option-backtest-selling-weekend-vol/),
covering 2024-01-13 to 2024-07-27. These contain actual bid, ask, mark, forward and open-interest
observations, rather than reconstructed theoretical option prices. Binance's
[official minute spot archives](https://github.com/binance/binance-public-data) supply the hedge
prices, and Deribit's [delivery-price endpoint](https://docs.deribit.com/api-reference/market-data/public-get_delivery_prices)
supplies the official settlement TWAPs. The third-party panel is not exchange-audited.

The pinned [protocol](docs/historical_protocol.md) defines weekly near-ATM call and put selections,
next-snapshot bid-side entry, delayed minute-open spot execution, BTC inverse-option accounting,
fees, slippage, borrowing sensitivities and a chronological calibration/holdout split. No entered
case is removed because it loses money or develops missing quotes. Full ledgers and opportunity
audits are retained. No API keys or paid subscriptions are needed.

```bash
python -m pip install -e ".[dev,historical]"
crypto-options download-historical --raw-dir data/raw
MPLCONFIGDIR=/tmp/crypto-options-mpl crypto-options backtest-historical \
  --raw-dir data/raw --out-dir artifacts/historical
```

Downloads are local and gitignored. The original Parquet is pinned by SHA-256, Binance ZIPs are
checked against published checksums, and `run_manifest.json` records source and code hashes,
dependencies and assumptions. Public availability is not treated as a redistribution license.
An offline rerun needs only the already-downloaded local files; downloads are a separate command.
The quote-level historical event ledger is generated locally, not redistributed in Git. Its
checksum and validation results accompany the public case and policy summaries.

### Holdout results

The 13-week calibration window selects the 0.05 BTC band from three predeclared candidates.
The following 14-week holdout contains 28 individual option cases. Call and put observations
from the same week are correlated; uncertainty estimates resample paired entry-week blocks.

| Policy | Mean PnL per case (USD) | PnL standard deviation (USD) | Mean hedge cost (USD) | Mean hedge trades |
| --- | ---: | ---: | ---: | ---: |
| Unhedged | -55.23 | 2,267.11 | 0.00 | 0.00 |
| Hourly | -39.77 | 478.40 | 159.63 | 152.00 |
| Four-hour | 34.16 | 500.04 | 95.97 | 40.71 |
| Selected 0.05 BTC band | 75.26 | 519.97 | 98.90 | 21.89 |

**Headline finding, not an alpha claim:** a 0.05 BTC no-trade band, selected using the 13-week
calibration period only, reduces normalized terminal PnL dispersion by about **77.7%** versus no
hedging on the 14-week holdout (28 call/put cases). Compared with hourly hedging it uses **85.6%
fewer hedge trades** and **38.0% lower hedge costs**, but has higher dispersion. The paired weekly
bootstrap interval does not establish lower dispersion than hourly hedging. Higher trading costs
turn its baseline positive average PnL negative. Capital returns and annualized Sharpe are not
reported because there is no collateral/margin model.

![Historical holdout: dispersion and hedge costs](artifacts/historical/historical_holdout.png)

Real-data SVI calibration is a separate held-out-strike validation; the historical hedger uses
observed mid IV and frozen-IV delta, **not an SVI delta**. Of 27 weekly surface attempts, 12 pass
the grid checks, 6 fail the checks and 9 fail to converge. Rejected fits remain in the diagnostics
and are not approved pricing surfaces. See the [historical report](artifacts/historical/REPORT.md),
[methodology](docs/historical_methodology.md) and [protocol](docs/historical_protocol.md).

## Design and validation controls

- **Bounded IV inversion:** rejects premiums outside discounted intrinsic/upper bounds, handles
  zero-volatility and infinite-volatility boundaries explicitly, and uses a bracketed root solver.
- **Quote hygiene:** requires genuine two-sided markets; crossed, non-finite, overly wide, expired,
  and negligible-vega observations receive audit reasons rather than mark-price substitution.
- **Total-variance SVI:** fits `w(k) = sigma_BS(k, T)^2 T`, uses liquidity/IV-spread weights and
  robust loss, and applies soft penalties for negative variance, Lee wing slopes, butterfly
  density, and calendar spread.
- **Post-fit safety gate:** independently checks Gatheral-Jacquier `g(k)` and calendar monotonicity
  on a disclosed log-moneyness grid; strict mode refuses a failing surface.
- **Self-financing hedging:** option premium, spot hedge, bid/ask execution, fees, cash interest,
  underlying carry, expiry settlement, and hedge close-out all flow through one cash account.
- **Structured payoff:** a stablecoin Dual-Currency Investment has explicit settlement-currency
  switching, fair-coupon solving, and price/Greeks through its cash-minus-short-put replication.
- **PnL identities:** the numerical simulator includes Greek attribution with an explicit
  residual. Historical ledgers reconcile to economic PnL components and serialized event cash
  flows; they do not claim historical Greek attribution.
- **Fair policy comparison:** every hedge policy receives the same seeded stochastic spot/vol path.
  In the historical experiment, every policy receives the same selected instruments and observed
  market history, with entry-week paired uncertainty estimates.

## Architecture

```text
Historical option panel + spot OPENs + official delivery prices
             |
             v
 as-of selection -> next-snapshot entry -> delayed hedge replay
                                              |
                                              v
                         cash-flow audit + temporal holdout + cost stress

Independent surface experiment / numerical benchmark:

Deribit REST / synthetic chain
             |
             v
 two-sided quote validation -----> rejection audit
             |
             v
 bounded Black-76 IV inversion
             |
             v
 weighted raw-SVI calibration ----> butterfly/calendar diagnostics
             |                                  |
             +------------------------ strict safety gate

 market event -> option mark/Greeks -> hedge policy -> execution/cash account
       |                                                    |
       +-------------------- PnL attribution <--------------+
```

## Quick start

Requires Python 3.11+.

```bash
git clone https://github.com/OwenWang19/Crypto-Options-Pricing-Volatility-Surface-Dynamic-Hedging-Engine.git
cd Crypto-Options-Pricing-Volatility-Surface-Dynamic-Hedging-Engine
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
make check

# Historical observations with explicit execution assumptions:
crypto-options download-historical
MPLCONFIGDIR=/tmp/crypto-options-mpl crypto-options backtest-historical
python scripts/verify_historical.py

# Separate seeded numerical regression benchmark:
crypto-options demo --out-dir artifacts/reference --paths 250 --steps 180 --seed 17
```

Price and invert a premium directly:

```bash
crypto-options price --forward 60000 --strike 60000 --maturity 0.25 \
  --volatility 0.60 --rate 0.03 --option-type call

crypto-options implied-vol --premium 7100.6678689251 --forward 60000 \
  --strike 60000 --maturity 0.25 --rate 0.03 --option-type call
```

## Synthetic numerical benchmark (not historical results)

The checked-in run uses seed 17, 250 common stochastic-volatility paths, a 30-day short BTC call,
180 four-hour events per path, 1 bp hedge half-spread, and 2 bp hedge fee. Results measure hedge
risk and cost under the simulator; they do **not** represent historical or expected trading profit.

| Policy | Mean PnL | PnL stdev | 5% expected shortfall | Avg. costs | Avg. trades | Stdev reduction |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Unhedged | -$495.01 | $6,989.00 | -$20,852.13 | $0.00 | 0.0 | 0.0% |
| Four-hour | -$137.74 | $502.61 | -$1,348.04 | $99.57 | 164.1 | 92.8% |
| Five-delta band | -$105.77 | $543.53 | -$1,364.30 | $69.20 | 36.8 | 92.2% |

The useful result is the cost-risk trade-off: four-hour hedging had the lowest dispersion, while
the delta-band policy retained most of that reduction with 78% fewer hedge trades. No strategy is
declared "best" without specifying an execution-cost and tail-risk objective.

The surface benchmark accepted 102 of 104 quotes, fitted market IV with **3.58 vol-bp RMSE**, and
recorded zero butterfly/calendar violations on the documented 401-point diagnostic grid.

![Calibrated SVI smiles](artifacts/reference/svi_smiles.png)

![Hedge cost-risk frontier](artifacts/reference/hedge_cost_risk.png)

## Public Deribit snapshot adapter

No credentials are required for public market data:

```bash
crypto-options fetch-deribit --currency BTC --output artifacts/deribit_btc.csv
```

The adapter joins [`public/get_instruments`](https://docs.deribit.com/api-reference/market-data/public-get_instruments),
[`public/get_book_summary_by_currency`](https://docs.deribit.com/api-reference/market-data/public-get_book_summary_by_currency),
and [`public/get_index_price`](https://docs.deribit.com/api-reference/upcoming/market-data/public-get_index_price).
BTC/ETH inverse option premiums are quoted in base currency; the adapter follows Deribit's
convention by multiplying premiums by the spot index for USD-equivalent prices while retaining the
expiry forward for IV. The raw snapshot timestamp and all cleaning rejections remain explicit.
Polling is for research snapshots; Deribit recommends WebSocket subscriptions for real-time systems.

## Validation

```bash
make test       # numerical, property-style, accounting, adapter, and integration tests
make lint       # Ruff formatting and static checks
make check      # both
python -m pytest -W error --cov=crypto_options --cov-fail-under=85
python scripts/verify_historical.py  # after the historical download and backtest
```

The local Python 3.12 run passes **93 tests**, with approximately **91.6% coverage**.
Independent artifact checks cover 2,268 case/policy/scenario rows and 54,420 baseline ledger
events. A second offline hedge replay produced byte-identical case summaries, policy statistics,
compressed ledgers and the result figure. See [verification](artifacts/historical/verification.json)
and [reproduction checks](artifacts/historical/reproduction.json). SVI calibration was not repeated
in that second hedge-only reproducibility check.

The test suite covers:

- put-call parity and Black-76/IV round trips across moneyness and volatility regimes;
- analytic delta/gamma/vega/volga versus central differences;
- SVI first/second derivatives, synthetic recovery, and static-arbitrage diagnostics;
- quote rejection and robust put-call-parity forward recovery;
- Deribit metadata/summary joining and inverse-premium conversion;
- event ordering, exact-expiry settlement, transaction-cost impact, seeded paths, and PnL
  reconciliation;
- end-to-end artifact generation from a clean environment.

## Repository layout

```text
src/crypto_options/
  pricing/black76.py        # price, Greeks, bounds, implied-vol inversion
  marketdata/chain.py       # chain cleaning, weights, parity forwards
  marketdata/deribit.py     # retrying public snapshot adapter
  marketdata/historical.py  # pinned datasets, checksums, UTC normalization, minute OPENs
  surface/svi.py            # raw-SVI fit, interpolation, safety diagnostics
  products/dual_currency.py # stablecoin DCI replication, settlement, Greeks
  hedging/simulator.py      # self-financing event loop and PnL attribution
  hedging/paths.py          # correlated spot/log-vol scenario generator
  hedging/historical.py     # observed-quote replay and BTC inverse-option cash flows
  research/                 # reproducible benchmark and artifact production
scripts/verify_historical.py # independent provenance, timing and accounting checks
tests/                      # unit, numerical, accounting, and integration checks
docs/methodology.md         # equations, event order, assumptions, limitations
docs/historical_protocol.md # selection rules, execution assumptions and temporal split
docs/historical_methodology.md # inverse-option equations and statistical interpretation
artifacts/reference/        # checked-in seeded outputs and ledgers
artifacts/historical/       # real-data reports, case statistics and validation records
```

## What this project does not claim

- The synthetic benchmark is not a historical backtest and contains no alpha claim. Historical
  results are stored separately; never substitute the synthetic 92.2% reduction for a historical
  estimate.
- Historical option snapshots do not include available size, exchange update timestamps or L2
  books. Bid-side fills and next-minute spot OPEN executions are indicative assumptions, not
  verified fill opportunities. BTC/USD and BTC/USDT are different markets; USD/USDT parity and
  borrowing availability are explicitly assumed, not measured away.
- SVI penalties and grid diagnostics reduce detected static arbitrage; they are not a proof over
  every real number or a replacement for production surface governance.
- The hedge simulator uses a spot hedge. Perpetual funding, margin, liquidation, exchange outages,
  and order-book impact require separate models before live use.
- A multi-request REST snapshot is not atomic and is not suitable for latency-sensitive trading.
- Black-76 is a quoting/risk convention; it does not capture crypto jumps, stochastic rates,
  discrete settlement effects, or smile dynamics by itself.

See [methodology](docs/methodology.md) for equations and [model validation](docs/model_validation.md)
for control objectives and failure-mode tests.
