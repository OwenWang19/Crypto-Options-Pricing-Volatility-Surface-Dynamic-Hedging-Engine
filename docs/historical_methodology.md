# Historical methodology and model-risk notes

## Research question

The experiment measures the trade-off between residual option exposure and the cost of
spot-delta hedging. Historical hedging and cross-sectional SVI validation are separate
experiments: the historical hedger uses observed mid IV with frozen-IV Greeks, not an SVI delta.
The synthetic 92.2% dispersion reduction and 3.58 vol-bp fit error are not historical estimates.

## Data lineage and availability

The source panel contains 4,498,832 recorded BTC option observations from January–July 2024,
recorded by Cryptarbitrage and linked by Deribit Insights. It contains bid, ask, mark,
forward and open interest, but no quoted size, L2 depth or exchange update timestamps.
Collection time is interpreted as UTC, not as a guarantee of a freshly updated quote.

Binance's official BTCUSDT minute archives supply spot prices; published SHA-256 files are
verified. Official Deribit delivery prices supply the expiry reference. The original option
Parquet is pinned by SHA-256. Integrity checks are not an independent audit of the recorder.

Raw files and the generated quote-level event ledger remain local. The repository publishes
derived case summaries, diagnostics and checksums. Source availability is not treated as a
blanket redistribution license. Run the documented download and backtest commands to recreate
the full accounting evidence before running `scripts/verify_historical.py`.

## Selection and chronology

At 09:00 UTC each Friday, select the next Friday's near-ATM call and put using observations
already recorded by selection time. The instrument is fixed before seeing its next snapshot.
Entry uses that next snapshot's bid with an adverse premium haircut. An unusable entry is
logged as skipped; it is not replaced by a contract selected with future information.

There are 54 cases across 27 entry weeks. Thirteen weeks select a band from 0.02, 0.05 and
0.10 BTC using normalized terminal-PnL RMSE. The band is then frozen for fourteen holdout weeks.
This is retrospective temporal validation, not an externally registered blind experiment.

Every hedge decision precedes its execution proxy. Only an already-recorded option quote
can inform delta; execution occurs at a strictly later minute OPEN. That minute's future
close, high and low are never used to trade at its opening price. Option observations are
approximately hourly, so the band policy is not continuously monitored.

## BTC inverse-option units

For a BTC-denominated premium `p_BTC`, expiry forward `F`, strike `K` and maturity `T`:

```text
p_BTC = Black76(F, K, T, sigma, discount=1) / F
USD-equivalent mark = spot * p_BTC
```

Equivalently, the USD-equivalent Black price uses discount `spot/F`. A BTC-denominated
premium cannot be passed directly into a USD pricer without converting its units.

Holding IV and the forward/spot ratio fixed, USD spot delta is `N(d1)` for a call and
`N(d1)-1` for a put. This is not the derivative of the BTC premium alone. The accounting
converts entry premium into USD-equivalent cash rather than adding an unmodelled speculative
BTC collateral balance.

For a call, the official expiry liability per contract is:

```text
payoff_BTC = max(delivery_price - K, 0) / delivery_price
payment_USD = payoff_BTC * independently_observed_expiry_spot
```

The delivery TWAP is not interchangeable with spot execution price. Conversions and hedge
close-out have explicit costs. USD/USDT parity and timely cross-venue funding are assumptions.

## Missing quotes and accounting

Entered cases are not removed because a later quote disappears or PnL is negative. When
IV cannot be inferred reliably, the prior delta is frozen. Once it exceeds the configured
age limit, rebalancing stops. The ledger counts frozen and stale signals. Exchange or stale
marks may support interim valuation but never replace an executable option entry price.

Borrowing and short-spot financing are explicit sensitivities. The engine does not verify
borrow availability or recreate collateral, margin calls and liquidation. Ordinary delta
rebalancing pauses before the settlement-TWAP window; a vanilla delta is not presented as
a complete model of the averaging payoff.

Two independent checks reconcile the accounting:

```text
terminal PnL
  = entry premium - settlement - option fees - conversion costs
    + spot hedge PnL + financing - hedge execution costs

cash at each event
  = cumulative serialized premium, settlement, spot, financing and fee cash flows
```

Reconciliation validates bookkeeping, not pricing accuracy or achievable fills.

## Statistical interpretation

Case counts and entry-week counts are reported together. Calls and puts from the same week
are correlated. The bootstrap resamples both sides and all policies together by entry week;
it does not treat 28 holdout cases as 28 independent weeks. Blocks may still have serial
dependence, and a six-month sample cannot establish rare-tail safety.

PnL normalized by entry BTC notional is not a capital return. No annualized Sharpe,
leverage benefit or capital-return estimate is asserted without a collateral model.
The selected band saves hedge trades and cost relative to hourly hedging but has higher
residual dispersion. Higher costs turn average PnL negative. These findings remain visible.

## Surface validation

Weekly calibration fits contemporaneous maturity slices and holds out internal strikes.
Calls and puts sharing a strike remain in one split. The final experiment uses OTM sides
and a minimum normalized-vega filter to reduce deep-ITM IV inversion instability. This
was a post-diagnostic refinement, and the initial all-quote results are preserved.

All 27 attempts are counted: 12 pass the grid checks, 6 fail them and 9 do not converge.
Accepted-date held-out error is conditional, not full-sample accuracy. Rejected fits are
not approved pricing surfaces. Finite-grid diagnostics are not a global no-arbitrage proof.

See the [results report](../artifacts/historical/REPORT.md),
[protocol](historical_protocol.md) and [validation controls](model_validation.md).
